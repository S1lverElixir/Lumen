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
        await status.set_phase("progress_thinking", timed=True)
        await status.stop()
        return status

    status = asyncio.run(scenario())
    sent = incoming.sent[0]
    assert status.message is sent
    text = sent.edits[-1][0]
    assert "Searched the web · found 5" in text
    assert "Thinking… 0s" in text
    # Язык статуса — это код языка, а не chat_id: строка "en" не должна
    # попасть в chat_state как ключ (прод 28.10.2026).
    assert "en" not in bot.chat_state
    bot.chat_state.pop(999900, None)
