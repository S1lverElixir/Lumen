"""
test_bot_state.py — Состояние и утилиты: хранилище, квоты, rate limit, медиа-память, lang, логи.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
import asyncio
import bot
import json
import logging
import lumen_chat_state
import lumen_limits
import pathlib
import sentry_sdk
import sys
import time
from tests.bot_test_helpers import (
    _FakeIncomingMessage,
    _FakeOwnerBot,
    _run_core_capturing_prompt,
)


def test_split_text_chunks_short_text_returns_single_chunk():
    assert bot._split_text_chunks("короткий текст", 100) == ["короткий текст"]


def test_split_text_chunks_respects_max_len():
    # Регрессионный тест на реальный баг: раньше сообщения длиннее лимита
    # Telegram (4096 симв.) просто не отправлялись вообще.
    text = "слово " * 200
    chunks = bot._split_text_chunks(text, 50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 50


def test_split_text_chunks_preserves_all_words():
    # Каждый чанк уходит отдельным сообщением в Telegram, поэтому граничный
    # пробел-разделитель намеренно обрезается с обеих сторон (rstrip/lstrip) —
    # это не баг. Проверяем через join(" "), а не через голую конкатенацию.
    text = "слово " * 200
    chunks = bot._split_text_chunks(text, 50)
    assert " ".join(chunks).split() == text.split()


def test_split_text_chunks_lives_in_formatting_module():
    # Срез монолита (сентябрь 2026): реализация — в lumen_formatting, bot.py
    # только ре-экспортирует имя, чтобы bot._split_text_chunks работал как раньше.
    import lumen_formatting
    assert bot._split_text_chunks is lumen_formatting._split_text_chunks


def test_sanitize_mime_type_guesses_from_extension():
    assert bot._sanitize_mime_type("photo.jpg", "") == "image/jpeg"


def test_sanitize_mime_type_lowercases_valid_mime():
    assert bot._sanitize_mime_type(None, "image/PNG") == "image/png"


def test_sanitize_mime_type_octet_stream_falls_back_to_extension():
    assert bot._sanitize_mime_type("file.pdf", "application/octet-stream") == "application/pdf"


def test_sanitize_mime_type_no_info_returns_default_fallback():
    assert bot._sanitize_mime_type(None, None) == "application/octet-stream"


def test_sanitize_mime_type_audio_ogg_passthrough():
    assert bot._sanitize_mime_type(None, "audio/ogg") == "audio/ogg"


def test_sanitize_mime_type_maps_containers_to_supported_mimes():
    # .m4v/.avi раньше давали video/x-m4v/x-msvideo, которые
    # _is_gemini_supported_mime тут же отвергал. На Linux mimetypes
    # угадывает x-msvideo сам (на Windows — нет), поэтому нормализация
    # проверяется и детерминированно, без оглядки на платформу.
    assert bot._sanitize_mime_type("x.m4v", "application/octet-stream") == "video/mp4"
    assert bot._sanitize_mime_type("x.avi", "application/octet-stream") == "video/avi"
    assert bot._sanitize_mime_type(None, "video/x-msvideo") == "video/avi"
    assert bot._sanitize_mime_type(None, "video/x-m4v") == "video/mp4"


def test_mime_suffix_maps_audio_video_subtypes_honestly():
    # Регрессия AUD-E-005: было ".mp3 для любого audio" — расширение врало.
    assert bot._mime_suffix("audio/ogg", "") == ".ogg"
    assert bot._mime_suffix("audio/mpeg", "") == ".mp3"
    assert bot._mime_suffix("audio/wav", "") == ".wav"
    assert bot._mime_suffix("video/quicktime", "") == ".mov"
    assert bot._mime_suffix("video/mp4", "") == ".mp4"
    assert bot._mime_suffix("image/jpeg", "") == ".jpg"


def test_ensure_prompt_text_gif_gets_animation_prompt_not_generic_image():
    assert "анимации" in bot._ensure_prompt_text(None, "image/gif")
    assert "анимации" in bot._ensure_prompt_text("  ", "image/GIF")
    assert "картинке" in bot._ensure_prompt_text(None, "image/jpeg")


def test_is_tiktok_true_for_tiktok_url():
    assert bot.is_tiktok("https://www.tiktok.com/@user/video/123") is True


def test_is_tiktok_false_for_other_url():
    assert bot.is_tiktok("https://youtube.com/watch?v=1") is False


def test_is_youtube_true_for_short_link():
    assert bot.is_youtube("https://youtu.be/abc123") is True


def test_is_youtube_false_for_other_url():
    assert bot.is_youtube("https://example.com") is False


def test_extract_url_strips_trailing_punctuation():
    assert bot.extract_url("check this out: https://example.com/page.") == "https://example.com/page"


def test_extract_url_returns_none_when_no_url():
    assert bot.extract_url("no url here") is None


def test_clean_mention_removes_username():
    assert bot.clean_mention(f"@{bot.BOT_USERNAME} привет") == "привет"


def test_clean_mention_is_case_insensitive():
    result = bot.clean_mention(f"привет @{bot.BOT_USERNAME.upper()} как дела")
    assert bot.BOT_USERNAME.lower() not in result.lower()


def test_cleanup_rate_limit_dict_removes_empty_and_stale_entries():
    bot.user_rate_limits.clear()
    bot.user_rate_limits[111] = []  # раньше оставался бы в словаре навсегда
    bot.user_rate_limits[222] = [time.time() - 7200]  # старше часа — тоже чистится
    bot.user_rate_limits[333] = [time.time()]  # свежая запись — должна остаться
    bot._cleanup_rate_limit_dict()
    assert 111 not in bot.user_rate_limits
    assert 222 not in bot.user_rate_limits
    assert 333 in bot.user_rate_limits
    bot.user_rate_limits.clear()


def test_rate_limit_dict_evicts_stale_keys_when_over_capacity():
    # Регрессия AUD-G-001: при переполнении новый ключ чистит протухшие.
    original_max = lumen_limits.MAX_RATE_LIMIT_KEYS
    lumen_limits.MAX_RATE_LIMIT_KEYS = 2
    bot.user_rate_limits.clear()
    try:
        bot.user_rate_limits[1] = [time.time() - 7200]
        bot.user_rate_limits[2] = [time.time() - 7200]
        assert bot._check_and_register_rate_limit(3) is False
        assert 1 not in bot.user_rate_limits
        assert 2 not in bot.user_rate_limits
        assert 3 in bot.user_rate_limits
    finally:
        lumen_limits.MAX_RATE_LIMIT_KEYS = original_max
        bot.user_rate_limits.clear()


def test_evict_orphan_chat_locks_keeps_live_and_held_locks():
    # Регрессия AUD-G-001: бесхозные локи сносятся, живые и занятые остаются.
    # Сначала чистим чужие сироты (другие тесты создают локи без chat_state) —
    # иначе результат зависит от порядка прогона файлов.
    bot._evict_orphan_chat_locks()
    orphan_cid, live_cid, held_cid = 900001, 900002, 900003
    for cid in (orphan_cid, live_cid, held_cid):
        bot._chat_locks.pop(cid, None)
    bot.chat_state.pop(live_cid, None)
    bot.chat_state[live_cid] = {"history": [], "last_activity": time.time()}
    bot.get_chat_lock(orphan_cid)
    bot.get_chat_lock(live_cid)
    held_lock = bot.get_chat_lock(held_cid)

    async def _hold_and_evict():
        async with held_lock:
            return bot._evict_orphan_chat_locks()

    try:
        assert asyncio.run(_hold_and_evict()) == 1
        assert orphan_cid not in bot._chat_locks
        assert live_cid in bot._chat_locks
        assert held_cid in bot._chat_locks
    finally:
        bot._chat_locks.pop(orphan_cid, None)
        bot._chat_locks.pop(held_cid, None)
        bot._chat_locks.pop(live_cid, None)
        bot.chat_state.pop(live_cid, None)


def test_enforce_pending_picks_cap_evicts_closest_to_expiry():
    # Регрессия AUD-G-001: сверх лимита уходят самые близкие к протуханию.
    original_max = lumen_limits.MAX_PENDING_PICKS
    original_picks = dict(bot._pending_picks)
    lumen_limits.MAX_PENDING_PICKS = 3
    bot._pending_picks.clear()
    try:
        now = time.monotonic()
        for i in range(5):
            bot._pending_picks[f"t{i}"] = {"expires": now + i}
        bot._enforce_pending_picks_cap()
        assert set(bot._pending_picks) == {"t2", "t3", "t4"}
    finally:
        lumen_limits.MAX_PENDING_PICKS = original_max
        bot._pending_picks.clear()
        bot._pending_picks.update(original_picks)


def test_storage_write_and_read_local_file_roundtrip(tmp_path):
    # USE_UPSTASH=False (по умолчанию в тестах) — должен использоваться локальный файл
    assert bot.USE_UPSTASH is False
    target = tmp_path / "state.json"
    bot._storage_write_text("lumen:test", target, '{"a": 1}')
    assert target.exists()
    assert bot._storage_read_text("lumen:test", target) == '{"a": 1}'


def test_storage_read_text_missing_local_file_returns_none(tmp_path):
    assert bot.USE_UPSTASH is False
    missing = tmp_path / "does_not_exist.json"
    assert bot._storage_read_text("lumen:test", missing) is None


def test_upstash_set_sends_correct_request_and_auth_header():
    # Реального аккаунта Upstash нет — мокаем urlopen, чтобы проверить, что МОЙ код
    # строит правильный запрос (URL, метод, заголовок авторизации), а не реальный ответ сервиса.
    bot.UPSTASH_REDIS_REST_URL = "https://fake-instance.upstash.io"
    bot.UPSTASH_REDIS_REST_TOKEN = "fake-token"
    try:
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"result":"OK"}'
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        with patch("bot._urllib_request.urlopen", return_value=fake_resp) as mock_urlopen:
            bot._upstash_set("lumen:test", '{"x": 1}')
            assert mock_urlopen.called
            sent_request = mock_urlopen.call_args[0][0]
            assert sent_request.full_url == "https://fake-instance.upstash.io/set/lumen%3Atest"
            assert sent_request.get_header("Authorization") == "Bearer fake-token"
            assert sent_request.get_method() == "POST"
    finally:
        bot.UPSTASH_REDIS_REST_URL = ""
        bot.UPSTASH_REDIS_REST_TOKEN = ""


def test_upstash_get_parses_result_field():
    bot.UPSTASH_REDIS_REST_URL = "https://fake-instance.upstash.io"
    bot.UPSTASH_REDIS_REST_TOKEN = "fake-token"
    try:
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"result": "{\\"x\\": 1}"}'
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        with patch("bot._urllib_request.urlopen", return_value=fake_resp):
            assert bot._upstash_get("lumen:test") == '{"x": 1}'
    finally:
        bot.UPSTASH_REDIS_REST_URL = ""
        bot.UPSTASH_REDIS_REST_TOKEN = ""


def test_save_chat_to_storage_returns_true_on_success(tmp_path):
    # Регрессия на найденный при код-ревью баг (см. test_flush_dirty_state_once_*
    # ниже): функция теперь ДОЛЖНА сигнализировать успех/неудачу вызывающему коду,
    # а не просто логировать исключение и возвращать None в обоих случаях.
    chat_id = 999601
    state = {"history": [{"role": "user", "content": "привет"}], "image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    original_chats_dir = lumen_chat_state._CHATS_DIR
    lumen_chat_state._CHATS_DIR = tmp_path
    try:
        assert bot._save_chat_to_storage(chat_id, state) is True
        assert (tmp_path / f"{chat_id}.json").exists()
    finally:
        lumen_chat_state._CHATS_DIR = original_chats_dir


def test_save_chat_to_storage_returns_false_on_failure():
    state = {"history": [], "image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    with patch("bot._storage_write_text", side_effect=RuntimeError("сбой хранилища")):
        assert bot._save_chat_to_storage(999602, state) is False


def test_delete_chat_storage_returns_true_on_success(tmp_path):
    chat_id = 999603
    original_chats_dir = lumen_chat_state._CHATS_DIR
    lumen_chat_state._CHATS_DIR = tmp_path
    try:
        (tmp_path / f"{chat_id}.json").write_text("{}")
        assert bot._delete_chat_storage(chat_id) is True
        assert not (tmp_path / f"{chat_id}.json").exists()
    finally:
        lumen_chat_state._CHATS_DIR = original_chats_dir


def test_delete_chat_storage_returns_false_on_failure():
    with patch("bot._storage_delete_text", side_effect=RuntimeError("сбой хранилища")):
        assert bot._delete_chat_storage(999604) is False


def test_flush_dirty_state_once_requeues_failed_saves():
    # КРИТИЧНАЯ РЕГРЕССИЯ, найденная при код-ревью: раньше _dirty_chat_ids
    # очищался ДО того, как запись реально прошла, а неудачный _save_chat_to_storage
    # просто логировал исключение и возвращал None — чат "терялся" из очереди
    # навсегда при транзиентном сбое хранилища (например, кратковременный сбой
    # Upstash), пока какая-то ДРУГАЯ мутация того же чата не пометит его "грязным"
    # заново. Теперь чат, для которого сохранение не удалось, должен остаться в
    # _dirty_chat_ids и попасть в следующий цикл.
    ok_chat, fail_chat = 999701, 999702
    bot.chat_state[ok_chat] = {"history": [{"role": "user", "content": "ok"}], "image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    bot.chat_state[fail_chat] = {"history": [{"role": "user", "content": "fail"}], "image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    bot._dirty_chat_ids.clear()
    bot._dirty_chat_ids.update({ok_chat, fail_chat})
    # Флаги живут в lumen_chat_state (P2) — ребиндим там же, где их читает
    # _flush_dirty_state_once, иначе тест и код увидят разные значения.
    lumen_chat_state._index_dirty = False
    lumen_chat_state._quota_dirty = False

    def fake_save(cid, payload):
        return cid != fail_chat  # успех для ok_chat, неудача для fail_chat

    with patch("bot._save_chat_payload", side_effect=fake_save), \
         patch("bot._save_chat_index_payload"), patch("bot._save_quota_payload"):
        try:
            asyncio.run(bot._flush_dirty_state_once())
            # Успешно сохранённый чат должен быть убран из очереди...
            assert ok_chat not in bot._dirty_chat_ids
            # ...а неудавшийся — остаться для повтора на следующем цикле.
            assert fail_chat in bot._dirty_chat_ids
        finally:
            bot.chat_state.pop(ok_chat, None)
            bot.chat_state.pop(fail_chat, None)
            bot._dirty_chat_ids.discard(ok_chat)
            bot._dirty_chat_ids.discard(fail_chat)


def test_flush_dirty_state_once_requeues_failed_deletes():
    ok_chat, fail_chat = 999703, 999704
    bot._pending_chat_deletions.clear()
    bot._pending_chat_deletions.update({ok_chat, fail_chat})
    bot._dirty_chat_ids.clear()
    lumen_chat_state._index_dirty = False
    lumen_chat_state._quota_dirty = False

    def fake_delete(cid):
        return cid != fail_chat

    with patch("bot._delete_chat_storage", side_effect=fake_delete), \
         patch("bot._save_chat_index_payload"), patch("bot._save_quota_payload"):
        try:
            asyncio.run(bot._flush_dirty_state_once())
            assert ok_chat not in bot._pending_chat_deletions
            assert fail_chat in bot._pending_chat_deletions
        finally:
            bot._pending_chat_deletions.discard(ok_chat)
            bot._pending_chat_deletions.discard(fail_chat)


def test_save_chat_to_storage_limited_snapshots_before_thread():
    # Гонка сериализации: JSON-снапшот строится в loop (без await гонки нет),
    # в поток едет уже готовая строка — живой словарь туда не передаётся.
    import json
    captured = {}

    def fake_write(cid, payload):
        captured["payload"] = payload
        return True

    with patch("bot._serialize_chat_state", return_value={"marker": 1}), \
         patch("bot._save_chat_payload", side_effect=fake_write):
        ok = asyncio.run(bot._save_chat_to_storage_limited(999705, {"history": []}))
        assert ok is True
        assert json.loads(captured["payload"]) == {"marker": 1}


def test_sentry_scrub_secrets_redacts_known_tokens():
    original_bot_token, original_gemini_key, original_or_key = bot.BOT_TOKEN, bot.GEMINI_API_KEY, bot.OPENROUTER_API_KEY
    bot.BOT_TOKEN = "secret-bot-token-123"
    bot.GEMINI_API_KEY = "secret-gemini-key-456"
    bot.OPENROUTER_API_KEY = "secret-or-key-789"
    try:
        event = {
            "message": "failed calling token secret-bot-token-123",
            "extra": {"note": "gemini key was secret-gemini-key-456, or key was secret-or-key-789"},
        }
        scrubbed = bot._sentry_scrub_secrets(event, {})
        payload = json.dumps(scrubbed)
        assert "secret-bot-token-123" not in payload
        assert "secret-gemini-key-456" not in payload
        assert "secret-or-key-789" not in payload
        assert payload.count("<REDACTED>") == 3
    finally:
        bot.BOT_TOKEN, bot.GEMINI_API_KEY, bot.OPENROUTER_API_KEY = original_bot_token, original_gemini_key, original_or_key


def test_sentry_scrub_secrets_redacts_webhook_admin_and_upstash_tokens():
    # РЕГРЕССИЯ: раньше _redactable_secrets (тогда — инлайн-кортеж) вычищал
    # только BOT_TOKEN/GEMINI_API_KEY/OPENROUTER_API_KEY — WEBHOOK_SECRET/
    # ADMIN_PANEL_KEY/UPSTASH_REDIS_REST_TOKEN утекли бы в Sentry как есть.
    original_upstash = bot.UPSTASH_REDIS_REST_TOKEN
    bot.UPSTASH_REDIS_REST_TOKEN = "secret-upstash-token-xyz"
    try:
        event = {"message": f"leak {bot.WEBHOOK_SECRET} {bot.ADMIN_PANEL_KEY} secret-upstash-token-xyz"}
        payload = json.dumps(bot._sentry_scrub_secrets(event, {}))
        assert bot.WEBHOOK_SECRET not in payload
        assert bot.ADMIN_PANEL_KEY not in payload
        assert "secret-upstash-token-xyz" not in payload
    finally:
        bot.UPSTASH_REDIS_REST_TOKEN = original_upstash


def test_sentry_scrub_secrets_passthrough_when_no_secrets_configured():
    original_bot_token, original_gemini_key, original_or_key = bot.BOT_TOKEN, bot.GEMINI_API_KEY, bot.OPENROUTER_API_KEY
    bot.BOT_TOKEN = bot.GEMINI_API_KEY = bot.OPENROUTER_API_KEY = ""
    try:
        event = {"message": "ordinary error, nothing secret here"}
        assert bot._sentry_scrub_secrets(event, {}) == event
    finally:
        bot.BOT_TOKEN, bot.GEMINI_API_KEY, bot.OPENROUTER_API_KEY = original_bot_token, original_gemini_key, original_or_key


def test_sentry_not_initialized_without_dsn_in_test_env():
    # SENTRY_DSN отсутствует в тестовом окружении (conftest.py не подставляет его,
    # в отличие от BOT_TOKEN/GEMINI_API_KEY) — sentry_sdk.init() не должен был
    # вызваться при импорте bot.py, значит нет активного Sentry-клиента.
    assert bot.SENTRY_DSN == ""
    assert sentry_sdk.is_initialized() is False


def test_setup_logging_respects_log_level_env(monkeypatch):
    # РЕГРЕССИЯ: раньше уровень был захардкожен INFO везде (root + оба handler'а) —
    # log.debug(...) не печатался ни при каком окружении. LOG_LEVEL теперь читается
    # так же, как и любой другой тюнинг в проекте.
    import logging as _logging
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    try:
        bot._setup_logging()
        assert _logging.getLogger().level == _logging.DEBUG
    finally:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        bot._setup_logging()  # восстановить дефолт INFO для остальных тестов


def test_setup_logging_defaults_to_info_when_unset(monkeypatch):
    import logging as _logging
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    bot._setup_logging()
    assert _logging.getLogger().level == _logging.INFO


def test_setup_logging_uses_bot_logger_name_not_dunder_main():
    # РЕГРЕССИЯ (аудит логирования): _setup_logging() раньше возвращал
    # logging.getLogger(__name__) — "bot" при import bot (как в тестах), но
    # "__main__" в реальном проде (python -u bot.py, см. Dockerfile CMD),
    # рассинхрон с lumen_*.py, где везде явно logging.getLogger("bot").
    # Подтверждено реальным событием в Sentry с тегом logger=__main__. Тест не
    # видит __name__ != "bot" (в тестах он и так "bot"), поэтому проверяет сам
    # факт, что _setup_logging() возвращает логгер по явному имени "bot", а не
    # по __name__ — тогда расхождение в проде физически невозможно.
    result_logger = bot._setup_logging()
    assert result_logger.name == "bot"
    assert bot.log.name == "bot"


def test_process_media_group_buffers_records_extra_photos_to_recent_media():
    chat_id = 999960
    user = SimpleNamespace(id=777)

    def _photo_msg(file_id):
        photo = SimpleNamespace(file_id=file_id, mime_type=None, file_name=None)
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE), from_user=user,
            media_group_id="mg1", text=None, caption=None, reply_to_message=None,
            photo=[photo], video=None, animation=None, video_note=None,
            voice=None, audio=None, document=None, sticker=None,
        )

    msg_main, msg_extra = _photo_msg("file_A"), _photo_msg("file_B")

    async def fake_fetch_media(file_id, mime):
        return (b"bytes", "image/jpeg")

    async def fake_handle_core(message, extra_media=None):
        pass  # основное фото (index 0) сохраняет _resolve_incoming_media — не в фокусе этого теста

    original_fetch, original_core = bot._fetch_media, bot._handle_message_core
    bot._fetch_media = fake_fetch_media
    bot._handle_message_core = fake_handle_core
    bot._mg_buffers["mg1"] = [msg_main, msg_extra]
    try:
        asyncio.run(bot._process_media_group_buffers("mg1"))
        recent = bot.chat_state[chat_id]["recent_media_ids"].get(str(user.id), [])
        file_ids = [fid for fid, _ in recent]
        assert "file_B" in file_ids
    finally:
        bot._fetch_media = original_fetch
        bot._handle_message_core = original_core
        bot.chat_state.pop(chat_id, None)


def test_process_media_group_buffers_fetches_extras_in_parallel_keeping_order():
    # Альбом качается параллельно: медленное первое фото не должно задерживать остальные,
    # а порядок вложений обязан совпасть с порядком сообщений.
    chat_id = 999961
    user = SimpleNamespace(id=778)

    def _photo_msg(file_id):
        photo = SimpleNamespace(file_id=file_id, mime_type=None, file_name=None)
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE), from_user=user,
            media_group_id="mg2", text=None, caption=None, reply_to_message=None,
            photo=[photo], video=None, animation=None, video_note=None,
            voice=None, audio=None, document=None, sticker=None,
        )

    msgs = [_photo_msg(f"file_{i}") for i in range(5)]
    captured = {}
    concurrent = 0
    max_concurrent = 0

    async def fake_fetch_media(file_id, mime):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        try:
            await asyncio.sleep(0.01)
            return (file_id.encode(), "image/jpeg")
        finally:
            concurrent -= 1

    async def fake_handle_core(message, extra_media=None):
        captured["extra"] = extra_media

    original_fetch, original_core = bot._fetch_media, bot._handle_message_core
    bot._fetch_media = fake_fetch_media
    bot._handle_message_core = fake_handle_core
    bot._mg_buffers["mg2"] = msgs
    try:
        asyncio.run(bot._process_media_group_buffers("mg2"))
        # Все 4 доп. фото качались одновременно, а не по очереди.
        assert max_concurrent == 4
        assert [b for b, _ in captured["extra"]] == [b"file_1", b"file_2", b"file_3", b"file_4"]
    finally:
        bot._fetch_media = original_fetch
        bot._handle_message_core = original_core
        bot.chat_state.pop(chat_id, None)


def test_media_question_without_file_gets_no_file_notice(rate_guard_setup, monkeypatch):
    # Прод-кейс 17.09.2026: "что на фото?" без файла — модель выдумала описание
    # несуществующего скриншота. Раз резолвинг ничего не нашёл, этот факт едет
    # модели явно, а не надеждой на один раздел промпта.
    message = rate_guard_setup()
    message.text = "что на фото?"
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert prompt.startswith("что на фото?")
    assert "[Служебная пометка" in prompt
    assert "не выдумывай" in prompt.lower()


def test_voice_message_transcribed_into_normal_routing(rate_guard_setup, monkeypatch):
    # Войс: транскрибация уходит в общий роутинг как текст, аудио дальше не едет.
    message = rate_guard_setup()
    message.text = ""
    captured = {}

    async def fake_resolve(message, state, clean_prompt, *, is_private):
        return None, "", "", (b"ogg-bytes", "audio/ogg")

    async def fake_transcribe(audio_bytes, mime, chat_id):
        assert audio_bytes == b"ogg-bytes"
        assert mime == "audio/ogg"
        return "текст из войса"

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        captured["media"] = kwargs.get("media")
        return "ok", False

    monkeypatch.setattr(bot, "_resolve_incoming_media", fake_resolve)
    monkeypatch.setattr(bot, "_transcribe_audio", fake_transcribe)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    monkeypatch.setattr(bot, "_safe_reply", AsyncMock())
    asyncio.run(bot._handle_message_core(message))
    assert "текст из войса" in captured["prompt"]
    assert captured["media"] is None
    bot.chat_state.pop(123, None)


def test_voice_message_falls_back_to_gemini_audio_on_transcribe_failure(rate_guard_setup, monkeypatch):
    # Whisper упал — войс идёт прежним путём (аудио в Gemini), а не в пустоту.
    message = rate_guard_setup()
    message.text = ""
    captured = {}

    async def fake_resolve(message, state, clean_prompt, *, is_private):
        return None, "", "", (b"ogg-bytes", "audio/ogg")

    async def fake_transcribe(audio_bytes, mime, chat_id):
        return None

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["media"] = kwargs.get("media")
        return "ok", False

    monkeypatch.setattr(bot, "_resolve_incoming_media", fake_resolve)
    monkeypatch.setattr(bot, "_transcribe_audio", fake_transcribe)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    monkeypatch.setattr(bot, "_safe_reply", AsyncMock())
    asyncio.run(bot._handle_message_core(message))
    assert captured["media"] == [(b"ogg-bytes", "audio/ogg")]
    bot.chat_state.pop(123, None)


def test_unfetchable_attachment_gets_honest_error_not_silence(rate_guard_setup, monkeypatch):
    # Вложение есть, а скачать не вышло — честная ошибка вместо "Слушаю" в пустоту (прод 23.09.2026).
    message = rate_guard_setup()
    message.text = ""
    message.photo = [SimpleNamespace(file_id="dead", mime_type=None, file_name=None)]
    replied = []

    async def fake_resolve(message, state, clean_prompt, *, is_private):
        return None, "", "", None

    async def fake_safe_reply(message, text, **kwargs):
        replied.append(text)

    monkeypatch.setattr(bot, "_resolve_incoming_media", fake_resolve)
    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    asyncio.run(bot._handle_message_core(message))
    assert len(replied) == 1
    assert "Слушаю" not in replied[0]
    bot.chat_state.pop(123, None)


def test_sticker_without_text_keeps_old_listening_behavior(rate_guard_setup, monkeypatch):
    # Стикер — не ошибка скачивания: прежнее поведение без изменений.
    message = rate_guard_setup()
    message.text = ""
    Sticker = type("Sticker", (), {})
    message.sticker = Sticker()
    called = []

    async def fake_resolve(message, state, clean_prompt, *, is_private):
        return None, "", "", None

    async def fake_tg_call(method, *args, **kwargs):
        called.append(args[0] if args else "")
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_resolve_incoming_media", fake_resolve)
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "message_mentions_bot", lambda message: True)
    asyncio.run(bot._handle_message_core(message))
    # Дефолтный язык чата — английский, сверяем с ключом дословно.
    assert called == [bot._t(123, "status_listening")]
    bot.chat_state.pop(123, None)


def test_ordinary_question_gets_no_file_notice(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "столица Венгрии?"
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert "[Служебная пометка" not in prompt


def test_message_core_sends_typing_indicator(rate_guard_setup, monkeypatch):
    # Регрессия: вызов шёл в bot.send_chat_action (такой функции нет) вместо bot.bot.send_chat_action — индикатор "печатает" молча не показывался.
    message = rate_guard_setup()
    message.text = "столица Венгрии?"
    fake_bot = SimpleNamespace(send_chat_action=AsyncMock())
    monkeypatch.setattr(bot, "bot", fake_bot)
    _run_core_capturing_prompt(message, monkeypatch)
    fake_bot.send_chat_action.assert_awaited_once_with(chat_id=123, action="typing")


def test_media_question_with_attached_file_gets_no_file_notice(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "что на фото?"

    async def fake_resolve(message, state, clean_prompt, *, is_private):
        return None, "", "", (b"fake-bytes", "image/jpeg")

    monkeypatch.setattr(bot, "_resolve_incoming_media", fake_resolve)
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert "[Служебная пометка" not in prompt


def test_history_user_text_strips_one_shot_service_note():
    # Пометка "файла нет" — на один ход модели, в историю пишется чистый текст,
    # иначе старые пометки про чужие сообщения запутают следующие ходы.
    assert bot._history_user_text("что на фото?" + bot._NO_MEDIA_NOTE) == "что на фото?"
    assert bot._history_user_text("обычный вопрос") == "обычный вопрос"


def _make_long_history(n=105):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"сообщение {i}"}
        for i in range(n)
    ]


def test_trim_history_short_is_untouched(monkeypatch):
    # Короткая история — ни саммари, ни сетевых вызовов вообще.
    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("no LLM call for short history")

    monkeypatch.setattr(bot, "_groq_request", must_not_be_called)
    monkeypatch.setattr(bot, "_or_request", must_not_be_called)
    history = _make_long_history(50)
    asyncio.run(bot._trim_history(history))
    assert len(history) == 50
    assert history[0]["content"] == "сообщение 0"


def test_trim_history_summarizes_old_keeps_recent(monkeypatch):
    # Переполнение: старое сжимается в первую запись с пометкой, свежие 80 — как были.
    async def fake_groq_request(path, method="GET", *, json_body=None):
        assert json_body["model"] == "qwen/qwen3.8-27b"
        return {"choices": [{"message": {"content": "Обсуждали погоду и котов."}}]}

    monkeypatch.setattr(bot, "_groq_request", fake_groq_request)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model: None)
    history = _make_long_history(105)
    asyncio.run(bot._trim_history(history))
    assert len(history) == 81
    assert history[0]["content"].startswith("[Ранее в диалоге]: ")
    assert "погоду" in history[0]["content"]
    assert history[1]["content"] == "сообщение 25"
    assert history[-1]["content"] == "сообщение 104"


def test_trim_history_falls_back_to_plain_cut_on_failure(monkeypatch):
    # Саммаризатор упал — режем по-старому, ответ не ломается.
    async def failing_request(*args, **kwargs):
        raise bot.GroqAPIError("overloaded", status_code=503)

    monkeypatch.setattr(bot, "_groq_request", failing_request)
    monkeypatch.setattr(bot, "_or_request", failing_request)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "fake-key")
    history = _make_long_history(105)
    asyncio.run(bot._trim_history(history))
    assert len(history) == 100
    assert history[0]["content"] == "сообщение 5"


def test_trim_history_without_keys_cuts_plainly(monkeypatch):
    # Нет ключей — даже не пробуем сеть, сразу старая обрезка.
    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("no LLM call without keys")

    monkeypatch.setattr(bot, "_groq_request", must_not_be_called)
    monkeypatch.setattr(bot, "_or_request", must_not_be_called)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "")
    monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
    history = _make_long_history(105)
    asyncio.run(bot._trim_history(history))
    assert len(history) == 100
    assert history[0]["content"] == "сообщение 5"


def test_looks_like_media_reference_true_for_explicit_media_nouns():
    assert bot._looks_like_media_reference("что на фото") is True
    assert bot._looks_like_media_reference("опиши это видео") is True
    assert bot._looks_like_media_reference("покажи стикер") is True
    assert bot._looks_like_media_reference("расскажи про тот гиф") is True


def test_looks_like_media_reference_no_false_positive_on_common_words():
    # Регрессия на реальный найденный баг: раньше ловились "это"/"тот"/"который"/
    # "раньше"/"покажи"/"опиши" без явного упоминания медиа — почти любое сообщение
    # заново подтягивало последнюю картинку пользователя и вызывало галлюцинации.
    assert bot._looks_like_media_reference("расскажи про эту компанию") is False
    assert bot._looks_like_media_reference("который час") is False
    assert bot._looks_like_media_reference("объясни это подробнее") is False
    assert bot._looks_like_media_reference("раньше было по-другому") is False
    assert bot._looks_like_media_reference("покажи пример кода") is False
    assert bot._looks_like_media_reference("до этого мы говорили про политику") is False


def test_media_reference_category_detects_each_type():
    assert bot._media_reference_category("покажи стикер") == "sticker"
    assert bot._media_reference_category("что на видео") == "video"
    assert bot._media_reference_category("расскажи про тот гиф") == "video"
    assert bot._media_reference_category("что было в голосовом") == "audio"
    assert bot._media_reference_category("что на фото") == "photo"
    assert bot._media_reference_category("опиши тот скриншот") == "photo"


def test_media_reference_category_none_when_no_media_word():
    assert bot._media_reference_category("расскажи про эту компанию") is None
    assert bot._media_reference_category("") is None


def test_media_reference_category_detects_documents_and_circles():
    # Категория document (сентябрь 2026): "что в документе/pdf" раньше вообще не
    # распознавалось — файл подтягивался только явным реплаем. "текст" сюда
    # намеренно НЕ входит (слишком общее слово — см. комментарий у регэкспа).
    assert bot._media_reference_category("что в этом документе") == "document"
    assert bot._media_reference_category("прочитай pdf") == "document"
    assert bot._media_reference_category("открой файл") == "document"
    assert bot._media_reference_category("глянь кружок") == "video"
    assert bot._media_reference_category("переведи этот текст") is None


def test_mime_matches_media_category_document():
    assert bot._mime_matches_media_category("application/pdf", "document") is True
    assert bot._mime_matches_media_category("text/plain", "document") is True
    assert bot._mime_matches_media_category("application/octet-stream", "document") is True
    assert bot._mime_matches_media_category("image/jpeg", "document") is False
    assert bot._mime_matches_media_category("application/pdf", "photo") is False


def test_mime_matches_media_category_sticker_is_exclusively_webp():
    assert bot._mime_matches_media_category("image/webp", "sticker") is True
    # Обычное фото Telegram всегда пережимает в JPEG — не webp, поэтому "image/webp"
    # надёжно отличает стикер от фото без обращения к самому объекту Sticker.
    assert bot._mime_matches_media_category("image/jpeg", "sticker") is False


def test_mime_matches_media_category_photo_excludes_webp():
    assert bot._mime_matches_media_category("image/jpeg", "photo") is True
    assert bot._mime_matches_media_category("image/png", "photo") is True
    assert bot._mime_matches_media_category("image/webp", "photo") is False


def test_mime_matches_media_category_video_and_audio_by_prefix():
    assert bot._mime_matches_media_category("video/mp4", "video") is True
    assert bot._mime_matches_media_category("audio/ogg", "audio") is True
    assert bot._mime_matches_media_category("video/mp4", "audio") is False


def test_find_recent_media_by_category_skips_type_mismatch():
    # Реальный сценарий бага: бакет содержит фото (новее) и стикер (старше) —
    # запрос "стикер" обязан найти именно стикер, а не просто последний элемент.
    bucket = [("sticker_id", "image/webp"), ("photo_id", "image/jpeg")]
    assert bot._find_recent_media_by_category(bucket, "sticker") == ("sticker_id", "image/webp")
    assert bot._find_recent_media_by_category(bucket, "photo") == ("photo_id", "image/jpeg")


def test_find_recent_media_by_category_none_when_no_type_match():
    bucket = [("photo_id", "image/jpeg")]
    assert bot._find_recent_media_by_category(bucket, "sticker") is None


def test_find_recent_media_by_category_none_for_empty_bucket():
    assert bot._find_recent_media_by_category([], "photo") is None
    assert bot._find_recent_media_by_category(None, "photo") is None


def test_reset_quota_if_new_day_clears_used_and_exhausted_on_day_rollover():
    # lumen_chat_state._last_quota_check_monotonic сбрасывается явно: в проде троттлинг (см.
    # _QUOTA_CHECK_THROTTLE_SEC) абсолютно безопасен, т.к. между реальными вызовами
    # проходят настоящие секунды — но в тестах десятки вызовов _quota_entry (через
    # ask_gemini/ask_openrouter_* в других тестах этого же файла) укладываются в
    # миллисекунды, и без сброса throttle-таймера этот тест непредсказуемо ловил бы
    # "ещё не прошла минута с прошлой проверки" и тихо становился no-op — именно
    # так и произошло при первом прогоне (нашли на code-review, тест падал только
    # в полном прогоне всего файла, а не в изоляции).
    #
    # РЕГРЕССИЯ (найдено при /engineering:debug): сброс в буквальный 0.0 неявно
    # предполагал, что time.monotonic() к моменту теста уже далеко за 60 секунд —
    # верно для процесса, который живёт часами, но не гарантировано для короткого
    # прогона тестов (~6 сек весь файл), запущенного вскоре после старта контейнера/
    # песочницы, где monotonic-часы сами могут ещё не дойти до 60. Тогда "0.0" уже
    # НЕ "далеко в прошлом" относительно "сейчас", и throttle съедает даже первый
    # вызов — детерминированно воспроизведено подменой time.monotonic() на 12.0.
    # Правильный сброс — не абсолютный ноль, а "текущий момент минус окно троттлинга
    # с запасом": так гарантированно "давно" независимо от того, сколько реально
    # прошло времени с момента запуска процесса.
    original_quota = {
        "gemini": dict(bot.GLOBAL_QUOTA.get("gemini", {})),
        "openrouter": dict(bot.GLOBAL_QUOTA.get("openrouter", {})),
        "quota_day": bot.GLOBAL_QUOTA.get("quota_day"),
    }
    original_throttle = lumen_chat_state._last_quota_check_monotonic
    try:
        bot.GLOBAL_QUOTA["gemini"] = {"gemini-2.5-flash": {"used": 106, "remaining": 0, "limit": 1500, "exhausted_at": 12345.0}}
        bot.GLOBAL_QUOTA["openrouter"] = {"some-model:free": {"used": 50, "remaining": None, "limit": None, "exhausted_at": None}}
        bot.GLOBAL_QUOTA["quota_day"] = "2020-01-01"  # заведомо "вчерашний" день
        lumen_chat_state._last_quota_check_monotonic = time.monotonic() - bot._QUOTA_CHECK_THROTTLE_SEC - 10.0

        bot._reset_quota_if_new_day()

        assert bot.GLOBAL_QUOTA["gemini"]["gemini-2.5-flash"]["used"] == 0
        assert bot.GLOBAL_QUOTA["gemini"]["gemini-2.5-flash"]["exhausted_at"] is None
        assert bot.GLOBAL_QUOTA["openrouter"]["some-model:free"]["used"] == 0
        assert bot.GLOBAL_QUOTA["quota_day"] == bot._current_quota_day()
    finally:
        bot.GLOBAL_QUOTA["gemini"] = original_quota["gemini"]
        bot.GLOBAL_QUOTA["openrouter"] = original_quota["openrouter"]
        bot.GLOBAL_QUOTA["quota_day"] = original_quota["quota_day"]
        lumen_chat_state._last_quota_check_monotonic = original_throttle


def test_reset_quota_if_new_day_is_noop_within_same_day():
    original_quota_day = bot.GLOBAL_QUOTA.get("quota_day")
    original_throttle = lumen_chat_state._last_quota_check_monotonic
    try:
        bot.GLOBAL_QUOTA["gemini"]["test-model-999"] = {"used": 7, "remaining": None, "limit": None, "exhausted_at": None}
        bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
        # См. комментарий в test_reset_quota_if_new_day_clears_used_and_exhausted_on_day_rollover
        # выше про то, почему буквальный 0.0 не годится как "точно давно".
        lumen_chat_state._last_quota_check_monotonic = time.monotonic() - bot._QUOTA_CHECK_THROTTLE_SEC - 10.0
        bot._reset_quota_if_new_day()
        assert bot.GLOBAL_QUOTA["gemini"]["test-model-999"]["used"] == 7
    finally:
        bot.GLOBAL_QUOTA["gemini"].pop("test-model-999", None)
        bot.GLOBAL_QUOTA["quota_day"] = original_quota_day
        lumen_chat_state._last_quota_check_monotonic = original_throttle


def test_reset_quota_if_new_day_throttles_repeated_calls():
    # Регрессия на найденное при code-review: без троттлинга _reset_quota_if_new_day
    # конструировала бы ZoneInfo/datetime.now на КАЖДЫЙ вызов _quota_entry (а таких —
    # по несколько на каждую попытку модели). Проверяем, что повторный вызов сразу
    # после первого не запускает вторую проверку даты — если бы троттлинг не
    # работал, _current_quota_day() был бы вызван трижды, а не один раз.
    #
    # См. комментарий в test_reset_quota_if_new_day_clears_used_and_exhausted_on_day_rollover
    # про то, почему сброс делается относительно time.monotonic(), а не в буквальный 0.0.
    original_throttle = lumen_chat_state._last_quota_check_monotonic
    calls = []
    original_fn = bot._current_quota_day
    try:
        lumen_chat_state._last_quota_check_monotonic = time.monotonic() - bot._QUOTA_CHECK_THROTTLE_SEC - 10.0
        bot._current_quota_day = lambda: (calls.append(1), original_fn())[1]
        bot._reset_quota_if_new_day()
        bot._reset_quota_if_new_day()
        bot._reset_quota_if_new_day()
        assert len(calls) == 1
    finally:
        bot._current_quota_day = original_fn
        lumen_chat_state._last_quota_check_monotonic = original_throttle


def test_serialize_chat_state_stamps_current_schema_version():
    state = {"image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "history": [], "quota": {}, "recent_media_ids": {}}
    snapshot = bot._serialize_chat_state(state)
    assert snapshot["schema_version"] == bot.CHAT_STATE_SCHEMA_VERSION


def test_restore_single_chat_accepts_legacy_record_without_schema_version():
    # Записи, сохранённые до введения schema_version, не имеют этого поля вообще —
    # восстановление не должно падать и должно вести себя так же, как раньше.
    cid = 999905
    try:
        bot._restore_single_chat(cid, {"image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "history": [{"role": "user", "content": "hi"}]})
        assert bot.chat_state[cid]["history"] == [{"role": "user", "content": "hi"}]
    finally:
        bot.chat_state.pop(cid, None)


def test_restore_single_chat_accepts_current_schema_version_record():
    cid = 999906
    try:
        snapshot = bot._serialize_chat_state({
            "image_model": bot.DEFAULT_POLLINATIONS_IMAGE_MODEL, "history": [{"role": "user", "content": "hi"}],
            "quota": {}, "recent_media_ids": {},
        })
        bot._restore_single_chat(cid, snapshot)
        assert bot.chat_state[cid]["history"] == [{"role": "user", "content": "hi"}]
    finally:
        bot.chat_state.pop(cid, None)


def test_restore_single_chat_keeps_language_across_restart():
    # Прод-баг: /lang слетал при каждом деплое — восстановление пересоздавало
    # состояние без поля lang, хотя сериализатор его писал.
    cid = 999907
    try:
        bot._restore_single_chat(cid, {"history": [], "lang": "uk"})
        assert bot.chat_state[cid]["lang"] == "uk"
        bot._restore_single_chat(cid, {"history": [], "lang": "xx-quebrada"})
        assert bot.chat_state[cid]["lang"] == "en"
        bot._restore_single_chat(cid, {"history": []})
        assert bot.chat_state[cid]["lang"] == "en"
    finally:
        bot.chat_state.pop(cid, None)


def test_check_and_register_rate_limit_blocks_after_five_requests():
    bot.user_rate_limits.pop(999950, None)
    try:
        for _ in range(5):
            assert bot._check_and_register_rate_limit(999950) is False
        assert bot._check_and_register_rate_limit(999950) is True
    finally:
        bot.user_rate_limits.pop(999950, None)


def test_check_and_register_rate_limit_does_not_extend_punishment():
    # Регрессия: после срабатывания лимита новый timestamp НЕ должен добавляться,
    # иначе пользователь, продолжающий писать, никогда не выйдет из-под лимита.
    bot.user_rate_limits.pop(999951, None)
    try:
        for _ in range(5):
            bot._check_and_register_rate_limit(999951)
        assert bot._check_and_register_rate_limit(999951) is True
        assert len(bot.user_rate_limits[999951]) == 5
    finally:
        bot.user_rate_limits.pop(999951, None)


def test_check_and_register_rate_limit_noop_for_missing_user_id():
    assert bot._check_and_register_rate_limit(None) is False
    assert bot._check_and_register_rate_limit(0) is False


def test_rate_limit_key_for_message_uses_from_user_when_present():
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=555), sender_chat=None,
        chat=SimpleNamespace(id=-100999),
    )
    assert bot._rate_limit_key_for_message(msg) == 555


def test_rate_limit_key_for_message_falls_back_to_sender_chat_without_from_user():
    # Сообщение "от имени канала" — from_user отсутствует, но есть sender_chat.
    msg = SimpleNamespace(
        from_user=None, sender_chat=SimpleNamespace(id=-100777),
        chat=SimpleNamespace(id=-100999),
    )
    assert bot._rate_limit_key_for_message(msg) == -100777


def test_rate_limit_key_for_message_falls_back_to_chat_id_as_last_resort():
    # Ни from_user, ни sender_chat — берём сам chat.id, лишь бы не None (регрессия
    # на сам факт обхода: раньше это давало полностью нелимитированного отправителя).
    msg = SimpleNamespace(from_user=None, sender_chat=None, chat=SimpleNamespace(id=-100999))
    assert bot._rate_limit_key_for_message(msg) == -100999


def test_rate_limit_key_for_message_never_falls_through_to_none():
    # Ни один разумный вход не должен давать falsy ключ — иначе
    # _check_and_register_rate_limit молча пропустит проверку (см. её докстринг).
    for msg in (
        SimpleNamespace(from_user=None, sender_chat=None, chat=SimpleNamespace(id=123)),
        SimpleNamespace(from_user=SimpleNamespace(id=1), sender_chat=None, chat=SimpleNamespace(id=123)),
    ):
        key = bot._rate_limit_key_for_message(msg)
        assert key is not None and key != 0


def test_should_only_record_passively_true_for_unmentioned_group_text():
    msg = _FakeIncomingMessage(1)
    assert bot._should_only_record_passively(msg, "привет всем", is_private=False, is_guest=False, mentioned=False) is True


def test_should_only_record_passively_false_when_mentioned_or_private_or_guest():
    msg = _FakeIncomingMessage(1)
    assert bot._should_only_record_passively(msg, "привет", is_private=True, is_guest=False, mentioned=False) is False
    assert bot._should_only_record_passively(msg, "привет", is_private=False, is_guest=True, mentioned=False) is False
    assert bot._should_only_record_passively(msg, "привет", is_private=False, is_guest=False, mentioned=True) is False


def test_should_only_record_passively_false_for_tiktok_link_without_mention():
    # TikTok-ссылка обрабатывается всегда, даже без упоминания бота в группе.
    msg = _FakeIncomingMessage(1)
    text = "гляньте https://www.tiktok.com/@user/video/123"
    assert bot._should_only_record_passively(msg, text, is_private=False, is_guest=False, mentioned=False) is False


def test_handle_message_core_known_route_outcome_logs_as_warning_not_exception(caplog):
    # РЕГРЕССИЯ (аудит логирования): GeminiAllModelsExhaustedError/
    # RouteBudgetExceededError — известные, уже обрабатываемые исходы (реальный
    # суточный лимит квоты / бюджет времени маршрута), для которых пользователь и
    # так получает понятный текст, а владелец (для квоты) отдельно уведомляется.
    # Раньше log.exception (ERROR) заводил issue в Sentry на КАЖДЫЙ такой случай —
    # см. LUMEN-3: 8 событий за месяц оказались этим классом. Теперь — log.warning.
    import logging
    chat_id = 555010
    incoming = _FakeIncomingMessage(chat_id)
    incoming.text = "привет"
    incoming.caption = None
    incoming.from_user = SimpleNamespace(id=chat_id, username="tester", language_code="ru")

    async def failing_run_route(*args, **kwargs):
        raise bot.GeminiAllModelsExhaustedError(["gemini-3.7-flash"])

    original_run_route = bot._run_route
    original_owner = bot.OWNER_ID
    bot._run_route = failing_run_route
    bot.OWNER_ID = None  # без owner-алерта, не в фокусе этого теста
    bot.user_rate_limits.pop(chat_id, None)
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot._handle_message_core(incoming))
        levels = [r.levelname for r in caplog.records if "Chat AI processing" in r.getMessage()]
        assert levels == ["WARNING"]
    finally:
        bot._run_route = original_run_route
        bot.OWNER_ID = original_owner
        bot.user_rate_limits.pop(chat_id, None)
        bot.chat_state.pop(chat_id, None)


def test_resolve_incoming_media_priority_2_reply_attachment_wins_over_priority_3_recent_media():
    # Явный реплай на медиа (приоритет 2) должен побеждать словесную отсылку к
    # недавнему медиа (приоритет 3), даже если оба технически применимы.
    chat_id = 999952
    state = bot.get_state(chat_id)
    state["recent_media_ids"] = {"555": [("old_file_id", "image/jpeg")]}
    try:
        msg = _FakeIncomingMessage(chat_id)
        msg.from_user = SimpleNamespace(id=555)
        reply_photo = SimpleNamespace(file_id="reply_file_id", mime_type="image/png", file_name="")
        msg.reply_to_message = SimpleNamespace(photo=[reply_photo], video=None, animation=None, video_note=None, voice=None, audio=None, document=None, sticker=None, text=None, caption=None)

        async def fake_fetch_media(file_id, mime):
            return (b"bytes-for-" + file_id.encode(), mime)

        original_fetch = bot._fetch_media
        bot._fetch_media = fake_fetch_media
        try:
            med_path, med_mime, med_name, media_tuple = asyncio.run(
                bot._resolve_incoming_media(msg, state, "что на фото", is_private=True)
            )
            assert media_tuple == (b"bytes-for-reply_file_id", "image/png")
            assert med_path is None  # приоритеты 2/3 не пишут временный файл на диск
        finally:
            bot._fetch_media = original_fetch
    finally:
        bot.chat_state.pop(chat_id, None)


def test_resolve_incoming_media_priority_3_only_with_explicit_media_reference_words():
    # Без явного слова-указания на медиа (см. _looks_like_media_reference)
    # словесная отсылка не должна срабатывать вообще.
    chat_id = 999953
    state = bot.get_state(chat_id)
    state["recent_media_ids"] = {"555": [("old_file_id", "image/jpeg")]}
    try:
        msg = _FakeIncomingMessage(chat_id)
        msg.from_user = SimpleNamespace(id=555)
        msg.reply_to_message = None

        called = []

        async def fake_fetch_media(file_id, mime):
            called.append(file_id)
            return (b"bytes", mime)

        original_fetch = bot._fetch_media
        bot._fetch_media = fake_fetch_media
        try:
            _, _, _, media_tuple = asyncio.run(
                bot._resolve_incoming_media(msg, state, "расскажи про эту компанию", is_private=True)
            )
            assert media_tuple is None
            assert called == []
        finally:
            bot._fetch_media = original_fetch
    finally:
        bot.chat_state.pop(chat_id, None)


def test_resolve_incoming_media_sticker_request_ignores_unrelated_recent_photo():
    chat_id = 999954
    state = bot.get_state(chat_id)
    # Только фото в истории, стикера не было вообще — точно как в реальном инциденте.
    state["recent_media_ids"] = {"555": [("photo_id", "image/jpeg")]}
    try:
        msg = _FakeIncomingMessage(chat_id)
        msg.from_user = SimpleNamespace(id=555)
        msg.reply_to_message = None

        called = []

        async def fake_fetch_media(file_id, mime):
            called.append(file_id)
            return (b"bytes", mime)

        original_fetch = bot._fetch_media
        bot._fetch_media = fake_fetch_media
        try:
            _, _, _, media_tuple = asyncio.run(
                bot._resolve_incoming_media(msg, state, "покажи стикер", is_private=True)
            )
            # Честное "не нахожу" — а не описание случайного фото под видом стикера.
            assert media_tuple is None
            assert called == []
        finally:
            bot._fetch_media = original_fetch
    finally:
        bot.chat_state.pop(chat_id, None)


def test_resolve_incoming_media_sticker_request_finds_older_sticker_past_newer_photo():
    chat_id = 999955
    state = bot.get_state(chat_id)
    # Стикер раньше фото — ищем именно стикер, а не "последний элемент".
    state["recent_media_ids"] = {"555": [("sticker_id", "image/webp"), ("photo_id", "image/jpeg")]}
    try:
        msg = _FakeIncomingMessage(chat_id)
        msg.from_user = SimpleNamespace(id=555)
        msg.reply_to_message = None

        async def fake_fetch_media(file_id, mime):
            return (b"bytes-for-" + file_id.encode(), mime)

        original_fetch = bot._fetch_media
        bot._fetch_media = fake_fetch_media
        try:
            _, _, _, media_tuple = asyncio.run(
                bot._resolve_incoming_media(msg, state, "покажи стикер", is_private=True)
            )
            assert media_tuple == (b"bytes-for-sticker_id", "image/webp")
        finally:
            bot._fetch_media = original_fetch
    finally:
        bot.chat_state.pop(chat_id, None)


def test_notify_owner_sends_message_when_owner_and_bot_configured():
    original_owner, original_bot = bot.OWNER_ID, bot.bot
    bot.OWNER_ID = 12345
    fake = _FakeOwnerBot()
    bot.bot = fake
    try:
        asyncio.run(bot._notify_owner("тестовый алерт"))
        assert len(fake.sent) == 1
        assert fake.sent[0]["chat_id"] == 12345
        assert fake.sent[0]["text"] == "тестовый алерт"
    finally:
        bot.OWNER_ID, bot.bot = original_owner, original_bot


def test_notify_owner_noop_without_owner_id():
    original_owner, original_bot = bot.OWNER_ID, bot.bot
    bot.OWNER_ID = None
    fake = _FakeOwnerBot()
    bot.bot = fake
    try:
        asyncio.run(bot._notify_owner("не должно уйти"))
        assert fake.sent == []
    finally:
        bot.OWNER_ID, bot.bot = original_owner, original_bot


def test_notify_owner_never_raises_on_send_failure():
    original_owner, original_bot = bot.OWNER_ID, bot.bot
    bot.OWNER_ID = 12345

    class _FailingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("boom")

    bot.bot = _FailingBot()
    try:
        asyncio.run(bot._notify_owner("не должно упасть"))  # не должно поднять исключение
    finally:
        bot.OWNER_ID, bot.bot = original_owner, original_bot


def test_maybe_alert_gemini_exhausted_throttled():
    # См. аналогичный урок в test_reset_quota_if_new_day_clears_used_and_exhausted_
    # on_day_rollover выше: time.monotonic() не гарантированно "далеко за" какой-то
    # абсолютной точкой (в свежем процессе может быть близко к нулю) — "давно" нужно
    # выражать относительно текущего time.monotonic(), а не буквальным 0.0.
    original_last = lumen_chat_state._last_gemini_exhausted_alert_monotonic
    original_owner, original_bot = bot.OWNER_ID, bot.bot
    bot.OWNER_ID = 12345
    fake = _FakeOwnerBot()
    bot.bot = fake
    lumen_chat_state._last_gemini_exhausted_alert_monotonic = time.monotonic() - bot.GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC - 10.0
    try:
        asyncio.run(bot._maybe_alert_gemini_exhausted())
        asyncio.run(bot._maybe_alert_gemini_exhausted())
        assert len(fake.sent) == 1  # второй вызов сразу же — троттлинг не пускает повтор
    finally:
        lumen_chat_state._last_gemini_exhausted_alert_monotonic = original_last
        bot.OWNER_ID, bot.bot = original_owner, original_bot


def test_passive_group_message_does_not_consume_quota(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.chat.type = bot.ChatType.GROUP
    message.text = "обычный разговор"
    record = MagicMock()
    monkeypatch.setattr(bot, "_record_passive_group_context", record)
    asyncio.run(bot._handle_message_core(message))
    record.assert_called_once()
    assert lumen_limits.user_rate_limits == {}
    bot._tg_call.assert_not_awaited()


def test_log_queue_handler_masks_secrets_in_args_and_traceback(monkeypatch):
    monkeypatch.setattr(bot, "LUMEN_PROXY_SECRET", "fake-proxy-secret-123", raising=False)
    try:
        raise RuntimeError("leak fake-proxy-secret-123")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "bot", logging.ERROR, "path", 1,
        "failed with token %s", ("fake-proxy-secret-123",), exc_info=exc_info,
    )
    prepared = bot._LOG_QUEUE_HANDLER.prepare(record)
    rendered = bot._LOG_QUEUE_HANDLER.format(prepared)
    assert "fake-proxy-secret-123" not in rendered
    assert "<REDACTED>" in rendered


def test_secret_log_formatter_masks_message_before_queue(monkeypatch):
    formatter = bot._SecretLogFormatter("%(message)s")
    monkeypatch.setattr(bot, "LUMEN_PROXY_SECRET", "fake-formatter-secret-456", raising=False)
    record = logging.LogRecord(
        "bot", logging.INFO, "path", 1,
        "key is %s", ("fake-formatter-secret-456",), None,
    )
    assert "fake-formatter-secret-456" not in formatter.format(record)
    assert "<REDACTED>" in formatter.format(record)


def test_current_log_secrets_reads_env_before_module_globals(monkeypatch):
    monkeypatch.delenv("LUMEN_PROXY_SECRET", raising=False)
    monkeypatch.setattr(bot, "LUMEN_PROXY_SECRET", "", raising=False)
    monkeypatch.setenv("LUMEN_PROXY_SECRET", "fake-env-only-secret-789")
    secrets = bot._current_log_secrets()
    assert "fake-env-only-secret-789" in secrets
    monkeypatch.delenv("LUMEN_PROXY_SECRET", raising=False)


def test_normalize_lang_fallbacks_to_english():
    import lumen_lang
    assert lumen_lang.normalize_lang(None) == "en"
    assert lumen_lang.normalize_lang("") == "en"
    assert lumen_lang.normalize_lang("xx") == "en"
    assert lumen_lang.normalize_lang("ru-RU") == "ru"
    assert lumen_lang.normalize_lang("UK") == "uk"


def test_chat_lang_defaults_to_english_and_survives_garbage(monkeypatch):
    monkeypatch.setattr(bot, "get_state", lambda chat_id: {})
    assert bot._chat_lang(123) == "en"
    monkeypatch.setattr(bot, "get_state", lambda chat_id: {"lang": "uk"})
    assert bot._chat_lang(123) == "uk"
    monkeypatch.setattr(bot, "get_state", lambda chat_id: {"lang": "xx"})
    assert bot._chat_lang(123) == "en"


def test_serialize_chat_state_persists_lang():
    import lumen_state_storage
    snap = lumen_state_storage._serialize_chat_state({"history": [], "recent_media_ids": {}, "lang": "kk"})
    assert snap["lang"] == "kk"
    snap2 = lumen_state_storage._serialize_chat_state({"history": []})
    assert snap2["lang"] == "en"


def test_module_alias_for_prod_entry():
    # Прод-инцидент: прод запускается как `python bot.py` (__main__), а десятки
    # отложенных `import bot` внутри функций без алиаса выполняли весь bot.py
    # вторым модулем — пустое состояние и вечный bot=None (все апдейты в 503).
    # Тест-канарейка: алиас обязан оставаться в голове bot.py, проверяется текстом.
    src = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
    assert 'sys.modules.setdefault("bot"' in src or "sys.modules.setdefault('bot'" in src
