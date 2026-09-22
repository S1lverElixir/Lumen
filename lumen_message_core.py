"""
lumen_message_core.py — ядро обработки входящих сообщений (вынесено из bot.py,
P2 аудита): альбомы, пассивный фон групп, rate limit, разбор вложений,
_handle_message_core, handle_message, _process_raw_update.

Буферы альбомов (_mg_buffers/_mg_tasks) живут в bot.py — здесь только чтение/
мутация тех же объектов через `bot.`. Остальные связи с рантаймом — тоже через
отложенный `import bot` внутри функций. bot.py реэкспортирует имена.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import Any

from aiogram.enums import ChatType
from aiogram.types import Message, Update

from lumen_message_parse import (
    DRAW_TRIGGER_PREFIXES,
    TTS_TRIGGER_PREFIXES,
    _NO_MEDIA_NOTE,
    _find_recent_media_by_category,
    _match_trigger_prefix,
    _media_reference_category,
    _strip_reply_marker,
    extract_url,
    is_tiktok,
    is_youtube,
    match_pick_request,
)
from lumen_media import (
    _ensure_prompt_text,
    _media_file_id_and_mime,
    _msg_media_source,
)
from lumen_router_config import (
    _build_route,
    _looks_like_freshness_query,
    _looks_like_heavy_query,
)
from lumen_security import _looks_like_injection_probe

log = logging.getLogger("bot")

async def _process_media_group_buffers(mgid: str) -> None:
    import bot
    await asyncio.sleep(0.8)
    messages = bot._mg_buffers.pop(mgid, [])
    bot._mg_tasks.pop(mgid, None)
    if not messages:
         return
    # Первое сообщение альбома с caption — основное, остальные файлы отдаём модели как доп. вложения.
    main_msg = messages[0]
    extra_media: list[tuple[bytes, str]] = []
    MAX_ALBUM_EXTRA = 9  # первое уходит как основное, до +9 дополнительных (итого 10 — как лимит TikTok-слайдшоу)
    album_state = bot.get_state(main_msg.chat.id)
    album_user_id = main_msg.from_user.id if main_msg.from_user else None
    targets: list[tuple[str, str, Any]] = []
    for m in messages[1:1 + MAX_ALBUM_EXTRA]:
        src = _msg_media_source(m)
        if not src:
            continue
        fid, mime, _ = _media_file_id_and_mime(src)
        if not fid:
            continue
        targets.append((fid, mime, src))
    # Качаем параллельно, а не по очереди: альбом из 9 фото иначе ждал бы до ~10-20с последовательных скачиваний.
    fetched_list = await asyncio.gather(*(bot._fetch_media(fid, mime) for fid, mime, _ in targets))
    for (fid, mime, src), fetched in zip(targets, fetched_list):
        if fetched:
            extra_media.append(fetched)
            # Регрессия: файлы альбома пишем в recent_media_ids, иначе "что на втором фото" не найдёт их.
            bot._save_media_to_history(src, album_state, album_user_id)
    await bot._handle_message_core(main_msg, extra_media=extra_media or None)

def _record_passive_group_context(message: Message, state: dict[str, Any], t: str) -> None:
    """Фон группы без упоминания: только контекст и медиа в state, без ответа."""
    import bot
    if t.strip():
        username = message.from_user.username or message.from_user.first_name or "User"
        state["ctx"].append(f"@{username}: {t.strip()}")
    bot._save_media_to_history(_msg_media_source(message), state, message.from_user.id if message.from_user else None)
    bot.mark_state_dirty(message.chat.id)


def _should_only_record_passively(message: Message, t: str, *, is_private: bool, is_guest: bool, mentioned: bool) -> bool:
    """True, если сообщение — это фон группового чата без обращения к боту (см.
    _record_passive_group_context выше) и активная обработка не нужна вообще.
    Единственное исключение — ссылка на TikTok обрабатывается ВСЕГДА, даже без
    упоминания бота (исторически так и задумано, см. комментарий в исходной
    _handle_message_core)."""
    if is_private or is_guest or mentioned:
        return False
    url = extract_url(t)
    return not url or not is_tiktok(url)


def _rate_limit_key_for_message(message: Message) -> int:
    """Ключ rate limit: без from_user (пост от канала) берём sender_chat/chat.id, иначе обход лимита к платным провайдерам."""
    if message.from_user:
        return message.from_user.id
    sender_chat = getattr(message, "sender_chat", None)
    return sender_chat.id if sender_chat else message.chat.id


async def _reject_rate_limited_message(message: Message) -> bool:
    import bot
    if not bot._check_and_register_rate_limit(bot._rate_limit_key_for_message(message)):
        return False
    await bot._tg_call(message.reply, bot._t(message.chat.id, "rate_limited"))
    return True


async def _resolve_incoming_media(
    message: Message, state: dict[str, Any], clean_prompt: str, *, is_private: bool,
) -> tuple[str | None, str, str, tuple[bytes, str] | None]:
    """Чьё медиа: 1) вложение, 2) реплай, 3) слова. Файл на диске только в №1, остальное через _fetch_media в памяти."""
    import bot
    asking_user_id = message.from_user.id if message.from_user else None
    media_src = _msg_media_source(message)
    med_path, med_mime, med_name = None, "", ""
    media_tuple = None

    if media_src:
        res = await bot._download_message_attachment_to_tmp(media_src)
        if res:
            med_path, med_mime, med_name = res
            bot._save_media_to_history(media_src, state, asking_user_id)
            with open(med_path, "rb") as f:
                media_tuple = (f.read(), med_mime)

    # Приоритет №2: явный реплай на сообщение с медиа — самый надёжный сигнал,
    # пользователь прямо указал, о каком файле речь. Работает без триггер-слов.
    if media_tuple is None and message.reply_to_message is not None:
        reply_src = _msg_media_source(message.reply_to_message)
        if reply_src:
            reply_fid, reply_mime, _ = _media_file_id_and_mime(reply_src)
            if reply_fid:
                fetched = await bot._fetch_media(reply_fid, reply_mime)
                if fetched:
                    media_tuple = fetched

    # №3 ищем у автора, не последнее в чате: калибровка 18.08.2026 — "покажи стикер" описывал чужое фото.
    if media_tuple is None and state.get("recent_media_ids"):
        category = _media_reference_category(clean_prompt)
        if category:
            buckets: dict[str, Any] = state["recent_media_ids"]
            own_bucket = buckets.get(str(asking_user_id)) if asking_user_id is not None else None
            fid_mime = None
            if own_bucket:
                fid_mime = _find_recent_media_by_category(own_bucket, category)
            elif is_private:
                # В личке с ботом собеседник ровно один — не так критично,
                # можно поискать по всем известным бакетам чата вообще.
                for bucket in buckets.values():
                    fid_mime = _find_recent_media_by_category(bucket, category)
                    if fid_mime:
                        break
            if fid_mime:
                fid, mime = fid_mime
                fetched = await bot._fetch_media(fid, mime)
                if fetched:
                    media_tuple = fetched

    return med_path, med_mime, med_name, media_tuple


async def _handle_message_core(message: Message, extra_media: list[tuple[bytes, str]] | None = None) -> None:
    import bot
    state = bot.get_state(message.chat.id)
    t = message.text or message.caption or ""
    is_private = message.chat.type == ChatType.PRIVATE
    is_guest = bot.is_guest_message(message)
    mentioned = bot.message_mentions_bot(message)

    if bot._should_only_record_passively(message, t, is_private=is_private, is_guest=is_guest, mentioned=mentioned):
        bot._record_passive_group_context(message, state, t)
        return

    # Начинаем обработку активного запроса с проверкой rate limit
    if await bot._reject_rate_limited_message(message):
        return

    # Проверка на ссылки загрузки (TikTok — сразу всегда, даже в группах без упоминания)

    url = extract_url(t)
    needs_youtube = False
    needs_website = False
    youtube_url_to_analyze: str | None = None
    if url:
        if is_tiktok(url):
             await bot.handle_tiktok(message, url)
             return
        # YouTube/сайт читает сама модель (file_uri/url_context), сервер чужое не качает.
        # Не открылось — скажем честно.
        if is_youtube(url):
             needs_youtube = True
             youtube_url_to_analyze = url
        else:
             # needs_website читает ссылку сама модель через url_context на
             # стороне Google — наш сервер произвольные URL не скачивает
             # (свои загрузки — только через _download_url_bin с гардом схемы).
             needs_website = True

    clean_prompt = bot.clean_mention(t).strip()

    if clean_prompt and _looks_like_injection_probe(clean_prompt):
        # Инъекция: отвечаем без LLM и логируем для /logs, чтобы пополнять паттерны реальными случаями.
        log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s: %r', message.chat.id, clean_prompt[:300])
        await bot._safe_reply(message, bot._t(message.chat.id, "injection_probe_reply"))
        return

    lower_prompt = clean_prompt.lower().strip()

    matched_draw_trigger = _match_trigger_prefix(lower_prompt, DRAW_TRIGGER_PREFIXES)
    matched_tts_trigger = _match_trigger_prefix(lower_prompt, TTS_TRIGGER_PREFIXES)

    if matched_draw_trigger:
        prompt_content = clean_prompt[len(matched_draw_trigger):].strip()
        prompt_content = re.sub(r'^[:\s\-\,]+', '', prompt_content).strip()
        # Триггер сказан без содержания ("нарисуй" / "нарисуй это" в ответ на
        # сообщение с описанием) — берём текст из reply вместо того, чтобы
        # просто промолчать/уйти в обычный диалог.
        prompt_content = _strip_reply_marker(prompt_content)
        if not prompt_content and message.reply_to_message is not None:
            reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if reply_text:
                prompt_content = reply_text
        if prompt_content:
            await bot.inline_draw(message, prompt_content)
            return

    if matched_tts_trigger:
        tts_content = clean_prompt[len(matched_tts_trigger):].strip()
        tts_content = re.sub(r'^[:\s\-\,]+', '', tts_content).strip()
        # То же самое для озвучки — реплай "озвучь"/"озвучь это" без текста
        # означает "озвучь ТО сообщение, на которое я отвечаю".
        tts_content = _strip_reply_marker(tts_content)
        if not tts_content and message.reply_to_message is not None:
            reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if reply_text:
                tts_content = reply_text
        if tts_content:
            await bot.inline_tts(message, tts_content)
            return

    # Кнопки-уточнения для вкусовых запросов без деталей ("посоветуй фильм"):
    # вместо гадания модели — вопрос с вариантами. _pick_resolved ставят только
    # колбэки (см. handle_pick_callback): дополненный текст всё ещё матчится
    # детектором, без флага ушёл бы в кнопки по кругу.
    if bot.PICK_BUTTONS_ENABLED and not getattr(message, "_pick_resolved", False):
        pick_scenario = match_pick_request(lower_prompt)
        if pick_scenario:
            await bot._send_pick_question(message, pick_scenario, clean_prompt)
            return

    med_path, med_mime, med_name, media_tuple = await bot._resolve_incoming_media(
        message, state, clean_prompt, is_private=is_private,
    )

    if media_tuple and not clean_prompt:
         clean_prompt = _ensure_prompt_text(None, media_tuple[1])
    if youtube_url_to_analyze and not clean_prompt:
         clean_prompt = "Подробно перескажи и опиши содержание этого YouTube-видео."

    if not clean_prompt and not media_tuple and not youtube_url_to_analyze:
         if mentioned:
              await bot._tg_call(message.reply, bot._t(message.chat.id, "status_listening"))
         return

    # Отправка typing экшена (bot.bot — инстанс aiogram Bot, не модуль).
    try:
        if not is_guest:
             await bot.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
         pass

    media_mime = media_tuple[1] if media_tuple else None
    is_heavy = _looks_like_heavy_query(clean_prompt)
    needs_freshness = _looks_like_freshness_query(clean_prompt)
    route = _build_route(
        needs_youtube=needs_youtube, needs_website=needs_website,
        media_mime=media_mime, is_heavy=is_heavy, needs_freshness=needs_freshness,
    )
    log.info(
        '[router] chat=%s heavy=%s freshness=%s youtube=%s website=%s media=%s route=%s',
        message.chat.id, is_heavy, needs_freshness, needs_youtube, needs_website, media_mime,
        [f"{p}:{m}" for p, m in route],
    )

    ai_prompt = clean_prompt
    if (
        media_tuple is None
        and not extra_media
        and youtube_url_to_analyze is None
        and _media_reference_category(clean_prompt) is not None
    ):
        # Прод-кейс 17.09.2026: без файла модель выдумывала фото. Дописываем _NO_MEDIA_NOTE, историю не режем.
        ai_prompt += _NO_MEDIA_NOTE
    gemini_media_list = ([media_tuple] if media_tuple else []) + list(extra_media or [])
    gemini_media_list = gemini_media_list or None
    # Стриминг ("живой" эффект печати) имеет смысл только для простого
    # текстового обмена без вложений/YouTube — см. _run_route.
    allow_stream = not gemini_media_list and not youtube_url_to_analyze
    try:
        ans, reply_already_sent = await bot._run_route(
            message.chat.id, ai_prompt, route, message,
            media=gemini_media_list, media_filename=med_name,
            youtube_url=youtube_url_to_analyze, allow_stream=allow_stream,
        )
        if not reply_already_sent:
            await bot._safe_reply(message, ans)
        bot.mark_state_dirty(message.chat.id)
    except Exception as exc:
        if isinstance(exc, (bot.GeminiAllModelsExhaustedError, bot.RouteBudgetExceededError)):
            # Квота/бюджет маршрута штатно, не баг: только warning, иначе Sentry тонет (LUMEN-3: 8 шумов за месяц).
            log.warning("Chat AI processing hit a known, already-handled outcome: %s", exc)
        else:
            log.exception("Chat AI processing failed:")
        head_model = route[0][1] if route else bot.DEFAULT_GEMINI_MODEL
        if isinstance(exc, bot.GeminiAllModelsExhaustedError):
            await bot._maybe_alert_gemini_exhausted()
        await bot._safe_reply(message, bot._route_error_reply_text(exc, head_model, youtube_url_to_analyze=youtube_url_to_analyze, lang=bot._chat_lang(message.chat.id)))
    finally:
        if med_path and os.path.exists(med_path):
             with contextlib.suppress(Exception):
                  os.unlink(med_path)


async def _process_raw_update(raw_update: dict) -> None:
    import bot
    if not isinstance(raw_update, dict):
         return
    try:
        guest = raw_update.get("guest_message")
        if isinstance(guest, dict):
            try:
                 msg_obj = Message.model_validate(guest, context={"bot": bot})
                 gq_id = guest.get("guest_query_id")
                 if gq_id is not None and not getattr(msg_obj, "guest_query_id", None):
                     with contextlib.suppress(Exception):
                         object.__setattr__(msg_obj, "guest_query_id", gq_id)
                 await bot._handle_message_core(msg_obj)
            except Exception as exc:
                 log.warning("[guest] Guest processing failed: %s", exc)
            return
        upd = Update.model_validate(raw_update, context={"bot": bot})
        await bot.dp.feed_update(bot.bot, upd)
    except Exception as exc:
        log.warning("[update] Raw update processing failed: %s", exc)
