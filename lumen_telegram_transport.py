"""
lumen_telegram_transport.py — низкоуровневый Telegram-транспорт: circuit breaker для
мёртвого HTTP-прокси перед Telegram Bot API, распознавание "прокси вернул мусор, а не
JSON", общая конфигурация TCP-коннектора и кэш aiohttp-сессии для прямых HTTP-вызовов.

Вынесено из bot.py при разбиении на модули (см. README, аудит техдолга). НЕ включает
`_tg_call`/`telegram_api_call`/`_rotate_telegram_proxy`/`_handle_proxy_failure` — эти
функции читают И мутируют `TELEGRAM_API_BASE_URL`/`bot`/`BOT_TOKEN`, которые в bot.py
используются ещё в добром десятке несвязанных мест (`/diag`, скачивание файлов из
Telegram, `main()` и т.д.). Вынос этой части потребовал бы переписывать все эти сайты
на доступ через новый модуль вместо простого чтения module-level переменной — реальный
риск регрессии ради небольшого выигрыша, не стоящий того при "чистом рефакторинге без
изменения поведения". Эта часть осознанно остаётся в bot.py как тонкая обёртка поверх
перенесённых сюда строительных блоков (см. секцию "Telegram-транспорт" там же).

Всё, что действительно самодостаточно (не требует global-мутации TELEGRAM_API_BASE_URL/
bot) — здесь: сам класс выключателя (`_TelegramProxyCircuitBreaker`), детектор "не-JSON
от прокси" (`_looks_like_proxy_garbage`), конфигурация `TCPConnector`
(`_build_telegram_connector`), aiogram-сессия с принудительным IPv4 (`IPv4AiohttpSession`)
и кэш aiohttp-сессии для `telegram_api_call` в bot.py (`get_telegram_session`/
`close_telegram_session`).
"""

from __future__ import annotations

import logging
import socket
import time

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

# Единый логгер "bot" (а не __name__) — тот же приём, что и в lumen_router_config.py/
# lumen_security.py, чтобы caplog.at_level(..., logger="bot") в тестах и реальные логи
# продолжали работать независимо от того, в каком физическом файле живёт код.
log = logging.getLogger("bot")


# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: состояние "выключателя" мёртвого Telegram-прокси
# раньше жило как четыре независимых module-level globals (_tg_proxy_down_until/
# _tg_proxy_down_logged_at/_tg_proxy_consecutive_failures/_tg_proxy_garbage_event_count),
# мутируемых через `global` из двух разных функций (_tg_call/telegram_api_call) —
# такое размазанное состояние сложнее читать и тестировать, чем один объект с
# понятными методами. _TelegramProxyCircuitBreaker ниже — чистая инкапсуляция,
# поведение (включая формулы cooldown/threshold) не изменилось ни на йоту.
#
# Выключатель срабатывает по СЧЁТЧИКУ подряд идущих сбоев, а не на первый же
# сбой. Раньше ОДНА-единственная заминка прокси (например разовый сетевой глюк
# на одной ноде anycast-CDN — Vercel/Cloudflare/Deno все матчат запросы на
# множество географически разных нод) полностью глушила ответы бота ВСЕМ чатам
# на TG_PROXY_COOLDOWN_SEC секунд — то есть один случайный сбой был неотличим
# от реально упавшего прокси. Теперь выключатель включается, только когда
# подряд (без единого успеха между ними) накопилось trip_threshold сбоев —
# единичные заминки его больше не запускают.
class _TelegramProxyCircuitBreaker:
    """Инкапсулирует состояние выключателя — см. комментарий выше. Используется
    как единственный module-level инстанс (_tg_proxy_breaker в bot.py), но методы
    не трогают globals напрямую, что делает поведение проще проверять."""

    def __init__(self, *, cooldown_sec: float, trip_threshold: int) -> None:
        self.cooldown_sec = cooldown_sec
        self.trip_threshold = trip_threshold
        self.down_until: float = 0.0
        self.down_logged_at: float = 0.0
        self.consecutive_failures: int = 0
        # Совокупный (не сбрасывается) счётчик срабатываний "прокси вернул не-JSON"
        # за время жизни процесса — виден через /stats, чтобы деградацию прокси
        # можно было заметить прямо из Telegram, а не только копаясь в логах контейнера.
        self.garbage_event_count: int = 0

    def is_down(self, now: float) -> bool:
        return now < self.down_until

    def log_still_down_if_due(self, now: float) -> None:
        """Логирует "прокси всё ещё недоступен" не чаще раза в cooldown_sec —
        иначе лавина одинаковых WARNING на каждый пропущенный вызов из бэклога."""
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


class IPv4AiohttpSession(AiohttpSession):
    async def create_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=_build_telegram_connector(limit=30),
                timeout=aiohttp.ClientTimeout(total=30.0, connect=10.0, sock_read=20.0),
                json_serialize=self.json_dumps,
            )
        return self._session


_telegram_session: aiohttp.ClientSession | None = None


async def get_telegram_session(request_timeout: float) -> aiohttp.ClientSession:
    """Кэширующий геттер общей aiohttp-сессии для прямых HTTP-вызовов к Telegram Bot
    API (используется telegram_api_call в bot.py). `request_timeout` — значение
    TELEGRAM_REQUEST_TIMEOUT из bot.py, передаётся параметром на каждый вызов (а не
    импортируется статически), т.к. это часть публичной, потенциально настраиваемой
    через env конфигурации bot.py, а не константа этого модуля."""
    global _telegram_session
    if _telegram_session is None or _telegram_session.closed:
        _telegram_session = aiohttp.ClientSession(
            connector=_build_telegram_connector(limit=10),
            timeout=aiohttp.ClientTimeout(total=request_timeout + 10.0, connect=10.0),
        )
    return _telegram_session


async def close_telegram_session() -> None:
    """Закрывает закешированную сессию, если она есть и ещё не закрыта — вызывается
    из _close_sessions в bot.py при остановке процесса."""
    if _telegram_session is not None and not _telegram_session.closed:
        await _telegram_session.close()
