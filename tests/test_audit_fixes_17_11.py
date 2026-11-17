"""
Регрессии ревью 17.11.2026: маршрутизация, защита состояния, квоты, /tts.

Каждый тест падает на коде до соответствующего фикса и проходит после него.
"""
import asyncio
import copy
import json
import math
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
    (400, "model is no longer available"),
    (400, "This model has been shut down"),
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
        {"title": "Курс", "url": "https://example.com/1", "snippet": "bitcoin: 1 USD"},
        {"title": "Курс", "url": "https://example.org/2", "snippet": "ethereum: 3000 USD"},
        {"title": "Курс", "url": "https://example.net/3", "snippet": "bitcoin: 1 USD"},
    ]
    kept = lumen_search._dedupe(items)
    assert [i["url"] for i in kept] == ["https://example.com/1", "https://example.org/2"]


# ── F-113: невидимки и чужой маркер поиска не доходят до промпта ──


def test_snippet_one_line_strips_tag_chars_and_foreign_search_marker():
    import lumen_search
    text = "\U000e0041he\u2065llo \ufff9 [[SEARCH]]"
    clean = lumen_search._one_line(text)
    assert not any(0xE0000 <= ord(ch) <= 0xE007F for ch in clean)
    assert "\u2065" not in clean and "\ufff9" not in clean
    assert lumen_search.search_marker_requested(clean) is False


# ── F-033 (проход №3): файл, в имени которого есть «model», не остывает ──


@pytest.mark.parametrize("text", [
    "File model.png does not exist",
    "model output does not exist",
    "model response invalid",
])
def test_file_and_output_errors_do_not_cool_the_model(text):
    async def boom(payload, deadline):
        raise bot.MunitoAPIError(f"HTTP 400 {text}", status_code=400)

    model = "probe/no-cool"
    with pytest.raises(bot.MunitoAPIError):
        asyncio.run(lumen_routes._chat_completion_chain(
            [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}],
            [model], model, request_fn=boom, provider="munito",
        ))
    entry = bot.GLOBAL_QUOTA.get("munito", {}).get(model) or {}
    assert entry.get("cooldown_until") is None


# ── F-182 (новая, проход №3): Infinity в timestamp не роняет /stats ──


def test_stats_survives_infinite_last_write_ts(monkeypatch):
    monkeypatch.setattr(bot, "_last_storage_write_ts", lambda: float("inf"))
    text = lumen_commands._storage_backend_text()
    assert "записей пока не было" in text


def test_quota_load_drops_infinite_last_write_ts(monkeypatch):
    raw = json.dumps({
        "quota_day": bot._current_quota_day(),
        "last_storage_write_ts": float("inf"),
    })
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: raw)
    lq.load_global_quota()
    ts = bot.GLOBAL_QUOTA.get("last_storage_write_ts")
    assert ts is None or math.isfinite(float(ts))


# ── F-102: служебный адрес из RSS/keyed-выдачи не доходит ни до промпта, ни до пузыря ──


def test_private_url_is_dropped_from_snippets_and_dedupe():
    import lumen_search
    items = [
        {"title": "metadata", "url": "http://169.254.169.254/latest/meta-data", "snippet": "secret"},
        {"title": "ok", "url": "https://example.com/a", "snippet": "public"},
    ]
    kept = lumen_search._dedupe(items)
    assert [i["url"] for i in kept] == ["https://example.com/a"]
    block = lumen_search.format_snippets_for_prompt(items)
    assert "169.254.169.254" not in block
    assert "example.com" in block


def test_stats_survives_out_of_range_finite_timestamp(monkeypatch):
    monkeypatch.setattr(bot, "_last_storage_write_ts", lambda: 1e300)
    text = lumen_commands._storage_backend_text()
    assert "записей пока не было" in text


# ── F-072/F-073/F-178: непарные теги и LaTeX больше не квадратичны ──


def _finishes_within(fn, *args, seconds=2.0):
    result = []

    def run():
        result.append(fn(*args))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"{getattr(fn, '__name__', fn)} не уложился в {seconds}s"
    return result[0]


def test_unpaired_html_tags_are_linear():
    import lumen_formatting
    assert _finishes_within(lumen_formatting._md_to_html, "<b>x" * 15000) is not None
    assert _finishes_within(lumen_formatting._md_to_rich_html, "<b>x" * 15000) is not None


def test_unpaired_latex_delimiters_are_linear():
    import lumen_formatting
    assert _finishes_within(lumen_formatting._md_to_html, "\\[x" * 15000) is not None
    assert _finishes_within(lumen_formatting._md_to_rich_html, "\\[x" * 15000) is not None


def test_guest_truncation_of_adversarial_text_is_bounded():
    import lumen_formatting
    out = _finishes_within(lumen_formatting._truncate_html_to_fit, "<b>x" * 15000, 4096)
    assert isinstance(out, str)


# ── F-114 (часть): неудачный keyed-запрос не тратит кредит ──


def test_failed_keyed_request_does_not_spend_api_call(monkeypatch):
    import lumen_search
    monkeypatch.setenv("TAVILY_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SEARCH_API_PROVIDER", "tavily")
    spent = []
    monkeypatch.setattr(lumen_search, "note_search_api_call", lambda: spent.append(1))

    class _BrokenSession:
        def post(self, *args, **kwargs):
            raise RuntimeError("network down")

    out = asyncio.run(lumen_search._api_provider_snippets(
        _BrokenSession(), "q", 3, deadline=time.monotonic() + 5))
    assert out == []
    assert spent == [], "упавший запрос не должен списывать кредит"


# ── F-074: модерация распознаётся при 403/500 и за капом detail ──


@pytest.mark.parametrize("status", [403, 500])
def test_moderation_marker_behind_long_detail_is_terminal(status):
    import lumen_images
    long_detail = "x" * 500 + " Your output has been flagged by moderation"
    data = {"messages": [{"message": long_detail}], "success": False}
    with pytest.raises(lumen_images.ImageModerationError):
        lumen_images._cloudflare_image_result(status, None, data)


def test_generic_500_without_marker_is_provider_error():
    import lumen_images
    with pytest.raises(lumen_images.ImageProviderError) as exc:
        lumen_images._cloudflare_image_result(500, None, {"messages": [{"message": "x" * 500}]})
    assert not isinstance(exc.value, lumen_images.ImageModerationError)


# ── F-114: per-user кап keyed-поиска; владелец без лимита ──


def test_keyed_per_user_cap_and_owner_bypass(monkeypatch):
    import lumen_search as ls
    monkeypatch.setattr(bot, "OWNER_ID", 999)
    ls.reset_search_counters_for_tests()
    token = ls.set_search_actor(501)
    try:
        for _ in range(ls._search_api_user_limit()):
            ls.note_search_api_call()
        assert ls.search_api_user_budget_left() is False, "свой лимит исчерпан"
        token2 = ls.set_search_actor(502)
        try:
            assert ls.search_api_user_budget_left() is True, "чужой расход не должен мешать"
        finally:
            ls.reset_search_actor(token2)
        ls._search_api_calls = 999
        assert ls.search_api_budget_left() is False, "общий потолок действует"
        token3 = ls.set_search_actor(999)
        try:
            assert ls.search_api_budget_left() is True
            assert ls.search_api_user_budget_left() is True
            for _ in range(20):
                ls.note_search_api_call()
            assert ls.search_api_user_budget_left() is True
        finally:
            ls.reset_search_actor(token3)
    finally:
        ls.reset_search_actor(token)
        ls.reset_search_counters_for_tests()



# ── F-044: сводка памяти не теряет старые факты молча ──


def test_summary_consumes_from_the_start_and_keeps_leftover(monkeypatch):
    history = [
        {"role": "user", "content": f"MSG{i} " + "x" * 1000, "sender": "u", "ts": time.time()}
        for i in range(201)
    ]
    state = {"history": history, "memory_summary": "", "memory_pending": [], "memory_epoch": 0}
    captured = {}

    async def fake_summarize(text):
        captured["digest"] = text
        return "ok-summary"

    monkeypatch.setattr(lcs, "_summarize_text", fake_summarize)
    asyncio.run(lcs._trim_history(history, state))
    assert "MSG0 " in captured["digest"], "самые старые факты должны попасть в сводку"
    assert state["memory_pending"], "непросуммированный остаток обязан ждать следующего круга"
    assert state["memory_summary"] == "ok-summary"


# ── F-111: подпись DDG принадлежит своей ссылке, а не соседней ──


def test_ddg_snippet_is_paired_with_its_own_link():
    import lumen_search
    page = (
        '<div><a class="result__a" href="https://a.com/1">Title A</a></div>'
        '<div><a class="result__a" href="https://b.com/2">Title B</a>'
        '<a class="result__snippet">Snippet belonging to B</a></div>'
    )
    out = lumen_search.parse_ddg_html(page)
    assert out[0]["snippet"] == "", "у ссылки A подписи нет"
    assert out[1]["snippet"] == "Snippet belonging to B"


# ── F-081: подготовка /logs идёт в рабочем потоке ──


def test_logs_preparation_runs_off_event_loop(monkeypatch):
    import threading
    from aiogram.enums import ChatType
    import lumen_commands

    seen = {}

    def fake_build():
        seen["thread"] = threading.current_thread()
        return None

    monkeypatch.setattr(lumen_commands, "_build_logs_attachment", fake_build)
    monkeypatch.setattr(bot, "_is_owner", lambda uid: True)
    monkeypatch.setattr(bot, "_tg_call", AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=5),
        reply=AsyncMock(), reply_document=AsyncMock(),
    )
    asyncio.run(lumen_commands.cmd_logs(message))
    assert seen["thread"] is not threading.main_thread(), "чтение лога не должно идти в петле"


# ── F-082: забаненный не останавливает ответ ни командой, ни кнопкой ──


def test_stop_command_and_callback_ignore_banned_user(monkeypatch):
    import lumen_commands
    calls = []
    monkeypatch.setattr(bot, "_is_banned", lambda uid: True)
    monkeypatch.setattr(bot, "_request_stream_stop", lambda cid, sender: calls.append((cid, sender)))
    monkeypatch.setattr(bot, "STOP_COMMAND_ENABLED", True)
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    message = SimpleNamespace(chat=SimpleNamespace(id=1), from_user=SimpleNamespace(id=77))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=77),
        message=SimpleNamespace(chat=SimpleNamespace(id=1)),
        answer=AsyncMock(),
    )
    asyncio.run(lumen_commands.cmd_stop(message))
    asyncio.run(lumen_commands.handle_stop_callback(query))
    assert calls == [], "забаненный не должен управлять остановкой"


# ── F-080: смена языка под общим окном 5/30с ──


def test_lang_callback_is_rate_limited(monkeypatch):
    import lumen_commands
    answers = []
    writes = []
    monkeypatch.setattr(bot, "_is_banned", lambda uid: False)
    monkeypatch.setattr(bot, "_check_and_register_rate_limit", lambda key: True)
    monkeypatch.setattr(bot, "mark_state_dirty", lambda *a, **k: writes.append(1))
    query = SimpleNamespace(
        data="lang:en",
        from_user=SimpleNamespace(id=77),
        message=SimpleNamespace(chat=SimpleNamespace(id=1, type="private")),
        answer=AsyncMock(side_effect=lambda text=None, **kw: answers.append(text)),
    )
    asyncio.run(lumen_commands.handle_lang_callback(query))
    assert answers and answers[0] == bot._t(1, "rate_limited")
    assert writes == [], "язык не должен меняться при отказе по окну"


# ── F-079: /tts и /reset идут через лок чата ──


def test_chat_locked_runs_handler_under_the_lock(monkeypatch):
    from lumen_commands import chat_locked
    locked = []
    done = []

    async def fake_lock(message, run):
        locked.append(message)
        await run()

    async def handler(message):
        done.append(message)

    monkeypatch.setattr(bot, "_run_under_chat_lock", fake_lock)
    message = SimpleNamespace(chat=SimpleNamespace(id=1))
    asyncio.run(chat_locked(handler)(message))
    assert locked == [message] and done == [message]


# ── F-046: _deleted_chat_ids не растёт без капа ──


def test_deleted_chat_ids_are_capped(monkeypatch):
    _reset_state_flags()
    lcs.chat_state.clear()
    monkeypatch.setattr(lcs, "PRUNED_CHAT_TARGET", 1)
    monkeypatch.setattr(lcs, "_DELETED_CHAT_IDS_CAP", 3)
    for wave in range(10):
        lcs.chat_state[900000 + wave] = {"last_activity": 0}
        lcs.chat_state[800000 + wave] = {"last_activity": 0}
        lcs._prune_old_chats()
    assert len(lcs._deleted_chat_ids) <= 3


# ── F-104: DOCTYPE/ENTITY во всём теле RSS, а не в первых 2000 символах ──


def test_rss_guard_sees_doctype_after_long_prefix():
    import lumen_search
    xml = (
        "<!--" + "x" * 2100 + "-->"
        '<!DOCTYPE rss [<!ENTITY a "boom">]>'
        "<rss><channel><item><title>&a;</title>"
        "<link>https://example.com/1</link><description>&a;</description></item></channel></rss>"
    )
    assert lumen_search.parse_rss_snippets(xml) == []


# ── F-108/F-109: ранг до капа издателя, authority по границам домена ──


def test_publisher_cap_is_applied_after_ranking():
    import lumen_search
    stale = [{"title": f"Old {i}", "url": f"https://reuters.com/o{i}",
              "publisher": "reuters.com", "date": "2020-01-01", "kind": "web"} for i in range(3)]
    fresh = [{"title": f"New {i}", "url": f"https://reuters.com/n{i}",
              "publisher": "reuters.com", "date": "2026-11-17", "kind": "web"} for i in range(2)]
    merged = lumen_search._merge_web_groups(
        [stale + fresh], wants_fresh=True, wants_entity=False, wants_game_wiki=False)
    assert {item["title"] for item in merged} == {"New 0", "New 1"}


def test_authority_uses_domain_boundaries():
    import lumen_search
    assert lumen_search._authority({"publisher": "reuters.com"}) == 2
    assert lumen_search._authority({"publisher": "news.reuters.com"}) == 2
    assert lumen_search._authority({"publisher": "reuters.com.evil.tld"}) == 0


# ── F-089: нулевая метка не блокирует первый сброс суток ──


def test_first_quota_day_reset_is_not_throttled_on_fresh_runner(monkeypatch):
    import lumen_quotas as lq
    original = lq._last_quota_check_monotonic
    try:
        lq._last_quota_check_monotonic = 0.0
        bot.GLOBAL_QUOTA["quota_day"] = "2000-01-01"
        bot.GLOBAL_QUOTA.setdefault("gemini", {})["m1"] = {
            "used": 50, "exhausted_at": 1.0, "cooldown_until": None}
        monkeypatch.setattr(lq.time, "monotonic", lambda: 5.0)
        lq._reset_quota_if_new_day()
        assert bot.GLOBAL_QUOTA["gemini"]["m1"]["used"] == 0
    finally:
        lq._last_quota_check_monotonic = original


# ── F-107: протухшее не выдаётся за свежее ──


def test_rank_does_not_fall_back_to_stale_items():
    import lumen_search
    stale = [{"title": "Old", "url": "https://example.com/old",
              "publisher": "example.com", "date": "2020-01-01", "kind": "web"}]
    assert lumen_search._rank(stale, wants_fresh=True, wants_entity=False) == []


# ── F-088: вытеснение не сносит израсходованные записи и bonus ──


def test_lru_eviction_keeps_spent_and_bonus_entries(monkeypatch):
    import lumen_quotas as lq
    monkeypatch.setattr(lq, "MAX_USER_DAILY_KEYS", 3)
    bucket = {
        "111": {"total": 30, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0},
        "222": {"total": 0, "gemini": 0, "tts": 0, "draw": 0, "bonus": 5},
        "333": {"total": 0, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0},
        "444": {"total": 0, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0},
        "555": {"total": 0, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0},
    }
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    bot.GLOBAL_QUOTA[lq.USER_DAILY_KEY] = bucket
    lq._user_daily_entry(999)
    assert "111" in bucket and "222" in bucket, "расход и bonus не вытесняются"
    assert not {"333", "444", "555"} & set(bucket)
    assert "999" in bucket


# ── F-106: погода не выходит за общий дедлайн ──


def test_weather_sequence_respects_the_deadline(monkeypatch):
    import time as _time
    import lumen_search

    class _Resp:
        status = 200

        async def text(self):
            return '{"results": [{"latitude": 1, "longitude": 2, "name": "X"}]}'

        def raise_for_status(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _SlowSession:
        def get(self, *args, **kwargs):
            delay = kwargs.get("timeout")
            total = getattr(delay, "total", 0.2) if delay is not None else 0.2

            class _CM:
                async def __aenter__(self_inner):
                    await asyncio.sleep(min(0.6, max(0.05, total)))
                    return _Resp()

                async def __aexit__(self_inner, *exc):
                    return False

            return _CM()

    monkeypatch.setattr(lumen_search, "_cities_in", lambda text: ["X"])
    started = _time.monotonic()
    asyncio.run(lumen_search._weather_snippets(_SlowSession(), "погода в X", _time.monotonic() + 1.0))
    assert _time.monotonic() - started < 1.6, "последовательность вышла за дедлайн"


# ── Medium-пакет 1: F-055, F-059, F-103, F-052 ──


def test_draw_budget_treats_zero_as_exhausted():
    import lumen_message_core as mc
    assert mc._draw_budget_seconds(0.0) == 1.0
    assert mc._draw_budget_seconds(None) == bot.DRAW_TOTAL_BUDGET_SEC


def test_history_service_regex_requires_a_closed_marker():
    import lumen_message_parse as mp
    content = "[Web search results без скобки и мой текст"
    entry = mp._clean_history_entry({"role": "user", "content": content})
    assert entry is not None and entry["content"] == content


@pytest.mark.parametrize("url", [
    "http://127.1/", "http://0177.0.0.1/", "http://foo.localhost/",
    "http://127.0.0.1.nip.io/",
])
def test_private_host_forms_are_rejected(url):
    import lumen_search
    assert lumen_search.url_host_is_public(url) is False


def test_normal_public_host_is_accepted():
    import lumen_search
    assert lumen_search.url_host_is_public("https://example.com/a") is True


def test_log_safe_url_hides_the_account():
    import lumen_tiktok
    assert "@private_person" not in lumen_tiktok._log_safe_url(
        "https://www.tiktok.com/@private_person/video/1")


# ── F-112: алиасы по границам слов ──


def test_aliases_respect_word_boundaries():
    import lumen_search
    assert lumen_search._cities_in("какие модели лучше") == []
    assert lumen_search._alias_present("eth", "we are together") is False
    assert lumen_search._alias_present("eth", "цена eth растёт") is True


# ── F-095: неудачное чтение не списывает личный кап ──


def test_failed_read_does_not_spend_user_quota(monkeypatch):
    import lumen_read_page as rp
    monkeypatch.setattr(rp, "_user_key", lambda: "u1")
    rp.reset_read_page_counters_for_tests()
    rp.note_read_page_call(count_user=False)
    assert rp.read_page_calls_today() == 1
    assert rp._USER_READS.get("u1", 0) == 0
    rp.note_read_page_call(count_global=False)
    assert rp._USER_READS["u1"] == 1
    assert rp.read_page_calls_today() == 1
    rp.reset_read_page_counters_for_tests()


# ── F-064: история на обрыве/стопе проходит тот же скраб ──


def test_stream_history_answer_is_scrubbed():
    import lumen_streaming as ls
    cleaned = ls._history_safe_answer("User Safety: safe\n\nОтвет")
    assert "User Safety" not in cleaned
    assert "Ответ" in cleaned
