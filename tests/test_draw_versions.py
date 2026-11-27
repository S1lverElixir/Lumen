"""Image versions: restoring a previous version without a provider call or a
    slot charge.

Protects parity with Undo/Redo in ChatGPT (10.11.2026): \"restore the previous
version\" must resend the finished photo rather than draw a new one; a canceled
version is restored through redo; another user group image cannot be
restored; /reset forgets version history.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot


def _photo_message(mid=10):
    return SimpleNamespace(
        message_id=mid,
        photo=[SimpleNamespace(file_id=f"fid{mid}", file_unique_id=f"u{mid}", width=8, height=8, file_size=8)],
    )


def _versions_state(n=3, user=456):
    return {
        "draw_versions": [
            {"file_id": f"fid{i}", "mime": "image/jpeg", "prompt": f"кот {i}", "user": user, "ts": time.time()}
            for i in range(n)
        ],
        "last_drawn_user": user,
        "last_drawn_message_id": 99,
        "last_draw_prompt": f"кот {n - 1}",
    }


@pytest.fixture
def revert_env(monkeypatch):
    sent = []

    async def fake_tg_call(method, *args, **kwargs):
        sent.append(kwargs)
        if "photo" in kwargs:
            return _photo_message(500 + len(sent))
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(
        send_photo=AsyncMock(), edit_message_reply_markup=AsyncMock(),
    ))
    monkeypatch.setattr(bot, "_safe_reply", AsyncMock())
    monkeypatch.setattr(bot, "_save_media_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bot, "mark_state_dirty", lambda cid: None)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=999500, type=bot.ChatType.PRIVATE),
        from_user=SimpleNamespace(id=456), message_id=1,
    )
    return message, sent


def test_revert_resends_previous_photo_without_provider(revert_env, monkeypatch):
    message, sent = revert_env
    state = _versions_state(3)
    provider_calls = []

    async def fake_provider(*a, **k):
        provider_calls.append(a)
        return b"img"

    monkeypatch.setattr(bot, "_cloudflare_text_to_image", fake_provider)
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_provider)
    assert asyncio.run(bot.revert_draw_version(message, state)) is True
    assert provider_calls == []
    photos = [kw for kw in sent if "photo" in kw]
    assert len(photos) == 1
    assert photos[0]["photo"] == "fid1"
    assert state["draw_versions"][-1]["file_id"] == "fid1"
    assert state["draw_redo"][-1]["file_id"] == "fid2"
    assert state["last_draw_prompt"] == "кот 1"
    # The keyboard was removed from the former anchor instead of being left on two photos.
    stripped = [kw for kw in sent if kw.get("message_id") == 99]
    assert stripped and stripped[0]["reply_markup"].inline_keyboard == []


def test_revert_state_survives_cancellation_during_keyboard_strip(revert_env, monkeypatch):
    # R3-03 (audit 26.11.2026): the photo is already in the chat, and cancellation happened during removal
    # of the old keyboard; the state must be persisted before this await,
    # otherwise the visible frame survives with stale versions/anchor.
    import asyncio as _aio
    message, sent = revert_env
    state = _versions_state(3)

    real_tg = bot._tg_call

    async def cancelling_tg_call(method, *args, **kwargs):
        if "photo" in kwargs:
            return await real_tg(method, *args, **kwargs)
        raise _aio.CancelledError

    monkeypatch.setattr(bot, "_tg_call", cancelling_tg_call)
    try:
        asyncio.run(bot.revert_draw_version(message, state))
        raised = False
    except _aio.CancelledError:
        raised = True
    assert raised, "отмена из обработчика не должна глотаться"
    photos = [kw for kw in sent if "photo" in kw]
    assert len(photos) == 1
    assert state["draw_versions"][-1]["file_id"] == "fid1"
    assert state["draw_redo"][-1]["file_id"] == "fid2"
    assert state["last_drawn_message_id"] == 501  # the anchor is moved to the returned photo


def test_revert_unknown_delivery_does_not_commit_state(revert_env, monkeypatch):
    # R3-06 (I23 review, 26.11.2026): a false _TG_UNKNOWN on send — delivery
    # unknown; rollback must not commit versions and the anchor, or
    # last_drawn_message_id will point to a message that may not exist.
    message, sent = revert_env
    state = _versions_state(3)
    before_v = list(state["draw_versions"])
    before_r = list(state.get("draw_redo") or [])

    async def unknown_tg_call(method, *args, **kwargs):
        sent.append(kwargs)
        if "photo" in kwargs:
            return bot._TG_UNKNOWN
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    assert asyncio.run(bot.revert_draw_version(message, state)) is False
    assert state["draw_versions"] == before_v
    assert list(state.get("draw_redo") or []) == before_r
    assert state.get("last_drawn_message_id") == 99

def test_redo_returns_the_undone_version(revert_env):
    message, sent = revert_env
    state = _versions_state(3)
    asyncio.run(bot.revert_draw_version(message, state))
    assert asyncio.run(bot.revert_draw_version(message, state, redo=True)) is True
    photos = [kw["photo"] for kw in sent if "photo" in kw]
    assert photos == ["fid1", "fid2"]
    assert state["draw_redo"] == []


def test_revert_without_history_is_a_localized_refusal(revert_env):
    message, sent = revert_env
    state = _versions_state(1)
    assert asyncio.run(bot.revert_draw_version(message, state)) is False
    assert [kw for kw in sent if "photo" in kw] == []
    bot._safe_reply.assert_awaited_once()
    assert bot._safe_reply.await_args.args[1] == bot._t(999500, "draw_no_versions")


def test_revert_of_someone_elses_picture_is_refused(revert_env):
    message, sent = revert_env
    state = _versions_state(3, user=777)
    assert asyncio.run(bot.revert_draw_version(message, state)) is False
    assert [kw for kw in sent if "photo" in kw] == []
    assert bot._safe_reply.await_args.args[1] == bot._t(999500, "pick_not_yours")


def test_failed_resend_keeps_the_version_lists(revert_env, monkeypatch):
    message, sent = revert_env

    async def failing_tg_call(method, *args, **kwargs):
        sent.append(kwargs)
        return None

    monkeypatch.setattr(bot, "_tg_call", failing_tg_call)
    state = _versions_state(3)
    before = [v["file_id"] for v in state["draw_versions"]]
    assert asyncio.run(bot.revert_draw_version(message, state)) is False
    assert [v["file_id"] for v in state["draw_versions"]] == before
    assert state.get("draw_redo") in (None, [])


@pytest.mark.parametrize("failure", ["sentinel", "json"])
def test_draw_unknown_delivery_keeps_slot_and_no_duplicate(monkeypatch, failure):
    # R4-03 (audit 26.11.2026): unknown delivery is not None. On None, draw returned
    # the purchased slot and forwarded the same photo as a document; both a timeout
    # and a post-response decode failure must keep the slot without a duplicate.
    chat_id = 999504
    uid = 777504

    async def fake_provider(session, prompt, timeout_sec=None, **_):
        return b"\x89PNG fake bytes"

    monkeypatch.setattr(bot, "image_provider_names", lambda: ("pollinations",))
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_provider)
    monkeypatch.setattr(bot, "_is_owner", lambda uid: False)
    sent = []
    if failure == "sentinel":
        monkeypatch.setattr(bot, "bot", SimpleNamespace(send_photo=AsyncMock(), send_document=AsyncMock()))

        async def unknown_tg_call(method, *args, **kwargs):
            sent.append(kwargs)
            return bot._TG_UNKNOWN

        monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    else:
        import json

        class _DecodeFailingBot:
            async def send_photo(self, **kwargs):
                sent.append(kwargs)
                raise json.JSONDecodeError("Expecting value", "<html>", 0)

            async def send_document(self, **kwargs):
                sent.append(kwargs)
                return SimpleNamespace(message_id=1)

        monkeypatch.setattr(bot, "bot", _DecodeFailingBot())
        monkeypatch.setattr(bot, "_tg_proxy_breaker", bot._TelegramProxyCircuitBreaker(
            cooldown_sec=20.0, trip_threshold=5,
        ))
    saved_day = bot.GLOBAL_QUOTA.get("quota_day")
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    bot.GLOBAL_QUOTA.setdefault("user_daily", {})[str(uid)] = {
        "total": 0, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0,
    }
    incoming = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE),
        from_user=SimpleNamespace(id=uid), message_id=1, text="кот",
        reply=AsyncMock(), reply_to_message=None,
    )
    try:
        assert asyncio.run(bot.inline_draw(incoming, "кот в куртке")) is True
        assert all("document" not in kw for kw in sent), "документ-дубль после unknown-исхода"
        assert len([kw for kw in sent if "photo" in kw]) == 1
        entry = bot.GLOBAL_QUOTA["user_daily"][str(uid)]
        assert entry["draw"] == 1 and entry["total"] == 1, "слот возвращён за возможную доставку"
    finally:
        bot.GLOBAL_QUOTA["quota_day"] = saved_day
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(chat_id, None)


def test_generation_records_versions_and_caps_at_five(monkeypatch):
    chat_id = 999501
    uid = 777501

    async def fake_provider(session, prompt, timeout_sec=None, **_):
        return b"\x89PNG fake bytes"

    monkeypatch.setattr(bot, "image_provider_names", lambda: ("pollinations",))
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_provider)
    # Check the version cap, not the quota: the owner is exempt, or the sixth drawing
    # would hit the daily limit, and the test would measure the wrong thing.
    monkeypatch.setattr(bot, "_is_owner", lambda uid: True)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_photo=AsyncMock()))

    async def fake_tg_call(method, *args, **kwargs):
        if "photo" in kwargs:
            return _photo_message(600 + len(bot.get_state(chat_id).get("draw_versions", [])))
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    incoming = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE),
        from_user=SimpleNamespace(id=uid), message_id=1, text="кот",
        reply=AsyncMock(), reply_to_message=None,
    )
    try:
        for i in range(7):
            assert asyncio.run(bot.inline_draw(incoming, f"кот {i}")) is True
        versions = bot.get_state(chat_id)["draw_versions"]
        assert len(versions) == 5
        assert [v["prompt"] for v in versions] == [f"кот {i}" for i in range(2, 7)]
        assert all(v["file_id"] for v in versions)
    finally:
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)


def test_draw_cancellation_after_send_keeps_marks_and_slot(monkeypatch):
    # R3-02 (audit 26.11.2026): cancellation during the network await for removing the old keyboard
    # must not consume a slot or erase anchors of an already sent photo: markers and
    # mark_state_dirty are written before this await, while the return sees sent_ok.
    chat_id = 999503
    uid = 777503
    bot.chat_state[chat_id] = {
        "last_drawn_message_id": 777, "last_drawn_user": uid,
        "last_draw_prompt": "старый",
    }
    refunds = []
    dirty = []

    async def fake_provider(session, prompt, timeout_sec=None, **_):
        return b"\x89PNG fake bytes"

    monkeypatch.setattr(bot, "image_provider_names", lambda: ("pollinations",))
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_provider)
    monkeypatch.setattr(bot, "_is_owner", lambda uid: True)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(
        send_photo=AsyncMock(), edit_message_reply_markup=AsyncMock(),
    ))

    async def fake_tg_call(method, *args, **kwargs):
        if "photo" in kwargs:
            return _photo_message(700)
        if kwargs.get("message_id") == 777:
            raise asyncio.CancelledError()  # cancellation while removing the old keyboard
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "_refund_user_daily", lambda _uid, **kw: refunds.append(kw))
    monkeypatch.setattr(bot, "_save_media_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bot, "mark_state_dirty", lambda cid: dirty.append(cid))
    incoming = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE),
        from_user=SimpleNamespace(id=uid), message_id=1, text="кот",
        reply=AsyncMock(), reply_to_message=None,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot.inline_draw(incoming, "кот"))
        state = bot.get_state(chat_id)
        # Anchors and version are written BEFORE cancellation: the next 'another variant' stays live.
        assert state["last_draw_prompt"] == "кот"
        assert state["last_drawn_message_id"] == 700
        assert state["draw_versions"][-1]["prompt"] == "кот"
        assert dirty == [chat_id]
        # Do not return the slot: the photo was delivered and generation was paid for (sent_ok).
        assert refunds and refunds[0]["total"] == 0
    finally:
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)


def test_reset_forgets_versions(monkeypatch):
    # /reset clears chat memory: afterward, "restore the previous version" must not
    # fetch a picture from forgotten history (parity 10.11.2026).
    chat_id = 999502
    state = _versions_state(3)
    state["draw_redo"] = [{"file_id": "x"}]
    state["history"] = [{"role": "user", "content": "hi"}]
    bot.chat_state[chat_id] = state
    incoming = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE),
        from_user=SimpleNamespace(id=456), message_id=1, reply=AsyncMock(),
    )
    monkeypatch.setattr(bot, "bot", SimpleNamespace(edit_message_reply_markup=AsyncMock()))
    monkeypatch.setattr(bot, "_is_privileged_in_chat", AsyncMock(return_value=True))
    monkeypatch.setattr(bot, "_tg_call", AsyncMock(return_value=SimpleNamespace()))
    try:
        asyncio.run(bot.cmd_reset(incoming))
        assert "draw_versions" not in bot.chat_state[chat_id]
        assert "draw_redo" not in bot.chat_state[chat_id]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_core_routes_revert_instead_of_drawing(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "верни предыдущий вариант"
    state = _versions_state(3)
    monkeypatch.setattr(bot, "get_state", lambda cid: state)
    reverted = []

    async def fake_revert(msg, st, *, redo=False):
        reverted.append(redo)
        return True

    monkeypatch.setattr(bot, "revert_draw_version", fake_revert)
    asyncio.run(bot._handle_message_core(message))
    assert reverted == [False]
    bot.inline_draw.assert_not_awaited()


def test_core_routes_redo(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "верни обратно"
    state = _versions_state(3)
    monkeypatch.setattr(bot, "get_state", lambda cid: state)
    reverted = []

    async def fake_revert(msg, st, *, redo=False):
        reverted.append(redo)
        return True

    monkeypatch.setattr(bot, "revert_draw_version", fake_revert)
    asyncio.run(bot._handle_message_core(message))
    assert reverted == [True]


def test_versions_survive_serialize_and_restore():
    # REGRESSION (review 10.11.2026): the serializer wrote only last_draw*,
    # so after the very first deploy, "restore the previous version" replied
    # "no versions", although the images remained in the chat.
    import lumen_chat_state
    import lumen_state_storage
    cid = 999560
    state = _versions_state(3)
    state["draw_redo"] = [{"file_id": "fidR", "mime": "image/jpeg", "prompt": "кот R", "user": 456, "ts": 1.0}]
    snapshot = lumen_state_storage._serialize_chat_state(state)
    assert [v["file_id"] for v in snapshot["draw_versions"]] == ["fid0", "fid1", "fid2"]
    lumen_chat_state.chat_state.pop(cid, None)
    lumen_chat_state._restore_single_chat(cid, snapshot)
    restored = lumen_chat_state.chat_state[cid]
    assert [v["file_id"] for v in restored["draw_versions"]] == ["fid0", "fid1", "fid2"]
    assert [v["file_id"] for v in restored["draw_redo"]] == ["fidR"]
    lumen_chat_state.chat_state.pop(cid, None)


def test_restore_drops_broken_version_entries():
    # A snapshot could come from an old schema version or be corrupt: lists() must not
    # turn out not to be dictionaries, otherwise revert will fail on target["file_id"].
    import lumen_chat_state
    cid = 999561
    lumen_chat_state.chat_state.pop(cid, None)
    lumen_chat_state._restore_single_chat(cid, {
        "draw_versions": [{"file_id": "ok"}, "мусор", {"prompt": "без file_id"}, 7],
        "draw_redo": None,
    })
    restored = lumen_chat_state.chat_state[cid]
    assert [v["file_id"] for v in restored["draw_versions"]] == ["ok"]
    assert restored["draw_redo"] == []
    lumen_chat_state.chat_state.pop(cid, None)


def test_revert_keeps_author_of_the_restored_version(revert_env, monkeypatch):
    # The frame author owns the context: otherwise the owner returning someone else's version
    # passed weak edit verbs to the wrong person (review 10.11.2026).
    message, _sent = revert_env
    monkeypatch.setattr(bot, "_is_owner", lambda uid: True)
    state = _versions_state(3, user=456)
    state["last_drawn_user"] = 777
    asyncio.run(bot.revert_draw_version(message, state))
    assert state["last_drawn_user"] == 456


def test_redo_without_undo_stack_answers_no_versions(revert_env):
    # Redo with an empty canceled stack must not return an image from
    # history baskets or crash; the person hears an honest answer (review 10.11.2026).
    message, sent = revert_env
    state = _versions_state(2)
    assert asyncio.run(bot.revert_draw_version(message, state, redo=True)) is False
    assert not sent
    # Specifically the "no versions" key, not an adjacent refusal (review 17.11.2026).
    assert bot._safe_reply.await_args.args[1] == bot._t(999500, "draw_no_versions")


def test_failed_redo_send_keeps_the_lists(revert_env, monkeypatch):
    # A failed redo forward must not consume a version: the lists stay as they were.
    message, _sent = revert_env

    async def failing_tg_call(method, *args, **kwargs):
        return None

    state = _versions_state(3)
    assert asyncio.run(bot.revert_draw_version(message, state)) is True
    monkeypatch.setattr(bot, "_tg_call", failing_tg_call)
    before_versions = [v["file_id"] for v in state["draw_versions"]]
    before_redo = [v["file_id"] for v in state["draw_redo"]]
    assert asyncio.run(bot.revert_draw_version(message, state, redo=True)) is False
    assert [v["file_id"] for v in state["draw_versions"]] == before_versions
    assert [v["file_id"] for v in state["draw_redo"]] == before_redo


def test_revert_consumes_no_daily_slot(revert_env, monkeypatch):
    # Docstring promise: the return costs neither a person slot nor budget
    # a provider. Without the check, a refactor could start charging it.
    message, _sent = revert_env
    state = _versions_state(3)
    calls = []
    monkeypatch.setattr(bot, "_reserve_user_daily", lambda *a, **k: calls.append("reserve"))
    monkeypatch.setattr(bot, "_image_budget_exhausted", lambda: calls.append("budget") or False)
    bot.GLOBAL_QUOTA.setdefault("user_daily", {}).pop("456", None)
    asyncio.run(bot.revert_draw_version(message, state))
    assert calls == []
    assert "456" not in bot.GLOBAL_QUOTA.get("user_daily", {})


def test_revert_with_trailing_words_goes_to_text(rate_guard_setup, monkeypatch):
    # "restore the previous translation version" — about text. The version resolver must
    # yield, otherwise a letter after the trigger turned a text request into
    # forwarding the image (review 10.11.2026).
    message = rate_guard_setup()
    message.text = "верни предыдущий вариант перевода"
    state = _versions_state(3)
    monkeypatch.setattr(bot, "get_state", lambda cid: state)
    reverted = []

    async def fake_revert(msg, st, *, redo=False):
        reverted.append(redo)
        return True

    monkeypatch.setattr(bot, "revert_draw_version", fake_revert)
    run_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", run_route)
    asyncio.run(bot._handle_message_core(message))
    assert reverted == []
    run_route.assert_awaited()


def test_revert_without_versions_does_not_swallow_the_message(rate_guard_setup, monkeypatch):
    # In a chat where nothing has been drawn, "bring back the previous one" is an ordinary request
    # model: the versions branch is not allowed to answer "no versions".
    message = rate_guard_setup()
    message.text = "верни предыдущий"
    reverted = []

    async def fake_revert(msg, st, *, redo=False):
        reverted.append(redo)
        return True

    monkeypatch.setattr(bot, "revert_draw_version", fake_revert)
    run_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", run_route)
    asyncio.run(bot._handle_message_core(message))
    assert reverted == []
    run_route.assert_awaited()


def test_core_revert_does_not_also_run_the_text_route(rate_guard_setup, monkeypatch):
    # Coverage review: without return after revert, the message also went to the model,
    # one 'undo' consumed a text-provider answer.
    message = rate_guard_setup()
    message.text = "верни предыдущий вариант"
    state = _versions_state(3)
    monkeypatch.setattr(bot, "get_state", lambda cid: state)

    async def fake_revert(msg, st, *, redo=False):
        return True

    monkeypatch.setattr(bot, "revert_draw_version", fake_revert)
    run_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", run_route)
    asyncio.run(bot._handle_message_core(message))
    run_route.assert_not_awaited()


def test_revert_after_politeness_still_reverts(rate_guard_setup, monkeypatch):
    # "return the previous version, please" is still a return: previously
    # a polite word turned the request into a text response (review 10.11.2026).
    for text in ("верни предыдущий вариант, пожалуйста", "верни картинку", "undo please"):
        message = rate_guard_setup()
        message.text = text
        state = _versions_state(3)
        monkeypatch.setattr(bot, "get_state", lambda cid: state)
        reverted = []

        async def fake_revert(msg, st, *, redo=False):
            reverted.append(redo)
            return True

        monkeypatch.setattr(bot, "revert_draw_version", fake_revert)
        run_route = AsyncMock(return_value=("ok", False))
        monkeypatch.setattr(bot, "_run_route", run_route)
        asyncio.run(bot._handle_message_core(message))
        assert reverted == [False], text
        run_route.assert_not_awaited()
