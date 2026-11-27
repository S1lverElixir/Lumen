"""lumen_errors.py parses model errors and provides user-facing text:
    rate_limit/paid/forbidden classification, localized templates, and a dated prompt.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from lumen_lang import DEFAULT_LANG, t as _lang_t
from system_prompt import SYSTEM_PROMPT

# OpenAI-compatible providers return a placeholder instead of an answer: choices is non-empty,
# text. Prod 17.09.2026 fixed only empty choices — the literal "Empty response"
# continued reaching the chat (backup sample 05.11.2026: 7 cases in one conversation).
# "none"/"n/a" were removed from the set: they are valid short answers (what the function returns
# without return) gave the model a quarantine and the user a refusal (review 17.11.2026).
_EMPTY_LIKE_TEXTS = frozenset({"empty response", "no response", "no answer"})


def _is_empty_like(text: Any) -> bool:
    """An empty response or a system placeholder in its place."""
    if not isinstance(text, str):
        return not text
    cleaned = text.strip().lower().rstrip(".!")
    return not cleaned or cleaned in _EMPTY_LIKE_TEXTS

log = logging.getLogger("bot")

def _error_text(e: Exception) -> str:
    return " ".join(p for p in [str(e), str(getattr(e, "message", "")), str(getattr(e, "detail", ""))] if p).strip()

# Only explicit patterns ("HTTP 503", "status_code=429", "error 400"): a bare number
# in text ('429 tokens') is not treated as a status; this caused false classification.
_STATUS_RE = re.compile(
    r"(?i)(?:http|status(?:[_ \-]?code)?|статус(?:[_ \-]?код)?"
    r"|error(?:[_ \-]?code)?|ошибка|code|код)[_ \-]*[:=]?\s*(\d{3})\b"
)

def _error_status(e: Exception, text: str) -> int | None:
    for a in ("status_code", "status", "code", "http_status"):
        val = getattr(e, a, None)
        try:
            if val is not None:
                return int(val)
        except Exception:
            pass
    m = _STATUS_RE.search(text or "")
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return None

def _classify_model_error(status: int | None, text: str) -> str:
    low = text.lower()
    # Status 402 takes priority over dictionary tokens: “billing quota exceeded” went to
    # rate_limit and masked an exhausted paid plan (review 17.11.2026).
    if status == 402:
        return "paid"
    if status == 429 or any(tok in low for tok in ("resource_exhausted", "too many requests", "rate limit", "quota", "лимит")):
        return "rate_limit"
    if any(tok in low for tok in ("payment required", "paid tier", "requires paid", "billing", "кредит")):
        return "paid"
    if status in {401, 403} or any(tok in low for tok in ("unauthorized", "forbidden", "permission", "blocked")):
        return "forbidden"
    if status in {400, 404} or any(tok in low for tok in ("not found", "invalid argument", "invalid model", "unsupported", "unavailable")):
        return "unavailable"
    return "other"

# A single text table instead of two copies of the classification: one was edited and the other forgotten.
# Texts without provider/model names and without /model|/provider: for the user, there is only Lumen (see IDENTITY).
_MODEL_ERROR_FALLBACK_MSG = _lang_t(DEFAULT_LANG, "model_err_fallback")

def _model_error_text(kind: str, lang: str = DEFAULT_LANG) -> str:
    key = {
        "rate_limit": "model_err_rate_limit",
        "paid": "model_err_paid",
        "forbidden": "model_err_forbidden",
        "unavailable": "model_err_unavailable",
    }.get(kind, "model_err_fallback")
    return _lang_t(lang, key)

def _or_error_msg(e: Exception, lang: str = DEFAULT_LANG) -> str:
    # No raw API text: it contains HTML/JSON and internals (same in _gemini_error_msg).
    txt = _error_text(e).strip() or e.__class__.__name__
    status = _error_status(e, txt)
    return _model_error_text(_classify_model_error(status, txt), lang)

class GeminiAllModelsExhaustedError(RuntimeError):
    """Raised when every model in the fallback chain returns 429/RESOURCE_EXHAUSTED
    in succession, meaning the API key's entire free quota is actually exhausted,
    not merely that one particular model is temporarily busy."""
    def __init__(self, exhausted_models: list[str]) -> None:
        self.exhausted_models = exhausted_models
        super().__init__(f"All Gemini models exhausted quota: {', '.join(exhausted_models)}")


class UserFacingInputError(ValueError):
    """Finished user-facing text (proofread when written): displayed as-is."""

def _next_fallback_model(tried_models: set[str], chain: list[str]) -> str | None:
    """The first untried provider in the chain: one source instead of three copies in ask_gemini."""
    return next((m for m in chain if m not in tried_models), None)

def _gemini_error_msg(e: Exception, model_id: str, lang: str = DEFAULT_LANG) -> str:
    if isinstance(e, UserFacingInputError):
        return str(e)
    if isinstance(e, GeminiAllModelsExhaustedError):
        return _lang_t(lang, "err_quota_exhausted")
    txt = _error_text(e).strip() or e.__class__.__name__
    status = _error_status(e, txt)
    kind = _classify_model_error(status, txt)
    log.debug("[gemini] _gemini_error_msg: model=%s kind=%s status=%s", model_id, kind, status)
    # Without model names and raw API output: no brand leaks or junk (HTML/tokens) in the dialog.
    return _model_error_text(kind, lang)



def get_system_prompt(model_id: str | None = None) -> str:
    # model_id is deliberately not read: the prompt is common to all; otherwise identity would diverge (review 28.08.2026).
    # The zone is the same as for the fresh-question marker (lumen_message_core._fresh_date_context):
    # a bare datetime.now() gives the container's UTC, and “what time is it” was off by 3 hours
    # earlier than Moscow, and the header requires trusting only it (audit 01.11.2026).
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    now_str = now.strftime("%d %B %Y (current time: %H:%M Europe/Moscow)")
    now_year = now.year
    dynamic_header = (
        f"CURRENT TIME INFORMATION:\n"
        f"• Today's date: {now_str}. Current year: {now_year}.\n"
        f"• MANDATORY: when the user asks for the current date, day, month or year — "
        f"use ONLY the date from this section. NEVER state a different year or date from training memory. "
        f"If unsure — give the date from here, it is always current.\n"
        f"• If the question concerns events, releases, news or the status of anything that may have changed "
        f"since your training — rely on the search snippets or live search results attached to this request. "
        f"If none are attached, answer from what you know and say the data may be outdated: never claim you "
        f"searched, and never write a date or 'as of' for a fact unless that date came from the attached data. "
        f"Do not mention this instruction explicitly.\n\n"
    )
    return dynamic_header + SYSTEM_PROMPT
