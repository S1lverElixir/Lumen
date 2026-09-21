"""
lumen_typing_pace.py — самокалибрующийся расчёт скорости "печати" при стриминге
ответа в Telegram (см. _run_streaming_reply в bot.py).

Скорость — свойство бэкенда под ":free"-слагом, а не модели: бэкенды шлют текст
одним чанком, таблица протухает молча. Замер по факту раз в стрим + EMA;
новичок стартует с дефолта и нащупывает сам. Единица — символы/с (токены SDK
не считает). EMA только в памяти: косметика, после рестарта нащупывается сама.
"""

from __future__ import annotations

# ── границы скорости печати (символов/сек) ──
# Подобраны эмпирически под ощущение "похоже на живой набор текста в Telegram",
# а не взяты из чьей-то спецификации — при желании владелец может изменить эти
# три константы прямо здесь, менять их часто не нужно.
DEFAULT_CHARS_PER_SEC = 90.0
MIN_CHARS_PER_SEC = 40.0
MAX_CHARS_PER_SEC = 260.0

# Насколько сильно один новый замер сдвигает EMA. Чем меньше — тем стабильнее
# оценка (не скачет от одного нетипичного ответа), но тем медленнее подстраивается
# под реальную смену бэкенда OpenRouter под тем же слагом.
_EMA_ALPHA = 0.3

_speed_ema: dict[str, float] = {}


def speed_key(provider: str, model_id: str) -> str:
    """Единый ключ для _speed_ema — тот же принцип пары (provider, model_id),
    что уже используется в GLOBAL_QUOTA (см. _quota_entry в bot.py)."""
    return f"{provider}:{model_id}"


def get_typing_speed(key: str) -> float:
    """Текущая оценка скорости печати для этой модели — DEFAULT_CHARS_PER_SEC,
    пока не накопилось ни одного реального замера. Результат всегда в границах
    [MIN_CHARS_PER_SEC, MAX_CHARS_PER_SEC], даже если константа DEFAULT когда-нибудь
    будет отредактирована за пределы этого диапазона по ошибке."""
    return max(MIN_CHARS_PER_SEC, min(MAX_CHARS_PER_SEC, _speed_ema.get(key, DEFAULT_CHARS_PER_SEC)))


def record_observed_speed(key: str, elapsed_sec: float, chars_len: int) -> None:
    """Обновляет EMA по итогам ОДНОГО завершённого стрима — вызывать один раз в
    конце (не на каждый кусок SSE), нас интересует средняя скорость всего ответа,
    а не шум отдельных кусков. elapsed_sec должен быть временем ЕСТЕСТВЕННОГО
    получения текста (от начала стрима до его исчерпания), БЕЗ искусственной фазы
    "довывода" (см. bot.py) — иначе самим же добавленным задержкам ЕМА поверила бы
    как настоящей медленной скорости бэкенда, и оценка бы разъехалась с реальностью.

    Сырое наблюдение зажимается в [MIN_CHARS_PER_SEC, MAX_CHARS_PER_SEC] ДО
    усреднения — без этого один нетипичный ответ, пришедший от бэкенда одним
    большим куском за доли секунды (наблюдаемая "скорость" тогда — тысячи
    симв/сек), утащил бы EMA в небеса, и следующий ответ той же модели "мигал"
    бы мгновенно вместо плавного набора — то есть ровно та проблема, которую
    эта функция должна была решить."""
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
