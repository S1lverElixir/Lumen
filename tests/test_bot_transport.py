"""
test_bot_transport.py — Транспорт: прокси-мидлварь, circuit breaker, ротация прокси, задачи shutdown.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
import asyncio
import bot
import lumen_telegram_transport
import pytest
import time
from unittest.mock import MagicMock
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from tests.bot_test_helpers import (
    _run_proxy_middleware,
)


def test_circuit_breaker_starts_closed():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    assert breaker.is_down(time.monotonic()) is False


def test_circuit_breaker_does_not_trip_before_threshold():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    assert breaker.note_failure() is False
    assert breaker.note_failure() is False
    assert breaker.is_down(time.monotonic()) is False


def test_circuit_breaker_trips_at_threshold():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_failure()
    tripped = breaker.note_failure()
    assert tripped is True
    breaker.trip()
    assert breaker.is_down(time.monotonic()) is True


def test_circuit_breaker_success_resets_consecutive_failures():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_failure()
    breaker.note_success()
    assert breaker.consecutive_failures == 0
    # Единичные последующие сбои не должны сразу срабатывать — счётчик правда сброшен.
    assert breaker.note_failure() is False


def test_circuit_breaker_garbage_event_count_never_resets():
    # В отличие от consecutive_failures, совокупный счётчик для /stats копится
    # за всё время жизни процесса и не должен сбрасываться на success.
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_success()
    breaker.note_failure()
    assert breaker.garbage_event_count == 2


def test_circuit_breaker_status_text_reflects_state():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    assert "в норме" in breaker.status_text()
    breaker.note_failure()
    breaker.note_failure()
    breaker.note_failure()
    breaker.trip()
    assert "ВЫКЛЮЧЕН" in breaker.status_text()


def test_track_inflight_task_registers_and_self_removes_on_completion():
    bot._inflight_tasks.clear()

    async def quick():
        return "done"

    async def _run():
        task = bot._track_inflight_task(asyncio.create_task(quick()))
        assert task in bot._inflight_tasks
        await task
        # done_callback снимает таску из набора сама, без ручной чистки.
        assert task not in bot._inflight_tasks

    asyncio.run(_run())


def test_drain_inflight_tasks_waits_for_quick_task_to_finish_on_its_own():
    bot._inflight_tasks.clear()
    finished = []

    async def quick():
        await asyncio.sleep(0)
        finished.append(1)

    async def _run():
        bot._track_inflight_task(asyncio.create_task(quick()))
        await bot._drain_inflight_tasks()

    asyncio.run(_run())
    assert finished == [1]
    assert not bot._inflight_tasks


def test_drain_inflight_tasks_cancels_tasks_that_time_out():
    bot._inflight_tasks.clear()
    original_timeout = bot.INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC
    bot.INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC = 0.01
    cancelled = []

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    async def _run():
        bot._track_inflight_task(asyncio.create_task(slow()))
        await bot._drain_inflight_tasks()

    try:
        asyncio.run(_run())
        assert cancelled == [1]
    finally:
        bot.INFLIGHT_TASKS_SHUTDOWN_TIMEOUT_SEC = original_timeout


def test_drain_inflight_tasks_noop_when_nothing_pending():
    bot._inflight_tasks.clear()
    # Явный failure вместо неявного pass: регрессия даст понятный отчёт, а не голый error.
    try:
        asyncio.run(bot._drain_inflight_tasks())
    except Exception as exc:
        raise AssertionError(f"drain on empty set must never raise, got {exc!r}") from exc


def test_rotate_telegram_proxy_noop_with_single_candidate():
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://only-one.example.com"]
    bot._telegram_proxy_idx = 0
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is False
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx


def test_rotate_telegram_proxy_switches_and_reports_lap_not_done():
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    original_base_url = bot.TELEGRAM_API_BASE_URL
    original_bot = bot.bot
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://primary.example.com", "https://fallback.example.com"]
    bot._telegram_proxy_idx = 0
    bot.TELEGRAM_API_BASE_URL = "https://primary.example.com"
    bot.bot = None  # без реального aiogram Bot — проверяем только URL-переключение
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is True  # ещё не замкнули круг — есть смысл пробовать сразу
        assert bot.TELEGRAM_API_BASE_URL == "https://fallback.example.com"
        assert bot._telegram_proxy_idx == 1
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx
        bot.TELEGRAM_API_BASE_URL = original_base_url
        bot.bot = original_bot


def test_rotate_telegram_proxy_reports_lap_done_after_full_cycle():
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    original_base_url = bot.TELEGRAM_API_BASE_URL
    original_bot = bot.bot
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://primary.example.com", "https://fallback.example.com"]
    bot._telegram_proxy_idx = 1  # уже на резервном — следующий поворот вернёт на primary (idx 0)
    bot.TELEGRAM_API_BASE_URL = "https://fallback.example.com"
    bot.bot = None
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is False  # круг замкнулся — пора паузу включать
        assert bot._telegram_proxy_idx == 0
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx
        bot.TELEGRAM_API_BASE_URL = original_base_url
        bot.bot = original_bot


def test_rotate_telegram_proxy_concurrent_rotations_advance_one_step_each():
    # Контракт AUD-E-004: индекс и URL мутируют под локом — две concurrent-
    # ротации дают два шага (0→1→2), а не два прыжка в одну точку.
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    original_base_url = bot.TELEGRAM_API_BASE_URL
    original_bot = bot.bot
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://one.example.com", "https://two.example.com", "https://three.example.com"]
    bot._telegram_proxy_idx = 0
    bot.TELEGRAM_API_BASE_URL = "https://one.example.com"
    bot.bot = None
    try:
        async def _two_rotations():
            return await asyncio.gather(bot._rotate_telegram_proxy(), bot._rotate_telegram_proxy())

        switched = asyncio.run(_two_rotations())
        assert switched == [True, True]
        assert bot._telegram_proxy_idx == 2
        assert bot.TELEGRAM_API_BASE_URL == "https://three.example.com"
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx
        bot.TELEGRAM_API_BASE_URL = original_base_url
        bot.bot = original_bot


def test_tikwm_proxy_candidates_single_empty_string_when_unset():
    original = bot.TIKWM_API_BASE_URL
    bot.TIKWM_API_BASE_URL = ""
    try:
        assert bot._tikwm_proxy_candidates() == [""]
    finally:
        bot.TIKWM_API_BASE_URL = original


def test_tikwm_proxy_candidates_primary_plus_fallbacks_deduped():
    original_primary = bot.TIKWM_API_BASE_URL
    original_fallbacks = bot._TIKWM_API_BASE_URL_FALLBACKS
    bot.TIKWM_API_BASE_URL = "https://primary.example.com/fetch/www.tikwm.com"
    bot._TIKWM_API_BASE_URL_FALLBACKS = [
        "https://primary.example.com/fetch/www.tikwm.com",  # дубль основного — должен быть отфильтрован
        "https://backup.example.com/fetch/www.tikwm.com",
    ]
    try:
        assert bot._tikwm_proxy_candidates() == [
            "https://primary.example.com/fetch/www.tikwm.com",
            "https://backup.example.com/fetch/www.tikwm.com",
        ]
    finally:
        bot.TIKWM_API_BASE_URL = original_primary
        bot._TIKWM_API_BASE_URL_FALLBACKS = original_fallbacks


def test_proxy_middleware_sends_secret_only_to_configured_proxy():
    _, _, seen = _run_proxy_middleware("https://proxy.example/fetch/api.telegram.org/bot123/sendMessage")
    assert seen["sent"].get("X-Lumen-Proxy-Secret") == "proxy-secret-abc"


def test_proxy_middleware_never_sends_secret_to_direct_or_unrelated_hosts():
    for url in (
        "https://api.telegram.org/bot123/sendMessage",
        "https://www.tikwm.com/api/?url=x",
        "https://cdn.example.com/file.jpg",
        "https://proxy.example/other-path",
        "http://proxy.example/fetch/api.telegram.org/bot123/sendMessage",
    ):
        _, _, seen = _run_proxy_middleware(url)
        assert "X-Lumen-Proxy-Secret" not in seen["sent"], url


def test_proxy_middleware_strips_stale_secret_and_requires_secret():
    _, _, seen = _run_proxy_middleware(
        "https://api.telegram.org/bot123/sendMessage",
        headers={"X-Lumen-Proxy-Secret": "stale"},
    )
    assert "X-Lumen-Proxy-Secret" not in seen["sent"]
    with pytest.raises(ValueError):
        lumen_telegram_transport.proxy_auth_middlewares(
            proxy_secret="", proxy_base_urls=("https://proxy.example/fetch/api.telegram.org",),
        )


def test_proxy_middleware_rejects_authenticated_redirects():
    # Редирект через прокси отключён в коде, но ветка была без теста: регрессия
    # осталась бы незамеченной. Секрет при этом уже снят с заголовков.
    from multidict import CIMultiDict
    from types import SimpleNamespace
    from yarl import URL

    (authenticate,) = lumen_telegram_transport.proxy_auth_middlewares(
        proxy_secret="proxy-secret-abc",
        proxy_base_urls=("https://proxy.example/fetch/api.telegram.org",),
    )

    class _Req:
        def __init__(self):
            self.url = URL("https://proxy.example/fetch/api.telegram.org/bot123/getMe")
            self.headers = CIMultiDict()

    class _Resp302:
        status = 302

        def __init__(self, req):
            self.request_info = SimpleNamespace(
                url=req.url, method="GET", headers=CIMultiDict(req.headers), real_url=req.url,
            )
            self.closed = False

        def close(self):
            self.closed = True

    async def _handler_302(request):
        resp = _Resp302(request)
        seen_resp["resp"] = resp
        return resp

    seen_resp = {}
    request = _Req()
    with pytest.raises(RuntimeError, match="redirects are disabled"):
        asyncio.run(authenticate(request, _handler_302))
    assert seen_resp["resp"].closed is True
    assert "X-Lumen-Proxy-Secret" not in request.headers


def _save_breaker_state():
    breaker = bot._tg_proxy_breaker
    return (breaker.consecutive_failures, breaker.down_until, breaker.down_logged_at)


def _restore_breaker_state(saved):
    breaker = bot._tg_proxy_breaker
    breaker.consecutive_failures, breaker.down_until, breaker.down_logged_at = saved


def test_tg_call_waits_retry_after_before_retry(monkeypatch):
    # Регрессия A8-02: флуд-контроль ждём по retry_after, а не 0.5с (ранний повтор продлевал бан).
    calls = []
    slept = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TelegramRetryAfter(method=MagicMock(), message="Flood", retry_after=7)
        return "ok"

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    saved = _save_breaker_state()
    try:
        assert asyncio.run(bot._tg_call(flaky, retries=1)) == "ok"
    finally:
        _restore_breaker_state(saved)
    assert len(calls) == 2
    assert slept == [7.0]


@pytest.mark.parametrize("exc_cls", [
    TelegramBadRequest,
    TelegramUnauthorizedError,
    TelegramForbiddenError,
    TelegramNotFound,
])
def test_tg_call_does_not_retry_client_errors(monkeypatch, exc_cls):
    # Регрессия A8-02: 4xx (кроме 429) повторять бессмысленно — один вызов, сразу None.
    calls = []

    async def always_bad():
        calls.append(1)
        raise exc_cls(method=MagicMock(), message="client error")

    saved = _save_breaker_state()
    try:
        assert asyncio.run(bot._tg_call(always_bad, retries=2)) is None
    finally:
        _restore_breaker_state(saved)
    assert len(calls) == 1


def test_tg_call_still_retries_transient_errors(monkeypatch):
    # Сторож к A8-02: запрет повторов 4xx не должен отменять ретраи сетевых сбоев.
    calls = []
    slept = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("boom")
        return "ok"

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    saved = _save_breaker_state()
    try:
        assert asyncio.run(bot._tg_call(flaky, retries=1)) == "ok"
    finally:
        _restore_breaker_state(saved)
    assert len(calls) == 2
    assert slept == [0.5]


def test_telegram_api_call_network_error_keeps_cause(monkeypatch):
    # Регрессия A8-03: цепочка для Sentry не рвётся (было from None).
    original = ConnectionError("dns boom")

    class _Post:
        async def __aenter__(self):
            raise original

        async def __aexit__(self, *args):
            return False

    class _Sess:
        def post(self, *args, **kwargs):
            return _Post()

    async def fake_session():
        return _Sess()

    monkeypatch.setattr(bot, "_get_telegram_session", fake_session)
    saved = _save_breaker_state()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(bot.telegram_api_call("sendMessage", {"chat_id": 1}))
    finally:
        _restore_breaker_state(saved)
    assert exc_info.value.__cause__ is original
    assert "sendMessage" in str(exc_info.value)


def test_telegram_api_call_ok_false_reports_description(monkeypatch):
    # Регрессия A8-03: в тексте код+описание, а не весь словарь ответа целиком.
    body = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, content_type=None):
            return body

    class _Sess:
        def post(self, *args, **kwargs):
            return _Resp()

    async def fake_session():
        return _Sess()

    monkeypatch.setattr(bot, "_get_telegram_session", fake_session)
    saved = _save_breaker_state()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(bot.telegram_api_call("sendMessage", {"chat_id": 1}))
    finally:
        _restore_breaker_state(saved)
    msg = str(exc_info.value)
    assert "Bad Request: chat not found" in msg
    assert "400" in msg
    assert "'ok'" not in msg

