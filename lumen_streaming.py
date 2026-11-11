"""
lumen_streaming.py — провайдер-агностичный стриминг (плейсхолдер, точки, троттлинг правок, скраб утечек на чанк, catch-up). Связи с bot.py — только через отложенный `import bot`; точки подмены для тестов читаются через `bot.` в момент вызова.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from typing import Any
import aiohttp
from aiogram.enums import ChatType, ParseMode
from aiogram.types import Message

from lumen_errors import _is_empty_like
from lumen_formatting import _md_to_html, _split_text_chunks, _strip_provider_artifacts, _tg_len, _truncate_html_to_fit
from lumen_lang import t as _lang_t
from lumen_message_parse import _history_user_text
import lumen_memory
from lumen_model_speed import (
    speed_key as _model_speed_key,
    record_response as _record_model_latency,
    first_chunk_limit_sec as _model_first_chunk_limit,
)
from lumen_router_config import GEMINI_MODELS
from lumen_search import (
    _SearchRequested,
    search_marker_state,
    has_leading_search_marker,
    strip_search_marker,
)
from lumen_security import (
    _leak_scan_window,
    _IDENTITY_LEAK_FALLBACK,
    _INJECTED_PAYLOAD_ECHO_FALLBACK,
    _detect_injected_payload_echo,
    _detect_garbled_mix,
    _is_garbled_echo,
    _detect_identity_leak,
    _polish_answer,
    _polished_prefix_len,
)
from lumen_typing_pace import (
    speed_key as _typing_speed_key,
    record_observed_speed as _record_typing_speed,
    catchup_reveal_steps as _typing_catchup_steps,
    catchup_tick_budget as _catchup_tick_budget,
    blend_arrival_speed as _typing_arrival_update,
    display_speed_for as _typing_display_speed,
    arm_edit_gate as _pace_arm_edit_gate,
    take_edit_slot as _pace_take_edit_slot,
)
from lumen_status import _spinner_glyph, thought_block

log = logging.getLogger("bot")


def _append_note_fit(md_text: str, note: str, limit: int) -> str:
    """HTML куска, уже обрезанного под лимит Telegram, + пометка: резать надо ТЕЛО,
    а не конкат — иначе строка вылезает за 4096, Telegram отвечает 400, и сама
    пометка («обрвалось» / «без живого поиска») теряется (аудит 01.11.2026)."""
    note_html = _md_to_html(note)
    return _truncate_html_to_fit(md_text, limit - len(note_html) - 1) + "\n" + note_html


# Первая правка стрима показывает минимум столько символов: у Claude индикатор
# живёт до первой буквы, а дальше сразу идёт текст; нулевой первый кадр ловил
# замёрзший «Думаю…» на целый интервал маляра (ревью 11.11.2026).
FIRST_PAINT_MIN_CHARS = 36
# ── partial-safe разметка для промежуточных правок (ревью 11.11.2026) ──
# Голый markdown в пузыре показывал пользователю «**» и «```», а финал ещё и
# менял форматирование — двойной reflow. На каждый кадр закрываем то, что уже
# закрыться может, а висячие маркеры и оборванную ссылку режем до безопасной
# границы: HTML правки тогда стабилен от кадра к кадру.
_PARTIAL_LINK_URL_RE = re.compile(r"\[([^\[\]\n]+)\]\([^)\n]*\Z")
_PARTIAL_LINK_LABEL_RE = re.compile(r"\[([^\[\]\n]*)\Z")
_PARTIAL_TRAILING_MARKERS_RE = re.compile(r"(?:[*_~]+|`+)\s*\Z")


def _close_partial_markdown(text: str) -> str:
    if text.count("```") % 2:
        # Незакрытый блок кода: если язык/тело ещё не дошли — режем сам маркер,
        # иначе дозакрываем: висячие ``` иначе видны голыми.
        head, _sep, tail = text.rpartition("```")
        if "\n" not in tail:
            return head.rstrip("` \n")
        return (text + "```") if text.endswith("\n") else (text + "\n```")
    # Висячий одиночный backtick: незакрытый inline-код показываем содержимым
    # без маркера (маркер — ОТКРЫВАЮЩИЙ, он последний из оставшихся).
    if text.count("`") % 2:
        i = text.rfind("`")
        text = text[:i] + text[i + 1:]
    text = _PARTIAL_LINK_URL_RE.sub(r"\1", text)
    text = _PARTIAL_LINK_LABEL_RE.sub(r"\1", text)
    text = _PARTIAL_TRAILING_MARKERS_RE.sub("", text)
    # Незакрытый bold дописываем маркером вниз: текст под ним остаётся жирным,
    # как при живом наборе.
    if len(re.findall(r"\*\*", text)) % 2:
        text += "**"
    return text


def _stream_reveal_html(md_text: str, limit: int) -> str:
    """HTML уже показанного среза: закрытые конструкции конвертируются, хвостовая
    недобранная разметка не видна. Если из-за дозакрывающих тегов кадр не влезает
    в лимит — общая граница по исходнику (тот же приём, что у _truncate_html_to_fit)."""
    if not md_text:
        return ""
    html_text = _md_to_html(_close_partial_markdown(md_text))
    if _tg_len(html_text) > limit:
        return _truncate_html_to_fit(md_text, limit)
    return html_text


def _stream_editor(message: Message):
    """edit_text для показанных стримом сообщений. _tg_call возвращает голую None
    и для сетевого сбоя, и для «message is not modified» — валидного ответа API,
    где содержимое уже на экране. Транспорт не трогаем (чужой scope), различие
    ловим здесь, до проглатывания исключения: not-modified — семантический успех.
    Иначе серия «сбоев» считала сообщение мёртвым и весь ответ уезжал дублём
    поверх показанного (ревью 11.11.2026). Непроверенным остаётся None после
    внутренних ретраев _tg_call (настоящая сеть) — он, как и раньше, считается
    сбоем серии."""
    async def edit_text(text, **kwargs):
        try:
            return await message.edit_text(text, **kwargs)
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return message
            raise
    return edit_text


async def _tick_waiting_dots(placeholder: Message, *, started: float | None = None) -> None:
    """Фраза ожидания до первого куска (как в Claude): «Думаю…» с анимацией, не голые точки. Первый кадр после паузы (быстрые модели анимации не видят), дальше кадр по тику. Правки через _tg_call с глотанием ошибок, отмена — штатная остановка."""
    import bot
    # started — общий старт ожидания маршрута: после сбоя одной модели таймер
    # продолжает идти, а не откатывается к нулю (ревью 28.10.2026).
    started = started if started is not None else time.monotonic()
    try:
        await bot._dots_sleep(bot._DOTS_START_AFTER_SEC)
        chat = getattr(placeholder, "chat", None)
        lang = bot._chat_lang(getattr(chat, "id", None))
        # Первый кадр (∴) уже стоит в отправленном плейсхолдере: начинаем со
        # второго, иначе 0.5с спустя индикатор «замирает» на том же символе.
        frame_idx = 1
        while True:
            elapsed = max(0, int(time.monotonic() - started))
            glyph = _spinner_glyph(frame_idx)
            # Тикер и стрим делят один бюджет правок на чат (ревью 11.11.2026);
            # до первой буквы индикатор обязан оставаться живым (пункт 2).
            if _pace_take_edit_slot(getattr(chat, "id", None)):
                with contextlib.suppress(Exception):
                    await bot._tg_call(
                        placeholder.edit_text, glyph + " " + _lang_t(lang, "progress_thinking", sec=elapsed),
                        parse_mode=None, call_timeout=15.0,
                    )
            frame_idx += 1
            await bot._dots_sleep(bot._DOTS_TICK_SEC)
    except asyncio.CancelledError:
        raise


async def _pieces_with_waiting_feedback(piece_agen, placeholder: Message, *, first_chunk_limit: float, first_task=None, status=None, wait_started: float | None = None):
    """Генератор + ожидание первого куска: адаптивный предел (зависшая попытка — TimeoutError, плейсхолдер уйдёт следующей модели) и живой статус строго до первого yield. Пустой стрим — просто конец.

    status — живой статус из lumen_status.StatusMessage: его тикер уже рисует
    «Думаю… N с», поэтому отдельные точки не запускаются. Без статуса (группы)
    работает тот же тикер, но с фразой вместо голых точек.

    first_task — уже запущенный запрос первого куска (см. _run_streaming_reply):
    он стартует параллельно с отправкой плейсхолдера."""
    dots_task = None if status is not None else asyncio.create_task(
        _tick_waiting_dots(placeholder, started=wait_started)
    )
    try:
        try:
            pending = first_task if first_task is not None else piece_agen.__anext__()
            first = await asyncio.wait_for(pending, timeout=first_chunk_limit)
        except StopAsyncIteration:
            return
        finally:
            if dots_task is not None:
                dots_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dots_task
        if status is not None:
            # Первый кусок пришёл: фиксируем «Думал N с» и гасим анимацию —
            # текст сейчас заменит статус.
            await status.finish()
        yield first
        async for piece in piece_agen:
            yield piece
    finally:
        aclose = getattr(piece_agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

def _remaining_budget(deadline: float | None) -> float:
    """Сколько секунд осталось до бюджета маршрута (float('inf') — потолка нет)."""
    if deadline is None:
        return float("inf")
    return max(0.0, deadline - time.monotonic())

def _stream_wait(bot: Any, deadline: float | None) -> float:
    """Таймаут на ОДНУ операцию стрима: не больше межкускового капа и не больше
    остатка бюджета маршрута. Раньше ожидание шло в обход бюджета, и кусок мог ждать
    STREAM_CHUNK_TIMEOUT_SEC уже после его исчерпания (аудит 26.09.2026). Значение
    меньше 0.1с не опускаем: это всё равно мгновенный TimeoutError."""
    return max(0.1, min(bot.STREAM_CHUNK_TIMEOUT_SEC, _remaining_budget(deadline)))

async def _gemini_stream_pieces(model_id: str, call_contents: list, gconfig, *, deadline: float | None = None):
    """Куски от Gemini (тонкая обёртка над generate_content_stream).

    В google-genai 2.27.0 это async def: вызов надо ждать (пример из SDK:
    `async for chunk in await client.aio.models.generate_content_stream(...)`).
    Реальную сетевую жду ловлю ниже: первый кусок идёт через __anext__ под тем же _stream_wait."""
    import bot
    stream = await bot.client.aio.models.generate_content_stream(model=model_id, contents=call_contents, config=gconfig)
    stream_iter = stream.__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=_stream_wait(bot, deadline))
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

async def _sse_pieces(
    session, url: str, headers: dict[str, str], payload: dict, *, err_cls, provider_label: str,
    deadline: float | None = None,
):
    """Общий разбор OpenAI-совместимого SSE (Munito даёт
    `data: {"choices":[{"delta":{"content":...}}]}` и финальный `data: [DONE]`).

    Раньше это были несколько копий по 45 строк, расходившихся деталями: в одной не
    было теста на HTTP-ошибку и на ошибку внутри чанка (аудит 26.09.2026). Ошибка
    внутри чанка (HTTP 200, стрим уже открыт) поднимается: ранний сбой — откат на
    следующую модель, поздний — пометка "соединение прервалось"."""
    import bot
    post_cm = session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=None, connect=12.0))
    try:
        # Заголовки без total висели вечно и держали lock чата до рестарта (аудит 29.09.2026),
        # поэтому вход ждём не дольше попытки маршрута, а тело стрима — по-прежнему покусочно.
        resp = await asyncio.wait_for(post_cm.__aenter__(), timeout=bot._attempt_timeout(bot, deadline))
    except BaseException:
        with contextlib.suppress(Exception):
            await post_cm.__aexit__(None, None, None)
        raise
    try:
        if resp.status >= 400:
            # Тело ошибки читаем с капом: 200-OK путь ограничивает каждый read, а
            # здесь провайдер мог вернуть многумегабайтный HTML (аудит run-2).
            body = await resp.content.read(65536)
            raise err_cls(f"HTTP {resp.status}: {body[:300]!r}", status_code=resp.status)
        line_iter = resp.content.__aiter__()
        while True:
            try:
                raw_line = await asyncio.wait_for(line_iter.__anext__(), timeout=_stream_wait(bot, deadline))
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
            err_obj = obj.get("error")
            if err_obj:
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                raise err_cls(err_msg or f"{provider_label} returned an error in the stream body", status_code=err_code)
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece
    finally:
        with contextlib.suppress(Exception):
            await post_cm.__aexit__(None, None, None)

async def _remember_turn(
    state: dict, history_text, user_text: str, answer: str, history_meta: dict | None,
) -> None:
    """Пишет пару вопрос-ответ в общий лог и запускает сводку при переполнении."""
    import bot
    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    lumen_memory.append_assistant(state, answer)
    await bot._trim_history(state["history"], state)


async def _munito_stream_pieces(model_id: str, messages: list[dict], *, deadline: float | None = None):
    """Куски от Munito AI: тот же OpenAI-SSE через общий _sse_pieces (замер 05.11.2026: первый токен 0.24с у головы цепочки)."""
    import bot
    if not bot.MUNITO_API_KEY:
        raise bot.MunitoAPIError("MUNITO_API_KEY is not set")
    headers = {
        "Authorization": f"Bearer {bot.MUNITO_API_KEY}",
        "Content-Type": "application/json",
    }
    session = await bot._get_http_session()
    from lumen_router_config import _munito_payload_extras
    async for piece in _sse_pieces(
        session, f"{bot.MUNITO_BASE_URL}/chat/completions", headers,
        {"model": model_id, "messages": messages, "stream": True, **_munito_payload_extras()},
        err_cls=bot.MunitoAPIError, provider_label="Munito", deadline=deadline,
    ):
        yield piece

async def _run_streaming_reply(
    chat_id: int, user_text: str, message: Message, *, provider: str, model_id: str, piece_agen,
    deadline: float | None = None, status=None, history_text=None,
    reuse_placeholder: Message | None = None, wait_started: float | None = None,
    history_meta: dict | None = None,
) -> tuple[str | None, Message | None]:
    """Стриминг: генератор кусков + плейсхолдер, чанкинг, троттлинг, обрыв при утечке/инъекции, HTML-финал, запись в историю. Возвращает (ответ, плейсхолдер): успех — (текст, None); сбой до показа — (None, плейсхолдер для следующей модели); сбой после — (показанное + пометка, None)."""
    import bot
    state = bot.get_state(chat_id)

    sent_messages: list[Message] = []
    full_text = ""
    # Границы финализированных кусков замораживаются один раз (ревью 11.11.2026):
    # _split_text_chunks пересчитывается только для хвоста full_text[covered_len:].
    # Прежний полный пересчёт сдвигал границы уже показанных кусков, текст перетекал
    # между пузырями, а правка их чинила (_resync_finalized, аудит 01.11.2026) —
    # вместе с пересчётом убран и resync. covered_len считается по длине кусков,
    # пробелы на стыках чанкер срезает с обеих сторон — покрытие не теряется.
    frozen: list[str] = []
    covered_len = 0
    tail_chunks: list[str] = []
    tail_basis_len = 0
    edit_fail_streak = 0
    edits_dead = False
    # last_painted_plain — md-срез, который реально стоит на экране растущего
    # сообщения; last_painted_html — отправленный HTML того же кадра: равенство
    # отбивает и повторные правки (coalescing), и заведомый «message is not modified».
    last_painted_plain = ""
    last_painted_html = ""
    # Счётчики плавности для прода: сколько кадров реально доехало и каким был
    # самый большой шаг. По ним судим, похож набор на Claude или идёт рывками
    # (кадры getUpdates свёрнуты Telegram в один, локально это не измерить).
    paint_frames = 0
    paint_max_step = 0
    paint_html_broken = False
    first_piece_ts: float | None = None
    # header_md — короткая activity-строка «✻ Думал N с» над первым пузырём с
    # первой же правки, у Claude она не исчезает (пункт 8, ревью 11.11.2026);
    # пустая при sec < 1 (пункт 11). Считается один раз на первом куске.
    header_md = ""
    # Замеры скорости/латентности — см. lumen_typing_pace.py (overall один раз на весь ответ, arrival — заново на каждое новое сообщение).
    pace_key = _typing_speed_key(provider, model_id)
    overall_start_ts = time.monotonic()
    # reveal_base_ts — момент ПЕРВОГО куска, а не старта стрима: иначе после долгой тишины формула сразу разрешала показать всё накопленное разом. last_piece_ts/arrival_ewma — замер реального темпа прихода.
    reveal_base_ts: float | None = None
    last_piece_ts: float | None = None
    arrival_ewma: float | None = None
    is_group = getattr(getattr(message, "chat", None), "type", None) != ChatType.PRIVATE
    # Кадр маляра: в группе бюджет правок делится на всех участников (пункт 12).
    paint_interval = bot.STREAM_PAINT_TICK_GROUP_SEC if is_group else bot.STREAM_PAINT_TICK_SEC
    gate_interval = bot.STREAM_EDIT_MIN_INTERVAL_GROUP_SEC if is_group else bot.STREAM_EDIT_MIN_INTERVAL_SEC

    def _header_html() -> str:
        return _md_to_html(header_md) if header_md else ""

    def _final_chunk_html(chunk_text: str, idx: int) -> str:
        # Заголовок живёт на первом пузыре и обязан пережить его финализацию,
        # иначе он исчезнет вместе с последней правкой куска 0 (пункт 8).
        head = _header_html()
        return (head + "\n\n" + _md_to_html(chunk_text)) if (idx == 0 and head) else _md_to_html(chunk_text)

    def _reveal_len(target: str, min_reveal: int) -> int:
        now = time.monotonic()
        typing_speed = _typing_display_speed(arrival_ewma, pace_key)
        reveal = min(len(target), max(0, int((now - (reveal_base_ts or now)) * typing_speed)))
        # Показ не откатывается назад, если граница хвоста сдвинулась при пересчёте.
        shown = len(last_painted_plain) if target.startswith(last_painted_plain) and last_painted_plain else 0
        return max(reveal, min_reveal, shown)

    async def _paint(*, force: bool = False, min_reveal: int = 0) -> None:
        """Кадр маляра: рисуем по таймеру из общего буфера, а не по приходу куска
        (пункт 1). Правки последовательны в этом цикле: пока одна в пути, новых не
        начинается, а следующий кадр берёт самое свежее состояние — coalescing,
        он же защита от мигания и лишних 429."""
        nonlocal edit_fail_streak, edits_dead, last_painted_plain, last_painted_html, paint_html_broken, paint_frames, paint_max_step
        if edits_dead or not tail_chunks:
            return
        target = tail_chunks[-1]
        if not target:
            return
        slice_md = target[:_reveal_len(target, min_reveal)]
        if slice_md == last_painted_plain:
            return
        head = _header_html() if len(sent_messages) == 1 else ""
        body_html = _stream_reveal_html(slice_md, bot.TG_MAX_LEN - (len(head) + 2))
        html_out = head + "\n\n" + body_html if head else body_html
        if html_out == last_painted_html:
            return
        if not _pace_take_edit_slot(chat_id, force=force):
            return
        if paint_html_broken:
            parse_mode, payload = None, slice_md
        else:
            parse_mode, payload = ParseMode.HTML, html_out
        edit_ok = await bot._tg_call(_stream_editor(sent_messages[-1]), payload, parse_mode=parse_mode, call_timeout=15.0)
        if edit_ok is not None:
            edit_fail_streak = 0
            paint_frames += 1
            paint_max_step = max(paint_max_step, len(slice_md) - len(last_painted_plain))
            last_painted_plain = slice_md
            last_painted_html = html_out
            return
        if parse_mode is ParseMode.HTML:
            # HTML-парсер Telegram отверг частичную разметку — это не мёртвое
            # сообщение: следующий кадр пробуем голым текстом (пункт 7).
            paint_html_broken = True
            return
        edit_fail_streak += 1
        edits_dead = edit_fail_streak >= 3

    # Первый кусок запрашиваем ДО плейсхолдера: отправка "…" — отдельный круг
    # к Telegram через прокси, и запрос к модели простаивал бы весь этот круг.
    # Кадр маляра в пути — сюда, а не внутрь try: finally должен видеть его всегда.
    paint_task: asyncio.Future | None = None
    first_task = asyncio.ensure_future(piece_agen.__anext__())
    read_task: asyncio.Future | None = None
    try:
        if reuse_placeholder is not None:
            # Резервная модель после раннего сбоя стрима: тот же пузырёк,
            # второй «Думаю…» в чате не нужен (аудит 28.10.2026).
            placeholder = reuse_placeholder
        elif status is not None:
            # Статус-пузырь уже в чате: переиспользуем его, второй не отправляем.
            placeholder = status.message
        else:
            placeholder = await bot._tg_call(
                bot._answer_sender(message), _spinner_glyph(0) + " " + bot._t(message.chat.id, "progress_thinking", sec=0),
                parse_mode=None, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
            )
        if placeholder is None:
            return None, None
        sent_messages.append(placeholder)
        # Попытка к модели состоялась (запрос уже ушёл в генератор кусков);
        # пустой считают только ранний обрыв до показа — частичный ответ показан.
        bot._record_stats_event("model_attempts")

        first_chunk_limit = _model_first_chunk_limit(_model_speed_key(provider, model_id), bot.FIRST_CHUNK_TIMEOUT_SEC)
        if deadline is not None:
            # Первый кусок не должен ждать дольше общего бюджета сообщения: до него
            # дедлайн не проверялся и ожидание могло выйти за бюджет (аудит 27.10.2026).
            first_chunk_limit = min(first_chunk_limit, max(0.5, deadline - time.monotonic()))
        piece_agen = _pieces_with_waiting_feedback(
            piece_agen, placeholder,
            first_chunk_limit=first_chunk_limit,
            first_task=first_task,
            status=status,
            wait_started=wait_started,
        )
        # Цикл мотает read_task с таймаутом до следующего кадра: кусок пришёл —
        # обрабатываем, таймер истёк — рисуем из буфера. Одна последовательная
        # задача вместо параллельного маляра: правки не набегают на финализации,
        # между await виден только текст, уже просканированный leak-проверкой
        # (она идёт до первой точки ожидания после full_text +=).
        iterator = piece_agen.__aiter__()
        next_paint_ts = 0.0
        # Кадр маляра идёт отдельной задачей: правка — это круг к Telegram через
        # прокси (0,5-0,7 с), и пока она в пути, цикл читал бы куски, ничего не
        # показывая. Прод 11.11.2026 мерил ровно 1 кадр в секунду вместо тика в
        # 0,55 с; в пути держим не больше одной правки, следующая берёт самое
        # свежее состояние (coalescing), перед финализацией обязательно ждём.
        def _schedule_paint(**kwargs) -> None:
            nonlocal paint_task
            if paint_task is not None and not paint_task.done():
                return
            paint_task = asyncio.ensure_future(_paint(**kwargs))

        async def _await_paint() -> None:
            nonlocal paint_task
            if paint_task is not None and not paint_task.done():
                await paint_task
            paint_task = None
        while True:
            if read_task is None:
                read_task = asyncio.ensure_future(iterator.__anext__())
            if first_piece_ts is None:
                wait_left = None
            else:
                wait_left = max(0.0, next_paint_ts - time.monotonic())
            done, _pending = await asyncio.wait({read_task}, timeout=wait_left)
            if read_task not in done:
                now_tick = time.monotonic()
                if deadline is not None and now_tick > deadline:
                    raise bot.RouteBudgetExceededError([f"{provider}:{model_id}"])
                _schedule_paint()
                next_paint_ts = now_tick + paint_interval
                continue
            try:
                piece = read_task.result()
            except StopAsyncIteration:
                break
            read_task = None
            if not piece:
                continue
            now_piece = time.monotonic()
            # Общий бюджет маршрута — и на стрим, но живой стрим не режем по
            # 40-секундному капит: прод 11.11.2026 оборвал им 5700-символьный ответ
            # на 35-й секунде, и человек видел «Соединение прервалось». Пока куски
            # прибывают, бюджет продлевается на grace-окно с потолком ниже 60 с,
            # которые срезает релей; lock чата по-прежнему держится MESSAGE_TOTAL_BUDGET_SEC.
            if deadline is not None:
                deadline = min(overall_start_ts + bot.STREAM_ROUTE_MAX_SEC,
                               now_piece + bot.STREAM_ROUTE_STALL_GRACE_SEC)
                if now_piece > deadline:
                    raise bot.RouteBudgetExceededError([f"{provider}:{model_id}"])
            first_piece = first_piece_ts is None
            if first_piece:
                first_piece_ts = now_piece
                reveal_base_ts = now_piece
                _pace_arm_edit_gate(chat_id, gate_interval)
                # Activity-строка ставится один раз на первом куске: sec < 1 —
                # пусто, как в Claude (пункты 8 и 11, ревью 11.11.2026).
                if status is not None:
                    header_md = status.paint_header_md()
                else:
                    thought_sec = int(max(0.0, now_piece - overall_start_ts))
                    if thought_sec >= 1:
                        header_md = "*✻ " + _lang_t(bot._chat_lang(message.chat.id), "progress_thought_for", sec=thought_sec) + "*"
            else:
                arrival_ewma = _typing_arrival_update(arrival_ewma, len(piece), now_piece - (last_piece_ts or now_piece))
            last_piece_ts = now_piece
            full_text += piece

            marker_state = search_marker_state(full_text)
            if marker_state == "pending":
                continue
            if marker_state == "requested":
                # Модель просит поиск: уходим ДО первой правки — в чате ни кадра
                # с маркером, пузырёк остаётся живым для прохода со сниппетами.
                log.info('[stream] %s/%s asked for a web search instead of answering', provider, model_id)
                raise _SearchRequested(sent_messages[-1] if sent_messages else None)
            if has_leading_search_marker(full_text):
                # Маркер плюс текст: служебную строку не показываем, ответ остаётся.
                full_text = strip_search_marker(full_text)

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
                    '[%s] Stream %s/%s started leaking internal details/echoing an injected instruction — aborting the stream and showing a neutral reply instead (accumulated_len=%d)', tag, provider, model_id, len(full_text),
                )
                aclose = getattr(piece_agen, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                final_answer = _IDENTITY_LEAK_FALLBACK if leak_kind == "identity" else _INJECTED_PAYLOAD_ECHO_FALLBACK
                await bot._tg_call(sent_messages[-1].edit_text, final_answer, parse_mode=None, call_timeout=15.0)
                await _remember_turn(state, history_text, user_text, final_answer, history_meta)
                bot._record_quota_usage(provider, model_id)
                return final_answer, None

            if not tail_chunks or len(full_text) - covered_len - tail_basis_len >= 1000:
                tail_chunks = _split_text_chunks(full_text[covered_len:], bot.TG_MAX_LEN)
                tail_basis_len = len(full_text) - covered_len
            # Чанки кроме последнего (растущего) финализируем тем же разбиением, что у нестримленных ответов.
            while len(frozen) + len(tail_chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                chunk_text = tail_chunks[0]
                if not chunk_text.strip():
                    # Граница съела весь остаток (хвост из одних пробелов):
                    # continuation не нужен, хвост допишет финал.
                    break
                frozen.append(chunk_text)
                covered_len += len(chunk_text)
                tail_chunks = _split_text_chunks(full_text[covered_len:], bot.TG_MAX_LEN)
                tail_basis_len = len(full_text) - covered_len
                # Continuation открывается ПЕРВЫМ реальным срезом, а не «…»:
                # строка-заглушка и правка следом — видимое мигание (пункт 6).
                open_md = tail_chunks[-1][:FIRST_PAINT_MIN_CHARS] if tail_chunks else ""
                open_html = _stream_reveal_html(open_md, bot.TG_MAX_LEN) if open_md else "…"
                # bot.bot — инстанс aiogram Bot (имя bot занято модулем).
                new_msg = await bot._tg_call(
                    bot.bot.send_message, chat_id=message.chat.id, text=open_html,
                    parse_mode=ParseMode.HTML, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                )
                if new_msg is None:
                    # idx ещё НЕ финализирован — не даём общему except перезаписать показанный текст чужим (там инвариант "sent[-1] == chunks[-1]" уже неверен).
                    note = bot._t(message.chat.id, "stream_note_send_fail")
                    with contextlib.suppress(Exception):
                        # Пометка важнее хвоста куска (см. _append_note_fit).
                        await bot._tg_call(sent_messages[idx].edit_text, _append_note_fit(chunk_text, note, bot.TG_MAX_LEN), parse_mode=ParseMode.HTML, call_timeout=15.0)
                    final_answer = full_text.strip()
                    await _remember_turn(state, history_text, user_text, final_answer, history_meta)
                    bot._record_quota_usage(provider, model_id)
                    return final_answer, None
                if not edits_dead:
                    # Финализация — обязательная правка: границу заморозили, текст
                    # обязан догнать её сразу (force берёт слот).
                    edit_ok = await bot._tg_call(
                        _stream_editor(sent_messages[idx]), _final_chunk_html(chunk_text, idx),
                        parse_mode=ParseMode.HTML, call_timeout=15.0,
                    )
                    # Серия неуспешных правок подряд — сообщение снесли посреди стрима:
                    # дальше не правим, в финале досылаем ответ новым сообщением.
                    edit_fail_streak = 0 if edit_ok is not None else edit_fail_streak + 1
                    edits_dead = edit_fail_streak >= 3
                sent_messages.append(new_msg)
                # Новое сообщение уже открыто первым срезом: повторная правка не нужна.
                last_painted_plain = open_md
                last_painted_html = open_html
                # Новое сообщение — новый "лист", показ в нём начинается с этого среза.
                reveal_base_ts = time.monotonic()
                next_paint_ts = reveal_base_ts + paint_interval

            # Первый кадр — сразу, от FIRST_PAINT_MIN_CHARS символов: замёрзший
            # «Думаю…» до целого интервала больше не ловится (пункт 2). Остальные —
            # по таймеру, если кусок приполз раньше тика, лишняя правка не делается.
            if first_piece:
                await _paint(force=True, min_reveal=FIRST_PAINT_MIN_CHARS)
                next_paint_ts = time.monotonic() + paint_interval
            elif time.monotonic() >= next_paint_ts:
                _schedule_paint()
                next_paint_ts = time.monotonic() + paint_interval

        # Правка в пути не должна переживать финал: две правки одного пузырька
        # одновременно — гонка содержимого, а не coalescing.
        await _await_paint()
        # Метки классификатора и разметка инструмента — не ответ: чат должен
        # видеть следующую модель, а не 39 символов «User Safety: safe».
        full_text = _strip_provider_artifacts(full_text)
        if _is_empty_like(full_text):
            # Пустой стрим — неудача, но плейсхолдер отдаём вызывающему коду (см. докстринг).
            return None, sent_messages[-1]

        # Замеры — ДО довывода (пауза не должна искажать оценку), catch-up паузы в total модели — сознательно (важна видимая задержка).
        _record_typing_speed(pace_key, time.monotonic() - overall_start_ts, len(full_text))
        _record_model_latency(
            _model_speed_key(provider, model_id),
            total_sec=time.monotonic() - overall_start_ts,
            ttf_sec=(first_piece_ts - overall_start_ts) if first_piece_ts is not None else None,
        )

        # Финальный скраб ДО последней правки: иначе пользователь видел ответ,
        # который в историю уже попал очищенным (аудит 28.10.2026).
        raw_full = full_text.strip()
        _polish_src = f"{provider}_stream_final:{model_id}"
        full_text = _polish_answer(raw_full, source=_polish_src)
        # "Довывод" остатка: бэкенды одним куском в конце показывали бы "…" до самого финала. Ограничено catchup-лимитом сверху.
        # Разбиение — только хвост от замороженной границы: пересчёт всего текста
        # сдвигал бы уже показанные границы (пункт 5; _resync_finalized удалён).
        # Граница считается в смещениях ДО полировки: правка укорачивает текст,
        # и срез по covered_len терял ровно столько символов, сколько снято
        # (ревью 11.11.2026: 11 символов пропали из второго пузыря).
        shown_len = covered_len
        if len(full_text) != len(raw_full):
            shown_len = _polished_prefix_len(raw_full, min(covered_len, len(raw_full)), source=_polish_src)
        final_tail = _split_text_chunks(full_text[shown_len:], bot.TG_MAX_LEN)
        final_chunks = frozen + final_tail
        # Маляр и довывод должны видеть ФИНАЛЬный хвост (со скрабом), а не
        # предфинальный: иначе catch-up кадры рисуют устаревший буфер.
        tail_chunks = final_tail
        tail_already_final = False
        if not edits_dead and len(final_chunks) > len(sent_messages):
            # Инкрементальное разбиение отстаёт на 1000 символов: к финалу
            # мог появиться новый кусок, и последняя правка затирала ответ
            # хвостом. Досылаем недостающие сообщения (аудит 28.10.2026).
            while len(final_chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                edit_ok = await bot._tg_call(
                    _stream_editor(sent_messages[idx]), _final_chunk_html(final_chunks[idx], idx),
                    parse_mode=ParseMode.HTML, call_timeout=15.0,
                )
                if bot.bot is None:
                    edits_dead = True
                    break
                # None от _tg_call был и флуд-429, и «message is not modified» —
                # семантическое не-действо теперь перехвачено в _stream_editor как
                # успех, в серию сбоя оно не попадает (пункт 7, ревью 11.11.2026).
                edit_fail_streak = 0 if edit_ok is not None else edit_fail_streak + 1
                edits_dead = edit_fail_streak >= 3
                if edits_dead:
                    break
                next_chunk = final_chunks[idx + 1]
                new_msg = await bot._tg_call(
                    bot.bot.send_message, chat_id=message.chat.id,
                    text=_md_to_html(next_chunk),
                    parse_mode=ParseMode.HTML, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                )
                if new_msg is None:
                    edits_dead = True
                    break
                sent_messages.append(new_msg)
                # Новое сообщение уже содержит полный кусок: catch-up для него не нужен.
                last_painted_plain = next_chunk
                last_painted_html = _md_to_html(next_chunk)
                reveal_base_ts = time.monotonic()
            tail_already_final = not edits_dead and last_painted_plain == final_chunks[-1]
        target_full = final_chunks[-1]
        already_shown_len = len(last_painted_plain) if last_painted_plain and target_full.startswith(last_painted_plain) else 0
        remaining_len = len(target_full) - already_shown_len
        if remaining_len > 0 and not edits_dead:
            typing_speed = _typing_display_speed(arrival_ewma, pace_key)
            # Бюджет довывода в тиках считаем из секунд, а не из жёстической
            # восьмёрки: иначе длинный хвост показывается несколькими прыжками.
            # Заодно не выходим за остаток бюджета сообщения.
            catchup_sec = (bot.STREAM_TYPING_MAX_CATCHUP_GROUP_SEC if is_group
                             else bot.STREAM_TYPING_MAX_CATCHUP_SEC)
            if deadline is not None:
                catchup_sec = min(catchup_sec, max(0.0, deadline - time.monotonic()))
            # Тик = пауза + правка: правка через реле стоит 0,5-0,7 с, и без
            # этого шага бюджет считался только по сну (ревью 11.11.2026).
            step_sec = bot.STREAM_TYPING_TICK_SEC + bot.STREAM_TYPING_STEP_RTT_SEC
            max_ticks = _catchup_tick_budget(catchup_sec, step_sec)
            # Времени не осталось вовсе — довывода не будет: финальная правка
            # покажет ответ целиком сразу, без паузы сверх бюджета.
            for step_len in _typing_catchup_steps(remaining_len, typing_speed, bot.STREAM_TYPING_TICK_SEC, max_ticks):
                if deadline is not None and time.monotonic() >= deadline:
                    break
                await bot._typing_sleep(bot.STREAM_TYPING_TICK_SEC)
                # Довывод идёт тем же маляром: тот же coalescing, partial-safe
                # разметка и общий бюджет правок на чат (пункты 1 и 12).
                await _paint(min_reveal=already_shown_len + step_len)

        # Финал — с полной HTML-конвертацией (частичные кадры до этого имели
        # закрытую хвостовую разметку; здесь она настоящая).
        final_text = final_chunks[-1]
        if edits_dead:
            # Правки умирали серией сетевых сбоев — сообщение снесли посреди
            # стрима: весь ответ досылаем новым сообщением (_send_text внутри
            # режет на части сам), иначе пользователь его не увидит.
            await bot._safe_reply(message, full_text.strip())
        elif len(final_chunks) == 1 and (
            status is not None or getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE
        ):
            # Одно сообщение: сверху добавляем свёрнутый блок активности,
            # как «Thought for Ns» в Claude. Длинный ответ не трогаем: заголовок
            # не влез бы в лимит первого сообщения. Без раннего статуса (обычный
            # текст) строим минимальный блок по времени до первого куска.
            if status is not None:
                html_text, plain_text = status.compose_answer(final_text)
            else:
                thought_sec = int(max(0.0, (first_piece_ts or overall_start_ts) - overall_start_ts))
                html_text, plain_text = thought_block(bot._chat_lang(message.chat.id), thought_sec, final_text)
            if html_text is None:
                # Таблицы и прочий rich-контент: обычный путь без блока активности —
                # но нарисованную во время стрима activity-строку терять нельзя.
                body = header_md + "\n\n" + final_text if header_md else final_text
                await bot._edit_message_quietly(sent_messages[-1], body, call_timeout=15.0)
            else:
                edited = await bot._tg_call(
                    sent_messages[-1].edit_text, html_text,
                    parse_mode=ParseMode.HTML, call_timeout=15.0,
                )
                if edited is None:
                    await bot._tg_call(sent_messages[-1].edit_text, plain_text, parse_mode=None, call_timeout=15.0)
        else:
            if not tail_already_final:
                # HTML->plain fallback — внутри _edit_message_quietly (раньше дублировался здесь вручную).
                # Досланный хвост уже содержит финальный HTML: повторная правка
                # получила бы "not modified" и срезала ссылки/разметку (аудит 28.10.2026).
                body = final_text
                if header_md and len(final_chunks) == 1:
                    body = header_md + "\n\n" + final_text
                await bot._edit_message_quietly(sent_messages[-1], body, call_timeout=15.0)

    except _SearchRequested:
        # Не сбой модели: остывка, карантин и model_failures тут ни при чём.
        raise
    except Exception as exc:
        if _is_empty_like(full_text):
            # 429 до первого куска — в /stats и ненадолго из роута. Суточный лимит
            # держим до утра, минутный всплеск — короткая остывка (иначе один 429
            # запирал модель до полуночи — враждебное ревью 27.09.2026).
            try:
                _txt = bot._error_text(exc).strip() or exc.__class__.__name__
                if bot._classify_model_error(bot._error_status(exc, _txt), _txt) == "rate_limit":
                    # Суточную квоту опознаёт только Gemini: у Munito лимиты оконные
                    # (200 запросов за 30 секунд), держать его 429 до утра незачем.
                    _daily = provider == "gemini" and bot._is_gemini_daily_quota(_txt)
                    if _daily:
                        bot._mark_quota_exhausted(provider, model_id)
                    else:
                        bot._mark_rate_limited(provider, model_id)
                else:
                    # 503/404 и прочие "недоступна" — остывка на тех же условиях
                    # (прод 05.10.2026: без неё пять моделей 3.x повторялись на
                    # каждом сообщении, по 22 секунды попытки впустую).
                    from lumen_routes import _mark_model_unavailable
                    _mark_model_unavailable(provider, model_id, exc)
            except Exception:
                pass
            # Плейсхолдер НЕ удаляем — возвращаем для переиспользования (см. докстринг).
            log.warning('[stream] Stream %s/%s failed before showing any content, falling back to a regular call: %s', provider, model_id, exc)
            bot._record_stats_event("model_failures")
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
                # Пометку ставим на ХВОСТ от замороженной границы: полный пересчёт
                # сдвинул бы границы и перелил показанный текст между пузырями
                # (пункт 5; раньше хвост пересобранного разбиения затирал показанное).
                tail_text = full_text[covered_len:] if frozen or covered_len else full_text
                note = bot._t(message.chat.id, "stream_note_interrupted")
                await bot._tg_call(sent_messages[-1].edit_text, _append_note_fit(tail_text, note, bot.TG_MAX_LEN), parse_mode=ParseMode.HTML, call_timeout=15.0)
            # Частичный ответ уже показан и ляжет в историю ниже — запоминаем, что
            # его оборвало: слово «продолжи» добьёт с места обрыва (_continue_after_interrupt).
            # stream_interrupted переживает успешный возврат: иначе _handle_message_core
            # снимал общий флаг и «продолжи» не срабатывал (аудит 27.10.2026).
            state["interrupted"] = True
            state["stream_interrupted"] = True
            bot.mark_state_dirty(chat_id)
    finally:
        # Ранний выход (плейсхолдер не отправился) оставляет запрос первого куска
        # висеть: гасим, иначе он живёт до таймаута провайдера. Висящий read_task
        # следующего куска гасим тем же образом до aclose генератора.
        if read_task is not None and not read_task.done():
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await read_task
        # Кадр, ушедший в сеть до раннего выхода, ждём или гасим здесь: иначе он
        # правил бы пузырёк уже после того, как ответ дописан другим путём.
        if paint_task is not None and not paint_task.done():
            paint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await paint_task
        if not first_task.done():
            first_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await first_task
        aclose = getattr(piece_agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    # Пустой full_text сюда не доходит — ветки "Empty response" нет.
    # Скраб уже применён до финальной правки (см. выше).
    final_answer = full_text
    bot._record_model_outcome(provider, model_id,
                              bad=_detect_garbled_mix(full_text.strip()) and not _is_garbled_echo(full_text.strip(), user_text))
    await _remember_turn(state, history_text, user_text, final_answer, history_meta)
    bot._record_quota_usage(provider, model_id)
    # Плавность судим по окну набора, а не по всему ответу: первый кусок мог
    # прийти на 15-й секунде, и тогда frames=4 — не рывки, а короткий хвост после
    # долгого ожидания (прод 11.11.2026: frames=4 max_step=848 sec=20.4 читалось
    # двояко: _ttft и paint_span разносят эти два случая).
    _now_end = time.monotonic()
    _ttft = (first_piece_ts - overall_start_ts) if first_piece_ts is not None else -1.0
    _paint_span = (_now_end - first_piece_ts) if first_piece_ts is not None else 0.0
    log.info("[stream] smoothness frames=%d max_step=%d chars=%d sec=%.1f ttft=%.1f paint_span=%.1f",
             paint_frames, paint_max_step, len(final_answer), _now_end - overall_start_ts, _ttft, _paint_span)
    return final_answer, None

async def _try_gemini_streaming(chat_id: int, user_text: str, message: Message, model_id: str, *, deadline: float | None = None, status=None, history_text=None, reuse_placeholder=None, wait_started=None, history_meta=None) -> tuple[str | None, Message | None]:
    """Обёртка _run_streaming_reply для Gemini (строит contents/config)."""
    import bot
    conf = GEMINI_MODELS.get(model_id, {})
    if not conf.get("stream", True):
        return None, None
    state = bot.get_state(chat_id)
    contents = await bot._build_gemini_turn_contents(chat_id, user_text, history_meta=history_meta)
    call_contents, gconfig = bot._build_gemini_call_config(
        model_id, contents, memory_summary=state.get("memory_summary") or "",
    )
    piece_agen = _gemini_stream_pieces(model_id, call_contents, gconfig, deadline=deadline)
    return await _run_streaming_reply(chat_id, user_text, message, provider="gemini", model_id=model_id, piece_agen=piece_agen, deadline=deadline, status=status, history_text=history_text, reuse_placeholder=reuse_placeholder, wait_started=wait_started, history_meta=history_meta)

async def _try_munito_streaming(chat_id: int, user_text: str, message: Message, model_id: str, *, deadline: float | None = None, status=None, history_text=None, reuse_placeholder=None, wait_started=None, history_meta=None) -> tuple[str | None, Message | None]:
    """Обёртка _run_streaming_reply для Munito (SSE через chat/completions), память без резки — см. ask_munito_text."""
    import bot
    messages = await bot._build_openai_turn_messages(
        chat_id, user_text, model_id, history_meta=history_meta,
    )
    # Генератор — через bot.: тесты подменяют bot._munito_stream_pieces фейком.
    piece_agen = bot._munito_stream_pieces(model_id, messages, deadline=deadline)
    return await _run_streaming_reply(chat_id, user_text, message, provider="munito", model_id=model_id, piece_agen=piece_agen, deadline=deadline, status=status, history_text=history_text, reuse_placeholder=reuse_placeholder, wait_started=wait_started, history_meta=history_meta)
