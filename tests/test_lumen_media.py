"""test_lumen_media.py contains unit tests for the pure utilities in
lumen_media.py (MIME types, file suffixes). The module does not depend on the
bot runtime and is tested directly (import lumen_media), without importing
bot.py.
"""
import mimetypes

import lumen_media


def test_mime_suffix_rejects_hostile_subtype():
    # Audit A6-5: an unknown subtype went into the tmp filename as is.
    assert lumen_media._mime_suffix("image/../../x") == ".bin"
    assert lumen_media._mime_suffix("audio/x-unknown") == ".bin"
    assert lumen_media._mime_suffix("image/png") == ".png"
    assert lumen_media._mime_suffix("audio/mpeg") == ".mp3"


def test_sanitize_mime_uses_builtin_map_without_system_guess(monkeypatch):
    # Audit A6-10: ext_map did not know .md/.py/.js/.css — on systems without them in
    # in the system table, files were sent as octet-stream.
    monkeypatch.setattr(mimetypes, "guess_type", lambda *args, **kwargs: (None, None))
    assert lumen_media._sanitize_mime_type("notes.md", None) == "text/markdown"
    assert lumen_media._sanitize_mime_type("x.py", None) == "text/x-python"
    assert lumen_media._sanitize_mime_type("x.js", None) == "text/javascript"
    assert lumen_media._sanitize_mime_type("x.css", None) == "text/css"
