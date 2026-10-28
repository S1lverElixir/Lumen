"""lumen_status.py — живой статус-плейсхолдер: этапы обработки запроса в одном сообщении (поиск, источники, таймер ожидания)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram.types import Message

log = logging.getLogger("bot")

_PROGRESS_FRAMES = (".", "..", "…")
# Страховка от утечки тикера, если обработчик упал между созданием статуса и
# маршрутом: дольше этого статус сам себя останавливает.
_STATUS_MAX_LIFETIME_SEC = 300.0


class StatusMessage:
    """Сообщение-статус: завершённые этапы + текущая фраза с анимацией или таймером.
    Все правки глотают ошибки: удалённое сообщение не должно ломать ответ."""

    def __init__(self, message: Message, lang: str):
        self.message = message
        self.lang = lang
        self._done: list[str] = []
        self._phrase_key = ""
        self._timed = False
        self._phase_started = time.monotonic()
        self._created = time.monotonic()
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
        """Завершённый этап отдельной строкой над текущей фразой."""
        import bot
        self._done.append(bot._t(self.lang, key, **kwargs))

    async def set_phase(self, key: str, *, timed: bool = False) -> None:
        """Смена текущей фразы с немедленной правкой, не дожидаясь тика."""
        import bot
        self._set_phrase(key, timed=timed)
        with contextlib.suppress(Exception):
            await bot._tg_call(self.message.edit_text, self._render(0), parse_mode=None, call_timeout=15.0)

    def _set_phrase(self, key: str, *, timed: bool) -> None:
        self._phrase_key = key
        self._timed = timed
        self._phase_started = time.monotonic()

    def _render(self, frame_idx: int) -> str:
        import bot
        lines = list(self._done)
        if self._timed:
            elapsed = max(0, int(time.monotonic() - self._phase_started))
            lines.append(bot._t(self.lang, self._phrase_key, sec=elapsed))
        else:
            lines.append(bot._t(self.lang, self._phrase_key) + _PROGRESS_FRAMES[frame_idx % len(_PROGRESS_FRAMES)])
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
