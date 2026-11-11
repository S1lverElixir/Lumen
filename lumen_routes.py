"""
lumen_routes.py — LLM-маршрутизация: Munito с фолбэком в Gemini, история,
конфиги, _run_route. Подменяемые в тестах имена вызываются через `bot.`,
чтобы monkeypatch видел их как раньше.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any

import aiohttp
from aiogram.enums import ChatType, ParseMode
from aiogram.types import Message
from google.genai import types

from lumen_errors import _is_empty_like
from lumen_formatting import _split_text_chunks, _strip_provider_artifacts
from lumen_lang import DEFAULT_LANG
from lumen_media import _is_gemini_supported_mime
from lumen_message_parse import _clean_history_entry, _history_user_text
import lumen_memory
from lumen_model_speed import (
    speed_key as _model_speed_key,
    record_response as _record_model_latency,
    reorder_route as _reorder_route_by_speed,
)
from lumen_router_config import (
    GEMINI_MODELS,
    GEMINI_DEFAULT_CHAIN,
    _looks_like_heavy_query,
)
from lumen_search import _SearchRequested, search_marker_state
from lumen_security import _scrub_identity_leak, _detect_garbled_mix, _is_garbled_echo
from lumen_status import thought_block

# Транскрипт длинного войса режем сверху — иначе маршрут ниже упрётся в лимиты моделей.
_TRANSCRIPT_MAX_CHARS = 4000
# Транскрибация не должна съедать весь бюджет маршрута: войсы короткие (до
# VOICE_TRANSCRIBE_MAX_BYTES), turbo-модель отвечает секундами — остаток оставляем модели.
_TRANSCRIBE_TIMEOUT_SEC = 15.0

log = logging.getLogger("bot")

class MunitoAPIError(RuntimeError):
    """Ошибка OpenAI-совместимого эндпоинта Munito (статус кладём рядом — его читает _error_status/_classify_model_error)."""
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

def _attempt_timeout(bot: Any, deadline: float | None) -> float:
    """Таймаут одной попытки: не больше ROUTE_MODEL_TIMEOUT_SEC и не больше
    остатка бюджета маршрута. Раньше каждая попытка ждала полные 22с, из-за
    чего маршрут держал лок чата до 40+22с вместо заявленных 40с (аудит 26.09.2026).
    Нижняя граница 0.5с: истёкший бюджет должен дать быстрый отказ, а не висеть."""
    if deadline is None:
        return bot.ROUTE_MODEL_TIMEOUT_SEC
    return max(0.5, min(bot.ROUTE_MODEL_TIMEOUT_SEC, deadline - time.monotonic()))

async def _munito_request(path: str, method: str = "GET", *, json_body: dict | None = None, deadline: float | None = None) -> Any:
    """Прямой запрос к Munito AI (OpenAI-совместимый API): бюджет одной попытки и
    вычистка ключа из текста ошибок. Whisper к Groq ходит отдельно и напрямую
    (_transcribe_audio: multipart-форма, а не JSON)."""
    import bot
    if not bot.MUNITO_API_KEY:
        raise bot.MunitoAPIError("MUNITO_API_KEY is not set")
    headers = {"Authorization": f"Bearer {bot.MUNITO_API_KEY}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    session = await bot._get_http_session()
    url = f"{bot.MUNITO_BASE_URL}/{path.lstrip('/')}"
    try:
        async with session.request(
            method.upper(), url, headers=headers, json=json_body,
            timeout=aiohttp.ClientTimeout(total=_attempt_timeout(bot, deadline), connect=10.0)
        ) as resp:
            if resp.status >= 400:
                payload = await resp.json(content_type=None)
                msg = payload.get("error", {}).get("message") or f"HTTP {resp.status}"
                raise bot.MunitoAPIError(msg, status_code=resp.status, payload=payload)
            return await resp.json(content_type=None)
    except bot.MunitoAPIError:
        raise
    except Exception as exc:
        exc_str = str(exc) or repr(exc) or exc.__class__.__name__
        if bot.MUNITO_API_KEY and len(bot.MUNITO_API_KEY) > 8:
            exc_str = exc_str.replace(bot.MUNITO_API_KEY, "<KEY>")
        raise bot.MunitoAPIError(f"Munito network error: {exc_str}") from exc


def _or_extract_text(data: Any) -> str:
    import bot
    if isinstance(data, str):
         return data.strip()
    if isinstance(data, dict):
         return bot._or_extract_text(data.get("content") or data.get("text") or "")
    if isinstance(data, list) and data:
         return "".join(bot._or_extract_text(i) for i in data)
    return ""

def _note_provenance(provenance: dict | None, provider: str, out: dict) -> None:
    """Кто фактически ответил и чем закончил ответ: без модели в provenance след
    нельзя привязать к модели (выборка бэкапа 05.11.2026 — OR/Groq писали None)."""
    if provenance is None:
        return
    provenance.update(provider=provider, model=out.get("model"), finish_reason=str(out.get("finish_reason") or ""))


def _log_llm_usage(*, chat_id: Any, provider: str, model_id: str,
                    prompt: Any = None, completion: Any = None, total: Any = None) -> None:
    """Одна строка usage на успешный ответ — только если API вернуло счётчики (иначе молчим)."""
    if prompt is None and completion is None and total is None:
        return
    log.info("[usage] chat=%s provider=%s model=%s prompt=%s completion=%s total=%s",
             chat_id, provider, model_id, prompt, completion, total)


def _usage_from_openai_response(resp: Any) -> tuple[Any, Any, Any]:
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return None, None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")


def _usage_from_gemini_response(resp: Any) -> tuple[Any, Any, Any]:
    meta = getattr(resp, "usage_metadata", None)
    if meta is None:
        return None, None, None
    return (getattr(meta, "prompt_token_count", None),
            getattr(meta, "candidates_token_count", None),
            getattr(meta, "total_token_count", None))


def _is_gemini_daily_quota(text: str) -> bool:
    """Суточная квота Gemini против минутного лимита запросов. Оба приходят как 429,
    но запирать модель до утра можно только за первый: минутный лимит у Google сыпется
    на любом всплеске (враждебное ревью 27.09.2026). Тексты ошибок — по документации
    Google (RESOURCE_EXHAUSTED с "per day"/"daily limit"). Отдельно ловим слитное
    PerDay из имён метрик вида GenerateRequestsPerDayPerProjectPerModel — иначе
    настоящая суточная квота выглядела бы минутным всплеском."""
    low = text.lower()
    return any(tok in low for tok in (
        "per day", "per_day", "perday", "daily limit", "daily quota", "quota exceeded for the day",
    ))

def _mark_model_unavailable(provider: str, model_id: str, exc: Exception) -> bool:
    """Отказ "модель недоступна" (503/404/invalid model) → короткая остывка, как на 429.

    Прод 05.10.2026: пять моделей линейки Gemini 3.x подряд отдавали 503, и каждая
    повторялась на каждом обращении — до 22 секунд и пяти попыток впустую на запрос.
    Официальный список моделей и дашборд AI Studio показывали, что модели живы и
    квота есть, то есть это был временный отказ, а не снятие с free. Поэтому НЕ
    выкидываем модель из цепочек, а пропускаем её на QUOTA_RATE_LIMIT_COOLDOWN_SEC:
    остывка сама истекает, успешный ответ и так снимает метку.
    """
    import bot
    try:
        txt = bot._error_text(exc).strip() or exc.__class__.__name__
        if bot._classify_model_error(bot._error_status(exc, txt), txt) != "unavailable":
            return False
        bot._mark_rate_limited(provider, model_id)
        return True
    except Exception:
        return False


def _raise_if_search_requested(answer: str, provider: str, model_id: str) -> None:
    """Ответ — ровно маркер [[SEARCH]]: данных не хватило, модель просит поиск.
    Останавливает маршрут ДО записи в память чата (маркер в истории подсказывал
    бы себе в следующих ходах); квоту вызова пишем — он реально состоялся
    (замер 11.11.2026)."""
    if search_marker_state(answer) != "requested":
        return
    import bot
    log.info('[router] %s/%s asked for a web search instead of answering', provider, model_id)
    bot._record_quota_usage(provider, model_id)
    raise _SearchRequested()


async def _chat_completion_chain(
    messages: list[dict], trial_models: list[str], primary_model_id: str, *,
    request_fn, provider: str, deadline: float | None = None,
    log_label: str | None = None, chat_id: Any = None, user_text: str | None = None,
    out: dict | None = None, extra_payload: dict | None = None,
    record_latency: bool = True,
) -> tuple[str, str]:
    """Общий fallback-цикл по цепочке OpenAI-совместимых моделей (сейчас — Munito).

    extra_payload — провайдерские поля поверх стандартного тела (у Munito это
    reasoning_effort): копия payload в одной ветке не должна расходиться с
    остальными (аудит 26.09.2026).

    Ровно одна попытка на модель — ретраи одной модели при массовой нестабильности
    замедляли весь маршрут. Раньше этот цикл жил в трёх копиях, и копии молча
    расходились с оригиналом (аудит 26.09.2026). Возвращает (answer, used_model);
    иначе последнее исключение или RouteBudgetExceededError.

    log_label — тег для логов/источников скраба: у провайдеров он исторически
    короткий ("or"), поэтому задаётся извне, а не выводится из provider-строки."""
    import bot
    label = log_label or provider
    last_exc: Exception | None = None
    tried: list[str] = []
    for model_trial in trial_models:
        if deadline is not None and time.monotonic() > deadline:
            log.warning('[%s] Route time budget exhausted before model %s. Tried: %s', label, model_trial, ", ".join(tried) or "none")
            raise bot.RouteBudgetExceededError(tried)
        tried.append(model_trial)
        bot._record_stats_event("model_attempts")
        messages[0]["content"] = bot.get_system_prompt(model_trial)
        attempt_start = time.monotonic()
        try:
            payload = {"model": model_trial, "messages": messages, "stream": False, **(extra_payload or {})}
            resp = await request_fn(payload, deadline)
            choices = resp.get("choices") or []
            answer = ""
            if choices:
                answer = bot._or_extract_text(choices[0].get("message") or "")
            answer = _strip_provider_artifacts(answer)
            if _is_empty_like(answer):
                # Пустой ответ — повод попробовать следующую модель (прод 17.09.2026: юзер дважды увидел "Empty response").
                bot._record_model_outcome(provider, model_trial, bad=True)
                raise RuntimeError(f"Model {model_trial} returned an empty response")

            answer = _scrub_identity_leak(answer, source=f"{provider}_chat_completion:{model_trial}")
            # Каша-каша тоже считается плохим (доставляется как есть, как раньше),
            # успех сбрасывает счётчик карантина. Повтор смешанных токенов за
            # пользователем — эхо, а не каша модели, в счётчик не идёт.
            mush = _detect_garbled_mix(answer) and not _is_garbled_echo(answer, user_text)
            bot._record_model_outcome(provider, model_trial, bad=mush)
            if out is not None:
                first = choices[0] if choices and isinstance(choices[0], dict) else {}
                out["model"] = model_trial
                out["finish_reason"] = str(first.get("finish_reason") or "")
                if out["finish_reason"] == "length":
                    # Потолок вывода провайдера: ответ обрезан (прод 05.11.2026 —
                    # обрезки не было видно ни в логе, ни в следе).
                    log.warning('[%s] Model %s hit the output cap, answer truncated', label, model_trial)
            log.info('[%s] Successful response from model %s (primary=%s, models tried: %d)', label, model_trial, primary_model_id, len(tried))
            _pt, _ct, _tt = _usage_from_openai_response(resp)
            _log_llm_usage(chat_id=chat_id, provider=provider, model_id=model_trial,
                           prompt=_pt, completion=_ct, total=_tt)
            if record_latency:
                # Служебная проба (selftest) не кормит общий EMA: четырьмя токенами
                # сдвинулся бы порядок моделей для всех чатов (ревью 08.11.2026).
                _record_model_latency(_model_speed_key(provider, model_trial), total_sec=time.monotonic() - attempt_start)
            return answer, model_trial
        except bot.RouteBudgetExceededError:
            raise
        except Exception as exc:
            last_exc = exc
            bot._record_stats_event("model_failures")
            _mark_model_unavailable(provider, model_trial, exc)
            # 429 — в остывку, как стриминг выше: у Munito лимиты оконные
            # (200 запросов за 30-секундное окно), суточной квоты, которую стоило
            # бы держать закрытой до утра, у него нет.
            try:
                _txt = bot._error_text(exc).strip() or exc.__class__.__name__
                if bot._classify_model_error(bot._error_status(exc, _txt), _txt) == "rate_limit":
                    bot._mark_rate_limited(provider, model_trial)
            except Exception:
                pass
            log.warning("[%s] Model %s failed: %s. Switching to next candidate...", label, model_trial, str(last_exc) or last_exc.__class__.__name__)

    if last_exc:
        raise last_exc
    raise RuntimeError("No candidate model returned an answer.")


async def ask_munito_text(chat_id: int, user_text: str, model_chain: list[str], *, deadline: float | None = None, history_text: str | None = None, provenance: dict | None = None, history_meta: dict[str, Any] | None = None) -> str:
    """Текст через Munito AI: одна попытка на модель, скраб утечек, расход в квоту "munito".
    Память — единый лог до 200 сообщений; прошлые фото прикладываются отдельной репликой."""
    import bot
    from lumen_router_config import _MUNITO_ORDER, _munito_payload_extras
    state = bot.get_state(chat_id)
    trial_models = list(dict.fromkeys(model_chain)) or [_MUNITO_ORDER[0]]
    primary_model_id = trial_models[0]
    messages = await bot._build_openai_turn_messages(
        chat_id, user_text, primary_model_id, history_meta=history_meta,
    )

    async def _request(payload: dict, dl: float | None):
        return await bot._munito_request("chat/completions", "POST", json_body=payload, deadline=dl)

    _out: dict = {}
    answer, model_trial = await _chat_completion_chain(
        messages, trial_models, primary_model_id,
        request_fn=_request, provider="munito", deadline=deadline, chat_id=chat_id, user_text=user_text,
        out=_out, extra_payload=_munito_payload_extras(),
    )
    _note_provenance(provenance, "munito", _out)
    _raise_if_search_requested(answer, "munito", model_trial)

    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    lumen_memory.append_assistant(state, answer)
    await bot._trim_history(state["history"], state)
    bot._record_quota_usage("munito", model_trial)
    return answer

async def ask_munito_multimodal(
    chat_id: int, user_text: str, media_tuple: tuple[bytes, str] | list[tuple[bytes, str]], media_filename: str,
    model_chain: list[str], *, deadline: float | None = None, history_text: str | None = None,
    provenance: dict | None = None, history_meta: dict[str, Any] | None = None,
) -> str:
    """Картинки через Munito AI (vision проверен 05.11.2026 живым вызовом): все картинки
    одним сообщением блоками image_url — по одному на фото, иначе 2-9 фото альбома
    молча терялись. Видео и аудио сюда не доходят: их забирает ветка Gemini,
    OpenAI-формат не-изображений не принимает. Остальное — как в ask_munito_text."""
    import bot
    from lumen_router_config import _MUNITO_ORDER, _munito_payload_extras
    state = bot.get_state(chat_id)
    if isinstance(media_tuple, tuple) and media_tuple and isinstance(media_tuple[0], bytes):
        media_items = [media_tuple]
    else:
        media_items = list(media_tuple) if media_tuple else []
    parts: list[dict] = []
    for raw_bytes, mime in media_items[:10]:
        if not (mime or "").startswith("image/"):
            continue
        b64 = base64.b64encode(raw_bytes).decode("utf-8")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if not parts:
        raise RuntimeError("No images for Munito multimodal (video goes to Gemini)")

    trial_models = list(dict.fromkeys(model_chain)) or [_MUNITO_ORDER[0]]
    primary_model_id = trial_models[0]
    messages = await bot._build_openai_turn_messages(
        chat_id, user_text, primary_model_id,
        history_meta=history_meta, current_parts=parts,
    )

    async def _request(payload: dict, dl: float | None):
        return await bot._munito_request("chat/completions", "POST", json_body=payload, deadline=dl)

    _out: dict = {}
    answer, model_trial = await _chat_completion_chain(
        messages, trial_models, primary_model_id,
        request_fn=_request, provider="munito", deadline=deadline, chat_id=chat_id, user_text=user_text,
        out=_out, extra_payload=_munito_payload_extras(),
    )
    _note_provenance(provenance, "munito", _out)
    _raise_if_search_requested(answer, "munito", model_trial)

    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    lumen_memory.append_assistant(state, answer)
    await bot._trim_history(state["history"], state)
    bot._record_quota_usage("munito", model_trial)
    return answer

_SELFTEST_PING_TEXT = "Reply with exactly: ok"


def _selftest_short_error(exc: BaseException) -> str:
    """Короткий однострочник для строки /selftest (только личка — ID моделей тут допустимы)."""
    text = f"{exc.__class__.__name__}: {exc}".strip()
    # ID организации провайдера — внутрянка даже для владельца: затираем, как токены.
    text = re.sub(r"org_[A-Za-z0-9]+", "<ORG>", text)
    return " ".join(text.split())[:120] or exc.__class__.__name__

async def selftest_llm_head(provider: str, *, chat_id: Any = None) -> tuple[bool, str, float]:
    """Одна крошечная проба в голову маршрута (munito/gemini) для /selftest.

    Успех честно пишется в квоту тем же _record_quota_usage, что у настоящих
    ответов; неуспех квоту не трогает (как в ask_*). Историю чата не трогаем —
    проба не должна оседать в контексте владельца. Никогда не кидает: неуспех
    возвращается кортежем (False, причина, секунды)."""
    import bot
    from lumen_router_config import _MUNITO_ORDER, _munito_payload_extras
    start = time.monotonic()
    try:
        # Имена запросов читаем через bot. в момент вызова — так подмена тестами продолжает работать.
        if provider == "munito":
            model = _MUNITO_ORDER[0]
            messages = [{"role": "system", "content": ""}, {"role": "user", "content": _SELFTEST_PING_TEXT}]

            async def _probe_request(payload: dict, dl: float | None):
                return await bot._munito_request("chat/completions", "POST", json_body=payload, deadline=dl)

            _ans, used = await _chat_completion_chain(
                messages, [model], model, request_fn=_probe_request,
                provider="munito", deadline=start + bot.ROUTE_MODEL_TIMEOUT_SEC,
                chat_id=chat_id, user_text=_SELFTEST_PING_TEXT,
                extra_payload=_munito_payload_extras(),
                record_latency=False,
            )
            # Пробный пинг — служебный вызов: в квоту и в снятые отметки об исчерпании
            # он не пишется, иначе одна проверка возвращает в строй весь провайдер.
            # Латентность тоже не пишем: флаг record_latency=False выше.
            bot._record_quota_usage("munito", used, service=True)
            return True, "", round(time.monotonic() - start, 2)
        if provider == "gemini":
            # Квота самая дефицитная — проба минимальная: голый текст без системного
            # промпта и истории (в отличие от ask_gemini, чистит только живость).
            model = GEMINI_DEFAULT_CHAIN[0]
            deadline = start + bot.ROUTE_MODEL_TIMEOUT_SEC
            contents = [types.Content(role="user", parts=[types.Part.from_text(text=_SELFTEST_PING_TEXT)])]
            call_contents, gconfig = bot._build_gemini_call_config(model, contents)
            resp = await asyncio.wait_for(
                bot.client.aio.models.generate_content(model=model, contents=call_contents, config=gconfig),
                timeout=max(1.0, deadline - time.monotonic()),
            )
            ans = await bot._extract_gemini_answer_text(
                resp, model_id=model, call_contents=call_contents, gconfig=gconfig, deadline=deadline)
            if _is_empty_like(_strip_provider_artifacts(ans)):
                bot._record_model_outcome("gemini", model, bad=True)
                raise RuntimeError(f"Model {model} returned an empty response")
            # Та же честность, что у остальных проб: успех сбрасывает счётчик.
            # Пинг без смешанных токенов — эху тут браться негде, проверка для единообразия.
            bot._record_model_outcome("gemini", model,
                                      bad=_detect_garbled_mix(ans) and not _is_garbled_echo(ans, _SELFTEST_PING_TEXT))
            _gpt, _gct, _gtt = _usage_from_gemini_response(resp)
            _log_llm_usage(chat_id=chat_id, provider="gemini", model_id=model,
                           prompt=_gpt, completion=_gct, total=_gtt)
            # Пинг в несколько токенов не идёт в общий EMA латентности: он сдвигал бы
            # порядок моделей и первый таймаут для всех чатов (аудит run-2).
            bot._record_quota_usage("gemini", model, service=True)
            return True, "", round(time.monotonic() - start, 2)
        raise ValueError(f"unknown selftest provider: {provider}")
    except Exception as exc:
        return False, _selftest_short_error(exc), round(time.monotonic() - start, 2)

async def _transcribe_audio(audio_bytes: bytes, mime: str, chat_id: int, deadline: float | None = None) -> str | None:
    """Голос/аудио в текст через Groq Whisper (дешевле Gemini-квоты на порядок). None при
    любой неудаче — вызывающий код молча идёт прежним путём (аудио напрямую в Gemini).
    Таймаут — остаток бюджета маршрута, а не полный лимит: иначе войс держал лок сверх бюджета."""
    import bot
    from lumen_media import _mime_suffix
    if not bot.GROQ_API_KEY or not audio_bytes:
        return None
    if len(audio_bytes) > bot.VOICE_TRANSCRIBE_MAX_BYTES:
        log.warning("[groq] Voice message too big for transcription (%d bytes), leaving to Gemini.", len(audio_bytes))
        return None
    lang = bot._chat_lang(chat_id)
    suffix = _mime_suffix(mime) or ".ogg"
    filename = "voice" + suffix
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type=mime or "audio/ogg")
    form.add_field("model", "whisper-large-v3-turbo")
    if isinstance(lang, str) and len(lang) == 2:
        form.add_field("language", lang)
    try:
        session = await bot._get_http_session()
        async with session.post(
            f"{bot.GROQ_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {bot.GROQ_API_KEY}"},
            data=form,
            timeout=aiohttp.ClientTimeout(total=min(_attempt_timeout(bot, deadline), _TRANSCRIBE_TIMEOUT_SEC), connect=10.0),
        ) as resp:
            if resp.status >= 400:
                body = await resp.read()
                log.warning("[groq] Whisper HTTP %s: %r", resp.status, body[:200])
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:
        exc_str = str(exc) or repr(exc) or exc.__class__.__name__
        if bot.GROQ_API_KEY:
            exc_str = exc_str.replace(bot.GROQ_API_KEY, "<KEY>")
        log.warning("[groq] Whisper request failed: %s", exc_str)
        return None
    text = (data.get("text") or "").strip() if isinstance(data, dict) else ""
    if not text:
        return None
    # Длинный войс без обрезки упёрся бы в лимиты моделей ниже по маршруту.
    if len(text) > _TRANSCRIPT_MAX_CHARS:
        text = text[:_TRANSCRIPT_MAX_CHARS].rstrip() + "…"
    bot._record_quota_usage("groq", "whisper-large-v3-turbo", service=True)
    return text

async def _gemini_history_contents(history: list[dict]) -> list[types.Content]:
    import bot
    contents: list[types.Content] = []
    for item in history:
        entry = _clean_history_entry(item) if isinstance(item, dict) else None
        if entry is None:
            continue
        r = "model" if str(entry.get("role")).lower() in {"assistant", "model"} else "user"
        cont = entry.get("content") or ""
        # Старые записи могли хранить не строку, а структуру провайдера: разбираем.
        txt = lumen_memory.format_entry(entry) if isinstance(cont, str) else bot._or_extract_text(cont)
        if txt:
             contents.append(types.Content(role=r, parts=[types.Part.from_text(text=txt)]))
    return contents

def _build_gemini_call_config(
    model_id: str, contents: list[types.Content], *, memory_summary: str = "",
) -> tuple[list[types.Content], "types.GenerateContentConfig | None"]:
    """Строит (call_contents, gconfig) для ОДНОГО вызова Gemini: system_instruction и
    grounding по дашборду. Одна функция на ask_gemini и стриминг — чтобы не расходились."""
    import bot
    conf = GEMINI_MODELS.get(model_id, {})
    kwargs: dict[str, Any] = {}
    kwargs["system_instruction"] = lumen_memory.system_prompt_with_memory(
        bot.get_system_prompt(model_id), {"memory_summary": memory_summary},
    )
    # Инструменты — только у кого есть free-квота по дашборду (у 3.5/preview map 0/0).
    tools_list = []
    if conf.get("search_grounding", True):
        try:
            tools_list.append(types.Tool(google_search=types.GoogleSearch()))
        except Exception:
            pass  # в этом SDK нет google_search
    if conf.get("map_grounding", False):
        try:
            tools_list.append(types.Tool(google_maps=types.GoogleMaps()))
        except Exception:
            pass  # в этом SDK нет google_maps
    if conf.get("url_context", True):
        try:
            tools_list.append(types.Tool(url_context=types.UrlContext()))
        except Exception:
            pass  # в этом SDK нет url_context
    if tools_list:
        kwargs["tools"] = tools_list
    # Эффорт мышления — низкий только для не-heavy: калибровка 18.08.2026 поймала 3.7-flash на 17 таймаутах из 18. Heavy не трогаем (дефолт модели балансирует лучше). 3.x — thinking_level, 2.5 — thinking_budget.
    last_text = next((p.text for p in reversed(contents[-1].parts) if getattr(p, "text", None)), "") if contents else ""
    if not _looks_like_heavy_query(last_text):
        if model_id.startswith("gemini-3"):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        elif model_id.startswith("gemini-2.5"):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    gconfig = types.GenerateContentConfig(**kwargs) if kwargs else None

    return contents, gconfig

async def _extract_gemini_answer_text(resp: Any, *, model_id: str, call_contents: list, gconfig, deadline: float | None = None) -> str:
    """Текст ответа Gemini: сначала resp.text, иначе разбор candidates/parts вручную (включая повтор БЕЗ инструментов при MALFORMED_FUNCTION_CALL)."""
    import bot
    ans = ""
    tool_calls: list[str] = []
    try:
        ans = getattr(resp, "text", "") or ""
    except Exception:
        ans = ""
    if not ans:
        reasons = []
        for cand in (getattr(resp, "candidates", []) or []):
            reasons.append(str(getattr(cand, "finish_reason", "UNKNOWN")))
            content = getattr(cand, "content", None)
            if content:
                parts = getattr(content, "parts", []) or []
                for part in parts:
                    part_text = getattr(part, "text", "") or ""
                    if part_text:
                         ans += part_text
                    fn_call = getattr(part, "function_call", None)
                    if fn_call:
                         fn_name = getattr(fn_call, "name", "tool")
                         fn_args = getattr(fn_call, "args", None) or getattr(fn_call, "arguments", None)
                         try:
                             fn_args_txt = json.dumps(bot._json_prune_defaults(fn_args), ensure_ascii=False) if fn_args is not None else "{}"
                         except Exception:
                             fn_args_txt = str(fn_args)
                         tool_calls.append(f"{fn_name}({fn_args_txt})")
                    fn_resp = getattr(part, "function_response", None)
                    if fn_resp and not part_text:
                         try:
                             tool_calls.append(f"response:{json.dumps(bot._json_prune_defaults(getattr(fn_resp, 'response', None)), ensure_ascii=False)}")
                         except Exception:
                             tool_calls.append("response")
        if not ans and (tool_calls or reasons):
            if tool_calls or any("MALFORMED_FUNCTION_CALL" in r for r in reasons):
                # Ответ без текста: битый вызов или голый tool_call — пользователю
                # JSON инструментов не показываем (прод 07.10.2026), повторяем без них.
                if tool_calls:
                    log.warning("[gemini] Model %s returned only tool calls without text, retrying without tools.", model_id)
                try:
                    retry_gconfig = gconfig.model_copy(update={"tools": None}) if gconfig is not None else None
                    # Async-клиент, а не to_thread: wait_for реально отменяет зависший
                    # запрос. Повтор тоже в бюджет маршрута, иначе держал бы лок
                    # поверх истраченного.
                    retry_timeout = bot.TELEGRAM_AI_TIMEOUT
                    if deadline is not None:
                        retry_timeout = min(retry_timeout, max(0.0, deadline - time.monotonic()))
                    retry_resp = None
                    if retry_timeout > 0:
                        retry_resp = await asyncio.wait_for(
                            bot.client.aio.models.generate_content(model=model_id, contents=call_contents, config=retry_gconfig),
                            timeout=retry_timeout,
                        )
                    retry_text = getattr(retry_resp, "text", "") or "" if retry_resp is not None else ""
                    if retry_text.strip():
                        ans = retry_text
                        log.warning("[gemini] Model %s had MALFORMED_FUNCTION_CALL, retried without tools successfully.", model_id)
                    elif retry_resp is not None:
                        # Повтор прошёл, но вернул пусто без исчерпания бюджета:
                        # это повод на следующую модель.
                        log.warning("[gemini] Retry after MALFORMED_FUNCTION_CALL returned empty text, trying the next model.")
                    elif deadline is not None and deadline - time.monotonic() <= 0:
                        # Повтор не успел уложиться в бюджет маршрута: отдаём пусто,
                        # ask_gemini перейдёт к следующей модели.
                        log.warning("[gemini] Retry after MALFORMED_FUNCTION_CALL skipped: route budget is exhausted, trying the next model.")
                except Exception as retry_exc:
                    log.warning("[gemini] Retry without tools after MALFORMED_FUNCTION_CALL also failed: %s", retry_exc)
                    # Повтор упал — отдаём пусто: маршрут уйдёт к следующей модели,
                    # а не остановится на первой же битой.
            # Блокировка (SAFETY/RECITATION) — не ответ: пустая строка двигает цепочку
            # дальше, а локализованный отказ пользователь увидит, только если не ответил
            # никто (аудит 27.10.2026: русская заглушка останавливала маршрут).
    # Пустая строка без блокировки — не "Empty response": ask_gemini пробует следующую модель.
    return ans.strip()

def _user_mime_label(mime: str | None) -> str:
    """Подпись типа вложения для текста ошибки: клиентский mime не должен стать
    разметкой (markdown-ссылка) в сообщении бота (аудит run-2, low). Параметры
    типа (charset) разрешены, скобки и угловые скобки — нет."""
    if mime and mime.isascii() and all(c.isalnum() or c in "._+-/:;= " for c in mime):
        return mime
    return "неизвестный тип"


async def ask_gemini(
    chat_id: int, user_text: str, media: list[tuple[bytes, str]] | None = None,
    youtube_url: str | None = None, model_chain: list[str] | None = None,
    deadline: float | None = None, provenance: dict | None = None,
    history_text: str | None = None, history_meta: dict[str, Any] | None = None,
) -> str:
    import bot
    state = bot.get_state(chat_id)
    chain = list(model_chain) if model_chain else list(GEMINI_DEFAULT_CHAIN)
    if deadline is None:
        deadline = time.monotonic() + bot.ROUTE_TOTAL_BUDGET_SEC

    # Медиа/YouTube-части в тот же Content, что и текст (сборка — только в _build_gemini_turn_contents).
    extra_parts: list[types.Part] = []
    if media:
        for b, mime in media:
             if _is_gemini_supported_mime(mime):
                 extra_parts.append(types.Part.from_bytes(data=b, mime_type=mime))
             else:
                 raise bot.UserFacingInputError(f"Тип вложения '{_user_mime_label(mime)}' не поддерживается для анализа. Отправьте картинку, аудиозапись, видео, PDF или текстовый документ.")
    if youtube_url:
          # YouTube — file_uri без скачивания. mime_type явно video/*: SDK не угадывает его для shorts-ссылок ("Failed to determine mime type").
         extra_parts.append(types.Part.from_uri(file_uri=youtube_url, mime_type="video/*"))
    contents = await bot._build_gemini_turn_contents(
        chat_id, user_text, extra_parts=extra_parts or None, history_meta=history_meta,
    )

    # to_thread — не вешаем event loop. Ровно одна попытка на модель (ретраи одной давали ответы по 2+ минуты); порядок — от роутера, сверху режет общий deadline.
    resp = None
    curr_model_id = chain[0]
    tried_models: set[str] = set()
    quota_exhausted_models: list[str] = []

    loop_guard = 0
    # Запас len+4 сверх длины цепочки: страховка от зацикливания при будущих
    # ветках фолбэка (сейчас все переходы идут только внутри chain).
    max_loop_guard = len(chain) + 4

    while True:
        loop_guard += 1
        if loop_guard > max_loop_guard:
            raise RuntimeError("Exceeded the allowed number of Gemini API attempts.")
        if time.monotonic() > deadline:
            log.warning('[gemini] Route time budget exhausted. Tried: %s', ", ".join(sorted(tried_models)) or "none")
            raise bot.RouteBudgetExceededError(sorted(tried_models))
        tried_models.add(curr_model_id)
        bot._record_stats_event("model_attempts")
        call_contents, gconfig = bot._build_gemini_call_config(
            curr_model_id, contents, memory_summary=state.get("memory_summary") or "",
        )

        attempt_start = time.monotonic()
        try:
            # Async-клиент, а не to_thread: wait_for тогда реально отменяет зависший
            # запрос (поток to_thread отменить нельзя — он бы жил дальше в фоне).
            resp = await asyncio.wait_for(
                bot.client.aio.models.generate_content(model=curr_model_id, contents=call_contents, config=gconfig),
                timeout=_attempt_timeout(bot, deadline),
            )
            ans = await bot._extract_gemini_answer_text(resp, model_id=curr_model_id, call_contents=call_contents, gconfig=gconfig, deadline=deadline)
            ans = _strip_provider_artifacts(ans)
            if _is_empty_like(ans):
                # Пустой ответ — пробуем следующую модель (прод 17.09.2026: юзер увидел буквальное "Empty response").
                bot._record_model_outcome("gemini", curr_model_id, bad=True)
                raise RuntimeError(f"Model {curr_model_id} returned an empty response")
            break
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            next_model = bot._next_fallback_model(tried_models, chain)
            if next_model:
                bot._record_stats_event("model_failures")
                log.warning("[gemini] Model %s timed out (%.0fs). Switching to %s", curr_model_id, bot.ROUTE_MODEL_TIMEOUT_SEC, next_model)
                curr_model_id = next_model
                continue
            bot._record_stats_event("model_failures")
            log.warning("[gemini] Model %s timed out and no fallback models remain in route.", curr_model_id)
            raise
        except Exception as exc:
            # Классификация — общими хелперами (_error_status/_classify_model_error), а не третьей ad-hoc копией.
            txt = bot._error_text(exc).strip() or exc.__class__.__name__
            status_code = bot._error_status(exc, txt)
            kind = bot._classify_model_error(status_code, txt)
            exc_class = exc.__class__.__name__

            if kind == "rate_limit":
                # 429 у Google — обычно минутный лимит запросов, а не суточная квота.
                # Раньше любой 429 ставил суточную метку, и один всплеск убирал модель из
                # роута до полуночи (враждебное ревью 27.09.2026). Суточную квоту Google
                # опознаём по тексту ошибки — тогда метка держится до утра.
                if bot._is_gemini_daily_quota(txt):
                    bot._mark_quota_exhausted("gemini", curr_model_id)
                    quota_exhausted_models.append(curr_model_id)
                else:
                    bot._mark_rate_limited("gemini", curr_model_id)
                next_model = bot._next_fallback_model(tried_models, chain)
                if next_model:
                    bot._record_stats_event("model_failures")
                    log.warning("[gemini] Model %s quota exhausted (429). Switching to %s", curr_model_id, next_model)
                    curr_model_id = next_model
                    continue
                if quota_exhausted_models:
                    log.warning("[gemini] All Gemini models in route exhausted their quota (429): %s", ", ".join(quota_exhausted_models))
                    raise bot.GeminiAllModelsExhaustedError(quota_exhausted_models) from exc
                # Одни минутные всплески: суточного исчерпания не было — отдаём
                # последний 429 как есть, а не пустой "исчерпано всё".
                log.warning("[gemini] All Gemini models in route rate-limited (429), no daily exhaustion.")
                raise

            # Остальные исходы — одна попытка и сразу следующая модель, без ретраев.
            _mark_model_unavailable("gemini", curr_model_id, exc)
            next_model = bot._next_fallback_model(tried_models, chain)
            if next_model:
                bot._record_stats_event("model_failures")
                reason = f"{kind}/{status_code}" if status_code else f"{kind}/{exc_class}"
                log.warning("[gemini] Model %s failed (%s). Switching to %s", curr_model_id, reason, next_model)
                curr_model_id = next_model
                continue
            bot._record_stats_event("model_failures")
            log.warning("[gemini] Model %s failed (%s) and no fallback models remain in route.", curr_model_id, exc_class)
            raise

    if resp is None:
        raise RuntimeError("No response received from Gemini after retries.")
    log.info('[gemini] Successful response from model %s (models tried: %d)', curr_model_id, len(tried_models))
    _gpt, _gct, _gtt = _usage_from_gemini_response(resp)
    _log_llm_usage(chat_id=chat_id, provider="gemini", model_id=curr_model_id,
                   prompt=_gpt, completion=_gct, total=_gtt)
    _record_model_latency(_model_speed_key("gemini", curr_model_id), total_sec=time.monotonic() - attempt_start)

    ans = _scrub_identity_leak(ans, source=f"ask_gemini:{curr_model_id}")
    bot._record_model_outcome("gemini", curr_model_id,
                              bad=_detect_garbled_mix(ans) and not _is_garbled_echo(ans, user_text))
    _raise_if_search_requested(ans, "gemini", curr_model_id)

    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    lumen_memory.append_assistant(state, ans)
    # Обрезка со сводкой старого (см. _trim_history), а не молчаливый срез.
    await bot._trim_history(state["history"], state)
    bot._record_quota_usage("gemini", curr_model_id)
    if provenance is not None:
        provenance.update(provider="gemini", model=curr_model_id)
    return ans

async def _build_gemini_turn_contents(
    chat_id: int, user_text: str, extra_parts: list[types.Part] | None = None,
    *, history_meta: dict[str, Any] | None = None,
) -> list[types.Content]:
    """Contents на один ход: память с авторами, прошлые фото и текущий вопрос."""
    import bot
    state = bot.get_state(chat_id)
    contents = await bot._gemini_history_contents(state.get("history") or [])
    meta = history_meta if isinstance(history_meta, dict) else {}
    skip = set(meta.get("skip_file_ids") or [])
    if meta.get("attach_past", True):
        past = lumen_memory.recent_media(
            state, "photo",
            window=lumen_memory.PHOTO_CONTEXT_WINDOW,
            max_items=lumen_memory.PHOTO_CONTEXT_MAX,
            skip_file_ids=skip,
        )
        if past:
            past_parts: list[types.Part] = [types.Part.from_text(text=(
                "Earlier in this chat, these photos were sent. Use them only if the "
                "current message refers to them; do not describe them otherwise."
            ))]
            for entry, ref in past:
                past_parts.append(types.Part.from_text(
                    text=f"Photo — {lumen_memory.format_entry(entry)}:",
                ))
                if not _is_gemini_supported_mime(ref["mime"]):
                    continue
                try:
                    fetched = await bot._fetch_media(ref["file_id"], ref["mime"])
                except Exception:
                    fetched = None
                if not fetched:
                    continue
                data, mime = fetched
                past_parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            if len(past_parts) > 1:
                contents.append(types.Content(role="user", parts=past_parts))
    parts = [types.Part.from_text(text=user_text)]
    if extra_parts:
        parts.extend(extra_parts)
    contents.append(types.Content(role="user", parts=parts))
    return contents

async def _build_openai_turn_messages(
    chat_id: int, user_text: str, model_id: str, *,
    history_meta: dict[str, Any] | None = None,
    current_parts: list[dict] | None = None,
) -> list[dict]:
    """Сообщения для OpenAI-совместимого chat/completions: память, авторы, прошлые фото.

    Прошлые фото прикладываются отдельной user-репликой, а не к текущему вопросу:
    модель не должна путать их с только что присланным вложением."""
    import bot
    state = bot.get_state(chat_id)
    meta = history_meta if isinstance(history_meta, dict) else {}
    system_text = lumen_memory.system_prompt_with_memory(bot.get_system_prompt(model_id), state)
    messages: list[dict] = [{"role": "system", "content": system_text}]
    history = state.get("history") or []
    skip = set(meta.get("skip_file_ids") or [])
    if meta.get("attach_past", True):
        past = lumen_memory.recent_media(
            state, "photo",
            window=lumen_memory.PHOTO_CONTEXT_WINDOW,
            max_items=lumen_memory.PHOTO_CONTEXT_MAX,
            skip_file_ids=skip,
        )
        if past:
            parts: list[dict] = [{"type": "text", "text": (
                "Earlier in this chat, these photos were sent. Use them only if the "
                "current message refers to them; do not describe them otherwise."
            )}]
            for entry, ref in past:
                parts.append({"type": "text", "text": f"Photo — {lumen_memory.format_entry(entry)}:"})
                try:
                    fetched = await bot._fetch_media(ref["file_id"], ref["mime"])
                except Exception:
                    fetched = None
                if not fetched:
                    continue
                data, mime = fetched
                b64 = base64.b64encode(data).decode("utf-8")
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            if len(parts) > 1:
                messages.append({"role": "user", "content": parts})
    # История: авторы и время; старые служебные блоки вычищаем на лету.
    for item in history:
        entry = _clean_history_entry(item) if isinstance(item, dict) else None
        if entry is None:
            continue
        cont = entry.get("content")
        # Старые записи могли хранить структуру провайдера: разбираем, как Gemini-ветка.
        text = lumen_memory.format_entry(entry) if isinstance(cont, str) else bot._or_extract_text(cont)
        if not text:
            continue
        role = "assistant" if entry.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": text})
    if current_parts:
        messages.append({"role": "user", "content": [{"type": "text", "text": user_text}, *current_parts]})
    else:
        messages.append({"role": "user", "content": user_text})
    return messages


def _route_error_reply_text(exc: Exception, head_model: str, *, youtube_url_to_analyze: str | None, lang: str = DEFAULT_LANG) -> str:
    """Текст ответа на исключение из _run_route — чистая функция (owner-алерт остаётся в вызывающем коде: там сетевой вызов)."""
    import bot
    if youtube_url_to_analyze:
        return bot._lang_t(lang, "err_youtube_fail")
    if isinstance(exc, bot.GeminiAllModelsExhaustedError):
        return bot._gemini_error_msg(exc, head_model, lang)
    if isinstance(exc, bot.RouteBudgetExceededError):
        return bot._lang_t(lang, "err_budget")
    if isinstance(exc, bot.MunitoAPIError):
        return bot._or_error_msg(exc, "munito", lang)
    return bot._gemini_error_msg(exc, head_model, lang)


class RouteBudgetExceededError(RuntimeError):
    """Бюджет времени маршрута исчерпан — защита от многоминутного ожидания при массовом сбое моделей подряд."""
    def __init__(self, tried: list[str]) -> None:
        self.tried = tried
        super().__init__(f"Route time budget exhausted. Tried: {', '.join(tried) or 'none'}")


# Маршрутизация вынесена в lumen_router_config.py — порядок имён для остального кода не менялся.

async def _run_route(
    chat_id: int, ai_prompt: str, route: list[tuple[str, str]], message: Message, *,
    media: list[tuple[bytes, str]] | None = None, media_filename: str = "",
    youtube_url: str | None = None, allow_stream: bool = False,
    deadline: float | None = None, provenance: dict | None = None,
    status: Any | None = None, history_text: str | None = None,
    history_meta: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Идёт по маршруту _build_route: внутри провайдера — по одной попытке на модель, при отказе всего провайдера — резервный. Возвращает (ответ, reply_already_sent)."""
    import bot
    if not route:
        raise RuntimeError("Empty route — no model to choose from.")
    route_started = time.monotonic()
    # В историю пишем чистый запрос пользователя, а не ai_prompt со сниппетами
    # и датой: чужой текст не должен переживать ход (аудит 28.10.2026).
    # Пустая строка — законный текст фото без подписи, поэтому None, а не or.
    if history_text is None:
        history_text = ai_prompt
    # Порядок внутри провайдера — по измеренным задержкам; сами блоки и защита квоты Gemini не трогаем.
    route = _reorder_route_by_speed(route)
    if deadline is None:
        # Снаружи дедлайн приходит с транскрибацией: она уже потратила часть бюджета.
        deadline = time.monotonic() + bot.ROUTE_TOTAL_BUDGET_SEC

    # Блоки идут ПОДРЯД, как собраны: Munito → Gemini-резерв.
    # Склейка по провайдеру ставила 11 Gemini-моделей впереди независимого провайдера
    # и съедала 40-секундный бюджет до него (аудит 27.10.2026).
    runs: list[tuple[str, list[str]]] = []
    for provider, model_id in route:
        if runs and runs[-1][0] == provider:
            runs[-1][1].append(model_id)
        else:
            runs.append((provider, [model_id]))

    # Учёт дневных лимитов пользователя — только дошедшие до модели ответы.
    # С 05.11.2026 в счёт идёт только Gemini: у Munito лимит оконный на стороне
    # провайдера, своя суточная метка ему не нужна.
    def _count_user_answer(provider_name: str) -> None:
        bot._record_user_daily(bot._user_key_for_message(message), gemini=provider_name == "gemini")
        bot._record_stats_event("answers_sent")

    # Кто фактически ответил (прод 12.10.2026): для пост-проверки живого поиска.
    def _note_answered(provider_name: str, model_id: str | None) -> None:
        if provenance is not None:
            provenance.update(provider=provider_name, model=model_id)

    # Стрим пробуем на каждой модели маршрута, а не только на голове: раньше
    # сбой головной модели (квота/404/таймаут) навсегда лишал чат эффекта
    # печати, хотя резервные модели отвечали (прод 28.10.2026).
    # Ранний сбой стрима не отменяет обычный вызов: модель пробуется ещё раз
    # без потока, иначе сломанный SSE у провайдера остался бы без ответа вовсе.
    reusable_placeholder: Message | None = status.message if status is not None else None
    # Единый старт ожидания на весь маршрут: таймер «Думаю… N с» не должен
    # откатываться при переходе к следующей модели (ревью 28.10.2026).
    stream_wait_started: float | None = None
    if allow_stream and (media or youtube_url):
        # Стрим-обёртки собирают контекст без вложений: с медиа только обычный путь.
        allow_stream = False

    def _streamable(provider_name: str, model_id: str) -> bool:
        if provider_name == "gemini":
            return bool(GEMINI_MODELS.get(model_id, {}).get("stream", True))
        return provider_name == "munito"

    async def _try_stream(provider_name: str, model_id: str):
        kwargs = dict(
            deadline=deadline, status=status, history_text=history_text,
            history_meta=history_meta,
            reuse_placeholder=reusable_placeholder, wait_started=stream_wait_started,
        )
        if provider_name == "gemini":
            return await bot._try_gemini_streaming(chat_id, ai_prompt, message, model_id, **kwargs)
        return await bot._try_munito_streaming(chat_id, ai_prompt, message, model_id, **kwargs)

    is_video_or_audio = bool(media) and any(not (m[1] or "").startswith("image/") for m in media)
    last_exc: Exception | None = None
    media_note_added = False
    for run_index, (provider, run_ids) in enumerate(runs):
        if provider == "munito" and is_video_or_audio:
            # Munito принимает только картинки: смешанный альбом уходит целиком в Gemini,
            # иначе видео-слайд потерялся бы без ответа на него.
            continue
        ids = list(run_ids)
        if time.monotonic() > deadline:
            log.warning('[router] Route time budget exhausted before trying provider %s.', provider)
            break
        if allow_stream:
            for stream_model in ids:
                if not _streamable(provider, stream_model):
                    continue
                if time.monotonic() > deadline:
                    log.warning('[router] Route time budget exhausted before streaming %s/%s.', provider, stream_model)
                    break
                if stream_wait_started is None:
                    stream_wait_started = time.monotonic()
                streamed, placeholder = await _try_stream(provider, stream_model)
                if placeholder is not None:
                    reusable_placeholder = placeholder
                if streamed is not None:
                    log.info('[router] chat=%s response received via streaming (%s:%s)', chat_id, provider, stream_model)
                    _count_user_answer(provider)
                    _note_answered(provider, stream_model)
                    return streamed, True
        try:
            if provider == "gemini":
                ans = await bot.ask_gemini(chat_id, ai_prompt, media=media, youtube_url=youtube_url, model_chain=ids, deadline=deadline, provenance=provenance, history_text=history_text, history_meta=history_meta)
            elif provider == "munito" and media:
                ans = await bot.ask_munito_multimodal(chat_id, ai_prompt, media, media_filename, model_chain=ids, deadline=deadline, history_text=history_text, provenance=provenance, history_meta=history_meta)
            elif provider == "munito":
                ans = await bot.ask_munito_text(chat_id, ai_prompt, model_chain=ids, deadline=deadline, history_text=history_text, provenance=provenance, history_meta=history_meta)
            else:
                # Явный провайдер вместо «всё остальное = Munito»: опечатка в имени
                # молча ушла бы в чужой API с нашим ключом (ревью 05.11.2026).
                raise RuntimeError(f"Route has unknown provider {provider!r}")

            if status is not None:
                # Фиксируем длительность ожидания и останавливаем анимацию до
                # записи ответа, иначе тикер перезапишет готовый текст.
                await status.finish()
            if reusable_placeholder is not None:
                # Готовый ответ — прямо в плейсхолдер (если влезает в одно сообщение), иначе обычным путём с разбиением.
                fits_one_message = len(_split_text_chunks(ans, bot.TG_MAX_LEN)) == 1
                reused = False
                if fits_one_message:
                    if status is not None:
                        html_text, plain_text = status.compose_answer(ans)
                    elif getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE:
                        # Обычный текст после раннего сбоя стрима: статуса нет,
                        # но «Thought for Ns» над ответом всё равно показываем.
                        html_text, plain_text = thought_block(
                            bot._chat_lang(chat_id), int(time.monotonic() - route_started), ans,
                        )
                    else:
                        html_text, plain_text = None, ans
                    if html_text is not None:
                        edited = await bot._tg_call(
                            reusable_placeholder.edit_text, html_text,
                            parse_mode=ParseMode.HTML, call_timeout=15.0,
                        )
                        reused = edited is not None
                        if not reused:
                            edited = await bot._tg_call(
                                reusable_placeholder.edit_text, plain_text,
                                parse_mode=None, call_timeout=15.0,
                            )
                            reused = edited is not None
                    else:
                        reused = await bot._edit_message_quietly(reusable_placeholder, ans)
                if not reused:
                    await bot._delete_message_quietly(reusable_placeholder)
                reusable_placeholder = None
                if reused:
                    _count_user_answer(provider)
                    return ans, True
            _count_user_answer(provider)
            return ans, False
        except _SearchRequested as req:
            # Не неудача провайдера: следующая модель ничего не изменит, а пузырь
            # (если он уже в чате) отдаётся вызывающему коду живым.
            if req.placeholder is None:
                req.placeholder = reusable_placeholder
            raise
        except asyncio.CancelledError:
            # Отмена посреди ответа: плейсхолдер чистим, отмену пробрасываем —
            # иначе в чате навсегда висело "…".
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass
                status = None
            if reusable_placeholder is not None:
                try:
                    await bot._delete_message_quietly(reusable_placeholder)
                except Exception:
                    pass
                reusable_placeholder = None
            raise
        except Exception as exc:
            last_exc = exc
            if provider == "gemini" and isinstance(exc, ValueError) and media and not media_note_added:
                # Неподдерживаемый mime: текстовый фолбэк медиа не видит, без пометки ответит вслепую.
                if not ai_prompt.endswith(bot._NO_MEDIA_NOTE):
                    ai_prompt += bot._NO_MEDIA_NOTE
                media_note_added = True
            log.warning('[router] Provider %s failed completely (%s), trying the next one on the route, if any.', provider, exc)
            # Суточный счётчик /stats: дальше по маршруту есть кому ответить.
            if any(ids for _, ids in runs[run_index + 1:]):
                bot._record_stats_event("fallbacks")
                if status is not None:
                    await status.set_phase("progress_retry")

    if status is not None:
        # Невостребованный статус убираем перед raise — иначе «Думаю…» повиснет в чате навсегда.
        await status.delete()
    elif reusable_placeholder is not None:
        await bot._delete_message_quietly(reusable_placeholder)

    # Суточный счётчик /stats: маршрут исчерпан, ответа не будет.
    bot._record_stats_event("all_failed")
    raise last_exc or RuntimeError("No route candidate returned an answer.")
