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
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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


def _hf_model_catalog() -> list[dict[str, Any]]:
    """Список моделей генерации изображений для клавиатуры /imgmodel — прямо из
    HF_IMAGE_MODELS (единственный источник правды, статический список).

    НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: раньше здесь был отдельный `HF_IMAGE_MODEL_CACHE`
    dict и `async def _hf_fetch_model_catalog()` — вестигиальные остатки более
    раннего дизайна, когда каталог реально динамически подтягивался из HF API.
    Тот динамический фетч убран (см. историю — он добавлял неизвестные модели в
    меню), и функция давно не делает ни одного `await` внутри, а просто
    пересобирает список из того же самого статического HF_IMAGE_MODELS — то есть
    кэшировать было уже нечего: HF_IMAGE_MODELS и так уже лежит в памяти целиком,
    а пересборка списка из 5 элементов не стоит отдельного кэш-слоя с ручной
    инвалидацией на каждом сайте вызова. Убрано вместе с самим кэшем."""
    return [{"id": mid, **meta} for mid, meta in HF_IMAGE_MODELS.items()]


def _imgmodel_keyboard(current: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора модели генерации изображений, два столбца.

    ПОНИЖЕНО (ponytail-audit): раньше здесь была постраничная навигация
    (HF_IMAGE_MODEL_PAGE_SIZE=8, кнопки "< Назад"/"Дальше >") — при каталоге
    из 5 моделей она всегда давала ровно одну страницу и ни разу не могла
    сработать: nav-кнопки никогда не появлялись. Убрано вместе с параметром
    `page`; если каталог когда-нибудь вырастет за пределы одного экрана —
    пагинацию стоит вернуть тогда, а не держать её мёртвым кодом сейчас."""
    all_models = _hf_model_catalog()
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(all_models), 2):
        row = [
            InlineKeyboardButton(
                text=f"{'• ' if item['id'] == current else ''}{item['name']}",
                callback_data=f"imgmodel:set:{item['id']}",
            )
            for item in all_models[i:i + 2]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
