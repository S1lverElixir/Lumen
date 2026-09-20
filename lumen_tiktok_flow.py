"""
lumen_tiktok_flow.py — оркестрация TikTok-загрузок: отправка музыки/видео/
слайдшоу и разбор ссылок (вынесено из bot.py, P2 аудита). Механика (URL,
пробинг, теги, слайды) живёт в lumen_tiktok.py и импортируется напрямую;
связи с рантаймом bot.py — только через отложенный `import bot` внутри функций.
bot.py реэкспортирует имена — `bot.handle_tiktok` и т.п. в тестах и
`_handle_message_core` не менялись.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from typing import Any

import aiohttp
from aiogram.exceptions import TelegramEntityTooLarge
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from lumen_tiktok import (
    _original_sound_label,
    _GENERIC_ORIGINAL_SOUND_PHRASES,
    _chunk_tiktok_media_items,
    _looks_like_video_bytes,
    _slideshow_slide_urls,
    _tiktok_video_candidates,
    TikTokUserFacingError,
    _tiktok_music_page_id,
)

log = logging.getLogger("bot")

async def _send_tiktok_music(session, media_data: dict, message: Message, author: str, headers: dict) -> None:
    import bot
    music_url = media_data.get("music")
    if not music_url:
         return
         
    try:
         music_bytes = await bot._download_url_bin(session, music_url, headers=headers)
         if not music_bytes:
              return
              
         music_info = media_data.get("music_info") or {}
         raw_music_title = music_info.get("title") or bot._t(message.chat.id, "tiktok_music")
         raw_music_author = music_info.get("author") or author
         
         # юзернейм и никнейм автора ВИДЕО без @ — нужны и для performer_name, и
         # для очистки заголовка ниже (TikTok иногда подставляет их в заголовок
         # безымянного звука вместо настоящего названия).
         author_nick = media_data.get("author", {}).get("nickname") or ""
         author_uniq = media_data.get("author", {}).get("unique_id") or ""
         author_uniq_clean = author_uniq.lstrip("@")
         
         # проверяем, оригинальный ли это звук
         m_title_lower = raw_music_title.lower()
         # ИСПРАВЛЕНО (отладка 11 августа 2026): раньше здесь проверялись только
         # русская и английская фразы буквально — raw_music_title генерируется
         # TikTok на языке АВТОРА ИСХОДНОГО видео (см. комментарий выше), который
         # может быть любым из _ORIGINAL_SOUND_LABELS (например украинским или
         # белорусским), а не только ru/en. Не совпав ни с одной из двух
         # захардкоженных фраз, is_original_sound ошибочно оставался False, и
         # получателю ссылки показывался НЕлокализованный чужой raw-заголовок
         # вместо подписи на ЕГО собственном языке интерфейса Telegram — см.
         # _GENERIC_ORIGINAL_SOUND_PHRASES в lumen_tiktok.py (строится из того
         # же словаря, что и _original_sound_label, единый источник правды).
         mentions_generic_phrase = any(phrase in m_title_lower for phrase in _GENERIC_ORIGINAL_SOUND_PHRASES)
         # НАЙДЕНО ПО ЖИВОМУ ТЕСТИРОВАНИЮ (реальный найденный регресс, часть 2): TikTok
         # разрешает автору дать "оригинальному звуку" СОБСТВЕННОЕ название при публикации
         # видео (см. реальный пример: TikTok показывает такой звук как "Оригинальный
         # звук: Night, Blooming Jasmine." на его собственной странице звука) — при этом
         # TikWM всё равно присылает raw_music_title с префиксом "original sound - "/
         # "оригинальный звук - " ПЕРЕД настоящим названием, а не одно только настоящее
         # название. Поэтому буквальное совпадение фразы "original sound" в заголовке —
         # это ещё НЕ финальный признак "звук совсем безымянный": вырезаем саму фразу
         # (и, если она там же, ник/юзернейм автора ВИДЕО — TikTok в ДЕЙСТВИТЕЛЬНО
         # безымянном случае подставляет в заголовок именно его) и смотрим, остаётся ли
         # после этого что-то ЕЩЁ. Если да — это настоящее, осмысленное название звука,
         # которое нужно показать как есть, а не подменять generic-подписью.
         residual_title = raw_music_title
         for _phrase in _GENERIC_ORIGINAL_SOUND_PHRASES:
              residual_title = re.sub(re.escape(_phrase), "", residual_title, flags=re.IGNORECASE)
         residual_title = residual_title.strip(" \t-–—:")
         if author_nick:
              residual_title = re.sub(re.escape(author_nick), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         if author_uniq_clean:
              residual_title = re.sub(re.escape(author_uniq_clean), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         is_original_sound = mentions_generic_phrase and not residual_title
         # Вычисляем ДО диагностического лога (а не только внутри ветки ниже) —
         # ИСПРАВЛЕНО (отладка 11 августа 2026): раньше language_code получателя
         # нигде не логировался, поэтому при жалобе "подпись не на моём языке"
         # не было возможности проверить по логам, что реально пришло от Telegram
         # (None/другой язык, отличный от того, что человек считает выставленным
         # в настройках приложения) — см. [tiktok-music][diag] ниже.
         sender_language_code = message.from_user.language_code if message.from_user else None

         # Диагностика: пока эта эвристика не "обкатана" на достаточном числе реальных
         # случаев, полезно видеть в /logs исходные поля TikWM целиком при каждом
         # решении — это то самое "сначала факты, потом фикс" вместо повторной догадки.
         log.info(
              "[tiktok-music][diag] raw_title=%r raw_author=%r cover=%r author_avatar=%r "
              "residual_title=%r sender_language_code=%r -> is_original_sound=%s",
              raw_music_title, raw_music_author, music_info.get("cover"),
              media_data.get("author", {}).get("avatar"), residual_title, sender_language_code, is_original_sound,
         )
         
         if is_original_sound:
               # в исполнителях — юзернейм без @
               performer_name = author_uniq_clean if author_uniq_clean else raw_music_author
               # Вместо генерации/очистки сырого raw_music_title от TikWM сразу подставляем
               # перевод по цепочке: язык интерфейса Telegram ИМЕННО отправителя
               # этой конкретной ссылки (персонально — даже в группе у каждого свой)
               # → язык чата из /lang (если у отправителя язык неизвестен) → английский.
               # Это единственный способ показать подпись на "его" языке, раз сам
               # TikTok эту связь не даёт: raw_music_title зависит от языка автора
               # исходного видео, а не от языка человека, приславшего ссылку в наш бот.
               cleaned_title = _original_sound_label(sender_language_code, bot._chat_lang(message.chat.id))
         else:
              # Либо обычный именованный трек с автором (раньше он всегда попадал только
              # сюда), либо "оригинальный звук" с собственным названием (см. комментарий
              # выше) — в обоих случаях реальное название важнее generic-подписи. Если
              # TikWM прислал raw_music_title с префиксом "original sound - "/"оригинальный
              # звук - " перед настоящим названием, используем уже очищенный остаток;
              # иначе (обычный трек без такого префикса) оставляем raw_music_title как есть.
              cleaned_title = residual_title if (mentions_generic_phrase and residual_title) else raw_music_title
              performer_name = raw_music_author
              
         # достаём обложку трека
         cover_url = music_info.get("cover") or music_info.get("avatar") or media_data.get("author", {}).get("avatar")
         cover_bytes = None
         if cover_url:
              try:
                   cover_bytes = await bot._download_url_bin(session, cover_url, headers=headers)
              except Exception as e:
                   log.warning("[tiktok] failed to download cover image: %s", e)
                   
         # готовим превью
         thumbnail_file = None
         if cover_bytes:
              thumbnail_file = BufferedInputFile(cover_bytes, filename="cover.jpg")
              
         # вшиваем метаданные и обложку в MP3 перед отправкой — чтобы теги видели и другие плееры
         tagged_music_bytes = music_bytes
         try:
              with tempfile.TemporaryDirectory() as tmp_dir:
                   tmp_mp3_path = os.path.join(tmp_dir, "music.mp3")
                   with open(tmp_mp3_path, "wb") as f:
                        f.write(music_bytes)
                   bot._write_mp3_tags(tmp_mp3_path, cleaned_title, performer_name, cover_bytes)
                   if os.path.exists(tmp_mp3_path) and os.path.getsize(tmp_mp3_path) > 0:
                        with open(tmp_mp3_path, "rb") as f:
                             tagged_music_bytes = f.read()
         except Exception as tag_err:
              log.warning("[tiktok] failed to write embedded tags to MP3: %s", tag_err)

         # Слэш и управляющие из чужого названия — в "_" (AUD-E-005),
         # иначе multipart-имя файла битое.
         safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", cleaned_title[:60]).strip() or "track"
         await bot.bot.send_audio(
               chat_id=message.chat.id,
               audio=BufferedInputFile(tagged_music_bytes, filename=f"{safe_title}.mp3"),
              title=cleaned_title, 
              performer=performer_name,
              thumbnail=thumbnail_file,
              reply_to_message_id=message.message_id
         )
    except Exception as e:
         log.warning("[tiktok] failed to send music: %s", e)

async def handle_tiktok_sound(message: Message, status: Message | None) -> None:
    """Ссылка на страницу звука TikTok (не видео) — см. комментарий выше по файлу:
    ни TikWM, ни прямой запрос к самой странице TikTok не дают получить звук по
    такой ссылке с текущей инфраструктурой бота. Сразу и честно сообщаем об этом,
    не пытаясь сделать сетевой запрос, который гарантированно ни к чему не приведёт."""
    import bot
    if bot.is_guest_message(message):
         await bot._answer_guest_text(message, bot._t(message.chat.id, "tiktok_sound_recognized"))
         return
    raise TikTokUserFacingError(
        bot._t(message.chat.id, "tiktok_sound_no_separate")
    )


async def _fetch_tikwm_media_data_with_proxy_fallback(session: aiohttp.ClientSession, resolved_url: str, headers: dict) -> dict | None:
    """Пробует прокси-кандидатов TikWM по очереди (см. _tikwm_proxy_candidates) —
    основной адрес, затем резервные из TIKWM_API_BASE_URL_FALLBACKS, до первого
    успеха. Без TIKWM_API_BASE_URL (не задан) — единственный "кандидат" — пустая
    строка, т.е. поведение как раньше (прямые запросы к обоим зеркалам TikWM
    внутри самой _fetch_tikwm_media_data, без изменений)."""
    import bot
    result = None
    for candidate in bot._tikwm_proxy_candidates():
        result = await bot._fetch_tikwm_media_data(session, resolved_url, headers, proxy_base_url=candidate)
        if result is not None:
            return result
    return result


async def _try_send_tiktok_slideshow(
    session: aiohttp.ClientSession, media_data: dict, message: Message, status: Message | None,
    author: str, headers: dict,
) -> bool:
    """Пробует обработать пост как слайдшоу (список `images` в ответе TikWM) —
    вынесено из handle_tiktok при разбиении на именованные шаги (аудит техдолга,
    7 сентября 2026, `bot.py` был самым длинным файлом проекта именно из-за
    handle_tiktok на 313 строк) — тело функции не изменилось ни на строчку,
    только получило имя и явные параметры вместо доступа к локалям handle_tiktok
    напрямую (session/media_data/message/status/author/headers).

    Возвращает True, если слайды реально были отправлены (вызывающий код должен
    завершиться) — False, если `images` в ответе TikWM нет вовсе, либо ни один
    слайд не удалось скачать (тогда вызывающий код продолжает обычной веткой
    одиночного видео, как и раньше).
    """
    import bot
    images = media_data.get("images")
    if images and isinstance(images, list):
         # НАЙДЕНО ПРИ РЕВИЗИИ: раньше здесь стоял срез images[:10] и всё,
         # что не влезало в первые 10 слайдов, просто ТИХО терялось — TikTok
         # официально разрешает до 35 слайдов в одном посте (см.
         # TIKTOK_SLIDESHOW_MAX_ITEMS), sendMediaGroup же ограничен 10 ЗА ОДИН
         # вызов (TELEGRAM_MEDIA_GROUP_CHUNK) — это ограничение Telegram, а не
         # TikTok. Теперь берём весь пост (до официального максимума TikTok) и
         # отправляем несколькими последовательными media group, а не только
         # первую десятку.
         images_to_fetch = images[:bot.TIKTOK_SLIDESHOW_MAX_ITEMS]
         status_text = f"Скачиваю слайдшоу TikTok ({len(images_to_fetch)} слайдов)"
         if len(images) > len(images_to_fetch):
              status_text += f" — показаны первые {len(images_to_fetch)} из {len(images)}"
         await bot._edit_message_quietly(status, status_text)
         # Скачиваем все слайды ПАРАЛЛЕЛЬНО (asyncio.gather), а не
         # последовательно одно за другим — реальный выигрыш в скорости для
         # слайдшоу из нескольких фото: раньше каждое следующее скачивание
         # ждало полного завершения предыдущего, хотя это независимые запросы
         # к разным URL и ничего не мешает вести их одновременно.
         # НАЙДЕНО ПРИ ПОВТОРНОМ АУДИТЕ (4 сентября 2026): комментарий "общий
         # лимит соединений в сессии (limit=40) с запасом покрывает слайдшоу"
         # был верен только для самого факта TCP-соединений, но не защищал
         # ОСТАЛЬНОЙ трафик бота (Gemini/OpenRouter/Pollinations и другие
         # TikTok-запросы делят тот же _get_http_session) от того, что один
         # слайдшоу из 35 слайдов занимает почти весь пул разом — см.
         # _tiktok_slide_download_semaphore выше. Ограничиваем конкурентность
         # именно здесь, а не через сам connector — так лимит применяется
         # только к TikTok-слайдам, не сужая пул для всего остального.
         # НАЙДЕНО ПРИ ПОВТОРНОЙ РЕВИЗИИ (см. _slideshow_slide_urls): для
         # каждого слайда предпочитаем `live_images[i]`, если TikWM его
         # отдаёт — по логам подтверждено, что `images[i]` для этого поста
         # всегда статичный `...photomode-image.jpeg`, а `play`/`hdplay`
         # (прежняя, ОШИБОЧНАЯ эвристика) указывают на аудиодорожку, а не
         # на видео — убраны из рассмотрения полностью.
         fetch_urls = _slideshow_slide_urls(media_data, images_to_fetch)

         async def _download_slide_bounded(slide_url: str) -> bytes | None:
              async with bot._tiktok_slide_download_semaphore:
                   return await bot._download_url_bin(session, slide_url, headers=headers)

         downloaded = list(await asyncio.gather(
              *(_download_slide_bounded(u) for u in fetch_urls)
         ))
         video_indices = [idx for idx, b in enumerate(downloaded) if b and _looks_like_video_bytes(b)]
         if video_indices:
              log.info('[tiktok] In the slideshow, %d of %d slides were recognized as video (live_images/magic bytes).', len(video_indices), len(downloaded))
         # Пробинг длительности/размеров/превью для видео-слайдов — ПАРАЛЛЕЛЬНО
         # для всех сразу (asyncio.gather), а не по очереди: каждый ffprobe/
         # ffmpeg-вызов занимает время, и при нескольких видео-слайдах в одном
         # слайдшоу последовательный перебор заметно увеличил бы общее время
         # ответа без необходимости — эти вызовы независимы друг от друга.
         probe_results: dict[int, tuple[int, int, int, bytes | None]] = {}
         if video_indices:
              async def _probe_bounded(item_bytes: bytes) -> tuple[int, int, int, bytes | None]:
                   async with bot._tiktok_probe_semaphore:
                        return await bot._probe_and_thumbnail_from_bytes(item_bytes)
              probed = await asyncio.gather(*(_probe_bounded(downloaded[i]) for i in video_indices))
              probe_results = dict(zip(video_indices, probed))
         media_items: list[Any] = []
         for idx, item_bytes in enumerate(downloaded):
              if not item_bytes:
                   continue
              if idx in probe_results:
                   # Видео-слайд внутри слайдшоу (см. _looks_like_video_bytes) —
                   # отправляем как реальное видео, а не статичный кадр; звук не
                   # обрабатываем отдельно — у таких слайдов его обычно и нет в
                   # исходнике, Telegram просто покажет клип без звука как есть.
                   # НАЙДЕНО ПРИ РЕВИЗИИ: duration/width/height/thumbnail теперь
                   # прокидываются так же, как и для обычного цельного TikTok-
                   # видео (см. handle_tiktok ниже) — без них Telegram иногда не
                   # умел сам распознать длительность контейнера видео-слайда.
                   duration, width, height, thumb_bytes = probe_results[idx]
                   video_kwargs: dict[str, Any] = {
                        "media": BufferedInputFile(item_bytes, filename=f"slide_{idx}.mp4"),
                        "supports_streaming": True,
                   }
                   if duration:
                        video_kwargs["duration"] = duration
                   if width and height:
                        video_kwargs["width"] = width
                        video_kwargs["height"] = height
                   if thumb_bytes:
                        video_kwargs["thumbnail"] = BufferedInputFile(thumb_bytes, filename=f"slide_{idx}_thumb.jpg")
                   media_items.append(InputMediaVideo(**video_kwargs))
              else:
                   media_items.append(InputMediaPhoto(media=BufferedInputFile(item_bytes, filename=f"photo_{idx}.jpg")))
         if media_items:
              await bot._delete_message_quietly(status)
              if len(media_items) == 1:
                   # sendMediaGroup требует МИНИМУМ 2 элемента (см.
                   # _chunk_tiktok_media_items) — единственный уцелевший слайд
                   # (например, если остальные не удалось скачать) отправляем
                   # обычным send_photo/send_video, а не media group.
                   only_item = media_items[0]
                   if isinstance(only_item, InputMediaVideo):
                        await bot.bot.send_video(
                             chat_id=message.chat.id, video=only_item.media,
                             supports_streaming=True, reply_to_message_id=message.message_id,
                        )
                   else:
                        await bot.bot.send_photo(
                             chat_id=message.chat.id, photo=only_item.media,
                             reply_to_message_id=message.message_id,
                        )
              else:
                   # Разбиваем на группы по TELEGRAM_MEDIA_GROUP_CHUNK (10),
                   # НЕ допуская хвостовой группы из 1 элемента (см.
                   # _chunk_tiktok_media_items — жёсткое требование Telegram
                   # 2-10 элементов НА группу). Первая группа идёт как ответ
                   # на исходное сообщение со ссылкой, остальные — обычными
                   # сообщениями сразу следом (как и у сравнимых ботов: "10
                   # медиа первым блоком, остальные — вторым").
                   chunks = _chunk_tiktok_media_items(media_items)
                   for chunk_idx, chunk in enumerate(chunks):
                        await bot.bot.send_media_group(
                             chat_id=message.chat.id, media=chunk,
                             reply_to_message_id=message.message_id if chunk_idx == 0 else None,
                        )
                        if chunk_idx + 1 < len(chunks):
                             # Небольшая пауза между блоками — вежливость по
                             # отношению к анти-флуд лимитам Telegram при
                             # нескольких media group подряд в одном чате, не
                             # влияет на восприятие скорости пользователем
                             # (доли секунды).
                             await asyncio.sleep(0.3)
              await _send_tiktok_music(session, media_data, message, author, headers)
              return True
    return False

async def _send_tiktok_single_video(
    session: aiohttp.ClientSession, media_data: dict, message: Message, status: Message | None,
    author: str, headers: dict,
) -> None:
    """Пробует кандидатов на скачивание одиночного видео по убыванию качества
    (HD -> стандартное -> с водяным знаком) — та же ветка handle_tiktok, что
    раньше срабатывала для постов без слайдшоу (см. `_try_send_tiktok_slideshow`
    выше и докстринг там же про сам факт разбиения). Тело не изменилось ни на
    строчку. Возвращает None при успешной отправке; поднимает
    TikTokUserFacingError, если ни один вариант качества не подошёл (слишком
    большой файл или TikTok вообще ничего не отдал по этой ссылке).
    """
    import bot
    # НАЙДЕНО ПРИ РЕВИЗИИ: раньше здесь бралось РОВНО одно качество —
    # media_data.get("play") or media_data.get("wmplay") — то есть бот всегда
    # отдавал стандартное (не HD) видео, даже когда у TikWM реально была версия
    # получше (см. _tiktok_video_candidates и добавленный параметр &hd=1 выше).
    # Теперь пробуем кандидатов по убыванию качества: HD → стандартное → (самый
    # последний резерв) с водяным знаком — и если Telegram всё же отклонит
    # конкретный файл как слишком большой, автоматически пробуем следующий,
    # более лёгкий вариант, а не сдаёмся сразу.
    video_candidates = _tiktok_video_candidates(media_data)
    if video_candidates:
         hit_size_limit = False
         for candidate in video_candidates:
              if candidate["size"] and candidate["size"] > bot.TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES:
                   # Известный заранее размер (hd_size/size/wm_size из ответа
                   # TikWM) уже больше лимита Telegram — не тратим время и
                   # трафик на заведомо обречённое скачивание, сразу переходим
                   # к следующему, более лёгкому варианту качества. Помечаем
                   # hit_size_limit=True уже здесь (а не только при реальном
                   # TelegramEntityTooLarge ниже) — НАЙДЕНО ПРИ ПОВТОРНОЙ
                   # РЕВИЗИИ: если ВСЕ качества оказываются известно большими
                   # ещё до попытки скачивания, без этого пользователь получил
                   # бы вводящее в заблуждение "контент удалён или недоступен"
                   # вместо честного "видео слишком большое".
                   hit_size_limit = True
                   log.info(
                        '[tiktok] Skipping variant %s (%s) — known size %.1f MB exceeds the Telegram Bot API limit.',
                        candidate["key"], candidate["label"], candidate["size"] / (1024 * 1024),
                   )
                   continue
              if candidate["key"] == "hdplay":
                   status_msg = bot._t(message.chat.id, "tiktok_dl_hd")
              elif candidate["key"] == "wmplay":
                   status_msg = bot._t(message.chat.id, "tiktok_dl_as_is")
              else:
                   status_msg = bot._t(message.chat.id, "tiktok_dl_plain")
              await bot._edit_message_quietly(status, status_msg)

              video_bytes = await bot._download_url_bin(session, candidate["url"], headers=headers)
              if not video_bytes:
                   continue

              duration, width, height = 0, 0, 0
              thumb_bytes = None
              try:
                   with tempfile.TemporaryDirectory() as tdir:
                        raw_path = os.path.join(tdir, "raw_tiktok.mp4")
                        with open(raw_path, "wb") as f:
                             f.write(video_bytes)
                        # Telegram не всегда сам умеет вытащить длительность/размеры
                        # из TikTok-контейнера — передаём их явно вместе с превью,
                        # иначе видео показывается как "нераспознанный файл" (0:00,
                        # без плеера, только кнопка "скачать").
                        duration, width, height = await bot._probe_video_dimensions(raw_path)
                        thumb_bytes = await bot._generate_video_thumbnail(raw_path, duration)
              except Exception as probe_exc:
                   log.warning("[tiktok] Video metadata probe failed, sending without: %s", probe_exc)

              send_kwargs: dict[str, Any] = {
                   "chat_id": message.chat.id,
                   "video": BufferedInputFile(video_bytes, filename="tiktok.mp4"),
                   "reply_to_message_id": message.message_id,
                   "supports_streaming": True,
              }
              if duration:
                   send_kwargs["duration"] = duration
              if width and height:
                   send_kwargs["width"] = width
                   send_kwargs["height"] = height
              if thumb_bytes:
                   send_kwargs["thumbnail"] = BufferedInputFile(thumb_bytes, filename="thumb.jpg")

              try:
                   await bot.bot.send_video(**send_kwargs)
              except TelegramEntityTooLarge:
                   # Реальный размер оказался больше лимита Telegram, хотя
                   # известный заранее size/hd_size либо не пришёл в ответе
                   # TikWM, либо оказался неточным — не сдаёмся сразу, пробуем
                   # следующий (более лёгкий) вариант качества по списку, пока
                   # он не закончится (тогда см. hit_size_limit ниже).
                   hit_size_limit = True
                   log.warning(
                        '[tiktok] Variant %s (%s, %d bytes) exceeded the Telegram limit when sending — trying the next quality option.',
                        candidate["key"], candidate["label"], len(video_bytes),
                   )
                   continue

              await bot._delete_message_quietly(status)
              await _send_tiktok_music(session, media_data, message, author, headers)
              return

         if hit_size_limit:
              # Хотя бы один вариант реально скачался, но НИ ОДИН (включая
              # самый лёгкий из доступных) не прошёл по размеру в Telegram —
              # это стоит явно отличать от "TikTok вообще ничего не отдал"
              # ниже, иначе пользователь получит вводящее в заблуждение
              # сообщение про "контент удалён", хотя видео на самом деле есть,
               # просто слишком большое для отправки через бота.
               raise TikTokUserFacingError(
                   bot._t(message.chat.id, "tiktok_too_big")
               )

    raise TikTokUserFacingError(bot._t(message.chat.id, "tiktok_no_media"))

async def handle_tiktok(message: Message, url: str) -> None:
    import bot
    if bot.is_guest_message(message):
         await bot._answer_guest_text(message, f"Ссылка на TikTok распознана: {url}")
         return
    status = await bot._tg_call(message.reply, bot._t(message.chat.id, "tiktok_processing"))
    try:
         session = await bot._get_http_session()
         resolved_url = await bot._resolve_tiktok_short(session, url)
         if "?" in resolved_url:
              resolved_url = resolved_url.split("?")[0]

         headers = {
              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
              "Accept": "application/json"
         }

         # Ссылка на страницу ЗВУКА (не видео) — см. handle_tiktok_sound выше.
         # Проверяем ПОСЛЕ разрешения короткой ссылки (vt.tiktok.com/... и т.п.),
         # т.к. до разрешения мы ещё не знаем реальный путь на tiktok.com.
         music_page_id = _tiktok_music_page_id(resolved_url)
         if music_page_id:
              await handle_tiktok_sound(message, status)
              return

         # &hd=1 — БЕЗ этого параметра TikWM не гарантирует присутствие поля
         # `hdplay` (HD-версия без водяных знаков) в ответе вообще; раньше он не
         # передавался, и бот молча всегда скачивал видео в обычном качестве, даже
         # когда у TikWM реально была версия получше (см. _tiktok_video_candidates).
         #
         # ИСПРАВЛЕНО (отладка 11-13 августа 2026, реальный инцидент — оба зеркала TikWM
         # стабильно отвечали HTTP 403 почти на любую ссылку): троттлинг, ретраи, валидация
         # резолвленного URL (см. _looks_like_resolved_tiktok_url) и заголовки Referer/Origin
         # — НЕ помогли, 403 с пустым телом продолжал приходить даже при полностью корректном
         # URL и правильно разнесённых по времени запросах. Причина подтверждена вручную —
         # блокировка исходящего IP HF Spaces (см. подробный диагностический комментарий в
         # _fetch_tikwm_media_data). TIKWM_API_BASE_URL — единственное реально работающее
         # решение: запрос уходит через выделенный прокси с другого IP (см. proxy/proxy.ts).
         media_data = await _fetch_tikwm_media_data_with_proxy_fallback(session, resolved_url, headers)

         if not media_data:
              raise TikTokUserFacingError(bot._t(message.chat.id, "tiktok_fetch_fail"))

         # ДИАГНОСТИКА структуры ответа TikWM для постов со слайдшоу — оставлена
         # постоянно (не одноразово): структура `images`/`live_images` теперь
         # понятна и подтверждена реальными тестами (см. _slideshow_slide_urls),
         # но лог продолжает быть полезен для мониторинга — например, если
         # TikWM когда-нибудь изменит формат ответа или появится пост с ещё не
         # виденной структурой (несовпадающая длина live_images и т.п.).
         if images_debug := media_data.get("images"):
              log.info(
                   '[tikwm][diag] slideshow post: response keys=%s, images(%d items)=%s, live_images=%s, top-level play=%s hdplay=%s wmplay=%s',
                   sorted(media_data.keys()), len(images_debug), images_debug, media_data.get("live_images"),
                   media_data.get("play"), media_data.get("hdplay"), media_data.get("wmplay"),
              )

         author = (media_data.get("author") or {}).get("nickname") or bot._t(message.chat.id, "tiktok_author")

         if await _try_send_tiktok_slideshow(session, media_data, message, status, author, headers):
              return
         await _send_tiktok_single_video(session, media_data, message, status, author, headers)
    except Exception as exc:
         if isinstance(exc, (TelegramEntityTooLarge, TikTokUserFacingError)):
              # Известные, уже обработанные исходы (видео приватное/удалено/слишком
              # большое) — не баг, а обычный ответ TikTok/Telegram, для которого
              # пользователь и так получает понятный текст ниже. log.exception (ERROR)
              # безусловно на КАЖДОЕ исключение здесь заводило issue в Sentry даже на
              # эти рутинные случаи — см. LUMEN-1 (аудит логирования): 24 события за
              # месяц оказались сплошь этим классом, что маскирует реальные новые баги
              # среди ожидаемого шума. log.warning без трейсбека — достаточно, чтобы
              # видеть частоту в bot.log, не засоряя Sentry.
              log.warning("TikTok download failed with a known, already-handled outcome: %s", exc)
         else:
              # Действительно неожиданное исключение (сетевое/библиотечное и т.п.) —
              # остаётся log.exception с полным трейсбеком, попадает в Sentry как и раньше.
              log.exception("TikTok download fail:")
         if isinstance(exc, TelegramEntityTooLarge):
              err_text = bot._t(message.chat.id, "tiktok_too_big")
         elif isinstance(exc, TikTokUserFacingError):
              # Наши собственные ошибки (см. raise TikTokUserFacingError выше по функции)
              # уже написаны как цельные самодостаточные предложения для пользователя.
              # ИСПРАВЛЕНО (ревизия TikTok-скачивания): раньше текст оборачивался в
              # f"Не получилось скачать видео: {str(exc)}" — при том что сами сообщения
              # уже начинаются со слова "видео"/"ссылка" и т.п., это давало неестественный
              # повтор вида "Не получилось скачать видео: Это видео из TikTok...".
              # Показываем текст как есть, без дополнительной обёртки.
              err_text = str(exc)
         else:
              # Сырые сетевые/библиотечные исключения пользователю не показываем
              # (см. log.exception выше) — та же логика, что и в остальных
              # обработчиках ошибок бота.
              err_text = bot._t(message.chat.id, "tiktok_generic_fail")
         edited = await bot._edit_message_quietly(status, err_text)
         if not edited:
              # status уже мог быть удалён раньше (например, перед отправкой видео) —
              # редактирование тихо проваливается, и без этой подстраховки пользователь
              # не увидит вообще никакого сообщения об ошибке.
              await bot._safe_reply(message, err_text)
