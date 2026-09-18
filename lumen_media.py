"""
lumen_media.py — чистые утилиты медиа: поддерживаемые mime-типы, нормализация
mime по расширению, разбор file_id/mime/имени из объектов Telegram, суффиксы
файлов, источник медиа из сообщения и текст-подсказка по умолчанию для
вложения без подписи.

Вынесено из bot.py (срез монолита): ни одна функция не читает module globals
и не зовёт сеть/Telegram — тот же класс, что lumen_formatting.py/
lumen_security.py/lumen_message_parse.py. bot.py импортирует нужные имена
напрямую, поэтому `bot._sanitize_mime_type(...)` и т.д. продолжают работать
ровно как раньше (включая подмену в тестах через module globals).

Сознательно НЕ вынесено (остаётся в bot.py): _download_telegram_file_bytes
(сессия, BOT_TOKEN, ретраи через bot.get_file), _fetch_media и
_download_message_attachment_to_tmp (поверх него), _save_media_to_history
(пишет в state + константа MAX_MEDIA_RECENT_IDS, живущая рядом с остальным
состоянием в bot.py) — у всех есть зависимость от рантайма, вынос дал бы
либо циклический импорт, либо проброс половины bot.py параметрами.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

def _is_gemini_supported_mime(mime: str) -> bool:
    m = mime.lower()
    if m in ("image/png", "image/jpeg", "image/webp", "image/heic", "image/heif", "image/gif"):
        return True
    if m in ("audio/mp3", "audio/wav", "audio/ogg", "audio/aac", "audio/flac", "audio/mp4", "audio/m4a", "audio/mpeg", "audio/x-m4a"):
        return True
    if m in ("video/mp4", "video/mpeg", "video/mov", "video/avi", "video/flv", "video/mpg", "video/webm", "video/wmv", "video/quicktime"):
        return True
    if m in (
        "application/pdf",
        "text/plain",
        "text/html",
        "text/css",
        "text/javascript",
        "application/x-javascript",
        "text/csv",
        "text/markdown",
        "text/xml",
        "application/xml",
        "application/json",
        "text/x-python",
        "application/x-python-code"
    ):
        return True
    return False

def _sanitize_mime_type(file_path: str | None, mime: str | None, default_fallback: str = "application/octet-stream") -> str:
    m = (mime or "").strip().lower()
    if not m or m in ("application/octet-stream", "binary/oct-stream", "application/x-binary", "octet/stream"):
        if file_path:
             guessed, _ = mimetypes.guess_type(file_path)
             if guessed:
                 return guessed.lower()
        if file_path:
            ext = Path(file_path).suffix.lower()
            ext_map = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".m4v": "video/x-m4v",
                ".avi": "video/x-msvideo",
                ".mp3": "audio/mpeg",
                ".ogg": "audio/ogg",
                ".oga": "audio/ogg",
                ".opus": "audio/ogg",
                ".m4a": "audio/mp4",
                ".wav": "audio/wav",
                ".pdf": "application/pdf",
                ".txt": "text/plain",
                ".csv": "text/csv",
                ".json": "application/json",
                ".html": "text/html",
                ".htm": "text/html",
                ".xml": "text/xml"
            }
            if ext in ext_map:
                return ext_map[ext]
        return default_fallback
    if "voice" in m or m == "audio/ogg":
        return "audio/ogg"
    if m == "video/quicktime":
        return "video/quicktime"
    return m

def _media_file_id_and_mime(source: Any) -> tuple[str, str, str]:
    file_id, mime, filename = "", "", ""
    if source is None:
        return file_id, mime, filename
    for key in ("file_id", "id"):
        v = getattr(source, key, None) if not isinstance(source, dict) else source.get(key)
        if isinstance(v, str) and v.strip():
            file_id = v.strip()
            break
    mt = getattr(source, "mime_type", None) if not isinstance(source, dict) else source.get("mime_type")
    if isinstance(mt, str):
         mime = mt.strip()
    nm = getattr(source, "file_name", None) if not isinstance(source, dict) else source.get("file_name")
    if isinstance(nm, str):
         filename = nm.strip()

    class_name = type(source).__name__
    if not mime:
        if class_name == "PhotoSize":
            mime = "image/jpeg"
        elif class_name == "Sticker":
            # Синтетическое значение для ВСЕХ стикеров (статичных webp, анимированных
            # TGS/Lottie и видео-webm) — не настоящий Content-Type, а маркер категории
            # "это стикер" для _mime_matches_media_category в _resolve_incoming_media.
            # Различать три реальных формата здесь не нужно: ни один них downstream-код
            # не декодирует как картинку напрямую (см. приоритеты медиа в
            # _resolve_incoming_media — стикеры участвуют только в поиске по категории,
            # не в реальной отправке байтов в Gemini/OpenRouter как "image/webp").
            mime = "image/webp"
        elif class_name == "Voice":
            mime = "audio/ogg"
        elif class_name == "VideoNote":
            mime = "video/mp4"
        elif class_name == "Animation":
            mime = "video/mp4"

    return file_id, mime, filename

def _mime_suffix(mime: str, filename: str = "") -> str:
    if filename:
        suffix = Path(filename).suffix
        if suffix:
             return suffix
    m = (mime or "").lower()
    if m.startswith("image/"):
        sub = m.split("/", 1)[1]
        return {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(sub, f".{sub}")
    if m.startswith("audio/"):
        return ".mp3"
    if m.startswith("video/"):
        return ".mp4"
    return ".bin"

def _msg_media_source(message: Any) -> Any | None:
    for attr in ("photo", "video", "animation", "video_note", "voice", "audio", "document", "sticker"):
        val = getattr(message, attr, None)
        if not val:
            continue
        if attr == "photo" and isinstance(val, list):
            return val[-1] if val else None
        return val
    return None

def _ensure_prompt_text(text: str | None, mime: str) -> str:
    s = (text or "").strip()
    if s:
        return s
    m = mime.lower()
    if m.startswith("image/"):
         return "Подробно опиши, что изображено на картинке."
    if m.startswith("video/") or m == "video/quicktime":
         return "Подробно опиши происходящее на этом видео."
    if m.startswith("audio/") or m == "audio/ogg" or m == "audio/mpeg" or m == "audio/mp3" or "voice" in m:
         return "Прослушай и подробно опиши, что на этой аудиозаписи, или кратко перескажи ее содержание."
    if m == "image/gif" or "animation" in m:
         return "Подробно опиши происходящее на этой анимации."
    return "Проанализируй и подробно опиши содержимое этого вложения."
