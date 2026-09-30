"""
test_lumen_media.py — юнит-тесты на чистые утилиты lumen_media.py (mime-типы,
суффиксы файлов). Модуль без зависимости от рантайма бота, тестируется
напрямую (import lumen_media), без импорта bot.py.
"""
import mimetypes

import lumen_media


def test_mime_suffix_rejects_hostile_subtype():
    # Аудит A6-5: неизвестный subtype уходил в имя tmp-файла как есть.
    assert lumen_media._mime_suffix("image/../../x") == ".bin"
    assert lumen_media._mime_suffix("audio/x-unknown") == ".bin"
    assert lumen_media._mime_suffix("image/png") == ".png"
    assert lumen_media._mime_suffix("audio/mpeg") == ".mp3"


def test_sanitize_mime_uses_builtin_map_without_system_guess(monkeypatch):
    # Аудит A6-10: ext_map не знал .md/.py/.js/.css — на системах без них в
    # системной таблице файлы уходили в octet-stream.
    monkeypatch.setattr(mimetypes, "guess_type", lambda *args, **kwargs: (None, None))
    assert lumen_media._sanitize_mime_type("notes.md", None) == "text/markdown"
    assert lumen_media._sanitize_mime_type("x.py", None) == "text/x-python"
    assert lumen_media._sanitize_mime_type("x.js", None) == "text/javascript"
    assert lumen_media._sanitize_mime_type("x.css", None) == "text/css"
