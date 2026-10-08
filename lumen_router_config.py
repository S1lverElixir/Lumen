"""
lumen_router_config.py — модели и выбор маршрута (Gemini/OpenRouter/Groq) для одного сообщения.

Конфигурация плюс функции решения (_build_route, эвристики тяжести и свежести, фильтры квот). Обращений к API отсюда нет, bot.py импортирует имена напрямую.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

# Логгер bot, а не __name__: предупреждения ловят те же тесты и фильтры логов.
log = logging.getLogger("bot")


# ── модели ──

# Поля name/badge/desc убраны (июль 2026, /model удалена) — читаются только grounding/url_context/no_search/no_system/stream/quota_unconfirmed.
#
# ── Как дашборд AI Studio считает бесплатную квоту grounding-инструментов ──
# Бакет "Gemini 3" — 0/0 на search и map (24.07 + перепроверка 17.08.2026 + аудит дашборда 08.10.2026); квота на поиск только у 2.5-flash/lite, map — только у 3.5/3.1-lite.
GEMINI_MODELS: dict[str, dict[str, Any]] = {
    # Флагман линейки Flash (аудит 17.09.2026, бакет 5 RPM/250K TPM/20 RPD). Grounding False: бакет Gemini 3 без квоты на оба инструмента.
    "gemini-3.8-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # Прошлый флагман (GA 13 августа 2026), резерв после 3.8.
    "gemini-3.7-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # Резерв после 3.7.
    "gemini-3.6-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # Резерв после 3.6. url_context включён: у него нет отдельной дневной квоты.
    "gemini-3.5-flash": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },
    # 22.08.2026 восстановлена: офиц. страница от 18.08.2026 перевешивает вывод о ретирке. Grounding False по дашборду.
    "gemini-3-flash-preview": {
        "stream": True,
        "search_grounding": False, "map_grounding": False, "url_context": True,
    },

    # Быстрая и экономичная (21.07.2026, до 350 токенов/сек).
    "gemini-3.5-flash-lite": {
        "stream": True,
        "search_grounding": False, "map_grounding": True, "url_context": True,
    },
    # Резерв после 3.5 Flash-Lite.
    "gemini-3.1-flash-lite": {
        "stream": True,
        "search_grounding": False, "map_grounding": True, "url_context": True,
    },
    # Баланс скорости и качества. Единственное поколение с квотой search grounding (21/1500).
    "gemini-2.5-flash": {
        "stream": True,
        "search_grounding": True, "map_grounding": True, "url_context": True,
    },
    # Экономичная: скорость важнее глубины рассуждений.
    "gemini-2.5-flash-lite": {
        "stream": True,
        "search_grounding": True, "map_grounding": True, "url_context": True,
    },
    "gemma-4-31b-it": {
        "no_system": True, "no_search": True, "stream": True, "url_context": True,
    },
    "gemma-4-26b-a4b-it": {
        "no_system": True,
        # 24.07.2026: без no_search Gemma получала бы инструменты, которых не поддерживает.
        "no_search": True, "stream": True, "url_context": True,
    },
}
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# ── TTS-модели (аудит техдолга, август 2026) ──
GEMINI_TTS_MODELS: list[str] = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

# Fish free — только до 31.08.2026 (fish.audio/blog); зеркала больше нет в живом каталоге.
FISH_AUDIO_TTS_MODEL = "fish-audio/s2.1-pro-free:free"
# ОТКЛЮЧЕНО (аудит 17.09.2026): зеркала нет в живом каталоге OpenRouter, продление после 31.08.2026 не объявлено. Флаг и функция оставлены: при возврате free-доступа достаточно вернуть True.
FISH_AUDIO_ENABLED = False

def _check_unconfirmed_model_quotas() -> None:
    """Напоминаем про модели с quota_unconfirmed: квота/grounding ещё не подтверждены по дашборду."""
    for mid, conf in GEMINI_MODELS.items():
        if conf.get("quota_unconfirmed"):
            log.warning(
                "[setup] Real RPD limits and search/map grounding availability for model %s are NOT yet confirmed against the AI Studio dashboard (model was recently released) — the current search_grounding/map_grounding values in GEMINI_MODELS are a guess by analogy with a model of the same class. Check the dashboard and remove 'quota_unconfirmed' for this model in lumen_router_config.py, adjusting the config if needed.",
                mid,
            )

# Плоский список ID для детектора утечек, перепроверен по openrouter.ai (июль 2026). Не порядок роутера; снятые с тарифа оставляем: их тоже нельзя в ответ.
# nemotron-3.5-content-safety — классификатор, а не диалоговая модель, не включаем намеренно.
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
    # Groq-ID без :free-суффикса — те же семейства, что выше через OpenRouter; дословно в ответе им тоже не место.
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]
TEXT_MODEL_ORDER = _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION  # алиас для обратной совместимости
# Аудит 02.08.2026 (логи + каталог): laguna-s-2.1 и ling-3.0-flash, калибровку не проходили (только факт free-квоты).

# ── Единый реестр нездоровых моделей ──
# Один dict вместо трёх рассинхронных механизмов; excluded-множество вычисляется из него.
@dataclass(frozen=True)
class _ModelHealthNote:
    reason: str
    # Только для временного промо-доступа; у снятых навсегда поле пустое: предупреждать не о чем.
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
    # ── Логи прода 05.10.2026 ──
    "nex-agi/nex-n2.5-mini:free": _ModelHealthNote(
        reason="Логи прода 05.10.2026: 'This model is unavailable for free. The paid version is available now' — стояла второй в лёгкой цепочке."
    ),
    # ── Логи прода 05–07.10.2026: та же платная отсечка, что у nex-mini выше ──
    "inclusionai/ling-3.0-flash-vl:free": _ModelHealthNote(
        reason="Логи прода 05–06.10.2026: 'unavailable for free... use this slug instead' — замыкала vision-цепочку, каждая картинка теряла попытку."
    ),
    "inclusionai/ling-3.0-flash-fin:free": _ModelHealthNote(
        reason="Логи прода 07.10.2026: тот же 'unavailable for free' — стояла третьей в лёгкой цепочке."
    ),
    "qwen/qwen3.8-27b:free": _ModelHealthNote(
        reason="Liveness 07.10.2026: 'unavailable for free... use this slug instead: qwen/qwen3.8-27b' — тот же паттерн снятия с free."
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
# Будущий анонс не исключает живую модель: только подтверждённые логи; реестр лишь предупреждает.
_SCHEDULED_OR_REMOVALS: dict[str, date] = {
    "dots-studio/dots-3-note-preview:free": date(2026, 9, 30),
}

def _check_scheduled_removals_due() -> None:
    """Предупреждает, когда дата снятия наступила. >= вместо >: анонс действует уже в день снятия."""
    today = date.today()
    for model_id, removal_date in _SCHEDULED_OR_REMOVALS.items():
        if today >= removal_date and model_id not in _ROUTER_EXCLUDED_OR_MODELS:
            log.warning(
                '[or] Model %s was scheduled by OpenRouter to go away on %s (today is %s) — check recent logs for 404/"no endpoints" errors on this model, and add a dated _OR_MODEL_HEALTH entry if confirmed dead. Not excluded automatically — still selectable until confirmed.',
                model_id, removal_date.isoformat(), today.isoformat(),
            )

def _is_quota_exhausted(provider: str, model_id: str) -> bool:
    """Метка _mark_quota_exhausted после 429: роутер пропускает такие без траты попытки (аудит 26.09.2026). Сброс — смена суток или успешный ответ. Короткая остывка минутного 429 тоже пропускается (враждебное ревью 27.09.2026)."""
    try:
        import bot
        sub = bot.GLOBAL_QUOTA.get(provider) or {}
        entry = sub.get(model_id) or {}
        if entry.get("exhausted_at"):
            return True
        cooldown_until = entry.get("cooldown_until")
        return bool(cooldown_until and cooldown_until > time.time())
    except Exception as exc:
        # Битое состояние квоты чиним видимой ошибкой в логе, а не молчаливыми
        # лишними попытками: fail-open здесь — пропуск фильтра.
        log.warning("[router] Quota state unreadable, treating %s/%s as available: %s", provider, model_id, exc)
        return False

def _skip_exhausted(provider: str, models: list[str]) -> list[str]:
    """Убирает модели с меткой квоты и временным карантином. Все мёртвы — один первый разрешённый вариант (не из реестра исключённых): каждое сообщение платит максимум одно лишнее обращение, а не по одному за всю цепочку."""
    alive = [m for m in models if not _is_quota_exhausted(provider, m) and not _is_quarantined(provider, m)]
    if alive:
        return alive
    for candidate in models:
        if candidate not in _ROUTER_EXCLUDED_OR_MODELS:
            return [candidate]
    return []

# ── Временный карантин моделей ──
# Только в памяти: N плохих ответов подряд (пусто или mush-каша) — и роутер
# пропускает модель до конца суток квоты. Реестр _OR_MODEL_HEALTH не трогаем:
# туда только датированные доказательства.
_QUARANTINE: dict[tuple[str, str], dict[str, Any]] = {}

def _quarantine_threshold() -> int:
    """Сколько плохих подряд до карантина (MODEL_QUARANTINE_BAD_LIMIT, дефолт 3)."""
    try:
        raw = (os.getenv("MODEL_QUARANTINE_BAD_LIMIT", "") or "").strip()
        value = int(raw) if raw else 3
    except (TypeError, ValueError):
        return 3
    return max(1, value)

def _quarantine_entry(provider: str, model_id: str) -> dict[str, Any]:
    """Сегодняшняя запись счётчика: смена суток квоты обнуляет молча."""
    import bot
    today = bot._current_quota_day()
    key = (provider, model_id)
    entry = _QUARANTINE.get(key)
    if entry is None or entry.get("day") != today:
        entry = {"bad": 0, "day": today}
        _QUARANTINE[key] = entry
    return entry

def _record_model_outcome(provider: str, model_id: str, *, bad: bool) -> None:
    """Плохой ответ +1 к счётчику (на пороге — карантин с записью в лог),
    успешный — сбрасывает счётчик."""
    if not bad:
        if _QUARANTINE.pop((provider, model_id), None) is not None:
            log.debug("[quarantine] %s/%s answered well, counter reset.", provider, model_id)
        return
    entry = _quarantine_entry(provider, model_id)
    entry["bad"] = int(entry.get("bad") or 0) + 1
    if entry["bad"] == _quarantine_threshold():
        log.warning(
            "[quarantine] %s/%s quarantined until end of quota day %s after %d consecutive bad responses (empty or garbled).",
            provider, model_id, entry["day"], entry["bad"],
        )

def _is_quarantined(provider: str, model_id: str) -> bool:
    try:
        import bot
        entry = _QUARANTINE.get((provider, model_id))
        return bool(entry and entry.get("day") == bot._current_quota_day()
                    and int(entry.get("bad") or 0) >= _quarantine_threshold())
    except Exception as exc:
        # Тот же fail-open, что у _is_quota_exhausted: битое состояние не должно
        # молча выкидывать живые модели из маршрута.
        log.warning("[router] Quarantine state unreadable, treating %s/%s as available: %s", provider, model_id, exc)
        return False

def _quarantine_status() -> list[tuple[str, str, int]]:
    """Кто сейчас в карантине: для /stats. Пустые и вчерашние записи не показываем."""
    try:
        import bot
        today = bot._current_quota_day()
        threshold = _quarantine_threshold()
        return sorted(
            (provider, model_id, int(entry.get("bad") or 0))
            for (provider, model_id), entry in _QUARANTINE.items()
            if entry.get("day") == today and int(entry.get("bad") or 0) >= threshold
        )
    except Exception:
        return []

def _or_route(models: list[str]) -> list[tuple[str, str]]:
    """Список ID в пары (provider, model_id), минус реестр исключённых и модели с исчерпанной квотой."""
    return [("openrouter", m) for m in _skip_exhausted("openrouter", models) if m not in _ROUTER_EXCLUDED_OR_MODELS]

def _gemini_route(models: list[str]) -> list[tuple[str, str]]:
    # Отдельного реестра здоровья, как _OR_MODEL_HEALTH, здесь нет осознанно:
    # временные отказы (503/unavailable) гасит остывка _mark_model_unavailable на
    # QUOTA_RATE_LIMIT_COOLDOWN_SEC, а снятие модели — только с датированным
    # подтверждением (аудит M8, 10.2026). При инциденте убирать из ORDER-списков с датой.
    return [("gemini", m) for m in _skip_exhausted("gemini", models)]

# ── Groq (прямой провайдер, не через OpenRouter) ──
# Калибровка живьём 21.09.2026: Qwen голова (чисто и по делу), gpt-oss второй (представляется ChatGPT и тратит reasoning-токены). Лимиты free: 30 RPM / 1000 RPD / 8K TPM / 200K TPD.
# Базовый URL один: bot.GROQ_BASE_URL, дубля здесь нет (аудит 05.10.2026).
_GROQ_LIGHT_ORDER: list[str] = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]

def _groq_route(models: list[str]) -> list[tuple[str, str]]:
    # То же про остывку вместо реестра, что у _gemini_route выше.
    return [("groq", m) for m in _skip_exhausted("groq", models)]


# ── "Лёгкие" запросы — самый частый маршрут, целиком OpenRouter.
# Порядок по аудитам 22.08/17.09.2026; nex-mini повышен продом 17.09.2026, снят
# 05.10.2026 (платная отсечка — см. _OR_MODEL_HEALTH): второй стала sante.
# 08.10.2026: fin и qwen3.8-27b:free сняты той же отсечкой — убраны из цепочки.
# 08.10.2026: голова — sante (фактически отвечает в проде), nemotron-3.5-lightning
# понижен в хвост (таймауты в проде 07.10.2026) — резервом перед openrouter/free.
_OR_LIGHT_ORDER: list[str] = [
    "inclusionai/ling-3.0-flash-sante:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "openrouter/free",
]

# ── "Тяжёлые" запросы без медиа — сначала OpenRouter, квоту Gemini бережём.
# super-120b понижен под ultra-550b (второй инцидент порчи текста 17.09.2026; при третьем — исключать).
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

# ── Картинки без свежести — vision OpenRouter (только base64-картинки).
# Голова gemma-4-31b (квота подтверждена); ling-vl снят 08.10.2026 той же
# платной отсечкой (см. _OR_MODEL_HEALTH) — убран из цепочки.
_OR_VISION_ORDER: list[str] = [
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free",
]

# ── Цепочки Gemini, где нужен именно Gemini.
# Голова — 2.5-flash: единственная, кто фактически отвечал в проде 05–07.10.2026,
# и единственная с реальной квотой search grounding (линейка 3.x — 0/0, аудит
# ДД.ММ.ГГГГ по дашборду AI Studio владельца).
# 08.10.2026: линейка 3.x ВОЗВРАЩЕНА в цепочки. Ошибки 503 на неё в проде 05.10 —
# не снятие моделей: официальный список моделей (обновлён 06.10.2026) и дашборд
# показывают живые лимиты у gemini-3.8/3.7/3.6/3.5-flash и gemini-3-flash-preview.
# Отход от этой линейки теперь делает не список цепочек, а остывка по "unavailable"
# (см. _mark_model_unavailable в lumen_routes.py) — модель с 503 пропускается на
# QUOTA_RATE_LIMIT_COOLDOWN_SEC, а не тратит попытку на каждом сообщении.
GEMINI_HEAVY_CHAIN: list[str] = [
    "gemini-2.5-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]
# Сначала модели с реальной квотой search grounding (2.5-flash/lite), линейка 3.x — резервом (ответ по знаниям/url_context).
GEMINI_SEARCH_CHAIN: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
]
# Дефолт для прямых вызовов ask_gemini без явной цепочки.
# Копия, а не алиас: раньше правка дефолта молча меняла тяжёлую цепочку (аудит 26.09.2026).
GEMINI_DEFAULT_CHAIN: list[str] = list(GEMINI_HEAVY_CHAIN)
# Сайты по ссылке и YouTube умеют только "полноценные" (не no_system/Gemma) модели.
GEMINI_LINK_CHAIN: list[str] = [m for m in GEMINI_HEAVY_CHAIN if not GEMINI_MODELS.get(m, {}).get("no_system")]
GEMINI_LINK_SEARCH_CHAIN: list[str] = [m for m in GEMINI_SEARCH_CHAIN if not GEMINI_MODELS.get(m, {}).get("no_system")]


# ── Эвристика тяжёлого запроса без LLM.
# Ложные срабатывания дёшевы: худшее — чуть более мощная модель, а не отказ.
_HEAVY_QUERY_RE = re.compile(
    r"напиши\s+(код|функци\w*|скрипт|программ\w*|класс\w*|запрос\s+sql|regex|регуляр\w*|тест\w*|парсер\w*|бот\w*|сайт\w*|приложени\w*)"
    r"|сгенерируй\s+код|исправь\s+(код|баг|ошибк\w*)|отрефактор\w*|рефактор\w*|оптимизируй"
    r"|разбер(и|ём|ись)\s+(этот\s+|подробно\s+)?(код|ошибк\w*|баг\w*|текст\w*|документ\w*|подробно)"
    r"|найди\s+(ошибк\w*|баг\w*)|объясни\s+(код|ошибк\w*)"
    r"|напиши\s+(эссе|статью|доклад|реферат|сочинение|резюме|cv)\b"
    r"|проанализируй\w*|разбер(и|ём)\s+подробно|объясни\s+подробно"
    # Голое "сравни" — тоже сравнение (прод 17.09.2026).
    r"|сравни\b|докажи\b|доказательство"
    r"|реши\s+(задач\w*|уравнени\w*|систем\w*)"
    r"|составь\s+(план|таблиц\w*|список\s+из)"
    r"|многошагов\w*|пошагов\w*\s+(инструкц\w*|план\w*)"
    r"|архитектур\w*|алгоритм\w*"
    # EN-набор: детект был почти весь русский (внешний аудит). Границы слов обязательны: prove ловил improve (ревью ветки).
    r"|write\s+(a\s+|an\s+|the\s+)?(\w+\s+)?(code|function\w*|script\w*|program\w*|class\w*|sql|regex|test\w*|parser\w*|bot|website\w*|app\w*)\b"
    r"|generate\s+code|fix\s+(this\s+|that\s+)?(code|bug|error|issue)\b|refactor\w*|optimiz\w*|debug"
    r"|explain\s+(code|error)|code\s+review|algorithm|architecture"
    r"|write\s+(an?\s+)?(essay|article|report|paper|thesis|cv|resume)\b"
    r"|compar(e|ison)|\bprove\b|\bproof\b|\bsolve\b|equation",
    re.IGNORECASE,
)

def _looks_like_heavy_query(text: str) -> bool:
    """Грубая эвристика тяжёлого запроса без вызова LLM, намеренно консервативная."""
    if not text:
        return False
    if "```" in text or len(text) > 600:
        return True
    if text.count("?") >= 3:
        return True
    return bool(_HEAVY_QUERY_RE.search(text))


# ── Эвристика свежести без LLM.
# Ложные срабатывания дёшевы: модель сама решает, вызывать ли поиск.
# Годы свежести — окном от текущей даты, а не зашитым диапазоном (иначе протухает).
_FRESHNESS_YEAR_ALTS = "|".join(str(date.today().year + i) for i in range(4))
_FRESHNESS_QUERY_RE = re.compile(
    r"сейчас|сегодня|текущ\w*|последн\w*|актуальн\w*|свеж\w*|недавно|на\s+данный\s+момент"
    r"|новост\w*|курс\s+(валют|доллара|евро|рубл\w*)|погод\w*|прогноз\w*"
    r"|расписани\w*|афиш\w*"
    r"|цена\w*|стоимост\w*|сколько\s+стоит"
    # Советы по покупке (прод 17.09.2026): цены и наличие протухают, ответ из памяти врёт.
    r"|лучше\s+(всего\s+)?(брать|взять|купить|выбрать)"
    r"|(брать|взять|купить|выбрать)\s+лучше"
    r"|кто\s+(сейчас|является|президент|премьер|глава|ceo|мэр)"
    r"|результат\w*\s+(матч\w*|игр\w*|выбор\w*)"
    r"|в\s+эт(ом|ой)\s+(году|месяце|неделе)"
    rf"|\b(?:{_FRESHNESS_YEAR_ALTS})\b"
    # EN-набор: детект был только русским при 25 языках (внешний аудит). Границы слов обязательны: now ловил know/snow.
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
    """Приоритетный список кандидатов (provider, model_id), непустой; первый пробуется первым. Внутри провайдера — по возрастанию цены для дефицитной квоты."""
    is_video_or_audio_media = bool(media_mime) and not media_mime.startswith("image/")

    if needs_youtube or needs_website:
        # Сайты и YouTube читает только Gemini — эскалировать некуда.
        chain = GEMINI_LINK_SEARCH_CHAIN if needs_freshness else GEMINI_LINK_CHAIN
        return _gemini_route(chain)

    if media_mime:
        if is_video_or_audio_media:
            # Видео/аудио — только Gemini (OpenRouter не примет не-изображение).
            chain = GEMINI_SEARCH_CHAIN if needs_freshness else GEMINI_HEAVY_CHAIN
            return _gemini_route(chain)
        if needs_freshness:
            # Картинка+свежесть: Gemini с поиском головой, дальше vision
            # OpenRouter без поиска (аудит D3, 30.09.2026: раньше резерва не было).
            return _gemini_route(GEMINI_SEARCH_CHAIN) + _or_route(_OR_VISION_ORDER)
        # Картинка без поиска — сначала vision OpenRouter, Gemini резервом.
        return _or_route(_OR_VISION_ORDER) + _gemini_route(GEMINI_HEAVY_CHAIN)

    if needs_freshness:
        # Текст со свежестью — Gemini с поиском; дальше OpenRouter и Groq резервом без поиска.
        return _gemini_route(GEMINI_SEARCH_CHAIN) + _or_route(_OR_HEAVY_ORDER if is_heavy else _OR_LIGHT_ORDER) + _groq_route(_GROQ_LIGHT_ORDER)

    # Обычный текст — сначала Groq (1000/день против 50 у OpenRouter), дальше OpenRouter, Gemini резервом.
    # Тяжёлый — тоже с Groq первой: свободная квота Groq-Tier экономит scarce-квоты
    # OR/Gemini (аудит D3, 30.09.2026); не потянет — цепочка уйдёт дальше сама.
    if is_heavy:
        return _groq_route(_GROQ_LIGHT_ORDER) + _or_route(_OR_HEAVY_ORDER) + _gemini_route(GEMINI_HEAVY_CHAIN)
    return _groq_route(_GROQ_LIGHT_ORDER) + _or_route(_OR_LIGHT_ORDER) + _gemini_route(GEMINI_SEARCH_CHAIN)
