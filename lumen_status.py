"""lumen_status.py — живой статус-плейсхолдер в духе Claude: анимированная искра, этапы, «Думал N с» и свёрнутый блок активности."""
from __future__ import annotations

import asyncio
import contextlib
import html
import time

from aiogram.types import Message

from lumen_formatting import _is_table_separator, _md_to_html
from lumen_lang import t as _lang_t

# Искры как в Claude Code: кадры спиннера, а не точки.
_SPINNER_FRAMES = ("·", "✢", "✳", "✶", "✻", "✽")
# Страховка от утечки тикера, если обработчик упал между созданием статуса и
# маршрутом: дольше этого статус сам себя останавливает.
_STATUS_MAX_LIFETIME_SEC = 300.0


def thought_block(lang: str, sec: int, answer_md: str) -> tuple[str | None, str]:
    """Свёрнутый блок «✻ Thought for Ns» над ответом: (HTML, простой текст).
    Таблицы возвращают None — их показываем обычным rich-путём без блока."""
    if any(_is_table_separator(line) for line in answer_md.splitlines()):
        return None, answer_md
    line = "✻ " + _lang_t(lang, "progress_thought_for", sec=max(0, int(sec)))
    html_block = "<blockquote expandable>" + html.escape(line) + "</blockquote>\n\n" + _md_to_html(answer_md)
    return html_block, line + "\n\n" + answer_md


class StatusMessage:
    """Сообщение-статус: завершённые этапы + текущая фраза с искрой и таймером.
    Все правки глотают ошибки: удалённое сообщение не должно ломать ответ."""

    def __init__(self, message: Message, lang: str):
        self.message = message
        self.lang = lang
        self._done: list[str] = []
        self._sources: list[str] = []
        self._phrase_key = ""
        self._timed = False
        self._phase_started = time.monotonic()
        self._created = time.monotonic()
        self._thought_seconds: int | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    @classmethod
    async def start(cls, reply_to: Message, lang: str, key: str, *, timed: bool = False) -> "StatusMessage | None":
        """Отправляет первую строку статуса; None — Telegram не принял сообщение."""
        import bot
        status = cls(message=reply_to, lang=lang)
        status._set_phrase(key, timed=timed)
        sent = await bot._tg_call(
            reply_to.reply, status._render(0), parse_mode=None,
            call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
        )
        if sent is None:
            return None
        status.message = sent
        status._task = asyncio.create_task(status._tick())
        return status

    def add_done(self, key: str, **kwargs: object) -> None:
        """Завершённый этап отдельной строкой с галочкой, как в трейсе Claude."""
        self._done.append("✓ " + _lang_t(self.lang, key, **kwargs))

    def set_sources(self, hosts: list[str]) -> None:
        """Домены найденных источников для раскрытого блока активности."""
        self._sources = [h for h in hosts if h][:5]

    async def set_phase(self, key: str, *, timed: bool = False) -> None:
        """Смена текущей фразы с немедленной правкой, не дожидаясь тика."""
        import bot
        self._set_phrase(key, timed=timed)
        with contextlib.suppress(Exception):
            await bot._tg_call(self.message.edit_text, self._render(0), parse_mode=None, call_timeout=15.0)

    async def finish(self) -> None:
        """Фиксирует длительность ожидания и останавливает анимацию."""
        if self._thought_seconds is None:
            self._thought_seconds = max(0, int(time.monotonic() - self._phase_started))
        await self.stop()

    def activity_prefix(self) -> tuple[str, str]:
        """(HTML-блок для ответа, тот же блок простым текстом); пусто — активности нет."""
        thought = self._thought_seconds if self._thought_seconds is not None else max(
            0, int(time.monotonic() - self._phase_started))
        lines = ["✻ " + _lang_t(self.lang, "progress_thought_for", sec=thought)]
        lines.extend(self._done)
        if self._phrase_key and not self._timed and self._phrase_key != "progress_thinking":
            lines.append("✓ " + _lang_t(self.lang, self._phrase_key))
        if self._sources:
            lines.extend("· " + host for host in self._sources)
        plain = "\n".join(lines)
        html_block = "<blockquote expandable>" + "\n".join(html.escape(line) for line in lines) + "</blockquote>"
        return html_block, plain

    def compose_answer(self, answer_md: str) -> tuple[str | None, str]:
        """Ответ с свёрнутым блоком активности: (HTML для parse_mode=HTML, простой текст).
        Таблицы отдаём обычным путём (rich-сообщение): цитата сломала бы их разметку."""
        if any(_is_table_separator(line) for line in answer_md.splitlines()):
            return None, answer_md
        html_block, plain_block = self.activity_prefix()
        if not html_block:
            return None, answer_md
        return html_block + "\n\n" + _md_to_html(answer_md), plain_block + "\n\n" + answer_md

    def _set_phrase(self, key: str, *, timed: bool) -> None:
        self._phrase_key = key
        self._timed = timed
        self._phase_started = time.monotonic()

    def _render(self, frame_idx: int) -> str:
        spark = _SPINNER_FRAMES[frame_idx % len(_SPINNER_FRAMES)]
        lines = list(self._done)
        if self._timed:
            elapsed = max(0, int(time.monotonic() - self._phase_started))
            lines.append(spark + " " + _lang_t(self.lang, self._phrase_key, sec=elapsed))
        else:
            lines.append(spark + " " + _lang_t(self.lang, self._phrase_key))
        return "\n".join(lines)

    async def _tick(self) -> None:
        import bot
        try:
            await bot._dots_sleep(bot._DOTS_START_AFTER_SEC)
            frame_idx = 1
            while not self._stopped:
                if time.monotonic() - self._created > _STATUS_MAX_LIFETIME_SEC:
                    return
                with contextlib.suppress(Exception):
                    await bot._tg_call(
                        self.message.edit_text, self._render(frame_idx),
                        parse_mode=None, call_timeout=15.0,
                    )
                frame_idx += 1
                await bot._dots_sleep(bot._DOTS_TICK_SEC)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        """Останавливает анимацию; само сообщение остаётся для ответа."""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def delete(self) -> None:
        """Останавливает анимацию и убирает сообщение (провал маршрута, отмена)."""
        import bot
        await self.stop()
        await bot._delete_message_quietly(self.message)
