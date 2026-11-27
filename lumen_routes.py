"""lumen_routes.py handles LLM routing: Munito with fallback to Gemini, history,
configs, and _run_route. Replaceable names are called through `bot.` in tests
so monkeypatch sees them as before.
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
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, Message
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
    reorder_route as _speed_reorder_route,
)
from lumen_router_config import (
    GEMINI_MODELS,
    GEMINI_DEFAULT_CHAIN,
    _looks_like_heavy_query,
)
from lumen_search import _SearchRequested, search_marker_requested, strip_search_marker
from lumen_security import _polish_answer, _detect_garbled_mix, _is_garbled_echo
from lumen_rich import NO_LINK_PREVIEW

# Trim the long voice transcript from the top — otherwise, the downstream route hits model limits.
_TRANSCRIPT_MAX_CHARS = 4000
# Transcription must not consume the entire route budget: voice notes are short (up to
# VOICE_TRANSCRIBE_MAX_BYTES), the turbo model responds in seconds — leave the remainder to the model.
_TRANSCRIBE_TIMEOUT_SEC = 15.0

log = logging.getLogger("bot")

class MunitoAPIError(RuntimeError):
    """Error from Munito's OpenAI-compatible endpoint (the status is stored alongside it; _error_status/_classify_model_error read it)."""
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

def _attempt_timeout(bot: Any, deadline: float | None) -> float:
    """Timeout for one attempt: no more than ROUTE_MODEL_TIMEOUT_SEC and no more than
    the route's remaining budget. Previously each attempt waited the full 22s, so
    the route held the chat lock for up to 40+22s instead of the stated 40s (audit 26.09.2026).
    The 0.5s lower bound makes an expired budget fail quickly instead of hanging."""
    if deadline is None:
        return bot.ROUTE_MODEL_TIMEOUT_SEC
    return max(0.5, min(bot.ROUTE_MODEL_TIMEOUT_SEC, deadline - time.monotonic()))

async def _munito_request(path: str, method: str = "GET", *, json_body: dict | None = None, deadline: float | None = None) -> Any:
    """Direct request to Munito AI (OpenAI-compatible API): one-attempt budget and
    removal of the key from error text. Whisper calls Groq separately and directly
    (_transcribe_audio uses multipart form, not JSON)."""
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
    """Who actually answered and how the answer ended: without a model in the provenance, the trace cannot be tied to a model (backup sample 05.11.2026: OR/Groq wrote None)."""
    if provenance is None:
        return
    provenance.update(provider=provider, model=out.get("model"), finish_reason=str(out.get("finish_reason") or ""))


def _log_llm_usage(*, chat_id: Any, provider: str, model_id: str,
                    prompt: Any = None, completion: Any = None, total: Any = None) -> None:
    """One usage line per successful response — only if the API returned counters (otherwise stay silent)."""
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
    """Gemini daily quota versus per-minute request limit. Both arrive as 429,
    but only the former should lock the model until morning: the per-minute limit at Google can
    trigger on any spike (adversarial review 27.09.2026). Error texts follow Google's
    documentation (RESOURCE_EXHAUSTED with \"per day\"/\"daily limit\"). We also catch joined
    PerDay in metric names such as GenerateRequestsPerDayPerProjectPerModel; otherwise a
    real daily quota looked like a per-minute spike."""
    low = text.lower()
    return any(tok in low for tok in (
        "per day", "per_day", "perday", "daily limit", "daily quota", "quota exceeded for the day",
    ))

# Refusal to name/version a model in 400 text: "invalid model," "does not exist,"
# «not found for API version», «deprecated», «unsupported model».
_MODEL_MISSING_RE = re.compile(
    r"(?:invalid|unknown|no such|unsupported|deprecated|not a valid)\s+model"
    r"|model(?!\s+(?:output|response|file|image|request))[^.\n]{0,60}"
    r"(?:not valid|invalid|does not exist|not found|deprecated|unsupported|no longer available|shut down)"
    r"|model_not_found",
    re.IGNORECASE,
)


def _mark_model_unavailable(provider: str, model_id: str, exc: Exception) -> bool:
    """A \"model unavailable\" error (503/404/invalid model) → a short cooldown, as for 429.

Production 05.10.2026: five Gemini 3.x models in a row returned 503, and each
retried on every request — up to 22 seconds and five wasted attempts per request.
The official model list and the AI Studio dashboard showed that the models were alive and
quota was available, so this was a temporary refusal, not removal from the free tier. Therefore we do NOT
remove the model from the chains; we skip it for QUOTA_RATE_LIMIT_COOLDOWN_SEC:
the cooldown expires on its own, and a successful response clears the mark anyway."""
    import bot
    try:
        txt = bot._error_text(exc).strip() or exc.__class__.__name__
        status = bot._error_status(exc, txt)
        if bot._classify_model_error(status, txt) != "unavailable":
            return False
        # 400 — a deterministic error for one request (an oversized image),
        # rather than model unavailability: cooldown disabled the head for everyone (review 17.11.2026).
        if status == 400:
            # Only a refusal about the MODEL: "File ... does not exist" and "Requested
            # entity does not exist" — a request error and cooldown do not deserve
            # (adversarial pass no. 2).
            if not _MODEL_MISSING_RE.search(txt):
                return False
        bot._mark_rate_limited(provider, model_id)
        return True
    except Exception:
        return False


def _raise_if_search_requested(answer: str, provider: str, model_id: str) -> str:
    """The response is exactly the [[SEARCH]] marker: data was insufficient and the model requests a search. Stops the route BEFORE writing to chat memory (the marker in history would prime subsequent turns); the call quota is still recorded because the call really happened (measured 11.11.2026)."""
    if not search_marker_requested(answer):
        return strip_search_marker(answer)
    import bot
    log.info('[router] %s/%s asked for a web search instead of answering', provider, model_id)
    bot._record_quota_usage(provider, model_id)
    raise _SearchRequested()


async def _chat_completion_chain(
    messages: list[dict], trial_models: list[str], primary_model_id: str, *,
    request_fn, provider: str, deadline: float | None = None,
    log_label: str | None = None, chat_id: Any = None, user_text: str | None = None,
    out: dict | None = None, extra_payload: dict | None = None,
    extra_payload_fn=None, record_latency: bool = True,
) -> tuple[str, str]:
    """Shared fallback loop over a chain of OpenAI-compatible models (currently Munito).

    extra_payload contains provider-specific fields on top of the standard body (for Munito, reasoning_effort): a payload copy in one branch must not diverge from the others (audit 26.09.2026). extra_payload_fn is needed where fields depend on the attempted model: not all catalog models accept reasoning_effort (measurement 12.11.2026: gemma-4-12b returns 400), and the field must be recalculated after falling back to another model.

    Exactly one attempt per model: retries of the same model during widespread instability slowed the entire route. This loop previously lived in three copies that silently diverged from the original (audit 26.09.2026). Returns (answer, used_model); otherwise raises the last exception or RouteBudgetExceededError.

    log_label is a tag for logs/scrub sources: providers historically used a short tag (\"or\"), so it is supplied externally rather than derived from the provider string."""
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
        system_text = bot.get_system_prompt(model_trial)
        if chat_id is not None:
            # The builder already includes memory; overwriting it with a bare prompt lost the memory
            # on every non-streaming Munito attempt (review 17.11.2026).
            system_text = lumen_memory.system_prompt_with_memory(
                system_text, bot.get_state(chat_id))
        messages[0]["content"] = system_text
        attempt_start = time.monotonic()
        try:
            extras = extra_payload_fn(model_trial) if extra_payload_fn else (extra_payload or {})
            payload = {"model": model_trial, "messages": messages, "stream": False, **extras}
            resp = await request_fn(payload, deadline)
            choices = resp.get("choices") or []
            answer = ""
            if choices:
                answer = bot._or_extract_text(choices[0].get("message") or "")
            answer = _strip_provider_artifacts(answer)
            if _is_empty_like(answer):
                # An empty response is a reason to try the next model (prod 17.09.2026: a user
                # saw "Empty response" twice).
                bot._record_model_outcome(provider, model_trial, bad=True)
                raise RuntimeError(f"Model {model_trial} returned an empty response")

            answer = _polish_answer(answer, source=f"{provider}_chat_completion:{model_trial}")
            # Mush-mush also counts as bad (delivered as is, as before),
            # success resets the quarantine counter. Retrying mixed tokens for
            # by the user is an echo, not model word salad, and does not count.
            mush = _detect_garbled_mix(answer) and not _is_garbled_echo(answer, user_text)
            bot._record_model_outcome(provider, model_trial, bad=mush)
            if out is not None:
                first = choices[0] if choices and isinstance(choices[0], dict) else {}
                out["model"] = model_trial
                out["finish_reason"] = str(first.get("finish_reason") or "")
                if out["finish_reason"] == "length":
                    # Provider output cap: the answer was cut off (prod 05.11.2026 —
                    # trimming was visible in neither the log nor the trace).
                    log.warning('[%s] Model %s hit the output cap, answer truncated', label, model_trial)
            log.info('[%s] Successful response from model %s (primary=%s, models tried: %d)', label, model_trial, primary_model_id, len(tried))
            _pt, _ct, _tt = _usage_from_openai_response(resp)
            _log_llm_usage(chat_id=chat_id, provider=provider, model_id=model_trial,
                           prompt=_pt, completion=_ct, total=_tt)
            if record_latency:
                # A service probe (selftest) does not feed the overall EMA: four tokens
                # would shift the model order for all chats (review 08.11.2026).
                _record_model_latency(_model_speed_key(provider, model_trial), total_sec=time.monotonic() - attempt_start)
            return answer, model_trial
        except bot.RouteBudgetExceededError:
            raise
        except Exception as exc:
            last_exc = exc
            bot._record_stats_event("model_failures")
            _mark_model_unavailable(provider, model_trial, exc)
            # 429 goes into cooldown, like streaming above: Munito has windowed limits
            # (200 requests per 30-second window), and a daily quota that was worth
            # keep it closed until morning; it has none.
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


async def ask_munito_text(chat_id: int, user_text: str, model_chain: list[str], *, deadline: float | None = None, history_text: str | None = None, provenance: dict | None = None, history_meta: dict[str, Any] | None = None, effort: str | None = None) -> str:
    """Text through Munito AI: one attempt per model, identity leak scrubbing, charged to the \"munito\" quota. Memory is a single log of up to 200 messages; past photos are attached as a separate message."""
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
        out=_out, extra_payload_fn=lambda model: _munito_payload_extras(effort, model),
    )
    _note_provenance(provenance, "munito", _out)
    answer = _raise_if_search_requested(answer, "munito", model_trial)

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
    effort: str | None = None,
) -> str:
    """Images through Munito AI (vision verified by a live call on 05.11.2026): all images in one message as image_url blocks, one per photo; otherwise 2-9 album photos were silently lost. Video and audio do not reach this path: the Gemini branch handles them because the OpenAI format does not accept non-images. Everything else follows ask_munito_text."""
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
        out=_out, extra_payload_fn=lambda model: _munito_payload_extras(effort, model),
    )
    _note_provenance(provenance, "munito", _out)
    answer = _raise_if_search_requested(answer, "munito", model_trial)

    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    lumen_memory.append_assistant(state, answer)
    await bot._trim_history(state["history"], state)
    bot._record_quota_usage("munito", model_trial)
    return answer

_SELFTEST_PING_TEXT = "Reply with exactly: ok"


def _selftest_short_error(exc: BaseException) -> str:
    """Short one-liner for the /selftest row (DMs only — model IDs are allowed here)."""
    text = f"{exc.__class__.__name__}: {exc}".strip()
    # The provider's organization ID is internal information even for the owner: scrub it like tokens.
    text = re.sub(r"org_[A-Za-z0-9]+", "<ORG>", text)
    return " ".join(text.split())[:120] or exc.__class__.__name__

async def selftest_llm_head(provider: str, *, chat_id: Any = None) -> tuple[bool, str, float]:
    """One tiny probe to the route head (munito/gemini) for /selftest.

    This is a service probe (service=True): it does NOT write to quota on either
    success or failure — it is not an answer to a person. It does not touch chat
    history — the probe must not linger in the owner's context. It never raises:
    failure is returned as a (False, reason, seconds) tuple."""
    import bot
    from lumen_router_config import _MUNITO_ORDER, _munito_payload_extras
    start = time.monotonic()
    try:
        # Read query names through bot. at call time so test substitution keeps working.
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
            # A trial ping is a service call: it must not count toward the quota or clear exhaustion marks
            # it is not written; otherwise one check restores the entire provider.
            # Do not record latency either: record_latency=False is set above.
            bot._record_quota_usage("munito", used, service=True)
            return True, "", round(time.monotonic() - start, 2)
        if provider == "gemini":
            # The scarcest quota gets a minimal probe: bare text without a system
            # prompt and history (unlike ask_gemini, which only checks liveness).
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
            # The same honesty as the other probes: success resets the counter.
            # A ping without mixed tokens has nowhere for an echo to come from; this check is for consistency.
            bot._record_model_outcome("gemini", model,
                                      bad=_detect_garbled_mix(ans) and not _is_garbled_echo(ans, _SELFTEST_PING_TEXT))
            _gpt, _gct, _gtt = _usage_from_gemini_response(resp)
            _log_llm_usage(chat_id=chat_id, provider="gemini", model_id=model,
                           prompt=_gpt, completion=_gct, total=_gtt)
            # A few-token ping does not feed the overall latency EMA: it would shift
            # model order and the first timeout for all chats (audit run-2).
            bot._record_quota_usage("gemini", model, service=True)
            return True, "", round(time.monotonic() - start, 2)
        raise ValueError(f"unknown selftest provider: {provider}")
    except Exception as exc:
        return False, _selftest_short_error(exc), round(time.monotonic() - start, 2)

async def _transcribe_audio(audio_bytes: bytes, mime: str, chat_id: int, deadline: float | None = None) -> str | None:
    """Transcribes voice/audio to text via Groq Whisper (an order of magnitude cheaper
    than Gemini quota). Returns None on any failure; the caller silently continues
    via the existing path (audio sent directly to Gemini). The timeout is the remaining
    route budget, not the full limit; otherwise voice notes would hold the lock past the budget."""
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
    # An untrimmed long voice note would hit the model limits further down the route.
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
        # Old records may have stored a provider structure instead of a string: parse
        # it.
        txt = lumen_memory.format_entry(entry) if isinstance(cont, str) else bot._or_extract_text(cont)
        if txt:
             contents.append(types.Content(role=r, parts=[types.Part.from_text(text=txt)]))
    return contents

def _build_gemini_call_config(
    model_id: str, contents: list[types.Content], *, memory_summary: str = "",
    effort: str | None = None,
) -> tuple[list[types.Content], "types.GenerateContentConfig | None"]:
    """Builds (call_contents, gconfig) for ONE Gemini call: system_instruction and dashboard grounding. One function serves ask_gemini and streaming so they cannot diverge."""
    import bot
    conf = GEMINI_MODELS.get(model_id, {})
    kwargs: dict[str, Any] = {}
    kwargs["system_instruction"] = lumen_memory.system_prompt_with_memory(
        bot.get_system_prompt(model_id), {"memory_summary": memory_summary},
    )
    # Tools — only for those with free quota on the dashboard (3.5/preview map is 0/0).
    tools_list = []
    if conf.get("search_grounding", True):
        try:
            tools_list.append(types.Tool(google_search=types.GoogleSearch()))
        except Exception:
            pass  # this SDK does not have google_search
    if conf.get("map_grounding", False):
        try:
            tools_list.append(types.Tool(google_maps=types.GoogleMaps()))
        except Exception:
            pass  # this SDK does not have google_maps
    if conf.get("url_context", True):
        try:
            tools_list.append(types.Tool(url_context=types.UrlContext()))
        except Exception:
            pass  # this SDK has no url_context
    if tools_list:
        kwargs["tools"] = tools_list
    # Thinking effort: since 12.11.2026 the level has been computed for the entire request (Claude-style
    # effort) and only when EFFORT_AUTO=0 does the previous non-heavy heuristic remain (calibration
    # On 18.08.2026, 3.7-flash was caught timing out 17 times out of 18). 3.x uses thinking_level; 2.5 uses thinking_budget.
    if effort is not None:
        from lumen_router_config import gemini_thinking_for

        _param, _value = gemini_thinking_for(model_id, effort)
        if _param:
            kwargs["thinking_config"] = types.ThinkingConfig(**{_param: _value})
    else:
        last_text = next((p.text for p in reversed(contents[-1].parts) if getattr(p, "text", None)), "") if contents else ""
        if not _looks_like_heavy_query(last_text):
            if model_id.startswith("gemini-3"):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
            elif model_id.startswith("gemini-2.5"):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    gconfig = types.GenerateContentConfig(**kwargs) if kwargs else None

    return contents, gconfig

async def _extract_gemini_answer_text(resp: Any, *, model_id: str, call_contents: list, gconfig, deadline: float | None = None) -> str:
    """Gemini response text: resp.text first, otherwise manual parsing of candidates/parts (including a retry WITHOUT tools on MALFORMED_FUNCTION_CALL)."""
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
                # A response without text: a broken call or a bare tool_call — the user
                # Do not show tool JSON (production 07.10.2026); retry without it.
                if tool_calls:
                    log.warning("[gemini] Model %s returned only tool calls without text, retrying without tools.", model_id)
                try:
                    retry_gconfig = gconfig.model_copy(update={"tools": None}) if gconfig is not None else None
                    # Async client, not to_thread: wait_for genuinely cancels a hung
                    # request. The retry also counts against the route budget, otherwise it would hold the lock
                    # on top of what has already been spent.
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
                        # The retry succeeded but returned empty without exhausting the budget:
                        # this is a reason to move to the next model.
                        log.warning("[gemini] Retry after MALFORMED_FUNCTION_CALL returned empty text, trying the next model.")
                    elif deadline is not None and deadline - time.monotonic() <= 0:
                        # The retry did not fit within the route budget: return empty,
                        # ask_gemini will move to the next model.
                        log.warning("[gemini] Retry after MALFORMED_FUNCTION_CALL skipped: route budget is exhausted, trying the next model.")
                except Exception as retry_exc:
                    log.warning("[gemini] Retry without tools after MALFORMED_FUNCTION_CALL also failed: %s", retry_exc)
                    # The retry failed — return empty: the route will move to the next model,
                    # rather than stopping at the first broken one.
            # A block (SAFETY/RECITATION) is not an answer: an empty string advances the chain
            # further, and the user sees the localized refusal only if no answer was produced
            # anyone (27.10.2026 audit: a Russian placeholder stopped the route).
    # An empty line without blocking is not “Empty response”: ask_gemini tries the next model.
    return ans.strip()

def _user_mime_label(mime: str | None) -> str:
    """Attachment type label for an error message: a client-supplied MIME type must
    not become markup (a markdown link) in the bot message (run-2 audit, low).
    Type parameters (charset) are allowed; parentheses and angle brackets are
    not."""
    if mime and mime.isascii() and all(c.isalnum() or c in "._+-/:;= " for c in mime):
        return mime
    return "неизвестный тип"


async def ask_gemini(
    chat_id: int, user_text: str, media: list[tuple[bytes, str]] | None = None,
    youtube_url: str | None = None, model_chain: list[str] | None = None,
    deadline: float | None = None, provenance: dict | None = None,
    history_text: str | None = None, history_meta: dict[str, Any] | None = None,
    effort: str | None = None,
) -> str:
    import bot
    state = bot.get_state(chat_id)
    chain = list(model_chain) if model_chain else list(GEMINI_DEFAULT_CHAIN)
    if deadline is None:
        deadline = time.monotonic() + bot.ROUTE_TOTAL_BUDGET_SEC

    # Media/YouTube parts in the same Content as the text (assembly happens only in _build_gemini_turn_contents).
    extra_parts: list[types.Part] = []
    if media:
        for b, mime in media:
             if _is_gemini_supported_mime(mime):
                 extra_parts.append(types.Part.from_bytes(data=b, mime_type=mime))
             else:
                 raise bot.UserFacingInputError(bot._t(
                     chat_id, "err_unsupported_attachment", mime=_user_mime_label(mime)))
    if youtube_url:
          # YouTube is a file_uri with no download. Set mime_type explicitly to video/*:
          # the SDK does not infer it for shorts URLs ("Failed to determine mime type").
         extra_parts.append(types.Part.from_uri(file_uri=youtube_url, mime_type="video/*"))
    contents = await bot._build_gemini_turn_contents(
        chat_id, user_text, extra_parts=extra_parts or None, history_meta=history_meta,
    )

    # to_thread keeps the event loop unblocked. Exactly one attempt per model (retries of the same model produced answers taking 2+ minutes); the order comes from the router, with the overall deadline cutting it off at the top.
    resp = None
    curr_model_id = chain[0]
    tried_models: set[str] = set()
    quota_exhausted_models: list[str] = []

    loop_guard = 0
    # An extra len+4 beyond the chain length: protection against looping in future
    # fallback branches (currently all transitions occur only within chain).
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
            effort=effort,
        )

        attempt_start = time.monotonic()
        try:
            # Use an async client, not to_thread: this way wait_for actually cancels a hung
            # request (a to_thread stream cannot be canceled — it would keep running in the background).
            resp = await asyncio.wait_for(
                bot.client.aio.models.generate_content(model=curr_model_id, contents=call_contents, config=gconfig),
                timeout=_attempt_timeout(bot, deadline),
            )
            ans = await bot._extract_gemini_answer_text(resp, model_id=curr_model_id, call_contents=call_contents, gconfig=gconfig, deadline=deadline)
            ans = _strip_provider_artifacts(ans)
            if _is_empty_like(ans):
                # Empty response — try the next model (prod 17.09.2026: a user saw the literal “Empty response”).
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
            # Use shared helpers (_error_status/_classify_model_error) for classification, not a third ad-hoc copy.
            txt = bot._error_text(exc).strip() or exc.__class__.__name__
            status_code = bot._error_status(exc, txt)
            kind = bot._classify_model_error(status_code, txt)
            exc_class = exc.__class__.__name__

            if kind == "rate_limit":
                # A 429 from Google is usually a per-minute request limit, not a daily quota.
                # Previously, any 429 set a daily mark, and one burst removed the model from
                # route until midnight (adversarial review 27.09.2026). The daily Google quota
                # identify it by the error text — then the marker stays until morning.
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
                # Per-minute spikes only: there was no daily exhaustion — return
                # return the last 429 as-is, not an empty "everything exhausted".
                log.warning("[gemini] All Gemini models in route rate-limited (429), no daily exhaustion.")
                raise

            # All other outcomes get one attempt, then move directly to the next model, without retries.
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

    ans = _polish_answer(ans, source=f"ask_gemini:{curr_model_id}")
    bot._record_model_outcome("gemini", curr_model_id,
                              bad=_detect_garbled_mix(ans) and not _is_garbled_echo(ans, user_text))
    ans = _raise_if_search_requested(ans, "gemini", curr_model_id)

    hist_text = _history_user_text(history_text if history_text is not None else user_text)
    lumen_memory.append_user_from_meta(state, hist_text, history_meta)
    lumen_memory.append_assistant(state, ans)
    # Trim old content with a summary (see _trim_history), rather than silently slicing.
    await bot._trim_history(state["history"], state)
    bot._record_quota_usage("gemini", curr_model_id)
    if provenance is not None:
        provenance.update(provider="gemini", model=curr_model_id)
    return ans

async def _build_gemini_turn_contents(
    chat_id: int, user_text: str, extra_parts: list[types.Part] | None = None,
    *, history_meta: dict[str, Any] | None = None,
) -> list[types.Content]:
    """Contents for one turn: memory with authors, past photos, and the current question."""
    import bot
    state = bot.get_state(chat_id)
    contents = await bot._gemini_history_contents(state.get("history") or [])
    meta = history_meta if isinstance(history_meta, dict) else {}
    skip = set(meta.get("skip_file_ids") or [])
    if meta.get("attach_past", True):
        past = lumen_memory.recent_media(
            state, "photo",
            window=lumen_memory.PHOTO_CONTEXT_WINDOW,
            max_items=lumen_memory.photo_context_limit(user_text),
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
    """Messages for OpenAI-compatible chat/completions: memory, authors, and past photos.

Past photos are attached as a separate user message rather than to the current question, so the model does not confuse them with the newly sent attachment."""
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
            max_items=lumen_memory.photo_context_limit(user_text),
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
    # History: authors and time; clean old service blocks on the fly.
    for item in history:
        entry = _clean_history_entry(item) if isinstance(item, dict) else None
        if entry is None:
            continue
        cont = entry.get("content")
        # Old records could store a provider structure: parse it like the Gemini branch.
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
    """Response text for an exception from _run_route; a pure function (the owner alert stays in the calling code, where the network call is made)."""
    import bot
    if youtube_url_to_analyze:
        return bot._lang_t(lang, "err_youtube_fail")
    if isinstance(exc, bot.GeminiAllModelsExhaustedError):
        return bot._gemini_error_msg(exc, head_model, lang)
    if isinstance(exc, bot.RouteBudgetExceededError):
        return bot._lang_t(lang, "err_budget")
    if isinstance(exc, bot.MunitoAPIError):
        return bot._or_error_msg(exc, lang)
    return bot._gemini_error_msg(exc, head_model, lang)


class RouteBudgetExceededError(RuntimeError):
    """The route budget expired before a provider response was accepted, including before the first attempt."""
    def __init__(self, tried: list[str]) -> None:
        self.tried = tried
        super().__init__(f"Route time budget exhausted. Tried: {', '.join(tried) or 'none'}")


def _reorder_route_by_speed(
    route: list[tuple[str, str]], *, preserve_grounded_head: bool = False,
) -> list[tuple[str, str]]:
    """Order by measured speed. On a fresh route, do not cross the search-grounding
    boundary: otherwise a model without live search is at the head (review 17.11.2026).
    On the regular route, pinning the head is unnecessary — the sorting was created
    to remove a slow model (prod 17.09.2026), and an adversarial pass showed
    that a shared pin froze the slow head of the heavy chain."""
    if not preserve_grounded_head:
        return _speed_reorder_route(route)

    def _capability(item: tuple[str, str]) -> bool:
        provider, model_id = item
        if provider == "gemini":
            # An unknown configuration is no reason to put it ahead of the other.
            return bool(GEMINI_MODELS.get(model_id, {}).get("search_grounding", False))
        return True
    return _speed_reorder_route(route, group_key=_capability)


# Routing has been moved to lumen_router_config.py — the order of names for the rest of the code has not changed.

async def _run_route(
    chat_id: int, ai_prompt: str, route: list[tuple[str, str]], message: Message, *,
    media: list[tuple[bytes, str]] | None = None, media_filename: str = "",
    youtube_url: str | None = None, allow_stream: bool = False,
    deadline: float | None = None, provenance: dict | None = None,
    status: Any | None = None, history_text: str | None = None,
    history_meta: dict[str, Any] | None = None, effort: str | None = None,
    preserve_grounded_head: bool = False,
) -> tuple[str, bool]:
    """Follows the _build_route route: one attempt per model within a provider, then the reserve if the entire provider fails. Returns (response, reply_already_sent)."""
    import bot
    if not route:
        raise RuntimeError("Empty route — no model to choose from.")
    # Write the clean user query to history, not ai_prompt with snippets
    # and date: third-party text must not survive a turn (audit 28.10.2026).
    # An empty string is valid text for a photo without a caption, so use None, not or.
    if history_text is None:
        history_text = ai_prompt
    # Order within a provider follows measured latencies; the blocks themselves and Gemini quota protection are untouched.
    route = _reorder_route_by_speed(route, preserve_grounded_head=preserve_grounded_head)
    if deadline is None:
        # Externally, the deadline arrives with transcription: it has already spent part of the budget.
        deadline = time.monotonic() + bot.ROUTE_TOTAL_BUDGET_SEC

    # Blocks go in order, as assembled: Munito → Gemini reserve.
    # Provider-based merging placed 11 Gemini models ahead of an independent provider
    # and consumed the entire 40-second budget before it (audit 27.10.2026).
    runs: list[tuple[str, list[str]]] = []
    for provider, model_id in route:
        if runs and runs[-1][0] == provider:
            runs[-1][1].append(model_id)
        else:
            runs.append((provider, [model_id]))

    # Per-user daily limit accounting — only responses that reached the model.
    # Since 05.11.2026, only Gemini counts toward the quota: the Munito limit is windowed on the
    # provider; it does not need its own daily mark.
    def _count_user_answer(provider_name: str) -> None:
        # One update means one charge: a retry after [[SEARCH]] or a freshness retry
        # and auto-continuation re-enter _run_route (audit 16.11.2026).
        if bot._ANSWER_COUNTED.get():
            return
        bot._ANSWER_COUNTED.set(True)
        bot._record_user_daily(bot._user_key_for_message(message), gemini=provider_name == "gemini")
        bot._record_stats_event("answers_sent")

    # Who actually answered (production 12.10.2026): for post-checking live search.
    def _note_answered(provider_name: str, model_id: str | None) -> None:
        if provenance is not None:
            provenance.update(provider=provider_name, model=model_id)

    # Try streaming on every model in the route, not just the head: previously
    # a head-model failure (quota/404/timeout) permanently deprived the chat of the effect
    # of typing, although the reserve models answered (prod 28.10.2026).
    # An early stream failure does not cancel a normal call: the model is tried again
    # without a stream; otherwise a broken provider SSE would leave no answer at all.
    reusable_placeholder: Message | None = status.message if status is not None else None
    # One wait start covers the entire route: the "Thinking... N s" timer must not
    # restart when moving to the next model (review 28.10.2026).
    stream_wait_started: float | None = None
    if allow_stream and (media or youtube_url):
        # Streaming wrappers build context without attachments: media uses only the ordinary path.
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
            effort=effort,
        )
        if provider_name == "gemini":
            return await bot._try_gemini_streaming(chat_id, ai_prompt, message, model_id, **kwargs)
        return await bot._try_munito_streaming(chat_id, ai_prompt, message, model_id, **kwargs)

    is_video_or_audio = bool(media) and any(not (m[1] or "").startswith("image/") for m in media)
    last_exc: Exception | None = None
    media_note_added = False
    for run_index, (provider, run_ids) in enumerate(runs):
        if provider == "munito" and is_video_or_audio:
            # Munito accepts images only: a mixed album goes entirely to Gemini,
            # otherwise the video slide would be lost without a response to it.
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
                    # Stop before the first chunk: the bubble is closed with a marker, there is no answer,
                    # do not deduct a slot (low-severity audit finding 16.11.2026).
                    if (streamed or "").strip():
                        _count_user_answer(provider)
                        _note_answered(provider, stream_model)
                    return streamed, True
        if reusable_placeholder is not None and getattr(reusable_placeholder, "reply_markup", None):
            # The non-streaming fallback cannot respond to Stop: remove the button,
            # so it does not look active during a normal request (review
            # 24.11.2026).
            clear = getattr(status, "clear_reply_markup", None) if status is not None else None
            if clear is not None and getattr(status, "reply_markup", None) is not None:
                await clear()
            else:
                edit = getattr(reusable_placeholder, "edit_reply_markup", None)
                if edit is not None:
                    await bot._tg_call(
                        edit, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
                        call_timeout=bot.TELEGRAM_REQUEST_TIMEOUT,
                    )
            reusable_placeholder.reply_markup = None
        try:
            if provider == "gemini":
                ans = await bot.ask_gemini(chat_id, ai_prompt, media=media, youtube_url=youtube_url, model_chain=ids, deadline=deadline, provenance=provenance, history_text=history_text, history_meta=history_meta, effort=effort)
            elif provider == "munito" and media:
                ans = await bot.ask_munito_multimodal(chat_id, ai_prompt, media, media_filename, model_chain=ids, deadline=deadline, history_text=history_text, provenance=provenance, history_meta=history_meta, effort=effort)
            elif provider == "munito":
                ans = await bot.ask_munito_text(chat_id, ai_prompt, model_chain=ids, deadline=deadline, history_text=history_text, provenance=provenance, history_meta=history_meta, effort=effort)
            else:
                # An explicit provider instead of “everything else = Munito”: a typo in the name
                # silently gone to someone else's API with our key (review 05.11.2026).
                raise RuntimeError(f"Route has unknown provider {provider!r}")

            if status is not None:
                # Fix the wait duration and stop the animation before writing the answer;
                # otherwise, the ticker will overwrite the finished text.
                await status.finish()
            if reusable_placeholder is not None:
                # A finished answer goes straight into the placeholder if it fits in one message; otherwise it follows the normal splitting path.
                fits_one_message = len(_split_text_chunks(ans, bot.TG_MAX_LEN)) == 1
                reused = False
                if fits_one_message:
                    if status is not None:
                        html_text, plain_text = status.compose_answer(ans)
                    else:
                        html_text, plain_text = None, ans
                    if html_text is not None:
                        edited = await bot._tg_call(
                            reusable_placeholder.edit_text, html_text,
                            parse_mode=ParseMode.HTML, call_timeout=15.0,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                        reused = edited is not None
                        if not reused:
                            edited = await bot._tg_call(
                                reusable_placeholder.edit_text, plain_text,
                                parse_mode=None, call_timeout=15.0,
                                link_preview_options=NO_LINK_PREVIEW,
                            )
                            reused = edited is not None
                    else:
                        reused = await bot._edit_message_quietly(reusable_placeholder, ans)
                if not reused:
                    await bot._delete_message_quietly(reusable_placeholder)
                reusable_placeholder = None
                if reused:
                    # An empty response (stopped before the first chunk) does not consume a slot:
                    # the person received nothing, and the limit concerns responses (audit 16.11.2026).
                    if (ans or "").strip():
                        _count_user_answer(provider)
                    return ans, True
            if (ans or "").strip():
                _count_user_answer(provider)
            return ans, False
        except _SearchRequested as req:
            # Not a provider failure: the next model will change nothing, while the bubble
            # (if already in the chat) is returned to the calling code alive.
            if req.placeholder is None:
                req.placeholder = reusable_placeholder
            raise
        except asyncio.CancelledError:
            # Cancellation mid-response: clean up the placeholder and propagate the cancellation—
            # otherwise, "…" would remain in the chat forever.
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
                # Unsupported MIME type: the media text fallback does not see it and would answer blindly without a note.
                if not ai_prompt.endswith(bot._NO_MEDIA_NOTE):
                    ai_prompt += bot._NO_MEDIA_NOTE
                media_note_added = True
            log.warning('[router] Provider %s failed completely (%s), trying the next one on the route, if any.', provider, exc)
            # Daily /stats counter: someone later in the route can answer it.
            if any(ids for _, ids in runs[run_index + 1:]):
                bot._record_stats_event("fallbacks")
                if status is not None:
                    await status.set_phase("progress_retry")

    if status is not None:
        # Remove an unclaimed status before raising; otherwise "Thinking..." stays in the chat forever.
        await status.delete()
    elif reusable_placeholder is not None:
        await bot._delete_message_quietly(reusable_placeholder)

    # Daily /stats counter: the route is exhausted; there will be no response.
    bot._record_stats_event("all_failed")
    if last_exc is None and time.monotonic() > deadline:
        # The budget ended before the first attempt: raise RouteBudgetExceededError,
        # not a vague RuntimeError, or the response text and statistics will be wrong
        # (review 17.11.2026).
        raise RouteBudgetExceededError([])
    raise last_exc or RuntimeError("No route candidate returned an answer.")
