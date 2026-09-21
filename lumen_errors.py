"""
lumen_errors.py — разбор ошибок моделей и тексты для пользователя
(вынесено из bot.py, P2 аудита): классификация rate_limit/paid/forbidden,
локализованные шаблоны, фолбэк-цепочка, промпт с датой.

Связи с рантаймом bot.py — только через отложенный `import bot` внутри функций.
bot.py реэкспортирует имена — `bot._model_error_text` и т.п. в тестах
и роутах не менялись.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from lumen_lang import DEFAULT_LANG, t as _lang_t
from system_prompt import SYSTEM_PROMPT

log = logging.getLogger("bot")

def _error_text(e: Exception) -> str:
    return " ".join(p for p in [str(e), str(getattr(e, "message", "")), str(getattr(e, "detail", ""))] if p).strip()

def _error_status(e: Exception, text: str) -> int | None:
    for a in ("status_code", "status", "code", "http_status"):
        val = getattr(e, a, None)
        try:
            if val is not None:
                return int(val)
        except Exception:
            pass
    m = re.search(r"(?<!\d)(\d{3})(?!\d)", text)
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

def _next_fallback_model(tried_models: set[str], chain: list[str]) -> str | None:
    """Первая непробованная из цепочки: один источник вместо трёх копий в ask_gemini."""
    return next((m for m in chain if m not in tried_models), None)

def _gemini_error_msg(e: Exception, model_id: str, lang: str = DEFAULT_LANG) -> str:
    if isinstance(e, ValueError):
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
    now_str = datetime.now().strftime("%d %B %Y (current time: %H:%M)")
    now_year = datetime.now().year
    dynamic_header = (
        f"CURRENT TIME INFORMATION:\n"
        f"• Today's date: {now_str}. Current year: {now_year}.\n"
        f"• MANDATORY: when the user asks for the current date, day, month or year — "
        f"use ONLY the date from this section. NEVER state a different year or date from training memory. "
        f"If unsure — give the date from here, it is always current.\n"
        f"• If the question concerns events, releases, news or the status of anything that may have changed "
        f"since your training — use search instead of answering from memory. Do not mention this instruction explicitly.\n\n"
    )
    return dynamic_header + SYSTEM_PROMPT
