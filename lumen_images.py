"""
lumen_images.py — генерация изображений через Pollinations.ai.

Вынесено из bot.py при разбиении на модули (см. README, раздел "Автоматический выбор
модели" и историю аудита техдолга) — самый изолированный кандидат из пяти намеченных:
не пишет ни в chat_state, ни в GLOBAL_QUOTA, не зовёт Telegram API напрямую и не зависит
от глобальных `bot`/`client`.

Единственная внешняя зависимость каждого вызова — aiohttp-сессия, но модуль намеренно НЕ
хранит собственную сессию и не заводит новый module-level singleton для неё: в bot.py уже
есть один общий httр-session getter (`_get_http_session`), которым пользуются TikTok/
OpenRouter/эта же генерация картинок — плодить второй, изолированный источник управления
HTTP-соединениями было бы речь не о разделении ответственности, а о случайном дублировании.
Поэтому `_pollinations_generate`/`_hf_text_to_image` принимают уже готовую сессию параметром;
вызывающий код (см. `inline_draw` в bot.py) сам получает её через `_get_http_session()` и
передаёт сюда — то же самое соглашение, по которому TikTok-скачивание в bot.py принимает
сессию параметром в `_download_url_bin`/`_resolve_tiktok_short` и т.п.

Публичные имена и поведение не изменились относительно прежнего кода внутри bot.py — кроме
добавленного параметра `session`, который раньше был получен неявно через `_get_http_session()`
внутри самой `_pollinations_generate`.

УБРАНО (аудит техдолга, 19 августа 2026): команда `/imgmodel` и ручной выбор модели через
inline-клавиатуру — тот же класс изменения, что уже был сделан для текстового провайдера/
модели (см. "Автоматический выбор модели" в README): пользователь никогда явно не выбирает
модель, роутер выбирает сам на каждый запрос (`_pick_image_model` ниже). `_hf_model_catalog`/
`_imgmodel_keyboard` были нужны ТОЛЬКО для этой клавиатуры — убраны вместе с ней как мёртвый
код, а не оставлены "на будущее".
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

import aiohttp

DEFAULT_HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "flux").strip()

HF_IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "flux": {
        "name": "FLUX Pro",
        "desc": "Высококачественный FLUX. Фотореализм, точное следование промпту, богатая детализация.",
    },
    "flux-realism": {
        "name": "FLUX Realism",
        "desc": "FLUX с акцентом на гиперреализм — детализированные текстуры, естественное освещение, кинематографичность.",
    },
    "flux-anime": {
        "name": "FLUX Anime",
        "desc": "FLUX для аниме и иллюстраций — характерные пропорции, яркие цвета, стилизация под японскую графику.",
    },
    "turbo": {
        "name": "Turbo",
        "desc": "Быстрая дистиллированная модель. Результат за несколько секунд — для черновиков и быстрых итераций.",
    },
    "dreamshaper": {
        "name": "DreamShaper",
        "desc": "Художественная модель для фэнтези, концепт-арта и стилизованных иллюстраций.",
    },
}

# ── Автоматический выбор модели генерации по содержимому промпта ──
# Тот же принцип, что и у текстового роутера (_looks_like_heavy_query/
# _looks_like_freshness_query в lumen_router_config.py): грубая эвристика по
# ключевым словам без обращения к LLM — отдельный классифицирующий вызов модели
# стоил бы дороже, чем просто попробовать разумный дефолт и дать сработать
# существующей fallback-цепочке (см. inline_draw в bot.py) при неудаче/плохом
# результате. Порядок проверки — от самых специфичных категорий к общей: аниме/
# фэнтези/реализм/черновик — явные сигналы жанра, при их отсутствии остаётся
# DEFAULT_HF_IMAGE_MODEL (универсальный FLUX Pro).
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
    """Заменяет ручной выбор через убранную команду /imgmodel — см. докстринг модуля
    и README, раздел "Автоматический выбор модели". Проверки НЕ взаимоисключающие по
    смыслу (промпт может упоминать и аниме, и дракона), поэтому порядок фиксирован:
    аниме/манга — самый визуально узнаваемый и однозначный стиль, проверяется первым;
    фэнтези/концепт-арт — вторая по специфичности категория; фотореализм — просьба
    "как фото" достаточно однозначна сама по себе; черновик/скетч — про скорость, а
    не стиль, поэтому последний перед дефолтом (стилевые сигналы важнее просьбы
    "побыстрее", если оба есть в одном промпте)."""
    if not prompt:
        return DEFAULT_HF_IMAGE_MODEL
    if _ANIME_RE.search(prompt):
        return "flux-anime"
    if _FANTASY_RE.search(prompt):
        return "dreamshaper"
    if _REALISM_RE.search(prompt):
        return "flux-realism"
    if _QUICK_RE.search(prompt):
        return "turbo"
    return DEFAULT_HF_IMAGE_MODEL


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
            if body and (ctype.startswith("image/") or body[:4] in (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8")):
                return body
            raise RuntimeError(f"Pollinations вернул не-изображение: {ctype}")
        raise RuntimeError(f"Pollinations.ai HTTP {resp.status}")


async def _hf_text_to_image(session: aiohttp.ClientSession, model_id: str, prompt: str) -> bytes:
    # Pollinations.ai — единственный провайдер генерации изображений (HF Inference
    # API-ветка убрана: старые HF-модели регулярно устаревали на стороне провайдера,
    # см. историю: FLUX.1-dev вернул 410 Gone). Раз провайдер всего один, отдельная
    # "pollinations:" приставка на каждом ключе HF_IMAGE_MODELS была лишней —
    # model_id и есть имя модели Pollinations как есть.
    if model_id not in HF_IMAGE_MODELS:
        raise ValueError(f"Неизвестная модель генерации изображений: {model_id}")
    return await _pollinations_generate(session, model_id, prompt)


def _image_model_label(model_id: str) -> str:
    meta = HF_IMAGE_MODELS.get(model_id, {})
    return meta.get("name", model_id)
