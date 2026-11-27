"""lumen_streaming.py — provider-agnostic streaming (placeholder, dots, edit throttling, per-chunk leak scrubbing, catch-up). The only connection to bot.py is a deferred `import bot`; test seams are read through `bot.` at call time.
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
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from lumen_errors import _error_status, _is_empty_like
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
    search_marker_requested,
    strip_search_marker,
)
from lumen_security import (
    _LeakScanState,
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
from lumen_status import _spinner_glyph
from lumen_rich import NO_LINK_PREVIEW, _rich_html_with_limit

log = logging.getLogger("bot")


def _append_note_fit(md_text: str, note: str, limit: int) -> str:
    """An HTML chunk already trimmed to Telegram's limit, plus a note: trim the BODY,
    not the concatenation — otherwise the line exceeds 4096, Telegram returns 400, and the
    note itself (\"truncated\" / \"without live search\") is lost (audit 01.11.2026)."""
    note_html = _md_to_html(note)
    return _truncate_html_to_fit(md_text, limit - len(note_html) - 1) + "\n" + note_html


# The first stream edit shows at least this many characters: at Claude, the indicator
# lives until the first letter, then text starts immediately; a zero first frame caught
# frozen “Thinking…” for an entire painter interval (review 11.11.2026).
FIRST_PAINT_MIN_CHARS = 36
# ── partial-safe markup for intermediate edits (review 11.11.2026) ──
# Bare markdown showed users "**" and "```" in the bubble, while the final version changed
# formatting again and caused double reflow. On each frame, close markup that may remain
# unclosed and cut dangling markers or a broken link back to a safe boundary, keeping HTML
# edits stable from frame to frame.
_PARTIAL_LINK_URL_RE = re.compile(r"\[([^\[\]\n]+)\]\([^)\n]*\Z")
_PARTIAL_LINK_LABEL_RE = re.compile(r"\[([^\[\]\n]*)\Z")
_PARTIAL_TRAILING_MARKERS_RE = re.compile(r"(?:[*_~]+|`+)\s*\Z")


def _close_partial_markdown(text: str) -> str:
    if text.count("```") % 2:
        # Unclosed code block: if the language/body has not arrived yet, cut the marker
        # itself,
        # otherwise, close them afterward: hanging ``` would be visible raw.
        head, _sep, tail = text.rpartition("```")
        if "\n" not in tail:
            return head.rstrip("` \n")
        return (text + "```") if text.endswith("\n") else (text + "\n```")
    # A dangling lone backtick: show the content of unclosed inline code
    # without a marker (the marker is OPENING, the last of those remaining).
    if text.count("`") % 2:
        i = text.rfind("`")
        text = text[:i] + text[i + 1:]
    text = _PARTIAL_LINK_URL_RE.sub(r"\1", text)
    text = _PARTIAL_LINK_LABEL_RE.sub(r"\1", text)
    text = _PARTIAL_TRAILING_MARKERS_RE.sub("", text)
    # Close unclosed bold by appending a marker below it: the text beneath it remains bold,
    # as during live typing.
    if len(re.findall(r"\*\*", text)) % 2:
        text += "**"
    return text


def _stream_reveal_html(md_text: str, limit: int) -> str:
    """HTML for the displayed slice: closed constructs are converted, while an incomplete
    trailing markup fragment remains hidden. If extra closing tags make the frame
    exceed the limit, use the overall source boundary (the same approach as
    _truncate_html_to_fit)."""
    if not md_text:
        return ""
    html_text = _md_to_html(_close_partial_markdown(md_text))
    if _tg_len(html_text) > limit:
        return _truncate_html_to_fit(md_text, limit)
    return html_text


def _stream_editor(message: Message, *, reply_markup=None):
    """edit_text for messages already shown by streaming. _tg_call returns bare None
both for a network failure and for \"message is not modified\" — a valid API response
where the content is already on screen. We leave transport alone (someone else's scope) and catch the difference
here, before swallowing the exception: not-modified is a semantic success.
Otherwise a series of \"failures\" treated the message as dead and the entire answer was sent as a duplicate
over what was already shown (review 11.11.2026). None after
_tg_call's internal retries (the real network) remains unverified and, as before, counts as
a stream-series failure."""
    async def edit_text(text, **kwargs):
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        try:
            return await message.edit_text(text, **kwargs)
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return message
            raise
    return edit_text


async def _tick_waiting_dots(placeholder: Message, *, started: float | None = None, reply_markup=None) -> None:
    """Waiting phrase before the first chunk (as in Claude): \"Thinking...\" with an
    animation, not bare dots. The first frame appears after a pause (fast models
    never show the animation), then one frame per tick. Edits go through _tg_call
    with errors swallowed; cancellation is a normal stop."""
    import bot
    # started is the common start of route waiting: after one model fails, the timer
    # continues instead of rolling back to zero (review 28.10.2026).
    started = started if started is not None else time.monotonic()
    try:
        await bot._dots_sleep(bot._DOTS_START_AFTER_SEC)
        chat = getattr(placeholder, "chat", None)
        lang = bot._chat_lang(getattr(chat, "id", None))
        # The first frame (∴) is already in the sent placeholder: start with
        # the second one, otherwise 0.5 s later the indicator "freezes" on the same symbol.
        frame_idx = 1
        markup_kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
        while True:
            elapsed = max(0, int(time.monotonic() - started))
            glyph = _spinner_glyph(frame_idx)
            # The ticker and stream share one per-chat edit budget (review 11.11.2026);
            # before the first letter, the indicator must remain alive (item 2).
            if _pace_take_edit_slot(getattr(chat, "id", None)):
                with contextlib.suppress(Exception):
                    await bot._tg_call(
                        placeholder.edit_text, glyph + " " + _lang_t(lang, "progress_thinking", sec=elapsed),
                        parse_mode=None, call_timeout=15.0, link_preview_options=NO_LINK_PREVIEW,
                        **markup_kwargs,
                    )
            frame_idx += 1
            await bot._dots_sleep(bot._DOTS_TICK_SEC)
    except asyncio.CancelledError:
        raise


async def _pieces_with_waiting_feedback(piece_agen, placeholder: Message, *, first_chunk_limit: float, first_task=None, status=None, wait_started: float | None = None, reply_markup=None):
    """Generator plus first-chunk wait: an adaptive limit (a stuck attempt raises TimeoutError, and the placeholder is handed to the next model), with a live status strictly until the first yield. An empty stream simply ends.

    status is a live lumen_status.StatusMessage: its ticker already shows “Thinking… N s”, so no separate dots are started. Without a status (groups), the same ticker runs with a phrase instead of bare dots.

    first_task is the already-started first-chunk request (see _run_streaming_reply); it starts in parallel with sending the placeholder."""
    dots_task = None if status is not None else asyncio.create_task(
        _tick_waiting_dots(placeholder, started=wait_started, reply_markup=reply_markup)
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
            # The first chunk arrived: record "Thought Ns" and stop the animation,
            # text will now replace the status.
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
    """Seconds remaining until the route budget (float('inf') — no cap)."""
    if deadline is None:
        return float("inf")
    return max(0.0, deadline - time.monotonic())

def _stream_deadline(overall_start_ts: float, now_piece: float) -> float:
    """Live-stream deadline: allows extra time for a long answer, but never beyond
    the message ceiling (audit 16.11.2026: the search phase plus the extension
    exceeded MESSAGE_TOTAL_BUDGET_SEC, which is promised as the ceiling for the
    entire message)."""
    import bot

    deadline = min(overall_start_ts + bot.STREAM_ROUTE_MAX_SEC,
                   now_piece + bot.STREAM_ROUTE_STALL_GRACE_SEC)
    message_left = bot._msg_remaining_sec()
    if message_left is not None:
        deadline = min(deadline, now_piece + message_left)
    return deadline


def _stream_wait(bot: Any, deadline: float | None) -> float:
    """Timeout for ONE streaming operation: no longer than the inter-chunk gap and no longer than
the remaining route budget. Previously the wait bypassed the budget, so a chunk could wait
STREAM_CHUNK_TIMEOUT_SEC after it had already been exhausted (audit 26.09.2026). We do not lower
the value below 0.1s: that would still be an immediate TimeoutError."""
    return max(0.1, min(bot.STREAM_CHUNK_TIMEOUT_SEC, _remaining_budget(deadline)))

async def _gemini_stream_pieces(model_id: str, call_contents: list, gconfig, *, deadline: float | None = None):
    """Chunks from Gemini (a thin wrapper over generate_content_stream).

    In google-genai 2.27.0 this is an async def: the call must be awaited (SDK example:
    `async for chunk in await client.aio.models.generate_content_stream(...)`).
    The actual network wait is handled below: the first chunk goes through __anext__
    under the same _stream_wait."""
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
    """Shared parsing of OpenAI-compatible SSE (Munito returns
    `data: {\"choices\":[{\"delta\":{\"content\":...}}]}` and a final `data: [DONE]`).

    Previously there were several 45-line copies that differed in details: one
    lacked checks for an HTTP error and an error inside a chunk (26.09.2026 audit).
    An error inside a chunk (HTTP 200, stream already open) is raised: an early
    failure falls back to the next model; a late one marks \"connection interrupted\"."""
    import bot
    post_cm = session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=None, connect=12.0))
    try:
        # Headers without a total hung forever and held the chat lock until restart (audit 29.09.2026).
        # Bound admission by the route attempt while still consuming the stream body chunk by chunk.
        resp = await asyncio.wait_for(post_cm.__aenter__(), timeout=bot._attempt_timeout(bot, deadline))
    except BaseException:
        with contextlib.suppress(Exception):
            await post_cm.__aexit__(None, None, None)
        raise
    try:
        if resp.status >= 400:
            # Read the error body with a cap: the 200-OK path limits each read, while
            # here the provider could return multi-megabyte HTML (run-2 audit).
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

def _history_safe_answer(text: str) -> str:
    """Use the same scrubber as on the successful path: disconnects and /stop previously
    put raw text with provider labels into history (review 17.11.2026)."""
    if not text:
        return text
    try:
        return _polish_answer(_strip_provider_artifacts(text), source="stream_history")
    except Exception:
        return _strip_provider_artifacts(text)


async def _remember_turn(
    state: dict, history_text, user_text: str, answer: str, history_meta: dict | None,
) -> None:
    """Writes the question-answer pair to the shared log and starts a summary when it overflows."""
    import bot
    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    # Stop before the first chunk: the question stays in history, but the empty
    # response does not
    # (otherwise the model sees the reply "the bot stayed silent" on the next turn).
    if (answer or "").strip():
        lumen_memory.append_assistant(state, answer)
    await bot._trim_history(state["history"], state)


async def _munito_stream_pieces(model_id: str, messages: list[dict], *, deadline: float | None = None, effort: str | None = None):
    """Chunks from Munito AI: the same OpenAI-SSE through the shared _sse_pieces (measured 05.11.2026: first token 0.24s at the chain head)."""
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
        {"model": model_id, "messages": messages, "stream": True, **_munito_payload_extras(effort, model_id)},
        err_cls=bot.MunitoAPIError, provider_label="Munito", deadline=deadline,
    ):
        yield piece

class _StoppedByUser(Exception):
    """Explicit stop via /stop: not a model failure, and not an error for history or quota."""


def _stop_button_markup(chat_id: int):
    """Inline variant for groups: the personal button above has no bubble binding."""
    import bot
    if not getattr(bot, "STOP_BUTTON_ENABLED", False):
        return None
    token = bot._stream_stop_token(chat_id)
    callback = f"stop:{chat_id}:{token}" if token else f"stop:{chat_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=bot._t(chat_id, "stop_button"), callback_data=callback),
    ]])


def _stop_reply_keyboard(chat_id: int):
    """In DMs, the button lives above the composer and does not leave with the answer."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/stop")]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def _run_streaming_reply(
    chat_id: int, user_text: str, message: Message, *, provider: str, model_id: str, piece_agen,
    deadline: float | None = None, status=None, history_text=None,
    reuse_placeholder: Message | None = None, wait_started: float | None = None,
    history_meta: dict | None = None,
) -> tuple[str | None, Message | None]:
    """Streaming: a chunk generator plus placeholder, chunking, throttling, interruption on
    a leak/injection, HTML finalization, and history recording. Returns (answer, placeholder):
    success -> (text, None); failure before display -> (None, placeholder for the next model);
    failure after display -> (displayed text plus marker, None)."""
    import bot
    state = bot.get_state(chat_id)
    # The stop flag lives on the chat: /stop sets it, and the loop reads it on every chunk.
    bot._begin_stream_stop_scope(
        chat_id, getattr(getattr(message, "from_user", None), "id", None), stream=True)
    # Initialize BEFORE try: an early return (the placeholder was not sent) must see
    # these names in finally.
    keep_placeholder_live = False
    _stop_control = None
    _stop_markup = None

    sent_messages: list[Message] = []
    full_text = ""
    leak_scan = _LeakScanState()
    # Finalized chunk boundaries are frozen once (review 11.11.2026):
    # _split_text_chunks is recalculated only for the tail full_text[covered_len:].
    # The previous full recalculation shifted the boundaries of chunks already shown; text flowed
    # between bubbles, while editing fixed it (_resync_finalized, 01.11.2026 audit) —
    # resync was removed along with the recalculation. covered_len is computed from chunk lengths,
    # spaces at chunk boundaries are trimmed from both sides by the chunker — coverage is not lost.
    frozen: list[str] = []
    covered_len = 0
    tail_chunks: list[str] = []
    tail_basis_len = 0
    edit_fail_streak = 0
    edits_dead = False
    delivery_unknown = False
    # last_painted_plain is the markdown slice actually visible in the growing
    # message; last_painted_html is the sent HTML for the same frame: equality
    # Render only the largest step (coalescing), avoiding "message is not modified".
    last_painted_plain = ""
    last_painted_html = ""
    # Production smoothness counters: how many frames actually arrived and what the
    # the largest step. They show whether the output resembles Claude or comes in jerks
    # (Telegram collapses getUpdates frames into one, so this cannot be measured
    # locally).
    paint_frames = 0
    paint_max_step = 0
    paint_html_broken = False
    first_piece_ts: float | None = None
    # The first frame where text appears on screen: the human wait ends here, not at the first chunk.
    first_paint_ts: float | None = None
    # header_md — a short activity line "✻ Thought for N s" above the first bubble with
    # the first edit, while at Claude it does not disappear (item 8, review 11.11.2026);
    # empty when sec < 1 (item 11). Calculated once on the first chunk.
    header_md = ""
    # Speed/latency measurements — see lumen_typing_pace.py (overall is measured once for the whole answer, arrival anew for each new message).
    pace_key = _typing_speed_key(provider, model_id)
    overall_start_ts = time.monotonic()
    # reveal_base_ts is the time of the FIRST chunk, not the stream start: otherwise,
    #     after a long silence, the formula would immediately allow all buffered text to
    #     appear at once. last_piece_ts/arrival_ewma measure the actual arrival rate.
    reveal_base_ts: float | None = None
    last_piece_ts: float | None = None
    arrival_ewma: float | None = None
    is_group = getattr(getattr(message, "chat", None), "type", None) != ChatType.PRIVATE
    # Painter frame: in a group, the edit budget is shared among all participants (item 12).
    paint_interval = bot.STREAM_PAINT_TICK_GROUP_SEC if is_group else bot.STREAM_PAINT_TICK_SEC
    gate_interval = bot.STREAM_EDIT_MIN_INTERVAL_GROUP_SEC if is_group else bot.STREAM_EDIT_MIN_INTERVAL_SEC

    def _active_markup(msg: Message):
        # Stop lives in a separate small bubble so a long answer does not carry
        # the keyboard below the bottom edge of the screen (prod 24.11.2026).
        return None

    def _markup_kwargs(msg: Message) -> dict:
        markup = _active_markup(msg)
        return {"reply_markup": markup} if markup is not None else {}

    def _header_html() -> str:
        return _md_to_html(header_md) if header_md else ""

    def _final_chunk_html(chunk_text: str, idx: int) -> str:
        # The header lives on the first bubble and must survive its finalization,
        # otherwise it will disappear along with the last edit of chunk 0 (item 8).
        head = _header_html()
        return (head + "\n\n" + _md_to_html(chunk_text)) if (idx == 0 and head) else _md_to_html(chunk_text)

    def _reveal_len(target: str, min_reveal: int) -> int:
        now = time.monotonic()
        typing_speed = _typing_display_speed(arrival_ewma, pace_key)
        reveal = min(len(target), max(0, int((now - (reveal_base_ts or now)) * typing_speed)))
        # Displaying does not move backward if the tail boundary shifts during recalculation.
        shown = len(last_painted_plain) if target.startswith(last_painted_plain) and last_painted_plain else 0
        return max(reveal, min_reveal, shown)

    async def _paint(*, force: bool = False, min_reveal: int = 0) -> None:
        """Painter’s frame: draw on a timer from the shared buffer, not when a chunk arrives
        (item 1). Edits are sequential in this loop: while one is in flight, no new one
        starts, and the next frame takes the latest state — coalescing, which also
        prevents flickering and unnecessary 429s."""
        nonlocal edit_fail_streak, edits_dead, last_painted_plain, last_painted_html, paint_html_broken, paint_frames, paint_max_step, first_paint_ts
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
        edit_ok = await bot._tg_call(
            _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
            payload, parse_mode=parse_mode, call_timeout=15.0, link_preview_options=NO_LINK_PREVIEW,
        )
        if edit_ok is not None:
            edit_fail_streak = 0
            paint_frames += 1
            paint_max_step = max(paint_max_step, len(slice_md) - len(last_painted_plain))
            last_painted_plain = slice_md
            last_painted_html = html_out
            if first_paint_ts is None and slice_md.strip():
                first_paint_ts = time.monotonic()
            return
        if parse_mode is ParseMode.HTML:
            # Telegram's HTML parser rejected partial markup — this is not dead
            # the message: try the next frame as plain text (point 7).
            paint_html_broken = True
            return
        edit_fail_streak += 1
        edits_dead = edit_fail_streak >= 3

    # Request the first chunk BEFORE the placeholder: sending "…" is a separate round
    # to Telegram through the proxy, and the model request would sit idle for that whole round trip.
    # Keep the painter frame in transit here, not inside try: finally must always
    # see it.
    paint_task: asyncio.Future | None = None
    first_task = asyncio.ensure_future(piece_agen.__anext__())
    read_task: asyncio.Future | None = None
    try:
        if reuse_placeholder is not None:
            # Fallback model after an early stream failure: the same bubble,
            # a second “Thinking…” in the chat is unnecessary (audit 28.10.2026).
            placeholder = reuse_placeholder
        elif status is not None:
            # The status bubble is already in the chat: reuse it, do not send a second one.
            placeholder = status.message
        else:
            placeholder = await bot._tg_call(
                bot._answer_sender(message), _spinner_glyph(0) + " " + bot._t(message.chat.id, "progress_thinking", sec=0),
                parse_mode=None, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
            )
        if bot._tg_outcome_unknown(placeholder):
            # Review I21 (26.11.2026): the bubble continues to live as a Message; it is edited
            # and delete it. With an unknown outcome this is AttributeError on the sentinel, so
            # here it collapses to None (as before R4-03): a repeated bubble
            # cheaper than a stream failure; duplicate bubbles are harmless to the text.
            placeholder = None
        if placeholder is None:
            return None, None
        sent_messages.append(placeholder)
        if reuse_placeholder is not None and status is not None:
            resume = getattr(status, "resume", None)
            if resume is not None:
                resume()
        # A model attempt occurred (the request already went to the chunk generator);
        # an empty result is counted only for an early cutoff before display — a partial response was shown.
        bot._record_stats_event("model_attempts")

        # Keep Stop as a separate small bubble above the input field. An inline button
        # under the growing answer moves off-screen, even when reply_markup is preserved.
        _stop_markup = _stop_button_markup(chat_id)

        async def _send_stop_control():
            if _stop_markup is None or message is None:
                return None
            private = getattr(message.chat, "type", None) == ChatType.PRIVATE
            markup = _stop_reply_keyboard(message.chat.id) if private else _stop_markup
            return await bot._tg_call(
                bot._answer_sender(message), bot._t(message.chat.id, "stop_button"), parse_mode=None,
                reply_markup=markup, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                retries=0, link_preview_options=NO_LINK_PREVIEW,
            )

        async def _move_stop_control() -> None:
            nonlocal _stop_control
            if getattr(message.chat, "type", None) == ChatType.PRIVATE:
                return
            send_task = asyncio.create_task(_send_stop_control())
            try:
                new_control = await asyncio.shield(send_task)
            except asyncio.CancelledError:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    new_control = await send_task
                if new_control is not None and not bot._tg_outcome_unknown(new_control):
                    old_control, _stop_control = _stop_control, new_control
                    if old_control is not None:
                        with contextlib.suppress(Exception):
                            await bot._tg_call(old_control.delete, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)
                raise
            if new_control is None or bot._tg_outcome_unknown(new_control):
                return
            old_control, _stop_control = _stop_control, new_control
            if old_control is not None:
                with contextlib.suppress(Exception):
                    await bot._tg_call(old_control.delete, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)

        send_task = asyncio.create_task(_send_stop_control())
        try:
            _stop_control = await asyncio.shield(send_task)
            if bot._tg_outcome_unknown(_stop_control):
                _stop_control = None
        except asyncio.CancelledError:
            # Cancellation must not lose the message_id already accepted by Telegram
            # of the control bubble: wait for the result, then finally deletes it.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                _stop_control = await send_task
                if bot._tg_outcome_unknown(_stop_control):
                    _stop_control = None
            raise

        first_chunk_limit = _model_first_chunk_limit(_model_speed_key(provider, model_id), bot.FIRST_CHUNK_TIMEOUT_SEC)
        if deadline is not None:
            # The first chunk must not wait longer than the overall message budget: before it
            # the deadline was not checked and the wait could exceed the budget (audit 27.10.2026).
            first_chunk_limit = min(first_chunk_limit, max(0.5, deadline - time.monotonic()))
        piece_agen = _pieces_with_waiting_feedback(
            piece_agen, placeholder,
            first_chunk_limit=first_chunk_limit,
            first_task=first_task,
            status=status,
            wait_started=wait_started,
        )
        # The loop advances read_task with a timeout to the next frame: a chunk arrived —
        # we process it; if the timer has expired, render from the buffer. One sequential
        # task instead of a parallel painter: edits do not race with finalization,
        # between awaits, only text already scanned by the leak check is visible
        # (it runs to the first await after full_text +=).
        iterator = piece_agen.__aiter__()
        next_paint_ts = 0.0
        # The painter frame runs as a separate task: an edit is a round trip to Telegram through
        # the proxy (0.5-0.7 seconds), and while it was in flight, the loop would read chunks without
        # while showing it. Production on 11.11.2026 measured exactly 1 frame per second instead of a tick every
        # 0.55s; keep at most one edit in flight, and the next takes the earliest
        # fresh state (coalescing); we must always wait for it before finalization.
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
            if bot._stream_stop_requested(chat_id):
                raise _StoppedByUser()
            if read_task is None:
                read_task = asyncio.ensure_future(iterator.__anext__())
            if first_piece_ts is None:
                # Wake once every half second: otherwise /stop before the first chunk
                # waits for the first-chunk timeout and is treated as a model failure
                # (review 17.11.2026).
                wait_left = 0.5
                if deadline is not None:
                    wait_left = min(wait_left, max(0.0, deadline - time.monotonic()))
            else:
                wait_left = max(0.0, next_paint_ts - time.monotonic())
            done, _pending = await asyncio.wait({read_task}, timeout=wait_left)
            if read_task not in done:
                if bot._stream_stop_requested(chat_id):
                    raise _StoppedByUser()
                if first_piece_ts is None:
                    # Wait for the first letter: the route deadline does not cut it off (that is
                    # done by
                    # a first-chunk timeout), and the stop was already checked above.
                    continue
                now_tick = time.monotonic()
                if deadline is not None and now_tick > deadline:
                    raise bot.RouteBudgetExceededError([f"{provider}:{model_id}"])
                if first_piece_ts is not None:
                    # Before the first letter, the frame renders a ticker; a second painter is unnecessary.
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
            # Second stop check: the generator could set the flag between yields,
            # and the chunk arriving immediately after must be discarded, not shown.
            if bot._stream_stop_requested(chat_id):
                raise _StoppedByUser()
            now_piece = time.monotonic()
            # The overall route budget also applies to streaming, but a live stream is not cut by
            # 40-second cap: prod 11.11.2026 cut off a 5,700-character answer
            # at the 35th second, and the person saw “Connection interrupted”. While chunks
            # arrive, the budget is extended by a grace window with a cap below 60s,
            # which the relay cuts off; the chat lock is still held for MESSAGE_TOTAL_BUDGET_SEC.
            if deadline is not None:
                deadline = _stream_deadline(overall_start_ts, now_piece)
                if now_piece > deadline:
                    raise bot.RouteBudgetExceededError([f"{provider}:{model_id}"])
            first_piece = first_piece_ts is None
            if first_piece:
                first_piece_ts = now_piece
                reveal_base_ts = now_piece
                _pace_arm_edit_gate(chat_id, gate_interval)
                # The Activity line is set once on the first chunk: sec < 1 —
                # empty, as in Claude (items 8 and 11, review 11.11.2026).
                if status is not None:
                    header_md = status.paint_header_md()
            else:
                arrival_ewma = _typing_arrival_update(arrival_ewma, len(piece), now_piece - (last_piece_ts or now_piece))
            last_piece_ts = now_piece
            full_text += piece

            marker_state = search_marker_state(full_text)
            if marker_state == "pending":
                continue
            if marker_state == "requested":
                # The model requests search: exit BEFORE the first edit, so no frame
                # with the marker, the bubble stays alive for the snippet pass.
                log.info('[stream] %s/%s asked for a web search instead of answering', provider, model_id)
                raise _SearchRequested(sent_messages[-1] if sent_messages else None)
            if search_marker_requested(full_text):
                # Marker plus text: do not show the service line; keep the answer.
                full_text = strip_search_marker(full_text)

            leak_kind = leak_scan.feed(piece)

            if leak_kind:
                # Check BEFORE edit_text so the leak does not have time to "flash" on screen.
                tag = "identity-leak" if leak_kind == "identity" else "injection-echo"
                log.warning(
                    '[%s] Stream %s/%s started leaking internal details/echoing an injected instruction — aborting the stream and showing a neutral reply instead (accumulated_len=%d)', tag, provider, model_id, len(full_text),
                )
                aclose = getattr(piece_agen, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                final_answer = _IDENTITY_LEAK_FALLBACK if leak_kind == "identity" else _INJECTED_PAYLOAD_ECHO_FALLBACK
                await bot._tg_call(
                    _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
                    final_answer, parse_mode=None, call_timeout=15.0, link_preview_options=NO_LINK_PREVIEW,
                )
                await _remember_turn(state, history_text, user_text, final_answer, history_meta)
                bot._record_quota_usage(provider, model_id)
                return final_answer, None

            visible_text = full_text[:leak_scan.safe_length(len(full_text))]
            if not tail_chunks or len(visible_text) - covered_len - tail_basis_len >= 1000:
                tail_chunks = _split_text_chunks(visible_text[covered_len:], bot.TG_MAX_LEN)
                tail_basis_len = len(visible_text) - covered_len
            # The painter edits the same bubble on the way that finalization is about to
            # it: a late frame overwrote the final slice. The order of edits
            # Telegram server does not guarantee it (review 17.11.2026).
            await _await_paint()
            # The collapsed “Thought for N s” block counts before the first text appears on screen:
            # Otherwise the status froze at “2 s” while the answer kept crawling for another twenty seconds (prod 17.11.2026).
            if status is not None and first_paint_ts is not None:
                status.note_extra_thought(first_paint_ts - overall_start_ts)
                # The line above the body is rebuilt too: otherwise, until the end of the answer
                # 'Thought for 2s' remained visible while the text was still being completed.
                header_md = status.paint_header_md()
            # Finalize all chunks except the last (growing) one using the same splitting as non-streamed replies.
            while len(frozen) + len(tail_chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                chunk_text = tail_chunks[0]
                if not chunk_text.strip():
                    # The boundary consumed the entire remainder (a tail of spaces only):
                    # continuation is unnecessary; the finalizer will append the tail.
                    break
                frozen.append(chunk_text)
                covered_len += len(chunk_text)
                tail_chunks = _split_text_chunks(visible_text[covered_len:], bot.TG_MAX_LEN)
                tail_basis_len = len(visible_text) - covered_len
                # Continuation is opened by the FIRST real slice, not by "...":
                # a placeholder line followed immediately by an edit causes visible flicker
                # (item 6).
                open_md = tail_chunks[-1][:FIRST_PAINT_MIN_CHARS] if tail_chunks else ""
                open_html = _stream_reveal_html(open_md, bot.TG_MAX_LEN) if open_md else "…"
                # bot.bot — an aiogram Bot instance (the name bot is taken by the module).
                new_msg = await bot._tg_call(
                    bot.bot.send_message, chat_id=message.chat.id, text=open_html,
                    parse_mode=ParseMode.HTML, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                if bot._tg_outcome_unknown(new_msg):
                    # Review I21 round 2 (26.11.2026): the continuation bubble is edited
                    # as Message; unknown outcome = "not created", then follow the None path.
                    new_msg = None
                if new_msg is None:
                    # idx is NOT finalized yet — do not let the common except overwrite the displayed text with someone else's (the invariant "sent[-1] == chunks[-1]" no longer holds there).
                    note = bot._t(message.chat.id, "stream_note_send_fail")
                    with contextlib.suppress(Exception):
                        # The mark is more important than the chunk's tail (see _append_note_fit).
                        await bot._tg_call(
                            _stream_editor(sent_messages[idx], reply_markup=_active_markup(sent_messages[idx])),
                            _append_note_fit(chunk_text, note, bot.TG_MAX_LEN),
                            parse_mode=ParseMode.HTML, call_timeout=15.0, link_preview_options=NO_LINK_PREVIEW,
                        )
                    final_answer = _history_safe_answer(full_text.strip())
                    await _remember_turn(state, history_text, user_text, final_answer, history_meta)
                    bot._record_quota_usage(provider, model_id)
                    return final_answer, None
                if not edits_dead:
                    # Finalization requires an edit: the boundary was frozen, but the text
                    # must catch up with it immediately (force takes the slot).
                    edit_ok = await bot._tg_call(
                        _stream_editor(sent_messages[idx], reply_markup=_active_markup(sent_messages[idx])),
                        _final_chunk_html(chunk_text, idx),
                        parse_mode=ParseMode.HTML, call_timeout=15.0,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    # A series of failed edits in a row — the message was deleted mid-stream:
                    # stop editing after this point; send the remaining answer as a new message at the end.
                    edit_fail_streak = 0 if edit_ok is not None else edit_fail_streak + 1
                    edits_dead = edit_fail_streak >= 3
                sent_messages.append(new_msg)
                await _move_stop_control()
                # The new message is already open in the first slice: no second edit is needed.
                last_painted_plain = open_md
                last_painted_html = open_html
                # A new message is a new "page"; display in it starts at this slice.
                reveal_base_ts = time.monotonic()
                next_paint_ts = reveal_base_ts + paint_interval

            # First frame immediately, from FIRST_PAINT_MIN_CHARS characters: a frozen
            # "Thinking..." for a full interval is no longer caught (item 2). The rest —
            # on the timer; if a chunk arrives before the tick, no extra edit is made.
            if first_piece:
                await _paint(force=True, min_reveal=FIRST_PAINT_MIN_CHARS)
                next_paint_ts = time.monotonic() + paint_interval
            elif time.monotonic() >= next_paint_ts:
                _schedule_paint()
                next_paint_ts = time.monotonic() + paint_interval

        # An in-flight edit must not outlive the final state: two edits to one bubble
        # simultaneously — a content race, not coalescing.
        await _await_paint()
        # Classifier labels and tool markup are not a response: the chat must
        # see the next model, not 39 characters of "User Safety: safe".
        full_text = _strip_provider_artifacts(full_text)
        if _is_empty_like(full_text):
            # An empty stream is a failure, but the placeholder is returned to the caller (see the docstring).
            keep_placeholder_live = True
            return None, sent_messages[-1]

        # Measurements exclude catch-up (the pause must not skew the estimate); catch-up pauses in the model's total are deliberate (visible latency matters).
        _record_typing_speed(pace_key, time.monotonic() - overall_start_ts, len(full_text))
        _record_model_latency(
            _model_speed_key(provider, model_id),
            total_sec=time.monotonic() - overall_start_ts,
            ttf_sec=(first_piece_ts - overall_start_ts) if first_piece_ts is not None else None,
        )

        # Final scrub BEFORE the last edit: otherwise the user saw the answer,
        # which had already entered history cleaned (audit 28.10.2026).
        raw_full = full_text.strip()
        _polish_src = f"{provider}_stream_final:{model_id}"
        full_text = _polish_answer(raw_full, source=_polish_src)
        # "Tail display" of the remainder: backends showing it all at once at the end would display "…" until the very end. Capped by the catch-up limit.
        # Split only the tail from the frozen boundary: recalculating the entire text
        # would shift already displayed boundaries (point 5; _resync_finalized was removed).
        # The boundary is calculated in offsets BEFORE polishing: editing shortens the text,
        # and slicing by covered_len lost exactly as many characters as were trimmed
        # (review 11.11.2026: 11 characters disappeared from the second bubble).
        shown_len = covered_len
        if len(full_text) != len(raw_full):
            shown_len = _polished_prefix_len(raw_full, min(covered_len, len(raw_full)), source=_polish_src)
        final_tail = _split_text_chunks(full_text[shown_len:], bot.TG_MAX_LEN)
        final_chunks = frozen + final_tail
        # The painter and the remainder must see the FINAL tail (after scrubbing), not
        # pre-final: otherwise catch-up frames render a stale buffer.
        tail_chunks = final_tail
        tail_already_final = False
        if not edits_dead and len(final_chunks) > len(sent_messages):
            # Incremental splitting lags by 1000 characters: by the final
            # a new chunk could arrive, and the last edit erased the answer
            # the tail. Send the missing messages (audit 28.10.2026).
            while len(final_chunks) > len(sent_messages):
                idx = len(sent_messages) - 1
                edit_ok = await bot._tg_call(
                    _stream_editor(sent_messages[idx], reply_markup=_active_markup(sent_messages[idx])),
                    _final_chunk_html(final_chunks[idx], idx),
                    parse_mode=ParseMode.HTML, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                if bot.bot is None:
                    edits_dead = True
                    break
                # None from _tg_call meant both flood-429 and "message is not modified"—
                # a semantic no-op is now intercepted in _stream_editor as
                # a success, it does not join the failure streak (point 7, review 11.11.2026).
                edit_fail_streak = 0 if edit_ok is not None else edit_fail_streak + 1
                edits_dead = edit_fail_streak >= 3
                if edits_dead:
                    break
                next_chunk = final_chunks[idx + 1]
                new_msg = await bot._tg_call(
                    bot.bot.send_message, chat_id=message.chat.id,
                    text=_md_to_html(next_chunk),
                    parse_mode=ParseMode.HTML, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                if bot._tg_outcome_unknown(new_msg):
                    # The continuation may already be delivered. Stop without the definite-failure
                    # fallback, which would send the complete answer a second time (review 27.11.2026).
                    delivery_unknown = True
                    edits_dead = True
                    break
                if new_msg is None:
                    edits_dead = True
                    break
                sent_messages.append(new_msg)
                await _move_stop_control()
                # The new message already contains the full chunk: no catch-up is needed for it.
                last_painted_plain = next_chunk
                last_painted_html = _md_to_html(next_chunk)
                reveal_base_ts = time.monotonic()
            tail_already_final = not edits_dead and last_painted_plain == final_chunks[-1]
        target_full = final_chunks[-1]
        already_shown_len = len(last_painted_plain) if last_painted_plain and target_full.startswith(last_painted_plain) else 0
        remaining_len = len(target_full) - already_shown_len
        if remaining_len > 0 and not edits_dead:
            typing_speed = _typing_display_speed(arrival_ewma, pace_key)
            # Calculate the output-tail budget in ticks from seconds, not from a gesticulation
            # groups of eight: otherwise a long tail is displayed in several jumps.
            # At the same time, do not exceed the remaining message budget.
            catchup_sec = (bot.STREAM_TYPING_MAX_CATCHUP_GROUP_SEC if is_group
                             else bot.STREAM_TYPING_MAX_CATCHUP_SEC)
            if deadline is not None:
                catchup_sec = min(catchup_sec, max(0.0, deadline - time.monotonic()))
            # A tick is a pause + edit: an edit through the relay costs 0.5–0.7 s, and without
            # of this step, the budget counted only sleep (review 11.11.2026).
            step_sec = bot.STREAM_TYPING_TICK_SEC + bot.STREAM_TYPING_STEP_RTT_SEC
            max_ticks = _catchup_tick_budget(catchup_sec, step_sec)
            # No time remains at all — there will be no further output: the final edit
            # shows the entire answer immediately, without a pause beyond the budget.
            for step_len in _typing_catchup_steps(remaining_len, typing_speed, bot.STREAM_TYPING_TICK_SEC, max_ticks):
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if bot._stream_stop_requested(chat_id):
                    # /stop during completion: stop appending and the flag
                    # consume it; otherwise it leaks into the next update
                    # (review 17.11.2026).
                    bot._consume_stream_stop(chat_id)
                    raise _StoppedByUser()
                await bot._typing_sleep(bot.STREAM_TYPING_TICK_SEC)
                # The remainder is rendered by the same painter: the same coalescing, partial-safe
                # markup and the overall per-chat edit budget (items 1 and 12).
                await _paint(min_reveal=already_shown_len + step_len)

        # Final — with full HTML conversion (partial frames before this had
        # closed tail markup; here it is real).
        final_text = final_chunks[-1]
        if edits_dead and not delivery_unknown:
            # Edits died to a series of network failures: the message was removed in the middle of
            # stream: send the entire remainder as a new message (_send_text inside
            # splits it into parts itself), otherwise the user will not see it.
            await bot._safe_reply(message, full_text.strip())
        elif len(final_chunks) == 1 and status is not None:
            # The activity block remains only where there were real stages
            # (search/media/link). Claude currently shows a simple answer without it.
            compose_with_reason = getattr(status, "compose_answer_with_reason", None)
            if compose_with_reason is not None:
                html_text, plain_text, compose_reason = compose_with_reason(final_text)
            else:
                html_text, plain_text = status.compose_answer(final_text)
                compose_reason = "composed" if html_text is not None else "table"
            if html_text is None:
                # Table/rich goes without a quote, but the saved stream header remains,
                # if both final forms fit. Overflow/no_prefix gets no header.
                body = final_text
                if compose_reason == "table" and header_md:
                    candidate = header_md + "\n\n" + final_text
                    if (_tg_len(candidate) <= bot.TG_MAX_LEN
                            and _tg_len(_md_to_html(candidate)) <= bot.TG_MAX_LEN
                            and (not bot.RICH_MESSAGES_ENABLED
                                 or _tg_len(_rich_html_with_limit(candidate)) <= bot.TG_MAX_LEN)):
                        body = candidate
                await bot._edit_message_quietly(
                    sent_messages[-1], body, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW, **_markup_kwargs(sent_messages[-1]),
                )
            else:
                edited = await bot._tg_call(
                    _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])), html_text,
                    parse_mode=ParseMode.HTML, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                if edited is None and not edits_dead:
                    # One HTML retry after a pause: a one-off 429 must not cost
                    # markup to the entire response (prod 17.11.2026: ** remained raw).
                    await asyncio.sleep(0.7)
                    edited = await bot._tg_call(
                        _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])), html_text,
                        parse_mode=ParseMode.HTML, call_timeout=15.0,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                if edited is None:
                    # The last line of defense is plain text without markdown syntax.
                    from lumen_rich import _strip_markdown
                    await bot._tg_call(
                        _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
                        _strip_markdown(plain_text), parse_mode=None, call_timeout=15.0,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
        else:
            if not tail_already_final and not delivery_unknown:
                # HTML->plain fallback — inside _edit_message_quietly (previously duplicated manually here).
                # The sent tail already contains the final HTML: another edit
                # would receive "not modified" and strip links/markup (audit 28.10.2026).
                # An unknown continuation is not in sent_messages; editing its predecessor
                # with final_text would overwrite the first chunk with the tail.
                body = final_text
                if header_md and len(final_chunks) == 1:
                    body = header_md + "\n\n" + final_text
                await bot._edit_message_quietly(
                    sent_messages[-1], body, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW, **_markup_kwargs(sent_messages[-1]),
                )

    except _StoppedByUser:
        # Intentional stop: the partial text stays on screen and is saved to
        # history below. Do NOT set stream_interrupted — automatic continuation after
        # an intentional stop does not trigger; “continue” works through interrupted.
        state["interrupted"] = True
        state["stopped_by_user"] = True
        bot._consume_stream_stop(chat_id)
        bot.mark_state_dirty(chat_id)
        if sent_messages:
            with contextlib.suppress(Exception):
                # Stop can arrive before a sentence boundary, so the incremental
                # gate has not scanned the tail yet. Never paint it unsanitized.
                stop_source = f"{provider}_stream_stop:{model_id}"
                raw_stopped = full_text.strip()
                full_text = _polish_answer(raw_stopped, source=stop_source)
                stop_covered_len = covered_len
                if len(full_text) != len(raw_stopped):
                    stop_covered_len = _polished_prefix_len(raw_stopped, min(covered_len, len(raw_stopped)), source=stop_source)
                tail_text = full_text[stop_covered_len:] if frozen or covered_len else full_text
                note = bot._t(message.chat.id, "stream_note_stopped")
                await bot._tg_call(
                    _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
                    _append_note_fit(tail_text, note, bot.TG_MAX_LEN),
                    parse_mode=ParseMode.HTML, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW,
                )
    except _SearchRequested:
        # The next search pass reuses the bubble and status.
        keep_placeholder_live = True
        # Not a model failure: cooldown, quarantine, and model_failures have nothing to do with it.
        # But the provider call already cost the provider money: record the spend; the limit
        # for the person — no (the real answer will charge it; audit 16.11.2026).
        bot._record_quota_usage(provider, model_id)
        raise
    except Exception as exc:
        if _is_empty_like(full_text):
            # 429 before the first chunk — into /stats and briefly out of routing. The daily limit
            # until morning, a one-minute spike — a short cooldown (otherwise one 429
            # locked the model until midnight — hostile review 27.09.2026).
            try:
                _txt = bot._error_text(exc).strip() or exc.__class__.__name__
                if bot._classify_model_error(bot._error_status(exc, _txt), _txt) == "rate_limit":
                    # Only Gemini recognizes a daily quota: Munito has windowed limits
                    # (200 requests in 30 seconds); there is no reason to keep it at 429 until morning.
                    _daily = provider == "gemini" and bot._is_gemini_daily_quota(_txt)
                    if _daily:
                        bot._mark_quota_exhausted(provider, model_id)
                    else:
                        bot._mark_rate_limited(provider, model_id)
                else:
                    # 503/404 and other "unavailable" errors get a cooldown on the same terms
                    # (production 05.10.2026: without it, five 3.x models repeated for
                    # on every message, wasting 22 seconds per attempt).
                    from lumen_routes import _mark_model_unavailable
                    _mark_model_unavailable(provider, model_id, exc)
            except Exception:
                pass
            # Do NOT remove the placeholder — return it for reuse (see the docstring).
            log.warning('[stream] Stream %s/%s failed before showing any content, falling back to a regular call (error=%s, status=%s)', provider, model_id, type(exc).__name__, _error_status(exc, ""))
            bot._record_stats_event("model_failures")
            keep_placeholder_live = True
            return None, (sent_messages[-1] if sent_messages else None)
        log.warning('[stream] Stream %s/%s failed after partially showing the response, finishing as-is (error=%s, status=%s)', provider, model_id, type(exc).__name__, _error_status(exc, ""))
        if _detect_identity_leak(full_text):
            # The last safeguard (the main check runs on every chunk above).
            log.warning('[identity-leak] Leak caught by the fallback guard (provider=%s, model=%s, accumulated_len=%d)', provider, model_id, len(full_text))
            full_text = _IDENTITY_LEAK_FALLBACK
            with contextlib.suppress(Exception):
                await bot._tg_call(
                    _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
                    _IDENTITY_LEAK_FALLBACK, parse_mode=None, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW,
                )
        elif _detect_injected_payload_echo(full_text):
            log.warning('[injection-echo] Injected-instruction echo caught by the fallback guard (provider=%s, model=%s, accumulated_len=%d)', provider, model_id, len(full_text))
            full_text = _INJECTED_PAYLOAD_ECHO_FALLBACK
            with contextlib.suppress(Exception):
                await bot._tg_call(
                    _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
                    _INJECTED_PAYLOAD_ECHO_FALLBACK, parse_mode=None, call_timeout=15.0,
                    link_preview_options=NO_LINK_PREVIEW,
                )
        else:
            with contextlib.suppress(Exception):
                # Set the marker on the TAIL from the frozen boundary: a full recalculation
                # shift boundaries and move displayed text between bubbles
                # (item 5; previously the tail of the recomputed split overwrote what was
                # displayed).
                tail_text = full_text[covered_len:] if frozen or covered_len else full_text
                note = bot._t(message.chat.id, "stream_note_interrupted")
                await bot._tg_call(
                    _stream_editor(sent_messages[-1], reply_markup=_active_markup(sent_messages[-1])),
                    _append_note_fit(tail_text, note, bot.TG_MAX_LEN),
                    parse_mode=ParseMode.HTML, call_timeout=15.0, link_preview_options=NO_LINK_PREVIEW,
                )
            # The partial answer is already displayed and will be saved to history below; remember that
            # it was cut off: the word “continue” will resume from the interruption point (_continue_after_interrupt).
            # stream_interrupted survives a successful return: otherwise _handle_message_core
            # cleared the shared flag and 'continue' stopped working (audit 27.10.2026).
            state["interrupted"] = True
            state["stream_interrupted"] = True
            bot.mark_state_dirty(chat_id)
    finally:
        if not keep_placeholder_live:
            bot._consume_stream_stop(chat_id)
        bot._end_stream_stop_scope(chat_id)
        # First stop everyone who can still edit the bubble. Button cleanup must
        # be the last Telegram edit, otherwise a late frame would restore it (review
        # 24.11.2026: reproduced with a controlled Telegram double).
        if read_task is not None and not read_task.done():
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await read_task
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
        if not keep_placeholder_live and status is not None:
            with contextlib.suppress(Exception):
                await status.stop()
            if getattr(status, "reply_markup", None) is not None:
                await status.clear_reply_markup()
        # In a private chat, remove the reply keyboard with a separate service message:
        # editMessageReplyMarkup accepts only inline keyboards.
        if (message is not None and getattr(message.chat, "type", None) == ChatType.PRIVATE
                and bot.STOP_BUTTON_ENABLED):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                remove = await bot._tg_call(
                    bot._answer_sender(message), "\u200b", parse_mode=None,
                    reply_markup=ReplyKeyboardRemove(), call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    retries=1,
                )
                if remove is not None and not bot._tg_outcome_unknown(remove):
                    await bot._tg_call(remove.delete, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)
        # The Control bubble is not reused: remove it on both success and early
        # fallback so the button does not remain next to the future answer.
        if _stop_control is not None:
            with contextlib.suppress(Exception):
                await bot._tg_call(_stop_control.delete, call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT)

    # Empty full_text never reaches here — there is no "Empty response" branch.
    # The scrub has already been applied before the final edit (see above).
    final_answer = full_text
    bot._record_model_outcome(provider, model_id,
                              bad=_detect_garbled_mix(full_text.strip()) and not _is_garbled_echo(full_text.strip(), user_text))
    final_answer = _history_safe_answer(final_answer)
    await _remember_turn(state, history_text, user_text, final_answer, history_meta)
    bot._record_quota_usage(provider, model_id)
    # Judge smoothness over the typing window, not the whole answer: the first chunk might
    # arrive at second 15, yielding frames=4 — not jumps, but a short tail after
    # of a long wait (prod 11.11.2026: frames=4 max_step=848 sec=20.4 was read
    # ambiguous: _ttft and paint_span distinguish these two cases).
    _now_end = time.monotonic()
    _ttft = (first_piece_ts - overall_start_ts) if first_piece_ts is not None else -1.0
    _paint_span = (_now_end - first_piece_ts) if first_piece_ts is not None else 0.0
    log.info("[stream] smoothness frames=%d max_step=%d chars=%d sec=%.1f ttft=%.1f paint_span=%.1f",
             paint_frames, paint_max_step, len(final_answer), _now_end - overall_start_ts, _ttft, _paint_span)
    return final_answer, None

async def _try_gemini_streaming(chat_id: int, user_text: str, message: Message, model_id: str, *, deadline: float | None = None, status=None, history_text=None, reuse_placeholder=None, wait_started=None, history_meta=None, effort=None) -> tuple[str | None, Message | None]:
    """The _run_streaming_reply wrapper for Gemini (builds contents/config)."""
    import bot
    conf = GEMINI_MODELS.get(model_id, {})
    if not conf.get("stream", True):
        return None, None
    state = bot.get_state(chat_id)
    contents = await bot._build_gemini_turn_contents(chat_id, user_text, history_meta=history_meta)
    call_contents, gconfig = bot._build_gemini_call_config(
        model_id, contents, memory_summary=state.get("memory_summary") or "", effort=effort,
    )
    piece_agen = _gemini_stream_pieces(model_id, call_contents, gconfig, deadline=deadline)
    return await _run_streaming_reply(chat_id, user_text, message, provider="gemini", model_id=model_id, piece_agen=piece_agen, deadline=deadline, status=status, history_text=history_text, reuse_placeholder=reuse_placeholder, wait_started=wait_started, history_meta=history_meta)

async def _try_munito_streaming(chat_id: int, user_text: str, message: Message, model_id: str, *, deadline: float | None = None, status=None, history_text=None, reuse_placeholder=None, wait_started=None, history_meta=None, effort=None) -> tuple[str | None, Message | None]:
    """_run_streaming_reply wrapper for Munito (SSE via chat/completions), with untrimmed memory; see ask_munito_text."""
    import bot
    messages = await bot._build_openai_turn_messages(
        chat_id, user_text, model_id, history_meta=history_meta,
    )
    # The generator goes through bot.: tests replace bot._munito_stream_pieces with a fake.
    piece_agen = bot._munito_stream_pieces(model_id, messages, deadline=deadline, effort=effort)
    return await _run_streaming_reply(chat_id, user_text, message, provider="munito", model_id=model_id, piece_agen=piece_agen, deadline=deadline, status=status, history_text=history_text, reuse_placeholder=reuse_placeholder, wait_started=wait_started, history_meta=history_meta)
