"""
lumen_tiktok_flow.py — оркестрация TikTok-загрузок (музыка/видео/слайдшоу). Механика — в lumen_tiktok.py; связи с bot.py — только через отложенный `import bot`.
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

        # Ник/юзернейм автора видео без @ — для performer и очистки заголовка (TikTok подставляет их в безымянный звук).
         author_nick = media_data.get("author", {}).get("nickname") or ""
         author_uniq = media_data.get("author", {}).get("unique_id") or ""
         author_uniq_clean = author_uniq.lstrip("@")

        # Проверяем по всем языкам словаря (11.08.2026: раньше только ru/en — заголовок генерится на языке автора видео, is_original_sound ложно падал).
         m_title_lower = raw_music_title.lower()
         mentions_generic_phrase = any(phrase in m_title_lower for phrase in _GENERIC_ORIGINAL_SOUND_PHRASES)
         # Но "original sound" в заголовке — ещё не признак безымянности: TikTok разрешает дать такому звуку СВОЁ название ("Оригинальный звук: Night, Blooming Jasmine"), а TikWM шлёт его с префиксом. Вырезаем фразу и ник автора — остался ли осмысленный остаток.
         residual_title = raw_music_title
         for _phrase in _GENERIC_ORIGINAL_SOUND_PHRASES:
              residual_title = re.sub(re.escape(_phrase), "", residual_title, flags=re.IGNORECASE)
         residual_title = residual_title.strip(" \t-–—:")
         if author_nick:
              residual_title = re.sub(re.escape(author_nick), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         if author_uniq_clean:
              residual_title = re.sub(re.escape(author_uniq_clean), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         is_original_sound = mentions_generic_phrase and not residual_title
         # language_code получателя — в диагн. лог: при жалобе "подпись не на моём языке" иначе не проверить, что пришло от Telegram.
         sender_language_code = message.from_user.language_code if message.from_user else None

         # Диагностика: исходные поля TikWM при каждом решении ("сначала факты, потом фикс").
         log.info(
              "[tiktok-music][diag] raw_title=%r raw_author=%r cover=%r author_avatar=%r "
              "residual_title=%r sender_language_code=%r -> is_original_sound=%s",
              raw_music_title, raw_music_author, music_info.get("cover"),
              media_data.get("author", {}).get("avatar"), residual_title, sender_language_code, is_original_sound,
         )

         if is_original_sound:
               # в исполнителях — юзернейм без @. Безымянный звук подменяем переводом на язык ОТПРАВИТЕЛЯ ссылки (raw зависит от языка автора видео, связи TikTok не даёт).
               performer_name = author_uniq_clean if author_uniq_clean else raw_music_author
               cleaned_title = _original_sound_label(sender_language_code, bot._chat_lang(message.chat.id))
         else:
              # Именованный трек или "оригинальный" с названием — реальное название важнее подписи; очищенный остаток — если был префикс.
              cleaned_title = residual_title if (mentions_generic_phrase and residual_title) else raw_music_title
              performer_name = raw_music_author
              # TikWM иногда привозит поля перевёрнутыми (прод 22.09.2026: в title лежал юзернейм
              # автора видео, в author — название трека; по той же ссылке в другой раз — наоборот).
              # Чиним очевидный случай: название без единого пробела совпало с хендлом автора видео,
              # а в "авторе" — фраза с пробелами. Однословный настоящий трек под правило не попадает:
              # он не равен хендлу автора.
              _title_handle = cleaned_title.strip().lower().lstrip("@")
              _known_handles = {h for h in (author_uniq_clean.lower(), author_nick.lower()) if h}
              if _title_handle in _known_handles and re.search(r"\s", performer_name or ""):
                   log.info(
                        "[tiktok-music][diag] Swapped music title/author from TikWM (title=%r looked like author handle, author=%r looked like a title) — unswapping.",
                        cleaned_title, performer_name,
                   )
                   cleaned_title, performer_name = performer_name, author_uniq_clean or author_nick or performer_name

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
         music_duration = 0
         try:
              with tempfile.TemporaryDirectory() as tmp_dir:
                   tmp_mp3_path = os.path.join(tmp_dir, "music.mp3")
                   with open(tmp_mp3_path, "wb") as f:
                        f.write(music_bytes)
                   bot._write_mp3_tags(tmp_mp3_path, cleaned_title, performer_name, cover_bytes)
                   if os.path.exists(tmp_mp3_path) and os.path.getsize(tmp_mp3_path) > 0:
                        with open(tmp_mp3_path, "rb") as f:
                             tagged_music_bytes = f.read()
                        # Длительность — явно: без неё Telegram показывает 0:00 (та же история, что была с видео).
                        music_duration = await bot._probe_audio_duration(tmp_mp3_path)
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
              duration=music_duration if music_duration > 0 else None,
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
    """Слайдшоу из images TikWM. True — слайды отправлены, False — нет images или ничего не скачалось, вызывающий код идёт веткой видео."""
    import bot
    images = media_data.get("images")
    if images and isinstance(images, list):
          # TikTok разрешает до 35 слайдов, Telegram — 10 за вызов: берём весь пост и шлём несколькими группами.
         images_to_fetch = images[:bot.TIKTOK_SLIDESHOW_MAX_ITEMS]
         # Статус слайдшоу — через переводы (был захардкожен по-русски).
         await bot._edit_message_quietly(status, bot._t(
             message.chat.id, "tiktok_dl_slideshow",
             shown=len(images_to_fetch), total=len(images),
         ))
          # Качаем слайды параллельно gather; конкурентность режем семафором, чтобы не занимать весь пул сессии.
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
                    # Видео-слайд — как видео со звуком-как-есть, плюс те же duration/size/thumbnail, что у цельного видео (иначе Telegram не распознаёт контейнер).
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
              # Статус держим до конца (включая музыку ниже) — иначе человек висит в тишине, пока качается трек.
              if len(media_items) == 1:
                    # Один уцелевший слайд — обычным send_photo/send_video: media group требует минимум 2.
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
                    # Группы по 10 без хвоста из 1 (жёсткое требование Telegram 2–10); первая — ответом на ссылку.
                   chunks = _chunk_tiktok_media_items(media_items)
                   for chunk_idx, chunk in enumerate(chunks):
                        await bot.bot.send_media_group(
                             chat_id=message.chat.id, media=chunk,
                             reply_to_message_id=message.message_id if chunk_idx == 0 else None,
                        )
                        if chunk_idx + 1 < len(chunks):
                              # Пауза между группами — против анти-флуда.
                             await asyncio.sleep(0.3)
              await _send_tiktok_music(session, media_data, message, author, headers)
              await bot._delete_message_quietly(status)
              return True
    return False

async def _send_tiktok_single_video(
    session: aiohttp.ClientSession, media_data: dict, message: Message, status: Message | None,
    author: str, headers: dict,
) -> None:
    """Пробуем качества HD → обычное → с водяным; EntityTooLarge — следующий вариант, не сдаёмся."""
    import bot
    # Качества HD → обычное → с водяным (раньше без HD); EntityTooLarge — следующий вариант.дальше.
    video_candidates = _tiktok_video_candidates(media_data)
    if video_candidates:
         hit_size_limit = False
         for candidate in video_candidates:
              if candidate["size"] and candidate["size"] > bot.TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES:
                    # Заранее большой размер пропускаем без скачивания и помечаем hit_size_limit, чтобы дать честное "слишком большое".
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
                        # Длительность/размеры — явно: TikTok-контейнер Telegram сам не всегда разбирает (иначе "файл" 0:00).
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
                   # Реальный размер больше лимита, хотя заявленный был неточен — пробуем следующий, более лёгкий вариант.
                   hit_size_limit = True
                   log.warning(
                        '[tiktok] Variant %s (%s, %d bytes) exceeded the Telegram limit when sending — trying the next quality option.',
                        candidate["key"], candidate["label"], len(video_bytes),
                   )
                   continue

              await _send_tiktok_music(session, media_data, message, author, headers)
              await bot._delete_message_quietly(status)
              return

         if hit_size_limit:
              # Скачался, но ни один вариант не влез в лимит — отличаем от "TikTok ничего не отдал" (иначе вводящее "контент удалён").
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

         # Страница ЗВУКА проверяется ПОСЛЕ резолва короткой ссылки — до него реальный путь неизвестен.
         music_page_id = _tiktok_music_page_id(resolved_url)
         if music_page_id:
              await handle_tiktok_sound(message, status)
              return

          # &hd=1 — для поля hdplay. 403 с пустым телом при корректном URL 11-13.08.2026 — блокировка IP HF, ходим через прокси.KWM_API_BASE_URL.
         media_data = await _fetch_tikwm_media_data_with_proxy_fallback(session, resolved_url, headers)

         if not media_data:
              raise TikTokUserFacingError(bot._t(message.chat.id, "tiktok_fetch_fail"))

          # Лог структуры images/live_images — видна смена формата TikWM.
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
              # Известные исходы — warning без Sentry, неожиданные — exception с трейсбеком (LUMEN-1).
              log.warning("TikTok download failed with a known, already-handled outcome: %s", exc)
          else:
               # Действительно неожиданное исключение (сетевое/библиотечное и т.п.) —
              # остаётся log.exception с полным трейсбеком, попадает в Sentry как и раньше.
              log.exception("TikTok download fail:")
          if isinstance(exc, TelegramEntityTooLarge):
               err_text = bot._t(message.chat.id, "tiktok_too_big")
          elif isinstance(exc, TikTokUserFacingError):
               err_text = str(exc)
          else:
               # Сырые исключения — generic-текст (та же логика, что в остальных обработчиках).
              err_text = bot._t(message.chat.id, "tiktok_generic_fail")
          edited = await bot._edit_message_quietly(status, err_text)
          if not edited:
               # status мог быть удалён — дублируем реплаем, иначе юзер ничего не увидит.
              await bot._safe_reply(message, err_text)
