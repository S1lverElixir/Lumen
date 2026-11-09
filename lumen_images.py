"""
lumen_images.py — text-to-image: Cloudflare Workers AI основной провайдер, Pollinations.ai резерв.
Модуль изолирован: не пишет в chat_state/квоту, не зовёт Telegram, сессию принимает параметром.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

# Замер 09.11.2026 живьём: image.pollinations.ai игнорирует model (всегда sana, каталог
# /models = ["sana"]) и nologo (водяной знак остаётся на ответе), поэтому стилевой
# каталог из пяти моделей и подбор по промпту убраны — они ничего не выбирали.
POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt"
POLLINATIONS_IMAGE_MODEL = (os.getenv("POLLINATIONS_IMAGE_MODEL", "").strip() or "sana")
CLOUDFLARE_AI_BASE_URL = (os.getenv("CLOUDFLARE_AI_BASE_URL", "").strip() or "https://api.cloudflare.com/client/v4/accounts").rstrip("/")
CLOUDFLARE_IMAGE_MODEL = (os.getenv("CLOUDFLARE_IMAGE_MODEL", "").strip() or "@cf/black-forest-labs/flux-1-schnell")
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


async def _cloudflare_text_to_image(session: aiohttp.ClientSession, prompt: str, *, timeout_sec: float | None = None) -> bytes:
    """POST на Workers AI. Только prompt и seed: 1024x1024 и 4 шага — дефолты модели,
    большего документация не обещает. При дефолтах это 4*4.8 + 4*9.6 = 57.6 Neuron,
    то есть ~170 картинок из 10 000 бесплатных Neuron в сутки."""
    if not cloudflare_configured():
        raise ImageProviderError("Cloudflare не настроен: нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN")
    # safe="/@": Cloudflare ждёт модель в пути как `@cf/имя/модель`, голый quote()
    # превратил бы слэш в %2F и получил бы 404 на заведомо существующую модель.
    url = (
        f"{CLOUDFLARE_AI_BASE_URL}/{urllib.parse.quote(CLOUDFLARE_ACCOUNT_ID, safe='')}"
        f"/ai/run/{urllib.parse.quote(CLOUDFLARE_IMAGE_MODEL, safe='/@')}"
    )
    payload = {"prompt": prompt[:IMAGE_PROMPT_MAX_CHARS], "seed": _random_seed()}
    async with session.post(
        url, json=payload, timeout=_timeout(timeout_sec),
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
    ) as resp:
        status = int(resp.status)
        body = await _read_capped_image(resp)
        if body is None:
            raise ImageProviderError("Cloudflare вернул тело больше капа")
        # Разбираем и на ошибках тоже: дневный бюджет Cloudflare сообщает текстом в
        # errors при HTTP 429, а не отдельным статусом.
        data: Any = None
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(body)
        if status == 200 and not isinstance(data, dict):
            raise ImageProviderError("Cloudflare вернул JSON не-объектом")
        detail = _error_strings(data)
        if status == 402:
            raise ImageBudgetExhaustedError(f"Cloudflare: бесплатный бюджет выбран (HTTP 402 {detail})")
        if status == 429:
            # Теми же 429 Cloudflare отвечает и на выбитый дневной бюджет Neuron.
            if any(word in detail.lower() for word in ("allocation", "free tier", "daily")):
                raise ImageBudgetExhaustedError(f"Cloudflare: бесплатный бюджет выбран (HTTP 429 {detail})")
            raise ImageRateLimitedError(
                f"Cloudflare HTTP 429 {detail}".strip(), retry_after_sec=_retry_after_sec(resp),
            )
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
