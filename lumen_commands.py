"""
lumen_commands.py — команды бота, TTS/Draw-пайплайны и кнопки-уточнения. Связи с bot.py — только через отложенный `import bot`; bot.py реэкспортирует имена и регистрирует хендлеры.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
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
    await bot._tg_call(
        message.reply,
        bot._t(message.chat.id, "start_text"),
        parse_mode=ParseMode.HTML,
    )

async def inline_draw(message: Message, prompt: str) -> None:
    import bot
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
                image_bytes = await bot._pollinations_text_to_image(session, attempt_model, prompt)
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
            await bot.bot.send_photo(
                chat_id=message.chat.id,
                photo=BufferedInputFile(image_bytes, filename="generated.jpg"),
                reply_to_message_id=message.message_id,
            )
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
        await bot._edit_message_quietly(status, user_err)


async def cmd_draw(message: Message) -> None:
    import bot
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
    return await _lumen_gemini_tts_bytes(
        bot.client, text, tts_models=bot.GEMINI_TTS_MODELS,
        is_rate_limit_error=_is_rate_limit,
        on_model_exhausted=lambda mname: bot._mark_quota_exhausted("gemini", mname),
        on_model_success=lambda mname: bot._record_quota_usage("gemini", mname),
    )


async def inline_tts(message: Message, text: str) -> None:
    import bot
    if len(text) > bot.TTS_MAX_CHARS:
        await bot._safe_reply(
            message,
            bot._t(message.chat.id, "tts_too_long", limit=bot.TTS_MAX_CHARS, length=len(text)),
        )
        return
    status = await bot._tg_call(message.reply, bot._t(message.chat.id, "status_voicing"))
    try:
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

        await bot._delete_message_quietly(status)
        await bot.bot.send_voice(
            chat_id=message.chat.id,
            voice=BufferedInputFile(final_audio, filename=final_filename),
            duration=voice_duration if voice_duration > 0 else None,
            reply_to_message_id=message.message_id
        )
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
        await bot._edit_message_quietly(status, user_err)

async def cmd_tts(message: Message) -> None:
    import bot
    text = message.text.partition(" ")[2].strip() if message.text else ""
    if not text:
        await bot._safe_reply(message, bot._t(message.chat.id, "tts_empty"))
        return
    if await bot._reject_rate_limited_message(message):
        return
    await bot.inline_tts(message, text)


async def cmd_reset(message: Message) -> None:
    """Сброс истории чата. Скрыта из меню (как /logs). Личка — всем, группа — админам/владельцу."""
    import bot
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
        tmp_dir = tempfile.gettempdir()
        temp_log_path = os.path.join(tmp_dir, "logs.txt")
        with open(temp_log_path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(log_content)

        await bot._tg_call(message.reply_document, FSInputFile(temp_log_path, filename="logs.txt"))

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

    total_chats = len(bot.chat_state)
    # "Активные" — с живой активностью за сутки, а не все записи в памяти (те копятся до пруна на 5000).
    _active_cutoff = time.monotonic() - 24 * 3600
    active_chats = sum(
        1 for s in bot.chat_state.values()
        if isinstance(s, dict) and s.get("last_activity", 0) >= _active_cutoff
    )
    uptime_sec = int(time.monotonic() - bot._PROCESS_START_MONOTONIC)
    uptime_str = f"{uptime_sec // 3600}ч {(uptime_sec % 3600) // 60}м"

    gemini_quota = bot.GLOBAL_QUOTA.get("gemini", {})
    _stats_lang = bot._chat_lang(message.chat.id)
    _limitTag = _lang_t(_stats_lang, "stats_limit_used")
    _noData = _lang_t(_stats_lang, "stats_no_data")

    def _quota_section(quota: dict, daily_limit: int | None) -> str:
        # Показываем только модели с движением (расход или метка исчерпания) — нули по
        # давно мёртвым моделям копятся в хранилище через рестарты и превращали вывод в простыню.
        items = sorted(quota.items(), key=lambda kv: -(kv[1].get("used") or 0))
        used_total = sum(int(e.get("used") or 0) for _, e in items)
        active = [(mid, e) for mid, e in items if (e.get("used") or 0) or e.get("exhausted_at")]
        lines = [
            f"  • {mid}: {e.get('used', 0)}{_limitTag if e.get('exhausted_at') else ''}"
            for mid, e in active
        ]
        idle = len(items) - len(active)
        if idle:
            lines.append(f"  …и ещё {idle} без обращений")
        total = f"  Σ: {used_total}"
        if daily_limit is not None:
            total += f" / {daily_limit} (осталось {max(0, daily_limit - used_total)})"
        lines.append(total)
        return "\n".join(lines) or _noData

    gemini_text = _quota_section(gemini_quota, None)
    # OpenRouter — дневной лимит free-моделей 50 (с $10 — 1000, тогда остаток врёт в меньшую сторону).
    or_text = _quota_section(bot.GLOBAL_QUOTA.get("openrouter", {}), 50)
    # Groq — дневной лимит free-плана 1000 запросов (калибровка 21.09.2026).
    groq_text = _quota_section(bot.GLOBAL_QUOTA.get("groq", {}), 1000)

    # Состояние прокси — из самого breaker'а (раньше команда лезла в четыре глобала напрямую).
    proxy_line = bot._tg_proxy_breaker.status_text()

    quota_day = bot.GLOBAL_QUOTA.get("quota_day") or "—"

    text = (
        f"<b>Статистика Lumen</b>\n\n"
        f"Активных чатов (24ч): {active_chats} (всего: {total_chats})\n"
        f"Аптайм процесса: {uptime_str}\n"
        f"Счётчики квоты за сутки: {quota_day} (America/Los_Angeles, сбрасываются автоматически)\n\n"
        f"<b>Gemini — запросов по моделям:</b>\n{gemini_text}\n\n"
        f"<b>OpenRouter — запросов по моделям:</b>\n{or_text}\n\n"
        f"<b>Groq — запросов по моделям:</b>\n{groq_text}"
        f"{proxy_line}"
    )
    await bot._tg_call(message.reply, text, parse_mode=ParseMode.HTML)


async def _send_pick_question(message: Message, scenario: str, original_text: str) -> None:
    """Отправляет уточняющий вопрос с кнопками и запоминает контекст выбора.
    token в callback_data короткий (лимит Telegram — 64 байта на всю строку)."""
    import bot
    _purge_expired_picks()
    _enforce_pending_picks_cap()
    token = secrets.token_hex(4)
    lang = bot._chat_lang(message.chat.id)
    bot._pending_picks[token] = {
        "chat_id": message.chat.id,
        "user_id": message.from_user.id if message.from_user else None,
        "scenario": scenario,
        "original": original_text,
        "expires": time.monotonic() + bot.PICK_TTL_SEC,
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
    """Кнопки-уточнения: чужие/протухшие отклоняем, выбор дописываем к запросу и гоним обычным путём (_handle_message_core)."""
    import bot
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
    # Pop сразу: повторный тап не плодит второй ответ (чужая кнопка "сгорает" — владелец переспросит текстом).
    rec = bot._pending_picks.pop(token, None)
    # Язык для служебных реплик: из записи (если есть), иначе из чата кнопки.
    _qchat = query.message.chat.id if query.message and query.message.chat else None
    rec_lang = (rec or {}).get("lang") or bot._chat_lang(_qchat)
    if rec is None or rec["expires"] < time.monotonic():
        with contextlib.suppress(Exception):
            await query.answer(_lang_t(rec_lang, "pick_expired"), show_alert=False)
        return
    _q, options, tpl = pick_texts(rec_lang, rec.get("scenario", ""))
    if not 0 <= idx < len(options):
        with contextlib.suppress(Exception):
            await query.answer()
        return
    if rec["user_id"] is not None and query.from_user is not None and query.from_user.id != rec["user_id"]:
        with contextlib.suppress(Exception):
            await query.answer(_lang_t(rec_lang, "pick_not_yours"), show_alert=True)
        return
    choice = options[idx]
    with contextlib.suppress(Exception):
        await query.answer()
    question_msg = query.message
    if question_msg is None:
        return
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
    lock = bot.get_chat_lock(chat.id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=10.0)
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
