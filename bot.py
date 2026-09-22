"""
Lumen — телеграм-бот на Gemini/OpenRouter, webhook-режим.
История диалога — 100 сообщений, TikTok через TikWM без водяных знаков,
генерация картинок через Pollinations, озвучка через Gemini TTS.
"""

from __future__ import annotations

# Алиас против дубля модуля при `python bot.py` + `import bot`: иначе второй инстанс,
# пустые state/квоты и вечный 503 (прод-инцидент, сентябрь 2026).
import sys as _sys
_sys.modules.setdefault("bot", _sys.modules[__name__])
del _sys

import asyncio
import atexit
import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import sys
import time
from pathlib import Path
from datetime import date
from typing import Any
import urllib.request as _urllib_request

import aiohttp
import sentry_sdk
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    Message,
)
from google import genai
from google.genai import types  # нужен тестам как bot.types (сборка Content/Part)

# язык системных сообщений бота (см. lumen_lang.py): фиксированные строки,
# которые бот отправляет сам (/start, подсказки, ошибки, статусы). Ответы ИИ
# не трогаем — модель отвечает на языке собеседника.
from lumen_lang import (
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    t as _lang_t,
)

# setMyCommands принимает только двухбуквенные ISO 639-1 коды (прод-инцидент:
# "fil" ронял регистрацию с 400 Bad Request). У филиппинского такого кода нет
# (fil — ISO 639-2), эти пользователи видят команды на английском по умолчанию.
COMMAND_LOCALES = [c for c in SUPPORTED_LANGS if c != DEFAULT_LANG and c != "fil"]

# логирование

LOG_FILE_PATH = Path(os.getenv("BOT_LOG_PATH", "/app/bot.log"))
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
_LOG_QUEUE: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
_LOG_QUEUE_HANDLER = logging.handlers.QueueHandler(_LOG_QUEUE)
_LOG_LISTENER: logging.handlers.QueueListener | None = None

_SECRET_NAMES = (
    "BOT_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY",
    "OPENROUTER_API_KEY", "OPENROUTER_KEY", "GROQ_API_KEY", "ADMIN_SECRET_SEED",
    "_ADMIN_SECRET_SEED", "WEBHOOK_SECRET", "ADMIN_PANEL_KEY",
    "UPSTASH_REDIS_REST_TOKEN", "LUMEN_PROXY_SECRET",
)


def _current_log_secrets() -> tuple[str, ...]:
    values = [value for name in _SECRET_NAMES
              for value in (globals().get(name), os.getenv(name, ""))
              if isinstance(value, str) and value]
    return tuple(sorted(set(values), key=len, reverse=True))


class _SecretLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in _current_log_secrets():
            text = text.replace(secret, "<REDACTED>")
        return text


def _setup_logging() -> logging.Logger:
    # LOG_LEVEL из env: раньше INFO был зашит везде и глушил все debug.
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
    )
    file_handler.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console_handler.setFormatter(fmt)
    file_handler.setFormatter(fmt)

    _LOG_QUEUE_HANDLER.setFormatter(_SecretLogFormatter())

    global _LOG_LISTENER
    # Перед стартом останавливаем старый QueueListener: иначе потоки плодятся на одной очереди и вешают выход после "N passed" (гонка за sentinel, 04.09.2026).
    if _LOG_LISTENER is not None:
        with contextlib.suppress(Exception):
            _LOG_LISTENER.stop()
    _LOG_LISTENER = logging.handlers.QueueListener(_LOG_QUEUE, file_handler, console_handler, respect_handler_level=True)
    _LOG_LISTENER.start()

    root.addHandler(_LOG_QUEUE_HANDLER)
    logging.captureWarnings(True)
    for name in ("httpx", "google_genai", "aiohttp", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Единый логгер "bot": __main__ в проде расходился с lumen_*.py (видно в Sentry).
    return logging.getLogger("bot")

logger = _setup_logging()
log = logger

def _stop_logging() -> None:
    global _LOG_LISTENER
    listener = _LOG_LISTENER
    _LOG_LISTENER = None
    if listener is not None:
        with contextlib.suppress(Exception):
            listener.stop()

atexit.register(_stop_logging)



def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            value = value.replace('\\n', '\n').replace('\\t', '\t')
            os.environ[key] = value
    except Exception as exc:
        log.warning("[setup] Failed to parse .env file %s: %s", path, exc)

for _env_path in (Path('/app/.env'), Path('.env')):
    _load_env_file(_env_path)

# переменные окружения и конфиг

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not BOT_TOKEN:
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    log.warning("[setup] BOT_TOKEN is empty! Please verify BOT_TOKEN/TELEGRAM_BOT_TOKEN environment variables in settings or .env.")
else:
    log.info("[setup] BOT_TOKEN configured successfully (length: %d)", len(BOT_TOKEN))

def _normalize_telegram_base_url(url: str) -> str:
    """Голый хост без схемы чиним в https://: иначе aiohttp валится на каждом вызове невнятной ошибкой."""
    url = url.strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        # Реальный инцидент: TELEGRAM_API_BASE_URL был задан как голый хост воркера
        # (например "tg-proxy.egor-kuzko-04.workers.dev") без схемы. aiohttp такой URL
        # не проглатывает — падает с "Network error" на КАЖДЫЙ вызов (getMe/setWebhook/
        # deleteWebhook/setMyCommands и далее вообще все reply/send_message через aiogram),
        # при этом само сообщение об ошибке невнятное (просто битый URL как текст), не
        # указывает на реальную причину. Раз уж опечатка в схеме случилась один раз —
        # молча чинить её тут дешевле, чем снова терять время на диагностику того же самого.
        log.warning('[setup] Telegram proxy URL given without a scheme (%r) — adding https:// automatically.', url)
        url = "https://" + url
    return url

LUMEN_PROXY_SECRET = os.getenv("LUMEN_PROXY_SECRET", "")
TELEGRAM_API_BASE_URL = _normalize_telegram_base_url(os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org"))
log.info("[setup] Using Telegram API Base URL: %s", TELEGRAM_API_BASE_URL)

# Список резервных прокси по кругу вместо единой точки отказа (аудит, август 2026).
_TELEGRAM_PROXY_FALLBACKS = [
    _normalize_telegram_base_url(u) for u in os.getenv("TELEGRAM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]
_TELEGRAM_PROXY_CANDIDATES: list[str] = [TELEGRAM_API_BASE_URL] + [u for u in _TELEGRAM_PROXY_FALLBACKS if u != TELEGRAM_API_BASE_URL]
_telegram_proxy_idx = 0  # индекс текущего активного прокси в _TELEGRAM_PROXY_CANDIDATES
# Ротация мутирует два глобала сразу — под локом, иначе два concurrent-сбоя
# уводят индекс на два шага и пропускают кандидата (AUD-E-004).
_proxy_rotation_lock = asyncio.Lock()

# База прокси для TikWM: HF IP банится с пустым 403, лечится только прокси (инцидент 11–12.08.2026).
# Пусто — стучимся в два зеркала напрямую, как раньше.
TIKWM_API_BASE_URL = os.getenv("TIKWM_API_BASE_URL", "").strip().rstrip("/")
# Резервные прокси для TikWM (тот же принцип, что и TELEGRAM_API_BASE_URL_FALLBACKS
# выше по логике — см. _TELEGRAM_PROXY_CANDIDATES) — асимметрии быть не должно:
# TikWM зависит от того же самого единственного Deno-прокси, что и Telegram, и
# точка отказа для обоих одна и та же, но раньше только у Telegram был путь
# переключиться на резервный адрес. Пусто по умолчанию — поведение не меняется
# для тех, кто не настраивал (см. _tikwm_proxy_candidates ниже).
_TIKWM_API_BASE_URL_FALLBACKS = [
    u.strip().rstrip("/") for u in os.getenv("TIKWM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]

def _tikwm_proxy_candidates() -> list[str]:
    """Список прокси-адресов для TikWM в порядке попытки: основной + резервные
    (без дублей). Пустая строка ("" — TIKWM_API_BASE_URL не задан) означает
    "без прокси, прямые запросы к обоим зеркалам TikWM" — в этом случае список
    всегда из одного элемента [""], т.к. у прямого режима нет понятия "резервный
    прокси" (он и так уже пробует оба зеркала TikWM внутри самого запроса,
    см. _fetch_tikwm_media_data)."""
    if not TIKWM_API_BASE_URL:
        return [""]
    candidates = [TIKWM_API_BASE_URL] + [u for u in _TIKWM_API_BASE_URL_FALLBACKS if u != TIKWM_API_BASE_URL]
    return candidates

BOT_USERNAME = os.getenv("BOT_USERNAME", "LumenAI_bot").strip().lstrip("@")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY") or "").strip()
# Referer пересчитываем в try_setup, если владелец не задал его явно: иначе t.me остался бы со заглушкой.
_OPENROUTER_HTTP_REFERER_ENV_SET = bool(os.getenv("OPENROUTER_HTTP_REFERER", "").strip())
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", f"https://t.me/{BOT_USERNAME}").strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", BOT_USERNAME).strip()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Groq — прямой провайдер лёгкого текста (калибровка 21.09.2026): 1000 запросов/день против 50 у OpenRouter.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Раньше у OpenRouter был свой отдельный лимит истории (30), меньший, чем у Gemini
# (100) — при переключении провайдера (/provider или /model) ощущалось резкое
# "обнуление" контекста разговора. Теперь история ОБЩАЯ (см. state["history"] в
# get_state) и лимит один и тот же для обоих провайдеров.
SHARED_HISTORY_MAX_LEN = 100

# ── Sentry (опционально) — персистентный трекинг ошибок между рестартами ──
# Sentry: bot.log теряется при редеплое, Upstash логи не покрывает; ловит log.error без правок точек вызова.
def _redactable_secrets() -> tuple[str, ...]:
    """Единый список секретов для /logs и Sentry (включая производные секреты и Upstash-токен — полный доступ к историям)."""
    return _current_log_secrets()

def _sentry_scrub_secrets(event: dict, hint: dict) -> dict | None:
    """before_send-хук Sentry: вычищает секреты. Определена безусловно — тестируется без настоящего DSN."""
    payload = json.dumps(event, default=str, ensure_ascii=False)
    for secret in _redactable_secrets():
        payload = payload.replace(secret, "<REDACTED>")
    return json.loads(payload)

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        before_send=_sentry_scrub_secrets,
        # Только трекинг ошибок (трейсинг выключен — бережём квоту Sentry 5000 событий/мес).
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
    log.info('[setup] Sentry error tracking enabled.')

OWNER_ID: int | None = None
for env_name in ("OWNER_ID", "BOT_OWNER_ID", "ADMIN_ID", "TELEGRAM_OWNER_ID"):
    val = os.getenv(env_name, "").strip()
    if val and val.isdigit():
        OWNER_ID = int(val)
        break

TELEGRAM_REQUEST_TIMEOUT = float(os.getenv("TELEGRAM_REQUEST_TIMEOUT", "45"))
TELEGRAM_AI_TIMEOUT = float(os.getenv("TELEGRAM_AI_TIMEOUT", "45"))
TELEGRAM_MEDIA_TIMEOUT = float(os.getenv("TELEGRAM_MEDIA_TIMEOUT", "25"))
# Раньше было захардкожено как 15.0 прямо внутри _download_telegram_file_bytes —
# несогласованно с остальными таймаутами, которые все конфигурируются через env.
TELEGRAM_GET_FILE_TIMEOUT = float(os.getenv("TELEGRAM_GET_FILE_TIMEOUT", "15"))
# Cooldown после HTML-мусора от прокси вместо JSON: без него десятки вызовов/сек валят лавину WARNING (см. _tg_call).
TELEGRAM_PROXY_COOLDOWN_SEC = float(os.getenv("TELEGRAM_PROXY_COOLDOWN_SEC", "20"))
# Лимит ожидания следующего куска для любого провайдера: зависший стрим иначе держит лок чата бесконечно.
STREAM_CHUNK_TIMEOUT_SEC = float(os.getenv("STREAM_CHUNK_TIMEOUT_SEC", "30"))
# ── Паттерн "живой печати" при стриминге (см. lumen_typing_pace.py и
# _run_streaming_reply ниже) ── Раньше во время стрима сообщение показывало РОВНО
# то, что успело накопиться с последнего edit_text — если бэкенд (особенно у
# бесплатных моделей OpenRouter, см. докстринг lumen_typing_pace.py про то, почему
# скорость там не свойство модели) присылал текст парой больших кусков вместо
# потока токен-в-токен, пользователь видел резкие скачки на 15-20 слов вместо
# плавного набора. Теперь показ "подкрашивается" под оценённую (самокалибрующуюся,
# НЕ захардкоженную — см. lumen_typing_pace.py) скорость конкретной модели: пока
# реальный текст ещё приходит, видимый срез растёт по времени, а не скачком до
# всего, что уже накоплено. STREAM_EDIT_MIN_INTERVAL_SEC — не чаще какого периода
# реально дёргаем edit_text (тот же лимит, что защищал от 429 Telegram и раньше,
# просто вынесен в именованную константу). STREAM_TYPING_TICK_SEC/_MAX_CATCHUP_TICKS —
# только для "довывода" остатка ПОСЛЕ того, как стрим уже полностью получен, но
# показан ещё не весь (см. catchup_reveal_steps) — произведение двух этих чисел
# ограничивает МАКСИМАЛЬНУЮ добавленную задержку сверху реальной скорости ответа,
# независимо от длины текста и точности оценки скорости.
STREAM_EDIT_MIN_INTERVAL_SEC = float(os.getenv("STREAM_EDIT_MIN_INTERVAL_SEC", "1.2"))
STREAM_TYPING_TICK_SEC = float(os.getenv("STREAM_TYPING_TICK_SEC", "0.5"))
STREAM_TYPING_MAX_CATCHUP_TICKS = int(os.getenv("STREAM_TYPING_MAX_CATCHUP_TICKS", "6"))
# FIRST_CHUNK_TIMEOUT_SEC — пол ожидания первого куска стрима (см.
# lumen_model_speed.first_chunk_limit_sec): обычно-быстрая модель, зависшая
# разово, бросается рано (12–25с вместо полных 30). Честная оговорка: предел
# только УКОРАЧИВАЕТ ожидание, но не удлиняет — внутри генераторов кусков уже
# стоит STREAM_CHUNK_TIMEOUT_SEC на каждый кусок, включая первый, и для
# обычно-медленной модели первым сработает именно он. Итоговый предел первого
# куска — всегда минимум из двух.
FIRST_CHUNK_TIMEOUT_SEC = float(os.getenv("FIRST_CHUNK_TIMEOUT_SEC", "12"))
# Анимация ожидания ("бегущие точки") в плейсхолдере, пока не пришёл первый
# кусок стрима: первые полсекунды висит статичное "…" (дешевле, чем дёргать
# API ради мгновенных ответов — их анимация вообще не касается), дальше —
# кадр каждые _DOTS_TICK_SEC. Интервалы подобраны под лимит Telegram (не чаще
# правки в секунду) с запасом; правки идут только показывать нечего (до
# первого куска), поэтому с показом текста не конфликтуют.
_DOTS_START_AFTER_SEC = 0.5
_DOTS_TICK_SEC = 1.2
_DOTS_FRAMES = (".", "..", "…")
# RICH_MESSAGES_ENABLED — отправка финальных ответов через sendRichMessage /
# editMessageText+rich_message (Bot API 10.1+: таблицы, заголовки, математика).
# Kill-switch на случай проблем с рендером на старых клиентах: 0 — вернуться
# на обычный HTML-путь без редеплоя кода (только рестарт). Стрим-правки всегда
# идут обычным HTML (транзиент), рич применяется только к финальным текстам.
RICH_MESSAGES_ENABLED = os.getenv("RICH_MESSAGES_ENABLED", "1") == "1"
# Лимит длины текста для /tts — без него пользователь мог отправить огромный
# текст, что вызывало бы очень долгий прогон Gemini TTS + ffmpeg на один запрос.
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "800"))
_PROCESS_START_MONOTONIC = time.monotonic()
# ── Тайминги автоматического маршрутизатора моделей (см. секцию "автоматический
# выбор модели" ниже) ──
# Без ретраев одной модели: любая ошибка — сразу следующая в маршруте; общий бюджет ROUTE_TOTAL_BUDGET_SEC держит лок чата от минутного зависания.
ROUTE_MODEL_TIMEOUT_SEC = float(os.getenv("ROUTE_MODEL_TIMEOUT_SEC", "22"))
# ROUTE_TOTAL_BUDGET_SEC — общий бюджет времени на ВЕСЬ маршрут одного сообщения,
# включая ОБА провайдера (Gemini и OpenRouter), если маршрут предполагает
# резервный переход между ними. Без этого потолка каскадный сбой сразу у многих
# моделей/провайдеров мог бы растянуть один ответ на несколько минут, всё это
# время удерживая лок чата (_chat_locks). При превышении бюджета дальнейшие
# попытки прекращаются и пользователь получает честное "сейчас всё перегружено"
# вместо тихого зависания.
ROUTE_TOTAL_BUDGET_SEC = float(os.getenv("ROUTE_TOTAL_BUDGET_SEC", "40"))
# Общий бюджет /draw 120с: иначе 5 моделей × 90с давали до 7.5 мин висящего "Генерирую" (ревью 28.08.2026).
DRAW_TOTAL_BUDGET_SEC = float(os.getenv("DRAW_TOTAL_BUDGET_SEC", "120"))
# INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC — сколько main() при остановке ждёт штатного
# завершения fire-and-forget задач перед отменой остатка (Sentry LUMEN-2: event loop убивал их посреди сетевых вызовов при редеплое).
INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC = float(os.getenv("INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC", "10"))
TG_MAX_LEN = 4096
# Upload-ботов Telegram режет 50 МБ — слишком большие варианты качества пропускаем до скачивания.
TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# Слайдшоу TikTok — до 35 штук, media group — по 10: шлём весь пост несколькими вызовами.
TIKTOK_SLIDESHOW_MAX_ITEMS = 35
TELEGRAM_MEDIA_GROUP_CHUNK = 10
# Лимит ffprobe/ffmpeg-процессов: без него слайдшоу кладёт CPU контейнера (ревью 28.08.2026).
TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY = int(os.getenv("TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY", "4"))
_tiktok_probe_semaphore = asyncio.Semaphore(TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY)
# Лимит скачивания слайдов 8: 35 слайдов иначе занимают весь пул сессии (limit=40) и стопорят другие чаты (аудит 04.09.2026).
TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY = int(os.getenv("TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY", "8"))
_tiktok_slide_download_semaphore = asyncio.Semaphore(TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY)

# Состояние/квоты/локи живут в lumen_chat_state.py (P2): здесь только реэкспорт
# имён (те же объекты — тесты мутируют bot.chat_state/bot.GLOBAL_QUOTA как раньше).
import lumen_chat_state
from lumen_chat_state import (
    ChatState,
    chat_state,
    MAX_CHAT_LIMIT,
    PRUNED_CHAT_TARGET,
    MAX_CHAT_HISTORY_LEN,
    _STATE_DIR,
    STATE_FILE_PATH,
    GLOBAL_QUOTA_FILE,
    _CHATS_DIR,
    CHAT_INDEX_KEY,
    CHAT_INDEX_FILE,
    _storage_config,
    _upstash_request,
    _upstash_set,
    _upstash_get,
    _upstash_delete,
    _storage_write_text,
    _storage_read_text,
    _storage_delete_text,
    _chat_storage_path,
    _save_chat_to_storage,
    _delete_chat_storage,
    QuotaEntry,
    GLOBAL_QUOTA,
    _QUOTA_CHECK_THROTTLE_SEC,
    _maybe_alert_gemini_exhausted,
    _reset_quota_if_new_day,
    load_global_quota,
    save_global_quota,
    _restore_single_chat,
    _save_chat_index,
    _dirty_chat_ids,
    _pending_chat_deletions,
    _last_quota_check_monotonic,
    _last_gemini_exhausted_alert_monotonic,
    _save_chat_to_storage_limited,
    _delete_chat_storage_limited,
    mark_state_dirty,
    _mark_new_chat_id,
    mark_quota_dirty,
    _flush_dirty_state_once,
    _flush_dirty_state,
    _flush_state_now,
    load_state_from_disk,
    get_state,
    _chat_lang,
    _t,
    _prune_old_chats,
    get_chat_lock,
    _evict_orphan_chat_locks,
    _is_owner,
    _notify_owner,
    _is_privileged_in_chat,
    _quota_entry,
    _mark_quota_exhausted,
    _record_quota_usage,
    _trim_history,
)

# Простой трекер для rate limiting и очередь кнопок-уточнений живут в
# lumen_limits.py (P2): здесь только реэкспорт имён, чтобы `bot.X` в тестах
# и вызывающий код не менялись.
from lumen_limits import (
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW_SEC,
    MAX_RATE_LIMIT_KEYS,
    user_rate_limits,
    _cleanup_rate_limit_dict,
    _check_and_register_rate_limit,
    PICK_TTL_SEC,
    MAX_PENDING_PICKS,
    _pending_picks,
    _purge_expired_picks,
    _enforce_pending_picks_cap,
)

# ── Инициализация клиентов внутри цикла обработки событий (решает RuntimeError) ──
bot: Bot = None
dp = Dispatcher()
client: genai.Client = None

_http_session: aiohttp.ClientSession | None = None
_chat_locks: dict[int, asyncio.Lock] = {}

# Транспорт — в lumen_transport_calls.py.
from lumen_telegram_transport import (
    _TelegramProxyCircuitBreaker,
    IPv4AiohttpSession,
)

TELEGRAM_PROXY_TRIP_THRESHOLD = int(os.getenv("TELEGRAM_PROXY_TRIP_THRESHOLD", "3"))
_tg_proxy_breaker = _TelegramProxyCircuitBreaker(cooldown_sec=TELEGRAM_PROXY_COOLDOWN_SEC, trip_threshold=TELEGRAM_PROXY_TRIP_THRESHOLD)

# конвертация markdown в html, утилиты json

# Конвертация markdown/LaTeX/таблиц в Telegram HTML — чистые функции в lumen_formatting.py.
from lumen_formatting import _split_text_chunks

_PRUNE_SENTINEL = object()

def _json_prune_defaults(val: Any) -> Any:
    if val.__class__.__name__ == "Default":
        return _PRUNE_SENTINEL
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            pruned = _json_prune_defaults(v)
            if pruned is not _PRUNE_SENTINEL:
                out[k] = pruned
        return out
    if isinstance(val, (list, tuple)):
        return [v for v in (_json_prune_defaults(i) for i in val) if v is not _PRUNE_SENTINEL]
    return val

# Транспортные вызовы живут в lumen_transport_calls.py (P2).
from lumen_transport_calls import (
    _get_telegram_session,
    _rotate_telegram_proxy,
    _get_http_session,
    _close_sessions,
    _handle_proxy_failure,
    _tg_call,
    telegram_api_call,
)

# Отправка (rich/гости/ч-text) живёт в lumen_rich.py, скачивание медиа — в
# lumen_media_flow.py (P2): здесь только реэкспорт имён.
from lumen_rich import (
    is_guest_message,
    _answer_guest_text,
    _is_real_telegram_message,
    _try_send_rich,
    _try_edit_rich,
    _send_text,
    _safe_reply,
    _delete_message_quietly,
    _edit_message_quietly,
)
from lumen_media_flow import (
    _download_telegram_file_bytes,
    _save_media_to_history,
    _download_message_attachment_to_tmp,
    _fetch_media,
)

# разбор ошибок

# список моделей
#
# Конфигурация моделей и маршрутизация — в lumen_router_config.py (импорт на месте прежнего GEMINI_MODELS; только реально используемые имена).
from lumen_router_config import (
    DEFAULT_GEMINI_MODEL,
    _check_unconfirmed_model_quotas,
    _check_temporary_free_models_expiry,
    _check_scheduled_removals_due,
    _OR_LIGHT_ORDER,
    _OR_HEAVY_ORDER,
    _OR_VISION_ORDER,
    _GROQ_LIGHT_ORDER,
    GEMINI_TTS_MODELS,
    FISH_AUDIO_TTS_MODEL,
    FISH_AUDIO_ENABLED,
    _check_fish_audio_tts_expiry,
    _looks_like_heavy_query,
    _looks_like_freshness_query,
    _build_route,
)
# Слой ошибок живёт в lumen_errors.py (P2): здесь только реэкспорт имён.
from lumen_errors import (
    _error_text,
    _error_status,
    _classify_model_error,
    _model_error_text,
    _or_error_msg,
    GeminiAllModelsExhaustedError,
    _next_fallback_model,
    _gemini_error_msg,
    get_system_prompt,
    _MODEL_ERROR_FALLBACK_MSG,
)

# Форма chat_state[chat_id] — TypedDict ChatState (только аннотация для типов/читаемости, рантайм не меняет).

# Буферы альбомов/медиа-групп
_mg_buffers: dict[str, list[Message]] = {}
_mg_tasks: dict[str, asyncio.Task] = {}

# _inflight_tasks — общий набор fire-and-forget задач (Sentry LUMEN-2: event loop убивал их посреди сетевых вызовов при редеплое); main() дренирует при остановке.
_inflight_tasks: set[asyncio.Task] = set()

def _track_inflight_task(task: asyncio.Task) -> asyncio.Task:
    """Регистрирует fire-and-forget таску для graceful shutdown — см. комментарий
    у _inflight_tasks выше. Возвращает ту же таску, чтобы вызов можно было
    обернуть прямо вокруг asyncio.create_task(...) без лишней временной переменной."""
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)
    return task

# HTTP-слой (app, секрет-гейты, /healthz, /admin_keys, /webhook_url, /webhook,
# /diag, /export_state) живёт в lumen_admin.py (P2): здесь только реэкспорт имён,
# чтобы `bot.X` в тестах и main() не менялись. Секреты выводятся здесь же ниже —
# тесты патчат bot.WEBHOOK_SECRET/bot.ADMIN_PANEL_KEY напрямую.
from lumen_admin import (
    app,
    _check_bearer_token,
    _check_admin_key,
    _redact_secret,
    _check_bot_token_auth,
    healthcheck,
    get_admin_keys,
    get_webhook_url,
    webhook_handler,
    network_diagnostics,
    export_state,
)

# ADMIN_SECRET_SEED — независимая соль для секретов (иначе всё выводилось из BOT_TOKEN и ротировалось только с ним). Пустой seed при пустом токене — случайный секрет процесса, а не захардкоженная строка.
_ADMIN_SECRET_SEED = os.getenv("ADMIN_SECRET_SEED", "").strip() or BOT_TOKEN or secrets.token_hex(32)
WEBHOOK_SECRET = hashlib.sha256(_ADMIN_SECRET_SEED.encode()).hexdigest()[:32]
ADMIN_PANEL_KEY = hashlib.sha256(_ADMIN_SECRET_SEED.encode() + b"admin_panel").hexdigest()[:24]

# guest_message — валидное поле Update из Bot API (guest mode: changelog и
# раздел Update на core.telegram.org/bots/api, проверено 2026-09-21).
# Удаление отсюда было ошибкой аудита AUD-J-001 (проверка от 2026-09-19
# устарела): без этого типа бот не получает гостевые апдейты.
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "guest_message"]

# хранение состояния и квот


# Опциональное персистентное хранилище (Upstash Redis, бесплатный тир — см. README).
# Если оба значения заданы, состояние пишется туда вместо эфемерного диска контейнера.
# Если не заданы — поведение полностью как раньше (локальный файл в STATE_DIR), без
# каких-либо изменений для тех, кто это не настраивал.
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
USE_UPSTASH = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)
if bool(UPSTASH_REDIS_REST_URL) != bool(UPSTASH_REDIS_REST_TOKEN):
    log.warning('[setup] Only one of UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN is set — both are required together, Upstash will not be used.')
log.info(
    '[setup] Persistent storage: %s',
    "Upstash Redis" if USE_UPSTASH else f"локальный файл в {_STATE_DIR} (см. README про эфемерность на HF Spaces)"
)

# Хранилище — в lumen_state_storage.py + lumen_chat_state.py.
from lumen_state_storage import (
    CHAT_STATE_SCHEMA_VERSION,
    _serialize_chat_state,
    _current_quota_day,
)


# `_urllib_request` (импортирован в самом начале файла) больше не используется
# напрямую нигде в коде bot.py — реальный клиент Upstash REST API (единственный
# потребитель) переехал в lumen_state_storage.py. Остаётся нужен как
# `bot._urllib_request.urlopen` для существующих тестов, которые патчат его именно
# по этому пути (см. пояснение про __all__ у первого блока (lumen_formatting) в
# начале файла). CHAT_STATE_SCHEMA_VERSION аналогично не используется напрямую в
# коде bot.py (сравнение идёт внутри _serialize_chat_state в lumen_state_storage.py),
# но нужен как `bot.CHAT_STATE_SCHEMA_VERSION` тестам, сверяющим версию схемы снимка.
# Имена из lumen_limits.py (RATE_LIMIT_*/user_rate_limits/MAX_PENDING_PICKS) код
# bot.py сам не читает — они нужны как `bot.X` существующим тестам, поэтому тоже
# здесь (импорт — вверху файла, рядом с остальными lumen_импортами).
__all__ = [
    "_urllib_request",
    "CHAT_STATE_SCHEMA_VERSION",
    "RATE_LIMIT_MAX_REQUESTS",
    "RATE_LIMIT_WINDOW_SEC",
    "MAX_RATE_LIMIT_KEYS",
    "user_rate_limits",
    "MAX_PENDING_PICKS",
    # Имена из lumen_admin.py (app, гейты, эндпоинты) код bot.py сам не читает
    # (main() берёт только app) — они нужны как `bot.X` существующим тестам.
    "app",
    "_check_bearer_token",
    "_check_admin_key",
    "_redact_secret",
    "_check_bot_token_auth",
    "healthcheck",
    "get_admin_keys",
    "get_webhook_url",
    "webhook_handler",
    "network_diagnostics",
    "export_state",
    # Имена из lumen_streaming.py код bot.py сам не читает — они нужны как `bot.X`
    # существующим тестам и `_run_route` ниже.
    "_tick_waiting_dots",
    "_pieces_with_waiting_feedback",
    "_gemini_stream_pieces",
    "_openrouter_stream_pieces",
    "_groq_stream_pieces",
    "_run_streaming_reply",
    "_try_gemini_streaming",
    "_try_openrouter_streaming",
    "_try_groq_streaming",
    # Точка подмены тестов (см. monkeypatch в tests/) — сам код bot.py её
    # больше не читает напрямую после выноса стриминга.
    "_model_first_chunk_limit",
    # Константы-фолбэки тестов (читаются как bot._IDENTITY_LEAK_FALLBACK).
    "_IDENTITY_LEAK_FALLBACK",
    "_INJECTED_PAYLOAD_ECHO_FALLBACK",
    # Имена из lumen_tiktok_flow.py код bot.py сам не читает — они нужны как
    # `bot.X` существующим тестам и `_handle_message_core` ниже.
    "_send_tiktok_music",
    "handle_tiktok_sound",
    "_fetch_tikwm_media_data_with_proxy_fallback",
    "_try_send_tiktok_slideshow",
    "_send_tiktok_single_video",
    "handle_tiktok",
    # Механика lumen_tiktok.py как `bot.X` для существующих тестов.
    "_original_sound_label",
    "_GENERIC_ORIGINAL_SOUND_PHRASES",
    "_chunk_tiktok_media_items",
    "_looks_like_video_bytes",
    "_slideshow_slide_urls",
    "_tiktok_video_candidates",
    "TikTokUserFacingError",
    "_tiktok_music_page_id",
    "_write_mp3_tags",
    "_download_url_bin",
    "_probe_video_dimensions",
    "_generate_video_thumbnail",
    "_probe_and_thumbnail_from_bytes",
    "_resolve_tiktok_short",
    "_fetch_tikwm_media_data",
    # Имена из lumen_routes.py код bot.py сам не читает — они нужны как `bot.X`
    # существующим тестам и `_handle_message_core` ниже.
    "OpenRouterAPIError",
    "GroqAPIError",
    "_or_request",
    "_groq_request",
    "_or_extract_text",
    "_is_account_wide_or_rate_limit",
    "_probe_or_model_liveness",
    "_or_chat_completion_with_fallback",
    "ask_openrouter_text",
    "ask_openrouter_multimodal",
    "ask_groq_text",
    "_gemini_history_contents",
    "_build_gemma_identity_contents",
    "_build_gemini_call_config",
    "_extract_gemini_answer_text",
    "ask_gemini",
    "_build_gemini_turn_contents",
    "_build_openrouter_turn_messages",
    "_route_error_reply_text",
    "RouteBudgetExceededError",
    "_run_route",
    # Порядки моделей и _history_user_text читаются тестами как bot.X.
    "_OR_LIGHT_ORDER",
    "_OR_HEAVY_ORDER",
    "_OR_VISION_ORDER",
    "_GROQ_LIGHT_ORDER",
    "_history_user_text",
    # Пространство имён genai-типов для тестов (bot.types.Content/...).
    "types",
    # Имена из lumen_chat_state.py код bot.py сам не читает — они нужны как
    # `bot.X` существующим тестам.
    "ChatState",
    "MAX_CHAT_LIMIT",
    "PRUNED_CHAT_TARGET",
    "MAX_CHAT_HISTORY_LEN",
    "STATE_FILE_PATH",
    "GLOBAL_QUOTA_FILE",
    "_CHATS_DIR",
    "CHAT_INDEX_KEY",
    "CHAT_INDEX_FILE",
    "_storage_config",
    "_upstash_request",
    "_upstash_set",
    "_upstash_get",
    "_upstash_delete",
    "_storage_write_text",
    "_storage_read_text",
    "_storage_delete_text",
    "_chat_storage_path",
    "_save_chat_to_storage",
    "_delete_chat_storage",
    "QuotaEntry",
    "_QUOTA_CHECK_THROTTLE_SEC",
    "load_global_quota",
    "_restore_single_chat",
    "_save_chat_index",
    "_save_chat_to_storage_limited",
    "_delete_chat_storage_limited",
    "_mark_new_chat_id",
    "mark_quota_dirty",
    "_flush_dirty_state_once",
    "_prune_old_chats",
    "_quota_entry",
    # Прямые имена lumen_state_storage для тестов.
    "_serialize_chat_state",
    "_current_quota_day",
    # Монотонный маркер троттлинга проверки даты (читают тесты).
    "_last_quota_check_monotonic",
    "_last_gemini_exhausted_alert_monotonic",
    # Имена остальных вынесенных модулей — только для `bot.X` в тестах.
    "GEMINI_TTS_MODELS",
    "FISH_AUDIO_TTS_MODEL",
    "FISH_AUDIO_ENABLED",
    "POLLINATIONS_IMAGE_MODELS",
    "_pick_image_model",
    "_pollinations_text_to_image",
    "_communicate_process",
    "_fish_audio_tts_bytes",
    "_gemini_tts_bytes",
    "chat_state",
    "GLOBAL_QUOTA",
    "_is_owner",
    "_is_privileged_in_chat",
    "_mark_quota_exhausted",
    "_record_quota_usage",
    "_trim_history",
    "PICK_TTL_SEC",
    "_pending_picks",
    "_purge_expired_picks",
    "_enforce_pending_picks_cap",
    # Имена из lumen_errors.py код bot.py сам не читает — только `bot.X` в тестах.
    "_error_text",
    "_error_status",
    "_classify_model_error",
    "_model_error_text",
    "_or_error_msg",
    "GeminiAllModelsExhaustedError",
    "_next_fallback_model",
    "_gemini_error_msg",
    "get_system_prompt",
    "_MODEL_ERROR_FALLBACK_MSG",
    # Имена остальных вынесенных модулей — только для `bot.X` в тестах.
    "ParseMode",
    "_split_text_chunks",
    "is_guest_message",
    "_answer_guest_text",
    "_is_real_telegram_message",
    "_try_send_rich",
    "_try_edit_rich",
    "_send_text",
    "_safe_reply",
    "_delete_message_quietly",
    "_edit_message_quietly",
    "_download_telegram_file_bytes",
    "_save_media_to_history",
    "_download_message_attachment_to_tmp",
    "_fetch_media",
    "_sanitize_mime_type",
    "_mime_suffix",
    "_msg_media_source",
    "_media_file_id_and_mime",
    "_ensure_prompt_text",
    "_looks_like_injection_probe",
    "DRAW_TRIGGER_PREFIXES",
    "TTS_TRIGGER_PREFIXES",
    "_NO_MEDIA_NOTE",
    "_match_trigger_prefix",
    "_media_reference_category",
    "_find_recent_media_by_category",
    "_strip_reply_marker",
    "extract_url",
    "is_tiktok",
    "is_youtube",
    "match_pick_request",
    "clean_mention",
    "inline_draw",
    "inline_tts",
    "_send_pick_question",
    "DEFAULT_GEMINI_MODEL",
    "_looks_like_heavy_query",
    "_looks_like_freshness_query",
    "_build_route",
    "_maybe_alert_gemini_exhausted",
    "mark_state_dirty",
    "get_state",
    "_chat_lang",
    "_check_and_register_rate_limit",
    "_record_passive_group_context",
    "_should_only_record_passively",
    "_rate_limit_key_for_message",
    "_reject_rate_limited_message",
    "_resolve_incoming_media",
    "_process_raw_update",
    # Имена транспортных вызовов и _notify_owner — только для `bot.X` в тестах.
    "_get_telegram_session",
    "_rotate_telegram_proxy",
    "_get_http_session",
    "_close_sessions",
    "_handle_proxy_failure",
    "_tg_call",
    "telegram_api_call",
    "_notify_owner",
]

# Троттлинг для уведомления владельца о полном исчерпании квоты Gemini (см.
# _handle_message_core) — без него один и тот же алерт улетал бы на КАЖДОЕ
# сообщение, требующее Gemini, пока квота не восстановится (может быть весь день).
GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC = 3600.0



# генерация изображений (pollinations)
#
# Генерация картинок — в lumen_images.py (чистые функции, сессию передаёт вызывающий код).
from lumen_images import (
    DEFAULT_POLLINATIONS_IMAGE_MODEL,
    POLLINATIONS_IMAGE_MODELS,
    _pick_image_model,
    _pollinations_text_to_image,
)

# DEFAULT_POLLINATIONS_IMAGE_MODEL больше не читается напрямую нигде в остальном коде bot.py
# (используется только внутри самой _pick_image_model в lumen_images.py) — но
# остаётся нужен как `bot.DEFAULT_POLLINATIONS_IMAGE_MODEL` для существующих тестов. См.
# пояснение про __all__ у первого блока (lumen_formatting) в начале файла.
__all__ += ["DEFAULT_POLLINATIONS_IMAGE_MODEL"]

# метаданные и скачивание медиа: чистые утилиты (mime-типы, file_id,
# суффиксы) вынесены в lumen_media.py (срез монолита) — импортируются напрямую,
# чтобы `bot._sanitize_mime_type(...)` и т.д. продолжали работать ровно как
# раньше (включая подмену в тестах через module globals). Сетевая часть
# (_download_telegram_file_bytes/_fetch_media/...) остаётся здесь: ей нужны
# сессия, BOT_TOKEN и bot.get_file.
from lumen_media import (
    _sanitize_mime_type,
    _media_file_id_and_mime,
    _mime_suffix,
    _msg_media_source,
    _ensure_prompt_text,
)
# (импорт lumen_lang — вверху файла, рядом с system_prompt: _MODEL_ERROR_MESSAGES
# выше по файлу уже использует его на уровне модуля)
# загрузка медиа




# openrouter api
#
# TEXT_MODEL_ORDER / _OR_MODEL_HEALTH / _ROUTER_EXCLUDED_OR_MODELS /
# _check_temporary_free_models_expiry вынесены в lumen_router_config.py (см.
# импорт рядом с GEMINI_MODELS выше по файлу) — здесь остаётся только код,
# который реально ХОДИТ в OpenRouter API (OpenRouterAPIError/_or_request/
# ask_openrouter_*/_or_chat_completion_with_fallback и т.д.).

# ─────────────────── защита от утечки провайдера/модели и промт-инъекций ───────────────────
# Детекторы утечек/инъекций — чистые функции в lumen_security.py (импортируем только используемое).
from lumen_security import (
    _IDENTITY_LEAK_FALLBACK,
    _INJECTED_PAYLOAD_ECHO_FALLBACK,
    _looks_like_injection_probe,
)
# (_INJECTION_PROBE_REPLY здесь больше не импортируется: ответ на провокации
# теперь берётся из lumen_lang.py по языку чата — см. ключ injection_probe_reply)


# LLM-маршрутизация живёт в lumen_routes.py (P2): здесь только реэкспорт
# имён, чтобы `bot.X` в тестах и `_handle_message_core` не менялись.
from lumen_routes import (
    OpenRouterAPIError,
    GroqAPIError,
    _or_request,
    _groq_request,
    _or_extract_text,
    _is_account_wide_or_rate_limit,
    _probe_or_model_liveness,
    _or_chat_completion_with_fallback,
    ask_openrouter_text,
    ask_openrouter_multimodal,
    ask_groq_text,
    _gemini_history_contents,
    _build_gemma_identity_contents,
    _build_gemini_call_config,
    _extract_gemini_answer_text,
    ask_gemini,
    _build_gemini_turn_contents,
    _build_openrouter_turn_messages,
    _route_error_reply_text,
    RouteBudgetExceededError,
    _run_route,
)

# скачивание тикток
#
# Механика загрузчика — в lumen_tiktok.py, оркестрация отправки — в
# lumen_tiktok_flow.py (P2). Имена ниже код bot.py сам не читает (кроме
# _communicate_process для TTS-пробинга) — они нужны как `bot.X` существующим
# тестам, поэтому тоже здесь.
from lumen_tiktok import (
    _original_sound_label,
    _GENERIC_ORIGINAL_SOUND_PHRASES,
    _chunk_tiktok_media_items,
    _looks_like_video_bytes,
    _slideshow_slide_urls,
    _tiktok_video_candidates,
    TikTokUserFacingError,
    _tiktok_music_page_id,
    _write_mp3_tags,
    _download_url_bin,
    _probe_video_dimensions,
    _generate_video_thumbnail,
    _communicate_process,
    _probe_and_thumbnail_from_bytes,
    _resolve_tiktok_short,
    _fetch_tikwm_media_data,
    _looks_like_resolved_tiktok_url,
)

# _looks_like_resolved_tiktok_url используется только внутри самой _resolve_tiktok_short
# в lumen_tiktok.py — но остаётся нужна как `bot._looks_like_resolved_tiktok_url(...)`
# для регрессионных тестов (см. пояснение про __all__ у первого блока (lumen_formatting)
# в начале файла).
__all__ += ["_looks_like_resolved_tiktok_url"]

# TikTok-оркестрация живёт в lumen_tiktok_flow.py (P2): здесь только реэкспорт
# имён, чтобы `bot.X` в тестах и `_handle_message_core` не менялись.
from lumen_tiktok_flow import (
    _send_tiktok_music,
    handle_tiktok_sound,
    _fetch_tikwm_media_data_with_proxy_fallback,
    _try_send_tiktok_slideshow,
    _send_tiktok_single_video,
    handle_tiktok,
)


# Самокалибрующаяся оценка скорости "печати" — см. докстринг lumen_typing_pace.py
# про то, почему это НЕ статическая таблица токенов/сек по каждой модели: реальная
# скорость отдачи текста бесплатными моделями OpenRouter не является свойством
# самой модели (провайдер маршрутизирует один слаг на разные бэкенды), поэтому
# любая захардкоженная цифра устарела бы быстрее, чем список живых/мёртвых моделей
# в _OR_MODEL_HEALTH. Вместо этого — измерение по факту на каждом стриме (см.
# _run_streaming_reply) и экспоненциальное усреднение; при добавлении/замене
# модели НИЧЕГО вручную обновлять не нужно — новая модель "нащупывает" свою
# реальную скорость сама за первые несколько ответов.

# Самокалибрующаяся оценка задержек моделей — см. докстринг lumen_model_speed.py.
# Здесь осталось только имя-точка подмены тестов; само измерение уехало в
# lumen_routes.py/lumen_streaming.py вместе с вызывающим кодом.
from lumen_model_speed import (
    first_chunk_limit_sec as _model_first_chunk_limit,
)

# Точка подмены сна довывода: tests/conftest.py глушит, иначе сьют тормозит.
_typing_sleep = asyncio.sleep

# Точка подмены анимации точек — намеренно БЕЗ autouse: no-op сломал бы проверки последовательностей правок.
_dots_sleep = asyncio.sleep


# Стриминг живёт в lumen_streaming.py (P2): здесь только реэкспорт имён,
# чтобы `bot.X` в тестах и `_run_route` не менялись.
from lumen_streaming import (
    _tick_waiting_dots,
    _pieces_with_waiting_feedback,
    _gemini_stream_pieces,
    _openrouter_stream_pieces,
    _groq_stream_pieces,
    _run_streaming_reply,
    _try_gemini_streaming,
    _try_openrouter_streaming,
    _try_groq_streaming,
)

# определение ссылок и упоминаний, триггеры draw/tts, категории медиа —
# вынесено в lumen_message_parse.py (срез монолита): чистые функции над
# строками + конфигурация, без зависимости от рантайма. Импортируется напрямую,
# чтобы `bot.extract_url(...)`, `bot.DRAW_TRIGGER_PREFIXES` и т.д. продолжали
# работать ровно как раньше (включая подмену в тестах через module globals).
# clean_mention остаётся здесь: ей нужен BOT_USERNAME из этого модуля.
from lumen_message_parse import (
    DRAW_TRIGGER_PREFIXES,
    TTS_TRIGGER_PREFIXES,
    _NO_MEDIA_NOTE,
    _history_user_text,
    _match_trigger_prefix,
    _media_reference_category,
    _looks_like_media_reference,
    _mime_matches_media_category,
    _find_recent_media_by_category,
    _strip_reply_marker,
    extract_url,
    is_tiktok,
    is_youtube,
    match_pick_request,
)

# _looks_like_media_reference/_mime_matches_media_category кодом bot.py больше
# не используются напрямую (только через _find_recent_media_by_category), но
# остаются нужны как `bot.X` для существующих тестов — см. пояснение про __all__
# у первого блока (lumen_formatting) в начале файла.
__all__ += ["_looks_like_media_reference", "_mime_matches_media_category"]

def clean_mention(text: str) -> str:
    return re.sub(rf"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE).strip()

# Триггеры, маркеры реплая, категории медиа и пометка "файла нет" живут в
# lumen_message_parse.py (импорт выше) — здесь остаются только clean_mention
# (нужен BOT_USERNAME) и message_mentions_bot ниже.

# Защита от промт-инъекций (входной префильтр) вынесена в lumen_security.py вместе
# с защитой от утечки идентичности (см. импорт рядом с _detect_identity_leak выше) —
# см. импорт _looks_like_injection_probe/_INJECTION_PROBE_REPLY там же.

def message_mentions_bot(message: Message) -> bool:
    if message.chat.type == ChatType.PRIVATE:
         return True
    t = message.text or message.caption or ""
    if f"@{BOT_USERNAME}".lower() in t.lower():
         return True
    if message.reply_to_message and message.reply_to_message.from_user:
         if message.reply_to_message.from_user.username and message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower():
              return True
    return False

# команды бота

# Команды/TTS/Draw/pick живут в lumen_commands.py (P2): здесь только реэкспорт
# имён и регистрация хендлеров (декораторы @dp.* заменены явными register —
# тот же порядок, то же поведение).
from lumen_commands import (
    cmd_start,
    inline_draw,
    cmd_draw,
    _fish_audio_tts_bytes,
    _gemini_tts_bytes,
    inline_tts,
    cmd_tts,
    cmd_reset,
    cmd_lang,
    handle_lang_callback,
    cmd_logs,
    cmd_stats,
    _send_pick_question,
    handle_pick_callback,
)
dp.message.register(cmd_start, Command("start"))
dp.message.register(cmd_draw, Command("draw"))
dp.message.register(cmd_tts, Command("tts"))
dp.message.register(cmd_reset, Command("reset"))
dp.message.register(cmd_lang, Command("lang"))
dp.callback_query.register(handle_lang_callback)
dp.message.register(cmd_logs, Command("logs"))
dp.message.register(cmd_stats, Command("stats"))
dp.callback_query.register(handle_pick_callback)


# ─────────────────── автоматический выбор модели (роутер) ───────────────────
# Маршрут строится заново на каждое сообщение; Gemini только под поиск/ссылки/YouTube — его квота самая дефицитная.


# ── Кнопки-уточнения (pick-сценарии) ──
# Детерминированная альтернатива "одному уточняющему вопросу" модели для
# вкусовых запросов без деталей ("посоветуй фильм"): вопрос с кнопками вместо
# гадания. Опции заданы кодом (см. PICK_TABLE в lumen_lang.py — вопросы,
# варианты и шаблоны на языке чата), никаких сгенерированных моделью
# вариантов — слабые модели их калечат.
# Без эмодзи в кнопках — по правилу эмодзи (см. system_prompt.py).
PICK_BUTTONS_ENABLED = os.getenv("PICK_BUTTONS_ENABLED", "1") == "1"
# PICK_TTL_SEC/MAX_PENDING_PICKS/_pending_picks/_purge/_enforce — в lumen_limits.py,
# импортированы выше рядом с rate limit (P2), здесь используются напрямую.






# обработка сообщений

# Ядро обработки сообщений живёт в lumen_message_core.py (P2).

@dp.errors()
async def global_error_handler(event: Any) -> bool:
    log.error("Global error handler caught exception", exc_info=event.exception)
    return True

@dp.message()
async def handle_message(message: Message) -> None:
    if message.media_group_id:
        mgid = message.media_group_id
        _mg_buffers.setdefault(mgid, []).append(message)
        if mgid not in _mg_tasks or _mg_tasks[mgid].done():
             _mg_tasks[mgid] = _track_inflight_task(asyncio.create_task(_process_media_group_buffers(mgid)))
        return

    chat_id = message.chat.id if message.chat else 0
    is_private = message.chat.type == ChatType.PRIVATE if message.chat else True
    is_guest = is_guest_message(message)
    mentioned = message_mentions_bot(message)

    # Если бот не упомянут в группе, это просто контекст — обрабатываем без локов и ожидания
    if not is_private and not is_guest and not mentioned:
        await _handle_message_core(message)
        return

    lock = get_chat_lock(chat_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=45.0)
    except asyncio.TimeoutError:
        log.warning("[lock] Timeout waiting for lock on chat %s", chat_id)
        with contextlib.suppress(Exception):
             await _tg_call(message.reply, _t(chat_id, "lock_busy"))
        return

    try:
         await _handle_message_core(message)
    finally:
         with contextlib.suppress(Exception):
              lock.release()

# вебхук и запуск
from lumen_message_core import (
    _process_media_group_buffers,
    _record_passive_group_context,
    _should_only_record_passively,
    _rate_limit_key_for_message,
    _reject_rate_limited_message,
    _resolve_incoming_media,
    _handle_message_core,
    _process_raw_update,
)

async def _webhook_startup() -> None:
    load_state_from_disk()
    log.info("Bot startup: webhook mode.")
    _check_temporary_free_models_expiry()
    _check_unconfirmed_model_quotas()
    _check_fish_audio_tts_expiry()
    _check_scheduled_removals_due()
    # запросы ДО того, как Telegram попробует провалидировать доступность
    # /webhook при регистрации через setWebhook.
    await asyncio.sleep(1.5)

    space_host = os.getenv("SPACE_HOST", "").strip()
    if not space_host:
        author = os.getenv("SPACE_AUTHOR_NAME", "silverelixir").lower()
        repo = os.getenv("SPACE_REPO_NAME", "lumen").lower()
        space_host = f"{author}-{repo}.hf.space"
    webhook_url = f"https://{space_host}/webhook"

    log.info("[webhook] Space URL: https://%s", space_host)
    log.info("[webhook] Webhook endpoint: %s", webhook_url)
    # Полные секреты в логи не печатаем (доступ к /diag и /webhook_url), только отпечаток; полные — через /admin_keys с Bearer BOT_TOKEN (не query — токен в URL оседает в логах прокси).
    log.info('[webhook] WEBHOOK_SECRET (fingerprint): %s', _redact_secret(WEBHOOK_SECRET))
    log.info(
        '[admin] Full keys (WEBHOOK_SECRET/ADMIN_PANEL_KEY): curl -H "Authorization: Bearer <your BOT_TOKEN>" https://%s/admin_keys',
        space_host,
    )

    commands = [
        BotCommand(command="start", description=_lang_t(DEFAULT_LANG, "cmd_desc_start")),
        BotCommand(command="draw", description=_lang_t(DEFAULT_LANG, "cmd_desc_draw")),
        BotCommand(command="tts", description=_lang_t(DEFAULT_LANG, "cmd_desc_tts")),
        BotCommand(command="reset", description=_lang_t(DEFAULT_LANG, "cmd_desc_reset")),
        BotCommand(command="lang", description=_lang_t(DEFAULT_LANG, "cmd_desc_lang")),
    ]
    # Локализованные описания команд: Telegram показывает меню на языке
    # клиента (language_code), если такой вариант задан. Не задали — клиент
    # увидит дефолтный английский список выше. Каждый язык — отдельным вызовом,
    # падение одного не роняет остальные.
    localized_commands = [
        (
            code,
            [
                BotCommand(command="start", description=_lang_t(code, "cmd_desc_start")),
                BotCommand(command="draw", description=_lang_t(code, "cmd_desc_draw")),
                BotCommand(command="tts", description=_lang_t(code, "cmd_desc_tts")),
                BotCommand(command="reset", description=_lang_t(code, "cmd_desc_reset")),
                BotCommand(command="lang", description=_lang_t(code, "cmd_desc_lang")),
            ],
        )
        for code in COMMAND_LOCALES
    ]

    async def try_setup():
        global BOT_USERNAME, OPENROUTER_HTTP_REFERER
        try:
            me_data = await telegram_api_call("getMe", {})
            if isinstance(me_data, dict) and me_data.get("username"):
                BOT_USERNAME = me_data["username"].strip().lstrip("@")
                log.info("[webhook] Dynamically fetched BOT_USERNAME: @%s", BOT_USERNAME)
                if not _OPENROUTER_HTTP_REFERER_ENV_SET:
                    # См. комментарий у _OPENROUTER_HTTP_REFERER_ENV_SET выше — без этого
                    # заголовок HTTP-Referer к OpenRouter так и остался бы со старым/
                    # заглушечным юзернеймом весь срок жизни процесса.
                    OPENROUTER_HTTP_REFERER = f"https://t.me/{BOT_USERNAME}"
        except Exception as exc:
            log.warning("[webhook] Failed fetching BOT_USERNAME dynamically, using fallback @%s: %s", BOT_USERNAME, exc)

        try:
            await asyncio.wait_for(
                telegram_api_call("deleteWebhook", {"drop_pending_updates": False}, request_timeout=15.0),
                timeout=18.0
            )
            log.info("[webhook] Old webhook/polling cleared.")
        except Exception as exc:
            log.warning("[webhook] deleteWebhook failed (will need manual setup): %s", exc)

        try:
            await asyncio.wait_for(
                telegram_api_call("setWebhook", {
                    "url": webhook_url,
                    "secret_token": WEBHOOK_SECRET,
                    "drop_pending_updates": True,
                    "allowed_updates": ALLOWED_UPDATES,
                }, request_timeout=15.0),
                timeout=18.0
            )
            log.info("[webhook] Webhook registered successfully: %s", webhook_url)
        except Exception as exc:
            # Секрет в лог не подставляем — готовая ссылка живёт в /webhook_url (Bearer ADMIN_PANEL_KEY, не query).
            log.warning(
                '[webhook] setWebhook failed — register it manually via: curl -H "Authorization: Bearer <ADMIN_PANEL_KEY>" https://.../webhook_url (get ADMIN_PANEL_KEY via curl -H "Authorization: Bearer <BOT_TOKEN>" .../admin_keys if you don\'t have it handy): %s',
                exc,
            )

        cmd_payloads = [
            {"commands": [{"command": c.command, "description": c.description} for c in commands]}
        ] + [
            {
                "commands": [{"command": c.command, "description": c.description} for c in cmds],
                "language_code": code,
            }
            for code, cmds in localized_commands
        ]
        for cmd_payload in cmd_payloads:
            try:
                await asyncio.wait_for(
                    telegram_api_call("setMyCommands", cmd_payload, request_timeout=15.0),
                    timeout=18.0
                )
            except Exception as exc:
                log.warning("[webhook] setMyCommands failed (%s): %s", cmd_payload.get("language_code", "default"), exc)
        else:
            log.info("[webhook] Bot commands set successfully.")

    await try_setup()

    log.info("[webhook] Bot is running in webhook mode. Updates arrive via POST /webhook")
    # Суточные перепроверки моделей в часовом цикле: иначе истёкшее промо (как hy3:free) видно только после рестарта.
    _last_daily_check_date = date.today()
    while True:
        await asyncio.sleep(3600)
        _cleanup_rate_limit_dict()
        _evict_orphan_chat_locks()
        # Проверяем и обнуляем счётчики квоты на каждом часовом тике (а не только
        # раз в сутки, как две проверки ниже) — если между тиками не пришло ни
        # одного сообщения, ленивая проверка внутри _quota_entry не сработает
        # сама, и /stats ещё какое-то время показывал бы вчерашние числа.
        _reset_quota_if_new_day()
        today = date.today()
        if today != _last_daily_check_date:
            _last_daily_check_date = today
            _check_temporary_free_models_expiry()
            _check_unconfirmed_model_quotas()
            _check_fish_audio_tts_expiry()
            _check_scheduled_removals_due()
            await _probe_or_model_liveness()

async def _drain_inflight_tasks() -> None:
    """Даёт fire-and-forget задачам обработки апдейтов (см. _inflight_tasks/
    _track_inflight_task) шанс завершиться штатно, вместо того чтобы быть
    уничтоженными event loop'ом на середине (см. LUMEN-2 в Sentry: "Task was
    destroyed but it is pending!", реальная асинхронная задача внутри держала
    вызов bot.send_message). Ждёт до INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC секунд;
    то, что не успело — явно отменяет и ДОЖИДАЕТСЯ самой отмены (а не просто
    вызывает cancel() и уходит — иначе получили бы то же самое предупреждение
    асинхронно, просто чуть позже, когда GC доберётся до объекта таски)."""
    pending = [t for t in _inflight_tasks if not t.done()]
    if not pending:
        return
    log.info('[shutdown] Waiting up to %.0fs for %d in-flight update task(s) to finish.', INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC, len(pending))
    _done, still_pending = await asyncio.wait(pending, timeout=INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC)
    if still_pending:
        log.warning('[shutdown] %d in-flight task(s) did not finish in time, cancelling.', len(still_pending))
        for t in still_pending:
            t.cancel()
        await asyncio.gather(*still_pending, return_exceptions=True)

async def main() -> None:
    global bot, client

    # Мы настраиваем Bot сессию с принудительным IPv4 и таймаутами для hg space
    if TELEGRAM_API_BASE_URL != "https://api.telegram.org":
        api_server = TelegramAPIServer.from_base(TELEGRAM_API_BASE_URL)
        sess = IPv4AiohttpSession(
            api=api_server,
            proxy_secret=LUMEN_PROXY_SECRET, proxy_base_urls=_TELEGRAM_PROXY_CANDIDATES,
        )
    else:
        sess = IPv4AiohttpSession(
            proxy_secret=LUMEN_PROXY_SECRET, proxy_base_urls=_TELEGRAM_PROXY_CANDIDATES,
        )
    bot = Bot(token=BOT_TOKEN, session=sess)
    client = genai.Client(api_key=GEMINI_API_KEY)

    startup_task = asyncio.create_task(_webhook_startup(), name="webhook_startup")
    flush_task = asyncio.create_task(_flush_dirty_state(), name="state_flush")
    srv_config = uvicorn.Config(
        app=app, host="0.0.0.0", port=7860, log_level="info", loop="asyncio", log_config=None,
    )
    server = uvicorn.Server(srv_config)
    try:
        await server.serve()
    finally:
        startup_task.cancel()
        flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
             await startup_task
        with contextlib.suppress(asyncio.CancelledError):
             await flush_task
        # Незавершённым апдейтам — шанс закончиться ДО сброса состояния/закрытия сессий (иначе правки chat_state терялись, сеть рвалась — LUMEN-2).
        await _drain_inflight_tasks()
        # Финальный синхронный сброс — не ждём следующего тика периодического
        # флаша (раз в FLUSH_INTERVAL_SEC), иначе последние изменения между
        # последним тиком и остановкой процесса терялись бы при рестарте.
        # Флаги — канонически в lumen_chat_state (P2): множества общие объектом,
        # bool-флаги читаем из модуля, т.к. bot-привязки после выноса stale.
        if _dirty_chat_ids or lumen_chat_state._index_dirty or _pending_chat_deletions:
            _flush_state_now()
        if lumen_chat_state._quota_dirty:
            save_global_quota()
        await _close_sessions()

if __name__ == "__main__":
    asyncio.run(main())
