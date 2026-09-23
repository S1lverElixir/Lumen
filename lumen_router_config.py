"""
lumen_router_config.py — конфигурация моделей и логика автоматического выбора
маршрута (Gemini/OpenRouter) для одного сообщения.

Вынесено из bot.py при аудите технического долга. Всё содержимое этого файла —
конфигурационные данные (какие модели существуют, какие из них сейчас "нездоровы")
и ЧИСТЫЕ функции принятия решения о маршруте (_build_route/_or_route/_gemini_route,
эвристики "это тяжёлый запрос?"/"нужна свежая информация?") — никакого обращения
к Telegram/Gemini/OpenRouter API отсюда не происходит, поэтому этот код не зависит
от рантайм-состояния бота (в отличие от ask_gemini/ask_openrouter_*/_run_route,
которые реально выполняют маршрут и остаются в bot.py). bot.py импортирует все
нужные имена напрямую — публичные имена и поведение не изменились.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

# Единый логгер "bot" (а не __name__ == "lumen_router_config") — намеренно,
# чтобы предупреждения из этого модуля попадали под те же тесты/фильтры логов
# (caplog.at_level(..., logger="bot")), что и остальной бот, независимо от того,
# в каком физическом файле живёт код.
log = logging.getLogger("bot")


# список моделей

# Поля name/badge/desc убраны (июль 2026, /model удалена) — читаются только grounding/url_context/no_search/no_system/stream/quota_unconfirmed.
#
# ── Как дашборд AI Studio считает бесплатную квоту grounding-инструментов ──
# Бакет "Gemini 3" — 0/0 на search и map (24.07 + перепроверка 17.08.2026); квота на поиск только у 2.5-flash/lite, map — только у 3.5/3.1-lite.
GEMINI_MODELS: dict[str, dict[str, Any]] = {
    # Gemini 3.8 Flash — новый флагман линейки Flash (аудит моделей, 17 сентября
    # 2026, по дашборду AI Studio владельца за 28 дней: бакет 5 RPM/250K TPM/
    # 20 RPD, 0/0 на search и map grounding — ровно тот же профиль, что у 3.7/
    # 3.6/3.5 Flash, поэтому без quota_unconfirmed). search_grounding/
    # map_grounding False — бакет "Gemini 3" по-прежнему 0/0 на оба инструмента.
    "gemini-3.8-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # Gemini 3.7 Flash — прошлый флагман линейки Flash (GA 13 августа 2026),
    # сохранён в цепочке как резерв после 3.8 Flash.
    "gemini-3.7-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # Gemini 3.6 Flash — прошлый флагман линейки Flash, сохранён в цепочке как
    # резерв после 3.7 Flash.
    "gemini-3.6-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # Gemini 3.5 Flash — ещё более ранний флагман линейки Flash, сохранён в
    # цепочке как резерв после 3.6 Flash. url_context оставлен включённым — в
    # отличие от grounding-инструментов, у него нет отдельной дневной квоты в
    # дашборде, он просто добавляет токены по обычной цене модели.
    "gemini-3.5-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # 17.08 убрана, 22.08.2026 восстановлена: офиц. страница модели от 18.08.2026 подтверждает gemini-3-flash-preview — перевешивает вывод о ретирке по чужому продукту. Флаги grounding всё равно False — дашборд даёт 0/0.
    "gemini-3-flash-preview": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },

    # Gemini 3.5 Flash-Lite — новая версия самой быстрой и экономичной модели,
    # вышла 21 июля 2026 вместе с 3.6 Flash; превосходит 3.1 Flash-Lite в агентных
    # задачах и длинном контексте, до 350 токенов/сек.
    "gemini-3.5-flash-lite": {
        "stream": True,
        "search_grounding": False, "map_grounding": True,
    },
    # Gemini 3.1 Flash-Lite — прошлая версия самой быстрой и экономичной модели
    # линейки, сохранена в цепочке как резерв после 3.5 Flash-Lite.
    "gemini-3.1-flash-lite": {
        "stream": True,
        "search_grounding": False, "map_grounding": True,
    },
    # Gemini 2.5 Flash — универсальная мультимодальная модель поколения 2.5,
    # хороший баланс скорости и качества для большинства повседневных задач.
    # Единственное поколение с реальной квотой на search grounding (21/1500).
    "gemini-2.5-flash": {
        "stream": True,
        "search_grounding": True, "map_grounding": True,
    },
    # Gemini 2.5 Flash-Lite — экономичная модель поколения 2.5 для задач, где
    # важна скорость ответа больше, чем глубина рассуждений.
    "gemini-2.5-flash-lite": {
        "stream": True,
        "search_grounding": True, "map_grounding": True,
    },
    # Gemma 4 31B — флагманская открытая модель Google на 31 млрд параметров.
    "gemma-4-31b-it": {
        "no_system": True, "no_search": True, "stream": True,
    },
    # Gemma 4 26B — компактная открытая модель Google на 26 млрд параметров с
    # расширенным мышлением (thinking).
    "gemma-4-26b-a4b-it": {
        "no_system": True,
        # 24.07.2026: пропущенный no_search у 26b включал бы google_search/url_context для Gemma, которая их не поддерживает — добавлен для консистентности с 31b.
        "no_search": True, "stream": True,
    },
}
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

# ── TTS-модели (аудит техдолга, август 2026) ──
# Вынесено из bot.py — второй источник правды об именах TTS-моделей.
GEMINI_TTS_MODELS: list[str] = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

# Fish free — только до 31.08.2026 (fish.audio/blog); зеркала больше нет в живом каталоге.
FISH_AUDIO_TTS_MODEL = "fish-audio/s2.1-pro-free:free"
# ОТКЛЮЧЕНО (аудит моделей, 17 сентября 2026): зеркала fish-audio/s2.1-pro-free:free
# больше нет в живом каталоге OpenRouter (публичный /models API — ни одного
# fish-слага среди 24 бесплатных), а блог fish.audio/blog/s2-1-pro-free-api так и
# не объявил продления после 31.08.2026. Пока флаг False, inline_tts идёт сразу на
# Gemini TTS без заведомо мёртвой первой попытки (та стоила бы лишний сетевой
# запрос — и до ROUTE_MODEL_TIMEOUT_SEC ожидания в худшем случае — на каждую
# озвучку). Сама функция _fish_audio_tts_bytes и этот флаг оставлены (не удалены):
# если Fish снова откроют free-доступ — достаточно вернуть True одной строкой.
FISH_AUDIO_ENABLED = False

def _check_unconfirmed_model_quotas() -> None:
    """Напоминаем про модели с quota_unconfirmed: квота/grounding ещё не подтверждены по дашборду."""
    for mid, conf in GEMINI_MODELS.items():
        if conf.get("quota_unconfirmed"):
            log.warning(
                "[setup] Real RPD limits and search/map grounding availability for model %s are NOT yet confirmed against the AI Studio dashboard (model was recently released) — the current search_grounding/map_grounding values in GEMINI_MODELS are a guess by analogy with a model of the same class. Check the dashboard and remove 'quota_unconfirmed' for this model in bot.py, adjusting the config if needed.",
                mid,
            )

# Раньше словарь {"id","name","description"} для /model — описания нигде не читались, оставлен плоский список ID для детектора утечек.
# Список перепроверен по openrouter.ai (июль 2026): снятые с free и переименованные провайдером ID вычищены.
# nemotron-3.5-content-safety — классификатор, а не диалоговая модель, не включаем намеренно.
# Не порядок роутера (тот — в _OR_*_ORDER), а реестр известных ID для детектора утечек: снятые с тарифа оставляем — их тоже нельзя в ответ. TEXT_MODEL_ORDER ниже — алиас для совместимости.
_KNOWN_MODEL_IDS_FOR_LEAK_DETECTION: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "openai/gpt-oss-120b:free",
    "z-ai/glm-4.5-air:free",
    "tencent/hy3:free",
    "openrouter/owl-alpha",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "qwen/qwen3-coder:free",
    "poolside/laguna-m.1:free",
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
    "cohere/north-mini-code:free",
    "inclusionai/ling-3.0-flash:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "openrouter/free",
    # Аудит 17.08.2026 (Top Weekly free): обе новые, калибровку с Sonnet не проходили.
    "nvidia/nemotron-3.5-lightning:free",
    "dots-studio/dots-3-note-preview:free",
    # Аудит 22.08.2026: новая reasoning-модель glm-5.2 (в heavy-цепочке).
    "z-ai/glm-5.2:free",
    # Аудит 17.09.2026 (живой каталог): новички + gemini-3.8-flash. Старые ID не удаляем никогда.
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "liquid/lfm-2.5-2.6b:free",
    "qwen/qwen3.8-27b:free",
    "gemini-3.8-flash",
    "openrouter/free",
    # Groq-ID без :free-суффикса — те же семейства, что выше через OpenRouter; дословно в ответе им тоже не место.
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]
TEXT_MODEL_ORDER = _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION  # алиас для обратной совместимости
# Аудит 02.08.2026 (логи + каталог): laguna-s-2.1 и ling-3.0-flash, калибровку не проходили (только факт free-квоты).

# ── Единый реестр "нездоровых" моделей (один dict вместо трёх рассинхронных механизмов: правки в 2-3 местах на инцидент). Excluded-множество и промо-варнинги вычисляются из него.
@dataclass(frozen=True)
class _ModelHealthNote:
    reason: str
    # Задано только для ВРЕМЕННОГО промо-доступа (акция провайдера) — после этой
    # даты в логи попадает предупреждение перепроверить актуальную цену на
    # openrouter.ai. Модели, снятые НАВСЕГДА (не промо, а прямая инструкция
    # провайдера использовать другой/платный слаг), оставляют это поле пустым —
    # предупреждать об "истечении" там нечего, они просто не должны выбираться.
    promo_expiry: date | None = None

_OR_MODEL_HEALTH: dict[str, _ModelHealthNote] = {
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free": _ModelHealthNote(
        reason="Uncensored-модель — может хуже соблюдать личность/правила Lumen. Раньше выбиралась "
               "вручную только владельцем через /provider (команда удалена) — автоматический роутер "
               "её не выбирает вообще."
    ),
    "qwen/qwen3-coder:free": _ModelHealthNote(
        reason="Подтверждено при аудите моделей (июль 2026): :free-эндпоинт снят провайдером.",
        promo_expiry=date(2026, 6, 30),
    ),
    "tencent/hy3:free": _ModelHealthNote(
        reason="Собственная страница OpenRouter показывала 'Going away July 19, 2026' — :free-эндпоинт "
               "уже снят провайдером.",
        promo_expiry=date(2026, 7, 21),
    ),
    "qwen/qwen3-next-80b-a3b-instruct:free": _ModelHealthNote(
        reason="Логи прода 25.07.2026, 20+ попыток: HTTP 404 'use this slug instead' (платный слаг) — не возвращать без бесплатного доступа."
    ),
    # ── Аудит 02.08.2026 (логи прода, ~5ч трафика) ──
    "z-ai/glm-4.5-air:free": _ModelHealthNote(
        reason="Логи прода 02.08.2026, 8 попыток: HTTP 404 'use this slug instead' (тот же паттерн снятия с free, что выше)."
    ),
    "meta-llama/llama-3.2-3b-instruct:free": _ModelHealthNote(
        reason="Логи прода 02.08.2026, 7 попыток: HTTP 404 'use this slug instead' — Meta, похоже, убрала весь free-тир Llama."
    ),
    "liquid/lfm-2.5-1.2b-instruct:free": _ModelHealthNote(
        reason="Логи прода 02.08.2026: 'No endpoints found' — для слага нет ни одного провайдера, плюс отсутствует в живом каталоге."
    ),
    "liquid/lfm-2.5-1.2b-thinking:free": _ModelHealthNote(
        reason="Каталог OpenRouter 17.09.2026: слага нет среди бесплатных, LiquidAI выпустили замену liquid/lfm-2.5-2.6b:free."
    ),
    "nousresearch/hermes-3-llama-3.1-405b:free": _ModelHealthNote(
        reason="Снимок API OpenRouter 27.07.2026 называет её среди снятых с free (5 из 7 того снимка уже независимо подтверждены) — уверенность ниже, чем по прямым логам."
    ),
    # ── Калибровка против Claude Sonnet 5 (18.08.2026, логи + скриншоты) ──
    "nvidia/nemotron-nano-9b-v2:free": _ModelHealthNote(
        reason="Логи+скриншоты 18.08.2026 (37/45 лёгких запросов): каждый развёрнутый ответ содержал вставки чужих языков внутрь слов ('modelo', 'زراعة', 'キュ', 'lagoo Isaacs' и т.п.) плюс стриминг 12-92с — текст нечитаем, хотя технически отвечает."
    ),
    # ── Логи прода 22.08.2026 ──
    "inclusionai/ling-3.0-flash:free": _ModelHealthNote(
        reason="Логи прода 22.08.2026, 2 попытки: HTTP 404 'use this slug instead' — стояла головой лёгкой цепочки, каждое сообщение теряло попытку."
    ),
    # ── Аудит 17.09.2026 (каталог OpenRouter через /models API: 444 модели, 24 бесплатных) ──
    "nvidia/nemotron-3-nano-30b-a3b:free": _ModelHealthNote(
        reason="Анонсированная дата снятия 24.08.2026 наступила, слага нет в живом каталоге free — стояла в лёгкой цепочке."
    ),
    "openai/gpt-oss-20b:free": _ModelHealthNote(
        reason="Слага нет в живом каталоге free 17.09.2026 (в логах напрямую не поймана — уверенность ниже; убрать запись при первом успешном вызове)."
    ),
    "openai/gpt-oss-120b:free": _ModelHealthNote(
        reason="То же снятие, что у gpt-oss-20b выше (каталог 17.09.2026, в логах не поймана)."
    ),
    "nvidia/nemotron-nano-12b-v2-vl:free": _ModelHealthNote(
        reason="Слага нет в живом каталоге free 17.09.2026 — стояла головой vision-цепочки; заменена на gemma-4-31b-it:free."
    ),
    # ── Логи прода вечером 17.09.2026 (после утреннего аудита — новички уже пошли в бой) ──
    "thinkingmachines/inkling-small:free": _ModelHealthNote(
        reason="Логи прода 17.09.2026: 'only available on agentic harnesses' — не обслуживает обычные chat-запросы; стояла второй в лёгкой цепочке."
    ),
}

# Вычисляется из _OR_MODEL_HEALTH — роутер не должен выбирать эти модели.
_ROUTER_EXCLUDED_OR_MODELS: frozenset[str] = frozenset(_OR_MODEL_HEALTH.keys())

def _check_temporary_free_models_expiry() -> None:
    """Предупреждает про модели с истёкшим промо (для снятых навсегда предупреждать не о чем)."""
    today = date.today()
    for model_id, note in _OR_MODEL_HEALTH.items():
        if note.promo_expiry is not None and today > note.promo_expiry:
            log.warning(
                '[or] Temporary free access to model %s expired on %s (today is %s) — %s The router no longer selects it (_ROUTER_EXCLUDED_OR_MODELS), but check the current price on openrouter.ai if you ever need to bring it back.',
                model_id, note.promo_expiry.isoformat(), today.isoformat(), note.reason,
            )

# ── Анонсированные даты снятия ("Going away <дата>" на карточке OpenRouter) ──
# Будущий анонс — не основание исключать живую модель из роутера (принцип: только подтверждённые логи); реестр лишь предупреждает, когда дата наступила, модель остаётся в цепочках до первого 404.
_SCHEDULED_OR_REMOVALS: dict[str, date] = {
    "dots-studio/dots-3-note-preview:free": date(2026, 9, 30),
}

def _check_scheduled_removals_due() -> None:
    """Предупреждает, когда анонсированная дата снятия наступила — сигнал проверить логи и завести запись в _OR_MODEL_HEALTH при реальном 404. ">=" вместо ">" — анонс действует уже в день снятия."""
    today = date.today()
    for model_id, removal_date in _SCHEDULED_OR_REMOVALS.items():
        if today >= removal_date and model_id not in _ROUTER_EXCLUDED_OR_MODELS:
            log.warning(
                '[or] Model %s was scheduled by OpenRouter to go away on %s (today is %s) — check recent logs for 404/"no endpoints" errors on this model, and add a dated _OR_MODEL_HEALTH entry if confirmed dead. Not excluded automatically — still selectable until confirmed.',
                model_id, removal_date.isoformat(), today.isoformat(),
            )

def _or_route(models: list[str]) -> list[tuple[str, str]]:
    """Превращает список ID моделей OpenRouter в список (provider, model_id) для
    маршрута, попутно исключая модели из _ROUTER_EXCLUDED_OR_MODELS."""
    return [("openrouter", m) for m in models if m not in _ROUTER_EXCLUDED_OR_MODELS]

def _gemini_route(models: list[str]) -> list[tuple[str, str]]:
    return [("gemini", m) for m in models]

# ── Groq (прямой провайдер, не через OpenRouter) ──
# Калибровка русского живьём 21.09.2026 (4 пробы на модель через API с VPN): Qwen отвечает чисто и по делу, gpt-oss-120b тоже верен, но представляется ChatGPT от OpenAI (ловит фильтр утечек) и тратит 30-70 reasoning-токенов на ответ — они едят минутный бюджет. Поэтому Qwen голова, gpt-oss второй. Лимиты free-плана по офиц. доке: 30 RPM / 1000 RPD / 8K TPM / 200K TPD на модель.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_GROQ_LIGHT_ORDER: list[str] = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]

def _groq_route(models: list[str]) -> list[tuple[str, str]]:
    return [("groq", m) for m in models]


# ── "Лёгкие" запросы — САМЫЙ ЧАСТЫЙ маршрут, целиком OpenRouter (квоту Gemini не трогаем).
#
# Порядок по аудитам 22.08/17.09.2026: мёртвые убраны (см. _OR_MODEL_HEALTH), некалиброванные новички — после проверенной головы lightning, перед generic-резервом. nex-mini подтверждён продом 17.09.2026 и повышен; inkling-small исключён (только agent harnesses).
# НАБЛЮДЕНИЕ 17.09.2026: один ответ nex-mini с вкраплениями чужих языков — при повторе понижать (прецедент super-120b ниже).
_OR_LIGHT_ORDER: list[str] = [
    "nvidia/nemotron-3.5-lightning:free",
    "nex-agi/nex-n2.5-mini:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "liquid/lfm-2.5-2.6b:free",
    # Аудит 21.09.2026 (живой каталог): qwen3.8-27b:free — то же семейство, что калиброванный
    # Groq-Qwen, но другой эндпоинт: некалиброван, поэтому после проверенных, перед резервом.
    "qwen/qwen3.8-27b:free",
    "openrouter/free",
]

# ── "Тяжёлые" запросы без интернета/медиа — тоже сначала OpenRouter (сильные бесплатные 120B/550B, квоту Gemini бережём).
#
# super-120b понижен под ultra-550b (второй инцидент порчи текста 17.09.2026; при третьем — исключать). Снятые с free убраны целиком (см. _OR_MODEL_HEALTH). Coding-новички 22.08 (glm-5.2, laguna, north-mini-code) — после проверенных калибровкой, перед резервом: без диалоговой специализации. dots-3-note-preview — последней перед резервом (снятие 30.09.2026, пока жива).
_OR_HEAVY_ORDER: list[str] = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-5.2:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
    "poolside/laguna-xs-2.1:free",
    "thinkingmachines/inkling:free",
    "nex-agi/nex-n2.5-pro:free",
    "dots-studio/dots-3-note-preview:free",
    "openrouter/free",
]

# ── Картинки без нужды в свежем — vision-модели OpenRouter (видео/аудио сюда не ходят, только base64-картинки).
# Головой gemma-4-31b (free-квота подтверждена дашбордом), ling-3.0-flash-vl в хвосте — некалиброванная (аудит 17.09.2026).
_OR_VISION_ORDER: list[str] = [
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free",
    "inclusionai/ling-3.0-flash-vl:free",
]

# ── Цепочки Gemini для случаев, где нужен именно Gemini (YouTube/сайт по ссылке, видео/аудио), но живой поиск не нужен. Голова — 3.8-flash (аудит 17.09.2026).
GEMINI_HEAVY_CHAIN: list[str] = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]
# Сначала модели с реальной квотой search grounding (2.5-flash/lite), линейка 3.x — резервом (ответ по знаниям/url_context).
GEMINI_SEARCH_CHAIN: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
]
# Дефолт для прямых вызовов ask_gemini без явной цепочки (например, из тестов).
GEMINI_DEFAULT_CHAIN: list[str] = GEMINI_HEAVY_CHAIN
# Сайты по ссылке и YouTube умеют только "полноценные" (не no_system/Gemma) модели.
GEMINI_LINK_CHAIN: list[str] = [m for m in GEMINI_HEAVY_CHAIN if not GEMINI_MODELS.get(m, {}).get("no_system")]
GEMINI_LINK_SEARCH_CHAIN: list[str] = [m for m in GEMINI_SEARCH_CHAIN if not GEMINI_MODELS.get(m, {}).get("no_system")]


# ── Эвристика "это сложный/тяжёлый запрос?" — без обращения к LLM. Ложные
# срабатывания недороги: худший случай — используется чуть более мощная
# модель, чем реально нужно, а не отказ в ответе.
_HEAVY_QUERY_RE = re.compile(
    r"напиши\s+(код|функци\w*|скрипт|программ\w*|класс\w*|запрос\s+sql|regex|регуляр\w*|тест\w*|парсер\w*|бот\w*|сайт\w*|приложени\w*)"
    r"|сгенерируй\s+код|исправь\s+(код|баг|ошибк\w*)|отрефактор\w*|рефактор\w*|оптимизируй"
    r"|разбер(и|ём|ись)\s+(этот\s+|подробно\s+)?(код|ошибк\w*|баг\w*|текст\w*|документ\w*|подробно)"
    r"|найди\s+(ошибк\w*|баг\w*)|объясни\s+(код|ошибк\w*)"
    r"|напиши\s+(эссе|статью|доклад|реферат|сочинение|резюме|cv)\b"
    r"|проанализируй\w*|разбер(и|ём)\s+подробно|объясни\s+подробно"
    # Голое "сравни( конкурентов)" — тоже сравнение (прод-кейс 17.09.2026),
    # вариант с "и/с" выше оставлен для явных пар.
    r"|сравни\b|докажи\b|доказательство"
    r"|реши\s+(задач\w*|уравнени\w*|систем\w*)"
    r"|составь\s+(план|таблиц\w*|список\s+из)"
    r"|многошагов\w*|пошагов\w*\s+(инструкц\w*|план\w*)"
    r"|архитектур\w*|алгоритм\w*"
    # EN-набор (найдено внешним аудитом: heavy-детект был почти весь русский).
    # Границы слов обязательны: голый prove ловил improve, solve — resolve (ревью ветки).
    r"|write\s+(a\s+|an\s+|the\s+)?(\w+\s+)?(code|function\w*|script\w*|program\w*|class\w*|sql|regex|test\w*|parser\w*|bot|website\w*|app\w*)\b"
    r"|generate\s+code|fix\s+(this\s+|that\s+)?(code|bug|error|issue)\b|refactor\w*|optimiz\w*|debug"
    r"|explain\s+(code|error)|code\s+review|algorithm|architecture"
    r"|write\s+(an?\s+)?(essay|article|report|paper|thesis|cv|resume)\b"
    r"|compar(e|ison)|\bprove\b|\bproof\b|\bsolve\b|equation",
    re.IGNORECASE,
)

def _looks_like_heavy_query(text: str) -> bool:
    """Грубая эвристика "это тяжёлый запрос (код/анализ/многошаговые рассуждения)?"
    Намеренно консервативная (без вызова LLM — см. комментарий в начале секции)."""
    if not text:
        return False
    if "```" in text or len(text) > 600:
        return True
    if text.count("?") >= 3:
        return True
    return bool(_HEAVY_QUERY_RE.search(text))


# ── Эвристика "нужна ли живая информация из интернета?" Ложные срабатывания
# тоже недороги: худший случай — маршрут отдаёт предпочтение search-способной
# модели там, где поиск был не нужен, но модель сама решает, вызывать ли его.
_FRESHNESS_QUERY_RE = re.compile(
    r"сейчас|сегодня|текущ\w*|последн\w*|актуальн\w*|свеж\w*|недавно|на\s+данный\s+момент"
    r"|новост\w*|курс\s+(валют|доллара|евро|рубл\w*)|погод\w*|прогноз\w*"
    r"|расписани\w*|афиш\w*"
    r"|цена\w*|стоимост\w*|сколько\s+стоит"
    # Советы по покупке (прод-кейс 17.09.2026: "какой проц лучше всего брать" —
    # цены, наличие и новинки меняются постоянно, ответ из памяти протухает).
    # Ложные срабатывания дешёвые (Gemini и так отвечает на всё подряд).
    r"|лучше\s+(всего\s+)?(брать|взять|купить|выбрать)"
    r"|(брать|взять|купить|выбрать)\s+лучше"
    r"|кто\s+(сейчас|является|президент|премьер|глава|ceo|мэр)"
    r"|результат\w*\s+(матч\w*|игр\w*|выбор\w*)"
    r"|в\s+эт(ом|ой)\s+(году|месяце|неделе)"
    r"|\b202[6-9]\b"
    # EN-набор (найдено внешним аудитом: детект был только русским при DEFAULT_LANG=en
    # и 25 языках; ложные срабатывания так же дёшевы). Границы слов обязательны:
    # голый now ловил know/snow (ревью ветки).
    r"|\bnow\b|\btoday\b|current\w*|latest|recent\w*|\bbreaking\b"
    r"|\bnews\b|weather|forecast|price\w*|\bcost\w*|how\s+much|exchange|\bscore\w*|schedule"
    r"|who\s+is\s+(now|currently|the\s+(president|ceo|prime\s+minister|mayor))"
    r"|best\s+(to\s+buy|buy)|should\s+i\s+buy|worth\s+buying",
    re.IGNORECASE,
)

def _looks_like_freshness_query(text: str) -> bool:
    return bool(text) and bool(_FRESHNESS_QUERY_RE.search(text))


def _build_route(
    *, needs_youtube: bool, needs_website: bool, media_mime: str | None,
    is_heavy: bool, needs_freshness: bool,
) -> list[tuple[str, str]]:
    """Строит приоритетный список кандидатов (provider, model_id) для текущего
    сообщения — НЕПУСТОЙ список, первый элемент пробуется первым (см. _run_route).
    Порядок кандидатов внутри одного провайдера — по возрастанию "дороговизны"
    для дефицитной квоты, а не по итоговому качеству ответа отдельно взятой модели."""
    is_video_or_audio_media = bool(media_mime) and not media_mime.startswith("image/")

    if needs_youtube or needs_website:
        # Только Gemini умеет читать сайты по ссылке и разбирать YouTube-видео —
        # у OpenRouter в этом маршруте вообще нет места, эскалировать некуда.
        chain = GEMINI_LINK_SEARCH_CHAIN if needs_freshness else GEMINI_LINK_CHAIN
        return _gemini_route(chain)

    if media_mime:
        if needs_freshness or is_video_or_audio_media:
            # Видео/аудио вложение ИЛИ нужен живой поиск вместе с медиа — может
            # только Gemini (OpenRouter физически не примет не-изображение, и
            # ни одна его модель не имеет доступа к поиску).
            chain = GEMINI_SEARCH_CHAIN if needs_freshness else GEMINI_HEAVY_CHAIN
            return _gemini_route(chain)
        # Изображение без нужды в поиске — сначала бесплатные vision-модели
        # OpenRouter, Gemini — резерв, если они все разом откажут.
        return _or_route(_OR_VISION_ORDER) + _gemini_route(GEMINI_HEAVY_CHAIN)

    if needs_freshness:
        # Текст без вложений, но нужна свежая информация — только у Gemini
        # реально есть поиск; OpenRouter в конце как резерв на случай, если
        # Gemini исчерпан целиком (без поиска, но хоть какой-то ответ), Groq —
        # последним (ответ по знаниям, если легли оба).
        return _gemini_route(GEMINI_SEARCH_CHAIN) + _or_route(_OR_HEAVY_ORDER if is_heavy else _OR_LIGHT_ORDER) + _groq_route(_GROQ_LIGHT_ORDER)

    # Основной случай: обычный текст без вложений/ссылок/признаков нужды в
    # интернете — сначала Groq (1000/день против 50 у OpenRouter — главный объём),
    # дальше OpenRouter, Gemini — резерв на случай отказа обоих разом.
    if is_heavy:
        return _or_route(_OR_HEAVY_ORDER) + _gemini_route(GEMINI_HEAVY_CHAIN)
    return _groq_route(_GROQ_LIGHT_ORDER) + _or_route(_OR_LIGHT_ORDER) + _gemini_route(GEMINI_SEARCH_CHAIN)
