"""
lumen_tts.py — TTS-пайплайн: синтез речи через Fish Audio S2.1 Pro (free, поверх
OpenRouter) с резервом на цепочку моделей Gemini TTS, плюс вспомогательная конвертация
сырого PCM в WAV (`pcm_to_wav`).

Вынесено из bot.py при разбиении на модули (см. README, аудит техдолга). В отличие от
lumen_images.py (полностью изолирован), эта пара функций синтеза действительно зовёт
внешнее для себя состояние — общую aiohttp-сессию, конфигурацию OpenRouter, глобальный
клиент Gemini, учёт квоты (GLOBAL_QUOTA) и классификацию ошибок. Ни один из этих кусков
состояния здесь НЕ дублируется новым module-level global — вместо этого обе функции
принимают всё нужное параметрами (сессию, ключи/URL, клиент, и три callback'а:
классификация "это рейт-лимит?", отметка исчерпанной модели, отметка успешного расхода).

bot.py держит тонкие обёртки с ТЕМИ ЖЕ именами (`_fish_audio_tts_bytes`/`_gemini_tts_bytes`,
см. секцию "TTS-пайплайн (Fish Audio + Gemini TTS)" там же), которые на каждый вызов читают
актуальные значения СВОИХ модульных глобалов (OPENROUTER_API_KEY, client и т.п. — в т.ч.
те, что подменяются в тестах через `bot.OPENROUTER_API_KEY = ...`/`bot.client = ...`) и
прокидывают их сюда — поэтому публичный интерфейс `bot._fish_audio_tts_bytes(text)`/
`bot._gemini_tts_bytes(text)` и поведение существующих тестов не изменились ни на йоту.
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


# ─────────────────── Fish Audio S2.1 Pro (free) — TTS через OpenRouter ───────────────────
# Пробуется ПЕРВОЙ (см. inline_tts в bot.py): у Gemini TTS лимит 10 запросов/сутки НА
# МОДЕЛЬ (обе модели вместе — 20/сутки) — жёстче, чем у любой текстовой модели в
# боте; у Fish Audio free-тира заявленного дневного потолка нет вообще (только
# Fair Use Policy). При любой неудаче — тихий откат на цепочку Gemini TTS
# (_gemini_tts_bytes) без изменений в её поведении.

async def _fish_audio_tts_bytes(
    session: aiohttp.ClientSession, text: str, *,
    api_key: str, http_referer: str, title: str, base_url: str,
    model_id: str, request_timeout_sec: float,
) -> bytes | None:
    """Синтез речи через Fish Audio S2.1 Pro (free) — аудио-модальность OpenRouter
    chat/completions (modalities=["text","audio"], обязательно stream=true, куски
    приходят как SSE data: {...} с base64 в delta.audio.data — см. openrouter.ai/
    docs/guides/overview/multimodal/audio). Возвращает сырые байты mp3 или None
    при ЛЮБОЙ неудаче (нет ключа, сетевая ошибка, неожиданный формат ответа) —
    вызывающий код (inline_tts в bot.py) в этом случае просто откатывается на Gemini TTS,
    поэтому здесь нарочно нет ни одного raise.
    Формат ответа не проверялся вручную на реальном трафике (модель для бота
    новая) — согласно принципу "сначала диагностика, потом фикс" (см. остальной
    проект), при любой странности в форме ответа функция логирует сырой кусок и
    возвращает None, а не пытается угадать дальше."""
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
        log.warning("[tts] Fish Audio request failed, falling back to Gemini TTS: %s", exc)
        return None
    if not chunks_b64:
        log.warning("[tts] Fish Audio: поток закончился без единого audio-чанка (формат ответа мог измениться) — откатываюсь на Gemini TTS.")
        return None
    try:
        return base64.b64decode("".join(chunks_b64))
    except Exception as exc:
        log.warning("[tts] Fish Audio: не удалось декодировать base64 аудио, откатываюсь на Gemini TTS: %s", exc)
        return None


async def _gemini_tts_bytes(
    client: Any, text: str, *, tts_models: list[str],
    is_rate_limit_error: Callable[[Exception], bool],
    on_model_exhausted: Callable[[str], None],
    on_model_success: Callable[[str], None],
) -> tuple[bytes, str, str]:
    """Синтез речи через цепочку Gemini TTS-моделей (резерв после Fish Audio,
    см. _fish_audio_tts_bytes выше). Возвращает (pcm_bytes, mime_type, used_model)
    или бросает исключение, если вся цепочка отказала — inline_tts в bot.py ловит
    его тем же except, что и раньше.

    `client` (genai.Client) и три callback'а передаются вызывающим кодом — см.
    докстринг модуля про то, почему они не читаются здесь напрямую из bot.py:
    `is_rate_limit_error` — та же классификация ошибок (_error_text/_error_status/
    _classify_model_error), что используется и для обычных чат-моделей в bot.py;
    `on_model_exhausted`/`on_model_success` — запись в GLOBAL_QUOTA (_mark_quota_
    exhausted/_record_quota_usage в bot.py), т.к. по дашборду AI Studio у TTS-моделей
    лимит всего 10 запросов/сутки на модель — жёстче даже флагманских текстовых
    моделей, и расход должен учитываться в том же реестре квоты, что и у них."""
    def call_tts(model_name: str):
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=text)]
            )
        ]
        return client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
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
            resp = await asyncio.to_thread(call_tts, mname)
            used_tts_model = mname
            break
        except Exception as e:
            log.warning("[tts] Failed with model %s: %s", mname, e)
            last_exc = e
            if is_rate_limit_error(e):
                on_model_exhausted(mname)
            continue

    if not resp:
        if last_exc:
            raise last_exc
        else:
            raise RuntimeError("Не удалось выполнить синтез с доступными моделями TTS.")

    # Расход реально состоявшегося успешного вызова — фиксируем сразу после
    # получения resp (а не после всей последующей обработки аудио/ffmpeg), т.к.
    # именно на этом шаге тратится дефицитная суточная квота API, независимо от
    # того, удастся ли дальше сконвертировать/отправить голосовое сообщение.
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

    # google-genai SDK возвращает inline_data.data как bytes, НЕ base64-строку.
    # base64.b64decode(bytes) трактует сырые байты как base64-алфавит → 33% данных
    # теряются → вместо речи слышен клик. Проверяем тип перед декодированием.
    if isinstance(audio_bytes, (bytes, bytearray)):
        pcm_bytes = bytes(audio_bytes)
    else:
        pcm_bytes = base64.b64decode(audio_bytes)
    return pcm_bytes, mime_type, used_tts_model
