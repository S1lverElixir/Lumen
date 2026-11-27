"""lumen_tiktok_flow.py — orchestration of TikTok downloads (music/video/slideshow). Mechanics live in lumen_tiktok.py; links to bot.py are only through a deferred `import bot`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import time
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
    TIKTOK_DOWNLOAD_MAX_BYTES,
    _original_sound_label,
    _GENERIC_ORIGINAL_SOUND_PHRASES,
    _chunk_tiktok_media_items,
    _looks_like_video_bytes,
    _slideshow_slide_urls,
    _tiktok_video_candidates,
    _write_file_sync,
    TikTokUserFacingError,
    _tiktok_music_page_id,
    _communicate_process,
    _fetch_dsp_track_metadata,
    _log_safe_url,
)
from lumen_status import StatusMessage

# Telegram limits per file: photos 10 MB, videos 50 MB. A slide is downloaded with
# with a fallback up to TIKTOK_DOWNLOAD_MAX_BYTES; discard a slide whose type overflows,
# otherwise Telegram rejects the entire group at once (external audit
# 16.11.2026).
_TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024
_TG_VIDEO_MAX_BYTES = 50 * 1024 * 1024


def _static_image_extension(data: bytes) -> str | None:
    """Format of a static slide determined by bytes, not by the URL or Content-Type from TikWM."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _status_message(status: "StatusMessage | Message | None"):
    """The actual status message for error edits (an actor or a bare Message)."""
    return status.message if isinstance(status, StatusMessage) else status


async def _status_set(status: "StatusMessage | Message | None", text: str) -> None:
    """Change the status phrase without stopping the animation: a literal for the actor, an ordinary edit for a bare message (tests and the legacy path)."""
    import bot
    if isinstance(status, StatusMessage):
        await status.set_action(text)
    elif status is not None:
        await bot._edit_message_quietly(status, text)


async def _status_stop(status: "StatusMessage | Message | None") -> None:
    if isinstance(status, StatusMessage):
        await status.stop()


async def _status_delete(status: "StatusMessage | Message | None") -> None:
    import bot
    if isinstance(status, StatusMessage):
        await status.delete()
    else:
        await bot._delete_message_quietly(status)

log = logging.getLogger("bot")


def _write_and_tag_mp3_sync(path: str, music_bytes: bytes, title: str, artist: str, cover: bytes | None) -> None:
    import bot
    with open(path, "wb") as f:
        f.write(music_bytes)
    bot._write_mp3_tags(path, title, artist, cover)


def _read_file_sync(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _fit_audio_thumbnail(cover_bytes: bytes) -> bytes | None:
    """Cover for send_audio: Telegram accepts JPEG up to 320×320 and 200 KB.
    TikTok returns 720–1080 px, which Telegram silently ignores —
    compress it with ffmpeg. If ffmpeg is missing or fails, return the original
    (the previous behavior)."""
    if not cover_bytes:
        return None
    try:
        with tempfile.TemporaryDirectory() as tdir:
            src = os.path.join(tdir, "cover.jpg")
            dst = os.path.join(tdir, "thumb.jpg")
            await asyncio.to_thread(_write_file_sync, src, cover_bytes)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src,
                "-vf", "scale=320:320:force_original_aspect_ratio=increase,crop=320:320",
                "-frames:v", "1", "-q:v", "5", dst,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Do not use wait_for(proc.wait()): that left ffmpeg alive on two cores and holding
            # an already deleted temporary file (proc.kill on timeout/cancellation can only
            # _communicate_process — audit 01.11.2026).
            await _communicate_process(proc, timeout=15)
            if proc.returncode == 0 and os.path.exists(dst):
                out = await asyncio.to_thread(_read_file_sync, dst)
                if out and len(out) <= 200 * 1024:
                    return out
    except Exception as exc:
        log.warning("[tiktok] cover resize failed, using original (error=%s)", type(exc).__name__)
    return cover_bytes

async def _send_tiktok_music(session, media_data: dict, message: Message, author: str, headers: dict) -> None:
    import bot
    # TikWM returns external JSON: a corrupted field must not break MP3 delivery.
    # Copies go to DSP search, so it too sees only safe types.
    author = author if isinstance(author, str) else ""
    music_info = media_data.get("music_info")
    music_info = dict(music_info) if isinstance(music_info, dict) else {}
    for key in ("title", "author", "cover", "avatar"):
         if not isinstance(music_info.get(key), str):
              music_info[key] = ""
    video_author = media_data.get("author")
    video_author = dict(video_author) if isinstance(video_author, dict) else {}
    for key in ("nickname", "unique_id", "avatar"):
         if not isinstance(video_author.get(key), str):
              video_author[key] = ""
    media_data = {**media_data, "music_info": music_info, "author": video_author}
    music_url = media_data.get("music")
    if not music_url:
         return

    # DSP metadata (TikTok -> iTunes) does not depend on mp3: run it in parallel
    # with downloading, so as not to add 1-6s after it (audit 29.10.2026).
    dsp_task = asyncio.create_task(_fetch_dsp_track_metadata(session, media_data))
    try:
         async with bot._media_download_slot():
              # The cap is Telegram's sending limit, not the default 75 MB: a track larger
              # send_audio will not accept it at all, and downloading, ffprobe, and a temporary file
              # would already have burned silently (the same fix as for video — audit 01.11.2026).
              music_bytes = await bot._download_url_bin(
                  session, music_url, headers=headers,
                  cap_bytes=bot.TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES,
              )
         if not music_bytes:
              return

         raw_music_title = music_info.get("title") or bot._t(message.chat.id, "tiktok_music")
         raw_music_author = music_info.get("author") or author

        # Author's nickname/username without @, for performer and title cleanup (TikTok
        #     inserts them into unnamed audio).
         author_nick = video_author.get("nickname") or ""
         author_uniq = video_author.get("unique_id") or ""
         author_uniq_clean = author_uniq.lstrip("@")

        # Check all dictionary languages (11.08.2026: previously only ru/en — the title is generated in the video author's language, so is_original_sound falsely dropped).
         m_title_lower = raw_music_title.lower()
         mentions_generic_phrase = any(phrase in m_title_lower for phrase in _GENERIC_ORIGINAL_SOUND_PHRASES)
         # But "original sound" in the title is not proof that it is unnamed: TikTok
         # allows a custom name for such audio ("Original sound: Night, Blooming Jasmine"),
         # while TikWM sends it with a prefix. We cut the phrase and the author name,
         # then check whether a meaningful remainder is left.
         residual_title = raw_music_title
         for _phrase in _GENERIC_ORIGINAL_SOUND_PHRASES:
              residual_title = re.sub(re.escape(_phrase), "", residual_title, flags=re.IGNORECASE)
         residual_title = residual_title.strip(" \t-–—:")
         # Read the full handle BEFORE the nickname: the nickname is its prefix, and after removal
         # "Hhh" from "hhh31721" left the tail "31721", which was accepted as
         # the track title (prod 28.10.2026: the audio was named "31721 — Hhh").
         if author_uniq_clean:
              residual_title = re.sub(re.escape(author_uniq_clean), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         if author_nick:
              residual_title = re.sub(re.escape(author_nick), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         # The numeric suffix of a handle is not a track title.
         if residual_title.isdigit():
              residual_title = ""
         # Also remove the audio author's handle: TikTok inserts it into the title
         # an unnamed sound (prod 22.09.2026); otherwise the remainder was treated as the title.
         if raw_music_author:
              residual_title = re.sub(re.escape(raw_music_author), "", residual_title, flags=re.IGNORECASE).strip(" \t-–—:")
         is_original_sound = mentions_generic_phrase and not residual_title
         # Log the recipient's language_code for diagnostics: otherwise, after a “caption is not in my language” complaint, there is no way to check what Telegram sent.
         sender_language_code = message.from_user.language_code if message.from_user else None

         # Diagnostics: original TikWM fields on every decision ('facts first, then the fix').
         # Do not write signed URLs to the log, only their presence (audit M7, 10.2026).
         log.info(
              "[tiktok-music][diag] raw_title_len=%d raw_author_len=%d has_cover=%s has_avatar=%s "
              "residual_title_len=%d sender_language_len=%d -> is_original_sound=%s",
              len(raw_music_title), len(raw_music_author), bool(music_info.get("cover")),
              bool(video_author.get("avatar")), len(residual_title),
              len(sender_language_code) if isinstance(sender_language_code, str) else 0, is_original_sound,
         )

         if is_original_sound:
               # in performers — the author of the AUDIO (not the video: other posts reuse the audio).
               performer_name = raw_music_author or author_uniq_clean
               cleaned_title = _original_sound_label(sender_language_code, bot._chat_lang(message.chat.id))
         else:
              # A named track or an "original" with a title — the real title matters more than the label; use the cleaned remainder if there was a prefix.
              cleaned_title = residual_title if (mentions_generic_phrase and residual_title) else raw_music_title
              performer_name = raw_music_author
              # "Title - Artist" in one field: the artist is sent separately in performer and removed from the end of the title (otherwise it is duplicated twice).
              if raw_music_author:
                   _stripped = re.sub(r"\s+[–—-]\s+" + re.escape(raw_music_author) + r"\s*$", "", cleaned_title, flags=re.IGNORECASE).strip()
                   if _stripped:
                        cleaned_title = _stripped
              # TikWM sometimes returns title/author reversed (prod 22.09.2026).
              # Fix the obvious case: a title without spaces matched the handle,
              # but in "the author" there is a phrase with spaces.
              _title_handle = cleaned_title.strip().lower().lstrip("@")
              _known_handles = {h for h in (author_uniq_clean.lower(), author_nick.lower()) if h}
              if _title_handle in _known_handles and re.search(r"\s", performer_name or ""):
                   log.info(
                        "[tiktok-music][diag] Swapped music title/author from TikWM (title matched an author handle while author contained spaces) — unswapping.",
                   )
                   cleaned_title, performer_name = performer_name, author_uniq_clean or author_nick or performer_name

         # Exact metadata for a licensed track: TikTok → tt2dsp → iTunes.
         # Without them, for commercial music TikWM returns "original sound - handle"
         # and an avatar instead of the real title/artist/cover.
         dsp_track = None
         try:
              dsp_track = await dsp_task
         except Exception as dsp_exc:
              log.info("[tiktok-music] DSP metadata lookup skipped (error=%s)", type(dsp_exc).__name__)
         if dsp_track:
              cleaned_title = dsp_track["title"]
              performer_name = dsp_track.get("artist") or performer_name

         # fetch the track cover: for preview only, so use a small cap instead of
         # the default 75MB (audit M6, 10.2026).
         cover_url = music_info.get("cover") or music_info.get("avatar") or video_author.get("avatar")
         if dsp_track and dsp_track.get("cover"):
              cover_url = dsp_track["cover"]
         cover_bytes = None
         if cover_url:
              try:
                   async with bot._media_download_slot():
                        cover_bytes = await bot._download_url_bin(session, cover_url, headers=headers, cap_bytes=2 * 1024 * 1024)
              except Exception as e:
                   log.warning("[tiktok] failed to download cover image (error=%s)", type(e).__name__)

         # prepare a preview: Telegram accepts only 320×320/≤200 KB
         thumb_bytes = await _fit_audio_thumbnail(cover_bytes) if cover_bytes else None
         thumbnail_file = None
         if thumb_bytes:
              thumbnail_file = BufferedInputFile(thumb_bytes, filename="cover.jpg")

         # Embed metadata and cover art in the MP3 before sending so other players also see
         #     the tags.
         tagged_music_bytes = music_bytes
         music_duration = 0
         try:
              with tempfile.TemporaryDirectory() as tmp_dir:
                   tmp_mp3_path = os.path.join(tmp_dir, "music.mp3")
                   # Recording and tagging are synchronous and heavy (up to 50 MB): do not block the stream or event loop.
                   await asyncio.to_thread(_write_and_tag_mp3_sync, tmp_mp3_path, music_bytes, cleaned_title, performer_name, thumb_bytes)
                   if os.path.exists(tmp_mp3_path) and os.path.getsize(tmp_mp3_path) > 0:
                        tagged_music_bytes = await asyncio.to_thread(_read_file_sync, tmp_mp3_path)
                        # Set duration explicitly: without it, Telegram shows 0:00 (the same issue as with video).
                        music_duration = await bot._probe_audio_duration(tmp_mp3_path)
         except Exception as tag_err:
              log.warning("[tiktok] failed to write embedded tags to MP3 (error=%s)", type(tag_err).__name__)

         # Slashes and control characters from someone else's title → "_" (AUD-E-005),
         # otherwise the multipart filename is corrupted.
         safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", cleaned_title[:60]).strip() or "track"
         # Via _tg_call: breaker gate, timeout and RetryAfter instead of direct send_audio.
         # None instead of an exception: the rest of the logic is the same as it was in except.
         sent_audio = await bot._tg_call(
              bot.bot.send_audio,
              chat_id=message.chat.id,
              audio=BufferedInputFile(tagged_music_bytes, filename=f"{safe_title}.mp3"),
              title=cleaned_title,
              performer=performer_name,
              duration=music_duration if music_duration > 0 else None,
              thumbnail=thumbnail_file,
              reply_to_message_id=message.message_id,
              call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
              # No blind retry: the retry without a cover below is the retry strategy.
              retries=0,
         )
         if sent_audio is None:
              if thumbnail_file is None:
                   raise RuntimeError("Telegram send_audio failed: connection timeout or proxy unavailable")
              # A TikWM cover can be large; Telegram rejects the entire send, so the track never arrives
              # failed silently. Retry without the cover art: music matters more than the image.
              log.warning("[tiktok] send_audio with thumbnail failed, retrying without")
              sent_audio = await bot._tg_call(
                   bot.bot.send_audio,
                   chat_id=message.chat.id,
                   audio=BufferedInputFile(tagged_music_bytes, filename=f"{safe_title}.mp3"),
                   title=cleaned_title,
                   performer=performer_name,
                   duration=music_duration if music_duration > 0 else None,
                   reply_to_message_id=message.message_id,
                   call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
                   retries=0,
              )
              if sent_audio is None:
                   raise RuntimeError("Telegram send_audio failed: connection timeout or proxy unavailable")
    except Exception as e:
         log.warning("[tiktok] failed to send music (error=%s)", type(e).__name__)
    finally:
         # An early return/exception must not leave the DSP request running in the background.
         with contextlib.suppress(Exception):
              if not dsp_task.done():
                   dsp_task.cancel()
         try:
              await dsp_task
         except asyncio.CancelledError:
              # Our own cancellation (message deadline) must propagate: suppress
              # muted it, and the handler continued past the budget (review 17.11.2026).
              if asyncio.current_task().cancelling():
                   raise

async def handle_tiktok_sound(message: Message, status: Message | None) -> None:
    """A link to a TikTok audio page (not a video); see the comment above in this file:
    neither TikWM nor a direct request to the TikTok page can retrieve audio from such a link
    with the bot's current infrastructure. Tell the user immediately rather than attempting a
    network request guaranteed to lead nowhere."""
    import bot
    if bot.is_guest_message(message):
         await bot._answer_guest_text(message, bot._t(message.chat.id, "tiktok_sound_recognized"))
         return
    raise TikTokUserFacingError(
        bot._t(message.chat.id, "tiktok_sound_no_separate")
    )


def _normalize_tikwm_media_data(value: object) -> dict[str, Any] | None:
    """Normalize external JSON containers before orchestration uses them."""
    if not isinstance(value, dict) or not value:
        return None
    media_data = dict(value)
    images = media_data.get("images")
    media_data["images"] = images if isinstance(images, list) else []
    live_images = media_data.get("live_images")
    media_data["live_images"] = live_images if isinstance(live_images, list) else []
    author = media_data.get("author")
    author = dict(author) if isinstance(author, dict) else {}
    for key in ("nickname", "unique_id", "avatar"):
        if not isinstance(author.get(key), str):
            author[key] = ""
    music_info = media_data.get("music_info")
    music_info = dict(music_info) if isinstance(music_info, dict) else {}
    for key in ("title", "author", "cover", "avatar"):
        if not isinstance(music_info.get(key), str):
            music_info[key] = ""
    media_data["author"] = author
    media_data["music_info"] = music_info
    return media_data


async def _fetch_tikwm_media_data_with_proxy_fallback(session: aiohttp.ClientSession, resolved_url: str, headers: dict) -> dict | None:
    """Tries TikWM proxy candidates in turn (see _tikwm_proxy_candidates): the primary address, then fallbacks from TIKWM_API_BASE_URL_FALLBACKS, until the first success. Without TIKWM_API_BASE_URL (unset), the only \"candidate\" is an empty string, preserving the previous behavior (direct requests to both TikWM mirrors inside _fetch_tikwm_media_data, unchanged)."""
    import bot
    result = None
    for candidate in bot._tikwm_proxy_candidates():
        try:
            result = await bot._fetch_tikwm_media_data(session, resolved_url, headers, proxy_base_url=candidate)
        except Exception as exc:
            # Exception messages and tracebacks can contain signed media URLs.
            level = logging.WARNING if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError)) else logging.ERROR
            log.log(level, "[tiktok] TikWM proxy candidate %s failed (error=%s)", _log_safe_url(candidate), type(exc).__name__)
            continue
        if result is not None:
            return result
    return result


async def _try_send_tiktok_slideshow(
    session: aiohttp.ClientSession, media_data: dict, message: Message, status: "StatusMessage | Message | None",
    author: str, headers: dict,
) -> bool:
    """Slideshow from TikWM images. True means slides were sent; False means there are no images or nothing downloaded, and the caller takes the video branch."""
    import bot
    images = media_data.get("images")
    if images and isinstance(images, list):
          # TikTok allows up to 35 slides, Telegram 10 per call: take the whole post and send it in several groups.
         images_to_fetch = images[:bot.TIKTOK_SLIDESHOW_MAX_ITEMS]
         # Timings and counters in the final line: previously success was written nowhere and
         # checking came down to screenshots (prod 27.10.2026).
         started_at = time.perf_counter()
         # Slideshow status uses translations (previously hardcoded in Russian).
         await _status_set(status, bot._t(
             message.chat.id, "tiktok_dl_slideshow",
             shown=len(images_to_fetch), total=len(images),
         ))
          # Download slides concurrently with gather; limit concurrency with a semaphore so we do not occupy the entire session pool.
         fetch_urls = _slideshow_slide_urls(media_data, images_to_fetch)
         # Overall RAM cap per post: stop downloading and skip the rest.
         post_budget = [bot.TIKTOK_SLIDESHOW_MAX_BYTES]
         # The reserve is taken under the lock BEFORE downloading: otherwise concurrent slides break through
         # cap (audit H4, 10.2026). If the budget is occupied by reservations, wait for
         # it to free up,
         # rather than skipping the slide: they chose a 75 MB reserve for a 200 MB post and took the first three
         # slide, and the rest were lost (prod 08-26.10.2026).
         _budget_cond = asyncio.Condition()
         _reserved = [0]

         async def _reserve_budget() -> int | None:
              async with _budget_cond:
                   while True:
                        # Free space = remaining space minus what is already promised to other slides.
                        free = post_budget[0] - _reserved[0]
                        if free > 0:
                             reservation = min(TIKTOK_DOWNLOAD_MAX_BYTES, free)
                             _reserved[0] += reservation
                             return reservation
                        # The budget is based on actual bytes — the remaining slides will not fit.
                        if post_budget[0] <= 0:
                             return None
                        await _budget_cond.wait()

         async def _release_budget(reservation: int, data: bytes | None) -> None:
              async with _budget_cond:
                   _reserved[0] -= reservation
                   post_budget[0] -= len(data or b"")
                   _budget_cond.notify_all()

         async def _download_slide_bounded(slide_url: str) -> bytes | None:
              reservation = await _reserve_budget()
              if reservation is None:
                   return None
              data = None
              try:
                   async with bot._tiktok_slide_download_semaphore:
                        async with bot._media_download_slot():
                             data = await bot._download_url_bin(
                                  session, slide_url, headers=headers,
                                  cap_bytes=reservation,
                             )
              finally:
                   # Return the unused reserve: actual usage is only len(data).
                   await _release_budget(reservation, data)
              return data or None

         downloaded = list(await asyncio.gather(
              *(_download_slide_bounded(u) for u in fetch_urls),
              return_exceptions=True,
         ))
         # Without return_exceptions, one failed link would kill the entire post. Because the
         # gather task was not canceled, the remaining downloads kept shared slots and memory
         # alive (audit 01.11.2026).
         for _slide_res in downloaded:
             if isinstance(_slide_res, BaseException):
                 log.warning("[tiktok] Slide download raised (error=%s)", type(_slide_res).__name__)
         downloaded = [r if isinstance(r, (bytes, type(None))) else None for r in downloaded]
         download_elapsed = time.perf_counter() - started_at
         downloaded_bytes = sum(len(b) for b in downloaded if b)
         skipped_slides = sum(1 for b in downloaded if not b)
         if skipped_slides:
              log.warning("[tiktok] Slideshow: %d of %d slides skipped (failed or over the post size cap).", skipped_slides, len(downloaded))
         video_indices = [idx for idx, b in enumerate(downloaded) if b and _looks_like_video_bytes(b)]
         if video_indices:
              log.info('[tiktok] In the slideshow, %d of %d slides were recognized as video (live_images/magic bytes).', len(video_indices), len(downloaded))
         # Video-slide probing runs in parallel (gather): the calls are independent,
         # one by one takes noticeably longer.
         probe_results: dict[int, tuple[int, int, int, bytes | None]] = {}
         if video_indices:
              async def _probe_bounded(item_bytes: bytes) -> tuple[int, int, int, bytes | None]:
                   async with bot._tiktok_probe_semaphore:
                        return await bot._probe_and_thumbnail_from_bytes(item_bytes)
              probed = await asyncio.gather(*(_probe_bounded(downloaded[i]) for i in video_indices))
              probe_results = dict(zip(video_indices, probed))
         media_items: list[Any] = []
         capped_slides = 0
         for idx, item_bytes in enumerate(downloaded):
              if not item_bytes:
                   continue
              if idx in probe_results and len(item_bytes) > _TG_VIDEO_MAX_BYTES:
                   log.warning("[tiktok] Slide %d skipped: %d MB over the 50 MB video cap.", idx, len(item_bytes) // (1024 * 1024))
                   capped_slides += 1
                   continue
              if idx not in probe_results and len(item_bytes) > _TG_PHOTO_MAX_BYTES:
                   log.warning("[tiktok] Slide %d skipped: %d MB over the 10 MB photo cap.", idx, len(item_bytes) // (1024 * 1024))
                   capped_slides += 1
                   continue
              if idx in probe_results:
                    # Video slide — like a video with its audio unchanged, plus the same duration/size/thumbnail as the full video (otherwise Telegram does not recognize the container).
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
                   photo_ext = _static_image_extension(item_bytes)
                   if photo_ext is None:
                        log.warning("[tiktok] Slide %d skipped: unsupported image bytes.", idx)
                        skipped_slides += 1
                        continue
                   media_items.append(InputMediaPhoto(media=BufferedInputFile(item_bytes, filename=f"photo_{idx}{photo_ext}")))
         if not media_items:
              # Video slides are correctly handled by the ordinary video branch, but only if play
              # really a video: on a photo post, play is audio, and via live_images.mp4
              # it was sent as video (regression 16.11.2026, review 17.11.2026).
              play_url = str(
                   media_data.get("play") or media_data.get("hdplay")
                   or media_data.get("wmplay") or "")
              if play_url.lower().split("?")[0].endswith((".mp4", ".m4v", ".webm")):
                   return False
              raise TikTokUserFacingError(bot._t(message.chat.id, "tiktok_generic_fail"))
         if media_items:
              # Keep the status until the end (including the music below); otherwise the person waits in silence while the track downloads.
              if len(media_items) == 1:
                    # A single surviving slide uses ordinary send_photo/send_video: a media group requires at least 2.
                   only_item = media_items[0]
                   if isinstance(only_item, InputMediaVideo):
                        # The same duration/size/thumbnail as for a group: without them
                        # a single slide went out as a raw 0:00 file (external audit 16.11.2026).
                        slide_kwargs: dict[str, Any] = {}
                        for attr in ("duration", "width", "height", "thumbnail"):
                             value = getattr(only_item, attr, None)
                             if value:
                                  slide_kwargs[attr] = value
                        sent_slide = await bot._tg_call(
                             bot.bot.send_video,
                             chat_id=message.chat.id, video=only_item.media,
                             supports_streaming=True, reply_to_message_id=message.message_id,
                             call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
                             # No blind retry: resending after a failure would create a duplicate.
                             retries=0,
                             **slide_kwargs,
                        )
                   else:
                        sent_slide = await bot._tg_call(
                             bot.bot.send_photo,
                             chat_id=message.chat.id, photo=only_item.media,
                             reply_to_message_id=message.message_id,
                             call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
                             retries=0,
                        )
                   if sent_slide is None:
                        raise RuntimeError("Telegram slideshow slide send failed: connection timeout or proxy unavailable")
              else:
                    # Groups of 10 without a single-item tail (Telegram's strict 2–10 requirement); the first is a reply to the link.
                   chunks = _chunk_tiktok_media_items(media_items)
                   for chunk_idx, chunk in enumerate(chunks):
                        sent_chunk = await bot._tg_call(
                             bot.bot.send_media_group,
                             chat_id=message.chat.id, media=chunk,
                             reply_to_message_id=message.message_id if chunk_idx == 0 else None,
                             call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
                             retries=0,
                        )
                        if sent_chunk is None:
                             raise RuntimeError("Telegram send_media_group failed: connection timeout or proxy unavailable")
                        if chunk_idx + 1 < len(chunks):
                              # A pause between groups protects against anti-flood measures.
                             await asyncio.sleep(0.3)
              total_elapsed = time.perf_counter() - started_at
              sent_videos = sum(1 for item in media_items if isinstance(item, InputMediaVideo))
              log.info(
                   '[tiktok] Slideshow delivered: %d of %d slides (%d video, %d photo) in %d group(s), %.1f MB, download %.1fs, send %.1fs.',
                   len(media_items), len(images_to_fetch), sent_videos,
                   len(media_items) - sent_videos,
                   len(_chunk_tiktok_media_items(media_items)),
                   downloaded_bytes / (1024 * 1024),
                   download_elapsed, total_elapsed - download_elapsed,
              )
              await _send_tiktok_music(session, media_data, message, author, headers)
              await _status_delete(status)
              if skipped_slides or capped_slides:
                   # Fewer frames arrived than the status promised. The same localized
                   # a counter template without animation: silently showing 5 of 12 —
                   # looks like "the bot ate half the post" (audit 01.11.2026).
                   await bot._safe_reply(message, bot._t(
                       message.chat.id, "tiktok_dl_slideshow",
                       shown=len(media_items), total=len(images),
                   ))
              return True
    return False

async def _send_tiktok_single_video(
    session: aiohttp.ClientSession, media_data: dict, message: Message, status: "StatusMessage | Message | None",
    author: str, headers: dict,
) -> None:
    """Try HD quality, then normal, then watermarked; a quota overflow means trying the next option, not giving up."""
    import bot
    # Quality order HD → regular → watermarked (previously without HD); EntityTooLarge → next option.
    video_candidates = _tiktok_video_candidates(media_data)
    if video_candidates:
         hit_size_limit = False
         send_failed = False
         for candidate in video_candidates:
              if candidate["size"] and candidate["size"] > bot.TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES:
                    # Skip an oversized download in advance and set hit_size_limit so we can give
                    # an honest "too large" response.
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
              await _status_set(status, status_msg)

              async with bot._media_download_slot():
                   video_bytes = await bot._download_url_bin(
                       session, candidate["url"], headers=headers,
                       # A video over the send limit will not go out anyway — cut it with the cap
                       # being sent, not the general 75MB (audit A7-5: 50-75MB were downloaded for nothing).
                       cap_bytes=bot.TELEGRAM_BOT_API_UPLOAD_LIMIT_BYTES,
                   )
              if not video_bytes:
                   continue

              duration, width, height = 0, 0, 0
              thumb_bytes = None
              try:
                   with tempfile.TemporaryDirectory() as tdir:
                        raw_path = os.path.join(tdir, "raw_tiktok.mp4")
                        # Writing up to 50MB in the stream: in the loop it stalled everyone.
                        await asyncio.to_thread(_write_file_sync, raw_path, video_bytes)
                        # Specify duration/dimensions explicitly: Telegram does not always parse a
                        # TikTok container itself (otherwise the "file" shows 0:00).
                        # Shared ffprobe/ffmpeg cap: single videos respect it too (audit 28.10.2026).
                        async with bot._tiktok_probe_semaphore:
                             duration, width, height = await bot._probe_video_dimensions(raw_path)
                             thumb_bytes = await bot._generate_video_thumbnail(raw_path, duration)
              except Exception as probe_exc:
                   log.warning("[tiktok] Video metadata probe failed, sending without (error=%s)", type(probe_exc).__name__)

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

              # The actual size is already downloaded: anything over the limit does not arrive here
              # (download cap above); an excessive declared size was filtered out before downloading.
              # The Telegram limit check is in the pair above; here only sending.
              # Through _tg_call: breaker gate, timeout, and RetryAfter. None means failure
              # sending: try the next option, as before when an exception occurred.
              # No blind retry: resending after a failure would produce a duplicate.
              sent_video = await bot._tg_call(
                   bot.bot.send_video, **send_kwargs,
                   call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
                   retries=0,
              )
              if sent_video is None:
                   send_failed = True
                   continue

              await _send_tiktok_music(session, media_data, message, author, headers)
              await _status_delete(status)
              return

         if send_failed:
              # The variants downloaded, but none reached Telegram — an honest
              # a send error, not "content deleted".
              raise RuntimeError("Telegram send_video failed: connection timeout or proxy unavailable")
         if hit_size_limit:
              # It downloaded, but no variant fit the limit — distinguish this from "TikTok returned nothing" (otherwise the misleading "content deleted" is shown).
               raise TikTokUserFacingError(
                   bot._t(message.chat.id, "tiktok_too_big")
               )

    raise TikTokUserFacingError(bot._t(message.chat.id, "tiktok_no_media"))

async def handle_tiktok(message: Message, url: str) -> None:
    import bot
    if bot.is_guest_message(message):
         # Do not send a file to a guest (answerGuestQuery is text-only): invite them to a private chat instead of using a stub.
         await bot._answer_guest_text(message, bot._t(message.chat.id, "tiktok_guest_dm_hint"))
         return
    status = await StatusMessage.start_action(
        message, bot._chat_lang(message.chat.id), bot._t(message.chat.id, "tiktok_processing"),
    )
    try:
         session = await bot._get_http_session()
         resolved_url = await bot._resolve_tiktok_short(session, url)
         if "?" in resolved_url:
              resolved_url = resolved_url.split("?")[0]

         headers = {
              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
              "Accept": "application/json"
         }

         # The AUDIO page is checked AFTER the short link is resolved — before that, the real path is unknown.
         music_page_id = _tiktok_music_page_id(resolved_url)
         if music_page_id:
              await handle_tiktok_sound(message, status)
              return

         # &hd=1 selects the hdplay field. A 403 with an empty body for a valid URL on
         # 11-13.08.2026 was an HF IP block; route through proxy.KWM_API_BASE_URL.
         media_data = _normalize_tikwm_media_data(
              await _fetch_tikwm_media_data_with_proxy_fallback(session, resolved_url, headers)
         )

         if not media_data:
              raise TikTokUserFacingError(bot._t(message.chat.id, "tiktok_fetch_fail"))

          # Log the images/live_images structure to reveal a TikWM format change.
         # Do not log full signed URLs, only counters (M7 audit, 10.2026).
         if images_debug := media_data.get("images"):
              log.info(
                   '[tikwm][diag] slideshow post: images=%d, has_live_images=%s, has_play=%s has_hdplay=%s has_wmplay=%s',
                   len(images_debug), bool(media_data.get("live_images")),
                   bool(media_data.get("play")), bool(media_data.get("hdplay")), bool(media_data.get("wmplay")),
              )
              log.debug('[tikwm][diag] slideshow post field_count=%d', len(media_data))

         author = (media_data.get("author") or {}).get("nickname") or bot._t(message.chat.id, "tiktok_author")

         if await _try_send_tiktok_slideshow(session, media_data, message, status, author, headers):
              return
         await _send_tiktok_single_video(session, media_data, message, status, author, headers)
    except asyncio.CancelledError:
          # Cancellation (restart/timeout) must not leave the status ticker alive for 5 minutes.
          await _status_stop(status)
          raise
    except Exception as exc:
          if isinstance(exc, (TelegramEntityTooLarge, TikTokUserFacingError)):
              # Known outcomes stay warnings; raw exception text may contain URLs.
              log.warning("TikTok download failed with a known, already-handled outcome (error=%s)", type(exc).__name__)
          else:
              log.error("TikTok download failed (error=%s)", type(exc).__name__)
          if isinstance(exc, TelegramEntityTooLarge):
               err_text = bot._t(message.chat.id, "tiktok_too_big")
          elif isinstance(exc, TikTokUserFacingError):
               err_text = str(exc)
          else:
               # Raw exceptions get generic text (the same logic as in the other handlers).
              err_text = bot._t(message.chat.id, "tiktok_generic_fail")
          await _status_stop(status)
          edited = await bot._edit_message_quietly(_status_message(status), err_text)
          if not edited:
               # status may have been deleted — duplicate it with a reply, or the user sees nothing.
              await bot._safe_reply(message, err_text)
