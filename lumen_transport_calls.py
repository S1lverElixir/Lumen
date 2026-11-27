"""lumen_transport_calls.py — Telegram API calls: proxy rotation, retries, _tg_call/telegram_api_call; the breaker class and sessions are defined in lumen_telegram_transport and imported here; this module only uses them."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
import socket
from typing import Any

import aiohttp
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramUnauthorizedError,
)

from lumen_errors import _error_status
from lumen_telegram_transport import (
    IPv4AiohttpSession,
    _looks_like_proxy_garbage,
    close_telegram_session as _lumen_close_telegram_session,
    get_telegram_session as _lumen_get_telegram_session,
    proxy_auth_middlewares,
)

log = logging.getLogger("bot")

# Deferred closures of old aiogram sessions: keep references so garbage collection
# does not kill the task prematurely.
_deferred_session_closes: set[asyncio.Task] = set()


async def _close_session_later(session) -> None:
    """Let live requests finish: closing the session underneath them caused
    \"Session is closed\" and lost edits from other chats (review 17.11.2026)."""
    import bot
    try:
        await asyncio.sleep(max(5.0, float(bot.TELEGRAM_REQUEST_TIMEOUT) + 5.0))
        with contextlib.suppress(Exception):
            await session.close()
    except asyncio.CancelledError:
        raise


async def _get_telegram_session() -> aiohttp.ClientSession:
    import bot
    return await _lumen_get_telegram_session(
        bot.TELEGRAM_REQUEST_TIMEOUT,
        proxy_secret=bot.LUMEN_PROXY_SECRET, proxy_base_urls=bot._TELEGRAM_PROXY_CANDIDATES,
    )

async def _rotate_telegram_proxy() -> bool:
    """The next candidate in _TELEGRAM_PROXY_CANDIDATES, in rotation. True means the cycle
    has not closed, so we can try immediately without pausing; False means there are
    no candidates or all have been tried, and the caller pauses for the breaker. The
    aiogram session is recreated (the server is baked into it); the aiohttp session
    is not (the URL is assembled on the fly)."""
    import bot
    async with bot._proxy_rotation_lock:
        if len(bot._TELEGRAM_PROXY_CANDIDATES) < 2:
            return False
        bot._telegram_proxy_idx = (bot._telegram_proxy_idx + 1) % len(bot._TELEGRAM_PROXY_CANDIDATES)
        new_url = bot._TELEGRAM_PROXY_CANDIDATES[bot._telegram_proxy_idx]
        old_url = bot.TELEGRAM_API_BASE_URL
        bot.TELEGRAM_API_BASE_URL = new_url
        # Session replacement and closure also happen under the lock: otherwise, two concurrent failures
        # read the same old_session and leave the new one unclosed (audit 16.11.2026).
        if bot.bot is not None:
            old_session = bot.bot.session
            bot.bot.session = IPv4AiohttpSession(
                api=TelegramAPIServer.from_base(new_url),
                proxy_secret=bot.LUMEN_PROXY_SECRET, proxy_base_urls=bot._TELEGRAM_PROXY_CANDIDATES,
            )
            # Do not close under live requests: defer and keep a reference.
            task = asyncio.create_task(_close_session_later(old_session))
            _deferred_session_closes.add(task)
            task.add_done_callback(_deferred_session_closes.discard)
    log.warning('[telegram] Switching to fallback proxy: %s -> %s', old_url, new_url)
    return bot._telegram_proxy_idx != 0

async def _get_http_session() -> aiohttp.ClientSession:
    import bot
    if bot._http_session is None or bot._http_session.closed:
        from lumen_tiktok import _PublicOnlyResolver
        bot._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            # limit 40 (24.07.2026): TikTok slideshows of up to 35 slides download concurrently via gather; with the old 16, some would wait in the queue. Other consumers use an order of magnitude fewer connections.
            connector=aiohttp.TCPConnector(
                family=socket.AF_INET, limit=40, ttl_dns_cache=300,
                # The resolver cuts non-public IPs on every resolution: this closes
                # DNS rebinding for all external requests made by this session.
                resolver=_PublicOnlyResolver(),
            ),
            middlewares=proxy_auth_middlewares(
                proxy_secret=bot.LUMEN_PROXY_SECRET,
                proxy_base_urls=(
                    *bot._TELEGRAM_PROXY_CANDIDATES, *bot._tikwm_proxy_candidates(),
                    *bot._cloudflare_proxy_candidates(),
                ),
            ),
        )
    return bot._http_session

async def _close_sessions() -> None:
    import bot
    await _lumen_close_telegram_session()
    if bot._http_session is not None and not bot._http_session.closed:
        await bot._http_session.close()
    if bot.bot is not None and hasattr(bot.bot, "session") and bot.bot.session:
        await bot.bot.session.close()

# safe wrappers around Telegram calls

# _handle_proxy_failure can reenter itself: owner notification uses _tg_call before trip(),
# so another failure of the same proxy calls the handler again in the same task.
# A context-local flag, rather than a global one, lets parallel tasks handle failures independently.
# The nested branch is disabled only within that task.
_proxy_failure_nested: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "proxy_failure_nested", default=False,
)


async def _handle_proxy_failure(context: str) -> None:
    """Handles \"proxy returned non-JSON/is unavailable\" (previously duplicated in _tg_call and telegram_api_call). It only updates the breaker and logs; the caller decides whether to abort the attempt."""
    import bot
    if _proxy_failure_nested.get():
        # Nested failure from _notify_owner below: the counter still grows, but a second
        # call does not start the notification branch, or the chain would repeat.
        bot._tg_proxy_breaker.note_failure()
        return
    tripped = bot._tg_proxy_breaker.note_failure()
    if not tripped:
        log.warning(
            '[telegram] Proxy unavailable during %s (%d/%d in a row, circuit breaker not tripped yet).',
            context, bot._tg_proxy_breaker.consecutive_failures, bot._tg_proxy_breaker.trip_threshold,
        )
        return
    lap_not_done = await bot._rotate_telegram_proxy()
    if lap_not_done:
        # There is another candidate not yet tried in this run; switch to
        # it and reset the counter to give it a fair chance without an immediate
        # pause (see _rotate_telegram_proxy).
        bot._tg_proxy_breaker.consecutive_failures = 0
        log.warning(
            '[telegram] Proxy unavailable during %s %d time(s) in a row — switching to fallback address %s without a pause.',
            context, bot._tg_proxy_breaker.trip_threshold, bot.TELEGRAM_API_BASE_URL,
        )
        return
    # Either there are no reserve proxies at all, or we have already cycled through all of them during
    # this pass — now it is a real pause. Notify BEFORE trip(), otherwise
    # its own is_down gate for _tg_call/telegram_api_call would block the notification itself.
    _nested_token = _proxy_failure_nested.set(True)
    try:
        await bot._notify_owner(
            f"⚠️ Telegram-прокси недоступен при {context} ({bot._tg_proxy_breaker.consecutive_failures} сбоев "
            f"подряд, резервные адреса тоже не помогли). Пауза {bot._tg_proxy_breaker.cooldown_sec:.0f}с. "
            f"Активный адрес: {bot.TELEGRAM_API_BASE_URL}"
        )
    finally:
        _proxy_failure_nested.reset(_nested_token)
    bot._tg_proxy_breaker.trip()
    log.warning(
        '[telegram] Proxy unavailable during %s %d time(s) in a row (threshold %d) — pausing for %.0fs. Check availability of %s.',
        context, bot._tg_proxy_breaker.consecutive_failures, bot._tg_proxy_breaker.trip_threshold, bot._tg_proxy_breaker.cooldown_sec,
        bot.TELEGRAM_API_BASE_URL,
    )

# 4xx (except 429 flooding): a retry will not help, so do not retry such a call.
_TG_CLIENT_ERRORS = (
    TelegramBadRequest,
    TelegramUnauthorizedError,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramMigrateToChat,
)


def _tg_retry_after_sec(exc: Exception) -> float | None:
    # Direct aiogram TelegramRetryAfter attribute; fallback: textual
    # templates "retry after N"/"retry in N" (flooding without waiting extended the ban).
    val = getattr(exc, "retry_after", None)
    try:
        if val is not None and float(val) > 0:
            return float(val)
    except (TypeError, ValueError):
        pass
    m = re.search(r"(?i)retry[_ ]after\D{0,8}(\d+)|retry in (\d+)", str(exc))
    if m:
        with contextlib.suppress(TypeError, ValueError):
            return float(m.group(1) or m.group(2))
    return None


def _tg_is_client_error(exc: Exception) -> bool:
    # aiogram types cover real 4xx; status parsed from text is a fallback
    # for untyped errors (after A8-04, it no longer has false positives).
    if isinstance(exc, _TG_CLIENT_ERRORS):
        return True
    try:
        status = _error_status(exc, str(exc))
    except Exception:
        return False
    return status in (400, 401, 403, 404, 409, 413, 422)


def _tg_is_post_response_garbage(exc: Exception) -> bool:
    # The response may already have been accepted; retrying a mutation creates a duplicate.
    # Connection failures are excluded: the request was not sent, so they remain definite None.
    if isinstance(exc, (UnicodeDecodeError, aiohttp.ContentTypeError)):
        return True
    if isinstance(exc, aiohttp.ClientConnectorError):
        return False
    return _looks_like_proxy_garbage(exc) and "cannot connect to host" not in str(exc).lower()


class _TgUnknownOutcome:
    # R4-03 (audit 26.11.2026): timeout/disconnect after sending the request — Telegram
    # could accept the mutation. None means "definitely not delivered": the caller
    # code returns the slot or retries sending, while an unknown outcome caused duplicates
    # media and free retries. False used to have the same outcome as None, so an
    # is-check matters wherever a retry has a real cost.
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<tg:unknown-outcome>"


_TG_UNKNOWN = _TgUnknownOutcome()


def _tg_outcome_unknown(value: Any) -> bool:
    return value is _TG_UNKNOWN


def _tg_cancelled_unknown(exc: asyncio.CancelledError, *methods: Any) -> bool:
    """Whether cancellation interrupted a Telegram mutation whose delivery is unknown."""
    if not getattr(exc, "_lumen_tg_unknown", False):
        return False
    return not methods or getattr(exc, "_lumen_tg_method", None) in methods


async def _tg_call(method: Any, *args: Any, call_timeout: float | None = None, retries: int = 1, **kwargs: Any) -> Any:
    # Contract: the method result or None (breaker down, client error,
    # the request definitely failed) or _TG_UNKNOWN (it may have succeeded: timeout/disconnect after
    # send). _TG_UNKNOWN is falsy, but not None (R4-03, audit 26.11.2026).
    import bot
    now = bot.time.monotonic()
    if bot._tg_proxy_breaker.is_down(now):
        # The breaker is already open: skip the call and preserve the silent None result.
        # Log at most once per cooldown; None here is not a successful response.
        bot._tg_proxy_breaker.log_still_down_if_due(now)
        return None
    last_exc = None
    unknown_outcome = False
    timeout_val = call_timeout if call_timeout is not None else 35.0
    for attempt in range(retries + 1):
        try:
            result = await asyncio.wait_for(method(*args, **kwargs), timeout=timeout_val)
            bot._tg_proxy_breaker.note_success()
            return result
        except asyncio.CancelledError as exc:
            # Cancellation can arrive after Telegram accepted the mutation. Preserve the
            # cancellation for task control, but mark the delivery outcome for refund guards.
            exc._lumen_tg_unknown = True
            exc._lumen_tg_method = method
            raise
        except asyncio.TimeoutError as exc:
            # A timeout has an unknown outcome (the request may have arrived): resending
            # would produce a duplicate. Do not retry, as for client errors. But for breaker
            # this is a failure: otherwise a silent proxy would never trigger the fallback
            # (review 17.11.2026).
            bot._tg_proxy_breaker.note_failure()
            last_exc = exc
            unknown_outcome = True
            break
        except Exception as exc:
            last_exc = exc
            if _tg_is_client_error(exc):
                # Client error — valid JSON from Telegram, proxy is healthy:
                # without resetting the counter, a series of "garbage" proxy responses after one
                # such an error silenced every chat for the cooldown period (audit 16.11.2026).
                bot._tg_proxy_breaker.note_success()
                break
            if _tg_is_post_response_garbage(exc):
                # JSON/decoding failed after a response: the request may have completed.
                # Do not retry or return None; a consumer would refund the slot or send a duplicate.
                await bot._handle_proxy_failure("вызове (не-JSON ответ прокси)")
                return _TG_UNKNOWN
            if isinstance(exc, TelegramNetworkError) and "timeout" in str(exc).lower():
                # The aiogram-session timeout has the same nature as above: it could reach Telegram.
                bot._tg_proxy_breaker.note_failure()
                unknown_outcome = True
                break
            if isinstance(exc, TelegramNetworkError) and any(
                k in str(exc).lower() for k in ("reset", "aborted", "eof", "disconnected")
            ):
                # Disconnect AFTER sending (unlike "cannot connect", where the request
                # did not get through): retrying the mutation would create a duplicate (R4-03, audit 26.11.2026).
                bot._tg_proxy_breaker.note_failure()
                unknown_outcome = True
                break
            if attempt < retries:
                # Wait retry_after for flood control; use a short backoff for everything else.
                # Cap the sleep: a long retry_after would otherwise hold the chat lock
                # beyond the route budget (audit 10.2026). Retrying after the cap is still allowed.
                wait = _tg_retry_after_sec(exc) or 0.5 * (attempt + 1)
                await asyncio.sleep(min(wait, bot.TELEGRAM_FLOOD_SLEEP_MAX_SEC))
    if last_exc is not None and "message is not modified" in str(last_exc).lower():
        # "message is not modified" is a valid API response (a semantic no-op), not a
        #     failure: count it as success, or repeated edit_text calls would inflate the counter.
        bot._tg_proxy_breaker.note_success()
        return None
    if last_exc is not None and _looks_like_proxy_garbage(last_exc):
        # A non-JSON response from the proxy is an isolated node hiccup, not a reason to
        # silence all chats: count it as ONE failure in a series (precedent: a one-off glitch
        # looked like "the bot is not responding").
        await bot._handle_proxy_failure("вызове (не-JSON ответ прокси)")
        return None
    log.warning("[telegram] call failed: %s", last_exc)
    return _TG_UNKNOWN if unknown_outcome else None

async def telegram_api_call(method: str, payload: dict, *, request_timeout: float | None = None) -> Any:
    import bot
    if bot._tg_proxy_breaker.is_down(bot.time.monotonic()):
        raise RuntimeError(f"Telegram API {method}: proxy is currently marked unavailable (see previous [telegram] warnings), skipping the network call.")
    url = f"{bot.TELEGRAM_API_BASE_URL}/bot{bot.BOT_TOKEN}/{method}"
    session = await bot._get_telegram_session()
    pruned = bot._json_prune_defaults(payload)
    timeout = aiohttp.ClientTimeout(total=request_timeout or bot.TELEGRAM_REQUEST_TIMEOUT)
    try:
        async with session.post(url, json=pruned, timeout=timeout) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        exc_str = str(exc) or repr(exc) or type(exc).__name__
        if bot.BOT_TOKEN:
            exc_str = exc_str.replace(bot.BOT_TOKEN, "<TOKEN>")
        if _looks_like_proxy_garbage(exc):
            # See _handle_proxy_failure: the breaker opens on the configured consecutive-failure
            # count (_TelegramProxyCircuitBreaker), not on the first failure.
            await bot._handle_proxy_failure(f"вызове {method}")
        # Preserve the chain with from exc: Sentry must see the root cause, not just the wrapper.
        raise RuntimeError(f"Network error in telegram_api_call for {method}: {exc_str}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        # The proxy completed the round trip and returned valid JSON. Telegram's "ok: false"
        # is an API-level rejection, not a proxy-link failure, so count the breaker as success.
        bot._tg_proxy_breaker.note_success()
        # Text gets only the code and description, not the entire response dictionary.
        if isinstance(data, dict):
            detail = f"{data.get('error_code', '?')} {data.get('description', '')}".strip()
        else:
            detail = repr(data)[:300]
        raise RuntimeError(f"Telegram API {method} failed: {detail}")
    bot._tg_proxy_breaker.note_success()
    return data["result"]
