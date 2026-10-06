"""
test_bot_tts.py — TTS: Fish Audio SSE и inline_tts (квоты, фолбэк на Gemini).

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from unittest.mock import MagicMock, AsyncMock
import asyncio
import base64
from types import SimpleNamespace
import bot
import pytest
from tests.bot_test_helpers import (
    _FakeIncomingMessage,
    _FakeSSEResponse,
    _FakeSessionForSSE,
    _FakeVoiceBot,
    _fake_tts_response,
)


def test_inline_tts_records_quota_usage_on_success():
    # У TTS всего 10 запросов/сутки на модель (дашборд) — расход учитываем в GLOBAL_QUOTA["gemini"] как у текстовых.

    # Минимальный RIFF/WAV-заголовок — достаточно, чтобы код распознал формат как
    # WAV и не пытался обернуть его заново через pcm_to_wav; реальная конвертация
    # через ffmpeg в этом окружении не установлена и ожидаемо упадёт — это штатно
    # ловится внутри inline_tts (тест проверяет учёт квоты, а не качество звука).
    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64

    def fake_generate_content(*, model, contents, config=None):
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    # Асинхронный путь (lumen_tts): синхронный вызов в потоке убран — зависший
    # Google держал лок чата бесконечно (аудит 26.09.2026).
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    incoming = _FakeIncomingMessage(999801)
    incoming.message_id = 12345  # inline_tts использует его для reply_to_message_id

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        entry = bot.GLOBAL_QUOTA["gemini"].get("gemini-3.1-flash-tts-preview")
        assert entry is not None
        assert entry["used"] >= 1
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)


def test_inline_tts_marks_quota_exhausted_on_daily_quota():
    # Суточная квота первой TTS-модели — метка до утра через _mark_quota_exhausted,
    # а синтез продолжается со второй моделью цепочки.
    class _RateLimitExc(Exception):
        status_code = 429

    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.1-flash-tts-preview":
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
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash-preview-tts", None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert calls == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
        exhausted = bot.GLOBAL_QUOTA["gemini"].get("gemini-3.1-flash-tts-preview")
        assert exhausted is not None and exhausted.get("exhausted_at") is not None
        succeeded = bot.GLOBAL_QUOTA["gemini"].get("gemini-2.5-flash-preview-tts")
        assert succeeded is not None and succeeded["used"] >= 1
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash-preview-tts", None)


def test_inline_tts_burst_rate_limit_only_cools_down():
    # Минутный всплеск у первой TTS-модели не должен ставить суточную метку:
    # раньше любой 429 убирал её до полуночи, хотя текстовый маршрут уже получил
    # короткую остывку. Синтез всё равно продолжается со второй моделью.
    import time as _time

    class _RateLimitExc(Exception):
        status_code = 429

    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.1-flash-tts-preview":
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
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash-preview-tts", None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert calls == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
        entry = bot.GLOBAL_QUOTA["gemini"].get("gemini-3.1-flash-tts-preview")
        assert entry is not None and entry.get("exhausted_at") is None
        assert entry.get("cooldown_until", 0) > _time.time()
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash-preview-tts", None)


def test_inline_tts_skips_fish_audio_when_disabled(monkeypatch):
    # FISH_AUDIO_ENABLED=False (аудит моделей, 17.09.2026 — зеркало снято с
    # бесплатного каталога): inline_tts не должен вообще трогать _fish_audio_tts_bytes.
    async def _fail_if_called(text):
        raise AssertionError("fish attempt must be skipped while disabled")

    monkeypatch.setattr(bot, "_fish_audio_tts_bytes", _fail_if_called)

    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64

    def fake_generate_content(*, model, contents, config=None):
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    # Асинхронный путь (lumen_tts): синхронный вызов в потоке убран — зависший
    # Google держал лок чата бесконечно (аудит 26.09.2026).
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)

    incoming = _FakeIncomingMessage(999803)
    incoming.message_id = 12347

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert bot.bot.sent_voice is not None
    finally:
        bot.client = original_client
        bot.bot = original_bot


def test_fish_audio_tts_bytes_parses_sse_audio_chunks():
    raw_audio = b"fake-mp3-bytes"
    b64_whole = base64.b64encode(raw_audio).decode("ascii")
    half = len(b64_whole) // 2
    lines = [
        ('data: {"choices":[{"delta":{"audio":{"data":"' + b64_whole[:half] + '"}}}]}\n').encode("utf-8"),
        ('data: {"choices":[{"delta":{"audio":{"data":"' + b64_whole[half:] + '"}}}]}\n').encode("utf-8"),
        b"data: [DONE]\n",
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        result = asyncio.run(bot._fish_audio_tts_bytes("Привет, мир"))
        assert result == raw_audio
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_fish_audio_tts_bytes_returns_none_on_http_error():
    fake_resp = _FakeSSEResponse([], status=500)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        assert asyncio.run(bot._fish_audio_tts_bytes("Привет")) is None
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_fish_audio_tts_bytes_returns_none_without_api_key():
    original_key = bot.OPENROUTER_API_KEY
    bot.OPENROUTER_API_KEY = ""
    try:
        assert asyncio.run(bot._fish_audio_tts_bytes("Привет")) is None
    finally:
        bot.OPENROUTER_API_KEY = original_key


def test_gemini_tts_synthesis_has_timeout_and_does_not_use_threads():
    # Регрессия (аудит 26.09.2026): синхронный вызов в asyncio.to_thread без
    # таймаута — зависший Google держал лок чата бесконечно и терял поток.
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

    with pytest.raises(Exception):
        _asyncio.run(synth(
            client, "текст", tts_models=["m1"],
            is_rate_limit_error=lambda e: False,
            on_model_exhausted=lambda m: None,
            on_model_success=lambda m: None,
            request_timeout_sec=0.3,
        ))
    assert calls, "async client must be used"


def test_gemini_tts_passes_http_timeout_to_sdk():
    # Второй рубеж: таймаут задан и в http_options, даже если внешний wait_for не сработал.
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


def test_fish_audio_tts_bytes_returns_none_on_empty_stream():
    # Поток отдал валидный SSE, но ни одного audio-чанка (например, если формат
    # ответа модели когда-нибудь изменится) — должны тихо откатиться на Gemini,
    # а не упасть с исключением или вернуть пустые байты как будто это успех.
    lines = [b"data: [DONE]\n"]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        assert asyncio.run(bot._fish_audio_tts_bytes("Привет")) is None
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


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
    # Асинхронный путь (lumen_tts): синхронный вызов в потоке убран — зависший
    # Google держал лок чата бесконечно (аудит 26.09.2026).
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
    # Длинный текст бьём на части вместо отказа: два голосовых, первый в ответ.
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
        assert len(incoming.sent) >= 1  # текст tts_too_long ушёл
    finally:
        bot.client = original_client
        bot.bot = original_bot


def test_pcm_to_wav_rejects_non_wav_riff():
    # Аудит A6-11: префиксу RIFF верили вслепую — у WEBP/AVI он тоже есть.
    import lumen_tts
    out = lumen_tts.pcm_to_wav(b"RIFF....WEBP....")
    assert out.startswith(b"RIFF") and out[8:12] == b"WAVE"
    assert lumen_tts.pcm_to_wav(b"RIFFxxxxWAVE....") == b"RIFFxxxxWAVE...."


def test_inline_tts_zero_budget_never_synthesizes(monkeypatch):
    # Аудит D2: нулевой общий бюджет — синтез не стартует вообще, сразу честная ошибка.
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
    # Аудит D2: второй кусок завис — первый уходит, пользователь узнаёт об обрезке.
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


def test_inline_tts_refuses_when_user_tts_limit_exhausted(monkeypatch):
    # Новое: исчерпанный TTS-лимит — отказ до синтеза, модель не вызывается.
    incoming = _FakeIncomingMessage(999988)
    incoming.message_id = 12352
    incoming.from_user = SimpleNamespace(id=777013)
    monkeypatch.setattr(bot, "DAILY_USER_TTS_LIMIT", 1)
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
        assert len(replies) == 1 and "1/1" in replies[0]
        # Отказ не считается: счётчик не вырос.
        assert bot._user_daily_entry(777013)["tts"] == 1
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777013", None)
        bot.chat_state.pop(999988, None)


def test_inline_tts_records_user_daily_on_success(monkeypatch):
    # Новое: успешная озвучка растит total+tts (пара к test_inline_tts_records_quota_usage_on_success).
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
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)


def test_inline_tts_charges_each_synthesized_chunk():
    # Каждый чанк — отдельный синтез за квоту: списываем поштучно, а не раз за сообщение.
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
    # Проверка считает все чанки сразу: 4/5 + текст на 3 чанка — отказ до синтеза.
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

