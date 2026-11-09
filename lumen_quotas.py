"""lumen_quotas.py — суточные квоты провайдеров, дневные лимиты пользователей и счётчики /stats: единое персистентное хранилище GLOBAL_QUOTA."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, TypedDict

log = logging.getLogger("bot")


# Форма записи GLOBAL_QUOTA — TypedDict QuotaEntry (только аннотация, рантайм не меняет).
class QuotaEntry(TypedDict):
    used: int
    exhausted_at: float | None
    cooldown_until: float | None

GLOBAL_QUOTA: dict[str, Any] = {
    "gemini": {},
    # used/exhausted сбрасываются в полночь LA: иначе вечный рост и залипший "лимит исчерпан" (калибровка 25.07.2026).
    "quota_day": None,
}
# Единственный список провайдеров с суточными счётчиками: новый провайдер обязан
# быть тут, иначе его счётчик не сбрасывается на новых сутках и не грузится из снапшота.
# "image" — картинки (Cloudflare/Pollinations), ключ словаря = имя провайдера.
_QUOTA_PROVIDERS = ("gemini", "groq", "munito", "image")

# Дата пересчитывается не чаще раза в минуту: ZoneInfo на каждое обращение дорого.
_QUOTA_CHECK_THROTTLE_SEC = 60.0


_last_gemini_exhausted_alert_monotonic: float = 0.0


async def _maybe_alert_gemini_exhausted() -> None:
    import bot
    global _last_gemini_exhausted_alert_monotonic
    now = time.monotonic()
    if now - _last_gemini_exhausted_alert_monotonic < bot.GEMINI_EXHAUSTED_ALERT_COOLDOWN_SEC:
        return
    _last_gemini_exhausted_alert_monotonic = now
    await bot._notify_owner(bot._t(bot.OWNER_ID, "owner_quota_notice"))

_last_quota_check_monotonic: float = 0.0


def _reset_quota_if_new_day() -> None:
    """Сбрасывает used/exhausted_at у ВСЕХ моделей всех провайдеров из
    _QUOTA_PROVIDERS, если с
    последнего сброса наступили новые сутки (по America/Los_Angeles). Вызывается и лениво (см.
    _quota_entry — на любое обращение к квоте), и явно раз в час из фонового цикла
    в _webhook_startup, чтобы сброс не зависел от того, придёт ли вообще новое
    сообщение сразу после полуночи."""
    import bot
    global _last_quota_check_monotonic
    now_mono = time.monotonic()
    if now_mono - _last_quota_check_monotonic < _QUOTA_CHECK_THROTTLE_SEC:
        return
    _last_quota_check_monotonic = now_mono
    today = bot._current_quota_day()
    if GLOBAL_QUOTA.get("quota_day") == today:
        return
    had_previous = GLOBAL_QUOTA.get("quota_day") is not None
    for provider in _QUOTA_PROVIDERS:
        for entry in GLOBAL_QUOTA.get(provider, {}).values():
            if isinstance(entry, dict):
                entry["used"] = 0
                entry["exhausted_at"] = None
                entry["cooldown_until"] = None
    # Пользовательские счётчики — те же сутки: нули всем, bonus переживает
    # (задел под платное продление). Без bonus записи не нужны до первого
    # завтрашнего сообщения — иначе словарь рос бы навсегда.
    bucket = GLOBAL_QUOTA.get(USER_DAILY_KEY)
    if isinstance(bucket, dict):
        kept = {}
        for uid, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            try:
                bonus = int(entry.get("bonus") or 0)
            except (TypeError, ValueError):
                # Мусор в bonus не роняет сброс суток, считается нулём.
                bonus = 0
            if bonus > 0:
                entry["total"] = 0
                entry["gemini"] = 0
                entry["tts"] = 0
                entry["draw"] = 0
                kept[uid] = entry
        GLOBAL_QUOTA[USER_DAILY_KEY] = kept
    # Суточные счётчики /stats — те же сутки: в ноль вместе с остальным.
    stats = GLOBAL_QUOTA.get(STATS_KEY)
    if isinstance(stats, dict):
        for field in _STATS_FIELDS:
            stats[field] = 0
    else:
        GLOBAL_QUOTA[STATS_KEY] = {field: 0 for field in _STATS_FIELDS}
    GLOBAL_QUOTA["quota_day"] = today
    if had_previous:
        log.info('[quota] New day started (%s) — used/exhausted_at counters reset for all models.', today)
    bot.mark_quota_dirty()

def load_global_quota() -> None:
    import bot
    import lumen_chat_state as lcs
    try:
        raw = bot._storage_read_text("lumen:global_quota", lcs.GLOBAL_QUOTA_FILE)
        if not raw:
            # Пусто — первый запуск, а не отказ: сбрасывать нечего.
            lcs._state_load_failed = False
            return
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            for provider in _QUOTA_PROVIDERS:
                if provider in loaded:
                    # Чужой тип (список вместо dict) раньше ломал _quota_entry на каждом обращении.
                    if isinstance(loaded[provider], dict):
                        GLOBAL_QUOTA[provider] = loaded[provider]
                    else:
                        log.warning("[quota] Skip provider %s: expected dict, got %s", provider, type(loaded[provider]).__name__)
            # Пользовательские счётчики переживают рестарт тем же файлом/ключом.
            raw_bucket = loaded.get(USER_DAILY_KEY)
            if isinstance(raw_bucket, dict):
                clean: dict[str, Any] = {}
                for uid, entry in raw_bucket.items():
                    if not isinstance(entry, dict):
                        continue
                    try:
                        clean[str(uid)] = {
                            "total": max(0, int(entry.get("total") or 0)),
                            "gemini": max(0, int(entry.get("gemini") or 0)),
                            "tts": max(0, int(entry.get("tts") or 0)),
                            "draw": max(0, int(entry.get("draw") or 0)),
                            "bonus": max(0, int(entry.get("bonus") or 0)),
                        }
                    except (TypeError, ValueError):
                        continue
                GLOBAL_QUOTA[USER_DAILY_KEY] = clean
            if "quota_day" in loaded:
                GLOBAL_QUOTA["quota_day"] = loaded["quota_day"]
            # Суточные счётчики /stats переживают рестарт тем же файлом/ключом.
            raw_stats = loaded.get(STATS_KEY)
            if isinstance(raw_stats, dict):
                clean_stats: dict[str, Any] = {}
                for field in _STATS_FIELDS:
                    try:
                        clean_stats[field] = max(0, int(raw_stats.get(field) or 0))
                    except (TypeError, ValueError):
                        clean_stats[field] = 0
                GLOBAL_QUOTA[STATS_KEY] = clean_stats
            # Владельческая блокировка переживает рестарт тем же файлом/ключом.
            raw_banned = loaded.get(lcs.BANNED_KEY)
            if isinstance(raw_banned, dict):
                clean_banned: dict[str, Any] = {}
                for uid, ts in raw_banned.items():
                    try:
                        clean_banned[str(int(uid))] = max(0, int(ts or 0))
                    except (TypeError, ValueError):
                        continue
                GLOBAL_QUOTA[lcs.BANNED_KEY] = clean_banned
            raw_ts = loaded.get("last_storage_write_ts")
            if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) and raw_ts > 0:
                GLOBAL_QUOTA["last_storage_write_ts"] = float(raw_ts)
        lcs._state_load_failed = False
    except Exception as exc:
        # Чтение/разбор упали — память недостоверна, перезапись запрещена (см. флаг).
        lcs._state_load_failed = True
        log.warning("[quota] Failed to load global quota: %s", exc)
    # Проверяем сразу после загрузки — если бот был перезапущен уже на следующие
    # сутки (обычное дело при редеплое), счётчики должны обнулиться сразу на
    # старте, а не ждать первого сообщения после полуночи или ближайшего часового тика.
    bot._reset_quota_if_new_day()

def save_global_quota() -> None:
    import bot
    import lumen_chat_state as lcs
    if lcs._state_load_failed:
        # Стартовая загрузка не удалась: удалённая квота новее пустой памяти, не затираем.
        log.warning('[quota] Skip quota save: startup load failed, keeping remote data intact.')
        return
    try:
        bot._storage_write_text("lumen:global_quota", lcs.GLOBAL_QUOTA_FILE, json.dumps(GLOBAL_QUOTA, ensure_ascii=False))
        lcs._note_storage_write()
    except Exception as exc:
        log.warning("[quota] Failed to save global quota: %s", exc)


def _quota_entry(provider: str, model_id: str) -> QuotaEntry:
    import bot
    bot._reset_quota_if_new_day()
    sub = GLOBAL_QUOTA.setdefault(provider, {})
    return sub.setdefault(model_id, {"used": 0, "exhausted_at": None, "cooldown_until": None})

# Сколько модель молчит после МИНУТНОГО 429 (лимит запросов в минуту), прежде чем её
# снова пробуют. Раньше минутный всплеск ставил метку до полуночи, и модель выпадала
# из роута на остаток суток (враждебное ревью 27.09.2026): снять метку было нечем,
# потому что заведомо мёртвую модель не зовут, а успешный ответ чистит метку.
# 10 минут с большим запасом переживают 30-секундное окно Munito (200 запросов).
QUOTA_RATE_LIMIT_COOLDOWN_SEC = 600.0

def _mark_quota_exhausted(provider: str, model_id: str) -> None:
    """Суточная метка exhausted: used растёт только на успехах, без неё 429 выглядел как 0."""
    import bot
    e = bot._quota_entry(provider, model_id)
    e["exhausted_at"] = time.time()
    e["cooldown_until"] = None
    bot.mark_quota_dirty()

def _mark_rate_limited(provider: str, model_id: str) -> None:
    """Минутный лимит запросов: короткая пауза вместо суточной метки. Модель вернётся
    в роут сама по истечении QUOTA_RATE_LIMIT_COOLDOWN_SEC — не нужно ждать суток."""
    import bot
    e = bot._quota_entry(provider, model_id)
    e["cooldown_until"] = time.time() + QUOTA_RATE_LIMIT_COOLDOWN_SEC
    bot.mark_quota_dirty()

def _record_quota_usage(provider: str, model_id: str, *, service: bool = False) -> None:
    """Служебные вызовы (саммари истории, транскрибация — service=True) в квоту
    не пишем: иначе /stats врёт, а триггеры exhausted срабатывают не на ответы людям."""
    import bot
    if service:
        log.debug("[quota] Service call %s/%s not counted.", provider, model_id)
        return
    e = bot._quota_entry(provider, model_id)
    e["used"] = int(e.get("used") or 0) + 1
    e["exhausted_at"] = None
    e["cooldown_until"] = None
    bot.mark_quota_dirty()

# ── Дневной бюджет и пауза картиночных провайдеров ──
# Тот же QuotaEntry, что у текстовых моделей: used на успехе, exhausted_at до завтра,
# cooldown_until на короткую перегрузку. Отдельного хранилища не заводим.
IMAGE_QUOTA_PROVIDER = "image"


def _image_usage_total() -> int:
    import bot
    bot._reset_quota_if_new_day()
    sub = GLOBAL_QUOTA.get(IMAGE_QUOTA_PROVIDER)
    if not isinstance(sub, dict):
        return 0
    total = 0
    for entry in sub.values():
        if isinstance(entry, dict):
            try:
                total += max(0, int(entry.get("used") or 0))
            except (TypeError, ValueError):
                continue
    return total


def _image_budget_exhausted() -> bool:
    """Общий дневной бюджет картинок: отказ до обращения к провайдеру, а не после.
    Владелец без лимита — ему нужен живой прогон фичи в диагностике."""
    import bot
    return _image_usage_total() >= bot.IMAGE_DAILY_LIMIT


def _image_provider_block_reason(provider: str) -> str | None:
    """None — трогать можно. "budget" — выбитый дневной бюджет (до следующих суток),
    "cooldown" — свежая перегрузка. Значения из снапшота могут быть мусором после
    битой записи, поэтому каждое приводится в try/except, а не голым float()."""
    import bot
    entry = bot._quota_entry(IMAGE_QUOTA_PROVIDER, provider)
    try:
        if entry.get("exhausted_at"):
            return "budget"
        cooldown = entry.get("cooldown_until")
        return "cooldown" if cooldown and time.time() < float(cooldown) else None
    except (TypeError, ValueError):
        return None


def _image_provider_ready(provider: str) -> bool:
    return _image_provider_block_reason(provider) is None


def _note_image_success(provider: str) -> None:
    """И used++, и снятие обеих меток — делает _record_quota_usage."""
    import bot
    bot._record_quota_usage(IMAGE_QUOTA_PROVIDER, provider)


def _note_image_rate_limited(provider: str, *, retry_after_sec: float | None = None) -> None:
    """Пауза на провайдера: Retry-After сервиса, но не меньше IMAGE_PROVIDER_COOLDOWN_SEC."""
    import bot
    entry = bot._quota_entry(IMAGE_QUOTA_PROVIDER, provider)
    wait = max(float(bot.IMAGE_PROVIDER_COOLDOWN_SEC), float(retry_after_sec or 0.0))
    entry["cooldown_until"] = time.time() + wait
    bot.mark_quota_dirty()


def _note_image_budget_exhausted(provider: str) -> None:
    import bot
    bot._mark_quota_exhausted(IMAGE_QUOTA_PROVIDER, provider)


# ── Суточные счётчики для /stats (получено/отвечено/отказы) ──
# Лежат внутри GLOBAL_QUOTA: тот же файл/ключ и тот же флаш, отдельной записи
# на каждое сообщение нет. Границы суток — те же (quota_day, PT).
STATS_KEY = "stats"
_STATS_FIELDS = (
    "messages_received",
    "answers_sent",
    "all_failed",
    "fallbacks",
    "daily_limit_denials",
    # 08.10.2026: каждая попытка к модели и каждая пустая (исключение/пусто до
    # показа) — по ним считается реальный КПД маршрута, а не гадается по логам.
    "model_attempts",
    "model_failures",
)


def _stats_entry() -> dict[str, Any]:
    """Живая запись суточных счётчиков (создаёт с нулями при первом обращении)."""
    import bot
    bot._reset_quota_if_new_day()
    stats = GLOBAL_QUOTA.get(STATS_KEY)
    if not isinstance(stats, dict):
        stats = {}
        GLOBAL_QUOTA[STATS_KEY] = stats
    for field in _STATS_FIELDS:
        try:
            stats[field] = max(0, int(stats.get(field) or 0))
        except (TypeError, ValueError):
            stats[field] = 0
    return stats


def _record_stats_event(name: str) -> None:
    """Плюс один к счётчику name. Чужие имена игнорирует: опечатка в вызове не
    должна раздувать персистентный словарь."""
    import bot
    if name not in _STATS_FIELDS:
        return
    entry = _stats_entry()
    entry[name] = int(entry.get(name) or 0) + 1
    bot.mark_quota_dirty()

# ── Дневные лимиты на пользователя (по user_id, не по чату) ──
# Хранятся внутри GLOBAL_QUOTA["user_daily"]: тот же персистентный файл/ключ и
# тот же пакетный флаш раз в FLUSH_INTERVAL_SEC — отдельной записи в Upstash на
# каждое сообщение нет. Границы суток — те же, что у квоты (quota_day, PT).
USER_DAILY_KEY = "user_daily"
# Потолок записей за сутки: легитимных пользователей в день на порядок меньше.
MAX_USER_DAILY_KEYS = 20000


def _user_daily_entry(user_id: int | None) -> dict[str, Any]:
    """Живая запись счётчиков пользователя (создаёт при первом обращении)."""
    import bot
    bot._reset_quota_if_new_day()
    bucket = GLOBAL_QUOTA.setdefault(USER_DAILY_KEY, {})
    key = str(user_id)
    entry = bucket.get(key)
    if not isinstance(entry, dict):
        entry = {}
        bucket[key] = entry
    else:
        # Недавнее использование — в конец: вытеснение ниже сносит давно молчавших.
        bucket[key] = bucket.pop(key)
    for field in ("total", "gemini", "tts", "draw", "bonus"):
        try:
            entry[field] = max(0, int(entry.get(field) or 0))
        except (TypeError, ValueError):
            entry[field] = 0
    # bonus — задел под платное продление, сам платёж не реализован.
    if len(bucket) > MAX_USER_DAILY_KEYS:
        drop = len(bucket) - int(MAX_USER_DAILY_KEYS * 0.9)
        for old in list(bucket)[:drop]:
            bucket.pop(old, None)
    return entry


def _user_daily_peek(user_id: int | None) -> dict[str, Any] | None:
    """Чтение без создания: проба лимита не заводит запись и не раздувает day_users."""
    import bot
    bot._reset_quota_if_new_day()
    bucket = GLOBAL_QUOTA.get(USER_DAILY_KEY)
    if not isinstance(bucket, dict):
        return None
    entry = bucket.get(str(user_id))
    return entry if isinstance(entry, dict) else None


def _user_daily_limit(user_id: int | None, kind: str, entry: dict[str, Any] | None = None) -> int:
    """Эффективный лимит с учётом bonus-задела (база env + bonus)."""
    import bot
    base = {
        "total": bot.DAILY_USER_MESSAGE_LIMIT,
        "gemini": bot.DAILY_USER_GEMINI_LIMIT,
        "tts": bot.DAILY_USER_TTS_LIMIT,
        "draw": bot.DAILY_USER_DRAW_LIMIT,
    }[kind]
    try:
        if entry is None:
            entry = _user_daily_peek(user_id)
        bonus = int((entry or {}).get("bonus") or 0)
        return int(base) + max(0, bonus)
    except Exception:
        return int(base)


def _user_daily_total_exhausted(user_id: int | None, entry: dict[str, Any] | None = None) -> bool:
    """Исчерпан ли общий дневной лимит. Владелец и неопределённый автор — без лимита."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return False
    if entry is None:
        entry = _user_daily_peek(user_id)
    if entry is None:
        return False
    try:
        used = int(entry.get("total") or 0)
    except (TypeError, ValueError):
        used = 0
    return used >= _user_daily_limit(user_id, "total", entry)


def _user_daily_gemini_exhausted(user_id: int | None, entry: dict[str, Any] | None = None) -> bool:
    import bot
    if user_id is None or bot._is_owner(user_id):
        return False
    if entry is None:
        entry = _user_daily_peek(user_id)
    if entry is None:
        return False
    try:
        used = int(entry.get("gemini") or 0)
    except (TypeError, ValueError):
        used = 0
    return used >= _user_daily_limit(user_id, "gemini", entry)


def _user_daily_tts_exhausted(user_id: int | None, entry: dict[str, Any] | None = None) -> bool:
    import bot
    if user_id is None or bot._is_owner(user_id):
        return False
    if entry is None:
        entry = _user_daily_peek(user_id)
    if entry is None:
        return False
    try:
        used = int(entry.get("tts") or 0)
    except (TypeError, ValueError):
        used = 0
    return used >= _user_daily_limit(user_id, "tts", entry)


def _record_user_daily(user_id: int | None, *, gemini: bool = False, tts: bool = False) -> None:
    """Учёт ответа модели: total всегда, gemini/tts — по факту провайдера.
    Только успехи: отказы по лимиту и неудачные вызовы сюда не доходят."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return
    entry = _user_daily_entry(user_id)
    entry["total"] = int(entry.get("total") or 0) + 1
    if gemini:
        entry["gemini"] = int(entry.get("gemini") or 0) + 1
    if tts:
        entry["tts"] = int(entry.get("tts") or 0) + 1
    bot.mark_quota_dirty()


def _reserve_user_daily(user_id: int | None, *, total: int = 0, tts: int = 0, draw: int = 0) -> bool:
    """Атомарно резервирует счётчики ДО первого await: параллельные /tts иначе
    читали 0 и обходили дневной лимит (аудит 28.10.2026). Возвращает False,
    если резерв не влезает; счётчики в этом случае не меняются."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return True
    entry = _user_daily_entry(user_id)
    used_total = int(entry.get("total") or 0)
    used_tts = int(entry.get("tts") or 0)
    used_draw = int(entry.get("draw") or 0)
    if used_total + total > _user_daily_limit(user_id, "total", entry):
        return False
    if used_tts + tts > _user_daily_limit(user_id, "tts", entry):
        return False
    if used_draw + draw > _user_daily_limit(user_id, "draw", entry):
        return False
    entry["total"] = used_total + total
    if tts:
        entry["tts"] = used_tts + tts
    if draw:
        entry["draw"] = used_draw + draw
    bot.mark_quota_dirty()
    return True


def _refund_user_daily(user_id: int | None, *, total: int = 0, tts: int = 0, draw: int = 0) -> None:
    """Возвращает неиспользованный резерв (синтез/отправка не удались)."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return
    if not total and not tts and not draw:
        return
    entry = _user_daily_entry(user_id)
    entry["total"] = max(0, int(entry.get("total") or 0) - total)
    if tts:
        entry["tts"] = max(0, int(entry.get("tts") or 0) - tts)
    if draw:
        entry["draw"] = max(0, int(entry.get("draw") or 0) - draw)
    bot.mark_quota_dirty()


def _user_key_for_message(message: Any) -> int | None:
    """user_id автора или ближайший устойчивый идентификатор (как у rate limit)."""
    if message is None:
        return None
    from_user = getattr(message, "from_user", None)
    if from_user is not None and getattr(from_user, "id", None) is not None:
        return from_user.id
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None and getattr(sender_chat, "id", None) is not None:
        return sender_chat.id
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", None) if chat is not None else None


def _user_daily_reset_in() -> tuple[int, int]:
    """Часы и минуты до сброса суток (для текста отказа по общему лимиту)."""
    import bot
    try:
        secs = float(bot._quota_day_reset_in_sec())
    except Exception:
        return 0, 0
    return int(secs // 3600), int((secs % 3600) // 60)
