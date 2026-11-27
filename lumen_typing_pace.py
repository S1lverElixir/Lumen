"""lumen_typing_pace.py — self-calibrating “typing” pace for Telegram streaming.

Speed belongs to the backend under the slug, not the model, so it is measured directly and smoothed with EMA. The unit is characters/s. EMA is kept only in memory.
"""

from __future__ import annotations

import time

# Typing-speed bounds (characters/sec)
# Tuned to feel like live typing; the owner can change them right here.
DEFAULT_CHARS_PER_SEC = 90.0
MIN_CHARS_PER_SEC = 40.0
MAX_CHARS_PER_SEC = 260.0
# A cap on display, not measurement: 260 chars/s is the arrival rate of chunks, while
# on screen, Claude types noticeably slower, and at 260 characters per typing boundary
# catches up with the buffer in one frame (prod 11.11.2026: frames=1).
DISPLAY_MAX_CHARS_PER_SEC = 110.0

# A lower alpha makes the estimate more stable but slows adaptation when the backend changes under the same slug.
_EMA_ALPHA = 0.3

# Higher than the EMA of the whole answer: the arrival rate changes within a single stream.
_ARRIVAL_ALPHA = 0.5

_speed_ema: dict[str, float] = {}

# Do not split a small remainder into one more tick: an edit for 20 characters is the
#     same unnecessary
# round trip to Telegram without a "typing" effect. 120 was too large: a tail of 300+
# characters arrived in three jumps of 100+ (prod 11.11.2026: max_step=214).
_CATCHUP_SMOOTH_MIN_CHARS = 60

# shared per-chat edit budget (review 11.11.2026)
# Previously, the status ticker, stream painter, and finalizer each throttled themselves; combined
# this produced about 3 edits/sec in one message, causing 429s and flickering. One slot per chat,
# everyone uses it; if not taken, the frame is simply skipped and the animation does not break.
_edit_gate_ts: dict[int, float] = {}
_edit_gate_interval: dict[int, float] = {}


def arm_edit_gate(chat_id: int, min_interval_sec: float) -> None:
    """Set one interval per chat between any edit_text calls (0 disables throttling)."""
    _edit_gate_interval[chat_id] = max(0.0, float(min_interval_sec))


def clear_edit_gate(chat_id: int) -> None:
    _edit_gate_ts.pop(chat_id, None)
    _edit_gate_interval.pop(chat_id, None)


def take_edit_slot(chat_id: int, *, force: bool = False) -> bool:
    """One edit slot per chat: force marks a mandatory edit (first frame, final),
which always takes the slot; ordinary frames are skipped while the slot is occupied.
A chat without a factory (not a stream: typing indicator, action) has no throttling."""
    interval = _edit_gate_interval.get(chat_id)
    if interval is None:
        return True
    now = time.monotonic()
    if not force and now < _edit_gate_ts.get(chat_id, 0.0):
        return False
    _edit_gate_ts[chat_id] = now + interval
    return True


def speed_key(provider: str, model_id: str) -> str:
    """A single key for the (provider, model_id) pair, as in GLOBAL_QUOTA."""
    return f"{provider}:{model_id}"


def get_typing_speed(key: str) -> float:
    """Estimate for a model, or a default without measurements. The result is always within the speed bounds."""
    return max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, _speed_ema.get(key, DEFAULT_CHARS_PER_SEC)))


def record_observed_speed(key: str, elapsed_sec: float, chars_len: int) -> None:
    """Account for one completed stream; call once at the end. elapsed_sec excludes the catch-up phase, otherwise the EMA would trust its own delays. Clamp the observation before averaging, otherwise one outlier will drag the EMA."""
    if elapsed_sec <= 0 or chars_len <= 0:
        return
    observed = max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, chars_len / elapsed_sec))
    prev = _speed_ema.get(key)
    _speed_ema[key] = observed if prev is None else (_EMA_ALPHA * observed + (1 - _EMA_ALPHA) * prev)


def catchup_tick_budget(catchup_sec: float, step_sec: float) -> int:
    """How many tail-display pauses fit in the remaining budget. A step is a pause
PLUS an edit; otherwise the budget counts only sleep. No time means no
pause: the final edit shows the tail (review 11.11.2026: max(1, ...)
slept one more tick past the deadline, while STREAM_TYPING_TICK_SEC=0 caused
ZeroDivisionError and marked the entire answer \"Connection interrupted\")."""
    if step_sec <= 0 or catchup_sec <= 0:
        return 0
    return int(catchup_sec / step_sec)


def catchup_reveal_steps(remaining_len: int, chars_per_sec: float, tick_interval_sec: float, max_ticks: int) -> list[int]:
    """Drains the remainder in steps; the delay is no more than max_ticks × tick.
    No clock access: tests replace the wait with a no-op, so a monotonic-based loop would hang."""
    if remaining_len <= 0 or max_ticks <= 0:
        return []
    chars_per_tick = max(1, min(int(chars_per_sec * tick_interval_sec), _CATCHUP_SMOOTH_MIN_CHARS))
    if -(-remaining_len // chars_per_tick) <= max_ticks:
        steps: list[int] = []
        revealed = 0
        while revealed < remaining_len:
            revealed = min(remaining_len, revealed + chars_per_tick)
            steps.append(revealed)
        return steps
    # The tail does not fit the speed budget: previously, the last step dumped
    # hundreds of characters at once (review 11.11.2026). Smooth them out into even
    # in steps: N = ceil(remaining/_CATCHUP_SMOOTH_MIN_CHARS), capped at max_ticks,
    # step = remaining/N.
    ticks = min(max(1, max_ticks), -(-remaining_len // _CATCHUP_SMOOTH_MIN_CHARS))
    step = -(-remaining_len // ticks)
    return [min(remaining_len, step * i) for i in range(1, ticks + 1)]


def blend_arrival_speed(prev_ewma: float | None, piece_len: int, dt_sec: float) -> float | None:
    """Mixes a chunk into the arrival rate. A zero delta is not an observation; return the previous value."""
    if piece_len <= 0 or dt_sec <= 0:
        return prev_ewma
    inst = max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, piece_len / dt_sec))
    return inst if prev_ewma is None else _ARRIVAL_ALPHA * inst + (1 - _ARRIVAL_ALPHA) * prev_ewma


def display_speed_for(arrival_ewma: float | None, pace_key: str) -> float:
    """Display speed: use the measured arrival rate while available; otherwise use the
    model's global EMA (seeded for the first chunks). Display speed is bounded below
    by Claude-like human typing: the measured SSE arrival rate for chunks of 200+
    characters outpaces the buffer, the typing boundary catches up with the entire
    text, and frames collapse into 1-3 edits (prod 11.11.2026: frames=1 max_step=2 chars=322
    instead of smooth typing)."""
    speed = (max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, arrival_ewma))
             if arrival_ewma is not None else get_typing_speed(pace_key))
    return max(MIN_CHARS_PER_SEC, min(DISPLAY_MAX_CHARS_PER_SEC, speed))
