"""
lumen_transport_calls.py — вызовы Telegram API: сессии, ротация прокси,
circuit breaker, _tg_call/telegram_api_call (вынесено из bot.py, P2 аудита).

Само состояние (TELEGRAM_API_BASE_URL, сессии, выключатель) живёт в bot.py —
здесь только операции над ним, связи через отложенный `import bot` внутри
функций. bot.py реэкспортирует имена — `bot._tg_call` и т.п. в тестах
и вызывающем коде не менялись.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any

import aiohttp
from aiogram.client.telegram import TelegramAPIServer

from lumen_telegram_transport import (
    IPv4AiohttpSession,
    _looks_like_proxy_garbage,
    close_telegram_session as _lumen_close_telegram_session,
    get_telegram_session as _lumen_get_telegram_session,
    proxy_auth_middlewares,
)

log = logging.getLogger("bot")

async def _get_telegram_session() -> aiohttp.ClientSession:
    import bot
    return await _lumen_get_telegram_session(
        bot.TELEGRAM_REQUEST_TIMEOUT,
        proxy_secret=bot.LUMEN_PROXY_SECRET, proxy_base_urls=bot._TELEGRAM_PROXY_CANDIDATES,
    )

async def _rotate_telegram_proxy() -> bool:
    """Переключается на следующий кандидат из _TELEGRAM_PROXY_CANDIDATES по кругу —
    вызывается из _tg_call/telegram_api_call сразу после срабатывания circuit breaker.
    Возвращает True, если переключились на кандидата, отличного от того, с которого
    начали этот заход (т.е. "круг" ещё не замкнулся — имеет смысл сразу попробовать
    новый адрес без паузы), и False, если кандидат только один или круг уже замкнулся
    (обошли всех и вернулись к началу) — в этом случае вызывающий код должен перейти
    в обычную паузу circuit breaker'а, как будто резервных прокси не было вовсе.

    _telegram_session (используется telegram_api_call) не требует пересоздания — это
    просто aiohttp.ClientSession с коннектором, URL собирается на лету из
    TELEGRAM_API_BASE_URL при каждом вызове. aiogram Bot.session — другое дело: сам
    целевой сервер (TelegramAPIServer) "запечён" в сессию при её создании, поэтому
    здесь она пересоздаётся заново, указывая на новый кандидат."""
    import bot
    async with bot._proxy_rotation_lock:
        if len(bot._TELEGRAM_PROXY_CANDIDATES) < 2:
            return False
        bot._telegram_proxy_idx = (bot._telegram_proxy_idx + 1) % len(bot._TELEGRAM_PROXY_CANDIDATES)
        new_url = bot._TELEGRAM_PROXY_CANDIDATES[bot._telegram_proxy_idx]
        old_url = bot.TELEGRAM_API_BASE_URL
        bot.TELEGRAM_API_BASE_URL = new_url
    log.warning('[telegram] Switching to fallback proxy: %s -> %s', old_url, new_url)
    if bot.bot is not None:
        old_session = bot.bot.session
        bot.bot.session = IPv4AiohttpSession(
            api=TelegramAPIServer.from_base(new_url),
            proxy_secret=bot.LUMEN_PROXY_SECRET, proxy_base_urls=bot._TELEGRAM_PROXY_CANDIDATES,
        )
        with contextlib.suppress(Exception):
            await old_session.close()
    return bot._telegram_proxy_idx != 0

async def _get_http_session() -> aiohttp.ClientSession:
    import bot
    if bot._http_session is None or bot._http_session.closed:
        bot._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            # limit поднят с 16 до 40 (24 июля 2026, ревизия TikTok-скачивания):
            # слайдшоу TikTok теперь скачивается целиком за раз через
            # asyncio.gather (см. handle_tiktok), и TikTok разрешает до 35 слайдов
            # в одном посте — со старым лимитом 16 часть слайдов ждала бы в
            # очереди на соединение вместо реально параллельной загрузки. Прочие
            # потребители этой же сессии (OpenRouter, Pollinations, TikWM-запросы)
            # используют на порядок меньше одновременных соединений, так что
            # повышение лимита их не затрагивает.
            connector=aiohttp.TCPConnector(family=socket.AF_INET, limit=40, ttl_dns_cache=300),
            middlewares=proxy_auth_middlewares(
                proxy_secret=bot.LUMEN_PROXY_SECRET,
                proxy_base_urls=(*bot._TELEGRAM_PROXY_CANDIDATES, *bot._tikwm_proxy_candidates()),
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

# безопасные обёртки над вызовами telegram

async def _handle_proxy_failure(context: str) -> None:
    """Общая реакция на "прокси вернул не-JSON/недоступен" — раньше этот блок
    (note_failure -> если сработал выключатель, попробовать резервный прокси без
    паузы, иначе уведомить владельца и включить паузу) был почти дословно
    продублирован в _tg_call и telegram_api_call. НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА:
    ровно тот класс дублирования, который проект уже устранял в других местах
    (_next_fallback_model, _model_error_text, _or_chat_completion_with_fallback) —
    здесь его просто не заметили при добавлении мультипрокси. `context` — короткое
    описание вызова для лога/уведомления владельца (например "call failed" или
    f"вызове {method}"), само решение (рубить ли попытку) остаётся на вызывающей
    стороне — эта функция только обновляет состояние выключателя и логирует."""
    import bot
    tripped = bot._tg_proxy_breaker.note_failure()
    if not tripped:
        log.warning(
            '[telegram] Proxy unavailable during %s (%d/%d in a row, circuit breaker not tripped yet).',
            context, bot._tg_proxy_breaker.consecutive_failures, bot._tg_proxy_breaker.trip_threshold,
        )
        return
    lap_not_done = await bot._rotate_telegram_proxy()
    if lap_not_done:
        # Есть ещё не испробованный в этом заходе кандидат — переключились на
        # него и сбрасываем счётчик, чтобы дать ему честный шанс без немедленной
        # паузы (см. _rotate_telegram_proxy).
        bot._tg_proxy_breaker.consecutive_failures = 0
        log.warning(
            '[telegram] Proxy unavailable during %s %d time(s) in a row — switching to fallback address %s without a pause.',
            context, bot._tg_proxy_breaker.trip_threshold, bot.TELEGRAM_API_BASE_URL,
        )
        return
    # Либо резервных прокси нет вообще, либо мы уже обошли их все по кругу за
    # этот заход — теперь действительно пауза. Уведомляем ДО trip(), иначе
    # собственный is_down-гейт _tg_call/telegram_api_call заблокирует само уведомление.
    await bot._notify_owner(
        f"⚠️ Telegram-прокси недоступен при {context} ({bot._tg_proxy_breaker.consecutive_failures} сбоев "
        f"подряд, резервные адреса тоже не помогли). Пауза {bot._tg_proxy_breaker.cooldown_sec:.0f}с. "
        f"Активный адрес: {bot.TELEGRAM_API_BASE_URL}"
    )
    bot._tg_proxy_breaker.trip()
    log.warning(
        '[telegram] Proxy unavailable during %s %d time(s) in a row (threshold %d) — pausing for %.0fs. Check availability of %s.',
        context, bot._tg_proxy_breaker.consecutive_failures, bot._tg_proxy_breaker.trip_threshold, bot._tg_proxy_breaker.cooldown_sec,
        bot.TELEGRAM_API_BASE_URL,
    )

async def _tg_call(method: Any, *args: Any, call_timeout: float | None = None, retries: int = 1, **kwargs: Any) -> Any:
    import bot
    now = bot.time.monotonic()
    if bot._tg_proxy_breaker.is_down(now):
        # Прокси уже недавно помечен недоступным (см. срабатывание ниже) — не бьёмся
        # заново в мёртвый прокси на каждое сообщение из бэклога, тихо возвращаем None,
        # как будто вызов не удался (вызывающий код и так умеет это обрабатывать).
        # Лог пишем не чаще раза в TELEGRAM_PROXY_COOLDOWN_SEC (см. log_still_down_if_due),
        # а не на каждый пропущенный вызов — иначе тот же лавинный спам никуда не
        # денется, просто сменит текст.
        bot._tg_proxy_breaker.log_still_down_if_due(now)
        return None
    last_exc = None
    timeout_val = call_timeout if call_timeout is not None else 35.0
    for attempt in range(retries + 1):
        try:
            result = await asyncio.wait_for(method(*args, **kwargs), timeout=timeout_val)
            bot._tg_proxy_breaker.note_success()
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    if last_exc is not None and "message is not modified" in str(last_exc).lower():
        # Это настоящий, валидный ответ Telegram API (сообщение не изменилось —
        # семантический не-op), а не признак сбоя прокси-звена — засчитываем как
        # успех, иначе безобидные повторные edit_text с тем же текстом ложно
        # накручивали бы счётчик сбоев прокси.
        bot._tg_proxy_breaker.note_success()
        return None
    if last_exc is not None and _looks_like_proxy_garbage(last_exc):
        # Не Telegram ответил ошибкой, а прокси перед ним отдал не-JSON (см.
        # _looks_like_proxy_garbage) — похоже на приостановку/лимит/сбой самого
        # прокси-хостинга (что бы это ни было — Vercel/Cloudflare/Deno/другое,
        # см. TELEGRAM_API_BASE_URL). Считаем это ОДНИМ сбоем в серии, а не
        # сразу включаем выключатель — единичная заминка на одной ноде anycast-
        # CDN не должна глушить ответы бота всем чатам целиком (см. историю
        # проекта: именно так один разовый глюк выглядел как "бот не отвечает").
        await bot._handle_proxy_failure("вызове (не-JSON ответ прокси)")
        return None
    log.warning("[telegram] call failed: %s", last_exc)
    return None

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
            # См. _handle_proxy_failure — выключатель срабатывает по счётчику
            # подряд идущих сбоев (см. _TelegramProxyCircuitBreaker), а не на первый же сбой.
            await bot._handle_proxy_failure(f"вызове {method}")
        raise RuntimeError(f"Network error in telegram_api_call for {method}: {exc_str}") from None
    if not isinstance(data, dict) or not data.get("ok"):
        # Прокси round-trip'нул нормально и вернул валидный JSON — сам факт, что
        # Telegram ответил "ok: false", НЕ вина прокси-звена, засчитываем успех.
        bot._tg_proxy_breaker.note_success()
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    bot._tg_proxy_breaker.note_success()
    return data["result"]
