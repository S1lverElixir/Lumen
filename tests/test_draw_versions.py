"""Версии картинки: возврат к предыдущей без провайдера и без списания слота.

Защищает паритет с Undo/Redo в ChatGPT (10.11.2026): «верни предыдущий вариант»
обязан переслать уже готовое фото, а не рисовать новое; отменённая версия
возвращается через redo; чужую картинку в группе вернуть нельзя; /reset
забывает историю версий.
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
    # Клавиатура снята с прежнего якоря, а не оставлена на двух фото.
    stripped = [kw for kw in sent if kw.get("message_id") == 99]
    assert stripped and stripped[0]["reply_markup"].inline_keyboard == []


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


def test_generation_records_versions_and_caps_at_five(monkeypatch):
    chat_id = 999501
    uid = 777501

    async def fake_provider(session, prompt, timeout_sec=None, **_):
        return b"\x89PNG fake bytes"

    monkeypatch.setattr(bot, "image_provider_names", lambda: ("pollinations",))
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_provider)
    # Кап версий проверяем, а не квоту: владелец exempt, иначе шестой рисунок
    # упёрся бы в дневной лимит и тест мерил бы не то.
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


def test_reset_forgets_versions(monkeypatch):
    # /reset стирает память чата: после него «верни предыдущий вариант» не должен
    # доставать картинку из забытой истории (паритет 10.11.2026).
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
