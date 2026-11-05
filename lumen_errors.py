"""
lumen_errors.py — разбор ошибок моделей и тексты для пользователя:
классификация rate_limit/paid/forbidden, локализованные шаблоны, промпт с датой.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from lumen_lang import DEFAULT_LANG, t as _lang_t
from system_prompt import SYSTEM_PROMPT

# Провайдеры OpenRouter отдают плейсхолдер вместо ответа: choices непустые, текста
# нет. Прод 17.09.2026 лечил только пустой choices — литерал "Empty response"
# продолжал уходить в чат (выборка бэкапа 05.11.2026: 7 таких в одном диалоге).
_EMPTY_LIKE_TEXTS = frozenset({"empty response", "no response", "n/a", "none", "no answer"})


def _is_empty_like(text: Any) -> bool:
    """Пустой ответ или системный плейсхолдер вместо него."""
    if not isinstance(text, str):
        return not text
    cleaned = text.strip().lower().rstrip(".!")
    return not cleaned or cleaned in _EMPTY_LIKE_TEXTS

log = logging.getLogger("bot")

def _error_text(e: Exception) -> str:
    return " ".join(p for p in [str(e), str(getattr(e, "message", "")), str(getattr(e, "detail", ""))] if p).strip()

# Только явные шаблоны ("HTTP 503", "status_code=429", "error 400"): голое число
# в тексте ("429 токенов") статусом не считаем — давало ложную классификацию.
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
    if status == 429 or any(tok in low for tok in ("resource_exhausted", "too many requests", "rate limit", "quota", "лимит")):
        return "rate_limit"
    if status == 402 or any(tok in low for tok in ("payment required", "paid tier", "requires paid", "billing", "кредит")):
        return "paid"
    if status in {401, 403} or any(tok in low for tok in ("unauthorized", "forbidden", "permission", "blocked")):
        return "forbidden"
    if status in {400, 404} or any(tok in low for tok in ("not found", "invalid argument", "invalid model", "unsupported", "unavailable")):
        return "unavailable"
    return "other"

# Единая таблица текстов вместо двух копий классификации: правили одну и забывали вторую.
# Тексты без имён провайдеров/моделей и без /model|/provider: для пользователя есть только Lumen (см. ИДЕНТИЧНОСТЬ).
_MODEL_ERROR_FALLBACK_MSG = _lang_t(DEFAULT_LANG, "model_err_fallback")

def _model_error_text(kind: str, lang: str = DEFAULT_LANG) -> str:
    key = {
        "rate_limit": "model_err_rate_limit",
        "paid": "model_err_paid",
        "forbidden": "model_err_forbidden",
        "unavailable": "model_err_unavailable",
    }.get(kind, "model_err_fallback")
    return _lang_t(lang, key)

def _or_error_msg(e: Exception, kind: str, lang: str = DEFAULT_LANG) -> str:
    # Без сырого текста API: там HTML/JSON и внутренности (то же в _gemini_error_msg).
    txt = _error_text(e).strip() or e.__class__.__name__
    status = _error_status(e, txt)
    return _model_error_text(_classify_model_error(status, txt), lang)

class GeminiAllModelsExhaustedError(RuntimeError):
    """Поднимается, когда 429/RESOURCE_EXHAUSTED получен подряд от всех моделей
    из цепочки фоллбека — то есть реально весь бесплатный лимит API-ключа исчерпан,
    а не просто конкретная модель временно занята."""
    def __init__(self, exhausted_models: list[str]) -> None:
        self.exhausted_models = exhausted_models
        super().__init__(f"All Gemini models exhausted quota: {', '.join(exhausted_models)}")


class UserFacingInputError(ValueError):
    """Готовый пользовательский текст (проверен при написании): показывается как есть."""

def _next_fallback_model(tried_models: set[str], chain: list[str]) -> str | None:
    """Первая непробованная из цепочки: один источник вместо трёх копий в ask_gemini."""
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
    # Без имён моделей и сырого API: утечка бренда и мусор (HTML/токены) в диалоге.
    return _model_error_text(kind, lang)



def get_system_prompt(model_id: str | None = None) -> str:
    # model_id не читается осознанно: промпт един для всех, иначе расхождение identity (ревью 28.08.2026).
    # Зона — та же, что у пометки свежих вопросов (lumen_message_core._fresh_date_context):
    # голый datetime.now() даёт UTC контейнера и «который час» отвечался на 3 часа
    # раньше Москвы, а шапка требует верить только ей (аудит 01.11.2026).
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
