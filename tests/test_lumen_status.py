"""
test_lumen_status.py — живой статус-плейсхолдер: строки этапов и таймер ожидания.

Что защищают: вместо голых точек пользователь видит этапы обработки
(«Поиск в интернете · нашёл N», «Думаю… N с»), а сам пузырь затем
становится ответом. Удалённое сообщение не должно ломать рендер.

Запуск:
    pytest tests/test_lumen_status.py -v
"""
import asyncio

import bot
from lumen_status import StatusMessage
from tests.bot_test_helpers import _FakeIncomingMessage


async def _passthrough(method, *args, **kwargs):
    return await method(*args, **kwargs)


def test_status_message_renders_done_lines_and_timer(monkeypatch):
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999900)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        assert status is not None
        status.add_done("progress_search_done", count=5)
        status.set_sources(["example.com", "example.com", "second.example"])
        await status.set_phase("progress_thinking", timed=True)
        await status.finish()
        return status

    status = asyncio.run(scenario())
    sent = incoming.sent[0]
    assert status.message is sent
    text = sent.edits[-1][0]
    assert "✓ Searched the web · found 5" in text
    assert "Thinking… 0s" in text
    html_block, plain_block = status.activity_prefix()
    assert html_block.startswith("<blockquote expandable>") and html_block.endswith("</blockquote>")
    assert "Thought for 0s" in plain_block
    assert "✓ Searched the web · found 5" in plain_block
    assert plain_block.count("· example.com") == 1
    assert "· second.example" in plain_block
    # Таблицы показываются rich-сообщением: блок-цитата вернула бы None.
    assert status.compose_answer("| a | b |\n|---|---|\n| 1 | 2 |")[0] is None
    # Язык статуса — это код языка, а не chat_id: строка "en" не должна
    # попасть в chat_state как ключ (прод 28.10.2026).
    assert "en" not in bot.chat_state
    bot.chat_state.pop(999900, None)


def test_simple_thought_activity_is_italic_not_quote(monkeypatch):
    # Простое ожидание без поиска/медиа — лёгкая курсивная строка, не цитата.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999902)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_thinking", timed=True)
        await status.finish()
        return status

    status = asyncio.run(scenario())
    html_block, plain = status.activity_prefix()
    assert html_block.startswith("<i>") and "blockquote" not in html_block
    assert "Thought for" in plain
    bot.chat_state.pop(999902, None)


def test_media_stage_becomes_done_and_thinking_timer(monkeypatch):
    # Фото/видео скачаны раньше вызова модели: этап закрывается галочкой,
    # а в статусе тикает «Думаю… N с», как в Claude (ревью 28.10.2026).
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999903)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_viewing_image")
        await status.mark_stage_done()
        await status.finish()
        return status

    status = asyncio.run(scenario())
    live = incoming.sent[0].edits[-1][0]
    assert "✓ Looking at the image" in live
    assert "Thinking… 0s" in live
    _, plain = status.activity_prefix()
    assert "✓ Looking at the image" in plain
    assert "Thought for" in plain
    bot.chat_state.pop(999903, None)


def test_initial_status_never_calls_audio_or_documents_a_video():
    # Голосовое и PDF раньше показывали «Смотрю видео» — ложный этап.
    from lumen_message_core import _initial_status
    assert _initial_status(False, False, "image/jpeg", False) == ("progress_viewing_image", False)
    assert _initial_status(False, False, "video/mp4", False) == ("progress_watching_video", False)
    assert _initial_status(False, False, "audio/ogg", False) == ("progress_thinking", True)
    assert _initial_status(False, False, "application/pdf", False) == ("progress_thinking", True)
    assert _initial_status(False, False, None, True) == ("progress_searching", False)


def test_action_status_keeps_animation_and_live_seconds(monkeypatch):
    # TikTok/рисование/озвучка: одна анимированная строка с секундами,
    # смена фразы не откатывает счётчик (ревью 28.10.2026).
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999904)

    async def scenario():
        status = await StatusMessage.start_action(incoming, "en", "Processing the TikTok link")
        await status.set_action("Taking a little longer")
        await status.finish()
        return status

    status = asyncio.run(scenario())
    assert status.message is incoming.sent[0]
    live = incoming.sent[0].edits[-1][0]
    assert "Taking a little longer" in live
    assert "0s" in live
    assert live[0] in ("✻", "✽", "·", "✢", "✳", "✶")
    bot.chat_state.pop(999904, None)


def test_retry_phase_is_not_a_completed_step(monkeypatch):
    # Прод 28.10.2026: фолбэк «пробую ещё раз» попадал в итоговый блок как
    # выполненный шаг и обнулял таймер «Думал N с».
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999901)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        await status.set_phase("progress_thinking", timed=True)
        await asyncio.sleep(0.01)
        await status.set_phase("progress_retry")
        await status.finish()
        return status

    status = asyncio.run(scenario())
    _, plain = status.activity_prefix()
    assert "Trying again" not in plain
    assert "Thought for" in plain
    bot.chat_state.pop(999901, None)
