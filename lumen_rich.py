"""lumen_rich.py — message sending: guests, Rich Messages, chunking.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatType, ParseMode
from aiogram.types import (
    InlineQueryResultArticle,
    InputRichMessage,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)

from lumen_formatting import (
    _md_to_html,
    _md_to_rich_html,
    _tg_len,
    _split_text_chunks,
    _strip_markdown,
    _truncate_html_to_fit,
)

log = logging.getLogger("bot")

# Answers with links must stay compact, as in Claude: Telegram
# otherwise inflates them with a preview card across the whole block (prod 23.11.2026).
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _rich_html_with_limit(text: str) -> str:
    """Rich formatting for the entire message: if splitting a table inflated the HTML,
    repeat the conversion without split while preserving the table itself (prod 24.11.2026)."""
    import bot
    rich_html = _md_to_rich_html(text)
    if _tg_len(rich_html) > bot.TG_MAX_LEN:
        rich_html = _md_to_rich_html(text, split_tables=False)
    return rich_html

def is_guest_message(message: Message | dict) -> bool:
    if isinstance(message, dict):
        return bool(message.get("guest_query_id"))
    return bool(getattr(message, "guest_query_id", None))

async def _answer_guest_text(message: Message, text: str) -> None:
    import bot
    qid = getattr(message, "guest_query_id", None)
    if not qid:
        return
    payload = _truncate_html_to_fit(text, bot.TG_MAX_LEN)
    res_id = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:32]
    res_art = InlineQueryResultArticle(
        id=res_id, title=bot._t(message.chat.id, "inline_answer_title"),
        input_message_content=InputTextMessageContent(message_text=payload, parse_mode="HTML",
                                                       link_preview_options=NO_LINK_PREVIEW),
    )
    try:
        await bot.telegram_api_call("answerGuestQuery", {
            "guest_query_id": str(qid),
            "result": res_art.model_dump(exclude_none=True),
        }, request_timeout=10)
    except Exception as exc:
        log.warning("[guest] Failed answering guest: %s", exc)

def _is_real_telegram_message(res: Any) -> bool:
    """Only a real Message (int message_id): test fakes quietly fall back to legacy behavior."""
    return isinstance(res, Message)


def _edit_with_not_modified_success(editor: Any, msg: Message) -> Any:
    """Not-modified is a semantic success, not a reason to remove formatting.
    The transport returns None for this response too, so catch the exception locally
    before _tg_call (R5-12, audit 26.11.2026).
    """
    async def edit(*args: Any, **kwargs: Any) -> Any:
        try:
            return await editor(*args, **kwargs)
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return msg
            raise
    return edit


def _answer_sender(message: Message):
    """Where to send the answer: in a private chat — a normal message without a quoted reply,
    as in Claude; in a group — a reply for context. Test fakes without a real
    aiogram Bot receive a reply (previous behavior)."""
    import bot
    chat = getattr(message, "chat", None)
    if (getattr(chat, "type", None) == ChatType.PRIVATE
            and isinstance(getattr(bot, "bot", None), Bot)):
        async def _send(text: str, **kwargs: Any):
            # Text as a separate kwarg: partial(chat_id=...) + positional text
            # caused "multiple values for argument 'chat_id'" and killed the stream
            # with responses in DMs (production 28.10.2026).
            return await bot.bot.send_message(chat_id=chat.id, text=text, **kwargs)
        return _send
    return message.reply


async def _try_send_rich(message: Message, rich_html: str, *, is_first_chunk: bool, **kwargs: Any) -> Any:
    """Rich send; definite failure returns None, unknown delivery preserves the sentinel.
    Only the first chunk is threaded, so each piece does not make a separate notification sound."""
    import bot
    if not bot.RICH_MESSAGES_ENABLED:
        return None
    sender = getattr(bot.bot, "send_rich_message", None)
    if sender is None or message is None:
        return None
    kwargs.pop("parse_mode", None)
    kwargs.pop("link_preview_options", None)
    reply_parameters = kwargs.pop("reply_parameters", None)
    is_private = getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE
    if (reply_parameters is None and is_first_chunk and not is_private
            and isinstance(getattr(message, "message_id", None), int)):
        reply_parameters = ReplyParameters(message_id=message.message_id)
    call_timeout = kwargs.pop("call_timeout", bot.TELEGRAM_REQUEST_TIMEOUT)
    res = await bot._tg_call(
        sender, chat_id=message.chat.id, rich_message=InputRichMessage(html=rich_html),
        reply_parameters=reply_parameters, call_timeout=call_timeout, **kwargs,
    )
    if bot._tg_outcome_unknown(res):
        return res
    return res if bot._is_real_telegram_message(res) else None


async def _try_edit_rich(msg: Message | None, rich_html: str, **kwargs: Any) -> Any:
    """Rich edit; True on success, None/False on definite failure, sentinel on unknown delivery."""
    import bot
    if not bot.RICH_MESSAGES_ENABLED or msg is None:
        return False
    editor = getattr(bot.bot, "edit_message_text", None)
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(msg, "message_id", None)
    if editor is None or not isinstance(chat_id, int) or not isinstance(message_id, int):
        return False
    kwargs.pop("parse_mode", None)
    call_timeout = kwargs.pop("call_timeout", 15.0)
    res = await bot._tg_call(
        _edit_with_not_modified_success(editor, msg),
        text=None, rich_message=InputRichMessage(html=rich_html),
        chat_id=chat_id, message_id=message_id, call_timeout=call_timeout, **kwargs,
    )
    if bot._tg_outcome_unknown(res):
        return res
    return bot._is_real_telegram_message(res)


async def _send_text(message: Message, text: str, parse_html: bool = True, **kwargs: Any) -> None:
    import bot
    if bot.is_guest_message(message):
        await bot._answer_guest_text(message, text)
        return

    chunks = _split_text_chunks(text, bot.TG_MAX_LEN)
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    sender = _answer_sender(message)
    for i, chunk in enumerate(chunks):
        chunk_kwargs = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        if i > 0:
            # A pause between chunks prevents flood control from a consecutive series.
            await bot._typing_sleep(bot.STREAM_TYPING_TICK_SEC)
        if parse_html and bot.RICH_MESSAGES_ENABLED:
            # Try rich content first (tables/headings/math); on any failure,
            # the regular HTML path below. Reply threading — only for the first chunk.
            # When Rich is disabled, do not spend time on conversion (0.3-0.7 ms/chunk).
            # Rich HTML length was not checked: a chunk within the 4096 markdown limit
            # became bloated with markup and got a 400 with a silent fallback to HTML
            # (external audit 16.11.2026). Send an overly long item immediately via the normal path.
            rich_html = _rich_html_with_limit(chunk)
            if _tg_len(rich_html) <= bot.TG_MAX_LEN:
                rich_res = await bot._try_send_rich(message, rich_html, is_first_chunk=(i == 0), **chunk_kwargs)
                if rich_res is not None:
                    continue
        if i == 0:
            res = await bot._tg_call(
                sender, _md_to_html(chunk) if parse_html else chunk,
                call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                parse_mode=ParseMode.HTML if parse_html else None,
                **chunk_kwargs,
            )
            if res is None and parse_html:
                res = await bot._tg_call(
                    sender, _strip_markdown(chunk),
                    call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    parse_mode=None,
                    **chunk_kwargs,
                )
        else:
            res = await bot._tg_call(
                bot.bot.send_message,
                chat_id=message.chat.id, text=_md_to_html(chunk) if parse_html else chunk,
                call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                parse_mode=ParseMode.HTML if parse_html else None,
                **chunk_kwargs,
            )
            if res is None and parse_html:
                await bot._tg_call(
                    bot.bot.send_message,
                    chat_id=message.chat.id, text=_strip_markdown(chunk),
                    call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    parse_mode=None,
                    **chunk_kwargs,
                )

async def _safe_reply(message: Message, text: str, parse_html: bool = True, **kwargs: Any) -> None:
    import bot
    await bot._send_text(message, text, parse_html=parse_html, **kwargs)

async def _delete_message_quietly(msg: Message | None) -> bool:
    if msg is None:
        return False
    try:
        await msg.delete()
        return True
    except Exception:
        return False

async def _edit_message_quietly(msg: Message | None, text: str, **kwargs: Any) -> bool:
    import bot
    if msg is None:
        return False
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        # Order: rich → HTML → plain text; tables are visible only through rich.
        rich_html = _rich_html_with_limit(text)
        if bot.RICH_MESSAGES_ENABLED and _tg_len(rich_html) <= bot.TG_MAX_LEN:
            rich_res = await bot._try_edit_rich(msg, rich_html, **kwargs)
            if rich_res is True:
                return True
            if bot._tg_outcome_unknown(rich_res):
                # Callers use a boolean `if not edited` check: unknown must close the retry
                # path, otherwise a legacy HTML/plain duplicate follows an accepted Rich response.
                return True
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        res = await bot._tg_call(_edit_with_not_modified_success(msg.edit_text, msg), _md_to_html(text), **kwargs)
        if res is not None:
            return True
        kwargs["parse_mode"] = None
        # The last line of defense — plain text WITHOUT markdown syntax (see
        # _strip_markdown): raw `**` in chat is worse than losing bold.
        res = await bot._tg_call(msg.edit_text, _strip_markdown(text), **kwargs)
        return res is not None
    except Exception:
        return False
