"""
Lumen — телеграм-бот на Gemini/OpenRouter, webhook-режим.
История диалога — 100 сообщений, TikTok через TikWM без водяных знаков,
генерация картинок через Pollinations, озвучка через Gemini TTS.
"""

from __future__ import annotations

# КРИТИЧНО (прод-инцидент, сентябрь 2026): прод запускается как `python bot.py`
# (`__main__`), а тесты — через `import bot`. Без этой строки любой отложенный
# `import bot` внутри функций (их десятки после распила P2) при прод-запуске
# ЗАНОВО выполнял весь bot.py как отдельный модуль: второе приложение, пустые
# chat_state/квоты и вечный bot=None → все апдейты уходили в 503, бот молчал.
# Алиас делает `import bot` везде тем же объектом, что и запущенный модуль.
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
    "OPENROUTER_API_KEY", "OPENROUTER_KEY", "ADMIN_SECRET_SEED",
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
    # LOG_LEVEL — раньше был захардкожен INFO везде (root+оба handler'а), из-за
    # чего оба существующих log.debug(...) в проекте не печатались никогда, ни в
    # каком окружении — единственный нетюнящийся через env уровень в проекте, где
    # даже таймауты в 15с настраиваются переменной. DEBUG остаётся дефолтом.
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
    # НАЙДЕНО ПРИ АУДИТЕ (4 сентября 2026, реальный воспроизведённый инцидент — см.
    # py-spy дамп стеков зависшего процесса): _setup_logging() вызывается больше
    # одного раза за процесс — сам модуль вызывает её один раз при импорте, а
    # test_setup_logging_respects_log_level_env/test_setup_logging_defaults_to_
    # info_when_unset в tests/test_bot_state.py вызывают её ещё 2 раза (проверка LOG_LEVEL).
    # Без остановки СТАРОГО листенера здесь каждый повторный вызов заводил ЕЩЁ
    # ОДИН QueueListener с ЕЩЁ ОДНИМ фоновым потоком-monitor'ом, читающим из ТОЙ
    # ЖЕ общей _LOG_QUEUE — несколько потоков-конкурентов дёргают dequeue() из
    # одной очереди одновременно. _stop_logging() (atexit) кладёт в очередь РОВНО
    # ОДИН sentinel и join()-ит только ПОСЛЕДНИЙ созданный листенер — если этот
    # единственный sentinel по гонке достаётся не тому потоку (а одному из более
    # старых, уже "осиротевших" от module-level ссылки, но всё ещё живых), поток,
    # который реально join()-ят, ждёт сигнала, который к нему никогда не придёт —
    # процесс зависает НАВСЕГДА уже ПОСЛЕ того, как pytest успел допечатать
    # "N passed" (сами тесты проходят, зависает только выход из процесса). Именно
    # так объясняется зафиксированный на практике недетерминированный (не каждый
    # прогон) хенг полного пакета тестов что локально, что в GitHub Actions —
    # обычный запуск строго одного файла/маркера, не затрагивающий эти два теста,
    # никогда не показывал проблему. Останавливаем предыдущий листенер ПЕРЕД тем,
    # как завести новый — гарантирует не больше одного живого monitor-потока на
    # эту очередь в любой момент времени, гонка исключена структурно.
    if _LOG_LISTENER is not None:
        with contextlib.suppress(Exception):
            _LOG_LISTENER.stop()
    _LOG_LISTENER = logging.handlers.QueueListener(_LOG_QUEUE, file_handler, console_handler, respect_handler_level=True)
    _LOG_LISTENER.start()

    root.addHandler(_LOG_QUEUE_HANDLER)
    logging.captureWarnings(True)
    for name in ("httpx", "google_genai", "aiohttp", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Единый логгер "bot" для всего проекта — logging.getLogger(__name__) здесь
    # давал "__main__" в проде (python -u bot.py), но "bot" при импорте тестами
    # (import bot) — рассинхрон с lumen_*.py, которые везде явно берут
    # logging.getLogger("bot") именно ради единого пространства имён логов.
    # Подтверждено реальным событием в Sentry с тегом logger=__main__.
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
    """Добавляет https://, если задан голый хост без схемы (см. реальный инцидент
    ниже) — общая логика для основного TELEGRAM_API_BASE_URL и резервных прокси
    из TELEGRAM_API_BASE_URL_FALLBACKS."""
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

# ИСПРАВЛЕНО (аудит техдолга, август 2026): раньше был ровно один настроенный прокси —
# единая точка отказа для ВСЕЙ исходящей и входящей связи с Telegram (см. README, раздел
# про блокировку датацентровых IP HF Spaces). TELEGRAM_API_BASE_URL_FALLBACKS — опциональный
# список через запятую (например второй прокси на Deno) — при срабатывании circuit breaker
# (см. _rotate_telegram_proxy ниже) бот переключается на следующий кандидат по кругу вместо
# того, чтобы просто ждать cooldown на единственном известном адресе. Если переменная не
# задана — список из одного элемента, поведение не меняется.
_TELEGRAM_PROXY_FALLBACKS = [
    _normalize_telegram_base_url(u) for u in os.getenv("TELEGRAM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]
_TELEGRAM_PROXY_CANDIDATES: list[str] = [TELEGRAM_API_BASE_URL] + [u for u in _TELEGRAM_PROXY_FALLBACKS if u != TELEGRAM_API_BASE_URL]
_telegram_proxy_idx = 0  # индекс текущего активного прокси в _TELEGRAM_PROXY_CANDIDATES
# Ротация мутирует два глобала сразу — под локом, иначе два concurrent-сбоя
# уводят индекс на два шага и пропускают кандидата (AUD-E-004).
_proxy_rotation_lock = asyncio.Lock()

# НАЙДЕНО ПРИ ОТЛАДКЕ (11-12 августа 2026, реальный инцидент): TikWM стабильно
# отвечает HTTP 403 с ПУСТЫМ телом на запросы с IP HF Spaces (см. историю правок
# в lumen_tiktok.py — троттлинг, ретраи и подмена заголовков не помогли, реальная
# причина — блокировка исходящего IP, а не что-либо, что чинится на нашей
# стороне). TIKWM_API_BASE_URL — опциональная база для прокси-запроса к TikWM
# (единый прокси с Telegram, см. README/proxy.ts — тот же принцип, что уже
# применяется для TELEGRAM_API_BASE_URL). Пусто по умолчанию — _fetch_tikwm_media_data
# в этом случае стучится в TikWM напрямую (два зеркала), как и раньше; если
# задано — идёт ОДНИМ запросом через прокси вместо прямого обращения к двум
# зеркалам напрямую (сам прокси уже решает, к какому реальному хосту TikWM
# стучаться — см. proxy/proxy.ts).
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
# Найдено при код-ревью: раньше этот дефолт вычислялся ОДИН РАЗ здесь, на старте
# модуля, из ЕЩЁ НЕ уточнённого BOT_USERNAME (env-заглушка "LumenAI_bot" по
# умолчанию) — до того, как try_setup() ниже реально спрашивает getMe и мог бы
# обновить настоящий юзернейм бота. Если владелец не задал BOT_USERNAME в env (или
# задал неверно) — заголовок HTTP-Referer к OpenRouter так и оставался бы со
# старым/неверным t.me/... адресом весь срок жизни процесса. _OPENROUTER_HTTP_
# REFERER_ENV_SET запоминает, была ли переменная задана ЯВНО владельцем — чтобы
# try_setup() ниже пересчитывал referer по свежему юзернейму, только если
# владелец сам не переопределил его в env (иначе не перетираем явную настройку).
_OPENROUTER_HTTP_REFERER_ENV_SET = bool(os.getenv("OPENROUTER_HTTP_REFERER", "").strip())
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", f"https://t.me/{BOT_USERNAME}").strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", BOT_USERNAME).strip()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Раньше у OpenRouter был свой отдельный лимит истории (30), меньший, чем у Gemini
# (100) — при переключении провайдера (/provider или /model) ощущалось резкое
# "обнуление" контекста разговора. Теперь история ОБЩАЯ (см. state["history"] в
# get_state) и лимит один и тот же для обоих провайдеров.
SHARED_HISTORY_MAX_LEN = 100

# ── Sentry (опционально) — персистентный трекинг ошибок между рестартами ──
# НАЙДЕНО: bot.log живёт на эфемерном диске контейнера HF Spaces и теряется при
# каждом редеплое (это верно даже с настроенным Upstash — тот покрывает только
# chat_state/quota, не логи), поэтому единственным способом узнать о падении
# было "/logs" вручную или жалоба пользователя (см. README, "Известные
# ограничения"). sentry_sdk по умолчанию патчит стандартный logging и сам ловит
# любой log.exception()/log.error() по всему проекту (их уже десятки — см.
# handle_tiktok/inline_draw/inline_tts/cmd_logs/_handle_message_core/
# global_error_handler) без единого изменения в местах вызова.
# Тот же принцип опциональности, что и у Upstash выше: SENTRY_DSN не задан —
# sentry_sdk.init() не вызывается вообще, поведение не меняется для тех, кто
# его не настроил.
def _redactable_secrets() -> tuple[str, ...]:
    """Единый список секретов для /logs и Sentry — раньше оба места вычищали
    только BOT_TOKEN/GEMINI_API_KEY/OPENROUTER_API_KEY, хотя WEBHOOK_SECRET/
    ADMIN_PANEL_KEY заявлены проектом как "никогда не логируются в plaintext"
    наравне с BOT_TOKEN, а UPSTASH_REDIS_REST_TOKEN даёт полный доступ ко всем
    сохранённым историям чатов. honey: имена читаются по значению на момент
    вызова — WEBHOOK_SECRET/ADMIN_PANEL_KEY/UPSTASH_REDIS_REST_TOKEN объявлены
    ниже по файлу, это безопасно для module-level globals в теле функции."""
    return _current_log_secrets()

def _sentry_scrub_secrets(event: dict, hint: dict) -> dict | None:
    """before_send-хук Sentry — вычищает секреты из события ПЕРЕД отправкой.
    Определена БЕЗУСЛОВНО (не только внутри `if SENTRY_DSN`), чтобы её можно
    было протестировать напрямую без настоящего DSN."""
    payload = json.dumps(event, default=str, ensure_ascii=False)
    for secret in _redactable_secrets():
        payload = payload.replace(secret, "<REDACTED>")
    return json.loads(payload)

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        before_send=_sentry_scrub_secrets,
        # Только трекинг ошибок — трейсинг производительности намеренно выключен
        # (traces_sample_rate=0), чтобы не тратить бесплатную квоту Sentry
        # (Developer-тир: 5000 событий/мес) на то, что тут отдельно не измеряется.
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
# Если сам прокси перед Telegram (tg-proxy на Deno Deploy) недоступен/приостановлен
# (например, исчерпан лимит бесплатного тарифа Deno — ответ вида "503 ... USAGE_EXCEEDED"),
# он вместо валидного JSON от Telegram отдаёт HTML/текстовую страницу ошибки. Ни aiogram,
# ни наш telegram_api_call не могут её распарсить — падают с JSONDecodeError на КАЖДЫЙ
# вызов, а исходящих вызовов в Telegram за секунду может быть десятки (reply, typing-экшен,
# get_file и т.д. на каждое входящее сообщение) — без выключателя это лавина одинаковых
# WARNING-строк в логах и бессмысленные повторные попытки в мёртвый прокси. См. _tg_call/
# telegram_api_call и _looks_like_proxy_garbage ниже. TELEGRAM_PROXY_COOLDOWN_SEC — на сколько
# секунд отключаем реальные сетевые попытки после первой пойманной такой ошибки.
TELEGRAM_PROXY_COOLDOWN_SEC = float(os.getenv("TELEGRAM_PROXY_COOLDOWN_SEC", "20"))
# В отличие от ask_gemini/ask_openrouter_text (которые ограничены ROUTE_TOTAL_
# BUDGET_SEC на весь маршрут), у стриминга раньше не было НИКАКОГО таймаута вокруг
# ожидания следующего куска — генуинно подвисший (не упавший с исключением, а
# просто переставший присылать куски) стрим мог держать лок чата (_chat_locks)
# бесконечно. Теперь каждое ожидание СЛЕДУЮЩЕГО куска (для ЛЮБОГО провайдера —
# Gemini или OpenRouter, см. _run_streaming_reply) ограничено этим таймаутом —
# если тишина затянулась дольше него, поднимается TimeoutError, которую функция
# и так уже умеет корректно обрабатывать.
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
# Раньше (до перехода на роутер) при таймауте/503/500 бот ретраил ОДНУ и ту же
# модель 2-3 раза с экспоненциальной задержкой, и только потом переключался на
# следующую в цепочке — именно это было причиной ответов по 2+ минуты при
# малейшей нестабильности API (см. историю: несколько моделей подряд по
# 3 попытки × до 45с каждая). Теперь ретраев ОДНОЙ модели нет вообще: любая
# ошибка (таймаут, 429, 503/500, что угодно ещё) — сразу переход к следующей
# модели в маршруте. ROUTE_MODEL_TIMEOUT_SEC — сколько ждём ОДНУ попытку одной
# модели, прежде чем считать её неудачной и пробовать следующую.
ROUTE_MODEL_TIMEOUT_SEC = float(os.getenv("ROUTE_MODEL_TIMEOUT_SEC", "22"))
# ROUTE_TOTAL_BUDGET_SEC — общий бюджет времени на ВЕСЬ маршрут одного сообщения,
# включая ОБА провайдера (Gemini и OpenRouter), если маршрут предполагает
# резервный переход между ними. Без этого потолка каскадный сбой сразу у многих
# моделей/провайдеров мог бы растянуть один ответ на несколько минут, всё это
# время удерживая лок чата (_chat_locks). При превышении бюджета дальнейшие
# попытки прекращаются и пользователь получает честное "сейчас всё перегружено"
# вместо тихого зависания.
ROUTE_TOTAL_BUDGET_SEC = float(os.getenv("ROUTE_TOTAL_BUDGET_SEC", "40"))
# DRAW_TOTAL_BUDGET_SEC — тот же принцип, что и ROUTE_TOTAL_BUDGET_SEC выше, но для
# фолбэк-цепочки генерации изображений (см. inline_draw). НАЙДЕНО ПРИ КОД-РЕВЬЮ
# (28 августа 2026): в отличие от текстового роутинга, у /draw не было ВООБЩЕ
# никакого общего бюджета времени — каждый вызов _pollinations_text_to_image ждёт до 90с
# (см. aiohttp.ClientTimeout в _pollinations_generate, lumen_images.py), а моделей
# в POLLINATIONS_IMAGE_MODELS пять. Если Pollinations.ai лежит целиком, пользователь мог
# ждать до ~7.5 минут, прежде чем увидеть любую ошибку — статусное сообщение
# "Генерирую изображение" всё это время просто висело. 120с — достаточно на одну
# полную попытку (90с) плюс запас на вторую, но ограничивает худший случай вдвое
# от одного медленного таймаута, а не в разы от их числа.
DRAW_TOTAL_BUDGET_SEC = float(os.getenv("DRAW_TOTAL_BUDGET_SEC", "120"))
# INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC — сколько main() при остановке ждёт штатного
# завершения fire-and-forget задач обработки апдейтов (см. _inflight_tasks/
# _drain_inflight_tasks) перед тем, как отменить оставшиеся. Найдено по реальному
# инциденту в Sentry (LUMEN-2, "Task was destroyed but it is pending!") — без
# этого такие задачи могли быть уничтожены event loop'ом прямо посреди сетевого
# вызова (например bot.send_message(...)) при SIGTERM/редеплое.
INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC = float(os.getenv("INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC", "10"))
TG_MAX_LEN = 4096
# Telegram Bot API ограничивает загрузку файлов, отправляемых ботом (upload, а не
# по file_id/URL), 50 МБ — используется в _tiktok_video_candidates/handle_tiktok
# ниже, чтобы заранее пропускать заведомо слишком большой вариант качества видео,
# не тратя время и трафик на скачивание файла, который Telegram всё равно отклонит.
TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# TikTok официально разрешает до 35 фото/слайдов в одном посте формата "слайдшоу"
# (photo mode) — см. справку TikTok. sendMediaGroup при этом жёстко ограничен 10
# элементами ЗА ОДИН вызов — это ограничение Telegram Bot API, а не наше. Чтобы
# реально доставить ВЕСЬ пост (а не только первые 10, как было раньше), слайды
# делятся на группы по TELEGRAM_MEDIA_GROUP_CHUNK и отправляются несколькими
# последовательными вызовами sendMediaGroup — см. handle_tiktok.
TIKTOK_SLIDESHOW_MAX_ITEMS = 35
TELEGRAM_MEDIA_GROUP_CHUNK = 10
# TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY — НАЙДЕНО ПРИ КОД-РЕВЬЮ (28 августа 2026):
# в отличие от скачивания слайдов (см. комментарий у asyncio.gather в handle_tiktok —
# то уже покрыто connector limit=40 в _get_http_session), пробинг видео-слайдов
# запускает НАСТОЯЩИЕ os-подпроцессы (ffprobe + ffmpeg на каждый видео-слайд) без
# единого ограничения — слайдшоу с несколькими видео-слайдами могло бы дать
# заметный всплеск CPU-нагрузки одновременно на контейнере HF Spaces с
# ограниченными ресурсами. Лимит небольшой (не 35, как для сетевых скачиваний) —
# это реальные CPU-тяжёлые процессы, а не ожидание сетевого I/O.
TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY = int(os.getenv("TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY", "4"))
_tiktok_probe_semaphore = asyncio.Semaphore(TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY)
# TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY — НАЙДЕНО ПРИ АУДИТЕ TikTok-функций (4 сентября
# 2026): слайды слайдшоу (до TIKTOK_SLIDESHOW_MAX_ITEMS=35) скачиваются через
# asyncio.gather БЕЗ единого ограничения конкурентности — тот же класс проблемы,
# что уже был найден и исправлен для CPU-тяжёлого пробинга (см. комментарий у
# TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY выше), только здесь это не CPU, а
# соединения общей aiohttp-сессии (_get_http_session, connector limit=40 — ОБЩИЙ
# на весь процесс, а не только на TikTok). Один слайдшоу-пост из 35 слайдов мог
# бы разом занять почти весь пул соединений и создать head-of-line blocking для
# несвязанных запросов из других чатов (Gemini/OpenRouter/Pollinations/другие
# TikTok-ссылки тоже используют этот же session). Небольшой лимит (не 35) —
# оставляет запас пула для остального трафика бота, при этом всё ещё заметно
# быстрее полностью последовательного скачивания.
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

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА (разбиение bot.py на модули): класс выключателя, детектор
# "прокси вернул не-JSON", конфигурация TCPConnector, IPv4-сессия aiogram и кэш общей
# aiohttp-сессии для telegram_api_call — всё это самодостаточно (не мутирует
# TELEGRAM_API_BASE_URL/bot/BOT_TOKEN) и вынесено в lumen_telegram_transport.py. Сама
# оркестрация (_tg_call/telegram_api_call/_rotate_telegram_proxy/_handle_proxy_failure)
# остаётся здесь — она читает И мутирует TELEGRAM_API_BASE_URL/bot, которые в этом файле
# используются ещё в добром десятке несвязанных мест (/diag, скачивание файлов, main()),
# так что вынос обошёлся бы дороже, чем стоит (см. докстринг lumen_telegram_transport.py).
from lumen_telegram_transport import (
    _TelegramProxyCircuitBreaker,
    IPv4AiohttpSession,
)

TELEGRAM_PROXY_TRIP_THRESHOLD = int(os.getenv("TELEGRAM_PROXY_TRIP_THRESHOLD", "3"))
_tg_proxy_breaker = _TelegramProxyCircuitBreaker(cooldown_sec=TELEGRAM_PROXY_COOLDOWN_SEC, trip_threshold=TELEGRAM_PROXY_TRIP_THRESHOLD)

# конвертация markdown в html, утилиты json

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: вся логика конвертации markdown/LaTeX/таблиц/
# маркеров списков в Telegram HTML вынесена в отдельный модуль lumen_formatting.py —
# это чистые функции над строками без единой зависимости от Telegram/Gemini/
# OpenRouter/рантайм-состояния бота, самый безопасный кандидат на выделение из
# монолитного bot.py. Публичные имена и поведение не изменились — импортируется
# напрямую, чтобы `bot._md_to_html(...)` продолжал работать ровно как раньше.
#
# Только `_md_to_html` реально нужна здесь (используется в коде bot.py) —
# остальные внутренние хелперы (`_scrub_latex`/`_normalize_bullet_markers`/
# `_TABLE_SEP_RE`/`_LATEX_SYMBOL_MAP` и т.п.) нужны только САМОЙ `_md_to_html`
# внутри lumen_formatting.py; тесты на них теперь тоже импортируют
# lumen_formatting напрямую (см. test_lumen_formatting.py), а не через `bot.X` —
# раньше `_scrub_latex`/`_normalize_bullet_markers` были ре-экспортированы здесь
# именно ради старых тестов на `bot.X`, но с переездом тестов на прямой импорт
# модуля этот ре-экспорт стал мёртвым (см. аудит техдолга, 26 августа 2026) и
# убран вместе с соответствующим `__all__`.
from lumen_formatting import _split_text_chunks

_PRUNE_SENTINEL = object()

def _json_prune_defaults(val: Any) -> Any:
    # Очистка дефолтных служебных значений
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
# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: конфигурация моделей и логика построения маршрута
# (GEMINI_MODELS, TEXT_MODEL_ORDER, единый реестр "нездоровых" моделей
# _OR_MODEL_HEALTH/_ROUTER_EXCLUDED_OR_MODELS, порядок моделей OpenRouter,
# цепочки Gemini, эвристики "тяжёлый запрос?"/"нужна свежая информация?" и сами
# _build_route/_or_route/_gemini_route) вынесены в lumen_router_config.py — это
# чистые конфигурация+функции принятия решения без единого обращения к Telegram/
# Gemini/OpenRouter API, поэтому безопасный кандидат на отдельный модуль (в
# отличие от ask_gemini/_run_route ниже, которые реально ИСПОЛНЯЮТ маршрут и
# остаются здесь). Импорт стоит именно тут (там, где раньше физически начиналось
# определение GEMINI_MODELS) для консистентности с историей файла, хотя строгой
# необходимости в этом больше нет: _LEAK_LITERAL_STRINGS (которая раньше требовала
# GEMINI_MODELS/TEXT_MODEL_ORDER на уровне модуля именно в этой точке файла)
# теперь целиком строится внутри lumen_security.py, а не здесь.
# Публичные имена и поведение не изменились. Импортируются только реально
# используемые здесь (в коде bot.py или напрямую в тестах через `bot.X`) имена —
# например, `_gemini_route`/`_OR_HEAVY_ORDER`/`TEXT_MODEL_ORDER` нужны только
# САМОЙ `_build_route` внутри lumen_router_config.py, а не bot.py.
from lumen_router_config import (
    DEFAULT_GEMINI_MODEL,
    _check_unconfirmed_model_quotas,
    _check_temporary_free_models_expiry,
    _check_scheduled_removals_due,
    _OR_LIGHT_ORDER,
    _OR_HEAVY_ORDER,
    _OR_VISION_ORDER,
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

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: форма одного элемента chat_state[chat_id] раньше
# нигде не была описана явно — она собиралась по кусочкам из трёх разных мест
# (get_state/_restore_single_chat/_serialize_chat_state), и чтобы понять "из чего
# вообще состоит состояние чата", нужно было читать все три. ChatState — чисто
# типовая аннотация (TypedDict), НЕ меняет поведение в рантайме: chat_state[cid]
# остаётся обычным dict, никакой валидации здесь не добавляется — это только
# документация формы для статических проверок типов и читаемости.

# Буферы альбомов/медиа-групп
_mg_buffers: dict[str, list[Message]] = {}
_mg_tasks: dict[str, asyncio.Task] = {}

# НАЙДЕНО ПРИ АУДИТЕ ЛОГИРОВАНИЯ (Sentry LUMEN-2: "Task was destroyed but it is
# pending!", logger=asyncio): fire-and-forget таски обработки входящих апдейтов
# (webhook_handler -> _process_raw_update, буферы медиа-групп -> _mg_tasks) нигде
# не собирались в единый набор — main() при остановке отменял только startup_task/
# flush_task, а эти задачи (внутри которых реальные bot.send_message(...) и т.п.)
# могли быть уничтожены event loop'ом прямо посреди сетевого вызова при SIGTERM/
# редеплое, без единого шанса штатно завершиться или хотя бы залогировать себя.
# _inflight_tasks — общий набор таких задач, done_callback снимает таску из набора
# сама (без отдельной периодической чистки); используется _drain_inflight_tasks
# в main() при остановке (см. там же).
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

# ИСПРАВЛЕНО (аудит техдолга, август 2026): раньше WEBHOOK_SECRET/ADMIN_PANEL_KEY
# выводились ИСКЛЮЧИТЕЛЬНО из BOT_TOKEN — компрометация одного токена бота мгновенно
# компрометировала оба производных секрета разом, и ни один нельзя было ротировать
# независимо (только вместе со сменой самого BOT_TOKEN у @BotFather, что рвёт webhook).
# ADMIN_SECRET_SEED — опциональная независимая соль: если задана, оба секрета выводятся
# из неё, а не из BOT_TOKEN, и можно сменить только её, не трогая токен бота. Если не
# задана — тихий откат на прежнее поведение (соль = BOT_TOKEN), никаких изменений для
# тех, кто её не настраивал.
# НАЙДЕНО ПРИ СЕКЬЮРИТИ-РЕВЬЮ: последний запасной вариант раньше был литеральной строкой
# "default" — этот репозиторий публичный, поэтому в сценарии "ADMIN_SECRET_SEED не задан
# И BOT_TOKEN пуст" (например, ошибка конфигурации) WEBHOOK_SECRET/ADMIN_PANEL_KEY стали
# бы ЗАРАНЕЕ ИЗВЕСТНЫМИ КОНСТАНТАМИ, вычислимыми любым, кто читает этот исходник — секрет,
# который не секрет. На практике без валидного BOT_TOKEN сам процесс всё равно не поднимется
# (main() падает на Bot(token=BOT_TOKEN, ...) ещё до старта uvicorn.serve(), см. main() ниже) —
# поэтому этот путь маловероятен в реальном продакшене, но это защита по глубине "на всякий
# случай": secrets.token_hex(32) даёт непредсказуемый секрет на время жизни процесса вместо
# захардкоженной в открытом коде строки.
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

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА (разбиение bot.py на модули): клиент Upstash REST API,
# ветвление backend'а (Upstash/локальный файл), сериализация одного чата в JSON-снимок
# и сохранение/удаление ОДНОЙ записи чата вынесены в lumen_state_storage.py — эти
# функции не заводят собственных module-level globals, завязанных на chat_state/
# GLOBAL_QUOTA (см. докстринг модуля). chat_state/GLOBAL_QUOTA и то, ЧТО считается
# "грязным" и когда сбрасывается — по-прежнему здесь: это состояние читается/
# мутируется из ~30 несвязанных мест по всему файлу, выносить его означало бы не
# разделение ответственности, а искусственное разрывание единого куска состояния.
#
# `_upstash_*`/`_storage_*`/`_chat_storage_path`/`_save_chat_to_storage`/
# `_delete_chat_storage` ниже — тонкие обёртки с ТЕМИ ЖЕ именами и (за вычетом
# внутреннего StorageConfig) сигнатурами, что были раньше: собирают свежий
# StorageConfig из текущих значений UPSTASH_REDIS_REST_URL/_TOKEN/USE_UPSTASH/
# _CHATS_DIR (в т.ч. подменённых в тестах через `bot.UPSTASH_REDIS_REST_URL = ...`/
# `bot._CHATS_DIR = ...`) на КАЖДЫЙ вызов и прокидывают в lumen_state_storage —
# публичный интерфейс и поведение не изменились.
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
    "_run_streaming_reply",
    "_try_gemini_streaming",
    "_try_openrouter_streaming",
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
    "_or_request",
    "_or_extract_text",
    "_is_account_wide_or_rate_limit",
    "_probe_or_model_liveness",
    "_or_chat_completion_with_fallback",
    "ask_openrouter_text",
    "ask_openrouter_multimodal",
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
# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА (разбиение bot.py на модули): вся эта секция (каталог
# моделей Pollinations, автоматический выбор модели по промпту, сам вызов
# Pollinations.ai) вынесена в lumen_images.py — не пишет в chat_state/GLOBAL_QUOTA,
# не зовёт Telegram API и не зависит от глобальных bot/client, самый изолированный
# кандидат из пяти намеченных. Единственное отличие от прежнего кода:
# _pollinations_generate/_pollinations_text_to_image теперь принимают уже готовую aiohttp-сессию
# параметром (см. докстринг модуля) — раньше сессия получалась неявно через
# _get_http_session() внутри самой функции, что означало бы либо тянуть этот геттер
# в новый модуль, либо заводить там свой отдельный источник сессий; вызывающий код
# (inline_draw ниже) теперь сам получает сессию и передаёт её. Публичные имена и
# остальное поведение не изменились.
# _image_model_label здесь больше не импортируется (сентябрь 2026): бот не
# показывает названия моделей генерации ни в статусе, ни в подписи — см.
# ИДЕНТИЧНОСТЬ в system_prompt.py. Сама функция живёт в lumen_images.py.
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
# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: детекторы утечки идентичности (_detect_identity_leak/
# _scrub_identity_leak/_detect_injected_payload_echo) и входной префильтр промт-
# инъекций (_looks_like_injection_probe) вынесены в lumen_security.py — чистые
# функции над строками (плюс регэкспы/константы), не зависящие от Telegram/рантайм-
# состояния бота. Импортируются напрямую — публичные имена и поведение (включая
# логирование через тот же логгер "bot", см. lumen_security.py) не изменились.
# Только реально используемые здесь имена импортируются явно — регэкспы
# (`_IDENTITY_LEAK_RE`/`_INJECTION_PROBE_RE`/`_INJECTED_PAYLOAD_ECHO_RE` и
# составляющие их `_LEAK_BRAND_TOKENS`/`_LEAK_LITERAL_STRINGS`) нужны только
# самим детекторам внутри lumen_security.py, а не коду bot.py.
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
    _or_request,
    _or_extract_text,
    _is_account_wide_or_rate_limit,
    _probe_or_model_liveness,
    _or_chat_completion_with_fallback,
    ask_openrouter_text,
    ask_openrouter_multimodal,
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

# Точка подмены для тестов (тот же приём, что и у bot._get_http_session/bot.
# _openrouter_stream_pieces и т.п. в этом файле) — реальный await asyncio.sleep()
# в фазе "довывода" (см. _run_streaming_reply) не нужен ни в одном тесте и заметно
# замедлил бы весь сьют без единой пользы; tests/conftest.py безусловно патчит эту
# ссылку на no-op для каждого теста.
_typing_sleep = asyncio.sleep

# Отдельная точка подмены для анимации точек (см. _tick_waiting_dots ниже) —
# НАМЕРЕННО не покрыта autouse-фикстурой tests/conftest.py: если бы она была no-op,
# тикер в каждом стриминг-тесте успевал бы наставить лишних правок до прихода
# мгновенного фейкового куска и сломал бы все проверки последовательностей
# правок. В проде — обычный asyncio.sleep; в тестах анимации патчится явно.
_dots_sleep = asyncio.sleep


# Стриминг живёт в lumen_streaming.py (P2): здесь только реэкспорт имён,
# чтобы `bot.X` в тестах и `_run_route` не менялись.
from lumen_streaming import (
    _tick_waiting_dots,
    _pieces_with_waiting_feedback,
    _gemini_stream_pieces,
    _openrouter_stream_pieces,
    _run_streaming_reply,
    _try_gemini_streaming,
    _try_openrouter_streaming,
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
    # 1. Прямое упоминание бота через @username
    if f"@{BOT_USERNAME}".lower() in t.lower():
         return True
    # 2. Ответ на сообщение бота в группе
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
# Полностью заменяет ручной выбор через /model и /provider (обе команды удалены).
# Пользователь никогда явно не выбирает ни провайдера, ни модель — на КАЖДОЕ
# сообщение маршрут строится заново, исходя из того, что реально требуется для
# ответа: наличие вложений/ссылок (детерминированно, из самого сообщения) и
# грубая эвристическая оценка сложности/нужды в свежей информации (без
# обращения к LLM — классификация отдельным вызовом модели тратила бы ровно ту
# же дефицитную квоту, которую роутер должен экономить).
#
# Ключевое архитектурное решение: Gemini — единственный провайдер с реальным
# доступом к поиску в интернете, чтению сайтов по ссылке (url_context) и
# разбору YouTube-видео по ссылке (file_uri). Квота Gemini (у флагмана
# gemini-3.5-flash — всего 20 запросов/сутки по дашборду AI Studio) — самый
# дефицитный ресурс бота, поэтому Gemini используется ТОЛЬКО когда сообщение
# реально требует одну из этих трёх возможностей. Все остальные (и
# значительно более частые) запросы — без вложений, без ссылок, без явных
# признаков нужды в свежих данных — обслуживаются бесплатными моделями
# OpenRouter, у которых лимит намного мягче и которые не тратят вообще ничего
# из бюджета Gemini. Каждый провайдер выступает резервом для другого, если его
# собственная цепочка кандидатов откажет целиком — раньше (при ручном выборе
# через /model и /provider) переход между провайдерами был намеренно запрещён
# ("выбрали провайдера — работает только его цепочка"), но это ограничение
# имело смысл только пока выбор был явным решением пользователя; при
# автоматическом роутинге такого выбора не существует, и честная эскалация в
# другой провайдер лучше, чем отказ там, где ответ в принципе можно было дать.


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
    # Полные WEBHOOK_SECRET/ADMIN_PANEL_KEY больше НЕ печатаются в логи при каждом
    # старте (см. критическую находку код-ревью — эти строки попадали в скриншоты/
    # чаты наравне с остальными логами, а тот, у кого есть ADMIN_PANEL_KEY, получает
    # полный доступ к /diag и /webhook_url). Показываем только урезанный "отпечаток"
    # для сверки между рестартами; полные значения — через GET /admin_keys с заголовком
    # Authorization (гейтится самим BOT_TOKEN — ИСПРАВЛЕНО при повторном код-ревью:
    # раньше токен передавался как ?bot_token=... в URL, что попадало в access-логи
    # прокси/историю браузера; см. _check_bot_token_auth).
    log.info('[webhook] WEBHOOK_SECRET (fingerprint): %s', _redact_secret(WEBHOOK_SECRET))
    log.info(
        '[admin] Full keys (WEBHOOK_SECRET/ADMIN_PANEL_KEY): curl -H "Authorization: Bearer <your BOT_TOKEN>" https://%s/admin_keys',
        space_host,
    )

    commands = [
        BotCommand(command="start", description=_lang_t(DEFAULT_LANG, "cmd_desc_start")),
        BotCommand(command="reset", description=_lang_t(DEFAULT_LANG, "cmd_desc_reset")),
        BotCommand(command="draw", description=_lang_t(DEFAULT_LANG, "cmd_desc_draw")),
        BotCommand(command="tts", description=_lang_t(DEFAULT_LANG, "cmd_desc_tts")),
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
                BotCommand(command="reset", description=_lang_t(code, "cmd_desc_reset")),
                BotCommand(command="draw", description=_lang_t(code, "cmd_desc_draw")),
                BotCommand(command="tts", description=_lang_t(code, "cmd_desc_tts")),
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
            # Полный WEBHOOK_SECRET сюда больше НЕ подставляется (см. критическую
            # находку код-ревью про секреты, печатавшиеся в лог целиком при каждом
            # старте). Готовая ссылка с реальным секретом доступна через уже
            # существующий /webhook_url — ИСПРАВЛЕНО (аудит техдолга): раньше вызывался
            # как GET .../webhook_url?key=<ADMIN_PANEL_KEY>, теперь (как и /admin_keys)
            # требует заголовок Authorization: Bearer <ADMIN_PANEL_KEY> — сам ADMIN_PANEL_KEY
            # при необходимости получить через curl -H "Authorization: Bearer <BOT_TOKEN>"
            # .../admin_keys (см. _check_bot_token_auth).
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
    # НАЙДЕНО ПРИ КОД-РЕВЬЮ: _check_temporary_free_models_expiry()/_check_unconfirmed_
    # model_quotas() вызывались ТОЛЬКО один раз при старте (см. вызов в начале этой же
    # функции). Если контейнер работает без редеплоя достаточно долго, чтобы промо-акция
    # истекла УЖЕ ПОСЛЕ старта (ровно так и вышло с tencent/hy3:free — истекла спустя пару
    # дней после последнего рестарта) — предупреждение не всплывёт в логах до следующего
    # рестарта. Используем уже существующий часовой цикл, чтобы дополнительно
    # перепроверять обе функции раз в сутки, без отдельного нового фонового таска.
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
        # Даём незавершённым апдейтам (webhook/медиа-группы) шанс закончиться
        # штатно ДО финального сброса состояния и закрытия сессий — иначе их
        # правки chat_state рисковали не попасть в _flush_state_now ниже, а сами
        # сетевые вызовы внутри них — быть оборваны на середине (см. LUMEN-2).
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
