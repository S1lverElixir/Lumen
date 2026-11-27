"""lumen_images.py — text-to-image: Cloudflare Workers AI is the primary provider, Pollinations.ai the fallback.
The module is isolated: it does not write chat_state/quotas or call Telegram, and receives the session as a parameter.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

log = logging.getLogger("bot")

# Live measurement 09.11.2026: image.pollinations.ai ignores model (always sana; the catalog
# /models = ["sana"]) and nologo (the watermark remains on the response), so the style
# the five-model catalog and prompt-based selection were removed — they selected nothing.
POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt"
POLLINATIONS_IMAGE_MODEL = (os.getenv("POLLINATIONS_IMAGE_MODEL", "").strip() or "sana")
CLOUDFLARE_AI_BASE_URL = (os.getenv("CLOUDFLARE_AI_BASE_URL", "").strip() or "https://api.cloudflare.com/client/v4/accounts").rstrip("/")
# Live measurement 10.11.2026: flux-2-klein-4b draws and edits with ONE model (input_image_0),
# accepts a Russian prompt directly and 1024x1024 input without resizing; ~104 Neuron per
# a 1024x1024 image versus 1364 at klein-9b. flux-1-schnell remains only
# the variable in case of rollback; generation does not use it by default.
CLOUDFLARE_IMAGE_MODEL = (os.getenv("CLOUDFLARE_IMAGE_MODEL", "").strip() or "@cf/black-forest-labs/flux-2-klein-4b")
CLOUDFLARE_EDIT_MODEL = (os.getenv("CLOUDFLARE_EDIT_MODEL", "").strip() or CLOUDFLARE_IMAGE_MODEL)
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
# Application name for Pollinations: their privacy page says that anonymous traffic
# Do not accept "referring site or app name" as a prompt: the model used that
# technical field as the subject of the picture.
IMAGE_APP_REFERRER = (os.getenv("IMAGE_APP_REFERRER", "").strip() or "lumen-telegram-bot")

# One limit for both providers: the fallback must accept the same text as the primary.
# Cloudflare supports 2048, but we cap below that: the prompt goes into the reserve provider's URL.
IMAGE_PROMPT_MAX_CHARS = 600
# Read the stream with a cap: uncontrolled resp.read() accumulated the body in memory
# of any size from someone else's service.
IMAGE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_URL_ENCODED_MAX_CHARS = 4000
# Responses from both providers are JPEG; PNG/WebP are allowed, GIF is not: sendPhoto, animation
# does not accept, and these APIs do not return a static GIF (audit 09.11.2026).
_IMAGE_SIGNATURES = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"RIFF", ".webp"))


class ImageProviderError(RuntimeError):
    """Image-provider failure. The text is for the log; the user sees a classified message."""


class ImageRateLimitedError(ImageProviderError):
    """429/402 FREE_LIMIT: the provider is alive but cannot keep up. retry_after_sec comes from the header."""

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class ImageModerationError(ImageProviderError):
    """Provider content filter: not a network failure and not a budget issue. The fallback can draw the same content without filtering, so the chain must stop (review 10.11.2026)."""


class ImageBudgetExhaustedError(ImageProviderError):
    """The provider's free daily budget is used up — retrying before the next day is pointless."""


def cloudflare_configured() -> bool:
    return bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN)


def image_provider_names() -> tuple[str, ...]:
    """Order = fallback order. If Cloudflare is not configured, only Pollinations remains."""
    names = ["cloudflare"] if cloudflare_configured() else []
    names.append("pollinations")
    return tuple(names)


def _random_seed() -> int:
    """Without a seed, Pollinations returns seed=42 and a byte-for-byte identical
    image when the prompt is repeated (measured 09.11.2026); without a random
    seed, the feature appears broken."""
    return secrets.randbelow(2_000_000_000) + 1


def image_filename(data: bytes) -> str | None:
    """File name derived from magic bytes rather than always .jpg: the name goes into multipart and
Telegram uses it to determine the format. None means a format sendPhoto will not accept."""
    for signature, ext in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return f"generated{ext}"
    return None


def _error_strings(payload: Any, cap: int | None = 400) -> str:
    """Combines error messages from a JSON response into one text: Cloudflare uses
    errors:[{code,message}], Pollinations uses a single error:{code,message}
    (which carries the FREE_LIMIT code), and the Workers AI wrapper text carries messages[]
    and result. Parses only dict/list, without traversing arbitrary foreign JSON."""
    out: list[str] = []

    def _append_item(item: dict[str, Any]) -> None:
        code = item.get("code")
        if isinstance(code, (str, int)):
            out.append(str(code))
        message = item.get("message")
        if isinstance(message, str):
            out.append(message)

    def _error_items(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, (dict, str)):
            return [value]
        return []
    if isinstance(payload, dict):
        for item in _error_items(payload.get("errors")):
            if isinstance(item, dict):
                _append_item(item)
            elif isinstance(item, str):
                out.append(item)
        error = payload.get("error")
        if isinstance(error, dict):
            _append_item(error)
        elif isinstance(error, str):
            out.append(error)
        if isinstance(payload.get("message"), str):
            out.append(payload["message"])
        # In the Workers AI wrapper, refusal text also lives in messages[] and in result —
        # a string or short dictionary fields. Long ones are excluded: detail goes into
        # the log, while result contains the base64 image on success (review 10.11.2026).
        for item in _error_items(payload.get("messages")):
            if isinstance(item, dict):
                _append_item(item)
            elif isinstance(item, str):
                out.append(item)
        result = payload.get("result")
        if isinstance(result, dict):
            values = [v for k, v in result.items() if k != "image"]
        elif isinstance(result, str):
            values = [result]
        else:
            values = []
        out.extend(v for v in values if isinstance(v, str) and len(v) < 120)
    joined = " ".join(out)
    return joined if cap is None else joined[:cap]


def _error_strings_full(data: Any) -> str:
    """One retry on status 400: that status is a transient backend failure rather than
    a bad request; on 403/500 it is not retried (measurement 17.11.2026)."""
    return _error_strings(data, cap=None)


def _retry_after_sec(resp: aiohttp.ClientResponse) -> float | None:
    """Retry-After can be either seconds or an HTTP-date (RFC 9110). A bare float() on a date
    returned None, and a 402 without the FREE_LIMIT code was falsely reported as
    “budget selected until tomorrow.”"""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is not None:
        # inf/1e309 — garbage header: use a daily cap instead of a permanent cooldown.
        # None here would mean “no header”, and a 402 without FREE_LIMIT would
        # into "budget exhausted" (review 17.11.2026). nan does not equal itself.
        if seconds != seconds:
            return 0.0
        return min(max(0.0, seconds), 86400.0)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


async def _read_capped_image(resp: aiohttp.ClientResponse) -> bytes | None:
    """Body with a refusal when the cap is exceeded (None): Content-Length lies,
    so verify the stream as it arrives."""
    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > IMAGE_MAX_BYTES:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > IMAGE_MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _error_detail(resp: aiohttp.ClientResponse) -> str:
    """Apply the same cap to provider error text as to the image: the error branch need
    not load an unbounded third-party response into memory (audit 09.11.2026)."""
    if int(resp.status) not in (402, 429):
        return ""
    with contextlib.suppress(Exception):
        body = await _read_capped_image(resp)
        if body is not None:
            return _error_strings(json.loads(body.decode("utf-8", "replace")))
    return ""


def _encoded_prompt(prompt: str) -> str:
    """The prompt goes into the URL path, so it must be capped by length AFTER percent-encoding: 600 emoji is about 7200 characters, and the CDN returns 414. Truncate character by character so a %-sequence is not split."""
    pieces: list[str] = []
    used = 0
    for ch in prompt[:IMAGE_PROMPT_MAX_CHARS]:
        piece = urllib.parse.quote(ch, safe="")
        if used + len(piece) > IMAGE_URL_ENCODED_MAX_CHARS:
            break
        pieces.append(piece)
        used += len(piece)
    return "".join(pieces)


def _timeout(total_sec: float | None) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=total_sec if total_sec is not None else 90.0, sock_connect=12.0)


def _take_image_bytes(body: bytes, ctype: str, provider: str) -> bytes:
    """Whether the response is really an image: a third-party Content-Type is no more
    reliable than magic bytes, so trust either one (regression AUD-E-001)."""
    if not body:
        raise ImageProviderError(f"{provider} вернул пустое тело")
    if image_filename(body) is not None:
        return body
    if ctype.startswith("image/"):
        raise ImageProviderError(f"{provider} вернул формат, который sendPhoto не примет: {ctype}")
    raise ImageProviderError(f"{provider} вернул не-изображение: {ctype}")


# Aspect ratios as in the ChatGPT generator: size is set by a marker in the prompt.
# All variants are about 1 megapixel (21:9 is 0.77 MP), so the price in Neuron does not grow
# (measurement 10.11.2026).
_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "1:1": (1024, 1024),
    # 3:2 and 4:5 are ChatGPT formats for a poster and a story portrait; sides are
    # multiples
    # 32 and the area is back to ~1 MP, so the price in Neuron does not jump
    # (10.11.2026).
    "3:2": (1248, 832),
    "2:3": (832, 1248),
    "4:5": (896, 1120),
    "5:4": (1120, 896),
    "21:9": (1344, 576),
}
_SIZE_WORDS: tuple[tuple[str, str], ...] = (
    ("16:9", "16:9"), ("16х9", "16:9"), ("16x9", "16:9"),
    ("9:16", "9:16"), ("9х16", "9:16"), ("9x16", "9:16"),
    ("4:3", "4:3"), ("4х3", "4:3"), ("4x3", "4:3"),
    ("3:4", "3:4"), ("3х4", "3:4"), ("3x4", "3:4"),
    ("1:1", "1:1"), ("3:2", "3:2"), ("2:3", "2:3"), ("3х2", "3:2"), ("2х3", "2:3"),
    ("3x2", "3:2"), ("2x3", "2:3"), ("4:5", "4:5"), ("5:4", "5:4"), ("4х5", "4:5"),
    ("5х4", "5:4"), ("4x5", "4:5"), ("5x4", "5:4"),
    ("21:9", "21:9"), ("21x9", "21:9"), ("21х9", "21:9"),
    ("горизонтальн", "16:9"),
    ("вертикальн", "9:16"),
    ("широкоформатн", "16:9"),
    # Only whole phrases: individual "wide"/"tall"/"portrait" words describe the scene
    # (“wide smile”, “tall hat”, “portrait of a woman”) and must not change the frame.
    ("панорамн", "16:9"), ("во всю ширину", "16:9"), ("widescreen", "16:9"),
    ("wide format", "16:9"), ("landscape format", "16:9"),
    ("landscape orientation", "16:9"),
    ("в полный рост", "9:16"), ("вертикальный формат", "9:16"),
    ("vertical format", "9:16"), ("vertical video", "9:16"), ("tall format", "9:16"),
    ("portrait format", "4:5"), ("square format", "1:1"),
    ("квадрат", "1:1"),
    # English "portrait"/"landscape" — genre, not orientation ("portrait of a woman"):
    # do not force a size, or every portrait would come out vertical (review 10.11.2026).
    ("square", "1:1"),
)


# "Make a poster", "phone wallpaper", "website banner" — the person expects
# the frame customary for this artifact, as in ChatGPT, rather than a square. Look after
# explicit aspect ratios, so "16:9 poster" remains 16:9.
# Patterns — with word boundaries and a required link word for ambiguous
# English nouns: "imposter" and "posterior view" are not posters, while "a cat
# telling stories" and "fisherman with reels" describe a subject, not a format
# (review 10.11.2026).
_SIZE_DEFAULTS: tuple[tuple[str, str], ...] = (
    (r"обои на (?:телефон|смартфон)|phone wallpaper", "9:16"),
    (r"\bсторис\b|(?:для|for)\s+(?:моей\s+|my\s+)?(?:сторис|story|stories|reels?)\b", "9:16"),
    (r"\b(?:instagram|insta)\s+(?:story|stories|reels?)\b|\breels?\b[\s-]*(?:формат|format)", "9:16"),
    (r"\btiktok\b", "9:16"),
    (r"в полный рост", "9:16"),
    # Stems with \w*: "cat on a poster", "column in a banner" are the same formats as
    # and in the nominative case (review 10.11.2026).
    (r"\b(?:постер\w*|плакат\w*|обложк\w*|posters?|book cover)\b", "2:3"),
    (r"\b(?:баннер\w*|превью|banners?|thumbnails?)\b", "16:9"),
    (r"(?:desktop|рабочего стол\w*) wallpaper|wallpaper for desktop"
     r"|обои\s+(?:на|для|в)\s+(?:рабоч(?:ий|его|ем))?\s*стол\w*", "16:9"),
    (r"\b(?:логотип|эмблем\w*|аватар\w*|стикер\w*|наклейк\w*|logos?|avatars?|stickers?)\b", "1:1"),
)


def detect_image_size(prompt: str) -> tuple[int, int]:
    """Size from the prompt marker; defaults to 1024x1024. Only explicit aspect
    formats/words are matched so "wide range" does not become 16:9. Formats are
    matched with digit boundaries; otherwise "13:45" contains "3:4" (found in
    review 10.11.2026)."""
    low = (prompt or "").lower()
    for token, key in _SIZE_WORDS:
        if any(c.isdigit() for c in token) and any(c in token for c in (":", "х", "x")):
            if re.search(r"(?<!\d)" + re.escape(token) + r"(?!\d)", low):
                return _SIZES[key]
        elif token in low:
            return _SIZES[key]
    for pattern, key in _SIZE_DEFAULTS:
        if re.search(pattern, low):
            return _SIZES[key]
    return _SIZES["1:1"]


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width/height from the file header without Pillow: edits must preserve the
    reference's proportions (measurement 10.11.2026: a 16:9 photo came out square)."""
    try:
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xDA:
                    # SOF always precedes SOS; after it comes entropy, and a byte-by-byte scan
                    # 8-10 MB held the GIL for 0.4-0.5 s (review 17.11.2026).
                    return None
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w, h) if w and h else None
                if length < 2:
                    return None
                i += 2 + length
            return None
        if data.startswith(b"\x89PNG") and len(data) >= 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h) if w and h else None
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
            if data[12:16] == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return (w, h)
            if data[12:16] == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
            if data[12:16] == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    except Exception:
        return None
    return None


def size_for_reference(dimensions: tuple[int, int] | None) -> tuple[int, int]:
    """Edit output size: the supported aspect ratio closest to the reference frame
    (including 4:3 and 3:4); unknown dimensions produce a square."""
    if not dimensions:
        return (1024, 1024)
    w, h = dimensions
    if not w or not h:
        return (1024, 1024)
    ratio = w / h
    # All supported aspect ratios, not just five: 3:2/21:9 previously shifted to 4:3/16:9
    # and the frame drifted during editing (review 17.11.2026).
    target = {name: int(name.split(":")[0]) / int(name.split(":")[1]) for name in _SIZES}
    best = min(_SIZES, key=lambda name: abs(math.log(ratio / target[name])))
    return _SIZES[best]


def _image_content_type(data: bytes) -> str:
    """Reference type by magic bytes: the filename in FormData is arbitrary."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _cloudflare_model_url(model: str) -> str:
    # safe="/@": Cloudflare expects the model in the path as `@cf/name/model`; bare quote()
    # would turn the slash into %2F and get a 404 for a model known to exist.
    return (
        f"{CLOUDFLARE_AI_BASE_URL}/{urllib.parse.quote(CLOUDFLARE_ACCOUNT_ID, safe='')}"
        f"/ai/run/{urllib.parse.quote(model, safe='/@')}"
    )


async def _cloudflare_request(
    session: aiohttp.ClientSession, url: str, *, form: aiohttp.FormData,
    timeout_sec: float | None,
) -> tuple[int, float | None, Any]:
    """One fully read and closed multipart POST: (status, retry_after, JSON).
    The error body is read with the same cap as the image and is parsed even on errors —
    Cloudflare reports its daily budget in the errors text on HTTP 429.
    Measured 10.11.2026: flux-2-klein accepts ONLY multipart; it rejects a JSON body with
    5006 "required properties at '/' are 'multipart'"."""
    async with session.post(
        url, data=form, timeout=_timeout(timeout_sec),
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
    ) as resp:
        status = int(resp.status)
        retry_after = _retry_after_sec(resp)
        body = await _read_capped_image(resp)
        if body is None:
            raise ImageProviderError("Cloudflare вернул тело больше капа")
        data: Any = None
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(body)
        return status, retry_after, data


_MODERATION_MARKERS = ("flagged", "moderation")


def _is_moderation_detail(detail: str) -> bool:
    """Classify an image-provider refusal (measured 10.11.2026: one case used code 3030
    "Your output has been flagged")."""
    return any(word in (detail or "").lower() for word in _MODERATION_MARKERS)


def _cloudflare_image_result(status: int, retry_after: float | None, data: Any) -> bytes:
    """Common decoding of a flux-2-klein response: success, budget, pacing, moderation."""
    full_detail = _error_strings_full(data)
    detail = full_detail[:400]
    if status == 200 and not isinstance(data, dict):
        raise ImageProviderError("Cloudflare вернул JSON не-объектом")
    if status == 402:
        raise ImageBudgetExhaustedError(f"Cloudflare: бесплатный бюджет выбран (HTTP 402 {detail})")
    if status == 429:
        # Cloudflare also returns the same 429s when the daily Neuron budget is exhausted.
        if any(word in detail.lower() for word in ("allocation", "free tier", "daily")):
            raise ImageBudgetExhaustedError(f"Cloudflare: бесплатный бюджет выбран (HTTP 429 {detail})")
        raise ImageRateLimitedError(f"Cloudflare HTTP 429 {detail}".strip(), retry_after_sec=retry_after)
    # Content filter (measurement 10.11.2026: code 3030 "Your output has been
    # flagged"): this is not a network failure; retries and fallback will not help,
    # so the user
    # is more honest than "generation error, rephrase it." The same flag also arrives in the body
    # (Workers AI wrapper) on any status, so check before the status branches:
    # otherwise sana would draw in the rejected content (review 10.11.2026).
    if (status != 200 or (isinstance(data, dict) and not data.get("success", True)))             and _is_moderation_detail(full_detail):
        # Full, untruncated text: otherwise "flagged" beyond the 400 cap is invisible,
        # and 403/500 fell back to the reserve (review 17.11.2026).
        raise ImageModerationError(f"Cloudflare отклонил содержимое по модерации: {full_detail[:200]}")
    if status != 200:
        raise ImageProviderError(f"Cloudflare HTTP {status} {detail}".strip())
    if not data.get("success", True):
        raise ImageProviderError(f"Cloudflare: {detail}")
    result = data.get("result")
    image_b64 = result.get("image") if isinstance(result, dict) else None
    if not isinstance(image_b64, str) or not image_b64:
        raise ImageProviderError("Cloudflare: в ответе нет поля result.image")
    try:
        image = base64.b64decode(image_b64)
    except (ValueError, TypeError):
        raise ImageProviderError("Cloudflare: result.image не декодируется из base64") from None
    return _take_image_bytes(image, "image/jpeg", "Cloudflare")


def _cloudflare_form(
    prompt: str, *, reference: bytes | None = None,
    second_reference: bytes | None = None,
    width: int = 1024, height: int = 1024,
) -> aiohttp.FormData:
    """klein request body: prompt/width/height/seed plus a reference image for edits.
    Dimensions 256..1920; about one megapixel is the cheap budget unit
    (4*26.05 = 104 Neurons for a 1024x1024 image)."""
    form = aiohttp.FormData()
    form.add_field("prompt", prompt[:IMAGE_PROMPT_MAX_CHARS])
    form.add_field("width", str(width))
    form.add_field("height", str(height))
    form.add_field("seed", str(_random_seed()))
    for index, data in enumerate((reference, second_reference)):
        if not data:
            continue
        form.add_field(
            f"input_image_{index}", data,
            filename=f"reference{index}" + (".png" if data.startswith(b"\x89PNG") else ".jpg"),
            content_type=_image_content_type(data),
        )
    return form


async def _cloudflare_text_to_image(
    session: aiohttp.ClientSession, prompt: str, *,
    timeout_sec: float | None = None, width: int = 1024, height: int = 1024,
) -> bytes:
    """Generation from scratch via Workers AI (multipart)."""
    if not cloudflare_configured():
        raise ImageProviderError("Cloudflare не настроен: нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN")
    status, retry_after, data = await _cloudflare_request(
        session, _cloudflare_model_url(CLOUDFLARE_IMAGE_MODEL),
        form=_cloudflare_form(prompt, width=width, height=height), timeout_sec=timeout_sec,
    )
    return _cloudflare_image_result(status, retry_after, data)


async def _cloudflare_edit_to_image(
    session: aiohttp.ClientSession, prompt: str, reference: bytes, *,
    second_reference: bytes | None = None,
    timeout_sec: float | None = None, width: int = 1024, height: int = 1024,
) -> bytes:
    """Editing an existing image: the reference is sent in input_image_0 (measured 10.11.2026: red apple -> green; 1024x1024 input is accepted as-is)."""
    if not cloudflare_configured():
        raise ImageProviderError("Cloudflare не настроен: нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN")
    if not reference:
        raise ImageProviderError("Cloudflare edit: пустой референс")
    status, retry_after, data = await _cloudflare_request(
        session, _cloudflare_model_url(CLOUDFLARE_EDIT_MODEL),
        form=_cloudflare_form(
            prompt, reference=reference, second_reference=second_reference,
            width=width, height=height,
        ),
        timeout_sec=timeout_sec,
    )
    return _cloudflare_image_result(status, retry_after, data)


async def _pollinations_text_to_image(session: aiohttp.ClientSession, prompt: str, *, timeout_sec: float | None = None) -> bytes:
    """The classic keyless endpoint is reserve only. Measured 09.11.2026: 1 image per
    45 seconds per IP (HTTP 402 FREE_LIMIT with Retry-After), model always sana, nologo
    ignored, so the watermark remains."""
    if not (prompt or "").isascii():
        # sana turns Cyrillic into '?' during URL parsing and draws random
        # (proof 09.11.2026): refusing without translation is more honest than a garbage image for
        # daily budget count (review 10.11.2026).
        raise ImageProviderError("Pollinations: описание не переведено на английский")
    encoded = _encoded_prompt(prompt)
    # safe=true: without it, the NSFW filter is disabled (according to their docs),
    # and the bot is public.
    url = (
        f"{POLLINATIONS_IMAGE_URL}/{encoded}"
        f"?width=1024&height=1024&model={urllib.parse.quote(POLLINATIONS_IMAGE_MODEL, safe='')}"
        f"&nologo=true&enhance=false&safe=true&seed={_random_seed()}"
        f"&referrer={urllib.parse.quote(IMAGE_APP_REFERRER, safe='')}"
    )
    async with session.get(url, timeout=_timeout(timeout_sec)) as resp:
        status = int(resp.status)
        if status == 200:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = await _read_capped_image(resp)
            if body is None:
                raise ImageProviderError("Pollinations вернул изображение больше капа")
            return _take_image_bytes(body, ctype, "Pollinations")
        retry_after = _retry_after_sec(resp)
        detail = await _error_detail(resp)
        if status == 429:
            raise ImageRateLimitedError(f"Pollinations HTTP 429 {detail}".strip(), retry_after_sec=retry_after)
        if status == 402:
            # FREE_LIMIT — the per-IP rate, i.e., overload protection; without it — an exhausted budget.
            if "FREE_LIMIT" in detail or retry_after is not None:
                raise ImageRateLimitedError(f"Pollinations HTTP 402 {detail}".strip(), retry_after_sec=retry_after)
            raise ImageBudgetExhaustedError(f"Pollinations HTTP 402 {detail}".strip())
        raise ImageProviderError(f"Pollinations.ai HTTP {status}")
