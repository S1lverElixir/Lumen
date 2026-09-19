"""
lumen_chat_state.py — состояние чатов, квоты и локи (вынесено из bot.py,
P2 аудита): ChatState/chat_state, персистентность (файл/Upstash), квоты,
флашинг грязного состояния, get_state/_t/_chat_lang, локи чатов, owner-утилиты.

Связи с рантаймом bot.py — только через отложенный `import bot` внутри функций
(модульного цикла нет). bot.py реэкспортирует имена — словари те же объекты,
тесты мутируют bot.chat_state/bot.GLOBAL_QUOTA как раньше.
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
# Per-chat хранилище (см. код-ревью suggestion #7): раньше ВЕСЬ chat_state (до 5000
# чатов) сериализовался и писался ОДНИМ блоком при каждом флаше — с Upstash это один
# большой REST-запрос; один неудачный/слишком большой write рисковал потерять сразу
# всё разом, а не одну запись. Теперь у каждого чата свой собственный ключ/файл, а
# CHAT_INDEX_KEY/CHAT_INDEX_FILE хранит только список ID чатов — так при рестарте
# известно, какие per-chat ключи вообще нужно прочитать.
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

# _save_chat_to_storage/_delete_chat_storage НАМЕРЕННО остаются здесь как реальные
# (не тонкие обёрточные) реализации, а не делегируют в lumen_state_storage.py, как
# остальные функции этой секции: они вызывают _storage_write_text/_storage_delete_text
# ПО ИМЕНИ, разрешаемому в собственном пространстве имён bot.py на момент вызова —
# это ЕДИНСТВЕННЫЙ способ, которым существующие тесты (патчащие именно
# `bot._storage_write_text`/`bot._storage_delete_text` через unittest.mock.patch)
# продолжают перехватывать вызов. Если бы эти две функции были обёртками вокруг
# lumen_state_storage._save_chat_to_storage (как остальные выше), тот вызывал бы
# СВОЮ собственную, непропатченную копию _storage_write_text внутри своего модуля —
# патч bot._storage_write_text никак её не затронул бы (patch мутирует атрибут
# только на объекте bot, а не на объекте lumen_state_storage).
def _save_chat_to_storage(chat_id: int, state: dict[str, Any]) -> bool:
    """Возвращает True при успехе, False при сбое. НАЙДЕНО ПРИ КОД-РЕВЬЮ: раньше эта
    функция ничего не возвращала — вызывающий код (_flush_dirty_state) уже успевал
    убрать chat_id из "грязного" набора ДО того, как запись реально прошла, и при
    сбое (например, временный 5xx/сетевой сбой Upstash) исключение здесь просто
    логировалось и терялось — состояние чата (вся история диалога) молча пропадало
    до следующей независимой мутации этого же чата. Если это было последнее
    сообщение перед долгим затишьем — при рестарте контейнера данные терялись
    безвозвратно, ровно то, что персистентность через Upstash должна была
    предотвращать. Теперь вызывающий код (_flush_dirty_state) возвращает неудавшиеся
    chat_id обратно в _dirty_chat_ids для повтора на следующем цикле."""
    import bot
    try:
        payload = json.dumps(_serialize_chat_state(state), ensure_ascii=False)
        bot._storage_write_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id), payload)
        return True
    except Exception as exc:
        log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
        return False

def _delete_chat_storage(chat_id: int) -> bool:
    """Возвращает True при успехе, False при сбое — тот же принцип, что и у
    _save_chat_to_storage выше (см. докстринг там): неудавшееся удаление теперь
    тоже возвращается в очередь на повтор, а не молча забывается (иначе вытесненный
    чат мог бы бесхозно остаться в Upstash/на диске навсегда при транзиентном сбое)."""
    import bot
    try:
        bot._storage_delete_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id))
        return True
    except Exception as exc:
        log.warning("[state] Deleting chat %s failed: %s", chat_id, exc)
        return False

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: форма одной записи GLOBAL_QUOTA[provider][model_id]
# (см. _quota_entry/_record_quota_usage/_mark_quota_exhausted ниже) была разбросанным
# по коду соглашением, а не задокументированной структурой. Как и ChatState выше —
# чисто типовая аннотация, ничего не меняет в рантайме (GLOBAL_QUOTA остаётся
# обычным dict из dict'ов).
class QuotaEntry(TypedDict):
    used: int
    exhausted_at: float | None

GLOBAL_QUOTA: dict[str, Any] = {
    "gemini": {},
    "openrouter": {},
    # НАЙДЕНО ПРИ КАЛИБРОВКЕ (25 июля 2026): /stats показывал сотни запросов
    # по моделям при аптайме процесса всего 12 минут — счётчик "used" копится
    # НАВСЕГДА (переживает рестарты через Upstash, см. _save_chat_to_storage/
    # load_global_quota), а реальные суточные лимиты Google/OpenRouter обнуляются
    # каждые сутки. Бот об этом не знал вообще — "used"/"exhausted_at" не
    # сбрасывались никогда, поэтому /stats после нескольких дней работы показывал
    # бы бессмысленно огромные числа, а модель, один раз поймавшая 429 в первый
    # день, так и висела бы с пометкой "(лимит исчерпан)" даже после того, как
    # реальный лимит давно обновился. quota_day хранит дату (ISO, по America/
    # Los_Angeles — именно там у Google полночь, когда реально обнуляется RPD-
    # лимит) последнего сброса счётчиков — см. _reset_quota_if_new_day ниже.
    "quota_day": None,
}

# НАЙДЕНО ПРИ CODE-REVIEW (перф): _quota_entry вызывает _reset_quota_if_new_day
# на КАЖДОЕ обращение к квоте (а таких обращений — по несколько на каждый успешный/
# неудачный вызов любой модели, т.е. потенциально десятки в секунду при активном
# трафике). Без троттлинга это означало бы конструирование ZoneInfo("America/
# Los_Angeles") и datetime.now(...) на каждый такой вызов — сама по себе дата не
# меняется чаще раза в сутки, минутная неточность здесь совершенно не важна.
# _QUOTA_CHECK_THROTTLE_SEC ограничивает, как часто мы вообще пересчитываем
# текущую дату; между пересчётами просто ничего не делаем.
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
    for provider in ("gemini", "openrouter"):
        for entry in GLOBAL_QUOTA.get(provider, {}).values():
            if isinstance(entry, dict):
                entry["used"] = 0
                entry["exhausted_at"] = None
    GLOBAL_QUOTA["quota_day"] = today
    if had_previous:
        log.info('[quota] New day started (%s) — used/exhausted_at counters reset for all models.', today)
    bot.mark_quota_dirty()

def load_global_quota() -> None:
    import bot
    try:
        raw = bot._storage_read_text("lumen:global_quota", GLOBAL_QUOTA_FILE)
        if not raw:
            return
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            if "gemini" in loaded:
                GLOBAL_QUOTA["gemini"] = loaded["gemini"]
            if "openrouter" in loaded:
                GLOBAL_QUOTA["openrouter"] = loaded["openrouter"]
            if "quota_day" in loaded:
                GLOBAL_QUOTA["quota_day"] = loaded["quota_day"]
    except Exception as exc:
        log.warning("[quota] Failed to load global quota: %s", exc)
    # Проверяем сразу после загрузки — если бот был перезапущен уже на следующие
    # сутки (обычное дело при редеплое), счётчики должны обнулиться сразу на
    # старте, а не ждать первого сообщения после полуночи или ближайшего часового тика.
    bot._reset_quota_if_new_day()

def save_global_quota() -> None:
    import bot
    try:
        bot._storage_write_text("lumen:global_quota", GLOBAL_QUOTA_FILE, json.dumps(GLOBAL_QUOTA, ensure_ascii=False))
    except Exception as exc:
        log.warning("[quota] Failed to save global quota: %s", exc)

# НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: до сих пор каждый формат-дрейф персистентного
# снимка чата (слияние gemini_history/or_history в единую history) обнаруживался
# в _restore_single_chat ad hoc проверками "есть ли такой-то ключ в JSON" —
# рабочий, но накопительный подход: с каждым новым изменением формата туда
# добавлялась ещё одна ветка "если ключа нет — значит старая запись".
# CHAT_STATE_SCHEMA_VERSION (импортирован из lumen_state_storage.py вместе с
# _serialize_chat_state — см. блок импорта в начале секции "хранение состояния и
# квот") делает следующую подобную миграцию однозначной: новый код сможет
# проверять `s.get("schema_version", 0)` одним явным числом вместо повторного
# гадания по присутствию ключей. Существующие персистентные записи (сделанные до
# введения этого поля) не имеют "schema_version" вообще — они естественно
# трактуются как версия 0 и продолжают проходить через уже отлаженные эвристики
# ниже без каких-либо изменений в их поведении (это поле — задел на будущее, а
# не ретроактивная миграция уже написанной логики).

def _restore_single_chat(cid: int, s: dict[str, Any]) -> None:
    """Разворачивает сериализованный снимок одного чата (см. _serialize_chat_state)
    обратно в chat_state[cid] — общая логика между новым per-chat форматом чтения
    и одноразовой миграцией из старого общего блоба (см. load_state_from_disk).

    Поля "gemini_model"/"openrouter_text_model"/"chat_provider"/"image_model" из
    старых записей (созданных до перехода на автоматический роутер для текста и,
    позже, для генерации изображений — см. README, "Автоматический выбор модели")
    намеренно нигде ниже не читаются — они устарели и больше ни на что не влияют.

    schema_version (см. CHAT_STATE_SCHEMA_VERSION выше) в самих записях, читаемых
    здесь, пока ни на что не влияет — существующая миграция (history/gemini_history)
    уже надёжно определяется по присутствию конкретных ключей, и это не нужно
    менять задним числом. Поле — задел на СЛЕДУЮЩИЙ формат-дрейф: тогда новую
    ветку можно будет добавить как `if s.get("schema_version", 0) < N`, а не
    подбирать очередную эвристику по ключам, как приходилось делать для миграции
    ниже."""
    import bot
    schema_version = s.get("schema_version", 0)
    log.debug('[state] Restoring chat %s (schema_version=%s)', cid, schema_version)
    raw_media = s.get("recent_media_ids", {})
    if isinstance(raw_media, dict):
        media_buckets = {
            str(uid): deque(items, maxlen=MAX_MEDIA_RECENT_IDS)
            for uid, items in raw_media.items()
        }
    else:
        # Старый формат (плоский список на весь чат) — не мигрируем содержимое,
        # просто стартуем с чистого состояния, новые записи наполнят сами по себе.
        media_buckets = {}
    if "history" in s:
        history = list(s.get("history") or [])
    else:
        # Миграция со старого формата раздельной памяти Gemini/OpenRouter (до
        # объединения в общую историю) — просто конкатенируем обе, обрезая до
        # общего лимита. Порядок между двумя источниками восстановить точно
        # нельзя (нет временных меток), но сохранить сам факт истории важнее,
        # чем идеальная хронология при одноразовой миграции старых чатов.
        history = list(s.get("gemini_history") or []) + list(s.get("or_history") or [])
        if len(history) > bot.SHARED_HISTORY_MAX_LEN:
            history = history[-bot.SHARED_HISTORY_MAX_LEN:]
    chat_state[cid] = {
        "history": history,
        "ctx": deque(maxlen=MAX_CHAT_HISTORY_LEN),
        "recent_media_ids": media_buckets,
        "last_activity": time.monotonic(),
    }

def _save_chat_index() -> None:
    import bot
    try:
        ids = sorted(chat_state.keys())
        bot._storage_write_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE, json.dumps(ids))
    except Exception as exc:
        log.warning("[state] Saving chat index failed: %s", exc)

# Раньше save_state_to_disk()/save_global_quota() вызывались синхронно почти на
# каждое сообщение прямо внутри асинхронных обработчиков — блокирующий json.dump
# на полном chat_state (до 5000 чатов) блокировал event loop для ВСЕХ чатов сразу,
# и с ростом числа активных чатов это становится всё дороже на каждое сообщение
# от любого одного пользователя. Теперь горячий путь только помечает КОНКРЕТНЫЙ
# чат "грязным" (см. mark_state_dirty(chat_id)), а реальная запись идёт из
# фоновой корутины _flush_dirty_state раз в FLUSH_INTERVAL_SEC через
# asyncio.to_thread (не блокируя loop) — и только по изменившимся чатам, а не
# по всем сразу (см. код-ревью suggestion #7 про размер payload и blast radius).
_dirty_chat_ids: set[int] = set()
_pending_chat_deletions: set[int] = set()
_index_dirty = False
_quota_dirty = False
FLUSH_INTERVAL_SEC = 10.0
# НАЙДЕНО ПРИ КОД-РЕВЬЮ (performance): раньше ничего не ограничивало число ОДНОВРЕМЕННЫХ
# asyncio.to_thread-вызовов внутри одного цикла _flush_dirty_state — резкий всплеск
# "грязных" чатов разом (например, после активности сразу в нескольких группах) мог бы
# породить сотни параллельных блокирующих HTTP-запросов к Upstash одновременно. Не
# критично при текущем масштабе бота, но дешёвая защита на будущее — ограничиваем
# конкурентность семафором, а не оставляем неограниченной.
STATE_FLUSH_CONCURRENCY = int(os.getenv("STATE_FLUSH_CONCURRENCY", "10"))
_state_flush_semaphore = asyncio.Semaphore(STATE_FLUSH_CONCURRENCY)

async def _save_chat_to_storage_limited(chat_id: int, state: dict[str, Any]) -> bool:
    import bot
    async with _state_flush_semaphore:
        return await asyncio.to_thread(bot._save_chat_to_storage, chat_id, state)

async def _delete_chat_storage_limited(chat_id: int) -> bool:
    import bot
    async with _state_flush_semaphore:
        return await asyncio.to_thread(bot._delete_chat_storage, chat_id)

def mark_state_dirty(chat_id: int | None = None) -> None:
    """Помечает состояние чата как требующее сохранения.
    Явный chat_id (предпочтительный путь для нового кода) — помечает "грязным"
    ТОЛЬКО этот чат, ничего больше. Вызов БЕЗ chat_id (для мест, которые меняют
    сразу много чатов разом — например _prune_old_chats при вытеснении старых
    чатов) помечает "грязными" вообще все текущие чаты и индекс целиком."""
    global _index_dirty
    if chat_id is not None:
        _dirty_chat_ids.add(chat_id)
    else:
        _dirty_chat_ids.update(chat_state.keys())
        _index_dirty = True

def _mark_new_chat_id(chat_id: int) -> None:
    """Регистрирует НОВЫЙ chat_id, только что появившийся в chat_state (см.
    get_state). Помечает и сам чат, и индекс "грязными" — если пометить только
    чат без индекса, после рестарта его данные будут недостижимы: per-chat ключ
    существует, но индекс (единственный способ узнать список ID при чтении) о
    нём не знает."""
    global _index_dirty
    _dirty_chat_ids.add(chat_id)
    _index_dirty = True

def mark_quota_dirty() -> None:
    global _quota_dirty
    _quota_dirty = True

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
            # ИСПРАВЛЕНО (код-ревью): раньше результат просто игнорировался — при
            # сбое удаление молча "терялось" (чат оставался бесхозно висеть в
            # хранилище навсегда, если это был единственный шанс его удалить).
            # Теперь неудавшиеся id возвращаются в очередь для повтора на
            # следующем цикле (return_exceptions=True защищает и от неожиданного
            # исключения, которое не было поймано внутри самой _delete_chat_storage).
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
            # ИСПРАВЛЕНО (найдено при код-ревью, КРИТИЧНО): раньше to_save
            # очищался ДО того, как запись реально прошла, а _save_chat_to_storage
            # сама ловила исключение и просто логировала его — наружу в gather
            # ничего не долетало. При транзиентном сбое Upstash (сетевой глюк,
            # 429 и т.п.) состояние чата (вся история диалога) молча терялось до
            # следующей независимой мутации этого же чата — а если это было
            # последнее сообщение перед долгим затишьем, данные пропадали
            # безвозвратно при следующем рестарте контейнера. Теперь неудавшиеся
            # id возвращаются обратно в _dirty_chat_ids для повтора на следующем
            # цикле (FLUSH_INTERVAL_SEC секунд спустя), а не теряются молча.
            failed_ids = {cid for cid, res in zip(attempted_ids, save_results) if res is not True}
            if failed_ids:
                _dirty_chat_ids.update(failed_ids)
                log.warning(
                    '[state] %d chat(s) failed to save this cycle, will retry next: %s',
                    len(failed_ids), ", ".join(str(c) for c in sorted(failed_ids)),
                )
        if _index_dirty:
            _index_dirty = False
            await asyncio.to_thread(bot._save_chat_index)
        if _quota_dirty:
            _quota_dirty = False
            await asyncio.to_thread(bot.save_global_quota)
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
    for cid in list(_pending_chat_deletions):
        bot._delete_chat_storage(cid)
    _pending_chat_deletions.clear()
    for cid in list(_dirty_chat_ids):
        st = chat_state.get(cid)
        if st is not None:
            bot._save_chat_to_storage(cid, st)
    _dirty_chat_ids.clear()
    if _index_dirty:
        bot._save_chat_index()
        _index_dirty = False

def load_state_from_disk() -> None:
    import bot
    bot.load_global_quota()

    index_raw = None
    try:
        index_raw = bot._storage_read_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE)
    except Exception as exc:
        log.warning("[state] Reading chat index failed, falling back to legacy combined blob: %s", exc)

    if index_raw is not None:
        # Новый формат (per-chat ключи) — индекс уже существует, читаем каждый
        # чат отдельно по своему ключу.
        try:
            chat_ids = json.loads(index_raw)
        except Exception as exc:
            log.warning('[state] Failed to parse chat index: %s', exc)
            chat_ids = []
        loaded_count = 0
        for chat_id_raw in chat_ids:
            try:
                cid = int(chat_id_raw)
            except Exception:
                continue
            try:
                raw = bot._storage_read_text(_chat_storage_key(cid), bot._chat_storage_path(cid))
            except Exception as exc:
                log.warning('[state] Failed to read chat %s: %s', cid, exc)
                continue
            if not raw:
                continue
            try:
                s = json.loads(raw)
            except Exception as exc:
                log.warning('[state] Failed to parse chat state %s: %s', cid, exc)
                continue
            bot._restore_single_chat(cid, s)
            loaded_count += 1
        log.info("[state] Restored states for %d chats (per-chat storage).", loaded_count)
        return

    # ── Legacy-формат (единый блоб на все чаты, старый ключ "lumen:chat_state") ──
    # Индекса ещё нет — значит бот ещё ни разу не сохранял состояние в новом
    # per-chat формате (первый запуск после этого обновления). Читаем как раньше,
    # но сразу помечаем ВСЕ восстановленные чаты и индекс "грязными" (mark_state_
    # dirty() без аргумента) — уже самый первый периодический флаш перепишет их
    # в новом per-chat формате; дальше старый общий ключ больше не читается.
    # Сам старый ключ/файл намеренно не удаляется автоматически — не хотим лишний
    # раз трогать чужие данные во время миграции, можно вычистить вручную позже.
    try:
        raw = bot._storage_read_text("lumen:chat_state", STATE_FILE_PATH)
        if not raw:
            return
        loaded = json.loads(raw)
        for chat_id_str, s in loaded.items():
            try:
                cid = int(chat_id_str)
            except Exception:
                continue
            bot._restore_single_chat(cid, s)
        log.info(
            '[state] Restored states for %d chats (migrated from the old shared storage format — will be rewritten in the new per-chat format on next flush).',
            len(chat_state),
        )
        bot.mark_state_dirty()
    except Exception as exc:
        log.warning("[state] Restoring states failed: %s", exc)

def get_state(chat_id: int) -> dict[str, Any]:
    import bot
    if chat_id not in chat_state:
        chat_state[chat_id] = {
            "history": [],
            "ctx": deque(maxlen=MAX_CHAT_HISTORY_LEN),
            "recent_media_ids": {},
            "last_activity": time.monotonic(),
        }
        # Новый chat_id должен попасть в индекс (см. per-chat хранилище выше) —
        # иначе после рестарта его данные будут недостижимы: собственный ключ
        # существует, но индекс о нём не знает.
        bot._mark_new_chat_id(chat_id)
    else:
        chat_state[chat_id]["last_activity"] = time.monotonic()
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
    """Системная строка key на языке чата (см. lumen_lang.py)."""
    import bot
    return _lang_t(bot._chat_lang(chat_id), key, **kwargs)

def _prune_old_chats() -> None:
    import bot
    sorted_ids = sorted(chat_state.keys(), key=lambda cid: chat_state[cid].get("last_activity", 0))
    to_remove = len(chat_state) - PRUNED_CHAT_TARGET
    removed_ids = sorted_ids[:to_remove]
    for cid in removed_ids:
        chat_state.pop(cid, None)
        bot._chat_locks.pop(cid, None)
    # Вытесненные чаты должны реально исчезнуть из хранилища (иначе их собственные
    # ключи/файлы бесхозно копятся навсегда) — ставим в очередь на удаление,
    # обрабатывается в _flush_dirty_state вместе с обычным сбросом.
    _pending_chat_deletions.update(removed_ids)
    bot.mark_state_dirty()

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    import bot
    lock = bot._chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        bot._chat_locks[chat_id] = lock
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
    """Единая точка проверки "это владелец бота?" — используется в /logs, /stats
    и при гейтинге привилегированных действий в группах (см. _is_privileged_in_chat
    ниже). Модель/провайдер бот теперь выбирает сам (см. секцию "автоматический
    выбор модели"), поэтому проверка реальных названий моделей ("показывать ли
    Gemini/Gemma/OpenRouter владельцу") больше не нужна нигде — эти названия
    вообще никому не показываются, включая владельца."""
    import bot
    return bot.OWNER_ID is not None and user_id is not None and user_id == bot.OWNER_ID

async def _notify_owner(text: str) -> None:
    """Минимальная наблюдаемость (аудит техдолга, август 2026): раньше единственным
    способом узнать о проблеме было ручное открытие /stats или /logs владельцем.
    Отправляет короткое ЛС владельцу через уже существующего бота — без внешнего
    сервиса мониторинга. Вызывается только на редкие, действительно важные события
    (срабатывание circuit breaker прокси, полное исчерпание квоты Gemini — см. сайты
    вызова), с собственным троттлингом на стороне вызывающего кода, чтобы не спамить
    владельца на каждое повторяющееся сообщение. Никогда не поднимает исключение —
    сбой уведомления не должен ронять обработку сообщения, из-за которого его вызвали."""
    import bot
    if bot.OWNER_ID is None or bot.bot is None:
        return
    with contextlib.suppress(Exception):
        await bot._tg_call(bot.bot.send_message, chat_id=bot.OWNER_ID, text=text, call_timeout=10.0)

async def _is_privileged_in_chat(chat_type: str, chat_id: int, user_id: int | None) -> bool:
    """Может ли этот пользователь менять ОБЩИЕ настройки данного чата (модель
    для генерации изображений, сброс истории — выбор модели/провайдера для
    текстового чата больше не настройка чата вообще, см. "автоматический выбор
    модели" ниже)? В личных сообщениях у чата всего один пользователь —
    разрешено всегда. Владелец бота (OWNER_ID) — разрешено всегда, в любом чате.
    В группах/супергруппах — только создатель или администратор ЭТОЙ группы
    (проверяется через getChatMember, не требует особых прав у бота помимо
    членства в чате)."""
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
    return sub.setdefault(model_id, {"used": 0, "exhausted_at": None})

def _mark_quota_exhausted(provider: str, model_id: str) -> None:
    """Записывает момент, когда API реально вернул 429/RESOURCE_EXHAUSTED для модели.
    Используется, потому что _record_quota_usage инкрементирует "used" только при
    успешном ответе — без этого счётчик мог годами показывать 0, даже если все
    запросы к модели упирались в реальный лимит на стороне Google/OpenRouter."""
    import bot
    e = bot._quota_entry(provider, model_id)
    e["exhausted_at"] = time.time()
    bot.mark_quota_dirty()

def _record_quota_usage(provider: str, model_id: str) -> None:
    import bot
    e = bot._quota_entry(provider, model_id)
    e["used"] = int(e.get("used") or 0) + 1
    e["exhausted_at"] = None
    bot.mark_quota_dirty()
