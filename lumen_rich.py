"""
lumen_rich.py — отправка сообщений: гости, Rich Messages, чанкинг (вынесено
из bot.py, P2 аудита).

Связи с рантаймом bot.py — только через отложенный `import bot` внутри функций.
bot.py реэкспортирует имена — `bot._send_text`, `bot._safe_reply` и т.п.
в тестах и вызывающем коде не менялись.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
from typing import Any

from aiogram.enums import ParseMode
from aiogram.types import (
    InlineQueryResultArticle,
    InputRichMessage,
    InputTextMessageContent,
    Message,
    ReplyParameters,
)

from lumen_formatting import (
    _md_to_html,
    _md_to_rich_html,
    _split_text_chunks,
    _strip_markdown,
    _truncate_html_to_fit,
)

log = logging.getLogger("bot")

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
        input_message_content=InputTextMessageContent(message_text=payload, parse_mode="HTML"),
    )
    try:
        await bot.telegram_api_call("answerGuestQuery", {
            "guest_query_id": str(qid),
            "result": res_art.model_dump(exclude_none=True),
        }, request_timeout=10)
    except Exception as exc:
        log.warning("[guest] Failed answering guest: %s", exc)

def _is_real_telegram_message(res: Any) -> bool:
    """Настоящий Message от Telegram API, а не тестовая заглушка: у реальных
    ответов message_id — int. Нужно, чтобы в тестах (фейковые боты без
    send_rich_message/edit_message_text) рич-путь тихо откатывался на legacy,
    а не "успешно" ронял ветку через MagicMock."""
    return isinstance(res, Message)


async def _try_send_rich(message: Message, rich_html: str, *, is_first_chunk: bool, **kwargs: Any) -> Any:
    """Отправка чанка через sendRichMessage (таблицы/заголовки/математика).
    Возвращает отправленное сообщение или None — тогда вызывающий код идёт
    обычным HTML-путём. Флаг RICH_MESSAGES_ENABLED — рубильник на случай
    проблем с рендером (см. комментарий у флага).
    Reply threading — ТОЛЬКО для первого чанка (is_first_chunk): иначе каждый
    кусок длинного ответа придёт отдельным ответом с нотификацией, а не
    продолжением (найдено код-ревью). parse_mode/reply_parameters из kwargs
    не пробрасываются: у рич-метода их нет (parse_mode) или он строится здесь
    (reply threading) — чужое значение дало бы TypeError/невалидный запрос,
    а aiogram сложил бы неизвестные kwargs в тело запроса молча."""
    import bot
    if not bot.RICH_MESSAGES_ENABLED:
        return None
    sender = getattr(bot.bot, "send_rich_message", None)
    if sender is None or message is None:
        return None
    kwargs.pop("parse_mode", None)
    reply_parameters = kwargs.pop("reply_parameters", None)
    if reply_parameters is None and is_first_chunk and isinstance(getattr(message, "message_id", None), int):
        reply_parameters = ReplyParameters(message_id=message.message_id)
    call_timeout = kwargs.pop("call_timeout", bot.TELEGRAM_REQUEST_TIMEOUT)
    res = await bot._tg_call(
        sender, chat_id=message.chat.id, rich_message=InputRichMessage(html=rich_html),
        reply_parameters=reply_parameters, call_timeout=call_timeout, **kwargs,
    )
    return res if bot._is_real_telegram_message(res) else None


async def _try_edit_rich(msg: Message | None, rich_html: str, **kwargs: Any) -> bool:
    """Правка сообщения через editMessageText+rich_message. Возвращает True
    только при реальном успехе — иначе вызывающий код идёт legacy-правкой.
    Фейковые сообщения тестов (без int chat.id/message_id) отсекаются гейтом."""
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
        editor, text=None, rich_message=InputRichMessage(html=rich_html),
        chat_id=chat_id, message_id=message_id, call_timeout=call_timeout, **kwargs,
    )
    return bot._is_real_telegram_message(res)


async def _send_text(message: Message, text: str, parse_html: bool = True, **kwargs: Any) -> None:
    import bot
    if bot.is_guest_message(message):
        await bot._answer_guest_text(message, text)
        return

    chunks = _split_text_chunks(text, bot.TG_MAX_LEN)
    for i, chunk in enumerate(chunks):
        chunk_kwargs = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        if parse_html:
            # Сначала рич (таблицы/заголовки/математика), при любом неуспехе —
            # обычный HTML-путь ниже. Reply threading — только у первого чанка.
            rich_res = await bot._try_send_rich(message, _md_to_rich_html(chunk), is_first_chunk=(i == 0), **chunk_kwargs)
            if rich_res is not None:
                continue
        if i == 0:
            res = await bot._tg_call(
                message.reply, _md_to_html(chunk) if parse_html else chunk,
                call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                parse_mode=ParseMode.HTML if parse_html else None,
                **chunk_kwargs,
            )
            if res is None and parse_html:
                res = await bot._tg_call(
                    message.reply, _strip_markdown(chunk),
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

async def _delete_message_quietly(msg: Message | None) -> None:
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.delete()

async def _edit_message_quietly(msg: Message | None, text: str, **kwargs: Any) -> bool:
    import bot
    if msg is None:
        return False
    try:
        # Сначала рич-правка (таблицы/заголовки/математика — см. _try_edit_rich),
        # при любом неуспехе — обычный HTML-путь, затем голый текст. Порядок
        # важен: таблицы и формулы видны только через рич.
        if await bot._try_edit_rich(msg, _md_to_rich_html(text), **kwargs):
            return True
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        res = await bot._tg_call(msg.edit_text, _md_to_html(text), **kwargs)
        if res is not None:
            return True
        kwargs["parse_mode"] = None
        # Последний рубеж — голый текст БЕЗ markdown-синтаксиса (см.
        # _strip_markdown): сырые `**` в чате хуже потери жирности.
        res = await bot._tg_call(msg.edit_text, _strip_markdown(text), **kwargs)
        return res is not None
    except Exception:
        return False
