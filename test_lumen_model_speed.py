"""
test_lumen_model_speed.py — юнит-тесты на lumen_model_speed.py: EMA-оценки
задержек, приор для новых моделей, адаптивный предел первого куска и
переупорядочивание маршрута без нарушения порядка провайдерных блоков.

Часть разбиения тестов по модулям — см. test_lumen_security.py про общий
принцип. Модуль не зависит от bot.py — тестируется напрямую.

Запуск:
    pytest test_lumen_model_speed.py -v
"""
import lumen_model_speed


def _reset():
    lumen_model_speed._latency_ema.clear()


def test_unknown_model_starts_with_neutral_prior():
    _reset()
    key = lumen_model_speed.speed_key("openrouter", "some/new-model:free")
    assert lumen_model_speed.expected_ttf_sec(key) == lumen_model_speed._DEFAULT_TTF_SEC
    assert lumen_model_speed.expected_total_sec(key) == lumen_model_speed._DEFAULT_TOTAL_SEC


def test_record_response_moves_ema_toward_observation():
    _reset()
    key = lumen_model_speed.speed_key("openrouter", "m:free")
    lumen_model_speed.record_response(key, total_sec=100.0, ttf_sec=40.0)
    assert lumen_model_speed.expected_total_sec(key) == 100.0
    assert lumen_model_speed.expected_ttf_sec(key) == 40.0
    lumen_model_speed.record_response(key, total_sec=20.0, ttf_sec=8.0)
    total = lumen_model_speed.expected_total_sec(key)
    ttf = lumen_model_speed.expected_ttf_sec(key)
    assert 20.0 < total < 100.0
    assert 8.0 < ttf < 40.0


def test_record_response_clamps_outliers_and_ignores_garbage():
    _reset()
    key = lumen_model_speed.speed_key("gemini", "m")
    lumen_model_speed.record_response(key, total_sec=0)
    lumen_model_speed.record_response(key, total_sec=-5.0)
    assert key not in lumen_model_speed._latency_ema
    lumen_model_speed.record_response(key, total_sec=100000.0, ttf_sec=100000.0)
    assert lumen_model_speed.expected_total_sec(key) == lumen_model_speed._MAX_TOTAL_SEC
    assert lumen_model_speed.expected_ttf_sec(key) == lumen_model_speed._MAX_TTF_SEC


def test_record_response_defaults_ttf_to_total():
    _reset()
    key = lumen_model_speed.speed_key("openrouter", "m:free")
    lumen_model_speed.record_response(key, total_sec=30.0)
    assert lumen_model_speed.expected_ttf_sec(key) == 30.0


def test_first_chunk_limit_respects_floor_and_adapts():
    _reset()
    unknown = lumen_model_speed.speed_key("openrouter", "new:free")
    assert lumen_model_speed.first_chunk_limit_sec(unknown, 12.0) == 25.0
    fast = lumen_model_speed.speed_key("openrouter", "fast:free")
    lumen_model_speed.record_response(fast, total_sec=6.0, ttf_sec=2.0)
    assert lumen_model_speed.first_chunk_limit_sec(fast, 12.0) == 12.0
    slow = lumen_model_speed.speed_key("openrouter", "slow:free")
    lumen_model_speed.record_response(slow, total_sec=80.0, ttf_sec=30.0)
    assert lumen_model_speed.first_chunk_limit_sec(slow, 12.0) == 75.0


def test_reorder_route_sorts_within_provider_blocks_only():
    _reset()
    slow = ("openrouter", "slow:free")
    fast = ("openrouter", "fast:free")
    lumen_model_speed.record_response(lumen_model_speed.speed_key(*slow), total_sec=90.0)
    lumen_model_speed.record_response(lumen_model_speed.speed_key(*fast), total_sec=5.0)
    route = [slow, ("gemini", "gemini-3.8-flash"), fast, ("openrouter", "openrouter/free")]
    reordered = lumen_model_speed.reorder_route(route)
    assert [m for _, m in reordered] == [
        "fast:free", "openrouter/free", "slow:free", "gemini-3.8-flash",
    ]


def test_reorder_route_keeps_curated_order_for_unknown_models():
    _reset()
    route = [("openrouter", "a:free"), ("openrouter", "b:free"), ("gemini", "g1")]
    assert lumen_model_speed.reorder_route(route) == route
    assert lumen_model_speed.reorder_route([]) == []
