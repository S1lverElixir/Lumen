"""
lumen_limits.py — скользящее окно rate limit и очередь кнопок-уточнений.

Вынесено из bot.py (P2 аудита): обе структуры — словари в памяти процесса
с часовой чисткой/потолками, без зависимости от Telegram/LLM, поэтому живут
отдельно. bot.py реэкспортирует имена — `bot.X` в тестах не менялся.
"""
from __future__ import annotations

import os
import time
from typing import Any

# Простой трекер для rate limiting
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SEC = float(os.getenv("RATE_LIMIT_WINDOW_SEC", "30"))
MAX_RATE_LIMIT_KEYS = int(os.getenv("MAX_RATE_LIMIT_KEYS", "20000"))
user_rate_limits: dict[int, list[float]] = {}


def _cleanup_rate_limit_dict() -> None:
    """Чистка раз в час: скользящее окно оставляло пустые ключи навсегда — медленная утечка."""
    now = time.time()
    stale = [uid for uid, ts in user_rate_limits.items() if not ts or now - ts[-1] > 3600]
    for uid in stale:
        user_rate_limits.pop(uid, None)


def _check_and_register_rate_limit(user_id: int | None) -> bool:
    """Окно 5/30с; при отказе метка не пишется — иначе наказание продлевалось бы само."""
    if not user_id:
        return False
    now = time.time()
    if user_id not in user_rate_limits and len(user_rate_limits) >= MAX_RATE_LIMIT_KEYS:
        _cleanup_rate_limit_dict()
        # Чистка сносит только протухших: при флуде свежими ID словарь рос бы без потолка.
        # Добиваем жёстко — самых давно молчавших (найдено внешним аудитом). Сносим с запасом
        # (до 90% потолка), чтобы не сканировать весь словарь на каждый новый ID (ревью ветки).
        if len(user_rate_limits) >= MAX_RATE_LIMIT_KEYS:
            ordered = sorted(
                user_rate_limits,
                key=lambda uid: user_rate_limits[uid][-1] if user_rate_limits[uid] else 0,
            )
            drop = len(ordered) - int(MAX_RATE_LIMIT_KEYS * 0.9) + 1
            for uid in ordered[:drop]:
                user_rate_limits.pop(uid, None)
    timestamps = user_rate_limits.setdefault(user_id, [])
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW_SEC:
        timestamps.pop(0)
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    timestamps.append(now)
    return False


PICK_TTL_SEC = float(os.getenv("PICK_TTL_SEC", "300"))
MAX_PENDING_PICKS = int(os.getenv("MAX_PENDING_PICKS", "500"))
# Только в памяти: после рестарта кнопки честно протухают, это штатно.
_pending_picks: dict[str, dict[str, Any]] = {}


def _purge_expired_picks(now: float | None = None) -> None:
    """Чистка протухших при создании новой: отдельный цикл не заводили."""
    now = time.monotonic() if now is None else now
    for token in [t for t, rec in _pending_picks.items() if rec["expires"] <= now]:
        del _pending_picks[token]


def _enforce_pending_picks_cap() -> None:
    """Потолок памяти на кнопки-уточнения: сверх лимита сносим самые
    близкие к протуханию (AUD-G-001)."""
    overflow = len(_pending_picks) - MAX_PENDING_PICKS
    if overflow <= 0:
        return
    oldest = sorted(_pending_picks, key=lambda t: _pending_picks[t]["expires"])[:overflow]
    for token in oldest:
        del _pending_picks[token]
