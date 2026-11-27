"""test_lumen_status.py covers the live status placeholder: stage lines and the wait timer.

What it protects: instead of bare dots, the user sees processing stages
("Searching the web · found N", "Thinking… Ns"), and the bubble then becomes the answer.
A deleted message must not break rendering.

Run:
    pytest tests/test_lumen_status.py -v
"""
import asyncio

import bot
import lumen_status
import pytest
from lumen_status import StatusMessage, discard_status_start
from tests.bot_test_helpers import _FakeIncomingMessage, _FakeSentMessage


async def _passthrough(method, *args, **kwargs):
    return await method(*args, **kwargs)




class _FakeClock:
    """Test clock: sleep is replaced with a no-op, and \"Thinking for N s\" is calculated from monotonic time — without this, every check involving seconds would depend on the actual test run duration."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


def _fixed_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(lumen_status, "time", clock)
    return clock

def test_status_message_renders_done_lines_and_timer(monkeypatch):
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999900)

    clock = _fixed_clock(monkeypatch)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        assert status is not None
        markup = object()
        status.reply_markup = markup
        status.add_done("progress_search_done", count=5)
        status.set_sources(["example.com", "example.com", "second.example"])
        await status.set_phase("progress_thinking", timed=True)
        clock.advance(3)
        await status.finish()
        return status

    status = asyncio.run(scenario())
    sent = incoming.sent[0]
    assert status.message is sent
    assert all(kw.get("reply_markup") is status.reply_markup for kw in sent.edit_kwargs)
    text = sent.edits[-1][0]
    assert "✓ Searched the web · found 5" in text
    # At 0 s, the live line has no number (smoke 11.11.2026: previously "Thinking… 0 seconds").
    assert "Thinking…" in text and "0 seconds" not in text
    html_block, plain_block = status.activity_prefix()
    assert html_block.startswith("<blockquote expandable>") and html_block.endswith("</blockquote>")
    assert "Thought for 3 seconds" in plain_block
    assert "✓ Searched the web · found 5" in plain_block
    assert plain_block.count("· example.com") == 1
    assert "· second.example" in plain_block
    # Tables are shown as a rich message: a block quote would return None.
    assert status.compose_answer("| a | b |\n|---|---|\n| 1 | 2 |")[0] is None
    assert status.compose_answer_with_reason("| a | b |\n|---|---|\n| 1 | 2 |")[2] == "table"
    # The status language is a language code, not chat_id: the string "en" must not
    # into chat_state as a key (prod 28.10.2026).
    assert "en" not in bot.chat_state
    bot.chat_state.pop(999900, None)


def test_simple_thought_activity_is_italic_not_quote(monkeypatch):
    # Plain waiting without search/media is a light italic line, not a quote.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999902)

    clock = _fixed_clock(monkeypatch)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_thinking", timed=True)
        clock.advance(2)
        await status.finish()
        return status

    status = asyncio.run(scenario())
    html_block, plain = status.activity_prefix()
    assert html_block.startswith("<i>") and "blockquote" not in html_block
    assert "Thought for 2 seconds" in plain
    bot.chat_state.pop(999902, None)


def test_media_stage_becomes_done_and_thinking_timer(monkeypatch):
    # Photos/videos were downloaded before the model call: the stage closes with a checkmark,
    # while the status ticks “Thinking… N s,” like Claude (review 28.10.2026).
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999903)

    clock = _fixed_clock(monkeypatch)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_viewing_image")
        await status.mark_stage_done()
        clock.advance(2)
        await status.finish()
        return status

    status = asyncio.run(scenario())
    live = incoming.sent[0].edits[-1][0]
    assert "✓ Viewed the image" in live
    assert "Thinking…" in live and "0 seconds" not in live
    _, plain = status.activity_prefix()
    assert "✓ Viewed the image" in plain
    assert "Thought for 2 seconds" in plain
    bot.chat_state.pop(999903, None)


def test_initial_status_never_calls_audio_or_documents_a_video():
    # Voice notes and PDFs previously showed 'Watching video', a false stage.
    from lumen_message_core import _initial_status
    assert _initial_status(False, False, "image/jpeg", False) == ("progress_viewing_image", False)
    assert _initial_status(False, False, "video/mp4", False) == ("progress_watching_video", False)
    assert _initial_status(False, False, "audio/ogg", False) == ("progress_thinking", True)
    assert _initial_status(False, False, "application/pdf", False) == ("progress_thinking", True)
    assert _initial_status(False, False, None, True) == ("progress_searching", False)


def test_action_status_keeps_animation_and_live_seconds(monkeypatch):
    # TikTok/drawing/TTS: one animated line with seconds,
    # changing the phrase does not roll back the counter (review 28.10.2026).
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
    # At 0 s, the action counter is removed entirely, without printing "· 0 seconds"
    # (smoke 11.11.2026).
    assert "0 seconds" not in live
    import lumen_status
    assert live[0] in lumen_status._TRIO_FRAMES
    bot.chat_state.pop(999904, None)


def test_start_action_unknown_send_leaves_no_status(monkeypatch):
    # Review I21 (26.11.2026): an unknown send result is a sentinel, not a Message.
    # status.message must be a real message: tick and set_action call
    # edit_text directly, and the sentinel would raise AttributeError in a background task.
    incoming = _FakeIncomingMessage(999907)

    async def unknown_send(reply_to, text, **kwargs):
        return bot._TG_UNKNOWN

    monkeypatch.setattr(lumen_status, "_send_status_message", unknown_send)
    status = asyncio.run(StatusMessage.start_action(incoming, "en", "Processing the TikTok link"))
    assert status is None
    bot.chat_state.pop(999907, None)


def test_start_unknown_send_leaves_no_status(monkeypatch):
    # I21 round 2 review (26.11.2026): the same boundary as start_action, but on
    # the normal search/answer status: a sentinel in status.message would deprive the stream
    # the placeholder and leave an unrenderable bubble.
    incoming = _FakeIncomingMessage(999908)

    async def unknown_send(reply_to, text, **kwargs):
        return bot._TG_UNKNOWN

    monkeypatch.setattr(lumen_status, "_send_status_message", unknown_send)
    status = asyncio.run(StatusMessage.start(incoming, "en", "progress_searching"))
    assert status is None
    bot.chat_state.pop(999908, None)


def test_status_delete_is_idempotent_across_route_and_handler_cleanup(monkeypatch):
    # Both the route and external handler clean the status on error/cancellation. The second
    # the call must not delete the message again and must not crash the task.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999905)
    deleted = []

    async def count_delete(msg):
        deleted.append(msg)
        return True

    monkeypatch.setattr(bot, "_delete_message_quietly", count_delete)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        await status.delete()
        await status.delete()
        return status

    status = asyncio.run(scenario())
    assert deleted == [status.message]
    bot.chat_state.pop(999905, None)


def test_discard_status_start_waits_for_inflight_send(monkeypatch):
    # Cancellation after sending but before Message is returned loses message_id and leaves
    # the bubble forever. Cleanup must wait for the bounded _tg_call.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999906)
    deleted = []

    async def count_delete(msg):
        deleted.append(msg)
        return True

    monkeypatch.setattr(bot, "_delete_message_quietly", count_delete)
    status = StatusMessage(incoming, "en")

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_start(reply_to, lang, key, *, timed=False):
            started.set()
            await release.wait()
            return status

        task = asyncio.create_task(fake_start(incoming, "en", "progress_searching"))
        await started.wait()
        cleanup = asyncio.create_task(discard_status_start(task))
        await asyncio.sleep(0)
        assert cleanup.done() is False
        release.set()
        await cleanup
        return task

    task = asyncio.run(scenario())
    assert task.cancelled() is False
    assert deleted == [status.message]
    bot.chat_state.pop(999906, None)


def test_status_start_cancellation_deletes_already_sent_message(monkeypatch):
    # Cancellation during a direct await of status_task must not lose message_id:
    # the send itself waits for _tg_call and deletes the message already accepted by Telegram.
    incoming = _FakeIncomingMessage(999908)
    sent = _FakeSentMessage()
    deleted = []

    async def count_delete(msg):
        deleted.append(msg)
        return True

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_send(method, *args, **kwargs):
            started.set()
            await release.wait()
            return sent

        monkeypatch.setattr(bot, "_tg_call", blocked_send)
        monkeypatch.setattr(bot, "_delete_message_quietly", count_delete)
        task = asyncio.create_task(StatusMessage.start(incoming, "en", "progress_searching"))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("StatusMessage.start was expected to be cancelled")

    asyncio.run(scenario())
    assert deleted == [sent]
    bot.chat_state.pop(999908, None)


def test_status_delete_can_retry_after_cancellation(monkeypatch):
    # The completion flag is set before await, making repeated deletion impossible if cleanup
    # canceled between stopping the ticker and deleting the message.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999907)
    deleted = []
    attempts = 0

    async def flaky_delete(msg):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        deleted.append(msg)
        return True

    monkeypatch.setattr(bot, "_delete_message_quietly", flaky_delete)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        try:
            await status.delete()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the first cleanup was expected to be cancelled")
        await status.delete()
        return status

    status = asyncio.run(scenario())
    assert deleted == [status.message]
    bot.chat_state.pop(999907, None)


def test_status_delete_retries_after_telegram_failure(monkeypatch):
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999909)
    attempts = 0

    async def flaky_delete(msg):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    monkeypatch.setattr(bot, "_delete_message_quietly", flaky_delete)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        await status.delete()
        assert status._deleted is False
        await status.delete()
        return status

    status = asyncio.run(scenario())
    assert attempts == 2
    assert status._deleted is True
    bot.chat_state.pop(999909, None)


def test_retry_phase_is_not_a_completed_step(monkeypatch):
    # Prod 28.10.2026: the "trying again" fallback reached the final block as
    # completed step and reset the "Thinking for N s" timer.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999901)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_searching")
        await status.set_phase("progress_thinking", timed=True)
        await asyncio.sleep(0.01)
        await status.set_phase("progress_retry")
        clock.advance(2)
        await status.finish()
        return status

    clock = _fixed_clock(monkeypatch)
    status = asyncio.run(scenario())
    _, plain = status.activity_prefix()
    assert "Trying again" not in plain
    assert "Thought for 2 seconds" in plain
    bot.chat_state.pop(999901, None)

def test_progress_strings_plural_and_zero_across_languages():
    # Smoke regression 11.11.2026: "Thought for 1 seconds" and a live
    # "Thinking... 0 seconds". Only en and zu decline; ru remains with
    # abbreviating “s”; compact packs are not touched by the test.
    from lumen_lang import t
    assert t("en", "progress_thought_for", sec=1) == "Thought for 1 second"
    assert t("en", "progress_thought_for", sec=2) == "Thought for 2 seconds"
    assert t("en", "progress_thought_for", sec=5) == "Thought for 5 seconds"
    assert t("en", "progress_thinking", sec=1) == "Thinking… 1 second"
    assert t("en", "progress_seconds", sec=1) == "1 second"
    assert t("en", "progress_seconds", sec=5) == "5 seconds"
    # Zero: the phrase has no number in either the expanded or compact pack.
    assert t("en", "progress_thinking", sec=0) == "Thinking…"
    assert t("ru", "progress_thinking", sec=0) == "Думаю…"
    assert t("ru", "progress_thinking", sec=1) == "Думаю… 1 с"
    assert t("ru", "progress_thinking", sec=2) == "Думаю… 2 с"
    assert t("ru", "progress_thought_for", sec=5) == "Думал 5 с"
    assert t("zu", "progress_thought_for", sec=1) == "Ngicabange umzuzwana ongu-1"
    assert t("zu", "progress_thought_for", sec=5) == "Ngicabange imizuzwana engu-5"
    assert t("uz", "progress_thinking", sec=0) == "O'ylayapman…"


def test_live_thinking_line_no_number_at_zero_singular_at_one(monkeypatch):
    # The same regression at the live-status level: ticker frames at 0 and 1 s.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999906)
    clock = _fixed_clock(monkeypatch)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_thinking", timed=True)
        # _render — exactly what the ticker writes between edits; set_phase
        # resets the phase at the same times and always produces a zero frame.
        zero = status._render(0)
        clock.advance(1)
        one = status._render(1)
        await status.finish()
        return zero, one

    zero, one = asyncio.run(scenario())
    assert zero.endswith("Thinking…") and "0" not in zero
    assert one.endswith("Thinking… 1 second") and "1 seconds" not in one
    bot.chat_state.pop(999906, None)


def test_subsecond_answer_shows_no_thought_line(monkeypatch):
    # Claude does not write "Thought for 0 seconds". Review 11.11.2026: the activity line
    # under 1 second should disappear entirely, not print zero.
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999905)
    _fixed_clock(monkeypatch)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_thinking", timed=True)
        await status.finish()
        return status

    status = asyncio.run(scenario())
    assert status.activity_prefix() == ("", "")
    html_block, plain_block = status.compose_answer("ответ")
    assert html_block is None and plain_block == "ответ"
    bot.chat_state.pop(999905, None)
def test_status_from_message_keeps_existing_stop_button():
    from types import SimpleNamespace

    markup = object()
    message = SimpleNamespace(reply_markup=markup, chat=SimpleNamespace(id=999907))

    async def scenario():
        status = StatusMessage.from_message(message, "en", "progress_searching")
        assert status.reply_markup is markup
        await status.stop()

    asyncio.run(scenario())


def test_status_resume_restarts_stopped_ticker():
    status = StatusMessage(message=object(), lang="en")
    status._stopped = True

    async def tick():
        await asyncio.Future()

    status._tick = tick

    async def scenario():
        status.resume()
        assert status._stopped is False
        assert status._task is not None
        await status.stop()

    asyncio.run(scenario())
    assert status._task is None


def test_status_clear_reply_markup_removes_keyboard(monkeypatch):
    monkeypatch.setattr(bot, "_tg_call", _passthrough)
    incoming = _FakeIncomingMessage(999908)

    async def scenario():
        status = await StatusMessage.start(incoming, "en", "progress_thinking", timed=True)
        status.reply_markup = object()
        await status.clear_reply_markup()
        await status.stop()
        return status

    status = asyncio.run(scenario())
    assert status.reply_markup is None
    assert status.message.markup_edits[-1]["reply_markup"].inline_keyboard == []
    bot.chat_state.pop(999908, None)


def test_discard_status_start_deletes_unclaimed_status_once():
    from lumen_status import discard_status_start

    class FakeStatus:
        delete_count = 0

        async def delete(self):
            self.delete_count += 1

    status = FakeStatus()

    async def start():
        return status

    async def scenario():
        task = asyncio.create_task(start())
        await asyncio.sleep(0)
        await discard_status_start(task)

    asyncio.run(scenario())
    assert status.delete_count == 1


def test_done_stage_phrase_is_past_tense_in_every_language():
    # Review 11.11.2026: the closed stage used the same key as the live one, and in 25
    # languages showed "✓ Viewing image" instead of the completed action.
    import lumen_lang
    pairs = {
        "progress_viewing_image": "progress_image_done",
        "progress_watching_video": "progress_video_done",
        "progress_reading_link": "progress_link_done",
    }
    for live_key, done_key in pairs.items():
        for lang in lumen_lang.SUPPORTED_LANGS:
            live = lumen_lang.t(lang, live_key)
            done = lumen_lang.t(lang, done_key)
            assert done != live, (lang, live_key, done)
            assert done != done_key and live != live_key, (lang, done)


def test_search_done_count_agrees_in_singular():
    # Review 11.11.2026: “1 resultados”, “1 trouvés”, “1 trovati” — the counter
    # is a noun, and singular is needed for inflected languages.
    import lumen_lang
    assert lumen_lang.t("es", "progress_search_done", count=1) == "Búsqueda en internet · 1 resultado"
    assert lumen_lang.t("fr", "progress_search_done", count=1) == "Recherche web · 1 trouvé"
    assert lumen_lang.t("it", "progress_search_done", count=1) == "Ricerca web · 1 trovato"
    assert lumen_lang.t("en", "progress_search_done", count=1) == "Searched the web · found 1"


@pytest.mark.parametrize("answer", [
    "x" * 4096,
    "**" + "x" * 4080 + "**",
    "😀" * 2047,
])
def test_activity_prefix_never_pushes_answer_over_telegram_limit(answer):
    # R5-06 (audit 26.11.2026): chunking checks the body before activity is added.
    # At the boundary, the prefix is disabled, while the answer remains complete and valid.
    from lumen_formatting import _md_to_html, _tg_len
    status = StatusMessage(message=object(), lang="en")
    status._thought_seconds = 2
    html, plain, reason = status.compose_answer_with_reason(answer)
    assert html is None
    assert plain == answer
    assert reason == "overflow"
    assert _tg_len(_md_to_html(answer)) <= bot.TG_MAX_LEN


def test_activity_prefix_is_kept_when_composed_answer_fits():
    from lumen_formatting import _tg_len
    status = StatusMessage(message=object(), lang="en")
    status._thought_seconds = 2
    html_prefix, plain_prefix = status.activity_prefix()
    room = min(bot.TG_MAX_LEN - _tg_len(html_prefix) - 2,
               bot.TG_MAX_LEN - _tg_len(plain_prefix) - 2)
    answer = "x" * room
    html, plain, reason = status.compose_answer_with_reason(answer)
    assert reason == "composed"
    assert html is not None and html.startswith("<i>")
    assert plain.startswith(plain_prefix)
    assert _tg_len(html) <= bot.TG_MAX_LEN
    assert _tg_len(plain) <= bot.TG_MAX_LEN


def test_status_send_disables_link_previews(monkeypatch):
    from aiogram.types import LinkPreviewOptions
    incoming = _FakeIncomingMessage(999910)
    captured = {}

    async def capture(method, *args, **kwargs):
        captured.update(kwargs)
        return await method(*args, **kwargs)

    monkeypatch.setattr(bot, "_tg_call", capture)
    asyncio.run(lumen_status._send_status_message(incoming, "https://example.com"))
    assert captured["link_preview_options"] == LinkPreviewOptions(is_disabled=True)
