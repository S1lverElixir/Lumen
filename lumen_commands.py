"""
lumen_commands.py — команды бота, TTS/Draw-пайплайны и кнопки-уточнения. Связи с bot.py — только через отложенный `import bot`; bot.py реэкспортирует имена и регистрирует хендлеры.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from types import SimpleNamespace
from typing import Any

from aiogram.enums import ChatType, ParseMode
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from lumen_images import (
    POLLINATIONS_IMAGE_MODELS,
    _pick_image_model,
)
from lumen_lang import SUPPORTED_LANGS, LANG_NAMES, normalize_lang, pick_texts, t as _lang_t
from lumen_limits import (
    PICK_TTL_SEC,
    _purge_expired_picks,
    _enforce_pending_picks_cap,
)
from lumen_tiktok import _communicate_process
from lumen_tts import (
    pcm_to_wav,
    _fish_audio_tts_bytes as _lumen_fish_audio_tts_bytes,
    _gemini_tts_bytes as _lumen_gemini_tts_bytes,
)

log = logging.getLogger("bot")

async def cmd_start(message: Message) -> None:
    import bot
    if getattr(message, "from_user", None) is not None and bot._is_banned(message.from_user.id):
        return
    # Лимит и здесь: хендлер зарегистрирован раньше общего catch-all, поэтому
    # сообщение до _reject_rate_limited_message не доходило — спамер получал
    # бесконечные ответы в группе, расходуя прокси-трафик (аудит 26.09.2026).
    if await bot._reject_rate_limited_message(message):
        return
    text = bot._t(message.chat.id, "start_text")
    if message.chat.type != ChatType.PRIVATE:
        # В группах контекст честно виден: фон чата (до 100 сообщений) уходит провайдерам вместе с вопросом.
        text += "\n\n" + bot._t(message.chat.id, "group_history_notice")
    await bot._tg_call(
        message.reply,
        text,
        parse_mode=ParseMode.HTML,
    )

async def inline_draw(message: Message, prompt: str) -> None:
    import bot
    # Дневной лимит пользователя, как у озвучки: проба без создания.
    uid = bot._user_key_for_message(message)
    _draw_entry = bot._user_daily_peek(uid)
    if bot._user_daily_total_exhausted(uid, _draw_entry):
        hours, mins = bot._user_daily_reset_in()
        bot._record_stats_event("daily_limit_denials")
        await bot._safe_reply(message, bot._t(
            message.chat.id, "user_daily_total",
            used=(_draw_entry or {}).get("total", 0), limit=bot._user_daily_limit(uid, "total", _draw_entry),
            hours=hours, mins=mins,
        ))
        return
    status = await bot._tg_call(message.reply, bot._t(message.chat.id, "status_generating_image"))
    try:
        session = await bot._get_http_session()
        # Модель — автовыбором по промпту (/imgmodel убран 19.08.2026), без состояния чата.
        primary_model = _pick_image_model(prompt)

        # Фолбэк-цепочка: подобранная модель первой, дальше остальные по порядку.
        all_model_ids = list(POLLINATIONS_IMAGE_MODELS.keys())
        fallback_chain = [primary_model] + [m for m in all_model_ids if m != primary_model]

        image_bytes = None
        last_error = None
        budget_exceeded = False
        rate_limited = False
        deadline = time.monotonic() + bot.DRAW_TOTAL_BUDGET_SEC

        for attempt_model in fallback_chain:
            if time.monotonic() > deadline:
                budget_exceeded = True
                log.warning(
                    '[draw] Overall time budget (%.0fs) exhausted before trying %s — stopping the fallback chain instead of trying the remaining models.',
                    bot.DRAW_TOTAL_BUDGET_SEC, attempt_model,
                )
                break
            try:
                # Статус без названий моделей (см. ИДЕНТИЧНОСТЬ в system_prompt.py); на первой попытке статус и так стоит.
                if attempt_model != primary_model:
                    await bot._edit_message_quietly(status, bot._t(message.chat.id, "status_taking_longer"))
                # Таймаут попытки — остаток общего бюджета: зависшая модель не
                # переживает дедлайн цепочки.
                attempt_timeout = max(1.0, deadline - time.monotonic())
                image_bytes = await bot._pollinations_text_to_image(session, attempt_model, prompt, timeout_sec=attempt_timeout)
                break
            except Exception as exc:
                last_error = exc
                txt = bot._error_text(exc).lower()
                # 429 — перегрузка ВСЕГО сервиса (прод 17.09.2026: все 5 моделей подряд), остаток цепочки не гоняем.
                if "429" in txt or "rate" in txt or "too many" in txt:
                    log.warning("[draw] Service rate-limited (429) on model %s — stopping the fallback chain.", attempt_model)
                    rate_limited = True
                    break
                # Пробуем следующую только при сетевых/серверных ошибках
                if any(kw in txt for kw in ("cannot connect", "ssl:", "no address", "503", "502", "timeout", "host")):
                    log.warning("[draw] Model %s failed (%s), trying next fallback", attempt_model, type(exc).__name__)
                    continue
                # При других ошибках (400, 404 и т.д.) тоже пробуем следующую
                log.warning("[draw] Model %s error: %s, trying next", attempt_model, exc)
                continue

        if image_bytes:
            await bot._delete_message_quietly(status)
            # Через _tg_call: breaker-гейт, таймаут и RetryAfter вместо прямого send_photo.
            # _tg_call отдаёт None вместо исключения — внешний except ждёт исключение,
            # поэтому отсутствие результата превращаем в ошибку сервиса здесь.
            sent = await bot._tg_call(
                bot.bot.send_photo,
                chat_id=message.chat.id,
                photo=BufferedInputFile(image_bytes, filename="generated.jpg"),
                reply_to_message_id=message.message_id,
                call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
            )
            if sent is None:
                raise RuntimeError("Telegram send_photo failed: connection timeout or proxy unavailable")
            # Суточный счётчик /stats: картинка ушла пользователю.
            bot._record_user_daily(uid)
            bot._record_stats_event("answers_sent")
        else:
            if rate_limited:
                raise RuntimeError("image generation service overloaded with requests") from last_error
            if budget_exceeded:
                raise RuntimeError("image generation total time budget exceeded") from last_error
            raise last_error or RuntimeError("all image generation models unavailable")

    except Exception as exc:
        log.exception("Pollinations image generation failed:")
        txt = bot._error_text(exc).strip()
        cid = message.chat.id
        if any(kw in txt.lower() for kw in ("cannot connect", "ssl:", "no address", "connection", "timeout", "host")):
            user_err = bot._t(cid, "draw_err_unavailable")
        elif "overloaded" in txt.lower():
            user_err = bot._t(cid, "draw_err_overloaded")
        elif "all image generation" in txt.lower():
            user_err = bot._t(cid, "draw_err_gone")
        elif "time budget" in txt.lower():
            user_err = bot._t(cid, "draw_err_budget")
        else:
            # Сырой текст провайдера пользователю не показываем — generic-текст.
            user_err = bot._t(cid, "draw_err_generic")
        # Статус уже снесён выше (перед send_photo): если правка не прошла — дублируем реплаем,
        # иначе пользователь не увидит вообще ничего (найдено внешним аудитом).
        if not await bot._edit_message_quietly(status, user_err):
            await bot._safe_reply(message, user_err)


async def cmd_draw(message: Message) -> None:
    import bot
    if getattr(message, "from_user", None) is not None and bot._is_banned(message.from_user.id):
        return
    prompt = message.text.partition(" ")[2].strip() if message.text else ""
    if not prompt:
        await bot._safe_reply(message, bot._t(message.chat.id, "draw_empty"))
        return
    if await bot._reject_rate_limited_message(message):
        return
    await bot.inline_draw(message, prompt)



async def _fish_audio_tts_bytes(text: str) -> bytes | None:
    import bot
    session = await bot._get_http_session()
    return await _lumen_fish_audio_tts_bytes(
        session, text,
        api_key=bot.OPENROUTER_API_KEY, http_referer=bot.OPENROUTER_HTTP_REFERER,
        title=bot.OPENROUTER_TITLE, base_url=bot.OPENROUTER_BASE_URL,
        model_id=bot.FISH_AUDIO_TTS_MODEL, request_timeout_sec=bot.ROUTE_MODEL_TIMEOUT_SEC,
    )


async def _gemini_tts_bytes(text: str) -> tuple[bytes, str, str]:
    import bot
    def _is_rate_limit(e: Exception) -> bool:
        err_txt = bot._error_text(e).strip() or e.__class__.__name__
        return bot._classify_model_error(bot._error_status(e, err_txt), err_txt) == "rate_limit"

    # TTS пишется в провайдер "gemini" — модели видны в /stats рядом с остальными.
    # Различаем суточную квоту и минутный всплеск так же, как текстовый маршрут:
    # иначе один 429 убирал бы TTS-модель из квоты до полуночи.
    def _is_daily_quota(e: Exception) -> bool:
        err_txt = bot._error_text(e).strip() or e.__class__.__name__
        return bot._is_gemini_daily_quota(err_txt)

    return await _lumen_gemini_tts_bytes(
        bot.client, text, tts_models=bot.GEMINI_TTS_MODELS,
        is_rate_limit_error=_is_rate_limit,
        on_model_exhausted=lambda mname: bot._mark_quota_exhausted("gemini", mname),
        on_model_success=lambda mname: bot._record_quota_usage("gemini", mname),
        is_daily_quota_error=_is_daily_quota,
        on_model_rate_limited=lambda mname: bot._mark_rate_limited("gemini", mname),
        request_timeout_sec=bot.TTS_SYNTH_TIMEOUT_SEC,
    )


# Длинную озвучку бьём на части вместо отказа: кап частей — чтобы вставка
# целой статьи не сожгла дневную квоту TTS и не спамила десятками голосовых.
TTS_MAX_PARTS = 5

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…\n])\s+")

def _split_tts_chunks(text: str, limit: int) -> list[str]:
    """Режем текст на куски ≤ limit по границам предложений; одиночное
    предложение длиннее лимита — жёстко. Пустой вход — пустой список."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for piece in [p for p in _SENTENCE_SPLIT_RE.split(text) if p]:
        while len(piece) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if not piece:
            continue
        cand = (cur + " " + piece).strip()
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks or [text[:limit]]


async def _synthesize_tts_voice(text: str) -> tuple[bytes, str, int]:
    """Синтез + ffmpeg-конвертация одного куска в OGG/Opus. Тело прежнего
    inline_tts без смены логики — чанки идут тем же путём, что одиночный текст."""
    import bot
    # Fish снят с free-каталога (17.09.2026) — пропускаем мёртвую попытку, ветка оставлена (см. флаг).
    fish_bytes = await bot._fish_audio_tts_bytes(text) if bot.FISH_AUDIO_ENABLED else None
    if fish_bytes is not None:
        pcm_bytes, mime_type, used_tts_model = fish_bytes, "audio/mp3", bot.FISH_AUDIO_TTS_MODEL
        bot._record_quota_usage("openrouter", bot.FISH_AUDIO_TTS_MODEL)
    else:
        pcm_bytes, mime_type, used_tts_model = await bot._gemini_tts_bytes(text)
    log.info('[tts] Synthesis received from %s, mime_type=%s, bytes=%d', used_tts_model, mime_type, len(pcm_bytes))

    # определяем формат исходника
    if mime_type.startswith("audio/mp3") or mime_type.startswith("audio/mpeg") or pcm_bytes.startswith(b'ID3') or pcm_bytes.startswith(b'\xff\xfb'):
        src_ext = ".mp3"
        raw_audio = pcm_bytes
    elif pcm_bytes.startswith(b'RIFF') or "wav" in mime_type:
        src_ext = ".wav"
        raw_audio = pcm_bytes
    else:
        # сырой PCM сначала оборачиваем в WAV
        src_ext = ".wav"
        raw_audio = pcm_to_wav(pcm_bytes, sample_rate=24000)

    # send_voice без ffmpeg-конвертации показал бы 0:00.
    final_audio = raw_audio
    final_filename = "speech.ogg"
    voice_duration = 0
    try:
        with tempfile.TemporaryDirectory() as tdir:
            src_path = os.path.join(tdir, f"tts_src{src_ext}")
            dst_path = os.path.join(tdir, "tts_out.ogg")
            with open(src_path, "wb") as fh:
                fh.write(raw_audio)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src_path,
                "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
                dst_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _communicate_process(proc, timeout=30)
            if os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
                with open(dst_path, "rb") as fh:
                    final_audio = fh.read()
                # ffprobe — получаем длительность для Telegram (без неё показывает 0:00)
                try:
                    probe = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        dst_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    probe_out, _ = await _communicate_process(probe, timeout=10)
                    raw_dur = probe_out.decode().strip()
                    voice_duration = max(1, round(float(raw_dur))) if raw_dur else 0
                except Exception as probe_exc:
                    log.warning("[tts] ffprobe failed: %s", probe_exc)
                log.info("[tts] OGG/Opus: %d bytes, duration: %ds", len(final_audio), voice_duration)
            else:
                log.warning("[tts] ffmpeg OGG conversion failed, falling back to raw audio")
                final_filename = f"speech{src_ext}"
    except Exception as conv_exc:
        log.warning("[tts] ffmpeg conversion error: %s", conv_exc)
        final_filename = f"speech{src_ext}"
    return final_audio, final_filename, voice_duration


async def inline_tts(message: Message, text: str) -> None:
    import bot
    chunks = _split_tts_chunks(text, bot.TTS_MAX_CHARS)
    if not chunks:
        return
    if len(chunks) > TTS_MAX_PARTS:
        await bot._safe_reply(
            message,
            bot._t(message.chat.id, "tts_too_long", limit=bot.TTS_MAX_CHARS * TTS_MAX_PARTS, length=len(text)),
        )
        return
    # Дневные лимиты пользователя: общий и отдельный на озвучку (он же тратит квоту Gemini TTS).
    # Проба без создания: отказ не заводит запись и не раздувает day_users.
    # Считаем сразу все чанки: каждый — отдельный синтез, иначе длинный текст
    # уводил бы счётчик за лимит.
    uid = bot._user_key_for_message(message)
    _tts_entry = bot._user_daily_peek(uid)
    try:
        _tts_over_total = int((_tts_entry or {}).get("total") or 0) + len(chunks) > bot._user_daily_limit(uid, "total", _tts_entry)
        _tts_over_tts = int((_tts_entry or {}).get("tts") or 0) + len(chunks) > bot._user_daily_limit(uid, "tts", _tts_entry)
    except (TypeError, ValueError):
        _tts_over_total = _tts_over_tts = False
    if bot._user_daily_total_exhausted(uid, _tts_entry) or _tts_over_total:
        hours, mins = bot._user_daily_reset_in()
        # Суточный счётчик /stats: отказ по лимиту, озвучки не будет.
        bot._record_stats_event("daily_limit_denials")
        await bot._safe_reply(message, bot._t(
            message.chat.id, "user_daily_total",
            used=(_tts_entry or {}).get("total", 0), limit=bot._user_daily_limit(uid, "total", _tts_entry),
            hours=hours, mins=mins,
        ))
        return
    if bot._user_daily_tts_exhausted(uid, _tts_entry) or _tts_over_tts:
        bot._record_stats_event("daily_limit_denials")
        await bot._safe_reply(message, bot._t(
            message.chat.id, "user_daily_tts",
            used=(_tts_entry or {}).get("tts", 0), limit=bot._user_daily_limit(uid, "tts", _tts_entry),
        ))
        return
    status = await bot._tg_call(message.reply, bot._t(message.chat.id, "status_voicing"))
    # Общий дедлайн на все чанки: зависший синтез иначе держал per-chat lock без края.
    deadline = time.monotonic() + bot.TTS_TOTAL_BUDGET_SEC
    shortened = False
    try:
        # Весь синтез ДО отправки: упавший кусок — одна ошибка вместо рваного "пол-ответа + ошибка".
        voices = []
        for chunk in chunks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                shortened = True
                break
            try:
                voices.append(await asyncio.wait_for(_synthesize_tts_voice(chunk), timeout=remaining))
            except asyncio.TimeoutError:
                shortened = True
                break
        if not voices:
            # Даже первый кусок не уложился — честная ошибка, а не пустота.
            raise RuntimeError("TTS total budget exceeded before the first chunk")
    except Exception as exc:
        log.exception("TTS synthesis failed:")
        # Сырой текст ошибки содержит ID моделей — показываем классифицированный текст.
        txt = bot._error_text(exc).strip() or exc.__class__.__name__
        kind = bot._classify_model_error(bot._error_status(exc, txt), txt)
        cid = message.chat.id
        if kind == "rate_limit":
            user_err = bot._t(cid, "tts_err_exhausted")
        else:
            user_err = bot._t(cid, "tts_err_generic")
        # Статус уже снесён выше (перед send_voice): если правка не прошла — дублируем реплаем.
        if not await bot._edit_message_quietly(status, user_err):
            await bot._safe_reply(message, user_err)
        return
    await bot._delete_message_quietly(status)
    for i, (final_audio, final_filename, voice_duration) in enumerate(voices):
        # Через _tg_call: breaker-гейт, таймаут и RetryAfter вместо прямого send_voice.
        # None вместо исключения — наружу то же исключение, что раньше при прямом вызове.
        sent = await bot._tg_call(
            bot.bot.send_voice,
            chat_id=message.chat.id,
            voice=BufferedInputFile(final_audio, filename=final_filename),
            duration=voice_duration if voice_duration > 0 else None,
            reply_to_message_id=message.message_id if i == 0 else None,
            call_timeout=bot.TELEGRAM_MEDIA_TIMEOUT,
        )
        if sent is None:
            raise RuntimeError("Telegram send_voice failed: connection timeout or proxy unavailable")
        # Каждый чанк — отдельный синтез за квоту: списываем поштучно.
        bot._record_user_daily(uid, tts=True)
    bot._record_stats_event("answers_sent")
    if shortened:
        # Обрезка по общему дедлайну — говорим прямо, что озвучено начало.
        await bot._safe_reply(message, bot._t(message.chat.id, "tts_shortened"))

async def cmd_tts(message: Message) -> None:
    import bot
    if getattr(message, "from_user", None) is not None and bot._is_banned(message.from_user.id):
        return
    text = message.text.partition(" ")[2].strip() if message.text else ""
    if not text:
        await bot._safe_reply(message, bot._t(message.chat.id, "tts_empty"))
        return
    if await bot._reject_rate_limited_message(message):
        return
    await bot.inline_tts(message, text)


async def cmd_reset(message: Message) -> None:
    """Сброс истории чата. В меню. Личка — всем, группа — админам/владельцу."""
    import bot
    if getattr(message, "from_user", None) is not None and bot._is_banned(message.from_user.id):
        return
    requester_id = message.from_user.id if message.from_user else None
    if not await bot._is_privileged_in_chat(message.chat.type, message.chat.id, requester_id):
        await bot._tg_call(
            message.reply,
            bot._t(message.chat.id, "reset_deny")
        )
        return
    state = bot.get_state(message.chat.id)
    state["history"] = []
    state["ctx"].clear()
    bot.mark_state_dirty(message.chat.id)
    await bot._tg_call(
        message.reply,
        bot._t(message.chat.id, "reset_done")
    )

# ── Язык системных сообщений (/lang) — ответы ИИ не трогает (см. RESPONSE LANGUAGE в system_prompt.py). Права как у /reset; кнопки без флагов/эмодзи, текущий — с ✓, меню по алфавиту кода.
async def cmd_lang(message: Message) -> None:
    import bot
    if getattr(message, "from_user", None) is not None and bot._is_banned(message.from_user.id):
        return
    lang = bot._chat_lang(message.chat.id)
    rows = []
    codes = list(SUPPORTED_LANGS)
    for i in range(0, len(codes), 2):
        row = []
        for code in codes[i:i + 2]:
            mark = " ✓" if code == lang else ""
            row.append(InlineKeyboardButton(
                text=f"{LANG_NAMES[code]}{mark}",
                callback_data=f"lang:{code}",
            ))
        rows.append(row)
    await bot._tg_call(
        message.reply,
        bot._t(message.chat.id, "lang_title"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def handle_lang_callback(query: CallbackQuery) -> None:
    """Кнопка языка ("lang:<код>", влезает в лимит 64 байт): права, сохранение, подтверждение на новом языке."""
    import bot
    if getattr(query, "from_user", None) is not None and bot._is_banned(query.from_user.id):
        with contextlib.suppress(Exception):
            await query.answer()
        return
    data = query.data or ""
    if not data.startswith("lang:"):
        return
    parts = data.split(":")
    if len(parts) != 2:
        with contextlib.suppress(Exception):
            await query.answer()
        return
    code = normalize_lang(parts[1])
    question_msg = query.message
    if question_msg is None or question_msg.chat is None:
        with contextlib.suppress(Exception):
            await query.answer()
        return
    chat = question_msg.chat
    requester_id = query.from_user.id if query.from_user else None
    if not await bot._is_privileged_in_chat(chat.type, chat.id, requester_id):
        with contextlib.suppress(Exception):
            await query.answer(bot._t(chat.id, "lang_deny"), show_alert=True)
        return
    state = bot.get_state(chat.id)
    state["lang"] = code
    bot.mark_state_dirty(chat.id)
    with contextlib.suppress(Exception):
        await query.answer()
    with contextlib.suppress(Exception):
        await bot._tg_call(
            question_msg.edit_text,
            _lang_t(code, "lang_done"),
            parse_mode=None, call_timeout=15.0,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )

# выгрузка логов (только для владельца)

async def cmd_logs(message: Message) -> None:
    import bot
    is_owner = bot._is_owner(message.from_user.id if message.from_user else None)

    if not is_owner:
         await bot._tg_call(message.reply, bot._t(message.chat.id, "logs_deny"))
         return

    if message.chat.type != ChatType.PRIVATE:
        # Результат команды видят ВСЕ в чате — файл логов с ID моделей в группы не отдаём.
        await bot._tg_call(message.reply, bot._t(message.chat.id, "logs_group_only"))
        return

    # Флашим хендлеры _LOG_LISTENER (у root — только QueueHandler с no-op flush).
    try:
        if bot._LOG_LISTENER is not None:
            for handler in bot._LOG_LISTENER.handlers:
                handler.flush()
    except Exception:
        pass

    try:
        log_content = ""
        if bot.LOG_FILE_PATH.exists():
            with open(bot.LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                log_content = f.read()

        if not log_content or len(log_content.strip()) == 0:
            await bot._tg_call(message.reply, bot._t(message.chat.id, "logs_empty"))
            return

        # вычищаем токены из логов перед отправкой — единый список, см. _redactable_secrets
        for secret in bot._redactable_secrets():
            log_content = log_content.replace(secret, "<REDACTED>")

        # пишем во временный файл, чтобы не ловить блокировку на живом логе
        fd, temp_log_path = tempfile.mkstemp(prefix="logs_", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="ignore") as f:
                f.write(log_content)

            await bot._tg_call(message.reply_document, FSInputFile(temp_log_path, filename="logs.txt"))
        finally:
            try:
                os.unlink(temp_log_path)
            except Exception:
                pass
    except Exception as exc:
        log.exception("Error extracting or sending logs:")
        await bot._tg_call(message.reply, bot._t(message.chat.id, "logs_send_error", error=exc))

async def cmd_stats(message: Message) -> None:
    """Статистика (скрыта, только владелец — данные по всем чатам)."""
    import bot
    is_owner = bot._is_owner(message.from_user.id if message.from_user else None)
    if not is_owner:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_deny"))
        return

    if message.chat.type != ChatType.PRIVATE:
    # ID моделей в группы не отдаём — та же проверка, что в /logs.
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_group_only"))
        return

    # Счётчики — по датам Google (сброс в _reset_quota_if_new_day проверяем перед отрисовкой: фоновый тик мог не успеть, а used копились через рестарты).
    bot._reset_quota_if_new_day()

    # Вебхук опрашиваем параллельно со сборкой текста: иначе висящий API
    # держал бы всю команду до 10с уже после готовой статистики.
    _webhook_task = asyncio.ensure_future(_webhook_info_text())

    total_chats = len(bot.chat_state)
    # "Активные" — с живой активностью за сутки по настенным часам (monotonic
    # сбрасывался рестартом и считал активными всех). Без метки — неактивен.
    _active_cutoff = time.time() - 24 * 3600

    def _is_active(s: Any) -> bool:
        ts = s.get("last_activity") if isinstance(s, dict) else None
        return isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= _active_cutoff

    active_chats = sum(1 for s in bot.chat_state.values() if _is_active(s))
    uptime_sec = int(time.monotonic() - bot._PROCESS_START_MONOTONIC)
    uptime_str = f"{uptime_sec // 3600}ч {(uptime_sec % 3600) // 60}м"

    gemini_quota = bot.GLOBAL_QUOTA.get("gemini", {})
    _stats_lang = bot._chat_lang(message.chat.id)
    _limitTag = _lang_t(_stats_lang, "stats_limit_used")
    _noData = _lang_t(_stats_lang, "stats_no_data")

    def _quota_section(quota: dict, daily_limit: int | None, per_model_limits: dict | None = None) -> str:
        # Показываем только модели с движением (расход или метка исчерпания) — нули по
        # давно мёртвым моделям копятся в хранилище через рестарты и превращали вывод в простыню.
        items = sorted(quota.items(), key=lambda kv: -(kv[1].get("used") or 0))
        used_total = sum(int(e.get("used") or 0) for _, e in items)
        active = [(mid, e) for mid, e in items if (e.get("used") or 0) or e.get("exhausted_at")]
        lines = []
        for mid, e in active:
            line = f"  • {mid}: {e.get('used', 0)}"
            # Лимит Gemini — только где задан в env (у каждой модели свой RPD).
            _mlim = (per_model_limits or {}).get(mid)
            if isinstance(_mlim, int) and not isinstance(_mlim, bool) and _mlim > 0:
                line += f" / {_mlim}"
            if e.get('exhausted_at'):
                line += _limitTag
            lines.append(line)
        idle = len(items) - len(active)
        if idle:
            lines.append(f"  …и ещё {idle} без обращений")
        total = f"  Σ: {used_total}"
        if daily_limit is not None:
            total += f" / {daily_limit} (осталось {max(0, daily_limit - used_total)})"
        lines.append(total)
        return "\n".join(lines) or _noData

    gemini_text = _quota_section(gemini_quota, None, bot.GEMINI_DAILY_LIMITS)
    or_text = _quota_section(bot.GLOBAL_QUOTA.get("openrouter", {}), bot.OPENROUTER_DAILY_LIMIT)
    groq_text = _quota_section(bot.GLOBAL_QUOTA.get("groq", {}), bot.GROQ_DAILY_LIMIT)
    _quarantined_now = bot._quarantine_status()
    if _quarantined_now:
        quarantine_text = "\n".join(
            f"  • {provider}/{mid}: {bad} плохих подряд (до конца суток)" for provider, mid, bad in _quarantined_now
        )
    else:
        quarantine_text = "  нет"

    # Суточные счётчики: уникальные пользователи — только числом, без ID.
    _day_stats = bot._stats_entry()
    _day_users = bot.GLOBAL_QUOTA.get(bot.USER_DAILY_KEY)
    day_users = len(_day_users) if isinstance(_day_users, dict) else 0

    # Состояние прокси — из самого breaker'а (раньше команда лезла в четыре глобала напрямую).
    proxy_line = bot._tg_proxy_breaker.status_text()

    quota_day = bot.GLOBAL_QUOTA.get("quota_day") or "—"
    webhook_text = await _webhook_task

    text = (
        f"<b>Статистика Lumen</b>\n"
        f"Сборка: {_build_version()}\n"
        f"Активных чатов (24ч): {active_chats} (всего: {total_chats})\n"
        f"Аптайм процесса: {uptime_str}\n"
        f"Счётчики квоты за сутки: {quota_day} (America/Los_Angeles, сбрасываются автоматически)\n"
        f"Пользователей за сутки: {day_users}\n"
        f"Сообщений получено: {_day_stats.get('messages_received', 0)}, ответов отправлено: {_day_stats.get('answers_sent', 0)}\n"
        f"Все модели отказали: {_day_stats.get('all_failed', 0)}, "
        f"переключений на резерв: {_day_stats.get('fallbacks', 0)}, "
        f"отказов по лимиту: {_day_stats.get('daily_limit_denials', 0)}\n\n"
        f"<b>Gemini — запросов по моделям:</b>\n{gemini_text}\n\n"
        f"<b>OpenRouter — запросов по моделям:</b>\n{or_text}\n\n"
        f"<b>Groq — запросов по моделям:</b>\n{groq_text}\n\n"
        f"<b>Карантин моделей:</b>\n{quarantine_text}\n\n"
        f"Вебхук: {webhook_text}\n"
        f"Память: {_process_memory_text()}\n"
        f"Хранилище: {_storage_backend_text()}"
        f"{proxy_line}"
    )
    # Короткий вывод для одного сообщения: режем по целым строкам, чтобы не
    # разорвать HTML-тег и не получить 400 от Telegram при parse_mode=HTML.
    if len(text) > bot.TG_MAX_LEN:
        cut = text[:bot.TG_MAX_LEN - 1]
        nl = cut.rfind("\n")
        if nl > bot.TG_MAX_LEN // 2:
            cut = cut[:nl]
        # Оборванный тег (<b ... без >) — откатываемся до его начала.
        lt, gt = cut.rfind("<"), cut.rfind(">")
        if lt > gt:
            cut = cut[:lt]
        text = cut.rstrip() + "…"
    await bot._tg_call(message.reply, text, parse_mode=ParseMode.HTML)


_SELFTEST_LAST_RUN_MONOTONIC = 0.0
SELFTEST_COOLDOWN_SEC = 60.0

def _selftest_row(label: str, ok: bool, detail: str, elapsed: float) -> str:
    """Одна строка итога без ID моделей (в группы команда не ходит вовсе)."""
    base = f"{label}: {'OK' if ok else 'FAIL'} ({elapsed:.1f}s)"
    return base if ok or not detail else f"{base} — {detail}"

async def cmd_selftest(message: Message) -> None:
    """Живая проверка голов маршрутов: по крошечной пробе в Groq, лёгкий OpenRouter
    и сеть (прокси Telegram, TikWM) логикой /diag. Gemini — только по явному
    аргументу, квота самая дефицитная. Только владелец, только личка, не чаще
    раза в минуту. Успешные пробы честно пишутся в квоту."""
    import bot
    global _SELFTEST_LAST_RUN_MONOTONIC
    if not bot._is_owner(message.from_user.id if message.from_user else None):
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_deny"))
        return
    if message.chat.type != ChatType.PRIVATE:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "selftest_group_only"))
        return
    now = time.monotonic()
    # Ноль — «ещё ни разу», а не время: monotonic() свежего процесса тоже
    #тики от загрузки, и 60 − 45 давали ложный кулдаун первому запуску (CI).
    if _SELFTEST_LAST_RUN_MONOTONIC > 0:
        wait = SELFTEST_COOLDOWN_SEC - (now - _SELFTEST_LAST_RUN_MONOTONIC)
        if wait > 0:
            await bot._tg_call(message.reply, bot._t(message.chat.id, "selftest_cooldown", sec=int(wait) + 1))
            return
    # Метку ставим до проб: повторный вызов во время долгой проверки тоже ждёт.
    _SELFTEST_LAST_RUN_MONOTONIC = now
    args = (getattr(message, "text", "") or "").split()
    want_gemini = len(args) > 1 and args[1].split("@")[0].lower() == "gemini"
    rows: list[str] = []
    summary: list[str] = []
    for provider, label in (("groq", "Groq"), ("openrouter", "OpenRouter")):
        ok, detail, elapsed = await bot.selftest_llm_head(provider, chat_id=message.chat.id)
        rows.append(_selftest_row(label, ok, detail, elapsed))
        summary.append(f"{provider}={'ok' if ok else 'FAIL'}/{elapsed:.1f}s")
    if want_gemini:
        ok, detail, elapsed = await bot.selftest_llm_head("gemini", chat_id=message.chat.id)
        rows.append(_selftest_row("Gemini", ok, detail, elapsed))
        summary.append(f"gemini={'ok' if ok else 'FAIL'}/{elapsed:.1f}s")
    try:
        session = await bot._get_http_session()
        token = bot.BOT_TOKEN or ""
        proxy_url = bot.TELEGRAM_API_BASE_URL + "/bot" + (token[:6] if token else "x") + "/getMe"
        net = await asyncio.gather(
            bot.probe_url(session, proxy_url, timeout_sec=6.0, redact=token),
            bot.probe_url(session, "https://www.tikwm.com", timeout_sec=6.0),
        )
    except Exception as exc:
        net = [
            {"ok": False, "elapsed_sec": 0.0, "error": f"{exc.__class__.__name__}"},
            {"ok": False, "elapsed_sec": 0.0, "error": f"{exc.__class__.__name__}"},
        ]
    for label, res in (("TG-proxy", net[0]), ("TikWM", net[1])):
        detail = "" if res.get("ok") else str(res.get("error") or res.get("status") or "error")[:120]
        rows.append(_selftest_row(label, bool(res.get("ok")), detail, float(res.get("elapsed_sec") or 0.0)))
        summary.append(f"{label}={'ok' if res.get('ok') else 'FAIL'}")
    text = bot._t(message.chat.id, "selftest_header") + "\n" + "\n".join(rows)
    if not want_gemini:
        text += "\n" + bot._t(message.chat.id, "selftest_gemini_skipped")
    await bot._tg_call(message.reply, text)
    log.info("[selftest] chat=%s gemini_arg=%s %s", message.chat.id, want_gemini, " ".join(summary))


def _ban_target_id(message: Message) -> int | None:
    """ID цели /ban//unban: ответ на сообщение или числовой аргумент."""
    replied = getattr(message, "reply_to_message", None)
    replied_user = getattr(replied, "from_user", None) if replied is not None else None
    replied_id = getattr(replied_user, "id", None)
    if isinstance(replied_id, int) and not isinstance(replied_id, bool):
        return replied_id
    args = (getattr(message, "text", "") or "").split()[1:]
    if args:
        raw = args[0].split("@")[0].lstrip("+")
        if raw.isdigit():
            return int(raw)
    return None

async def cmd_ban(message: Message) -> None:
    """Владельческая блокировка (только владелец, только личка)."""
    import bot
    if not bot._is_owner(message.from_user.id if message.from_user else None):
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_deny"))
        return
    if message.chat.type != ChatType.PRIVATE:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_group_only"))
        return
    target = _ban_target_id(message)
    if target is None:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_usage"))
        return
    if bot._is_owner(target):
        await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_owner_refuse"))
        return
    bot._ban_user(target)
    await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_done", user_id=target))
    log.info("[ban] Owner banned user %s", target)

async def cmd_unban(message: Message) -> None:
    """Снятие владельческой блокировки (только владелец, только личка)."""
    import bot
    if not bot._is_owner(message.from_user.id if message.from_user else None):
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_deny"))
        return
    if message.chat.type != ChatType.PRIVATE:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_group_only"))
        return
    target = _ban_target_id(message)
    if target is None:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_usage"))
        return
    if bot._unban_user(target):
        await bot._tg_call(message.reply, bot._t(message.chat.id, "unban_done", user_id=target))
        log.info("[ban] Owner unbanned user %s", target)
    else:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "unban_missing", user_id=target))

async def cmd_banlist(message: Message) -> None:
    """Список заблокированных (только владелец, только личка)."""
    import bot
    if not bot._is_owner(message.from_user.id if message.from_user else None):
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_deny"))
        return
    if message.chat.type != ChatType.PRIVATE:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "ban_group_only"))
        return
    ids = bot._banned_list()
    if not ids:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "banlist_empty"))
        return
    text = bot._t(message.chat.id, "banlist_header") + "\n" + "\n".join(f"• {uid}" for uid in ids)
    # Список растёт руками, но упереться в лимит Telegram не должен: режем по
    # строкам, как вывод /stats выше.
    if len(text) > bot.TG_MAX_LEN:
        cut = text[:bot.TG_MAX_LEN - 1]
        nl = cut.rfind("\n")
        if nl > bot.TG_MAX_LEN // 2:
            cut = cut[:nl]
        text = cut.rstrip() + "…"
    await bot._tg_call(message.reply, text)


_BUILD_VERSION_CACHED: str | None = None


def _build_version() -> str:
    """Короткий хеш сборки для /stats: env сборки, иначе git, иначе unknown."""
    global _BUILD_VERSION_CACHED
    # git-вызов блокирует loop до 5с — считаем один раз за жизнь процесса.
    if _BUILD_VERSION_CACHED is not None:
        return _BUILD_VERSION_CACHED
    for env_name in ("LUMEN_BUILD_VERSION", "BUILD_VERSION", "GIT_COMMIT", "COMMIT_SHA"):
        raw = os.getenv(env_name, "").strip()
        if raw:
            _BUILD_VERSION_CACHED = raw[:12]
            return _BUILD_VERSION_CACHED
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = (proc.stdout or "").strip()
        if proc.returncode == 0 and sha:
            _BUILD_VERSION_CACHED = sha[:12]
            return _BUILD_VERSION_CACHED
    except Exception:
        pass
    _BUILD_VERSION_CACHED = "unknown"
    return "unknown"


_WEBHOOK_INFO_TIMEOUT_SEC = 10.0


async def _webhook_info_text() -> str:
    """Строка про вебхук для /stats: очередь и последняя ошибка, иначе н/д."""
    import bot
    try:
        if bot.bot is None:
            return "н/д"
        info = await asyncio.wait_for(bot.bot.get_webhook_info(), timeout=_WEBHOOK_INFO_TIMEOUT_SEC)
    except Exception:
        # Сбой/таймаут не роняют всю статистику: показываем н/д.
        return "н/д"
    try:
        pending = getattr(info, "pending_update_count", None)
        if isinstance(pending, bool) or not isinstance(pending, int):
            return "н/д"
        err_msg = (getattr(info, "last_error_message", None) or "").strip()
        if len(err_msg) > 120:
            err_msg = err_msg[:117] + "..."
        if err_msg:
            return f"pending={pending}, ошибка: {err_msg}"
        return f"pending={pending}, ошибок нет"
    except Exception:
        return "н/д"


def _process_memory_text() -> str:
    """RSS процесса для /stats: сначала текущий (Linux), иначе пик, иначе н/д."""
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            rss_pages = int(fh.read().split()[1])
        mb = rss_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
        return f"{mb:.0f} МБ"
    except Exception:
        pass
    try:
        import resource
        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS отдаёт байты, Linux — килобайты; на Windows модуля нет вообще.
        if getattr(os, "uname", None) is not None and os.uname().sysname == "Darwin":
            rss /= 1024 * 1024
        else:
            rss /= 1024
        return f"{rss:.0f} МБ"
    except Exception:
        return "н/д"


def _storage_backend_text() -> str:
    """Бэкенд хранилища и время последней успешной записи для /stats."""
    import bot
    backend = "Upstash" if bot.USE_UPSTASH else "локальный диск"
    ts = bot._last_storage_write_ts()
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        when = time.strftime("%d.%m %H:%M", time.localtime(ts))
        return f"{backend}, запись: {when}"
    return f"{backend}, записей пока не было"


async def _send_pick_question(message: Message, scenario: str, original_text: str) -> None:
    """Отправляет уточняющий вопрос с кнопками и запоминает контекст выбора.
    token в callback_data короткий (лимит Telegram — 64 байта на всю строку)."""
    import bot
    _purge_expired_picks()
    _enforce_pending_picks_cap()
    token = secrets.token_hex(8)
    lang = bot._chat_lang(message.chat.id)
    bot._pending_picks[token] = {
        "chat_id": message.chat.id,
        "user_id": message.from_user.id if message.from_user else None,
        "scenario": scenario,
        "original": original_text,
        "expires": time.monotonic() + PICK_TTL_SEC,
        "lang": lang,
    }
    question, options, _tpl = pick_texts(lang, scenario)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=option, callback_data=f"pick:{token}:{idx}")]
        for idx, option in enumerate(options)
    ])
    await bot._tg_call(
        message.reply,
        question + bot._t(message.chat.id, "pick_suffix"),
        reply_markup=keyboard,
    )


async def handle_pick_callback(query: CallbackQuery) -> None:
    """Кнопки-уточнения: чужие отклоняем, протухшие известные перевыпускаем разок, выбор дописываем к запросу и гоним обычным путём (_handle_message_core)."""
    import bot
    if getattr(query, "from_user", None) is not None and bot._is_banned(query.from_user.id):
        # Забаненный и кнопками не отвечает: иначе игнор обходился живыми пиками.
        with contextlib.suppress(Exception):
            await query.answer()
        return
    data = query.data or ""
    if not data.startswith("pick:"):
        return
    parts = data.split(":")
    if len(parts) != 3:
        with contextlib.suppress(Exception):
            await query.answer()
        return
    _, token, idx_raw = parts
    try:
        idx = int(idx_raw)
    except (TypeError, ValueError):
        with contextlib.suppress(Exception):
            await query.answer()
        return
    # Сначала все проверки по записи, удаление — только перед делом: чужой тап
    # или кривой индекс больше не сжигают кнопку владельца. Атомарность та же,
    # что у прежнего pop-first: всё синхронно до первого await ниже.
    rec = bot._pending_picks.get(token)
    # Язык для служебных реплик: из записи (если есть), иначе из чата кнопки.
    # Фолбэк без создания записи: обычный _chat_lang через get_state заводил бы чат
    # даже на чужой тап по неизвестному токену.
    _qchat = query.message.chat.id if query.message and query.message.chat else None
    rec_lang = (rec or {}).get("lang") or bot._peek_chat_lang(_qchat)

    def _is_rec_owner(rec_rec: dict) -> bool:
        """Чей это выбор. Запись без user_id (пост канала/аноним) принадлежит чату:
        иначе любой участник нажал бы чужую кнопку и сжёг токен (аудит 26.09.2026)."""
        owner_id = rec_rec.get("user_id")
        if owner_id is not None and query.from_user is not None and query.from_user.id != owner_id:
            return False
        if owner_id is None and query.message is not None and query.message.chat is not None:
            return query.message.chat.id == rec_rec.get("chat_id")
        return True

    if rec is not None and not _is_rec_owner(rec):
        # Проверка авторства ДО перевыпуска: иначе чужой тап по протухшей кнопке
        # продлевал бы чужой выбор новыми кнопками (враждебное ревью 27.09.2026).
        with contextlib.suppress(Exception):
            await query.answer(_lang_t(rec_lang, "pick_not_yours"), show_alert=True)
        return

    if rec is None or rec["expires"] < time.monotonic():
        if rec is not None and query.message is not None:
            # Протухший известный выбор — молча свежие кнопки вместо стены текста.
            # Один ресенд: второй тап упрётся в rec None и честно покажет expired.
            bot._pending_picks.pop(token, None)
            _purge_expired_picks()
            _enforce_pending_picks_cap()
            fresh = secrets.token_hex(8)
            bot._pending_picks[fresh] = {
                "chat_id": rec.get("chat_id"),
                "user_id": rec.get("user_id"),
                "scenario": rec.get("scenario", ""),
                "original": rec.get("original", ""),
                "expires": time.monotonic() + PICK_TTL_SEC,
                "lang": rec_lang,
            }
            _rq, _ropts, _tpl = pick_texts(rec_lang, rec.get("scenario", ""))
            with contextlib.suppress(Exception):
                await query.answer()
            with contextlib.suppress(Exception):
                await bot._tg_call(
                    query.message.edit_text,
                    _rq + bot._t(rec.get("chat_id"), "pick_suffix"),
                    parse_mode=None, call_timeout=15.0,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=opt, callback_data=f"pick:{fresh}:{i}")]
                        for i, opt in enumerate(_ropts)
                    ]),
                )
            return
        with contextlib.suppress(Exception):
            await query.answer(_lang_t(rec_lang, "pick_expired"), show_alert=False)
        return
    _q, options, tpl = pick_texts(rec_lang, rec.get("scenario", ""))
    if not 0 <= idx < len(options):
        with contextlib.suppress(Exception):
            await query.answer()
        return
    # Все проверки пройдены — только теперь забираем токен (см. комментарий у get выше).
    # Без сообщения кнопки не во что упереть: токен не трогаем, иначе тап из инлайн
    # режима сжёг бы чужой выбор без дела.
    question_msg = query.message
    if question_msg is None:
        with contextlib.suppress(Exception):
            await query.answer()
        return
    bot._pending_picks.pop(token, None)
    choice = options[idx]
    with contextlib.suppress(Exception):
        await query.answer()
    with contextlib.suppress(Exception):
        # Клавиатуру снимаем пустой разметкой, иначе кнопки повиснут (повторный тап всё равно упрётся в pop).
        await bot._tg_call(
            question_msg.edit_text,
            f"{_q}\n\n{_lang_t(rec_lang, 'pick_choice', choice=choice)}",
            parse_mode=None, call_timeout=15.0,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    augmented = tpl.format(original=rec["original"], choice=choice)
    chat = question_msg.chat
    from_user = query.from_user

    async def _pick_reply(text: str, **kwargs: Any) -> Any:
        return await bot._tg_call(
            bot.bot.send_message, chat_id=chat.id, text=text,
            reply_to_message_id=getattr(question_msg, "message_id", None),
            **kwargs,
        )

    ns = SimpleNamespace(
        text=augmented, caption=None, chat=chat, from_user=from_user,
        sender_chat=None, reply_to_message=None,
        message_id=getattr(question_msg, "message_id", None),
        reply=_pick_reply,
    )
    # Флаг против зацикливания: дополненный текст всё ещё матчит детектор — без флага снова ушёл бы в кнопки.
    ns._pick_resolved = True
    try:
        # Тот же лимит лока, что и в основном пути (bot.CHAT_LOCK_TIMEOUT_SEC):
        # третье значение в 10с отдавало «занято» на любом живом маршруте.
        lock = await bot.acquire_chat_lock(chat.id, bot.CHAT_LOCK_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        log.warning("[pick] Timeout waiting for lock on chat %s", chat.id)
        with contextlib.suppress(Exception):
            await query.answer(_lang_t(rec_lang, "pick_lock_busy"), show_alert=True)
        return
    try:
        await bot._handle_message_core(ns)
    finally:
        with contextlib.suppress(Exception):
            lock.release()
