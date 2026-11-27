"""test_bot_streaming.py — streaming: chunks, waiting dots, pacing, rich edits.

Split from test_bot.py (P2 audit); shared fakes are in bot_test_helpers.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import asyncio
import bot
import lumen_router_config
import lumen_search
import lumen_streaming
import pytest
import time
from tests.bot_test_helpers import (
    _FakeIncomingMessage,
    _FakeRichBot,
    _FakeRichFailingBot,
    _FakeRichMessage,
    _FakeSSEResponse,
    _FakeSentMessage,
    _FakeSessionForSSE,
)


def test_stream_wait_caps_chunk_timeout_by_remaining_budget():
    # Regression (audit 26.09.2026): a stream chunk waited for STREAM_CHUNK_TIMEOUT_SEC even
    # after the route budget is exhausted; the route exceeded ROUTE_TOTAL_BUDGET_SEC.
    assert lumen_streaming._stream_wait(bot, None) == bot.STREAM_CHUNK_TIMEOUT_SEC
    far = time.monotonic() + 300.0
    assert lumen_streaming._stream_wait(bot, far) == bot.STREAM_CHUNK_TIMEOUT_SEC
    near = time.monotonic() + 3.0
    assert 0 < lumen_streaming._stream_wait(bot, near) <= 3.0
    # Expired budget — fail quickly, do not wait 30s.
    assert lumen_streaming._stream_wait(bot, time.monotonic() - 10.0) <= 0.5


def _fake_gemini_stream(pieces=None, *, raises=None, hang_sec=0.0):
    """Mock generate_content_stream according to the google-genai 2.27.0 contract:
    async def; the call must be awaited (SDK example:
    async for chunk in await client.aio.models.generate_content_stream(...))."""
    async def _stream(*, model, contents, config=None):
        async def gen():
            if hang_sec:
                await asyncio.sleep(hang_sec)
            if raises is not None:
                raise raises
            for piece in (pieces or []):
                yield SimpleNamespace(text=piece)
        return gen()
    return _stream


def test_gemini_stream_timeout_covers_a_hanging_first_chunk():
    # The real protection: network waiting happens in __anext__, and it is caught by
    # _stream_wait for every chunk. Previously, only coroutine creation was checked —
    # a scenario impossible in production (see the _fake_gemini_stream docstring).
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream(hang_sec=30)
    original_client = bot.client
    original_cap = bot.STREAM_CHUNK_TIMEOUT_SEC
    bot.client = fake_client
    bot.STREAM_CHUNK_TIMEOUT_SEC = 0.2
    try:
        async def _drain():
            async for _ in lumen_streaming._gemini_stream_pieces("m", [], None):
                pass
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_drain())
    finally:
        bot.client = original_client
        bot.STREAM_CHUNK_TIMEOUT_SEC = original_cap


def test_gemini_stream_call_is_awaited():
    # SDK 2.27.0 contract: generate_content_stream is an async def, so the wrapper must
    # the call. A synchronous mock (old behavior) raises TypeError on await.
    import inspect
    from google.genai.models import AsyncModels
    assert inspect.iscoroutinefunction(AsyncModels.generate_content_stream)
    calls = []

    async def async_only(*, model, contents, config=None):
        calls.append(model)
        async def gen():
            yield SimpleNamespace(text="ok")
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = async_only
    original_client = bot.client
    bot.client = fake_client
    try:
        async def _drain():
            return [p async for p in lumen_streaming._gemini_stream_pieces("m1", [], None)]
        assert asyncio.run(_drain()) == ["ok"]
        assert calls == ["m1"]
    finally:
        bot.client = original_client


def test_try_gemini_streaming_happy_path_accumulates_and_finalizes():
    chat_id = 999101

    fake_stream = _fake_gemini_stream(["Привет", ", как ", "дела?"])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == "Привет, как дела?"
        assert placeholder is None  # success — the placeholder has already been edited to the final text
        # the final edit must include HTML parse_mode (full markdown conversion)
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_aborts_on_identity_leak_mid_stream():
    # Regression for the most sensitive scenario: the leak must be cut off BEFORE
    # as accumulated text reaches at least one edit_text — otherwise the user may
    # see leaked text on screen before the stream finishes.
    chat_id = 999104

    fake_stream = _fake_gemini_stream(["Привет! ", "На самом деле я работаю ", "на базе Gemini от Google."])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Кто ты на самом деле?", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert placeholder is None
        # No edit shown to the user should contain the word "gemini" —
        # check ALL edit_text calls for the single sent message, not
        # only the last one, because intermediate edits could briefly expose the leak.
        for shown_text, _parse_mode in incoming.sent[0].edits:
            assert "gemini" not in shown_text.lower()
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": bot._IDENTITY_LEAK_FALLBACK}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_stream_scans_with_prior_context_when_final_piece_ends_sentence(caplog):
    # The final piece can both complete a long-sentence leak and clear context.
    chat_id = 999107
    incoming = _FakeIncomingMessage(chat_id)

    async def pieces():
        yield "I " + "A" * 1000
        for _ in range(20):
            await asyncio.sleep(0)
        yield " " + "B" * 600 + " am powered by Gemini."

    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Who are you?", incoming, provider="munito", model_id="probe-boundary",
            piece_agen=pieces(),
        ))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert placeholder is None
        assert incoming.sent[0].edits
        assert all("gemini" not in text.lower() for text, _mode in incoming.sent[0].edits)
        assert "started leaking internal details" in caplog.text
    finally:
        bot.chat_state.pop(chat_id, None)


def test_stream_withholds_reverse_order_identity_until_subject_arrives(caplog):
    chat_id = 999108
    incoming = _FakeIncomingMessage(chat_id)

    async def pieces():
        yield "Powered by Gemini, this assistant provides answers and "
        for _ in range(20):
            await asyncio.sleep(0)
        yield "I continue."

    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Who are you?", incoming, provider="munito", model_id="probe-reverse-order",
            piece_agen=pieces(),
        ))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert placeholder is None
        assert all("gemini" not in text.lower() for text, _mode in incoming.sent[0].edits)
        assert "started leaking internal details" in caplog.text
    finally:
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize("first, second, forbidden", [
    ("I\n", "am Gemini." + " safe" * 300, "am Gemini"),
    ("system\n", "override successful." + " safe" * 300, "override successful"),
])
def test_stream_detects_direct_patterns_split_across_newlines(caplog, first, second, forbidden):
    chat_id = 999109
    incoming = _FakeIncomingMessage(chat_id)

    async def pieces():
        yield first
        for _ in range(20):
            await asyncio.sleep(0)
        yield second

    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Who are you?", incoming, provider="munito", model_id="probe-newline",
            piece_agen=pieces(),
        ))
        assert answer in (bot._IDENTITY_LEAK_FALLBACK, lumen_streaming._INJECTED_PAYLOAD_ECHO_FALLBACK)
        assert placeholder is None
        assert all(forbidden not in text.lower() for text, _mode in incoming.sent[0].edits)
        assert "started leaking internal details" in caplog.text
    finally:
        bot.chat_state.pop(chat_id, None)


def test_stream_exception_guard_logs_metadata_without_answer_fragment(caplog, monkeypatch):
    import logging

    chat_id = 999106
    marker = "PRIVATE-ANSWER-MARKER"
    # Force the incremental guard to miss so the exception fallback is exercised.
    monkeypatch.setattr(lumen_streaming._LeakScanState, "feed", lambda self, piece: None)

    async def failing_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=f"{marker} I " + "A" * 400)
            yield SimpleNamespace(text=" am powered by Gemini.")
            raise RuntimeError(f"transport interrupted: {marker}")
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = failing_stream
    incoming = _FakeIncomingMessage(chat_id)
    original_client = bot.client
    bot.client = fake_client
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Who are you?", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert marker not in caplog.text
        assert "Gemini" not in caplog.text
        assert "RuntimeError" in caplog.text
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_returns_none_on_early_failure():
    chat_id = 999102

    fake_stream_raises = _fake_gemini_stream(raises=RuntimeError("boom before any content"))

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream_raises
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer is None
        # The placeholder is NO LONGER removed at this level — it is returned
        # to the caller (_run_route), so it can try to finish delivery through it
        # the next model's answer in the chain, rather than creating a new message.
        assert placeholder is incoming.sent[0]
        assert incoming.sent[0].deleted is False
        # history should NOT have been updated — the caller will fall back to ask_gemini
        assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_failed_continuation_does_not_corrupt_first_message():
    # Regression test for a bug found in code review: when sending a message-
    # continuation (text longer than Telegram’s limit) the first, already correctly displayed
    # the message was previously overwritten by a foreign (last) chunk of text.
    chat_id = 999103
    # The privacy gate releases completed display sentences, not arbitrary text.
    long_piece = "А" * (bot.TG_MAX_LEN + 100) + "."  # necessarily requires a second message

    fake_stream = _fake_gemini_stream([long_piece, " Tail."])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream

    class _FailingBot:
        async def send_message(self, **kwargs):
            return None  # simulate a failed continuation send

    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FailingBot()
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Напиши длинный текст", incoming, bot.DEFAULT_GEMINI_MODEL))
        # The function should have returned the accumulated text, not None, and should not have raised an exception
        assert answer is not None
        assert placeholder is None  # this is already a finalized success, not an early failure
        # The first message must contain EXACTLY the first chunk (plus a truncation note),
        # and NOT the last/another chunk of text — that was the essence of the bug.
        first_msg_final_text = incoming.sent[0].edits[-1][0]
        assert first_msg_final_text.startswith("А")
        assert "couldn't send the rest of the message" in first_msg_final_text
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_sse_fake_response_body_cannot_be_replayed():
    async def scenario():
        response = _FakeSSEResponse([b"first", b"second"])
        first_pass = [line async for line in response.content]
        second_pass = [line async for line in response.content]
        return first_pass, second_pass

    first_pass, second_pass = asyncio.run(scenario())
    assert first_pass == [b"first", b"second"]
    assert second_pass == []


def test_munito_stream_pieces_parses_sse_chunks():
    lines = [
        'data: {"choices":[{"delta":{"content":"Привет"}}]}\n'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":", мир"}}]}\n'.encode("utf-8"),
        b"data: [DONE]\n",
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-" + "key"
    try:
        async def collect():
            pieces = []
            async for piece in bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", [{"role": "user", "content": "hi"}]):
                pieces.append(piece)
            return pieces
        pieces = asyncio.run(collect())
        assert pieces == ["Привет", ", мир"]
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_raises_on_http_error_status():
    fake_resp = _FakeSSEResponse([], status=500)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-" + "key"
    try:
        async def collect():
            async for _ in bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", []):
                pass
        with pytest.raises(bot.MunitoAPIError):
            asyncio.run(collect())
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_caps_http_error_body_read():
    # Cap on the error body: read-all would buffer the entire multi-megabyte response
    # provider (audit run-2). Check the requested size itself, not the log length:
    # the message is still cut to 300 bytes and the cap would not distinguish it.
    seen = []

    class _Content:
        async def read(self, n=-1):
            seen.append(n)
            return b"x" * 200000

    class _Resp:
        status = 500
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp()

    async def fake_get_http_session():
        return _Session()

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-" + "key"
    try:
        async def collect():
            async for _ in bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", []):
                pass
        with pytest.raises(bot.MunitoAPIError) as exc:
            asyncio.run(collect())
        assert seen == [65536]
        assert len(str(exc.value)) < 400
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_raises_on_midstream_error_chunk():
    # REGRESSION (streaming audit): the backend may fail AFTER generation has already started —
    # The HTTP status has long been 200 (stream open), and the error arrives not
    # the response code, but directly inside the SSE chunk: {"error": {...}} instead of {"choices":
    # [...]}. Previously this was silently skipped as an “empty chunk” (no choices ->
    # continue) — the user silently received a shortened answer without any hint
    # on the cause. The first chunk ("Start") should have time to go out before the
    #     error — we verify that
    # that it is raised AFTER partial content, rather than suppressed.
    lines = [
        'data: {"choices":[{"delta":{"content":"Начало"}}]}\n'.encode("utf-8"),
        'data: {"error":{"message":"Provider returned error","code":502}}\n'.encode("utf-8"),
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-" + "key"
    try:
        collected = []

        async def collect_partial():
            agen = bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", [{"role": "user", "content": "hi"}])
            async for piece in agen:
                collected.append(piece)

        with pytest.raises(bot.MunitoAPIError) as exc_info:
            asyncio.run(collect_partial())
        assert collected == ["Начало"]
        assert exc_info.value.status_code == 502
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_without_key_fails_fast(monkeypatch):
    # Without the key, the provider must refuse before a single network call; otherwise
    # every route attempt would hang until the connection timeout.
    async def must_not_be_called():
        raise AssertionError("no session without an API key")

    monkeypatch.setattr(bot, "MUNITO_API_KEY", "")
    monkeypatch.setattr(bot, "_get_http_session", must_not_be_called)

    async def collect():
        async for _ in bot._munito_stream_pieces("m", []):
            pass

    with pytest.raises(bot.MunitoAPIError):
        asyncio.run(collect())


def test_munito_stream_pieces_sends_reasoning_effort(monkeypatch):
    # protected: Munito's SSE path carries the same reasoning_effort as regular text.
    # Regression: the field was added only to ask_munito_text, so a character-by-character
    # answer
    # again burns budget on reasoning and slows things down.
    captured = {}

    class _RecordingSSESession(_FakeSessionForSSE):
        def post(self, url, **kwargs):
            captured.update(kwargs)
            return self._resp

    lines = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b"data: [DONE]\n"]

    async def _fake_session():
        return _RecordingSSESession(_FakeSSEResponse(lines))

    async def collect(gen):
        return [piece async for piece in gen]

    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_get_http_session", _fake_session)
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "low")
    assert asyncio.run(collect(bot._munito_stream_pieces("m", [{"role": "user", "content": "hi"}]))) == ["ok"]
    assert captured["json"]["reasoning_effort"] == "low"

    captured.clear()
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "")
    asyncio.run(collect(bot._munito_stream_pieces("m", [{"role": "user", "content": "hi"}])))
    assert "reasoning_effort" not in captured["json"]


def test_streaming_history_goes_through_summarizing_trim(monkeypatch):
    # Behavioral pair for the grep test test_streaming_history_uses_summarizing_trim:
    # a long history in streaming goes through _trim_history with summaries,
    # rather than a silent slice.
    chat_id = 999981
    state = bot.get_state(chat_id)
    state["history"] = [{"role": "user", "content": f"q{i}"} for i in range(120)]

    async def fake_pieces():
        yield "готово"

    calls = {}

    async def fake_trim(hist, state=None):
        calls["n"] = calls.get("n", 0) + 1
        hist[:] = hist[-10:]

    monkeypatch.setattr(bot, "_trim_history", fake_trim)
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "вопрос", incoming, provider="munito", model_id="probe-trim",
            piece_agen=fake_pieces(),
        ))
        assert answer == "готово"
        assert calls.get("n", 0) >= 1
    finally:
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize("deadline_mode", ["none", "expired"])
def test_sse_pieces_hanging_post_entry_times_out(deadline_mode, monkeypatch):
    # A4-01 regression: entering session.post used total=None without an overall limit —
    # a stuck provider held the per-chat lock forever. Now headers wait no longer than
    # longer than the route attempt; a hung input quickly produces TimeoutError.
    class _HangingPostCM:
        def __init__(self):
            self.exited = False

        async def __aenter__(self):
            await asyncio.sleep(3600)
            return self

        async def __aexit__(self, *args):
            self.exited = True
            return False

    class _HangingPostSession:
        def __init__(self, cm):
            self._cm = cm

        def post(self, *args, **kwargs):
            return self._cm

    monkeypatch.setattr(bot, "ROUTE_MODEL_TIMEOUT_SEC", 0.2)
    cm = _HangingPostCM()
    session = _HangingPostSession(cm)
    deadline = None if deadline_mode == "none" else time.monotonic() - 1.0

    async def collect():
        async for _ in lumen_streaming._sse_pieces(
            session, "http://example.test/chat", {}, {},
            err_cls=bot.MunitoAPIError, provider_label="Munito", deadline=deadline,
        ):
            pass

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(collect(), timeout=5.0))
    elapsed = time.monotonic() - started
    # The old code hung for the entire 5-second external wait_for; the new one aborts input in about 0.2-0.5 seconds.
    assert elapsed < 2.5, f"вход в POST ждал мимо лимита попытки: {elapsed:.1f}с"
    assert cm.exited


def test_try_munito_streaming_happy_path_accumulates_and_finalizes(monkeypatch):
    chat_id = 999105

    async def fake_stream_pieces(model_id, messages, *, deadline=None, effort=None):
        for piece in ["Привет", ", как ", "дела?"]:
            yield piece

    incoming = _FakeIncomingMessage(chat_id)
    monkeypatch.setattr(bot, "_munito_stream_pieces", fake_stream_pieces)
    answer, placeholder = asyncio.run(bot._try_munito_streaming(chat_id, "Привет!", incoming, "deepseek-ai/deepseek-v4.1-flash-1017"))
    assert answer == "Привет, как дела?"
    assert placeholder is None
    assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
    history = bot.chat_state[chat_id]["history"]
    assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
    assert bot.GLOBAL_QUOTA["munito"]["deepseek-ai/deepseek-v4.1-flash-1017"]["used"] >= 1


def test_streaming_abandons_hung_first_chunk_within_limit(monkeypatch):
    # A hung first chunk raises TimeoutError and the placeholder continues down the
    # chain (previously there was no limit, so the lock could be held for 30 seconds or
    # forever).
    #
    # Patch lumen_streaming, NOT bot: _run_streaming_reply reads the name from its own
    # the module, and the previous test patched bot._model_first_chunk_limit in vain: it
    # honestly waited the normal 25 s and passed, because the boundary was 30 s
    # (hostile review 27.09.2026). The limit is now stricter than the configured limit.
    chat_id = 999301

    async def hanging_pieces():
        await asyncio.sleep(3600)
        yield "never arrives"

    monkeypatch.setattr(lumen_streaming, "_model_first_chunk_limit", lambda key, floor: 0.05)
    incoming = _FakeIncomingMessage(chat_id)
    try:
        started = time.monotonic()
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-x",
            piece_agen=hanging_pieces(),
        ))
        elapsed = time.monotonic() - started
        assert answer is None
        assert placeholder is incoming.sent[0]
        # 0.05s limit + a little headroom for fake I/O. The previous 30s
        # missed a real 25 s wait, so the test verified nothing.
        assert elapsed < 5, f"тест ждёт настоящего таймаута, а не подменённого: {elapsed:.1f}с"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_respects_route_deadline(monkeypatch):
    # The live-stream grace window is 20s in prod; shrink it in the test, otherwise
    # it sleeps for 20 seconds, while the test checks specifically for a stalled chunk.
    monkeypatch.setattr(bot, "STREAM_ROUTE_STALL_GRACE_SEC", 0.3)
    # External audit: a stream dripping one chunk at a time lived outside ROUTE_TOTAL_BUDGET_SEC and kept the lock held.
    chat_id = 999309

    async def slow_drip_pieces():
        yield "начало"
        await asyncio.sleep(3600)
        yield "никогда"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        started = time.monotonic()
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=slow_drip_pieces(), deadline=time.monotonic() - 1.0,
        ))
        elapsed = time.monotonic() - started
        # The budget has already elapsed: either keep the placeholder or show a marked final, but never wait forever.
        # A partial answer must be returned, not an empty stub (review 17.11.2026).
        assert answer is not None and answer.startswith("начало")
        # A hanging second chunk (3600s) must be stopped by the budget, not hang forever.
        assert elapsed < 30, f"стрим ждал висящий кусок мимо дедлайна: {elapsed:.1f}с"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_first_chunk_limit_capped_by_route_deadline(monkeypatch):
    # What this protects: waiting for the first chunk stays within the route budget.
    # Regression: the model limit (measured at up to 75s) ignored the deadline, and the stream
    # held the lock beyond the budget (audit 27.10.2026).
    import lumen_streaming
    seen = {}

    async def fake_feedback(piece_agen, placeholder, *, first_chunk_limit, first_task=None, status=None, wait_started=None, reply_markup=None):
        seen["limit"] = first_chunk_limit
        if first_task is not None:
            yield await first_task
        async for piece in piece_agen:
            yield piece

    async def one_piece():
        yield "готово"

    monkeypatch.setattr(lumen_streaming, "_pieces_with_waiting_feedback", fake_feedback)
    monkeypatch.setattr(lumen_streaming, "_model_first_chunk_limit", lambda key, floor: 3600)
    chat_id = 999318
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-w",
            piece_agen=one_piece(), deadline=time.monotonic() + 1,
        ))
        assert answer == "готово"
        assert seen["limit"] <= 1.1
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_starts_model_request_before_placeholder():
    # What it protects: the model request starts before sending “...”, not after it.
    # Regression: placeholder first — the model wastes an extra round trip to Telegram
    # (0.1-0.3 seconds per streaming response).
    chat_id = 999319
    order = []

    async def pieces():
        order.append("model")
        yield "готово"

    class _Msg(_FakeIncomingMessage):
        async def reply(self, text, **kwargs):
            # A real reply is a network call: it lets the first-chunk task start.
            await asyncio.sleep(0)
            order.append("placeholder")
            return await super().reply(text, **kwargs)

    incoming = _Msg(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-w",
            piece_agen=pieces(),
        ))
        assert answer == "готово"
        assert order[:2] == ["model", "placeholder"]
    finally:
        bot.chat_state.pop(chat_id, None)


def _streaming_rate_limit_quotas(chat_id, model_id, error_text):
    async def pieces_429():
        raise bot.MunitoAPIError(error_text, status_code=429)
        yield ""
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id=model_id,
            piece_agen=pieces_429(),
        ))
        assert answer is None
        return dict(bot.GLOBAL_QUOTA["munito"][model_id])
    finally:
        bot.chat_state.pop(chat_id, None)


# The "daily" text is also a munito 429 here: the provider returns windowed limits
# (200 requests in 30 seconds), and there is no way to keep the model blocked until morning —
# the daily mark is set only by the Gemini branch (see the gemini test below in the file).
@pytest.mark.parametrize("error_text", ["Rate limit reached", "free-models-per-day limit reached"])
def test_streaming_rate_limit_only_cools_down(error_text):
    # Regression (hostile review 27.09.2026): a burst of 429s set the daily mark, and
    # the model dropped out of the route until midnight — there was no way to clear the mark, because
    # a known-dead model is not called. Now this is a short cooldown.
    provider = bot.GLOBAL_QUOTA.setdefault("munito", {})
    had_model = "probe-y" in provider
    old_model = dict(provider.get("probe-y", {}))
    try:
        entry = _streaming_rate_limit_quotas(999311, "probe-y", error_text)
        assert entry["exhausted_at"] is None
        assert entry["cooldown_until"] > time.time()
        assert lumen_router_config._is_quota_exhausted("munito", "probe-y") is True
        # After the cooldown expires, the model returns to the route by itself, without waiting for midnight.
        provider["probe-y"]["cooldown_until"] = time.time() - 1.0
        assert lumen_router_config._is_quota_exhausted("munito", "probe-y") is False
    finally:
        if had_model:
            provider["probe-y"] = old_model
        else:
            provider.pop("probe-y", None)


def test_streaming_unknown_placeholder_send_abands_quietly(monkeypatch):
    # Review I21 (26.11.2026): the placeholder continues to live as a Message (edits,
    # deletion). With an unknown send outcome, the sentinel must collapse to None:
    # the stream refuses cleanly instead of crashing with AttributeError on edit_text.
    chat_id = 999313

    async def pieces():
        yield "Привет на связь"

    async def unknown_tg_call(method, *args, **kwargs):
        return bot._TG_UNKNOWN

    monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-z",
            piece_agen=pieces(),
        ))
        assert answer is None
        assert placeholder is None
    finally:
        bot.chat_state.pop(chat_id, None)


def test_waiting_dots_cycles_frames_then_stops_on_cancel(monkeypatch):
    # Unit test of the ticker itself: the first frame only after _DOTS_START_AFTER_SEC,
    # then through the frame every _DOTS_TICK_SEC; cancellation is a normal stop.
    calls = []

    async def fake_sleep(delay):
        calls.append(delay)
        if len(calls) > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "_dots_sleep", fake_sleep)
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    import lumen_streaming
    markup = lumen_streaming._stop_button_markup(999300)
    placeholder = _FakeSentMessage()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await bot._tick_waiting_dots(placeholder, reply_markup=markup)

    asyncio.run(run())
    assert calls[0] == bot._DOTS_START_AFTER_SEC
    assert calls[1:] == [bot._DOTS_TICK_SEC] * 3
    assert len(placeholder.edits) == 3
    import lumen_status
    assert all(text[0] in lumen_status._TRIO_FRAMES and "Thinking" in text
               for text, _ in placeholder.edits)
    # The placeholder already shows frame 0 (triangle): the ticker must start at the second,
    # otherwise the very first edit repeats the same character (review 28.10.2026).
    assert placeholder.edits[0][0].startswith(lumen_status._TRIO_FRAMES[1])
    assert [text[0] for text, _ in placeholder.edits] == list(lumen_status._TRIO_FRAMES[1:4])
    assert all(kw.get("reply_markup") is markup for kw in placeholder.edit_kwargs)
    # The language code must not leak into chat_state as a string key (production 28.10.2026).
    assert "en" not in bot.chat_state


def test_streaming_fast_path_shows_no_dots_frames():
    # An instantly responding model: the ticker is stopped before the first frame (2.5s of silence
    # does not occur) — edits contain only content, no "." / "..".
    chat_id = 999302

    async def instant_pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=instant_pieces(),
        ))
        assert answer == "Привет"
        texts = [text for text, _ in incoming.sent[0].edits]
        assert texts and texts[-1] == "Привет", "финальная правка — ровно ответ"
        # The old assertion “text not in _DOTS_FRAMES” was vacuous: the ticker writes
        # "glyph + Thinking...", not bare dots, and a stream failure with the suffix
        # stayed green (review 17.11.2026). In the fast path, every edit is
        # the answer prefix, neither a ticker frame nor addenda.
        assert all("Привет".startswith(t) for t in texts), texts
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_plain_text_has_no_thought_line():
    # Prod 23.11.2026: Claude removed the final activity line from a plain answer.
    # Lumen must not leave 'Thinking for Ns' over already finished text.
    chat_id = 999321

    async def pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(),
        ))
        assert answer == "Привет"
        final_text, parse_mode = incoming.sent[0].edits[-1]
        assert parse_mode == bot.ParseMode.HTML
        assert final_text == "Привет"
        assert "Thought for" not in final_text
        assert "<blockquote" not in final_text
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_slow_first_chunk_has_no_synthetic_thought_line():
    # Production 23.11.2026: Claude removed the final activity line for a simple
    # of the answer. A slow first chunk must not bring back the synthetic heading.
    chat_id = 999322

    async def pieces():
        await asyncio.sleep(1.05)
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-slow",
            piece_agen=pieces(),
        ))
        assert answer == "Привет"
        assert all("✻" not in text for text, _ in incoming.sent[0].edits)
        assert incoming.sent[0].edits[-1][0].strip() == "Привет"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_final_split_sends_missing_continuation(monkeypatch):
    # 4×1000 + 200 characters: incremental splitting saw one chunk,
    # final produced two; previously, the last edit overwrote the response with the tail.
    chat_id = 999324

    class _RecordingBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            message = _FakeSentMessage()
            self.sent.append((kwargs, message))
            return message

    fake_bot = _RecordingBot()
    monkeypatch.setattr(bot, "bot", fake_bot)

    async def pieces():
        for _ in range(4):
            yield "а" * 1000
        yield "б" * 180 + " https://example.com"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "длинный", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(),
        ))
        assert answer and answer.startswith("а" * 1000) and answer.endswith("https://example.com")
        assert len(fake_bot.sent) == 1
        first_edit = incoming.sent[0].edits[-1][0]
        tail_text = fake_bot.sent[0][0]["text"]
        assert first_edit.startswith("а")
        assert tail_text.endswith("https://example.com")
        from aiogram.types import LinkPreviewOptions
        assert fake_bot.sent[0][0]["link_preview_options"] == LinkPreviewOptions(is_disabled=True)
        # Together, both messages cover the entire answer, not just the tail.
        assert len(first_edit) + len(tail_text) == len(answer)
        # The appended tail is already final: another edit would get
        # "not modified" and replaced HTML with plain text (audit 28.10.2026).
        assert fake_bot.sent[0][1].edits == []
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_continuation_unknown_send_does_not_resend_answer(monkeypatch):
    # Review 27.11.2026: an unknown continuation may already be delivered. It must not
    # enter the definite-failure fallback, which would send the complete answer again.
    chat_id = 999327

    class _UnknownSendBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return bot._TG_UNKNOWN

    monkeypatch.setattr(bot, "bot", _UnknownSendBot())
    replied = []

    async def record_reply(message, text, *args, **kwargs):
        replied.append(text)
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_safe_reply", record_reply)

    async def pieces():
        for _ in range(4):
            yield "в" * 1000
        yield "г" * 180 + " https://example.com"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "длинный", incoming, provider="munito", model_id="probe-w",
            piece_agen=pieces(),
        ))
        assert answer and answer.endswith("https://example.com")
        # The complete answer is retained internally, but no duplicate fallback is sent.
        assert replied == []
        # The unknown continuation is not added to sent_messages. Its predecessor must
        # keep the first chunk instead of being overwritten by the final tail.
        final_visible = incoming.sent[0].edits[-1][0]
        assert final_visible.startswith("в" * 100)
        assert "https://example.com" not in final_visible
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_resplit_shifted_boundary_re_edits_shown_message(monkeypatch):
    # The boundaries of _split_text_chunks depend on the entire text (joining neighbors by
    # the final HTML), so response growth shifted the boundary of the ALREADY displayed chunk,
    # the old code then edited the finalized message, and the visible
    # text flowed between bubbles (audit 01.11.2026; review 11.11.2026, item 5
    # the opposite: the boundary is frozen once; only the tail is recalculated).
    from lumen_formatting import _md_to_html, _split_text_chunks

    chat_id = 999326
    base = "```\n" + "line text. " * 400 + "\n\n" + "y" * 300 + ".\n\n" + "z" * 300
    tail = "\n```\nAnd more text written by the model. "

    class _RecordingBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            message = _FakeSentMessage()
            self.sent.append((kwargs, message))
            return message

    monkeypatch.setattr(bot, "bot", _RecordingBot())

    async def pieces():
        yield base
        yield tail * 5

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "длинный", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(),
        ))
        final = _split_text_chunks(answer, bot.TG_MAX_LEN)
        assert len(final) > 1
        # On the first chunk the boundary was further left — otherwise the test checks nothing.
        frozen_boundary = _split_text_chunks(base, bot.TG_MAX_LEN)
        assert len(frozen_boundary) > 1, "первое разбиение обязано дать >1 куска"
        # The completed tail changes the final split, but not an already shown chunk.
        assert len(frozen_boundary[0]) < len(final[0])
        shown_first = incoming.sent[0].edits[-1][0]
        # The first bubble remained at a frozen boundary: the old code would move
        # it to the final (shifted) boundary.
        assert shown_first == _md_to_html(frozen_boundary[0])
        # Coverage was not lost: the tail went to continuations; the last displayed
        # the message ends with a model phrase; "…" is not used among continuations.
        cont_texts = [str(kw.get("text", "")) for kw, _m in bot.bot.sent]
        assert cont_texts and "…" not in cont_texts
        last_shown = cont_texts[-1] or ""
        for _kw, msg in bot.bot.sent:
            if msg.edits:
                last_shown = msg.edits[-1][0]
        assert "written by the model" in last_shown
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_stores_clean_history_text_not_full_prompt():
    # Audit 28.10.2026: ai_prompt with snippets and a date went into history;
    # keep only the clean user request.
    chat_id = 999323

    async def pieces():
        yield "Ответ"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "полный промпт со сниппетами", incoming,
            provider="munito", model_id="probe-y", piece_agen=pieces(),
            history_text="чистый вопрос",
        ))
        assert answer == "Ответ"
        history = bot.chat_state[chat_id]["history"]
        assert history[-2] == {"role": "user", "content": "чистый вопрос"}
        assert history[-1] == {"role": "assistant", "content": "Ответ"}
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_reuses_status_bubble_instead_of_sending_new_placeholder():
    # The live status is already in the chat: the stream writes the answer into it, so there is no second "…".
    chat_id = 999320

    class _FakeStatus:
        def __init__(self):
            self.message = _FakeSentMessage()
            self.finished = False

        async def finish(self):
            self.finished = True

        def paint_header_md(self):
            # The stream would draw the response body under this line; here the response
            # instantaneous, 0 seconds means no line (review 11.11.2026).
            return ""

        def compose_answer(self, answer_md):
            return ("<blockquote expandable>✻ Thought for 1s</blockquote>\n\n" + answer_md,
                    "✻ Thought for 1s\n\n" + answer_md)

    async def pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    status = _FakeStatus()
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(), status=status,
        ))
        assert answer == "Привет"
        assert incoming.sent == []  # the second bubble was not sent
        assert status.finished is True
        final_edit = status.message.edits[-1][0]
        assert "Привет" in final_edit
        assert "Thought for" in final_edit
    finally:
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize("kind", ["plain", "table"])
def test_streaming_limit_overflow_drops_activity_without_losing_final_answer(kind):
    # Review I26: neither overflow nor a table at the boundary should get a header again —
    # otherwise Telegram rejects the entire final frame.
    from lumen_formatting import _md_to_html, _tg_len
    from lumen_status import StatusMessage

    chat_id = 999319
    if kind == "table":
        prefix = "| a | b |\n| --- | --- |\n| "
        suffix = " | y |"
        answer = prefix + "x" * (bot.TG_MAX_LEN - len(prefix) - len(suffix)) + suffix
    else:
        answer = "x" * bot.TG_MAX_LEN
    assert _tg_len(answer) == bot.TG_MAX_LEN
    incoming = _FakeIncomingMessage(chat_id)
    bubble = _FakeSentMessage()
    bubble.chat = incoming.chat
    status = StatusMessage(message=bubble, lang="en")
    status._thought_seconds = 2

    async def pieces():
        yield answer

    try:
        returned, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "граница", incoming, provider="munito", model_id="probe-limit",
            piece_agen=pieces(), status=status,
        ))
        assert returned == answer
        assert placeholder is None
        final_edit = bubble.edits[-1][0]
        assert final_edit == (_md_to_html(answer) if kind == "table" else answer)
        assert "Thought for" not in final_edit
        assert _tg_len(final_edit) <= bot.TG_MAX_LEN
        assert bot.chat_state[chat_id]["history"][-1] == {"role": "assistant", "content": answer}
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_table_header_is_dropped_when_only_rich_html_overflows(monkeypatch):
    # Review I26 round 3: the markdown/HTML candidate fits, but the rich table with a header does not.
    from lumen_formatting import _tg_len
    from lumen_rich import _rich_html_with_limit
    from lumen_status import StatusMessage

    chat_id = 777
    answer = "| x | y |\n| --- | --- |\n" + "\n".join("| x | y |" for _ in range(139))
    header = "*✻ Thought for 2 seconds*"
    candidate = header + "\n\n" + answer
    assert _tg_len(answer) <= bot.TG_MAX_LEN
    assert _tg_len(_rich_html_with_limit(answer)) <= bot.TG_MAX_LEN
    assert _tg_len(_rich_html_with_limit(candidate)) > bot.TG_MAX_LEN

    class _CallableRichBot(_FakeRichBot):
        async def __call__(self, method, **kwargs):
            self.rich_edited.append({"method": method})
            return self._real_message(method.message_id)

    rich_bot = _CallableRichBot()
    monkeypatch.setattr(bot, "bot", rich_bot)
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", True)
    incoming = _FakeIncomingMessage(chat_id)
    status = StatusMessage(message=rich_bot._real_message(42).as_(rich_bot), lang="en")
    status._thought_seconds = 2

    async def pieces():
        yield answer

    try:
        returned, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "rich-граница", incoming, provider="munito", model_id="probe-rich-limit",
            piece_agen=pieces(), status=status,
        ))
        assert returned == answer
        assert placeholder is None
        last = rich_bot.rich_edited[-1]
        if "method" in last:
            method = last["method"]
            html = method.rich_message.html if method.rich_message else method.text
        else:
            html = last["rich_message"].html if "rich_message" in last else last["text"]
        assert "<table" in html
        assert "Thought for" not in html
        assert _tg_len(html) <= bot.TG_MAX_LEN
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_whitespace_only_returns_none_not_empty_response():
    # A stream of spaces only is also "sent nothing": the placeholder leaves
    # falls through the chain instead of turning into "Empty response" for the
    # user.
    chat_id = 999305

    async def whitespace_pieces():
        yield "   "

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-z",
            piece_agen=whitespace_pieces(),
        ))
        assert answer is None
        assert placeholder is incoming.sent[0]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_reveal_follows_arrival_pace_not_full_dump():
    # Chunks arrive gradually (10 × 20 characters with pauses): the first edit must show
    # PART of the response, not the entire text at once — the display clock starts from the first chunk.
    chat_id = 999308
    full = "x" * 200

    async def paced_pieces():
        for _ in range(10):
            await asyncio.sleep(0.05)
            yield "x" * 20

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-pace",
            piece_agen=paced_pieces(),
        ))
        assert answer == full
        first_edit = incoming.sent[0].edits[0][0]
        assert 0 < len(first_edit) < len(full)
    finally:
        bot.chat_state.pop(chat_id, None)


def test_try_munito_streaming_returns_none_on_early_failure(monkeypatch):
    chat_id = 999106

    async def fake_stream_pieces_raises(model_id, messages, *, deadline=None, effort=None):
        raise RuntimeError("boom before any content")
        yield ""  # makes the function an async generator (unreachable)

    incoming = _FakeIncomingMessage(chat_id)
    monkeypatch.setattr(bot, "_munito_stream_pieces", fake_stream_pieces_raises)
    answer, placeholder = asyncio.run(bot._try_munito_streaming(chat_id, "Привет!", incoming, "deepseek-ai/deepseek-v4.1-flash-1017"))
    assert answer is None
    assert placeholder is incoming.sent[0]
    assert incoming.sent[0].deleted is False
    assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")


def test_run_streaming_reply_reuses_given_placeholder():
    # The reserve model after an early stream failure writes into the same bubble:
    # a second "Thinking…" appears in the chat (prod 28.10.2026).
    chat_id = 999105

    async def pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    reused = _FakeSentMessage()

    async def must_not_send(*args, **kwargs):
        raise AssertionError("новый пузырёк не нужен: передан готовый")

    original_sender = bot._answer_sender
    bot._answer_sender = must_not_send
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет", incoming, provider="munito", model_id="probe-m",
            piece_agen=pieces(), reuse_placeholder=reused,
        ))
        assert answer == "Привет" and placeholder is None
        assert incoming.sent == []
        assert reused.edits
    finally:
        bot._answer_sender = original_sender
        bot.chat_state.pop(chat_id, None)


def test_run_streaming_reply_paces_reveal_for_burst_instead_of_dumping_full_text():
    import lumen_typing_pace
    chat_id = 999110
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    # A realistic imitation of a backend that does not stream token by token, but sends
    # the entire response in ONE chunk (see the lumen_typing_pace.py docstring on why
    # this is normal for free models with a shared lag).
    long_text = "Слово " * 80  # ~480 characters in one SSE chunk

    fake_stream = _fake_gemini_stream([long_text])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        assert placeholder is None
        partial_edits = [text for text, parse_mode in incoming.sent[0].edits if parse_mode is bot.ParseMode.HTML]
        # At least one intermediate edit must show part, but not all, of the text; otherwise
        # pacing would not engage. This protects against the original 15–20-word jumps. Review
        # 11.11.2026 item 4 also requires intermediate frames to use HTML with closed markup,
        # so filter by HTML rather than None.
        assert any(0 < len(p) < len(long_text) for p in partial_edits)
        assert all("**" not in p for p in partial_edits)
        # The final edit is already HTML with the full text, as before.
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": long_text.strip()}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_records_observed_speed_on_success():
    import lumen_typing_pace
    chat_id = 999111
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    fake_stream = _fake_gemini_stream(["Привет", ", мир!"])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        # After a successful stream, the model must get its own EMA entry; that enables
        # self-calibration without manual table edits.
        assert pace_key in lumen_typing_pace._speed_ema
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_catchup_never_exceeds_max_ticks_even_for_long_slow_burst():
    # REGRESSION against a key requirement: no matter how model speed is estimated
    # too low, and no matter how many characters remain to 'finish' in one chunk, the count
    # artificial pauses is capped by STREAM_TYPING_MAX_CATCHUP_SEC — the real
    # response speed must not suffer for the sake of pretty typing.
    import lumen_typing_pace
    chat_id = 999112
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC  # an intentionally “slow” model

    long_text = "Буква " * 500  # ~3000 characters, in one chunk, below TG_MAX_LEN

    fake_stream = _fake_gemini_stream([long_text])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    sleep_calls: list[float] = []
    original_sleep = bot._typing_sleep

    async def counting_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    bot._typing_sleep = counting_sleep
    original_client = bot.client
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        # A step is a pause plus an actual edit, not just a pause (review 17.11.2026).
        tick_ceiling = max(1, int(
            bot.STREAM_TYPING_MAX_CATCHUP_SEC
            / (bot.STREAM_TYPING_TICK_SEC + bot.STREAM_TYPING_STEP_RTT_SEC)))
        assert len(sleep_calls) <= tick_ceiling
        # The long tail prints steadily, not in eight jumps: there are noticeably more ticks.
        assert len(sleep_calls) > 8, sleep_calls
    finally:
        bot._typing_sleep = original_sleep
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_catchup_edits_respect_min_interval(monkeypatch):
    # Catch-up is edited with the same throttling as the main loop: a shorter tick
    # the edit interval, the series would quickly hit 429.
    import lumen_typing_pace
    import time
    chat_id = 999113
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC
    monkeypatch.setattr(bot, "STREAM_EDIT_MIN_INTERVAL_SEC", 0.4)
    monkeypatch.setattr(bot, "STREAM_TYPING_TICK_SEC", 0.3)
    monkeypatch.setattr(bot, "_typing_sleep", asyncio.sleep)
    long_text = "Буква " * 500
    fake_stream = _fake_gemini_stream([long_text])
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)
    edit_ts = []
    orig_tg_call = bot._tg_call

    async def rec_tg_call(method, *args, **kwargs):
        if getattr(method, "__name__", "") == "edit_text":
            edit_ts.append(time.monotonic())
        return await orig_tg_call(method, *args, **kwargs)

    monkeypatch.setattr(bot, "_tg_call", rec_tg_call)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        # The last edit is final and terminal (one call, outside throttling
        # by design) — measure the intervals between progressive edits.
        gaps = [b - a for a, b in zip(edit_ts, edit_ts[1:-1])]
        assert gaps, "expected progressive edits during streaming"
        assert min(gaps) >= 0.35
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_send_text_pauses_between_chunks(monkeypatch):
    # Chunks are sent with a pause to respect flood control for a consecutive
    # series.
    from types import SimpleNamespace
    sleeps = []

    async def rec_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bot, "_typing_sleep", rec_sleep)

    class _Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace()

    monkeypatch.setattr(bot, "bot", _Bot())
    incoming = _FakeIncomingMessage(999114)
    text = "слово " * 800
    chunks = lumen_streaming._split_text_chunks(text, bot.TG_MAX_LEN)
    assert len(chunks) > 1
    try:
        asyncio.run(bot._send_text(incoming, text))
        assert sleeps == [bot.STREAM_TYPING_TICK_SEC] * (len(chunks) - 1)
    finally:
        bot.chat_state.pop(999114, None)


def test_bot_text_send_and_edit_disable_link_previews(monkeypatch):
    # Production 23.11.2026: an answer with an MDN link expanded into a huge preview card,
    # while Claude retained a compact hyperlink.
    from aiogram.types import LinkPreviewOptions

    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    reply_calls: list[dict] = []
    incoming = _FakeIncomingMessage(999117)

    async def capture_reply(text, **kwargs):
        reply_calls.append(kwargs)
        return _FakeSentMessage()

    incoming.reply = capture_reply
    asyncio.run(bot._send_text(incoming, "https://developer.mozilla.org/ru/"))
    assert reply_calls[0]["link_preview_options"] == LinkPreviewOptions(is_disabled=True)

    edit_calls: list[dict] = []
    msg = _FakeSentMessage()

    async def capture_tg_call(func, *args, **kwargs):
        edit_calls.append(kwargs)
        return msg

    monkeypatch.setattr(bot, "_tg_call", capture_tg_call)
    assert asyncio.run(bot._edit_message_quietly(msg, "https://developer.mozilla.org/ru/")) is True
    assert edit_calls[0]["link_preview_options"] == LinkPreviewOptions(is_disabled=True)
    bot.chat_state.pop(999117, None)


def test_send_text_keeps_reply_markup_only_on_last_chunk(monkeypatch):
    # The keyboard must be sent once, with the last chunk: reply_markup on
    # each chunk duplicates the button in the chat, so among all chunks, except
    # the last one, the kwarg is removed (lumen_rich._send_text). Existing
    # send_text tests do not pass the reply_markup kwarg at all.
    from types import SimpleNamespace
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    cont_calls: list[dict] = []

    class _Bot:
        async def send_message(self, **kwargs):
            cont_calls.append(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(bot, "bot", _Bot())
    incoming = _FakeIncomingMessage(999115)
    reply_calls: list[dict] = []

    async def capture_reply(text, **kwargs):
        reply_calls.append(kwargs)
        return SimpleNamespace()

    incoming.reply = capture_reply
    keyboard = {"inline_keyboard": [[{"text": "ok", "callback_data": "x"}]]}
    text = "слово " * 1600
    try:
        asyncio.run(bot._send_text(incoming, text, reply_markup=keyboard))
    finally:
        bot.chat_state.pop(999115, None)
    assert len(bot._split_text_chunks(text, bot.TG_MAX_LEN)) >= 3
    assert len(cont_calls) >= 2
    # The first chunk goes through message.reply; continuations go through bot.send_message.
    assert "reply_markup" not in reply_calls[0]
    assert "reply_markup" not in cont_calls[0]
    assert cont_calls[-1].get("reply_markup") == keyboard


def test_gemini_streaming_daily_429_marks_exhausted_like_text_path():
    # Streaming identified the daily quota with a foreign predicate and applied a minute-long
    # cooldown instead of a mark until morning.
    chat_id = 999116
    model = bot.DEFAULT_GEMINI_MODEL
    fake_stream = _fake_gemini_stream(raises=RuntimeError("429 Quota exceeded for the day"))
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA.setdefault("gemini", {}).pop(model, None)
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, model))
        assert answer is None
        entry = bot.GLOBAL_QUOTA["gemini"][model]
        assert entry["exhausted_at"] is not None
        assert lumen_router_config._is_quota_exhausted("gemini", model) is True
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop(model, None)


def test_rich_send_used_for_final_answer_with_table():
    chat_id = 999401
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    try:
        asyncio.run(bot._safe_reply(incoming, "| A | B |\n|---|---|\n| 1 | 2 |"))
        assert len(bot.bot.rich_sent) == 1
        assert bot.bot.rich_sent[0]["rich_message"].html.startswith("<table bordered>")
        assert incoming.sent == []
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_send_keeps_table_when_split_would_exceed_message_limit():
    # Prod 24.11.2026: splitting a wide table plus ordinary text could exceed
    # 4096, and the rich path was lost entirely. A retry without split preserves the table.
    from lumen_formatting import _tg_len

    chat_id = 999403
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    rows = "\n".join(f"| row{i} | a{i} | b{i} | c{i} |" for i in range(40))
    text = "x" * 1000 + "\n| key | a | b | c |\n|---|---|---|---|\n" + rows
    try:
        asyncio.run(bot._safe_reply(incoming, text))
        assert len(bot.bot.rich_sent) == 1
        html = bot.bot.rich_sent[0]["rich_message"].html
        assert html.count("<table bordered compact>") == 1
        assert _tg_len(html) <= bot.TG_MAX_LEN
        assert incoming.sent == []
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_failure_falls_back_to_legacy_html():
    chat_id = 999402
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichFailingBot()
    try:
        asyncio.run(bot._safe_reply(incoming, "**жирный** текст"))
        assert len(incoming.sent) == 1
        assert incoming.sent[0].edits == []
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_send_unknown_preserves_sentinel(monkeypatch):
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", True)
    monkeypatch.setattr(bot, "bot", _FakeRichBot())
    calls = []

    async def unknown_tg_call(method, *args, **kwargs):
        calls.append(kwargs)
        return bot._TG_UNKNOWN

    monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    incoming = _FakeIncomingMessage(999408)
    res = asyncio.run(bot._try_send_rich(incoming, "<b>жирный</b>", is_first_chunk=True))
    assert res is bot._TG_UNKNOWN
    assert len(calls) == 1


def test_send_text_unknown_rich_delivery_skips_legacy(monkeypatch):
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", True)
    monkeypatch.setattr(bot, "bot", _FakeRichBot())
    calls = []

    async def unknown_tg_call(method, *args, **kwargs):
        calls.append(kwargs)
        return bot._TG_UNKNOWN

    monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    incoming = _FakeIncomingMessage(999409)
    asyncio.run(bot._safe_reply(incoming, "**жирный** текст"))
    assert len(calls) == 1
    assert "rich_message" in calls[0]
    assert incoming.sent == []


def test_rich_edit_unknown_preserves_sentinel(monkeypatch):
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", True)
    monkeypatch.setattr(bot, "bot", _FakeRichBot())
    calls = []

    async def unknown_tg_call(method, *args, **kwargs):
        calls.append(kwargs)
        return bot._TG_UNKNOWN

    monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    msg = _FakeRichMessage()
    res = asyncio.run(bot._try_edit_rich(msg, "<b>жирный</b>"))
    assert res is bot._TG_UNKNOWN
    assert len(calls) == 1


def test_edit_message_quietly_unknown_rich_delivery_skips_legacy(monkeypatch):
    # Callers check `if not edited`: unknown must close the retry path,
    # otherwise an HTML/plain duplicate follows an already accepted Rich response.
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", True)
    monkeypatch.setattr(bot, "bot", _FakeRichBot())
    calls = []

    async def unknown_tg_call(method, *args, **kwargs):
        calls.append(kwargs)
        return bot._TG_UNKNOWN

    monkeypatch.setattr(bot, "_tg_call", unknown_tg_call)
    msg = _FakeRichMessage()
    assert asyncio.run(bot._edit_message_quietly(msg, "**жирный**")) is True
    assert len(calls) == 1
    assert "rich_message" in calls[0]
    assert msg.edits == []


def test_rich_disabled_flag_uses_legacy_path(monkeypatch):
    chat_id = 999403
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    try:
        asyncio.run(bot._safe_reply(incoming, "**жирный** текст"))
        assert bot.bot.rich_sent == []
        assert len(incoming.sent) == 1
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_edit_used_for_final_message_edit():
    from aiogram.types import LinkPreviewOptions
    msg = _FakeRichMessage()
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    try:
        assert asyncio.run(bot._edit_message_quietly(msg, "## Заголовок")) is True
        assert len(bot.bot.rich_edited) == 1
        assert bot.bot.rich_edited[0]["rich_message"].html == "<h3>Заголовок</h3>"
        assert bot.bot.rich_edited[0]["link_preview_options"] == LinkPreviewOptions(is_disabled=True)
        assert msg.edits == []
    finally:
        bot.bot = original_bot


def test_rich_edit_falls_back_to_legacy_on_failure():
    msg = _FakeRichMessage()
    original_bot = bot.bot
    bot.bot = _FakeRichFailingBot()
    try:
        assert asyncio.run(bot._edit_message_quietly(msg, "**жирный**")) is True
        assert msg.edits and msg.edits[0][0] == "<b>жирный</b>"
    finally:
        bot.bot = original_bot


@pytest.mark.parametrize("rich", [False, True])
def test_not_modified_edit_is_success_without_plain_retry(monkeypatch, rich):
    # R5-12 (audit 26.11.2026): Telegram already displayed the required text; repeating
    # with plain text only removes formatting.
    import datetime
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import Chat, Message
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", rich)
    monkeypatch.setattr(bot, "_tg_call", lambda method, *a, **kw: method(*a, **kw))
    calls = []

    class Editor:
        async def edit_message_text(self, **kwargs):
            calls.append(kwargs)
            raise TelegramBadRequest(method="editMessageText", message="message is not modified")

        async def edit_text(self, text, **kwargs):
            calls.append(kwargs)
            raise TelegramBadRequest(method="editMessageText", message="message is not modified")

    original_bot = bot.bot
    bot.bot = Editor()
    if rich:
        msg = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=777, type="private"))
    else:
        msg = SimpleNamespace(chat=Chat(id=777, type="private"), message_id=42,
                              edit_text=bot.bot.edit_text)
    try:
        assert asyncio.run(bot._edit_message_quietly(msg, "**жирный**")) is True
        assert len(calls) == 1
        assert calls[0].get("parse_mode") == (None if rich else "HTML")
    finally:
        bot.bot = original_bot


def test_guest_answer_disables_link_previews(monkeypatch):
    from types import SimpleNamespace
    captured = {}

    async def capture(method, payload, **kwargs):
        captured.update(payload)
        return True

    monkeypatch.setattr(bot, "telegram_api_call", capture)
    msg = SimpleNamespace(chat=SimpleNamespace(id=777), guest_query_id="guest-1")
    asyncio.run(bot._answer_guest_text(msg, "https://example.com"))
    content = captured["result"]["input_message_content"]
    assert content["link_preview_options"]["is_disabled"] is True



def test_streaming_limits_chunk_resplits(monkeypatch):
    # Audit A4-11: _split_text_chunks made the loop O(n²) for every piece —
    # recalculation now happens only after a noticeable increase in text.
    real_split = lumen_streaming._split_text_chunks
    calls = {"n": 0}

    def _counting_split(text, limit):
        calls["n"] += 1
        return real_split(text, limit)

    monkeypatch.setattr(lumen_streaming, "_split_text_chunks", _counting_split)
    chat_id = 999971

    async def many_pieces():
        for i in range(30):
            yield f"кусок{i:02d} " + "x" * 60

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-split",
            piece_agen=many_pieces(),
        ))
        assert answer is not None and "кусок29" in answer
        assert calls["n"] < 30, f"разбиение на каждый кусок: {calls['n']}"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_falls_back_to_reply_when_edits_die(monkeypatch):
    # Audit A4-12: the message was deleted mid-stream; a series of unsuccessful edits
    # causes the answer to be sent as a new message rather than added to history under invisible text.
    chat_id = 999972
    incoming = _FakeIncomingMessage(chat_id)

    async def many_big_pieces():
        for i in range(5):
            yield f"часть{i} " + "y" * 5000

    async def _failing_edits_tg_call(method, *args, **kwargs):
        if getattr(method, "__name__", "") in ("reply", "send_message"):
            return await incoming.reply("…")
        return None

    async def send_message(*args, **kwargs):
        return await incoming.reply("…")

    sent_fallback = {}

    async def _recorder_fallback(message, text, **kwargs):
        sent_fallback["text"] = text

    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_message=send_message))
    monkeypatch.setattr(bot, "_tg_call", _failing_edits_tg_call)
    monkeypatch.setattr(bot, "_safe_reply", _recorder_fallback)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="dead:edit",
            piece_agen=many_big_pieces(),
        ))
        assert answer is not None and "часть4" in answer
        assert "часть4" in sent_fallback.get("text", "")
        history = bot.chat_state[chat_id]["history"]
        assert history[-1]["content"] == answer
    finally:
        bot.chat_state.pop(chat_id, None)



def test_stream_search_marker_never_reaches_the_screen_and_keeps_the_bubble():
    # Screenshots from the group 11.11.2026: "when was Fable 5.2 released" was answered from memory,
    # because the wording classifier does not recognize them. The decision is delegated to the model
    # the [[SEARCH]] marker — it must never appear on screen, even piece by piece,
    # and the “Thinking…” bubble must survive the second pass with snippets.
    chat_id = 999151
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream(
        ["[[SE", "ARCH]]", " этот хвост не дойдёт"])
    incoming = _FakeIncomingMessage(chat_id)
    original_client = bot.client
    saved_gemini = dict(bot.GLOBAL_QUOTA.get("gemini") or {})
    before_used = sum(int((e or {}).get("used") or 0)
                      for e in saved_gemini.values() if isinstance(e, dict))
    bot.client = fake_client
    try:
        with pytest.raises(lumen_search._SearchRequested) as caught:
            asyncio.run(bot._try_gemini_streaming(chat_id, "когда вышла Fable 5.2", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert incoming.sent[0].edits == []
        assert caught.value.placeholder is incoming.sent[0]
        assert bot.chat_state[chat_id].get("history") == []
        after_used = sum(int((e or {}).get("used") or 0)
                         for e in (bot.GLOBAL_QUOTA.get("gemini") or {}).values() if isinstance(e, dict))
        # The "search needed" decision is also a provider call: usage was recorded (audit 16.11.2026).
        assert after_used == before_used + 1
    finally:
        bot.client = original_client
        bot.GLOBAL_QUOTA["gemini"] = saved_gemini
        bot.chat_state.pop(chat_id, None)


def test_live_stream_is_not_cut_by_the_route_budget(monkeypatch):
    # PRODUCTION REGRESSION on 11.11.2026: a 5700-character answer was cut off at the 35th
    # second with "Connection interrupted", because the stream was checked
    # the shared 40-second route budget. While chunks are arriving, the response must
    # reach the end.
    chat_id = 999311
    monkeypatch.setattr(bot, "STREAM_ROUTE_MAX_SEC", 30)

    async def steady_drip_pieces():
        for n in range(30):
            yield "кусочек%02d " % n
            await asyncio.sleep(0.05)

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=steady_drip_pieces(), deadline=time.monotonic() - 1.0,
        ))
        assert answer is not None and placeholder is None
        assert "кусочек29" in answer, answer
        assert not any("interrupted" in (t or "").lower() for t, _ in incoming.sent[0].edits)
    finally:
        bot.chat_state.pop(chat_id, None)


def test_stream_extension_never_exceeds_the_message_budget():
    # Audit 16.11.2026: extending a live stream (STREAM_ROUTE_MAX_SEC) did not account for
    # the remainder of MESSAGE_TOTAL_BUDGET_SEC, although the comment in bot.py promises min(own
    # timeout, remainder) at every stage. Without the clamp, a 40 s search plus a 55 s
    #     stream
    # held the chat lock longer than the stated message cap.
    from lumen_streaming import _stream_deadline

    now = time.monotonic()
    token = bot._MSG_DEADLINE.set(now + 3.0)
    try:
        assert _stream_deadline(now - 1.0, now) <= now + 3.05
    finally:
        bot._MSG_DEADLINE.reset(token)
    # Outside handle_message there is no ceiling: extending a long answer works as before.
    assert _stream_deadline(now - 1.0, now) == now + bot.STREAM_ROUTE_STALL_GRACE_SEC


def test_stop_ends_the_stream_and_keeps_the_partial_answer():
    # Audit 16.11.2026: stopping an answer is a promised control. Partial text
    # remains on screen and enters history; the tail arriving after the stop
    # does not reach the answer; auto-continuation does not start after an intentional stop.
    chat_id = 999313
    incoming = _FakeIncomingMessage(chat_id)

    async def pieces():
        yield "Первый кусок текста. "
        bot._request_stream_stop(chat_id)
        yield "этот кусок не должен попасть в ответ"

    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-stop",
            piece_agen=pieces(), deadline=time.monotonic() + 30,
        ))
        assert answer is not None and "Первый кусок текста." in answer
        assert "не должен попасть" not in answer
        note = bot._t(chat_id, "stream_note_stopped").strip()
        assert any(note in (text or "") for text, _ in incoming.sent[0].edits)
        assert bot.chat_state[chat_id]["interrupted"] is True
        assert bot.chat_state[chat_id].get("stopped_by_user") is True
        assert bot.chat_state[chat_id].get("stream_interrupted") is None
        assert bot._STREAM_STOPS.get(chat_id) is None
    finally:
        bot.chat_state.pop(chat_id, None)
        bot._STREAM_STOPS.pop(chat_id, None)


def test_stop_scrubs_an_unfinished_identity_leak_before_painting(caplog):
    chat_id = 999314
    incoming = _FakeIncomingMessage(chat_id)

    async def pieces():
        yield "I am Gemini"
        bot._request_stream_stop(chat_id)
        yield " and this must not be shown"

    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Who are you?", incoming, provider="munito", model_id="probe-stop-leak",
            piece_agen=pieces(),
        ))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert placeholder is None
        assert all("gemini" not in text.lower() for text, _mode in incoming.sent[0].edits)
        assert bot.chat_state[chat_id]["history"][-1]["content"] == bot._IDENTITY_LEAK_FALLBACK
        assert "[identity-leak] Detected and blocked an identity leak" in caplog.text
    finally:
        bot.chat_state.pop(chat_id, None)
        bot._STREAM_STOPS.pop(chat_id, None)


def test_stop_recalculates_continuation_offset_after_polish(monkeypatch):
    import lumen_streaming

    monkeypatch.setattr(
        lumen_streaming, "_split_text_chunks",
        lambda text, limit: [text[index:index + 10] for index in range(0, len(text), 10)],
    )
    chat_id = 999315
    incoming = _FakeIncomingMessage(chat_id)
    raw = "🎉🎉🎉 ABCDEFGHI" + "z" * 50 + "."

    async def pieces():
        yield raw
        bot._request_stream_stop(chat_id)
        yield "ignored"

    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Напиши длинный текст", incoming, provider="munito", model_id="probe-stop-offset",
            piece_agen=pieces(),
        ))
        assert placeholder is None
        assert answer.startswith("ABCDEFGHI")
        assert "G" in answer
        shown = [message.edits[-1][0] for message in incoming.sent if message.edits][-1]
        assert "G" in shown
        assert bot._t(chat_id, "stream_note_stopped").strip() in shown
    finally:
        bot.chat_state.pop(chat_id, None)
        bot._STREAM_STOPS.pop(chat_id, None)


def test_stop_command_is_silent_without_an_active_stream():
    from types import SimpleNamespace
    import lumen_commands

    sent = []

    async def answer(*args, **kwargs):
        sent.append(args)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=999314),
        from_user=SimpleNamespace(id=777001),
        answer=answer,
    )
    asyncio.run(lumen_commands.cmd_stop(message))
    assert sent == []


def test_group_catchup_uses_the_shorter_ceiling():
    # In a group, the chat lock is shared: a long tail must not hold it for 20 seconds.
    import lumen_typing_pace

    chat_id = 999113
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC
    incoming = _FakeIncomingMessage(chat_id)
    incoming.chat.type = "group"
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream(["Буква " * 500])
    sleep_calls: list[float] = []

    async def counting_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    original_sleep, original_client = bot._typing_sleep, bot.client
    bot._typing_sleep = counting_sleep
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
    finally:
        bot._typing_sleep, bot.client = original_sleep, original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)

    step_sec = bot.STREAM_TYPING_TICK_SEC + bot.STREAM_TYPING_STEP_RTT_SEC
    group_ceiling = int(bot.STREAM_TYPING_MAX_CATCHUP_GROUP_SEC / step_sec)
    private_ceiling = int(bot.STREAM_TYPING_MAX_CATCHUP_SEC / step_sec)
    assert len(sleep_calls) <= group_ceiling < private_ceiling
    # Disabled finish-generation (budget 0) previously left the test green:
    assert len(sleep_calls) > 0
    assert (answer or "").strip() == ("Буква " * 500).strip()
def test_catchup_tick_budget_arithmetic():
    import lumen_typing_pace
    # Review 11.11.2026: the tick ceiling equals budget seconds / actual step (pause
    # + an edit). A zero budget and zero step must not cause even one pause.
    assert lumen_typing_pace.catchup_tick_budget(20.0, 1.2) == 16
    assert lumen_typing_pace.catchup_tick_budget(8.0, 1.2) == 6
    assert lumen_typing_pace.catchup_tick_budget(0.0, 1.2) == 0
    assert lumen_typing_pace.catchup_tick_budget(20.0, 0.0) == 0


def test_catchup_tick_count_follows_the_real_step_not_the_sleep_only():
    # REGRESSION found in the 11.11.2026 review: the remaining output budget was split only by the pause,
    # even though each tick also costs an edit (0.5-0.7 s through the relay). 20 seconds
    # budget at steps of 1, 2s means 16 ticks, not 40: previously a long tail
    # held the chat lock twice as long as allowed and exceeded the message budget.
    import lumen_typing_pace
    chat_id = 999312
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    saved_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC
    text = "Буква " * 500
    incoming = _FakeIncomingMessage(chat_id)
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream([text])
    sleeps: list[float] = []

    async def counting_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    saved_client, saved_sleep = bot.client, bot._typing_sleep
    bot.client, bot._typing_sleep = fake_client, counting_sleep
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == text.strip()
        step_sec = bot.STREAM_TYPING_TICK_SEC + bot.STREAM_TYPING_STEP_RTT_SEC
        ceiling = int(bot.STREAM_TYPING_MAX_CATCHUP_SEC / step_sec)
        assert len(sleeps) <= ceiling, (len(sleeps), ceiling)
        assert len(sleeps) > 8, "хвост длинного ответа всё ещё доводится плавно, а не одним куском"
    finally:
        bot.client, bot._typing_sleep = saved_client, saved_sleep
        bot.chat_state.pop(chat_id, None)
        if saved_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = saved_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_catchup_survives_zero_typing_tick_setting(monkeypatch):
    # Review 11.11.2026: STREAM_TYPING_TICK_SEC allows 0, and dividing the budget
    # to zero sent the entire answer down the failure branch marked "Connection
    # interrupted”. A config typo must not break delivery.
    import lumen_typing_pace
    chat_id = 999313
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    saved_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC
    monkeypatch.setattr(bot, "STREAM_TYPING_TICK_SEC", 0.0)
    text = "Буква " * 300
    incoming = _FakeIncomingMessage(chat_id)
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream([text])
    saved_client = bot.client
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(
            chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL,
        ))
        assert answer == text.strip()
    finally:
        bot.client = saved_client
        bot.chat_state.pop(chat_id, None)
        if saved_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = saved_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_stop_permissions_and_idempotency():
    # Audit 16.11.2026: another person in a group cannot stop the response; a repeated
    # /stop does nothing; remove the area without leaking it into the next answer.
    bot._STREAM_STOPS.clear()
    bot._begin_stream_stop_scope(424242, 100)
    try:
        assert bot._request_stream_stop(424242, 999) is False
        assert bot._stream_stop_requested(424242) is False
        assert bot._request_stream_stop(424242, 100) is True
        assert bot._stream_stop_requested(424242) is True
        assert bot._request_stream_stop(424242, 100) is False
        # A nested scope (stream) does not release the outer one or lose the flag.
        bot._begin_stream_stop_scope(424242, 100)
        bot._end_stream_stop_scope(424242)
        assert bot._stream_stop_requested(424242) is True
    finally:
        bot._end_stream_stop_scope(424242)
    assert bot._STREAM_STOPS.get(424242) is None


def test_stop_flag_is_consumed_and_does_not_kill_the_next_queued_question():
    # Second audit pass 16.11.2026: a per-chat flag survived the update, and the second
    # question waiting for the lock dies at the very first stream check.
    bot._STREAM_STOPS.clear()
    bot._begin_stream_stop_scope(515151, 100)
    bot._begin_stream_stop_scope(515151, 200)
    try:
        assert bot._request_stream_stop(515151, 100) is True
        bot._consume_stream_stop(515151)
        assert bot._stream_stop_requested(515151) is False
        assert bot._request_stream_stop(515151, 200) is True
    finally:
        bot._end_stream_stop_scope(515151)
        bot._end_stream_stop_scope(515151)
    assert bot._STREAM_STOPS.get(515151) is None


def test_stop_button_is_off_by_default_and_wired_when_on(monkeypatch):
    # Audit 16.11.2026: the button adds two edits per response, so by default
    # is disabled until a live smoke test; when enabled, it carries a localized caption and callback
    # to a specific chat.
    import lumen_streaming

    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", False)
    assert lumen_streaming._stop_button_markup(1) is None
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    kb = lumen_streaming._stop_button_markup(424242)
    assert kb is not None
    btn = kb.inline_keyboard[0][0]
    assert btn.callback_data == "stop:424242"
    assert btn.text == bot._t(424242, "stop_button")


def test_private_stop_keyboard_survives_stream_edits(monkeypatch):
    # Production 24.11.2026: an inline button under a growing answer moved off-screen.
    # In private chats, Stop lives in the reply keyboard above the composer.
    from types import SimpleNamespace
    from aiogram.types import ReplyKeyboardRemove

    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    chat_id = 999404
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=77)

    class _Placeholder:
        def __init__(self):
            self.chat = incoming.chat
            self.message_id = 1
            self.text_edits = []

        async def edit_text(self, text, **kwargs):
            self.text_edits.append(kwargs)
            return self

    async def pieces():
        yield "Привет"

    placeholder = _Placeholder()
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-stop-button",
            piece_agen=pieces(), reuse_placeholder=placeholder,
        ))
        assert answer == "Привет"
        assert len(incoming.sent) == 2
        control, remover = incoming.sent
        assert control.send_kwargs["reply_markup"].keyboard[0][0].text == "/stop"
        assert isinstance(remover.send_kwargs["reply_markup"], ReplyKeyboardRemove)
        assert all("reply_markup" not in kw for kw in placeholder.text_edits)
        assert control.deleted is True and remover.deleted is True
    finally:
        bot.chat_state.pop(chat_id, None)


def test_cancelled_unknown_stop_control_is_not_deleted(monkeypatch):
    # Review 27.11.2026: cancellation while the control send is in flight can still
    # return _TG_UNKNOWN; cleanup must not treat that sentinel as a Message.
    from types import SimpleNamespace

    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    chat_id = 999325
    entered = asyncio.Event()
    release = asyncio.Event()
    delete_accesses = []

    class _Sent:
        async def delete(self):
            raise AssertionError("placeholder cleanup is outside this test")

    calls = 0

    async def fake_tg_call(method, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Sent()
        entered.set()
        await release.wait()
        return bot._TG_UNKNOWN

    def delete_property(obj):
        delete_accesses.append(obj)
        raise AttributeError("unknown outcome has no delete")

    monkeypatch.setattr(type(bot._TG_UNKNOWN), "delete", property(delete_property), raising=False)
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=77)

    async def pieces():
        await asyncio.Event().wait()
        yield "unused"

    async def scenario():
        task = asyncio.create_task(bot._run_streaming_reply(
            chat_id, "вопрос", incoming, provider="munito", model_id="probe-stop",
            piece_agen=pieces(),
        ))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("stream cancellation did not propagate")

    try:
        asyncio.run(scenario())
        assert delete_accesses == []
    finally:
        bot.chat_state.pop(chat_id, None)


def test_stop_control_moves_below_each_continuation(monkeypatch):
    # A long answer creates continuation bubbles. Stop must remain the last
    # message, otherwise the new bubble pushes the button off-screen again.
    from types import SimpleNamespace

    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    chat_id = 999407
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=77)
    events = []

    class _Bot:
        async def send_message(self, **kwargs):
            msg = _FakeSentMessage()
            msg.chat = incoming.chat
            msg.message_id = len(events) + 10
            events.append(("answer", msg))
            return msg

    async def reply(text, **kwargs):
        msg = _FakeSentMessage()
        msg.send_kwargs = kwargs
        events.append(("control", msg))
        return msg

    original_bot = bot.bot
    bot.bot = _Bot()
    incoming.reply = reply
    placeholder = _FakeSentMessage()
    placeholder.chat = incoming.chat
    placeholder.message_id = 1

    async def pieces():
        yield "a" * 5000

    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "длинный ответ", incoming, provider="munito", model_id="probe-stop-continuation",
            piece_agen=pieces(), reuse_placeholder=placeholder,
        ))
        assert answer == "a" * 5000
        controls = [msg for kind, msg in events if kind == "control"]
        answers = [msg for kind, msg in events if kind == "answer"]
        assert len(answers) >= 1
        # In DMs, the reply keyboard is not recreated for continuation; a second
        # control — a service message with ReplyKeyboardRemove.
        assert len(controls) == 2
        assert all(msg.deleted for msg in controls)
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_stop_button_cleanup_waits_for_status_ticker(monkeypatch):
    # On an explicit stop, the ticker stops before the separate
    # control bubble; there are no later edits that could restore the button.
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    chat_id = 999405
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=77)
    events = []

    class _Control(_FakeSentMessage):
        async def delete(self):
            events.append("control_delete")
            await super().delete()

    control = _Control()
    remover = _FakeSentMessage()

    async def reply(text, **kwargs):
        if kwargs["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove":
            events.append("keyboard_remove")
            remover.send_kwargs = kwargs
            return remover
        events.append("control_sent")
        control.send_kwargs = kwargs
        return control

    incoming.reply = reply

    class _Placeholder:
        def __init__(self):
            self.chat = incoming.chat
            self.message_id = 1

        async def edit_text(self, text, **kwargs):
            return self

    class _Status:
        def __init__(self, placeholder):
            self.message = placeholder
            self.reply_markup = None

        async def finish(self):
            pass

        async def stop(self):
            events.append("status_stop")

    async def stop_before_first_piece():
        assert bot._request_stream_stop(chat_id, 77)
        yield "Привет"

    placeholder = _Placeholder()
    status = _Status(placeholder)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-stop-cleanup",
            piece_agen=stop_before_first_piece(), reuse_placeholder=placeholder, status=status,
        ))
        assert answer == ""
        assert events == ["control_sent", "status_stop", "keyboard_remove", "control_delete"]
        assert control.deleted is True and remover.deleted is True
    finally:
        bot.chat_state.pop(chat_id, None)


def test_stop_control_is_removed_when_placeholder_is_reused(monkeypatch):
    # An early timeout returns the bubble to the next attempt, but the separate Stop control
    # is removed: the next attempt will create its own and leave no two Controls in the chat.
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    monkeypatch.setattr(lumen_streaming, "_model_first_chunk_limit", lambda key, floor: 0.01)
    chat_id = 999406
    incoming = _FakeIncomingMessage(chat_id)

    class _Placeholder:
        def __init__(self):
            self.chat = incoming.chat
            self.message_id = 1
            self.reply_markup = None

    class _Status:
        def __init__(self, placeholder):
            self.message = placeholder
            self.reply_markup = None
            self.stopped = False

        async def stop(self):
            self.stopped = True

    async def hanging():
        if False:
            yield ""
        await asyncio.Future()

    placeholder = _Placeholder()
    status = _Status(placeholder)
    try:
        answer, reused = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-stop-reuse",
            piece_agen=hanging(), reuse_placeholder=placeholder, status=status,
        ))
        assert answer is None and reused is placeholder
        assert len(incoming.sent) == 2
        assert incoming.sent[0].send_kwargs["reply_markup"].keyboard
        assert incoming.sent[1].send_kwargs["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove"
        assert all(msg.deleted for msg in incoming.sent)
        assert status.stopped is False
        assert placeholder.reply_markup is None
    finally:
        bot.chat_state.pop(chat_id, None)


def test_private_stop_keyboard_uses_prelock_stop_command(monkeypatch):
    # /stop is handled by the command handler before the chat lock, so the button
    # actually interrupts the current stream rather than waiting for it to finish.
    import lumen_streaming

    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    keyboard = lumen_streaming._stop_reply_keyboard(999408)
    assert keyboard.keyboard[0][0].text == "/stop"


def test_stop_token_rotates_for_next_queued_stream():
    bot._STREAM_STOPS.clear()
    bot._begin_stream_stop_scope(77, 5)
    outer_token = bot._stream_stop_token(77)
    bot._begin_stream_stop_scope(77, 5, stream=True)
    first_token = bot._stream_stop_token(77)
    bot._begin_stream_stop_scope(77, 5)  # the next request is already waiting for the lock
    assert bot._stream_stop_token(77) == first_token
    bot._end_stream_stop_scope(77)
    bot._end_stream_stop_scope(77)
    bot._begin_stream_stop_scope(77, 5, stream=True)
    second_token = bot._stream_stop_token(77)
    try:
        assert outer_token and first_token and second_token
        assert first_token != outer_token
        assert second_token not in (outer_token, first_token)
    finally:
        bot._end_stream_stop_scope(77)
        bot._end_stream_stop_scope(77)
        bot._STREAM_STOPS.clear()


def test_stop_callback_rejects_stale_button_token(monkeypatch):
    from types import SimpleNamespace
    import lumen_commands

    bot._STREAM_STOPS.clear()
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    bot._begin_stream_stop_scope(77, 5)
    token = bot._stream_stop_token(77)

    async def answer(*args, **kwargs):
        pass

    stale = SimpleNamespace(
        data="stop:77:stale", from_user=SimpleNamespace(id=5),
        message=SimpleNamespace(chat=SimpleNamespace(id=77)), answer=answer,
    )
    try:
        asyncio.run(lumen_commands.handle_stop_callback(stale))
        assert bot._stream_stop_requested(77) is False
        legacy = SimpleNamespace(
            data="stop:77", from_user=SimpleNamespace(id=5),
            message=SimpleNamespace(chat=SimpleNamespace(id=77)), answer=answer,
        )
        asyncio.run(lumen_commands.handle_stop_callback(legacy))
        assert bot._stream_stop_requested(77) is False
        fresh = SimpleNamespace(
            data=f"stop:77:{token}", from_user=SimpleNamespace(id=5),
            message=SimpleNamespace(chat=SimpleNamespace(id=77)), answer=answer,
        )
        asyncio.run(lumen_commands.handle_stop_callback(fresh))
        assert bot._stream_stop_requested(77) is True
    finally:
        bot._end_stream_stop_scope(77)
        bot._STREAM_STOPS.clear()


def test_stop_callback_stops_only_for_the_author(monkeypatch):
    from types import SimpleNamespace
    import lumen_commands

    bot._STREAM_STOPS.clear()
    monkeypatch.setattr(bot, "STOP_BUTTON_ENABLED", True)
    answered = []

    async def answer(*args, **kwargs):
        answered.append(True)

    bot._begin_stream_stop_scope(77, 5)
    token = bot._stream_stop_token(77)
    q = SimpleNamespace(
        data=f"stop:77:{token}", from_user=SimpleNamespace(id=5),
        message=SimpleNamespace(chat=SimpleNamespace(id=77)), answer=answer,
    )
    try:
        asyncio.run(lumen_commands.handle_stop_callback(q))
        assert bot._stream_stop_requested(77) is True
        assert answered
    finally:
        bot._end_stream_stop_scope(77)
        bot._STREAM_STOPS.clear()
