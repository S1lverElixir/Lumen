"""lumen_telegram_transport.py is a self-contained low-level Telegram transport: a TCP
    connector, an IPv4 session with caching, a \"proxy returned garbage\" detector, and a circuit
    breaker class; mutating wrappers (_tg_call and others) live in lumen_transport_calls.py."""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

# Use the shared "bot" logger (not __name__) — the same approach as in lumen_router_config.py/
# in lumen_security.py, so caplog.at_level(..., logger="bot") in tests and real logs
# continued working independently of which physical file contained the code.
log = logging.getLogger("bot")


# The switch's scattered globals are consolidated into a class (behavior/formulas unchanged).
#
# An outage always means cooldown, not model quarantine: when fallbacks exist,
# the next request tries the reserve first.
class _TelegramProxyCircuitBreaker:
    """Circuit-breaker state (the only instance is in bot.py); methods do not touch globals, which makes testing easier."""

    def __init__(self, *, cooldown_sec: float, trip_threshold: int) -> None:
        self.cooldown_sec = cooldown_sec
        self.trip_threshold = trip_threshold
        self.down_until: float = 0.0
        self.down_logged_at: float = 0.0
        self.consecutive_failures: int = 0
        # Cumulative counter for 'proxy returned non-JSON' over the process lifetime, visible in /stats.
        self.garbage_event_count: int = 0

    def is_down(self, now: float) -> bool:
        return now < self.down_until

    def log_still_down_if_due(self, now: float) -> None:
        """Log \"proxy is still unavailable\" at most once per cooldown (otherwise a flood of WARNINGs from the backlog)."""
        if now - self.down_logged_at > self.cooldown_sec:
            self.down_logged_at = now
            log.warning('[telegram] Proxy still unavailable, skipping calls for another ~%.0fs.', self.down_until - now)

    def note_success(self) -> None:
        """Resets the consecutive-failure counter; called for any outcome that means the
        proxy actually responded with valid JSON (success OR a genuine Telegram
        API-level error), i.e. the proxy link is not at fault."""
        self.consecutive_failures = 0

    def note_failure(self) -> bool:
        """Increments the consecutive proxy failure counter (and the total counter for
        /stats). Returns True when trip_threshold is reached and it is time to open
        the circuit breaker (see trip() below)."""
        self.consecutive_failures += 1
        self.garbage_event_count += 1
        return self.consecutive_failures >= self.trip_threshold

    def trip(self) -> None:
        now = time.monotonic()
        self.down_until = now + self.cooldown_sec
        self.down_logged_at = now

    def status_text(self) -> str:
        """Ready-made HTML fragment for /stats: previously assembled in the command itself
        directly from four globals, now encapsulated together with the state.
        """
        now = time.monotonic()
        if now < self.down_until:
            state = f"ВЫКЛЮЧЕН ещё ~{int(self.down_until - now)}с"
        else:
            state = "в норме"
        return (
            f"\n\n<b>Telegram-прокси:</b> {state}\n"
            f"Подряд сбоев сейчас: {self.consecutive_failures}/{self.trip_threshold}, "
            f"всего за время работы: {self.garbage_event_count}"
        )


def _looks_like_proxy_garbage(exc: Exception) -> bool:
    """Distinguishes a REAL Telegram API error (valid JSON such as {"ok": false, ...})
    from a case where the HTTP proxy itself in front of Telegram (tg-proxy on Deno Deploy)
    returned a non-JSON body, such as an account suspension page after the Deno quota was
    exceeded ("USAGE_EXCEEDED"). The signature of this case is a JSON parse error:
    Telegram always returns valid JSON, even for ITS errors, while a proxy that has crashed
    or been suspended entirely returns HTML/plain text that neither json.loads nor aiogram
    can parse.
    """
    low = str(exc).lower()
    cls = exc.__class__.__name__.lower()
    if "jsondecodeerror" in cls or "jsondecodeerror" in low:
        return True
    if "failed to decode" in low or "usage_exceeded" in low:
        return True
    # The proxy host accepts no connection at all (a TCP/TLS-level disconnect, not
    # an error response) is as reliable a signal that "the proxy is entirely unavailable" as
    # and the non-JSON response above. A real incident without this branch: ClientConnectorError
    # ("Cannot connect to host ...") was not caught by the switch, and the bot called
    # message again and waited a long time for the timeout — up to Duration 226754 ms
    # to one message, even though it was the same problem for hours.
    if "clientconnectorerror" in cls or "cannot connect to host" in low:
        return True
    return False


def _build_telegram_connector(limit: int) -> aiohttp.TCPConnector:
    """Shared TCPConnector configuration for Telegram API connections, used by both
    get_telegram_session (the aiohttp session for telegram_api_call in bot.py) and
    IPv4AiohttpSession (the session of the aiogram Bot itself). These two locations
    used to duplicate the same settings block; it lives here so a future adjustment
    (for example, another ttl_dns_cache/keepalive_timeout tuning for a specific
    proxy host) does not require manually keeping two places in sync.

    ttl_dns_cache was reduced from 300 to 10 seconds: a proxy host
    (Vercel/Cloudflare/Deno, an anycast CDN with many edge nodes worldwide)
    could get stuck on one hung or overloaded node for the entire DNS cache TTL,
    causing failures in bursts (several in a row, then a pause) rather than
    isolated, random failures.
    keepalive_timeout was reduced to 15 seconds instead of the previously tried
    force_close=True: disabling keep-alive entirely made EVERY call (reply,
    send_message, get_file, typing action, etc., several per message) pay for a
    full TCP+TLS handshake, which is excessive. A short keepalive_timeout is
    enough to avoid staying stuck on a bad node for long without requiring a new
    handshake on every call."""
    return aiohttp.TCPConnector(
        family=socket.AF_INET, limit=limit, ttl_dns_cache=10,
        keepalive_timeout=15.0, enable_cleanup_closed=True,
    )


PROXY_AUTH_HEADER = "X-Lumen-Proxy-Secret"


def proxy_auth_middlewares(
    *, proxy_secret: str = "", proxy_base_urls: Sequence[str] = (),
) -> tuple:
    scopes = []
    read_scopes = []
    for base_url in proxy_base_urls:
        if not base_url:
            continue
        try:
            parsed = urlsplit(base_url)
            port = parsed.port or 443
        except ValueError:
            raise ValueError("Invalid proxy base URL") from None
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("Proxy base URL must use HTTPS without credentials, query or fragment")
        if parsed.hostname in {"api.telegram.org", "www.tikwm.com", "tikwm.com"}:
            continue
        scopes.append((parsed.hostname, port, parsed.path.rstrip("/")))
        # Page reading lives on the same origin behind a separate /read route:
        # without this entry, middleware removed the secret and the proxy returned 401
        # (audit 16.11.2026 — the feature was completely dead).
        read_scopes.append((parsed.hostname, port))
    if scopes and (not proxy_secret or len(proxy_secret) < 32 or any(not 33 <= ord(c) <= 126 for c in proxy_secret)):
        raise ValueError("LUMEN_PROXY_SECRET is required for configured proxies (min 32 printable ASCII chars without spaces)")

    async def authenticate(request: aiohttp.ClientRequest, handler):
        request.headers.popall(PROXY_AUTH_HEADER, None)
        url = request.url
        authenticated = url.scheme == "https" and (
            any(
                url.host == host and url.port == port
                and (url.path == path or url.path.startswith(path + "/"))
                for host, port, path in scopes
            )
            or (url.path == "/read" and any(
                url.host == host and url.port == port for host, port in read_scopes))
        )
        if authenticated:
            request.headers[PROXY_AUTH_HEADER] = proxy_secret
        try:
            response = await handler(request)
        except Exception as exc:
            if authenticated:
                # Do not break the chain (from exc) + include the root-cause type in the text
                # for Sentry.
                raise RuntimeError(f"Authenticated proxy request failed: {type(exc).__name__}") from exc
            raise
        finally:
            request.headers.popall(PROXY_AUTH_HEADER, None)
        if authenticated:
            safe_headers = response.request_info.headers.copy()
            safe_headers.popall(PROXY_AUTH_HEADER, None)
            response._request_info = aiohttp.RequestInfo(
                url=response.request_info.url, method=response.request_info.method,
                headers=safe_headers, real_url=response.request_info.real_url,
            )
        if authenticated and 300 <= response.status < 400:
            response.close()
            raise RuntimeError("Authenticated proxy redirects are disabled")
        return response

    return (authenticate,)


class IPv4AiohttpSession(AiohttpSession):
    def __init__(
        self, *, proxy_secret: str = "", proxy_base_urls: Sequence[str] = (),
        # aiogram applies self.timeout to each request on top of the session default:
        # without explicit 30s here, aiogram's silent 60s would apply instead of total=30 below.
        timeout: float = 30.0, **kwargs,
    ) -> None:
        self._proxy_middlewares = proxy_auth_middlewares(
            proxy_secret=proxy_secret, proxy_base_urls=proxy_base_urls,
        )
        super().__init__(timeout=timeout, **kwargs)

    async def create_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=_build_telegram_connector(limit=30),
                timeout=aiohttp.ClientTimeout(total=30.0, connect=10.0, sock_read=20.0),
                json_serialize=self.json_dumps,
                middlewares=self._proxy_middlewares,
            )
        return self._session


_telegram_session: aiohttp.ClientSession | None = None
_telegram_session_auth: tuple[str, tuple[str, ...]] | None = None


async def get_telegram_session(
    request_timeout: float, *, proxy_secret: str = "", proxy_base_urls: Sequence[str] = (),
) -> aiohttp.ClientSession:
    """Cached getter for the shared aiohttp session used for direct HTTP calls to the Telegram Bot API (used by telegram_api_call in bot.py). request_timeout is the TELEGRAM_REQUEST_TIMEOUT value from bot.py, passed on every call rather than statically imported, because it is part of bot.py's public, potentially env-configurable configuration, not a constant of this module."""
    global _telegram_session, _telegram_session_auth
    auth = (proxy_secret, tuple(proxy_base_urls))
    if _telegram_session is not None and not _telegram_session.closed and _telegram_session_auth != auth:
        await _telegram_session.close()
    if _telegram_session is None or _telegram_session.closed:
        # Assemble only on creation: when reusing the cache, this was
        # unnecessary work on every call (17.11.2026 review).
        middlewares = proxy_auth_middlewares(
            proxy_secret=proxy_secret, proxy_base_urls=auth[1],
        )
        _telegram_session = aiohttp.ClientSession(
            connector=_build_telegram_connector(limit=10),
            timeout=aiohttp.ClientTimeout(total=request_timeout + 10.0, connect=10.0),
            middlewares=middlewares,
        )
        _telegram_session_auth = auth
    return _telegram_session


async def close_telegram_session() -> None:
    """Closes the cached session if it exists and is not already closed — called
    from _close_sessions in bot.py when the process stops."""
    if _telegram_session is not None and not _telegram_session.closed:
        await _telegram_session.close()
