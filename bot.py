"""
Lumen — телеграм-бот на Gemini/OpenRouter, webhook-режим.
История диалога — 100 сообщений, TikTok через TikWM без водяных знаков,
генерация картинок через Pollinations, озвучка через Gemini TTS.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import hashlib
import hmac
import html as _html_mod
import json
import logging
import logging.handlers
import os
import queue
import re
import socket
import sys
import tempfile
import time
from pathlib import Path
from collections import deque
from datetime import date, datetime
import mimetypes
from typing import Any, TypedDict
import urllib.request as _urllib_request
from urllib.parse import urlparse

import aiohttp
import sentry_sdk
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramEntityTooLarge
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    FSInputFile,
    CallbackQuery,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
    Update,
)
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

from system_prompt import SYSTEM_PROMPT

# логирование

LOG_FILE_PATH = Path(os.getenv("BOT_LOG_PATH", "/app/bot.log"))
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
_LOG_QUEUE: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
_LOG_QUEUE_HANDLER = logging.handlers.QueueHandler(_LOG_QUEUE)
_LOG_LISTENER: logging.handlers.QueueListener | None = None

def _setup_logging() -> logging.Logger:
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

    global _LOG_LISTENER
    _LOG_LISTENER = logging.handlers.QueueListener(_LOG_QUEUE, file_handler, console_handler, respect_handler_level=True)
    _LOG_LISTENER.start()

    root.addHandler(_LOG_QUEUE_HANDLER)
    logging.captureWarnings(True)
    for name in ("httpx", "google_genai", "aiohttp", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logging.getLogger(__name__)

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
    url = url.strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        log.warning('[setup] Telegram proxy URL given without a scheme (%r) — adding https:// automatically.', url)
        url = "https://" + url
    return url

TELEGRAM_API_BASE_URL = _normalize_telegram_base_url(os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org"))
log.info("[setup] Using Telegram API Base URL: %s", TELEGRAM_API_BASE_URL)

_TELEGRAM_PROXY_FALLBACKS = [
    _normalize_telegram_base_url(u) for u in os.getenv("TELEGRAM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]
_TELEGRAM_PROXY_CANDIDATES: list[str] = [TELEGRAM_API_BASE_URL] + [u for u in _TELEGRAM_PROXY_FALLBACKS if u != TELEGRAM_API_BASE_URL]
_telegram_proxy_idx = 0

TIKWM_API_BASE_URL = os.getenv("TIKWM_API_BASE_URL", "").strip().rstrip("/")
_TIKWM_API_BASE_URL_FALLBACKS = [
    u.strip().rstrip("/") for u in os.getenv("TIKWM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]

def _tikwm_proxy_candidates() -> list[str]:
    if not TIKWM_API_BASE_URL:
        return [""]
    candidates = [TIKWM_API_BASE_URL] + [u for u in _TIKWM_API_BASE_URL_FALLBACKS if u != TIKWM_API_BASE_URL]
    return candidates

BOT_USERNAME = os.getenv("BOT_USERNAME", "LumenAI_bot").strip().lstrip("@")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY") or "").strip()
_OPENROUTER_HTTP_REFERER_ENV_SET = bool(os.getenv("OPENROUTER_HTTP_REFERER", "").strip())
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", f"https://t.me/{BOT_USERNAME}").strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", BOT_USERNAME).strip()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
SHARED_HISTORY_MAX_LEN = 100

def _redactable_secrets() -> tuple[str, ...]:
    return tuple(s for s in (
        BOT_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY, _ADMIN_SECRET_SEED,
        WEBHOOK_SECRET, ADMIN_PANEL_KEY, UPSTASH_REDIS_REST_TOKEN,
    ) if s)

def _sentry_scrub_secrets(event: dict, hint: dict) -> dict | None:
    payload = json.dumps(event, default=str, ensure_ascii=False)
    for secret in _redactable_secrets():
        payload = payload.replace(secret, "<REDACTED>")
    return json.loads(payload)

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        before_send=_sentry_scrub_secrets,
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
TELEGRAM_GET_FILE_TIMEOUT = float(os.getenv("TELEGRAM_GET_FILE_TIMEOUT", "15"))
TG_PROXY_COOLDOWN_SEC = float(os.getenv("TG_PROXY_COOLDOWN_SEC", "20"))
STREAM_CHUNK_TIMEOUT_SEC = float(os.getenv("STREAM_CHUNK_TIMEOUT_SEC", "30"))
STREAM_EDIT_MIN_INTERVAL_SEC = float(os.getenv("STREAM_EDIT_MIN_INTERVAL_SEC", "1.2"))
STREAM_TYPING_TICK_SEC = float(os.getenv("STREAM_TYPING_TICK_SEC", "0.5"))
STREAM_TYPING_MAX_CATCHUP_TICKS = int(os.getenv("STREAM_TYPING_MAX_CATCHUP_TICKS", "6"))
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "800"))
_PROCESS_START_MONOTONIC = time.monotonic()
ROUTE_MODEL_TIMEOUT_SEC = float(os.getenv("ROUTE_MODEL_TIMEOUT_SEC", "22"))
ROUTE_TOTAL_BUDGET_SEC = float(os.getenv("ROUTE_TOTAL_BUDGET_SEC", "40"))
TG_MAX_LEN = 4096
TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
TIKTOK_SLIDESHOW_MAX_ITEMS = 35
TELEGRAM_MEDIA_GROUP_CHUNK = 10

MAX_CHAT_LIMIT = 5000
PRUNED_CHAT_TARGET = 4500
MAX_CHAT_HISTORY_LEN = 100
MAX_MEDIA_RECENT_IDS = 8

RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SEC = float(os.getenv("RATE_LIMIT_WINDOW_SEC", "30"))
user_rate_limits: dict[int, list[float]] = {}

def _cleanup_rate_limit_dict() -> None:
    now = time.time()
    stale = [uid for uid, ts in user_rate_limits.items() if not ts or now - ts[-1] > 3600]
    for uid in stale:
        user_rate_limits.pop(uid, None)

bot: Bot = None
dp = Dispatcher()
client: genai.Client = None

_http_session: aiohttp.ClientSession | None = None
_chat_locks: dict[int, asyncio.Lock] = {}

from lumen_telegram_transport import (
    _TelegramProxyCircuitBreaker,
    _looks_like_proxy_garbage,
    IPv4AiohttpSession,
    get_telegram_session as _lumen_get_telegram_session,
    close_telegram_session as _lumen_close_telegram_session,
)

TG_PROXY_TRIP_THRESHOLD = int(os.getenv("TG_PROXY_TRIP_THRESHOLD", "3"))
_tg_proxy_breaker = _TelegramProxyCircuitBreaker(cooldown_sec=TG_PROXY_COOLDOWN_SEC, trip_threshold=TG_PROXY_TRIP_THRESHOLD)

async def _get_telegram_session() -> aiohttp.ClientSession:
    return await _lumen_get_telegram_session(TELEGRAM_REQUEST_TIMEOUT)

async def _rotate_telegram_proxy() -> bool:
    global TELEGRAM_API_BASE_URL, _telegram_proxy_idx
    if len(_TELEGRAM_PROXY_CANDIDATES) < 2:
        return False
    _telegram_proxy_idx = (_telegram_proxy_idx + 1) % len(_TELEGRAM_PROXY_CANDIDATES)
    new_url = _TELEGRAM_PROXY_CANDIDATES[_telegram_proxy_idx]
    old_url = TELEGRAM_API_BASE_URL
    TELEGRAM_API_BASE_URL = new_url
    log.warning('[telegram] Switching to fallback proxy: %s -> %s', old_url, new_url)
    if bot is not None:
        old_session = bot.session
        bot.session = IPv4AiohttpSession(api=TelegramAPIServer.from_base(new_url))
        with contextlib.suppress(Exception):
            await old_session.close()
    return _telegram_proxy_idx != 0

async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            connector=aiohttp.TCPConnector(family=socket.AF_INET, limit=40, ttl_dns_cache=300),
        )
    return _http_session

async def _close_sessions() -> None:
    await _lumen_close_telegram_session()
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    if bot is not None and hasattr(bot, "session") and bot.session:
        await bot.session.close()

from lumen_formatting import _md_to_html

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

async def _handle_proxy_failure(context: str) -> None:
    tripped = _tg_proxy_breaker.note_failure()
    if not tripped:
        log.warning(
            '[telegram] Proxy unavailable during %s (%d/%d in a row, circuit breaker not tripped yet).',
            context, _tg_proxy_breaker.consecutive_failures, _tg_proxy_breaker.trip_threshold,
        )
        return
    lap_not_done = await _rotate_telegram_proxy()
    if lap_not_done:
        _tg_proxy_breaker.consecutive_failures = 0
        log.warning(
            '[telegram] Proxy unavailable during %s %d time(s) in a row — switching to fallback address %s without a pause.',
            context, _tg_proxy_breaker.trip_threshold, TELEGRAM_API_BASE_URL,
        )
        return
    await _notify_owner(
        f"⚠️ Telegram-прокси недоступен при {context} ({_tg_proxy_breaker.consecutive_failures} сбоев "
        f"подряд, резервные адреса тоже не помогли). Пауза {_tg_proxy_breaker.cooldown_sec:.0f}с. "
        f"Активный адрес: {TELEGRAM_API_BASE_URL}"
    )
    _tg_proxy_breaker.trip()
    log.warning(
        '[telegram] Proxy unavailable during %s %d time(s) in a row (threshold %d) — pausing for %.0fs. Check availability of %s.',
        context, _tg_proxy_breaker.consecutive_failures, _tg_proxy_breaker.trip_threshold, _tg_proxy_breaker.cooldown_sec,
        TELEGRAM_API_BASE_URL,
    )

async def _tg_call(method: Any, *args: Any, call_timeout: float | None = None, retries: int = 1, **kwargs: Any) -> Any:
    now = time.monotonic()
    if _tg_proxy_breaker.is_down(now):
        _tg_proxy_breaker.log_still_down_if_due(now)
        return None
    last_exc = None
    timeout_val = call_timeout if call_timeout is not None else 35.0
    for attempt in range(retries + 1):
        try:
            result = await asyncio.wait_for(method(*args, **kwargs), timeout=timeout_val)
            _tg_proxy_breaker.note_success()
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    if last_exc is not None and "message is not modified" in str(last_exc).lower():
        _tg_proxy_breaker.note_success()
        return None
    if last_exc is not None and _looks_like_proxy_garbage(last_exc):
        await _handle_proxy_failure("вызове (не-JSON ответ прокси)")
        return None
    log.warning("[telegram] call failed: %s", last_exc)
    return None

async def telegram_api_call(method: str, payload: dict, *, request_timeout: float | None = None) -> Any:
    if _tg_proxy_breaker.is_down(time.monotonic()):
        raise RuntimeError(f"Telegram API {method}: прокси сейчас помечен недоступным (см. предыдущие [telegram] предупреждения), не дёргаю сеть повторно.")
    url = f"{TELEGRAM_API_BASE_URL}/bot{BOT_TOKEN}/{method}"
    session = await _get_telegram_session()
    pruned = _json_prune_defaults(payload)
    timeout = aiohttp.ClientTimeout(total=request_timeout or TELEGRAM_REQUEST_TIMEOUT)
    try:
        async with session.post(url, json=pruned, timeout=timeout) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        exc_str = str(exc) or repr(exc) or type(exc).__name__
        if BOT_TOKEN:
            exc_str = exc_str.replace(BOT_TOKEN, "<TOKEN>")
        if _looks_like_proxy_garbage(exc):
            await _handle_proxy_failure(f"вызове {method}")
        raise RuntimeError(f"Network error in telegram_api_call for {method}: {exc_str}") from None
    if not isinstance(data, dict) or not data.get("ok"):
        _tg_proxy_breaker.note_success()
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    _tg_proxy_breaker.note_success()
    return data["result"]

def is_guest_message(message: Message | dict) -> bool:
    if isinstance(message, dict):
        return bool(message.get("guest_query_id"))
    return bool(getattr(message, "guest_query_id", None))

async def _answer_guest_text(message: Message, text: str) -> None:
    qid = getattr(message, "guest_query_id", None)
    if not qid:
        return
    payload = _md_to_html(text)
    if len(payload) > TG_MAX_LEN:
        payload = payload[:TG_MAX_LEN - 1] + "\u2026"
    res_id = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:32]
    res_art = InlineQueryResultArticle(
        id=res_id, title="Ответ бота",
        input_message_content=InputTextMessageContent(message_text=payload, parse_mode="HTML"),
    )
    try:
        await telegram_api_call("answerGuestQuery", {
            "guest_query_id": str(qid),
            "result": res_art.model_dump(exclude_none=True),
        }, request_timeout=10)
    except Exception as exc:
        log.warning("[guest] Failed answering guest: %s", exc)

def _split_text_chunks(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > max_len * 0.5:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

async def _send_text(message: Message, text: str, parse_html: bool = True, **kwargs: Any) -> None:
    if is_guest_message(message):
        await _answer_guest_text(message, text)
        return

    chunks = _split_text_chunks(text, TG_MAX_LEN)
    for i, chunk in enumerate(chunks):
        chunk_kwargs = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        if i == 0:
            res = await _tg_call(
                message.reply, _md_to_html(chunk) if parse_html else chunk,
                call_timeout=TELEGRAM_REQUEST_TIMEOUT,
                parse_mode=ParseMode.HTML if parse_html else None,
                **chunk_kwargs,
            )
            if res is None and parse_html:
                res = await _tg_call(
                    message.reply, chunk,
                    call_timeout=TELEGRAM_REQUEST_TIMEOUT,
                    parse_mode=None,
                    **chunk_kwargs,
                )
        else:
            res = await _tg_call(
                bot.send_message,
                chat_id=message.chat.id, text=_md_to_html(chunk) if parse_html else chunk,
                call_timeout=TELEGRAM_REQUEST_TIMEOUT,
                parse_mode=ParseMode.HTML if parse_html else None,
                **chunk_kwargs,
            )
            if res is None and parse_html:
                await _tg_call(
                    bot.send_message,
                    chat_id=message.chat.id, text=chunk,
                    call_timeout=TELEGRAM_REQUEST_TIMEOUT,
                    parse_mode=None,
                    **chunk_kwargs,
                )

async def _safe_reply(message: Message, text: str, parse_html: bool = True, **kwargs: Any) -> None:
    await _send_text(message, text, parse_html=parse_html, **kwargs)

async def _safe_callback_answer(cb: CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        if text is None:
            await _tg_call(cb.answer, call_timeout=5.0, show_alert=show_alert)
        else:
            await _tg_call(cb.answer, text, call_timeout=5.0, show_alert=show_alert)
    except Exception as exc:
        log.warning("[callback] Failed to answer callback: %s", exc)

async def _delete_message_quietly(msg: Message | None) -> None:
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.delete()

async def _edit_message_quietly(msg: Message | None, text: str, **kwargs: Any) -> bool:
    if msg is None:
        return False
    try:
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        res = await _tg_call(msg.edit_text, _md_to_html(text), **kwargs)
        if res is not None:
            return True
        kwargs["parse_mode"] = None
        res = await _tg_call(msg.edit_text, text, **kwargs)
        return res is not None
    except Exception:
        return False

# разбор ошибок

def _error_text(e: Exception) -> str:
    return " ".join(p for p in [str(e), str(getattr(e, "message", "")), str(getattr(e, "detail", ""))] if p).strip()

def _error_status(e: Exception, text: str) -> int | None:
    for a in ("status_code", "status", "code", "http_status"):
        val = getattr(e, a, None)
        try:
            if val is not None:
                return int(val)
        except Exception:
            pass
    m = re.search(r"(?<!\d)(\d{3})(?!\d)", text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return None

def _classify_model_error(status: int | None, text: str) -> str:
    low = text.lower()
    if status == 429 or any(tok in low for tok in ("resource_exhausted", "too many requests", "rate limit", "quota", "лимит")):
        return "rate_limit"
    if status == 402 or any(tok in low for tok in ("payment required", "paid tier", "requires paid", "billing", "кредит")):
        return "paid"
    if status in {401, 403} or any(tok in low for tok in ("unauthorized", "forbidden", "permission", "blocked")):
        return "forbidden"
    if status in {400, 404} or any(tok in low for tok in ("not found", "invalid argument", "invalid model", "unsupported", "unavailable")):
        return "unavailable"
    return "other"

_MODEL_ERROR_MESSAGES: dict[str, str] = {
    "rate_limit": "Лимит запросов для этой модели сейчас исчерпан. Подождите немного и попробуйте ещё раз — бот сам подберёт другую модель.",
    "paid": "Эта модель сейчас недоступна. Попробуйте повторить запрос — бот сам подберёт другую модель.",
    "forbidden": "Временная ошибка доступа к сервису. Попробуйте ещё раз.",
    "unavailable": "Эта модель сейчас недоступна. Попробуйте повторить запрос — бот сам подберёт другую модель.",
}
_MODEL_ERROR_FALLBACK_MSG = "Временная ошибка сервиса. Попробуйте ещё раз через некоторое время."

def _model_error_text(kind: str) -> str:
    return _MODEL_ERROR_MESSAGES.get(kind, _MODEL_ERROR_FALLBACK_MSG)

def _or_error_msg(e: Exception, kind: str) -> str:
    txt = _error_text(e).strip() or e.__class__.__name__
    status = _error_status(e, txt)
    return _model_error_text(_classify_model_error(status, txt))

class GeminiAllModelsExhaustedError(RuntimeError):
    def __init__(self, exhausted_models: list[str]) -> None:
        self.exhausted_models = exhausted_models
        super().__init__(f"All Gemini models exhausted quota: {', '.join(exhausted_models)}")

def _next_fallback_model(tried_models: set[str], chain: list[str]) -> str | None:
    return next((m for m in chain if m not in tried_models), None)

def _gemini_error_msg(e: Exception, model_id: str) -> str:
    if isinstance(e, ValueError):
        return str(e)
    if isinstance(e, GeminiAllModelsExhaustedError):
        return (
            "Лимит бесплатных запросов исчерпан для всех доступных моделей — "
            "это реальный суточный лимит, а не баг. Попробуйте позже."
        )
    txt = _error_text(e).strip() or e.__class__.__name__
    status = _error_status(e, txt)
    kind = _classify_model_error(status, txt)
    log.debug("[gemini] _gemini_error_msg: model=%s kind=%s status=%s", model_id, kind, status)
    return _model_error_text(kind)

from lumen_router_config import (
    GEMINI_MODELS,
    DEFAULT_GEMINI_MODEL,
    _check_unconfirmed_model_quotas,
    _check_temporary_free_models_expiry,
    _check_scheduled_removals_due,
    _OR_LIGHT_ORDER,
    _OR_HEAVY_ORDER,
    _OR_VISION_ORDER,
    GEMINI_DEFAULT_CHAIN,
    GEMINI_TTS_MODELS,
    FISH_AUDIO_TTS_MODEL,
    _check_fish_audio_tts_expiry,
    _looks_like_heavy_query,
    _looks_like_freshness_query,
    _build_route,
)


def get_system_prompt(model_id: str | None = None) -> str:
    now_str = datetime.now().strftime("%d %B %Y года (текущее время: %H:%M)")
    now_year = datetime.now().year
    dynamic_header = (
        f"ИНФОРМАЦИЯ О ТЕКУЩЕМ ВРЕМЕНИ:\n"
        f"• Сегодняшняя дата: {now_str}. Текущий год: {now_year}.\n"
    )
    return dynamic_header + SYSTEM_PROMPT

class ChatState(TypedDict, total=False):
    history: list[dict[str, Any]]
    quota: dict[str, Any]
    ctx: "deque[str]"
    recent_media_ids: dict[str, "deque[tuple[str, str]]"]
    last_activity: float

chat_state: dict[int, ChatState] = {}

_mg_buffers: dict[str, list[Message]] = {}
_mg_tasks: dict[str, asyncio.Task] = {}

app = FastAPI()

_ADMIN_SECRET_SEED = os.getenv("ADMIN_SECRET_SEED", "").strip() or BOT_TOKEN or "default"
WEBHOOK_SECRET = hashlib.sha256(_ADMIN_SECRET_SEED.encode()).hexdigest()[:32]
ADMIN_PANEL_KEY = hashlib.sha256(_ADMIN_SECRET_SEED.encode() + b"admin_panel").hexdigest()[:24]

def _check_bearer_token(request: Request, expected: str) -> bool:
    auth_header = request.headers.get("Authorization", "")
    provided = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)

def _check_admin_key(request: Request) -> bool:
    return _check_bearer_token(request, ADMIN_PANEL_KEY)

def _redact_secret(value: str) -> str:
    if not value:
        return "<empty>"
    return "…" + value[-6:] if len(value) > 6 else "…" + value

def _check_bot_token_auth(request: Request) -> bool:
    return _check_bearer_token(request, BOT_TOKEN)

@app.get("/")
async def healthcheck() -> dict[str, Any]:
    ready = bot is not None and client is not None
    return {"status": "ok" if ready else "starting", "ready": ready}

@app.get("/admin_keys")
async def get_admin_keys(request: Request) -> dict[str, str]:
    if not _check_bot_token_auth(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <BOT_TOKEN> header"}
    return {"webhook_secret": WEBHOOK_SECRET, "admin_panel_key": ADMIN_PANEL_KEY}

@app.get("/webhook_url")
async def get_webhook_url(request: Request) -> dict[str, str]:
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
    space_host = os.getenv("SPACE_HOST", "").strip()
    if not space_host:
        author = os.getenv("SPACE_AUTHOR_NAME", "silverelixir").lower()
        repo = os.getenv("SPACE_REPO_NAME", "lumen").lower()
        space_host = f"{author}-{repo}.hf.space"
    webhook_url = f"https://{space_host}/webhook"
    register_url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        f"?url={webhook_url}"
        f"&secret_token={WEBHOOK_SECRET}"
        f"&drop_pending_updates=true"
    )
    return {
        "webhook_url": webhook_url,
        "register_link": register_url,
        "instruction": "Открой register_link в браузере чтобы зарегистрировать вебхук"
    }

@app.post("/webhook")
async def webhook_handler(request: Request) -> dict[str, bool]:
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token, WEBHOOK_SECRET):
        log.warning("[webhook] Rejected request with invalid secret token")
        return {"ok": False}
    try:
        body = await request.json()
        if bot is not None:
            asyncio.create_task(_process_raw_update(body))
        else:
            log.warning("[webhook] Bot not initialized yet, dropping update")
    except Exception as exc:
        log.warning("[webhook] Failed to process incoming update: %s", exc)
    return {"ok": True}

@app.get("/diag")
async def network_diagnostics(request: Request) -> dict[str, Any]:
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
    targets = {
        "telegram_api": "https://api.telegram.org",
        "telegram_file_api": "https://api.telegram.org/bot" + (BOT_TOKEN[:6] if BOT_TOKEN else "x") + "/getMe",
        "configured_tg_proxy": TELEGRAM_API_BASE_URL + "/bot" + (BOT_TOKEN[:6] if BOT_TOKEN else "x") + "/getMe",
        "cloudflare_dot_com": "https://www.cloudflare.com",
        "cloudflare_workers_dev_root": "https://workers.dev",
        "deno_deploy": "https://deno.com",
        "google_generic": "https://www.google.com",
        "gemini_api": "https://generativelanguage.googleapis.com",
        "huggingface": "https://huggingface.co",
        "openrouter": "https://openrouter.ai",
        "tikwm": "https://www.tikwm.com",
        "pollinations": "https://image.pollinations.ai",
        "upstash": UPSTASH_REDIS_REST_URL if USE_UPSTASH else "https://upstash.com",
    }
    results: dict[str, Any] = {}
    session = await _get_http_session()
    for name, url in targets.items():
        start = time.time()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                elapsed = round(time.time() - start, 2)
                results[name] = {"status": resp.status, "elapsed_sec": elapsed, "ok": True}
        except Exception as exc:
            elapsed = round(time.time() - start, 2)
            exc_str = str(exc) or repr(exc) or type(exc).__name__
            if BOT_TOKEN:
                exc_str = exc_str.replace(BOT_TOKEN, "<TOKEN>")
            results[name] = {"error": exc_str, "elapsed_sec": elapsed, "ok": False}
    return {"diagnostics": results, "telegram_api_base_configured": TELEGRAM_API_BASE_URL}

@app.get("/export_state")
async def export_state(request: Request) -> dict[str, Any]:
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
    return {
        "exported_at": datetime.now().isoformat(),
        "chats": {str(cid): _serialize_chat_state(state) for cid, state in chat_state.items()},
        "global_quota": GLOBAL_QUOTA,
    }

ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "guest_message"]

_STATE_DIR = Path(os.getenv("STATE_DIR", "/app")).resolve()
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except Exception as _state_dir_exc:
    log.warning('[setup] STATE_DIR %s is not writable (%s), using a temp directory instead.', _STATE_DIR, _state_dir_exc)
    _STATE_DIR = Path(tempfile.gettempdir())
STATE_FILE_PATH = _STATE_DIR / "chat_state.json"
GLOBAL_QUOTA_FILE = _STATE_DIR / "global_quota.json"
_CHATS_DIR = _STATE_DIR / "chats"
with contextlib.suppress(Exception):
    _CHATS_DIR.mkdir(parents=True, exist_ok=True)
CHAT_INDEX_KEY = "lumen:chat_index"
CHAT_INDEX_FILE = _STATE_DIR / "chat_index.json"

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
USE_UPSTASH = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)
if bool(UPSTASH_REDIS_REST_URL) != bool(UPSTASH_REDIS_REST_TOKEN):
    log.warning('[setup] Only one of UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN is set — both are required together, Upstash will not be used.')
log.info(
    '[setup] Persistent storage: %s',
    "Upstash Redis" if USE_UPSTASH else f"локальный файл в {_STATE_DIR} (см. README про эфемерность на HF Spaces)"
)

from lumen_state_storage import (
    CHAT_STATE_SCHEMA_VERSION,
    StorageConfig,
    _chat_storage_key,
    _serialize_chat_state,
    _current_quota_day,
    _upstash_request as _lumen_upstash_request,
    _upstash_set as _lumen_upstash_set,
    _upstash_get as _lumen_upstash_get,
    _upstash_delete as _lumen_upstash_delete,
    _storage_write_text as _lumen_storage_write_text,
    _storage_read_text as _lumen_storage_read_text,
    _storage_delete_text as _lumen_storage_delete_text,
    _chat_storage_path as _lumen_chat_storage_path,
)

def _storage_config() -> StorageConfig:
    return StorageConfig(
        use_upstash=USE_UPSTASH, upstash_url=UPSTASH_REDIS_REST_URL,
        upstash_token=UPSTASH_REDIS_REST_TOKEN, chats_dir=_CHATS_DIR,
    )

__all__ = ["_urllib_request", "CHAT_STATE_SCHEMA_VERSION"]

def _upstash_request(command_path: str, *, method: str = "GET", body: bytes | None = None) -> Any:
    return _lumen_upstash_request(UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, command_path, method=method, body=body)

def _upstash_set(key: str, value: str) -> None:
    _lumen_upstash_set(UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, key, value)

def _upstash_get(key: str) -> str | None:
    return _lumen_upstash_get(UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, key)

def _upstash_delete(key: str) -> None:
    _lumen_upstash_delete(UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, key)

def _storage_write_text(key: str, path: Path, text: str) -> None:
    _lumen_storage_write_text(_storage_config(), key, path, text)

def _storage_read_text(key: str, path: Path) -> str | None:
    return _lumen_storage_read_text(_storage_config(), key, path)

def _storage_delete_text(key: str, path: Path) -> None:
    _lumen_storage_delete_text(_storage_config(), key, path)

def _chat_storage_path(chat_id: int) -> Path:
    return _lumen_chat_storage_path(_storage_config(), chat_id)

def _save_chat_to_storage(chat_id: int, state: dict[str, Any]) -> bool:
    try:
        payload = json.dumps(_serialize_chat_state(state), ensure_ascii=False)
        _storage_write_text(_chat_storage_key(chat_id), _chat_storage_path(chat_id), payload)
        return True
    except Exception as exc:
        log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
        return False

def _delete_chat_storage(chat_id: int) -> bool:
    try:
        _storage_delete_text(_chat_storage_key(chat_id), _chat_storage_path(chat_id))
        return True
    except Exception as exc:
        log.warning("[state] Deleting chat %s failed: %s", chat_id, exc)
        return False

class QuotaEntry(TypedDict):
    used: int
    exhausted_at: float | None

GLOBAL_QUOTA: dict[str, Any] = {
    "gemini": {},
    "openrouter": {},
    "quota_day": None,
}

_QUOTA_CHECK_THROTTLE_SEC = 60.0
_last_quota_check_monotonic: float = 0.0

GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC = 3600.0
_last_gemini_exhausted_alert_monotonic: float = 0.0

async def _maybe_alert_gemini_exhausted() -> None:
    global _last_gemini_exhausted_alert_monotonic
    now = time.monotonic()
    if now - _last_gemini_exhausted_alert_monotonic < GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC:
        return
    _last_gemini_exhausted_alert_monotonic = now
    await _notify_owner("⚠️ Квота Gemini исчерпана целиком по всем моделям в маршруте (см. /stats для деталей).")

def _reset_quota_if_new_day() -> None:
    global _last_quota_check_monotonic
    now_mono = time.monotonic()
    if now_mono - _last_quota_check_monotonic < _QUOTA_CHECK_THROTTLE_SEC:
        return
    _last_quota_check_monotonic = now_mono
    today = _current_quota_day()
    if GLOBAL_QUOTA.get("quota_day") == today:
        return
    had_previous = GLOBAL_QUOTA.get("quota_day") is not None
    for provider in ("gemini", "openrouter"):
        for entry in GLOBAL_QUOTA.get(provider, {}).values():
            if isinstance(entry, dict):
                entry["used"] = 0
                entry["exhausted_at"] = None
    GLOBAL_QUOTA["quota_day"] = today
    if had_previous:
        log.info('[quota] New day started (%s) — used/exhausted_at counters reset for all models.', today)
    mark_quota_dirty()

def load_global_quota() -> None:
    try:
        raw = _storage_read_text("lumen:global_quota", GLOBAL_QUOTA_FILE)
        if not raw:
            return
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            if "gemini" in loaded:
                GLOBAL_QUOTA["gemini"] = loaded["gemini"]
            if "openrouter" in loaded:
                GLOBAL_QUOTA["openrouter"] = loaded["openrouter"]
            if "quota_day" in loaded:
                GLOBAL_QUOTA["quota_day"] = loaded["quota_day"]
    except Exception as exc:
        log.warning("[quota] Failed to load global quota: %s", exc)
    _reset_quota_if_new_day()

def save_global_quota() -> None:
    try:
        _storage_write_text("lumen:global_quota", GLOBAL_QUOTA_FILE, json.dumps(GLOBAL_QUOTA, ensure_ascii=False))
    except Exception as exc:
        log.warning("[quota] Failed to save global quota: %s", exc)

def _restore_single_chat(cid: int, s: dict[str, Any]) -> None:
    schema_version = s.get("schema_version", 0)
    log.debug('[state] Restoring chat %s (schema_version=%s)', cid, schema_version)
    raw_media = s.get("recent_media_ids", {})
    if isinstance(raw_media, dict):
        media_buckets = {
            str(uid): deque(items, maxlen=MAX_MEDIA_RECENT_IDS)
            for uid, items in raw_media.items()
        }
    else:
        media_buckets = {}
    if "history" in s:
        history = list(s.get("history") or [])
    else:
        history = list(s.get("gemini_history") or []) + list(s.get("or_history") or [])
        if len(history) > SHARED_HISTORY_MAX_LEN:
            history = history[-SHARED_HISTORY_MAX_LEN:]
    chat_state[cid] = {
        "history": history,
        "quota": s.get("quota", {}),
        "ctx": deque(maxlen=MAX_CHAT_HISTORY_LEN),
        "recent_media_ids": media_buckets,
        "last_activity": time.monotonic(),
    }

def _save_chat_index() -> None:
    try:
        ids = sorted(chat_state.keys())
        _storage_write_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE, json.dumps(ids))
    except Exception as exc:
        log.warning("[state] Saving chat index failed: %s", exc)

_dirty_chat_ids: set[int] = set()
_pending_chat_deletions: set[int] = set()
_index_dirty = False
_quota_dirty = False
FLUSH_INTERVAL_SEC = 10.0
STATE_FLUSH_CONCURRENCY = int(os.getenv("STATE_FLUSH_CONCURRENCY", "10"))
_state_flush_semaphore = asyncio.Semaphore(STATE_FLUSH_CONCURRENCY)

async def _save_chat_to_storage_limited(chat_id: int, state: dict[str, Any]) -> bool:
    async with _state_flush_semaphore:
        return await asyncio.to_thread(_save_chat_to_storage, chat_id, state)

async def _delete_chat_storage_limited(chat_id: int) -> bool:
    async with _state_flush_semaphore:
        return await asyncio.to_thread(_delete_chat_storage, chat_id)

def mark_state_dirty(chat_id: int | None = None) -> None:
    global _index_dirty
    if chat_id is not None:
        _dirty_chat_ids.add(chat_id)
    else:
        _dirty_chat_ids.update(chat_state.keys())
        _index_dirty = True

def _mark_new_chat_id(chat_id: int) -> None:
    global _index_dirty
    _dirty_chat_ids.add(chat_id)
    _index_dirty = True

def mark_quota_dirty() -> None:
    global _quota_dirty
    _quota_dirty = True

async def _flush_dirty_state_once() -> None:
    global _dirty_chat_ids, _index_dirty, _quota_dirty, _pending_chat_deletions
    try:
        if _pending_chat_deletions:
            to_delete = list(_pending_chat_deletions)
            _pending_chat_deletions = set()
            del_results = await asyncio.gather(
                *(_delete_chat_storage_limited(cid) for cid in to_delete),
                return_exceptions=True,
            )
            failed_deletes = {cid for cid, res in zip(to_delete, del_results) if res is not True}
            if failed_deletes:
                _pending_chat_deletions.update(failed_deletes)
                log.warning(
                    '[state] %d chat deletion(s) failed, will retry next cycle: %s',
                    len(failed_deletes), ", ".join(str(c) for c in sorted(failed_deletes)),
                )
        if _dirty_chat_ids:
            to_save = list(_dirty_chat_ids)
            _dirty_chat_ids = set()
            attempted_ids = [cid for cid in to_save if cid in chat_state]
            save_results = await asyncio.gather(
                *(_save_chat_to_storage_limited(cid, chat_state[cid]) for cid in attempted_ids),
                return_exceptions=True,
            )
            failed_ids = {cid for cid, res in zip(attempted_ids, save_results) if res is not True}
            if failed_ids:
                _dirty_chat_ids.update(failed_ids)
                log.warning(
                    '[state] %d chat(s) failed to save this cycle, will retry next: %s',
                    len(failed_ids), ", ".join(str(c) for c in sorted(failed_ids)),
                )
        if _index_dirty:
            _index_dirty = False
            await asyncio.to_thread(_save_chat_index)
        if _quota_dirty:
            _quota_dirty = False
            await asyncio.to_thread(save_global_quota)
    except Exception as exc:
        log.warning('[state] Periodic state flush failed: %s', exc)


async def _flush_dirty_state() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SEC)
        await _flush_dirty_state_once()

def _flush_state_now() -> None:
    global _dirty_chat_ids, _index_dirty, _pending_chat_deletions
    for cid in list(_pending_chat_deletions):
        _delete_chat_storage(cid)
    _pending_chat_deletions = set()
    for cid in list(_dirty_chat_ids):
        st = chat_state.get(cid)
        if st is not None:
            _save_chat_to_storage(cid, st)
    _dirty_chat_ids = set()
    if _index_dirty:
        _save_chat_index()
        _index_dirty = False

def load_state_from_disk() -> None:
    load_global_quota()

    index_raw = None
    try:
        index_raw = _storage_read_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE)
    except Exception as exc:
        log.warning("[state] Reading chat index failed, falling back to legacy combined blob: %s", exc)

    if index_raw is not None:
        try:
            chat_ids = json.loads(index_raw)
        except Exception as exc:
            log.warning('[state] Failed to parse chat index: %s', exc)
            chat_ids = []
        loaded_count = 0
        for chat_id_raw in chat_ids:
            try:
                cid = int(chat_id_raw)
            except Exception:
                continue
            try:
                raw = _storage_read_text(_chat_storage_key(cid), _chat_storage_path(cid))
            except Exception as exc:
                log.warning('[state] Failed to read chat %s: %s', cid, exc)
                continue
            if not raw:
                continue
            try:
                s = json.loads(raw)
            except Exception as exc:
                log.warning('[state] Failed to parse chat state %s: %s', cid, exc)
                continue
            _restore_single_chat(cid, s)
            loaded_count += 1
        log.info("[state] Restored states for %d chats (per-chat storage).", loaded_count)
        return

    try:
        raw = _storage_read_text("lumen:chat_state", STATE_FILE_PATH)
        if not raw:
            return
        loaded = json.loads(raw)
        for chat_id_str, s in loaded.items():
            try:
                cid = int(chat_id_str)
            except Exception:
                continue
            _restore_single_chat(cid, s)
        log.info(
            '[state] Restored states for %d chats (migrated from the old shared storage format — will be rewritten in the new per-chat format on next flush).',
            len(chat_state),
        )
        mark_state_dirty()
    except Exception as exc:
        log.warning("[state] Restoring states failed: %s", exc)

def get_state(chat_id: int) -> dict[str, Any]:
    if chat_id not in chat_state:
        chat_state[chat_id] = {
            "history": [],
            "quota": {},
            "ctx": deque(maxlen=MAX_CHAT_HISTORY_LEN),
            "recent_media_ids": {},
            "last_activity": time.monotonic(),
        }
        _mark_new_chat_id(chat_id)
    else:
        chat_state[chat_id]["last_activity"] = time.monotonic()
    if len(chat_state) > MAX_CHAT_LIMIT:
         _prune_old_chats()
    return chat_state[chat_id]

def _prune_old_chats() -> None:
    sorted_ids = sorted(chat_state.keys(), key=lambda cid: chat_state[cid].get("last_activity", 0))
    to_remove = len(chat_state) - PRUNED_CHAT_TARGET
    removed_ids = sorted_ids[:to_remove]
    for cid in removed_ids:
        chat_state.pop(cid, None)
        _chat_locks.pop(cid, None)
    _pending_chat_deletions.update(removed_ids)
    mark_state_dirty()

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock

def _is_owner(user_id: int | None) -> bool:
    return OWNER_ID is not None and user_id is not None and user_id == OWNER_ID

async def _notify_owner(text: str) -> None:
    if OWNER_ID is None or bot is None:
        return
    with contextlib.suppress(Exception):
        await _tg_call(bot.send_message, chat_id=OWNER_ID, text=text, call_timeout=10.0)

async def _is_privileged_in_chat(chat_type: str, chat_id: int, user_id: int | None) -> bool:
    if chat_type == ChatType.PRIVATE:
        return True
    if _is_owner(user_id):
        return True
    if user_id is None or bot is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return getattr(member, "status", None) in ("creator", "administrator")
    except Exception as exc:
        log.warning('[perm] Failed to check admin status in chat %s: %s', chat_id, exc)
        return False


from lumen_images import (
    DEFAULT_HF_IMAGE_MODEL,
    HF_IMAGE_MODELS,
    _pick_image_model,
    _hf_text_to_image,
    _image_model_label,
)

__all__ += ["DEFAULT_HF_IMAGE_MODEL"]

def _is_gemini_supported_mime(mime: str) -> bool:
    m = mime.lower()
    if m in ("image/png", "image/jpeg", "image/webp", "image/heic", "image/heif", "image/gif"):
        return True
    if m in ("audio/mp3", "audio/wav", "audio/ogg", "audio/aac", "audio/flac", "audio/mp4", "audio/m4a", "audio/mpeg", "audio/x-m4a"):
        return True
    if m in ("video/mp4", "video/mpeg", "video/mov", "video/avi", "video/flv", "video/mpg", "video/webm", "video/wmv", "video/quicktime"):
        return True
    if m in (
        "application/pdf",
        "text/plain",
        "text/html",
        "text/css",
        "text/javascript",
        "application/x-javascript",
        "text/csv",
        "text/markdown",
        "text/xml",
        "application/xml",
        "application/json",
        "text/x-python",
        "application/x-python-code"
    ):
        return True
    return False

def _sanitize_mime_type(file_path: str | None, mime: str | None, default_fallback: str = "application/octet-stream") -> str:
    m = (mime or "").strip().lower()
    if not m or m in ("application/octet-stream", "binary/oct-stream", "application/x-binary", "octet/stream"):
        if file_path:
             guessed, _ = mimetypes.guess_type(file_path)
             if guessed:
                 return guessed.lower()
        if file_path:
            ext = Path(file_path).suffix.lower()
            ext_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".m4v": "video/x-m4v",
                ".avi": "video/x-msvideo",
                ".mp3": "audio/mpeg",
                ".ogg": "audio/ogg",
                ".oga": "audio/ogg",
                ".opus": "audio/ogg",
                ".m4a": "audio/mp4",
                ".wav": "audio/wav",
                ".pdf": "application/pdf",
                ".txt": "text/plain",
                ".csv": "text/csv",
                ".json": "application/json",
                ".html": "text/html",
                ".htm": "text/html",
                ".xml": "text/xml"
            }
            if ext in ext_map:
                return ext_map[ext]
        return default_fallback
    if "voice" in m or m == "audio/ogg":
        return "audio/ogg"
    if m == "video/quicktime":
        return "video/quicktime"
    return m

def _media_file_id_and_mime(source: Any) -> tuple[str, str, str]:
    file_id, mime, filename = "", "", ""
    if source is None:
        return file_id, mime, filename
    for key in ("file_id", "id"):
        v = getattr(source, key, None) if not isinstance(source, dict) else source.get(key)
        if isinstance(v, str) and v.strip():
            file_id = v.strip()
            break
    mt = getattr(source, "mime_type", None) if not isinstance(source, dict) else source.get("mime_type")
    if isinstance(mt, str):
         mime = mt.strip()
    nm = getattr(source, "file_name", None) if not isinstance(source, dict) else source.get("file_name")
    if isinstance(nm, str):
         filename = nm.strip()

    class_name = type(source).__name__
    if not mime:
        if class_name == "PhotoSize":
            mime = "image/jpeg"
        elif class_name == "Sticker":
            mime = "image/webp"
        elif class_name == "Voice":
            mime = "audio/ogg"
        elif class_name == "VideoNote":
            mime = "video/mp4"
        elif class_name == "Animation":
            mime = "video/mp4"

    return file_id, mime, filename

def _mime_suffix(mime: str, filename: str = "") -> str:
    if filename:
        suffix = Path(filename).suffix
        if suffix:
             return suffix
    m = (mime or "").lower()
    if m.startswith("image/"):
        sub = m.split("/", 1)[1]
        return {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(sub, f".{sub}")
    if m.startswith("audio/"):
        return ".mp3"
    if m.startswith("video/"):
        return ".mp4"
    return ".bin"

def _msg_media_source(message: Any) -> Any | None:
    for attr in ("photo", "video", "animation", "video_note", "voice", "audio", "document", "sticker"):
        val = getattr(message, attr, None)
        if not val:
            continue
        if attr == "photo" and isinstance(val, list):
            return val[-1] if val else None
        return val
    return None

async def _download_telegram_file_bytes(file_id: str, *, timeout: float | None = None, retries: int = 1) -> tuple[bytes, str]:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            file = await asyncio.wait_for(bot.get_file(file_id), timeout=TELEGRAM_GET_FILE_TIMEOUT)
            file_path = getattr(file, "file_path", None) or getattr(file, "path", None)
            if not file_path:
                raise RuntimeError("File path is empty")
            session = await _get_telegram_session()
            url = f"{TELEGRAM_API_BASE_URL}/file/bot{BOT_TOKEN}/{file_path}"
            async with session.get(url, timeout=timeout or TELEGRAM_MEDIA_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.read()
                mime = resp.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip()
            real_mime = _sanitize_mime_type(file_path, mime)
            return data, real_mime
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                log.warning('[media] Attempt %d/%d to download file_id %s failed, retrying in 0.5s: %s', attempt + 1, retries + 1, file_id, exc)
                await asyncio.sleep(0.5)
    exc_str = str(last_exc) or repr(last_exc) or type(last_exc).__name__
    if BOT_TOKEN:
        exc_str = exc_str.replace(BOT_TOKEN, "<TOKEN>")
    raise RuntimeError(f"Network error in download_telegram_file_bytes: {exc_str}") from None

def _save_media_to_history(source: Any, state: dict[str, Any], user_id: int | None) -> None:
    file_id, mime, _ = _media_file_id_and_mime(source)
    if not file_id or user_id is None:
        return
    buckets: dict[str, deque] = state.setdefault("recent_media_ids", {})
    key = str(user_id)
    recent = buckets.setdefault(key, deque(maxlen=MAX_MEDIA_RECENT_IDS))
    if not recent or recent[-1][0] != file_id:
        recent.append((file_id, mime or "application/octet-stream"))

async def _download_message_attachment_to_tmp(source: Any) -> tuple[str, str, str] | None:
    file_id, mime, filename = _media_file_id_and_mime(source)
    if not file_id:
        return None
    suffix = _mime_suffix(mime, filename)
    fd, tmp_path = tempfile.mkstemp(prefix="tg_media_", suffix=suffix)
    os.close(fd)
    try:
        data, real_mime = await _download_telegram_file_bytes(file_id)
        final_mime = _sanitize_mime_type(filename or "", mime)
        if final_mime == "application/octet-stream" or not final_mime:
             final_mime = _sanitize_mime_type(None, real_mime)
        if final_mime == "application/octet-stream" or not final_mime:
             final_mime = mime or "application/octet-stream"
        with open(tmp_path, "wb") as h:
            h.write(data)
        return tmp_path, final_mime, filename or Path(tmp_path).name
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise

async def _fetch_media(file_id: str, mime: str) -> tuple[bytes, str] | None:
    if not file_id:
        return None
    try:
        data, real_mime = await _download_telegram_file_bytes(file_id)
        final_mime = _sanitize_mime_type(None, mime)
        if final_mime == "application/octet-stream":
            final_mime = _sanitize_mime_type(None, real_mime)
        return data, final_mime
    except Exception as exc:
        log.warning("[media] Download media failed for file_id %s: %s", file_id, exc)
        return None

def _ensure_prompt_text(text: str | None, mime: str) -> str:
    s = (text or "").strip()
    if s:
        return s
    m = mime.lower()
    if m.startswith("image/"):
         return "Подробно опиши, что изображено на картинке."
    if m.startswith("video/") or m == "video/quicktime":
         return "Подробно опиши происходящее на этом видео."
    if m.startswith("audio/") or m == "audio/ogg" or m == "audio/mpeg" or m == "audio/mp3" or "voice" in m:
         return "Прослушай и подробно опиши, что на этой аудиозаписи, или кратко перескажи ее содержание."
    if m == "image/gif" or "animation" in m:
         return "Подробно опиши происходящее на этой анимации."
    return "Проанализируй и подробно опиши содержимое этого вложения."

def _quota_entry(provider: str, model_id: str) -> QuotaEntry:
    _reset_quota_if_new_day()
    sub = GLOBAL_QUOTA.setdefault(provider, {})
    return sub.setdefault(model_id, {"used": 0, "exhausted_at": None})

def _mark_quota_exhausted(provider: str, model_id: str) -> None:
    e = _quota_entry(provider, model_id)
    e["exhausted_at"] = time.time()
    mark_quota_dirty()

def _record_quota_usage(provider: str, model_id: str) -> None:
    e = _quota_entry(provider, model_id)
    e["used"] = int(e.get("used") or 0) + 1
    e["exhausted_at"] = None
    mark_quota_dirty()

from lumen_security import (
    _leak_scan_window,
    _IDENTITY_LEAK_FALLBACK,
    _INJECTED_PAYLOAD_ECHO_FALLBACK,
    _detect_injected_payload_echo,
    _detect_identity_leak,
    _scrub_identity_leak,
    _INJECTION_PROBE_REPLY,
    _looks_like_injection_probe,
)


class OpenRouterAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

async def _or_request(path: str, method: str = "GET", *, json_body: dict | None = None) -> Any:
    if not OPENROUTER_API_KEY:
        raise OpenRouterAPIError("OPENROUTER_API_KEY не задан")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-OpenRouter-Title": OPENROUTER_TITLE,
    }
    if json_body is not None:
         headers["Content-Type"] = "application/json"
    session = await _get_http_session()
    url = f"{OPENROUTER_BASE_URL}/{path.lstrip('/')}"
    try:
        async with session.request(
            method.upper(), url, headers=headers, json=json_body,
            timeout=aiohttp.ClientTimeout(total=ROUTE_MODEL_TIMEOUT_SEC, connect=10.0)
        ) as resp:
            if resp.status >= 400:
                payload = await resp.json(content_type=None)
                msg = payload.get("error", {}).get("message") or f"HTTP {resp.status}"
                raise OpenRouterAPIError(msg, status_code=resp.status, payload=payload)
            return await resp.json(content_type=None)
    except OpenRouterAPIError:
        raise
    except Exception as exc:
        exc_str = str(exc) or repr(exc) or exc.__class__.__name__
        raise OpenRouterAPIError(f"Сетевая ошибка OpenRouter: {exc_str}") from exc

def _or_extract_text(data: Any) -> str:
    if isinstance(data, str):
         return data.strip()
    if isinstance(data, dict):
         return _or_extract_text(data.get("content") or data.get("text") or "")
    if isinstance(data, list) and data:
         return "".join(_or_extract_text(i) for i in data)
    return ""

def _is_account_wide_or_rate_limit(text: str) -> bool:
    low = text.lower()
    return "free-models-per-day" in low

async def _probe_or_model_liveness() -> None:
    if not OPENROUTER_API_KEY:
        return
    day_idx = date.today().timetuple().tm_yday
    lists = {
        "_OR_LIGHT_ORDER": _OR_LIGHT_ORDER,
        "_OR_HEAVY_ORDER": _OR_HEAVY_ORDER,
        "_OR_VISION_ORDER": _OR_VISION_ORDER,
    }
    heads = {name: models[day_idx % len(models)] for name, models in lists.items() if models}
    for list_name, model_id in heads.items():
        try:
            payload = {"model": model_id, "messages": [{"role": "user", "content": "ping"}], "stream": False}
            await _or_request("chat/completions", "POST", json_body=payload)
        except Exception as exc:
            txt = _error_text(exc).strip() or exc.__class__.__name__
            kind = _classify_model_error(_error_status(exc, txt), txt)
            if kind in ("unavailable", "forbidden"):
                log.warning(
                    '[or][liveness] Head-of-list model %s (%s) is responding as if pulled from the free tier (%s: %s) — looks like the same pattern as already-known dead models in _OR_MODEL_HEALTH. Check openrouter.ai and add a registry entry if confirmed.',
                    list_name, model_id, kind, txt[:200],
                )

async def _or_chat_completion_with_fallback(
    messages: list[dict], trial_models: list[str], primary_model_id: str, *,
    deadline: float | None = None,
) -> tuple[str, str]:
    last_exc: Exception | None = None
    tried: list[str] = []
    for model_trial in trial_models:
        if deadline is not None and time.monotonic() > deadline:
            log.warning('[or] Route time budget exhausted before model %s. Tried: %s', model_trial, ", ".join(tried) or "ничего")
            raise RouteBudgetExceededError(tried)
        tried.append(model_trial)
        messages[0]["content"] = get_system_prompt(model_trial)
        try:
            payload = {"model": model_trial, "messages": messages, "stream": False}
            resp = await _or_request("chat/completions", "POST", json_body=payload)
            choices = resp.get("choices") or []
            answer = ""
            if choices:
                answer = _or_extract_text(choices[0].get("message") or "")
            answer = answer.strip() or "Empty response"

            answer = _scrub_identity_leak(answer, source=f"or_chat_completion:{model_trial}")
            log.info('[or] Successful response from model %s (primary=%s, models tried: %d)', model_trial, primary_model_id, len(tried))
            return answer, model_trial
        except Exception as exc:
            last_exc = exc
            err_text = str(exc).lower()
            if _is_account_wide_or_rate_limit(err_text):
                log.warning(
                    '[or] Detected an account-wide OpenRouter limit (free-models-per-day) on model %s — stopping the remaining candidates in the chain, they would fail with the same error anyway.',
                    model_trial,
                )
                raise
            log.warning("[or] Model %s failed: %s. Switching to next candidate...", model_trial, str(last_exc) or last_exc.__class__.__name__)

    if last_exc:
        raise last_exc
    raise RuntimeError("Не удалось получить ответ ни от одной модели-кандидата.")

async def ask_openrouter_text(chat_id: int, user_text: str, model_chain: list[str], *, deadline: float | None = None) -> str:
    state = get_state(chat_id)
    history = state.setdefault("history", [])
    ctx = state.get("ctx", deque())
    trial_models = list(dict.fromkeys(model_chain)) or [_OR_LIGHT_ORDER[0]]
    primary_model_id = trial_models[0]
    messages = _build_openrouter_turn_messages(chat_id, user_text, primary_model_id)

    answer, model_trial = await _or_chat_completion_with_fallback(messages, trial_models, primary_model_id, deadline=deadline)

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    ctx.clear()
    if len(history) > SHARED_HISTORY_MAX_LEN:
         del history[:-SHARED_HISTORY_MAX_LEN]
    _record_quota_usage("openrouter", model_trial)
    return answer

async def ask_openrouter_multimodal(
    chat_id: int, user_text: str, media_tuple: tuple[bytes, str], media_filename: str,
    model_chain: list[str], *, deadline: float | None = None,
) -> str:
    state = get_state(chat_id)
    b64 = base64.b64encode(media_tuple[0]).decode("utf-8")
    img_url = f"data:{media_tuple[1]};base64,{b64}"

    ctx = state.get("ctx", deque())
    full_text = user_text
    if ctx:
        full_text = "Фон разговора в чате (для контекста, не обращение к тебе):\n" + "\n".join(ctx) + "\n\nТекущий вопрос/сообщение: " + user_text

    history = state.setdefault("history", [])
    trial_models = list(dict.fromkeys(model_chain)) or [_OR_VISION_ORDER[0]]
    primary_model_id = trial_models[0]
    messages: list[dict] = [{"role": "system", "content": get_system_prompt(primary_model_id)}]
    messages.extend(history)
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": full_text},
            {"type": "image_url", "image_url": {"url": img_url}}
        ]
    })

    answer, model_trial = await _or_chat_completion_with_fallback(messages, trial_models, primary_model_id, deadline=deadline)

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > SHARED_HISTORY_MAX_LEN:
         del history[:-SHARED_HISTORY_MAX_LEN]
    ctx.clear()
    _record_quota_usage("openrouter", model_trial)
    return answer

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
    _probe_and_thumbnail_from_bytes,
    _resolve_tiktok_short,
    _fetch_tikwm_media_data,
    _looks_like_resolved_tiktok_url,
)

__all__ += ["_looks_like_resolved_tiktok_url"]

async def _send_tiktok_music(session, media_data: dict, message: Message, author: str, headers: dict) -> None:
    music_url = media_data.get("music")
    if not music_url:
         return

    try:
         music_bytes = await _download_url_bin(session, music_url, headers=headers)
         if not music_bytes:
              return

         music_info = media_data.get("music_info") or {}
         raw_music_title = music_info.get("title") or "Музыка из TikTok"
         raw_music_author = music_info.get("author") or author

         author_nick = media_data.get("author", {}).get("nickname") or ""
         author_uniq = media_data.get("author", {}).get("unique_id") or ""
         author_uniq_clean = author_uniq.lstrip("@")

         m_title_lower = raw_music_title.lower()
         mentions_generic_phrase = any(phrase in m_title_lower for phrase in _GENERIC_ORIGINAL_SOUND_PHRASES)
         residual_title = raw_music_title
         for _phrase in _GENERIC_ORIGINAL_SOUND_PHRASES:
              residual_title = re.sub(re.escape(_phrase), "", residual_title, flags=re.IGNORECASE)
         residual_title = residual_title.strip(" \t-–—:")
         if author_nick:
              residual_title = re.sub(re.escape(author_nick), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         if author_uniq_clean:
              residual_title = re.sub(re.escape(author_uniq_clean), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         is_original_sound = mentions_generic_phrase and not residual_title
         sender_language_code = message.from_user.language_code if message.from_user else None

         log.info(
              "[tiktok-music][diag] raw_title=%r raw_author=%r cover=%r author_avatar=%r "
              "residual_title=%r sender_language_code=%r -> is_original_sound=%s",
              raw_music_title, raw_music_author, music_info.get("cover"),
              media_data.get("author", {}).get("avatar"), residual_title, sender_language_code, is_original_sound,
         )

         if is_original_sound:
              performer_name = author_uniq_clean if author_uniq_clean else raw_music_author
              cleaned_title = _original_sound_label(sender_language_code)
         else:
              cleaned_title = residual_title if (mentions_generic_phrase and residual_title) else raw_music_title
              performer_name = raw_music_author

         cover_url = music_info.get("cover") or music_info.get("avatar") or media_data.get("author", {}).get("avatar")
         cover_bytes = None
         if cover_url:
              try:
                   cover_bytes = await _download_url_bin(session, cover_url, headers=headers)
              except Exception as e:
                   log.warning("[tiktok] failed to download cover image: %s", e)

         thumbnail_file = None
         if cover_bytes:
              thumbnail_file = BufferedInputFile(cover_bytes, filename="cover.jpg")

         tagged_music_bytes = music_bytes
         try:
              with tempfile.TemporaryDirectory() as tmp_dir:
                   tmp_mp3_path = os.path.join(tmp_dir, "music.mp3")
                   with open(tmp_mp3_path, "wb") as f:
                        f.write(music_bytes)
                   _write_mp3_tags(tmp_mp3_path, cleaned_title, performer_name, cover_bytes)
                   if os.path.exists(tmp_mp3_path) and os.path.getsize(tmp_mp3_path) > 0:
                        with open(tmp_mp3_path, "rb") as f:
                             tagged_music_bytes = f.read()
         except Exception as tag_err:
              log.warning("[tiktok] failed to write embedded tags to MP3: %s", tag_err)

         await bot.send_audio(
              chat_id=message.chat.id,
              audio=BufferedInputFile(tagged_music_bytes, filename=f"{cleaned_title[:60]}.mp3"),
              title=cleaned_title,
              performer=performer_name,
              thumbnail=thumbnail_file,
              reply_to_message_id=message.message_id
         )
    except Exception as e:
         log.warning("[tiktok] failed to send music: %s", e)

async def handle_tiktok_sound(message: Message, status: Message | None) -> None:
    if is_guest_message(message):
         await _answer_guest_text(message, "Ссылка на звук TikTok распознана.")
         return
    raise TikTokUserFacingError(
         "Скачать звук отдельно по ссылке на его страницу не получится — ни TikWM, ни сам TikTok "
         "не отдают нужные данные по такому виду ссылки этому боту. Пришлите, пожалуйста, ссылку "
         "на любое видео с этим звуком — бот пришлёт звук вместе с ним."
    )


async def _fetch_tikwm_media_data_with_proxy_fallback(session: aiohttp.ClientSession, resolved_url: str, headers: dict) -> dict | None:
    result = None
    for candidate in _tikwm_proxy_candidates():
        result = await _fetch_tikwm_media_data(session, resolved_url, headers, proxy_base_url=candidate)
        if result is not None:
            return result
    return result


async def handle_tiktok(message: Message, url: str) -> None:
    if is_guest_message(message):
         await _answer_guest_text(message, f"Ссылка на TikTok распознана: {url}")
         return
    status = await _tg_call(message.reply, "Обрабатываю ссылку на TikTok")
    try:
         session = await _get_http_session()
         resolved_url = await _resolve_tiktok_short(session, url)
         if "?" in resolved_url:
              resolved_url = resolved_url.split("?")[0]

         headers = {
              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
              "Accept": "application/json"
         }

         music_page_id = _tiktok_music_page_id(resolved_url)
         if music_page_id:
              await handle_tiktok_sound(message, status)
              return

         media_data = await _fetch_tikwm_media_data_with_proxy_fallback(session, resolved_url, headers)

         if not media_data:
              raise TikTokUserFacingError("Не удалось получить видео по этой ссылке — возможно, оно приватное, удалено, заблокировано по региону или ссылка битая.")

         if images_debug := media_data.get("images"):
              log.info(
                   '[tikwm][diag] slideshow post: response keys=%s, images(%d items)=%s, live_images=%s, top-level play=%s hdplay=%s wmplay=%s',
                   sorted(media_data.keys()), len(images_debug), images_debug, media_data.get("live_images"),
                   media_data.get("play"), media_data.get("hdplay"), media_data.get("wmplay"),
              )

         author = (media_data.get("author") or {}).get("nickname") or "Автор TikTok"

         images = media_data.get("images")
         if images and isinstance(images, list):
              images_to_fetch = images[:TIKTOK_SLIDESHOW_MAX_ITEMS]
              status_text = f"Скачиваю слайдшоу TikTok ({len(images_to_fetch)} слайдов)"
              if len(images) > len(images_to_fetch):
                   status_text += f" — показаны первые {len(images_to_fetch)} из {len(images)}"
              await _edit_message_quietly(status, status_text)
              fetch_urls = _slideshow_slide_urls(media_data, images_to_fetch)
              downloaded = list(await asyncio.gather(
                   *(_download_url_bin(session, u, headers=headers) for u in fetch_urls)
              ))
              video_indices = [idx for idx, b in enumerate(downloaded) if b and _looks_like_video_bytes(b)]
              if video_indices:
                   log.info('[tiktok] In the slideshow, %d of %d slides were recognized as video (live_images/magic bytes).', len(video_indices), len(downloaded))
              probe_results: dict[int, tuple[int, int, int, bytes | None]] = {}
              if video_indices:
                   probed = await asyncio.gather(*(_probe_and_thumbnail_from_bytes(downloaded[i]) for i in video_indices))
                   probe_results = dict(zip(video_indices, probed))
              media_items: list[Any] = []
              for idx, item_bytes in enumerate(downloaded):
                   if not item_bytes:
                        continue
                   if idx in probe_results:
                        duration, width, height, thumb_bytes = probe_results[idx]
                        video_kwargs: dict[str, Any] = {
                             "media": BufferedInputFile(item_bytes, filename=f"slide_{idx}.mp4"),
                             "supports_streaming": True,
                        }
                        if duration:
                             video_kwargs["duration"] = duration
                        if width and height:
                             video_kwargs["width"] = width
                             video_kwargs["height"] = height
                        if thumb_bytes:
                             video_kwargs["thumbnail"] = BufferedInputFile(thumb_bytes, filename=f"slide_{idx}_thumb.jpg")
                        media_items.append(InputMediaVideo(**video_kwargs))
                   else:
                        media_items.append(InputMediaPhoto(media=BufferedInputFile(item_bytes, filename=f"photo_{idx}.jpg")))
              if media_items:
                   await _delete_message_quietly(status)
                   if len(media_items) == 1:
                        only_item = media_items[0]
                        if isinstance(only_item, InputMediaVideo):
                             await bot.send_video(
                                  chat_id=message.chat.id, video=only_item.media,
                                  supports_streaming=True, reply_to_message_id=message.message_id,
                             )
                        else:
                             await bot.send_photo(
                                  chat_id=message.chat.id, photo=only_item.media,
                                  reply_to_message_id=message.message_id,
                             )
                   else:
                        chunks = _chunk_tiktok_media_items(media_items)
                        for chunk_idx, chunk in enumerate(chunks):
                             await bot.send_media_group(
                                  chat_id=message.chat.id, media=chunk,
                                  reply_to_message_id=message.message_id if chunk_idx == 0 else None,
                             )
                             if chunk_idx + 1 < len(chunks):
                                  await asyncio.sleep(0.3)
                   await _send_tiktok_music(session, media_data, message, author, headers)
                   return

         video_candidates = _tiktok_video_candidates(media_data)
         if video_candidates:
              hit_size_limit = False
              for candidate in video_candidates:
                   if candidate["size"] and candidate["size"] > TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES:
                        hit_size_limit = True
                        log.info(
                             '[tiktok] Skipping variant %s (%s) — known size %.1f MB exceeds the Telegram Bot API limit.',
                             candidate["key"], candidate["label"], candidate["size"] / (1024 * 1024),
                        )
                        continue
                   if candidate["key"] == "hdplay":
                        status_msg = "Скачиваю видео без водяных знаков (HD)"
                   elif candidate["key"] == "wmplay":
                        status_msg = "Версии без водяных знаков не нашлось — скачиваю как есть"
                   else:
                        status_msg = "Скачиваю видео без водяных знаков"
                   await _edit_message_quietly(status, status_msg)

                   video_bytes = await _download_url_bin(session, candidate["url"], headers=headers)
                   if not video_bytes:
                        continue

                   duration, width, height = 0, 0, 0
                   thumb_bytes = None
                   try:
                        with tempfile.TemporaryDirectory() as tdir:
                             raw_path = os.path.join(tdir, "raw_tiktok.mp4")
                             with open(raw_path, "wb") as f:
                                  f.write(video_bytes)
                             duration, width, height = await _probe_video_dimensions(raw_path)
                             thumb_bytes = await _generate_video_thumbnail(raw_path, duration)
                   except Exception as probe_exc:
                        log.warning("[tiktok] Video metadata probe failed, sending without: %s", probe_exc)

                   send_kwargs: dict[str, Any] = {
                        "chat_id": message.chat.id,
                        "video": BufferedInputFile(video_bytes, filename="tiktok.mp4"),
                        "reply_to_message_id": message.message_id,
                        "supports_streaming": True,
                   }
                   if duration:
                        send_kwargs["duration"] = duration
                   if width and height:
                        send_kwargs["width"] = width
                        send_kwargs["height"] = height
                   if thumb_bytes:
                        send_kwargs["thumbnail"] = BufferedInputFile(thumb_bytes, filename="thumb.jpg")

                   try:
                        await bot.send_video(**send_kwargs)
                   except TelegramEntityTooLarge:
                        hit_size_limit = True
                        log.warning(
                             '[tiktok] Variant %s (%s, %d bytes) exceeded the Telegram limit when sending — trying the next quality option.',
                             candidate["key"], candidate["label"], len(video_bytes),
                        )
                        continue

                   await _delete_message_quietly(status)
                   await _send_tiktok_music(session, media_data, message, author, headers)
                   return

              if hit_size_limit:
                   raise TikTokUserFacingError(
                        "Это видео из TikTok слишком большое для отправки даже в самом лёгком из доступных "
                        "качеств — Telegram Bot API ограничивает загрузку файлов 50 МБ. Попробуйте скачать "
                        "это видео другим способом."
                   )

         raise TikTokUserFacingError("Ссылка распознана, но TikTok не отдал ни видео, ни фото по ней — возможно, контент удалён или недоступен.")
    except Exception as exc:
         log.exception("TikTok download fail:")
         if isinstance(exc, TelegramEntityTooLarge):
              err_text = "Видео слишком большое для отправки через бота — Telegram Bot API ограничивает загрузку файлов 50 МБ. Попробуйте скачать это видео другим способом."
         elif isinstance(exc, TikTokUserFacingError):
              err_text = str(exc)
         else:
              err_text = "Не получилось скачать это видео или слайдшоу из TikTok. Попробуйте другую ссылку или повторите чуть позже."
         edited = await _edit_message_quietly(status, err_text)
         if not edited:
              await _safe_reply(message, err_text)

async def _gemini_history_contents(history: list[dict]) -> list[types.Content]:
    contents: list[types.Content] = []
    for item in history:
        r = "model" if str(item.get("role")).lower() in {"assistant", "model"} else "user"
        cont = item.get("content") or ""
        txt = _or_extract_text(cont) if isinstance(cont, (dict, list)) else str(cont).strip()
        if txt:
             contents.append(types.Content(role=r, parts=[types.Part.from_text(text=txt)]))
    return contents

def _build_gemini_call_config(model_id: str, contents: list[types.Content]) -> tuple[list[types.Content], "types.GenerateContentConfig | None"]:
    conf = GEMINI_MODELS.get(model_id, {})
    kwargs: dict[str, Any] = {}
    if not conf.get("no_system"):
        kwargs["system_instruction"] = get_system_prompt(model_id)
    tools_list = []
    if not conf.get("no_search"):
        if conf.get("search_grounding", True):
            try:
                tools_list.append(types.Tool(google_search=types.GoogleSearch()))
            except Exception:
                pass
        if conf.get("map_grounding", False):
            try:
                tools_list.append(types.Tool(google_maps=types.GoogleMaps()))
            except Exception:
                pass
        if conf.get("url_context", True):
            try:
                tools_list.append(types.Tool(url_context=types.UrlContext()))
            except Exception:
                pass
    if tools_list:
        kwargs["tools"] = tools_list
    if not conf.get("no_system"):
        last_text = next((p.text for p in reversed(contents[-1].parts) if getattr(p, "text", None)), "") if contents else ""
        if not _looks_like_heavy_query(last_text):
            if model_id.startswith("gemini-3"):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
            elif model_id.startswith("gemini-2.5"):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    gconfig = types.GenerateContentConfig(**kwargs) if kwargs else None

    call_contents = contents
    if conf.get("no_system"):
        _now_date = datetime.now().strftime("%d %B %Y")
        _now_year = datetime.now().year
        _identity_text = (
            f"{get_system_prompt(model_id)}\n\n"
            f"Из всего вышеперечисленного особенно запомни на весь наш разговор:\n"
            f"1. Твоё имя — Lumen.\n"
            f"Подтверди что понял инструкции."
        )
        _identity_ctx = [
            types.Content(role="user", parts=[types.Part.from_text(text=_identity_text)]),
            types.Content(role="model", parts=[types.Part.from_text(
                text=f"Понял. Я — Lumen, создан @SilverElixir. Сегодня {_now_date}, год {_now_year}. Буду отвечать кратко.")]),
        ]
        call_contents = _identity_ctx + contents

        if len(contents) > 12:
            _reminder = types.Content(role="user", parts=[types.Part.from_text(
                text="[Напоминание перед ответом: ты — Lumen, не называй себя Gemini/Gemma/Google.]"
            )])
            _reminder_ack = types.Content(role="model", parts=[types.Part.from_text(text="Понял, помню.")])
            call_contents = call_contents[:-1] + [_reminder, _reminder_ack] + call_contents[-1:]

    return call_contents, gconfig

async def ask_gemini(
    chat_id: int, user_text: str, media: list[tuple[bytes, str]] | None = None,
    youtube_url: str | None = None, model_chain: list[str] | None = None,
    deadline: float | None = None,
) -> str:
    state = get_state(chat_id)
    chain = list(model_chain) if model_chain else list(GEMINI_DEFAULT_CHAIN)
    if not chain:
        chain = list(GEMINI_DEFAULT_CHAIN)
    if deadline is None:
        deadline = time.monotonic() + ROUTE_TOTAL_BUDGET_SEC
    hist = state.setdefault("history", [])
    ctx = state.get("ctx", deque())

    extra_parts: list[types.Part] = []
    if media:
        for b, mime in media:
             if _is_gemini_supported_mime(mime):
                 extra_parts.append(types.Part.from_bytes(data=b, mime_type=mime))
             else:
                 raise ValueError(f"Тип вложения '{mime}' не поддерживается для анализа. Отправьте картинку, аудиозапись, видео, PDF или текстовый документ.")
    if youtube_url:
         extra_parts.append(types.Part.from_uri(file_uri=youtube_url, mime_type="video/*"))
    contents = await _build_gemini_turn_contents(chat_id, user_text, extra_parts=extra_parts or None)

    resp = None
    curr_model_id = chain[0]
    tried_models: set[str] = set()
    quota_exhausted_models: list[str] = []

    loop_guard = 0
    max_loop_guard = len(chain) + 4

    while True:
        loop_guard += 1
        if loop_guard > max_loop_guard:
            raise RuntimeError("Превышено допустимое число попыток обращения к Gemini API.")
        if time.monotonic() > deadline:
            log.warning('[gemini] Route time budget exhausted. Tried: %s', ", ".join(sorted(tried_models)) or "ничего")
            raise RouteBudgetExceededError(sorted(tried_models))
        tried_models.add(curr_model_id)
        call_contents, gconfig = _build_gemini_call_config(curr_model_id, contents)

        try:
            fut = asyncio.to_thread(client.models.generate_content, model=curr_model_id, contents=call_contents, config=gconfig)
            resp = await asyncio.wait_for(fut, timeout=ROUTE_MODEL_TIMEOUT_SEC)
            break
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            next_model = _next_fallback_model(tried_models, chain)
            if next_model:
                log.warning("[gemini] Model %s timed out (%.0fs). Switching to %s", curr_model_id, ROUTE_MODEL_TIMEOUT_SEC, next_model)
                curr_model_id = next_model
                continue
            log.warning("[gemini] Model %s timed out and no fallback models remain in route.", curr_model_id)
            raise
        except Exception as exc:
            txt = _error_text(exc).strip() or exc.__class__.__name__
            status_code = _error_status(exc, txt)
            kind = _classify_model_error(status_code, txt)
            exc_class = exc.__class__.__name__

            if kind == "rate_limit":
                _mark_quota_exhausted("gemini", curr_model_id)
                quota_exhausted_models.append(curr_model_id)
                next_model = _next_fallback_model(tried_models, chain)
                if next_model:
                    log.warning("[gemini] Model %s quota exhausted (429). Switching to %s", curr_model_id, next_model)
                    curr_model_id = next_model
                    continue
                log.warning("[gemini] All Gemini models in route exhausted their quota (429): %s", ", ".join(quota_exhausted_models))
                raise GeminiAllModelsExhaustedError(quota_exhausted_models) from exc

            next_model = _next_fallback_model(tried_models, chain)
            if next_model:
                reason = f"{kind}/{status_code}" if status_code else f"{kind}/{exc_class}"
                log.warning("[gemini] Model %s failed (%s). Switching to %s", curr_model_id, reason, next_model)
                curr_model_id = next_model
                continue
            log.warning("[gemini] Model %s failed (%s) and no fallback models remain in route.", curr_model_id, exc_class)
            raise

    if resp is None:
        raise RuntimeError("No response received from Gemini after retries.")
    log.info('[gemini] Successful response from model %s (models tried: %d)', curr_model_id, len(tried_models))

    ans = ""
    tool_calls: list[str] = []
    try:
        ans = getattr(resp, "text", "") or ""
    except Exception:
        ans = ""
    if not ans:
        reasons = []
        for cand in (getattr(resp, "candidates", []) or []):
            reasons.append(str(getattr(cand, "finish_reason", "UNKNOWN")))
            content = getattr(cand, "content", None)
            if content:
                parts = getattr(content, "parts", []) or []
                for part in parts:
                    part_text = getattr(part, "text", "") or ""
                    if part_text:
                         ans += part_text
                    fn_call = getattr(part, "function_call", None)
                    if fn_call:
                         fn_name = getattr(fn_call, "name", "tool")
                         fn_args = getattr(fn_call, "args", None) or getattr(fn_call, "arguments", None)
                         try:
                             fn_args_txt = json.dumps(_json_prune_defaults(fn_args), ensure_ascii=False) if fn_args is not None else "{}"
                         except Exception:
                             fn_args_txt = str(fn_args)
                         tool_calls.append(f"{fn_name}({fn_args_txt})")
                    fn_resp = getattr(part, "function_response", None)
                    if fn_resp and not part_text:
                         try:
                             tool_calls.append(f"response:{json.dumps(_json_prune_defaults(getattr(fn_resp, 'response', None)), ensure_ascii=False)}")
                         except Exception:
                             tool_calls.append("response")
        if not ans and tool_calls:
            ans = "[Tool call: " + "; ".join(tool_calls) + "]"
        elif not ans and reasons:
            if any("MALFORMED_FUNCTION_CALL" in r for r in reasons):
                try:
                    retry_gconfig = gconfig.model_copy(update={"tools": None}) if gconfig is not None else None
                    retry_fut = asyncio.to_thread(
                        client.models.generate_content, model=curr_model_id, contents=call_contents, config=retry_gconfig
                    )
                    retry_resp = await asyncio.wait_for(retry_fut, timeout=TELEGRAM_AI_TIMEOUT)
                    retry_text = getattr(retry_resp, "text", "") or ""
                    if retry_text.strip():
                        ans = retry_text
                        log.warning("[gemini] Model %s had MALFORMED_FUNCTION_CALL, retried without tools successfully.", curr_model_id)
                except Exception as retry_exc:
                    log.warning("[gemini] Retry without tools after MALFORMED_FUNCTION_CALL also failed: %s", retry_exc)
            if not ans:
                ans = f"[Ответ заблокирован или пуст. Причина: {', '.join(reasons)}]"
    ans = ans.strip() or "Empty response"
    ans = _scrub_identity_leak(ans, source=f"ask_gemini:{curr_model_id}")

    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": ans})
    if len(hist) > SHARED_HISTORY_MAX_LEN:
         del hist[:-SHARED_HISTORY_MAX_LEN]
    ctx.clear()
    _record_quota_usage("gemini", curr_model_id)
    return ans

async def _build_gemini_turn_contents(
    chat_id: int, user_text: str, extra_parts: list[types.Part] | None = None,
) -> list[types.Content]:
    state = get_state(chat_id)
    hist = state.setdefault("history", [])
    contents = await _gemini_history_contents(hist)
    ctx = state.get("ctx", deque())
    full_prompt = user_text
    if ctx:
        full_prompt = "Фон разговора в чате (для контекста, не обращение к тебе):\n" + "\n".join(ctx) + "\n\nТекущий вопрос/сообщение: " + user_text
    parts = [types.Part.from_text(text=full_prompt)]
    if extra_parts:
        parts.extend(extra_parts)
    contents.append(types.Content(role="user", parts=parts))
    return contents

def _build_openrouter_turn_messages(chat_id: int, user_text: str, model_id: str) -> list[dict]:
    state = get_state(chat_id)
    ctx = state.get("ctx", deque())
    full = ""
    if ctx:
        full = "Фон разговора:\n" + "\n".join(ctx) + "\n\nТекущий вопрос: "
    full += user_text
    history = state.setdefault("history", [])
    messages: list[dict] = [{"role": "system", "content": get_system_prompt(model_id)}]
    messages.extend(history)
    messages.append({"role": "user", "content": full})
    return messages

from lumen_typing_pace import (
    speed_key as _typing_speed_key,
    get_typing_speed as _get_typing_speed,
    record_observed_speed as _record_typing_speed,
    catchup_reveal_steps as _typing_catchup_steps,
)

_typing_sleep = asyncio.sleep

async def _gemini_stream_pieces(model_id: str, call_contents: list, gconfig):
    stream = await client.aio.models.generate_content_stream(model=model_id, contents=call_contents, config=gconfig)
    stream_iter = stream.__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=STREAM_CHUNK_TIMEOUT_SEC)
            except StopAsyncIteration:
                break
            try:
                piece = getattr(chunk, "text", "") or ""
            except Exception:
                piece = ""
            if piece:
                yield piece
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

async def _openrouter_stream_pieces(model_id: str, messages: list[dict]):
    if not OPENROUTER_API_KEY:
        raise OpenRouterAPIError("OPENROUTER_API_KEY не задан")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-OpenRouter-Title": OPENROUTER_TITLE,
        "Content-Type": "application/json",
    }
    session = await _get_http_session()
    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": True}
    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=None, connect=12.0)) as resp:
        if resp.status >= 400:
            body = await resp.read()
            raise OpenRouterAPIError(f"HTTP {resp.status}: {body[:300]!r}", status_code=resp.status)
        line_iter = resp.content.__aiter__()
        while True:
            try:
                raw_line = await asyncio.wait_for(line_iter.__anext__(), timeout=STREAM_CHUNK_TIMEOUT_SEC)
            except StopAsyncIteration:
                break
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                obj = json.loads(data_str)
            except Exception:
                continue
            err_obj = obj.get("error")
            if err_obj:
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                raise OpenRouterAPIError(err_msg or "OpenRouter вернул ошибку в теле стрима", status_code=err_code)
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece

async def _run_streaming_reply(
    chat_id: int, user_text: str, message: Message, *, provider: str, model_id: str, piece_agen,
) -> tuple[str | None, Message | None]:
    state = get_state(chat_id)
    hist = state.setdefault("history", [])
    ctx = state.get("ctx", deque())

    sent_messages: list[Message] = []
    full_text = ""
    last_edit_ts = 0.0
    last_edited_plain = ""
    pace_key = _typing_speed_key(provider, model_id)
    overall_start_ts = time.monotonic()
    current_chunk_start_ts = overall_start_ts

    try:
        placeholder = await _tg_call(message.reply, "…", call_timeout=TELEGRAM_REQUEST_TIMEOUT)
        if placeholder is None:
            return None, None
        sent_messages.append(placeholder)

        async for piece in piece_agen:
            if not piece:
                continue
            full_text += piece

            leak_kind = None
            _scan_text = _leak_scan_window(full_text, piece)
            if _detect_identity_leak(_scan_text):
                leak_kind = "identity"
            elif _detect_injected_payload_echo(_scan_text):
                leak_kind = "payload_echo"

            if leak_kind:
                tag = "identity-leak" if leak_kind == "identity" else "injection-echo"
                log.warning(
                    '[%s] Stream %s/%s started leaking internal details/echoing an injected instruction — aborting the stream and showing a neutral reply instead of the partially accumulated text: %r', tag, provider, model_id, full_text[:500],
                )
                aclose = getattr(piece_agen, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                final_answer = _IDENTITY_LEAK_FALLBACK if leak_kind == "identity" else _INJECTED_PAYLOAD_ECHO_FALLBACK
                await _tg_call(sent_messages[-1].edit_text, final_answer, parse_mode=None, call_timeout=15.0)
                hist.append({"role": "user", "content": user_text})
                hist.append({"role": "assistant", "content": final_answer})
                if len(hist) > SHARED_HISTORY_MAX_LEN:
                    del hist[:-SHARED_HISTORY_MAX_LEN]
                ctx.clear()
                _record_quota_usage(provider, model_id)
                return final_answer, None

            chunks = _split_text_chunks(full_text, TG_MAX_LEN)
            while len(chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                new_msg = await _tg_call(bot.send_message, chat_id=message.chat.id, text="…", call_timeout=TELEGRAM_REQUEST_TIMEOUT)
                if new_msg is None:
                    note = "\n\n[не удалось отправить продолжение сообщения]"
                    with contextlib.suppress(Exception):
                        await _tg_call(sent_messages[idx].edit_text, _md_to_html(chunks[idx]) + note, parse_mode=ParseMode.HTML, call_timeout=15.0)
                    final_answer = full_text.strip() or "Empty response"
                    hist.append({"role": "user", "content": user_text})
                    hist.append({"role": "assistant", "content": final_answer})
                    if len(hist) > SHARED_HISTORY_MAX_LEN:
                        del hist[:-SHARED_HISTORY_MAX_LEN]
                    ctx.clear()
                    _record_quota_usage(provider, model_id)
                    return final_answer, None
                await _tg_call(sent_messages[idx].edit_text, _md_to_html(chunks[idx]), parse_mode=ParseMode.HTML, call_timeout=15.0)
                sent_messages.append(new_msg)
                last_edited_plain = ""
                current_chunk_start_ts = time.monotonic()

            now = time.monotonic()
            target_full = chunks[-1] if chunks else ""
            typing_speed = _get_typing_speed(pace_key)
            reveal_len = min(len(target_full), max(0, int((now - current_chunk_start_ts) * typing_speed)))
            current_chunk_text = target_full[:reveal_len]
            if now - last_edit_ts >= STREAM_EDIT_MIN_INTERVAL_SEC and current_chunk_text != last_edited_plain:
                await _tg_call(sent_messages[-1].edit_text, current_chunk_text, parse_mode=None, call_timeout=15.0)
                last_edited_plain = current_chunk_text
                last_edit_ts = now

        if not full_text.strip():
            return None, sent_messages[-1]

        _record_typing_speed(pace_key, time.monotonic() - overall_start_ts, len(full_text))

        final_chunks = _split_text_chunks(full_text, TG_MAX_LEN)
        target_full = final_chunks[-1]
        already_shown_len = len(last_edited_plain) if last_edited_plain and target_full.startswith(last_edited_plain) else 0
        remaining_len = len(target_full) - already_shown_len
        if remaining_len > 0:
            typing_speed = _get_typing_speed(pace_key)
            for step_len in _typing_catchup_steps(remaining_len, typing_speed, STREAM_TYPING_TICK_SEC, STREAM_TYPING_MAX_CATCHUP_TICKS):
                await _typing_sleep(STREAM_TYPING_TICK_SEC)
                current_chunk_text = target_full[:already_shown_len + step_len]
                if current_chunk_text != last_edited_plain:
                    await _tg_call(sent_messages[-1].edit_text, current_chunk_text, parse_mode=None, call_timeout=15.0)
                    last_edited_plain = current_chunk_text

        final_text = final_chunks[-1]
        res = await _tg_call(sent_messages[-1].edit_text, _md_to_html(final_text), parse_mode=ParseMode.HTML, call_timeout=15.0)
        if res is None:
            await _tg_call(sent_messages[-1].edit_text, final_text, parse_mode=None, call_timeout=15.0)

    except Exception as exc:
        if not full_text.strip():
            log.warning('[stream] Stream %s/%s failed before showing any content, falling back to a regular call: %s', provider, model_id, exc)
            return None, (sent_messages[-1] if sent_messages else None)
        log.warning('[stream] Stream %s/%s failed after partially showing the response, finishing as-is: %s', provider, model_id, exc)
        if _detect_identity_leak(full_text):
            log.warning('[identity-leak] Leak caught by the fallback guard (%s_stream_exception_path): %r', provider, full_text[:500])
            full_text = _IDENTITY_LEAK_FALLBACK
            with contextlib.suppress(Exception):
                await _tg_call(sent_messages[-1].edit_text, _IDENTITY_LEAK_FALLBACK, parse_mode=None, call_timeout=15.0)
        elif _detect_injected_payload_echo(full_text):
            log.warning('[injection-echo] Injected-instruction echo caught by the fallback guard (%s_stream_exception_path): %r', provider, full_text[:500])
            full_text = _INJECTED_PAYLOAD_ECHO_FALLBACK
            with contextlib.suppress(Exception):
                await _tg_call(sent_messages[-1].edit_text, _INJECTED_PAYLOAD_ECHO_FALLBACK, parse_mode=None, call_timeout=15.0)
        else:
            with contextlib.suppress(Exception):
                chunks = _split_text_chunks(full_text, TG_MAX_LEN)
                final_text = chunks[-1] if chunks else full_text
                note = "\n\n[соединение прервалось — возможно, ответ неполный]"
                await _tg_call(sent_messages[-1].edit_text, _md_to_html(final_text + note), parse_mode=ParseMode.HTML, call_timeout=15.0)
    finally:
        aclose = getattr(piece_agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    final_answer = _scrub_identity_leak(full_text.strip() or "Empty response", source=f"{provider}_stream_final:{model_id}")
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": final_answer})
    if len(hist) > SHARED_HISTORY_MAX_LEN:
        del hist[:-SHARED_HISTORY_MAX_LEN]
    ctx.clear()
    _record_quota_usage(provider, model_id)
    return final_answer, None

async def _try_gemini_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    conf = GEMINI_MODELS.get(model_id, {})
    if not conf.get("stream", True):
        return None, None
    contents = await _build_gemini_turn_contents(chat_id, user_text)
    call_contents, gconfig = _build_gemini_call_config(model_id, contents)
    piece_agen = _gemini_stream_pieces(model_id, call_contents, gconfig)
    return await _run_streaming_reply(chat_id, user_text, message, provider="gemini", model_id=model_id, piece_agen=piece_agen)

async def _try_openrouter_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    messages = _build_openrouter_turn_messages(chat_id, user_text, model_id)
    piece_agen = _openrouter_stream_pieces(model_id, messages)
    return await _run_streaming_reply(chat_id, user_text, message, provider="openrouter", model_id=model_id, piece_agen=piece_agen)

_TIKTOK_RE = re.compile(r"(?:^|\.)tiktok\.com$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

def extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s]+", text)
    if not m:
         return None
    return m.group(0).rstrip(".,!?;:)>]'\"")

def is_tiktok(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return bool(_TIKTOK_RE.search(host.lower()))
    except Exception:
         return False

def is_youtube(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in _YOUTUBE_HOSTS
    except Exception:
         return False

def clean_mention(text: str) -> str:
    return re.sub(rf"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE).strip()

DRAW_TRIGGER_PREFIXES = [
    "сгенерируй картинку", "сгенерируй мне картинку", "сгенерируй изображение",
    "создай картинку", "создай изображение", "нарисуй картинку", "нарисуй изображение",
    "нарисуй мне", "нарисуй", "изобрази картинку", "изобрази", "нарисуй-ка",
    "сгенери картинку", "сгенери изображение", "можешь нарисовать", "можешь нарисовать мне",
]
TTS_TRIGGER_PREFIXES = [
    "озвучь текст", "озвучь мне", "озвуч текст", "озвучь", "озвуч",
    "переведи текст в голос", "переведи слова в голос", "переведи в голос",
    "переведи в аудио", "произнеси текст", "произнеси", "проговори",
    "скажи голосом", "переведи текст в аудио", "переведи слова в аудио",
    "преврати в аудио", "преврати текст в аудио", "преврати это в аудио",
    "конвертируй в аудио", "переведи в звук", "начитай текст", "начитай",
    "прочитай вслух", "сделай аудио", "сделай голосовое", "запиши голосовое",
]

def _match_trigger_prefix(text_lower: str, prefixes: list[str]) -> str | None:
    for prefix in prefixes:
        if text_lower.startswith(prefix):
            return prefix
    return None

_MEDIA_REFERENCE_RE = re.compile(
    r"\b(?:"
    r"(?P<sticker>стикер\w*)|"
    r"(?P<video>видео\w*|видос\w*|ролик\w*|клип\w*|gif\w*|гиф\w*)|"
    r"(?P<audio>аудио\w*|голосов\w*|войс\w*)|"
    r"(?P<photo>фото\w*|снимок\w*|изображени\w*|скрин\w*|скриншот\w*|картинк\w*)"
    r")\b",
    re.IGNORECASE,
)

def _media_reference_category(text: str) -> str | None:
    if not text:
        return None
    m = _MEDIA_REFERENCE_RE.search(text)
    if not m:
        return None
    return next(name for name, val in m.groupdict().items() if val is not None)

def _looks_like_media_reference(text: str) -> bool:
    return _media_reference_category(text) is not None

def _mime_matches_media_category(mime: str, category: str) -> bool:
    low = (mime or "").lower()
    if category == "sticker":
        return low == "image/webp"
    if category == "photo":
        return low.startswith("image/") and low != "image/webp"
    if category in ("video", "audio"):
        return low.startswith(f"{category}/")
    return False

def _find_recent_media_by_category(bucket: Any, category: str) -> tuple[str, str] | None:
    if not bucket:
        return None
    for fid, mime in reversed(bucket):
        if _mime_matches_media_category(mime, category):
            return fid, mime
    return None

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

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await _tg_call(
        message.reply,
        "<b>Lumen</b>\n\n"
        "Отвечаю на вопросы (с поиском в интернете, когда это нужно), читаю сайты и YouTube-видео по ссылке, разбираю фото, видео, аудио и документы, рисую изображения по описанию и озвучиваю текст.\n\n"
        "<b>Команды</b>\n"
        "/draw [описание] — нарисовать изображение\n"
        "/tts [текст] — озвучить текст\n"
        "/reset — очистить историю диалога\n\n"
        "Рисовать и озвучивать можно и просто словами, без команд — например «нарисуй кота» или «озвучь это».\n\n"
        "<b>TikTok</b>\n"
        "Пришлите ссылку — скачаю видео или фото без водяных знаков.\n\n"
        "Спрашивайте что угодно — я слушаю.",
        parse_mode=ParseMode.HTML,
    )

async def inline_draw(message: Message, prompt: str) -> None:
    status = await _tg_call(message.reply, "Генерирую изображение")
    try:
        session = await _get_http_session()
        primary_model = _pick_image_model(prompt)

        all_model_ids = list(HF_IMAGE_MODELS.keys())
        fallback_chain = [primary_model] + [m for m in all_model_ids if m != primary_model]

        image_bytes = None
        used_model = primary_model
        last_error = None

        for attempt_model in fallback_chain:
            try:
                if attempt_model != primary_model:
                    await _edit_message_quietly(
                        status,
                        f"Модель {_image_model_label(primary_model)} недоступна, пробую {_image_model_label(attempt_model)}"
                    )
                else:
                    await _edit_message_quietly(status, f"Использую модель {_image_model_label(attempt_model)}")
                image_bytes = await _hf_text_to_image(session, attempt_model, prompt)
                used_model = attempt_model
                break
            except Exception as exc:
                last_error = exc
                txt = _error_text(exc).lower()
                if any(kw in txt for kw in ("cannot connect", "ssl:", "no address", "503", "502", "timeout", "host")):
                    log.warning("[draw] Model %s failed (%s), trying next fallback", attempt_model, type(exc).__name__)
                    continue
                log.warning("[draw] Model %s error: %s, trying next", attempt_model, exc)
                continue

        if image_bytes:
            await _delete_message_quietly(status)
            caption = f"Модель: {_html_mod.escape(_image_model_label(used_model), quote=False)}"
            if used_model != primary_model:
                caption += "\n(основная модель недоступна)"
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=BufferedInputFile(image_bytes, filename="generated.jpg"),
                caption=caption,
                reply_to_message_id=message.message_id,
            )
        else:
            raise last_error or RuntimeError("Все модели генерации недоступны")

    except Exception as exc:
        log.exception("Hugging Face image generation failed:")
        txt = _error_text(exc).strip()
        if any(kw in txt.lower() for kw in ("cannot connect", "ssl:", "no address", "connection", "timeout", "host")):
            user_err = "Сервис генерации изображений временно недоступен. Попробуйте позже."
        elif "все модели" in txt.lower():
            user_err = "Все модели генерации изображений сейчас недоступны. Попробуйте позже."
        else:
            user_err = "Ошибка генерации изображения. Попробуйте ещё раз или переформулируйте описание."
        await _edit_message_quietly(status, user_err)


@dp.message(Command("draw"))
async def cmd_draw(message: Message) -> None:
    prompt = message.text.partition(" ")[2].strip() if message.text else ""
    if not prompt:
        await _safe_reply(message, "Укажите текст после команды /draw. Пример: /draw космическая станция")
        return
    await inline_draw(message, prompt)

from lumen_tts import (
    pcm_to_wav,
    _fish_audio_tts_bytes as _lumen_fish_audio_tts_bytes,
    _gemini_tts_bytes as _lumen_gemini_tts_bytes,
)


async def _fish_audio_tts_bytes(text: str) -> bytes | None:
    session = await _get_http_session()
    return await _lumen_fish_audio_tts_bytes(
        session, text,
        api_key=OPENROUTER_API_KEY, http_referer=OPENROUTER_HTTP_REFERER,
        title=OPENROUTER_TITLE, base_url=OPENROUTER_BASE_URL,
        model_id=FISH_AUDIO_TTS_MODEL, request_timeout_sec=ROUTE_MODEL_TIMEOUT_SEC,
    )


async def _gemini_tts_bytes(text: str) -> tuple[bytes, str, str]:
    def _is_rate_limit(e: Exception) -> bool:
        err_txt = _error_text(e).strip() or e.__class__.__name__
        return _classify_model_error(_error_status(e, err_txt), err_txt) == "rate_limit"

    return await _lumen_gemini_tts_bytes(
        client, text, tts_models=GEMINI_TTS_MODELS,
        is_rate_limit_error=_is_rate_limit,
        on_model_exhausted=lambda mname: _mark_quota_exhausted("gemini", mname),
        on_model_success=lambda mname: _record_quota_usage("gemini", mname),
    )


async def inline_tts(message: Message, text: str) -> None:
    if len(text) > TTS_MAX_CHARS:
        await _safe_reply(
            message,
            f"Текст слишком длинный для озвучки (лимит {TTS_MAX_CHARS} символов, сейчас {len(text)}). Сократите текст и попробуйте снова."
        )
        return
    status = await _tg_call(message.reply, "Озвучиваю текст")
    try:
        fish_bytes = await _fish_audio_tts_bytes(text)
        if fish_bytes is not None:
            pcm_bytes, mime_type, used_tts_model = fish_bytes, "audio/mp3", FISH_AUDIO_TTS_MODEL
            _record_quota_usage("openrouter", FISH_AUDIO_TTS_MODEL)
        else:
            pcm_bytes, mime_type, used_tts_model = await _gemini_tts_bytes(text)
        log.info('[tts] Synthesis received from %s, mime_type=%s, bytes=%d', used_tts_model, mime_type, len(pcm_bytes))

        if mime_type.startswith("audio/mp3") or mime_type.startswith("audio/mpeg") or pcm_bytes.startswith(b'ID3') or pcm_bytes.startswith(b'\xff\xfb'):
            src_ext = ".mp3"
            raw_audio = pcm_bytes
        elif pcm_bytes.startswith(b'RIFF') or "wav" in mime_type:
            src_ext = ".wav"
            raw_audio = pcm_bytes
        else:
            src_ext = ".wav"
            raw_audio = pcm_to_wav(pcm_bytes, sample_rate=24000)

        final_audio = raw_audio
        final_filename = "speech.ogg"
        voice_duration = 0
        try:
            with tempfile.TemporaryDirectory() as tdir:
                src_path = os.path.join(tdir, f"tts_src{src_ext}")
                dst_path = os.path.join(tdir, "tts_out.ogg")
                with open(src_path, "wb") as fh:
                    fh.write(raw_audio)
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", src_path,
                    "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
                    dst_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
                if os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
                    with open(dst_path, "rb") as fh:
                        final_audio = fh.read()
                    try:
                        probe = await asyncio.create_subprocess_exec(
                            "ffprobe", "-v", "error",
                            "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1",
                            dst_path,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        probe_out, _ = await asyncio.wait_for(probe.communicate(), timeout=10)
                        raw_dur = probe_out.decode().strip()
                        voice_duration = max(1, round(float(raw_dur))) if raw_dur else 0
                    except Exception as probe_exc:
                        log.warning("[tts] ffprobe failed: %s", probe_exc)
                    log.info("[tts] OGG/Opus: %d bytes, duration: %ds", len(final_audio), voice_duration)
                else:
                    log.warning("[tts] ffmpeg OGG conversion failed, falling back to raw audio")
                    final_filename = f"speech{src_ext}"
        except Exception as conv_exc:
            log.warning("[tts] ffmpeg conversion error: %s", conv_exc)
            final_filename = f"speech{src_ext}"

        await _delete_message_quietly(status)
        await bot.send_voice(
            chat_id=message.chat.id,
            voice=BufferedInputFile(final_audio, filename=final_filename),
            duration=voice_duration if voice_duration > 0 else None,
            reply_to_message_id=message.message_id
        )
    except Exception as exc:
        log.exception("TTS synthesis failed:")
        txt = _error_text(exc).strip() or exc.__class__.__name__
        kind = _classify_model_error(_error_status(exc, txt), txt)
        if kind == "rate_limit":
            user_err = "Лимит запросов на озвучку временно исчерпан. Попробуйте немного позже."
        else:
            user_err = "Не получилось озвучить текст. Попробуйте ещё раз или сократите текст."
        await _edit_message_quietly(status, user_err)

@dp.message(Command("tts"))
async def cmd_tts(message: Message) -> None:
    text = message.text.partition(" ")[2].strip() if message.text else ""
    if not text:
        await _safe_reply(message, "Укажите текст после команды /tts. Пример: /tts Добрый день")
        return
    await inline_tts(message, text)

@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    requester_id = message.from_user.id if message.from_user else None
    if not await _is_privileged_in_chat(message.chat.type, message.chat.id, requester_id):
        await _tg_call(
            message.reply,
            "В группе сбросить историю может только администратор или создатель группы (либо владелец бота). В личных сообщениях доступно всем."
        )
        return
    state = get_state(message.chat.id)
    state["history"] = []
    state["ctx"].clear()
    mark_state_dirty(message.chat.id)
    await _tg_call(
        message.reply,
        "История диалога в этом чате очищена. Начинаем с чистого листа."
    )

@dp.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    is_owner = _is_owner(message.from_user.id if message.from_user else None)

    if not is_owner:
         await _tg_call(message.reply, "У вас нет доступа к этой команде.")
         return

    if message.chat.type != ChatType.PRIVATE:
        await _tg_call(message.reply, "Эта команда показывает технические логи — доступна только в личных сообщениях с ботом, не в группах.")
        return

    try:
        for handler in logging.getLogger().handlers:
            handler.flush()
    except Exception:
        pass

    try:
        log_content = ""
        if LOG_FILE_PATH.exists():
            with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                log_content = f.read()

        if not log_content or len(log_content.strip()) == 0:
            await _tg_call(message.reply, "Лог-файл пуст или ещё не был создан.")
            return

        for secret in _redactable_secrets():
            log_content = log_content.replace(secret, "<REDACTED>")

        tmp_dir = tempfile.gettempdir()
        temp_log_path = os.path.join(tmp_dir, "logs.txt")
        with open(temp_log_path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(log_content)

        await _tg_call(message.reply_document, FSInputFile(temp_log_path, filename="logs.txt"))

        try:
            os.unlink(temp_log_path)
        except Exception:
            pass
    except Exception as exc:
        log.exception("Error extracting or sending logs:")
        await _tg_call(message.reply, f"Ошибка при отправке логов: {exc}")

@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    is_owner = _is_owner(message.from_user.id if message.from_user else None)
    if not is_owner:
        await _tg_call(message.reply, "У вас нет доступа к этой команде.")
        return

    if message.chat.type != ChatType.PRIVATE:
        await _tg_call(message.reply, "Эта команда показывает техническую статистику — доступна только в личных сообщениях с ботом, не в группах.")
        return

    _reset_quota_if_new_day()

    total_chats = len(chat_state)
    uptime_sec = int(time.monotonic() - _PROCESS_START_MONOTONIC)
    uptime_str = f"{uptime_sec // 3600}ч {(uptime_sec % 3600) // 60}м"

    gemini_quota = GLOBAL_QUOTA.get("gemini", {})
    gemini_lines = [
        f"  • {mid}: {e.get('used', 0)}{' (лимит исчерпан)' if e.get('exhausted_at') else ''}"
        for mid, e in sorted(gemini_quota.items(), key=lambda kv: -(kv[1].get("used") or 0))
    ]
    gemini_text = "\n".join(gemini_lines) or "  нет данных"

    or_quota = GLOBAL_QUOTA.get("openrouter", {})
    or_lines = [
        f"  • {mid}: {e.get('used', 0)}{' (лимит исчерпан)' if e.get('exhausted_at') else ''}"
        for mid, e in sorted(or_quota.items(), key=lambda kv: -(kv[1].get("used") or 0))
    ]
    or_text = "\n".join(or_lines) or "  нет данных"

    proxy_line = _tg_proxy_breaker.status_text()

    quota_day = GLOBAL_QUOTA.get("quota_day") or "—"

    text = (
        f"<b>Статистика Lumen</b>\n\n"
        f"Активных чатов: {total_chats}\n"
        f"Аптайм процесса: {uptime_str}\n"
        f"Счётчики квоты за сутки: {quota_day} (America/Los_Angeles, сбрасываются автоматически)\n\n"
        f"<b>Gemini — запросов по моделям:</b>\n{gemini_text}\n\n"
        f"<b>OpenRouter — запросов по моделям:</b>\n{or_text}"
        f"{proxy_line}"
    )
    await _tg_call(message.reply, text, parse_mode=ParseMode.HTML)

def _route_error_reply_text(exc: Exception, head_model: str, *, youtube_url_to_analyze: str | None) -> str:
    if youtube_url_to_analyze:
        return (
            "Не получилось открыть это видео (возможно, оно приватное, удалено, слишком длинное "
            "или недоступно для анализа). Опишите, пожалуйста, о чём оно словами — тогда смогу помочь."
        )
    if isinstance(exc, GeminiAllModelsExhaustedError):
        return _gemini_error_msg(exc, head_model)
    if isinstance(exc, RouteBudgetExceededError):
        return "Сейчас все доступные модели перегружены или недоступны. Попробуйте, пожалуйста, ещё раз через минуту."
    if isinstance(exc, OpenRouterAPIError):
        return _or_error_msg(exc, "text")
    return _gemini_error_msg(exc, head_model)


class RouteBudgetExceededError(RuntimeError):
    def __init__(self, tried: list[str]) -> None:
        self.tried = tried
        super().__init__(f"Бюджет времени на маршрут исчерпан. Испробовано: {', '.join(tried) or '—'}")


async def _run_route(
    chat_id: int, ai_prompt: str, route: list[tuple[str, str]], message: Message, *,
    media: list[tuple[bytes, str]] | None = None, media_filename: str = "",
    youtube_url: str | None = None, allow_stream: bool = False,
) -> tuple[str, bool]:
    if not route:
        raise RuntimeError("Пустой маршрут — не из чего выбирать модель.")
    deadline = time.monotonic() + ROUTE_TOTAL_BUDGET_SEC

    groups: dict[str, list[str]] = {"gemini": [], "openrouter": []}
    for provider, model_id in route:
        groups[provider].append(model_id)
    first_provider = route[0][0]
    provider_order = [first_provider, "openrouter" if first_provider == "gemini" else "gemini"]

    tried_stream_model: str | None = None
    tried_stream_provider: str | None = None
    reusable_placeholder: Message | None = None
    if allow_stream and route:
        head_model = route[0][1]
        if first_provider == "gemini" and GEMINI_MODELS.get(head_model, {}).get("stream", True):
            streamed, placeholder = await _try_gemini_streaming(chat_id, ai_prompt, message, head_model)
            if streamed is not None:
                log.info('[router] chat=%s response received via streaming (gemini:%s)', chat_id, head_model)
                return streamed, True
            tried_stream_model, tried_stream_provider = head_model, "gemini"
            reusable_placeholder = placeholder
        elif first_provider == "openrouter":
            streamed, placeholder = await _try_openrouter_streaming(chat_id, ai_prompt, message, head_model)
            if streamed is not None:
                log.info('[router] chat=%s response received via streaming (openrouter:%s)', chat_id, head_model)
                return streamed, True
            tried_stream_model, tried_stream_provider = head_model, "openrouter"
            reusable_placeholder = placeholder

    is_video_or_audio = bool(media) and not media[0][1].startswith("image/")
    last_exc: Exception | None = None
    for provider in provider_order:
        ids = list(groups.get(provider) or [])
        if provider == tried_stream_provider and tried_stream_model in ids:
            ids = [m for m in ids if m != tried_stream_model]
        if not ids:
            continue
        if provider == "openrouter" and is_video_or_audio:
            continue
        if time.monotonic() > deadline:
            log.warning('[router] Route time budget exhausted before trying provider %s.', provider)
            break
        try:
            if provider == "gemini":
                ans = await ask_gemini(chat_id, ai_prompt, media=media, youtube_url=youtube_url, model_chain=ids, deadline=deadline)
            elif media:
                ans = await ask_openrouter_multimodal(chat_id, ai_prompt, media[0], media_filename, model_chain=ids, deadline=deadline)
            else:
                ans = await ask_openrouter_text(chat_id, ai_prompt, model_chain=ids, deadline=deadline)

            if reusable_placeholder is not None:
                fits_one_message = len(_split_text_chunks(ans, TG_MAX_LEN)) == 1
                reused = fits_one_message and await _edit_message_quietly(reusable_placeholder, ans)
                if not reused:
                    await _delete_message_quietly(reusable_placeholder)
                reusable_placeholder = None
                if reused:
                    return ans, True
            return ans, False
        except Exception as exc:
            last_exc = exc
            log.warning('[router] Provider %s failed completely (%s), trying the next one on the route, if any.', provider, exc)

    if reusable_placeholder is not None:
        await _delete_message_quietly(reusable_placeholder)

    raise last_exc or RuntimeError("Не удалось получить ответ ни от одного кандидата маршрута.")


async def _process_media_group_buffers(mgid: str) -> None:
    await asyncio.sleep(0.8)
    messages = _mg_buffers.pop(mgid, [])
    _mg_tasks.pop(mgid, None)
    if not messages:
         return
    main_msg = messages[0]
    extra_media: list[tuple[bytes, str]] = []
    MAX_ALBUM_EXTRA = 9
    album_state = get_state(main_msg.chat.id)
    album_user_id = main_msg.from_user.id if main_msg.from_user else None
    for m in messages[1:1 + MAX_ALBUM_EXTRA]:
        src = _msg_media_source(m)
        if not src:
            continue
        fid, mime, _ = _media_file_id_and_mime(src)
        if not fid:
            continue
        fetched = await _fetch_media(fid, mime)
        if fetched:
            extra_media.append(fetched)
            _save_media_to_history(src, album_state, album_user_id)
    await _handle_message_core(main_msg, extra_media=extra_media or None)

def _record_passive_group_context(message: Message, state: dict[str, Any], t: str) -> None:
    if t.strip():
        username = message.from_user.username or message.from_user.first_name or "User"
        state["ctx"].append(f"@{username}: {t.strip()}")
    _save_media_to_history(_msg_media_source(message), state, message.from_user.id if message.from_user else None)
    mark_state_dirty(message.chat.id)


def _should_only_record_passively(message: Message, t: str, *, is_private: bool, is_guest: bool, mentioned: bool) -> bool:
    if is_private or is_guest or mentioned:
        return False
    url = extract_url(t)
    return not url or not is_tiktok(url)


def _check_and_register_rate_limit(user_id: int | None) -> bool:
    if not user_id:
        return False
    now = time.time()
    timestamps = user_rate_limits.setdefault(user_id, [])
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW_SEC:
        timestamps.pop(0)
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    timestamps.append(now)
    return False


async def _resolve_incoming_media(
    message: Message, state: dict[str, Any], clean_prompt: str, *, is_private: bool,
) -> tuple[str | None, str, str, tuple[bytes, str] | None]:
    asking_user_id = message.from_user.id if message.from_user else None
    media_src = _msg_media_source(message)
    med_path, med_mime, med_name = None, "", ""
    media_tuple = None

    if media_src:
        res = await _download_message_attachment_to_tmp(media_src)
        if res:
            med_path, med_mime, med_name = res
            _save_media_to_history(media_src, state, asking_user_id)
            with open(med_path, "rb") as f:
                media_tuple = (f.read(), med_mime)

    if media_tuple is None and message.reply_to_message is not None:
        reply_src = _msg_media_source(message.reply_to_message)
        if reply_src:
            reply_fid, reply_mime, _ = _media_file_id_and_mime(reply_src)
            if reply_fid:
                fetched = await _fetch_media(reply_fid, reply_mime)
                if fetched:
                    media_tuple = fetched

    if media_tuple is None and state.get("recent_media_ids"):
        category = _media_reference_category(clean_prompt)
        if category:
            buckets: dict[str, Any] = state["recent_media_ids"]
            own_bucket = buckets.get(str(asking_user_id)) if asking_user_id is not None else None
            fid_mime = None
            if own_bucket:
                fid_mime = _find_recent_media_by_category(own_bucket, category)
            elif is_private:
                for bucket in buckets.values():
                    fid_mime = _find_recent_media_by_category(bucket, category)
                    if fid_mime:
                        break
            if fid_mime:
                fid, mime = fid_mime
                fetched = await _fetch_media(fid, mime)
                if fetched:
                    media_tuple = fetched

    return med_path, med_mime, med_name, media_tuple


async def _handle_message_core(message: Message, extra_media: list[tuple[bytes, str]] | None = None) -> None:
    state = get_state(message.chat.id)
    t = message.text or message.caption or ""
    is_private = message.chat.type == ChatType.PRIVATE
    is_guest = is_guest_message(message)
    mentioned = message_mentions_bot(message)

    if _should_only_record_passively(message, t, is_private=is_private, is_guest=is_guest, mentioned=mentioned):
        _record_passive_group_context(message, state, t)
        return

    user_id = message.from_user.id if message.from_user else None
    if _check_and_register_rate_limit(user_id):
        await _tg_call(message.reply, "Вы отправляете слишком много запросов. Подождите немного.")
        return

    url = extract_url(t)
    needs_youtube = False
    needs_website = False
    youtube_url_to_analyze: str | None = None
    if url:
        if is_tiktok(url):
             await handle_tiktok(message, url)
             return
        if is_youtube(url):
             needs_youtube = True
             youtube_url_to_analyze = url
        else:
             needs_website = True

    clean_prompt = clean_mention(t).strip()

    if clean_prompt and _looks_like_injection_probe(clean_prompt):
        log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s: %r', message.chat.id, clean_prompt[:300])
        await _safe_reply(message, _INJECTION_PROBE_REPLY)
        return

    lower_prompt = clean_prompt.lower().strip()

    matched_draw_trigger = _match_trigger_prefix(lower_prompt, DRAW_TRIGGER_PREFIXES)
    matched_tts_trigger = _match_trigger_prefix(lower_prompt, TTS_TRIGGER_PREFIXES)

    if matched_draw_trigger:
        prompt_content = clean_prompt[len(matched_draw_trigger):].strip()
        prompt_content = re.sub(r'^[:\s\-\,]+', '', prompt_content).strip()
        if not prompt_content and message.reply_to_message is not None:
            reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if reply_text:
                prompt_content = reply_text
        if prompt_content:
            await inline_draw(message, prompt_content)
            return

    if matched_tts_trigger:
        tts_content = clean_prompt[len(matched_tts_trigger):].strip()
        tts_content = re.sub(r'^[:\s\-\,]+', '', tts_content).strip()
        if not tts_content and message.reply_to_message is not None:
            reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if reply_text:
                tts_content = reply_text
        if tts_content:
            await inline_tts(message, tts_content)
            return

    med_path, med_mime, med_name, media_tuple = await _resolve_incoming_media(
        message, state, clean_prompt, is_private=is_private,
    )

    if media_tuple and not clean_prompt:
         clean_prompt = _ensure_prompt_text(None, media_tuple[1])
    if youtube_url_to_analyze and not clean_prompt:
         clean_prompt = "Подробно перескажи и опиши содержание этого YouTube-видео."

    if not clean_prompt and not media_tuple and not youtube_url_to_analyze:
         if mentioned:
              await _tg_call(message.reply, "Слушаю вас.")
         return

    try:
        if not is_guest:
             await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
         pass

    media_mime = media_tuple[1] if media_tuple else None
    is_heavy = _looks_like_heavy_query(clean_prompt)
    needs_freshness = _looks_like_freshness_query(clean_prompt)
    route = _build_route(
        needs_youtube=needs_youtube, needs_website=needs_website,
        media_mime=media_mime, is_heavy=is_heavy, needs_freshness=needs_freshness,
    )
    log.info(
        '[router] chat=%s heavy=%s freshness=%s youtube=%s website=%s media=%s route=%s',
        message.chat.id, is_heavy, needs_freshness, needs_youtube, needs_website, media_mime,
        [f"{p}:{m}" for p, m in route],
    )

    ai_prompt = clean_prompt
    gemini_media_list = ([media_tuple] if media_tuple else []) + list(extra_media or [])
    gemini_media_list = gemini_media_list or None
    allow_stream = not gemini_media_list and not youtube_url_to_analyze
    try:
        ans, reply_already_sent = await _run_route(
            message.chat.id, ai_prompt, route, message,
            media=gemini_media_list, media_filename=med_name,
            youtube_url=youtube_url_to_analyze, allow_stream=allow_stream,
        )
        if not reply_already_sent:
            await _safe_reply(message, ans)
        mark_state_dirty(message.chat.id)
    except Exception as exc:
        log.exception("Chat AI processing failed:")
        head_model = route[0][1] if route else DEFAULT_GEMINI_MODEL
        if isinstance(exc, GeminiAllModelsExhaustedError):
            await _maybe_alert_gemini_exhausted()
        await _safe_reply(message, _route_error_reply_text(exc, head_model, youtube_url_to_analyze=youtube_url_to_analyze))
    finally:
        if med_path and os.path.exists(med_path):
             with contextlib.suppress(Exception):
                  os.unlink(med_path)

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
             _mg_tasks[mgid] = asyncio.create_task(_process_media_group_buffers(mgid))
        return

    chat_id = message.chat.id if message.chat else 0
    is_private = message.chat.type == ChatType.PRIVATE if message.chat else True
    is_guest = is_guest_message(message)
    mentioned = message_mentions_bot(message)

    if not is_private and not is_guest and not mentioned:
        await _handle_message_core(message)
        return

    lock = get_chat_lock(chat_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=45.0)
    except asyncio.TimeoutError:
        log.warning("[lock] Timeout waiting for lock on chat %s", chat_id)
        with contextlib.suppress(Exception):
             await _tg_call(message.reply, "Предыдущий запрос ещё обрабатывается. Подождите или попробуйте позже.")
        return

    try:
         await _handle_message_core(message)
    finally:
         with contextlib.suppress(Exception):
              lock.release()

async def _process_raw_update(raw_update: dict) -> None:
    if not isinstance(raw_update, dict):
         return
    try:
        guest = raw_update.get("guest_message")
        if isinstance(guest, dict):
            try:
                 msg_obj = Message.model_validate(guest, context={"bot": bot})
                 gq_id = guest.get("guest_query_id")
                 if gq_id is not None and not getattr(msg_obj, "guest_query_id", None):
                     with contextlib.suppress(Exception):
                         object.__setattr__(msg_obj, "guest_query_id", gq_id)
                 await _handle_message_core(msg_obj)
            except Exception as exc:
                 log.warning("[guest] Guest processing failed: %s", exc)
            return
        upd = Update.model_validate(raw_update, context={"bot": bot})
        await dp.feed_update(bot, upd)
    except Exception as exc:
        log.warning("[update] Raw update processing failed: %s", exc)

async def _webhook_startup() -> None:
    load_state_from_disk()
    log.info("Bot startup: webhook mode.")
    _check_temporary_free_models_expiry()
    _check_unconfirmed_model_quotas()
    _check_fish_audio_tts_expiry()
    _check_scheduled_removals_due()

    await asyncio.sleep(1.5)

    space_host = os.getenv("SPACE_HOST", "").strip()
    if not space_host:
        author = os.getenv("SPACE_AUTHOR_NAME", "silverelixir").lower()
        repo = os.getenv("SPACE_REPO_NAME", "lumen").lower()
        space_host = f"{author}-{repo}.hf.space"
    webhook_url = f"https://{space_host}/webhook"

    log.info("[webhook] Space URL: https://%s", space_host)
    log.info("[webhook] Webhook endpoint: %s", webhook_url)
    log.info('[webhook] WEBHOOK_SECRET (fingerprint): %s', _redact_secret(WEBHOOK_SECRET))
    log.info(
        '[admin] Full keys (WEBHOOK_SECRET/ADMIN_PANEL_KEY): curl -H "Authorization: Bearer <your BOT_TOKEN>" https://%s/admin_keys',
        space_host,
    )

    commands = [
        BotCommand(command="start", description="О боте и список команд"),
        BotCommand(command="reset", description="Очистить историю диалога"),
        BotCommand(command="draw", description="Нарисовать изображение по описанию"),
        BotCommand(command="tts", description="Озвучить текст"),
    ]

    async def try_setup():
        global BOT_USERNAME, OPENROUTER_HTTP_REFERER
        try:
            me_data = await telegram_api_call("getMe", {})
            if isinstance(me_data, dict) and me_data.get("username"):
                BOT_USERNAME = me_data["username"].strip().lstrip("@")
                log.info("[webhook] Dynamically fetched BOT_USERNAME: @%s", BOT_USERNAME)
                if not _OPENROUTER_HTTP_REFERER_ENV_SET:
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
            log.warning(
                '[webhook] setWebhook failed — register it manually via: curl -H "Authorization: Bearer <ADMIN_PANEL_KEY>" https://.../webhook_url (get ADMIN_PANEL_KEY via curl -H "Authorization: Bearer <BOT_TOKEN>" .../admin_keys if you don\'t have it handy): %s',
                exc,
            )

        try:
            cmd_payload = {
                "commands": [{"command": c.command, "description": c.description} for c in commands]
            }
            await asyncio.wait_for(
                telegram_api_call("setMyCommands", cmd_payload, request_timeout=15.0),
                timeout=18.0
            )
            log.info("[webhook] Bot commands set successfully.")
        except Exception as exc:
            log.warning("[webhook] setMyCommands failed: %s", exc)

    await try_setup()

    log.info("[webhook] Bot is running in webhook mode. Updates arrive via POST /webhook")
    _last_daily_check_date = date.today()
    while True:
        await asyncio.sleep(3600)
        _cleanup_rate_limit_dict()
        _reset_quota_if_new_day()
        today = date.today()
        if today != _last_daily_check_date:
            _last_daily_check_date = today
            _check_temporary_free_models_expiry()
            _check_unconfirmed_model_quotas()
            _check_fish_audio_tts_expiry()
            _check_scheduled_removals_due()
            await _probe_or_model_liveness()

async def main() -> None:
    global bot, client

    if TELEGRAM_API_BASE_URL != "https://api.telegram.org":
        api_server = TelegramAPIServer.from_base(TELEGRAM_API_BASE_URL)
        sess = IPv4AiohttpSession(api=api_server)
    else:
        sess = IPv4AiohttpSession()
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
        if _dirty_chat_ids or _index_dirty or _pending_chat_deletions:
            _flush_state_now()
        if _quota_dirty:
            save_global_quota()
        await _close_sessions()

if __name__ == "__main__":
    asyncio.run(main())
