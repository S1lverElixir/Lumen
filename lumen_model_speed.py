"""lumen_model_speed.py — self-calibrating model latency estimation.

Production 17.09.2026: the chain head stalled for 96s, while the router put it first. Therefore: actual measurement + EMA, with a neutral prior for newcomers. EMA is kept only in memory.
"""

from __future__ import annotations

# Key provider:model_id, from lumen_typing_pace, to avoid creating a second
# formatter.
from lumen_typing_pace import speed_key

# The same stability/adaptation tradeoff as in typing_pace.
_EMA_ALPHA = 0.3

# Priority is neutral: a newcomer without measurements neither jumps to the head nor
#     falls to the tail.
_DEFAULT_TTF_SEC = 10.0
_DEFAULT_TOTAL_SEC = 25.0

# Clamp bounds BEFORE averaging (the same technique as clamp in
# record_observed_speed):
# a single outlier must not drag the EMA skyward or to the floor.
_MIN_TTF_SEC = 1.0
_MAX_TTF_SEC = 90.0
_MIN_TOTAL_SEC = 3.0
_MAX_TOTAL_SEC = 120.0

# Headroom for a one-time first-chunk hang, not an infinite wait.
_TTF_ABANDON_MULT = 2.5

_latency_ema: dict[str, tuple[float, float]] = {}


def expected_ttf_sec(key: str) -> float:
    """Expected time to the first chunk — EMA or a prior for a new model."""
    ttf = _latency_ema.get(key, (_DEFAULT_TTF_SEC, _DEFAULT_TOTAL_SEC))[0]
    return max(_MIN_TTF_SEC, min(_MAX_TTF_SEC, ttf))


def expected_total_sec(key: str) -> float:
    """Expected total response time: an EMA or a prior for a new model."""
    total = _latency_ema.get(key, (_DEFAULT_TTF_SEC, _DEFAULT_TOTAL_SEC))[1]
    return max(_MIN_TOTAL_SEC, min(_MAX_TOTAL_SEC, total))


def record_response(key: str, *, total_sec: float, ttf_sec: float | None = None) -> None:
    """Account for one SUCCESSFUL response. Without ttf_sec (a non-streaming response never saw a first chunk), leave ttf-EMA unchanged: substituting total inflated first_chunk_limit and kept a stuck head alive longer. Non-positive measurements are ignored."""
    if total_sec <= 0:
        return
    prev = _latency_ema.get(key)
    if ttf_sec is None or ttf_sec <= 0:
        obs_ttf = prev[0] if prev is not None else _DEFAULT_TTF_SEC
    else:
        obs_ttf = max(_MIN_TTF_SEC, min(_MAX_TTF_SEC, ttf_sec))
    obs_total = max(_MIN_TOTAL_SEC, min(_MAX_TOTAL_SEC, total_sec))
    if prev is None:
        _latency_ema[key] = (obs_ttf, obs_total)
    else:
        _latency_ema[key] = (
            _EMA_ALPHA * obs_ttf + (1 - _EMA_ALPHA) * prev[0],
            _EMA_ALPHA * obs_total + (1 - _EMA_ALPHA) * prev[1],
        )


def first_chunk_limit_sec(key: str, floor_sec: float) -> float:
    """min — a floor for cold start, max — EMA_ttf × a multiplier for a one-off hang."""
    return max(floor_sec, expected_ttf_sec(key) * _TTF_ABANDON_MULT)


def reorder_route(
    route: list[tuple[str, str]], group_key=None,
) -> list[tuple[str, str]]:
    """Reorders models by latency within a contiguous block of one provider.

    Merging blocks by provider had moved the Gemini fallback chain ahead of Groq
    (audit 27.10.2026); block order is part of quota protection, so leave it
    unchanged. group_key defines the capability boundary: sorting must not move
    a model across it (without search grounding, the head of the freshness route
    cannot answer with search; review 17.11.2026)."""
    def _sort_key(item: tuple[str, str]) -> float:
        return expected_total_sec(speed_key(item[0], item[1]))

    ordered: list[tuple[str, str]] = []
    run: list[tuple[str, str]] = []

    def _emit(items: list[tuple[str, str]]) -> None:
        segment: list[tuple[str, str]] = []
        for item in items:
            if group_key is not None and segment and group_key(item) != group_key(segment[-1]):
                ordered.extend(sorted(segment, key=_sort_key))
                segment = []
            segment.append(item)
        if segment:
            ordered.extend(sorted(segment, key=_sort_key))

    for item in route:
        if run and item[0] != run[-1][0]:
            _emit(run)
            run = []
        run.append(item)
    if run:
        _emit(run)
    return ordered
