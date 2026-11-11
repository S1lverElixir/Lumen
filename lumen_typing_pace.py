"""
lumen_typing_pace.py — самокалибрующийся темп "печати" при стриминге в Telegram.

Скорость — свойство бэкенда под слагом, а не модели, поэтому замер по факту + EMA. Единица — символы/с. EMA только в памяти.
"""

from __future__ import annotations

import time

# ── границы скорости печати (символов/сек) ──
# Подобраны под ощущение живого набора, владелец может менять прямо здесь.
DEFAULT_CHARS_PER_SEC = 90.0
MIN_CHARS_PER_SEC = 40.0
MAX_CHARS_PER_SEC = 260.0
# Потолок именно показа, не замера: 260 симв/с — скорость прихода кусков, а
# на экране Claude печатает заметно медленнее, и при 260 границы набора
# догоняет буфер за один кадр (прод 11.11.2026: frames=1).
DISPLAY_MAX_CHARS_PER_SEC = 110.0

# Меньше альфа — стабильнее оценка, но медленнее подстройка под смену бэкенда под тем же слагом.
_EMA_ALPHA = 0.3

# Выше, чем у EMA всего ответа: темп прихода меняется внутри одного стрима.
_ARRIVAL_ALPHA = 0.5

_speed_ema: dict[str, float] = {}

# Мелкий остаток в один тик не дробим: правка ради 20 символов — тот же лишний
# круг к Telegram без эффекта «печати». 120 было слишком крупно: хвост в 300+
# символов доезжал тремя прыжками по 100+ (прод 11.11.2026: max_step=214).
_CATCHUP_SMOOTH_MIN_CHARS = 60

# ── единый бюджет правок на чат (ревью 11.11.2026) ──
# Раньше тикер статуса, маляр стрима и финал троттлили каждый сам себя: в сумме
# получалось ~3 правки/с в одно сообщение, отсюда 429 и мигание. Слот один на чат,
# его берут все; не взят — кадр просто пропускается, анимация не ломается.
_edit_gate_ts: dict[int, float] = {}
_edit_gate_interval: dict[int, float] = {}


def arm_edit_gate(chat_id: int, min_interval_sec: float) -> None:
    """Завести на чат единый интервал между любыми edit_text (0 — троттлинг выключен)."""
    _edit_gate_interval[chat_id] = max(0.0, float(min_interval_sec))


def clear_edit_gate(chat_id: int) -> None:
    _edit_gate_ts.pop(chat_id, None)
    _edit_gate_interval.pop(chat_id, None)


def take_edit_slot(chat_id: int, *, force: bool = False) -> bool:
    """Один слот правки на чат: force — обязательная правка (первый кадр, финал),
    она слот берёт всегда; обычные кадры при занятом слоте пропускаются.
    Чат без завода (не стрим: точки, действие) — без троттлинга."""
    interval = _edit_gate_interval.get(chat_id)
    if interval is None:
        return True
    now = time.monotonic()
    if not force and now < _edit_gate_ts.get(chat_id, 0.0):
        return False
    _edit_gate_ts[chat_id] = now + interval
    return True


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
    """Довывод остатка шагами; задержка не больше max_ticks × tick.
    Без обращения к часам: в тестах ожидание подменено no-op, цикл на monotonic завис бы."""
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
    # Хвост не влезает в бюджет по скорости: раньше последний шаг вываливал
    # сразу сотни символов одним прыжком (ревью 11.11.2026). Доводим ровными
    # шагами: N = ceil(remaining/_CATCHUP_SMOOTH_MIN_CHARS) с потолком max_ticks,
    # шаг = remaining/N.
    ticks = min(max(1, max_ticks), -(-remaining_len // _CATCHUP_SMOOTH_MIN_CHARS))
    step = -(-remaining_len // ticks)
    return [min(remaining_len, step * i) for i in range(1, ticks + 1)]


def blend_arrival_speed(prev_ewma: float | None, piece_len: int, dt_sec: float) -> float | None:
    """Подмешивает кусок в темп прихода. Нулевая дельта не наблюдение, возвращаем прежнее."""
    if piece_len <= 0 or dt_sec <= 0:
        return prev_ewma
    inst = max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, piece_len / dt_sec))
    return inst if prev_ewma is None else _ARRIVAL_ALPHA * inst + (1 - _ARRIVAL_ALPHA) * prev_ewma


def display_speed_for(arrival_ewma: float | None, pace_key: str) -> float:
    """Скорость показа: измеренный темп прихода, пока он есть; иначе глобальная EMA модели (затравка на первые куски).
    Показ зажат снизу человеческой печатью Claude: измеренный темп прихода SSE
    кусками по 200+ символов обгоняет буфер, граница набора догоняет текст целиком
    и кадры схлопываются в 1-3 правки (прод 11.11.2026: frames=1 max_step=2 chars=322
    вместо плавного набора)."""
    speed = (max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, arrival_ewma))
             if arrival_ewma is not None else get_typing_speed(pace_key))
    return max(MIN_CHARS_PER_SEC, min(DISPLAY_MAX_CHARS_PER_SEC, speed))
