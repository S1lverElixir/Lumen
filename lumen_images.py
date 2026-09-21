"""
lumen_images.py — генерация изображений через Pollinations.ai.

Вынесено из bot.py (аудит техдолга) — самый изолированный модуль: не пишет в chat_state/квоту, не зовёт Telegram. Сессию принимает параметром (общий getter `_get_http_session` в bot.py — второй источник соединений не заводим). `/imgmodel` и клавиатура выбора убраны 19.08.2026 — модель выбирает роутер (`_pick_image_model`).
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


async def _pollinations_generate(session: aiohttp.ClientSession, model_name: str, prompt: str) -> bytes:
    """Бесплатная генерация через Pollinations.ai — не требует авторизации.
    `session` передаётся вызывающим кодом (см. докстринг модуля) — раньше получалась
    неявно через `_get_http_session()` внутри этой же функции, когда она жила в bot.py."""
    encoded = urllib.parse.quote(prompt[:600], safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1024&height=1024&model={model_name}&nologo=true&enhance=false"
    )
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
        if resp.status == 200:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = await resp.read()
            if body and (ctype.startswith("image/") or body.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8"))):
                return body
            raise RuntimeError(f"Pollinations вернул не-изображение: {ctype}")
        raise RuntimeError(f"Pollinations.ai HTTP {resp.status}")


async def _pollinations_text_to_image(session: aiohttp.ClientSession, model_id: str, prompt: str) -> bytes:
    # HF-ветка убрана (FLUX.1-dev вернул 410 Gone) — провайдер один, приставки "pollinations:" на ключах не нужны.
    if model_id not in POLLINATIONS_IMAGE_MODELS:
        raise ValueError(f"Неизвестная модель генерации изображений: {model_id}")
    return await _pollinations_generate(session, model_id, prompt)
