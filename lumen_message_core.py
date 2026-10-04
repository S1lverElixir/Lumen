"""
lumen_message_core.py — ядро обработки входящих сообщений: альбомы, пассивный
фон групп, rate limit, разбор вложений, _handle_message_core. Буферы альбомов
живут в bot.py, связь с ним — отложенным импортом внутри функций.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
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

# Суммарный кап extra-файлов альбома: 9 файлов по 20МБ + base64 давали ~250МБ
# в RAM (аудит D1, 30.09.2026). Обычный альбом — десятки МБ.
_ALBUM_EXTRA_MAX_BYTES = 100 * 1024 * 1024
# Параллельных скачиваний альбома: ограничивает in-flight память (3×20МБ).
_ALBUM_FETCH_CONCURRENCY = 3

async def _process_media_group_buffers(mgid: str) -> None:
    import bot
    messages: list = []
    try:
        await asyncio.sleep(0.8)
    finally:
        # Отмена во сне — запись обязана уйти, иначе висит вечно. Чужую (новую)
        # задачу под тем же mgid не трогаем — сверяемся, что это мы.
        messages = bot._mg_buffers.pop(mgid, []) or []
        if bot._mg_tasks.get(mgid) is asyncio.current_task():
            bot._mg_tasks.pop(mgid, None)
    if not messages:
         return
    # Альбом — под тем же per-chat lock: иначе фоновый таск и свежий вопрос
    # гоняются за history/ctx (аудит). Ожидание ограничено: вечное висело в фоне
    # дольше любого бюджета (аудит 26.09.2026).
    main_msg = messages[0]
    try:
        lock = await bot.acquire_chat_lock(main_msg.chat.id if main_msg.chat else 0, bot.CHAT_LOCK_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        log.warning("[album] Timeout waiting for lock on chat %s", main_msg.chat.id if main_msg.chat else None)
        with contextlib.suppress(Exception):
            await bot._safe_reply(main_msg, bot._t(main_msg.chat.id if main_msg.chat else None, "lock_busy"))
        return
    try:
        await _process_media_group_buffers_locked(messages)
    finally:
        with contextlib.suppress(Exception):
            lock.release()

async def _process_media_group_buffers_locked(messages: list) -> None:
    """Тело обработки альбома под per-chat lock (см. выше)."""
    import bot
    # Основное — сообщение с подписью (caption бывает не на первом фото); иначе промт терялся.
    main_msg = next((m for m in messages if m.text or m.caption), messages[0])
    # Альбом в группе без упоминания бота — такой же пассивный фон, как обычное
    # сообщение: раньше он всё равно тратил слот лимита и качал файлы (враждебное
    # ревью 27.09.2026). В личке и при прямом обращении альбом обрабатывается как раньше.
    if main_msg.chat and main_msg.chat.type != ChatType.PRIVATE:
        t_text = main_msg.text or main_msg.caption or ""
        if bot._should_only_record_passively(
            main_msg, t_text,
            is_private=False,
            is_guest=bot.is_guest_message(main_msg),
            mentioned=bot.message_mentions_bot(main_msg),
        ):
            # Файлы 2..10 пассивного альбома тоже в recent_media_ids: иначе
            # "что на втором фото" их не найдёт (порядок — как в альбоме).
            _passive_state = bot.get_state(main_msg.chat.id)
            bot._record_passive_group_context(main_msg, _passive_state, t_text)
            _passive_uid = main_msg.from_user.id if main_msg.from_user else None
            for _m in messages[1:]:
                bot._save_media_to_history(_msg_media_source(_m), _passive_state, _passive_uid)
            return
    # Лимит — ДО скачивания: раньше альбом грузился целиком даже для отклонённого
    # автора (аудит 26.09.2026). Слот ровно один: handle_message буферизует альбом
    # целиком и в _handle_message_core не заходит.
    if await bot._reject_rate_limited_message(main_msg):
        return
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
    fetched_list: list = [None] * len(targets)
    # Семафор режет in-flight память, суммарный кап — накопленную (аудит D1, 30.09.2026).
    _album_fetch_semaphore = asyncio.Semaphore(_ALBUM_FETCH_CONCURRENCY)

    async def _fetch_one_bounded(fid: str, mime: str) -> tuple[bytes, str] | None:
        async with _album_fetch_semaphore:
            return await bot._fetch_media(fid, mime)

    try:
        fetched_list = list(await asyncio.gather(
            *(_fetch_one_bounded(fid, mime) for fid, mime, _ in targets),
            return_exceptions=True,
        ))
    except Exception:
        pass
    skipped = 0
    too_big = 0
    extra_bytes = 0
    for (fid, mime, src), fetched in zip(targets, fetched_list):
        if isinstance(fetched, bot._MediaTooLargeError):
            too_big += 1
            continue
        if not fetched or isinstance(fetched, BaseException):
            skipped += 1
            continue
        if extra_bytes + len(fetched[0]) > _ALBUM_EXTRA_MAX_BYTES:
            skipped += 1
            continue
        extra_bytes += len(fetched[0])
        extra_media.append(fetched)
        # Файлы альбома — в recent_media_ids, иначе последующие вопросы их не найдут.
        bot._save_media_to_history(src, album_state, album_user_id)
    if skipped:
        # Упавшие слайды молча выпадали и анализ шёл по части файлов.
        log.warning("[album] Skipped %d of %d files: download failed, analysing the rest.", skipped, len(targets))
    if too_big:
        # Большие отклонены капом: честный отказ пользователю — из _handle_message_core.
        log.warning("[album] Skipped %d of %d files: over the download size cap.", too_big, len(targets))
    await bot._handle_message_core(main_msg, extra_media=extra_media or None)

def _record_passive_group_context(message: Message, state: dict[str, Any], t: str) -> None:
    """Фон группы без упоминания: только контекст и медиа в state, без ответа."""
    import bot
    if t.strip():
        _sender = message.from_user or {}
        _username = getattr(_sender, "username", None) or getattr(_sender, "first_name", None) or "User"
        state["ctx"].append(f"@{_username}: {t.strip()}")
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


_CONTINUE_RE = re.compile(
    r"^(продолжи(ть)?|продолжай|дальше|договори|continue)\s*,?\s*(пожалуйста)?\s*[.!…]*$",
    re.IGNORECASE,
)

_CONTINUE_INSTRUCTION = (
    "\n\n[Продолжи оборванный ответ с места обрыва, не повторяя уже сказанное. / "
    "Continue the interrupted answer from the cut point without repeating what was already said.]"
)

def _continue_after_interrupt(state: dict, clean_prompt: str) -> str | None:
    """«продолжи» после оборванного стрима: исходный вопрос + пометка продолжить
    с места обрыва. Частичный ответ уже лежит в истории — модель его видит,
    дублировать его в промт не нужно. Без флага обрыва или без пары
    вопрос-ответ в хвосте истории — None (обычный путь)."""
    if not _CONTINUE_RE.match((clean_prompt or "").strip()):
        return None
    hist = state.get("history") or []
    if (
        state.get("interrupted")
        and len(hist) >= 2
        and isinstance(hist[-1], dict) and hist[-1].get("role") == "assistant" and (hist[-1].get("content") or "").strip()
        and isinstance(hist[-2], dict) and hist[-2].get("role") == "user" and (hist[-2].get("content") or "").strip()
    ):
        return hist[-2]["content"].strip() + _CONTINUE_INSTRUCTION
    return None


async def _reject_rate_limited_message(message: Message) -> bool:
    import bot
    if not bot._check_and_register_rate_limit(bot._rate_limit_key_for_message(message)):
        return False
    # Ответ без создания чата: обычный _t через _chat_lang заводил бы запись даже
    # отклонённому сообщению. Язык уже существующего чата подхватывается так же.
    await bot._tg_call(message.reply, bot._t_no_create(message.chat.id, "rate_limited"))
    return True


def _read_attachment_sync(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


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
            # Чтение вложения в потоке: файлы до десятков МБ стопорили loop.
            media_tuple = (await asyncio.to_thread(_read_attachment_sync, med_path), med_mime)

    # Приоритет №2: явный реплай на сообщение с медиа — самый надёжный сигнал,
    # пользователь прямо указал, о каком файле речь. Работает без триггер-слов.
    if media_tuple is None and message.reply_to_message is not None:
        reply_src = _msg_media_source(message.reply_to_message)
        if reply_src:
            reply_fid, reply_mime, _ = _media_file_id_and_mime(reply_src)
            if reply_fid:
                fetched = await bot._fetch_media(reply_fid, reply_mime)
                if fetched:
                    # Медиа из реплая тоже в историю: ссылка словами должна находить файл.
                    bot._save_media_to_history(reply_src, state, asking_user_id)
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


def _strip_trigger_content(clean_prompt: str, trigger: str, message: Message) -> str:
    """Текст после draw/tts-триггера: чистка префикса + фолбэк на текст реплая
    ("нарисуй это" в ответ на сообщение с описанием)."""
    content = re.sub(r'^[:\s\-\,]+', '', clean_prompt[len(trigger):].strip()).strip()
    content = _strip_reply_marker(content)
    if not content and message.reply_to_message is not None:
        reply_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        if reply_text:
            content = reply_text
    return content


def _direct_attachment_needs_gemini(message: Message) -> bool:
    """Видео/аудио/документ (не картинка) во вложении или реплае — такое поздно
    отдавать только Gemini, скачивание заранее бесполезно при исчерпанном лимите."""
    for msg in (message, getattr(message, "reply_to_message", None)):
        if msg is None:
            continue
        # Имя атрибута уже говорит о категории без скачивания (и без опоры на
        # имя класса — в тестах лежат даблы, у aiogram свои типы).
        for attr in ("video", "video_note", "animation", "voice", "audio"):
            if getattr(msg, attr, None):
                return True
        src = _msg_media_source(msg)
        if src is None:
            continue
        if type(src).__name__ in ("Video", "VideoNote", "Animation", "Voice", "Audio"):
            return True
        _, mime, _ = _media_file_id_and_mime(src)
        m = (mime or "").lower()
        if m.startswith(("video/", "audio/")):
            return True
        if type(src).__name__ == "Document" and not m.startswith("image/"):
            return True
    return False


async def _handle_message_core(message: Message, extra_media: list[tuple[bytes, str]] | None = None) -> None:
    import bot
    t = message.text or message.caption or ""
    is_private = message.chat.type == ChatType.PRIVATE
    is_guest = bot.is_guest_message(message)
    mentioned = bot.message_mentions_bot(message)

    if bot._should_only_record_passively(message, t, is_private=is_private, is_guest=is_guest, mentioned=mentioned):
        # Пассивный фон — единственное место, где состояние нужно ДО лимита.
        bot._record_passive_group_context(message, bot.get_state(message.chat.id), t)
        return

    # Лимит проверяем ДО get_state: раньше отклонённое по лимиту сообщение всё равно
    # заводило запись чата (и переписывало индекс), раздувая состояние и вытесняя
    # реальные чаты (аудит 26.09.2026).
    if await bot._reject_rate_limited_message(message):
        return

    # Общий дневной лимит — до любой обработки и до get_state: отказ не создаёт
    # ни запись чата, ни запись пользователя, ни messages_received.
    uid_early = bot._user_key_for_message(message)
    _early_entry = bot._user_daily_peek(uid_early)
    if bot._user_daily_total_exhausted(uid_early, _early_entry):
        hours, mins = bot._user_daily_reset_in()
        bot._record_stats_event("daily_limit_denials")
        await bot._safe_reply(message, bot._t(
            message.chat.id, "user_daily_total",
            used=(_early_entry or {}).get("total", 0), limit=bot._user_daily_limit(uid_early, "total", _early_entry),
            hours=hours, mins=mins,
        ))
        return

    # Видео/аудио при исчерпанном Gemini отказываем до скачивания: поздняя
    # проверка после маршрута уже скачала бы файл и пожгла транскрибацию.
    # Картинки пропускаем — их отдаст Groq/OpenRouter через поздний фолбэк.
    if bot._user_daily_gemini_exhausted(uid_early, _early_entry) and _direct_attachment_needs_gemini(message):
        bot._record_stats_event("daily_limit_denials")
        await bot._safe_reply(message, bot._t(
            message.chat.id, "user_daily_gemini",
            used=(_early_entry or {}).get("gemini", 0), limit=bot._user_daily_limit(uid_early, "gemini", _early_entry),
        ))
        return

    state = bot.get_state(message.chat.id)

    # Проверка на ссылки загрузки (TikTok — сразу всегда, даже в группах без упоминания)

    url = extract_url(t)
    needs_youtube = False
    needs_website = False
    youtube_url_to_analyze: str | None = None
    if url:
        if is_tiktok(url):
            # Считаем только дошедшее до обработки: фон и отказы выше не в счёт.
            bot._record_stats_event("messages_received")
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

    # «продолжи» после оборванного стрима: подменяем промт исходным вопросом с
    # пометкой (частичный ответ уже в истории). Кнопки-уточнения при добивке не
    # показываем — вопрос уже задан, гадать нечего.
    continued_prompt = _continue_after_interrupt(state, clean_prompt)
    is_continuation = continued_prompt is not None
    if is_continuation:
        clean_prompt = continued_prompt
        lower_prompt = clean_prompt.lower().strip()

    matched_draw_trigger = _match_trigger_prefix(lower_prompt, DRAW_TRIGGER_PREFIXES)
    matched_tts_trigger = _match_trigger_prefix(lower_prompt, TTS_TRIGGER_PREFIXES)

    if matched_draw_trigger:
        prompt_content = _strip_trigger_content(clean_prompt, matched_draw_trigger, message)
        if prompt_content:
            await bot.inline_draw(message, prompt_content)
            return

    if matched_tts_trigger:
        tts_content = _strip_trigger_content(clean_prompt, matched_tts_trigger, message)
        if tts_content:
            await bot.inline_tts(message, tts_content)
            return

    # Кнопки-уточнения для вкусовых запросов без деталей — вместо гадания модели.
    # _pick_resolved ставят только колбэки: без флага дополненный текст ушёл бы
    # в кнопки по кругу.
    if bot.PICK_BUTTONS_ENABLED and not is_continuation and not getattr(message, "_pick_resolved", False):
        pick_scenario = match_pick_request(lower_prompt)
        if pick_scenario:
            await bot._send_pick_question(message, pick_scenario, clean_prompt)
            return

    media_too_big_mb = 0
    try:
        med_path, med_mime, med_name, media_tuple = await bot._resolve_incoming_media(
            message, state, clean_prompt, is_private=is_private,
        )
    except bot._MediaTooLargeError as exc:
        # Файл больше капа скачивания: честный отказ ниже вместо слепого ответа.
        med_path, med_name, media_tuple = None, "", None
        media_too_big_mb = max(1, round(exc.cap_bytes / (1024 * 1024)))

    # Голос/аудио: сначала дешёвая транскрибация — дальше текст идёт общим роутингом по
    # сценарию (тяжесть/свежесть определяются по сказанному). Не вышло — падает в прежний
    # путь: аудио напрямую в Gemini (см. is_video_or_audio_media в роутере).
    # Один бюджет на транскрибацию и маршрут: иначе войс держал лок сверх бюджета.
    route_deadline: float | None = None
    if media_tuple and media_tuple[1].startswith("audio/"):
        route_deadline = time.monotonic() + bot.ROUTE_TOTAL_BUDGET_SEC
        transcript = await bot._transcribe_audio(media_tuple[0], media_tuple[1], message.chat.id, deadline=route_deadline)
        if transcript:
            clean_prompt = (clean_prompt + "\n" + transcript).strip() if clean_prompt else transcript
            media_tuple = None
            if _looks_like_injection_probe(clean_prompt):
                # Голосовой транскрипт дописывается после первого префильтра (аудит A3-1).
                log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s: %r', message.chat.id, clean_prompt[:300])
                await bot._safe_reply(message, bot._t(message.chat.id, "injection_probe_reply"))
                return

    if media_tuple and not clean_prompt:
         clean_prompt = _ensure_prompt_text(None, media_tuple[1])
    if youtube_url_to_analyze and not clean_prompt:
         clean_prompt = "Подробно перескажи и опиши содержание этого YouTube-видео."

    if not clean_prompt and not media_tuple and not youtube_url_to_analyze:
         src = _msg_media_source(message)
         if src is not None and type(src).__name__ != "Sticker":
              # Вложение было, но скачать/распознать не вышло — честно говорим, а не "Слушаю" в пустоту.
              if media_too_big_mb:
                   await bot._safe_reply(message, bot._t(message.chat.id, "media_too_big", limit_mb=media_too_big_mb))
              else:
                   await bot._safe_reply(message, bot._model_error_text("fallback", bot._chat_lang(message.chat.id)))
              return
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
    # Смешанный альбом (фото + видео-слайды): достаточно одного видео — весь набор едет
    # в Gemini-ветку, иначе видео-слайды молча терялись (ревью ветки: смотрели только media[0]).
    if media_mime and media_mime.startswith("image/") and extra_media:
        for _, extra_mime in extra_media:
            if not (extra_mime or "").startswith("image/"):
                media_mime = extra_mime
                break
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

    # Дневные лимиты на пользователя — до скачивания/маршрута уже поздно, но до
    # вызова моделей: отказы и неудачи не считаются, только успешные ответы.
    # Проба без создания: иначе отказ заводил бы запись и раздувал day_users.
    uid = bot._user_key_for_message(message)
    _late_entry = bot._user_daily_peek(uid)
    if bot._user_daily_total_exhausted(uid, _late_entry):
        hours, mins = bot._user_daily_reset_in()
        # Суточный счётчик /stats: отказ по лимиту, ответа модели не будет.
        bot._record_stats_event("daily_limit_denials")
        await bot._safe_reply(message, bot._t(
            message.chat.id, "user_daily_total",
            used=(_late_entry or {}).get("total", 0), limit=bot._user_daily_limit(uid, "total", _late_entry),
            hours=hours, mins=mins,
        ))
        return
    no_search_note = False
    if bot._user_daily_gemini_exhausted(uid, _late_entry):
        non_gemini = [(p, m) for p, m in route if p != "gemini"]
        if not non_gemini:
            # Ссылки, YouTube, видео/аудио и документы читает только Gemini.
            bot._record_stats_event("daily_limit_denials")
            await bot._safe_reply(message, bot._t(
                message.chat.id, "user_daily_gemini",
                used=(_late_entry or {}).get("gemini", 0), limit=bot._user_daily_limit(uid, "gemini", _late_entry),
            ))
            return
        route = non_gemini
        # Свежесть без Gemini — честная пометка, что ответ без поиска.
        no_search_note = needs_freshness
    # Дошло до обработки: фон, инъекции и отказы выше уже отсеяны.
    bot._record_stats_event("messages_received")

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
    # текстового обмена без вложений/YouTube — см. _run_route. При пометке
    # "без поиска" стрим гасим: она дописывается к готовому ответу.
    allow_stream = not gemini_media_list and not youtube_url_to_analyze and not no_search_note
    try:
        ans, reply_already_sent = await bot._run_route(
            message.chat.id, ai_prompt, route, message,
            media=gemini_media_list, media_filename=med_name,
            youtube_url=youtube_url_to_analyze, allow_stream=allow_stream,
            deadline=route_deadline,
        )
        if no_search_note:
            ans = bot._t(message.chat.id, "fallback_no_search") + "\n\n" + ans
        if not reply_already_sent:
            await bot._safe_reply(message, ans)
        # Успешный ответ закрывает флаг обрыва (добивка тоже считается успехом).
        state.pop("interrupted", None)
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
                 # Тот же per-chat lock, что у обычного пути: параллельные апдейты иначе гоняются за history/ctx.
                 guest_chat_id = msg_obj.chat.id if msg_obj.chat else 0
                 try:
                     guest_lock = await bot.acquire_chat_lock(guest_chat_id, bot.CHAT_LOCK_TIMEOUT_SEC)
                 except asyncio.TimeoutError:
                     log.warning("[guest] Timeout waiting for lock on chat %s", guest_chat_id)
                     with contextlib.suppress(Exception):
                         await bot._tg_call(msg_obj.reply, bot._t_no_create(guest_chat_id, "lock_busy"))
                     return
                 try:
                     await bot._handle_message_core(msg_obj)
                 finally:
                     with contextlib.suppress(Exception):
                         guest_lock.release()
            except Exception as exc:
                 log.warning("[guest] Guest processing failed: %s", exc)
            return
        upd = Update.model_validate(raw_update, context={"bot": bot})
        await bot.dp.feed_update(bot.bot, upd)
    except Exception as exc:
        log.warning("[update] Raw update processing failed: %s", exc)
