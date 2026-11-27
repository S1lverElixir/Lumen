"""conftest.py — environment setup BEFORE tests import bot.py.

At module level, bot.py reads environment variables (BOT_TOKEN, GEMINI_API_KEY, etc.) and writes a log file at BOT_LOG_PATH (default /app/bot.log). Safe stubs are set here before pytest collects and imports test modules, so tests can run anywhere, not just inside an HF Spaces Docker container where /app exists and is writable.

This needs no network and creates no real Bot()/genai.Client(); those are created only inside main(), which tests never call.
"""
import asyncio
import errno
import ipaddress
import os
import socket
import tempfile

# Test configuration is authoritative, never inherited from the owner's shell or .env.
os.environ["BOT_LOG_PATH"] = os.path.join(tempfile.gettempdir(), "lumen_test_bot.log")
os.environ["BOT_TOKEN"] = "test-token-not-real"
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
os.environ["GROUP_WAKE_ENABLED"] = "1"
os.environ["GROUP_WAKE_WORDS"] = "люмен,lumen"
# The Stop control is enabled separately in its own tests; the shared suite must not
# receive extra control messages from the owner variable.
os.environ["STOP_BUTTON_ENABLED"] = "0"
# Provider keys — empty only: otherwise bot.py below would pick up .env
# developer, and the test reaches a live API (network, someone else's quota, instability).
# MUNITO_ENABLED/MUNITO_KEEP_FALLBACKS are disabled along with the key: experimental mode
# read from the environment when the router is imported and must not depend on the local .env.
# Cloudflare keys must be in the list: lumen_images reads them at import, and a local
# .env could otherwise send tests to live Workers AI (network, someone else's quota, instability).
for _provider_key in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "USE_UPSTASH",
                      "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_KEY", "SENTRY_DSN",
                      "MUNITO_API_KEY", "MUNITO_ENABLED", "MUNITO_KEEP_FALLBACKS", "MUNITO_REASONING_EFFORT",
                      "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                      # Agent-search keys: with a local .env, the test would call a paid API.
                      "TAVILY_API_KEY", "BRAVE_API_KEY", "EXA_API_KEY", "SEARCH_API_PROVIDER"):
    os.environ[_provider_key] = ""

import copy
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot
# Production always shows Stop; the shared suite must not get extra
# control messages; separate tests enable them.
bot.STOP_BUTTON_ENABLED = False
import lumen_limits
import lumen_router_config


@pytest.fixture(autouse=True)
def _bot_global_state_guard():
    """Snapshot the most frequently manually saved bot.py module globals before
    each test and restore them afterward (technical debt audit, August 2026).
    Model quarantine (lumen_router_config._QUARANTINE) is also cleared: empty
    test answers legitimately increment the counter; without a reset, the same fake
    ID would enter quarantine after three tests and start filtering routes.

    Dozens of tests in test_bot_*.py manually saved/restored
    bot.chat_state/bot.GLOBAL_QUOTA/bot.client/bot.bot in try/finally — workable, but
    repetitive boilerplate and a potential source of subtle leakage between tests
    as the suite grows (a forgotten finally silently \"poisons\" subsequent tests).
    This is an ADDITIONAL safety net, not a replacement — existing tests with their
    own manual cleanup continue to work exactly as before (double restoration is
    harmless); new tests may rely only on this fixture and omit their own
    try/finally for these four globals.

    Tests restore other module state (TELEGRAM_API_BASE_URL, etc.)
    themselves; the snapshot is extended only for confirmed leaks."""
    import bot as _bot_module
    import lumen_chat_state as _chat_state_module
    import lumen_commands as _commands_module
    import lumen_typing_pace as _pace_module
    chat_state_snapshot = copy.deepcopy(_bot_module.chat_state)
    quota_snapshot = copy.deepcopy(_bot_module.GLOBAL_QUOTA)
    migration_pending_snapshot = set(_chat_state_module._legacy_migration_pending_ids)
    deleting_snapshot = set(_chat_state_module._deleting_chat_ids)
    recreated_snapshot = set(_chat_state_module._recreated_chat_ids)
    index_extra_snapshot = set(_chat_state_module._index_extra_ids)
    unindexed_snapshot = set(_chat_state_module._unindexed_chat_ids)
    quarantine_snapshot = copy.deepcopy(lumen_router_config._QUARANTINE)
    dirty_snapshot = set(_chat_state_module._dirty_chat_ids)
    index_dirty_snapshot = _chat_state_module._index_dirty
    index_generation_snapshot = _chat_state_module._index_generation
    client_snapshot = _bot_module.client
    bot_snapshot = _bot_module.bot
    # In-flight generation and the proxy switch live outside the chat snapshot: the remaining uid
    # blocked the next test with "already in flight," while tripped down_until
    # turned photo sending into None (review 10.11.2026 mutant run).
    inflight_snapshot = set(_commands_module._draw_inflight)
    speed_ema_snapshot = dict(_pace_module._speed_ema)
    breaker_snapshot = dict(_bot_module._tg_proxy_breaker.__dict__)
    # Confirmed R7-12 leaks: stop flags and lock counters are visible to a third
    # to the test, chat locks are bound to a closed loop, webhook deduplication swallows
    # update_id from another file, and a forgotten aiohttp session emits Unclosed
    # client session for the entire suite (review 17.11.2026).
    import lumen_admin as _admin_module
    stream_stops_snapshot = dict(_bot_module._STREAM_STOPS)
    lock_waiters_snapshot = dict(_bot_module._chat_lock_waiters)
    bg_waiters_snapshot = dict(_bot_module._chat_lock_background_waiters)
    chat_locks_snapshot = dict(_bot_module._chat_locks)
    seen_ids_snapshot = list(_admin_module._seen_update_ids)
    seen_set_snapshot = set(_admin_module._seen_update_id_set)
    http_session_snapshot = _bot_module._http_session
    yield
    _bot_module.chat_state.clear()
    _bot_module.chat_state.update(chat_state_snapshot)
    _bot_module.GLOBAL_QUOTA.clear()
    _bot_module.GLOBAL_QUOTA.update(quota_snapshot)
    _chat_state_module._legacy_migration_pending_ids.clear()
    _chat_state_module._legacy_migration_pending_ids.update(migration_pending_snapshot)
    _chat_state_module._deleting_chat_ids.clear()
    _chat_state_module._deleting_chat_ids.update(deleting_snapshot)
    _chat_state_module._recreated_chat_ids.clear()
    _chat_state_module._recreated_chat_ids.update(recreated_snapshot)
    _chat_state_module._index_extra_ids.clear()
    _chat_state_module._index_extra_ids.update(index_extra_snapshot)
    _chat_state_module._unindexed_chat_ids.clear()
    _chat_state_module._unindexed_chat_ids.update(unindexed_snapshot)
    lumen_router_config._QUARANTINE.clear()
    lumen_router_config._QUARANTINE.update(quarantine_snapshot)
    _chat_state_module._dirty_chat_ids.clear()
    _chat_state_module._dirty_chat_ids.update(dirty_snapshot)
    _chat_state_module._index_dirty = index_dirty_snapshot
    _chat_state_module._index_generation = index_generation_snapshot
    _bot_module.client = client_snapshot
    _bot_module.bot = bot_snapshot
    _commands_module._draw_inflight.clear()
    _commands_module._draw_inflight.update(inflight_snapshot)
    _pace_module._speed_ema.clear()
    _pace_module._speed_ema.update(speed_ema_snapshot)
    _bot_module._tg_proxy_breaker.__dict__.clear()
    _bot_module._tg_proxy_breaker.__dict__.update(breaker_snapshot)
    _bot_module._STREAM_STOPS.clear()
    _bot_module._STREAM_STOPS.update(stream_stops_snapshot)
    _bot_module._chat_lock_waiters.clear()
    _bot_module._chat_lock_waiters.update(lock_waiters_snapshot)
    _bot_module._chat_lock_background_waiters.clear()
    _bot_module._chat_lock_background_waiters.update(bg_waiters_snapshot)
    # Locks created by a test on a closed asyncio.run loop cannot survive the test:
    # keep only snapshot entries; discard the others (including locked-state leaks).
    _bot_module._chat_locks.clear()
    _bot_module._chat_locks.update(chat_locks_snapshot)
    _admin_module._seen_update_ids.clear()
    _admin_module._seen_update_ids.extend(seen_ids_snapshot)
    _admin_module._seen_update_id_set.clear()
    _admin_module._seen_update_id_set.update(seen_set_snapshot)
    if _bot_module._http_session is not http_session_snapshot:
        leaked = _bot_module._http_session
        try:
            if leaked is not None:
                # aiogram AiohttpSession and aiohttp>=3.14 ClientSession.close() are coroutines;
                # in synchronous teardown, close the nested session in a fresh loop (the source
                # Unclosed client session; verified on aiohttp 3.14.5).
                inner = leaked if hasattr(leaked, "closed") else getattr(leaked, "_session", None)
                if inner is not None and not inner.closed:
                    closed = inner.close()
                    if hasattr(closed, "__await__"):
                        asyncio.run(closed)
        finally:
            # Even if closing raises, return the reference to the snapshot; otherwise a leaked
            # the session becomes the "baseline" for all subsequent guard snapshots (review 17.11.2026).
            _bot_module._http_session = http_session_snapshot


@pytest.fixture(autouse=True)
def _offline_network_guard(monkeypatch):
    """Reject external DNS and connects while keeping explicit test seams intact."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sock_connect = asyncio.BaseEventLoop.sock_connect
    proactor_loop = getattr(asyncio, "ProactorEventLoop", None)
    real_proactor_sock_connect = (
        proactor_loop.sock_connect if proactor_loop is not None else None
    )

    def _blocked_dns(host, port=None, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "External DNS is disabled in tests")

    def _check_connect(sock, address):
        family = sock.family
        if family == getattr(socket, "AF_UNIX", None):
            return
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise OSError("External network access is disabled in tests")
        host = address[0] if isinstance(address, tuple) and address else None
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise OSError("External network access is disabled in tests")

    def _connect(sock, address):
        _check_connect(sock, address)
        return real_connect(sock, address)

    def _connect_ex(sock, address):
        try:
            _check_connect(sock, address)
        except OSError as exc:
            return exc.errno or errno.EACCES
        return real_connect_ex(sock, address)

    def _sock_connect(loop, sock, address):
        _check_connect(sock, address)
        return real_sock_connect(loop, sock, address)

    def _proactor_sock_connect(loop, sock, address):
        _check_connect(sock, address)
        return real_proactor_sock_connect(loop, sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", _blocked_dns)
    monkeypatch.setattr(socket, "gethostbyname", _blocked_dns)
    monkeypatch.setattr(socket, "gethostbyname_ex", _blocked_dns)
    monkeypatch.setattr(socket, "gethostbyaddr", _blocked_dns)
    monkeypatch.setattr(socket, "getnameinfo", _blocked_dns)
    monkeypatch.setattr(asyncio.BaseEventLoop, "sock_connect", _sock_connect)
    if proactor_loop is not None:
        monkeypatch.setattr(proactor_loop, "sock_connect", _proactor_sock_connect)
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)


@pytest.fixture(autouse=True)
def _instant_typing_pace():
    """Streaming now artificially paces out the remainder of text already received but not
    yet displayed (see STREAM_TYPING_*/_run_streaming_reply in bot.py and
    lumen_typing_pace.py): several await bot._typing_sleep(...) calls between message
    edits make it look like live typing. Tests do not need real waits, which would
    noticeably slow the entire suite for no benefit: bot._typing_sleep exists as a
    patch point (the same pattern as bot._get_http_session/bot._openrouter_stream_pieces
    and others elsewhere). Here it is unconditionally patched to a no-op for EVERY
    test, not just those that explicitly test streaming.
    """
    import bot as _bot_module
    original_typing_sleep = _bot_module._typing_sleep

    async def _instant_sleep(_seconds: float) -> None:
        return None

    _bot_module._typing_sleep = _instant_sleep
    yield
    _bot_module._typing_sleep = original_typing_sleep


@pytest.fixture(autouse=True)
def _instant_tikwm_throttle():
    """The same technique and reason as _instant_typing_pace above, but for throttling TikWM API requests (see _tikwm_throttle/_fetch_tikwm_media_data in lumen_tiktok.py, added while debugging TikWM 403 errors on August 11, 2026). _tikwm_last_request_ts persists for the whole process; without a reset between tests, accumulated state would make EVERY subsequent TikTok test actually wait up to _TIKWM_MIN_INTERVAL_SEC seconds to no benefit."""
    import lumen_tiktok as _tiktok_module
    original_sleep = _tiktok_module._sleep
    original_last_ts = _tiktok_module._tikwm_last_request_ts

    async def _instant_sleep(_seconds: float) -> None:
        return None

    _tiktok_module._sleep = _instant_sleep
    _tiktok_module._tikwm_last_request_ts = None
    yield
    _tiktok_module._sleep = original_sleep
    _tiktok_module._tikwm_last_request_ts = original_last_ts


@pytest.fixture
def rate_guard_setup(monkeypatch):
    def _setup():
        monkeypatch.setattr(lumen_limits, "user_rate_limits", {})
        monkeypatch.setattr(lumen_limits, "RATE_LIMIT_MAX_REQUESTS", 2)
        monkeypatch.setattr(bot, "get_state", lambda chat_id: {})
        monkeypatch.setattr(bot, "is_guest_message", lambda message: False)
        monkeypatch.setattr(bot, "message_mentions_bot", lambda message: False)
        monkeypatch.setattr(bot, "_safe_reply", AsyncMock())
        monkeypatch.setattr(bot, "_tg_call", AsyncMock())
        monkeypatch.setattr(bot, "inline_draw", AsyncMock())
        monkeypatch.setattr(bot, "inline_tts", AsyncMock())
        return SimpleNamespace(
            text="", caption=None, chat=SimpleNamespace(id=123, type=bot.ChatType.PRIVATE),
            from_user=SimpleNamespace(id=456), reply=AsyncMock(), reply_to_message=None,
        )
    return _setup


@pytest.fixture(autouse=True)
def _fresh_tiktok_semaphores():
    """Recreate TikTok's semaphores per test because reused semaphores remain bound to the first event loop and fail on the next asyncio.run (review 17.11.2026)."""
    import asyncio as _asyncio
    original_probe = bot._tiktok_probe_semaphore
    original_download = bot._tiktok_slide_download_semaphore
    bot._tiktok_probe_semaphore = _asyncio.Semaphore(bot.TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY)
    bot._tiktok_slide_download_semaphore = _asyncio.Semaphore(bot.TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY)
    yield
    bot._tiktok_probe_semaphore = original_probe
    bot._tiktok_slide_download_semaphore = original_download
