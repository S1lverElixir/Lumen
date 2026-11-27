"""test_bot_tts.py — TTS: Gemini TTS and inline_tts (quotas, budgets, user limits).

Split out from test_bot.py (P2 audit); shared fakes are in bot_test_helpers.py.
"""
from unittest.mock import MagicMock, AsyncMock
import asyncio
from types import SimpleNamespace
import bot
import pytest
from lumen_router_config import GEMINI_TTS_MODELS as _TTS_MODELS
TTS_HEAD, TTS_FALLBACK = _TTS_MODELS[0], _TTS_MODELS[1]
from tests.bot_test_helpers import (
    _FakeIncomingMessage,
    _FakeVoiceBot,
    _fake_tts_response,
)


def test_inline_tts_records_quota_usage_on_success():
    # TTS allows only 10 requests/day per model (dashboard) — charge usage to GLOBAL_QUOTA["gemini"] as for text.

    # A minimal RIFF/WAV header is enough for the code to recognize the format as
    # WAV and did not try to wrap it again through pcm_to_wav; the actual conversion
    # ffmpeg is not installed in this environment and is expected to fail — this is normal
    # is caught inside inline_tts (the test checks quota accounting, not audio quality).
    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64

    def fake_generate_content(*, model, contents, config=None):
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    # Async path (lumen_tts): the synchronous call in a thread was removed — a hung
    # Google held the chat lock indefinitely (audit 26.09.2026).
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    incoming = _FakeIncomingMessage(999801)
    incoming.message_id = 12345  # inline_tts uses it for reply_to_message_id

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        entry = bot.GLOBAL_QUOTA["gemini"].get(TTS_HEAD)
        assert entry is not None
        assert entry["used"] >= 1
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)


def test_inline_tts_marks_quota_exhausted_on_daily_quota():
    # The first TTS model's daily quota is marked until morning via _mark_quota_exhausted,
    # while synthesis continues with the second model in the chain.
    class _RateLimitExc(Exception):
        status_code = 429

    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == TTS_HEAD:
            raise _RateLimitExc("Quota exceeded for quota metric 'GenerateRequestsPerDayPerProjectPerModel'")
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    incoming = _FakeIncomingMessage(999802)
    incoming.message_id = 12346

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)
    bot.GLOBAL_QUOTA["gemini"].pop(TTS_FALLBACK, None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert calls == [TTS_HEAD, TTS_FALLBACK]
        exhausted = bot.GLOBAL_QUOTA["gemini"].get(TTS_HEAD)
        assert exhausted is not None and exhausted.get("exhausted_at") is not None
        succeeded = bot.GLOBAL_QUOTA["gemini"].get(TTS_FALLBACK)
        assert succeeded is not None and succeeded["used"] >= 1
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)
        bot.GLOBAL_QUOTA["gemini"].pop(TTS_FALLBACK, None)


def test_inline_tts_burst_rate_limit_only_cools_down():
    # A one-minute spike from the first TTS model must not set the daily marker:
    # previously, any 429 removed it until midnight, although the text route had already received
    # a short cooldown. Synthesis still continues with the second model.
    import time as _time

    class _RateLimitExc(Exception):
        status_code = 429

    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == TTS_HEAD:
            raise _RateLimitExc("rate limit exceeded")
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    incoming = _FakeIncomingMessage(999803)
    incoming.message_id = 12347

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)
    bot.GLOBAL_QUOTA["gemini"].pop(TTS_FALLBACK, None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert calls == [TTS_HEAD, TTS_FALLBACK]
        entry = bot.GLOBAL_QUOTA["gemini"].get(TTS_HEAD)
        assert entry is not None and entry.get("exhausted_at") is None
        assert entry.get("cooldown_until", 0) > _time.time()
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)
        bot.GLOBAL_QUOTA["gemini"].pop(TTS_FALLBACK, None)


def test_gemini_tts_synthesis_has_timeout_and_does_not_use_threads():
    # Regression (audit 26.09.2026): a synchronous call inside asyncio.to_thread without
    # the timeout — a hung Google call held the chat lock indefinitely and lost the stream.
    import asyncio as _asyncio
    from lumen_tts import _gemini_tts_bytes as synth

    calls = []

    class _HangingAio:
        class models:
            @staticmethod
            async def generate_content(**kwargs):
                calls.append(kwargs)
                await _asyncio.sleep(30)

    client = SimpleNamespace(aio=_HangingAio)

    def never(*args, **kwargs):
        raise AssertionError("sync client path must not be used")

    client.models = SimpleNamespace(generate_content=never)

    # Specifically a timeout, not just any exception: RuntimeError from a hung mock
    # passed the old pytest.raises(Exception) check (review 17.11.2026).
    with pytest.raises(_asyncio.TimeoutError):
        _asyncio.run(synth(
            client, "текст", tts_models=["m1"],
            is_rate_limit_error=lambda e: False,
            on_model_exhausted=lambda m: None,
            on_model_success=lambda m: None,
            request_timeout_sec=0.3,
        ))
    assert calls, "async client must be used"


def test_gemini_tts_passes_http_timeout_to_sdk():
    # Second line of defense: the timeout is set in http_options even if the outer wait_for does not fire.
    import asyncio as _asyncio
    from lumen_tts import _gemini_tts_bytes as synth

    seen = {}

    def fake_generate_content(*, model, contents, config=None):
        seen["config"] = config
        return _fake_tts_response(b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64)

    class _Aio:
        class models:
            @staticmethod
            async def generate_content(**kwargs):
                return fake_generate_content(model=kwargs["model"], contents=kwargs["contents"], config=kwargs["config"])

    _asyncio.run(synth(
        SimpleNamespace(aio=_Aio), "текст", tts_models=["m1"],
        is_rate_limit_error=lambda e: False,
        on_model_exhausted=lambda m: None,
        on_model_success=lambda m: None,
        request_timeout_sec=7.0,
    ))
    http_options = getattr(seen["config"], "http_options", None)
    assert http_options is not None and http_options.timeout == 7000


class _CountingVoiceBot(_FakeVoiceBot):
    def __init__(self):
        super().__init__()
        self.voices = []

    async def send_voice(self, **kwargs):
        self.voices.append(kwargs)
        return SimpleNamespace()


def _wav_client():
    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64

    def fake_generate_content(*, model, contents, config=None):
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    # Asynchronous path (lumen_tts): the synchronous call in a thread was removed; a hung
    # A hung Google call held the chat lock indefinitely (audit 26.09.2026).
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    return fake_client


def test_split_tts_chunks_short_text_unchanged():
    from lumen_commands import _split_tts_chunks
    assert _split_tts_chunks("Привет, мир", 800) == ["Привет, мир"]
    assert _split_tts_chunks("   ", 800) == []


def test_split_tts_chunks_packs_sentences_within_limit():
    from lumen_commands import _split_tts_chunks
    s1 = "Первое предложение про котиков. "
    s2 = "Второе предложение про погоду за окном. "
    s3 = "Третье предложение про смысл жизни и всё такое."
    chunks = _split_tts_chunks(s1 + s2 + s3, 80)
    assert all(len(c) <= 80 for c in chunks)
    assert len(chunks) == 2
    assert chunks[0].startswith("Первое") and "Второе" in chunks[0]
    assert chunks[1].startswith("Третье")


def test_split_tts_chunks_hard_cuts_single_long_sentence():
    from lumen_commands import _split_tts_chunks
    long_single = "Слово " * 200
    chunks = _split_tts_chunks(long_single.strip(), 800)
    assert all(len(c) <= 800 for c in chunks)
    assert len(chunks) >= 2


def test_inline_tts_sends_long_text_in_parts():
    # Split long text into parts instead of failing: two voice messages, the first in response.
    from lumen_commands import TTS_MAX_PARTS
    assert TTS_MAX_PARTS >= 2
    limit = bot.TTS_MAX_CHARS
    part = "Предложение номер раз про интересные вещи. "
    text = part * ((limit // len(part)) + 2)
    assert len(text) > limit

    incoming = _FakeIncomingMessage(999804)
    incoming.message_id = 12348

    original_client = bot.client
    original_bot = bot.bot
    bot.client = _wav_client()
    counting = _CountingVoiceBot()
    bot.bot = counting
    try:
        asyncio.run(bot.inline_tts(incoming, text))
        assert len(counting.voices) == 2
        assert counting.voices[0].get("reply_to_message_id") == 12348
        assert counting.voices[1].get("reply_to_message_id") is None
    finally:
        bot.client = original_client
        bot.bot = original_bot


def test_inline_tts_refuses_beyond_parts_cap_without_sending():
    from lumen_commands import TTS_MAX_PARTS
    limit = bot.TTS_MAX_CHARS
    text = "Слово " * ((limit * TTS_MAX_PARTS) // 6 + 10)

    incoming = _FakeIncomingMessage(999805)
    incoming.message_id = 12349

    original_client = bot.client
    original_bot = bot.bot
    bot.client = _wav_client()
    counting = _CountingVoiceBot()
    bot.bot = counting
    try:
        asyncio.run(bot.inline_tts(incoming, text))
        assert counting.voices == []
        assert len(incoming.sent) >= 1  # the tts_too_long text was sent
    finally:
        bot.client = original_client
        bot.bot = original_bot


def test_pcm_to_wav_rejects_non_wav_riff():
    # Audit A6-11: the RIFF prefix was trusted blindly; WEBP/AVI also have it.
    import lumen_tts
    out = lumen_tts.pcm_to_wav(b"RIFF....WEBP....")
    assert out.startswith(b"RIFF") and out[8:12] == b"WAVE"
    assert lumen_tts.pcm_to_wav(b"RIFFxxxxWAVE....") == b"RIFFxxxxWAVE...."


def test_inline_tts_zero_budget_never_synthesizes(monkeypatch):
    # Audit D2: with zero total budget, synthesis does not start at all; an honest error is returned immediately.
    import lumen_commands

    async def must_not_synthesize(text):
        raise AssertionError("synthesis must not start on zero budget")

    monkeypatch.setattr(bot, "TTS_TOTAL_BUDGET_SEC", 0)
    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", must_not_synthesize)
    incoming = _FakeIncomingMessage(999986)
    incoming.message_id = 12350
    original_bot = bot.bot
    bot.bot = _FakeVoiceBot()
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        shown = [text for msg in incoming.sent for text, _ in msg.edits]
        assert shown, "пользователь должен увидеть ошибку, а не тишину"
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(999986, None)


def test_inline_tts_sends_partial_with_shortened_note(monkeypatch):
    # Audit D2: the second chunk hung; the first is sent, and the user is told about the truncation.
    import lumen_commands

    class _CountingVoiceBot(_FakeVoiceBot):
        def __init__(self):
            super().__init__()
            self.voice_count = 0

        async def send_voice(self, **kwargs):
            self.voice_count += 1
            return await super().send_voice(**kwargs)

    calls = {"n": 0}

    async def flaky_synth(text):
        calls["n"] += 1
        if calls["n"] > 1:
            await asyncio.sleep(30)
        return (b"audio1", "speech.ogg", 5)

    sent = {}

    async def fake_reply(message, text, **kwargs):
        sent["text"] = text

    monkeypatch.setattr(bot, "TTS_TOTAL_BUDGET_SEC", 0.3)
    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", flaky_synth)
    monkeypatch.setattr(bot, "_safe_reply", fake_reply)
    voice_bot = _CountingVoiceBot()
    monkeypatch.setattr(bot, "bot", voice_bot)
    incoming = _FakeIncomingMessage(999987)
    incoming.message_id = 12351
    bot.get_state(999987)["lang"] = "ru"
    try:
        asyncio.run(bot.inline_tts(incoming, "x" * 900))
        assert voice_bot.voice_count == 1
        assert "сокращ" in sent.get("text", "")
    finally:
        bot.chat_state.pop(999987, None)


def test_inline_tts_replies_when_voice_send_fails(monkeypatch):
    # Audit 28.10.2026: the “Voice synthesis” status was already removed, and send_voice returned None —
    # previously left the user in complete silence. Now there is an honest error,
    # while an unsent chunk is returned to the daily limit.
    import lumen_commands

    class _SilentVoiceBot(_FakeVoiceBot):
        async def send_voice(self, **kwargs):
            return None

    async def fake_synth(text):
        return (b"audio1", "speech.ogg", 5)

    sent = {}

    async def fake_reply(message, text, **kwargs):
        sent["text"] = text

    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", fake_synth)
    monkeypatch.setattr(bot, "_safe_reply", fake_reply)
    monkeypatch.setattr(bot, "bot", _SilentVoiceBot())
    incoming = _FakeIncomingMessage(999989)
    incoming.message_id = 12353
    bot.chat_state.pop(999989, None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert sent.get("text") == bot._t(999989, "tts_err_generic")
        assert int((bot._user_daily_peek(999989) or {}).get("total") or 0) == 0
    finally:
        bot.chat_state.pop(999989, None)


def test_inline_tts_decode_failure_unknown_keeps_slot_without_retry(monkeypatch):
    # JSON failed after sending: the voice may have been accepted. None would refund the
    # slot and show an error over audio that may already have been delivered.
    import json
    import lumen_commands

    class _DecodeFailingVoiceBot(_FakeVoiceBot):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def send_voice(self, **kwargs):
            self.calls += 1
            raise json.JSONDecodeError("Expecting value", "<html>", 0)

    async def fake_synth(text):
        return (b"audio1", "speech.ogg", 5)

    refunds = []
    notices = []

    async def fake_reply(message, text, **kwargs):
        notices.append(text)

    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", fake_synth)
    monkeypatch.setattr(bot, "_refund_user_daily", lambda *a, **kw: refunds.append((a, kw)))
    monkeypatch.setattr(bot, "_safe_reply", fake_reply)
    monkeypatch.setattr(bot, "_tg_proxy_breaker", bot._TelegramProxyCircuitBreaker(
        cooldown_sec=20.0, trip_threshold=5,
    ))
    voice_bot = _DecodeFailingVoiceBot()
    monkeypatch.setattr(bot, "bot", voice_bot)
    incoming = _FakeIncomingMessage(999990)
    incoming.message_id = 12354
    incoming.from_user = SimpleNamespace(id=777099)
    saved_day = bot.GLOBAL_QUOTA.get("quota_day")
    bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert voice_bot.calls == 1
        assert refunds == []
        assert notices == []
        entry = bot.GLOBAL_QUOTA["user_daily"][str(777099)]
        assert entry["total"] == 1 and entry["tts"] == 1
    finally:
        bot.GLOBAL_QUOTA["quota_day"] = saved_day
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(777099), None)
        bot.chat_state.pop(999990, None)


def test_inline_tts_refuses_when_user_tts_limit_exhausted(monkeypatch):
    # New: an exhausted TTS limit causes a refusal before synthesis; the model is not called.
    incoming = _FakeIncomingMessage(999988)
    incoming.message_id = 12352
    incoming.from_user = SimpleNamespace(id=777013)
    monkeypatch.setattr(bot, "DAILY_USER_TTS_LIMIT", 1)
    monkeypatch.setitem(bot.GLOBAL_QUOTA, "quota_day", bot._current_quota_day())
    bot._record_user_daily(777013, tts=True)

    async def fail_generate(*args, **kwargs):
        raise AssertionError("synthesis must not run for an exhausted user")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fail_generate)
    monkeypatch.setattr(bot, "client", fake_client)
    monkeypatch.setattr(bot, "bot", _FakeVoiceBot())
    replies = []

    async def fake_safe_reply(msg, text, **kwargs):
        replies.append(text)

    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert len(replies) == 1 and "Daily voicing limit" in replies[0]
        # A refusal is not counted: the counter does not increase.
        assert bot._user_daily_entry(777013)["tts"] == 1
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777013", None)
        bot.chat_state.pop(999988, None)


def test_inline_tts_replies_when_daily_bucket_is_full(monkeypatch):
    # R2-14 review: a capacity refusal is not a personal-limit refusal.
    import lumen_commands
    import lumen_quotas as lq

    incoming = _FakeIncomingMessage(999987)
    incoming.message_id = 12351
    incoming.from_user = SimpleNamespace(id=777012)
    monkeypatch.setattr(lq, "MAX_USER_DAILY_KEYS", 2)
    monkeypatch.setitem(bot.GLOBAL_QUOTA, "quota_day", bot._current_quota_day())
    monkeypatch.setitem(bot.GLOBAL_QUOTA, lq.USER_DAILY_KEY, {
        str(uid): {"total": 1, "gemini": 0, "tts": 0, "draw": 0, "bonus": 0}
        for uid in (777010, 777011)
    })
    replies = []

    async def fake_safe_reply(msg, text, **kwargs):
        replies.append(text)

    async def fail_synth(_text):
        raise AssertionError("synthesis must not run when the daily bucket is full")

    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", fail_synth)
    asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
    assert replies == [bot._model_error_text("fallback", bot._chat_lang(incoming.chat.id))]
    assert bot._user_daily_peek(777012) is None


def test_inline_tts_ignores_unrelated_exhausted_draw_counter(monkeypatch):
    # R2-14 review: a stale over-limit draw counter must not block or silence TTS.
    import lumen_commands

    incoming = _FakeIncomingMessage(999986)
    incoming.message_id = 12350
    incoming.from_user = SimpleNamespace(id=777011)
    monkeypatch.setattr(bot, "DAILY_USER_DRAW_LIMIT", 5)
    monkeypatch.setitem(bot.GLOBAL_QUOTA, "quota_day", bot._current_quota_day())
    monkeypatch.setitem(bot.GLOBAL_QUOTA, "user_daily", {
        "777011": {"total": 0, "gemini": 0, "tts": 0, "draw": 6, "bonus": 0},
    })
    synth = AsyncMock(return_value=(b"audio", "speech.ogg", 1))
    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", synth)
    monkeypatch.setattr(bot, "bot", _FakeVoiceBot())
    asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
    assert synth.await_count == 1
    entry = bot._user_daily_peek(777011)
    assert entry is not None
    assert (entry["total"], entry["tts"], entry["draw"]) == (1, 1, 6)


def test_inline_tts_records_user_daily_on_success(monkeypatch):
    # New: successful TTS increases total+tts (paired with test_inline_tts_records_quota_usage_on_success).
    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64

    def fake_generate_content(*, model, contents, config=None):
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    incoming = _FakeIncomingMessage(999989)
    incoming.message_id = 12353
    incoming.from_user = SimpleNamespace(id=777014)
    monkeypatch.setattr(bot, "client", fake_client)
    monkeypatch.setattr(bot, "bot", _FakeVoiceBot())
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        entry = bot.GLOBAL_QUOTA.get("user_daily", {}).get("777014")
        assert entry is not None
        assert entry["total"] == 1 and entry["tts"] == 1 and entry["gemini"] == 0
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777014", None)
        bot.chat_state.pop(999989, None)
        bot.GLOBAL_QUOTA["gemini"].pop(TTS_HEAD, None)


def test_inline_tts_charges_each_synthesized_chunk():
    # Each chunk is a separate synthesis charged to the quota: charge per chunk,
    # not once per message.
    limit = bot.TTS_MAX_CHARS
    part = "Предложение номер раз про интересные вещи. "
    text = part * ((limit // len(part)) + 2)
    assert len(text) > limit

    incoming = _FakeIncomingMessage(999810)
    incoming.message_id = 12360
    incoming.from_user = SimpleNamespace(id=777015)

    original_client = bot.client
    original_bot = bot.bot
    bot.client = _wav_client()
    counting = _CountingVoiceBot()
    bot.bot = counting
    try:
        asyncio.run(bot.inline_tts(incoming, text))
        assert len(counting.voices) == 2
        entry = bot.GLOBAL_QUOTA.get("user_daily", {}).get("777015")
        assert entry is not None
        assert entry["tts"] == 2 and entry["total"] == 2
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777015", None)
        bot.chat_state.pop(999810, None)


def test_inline_tts_refuses_when_chunks_would_exceed_limit(monkeypatch):
    # The check counts all chunks up front: 4/5 plus text spanning 3 chunks — reject before synthesis.
    import lumen_commands
    from types import SimpleNamespace
    limit = bot.TTS_MAX_CHARS
    part = "Предложение номер раз про интересные вещи. "
    text = part * ((limit // len(part)) + 2)
    assert len(text) > limit
    incoming = _FakeIncomingMessage(999811)
    incoming.message_id = 12361
    incoming.from_user = SimpleNamespace(id=777016)
    monkeypatch.setattr(bot, "DAILY_USER_TTS_LIMIT", 5)
    monkeypatch.setitem(bot.GLOBAL_QUOTA, "quota_day", bot._current_quota_day())
    for _ in range(4):
        bot._record_user_daily(777016, tts=True)

    async def fail_synth(chunk_text):
        raise AssertionError("synthesis must not run when chunks exceed the limit")

    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", fail_synth)
    replies = []

    async def fake_safe_reply(msg, reply_text, **kwargs):
        replies.append(reply_text)

    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    try:
        asyncio.run(bot.inline_tts(incoming, text))
        assert len(replies) == 1
        assert bot._user_daily_entry(777016)["tts"] == 4
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777016", None)
        bot.chat_state.pop(999811, None)

