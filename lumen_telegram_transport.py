"""
lumen_telegram_transport.py — низкоуровневый Telegram-транспорт: circuit breaker мёртвого прокси, детектор "прокси вернул мусор", TCP-коннектор, IPv4-сессия, кэш сессии.

Только самодостаточное: мутирующие TELEGRAM_API_BASE_URL/bot обёртки (_tg_call и др.) осознанно остались в bot.py — их вынос трогал бы десяток мест ради "чистого рефакторинга".
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

# Единый логгер "bot" (а не __name__) — тот же приём, что и в lumen_router_config.py/
# lumen_security.py, чтобы caplog.at_level(..., logger="bot") в тестах и реальные логи
# продолжали работать независимо от того, в каком физическом файле живёт код.
log = logging.getLogger("bot")


# Размазанные globals выключателя собраны в класс (поведение/формулы не менялись).
#
# Срабатывает по СЧЁТЧИКУ подряд идущих сбоев: раньше одна заминка ноды глушила все чаты на весь cooldown.
class _TelegramProxyCircuitBreaker:
    """Состояние выключателя (единственный инстанс — в bot.py); методы globals не трогают — проще тестировать."""

    def __init__(self, *, cooldown_sec: float, trip_threshold: int) -> None:
        self.cooldown_sec = cooldown_sec
        self.trip_threshold = trip_threshold
        self.down_until: float = 0.0
        self.down_logged_at: float = 0.0
        self.consecutive_failures: int = 0
        # Совокупный счётчик "прокси вернул не-JSON" за жизнь процесса — виден в /stats.
        self.garbage_event_count: int = 0

    def is_down(self, now: float) -> bool:
        return now < self.down_until

    def log_still_down_if_due(self, now: float) -> None:
        """Лог "прокси всё ещё недоступен" — не чаще раза в cooldown (иначе лавина WARNING из бэклога)."""
        if now - self.down_logged_at > self.cooldown_sec:
            self.down_logged_at = now
            log.warning('[telegram] Proxy still unavailable, skipping calls for another ~%.0fs.', self.down_until - now)

    def note_success(self) -> None:
        """Сбрасывает счётчик подряд идущих сбоев — вызывается на любой исход,
        который означает, что прокси реально ответил валидным JSON (успех ИЛИ
        настоящая ошибка Telegram уровня API), т.е. прокси-звено не виновато."""
        self.consecutive_failures = 0

    def note_failure(self) -> bool:
        """Увеличивает счётчик подряд идущих сбоев прокси (и общий счётчик для
        /stats). Возвращает True, если достигнут trip_threshold и пора включать
        выключатель (см. trip() ниже)."""
        self.consecutive_failures += 1
        self.garbage_event_count += 1
        return self.consecutive_failures >= self.trip_threshold

    def trip(self) -> None:
        now = time.monotonic()
        self.down_until = now + self.cooldown_sec
        self.down_logged_at = now

    def status_text(self) -> str:
        """Готовый HTML-фрагмент для /stats — раньше собирался в самой команде
        по четырём глобалам напрямую, теперь инкапсулирован вместе с состоянием."""
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
    """Отличает РЕАЛЬНУЮ ошибку Telegram API (валидный JSON вида {"ok": false, ...})
    от случая, когда сам HTTP-прокси перед Telegram (tg-proxy на Deno Deploy) вернул
    не-JSON тело — например, страницу приостановки аккаунта при исчерпанном лимите
    Deno ("USAGE_EXCEEDED"). Сигнатура именно этого случая — ошибка разбора JSON:
    Telegram, даже сообщая о СВОИХ ошибках, всегда отвечает валидным JSON, а вот
    прокси, упавший или приостановленный целиком, отдаёт HTML/plain-text, который
    ни json.loads, ни aiogram распарсить не могут."""
    low = str(exc).lower()
    cls = exc.__class__.__name__.lower()
    if "jsondecodeerror" in cls or "jsondecodeerror" in low:
        return True
    if "failed to decode" in low or "usage_exceeded" in low:
        return True
    # Прокси-хост вообще не принимает соединение (обрыв на уровне TCP/TLS, а не
    # ответ с ошибкой) — такой же надёжный сигнал "прокси недоступен целиком", как
    # и не-JSON ответ выше. Реальный инцидент без этой ветки: ClientConnectorError
    # ("Cannot connect to host ...") не ловился выключателем, и бот на каждое
    # сообщение заново пытался и подолгу ждал таймаута — вплоть до Duration 226754 ms
    # на одно сообщение, при том что проблема была одна и та же на протяжении часов.
    if "clientconnectorerror" in cls or "cannot connect to host" in low:
        return True
    return False


def _build_telegram_connector(limit: int) -> aiohttp.TCPConnector:
    """Общая конфигурация TCPConnector для соединений с Telegram API — используется
    и в get_telegram_session (aiohttp-сессия для telegram_api_call в bot.py), и в
    IPv4AiohttpSession (сессия самого aiogram Bot). Раньше эти два места дублировали
    один и тот же блок настроек по отдельности — вынесено сюда, чтобы будущая правка
    (например, очередная донастройка ttl_dns_cache/keepalive_timeout под конкретный
    прокси-хостинг) не требовала синхронизировать два места вручную.

    ttl_dns_cache сокращён с 300 до 10 сек: прокси-хостинг (Vercel/Cloudflare/Deno —
    anycast-CDN с множеством edge-нод по всему миру) мог "залипать" на одной
    подвисающей/перегруженной ноде на весь TTL DNS-кэша — отсюда сбои шли ПАЧКАМИ
    (несколько подряд, потом пауза), а не единично-случайно.
    keepalive_timeout сокращён до 15с вместо ранее пробовавшегося force_close=True:
    полное отключение keep-alive заставляло КАЖДЫЙ вызов (reply, send_message,
    get_file, typing-экшен и т.д. — на одно сообщение их несколько) платить полный
    TCP+TLS handshake — это перебор. Короткого keepalive_timeout достаточно, чтобы
    не залипать на плохой ноде надолго, но не требовать новый handshake на каждый вызов."""
    return aiohttp.TCPConnector(
        family=socket.AF_INET, limit=limit, ttl_dns_cache=10,
        keepalive_timeout=15.0, enable_cleanup_closed=True,
    )


PROXY_AUTH_HEADER = "X-Lumen-Proxy-Secret"


def proxy_auth_middlewares(
    *, proxy_secret: str = "", proxy_base_urls: Sequence[str] = (),
) -> tuple:
    scopes = []
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
    if scopes and (not proxy_secret or any(not 33 <= ord(c) <= 126 for c in proxy_secret)):
        raise ValueError("LUMEN_PROXY_SECRET is required for configured proxies and must be printable ASCII without spaces")

    async def authenticate(request: aiohttp.ClientRequest, handler):
        request.headers.popall(PROXY_AUTH_HEADER, None)
        url = request.url
        authenticated = url.scheme == "https" and any(
            url.host == host and url.port == port
            and (url.path == path or url.path.startswith(path + "/"))
            for host, port, path in scopes
        )
        if authenticated:
            request.headers[PROXY_AUTH_HEADER] = proxy_secret
        try:
            response = await handler(request)
        except Exception:
            if authenticated:
                raise RuntimeError("Authenticated proxy request failed") from None
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
        self, *, proxy_secret: str = "", proxy_base_urls: Sequence[str] = (), **kwargs,
    ) -> None:
        self._proxy_middlewares = proxy_auth_middlewares(
            proxy_secret=proxy_secret, proxy_base_urls=proxy_base_urls,
        )
        super().__init__(**kwargs)

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
    """Кэширующий геттер общей aiohttp-сессии для прямых HTTP-вызовов к Telegram Bot
    API (используется telegram_api_call в bot.py). `request_timeout` — значение
    TELEGRAM_REQUEST_TIMEOUT из bot.py, передаётся параметром на каждый вызов (а не
    импортируется статически), т.к. это часть публичной, потенциально настраиваемой
    через env конфигурации bot.py, а не константа этого модуля."""
    global _telegram_session, _telegram_session_auth
    auth = (proxy_secret, tuple(proxy_base_urls))
    middlewares = proxy_auth_middlewares(
        proxy_secret=proxy_secret, proxy_base_urls=auth[1],
    )
    if _telegram_session is not None and not _telegram_session.closed and _telegram_session_auth != auth:
        await _telegram_session.close()
    if _telegram_session is None or _telegram_session.closed:
        _telegram_session = aiohttp.ClientSession(
            connector=_build_telegram_connector(limit=10),
            timeout=aiohttp.ClientTimeout(total=request_timeout + 10.0, connect=10.0),
            middlewares=middlewares,
        )
        _telegram_session_auth = auth
    return _telegram_session


async def close_telegram_session() -> None:
    """Закрывает закешированную сессию, если она есть и ещё не закрыта — вызывается
    из _close_sessions в bot.py при остановке процесса."""
    if _telegram_session is not None and not _telegram_session.closed:
        await _telegram_session.close()
