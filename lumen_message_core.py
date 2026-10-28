"""
lumen_message_core.py — ядро обработки входящих сообщений: альбомы, пассивный
фон групп, rate limit, разбор вложений, _handle_message_core. Буферы альбомов
живут в bot.py, связь с ним — отложенным импортом внутри функций.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.enums import ChatType, ParseMode
from aiogram.types import Message, Update
from urllib.parse import urlparse

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
    _GEMMA_TEXT_CHAIN,
    _build_route,
    _looks_like_freshness_query,
    _looks_like_heavy_query,
    _looks_like_search_request,
)
from lumen_search import (
    answered_with_live_search,
    fetch_web_snippets,
    format_snippets_for_prompt,
    route_has_live_search,
    web_search_is_enabled,
    web_search_max_results,
    web_search_timeout_sec,
)
from lumen_security import _looks_like_injection_probe
from lumen_status import StatusMessage
import lumen_devtrace

log = logging.getLogger("bot")

# Дата для свежих вопросов считается внутри вызова; Москва как дефолт RU-аудитории —
# часовой пояс пользователя боту неизвестен. %A/%B дают английские названия стабильно:
# setlocale нигде не вызывается (локаль C и на HF, и в тестах).
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _fresh_date_context() -> str:
    """Однострочный контекст текущей даты для промптов свежих вопросов."""
    now = datetime.now(_MOSCOW_TZ)
    return f"[Context: today is {now.strftime('%d %B %Y')}, {now.strftime('%A')} (Europe/Moscow).]"


def _initial_status(needs_youtube: bool, needs_website: bool, media_mime: str | None,
                    search_will_run: bool) -> tuple[str, bool]:
    """Первая фраза статус-пузыря: показываем тот этап, который реально идёт первым."""
    if needs_youtube:
        return "progress_watching_video", False
    if needs_website:
        return "progress_reading_link", False
    if search_will_run:
        return "progress_searching", False
    if media_mime:
        if media_mime.startswith("image/"):
            return "progress_viewing_image", False
        return "progress_watching_video", False
    return "progress_thinking", True

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
    # Дневные лимиты — до скачивания extras: отказ по общей квоте всё равно
    # выкачивал девять файлов (аудит 26.10.2026).
    uid = bot._user_key_for_message(main_msg)
    entry = bot._user_daily_peek(uid)
    if await _refuse_daily_total(main_msg, uid, entry):
        return
    # Видео-слайд делает весь альбом Gemini-only: без этой проверки extras
    # качались бы до отказа по квоте Gemini.
    if any(_direct_attachment_needs_gemini(m) for m in messages) and await _refuse_daily_gemini(main_msg, uid, entry):
        return
    extra_media: list[tuple[bytes, str]] = []
    album_state = bot.get_state(main_msg.chat.id)
    album_user_id = main_msg.from_user.id if main_msg.from_user else None
    targets = _album_extra_sources(messages, main_msg)
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


# Основное сообщение уходит отдельно, до +9 дополнительных (итого 10 — как лимит TikTok-слайдшоу).
_MAX_ALBUM_EXTRA = 9


def _album_extra_sources(messages: list, main_msg: Any) -> list[tuple[str, str, Any]]:
    """Файлы альбома кроме основного: подпись бывает не на первом фото (аудит 27.10.2026)."""
    sources: list[tuple[str, str, Any]] = []
    for m in messages:
        if m is main_msg:
            continue
        if len(sources) >= _MAX_ALBUM_EXTRA:
            break
        src = _msg_media_source(m)
        if not src:
            continue
        fid, mime, _ = _media_file_id_and_mime(src)
        if not fid:
            continue
        sources.append((fid, mime, src))
    return sources


_PASSIVE_LINE_MAX_CHARS = 500
_PASSIVE_CTX_MAX_CHARS = 12000


def _record_passive_group_context(message: Message, state: dict[str, Any], t: str) -> None:
    """Фон группы без упоминания: только контекст и медиа в state, без ответа."""
    import bot
    if t.strip():
        sender = message.from_user
        display = bot._note_sender(
            state,
            sender.id if sender is not None else None,
            getattr(sender, "username", None) if sender is not None else None,
            getattr(sender, "first_name", None) if sender is not None else None,
        )
        display = _flatten_untrusted(display, 80)
        text_line = _flatten_untrusted(t, _PASSIVE_LINE_MAX_CHARS)
        state["ctx"].append(f"{display}: {text_line}")
        # Общий лимит 100 сообщений на чат, старые фоновые уходят первыми.
        bot._trim_combined(state)
        # Кап по символам: 100 сообщений по 4096 символов раздували промпт
        # и позволяли одному участнику забить чужой контекст (аудит 28.10.2026).
        while len(state["ctx"]) > 1 and sum(len(line) for line in state["ctx"]) > _PASSIVE_CTX_MAX_CHARS:
            state["ctx"].popleft()
    bot._save_media_to_history(_msg_media_source(message), state, message.from_user.id if message.from_user else None)
    bot.mark_state_dirty(message.chat.id)


# Сколько текста реплая несём модели: хватает на цитату, чат не раздувает.
_REPLY_TARGET_MAX_CHARS = 1000


def _flatten_untrusted(text: str, limit: int = _REPLY_TARGET_MAX_CHARS) -> str:
    """Чужой текст в промпте: одна строка, скобочные метки не подделать.
    Natural-language инъекции остаются за системным промптом и выходными
    фильтрами, но структурный breakout через ] и перевод строки закрыт."""
    clean = re.sub(r"[\r\n\t]+", " ", str(text or ""))
    clean = clean.replace("[", "(").replace("]", ")")
    return clean.strip()[:limit]


def _reply_target_block(message: Message, state: dict[str, Any]) -> str:
    """Цитата сообщения-реплая: реплай на бота отвечает по нему, а не по последнему."""
    import bot
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return ""
    text = _flatten_untrusted(getattr(reply, "text", None) or getattr(reply, "caption", None) or "")
    if not text:
        return ""
    ruser = getattr(reply, "from_user", None)
    try:
        bot_username = (bot.BOT_USERNAME or "").lower()
    except Exception:
        bot_username = ""
    r_username = getattr(ruser, "username", None) if ruser is not None else None
    if r_username and bot_username and str(r_username).lower() == bot_username:
        return f"[Отвечаешь именно на это сообщение (реплай) — твой прошлый ответ: {text}]\n"
    if ruser is not None:
        display = bot._note_sender(
            state, getattr(ruser, "id", None),
            getattr(ruser, "username", None), getattr(ruser, "first_name", None),
        )
    else:
        display = "User"
    display = _flatten_untrusted(display, 80)
    return f"[Отвечаешь именно на это сообщение (реплай) от {display}: {text}]\n"


def _tagged_prompt(state: dict[str, Any], message: Message, clean_prompt: str) -> str:
    """Вопрос с автором и реплаем: модель знает кто спрашивает и на что отвечает."""
    import bot
    sender = message.from_user
    display = _flatten_untrusted(bot._note_sender(
        state,
        sender.id if sender is not None else None,
        getattr(sender, "username", None) if sender is not None else None,
        getattr(sender, "first_name", None) if sender is not None else None,
    ), 80)
    tagged = f"[{display}] {clean_prompt}" if (clean_prompt or "").strip() else f"[{display}]"
    return _reply_target_block(message, state) + tagged + _nick_hints(state, clean_prompt)


# Сколько подсказок по прозвищам несём модели: хватает на участников, шум не раздувает.
_NICK_HINT_MAX = 3
# Короткие слова не резолвим: двухбуквенные совпадения шумят на каждом сообщении.
_NICK_HINT_MIN_LEN = 3


def _nick_hints(state: dict[str, Any], clean_prompt: str) -> str:
    """Связывает прозвища из вопроса с авторами («блинчик» — это @user (Имя))."""
    import bot
    try:
        words = re.findall(r"[\w@]+", clean_prompt or "", re.UNICODE)
    except Exception:
        return ""
    seen: set[str] = set()
    hints: list[str] = []
    for word in words:
        surface = word.strip().lstrip("@")
        if len(surface) < _NICK_HINT_MIN_LEN or surface.lower() in seen:
            continue
        seen.add(surface.lower())
        resolved = _flatten_untrusted(bot._resolve_nick(state, surface) or "", 80)
        surface = _flatten_untrusted(surface, 40)
        if resolved and resolved not in hints:
            hints.append(f"[Подсказка: «{surface}» — это {resolved}.]")
        if len(hints) >= _NICK_HINT_MAX:
            break
    return ("\n" + "\n".join(hints)) if hints else ""


def _should_only_record_passively(message: Message, t: str, *, is_private: bool, is_guest: bool, mentioned: bool) -> bool:
    """True, если сообщение — это фон группового чата без обращения к боту (см.
    _record_passive_group_context выше) и активная обработка не нужна вообще.
    Единственное исключение — ссылка на TikTok обрабатывается ВСЕГДА, даже без
    упоминания бота (исторически так и задумано, см. комментарий в исходной
    _handle_message_core)."""
    if is_private or is_guest or mentioned:
        return False
    if getattr(message, "_pick_resolved", False):
        # Выбор кнопки — уже прямой ответ боту: в группе без этой проверки он
        # уходил в пассивный фон и человек не получал ответа (аудит 27.10.2026).
        return False
    url = extract_url(t)
    if url and is_tiktok(url):
        import bot
        # Групповой TikTok без обращения по умолчанию качается (удобство шаринга);
        # TIKTOK_GROUP_REQUIRE_MENTION=1 оставляет только запись в фон (аудит H2, 10.2026).
        return bool(bot.TIKTOK_GROUP_REQUIRE_MENTION)
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


async def _refuse_daily_total(message: Message, uid: int | None, entry: dict | None) -> bool:
    """Отказ по общему дневному лимиту; True — сообщение уже обработано отказом."""
    import bot
    if not bot._user_daily_total_exhausted(uid, entry):
        return False
    hours, mins = bot._user_daily_reset_in()
    # Суточный счётчик /stats: отказ по лимиту, ответа модели не будет.
    bot._record_stats_event("daily_limit_denials")
    await bot._safe_reply(message, bot._t(
        message.chat.id, "user_daily_total",
        used=(entry or {}).get("total", 0), limit=bot._user_daily_limit(uid, "total", entry),
        hours=hours, mins=mins,
    ))
    return True


async def _refuse_daily_gemini(message: Message, uid: int | None, entry: dict | None) -> bool:
    """Отказ по дневной квоте Gemini; True — сообщение уже обработано отказом."""
    import bot
    if not bot._user_daily_gemini_exhausted(uid, entry):
        return False
    bot._record_stats_event("daily_limit_denials")
    await bot._safe_reply(message, bot._t(
        message.chat.id, "user_daily_gemini",
        used=(entry or {}).get("gemini", 0), limit=bot._user_daily_limit(uid, "gemini", entry),
    ))
    return True


async def _reject_rate_limited_message(message: Message) -> bool:
    import bot
    if not bot._check_and_register_rate_limit(bot._rate_limit_key_for_message(message)):
        return False
    # Ответ без создания чата: обычный _t через _chat_lang заводил бы запись даже
    # отклонённому сообщению. Язык уже существующего чата подхватывается так же.
    await bot._tg_call(bot._answer_sender(message), bot._t_no_create(message.chat.id, "rate_limited"))
    return True


async def _resolve_incoming_media(
    message: Message, state: dict[str, Any], clean_prompt: str, *, is_private: bool,
) -> tuple[str, tuple[bytes, str] | None]:
    """Чьё медиа: 1) вложение, 2) реплай, 3) слова. Вложение читается сразу в
    байты (без временного файла), остальное — через _fetch_media в памяти."""
    import bot
    asking_user_id = message.from_user.id if message.from_user else None
    media_src = _msg_media_source(message)
    med_name = ""
    media_tuple = None

    if media_src:
        res = await bot._download_message_attachment(media_src)
        if res:
            media_bytes, media_mime, med_name = res
            bot._save_media_to_history(media_src, state, asking_user_id)
            media_tuple = (media_bytes, media_mime)

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

    return med_name, media_tuple


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
    # Старт для замера задержки ответа в след.
    _trace_start = time.monotonic()

    if bot._should_only_record_passively(message, t, is_private=is_private, is_guest=is_guest, mentioned=mentioned):
        # Пассивный фон — единственное место, где состояние нужно ДО лимита.
        bot._record_passive_group_context(message, bot.get_state(message.chat.id), t)
        lumen_devtrace.record_passive(message.chat.id, t, is_private=is_private, mentioned=mentioned)
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
    if await _refuse_daily_total(message, uid_early, _early_entry):
        return

    # Видео/аудио при исчерпанном Gemini отказываем до скачивания: поздняя
    # проверка после маршрута уже скачала бы файл и пожгла транскрибацию.
    # Картинки пропускаем — их отдаст Groq/OpenRouter через поздний фолбэк.
    if _direct_attachment_needs_gemini(message) and await _refuse_daily_gemini(message, uid_early, _early_entry):
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
        # Инъекция: отвечаем без LLM. Текст в лог не пишем, только длину (аудит M5, 10.2026).
        log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s (prompt_len=%d)', message.chat.id, len(clean_prompt))
        lumen_devtrace.record_blocked(message.chat.id, reason="injection-probe", prompt_len=len(clean_prompt))
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
        med_name, media_tuple = await bot._resolve_incoming_media(
            message, state, clean_prompt, is_private=is_private,
        )
    except bot._MediaTooLargeError as exc:
        # Файл больше капа скачивания: честный отказ ниже вместо слепого ответа.
        med_name, media_tuple = "", None
        media_too_big_mb = max(1, round(exc.cap_bytes / (1024 * 1024)))
    except Exception as exc:
        # Сеть/прокси не отдали файл: исключение уходило из обработчика,
        # пользователь оставался без ответа (аудит 27.10.2026).
        log.warning("[media] Attachment download failed, replying with a fallback: %s", exc)
        await bot._safe_reply(message, bot._model_error_text("fallback", bot._chat_lang(message.chat.id)))
        return

    # Голос/аудио: сначала дешёвая транскрибация — дальше текст идёт общим роутингом по
    # сценарию (тяжесть/свежесть определяются по сказанному). Не вышло — падает в прежний
    # путь: аудио напрямую в Gemini (см. is_video_or_audio_media в роутере).
    # Один бюджет на транскрибацию и маршрут: иначе войс держал лок сверх бюджета.
    route_deadline: float | None = None
    if media_tuple and media_tuple[1].startswith("audio/"):
        route_deadline = bot._deadline_within_message(bot.ROUTE_TOTAL_BUDGET_SEC)
        transcript = await bot._transcribe_audio(media_tuple[0], media_tuple[1], message.chat.id, deadline=route_deadline)
        if transcript:
            clean_prompt = (clean_prompt + "\n" + transcript).strip() if clean_prompt else transcript
            media_tuple = None
            if _looks_like_injection_probe(clean_prompt):
                # Голосовой транскрипт дописывается после первого префильтра (аудит A3-1).
                log.warning('[injection-probe] Blocked a prompt-injection attempt in chat %s (prompt_len=%d)', message.chat.id, len(clean_prompt))
                lumen_devtrace.record_blocked(message.chat.id, reason="injection-probe", prompt_len=len(clean_prompt))
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
    # Не ждём ответа: это отдельный круг к Telegram через прокси, и вызов
    # модели стартовал бы на 0.1-0.3с позже. Индикатор косметический.
    if not is_guest:
        async def _send_typing() -> None:
            with contextlib.suppress(Exception):
                await bot.bot.send_chat_action(chat_id=message.chat.id, action="typing")

        bot._track_inflight_task(asyncio.create_task(_send_typing()))

    media_mime = media_tuple[1] if media_tuple else None
    # Смешанный альбом (фото + видео-слайды): достаточно одного видео — весь набор едет
    # в Gemini-ветку, иначе видео-слайды молча терялись (ревью ветки: смотрели только media[0]).
    if media_mime and media_mime.startswith("image/") and extra_media:
        for _, extra_mime in extra_media:
            if not (extra_mime or "").startswith("image/"):
                media_mime = extra_mime
                break
    is_heavy = _looks_like_heavy_query(clean_prompt)
    # Явная просьба искать тоже ведёт на свежий маршрут: иначе "погугли" без слов-маркеров уходило на модели без поиска (прод 11.10.2026).
    needs_freshness = _looks_like_freshness_query(clean_prompt) or _looks_like_search_request(clean_prompt)
    route = _build_route(
        needs_youtube=needs_youtube, needs_website=needs_website,
        media_mime=media_mime, is_heavy=is_heavy, needs_freshness=needs_freshness,
    )
    # Блоки и число моделей в них: полный список из 16 строк не читался.
    route_shape: list[list[Any]] = []
    for provider, _model_id in route:
        if route_shape and route_shape[-1][0] == provider:
            route_shape[-1][1] += 1
        else:
            route_shape.append([provider, 1])
    log.info(
        '[router] chat=%s heavy=%s freshness=%s youtube=%s website=%s media=%s route=%s (%d)',
        message.chat.id, is_heavy, needs_freshness, needs_youtube, needs_website, media_mime,
        " -> ".join(f"{p}:{n}" for p, n in route_shape), len(route),
    )

    # Дневные лимиты на пользователя — до скачивания/маршрута уже поздно, но до
    # вызова моделей: отказы и неудачи не считаются, только успешные ответы.
    # Проба без создания: иначе отказ заводил бы запись и раздувал day_users.
    uid = bot._user_key_for_message(message)
    _late_entry = bot._user_daily_peek(uid)
    if await _refuse_daily_total(message, uid, _late_entry):
        return
    user_gemini_limited = False
    if bot._user_daily_gemini_exhausted(uid, _late_entry):
        # Gemma не тратит дефицитную квоту, поэтому остаётся и после лимита:
        # обычный текст должен работать, как и обещает отказ (аудит 27.10.2026).
        allowed = [(p, m) for p, m in route if p != "gemini" or m in _GEMMA_TEXT_CHAIN]
        if not allowed:
            # Ссылки, YouTube, видео/аудио и документы читает только Gemini.
            await _refuse_daily_gemini(message, uid, _late_entry)
            return
        route = allowed
        user_gemini_limited = True
    # Дошло до обработки: фон, инъекции и отказы выше уже отсеяны.
    bot._record_stats_event("messages_received")

    # Внешний поиск — для любого свежего запроса: сниппеты едут всем моделям маршрута,
    # а не только резерву, когда grounding-моделей не осталось (задумка поиска: искать
    # умеет не только Gemini). Ссылки/YouTube читает только Gemini — им сниппеты не замена.
    # Выключен свитчом или пуст — отвечаем с пометкой.
    need_external_search = (
        needs_freshness
        and not needs_youtube
        and not needs_website
    )
    search_will_run = need_external_search and web_search_is_enabled()
    # Живой статус (как в Claude): этапы в одном сообщении, которое потом становится
    # ответом. Только личка и только запросы с реальным ранним этапом (поиск/медиа/
    # ссылка): для обычного текста пузырь создаст сам стрим, не тормозя старт модели.
    status: StatusMessage | None = None
    status_task: asyncio.Task | None = None
    early_status = search_will_run or bool(media_mime) or needs_website or needs_youtube
    if is_private and not is_guest and early_status:
        status_key, status_timed = _initial_status(needs_youtube, needs_website, media_mime, search_will_run)
        status_task = asyncio.create_task(
            StatusMessage.start(message, bot._chat_lang(message.chat.id), status_key, timed=status_timed)
        )

    # Автор и реплай в промпте: модель знает кто спрашивает и на что отвечает.
    ai_prompt = bot._tagged_prompt(state, message, clean_prompt)
    # Дата нужна свежим вопросам: без неё «завтра» модель строит по догадке
    # (eval 22.10.2026: flash-lite «вспомнила» вымышленное расписание). Инъекция
    # до промпта — попадает и в пост-хок ретрай, он переиспользует этот ai_prompt.
    if needs_freshness:
        ai_prompt += "\n\n" + _fresh_date_context()
    web_search_failed = False
    snippets_injected = False
    if search_will_run:
        # Сниппеты DuckDuckGo едут любому провайдеру, иначе ответ был бы по памяти (прод 11.10.2026).
        # Поиск идёт до старта бюджета маршрута: худшее +8с к 40с лока (параллельный вопрос получит "занято").
        # Статус-пузырь отправляется параллельно поиску, чтобы не добавлять круг к задержке.
        search_budget = web_search_timeout_sec()
        if route_deadline is not None:
            search_budget = min(search_budget, max(0.0, route_deadline - time.monotonic()))
        else:
            remaining = bot._msg_remaining_sec()
            if remaining is not None:
                search_budget = min(search_budget, remaining)
        snippets: list = []
        if search_budget > 0:
            try:
                snippets = await fetch_web_snippets(
                    clean_prompt, timeout_sec=search_budget, max_results=web_search_max_results(),
                )
            except Exception as exc:
                log.warning("[web-search] Snippet fetch failed, answering with a no-search note: %s", exc.__class__.__name__)
        if snippets:
            ai_prompt += format_snippets_for_prompt(snippets)
            snippets_injected = True
        else:
            web_search_failed = True
        status = await status_task if status_task is not None else None
        if status is not None:
            if snippets:
                status.add_done("progress_search_done", count=len(snippets))
                status.set_sources([urlparse(item.get("url") or "").netloc for item in snippets])
            else:
                status.add_done("progress_search_empty")
            await status.set_phase("progress_thinking", timed=True)
    elif need_external_search:
        web_search_failed = True
    if status is None and status_task is not None:
        status = await status_task
    # Пометка «без поиска» — только если поиск реально не дал результатов: при
    # исчерпанном лимите Gemini сниппеты едут дешёвым моделям, а не отключаются.
    no_search_note = user_gemini_limited and needs_freshness and not snippets_injected
    if no_search_note:
        web_search_failed = False
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
    # Свежий ответ без сниппетов и без grounding-головы не стримим: иначе он
    # уйдёт до пост-проверки и ретрай уже невозможен (прод 12.10.2026).
    head_has_live_search = (not route) or route_has_live_search(route[:1])
    allow_stream = (
        not gemini_media_list
        and not youtube_url_to_analyze
        and not no_search_note
        and (not needs_freshness or head_has_live_search or snippets_injected)
    )
    if route_deadline is None:
        # Один дедлайн на первый проход и пост-хок ретрай: раньше ретрай
        # получал свежие 40с и суммарно выходил за потолок сообщения.
        route_deadline = bot._deadline_within_message(bot.ROUTE_TOTAL_BUDGET_SEC)
    # Маркер обрыва прошлого сообщения не должен влиять на это.
    state.pop("stream_interrupted", None)
    try:
        provenance: dict = {}
        ans, reply_already_sent = await bot._run_route(
            message.chat.id, ai_prompt, route, message,
            media=gemini_media_list, media_filename=med_name,
            youtube_url=youtube_url_to_analyze, allow_stream=allow_stream,
            deadline=route_deadline, provenance=provenance, status=status,
            history_text=clean_prompt,
        )
        answered_grounded = answered_with_live_search(provenance)
        if needs_freshness and not snippets_injected and not answered_grounded and not reply_already_sent and not no_search_note:
            # Поиск до маршрута ничего не дал, а ответила модель без живого
            # поиска: пробуем ещё раз и один ретрай со сниппетами; не вышло —
            # честная пометка.
            retry_snippets: list = []
            try:
                retry_budget = web_search_timeout_sec()
                if route_deadline is not None:
                    retry_budget = min(retry_budget, max(0.0, route_deadline - time.monotonic()))
                if retry_budget > 0:
                    retry_snippets = await fetch_web_snippets(
                        clean_prompt, timeout_sec=retry_budget, max_results=web_search_max_results(),
                    )
            except Exception as exc:
                log.warning("[web-search] Post-hoc snippet fetch failed: %s", exc.__class__.__name__)
            if retry_snippets:
                log.info("[web-search] Retrying route once with fresh snippets (first answer had no live search).")
                ai_prompt += format_snippets_for_prompt(retry_snippets)
                snippets_injected = True
                try:
                    ans, reply_already_sent = await bot._run_route(
                        message.chat.id, ai_prompt, route, message,
                        media=gemini_media_list, media_filename=med_name,
                        youtube_url=youtube_url_to_analyze, allow_stream=False,
                        deadline=route_deadline, provenance=provenance,
                    )
                except Exception as exc:
                    log.warning("[web-search] Retry with snippets failed, keeping first answer with a no-search note: %s", exc.__class__.__name__)
                    web_search_failed = True
            else:
                web_search_failed = True
        elif needs_freshness and not snippets_injected and not answered_grounded:
            log.warning("[web-search] Answer already streamed without live search; cannot retry cleanly.")
        # Пометка «без поиска» — только если поиск реально не дал результата и
        # ответила модель без граундинга: при живом Gemini-поиске она была бы ложью.
        note_text = ""
        if no_search_note:
            note_text = bot._t(message.chat.id, "fallback_no_search")
        elif web_search_failed and not answered_grounded:
            note_text = bot._t(message.chat.id, "fallback_no_live_search")
        if note_text:
            ans = note_text + "\n\n" + ans
            if reply_already_sent and status is not None:
                # Ответ уже в статус-пузыре (стрим или переиспользование) —
                # пометку дописываем туда же вместе со свёрнутым блоком активности.
                html_text, plain_text = status.compose_answer(ans)
                if html_text is not None:
                    edited = await bot._tg_call(
                        status.message.edit_text, html_text,
                        parse_mode=ParseMode.HTML, call_timeout=15.0,
                    )
                    if edited is None:
                        await bot._tg_call(status.message.edit_text, plain_text, parse_mode=None, call_timeout=15.0)
                else:
                    await bot._edit_message_quietly(status.message, ans)
        if not reply_already_sent:
            await bot._safe_reply(message, ans)
        # Частичный стрим тоже «успешен» для вызывающего, но его пометка
        # «продолжи» должна пережить этот блок (аудит 27.10.2026).
        if state.pop("stream_interrupted", False):
            state["interrupted"] = True
        else:
            state.pop("interrupted", None)
        bot.mark_state_dirty(message.chat.id)
        lumen_devtrace.record_answer(
            message.chat.id, user_text=clean_prompt, route=route,
            provider=provenance.get("provider", ""), model=provenance.get("model") or "",
            latency_ms=int((time.monotonic() - _trace_start) * 1000), reply=ans,
            ctx_tail=list(state.get("ctx") or [])[-3:],
            flags={"fresh": bool(needs_freshness), "snippets": bool(snippets_injected),
                   "grounded": bool(answered_grounded), "streamed": bool(reply_already_sent)},
        )
    except Exception as exc:
        lumen_devtrace.record_failure(
            message.chat.id, user_text=clean_prompt, route=route,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.monotonic() - _trace_start) * 1000),
        )
        if isinstance(exc, (bot.GeminiAllModelsExhaustedError, bot.RouteBudgetExceededError)):
            # Квота/бюджет маршрута штатно, не баг: только warning, иначе Sentry тонет (LUMEN-3: 8 шумов за месяц).
            log.warning("Chat AI processing hit a known, already-handled outcome: %s", exc)
        else:
            log.exception("Chat AI processing failed:")
        head_model = route[0][1] if route else bot.DEFAULT_GEMINI_MODEL
        if isinstance(exc, bot.GeminiAllModelsExhaustedError):
            await bot._maybe_alert_gemini_exhausted()
        if status is not None:
            # Статус-пузырь не должен остаться висеть с «Думаю…» после ошибки.
            await status.delete()
        await bot._safe_reply(message, bot._route_error_reply_text(exc, head_model, youtube_url_to_analyze=youtube_url_to_analyze, lang=bot._chat_lang(message.chat.id)))
    finally:
        if status is not None:
            await status.stop()


async def _process_raw_update(raw_update: dict) -> None:
    import bot
    if not isinstance(raw_update, dict):
         return
    try:
        guest = raw_update.get("guest_message")
        if isinstance(guest, dict):
            try:
                 msg_obj = Message.model_validate(guest, context={"bot": bot})
                 guest_user = getattr(msg_obj, "from_user", None)
                 if guest_user is not None and bot._is_banned(guest_user.id):
                     # Гость в бане — тот же игнор, что у обычных сообщений.
                     return
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
