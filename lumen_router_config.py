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

# name/badge/desc/public_name/public_desc, ранее украшавшие каждую запись здесь,
# убраны целиком (ponytail-audit, июль 2026) — это были чисто отображаемые строки
# для команды /model, которая с тех пор удалена (см. "автоматический выбор модели"
# ниже); ни одно из них нигде не читалось. Настоящая модель, которую обозначает
# каждый ключ, и так понятна по самому ключу и по комментариям ниже — ничего не
# потеряно. Единственные поля, которые здесь реально используются: search_grounding/
# map_grounding/url_context/no_search/no_system/stream (см. _build_gemini_call_config)
# и quota_unconfirmed (см. _check_unconfirmed_model_quotas).
#
# ── Как дашборд AI Studio считает бесплатную квоту grounding-инструментов ──
# (подтверждено 24 июля 2026 по реальному дашборду владельца, ПЕРЕПРОВЕРЕНО
# 17 августа 2026 на актуальном дашборде — картина не изменилась, только
# добавился Gemini 3.7 Flash в тот же бакет "Gemini 3", относится ко ВСЕЙ
# линейке ниже — отдельно для каждой модели дальше не повторяется). Search
# grounding считается не по конкретной модели, а по общему бакету ПОКОЛЕНИЯ:
# бакет "Gemini 3" (объединяет 3/3.1/3.5/3.6/3.7) — 0/0, то есть реальной квоты
# на поиск нет ни у одной модели линейки Gemini 3.x, сколько бы ни было соблазна
# предположить "раз lite-класс — значит есть квота" (именно так ошиблись раньше
# с 3.5/3.1 Flash-Lite, см. историю правок). Бакет "Gemini 2.5" по дашборду
# 17 августа 2026 показывает 0/1.5K (используемая часть на момент снимка/
# доступный лимит) — реальная рабочая квота на поиск по-прежнему только у
# gemini-2.5-flash/-flash-lite (поэтому они первые в GEMINI_SEARCH_CHAIN ниже).
# Map grounding — наоборот,
# считается ПО КОНКРЕТНОЙ модели: у 3.5/3.1 Flash-Lite он реально есть
# (500/сутки), у остальных моделей линейки 3.x — 0/0.
GEMINI_MODELS: dict[str, dict[str, Any]] = {
    # Gemini 3.7 Flash — новый флагман линейки Flash, вышел 13 августа 2026 (GA),
    # сменяет 3.6 Flash ("наша самая умная рабочая лошадка для разработки и
    # агентных сценариев" — офиц. анонс Google). Тот же набор инструментов, что
    # и у 3.6 Flash (см. release notes ai.google.dev/gemini-api/docs/changelog),
    # контекст 1 млн токенов. RPD-лимит подтверждён по дашборду AI Studio
    # (аудит моделей, 17 августа 2026) — тот же бакет 5 RPM/250K TPM/20 RPD, что
    # и у 3.6 Flash, поэтому не помечена quota_unconfirmed.
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
    # УБРАНО (аудит моделей, 17 августа 2026): "gemini-3-flash-preview" был здесь
    # резервом после 3.5 Flash — подтверждено по официальной документации Google
    # (ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.5:
    # "Update model name: gemini-3-flash-preview → gemini-3.5-flash") и
    # независимо по дате ретирки (retired 15 июля 2026, см. карточку модели на
    # ollama.com/library/gemini-3-flash-preview) — идентификатор больше не
    # обслуживается API, вызов гарантированно вернёт ошибку "модель не найдена".
    # Официальная замена (gemini-3.5-flash) уже есть в цепочке строкой выше —
    # отдельного резерва вместо убранной модели не требуется.
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
        # НАЙДЕНО при перепроверке конфига (24 июля 2026): у "родственной" модели
        # gemma-4-31b-it выше стоит "no_search": True (Gemma, как открытая модель,
        # не поддерживает grounding-инструменты Gemini API в принципе), а здесь этот
        # флаг был случайно пропущен. Без него _build_gemini_call_config по умолчанию
        # (search_grounding/url_context по умолчанию True при отсутствии ключа в конфиге)
        # пытался бы добавить в запрос google_search И url_context для модели, которая
        # их не поддерживает вообще — реальный риск ошибки API на КАЖДЫЙ вызов этой
        # модели (она сейчас последняя в GEMINI_HEAVY_CHAIN, поэтому баг маловероятно
        # проявлялся на практике, но был реальным). Добавлено для консистентности с 31B.
        "no_search": True, "stream": True,
    },
}
DEFAULT_GEMINI_MODEL = "gemini-3.7-flash"

# ── TTS-модели (аудит техдолга, август 2026) ──
# GEMINI_TTS_MODELS раньше был отдельным хардкодом внутри _gemini_tts_bytes в bot.py —
# второй, не связанный с GEMINI_MODELS источник правды об именах моделей Gemini. Перенесено
# сюда по тому же принципу, что и остальная конфигурация моделей.
GEMINI_TTS_MODELS: list[str] = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

# Fish Audio S2.1 Pro пробуется ПЕРВОЙ в inline_tts (см. bot.py) — бесплатный доступ
# обещан провайдером только до этой даты (fish.audio/blog/s2-1-pro-free-api). Раньше
# истечение отслеживалось только комментарием в коде, без автоматической проверки — тот
# же класс пробела, из-за которого истечение tencent/hy3:free было замечено постфактум,
# а не заранее. Проверяется тем же ежесуточным циклом, что и _check_temporary_free_models_expiry.
FISH_AUDIO_TTS_MODEL = "fish-audio/s2.1-pro-free:free"
FISH_AUDIO_FREE_TIER_EXPIRY = date(2026, 8, 31)

def _check_fish_audio_tts_expiry() -> None:
    today = date.today()
    if today > FISH_AUDIO_FREE_TIER_EXPIRY:
        log.warning(
            '[tts] The advertised free-tier access to %s expired on %s (today is %s) — check fish.audio/blog/s2-1-pro-free-api in case it was extended again, and update FISH_AUDIO_FREE_TIER_EXPIRY. If access is really gone, _fish_audio_tts_bytes in bot.py already falls back to Gemini TTS silently on any failure — nothing breaks functionally, but the wasted failing requests are worth removing.',
            FISH_AUDIO_TTS_MODEL, FISH_AUDIO_FREE_TIER_EXPIRY.isoformat(), today.isoformat(),
        )

def _check_unconfirmed_model_quotas() -> None:
    """Модели, добавленные сразу после релиза (см. quota_unconfirmed=True в
    GEMINI_MODELS), — их реальные RPD-лимиты и доступность search/map grounding
    ещё не подтверждены по дашборду AI Studio (дашборд обновляется с задержкой
    после релиза модели, иногда на несколько дней). Громко напоминаем при
    каждом старте, пока флаг не снят вручную после реальной проверки — та же
    идея, что и у _check_temporary_free_models_expiry выше, только для новых,
    а не для истекающих моделей."""
    for mid, conf in GEMINI_MODELS.items():
        if conf.get("quota_unconfirmed"):
            log.warning(
                "[setup] Real RPD limits and search/map grounding availability for model %s are NOT yet confirmed against the AI Studio dashboard (model was recently released) — the current search_grounding/map_grounding values in GEMINI_MODELS are a guess by analogy with a model of the same class. Check the dashboard and remove 'quota_unconfirmed' for this model in bot.py, adjusting the config if needed.",
                mid,
            )

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: раньше здесь был словарь OPENROUTER_MODELS["text"]
# со списком dict'ов {"id", "name", "description"} на ~25 моделей — то же самое
# "name/badge/desc", что уже было вычищено из GEMINI_MODELS (см. комментарий там,
# ponytail-audit, июль 2026), но по ошибке не сделано для OpenRouter. "name"/
# "description" были чисто отображаемыми строками для команды /model, которая
# с тех пор удалена (см. README, "Автоматический выбор модели") — единственное
# реальное использование всего словаря было `[m["id"] for m in ...]`. Раз
# описания нигде не читаются, оставляем сразу плоский список ID — тот же
# TEXT_MODEL_ORDER, что раньше вычислялся ИЗ словаря, теперь и есть сам список.
#
# Список перепроверен вручную по openrouter.ai (июль 2026) — модель за моделью,
# т.к. часть ID из старого списка либо сняты с бесплатного тира (arcee-ai/trinity-
# large-thinking:free — акция закончилась 23.05, теперь платная; baidu/cobuddy:free —
# больше не бесплатна), либо заменены провайдером на новую версию (poolside/laguna-xs.2:free
# официально сворачивается в пользу laguna-xs-2.1:free). nvidia/nemotron-3.5-content-safety:free
# НАМЕРЕННО не включена — это guardrail/классификатор safe/unsafe, а не диалоговая модель,
# добавлять её сюда бессмысленно и вредно (не будет отвечать текстом на вопросы).
# ПЕРЕИМЕНОВАНО (аудит техдолга, август 2026): этот список больше НЕ используется как
# источник порядка для роутера — тот давно живёт отдельно в _OR_LIGHT_ORDER/_OR_HEAVY_ORDER/
# _OR_VISION_ORDER. Единственный оставшийся потребитель — _LEAK_LITERAL_STRINGS в
# lumen_security.py (список известных ID моделей, которые не должны дословно всплывать в
# ответе). Устаревшие/снятые с тарифа модели здесь оставлять безопасно и даже нужно — их
# ID всё ещё нельзя допускать в ответ. Старое имя TEXT_MODEL_ORDER сохранено ниже как
# алиас, чтобы не ломать импорт в lumen_security.py и внешние тесты одним махом.
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
    # ДОБАВЛЕНО (аудит моделей, 17 августа 2026, по актуальному "Top Weekly free"
    # каталогу OpenRouter, сверено точными слагами через web-поиск по офиц.
    # страницам openrouter.ai — см. историю правок): обе модели новые (вышли
    # первая-вторая неделя августа 2026), ещё ни разу не прогонялись через
    # калибровочное сравнение с Claude Sonnet.
    "nvidia/nemotron-3.5-lightning:free",
    "dots-studio/dots-3-note-preview:free",
]
TEXT_MODEL_ORDER = _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION  # алиас для обратной совместимости
# ПОПОЛНЕНО (аудит моделей, 2 августа 2026, по реальным логам продакшена + сверке
# с живым каталогом OpenRouter): добавлены poolside/laguna-s-2.1:free (новый
# средний вариант линейки Laguna, появился в каталоге в конце июля 2026 вместе с
# ling-3.0-flash) и inclusionai/ling-3.0-flash:free (самая используемая по объёму
# токенов свежедобавленная модель на дашборде владельца — 1.49T токенов/неделю,
# уступает только nemotron-3-ultra). Обе пока НЕ прогонялись через калибровочное
# сравнение с Claude Sonnet (см. историю проекта про калибровочные сессии) — качество
# и устойчивость на русском языке не подтверждены вручную, только сам факт наличия
# бесплатной квоты. См. _OR_LIGHT_ORDER ниже про фактическое место в маршруте.

# ── Единый реестр "нездоровых" моделей OpenRouter (аудит техдолга, август 2026) ──
# РАНЬШЕ это отслеживалось ТРЕМЯ независимыми механизмами: _TEMPORARY_FREE_MODELS
# (dict с датой истечения промо), _ROUTER_EXCLUDED_OR_MODELS (отдельное множество
# для ручного исключения из роутинга) и точечные комментарии в _OR_LIGHT_ORDER/
# _OR_HEAVY_ORDER о моделях, вычеркнутых оттуда вручную. Три реальных инцидента
# (tencent/hy3:free, qwen/qwen3-coder:free, qwen/qwen3-next-80b-a3b-instruct:free)
# потребовали правок в 2-3 местах каждый — ровно тот класс рассинхрона, которого
# проект и так избегает в других местах (см. TEXT_MODEL_ORDER/_next_fallback_model
# выше). Теперь один dict хранит причину/срок для каждой проблемной модели, а
# _ROUTER_EXCLUDED_OR_MODELS и предупреждение об истёкшем промо вычисляются ИЗ
# него, а не поддерживаются параллельно вручную.
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
        reason="ПОДТВЕРЖДЕНО ПО РЕАЛЬНЫМ ЛОГАМ ПРОДА (25 июля 2026, ~40 минут живого трафика, 20+ "
               "попыток подряд): HTTP 404 абсолютно каждый раз — 'This model is unavailable for "
               "free... use this slug instead: qwen/qwen3-next-80b-a3b-instruct' (платный слаг). "
               "Не временное промо, а прямая инструкция провайдера использовать другой (платный) "
               "слаг — не возвращать в _OR_*_ORDER, пока провайдер вновь не откроет бесплатный "
               "доступ именно к этому слагу."
    ),
    # ── Найдено при аудите моделей 2 августа 2026 (реальные логи прода, ~5 часов
    # живого трафика, 18 обработанных сообщений) ──
    "z-ai/glm-4.5-air:free": _ModelHealthNote(
        reason="ПОДТВЕРЖДЕНО ПО РЕАЛЬНЫМ ЛОГАМ ПРОДА (2 августа 2026, 8 попыток подряд за ~5 часов, "
               "во всех — идентичная ошибка): HTTP 404 'This model is unavailable for free. The paid "
               "version is available now - use this slug instead: z-ai/glm-4.5-air' — тот же самый "
               "паттерн, что и у уже подтверждённых мёртвых моделей выше. Модель также отсутствует в "
               "собственном 'Top Weekly free' дашборде OpenRouter владельца, хотя по историческому "
               "объёму токенов должна была бы там появиться, если бы бесплатный доступ ещё "
               "действовал. Раньше стояла первой в _OR_LIGHT_ORDER и третьей в _OR_HEAVY_ORDER — "
               "именно она открывала цепочку почти на каждом обычном сообщении."
    ),
    "meta-llama/llama-3.2-3b-instruct:free": _ModelHealthNote(
        reason="ПОДТВЕРЖДЕНО ПО РЕАЛЬНЫМ ЛОГАМ ПРОДА (2 августа 2026, 7 попыток подряд, идентичная "
               "ошибка каждый раз): HTTP 404 'This model is unavailable for free. The paid version is "
               "available now - use this slug instead: meta-llama/llama-3.2-3b-instruct'. Тот же "
               "провайдерский паттерн снятия с бесплатного тира, что и у llama-3.3-70b (уже "
               "исключена) — Meta, судя по всему, убрала весь бесплатный тир линейки Llama целиком."
    ),
    "liquid/lfm-2.5-1.2b-instruct:free": _ModelHealthNote(
        reason="ПОДТВЕРЖДЕНО ПО РЕАЛЬНЫМ ЛОГАМ ПРОДА (2 августа 2026): 'No endpoints found for "
               "liquid/lfm-2.5-1.2b-instruct:free.' — это НЕ таймаут и не перегрузка, а прямой сигнал "
               "от OpenRouter, что для этого слага прямо сейчас не существует ни одного обслуживающего "
               "провайдера вообще. Также отсутствует в текущем живом каталоге бесплатных моделей "
               "OpenRouter (сверено отдельно от логов)."
    ),
    "liquid/lfm-2.5-1.2b-thinking:free": _ModelHealthNote(
        reason="Не поймана напрямую в логах (соседняя liquid/lfm-2.5-1.2b-instruct:free — поймана, "
               "см. выше), но тоже отсутствует в текущем живом каталоге бесплатных моделей OpenRouter — "
               "похоже, LiquidAI сняли оба lfm-2.5-1.2b слага с бесплатного тира одновременно. Более "
               "низкая уверенность, чем у остальных записей в этом реестре — если у владельца будет "
               "прямое подтверждение (успешный вызов или другая ошибка, не 'no endpoints') — эту запись "
               "стоит убрать."
    ),
    "nousresearch/hermes-3-llama-3.1-405b:free": _ModelHealthNote(
        reason="Внешне подтверждено (не поймано напрямую в логах владельца — heavy-маршрут в этом "
               "окне логов не запускался): независимый снимок публичного API OpenRouter от 27 июля "
               "2026 явно называет эту модель в числе семи, снятых с бесплатного тира в те же девять "
               "дней, что и уже независимо подтверждённые в этом же реестре llama-3.2-3b/llama-3.3-70b/"
               "qwen3-coder/qwen3-next-80b/tencent-hy3/dolphin-mistral-venice — 5 из 7 моделей того "
               "снимка уже были подтверждены именно этим проектом независимо, что даёт высокую "
               "уверенность и в оставшихся двух (вторая — dolphin-mistral, уже была исключена по "
               "другой причине выше)."
    ),
    # ── Найдено при калибровочном прогоне против Claude Sonnet 5 (18 августа 2026,
    # реальные продакшен-логи + скриншоты Telegram) ──
    "nvidia/nemotron-nano-9b-v2:free": _ModelHealthNote(
        reason="ПОДТВЕРЖДЕНО ПО РЕАЛЬНЫМ ЛОГАМ ПРОДА И СКРИНШОТАМ (18 августа 2026): стояла первой в "
               "_OR_LIGHT_ORDER и реально ответила на 37 из 45 (82%) успешных лёгких текстовых запросов "
               "за сессию — но КАЖДЫЙ развёрнутый ответ (2+ предложения) содержал грубую порчу текста: "
               "случайные фрагменты слов из десятка не относящихся к разговору языков, вклиненные "
               "прямо ВНУТРЬ русских слов и предложений (подтверждённые примеры из одной сессии: "
               "испанское 'modelo' вместо 'модель', португальское 'mesmo'/'ambiental', французское "
               "'sommet'/'fermer'/'pente'/'Série', арабское 'زراعة' и 'دان', корейское '택' и '종료', "
               "китайское '欧洲' и '諜', японская катакана 'キュ', деванагари 'कंपनी', вьетнамское "
               "'tiền', итальянское 'militarizzazione', полностью вымышленное имя сооснователя "
               "Anthropic 'lagoo Isaacs' вместо реальных Дарио/Дэниэлы Амодей, и медицинский термин "
               "'carcinogenesis', случайно подставленный вместо слова 'убийство' в разборе причин "
               "Первой мировой войны). Модель НЕ мертва технически (не 404, отвечает и тратит "
               "заметную часть бесплатного дневного лимита), но выдаёт продакшен-трафику постоянно "
               "испорченный на нечитаемость текст — тот самый класс проблемы, ради которого этот "
               "реестр уже исключает uncensored/некачественные модели (см. dolphin-mistral выше), а "
               "не только официально снятые с тарифа. Дополнительно (не решающий, но подтверждающий "
               "аргумент): вопреки размеру 'nano'/9B и позиции головы 'лёгкой' цепочки, реальные "
               "длительности стриминга по логам — от 12 до 92 секунд на ответ, то есть заметно "
               "медленнее, чем у более крупных моделей дальше по списку — таблоид-эффект 'самая "
               "быстрая и лёгкая' на практике не подтвердился ни разу за сессию."
    ),
}

# Вычисляется ИЗ _OR_MODEL_HEALTH выше — единственное место, где решается, какие
# модели роутер не должен выбирать (см. _or_route дальше по файлу).
_ROUTER_EXCLUDED_OR_MODELS: frozenset[str] = frozenset(_OR_MODEL_HEALTH.keys())

def _check_temporary_free_models_expiry() -> None:
    """Предупреждает в логах (при каждом старте и раз в сутки, см. фоновый цикл в
    _webhook_startup) про модели с истёкшим временным промо-доступом — на случай,
    если запись когда-нибудь понадобится вернуть в оборот и стоит перепроверить
    актуальную цену на openrouter.ai. Модели без promo_expiry (сняты навсегда, а
    не по истечении акции) сюда не попадают — предупреждать об "истечении" для
    них нечего."""
    today = date.today()
    for model_id, note in _OR_MODEL_HEALTH.items():
        if note.promo_expiry is not None and today > note.promo_expiry:
            log.warning(
                '[or] Temporary free access to model %s expired on %s (today is %s) — %s The router no longer selects it (_ROUTER_EXCLUDED_OR_MODELS), but check the current price on openrouter.ai if you ever need to bring it back.',
                model_id, note.promo_expiry.isoformat(), today.isoformat(), note.reason,
            )

def _or_route(models: list[str]) -> list[tuple[str, str]]:
    """Превращает список ID моделей OpenRouter в список (provider, model_id) для
    маршрута, попутно исключая модели из _ROUTER_EXCLUDED_OR_MODELS."""
    return [("openrouter", m) for m in models if m not in _ROUTER_EXCLUDED_OR_MODELS]

def _gemini_route(models: list[str]) -> list[tuple[str, str]]:
    return [("gemini", m) for m in models]


# ── "Лёгкие"/"стандартные" запросы без вложений и ссылок — САМЫЙ ЧАСТЫЙ
# маршрут в обычном чате. Целиком обслуживается OpenRouter'ом, чтобы вообще не
# трогать скудную квоту Gemini на самом массовом классе сообщений.
#
# Текущий порядок и его основания (последний аудит — 2 августа 2026 по реальным
# логам прода за ~5 часов; калибровочное сравнение с Claude Sonnet — 25 июля):
# - ling-3.0-flash — самая используемая по объёму токенов свежедобавленная
#   бесплатная модель на дашборде OpenRouter (1.49T токенов/неделю), но
#   качество на русском ещё НЕ проверено калибровочным сравнением — стоит
#   последить за первыми ответами, прежде чем поднимать выше.
# - nemotron-3-nano-30b-a3b понижена, но не убрана: калибровка нашла 3
#   инцидента порчи текста (деванагари-мусор внутри слов, сырой LaTeX вопреки
#   прямому запрету в system_prompt.py, галлюцинация названия фильма) —
#   однако по логам прода она же реально отвечает чаще всех остальных
#   кандидатов этого списка. Баланс между подтверждённой доступностью и
#   подтверждённым качеством — осознанное решение владельца, а не
#   автоматическое повышение по одной лишь доступности.
# - gpt-oss-20b понижена по тем же основаниям (смесь языков и нечитаемые
#   фрагменты на 2 из ~8 наблюдавшихся вызовов), но не убрана — если и более
#   мелкие модели ниже в цепочке дадут похожие инциденты, тогда стоит
#   рассмотреть полное исключение, а не просто понижение приоритета.
#
# Модели, убранные из списка целиком (провайдер снял с бесплатного тира, слаг
# подтверждённо не обслуживается, или подтверждена грубая порча текста — полные
# причины и даты см. в _OR_MODEL_HEALTH выше): meta-llama/llama-3.3-70b-instruct,
# qwen/qwen3-next-80b-a3b-instruct, z-ai/glm-4.5-air, meta-llama/llama-3.2-3b-
# instruct, liquid/lfm-2.5-1.2b-instruct, nvidia/nemotron-nano-9b-v2 (стояла
# первой здесь — см. подробный разбор порчи текста на реальном трафике в записи
# реестра выше, калибровка 18 августа 2026).
#
# nemotron-3.5-lightning ДОБАВЛЕНА (аудит моделей, 17 августа 2026, по
# актуальному "Top Weekly free" каталогу OpenRouter): 30B MoE (3B активных),
# NVIDIA прямо позиционирует её для "высокообъёмных, специализированных
# агентных задач" — та же роль, что и у остальных моделей этого списка.
# Вышла всего неделю назад (11 августа 2026) — калибровочного сравнения с
# Claude Sonnet ещё не было ни разу, поэтому поставлена последней перед
# generic-резервом, а не выше уже проверенных калибровкой моделей.
#
# ПОСЛЕ ИСКЛЮЧЕНИЯ nemotron-nano-9b-v2 (см. выше) ling-3.0-flash становится
# НОВОЙ головой списка — ни разу не поймана в порче текста в той же сессии
# логов, но и калибровочного сравнения с Claude Sonnet у неё тоже пока не было
# (см. её же комментарий в докстринге секции ниже про 1.49T токенов/неделю).
# Стоит присмотреться к первым реальным ответам именно в этой новой роли головы
# списка — если порча обнаружится и там, кандидат на аналогичное исключение.
_OR_LIGHT_ORDER: list[str] = [
    "inclusionai/ling-3.0-flash:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "openai/gpt-oss-20b:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "nvidia/nemotron-3.5-lightning:free",
    "openrouter/free",
]

# ── "Тяжёлые" запросы (код, многошаговые рассуждения, объёмный анализ) без
# нужды в интернете/медиа — тоже сначала к OpenRouter: среди бесплатных
# моделей там есть по-настоящему сильные кандидаты (120B/550B), не уступающие
# по мощи флагману Gemini, но не занимающие его 20 запросов/сутки.
#
# nemotron-3-super-120b-a12b — единственная замеченная порча текста за всё
# калибровочное тестирование: 1 инцидент из 4 тяжёлых запросов (25 июля 2026 —
# китайский иероглиф вместо "хвост" в ответе про TCP/IP). Не понижена — один
# инцидент на четыре успешных попытки не повод убирать флагмана, но стоит
# присматривать за логами `[stream]` этой модели.
#
# Модели, убранные из цепочки целиком (провайдер снял с бесплатного тира или
# слаг подтверждённо не обслуживается — полные причины и даты см. в
# _OR_MODEL_HEALTH выше): qwen/qwen3-next-80b-a3b-instruct, z-ai/glm-4.5-air,
# nousresearch/hermes-3-llama-3.1-405b.
#
# dots-3-note-preview ДОБАВЛЕНА (аудит моделей, 17 августа 2026, по актуальному
# "Top Weekly free" каталогу OpenRouter): 280B total/16B active MoE от нового
# для этого проекта провайдера (Dots Studio), позиционируется под рассуждения/
# код/длинный контекст — подходит по роли для этого списка. Вышла буквально
# несколько дней назад, статус "Preview" в самом названии и совершенно новый,
# ранее не встречавшийся в проекте провайдер — поставлена последней перед
# generic-резервом, ниже уже проверенных калибровкой сильных моделей, пока не
# накопится собственная история наблюдений (аналогично nemotron-3.5-lightning
# в _OR_LIGHT_ORDER выше).
_OR_HEAVY_ORDER: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "dots-studio/dots-3-note-preview:free",
    "openrouter/free",
]

# ── Вложение (изображение) без нужды в свежей информации — у OpenRouter
# достаточно бесплатных vision-моделей, чтобы не трогать Gemini. OpenRouter
# физически принимает только изображения (base64 data URL) — для видео/аудио
# этот список не используется вообще, см. _build_route/_run_route ниже.
_OR_VISION_ORDER: list[str] = [
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free",
]

# ── Цепочки Gemini. GEMINI_HEAVY_CHAIN — от сильной модели к слабой (тот же
# состав/порядок, что был у прежнего единственного quota_fallback_chain), для
# случаев, где ТРЕБУЕТСЯ именно Gemini (YouTube/сайт по ссылке, видео/аудио
# вложение), но живой поиск не нужен.
GEMINI_HEAVY_CHAIN: list[str] = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]
# GEMINI_SEARCH_CHAIN — те же модели, но начиная с тех, у кого реально ЕСТЬ
# квота на search grounding: gemini-2.5-flash/-lite первыми (единственный
# бакет с подтверждённой квотой — см. примечание про бакеты в начале
# GEMINI_MODELS выше), вся линейка 3.x — резервом (не смогут вызвать
# google_search, но всё ещё могут ответить по своим знаниям и через url_context).
GEMINI_SEARCH_CHAIN: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]
# Совпадает по составу с прежним quota_fallback_chain — используется как дефолт,
# если ask_gemini вызвана без явной цепочки (например, напрямую из теста).
GEMINI_DEFAULT_CHAIN: list[str] = GEMINI_HEAVY_CHAIN
# Только "полноценные" (не no_system/Gemma) модели умеют читать сайты по ссылке
# (url_context) и разбирать YouTube-видео по ссылке (file_uri) — то же
# ограничение, что раньше проверялось в _handle_message_core через
# current_gemini_conf.get("no_system").
GEMINI_LINK_CHAIN: list[str] = [m for m in GEMINI_HEAVY_CHAIN if not GEMINI_MODELS.get(m, {}).get("no_system")]
GEMINI_LINK_SEARCH_CHAIN: list[str] = [m for m in GEMINI_SEARCH_CHAIN if not GEMINI_MODELS.get(m, {}).get("no_system")]


# ── Эвристика "это сложный/тяжёлый запрос?" — без обращения к LLM. Ложные
# срабатывания недороги: худший случай — используется чуть более мощная
# модель, чем реально нужно, а не отказ в ответе.
_HEAVY_QUERY_RE = re.compile(
    r"напиши\s+(код|функци\w*|скрипт|программ\w*|класс\w*|запрос\s+sql|regex|регуляр\w*)"
    r"|сгенерируй\s+код|исправь\s+(код|баг|ошибк\w*)|отрефактор\w*|рефактор\w*|оптимизируй"
    r"|напиши\s+(эссе|статью|доклад|реферат|сочинение|резюме|cv)\b"
    r"|проанализируй\w*|разбер(и|ём)\s+подробно|объясни\s+подробно"
    r"|сравни\s+.{0,40}(и|с)\s+|докажи\b|доказательство"
    r"|реши\s+(задач\w*|уравнени\w*|систем\w*)"
    r"|составь\s+(план|таблиц\w*|список\s+из)"
    r"|многошагов\w*|пошагов\w*\s+(инструкц\w*|план\w*)"
    r"|архитектур\w*|алгоритм\w*",
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
    r"|новост\w*|курс\s+(валют|доллара|евро|рубл\w*)|погод\w*"
    r"|цена\w*|стоимост\w*|сколько\s+стоит"
    r"|кто\s+(сейчас|является|президент|премьер|глава|ceo|мэр)"
    r"|результат\w*\s+(матч\w*|игр\w*|выбор\w*)"
    r"|в\s+эт(ом|ой)\s+(году|месяце|неделе)"
    r"|\b202[6-9]\b",
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
        # Gemini исчерпан целиком (без поиска, но хоть какой-то ответ).
        return _gemini_route(GEMINI_SEARCH_CHAIN) + _or_route(_OR_HEAVY_ORDER if is_heavy else _OR_LIGHT_ORDER)

    # Основной случай: обычный текст без вложений/ссылок/признаков нужды в
    # интернете — целиком к OpenRouter, Gemini — резерв на случай отказа всей
    # цепочки OpenRouter разом.
    if is_heavy:
        return _or_route(_OR_HEAVY_ORDER) + _gemini_route(GEMINI_HEAVY_CHAIN)
    return _or_route(_OR_LIGHT_ORDER) + _gemini_route(GEMINI_SEARCH_CHAIN)
