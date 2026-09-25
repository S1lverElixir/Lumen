"""
test_bot_tts.py — TTS: Fish Audio SSE и inline_tts (квоты, фолбэк на Gemini).

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from unittest.mock import MagicMock
import asyncio
import base64
from types import SimpleNamespace
import bot
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
    fake_client.models.generate_content.side_effect = fake_generate_content

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


def test_inline_tts_marks_quota_exhausted_on_rate_limit():
    # Первая модель отдаёт явный 429 — должна быть помечена исчерпанной через
    # _mark_quota_exhausted, а синтез должен продолжиться со второй моделью цепочки.
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
    fake_client.models.generate_content.side_effect = fake_generate_content

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
    fake_client.models.generate_content.side_effect = fake_generate_content

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
    fake_client.models.generate_content.side_effect = fake_generate_content
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

