"""
lumen_streaming.py — провайдер-агностичный стриминг ответов (вынесено из bot.py,
P2 аудита): плейсхолдер, бегущие точки, троттлинг правок, пейсинг по измеренной
скорости, скраб утечек на каждый чанк, catch-up довывод.

Связи с рантаймом bot.py — ТОЛЬКО через отложенный `import bot` внутри функций
(модульного цикла нет). bot.py реэкспортирует имена — `bot._run_streaming_reply`
и т.п. в тестах и `_run_route` не менялись. Точки подмены для тестов
(`bot._typing_sleep`, `bot._dots_sleep`, `bot._DOTS_*`) читаются через `bot.`
в момент вызова, поэтому monkeypatch/conftest видят их как раньше.
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
    get_typing_speed as _get_typing_speed,
    record_observed_speed as _record_typing_speed,
    catchup_reveal_steps as _typing_catchup_steps,
)

log = logging.getLogger("bot")

async def _tick_waiting_dots(placeholder: Message) -> None:
    """Бегущие точки в плейсхолдере, пока стрим не прислал первый кусок.
    Первый кадр — только после _DOTS_START_AFTER_SEC тишины (быстрые модели
    анимации не видят вообще), дальше — кадр каждые _DOTS_TICK_SEC. Правки идут
    через _tg_call (ошибки глотаются — плейсхолдер могли удалить/переиспользовать
    параллельно), отмена задачи — штатный путь остановки после первого куска."""
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
    """Оборачивает генератор кусков стрима двумя вещами сразу (обе касаются
    только ОЖИДАНИЯ ПЕРВОГО куска — дальше генератор пробрасывается как есть):
    1. Адаптивный предел (см. lumen_model_speed.first_chunk_limit_sec): зависшая
       попытка бросается TimeoutError — вызывающий код (_run_streaming_reply)
       уже умеет отдавать плейсхолдер следующей модели по цепочке.
    2. Анимация точек (_tick_waiting_dots): гасится строго до yield первого
       куска, поэтому с показом текста не пересекается ни одним кадром.
     Пустой стрим (StopAsyncIteration сразу) — просто конец без кусков."""
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
    """Асинхронный генератор кусков текста от Gemini — тонкая обёртка над
    client.aio.models.generate_content_stream с таймаутом на КАЖДЫЙ следующий
    кусок (см. STREAM_CHUNK_TIMEOUT_SEC), чтобы генуинно подвисший стрим не
    держал лок чата бесконечно. Провайдер-специфичная часть стриминга — вся
    Telegram-логика (плейсхолдер, чанкинг, троттлинг, защита от утечек) теперь
    общая для любого провайдера, см. _run_streaming_reply."""
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
    """Асинхронный генератор кусков текста от OpenRouter — SSE-стриминг
    (`"stream": true`) через тот же chat/completions эндпоинт, что и обычный
    (нестримленный) вызов. OpenRouter отдаёт события построчно, вида
    `data: {...}\\n\\n`, с финальной строкой `data: [DONE]`. Таймаут на каждую
    следующую строку — тот же STREAM_CHUNK_TIMEOUT_SEC, что и у Gemini."""
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
            # НАЙДЕНО ПРИ АУДИТЕ СТРИМИНГА: если провайдер за OpenRouter падает
            # УЖЕ ПОСЛЕ старта генерации (не сразу, на середине ответа), сам HTTP-
            # статус остаётся 200 (стрим уже открыт) — ошибка приходит не как
            # resp.status >= 400 выше, а прямо ВНУТРИ SSE-чанка:
            # {"error": {"message": ..., "code": ...}} вместо {"choices": [...]}.
            # Раньше такой чанк тихо пропускался (choices пустой -> continue), и
            # пользователь получал молча укороченный ответ без единого намёка на
            # причину — то же самое силентное поглощение, которого проект уже
            # избегает во всех остальных местах (ask_gemini/ask_openrouter_text).
            # Поднимаем как OpenRouterAPIError — дальше её уже штатно обрабатывает
            # _run_streaming_reply: ранний сбой (ничего ещё не показано) -> откат
            # на следующую модель маршрута, поздний сбой (часть ответа уже
            # показана) -> честная пометка "соединение прервалось".
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

async def _run_streaming_reply(
    chat_id: int, user_text: str, message: Message, *, provider: str, model_id: str, piece_agen,
) -> tuple[str | None, Message | None]:
    """Провайдер-агностичная реализация стриминга: принимает асинхронный генератор
    кусков текста (см. _gemini_stream_pieces/_openrouter_stream_pieces) и делает
    всё остальное — плейсхолдер, разбиение на несколько сообщений при превышении
    лимита Telegram, троттлинг edit_text, обрыв при обнаружении утечки идентичности
    или эха внедрённой инъекции, финальную HTML-конвертацию и запись в общую
    историю чата. Раньше эта логика была написана только под Gemini — сейчас она
    ОДНА на любого провайдера, чтобы стриминг работал одинаково для Gemini и для
    OpenRouter (см. _try_gemini_streaming/_try_openrouter_streaming — тонкие
    обёртки, которые строят провайдер-специфичный call_contents/messages и
    генератор кусков, а дальше передают его сюда).

    Возвращает (ответ, плейсхолдер):
    - Успех: (текст_ответа, None) — плейсхолдер уже отредактирован до финального
      текста, вызывающему коду больше нечего с ним делать.
    - Ошибка ДО показа хоть одного символа ответа: (None, плейсхолдер_или_None).
      РАНЬШЕ плейсхолдер тут же удалялся, и следующая модель по цепочке отправляла
      СОВСЕМ НОВОЕ сообщение — визуально это выглядело как "точки исчезли, потом
      из ниоткуда появился целый ответ одним блоком", без единого "живого" эффекта.
      Теперь плейсхолдер НЕ удаляется здесь — он возвращается вызывающему коду
      (_run_route), чтобы тот попробовал доправить в него финальный ответ от
      следующей модели по цепочке напрямую, а не создавать новое сообщение.
      Если ни одна дальнейшая модель не пригодится, _run_route сам аккуратно
      уберёт этот плейсхолдер.
    - Ошибка ПОСЛЕ показа части ответа — уже показанное не удаляется и не
      подменяется другим ответом, в конец добавляется пометка о возможном обрыве;
      возвращается (текст_ответа, None), как и при обычном успехе.
    """
    import bot
    state = bot.get_state(chat_id)
    hist = state.setdefault("history", [])
    ctx = state.get("ctx", deque())

    sent_messages: list[Message] = []
    full_text = ""
    last_edit_ts = 0.0
    last_edited_plain = ""
    first_piece_ts: float | None = None
    # pace_key/overall_start_ts/current_chunk_start_ts — см. lumen_typing_pace.py.
    # overall_start_ts фиксируется ОДИН раз (для замера реальной скорости бэкенда
    # целиком, даже если ответ займёт несколько сообщений), current_chunk_start_ts
    # сбрасывается на каждое НОВОЕ сообщение (см. continuation ниже) — паттерн
    # печати у каждого отдельного Telegram-сообщения свой, начинается заново.
    pace_key = _typing_speed_key(provider, model_id)
    overall_start_ts = time.monotonic()
    current_chunk_start_ts = overall_start_ts

    try:
        placeholder = await bot._tg_call(message.reply, "…", call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)
        if placeholder is None:
            return None, None
        sent_messages.append(placeholder)

        # Обёртка ожидания первого куска: адаптивный предел + бегущие точки
        # (см. _pieces_with_waiting_feedback). Дальше цикл не отличим от чтения
        # исходного генератора напрямую — все существующие проверки кусков,
        # утечек и троттлинга ниже работают без изменений.
        piece_agen = _pieces_with_waiting_feedback(
            piece_agen, placeholder,
            first_chunk_limit=_model_first_chunk_limit(_model_speed_key(provider, model_id), bot.FIRST_CHUNK_TIMEOUT_SEC),
        )
        async for piece in piece_agen:
            if not piece:
                continue
            if first_piece_ts is None:
                first_piece_ts = time.monotonic()
            full_text += piece

            leak_kind = None
            _scan_text = _leak_scan_window(full_text, piece)
            if _detect_identity_leak(_scan_text):
                leak_kind = "identity"
            elif _detect_injected_payload_echo(_scan_text):
                leak_kind = "payload_echo"

            if leak_kind:
                # Проверяем СРАЗУ после накопления куска и ДО любого edit_text ниже —
                # на этот момент ни одно уже показанное пользователю сообщение ещё не
                # содержит только что добавленный (утекающий) кусок текста, поэтому
                # обрыв здесь гарантированно не даёт утечке "мигнуть" на экране хотя бы
                # на долю секунды, в отличие от проверки уже после финальной правки.
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
            # Финализируем все чанки кроме последнего (текущего, ещё растущего) —
            # тот же алгоритм разбиения, что и для обычных (нестримленных) длинных ответов.
            while len(chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                # НАЙДЕНО ПРИ АУДИТЕ СТРИМИНГА: раньше здесь СНАЧАЛА финализировался
                # sent_messages[idx] полным edit_text, а ПОТОМ, если открыть сообщение-
                # продолжение не удавалось, тот же самый edit_text вызывался ЕЩЁ РАЗ с
                # тем же текстом плюс пометка — два сетевых вызова ради одного и того же
                # результата в ветке отказа. Пробуем continuation ПЕРВЫМ, и финализируем
                # idx ровно одним вызовом сразу с нужным текстом (с пометкой или без) —
                # тот же итоговый результат для пользователя, но без лишнего HTTP-запроса
                # к Telegram, когда continuation всё равно проваливается.
                # bot.bot — инстанс aiogram Bot (в модуле имя bot занято самим модулем).
                new_msg = await bot._tg_call(bot.bot.send_message, chat_id=message.chat.id, text="…", call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)
                if new_msg is None:
                    # Не удалось открыть сообщение под продолжение. ВАЖНО: sent_messages[idx]
                    # ещё НЕ финализирован — это НЕ то же самое, что общий except ниже
                    # (который считает, что sent_messages[-1] соответствует chunks[-1] —
                    # здесь это неверно, там уже другой, ещё не начатый кусок). Обрабатываем
                    # прямо тут, не давая общему except перезаписать корректно показанный
                    # текст чужим содержимым.
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
                # Новое сообщение — новый "лист", печать в нём начинается с нуля.
                current_chunk_start_ts = time.monotonic()

            # Троттлинг: реальный edit_text не чаще ~раза в STREAM_EDIT_MIN_INTERVAL_SEC,
            # иначе Telegram начинает отвечать 429 на слишком частые правки одного
            # сообщения. Видимый текст — не всё, что уже накоплено (full_text), а
            # срез, растущий по оценённой скорости печати этой модели (см.
            # lumen_typing_pace.py) — реальному приходу кусков он "верит" только
            # как верхней границе (min(...)): если модель прислала текст МЕДЛЕННЕЕ
            # оценённой скорости, показывается всё, что реально пришло, без
            # искусственного придерживания; лимитирует именно случай, когда бэкенд
            # присылает крупными редкими кусками быстрее, чем "читалось" бы вслух.
            now = time.monotonic()
            target_full = chunks[-1] if chunks else ""
            typing_speed = _get_typing_speed(pace_key)
            reveal_len = min(len(target_full), max(0, int((now - current_chunk_start_ts) * typing_speed)))
            current_chunk_text = target_full[:reveal_len]
            if now - last_edit_ts >= bot.STREAM_EDIT_MIN_INTERVAL_SEC and current_chunk_text != last_edited_plain:
                await bot._tg_call(sent_messages[-1].edit_text, current_chunk_text, parse_mode=None, call_timeout=15.0)
                last_edited_plain = current_chunk_text
                last_edit_ts = now

        if not full_text.strip():
            # Стрим завершился, но не прислал ни одного символа текста — считаем
            # попытку неудавшейся. Плейсхолдер НЕ удаляем (см. докстринг) — отдаём
            # его вызывающему коду, вдруг пригодится для следующей модели.
            return None, sent_messages[-1]

        # Замер РЕАЛЬНОЙ скорости бэкенда — обязательно ДО фазы "довывода" ниже,
        # иначе самим же добавленная пауза исказила бы будущую оценку скорости
        # этой модели (см. докстринг record_observed_speed в lumen_typing_pace.py).
        _record_typing_speed(pace_key, time.monotonic() - overall_start_ts, len(full_text))
        # Замер задержки модели для умного роутера (см. lumen_model_speed.py) —
        # рядом с замером скорости печати, из тех же меток времени. first_piece_ts
        # здесь уже точно установлен (успех означает ≥1 кусок). Catch-up паузы
        # сознательно ВКЛЮЧЕНЫ в total: для решения "кого ставить первым" важна
        # задержка, видимая пользователем, а не только время бэкенда.
        _record_model_latency(
            _model_speed_key(provider, model_id),
            total_sec=time.monotonic() - overall_start_ts,
            ttf_sec=(first_piece_ts - overall_start_ts) if first_piece_ts is not None else None,
        )

        # "Довывод" остатка последнего сообщения, который стрим уже прислал
        # целиком, но пейсинг выше ещё не успел показать (частый случай для
        # бэкендов, присылающих готовый текст одним большим SSE-куском в конце —
        # см. lumen_typing_pace.py) — без этого пользователь увидел бы "…" почти
        # до самого конца, а затем весь ответ разом. Ограничено по построению
        # (см. catchup_reveal_steps) сверху STREAM_TYPING_MAX_CATCHUP_TICKS *
        # STREAM_TYPING_TICK_SEC секунд — не тянет отправку ответа надолго.
        final_chunks = _split_text_chunks(full_text, bot.TG_MAX_LEN)
        target_full = final_chunks[-1]
        already_shown_len = len(last_edited_plain) if last_edited_plain and target_full.startswith(last_edited_plain) else 0
        remaining_len = len(target_full) - already_shown_len
        if remaining_len > 0:
            typing_speed = _get_typing_speed(pace_key)
            for step_len in _typing_catchup_steps(remaining_len, typing_speed, bot.STREAM_TYPING_TICK_SEC, bot.STREAM_TYPING_MAX_CATCHUP_TICKS):
                await bot._typing_sleep(bot.STREAM_TYPING_TICK_SEC)
                current_chunk_text = target_full[:already_shown_len + step_len]
                if current_chunk_text != last_edited_plain:
                    await bot._tg_call(sent_messages[-1].edit_text, current_chunk_text, parse_mode=None, call_timeout=15.0)
                    last_edited_plain = current_chunk_text

        # Финальный сброс последнего сообщения — уже с полной HTML-конвертацией
        # markdown (во время стрима сознательно показывался голый текст: частичный
        # markdown при редактировании мог бы дать несбалансированные теги и сломать
        # parse_mode=HTML на промежуточных правках).
        final_text = final_chunks[-1]
        # _edit_message_quietly уже инкапсулирует тот же HTML->plain fallback,
        # что здесь раньше был продублирован вручную (см. аудит техдолга) —
        # **kwargs проходит через неё прямиком в _tg_call, поэтому call_timeout
        # передаётся без изменений в сигнатуре самой _edit_message_quietly.
        await bot._edit_message_quietly(sent_messages[-1], final_text, call_timeout=15.0)

    except Exception as exc:
        if not full_text.strip():
            # ВАЖНО: плейсхолдер больше НЕ удаляется здесь (в отличие от старой
            # версии) — см. докстринг функции про переиспользование сообщения.
            log.warning('[stream] Stream %s/%s failed before showing any content, falling back to a regular call: %s', provider, model_id, exc)
            return None, (sent_messages[-1] if sent_messages else None)
        log.warning('[stream] Stream %s/%s failed after partially showing the response, finishing as-is: %s', provider, model_id, exc)
        if _detect_identity_leak(full_text):
            # На практике сюда почти невозможно попасть (см. проверку сразу после
            # каждого куска выше) — оставлено как последняя страховка на случай бага
            # в основной проверке, а не полагаясь только на один рубеж.
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

    # Пустой full_text сюда не доходит (проверка выше возвращает None раньше),
    # поэтому фолбэка "Empty response" больше нет — мёртвый код убран.
    final_answer = _scrub_identity_leak(full_text.strip(), source=f"{provider}_stream_final:{model_id}")
    hist.append({"role": "user", "content": _history_user_text(user_text)})
    hist.append({"role": "assistant", "content": final_answer})
    if len(hist) > bot.SHARED_HISTORY_MAX_LEN:
        del hist[:-bot.SHARED_HISTORY_MAX_LEN]
    ctx.clear()
    bot._record_quota_usage(provider, model_id)
    return final_answer, None

async def _try_gemini_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    """Тонкая обёртка над _run_streaming_reply для Gemini: строит contents/config,
    специфичные для Gemini API, и передаёт их в общую (провайдер-агностичную)
    реализацию стриминга. Возвращает (ответ, плейсхолдер) — см. _run_streaming_reply."""
    import bot
    conf = GEMINI_MODELS.get(model_id, {})
    if not conf.get("stream", True):
        return None, None
    contents = await bot._build_gemini_turn_contents(chat_id, user_text)
    call_contents, gconfig = bot._build_gemini_call_config(model_id, contents)
    piece_agen = _gemini_stream_pieces(model_id, call_contents, gconfig)
    return await _run_streaming_reply(chat_id, user_text, message, provider="gemini", model_id=model_id, piece_agen=piece_agen)

async def _try_openrouter_streaming(chat_id: int, user_text: str, message: Message, model_id: str) -> tuple[str | None, Message | None]:
    """Тонкая обёртка над _run_streaming_reply для OpenRouter — тот же принцип,
    что и _try_gemini_streaming, но с SSE-стримингом через chat/completions."""
    import bot
    messages = bot._build_openrouter_turn_messages(chat_id, user_text, model_id)
    # Генератор — через bot.: тесты подменяют bot._openrouter_stream_pieces фейком.
    piece_agen = bot._openrouter_stream_pieces(model_id, messages)
    return await _run_streaming_reply(chat_id, user_text, message, provider="openrouter", model_id=model_id, piece_agen=piece_agen)
