"""
lumen_commands.py — команды бота, TTS/Draw-пайплайны и кнопки-уточнения
(вынесено из bot.py, P2 аудита).

Связи с рантаймом bot.py — только через отложенный `import bot` внутри функций
(модульного цикла нет). bot.py реэкспортирует имена и регистрирует хендлеры
в диспетчере явными dp.*.register() — `bot.cmd_draw` и т.п. в тестах
не менялись.
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
        # УБРАНО (аудит техдолга, 19 августа 2026): раньше основная модель бралась
        # из ручного выбора пользователя (/imgmodel, команда удалена — см. README,
        # "Автоматический выбор модели"). Теперь _pick_image_model сама подбирает
        # модель по содержимому промпта на каждый вызов, без какого-либо состояния
        # чата — тот же принцип, что уже применяется к тексту (_build_route).
        primary_model = _pick_image_model(prompt)

        # Фолбэк-цепочка: пробуем сначала подобранную модель,
        # при ошибке переключаемся на следующие по порядку из POLLINATIONS_IMAGE_MODELS
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
                # Статус нейтральный, без названий моделей: бот не раскрывает
                # внутреннюю реализацию (см. ИДЕНТИЧНОСТЬ в system_prompt.py).
                # На первой попытке статус и так "Генерирую изображение" — не трогаем.
                if attempt_model != primary_model:
                    await bot._edit_message_quietly(status, bot._t(message.chat.id, "status_taking_longer"))
                image_bytes = await bot._pollinations_text_to_image(session, attempt_model, prompt)
                break
            except Exception as exc:
                last_error = exc
                txt = bot._error_text(exc).lower()
                # 429 — перегрузка ВСЕГО сервиса (см. прод 17.09.2026: все 5 моделей
                # вернули 429 подряд), а не одной модели — гонять остаток цепочки
                # бессмысленно, сразу говорим пользователю подождать.
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
            # Сырой текст ошибки провайдера пользователю не показываем (см. log.exception
            # выше) — та же логика, что и в остальных обработчиках ошибок бота.
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

    # Пишем в тот же провайдер "gemini" — /stats уже показывает GLOBAL_QUOTA["gemini"]
    # по всем моделям отсортированным по расходу, TTS-модели появляются там же.
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
        # FISH_AUDIO_ENABLED=False (аудит моделей, 17.09.2026 — зеркало снято с
        # бесплатного каталога OpenRouter): пропускаем заведомо мёртвую первую
        # попытку и идём сразу на Gemini TTS. Ветка fish оставлена, не удалена —
        # см. комментарий у флага в lumen_router_config.py.
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

        # конвертируем в OGG/Opus через ffmpeg — send_voice в Telegram без этого
        # покажет длительность 0:00
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
        # Сырой текст ошибки пользователю не показываем — он может содержать
        # реальные ID моделей ("gemini-3.1-flash-tts-preview" и т.п.) или другие
        # служебные детали. Используем ту же классификацию, что и для чата.
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
    """Сбрасывает историю диалога в текущем чате. Намеренно скрыта: не добавлена
    в setMyCommands и не упомянута в /start — чтобы не загромождать меню команд
    (как и /logs). Доступ: в личных сообщениях — всем (это история только одного
    человека), в группах — только администратору/создателю группы или владельцу
    бота (сброс общей истории всей группы — не рядовое действие)."""
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

# ── Язык бота (/lang) ──
# Переключает язык СИСТЕМНЫХ сообщений бота в этом чате (/start, подсказки,
# ошибки, статусы — всё, что бот пишет сам). Ответы ИИ не трогает: модель
# отвечает на языке собеседника (см. RESPONSE LANGUAGE в system_prompt.py).
# Права — как у /reset (см. _is_privileged_in_chat): в личке меняет кто
# угодно, в группе — админ/создатель группы или владелец бота. Смотреть меню
# может любой; непривилегированный тап вежливо отклоняется. Кнопки без флагов
# (флаг ≠ язык) и без эмодзи, текущий язык помечен текстовой галочкой ✓.
# Языки в меню — по алфавиту кода (SUPPORTED_LANGS в lumen_lang.py).
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
    """Нажатие кнопки языка: проверка прав, сохранение, подтверждение на новом
    языке. callback_data — "lang:<код>", короткие коды влезают в лимит 64 байт
    с запасом."""
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
        # Найдено при код-ревью: результат команды видят ВСЕ участники чата, в
        # котором она вызвана, а не только владелец — файл логов содержит реальные
        # технические детали (ID моделей и т.п.), которые не должны светиться в
        # групповых чатах.
        await bot._tg_call(message.reply, bot._t(message.chat.id, "logs_group_only"))
        return

    # сбрасываем буфер логов на диск — НАЙДЕНО ПРИ АУДИТЕ ЛОГИРОВАНИЯ: после
    # перехода на QueueHandler/QueueListener у root-логгера остался только сам
    # QueueHandler (его flush() — no-op), реальные file_handler/console_handler
    # живут внутри _LOG_LISTENER, а не на root — цикл по logging.getLogger().handlers
    # ничего не флашил уже с момента этой миграции.
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
    """Глобальная статистика бота. Скрыта (не в setMyCommands, не в /start) и
    доступна только владельцу — тот же принцип доступа, что и у /logs, т.к.
    показывает данные по всем чатам, а не только текущему."""
    import bot
    is_owner = bot._is_owner(message.from_user.id if message.from_user else None)
    if not is_owner:
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_deny"))
        return

    if message.chat.type != ChatType.PRIVATE:
        # См. аналогичную проверку в /logs — статистика содержит реальные ID
        # моделей Gemini/OpenRouter, не должна светиться в групповых чатах.
        await bot._tg_call(message.reply, bot._t(message.chat.id, "stats_group_only"))
        return

    # Счётчики квоты — по датам America/Los_Angeles (полночь Google для RPD-лимитов),
    # см. _reset_quota_if_new_day. Проверяем прямо перед отрисовкой /stats, чтобы
    # владелец не увидел вчерашние числа, даже если часовой фоновый тик ещё не
    # успел сработать (реальный найденный баг — см. историю: used-счётчики копились
    # НАВСЕГДА через рестарты и не имели отношения к аптайму процесса).
    bot._reset_quota_if_new_day()

    total_chats = len(bot.chat_state)
    uptime_sec = int(time.monotonic() - bot._PROCESS_START_MONOTONIC)
    uptime_str = f"{uptime_sec // 3600}ч {(uptime_sec % 3600) // 60}м"

    gemini_quota = bot.GLOBAL_QUOTA.get("gemini", {})
    _stats_lang = bot._chat_lang(message.chat.id)
    _limitTag = _lang_t(_stats_lang, "stats_limit_used")
    _noData = _lang_t(_stats_lang, "stats_no_data")
    gemini_lines = [
        f"  • {mid}: {e.get('used', 0)}{_limitTag if e.get('exhausted_at') else ''}"
        for mid, e in sorted(gemini_quota.items(), key=lambda kv: -(kv[1].get("used") or 0))
    ]
    gemini_text = "\n".join(gemini_lines) or _noData

    # ИСПРАВЛЕНО (найдено при калибровке 25 июля 2026): раньше здесь была только
    # ОДНА суммарная цифра запросов OpenRouter плюс денежный $-баланс аккаунта —
    # для отладки роутинга и выявления "тупящих" моделей это бесполезно: не видно,
    # КАКАЯ именно модель отвечала и сколько раз. Теперь — та же разбивка по
    # моделям, что уже была у Gemini, отсортированная по расходу. $-баланс убран
    # целиком: все модели в списке — :free, платный баланс тут ни на что не влияет
    # и только замусоривал вывод.
    or_quota = bot.GLOBAL_QUOTA.get("openrouter", {})
    or_lines = [
        f"  • {mid}: {e.get('used', 0)}{_limitTag if e.get('exhausted_at') else ''}"
        for mid, e in sorted(or_quota.items(), key=lambda kv: -(kv[1].get("used") or 0))
    ]
    or_text = "\n".join(or_lines) or _noData

    # Видимость состояния "выключателя" Telegram-прокси прямо из Telegram, а не
    # только по логам контейнера — иначе деградацию прокси можно было заметить
    # только копаясь в логах HF Spaces (см. код-ревью, suggestion #1). Текст
    # теперь собирает сам _tg_proxy_breaker (см. _TelegramProxyCircuitBreaker) —
    # раньше эта команда лезла в четыре module-level globals напрямую.
    proxy_line = bot._tg_proxy_breaker.status_text()

    quota_day = bot.GLOBAL_QUOTA.get("quota_day") or "—"

    text = (
        f"<b>Статистика Lumen</b>\n\n"
        f"Активных чатов: {total_chats}\n"
        f"Аптайм процесса: {uptime_str}\n"
        f"Счётчики квоты за сутки: {quota_day} (America/Los_Angeles, сбрасываются автоматически)\n\n"
        f"<b>Gemini — запросов по моделям:</b>\n{gemini_text}\n\n"
        f"<b>OpenRouter — запросов по моделям:</b>\n{or_text}"
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
    """Обработчик нажатий кнопок-уточнений. Чужие кнопки (другой пользователь
    в группе) и протухшие/перезапущенные записи отклоняются вежливо, без
    обработки. Выбор дописывается к исходному запросу по шаблону сценария и
    уходит обычным путём через _handle_message_core — дальше роутер, стриминг
    и история работают как для текстового сообщения."""
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
    # Pop сразу (а не после проверок): повторный тап по тем же кнопкам не
    # должен порождать второй ответ. Чужая кнопка при этом "сгорает" — цена
    # приемлема: владелец переспросит текстом.
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
        # Клавиатуру снимаем пустой разметкой, иначе кнопки останутся висеть
        # под сообщением (повторный тап при этом всё равно упрётся в pop выше).
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
    # Флаг против зацикливания: дополненный текст всё ещё матчится детектором
    # ("посоветуй фильм (жанр: ...)"), без флага ушёл бы снова в кнопки.
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
