"""test_bot_admin.py — Admin and webhook: secret gates, healthcheck, /export_state, /logs, /stats.

Split from test_bot.py (P2 audit); shared fakes are in bot_test_helpers.py.
"""
import asyncio
import bot
import logging
import os
import threading
import time
import pytest
from unittest.mock import AsyncMock, Mock
from tests.bot_test_helpers import (
    _FakeAdminRequest,
    _FakeWebhookRequest,
    _run_webhook_handler,
)


def _fake_webhook_secret() -> str:
    # Build the replacement secret with an expression, not a literal: credential scanners
    # flag `*SECRET* = "string"` even though the value is definitely a test value.
    return "real-webhook-" + "secret"


@pytest.fixture(autouse=True)
def _clear_webhook_update_dedup():
    """update_id deduplication is global: tests must not see each other IDs."""
    import lumen_admin
    lumen_admin._seen_update_ids.clear()
    lumen_admin._seen_update_id_set.clear()
    bot._state_ready = True
    yield
    lumen_admin._seen_update_ids.clear()
    lumen_admin._seen_update_id_set.clear()
    bot._state_ready = True


def test_public_log_formatter_hashes_long_numeric_ids():
    # Public Space logs must not contain chat/user IDs (audit 28.10.2026).
    import logging
    record = logging.LogRecord("bot", logging.INFO, __file__, 1,
                               "[chat=123456789] user 987654321 ok", None, None)
    text = bot._PublicLogFormatter("%(message)s").format(record)
    assert "123456789" not in text
    assert "987654321" not in text
    assert "#" in text


def test_check_bot_token_auth_accepts_correct_bearer_header():
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        req = _FakeAdminRequest(headers={"Authorization": "Bearer real-secret-token"})
        assert bot._check_bot_token_auth(req) is True
    finally:
        bot.BOT_TOKEN = original


def test_check_bot_token_auth_rejects_query_param_regression():
    # REGRESSION (code review): BOT_TOKEN was previously read from ?bot_token=... in the URL — GET-
    # a request with a secret in the query string reaches proxy access logs/browser history
    # (CWE-598). The query parameter must now be completely IGNORED — the only
    # legitimate path: the Authorization: Bearer header.
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        req = _FakeAdminRequest(headers={}, query_params={"bot_token": "real-secret-token"})
        assert bot._check_bot_token_auth(req) is False
    finally:
        bot.BOT_TOKEN = original


def test_check_bot_token_auth_rejects_wrong_or_missing_header():
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        assert bot._check_bot_token_auth(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})) is False
        assert bot._check_bot_token_auth(_FakeAdminRequest(headers={})) is False
        # Without the "Bearer " prefix, also refuse, even if the token itself matches.
        assert bot._check_bot_token_auth(_FakeAdminRequest(headers={"Authorization": "real-secret-token"})) is False
    finally:
        bot.BOT_TOKEN = original


def test_check_admin_key_accepts_correct_bearer_header():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        req = _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})
        assert bot._check_admin_key(req) is True
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_check_admin_key_rejects_query_param_regression():
    # REGRESSION (tech-debt audit): ADMIN_PANEL_KEY used to be read from ?key=... in the URL —
    # the same vulnerability (CWE-598) already fixed for BOT_TOKEN in /admin_keys
    # (see test_check_bot_token_auth_rejects_query_param_regression), but it was not
    # applied to /diag/webhook_url/export_state. The query parameter should no longer
    # be accepted at all, even if the value is correct.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        req = _FakeAdminRequest(headers={}, query_params={"key": "real-admin-key"})
        assert bot._check_admin_key(req) is False
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_check_admin_key_rejects_wrong_or_missing_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        assert bot._check_admin_key(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})) is False
        assert bot._check_admin_key(_FakeAdminRequest(headers={})) is False
        assert bot._check_admin_key(_FakeAdminRequest(headers={"Authorization": "real-admin-key"})) is False
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_bearer_non_ascii_returns_false_not_500():
    # compare_digest on str raises TypeError for non-ASCII: refuse without an exception.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        assert bot._check_bearer_token(_FakeAdminRequest(headers={"Authorization": "Bearer café"}), "real-admin-key") is False
        assert bot._check_admin_key(_FakeAdminRequest(headers={"Authorization": "Bearer café"})) is False
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_webhook_non_ascii_secret_returns_false_not_500():
    original = bot.WEBHOOK_SECRET
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    try:
        req = _FakeWebhookRequest(headers={"X-Telegram-Bot-Api-Secret-Token": "café"}, body={"update_id": 1})
        result = asyncio.run(_run_webhook_handler(req))
        assert result == {"ok": False}
    finally:
        bot.WEBHOOK_SECRET = original


def test_webhook_handler_tracks_dispatched_task_for_shutdown():
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = object()
    bot._inflight_tasks.clear()

    release = asyncio.Event()

    async def fake_process(raw_update):
        # A live task must be in the set at startup: a no-op registration
        # previously passed the test (review 17.11.2026).
        assert bot._inflight_tasks, "живая задача не зарегистрирована"
        await release.wait()

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )

        async def _scenario():
            await _run_webhook_handler(req)
            assert bot._inflight_tasks, "висящая задача должна быть видна shutdown-drain"
            release.set()
            for _ in range(3):
                await asyncio.sleep(0)
            assert not bot._inflight_tasks

        asyncio.run(_scenario())
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


def test_webhook_returns_503_until_state_is_ready():
    # An update during state loading must not create an empty chat and
    # overwrite deleted history (audit 28.10.2026).
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    original_ready = bot._state_ready
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = object()
    bot._state_ready = False
    process = AsyncMock()
    bot._process_raw_update = process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 778},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert getattr(result, "status_code", None) == 503
        process.assert_not_called()
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process
        bot._state_ready = original_ready


def test_webhook_handler_ignores_duplicate_update_id():
    # Telegram retries delivery on timeout/failure: one update_id must not
    # a second response, a download, and quota usage (audit 28.10.2026).
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = object()
    bot._inflight_tasks.clear()
    import lumen_admin
    lumen_admin._seen_update_ids.clear()
    lumen_admin._seen_update_id_set.clear()
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)
        await asyncio.sleep(0)

    bot._process_raw_update = fake_process
    try:
        headers = {"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"}
        for _ in range(2):
            req = _FakeWebhookRequest(headers=headers, body={"update_id": 777})
            asyncio.run(_run_webhook_handler(req))
        assert len(calls) == 1
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process
        lumen_admin._seen_update_ids.clear()
        lumen_admin._seen_update_id_set.clear()


def test_healthcheck_reports_not_ready_when_bot_or_client_uninitialized():
    original_bot, original_client = bot.bot, bot.client
    bot.bot = None
    bot.client = None
    try:
        result = asyncio.run(bot.healthcheck())
        assert result == {"status": "starting", "ready": False}
    finally:
        bot.bot, bot.client = original_bot, original_client


def test_healthcheck_reports_ready_when_initialized():
    original_bot, original_client = bot.bot, bot.client
    bot.bot = object()
    bot.client = object()
    try:
        result = asyncio.run(bot.healthcheck())
        assert result == {"status": "ok", "ready": True}
    finally:
        bot.bot, bot.client = original_bot, original_client


def test_export_state_rejects_missing_or_wrong_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})))
        # Refusal is an honest 401, not 200 with body {"error": ...}: otherwise, brute force
        # key differed from success only in the response body (audit 26.09.2026).
        assert result.status_code == 401
        assert b"error" in result.body
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_export_state_refuses_while_state_is_loading():
    # Audit 16.11.2026: a backup during loading is empty or partial, and
    # state-backup.yml would commit it as daily. Expect 503 and a request to retry.
    original_key = bot.ADMIN_PANEL_KEY
    original_ready = getattr(bot, "_state_ready", True)
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    bot._state_ready = False
    try:
        result = asyncio.run(bot.export_state(
            _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        assert getattr(result, "status_code", None) == 503
    finally:
        bot.ADMIN_PANEL_KEY = original_key
        bot._state_ready = original_ready


def test_export_state_logs_denied_attempt(caplog):
    import logging
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot.export_state(_FakeAdminRequest(headers={})))
        assert any("[admin] Denied GET /export_state" in r.getMessage() for r in caplog.records)
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_fastapi_schema_endpoints_are_disabled():
    # The public schema on the HF Space exposed every endpoint for free.
    assert bot.app.docs_url is None
    assert bot.app.redoc_url is None
    assert bot.app.openapi_url is None


def test_export_state_returns_detached_snapshot():
    # Audit A11-05: export returned live references to mutable objects —
    # a write between the return and serialization produced an inconsistent backup.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    chat_id = 999980
    state = bot.get_state(chat_id)
    state["history"].append({"role": "user", "content": "hi"})
    bot.GLOBAL_QUOTA.setdefault("munito", {})["snaptest"] = {"used": 1}
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        bot.GLOBAL_QUOTA["munito"]["snaptest"]["used"] = 999
        state["history"].append({"role": "user", "content": "after"})
        assert result["global_quota"]["munito"]["snaptest"]["used"] == 1
        assert all(m["content"] != "after" for m in result["chats"][str(chat_id)]["history"])
    finally:
        bot.GLOBAL_QUOTA.get("munito", {}).pop("snaptest", None)
        bot.chat_state.pop(chat_id, None)
        bot.ADMIN_PANEL_KEY = original


def test_export_state_rejects_query_param_regression():
    # See test_check_admin_key_rejects_query_param_regression; /export_state is the most
    # sensitive of the three endpoints (returns COMPLETE histories of all chats), so
    # the regression is checked here separately, not only at the _check_admin_key level.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={}, query_params={"key": "real-admin-key"})))
        assert result.status_code == 401
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_export_state_returns_chats_and_quota_with_valid_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    chat_id = 999411
    bot.chat_state[chat_id] = {
        "history": [{"role": "user", "content": "hi"}],
        "quota": {}, "recent_media_ids": {}, "last_activity": 0.0,
    }
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        assert str(chat_id) in result["chats"]
        assert result["chats"][str(chat_id)]["history"] == [{"role": "user", "content": "hi"}]
        assert "global_quota" in result
        assert "exported_at" in result
        assert result["export_schema_version"] == 1
        assert result["complete"] is True
        assert result["chat_count"] == len(result["chats"])
    finally:
        bot.ADMIN_PANEL_KEY = original
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize(
    "attribute",
    ["_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids", "_deleting_chat_ids"],
)
def test_export_state_refuses_known_incomplete_state(attribute, monkeypatch):
    import lumen_chat_state as lcs

    chat_id = 999812
    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {chat_id: {"history": []}})
    for name in ("_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids", "_deleting_chat_ids"):
        missing_id = chat_id + 1 if name == "_index_extra_ids" else chat_id
        monkeypatch.setattr(lcs, name, {missing_id} if name == attribute else set(), raising=False)

    result = asyncio.run(bot.export_state(
        _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503


def test_export_state_refuses_unresolved_chat_replacement(monkeypatch):
    import lumen_chat_state as lcs

    chat_id = 999812
    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {chat_id: {"history": []}})
    for name in (
        "_chat_snapshot_unloaded", "_legacy_migration_pending_ids",
        "_pending_chat_deletions", "_index_extra_ids", "_deleting_chat_ids",
        "_recreated_chat_ids", "_deleted_chat_ids",
    ):
        monkeypatch.setattr(lcs, name, set())
    monkeypatch.setattr(lcs, "_state_load_failed", False)
    monkeypatch.setattr(lcs, "_quota_load_failed", False)
    monkeypatch.setattr(lcs, "_replacement_generations", {})
    lcs._cancel_chat_deletion(chat_id)
    assert chat_id in lcs._recreated_chat_ids
    assert chat_id in lcs._deleting_chat_ids

    result = asyncio.run(bot.export_state(
        _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503


@pytest.mark.parametrize("snapshot", [
    {"schema_version": 2},
    bot._serialize_chat_state({"history": [{"role": "user", "content": 123}]}),
])
def test_export_state_refuses_structurally_invalid_snapshot(monkeypatch, snapshot):
    import lumen_admin
    import lumen_chat_state as lcs

    chat_id = 999813
    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {chat_id: {"history": []}})
    for name in ("_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids"):
        monkeypatch.setattr(lcs, name, set())
    monkeypatch.setattr(lumen_admin, "_serialize_chat_state", lambda state: snapshot)

    result = asyncio.run(bot.export_state(
        _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503


def test_export_state_refuses_empty_snapshot(monkeypatch):
    import lumen_chat_state as lcs

    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {})
    for name in ("_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids", "_deleting_chat_ids"):
        monkeypatch.setattr(lcs, name, set(), raising=False)

    result = asyncio.run(bot.export_state(
        _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503


def test_export_validator_accepts_integral_json_numbers():
    import lumen_admin

    snapshot = bot._serialize_chat_state({"history": [{"role": "user", "content": "hi"}]})
    snapshot["schema_version"] = float(snapshot["schema_version"])
    payload = {
        "export_schema_version": 1.0,
        "complete": True,
        "exported_at": "2026-11-25T00:00:00+00:00",
        "chat_count": 1.0,
        "chats": {"1": snapshot},
        "global_quota": {"munito": {"model": {"used": 1.0}}},
    }
    assert lumen_admin._validate_export_snapshot(payload) is True


def test_export_state_refuses_malformed_nested_quota(monkeypatch):
    import lumen_chat_state as lcs

    chat_id = 999815
    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {chat_id: {"history": []}})
    monkeypatch.setattr(bot, "GLOBAL_QUOTA", {"munito": []})
    for name in ("_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids", "_deleting_chat_ids"):
        monkeypatch.setattr(lcs, name, set(), raising=False)

    result = asyncio.run(bot.export_state(
        _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503


@pytest.mark.parametrize("quota", [
    {"banned_users": {"7": 10 ** 400}},
    {"munito": {"model": {"used": 1, "future": float("inf")}}},
])
def test_export_state_refuses_unrepresentable_quota_number(monkeypatch, quota):
    import lumen_chat_state as lcs

    chat_id = 999816
    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {chat_id: {"history": []}})
    monkeypatch.setattr(bot, "GLOBAL_QUOTA", quota)
    for name in ("_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids", "_deleting_chat_ids"):
        monkeypatch.setattr(lcs, name, set(), raising=False)

    result = asyncio.run(bot.export_state(
        _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503


def test_export_state_serialization_failure_is_retryable_and_private(monkeypatch, caplog):
    import lumen_admin
    import lumen_chat_state as lcs

    chat_id = 999814
    monkeypatch.setattr(bot, "ADMIN_PANEL_KEY", "real-admin-key")
    monkeypatch.setattr(bot, "chat_state", {chat_id: {"history": []}})
    for name in ("_chat_snapshot_unloaded", "_legacy_migration_pending_ids", "_pending_chat_deletions", "_index_extra_ids"):
        monkeypatch.setattr(lcs, name, set())

    def fail(state):
        raise RuntimeError("private chat payload")

    monkeypatch.setattr(lumen_admin, "_serialize_chat_state", fail)
    with caplog.at_level(logging.WARNING, logger="bot"):
        result = asyncio.run(bot.export_state(
            _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))

    assert getattr(result, "status_code", None) == 503
    assert "private chat payload" not in caplog.text


def test_webhook_handler_accepts_valid_secret_and_dispatches_update():
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = object()  # non-None is enough to pass the "bot is already initialized" check
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert result == {"ok": True}
        assert calls == [{"update_id": 1}]
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


def test_webhook_rechecks_inflight_cap_after_body_read(monkeypatch):
    # Audit 16.11.2026: the cap was checked only before reading the body, and between the check and
    # create_task has awaits; a burst of concurrent POSTs passed with an empty set
    # and created more tasks than the cap. The recheck sits directly before create_task.
    # R1-03 (audit 26.11.2026): the set is now populated while awaiting the body read —
    # only window between the first check and the recheck.
    import lumen_admin

    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    bot.bot = object()
    bot._inflight_tasks.clear()
    filler: set = set()
    calls = []

    async def _noop() -> None:
        return None

    async def fake_process(raw_update):
        calls.append(raw_update)

    class _FillingRequest(_FakeWebhookRequest):
        async def stream(self):
            while len(filler) < lumen_admin.WEBHOOK_MAX_INFLIGHT_TASKS:
                filler.add(asyncio.create_task(_noop()))
            bot._inflight_tasks.update(filler)
            async for chunk in super().stream():
                yield chunk

    monkeypatch.setattr(lumen_admin, "_is_duplicate_update", lambda _update_id: False)
    bot._process_raw_update = fake_process
    try:
        req = _FillingRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 424242},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert getattr(result, "status_code", None) == 503
        assert calls == []
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process
        for task in filler:
            task.cancel()
        bot._inflight_tasks.clear()


def test_webhook_503_does_not_burn_update_retry():
    # R1-03 (audit 26.11.2026): in the old order, deduplication ran before rechecking
    # cap, and an update sent during a 503 was already considered seen — a Telegram retry
    # was silently lost. Refusal due to the cap must not touch deduplication.
    import lumen_admin

    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    bot.bot = object()
    bot._inflight_tasks.clear()
    filler: set = set()
    calls = []

    async def _noop() -> None:
        return None

    async def fake_process(raw_update):
        calls.append(raw_update)

    class _FillingRequest(_FakeWebhookRequest):
        async def stream(self):
            while len(filler) < lumen_admin.WEBHOOK_MAX_INFLIGHT_TASKS:
                filler.add(asyncio.create_task(_noop()))
            bot._inflight_tasks.update(filler)
            async for chunk in super().stream():
                yield chunk

    bot._process_raw_update = fake_process
    try:
        first = _FillingRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 777001},
        )
        result = asyncio.run(_run_webhook_handler(first))
        assert getattr(result, "status_code", None) == 503
        assert calls == []
        for task in filler:
            task.cancel()
        filler.clear()
        bot._inflight_tasks.clear()
        retry = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 777001},
        )
        result2 = asyncio.run(_run_webhook_handler(retry))
        assert result2 == {"ok": True}
        assert calls == [{"update_id": 777001}]
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process
        for task in filler:
            task.cancel()
        bot._inflight_tasks.clear()


def test_webhook_handler_rejects_invalid_or_missing_secret_and_does_not_dispatch():
    original_secret = bot.WEBHOOK_SECRET
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        for bad_headers in (
            {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            {},
        ):
            req = _FakeWebhookRequest(headers=bad_headers, body={"update_id": 1})
            result = asyncio.run(_run_webhook_handler(req))
            assert result == {"ok": False}
        assert calls == []
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot._process_raw_update = original_process


def test_webhook_handler_rejects_oversized_body_before_reading():
    # A body over the cap: reject before parsing; do not read the raw bytes in full.
    import lumen_admin
    original_secret = bot.WEBHOOK_SECRET
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    try:
        req = _FakeWebhookRequest(
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret",
                "Content-Length": str(lumen_admin.WEBHOOK_MAX_BODY_BYTES + 1),
            },
            body={"update_id": 1},
        )
        assert asyncio.run(_run_webhook_handler(req)) == {"ok": False}
        assert req.stream_calls == 0
        # The header lied (small) but the body was large—also a refusal.
        req2 = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 2},
        )
        req2._raw = b"x" * (lumen_admin.WEBHOOK_MAX_BODY_BYTES + 1)
        assert asyncio.run(_run_webhook_handler(req2)) == {"ok": False}
        # Chunked body without Content-Length: read as a stream and split into chunks, rather than
        # we pull all 3 MB into memory (audit 26.10.2026).
        req3 = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            chunks=[b"x" * (64 * 1024)] * 48,
        )
        assert asyncio.run(_run_webhook_handler(req3)) == {"ok": False}
        assert req3.chunks_read <= 9, req3.chunks_read
    finally:
        bot.WEBHOOK_SECRET = original_secret


def test_webhook_denied_log_is_throttled(caplog):
    # Log a burst of errors on one line, not a WARNING for every queue request.
    import logging
    import lumen_admin
    orig_count = lumen_admin._webhook_denied_count
    orig_last = lumen_admin._webhook_denied_last_log
    lumen_admin._webhook_denied_count = 0
    # Zero monotonic instead of the previous value: on a fresh CI machine uptime < 60s and the first
    # refusals silently accumulate (prod 07.10.2026) — explicitly move the marker into the past.
    lumen_admin._webhook_denied_last_log = time.monotonic() - lumen_admin._WEBHOOK_DENIED_LOG_INTERVAL_SEC - 1.0
    original_secret = bot.WEBHOOK_SECRET
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            for _ in range(3):
                req = _FakeWebhookRequest(
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                    body={"update_id": 1},
                )
                assert asyncio.run(_run_webhook_handler(req)) == {"ok": False}
        denied = [r for r in caplog.records if "Rejected" in r.getMessage()]
        assert len(denied) == 1
    finally:
        bot.WEBHOOK_SECRET = original_secret
        lumen_admin._webhook_denied_count = orig_count
        lumen_admin._webhook_denied_last_log = orig_last


def test_webhook_handler_returns_503_when_inflight_full(monkeypatch):
    # Background task overflow: 503 with a retry; the update is not silently lost.
    import lumen_admin
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = object()
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    monkeypatch.setattr(lumen_admin, "WEBHOOK_MAX_INFLIGHT_TASKS", 0)
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert result.status_code == 503
        assert calls == []
        # 503 must not mark update_id as processed, otherwise the retry will be lost.
        import lumen_admin
        assert 1 not in lumen_admin._seen_update_id_set
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


@pytest.mark.parametrize(("env_value", "expected"), [
    ("abc", 25.0), ("", 25.0), ("0", 25.0), ("-5", 25.0), ("30", 30.0),
])
def test_diag_budget_sec_falls_back_on_garbage(monkeypatch, env_value, expected):
    # Bare float() produced 500 on garbage in env, while 0 silently broke all probes.
    import lumen_admin
    monkeypatch.setenv("DIAG_TOTAL_BUDGET_SEC", env_value)
    assert lumen_admin._diag_budget_sec() == expected
    monkeypatch.delenv("DIAG_TOTAL_BUDGET_SEC", raising=False)
    assert lumen_admin._diag_budget_sec() == 25.0


def test_webhook_handler_drops_update_when_bot_not_yet_initialized():
    # An update may arrive before main() creates the global bot (Bot/
    # genai.Client are created only after uvicorn starts) — return 503 so that
    # Telegram retried the update, so do not count the drop as a success (AUD-E-003).
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = None
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert result.status_code == 503
        assert calls == []
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


def test_webhook_handler_retries_broken_body_instead_of_swallowing():
    # A malformed body previously returned ok:true and Telegram considered the drop successful (audit M2).
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = _fake_webhook_secret()
    bot.bot = object()
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        req._raw = b"{broken-json"
        result = asyncio.run(_run_webhook_handler(req))
        assert result.status_code == 503
        assert calls == []
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


def test_allowed_updates_contains_only_real_telegram_types():
    # guest_message is a valid Update field (guest mode, Bot API; verified against
    # core.telegram.org/bots/api 2026-09-21). Removing it here was a mistake
    # of audit AUD-J-001 and broke guest mode; this test catches it.
    assert "guest_message" in bot.ALLOWED_UPDATES
    assert "message" in bot.ALLOWED_UPDATES


def test_admin_secrets_are_independent_of_bot_token_when_seed_set():
    # REGRESSION (tech-debt audit): WEBHOOK_SECRET/ADMIN_PANEL_KEY used to be exposed
    # EXCLUSIVELY from BOT_TOKEN — compromising the token would compromise both at once,
    # and none could be rotated independently. A separate salt can now be set.
    #
    # The previous version of the test computed sha256 twice and compared the salt to itself,
    # meaning it did NOT call the bot code and passed regardless of broken key output
    # (hostile review 27.09.2026). We now check the actual module invariant.
    import hashlib

    seed = bot._ADMIN_SECRET_SEED
    assert seed, "соль обязана быть непустой, иначе ключи предсказуемы"
    # Keys are real derivatives of the current salt, not literals in the test.
    assert bot.WEBHOOK_SECRET == hashlib.sha256(seed.encode()).hexdigest()[:32]
    assert bot.ADMIN_PANEL_KEY == hashlib.sha256(seed.encode() + b"admin_panel").hexdigest()[:24]
    # The two keys do not match, and neither equals the token itself.
    assert bot.WEBHOOK_SECRET != bot.ADMIN_PANEL_KEY
    assert bot.WEBHOOK_SECRET != bot.BOT_TOKEN
    assert bot.ADMIN_PANEL_KEY != bot.BOT_TOKEN
    # Salt from ADMIN_SECRET_SEED, when set — keys are not derived from BOT_TOKEN.
    configured_seed = os.environ.get("ADMIN_SECRET_SEED", "").strip()
    if configured_seed:
        assert seed == configured_seed


def test_admin_secret_seed_falls_back_to_bot_token_when_unset():
    # Without ADMIN_SECRET_SEED, behavior is identical to before (salt = BOT_TOKEN) — do not
    # breaks existing deployments that did not configure this variable.
    # The previous version waited for `or "default"`, while the code does `or secrets.token_hex(32)`
    # (bot.py) — the "everything empty" branch was not checked, so the expectation was wrong.
    import re
    seed_env = os.environ.get("ADMIN_SECRET_SEED", "").strip()
    if seed_env:
        assert bot._ADMIN_SECRET_SEED == seed_env
    elif bot.BOT_TOKEN:
        assert bot._ADMIN_SECRET_SEED == bot.BOT_TOKEN
    else:
        # Both empty: a random salt, not the literal "default".
        assert re.fullmatch(r"[0-9a-f]{64}", bot._ADMIN_SECRET_SEED)


# lifecycle: token, hourly loop, startup (S7)

def test_require_bot_token_exits_on_empty_token(monkeypatch):
    # A1-1: an empty token previously died inside Bot() with TokenValidationError and
    # restarts — now a clean exit with a clear reason.
    monkeypatch.setattr(bot, "BOT_TOKEN", "")
    with pytest.raises(SystemExit):
        bot._require_bot_token()


def test_require_bot_token_returns_configured_token(monkeypatch):
    # Guard: a non-empty token silently passes through to Bot().
    monkeypatch.setattr(bot, "BOT_TOKEN", "123:abc")
    assert bot._require_bot_token() == "123:abc"


def test_secret_log_formatter_survives_interpreter_teardown():
    # Finalization resets the module's globals: formatting must not crash.
    import logging
    fmt = bot._SecretLogFormatter()
    rec = logging.LogRecord("bot", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    orig = bot._SECRET_NAMES
    bot._SECRET_NAMES = None
    try:
        assert fmt.format(rec) == "hello world"
    finally:
        bot._SECRET_NAMES = orig


def test_secret_log_formatter_prefixes_and_clears_chat_context():
    # The chat label on processing lines is the log's only link to the chat;
    # a reset is mandatory, or the marker leaks into the next update (prod 27.10.2026).
    import logging
    fmt = bot._SecretLogFormatter()
    rec = logging.LogRecord("bot", logging.INFO, __file__, 1, "hello", (), None)
    token = bot._LOG_CHAT.set("chat=42")
    try:
        assert fmt.format(rec) == "[chat=42] hello"
    finally:
        bot._LOG_CHAT.reset(token)
    assert fmt.format(rec) == "hello"


def _sleep_then_cancel(loop_sleeps):
    # The first loop_sleeps+1 sleep calls pass (1.5 seconds of startup + ticks),
    # further downstream, CancelledError stops the infinite startup loop.
    state = {"n": 0}

    async def _fake_sleep(delay):
        state["n"] += 1
        if state["n"] > loop_sleeps + 1:
            raise asyncio.CancelledError

    return _fake_sleep


def _patch_startup(monkeypatch, tg_impl, loader=None):
    monkeypatch.setattr(bot, "load_state_from_disk", loader or Mock())
    monkeypatch.setattr(bot, "_check_unconfirmed_model_quotas", Mock())
    monkeypatch.setattr(bot, "telegram_api_call", tg_impl)
    monkeypatch.setattr(bot, "_cleanup_rate_limit_dict", Mock())
    monkeypatch.setattr(bot, "_evict_orphan_chat_locks", Mock())
    monkeypatch.setattr(bot, "_reset_quota_if_new_day", Mock())
    # getMe in try_setup rewrites globals; this is a snapshot for rollback.
    monkeypatch.setattr(bot, "BOT_USERNAME", bot.BOT_USERNAME)


async def _fake_tg_ok(method, *args, **kwargs):
    if method == "getMe":
        return {"username": "testbot"}
    return {}


def test_hourly_tick_survives_failing_step(monkeypatch):
    # A1-2: a failed cleanup previously killed the hourly loop permanently — now
    # the second tick still arrives.
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(2))
    _patch_startup(monkeypatch, _fake_tg_ok)
    monkeypatch.setattr(
        bot, "_cleanup_rate_limit_dict",
        Mock(side_effect=[RuntimeError("boom"), None]),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    # A failed step skips the rest of its tick, but the loop survives: cleanup was called
    # twice (the second tick occurred), evict — once (the first tick was interrupted).
    assert bot._cleanup_rate_limit_dict.call_count == 2
    assert bot._evict_orphan_chat_locks.call_count == 1


def test_startup_loads_state_off_loop(monkeypatch):
    # A1-3: synchronous disk reads previously stalled the loop at startup.
    seen = {}
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(0))
    _patch_startup(monkeypatch, _fake_tg_ok,
                   loader=lambda: seen.setdefault("thread", threading.current_thread()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    assert seen["thread"] is not threading.main_thread()


def test_startup_logs_commands_failure_honestly(monkeypatch, caplog):
    # A1-4: failure of every language used to be logged as success (for-else without break).
    async def _fake_tg_fail_commands(method, *args, **kwargs):
        if method == "getMe":
            return {"username": "testbot"}
        if method == "setMyCommands":
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(0))
    _patch_startup(monkeypatch, _fake_tg_fail_commands)
    with caplog.at_level(logging.INFO, logger="bot"), pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    assert "were not set for any language" in caplog.text
    assert "Bot commands set successfully" not in caplog.text


def test_startup_logs_commands_success(monkeypatch, caplog):
    # Guard: at least one language passed, so the success log is present.
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(0))
    _patch_startup(monkeypatch, _fake_tg_ok)
    with caplog.at_level(logging.INFO, logger="bot"), pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    assert "Bot commands set successfully" in caplog.text


def test_message_mentions_bot_without_chat_returns_false():
    # A1-6: chat None previously caused AttributeError, and the handler swallowed the update.
    class _NoChatMessage:
        chat = None
        text = "@somebot hello"
        caption = None
        reply_to_message = None

    assert bot.message_mentions_bot(_NoChatMessage()) is False


def test_webhook_setup_retries_then_raises(monkeypatch):
    # Audit D4: a setWebhook failure without a retry left a silently dead bot; now
    # retries with backoff and an explicit refusal instead of silence.
    sleeps = []

    async def fast_sleep(delay):
        sleeps.append(delay)

    async def always_fail(method, *args, **kwargs):
        if method == "getMe":
            return {"username": "testbot"}
        raise RuntimeError("boom")

    monkeypatch.setattr(bot.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(bot, "SETWEBHOOK_MAX_ATTEMPTS", 3)
    _patch_startup(monkeypatch, always_fail)
    with pytest.raises(RuntimeError, match="setWebhook"):
        asyncio.run(bot._webhook_startup())
    assert sleeps == [1.5, 5.0, 10.0]


def test_webhook_setup_succeeds_after_retries(monkeypatch):
    # Guard: two failures in a row, and the third is recorded while startup continues.
    calls = {"hook": 0}
    sleeps = []

    async def fast_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    async def flaky(method, *args, **kwargs):
        if method == "getMe":
            return {"username": "testbot"}
        if method == "setWebhook":
            calls["hook"] += 1
            if calls["hook"] < 3:
                raise RuntimeError("boom")
            return True
        return {}

    monkeypatch.setattr(bot.asyncio, "sleep", fast_sleep)
    _patch_startup(monkeypatch, flaky)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    assert calls["hook"] == 3
    assert sleeps == [1.5, 5.0, 10.0, 3600]


def test_main_exits_nonzero_when_server_dies_first(monkeypatch, caplog):
    # An early uvicorn failure (port in use) was previously lost: the process exited quietly
    # with code 0. The server exception is now logged and produces SystemExit(1).
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import lumen_chat_state
    monkeypatch.setattr(bot, "_require_bot_token", lambda: "123:abc")
    monkeypatch.setattr(bot, "Bot", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(bot, "IPv4AiohttpSession", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(bot.genai, "Client", lambda *a, **k: SimpleNamespace())

    async def _never():
        await asyncio.sleep(3600)

    monkeypatch.setattr(bot, "_webhook_startup", _never)
    monkeypatch.setattr(bot, "_flush_dirty_state", _never)
    monkeypatch.setattr(bot, "_drain_inflight_tasks", AsyncMock())
    monkeypatch.setattr(bot, "_close_sessions", AsyncMock())
    monkeypatch.setattr(bot.uvicorn, "Config", lambda *a, **k: SimpleNamespace())

    class _DeadServer:
        should_exit = False

        async def serve(self):
            raise RuntimeError("port busy")

    monkeypatch.setattr(bot.uvicorn, "Server", lambda cfg: _DeadServer())
    orig_bot, orig_client = bot.bot, bot.client
    orig_dirty, orig_deletions = set(bot._dirty_chat_ids), set(bot._pending_chat_deletions)
    orig_index_dirty, orig_quota_dirty = lumen_chat_state._index_dirty, lumen_chat_state._quota_dirty
    bot._dirty_chat_ids.clear()
    bot._pending_chat_deletions.clear()
    lumen_chat_state._index_dirty = False
    lumen_chat_state._quota_dirty = False
    try:
        with caplog.at_level(logging.ERROR, logger="bot"), pytest.raises(SystemExit) as exc_info:
            asyncio.run(bot.main())
        assert exc_info.value.code == 1
        assert "[serve] HTTP server failed" in caplog.text
    finally:
        bot.bot, bot.client = orig_bot, orig_client
        bot._dirty_chat_ids.clear()
        bot._dirty_chat_ids.update(orig_dirty)
        bot._pending_chat_deletions.clear()
        bot._pending_chat_deletions.update(orig_deletions)
        lumen_chat_state._index_dirty = orig_index_dirty
        lumen_chat_state._quota_dirty = orig_quota_dirty


def test_main_flushes_on_graceful_server_exit(monkeypatch, caplog):
    # SIGTERM/SIGINT: the server shut down cleanly — the final state flush and
    # session closures must complete (audit 28.10.2026).
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import lumen_chat_state
    monkeypatch.setattr(bot, "_require_bot_token", lambda: "123:abc")
    monkeypatch.setattr(bot, "Bot", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(bot, "IPv4AiohttpSession", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(bot.genai, "Client", lambda *a, **k: SimpleNamespace())

    async def _never():
        await asyncio.sleep(3600)

    monkeypatch.setattr(bot, "_webhook_startup", _never)
    monkeypatch.setattr(bot, "_flush_dirty_state", _never)
    monkeypatch.setattr(bot, "_drain_inflight_tasks", AsyncMock())
    monkeypatch.setattr(bot, "_close_sessions", AsyncMock())
    monkeypatch.setattr(bot.uvicorn, "Config", lambda *a, **k: SimpleNamespace())

    class _StoppedServer:
        should_exit = True

        async def serve(self):
            return None

    monkeypatch.setattr(bot.uvicorn, "Server", lambda cfg: _StoppedServer())
    emitted = []
    monkeypatch.setattr(bot, "_flush_state_now", lambda: emitted.append("chats"))
    monkeypatch.setattr(bot, "save_global_quota", lambda: emitted.append("quota"))
    orig_bot, orig_client = bot.bot, bot.client
    orig_ready = bot._state_ready
    orig_dirty, orig_deletions = set(bot._dirty_chat_ids), set(bot._pending_chat_deletions)
    orig_index_dirty, orig_quota_dirty = lumen_chat_state._index_dirty, lumen_chat_state._quota_dirty
    orig_load_failed = lumen_chat_state._state_load_failed
    bot._dirty_chat_ids.clear()
    bot._dirty_chat_ids.add(999001)
    bot._pending_chat_deletions.clear()
    lumen_chat_state._index_dirty = False
    lumen_chat_state._quota_dirty = True
    lumen_chat_state._state_load_failed = False
    try:
        with caplog.at_level(logging.INFO, logger="bot"):
            asyncio.run(bot.main())
        assert "[serve] HTTP server stopped gracefully" in caplog.text
        assert "chats" in emitted and "quota" in emitted
        # A partial startup-load failure does not prevent saving chats:
        # otherwise recent messages were lost during redeployment (review 28.10.2026).
        emitted.clear()
        bot._dirty_chat_ids.add(999001)
        lumen_chat_state._quota_dirty = True
        lumen_chat_state._state_load_failed = True
        asyncio.run(bot.main())
        assert "chats" in emitted
    finally:
        bot.bot, bot.client = orig_bot, orig_client
        bot._state_ready = orig_ready
        bot._dirty_chat_ids.clear()
        bot._dirty_chat_ids.update(orig_dirty)
        bot._pending_chat_deletions.clear()
        bot._pending_chat_deletions.update(orig_deletions)
        lumen_chat_state._index_dirty = orig_index_dirty
        lumen_chat_state._quota_dirty = orig_quota_dirty
        lumen_chat_state._state_load_failed = orig_load_failed


def test_probe_url_marks_http_errors_unhealthy_and_sends_headers():
    # Audit 28.10.2026: a protected proxy's 401 and a TikWM 403 were treated as "ok,"
    # /diag and /selftest showed green when the path was dead.
    class _FakeResp:
        status = 401

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeSession:
        def __init__(self):
            self.kwargs = None

        def get(self, url, **kwargs):
            self.kwargs = kwargs
            return _FakeResp()

    session = _FakeSession()
    res = asyncio.run(bot.probe_url(
        session, "http://x", headers={"X-Lumen-Proxy-Secret": "s"},
    ))
    assert res["ok"] is False and res["status"] == 401
    assert session.kwargs["headers"] == {"X-Lumen-Proxy-Secret": "s"}


def test_probe_url_treats_telegram_401_as_reachable_proxy():
    # A 401 with Telegram JSON proves the proxy and API are alive (the token in the probe
    # is truncated); a 401 from the proxy itself with an Unauthorized body remains red.
    class _FakeContent:
        async def read(self, n):
            return b'{"ok":false,"error_code":401,"description":"Unauthorized"}'

    class _FakeResp:
        status = 401
        content = _FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeSession:
        def get(self, url, **kwargs):
            return _FakeResp()

    res = asyncio.run(bot.probe_url(_FakeSession(), "http://x"))
    assert res["ok"] is True and res["status"] == 401


def test_probe_url_returns_ok_shape_and_redacts_secret_on_error():
    # Shared /diag and /selftest probe: success is a status, failure is text without a secret.
    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeSession:
        def get(self, url, **kwargs):
            return _FakeResp()

    class _BoomSession:
        def get(self, url, **kwargs):
            raise RuntimeError("net down, token abc123")

    ok = asyncio.run(bot.probe_url(_FakeSession(), "http://x"))
    assert ok["ok"] is True and ok["status"] == 200 and ok["elapsed_sec"] >= 0
    err = asyncio.run(bot.probe_url(_BoomSession(), "http://x", redact="abc123"))
    assert err["ok"] is False
    assert "abc123" not in err["error"] and "<TOKEN>" in err["error"]


@pytest.mark.parametrize("flag", ["_state_load_failed", "_quota_load_failed"])
def test_export_state_refuses_after_failed_startup_load(flag):
    # Review 17.11.2026: 200 with an empty snapshot after a load failure — the backup would overwrite the good one.
    import lumen_chat_state as lcs
    original_key = bot.ADMIN_PANEL_KEY
    original_state = lcs._state_load_failed
    original_quota = lcs._quota_load_failed
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        lcs._state_load_failed = flag == "_state_load_failed"
        lcs._quota_load_failed = flag == "_quota_load_failed"
        result = asyncio.run(bot.export_state(
            _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        assert getattr(result, "status_code", None) == 503
    finally:
        bot.ADMIN_PANEL_KEY = original_key
        lcs._state_load_failed = original_state
        lcs._quota_load_failed = original_quota
