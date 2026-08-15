"""
lumen_tiktok.py — самодостаточная механика TikTok-загрузчика: локализация подписи
"оригинальный звук", разбиение слайдшоу на группы для sendMediaGroup, детект
видео-слайдов по магическим байтам, выбор URL слайда (live_images vs images), выбор
кандидата на скачивание видео по качеству, разбор ссылки на страницу звука, теги MP3,
ffmpeg-пробинг длительности/превью и скачивание бинарных URL.

Вынесено из bot.py при разбиении на модули (см. README, аудит техдолга) — это ровно те
части TikTok-загрузчика, которые НЕ зовут Telegram напрямую (ни `bot.send_*`, ни `_tg_call`,
ни объекты `Message`) и поэтому переносятся как есть, без параметризации: чистые функции
над байтами/словарями/URL плюс несколько тонких обёрток над `aiohttp`/`ffmpeg`/`mutagen`,
принимающих сессию/пути явными параметрами (как и раньше).

`handle_tiktok`/`handle_tiktok_sound`/`_send_tiktok_music` — сама оркестрация скачивания
и отправки в Telegram — остаются в bot.py: они вызывают `bot.send_video`/`send_photo`/
`send_media_group`/`send_audio` и `_tg_call`, то есть неотделимы от глобального `bot` и
Telegram-специфичных хелперов bot.py, и вынос их сюда потребовал бы либо тащить эти
зависимости в новый модуль, либо превращать каждый вызов в колбэк — накладные расходы,
не стоящие выигрыша для этой пары функций (см. тот же принцип, применённый к
`_tg_call`/`telegram_api_call` в lumen_telegram_transport.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import time
import urllib.parse
from typing import Any

import aiohttp
from mutagen.id3 import APIC, ID3, TPE1, TIT2
from mutagen.mp3 import MP3

log = logging.getLogger("bot")


# ─────────────────── локализация подписи "оригинальный звук" ───────────────────
# TikWM отдаёт название "оригинального звука" (music_info.title) либо на английском
# ("original sound"), либо на языке автора ИСХОДНОГО видео, породившего этот звук —
# то есть никак не связано с языком человека, который прислал ссылку В НАШ бот.
# Telegram передаёт язык интерфейса КАЖДОГО отправителя в message.from_user.
# language_code (IETF-тег вроде "ru"/"uk"/"en-US") — это язык, который сам человек
# выбрал в настройках Telegram, приходит с любым сообщением без доп. разрешений и
# не требует ничего от пользователя. Используем именно его (а не raw_music_title
# от TikWM), чтобы подпись "оригинальный звук"/"original sound"/... совпадала с
# языком ТОГО, кто прислал конкретную ссылку — даже в группе, где разные участники
# могут иметь разный язык интерфейса.
#
# Список языков — приоритет отдан региону СНГ/ближнего зарубежья (основная
# аудитория бота), плюс крупные европейские и соседние языки. Любой язык, которого
# нет в словаре, тихо откатывается на английский вариант (нейтральный и понятный
# дефолт, а не гадание с неизвестным алфавитом).
_ORIGINAL_SOUND_LABELS: dict[str, str] = {
    "ru": "Оригинальный звук",
    "uk": "Оригінальний звук",
    "be": "Арыгінальны гук",
    # НАЙДЕНО ПО ВОПРОСУ ВЛАДЕЛЬЦА: единая схема регистра для всех языков — с
    # большой буквы у первого слова (Sentence case), как и положено названию
    # трека (это поле идёт в MP3-тег "название", т.е. в тот же слот, где обычно
    # показывается настоящее название песни — оно тоже всегда с большой буквы).
    # Раньше английский вариант был строчным ("original sound") по инерции от
    # того, как сам TikTok показывает его в своём интерфейсе — но раз это теперь
    # НАША подпись, а не дословная копия чужого UI, приводим её к тому же виду,
    # что и остальные языки, а не оставляем единственным исключением.
    "en": "Original sound",
    "pl": "Oryginalny dźwięk",
    "de": "Originalton",
    "es": "Sonido original",
    "fr": "Son original",
    "it": "Audio originale",
    "pt": "Som original",
    "tr": "Orijinal ses",
    "kk": "Түпнұсқа дыбыс",
    "uz": "Original tovush",
    "az": "Orijinal səs",
    "ka": "ორიგინალური ხმა",
    "hy": "Օրիգինալ ձայն",
    "ky": "Түпнуска үн",
    "ar": "الصوت الأصلي",
}
_ORIGINAL_SOUND_LABEL_DEFAULT = _ORIGINAL_SOUND_LABELS["en"]

# Фразы для распознавания "это безымянный оригинальный звук" в _send_tiktok_music
# (bot.py) — раньше там проверялись только русская и английская фразы буквально.
# raw_music_title в ответе TikWM генерируется TikTok на языке автора ИСХОДНОГО
# видео (см. комментарий выше), который почти никогда не совпадает с языком
# получателя конкретной ссылки — если у видео-автора, например, украинский или
# белорусский интерфейс, raw-заголовок придёт как "оригінальний звук"/"арыгінальны
# гук" и т.п., а не "оригинальный звук"/"original sound". Старая проверка на эти
# случаи не срабатывала вообще: is_original_sound оставался False, и получателю
# показывался НЕлокализованный (чужого языка) raw-заголовок вместо подписи на его
# собственном языке интерфейса. Строится из ТЕХ ЖЕ значений _ORIGINAL_SOUND_LABELS
# (единый источник правды) — новый язык в словаре автоматически появляется и здесь.
_GENERIC_ORIGINAL_SOUND_PHRASES: tuple[str, ...] = tuple(sorted({v.lower() for v in _ORIGINAL_SOUND_LABELS.values()}))


def _original_sound_label(language_code: str | None) -> str:
    """Возвращает локализованную подпись "оригинальный звук" по IETF-коду языка
    (например, из message.from_user.language_code). Код языка может приходить с
    региональным уточнением (например "en-US", "pt-BR") — берём только первичный
    подтег до дефиса. Неизвестный/отсутствующий код — тихий откат на английский."""
    if not language_code:
        return _ORIGINAL_SOUND_LABEL_DEFAULT
    primary = language_code.split("-", 1)[0].strip().lower()
    return _ORIGINAL_SOUND_LABELS.get(primary, _ORIGINAL_SOUND_LABEL_DEFAULT)


# ─────────────────── разбиение слайдшоу на группы sendMediaGroup ───────────────────

TELEGRAM_MEDIA_GROUP_CHUNK = 10


def _chunk_tiktok_media_items(items: list, chunk_size: int = TELEGRAM_MEDIA_GROUP_CHUNK) -> list[list]:
    """Разбивает список медиа-элементов слайдшоу на группы для sendMediaGroup.

    НАЙДЕНО ПРИ ПОВТОРНОЙ РЕВИЗИИ (КРИТИЧНО): у Telegram Bot API `sendMediaGroup`
    жёсткое требование — от 2 до 10 элементов НА ОДИН вызов, а не просто "не
    больше 10". Наивное разбиение "по chunk_size без остатка" (см. предыдущую
    версию этого кода) даёт хвостовую группу РОВНО из ОДНОГО элемента всякий
    раз, когда общее число элементов даёт остаток 1 при делении на chunk_size
    (11, 21, 31 элемент и т.п. — а слайдшоу TikTok реально может состоять из
    любого числа слайдов вплоть до 35, так что это не гипотетический случай).
    Такой вызов Telegram гарантированно отклоняет как невалидный — причём это
    произошло бы уже ПОСЛЕ того, как предыдущие группы успешно отправились,
    то есть пользователь получил бы часть слайдшоу и затем непонятную ошибку.

    Если наивное разбиение даёт хвост из 1 элемента — "занимаем" один элемент у
    предпоследней группы, превращая последние две группы из (chunk_size, 1) в
    (chunk_size - 1, 2). Единственный элемент целиком (0 или 1 элементов на
    входе) эта функция не обрабатывает — такие случаи вызывающий код (handle_tiktok
    в bot.py) должен отправлять напрямую через send_photo/send_video, а не через эту
    функцию/sendMediaGroup вообще."""
    if not items:
        return []
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    if len(chunks) >= 2 and len(chunks[-1]) == 1:
        borrowed = chunks[-2].pop()
        chunks[-1].insert(0, borrowed)
    return chunks


def _looks_like_video_bytes(data: bytes) -> bool:
    """Определяет, что скачанный файл — это видео (MP4/MOV/ISO base media file
    format), а не статичная картинка, по магическим байтам начала файла.

    НАЙДЕНО ПРИ РЕВИЗИИ: TikTok официально разрешает комбинировать в одном
    слайдшоу-посте (photo mode) обычные статичные фото-слайды И короткие
    видео-слайды (TikTok сам называет это "combine photo and video slides").
    TikWM отдаёт URL такого видео-слайда в том же списке `images`, что и обычные
    фото — без явного признака "это видео", и Content-Type в ответе CDN для
    таких слайдов тоже не всегда достоверен. Раньше такой URL молча скачивался
    и оборачивался в InputMediaPhoto — в лучшем случае Telegram показывал статичный
    кадр вместо реального движения слайда (то, что пользователь называет
    TikTok-'живым фото'), в худшем — вовсе не мог корректно отрендерить не-JPEG/
    PNG/WEBP байты как фото.

    Проверяем сигнатуру ISO base media file format ("ftyp" на смещении 4 байта) —
    это надёжный и стандартный способ отличить MP4/MOV-контейнер от растрового
    изображения без сторонних библиотек, не зависящий от того, как именно TikWM
    называет поле в JSON."""
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _slideshow_slide_urls(media_data: dict, images_to_fetch: list[str]) -> list[str]:
    """Для каждого слайда слайдшоу возвращает URL, который реально стоит скачать —
    предпочитая `live_images[i]` вместо `images[i]`, если TikWM отдал непустую
    запись на этой позиции.

    НАЙДЕНО (по логам диагностики) и ПОДТВЕРЖДЕНО на реальных постах: у ответа
    TikWM для фото-поста ЕСТЬ отдельное поле `live_images` помимо обычного
    `images`. Прежняя эвристика (см. историю — пробовала верхнеуровневые
    `play`/`hdplay`) была основана на неверном предположении: для фото-постов
    эти поля указывают НЕ на видео, а на ту же самую фоновую музыку, что и поле
    `music` (реальный найденный URL содержал `mime_type=audio_mpeg` на домене
    `...music.tiktokcdn...`), поэтому убрана целиком. `images[]` всегда отдаёт
    статичные `...~tplv-photomode-image.jpeg` кадры — то есть настоящую "живую"
    (двигающуюся) версию слайда, если она есть у этого поста, даёт именно
    `live_images`.

    ПОДТВЕРЖДЕНО РЕАЛЬНЫМИ ТЕСТАМИ (см. /logs с реальных постов): позиционное
    соответствие `live_images[i]` <-> `images[i]` верно — например, для поста с
    2 слайдами, где только один реально "живой", `live_images` пришёл как
    `[None, "<url c mime_type=video_mp4>"]` (ровно на позиции живого слайда),
    и итоговый детект (`_looks_like_video_bytes` после скачивания) корректно
    показал "1 из 2 слайдов — видео". Для постов, где живые оба слайда или
    только один из одного — тоже совпало 1-в-1. Пустая/отсутствующая запись на
    позиции означает "этот слайд не живой, обычное статичное фото" — на этот
    случай функция просто продолжает использовать `images[i]`."""
    live_images = media_data.get("live_images")
    if not isinstance(live_images, list):
        return images_to_fetch
    resolved: list[str] = []
    for idx, fallback_url in enumerate(images_to_fetch):
        live_url = live_images[idx] if idx < len(live_images) else None
        resolved.append(live_url if isinstance(live_url, str) and live_url.strip() else fallback_url)
    return resolved


def _tiktok_video_candidates(media_data: dict) -> list[dict[str, Any]]:
    """Строит список кандидатов на скачивание видео TikTok в порядке убывания
    качества: HD без водяных знаков → стандартное без водяных знаков → (только
    как самый последний резерв, если вообще ничего другого нет) видео с водяным
    знаком.

    НАЙДЕНО ПРИ РЕВИЗИИ: раньше запрос к TikWM не передавал параметр hd=1, и код
    брал только `media_data.get("play") or media_data.get("wmplay")` — то есть
    ВСЕГДА уходило видео в обычном (не HD) качестве без водяных знаков, даже когда
    у TikWM реально есть более качественная версия (`hdplay`). См. добавленный
    `&hd=1` в tikwm_endpoints в handle_tiktok (bot.py) — без него поле `hdplay` в
    ответе вообще не гарантированно присутствует.

    TikWM вместе с каждой ссылкой отдаёт реальный размер файла в байтах
    (`hd_size`/`size`/`wm_size`) — используем его, чтобы сразу пропустить вариант,
    который заведомо не пролезет в лимит Telegram Bot API на загрузку (см.
    TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES в bot.py), а не тратить время и трафик на
    скачивание файла, который всё равно не отправится. Если размер не пришёл в
    ответе (0 или отсутствует — TikWM не всегда его отдаёт) — не отбрасываем
    вариант заранее, просто пробуем; на случай реального превышения лимита
    handle_tiktok сам ловит TelegramEntityTooLarge и переходит к следующему
    кандидату по качеству."""
    candidates: list[dict[str, Any]] = []
    for url_key, size_key, label in (
        ("hdplay", "hd_size", "HD"),
        ("play", "size", "стандартное"),
        ("wmplay", "wm_size", "с водяным знаком — резерв"),
    ):
        raw_url = media_data.get(url_key)
        if not raw_url:
            continue
        if not raw_url.startswith("http"):
            raw_url = "https://www.tikwm.com" + raw_url
        try:
            size_bytes = int(media_data.get(size_key) or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        candidates.append({"key": url_key, "url": raw_url, "size": size_bytes, "label": label})
    return candidates


# ─────────────────── запрос к TikWM API (троттлинг + ретрай при 403) ───────────────────
# НАЙДЕНО ПРИ ОТЛАДКЕ (11 августа 2026, реальный инцидент по логам Sentry): почти
# каждая ссылка на TikTok стала отвечать "Не удалось получить видео по этой ссылке"
# — оба зеркала TikWM (www.tikwm.com и tikwm.com) отвечали HTTP 403 на один и тот же
# запрос, причём second-попытка (второе зеркало) уходила буквально через ~36мс после
# первой. По независимому наблюдению сторонних инструментов над публичным TikWM API
# (см. описание userscript'а "TikWM TikTok Batch Downloader" на greasyfork.org) у
# TikWM есть фактический лимит порядка 1 запроса/сек — наш же код раньше стрелял
# в оба зеркала практически одновременно БЕЗ единой паузы между ними на КАЖДОЙ
# ссылке, что само по себе гарантированно нарушает такой лимит. `_tikwm_throttle`
# ниже — общий (на весь процесс, а не на чат) минимальный интервал между ЛЮБЫМИ
# двумя исходящими запросами к TikWM, включая оба зеркала одной и той же ссылки и
# параллельные запросы из разных чатов.
#
# HTTP 403 специально отличается от "TikWM понял запрос, но видео нет" (тот случай
# отдаёт HTTP 200 с `code != 0`, см. _fetch_tikwm_media_data ниже) — 403 означает,
# что нас не пустили на уровне самого HTTP-запроса, а не что конкретное видео
# недоступно. Поэтому если 403 пришёл от ОБОИХ зеркал подряд — это, скорее всего,
# срабатывание троттлинга/временной блокировки, а не факт "видео действительно
# недоступно", и стоит попробовать ещё раз после паузы, а не сразу сдаваться.
_TIKWM_MIN_INTERVAL_SEC = 1.1
_TIKWM_RETRY_BACKOFF_SEC = 2.0
_tikwm_last_request_ts: float | None = None
_tikwm_throttle_lock = asyncio.Lock()
# Точка подмены для тестов (тот же приём, что и `bot._typing_sleep`/`bot._get_http_session`
# в остальном проекте) — реальные секунды ожидания не нужны ни одному тесту.
_sleep = asyncio.sleep


async def _tikwm_throttle() -> None:
    """Дожидается минимального интервала с прошлого запроса к TikWM (см. секцию
    выше). `_tikwm_last_request_ts` намеренно стартует как None, а не 0.0 — с
    буквальным 0.0 самый первый вызов в свежем процессе мог бы ошибочно решить,
    что "с последнего запроса прошло меньше интервала" (та же ловушка, что уже
    была найдена в этом проекте для сброса дневной квоты — см. комментарии в
    test_bot.py про time.monotonic() не гарантированно "далеко за" нулём в
    коротко живущем процессе). None однозначно means "запросов ещё не было —
    ждать нечего"."""
    global _tikwm_last_request_ts
    async with _tikwm_throttle_lock:
        now = time.monotonic()
        if _tikwm_last_request_ts is not None:
            wait = _tikwm_last_request_ts + _TIKWM_MIN_INTERVAL_SEC - now
            if wait > 0:
                await _sleep(wait)
                now = time.monotonic()
        _tikwm_last_request_ts = now


async def _fetch_tikwm_media_data(session: aiohttp.ClientSession, resolved_url: str, headers: dict, *, hd: bool = True) -> dict | None:
    """Запрашивает метаданные поста TikTok у публичного API TikWM, пробуя оба
    известных зеркала (www.tikwm.com и tikwm.com) — см. секцию выше про троттлинг
    и почему 403 от обоих зеркал заслуживает одной повторной попытки. Возвращает
    `data` из ответа при успехе (`code == 0`) или None, если ни одно зеркало не
    отдало рабочих данных даже после повторной попытки.

    Ответ с ЛЮБЫМ статусом, кроме 200, логируется вместе с телом ответа (а не
    только кодом статуса) — без текста самой ошибки TikWM невозможно отличить
    временную проблему от постоянной при следующем подобном инциденте.

    НАЙДЕНО ПРИ ПОВТОРНОЙ ОТЛАДКЕ (12 августа 2026): даже полностью корректный,
    заново провалидированный URL (см. _looks_like_resolved_tiktok_url) и
    корректно разнесённые по времени запросы (throttle+retry выше — оба реально
    сработали в реальном инциденте, см. историю правок) всё равно стабильно
    получали HTTP 403 с ПОЛНОСТЬЮ ПУСТЫМ телом от ОБОИХ зеркал. Это не похоже
    на обычную ошибку самого приложения TikWM (та приходит как HTTP 200 с JSON
    {"code":..., "msg":...}, см. ветку ниже) — пустое тело на 403 гораздо больше
    похоже на блокировку на уровне прокси/WAF/файрвола ПЕРЕД TikWM (тот же класс
    проблемы, что уже задокументирован в README для YouTube — блокировка
    датацентровых IP HF Spaces), чем на что-либо, что чинится тайминг- или URL-
    правками на нашей стороне. `Referer`/`Origin`, имитирующие вызов со страницы
    самого tikwm.com — распространённая, но НЕ гарантированная техника обхода
    подобных анти-скрейпинг проверок; честно говоря, из песочницы разработки нет
    возможности проверить исходящую сеть до tikwm.com напрямую, поэтому эффект
    этого изменения можно подтвердить только по реальному продакшен-трафику."""
    api_headers = {
        **headers,
        "Referer": "https://www.tikwm.com/",
        "Origin": "https://www.tikwm.com",
        "Accept": "application/json, text/plain, */*",
    }
    quoted = urllib.parse.quote(resolved_url)
    endpoints = [
        f"https://www.tikwm.com/api/?url={quoted}&hd=1" if hd else f"https://www.tikwm.com/api/?url={quoted}",
        f"https://tikwm.com/api/?url={quoted}&hd=1" if hd else f"https://tikwm.com/api/?url={quoted}",
    ]
    # Остаётся True только если ВООБЩЕ каждая попытка (оба зеркала, оба раунда)
    # была именно "403 + пустое тело" — ни одной другой ошибки/статуса/исключения
    # среди них не было. Используется только для диагностического лога ниже —
    # намеренно узкий сигнал (не срабатывает на смешанную картину ошибок), чтобы
    # не путать реальный признак блокировки с обычной нестабильностью сети.
    all_attempts_403_empty = True
    any_attempt_made = False
    for retry_round in range(2):
        saw_403 = False
        for api_url in endpoints:
            await _tikwm_throttle()
            any_attempt_made = True
            try:
                async with session.get(api_url, timeout=12, headers=api_headers) as r:
                    if r.status == 200:
                        all_attempts_403_empty = False
                        res = await r.json(content_type=None)
                        if res.get("code") == 0 and isinstance(res.get("data"), dict):
                            log.info("[tikwm] Successfully fetched media data from %s", api_url)
                            return res.get("data")
                        log.warning("[tikwm] Endpoint %s returned code %s: %s", api_url, res.get("code"), res.get("msg"))
                    else:
                        body = await r.read()
                        if r.status == 403:
                            saw_403 = True
                        if r.status != 403 or body:
                            all_attempts_403_empty = False
                        log.warning("[tikwm] Endpoint %s returned status %s: %r", api_url, r.status, body[:300])
            except Exception as e:
                all_attempts_403_empty = False
                log.warning("[tikwm] Request failed for endpoint %s: %s", api_url, e)
        if not saw_403 or retry_round == 1:
            break
        log.warning(
            "[tikwm] Оба зеркала вернули 403 подряд — похоже на срабатывание троттлинга TikWM "
            "(~1 запрос/сек), а не на реально недоступное видео. Пробую ещё раз через %.1fс.",
            _TIKWM_RETRY_BACKOFF_SEC,
        )
        await _sleep(_TIKWM_RETRY_BACKOFF_SEC)
    if any_attempt_made and all_attempts_403_empty:
        log.warning(
            "[tikwm][diag] ВСЕ попытки (оба зеркала, с троттлингом и повторным раундом) вернули "
            "HTTP 403 с ПУСТЫМ телом — url=%s. URL корректно резолвлен, запросы разнесены по "
            "времени — это не похоже на обычную временную ошибку. Похоже на блокировку исходящего "
            "IP этого сервера на уровне прокси/WAF перед TikWM (см. README про аналогичный "
            "задокументированный случай с YouTube), которую тайминг/URL-правки на нашей стороне "
            "не могут обойти. Проверить гипотезу: тот же запрос к TikWM с ДРУГОГО IP (не HF Spaces).",
            resolved_url,
        )
    return None


class TikTokUserFacingError(RuntimeError):
    """Ошибка TikTok-загрузки с текстом, уже написанным для пользователя (см. raise
    в функции handle_tiktok в bot.py). ВАЖНО для будущих правок: любой raise этого
    класса должен содержать ТОЛЬКО чистый русский текст без внутренних деталей/сырых
    исключений — except-блок в handle_tiktok показывает str(exc) пользователю as-is,
    без дополнительной проверки содержимого. Обычный RuntimeError (не этот подкласс)
    считается "сырым" и пользователю не показывается — см. except Exception там же."""


# ─────────────────── ссылка на страницу звука (не видео) ───────────────────
# НАЙДЕНО ПО РЕАЛЬНЫМ ЛОГАМ (см. /logs владельца): если зайти в приложении TikTok
# не на видео, а на сам ЗВУК (карточка "название звука" под видео → тап → "Поделиться"),
# скопированная ссылка выглядит как https://www.tiktok.com/music/original-sound-7666630127215823637
# — числовой ID звука в конце после последнего дефиса. Основной эндпоинт TikWM
# (`/api/?url=`, единственный, которым пользуется остальной код этого файла) на
# такие ссылки отвечает "Url parsing is failed! Please check url." — он умеет
# парсить только ссылки на видео/фото-посты, не на страницы звука.
#
# ИСТОРИЯ ДВУХ ПРОВАЛИВШИХСЯ ПОПЫТОК (обе подтверждены реальным тестированием,
# см. логи владельца, — не гипотетические, а фактически проверенные и опровергнутые):
# 1) Предположение, что TikWM принимает голый числовой ID видео вместо полной
#    ссылки, и что у "оригинальных звуков" ID звука совпадает с ID видео-источника.
#    Опровергнуто: TikWM отвечает "Url parsing is failed!" на голый числовой ID
#    ВСЕГДА, и для именованных песен, и для настоящих оригинальных звуков.
# 2) Прямой запрос страницы tiktok.com/music/... и парсинг встроенного в неё JSON
#    (<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">, структура подтверждена по
#    исходникам github.com/davidteather/TikTok-Api). HTML реально скачивался (200,
#    ~330 КБ, JSON внутри валидный), НО __DEFAULT_SCOPE__ содержал только служебные
#    ключи (seo.abtest, webapp.a-b, webapp.app-context, webapp.biz-context,
#    webapp.i18n-translation) — БЕЗ какого-либо ключа с данными о звуке вообще.
#    Это не баг парсинга — TikTok в принципе не прислал контентные данные на этот
#    запрос, что похоже на то же самое, известное по многим независимым источникам,
#    выборочное урезание страницы для дата-центровых/подозрительных IP (bot-scoring),
#    — ровно та же причина, по которой в этом проекте уже отключено скачивание с
#    YouTube (см. README, "Известные ограничения"). Раз сама страница не содержит
#    нужных данных на этом хостинге, никакая правка регулярных выражений/путей в
#    JSON это не исправит — поэтому эта попытка полностью убрана, а не оставлена
#    как "иногда работает".
#
# ВЫВОД: скачать звук ОТДЕЛЬНО по одной лишь ссылке на его страницу с текущей
# инфраструктурой бота (TikWM + без прокси/резидентных IP для скрапинга самого
# TikTok) не получится — сразу честно сообщаем об этом, не тратя время и сетевые
# попытки на заведомо обречённый запрос. Единственный реально рабочий путь
# получить именно этот звук — прислать ссылку на любое ВИДЕО с ним (обычный,
# давно работающий путь через TikWM, см. handle_tiktok/_send_tiktok_music в bot.py).
_TIKTOK_MUSIC_PAGE_RE = re.compile(r"/music/\S*-(\d{6,})/?$", re.IGNORECASE)


def _tiktok_music_page_id(url: str) -> str | None:
    """Возвращает числовой ID из ссылки на страницу звука TikTok, если это вообще
    ссылка такого типа (используется только как признак "это страница звука, а не
    видео" — см. handle_tiktok в bot.py), либо None для обычных ссылок на видео/
    фото-пост."""
    m = _TIKTOK_MUSIC_PAGE_RE.search(url)
    return m.group(1) if m else None


# ─────────────────── теги MP3 ───────────────────

def _write_mp3_tags(path: str, title: str, artist: str, cover: bytes | None) -> None:
    try:
        audio = MP3(path, ID3=ID3)
        with contextlib.suppress(Exception):
             audio.add_tags()
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        if cover:
            mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
            audio.tags["APIC"] = APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover)
        audio.save()
    except Exception as exc:
        log.warning("[tags] Writer tags failed: %s", exc)


# ─────────────────── ffmpeg и скачивание бинарных URL ───────────────────

async def _download_url_bin(session: aiohttp.ClientSession, url: str, headers: dict | None = None) -> bytes | None:
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        }
    try:
        async with session.get(url, headers=headers, timeout=60) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        log.warning("[download] Failed to download URL: %s", e)
    return None


async def _probe_video_dimensions(path: str) -> tuple[int, int, int]:
    """Возвращает (duration_seconds, width, height). Без этих полей Telegram иногда
    не может сам распознать видео и показывает его как "сырой файл" с 0:00 вместо
    нормального плеера с превью — особенно для нестандартно смукшированных MP4
    (TikTok, например, не всегда ставит faststart-флаг)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        width = height = 0
        duration = 0
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("width="):
                width = int(float(line.split("=", 1)[1] or 0))
            elif line.startswith("height="):
                height = int(float(line.split("=", 1)[1] or 0))
            elif line.startswith("duration="):
                raw = line.split("=", 1)[1]
                if raw and raw != "N/A":
                    duration = max(1, round(float(raw)))
        return duration, width, height
    except Exception as e:
        log.warning("[ffmpeg] Video probe failed: %s", e)
        return 0, 0, 0


async def _generate_video_thumbnail(path: str, duration: int) -> bytes | None:
    """Достаёт один кадр из видео как JPEG-превью для Telegram."""
    seek_at = min(1.0, max(0.0, duration / 2)) if duration else 0.5
    try:
        with tempfile.TemporaryDirectory() as tdir:
            thumb_path = os.path.join(tdir, "thumb.jpg")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", str(seek_at), "-i", path,
                "-frames:v", "1", "-vf", "scale=320:-1", thumb_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                with open(thumb_path, "rb") as f:
                    return f.read()
    except Exception as e:
        log.warning("[ffmpeg] Thumbnail generation failed: %s", e)
    return None


async def _probe_and_thumbnail_from_bytes(video_bytes: bytes) -> tuple[int, int, int, bytes | None]:
    """Обёртка над _probe_video_dimensions/_generate_video_thumbnail для уже
    скачанных В ПАМЯТИ байтов видео (а не файла на диске) — нужна видео-слайдам
    TikTok-слайдшоу (см. handle_tiktok/_looks_like_video_bytes). НАЙДЕНО ПРИ
    РЕВИЗИИ: у обычного цельного TikTok-видео уже применяется этот же приём
    (Telegram не всегда сам умеет вытащить длительность/размеры из TikTok-
    контейнера без явной передачи их вместе с превью, см. handle_tiktok в bot.py) —
    видео-слайды внутри слайдшоу используют тот же CDN и, вероятно, ту же
    особенность контейнера, но раньше отправлялись вообще без этих метаданных."""
    duration, width, height = 0, 0, 0
    thumb_bytes = None
    try:
        with tempfile.TemporaryDirectory() as tdir:
            raw_path = os.path.join(tdir, "slide.mp4")
            with open(raw_path, "wb") as f:
                f.write(video_bytes)
            duration, width, height = await _probe_video_dimensions(raw_path)
            thumb_bytes = await _generate_video_thumbnail(raw_path, duration)
    except Exception as probe_exc:
        log.warning("[tiktok] Video-slide metadata probe failed, sending without: %s", probe_exc)
    return duration, width, height, thumb_bytes


# ─────────────────── проверка "это реально резолвленный URL поста?" ───────────────────
# НАЙДЕНО ПРИ ОТЛАДКЕ (12 августа 2026, реальный инцидент — Sentry-трейс, /logs
# владельца): TikWM стабильно отвечал HTTP 403 с ПУСТЫМ телом на короткую ссылку
# vt.tiktok.com, даже после троттлинга и повторной попытки (см. _fetch_tikwm_media_
# data выше) — значит дело не в скорости запросов. Реальный URL, ушедший в TikWM:
# "https://www.tiktok.com/@/photo/7512093374153772309" — юзернейм между "@" и "/"
# ПУСТОЙ. Причина — в _resolve_tiktok_short ниже: старая проверка результата HEAD-
# запроса ("video" in resolved or "@" in resolved) слишком слабая — голый символ
# "@" в такой строке есть, поэтому проверка засчитывала HEAD-результат успешным,
# даже когда TikTok (или анти-бот прослойка перед ним — датацентровые IP HF Spaces
# ей известны, см. README про уже задокументированный аналогичный случай с YouTube)
# в ответ на HEAD отдал URL без настоящего юзернейма. Из-за этого GET-фоллбек ниже
# (который мог бы пройти больше редиректов и получить нормальный URL) даже не
# пробовался — HEAD "успешно" вернул битый URL, и на этом резолвинг заканчивался.
_RESOLVED_TIKTOK_POST_RE = re.compile(r"tiktok\.com/@[^/\s]+/(?:video|photo)/\d+", re.IGNORECASE)


def _looks_like_resolved_tiktok_url(url: str) -> bool:
    """True, только если url реально похож на канонический адрес конкретного
    поста TikTok (видео ИЛИ фото-слайдшоу) с НЕПУСТЫМ юзернеймом — то есть
    короткая ссылка (vt.tiktok.com/vm.tiktok.com) действительно довелась до
    финального адреса, а не до промежуточной/урезанной/сервисной страницы.
    Пустая ссылка (None/"") или отсутствие непустого сегмента между "@" и "/" —
    False; вызывающий код (_resolve_tiktok_short) в этом случае не принимает
    такой результат сразу, а пробует резолвить ещё раз через GET."""
    return bool(url) and bool(_RESOLVED_TIKTOK_POST_RE.search(url))


async def _resolve_tiktok_short(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        async with session.head(url, allow_redirects=True, timeout=8, headers=headers) as resp:
             if resp.status < 400:
                  resolved = str(resp.url)
                  if _looks_like_resolved_tiktok_url(resolved):
                       return resolved
    except Exception as e:
         log.warning("[tiktok] HEAD resolution failed: %s", e)

    try:
        async with session.get(url, allow_redirects=True, timeout=10, headers=headers) as resp:
             resolved = str(resp.url)
             if not _looks_like_resolved_tiktok_url(resolved):
                  # НАЙДЕНО ПРИ ОТЛАДКЕ (12 августа 2026, реальный инцидент — см.
                  # комментарий у _looks_like_resolved_tiktok_url ниже): и GET-фоллбек
                  # тоже может не довести резолвинг до нормального URL поста. Раньше
                  # это никак не логировалось — итоговый (возможно, битый) URL молча
                  # уходил в TikWM, и единственным следом оставался малопонятный 403
                  # уже НА СТОРОНЕ TikWM, без единой зацепки, что проблема началась
                  # раньше, на этапе резолвинга короткой ссылки.
                  log.warning(
                       "[tiktok] Резолвинг короткой ссылки %s не дал похожего на пост URL "
                       "ни через HEAD, ни через GET (итог: %s) — передаю как есть, TikWM "
                       "может отказать.", url, resolved,
                  )
             return resolved
    except Exception as e:
         log.warning("[tiktok] GET resolution failed: %s", e)

    return url
