"""
lumen_media_flow.py — скачивание вложений из Telegram и учёт медиа-истории.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
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
            async with bot._media_download_slot():
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
    # Какой именно файл не влез — иначе в альбоме из 10 файлов отказ нечем
    # локализовать. Ссылка Telegram содержит токен в пути, поэтому только с
    # заменённым токеном (тот же приём, что в тексте ошибок выше).
    safe_url = url.replace(bot.BOT_TOKEN, "<TOKEN>") if bot.BOT_TOKEN else url
    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > bot.TELEGRAM_DOWNLOAD_MAX_BYTES:
                log.warning("[media] Telegram file over the cap by Content-Length: %s", safe_url)
                raise _MediaTooLargeError(int(content_length), bot.TELEGRAM_DOWNLOAD_MAX_BYTES)
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if total > bot.TELEGRAM_DOWNLOAD_MAX_BYTES:
            log.warning("[media] Telegram file over the cap mid-stream: %s", safe_url)
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

def _fresh_generation_ref(state: dict[str, Any], sender_id: int | None) -> bool:
    """Своя генерация не старше окна: только тогда слабый глагол ("добавь") —
    правка, иначе "сделай мне кофе" рисовало бы поверх древнего фото."""
    import bot
    if not isinstance(state, dict):
        return False
    drawn_at = state.get("last_drawn_at")
    drawn_user = state.get("last_drawn_user")
    if not isinstance(drawn_at, (int, float)) or isinstance(drawn_at, bool):
        return False
    if drawn_user is not None and sender_id is not None and drawn_user != sender_id:
        return False
    return time.time() - float(drawn_at) <= bot.DRAW_EDIT_CONTEXT_TTL_SEC


async def _resolve_edit_references(
    message: Any, state: dict[str, Any], *, is_private: bool, implicit: bool,
) -> list[tuple[bytes, str]]:
    """Референсы правки: вложение, реплай, затем (если других нет) свежая своя
    генерация; для сильных глаголов — свежее фото автора. До двух картинок:
    «совмести это с этим» у klein работает как multi-image (замер 10.11.2026)."""
    import bot
    import lumen_memory
    from lumen_media import _media_file_id_and_mime, _msg_media_source

    def _is_image(src: Any) -> bool:
        _, mime, _ = _media_file_id_and_mime(src)
        return (mime or "").startswith("image/")

    sender = getattr(message, "from_user", None)
    sender_id = sender.id if sender is not None else None
    collected: list[tuple[bytes, str]] = []
    seen_ids: set[str] = set()

    src = _msg_media_source(message)
    if src is not None and _is_image(src):
        res = await bot._download_message_attachment(src)
        if res:
            data, mime, _ = res
            if (mime or "").startswith("image/"):
                collected.append((data, mime))
                fid, _mime, _ = _media_file_id_and_mime(src)
                if fid:
                    seen_ids.add(fid)
                bot._save_media_to_history(src, state, sender_id)

    reply = getattr(message, "reply_to_message", None)
    if len(collected) < 2 and reply is not None:
        reply_src = _msg_media_source(reply)
        if reply_src is not None and _is_image(reply_src):
            fid, mime, _ = _media_file_id_and_mime(reply_src)
            if fid and fid not in seen_ids:
                try:
                    fetched = await bot._fetch_media(fid, mime)
                except _MediaTooLargeError:
                    # Второй референс не влез — правим по уже собранному первому,
                    # а не отменяем всю правку (ревью 10.11.2026).
                    fetched = None
                if fetched:
                    collected.append(fetched)
                    # Корзина владельца фото, а не ответившего: иначе позже «добавь
                    # звезду» без реплая правит чужой снимок (ревью 10.11.2026).
                    _reply_owner = getattr(reply, "from_user", None)
                    bot._save_media_to_history(
                        reply_src, state,
                        _reply_owner.id if _reply_owner is not None else sender_id,
                    )

    # Неявный контекст подключаем только когда явных референсов нет: иначе
    # «совмести эти два» тянул бы ещё и старую картинку из истории.
    if not collected and _fresh_generation_ref(state, sender_id):
        from lumen_message_parse import _find_recent_media_by_category

        buckets = state.get("recent_media_ids") if isinstance(state, dict) else None
        if isinstance(buckets, dict):
            own_bucket = buckets.get(str(sender_id)) if sender_id is not None else None
            candidates = [own_bucket] if own_bucket else (list(buckets.values()) if is_private else [])
            for bucket in candidates:
                fid_mime = _find_recent_media_by_category(bucket, "photo")
                if not fid_mime:
                    continue
                fetched = await bot._fetch_media(fid_mime[0], fid_mime[1])
                if fetched:
                    collected.append(fetched)
                    break

    if not collected and implicit:
        if not is_private and sender_id is None:
            return []
        found = lumen_memory.recent_media(
            state, "photo", window=6, max_items=1,
            sender_id=sender_id, any_sender=is_private,
        )
        if found:
            entry, ref = found[0]
            ts = entry.get("ts")
            fresh = (
                isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and time.time() - float(ts) <= bot.DRAW_EDIT_CONTEXT_TTL_SEC
            )
            if fresh:
                fetched = await bot._fetch_media(ref["file_id"], ref["mime"])
                if fetched:
                    collected.append(fetched)

    return collected[:2]


async def _resolve_edit_reference(
    message: Any, state: dict[str, Any], *, is_private: bool, implicit: bool,
) -> tuple[bytes, str] | None:
    """Первый референс правки. Оставлено для одиночных вызовов и тестов;
    многокартиночный путь обязан идти через _resolve_edit_references, иначе
    второй референс молча теряется (ревью 10.11.2026)."""
    refs = await _resolve_edit_references(
        message, state, is_private=is_private, implicit=implicit,
    )
    return refs[0] if refs else None


async def _download_message_attachment(source: Any) -> tuple[bytes, str, str] | None:
    """Байты вложения сразу в память: временный файл на диске (запись и обратное
    чтение до 20МБ) был лишним кругом на каждое вложение."""
    import bot
    file_id, mime, filename = _media_file_id_and_mime(source)
    if not file_id:
        return None
    data, real_mime = await bot._download_telegram_file_bytes(file_id)
    final_mime = _sanitize_mime_type(filename or "", mime)
    if final_mime == "application/octet-stream" or not final_mime:
        final_mime = _sanitize_mime_type(None, real_mime)
    if final_mime == "application/octet-stream" or not final_mime:
        final_mime = mime or "application/octet-stream"
    return data, final_mime, filename or f"attachment{_mime_suffix(mime, filename)}"

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
