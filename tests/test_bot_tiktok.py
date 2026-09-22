"""
test_bot_tiktok.py — TikTok: резолв, слайдшоу, видео, музыка, concurrency-капы.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from types import SimpleNamespace
import asyncio
import bot
import lumen_tiktok
import pytest
import time
from tests.bot_test_helpers import (
    _FakeAudioBot,
    _FakeDownloadResponse,
    _FakeDownloadSession,
    _FakeIncomingMessage,
    _FakeProc,
    _FakeResolveResponse,
    _FakeResolveSession,
    _FakeSentMessage,
    _FakeTikTokBot,
    _FakeTikTokSession,
    _FakeTikwmApiResponse,
    _FakeTikwmApiSession,
    _run_send_tiktok_music,
)


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
    assert bot._original_sound_label("sw") == "Original sound"
    assert bot._original_sound_label("xx-YY") == "Original sound"


def test_original_sound_label_falls_back_to_english_when_missing():
    assert bot._original_sound_label(None) == "Original sound"
    assert bot._original_sound_label("") == "Original sound"


def test_original_sound_label_case_insensitive():
    assert bot._original_sound_label("RU") == "Оригинальный звук"


def test_original_sound_label_fallback_chain():
    # Цепочка: язык отправителя → язык чата (/lang) → английский.
    assert bot._original_sound_label("de", "uk") == "Originalton"
    assert bot._original_sound_label(None, "uk") == "Оригінальний звук"
    assert bot._original_sound_label("sw", "kk") == "Түпнұсқа дыбыс"
    assert bot._original_sound_label("th", "kk") == "เสียงต้นฉบับ"
    assert bot._original_sound_label("sw", "xx") == "Original sound"
    assert bot._original_sound_label(None, None) == "Original sound"


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


def test_send_tiktok_music_filename_sanitizes_slashes_and_control_chars():
    # Регрессия AUD-E-005: слэш/контролы из чужого названия в multipart-имени.
    incoming = _FakeIncomingMessage(999602)
    incoming.message_id = 12345
    incoming.from_user = SimpleNamespace(language_code="ru")
    media_data = {
        "music": "https://example.com/sound.mp3",
        "music_info": {"title": "a/b\x01c", "author": "X", "cover": ""},
        "author": {"nickname": "N", "unique_id": "n"},
    }

    async def fake_download(session, url, headers=None):
        return b"fake-bytes"

    fake_bot = _FakeAudioBot()
    original_download, original_bot = bot._download_url_bin, bot.bot
    bot._download_url_bin = fake_download
    bot.bot = fake_bot
    try:
        asyncio.run(bot._send_tiktok_music(None, media_data, incoming, "N", {}))
    finally:
        bot._download_url_bin = original_download
        bot.bot = original_bot
    filename = fake_bot.sent_audio["audio"].filename
    assert "/" not in filename and "\\" not in filename and "\x01" not in filename
    assert filename.endswith(".mp3")


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
    # Исполнитель — автор ЗВУКА из music_info (прод 22.09.2026: звук переиспользуют чужие
    # посты, автор видео тут ни при чём).
    assert artist == "SomeArtist"


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
    overlapped = asyncio.Event()

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
            if current_concurrent >= 2:
                overlapped.set()
        # Детерминированный барьер вместо sleep: дальше идёт только тот, кто
        # реально пересёкся с соседом; последовательный регресс упрётся в таймаут.
        await asyncio.wait_for(overlapped.wait(), timeout=2)
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


def test_handle_tiktok_slideshow_download_respects_concurrency_cap():
    # РЕГРЕССИЯ (аудит TikTok-функций, 4 сентября 2026): скачивание слайдов
    # слайдшоу шло через asyncio.gather без единого ограничения конкурентности —
    # тот же класс проблемы, что уже был найден и исправлен для CPU-тяжёлого
    # пробинга видео-слайдов (см. test_handle_tiktok_slideshow_video_probing_
    # respects_concurrency_cap выше), только здесь риск не CPU, а исчерпание
    # общего пула соединений _get_http_session (connector limit=40, шарится со
    # ВСЕМ остальным трафиком бота — Gemini/OpenRouter/Pollinations и т.д.).
    n_slides = bot.TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY + 3
    tikwm_json = {
        "code": 0,
        "data": {
            "images": [f"https://tikwm.com/slide{i}.jpg" for i in range(n_slides)],
            "author": {"nickname": "TestAuthor"},
        },
    }

    incoming = _FakeIncomingMessage(999434)
    incoming.message_id = 1
    fake_bot = _FakeTikTokBot()

    current_concurrent = 0
    max_concurrent_seen = 0
    lock = asyncio.Lock()
    overlapped = asyncio.Event()

    async def fake_get_http_session():
        return _FakeTikTokSession(tikwm_json)

    async def fake_resolve(session, url):
        return url

    async def fake_download_url_bin(session, url, headers=None):
        nonlocal current_concurrent, max_concurrent_seen
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
            if current_concurrent >= 2:
                overlapped.set()
        # Детерминированный барьер вместо sleep (см. комментарий в probe-тесте выше).
        await asyncio.wait_for(overlapped.wait(), timeout=2)
        async with lock:
            current_concurrent -= 1
        return b"\xff\xd8\xff\xe0fake jpeg bytes"  # не ftyp -> обычное фото, не видео-слайд

    original_get_session = bot._get_http_session
    original_resolve = bot._resolve_tiktok_short
    original_download = bot._download_url_bin
    original_bot = bot.bot

    bot._get_http_session = fake_get_http_session
    bot._resolve_tiktok_short = fake_resolve
    bot._download_url_bin = fake_download_url_bin
    bot.bot = fake_bot
    try:
        asyncio.run(bot.handle_tiktok(incoming, "https://www.tiktok.com/@test/video/789"))
        assert max_concurrent_seen <= bot.TIKTOK_SLIDE_DOWNLOAD_CONCURRENCY
        # Реально были параллельны хотя бы несколько — не выродилось в строго
        # последовательный перебор без всякой пользы от asyncio.gather.
        assert max_concurrent_seen > 1
    finally:
        bot._get_http_session = original_get_session
        bot._resolve_tiktok_short = original_resolve
        bot._download_url_bin = original_download
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


def test_handle_tiktok_known_user_facing_error_logs_as_warning_not_exception(caplog):
    # РЕГРЕССИЯ (аудит логирования): TikTokUserFacingError — известный, уже
    # обработанный исход (видео недоступно и т.п.), для которого пользователь
    # получает понятный текст. Раньше здесь был безусловный log.exception (ERROR)
    # на КАЖДОЕ исключение — заводило issue в Sentry даже на рутинные случаи (см.
    # LUMEN-1: 24 события за месяц оказались этим классом). Теперь — log.warning
    # без трейсбека, log.exception остаётся только для реально неожиданных ошибок.
    import logging
    tikwm_json = {"code": 0, "data": {"author": {"nickname": "TestAuthor"}}}  # нет ни play, ни images

    incoming = _FakeIncomingMessage(999422)
    incoming.message_id = 3

    async def fake_get_http_session():
        return _FakeTikTokSession(tikwm_json)

    async def fake_resolve(session, url):
        return url

    original_get_session = bot._get_http_session
    original_resolve = bot._resolve_tiktok_short
    bot._get_http_session = fake_get_http_session
    bot._resolve_tiktok_short = fake_resolve
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot.handle_tiktok(incoming, "https://www.tiktok.com/@test/video/789"))
        levels = [r.levelname for r in caplog.records if "TikTok download" in r.getMessage()]
        assert levels == ["WARNING"]
    finally:
        bot._get_http_session = original_get_session
        bot._resolve_tiktok_short = original_resolve


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
    # 12.08.2026: TikWM отвечал 403 даже с троттлингом — Referer/Origin под страницу tikwm.com (best-effort); исходные заголовки не теряем.
    resp = _FakeTikwmApiResponse(status=200, json_body={"code": 0, "data": {"play": "x"}})
    session = _FakeTikwmApiSession([resp])
    asyncio.run(bot._fetch_tikwm_media_data(session, "https://www.tiktok.com/@u/video/7", {"User-Agent": "test-ua"}))
    sent_headers = session.requested_headers[0]
    assert sent_headers["Referer"] == "https://www.tikwm.com/"
    assert sent_headers["Origin"] == "https://www.tikwm.com"
    assert sent_headers["User-Agent"] == "test-ua"


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


def test_download_url_bin_returns_bytes_under_the_cap():
    resp = _FakeDownloadResponse([b"abc", b"def"])
    session = _FakeDownloadSession(resp)
    result = asyncio.run(lumen_tiktok._download_url_bin(session, "https://tikwm.com/x.jpg"))
    assert result == b"abcdef"


def test_download_url_bin_rejects_upfront_via_content_length_header():
    # Content-Length превышает лимит — отказываем ДО чтения тела вообще (быстрый
    # путь, экономит трафик и время, см. докстринг _download_url_bin).
    resp = _FakeDownloadResponse([b"should not be read"], content_length=str(lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES + 1))
    session = _FakeDownloadSession(resp)
    result = asyncio.run(lumen_tiktok._download_url_bin(session, "https://tikwm.com/huge.mp4"))
    assert result is None


def test_download_url_bin_aborts_mid_stream_when_no_content_length_but_body_too_big():
    # Сервер НЕ прислал Content-Length (обычное дело для chunked-ответов) — тело
    # всё равно не должно скачаться целиком в память, если суммарно превышает
    # лимит; проверка идёт потоково по факту реально пришедших байт.
    original_cap = lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES
    lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES = 10
    try:
        resp = _FakeDownloadResponse([b"12345", b"67890", b"11111"])  # 15 байт суммарно > лимита 10
        session = _FakeDownloadSession(resp)
        result = asyncio.run(lumen_tiktok._download_url_bin(session, "https://tikwm.com/huge.mp4"))
        assert result is None
    finally:
        lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES = original_cap


def test_download_url_bin_exactly_at_cap_still_succeeds():
    original_cap = lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES
    lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES = 10
    try:
        resp = _FakeDownloadResponse([b"1234567890"])  # ровно 10 байт == лимиту, не больше
        session = _FakeDownloadSession(resp)
        result = asyncio.run(lumen_tiktok._download_url_bin(session, "https://tikwm.com/exact.jpg"))
        assert result == b"1234567890"
    finally:
        lumen_tiktok.TIKTOK_DOWNLOAD_MAX_BYTES = original_cap


def test_download_url_bin_ignores_malformed_content_length_header():
    # Не-числовой Content-Length не должен ронять функцию исключением — просто
    # пропускаем быстрый путь и полагаемся на потоковую проверку ниже.
    resp = _FakeDownloadResponse([b"ok"], content_length="not-a-number")
    session = _FakeDownloadSession(resp)
    result = asyncio.run(lumen_tiktok._download_url_bin(session, "https://tikwm.com/x.jpg"))
    assert result == b"ok"


def test_download_url_bin_refuses_non_http_scheme_without_fetching():
    # Регрессия AUD-D-003: URL приходят из JSON постороннего сервиса — не-HTTP
    # отбрасываем до запроса (фейк вернул бы байты на что угодно, None доказывает гард).
    session = _FakeDownloadSession(_FakeDownloadResponse([b"should never be fetched"]))
    assert asyncio.run(lumen_tiktok._download_url_bin(session, "file:///etc/passwd")) is None
    assert asyncio.run(lumen_tiktok._download_url_bin(session, "ftp://evil.example/x.mp4")) is None


def test_download_url_bin_returns_none_on_non_200_status():
    resp = _FakeDownloadResponse([b"error page"], status=404)
    session = _FakeDownloadSession(resp)
    result = asyncio.run(lumen_tiktok._download_url_bin(session, "https://tikwm.com/missing.jpg"))
    assert result is None


def test_communicate_process_returns_output_on_success():
    proc = _FakeProc()
    out, err = asyncio.run(lumen_tiktok._communicate_process(proc, timeout=5))
    assert (out, err) == (b"out", b"err")
    assert not proc.killed


def test_communicate_process_kills_hung_process_on_timeout():
    async def run():
        proc = _FakeProc(hang=True)
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await lumen_tiktok._communicate_process(proc, timeout=0.05)
        elapsed = time.monotonic() - started
        assert proc.killed
        assert proc.wait_count >= 1
        return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 30


def test_communicate_process_cleans_up_when_outer_task_cancelled():
    async def run():
        proc = _FakeProc(hang=True)
        task = asyncio.create_task(lumen_tiktok._communicate_process(proc, timeout=5))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(5):
            if proc.killed and task.done():
                break
            await asyncio.sleep(0.01)
        assert proc.killed
        assert task.cancelled()

    asyncio.run(run())


def test_communicate_process_propagates_communicate_failure():
    async def run():
        proc = _FakeProc(fail_communicate=True)
        with pytest.raises(RuntimeError):
            await lumen_tiktok._communicate_process(proc, timeout=5)
        assert proc.wait_count >= 1

    asyncio.run(run())


def test_slideshow_status_uses_localized_key():
    # Статус слайдшоу был захардкожен по-русски — теперь ключ tiktok_dl_slideshow с плейсхолдерами.
    text = bot._t(999451, "tiktok_dl_slideshow", shown=35, total=40)
    assert "35/40" in text
    try:
        assert "слайдов" in text or "slideshow" in text.lower() or "Слайдшоу" in text
    finally:
        bot.chat_state.pop(999451, None)


def test_send_tiktok_music_passes_audio_duration():
    # Без явной длительности Telegram показывает 0:00 — пробинг обязан доезжать до send_audio.
    import lumen_tiktok_flow

    incoming = _FakeIncomingMessage(999452)
    incoming.message_id = 1
    incoming.from_user = SimpleNamespace(language_code="ru")
    sent = {}

    class _AudioBot:
        async def send_audio(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace()

    async def fake_download(session, url, headers=None):
        return b"fake-bytes"

    async def fake_probe(path):
        return 42

    def fake_write_tags(path, title, artist, cover):
        pass

    media_data = {
        "music": "https://tikwm.com/song.mp3",
        "music_info": {"title": "Song", "author": "Auth"},
        "author": {"nickname": "Nick", "unique_id": "@nick"},
    }
    original_download = bot._download_url_bin
    original_write_tags = bot._write_mp3_tags
    original_probe = bot._probe_audio_duration
    original_bot = bot.bot
    bot._download_url_bin = fake_download
    bot._write_mp3_tags = fake_write_tags
    bot._probe_audio_duration = fake_probe
    bot.bot = _AudioBot()
    try:
        asyncio.run(lumen_tiktok_flow._send_tiktok_music(None, media_data, incoming, "Nick", {}))
        assert sent.get("duration") == 42
        assert sent.get("title") == "Song"
    finally:
        bot._download_url_bin = original_download
        bot._write_mp3_tags = original_write_tags
        bot._probe_audio_duration = original_probe
        bot.bot = original_bot
        bot.chat_state.pop(999452, None)


def _run_music_capturing_audio(media_data, chat_id=999454):
    import lumen_tiktok_flow
    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    incoming.from_user = SimpleNamespace(language_code="ru")
    sent = {}

    class _AudioBot:
        async def send_audio(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace()

    async def fake_download(session, url, headers=None):
        return b"fake-bytes"

    def fake_write_tags(path, title, artist, cover):
        pass

    original_download = bot._download_url_bin
    original_write_tags = bot._write_mp3_tags
    original_bot = bot.bot
    bot._download_url_bin = fake_download
    bot._write_mp3_tags = fake_write_tags
    bot.bot = _AudioBot()
    try:
        asyncio.run(lumen_tiktok_flow._send_tiktok_music(None, media_data, incoming, "Nick", {}))
    finally:
        bot._download_url_bin = original_download
        bot._write_mp3_tags = original_write_tags
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)
    return sent


def test_send_tiktok_music_unswaps_tikwm_title_author():
    # Прод 22.09.2026: TikWM иногда кладёт юзернейм в title, а название трека — в author.
    sent = _run_music_capturing_audio({
        "music": "https://tikwm.com/song.mp3",
        "music_info": {"title": "milka_kicm12", "author": "Миля, ты че творишь"},
        "author": {"nickname": "Milka", "unique_id": "milka_kicm12"},
    })
    assert sent.get("title") == "Миля, ты че творишь"
    assert sent.get("performer") == "milka_kicm12"


def test_send_tiktok_music_keeps_one_word_track_title():
    # Однословный настоящий трек НЕ равен хендлу автора — не трогаем.
    sent = _run_music_capturing_audio({
        "music": "https://tikwm.com/song.mp3",
        "music_info": {"title": "Believer", "author": "Imagine Dragons"},
        "author": {"nickname": "Other", "unique_id": "other_user"},
    })
    assert sent.get("title") == "Believer"
    assert sent.get("performer") == "Imagine Dragons"


def test_send_tiktok_music_truly_unnamed_sound_gets_localized_label():
    # Прод 22.09.2026 ([tiktok-music][diag]): raw 'original sound - account2525101295' при
    # author 'account2525101295' — хендл автора звука в заголовке БЕЗЫМЯННОГО звука, а не
    # название. Раньше остаток считался названием и в ТГ уезжало title=performer=хендл.
    sent = _run_music_capturing_audio({
        "music": "https://tikwm.com/song.mp3",
        "music_info": {"title": "original sound - account2525101295", "author": "account2525101295"},
        "author": {"nickname": "VideoPoster", "unique_id": "videoposter"},
    })
    assert sent.get("title") == "Оригинальный звук"
    assert sent.get("performer") == "account2525101295"


def test_send_tiktok_music_named_track_keeping_artist_in_title():
    # Именованный трек, где автор упомянут в заголовке, — остаётся названием, хендл автора
    # вырезается только как довесок, а не целиком.
    sent = _run_music_capturing_audio({
        "music": "https://tikwm.com/song.mp3",
        "music_info": {"title": "Night Call - Kavinsky", "author": "Kavinsky"},
        "author": {"nickname": "VideoPoster", "unique_id": "videoposter"},
    })
    assert sent.get("title") == "Night Call"
    assert sent.get("performer") == "Kavinsky"


def test_send_tiktok_music_retries_without_thumbnail_on_failure():
    # Жирная обложка роняет send_audio целиком — повторяем без неё, трек важнее картинки.
    import lumen_tiktok_flow
    incoming = _FakeIncomingMessage(999455)
    incoming.message_id = 1
    incoming.from_user = SimpleNamespace(language_code="ru")
    attempts = []

    class _FlakyAudioBot:
        async def send_audio(self, **kwargs):
            attempts.append(kwargs)
            if kwargs.get("thumbnail") is not None:
                raise RuntimeError("thumbnail too big")
            return SimpleNamespace()

    async def fake_download(session, url, headers=None):
        return b"fake-bytes"

    def fake_write_tags(path, title, artist, cover):
        pass

    media_data = {
        "music": "https://tikwm.com/song.mp3",
        "music_info": {"title": "Song", "author": "Auth", "cover": "https://tikwm.com/cover.jpg"},
        "author": {"nickname": "Nick", "unique_id": "@nick"},
    }
    original_download = bot._download_url_bin
    original_write_tags = bot._write_mp3_tags
    original_bot = bot.bot
    bot._download_url_bin = fake_download
    bot._write_mp3_tags = fake_write_tags
    bot.bot = _FlakyAudioBot()
    try:
        asyncio.run(lumen_tiktok_flow._send_tiktok_music(None, media_data, incoming, "Nick", {}))
        assert len(attempts) == 2
        assert attempts[0].get("thumbnail") is not None
        assert attempts[1].get("thumbnail") is None
        assert attempts[1].get("title") == "Song"
    finally:
        bot._download_url_bin = original_download
        bot._write_mp3_tags = original_write_tags
        bot.bot = original_bot
        bot.chat_state.pop(999455, None)


def test_single_video_status_deleted_after_music():
    # Статус живёт и во время скачивания музыки тоже — сносится только после неё, а не до.
    import lumen_tiktok_flow

    tikwm_json = {
        "code": 0,
        "data": {
            "play": "https://tikwm.com/sd.mp4", "size": 1000,
            "author": {"nickname": "TestAuthor"},
        },
    }
    incoming = _FakeIncomingMessage(999453)
    incoming.message_id = 1
    fake_bot = _FakeTikTokBot()
    events = []

    async def fake_get_http_session():
        return _FakeTikTokSession(tikwm_json)

    async def fake_resolve(session, url):
        return url

    async def fake_download_url_bin(session, url, headers=None):
        return b"\x00" * 100

    async def fake_probe_dims(path):
        return 0, 0, 0

    async def fake_thumb(path, duration):
        return None

    async def rec_music(*args, **kwargs):
        events.append("music")
        return None

    async def rec_delete(message):
        events.append("delete")
        return True

    original_get_session = bot._get_http_session
    original_resolve = bot._resolve_tiktok_short
    original_download = bot._download_url_bin
    original_probe = bot._probe_video_dimensions
    original_thumb = bot._generate_video_thumbnail
    original_bot = bot.bot
    original_delete = bot._delete_message_quietly
    original_music = lumen_tiktok_flow._send_tiktok_music
    bot._get_http_session = fake_get_http_session
    bot._resolve_tiktok_short = fake_resolve
    bot._download_url_bin = fake_download_url_bin
    bot._probe_video_dimensions = fake_probe_dims
    bot._generate_video_thumbnail = fake_thumb
    bot.bot = fake_bot
    bot._delete_message_quietly = rec_delete
    lumen_tiktok_flow._send_tiktok_music = rec_music
    try:
        asyncio.run(bot.handle_tiktok(incoming, "https://www.tiktok.com/@test/video/123"))
        assert events == ["music", "delete"]
        assert len(fake_bot.sent_videos) == 1
    finally:
        bot._get_http_session = original_get_session
        bot._resolve_tiktok_short = original_resolve
        bot._download_url_bin = original_download
        bot._probe_video_dimensions = original_probe
        bot._generate_video_thumbnail = original_thumb
        bot.bot = original_bot
        bot._delete_message_quietly = original_delete
        lumen_tiktok_flow._send_tiktok_music = original_music

