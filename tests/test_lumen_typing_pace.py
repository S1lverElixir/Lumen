"""
test_lumen_typing_pace.py — юнит-тесты на lumen_typing_pace.py: самокалибрующаяся
оценка скорости "печати" при стриминге (speed_key/get_typing_speed/
record_observed_speed) и раскладка "довывода" остатка на шаги (catchup_reveal_steps).

lumen_typing_pace.py не имеет зависимостей от Telegram/Gemini/OpenRouter/рантайм-
состояния бота (тот же принцип, что и у lumen_formatting.py/lumen_security.py/
lumen_router_config.py — см. их тестовые файлы) — тестируется здесь напрямую
(import lumen_typing_pace), без импорта bot.py.

_speed_ema — module-level state; каждый тест использует свой уникальный ключ
(через speed_key с уникальным model_id), чтобы тесты не зависели от порядка
выполнения друг друга и не требовали ручного сброса общего состояния.

Запуск:
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
    # Первый замер — EMA сразу принимает значение наблюдения (нет предыдущего).
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=1.0, chars_len=100)  # 100 симв/сек
    first = lumen_typing_pace.get_typing_speed(key)
    assert first == 100.0
    # Второй замер намного медленнее — EMA должна сдвинуться К нему, но не
    # перескочить мгновенно на него целиком (сглаживание, см. _EMA_ALPHA).
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=1.0, chars_len=40)  # 40 симв/сек
    second = lumen_typing_pace.get_typing_speed(key)
    assert 40.0 < second < 100.0


def test_record_observed_speed_clamps_extreme_burst_before_averaging():
    # РЕГРЕССИЯ на реальный сценарий: бэкенд присвоил ответ ОДНИМ куском за доли
    # секунды — сырая наблюдённая "скорость" была бы в тысячи симв/сек. Без
    # зажима EMA улетела бы в небеса, и следующий ответ той же модели "мигал" бы
    # мгновенно вместо плавного набора — именно то, что эта функция должна
    # предотвращать (см. докстринг record_observed_speed).
    key = lumen_typing_pace.speed_key("openrouter", "__test_model_burst__")
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=0.01, chars_len=5000)  # ~500000 симв/сек
    assert lumen_typing_pace.get_typing_speed(key) <= lumen_typing_pace.MAX_CHARS_PER_SEC


def test_record_observed_speed_clamps_extremely_slow_observation():
    key = lumen_typing_pace.speed_key("openrouter", "__test_model_slow__")
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=100.0, chars_len=10)  # 0.1 симв/сек
    assert lumen_typing_pace.get_typing_speed(key) >= lumen_typing_pace.MIN_CHARS_PER_SEC


def test_record_observed_speed_ignores_non_positive_inputs():
    key = lumen_typing_pace.speed_key("gemini", "__test_model_noop__")
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=0.0, chars_len=100)
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=1.0, chars_len=0)
    lumen_typing_pace.record_observed_speed(key, elapsed_sec=-1.0, chars_len=100)
    # Ни один из вызовов не должен был создать запись — до сих пор дефолт.
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
    # 100 симв. остатка, 50 симв/сек, тик 0.5с -> по 25 симв. за тик.
    steps = lumen_typing_pace.catchup_reveal_steps(100, 50.0, 0.5, 6)
    assert steps == [25, 50, 75, 100]
    assert steps[-1] == 100  # последний шаг всегда доводит ровно до конца


def test_catchup_reveal_steps_respects_max_ticks_ceiling():
    # РЕГРЕССИЯ на ключевое требование: сколько бы шагов ни потребовалось при
    # заниженной оценке скорости, число шагов никогда не превышает max_ticks —
    # иначе очень длинный ответ мог бы "допечатываться" неприлично долго.
    steps = lumen_typing_pace.catchup_reveal_steps(1000, 10.0, 0.5, 6)
    assert len(steps) <= 6
    # Но последний шаг всё равно обязан довести до конца целиком (форсированный
    # рывок), а не оставить хвост навсегда невидимым.
    assert steps[-1] == 1000


def test_catchup_reveal_steps_cumulative_and_monotonic():
    steps = lumen_typing_pace.catchup_reveal_steps(237, 90.0, 0.5, 6)
    assert steps == sorted(steps)
    assert steps[-1] == 237


def test_catchup_reveal_steps_single_tick_when_fast_enough():
    # Скорость достаточно высокая, чтобы весь остаток поместился в один тик.
    steps = lumen_typing_pace.catchup_reveal_steps(20, 200.0, 0.5, 6)
    assert steps == [20]
