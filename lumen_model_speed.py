"""
lumen_model_speed.py — самокалибрующаяся оценка задержек моделей.

Прод 17.09.2026: голова цепочки тормозила 96с, а роутер ставил её первой. Поэтому замер по факту + EMA, новичок с нейтрального приора. EMA только в памяти.
"""

from __future__ import annotations

# Ключ provider:model_id — из lumen_typing_pace, чтобы не плодить два форматтера.
from lumen_typing_pace import speed_key

# Тот же компромисс стабильности/подстройки, что в typing_pace.
_EMA_ALPHA = 0.3

# Приор нейтрален: новичок без замеров не взлетает в голову и не падает в хвост.
_DEFAULT_TTF_SEC = 10.0
_DEFAULT_TOTAL_SEC = 25.0

# Границы-зажимы ДО усреднения (тот же приём, что clamp в record_observed_speed):
# один выброс не должен утащить EMA в небеса или в пол.
_MIN_TTF_SEC = 1.0
_MAX_TTF_SEC = 90.0
_MIN_TOTAL_SEC = 3.0
_MAX_TOTAL_SEC = 120.0

# Запас на разовое зависание первого куска, а не вечное ожидание.
_TTF_ABANDON_MULT = 2.5

_latency_ema: dict[str, tuple[float, float]] = {}


def expected_ttf_sec(key: str) -> float:
    """Ожидаемое время до первого куска — EMA или приор для новой модели."""
    ttf = _latency_ema.get(key, (_DEFAULT_TTF_SEC, _DEFAULT_TOTAL_SEC))[0]
    return max(_MIN_TTF_SEC, min(_MAX_TTF_SEC, ttf))


def expected_total_sec(key: str) -> float:
    """Ожидаемое полное время ответа — EMA или приор для новой модели."""
    total = _latency_ema.get(key, (_DEFAULT_TTF_SEC, _DEFAULT_TOTAL_SEC))[1]
    return max(_MIN_TOTAL_SEC, min(_MAX_TOTAL_SEC, total))


def record_response(key: str, *, total_sec: float, ttf_sec: float | None = None) -> None:
    """Учесть один УСПЕШНЫЙ ответ. Без ttf_sec подставляется total_sec, неположительные замеры игнорируются."""
    if total_sec <= 0:
        return
    if ttf_sec is None or ttf_sec <= 0:
        ttf_sec = total_sec
    obs_ttf = max(_MIN_TTF_SEC, min(_MAX_TTF_SEC, ttf_sec))
    obs_total = max(_MIN_TOTAL_SEC, min(_MAX_TOTAL_SEC, total_sec))
    prev = _latency_ema.get(key)
    if prev is None:
        _latency_ema[key] = (obs_ttf, obs_total)
    else:
        _latency_ema[key] = (
            _EMA_ALPHA * obs_ttf + (1 - _EMA_ALPHA) * prev[0],
            _EMA_ALPHA * obs_total + (1 - _EMA_ALPHA) * prev[1],
        )


def first_chunk_limit_sec(key: str, floor_sec: float) -> float:
    """min — floor от холодного старта, max — EMA_ttf × множитель от разового зависания."""
    return max(floor_sec, expected_ttf_sec(key) * _TTF_ABANDON_MULT)


def reorder_route(route: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Переупорядочить маршрут по задержкам, не ломая архитектуру: блоки провайдеров на месте, внутри блока — по ожидаемому времени. Сортировка стабильная, новички сохраняют кураторский порядок."""
    blocks: list[list[tuple[str, str]]] = []
    block_index: dict[str, int] = {}
    for item in route:
        provider = item[0]
        if provider not in block_index:
            block_index[provider] = len(blocks)
            blocks.append([])
        blocks[block_index[provider]].append(item)
    ordered: list[tuple[str, str]] = []
    for block in blocks:
        ordered.extend(sorted(block, key=lambda item: expected_total_sec(speed_key(item[0], item[1]))))
    return ordered
