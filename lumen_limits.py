"""lumen_limits.py. Sliding-window rate limiting and a queue of clarification buttons.
Split out from bot.py (P2 audit). Storage is in process memory, with no Telegram or LLM.
bot.py re-exports the names, so tests using bot.X did not change.
"""
from __future__ import annotations

import time
from typing import Any

# Numbers go through bot._env_number with a deferred import.
# A typo in env does not crash startup (audit 26.09.2026).
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
    """Cleanup once an hour. Empty keys accumulated forever and caused a slow leak."""
    now = time.time()
    stale = [uid for uid, ts in user_rate_limits.items() if not ts or now - ts[-1] > 3600]
    for uid in stale:
        user_rate_limits.pop(uid, None)


def _check_and_register_rate_limit(user_id: int | None) -> bool:
    """A 5/30 s window. On refusal, no marker is written, or the refusal would extend itself."""
    if not user_id:
        return False
    now = time.time()
    if user_id not in user_rate_limits and len(user_rate_limits) >= MAX_RATE_LIMIT_KEYS:
        _cleanup_rate_limit_dict()
        # Cleanup removes only stale entries; when fresh IDs are flooding in, a hard purge is needed (audit).
        # Trim to 90 percent of the cap so the dictionary is not scanned for every new ID.
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
# Memory only. After restart the buttons expire; this is expected.
_pending_picks: dict[str, dict[str, Any]] = {}


def _purge_expired_picks(now: float | None = None) -> None:
    """Remove stale entries when creating a new one: no separate loop was introduced."""
    now = time.monotonic() if now is None else now
    for token in [t for t, rec in _pending_picks.items() if rec["expires"] <= now]:
        del _pending_picks[token]


def _enforce_pending_picks_cap(pending_add: int = 0) -> None:
    """Memory cap for clarification buttons: above the limit, remove those
closest to expiry (AUD-G-001). pending_add accounts for the new token
that will be inserted immediately afterward (otherwise the maximum would be MAX+1)."""
    overflow = len(_pending_picks) + pending_add - MAX_PENDING_PICKS
    if overflow <= 0:
        return
    oldest = sorted(_pending_picks, key=lambda t: _pending_picks[t]["expires"])[:overflow]
    for token in oldest:
        del _pending_picks[token]
