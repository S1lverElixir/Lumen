"""
lumen_images.py — text-to-image: Cloudflare Workers AI основной провайдер, Pollinations.ai резерв.
Модуль изолирован: не пишет в chat_state/квоту, не зовёт Telegram, сессию принимает параметром.
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

# Замер 09.11.2026 живьём: image.pollinations.ai игнорирует model (всегда sana, каталог
# /models = ["sana"]) и nologo (водяной знак остаётся на ответе), поэтому стилевой
# каталог из пяти моделей и подбор по промпту убраны — они ничего не выбирали.
POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt"
POLLINATIONS_IMAGE_MODEL = (os.getenv("POLLINATIONS_IMAGE_MODEL", "").strip() or "sana")
CLOUDFLARE_AI_BASE_URL = (os.getenv("CLOUDFLARE_AI_BASE_URL", "").strip() or "https://api.cloudflare.com/client/v4/accounts").rstrip("/")
# Замер 10.11.2026 живьём: flux-2-klein-4b рисует и правит ОДНОЙ моделью (input_image_0),
# принимает русский промпт напрямую и вход 1024x1024 без ужатия; ~104 Neuron за
# картинку 1024x1024 против 1364 у klein-9b. flux-1-schnell остаётся только
# переменной на случай отката, генерация через него по умолчанию не идёт.
CLOUDFLARE_IMAGE_MODEL = (os.getenv("CLOUDFLARE_IMAGE_MODEL", "").strip() or "@cf/black-forest-labs/flux-2-klein-4b")
CLOUDFLARE_EDIT_MODEL = (os.getenv("CLOUDFLARE_EDIT_MODEL", "").strip() or CLOUDFLARE_IMAGE_MODEL)
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
# Имя приложения для Pollinations: их privacy-страница пишет, что анонимный трафик
# логируется вместе с "referring site or app name" — без него запрос неотличим от чужого скрипта.
IMAGE_APP_REFERRER = (os.getenv("IMAGE_APP_REFERRER", "").strip() or "lumen-telegram-bot")

# Один лимит на обоих провайдеров: резерв обязан принять тот же текст, что основной.
# Cloudflare умеет 2048, капаем ниже — промпт уходит в URL резервного провайдера.
IMAGE_PROMPT_MAX_CHARS = 600
# Читаем потоково с капом: бесконтрольный resp.read() складывал в память тело
# любого размера от чужого сервиса.
IMAGE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_URL_ENCODED_MAX_CHARS = 4000
# Ответы обоих провайдеров — JPEG; PNG/WebP допускаем, GIF нет: sendPhoto анимацию
# не принимает, а статичный GIF такие API не отдают (аудит 09.11.2026).
_IMAGE_SIGNATURES = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"RIFF", ".webp"))


class ImageProviderError(RuntimeError):
    """Сбой провайдера картинок. Текст — для лога; человеку показывается классифицированный текст."""


class ImageRateLimitedError(ImageProviderError):
    """429/402 FREE_LIMIT: провайдер жив, но не тянет темп. retry_after_sec — из заголовка."""

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class ImageBudgetExhaustedError(ImageProviderError):
    """Бесплатный дневной бюджет провайдера выбран — до следующих суток пробовать бессмысленно."""


def cloudflare_configured() -> bool:
    return bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN)


def image_provider_names() -> tuple[str, ...]:
    """Порядок = порядок фолбэка. Cloudflare не настроен — остаётся один Pollinations."""
    names = ["cloudflare"] if cloudflare_configured() else []
    names.append("pollinations")
    return tuple(names)


def _random_seed() -> int:
    """Pollinations без seed отдаёт seed=42 и байт-в-байт ту же картинку на повторе
    промпта (замер 09.11.2026) — без случайного seed фича выглядит сломанной."""
    return secrets.randbelow(2_000_000_000) + 1


def image_filename(data: bytes) -> str | None:
    """Имя файла по магическим байтам, а не всегда .jpg: имя уходит в multipart и по
    нему Telegram определяет формат. None — формат, который sendPhoto не примет."""
    for signature, ext in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return f"generated{ext}"
    return None


def _error_strings(payload: Any) -> str:
    """Сообщения об ошибке из JSON-ответа одним текстом: у Cloudflare это
    errors:[{code,message}], у Pollinations — одиночный error:{code,message}
    (в нём и живёт код FREE_LIMIT). Разбор только dict/list, без обхода
    произвольного чужого JSON."""
    out: list[str] = []
    if isinstance(payload, dict):
        for item in payload.get("errors") or []:
            if isinstance(item, dict):
                out.append(f"{item.get('code')} {item.get('message')}")
            elif isinstance(item, str):
                out.append(item)
        error = payload.get("error")
        if isinstance(error, dict):
            out.append(f"{error.get('code')} {error.get('message')}")
        elif isinstance(error, str):
            out.append(error)
        if isinstance(payload.get("message"), str):
            out.append(payload["message"])
    return " ".join(out)[:400]


def _retry_after_sec(resp: aiohttp.ClientResponse) -> float | None:
    """Retry-After бывает и секундами, и HTTP-date (RFC 9110). Голое float() на дате
    давало None, и 402 без кода FREE_LIMIT ложно становился «бюджет выбран до завтра»."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
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
    """Тело с отказом при превышении капа (None): Content-Length врёт — поток
    перепроверяем по факту."""
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
    """Текст ошибки провайдера под тем же капом, что и картинка: error-ветка не
    обязана лить в память чужое тело без предела (аудит 09.11.2026)."""
    if int(resp.status) not in (402, 429):
        return ""
    with contextlib.suppress(Exception):
        body = await _read_capped_image(resp)
        if body is not None:
            return _error_strings(json.loads(body.decode("utf-8", "replace")))
    return ""


def _encoded_prompt(prompt: str) -> str:
    """Промпт уходит в путь URL, поэтому каппировать надо по длине ПОСЛЕ
    percent-encoding: 600 эмодзи это ~7200 символов, и CDN отвечает 414. Отсечка
    посимвольная, чтобы не разорвать %-последовательность."""
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
    """Что в ответе действительно картинка: Content-Type чужого сервиса не надёжнее
    магических байтов, верим любому из двух (регрессия AUD-E-001)."""
    if not body:
        raise ImageProviderError(f"{provider} вернул пустое тело")
    if image_filename(body) is not None:
        return body
    if ctype.startswith("image/"):
        raise ImageProviderError(f"{provider} вернул формат, который sendPhoto не примет: {ctype}")
    raise ImageProviderError(f"{provider} вернул не-изображение: {ctype}")


# Пропорции как в генераторе ChatGPT: размер задаётся маркером в промпте.
# Все варианты ≈1 мегапиксель, чтобы цена в Neuron не росла (замер 10.11.2026).
_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "1:1": (1024, 1024),
}
_SIZE_WORDS: tuple[tuple[str, str], ...] = (
    ("16:9", "16:9"), ("16х9", "16:9"),
    ("9:16", "9:16"), ("9х16", "9:16"),
    ("4:3", "4:3"), ("4х3", "4:3"),
    ("3:4", "3:4"), ("3х4", "3:4"),
    ("1:1", "1:1"),
    ("горизонтальн", "16:9"),
    ("вертикальн", "9:16"),
    ("широкоформатн", "16:9"),
    ("квадрат", "1:1"),
    # Английские "portrait"/"landscape" — жанр, а не ориентация ("portrait of a woman"):
    # размер не навязываем, иначе каждый портрет выходил вертикальным (ревью 10.11.2026).
    ("square", "1:1"),
)


def detect_image_size(prompt: str) -> tuple[int, int]:
    """Размер из маркера в промпте; по умолчанию 1024x1024. Ищем только явные
    форматы/слова пропорций, чтобы "широкий спектр" не стал 16:9. Форматы ищем
    с границами цифр: иначе "13:45" содержит "3:4" (найдено ревью 10.11.2026)."""
    low = (prompt or "").lower()
    for token, key in _SIZE_WORDS:
        if any(c.isdigit() for c in token) and any(c in token for c in (":", "х", "x")):
            if re.search(r"(?<!\d)" + re.escape(token) + r"(?!\d)", low):
                return _SIZES[key]
        elif token in low:
            return _SIZES[key]
    return _SIZES["1:1"]


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Ширина/высота по заголовку файла, без Pillow: правка должна сохранять
    пропорции референса (замер 10.11.2026: 16:9 фото выходило квадратом)."""
    try:
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
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
    """Выходной размер правки: ближайшая из поддерживаемых пропорций к кадру
    референса (4:3 и 3:4 тоже), неизвестные размеры дают квадрат."""
    if not dimensions:
        return (1024, 1024)
    w, h = dimensions
    if not w or not h:
        return (1024, 1024)
    ratio = w / h
    names = ("1:1", "4:3", "3:4", "16:9", "9:16")
    target = {"1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16}
    best = min(names, key=lambda name: abs(math.log(ratio / target[name])))
    return _SIZES[best]


def _image_content_type(data: bytes) -> str:
    """Тип референса по магическим байтам: имя файла у FormData условное."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _cloudflare_model_url(model: str) -> str:
    # safe="/@": Cloudflare ждёт модель в пути как `@cf/имя/модель`, голый quote()
    # превратил бы слэш в %2F и получил бы 404 на заведомо существующую модель.
    return (
        f"{CLOUDFLARE_AI_BASE_URL}/{urllib.parse.quote(CLOUDFLARE_ACCOUNT_ID, safe='')}"
        f"/ai/run/{urllib.parse.quote(model, safe='/@')}"
    )


async def _cloudflare_request(
    session: aiohttp.ClientSession, url: str, *, form: aiohttp.FormData,
    timeout_sec: float | None,
) -> tuple[int, float | None, Any]:
    """Один multipart-POST, дочитанный и закрытый: (статус, retry_after, JSON).
    Тело ошибки читается тем же капом, что и картинка; разбираем и на ошибках —
    дневный бюджет Cloudflare сообщает текстом в errors при HTTP 429.
    Замер 10.11.2026: flux-2-klein принимает ТОЛЬКО multipart, JSON-тело
    отвергает 5006 "required properties at '/' are 'multipart'"."""
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


def _cloudflare_image_result(status: int, retry_after: float | None, data: Any) -> bytes:
    """Общая расшифровка ответа flux-2-klein: успех, бюджет, темп, модерация."""
    detail = _error_strings(data)
    if status == 200 and not isinstance(data, dict):
        raise ImageProviderError("Cloudflare вернул JSON не-объектом")
    if status == 402:
        raise ImageBudgetExhaustedError(f"Cloudflare: бесплатный бюджет выбран (HTTP 402 {detail})")
    if status == 429:
        # Теми же 429 Cloudflare отвечает и на выбитый дневной бюджет Neuron.
        if any(word in detail.lower() for word in ("allocation", "free tier", "daily")):
            raise ImageBudgetExhaustedError(f"Cloudflare: бесплатный бюджет выбран (HTTP 429 {detail})")
        raise ImageRateLimitedError(f"Cloudflare HTTP 429 {detail}".strip(), retry_after_sec=retry_after)
    if status == 400 and any(word in detail.lower() for word in ("flagged", "moderation")):
        # Фильтр содержания (замер 10.11.2026: код 3030 "Your output has been
        # flagged"): это не сетевой сбой, повтор и резерв не помогут — человеку
        # честнее «ошибка генерации, переформулируй».
        raise ImageProviderError(f"Cloudflare отклонил содержимое по модерации: {detail[:200]}")
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
    width: int = 1024, height: int = 1024,
) -> aiohttp.FormData:
    """Тело запроса klein: prompt/width/height/seed и референс для правки.
    Размеры 256..1920, около мегапикселя — дешёвая единица бюджета
    (4*26.05 = 104 Neuron за картинку 1024x1024)."""
    form = aiohttp.FormData()
    form.add_field("prompt", prompt[:IMAGE_PROMPT_MAX_CHARS])
    form.add_field("width", str(width))
    form.add_field("height", str(height))
    form.add_field("seed", str(_random_seed()))
    if reference:
        form.add_field(
            "input_image_0", reference,
            filename="reference" + (".png" if reference.startswith(b"\x89PNG") else ".jpg"),
            content_type=_image_content_type(reference),
        )
    return form


async def _cloudflare_text_to_image(
    session: aiohttp.ClientSession, prompt: str, *,
    timeout_sec: float | None = None, width: int = 1024, height: int = 1024,
) -> bytes:
    """Генерация с нуля через Workers AI (multipart)."""
    if not cloudflare_configured():
        raise ImageProviderError("Cloudflare не настроен: нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN")
    status, retry_after, data = await _cloudflare_request(
        session, _cloudflare_model_url(CLOUDFLARE_IMAGE_MODEL),
        form=_cloudflare_form(prompt, width=width, height=height), timeout_sec=timeout_sec,
    )
    return _cloudflare_image_result(status, retry_after, data)


async def _cloudflare_edit_to_image(
    session: aiohttp.ClientSession, prompt: str, reference: bytes, *,
    timeout_sec: float | None = None, width: int = 1024, height: int = 1024,
) -> bytes:
    """Правка готовой картинки: референс уходит полем input_image_0 (замер
    10.11.2026: красное яблоко -> зелёное, вход 1024x1024 принимается как есть)."""
    if not cloudflare_configured():
        raise ImageProviderError("Cloudflare не настроен: нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN")
    if not reference:
        raise ImageProviderError("Cloudflare edit: пустой референс")
    status, retry_after, data = await _cloudflare_request(
        session, _cloudflare_model_url(CLOUDFLARE_EDIT_MODEL),
        form=_cloudflare_form(prompt, reference=reference, width=width, height=height),
        timeout_sec=timeout_sec,
    )
    return _cloudflare_image_result(status, retry_after, data)


async def _pollinations_text_to_image(session: aiohttp.ClientSession, prompt: str, *, timeout_sec: float | None = None) -> bytes:
    """Классический эндпоинт без ключа — только резерв. Замер 09.11.2026: 1 картинка в
    45 секунд на IP (HTTP 402 FREE_LIMIT с Retry-After), модель всегда sana, nologo
    игнорируется, поэтому водяной знак остаётся."""
    encoded = _encoded_prompt(prompt)
    # safe=true: без него фильтр NSFW выключен (так в их доках), а бот публичный.
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
            # FREE_LIMIT — темп на IP, то есть перегрузка; без него — конченный бюджет.
            if "FREE_LIMIT" in detail or retry_after is not None:
                raise ImageRateLimitedError(f"Pollinations HTTP 402 {detail}".strip(), retry_after_sec=retry_after)
            raise ImageBudgetExhaustedError(f"Pollinations HTTP 402 {detail}".strip())
        raise ImageProviderError(f"Pollinations.ai HTTP {status}")
