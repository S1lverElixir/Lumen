"""
Регрессии ревью 17.11.2026: маршрутизация, защита состояния, квоты, /tts.

Каждый тест падает на коде до соответствующего фикса и проходит после него.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import lumen_chat_state as lcs
import lumen_commands
import lumen_images
import lumen_model_speed
import lumen_quotas as lq
import lumen_router_config
import lumen_routes
import lumen_state_storage as lss
import lumen_status
import pytest
from tests.bot_test_helpers import _FakeIncomingMessage


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
    chat_id = 998301
    state = bot.get_state(chat_id)
    state["memory_summary"] = "MEMFACT-7731: любит чай"
    payloads = []

    async def fake_request(payload, deadline):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    try:
        answer, _ = asyncio.run(lumen_routes._chat_completion_chain(
            [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}],
            ["probe/mem-model"], "probe/mem-model",
            request_fn=fake_request, provider="munito", chat_id=chat_id,
        ))
    finally:
        bot.chat_state.pop(chat_id, None)
    assert answer == "ok"
    assert "MEMFACT-7731" in payloads[0]["messages"][0]["content"]


# ── F-032: перестановка по скорости не ломает голову search-цепочки ──


def test_search_chain_keeps_grounded_head_after_speed_reorder():
    lumen_model_speed._latency_ema.clear()
    try:
        lumen_model_speed.record_response(
            lumen_model_speed.speed_key("gemini", "gemini-2.5-flash"), total_sec=90.0)
        for model in (
            "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
            "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
            "gemini-3-flash-preview",
        ):
            lumen_model_speed.record_response(
                lumen_model_speed.speed_key("gemini", model), total_sec=3.0)
        route = [("gemini", m) for m in lumen_router_config.GEMINI_SEARCH_CHAIN]
        ordered = lumen_routes._reorder_route_by_speed(route)
        head = ordered[0][1]
        assert lumen_router_config.GEMINI_MODELS.get(head, {}).get("search_grounding") is True, (
            f"голова search-цепочки {head} без search grounding")
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


@pytest.mark.parametrize("raw", ["inf", "1e309", "-inf"])
def test_retry_after_rejects_non_finite(raw):
    resp = SimpleNamespace(headers={"Retry-After": raw})
    assert lumen_images._retry_after_sec(resp) is None


def test_image_rate_limit_cooldown_is_bounded():
    provider = "probe-image"
    bot.GLOBAL_QUOTA.setdefault(lq.IMAGE_QUOTA_PROVIDER, {}).pop(provider, None)
    try:
        lq._note_image_rate_limited(provider, retry_after_sec=float("inf"))
        entry = bot._quota_entry(lq.IMAGE_QUOTA_PROVIDER, provider)
        until = entry["cooldown_until"]
        assert until is not None and until < float("inf")
        assert until <= time.time() + 86400 + 60  # не дальше суток (плюс запас на время теста)
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


def test_quota_recovery_merges_live_bans_and_counters(monkeypatch):
    today = bot._current_quota_day()
    remote = json.dumps({
        "gemini": {"m1": {"used": 0, "exhausted_at": None, "cooldown_until": None}},
        "user_daily": {}, "banned_users": {}, "quota_day": today,
    })
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({"gemini": {}, "quota_day": today})
    lcs._quota_load_failed = True
    bot._ban_user(998331)
    bot._record_user_daily(998332)
    bot._record_user_daily(998332)
    lq._quota_retry_last_ts = -1e9
    monkeypatch.setattr(bot, "_storage_read_text", lambda key, path: remote)
    try:
        ok = lq.recover_global_quota()
        assert ok is True
        assert bot._is_banned(998331) is True, "бан, поставленный во время сбоя, не теряется"
        entry = bot._user_daily_peek(998332)
        assert entry is not None and entry["total"] >= 2, "счётчики сливаются по максимуму"
    finally:
        lcs._quota_load_failed = False
        lq._quota_retry_last_ts = 0.0
        bot.GLOBAL_QUOTA.get("banned_users", {}).pop("998331", None)
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("998332", None)


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


def test_inline_tts_cancel_during_synthesis_refunds_slots(monkeypatch):
    incoming = _tts_incoming(998341)
    uid = bot._user_key_for_message(incoming)
    bot.GLOBAL_QUOTA["user_daily"] = {}
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    started = asyncio.Event()

    async def hang(chunk):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(bot.inline_tts(incoming, "hello world"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = bot._user_daily_peek(uid)
        assert entry is None or (entry["total"] == 0 and entry["tts"] == 0)

    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", hang)
    monkeypatch.setattr(lumen_status.StatusMessage, "start_action", AsyncMock(return_value=None))
    try:
        asyncio.run(scenario())
    finally:
        bot.GLOBAL_QUOTA["user_daily"] = {}


def test_inline_tts_cancel_during_status_refunds_slots(monkeypatch):
    incoming = _tts_incoming(998342)
    uid = bot._user_key_for_message(incoming)
    bot.GLOBAL_QUOTA["user_daily"] = {}
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    started = asyncio.Event()

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
        assert entry is None or (entry["total"] == 0 and entry["tts"] == 0)

    monkeypatch.setattr(lumen_status.StatusMessage, "start_action", hang_status)
    try:
        asyncio.run(scenario())
    finally:
        bot.GLOBAL_QUOTA["user_daily"] = {}


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
