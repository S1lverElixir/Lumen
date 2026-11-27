"""lumen_status.py — a Claude-style live status placeholder: animated indicator, stages, \"Thinking for N s\", and a collapsed activity block."""
from __future__ import annotations

import asyncio
import contextlib
import html
import time

from aiogram.types import InlineKeyboardMarkup, Message

from lumen_formatting import _is_table_separator, _md_to_html, _tg_len
from lumen_lang import t as _lang_t
from lumen_rich import NO_LINK_PREVIEW
from lumen_typing_pace import take_edit_slot

# Claude indicator: three dots in a triangle, rotating around the center.
# Single glyphs instead of ASCII art do not spread apart on a proportional
# Telegram font. Full cycle: up → superscript → horizontal row along
# center → tilt → down → tilt → centered row → tilt. Symmetric
# the loop without a jump at the seam; intermediate tilts matter — without them, rotation
# looks like flickering up/down (checked against Claude video 29.10.2026).
_TRIO_FRAMES = ("∴", "⋰", "⋯", "⋱", "∵", "⋱", "⋯", "⋰")


def _spinner_glyph(frame_idx: int) -> str:
    return _TRIO_FRAMES[frame_idx % len(_TRIO_FRAMES)]
# Safety net against a ticker leak if the handler crashes between creating the status and
# route: beyond this, the status stops itself.
_STATUS_MAX_LIFETIME_SEC = 300.0
# In Claude, a completed stage uses the past tense, while a live phase uses the
# for real: media and link stages have separate keys; otherwise, in the trace
# "✓ Viewing image" remained visible (review 11.11.2026).
_DONE_KEY_BY_PHASE = {
    "progress_viewing_image": "progress_image_done",
    "progress_watching_video": "progress_video_done",
    "progress_reading_link": "progress_link_done",
    "progress_searching": "progress_search_done",
}


def thought_line_html(lang: str, sec: int) -> str:
    """One italic line, '✻ Thought for Ns', as in Claude. An empty line when
sec < 1: a quick answer without reasoning gets no activity line
(review 11.11.2026). Shared by the final answer and the first stream edit's header."""
    if sec < 1:
        return ""
    return "<i>✻ " + html.escape(_lang_t(lang, "progress_thought_for", sec=sec)) + "</i>"


async def _send_status_message(reply_to: Message, text: str) -> Message | None:
    """Send status so cancellation does not lose a message already accepted by Telegram."""
    import bot
    send_task = asyncio.create_task(bot._tg_call(
        bot._answer_sender(reply_to), text, parse_mode=None,
        link_preview_options=NO_LINK_PREVIEW,
        call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
    ))
    try:
        return await asyncio.shield(send_task)
    except asyncio.CancelledError:
        # Cancellation must not cancel an already sent request: wait for it to
        # the bounded _tg_call, and delete the returned message_id.
        sent = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            sent = await send_task
        if sent is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bot._delete_message_quietly(sent)
        raise


async def discard_status_start(task: asyncio.Task | None) -> None:
    """Finish an unpinned startup status and remove its bubble.

    The task is created in parallel with search. If prompt assembly fails before the
    caller receives StatusMessage, its ticker would otherwise outlive the request and
    keep changing the old message over subsequent ones (prod 23.11.2026).
    """
    if task is None:
        return
    # StatusMessage.start cleans up the message itself on cancellation, so here we can
    # wait for the task without risking the loss of an already sent message_id.
    status = None
    with contextlib.suppress(asyncio.CancelledError, Exception):
        status = await task
    if status is not None:
        await status.delete()


class StatusMessage:
    """Status message: completed stages + the current phrase with an animated indicator and a timer.
    All edits swallow errors: a deleted message must not break the answer."""

    def __init__(self, message: Message, lang: str):
        self.message = message
        self.lang = lang
        self.reply_markup = None
        self._done: list[str] = []
        self._sources: list[str] = []
        self._phrase_key = ""
        self._phrase_kwargs: dict[str, object] = {}
        self._literal = ""
        self._literal_timed = False
        self._timed = False
        self._phase_started = time.monotonic()
        self._created = time.monotonic()
        self._thinking_started: float | None = None
        self._thought_seconds: int | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._deleted = False
        # One bubble edit at a time: otherwise a late frame from an old phase would overwrite
        # a new one after delivery through the proxy (review 17.11.2026).
        self._edit_lock = asyncio.Lock()

    def _reply_markup_kwargs(self) -> dict:
        # The stop button lives on the same bubble; every ticker edit must
        # pass it explicitly, otherwise Telegram removes the keyboard
        # (prod 24.11.2026).
        return {"reply_markup": self.reply_markup} if self.reply_markup is not None else {}

    @classmethod
    async def start(cls, reply_to: Message, lang: str, key: str, *, timed: bool = False,
                    **kwargs: object) -> "StatusMessage | None":
        """Sends the first status line; None means Telegram did not accept the message."""
        status = cls(message=reply_to, lang=lang)
        status._set_phrase(key, timed=timed, **kwargs)
        sent = await _send_status_message(reply_to, status._render(0))
        import bot
        if sent is None or bot._tg_outcome_unknown(sent):
            # Review I21 round 2 (26.11.2026): the same invariant as in start_action —
            # status.message must be a real Message; a sentinel would break the ticker,
            # and the stream would take it as a placeholder and get lost entirely.
            return None
        status.message = sent
        status._task = asyncio.create_task(status._tick())
        return status

    def add_done(self, key: str, **kwargs: object) -> None:
        """A completed stage on its own line with a checkmark, as in the Claude trace."""
        self._done.append("✓ " + _lang_t(self.lang, key, **kwargs))

    def set_sources(self, hosts: list[str]) -> None:
        """Domains of found sources for the expanded activity block.
        Remove duplicates: Google News returns several links from the same domain."""
        seen: set[str] = set()
        unique: list[str] = []
        for host in hosts:
            if not host or host in seen:
                continue
            seen.add(host)
            unique.append(host)
            if len(unique) >= 5:
                break
        self._sources = unique

    async def set_phase(self, key: str, *, timed: bool = False, **kwargs: object) -> None:
        """Changes the current phrase and edits it immediately without waiting for the next tick. kwargs substitute into the stage line: Claude's live step names what the model is doing now, not just the kind of work (\"Searching the web for the ruble exchange rate\")."""
        import bot
        self._set_phrase(key, timed=timed, **kwargs)
        # An immediate edit must count toward the shared chat budget (force).
        take_edit_slot(self._chat_id(), force=True)
        async with self._edit_lock:
            with contextlib.suppress(Exception):
                await bot._tg_call(
                    self.message.edit_text, self._render(0), parse_mode=None, call_timeout=15.0,
                    **self._reply_markup_kwargs(),
                )

    @classmethod
    async def start_action(cls, reply_to: Message, lang: str, text: str) -> "StatusMessage | None":
        """Action status (downloading/generating/voice synthesis): an indicator, a phrase, and a live seconds counter, like tool statuses in Claude."""
        status = cls(message=reply_to, lang=lang)
        status._literal = text
        status._literal_timed = True
        sent = await _send_status_message(reply_to, status._render(0))
        import bot
        if sent is None or bot._tg_outcome_unknown(sent):
            # Review I21 (26.11.2026): status.message must be a real Message —
            # the tick, and edits call edit_text directly. Unknown outcome = no status.
            return None
        status.message = sent
        status._task = asyncio.create_task(status._tick())
        return status

    async def set_action(self, text: str, *, timed: bool = True) -> None:
        """Change the action phrase without stopping the animation."""
        import bot
        self._literal = text
        self._literal_timed = timed
        take_edit_slot(self._chat_id(), force=True)
        # Under the same lock as the tick and set_phase: a late frame must not
        # restore the old phrase after this edit (F-070, pass #5).
        async with self._edit_lock:
            with contextlib.suppress(Exception):
                await bot._tg_call(
                    self.message.edit_text, self._render(0), parse_mode=None, call_timeout=15.0,
                    **self._reply_markup_kwargs(),
                )

    async def set_action_key(self, key: str) -> None:
        await self.set_action(_lang_t(self.lang, key), timed=self._literal_timed)

    @classmethod
    def from_message(cls, message: Message, lang: str, key: str, **kwargs: object) -> "StatusMessage":
        """Status over a bubble that is ALREADY in the chat (the stream placeholder):
        no new message is sent, or two \"Thinking...\" bubbles would appear side by side (measurement 11.11.2026)."""
        status = cls(message=message, lang=lang)
        status.reply_markup = getattr(message, "reply_markup", None)
        status._set_phrase(key, timed=False, **kwargs)
        status._task = asyncio.create_task(status._tick())
        return status

    async def finish(self) -> None:
        """Records waiting duration and stops the animation."""
        if self._thought_seconds is None:
            self._thought_seconds = self._elapsed_seconds()
        await self.stop()

    def note_extra_thought(self, sec: float) -> None:
        """Do not let the final block understate elapsed time: the first chunk may have arrived
        before any text appeared on screen (painter, catch-up).
        """
        cur = self._thought_seconds or 0
        self._thought_seconds = max(cur, max(0, int(sec)))

    async def mark_stage_done(self) -> None:
        """Complete the early stage (file already downloaded) with a checkmark and start the live
        thinking timer; otherwise photos/videos would remain a static phrase without
        seconds while the model responds (review 28.10.2026)."""
        if self._phrase_key and not self._timed and self._phrase_key not in (
                "progress_thinking", "progress_retry"):
            self.add_done(_DONE_KEY_BY_PHASE.get(self._phrase_key, self._phrase_key))
            await self.set_phase("progress_thinking", timed=True)

    def _elapsed_seconds(self) -> int:
        # Measure duration from the start of thinking, not from the last phase:
        # the "trying again" fallback reset the timer to 0 (production 28.10.2026).
        start = self._thinking_started or self._created
        return max(0, int(time.monotonic() - start))

    def activity_prefix(self) -> tuple[str, str]:
        """(Response HTML prefix, the same prefix as plain text). Plain waiting is a cursor line like Claude's; searching/media with stages uses a collapsed quote."""
        thought = self._thought_seconds if self._thought_seconds is not None else self._elapsed_seconds()
        only_thought = not self._done and not self._sources and (
            not self._phrase_key or self._timed
            or self._phrase_key in ("progress_thinking", "progress_retry"))
        if only_thought:
            line_html = thought_line_html(self.lang, thought)
            if not line_html:
                # <1 s: there is no activity line at all, as in Claude (review 11.11.2026).
                return "", ""
            line = "✻ " + _lang_t(self.lang, "progress_thought_for", sec=thought)
            return line_html, line
        lines = [] if thought < 1 else ["✻ " + _lang_t(self.lang, "progress_thought_for", sec=thought)]
        lines.extend(self._done)
        if (self._phrase_key and not self._timed
                and self._phrase_key not in ("progress_thinking", "progress_retry")):
            lines.append("✓ " + _lang_t(self.lang, self._phrase_key, **self._phrase_kwargs))
        if self._sources:
            lines.extend("· " + host for host in self._sources)
        plain = "\n".join(lines)
        html_block = "<blockquote expandable>" + "\n".join(html.escape(line) for line in lines) + "</blockquote>"
        return html_block, plain

    def compose_answer(self, answer_md: str) -> tuple[str | None, str]:
        """(HTML for parse_mode=HTML, plain text) with an activity block."""
        html_text, plain_text, _reason = self.compose_answer_with_reason(answer_md)
        return html_text, plain_text

    def compose_answer_with_reason(self, answer_md: str) -> tuple[str | None, str, str]:
        """The same result plus the rejection reason: table, no_prefix, overflow, or composed.
        The stream must distinguish a table from a limit: only a table may return header_md."""
        if any(_is_table_separator(line) for line in answer_md.splitlines()):
            return None, answer_md, "table"
        html_block, plain_block = self.activity_prefix()
        if not html_block:
            return None, answer_md, "no_prefix"
        composed_html = html_block + "\n\n" + _md_to_html(answer_md)
        composed_plain = plain_block + "\n\n" + answer_md
        # R5-06 (audit 26.11.2026): chunking checks the body before the activity prefix.
        # At the boundary, disable only the prefix without cutting off or losing the answer.
        import bot
        if _tg_len(composed_html) > bot.TG_MAX_LEN or _tg_len(composed_plain) > bot.TG_MAX_LEN:
            return None, answer_md, "overflow"
        return composed_html, composed_plain, "composed"

    def paint_header_md(self) -> str:
        """A short activity line above the body during streaming (markdown, one line): with
        Claude it appears from the first character and does not disappear
        (11.11.2026 review); when sec < 1, it is absent entirely. The full stage
        block is placed only at the end."""
        thought = self._thought_seconds if self._thought_seconds is not None else self._elapsed_seconds()
        if thought < 1:
            return ""
        return "*✻ " + _lang_t(self.lang, "progress_thought_for", sec=thought) + "*"

    def _set_phrase(self, key: str, *, timed: bool, **kwargs: object) -> None:
        if key == "progress_thinking" and self._thinking_started is None:
            self._thinking_started = time.monotonic()
        self._phrase_key = key
        self._phrase_kwargs = kwargs
        self._literal = ""
        self._timed = timed
        self._phase_started = time.monotonic()

    def _render(self, frame_idx: int) -> str:
        glyph = _spinner_glyph(frame_idx)
        lines = list(self._done)
        if self._literal:
            line = glyph + " " + self._literal
            if self._literal_timed:
                elapsed = max(0, int(time.monotonic() - self._created))
                # At 0 s, do not show the counter at all, just like Claude (smoke 11.11.2026).
                if elapsed:
                    line += " · " + _lang_t(self.lang, "progress_seconds", sec=elapsed)
            lines.append(line)
        elif self._timed:
            elapsed = max(0, int(time.monotonic() - self._phase_started))
            lines.append(glyph + " " + _lang_t(self.lang, self._phrase_key, sec=elapsed,
                                               **self._phrase_kwargs))
        else:
            lines.append(glyph + " " + _lang_t(self.lang, self._phrase_key, **self._phrase_kwargs))
        return "\n".join(lines)

    async def _tick(self) -> None:
        import bot
        try:
            await bot._dots_sleep(bot._DOTS_START_AFTER_SEC)
            frame_idx = 1
            while not self._stopped:
                if time.monotonic() - self._created > _STATUS_MAX_LIFETIME_SEC:
                    return
                # Overall edit budget per chat: an occupied slot is a skipped frame,
                # not an animation delay (review 11.11.2026).
                if take_edit_slot(self._chat_id()):
                    async with self._edit_lock:
                        with contextlib.suppress(Exception):
                            await bot._tg_call(
                                self.message.edit_text, self._render(frame_idx),
                                parse_mode=None, call_timeout=15.0,
                                **self._reply_markup_kwargs(),
                            )
                frame_idx += 1
                await bot._dots_sleep(bot._DOTS_TICK_SEC)
        except asyncio.CancelledError:
            raise

    def _chat_id(self):
        return getattr(getattr(self.message, "chat", None), "id", None)

    def resume(self) -> None:
        """Resumes the ticker after a search pass reuses the bubble."""
        if self._deleted:
            return
        self._stopped = False
        if self._task is None:
            self._task = asyncio.create_task(self._tick())

    async def clear_reply_markup(self) -> None:
        """Removes the stop button from an abandoned bubble after a search fallback."""
        import bot
        async with self._edit_lock:
            self.reply_markup = None
            self.message.reply_markup = None
            edit = getattr(self.message, "edit_reply_markup", None)
            if edit is not None:
                with contextlib.suppress(Exception):
                    await bot._tg_call(
                        edit, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
                        call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    )

    async def stop(self) -> None:
        """Stops the animation; the message itself remains for the answer."""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def delete(self) -> None:
        """Stops the animation and removes the message (route failure, cancellation)."""
        if self._deleted:
            return
        import bot
        await self.stop()
        deleted = await bot._delete_message_quietly(self.message)
        # A failed Telegram delete is not completion: external cleanup must
        # be allowed to retry, otherwise a temporary failure will leave an empty bubble.
        if deleted:
            self._deleted = True
