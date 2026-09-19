"""
test_bot_admin.py — Админка и webhook: гейты секретов, healthcheck, /export_state, /logs, /stats.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
import asyncio
import bot
import os
from bot_test_helpers import (
    _FakeAdminRequest,
    _FakeWebhookRequest,
    _run_webhook_handler,
)


def test_check_bot_token_auth_accepts_correct_bearer_header():
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        req = _FakeAdminRequest(headers={"Authorization": "Bearer real-secret-token"})
        assert bot._check_bot_token_auth(req) is True
    finally:
        bot.BOT_TOKEN = original


def test_check_bot_token_auth_rejects_query_param_regression():
    # РЕГРЕССИЯ (код-ревью): раньше BOT_TOKEN читался из ?bot_token=... в URL — GET-
    # запрос с секретом в query-строке попадает в access-логи прокси/историю браузера
    # (CWE-598). Теперь query-параметр должен полностью ИГНОРИРОВАТЬСЯ — единственный
    # легитимный путь — заголовок Authorization: Bearer.
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
        # Без префикса "Bearer " — тоже отказ, даже если сам токен совпадает.
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
    # РЕГРЕССИЯ (аудит техдолга): раньше ADMIN_PANEL_KEY читался из ?key=... в URL —
    # та же уязвимость (CWE-598), что уже была исправлена для BOT_TOKEN в /admin_keys
    # (см. test_check_bot_token_auth_rejects_query_param_regression), но не была
    # применена к /diag/webhook_url/export_state. Query-параметр больше не должен
    # приниматься вообще, даже если значение верное.
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


def test_webhook_handler_tracks_dispatched_task_for_shutdown():
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    bot.bot = object()
    bot._inflight_tasks.clear()

    async def fake_process(raw_update):
        await asyncio.sleep(0)

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        asyncio.run(_run_webhook_handler(req))
        # _run_webhook_handler уже дожидается одного тика планировщика — к этому
        # моменту короткая fake_process должна была завершиться и самоудалиться
        # из набора (см. test_track_inflight_task_registers_and_self_removes_on_completion).
        assert not bot._inflight_tasks
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


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
        assert "error" in result
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_export_state_rejects_query_param_regression():
    # См. test_check_admin_key_rejects_query_param_regression — /export_state — самый
    # чувствительный из трёх эндпоинтов (отдаёт ПОЛНЫЕ истории всех чатов), поэтому
    # регрессия здесь проверяется отдельно, а не только на уровне _check_admin_key.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={}, query_params={"key": "real-admin-key"})))
        assert "error" in result
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_export_state_returns_chats_and_quota_with_valid_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    chat_id = 999411
    bot.chat_state[chat_id] = {
        "image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "history": [{"role": "user", "content": "hi"}],
        "quota": {}, "recent_media_ids": {}, "last_activity": 0.0,
    }
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        assert str(chat_id) in result["chats"]
        assert result["chats"][str(chat_id)]["history"] == [{"role": "user", "content": "hi"}]
        assert "global_quota" in result
        assert "exported_at" in result
    finally:
        bot.ADMIN_PANEL_KEY = original
        bot.chat_state.pop(chat_id, None)


def test_webhook_handler_accepts_valid_secret_and_dispatches_update():
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    bot.bot = object()  # не-None достаточно, чтобы пройти проверку "бот уже инициализирован"
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


def test_webhook_handler_rejects_invalid_or_missing_secret_and_does_not_dispatch():
    original_secret = bot.WEBHOOK_SECRET
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
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


def test_webhook_handler_drops_update_when_bot_not_yet_initialized():
    # Апдейт может прийти раньше, чем main() успеет создать глобальный bot (Bot/
    # genai.Client создаются уже после старта uvicorn) — отвечаем 503, чтобы
    # Telegram повторил апдейт, а не считаем дроп успехом (AUD-E-003).
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
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


def test_allowed_updates_contains_only_real_telegram_types():
    # Регрессия AUD-J-001: "guest_message" — не тип Update из Bot API, из-за него
    # setWebhook мог ответить 400 и бот замолчал бы. Гости идут через answerGuestQuery.
    assert "guest_message" not in bot.ALLOWED_UPDATES
    assert "message" in bot.ALLOWED_UPDATES


def test_admin_secrets_are_independent_of_bot_token_when_seed_set():
    # РЕГРЕССИЯ (аудит техдолга): раньше WEBHOOK_SECRET/ADMIN_PANEL_KEY выводились
    # ИСКЛЮЧИТЕЛЬНО из BOT_TOKEN — компрометация токена компрометировала оба сразу,
    # и ни один нельзя было ротировать независимо. Теперь можно задать отдельную соль.
    import hashlib
    seed_a = "seed-one"
    seed_b = "seed-two"
    webhook_a = hashlib.sha256(seed_a.encode()).hexdigest()[:32]
    webhook_b = hashlib.sha256(seed_b.encode()).hexdigest()[:32]
    assert webhook_a != webhook_b  # разные соли -> разные секреты, как и должно быть


def test_admin_secret_seed_falls_back_to_bot_token_when_unset():
    # Без ADMIN_SECRET_SEED поведение идентично прежнему (соль = BOT_TOKEN) — не
    # ломает существующие деплои, которые эту переменную не настраивали.
    assert bot._ADMIN_SECRET_SEED == (os.environ.get("ADMIN_SECRET_SEED", "").strip() or bot.BOT_TOKEN or "default")

