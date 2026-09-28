"""
lumen_typing_pace.py — самокалибрующийся темп "печати" при стриминге в Telegram.

Скорость — свойство бэкенда под слагом, а не модели, поэтому замер по факту + EMA. Единица — символы/с. EMA только в памяти.
"""

from __future__ import annotations

# ── границы скорости печати (символов/сек) ──
# Подобраны под ощущение живого набора, владелец может менять прямо здесь.
DEFAULT_CHARS_PER_SEC = 90.0
MIN_CHARS_PER_SEC = 40.0
MAX_CHARS_PER_SEC = 260.0

# Меньше альфа — стабильнее оценка, но медленнее подстройка под смену бэкенда под тем же слагом.
_EMA_ALPHA = 0.3

# Выше, чем у EMA всего ответа: темп прихода меняется внутри одного стрима.
_ARRIVAL_ALPHA = 0.5

_speed_ema: dict[str, float] = {}


def speed_key(provider: str, model_id: str) -> str:
    """Единый ключ пары (provider, model_id), как в GLOBAL_QUOTA."""
    return f"{provider}:{model_id}"


def get_typing_speed(key: str) -> float:
    """Оценка для модели или дефолт без замеров. Результат всегда в границах скорости."""
    return max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, _speed_ema.get(key, DEFAULT_CHARS_PER_SEC)))


def record_observed_speed(key: str, elapsed_sec: float, chars_len: int) -> None:
    """Учесть один завершённый стрим, вызывать раз в конце. elapsed_sec — без фазы довывода, иначе EMA поверит своим же задержкам. Наблюдение зажимается до усреднения, иначе один выброс утащит EMA."""
    if elapsed_sec <= 0 or chars_len <= 0:
        return
    observed = max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, chars_len / elapsed_sec))
    prev = _speed_ema.get(key)
    _speed_ema[key] = observed if prev is None else (_EMA_ALPHA * observed + (1 - _EMA_ALPHA) * prev)


def catchup_reveal_steps(remaining_len: int, chars_per_sec: float, tick_interval_sec: float, max_ticks: int) -> list[int]:
    """Довывод остатка шагами; хвост добирается форсированно, задержка не больше max_ticks × tick.
    Без обращения к часам: в тестах ожидание подменено no-op, цикл на monotonic завис бы."""
    if remaining_len <= 0:
        return []
    chars_per_tick = max(1, int(chars_per_sec * tick_interval_sec))
    steps: list[int] = []
    revealed = 0
    while revealed < remaining_len and len(steps) < max_ticks:
        revealed = min(remaining_len, revealed + chars_per_tick)
        steps.append(revealed)
    if steps and steps[-1] < remaining_len:
        steps[-1] = remaining_len
    return steps


def blend_arrival_speed(prev_ewma: float | None, piece_len: int, dt_sec: float) -> float | None:
    """Подмешивает кусок в темп прихода. Нулевая дельта не наблюдение, возвращаем прежнее."""
    if piece_len <= 0 or dt_sec <= 0:
        return prev_ewma
    inst = max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, piece_len / dt_sec))
    return inst if prev_ewma is None else _ARRIVAL_ALPHA * inst + (1 - _ARRIVAL_ALPHA) * prev_ewma


def display_speed_for(arrival_ewma: float | None, pace_key: str) -> float:
    """Скорость показа: измеренный темп прихода, пока он есть; иначе глобальная EMA модели (затравка на первые куски)."""
    if arrival_ewma is not None:
        return max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, arrival_ewma))
    return get_typing_speed(pace_key)
