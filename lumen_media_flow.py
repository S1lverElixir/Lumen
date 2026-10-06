"""
lumen_media_flow.py — скачивание вложений из Telegram и учёт медиа-истории.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

from lumen_chat_state import MAX_MEDIA_BUCKETS_PER_CHAT, MAX_MEDIA_RECENT_IDS
from lumen_media import (
    _sanitize_mime_type,
    _media_file_id_and_mime,
    _mime_suffix,
)

log = logging.getLogger("bot")

class _MediaTooLargeError(RuntimeError):
    """Файл больше капа скачивания: вызывающий код отвечает пользователю
    понятным текстом вместо слепого анализа (аудит D1, 30.09.2026)."""
    def __init__(self, size_bytes: int, cap_bytes: int):
        self.size_bytes = size_bytes
        self.cap_bytes = cap_bytes
        super().__init__(f"file size {size_bytes} exceeds the {cap_bytes} byte download cap")

async def _download_telegram_file_bytes(file_id: str, *, timeout: float | None = None, retries: int = 1) -> tuple[bytes, str]:
    # Один ретрай getFile/скачивания: единичный сбой прокси иначе слепил бота ("NOT_FOUND" в логах).
    import bot
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            file = await asyncio.wait_for(bot.bot.get_file(file_id), timeout=bot.TELEGRAM_GET_FILE_TIMEOUT)
            file_path = getattr(file, "file_path", None) or getattr(file, "path", None)
            if not file_path:
                raise RuntimeError("File path is empty")
            declared_size = getattr(file, "file_size", None)
            if isinstance(declared_size, int) and declared_size > bot.TELEGRAM_DOWNLOAD_MAX_BYTES:
                # Заранее большой размер — без скачивания, с честным отказом выше.
                raise _MediaTooLargeError(declared_size, bot.TELEGRAM_DOWNLOAD_MAX_BYTES)
            session = await bot._get_telegram_session()
            url = f"{bot.TELEGRAM_API_BASE_URL}/file/bot{bot.BOT_TOKEN}/{file_path}"
            async with session.get(url, timeout=timeout or bot.TELEGRAM_MEDIA_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await _read_capped_telegram_body(resp, url)
                mime = resp.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip()
            real_mime = _sanitize_mime_type(file_path, mime)
            return data, real_mime
        except _MediaTooLargeError:
            # Кап — не сеть: ретрай бессмыслен, пробрасываем сразу.
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                retry_msg = str(exc) or repr(exc) or type(exc).__name__
                if bot.BOT_TOKEN:
                    retry_msg = retry_msg.replace(bot.BOT_TOKEN, "<TOKEN>")
                log.warning('[media] Attempt %d/%d to download file_id %s failed, retrying in 0.5s: %s', attempt + 1, retries + 1, file_id, retry_msg)
                await asyncio.sleep(0.5)
    exc_str = str(last_exc) or repr(last_exc) or type(last_exc).__name__
    if bot.BOT_TOKEN:
        exc_str = exc_str.replace(bot.BOT_TOKEN, "<TOKEN>")
    raise RuntimeError(f"Network error in download_telegram_file_bytes: {exc_str}") from None

async def _read_capped_telegram_body(resp: Any, url: str) -> bytes:
    """Тело ответа Telegram с капом: Content-Length врёт/отсутствует —
    та же проверка потоково по факту (см. кап TikWM-скачиваний)."""
    import bot
    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > bot.TELEGRAM_DOWNLOAD_MAX_BYTES:
                raise _MediaTooLargeError(int(content_length), bot.TELEGRAM_DOWNLOAD_MAX_BYTES)
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > bot.TELEGRAM_DOWNLOAD_MAX_BYTES:
            raise _MediaTooLargeError(total, bot.TELEGRAM_DOWNLOAD_MAX_BYTES)
        chunks.append(chunk)
    return b"".join(chunks)

def _save_media_to_history(source: Any, state: dict[str, Any], user_id: int | None) -> None:
    file_id, mime, _ = _media_file_id_and_mime(source)
    if not file_id or user_id is None:
        return
    buckets: dict[str, deque] = state.setdefault("recent_media_ids", {})
    if not isinstance(buckets, dict):
        buckets = {}
        state["recent_media_ids"] = buckets
    key = str(user_id)
    if key in buckets:
        # Поднимаем активный бакет в хвост: вытеснение ниже бьёт по давно неактивным.
        buckets[key] = buckets.pop(key)
    elif len(buckets) >= MAX_MEDIA_BUCKETS_PER_CHAT:
        buckets.pop(next(iter(buckets)))
    recent = buckets.get(key)
    if not isinstance(recent, deque):
        recent = deque(maxlen=MAX_MEDIA_RECENT_IDS)
        buckets[key] = recent
    if not recent or recent[-1][0] != file_id:
        recent.append((file_id, mime or "application/octet-stream"))

async def _download_message_attachment_to_tmp(source: Any) -> tuple[str, str, str] | None:
    import bot
    file_id, mime, filename = _media_file_id_and_mime(source)
    if not file_id:
        return None
    suffix = _mime_suffix(mime, filename)
    fd, tmp_path = tempfile.mkstemp(prefix="tg_media_", suffix=suffix)
    os.close(fd)
    try:
        data, real_mime = await bot._download_telegram_file_bytes(file_id)
        final_mime = _sanitize_mime_type(filename or "", mime)
        if final_mime == "application/octet-stream" or not final_mime:
             final_mime = _sanitize_mime_type(None, real_mime)
        if final_mime == "application/octet-stream" or not final_mime:
             final_mime = mime or "application/octet-stream"
        # Запись до 20МБ — в потоке, loop не стопорим.
        await asyncio.to_thread(Path(tmp_path).write_bytes, data)
        return tmp_path, final_mime, filename or Path(tmp_path).name
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise

async def _fetch_media(file_id: str, mime: str) -> tuple[bytes, str] | None:
    import bot
    if not file_id:
        return None
    try:
        data, real_mime = await bot._download_telegram_file_bytes(file_id)
        final_mime = _sanitize_mime_type(None, mime)
        if final_mime == "application/octet-stream":
            final_mime = _sanitize_mime_type(None, real_mime)
        return data, final_mime
    except _MediaTooLargeError:
        # Честный отказ выше по стеку, а не тихое None.
        raise
    except Exception as exc:
        log.warning("[media] Download media failed for file_id %s: %s", file_id, exc)
        return None
