"""Unit tests for lumen_typing_pace.py: self-calibrating \"typing\" speed estimation during streaming (speed_key/get_typing_speed/record_observed_speed) and distribution of catch-up output into steps (catchup_reveal_steps).

lumen_typing_pace.py has no dependencies on Telegram/Gemini/OpenRouter or bot runtime state (the same principle as lumen_formatting.py/lumen_security.py/lumen_router_config.py; see their test files), so it is tested directly (import lumen_typing_pace) without importing bot.py.

_speed_ema is module-level state; each test uses a unique key (via speed_key with a unique model_id) so tests do not depend on execution order or require manually resetting shared state.

Run:
    pytest test_lumen_typing_pace.py -v
"""
import lumen_typing_pace


# ─────────────────────────── speed_key ───────────────────────────

def test_speed_key_combines_provider_and_model():
    assert lumen_typing_pace.speed_key("gemini", "gemini-3.6-flash") == "gemini:gemini-3.6-flash"
    assert lumen_typing_pace.speed_key("openrouter", "nvidia/nemotron-nano-9b-v2:free") == "openrouter:nvidia/nemotron-nano-9b-v2:free"


# ─────────────────────────── get_typing_speed / record_observed_speed ───────────────────────────

def test_get_typing_speed_returns_default_when_no_samples_yet():
    key = lumen_typing_pace.speed_key("gemini", "__test_never_seen_model__")
    assert lumen_typing_pace.get_typing_speed(key) == lumen_typing_pace.DEFAULT_CHARS_PER_SEC


def test_record_observed_speed_updates_ema_towards_observed_value():
    key = lumen_typing_pace.speed_key("openrouter", "__test_model_ema__")
    # For the first measurement, EMA immediately adopts the observed value (there is no previous one).
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=1.0, chars_len=100)  # 100 chars/sec
    first = lumen_typing_pace.get_typing_speed(key)
    assert first == 100.0
    # The second measurement is much slower — the EMA must move TOWARD it, but not
    # jump to it all at once (smoothing, see _EMA_ALPHA).
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=1.0, chars_len=40)  # 40 characters/sec
    second = lumen_typing_pace.get_typing_speed(key)
    assert 40.0 < second < 100.0


def test_record_observed_speed_clamps_extreme_burst_before_averaging():
    # REGRESSION for a real scenario: the backend delivered the answer in ONE chunk within a fraction
    # seconds — the raw observed “speed” would be thousands of characters/sec. Without
    # clamp, the EMA would soar, and the next response from the same model would "flicker"
    # instantly instead of smooth typing — exactly what this function should
    # prevent (see the record_observed_speed docstring).
    key = lumen_typing_pace.speed_key("openrouter", "__test_model_burst__")
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=0.01, chars_len=5000)  # ~500000 chars/sec
    assert lumen_typing_pace.get_typing_speed(key) <= lumen_typing_pace.MAX_CHARS_PER_SEC


def test_record_observed_speed_clamps_extremely_slow_observation():
    key = lumen_typing_pace.speed_key("openrouter", "__test_model_slow__")
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=100.0, chars_len=10)  # 0.1 characters/sec
    assert lumen_typing_pace.get_typing_speed(key) >= lumen_typing_pace.MIN_CHARS_PER_SEC


def test_record_observed_speed_ignores_non_positive_inputs():
    key = lumen_typing_pace.speed_key("gemini", "__test_model_noop__")
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=0.0, chars_len=100)
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=1.0, chars_len=0)
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=-1.0, chars_len=100)
    # None of the calls should have created a record; that is still the default.
    assert lumen_typing_pace.get_typing_speed(key) == lumen_typing_pace.DEFAULT_CHARS_PER_SEC


def test_get_typing_speed_different_models_are_independent():
    key_fast = lumen_typing_pace.speed_key("gemini", "__test_fast_model__")
    key_slow = lumen_typing_pace.speed_key("openrouter", "__test_slow_model__")
    lumen_typing_pace.record_observed_speed(key_fast, elapsed_sec=1.0, chars_len=200)
    lumen_typing_pace.record_observed_speed(key_slow, elapsed_sec=1.0, chars_len=50)
    assert lumen_typing_pace.get_typing_speed(key_fast) > lumen_typing_pace.get_typing_speed(key_slow)


# ─────────────────────────── catchup_reveal_steps ───────────────────────────

def test_catchup_reveal_steps_empty_when_nothing_remaining():
    assert lumen_typing_pace.catchup_reveal_steps(0, 100.0, 0.5, 6) == []
    assert lumen_typing_pace.catchup_reveal_steps(-5, 100.0, 0.5, 6) == []


def test_catchup_reveal_steps_normal_case_reaches_exact_total():
    # 100 chars remaining, 50 chars/sec, 0.5s tick -> 25 chars per tick.
    steps = lumen_typing_pace.catchup_reveal_steps(100, 50.0, 0.5, 6)
    assert steps == [25, 50, 75, 100]
    assert steps[-1] == 100  # the last step always reaches exactly the end


def test_catchup_reveal_steps_respects_max_ticks_ceiling():
    # REGRESSION in a key requirement: however many steps are needed for
    # underestimated speed, the number of steps never exceeds max_ticks —
    # otherwise a very long answer could "finish typing" for an absurdly long time.
    steps = lumen_typing_pace.catchup_reveal_steps(1000, 10.0, 0.5, 6)
    assert len(steps) <= 6
    # But the final step must still reach the end in full (forced
    # jerk), rather than leaving the tail invisible forever.
    assert steps[-1] == 1000


def test_catchup_reveal_steps_cumulative_and_monotonic():
    steps = lumen_typing_pace.catchup_reveal_steps(237, 90.0, 0.5, 6)
    assert steps == sorted(steps)
    assert steps[-1] == 237


def test_catchup_reveal_steps_single_tick_when_fast_enough():
    # The speed is high enough for the entire remainder to fit in one tick.
    steps = lumen_typing_pace.catchup_reveal_steps(20, 200.0, 0.5, 6)
    assert steps == [20]


# ─────────────────────────── blend_arrival_speed / display_speed_for ───────────────────────────

def test_blend_arrival_speed_first_sample_sets_estimate():
    # First chunk: there is no estimate yet — the instantaneous rate becomes it (within bounds).
    assert lumen_typing_pace.blend_arrival_speed(None, 100, 1.0) == 100.0
    # Outliers are clamped to the bounds instead of skewing the estimate.
    assert lumen_typing_pace.blend_arrival_speed(None, 10000, 0.01) == lumen_typing_pace.MAX_CHARS_PER_SEC
    assert lumen_typing_pace.blend_arrival_speed(None, 1, 100.0) == lumen_typing_pace.MIN_CHARS_PER_SEC


def test_blend_arrival_speed_ignores_non_positive_inputs():
    assert lumen_typing_pace.blend_arrival_speed(90.0, 0, 1.0) == 90.0
    assert lumen_typing_pace.blend_arrival_speed(90.0, 50, 0.0) == 90.0
    assert lumen_typing_pace.blend_arrival_speed(None, 50, 0.0) is None


def test_blend_arrival_speed_moves_toward_new_sample():
    # 100 chars/sec became 200 chars/sec: the estimate moved up but did not jump all the way.
    updated = lumen_typing_pace.blend_arrival_speed(100.0, 200, 1.0)
    assert 100.0 < updated < 200.0


def test_display_speed_for_prefers_measured_arrival():
    key = lumen_typing_pace.speed_key("gemini", "__test_display_model__")
    # Below the display cap, the measured pace is the display speed.
    assert lumen_typing_pace.display_speed_for(80.0, key) == 80.0
    # Without arrival timing — the model's global EMA (seed for the first chunks).
    assert lumen_typing_pace.display_speed_for(None, key) == min(
        lumen_typing_pace.DISPLAY_MAX_CHARS_PER_SEC, lumen_typing_pace.get_typing_speed(key))


def test_display_speed_never_exceeds_human_pace():
    # REGRESSION in production 11.11.2026 (frames=1 max_step=2 chars=322): SSE arrives
    # in chunks of 200+ characters, measured pace 260 chars/s, and the display boundary
    # caught up with the buffer in a single frame — the user saw text replacement rather than typing.
    key = lumen_typing_pace.speed_key("munito", "__test_display_model__")
    assert lumen_typing_pace.display_speed_for(260.0, key) == lumen_typing_pace.DISPLAY_MAX_CHARS_PER_SEC
    assert lumen_typing_pace.display_speed_for(10_000.0, key) == lumen_typing_pace.DISPLAY_MAX_CHARS_PER_SEC


def test_catchup_splits_long_tail_into_small_even_steps():
    # Same production case on the output remainder: 322 characters left with an inflated
    # at the estimated rate, it must arrive in steps of <=60, not three jumps of 108.
    steps = lumen_typing_pace.catchup_reveal_steps(322, 260.0, 0.35, 8)
    assert steps[-1] == 322
    sizes = [steps[0]] + [steps[i] - steps[i - 1] for i in range(1, len(steps))]
    assert max(sizes) <= lumen_typing_pace._CATCHUP_SMOOTH_MIN_CHARS
    assert len(steps) >= 5
