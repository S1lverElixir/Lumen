"""lumen_tts.py — TTS: Gemini TTS (the only branch since 05.11.2026: the Fish Audio mirror on OpenRouter was removed with the provider), plus pcm_to_wav.

Extracted from bot.py (tech-debt audit). External state (session, keys, client, quota, error classification) is not duplicated — it is passed as parameters and callbacks; bot.py keeps thin wrappers with the same names, and the interface and tests are unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any, Callable

from google.genai import types

log = logging.getLogger("bot")


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    import wave
    # Trust only a real WAV header: WEBP/AVI also have a RIFF prefix.
    if pcm_data.startswith(b'RIFF') and pcm_data[8:12] == b'WAVE':
        return pcm_data
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, 'wb') as wav_file:
         wav_file.setnchannels(channels)
         wav_file.setsampwidth(sample_width)
         wav_file.setframerate(sample_rate)
         wav_file.writeframes(pcm_data)
    return wav_buf.getvalue()


async def _gemini_tts_bytes(
    client: Any, text: str, *, tts_models: list[str],
    is_rate_limit_error: Callable[[Exception], bool],
    on_model_exhausted: Callable[[str], None],
    on_model_success: Callable[[str], None],
    is_daily_quota_error: Callable[[Exception], bool] | None = None,
    on_model_rate_limited: Callable[[str], None] | None = None,
    request_timeout_sec: float = 60.0,
) -> tuple[bytes, str, str]:
    """Gemini TTS; returns (pcm_bytes, mime_type, used_model). State is provided via parameters,
    usage goes to GLOBAL_QUOTA (TTS has 10 requests/day per model). Async client +
    wait_for: synchronous to_thread cannot be cancelled, and a hung call held the chat
    lock (26.09.2026 audit)."""
    async def call_tts(model_name: str):
        # GA TTS path — Interactions API: generate_content with speech_config now
        # responds with "Unknown name speechConfig" (prod 17.11.2026). The old path remains
        # as a fallback for SDKs without interactions and for test doubles.
        interactions = getattr(client.aio, "interactions", None)
        if interactions is not None:
            try:
                return await interactions.create(
                    model=model_name,
                    input=text,
                    # Lowercase: the API replies "'AUDIO' is not supported" and expects 'audio'.
                    response_modalities=["audio"],
                    # v1 responds "response_modalities is only available in v1beta"
                    # (production 17.11.2026): audio lives only on beta.
                    api_version="v1beta",
                )
            except (AttributeError, TypeError):
                # A double without a real create: below is the old generate_content path.
                pass
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=text)]
            )
        ]
        return await client.aio.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(timeout=int(request_timeout_sec * 1000)),
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Puck"
                        )
                    )
                )
            )
        )

    resp = None
    last_exc = None
    used_tts_model = None
    for mname in tts_models:
        try:
            resp = await asyncio.wait_for(call_tts(mname), timeout=request_timeout_sec)
            used_tts_model = mname
            break
        except Exception as e:
            log.warning("[tts] Failed with model %s: %s", mname, e)
            last_exc = e
            if is_rate_limit_error(e):
                # A one-minute cooldown, not a daily timestamp: otherwise one 429 would remove
                # the TTS model until midnight (hostile review 27.09.2026).
                if (
                    is_daily_quota_error is not None
                    and on_model_rate_limited is not None
                    and not is_daily_quota_error(e)
                ):
                    on_model_rate_limited(mname)
                else:
                    on_model_exhausted(mname)
            continue

    if not resp:
        if last_exc:
            raise last_exc
        else:
            raise RuntimeError("Не удалось выполнить синтез с доступными моделями TTS.")

    # Record usage immediately after resp: this is where the daily quota is spent, regardless of subsequent conversion.
    on_model_success(used_tts_model)

    audio_bytes = None
    mime_type = "audio/mp3"
    # Interactions API: ready output_audio with base64 data.
    _out_audio = getattr(resp, "output_audio", None)
    if _out_audio is not None:
        audio_bytes = getattr(_out_audio, "data", None)
        mime_type = getattr(_out_audio, "mime_type", mime_type) or mime_type
    for candidate in ([] if audio_bytes else (getattr(resp, "candidates", []) or [])):
        content = getattr(candidate, "content", None)
        if content:
            for part in (getattr(content, "parts", []) or []):
                inline_data = getattr(part, "inline_data", None)
                if inline_data:
                    audio_bytes = getattr(inline_data, "data", None)
                    mime_type = getattr(inline_data, "mime_type", "audio/mp3")
                    break
        if audio_bytes:
            break
    if not audio_bytes:
        raise RuntimeError("В ответе API отсутствуют звуковые данные.")

    # The SDK returns bytes, not a base64 string: decoding raw bytes as base64
    # loses a third of the data (a click instead of speech).
    if isinstance(audio_bytes, (bytes, bytearray)):
        pcm_bytes = bytes(audio_bytes)
    else:
        pcm_bytes = base64.b64decode(audio_bytes)
    return pcm_bytes, mime_type, used_tts_model
