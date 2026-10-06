"""
lumen_images.py — генерация изображений через Pollinations.ai. Модуль изолирован:
не пишет в chat_state/квоту, не зовёт Telegram, сессию принимает параметром.
Модель выбирает роутер (`_pick_image_model`).
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

import aiohttp

DEFAULT_POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip()

POLLINATIONS_IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "flux": {"name": "FLUX Pro"},
    "flux-realism": {"name": "FLUX Realism"},
    "flux-anime": {"name": "FLUX Anime"},
    "turbo": {"name": "Turbo"},
    "dreamshaper": {"name": "DreamShaper"},
}

# ── Выбор модели по промпту: грубая эвристика без LLM (классифицирующий вызов стоил бы дороже дефолта + fallback). Порядок — от специфичного к общему: аниме → фэнтези → реализм → черновик → дефолт.
_ANIME_RE = re.compile(r"аниме|манг[аи]|манхв\w*|вебтун\w*|чиби|ваифу|anime|manga|waifu|chibi", re.IGNORECASE)
_FANTASY_RE = re.compile(
    r"фэнтези|фентези|фентезийн\w*|концепт-?арт\w*|дракон\w*|эльф\w*|волшебн\w*|магическ\w*|"
    r"орк\w*|фея|фей\b|замок\w*|рыцар\w*|fantasy|concept\s?art|dragon|wizard|elf|elves",
    re.IGNORECASE,
)
_REALISM_RE = re.compile(
    r"фотореалистичн\w*|гиперреалистичн\w*|реалистичн\w*|как\s+(на\s+)?фото|фотограф\w*|"
    r"portrait|realistic|photorealistic|hyperrealistic",
    re.IGNORECASE,
)
_QUICK_RE = re.compile(r"побыстрее|быстро|набросок|черновик|скетч|эскиз|draft|sketch|quick", re.IGNORECASE)


def _pick_image_model(prompt: str) -> str:
    """Замена ручного /imgmodel (убран, см. докстринг модуля). Проверки не взаимоисключающие — порядок фиксирован: стилевые сигналы важнее просьбы "побыстрее", поэтому черновик последний перед дефолтом."""
    if not prompt:
        return DEFAULT_POLLINATIONS_IMAGE_MODEL
    if _ANIME_RE.search(prompt):
        return "flux-anime"
    if _FANTASY_RE.search(prompt):
        return "dreamshaper"
    if _REALISM_RE.search(prompt):
        return "flux-realism"
    if _QUICK_RE.search(prompt):
        return "turbo"
    return DEFAULT_POLLINATIONS_IMAGE_MODEL


# Читаем потоково с капом: бесконтрольный resp.read() складывал в память тело
# любого размера от чужого сервиса.
POLLINATIONS_IMAGE_MAX_BYTES = 30 * 1024 * 1024


async def _read_capped_image(resp: aiohttp.ClientResponse, url: str) -> bytes | None:
    """Тело картинки с отказом при превышении капа (None): Content-Length врёт —
    поток перепроверяем по факту."""
    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > POLLINATIONS_IMAGE_MAX_BYTES:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > POLLINATIONS_IMAGE_MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _pollinations_generate(session: aiohttp.ClientSession, model_name: str, prompt: str, *, timeout_sec: float | None = None) -> bytes:
    """Бесплатная генерация через Pollinations.ai — не требует авторизации.
    `session` передаётся вызывающим кодом (см. докстринг модуля) — раньше получалась
    неявно через `_get_http_session()` внутри этой же функции, когда она жила в bot.py."""
    encoded = urllib.parse.quote(prompt[:600], safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1024&height=1024&model={model_name}&nologo=true&enhance=false"
    )
    total_timeout = timeout_sec if timeout_sec is not None else 90.0
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=total_timeout, sock_connect=12.0),
    ) as resp:
        if resp.status == 200:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = await _read_capped_image(resp, url)
            if body is None:
                raise RuntimeError("Pollinations вернул изображение больше капа")
            if body and (ctype.startswith("image/") or body.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8"))):
                return body
            raise RuntimeError(f"Pollinations вернул не-изображение: {ctype}")
        raise RuntimeError(f"Pollinations.ai HTTP {resp.status}")


async def _pollinations_text_to_image(session: aiohttp.ClientSession, model_id: str, prompt: str, *, timeout_sec: float | None = None) -> bytes:
    # HF-ветка убрана (FLUX.1-dev вернул 410 Gone) — провайдер один, приставки "pollinations:" на ключах не нужны.
    if model_id not in POLLINATIONS_IMAGE_MODELS:
        raise ValueError(f"Неизвестная модель генерации изображений: {model_id}")
    return await _pollinations_generate(session, model_id, prompt, timeout_sec=timeout_sec)
