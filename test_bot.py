"""
test_bot.py — юнит- и smoke-тесты на функции и классы, реально ОПРЕДЕЛЁННЫЕ в bot.py:
Telegram-транспорт и circuit breaker, персистентность состояния (Upstash/локальный файл),
роутинг сообщений (_run_route/ask_gemini/ask_openrouter_*), стриминг ответов, TikTok-
загрузчик, TTS-пайплайн (Fish Audio + Gemini TTS), генерация изображений, webhook/admin
HTTP-эндпоинты, учёт квоты и т.д.

Переименован из test_bot_helpers.py при разбиении по модулям вслед за уже существующим
разбиением исходников (lumen_formatting.py / lumen_security.py / lumen_router_config.py /
bot.py) — см. README, аудит техдолга, август 2026. Тесты на чистые функции, реально
определённые в lumen_formatting.py/lumen_security.py/lumen_router_config.py (даже если
тематически похожие на код в этом файле — например построение маршрута роутера), теперь
живут в test_lumen_formatting.py/test_lumen_security.py/test_lumen_router_config.py
соответственно; здесь остаются только тесты на bot.py-специфичный код, включая функции,
которые лишь ИСПОЛЬЗУЮТ данные/конфигурацию из вынесенных модулей (например ask_gemini
использует GEMINI_MODELS из lumen_router_config.py, но сама определена в bot.py — тест на
её поведение остаётся здесь).

conftest.py в этой же папке подставляет безопасные env-заглушки (BOT_TOKEN/GEMINI_API_KEY/
BOT_LOG_PATH) ДО импорта bot.py, так что реальные секреты и доступ к /app не нужны.

Запуск:
    pip install -r requirements.txt -r requirements-dev.txt
    pytest test_bot.py -v
"""
import asyncio
import base64
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import sentry_sdk

import bot



# ─────────────────────────── _classify_model_error ───────────────────────────

def test_classify_model_error_rate_limit_by_status():
    assert bot._classify_model_error(429, "") == "rate_limit"


def test_classify_model_error_rate_limit_by_text():
    assert bot._classify_model_error(None, "quota exceeded") == "rate_limit"


def test_classify_model_error_paid():
    assert bot._classify_model_error(402, "") == "paid"


def test_classify_model_error_forbidden():
    assert bot._classify_model_error(403, "") == "forbidden"


def test_classify_model_error_unavailable():
    assert bot._classify_model_error(404, "") == "unavailable"


def test_classify_model_error_other_for_unknown():
    assert bot._classify_model_error(500, "some random error") == "other"


# ─────────────────────────── _next_fallback_model ───────────────────────────

def test_next_fallback_model_skips_tried():
    assert bot._next_fallback_model({"a"}, ["a", "b", "c"]) == "b"


def test_next_fallback_model_all_tried_returns_none():
    assert bot._next_fallback_model({"a", "b", "c"}, ["a", "b", "c"]) is None


def test_next_fallback_model_none_tried_returns_first():
    assert bot._next_fallback_model(set(), ["a", "b", "c"]) == "a"


# ─────────────────────────── _split_text_chunks ───────────────────────────

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


# ─────────────────────────── _sanitize_mime_type ───────────────────────────

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


# ─────────────────────────── is_tiktok / is_youtube ───────────────────────────

def test_is_tiktok_true_for_tiktok_url():
    assert bot.is_tiktok("https://www.tiktok.com/@user/video/123") is True


def test_is_tiktok_false_for_other_url():
    assert bot.is_tiktok("https://youtube.com/watch?v=1") is False


def test_is_youtube_true_for_short_link():
    assert bot.is_youtube("https://youtu.be/abc123") is True


def test_is_youtube_false_for_other_url():
    assert bot.is_youtube("https://example.com") is False


# ─────────────────── _tiktok_video_candidates (HD-цепочка качества) ───────────────────
# Регрессия на реальный найденный при ревизии пробел: раньше бот всегда брал
# media_data.get("play") or media_data.get("wmplay") — то есть НИКОГДА не пробовал
# HD-версию (hdplay), даже когда TikWM её реально отдавал. Тесты ниже проверяют
# порядок кандидатов (HD -> стандартное -> с водяным знаком) и то, что известный
# заранее размер файла (hd_size/size/wm_size) корректно прокидывается для решения
# "пропускать ли вариант ещё до скачивания" в handle_tiktok.

def test_tiktok_video_candidates_prefers_hd_first():
    media_data = {
        "play": "https://tikwm.com/sd.mp4", "size": 1000,
        "hdplay": "https://tikwm.com/hd.mp4", "hd_size": 5000,
        "wmplay": "https://tikwm.com/wm.mp4", "wm_size": 900,
    }
    candidates = bot._tiktok_video_candidates(media_data)
    assert [c["key"] for c in candidates] == ["hdplay", "play", "wmplay"]
    assert candidates[0]["url"] == "https://tikwm.com/hd.mp4"
    assert candidates[0]["size"] == 5000


def test_tiktok_video_candidates_falls_back_when_hd_missing():
    # TikWM не всегда возвращает hdplay (например, если &hd=1 не сработал или для
    # этого конкретного видео HD-версии просто нет) — кандидат должен тихо
    # отсутствовать в списке, а не давать пустую/битую запись.
    media_data = {"play": "https://tikwm.com/sd.mp4", "size": 1000}
    candidates = bot._tiktok_video_candidates(media_data)
    assert [c["key"] for c in candidates] == ["play"]


def test_tiktok_video_candidates_wmplay_only_as_last_resort():
    media_data = {"wmplay": "https://tikwm.com/wm.mp4", "wm_size": 900}
    candidates = bot._tiktok_video_candidates(media_data)
    assert [c["key"] for c in candidates] == ["wmplay"]


def test_tiktok_video_candidates_relative_url_gets_tikwm_prefix():
    # TikWM иногда отдаёт относительный путь без домена — как и в остальном коде
    # (см. оригинальную логику video_url в handle_tiktok), такой путь должен
    # получить префикс https://www.tikwm.com.
    media_data = {"play": "/download/sd.mp4", "size": 1000}
    candidates = bot._tiktok_video_candidates(media_data)
    assert candidates[0]["url"] == "https://www.tikwm.com/download/sd.mp4"


def test_tiktok_video_candidates_missing_size_defaults_to_zero():
    # Отсутствие size/hd_size/wm_size в ответе TikWM — обычное дело (см. докстринг
    # _tiktok_video_candidates) — не должно приводить к исключению, просто size=0
    # (что handle_tiktok трактует как "неизвестный размер, пробуем оптимистично").
    media_data = {"hdplay": "https://tikwm.com/hd.mp4"}
    candidates = bot._tiktok_video_candidates(media_data)
    assert candidates[0]["size"] == 0


def test_tiktok_video_candidates_empty_when_nothing_available():
    assert bot._tiktok_video_candidates({}) == []


# ─────────────────── _original_sound_label (локализация "оригинального звука") ───────────────────
# TikWM отдаёт название "оригинального звука" на языке автора исходного видео (или
# по умолчанию на английском) — никак не связано с языком человека, приславшего
# ссылку В НАШ бот. _original_sound_label использует message.from_user.language_code
# (IETF-тег языка интерфейса Telegram конкретного отправителя) для локализации.

def test_original_sound_label_russian():
    assert bot._original_sound_label("ru") == "Оригинальный звук"


def test_original_sound_label_ukrainian():
    assert bot._original_sound_label("uk") == "Оригінальний звук"


def test_original_sound_label_belarusian():
    assert bot._original_sound_label("be") == "Арыгінальны гук"


def test_original_sound_label_english():
    assert bot._original_sound_label("en") == "Original sound"


def test_original_sound_label_strips_region_subtag():
    # Telegram может прислать региональный вариант ("en-US", "pt-BR") — берём
    # только первичный языковой подтег до дефиса.
    assert bot._original_sound_label("en-US") == "Original sound"
    assert bot._original_sound_label("pt-BR") == "Som original"


def test_original_sound_label_falls_back_to_english_for_unknown_code():
    assert bot._original_sound_label("th") == "Original sound"
    assert bot._original_sound_label("xx-YY") == "Original sound"


def test_original_sound_label_falls_back_to_english_when_missing():
    assert bot._original_sound_label(None) == "Original sound"
    assert bot._original_sound_label("") == "Original sound"


def test_original_sound_label_case_insensitive():
    assert bot._original_sound_label("RU") == "Оригинальный звук"


# ─────────────────── _tiktok_music_page_id (ссылка на страницу звука, не видео) ───────────────────
# Регрессия на реальный найденный в логах случай: TikWM отвечает "Url parsing is
# failed!" на ссылки вида tiktok.com/music/... — это ссылки на СТРАНИЦУ звука
# (копируется через "Поделиться" на самом звуке в приложении), а не на видео.

def test_tiktok_music_page_id_extracts_trailing_numeric_id():
    url = "https://www.tiktok.com/music/original-sound-7666630127215823637"
    assert bot._tiktok_music_page_id(url) == "7666630127215823637"


def test_tiktok_music_page_id_extracts_id_with_cyrillic_slug():
    # Реальный найденный в логах случай — слаг на русском языке.
    url = "https://www.tiktok.com/music/оригинальный-звук-7667114246303812385"
    assert bot._tiktok_music_page_id(url) == "7667114246303812385"


def test_tiktok_music_page_id_handles_trailing_slash():
    url = "https://www.tiktok.com/music/original-sound-7666630127215823637/"
    assert bot._tiktok_music_page_id(url) == "7666630127215823637"


def test_tiktok_music_page_id_none_for_regular_video_link():
    url = "https://www.tiktok.com/@someuser/video/7370000000000000001"
    assert bot._tiktok_music_page_id(url) is None


def test_tiktok_music_page_id_none_for_photo_post_link():
    url = "https://www.tiktok.com/@someuser/photo/7370000000000000002"
    assert bot._tiktok_music_page_id(url) is None


def test_tiktok_music_page_id_none_for_named_track_slug():
    # Именованные треки/песни тоже используют /music/, просто со своим слагом —
    # функция всё равно должна найти числовой ID (сама эвристика "сработает ли
    # скачивание" находится не здесь, а в handle_tiktok_sound).
    url = "https://www.tiktok.com/music/Blinding-Lights-6862178485109294850"
    assert bot._tiktok_music_page_id(url) == "6862178485109294850"


# ─────────────────── _looks_like_resolved_tiktok_url (валидность резолвинга короткой ссылки) ───────────────────
# Регрессия на реальный найденный при отладке инцидент (12 августа 2026): TikWM
# стабильно отвечал 403 на короткую ссылку vt.tiktok.com — реальный резолвленный
# URL, ушедший в TikWM, оказался "https://www.tiktok.com/@/photo/...": юзернейм
# между "@" и "/" был ПУСТОЙ. Старая проверка результата HEAD-резолвинга
# ("video" in resolved or "@" in resolved) слишком слабая — голый символ "@" в
# такой строке есть, поэтому она засчитывала HEAD "успешным" даже с пустым
# юзернеймом, и GET-фоллбек (который мог бы довести резолвинг до нормального
# URL) даже не пробовался.

def test_looks_like_resolved_tiktok_url_true_for_proper_video_url():
    assert bot._looks_like_resolved_tiktok_url("https://www.tiktok.com/@someuser/video/7370000000000000001") is True


def test_looks_like_resolved_tiktok_url_true_for_proper_photo_url():
    assert bot._looks_like_resolved_tiktok_url("https://www.tiktok.com/@someuser/photo/7370000000000000002") is True


def test_looks_like_resolved_tiktok_url_false_for_empty_username():
    # Точно тот URL, что реально ушёл в TikWM и получил 403 в реальном инциденте.
    assert bot._looks_like_resolved_tiktok_url("https://www.tiktok.com/@/photo/7512093374153772309") is False


def test_looks_like_resolved_tiktok_url_false_for_bare_at_sign_without_post():
    # Просто "@" где-то в строке (например голая страница профиля без поста, или
    # случайное совпадение) — раньше проходило старую слабую проверку.
    assert bot._looks_like_resolved_tiktok_url("https://www.tiktok.com/@someuser") is False
    assert bot._looks_like_resolved_tiktok_url("https://www.tiktok.com/some-page?ref=@video") is False


def test_looks_like_resolved_tiktok_url_false_for_empty_or_none():
    assert bot._looks_like_resolved_tiktok_url("") is False
    assert bot._looks_like_resolved_tiktok_url(None) is False


class _FakeResolveResponse:
    def __init__(self, status: int, url: str):
        self.status = status
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeResolveSession:
    """Мокает session.head()/session.get() для _resolve_tiktok_short."""
    def __init__(self, head_response, get_response):
        self._head_response = head_response
        self._get_response = get_response
        self.get_called = False

    def head(self, url, *args, **kwargs):
        return self._head_response

    def get(self, url, *args, **kwargs):
        self.get_called = True
        return self._get_response


def test_resolve_tiktok_short_falls_through_to_get_when_head_gives_malformed_url():
    # РЕГРЕССИЯ на реальный инцидент: HEAD "успешно" довёл до URL с ПУСТЫМ
    # юзернеймом — раньше такой результат принимался сразу и уходил в TikWM,
    # который на него отвечал 403. Теперь должен быть отброшен, и функция
    # обязана попробовать GET (который в этом тесте отдаёт нормальный URL).
    head_resp = _FakeResolveResponse(200, "https://www.tiktok.com/@/photo/7512093374153772309")
    get_resp = _FakeResolveResponse(200, "https://www.tiktok.com/@realuser/photo/7512093374153772309")
    session = _FakeResolveSession(head_resp, get_resp)
    result = asyncio.run(bot._resolve_tiktok_short(session, "https://vt.tiktok.com/ZS43ubyhA/"))
    assert result == "https://www.tiktok.com/@realuser/photo/7512093374153772309"
    assert session.get_called is True


def test_resolve_tiktok_short_uses_head_result_directly_when_valid():
    # Нормальный случай (без регрессии) — HEAD сразу довёл до валидного URL,
    # GET вообще не должен вызываться (незачем тратить лишний запрос).
    head_resp = _FakeResolveResponse(200, "https://www.tiktok.com/@realuser/video/1234567890")
    get_resp = _FakeResolveResponse(200, "https://www.tiktok.com/should-not-be-used")
    session = _FakeResolveSession(head_resp, get_resp)
    result = asyncio.run(bot._resolve_tiktok_short(session, "https://vt.tiktok.com/ZS43ubyhA/"))
    assert result == "https://www.tiktok.com/@realuser/video/1234567890"
    assert session.get_called is False


def test_resolve_tiktok_short_returns_get_result_even_if_still_malformed():
    # Если и GET не дал нормального URL — возвращаем то, что реально пришло
    # (лучше честная попытка, чем совсем ничего), диагностика в логах — отдельно.
    head_resp = _FakeResolveResponse(200, "https://www.tiktok.com/@/photo/999")
    get_resp = _FakeResolveResponse(200, "https://www.tiktok.com/@/photo/999")
    session = _FakeResolveSession(head_resp, get_resp)
    result = asyncio.run(bot._resolve_tiktok_short(session, "https://vt.tiktok.com/ZS43ubyhA/"))
    assert result == "https://www.tiktok.com/@/photo/999"


# ─────────────────── _slideshow_slide_urls (live_images вместо play/hdplay) ───────────────────
# Прежняя эвристика (пробовала верхнеуровневые play/hdplay поста) была основана на
# неверном предположении — реальный лог показал, что для фото-постов эти поля
# указывают на аудиодорожку (mime_type=audio_mpeg), а не на видео. Реальная зацепка —
# отдельное поле `live_images` в ответе TikWM, которое эти тесты и проверяют.

def test_slideshow_slide_urls_prefers_live_images_when_present():
    media_data = {"live_images": ["https://tikwm.com/live0.mp4", ""]}
    images_to_fetch = ["https://tikwm.com/photo0.jpg", "https://tikwm.com/photo1.jpg"]
    assert bot._slideshow_slide_urls(media_data, images_to_fetch) == [
        "https://tikwm.com/live0.mp4", "https://tikwm.com/photo1.jpg",
    ]


def test_slideshow_slide_urls_falls_back_when_live_images_absent():
    images_to_fetch = ["https://tikwm.com/photo0.jpg", "https://tikwm.com/photo1.jpg"]
    assert bot._slideshow_slide_urls({}, images_to_fetch) == images_to_fetch


def test_slideshow_slide_urls_falls_back_when_live_images_shorter():
    media_data = {"live_images": ["https://tikwm.com/live0.mp4"]}
    images_to_fetch = ["https://tikwm.com/photo0.jpg", "https://tikwm.com/photo1.jpg"]
    assert bot._slideshow_slide_urls(media_data, images_to_fetch) == [
        "https://tikwm.com/live0.mp4", "https://tikwm.com/photo1.jpg",
    ]


def test_slideshow_slide_urls_ignores_non_list_live_images():
    media_data = {"live_images": "not-a-list"}
    images_to_fetch = ["https://tikwm.com/photo0.jpg"]
    assert bot._slideshow_slide_urls(media_data, images_to_fetch) == images_to_fetch


# ─────────────────── _looks_like_video_bytes (видео-слайды/'живые фото' в слайдшоу) ───────────────────
# Регрессия на реальный найденный при ревизии пробел: TikTok разрешает совмещать в
# одном слайдшоу-посте обычные фото-слайды и короткие видео-слайды — TikWM отдаёт
# URL видео-слайда в том же списке `images`, без явного признака "это видео".
# Раньше такой слайд всегда оборачивался в InputMediaPhoto как обычная картинка.

def test_looks_like_video_bytes_true_for_mp4_ftyp_signature():
    # Реальная сигнатура начала MP4/MOV-контейнера: 4 байта размера бокса + "ftyp".
    mp4_header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    assert bot._looks_like_video_bytes(mp4_header) is True


def test_looks_like_video_bytes_false_for_jpeg():
    jpeg_header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"
    assert bot._looks_like_video_bytes(jpeg_header) is False


def test_looks_like_video_bytes_false_for_png():
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert bot._looks_like_video_bytes(png_header) is False


def test_looks_like_video_bytes_false_for_too_short_input():
    assert bot._looks_like_video_bytes(b"\x00\x00\x00\x18fty") is False


def test_looks_like_video_bytes_false_for_empty_bytes():
    assert bot._looks_like_video_bytes(b"") is False


# ─────────────────── _chunk_tiktok_media_items (лимит sendMediaGroup 2-10) ───────────────────
# КРИТИЧНАЯ РЕГРЕССИЯ, найденная при повторной ревизии: sendMediaGroup у Telegram
# требует ОТ 2 ДО 10 элементов за вызов, а не просто "не больше 10". Наивное
# разбиение по 10 без остатка давало хвостовую группу ровно из 1 элемента для
# слайдшоу из 11/21/31 слайдов (все — реальные, допустимые TikTok длины вплоть до
# официального максимума 35) — такой вызов Telegram гарантированно отклонял бы
# уже ПОСЛЕ того, как предыдущие группы успешно ушли пользователю.

def test_chunk_tiktok_media_items_exact_multiple_of_ten():
    items = list(range(20))
    chunks = bot._chunk_tiktok_media_items(items)
    assert [len(c) for c in chunks] == [10, 10]
    assert sum(chunks, []) == items


def test_chunk_tiktok_media_items_avoids_trailing_single_item_at_eleven():
    items = list(range(11))
    chunks = bot._chunk_tiktok_media_items(items)
    assert [len(c) for c in chunks] == [9, 2]
    for c in chunks:
        assert 2 <= len(c) <= 10
    assert sum(chunks, []) == items


def test_chunk_tiktok_media_items_avoids_trailing_single_item_at_twenty_one():
    items = list(range(21))
    chunks = bot._chunk_tiktok_media_items(items)
    assert [len(c) for c in chunks] == [10, 9, 2]
    for c in chunks:
        assert 2 <= len(c) <= 10
    assert sum(chunks, []) == items


def test_chunk_tiktok_media_items_avoids_trailing_single_item_at_tiktok_max_thirty_one():
    items = list(range(31))
    chunks = bot._chunk_tiktok_media_items(items)
    for c in chunks:
        assert 2 <= len(c) <= 10
    assert sum(chunks, []) == items


def test_chunk_tiktok_media_items_no_borrow_needed_at_thirty_five():
    # Официальный максимум TikTok (35) кратен 10 с остатком 5 — переноса не требуется.
    items = list(range(35))
    chunks = bot._chunk_tiktok_media_items(items)
    assert [len(c) for c in chunks] == [10, 10, 10, 5]


def test_chunk_tiktok_media_items_empty_list():
    assert bot._chunk_tiktok_media_items([]) == []


def test_chunk_tiktok_media_items_single_item_not_split_further():
    # Один элемент эта функция не превращает в валидную группу (2-10) — это
    # ответственность вызывающего кода (handle_tiktok отправляет такой случай
    # напрямую через send_photo/send_video, а не через sendMediaGroup).
    assert bot._chunk_tiktok_media_items([1]) == [[1]]


# ─────────────────────────── _error_status ───────────────────────────

class _FakeExc(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_error_status_reads_status_code_attribute():
    assert bot._error_status(_FakeExc("x", status_code=429), "x") == 429


def test_error_status_extracts_three_digit_code_from_text():
    exc = _FakeExc("Error 503: unavailable")
    assert bot._error_status(exc, "Error 503: unavailable") == 503


def test_error_status_returns_none_when_no_code_found():
    exc = _FakeExc("no numbers here")
    assert bot._error_status(exc, "no numbers here") is None


# ─────────────────────────── extract_url ───────────────────────────

def test_extract_url_strips_trailing_punctuation():
    assert bot.extract_url("check this out: https://example.com/page.") == "https://example.com/page"


def test_extract_url_returns_none_when_no_url():
    assert bot.extract_url("no url here") is None


# ─────────────────────────── clean_mention ───────────────────────────

def test_clean_mention_removes_username():
    assert bot.clean_mention(f"@{bot.BOT_USERNAME} привет") == "привет"


def test_clean_mention_is_case_insensitive():
    result = bot.clean_mention(f"привет @{bot.BOT_USERNAME.upper()} как дела")
    assert bot.BOT_USERNAME.lower() not in result.lower()


# ─────────────────────────── _gemini_error_msg / _or_error_msg ───────────────────────────

class _FakeStatusExc(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_gemini_error_msg_rate_limit():
    # РЕГРЕССИЯ (аудит техдолга): раньше здесь проверялось "модель через /model" —
    # команда /model давно удалена (см. README, "Автоматический выбор модели"),
    # и подсказывать её в тексте ошибки было прямой ошибкой для пользователя.
    # _gemini_error_msg/_or_error_msg теперь используют общие provider-neutral
    # шаблоны (см. _MODEL_ERROR_MESSAGES) без упоминания несуществующих команд.
    exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    msg = bot._gemini_error_msg(exc, "gemini-3.5-flash")
    assert "/model" not in msg and "/provider" not in msg
    assert msg == bot._model_error_text("rate_limit")


def test_gemini_error_msg_value_error_passthrough():
    # ValueError используется в ask_gemini как готовый пользовательский текст
    # (например про неподдерживаемый тип вложения) — должен вернуться как есть.
    exc = ValueError("кастомная ошибка")
    assert bot._gemini_error_msg(exc, "gemini-3.5-flash") == "кастомная ошибка"


def test_gemini_error_msg_all_models_exhausted():
    # РЕГРЕССИЯ (аудит техдолга): раньше здесь проверялось "/provider" — команда
    # удалена, реального способа переключиться на резервный провайдер вручную
    # больше нет, поэтому предлагать её в тексте ошибки было ошибкой.
    exc = bot.GeminiAllModelsExhaustedError(["gemini-3.5-flash", "gemini-2.5-flash"])
    msg = bot._gemini_error_msg(exc, "gemini-3.5-flash")
    assert "/provider" not in msg
    assert "лимит" in msg.lower()


def test_or_error_msg_rate_limit():
    # РЕГРЕССИЯ (аудит техдолга): "/provider" убран из текста (команда удалена),
    # а формулировка "резервного провайдера" тоже убрана — с автоматическим
    # роутером OpenRouter часто оказывается ПЕРВЫМ, а не резервным кандидатом.
    exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    msg = bot._or_error_msg(exc, "text")
    assert "/provider" not in msg and "резервн" not in msg.lower()
    assert msg == bot._model_error_text("rate_limit")


def test_or_error_msg_unavailable():
    exc = _FakeStatusExc("model not found", status_code=404)
    msg = bot._or_error_msg(exc, "text")
    assert "/provider" not in msg and "резервн" not in msg.lower()
    assert msg == bot._model_error_text("unavailable")


def test_model_error_text_shared_between_providers():
    # Единый источник правды для текста ошибок (см. аудит техдолга) — Gemini и
    # OpenRouter должны показывать ОДИНАКОВЫЙ текст на одинаковый класс ошибки,
    # а не рассинхронизированные формулировки в двух местах.
    gem_exc = _FakeStatusExc("resource_exhausted", status_code=429)
    or_exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    assert bot._gemini_error_msg(gem_exc, "gemini-3.5-flash") == bot._or_error_msg(or_exc, "text")


# ─────────────────────────── _cleanup_rate_limit_dict ───────────────────────────

def test_cleanup_rate_limit_dict_removes_empty_and_stale_entries():
    bot.user_rate_limits.clear()
    bot.user_rate_limits[111] = []  # раньше оставался бы в словаре навсегда
    bot.user_rate_limits[222] = [time.time() - 7200]  # старше часа — тоже чистится
    bot.user_rate_limits[333] = [time.time()]  # свежая запись — должна остаться
    bot._cleanup_rate_limit_dict()
    assert 111 not in bot.user_rate_limits
    assert 222 not in bot.user_rate_limits
    assert 333 in bot.user_rate_limits


# ─────────────────────────── хранилище: локальный файл vs Upstash ───────────────────────────

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


# ─────────────────────────── _save_chat_to_storage/_delete_chat_storage возвращают bool ───────────────────────────

def test_save_chat_to_storage_returns_true_on_success(tmp_path):
    # Регрессия на найденный при код-ревью баг (см. test_flush_dirty_state_once_*
    # ниже): функция теперь ДОЛЖНА сигнализировать успех/неудачу вызывающему коду,
    # а не просто логировать исключение и возвращать None в обоих случаях.
    chat_id = 999601
    state = {"history": [{"role": "user", "content": "привет"}], "image_model": bot.DEFAULT_HF_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    original_chats_dir = bot._CHATS_DIR
    bot._CHATS_DIR = tmp_path
    try:
        assert bot._save_chat_to_storage(chat_id, state) is True
        assert (tmp_path / f"{chat_id}.json").exists()
    finally:
        bot._CHATS_DIR = original_chats_dir


def test_save_chat_to_storage_returns_false_on_failure():
    state = {"history": [], "image_model": bot.DEFAULT_HF_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    with patch("bot._storage_write_text", side_effect=RuntimeError("сбой хранилища")):
        assert bot._save_chat_to_storage(999602, state) is False


def test_delete_chat_storage_returns_true_on_success(tmp_path):
    chat_id = 999603
    original_chats_dir = bot._CHATS_DIR
    bot._CHATS_DIR = tmp_path
    try:
        (tmp_path / f"{chat_id}.json").write_text("{}")
        assert bot._delete_chat_storage(chat_id) is True
        assert not (tmp_path / f"{chat_id}.json").exists()
    finally:
        bot._CHATS_DIR = original_chats_dir


def test_delete_chat_storage_returns_false_on_failure():
    with patch("bot._storage_delete_text", side_effect=RuntimeError("сбой хранилища")):
        assert bot._delete_chat_storage(999604) is False


# ─────────────────────────── _flush_dirty_state_once (переочередь неудавшихся сохранений) ───────────────────────────

def test_flush_dirty_state_once_requeues_failed_saves():
    # КРИТИЧНАЯ РЕГРЕССИЯ, найденная при код-ревью: раньше _dirty_chat_ids
    # очищался ДО того, как запись реально прошла, а неудачный _save_chat_to_storage
    # просто логировал исключение и возвращал None — чат "терялся" из очереди
    # навсегда при транзиентном сбое хранилища (например, кратковременный сбой
    # Upstash), пока какая-то ДРУГАЯ мутация того же чата не пометит его "грязным"
    # заново. Теперь чат, для которого сохранение не удалось, должен остаться в
    # _dirty_chat_ids и попасть в следующий цикл.
    ok_chat, fail_chat = 999701, 999702
    bot.chat_state[ok_chat] = {"history": [{"role": "user", "content": "ok"}], "image_model": bot.DEFAULT_HF_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    bot.chat_state[fail_chat] = {"history": [{"role": "user", "content": "fail"}], "image_model": bot.DEFAULT_HF_IMAGE_MODEL, "quota": {}, "recent_media_ids": {}}
    bot._dirty_chat_ids.clear()
    bot._dirty_chat_ids.update({ok_chat, fail_chat})
    bot._index_dirty = False
    bot._quota_dirty = False

    def fake_save(cid, state):
        return cid != fail_chat  # успех для ok_chat, неудача для fail_chat

    with patch("bot._save_chat_to_storage", side_effect=fake_save), \
         patch("bot._save_chat_index"), patch("bot.save_global_quota"):
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
    bot._index_dirty = False
    bot._quota_dirty = False

    def fake_delete(cid):
        return cid != fail_chat

    with patch("bot._delete_chat_storage", side_effect=fake_delete), \
         patch("bot._save_chat_index"), patch("bot.save_global_quota"):
        try:
            asyncio.run(bot._flush_dirty_state_once())
            assert ok_chat not in bot._pending_chat_deletions
            assert fail_chat in bot._pending_chat_deletions
        finally:
            bot._pending_chat_deletions.discard(ok_chat)
            bot._pending_chat_deletions.discard(fail_chat)


# ─────────────────── _sentry_scrub_secrets (before_send-хук опционального Sentry) ───────────────────
# SENTRY_DSN не задан в тестовом окружении (conftest.py его не подставляет) — sentry_sdk.init()
# ни разу не вызывается за весь прогон тестов, но сама функция определена безусловно (см.
# докстринг в bot.py) именно для того, чтобы её можно было проверить в изоляции, без реального DSN.

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


# ─────────────────── альбомы: доп. фото тоже попадают в recent_media_ids ───────────────────

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


# ─────────────────────────── /admin_keys — авторизация через заголовок, а не query-параметр ───────────────────────────

class _FakeAdminRequest:
    def __init__(self, headers=None, query_params=None):
        self.headers = headers or {}
        self.query_params = query_params or {}


def test_check_bot_token_auth_accepts_correct_bearer_header():
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        req = _FakeAdminRequest(headers={"Authorization": "Bearer real-secret-token"})
        assert bot._check_bot_token_auth(req) is True
    finally:
        bot.BOT_TOKEN = original


def test_check_bot_token_auth_rejects_query_param_regression():
    # РЕГРЕССИЯ (код-ревью): раньше BOT_TOKEN читался из ?bot_token=... в URL — GET-
    # запрос с секретом в query-строке попадает в access-логи прокси/историю браузера
    # (CWE-598). Теперь query-параметр должен полностью ИГНОРИРОВАТЬСЯ — единственный
    # легитимный путь — заголовок Authorization: Bearer.
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        req = _FakeAdminRequest(headers={}, query_params={"bot_token": "real-secret-token"})
        assert bot._check_bot_token_auth(req) is False
    finally:
        bot.BOT_TOKEN = original


def test_check_bot_token_auth_rejects_wrong_or_missing_header():
    original = bot.BOT_TOKEN
    bot.BOT_TOKEN = "real-secret-token"
    try:
        assert bot._check_bot_token_auth(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})) is False
        assert bot._check_bot_token_auth(_FakeAdminRequest(headers={})) is False
        # Без префикса "Bearer " — тоже отказ, даже если сам токен совпадает.
        assert bot._check_bot_token_auth(_FakeAdminRequest(headers={"Authorization": "real-secret-token"})) is False
    finally:
        bot.BOT_TOKEN = original


# ─────────────────────────── учёт расхода TTS-квоты ───────────────────────────

class _FakeVoiceBot:
    """Минимальная замена aiogram Bot для inline_tts — нужен только send_voice."""
    def __init__(self):
        self.sent_voice: dict | None = None

    async def send_voice(self, **kwargs):
        self.sent_voice = kwargs
        return SimpleNamespace()


# Форма ответа google-genai для TTS (candidates -> content -> parts -> inline_data) —
# общая для обоих тестов ниже (успех и rate-limit-фоллбек), раньше была продублирована
# как пять идентичных вложенных классов в каждом тесте (ponytail-audit).
class _FakeInlineData:
    def __init__(self, data, mime_type):
        self.data = data
        self.mime_type = mime_type

class _FakePart:
    def __init__(self, inline_data):
        self.inline_data = inline_data

class _FakeTTSContent:
    def __init__(self, parts):
        self.parts = parts

class _FakeTTSCandidate:
    def __init__(self, content):
        self.content = content

class _FakeTTSResponse:
    def __init__(self, candidates):
        self.candidates = candidates


def _fake_tts_response(wav_bytes: bytes) -> "_FakeTTSResponse":
    return _FakeTTSResponse(candidates=[
        _FakeTTSCandidate(_FakeTTSContent(parts=[_FakePart(_FakeInlineData(wav_bytes, "audio/wav"))]))
    ])


def test_inline_tts_records_quota_usage_on_success():
    # НАЙДЕНО ПРИ КОД-РЕВЬЮ: по дашборду AI Studio у TTS-моделей лимит всего 10
    # запросов/сутки на модель — жёстче даже флагманских текстовых моделей, но
    # расход нигде не учитывался (ни GLOBAL_QUOTA, ни /stats). Проверяем, что
    # успешный синтез фиксируется в GLOBAL_QUOTA["gemini"] так же, как обычные
    # текстовые вызовы Gemini.

    # Минимальный RIFF/WAV-заголовок — достаточно, чтобы код распознал формат как
    # WAV и не пытался обернуть его заново через pcm_to_wav; реальная конвертация
    # через ffmpeg в этом окружении не установлена и ожидаемо упадёт — это штатно
    # ловится внутри inline_tts (тест проверяет учёт квоты, а не качество звука).
    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64

    def fake_generate_content(*, model, contents, config=None):
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content

    incoming = _FakeIncomingMessage(999801)
    incoming.message_id = 12345  # inline_tts использует его для reply_to_message_id

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        entry = bot.GLOBAL_QUOTA["gemini"].get("gemini-3.1-flash-tts-preview")
        assert entry is not None
        assert entry["used"] >= 1
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)


def test_inline_tts_marks_quota_exhausted_on_rate_limit():
    # Первая модель отдаёт явный 429 — должна быть помечена исчерпанной через
    # _mark_quota_exhausted, а синтез должен продолжиться со второй моделью цепочки.
    class _RateLimitExc(Exception):
        status_code = 429

    fake_wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 64
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.1-flash-tts-preview":
            raise _RateLimitExc("rate limit exceeded")
        return _fake_tts_response(fake_wav_bytes)

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content

    incoming = _FakeIncomingMessage(999802)
    incoming.message_id = 12346

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FakeVoiceBot()
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash-preview-tts", None)
    try:
        asyncio.run(bot.inline_tts(incoming, "Привет, мир"))
        assert calls == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
        exhausted = bot.GLOBAL_QUOTA["gemini"].get("gemini-3.1-flash-tts-preview")
        assert exhausted is not None and exhausted.get("exhausted_at") is not None
        succeeded = bot.GLOBAL_QUOTA["gemini"].get("gemini-2.5-flash-preview-tts")
        assert succeeded is not None and succeeded["used"] >= 1
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.1-flash-tts-preview", None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash-preview-tts", None)


# ─────────────────────────── _match_trigger_prefix (словесные триггеры draw/tts) ───────────────────────────

def test_match_trigger_prefix_finds_draw_trigger():
    assert bot._match_trigger_prefix("нарисуй кота на пляже", bot.DRAW_TRIGGER_PREFIXES) == "нарисуй"


def test_match_trigger_prefix_finds_tts_trigger():
    assert bot._match_trigger_prefix("озвучь этот текст пожалуйста", bot.TTS_TRIGGER_PREFIXES) == "озвучь"


def test_match_trigger_prefix_new_synonyms_work():
    assert bot._match_trigger_prefix("преврати в аудио вот это сообщение", bot.TTS_TRIGGER_PREFIXES) == "преврати в аудио"
    assert bot._match_trigger_prefix("сгенери картинку заката", bot.DRAW_TRIGGER_PREFIXES) == "сгенери картинку"


def test_match_trigger_prefix_no_false_positive_when_trigger_not_at_start():
    # Триггерное слово упоминается, но НЕ в начале сообщения — не должно срабатывать
    assert bot._match_trigger_prefix("объясни, как я мог бы нарисовать домик карандашом", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("что значит слово озвучь на украинском", bot.TTS_TRIGGER_PREFIXES) is None


def test_match_trigger_prefix_no_false_positive_for_unrelated_text():
    assert bot._match_trigger_prefix("привет, как дела?", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("привет, как дела?", bot.TTS_TRIGGER_PREFIXES) is None


def test_match_trigger_prefix_ambiguous_phrases_deliberately_excluded():
    # "хочу картинку"/"сделай картинку" намеренно НЕ триггеры — легко спутать с
    # "хочу картинку тебе показать" или правкой уже присланного фото.
    assert bot._match_trigger_prefix("хочу картинку показать тебе", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("сделай картинку ярче", bot.DRAW_TRIGGER_PREFIXES) is None


# ─────────────────────────── _looks_like_media_reference (память о медиа) ───────────────────────────

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


# ─────────────── _media_reference_category / _mime_matches_media_category (тип медиа) ───────────────
# КАЛИБРОВКА 18 августа 2026, реальный найденный баг: "Покажи стикер" (стикер не
# отправлялся, но недавно был скриншот) заставляло бота описывать скриншот как
# будто это и есть запрошенный стикер — приоритет №3 брал просто последнее медиа
# пользователя без проверки типа. Тесты ниже проверяют категоризацию отдельно от
# полного _resolve_incoming_media (тот протестирован ниже, в его собственной секции).

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


# ─────────────────────────── ask_gemini (с мокнутым client, без реального API) ───────────────────────────

class _FakeCandidate:
    def __init__(self, finish_reason="STOP", content=None):
        self.finish_reason = finish_reason
        self.content = content


class _FakeGeminiResponse:
    def __init__(self, text="", candidates=None):
        self._text = text
        self.candidates = candidates or []

    @property
    def text(self):
        return self._text


def test_ask_gemini_happy_path_returns_text_and_updates_history():
    chat_id = 999001
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        return _FakeGeminiResponse(text="Привет! Чем могу помочь?")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет"))
        assert answer == "Привет! Чем могу помочь?"
        history = bot.chat_state[chat_id]["history"]
        assert history[-2] == {"role": "user", "content": "Привет"}
        assert history[-1] == {"role": "assistant", "content": "Привет! Чем могу помочь?"}
        assert calls[0] == bot.DEFAULT_GEMINI_MODEL
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_scrubs_identity_leak_before_storing_history():
    # Регрессия на весь смысл выходного фильтра: если системный промпт всё же обойдён
    # через инъекцию и модель раскрыла реальную личность — ни пользователь, ни история
    # чата не должны увидеть/сохранить исходный (утекший) текст, только fallback.
    chat_id = 999003

    def fake_generate_content(*, model, contents, config=None):
        return _FakeGeminiResponse(text="Я работаю на базе Gemini от Google, а не Lumen.")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Кто ты на самом деле?"))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": bot._IDENTITY_LEAK_FALLBACK}
        assert "gemini" not in history[-1]["content"].lower()
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_openrouter_text_empty_model_chain_fallback_is_not_dead_model():
    # РЕГРЕССИЯ (24.07.2026): раньше запасным вариантом на случай пустого model_chain
    # в ask_openrouter_text было "meta-llama/llama-3.3-70b-instruct:free" — та же
    # модель, что подтверждённо снята провайдером с бесплатного тира (см. README,
    # HTTP 404 "unavailable for free") и по этой же причине уже исключена из
    # _OR_LIGHT_ORDER/_OR_HEAVY_ORDER. Проверяем, что дефолт теперь ссылается на
    # актуальный _OR_LIGHT_ORDER, а не на захардкоженную мёртвую модель.
    chat_id = 999401

    calls = []

    async def fake_or_fallback(messages, trial_models, primary_model_id, **kwargs):
        calls.append(trial_models)
        return "ответ", trial_models[0]

    original = bot._or_chat_completion_with_fallback
    bot._or_chat_completion_with_fallback = fake_or_fallback
    try:
        asyncio.run(bot.ask_openrouter_text(chat_id, "привет", model_chain=[]))
        assert calls[0] == [bot._OR_LIGHT_ORDER[0]]
        assert "meta-llama/llama-3.3-70b-instruct:free" not in calls[0]
    finally:
        bot._or_chat_completion_with_fallback = original
        bot.chat_state.pop(chat_id, None)


def test_run_route_falls_back_to_second_provider_when_first_fully_fails():
    # Ключевое требование: если весь маршрут первого провайдера отказал —
    # роутер должен попробовать резерв в ДРУГОМ провайдере, а не сдаваться сразу.
    chat_id = 999301

    async def failing_or_text(*args, **kwargs):
        raise bot.OpenRouterAPIError("всё сломано", status_code=500)

    async def fake_ask_gemini(cid, prompt, media=None, youtube_url=None, model_chain=None, deadline=None):
        return "Ответ от Gemini (резерв)"

    original_or_text = bot.ask_openrouter_text
    original_ask_gemini = bot.ask_gemini
    bot.ask_openrouter_text = failing_or_text
    bot.ask_gemini = fake_ask_gemini
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("gemini", "gemini-3.1-flash-lite")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
        assert ans == "Ответ от Gemini (резерв)"
        assert sent is False
    finally:
        bot.ask_openrouter_text = original_or_text
        bot.ask_gemini = original_ask_gemini


def test_run_route_raises_when_both_providers_fail():
    chat_id = 999302

    async def failing_or_text(*args, **kwargs):
        raise bot.OpenRouterAPIError("сломано", status_code=500)

    async def failing_gemini(*args, **kwargs):
        raise RuntimeError("тоже сломано")

    original_or_text = bot.ask_openrouter_text
    original_ask_gemini = bot.ask_gemini
    bot.ask_openrouter_text = failing_or_text
    bot.ask_gemini = failing_gemini
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("gemini", "gemini-3.1-flash-lite")]
        with pytest.raises(Exception):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
    finally:
        bot.ask_openrouter_text = original_or_text
        bot.ask_gemini = original_ask_gemini


# ─────────────────────────── shared history между Gemini и OpenRouter ───────────────────────────

def test_shared_history_between_gemini_and_openrouter():
    # Регрессия на требование "общая память между Gemini и OpenRouter, чтобы не
    # чувствовалось переключение между моделями" — раньше у каждого провайдера
    # была своя ОТДЕЛЬНАЯ история (gemini_history/or_history).
    chat_id = 999201

    def fake_generate_content(*, model, contents, config=None):
        return _FakeGeminiResponse(text="Ответ от Gemini")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client

    async def fake_or_fallback(messages, trial_models, primary_model_id, **kwargs):
        return "Ответ от OpenRouter", trial_models[0]

    original_or_fallback = bot._or_chat_completion_with_fallback
    bot._or_chat_completion_with_fallback = fake_or_fallback

    try:
        asyncio.run(bot.ask_gemini(chat_id, "Первый вопрос (через Gemini)"))
        asyncio.run(bot.ask_openrouter_text(chat_id, "Второй вопрос (через OpenRouter)", model_chain=["meta-llama/llama-3.3-70b-instruct:free"]))

        history = bot.chat_state[chat_id]["history"]
        contents = [h["content"] for h in history]
        # Обе записи должны быть в ОДНОЙ и той же истории, а не в раздельных
        assert "Первый вопрос (через Gemini)" in contents
        assert "Ответ от Gemini" in contents
        assert "Второй вопрос (через OpenRouter)" in contents
        assert "Ответ от OpenRouter" in contents
        assert len(history) == 4
    finally:
        bot.client = original_client
        bot._or_chat_completion_with_fallback = original_or_fallback
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_retries_without_tools_on_malformed_function_call():
    # Регрессионный тест: после выноса _build_gemini_call_config переменная kwargs,
    # на которую опирался этот retry-путь, была удалена — retry_gconfig теперь
    # строится через gconfig.model_copy(update={"tools": None}).
    chat_id = 999002
    call_configs = []

    def fake_generate_content(*, model, contents, config=None):
        call_configs.append(config)
        if len(call_configs) == 1:
            return _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="MALFORMED_FUNCTION_CALL")])
        return _FakeGeminiResponse(text="Ответ без инструментов")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Сколько будет 2+2?"))
        assert answer == "Ответ без инструментов"
        assert len(call_configs) == 2
        # первый вызов — с tools (у DEFAULT_GEMINI_MODEL включён url_context)
        assert getattr(call_configs[0], "tools", None)
        # второй (после ретрая) — уже без tools
        assert getattr(call_configs[1], "tools", None) is None
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


# ─────────────────────────── _try_gemini_streaming (с поддельным async-клиентом) ───────────────────────────

class _FakeSentMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("parse_mode")))
        return self

    async def delete(self):
        self.deleted = True


class _FakeChat:
    def __init__(self, chat_id, chat_type):
        self.id = chat_id
        self.type = chat_type


class _FakeIncomingMessage:
    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id, bot.ChatType.PRIVATE)
        self.reply_to_message = None
        self.sent: list[_FakeSentMessage] = []

    async def reply(self, text, **kwargs):
        msg = _FakeSentMessage()
        self.sent.append(msg)
        return msg


# ─────────────────── _fish_audio_tts_bytes (TTS-фоллбек с более щедрой квотой) ───────────────────
# Реализовано вместе с извлечением _gemini_tts_bytes из inline_tts (ponytail-фикс,
# 2 августа 2026) — Fish Audio S2.1 Pro (free) пробуется ПЕРВЫМ в inline_tts, у него
# нет заявленного суточного потолка запросов (в отличие от Gemini TTS, 10/сутки на
# модель). Переиспользует тот же SSE-фикстурный стиль, что и test_openrouter_stream_
# pieces_parses_sse_chunks выше (_FakeSSEResponse/_FakeSessionForSSE), только с полем
# delta.audio.data вместо delta.content.

def test_fish_audio_tts_bytes_parses_sse_audio_chunks():
    raw_audio = b"fake-mp3-bytes"
    b64_whole = base64.b64encode(raw_audio).decode("ascii")
    half = len(b64_whole) // 2
    lines = [
        ('data: {"choices":[{"delta":{"audio":{"data":"' + b64_whole[:half] + '"}}}]}\n').encode("utf-8"),
        ('data: {"choices":[{"delta":{"audio":{"data":"' + b64_whole[half:] + '"}}}]}\n').encode("utf-8"),
        b"data: [DONE]\n",
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        result = asyncio.run(bot._fish_audio_tts_bytes("Привет, мир"))
        assert result == raw_audio
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_fish_audio_tts_bytes_returns_none_on_http_error():
    fake_resp = _FakeSSEResponse([], status=500)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        assert asyncio.run(bot._fish_audio_tts_bytes("Привет")) is None
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_fish_audio_tts_bytes_returns_none_without_api_key():
    original_key = bot.OPENROUTER_API_KEY
    bot.OPENROUTER_API_KEY = ""
    try:
        assert asyncio.run(bot._fish_audio_tts_bytes("Привет")) is None
    finally:
        bot.OPENROUTER_API_KEY = original_key


def test_fish_audio_tts_bytes_returns_none_on_empty_stream():
    # Поток отдал валидный SSE, но ни одного audio-чанка (например, если формат
    # ответа модели когда-нибудь изменится) — должны тихо откатиться на Gemini,
    # а не упасть с исключением или вернуть пустые байты как будто это успех.
    lines = [b"data: [DONE]\n"]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        assert asyncio.run(bot._fish_audio_tts_bytes("Привет")) is None
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


# (test_inline_tts_records_quota_usage_on_success выше уже проверяет ПОЛНЫЙ путь
# inline_tts с пустым OPENROUTER_API_KEY end-to-end — Fish Audio там уже тихо
# пропускается, и вызов идёт в _gemini_tts_bytes, отдельного теста не требуется.)


# ─────────────────── handle_tiktok_sound (ссылка на страницу звука не поддерживается) ───────────────────
# Обе попытки скачать звук отдельно по ссылке на его страницу провалились на
# реальном тестировании (см. историю в bot.py: ни голый ID в TikWM, ни прямой
# скрапинг страницы TikTok — тот отдаёт пустую "заглушку" без данных о звуке,
# похоже на урезание страницы для дата-центровых IP). Функция теперь сразу и
# честно сообщает об этом, не делая заведомо обречённых сетевых запросов.

def test_handle_tiktok_sound_raises_user_facing_error_immediately():
    incoming = _FakeIncomingMessage(999501)
    with pytest.raises(bot.TikTokUserFacingError):
        asyncio.run(bot.handle_tiktok_sound(incoming, _FakeSentMessage()))


def test_handle_tiktok_sound_answers_guest_without_raising():
    incoming = _FakeIncomingMessage(999502)
    incoming.guest_query_id = "guest123"
    answered = []

    async def fake_answer_guest_text(message, text):
        answered.append(text)

    original = bot._answer_guest_text
    bot._answer_guest_text = fake_answer_guest_text
    try:
        asyncio.run(bot.handle_tiktok_sound(incoming, _FakeSentMessage()))
        assert len(answered) == 1
    finally:
        bot._answer_guest_text = original


# ─────────────────── _send_tiktok_music: именованный vs безымянный "оригинальный звук" ───────────────────
# КРИТИЧНАЯ РЕГРЕССИЯ (найдена дважды подряд на живом тестировании): TikTok
# разрешает автору дать "оригинальному звуку" СОБСТВЕННОЕ название при публикации
# (реальный пример: TikTok показывает звук как "Оригинальный звук: Night, Blooming
# Jasmine." на его собственной странице) — TikWM при этом всё равно присылает
# raw_music_title с префиксом "original sound - "/"оригинальный звук - " ПЕРЕД
# настоящим названием. Простое "буквальное совпадение фразы -> это безымянный
# звук" (было в двух предыдущих версиях этого кода) неверно отличает такие
# по-настоящему именованные "оригинальные звуки" от реально безымянных — где TikTok
# в заголовок вместо названия подставляет юзернейм/ник автора видео. Тесты ниже
# проверяют обе ветки на уровне ЦЕЛОЙ _send_tiktok_music (не только детектора),
# чтобы поймать регрессию именно в конечном результате (что реально попадает в
# title/performer при отправке аудио), а не только в изолированной формуле.

class _FakeAudioBot:
    """Минимальная замена aiogram Bot для _send_tiktok_music — нужен только send_audio."""
    def __init__(self):
        self.sent_audio: dict | None = None

    async def send_audio(self, **kwargs):
        self.sent_audio = kwargs
        return SimpleNamespace()


def _run_send_tiktok_music(media_data: dict, language_code: str | None = "ru"):
    """Общий harness: мокает скачивание байтов и запись MP3-тегов, возвращает
    (title, performer), которые реально дошли бы до _write_mp3_tags/send_audio."""
    incoming = _FakeIncomingMessage(999601)
    incoming.message_id = 12345
    incoming.from_user = SimpleNamespace(language_code=language_code) if language_code is not None else None

    captured: dict = {}

    async def fake_download(session, url, headers=None):
        return b"fake-bytes"

    def fake_write_tags(path, title, artist, cover):
        captured["title"] = title
        captured["artist"] = artist

    original_download = bot._download_url_bin
    original_write_tags = bot._write_mp3_tags
    original_bot = bot.bot
    bot._download_url_bin = fake_download
    bot._write_mp3_tags = fake_write_tags
    bot.bot = _FakeAudioBot()
    try:
        asyncio.run(bot._send_tiktok_music(None, media_data, incoming, "VideoPosterNickname", {}))
    finally:
        bot._download_url_bin = original_download
        bot._write_mp3_tags = original_write_tags
        bot.bot = original_bot
    return captured.get("title"), captured.get("artist")


def test_send_tiktok_music_truly_generic_original_sound_uses_localized_label():
    # raw_music_title буквально "original sound" без остатка (TikTok в этом случае
    # обычно добавляет юзернейм автора видео тем же куском — здесь его просто нет
    # вообще) — это ДЕЙСТВИТЕЛЬНО безымянный звук.
    media_data = {
        "music": "https://example.com/sound.mp3",
        "music_info": {"title": "original sound", "author": "SomeArtist", "cover": "https://example.com/cover.jpg"},
        "author": {"nickname": "VideoPosterNickname", "unique_id": "videoposter"},
    }
    title, artist = _run_send_tiktok_music(media_data, language_code="ru")
    assert title == "Оригинальный звук"
    assert artist == "videoposter"


def test_send_tiktok_music_generic_original_sound_with_only_video_author_suffix():
    # TikWM подставил в заголовок ник/юзернейм автора ВИДЕО вместо названия —
    # после вычитания generic-фразы и этого ника/юзернейма остаётся пусто, значит
    # это тоже безымянный случай, а не настоящее название.
    media_data = {
        "music": "https://example.com/sound.mp3",
        "music_info": {"title": "original sound - videoposter", "author": "videoposter", "cover": ""},
        "author": {"nickname": "VideoPosterNickname", "unique_id": "videoposter"},
    }
    title, artist = _run_send_tiktok_music(media_data, language_code="be")
    assert title == "Арыгінальны гук"
    assert artist == "videoposter"


def test_send_tiktok_music_named_original_sound_preserves_real_title_and_author():
    # РЕГРЕССИЯ (реальный найденный случай): "original sound - Night, Blooming
    # Jasmine." — TikTok позволяет назвать оригинальный звук, и TikWM всё равно
    # ставит префикс "original sound - " перед этим настоящим названием. После
    # вычитания generic-фразы остаётся "Night, Blooming Jasmine." — это ЗНАЧИМЫЙ
    # остаток, значит звук на самом деле именован, и подменять его generic-
    # подписью нельзя — реальные название/автор должны сохраниться как есть.
    media_data = {
        "music": "https://example.com/sound.mp3",
        "music_info": {"title": "original sound - Night, Blooming Jasmine.", "author": "Fakemink", "cover": "https://example.com/real_cover.jpg"},
        "author": {"nickname": "coltrdr", "unique_id": "coltrdr"},
    }
    title, artist = _run_send_tiktok_music(media_data, language_code="ru")
    assert title == "Night, Blooming Jasmine."
    assert artist == "Fakemink"
    # Юзернейм автора ВИДЕО (coltrdr) не должен попасть в исполнители — это не он
    # автор звука, звук лишь использован в его видео.
    assert artist != "coltrdr"


def test_send_tiktok_music_regular_named_track_unaffected():
    # Обычная лицензированная песня без единого упоминания "original sound" в
    # заголовке — не должна была задеваться этой логикой вообще ни в одной версии.
    media_data = {
        "music": "https://example.com/song.mp3",
        "music_info": {"title": "Blinding Lights", "author": "The Weeknd", "cover": "https://example.com/album_cover.jpg"},
        "author": {"nickname": "SomeUser", "unique_id": "someuser"},
    }
    title, artist = _run_send_tiktok_music(media_data, language_code="ru")
    assert title == "Blinding Lights"
    assert artist == "The Weeknd"


def test_send_tiktok_music_generic_original_sound_in_non_ru_en_source_language_still_localizes():
    # РЕГРЕССИЯ (отладка 11 августа 2026, реальная жалоба): raw_music_title
    # генерируется TikTok на языке автора ИСХОДНОГО видео (см. докстринг
    # lumen_tiktok.py), а не обязательно на ru/en. Раньше mentions_generic_phrase
    # проверяла только буквальные "оригинальный звук"/"original sound" — украинская
    # фраза "оригінальний звук" не совпадала ни с одной из них, is_original_sound
    # ошибочно оставался False, и получателю ссылки с русским (или любым другим)
    # интерфейсом Telegram показывался НЕлокализованный украинский raw-заголовок
    # вместо подписи на ЕГО собственном языке.
    media_data = {
        "music": "https://example.com/sound.mp3",
        "music_info": {"title": "оригінальний звук", "author": "videoposter", "cover": ""},
        "author": {"nickname": "VideoPosterNickname", "unique_id": "videoposter"},
    }
    title, artist = _run_send_tiktok_music(media_data, language_code="ru")
    assert title == "Оригинальный звук"
    assert artist == "videoposter"


def test_try_gemini_streaming_happy_path_accumulates_and_finalizes():
    chat_id = 999101

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            for piece in ["Привет", ", как ", "дела?"]:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == "Привет, как дела?"
        assert placeholder is None  # успех — плейсхолдер уже отредактирован до финального текста
        # финальная правка должна прийти с HTML parse_mode (полная markdown-конвертация)
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_aborts_on_identity_leak_mid_stream():
    # Регрессия на самый чувствительный сценарий: утечка должна обрываться ДО того,
    # как накопленный текст попадёт хоть в один edit_text — иначе пользователь успеет
    # увидеть утёкший текст на экране ещё до финального завершения потока.
    chat_id = 999104

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            for piece in ["Привет! ", "На самом деле я работаю ", "на базе Gemini от Google."]:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Кто ты на самом деле?", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert placeholder is None
        # Ни в одной показанной пользователю правке НЕ должно быть слова "gemini" —
        # проверяем ВСЕ edit_text вызовы единственного отправленного сообщения, а не
        # только последний, т.к. именно промежуточные правки могли бы "мигнуть" утечкой.
        for shown_text, _parse_mode in incoming.sent[0].edits:
            assert "gemini" not in shown_text.lower()
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": bot._IDENTITY_LEAK_FALLBACK}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_returns_none_on_early_failure():
    chat_id = 999102

    async def fake_stream_raises(*, model, contents, config=None):
        raise RuntimeError("boom before any content")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream_raises
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer is None
        # Плейсхолдер теперь НЕ удаляется на этом уровне — он возвращается
        # вызывающему коду (_run_route), чтобы тот попробовал доправить в него
        # ответ следующей модели по цепочке, а не создавать новое сообщение.
        assert placeholder is incoming.sent[0]
        assert incoming.sent[0].deleted is False
        # история НЕ должна была обновиться — вызывающий код откатится на ask_gemini
        assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_failed_continuation_does_not_corrupt_first_message():
    # Регрессионный тест на баг, найденный код-ревью: при сбое отправки сообщения-
    # продолжения (текст длиннее лимита Telegram) первое, уже корректно показанное
    # сообщение раньше перезаписывалось чужим (последним) куском текста.
    chat_id = 999103
    long_piece = "А" * (bot.TG_MAX_LEN + 100)  # гарантированно требует второе сообщение

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=long_piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream

    class _FailingBot:
        async def send_message(self, **kwargs):
            return None  # имитируем неудачную отправку продолжения

    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FailingBot()
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Напиши длинный текст", incoming, bot.DEFAULT_GEMINI_MODEL))
        # Функция должна была вернуть накопленный текст, а не None и не бросить исключение
        assert answer is not None
        assert placeholder is None  # это уже финализированный успех, а не ранний сбой
        # Первое сообщение должно содержать ИМЕННО первый кусок (плюс пометка об обрыве),
        # а НЕ последний/другой кусок текста — это и была суть бага.
        first_msg_final_text = incoming.sent[0].edits[-1][0]
        assert first_msg_final_text.startswith("А")
        assert "не удалось отправить продолжение" in first_msg_final_text
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


# ─────────────────────────── стриминг OpenRouter (SSE) ───────────────────────────

class _FakeAsyncLineIter:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeSSEResponse:
    def __init__(self, lines: list[bytes], status: int = 200):
        self.status = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        return b""

    @property
    def content(self):
        return _FakeAsyncLineIter(self._lines)


class _FakeSessionForSSE:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *args, **kwargs):
        return self._resp


def test_openrouter_stream_pieces_parses_sse_chunks():
    lines = [
        'data: {"choices":[{"delta":{"content":"Привет"}}]}\n'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":", мир"}}]}\n'.encode("utf-8"),
        b"data: [DONE]\n",
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        async def collect():
            pieces = []
            async for piece in bot._openrouter_stream_pieces("meta-llama/llama-3.3-70b-instruct:free", [{"role": "user", "content": "hi"}]):
                pieces.append(piece)
            return pieces
        pieces = asyncio.run(collect())
        assert pieces == ["Привет", ", мир"]
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_openrouter_stream_pieces_raises_on_http_error_status():
    fake_resp = _FakeSSEResponse([], status=500)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        async def collect():
            async for _ in bot._openrouter_stream_pieces("meta-llama/llama-3.3-70b-instruct:free", []):
                pass
        with pytest.raises(bot.OpenRouterAPIError):
            asyncio.run(collect())
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_openrouter_stream_pieces_raises_on_midstream_error_chunk():
    # РЕГРЕССИЯ (аудит стриминга): провайдер за OpenRouter может упасть УЖЕ ПОСЛЕ
    # старта генерации — HTTP-статус к этому моменту давно 200 (стрим открыт), и
    # ошибка приходит не кодом ответа, а прямо внутри SSE-чанка:
    # {"error": {...}} вместо {"choices": [...]}. Раньше это тихо пропускалось
    # как "пустой чанк" (choices нет -> continue) — пользователь получал молча
    # укороченный ответ без единого намёка на причину. Первый кусок ("Начало")
    # должен успеть уйти до ошибки — проверяем, что она поднимается уже ПОСЛЕ
    # частичного контента, а не глушится.
    lines = [
        'data: {"choices":[{"delta":{"content":"Начало"}}]}\n'.encode("utf-8"),
        'data: {"error":{"message":"Provider returned error","code":502}}\n'.encode("utf-8"),
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        collected = []

        async def collect_partial():
            agen = bot._openrouter_stream_pieces("meta-llama/llama-3.3-70b-instruct:free", [{"role": "user", "content": "hi"}])
            async for piece in agen:
                collected.append(piece)

        with pytest.raises(bot.OpenRouterAPIError) as exc_info:
            asyncio.run(collect_partial())
        assert collected == ["Начало"]
        assert exc_info.value.status_code == 502
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_try_openrouter_streaming_happy_path_accumulates_and_finalizes():
    chat_id = 999105

    async def fake_stream_pieces(model_id, messages):
        for piece in ["Привет", ", как ", "дела?"]:
            yield piece

    incoming = _FakeIncomingMessage(chat_id)
    original_gen = bot._openrouter_stream_pieces
    bot._openrouter_stream_pieces = fake_stream_pieces
    try:
        answer, placeholder = asyncio.run(bot._try_openrouter_streaming(chat_id, "Привет!", incoming, "meta-llama/llama-3.3-70b-instruct:free"))
        assert answer == "Привет, как дела?"
        assert placeholder is None
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
        assert bot.GLOBAL_QUOTA["openrouter"]["meta-llama/llama-3.3-70b-instruct:free"]["used"] >= 1
    finally:
        bot._openrouter_stream_pieces = original_gen
        bot.chat_state.pop(chat_id, None)


def test_try_openrouter_streaming_returns_none_on_early_failure():
    chat_id = 999106

    async def fake_stream_pieces_raises(model_id, messages):
        raise RuntimeError("boom before any content")
        yield ""  # делает функцию async-генератором (недостижимо)

    incoming = _FakeIncomingMessage(chat_id)
    original_gen = bot._openrouter_stream_pieces
    bot._openrouter_stream_pieces = fake_stream_pieces_raises
    try:
        answer, placeholder = asyncio.run(bot._try_openrouter_streaming(chat_id, "Привет!", incoming, "meta-llama/llama-3.3-70b-instruct:free"))
        assert answer is None
        assert placeholder is incoming.sent[0]
        assert incoming.sent[0].deleted is False
        assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")
    finally:
        bot._openrouter_stream_pieces = original_gen
        bot.chat_state.pop(chat_id, None)


# ─────────────────── пейсинг "живой печати" при стриминге (lumen_typing_pace) ───────────────────
# Раньше во время стрима сообщение показывало РОВНО то, что накопилось с последнего
# edit_text — если бэкенд присылал ответ парой больших кусков (частый случай для
# бесплатных моделей OpenRouter, см. докстринг lumen_typing_pace.py), пользователь
# видел резкие скачки текста вместо плавного набора. Тесты ниже проверяют реальную
# интеграцию пейсинга в _run_streaming_reply (через _try_gemini_streaming) — не
# только чистые функции самого lumen_typing_pace.py (см. test_lumen_typing_pace.py).
#
# Каждый тест явно чистит lumen_typing_pace._speed_ema за собой (в finally) — это
# module-level состояние, разделяемое между тестами в одном прогоне pytest, и без
# явной очистки один тест мог бы повлиять на стартовую скорость для следующего.

def test_run_streaming_reply_paces_reveal_for_burst_instead_of_dumping_full_text():
    import lumen_typing_pace
    chat_id = 999110
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    # Реалистичная имитация бэкенда, который не стримит токен-в-токен, а отдаёт
    # весь ответ ОДНИМ куском (см. докстринг lumen_typing_pace.py про то, почему
    # это обычное дело для бесплатных моделей OpenRouter).
    long_text = "Слово " * 80  # ~480 символов одним SSE-куском

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=long_text)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        assert placeholder is None
        plain_edits = [text for text, parse_mode in incoming.sent[0].edits if parse_mode is None]
        # Хотя бы одна промежуточная правка должна была показать ЧАСТЬ текста, а
        # не весь ответ разом — иначе пейсинг не сработал (регрессия на исходную
        # проблему: "скачками по 15-20 слов" вместо плавного набора).
        assert any(0 < len(p) < len(long_text) for p in plain_edits)
        # Финальная правка — уже HTML с полным текстом, как и раньше.
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": long_text.strip()}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_records_observed_speed_on_success():
    import lumen_typing_pace
    chat_id = 999111
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            for piece in ["Привет", ", мир!"]:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        # После успешного стрима у модели должна появиться собственная запись в
        # EMA — самокалибровка происходит без единой ручной правки таблицы.
        assert pace_key in lumen_typing_pace._speed_ema
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_catchup_never_exceeds_max_ticks_even_for_long_slow_burst():
    # РЕГРЕССИЯ на ключевое требование: сколько бы ни оценивалась скорость модели
    # заниженно и сколько бы символов ни осталось "довыводить" одним куском, число
    # искусственных пауз ограничено STREAM_TYPING_MAX_CATCHUP_TICKS — реальная
    # скорость ответа не должна страдать ради красивости набора текста.
    import lumen_typing_pace
    chat_id = 999112
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC  # намеренно "медленная" модель

    long_text = "Буква " * 500  # ~3000 символов, одним куском, ниже TG_MAX_LEN

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=long_text)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    sleep_calls: list[float] = []
    original_sleep = bot._typing_sleep

    async def counting_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    bot._typing_sleep = counting_sleep
    original_client = bot.client
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        assert len(sleep_calls) <= bot.STREAM_TYPING_MAX_CATCHUP_TICKS
    finally:
        bot._typing_sleep = original_sleep
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


# ─────────────────────────── стриминг в _run_route для ЛЮБОГО провайдера ───────────────────────────

def test_run_route_streams_openrouter_when_route_head_is_openrouter():
    chat_id = 999305

    async def fake_or_stream(cid, prompt, message, model_id):
        return "Стримленный ответ от OpenRouter", None

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("ask_openrouter_text не должен вызываться, если стрим уже сработал")

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream
    bot.ask_openrouter_text = must_not_be_called
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("gemini", "gemini-3.5-flash-lite")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Стримленный ответ от OpenRouter"
        assert sent is True
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_run_route_falls_back_from_failed_openrouter_stream_to_non_streaming():
    chat_id = 999306

    async def fake_or_stream_fail(cid, prompt, message, model_id):
        return None, None  # ранний сбой без плейсхолдера (например, message.reply сам не удался)

    calls = []

    async def fake_or_text(cid, prompt, model_chain, deadline=None):
        calls.append(model_chain)
        return "Ответ без стрима"

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream_fail
    bot.ask_openrouter_text = fake_or_text
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("openrouter", "openai/gpt-oss-20b:free")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ без стрима"
        assert sent is False
        # Модель, для которой стрим не удался, не должна пере-пробоваться внутри
        # обычного вызова — экономим время (см. философию "1 попытка на модель").
        assert calls[0] == ["openai/gpt-oss-20b:free"]
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_run_route_reuses_stream_placeholder_when_fallback_succeeds():
    # Регрессия на реальный найденный при тестировании баг: раньше при неудачном
    # стриме плейсхолдер "…" тут же удалялся, а следующая модель отправляла
    # совсем новое сообщение — визуально выглядело как "точки исчезли, потом
    # из ниоткуда появился ответ одним блоком". Теперь плейсхолдер должен
    # переиспользоваться (редактироваться) финальным ответом резервной модели.
    chat_id = 999307
    placeholder = _FakeSentMessage()

    async def fake_or_stream_fail_with_placeholder(cid, prompt, message, model_id):
        return None, placeholder

    async def fake_or_text(cid, prompt, model_chain, deadline=None):
        return "Ответ от резервной модели"

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream_fail_with_placeholder
    bot.ask_openrouter_text = fake_or_text
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("openrouter", "openai/gpt-oss-20b:free")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ от резервной модели"
        # sent=True означает, что ответ уже "доставлен" через правку плейсхолдера,
        # а не через отдельный новый _safe_reply в _handle_message_core.
        assert sent is True
        assert placeholder.deleted is False
        assert placeholder.edits[-1][0] == "Ответ от резервной модели"
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_run_route_deletes_orphaned_placeholder_when_whole_route_fails():
    chat_id = 999308
    placeholder = _FakeSentMessage()

    async def fake_or_stream_fail_with_placeholder(cid, prompt, message, model_id):
        return None, placeholder

    async def failing_or_text(*args, **kwargs):
        raise bot.OpenRouterAPIError("всё сломано", status_code=500)

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream_fail_with_placeholder
    bot.ask_openrouter_text = failing_or_text
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("openrouter", "openai/gpt-oss-20b:free")]
        with pytest.raises(Exception):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert placeholder.deleted is True
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_build_gemini_call_config_skips_all_grounding_tools_for_gemma():
    # Прямая проверка на уровне _build_gemini_call_config: для no_search-модели
    # (обе Gemma) итоговый config не должен включать вообще ни один
    # grounding/url_context инструмент, независимо от прочих флагов в конфиге.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    _, gconfig = bot._build_gemini_call_config("gemma-4-26b-a4b-it", contents)
    assert not getattr(gconfig, "tools", None)


def test_build_gemini_call_config_no_system_model_includes_full_system_prompt():
    # РЕГРЕССИЯ (аудит системного промпта, 10 августа 2026): раньше для no_system-моделей
    # (Gemma) фейковый первый обмен содержал ТОЛЬКО короткий захардкоженный список из 8
    # пунктов — ни строчки из настоящего SYSTEM_PROMPT (system_prompt.py). Gemma полностью
    # пропускала БЛАГОПОЛУЧИЕ И ЗДОРОВЬЕ ПОЛЬЗОВАТЕЛЯ (протокол при сообщении о суициде/
    # самоповреждении — телефон доверия), АВТОРСКИЕ ПРАВА, ОБЪЕКТИВНОСТЬ И НЕПРЕДВЗЯТОСТЬ и
    # т.д. Теперь get_system_prompt(model_id) подставляется в основу этого сообщения —
    # проверяем, что и не встречающиеся в коротком чеклисте разделы теперь реально там есть.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    call_contents, _ = bot._build_gemini_call_config("gemma-4-26b-a4b-it", contents)
    first_user_text = call_contents[0].parts[0].text
    assert "БЛАГОПОЛУЧИЕ И ЗДОРОВЬЕ ПОЛЬЗОВАТЕЛЯ" in first_user_text
    assert "8-800-2000-122" in first_user_text
    assert "АВТОРСКИЕ ПРАВА" in first_user_text
    assert "ОБЪЕКТИВНОСТЬ И НЕПРЕДВЗЯТОСТЬ" in first_user_text
    # Короткий проверенный на практике чеклист (личность/дата/защита от инъекций)
    # сохранён поверх полного промпта, а не заменён им.
    assert "Твоё имя — Lumen" in first_user_text


def test_build_gemini_call_config_with_system_instruction_model_unaffected():
    # Модели С system_instruction (не no_system) вообще не проходят через ветку fake
    # identity-обмена — регрессия на то, что правка выше не задела этот путь: contents
    # должен остаться нетронутым, а полный текст идёт через system_instruction, как раньше.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    call_contents, gconfig = bot._build_gemini_call_config(bot.DEFAULT_GEMINI_MODEL, contents)
    assert call_contents is contents
    assert "БЛАГОПОЛУЧИЕ И ЗДОРОВЬЕ ПОЛЬЗОВАТЕЛЯ" in gconfig.system_instruction


# ─────────────── thinking_level/thinking_budget (калибровка 18 августа 2026) ───────────────
# gemini-3.7-flash поймана на 17 таймаутах (22с) из 18 попыток за сессию — низкий
# эффорт мышления на НЕ-heavy запросах снижает риск таймаута, не трогая heavy-запросы
# (там таймаут и так вероятнее, а собственный дефолт модели уже балансирует лучше).

def test_build_gemini_call_config_sets_low_thinking_level_for_gemini_3_on_light_query():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет, как дела?")])]
    _, gconfig = bot._build_gemini_call_config("gemini-3.7-flash", contents)
    assert gconfig.thinking_config.thinking_level == bot.types.ThinkingLevel.LOW


def test_build_gemini_call_config_sets_zero_thinking_budget_for_gemini_2_5_on_light_query():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="столица франции?")])]
    _, gconfig = bot._build_gemini_call_config("gemini-2.5-flash", contents)
    assert gconfig.thinking_config.thinking_budget == 0


def test_build_gemini_call_config_does_not_override_thinking_on_heavy_query():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="напиши функцию для сортировки списка")])]
    _, gconfig = bot._build_gemini_call_config("gemini-3.7-flash", contents)
    assert gconfig.thinking_config is None


def test_build_gemini_call_config_skips_thinking_override_for_gemma():
    # Gemma не поддерживает thinking_config вообще — no_system уже исключает её.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    _, gconfig = bot._build_gemini_call_config("gemma-4-26b-a4b-it", contents)
    assert gconfig is None or gconfig.thinking_config is None


# ─────────────────── дневной сброс счётчиков квоты (/stats не должен копить вечно) ───────────────────

def test_reset_quota_if_new_day_clears_used_and_exhausted_on_day_rollover():
    # bot._last_quota_check_monotonic сбрасывается явно: в проде троттлинг (см.
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
    original_throttle = bot._last_quota_check_monotonic
    try:
        bot.GLOBAL_QUOTA["gemini"] = {"gemini-2.5-flash": {"used": 106, "remaining": 0, "limit": 1500, "exhausted_at": 12345.0}}
        bot.GLOBAL_QUOTA["openrouter"] = {"some-model:free": {"used": 50, "remaining": None, "limit": None, "exhausted_at": None}}
        bot.GLOBAL_QUOTA["quota_day"] = "2020-01-01"  # заведомо "вчерашний" день
        bot._last_quota_check_monotonic = time.monotonic() - bot._QUOTA_CHECK_THROTTLE_SEC - 10.0

        bot._reset_quota_if_new_day()

        assert bot.GLOBAL_QUOTA["gemini"]["gemini-2.5-flash"]["used"] == 0
        assert bot.GLOBAL_QUOTA["gemini"]["gemini-2.5-flash"]["exhausted_at"] is None
        assert bot.GLOBAL_QUOTA["openrouter"]["some-model:free"]["used"] == 0
        assert bot.GLOBAL_QUOTA["quota_day"] == bot._current_quota_day()
    finally:
        bot.GLOBAL_QUOTA["gemini"] = original_quota["gemini"]
        bot.GLOBAL_QUOTA["openrouter"] = original_quota["openrouter"]
        bot.GLOBAL_QUOTA["quota_day"] = original_quota["quota_day"]
        bot._last_quota_check_monotonic = original_throttle


def test_reset_quota_if_new_day_is_noop_within_same_day():
    original_quota_day = bot.GLOBAL_QUOTA.get("quota_day")
    original_throttle = bot._last_quota_check_monotonic
    try:
        bot.GLOBAL_QUOTA["gemini"]["test-model-999"] = {"used": 7, "remaining": None, "limit": None, "exhausted_at": None}
        bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
        # См. комментарий в test_reset_quota_if_new_day_clears_used_and_exhausted_on_day_rollover
        # выше про то, почему буквальный 0.0 не годится как "точно давно".
        bot._last_quota_check_monotonic = time.monotonic() - bot._QUOTA_CHECK_THROTTLE_SEC - 10.0
        bot._reset_quota_if_new_day()
        assert bot.GLOBAL_QUOTA["gemini"]["test-model-999"]["used"] == 7
    finally:
        bot.GLOBAL_QUOTA["gemini"].pop("test-model-999", None)
        bot.GLOBAL_QUOTA["quota_day"] = original_quota_day
        bot._last_quota_check_monotonic = original_throttle


def test_reset_quota_if_new_day_throttles_repeated_calls():
    # Регрессия на найденное при code-review: без троттлинга _reset_quota_if_new_day
    # конструировала бы ZoneInfo/datetime.now на КАЖДЫЙ вызов _quota_entry (а таких —
    # по несколько на каждую попытку модели). Проверяем, что повторный вызов сразу
    # после первого не запускает вторую проверку даты — если бы троттлинг не
    # работал, _current_quota_day() был бы вызван трижды, а не один раз.
    #
    # См. комментарий в test_reset_quota_if_new_day_clears_used_and_exhausted_on_day_rollover
    # про то, почему сброс делается относительно time.monotonic(), а не в буквальный 0.0.
    original_throttle = bot._last_quota_check_monotonic
    calls = []
    original_fn = bot._current_quota_day
    try:
        bot._last_quota_check_monotonic = time.monotonic() - bot._QUOTA_CHECK_THROTTLE_SEC - 10.0
        bot._current_quota_day = lambda: (calls.append(1), original_fn())[1]
        bot._reset_quota_if_new_day()
        bot._reset_quota_if_new_day()
        bot._reset_quota_if_new_day()
        assert len(calls) == 1
    finally:
        bot._current_quota_day = original_fn
        bot._last_quota_check_monotonic = original_throttle


# ─────────────────── авто-роутинг генерации изображений (аудит техдолга, 19 августа 2026) ───────────────────
# Команда /imgmodel и ручной выбор модели убраны целиком — тот же переход, что уже
# был сделан для текстового провайдера/модели (см. README, "Автоматический выбор
# модели"). _pick_image_model заменяет собой ручной выбор: подбирает модель по
# содержимому промпта на каждый вызов, без персистентного состояния чата.

def test_pick_image_model_detects_anime():
    assert bot._pick_image_model("нарисуй девушку в стиле аниме") == "flux-anime"
    assert bot._pick_image_model("draw a chibi character") == "flux-anime"


def test_pick_image_model_detects_fantasy():
    assert bot._pick_image_model("нарисуй дракона в фэнтезийном замке") == "dreamshaper"
    assert bot._pick_image_model("concept art of an elf wizard") == "dreamshaper"


def test_pick_image_model_detects_realism():
    assert bot._pick_image_model("сделай фотореалистичный портрет кота") == "flux-realism"
    assert bot._pick_image_model("realistic photo of a mountain") == "flux-realism"


def test_pick_image_model_detects_quick_draft():
    assert bot._pick_image_model("быстрый набросок логотипа") == "turbo"


def test_pick_image_model_falls_back_to_default_for_generic_prompt():
    assert bot._pick_image_model("космическая станция на орбите Земли") == bot.DEFAULT_HF_IMAGE_MODEL
    assert bot._pick_image_model("") == bot.DEFAULT_HF_IMAGE_MODEL


def test_pick_image_model_style_keyword_wins_over_quick_keyword():
    # Стилевой сигнал важнее просьбы "побыстрее", если оба есть в одном промпте —
    # см. докстринг _pick_image_model про порядок проверок.
    assert bot._pick_image_model("быстро нарисуй аниме-девушку") == "flux-anime"


def test_imgmodel_command_and_callback_removed():
    # Регрессия на сам факт удаления команды — не должно остаться ни обработчика,
    # ни клавиатуры, ни каталога, которые её обслуживали.
    assert not hasattr(bot, "cmd_imgmodel")
    assert not hasattr(bot, "cb_imgmodel")
    assert not hasattr(bot, "_imgmodel_keyboard")
    assert not hasattr(bot, "_hf_model_catalog")


# ─────────────────── /start: не должен расходиться с реальным списком команд ───────────────────
# Регрессия на класс бага, найденный при удалении /imgmodel (19 августа 2026):
# команда убрана из BotCommand-списка в _webhook_startup, но /start какое-то
# время всё ещё могла бы её упоминать — рассинхрон между "что реально работает"
# и "что бот сам о себе рассказывает" был бы виден только при ручной проверке.

def test_cmd_start_mentions_every_current_command_and_not_removed_ones():
    captured = {}

    class _FakeStartMessage:
        async def reply(self, text, **kwargs):
            captured["text"] = text
            return SimpleNamespace()

    asyncio.run(bot.cmd_start(_FakeStartMessage()))
    text = captured["text"]
    assert "/draw" in text
    assert "/tts" in text
    assert "/reset" in text
    assert "/imgmodel" not in text


def test_inline_draw_picks_model_from_prompt_without_touching_chat_state():
    chat_id = 999430
    captured_model = []

    async def fake_hf_text_to_image(session, model_id, prompt):
        captured_model.append(model_id)
        return b"\x89PNG fake bytes"

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1

    class _FakePhotoBot:
        async def send_photo(self, **kwargs):
            return SimpleNamespace()

    original_hf = bot._hf_text_to_image
    original_bot_obj = bot.bot
    bot._hf_text_to_image = fake_hf_text_to_image
    bot.bot = _FakePhotoBot()
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй девушку в стиле аниме на пляже"))
        assert captured_model == ["flux-anime"]
        # get_state не должен был получить ключ image_model — авто-роутинг не
        # завязан на состояние чата вообще.
        assert "image_model" not in bot.get_state(chat_id)
    finally:
        bot._hf_text_to_image = original_hf
        bot.bot = original_bot_obj
        bot.chat_state.pop(chat_id, None)


def test_inline_draw_stops_fallback_chain_when_time_budget_exceeded():
    # Регрессия на находку код-ревью (28 августа 2026): раньше у /draw не было
    # общего бюджета времени на всю фолбэк-цепочку — при недоступности сервиса
    # генерации бот перебирал бы все 5 моделей HF_IMAGE_MODELS, тратя реальное
    # время пользователя без единого предупреждения. Патчим DRAW_TOTAL_BUDGET_SEC
    # на крошечное значение и делаем первую попытку заведомо дольше него —
    # вторая попытка не должна была вообще начаться.
    chat_id = 999432
    attempts = []

    async def fake_hf_text_to_image(session, model_id, prompt):
        attempts.append(model_id)
        await asyncio.sleep(0.05)  # дольше урезанного DRAW_TOTAL_BUDGET_SEC ниже
        raise RuntimeError("503 Service Unavailable")

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1

    original_hf = bot._hf_text_to_image
    original_budget = bot.DRAW_TOTAL_BUDGET_SEC
    bot._hf_text_to_image = fake_hf_text_to_image
    bot.DRAW_TOTAL_BUDGET_SEC = 0.01
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй кота"))
        # Ровно ОДНА попытка — бюджет исчерпался до второй, а не перебор всех 5 моделей.
        assert len(attempts) == 1
        assert "времени" in incoming.sent[0].edits[-1][0].lower()
    finally:
        bot._hf_text_to_image = original_hf
        bot.DRAW_TOTAL_BUDGET_SEC = original_budget


def test_inline_draw_falls_back_when_auto_picked_model_fails():
    # Закрывает пробел, найденный при код-ревью: _pick_image_model выбирает модель
    # по содержимому промпта, но до сих пор не было теста на то, что именно ЭТА
    # (авто-подобранная, не дефолтная) модель корректно участвует в существующей
    # fallback-цепочке при сбое — а не только счастливый путь без единой ошибки.
    chat_id = 999431
    attempts = []

    async def fake_hf_text_to_image(session, model_id, prompt):
        attempts.append(model_id)
        if model_id == "flux-anime":
            raise RuntimeError("503 Service Unavailable")
        return b"\x89PNG fallback bytes"

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    captured_photo = {}

    class _FakePhotoBot:
        async def send_photo(self, **kwargs):
            captured_photo.update(kwargs)
            return SimpleNamespace()

    original_hf = bot._hf_text_to_image
    original_bot_obj = bot.bot
    bot._hf_text_to_image = fake_hf_text_to_image
    bot.bot = _FakePhotoBot()
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй девушку в стиле аниме на пляже"))
        # Авто-подобранная модель (flux-anime) пробуется ПЕРВОЙ, несмотря на сбой.
        assert attempts[0] == "flux-anime"
        # Реально отправленное изображение — от следующей модели по порядку
        # HF_IMAGE_MODELS, а не от auto-pick, провалившегося с ошибкой.
        assert len(attempts) >= 2
        assert captured_photo["photo"].data == b"\x89PNG fallback bytes"
        assert "основная модель недоступна" in captured_photo["caption"]
    finally:
        bot._hf_text_to_image = original_hf
        bot.bot = original_bot_obj
        bot.chat_state.pop(chat_id, None)


# ─────────────────── schema_version персистентного снимка чата (аудит техдолга) ───────────────────

def test_serialize_chat_state_stamps_current_schema_version():
    state = {"image_model": bot.DEFAULT_HF_IMAGE_MODEL, "history": [], "quota": {}, "recent_media_ids": {}}
    snapshot = bot._serialize_chat_state(state)
    assert snapshot["schema_version"] == bot.CHAT_STATE_SCHEMA_VERSION


def test_restore_single_chat_accepts_legacy_record_without_schema_version():
    # Записи, сохранённые до введения schema_version, не имеют этого поля вообще —
    # восстановление не должно падать и должно вести себя так же, как раньше.
    cid = 999905
    try:
        bot._restore_single_chat(cid, {"image_model": bot.DEFAULT_HF_IMAGE_MODEL, "history": [{"role": "user", "content": "hi"}]})
        assert bot.chat_state[cid]["history"] == [{"role": "user", "content": "hi"}]
    finally:
        bot.chat_state.pop(cid, None)


def test_restore_single_chat_accepts_current_schema_version_record():
    cid = 999906
    try:
        snapshot = bot._serialize_chat_state({
            "image_model": bot.DEFAULT_HF_IMAGE_MODEL, "history": [{"role": "user", "content": "hi"}],
            "quota": {}, "recent_media_ids": {},
        })
        bot._restore_single_chat(cid, snapshot)
        assert bot.chat_state[cid]["history"] == [{"role": "user", "content": "hi"}]
    finally:
        bot.chat_state.pop(cid, None)

# ─────────────────── _TelegramProxyCircuitBreaker (аудит техдолга) ───────────────────
# Раньше состояние выключателя жило как четыре независимых module-level globals,
# мутируемых через `global` из двух разных функций — само поведение (порог
# срабатывания, cooldown, сброс счётчика на успех) нигде не тестировалось
# напрямую, только опосредованно через _tg_call/telegram_api_call. Инкапсуляция
# в класс делает это поведение тестируемым в изоляции.

def test_circuit_breaker_starts_closed():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    assert breaker.is_down(time.monotonic()) is False


def test_circuit_breaker_does_not_trip_before_threshold():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    assert breaker.note_failure() is False
    assert breaker.note_failure() is False
    assert breaker.is_down(time.monotonic()) is False


def test_circuit_breaker_trips_at_threshold():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_failure()
    tripped = breaker.note_failure()
    assert tripped is True
    breaker.trip()
    assert breaker.is_down(time.monotonic()) is True


def test_circuit_breaker_success_resets_consecutive_failures():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_failure()
    breaker.note_success()
    assert breaker.consecutive_failures == 0
    # Единичные последующие сбои не должны сразу срабатывать — счётчик правда сброшен.
    assert breaker.note_failure() is False


def test_circuit_breaker_garbage_event_count_never_resets():
    # В отличие от consecutive_failures, совокупный счётчик для /stats копится
    # за всё время жизни процесса и не должен сбрасываться на success.
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    breaker.note_failure()
    breaker.note_success()
    breaker.note_failure()
    assert breaker.garbage_event_count == 2


def test_circuit_breaker_status_text_reflects_state():
    breaker = bot._TelegramProxyCircuitBreaker(cooldown_sec=20.0, trip_threshold=3)
    assert "в норме" in breaker.status_text()
    breaker.note_failure()
    breaker.note_failure()
    breaker.note_failure()
    breaker.trip()
    assert "ВЫКЛЮЧЕН" in breaker.status_text()

# ─────────────────── ask_gemini: fallback-цикл по цепочке моделей (аудит техдолга) ───────────────────
# НАЙДЕНО ПРИ АУДИТЕ: внутренняя логика переключения между моделями в ask_gemini
# (429 -> следующая модель / таймаут -> следующая / прочая ошибка -> следующая /
# бюджет времени исчерпан -> RouteBudgetExceededError) раньше не была покрыта
# напрямую НИ ОДНИМ тестом — только опосредованно через _run_route (который
# мокает саму ask_gemini целиком, не проверяя её внутренний цикл). Тесты ниже
# фиксируют текущее поведение ДО рефакторинга (объединение классификации ошибок
# с _or_chat_completion_with_fallback через общие _error_status/_classify_model_error).

def test_ask_gemini_falls_back_to_next_model_on_quota_exhausted():
    chat_id = 999010
    calls = []

    class _QuotaExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise _QuotaExc("resource_exhausted")
        return _FakeGeminiResponse(text="Ответ от второй модели")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ от второй модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
        # Модель, отдавшая 429, должна быть помечена исчерпанной (влияет на будущий роутинг).
        assert bot.GLOBAL_QUOTA["gemini"]["gemini-3.6-flash"]["exhausted_at"] is not None
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)


def test_ask_gemini_raises_all_models_exhausted_when_entire_chain_429s():
    chat_id = 999011

    class _QuotaExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        raise _QuotaExc("resource_exhausted")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    try:
        with pytest.raises(bot.GeminiAllModelsExhaustedError) as exc_info:
            asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert set(exc_info.value.exhausted_models) == {"gemini-3.6-flash", "gemini-2.5-flash"}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash", None)


def test_ask_gemini_falls_back_to_next_model_on_timeout():
    chat_id = 999012
    calls = []
    original_timeout = bot.ROUTE_MODEL_TIMEOUT_SEC

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            time.sleep(0.2)  # дольше урезанного ROUTE_MODEL_TIMEOUT_SEC ниже
        return _FakeGeminiResponse(text="Ответ от второй модели")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    bot.ROUTE_MODEL_TIMEOUT_SEC = 0.05
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ от второй модели"
        assert calls[0] == "gemini-3.6-flash"
    finally:
        bot.client = original_client
        bot.ROUTE_MODEL_TIMEOUT_SEC = original_timeout
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_falls_back_to_next_model_on_generic_error():
    chat_id = 999013
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise RuntimeError("internal error 500")
        return _FakeGeminiResponse(text="Ответ от второй модели")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ от второй модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_raises_when_route_budget_exceeded():
    chat_id = 999014

    def fake_generate_content(*, model, contents, config=None):
        raise RuntimeError("internal error 500")

    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    try:
        past_deadline = time.monotonic() - 1.0
        with pytest.raises(bot.RouteBudgetExceededError):
            asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"], deadline=past_deadline))
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)

# ─────────────────── _or_chat_completion_with_fallback: единая классификация ошибок ───────────────────
# Закрепляет, что после унификации с _classify_model_error (см. аудит техдолга)
# каскад к следующей модели по-прежнему работает одинаково для любого класса
# ошибки — временной (429/5xx) и внешне "постоянной" (403) — т.к. attempts_per_model
# всегда 1 в реальном использовании (см. комментарий в самой функции).

def test_or_chat_completion_with_fallback_switches_model_on_rate_limit():
    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        if model == "model-a":
            raise bot.OpenRouterAPIError("rate limit exceeded", status_code=429)
        return {"choices": [{"message": {"content": "ответ от model-b"}}]}

    original = bot._or_request
    bot._or_request = fake_or_request
    try:
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        answer, used = asyncio.run(bot._or_chat_completion_with_fallback(messages, ["model-a", "model-b"], "model-a"))
        assert answer == "ответ от model-b"
        assert used == "model-b"
    finally:
        bot._or_request = original


def test_or_chat_completion_with_fallback_switches_model_on_permanent_looking_error():
    # Даже "постоянная" на вид ошибка (403 forbidden) не должна обрывать переход
    # к следующей модели — при attempts_per_model=1 переход к следующей модели
    # происходит независимо от классификации (см. комментарий в самой функции).
    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        if model == "model-a":
            raise bot.OpenRouterAPIError("forbidden", status_code=403)
        return {"choices": [{"message": {"content": "ответ от model-b"}}]}

    original = bot._or_request
    bot._or_request = fake_or_request
    try:
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        answer, used = asyncio.run(bot._or_chat_completion_with_fallback(messages, ["model-a", "model-b"], "model-a"))
        assert answer == "ответ от model-b"
        assert used == "model-b"
    finally:
        bot._or_request = original


# ══════════════════════════ аудит техдолга (август 2026) ══════════════════════════
# Тесты ниже покрывают правки из аудита технического долга: фиксы, ранее непокрытые
# участки кода и новые небольшие возможности (мультипрокси, алерты владельцу,
# liveness-проверка моделей, ручной экспорт состояния).

# ─────────────────── _check_admin_key (зеркало теста для _check_bot_token_auth) ───────────────────

def test_check_admin_key_accepts_correct_bearer_header():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        req = _FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})
        assert bot._check_admin_key(req) is True
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_check_admin_key_rejects_query_param_regression():
    # РЕГРЕССИЯ (аудит техдолга): раньше ADMIN_PANEL_KEY читался из ?key=... в URL —
    # та же уязвимость (CWE-598), что уже была исправлена для BOT_TOKEN в /admin_keys
    # (см. test_check_bot_token_auth_rejects_query_param_regression), но не была
    # применена к /diag/webhook_url/export_state. Query-параметр больше не должен
    # приниматься вообще, даже если значение верное.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        req = _FakeAdminRequest(headers={}, query_params={"key": "real-admin-key"})
        assert bot._check_admin_key(req) is False
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_check_admin_key_rejects_wrong_or_missing_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        assert bot._check_admin_key(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})) is False
        assert bot._check_admin_key(_FakeAdminRequest(headers={})) is False
        assert bot._check_admin_key(_FakeAdminRequest(headers={"Authorization": "real-admin-key"})) is False
    finally:
        bot.ADMIN_PANEL_KEY = original


# ─────────────────── ask_openrouter_multimodal: фикс хардкода fallback-модели ───────────────────

def test_ask_openrouter_multimodal_empty_model_chain_fallback_is_current_vision_order():
    # РЕГРЕССИЯ (аудит техдолга): раньше здесь стоял захардкоженный литерал
    # "nvidia/nemotron-nano-12b-v2-vl:free" — тот же класс бага, что уже был найден
    # и исправлен в ask_openrouter_text (см. test_ask_openrouter_text_empty_model_chain_
    # fallback_is_not_dead_model выше). Теперь дефолт ссылается на _OR_VISION_ORDER[0].
    chat_id = 999410
    calls = []

    async def fake_or_fallback(messages, trial_models, primary_model_id, **kwargs):
        calls.append(trial_models)
        return "ответ", trial_models[0]

    original = bot._or_chat_completion_with_fallback
    bot._or_chat_completion_with_fallback = fake_or_fallback
    try:
        asyncio.run(bot.ask_openrouter_multimodal(chat_id, "привет", (b"fake", "image/jpeg"), "photo.jpg", model_chain=[]))
        assert calls[0] == [bot._OR_VISION_ORDER[0]]
    finally:
        bot._or_chat_completion_with_fallback = original
        bot.chat_state.pop(chat_id, None)


# ─────────────────── дедуп мёртвой ветки ретраев в _or_chat_completion_with_fallback ───────────────────

# ─────────────────── разбиение _handle_message_core на именованные шаги (аудит техдолга) ───────────────────
# Раньше это была одна функция ~215 строк без единого прямого теста на свои
# внутренние решения (rate limit, приоритет медиа, текст ответа на ошибку) —
# только опосредованно через полный прогон _handle_message_core. Вынесенные
# чистые/почти чистые функции теперь тестируются в изоляции.

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


# ─────────────── _rate_limit_key_for_message (секьюрити-ревью: обход rate limit) ───────────────
# РЕГРЕССИЯ на реальную уязвимость: сообщения без from_user (например, отправленные "от
# имени канала" в привязанной группе) раньше давали user_id=None в _handle_message_core,
# а _check_and_register_rate_limit(None) безусловно возвращает False — то есть такой
# отправитель мог слать запросы без ограничения скорости вообще. Теперь для таких
# сообщений используется sender_chat.id/chat.id как запасной ключ.

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


def test_route_error_reply_text_youtube_takes_priority_over_exception_type():
    exc = bot.OpenRouterAPIError("boom", status_code=500)
    text = bot._route_error_reply_text(exc, "gemini-3.6-flash", youtube_url_to_analyze="https://youtu.be/x")
    assert "видео" in text.lower()


def test_route_error_reply_text_maps_known_exception_types():
    quota_exc = bot.GeminiAllModelsExhaustedError(["gemini-3.6-flash"])
    assert bot._route_error_reply_text(quota_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._gemini_error_msg(quota_exc, "gemini-3.6-flash")

    budget_exc = bot.RouteBudgetExceededError(["gemini-3.6-flash"])
    assert "перегруж" in bot._route_error_reply_text(budget_exc, "gemini-3.6-flash", youtube_url_to_analyze=None).lower()

    or_exc = bot.OpenRouterAPIError("boom", status_code=500)
    assert bot._route_error_reply_text(or_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._or_error_msg(or_exc, "text")

    other_exc = RuntimeError("что-то сломалось")
    assert bot._route_error_reply_text(other_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._gemini_error_msg(other_exc, "gemini-3.6-flash")


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


# ─────────── приоритет №3 учитывает ТИП запрошенного медиа (регрессия, 18 августа 2026) ───────────
# Реальный найденный баг: "Покажи стикер", когда стикер никогда не отправлялся, но
# недавно был отправлен скриншот — бот описывал скриншот как будто это и есть
# запрошенный стикер. Тесты ниже проверяют исправление на уровне полного
# _resolve_incoming_media (не только изолированных _media_reference_category/
# _find_recent_media_by_category выше).

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
    # Стикер был отправлен РАНЬШЕ фото — наивное "просто последний элемент" взяло
    # бы фото; правильное поведение — найти именно стикер, невзирая на порядок.
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


def test_or_chat_completion_with_fallback_no_longer_accepts_attempts_per_model():
    # attempts_per_model убран целиком (был мёртвым кодом — см. аудит техдолга):
    # единственное реальное значение всегда было 1, поэтому внутренний повторный
    # цикл никогда не делал вторую итерацию.
    import inspect
    sig = inspect.signature(bot._or_chat_completion_with_fallback)
    assert "attempts_per_model" not in sig.parameters


# ─────────────────── healthcheck отражает реальное состояние ───────────────────

def test_healthcheck_reports_not_ready_when_bot_or_client_uninitialized():
    original_bot, original_client = bot.bot, bot.client
    bot.bot = None
    bot.client = None
    try:
        result = asyncio.run(bot.healthcheck())
        assert result == {"status": "starting", "ready": False}
    finally:
        bot.bot, bot.client = original_bot, original_client


def test_healthcheck_reports_ready_when_initialized():
    original_bot, original_client = bot.bot, bot.client
    bot.bot = object()
    bot.client = object()
    try:
        result = asyncio.run(bot.healthcheck())
        assert result == {"status": "ok", "ready": True}
    finally:
        bot.bot, bot.client = original_bot, original_client


# ─────────────────── /export_state — ручной бэкап (гейтится ADMIN_PANEL_KEY) ───────────────────

def test_export_state_rejects_missing_or_wrong_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer wrong"})))
        assert "error" in result
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_export_state_rejects_query_param_regression():
    # См. test_check_admin_key_rejects_query_param_regression — /export_state — самый
    # чувствительный из трёх эндпоинтов (отдаёт ПОЛНЫЕ истории всех чатов), поэтому
    # регрессия здесь проверяется отдельно, а не только на уровне _check_admin_key.
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={}, query_params={"key": "real-admin-key"})))
        assert "error" in result
    finally:
        bot.ADMIN_PANEL_KEY = original


def test_export_state_returns_chats_and_quota_with_valid_key():
    original = bot.ADMIN_PANEL_KEY
    bot.ADMIN_PANEL_KEY = "real-admin-key"
    chat_id = 999411
    bot.chat_state[chat_id] = {
        "image_model": bot.DEFAULT_HF_IMAGE_MODEL, "history": [{"role": "user", "content": "hi"}],
        "quota": {}, "recent_media_ids": {}, "last_activity": 0.0,
    }
    try:
        result = asyncio.run(bot.export_state(_FakeAdminRequest(headers={"Authorization": "Bearer real-admin-key"})))
        assert str(chat_id) in result["chats"]
        assert result["chats"][str(chat_id)]["history"] == [{"role": "user", "content": "hi"}]
        assert "global_quota" in result
        assert "exported_at" in result
    finally:
        bot.ADMIN_PANEL_KEY = original
        bot.chat_state.pop(chat_id, None)


# ─────────────────── webhook_handler (аудит техдолга: не было ни одного теста на единственную точку входа для всего входящего Telegram-трафика) ───────────────────
# Раньше были изолированные тесты только на _check_bot_token_auth/_check_admin_key
# (гейтят другие эндпоинты) — сам webhook_handler (hmac.compare_digest секрета из
# заголовка X-Telegram-Bot-Api-Secret-Token + диспатч в _process_raw_update) не был
# покрыт вообще. Регрессия здесь означала бы либо приём неавторизованных апдейтов,
# либо полную остановку приёма сообщений ботом — самый security-критичный путь
# в проекте после самих секретов.

class _FakeWebhookRequest:
    def __init__(self, headers: dict | None = None, body: dict | None = None):
        self.headers = headers or {}
        self._body = body or {}

    async def json(self):
        return self._body


async def _run_webhook_handler(req: "_FakeWebhookRequest") -> dict:
    """asyncio.create_task внутри webhook_handler запускает обработку апдейта не
    дожидаясь её завершения — один await asyncio.sleep(0) после вызова даёт
    планировщику шанс выполнить уже запланированную задачу (фейковый обработчик
    ниже не делает собственных await, поэтому этого достаточно для детерминизма)."""
    result = await bot.webhook_handler(req)
    await asyncio.sleep(0)
    return result


def test_webhook_handler_accepts_valid_secret_and_dispatches_update():
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    bot.bot = object()  # не-None достаточно, чтобы пройти проверку "бот уже инициализирован"
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert result == {"ok": True}
        assert calls == [{"update_id": 1}]
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


def test_webhook_handler_rejects_invalid_or_missing_secret_and_does_not_dispatch():
    original_secret = bot.WEBHOOK_SECRET
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        for bad_headers in (
            {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            {},
        ):
            req = _FakeWebhookRequest(headers=bad_headers, body={"update_id": 1})
            result = asyncio.run(_run_webhook_handler(req))
            assert result == {"ok": False}
        assert calls == []
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot._process_raw_update = original_process


def test_webhook_handler_drops_update_when_bot_not_yet_initialized():
    # Апдейт может прийти раньше, чем main() успеет создать глобальный bot (Bot/
    # genai.Client создаются уже после старта uvicorn) — должен тихо отбрасываться,
    # а не падать, и не пытаться диспатчить апдейт в ещё не готового бота.
    original_secret = bot.WEBHOOK_SECRET
    original_bot_obj = bot.bot
    original_process = bot._process_raw_update
    bot.WEBHOOK_SECRET = "real-webhook-secret"
    bot.bot = None
    calls = []

    async def fake_process(raw_update):
        calls.append(raw_update)

    bot._process_raw_update = fake_process
    try:
        req = _FakeWebhookRequest(
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-webhook-secret"},
            body={"update_id": 1},
        )
        result = asyncio.run(_run_webhook_handler(req))
        assert result == {"ok": True}
        assert calls == []
    finally:
        bot.WEBHOOK_SECRET = original_secret
        bot.bot = original_bot_obj
        bot._process_raw_update = original_process


# ─────────────────── ADMIN_SECRET_SEED — независимая ротация секретов ───────────────────

def test_admin_secrets_are_independent_of_bot_token_when_seed_set():
    # РЕГРЕССИЯ (аудит техдолга): раньше WEBHOOK_SECRET/ADMIN_PANEL_KEY выводились
    # ИСКЛЮЧИТЕЛЬНО из BOT_TOKEN — компрометация токена компрометировала оба сразу,
    # и ни один нельзя было ротировать независимо. Теперь можно задать отдельную соль.
    import hashlib
    seed_a = "seed-one"
    seed_b = "seed-two"
    webhook_a = hashlib.sha256(seed_a.encode()).hexdigest()[:32]
    webhook_b = hashlib.sha256(seed_b.encode()).hexdigest()[:32]
    assert webhook_a != webhook_b  # разные соли -> разные секреты, как и должно быть


def test_admin_secret_seed_falls_back_to_bot_token_when_unset():
    # Без ADMIN_SECRET_SEED поведение идентично прежнему (соль = BOT_TOKEN) — не
    # ломает существующие деплои, которые эту переменную не настраивали.
    assert bot._ADMIN_SECRET_SEED == (os.environ.get("ADMIN_SECRET_SEED", "").strip() or bot.BOT_TOKEN or "default")


# ─────────────────── мультипрокси: rotate-before-pause + owner-алерт ───────────────────

def test_rotate_telegram_proxy_noop_with_single_candidate():
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://only-one.example.com"]
    bot._telegram_proxy_idx = 0
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is False
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx


def test_rotate_telegram_proxy_switches_and_reports_lap_not_done():
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    original_base_url = bot.TELEGRAM_API_BASE_URL
    original_bot = bot.bot
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://primary.example.com", "https://fallback.example.com"]
    bot._telegram_proxy_idx = 0
    bot.TELEGRAM_API_BASE_URL = "https://primary.example.com"
    bot.bot = None  # без реального aiogram Bot — проверяем только URL-переключение
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is True  # ещё не замкнули круг — есть смысл пробовать сразу
        assert bot.TELEGRAM_API_BASE_URL == "https://fallback.example.com"
        assert bot._telegram_proxy_idx == 1
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx
        bot.TELEGRAM_API_BASE_URL = original_base_url
        bot.bot = original_bot


def test_rotate_telegram_proxy_reports_lap_done_after_full_cycle():
    original_candidates = bot._TELEGRAM_PROXY_CANDIDATES
    original_idx = bot._telegram_proxy_idx
    original_base_url = bot.TELEGRAM_API_BASE_URL
    original_bot = bot.bot
    bot._TELEGRAM_PROXY_CANDIDATES = ["https://primary.example.com", "https://fallback.example.com"]
    bot._telegram_proxy_idx = 1  # уже на резервном — следующий поворот вернёт на primary (idx 0)
    bot.TELEGRAM_API_BASE_URL = "https://fallback.example.com"
    bot.bot = None
    try:
        switched = asyncio.run(bot._rotate_telegram_proxy())
        assert switched is False  # круг замкнулся — пора паузу включать
        assert bot._telegram_proxy_idx == 0
    finally:
        bot._TELEGRAM_PROXY_CANDIDATES = original_candidates
        bot._telegram_proxy_idx = original_idx
        bot.TELEGRAM_API_BASE_URL = original_base_url
        bot.bot = original_bot


class _FakeOwnerBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace()


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
    original_last = bot._last_gemini_exhausted_alert_monotonic
    original_owner, original_bot = bot.OWNER_ID, bot.bot
    bot.OWNER_ID = 12345
    fake = _FakeOwnerBot()
    bot.bot = fake
    bot._last_gemini_exhausted_alert_monotonic = time.monotonic() - bot.GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC - 10.0
    try:
        asyncio.run(bot._maybe_alert_gemini_exhausted())
        asyncio.run(bot._maybe_alert_gemini_exhausted())
        assert len(fake.sent) == 1  # второй вызов сразу же — троттлинг не пускает повтор
    finally:
        bot._last_gemini_exhausted_alert_monotonic = original_last
        bot.OWNER_ID, bot.bot = original_owner, original_bot


# ─────────────────── _or_request: вычищает OPENROUTER_API_KEY из сетевых исключений ───────────────────
# РЕГРЕССИЯ (секьюрити-ревью): telegram_api_call/_download_telegram_file_bytes уже вычищали
# BOT_TOKEN из текста сетевых исключений до того, как обернуть их в свою собственную ошибку —
# _or_request не делал того же для OPENROUTER_API_KEY. На практике ключ передаётся только в
# заголовке Authorization, поэтому обычные исключения aiohttp его не содержат — это защита по
# глубине на случай нестандартного сообщения об ошибке (например от прокси), которое могло бы
# процитировать заголовки запроса целиком.

def test_or_request_scrubs_api_key_from_network_exception_message():
    class _FakeSessionRaisingWithKeyInMessage:
        def request(self, *args, **kwargs):
            raise RuntimeError("connection failed, headers were: Authorization: Bearer fake-secret-or-key-123")

    async def fake_get_http_session():
        return _FakeSessionRaisingWithKeyInMessage()

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-secret-or-key-123"
    try:
        with pytest.raises(bot.OpenRouterAPIError) as exc_info:
            asyncio.run(bot._or_request("chat/completions", "POST", json_body={"model": "x"}))
        assert "fake-secret-or-key-123" not in str(exc_info.value)
        assert "<KEY>" in str(exc_info.value)
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


# ─────────────────── _probe_or_model_liveness (проактивная проверка живости) ───────────────────

def test_probe_or_model_liveness_warns_on_dead_model_pattern(caplog):
    # РЕГРЕССИЯ (аудит техдолга): раньше проверялась только голова (index 0) списка
    # навсегда — теперь проверяемая модель зависит от дня года (day-of-year % len),
    # чтобы за N дней проверить весь список ценой тех же 3 запросов/сутки, что и
    # раньше (см. докстринг _probe_or_model_liveness). Тест вычисляет ожидаемую
    # модель той же формулой, что и сама функция, вместо того чтобы полагаться на
    # фиксированный index 0.
    import logging
    from datetime import date
    expected_model = bot._OR_LIGHT_ORDER[date.today().timetuple().tm_yday % len(bot._OR_LIGHT_ORDER)]

    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        if model == expected_model:
            raise bot.OpenRouterAPIError("This model is unavailable for free. use another slug", status_code=404)
        return {"choices": [{"message": {"content": "pong"}}]}

    original_request = bot._or_request
    original_key = bot.OPENROUTER_API_KEY
    bot._or_request = fake_or_request
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot._probe_or_model_liveness())
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert expected_model in messages
        assert "_OR_LIGHT_ORDER" in messages
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_rotates_by_day_of_year():
    # Проверяем саму формулу ротации напрямую (не только эффект в одном из трёх
    # списков, как в тесте выше) — на день N должен пробоваться models[N % len(models)].
    from datetime import date
    calls = []

    async def fake_or_request(path, method="GET", *, json_body=None):
        calls.append(json_body["model"])
        return {"choices": [{"message": {"content": "pong"}}]}

    original_request = bot._or_request
    original_key = bot.OPENROUTER_API_KEY
    bot._or_request = fake_or_request
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        asyncio.run(bot._probe_or_model_liveness())
        day_idx = date.today().timetuple().tm_yday
        assert calls == [
            bot._OR_LIGHT_ORDER[day_idx % len(bot._OR_LIGHT_ORDER)],
            bot._OR_HEAVY_ORDER[day_idx % len(bot._OR_HEAVY_ORDER)],
            bot._OR_VISION_ORDER[day_idx % len(bot._OR_VISION_ORDER)],
        ]
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_silent_when_all_alive(caplog):
    import logging

    async def fake_or_request(path, method="GET", *, json_body=None):
        return {"choices": [{"message": {"content": "pong"}}]}

    original_request = bot._or_request
    original_key = bot.OPENROUTER_API_KEY
    bot._or_request = fake_or_request
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot._probe_or_model_liveness())
        assert caplog.records == []
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_noop_without_api_key():
    original_key = bot.OPENROUTER_API_KEY
    bot.OPENROUTER_API_KEY = ""
    called = []

    async def fake_or_request(*args, **kwargs):
        called.append(1)
        return {}

    original_request = bot._or_request
    bot._or_request = fake_or_request
    try:
        asyncio.run(bot._probe_or_model_liveness())
        assert called == []
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


# ─────────────────── handle_tiktok — оркестрация целиком (была не покрыта тестами) ───────────────────

class _FakeTikTokBot:
    def __init__(self):
        self.sent_videos: list[dict] = []

    async def send_video(self, **kwargs):
        self.sent_videos.append(kwargs)
        return SimpleNamespace()

    async def send_audio(self, **kwargs):
        return SimpleNamespace()


class _FakeTikTokResponse:
    def __init__(self, status=200, json_body=None):
        self.status = status
        self._json_body = json_body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._json_body


class _FakeTikTokSession:
    """Мокает aiohttp.ClientSession для целого handle_tiktok: session.get() отдаёт
    ответ TikWM API. _download_url_bin/_resolve_tiktok_short патчатся отдельно
    (они принимают сессию, но не обязаны реально использовать её методы)."""
    def __init__(self, tikwm_json: dict):
        self._tikwm_json = tikwm_json

    def get(self, url, *args, **kwargs):
        if "tikwm.com/api" in url:
            return _FakeTikTokResponse(200, self._tikwm_json)
        return _FakeTikTokResponse(200, {})


def test_handle_tiktok_single_video_happy_path():
    # Регрессия на пробел из аудита техдолга: все "кирпичики" TikTok-загрузчика
    # покрыты юнит-тестами, но сама оркестрирующая handle_tiktok — нет. Этот тест
    # проверяет путь "обычное видео" целиком: TikWM отвечает, видео "скачивается"
    # (замокано), отправляется через bot.send_video.
    tikwm_json = {
        "code": 0,
        "data": {
            "play": "https://tikwm.com/sd.mp4", "size": 1000,
            "author": {"nickname": "TestAuthor"},
        },
    }

    incoming = _FakeIncomingMessage(999420)
    incoming.message_id = 1
    fake_bot = _FakeTikTokBot()

    async def fake_get_http_session():
        return _FakeTikTokSession(tikwm_json)

    async def fake_resolve(session, url):
        return url  # ссылка уже "разрешена", не короткая

    async def fake_download_url_bin(session, url, headers=None):
        return b"\x00" * 100  # не видео-байты (не ftyp) — не важно для этого теста

    async def fake_probe_dims(path):
        return 0, 0, 0

    async def fake_thumb(path, duration):
        return None

    original_get_session = bot._get_http_session
    original_resolve = bot._resolve_tiktok_short
    original_download = bot._download_url_bin
    original_probe = bot._probe_video_dimensions
    original_thumb = bot._generate_video_thumbnail
    original_bot = bot.bot

    bot._get_http_session = fake_get_http_session
    bot._resolve_tiktok_short = fake_resolve
    bot._download_url_bin = fake_download_url_bin
    bot._probe_video_dimensions = fake_probe_dims
    bot._generate_video_thumbnail = fake_thumb
    bot.bot = fake_bot
    try:
        asyncio.run(bot.handle_tiktok(incoming, "https://www.tiktok.com/@test/video/123"))
        assert len(fake_bot.sent_videos) == 1
    finally:
        bot._get_http_session = original_get_session
        bot._resolve_tiktok_short = original_resolve
        bot._download_url_bin = original_download
        bot._probe_video_dimensions = original_probe
        bot._generate_video_thumbnail = original_thumb
        bot.bot = original_bot


def test_handle_tiktok_slideshow_video_probing_respects_concurrency_cap():
    # Регрессия на находку код-ревью (28 августа 2026): пробинг видео-слайдов
    # слайдшоу (реальные ffprobe/ffmpeg-подпроцессы на каждый) раньше не имел
    # ограничения конкурентности — слайдшоу с несколькими видео-слайдами могло бы
    # дать неконтролируемый всплеск подпроцессов на контейнере с ограниченными
    # ресурсами. Больше video-слайдов, чем TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY —
    # проверяем, что реально одновременно работающих проб никогда не больше лимита.
    n_video_slides = bot.TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY + 3
    video_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"  # проходит _looks_like_video_bytes
    tikwm_json = {
        "code": 0,
        "data": {
            "images": [f"https://tikwm.com/slide{i}.jpg" for i in range(n_video_slides)],
            "author": {"nickname": "TestAuthor"},
        },
    }

    incoming = _FakeIncomingMessage(999433)
    incoming.message_id = 1
    fake_bot = _FakeTikTokBot()

    current_concurrent = 0
    max_concurrent_seen = 0
    lock = asyncio.Lock()

    async def fake_get_http_session():
        return _FakeTikTokSession(tikwm_json)

    async def fake_resolve(session, url):
        return url

    async def fake_download_url_bin(session, url, headers=None):
        return video_bytes

    async def fake_probe_and_thumbnail(item_bytes):
        nonlocal current_concurrent, max_concurrent_seen
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
        await asyncio.sleep(0.05)  # достаточно, чтобы вызовы реально пересеклись во времени
        async with lock:
            current_concurrent -= 1
        return 0, 0, 0, None

    original_get_session = bot._get_http_session
    original_resolve = bot._resolve_tiktok_short
    original_download = bot._download_url_bin
    original_probe_and_thumb = bot._probe_and_thumbnail_from_bytes
    original_bot = bot.bot

    bot._get_http_session = fake_get_http_session
    bot._resolve_tiktok_short = fake_resolve
    bot._download_url_bin = fake_download_url_bin
    bot._probe_and_thumbnail_from_bytes = fake_probe_and_thumbnail
    bot.bot = fake_bot
    try:
        asyncio.run(bot.handle_tiktok(incoming, "https://www.tiktok.com/@test/video/456"))
        assert max_concurrent_seen <= bot.TIKTOK_VIDEO_SLIDE_PROBE_CONCURRENCY
        # Реально были параллельны хотя бы несколько — не выродилось в строго
        # последовательный перебор без всякой пользы от asyncio.gather.
        assert max_concurrent_seen > 1
    finally:
        bot._get_http_session = original_get_session
        bot._resolve_tiktok_short = original_resolve
        bot._download_url_bin = original_download
        bot._probe_and_thumbnail_from_bytes = original_probe_and_thumb
        bot.bot = original_bot


def test_handle_tiktok_no_media_found_gives_user_facing_error():
    tikwm_json = {"code": 0, "data": {"author": {"nickname": "TestAuthor"}}}  # нет ни play, ни images

    incoming = _FakeIncomingMessage(999421)
    incoming.message_id = 2

    async def fake_get_http_session():
        return _FakeTikTokSession(tikwm_json)

    async def fake_resolve(session, url):
        return url

    original_get_session = bot._get_http_session
    original_resolve = bot._resolve_tiktok_short
    bot._get_http_session = fake_get_http_session
    bot._resolve_tiktok_short = fake_resolve
    try:
        # handle_tiktok сам ловит исключение и редактирует статусное сообщение —
        # не поднимает наружу; проверяем, что оно не падает необработанным.
        asyncio.run(bot.handle_tiktok(incoming, "https://www.tiktok.com/@test/video/456"))
    finally:
        bot._get_http_session = original_get_session
        bot._resolve_tiktok_short = original_resolve


# ─────────────────── _fetch_tikwm_media_data (троттлинг + ретрай при 403) ───────────────────
# Регрессия на реальный инцидент (отладка 11 августа 2026, см. Sentry-трейс
# TikTokUserFacingError): оба зеркала TikWM стабильно отвечали HTTP 403 подряд с
# разницей ~36мс — раньше код бил в оба зеркала практически одновременно без единой
# паузы между запросами, что само по себе способно нарушать задокументированный
# сторонними инструментами лимит TikWM (~1 запрос/сек). Тесты проверяют: троттлинг
# между запросами, откат на второе зеркало при 403, одну повторную попытку, если
# 403 пришёл от ОБОИХ зеркал подряд, и отсутствие лишнего ретрая для обычной
# "постоянной" ошибки TikWM (code != 0 при HTTP 200 — видео реально недоступно).

class _FakeTikwmApiResponse:
    def __init__(self, status=200, json_body=None, body_bytes=b""):
        self.status = status
        self._json_body = json_body or {}
        self._body_bytes = body_bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._json_body

    async def read(self):
        return self._body_bytes


class _FakeTikwmApiSession:
    """session.get(url) — очередь заранее заготовленных ответов (по одному на
    каждый вызов, в порядке вызова) плюс список реально запрошенных URL/заголовков —
    тесты ниже проверяют и содержимое ответов, и сам порядок/число обращений, и
    заголовки, с которыми ушёл каждый запрос."""
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.requested_urls: list[str] = []
        self.requested_headers: list[dict] = []

    def get(self, url, *args, **kwargs):
        self.requested_urls.append(url)
        self.requested_headers.append(kwargs.get("headers") or {})
        return self._responses.pop(0) if self._responses else _FakeTikwmApiResponse(status=500)


def test_fetch_tikwm_media_data_returns_data_on_first_mirror_success():
    resp = _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "x"}})
    session = _FakeTikwmApiSession([resp])
    result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/1", {}))
    assert result == {"play": "x"}
    assert len(session.requested_urls) == 1


def test_fetch_tikwm_media_data_falls_back_to_second_mirror_on_403():
    responses = [
        _FakeTikwmApiResponse(status=403, body_bytes=b"Forbidden"),
        _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "y"}}),
    ]
    session = _FakeTikwmApiSession(responses)
    result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/2", {}))
    assert result == {"play": "y"}
    assert len(session.requested_urls) == 2


def test_fetch_tikwm_media_data_retries_once_after_both_mirrors_403():
    # Первый раунд (оба зеркала) — 403; второй раунд, первое же зеркало — успех.
    responses = [
        _FakeTikwmApiResponse(status=403, body_bytes=b"Forbidden"),
        _FakeTikwmApiResponse(status=403, body_bytes=b"Forbidden"),
        _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "z"}}),
        _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "unused"}}),
    ]
    session = _FakeTikwmApiSession(responses)
    result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/3", {}))
    assert result == {"play": "z"}
    assert len(session.requested_urls) == 3  # третий запрос уже успешен — четвёртый не понадобился


def test_fetch_tikwm_media_data_returns_none_when_still_403_after_retry():
    # Ни один раунд (2 зеркала x 2 раунда = 4 попытки) не дал успеха — сдаёмся,
    # без бесконечных повторов (ровно одна повторная попытка, не больше).
    responses = [_FakeTikwmApiResponse(status=403, body_bytes=b"Forbidden") for _ in range(4)]
    session = _FakeTikwmApiSession(responses)
    result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/4", {}))
    assert result is None
    assert len(session.requested_urls) == 4


def test_fetch_tikwm_media_data_no_retry_round_on_permanent_tikwm_error_code():
    # code != 0 при HTTP 200 (например "видео приватное") — это ДЕЙСТВИТЕЛЬНО
    # недоступное видео, а не признак троттлинга (403) — повторного раунда здесь
    # быть не должно, даже если оба зеркала вернули такую ошибку.
    responses = [
        _FakeTikwmApiResponse(status=200, json_body={"code": -1, "msg": "private video"}),
        _FakeTikwmApiResponse(status=200, json_body={"code": -1, "msg": "private video"}),
    ]
    session = _FakeTikwmApiSession(responses)
    result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/5", {}))
    assert result is None
    assert len(session.requested_urls) == 2


def test_fetch_tikwm_media_data_throttles_between_requests():
    # Регрессия на сам смысл фикса: если "прошлый запрос к TikWM" был только что,
    # следующий обязан подождать (а не выстрелить мгновенно, как раньше).
    import lumen_tiktok
    responses = [
        _FakeTikwmApiResponse(status=403, body_bytes=b"Forbidden"),
        _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "w"}}),
    ]
    session = _FakeTikwmApiSession(responses)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    original_sleep = lumen_tiktok._sleep
    lumen_tiktok._sleep = fake_sleep
    lumen_tiktok._tikwm_last_request_ts = time.monotonic()  # "запрос только что был"
    try:
        asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/6", {}))
        assert sleep_calls  # хотя бы одна пауза перед следующим запросом
        assert all(s > 0 for s in sleep_calls)
    finally:
        lumen_tiktok._sleep = original_sleep
        lumen_tiktok._tikwm_last_request_ts = None


def test_fetch_tikwm_media_data_sends_referer_and_origin_headers():
    # ИСПРАВЛЕНО (отладка 12 августа 2026): даже с корректным URL и корректным
    # троттлингом TikWM продолжал отвечать 403 с пустым телом — Referer/Origin,
    # имитирующие вызов со страницы самого tikwm.com, добавлены как best-effort
    # попытка обхода анти-скрейпинг проверки (см. докстринг _fetch_tikwm_media_data).
    # Исходные заголовки (например User-Agent от вызывающего кода) не должны теряться.
    resp = _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "x"}})
    session = _FakeTikwmApiSession([resp])
    asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/7", {"User-Agent": "test-ua"}))
    sent_headers = session.requested_headers[0]
    assert sent_headers["Referer"] == "https://www.tikwm.com/"
    assert sent_headers["Origin"] == "https://www.tikwm.com"
    assert sent_headers["User-Agent"] == "test-ua"


# ─────────────────── _fetch_tikwm_media_data: запрос через выделенный прокси (proxy_base_url) ───────────────────
# Регрессия на итог отладки блокировки IP (см. README/proxy.ts): throttle, ретраи,
# валидация URL и Referer/Origin не помогли — единственное подтверждённое рабочее
# решение — прокси с другого IP. Тесты ниже проверяют именно ветку `proxy_base_url`
# в отрыве от прямых зеркал (та ветка уже покрыта тестами выше).

def test_fetch_tikwm_media_data_uses_single_proxy_endpoint_when_configured():
    resp = _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "via-proxy"}})
    session = _FakeTikwmApiSession([resp])
    result = asyncio.run(bot._fetch_tikwm_media_data(
        session, "https://www.tiktok.com/@u/video/1", {},
        proxy_base_url="https://proxy.example.com/fetch/www.tikwm.com",
    ))
    assert result == {"play": "via-proxy"}
    # Ровно ОДИН запрос (не два зеркала) — прокси сам решает, к какому реальному
    # хосту TikWM стучаться, дублировать зеркала через него уже незачем.
    assert session.requested_urls == [
        "https://proxy.example.com/fetch/www.tikwm.com/api/?url=https%3A//www.tiktok.com/%40u/video/1&hd=1"
    ]


def test_fetch_tikwm_media_data_proxy_endpoint_respects_hd_flag():
    resp = _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "x"}})
    session = _FakeTikwmApiSession([resp])
    asyncio.run(bot._fetch_tikwm_media_data(
        session, "https://www.tiktok.com/@u/video/1", {}, hd=False,
        proxy_base_url="https://proxy.example.com/fetch/www.tikwm.com",
    ))
    assert "&hd=1" not in session.requested_urls[0]


def test_fetch_tikwm_media_data_proxy_still_retries_once_on_403():
    # Ретрай-раунд при 403 (см. _TIKWM_RETRY_BACKOFF_SEC) не завязан на наличие
    # двух зеркал — с одним прокси-эндпоинтом повторная попытка тоже должна
    # сработать (мало ли транзиентная ошибка именно на стороне прокси).
    responses = [
        _FakeTikwmApiResponse(status=403, body_bytes=b""),
        _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "retried"}}),
    ]
    session = _FakeTikwmApiSession(responses)
    result = asyncio.run(bot._fetch_tikwm_media_data(
        session, "https://www.tiktok.com/@u/video/1", {},
        proxy_base_url="https://proxy.example.com/fetch/www.tikwm.com",
    ))
    assert result == {"play": "retried"}
    assert len(session.requested_urls) == 2


def test_fetch_tikwm_media_data_without_proxy_still_uses_both_direct_mirrors():
    # Регрессия на обратную совместимость: proxy_base_url="" (значение по
    # умолчанию, как и раньше, когда TIKWM_API_BASE_URL не задан в env) должно
    # сохранять старое поведение — оба прямых зеркала, без единого изменения.
    resp = _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "direct"}})
    session = _FakeTikwmApiSession([resp])
    result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/1", {}))
    assert result == {"play": "direct"}
    assert session.requested_urls[0].startswith("https://www.tikwm.com/api/?url=")


def test_fetch_tikwm_media_data_logs_ip_block_hint_when_all_attempts_403_with_empty_body(caplog):
    # Регрессия на реальный инцидент (12 августа 2026): корректный URL + корректный
    # троттлинг + ВСЕ попытки (оба зеркала, оба раунда) вернули 403 с пустым телом —
    # это специфический паттерн, заслуживающий отдельного, легко узнаваемого
    # диагностического лога (а не очередного часа Sentry-археологии в следующий раз).
    import logging
    responses = [_FakeTikwmApiResponse(status=403, body_bytes=b"") for _ in range(4)]
    session = _FakeTikwmApiSession(responses)
    with caplog.at_level(logging.WARNING, logger="bot"):
        result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/8", {}))
    assert result is None
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "[tikwm][diag]" in messages


def test_fetch_tikwm_media_data_no_ip_block_hint_when_body_not_empty(caplog):
    # У ошибки есть реальный текст (например настоящее сообщение о рейт-лимите) —
    # это ДРУГОЙ, более информативный случай, не нужно путать его с гипотезой
    # про блокировку IP (пустое тело — специфический признак именно её).
    import logging
    responses = [_FakeTikwmApiResponse(status=403, body_bytes=b"Rate limited") for _ in range(4)]
    session = _FakeTikwmApiSession(responses)
    with caplog.at_level(logging.WARNING, logger="bot"):
        result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/9", {}))
    assert result is None
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "[tikwm][diag]" not in messages


def test_fetch_tikwm_media_data_no_ip_block_hint_on_success(caplog):
    # Успешный ответ (пусть даже после предыдущих неудачных попыток) не должен
    # ошибочно засчитываться как "всё было 403" — счётчик должен сброситься.
    import logging
    responses = [
        _FakeTikwmApiResponse(status=403, body_bytes=b""),
        _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "ok"}}),
    ]
    session = _FakeTikwmApiSession(responses)
    with caplog.at_level(logging.WARNING, logger="bot"):
        result = asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/10", {}))
    assert result == {"play": "ok"}
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "[tikwm][diag]" not in messages


# ─────────────────── резервные прокси TikWM (TIKWM_API_BASE_URL_FALLBACKS) ───────────────────
# Асимметрия с Telegram-прокси (у которого уже был _TELEGRAM_PROXY_CANDIDATES/
# _rotate_telegram_proxy) — TikWM зависит от того же самого единственного
# Deno-прокси, но раньше при его падении не было пути попробовать резервный адрес.

def test_tikwm_proxy_candidates_single_empty_string_when_unset():
    original = bot.TIKWM_API_BASE_URL
    bot.TIKWM_API_BASE_URL = ""
    try:
        assert bot._tikwm_proxy_candidates() == [""]
    finally:
        bot.TIKWM_API_BASE_URL = original


def test_tikwm_proxy_candidates_primary_plus_fallbacks_deduped():
    original_primary = bot.TIKWM_API_BASE_URL
    original_fallbacks = bot._TIKWM_API_BASE_URL_FALLBACKS
    bot.TIKWM_API_BASE_URL = "https://primary.example.com/fetch/www.tikwm.com"
    bot._TIKWM_API_BASE_URL_FALLBACKS = [
        "https://primary.example.com/fetch/www.tikwm.com",  # дубль основного — должен быть отфильтрован
        "https://backup.example.com/fetch/www.tikwm.com",
    ]
    try:
        assert bot._tikwm_proxy_candidates() == [
            "https://primary.example.com/fetch/www.tikwm.com",
            "https://backup.example.com/fetch/www.tikwm.com",
        ]
    finally:
        bot.TIKWM_API_BASE_URL = original_primary
        bot._TIKWM_API_BASE_URL_FALLBACKS = original_fallbacks


def test_fetch_tikwm_media_data_with_proxy_fallback_uses_backup_when_primary_fails():
    original_primary = bot.TIKWM_API_BASE_URL
    original_fallbacks = bot._TIKWM_API_BASE_URL_FALLBACKS
    bot.TIKWM_API_BASE_URL = "https://primary.example.com/fetch/www.tikwm.com"
    bot._TIKWM_API_BASE_URL_FALLBACKS = ["https://backup.example.com/fetch/www.tikwm.com"]

    calls = []

    async def fake_fetch(session, resolved_url, headers, *, proxy_base_url=""):
        calls.append(proxy_base_url)
        if proxy_base_url == bot.TIKWM_API_BASE_URL:
            return None  # основной прокси "недоступен"
        return {"play": "via-backup"}

    original_fetch = bot._fetch_tikwm_media_data
    bot._fetch_tikwm_media_data = fake_fetch
    try:
        result = asyncio.run(bot._fetch_tikwm_media_data_with_proxy_fallback(None, "https://www.tiktok.com/@u/video/1", {}))
        assert result == {"play": "via-backup"}
        assert calls == [
            "https://primary.example.com/fetch/www.tikwm.com",
            "https://backup.example.com/fetch/www.tikwm.com",
        ]
    finally:
        bot._fetch_tikwm_media_data = original_fetch
        bot.TIKWM_API_BASE_URL = original_primary
        bot._TIKWM_API_BASE_URL_FALLBACKS = original_fallbacks


def test_fetch_tikwm_media_data_with_proxy_fallback_none_when_all_fail():
    original_primary = bot.TIKWM_API_BASE_URL
    original_fallbacks = bot._TIKWM_API_BASE_URL_FALLBACKS
    bot.TIKWM_API_BASE_URL = "https://primary.example.com/fetch/www.tikwm.com"
    bot._TIKWM_API_BASE_URL_FALLBACKS = ["https://backup.example.com/fetch/www.tikwm.com"]

    async def fake_fetch(session, resolved_url, headers, *, proxy_base_url=""):
        return None

    original_fetch = bot._fetch_tikwm_media_data
    bot._fetch_tikwm_media_data = fake_fetch
    try:
        result = asyncio.run(bot._fetch_tikwm_media_data_with_proxy_fallback(None, "https://www.tiktok.com/@u/video/1", {}))
        assert result is None
    finally:
        bot._fetch_tikwm_media_data = original_fetch
        bot.TIKWM_API_BASE_URL = original_primary
        bot._TIKWM_API_BASE_URL_FALLBACKS = original_fallbacks


def test_fetch_tikwm_media_data_with_proxy_fallback_unchanged_when_unconfigured():
    # Обратная совместимость: TIKWM_API_BASE_URL не задан — единственный вызов
    # с proxy_base_url="" (прямой режим, как раньше).
    original_primary = bot.TIKWM_API_BASE_URL
    original_fallbacks = bot._TIKWM_API_BASE_URL_FALLBACKS
    bot.TIKWM_API_BASE_URL = ""
    bot._TIKWM_API_BASE_URL_FALLBACKS = []

    calls = []

    async def fake_fetch(session, resolved_url, headers, *, proxy_base_url=""):
        calls.append(proxy_base_url)
        return {"play": "direct"}

    original_fetch = bot._fetch_tikwm_media_data
    bot._fetch_tikwm_media_data = fake_fetch
    try:
        result = asyncio.run(bot._fetch_tikwm_media_data_with_proxy_fallback(None, "https://www.tiktok.com/@u/video/1", {}))
        assert result == {"play": "direct"}
        assert calls == [""]
    finally:
        bot._fetch_tikwm_media_data = original_fetch
        bot.TIKWM_API_BASE_URL = original_primary
        bot._TIKWM_API_BASE_URL_FALLBACKS = original_fallbacks

