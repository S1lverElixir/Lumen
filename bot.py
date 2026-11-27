"""Lumen is a Munito-based Telegram bot with Gemini fallback, in webhook mode.
Dialog history is 100 messages; TikTok goes through TikWM without watermarks;
image generation uses Cloudflare Workers AI with Pollinations fallback, and speech uses Gemini TTS.
"""

from __future__ import annotations

# Alias to prevent a duplicate module with `python bot.py` + `import bot`; otherwise there is a second instance,
# empty state/quotas and permanent 503 (production incident, September 2026).
import sys as _sys
_sys.modules.setdefault("bot", _sys.modules[__name__])
del _sys

import asyncio
import atexit
import contextlib
import contextvars
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any
import urllib.request as _urllib_request
from urllib.parse import urlsplit

import aiohttp
import sentry_sdk
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    Message,
)
from google import genai
from google.genai import types  # tests need it as bot.types (Content/Part assembly)

# language of the bot system messages (see lumen_lang.py): fixed strings,
# that the bot sends itself (/start, hints, errors, statuses). AI answers
# do not touch it — the model answers in the interlocutor's language.
from lumen_lang import (
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    t as _lang_t,
)

# setMyCommands accepts only two-letter ISO 639-1 codes (production incident:
# "fil" broke registration with 400 Bad Request). Filipino has no such code
# (fil — ISO 639-2); these users see commands in English by default.
COMMAND_LOCALES = [c for c in SUPPORTED_LANGS if c != DEFAULT_LANG and c != "fil"]

# logging

LOG_FILE_PATH = Path(os.getenv("BOT_LOG_PATH", "/app/bot.log"))
LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
_LOG_QUEUE: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
_LOG_QUEUE_HANDLER = logging.handlers.QueueHandler(_LOG_QUEUE)
_LOG_LISTENER: logging.handlers.QueueListener | None = None

_SECRET_NAMES = (
    "BOT_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY",
    "GROQ_API_KEY", "ADMIN_SECRET_SEED",
    "MUNITO_API_KEY",
    "_ADMIN_SECRET_SEED", "WEBHOOK_SECRET", "ADMIN_PANEL_KEY",
    "UPSTASH_REDIS_REST_TOKEN", "LUMEN_PROXY_SECRET", "CLOUDFLARE_API_TOKEN",
)


def _current_log_secrets() -> tuple[str, ...]:
    values = [value for name in _SECRET_NAMES
              for value in (globals().get(name), os.getenv(name, ""))
              if isinstance(value, str) and value]
    return tuple(sorted(set(values), key=len, reverse=True))


# Current-chat label on every processing line: aiogram prints only update
# id, making it impossible to reconstruct a single request's path from the log (prod 27.10.2026).
_LOG_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar("log_chat", default="")
# Source of the request contributing to model quarantine: two chats belonging
# to one person are
# one person, not two sources of bad answers (audit 16.11.2026).
_LOG_USER: contextvars.ContextVar[str] = contextvars.ContextVar("log_user", default="")

# Overall message deadline: every stage takes min(its timeout, remaining time), or
# lock + search + route + summary together took up to two minutes (audit 27.10.2026).
_MSG_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("msg_deadline", default=None)

# One update = one daily-limit charge. The second and third route passes
# (retry after [[SEARCH]], a freshness retry, auto-continuation) — this is the same
# a response to the person, not a new one: without the flag, 30 messages were consumed in 15 (audit 16.11.2026).
_ANSWER_COUNTED: contextvars.ContextVar[bool] = contextvars.ContextVar("answer_counted", default=False)

# Stopping an answer (audit 16.11.2026): /stop sets the flag; the stream loop exits at
# next chunk; partial text stays on screen and is saved to history. One
# active stream per chat; the user is the question's author, and refusal is silent.
_STREAM_STOPS: dict[int, dict[str, object]] = {}


def _begin_stream_stop_scope(chat_id: int, user_id: int | None, *, stream: bool = False) -> None:
    """Scope with depth: the outer one covers the entire update (so /stop works
    during the search phase too), the inner one covers streaming. The flag survives a nested scope."""
    entry = _STREAM_STOPS.get(chat_id)
    if entry is None:
        entry = {"event": asyncio.Event(), "user_id": user_id, "depth": 0,
                 "token": secrets.token_hex(4)}
        _STREAM_STOPS[chat_id] = entry
    if user_id is not None and entry.get("user_id") is None:
        entry["user_id"] = user_id
    depth = int(entry.get("depth") or 0)
    # The queue may open the external area of the next message before completion
    # the current one. Change the token only when entering a new stream, so the button
    # of the previous answer did not clear the next one (review 24.11.2026).
    if stream and depth == 1:
        entry["token"] = secrets.token_hex(4)
        entry["user_id"] = user_id
    entry["depth"] = depth + 1


def _end_stream_stop_scope(chat_id: int) -> None:
    entry = _STREAM_STOPS.get(chat_id)
    if not entry:
        return
    entry["depth"] = int(entry.get("depth") or 0) - 1
    if entry["depth"] <= 0:
        _STREAM_STOPS.pop(chat_id, None)


def _stream_stop_token(chat_id: int) -> str | None:
    entry = _STREAM_STOPS.get(chat_id)
    return str(entry.get("token")) if entry and entry.get("token") else None


def _stream_stop_requested(chat_id: int) -> bool:
    entry = _STREAM_STOPS.get(chat_id)
    event = entry.get("event") if entry else None
    return bool(event is not None and event.is_set())


def _consume_stream_stop(chat_id: int) -> None:
    """Stop completed: the flag and author are reset, otherwise the next queued update in the same chat silently turns off (second audit pass 16.11.2026)."""
    entry = _STREAM_STOPS.get(chat_id)
    if not entry:
        return
    event = entry.get("event")
    if event is not None:
        event.clear()
    entry["user_id"] = None


def _request_stream_stop(chat_id: int, user_id: int | None = None, *, token: str | None = None) -> bool:
    """True means the flag is set. Another person in a group cannot stop the answer. The token protects against a late press of a button from a previous answer."""
    entry = _STREAM_STOPS.get(chat_id)
    if not entry:
        return False
    if token is not None and entry.get("token") != token:
        return False
    owner = entry.get("user_id")
    if owner is not None and user_id is not None and user_id != owner and user_id != OWNER_ID:
        return False
    event = entry.get("event")
    if event is None or event.is_set():
        return False
    event.set()
    return True


def _msg_remaining_sec() -> float | None:
    """Time remaining before the message ceiling; None means processing outside handle_message."""
    deadline = _MSG_DEADLINE.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _deadline_within_message(seconds: float) -> float:
    """Stage deadline that does not exceed the message's overall cap."""
    now = time.monotonic()
    remaining = _msg_remaining_sec()
    return now + seconds if remaining is None else now + min(seconds, remaining)


class _SecretLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord, _secrets=_current_log_secrets) -> str:
        # Secrets through a closure, not a global: during interpreter finalization
        # module globals were already None, and formatting failed with TypeError.
        # Suppress teardown leftovers: a clean line is better than a noisy traceback.
        text = super().format(record)
        with contextlib.suppress(Exception):
            chat = _LOG_CHAT.get()
            if chat:
                text = f"[{chat}] {text}"
            for secret in _secrets():
                text = text.replace(secret, "<REDACTED>")
        return text


class _PublicLogFormatter(logging.Formatter):
    """Public HF Space logs: replace long numeric IDs (chats/users) with
    a stable hash. The file log retains full IDs for analysis (audit 28.10.2026)."""
    _ID_RE = re.compile(r"(?<!\d)(-?\d{6,})(?!\d)")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return self._ID_RE.sub(lambda m: "#" + hashlib.sha1(m.group(1).encode()).hexdigest()[:8], text)


def _setup_logging() -> logging.Logger:
    # LOG_LEVEL from env: previously INFO was hardcoded everywhere and suppressed all debug output.
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
    # stdout on a public Space hashes the id; the file (ephemeral, non-public)
    # remains with full IDs.
    console_handler.setFormatter(_PublicLogFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    file_handler.setFormatter(fmt)

    _LOG_QUEUE_HANDLER.setFormatter(_SecretLogFormatter())

    global _LOG_LISTENER
    # Before startup, stop the old QueueListener: otherwise threads multiply on one queue and hang the exit after "N passed" (sentinel race, 04.09.2026).
    if _LOG_LISTENER is not None:
        with contextlib.suppress(Exception):
            _LOG_LISTENER.stop()
    _LOG_LISTENER = logging.handlers.QueueListener(_LOG_QUEUE, file_handler, console_handler, respect_handler_level=True)
    _LOG_LISTENER.start()

    root.addHandler(_LOG_QUEUE_HANDLER)
    logging.captureWarnings(True)
    for name in ("httpx", "google_genai", "aiohttp", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # A single "bot" logger: __main__ in production diverged from lumen_*.py (visible in Sentry).
    logger = logging.getLogger("bot")
    logger.info("[setup] Logging: level=%s, file=%s", logging.getLevelName(level), LOG_FILE_PATH)
    return logger

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

# environment variables and configuration

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
    """Fixes a bare host with no scheme to https://; otherwise aiohttp fails on every call with an unclear error."""
    url = url.strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        # Real incident: TELEGRAM_API_BASE_URL was set to a bare host without a scheme,
        # aiohttp failed with a vague "Network error" on every call. Silently fix
        # the schema here: cheaper than diagnosing the same typo again.
        log.warning('[setup] Telegram proxy URL given without a scheme (%r) — adding https:// automatically.', url)
        url = "https://" + url
    return url

LUMEN_PROXY_SECRET = os.getenv("LUMEN_PROXY_SECRET", "")
TELEGRAM_API_BASE_URL = _normalize_telegram_base_url(os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org"))
log.info("[setup] Using Telegram API Base URL: %s", TELEGRAM_API_BASE_URL)

# Rotate a list of fallback proxies instead of relying on a single point of failure (audit, August 2026).
_TELEGRAM_PROXY_FALLBACKS = [
    _normalize_telegram_base_url(u) for u in os.getenv("TELEGRAM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]
_TELEGRAM_PROXY_CANDIDATES: list[str] = [TELEGRAM_API_BASE_URL] + [u for u in _TELEGRAM_PROXY_FALLBACKS if u != TELEGRAM_API_BASE_URL]
# Wake word in groups: “Lumen, ...” works as a mention. Turning off one
# of the variable, word synonyms — as a list (prod 18.11.2026 at the owner's request).
GROUP_WAKE_ENABLED = os.getenv("GROUP_WAKE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
GROUP_WAKE_WORDS = tuple(
    w.strip() for w in os.getenv("GROUP_WAKE_WORDS", "люмен,lumen").split(",") if w.strip()
) or ("люмен", "lumen")

def group_wake_request(text: str) -> tuple[bool, str]:
    """(whether it was addressed by a word, text without a name). Switch — GROUP_WAKE_ENABLED."""
    if not GROUP_WAKE_ENABLED or not text:
        return False, text
    return strip_group_wake_word(text, GROUP_WAKE_WORDS)


_telegram_proxy_idx = 0  # index of the currently active proxy in _TELEGRAM_PROXY_CANDIDATES
# Rotation mutates two globals at once — under the lock, or two concurrent failures
# move the index two steps ahead and skip a candidate (AUD-E-004).
_proxy_rotation_lock = asyncio.Lock()

# TikWM proxy base: the HF IP is banned with an empty 403; only the proxy fixes it (incident 11–12.08.2026).
# When empty, call both mirrors directly, as before.
TIKWM_API_BASE_URL = os.getenv("TIKWM_API_BASE_URL", "").strip().rstrip("/")
# Fallback proxies for TikWM use the same scheme as Telegram above: the fallback
# existed only for Telegram, although the failure point is shared. Empty by default,
# behavior without the setting does not change.
_TIKWM_API_BASE_URL_FALLBACKS = [
    u.strip().rstrip("/") for u in os.getenv("TIKWM_API_BASE_URL_FALLBACKS", "").split(",") if u.strip()
]

# Relays with a hard body limit (Vercel Hobby: 4.5 MB) are unsuitable for sending
# media. The host list is set in env; the aiogram session head is selected only
# from “large” relays, otherwise video/audio are guaranteed to fail (prod 17.11.2026).
_SMALL_BODY_PROXY_HOSTS = {
    h.strip().lower() for h in os.getenv("TELEGRAM_API_SMALL_BODY_HOSTS", "").split(",") if h.strip()
}


def _proxy_host(base_url: str) -> str:
    try:
        return (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return ""


def _pick_media_candidate(
    candidates: list[str], healthy: dict[str, bool], small_hosts: set[str] | None = None,
) -> str | None:
    """The first healthy base not marked "small"; if none exists — the first
    healthy one. Pure function: tests verify the selection without network access."""
    small = _SMALL_BODY_PROXY_HOSTS if small_hosts is None else small_hosts
    for base in candidates:
        if healthy.get(base) and _proxy_host(base) not in small:
            return base
    for base in candidates:
        if healthy.get(base):
            return base
    return None


async def _probe_telegram_base(base_url: str, timeout_sec: float = 5.0) -> bool:
    """getMe through the relay: 200=alive, anything else=not. The secret is in the header,
    the token is not logged (it is in the URL, so logs contain only the host)."""
    if not base_url or base_url == "https://api.telegram.org":
        return True
    url = f"{base_url}/bot{BOT_TOKEN}/getMe"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as probe:
            async with probe.get(url, headers={PROXY_AUTH_HEADER: LUMEN_PROXY_SECRET}) as resp:
                return resp.status == 200
    except Exception:
        return False


async def _choose_telegram_media_base() -> str | None:
    """Checks relays in parallel and returns the head for the aiogram session."""
    candidates = list(_TELEGRAM_PROXY_CANDIDATES)
    checks = await asyncio.gather(*(_probe_telegram_base(u) for u in candidates))
    healthy = dict(zip(candidates, checks))
    for base, ok in healthy.items():
        log.info("[telegram] Relay probe %s: %s", _proxy_host(base) or base, "ok" if ok else "down")
    return _pick_media_candidate(candidates, healthy)


_media_base_url = TELEGRAM_API_BASE_URL
_media_probe_failures = 0


async def _telegram_media_relay_watchdog() -> None:
    """Once a minute, checks the current media database and switches the aiogram session to a healthy "large" relay. Direct api.telegram.org is not touched."""
    global _media_base_url, _media_probe_failures
    while True:
        await asyncio.sleep(60)
        if _media_base_url in ("", "https://api.telegram.org"):
            return
        if await _probe_telegram_base(_media_base_url):
            _media_probe_failures = 0
            continue
        _media_probe_failures += 1
        if _media_probe_failures < 2:
            continue
        chosen = await _choose_telegram_media_base()
        _media_probe_failures = 0
        if chosen and chosen != _media_base_url:
            _media_base_url = chosen
            if bot is not None and getattr(bot, "session", None) is not None:
                with contextlib.suppress(Exception):
                    bot.session.api = TelegramAPIServer.from_base(chosen)
            log.warning("[telegram] Media relay switched to %s", _proxy_host(chosen) or "direct")
        elif not chosen:
            log.error("[telegram] No healthy media relay found; keeping %s", _proxy_host(_media_base_url))


def _tikwm_proxy_candidates() -> list[str]:
    """TikWM proxy addresses in attempt order: primary + fallbacks
    (no duplicates). An empty string ("" — TIKWM_API_BASE_URL is unset) means
    "no proxy, direct requests to both TikWM mirrors" — in that case the list
    is always the single element [""], because direct mode has no concept of a
    "fallback proxy" (it already tries both TikWM mirrors within the request;
    see _fetch_tikwm_media_data)."""
    if not TIKWM_API_BASE_URL:
        return [""]
    candidates = [TIKWM_API_BASE_URL] + [u for u in _TIKWM_API_BASE_URL_FALLBACKS if u != TIKWM_API_BASE_URL]
    return candidates

def _cloudflare_proxy_candidates() -> list[str]:
    """The Workers AI base URL enters proxy scopes only if it is actually a proxy:
the proxy secret must not be sent to api.cloudflare.com directly. An invalid or direct URL means
an empty list, not a crash of the shared session (created by middleware with validation)."""
    base = os.getenv("CLOUDFLARE_AI_BASE_URL", "").strip().rstrip("/")
    if not base:
        return []
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(base)
    except ValueError:
        return []
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.hostname == "api.cloudflare.com"
        or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
    ):
        return []
    return [base]

BOT_USERNAME = os.getenv("BOT_USERNAME", "LumenAI_bot").strip().lstrip("@")

def _env_number(name: str, default: float | int, *, cast: type = float,
                min_value: float | None = None, max_value: float | None = None) -> float | int:
    """Read a number from an environment variable with clear diagnostics.

    Previously, all timeouts and limits were read with bare int()/float(): a typo
    in an HF Variable (for example, ROUTE_MODEL_TIMEOUT_SEC=22s or an empty value)
    produced a bare ValueError at startup, meaning a crash loop with an unclear
    log instead of a clear message. Invalid values and values below the lower
    bound now fall back to the default with a WARNING (the bot keeps running)
    instead of crashing (audit 26.09.2026)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return cast(default)
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("[setup] %s=%r is not a number — using default %s", name, raw, default)
        return cast(default)
    if min_value is not None and value < min_value:
        log.warning("[setup] %s=%s is below the allowed minimum %s — using default %s", name, value, min_value, default)
        return cast(default)
    if max_value is not None and value > max_value:
        log.warning("[setup] %s=%s is above the allowed maximum %s — using default %s", name, value, max_value, default)
        return cast(default)
    return value

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Groq is only for Whisper voice transcription (_transcribe_audio); Groq chat models were removed from the code on 05.11.2026.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Munito AI — the primary text and vision provider since 05.11.2026 (measured by eval/munito_probe.py):
# zero retention by default, and training on prompts is prohibited. With no key, the route
# pretends the provider does not exist and answers only with Gemini.
MUNITO_API_KEY = os.getenv("MUNITO_API_KEY", "").strip()
MUNITO_BASE_URL = (os.getenv("MUNITO_BASE_URL", "").strip() or "https://api.munito.ai/inference/v1").rstrip("/")
# 3M tokens/day under the owner plan at ~11K input tokens per request is
# about 250 responses.
MUNITO_DAILY_LIMIT = _env_number("MUNITO_DAILY_LIMIT", 250, cast=int, min_value=1)
# For voice, do not forward the original Telegram file: its size is unknown (the Whisper API limit is 25 MB) and it may fail before the Gemini STT fallback.
VOICE_TRANSCRIBE_MAX_BYTES = _env_number("VOICE_TRANSCRIBE_MAX_BYTES", 10 * 1024 * 1024, cast=int, min_value=1024)
# ── Sentry (optional) — persistent error tracking across restarts ──
# Sentry: bot.log is lost on redeploy, and Upstash does not cover logs; it catches log.error without changing call sites.
def _redactable_secrets() -> tuple[str, ...]:
    """Reject /logs from Sentry (in debug mode, all Upstash keys and chat contents went there along with the logs)."""
    return _current_log_secrets()

def _sentry_scrub_secrets(event: dict, hint: dict) -> dict | None:
    """Sentry before_send hook: scrubs secrets. Defined unconditionally so it can be tested without a real DSN."""
    payload = json.dumps(event, default=str, ensure_ascii=False)
    for secret in _redactable_secrets():
        payload = payload.replace(secret, "<REDACTED>")
        # JSON escapes quotes/backslashes/control characters: the exact secret string
        # it is not found then and goes to Sentry (review 17.11.2026).
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        if escaped != secret:
            payload = payload.replace(escaped, "<REDACTED>")
    return json.loads(payload)

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        before_send=_sentry_scrub_secrets,
        # Error tracking only (tracing disabled to preserve Sentry's 5000 events/month quota).
        traces_sample_rate=0.0,
        send_default_pii=False,
        # Local variables and request bodies contain prompts and history:
        # before_send scrubs only exact secrets, so do not send them at all
        # (28.10.2026 audit).
        include_local_variables=False,
        max_request_body_size="never",
    )
    log.info('[setup] Sentry error tracking enabled.')

OWNER_ID: int | None = None
for env_name in ("OWNER_ID", "BOT_OWNER_ID", "ADMIN_ID", "TELEGRAM_OWNER_ID"):
    val = os.getenv(env_name, "").strip()
    if val and val.isdigit():
        OWNER_ID = int(val)
        break

TELEGRAM_REQUEST_TIMEOUT = _env_number("TELEGRAM_REQUEST_TIMEOUT", 45, min_value=1)
TELEGRAM_AI_TIMEOUT = _env_number("TELEGRAM_AI_TIMEOUT", 45, min_value=1)
TELEGRAM_MEDIA_TIMEOUT = _env_number("TELEGRAM_MEDIA_TIMEOUT", 25, min_value=1)
# Telegram download cap: the Bot API does not serve files larger than 20 MB via
# getFile/path (audit D1, 30.09.2026) — reject a larger value before downloading.
TELEGRAM_DOWNLOAD_MAX_BYTES = _env_number("TELEGRAM_DOWNLOAD_MAX_BYTES", 20 * 1024 * 1024, cast=int, min_value=1024 * 1024)
# Global cap on concurrent media downloads: albums and TikTok slideshows from
# different chats otherwise accumulated in memory without a shared limit (audit 28.10.2026).
MEDIA_GLOBAL_CONCURRENCY = _env_number("MEDIA_GLOBAL_CONCURRENCY", 2, min_value=1)
_media_semaphores: dict[Any, asyncio.Semaphore] = {}


def _media_semaphore() -> asyncio.Semaphore:
    # A semaphore per event loop: tests run many short asyncio.run calls,
    # one shared object would be bound to the first loop.
    loop = asyncio.get_running_loop()
    sem = _media_semaphores.get(loop)
    if sem is None:
        # Closed loops hold a reference and everything attached to them: remove
        # old entries when creating a new one (review 17.11.2026).
        for stale_loop in [lp for lp in _media_semaphores if lp.is_closed()]:
            _media_semaphores.pop(stale_loop, None)
        sem = asyncio.Semaphore(MEDIA_GLOBAL_CONCURRENCY)
        _media_semaphores[loop] = sem
    return sem


@contextlib.asynccontextmanager
async def _media_download_slot():
    async with _media_semaphore():
        yield
# Previously hardcoded as 15.0 inside _download_telegram_file_bytes,
# inconsistently with the other timeouts, which are all configured through env.
TELEGRAM_GET_FILE_TIMEOUT = _env_number("TELEGRAM_GET_FILE_TIMEOUT", 15, min_value=1)
# Cooldown after HTML garbage from the proxy instead of JSON: without it, dozens of calls per second trigger an avalanche of WARNINGs (see _tg_call).
TELEGRAM_PROXY_COOLDOWN_SEC = _env_number("TELEGRAM_PROXY_COOLDOWN_SEC", 20, min_value=0)
# Cap a single Retry-After sleep in _tg_call: otherwise a long flood wait holds
# the chat lock beyond the route budget (audit 10.2026). The retry itself is preserved.
TELEGRAM_FLOOD_SLEEP_MAX_SEC = _env_number("TELEGRAM_FLOOD_SLEEP_MAX_SEC", 10, min_value=1)
# TIKTOK_GROUP_REQUIRE_MENTION=1 — TikTok links in groups without addressing the bot
# only written in the background, not downloaded (audit H2, 10.2026). Disabled by default.
TIKTOK_GROUP_REQUIRE_MENTION = os.getenv("TIKTOK_GROUP_REQUIRE_MENTION", "0") == "1"
# Limit for waiting for the next chunk from any provider: otherwise a stuck stream holds the chat lock indefinitely.
STREAM_CHUNK_TIMEOUT_SEC = _env_number("STREAM_CHUNK_TIMEOUT_SEC", 30, min_value=1)
# ── "Live typing" pattern during streaming (see lumen_typing_pace.py) ──
# Edits are drawn on a timer (the painter), not when a chunk arrives; the edit interval
# one per chat for both the status ticker and the stream (review 11.11.2026).
# STREAM_EDIT_MIN_INTERVAL_SEC — the shared budget in private chats: the ticker (0.3) uses it
# fits, totaling no more than ~3 edits/sec in one chat.
STREAM_EDIT_MIN_INTERVAL_SEC = _env_number("STREAM_EDIT_MIN_INTERVAL_SEC", 0.3, min_value=0)
# In a group, the same animation consumes the chat quota for several people: budget 1.5–2 s.
STREAM_EDIT_MIN_INTERVAL_GROUP_SEC = _env_number("STREAM_EDIT_MIN_INTERVAL_GROUP_SEC", 1.8, min_value=0)
# Painter frame: Claude appends word by word every ~50-100 ms, while Telegram allows
# more than once per second; 0.55 s is a compromise between smoothness and 429 (private chat).
STREAM_PAINT_TICK_SEC = _env_number("STREAM_PAINT_TICK_SEC", 0.55, min_value=0)
STREAM_PAINT_TICK_GROUP_SEC = _env_number("STREAM_PAINT_TICK_GROUP_SEC", 1.8, min_value=0)
STREAM_TYPING_TICK_SEC = _env_number("STREAM_TYPING_TICK_SEC", 0.5, min_value=0)
# The completion cap is in seconds, not ticks: with 8 ticks, the tail of a long
# the answer arrived in jumps of 300-500 characters (production 11.11.2026: 6201
# characters, max_step=326), while Claude prints a long answer exactly as it types it.
STREAM_TYPING_MAX_CATCHUP_SEC = _env_number("STREAM_TYPING_MAX_CATCHUP_SEC", 20, min_value=1)
# In a group the chat lock is shared: 20 seconds of tail display for one long response
# would delay everyone, so the cap is shorter there.
STREAM_TYPING_MAX_CATCHUP_GROUP_SEC = _env_number("STREAM_TYPING_MAX_CATCHUP_GROUP_SEC", 8, min_value=1)
# A finish-generation step is a pause PLUS an edit: through the relay it costs
# 0.5-0.7 seconds, and
# counting the budget only by sleep would exceed the message deadline
# (review 11.11.2026: 40 ticks × 0.5s = 20s of sleep, about 40s wall-clock).
STREAM_TYPING_STEP_RTT_SEC = _env_number("STREAM_TYPING_STEP_RTT_SEC", 0.7, min_value=0)
# FIRST_CHUNK_TIMEOUT_SEC is half the wait for the first chunk (see
# lumen_model_speed.first_chunk_limit_sec): drop a quickly stalled model early.
# The limit only shortens the wait: whichever of the two timeouts expires first wins.
# 12 s kept "Thinking..." displayed for a stalled head; 5 s is enough for text — slow
# models are already covered by the adaptive floor of EMA×2.5, which acts only as a floor here.
FIRST_CHUNK_TIMEOUT_SEC = _env_number("FIRST_CHUNK_TIMEOUT_SEC", 5, min_value=1)
# While chunks arrive, the stream budget is extended: on 11.11.2026, production cut off
# a 5,700-character response at second 35 (“Route time budget exhausted”), and
# the user saw "Connection interrupted". Keep the ceiling below 60 s; beyond that the relay
# (proxy/proxy.ts) cuts off the upstream itself.
# Upper limit 59: the chat lock cannot be held longer; the default 55 leaves headroom.
STREAM_ROUTE_MAX_SEC = _env_number("STREAM_ROUTE_MAX_SEC", 55, min_value=10, max_value=59)
# Stopping a response with /stop: a feature switch, like the other new layers.
STOP_COMMAND_ENABLED = (os.getenv("STOP_COMMAND_ENABLED", "1") or "").strip().lower() not in ("0", "false", "off")
# The Stop control is always enabled: a separate reply button does not inflate edits
# the answer and remains available even for a long message.
STOP_BUTTON_ENABLED = True
STREAM_ROUTE_STALL_GRACE_SEC = _env_number("STREAM_ROUTE_STALL_GRACE_SEC", 20, min_value=2)
# Waiting animation in the placeholder before the first chunk: half a second of static "...",
# then a frame every _DOTS_TICK_SEC (within Telegram edit limit).
_DOTS_START_AFTER_SEC = 0.5
_DOTS_TICK_SEC = 0.3
_DOTS_FRAMES = (".", "..", "…")
# RICH_MESSAGES_ENABLED — sending final answers via sendRichMessage /
# editMessageText+rich_message (Bot API 10.1+: tables, headings, math).
# Kill switch for rendering problems on old clients: 0 — revert
# to the ordinary HTML path without redeploying code (restart only). Streaming changes always
# use regular HTML (transient); rich formatting applies only to final texts.
RICH_MESSAGES_ENABLED = os.getenv("RICH_MESSAGES_ENABLED", "1") == "1"
# Text length limit for /tts — without it, the user could send a huge
# text that would trigger a very long Gemini TTS + ffmpeg run for a single request.
TTS_MAX_CHARS = _env_number("TTS_MAX_CHARS", 800, cast=int, min_value=1)
# Cap for a single TTS synthesis. Previously there was none at all: a
# synchronous Gemini call
# went into streaming without a timeout and without http_options on the client, and a stuck Google
# held the chat lock indefinitely (audit 26.09.2026).
TTS_SYNTH_TIMEOUT_SEC = _env_number("TTS_SYNTH_TIMEOUT_SEC", 60, min_value=1)
# A common deadline for all chunks of multi-chunk TTS: a hung synthesis otherwise held
# per-chat lock without a bound (audit D2, 30.09.2026). Worst chunk ~100s (60 synthesis +
# 30 ffmpeg + 10 probe); five typical chunks fit with room to spare.
TTS_TOTAL_BUDGET_SEC = _env_number("TTS_TOTAL_BUDGET_SEC", 240, min_value=30)
# Daily per-user limits (by user_id): total messages, including those through
# Gemini (links/YouTube/media/freshness with search), plus /tts voiceover and images
# /draw. 5, not 3: generation is iterative ("not that, redraw it"), and the stage does not preserve the budget
# digit, while IMAGE_DAILY_LIMIT is lower (audit 09.11.2026).
DAILY_USER_MESSAGE_LIMIT = _env_number("DAILY_USER_MESSAGE_LIMIT", 30, cast=int, min_value=1)
DAILY_USER_GEMINI_LIMIT = _env_number("DAILY_USER_GEMINI_LIMIT", 5, cast=int, min_value=1)
DAILY_USER_TTS_LIMIT = _env_number("DAILY_USER_TTS_LIMIT", 5, cast=int, min_value=1)
DAILY_USER_DRAW_LIMIT = _env_number("DAILY_USER_DRAW_LIMIT", 5, cast=int, min_value=1)


def _parse_gemini_daily_limits(raw: str) -> dict[str, int]:
    """Parses GEMINI_DAILY_LIMITS in "model=limit,model=limit" form (RPD from the AI Studio dashboard; each model has its own). Silently skips garbage."""
    out: dict[str, int] = {}
    for chunk in (raw or "").split(","):
        name, sep, val = chunk.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        try:
            num = int(val.strip())
        except (TypeError, ValueError):
            continue
        if num >= 1:
            out[name] = num
    return out


# Empty, as before: Gemini has no overall limit, so show only usage.
GEMINI_DAILY_LIMITS: dict[str, int] = _parse_gemini_daily_limits(os.getenv("GEMINI_DAILY_LIMITS", ""))
_PROCESS_START_MONOTONIC = time.monotonic()
# ── Automatic model router timings (see the "automatic
# model selection" below) ──
# No retries of the same model: any error immediately advances to the next in the route; the overall ROUTE_TOTAL_BUDGET_SEC keeps the chat lock from hanging for minutes.
ROUTE_MODEL_TIMEOUT_SEC = _env_number("ROUTE_MODEL_TIMEOUT_SEC", 22, min_value=1)
# ROUTE_TOTAL_BUDGET_SEC is the overall budget for the ENTIRE route of one message. Without it,
# a cascading failure across many models would stretch one answer to minutes under the chat lock.
ROUTE_TOTAL_BUDGET_SEC = _env_number("ROUTE_TOTAL_BUDGET_SEC", 40, min_value=1)
# MESSAGE_TOTAL_BUDGET_SEC is the ceiling for the entire message (lock + search + route).
# The stages below take min(their timeout, the remainder); otherwise their sum exceeded the budget.
MESSAGE_TOTAL_BUDGET_SEC = _env_number("MESSAGE_TOTAL_BUDGET_SEC", 90, min_value=10)
# HISTORY_SUMMARY_BUDGET_SEC caps history summarization: without it, hung
# calls would hold the chat lock for tens of seconds after the answer was ready.
HISTORY_SUMMARY_BUDGET_SEC = _env_number("HISTORY_SUMMARY_BUDGET_SEC", 30, min_value=1)
# Shared /draw budget of 120s: otherwise 5 models × 90s produced up to 7.5 minutes of a hanging "Generating" message (review 28.08.2026).
DRAW_TOTAL_BUDGET_SEC = _env_number("DRAW_TOTAL_BUDGET_SEC", 120, min_value=1)
# Timeout for one image-provider attempt: 40s was calculated for "model thinking,"
# and an unavailable host hung for exactly that long for each user before switching to the reserve
# (production 09.11.2026: Cloudflare silently dropped container connections,
#     46 s before the image arrived).
IMAGE_ATTEMPT_TIMEOUT_SEC = _env_number("IMAGE_ATTEMPT_TIMEOUT_SEC", 20, min_value=1)
# Translate the non-Latin prompt into English before generation: sana loses Cyrillic
# into '?' (proof 09.11.2026). A translation failure does not break the image: the original is sent.
DRAW_TRANSLATE_TIMEOUT_SEC = _env_number("DRAW_TRANSLATE_TIMEOUT_SEC", 12, min_value=1)
# Space measurement 10.11.2026: a short request to the Munito head takes 6–9 s,
# and pronoun-heavy prompts took 10 s. The preparation cap is 12 s; an 8 s cap
# cut off a real response at 8.3 s, raised TimeoutError and silently fell back to text.
DRAW_PROMPT_PREP_TIMEOUT_SEC = _env_number("DRAW_PROMPT_PREP_TIMEOUT_SEC", 12, min_value=1)
DRAW_CONTEXT_TIMEOUT_SEC = _env_number("DRAW_CONTEXT_TIMEOUT_SEC", 10, min_value=1)
# Global daily image budget: 10,000 free Cloudflare Neurons per day.
# flux-2-klein-4b costs 104 Neurons per 1024x1024 image (~96/day); an edit
# adds about 21 Neurons per input megapixel. Around the 97th generation, Cloudflare
# returns 429 for an exhausted allocation while the person sees "service unavailable"
# (measurement 10.11.2026). Without a counter, every new request would keep hitting
# a provider whose daily allocation is already over.
IMAGE_DAILY_LIMIT = _env_number("IMAGE_DAILY_LIMIT", 80, cast=int, min_value=1)
# Image budget in Neuron: 80 generations × 104.2 = 8336 out of 10,000 free,
# 8300 leaves headroom; an edit weighs 1 + 0.2 per input MP (audit 16.11.2026).
IMAGE_DAILY_NEURONS = _env_number("IMAGE_DAILY_NEURONS", 8300, cast=int, min_value=1)
# An image is the bot's most expensive call in memory and budget. Previously, parallel
# /draw calls from different chats each put up to 30 MB into RAM without a shared limit;
# the semaphore bounds that peak (audit 09.11.2026, same class as MEDIA_GLOBAL_CONCURRENCY).
IMAGE_GLOBAL_CONCURRENCY = _env_number("IMAGE_GLOBAL_CONCURRENCY", 2, min_value=1)
# How long to wait for a generation slot. The queue wait cannot consume the entire
# DRAW_TOTAL_BUDGET_SEC: the person cancels before the queue reaches their image.
IMAGE_QUEUE_TIMEOUT_SEC = _env_number("IMAGE_QUEUE_TIMEOUT_SEC", 10, min_value=1)
# How long after generation "add a star"/"make it blue" still refers to this
# image: without a window, a weak verb would search for a photo indefinitely (prod 10.11.2026).
DRAW_EDIT_CONTEXT_TTL_SEC = _env_number("DRAW_EDIT_CONTEXT_TTL_SEC", 600, min_value=1)
# Provider pause after its 429/402 FREE_LIMIT: Pollinations returns Retry-After
# ~35s, default 60s with headroom. Without a pause, the next person would repeat the same attempts.
IMAGE_PROVIDER_COOLDOWN_SEC = _env_number("IMAGE_PROVIDER_COOLDOWN_SEC", 60, min_value=1)
# CHAT_LOCK_TIMEOUT_SEC — how long to wait for the chat lock before replying “busy.”
# The downside: while waiting, the user sees silence. Therefore do not keep it above the hardcoded
# user wait while the lock remains protected: work under the lock
# bounded by its own budget and always releases it in try/finally
# (hostile review 27.09.2026: the value 150 s stayed silent for almost 2.5 minutes).
# Only slash commands (/draw, /tts — their handlers are registered
# the general @dp.message handler). Trigger phrases ("draw", "voice") go through
# _handle_message_core UNDER the lock: rendering holds it until DRAW_TOTAL_BUDGET_SEC,
# speech — until synthesis finishes. This is deliberate (one chat, one task at a time), but
# the second question during a long draw honestly receives "busy" after 45 s.
CHAT_LOCK_TIMEOUT_SEC = _env_number("CHAT_LOCK_TIMEOUT_SEC", 45, min_value=1)
# How many messages from one chat may WAIT for the lock at once. Previously, a hundred
# messages occupied all 100 in-flight slots, and other chats received 503
# (audit 28.10.2026). The fourth and subsequent callers immediately get "busy".
_MAX_CHAT_LOCK_WAITERS = 3
_chat_lock_waiters: dict[int, int] = {}
# Passive group background waits for the lock with a separate counter: it does not reply,
# does not take a place in the people queue and does not receive "busy". Otherwise, replies in
# a busy chat filled the people cap, and an addressed question was rejected (prod 18.11.2026).
_MAX_CHAT_LOCK_BACKGROUND_WAITERS = 20
_chat_lock_background_waiters: dict[int, int] = {}
# INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC — how long main() waits for a graceful
# Wait for fire-and-forget tasks to finish before canceling the rest (Sentry LUMEN-2: the event loop killed them during network calls on redeploy).
INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC = _env_number("INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC", 10, min_value=0)
TG_MAX_LEN = 4096
# Telegram upload bots are capped at 50 MB; skip oversized quality variants
# before downloading.
TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
# TikTok slideshow: up to 35 items, media group: 10 at a time; send the whole post in several calls.
# The chunk size itself lives in lumen_tiktok.TELEGRAM_MEDIA_GROUP_CHUNK (the only
# source of truth; the duplicate here was dead — audit 26.09.2026).
TIKTOK_SLIDESHOW_MAX_ITEMS = 35
# Throttle ffprobe/ffmpeg subprocesses: concurrent media processing can exhaust
#     CPU (measurement 28.08.2026).
# 2 slots, not 4: a live slide holds TWO processes simultaneously in one slot
# (ffprobe + ffmpeg in _probe_and_thumbnail_from_path); otherwise the cap is 8 processes.
# on two Space cores plus a temporary file of up to 50 MB per core (audit 01.11.2026).
TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY = _env_number("TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY", 2, cast=int, min_value=1)
_tiktok_probe_semaphore = asyncio.Semaphore(TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY)
# Slide download limit is 8: 35 slides would otherwise occupy the entire session pool (limit=40) and stall other chats (audit 04.09.2026).
TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY = _env_number("TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY", 8, cast=int, min_value=1)
_tiktok_slide_download_semaphore = asyncio.Semaphore(TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY)
# Shared RAM cap per post: 35 slides at 75MB in memory (up to ~2.6GB) crashed
# container for all chats (audit D1, 30.09.2026). An ordinary post weighs up to 50MB.
TIKTOK_SLIDESHOW_MAX_BYTES = _env_number("TIKTOK_SLIDESHOW_MAX_BYTES", 200 * 1024 * 1024, cast=int, min_value=1024 * 1024)

# State/quotas/locks live in lumen_chat_state.py (P2): here only the re-export
# of names (the same objects — tests mutate bot.chat_state/bot.GLOBAL_QUOTA as before).
import lumen_chat_state
from lumen_chat_state import (
    chat_state,
    MAX_TOTAL_MEMORY_MESSAGES,
    HISTORY_SUMMARIZE_KEEP,
    _STATE_DIR,
    _CHATS_DIR,
    _storage_config,
    _upstash_set,
    _upstash_get,
    _upstash_delete,
    _storage_write_text,
    _storage_read_text,
    _storage_delete_text,
    _chat_storage_path,
    _save_chat_to_storage,
    _delete_chat_storage,
    _save_chat_payload,
    GLOBAL_QUOTA,
    _QUOTA_CHECK_THROTTLE_SEC,
    _maybe_alert_gemini_exhausted,
    _reset_quota_if_new_day,
    _mark_rate_limited,
    load_global_quota,
    save_global_quota,
    _restore_single_chat,
    _save_chat_index,
    _save_chat_index_payload,
    _save_quota_payload,
    _dirty_chat_ids,
    _pending_chat_deletions,
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
    _t_no_create,
    _peek_chat_lang,
    _prune_old_chats,
    get_chat_lock,
    acquire_chat_lock,
    _evict_orphan_chat_locks,
    _is_owner,
    _notify_owner,
    BANNED_KEY,
    _is_banned,
    _ban_user,
    _unban_user,
    _banned_list,
    _is_privileged_in_chat,
    _quota_entry,
    _mark_quota_exhausted,
    _record_quota_usage,
    _stats_entry,
    _record_stats_event,
    _image_usage_total,
    _image_budget_exhausted,
    _reserve_image_neurons,
    _release_image_neurons,
    _image_neurons_total,
    recover_global_quota,
    _image_provider_block_reason,
    _image_provider_ready,
    _note_image_success,
    _note_image_rate_limited,
    _note_image_budget_exhausted,
    _last_storage_write_ts,
    USER_DAILY_KEY,
    _user_daily_entry,
    _user_daily_peek,
    _user_daily_admission_blocked,
    _user_daily_limit,
    _user_daily_total_exhausted,
    _user_daily_gemini_exhausted,
    _user_daily_tts_exhausted,
    _record_user_daily,
    _reserve_user_daily,
    _refund_user_daily,
    _user_key_for_message,
    _user_daily_reset_in,
    _trim_history,
    _note_sender,
    _resolve_nick,
)

# The simple rate-limit tracker and the clarification-button queue live in
# lumen_limits.py (P2): read scalars from their owning module rather than copying them—
# copy in bot.X would silently diverge from test patches (audit A5-8).
from lumen_limits import (
    user_rate_limits,
    _cleanup_rate_limit_dict,
    _check_and_register_rate_limit,
    _pending_picks,
    _enforce_pending_picks_cap,
)

# Initialize clients inside the event-processing loop (fixes RuntimeError).
bot: Bot | None = None
dp = Dispatcher()
client: genai.Client | None = None

_http_session: aiohttp.ClientSession | None = None
_chat_locks: dict[int, asyncio.Lock] = {}

# Transport belongs in lumen_transport_calls.py.
from lumen_telegram_transport import (
    PROXY_AUTH_HEADER,
    _TelegramProxyCircuitBreaker,
    IPv4AiohttpSession,
)

TELEGRAM_PROXY_TRIP_THRESHOLD = _env_number("TELEGRAM_PROXY_TRIP_THRESHOLD", 3, cast=int, min_value=1)
_tg_proxy_breaker = _TelegramProxyCircuitBreaker(cooldown_sec=TELEGRAM_PROXY_COOLDOWN_SEC, trip_threshold=TELEGRAM_PROXY_TRIP_THRESHOLD)

# Markdown-to-HTML conversion, JSON utilities

# Conversion of markdown/LaTeX/tables to Telegram HTML — pure functions in lumen_formatting.py.
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

# Transport calls live in lumen_transport_calls.py (P2).
from lumen_transport_calls import (
    _get_telegram_session,
    _rotate_telegram_proxy,
    _get_http_session,
    _close_sessions,
    _handle_proxy_failure,
    _tg_call,
    _TG_UNKNOWN,
    _tg_outcome_unknown,
    _tg_cancelled_unknown,
    telegram_api_call,
)

# Sending (rich/guests/ch-text) lives in lumen_rich.py, media downloading in
# lumen_media_flow.py (P2): here it is only a re-export of names.
from lumen_rich import (
    is_guest_message,
    _answer_guest_text,
    _is_real_telegram_message,
    _answer_sender,
    _try_send_rich,
    _try_edit_rich,
    _send_text,
    _safe_reply,
    _delete_message_quietly,
    _edit_message_quietly,
)
from lumen_media_flow import (
    _MediaTooLargeError,
    _download_telegram_file_bytes,
    _save_media_to_history,
    _download_message_attachment,
    _fetch_media,
    _resolve_edit_references,
    _fresh_generation_ref,
)

# error parsing

# model list
#
# Model configuration and routing live in lumen_router_config.py (imported where the former GEMINI_MODELS was; only names actually used).
from lumen_router_config import (
    DEFAULT_GEMINI_MODEL,
    _check_unconfirmed_model_quotas,
    _MUNITO_ORDER,
    GEMINI_TTS_MODELS,
    _record_model_outcome,
    _is_quarantined,
    _quarantine_status,
)
# The error layer lives in lumen_errors.py (P2): only name re-exports are here.
from lumen_errors import (
    _error_text,
    _error_status,
    _classify_model_error,
    _model_error_text,
    _or_error_msg,
    UserFacingInputError,
    GeminiAllModelsExhaustedError,
    _next_fallback_model,
    _gemini_error_msg,
    get_system_prompt,
    _MODEL_ERROR_FALLBACK_MSG,
)

# The shape of chat_state[chat_id] is the ChatState TypedDict in lumen_chat_state.py.

# Buffers for albums/media groups
_mg_buffers: dict[str, list[Message]] = {}
_mg_tasks: dict[str, asyncio.Task] = {}
# There are only a few live media groups (each lives 0.8s); hundreds of distinct mgid values are a flood.
# The cap only prevents task and memory bloat; it does not affect legitimate albums.
_MG_TRACK_CAP = 200

def _mg_evict_if_full() -> None:
    """Evict the oldest media groups above the cap (the task is canceled —
    its messages have already been lost to the flood, so there is nothing to wait for)."""
    while len(_mg_buffers) > _MG_TRACK_CAP:
        oldest = next(iter(_mg_buffers))
        _mg_buffers.pop(oldest, None)
        task = _mg_tasks.pop(oldest, None)
        if task is not None and not task.done():
            task.cancel()

# _inflight_tasks is the shared set of fire-and-forget tasks (Sentry LUMEN-2: the
# event loop killed them during network calls on redeploy); main() drains them on
# shutdown.
_inflight_tasks: set[asyncio.Task] = set()

def _track_inflight_task(task: asyncio.Task) -> asyncio.Task:
    """Registers a fire-and-forget task for graceful shutdown — see the comment
    by _inflight_tasks above. Returns the same task so the call can wrap
    asyncio.create_task(...) directly, without an extra temporary variable."""
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)
    return task

# HTTP layer (app, secret gates, /healthz, /admin_keys, /webhook_url, /webhook,
# /diag, /export_state) lives in lumen_admin.py (P2): here only the names are re-exported,
# so that `bot.X` in tests and main() do not change. Secrets are printed here below —
# tests patch bot.WEBHOOK_SECRET/bot.ADMIN_PANEL_KEY directly.
from lumen_admin import (
    app,
    _check_bearer_token,
    _check_admin_key,
    _redact_secret,
    _check_bot_token_auth,
    healthcheck,
    webhook_handler,
    probe_url,
    export_state,
    devtrace,
)

# ADMIN_SECRET_SEED — an independent salt for secrets (otherwise everything was derived from BOT_TOKEN and rotated only with it). An empty seed with an empty token means a random process secret, not a hardcoded string.
_ADMIN_SECRET_SEED_ENV = os.getenv("ADMIN_SECRET_SEED", "").strip()
if not _ADMIN_SECRET_SEED_ENV and BOT_TOKEN and (os.getenv("SPACE_ID") or os.getenv("SPACE_HOST")):
    # Weak fallback is forbidden in production: otherwise a token leak leads to /export_state
    # and update forgery. The seed is set on HF Space; losing configuration should
    # crash startup rather than silently weaken the admin panel (28.10.2026 audit).
    raise RuntimeError(
        "ADMIN_SECRET_SEED is required in production (HF Space): without it "
        "WEBHOOK_SECRET/ADMIN_PANEL_KEY are derived from BOT_TOKEN."
    )
_ADMIN_SECRET_SEED = _ADMIN_SECRET_SEED_ENV or BOT_TOKEN or secrets.token_hex(32)
if not _ADMIN_SECRET_SEED_ENV and BOT_TOKEN:
    # This is not a failure, but a loud warning in local development: the keys
    # are derived from the bot token, so a token leak means exposure of all chats
    # and forged updates (audit 26.09.2026).
    log.warning(
        "[setup] ADMIN_SECRET_SEED is not set — WEBHOOK_SECRET/ADMIN_PANEL_KEY are derived from BOT_TOKEN. "
        "A leaked bot token then exposes /export_state and /webhook. Set ADMIN_SECRET_SEED as an HF Space secret.",
    )
WEBHOOK_SECRET = hashlib.sha256(_ADMIN_SECRET_SEED.encode()).hexdigest()[:32]
ADMIN_PANEL_KEY = hashlib.sha256(_ADMIN_SECRET_SEED.encode() + b"admin_panel").hexdigest()[:24]

# guest_message is a valid Update field from the Bot API (guest mode: changelog and
# the Update section at core.telegram.org/bots/api, verified 2026-09-21).
# Removing this was an AUD-J-001 audit error (check dated 2026-09-19
# is outdated): without this update type, the bot does not receive guest updates.
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "guest_message"]

# state and quota storage


# Optional Upstash Redis storage (see README). When both values are set, write
# there instead of the container disk; if unset, everything remains unchanged.
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
USE_UPSTASH = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)
if bool(UPSTASH_REDIS_REST_URL) != bool(UPSTASH_REDIS_REST_TOKEN):
    log.warning('[setup] Only one of UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN is set — both are required together, Upstash will not be used.')
log.info(
    '[setup] Persistent storage: %s',
    "Upstash Redis" if USE_UPSTASH else f"локальный файл в {_STATE_DIR} (см. README про эфемерность на HF Spaces)"
)

# Storage is in lumen_state_storage.py + lumen_chat_state.py.
from lumen_state_storage import (
    CHAT_STATE_SCHEMA_VERSION,
    _serialize_chat_state,
    _current_quota_day,
    _quota_day_reset_in_sec,
)


# `_urllib_request` is no longer used in bot.py — the Upstash client moved to
# lumen_state_storage.py. Kept for tests that patch `bot._urllib_request.urlopen`
# (Like CHAT_STATE_SCHEMA_VERSION; see the explanation of __all__ in the first block at the top of the file.)
__all__ = [
    "_urllib_request",
    "CHAT_STATE_SCHEMA_VERSION",
    "user_rate_limits",
    # Names from lumen_admin.py (app, gates, endpoints) are not read directly by bot.py
    # (main() takes only app) — existing tests need them as `bot.X`.
    "app",
    "_check_bearer_token",
    "_check_admin_key",
    "_redact_secret",
    "_check_bot_token_auth",
    "healthcheck",
    "webhook_handler",
    "probe_url",
    "export_state",
    "devtrace",
    # Names from lumen_streaming.py are not read by bot.py itself — they are needed as `bot.X`
    # existing tests and `_run_route` below.
    "_tick_waiting_dots",
    "_pieces_with_waiting_feedback",
    "_munito_stream_pieces",
    "_run_streaming_reply",
    "_try_gemini_streaming",
    "_try_munito_streaming",
    # Test substitution point (see monkeypatch in tests/); bot.py itself
    # no longer reads directly after streaming was moved out.
    "_model_first_chunk_limit",
    # Test fallback constants (read as bot._IDENTITY_LEAK_FALLBACK).
    "_IDENTITY_LEAK_FALLBACK",
    # Names from lumen_tiktok_flow.py are not read by bot.py itself; they are needed as
    # `bot.X` for existing tests and `_handle_message_core` below.
    "_send_tiktok_music",
    "handle_tiktok_sound",
    "_fetch_tikwm_media_data_with_proxy_fallback",
    "_try_send_tiktok_slideshow",
    "handle_tiktok",
    # The mechanics of lumen_tiktok.py as `bot.X` for existing tests.
    "_original_sound_label",
    "_chunk_tiktok_media_items",
    "_looks_like_video_bytes",
    "_slideshow_slide_urls",
    "_tiktok_video_candidates",
    "TikTokUserFacingError",
    "_tiktok_music_page_id",
    "_write_mp3_tags",
    "_download_url_bin",
    "_probe_audio_duration",
    "_probe_video_dimensions",
    "_generate_video_thumbnail",
    "_probe_and_thumbnail_from_bytes",
    "_resolve_tiktok_short",
    "_fetch_tikwm_media_data",
    # bot.py does not read names from lumen_routes.py itself — they are needed as `bot.X`
    # existing tests and `_handle_message_core` below.
    "MunitoAPIError",
    "_munito_request",
    "_attempt_timeout",
    "_or_extract_text",
    "_is_gemini_daily_quota",
    "ask_munito_text",
    "ask_munito_multimodal",
    "selftest_llm_head",
    "_transcribe_audio",
    "_gemini_history_contents",
    "_build_gemini_call_config",
    "_extract_gemini_answer_text",
    "ask_gemini",
    "_build_gemini_turn_contents",
    "_build_openai_turn_messages",
    "_route_error_reply_text",
    "RouteBudgetExceededError",
    "_run_route",
    # Model orders and history cleanup are read by tests as bot.X.
    "_MUNITO_ORDER",
    "_history_user_text",
    "_clean_history_entry",
    # Namespace for genai types in tests (bot.types.Content/...).
    "types",
    # bot.py does not read the names from lumen_chat_state.py itself; they are
    # needed as
    # `bot.X` for existing tests.
    "MAX_TOTAL_MEMORY_MESSAGES",
    "HISTORY_SUMMARIZE_KEEP",
    "_CHATS_DIR",
    "_storage_config",
    "_upstash_set",
    "_upstash_get",
    "_upstash_delete",
    "_storage_write_text",
    "_storage_read_text",
    "_storage_delete_text",
    "_chat_storage_path",
    "_save_chat_to_storage",
    "_delete_chat_storage",
    "_save_chat_payload",
    "_QUOTA_CHECK_THROTTLE_SEC",
    "load_global_quota",
    "_restore_single_chat",
    "_save_chat_index",
    "_save_chat_index_payload",
    "_save_quota_payload",
    "_save_chat_to_storage_limited",
    "_delete_chat_storage_limited",
    "_mark_new_chat_id",
    "mark_quota_dirty",
    "_flush_dirty_state_once",
    "_prune_old_chats",
    "_quota_entry",
    "_stats_entry",
    "_record_stats_event",
    "_last_storage_write_ts",
    # bot.py obtains chat locks through acquire_chat_lock; both names below are
    # only for `bot.X` in tests.
    "get_chat_lock",
    "acquire_chat_lock",
    # Direct lumen_state_storage names for tests.
    "_serialize_chat_state",
    "_current_quota_day",
    "_quota_day_reset_in_sec",
    "USER_DAILY_KEY",
    "_user_daily_entry",
    "_user_daily_peek",
    "_user_daily_admission_blocked",
    "_user_daily_limit",
    "_user_daily_total_exhausted",
    "_user_daily_gemini_exhausted",
    "_user_daily_tts_exhausted",
    "_record_user_daily",
    "_reserve_user_daily",
    "_refund_user_daily",
    "_user_key_for_message",
    "_user_daily_reset_in",
    # Unknown-outcome transport contract — for `bot.X` in tests (R4-03).
    "_TG_UNKNOWN",
    "_tg_outcome_unknown",
    "_tg_cancelled_unknown",
    # Names of the other extracted modules are only for `bot.X` in tests.
    "GEMINI_TTS_MODELS",
    "_cloudflare_text_to_image",
    "_cloudflare_edit_to_image",
    "_pollinations_text_to_image",
    "detect_image_size",
    "image_dimensions",
    "size_for_reference",
    "image_provider_names",
    "image_filename",
    "_gemini_tts_bytes",
    "chat_state",
    "GLOBAL_QUOTA",
    "_is_owner",
    "_is_privileged_in_chat",
    "BANNED_KEY",
    "_is_banned",
    "_ban_user",
    "_unban_user",
    "_banned_list",
    "_mark_quota_exhausted",
    "_mark_rate_limited",
    "_record_quota_usage",
    "_image_usage_total",
    "_image_budget_exhausted",
    "_reserve_image_neurons",
    "_release_image_neurons",
    "_image_neurons_total",
    "recover_global_quota",
    "_image_provider_block_reason",
    "_image_provider_ready",
    "_note_image_success",
    "_note_image_rate_limited",
    "_note_image_budget_exhausted",
    "_trim_history",
    "_note_sender",
    "_resolve_nick",
    "_pending_picks",
    "_enforce_pending_picks_cap",
    # The bot code does not read names from lumen_errors.py itself — tests only use `bot.X`.
    "_error_text",
    "_error_status",
    "_classify_model_error",
    "_model_error_text",
    "_or_error_msg",
    "UserFacingInputError",
    "GeminiAllModelsExhaustedError",
    "_next_fallback_model",
    "_gemini_error_msg",
    "get_system_prompt",
    "_MODEL_ERROR_FALLBACK_MSG",
    # Names of the other extracted modules are only for `bot.X` in tests.
    "ParseMode",
    "_split_text_chunks",
    "is_guest_message",
    "_answer_guest_text",
    "_is_real_telegram_message",
    "_answer_sender",
    "_try_send_rich",
    "_try_edit_rich",
    "_send_text",
    "_safe_reply",
    "_delete_message_quietly",
    "_edit_message_quietly",
    "_download_telegram_file_bytes",
    "_MediaTooLargeError",
    "_save_media_to_history",
    "_download_message_attachment",
    "_resolve_edit_references",
    "_fresh_generation_ref",
    "_fetch_media",
    "_sanitize_mime_type",
    "_mime_suffix",
    "_msg_media_source",
    "_ensure_prompt_text",
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
    "_translate_draw_prompt",
    "resolve_draw_request",
    "revert_draw_version",
    "handle_draw_more_callback",
    "inline_draw_variants",
    "inline_tts",
    "_send_pick_question",
    "DEFAULT_GEMINI_MODEL",
    "_record_model_outcome",
    "_is_quarantined",
    "_quarantine_status",
    "_maybe_alert_gemini_exhausted",
    "mark_state_dirty",
    "get_state",
    "_chat_lang",
    "_t",
    "_t_no_create",
    "_peek_chat_lang",
    "_check_and_register_rate_limit",
    "_record_passive_group_context",
    "_should_only_record_passively",
    "_reply_target_block",
    "_tagged_prompt",
    "_rate_limit_key_for_message",
    "_reject_rate_limited_message",
    "_resolve_incoming_media",
    "_process_raw_update",
    # Transport call names and _notify_owner are only for `bot.X` in tests.
    "_get_telegram_session",
    "_rotate_telegram_proxy",
    "_get_http_session",
    "_close_sessions",
    "_handle_proxy_failure",
    "_tg_call",
    "telegram_api_call",
    "_notify_owner",
]

# Throttling for notifying the owner that the Gemini quota is fully exhausted (see
# _handle_message_core) — without it, the same alert would be sent for EVERY
# a message requiring Gemini until the quota recovers (it may take all day).
GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC = 3600.0



# image generation (Cloudflare Workers AI + Pollinations reserve)
#
# Image generation belongs in lumen_images.py (pure functions; the caller passes the session).
from lumen_images import (
    ImageBudgetExhaustedError,
    ImageModerationError,
    ImageProviderError,
    ImageRateLimitedError,
    _cloudflare_edit_to_image,
    _cloudflare_text_to_image,
    _pollinations_text_to_image,
    detect_image_size,
    image_dimensions,
    image_filename,
    image_provider_names,
    size_for_reference,
)

# Provider and error names are needed only by tests as `bot.X` (see the explanation
# see __all__ in the first block at the start of the file).
__all__ += ["ImageProviderError", "ImageRateLimitedError", "ImageBudgetExhaustedError", "ImageModerationError"]

# Pure media utilities belong in lumen_media.py; the network part remains here
# (it needs a session, BOT_TOKEN, and bot.get_file).
from lumen_media import (
    _sanitize_mime_type,
    _mime_suffix,
    _msg_media_source,
    _ensure_prompt_text,
)
# (import lumen_lang is at the top of the file, next to system_prompt: _MODEL_ERROR_MESSAGES
# already uses it at module level above in the file),
# media loading




# Model lists and route health live in lumen_router_config.py — here only
# the code that actually calls the API.

# ─────────────────── protection against provider/model leaks and prompt injection ───────────────────
# Leak/injection detectors are pure functions in lumen_security.py (import only what
#     is used).
from lumen_security import (
    _IDENTITY_LEAK_FALLBACK,
)
# (_INJECTION_PROBE_REPLY is no longer imported here: the response to provocations
# is now taken from lumen_lang.py according to the chat language — see the injection_probe_reply key)


# LLM routing lives in lumen_routes.py (P2): this is only a re-export
# names, so `bot.X` in tests and `_handle_message_core` do not change.
from lumen_routes import (
    MunitoAPIError,
    _munito_request,
    _or_extract_text,
    _attempt_timeout,
    _is_gemini_daily_quota,
    ask_munito_text,
    ask_munito_multimodal,
    selftest_llm_head,
    _transcribe_audio,
    _gemini_history_contents,
    _build_gemini_call_config,
    _extract_gemini_answer_text,
    ask_gemini,
    _build_gemini_turn_contents,
    _build_openai_turn_messages,
    _route_error_reply_text,
    RouteBudgetExceededError,
    _run_route,
)

# downloading TikTok
#
# Downloader mechanics — in lumen_tiktok.py, orchestration — in lumen_tiktok_flow.py.
# The names below are needed as `bot.X` by existing tests, so they are here too.
from lumen_tiktok import (
    _original_sound_label,
    _chunk_tiktok_media_items,
    _looks_like_video_bytes,
    _slideshow_slide_urls,
    _tiktok_video_candidates,
    TikTokUserFacingError,
    _tiktok_music_page_id,
    _write_mp3_tags,
    _download_url_bin,
    TIKTOK_DOWNLOAD_MAX_BYTES,
    _probe_audio_duration,
    _probe_video_dimensions,
    _generate_video_thumbnail,
    _probe_and_thumbnail_from_bytes,
    _resolve_tiktok_short,
    _fetch_tikwm_media_data,
    _looks_like_resolved_tiktok_url,
)

# Needed only by regression tests as `bot._looks_like_resolved_tiktok_url`
# (see the explanation of __all__ by the first block at the start of the file).
__all__ += ["_looks_like_resolved_tiktok_url"]

# TikTok orchestration lives in lumen_tiktok_flow.py (P2); here there is only a re-export
# names, so that `bot.X` in tests and `_handle_message_core` do not change.
from lumen_tiktok_flow import (
    _send_tiktok_music,
    handle_tiktok_sound,
    _fetch_tikwm_media_data_with_proxy_fallback,
    _try_send_tiktok_slideshow,
    handle_tiktok,
)


# Text delivery speed is a backend property, not a model property, so measure it directly
# on every stream + EMA (see lumen_typing_pace.py). A new model is calibrated
# learns them from the first answers; nothing needs manual updating.

# Self-calibrating model latency estimate — see the lumen_model_speed.py docstring.
# Only the test replacement name placeholder remains here; the measurement itself moved to
# lumen_routes.py/lumen_streaming.py together with the calling code.
from lumen_model_speed import (
    first_chunk_limit_sec as _model_first_chunk_limit,
)

# Seam for replacing completion sleep: tests/conftest.py patches it, or the suite slows down.
_typing_sleep = asyncio.sleep

# The typing-animation substitution point is intentionally NOT autouse: a no-op would break the edit-sequence assertions.
_dots_sleep = asyncio.sleep


# Streaming lives in lumen_streaming.py (P2): here, only name re-exports,
# so `bot.X` in tests and `_run_route` do not change.
from lumen_streaming import (
    _tick_waiting_dots,
    _pieces_with_waiting_feedback,
    _munito_stream_pieces,
    _run_streaming_reply,
    _try_gemini_streaming,
    _try_munito_streaming,
)

# Pure parsing functions — in lumen_message_parse.py. clean_mention remains
# here: it needs BOT_USERNAME from this module.
from lumen_message_parse import (
    DRAW_TRIGGER_PREFIXES,
    strip_group_wake_word,
    TTS_TRIGGER_PREFIXES,
    _NO_MEDIA_NOTE,
    _clean_history_entry,
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

# Needed only by tests as `bot.X` (see the explanation of __all__ in the first block
# at the top of the file).
__all__ += ["_looks_like_media_reference", "_mime_matches_media_category"]

def clean_mention(text: str) -> str:
    return re.sub(rf"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE).strip()

# Triggers, reply markers, media categories, and the "no file" mark live in
# lumen_message_parse.py (imported above) — only clean_mention remains here
# (requires BOT_USERNAME) and message_mentions_bot below.

# Prompt-injection protection (input prefilter) has been moved to lumen_security.py together
# with protection against identity leaks (see the import next to _detect_identity_leak above).

def message_mentions_bot(message: Message) -> bool:
    # chat None (a raw update without a chat) — there is nowhere to look for mentions; do not crash the update.
    if message.chat is None:
        return False
    if message.chat.type == ChatType.PRIVATE:
         return True
    t = message.text or message.caption or ""
    if f"@{BOT_USERNAME}".lower() in t.lower():
         return True
    if message.reply_to_message and message.reply_to_message.from_user:
         if message.reply_to_message.from_user.username and message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower():
              return True
    return False

# bot commands

# Commands/TTS/Draw/pick live in lumen_commands.py; this only imports their names
# and handler registration use explicit register calls (same order, same behavior).
from lumen_commands import (
    cmd_start,
    cmd_stop,
    handle_stop_callback,
    inline_draw,
    _translate_draw_prompt,
    resolve_draw_request,
    revert_draw_version,
    cmd_draw,
    handle_draw_more_callback,
    inline_draw_variants,
    _gemini_tts_bytes,
    inline_tts,
    cmd_tts,
    cmd_reset,
    cmd_lang,
    handle_lang_callback,
    cmd_logs,
    cmd_stats,
    cmd_selftest,
    cmd_ban,
    cmd_unban,
    cmd_banlist,
    chat_locked,
    _send_pick_question,
    handle_pick_callback,
)
# /help is an alias for /start: people habitually ask for help this way, and without it
# of the alias, its text was sent to the model (owner decision 11.11.2026). It is not duplicated in the menu.
dp.message.register(cmd_start, Command("start", "help"))
dp.message.register(cmd_draw, Command("draw"))
dp.callback_query.register(handle_draw_more_callback, F.data == "draw:more")
dp.message.register(chat_locked(cmd_tts), Command("tts"))
dp.message.register(chat_locked(cmd_reset), Command("reset"))
dp.message.register(cmd_stop, Command("stop"))
dp.callback_query.register(handle_stop_callback, F.data.startswith("stop:"))
dp.message.register(cmd_lang, Command("lang"))
dp.callback_query.register(handle_lang_callback, F.data.startswith("lang:"))
dp.message.register(cmd_logs, Command("logs"))
dp.message.register(cmd_stats, Command("stats"))
dp.message.register(cmd_selftest, Command("selftest"))
dp.message.register(cmd_ban, Command("ban"))
dp.message.register(cmd_unban, Command("unban"))
dp.message.register(cmd_banlist, Command("banlist"))
dp.callback_query.register(handle_pick_callback, F.data.startswith("pick:"))


# ─────────────────── automatic model selection (router) ───────────────────
# The route is rebuilt for every message; Gemini is used only for search/links/YouTube because its quota is the tightest.


# ── Clarification buttons (pick scenarios) ──
# A question with buttons instead of model guesswork for taste requests without details
# ("recommend a movie"). Options are defined in code (PICK_TABLE in lumen_lang.py); the model
# does not generate options — weak verbs mangle them. No emoji in buttons.
PICK_BUTTONS_ENABLED = os.getenv("PICK_BUTTONS_ENABLED", "1") == "1"
# PICK_TTL_SEC/MAX_PENDING_PICKS/_pending_picks/_purge/_enforce are in lumen_limits.py,
# imported above next to rate limit (P2); they are used directly here.






# message handling

# Message handling core lives in lumen_message_core.py (P2).

@dp.errors()
async def global_error_handler(event: Any) -> bool:
    log.error("Global error handler caught exception", exc_info=event.exception)
    return True

@dp.message()
async def handle_message(message: Message) -> None:
    # The chat marker on every handling line (see _LOG_CHAT); reset in finally —
    # otherwise the value would leak into the next update in the same process.
    token = _LOG_CHAT.set(f"chat={message.chat.id if message.chat else 0}")
    utoken = _LOG_USER.set(f"user={message.from_user.id if getattr(message, 'from_user', None) else 0}")
    # The message ceiling is set here: album tasks inherit the context.
    dtoken = _MSG_DEADLINE.set(time.monotonic() + MESSAGE_TOTAL_BUDGET_SEC)
    # Reset on entry: the value must not leak into the next update.
    ctoken = _ANSWER_COUNTED.set(False)
    try:
        await _handle_message(message)
    finally:
        _LOG_CHAT.reset(token)
        _LOG_USER.reset(utoken)
        _ANSWER_COUNTED.reset(ctoken)
        _MSG_DEADLINE.reset(dtoken)


async def _handle_message(message: Message) -> None:
    # Owner lock: no reply, no group context, no counters —
    # cut off at the earliest point, including media groups and passive background. A channel post
    # (sender_chat without from_user) is also an author: otherwise a ban is bypassed through a channel
    # (review 17.11.2026).
    _author = message.from_user if message.from_user is not None else getattr(message, "sender_chat", None)
    if _author is not None and getattr(_author, "id", None) is not None and _is_banned(_author.id):
        return

    if message.media_group_id:
        mgid = message.media_group_id
        _mg_buffers.setdefault(mgid, []).append(message)
        _mg_evict_if_full()
        if mgid not in _mg_tasks or _mg_tasks[mgid].done():
             _mg_tasks[mgid] = _track_inflight_task(asyncio.create_task(_process_media_group_buffers(mgid)))
        return

    chat_id = message.chat.id if message.chat else 0
    is_private = message.chat.type == ChatType.PRIVATE if message.chat else True
    is_guest = is_guest_message(message)
    mentioned = message_mentions_bot(message)
    # 'Lumen, ...' is also an address: without this, such a question would go through quiet background
    # and did not get an honest "busy" reply in a busy chat (wake-word 18.11.2026).
    if (
        not mentioned and not is_private and not is_guest
        and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        and not getattr(message, "forward_origin", None)
    ):
        mentioned = group_wake_request(message.text or message.caption or "")[0]
    # Always log incoming media: otherwise a silent drop (production 23.09.2026)
    # not diagnose anything at all.
    _in_src = _msg_media_source(message)
    if _in_src is not None:
        log.info(
            "[in] media message: chat=%s kind=%s has_text=%s",
            chat_id, type(_in_src).__name__, bool(message.text or message.caption),
        )

    # An unaddressed group is also a chat-touching path: the same lock and deadline,
    # otherwise background wrote history in parallel with the answer (review 17.11.2026).
    # background=True: background work does not reply "busy" and does not take a people queue slot
    # (production 18.11.2026: "Day by day" in the middle of an answer received an
    #     addressed "busy" reply).
    if not is_private and not is_guest and not mentioned:
        await _run_under_chat_lock(message, lambda: _handle_message_core(message), background=True)
        return

    # The lock, waiter cap, and message deadline belong in the shared helper so this path,
    # album and guest diverged in their protections (audit 28.10.2026 + 01.11.2026).
    await _run_under_chat_lock(message, lambda: _handle_message_core(message))

# webhook and startup
from lumen_message_core import (
    _process_media_group_buffers,
    _record_passive_group_context,
    _should_only_record_passively,
    _rate_limit_key_for_message,
    _reject_rate_limited_message,
    _resolve_incoming_media,
    _reply_target_block,
    _tagged_prompt,
    _run_under_chat_lock,
    _handle_message_core,
    _process_raw_update,
)

# setWebhook: attempts with exponential backoff starting from a base (audit D4, 30.09.2026).
SETWEBHOOK_MAX_ATTEMPTS = 5
SETWEBHOOK_BACKOFF_SEC = 5.0

# /webhook returns 503 until startup has loaded state: otherwise an update
# arriving during loading creates an empty chat and overwrites the deleted
# history/index (28.10.2026 audit).
_state_ready = True


# Profile locales are the same as for the command menu (the same ISO 639-1 gate, the same "fil"
# outside the list). Keeping only the ten "most popular" languages would leave
# Thai, Urdu, and Zulu with English
# "About," even though command descriptions exist in their languages (verified 11.11.2026).
PROFILE_LANGS: tuple[str, ...] = (DEFAULT_LANG,) + tuple(COMMAND_LOCALES)

# the STRINGS key -> (Bot method, text argument name)
_PROFILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("profile_name", "set_my_name", "name"),
    ("profile_short", "set_my_short_description", "short_description"),
    ("profile_description", "set_my_description", "description"),
)


# Profile field queue: PROFILE_LANGS × 3 fields = 72 calls. Seventy-two calls
# in parallel (as before), they hit flood control: prod 11.11.2026 received
# storm “Flood control exceeded on method 'SetMyName'. Retry in 79522 seconds” with
# 17:44 to 17:47 — and real users' responses broke during those minutes. Therefore the queue
# is sent one call at a time with a pause, and stops at the first failure; do not hammer
# Telegram is not possible; the hourly cycle handles the remainder.
# Second conclusion from the same measurement: repeated writes accumulate the penalty, while a restart
# the Space rewrote every field after deployment. Hence two rules: do not send
# a localized field if the text matches the default (the name “Lumen” is the same
# across all packs: 19 SetMyName calls collapse into one), and compare with
# Telegram before writing: anything already present is not written at all.
_profile_queue: list[tuple[str, dict[str, str]]] = []
_PROFILE_CALL_GAP_SEC = 0.6
_PROFILE_STARTUP_FIELDS = 20
_profile_sleep = asyncio.sleep
# Every set* has a matching get*: reads are barely rate-limited, while repeated writes
# of the same value costs the same limit allowance as the first.
_PROFILE_READ_METHODS = {
    "set_my_name": ("get_my_name", "name"),
    "set_my_short_description": ("get_my_short_description", "short_description"),
    "set_my_description": ("get_my_description", "description"),
}


def _profile_payloads() -> list[tuple[str, dict[str, str]]]:
    payloads: list[tuple[str, dict[str, str]]] = []
    for lang in PROFILE_LANGS:
        where = {} if lang == DEFAULT_LANG else {"language_code": lang}
        for key, method_name, arg_name in _PROFILE_FIELDS:
            value = _lang_t(lang, key)
            # For a language without its own text, Telegram shows the value for
            # by default: a separate record changes nothing, but consumes the limit.
            if lang != DEFAULT_LANG and value == _lang_t(DEFAULT_LANG, key):
                continue
            payloads.append((method_name, {arg_name: value, **where}))
    return payloads


def _profile_field_value(kwargs: dict[str, str]) -> str:
    return next(v for k, v in kwargs.items() if k != "language_code")


async def _profile_pending_payloads() -> list[tuple[str, dict[str, str]]]:
    """Fields Telegram does not yet have. A read error is non-blocking: an extra edit is better than a profile stuck in text from BotFather."""
    pending: list[tuple[str, dict[str, str]]] = []
    for method_name, kwargs in _profile_payloads():
        read = _PROFILE_READ_METHODS.get(method_name)
        method = getattr(bot, read[0], None) if read else None
        if method is None:
            pending.append((method_name, kwargs))
            continue
        getter_name, attr = read
        read_kwargs = {"language_code": kwargs["language_code"]} if "language_code" in kwargs else {}
        try:
            got = await _tg_call(method, **read_kwargs, call_timeout=10.0, retries=0)
            current = getattr(got, attr, None)
        except Exception as exc:
            log.info("[webhook] %s read skipped (%s): %s", getter_name, kwargs.get("language_code", DEFAULT_LANG), type(exc).__name__)
            current = None
        if current != _profile_field_value(kwargs):
            pending.append((method_name, kwargs))
    return pending


async def _register_bot_profile(limit: int | None = None) -> int:
    """The bot profile name and “About” text are set from code, not only through BotFather; otherwise they drift from lumen_lang and are not covered by tests. Nothing is raised: the profile does not affect message reception, so startup must not fail because of it."""
    global _profile_queue
    if bot is None:
        log.warning("[webhook] Bot profile skipped: bot instance is not initialized")
        return 0
    if not _profile_queue:
        _profile_queue = await _profile_pending_payloads()
        if not _profile_queue:
            log.info("[webhook] Bot profile already matches lumen_lang: nothing to write.")
            return 0

    done = 0
    while _profile_queue and (limit is None or done < limit):
        method_name, kwargs = _profile_queue[0]
        try:
            # retries=0: retrying on flood control only worsens the penalty.
            # R4-03 (audit 26.11.2026): an unknown result is not success — the profile write
            # is idempotent and will be rescheduled on the next startup; false-ok would suppress retries.
            _profile_res = await _tg_call(getattr(bot, method_name), **kwargs, call_timeout=15.0, retries=0)
            ok = _profile_res is not None and not _tg_outcome_unknown(_profile_res)
        except Exception as exc:
            log.warning(
                "[webhook] %s failed (%s): %s",
                method_name, kwargs.get("language_code", DEFAULT_LANG), exc,
            )
            ok = False
        if not ok:
            if not done:
                log.warning("[webhook] Bot profile was not set for any language.")
            log.info("[webhook] Bot profile paused: %d field(s) left for the hourly pass.", len(_profile_queue))
            return done
        _profile_queue.pop(0)
        done += 1
        if _profile_queue:
            await _profile_sleep(_PROFILE_CALL_GAP_SEC)
    if done:
        log.info("[webhook] Bot profile set for %d field(s); %d left.", done, len(_profile_queue))
    return done


async def _webhook_startup() -> None:
    global _state_ready
    # Synchronous disk read in a thread: otherwise startup blocks /webhook reception.
    await asyncio.to_thread(load_state_from_disk)
    _state_ready = True
    log.info("Bot startup: webhook mode.")
    # On Space, the cap can be overridden by env: without this line, the logs do not show
    # which limit actually works (26.10.2026 analysis).
    log.info(
        "[setup] TikTok limits: post=%.0fMB, slide=%.0fMB, slide concurrency=%d, Telegram upload=%.0fMB",
        TIKTOK_SLIDESHOW_MAX_BYTES / (1024 * 1024),
        TIKTOK_DOWNLOAD_MAX_BYTES / (1024 * 1024),
        TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY,
        TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES / (1024 * 1024),
    )
    _check_unconfirmed_model_quotas()
    # requests BEFORE Telegram tries to validate availability
    # /webhook when registered through setWebhook.
    await asyncio.sleep(1.5)

    space_host = os.getenv("SPACE_HOST", "").strip()
    if not space_host:
        author = os.getenv("SPACE_AUTHOR_NAME", "silverelixir").lower()
        repo = os.getenv("SPACE_REPO_NAME", "lumen").lower()
        space_host = f"{author}-{repo}.hf.space"
    webhook_url = f"https://{space_host}/webhook"

    log.info("[webhook] Space URL: https://%s", space_host)
    log.info("[webhook] Webhook endpoint: %s", webhook_url)
    # Do not print full secrets to logs, only a fingerprint; full ones go through
    # /admin_keys with Bearer BOT_TOKEN (a token in the URL remains in proxy logs).
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
    # Command descriptions in the client’s language; one language failing does not break the others.
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
        global BOT_USERNAME
        try:
            me_data = await telegram_api_call("getMe", {})
            if isinstance(me_data, dict) and me_data.get("username"):
                BOT_USERNAME = me_data["username"].strip().lstrip("@")
                log.info("[webhook] Dynamically fetched BOT_USERNAME: @%s", BOT_USERNAME)
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

        # setWebhook uses retries with exponential backoff: without them, a failure left
        # a silent, dead bot (audit D4, 30.09.2026). After exhaustion, propagate RuntimeError
        # so main stops the server and exits with code 1; the platform then restarts the
        # container instead of serving an empty webhook.
        webhook_ok = False
        backoff = SETWEBHOOK_BACKOFF_SEC
        for attempt in range(1, SETWEBHOOK_MAX_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    telegram_api_call("setWebhook", {
                        "url": webhook_url,
                        "secret_token": WEBHOOK_SECRET,
                        # drop_pending_updates=True — the owner's decision (27.09.2026): after
                        # downtime from replying in a burst to old messages. The cost is messages,
                        # updates that arrived while the bot was down are lost.
                        "drop_pending_updates": True,
                        "allowed_updates": ALLOWED_UPDATES,
                    }, request_timeout=15.0),
                    timeout=18.0
                )
                webhook_ok = True
                break
            except Exception as exc:
                log.warning(
                    "[webhook] setWebhook attempt %d/%d failed: %s",
                    attempt, SETWEBHOOK_MAX_ATTEMPTS, exc,
                )
                if attempt < SETWEBHOOK_MAX_ATTEMPTS:
                    await asyncio.sleep(backoff)
                    backoff *= 2
        if webhook_ok:
            log.info("[webhook] Webhook registered successfully: %s", webhook_url)
        else:
            # Do not put the secret in the log; the ready-made link lives in /webhook_url (Bearer ADMIN_PANEL_KEY, not query).
            log.error(
                '[webhook] setWebhook failed after %d attempts — register it manually via: curl -H "Authorization: Bearer <ADMIN_PANEL_KEY>" https://.../webhook_url (get ADMIN_PANEL_KEY via curl -H "Authorization: Bearer <BOT_TOKEN>" .../admin_keys if you don\'t have it handy)',
                SETWEBHOOK_MAX_ATTEMPTS,
            )
            return False

        cmd_payloads = [
            {"commands": [{"command": c.command, "description": c.description} for c in commands]}
        ] + [
            {
                "commands": [{"command": c.command, "description": c.description} for c in cmds],
                "language_code": code,
            }
            for code, cmds in localized_commands
        ]
        # 24 locales sequentially — that is 5-12 s of cold start through the proxy.
        # Register with limited concurrency: the menu does not affect replies,
        # but there is no desire to hit Telegram's flood control.
        _commands_sem = asyncio.Semaphore(4)

        async def _register_commands(cmd_payload: dict) -> None:
            async with _commands_sem:
                await asyncio.wait_for(
                    telegram_api_call("setMyCommands", cmd_payload, request_timeout=15.0),
                    timeout=18.0,
                )

        results = await asyncio.gather(
            *(_register_commands(payload) for payload in cmd_payloads),
            return_exceptions=True,
        )
        commands_ok = False
        for cmd_payload, result in zip(cmd_payloads, results):
            if isinstance(result, BaseException):
                log.warning("[webhook] setMyCommands failed (%s): %s", cmd_payload.get("language_code", "default"), result)
            else:
                commands_ok = True
        if commands_ok:
            log.info("[webhook] Bot commands set successfully.")
        else:
            log.warning("[webhook] Bot commands were not set for any language.")
        # The profile does not affect message reception, so its failure must not
        # go to main(): any startup error there means the container exits with code 1.
        # At startup, only the first fields: the hourly cycle adds the rest, otherwise
        # 72 consecutive calls hit flood control (production 11.11.2026).
        with contextlib.suppress(Exception):
            await _register_bot_profile(limit=_PROFILE_STARTUP_FIELDS)
        return webhook_ok

    setup_ok = await try_setup()
    if not setup_ok:
        raise RuntimeError("setWebhook failed after retries — refusing to serve a dead webhook")

    log.info("[webhook] Bot is running in webhook mode. Updates arrive via POST /webhook")
    # Daily model rechecks in the hourly loop: otherwise an expired promotion is visible
    # only after a restart.
    # The day matches the quota day (America/Los_Angeles), not the container's UTC
    # (review 17.11.2026).
    _last_daily_check_date = _current_quota_day()
    while True:
        await asyncio.sleep(3600)
        try:
            _cleanup_rate_limit_dict()
            _evict_orphan_chat_locks()
            # Profile fields not received at startup or inserted during flood control:
            # the hourly cycle drains the queue slowly, outside the live-answer path.
            await _register_bot_profile()
            # Quota counters — on every hourly tick, not just once a day: without
            # messages, the lazy check in _quota_entry does not fire and /stats lies.
            _reset_quota_if_new_day()
            today = _current_quota_day()
            if today != _last_daily_check_date:
                _last_daily_check_date = today
                _check_unconfirmed_model_quotas()
        except Exception:
            # A failed tick does not kill the loop: otherwise cleanup and quotas would stall forever.
            log.exception("[webhook] Hourly maintenance tick failed, next tick in an hour")

async def _drain_inflight_tasks() -> None:
    """Gives background update tasks a chance to finish cleanly during shutdown
    (LUMEN-2: the loop killed them in the middle of network calls during a
    redeploy). Any that do not finish are cancelled, and cancellation is awaited."""
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

def _require_bot_token() -> str:
    # An empty token previously died inside Bot() with TokenValidationError and caused restarts:
    # exit immediately with a clear reason.
    if not BOT_TOKEN:
        log.error("[setup] BOT_TOKEN пуст — задайте BOT_TOKEN/TELEGRAM_BOT_TOKEN и перезапустите.")
        raise SystemExit(1)
    return BOT_TOKEN


@contextlib.contextmanager
def _capture_signals_gracefully(server: Any):
    """SIGTERM/SIGINT: uvicorn re-raises the signal after
    serve() by default, so finally with the final state flush does not run
    (audit 28.10.2026). Intercept the signal and only ask the server to exit."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    original = {}

    def _stop(signum, frame):
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(Exception):
            original[sig] = signal.signal(sig, _stop)
    try:
        yield
    finally:
        for sig, handler in original.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)


async def main() -> None:
    global bot, client, _state_ready, _media_base_url

    _require_bot_token()

    global _media_base_url
    # A session with forced IPv4 and timeouts for HF Space. The aiogram head —
    # only a healthy relay without a body limit (otherwise the video fails at 4.5 MB).
    _media_base_url = await _choose_telegram_media_base() or TELEGRAM_API_BASE_URL
    log.info("[telegram] Media relay in use: %s", _proxy_host(_media_base_url) or "direct")
    if _media_base_url != "https://api.telegram.org":
        api_server = TelegramAPIServer.from_base(_media_base_url)
        sess = IPv4AiohttpSession(
            api=api_server,
            proxy_secret=LUMEN_PROXY_SECRET, proxy_base_urls=_TELEGRAM_PROXY_CANDIDATES,
        )
    else:
        sess = IPv4AiohttpSession(
            proxy_secret=LUMEN_PROXY_SECRET, proxy_base_urls=_TELEGRAM_PROXY_CANDIDATES,
        )
    bot = Bot(token=BOT_TOKEN, session=sess)
    # v1beta: system_instruction (“Developer instruction”) and response_modalities
    # on v1 respond "not enabled for api version v1" (prod 17.11.2026).
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )

    _state_ready = False
    startup_task = asyncio.create_task(_webhook_startup(), name="webhook_startup")
    flush_task = asyncio.create_task(_flush_dirty_state(), name="state_flush")
    media_watchdog = asyncio.create_task(_telegram_media_relay_watchdog(), name="media_relay_watchdog")
    srv_config = uvicorn.Config(
        # log_level=None: uvicorn itself enables uvicorn.access at any level
        # back to INFO and overrides suppression in _setup_logging — health-check
        # GET / was logged once a minute (production 27.10.2026).
        app=app, host="0.0.0.0", port=7860, log_level=None, loop="asyncio", log_config=None,
    )
    server = uvicorn.Server(srv_config)
    # Do not let uvicorn re-raise SIGTERM after serve(): otherwise the final
    # state reset and session closure are not performed.
    server.capture_signals = lambda: _capture_signals_gracefully(server)
    try:
        serve_task = asyncio.create_task(server.serve(), name="uvicorn_serve")
        done, _pending = await asyncio.wait(
            {startup_task, serve_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        # Startup is infinite (the hourly loop is at the end): if it ended, it crashed.
        # A failure was previously silent (await only in finally); the process kept serving
        # /webhook that Telegram did not call (audit D4, 30.09.2026).
        if startup_task in done and not startup_task.cancelled():
            exc = startup_task.exception()
            if exc is not None:
                log.error("[webhook] Startup failed, stopping the server: %s", exc)
                server.should_exit = True
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await serve_task
                raise SystemExit(1) from exc
        # The server crashed first (port occupied, uvicorn failure): without this branch
        # the exception was lost and the process silently exited with code 0.
        if serve_task in done and not serve_task.cancelled():
            exc = serve_task.exception()
            if exc is not None:
                log.error("[serve] HTTP server failed, stopping: %s", exc)
                raise SystemExit(1) from exc
            if getattr(server, "should_exit", False):
                log.info("[serve] HTTP server stopped gracefully")
            else:
                log.error("[serve] HTTP server exited unexpectedly, stopping")
                raise SystemExit(1)
    finally:
        startup_task.cancel()
        flush_task.cancel()
        media_watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
             await startup_task
        with contextlib.suppress(asyncio.CancelledError):
             await flush_task
        with contextlib.suppress(asyncio.CancelledError):
             await media_watchdog
        # Give unfinished updates a chance to finish BEFORE resetting state/closing sessions (otherwise chat_state edits were lost and the connection dropped — LUMEN-2).
        await _drain_inflight_tasks()
        # The final synchronous flush — do not wait for the next periodic
        # flush (once per FLUSH_INTERVAL_SEC); otherwise the latest changes between
        # the final tick and process shutdown would be lost on restart.
        # Flags live canonically in lumen_chat_state (P2): the sets are shared by object.
        # Read bool flags from the module because bot bindings became stale after extraction.
        # Synchronous write in a thread: the loop is already draining tasks, so it cannot hang.
        # Startup loading may have partially failed: we preserve the chats (this
        # sole copy of new messages), while the index and quota internally
        # are skipped so deleted data is not overwritten (audit A5-2).
        if _dirty_chat_ids or lumen_chat_state._index_dirty or _pending_chat_deletions:
            try:
                await asyncio.wait_for(asyncio.to_thread(_flush_state_now), timeout=15.0)
            except asyncio.TimeoutError:
                log.warning('[state] Final flush exceeded 15s; abandoning the remainder.')
        if lumen_chat_state._quota_dirty:
            try:
                await asyncio.wait_for(asyncio.to_thread(save_global_quota), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning('[state] Final quota save exceeded 10s; abandoning the remainder.')
        await _close_sessions()

if __name__ == "__main__":
    asyncio.run(main())
