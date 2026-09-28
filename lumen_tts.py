"""
lumen_tts.py — TTS: Gemini TTS (ветка Fish Audio S2.1 Pro отключена флагом — зеркало снято с free-каталога 17.09.2026), плюс pcm_to_wav.

Вынесено из bot.py (аудит техдолга). Внешнее состояние (сессия, ключи, клиент, квота, классификация ошибок) не дублируется — принимается параметрами и callback'ами; bot.py держит тонкие обёртки с теми же именами, интерфейс и тесты не изменились.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from typing import Any, Callable

import aiohttp
from google.genai import types

log = logging.getLogger("bot")


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    import wave
    if pcm_data.startswith(b'RIFF'):
        return pcm_data
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, 'wb') as wav_file:
         wav_file.setnchannels(channels)
         wav_file.setsampwidth(sample_width)
         wav_file.setframerate(sample_rate)
         wav_file.writeframes(pcm_data)
    return wav_buf.getvalue()


# ── Fish Audio S2.1 Pro (free) — ОТКЛЮЧЁН флагом FISH_AUDIO_ENABLED: сейчас всегда идёт Gemini TTS. Ветка оставлена: при возврате free-доступа достаточно вернуть True.

async def _fish_audio_tts_bytes(
    session: aiohttp.ClientSession, text: str, *,
    api_key: str, http_referer: str, title: str, base_url: str,
    model_id: str, request_timeout_sec: float,
) -> bytes | None:
    """Fish Audio через OpenRouter, mp3-байты или None при любой неудаче для отката на Gemini TTS. Формат ответа на реальном трафике не проверялся."""
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": http_referer,
        "X-OpenRouter-Title": title,
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": text}],
        "modalities": ["text", "audio"],
        "audio": {"voice": "default", "format": "mp3"},
        "stream": True,
    }
    url = f"{base_url}/chat/completions"
    chunks_b64: list[str] = []
    try:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=request_timeout_sec, connect=10.0),
        ) as resp:
            if resp.status >= 400:
                body = await resp.read()
                log.warning("[tts] Fish Audio HTTP %s: %r", resp.status, body[:300])
                return None
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    obj = json.loads(data_str)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                audio_piece = ((choices[0].get("delta") or {}).get("audio") or {}).get("data")
                if audio_piece:
                    chunks_b64.append(audio_piece)
    except Exception as exc:
        # Ключ из текста ошибок вычищаем (тот же defense-in-depth, что у _or_request/_groq_request).
        exc_str = str(exc) or repr(exc) or exc.__class__.__name__
        if api_key and len(api_key) > 8:
            exc_str = exc_str.replace(api_key, "<KEY>")
        log.warning("[tts] Fish Audio request failed, falling back to Gemini TTS: %s", exc_str)
        return None
    if not chunks_b64:
        log.warning('[tts] Fish Audio: stream ended without a single audio chunk (response format may have changed) — falling back to Gemini TTS.')
        return None
    try:
        return base64.b64decode("".join(chunks_b64))
    except Exception as exc:
        log.warning('[tts] Fish Audio: failed to decode base64 audio, falling back to Gemini TTS: %s', exc)
        return None


async def _gemini_tts_bytes(
    client: Any, text: str, *, tts_models: list[str],
    is_rate_limit_error: Callable[[Exception], bool],
    on_model_exhausted: Callable[[str], None],
    on_model_success: Callable[[str], None],
    is_daily_quota_error: Callable[[Exception], bool] | None = None,
    on_model_rate_limited: Callable[[str], None] | None = None,
    request_timeout_sec: float = 60.0,
) -> tuple[bytes, str, str]:
    """Gemini TTS, возвращает (pcm_bytes, mime_type, used_model). Состояние параметрами, расход в GLOBAL_QUOTA (у TTS 10 запросов/сутки на модель). Async-клиент + wait_for: синхронный to_thread нельзя отменить, зависший вызов держал лок чата (аудит 26.09.2026)."""
    async def call_tts(model_name: str):
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
                # Минутная остывка, а не суточная метка: иначе один 429 убирал бы TTS-модель до полуночи (враждебное ревью 27.09.2026).
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

    # Расход фиксируем сразу после resp: именно здесь тратится суточная квота, независимо от дальнейшей конвертации.
    on_model_success(used_tts_model)

    audio_bytes = None
    mime_type = "audio/mp3"
    for candidate in (getattr(resp, "candidates", []) or []):
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

    # SDK возвращает bytes, а не base64-строку: декодировать сырые байты как base64 — потерять треть данных (вместо речи клик).
    if isinstance(audio_bytes, (bytes, bytearray)):
        pcm_bytes = bytes(audio_bytes)
    else:
        pcm_bytes = base64.b64decode(audio_bytes)
    return pcm_bytes, mime_type, used_tts_model
