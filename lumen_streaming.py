"""
lumen_streaming.py — провайдер-агностичный стриминг (плейсхолдер, точки, троттлинг правок, скраб утечек на чанк, catch-up). Связи с bot.py — только через отложенный `import bot`; точки подмены для тестов читаются через `bot.` в момент вызова.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
import aiohttp
from aiogram.enums import ParseMode
from aiogram.types import Message

from lumen_formatting import _md_to_html, _split_text_chunks
from lumen_message_parse import _history_user_text
from lumen_model_speed import (
    speed_key as _model_speed_key,
    record_response as _record_model_latency,
    first_chunk_limit_sec as _model_first_chunk_limit,
)
from lumen_router_config import GEMINI_MODELS
from lumen_security import (
    _leak_scan_window,
    _IDENTITY_LEAK_FALLBACK,
    _INJECTED_PAYLOAD_ECHO_FALLBACK,
    _detect_injected_payload_echo,
    _detect_identity_leak,
    _scrub_identity_leak,
)
from lumen_typing_pace import (
    speed_key as _typing_speed_key,
    record_observed_speed as _record_typing_speed,
    catchup_reveal_steps as _typing_catchup_steps,
    blend_arrival_speed as _typing_arrival_update,
    display_speed_for as _typing_display_speed,
)

log = logging.getLogger("bot")

async def _tick_waiting_dots(placeholder: Message) -> None:
    """Точки в плейсхолдере до первого куска: первый кадр после паузы (быстрые модели анимации не видят), дальше кадр по тику. Правки через _tg_call с глотанием ошибок, отмена — штатная остановка."""
    import bot
    try:
        await bot._dots_sleep(bot._DOTS_START_AFTER_SEC)
        frame_idx = 0
        while True:
            with contextlib.suppress(Exception):
                await bot._tg_call(placeholder.edit_text, bot._DOTS_FRAMES[frame_idx % len(bot._DOTS_FRAMES)], parse_mode=None, call_timeout=15.0)
            frame_idx += 1
            await bot._dots_sleep(bot._DOTS_TICK_SEC)
    except asyncio.CancelledError:
        raise


async def _pieces_with_waiting_feedback(piece_agen, placeholder: Message, *, first_chunk_limit: float):
    """Генератор + ожидание первого куска: адаптивный предел (зависшая попытка — TimeoutError, плейсхолдер уйдёт следующей модели) и анимация точек строго до первого yield. Пустой стрим — просто конец."""
    dots_task = asyncio.create_task(_tick_waiting_dots(placeholder))
    try:
        try:
            first = await asyncio.wait_for(piece_agen.__anext__(), timeout=first_chunk_limit)
        except StopAsyncIteration:
            return
        finally:
            dots_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dots_task
        yield first
        async for piece in piece_agen:
            yield piece
    finally:
        aclose = getattr(piece_agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

async def _gemini_stream_pieces(model_id: str, call_contents: list, gconfig):
    """Куски от Gemini (тонкая обёртка над generate_content_stream). Таймаут на каждый кусок — подвисший стрим не держит лок чата. Telegram-логика — общая в _run_streaming_reply."""
    import bot
    stream = await bot.client.aio.models.generate_content_stream(model=model_id, contents=call_contents, config=gconfig)
    stream_iter = stream.__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=bot.STREAM_CHUNK_TIMEOUT_SEC)
            except StopAsyncIteration:
                break
            try:
                piece = getattr(chunk, "text", "") or ""
            except Exception:
                piece = ""
            if piece:
                yield piece
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

async def _openrouter_stream_pieces(model_id: str, messages: list[dict]):
    """Куски от OpenRouter: SSE `data: {...}` с финальным `data: [DONE]`, таймаут на строку — тот же STREAM_CHUNK_TIMEOUT_SEC."""
    import bot
    if not bot.OPENROUTER_API_KEY:
        raise bot.OpenRouterAPIError("OPENROUTER_API_KEY is not set")
    headers = {
        "Authorization": f"Bearer {bot.OPENROUTER_API_KEY}",
        "HTTP-Referer": bot.OPENROUTER_HTTP_REFERER,
        "X-OpenRouter-Title": bot.OPENROUTER_TITLE,
        "Content-Type": "application/json",
    }
    session = await bot._get_http_session()
    url = f"{bot.OPENROUTER_BASE_URL}/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": True}
    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=None, connect=12.0)) as resp:
        if resp.status >= 400:
            body = await resp.read()
            raise bot.OpenRouterAPIError(f"HTTP {resp.status}: {body[:300]!r}", status_code=resp.status)
        line_iter = resp.content.__aiter__()
        while True:
            try:
                raw_line = await asyncio.wait_for(line_iter.__anext__(), timeout=bot.STREAM_CHUNK_TIMEOUT_SEC)
            except StopAsyncIteration:
                break
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
            # Ошибка может прийти ВНУТРИ SSE-чанка (HTTP 200, стрим уже открыт): {"error": ...} вместо choices. Раньше молча резало ответ — поднимаем, дальше штатно: ранний сбой — откат на следующую модель, поздний — пометка "соединение прервалось".
            err_obj = obj.get("error")
            if err_obj:
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                raise bot.OpenRouterAPIError(err_msg or "OpenRouter returned an error in the stream body", status_code=err_code)
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece

async def _groq_stream_pieces(model_id: str, messages: list[dict]):
    """Куски от Groq: тот же OpenAI-SSE, что у OpenRouter (Groq — OpenAI-совместимый API). Таймаут на строку — STREAM_CHUNK_TIMEOUT_SEC."""
    import bot
    if not bot.GROQ_API_KEY:
        raise bot.GroqAPIError("GROQ_API_KEY is not set")
    headers = {
        "Authorization": f"Bearer {bot.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    session = await bot._get_http_session()
    url = f"{bot.GROQ_BASE_URL}/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": True}
    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=None, connect=12.0)) as resp:
        if resp.status >= 400:
            body = await resp.read()
            raise bot.GroqAPIError(f"HTTP {resp.status}: {body[:300]!r}", status_code=resp.status)
        line_iter = resp.content.__aiter__()
        while True:
            try:
                raw_line = await asyncio.wait_for(line_iter.__anext__(), timeout=bot.STREAM_CHUNK_TIMEOUT_SEC)
            except StopAsyncIteration:
                break
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
            # Ошибка внутри SSE-чанка — как у OpenRouter: поднимаем, дальше штатно (ранний сбой — откат, поздний — пометка).
            err_obj = obj.get("error")
            if err_obj:
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                raise bot.GroqAPIError(err_msg or "Groq returned an error in the stream body", status_code=err_code)
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece

async def _run_streaming_reply(
    chat_id: int, user_text: str, message: Message, *, provider: str, model_id: str, piece_agen,
) -> tuple[str | None, Message | None]:
    """Стриминг: генератор кусков + плейсхолдер, чанкинг, троттлинг, обрыв при утечке/инъекции, HTML-финал, запись в историю. Возвращает (ответ, плейсхолдер): успех — (текст, None); сбой до показа — (None, плейсхолдер для следующей модели); сбой после — (показанное + пометка, None)."""
    import bot
    state = bot.get_state(chat_id)
    hist = state.setdefault("history", [])
    ctx = state.get("ctx", deque())

    sent_messages: list[Message] = []
    full_text = ""
    last_edit_ts = 0.0
    last_edited_plain = ""
    first_piece_ts: float | None = None
    # Замеры скорости/латентности — см. lumen_typing_pace.py (overall один раз на весь ответ, arrival — заново на каждое новое сообщение).
    pace_key = _typing_speed_key(provider, model_id)
    overall_start_ts = time.monotonic()
    # reveal_base_ts — момент ПЕРВОГО куска, а не старта стрима: иначе после долгой тишины формула сразу разрешала показать всё накопленное разом. last_piece_ts/arrival_ewma — замер реального темпа прихода.
    reveal_base_ts: float | None = None
    last_piece_ts: float | None = None
    arrival_ewma: float | None = None

    try:
        placeholder = await bot._tg_call(message.reply, "…", call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)
        if placeholder is None:
            return None, None
        sent_messages.append(placeholder)

        # Дальше цикл читает генератор напрямую — проверки кусков/утечек/троттлинга ниже без изменений.
        piece_agen = _pieces_with_waiting_feedback(
            piece_agen, placeholder,
            first_chunk_limit=_model_first_chunk_limit(_model_speed_key(provider, model_id), bot.FIRST_CHUNK_TIMEOUT_SEC),
        )
        async for piece in piece_agen:
            if not piece:
                continue
            now_piece = time.monotonic()
            if first_piece_ts is None:
                first_piece_ts = now_piece
                reveal_base_ts = now_piece
            else:
                arrival_ewma = _typing_arrival_update(arrival_ewma, len(piece), now_piece - (last_piece_ts or now_piece))
            last_piece_ts = now_piece
            full_text += piece

            leak_kind = None
            _scan_text = _leak_scan_window(full_text, piece)
            if _detect_identity_leak(_scan_text):
                leak_kind = "identity"
            elif _detect_injected_payload_echo(_scan_text):
                leak_kind = "payload_echo"

            if leak_kind:
                # Проверяем ДО edit_text — утечка не успевает "мигнуть" на экране.
                tag = "identity-leak" if leak_kind == "identity" else "injection-echo"
                log.warning(
                    '[%s] Stream %s/%s started leaking internal details/echoing an injected instruction — aborting the stream and showing a neutral reply instead of the partially accumulated text: %r', tag, provider, model_id, full_text[:500],
                )
                aclose = getattr(piece_agen, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                final_answer = _IDENTITY_LEAK_FALLBACK if leak_kind == "identity" else _INJECTED_PAYLOAD_ECHO_FALLBACK
                await bot._tg_call(sent_messages[-1].edit_text, final_answer, parse_mode=None, call_timeout=15.0)
                hist.append({"role": "user", "content": _history_user_text(user_text)})
                hist.append({"role": "assistant", "content": final_answer})
                if len(hist) > bot.SHARED_HISTORY_MAX_LEN:
                    del hist[:-bot.SHARED_HISTORY_MAX_LEN]
                ctx.clear()
                bot._record_quota_usage(provider, model_id)
                return final_answer, None

            chunks = _split_text_chunks(full_text, bot.TG_MAX_LEN)
            # Чанки кроме последнего (растущего) финализируем тем же разбиением, что у нестримленных ответов.
            while len(chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                # Continuation открываем ПЕРВЫМ и финализируем одним вызовом — раньше было два edit_text при отказе (лишний HTTP-запрос).
                # bot.bot — инстанс aiogram Bot (имя bot занято модулем).
                new_msg = await bot._tg_call(bot.bot.send_message, chat_id=message.chat.id, text="…", call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)
                if new_msg is None:
                    # idx ещё НЕ финализирован — не даём общему except перезаписать показанный текст чужим (там инвариант "sent[-1] == chunks[-1]" уже неверен).
                    note = bot._t(message.chat.id, "stream_note_send_fail")
                    with contextlib.suppress(Exception):
                        await bot._tg_call(sent_messages[idx].edit_text, _md_to_html(chunks[idx]) + note, parse_mode=ParseMode.HTML, call_timeout=15.0)
                    final_answer = full_text.strip()
                    hist.append({"role": "user", "content": _history_user_text(user_text)})
                    hist.append({"role": "assistant", "content": final_answer})
                    if len(hist) > bot.SHARED_HISTORY_MAX_LEN:
                        del hist[:-bot.SHARED_HISTORY_MAX_LEN]
                    ctx.clear()
                    bot._record_quota_usage(provider, model_id)
                    return final_answer, None
                await bot._tg_call(sent_messages[idx].edit_text, _md_to_html(chunks[idx]), parse_mode=ParseMode.HTML, call_timeout=15.0)
                sent_messages.append(new_msg)
                last_edited_plain = ""
                # Новое сообщение — новый "лист", показ в нём начинается с нуля (темп прихода бэкенда тот же).
                reveal_base_ts = time.monotonic()

            # Троттлинг edit_text (~раз в интервал, иначе 429) + видимый срез по измеренному темпу прихода (верхняя граница — реально пришедшее).
            now = time.monotonic()
            target_full = chunks[-1] if chunks else ""
            typing_speed = _typing_display_speed(arrival_ewma, pace_key)
            reveal_len = min(len(target_full), max(0, int((now - (reveal_base_ts or now)) * typing_speed)))
            current_chunk_text = target_full[:reveal_len]
            if now - last_edit_ts >= bot.STREAM_EDIT_MIN_INTERVAL_SEC and current_chunk_text != last_edited_plain:
                await bot._tg_call(sent_messages[-1].edit_text, current_chunk_text, parse_mode=None, call_timeout=15.0)
                last_edited_plain = current_chunk_text
                last_edit_ts = now

        if not full_text.strip():
            # Пустой стрим — неудача, но плейсхолдер отдаём вызывающему коду (см. докстринг).
            return None, sent_messages[-1]

        # Замеры — ДО довывода (пауза не должна искажать оценку), catch-up паузы в total модели — сознательно (важна видимая задержка).
        _record_typing_speed(pace_key, time.monotonic() - overall_start_ts, len(full_text))
        _record_model_latency(
            _model_speed_key(provider, model_id),
            total_sec=time.monotonic() - overall_start_ts,
            ttf_sec=(first_piece_ts - overall_start_ts) if first_piece_ts is not None else None,
        )

        # "Довывод" остатка: бэкенды одним куском в конце показывали бы "…" до самого финала. Ограничено catchup-лимитом сверху.
        final_chunks = _split_text_chunks(full_text, bot.TG_MAX_LEN)
        target_full = final_chunks[-1]
        already_shown_len = len(last_edited_plain) if last_edited_plain and target_full.startswith(last_edited_plain) else 0
        remaining_len = len(target_full) - already_shown_len
        if remaining_len > 0:
            typing_speed = _typing_display_speed(arrival_ewma, pace_key)
            for step_len in _typing_catchup_steps(remaining_len, typing_speed, bot.STREAM_TYPING_TICK_SEC, bot.STREAM_TYPING_MAX_CATCHUP_TICKS):
                await bot._typing_sleep(bot.STREAM_TYPING_TICK_SEC)
                current_chunk_text = target_full[:already_shown_len + step_len]
                if current_chunk_text != last_edited_plain:
                    await bot._tg_call(sent_messages[-1].edit_text, current_chunk_text, parse_mode=None, call_timeout=15.0)
                    last_edited_plain = current_chunk_text

        # Финал — с полной HTML-конвертацией (во время стрима голый текст: частичный markdown дал бы несбалансированные теги).
        final_text = final_chunks[-1]
        # HTML->plain fallback — внутри _edit_message_quietly (раньше дублировался здесь вручную).
        await bot._edit_message_quietly(sent_messages[-1], final_text, call_timeout=15.0)

    except Exception as exc:
        if not full_text.strip():
            # Плейсхолдер НЕ удаляем — возвращаем для переиспользования (см. докстринг).
            log.warning('[stream] Stream %s/%s failed before showing any content, falling back to a regular call: %s', provider, model_id, exc)
            return None, (sent_messages[-1] if sent_messages else None)
        log.warning('[stream] Stream %s/%s failed after partially showing the response, finishing as-is: %s', provider, model_id, exc)
        if _detect_identity_leak(full_text):
            # Последняя страховка (основная проверка — на каждый кусок выше).
            log.warning('[identity-leak] Leak caught by the fallback guard (%s_stream_exception_path): %r', provider, full_text[:500])
            full_text = _IDENTITY_LEAK_FALLBACK
            with contextlib.suppress(Exception):
                await bot._tg_call(sent_messages[-1].edit_text, _IDENTITY_LEAK_FALLBACK, parse_mode=None, call_timeout=15.0)
        elif _detect_injected_payload_echo(full_text):
            log.warning('[injection-echo] Injected-instruction echo caught by the fallback guard (%s_stream_exception_path): %r', provider, full_text[:500])
            full_text = _INJECTED_PAYLOAD_ECHO_FALLBACK
            with contextlib.suppress(Exception):
                await bot._tg_call(sent_messages[-1].edit_text, _INJECTED_PAYLOAD_ECHO_FALLBACK, parse_mode=None, call_timeout=15.0)
        else:
            with contextlib.suppress(Exception):
                chunks = _split_text_chunks(full_text, bot.TG_MAX_LEN)
                final_text = chunks[-1] if chunks else full_text
                note = bot._t(message.chat.id, "stream_note_interrupted")
                await bot._tg_call(sent_messages[-1].edit_text, _md_to_html(final_text + note), parse_mode=ParseMode.HTML, call_timeout=15.0)
    finally:
        aclose = getattr(piece_agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    # Пустой full_text сюда не доходит — ветки "Empty response" нет.
    final_answer = _scrub_identity_leak(full_text.strip(), source=f"{provider}_stream_final:{model_id}")
    hist.append({"role": "user", "content": _history_user_text(user_text)})
    hist.append({"role": "assistant", "content": final_answer})
    if len(hist) > bot.SHARED_HISTORY_MAX_LEN:
        del hist[:-bot.SHARED_HISTORY_MAX_LEN]
    ctx.clear()
    bot._record_quota_usage(provider, model_id)
    return final_answer, None

async def _try_gemini_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    """Обёртка _run_streaming_reply для Gemini (строит contents/config)."""
    import bot
    conf = GEMINI_MODELS.get(model_id, {})
    if not conf.get("stream", True):
        return None, None
    contents = await bot._build_gemini_turn_contents(chat_id, user_text)
    call_contents, gconfig = bot._build_gemini_call_config(model_id, contents)
    piece_agen = _gemini_stream_pieces(model_id, call_contents, gconfig)
    return await _run_streaming_reply(chat_id, user_text, message, provider="gemini", model_id=model_id, piece_agen=piece_agen)

async def _try_openrouter_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    """Обёртка _run_streaming_reply для OpenRouter (SSE через chat/completions)."""
    import bot
    messages = bot._build_openrouter_turn_messages(chat_id, user_text, model_id)
    # Генератор — через bot.: тесты подменяют bot._openrouter_stream_pieces фейком.
    piece_agen = bot._openrouter_stream_pieces(model_id, messages)
    return await _run_streaming_reply(chat_id, user_text, message, provider="openrouter", model_id=model_id, piece_agen=piece_agen)

async def _try_groq_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    """Обёртка _run_streaming_reply для Groq (SSE через chat/completions)."""
    import bot
    messages = bot._build_openrouter_turn_messages(chat_id, user_text, model_id)
    # Генератор — через bot.: тесты подменяют bot._groq_stream_pieces фейком.
    piece_agen = bot._groq_stream_pieces(model_id, messages)
    return await _run_streaming_reply(chat_id, user_text, message, provider="groq", model_id=model_id, piece_agen=piece_agen)
