"""
test_bot_admin.py — Админка и webhook: гейты секретов, healthcheck, /export_state, /logs, /stats.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
import asyncio
import bot
import logging
import os
import threading
import pytest
from unittest.mock import Mock
from tests.bot_test_helpers import (
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


def test_bearer_non_ascii_returns_false_not_500():
    # compare_digest на str падает TypeError на не-ASCII: отказ без исключения.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        assert bot._check_bearer_token(_FakeAdminRequest(headers={"Authorization": "Bearer café"}), "real-admin-key") is False
        assert bot._check_admin_key(_FakeAdminRequest(headers={"Authorization": "Bearer café"})) is False
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_webhook_non_ascii_secret_returns_false_not_500():
    original = bot.WEBHOOK_SECRET
    bot.WEBHOOK_SECRET = "real-webhook-secret"
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
        # Отказ — честный 401, а не 200 с телом {"error": ...}: иначе брутфорс
        # ключа отличался от успеха только телом ответа (аудит 26.09.2026).
        assert result.status_code == 401
        assert b"error" in result.body
    finally:
        bot.ADMIN_PANEL_KEY = original


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
    # Публичная схема на HF Space описывала все эндпоинты бесплатно.
    assert bot.app.docs_url is None
    assert bot.app.redoc_url is None
    assert bot.app.openapi_url is None


def test_export_state_returns_detached_snapshot():
    # Аудит A11-05: экспорт отдавал живые ссылки на мутабельные объекты —
    # запись между возвратом и сериализацией давала несогласованный бэкап.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    chat_id = 999980
    state = bot.get_state(chat_id)
    state["history"].append({"role": "user", "content": "hi"})
    bot.GLOBAL_QUOTA.setdefault("groq", {})["snaptest"] = {"used": 1}
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        bot.GLOBAL_QUOTA["groq"]["snaptest"]["used"] = 999
        state["history"].append({"role": "user", "content": "after"})
        assert result["global_quota"]["groq"]["snaptest"]["used"] == 1
        assert all(m["content"] != "after" for m in result["chats"][str(chat_id)]["history"])
    finally:
        bot.GLOBAL_QUOTA.get("groq", {}).pop("snaptest", None)
        bot.chat_state.pop(chat_id, None)
        bot.ADMIN_PANEL_KEY = original


def test_export_state_rejects_query_param_regression():
    # См. test_check_admin_key_rejects_query_param_regression — /export_state — самый
    # чувствительный из трёх эндпоинтов (отдаёт ПОЛНЫЕ истории всех чатов), поэтому
    # регрессия здесь проверяется отдельно, а не только на уровне _check_admin_key.
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
    # guest_message — валидное поле Update (guest mode, Bot API; проверено по
    # core.telegram.org/bots/api 2026-09-21). Удаление отсюда было ошибкой
    # аудита AUD-J-001 и ломало гостевой режим — этот тест её ловит.
    assert "guest_message" in bot.ALLOWED_UPDATES
    assert "message" in bot.ALLOWED_UPDATES


def test_admin_secrets_are_independent_of_bot_token_when_seed_set():
    # РЕГРЕССИЯ (аудит техдолга): раньше WEBHOOK_SECRET/ADMIN_PANEL_KEY выводились
    # ИСКЛЮЧИТЕЛЬНО из BOT_TOKEN — компрометация токена компрометировала оба сразу,
    # и ни один нельзя было ротировать независимо. Теперь можно задать отдельную соль.
    #
    # Прежняя версия теста считала sha256 дважды и сравнивала соль саму с собой,
    # то есть НЕ вызывала код бота и проходила при любой поломке вывода ключей
    # (враждебное ревью 27.09.2026). Теперь проверяем настоящий инвариант модуля.
    import hashlib

    seed = bot._ADMIN_SECRET_SEED
    assert seed, "соль обязана быть непустой, иначе ключи предсказуемы"
    # Ключи — реальные производные текущей соли, а не литералы в тесте.
    assert bot.WEBHOOK_SECRET == hashlib.sha256(seed.encode()).hexdigest()[:32]
    assert bot.ADMIN_PANEL_KEY == hashlib.sha256(seed.encode() + b"admin_panel").hexdigest()[:24]
    # Два ключа не совпадают, и ни один не равен самому токену.
    assert bot.WEBHOOK_SECRET != bot.ADMIN_PANEL_KEY
    assert bot.WEBHOOK_SECRET != bot.BOT_TOKEN
    assert bot.ADMIN_PANEL_KEY != bot.BOT_TOKEN
    # Соль из ADMIN_SECRET_SEED, когда она задана, — ключи не выводятся из BOT_TOKEN.
    configured_seed = os.environ.get("ADMIN_SECRET_SEED", "").strip()
    if configured_seed:
        assert seed == configured_seed


def test_admin_secret_seed_falls_back_to_bot_token_when_unset():
    # Без ADMIN_SECRET_SEED поведение идентично прежнему (соль = BOT_TOKEN) — не
    # ломает существующие деплои, которые эту переменную не настраивали.
    # Прежняя версия ждала `or "default"`, а код делает `or secrets.token_hex(32)`
    # (bot.py) — ветка «всё пусто» не проверялась и ожидание было неверным.
    import re
    seed_env = os.environ.get("ADMIN_SECRET_SEED", "").strip()
    if seed_env:
        assert bot._ADMIN_SECRET_SEED == seed_env
    elif bot.BOT_TOKEN:
        assert bot._ADMIN_SECRET_SEED == bot.BOT_TOKEN
    else:
        # Оба пустые: случайная соль, а не литерал "default".
        assert re.fullmatch(r"[0-9a-f]{64}", bot._ADMIN_SECRET_SEED)


# ─────────────── lifecycle: токен, часовой цикл, старт (S7) ───────────────

def test_require_bot_token_exits_on_empty_token(monkeypatch):
    # A1-1: пустой токен раньше умирал внутри Bot() с TokenValidationError и
    # рестартами — теперь чистый выход с понятной причиной.
    monkeypatch.setattr(bot, "BOT_TOKEN", "")
    with pytest.raises(SystemExit):
        bot._require_bot_token()


def test_require_bot_token_returns_configured_token(monkeypatch):
    # Сторож: непустой токен молча проходит дальше в Bot().
    monkeypatch.setattr(bot, "BOT_TOKEN", "123:abc")
    assert bot._require_bot_token() == "123:abc"


def _sleep_then_cancel(loop_sleeps):
    # Первые loop_sleeps+1 вызовов sleep проходят (1.5с старта + тики),
    # дальше CancelledError останавливает бесконечный цикл стартапа.
    state = {"n": 0}

    async def _fake_sleep(delay):
        state["n"] += 1
        if state["n"] > loop_sleeps + 1:
            raise asyncio.CancelledError

    return _fake_sleep


def _patch_startup(monkeypatch, tg_impl, loader=None):
    monkeypatch.setattr(bot, "load_state_from_disk", loader or Mock())
    monkeypatch.setattr(bot, "_check_temporary_free_models_expiry", Mock())
    monkeypatch.setattr(bot, "_check_unconfirmed_model_quotas", Mock())
    monkeypatch.setattr(bot, "_check_scheduled_removals_due", Mock())
    monkeypatch.setattr(bot, "telegram_api_call", tg_impl)
    monkeypatch.setattr(bot, "_cleanup_rate_limit_dict", Mock())
    monkeypatch.setattr(bot, "_evict_orphan_chat_locks", Mock())
    monkeypatch.setattr(bot, "_reset_quota_if_new_day", Mock())
    # getMe в try_setup переписывает globals — снапшот для отката.
    monkeypatch.setattr(bot, "BOT_USERNAME", bot.BOT_USERNAME)
    monkeypatch.setattr(bot, "OPENROUTER_HTTP_REFERER", bot.OPENROUTER_HTTP_REFERER)


async def _fake_tg_ok(method, *args, **kwargs):
    if method == "getMe":
        return {"username": "testbot"}
    return {}


def test_hourly_tick_survives_failing_step(monkeypatch):
    # A1-2: упавшая чистка раньше убивала часовой цикл навсегда — теперь
    # второй тик всё равно наступает.
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(2))
    _patch_startup(monkeypatch, _fake_tg_ok)
    monkeypatch.setattr(
        bot, "_cleanup_rate_limit_dict",
        Mock(side_effect=[RuntimeError("boom"), None]),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    # Упавший шаг пропускает остаток своего тика, но цикл живёт: чистка звалась
    # дважды (второй тик наступил), evict — один раз (первый тик оборвался).
    assert bot._cleanup_rate_limit_dict.call_count == 2
    assert bot._evict_orphan_chat_locks.call_count == 1


def test_startup_loads_state_off_loop(monkeypatch):
    # A1-3: синхронное чтение диска раньше стопорило loop на старте.
    seen = {}
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(0))
    _patch_startup(monkeypatch, _fake_tg_ok,
                   loader=lambda: seen.setdefault("thread", threading.current_thread()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    assert seen["thread"] is not threading.main_thread()


def test_startup_logs_commands_failure_honestly(monkeypatch, caplog):
    # A1-4: провал всех языков раньше логировался как успех (for-else без break).
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
    # Сторож: хотя бы один язык прошёл — лог успеха на месте.
    monkeypatch.setattr(bot.asyncio, "sleep", _sleep_then_cancel(0))
    _patch_startup(monkeypatch, _fake_tg_ok)
    with caplog.at_level(logging.INFO, logger="bot"), pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._webhook_startup())
    assert "Bot commands set successfully" in caplog.text


def test_message_mentions_bot_without_chat_returns_false():
    # A1-6: chat None раньше давал AttributeError, апдейт глотался хендлером.
    class _NoChatMessage:
        chat = None
        text = "@somebot hello"
        caption = None
        reply_to_message = None

    assert bot.message_mentions_bot(_NoChatMessage()) is False


def test_webhook_setup_retries_then_raises(monkeypatch):
    # Аудит D4: провал setWebhook без ретрая давал тихий мёртвый бот — теперь
    # ретраи с бэкоффом и явный отказ вместо молчания.
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
    # Сторож: два провала подряд — третий регистрирует, стартап живёт дальше.
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


def test_probe_url_returns_ok_shape_and_redacts_secret_on_error():
    # Общий зонд /diag и /selftest: успех — статус, неуспех — текст без секрета.
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

