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


def test_versions_survive_serialize_and_restore():
    # РЕГРЕССИЯ (ревью 10.11.2026): сериализатор писал только last_draw*,
    # поэтому после первого же деплоя «верни предыдущий вариант» отвечал
    # «версий нет», хотя картинки в чате остались.
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
    # Снимок мог прийти из старой версии схемы или битым: в lists() не должно
    # оказаться не-словарей, иначе revert упадёт на target["file_id"].
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
    # Владелец контекста — автор кадра: иначе возврат чужой версии владельцем
    # передавал слабые глаголы правки не тому человеку (ревью 10.11.2026).
    message, _sent = revert_env
    monkeypatch.setattr(bot, "_is_owner", lambda uid: True)
    state = _versions_state(3, user=456)
    state["last_drawn_user"] = 777
    asyncio.run(bot.revert_draw_version(message, state))
    assert state["last_drawn_user"] == 456


def test_redo_without_undo_stack_answers_no_versions(revert_env):
    # Redo при пустой отменённой стопке не должен выдавать картинку из
    # history-корзинки или падать: человек слыжит честный ответ (ревью 10.11.2026).
    message, sent = revert_env
    state = _versions_state(2)
    assert asyncio.run(bot.revert_draw_version(message, state, redo=True)) is False
    assert not sent
    assert bot._safe_reply.await_args is not None


def test_failed_redo_send_keeps_the_lists(revert_env, monkeypatch):
    # Неудачная пересылка redo не должна съедать версию: списки остаются как были.
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
    # Обещание из docstring: возврат не платит ни слотом человека, ни бюджетом
    # провайдера. Без проверки его могли бы начать списывать при рефакторе.
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
    # «верни предыдущий вариант перевода» — про текст. Резолвер версий обязан
    # уступить, иначе буква после триггера превращала текстовую просьбу в
    # пересылку картинки (ревью 10.11.2026).
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
    # В чате, где ничего не рисовали, «верни предыдущий» — обычная просьба
    # модели: ветка версий не имеет права отвечать «версий нет».
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
    # Ревью покрытия: без return после revert сообщение уезжало ещё и модели,
    # то есть один «верни» тратил ответ текстового провайдера.
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
