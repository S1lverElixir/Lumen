"""lumen_devtrace.py — построчный JSONL-след запросов и ответов для разбора багов."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("bot")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
TAIL_HARD_CAP = 1000
_USER_CLIP = 2000
_REPLY_CLIP = 4000
_CTX_TAIL_LINES = 3
_CTX_CLIP = 500
_ERROR_CLIP = 500


def enabled() -> bool:
    return os.getenv("DEV_TRACE", "").strip().lower() in _TRUE_VALUES


def trace_path() -> Path:
    explicit = os.getenv("DEV_TRACE_PATH", "").strip()
    if explicit:
        return Path(explicit)
    # Рядом с bot.log: в тестах BOT_LOG_PATH уже ведёт во временный каталог.
    base = os.path.dirname(os.getenv("BOT_LOG_PATH", "/app/bot.log")) or "/app"
    return Path(base) / "devtrace.jsonl"


def max_bytes() -> int:
    try:
        value = int(os.getenv("DEV_TRACE_MAX_BYTES", str(_DEFAULT_MAX_BYTES)).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BYTES
    # Мусор и нули в env давали бы ротацию на каждой строке.
    return value if value >= 1024 else _DEFAULT_MAX_BYTES


def _clip(text: str, limit: int) -> str:
    if not isinstance(text, str):
        text = str(text)
    return text if len(text) <= limit else text[:limit]


def _current_secrets() -> tuple[str, ...]:
    # Ленивый импорт: bot импортирует этот модуль, топ-уровень закольцует.
    try:
        import bot as _bot
        getter = getattr(_bot, "_current_log_secrets", None)
        if callable(getter):
            return tuple(getter())
    except Exception:
        pass
    return ()


def _scrub(text: str) -> str:
    for secret in _current_secrets():
        if secret:
            text = text.replace(secret, "<REDACTED>")
    return text


def _append(kind: str, payload: dict) -> bool:
    if not enabled():
        return False
    try:
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        payload["kind"] = kind
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= max_bytes():
            # Один бэкап вместо series: диску на Space есть предел, старьё не копим.
            backup = Path(str(path) + ".1")
            try:
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        log.warning("[devtrace] Failed to append trace entry: %s", exc.__class__.__name__, exc_info=True)
        return False


def _route_names(route: list | None) -> list[str]:
    names = []
    for item in route or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            names.append(f"{item[0]}:{item[1]}")
        else:
            names.append(str(item))
    return names


def record_passive(chat_id: int, text: str, *, is_private: bool = False, mentioned: bool = False) -> bool:
    # Сначала вычистка, потом обрезка: секрет в хвосте иначе срезался бы молча.
    return _append("passive", {
        "chat_id": chat_id,
        "text": _clip(_scrub(text or ""), _USER_CLIP),
        "private": bool(is_private),
        "mentioned": bool(mentioned),
    })


def record_blocked(chat_id: int, *, reason: str, prompt_len: int = 0) -> bool:
    # Текст не пишем, только длину: тот же запрет, что у injection-варнинга в логе.
    return _append("blocked", {"chat_id": chat_id, "reason": reason, "prompt_len": int(prompt_len)})


def record_answer(
    chat_id: int,
    *,
    user_text: str,
    route: list | None = None,
    provider: str = "",
    model: str = "",
    latency_ms: int = 0,
    reply: str = "",
    ctx_tail: list | None = None,
    flags: dict | None = None,
) -> bool:
    tail = [_clip(_scrub(line), _CTX_CLIP) for line in (ctx_tail or []) if isinstance(line, str)]
    return _append("answer", {
        "chat_id": chat_id,
        "user": _clip(_scrub(user_text or ""), _USER_CLIP),
        "route": _route_names(route),
        "provider": provider or "",
        "model": model or "",
        "latency_ms": int(latency_ms),
        "ctx_tail": tail[-_CTX_TAIL_LINES:],
        "flags": dict(flags or {}),
        "reply": _clip(_scrub(reply or ""), _REPLY_CLIP),
    })


def record_failure(
    chat_id: int, *, user_text: str, route: list | None = None, error: str = "", latency_ms: int = 0,
) -> bool:
    return _append("failure", {
        "chat_id": chat_id,
        "user": _clip(_scrub(user_text or ""), _USER_CLIP),
        "route": _route_names(route),
        "error": _clip(_scrub(error or ""), _ERROR_CLIP),
        "latency_ms": int(latency_ms),
    })


def read_tail(n: int = 200) -> list[dict]:
    try:
        count = max(1, min(int(n), TAIL_HARD_CAP))
    except (TypeError, ValueError):
        count = 200
    try:
        path = trace_path()
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # С конца до N валидных: битая строка в хвосте иначе съедала бы лимит.
        out = []
        for line in reversed(lines):
            if len(out) >= count:
                break
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out[::-1]
    except OSError:
        return []
