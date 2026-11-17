"""
Регрессии ревью 17.11.2026: маршрутизация, защита состояния, квоты, /tts.

Каждый тест падает на коде до соответствующего фикса и проходит после него.
"""
import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import lumen_chat_state as lcs
import lumen_commands
import lumen_errors
import lumen_images
import lumen_model_speed
import lumen_quotas as lq
import lumen_router_config
import lumen_routes
import lumen_state_storage as lss
import lumen_status
import pytest
from tests.bot_test_helpers import _FakeIncomingMessage


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """Каждый тест файла оставляет квоту, чаты и флаги как были: иначе соседний
    файл сьюта видит обрезанную квоту и грязные очереди (адверсариальный проход)."""
    saved_quota = copy.deepcopy(bot.GLOBAL_QUOTA)
    saved_chats = dict(bot.chat_state)
    yield
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update(saved_quota)
    bot.chat_state.clear()
    bot.chat_state.update(saved_chats)
    lcs._state_load_failed = False
    lcs._quota_load_failed = False
    lcs._dirty_chat_ids.clear()
    lcs._index_dirty = False
    lcs._quota_dirty = False
    lcs._pending_chat_deletions.clear()
    lcs._chat_snapshot_unloaded.clear()
    lcs._index_retry_last_ts = 0.0
    lcs._index_extra_ids.clear()
    lcs._deleted_chat_ids.clear()
    lq._quota_retry_last_ts = 0.0
    lumen_model_speed._latency_ema.clear()


def _reset_state_flags():
    lcs._state_load_failed = False
    lcs._quota_load_failed = False
    lcs._pending_chat_deletions.clear()
    lcs._chat_snapshot_unloaded.clear()
    lcs._dirty_chat_ids.clear()
    lcs._index_dirty = False
    lcs._quota_dirty = False


# ── F-031: память чата не теряется в нестримовом цикле Munito ──


def test_chat_completion_chain_keeps_memory_in_system_message():
    # Память обязана пережить КАЖДУЮ попытку: перезапись system шла на каждой модели.
    chat_id = 998301
    state = bot.get_state(chat_id)
    state["memory_summary"] = "MEMFACT-7731: любит чай"
    payloads = []

    async def fake_request(payload, deadline):
        payloads.append(payload)
        if payload["model"] == "probe/mem-first":
            raise bot.MunitoAPIError("HTTP 503 no free capacity", status_code=503)
        return {"choices": [{"message": {"content": "ok"}}]}

    try:
        answer, _ = asyncio.run(lumen_routes._chat_completion_chain(
            [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}],
            ["probe/mem-first", "probe/mem-model"], "probe/mem-first",
            request_fn=fake_request, provider="munito", chat_id=chat_id,
        ))
    finally:
        bot.chat_state.pop(chat_id, None)
    assert answer == "ok" and len(payloads) == 2
    for payload in payloads:
        system = payload["messages"][0]["content"]
        assert "MEMFACT-7731" in system, "память теряется на одной из попыток"
        assert len(system) > 500, "базовый промпт подменён пустотой"


# ── F-032: перестановка по скорости не ломает голову search-цепочки ──


def test_search_chain_keeps_grounded_head_after_speed_reorder():
    lumen_model_speed._latency_ema.clear()
    try:
        lumen_model_speed.record_response(
            lumen_model_speed.speed_key("gemini", "gemini-2.5-flash"), total_sec=90.0)
        speeds = {
            "gemini-3.8-flash": 10.0, "gemini-3.5-flash-lite": 2.0,
            "gemini-3.1-flash-lite": 6.0, "gemini-3.7-flash": 8.0,
            "gemini-3.6-flash": 9.0, "gemini-3.5-flash": 5.0,
            "gemini-3-flash-preview": 7.0,
        }
        for model, speed in speeds.items():
            lumen_model_speed.record_response(
                lumen_model_speed.speed_key("gemini", model), total_sec=speed)
        route = [("gemini", m) for m in lumen_router_config.GEMINI_SEARCH_CHAIN]
        ordered = lumen_routes._reorder_route_by_speed(route, preserve_grounded_head=True)
        head = ordered[0][1]
        assert lumen_router_config.GEMINI_MODELS.get(head, {}).get("search_grounding") is True, (
            f"голова search-цепочки {head} без search grounding")
        # Сортировка по скорости не выключена: самый быстрый из негрупповых — первый.
        non_grounded = [
            m for _, m in ordered
            if not lumen_router_config.GEMINI_MODELS.get(m, {}).get("search_grounding", True)
        ]
        assert non_grounded[0] == "gemini-3.5-flash-lite"
    finally:
        lumen_model_speed._latency_ema.clear()


# ── F-033: HTTP 400 одного запроса не выключает модель для всех ──


def test_chat_completion_chain_does_not_cool_down_on_400():
    async def boom_400(payload, deadline):
        raise bot.MunitoAPIError("HTTP 400 invalid argument: image too large", status_code=400)

    model = "probe/bad-request"
    provider = "munito"
    bot.GLOBAL_QUOTA.setdefault(provider, {}).pop(model, None)
    try:
        with pytest.raises(bot.MunitoAPIError):
            asyncio.run(lumen_routes._chat_completion_chain(
                [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}],
                [model], model, request_fn=boom_400, provider=provider,
            ))
        entry = bot.GLOBAL_QUOTA.get(provider, {}).get(model) or {}
        assert entry.get("cooldown_until") is None, (
            "детерминированный 400 не должен ставить глобальную остывку")
    finally:
        bot.GLOBAL_QUOTA.get(provider, {}).pop(model, None)


# ── F-039: флаш не удаляет удалённые истории замороженных чатов ──


def test_flush_skips_deletes_of_frozen_and_failed_state():
    ok_chat, frozen_chat = 998311, 998312
    deleted = []
    _reset_state_flags()
    lcs._pending_chat_deletions.update({ok_chat, frozen_chat})
    lcs._chat_snapshot_unloaded.add(frozen_chat)

    def fake_delete(cid):
        deleted.append(cid)
        return True

    try:
        with patch("bot._delete_chat_storage", side_effect=fake_delete), \
             patch("bot._save_chat_index_payload"), patch("bot._save_quota_payload"):
            asyncio.run(bot._flush_dirty_state_once())
        assert frozen_chat not in deleted, "снимок не прочитан — удалённую копию не трогаем"
        assert frozen_chat in lcs._pending_chat_deletions, "id остаётся в очереди до загрузки"
        assert ok_chat in deleted, "обычное удаление не должно блокироваться"

        deleted.clear()
        lcs._pending_chat_deletions.update({ok_chat, frozen_chat})
        lcs._chat_snapshot_unloaded.clear()
        lcs._state_load_failed = True
        with patch("bot._delete_chat_storage", side_effect=fake_delete), \
             patch("bot._save_chat_index_payload"), patch("bot._save_quota_payload"):
            asyncio.run(bot._flush_dirty_state_once())
        assert deleted == [], "при сбое загрузки никакие удаления не выполняются"
        assert {ok_chat, frozen_chat} <= lcs._pending_chat_deletions
    finally:
        _reset_state_flags()


# ── F-042: четвёртая попытка лока не отдаёт чужой лок ──


def test_acquire_chat_lock_never_returns_unregistered_lock(monkeypatch):
    made = []

    def fake_get_chat_lock(chat_id):
        lock = asyncio.Lock()
        made.append(lock)
        return lock  # реестр его не помнит: поколение всегда другое

    monkeypatch.setattr(lcs, "get_chat_lock", fake_get_chat_lock)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await lcs.acquire_chat_lock(998321, timeout=0.01)

    asyncio.run(scenario())
    assert len(made) == lcs._CHAT_LOCK_MAX_RETRIES, "ровно три попытки, без четвёртой"


# ── F-043: восстановление квоты во флаше не блокирует event loop ──


def test_flush_quota_recovery_runs_off_event_loop():
    _reset_state_flags()
    lcs._quota_load_failed = True
    lcs._quota_dirty = True
    main_thread = threading.current_thread()
    seen = []

    def fake_recover():
        seen.append(threading.current_thread())
        return False

    try:
        with patch("bot.recover_global_quota", side_effect=fake_recover), \
             patch("bot._save_chat_index_payload"), patch("bot._save_quota_payload"):
            asyncio.run(bot._flush_dirty_state_once())
        assert seen, "восстановление квоты должно вызываться"
        assert seen[0] is not main_thread, "синхронный urllib не должен идти в петле"
    finally:
        _reset_state_flags()


# ── F-045: нулевая метка ретрая не блокирует первое чтение квоты ──


def test_quota_recovery_first_retry_reads_immediately(monkeypatch):
    lcs._quota_load_failed = True
    lq._quota_retry_last_ts = 0.0
    calls = []

    def fake_load():
        calls.append(1)

    monkeypatch.setattr(lq, "load_global_quota", fake_load)
    monkeypatch.setattr(lq.time, "monotonic", lambda: 5.0)
    try:
        lq.recover_global_quota()
        assert calls == [1], "monotonic()<60 не означает «только что пробовали»"
    finally:
        lcs._quota_load_failed = False
        lq._quota_retry_last_ts = 0.0


# ── F-075: нефинитный Retry-After не замораживает картинки навсегда ──


@pytest.mark.parametrize(("raw", "expected"), [
    ("inf", 86400.0), ("1e309", 86400.0), ("-inf", 0.0), ("nan", 0.0),
])
def test_retry_after_is_bounded(raw, expected):
    resp = SimpleNamespace(headers={"Retry-After": raw})
    assert lumen_images._retry_after_sec(resp) == expected


def test_image_rate_limit_cooldown_is_bounded():
    provider = "probe-image"
    bot.GLOBAL_QUOTA.setdefault(lq.IMAGE_QUOTA_PROVIDER, {}).pop(provider, None)
    try:
        lq._note_image_rate_limited(provider, retry_after_sec=float("inf"))
        entry = bot._quota_entry(lq.IMAGE_QUOTA_PROVIDER, provider)
        until = entry["cooldown_until"]
        assert until is not None and until < float("inf")
        # Нижняя граница — штатная пауза провайдера, верхняя — сутки.
        assert until >= time.time() + bot.IMAGE_PROVIDER_COOLDOWN_SEC - 1
        assert until <= time.time() + 86400 + 60
    finally:
        bot.GLOBAL_QUOTA.get(lq.IMAGE_QUOTA_PROVIDER, {}).pop(provider, None)


# ── F-086: валидный JSON не-словарь — сбой загрузки, а не успех ──


@pytest.mark.parametrize("raw", ["null", "[]", "\"text\""])
def test_quota_load_treats_non_dict_payload_as_failure(raw, monkeypatch):
    lcs._quota_load_failed = False
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: raw)
    try:
        lq.load_global_quota()
        assert lcs._quota_load_failed is True, "не-словарь нельзя считать прочитанной квотой"
    finally:
        lcs._quota_load_failed = False


# ── F-087: recovery сливает живую память с удалённым снапшотом ──


def test_quota_recovery_sums_live_counters_and_merges_all_providers(monkeypatch):
    today = bot._current_quota_day()
    remote = json.dumps({
        "gemini": {"m1": {"used": 1, "exhausted_at": None, "cooldown_until": None}},
        "image": {"cf": {"used": 3, "neurons": 300.0, "exhausted_at": None, "cooldown_until": None}},
        "user_daily": {
            "777": {"total": 4, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0},
            "998332": {"total": 1, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0},
        },
        "banned_users": {"888": 1}, "quota_day": today,
    })
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({"gemini": {}, "image": {}, "user_daily": {}, "quota_day": today})
    lcs._quota_load_failed = True
    bot._ban_user(998331)
    bot._record_user_daily(998332)
    bot._record_user_daily(998332)
    bot._record_quota_usage("gemini", "m1")
    bot.GLOBAL_QUOTA["image"]["cf"] = {
        "used": 7, "neurons": 700.0, "exhausted_at": None, "cooldown_until": None,
    }
    lq._quota_retry_last_ts = -1e9
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: remote)
    ok = lq.recover_global_quota()
    assert ok is True
    assert bot._is_banned(998331) is True, "бан, поставленный во время сбоя, не теряется"
    assert bot._is_banned(888) is True, "удалённый бан остаётся"
    assert bot._user_daily_peek(998332)["total"] == 3, "удалённая единица + две локальные"
    assert bot.GLOBAL_QUOTA["user_daily"]["777"]["total"] == 4
    assert bot.GLOBAL_QUOTA["gemini"]["m1"]["used"] == 2, "расход сбоя суммируется"
    assert bot.GLOBAL_QUOTA["image"]["cf"]["used"] == 10
    assert bot.GLOBAL_QUOTA["image"]["cf"]["neurons"] == 1000.0


def test_quota_recovery_keeps_ban_across_midnight(monkeypatch):
    # Сутки сменились за время сбоя: счётчики не воскрешаем, но бан обязан остаться.
    remote = json.dumps({
        "gemini": {}, "user_daily": {}, "banned_users": {}, "quota_day": "2000-01-01",
    })
    bot.GLOBAL_QUOTA.update({"quota_day": "2026-11-17"})
    lcs._quota_load_failed = True
    bot._ban_user(998333)
    lq._quota_retry_last_ts = -1e9
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: remote)
    assert lq.recover_global_quota() is True
    assert bot._is_banned(998333) is True


# ── F-181: ответ шлюза без result — сбой чтения, а не «ключа нет» ──


def test_upstash_get_raises_when_gateway_response_has_no_result(monkeypatch):
    monkeypatch.setattr(lss, "_upstash_request", lambda *a, **k: {"error": "bad gateway"})
    with pytest.raises(RuntimeError):
        lss._upstash_get("https://example.invalid", "token", "lumen:chat_index")


def test_upstash_get_missing_key_still_returns_none(monkeypatch):
    monkeypatch.setattr(lss, "_upstash_request", lambda *a, **k: {"result": None})
    assert lss._upstash_get("https://example.invalid", "token", "lumen:chat_index") is None


# ── F-085: отмена /tts возвращает зарезервированные слоты ──


def _tts_incoming(chat_id):
    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    return incoming


def _spy_daily_reserve(monkeypatch, calls):
    orig_reserve = bot._reserve_user_daily
    orig_refund = bot._refund_user_daily

    def reserve(*args, **kwargs):
        calls.append(("res", kwargs.get("total"), kwargs.get("tts")))
        return orig_reserve(*args, **kwargs)

    def refund(*args, **kwargs):
        calls.append(("ref", kwargs.get("total"), kwargs.get("tts")))
        return orig_refund(*args, **kwargs)

    monkeypatch.setattr(bot, "_reserve_user_daily", reserve)
    monkeypatch.setattr(bot, "_refund_user_daily", refund)


def test_inline_tts_cancel_during_synthesis_refunds_slots(monkeypatch):
    incoming = _tts_incoming(998341)
    uid = bot._user_key_for_message(incoming)
    bot.GLOBAL_QUOTA["user_daily"] = {}
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    started = asyncio.Event()
    calls = []
    spent = {}
    _spy_daily_reserve(monkeypatch, calls)

    async def hang(chunk):
        spent.update(bot._user_daily_peek(uid) or {})
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(bot.inline_tts(incoming, "hello world"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0 and entry["tts"] == 0

    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", hang)
    monkeypatch.setattr(lumen_status.StatusMessage, "start_action", AsyncMock(return_value=None))
    asyncio.run(scenario())
    assert spent.get("total") == 1, "резерв обязан реально списываться"
    assert ("res", 1, 1) in calls and ("ref", 1, 1) in calls


def test_inline_tts_cancel_during_status_refunds_slots(monkeypatch):
    incoming = _tts_incoming(998342)
    uid = bot._user_key_for_message(incoming)
    bot.GLOBAL_QUOTA["user_daily"] = {}
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    started = asyncio.Event()
    calls = []
    _spy_daily_reserve(monkeypatch, calls)

    async def hang_status(*a, **k):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(bot.inline_tts(incoming, "hello world"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0 and entry["tts"] == 0

    monkeypatch.setattr(lumen_status.StatusMessage, "start_action", hang_status)
    asyncio.run(scenario())
    assert ("res", 1, 1) in calls and ("ref", 1, 1) in calls


# ── F-049: фото-пост без скачанных слайдов отказывает, а не шлёт аудио видео ──


def test_slideshow_with_no_downloaded_slides_refuses_instead_of_sending_audio(monkeypatch):
    import lumen_tiktok

    async def fake_download(session, url, headers=None, cap_bytes=None):
        return None

    sends = []

    class _FakeTgBot:
        async def send_photo(self, **kwargs):
            sends.append("photo")
            return SimpleNamespace()

        async def send_video(self, **kwargs):
            sends.append("video")
            return SimpleNamespace()

        async def send_media_group(self, **kwargs):
            sends.append("group")
            return SimpleNamespace()

    monkeypatch.setattr(bot, "_download_url_bin", fake_download)
    monkeypatch.setattr(bot, "bot", _FakeTgBot())
    incoming = _FakeIncomingMessage(998351)
    incoming.message_id = 7
    media_data = {
        "images": ["http://x/0.jpg"],
        "live_images": ["http://x/0.mp4"],
        "play": "http://x/audio.mp3",
    }
    with pytest.raises(lumen_tiktok.TikTokUserFacingError):
        asyncio.run(bot._try_send_tiktok_slideshow(None, media_data, incoming, None, "author", {}))
    assert sends == [], "аудио не должно уходить ни как видео, ни как фото"
    bot.chat_state.pop(998351, None)


# ── F-050: отмена в finally музыкального шага не глотается ──


def test_music_cancellation_in_finally_propagates(monkeypatch):
    import lumen_tiktok_flow as ltf

    holder = {}

    async def fake_download(session, url, headers=None, cap_bytes=None):
        holder["loop"].call_soon(holder["task"].cancel)
        return None

    async def hang(session, media_data):
        await asyncio.Event().wait()

    class _Slot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(bot, "_download_url_bin", fake_download)
    monkeypatch.setattr(bot, "_media_download_slot", lambda: _Slot())
    monkeypatch.setattr(ltf, "_fetch_dsp_track_metadata", hang)
    incoming = _FakeIncomingMessage(998361)
    media = {
        "id": "1",
        "music": "http://x/m.mp3",
        "music_info": {"id": "s1", "title": "T", "author": "A"},
        "author": {"nickname": "N", "unique_id": "u"},
    }

    async def scenario():
        holder["loop"] = asyncio.get_running_loop()
        task = asyncio.create_task(ltf._send_tiktok_music(None, media, incoming, "author", {}))
        holder["task"] = task
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(scenario()) is True, "отмена снаружи обязана пройти дальше finally"
    bot.chat_state.pop(998361, None)


# ── F-034: законный короткий ответ «None» не считается плейсхолдером ──


@pytest.mark.parametrize("text", ["None", "none", "N/A", "n/a"])
def test_honest_short_answer_is_not_empty_like(text):
    assert lumen_errors._is_empty_like(text) is False


@pytest.mark.parametrize("text", ["Empty response", "no response", "No answer", "   "])
def test_placeholders_are_still_empty_like(text):
    assert lumen_errors._is_empty_like(text) is True


# ── F-037: 402 важнее слова «quota» в тексте ──


def test_402_billing_quota_is_paid_not_rate_limit():
    assert bot._classify_model_error(402, "billing quota exceeded") == "paid"


# ── F-039 (адверсариальный проход): shutdown-флаш не удаляет замороженные чаты ──


def test_shutdown_flush_skips_frozen_chat_deletes():
    ok_chat, frozen_chat = 998371, 998372
    _reset_state_flags()
    lcs._pending_chat_deletions.update({ok_chat, frozen_chat})
    lcs._chat_snapshot_unloaded.add(frozen_chat)
    deleted = []

    def fake_delete(cid):
        deleted.append(cid)
        return True

    with patch("bot._delete_chat_storage", side_effect=fake_delete):
        lcs._flush_state_now()
    assert frozen_chat not in deleted
    assert frozen_chat in lcs._pending_chat_deletions
    assert ok_chat in deleted


# ── F-181 (адверсариальный проход): сбой загрузки сам будит ретрай индекса ──


def test_flush_retries_index_after_load_failure_without_dirty_flag():
    _reset_state_flags()
    lcs._state_load_failed = True
    lcs._index_dirty = False
    calls = []
    with patch.object(lcs, "_recover_index_ids", side_effect=lambda: calls.append(1) or False):
        asyncio.run(bot._flush_dirty_state_once())
    assert calls, "без ретрая запись стоит до рестарта"


# ── F-075 (адверсариальный проход): Infinity из старого снапшота снимается ──


def test_loaded_non_finite_cooldown_is_cleared(monkeypatch):
    raw = json.dumps({
        "image": {"cf": {"used": 1, "neurons": 104.2, "exhausted_at": None,
                          "cooldown_until": float("inf")}},
        "quota_day": bot._current_quota_day(),
    })
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: raw)
    lq.load_global_quota()
    assert bot.GLOBAL_QUOTA["image"]["cf"]["cooldown_until"] is None
    assert bot._image_provider_block_reason("cf") is None


# ── F-033 (адверсариальный проход): 503 и invalid model по-прежнему остывают ──


@pytest.mark.parametrize(("status", "text"), [
    (503, "model is currently unavailable"),
    (400, "invalid model id"),
    (400, "gemini-9 is not a valid model ID"),
    (400, "model_not_found"),
    (400, "The model `x` has been deprecated"),
    (400, "model `x` is not found for API version v1beta"),
    (400, "unsupported model foo"),
])
def test_unavailable_and_invalid_model_still_cool_down(status, text):
    async def boom(payload, deadline):
        raise bot.MunitoAPIError(f"HTTP {status} {text}", status_code=status)

    model = f"probe/cool-{status}"
    try:
        with pytest.raises(bot.MunitoAPIError):
            asyncio.run(lumen_routes._chat_completion_chain(
                [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}],
                [model], model, request_fn=boom, provider="munito",
            ))
        assert bot.GLOBAL_QUOTA["munito"][model]["cooldown_until"] > time.time()
    finally:
        bot.GLOBAL_QUOTA.get("munito", {}).pop(model, None)


# ── F-032 (адверсариальный проход): обычный маршрут по-прежнему снимает медленную голову ──


def test_heavy_route_still_demotes_slow_grounded_head():
    import lumen_model_speed
    lumen_model_speed._latency_ema.clear()
    try:
        lumen_model_speed.record_response(
            lumen_model_speed.speed_key("gemini", "gemini-2.5-flash"), total_sec=18.0)
        lumen_model_speed.record_response(
            lumen_model_speed.speed_key("gemini", "gemini-3.8-flash"), total_sec=2.0)
        route = [("gemini", "gemini-2.5-flash"), ("gemini", "gemini-3.8-flash")]
        ordered = lumen_routes._reorder_route_by_speed(route)  # без пина головы
        assert ordered[0][1] == "gemini-3.8-flash", "медленная голова тяжёлой цепочки не снята"
    finally:
        lumen_model_speed._latency_ema.clear()


# ── F-085 (адверсариальный проход): отмена в отправке возвращает неотданные чанки ──


def test_inline_tts_cancel_during_send_refunds_remaining(monkeypatch):
    incoming = _tts_incoming(998352)
    uid = bot._user_key_for_message(incoming)
    bot.GLOBAL_QUOTA["user_daily"] = {}
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    started = asyncio.Event()

    async def synth(text):
        return (b"fake-ogg", "speech.ogg", 1)

    async def hang_send(func, **kwargs):
        if "voice" in kwargs:
            started.set()
            await asyncio.Event().wait()
        return None

    async def scenario():
        task = asyncio.create_task(bot.inline_tts(incoming, "hello world"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0 and entry["tts"] == 0

    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", synth)
    monkeypatch.setattr(bot, "_tg_call", hang_send)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_voice=AsyncMock()))
    monkeypatch.setattr(bot, "_delete_message_quietly", AsyncMock())
    monkeypatch.setattr(lumen_status.StatusMessage, "start_action", AsyncMock(return_value=None))
    asyncio.run(scenario())


# ── F-077: после SOS размеры не ищем, GIL не держим на энтропии ──


def test_jpeg_scan_stops_at_scan_start():
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sos = b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    sof_in_entropy = b"\xff\xc0\x00\x11\x08\x00\x10\x00\x20\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    data = b"\xff\xd8" + app0 + sos + sof_in_entropy + b"\x00" * 64
    assert lumen_images.image_dimensions(data) is None, (
        "SOF после SOS — это энтропия, а не заголовок")


# ── F-078: правка сохраняет новые пропорции ──


@pytest.mark.parametrize(("dims", "expected"), [
    ((1248, 832), lumen_images._SIZES["3:2"]),
    ((832, 1248), lumen_images._SIZES["2:3"]),
    ((1344, 576), lumen_images._SIZES["21:9"]),
    ((896, 1120), lumen_images._SIZES["4:5"]),
])
def test_size_for_reference_keeps_supported_proportions(dims, expected):
    assert lumen_images.size_for_reference(dims) == expected


# ── F-036: бюджет, истёкший до первой попытки, — RouteBudgetExceededError ──


def test_run_route_budget_exhausted_before_first_attempt_raises_budget_error(monkeypatch):
    import time as _time

    async def fake_ask(*args, **kwargs):
        raise AssertionError("модель не должна зваться при истёкшем бюджете")

    monkeypatch.setattr(bot, "ask_munito_text", fake_ask)
    incoming = _FakeIncomingMessage(998381)
    with pytest.raises(bot.RouteBudgetExceededError):
        asyncio.run(bot._run_route(
            998381, "hi", [("munito", "probe/budget")], incoming,
            deadline=_time.monotonic() - 1.0,
        ))
    bot.chat_state.pop(998381, None)


# ── F-039/F-181 (проход №2): замороженный чат не вытесняется, ключ не сиротеет ──


def test_prune_keeps_frozen_chat_and_does_not_queue_deletion(monkeypatch):
    _reset_state_flags()
    lcs.chat_state.clear()
    lcs.chat_state[998391] = {"last_activity": 0}
    lcs.chat_state[998392] = {"last_activity": 0}
    lcs._chat_snapshot_unloaded.add(998391)
    monkeypatch.setattr(lcs, "PRUNED_CHAT_TARGET", 1)
    lcs._prune_old_chats()
    assert 998391 in lcs.chat_state, "замороженный чат вытеснен — id выпадет из индекса"
    assert 998392 not in lcs.chat_state
    assert 998391 not in lcs._pending_chat_deletions


def test_shutdown_flush_skips_all_deletes_on_load_failure():
    _reset_state_flags()
    lcs._state_load_failed = True
    lcs._pending_chat_deletions.update({998393, 998394})
    deleted = []

    def fake_delete(cid):
        deleted.append(cid)
        return True

    with patch("bot._delete_chat_storage", side_effect=fake_delete):
        lcs._flush_state_now()
    assert deleted == []
    assert {998393, 998394} <= lcs._pending_chat_deletions


# ── F-087 (проход №2): отсутствующий в снапшоте ключ не складывается сам с собой ──


def test_quota_recovery_does_not_double_count_absent_remote_keys(monkeypatch):
    today = bot._current_quota_day()
    remote = json.dumps({
        "gemini": {}, "user_daily": {}, "banned_users": {},
        "stats": {"answers_sent": 500}, "quota_day": today,
    })
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({
        "image": {"cf": {"used": 7, "neurons": 700.0, "exhausted_at": None, "cooldown_until": None}},
        "stats": {"answers_sent": 3},
        "quota_day": today,
    })
    lcs._quota_load_failed = True
    lq._quota_retry_last_ts = -1e9
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: remote)
    assert lq.recover_global_quota() is True
    assert bot.GLOBAL_QUOTA["image"]["cf"]["used"] == 7, "локальная копия сложилась сама с собой"
    assert bot.GLOBAL_QUOTA["image"]["cf"]["neurons"] == 700.0
    assert bot.GLOBAL_QUOTA["stats"]["answers_sent"] == 503


def test_quota_load_survives_infinity_in_user_stats_and_bans(monkeypatch):
    raw = json.dumps({
        "user_daily": {"1": {"total": float("inf")}},
        "stats": {"answers_sent": float("inf"), "model_attempts": 5},
        "banned_users": {"2": float("inf")},
        "quota_day": bot._current_quota_day(),
    })
    lcs._quota_load_failed = True
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: raw)
    lq.load_global_quota()
    assert lcs._quota_load_failed is False, "OverflowError не должен ронять всю загрузку"
    assert bot.GLOBAL_QUOTA["stats"]["model_attempts"] == 5


# ── F-033 (проход №2): ошибка запроса на 400 не остывает ──


def test_request_specific_400_does_not_cool_the_model():
    async def boom(payload, deadline):
        raise bot.MunitoAPIError("HTTP 400 File abc does not exist", status_code=400)

    model = "probe/file-missing"
    with pytest.raises(bot.MunitoAPIError):
        asyncio.run(lumen_routes._chat_completion_chain(
            [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}],
            [model], model, request_fn=boom, provider="munito",
        ))
    entry = bot.GLOBAL_QUOTA.get("munito", {}).get(model) or {}
    assert entry.get("cooldown_until") is None


# ── F-110: дедуп по паре «заголовок + текст», а не по одному заголовку ──


def test_dedupe_keeps_same_title_with_different_bodies():
    import lumen_search
    items = [
        {"title": "Курс", "url": "https://a/1", "snippet": "bitcoin: 1 USD"},
        {"title": "Курс", "url": "https://a/2", "snippet": "ethereum: 3000 USD"},
        {"title": "Курс", "url": "https://a/3", "snippet": "bitcoin: 1 USD"},
    ]
    kept = lumen_search._dedupe(items)
    assert [i["url"] for i in kept] == ["https://a/1", "https://a/2"]
