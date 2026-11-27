"""test_bot_transport.py — Transport: proxy middleware, circuit breaker, proxy rotation, shutdown tasks.

Extracted from test_bot.py (P2 audit); shared fakes are in bot_test_helpers.py.
"""
import asyncio
import json
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


@pytest.mark.parametrize(("failures", "tripped"), [(2, False), (3, True)])
def test_circuit_breaker_trip_threshold(failures, tripped):
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    result = False
    for _ in range(failures):
        result = breaker.note_failure()
    assert result is tripped
    if tripped:
        breaker.trip()
    assert breaker.is_down(time.monotonic()) is tripped


def test_circuit_breaker_success_resets_consecutive_failures():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_failure()
    breaker.note_success()
    assert breaker.consecutive_failures == 0
    # Isolated subsequent failures must not trigger immediately — the counter really was reset.
    assert breaker.note_failure() is False


def test_circuit_breaker_garbage_event_count_never_resets():
    # Unlike consecutive_failures, the cumulative counter for /stats keeps growing
    # over the entire process lifetime and must not reset on success.
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
        # done_callback removes the task from the set itself, without manual cleanup.
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
    # Explicit failure instead of an implicit pass: a regression will produce a clear report, not a bare error.
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


@pytest.mark.parametrize(("start_idx", "expected_idx", "expected_switched", "expected_url"), [
    # The circuit is not closed yet, so an immediate retry makes sense.
    (0, 1, True, "https://fallback.example.com"),
    # Already on fallback — switching back would return to primary, the loop is closed, time for a pause.
    (1, 0, False, "https://primary.example.com"),
])
def test_rotate_telegram_proxy_advances_and_reports_lap(start_idx, expected_idx, expected_switched, expected_url):
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    original_base_url = bot.TELEGRAM_API_BASE_URL
    original_bot = bot.bot
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://primary.example.com", "https://fallback.example.com"]
    bot._telegram_proxy_idx = start_idx
    bot.TELEGRAM_API_BASE_URL = "https://primary.example.com" if start_idx == 0 else "https://fallback.example.com"
    bot.bot = None  # without a real aiogram Bot; we are checking only URL switching
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is expected_switched
        assert bot._telegram_proxy_idx == expected_idx
        assert bot.TELEGRAM_API_BASE_URL == expected_url
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx
        bot.TELEGRAM_API_BASE_URL = original_base_url
        bot.bot = original_bot


def test_rotate_telegram_proxy_concurrent_rotations_advance_one_step_each():
    # AUD-E-004 contract: the index and URL are mutated under the lock — two concurrent
    # from rotation take two steps (0→1→2), not two jumps to the same point.
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
        "https://primary.example.com/fetch/www.tikwm.com",  # a duplicate of the primary — must be filtered out
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
    assert seen["sent"].get("X-Lumen-Proxy-Secret") == "proxy-secret-abc-0123456789abcdef"


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
    with pytest.raises(ValueError):
        lumen_telegram_transport.proxy_auth_middlewares(
            proxy_secret="short",
            proxy_base_urls=("https://proxy.example/fetch/api.telegram.org",),
        )


def test_proxy_middleware_rejects_authenticated_redirects():
    # Redirects through the proxy are disabled in code, but the branch had no test: a regression
    # would have gone unnoticed. The secret has already been removed from the headers.
    from multidict import CIMultiDict
    from types import SimpleNamespace
    from yarl import URL

    (authenticate,) = lumen_telegram_transport.proxy_auth_middlewares(
        proxy_secret="proxy-secret-abc-0123456789abcdef",
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


def test_ipv4_session_request_timeout_matches_session_total():
    # aiogram applies self.timeout (default 60 seconds) to each request on top of the default
    # session: without an explicit value here, 60s would apply instead of total=30.
    import lumen_telegram_transport
    sess = lumen_telegram_transport.IPv4AiohttpSession(proxy_secret="x", proxy_base_urls=())
    try:
        assert sess.timeout == 30.0
    finally:
        asyncio.run(sess.close())


def test_get_http_session_reuses_cached_and_rebuilds_closed(monkeypatch):
    # Shared connection pool (TikTok slideshow, TTS, Whisper): the session is created once
    # and recreated only after close. A session per call would burn through container
    # sockets. Other tests replace _get_http_session entirely, so caching needs this test.
    from lumen_transport_calls import _get_http_session
    monkeypatch.setattr(bot, "_http_session", None)

    async def _run():
        s1 = await _get_http_session()
        s2 = await _get_http_session()
        assert s1 is s2
        await s1.close()
        s3 = await _get_http_session()
        assert s3 is not s1
        assert s3.closed is False
        await s3.close()

    asyncio.run(_run())


def test_get_http_session_connector_ipv4_limit_and_public_resolver(monkeypatch):
    # Dated decisions pinned for the shared connector pool: limit=40 lets a TikTok
    # slideshow fetch up to 35 slides through gather (24.07.2026); AF_INET matches the
    # container's lack of IPv6. _PublicOnlyResolver blocks non-public IPs on every
    # resolution, guarding all external requests in this session against DNS rebinding.
    # R7-17: pin the arguments of our TCPConnector call, not private fields
    # aiohttp (connector._resolver) is a private name that may disappear on upgrade,
    # and the test will turn red even with healthy prod.
    import aiohttp
    import socket
    from lumen_tiktok import _PublicOnlyResolver
    import lumen_transport_calls
    monkeypatch.setattr(bot, "_http_session", None)
    captured = {}
    real_connector = aiohttp.TCPConnector

    def spy_connector(*args, **kwargs):
        captured.update(kwargs)
        return real_connector(*args, **kwargs)

    monkeypatch.setattr(aiohttp, "TCPConnector", spy_connector)

    async def _run():
        s = await lumen_transport_calls._get_http_session()
        try:
            assert s is not None
        finally:
            await s.close()

    asyncio.run(_run())
    assert captured.get("family") == socket.AF_INET
    assert captured.get("limit") == 40
    assert captured.get("ttl_dns_cache") == 300
    assert isinstance(captured.get("resolver"), _PublicOnlyResolver)


def test_tg_call_does_not_retry_send_after_timeout(monkeypatch):
    # Resending after a timeout (outcome unknown) produced duplicate messages.
    from aiogram.exceptions import TelegramNetworkError
    calls = []

    async def flaky_send(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.TimeoutError("slow proxy")
        return "ok"

    async def slow_aiogram(**kwargs):
        raise TelegramNetworkError(method=MagicMock(), message="Request timeout error")

    saved = _save_breaker_state()
    try:
        # R4-03 (audit 26.11.2026): a timeout must not look like definite non-delivery.
        # The falsy _TG_UNKNOWN sentinel stops draw/TTS refunds and TikTok duplicate sends
        # when Telegram may already have accepted the media.
        res = asyncio.run(bot._tg_call(flaky_send, retries=1))
        assert bot._tg_outcome_unknown(res) and not res
        res = asyncio.run(bot._tg_call(slow_aiogram, retries=1))
        assert bot._tg_outcome_unknown(res) and not res
    finally:
        _restore_breaker_state(saved)
    assert len(calls) == 1


def test_tg_call_cancellation_preserves_unknown_method_outcome():
    # Cancellation after a mutation starts may arrive after Telegram accepted it. The task
    # must still cancel, while quota consumers can distinguish that send from a status edit.
    async def scenario():
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def send_voice(**kwargs):
            started.set()
            await blocker.wait()

        async def other_method(**kwargs):
            return None

        saved = _save_breaker_state()
        try:
            task = asyncio.create_task(bot._tg_call(send_voice))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert bot._tg_cancelled_unknown(caught.value, send_voice)
            assert not bot._tg_cancelled_unknown(caught.value, other_method)
        finally:
            _restore_breaker_state(saved)

    asyncio.run(scenario())


def test_tg_call_reset_after_send_is_unknown_without_retry():
    # R4-03: a disconnect after sending the request — outcome unknown;
    # retrying a mutation (send_photo) would create a duplicate, so make one call and use _TG_UNKNOWN.
    from aiogram.exceptions import TelegramNetworkError
    calls = []

    async def dropping_send(**kwargs):
        calls.append(1)
        raise TelegramNetworkError(method=MagicMock(), message="Connection reset by peer")

    saved = _save_breaker_state()
    try:
        res = asyncio.run(bot._tg_call(dropping_send, retries=2))
    finally:
        _restore_breaker_state(saved)
    assert bot._tg_outcome_unknown(res)
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [
    json.JSONDecodeError("Expecting value", "<html>", 0),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    RuntimeError("Failed to decode response body"),
])
def test_tg_call_post_response_decode_failure_is_unknown_without_retry(monkeypatch, exc):
    # The response may have been accepted: retrying creates a duplicate, while None
    # incorrectly lets the consumer refund the slot or enable the legacy retry.
    calls = []
    slept = []

    async def garbage(**kwargs):
        calls.append(kwargs)
        raise exc

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(bot, "_tg_proxy_breaker", bot._TelegramProxyCircuitBreaker(
        cooldown_sec=20.0, trip_threshold=5,
    ))
    res = asyncio.run(bot._tg_call(garbage, retries=2))
    assert bot._tg_outcome_unknown(res)
    assert len(calls) == 1
    assert slept == []


def test_tg_call_connect_failure_stays_definite_none():
    # "Cannot connect" — the request never left: this is definitely None (return the slot and
    # retries are safe), not unknown (R4-03, audit 26.11.2026).
    from aiogram.exceptions import TelegramNetworkError

    async def no_connect(**kwargs):
        raise TelegramNetworkError(method=MagicMock(), message="Cannot connect to host api.telegram.org")

    saved = _save_breaker_state()
    try:
        assert asyncio.run(bot._tg_call(no_connect, retries=0)) is None
    finally:
        _restore_breaker_state(saved)


def test_handle_proxy_failure_does_not_reenter_on_failing_notify(monkeypatch):
    # Reentrancy: the _notify_owner gate runs BEFORE trip() via _tg_call;
    # failure of the same proxy called _handle_proxy_failure again (the counter was already
    # threshold) — the chain repeated. A nested failure does not start the second branch.
    import json
    from types import SimpleNamespace

    entries = []
    real_handler = bot._handle_proxy_failure

    class _Brake(Exception):
        pass

    async def counting_handler(context):
        entries.append(context)
        if len(entries) > 2:
            raise _Brake()
        await real_handler(context)

    async def always_garbage(**kwargs):
        raise json.JSONDecodeError("Expecting value", "", 0)

    async def fake_rotate():
        return False

    async def fake_sleep(sec):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(bot, "_handle_proxy_failure", counting_handler)
    monkeypatch.setattr(bot, "_rotate_telegram_proxy", fake_rotate)
    monkeypatch.setattr(bot, "OWNER_ID", 999001)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_message=always_garbage))
    saved = _save_breaker_state()
    breaker = bot._tg_proxy_breaker
    breaker.consecutive_failures = breaker.trip_threshold - 1
    breaker.down_until = 0.0
    try:
        asyncio.run(bot._handle_proxy_failure("probe"))
        # One external call plus one nested call from the notification; the branch is closed beyond that.
        assert len(entries) == 2
    finally:
        _restore_breaker_state(saved)


def test_tg_call_waits_retry_after_before_retry(monkeypatch):
    # A8-02 regression: wait for flood control according to retry_after, not 0.5s (an early retry extended the ban).
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
    # Regression A8-02: retrying 4xx (except 429) is pointless: one call, immediately None.
    calls = []

    async def always_bad():
        calls.append(1)
        raise exc_cls(method=MagicMock(), message="client error")

    saved = _save_breaker_state()
    try:
        # Audit 16.11.2026: valid JSON from Telegram is a sign of a live proxy.
        # Without resetting the counter, the next single proxy delay would silence all chats.
        bot._tg_proxy_breaker.note_failure()
        bot._tg_proxy_breaker.note_failure()
        assert bot._tg_proxy_breaker.consecutive_failures == 2
        assert asyncio.run(bot._tg_call(always_bad, retries=2)) is None
        assert bot._tg_proxy_breaker.consecutive_failures == 0
    finally:
        _restore_breaker_state(saved)
    assert len(calls) == 1


def test_tg_call_still_retries_transient_errors(monkeypatch):
    # Guard for A8-02: banning 4xx retries must not cancel retries for network failures.
    # Timeouts are excluded: retrying after a timeout produced duplicates (outcome unknown).
    calls = []
    slept = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError("boom")
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
    # Regression A8-03: the chain for Sentry is not broken (previously from None).
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
    # Regression A8-03: the text contains code+description, not the entire response
    # dictionary.
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


def test_message_deadline_caps_stage_budget():
    # What it protects: the overall message ceiling (MESSAGE_TOTAL_BUDGET_SEC). Regression:
    # stage timeouts were summed without the remaining budget, and processing exceeded
    # budget (audit 27.10.2026).
    token = bot._MSG_DEADLINE.set(time.monotonic() + 5)
    try:
        assert bot._msg_remaining_sec() <= 5
        assert bot._deadline_within_message(40) <= time.monotonic() + 5.1
    finally:
        bot._MSG_DEADLINE.reset(token)
    assert bot._msg_remaining_sec() is None
    assert bot._deadline_within_message(40) > time.monotonic() + 39


def test_handle_message_sets_and_resets_message_deadline(monkeypatch):
    # What this protects: setting the ceiling on handler entry. Regression: the deadline
    # would remain in the context after processing and truncate the next update
    # (audit 27.10.2026).
    from types import SimpleNamespace
    seen = {}

    async def fake_handle(message):
        seen["remaining"] = bot._msg_remaining_sec()

    monkeypatch.setattr(bot, "_handle_message", fake_handle)
    asyncio.run(bot.handle_message(SimpleNamespace(chat=None, from_user=None)))
    assert seen["remaining"] is not None and seen["remaining"] > 0
    assert bot._msg_remaining_sec() is None


def test_proxy_middleware_sends_secret_to_the_read_route():
    # Audit 16.11.2026: /read was sent without a secret, the proxy returned 401, page reading
    # was entirely dead. The secret must reach /read on the same origin.
    _, _, seen = _run_proxy_middleware(
        "https://proxy.example/read?url=https%3A%2F%2Fexample.com%2F",
    )
    assert seen["sent"].get("X-Lumen-Proxy-Secret") == "proxy-secret-abc-0123456789abcdef"
    _, _, seen = _run_proxy_middleware("https://other.example/read?url=https%3A%2F%2Fexample.com%2F")
    assert "X-Lumen-Proxy-Secret" not in seen["sent"]
    _, _, seen = _run_proxy_middleware("https://proxy.example/read/extra")
    assert "X-Lumen-Proxy-Secret" not in seen["sent"]
