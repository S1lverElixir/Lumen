"""
lumen_router_config.py — модели и выбор маршрута (Munito/Gemini) для одного сообщения.

Конфигурация плюс функции решения (_build_route, эвристики тяжести и свежести, фильтры квот). Обращений к API отсюда нет, bot.py импортирует имена напрямую.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date
from typing import Any

# Логгер bot, а не __name__: предупреждения ловят те же тесты и фильтры логов.
log = logging.getLogger("bot")


# ── модели ──

# Поля name/badge/desc убраны (июль 2026, /model удалена) — читаются только grounding/url_context/stream/quota_unconfirmed.
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
}
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# ── TTS-модели (аудит техдолга, август 2026) ──
GEMINI_TTS_MODELS: list[str] = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

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
    # Munito AI (эксперимент 05.11.2026): org-префикс part_of_org тут тоже узнаётся детектором.
    "deepseek-ai/deepseek-v4.1-flash-1017",
    "deepseek-ai/deepseek-v4-flash",
    "zai-org/glm-5.3-flash",
]
TEXT_MODEL_ORDER = _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION  # алиас для обратной совместимости
# Аудит 02.08.2026 (логи + каталог): laguna-s-2.1 и ling-3.0-flash, калибровку не проходили (только факт free-квоты).

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
    """Убирает модели с меткой квоты и временным карантином. Все мёртвы — первый из
    списка: маршрут обязан остаться непустым, иначе сообщение вообще не дойдёт ни до
    одной модели (каждое сообщение платит максимум одно лишнее обращение)."""
    alive = [m for m in models if not _is_quota_exhausted(provider, m) and not _is_quarantined(provider, m)]
    if alive:
        return alive
    return [models[0]] if models else []

# ── Временный карантин моделей ──
# Только в памяти: N плохих ответов подряд (пусто или mush-каша) — и роутер
# пропускает модель до конца суток квоты.
_QUARANTINE: dict[tuple[str, str], dict[str, Any]] = {}
# Окно карантина: счётчик и сам бан живут не до полуночи, а столько секунд
# (аудит run-2: три плохих ответа из одного чата выключали модель всем до конца
# квотных суток, потому что успехом проверить её было уже нечем).
QUARANTINE_WINDOW_SEC = max(60, int(os.getenv("QUARANTINE_WINDOW_SEC", "") or 900))

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
        entry = {"bad": 0, "day": today, "since": time.monotonic()}
        _QUARANTINE[key] = entry
    return entry

def _quarantine_window_over(entry: dict[str, Any]) -> bool:
    return time.monotonic() - float(entry.get("since") or 0.0) > QUARANTINE_WINDOW_SEC

def _record_model_outcome(provider: str, model_id: str, *, bad: bool) -> None:
    """Плохой ответ +1 к счётчику в текущем окне (на пороге — карантин с записью
    в лог), успешный — сбрасывает счётчик. За пределами окна счётчик начинает
    с нуля, а один чат вносит не больше threshold-1 из порога: раньше три пустых
    ответа из одного чата выключали модель всем остальным (аудит run-2). Вклад
    лимитим только когда чат известен — служебные вызовы вне контекста сообщения
    не считаем."""
    if not bad:
        if _QUARANTINE.pop((provider, model_id), None) is not None:
            log.debug("[quarantine] %s/%s answered well, counter reset.", provider, model_id)
        return
    import bot
    entry = _quarantine_entry(provider, model_id)
    if _quarantine_window_over(entry):
        entry.update({"bad": 0, "since": time.monotonic()})
        entry.pop("by_chat", None)
    threshold = _quarantine_threshold()
    chat = str(bot._LOG_CHAT.get() or "")
    if chat:
        per_chat = entry.setdefault("by_chat", {})
        if int(per_chat.get(chat) or 0) >= max(1, threshold - 1):
            return
        per_chat[chat] = int(per_chat.get(chat) or 0) + 1
    entry["bad"] = int(entry.get("bad") or 0) + 1
    if entry["bad"] == threshold:
        log.warning(
            "[quarantine] %s/%s skipped for %ds after %d bad responses from %d chats (empty or garbled).",
            provider, model_id, QUARANTINE_WINDOW_SEC, entry["bad"], max(1, len(entry.get("by_chat") or {})),
        )

def _is_quarantined(provider: str, model_id: str) -> bool:
    try:
        import bot
        entry = _QUARANTINE.get((provider, model_id))
        if not entry or entry.get("day") != bot._current_quota_day():
            return False
        if _quarantine_window_over(entry) or int(entry.get("bad") or 0) < _quarantine_threshold():
            return False
        return True
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
            if entry.get("day") == today and int(entry.get("bad") or 0) >= threshold and not _quarantine_window_over(entry)
        )
    except Exception:
        return []

def _gemini_route(models: list[str]) -> list[tuple[str, str]]:
    # Отдельного реестра здоровья здесь нет осознанно:
    # временные отказы (503/unavailable) гасит остывка _mark_model_unavailable на
    # QUOTA_RATE_LIMIT_COOLDOWN_SEC, а снятие модели — только с датированным
    # подтверждением (аудит M8, 10.2026). При инциденте убирать из ORDER-списков с датой.
    return [("gemini", m) for m in _skip_exhausted("gemini", models)]

# ── Munito AI (основной текстовый провайдер с 05.11.2026) ──
# Живой замер eval/munito_probe.py 05.11.2026, глобальный хост, ключ владельца:
# deepseek-v4.1-flash-1017 — первый токен 0.24с и 200 запросов за 30-секундное окно;
# glm-5.3-flash — 1.50с и 10 запросов; deepseek-v4-flash — 0.19с и 10 запросов;
# qwen3.8-27b — без стрима вернул ПУСТОЙ content (400 reasoning-токенов), поэтому
# головой не берём. Данные: ноль-хранение по умолчанию, обучение на промптах запрещено.
# Картинки проверены 05.11.2026 живым вызовом: обе головы называют цвета
# синтетического PNG и отдают реальные tool_calls, поэтому vision тоже здесь.
_MUNITO_ORDER: list[str] = [
    "deepseek-ai/deepseek-v4.1-flash-1017",
    "zai-org/glm-5.3-flash",
    "deepseek-ai/deepseek-v4-flash",
]

# Выключатель, а не включатель: Munito активен, пока задан ключ и переменная не
# выставлена в 0/false/off. Удаление секрета из Space само возвращает маршрут на
# цепочки Gemini, правки кода для этого не нужны.
_MUNITO_DISABLED = (os.getenv("MUNITO_ENABLED", "") or "").strip().lower() in ("0", "false", "off")


def _munito_active() -> bool:
    """Ключ плюс невыключенный флаг: без ключа путь в Munito бессмысленен и маршруту не виден."""
    try:
        import bot
        return bool(not _MUNITO_DISABLED and bot.MUNITO_API_KEY)
    except Exception:
        return False


def _munito_route() -> list[tuple[str, str]]:
    """Пусто без ключа или под выключателем: маршрут тогда целиком идёт в Gemini."""
    if not _munito_active():
        return []
    return [("munito", m) for m in _skip_exhausted("munito", _MUNITO_ORDER)]


# ── Усилие модели: автовыбор на каждый запрос (Claude-like effort, 12.11.2026) ──
# Claude не получает бюджет размышления руками: adaptive thinking решает сам, а
# effort (low..max) — поведенческий сигнал о том, сколько работы вложить в ответ,
# включая число и длину вызовов инструментов (офиц. docs "Thinking" и "Effort").
# У наших открытых моделей тот же рычаг — reasoning_effort (Munito) и
# thinking_level/thinking_budget (Gemini), поэтому уровень считается по тексту
# запроса детерминированно, без второго вызова LLM.
# Замер 12.11.2026 живым вызовом (3 задачи x effort low/high/xhigh): у
# glm-5.3-flash «средняя скорость пути» дала 64 при low и верные 60 при high/xhigh,
# то есть уровень переключает правильность, а не только длину; deepseek-1017 верен
# на всех уровнях, у него меняются только reasoning-токены (89 против 242).
# Потолок: на логике про шляпы обе головы при medium+ съели весь бюджет вывода
# размышлением и вернули пустой content (finish_reason=length, ~1200 токенов),
# qwen3.8-27b при xhigh ответил неверно — выше high без замера не поднимаемся.
EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")

# Возможности моделей по параметрам запроса — по живому замеру 12.11.2026, по
# одному вызову на пару (модель, параметр): gemma-4-12b принимает tools (отдала
# корректный tool_calls) и отвечает 400 на reasoning_effort; pplx-decider-v1.2
# отвечает 400 и на tools, и на reasoning_effort; четыре текстовые головы
# принимают minimal..xhigh и отдают tool_calls и в стриме, и без него. Модель вне
# каталога считается поддерживающей прежний набор: молча потерять усилие или tools
# для новой головы хуже, чем один 400, который виден в логе и в резервной ветке.
_MUNITO_CAPS: dict[str, dict[str, bool]] = {
    "deepseek-ai/deepseek-v4.1-flash-1017": {"reasoning_effort": True, "tools": True},
    "deepseek-ai/deepseek-v4-flash": {"reasoning_effort": True, "tools": True},
    "zai-org/glm-5.3-flash": {"reasoning_effort": True, "tools": True},
    "qwen/qwen3.8-27b": {"reasoning_effort": True, "tools": True},
    "google/gemma-4-12b": {"reasoning_effort": False, "tools": True},
    "perplexity-ai/pplx-decider-v1.2": {"reasoning_effort": False, "tools": False},
}


def _known_effort(value: str) -> bool:
    """Значение усилия из шкалы провайдера. Munito отвечает 200 и на «none», и на
    опечатку (замер 12.11.2026), то есть кривое значение молча ничего не меняет —
    его видно только в логе."""
    return value in EFFORT_LEVELS


def _munito_model_supports(model_id: str | None, param: str) -> bool:
    """Есть ли у модели параметр. None (цепочка не выбрана) и модель вне каталога =
    не проверяем: тихо выключить усилие для новой модели хуже, чем один 400 на
    неизвестный параметр, а тест на полноте записи не даёт поставить её молча."""
    if not model_id:
        return True
    return bool(_MUNITO_CAPS.get(model_id, {}).get(param, True))


def _effort_auto_on() -> bool:
    """EFFORT_AUTO=0/off возвращает прежний плоский режим: уровень берётся только
    из MUNITO_REASONING_EFFORT, как было до 12.11.2026."""
    return (os.getenv("EFFORT_AUTO", "") or "").strip().lower() not in ("0", "false", "off")


# Явная просьба подумать как следует или разбор предыдущей ошибки: Claude в этом
# случае поднимает усилие, а не отвечает быстрее.
_EFFORT_DEEPER_RE = re.compile(
    r"подумай|посчитай\s+(внимательн|заново|ещё)|не торопи|как следует|перепровер|проверь\s+расчёт"
    r"|внимательн\w*|разбер(и|ём)\s+(детальн|по\s+косточкам)|подробн\w*\s+и\s+объясн"
    r"|think\s+(carefully|hard|twice)|take\s+your\s+time|double[\s-]check|reconsider"
    # Человек ловит модель на ошибке: прошлый ответ был слабым, повторять его нельзя.
    r"|ты\s+(не\s+прав|ошиб|снова|опять)|снова\s+ошиб|неверн\w*,?\s+(это|тут)|я\s+(же\s+)?говорил",
    re.IGNORECASE,
)
# Просьба коротко — снижаем, но не ниже low: ниже low не опускаемся сознательно
# (minimal провайдер принимает, но на нём ничего не меряло; неверная арифметика
# показана на low — замер 12.11.2026), а «коротко» не значит «неверно».
_EFFORT_SHORTER_RE = re.compile(
    r"коротко|кратко|в\s+один\s+(строку|слова?)|одним\s+словом|без\s+(пояснений|воды|деталей)"
    r"|быстро|парой\s+слов|максимально\s+коротк|в одном предложении"
    r"|\bbriefly\b|\bshort\s+answer\b|one\s+word|\bconcise\b|no\s+explanation|tl;?dr"
    # Те же просьбы на остальных языках бота: из-за одного RU/EN-списка сетка
    # 12.11.2026 оставляла на medium «was ist ein JWT kurz» и «kiut a költő?
    # röviden», хотя человек просил две строки.
    r"|\bkurz\b|\bkurze\b|\bkurzen\b|r[öo]viden|lyhysti|lyhemmin"
    # Только те формы, которые в своём языке значат «коротко», а не «быстро,
    # скоро, маленький». «rapid», «vite», «presto», «pronto» и «corto» без
    # ударения в живых фразах — обычные слова, и они снижали настоящие вопросы:
    # «how does a rapid transit system work» уходило на low (ревью 12.11.2026).
    r"|promptement|r[áa]pid[oa]\b|m[áa]s\s+(?:corto|r[áa]pido)|brevemente"
    r"|\bkort\b|kr[óo]tko|k[ıi]sa\b|qisqa\b"
    r"|ng[ảa]n\b|ng[ảa]n ?g[ọo]n|singkat|ringkas|pendek|maikli"
    "|สั้น|สั้นๆ|\u0628\u062e\u0627\u0635\u0631|\u062e\u0644\u0627\u0635\u0647"
    "|\u0645\u062e\u062a\u0635\u0631|\u0938\u0902\u0915\u094d\u0937\u0947\u092a",
    re.IGNORECASE,
)
# Дальний горизонт: чертёж, доказательство, многошаговый разбор — то, где Claude
# думает дольше всего. Снят с _HEAVY_QUERY_RE, но уже: heavy — это «код/статья»,
# xhigh — «архитектура/доказательство/план на несколько недель». Слово «код» и
# «рефакторинг» сюда НЕ входят: это heavy-задача, а не дальний горизонт (сетка
# effort_grid ловила перегруз на «объясни как ребёнку, что такое рефакторинг»).
_EFFORT_LONGHORIZON_RE = re.compile(
    r"архитектур|докажи|доказательств|спроектир\w*|аппроксим\w*|сходимость"
    # «план на четыре недели», «график на месяц», «план тренировок на 12 недель».
    # «план на четыре недели», «график на месяц», «программа на 12 недель».
    # Границы слова обязательны: без них «Планирую отпуск на месяц» и «Планета
    # Венера, месяц за месяцем» уезжали на xhigh (блоклер ревью 12.11.2026).
    # Окно — 25 знаков внутри одной фразы: склеивать несвязные предложения
    # «план …» и «…неделя» права нет.
    r"|\b(?:план|плана|плану|плане|график|программ\w*|список\s+задач"
    r"|инструкц\w*|разбор|разбора|сравнен\w*)\b"
    r"[^\n.!?]{0,25}?\b(?:недел\w*|мес[ея]ц\w*|этап\w*|\d+\s+(?:этап|шаг|ден\w*))\b"

    r"|по\s*шагам|пошагов\w*|многошагов\w*|шаг\s+за\s+шагом|сравни\s+(все|нескольк|эти)"
    r"|продумай\s+все|trade-?offs?|end[\s-]to[\s-]end|architecture|proof\b|\bprove\b"
    r"|step[\s-]by[\s-]step|\btwo[\s-]week\b|\broadmap\b",
    re.IGNORECASE,
)

# Сколько ПОИСКОВ разрешаем за один ход на каждом уровне (Claude: effort решает,
# сколько работы модель делает, а web_search ограничен max_uses). Каждый поиск —
# это отдельный планирующий вызов плюс внешняя сеть, поэтому потолок жёсткий, а
# шаги цикла считаются отдельно (lumen_agent._MAX_STEPS).
_AGENT_SEARCHES_BY_EFFORT: dict[str, int] = {
    "minimal": 0, "low": 0, "medium": 1, "high": 2, "xhigh": 2,
}


# Вопрос по форме, а не только по маркеру свежести: «что за город Брест» —
# стабильный факт, но на нём цена догадки выше цены лишних reasoning-токенов
# (замер 12.11.2026: при low glm-5.3-flash дал 64 вместо верных 60; на medium эту
# задачу не проверяли, но на логической задаче medium+ съедал весь бюджет вывода
# размышлением и возвращал пустой content).
# Сетка eval/effort_grid.py 12.11.2026: одних знаков вопроса и длины мало —
# «почему небо голубое» и «deux fois deux font combien» приходили на low, а
# список слов только RU/EN для бота на 25 языках (lumen_lang.SUPPORTED_LANGS)
# непригоден в принципе. Сигнал теперь такой: знак вопроса любой письменности,
# цифра, длиннее четырёх слов ИЛИ вопросительное слово.
_QUESTION_ANY_RE = re.compile(
    # ASCII «?», перевёрнутый испанский U+00BF, арабский и персидский U+061F,
    # полноширинный U+FF1F, греческий вопросительный знак U+037E. Данда U+0965 и
    # бирманский U+104A из прежнего класса убраны: это концы фраз, а не вопрос.
    # Персидский знак «؛» (U+061B) не добавлен: в арабском это точка с запятой.
    r"[?\u00bf\u061f\uff1f\u037e]"
)
_ANY_DIGIT_RE = re.compile(r"\d")

# «huh????» — один вопрос с четырьмя знаками, а не четыре вопроса в столбик:
# на дальний горизонт ведёт только достаточное число слов (проверено 12.11.2026).
_MULTI_QUESTION_MARKS = 4
_MULTI_QUESTION_WORDS = 5

# Список шире SUPPORTED_LANGS: люди пишут боту и на языках, которых у него в
# переводах нет (польский, чешский, шведский), а промах вверх стоит одну ступень
# усилия, промах вниз — неверный факт. Часть орфографии сверена со строк самого
# бота (lumen_lang._START_EXAMPLES и его переводы): так сняты тайские ใคร иอะไร;
# «ทำไม» в строках бота не встречается, и её в списке нет.
# СОЗНАТЕЛЬНО ИСКЛЮЧЕНЫ слова, которые в короткой реплике чаще значат не вопрос:
# «как»/«how»/«wie»/«hur»/«comment»/«come»/«nasıl» — это приветствие («как дела»,
# «how are you»), и настоящее вопросительное слово в длинной фразе и так
# попадает на medium по длине; шведские «var» (это ещё и прошедшее время «был»)
# и «när» с норвежским «når» (союз «когда») — они давали ложный вопрос на
# «var i = arr» и на коротких фразах без вопроса.
# Многословные «how much», «how many», «por que» оставлены: они однозначны.
_ASK_WORDS = (
    # русский, украинский, белорусский
    "кто", "кем", "кём", "чем", "что", "где", "куда", "откуда", "когда",
    "почему", "зачем", "какой", "какая", "какое", "какие", "сколько", "который",
    "которая", "которое", "которые",
    "хто", "що", "де", "куди", "звідки", "коли", "чому", "навіщо", "який",
    "яка", "яке", "які", "скільки", "ким", "чим", "чи",
    # казахский, узбекский: кириллическое «не» — отрицание, поэтому его здесь нет
    "кім", "қайда", "қашан", "неге", "қанша", "қандай", "неше",
    "nima", "qayerda", "qayerga", "qachon", "nega", "necha", "nechta", "qaysi", "qanday",
    # английский, немецкий, нидерландский
    "who", "whom", "whose", "what", "where", "when", "why", "which",
    "how much", "how many", "how old", "how long", "how far", "how come",
    "wer", "was", "wo", "woher", "wohin", "wann", "warum", "weshalb", "wieso",
    "welche", "welcher", "welches", "wieviel", "wie viel",
    "wat", "waar", "wanneer", "waarom", "welke", "hoeveel", "hoe veel",
    # французский, испанский, итальянский, португальский
    "qui", "quoi", "quand", "pourquoi", "combien", "quel", "quelle", "quels",
    "quelles", "où",
    "quién", "quiénes", "qué", "dónde", "cuándo", "cuánto", "cuánta", "cuántos",
    "cuántas", "cuál", "cuáles",
    "por qué",
    "chi", "dove", "quando", "perché", "perche", "quanto", "quanti", "quante",
    "quale", "quali",
    "quem", "onde", "quanto", "quantos", "quantas", "qual", "quais", "por que",
    # польский, турецкий, шведский, датский, норвежский
    "kto", "czy", "gdzie", "kiedy", "dlaczego", "jaki", "jaka", "jakie", "ile",
    "kim", "czym",
    "kim", "nerede", "nereye", "ne zaman", "neden", "niçin", "kaç", "hangi",
    "vem", "vad", "varför", "vilken", "vilket", "vilka",
    "hvem", "hvad", "hva", "hvor", "hvorfor", "hvilken", "hvilket",
    # индонезийский, малайский, филиппинский
    "siapa", "apa", "di mana", "kapan", "bila", "kenapa", "mengapa", "berapa",
    "sino", "ano", "saan", "kailan", "bakit", "gaano", "alin", "ilang",
    # вьетнамский, зулусский
    "ai", "gì", "đâu", "khi nào", "bao giờ", "tại sao", "vì sao", "bao nhiêu",
    "mấy", "nào", "không",
    "ubani", "yini", "phi", "nini", "kungani", "angaki", "mingaki", "kuphi",
    # хинди, бенгали
    "कौन", "क्या", "कहाँ", "कहां", "कब", "क्यों", "कितना", "कितने", "किस",
    "কে", "কী", "কোথায়", "কখন", "কেন", "কত",
    # арабский, персидский, урду: двусмысленные «кто/что» (من، ما، کیا، چه)
    # ушли в _ASK_LEADING_RE, здесь только однозначные
    "أين", "متى", "لماذا", "كم",
    "کی", "کجا", "چرا", "چند", "کدام",
    "کون", "کہاں", "کتنی", "کتنا", "کس", "کیوں",
    # тайский пишет слова подряд, границы там не работают — см. _ASK_TIGHT_RE
)

# Слово в деванагари или бенгали обязан граничить не с «не-буквой», а с чем-то
# ВНЕ самой письменности: гласные и носовые знаки (U+093E-U+094F, U+0982-U+09FC)
# буквами не считаются, поэтому "\b" рвётся внутри слова — "\bकिस\b" ловило
# «किसी» («чей-то»), а подстрока «কে» ловила «থেকে» («из, от») (блоклер ревью
# 12.11.2026: 13 из 24 коротких бытовых фраз на хинди и бенгали теряли low).
# Те же look-around заодно принимают формы на гласовом знаке (कहाँ, কে), которым
# граница слова противопоказана. Тайский пишется без пробелов вообще: там только
# подстрока (_ASK_WORDS_THAI).
_INDIC_SCRIPT = "\u0900-\u097F\u0980-\u09FF"


def _is_indic(word: str) -> bool:
    """Первый символ — буква деванагари или бенгали: значит, слово из той же
    письменности и границу надо проверять по диапазону, а не по \b."""
    return bool(word) and re.match("[" + _INDIC_SCRIPT + "]", word) is not None


def _alternation(words) -> str:
    """Альтернатива регулярки из слов: каждое экранировано (иначе точка или
    вопросительный знак в слом молча испортят весь класс), длинные вперёд —
    короткая форма иначе затенит длинную («сколько» против «сколь»)."""
    return "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))


# Тайский вопросительный ряд: кто, что, какой/где.
_ASK_WORDS_NO_SPACE = (
    "ใคร", "อะไร", "ไหน"
)

_ASK_WORDS_SPACED = tuple(w for w in _ASK_WORDS if not _is_indic(w))
_ASK_WORDS_INDIC = tuple(w for w in _ASK_WORDS if _is_indic(w))

# Границы слова работают везде, где слова разделяются пробелом, а буквы
# считаются словами: без них «что» ловилось бы внутри «почтово».
_ASK_WORDS_RE = re.compile(
    r"\b(?:%s)\b" % _alternation(_ASK_WORDS_SPACED),
    re.IGNORECASE,
)

_ASK_INDIC_RE = re.compile(
    f"(?<![{_INDIC_SCRIPT}])(?:{_alternation(_ASK_WORDS_INDIC)})(?![{_INDIC_SCRIPT}])",
    re.IGNORECASE,
)

# Тайский вопросительный ряд: кто, что, какой/где. Подстрока — единственное,
# что там работает, и она обязана оставаться короткой и однозначной.
_ASK_TIGHT_RE = re.compile(_alternation(_ASK_WORDS_NO_SPACE), re.IGNORECASE)

# Двусмысленные «кто/что» там, где то же слово — союз, предлог или междометие:
# рус. просторечное «чё», араб. «من» и «ما», урду «کیا», перс. «چه» и ударные
# испанские «qué/quién» (только с ударением: «que» без него — союз). Ловим их только в начале реплики и
# только в начале строки (MULTILINE: «ok\nque pasa» — одно сообщение).
# Голые «que», «che», «co», «ne», «ou» из правила убраны целиком: они и в начале
# фразы чаще служебные слова, чем вопрос («que bom!», «ne bouge pas»,
# «co-founder thing»), а вопрос без ударения и без знака в испанском редкость
# дороже, чем лишний шаг усилия (ревью 12.11.2026).
_ASK_LEADING_RE = re.compile(
    r"^(?:qué|qui[ée]n(?:es)?|чё|чо|\u0645\u0646|\u0645\u0627"
    r"|\u06a9\u06cc\u0627|\u0686\u0647)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Болтовня короче этих границ и без вопросительных сигналов — единственный
# случай, когда берём low. Считаем и слова, и длину: в тайском и в деванагари
# пробелов мало, и по одному «слову» уезжал бы целый вопрос.
_CHAT_MAX_WORDS = 4
_CHAT_MAX_CHARS = 40

def pick_reasoning_effort(
    text: str, *, is_heavy: bool = False, needs_freshness: bool = False,
    corrections: int = 0, history_len: int = 0,
) -> str:
    """Уровень усилия для одного запроса. Дешевле ошибиться вверх, чем вниз:
    лишние reasoning-токены стоят секунды, неверный ответ — доверие.
    minimal не используем намеренно: провайдер его принимает, но на нём ничего
    не меряло, а выигрыш — доли секунды; неверная арифметика показана на low."""
    body = text or ""
    marks = len(_QUESTION_ANY_RE.findall(body))
    if (_EFFORT_LONGHORIZON_RE.search(body) or len(body) > 1200
            or (marks >= _MULTI_QUESTION_MARKS
                and len(body.split()) >= _MULTI_QUESTION_WORDS)):
        level = "xhigh"
    elif is_heavy:
        level = "high"
    elif needs_freshness:
        # Свежий факт требует аккуратности с источниками, а не глубокого reasoning.
        level = "medium"
    else:
        stripped = body.strip()
        words = stripped.split()
        is_small_talk = (
            len(words) <= _CHAT_MAX_WORDS
            and len(stripped) <= _CHAT_MAX_CHARS
            and not _QUESTION_ANY_RE.search(stripped)
            and not _ASK_WORDS_RE.search(stripped)
            and not _ASK_INDIC_RE.search(stripped)
            and not _ASK_TIGHT_RE.search(stripped)
            and not _ASK_LEADING_RE.search(stripped)
            and not _ANY_DIGIT_RE.search(stripped)
        )
        if is_small_talk:
            # Болтовня и короткие реплики: здесь печать важнее глубины.
            level = "low"
        else:
            level = "medium"
    if _EFFORT_DEEPER_RE.search(body):
        level = _bump_effort(level, 1)
    if corrections > 0:
        # Человек уже ловил модель на ошибке в этом разговоре.
        level = _bump_effort(level, 1)
    if _EFFORT_SHORTER_RE.search(body):
        level = _bump_effort(level, -2 if level == "xhigh" else -1)
        if level == "minimal":
            level = "low"
    if history_len > 40 and level == "low":
        # Длинный разговор: цена неверного «быстрого» ответа выше, чем пауза.
        level = "medium"
    return level


def recent_corrections(history: list[Any], limit: int = 3) -> int:
    """Сколько из последних реплик человека ловили модель на ошибке («ты не прав»,
    «неверно»): по ним усилие поднимается на ступень, как у Claude, где человек
    прямо выбирает уровень глубже после промаха. Считаем по роли user и только по
    коротким репликам — в развёрнутом тексте та же фраза может быть цитатой."""
    found = 0
    for entry in reversed(history or []):
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        text = entry.get("content")
        if not isinstance(text, str):
            continue
        if len(text) <= 200 and _EFFORT_DEEPER_RE.search(text):
            found += 1
        if len(text) <= 200:
            limit -= 1
            if limit <= 0:
                break
    return found


def _bump_effort(level: str, delta: int) -> str:
    """Сдвиг по шкале EFFORT_LEVELS с упором в края. xhigh — потолок: выше него
    открытые модели на замере 12.11.2026 возвращали пустой content."""
    try:
        idx = EFFORT_LEVELS.index(level)
    except ValueError:
        return "low"
    return EFFORT_LEVELS[max(0, min(len(EFFORT_LEVELS) - 1, idx + delta))]


def agent_max_searches(effort: str | None) -> int:
    """Сколько поисков за ход разрешено при этом усилии. None = автовыбор выключен
    (EFFORT_AUTO=0), и цикл тогда выключается тоже: без усилия нет и обоснованного
    потолка поиска, а гибрид «авто выключено, цикл работает» не замерян.
    AGENT_MAX_SEARCHES перекрывает, 0 глушит цикл совсем."""
    if not effort:
        return 0
    raw = (os.getenv("AGENT_MAX_SEARCHES", "") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _AGENT_SEARCHES_BY_EFFORT.get(effort, 0)


def gemini_thinking_for(model_id: str, effort: str | None) -> tuple[str, Any]:
    """Пары (параметр, значение) для thinking_config резерва: у линейки 3.x это
    thinking_level (офиц. docs Gemini: minimal/low/medium/high по модели), у 2.5 —
    thinking_budget в токенах. ('', None) = поле не трогаем, дефолт модели.
    effort=None — автовыбор выключен (EFFORT_AUTO=0), прежняя эвристика остаётся
    на вызывающей стороне, здесь ничего не назначаем."""
    if not effort:
        return "", None
    if model_id.startswith("gemini-3"):
        if effort in ("minimal", "low", "medium", "high"):
            return "thinking_level", effort
        return "thinking_level", "high"
    if model_id.startswith("gemini-2.5"):
        # Замера по budget у нас нет; калибровка 18.08.2026 оставила только две
        # подтверждённые точки: выключенное размышление на лёгких и дефолт на
        # остальных, поэтому high/xhigh здесь не поднимаем руками.
        if effort in ("minimal", "low"):
            return "thinking_budget", 0
        return "", None
    return "", None


def _munito_payload_extras(effort: str | None = None, model_id: str | None = None) -> dict[str, Any]:
    """reasoning_effort для запроса Munito.
    Порядок: пустая MUNITO_REASONING_EFFORT = поле не отправляем вовсе (замер
    05.11.2026: у glm-5.3-flash reasoning падает с ~60 токенов до 0-20, первый
    токен 0.13-0.26s вместо 0.5-1.0s; qwen3.8-27b без поля съедал весь лимит
    вывода размышлением и отдавал пустой content); непустая = пин, он важнее
    автовыбора, так владелец глушит эксперимент без правки кода; без переменной —
    автовыбор по запросу, а без него прежний дефолт low. Без аргумента effort
    служебные вызовы (selftest, описания /draw, сводки) остаются на дефолте."""
    raw = os.getenv("MUNITO_REASONING_EFFORT")
    if raw is not None and not raw.strip():
        return {}
    chosen = raw.strip() if raw is not None else (effort or "low").strip()
    if not chosen:
        return {}
    if not _known_effort(chosen):
        # Munito принимает любое значение с HTTP 200 (замер 12.11.2026: «none» и
        # опечатка проходят), то есть кривой пин выглядел бы как работающая галочка.
        log.warning("[effort] Unknown reasoning_effort %r is not in the provider scale,"
                    " the field is sent as is — check MUNITO_REASONING_EFFORT", chosen)
    if not _munito_model_supports(model_id, "reasoning_effort"):
        return {}
    return {"reasoning_effort": chosen}


# ── Цепочки Gemini: где нужен именно Gemini (ссылки, YouTube, видео/аудио) и где
# он резерв после Munito (весь остальной текст и картинки).
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
# Сайты по ссылке и YouTube умеют те же цепочки; имена оставлены раздельными,
# потому что их читают разные вызывающие места (фильтр по no_system умер вместе с Gemma).
GEMINI_LINK_CHAIN: list[str] = list(GEMINI_HEAVY_CHAIN)
GEMINI_LINK_SEARCH_CHAIN: list[str] = list(GEMINI_SEARCH_CHAIN)


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
    # Астрономия, зависящая от даты (eval 22.10.2026: «рассвет в Москве» ушёл
    # по памяти — Q6 провал, маршрут строился как обычный текст без поиска).
    r"|рассвет|закат|восход|лунн\w*|фаз\w*\s+луны"
    r"|цена\w*|стоимост\w*|сколько\s+стоит"
    # Советы по покупке (прод 17.09.2026): цены и наличие протухают, ответ из памяти врёт.
    r"|лучше\s+(всего\s+)?(брать|взять|купить|выбрать)"
    r"|(брать|взять|купить|выбрать)\s+лучше"
    r"|кто\s+(сейчас|является|президент|премьер|глава|ceo|мэр)"
    r"|результат\w*\s+(матч\w*|игр\w*|выбор\w*)"
    # Ставка и курс меняются по расписанию: вопрос о них свежий и без слова «сейчас»
    # (замер 11.11.2026: «какая ключевая ставка в России» — Claude ищет, бот — нет).
    r"|ключев\w*\s+ставк|ставк\w*\s+(?:цб|банка\w*\s+россии)"
    # «вышёл ли финал / что нового в сезоне» — вопрос о текущем состоянии дела: поиск
    # не запускался вовсе, хотя на таком вопросе он нужен (замер 11.11.2026).
    r"|что\s+нов\w*|выш\w*\s+ли|когда\s+выйдет|когда\s+вышли|премьер\w*|последн\w*\s+(эпизод|серии|серий|выпуск|обновление|тур)"
    r"|new\s+episode|premiere[ds]?\b|\b(?:come\s+out|out\s+now|aired|is\s+out)\b|\brelease\s+date\b"
    r"|в\s+эт(ом|ой)\s+(году|месяце|неделе)"
    rf"|\b(?:{_FRESHNESS_YEAR_ALTS})\b"
    # EN-набор: детект был только русским при 25 языках (внешний аудит). Границы слов обязательны: now ловил know/snow.
    r"|\bnow\b|\btoday\b|\btomorrow\b|\byesterday\b|current\w*|latest|recent\w*|\bbreaking\b"
    r"|\bnews\b|weather|forecast|price\w*|\bcost\w*|how\s+much|exchange|\bscore\w*|schedule"
    r"|who\s+is\s+(now|currently|the\s+(president|ceo|prime\s+minister|mayor))"
    r"|best\s+(to\s+buy|buy)|should\s+i\s+buy|worth\s+buying"
    # Мультиязычный набор: те же сигналы для основных языков бота, иначе
    # не-RU/EN свежие вопросы уходили в обычный текст без даты (аудит 28.10.2026).
    # Без tiempo/tempo/weer/son/panahon/morgen: они значат ещё «время», «снова»,
    # «конец», «утро» — ложные срабатывания на обычной болтовне дороже пропуска.
    # actual убран: в испанском «текущий», но в английском это обычное «фактический».
    r"|\b(hoy|ahora|reciente|noticias|precio|clima|mañana|ayer)\b"
    r"|\b(heute|jetzt|aktuell|neueste|nachrichten|preis|wetter|vorhersage|gestern)\b"
    r"|\b(aujourd'hui|maintenant|actuel|récent|actualités|prix|météo|prévisions|demain|hier)\b"
    r"|\b(oggi|adesso|attuale|recente|notizie|prezzo|meteo|previsioni|domani|ieri)\b"
    r"|\b(hoje|agora|atual|recente|notícias|preço|previsão|amanhã|ontem)\b"
    # morgen с оговоркой: «Guten Morgen» — приветствие, а не завтра (мультиязычный бот).
    r"|\b(vandaag|nu|actueel|recent|nieuws|prijs|voorspelling|(?<!guten )morgen|gisteren)\b"
    r"|\b(dziś|teraz|aktualn\w*|ostatni\w*|wiadomości|cena|pogoda|prognoza|jutro|wczoraj)\b"
    r"|\b(bugün|şimdi|güncel|haberler|fiyat|hava|tahmini|yarın|dün)\b"
    r"|\b(اليوم|الآن|الحالي|الأخير|أخبار|سعر|الطقس|توقعات|غدا|أمس)\b"
    # Границы по диапазону письменности, а не \b: матры не входят в \w, и
    # \bहाल\b ловил «हालांकि», \bकल\b — «कल्पना» (аудит 28.10.2026).
    r"|(?<![\u0900-\u097F])(?:आज|अभी|वर्तमान|हाल|समाचार|खबर|कीमत|मौसम|पूर्वानुमान|कल)(?![\u0900-\u097F])"
    r"|\b(hari ini|sekarang|terbaru|berita|harga|cuaca|prakiraan|besok|kemarin)\b"
    r"|\b(hari ini|sekarang|terkini|berita|harga|cuaca|ramalan|esok|semalam)\b"
    r"|\b(hôm nay|bây giờ|hiện tại|mới nhất|tin tức|giá|thời tiết|dự báo|ngày mai|hôm qua)\b"
    r"|(วันนี้|ตอนนี้|ปัจจุบัน|ล่าสุด|ข่าว|ราคา|อากาศ|พยากรณ์|พรุ่งนี้|เมื่อวาน)"
    r"|\b(امروز|اکنون|فعلی|اخیر|اخبار|قیمت|هوا|پیش‌بینی|فردا|دیروز)\b"
    r"|\b(آج|ابھی|موجودہ|حالیہ|خبریں|قیمت|موسم|پیش گوئی|کل)\b"
    r"|\b(bugun|hozir|joriy|so'nggi|yangiliklar|narx|ob-havo|prognoz|ertaga|kecha)\b"
    r"|(?<![\u0980-\u09FF])(?:আজ|এখন|বর্তমান|সাম্প্রতিক|সংবাদ|খবর|দাম|আবহাওয়া|পূর্বাভাস|আগামীকাল|গতকাল)(?![\u0980-\u09FF])"
    r"|\b(ngayon|kasalukuyan|pinakabago|balita|presyo|bukas|kahapon)\b"
    r"|\b(namuhla|manje|okwamanje|kwakamuva|izindaba|intengo|isimo sezulu|isibikezelo|kusasa|izolo)\b"
    r"|\b(сёння|зараз|актуальн\w*|апошн\w*|навіны|кошт|цана|надвор'е|прагноз|заўтра|учора)\b"
    r"|\b(сьогодні|зараз|актуальн\w*|останн\w*|новини|ціна|погода|прогноз|завтра|вчора)\b"
    r"|\b(бүгін|қазір|ағымдағы|соңғы|жаңалықтар|баға|ауа райы|болжам|ертең|кеше)\b"
    r"|\bзавтра\b|\bвчера\b",
    re.IGNORECASE,
)

# ── Структурные сигналы свежести (требование владельца 11.11.2026: списочный
# перечень форм не покрывает язык — в проде «когда вышла Fable 5.2» ответили без
# поиска; Claude решает по классу вопроса, а не по ключевому слову).
# Ложное срабатывание здесь так же дёшево, как у _FRESHNESS_QUERY_RE: искать или нет,
# решает модель.

# Морфология релизов: корень вместо перечисления рода и числа (вышла/вышло/вышли/вышедшая).
_RELEASE_LEX_RE = re.compile(
    r"\b(?:выш(?:ел|ла|ло|ли|ед|увш)\w*|выйд\w*|выпуст\w*|выпуск\w*|релиз\w*|анонс\w*|презент\w*"
    r"|появ\w*|обнов\w*|добав(?:и|л)\w*|стартов\w*|откры(?:л|т)\w*|поступ(?:ил|а|я)\w*"
    r"|в\s+(?:прокат|продаж)\w*)",
    re.IGNORECASE,
)
# Слово про релиз — сигнал только вместе с вопросом: вопросительное слово, частица «ли»
# или знак вопроса. «как» в список не входит: «как добавить муку» — рецепт, не новость.
_QUESTION_MARK_RE = re.compile(
    r"\b(?:когда|уже|ли|сколько|какой|какая|какое|какие|каков|правда|существует)\b"
    r"|есть\s+ли|что\s+нов\w*|[?？]",
    re.IGNORECASE,
)
_HAS_DIGIT_RE = re.compile(r"\d")

# Имя собственное: латиница в любом регистре (прод: «claude Fable 5.2» писали строчными)
# или кириллица с большой буквы. Вопросительные, служебные и глаголы-приказы в начале фразы
# отфильтрованы: иначе «Сколько будет 45.5» превращало арифметику в новость.
_NAME_TOK = (
    r"(?!(?:is|are|was|were|the|this|that|with|from|just|about|will|would|could|should|shall"
    r"|how|what|when|why|who|which|does|did|have|has|had|not|now|out|it|on|in|at|to|of)\b)[A-Za-z]{2,}"
    r"|(?!(?:Что|Как|Кто|Где|Когда|Почему|Зачем|Какой|Какая|Какое|Какие|Сколько|Это|Этот|Эта|Эти"
    r"|Он|Она|Оно|Они|Мы|Вы|Ты|Будет|Есть|Вот|Тут|Здесь|Напиши|Переведи|Исправь|Реши|Составь"
    r"|Расскажи|Объясни|Покажи|Сделай|Найди|Посчитай|Вычисли|Помнишь)\s)[А-ЯЁ][а-яё]{2,}"
)
# Токены версии: x.y, имя-число, 19 Pro, римские цифры, поколение. Регистр значим:
# под IGNORECASE [А-ЯЁ] ловил строчное «вспомним 2039» как имя (тест 11.11.2026).
_ROMAN_TOKEN = r"\b(?:XVIII|XVII|XVI|XV|XIV|XIII|XII|XI|XXI|IX|VIII|VII|VI|V|IV|III|II)\b"
_NAMED_VERSION_RE = re.compile(
    # «Fable 5.2», «GTA 7», «Silent Hill 2», «iPhone-15»: имя рядом с версией — сущности
    # нет в памяти модели, ответ только из веба, даже без слов-триггеров (замер 11.11.2026).
    # \b вокруг имени обязателен: без него ловились латинские подслова «ко», «то» внутри русских слов.
    rf"\b(?:{_NAME_TOK})\b\s*[-—–]?\s*\d+[.,]\d+"
    rf"|\b(?:{_NAME_TOK})\b\s*(?:[-—–]\s*\d+|\s*\d{{1,4}}\b)"
    rf"|\b(?:{_NAME_TOK})\b[^.?!,;]{{0,12}}{_ROMAN_TOKEN}"
    r"|\d+\s*(?i:pro|max|ultra|mini|plus)\b"
    r"|(?i:поколен\w*|gen)\s*№?\s*\d+"
    r"|\d+\s*(?:[-—–]\s*(?:е|й))?\s*(?i:поколен\w*|gen\b)"
    # Название с однобуквенным хвостом: «Silent Hill f», «Wolverine X» — у такой
    # сущности нет номера версии, но её точно не было в обучающей выборке
    # (замер корпуса 11.11.2026: «расскажи про Silent Hill f» отвечали по памяти).
    rf"|\b(?:{_NAME_TOK})\b\s+\b(?:{_NAME_TOK})\b\s+[A-Za-z]\b"
)
# «модель claude», «фильм „Аватар“», «сезон 3 The Bear»: класс артефакта или события рядом с именем.
_EVENT_CLASS = (
    r"модел\w*|верси\w*|релиз\w*|игр\w*|фильм\w*|сериал\w*|сезон\w*|эпизод\w*|альбом\w*"
    r"|смартфон\w*|процессор\w*|видеокарт\w*|обновлени\w*|прошивк\w*|приложени\w*|фреймворк\w*"
    r"|лиг\w*|турнир\w*|выбор\w*|назначени\w*|отставк\w*|смерт\w*|свадьб\w*|закон\w*|санкци\w*|суд\w*"
)
_CLASS_NEAR_NAME_RE = re.compile(rf"(?i:\b(?:{_EVENT_CLASS}))\s+(?:\d+\s+)?\b(?:{_NAME_TOK})\b")

# Вопросы про собственную модель и версию бота: ответ не из веба, а поиск подталкивал бы
# говорить о модели (требование владельца 11.11.2026). Гасит только новые структурные правила.
_SELF_MODEL_RE = re.compile(
    r"\b(?:ты|тебя|у\s+тебя|у\s+вас)\b[^.?!,;]{0,30}\b(?:верси|модел|движк|нейросет)\w*"
    r"|\b(?:верси|модел|нейросет)\w*[^.?!,;]{0,30}\b(?:ты|тебя|у\s+тебя|у\s+вас|работаешь)\b"
    r"|\b(?:what|which)\b[^.?]{0,30}\b(?:model|version)\b[^.?]{0,30}\b(?:you|your|running|using)\b",
    re.IGNORECASE,
)

# Английский набор той же схемы (мультиязычные наборы основного regex не тронуты).
_EN_RELEASE_RE = re.compile(
    r"\bis\s+out\b|\bout\s+now\b|\b(?:come|came)\s+out\b|\brelease\s+date|\breleas(?:e|ed|es)\b"
    r"|\bis\s+there\s+a\b|\bany\s+news\s+about\b|\bdid\s+(?:it|they|this|that)?\s*drop\b"
    r"|\blatest\s+version\b|\bwhat(?:'s|\s+is)\s+new\b",
    re.IGNORECASE,
)

def _looks_like_release_question(text: str) -> bool:
    """Форма глагола релиза (вышла/выйдет/анонсирована…) плюс любой вопрос или версия рядом."""
    if _SELF_MODEL_RE.search(text):
        return False
    if not _RELEASE_LEX_RE.search(text):
        return False
    return bool(_QUESTION_MARK_RE.search(text)) or bool(_HAS_DIGIT_RE.search(text)) or _is_bare_question(text)

def _looks_like_named_version_query(text: str) -> bool:
    """Неизвестная сущность с номером версии — самый важный сигнал: ловит «Fable 5.2» без слов-триггеров."""
    if _SELF_MODEL_RE.search(text):
        return False
    return bool(_NAMED_VERSION_RE.search(text)) or bool(_CLASS_NEAR_NAME_RE.search(text))

def _looks_like_en_freshness_query(text: str) -> bool:
    """Английские эквиваленты релизного сигнала: is out, released, is there a, what's new."""
    if _SELF_MODEL_RE.search(text):
        return False
    return bool(_EN_RELEASE_RE.search(text))

# Должности, спортивные результаты и счётчики серий протухают независимо от слова
# «сейчас»: на «кто канцлер Германии» и «кто выиграл Лигу наций» Claude ищет, а
# списочный детект пропускал (замер корпуса eval/search_trigger_corpus.py
# 11.11.2026: 7 промахов из 35). Имя собственное рядом обязательно — иначе «кто
# главный в семье» уезжал бы в веб.
_OFFICE_OR_RESULT_RE = re.compile(
    r"\b(?:кто|какой|какая|какие|каков|чем|за\s+кого)\b[^.?]{0,40}"
    r"\b(?:канцлер\w*|президент\w*|премьер\w*|глава\w*|главн\w*|руководител\w*|председател\w*"
    r"|директор\w*|ректор\w*|спикер\w*|мэр\w*|губернатор\w*|сенатор\w*|министр\w*|monarch\w*"
    r"|king|queen|pm|ceo|founder|основател\w*)\b[^.?]{0,40}"
    r"(?:[A-ZА-ЯЁ][\wА-ЯЁ-]{2,}|\b(?:германи|франци|великобритани|сша|америк|росси|украин|белорусси"
    r"|польши|поли|итали|испани|китая|китай|турци|турции|казаш|беларус)\w*)",
    re.IGNORECASE,
)
_SPORT_RESULT_RE = re.compile(
    r"\b(?:выиграл\w*|выигрыва\w*|победил\w*|побежда\w*|чемпион\w*|призёр\w*|обладател\w*"
    r"|финалист\w*|полуфинал\w*)\b[^.?]{0,40}"
    r"(?:[A-ZА-ЯЁ][\wА-ЯЁ-]{2,}|\b(?:лиг\w*|кубк\w*|турнир\w*|олимпиад\w*|чемпионат\w*)\b)",
    re.IGNORECASE,
)
# Счётчик эпизодов и сезонов: меняется с каждым показом, памяти не доверять.
_EPISODE_COUNT_RE = re.compile(
    r"\b(?:сколько|какой|какие|когда)\b[^.?]{0,40}\b(?:эпизод\w*|серий|серия|серии|выпуск\w*|сезон\w*|трейлер\w*)",
    re.IGNORECASE,
)

# ── Явная просьба искать в вебе — отдельно от свежести.
# Голые "найди/ищи" без упоминания поиска не ловим: "найди баг" про код, а не про веб (прод 11.10.2026).
_SEARCH_REQUEST_RE = re.compile(
    r"веб[\s\-]?поиск|поиск\w*\s+в\s+интернете|поищи\s+в\s+интернете"
    r"|погугли\w*|загугли\w*|нагугли\w*|в\s+гугле"
    r"|с\s+помощью\s+поиска|через\s+поиск|воспользуйся\s+поиском"
    r"|search\s+the\s+web|web\s+search|google\s+it|look\s+it\s+up\s+online|browse\s+the\s+(web|internet)",
    re.IGNORECASE,
)

def _looks_like_search_request(text: str) -> bool:
    return bool(text) and bool(_SEARCH_REQUEST_RE.search(text))


def _is_bare_question(text: str) -> bool:
    """Реплика без точки в конце — в мессенджере это вопрос по умолчанию:
    «обновление прошивки на телефоне вышло» без вопросительного знака (замер
    корпуса 11.11.2026). Работает только вместе с релизным или событийным глаголом,
    поэтому на болтовне без такого глагола не срабатывает вовсе."""
    stripped = (text or "").strip()
    return bool(stripped) and stripped[-1] not in ".!…?"


# Состояние дела, которое меняется: «забастовка закончилась», «переговоры продолжаются»,
# «суд идёт». Память модели здесь всегда устаревшая, а слов свежести в фразе нет.
_STATE_CHANGE_RE = re.compile(
    r"\b(?:забастовк\w*|переговор\w*|перемири\w*|оружие|санкци\w*|дело|суд|процесс|"
    r"режим|договор|контракт|сделка|стачка|выборы|референдум)\b[^.?]{0,40}"
    r"\b(?:законч\w*|прекрат\w*|возобнов\w*|продолжа\w*|заверш\w*|идёт|идут|действу\w*|"
    r"подписан\w*|введён\w*|снят\w*|продлён\w*|истёк\w*|истёкш\w*|достигнут\w*)",
    re.IGNORECASE,
)


def _looks_like_office_or_state_query(text: str) -> bool:
    """Должность, результат турнира, счётчик серий и текущее состояние события —
    классы вопросов, которые Claude ищет всегда, независимо от слов «сейчас»."""
    if _SELF_MODEL_RE.search(text):
        return False
    if _OFFICE_OR_RESULT_RE.search(text) or _SPORT_RESULT_RE.search(text) or _EPISODE_COUNT_RE.search(text):
        return True
    return bool(_STATE_CHANGE_RE.search(text)) and (bool(_QUESTION_MARK_RE.search(text)) or _is_bare_question(text))


def _looks_like_freshness_query(text: str) -> bool:
    if not text:
        return False
    return bool(_FRESHNESS_QUERY_RE.search(text)) or _looks_like_release_question(text) \
        or _looks_like_named_version_query(text) or _looks_like_en_freshness_query(text) \
        or _looks_like_office_or_state_query(text)


def _gemini_text_chain(*, needs_freshness: bool) -> list[str]:
    """Какая цепочка Gemini резервом для текста: с поиском — если нужна свежесть,
    иначе общая тяжёлая. Разделения на «лёгкий/тяжёлый» больше нет: с 05.11.2026
    сила запроса влияет только на reasoning внутри Munito, а резерв один."""
    return GEMINI_SEARCH_CHAIN if needs_freshness else GEMINI_HEAVY_CHAIN


def _build_route(
    *, needs_youtube: bool, needs_website: bool, media_mime: str | None,
    is_heavy: bool, needs_freshness: bool,
) -> list[tuple[str, str]]:
    """Приоритетный список кандидатов (provider, model_id), непустой; первый пробуется первым. Внутри провайдера — по возрастанию цены для дефицитной квоты. is_heavy с 05.11.2026 на состав не влияет: различие «лёгкий/тяжёлый» остался только в reasoning самой модели (Munito), поэтому резерв у обоих один — особый тяжёлый резерв заводить здесь."""
    is_video_or_audio_media = bool(media_mime) and not media_mime.startswith("image/")

    if needs_youtube or needs_website:
        # Сайты и YouTube читает только Gemini — эскалировать некуда.
        chain = GEMINI_LINK_SEARCH_CHAIN if needs_freshness else GEMINI_LINK_CHAIN
        return _gemini_route(chain)

    if media_mime:
        if is_video_or_audio_media:
            # Видео/аудио — только Gemini (Munito принимает только картинки).
            chain = GEMINI_SEARCH_CHAIN if needs_freshness else GEMINI_HEAVY_CHAIN
            return _gemini_route(chain)
        if needs_freshness:
            # Картинка+свежесть: нужен живой поиск, а его у Munito нет — только
            # Gemini, как со ссылками.
            return _gemini_route(GEMINI_SEARCH_CHAIN)
        # Картинка без поиска: vision Munito головой (проверен 05.11.2026),
        # тяжёлая цепочка Gemini резервом.
        return _munito_route() + _gemini_route(GEMINI_HEAVY_CHAIN)

    # Весь обычный текст — Munito, Gemini резервом: провайдер отдаёт 503
    # "no free capacity" на бесплатном тарифе, без резерва это тишина для всех.
    return _munito_route() + _gemini_route(_gemini_text_chain(needs_freshness=needs_freshness))
