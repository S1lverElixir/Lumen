"""
lumen_chat_state.py — состояние чатов, квоты и локи: ChatState/chat_state,
персистентность (файл/Upstash), флашинг грязного состояния, get_state/_t,
локи чатов, owner-утилиты. Связь с bot.py — только отложенным импортом.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, TypedDict

from aiogram.enums import ChatType
from lumen_lang import DEFAULT_LANG, normalize_lang, t as _lang_t
from lumen_state_storage import (
    StorageConfig,
    _chat_storage_key,
    _serialize_chat_state,
    _upstash_request as _lumen_upstash_request,
    _upstash_set as _lumen_upstash_set,
    _upstash_get as _lumen_upstash_get,
    _upstash_delete as _lumen_upstash_delete,
    _storage_write_text as _lumen_storage_write_text,
    _storage_read_text as _lumen_storage_read_text,
    _storage_delete_text as _lumen_storage_delete_text,
    _chat_storage_path as _lumen_chat_storage_path,
)

log = logging.getLogger("bot")

MAX_CHAT_LIMIT = 5000
PRUNED_CHAT_TARGET = 4500
MAX_CHAT_HISTORY_LEN = 100
MAX_MEDIA_RECENT_IDS = 8  # хранится ОТДЕЛЬНО на каждого пользователя чата (см. recent_media_ids: dict[user_id, deque])
# Живых отправителей медиа в чате десятки, 500 покрывает большие группы с запасом.
# Потолок только против подделки user_id и раздувания снапшота, легитимные чаты не задевает.
MAX_MEDIA_BUCKETS_PER_CHAT = 500


class ChatState(TypedDict, total=False):
    history: list[dict[str, Any]]
    ctx: "deque[str]"
    recent_media_ids: dict[str, "deque[tuple[str, str]]"]
    last_activity: float
    lang: str

chat_state: dict[int, ChatState] = {}


_STATE_DIR = Path(os.getenv("STATE_DIR", "/app")).resolve()
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except Exception as _state_dir_exc:
    log.warning('[setup] STATE_DIR %s is not writable (%s), using a temp directory instead.', _STATE_DIR, _state_dir_exc)
    _STATE_DIR = Path(tempfile.gettempdir())
STATE_FILE_PATH = _STATE_DIR / "chat_state.json"
GLOBAL_QUOTA_FILE = _STATE_DIR / "global_quota.json"
# Per-chat ключи вместо одного блоба: меньше payload и blast radius при сбое.
_CHATS_DIR = _STATE_DIR / "chats"
with contextlib.suppress(Exception):
    _CHATS_DIR.mkdir(parents=True, exist_ok=True)
CHAT_INDEX_KEY = "lumen:chat_index"
CHAT_INDEX_FILE = _STATE_DIR / "chat_index.json"


def _storage_config() -> StorageConfig:
    import bot
    return StorageConfig(
        use_upstash=bot.USE_UPSTASH, upstash_url=bot.UPSTASH_REDIS_REST_URL,
        upstash_token=bot.UPSTASH_REDIS_REST_TOKEN, chats_dir=_CHATS_DIR,
    )


def _upstash_request(command_path: str, *, method: str = "GET", body: bytes | None = None) -> Any:
    import bot
    return _lumen_upstash_request(bot.UPSTASH_REDIS_REST_URL, bot.UPSTASH_REDIS_REST_TOKEN, command_path, method=method, body=body)

def _upstash_set(key: str, value: str) -> None:
    import bot
    _lumen_upstash_set(bot.UPSTASH_REDIS_REST_URL, bot.UPSTASH_REDIS_REST_TOKEN, key, value)

def _upstash_get(key: str) -> str | None:
    import bot
    return _lumen_upstash_get(bot.UPSTASH_REDIS_REST_URL, bot.UPSTASH_REDIS_REST_TOKEN, key)

def _upstash_delete(key: str) -> None:
    import bot
    _lumen_upstash_delete(bot.UPSTASH_REDIS_REST_URL, bot.UPSTASH_REDIS_REST_TOKEN, key)

def _storage_write_text(key: str, path: Path, text: str) -> None:
    import bot
    _lumen_storage_write_text(bot._storage_config(), key, path, text)

def _storage_read_text(key: str, path: Path) -> str | None:
    import bot
    return _lumen_storage_read_text(bot._storage_config(), key, path)

def _storage_delete_text(key: str, path: Path) -> None:
    import bot
    _lumen_storage_delete_text(bot._storage_config(), key, path)

def _chat_storage_path(chat_id: int) -> Path:
    import bot
    return _lumen_chat_storage_path(bot._storage_config(), chat_id)

# Момент последней успешной записи для /stats: дубль в GLOBAL_QUOTA переживает
# рестарт, иначе после рестарта показывало "записей не было" при живых данных.
_LAST_STORAGE_WRITE_TS: float | None = None


def _note_storage_write() -> None:
    """Фиксирует успешную запись (см. выше). Вызывать только при успехе."""
    global _LAST_STORAGE_WRITE_TS
    _LAST_STORAGE_WRITE_TS = time.time()
    try:
        GLOBAL_QUOTA["last_storage_write_ts"] = _LAST_STORAGE_WRITE_TS
    except Exception:
        pass


def _last_storage_write_ts() -> float | None:
    """Когда хранилище последний раз приняло запись, None — ещё ни разу."""
    if _LAST_STORAGE_WRITE_TS is not None:
        return _LAST_STORAGE_WRITE_TS
    try:
        ts = GLOBAL_QUOTA.get("last_storage_write_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            return float(ts)
    except Exception:
        pass
    return None


# Реальный код здесь, а не обёртки над storage: иначе тесты не перехватили бы вызовы через bot._storage_*.
def _save_chat_to_storage(chat_id: int, state: dict[str, Any]) -> bool:
    """True/False для повтора во флаше: раньше сбой молча терял историю до рестарта."""
    import bot
    try:
        payload = json.dumps(_serialize_chat_state(state), ensure_ascii=False)
        bot._storage_write_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id), payload)
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
        return False

def _delete_chat_storage(chat_id: int) -> bool:
    """Как выше: неудалённое возвращается в очередь, иначе бесхозные ключи навсегда."""
    import bot
    try:
        bot._storage_delete_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id))
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[state] Deleting chat %s failed: %s", chat_id, exc)
        return False

# Форма записи GLOBAL_QUOTA — TypedDict QuotaEntry (только аннотация, рантайм не меняет).
class QuotaEntry(TypedDict):
    used: int
    exhausted_at: float | None
    cooldown_until: float | None

GLOBAL_QUOTA: dict[str, Any] = {
    "gemini": {},
    "openrouter": {},
    # used/exhausted сбрасываются в полночь LA: иначе вечный рост и залипший "лимит исчерпан" (калибровка 25.07.2026).
    "quota_day": None,
}

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
    """Сбрасывает used/exhausted_at у ВСЕХ моделей (Gemini и OpenRouter), если с
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
    for provider in ("gemini", "openrouter", "groq"):
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
    global _state_load_failed
    try:
        raw = bot._storage_read_text("lumen:global_quota", GLOBAL_QUOTA_FILE)
        if not raw:
            # Пусто — первый запуск, а не отказ: сбрасывать нечего.
            _state_load_failed = False
            return
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            for provider in ("gemini", "openrouter", "groq"):
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
            raw_banned = loaded.get(BANNED_KEY)
            if isinstance(raw_banned, dict):
                clean_banned: dict[str, Any] = {}
                for uid, ts in raw_banned.items():
                    try:
                        clean_banned[str(int(uid))] = max(0, int(ts or 0))
                    except (TypeError, ValueError):
                        continue
                GLOBAL_QUOTA[BANNED_KEY] = clean_banned
            raw_ts = loaded.get("last_storage_write_ts")
            if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) and raw_ts > 0:
                GLOBAL_QUOTA["last_storage_write_ts"] = float(raw_ts)
        _state_load_failed = False
    except Exception as exc:
        # Чтение/разбор упали — память недостоверна, перезапись запрещена (см. флаг).
        _state_load_failed = True
        log.warning("[quota] Failed to load global quota: %s", exc)
    # Проверяем сразу после загрузки — если бот был перезапущен уже на следующие
    # сутки (обычное дело при редеплое), счётчики должны обнулиться сразу на
    # старте, а не ждать первого сообщения после полуночи или ближайшего часового тика.
    bot._reset_quota_if_new_day()

def save_global_quota() -> None:
    import bot
    if _state_load_failed:
        # Стартовая загрузка не удалась: удалённая квота новее пустой памяти, не затираем.
        log.warning('[quota] Skip quota save: startup load failed, keeping remote data intact.')
        return
    try:
        bot._storage_write_text("lumen:global_quota", GLOBAL_QUOTA_FILE, json.dumps(GLOBAL_QUOTA, ensure_ascii=False))
        _note_storage_write()
    except Exception as exc:
        log.warning("[quota] Failed to save global quota: %s", exc)

# schema_version — задел под следующую миграцию (if version < N); старые записи = v0 без смены поведения.

def _restore_single_chat(cid: int, s: dict[str, Any]) -> None:
    """Разворачивает сериализованный снимок одного чата (см. _serialize_chat_state)
    обратно в chat_state[cid] — общая логика между новым per-chat форматом чтения
    и одноразовой миграцией из старого общего блоба (см. load_state_from_disk).

    Старые поля моделей игнорируются: роутер автоматический; schema пока только задел."""
    import bot
    # Битая запись не должна ронять восстановление остальных чатов: пропускаем с warning.
    if not isinstance(s, dict):
        log.warning('[state] Skip chat %s: snapshot is not a dict', cid)
        return
    schema_version = s.get("schema_version", 0)
    log.debug('[state] Restoring chat %s (schema_version=%s)', cid, schema_version)
    raw_media = s.get("recent_media_ids", {})
    if isinstance(raw_media, dict):
        media_buckets: dict[str, Any] = {}
        for uid, items in raw_media.items():
            # Строка вместо списка давала deque из символов — такой бакет отбрасываем.
            if not isinstance(items, list):
                continue
            clean = [
                tuple(it) if isinstance(it, list) else it
                for it in items
                if isinstance(it, (list, tuple)) and len(it) == 2
                and isinstance(it[0], str) and isinstance(it[1], str)
            ]
            media_buckets[str(uid)] = deque(clean, maxlen=MAX_MEDIA_RECENT_IDS)
        if len(media_buckets) > MAX_MEDIA_BUCKETS_PER_CHAT:
            # Снимок старше фикса мог накопить лишнее — оставляем хвост, новые вытесняются в _save_media_to_history.
            media_buckets = dict(list(media_buckets.items())[-MAX_MEDIA_BUCKETS_PER_CHAT:])
    else:
        # Старый формат (плоский список на весь чат) — не мигрируем содержимое,
        # просто стартуем с чистого состояния, новые записи наполнят сами по себе.
        media_buckets = {}
    if "history" in s:
        raw_history = s.get("history")
        if raw_history is None:
            history = []
        elif not isinstance(raw_history, list):
            # list("строка") давал список символов и пересохранял мусор — пропускаем запись.
            log.warning('[state] Skip chat %s: history is not a list', cid)
            return
        else:
            history = list(raw_history)
    else:
        # Миграция со старого раздельного формата памяти: конкатенируем обе истории.
        # Точный порядок уже не восстановить, сам факт истории важнее.
        raw_gem = s.get("gemini_history")
        raw_or = s.get("or_history")
        if raw_gem is not None and not isinstance(raw_gem, list):
            log.warning('[state] Skip chat %s: gemini_history is not a list', cid)
            return
        if raw_or is not None and not isinstance(raw_or, list):
            log.warning('[state] Skip chat %s: or_history is not a list', cid)
            return
        history = list(raw_gem or []) + list(raw_or or [])
        if len(history) > bot.SHARED_HISTORY_MAX_LEN:
            history = history[-bot.SHARED_HISTORY_MAX_LEN:]
    chat_state[cid] = {
        "history": history,
        "ctx": deque(maxlen=MAX_CHAT_HISTORY_LEN),
        "recent_media_ids": media_buckets,
        # Настенные часы, а не monotonic: тот сбрасывается рестартом и делал все
        # чаты "активными" в /stats. Снимкам без метки — 0: такой чат неактивен.
        "last_activity": _restore_last_activity(s),
        # Язык переживает рестарт: сериализатор его пишет, а восстановление
        # раньше теряло (прод-баг: /lang слетал при каждом деплое).
        "lang": normalize_lang(s.get("lang")),
    }


def _restore_last_activity(s: dict[str, Any]) -> float:
    """Метка из снимка или 0 для старых записей без неё (см. выше)."""
    raw = s.get("last_activity")
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return 0.0

def _save_chat_index() -> bool:
    """True/False для повтора в _flush_state_now: раньше неуспех молча терялся (аудит A5-1)."""
    import bot
    try:
        ids = sorted(chat_state.keys())
        bot._storage_write_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE, json.dumps(ids))
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[state] Saving chat index failed: %s", exc)
        return False

def _save_chat_index_payload(payload: str) -> bool:
    """Только блокирующая запись готового снапшота индекса — для to_thread. True/False для повтора."""
    import bot
    try:
        bot._storage_write_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE, payload)
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[state] Saving chat index failed: %s", exc)
        return False

def _save_quota_payload(payload: str) -> bool:
    """Только блокирующая запись готового снапшота квоты — для to_thread. True/False для повтора."""
    import bot
    try:
        bot._storage_write_text("lumen:global_quota", GLOBAL_QUOTA_FILE, payload)
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[quota] Failed to save global quota: %s", exc)
        return False

# Раньше save_state_to_disk()/save_global_quota() вызывались синхронно почти на
# Горячий путь только помечает конкретный чат грязным, запись — из фоновой
# корутины раз в FLUSH_INTERVAL_SEC и только по изменившимся чатам: блокирующий
# dump всего состояния на каждое сообщение вешал loop для всех чатов сразу.
_dirty_chat_ids: set[int] = set()
_pending_chat_deletions: set[int] = set()
_index_dirty = False
_quota_dirty = False
# Поколения мутаций под гонку "payload построен — запись висит — состояние
# изменилось": флаш сбрасывает флаг, только если поколение не выросло за время
# записи, иначе изменение терялось бы до следующей мутации.
_index_generation = 0
_quota_generation = 0
# Отказ стартовой загрузки: пока стоит, индекс и квоту не перезаписываем,
# иначе первый флаш затрёт хорошие удалённые данные пустым снимком (аудит A5-2).
_state_load_failed = False
FLUSH_INTERVAL_SEC = 10.0
# Конкурентность флаша — семафором: всплеск "грязных" чатов иначе породил бы сотни параллельных HTTP к Upstash.
def _flush_concurrency() -> int:
    """Лениво через bot._env_number: голый int() ронял импорт на мусоре,
    а 0 давал висящий семафор."""
    import bot
    return bot._env_number("STATE_FLUSH_CONCURRENCY", 10, cast=int, min_value=1)


_state_flush_semaphore: asyncio.Semaphore | None = None


def _flush_semaphore() -> asyncio.Semaphore:
    """Один семафор на процесс, создаётся при первом флаше."""
    global _state_flush_semaphore
    if _state_flush_semaphore is None:
        _state_flush_semaphore = asyncio.Semaphore(_flush_concurrency())
    return _state_flush_semaphore

async def _save_chat_to_storage_limited(chat_id: int, state: dict[str, Any]) -> bool:
    import bot
    async with _flush_semaphore():
        # Снапшот JSON — ЗДЕСЬ, а не в потоке: dumps без await атомарен для loop'а,
        # а живой state в to_thread могли мутировать между итерациями (битый снапшот).
        try:
            payload = json.dumps(bot._serialize_chat_state(state), ensure_ascii=False)
        except Exception as exc:
            log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
            return False
        return await asyncio.to_thread(bot._save_chat_payload, chat_id, payload)

def _save_chat_payload(chat_id: int, payload: str) -> bool:
    """Только блокирующая запись готового снапшота (см. выше) — для to_thread."""
    import bot
    try:
        bot._storage_write_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id), payload)
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
        return False

async def _delete_chat_storage_limited(chat_id: int) -> bool:
    import bot
    async with _flush_semaphore():
        return await asyncio.to_thread(bot._delete_chat_storage, chat_id)

def mark_state_dirty(chat_id: int | None = None) -> None:
    """Помечает состояние чата как требующее сохранения.
    Явный chat_id (предпочтительный путь для нового кода) — помечает "грязным"
    ТОЛЬКО этот чат, ничего больше. Вызов БЕЗ chat_id (миграция из старого
    общего блоба) помечает "грязными" вообще все текущие чаты и индекс целиком."""
    global _index_dirty, _index_generation
    if chat_id is not None:
        _dirty_chat_ids.add(chat_id)
    else:
        _dirty_chat_ids.update(chat_state.keys())
        _index_dirty = True
        _index_generation += 1

def _mark_new_chat_id(chat_id: int) -> None:
    """Регистрирует НОВЫЙ chat_id, только что появившийся в chat_state (см.
    get_state). Помечает и сам чат, и индекс "грязными" — если пометить только
    чат без индекса, после рестарта его данные будут недостижимы: per-chat ключ
    существует, но индекс (единственный способ узнать список ID при чтении) о
    нём не знает."""
    global _index_dirty, _index_generation
    _dirty_chat_ids.add(chat_id)
    _index_dirty = True
    _index_generation += 1

def mark_quota_dirty() -> None:
    global _quota_dirty, _quota_generation
    _quota_dirty = True
    _quota_generation += 1

async def _flush_dirty_state_once() -> None:
    """Тело ОДНОЙ итерации периодического сброса состояния — вынесено из
    _flush_dirty_state (которая теперь только спит и вызывает эту функцию в цикле)
    отдельной функцией, чтобы её можно было протестировать напрямую, не дожидаясь
    реального FLUSH_INTERVAL_SEC в тестах."""
    import bot
    global _index_dirty, _quota_dirty
    try:
        if _pending_chat_deletions:
            to_delete = list(_pending_chat_deletions)
            _pending_chat_deletions.clear()
            del_results = await asyncio.gather(
                *(bot._delete_chat_storage_limited(cid) for cid in to_delete),
                return_exceptions=True,
            )
            # Неудавшиеся id возвращаются в очередь для повтора, а не теряются при 5xx/429 Upstash.
            failed_deletes = {cid for cid, res in zip(to_delete, del_results) if res is not True}
            if failed_deletes:
                _pending_chat_deletions.update(failed_deletes)
                log.warning(
                    '[state] %d chat deletion(s) failed, will retry next cycle: %s',
                    len(failed_deletes), ", ".join(str(c) for c in sorted(failed_deletes)),
                )
        if _dirty_chat_ids:
            to_save = list(_dirty_chat_ids)
            _dirty_chat_ids.clear()
            # Каждый чат — свой независимый write; если один упадёт, это не
            # заденет сохранение остальных чатов из этой же пачки.
            attempted_ids = [cid for cid in to_save if cid in chat_state]
            save_results = await asyncio.gather(
                *(bot._save_chat_to_storage_limited(cid, chat_state[cid]) for cid in attempted_ids),
                return_exceptions=True,
            )
            # Неудавшиеся id возвращаются в грязные для повтора, а не теряются при 5xx/429 Upstash.
            failed_ids = {cid for cid, res in zip(attempted_ids, save_results) if res is not True}
            if failed_ids:
                _dirty_chat_ids.update(failed_ids)
                log.warning(
                    '[state] %d chat(s) failed to save this cycle, will retry next: %s',
                    len(failed_ids), ", ".join(str(c) for c in sorted(failed_ids)),
                )
        if _index_dirty:
            # Флаг сбрасываем только после подтверждённой записи — иначе упавший индекс
            # молча терялся бы до следующей мутации (найдено внешним аудитом).
            # Снапшот ключей — здесь (см. комментарий у _save_chat_to_storage_limited).
            if _state_load_failed:
                # Удалённый индекс новее пустой памяти — не затираем, очередь живёт дальше.
                log.warning('[state] Skip index save: startup load failed, keeping remote data intact.')
            else:
                index_payload = json.dumps(sorted(chat_state.keys()))
                started_index_generation = _index_generation
                if await asyncio.to_thread(bot._save_chat_index_payload, index_payload):
                    # Сбрасываем, только если за время записи никто не мутировал:
                    # иначе свежий снимок уже устарел и флаг обязан жить дальше.
                    if _index_generation == started_index_generation:
                        _index_dirty = False
        if _quota_dirty:
            if _state_load_failed:
                # Удалённая квота новее пустой памяти — не затираем, очередь живёт дальше.
                log.warning('[quota] Skip quota save: startup load failed, keeping remote data intact.')
            else:
                quota_payload = json.dumps(GLOBAL_QUOTA, ensure_ascii=False)
                started_quota_generation = _quota_generation
                if await asyncio.to_thread(bot._save_quota_payload, quota_payload):
                    if _quota_generation == started_quota_generation:
                        _quota_dirty = False
    except Exception as exc:
        log.warning('[state] Periodic state flush failed: %s', exc)


async def _flush_dirty_state() -> None:
    import bot
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SEC)
        await bot._flush_dirty_state_once()
def _flush_state_now() -> None:
    """Синхронный финальный сброс всего "грязного" состояния — используется только
    при остановке процесса (main(), finally): event loop всё равно останавливается,
    поэтому блокирующие вызовы здесь не проблема, а вот пропустить несохранённые
    изменения между последним тиком периодического флаша и остановкой контейнера —
    проблема."""
    import bot
    global _index_dirty
    # Неуспех — обратно в очередь с громким логом, как в async-флаше: молча
    # чистить очереди на shutdown значило бы тихо терять записи (аудит A5-1).
    for cid in list(_pending_chat_deletions):
        try:
            deleted = bot._delete_chat_storage(cid)
        except Exception as exc:
            deleted = False
            log.warning('[state] Deleting chat %s failed: %s', cid, exc)
        if deleted is True:
            _pending_chat_deletions.discard(cid)
        else:
            log.warning('[state] Chat %s deletion failed, keeping it queued.', cid)
    for cid in list(_dirty_chat_ids):
        st = chat_state.get(cid)
        if st is None:
            _dirty_chat_ids.discard(cid)
            continue
        try:
            saved = bot._save_chat_to_storage(cid, st)
        except Exception as exc:
            saved = False
            log.warning('[state] Saving chat %s failed: %s', cid, exc)
        if saved is True:
            _dirty_chat_ids.discard(cid)
        else:
            log.warning('[state] Chat %s save failed, keeping it dirty.', cid)
    if _index_dirty:
        if _state_load_failed:
            # Удалённый индекс новее пустой памяти — не затираем (аудит A5-2).
            log.warning('[state] Skip index save: startup load failed, keeping remote data intact.')
        else:
            try:
                index_saved = bot._save_chat_index()
            except Exception as exc:
                index_saved = False
                log.warning('[state] Saving chat index failed: %s', exc)
            if index_saved is True:
                _index_dirty = False
            else:
                log.warning('[state] Chat index save failed, keeping it dirty.')

def load_state_from_disk() -> None:
    import bot
    global _state_load_failed
    # Новая попытка снимает старый отказ; неуспех ниже выставит его заново.
    _state_load_failed = False
    bot.load_global_quota()

    index_raw = None
    try:
        index_raw = bot._storage_read_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE)
    except Exception as exc:
        _state_load_failed = True
        log.warning("[state] Reading chat index failed, falling back to legacy combined blob: %s", exc)

    if index_raw is not None:
        # Новый формат (per-chat ключи) — индекс уже существует, читаем каждый
        # чат отдельно по своему ключу.
        try:
            chat_ids = json.loads(index_raw)
        except Exception as exc:
            log.warning('[state] Failed to parse chat index: %s', exc)
            chat_ids = None
        if isinstance(chat_ids, list):
            loaded_count = 0
            for chat_id_raw in chat_ids:
                try:
                    cid = int(chat_id_raw)
                except Exception:
                    continue
                try:
                    raw = bot._storage_read_text(_chat_storage_key(cid), bot._chat_storage_path(cid))
                except Exception as exc:
                    _state_load_failed = True
                    log.warning('[state] Failed to read chat %s: %s', cid, exc)
                    continue
                if not raw:
                    continue
                try:
                    s = json.loads(raw)
                except Exception as exc:
                    _state_load_failed = True
                    log.warning('[state] Failed to parse chat state %s: %s', cid, exc)
                    continue
                try:
                    bot._restore_single_chat(cid, s)
                except Exception as exc:
                    # Одна битая запись не обрывает восстановление остальных чатов.
                    _state_load_failed = True
                    log.warning('[state] Skip chat %s: restore failed: %s', cid, exc)
                    continue
                if cid in chat_state:
                    loaded_count += 1
            log.info("[state] Restored states for %d chats (per-chat storage).", loaded_count)
            return
        # Индекс битый, но per-chat файлы могут быть целы — пробуем legacy-блоб:
        # лучше старые данные, чем пустые чаты (найдено внешним аудитом).

    # ── Legacy-формат (единый блоб на все чаты, старый ключ "lumen:chat_state") ──
    # Старого индекса нет — первый запуск на новом формате. Восстановленные чаты
    # помечаем грязными: первый же флаш перепишет их по-новому. Старый ключ
    # не удаляем — чужие данные при миграции не трогаем.
    try:
        raw = bot._storage_read_text("lumen:chat_state", STATE_FILE_PATH)
        if not raw:
            if index_raw is not None:
                # Индекс был, но не разобрался, а legacy нет: удалённые per-chat
                # данные новее пустой памяти — следующий флаш их бы затирал.
                _state_load_failed = True
                log.warning("[state] Chat index unreadable and no legacy blob, keeping remote data intact.")
            return
        loaded = json.loads(raw)
        for chat_id_str, s in loaded.items():
            try:
                cid = int(chat_id_str)
            except Exception:
                continue
            try:
                bot._restore_single_chat(cid, s)
            except Exception as exc:
                # Одна битая запись не обрывает восстановление остальных чатов.
                _state_load_failed = True
                log.warning('[state] Skip chat %s: restore failed: %s', cid, exc)
                continue
        log.info(
            '[state] Restored states for %d chats (migrated from the old shared storage format — will be rewritten in the new per-chat format on next flush).',
            len(chat_state),
        )
        bot.mark_state_dirty()
    except Exception as exc:
        _state_load_failed = True
        log.warning("[state] Restoring states failed: %s", exc)

def get_state(chat_id: int) -> dict[str, Any]:
    import bot
    if chat_id not in chat_state:
        chat_state[chat_id] = {
            "history": [],
            "ctx": deque(maxlen=MAX_CHAT_HISTORY_LEN),
            "recent_media_ids": {},
            # Настенные часы: monotonic обнуляется рестартом и ломал /stats.
            "last_activity": time.time(),
        }
        # Новый chat_id должен попасть в индекс (см. per-chat хранилище выше) —
        # иначе после рестарта его данные будут недостижимы: собственный ключ
        # существует, но индекс о нём не знает.
        bot._mark_new_chat_id(chat_id)
    else:
        # Настенные часы: monotonic обнуляется рестартом и ломал /stats.
        chat_state[chat_id]["last_activity"] = time.time()
    if len(chat_state) > MAX_CHAT_LIMIT:
         bot._prune_old_chats()
    return chat_state[chat_id]

def _chat_lang(chat_id: int | None) -> str:
    """Язык системных сообщений для чата: поле "lang" состояния, иначе "en".
    Никогда не падает — при любом мусоре в поле отдаёт дефолт."""
    import bot
    try:
        if chat_id is None:
            return DEFAULT_LANG
        return normalize_lang(bot.get_state(chat_id).get("lang", DEFAULT_LANG))
    except Exception:
        return DEFAULT_LANG

def _t(chat_id: int | None, key: str, **kwargs: Any) -> str:
    """Локализованная реплика key на языке чата (см. lumen_lang.py)."""
    import bot
    return _lang_t(bot._chat_lang(chat_id), key, **kwargs)


def _peek_chat_lang(chat_id: int | None) -> str:
    """Язык чата без создания записи состояния. Нужен отказам до get_state:
    обычный _chat_lang через get_state заводил бы чат даже отклонённому по лимиту
    сообщению — и перенос проверки лимита выше get_state не давал эффекта."""
    try:
        if chat_id is None:
            return DEFAULT_LANG
        entry = chat_state.get(chat_id) or {}
        return normalize_lang(entry.get("lang", DEFAULT_LANG))
    except Exception:
        return DEFAULT_LANG


def _t_no_create(chat_id: int | None, key: str, **kwargs: Any) -> str:
    """Та же локализация, что _t, но без побочных эффектов для хранилища."""
    return _lang_t(_peek_chat_lang(chat_id), key, **kwargs)

def _prune_old_chats() -> None:
    import bot
    global _index_dirty, _index_generation
    sorted_ids = sorted(chat_state.keys(), key=lambda cid: chat_state[cid].get("last_activity", 0))
    # Лок ЗАНЯТ = в чате идёт ответ. Такой чат вытеснять нельзя: идущий маршрут
    # продолжал бы писать в отвязанный state, а следующее сообщение создало бы
    # новый лок — два параллельных ответа в одном чате и порча истории
    # (аудит 26.09.2026). Такой чат просто не попадает в выборку.
    def _is_busy(cid: int) -> bool:
        lock = bot._chat_locks.get(cid)
        return lock is not None and lock.locked()

    removable = [cid for cid in sorted_ids if not _is_busy(cid)]
    # Потолок считаем от всего состояния, а не от выборки removable: иначе при
    # плотной загрузке вытеснять было нечего и чаты копились без ограничения
    # (враждебное ревью 27.09.2026). Занятые пропускаем до следующих прогонов.
    to_remove = max(0, len(chat_state) - PRUNED_CHAT_TARGET)
    removed_ids = removable[:to_remove]
    for cid in removed_ids:
        chat_state.pop(cid, None)
        bot._chat_locks.pop(cid, None)
    # Вытесненные чаты должны реально исчезнуть из хранилища (иначе их собственные
    # ключи/файлы бесхозно копятся вечно) — ставим в очередь на удаление,
    # обрабатывается в _flush_dirty_state вместе с обычным сбросом.
    _pending_chat_deletions.update(removed_ids)
    _dirty_chat_ids.difference_update(removed_ids)
    if removed_ids:
        # Неизменённые чаты не пачкаем: иначе следующий флаш перезаписывал тысячи лишних ключей.
        _index_dirty = True
        _index_generation += 1

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    import bot
    lock = bot._chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        bot._chat_locks[chat_id] = lock
    return lock

_CHAT_LOCK_MAX_RETRIES = 3

async def acquire_chat_lock(chat_id: int, timeout: float) -> asyncio.Lock:
    """Захват per-chat лока с проверкой поколения: между get и acquire прунинг
    или эвикт могли снести чат и лок — захваченный лок тогда чужой, отпускаем
    и берём заново. Без проверки два сообщения отвечали бы параллельно, портя
    историю (аудит A5-10). Таймаут — на каждую попытку, кругов не больше трёх."""
    import bot
    for _ in range(_CHAT_LOCK_MAX_RETRIES):
        lock = get_chat_lock(chat_id)
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
        if bot._chat_locks.get(chat_id) is lock:
            return lock
        lock.release()
    lock = get_chat_lock(chat_id)
    await asyncio.wait_for(lock.acquire(), timeout=timeout)
    return lock

def _evict_orphan_chat_locks() -> int:
    """Сносит локи чатов, которых уже нет в chat_state и которые никто не
    держит, — иначе _chat_locks растёт навсегда (AUD-G-001). Занятые не трогаем."""
    import bot
    orphan = [cid for cid, lock in bot._chat_locks.items() if cid not in chat_state and not lock.locked()]
    for cid in orphan:
        bot._chat_locks.pop(cid, None)
    return len(orphan)

def _is_owner(user_id: int | None) -> bool:
    """Владелец по OWNER_ID; названий моделей никому не показываем (см. автороутер)."""
    import bot
    return bot.OWNER_ID is not None and user_id is not None and user_id == bot.OWNER_ID

# ── Владельческая блокировка (/ban): тот же персистентный файл/ключ, что квота.
BANNED_KEY = "banned_users"

def _banned_map() -> dict[str, Any]:
    import bot
    raw = bot.GLOBAL_QUOTA.get(BANNED_KEY)
    if not isinstance(raw, dict):
        raw = {}
        bot.GLOBAL_QUOTA[BANNED_KEY] = raw
    return raw

def _is_banned(user_id: int | None) -> bool:
    """Заблокирован ли пользователь. Владельца забанить нельзя — всегда False."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return False
    return str(user_id) in _banned_map()

def _ban_user(user_id: int) -> bool:
    """True если добавился сейчас, False если уже был (или это владелец)."""
    import bot
    if bot._is_owner(user_id):
        return False
    entry = _banned_map()
    if str(user_id) in entry:
        return False
    entry[str(user_id)] = int(time.time())
    bot.mark_quota_dirty()
    return True

def _unban_user(user_id: int) -> bool:
    """True если был в списке и убран."""
    import bot
    if str(user_id) not in _banned_map():
        return False
    del _banned_map()[str(user_id)]
    bot.mark_quota_dirty()
    return True

def _banned_list() -> list[int]:
    """ID по возрастанию — для /banlist."""
    ids = []
    for key in _banned_map():
        try:
            ids.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(ids)

async def _notify_owner(text: str) -> None:
    """ЛС владельцу о редких важных событиях; с троттлингом у вызывателя, никогда не кидает."""
    import bot
    if bot.OWNER_ID is None or bot.bot is None:
        return
    with contextlib.suppress(Exception):
        await bot._tg_call(bot.bot.send_message, chat_id=bot.OWNER_ID, text=text, call_timeout=10.0)

async def _is_privileged_in_chat(chat_type: str, chat_id: int, user_id: int | None) -> bool:
    """Общие настройки чата: личка — всегда, владелец — везде, группы — только creator/admin."""
    import bot
    if chat_type == ChatType.PRIVATE:
        return True
    if bot._is_owner(user_id):
        return True
    if user_id is None or bot.bot is None:
        return False
    try:
        member = await bot.bot.get_chat_member(chat_id, user_id)
        return getattr(member, "status", None) in ("creator", "administrator")
    except Exception as exc:
        log.warning('[perm] Failed to check admin status in chat %s: %s', chat_id, exc)
        return False


def _quota_entry(provider: str, model_id: str) -> QuotaEntry:
    import bot
    bot._reset_quota_if_new_day()
    sub = GLOBAL_QUOTA.setdefault(provider, {})
    return sub.setdefault(model_id, {"used": 0, "exhausted_at": None, "cooldown_until": None})

# Сколько модель молчит после МИНУТНОГО 429 (лимит запросов в минуту), прежде чем её
# снова пробуют. Раньше минутный всплеск ставил метку до полуночи, и модель выпадала
# из роута на остаток суток (враждебное ревью 27.09.2026): снять метку было нечем,
# потому что заведомо мёртвую модель не зовут, а успешный ответ чистит метку.
# 10 минут с большим запасом переживают окно Groq в 30 RPM.
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
    for field in ("total", "gemini", "tts", "bonus"):
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

# Сколько свежих сообщений точно не трогаем при обрезке (остальное уходит в саммари).
HISTORY_SUMMARIZE_KEEP = 80
# Вход саммаризатора режем, чтобы саммари не съело квоту целиком на километровом чате.
_HISTORY_SUMMARY_INPUT_MAX_CHARS = 6000

async def _trim_history(history: list[dict[str, Any]]) -> None:
    """Обрезка истории с саммари: старшие сообщения сверх HISTORY_SUMMARIZE_KEEP сжимаются
    одним вызовом дешёвой модели (Groq, резерв — голова OpenRouter), свежие остаются как есть.
    При любой неудаче (нет ключей, ошибка API, пустое саммари) — старая молчаливая обрезка до лимита."""
    import bot
    if len(history) <= bot.SHARED_HISTORY_MAX_LEN:
        return
    recent = history[-HISTORY_SUMMARIZE_KEEP:]
    old = history[:-HISTORY_SUMMARIZE_KEEP]
    lines = []
    for item in old:
        cont = item.get("content") if isinstance(item, dict) else None
        txt = bot._or_extract_text(cont) if isinstance(cont, (dict, list)) else str(cont or "").strip()
        if txt:
            role = str(item.get("role", "user")) if isinstance(item, dict) else "user"
            lines.append(f"{role}: {txt}")
    digest_input = "\n".join(lines)[:_HISTORY_SUMMARY_INPUT_MAX_CHARS]
    summary = ""
    if digest_input.strip():
        try:
            summary = await asyncio.wait_for(
                _summarize_text(digest_input), timeout=bot.HISTORY_SUMMARY_BUDGET_SEC,
            )
        except Exception as exc:
            log.warning("[history] Summarization over budget/failed, plain cut: %s", exc)
    if summary:
        history[:] = [{"role": "user", "content": "[Ранее в диалоге]: " + summary}] + recent
    else:
        history[:] = history[-bot.SHARED_HISTORY_MAX_LEN:]

async def _summarize_text(text: str) -> str:
    """Одно саммари дешёвой моделью. Пустая строка при любой неудаче — вызывающий код режет по-старому."""
    import bot
    messages = [
        {"role": "system", "content": "Summarize the conversation below briefly (5-8 sentences), in the conversation's own language. Facts and open questions only, no preamble."},
        {"role": "user", "content": text},
    ]
    payload = {"messages": messages, "temperature": 0.2, "max_tokens": 400, "stream": False}
    candidates: list[tuple[str, str, str]] = []
    if bot.GROQ_API_KEY:
        from lumen_router_config import _GROQ_LIGHT_ORDER
        candidates = [("groq", _GROQ_LIGHT_ORDER[0], "chat/completions")]
    if bot.OPENROUTER_API_KEY:
        from lumen_router_config import _OR_LIGHT_ORDER
        candidates.append(("openrouter", _OR_LIGHT_ORDER[0], "chat/completions"))
    for provider, model, path in candidates:
        try:
            payload["model"] = model
            if provider == "groq":
                resp = await bot._groq_request(path, "POST", json_body=payload)
            else:
                resp = await bot._or_request(path, "POST", json_body=payload)
            choices = resp.get("choices") or []
            answer = ""
            if choices:
                answer = bot._or_extract_text(choices[0].get("message") or "").strip()
            if answer:
                bot._record_quota_usage(provider, model, service=True)
                return answer
        except Exception as exc:
            log.warning("[history] Summarization via %s/%s failed, trying next: %s", provider, model, exc)
            continue
    return ""
