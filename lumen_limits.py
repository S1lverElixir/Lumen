"""
lumen_limits.py. Скользящее окно rate limit и очередь кнопок уточнений.
Выделено из bot.py (P2 аудита). Хранилища в памяти процесса, без Telegram и LLM.
bot.py реэкспортирует имена, тесты через bot.X не менялись.
"""
from __future__ import annotations

import time
from typing import Any

# Числа через bot._env_number с отложенным импортом.
# Опечатка в env не роняет старт (аудит 26.09.2026).
def _env_number(name: str, default: float | int, *, cast: type = float, min_value: float | None = None) -> float | int:
    try:
        import bot
        return bot._env_number(name, default, cast=cast, min_value=min_value)
    except Exception:
        return cast(default)

RATE_LIMIT_MAX_REQUESTS = _env_number("RATE_LIMIT_MAX_REQUESTS", 5, cast=int, min_value=1)
RATE_LIMIT_WINDOW_SEC = _env_number("RATE_LIMIT_WINDOW_SEC", 30, min_value=1)
MAX_RATE_LIMIT_KEYS = _env_number("MAX_RATE_LIMIT_KEYS", 20000, cast=int, min_value=100)
user_rate_limits: dict[int, list[float]] = {}


def _cleanup_rate_limit_dict() -> None:
    """Чистка раз в час. Пустые ключи копились навсегда и давали медленную утечку."""
    now = time.time()
    stale = [uid for uid, ts in user_rate_limits.items() if not ts or now - ts[-1] > 3600]
    for uid in stale:
        user_rate_limits.pop(uid, None)


def _check_and_register_rate_limit(user_id: int | None) -> bool:
    """Окно 5/30с. При отказе метка не пишется, иначе отказ продлевал бы сам себя."""
    if not user_id:
        return False
    now = time.time()
    if user_id not in user_rate_limits and len(user_rate_limits) >= MAX_RATE_LIMIT_KEYS:
        _cleanup_rate_limit_dict()
        # Чистка убирает только протухших, при флуде свежими ID нужен жесткий снос (аудит).
        # Сносим до 90 процентов потолка, чтобы не сканировать словарь на каждый новый ID.
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


PICK_TTL_SEC = _env_number("PICK_TTL_SEC", 300, min_value=1)
MAX_PENDING_PICKS = _env_number("MAX_PENDING_PICKS", 500, cast=int, min_value=1)
# Только память. После рестарта кнопки протухают, это штатно.
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
