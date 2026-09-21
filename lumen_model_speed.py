"""
lumen_model_speed.py — самокалибрующаяся оценка задержек моделей (см. _run_route
и _run_streaming_reply в bot.py).

Прод-кейс 17.09.2026: 96с на обычный вопрос — голова цепочки тормозила, а роутер
ставил её первой (порядок цепочек статичен). Скорость — свойство бэкенда под
слагом, а не модели, поэтому вместо таблицы замер по факту + EMA; новичок
стартует с нейтрального приора и нащупывает место сам. EMA только в памяти:
после рестарта калибруется за первые сообщения.
"""

from __future__ import annotations

# Единый ключ provider:model_id живёт в lumen_typing_pace (там каноническое
# определение) — здесь реэкспорт, чтобы не плодить два одинаковых форматтера.
from lumen_typing_pace import speed_key

# Тот же компромисс стабильности/подстройки, что в typing_pace.
_EMA_ALPHA = 0.3

# Приор для моделей без единого замера: "обычный" ответ. Нейтрален специально —
# новичок не взлетает в голову и не падает в хвост только за то, что новый;
# кураторский порядок для таких решает (см. reorder_route).
_DEFAULT_TTF_SEC = 10.0
_DEFAULT_TOTAL_SEC = 25.0

# Границы-зажимы ДО усреднения (тот же приём, что clamp в record_observed_speed):
# один выброс не должен утащить EMA в небеса или в пол.
_MIN_TTF_SEC = 1.0
_MAX_TTF_SEC = 90.0
_MIN_TOTAL_SEC = 3.0
_MAX_TOTAL_SEC = 120.0

# Во сколько раз ожидание первого куска может превысить EMA_ttf модели, прежде
# чем попытка считается зависшей (см. first_chunk_limit_sec).
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
    """Учесть один УСПЕШНЫЙ ответ. ttf_sec — время до первого куска (у стриминга
    есть отдельно, у обычного запроса равно полному времени — тогда можно не
    передавать, подставится total_sec). Неположительные замеры игнорируются."""
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
    """Переупорядочить маршрут по измеренным задержкам, сохранив архитектуру:
    блоки провайдеров — в исходном порядке (первый блок остаётся первым),
    внутри блока — по возрастанию ожидаемого полного времени. Сортировка
    стабильная, поэтому модели с равными оценками (все новички) сохраняют
    кураторский порядок. Пустой маршрут возвращается как есть."""
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
