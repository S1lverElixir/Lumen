"""
lumen_limits.py — скользящее окно rate limit и очередь кнопок-уточнений.

Вынесено из bot.py (P2 аудита): обе структуры — словари в памяти процесса
с часовой чисткой/потолками, без зависимости от Telegram/LLM, поэтому живут
отдельно. bot.py реэкспортирует имена — `bot.X` в тестах не менялся.
"""
from __future__ import annotations

import os
import time
from typing import Any

# Простой трекер для rate limiting
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SEC = float(os.getenv("RATE_LIMIT_WINDOW_SEC", "30"))
MAX_RATE_LIMIT_KEYS = int(os.getenv("MAX_RATE_LIMIT_KEYS", "20000"))
user_rate_limits: dict[int, list[float]] = {}


def _cleanup_rate_limit_dict() -> None:
    """user_rate_limits раньше никогда не уменьшался — ключи (user_id) оставались
    в словаре навсегда, даже когда список timestamp'ов у конкретного пользователя
    полностью очищался скользящим окном в _handle_message_core. За месяцы работы
    с большим числом разных пользователей это медленная, но реальная утечка
    памяти. Вызывается раз в час из фонового цикла в _webhook_startup."""
    now = time.time()
    stale = [uid for uid, ts in user_rate_limits.items() if not ts or now - ts[-1] > 3600]
    for uid in stale:
        user_rate_limits.pop(uid, None)


def _check_and_register_rate_limit(user_id: int | None) -> bool:
    """Скользящее окно 5 запросов/30 сек на пользователя (или запасной ключ — см.
    _rate_limit_key_for_message в bot.py). Возвращает True, если лимит уже исчерпан
    (вызывающий код должен ответить и прекратить обработку) — в этом случае, в
    отличие от успешного случая, TIMESTAMP НЕ добавляется, чтобы не продлевать
    наказание бесконечно на каждое следующее сообщение сверху лимита."""
    if not user_id:
        return False
    now = time.time()
    if user_id not in user_rate_limits and len(user_rate_limits) >= MAX_RATE_LIMIT_KEYS:
        _cleanup_rate_limit_dict()
    timestamps = user_rate_limits.setdefault(user_id, [])
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW_SEC:
        timestamps.pop(0)
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    timestamps.append(now)
    return False


PICK_TTL_SEC = float(os.getenv("PICK_TTL_SEC", "300"))
MAX_PENDING_PICKS = int(os.getenv("MAX_PENDING_PICKS", "500"))
# token -> {chat_id, user_id, scenario, original, expires}: только в памяти
# процесса (как _speed_ema в lumen_typing_pace.py) — после рестарта кнопки
# честно считаются протухшими, это штатный путь, а не баг.
_pending_picks: dict[str, dict[str, Any]] = {}


def _purge_expired_picks(now: float | None = None) -> None:
    """Сносит протухшие записи ожидания кнопок — вызывается при создании новой
    (отдельного фонового цикла ради этого заводить не стали)."""
    now = time.monotonic() if now is None else now
    for token in [t for t, rec in _pending_picks.items() if rec["expires"] <= now]:
        del _pending_picks[token]


def _enforce_pending_picks_cap() -> None:
    """Потолок памяти на кнопки-уточнения: сверх лимита сносим самые
    близкие к протуханию (AUD-G-001)."""
    overflow = len(_pending_picks) - MAX_PENDING_PICKS
    if overflow <= 0:
        return
    oldest = sorted(_pending_picks, key=lambda t: _pending_picks[t]["expires"])[:overflow]
    for token in oldest:
        del _pending_picks[token]
