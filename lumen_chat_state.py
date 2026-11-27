"""lumen_chat_state.py — chat state, quotas, and locks: ChatState/chat_state,
persistence (file/Upstash), flushing dirty state, get_state/_t,
chat locks, and owner utilities. The link to bot.py is through deferred imports only.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
import time
from collections import deque
from pathlib import Path
from typing import Any, TypedDict

from aiogram.enums import ChatType
from lumen_lang import DEFAULT_LANG, normalize_lang, t as _lang_t
from lumen_memory import (
    MAX_MEMORY_MESSAGES,
    MEMORY_KEEP_MESSAGES,
    MAX_MEMORY_PENDING,
    summary_batch,
)
from lumen_state_storage import (
    CHAT_REPLACEMENT_SCHEMA_VERSION,
    CHAT_STATE_SCHEMA_VERSION,
    ChatJournalEntry,
    StorageConfig,
    _chat_storage_key,
    _is_json_integer,
    _serialize_chat_state,
    _validate_chat_snapshot,
    _validate_replacement_journal,
    _upstash_set as _lumen_upstash_set,
    _upstash_get as _lumen_upstash_get,
    _upstash_delete as _lumen_upstash_delete,
    _storage_write_text as _lumen_storage_write_text,
    _storage_read_text as _lumen_storage_read_text,
    _storage_delete_text as _lumen_storage_delete_text,
    _chat_storage_path as _lumen_chat_storage_path,
)

# Re-export for the bot.py/tests seam: bot.X and lcs.X see the same quota objects.
# GLOBAL_QUOTA is only mutated, never rebound — re-export shares the object (audit F-02, 09.11.2026).
# __all__ is explicit so pyflakes sees imports used for the public API rather
# than reporting them as unused.
from lumen_quotas import (
    GLOBAL_QUOTA,
    USER_DAILY_KEY,
    _QUOTA_CHECK_THROTTLE_SEC,
    _mark_quota_exhausted,
    _mark_rate_limited,
    _maybe_alert_gemini_exhausted,
    _image_budget_exhausted,
    _reserve_image_neurons,
    _release_image_neurons,
    _image_neurons_total,
    recover_global_quota,
    _image_provider_block_reason,
    _image_provider_ready,
    _image_usage_total,
    _note_image_budget_exhausted,
    _note_image_rate_limited,
    _note_image_success,
    _record_quota_usage,
    _record_stats_event,
    _record_user_daily,
    _refund_user_daily,
    _reserve_user_daily,
    _reset_quota_if_new_day,
    _stats_entry,
    _user_daily_entry,
    _user_daily_admission_blocked,
    _user_daily_gemini_exhausted,
    _user_daily_limit,
    _user_daily_peek,
    _user_daily_reset_in,
    _user_daily_total_exhausted,
    _user_daily_tts_exhausted,
    _user_key_for_message,
    _quota_entry,
    load_global_quota,
    save_global_quota,
)

__all__ = [
    "GLOBAL_QUOTA",
    "USER_DAILY_KEY",
    "_QUOTA_CHECK_THROTTLE_SEC",
    "_mark_quota_exhausted",
    "_mark_rate_limited",
    "_maybe_alert_gemini_exhausted",
    "_image_budget_exhausted",
    "_reserve_image_neurons",
    "_release_image_neurons",
    "_image_neurons_total",
    "recover_global_quota",
    "_image_provider_block_reason",
    "_image_provider_ready",
    "_image_usage_total",
    "_note_image_budget_exhausted",
    "_note_image_rate_limited",
    "_note_image_success",
    "_record_quota_usage",
    "_record_stats_event",
    "_record_user_daily",
    "_refund_user_daily",
    "_reserve_user_daily",
    "_reset_quota_if_new_day",
    "_stats_entry",
    "_user_daily_entry",
    "_user_daily_admission_blocked",
    "_user_daily_gemini_exhausted",
    "_user_daily_limit",
    "_user_daily_peek",
    "_user_daily_reset_in",
    "_user_daily_total_exhausted",
    "_user_daily_tts_exhausted",
    "_user_key_for_message",
    "_quota_entry",
    "load_global_quota",
    "save_global_quota",
]

log = logging.getLogger("bot")

MAX_CHAT_LIMIT = 5000
_MAX_UNRESOLVED_CHAT_IDS = MAX_CHAT_LIMIT
PRUNED_CHAT_TARGET = 4500
MAX_MEDIA_RECENT_IDS = 8  # stored SEPARATELY for each chat user (see recent_media_ids: dict[user_id, deque])
# A chat has dozens of active media senders; 500 covers large groups with room
# to spare.
# Check recipient membership first, before loading or creating a chat.
MAX_MEDIA_BUCKETS_PER_CHAT = 500
# A single memory limit per chat, including group background (owner's decision 06.11.2026:
# 100 was too few; background content was flushed to zero. 200 is roughly one
# day of an active group chat.
MAX_TOTAL_MEMORY_MESSAGES = MAX_MEMORY_MESSAGES
# How many senders to remember per chat: nicknames for signatures and nickname resolution.
MAX_KNOWN_SENDERS_PER_CHAT = 500


class ChatState(TypedDict, total=False):
    # history is a single chronological log: group background, messages and bot replies.
    history: list[dict[str, Any]]
    memory_summary: str
    memory_pending: list[dict[str, Any]]
    # Memory generation: /reset during summarization must not bring old facts back.
    memory_epoch: int
    recent_media_ids: dict[str, "deque[tuple[str, str]]"]
    user_names: dict[str, dict[str, str]]
    last_activity: float
    lang: str

chat_state: dict[int, ChatState] = {}


_STATE_DIR = Path(os.getenv("STATE_DIR", "/app")).resolve()
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except Exception as _state_dir_exc:
    log.warning('[setup] STATE_DIR %s is not writable (%s), using a private temp directory instead.', _STATE_DIR, _state_dir_exc)
    # Private subdirectory with mode 0o700: a bare gettempdir() is readable by others on the machine,
    # where the chat histories are (audit M4, 10.2026).
    _STATE_DIR = Path(tempfile.mkdtemp(prefix="lumen-state-"))
    os.chmod(_STATE_DIR, 0o700)
STATE_FILE_PATH = _STATE_DIR / "chat_state.json"
GLOBAL_QUOTA_FILE = _STATE_DIR / "global_quota.json"
# Per-chat keys instead of one blob: smaller payload and failure blast radius.
_CHATS_DIR = _STATE_DIR / "chats"
with contextlib.suppress(Exception):
    _CHATS_DIR.mkdir(parents=True, exist_ok=True)
CHAT_INDEX_KEY = "lumen:chat_index"
CHAT_INDEX_FILE = _STATE_DIR / "chat_index.json"
CHAT_REPLACEMENTS_KEY = "lumen:chat_replacements"
CHAT_REPLACEMENTS_FILE = _STATE_DIR / "chat_replacements.json"


def _storage_config() -> StorageConfig:
    import bot
    return StorageConfig(
        use_upstash=bot.USE_UPSTASH, upstash_url=bot.UPSTASH_REDIS_REST_URL,
        upstash_token=bot.UPSTASH_REDIS_REST_TOKEN, chats_dir=_CHATS_DIR,
    )


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


_replacement_journal_lock = threading.Lock()
_replacement_state_lock = threading.Lock()
_replacement_generations: dict[int, str] = {}
_journal_deletion_ids: set[int] = set()
_replacement_ambiguous_clears: dict[int, str] = {}
_replacement_publication_dirty: set[int] = set()


def _replacement_journal_document(pending: dict[int, ChatJournalEntry]) -> dict[str, object]:
    serialized = {
        str(chat_id): generation if kind == "replacement" else {"kind": "delete"}
        for chat_id, (kind, generation) in pending.items()
    }
    return {"schema_version": CHAT_REPLACEMENT_SCHEMA_VERSION, "pending": serialized}


def _read_replacement_journal_unlocked() -> dict[int, ChatJournalEntry]:
    import bot
    raw = bot._storage_read_text(CHAT_REPLACEMENTS_KEY, CHAT_REPLACEMENTS_FILE)
    if raw is None:
        return {}
    parsed = _validate_replacement_journal(json.loads(raw))
    if parsed is None:
        raise ValueError("invalid chat replacement journal")
    return parsed


def _unresolved_chat_ids_locked() -> set[int]:
    return (
        _recreated_chat_ids
        | _pending_chat_deletions
        | _deleting_chat_ids
        | _deletion_index_pending_ids
        | _journal_deletion_ids
        | set(_replacement_generations)
    )


def _reconcile_replacement_state_locked(chat_id: int) -> None:
    _replacement_generations.pop(chat_id, None)
    _journal_deletion_ids.discard(chat_id)
    _replacement_ambiguous_clears.pop(chat_id, None)
    _recreated_chat_ids.discard(chat_id)
    _deleting_chat_ids.discard(chat_id)
    if chat_id in _replacement_publication_dirty:
        _dirty_chat_ids.discard(chat_id)
    _replacement_publication_dirty.discard(chat_id)


def _load_replacement_journal() -> bool:
    """Load the journal fail-closed before any indexed snapshot can be trusted."""
    global _replacement_generations, _journal_deletion_ids, _replacement_ambiguous_clears
    with _replacement_journal_lock:
        try:
            pending = _read_replacement_journal_unlocked()
        except Exception as exc:
            with _replacement_state_lock:
                _replacement_generations = {}
                _journal_deletion_ids = set()
                _replacement_ambiguous_clears = {}
            log.warning("[state] Reading chat replacement journal failed: %s", exc)
            return False
        if len(pending) > _MAX_UNRESOLVED_CHAT_IDS:
            with _replacement_state_lock:
                _replacement_generations = {}
                _journal_deletion_ids = set()
                _replacement_ambiguous_clears = {}
            log.warning("[state] Chat replacement journal exceeds the unresolved-state cap.")
            return False
        with _replacement_state_lock:
            _replacement_generations = {
                chat_id: generation
                for chat_id, (kind, generation) in pending.items()
                if kind == "replacement" and generation is not None
            }
            _journal_deletion_ids = {
                chat_id for chat_id, (kind, _generation) in pending.items()
                if kind == "delete"
            }
            _replacement_ambiguous_clears = {}
        return True


def _ensure_replacement_generation(chat_id: int) -> str:
    """Persist the replacement intent before its snapshot and retain it across retries."""
    import bot
    while True:
        with _replacement_journal_lock:
            pending = _read_replacement_journal_unlocked()
            with _replacement_state_lock:
                unresolved = _unresolved_chat_ids_locked() | set(pending)
                if len(pending) > _MAX_UNRESOLVED_CHAT_IDS or (
                    chat_id not in unresolved and len(unresolved) >= _MAX_UNRESOLVED_CHAT_IDS
                ):
                    raise RuntimeError("chat replacement journal capacity reached")
                current = _replacement_generations.get(chat_id)
                current_entry = pending.get(chat_id)
                generation = (
                    current_entry[1]
                    if current_entry is not None and current_entry[0] == "replacement"
                    else current or uuid.uuid4().hex
                )
            if current_entry != ("replacement", generation):
                pending[chat_id] = ("replacement", generation)
                bot._storage_write_text(
                    CHAT_REPLACEMENTS_KEY, CHAT_REPLACEMENTS_FILE,
                    json.dumps(_replacement_journal_document(pending), ensure_ascii=False),
                )
                _note_storage_write()
            with _replacement_state_lock:
                if _replacement_generations.get(chat_id) != current:
                    continue
                _replacement_generations[chat_id] = generation
                _journal_deletion_ids.discard(chat_id)
                _replacement_ambiguous_clears.pop(chat_id, None)
                return generation


def _confirm_ambiguous_replacement_clear_locked(chat_id: int, generation: str) -> bool:
    import bot
    try:
        raw_index = bot._storage_read_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE)
        parsed_index = _parse_index_document(raw_index) if raw_index is not None else None
        if parsed_index is None or chat_id not in parsed_index[0]:
            return False
        raw_snapshot = bot._storage_read_text(
            _chat_storage_key(chat_id), bot._chat_storage_path(chat_id),
        )
        if raw_snapshot is None:
            return False
        snapshot = json.loads(raw_snapshot)
        return bool(
            _validate_chat_snapshot(snapshot)
            and snapshot.get("replacement_generation") == generation
        )
    except Exception as exc:
        log.warning("[state] Confirming chat %s replacement clear failed: %s", chat_id, exc)
        return False


def _clear_replacement_journal_locked(chat_id: int, generation: str) -> bool:
    import bot
    with _replacement_state_lock:
        if _replacement_generations.get(chat_id) != generation:
            return False
    pending = _read_replacement_journal_unlocked()
    if pending.get(chat_id) == ("replacement", generation):
        pending.pop(chat_id, None)
        try:
            bot._storage_write_text(
                CHAT_REPLACEMENTS_KEY, CHAT_REPLACEMENTS_FILE,
                json.dumps(_replacement_journal_document(pending), ensure_ascii=False),
            )
            _note_storage_write()
        except Exception:
            with _replacement_state_lock:
                if _replacement_generations.get(chat_id) == generation:
                    _replacement_ambiguous_clears[chat_id] = generation
            raise
        with _replacement_state_lock:
            if _replacement_generations.get(chat_id) != generation:
                return False
            _reconcile_replacement_state_locked(chat_id)
        return True
    if pending.get(chat_id) is not None:
        return False
    with _replacement_state_lock:
        if (
            _replacement_ambiguous_clears.get(chat_id) != generation
            or _replacement_generations.get(chat_id) != generation
        ):
            return False
    if not _confirm_ambiguous_replacement_clear_locked(chat_id, generation):
        return False
    with _replacement_state_lock:
        if (
            _replacement_ambiguous_clears.get(chat_id) != generation
            or _replacement_generations.get(chat_id) != generation
        ):
            return False
        _reconcile_replacement_state_locked(chat_id)
    return True


def _clear_replacement_journal(chat_id: int, generation: str) -> bool:
    """Clear only the generation that was actually published or deleted."""
    with _replacement_journal_lock:
        return _clear_replacement_journal_locked(chat_id, generation)


def _ensure_chat_deletion_intent(
    chat_id: int, *, generation: int, order: _StateWriteOrder,
) -> bool:
    """Persist deletion authority before removing any snapshot."""
    import bot
    if _state_load_failed:
        return False
    with _replacement_journal_lock:
        pending = _read_replacement_journal_unlocked()
        with _replacement_state_lock:
            unresolved = _unresolved_chat_ids_locked() | set(pending)
            if len(pending) > _MAX_UNRESOLVED_CHAT_IDS or (
                chat_id not in unresolved and len(unresolved) >= _MAX_UNRESOLVED_CHAT_IDS
            ):
                raise RuntimeError("chat deletion journal capacity reached")
        if pending.get(chat_id) != ("delete", None):
            pending[chat_id] = ("delete", None)
            bot._storage_write_text(
                CHAT_REPLACEMENTS_KEY, CHAT_REPLACEMENTS_FILE,
                json.dumps(_replacement_journal_document(pending), ensure_ascii=False),
            )
            _note_storage_write()
        with _replacement_state_lock:
            if generation != order.generation or chat_id in chat_state:
                return False
            _journal_deletion_ids.add(chat_id)
            _deleting_chat_ids.add(chat_id)
            # A durable deletion intent supersedes an older replacement barrier.
            _recreated_chat_ids.discard(chat_id)
    return True


def _clear_deletion_journal_locked(chat_id: int) -> bool:
    import bot
    pending = _read_replacement_journal_unlocked()
    if pending.get(chat_id) != ("delete", None):
        return False
    pending.pop(chat_id, None)
    bot._storage_write_text(
        CHAT_REPLACEMENTS_KEY, CHAT_REPLACEMENTS_FILE,
        json.dumps(_replacement_journal_document(pending), ensure_ascii=False),
    )
    _note_storage_write()
    with _replacement_state_lock:
        _reconcile_replacement_state_locked(chat_id)
    return True


def _finish_chat_deletions(
    chat_ids: set[int], *, generation: int | None = None,
    order: _StateWriteOrder | None = None, delete_snapshots: bool = False,
) -> set[int]:
    """Publish durable index exclusions before releasing replacement journals."""
    import bot
    global _index_dirty, _index_generation
    if not chat_ids:
        return set()
    if _state_load_failed:
        log.warning("[state] Chat deletion index publication blocked: startup state is unreadable.")
        return set()
    with _replacement_journal_lock:
        try:
            pending = _read_replacement_journal_unlocked()
        except Exception as exc:
            log.warning("[state] Reading chat deletion journal failed: %s", exc)
            return set()
        entries = {cid: pending.get(cid) for cid in chat_ids}
        if delete_snapshots:
            try:
                for cid in sorted(chat_ids):
                    bot._storage_delete_text(_chat_storage_key(cid), bot._chat_storage_path(cid))
            except Exception as exc:
                log.warning("[state] Removing interrupted chat snapshot(s) failed: %s", exc)
                return set()
        _index_dirty = True
        _index_generation += 1
        started_generation = _index_generation
        try:
            payload = json.dumps(_index_document(exclude=chat_ids))
            saved = bot._save_chat_index_payload(payload, generation=started_generation)
        except Exception as exc:
            log.warning("[state] Publishing chat deletion index failed: %s", exc)
            return set()
        if not saved or _index_generation != started_generation:
            log.warning("[state] Chat deletion index publication failed or was superseded; keeping retry state.")
            return set()
        if order is not None and generation != order.generation:
            return set()
        completed: set[int] = set()
        for cid in sorted(chat_ids):
            entry = entries[cid]
            if entry is not None and entry[0] == "delete":
                try:
                    cleared = _clear_deletion_journal_locked(cid)
                except Exception as exc:
                    log.warning("[state] Chat %s was deleted but its deletion journal could not be cleared: %s", cid, exc)
                    continue
                if not cleared:
                    log.warning("[state] Chat %s was deleted but its deletion journal could not be cleared.", cid)
                    continue
            elif entry is not None:
                try:
                    cleared = _clear_replacement_journal_locked(cid, entry[1] or "")
                except Exception as exc:
                    log.warning("[state] Chat %s was deleted but its replacement journal could not be cleared: %s", cid, exc)
                    continue
                if not cleared:
                    log.warning("[state] Chat %s was deleted but its replacement journal could not be cleared.", cid)
                    continue
            else:
                with _replacement_state_lock:
                    current = _replacement_generations.get(cid)
                    if cid in chat_state or (
                        current is not None
                        and current != _replacement_ambiguous_clears.get(cid)
                    ):
                        continue
                    _reconcile_replacement_state_locked(cid)
            with _replacement_state_lock:
                if order is not None and (
                    generation != order.generation or cid in _recreated_chat_ids
                ):
                    continue
                _pending_chat_deletions.discard(cid)
                _index_extra_ids.discard(cid)
                _legacy_migration_pending_ids.discard(cid)
                _deletion_index_pending_ids.discard(cid)
            completed.add(cid)
        return completed


def _finish_chat_deletion(
    chat_id: int, *, generation: int | None = None,
    order: _StateWriteOrder | None = None,
) -> bool:
    """Keep the recreation barrier if durable deletion cannot clear its journal."""
    return _finish_chat_deletions(
        {chat_id}, generation=generation, order=order,
    ) == {chat_id}


def _finish_interrupted_chat_deletions(chat_ids: set[int]) -> None:
    """Finalize journaled missing snapshots, or retain them for a storage retry."""
    if not chat_ids:
        return
    with _replacement_state_lock:
        _deletion_index_pending_ids.update(chat_ids)
    completed = _finish_chat_deletions(chat_ids, delete_snapshots=True)
    with _replacement_state_lock:
        _legacy_migration_pending_ids.difference_update(chat_ids)
        _pending_chat_deletions.difference_update(completed)
        _pending_chat_deletions.update(chat_ids - completed)
    if completed:
        log.info("[state] Finalized %d interrupted chat deletion(s).", len(completed))
    if completed != chat_ids:
        log.warning("[state] Keeping %d interrupted chat deletion(s) queued for retry.", len(chat_ids - completed))


# Time of the last successful write for /stats: the duplicate in GLOBAL_QUOTA survives
# restart; otherwise, after a restart it showed "there were no records" despite live data.
_LAST_STORAGE_WRITE_TS: float | None = None


def _note_storage_write() -> None:
    """Records a successful write (see above). Call only on success."""
    global _LAST_STORAGE_WRITE_TS
    _LAST_STORAGE_WRITE_TS = time.time()
    try:
        GLOBAL_QUOTA["last_storage_write_ts"] = _LAST_STORAGE_WRITE_TS
    except Exception:
        pass


def _last_storage_write_ts() -> float | None:
    """When storage last accepted a write; None means it never has."""
    if _LAST_STORAGE_WRITE_TS is not None:
        return _LAST_STORAGE_WRITE_TS
    try:
        ts = GLOBAL_QUOTA.get("last_storage_write_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            return float(ts)
    except Exception:
        pass
    return None


# Put the real code here, not wrappers over storage; otherwise tests could not intercept calls through bot._storage_*.
def _save_chat_to_storage(chat_id: int, state: dict[str, Any]) -> bool:
    """True/False for retrying the flash: previously, a failure silently lost history until restart."""
    if _state_load_failed:
        # Index not read: we do not know which chats already exist remotely. While the flag
        # is set, do not write to any chat — otherwise the first message to an old chat overwrites
        # history earlier than a retry lists ids (the third pass on 16.11.2026).
        log.debug("[state] Skip chat %s shutdown save: chat index unreadable.", chat_id)
        return False
    if chat_id in _chat_snapshot_unloaded:
        # The same protective skip as in the async flush: the shutdown path on HF Space
        # runs on almost every restart (run-2 audit, review 08.11.2026).
        log.debug("[state] Skip chat %s shutdown save: remote snapshot was never restored.", chat_id)
        return False
    try:
        payload = json.dumps(_serialize_chat_state(state), ensure_ascii=False)
        _order, generation = _reserve_state_write(chat_id)
        return _save_chat_payload(chat_id, payload, generation=generation, order=_order)
    except Exception as exc:
        log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
        return False

def _delete_chat_storage(
    chat_id: int, *, generation: int | None = None,
    order: _StateWriteOrder | None = None,
) -> bool:
    """As above: undeleted items return to the queue, otherwise orphaned keys remain forever."""
    import bot
    if chat_id in _legacy_migration_pending_ids:
        log.warning('[state] Skip deleting chat %s: legacy migration is not complete.', chat_id)
        return False
    if generation is None:
        order, generation = _reserve_state_write(chat_id)
    elif order is None:
        order = _state_write_order(chat_id)
    with order.lock:
        if generation != order.generation:
            # No deletion happened. Retry so a later prune cannot orphan the newer key.
            return False
        try:
            if not _ensure_chat_deletion_intent(chat_id, generation=generation, order=order):
                return False
            bot._storage_delete_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id))
            _note_storage_write()
            with _replacement_state_lock:
                if (
                    generation != order.generation
                    or chat_id in _recreated_chat_ids
                    or chat_id in chat_state
                ):
                    return False
                _deletion_index_pending_ids.add(chat_id)
            if generation != order.generation:
                return False
            return _finish_chat_deletion(chat_id, generation=generation, order=order)
        except Exception as exc:
            log.warning("[state] Deleting chat %s failed: %s", chat_id, exc)
            return False

# schema_version is groundwork for the next migration (if version < N); old records = v0 with no behavior change.

def _split_legacy_ctx_line(line: str) -> tuple[str | None, str]:
    """Old background line "Display: text" → (display, text); without the marker — the entire text."""
    head, sep, tail = line.partition(": ")
    if sep and head and len(head) <= 80 and "\n" not in head:
        return head.strip(), tail.strip()
    return None, line.strip()


def _restore_single_chat(cid: int, s: dict[str, Any]) -> None:
    """Expands a serialized snapshot of one chat (see _serialize_chat_state) back into chat_state[cid]: shared logic between the new per-chat read format and the one-time migration from the old shared blob (see load_state_from_disk).

Legacy model fields are ignored: routing is automatic; schema is only a placeholder for now."""
    # Freeze a chat only for deletion: a load failure must not cause partial
    #     history to be overwritten; preserve the warning.
    if not isinstance(s, dict):
        log.warning('[state] Skip chat %s: snapshot is not a dict', cid)
        return
    schema_version = s.get("schema_version", 0)
    log.debug('[state] Restoring chat %s (schema_version=%s)', cid, schema_version)
    if not (type(schema_version) is int and schema_version in (0, 1)):
        # Versions 0 and 1 used the legacy field set. Current snapshots are a
        # strict contract; silent normalization could publish a partial backup.
        if (not _is_json_integer(schema_version)
                or schema_version != CHAT_STATE_SCHEMA_VERSION
                or not _validate_chat_snapshot(s, expected_version=CHAT_STATE_SCHEMA_VERSION)):
            raise ValueError("invalid current chat snapshot")
    raw_media = s.get("recent_media_ids", {})
    if isinstance(raw_media, dict):
        media_buckets: dict[str, Any] = {}
        for uid, items in raw_media.items():
            # A string instead of a list produced a deque of characters — discard such a bucket.
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
            # A snapshot older than the fix may have accumulated extras — retain the tail; new entries are evicted in _save_media_to_history.
            media_buckets = dict(list(media_buckets.items())[-MAX_MEDIA_BUCKETS_PER_CHAT:])
    else:
        # Old format (a flat list for the entire chat) — do not migrate its contents,
        # just start from clean state; new records will populate it on their own.
        media_buckets = {}
    if "history" in s:
        raw_history = s.get("history")
        if raw_history is None:
            history = []
        elif not isinstance(raw_history, list):
            # list("string") produced a list of characters and re-saved garbage; skip the write.
            log.warning('[state] Skip chat %s: history is not a list', cid)
            return
        else:
            history = list(raw_history)
    else:
        # Migration from the old split memory format: concatenate both histories.
        # The exact order can no longer be restored; the fact of the history matters more.
        raw_gem = s.get("gemini_history")
        raw_or = s.get("or_history")
        if raw_gem is not None and not isinstance(raw_gem, list):
            log.warning('[state] Skip chat %s: gemini_history is not a list', cid)
            return
        if raw_or is not None and not isinstance(raw_or, list):
            log.warning('[state] Skip chat %s: or_history is not a list', cid)
            return
        # Do not trim old messages here: the overall limit and summary queue handle that below.
        history = list(raw_gem or []) + list(raw_or or [])
    # Old background (ctx) is no longer separate storage: merge it into the common log
    # at the end. The exact order between old formats cannot be reconstructed, but the
    #     facts are intact.
    raw_ctx = s.get("ctx")
    if isinstance(raw_ctx, list):
        for line in raw_ctx[:MAX_TOTAL_MEMORY_MESSAGES]:
            if not isinstance(line, str) or not line.strip():
                continue
            ctx_sender, ctx_text = _split_legacy_ctx_line(line)
            entry: dict[str, Any] = {"role": "user", "content": ctx_text, "addressed": False}
            if ctx_sender:
                entry["sender"] = ctx_sender
            history.append(entry)
    raw_summary = s.get("memory_summary")
    summary = raw_summary.strip()[:8000] if isinstance(raw_summary, str) else ""
    raw_pending = s.get("memory_pending")
    pending = (
        [item for item in raw_pending if isinstance(item, dict)][-MAX_MEMORY_PENDING:]
        if isinstance(raw_pending, list) else []
    )
    # Reading/updating counters is also a write: merging must include those chats,
    #     not just add ours.
    if len(history) > MAX_TOTAL_MEMORY_MESSAGES:
        pending = (pending + history[:-MEMORY_KEEP_MESSAGES])[-MAX_MEMORY_PENDING:]
        del history[:-MEMORY_KEEP_MESSAGES]
    chat_state[cid] = {
        "history": history,
        "memory_summary": summary,
        "memory_pending": pending,
        "recent_media_ids": media_buckets,
        "user_names": _restore_user_names(s.get("user_names")),
        # Wall clock, not monotonic: the latter resets on restart and made all
        # chats as “active” in /stats. Snapshots without a marker get 0: such a chat is inactive.
        "last_activity": _restore_last_activity(s),
        # The language survives a restart: the serializer writes it, and restoration
        # previously lost (production bug: /lang reset on every deploy).
        "lang": normalize_lang(s.get("lang")),
        # Context for image editing/regeneration (the serializer writes it; old snapshots get nothing).
        "last_drawn_message_id": _restore_int_field(s, "last_drawn_message_id"),
        "last_drawn_at": _restore_last_drawn_at(s),
        "last_drawn_user": _restore_int_field(s, "last_drawn_user"),
        "last_draw_prompt": s.get("last_draw_prompt")[:600] if isinstance(s.get("last_draw_prompt"), str) else "",
        "draw_versions": _restore_draw_versions(s.get("draw_versions")),
        "draw_redo": _restore_draw_versions(s.get("draw_redo")),
    }


def _restore_draw_versions(raw: Any) -> list[dict[str, Any]]:
    """Image versions after a restart: dictionaries with file_id, at most five, with the description truncated like last_draw_prompt (review 10.11.2026)."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[-5:]:
        if not isinstance(item, dict) or not isinstance(item.get("file_id"), str):
            continue
        version = dict(item)
        if isinstance(version.get("prompt"), str):
            version["prompt"] = version["prompt"][:600]
        out.append(version)
    return out


def _restore_last_drawn_at(s: dict[str, Any]) -> float:
    """Time of the last generation from the snapshot; 0 for old snapshots (no context)."""
    raw = s.get("last_drawn_at")
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return 0.0


def _restore_int_field(s: dict[str, Any], key: str) -> int | None:
    """An integer from the snapshot: bool is also an int in Python, and garbage in the id field
    turned every "Show another option" tap into a permanent refusal (review 10.11.2026)."""
    raw = s.get(key)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return None


def _restore_last_activity(s: dict[str, Any]) -> float:
    """A mark from the snapshot, or 0 for old records without one (see above)."""
    raw = s.get("last_activity")
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return 0.0


def _sender_display(username: str | None, first_name: str | None) -> str:
    """Display name: @username, with first_name in parentheses if they differ."""
    clean_username = username.strip().lstrip("@") if isinstance(username, str) else ""
    clean_first = first_name.strip()[:64] if isinstance(first_name, str) and first_name.strip() else ""
    if clean_username:
        if clean_first and clean_first.lower() != clean_username.lower():
            return f"@{clean_username} ({clean_first})"
        return "@" + clean_username
    if clean_first:
        return clean_first
    return "User"


def _note_sender(state: dict[str, Any], user_id: int | None, username: str | None, first_name: str | None) -> str:
    """Remembers the sender in state[user_names] and returns the display name."""
    display = _sender_display(username, first_name)
    try:
        if user_id is None:
            return display
        bucket = state.get("user_names")
        if not isinstance(bucket, dict):
            bucket = {}
            state["user_names"] = bucket
        key = str(user_id)
        entry = bucket.get(key)
        if not isinstance(entry, dict):
            entry = {}
            bucket[key] = entry
        if isinstance(username, str) and username.strip():
            entry["username"] = username.strip().lstrip("@")[:64]
        if isinstance(first_name, str) and first_name.strip():
            entry["first_name"] = first_name.strip()[:64]
        if len(bucket) > MAX_KNOWN_SENDERS_PER_CHAT:
            # Evict the oldest: the dictionary is insertion-ordered.
            for old_key in list(bucket.keys())[:len(bucket) - MAX_KNOWN_SENDERS_PER_CHAT]:
                bucket.pop(old_key, None)
    except Exception:
        pass
    return display


def _restore_user_names(raw: Any) -> dict[str, dict[str, str]]:
    """Sender dictionary from a snapshot; discard garbage."""
    clean: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return clean
    for uid, entry in list(raw.items())[:MAX_KNOWN_SENDERS_PER_CHAT]:
        try:
            key = str(int(uid))
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        item: dict[str, str] = {}
        for field in ("username", "first_name"):
            val = entry.get(field)
            if isinstance(val, str) and val.strip():
                item[field] = val.strip()[:64]
        if item:
            clean[key] = item
    return clean


def _resolve_nick(state: dict[str, Any], nick: str) -> str | None:
    """Nickname to display name: searches username and first_name case-insensitively.

Checks both directions: "blin" matches "Blinchik", and "blinchika" also matches "Blinchik" (inflections in natural speech). Short matches are discarded; otherwise two-letter fragments would create noise on every message."""
    try:
        needle = (nick or "").strip().lstrip("@").lower()
        if len(needle) < 3:
            return None
        bucket = state.get("user_names") or {}
        if not isinstance(bucket, dict):
            return None
        for entry in bucket.values():
            if not isinstance(entry, dict):
                continue
            for field in ("username", "first_name"):
                val = entry.get(field)
                if not isinstance(val, str):
                    continue
                low = val.lower()
                if needle in low or (len(low) >= 4 and low in needle):
                    return _sender_display(entry.get("username"), entry.get("first_name"))
    except Exception:
        pass
    return None


def _parse_index_document(raw: str) -> tuple[list[int], set[int]] | None:
    """Read both the completed index format and the migration-pending extension."""
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if isinstance(parsed, list):
        raw_ids = parsed
        raw_pending: list[object] = []
    elif isinstance(parsed, dict):
        raw_ids = parsed.get("ids")
        raw_pending = parsed.get("legacy_pending", [])
        if not isinstance(raw_ids, list) or not isinstance(raw_pending, list):
            return None
    else:
        return None
    ids: list[int] = []
    for value in raw_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        ids.append(value)
    pending: set[int] = set()
    for value in raw_pending:
        # This extension is an authority boundary: coercing 991001.9 or "991001"
        # could make stale legacy data authoritative for a different, deleted chat.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        pending.add(value)
    if not pending.issubset(ids):
        return None
    return ids, pending


def _index_ids_unlocked(
    *, include_recreated: set[int] | None = None,
    exclude: set[int] | None = None,
) -> list[int]:
    # A pruned chat leaves memory before its remote key is deleted. Keep it discoverable
    # until deletion succeeds so a transient failure cannot orphan the stored snapshot.
    # A recreated chat is different: its old remote snapshot must stay undiscoverable
    # until the replacement snapshot is durable, or a failed first save resurrects it.
    return sorted(
        (
            (set(chat_state.keys())
             | _index_extra_ids
             | _pending_chat_deletions
             | _deleting_chat_ids
             | _legacy_migration_pending_ids)
            - _recreated_chat_ids
            | (include_recreated or set())
        )
        - (exclude or set())
        - _deletion_index_pending_ids
    )


def _index_ids(
    *, include_recreated: set[int] | None = None,
    exclude: set[int] | None = None,
) -> list[int]:
    with _replacement_state_lock:
        return _index_ids_unlocked(include_recreated=include_recreated, exclude=exclude)


def _index_document(
    pending: set[int] | None = None, *, include_recreated: set[int] | None = None,
    exclude: set[int] | None = None,
) -> list[int] | dict[str, object]:
    """Pending migration IDs share the index's atomic write and generation fence."""
    with _replacement_state_lock:
        migration_pending = set(_legacy_migration_pending_ids if pending is None else pending)
        migration_pending -= _deletion_index_pending_ids
        if exclude:
            migration_pending -= exclude
        ids = _index_ids_unlocked(include_recreated=include_recreated, exclude=exclude)
    if migration_pending:
        return {"ids": ids, "legacy_pending": sorted(migration_pending)}
    return ids


def _fetch_index_ids(*, migration_pending: set[int] | None = None) -> set[int] | None:
    """Throttles and reads the remote index WITHOUT mutating shared sets.

Called from to_thread: the previous version changed _index_extra_ids and _chat_snapshot_unloaded directly in a worker thread, and the window between update and clear removed protection from a chat created in the meantime (review 17.11.2026)."""
    global _state_load_failed, _index_retry_last_ts
    if not _state_load_failed:
        return set()
    now = time.monotonic()
    # Zero means "not tried yet": on a fresh runner, monotonic() is under a minute.
    if _index_retry_last_ts and now - _index_retry_last_ts < _INDEX_RETRY_SEC:
        return None
    _index_retry_last_ts = now
    import bot
    try:
        raw = bot._storage_read_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE)
    except Exception as exc:
        log.warning('[state] Chat index retry failed: %s', exc)
        return None
    if raw is None:
        # None means both "there is no key" and "the gateway returned no result". An empty index
        # must not be counted this way (audit 16.11.2026).
        log.warning('[state] Chat index retry returned no body — keeping the write guard.')
        return None
    parsed = _parse_index_document(raw)
    if parsed is None:
        _state_load_failed = True
        log.warning('[state] Chat index is invalid — keeping the write guard.')
        return None
    ids, pending = parsed
    if migration_pending is not None:
        migration_pending.update(pending)
    return set(ids)


def _apply_recovered_ids(ids: set[int], migration_pending: set[int] | None = None) -> bool:
    """Merge the read index with memory — from the loop thread only."""
    global _state_load_failed
    if _replacement_journal_load_failed:
        log.warning('[state] Chat index recovery blocked: replacement journal is unreadable.')
        return False
    # Do not resurrect evicted or deleted ids (second audit pass 16.11.2026).
    ids = {cid for cid in ids if cid not in _pending_chat_deletions and cid not in _deleted_chat_ids}
    recovered_pending = (
        (set(migration_pending or ()) | _legacy_migration_pending_ids)
        - _pending_chat_deletions - _deleted_chat_ids
    )
    _legacy_migration_pending_ids.clear()
    _legacy_migration_pending_ids.update(recovered_pending)
    _index_extra_ids.update(ids)
    # Freeze only those whose snapshot was not read (not in memory).
    _chat_snapshot_unloaded.update(cid for cid in ids if cid not in chat_state)
    # Pending migration snapshots were not read on this path; legacy is not
    # authoritative enough to overwrite whatever per-chat copy may already exist.
    _chat_snapshot_unloaded.update(recovered_pending)
    # Freeze only outage-created chats that actually exist remotely. An ID proven
    # absent is new and must remain writable after its index entry is published.
    existing_outage_chats = _created_during_outage & ids
    _chat_snapshot_unloaded.update(existing_outage_chats)
    # A recovered remote snapshot proves this is an existing chat: remove only the
    # replacement barrier while preserving the freeze and dirty queue.
    with _replacement_state_lock:
        _recreated_chat_ids.difference_update(existing_outage_chats)
        for cid in existing_outage_chats:
            _replacement_generations.pop(cid, None)
    _created_during_outage.clear()
    _state_load_failed = False
    log.info('[state] Chat index recovered after the startup failure: %d remote id(s) merged.', len(ids))
    return True


def _recover_index_ids() -> bool:
    """Synchronous path (shutdown): reading + merging in one thread."""
    if not _state_load_failed:
        return True
    migration_pending: set[int] = set()
    ids = _fetch_index_ids(migration_pending=migration_pending)
    if ids is None:
        return False
    return _apply_recovered_ids(ids, migration_pending)


def _save_chat_index() -> bool:
    """True/False for retry in _flush_state_now: failure was previously lost silently (audit A5-1)."""
    import bot
    try:
        return bot._save_chat_index_payload(json.dumps(_index_document()), generation=_index_generation)
    except Exception as exc:
        log.warning("[state] Saving chat index failed: %s", exc)
        return False

def _save_chat_index_payload(payload: str, *, generation: int | None = None) -> bool:
    """Write one index snapshot; a cancelled flush's late worker cannot replace a newer index."""
    import bot
    with _index_write_lock:
        if generation is not None and generation != _index_generation:
            return False
        try:
            bot._storage_write_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE, payload)
            _note_storage_write()
            return True
        except Exception as exc:
            log.warning("[state] Saving chat index failed: %s", exc)
            return False


def _publish_recreated_chats(saved_ids: set[int]) -> bool:
    """Publish durable replacements, then clear their journals and barriers."""
    import bot
    global _index_dirty, _index_generation
    with _replacement_state_lock:
        candidates = saved_ids & _recreated_chat_ids & set(chat_state)
        candidates -= _pending_chat_deletions | _deleted_chat_ids
    if not candidates:
        return True
    try:
        with _replacement_journal_lock:
            pending = _read_replacement_journal_unlocked()
        with _replacement_state_lock:
            generations: dict[int, str] = {}
            for cid in candidates:
                entry = pending.get(cid)
                generation = (
                    entry[1]
                    if entry is not None and entry[0] == "replacement"
                    else None
                )
                current = _replacement_generations.get(cid)
                if generation is None and current == _replacement_ambiguous_clears.get(cid):
                    generation = current
                generations[cid] = generation
        if any(not generation for generation in generations.values()):
            log.warning("[state] Replacement journal is missing a saved chat; keeping barriers.")
            return False
        _index_dirty = True
        _index_generation += 1
        started_generation = _index_generation
        payload = json.dumps(_index_document(include_recreated=candidates))
        if not bot._save_chat_index_payload(payload, generation=started_generation):
            log.warning("[state] Republishing recreated chat(s) failed, keeping index dirty.")
            return False
        if _index_generation != started_generation:
            return False
        for cid, generation in generations.items():
            if not _clear_replacement_journal(cid, generation):
                log.warning("[state] Clearing replacement journal for chat %s failed.", cid)
        if _index_generation != started_generation:
            return False
        with _replacement_state_lock:
            _index_dirty = bool(candidates & _recreated_chat_ids)
        return not _index_dirty
    except Exception as exc:
        log.warning("[state] Publishing recreated chat(s) failed: %s", exc)
        return False

async def _persist_migration_progress(saved_ids: set[int]) -> None:
    """Publish snapshot completion before allowing legacy-backed chats to be deleted."""
    import bot
    global _index_dirty, _index_generation
    completed = _legacy_migration_pending_ids & saved_ids
    if not completed:
        return
    remaining = _legacy_migration_pending_ids - completed
    # Clear only IDs that have not been mutated again since their snapshot write.
    # A newer dirty mark must survive both the await and successful completion.
    newer_dirty = completed & _dirty_chat_ids
    _dirty_chat_ids.difference_update(completed - newer_dirty)
    _index_dirty = True
    _index_generation += 1
    started_generation = _index_generation
    payload = json.dumps(_index_document(remaining))
    try:
        saved = await asyncio.to_thread(
            bot._save_chat_index_payload, payload, generation=started_generation
        )
    except asyncio.CancelledError:
        _dirty_chat_ids.update(completed)
        raise
    if saved and _index_generation == started_generation:
        _legacy_migration_pending_ids.clear()
        _legacy_migration_pending_ids.update(remaining)
        _index_dirty = False
    else:
        _dirty_chat_ids.update(completed)
        log.warning('[state] Legacy migration completion write failed; keeping deletion blocked.')


def _persist_migration_progress_sync(saved_ids: set[int]) -> None:
    """Synchronous shutdown counterpart of _persist_migration_progress."""
    import bot
    global _index_dirty, _index_generation
    completed = _legacy_migration_pending_ids & saved_ids
    if not completed:
        return
    remaining = _legacy_migration_pending_ids - completed
    _dirty_chat_ids.difference_update(completed)
    _index_dirty = True
    _index_generation += 1
    started_generation = _index_generation
    saved = bot._save_chat_index_payload(
        json.dumps(_index_document(remaining)), generation=started_generation
    )
    if saved and _index_generation == started_generation:
        _legacy_migration_pending_ids.clear()
        _legacy_migration_pending_ids.update(remaining)
        _index_dirty = False
    else:
        _dirty_chat_ids.update(completed)
        log.warning('[state] Legacy migration completion write failed; keeping deletion blocked.')


def _save_quota_payload(payload: str, *, generation: int | None = None) -> bool:
    """Write one quota snapshot unless a newer mutation already invalidated it."""
    import bot
    with _quota_write_lock:
        if generation is not None and generation != _quota_generation:
            return False
        try:
            bot._storage_write_text("lumen:global_quota", GLOBAL_QUOTA_FILE, payload)
            _note_storage_write()
            return True
        except Exception as exc:
            log.warning("[quota] Failed to save global quota: %s", exc)
            return False

# Previously, save_state_to_disk()/save_global_quota() ran synchronously on almost every
# message. The hot path now only marks the specific chat dirty; background coroutines
# write changed chats once per FLUSH_INTERVAL_SEC. Dumping the entire state on every
# message stalled the loop for all chats at once.
_dirty_chat_ids: set[int] = set()
# Legacy chat keys stay unwritten until their discovery index entry is durable.
_unindexed_chat_ids: set[int] = set()
# IDs whose per-chat snapshots still depend on the retained legacy blob.
_legacy_migration_pending_ids: set[int] = set()
_pending_chat_deletions: set[int] = set()
# New and recreated chats stay undiscoverable until their replacement snapshot,
# index and journal commit; otherwise a failed first save leaves an orphaned ID.
_recreated_chat_ids: set[int] = set()
# This completeness marker covers in-flight deletions and replacement recovery.
# Backups must refuse windows with partial memory; unsaved new chats are already complete.
_deleting_chat_ids: set[int] = set()
# A missing snapshot must not be rediscovered by another index writer before its
# durable exclusion and journal clearance finish.
_deletion_index_pending_ids: set[int] = set()
_index_dirty = False
_quota_dirty = False
# Mutation generations guard the race "payload built — write pending — state
# changed”: the flush resets the flag only if the generation has not increased during
# record, otherwise the change would be lost before the next mutation.
_index_generation = 0
# periodic and shutdown index writes share one lock; a cancelled to_thread worker
# otherwise can commit its older payload after the final flush.
_index_write_lock = threading.Lock()
_quota_write_lock = threading.Lock()
_quota_generation = 0
# Startup load failure: while this is set, do not overwrite the index or quota,
# otherwise the first flush would overwrite good remote data with an empty
# snapshot (audit A5-2).
_state_load_failed = False
_replacement_journal_load_failed = False
# A QUOTA READ failure has its own flag: lifting index protection after a successful
# a retry must not unblock a write of an empty quota over bans and
# daily counters (audit 16.11.2026).
_quota_load_failed = False
# IDs read from a deleted index must not return after a startup failure: recovery
# rebuilds the index by merging instead of replacing it with only our chats
# (audit 16.11.2026: new chats received a key, dropped out of the index, and remained
# orphans after a restart).
_index_extra_ids: set[int] = set()
# Chats created in memory while the index was unreadable: we do not have their remote copy
# were seen, so after recovery they are frozen rather than overwritten.
_created_during_outage: set[int] = set()
# ids actually deleted by this process: an index retry must not bring them back.
_deleted_chat_ids: set[int] = set()
_index_retry_last_ts = 0.0
_INDEX_RETRY_SEC = 60.0
# Parallel chat reads at startup: Upstash/disk responds in tens of milliseconds,
# and eight threads reduce cold-start time severalfold without pressure on the quota.
_PARALLEL_CHAT_READS = 8
# Chats whose remote snapshot was NOT restored: their keys must not be overwritten
# a one-line in-memory snapshot; the remote copy is fuller (run-2 audit,
# 08.11.2026).
_chat_snapshot_unloaded: set[int] = set()
FLUSH_INTERVAL_SEC = 10.0
# Serialize flushes with a semaphore: a burst of "dirty" chats would otherwise create hundreds of parallel HTTP requests to Upstash.
def _flush_concurrency() -> int:
    """Lazily via bot._env_number: bare int() crashed the import on garbage input,
    while 0 created a hanging semaphore."""
    import bot
    return bot._env_number("STATE_FLUSH_CONCURRENCY", 10, cast=int, min_value=1)


_state_flush_semaphore: asyncio.Semaphore | None = None

# to_thread survives cancellation of the awaiting flush. Serialize each chat's writes and
# invalidate older snapshots so a late worker cannot overwrite the final shutdown state.
class _StateWriteOrder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0


_state_write_registry_lock = threading.Lock()
# Active callers keep their order alive; idle chats must not leave permanent registry entries.
_state_write_orders: weakref.WeakValueDictionary[int, _StateWriteOrder] = weakref.WeakValueDictionary()


def _state_write_order(chat_id: int) -> _StateWriteOrder:
    with _state_write_registry_lock:
        order = _state_write_orders.get(chat_id)
        if order is None:
            order = _StateWriteOrder()
            _state_write_orders[chat_id] = order
        return order


def _reserve_state_write(chat_id: int) -> tuple[_StateWriteOrder, int]:
    with _state_write_registry_lock:
        order = _state_write_orders.get(chat_id)
        if order is None:
            order = _StateWriteOrder()
            _state_write_orders[chat_id] = order
        order.generation += 1
        return order, order.generation


def _flush_semaphore() -> asyncio.Semaphore:
    """One semaphore per process, created on the first flush."""
    global _state_flush_semaphore
    if _state_flush_semaphore is None:
        _state_flush_semaphore = asyncio.Semaphore(_flush_concurrency())
    return _state_flush_semaphore

async def _save_chat_to_storage_limited(chat_id: int, state: dict[str, Any]) -> bool:
    import bot
    async with _flush_semaphore():
        # The JSON snapshot is taken HERE, not in the stream: dumps without await is atomic for the loop,
        # and live state in to_thread could mutate between iterations (broken snapshot).
        try:
            payload = json.dumps(bot._serialize_chat_state(state), ensure_ascii=False)
        except Exception as exc:
            log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
            return False
        _order, generation = _reserve_state_write(chat_id)
        return await asyncio.to_thread(bot._save_chat_payload, chat_id, payload, generation=generation, order=_order)

def _save_chat_payload(
    chat_id: int, payload: str, *, generation: int | None = None,
    order: _StateWriteOrder | None = None,
) -> bool:
    """Write one ready snapshot unless a newer write or deletion has superseded it."""
    if order is None:
        order = _state_write_order(chat_id)
    with order.lock:
        if generation is not None and generation != order.generation:
            # A newer save may have failed; this worker wrote nothing and must not
            # let migration completion treat the snapshot as durable.
            return False
        return _save_chat_payload_locked(chat_id, payload)


def _save_chat_payload_locked(chat_id: int, payload: str) -> bool:
    """Only blocking I/O that writes a ready snapshot (see above), for to_thread."""
    import bot
    if _state_load_failed:
        # See the shutdown path: an unknown set of remote chats prohibits writes.
        log.debug("[state] Skip chat %s save: chat index unreadable, keeping remote data intact.", chat_id)
        return False
    if chat_id in _chat_snapshot_unloaded:
        # Removing the protection requires a full state reload (process startup);
        # for the lifetime of the process, keep the key; the queue remains dirty.
        log.debug("[state] Skip chat %s save: its remote snapshot was never restored, keeping the remote copy.", chat_id)
        return False
    try:
        with _replacement_state_lock:
            recreated = chat_id in _recreated_chat_ids
        if recreated:
            replacement_generation = _ensure_replacement_generation(chat_id)
            snapshot = json.loads(payload)
            if not isinstance(snapshot, dict):
                raise ValueError("replacement snapshot is not an object")
            snapshot["replacement_generation"] = replacement_generation
            payload = json.dumps(snapshot, ensure_ascii=False)
        bot._storage_write_text(_chat_storage_key(chat_id), bot._chat_storage_path(chat_id), payload)
        _note_storage_write()
        return True
    except Exception as exc:
        log.warning("[state] Saving chat %s failed: %s", chat_id, exc)
        return False

async def _delete_chat_storage_limited(chat_id: int) -> bool:
    import bot
    async with _flush_semaphore():
        # The chat may have been recreated while this deletion waited for a
        # semaphore slot. Never let the stale task supersede that newer state.
        if chat_id in chat_state:
            _cancel_chat_deletion(chat_id)
            return True
        _order, generation = _reserve_state_write(chat_id)
        return await asyncio.to_thread(bot._delete_chat_storage, chat_id, generation=generation, order=_order)

def mark_state_dirty(chat_id: int | None = None) -> None:
    """Marks a chat state as requiring persistence.
An explicit chat_id (the preferred path for new code) marks ONLY
that chat 'dirty', nothing else. A call WITHOUT chat_id (migration from the old
shared blob) marks ALL current chats and the entire index 'dirty'."""
    global _index_dirty, _index_generation
    if chat_id is not None:
        with _replacement_state_lock:
            _replacement_publication_dirty.discard(chat_id)
            _dirty_chat_ids.add(chat_id)
    else:
        with _replacement_state_lock:
            _replacement_publication_dirty.clear()
            _dirty_chat_ids.update(chat_state.keys())
        _index_dirty = True
        _index_generation += 1

def _begin_chat_replacement_locked(chat_id: int) -> None:
    """Hide the ID from the index until the snapshot is durable; journal before writing."""
    unresolved = _unresolved_chat_ids_locked()
    if chat_id not in unresolved and len(unresolved) >= _MAX_UNRESOLVED_CHAT_IDS:
        raise RuntimeError("unresolved chat state capacity reached")
    _recreated_chat_ids.add(chat_id)
    _replacement_generations[chat_id] = uuid.uuid4().hex
    _replacement_ambiguous_clears.pop(chat_id, None)


def _mark_new_chat_id(chat_id: int) -> None:
    """Route a new chat through the replacement barrier before its first snapshot.

    Index-first publication made a crash durable as an ID without a snapshot.
    The journal now precedes the snapshot, and the index is published only after it.
    """
    global _index_dirty, _index_generation
    with _replacement_state_lock:
        _begin_chat_replacement_locked(chat_id)
        _replacement_publication_dirty.discard(chat_id)
        _dirty_chat_ids.add(chat_id)
        _unindexed_chat_ids.discard(chat_id)
    _reserve_state_write(chat_id)
    _index_dirty = True
    _index_generation += 1


def _cancel_chat_deletion(chat_id: int) -> None:
    """A returning chat invalidates queued and in-flight pruning deletes."""
    global _index_dirty, _index_generation
    with _replacement_state_lock:
        _begin_chat_replacement_locked(chat_id)
        _pending_chat_deletions.discard(chat_id)
        _deleting_chat_ids.discard(chat_id)
        _deletion_index_pending_ids.discard(chat_id)
        _deleted_chat_ids.discard(chat_id)
        # Backup completeness already treats this set as unresolved storage work.
        _deleting_chat_ids.add(chat_id)
    _reserve_state_write(chat_id)
    _index_dirty = True
    _index_generation += 1


def mark_quota_dirty() -> None:
    global _quota_dirty, _quota_generation
    _quota_dirty = True
    _quota_generation += 1

async def _flush_dirty_state_once() -> None:
    """The body of ONE iteration of periodic state flushing — extracted from
    _flush_dirty_state (which now only sleeps and calls this function in a loop)
    into a separate function so it can be tested directly without waiting for
    the real FLUSH_INTERVAL_SEC in tests."""
    import bot
    global _index_dirty, _index_generation, _quota_dirty
    attempted_ids: list[int] = []
    failed_ids: set[int] = set()
    try:
        if _pending_chat_deletions:
            to_delete = list(_pending_chat_deletions - (_deleting_chat_ids - _recreated_chat_ids))
            _pending_chat_deletions.difference_update(to_delete)
            revived = {cid for cid in to_delete if cid in chat_state}
            for cid in revived:
                _cancel_chat_deletion(cid)
            to_delete = [cid for cid in to_delete if cid not in revived]
            # Deletion is also a storage write: a frozen chat and a chat during a failure
            # on load retain the deleted copy until a successful read. Previously, protection
            # was only on the write (review 17.11.2026).
            guarded = {cid for cid in to_delete
                       if _state_load_failed or cid in _chat_snapshot_unloaded
                       or cid in _legacy_migration_pending_ids}
            if guarded:
                _pending_chat_deletions.update(guarded)
                to_delete = [cid for cid in to_delete if cid not in guarded]
                log.warning('[state] Skip %d chat deletion(s): remote snapshot not restored yet.',
                            len(guarded))
            _deleting_chat_ids.update(to_delete)
            delete_gather = asyncio.gather(
                *(bot._delete_chat_storage_limited(cid) for cid in to_delete),
                return_exceptions=True,
            )
            cancelled = False
            while True:
                try:
                    # Cancellation must not abandon a running storage thread or
                    # clear the incomplete-state marker before its result is known.
                    del_results = await asyncio.shield(delete_gather)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            try:
                # Failed IDs are returned to the queue for retry instead of being lost on Upstash 5xx/429.
                failed_deletes = {
                    cid for cid, res in zip(to_delete, del_results)
                    if res is not True and cid not in chat_state
                }
                completed_deletes = {
                    cid for cid, res in zip(to_delete, del_results)
                    if res is True and cid not in chat_state
                }
                if completed_deletes:
                    # Only a real delete proves that the old recreated key is gone.
                    _recreated_chat_ids.difference_update(completed_deletes)
                    # The previous index may have retained these IDs while deletion failed.
                    # A successful retry must schedule their removal even if _index_dirty was cleared.
                    _index_dirty = True
                    _index_generation += 1
                if failed_deletes:
                    _pending_chat_deletions.update(failed_deletes)
                    log.warning(
                        '[state] %d chat deletion(s) failed, will retry next cycle: %s',
                        len(failed_deletes), ", ".join(str(c) for c in sorted(failed_deletes)),
                    )
            finally:
                # Keep the marker until failed IDs are queued again; otherwise an
                # export can observe neither pending nor in-flight deletion.
                _deleting_chat_ids.difference_update(set(to_delete) - _recreated_chat_ids)
            if cancelled:
                raise asyncio.CancelledError
        if _state_load_failed and _dirty_chat_ids:
            # Deleted histories are not overwritten: the in-memory file is either a new chat,
            # either everything left after a failed load; the index and quota must
            # are not written there (audit A5-2, 28.10.2026).
            log.warning('[state] Startup load failed; saving chats from memory, index stays untouched.')
        if _state_load_failed or (_index_dirty and not (_recreated_chat_ids & _dirty_chat_ids)):
            # Persist discovery metadata before new per-chat keys. If this write fails,
            # an otherwise successful snapshot would be orphaned after a restart.
            if _state_load_failed:
                # The remote index is newer than empty memory: merge it, never replace it.
                recovered_pending: set[int] = set()
                fetched_ids = await asyncio.to_thread(
                    _fetch_index_ids, migration_pending=recovered_pending
                )
                if fetched_ids is None:
                    log.warning('[state] Skip index save: startup load failed, keeping remote data intact.')
                else:
                    _apply_recovered_ids(fetched_ids, recovered_pending)
                    indexed_new_ids = set(_unindexed_chat_ids)
                    index_payload = json.dumps(_index_document())
                    started_index_generation = _index_generation
                    if await asyncio.to_thread(
                        bot._save_chat_index_payload, index_payload, generation=started_index_generation
                    ):
                        if _index_generation == started_index_generation:
                            _unindexed_chat_ids.difference_update(indexed_new_ids)
                            _index_dirty = False
            else:
                indexed_new_ids = set(_unindexed_chat_ids)
                index_payload = json.dumps(_index_document())
                started_index_generation = _index_generation
                if await asyncio.to_thread(
                    bot._save_chat_index_payload, index_payload, generation=started_index_generation
                ):
                    if _index_generation == started_index_generation:
                        _unindexed_chat_ids.difference_update(indexed_new_ids)
                        _index_dirty = False
        if _dirty_chat_ids:
            to_save = list(_dirty_chat_ids)
            _replacement_publication_dirty.update(set(to_save) & _recreated_chat_ids)
            _dirty_chat_ids.clear()
            # Each chat has its own independent write; if one fails, it does not
            # affect saving the other chats from the same batch.
            attempted_ids = [
                cid for cid in to_save
                if cid in chat_state and (cid not in _unindexed_chat_ids or _state_load_failed)
            ]
            # A failed index write leaves new chats queued, not silently clean.
            _dirty_chat_ids.update(
                cid for cid in set(to_save) - set(attempted_ids)
                if cid in chat_state and cid not in _deleted_chat_ids
            )
            try:
                save_results = await asyncio.gather(
                    *(bot._save_chat_to_storage_limited(cid, chat_state[cid]) for cid in attempted_ids),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                # Cancellation (shutdown/restart) must not lose dirty chats:
                # ids are already removed from the set; return them before exiting (audit 29.10.2026).
                _dirty_chat_ids.update(attempted_ids)
                raise
            # Failed IDs return to the dirty set for retry instead of being lost on Upstash 5xx/429 errors.
            failed_ids = {cid for cid, res in zip(attempted_ids, save_results) if res is not True}
            retry_ids = {
                cid for cid in failed_ids
                if cid in chat_state and cid not in _deleted_chat_ids
            }
            if retry_ids:
                _dirty_chat_ids.update(retry_ids)
                log.warning(
                    '[state] %d chat(s) failed to save this cycle, will retry next: %s',
                    len(retry_ids), ", ".join(str(c) for c in sorted(retry_ids)),
                )
        saved_ids = set(attempted_ids) - failed_ids
        # A saved replacement stays dirty until its index and journal both commit.
        _dirty_chat_ids.update(saved_ids & _recreated_chat_ids)
        if not _state_load_failed:
            published = await asyncio.to_thread(_publish_recreated_chats, saved_ids)
            if not published and (saved_ids & _recreated_chat_ids):
                _index_dirty = True
        await _persist_migration_progress(saved_ids)
        if _quota_dirty:
            import lumen_quotas as lq
            if _quota_load_failed and not await lq.recover_global_quota_async():
                # The remote quota is newer than empty memory: do not overwrite it; the queue stays alive.
                log.warning('[quota] Skip quota save: startup load failed, keeping remote data intact.')
            else:
                quota_payload = json.dumps(GLOBAL_QUOTA, ensure_ascii=False, allow_nan=False)
                started_quota_generation = _quota_generation
                if await asyncio.to_thread(
                    bot._save_quota_payload,
                    quota_payload,
                    generation=started_quota_generation,
                ):
                    if _quota_generation == started_quota_generation:
                        _quota_dirty = False
    except Exception as exc:
        log.warning('[state] Periodic state flush failed: %s', exc, exc_info=True)


async def _flush_dirty_state() -> None:
    import bot
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SEC)
        await bot._flush_dirty_state_once()
def _flush_state_now() -> None:
    """Synchronously flushes all "dirty" state at shutdown; used only when stopping the process (main(), finally): the event loop is stopping anyway, so blocking calls are not a problem, but losing unsaved changes between the last periodic flush tick and container shutdown is."""
    import bot
    global _index_dirty, _index_generation
    # Failure goes back into the queue with a loud log, as in the async flush: silently
    # clearing queues on shutdown would silently lose records (audit A5-1).
    for cid in list(_pending_chat_deletions):
        if cid in chat_state:
            _cancel_chat_deletion(cid)
            continue
        if (_state_load_failed or cid in _chat_snapshot_unloaded
                or cid in _legacy_migration_pending_ids):
            # Shutdown does not bypass deletion protection: a frozen chat retains
            # the deleted copy until a successful read (adversarial pass 17.11.2026).
            log.warning('[state] Shutdown: skip deleting chat %s, snapshot not restored.', cid)
            continue
        try:
            deleted = bot._delete_chat_storage(cid)
        except Exception as exc:
            deleted = False
            log.warning('[state] Deleting chat %s failed: %s', cid, exc)
        if cid in chat_state:
            _cancel_chat_deletion(cid)
            continue
        if deleted is True:
            _pending_chat_deletions.discard(cid)
            _recreated_chat_ids.discard(cid)
            _index_dirty = True
            _index_generation += 1
        else:
            log.warning('[state] Chat %s deletion failed, keeping it queued.', cid)
    if _state_load_failed or (_index_dirty and not (_recreated_chat_ids & _dirty_chat_ids)):
        # Mirror the periodic flush: a new key must be discoverable before it exists.
        indexed_new_ids = set(_unindexed_chat_ids)
        started_index_generation = _index_generation
        index_saved = False
        if _state_load_failed:
            if _recover_index_ids():
                index_saved = bot._save_chat_index() is True
            else:
                log.warning('[state] Skip index save: startup load failed, keeping remote data intact.')
        else:
            try:
                index_saved = bot._save_chat_index() is True
            except Exception as exc:
                index_saved = False
                log.warning('[state] Saving chat index failed: %s', exc)
        if index_saved and _index_generation == started_index_generation:
            _unindexed_chat_ids.difference_update(indexed_new_ids)
            _index_dirty = False
        elif not index_saved:
            log.warning('[state] Chat index save failed, keeping it dirty.')
    saved_ids: set[int] = set()
    _replacement_publication_dirty.update(set(_dirty_chat_ids) & _recreated_chat_ids)
    for cid in list(_dirty_chat_ids):
        if cid in _unindexed_chat_ids:
            continue
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
            saved_ids.add(cid)
            if cid not in _legacy_migration_pending_ids and cid not in _recreated_chat_ids:
                _dirty_chat_ids.discard(cid)
        else:
            log.warning('[state] Chat %s save failed, keeping it dirty.', cid)
    _persist_migration_progress_sync(saved_ids)
    if not _state_load_failed:
        published = _publish_recreated_chats(saved_ids)
        if not published and (saved_ids & _recreated_chat_ids):
            _index_dirty = True

def _restore_pending_replacement_chats() -> set[int]:
    """Recover matching snapshots and identify journaled interrupted deletions."""
    import bot
    global _index_dirty, _index_generation
    interrupted_deletions: set[int] = set()
    for cid in list(_journal_deletion_ids):
        chat_state.pop(cid, None)
        _recreated_chat_ids.add(cid)
        _deleting_chat_ids.add(cid)
        interrupted_deletions.add(cid)
        log.info("[state] Journaled chat %s has a deletion intent; finalizing interrupted deletion.", cid)
    for cid, generation in list(_replacement_generations.items()):
        chat_state.pop(cid, None)
        _recreated_chat_ids.add(cid)
        _deleting_chat_ids.add(cid)
        try:
            raw = bot._storage_read_text(_chat_storage_key(cid), bot._chat_storage_path(cid))
            if raw is None:
                interrupted_deletions.add(cid)
                log.info("[state] Journaled chat %s has no snapshot; finalizing interrupted deletion.", cid)
                continue
            snapshot = json.loads(raw)
            if not _validate_chat_snapshot(snapshot):
                raise ValueError("replacement snapshot is malformed")
            if snapshot.get("replacement_generation") != generation:
                interrupted_deletions.add(cid)
                log.info("[state] Journaled chat %s has a stale snapshot; finalizing interrupted deletion.", cid)
                continue
            bot._restore_single_chat(cid, snapshot)
            if cid not in chat_state:
                raise ValueError("replacement snapshot did not restore")
        except Exception as exc:
            _chat_snapshot_unloaded.add(cid)
            log.warning("[state] Quarantining replacement chat %s: %s", cid, exc)
            continue
        _replacement_publication_dirty.add(cid)
        _dirty_chat_ids.add(cid)
        _index_dirty = True
        _index_generation += 1
        log.info("[state] Recovered replacement chat %s from its journal.", cid)
    return interrupted_deletions


def load_state_from_disk() -> None:
    import bot
    global _state_load_failed, _quota_load_failed, _index_retry_last_ts
    global _replacement_journal_load_failed
    # A new attempt clears the old refusal; a failure below will set it again.
    _state_load_failed = False
    _quota_load_failed = False
    replacement_journal_failed = not _load_replacement_journal()
    _replacement_journal_load_failed = replacement_journal_failed
    if replacement_journal_failed:
        _state_load_failed = True
    _index_extra_ids.clear()
    _unindexed_chat_ids.clear()
    _legacy_migration_pending_ids.clear()
    _created_during_outage.clear()
    _deleted_chat_ids.clear()
    _deletion_index_pending_ids.clear()
    _recreated_chat_ids.clear()
    _replacement_publication_dirty.clear()
    _index_retry_last_ts = 0.0
    _chat_snapshot_unloaded.clear()
    interrupted_deletions = _restore_pending_replacement_chats()
    bot.load_global_quota()
    if replacement_journal_failed:
        # Without the pending set, an old snapshot cannot be distinguished from a
        # replacement. Load nothing and keep every persistence write blocked.
        return

    index_raw = None
    index_read_failed = False
    try:
        index_raw = bot._storage_read_text(CHAT_INDEX_KEY, CHAT_INDEX_FILE)
    except Exception as exc:
        index_read_failed = True
        _state_load_failed = True
        log.warning("[state] Reading chat index failed, falling back to legacy combined blob: %s", exc)

    if index_raw is not None:
        # New format (per-chat keys) — the index already exists; read each
        # the chat separately under its own key.
        parsed_index = _parse_index_document(index_raw)
        if parsed_index is None:
            # The set of deleted chats is unknown: overwriting the index would lose
            # per-chat records absent from the legacy blob (audit 16.11.2026).
            _state_load_failed = True
            log.warning('[state] Failed to parse chat index.')
            chat_ids = None
            legacy_pending: set[int] = set()
        else:
            chat_ids, legacy_pending = parsed_index
            _legacy_migration_pending_ids.update(legacy_pending)
        if isinstance(chat_ids, list):
            loaded_count = 0
            missing_ids: list[int] = []
            ids: list[int] = []
            for chat_id_raw in chat_ids:
                if chat_id_raw in _recreated_chat_ids:
                    continue
                try:
                    ids.append(int(chat_id_raw))
                except Exception:
                    continue

            def _read_one(cid: int) -> tuple[int, object]:
                try:
                    return cid, bot._storage_read_text(_chat_storage_key(cid), bot._chat_storage_path(cid))
                except Exception as exc:
                    return cid, exc

            # Reads run in parallel: sequentially walking 4500 chats one
            # An HTTPS request took minutes of cold start, and throughout that time the webhook
            # returned 503 (audit 16.11.2026). Restoration into memory remains in
            # in this thread: do not touch shared dictionaries from the pool.
            workers = max(1, min(_PARALLEL_CHAT_READS, len(ids)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for cid, raw in pool.map(_read_one, ids):
                    if isinstance(raw, Exception):
                        _chat_snapshot_unloaded.add(cid)
                        _index_extra_ids.add(cid)
                        log.warning('[state] Failed to read chat %s: %s', cid, raw)
                        continue
                    if not raw:
                        _index_extra_ids.add(cid)
                        _chat_snapshot_unloaded.add(cid)
                        missing_ids.append(cid)
                        continue
                    try:
                        parsed_chat = json.loads(raw)
                    except Exception as exc:
                        _chat_snapshot_unloaded.add(cid)
                        _index_extra_ids.add(cid)
                        log.warning('[state] Failed to parse chat state %s: %s', cid, exc)
                        continue
                    try:
                        bot._restore_single_chat(cid, parsed_chat)
                    except Exception as exc:
                        # One corrupt record does not interrupt recovery of the other chats.
                        _chat_snapshot_unloaded.add(cid)
                        _index_extra_ids.add(cid)
                        log.warning('[state] Skip chat %s: restore failed: %s', cid, exc)
                        continue
                    if cid in chat_state:
                        loaded_count += 1
                        if cid in _legacy_migration_pending_ids:
                            # Re-save once so the completion marker can advance safely.
                            _dirty_chat_ids.add(cid)
                    else:
                        # Structurally invalid JSON can be skipped without raising.
                        # Keep its remote key quarantined instead of creating and saving empty state.
                        _chat_snapshot_unloaded.add(cid)
                        _index_extra_ids.add(cid)
                        log.warning('[state] Skip chat %s: snapshot did not restore', cid)
            recoverable_ids = [cid for cid in missing_ids if cid in legacy_pending]
            if recoverable_ids:
                # Only the durable pending set proves that legacy is authoritative.
                # Other missing keys may be deletions and must stay quarantined.
                try:
                    legacy_raw = bot._storage_read_text("lumen:chat_state", STATE_FILE_PATH)
                except Exception as exc:
                    legacy_raw = None
                    log.warning('[state] Failed to read legacy fallback for missing snapshots: %s', exc)
                legacy_chats: object = None
                if legacy_raw:
                    try:
                        legacy_chats = json.loads(legacy_raw)
                    except Exception as exc:
                        log.warning('[state] Failed to parse legacy fallback for missing snapshots: %s', exc)
                if isinstance(legacy_chats, dict):
                    for cid in recoverable_ids:
                        snapshot = legacy_chats.get(str(cid))
                        if snapshot is None:
                            log.warning('[state] Legacy fallback has no snapshot for pending chat %s', cid)
                            continue
                        try:
                            bot._restore_single_chat(cid, snapshot)
                        except Exception as exc:
                            log.warning('[state] Failed to restore legacy fallback for chat %s: %s', cid, exc)
                            continue
                        if cid in chat_state:
                            _chat_snapshot_unloaded.discard(cid)
                            _dirty_chat_ids.add(cid)
                            loaded_count += 1
                        else:
                            log.warning('[state] Legacy fallback did not restore chat %s', cid)
                else:
                    log.warning('[state] Legacy fallback unavailable; keeping pending chats quarantined.')
            log.info("[state] Restored states for %d chats (per-chat storage, %d parallel reads).",
                     loaded_count, workers)
            _finish_interrupted_chat_deletions(interrupted_deletions)
            return
        # The index is corrupt, but per-chat files may be intact; try the legacy blob:
        # better to keep old data than empty chats (found by an external audit).

    # ── Legacy format (one blob for all chats, old key "lumen:chat_state") ──
    # There is no old index — first run on the new format. Restored chats
    # mark them dirty: the very first flush will rewrite them. The old key
    # do not delete — do not touch other people's data during migration.
    try:
        raw = bot._storage_read_text("lumen:chat_state", STATE_FILE_PATH)
        if not raw:
            if index_raw is not None:
                # The index existed but could not be parsed, and there is no legacy: deleted per-chat
                # data is newer than empty memory; the next flush would overwrite it.
                _state_load_failed = True
                log.warning("[state] Chat index unreadable and no legacy blob, keeping remote data intact.")
            _finish_interrupted_chat_deletions(interrupted_deletions)
            return
        loaded = json.loads(raw)
        for chat_id_str, s in loaded.items():
            try:
                cid = int(chat_id_str)
            except Exception:
                # An unidentifiable record cannot be quarantined in the integer index.
                _state_load_failed = True
                log.warning('[state] Legacy chat ID is not an integer; preserving the legacy blob.')
                break
            if cid in _recreated_chat_ids:
                continue
            try:
                bot._restore_single_chat(cid, s)
            except Exception as exc:
                # Keep the legacy-backed ID discoverable without freezing valid siblings.
                chat_state.pop(cid, None)
                _chat_snapshot_unloaded.add(cid)
                _index_extra_ids.add(cid)
                _legacy_migration_pending_ids.add(cid)
                log.warning('[state] Skip chat %s: restore failed: %s', cid, exc)
                continue
            if cid not in chat_state:
                # Structural validation can reject a record without raising.
                _chat_snapshot_unloaded.add(cid)
                _index_extra_ids.add(cid)
                _legacy_migration_pending_ids.add(cid)
                log.warning('[state] Skip chat %s: snapshot did not restore', cid)
        log.info(
            '[state] Restored states for %d chats (migrated from the old shared storage format — will be rewritten in the new per-chat format on next flush).',
            len(chat_state),
        )
        if _state_load_failed or index_read_failed or index_raw is not None:
            # The index was not read (or is corrupt), while per-chat keys may be NEWER than the blob:
            # restored from the blob is suitable only for replies; writing it back
            # is impossible, otherwise an old snapshot would overwrite newer history (audit 16.11.2026).
            for _cid in list(chat_state):
                try:
                    _chat_snapshot_unloaded.add(int(_cid))
                except Exception:
                    continue
            log.warning(
                '[state] Restored %d chats from the legacy blob for answering only; remote per-chat data is kept.',
                len(chat_state),
            )
        else:
            # Legacy migration creates discoverable IDs only when the new index lands.
            _legacy_migration_pending_ids.update(chat_state.keys())
            _unindexed_chat_ids.update(chat_state.keys())
            bot.mark_state_dirty()
    except Exception as exc:
        _state_load_failed = True
        log.warning("[state] Restoring states failed: %s", exc)
    _finish_interrupted_chat_deletions(interrupted_deletions)

def get_state(chat_id: int) -> dict[str, Any]:
    import bot
    if chat_id not in chat_state:
        if chat_id in _pending_chat_deletions or chat_id in _deleted_chat_ids:
            _cancel_chat_deletion(chat_id)
        # Barrier before insertion: when the journal cap is full, a new ID must not
        # remain in memory without protection against orphan-index publication.
        bot._mark_new_chat_id(chat_id)
        if _state_load_failed:
            _created_during_outage.add(chat_id)
        chat_state[chat_id] = {
            "history": [],
            "memory_summary": "",
            "memory_pending": [],
            "memory_epoch": 0,
            "recent_media_ids": {},
            "user_names": {},
            # Wall-clock time: monotonic resets on restart and broke /stats.
            "last_activity": time.time(),
        }
    else:
        # Wall clock: monotonic resets on restart and broke /stats.
        chat_state[chat_id]["last_activity"] = time.time()
    if len(chat_state) > MAX_CHAT_LIMIT:
        # The requested chat may be the only removable entry when memory is full.
        # Never evict it before returning state to its active caller.
        bot._prune_old_chats(keep_chat_id=chat_id)
    return chat_state[chat_id]

def _chat_lang(chat_id: int | None) -> str:
    """Language of system messages for the chat: the state "lang" field, otherwise "en".
    Never fails; any garbage in the field falls back to the default."""
    import bot
    try:
        if chat_id is None:
            return DEFAULT_LANG
        return normalize_lang(bot.get_state(chat_id).get("lang", DEFAULT_LANG))
    except Exception:
        return DEFAULT_LANG

def _t(chat_id: int | None, key: str, **kwargs: Any) -> str:
    """A localized key utterance in the chat language (see lumen_lang.py)."""
    import bot
    return _lang_t(bot._chat_lang(chat_id), key, **kwargs)


def _peek_chat_lang(chat_id: int | None) -> str:
    """The chat language without creating a state entry. Needed for rejections before get_state:
    the normal _chat_lang via get_state would create a chat even for a message
    rejected by the limit — moving the limit check above get_state had no effect."""
    try:
        if chat_id is None:
            return DEFAULT_LANG
        entry = chat_state.get(chat_id) or {}
        return normalize_lang(entry.get("lang", DEFAULT_LANG))
    except Exception:
        return DEFAULT_LANG


def _t_no_create(chat_id: int | None, key: str, **kwargs: Any) -> str:
    """The same localization as _t, but without side effects for storage."""
    return _lang_t(_peek_chat_lang(chat_id), key, **kwargs)

_DELETED_CHAT_IDS_CAP = 5000


def _prune_old_chats(*, keep_chat_id: int | None = None) -> None:
    import bot
    global _index_dirty, _index_generation
    sorted_ids = sorted(chat_state.keys(), key=lambda cid: chat_state[cid].get("last_activity", 0))
    # Lock BUSY = a reply is in progress in the chat. Such a chat cannot be evicted: the active route
    # would keep writing to the detached state, while the next message would create
    # a new lock — two parallel answers in one chat and history corruption
    # (audit 26.09.2026). Such a chat is simply excluded from the sample.
    def _is_busy(cid: int) -> bool:
        lock = bot._chat_locks.get(cid)
        # A queued waiter is accepted work even during the handoff window where
        # the holder released the lock but the waiter has not resumed yet.
        return ((lock is not None and lock.locked())
                or bot._chat_lock_waiters.get(cid, 0) > 0
                or bot._chat_lock_background_waiters.get(cid, 0) > 0)

    # Do not evict a frozen chat: its ID would fall out of the index, and the key
    # would remain orphaned forever (adversarial pass no. 2).
    # A replacement barrier stays in place through journal clearance; pruning
    # that chat concurrently could republish it after a crash.
    with _replacement_state_lock:
        recreated = set(_recreated_chat_ids)
        unresolved = _unresolved_chat_ids_locked()
        capacity = max(0, _MAX_UNRESOLVED_CHAT_IDS - len(unresolved))
    removable = [cid for cid in sorted_ids
                 if cid != keep_chat_id
                 and not _is_busy(cid)
                 and cid not in _chat_snapshot_unloaded
                 and cid not in _legacy_migration_pending_ids
                 and cid not in recreated
                 and not _state_load_failed]
    # Compute the cap from all state, not just the removable selection; otherwise, with
    # under heavy load there was nothing to evict, so chats accumulated without limit
    # (hostile review 27.09.2026). Busy ones are skipped until the next runs.
    to_remove = max(0, len(chat_state) - PRUNED_CHAT_TARGET)
    # At capacity, keep both live chats and existing barriers; unresolved work
    # remains retryable without allowing another prune deletion to grow it.
    removed_ids = removable[:min(to_remove, capacity)]
    for cid in removed_ids:
        chat_state.pop(cid, None)
        _index_extra_ids.discard(cid)
        bot._chat_locks.pop(cid, None)
    # Evicted chats must actually disappear from storage (otherwise their own
    # orphaned keys/files accumulate forever) — queue them for deletion,
    # handled in _flush_dirty_state along with the regular flush.
    _deleted_chat_ids.update(removed_ids)
    # Cap: without it, the set grows for the rest of the process lifetime. Old ids are already
    # removed from storage; fresh ones matter more (review 17.11.2026).
    if len(_deleted_chat_ids) > _DELETED_CHAT_IDS_CAP:
        for extra_id in list(_deleted_chat_ids)[:len(_deleted_chat_ids) - _DELETED_CHAT_IDS_CAP]:
            _deleted_chat_ids.discard(extra_id)
    with _replacement_state_lock:
        _pending_chat_deletions.update(removed_ids)
        _dirty_chat_ids.difference_update(removed_ids)
        _replacement_publication_dirty.difference_update(removed_ids)
        _unindexed_chat_ids.difference_update(removed_ids)
    # Keep the recreation barrier until a real delete or replacement publication succeeds.
    if removed_ids:
        # Do not dirty unchanged chats; otherwise the next flush would rewrite thousands of extra keys.
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
    """Acquire the per-chat lock with a generation check: between get and acquire,
    pruning or eviction may have removed the chat and its lock — the acquired lock
    then belongs to someone else, so release it and retry. Without the check, two
    messages could be answered concurrently, corrupting history (audit A5-10).
    The timeout applies to each attempt, with at most three rounds."""
    import bot
    for _ in range(_CHAT_LOCK_MAX_RETRIES):
        lock = get_chat_lock(chat_id)
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
        if bot._chat_locks.get(chat_id) is lock:
            return lock
        lock.release()
    # Without the generation check, a fourth attempt could acquire another caller's lock:
    # two messages from one chat were then answered in parallel (review 17.11.2026, a hole in fix A5-10).
    raise TimeoutError("chat lock kept changing during acquisition")

def _evict_orphan_chat_locks() -> int:
    """Removes locks for chats that no longer exist in chat_state and are not held by anyone — otherwise _chat_locks grows forever (AUD-G-001). Busy locks are left alone."""
    import bot
    orphan = [cid for cid, lock in bot._chat_locks.items() if cid not in chat_state and not lock.locked()]
    for cid in orphan:
        bot._chat_locks.pop(cid, None)
    return len(orphan)

def _is_owner(user_id: int | None) -> bool:
    """Owner via OWNER_ID; never show model names to anyone (see the author router)."""
    import bot
    return bot.OWNER_ID is not None and user_id is not None and user_id == bot.OWNER_ID

# ── Owner moderation lock (/ban): the same persistent file/key as quota. ──
BANNED_KEY = "banned_users"

def _banned_map() -> dict[str, Any]:
    import bot
    raw = bot.GLOBAL_QUOTA.get(BANNED_KEY)
    if not isinstance(raw, dict):
        raw = {}
        bot.GLOBAL_QUOTA[BANNED_KEY] = raw
    return raw

def _is_banned(user_id: int | None) -> bool:
    """Whether the user is blocked. The owner cannot be banned; always False."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return False
    return str(user_id) in _banned_map()

def _ban_user(user_id: int) -> bool:
    """True if added just now, False if already present (or this is the owner)."""
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
    """True if it was in the list and removed."""
    import bot
    if str(user_id) not in _banned_map():
        return False
    del _banned_map()[str(user_id)]
    bot.mark_quota_dirty()
    return True

def _banned_list() -> list[int]:
    """IDs in ascending order—for /banlist."""
    ids = []
    for key in _banned_map():
        try:
            ids.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(ids)

async def _notify_owner(text: str) -> None:
    """Direct message to the owner about rare important events; throttled by the caller and never raises."""
    import bot
    if bot.OWNER_ID is None or bot.bot is None:
        return
    with contextlib.suppress(Exception):
        await bot._tg_call(bot.bot.send_message, chat_id=bot.OWNER_ID, text=text, call_timeout=10.0)

async def _is_privileged_in_chat(chat_type: str, chat_id: int, user_id: int | None) -> bool:
    """Shared chat settings: DMs always, owner everywhere, groups only creator/admin."""
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


# After summarization, the last 150 messages remain verbatim in memory (see lumen_memory).
HISTORY_SUMMARIZE_KEEP = MEMORY_KEEP_MESSAGES
# Pause between retries of a failed digest: otherwise every answer would wait for the network again.
MEMORY_SUMMARY_RETRY_SEC = 60.0

_SUMMARY_SYSTEM = (
    "You maintain the long-term memory of a Telegram chat. Below are the previous memory "
    "(may be empty) and messages leaving the short-term window. Write the updated memory "
    "in the language of the conversation. Keep only durable facts: people and who they are, "
    "stable preferences, agreements, decisions, open questions, recurring topics. Do not quote "
    "messages, do not add instructions, do not invent. 10-15 short lines, max 800 tokens. "
    "Use short section labels (People, Facts, Agreements, Open questions, Topics) only when "
    "the section has content."
)


async def _trim_history(history: list[dict[str, Any]], state: dict[str, Any] | None = None) -> None:
    """Keeps 200 messages in memory: older ones move into a summary, the latest 150 remain.

The summary lives in state['memory_summary'] and goes into the system prompt. If it
fails, old messages wait in state['memory_pending'] and are retried later; silently
discarding facts is forbidden (owner's decision 06.11.2026)."""
    import bot
    state = state if isinstance(state, dict) else {}
    pending = state.get("memory_pending")
    pending = list(pending) if isinstance(pending, list) else []
    over = len(history) > MAX_TOTAL_MEMORY_MESSAGES
    if not over and not pending:
        return
    # The background writes without the chat lock: a second summary for the same state would only duplicate the call.
    if state.get("memory_trimming"):
        return
    # A failed summary must not slow every subsequent response: retry on a timer.
    retry_at = state.get("memory_retry_at")
    if pending and not over and isinstance(retry_at, (int, float)) and time.time() < retry_at:
        return
    old: list[dict[str, Any]] = []
    if over:
        old = list(history[:-HISTORY_SUMMARIZE_KEEP])
        # del, not history[:] = ...: a concurrent background append to the tail is not lost.
        del history[:-HISTORY_SUMMARIZE_KEEP]
    old = pending + old
    if not old:
        return
    if len(old) > MAX_MEMORY_PENDING:
        log.warning(
            "[history] Pending memory exceeds %d messages; the oldest %d facts are dropped.",
            MAX_MEMORY_PENDING, len(old) - MAX_MEMORY_PENDING,
        )
    # Enqueue BEFORE the network call: a restart during summarization must not
    # lose facts.
    state["memory_pending"] = old[-MAX_MEMORY_PENDING:]
    epoch = state.get("memory_epoch")
    digest, consumed = summary_batch(state.get("memory_summary") or "", old)
    # Summarization runs under the chat lock: it must not exceed the message budget.
    budget = bot.HISTORY_SUMMARY_BUDGET_SEC
    remaining = bot._msg_remaining_sec()
    if remaining is not None:
        budget = max(0.1, min(budget, remaining))
    state["memory_trimming"] = True
    try:
        summary = await asyncio.wait_for(_summarize_text(digest), timeout=budget)
    except Exception as exc:
        summary = ""
        log.warning("[history] Summarization over budget/failed, keeping %d message(s) pending: %s", len(old), exc)
    finally:
        state.pop("memory_trimming", None)
    # /reset during a summary advanced the generation: old facts must not be returned.
    if state.get("memory_epoch") != epoch:
        return
    if summary:
        state["memory_summary"] = summary
        # Only the beginning was consumed: the remainder stays in pending and goes into the next
        # the summary instead of disappearing with the clearing (review 17.11.2026).
        leftover = old[consumed:]
        state["memory_pending"] = leftover[-MAX_MEMORY_PENDING:] if leftover else []
        state.pop("memory_retry_at", None)
    else:
        state["memory_retry_at"] = time.time() + MEMORY_SUMMARY_RETRY_SEC
        log.warning(
            "[history] Summary failed; %d message(s) kept for retry, prompt keeps the last %d.",
            len(state["memory_pending"]), HISTORY_SUMMARIZE_KEEP,
        )


async def _summarize_text(text: str) -> str:
    """Updated chat memory: Gemini flash-lite, then the Munito head; empty means failure."""
    import bot
    from lumen_router_config import _MUNITO_ORDER, _munito_active, _munito_payload_extras
    budget = bot.HISTORY_SUMMARY_BUDGET_SEC
    remaining = bot._msg_remaining_sec()
    if remaining is not None:
        budget = max(0.1, min(budget, remaining))
    deadline = time.monotonic() + budget
    gemini_model = "gemini-2.5-flash-lite"
    # Freeze all three state aspects together: a snapshot while one of them is
    #     being replaced loses the old history.
    if bot.client is not None:
        try:
            from google.genai import types
            gemini_timeout = max(1.0, min(budget * 0.6, 12.0))
            resp = await asyncio.wait_for(
                bot.client.aio.models.generate_content(
                    model=gemini_model,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=text)])],
                    config=types.GenerateContentConfig(
                        system_instruction=_SUMMARY_SYSTEM,
                        temperature=0.2,
                        max_output_tokens=800,
                        # Without thinking: a summary retells facts rather than solving a reasoning task.
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                ),
                timeout=gemini_timeout,
            )
            answer = (getattr(resp, "text", "") or "").strip()
            if answer:
                bot._record_quota_usage("gemini", gemini_model, service=True)
                return answer
        except Exception as exc:
            log.warning("[history] Summary via %s failed, trying Munito: %s", gemini_model, exc)
    if not _munito_active() or time.monotonic() >= deadline:
        return ""
    model = _MUNITO_ORDER[0]
    payload = {
        "messages": [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2, "max_tokens": 800, "stream": False,
    }
    try:
        # Read the request name through bot.: test patching continues to work.
        resp = await bot._munito_request(
            "chat/completions", "POST",
            json_body={**payload, "model": model, **_munito_payload_extras()},
            deadline=deadline,
        )
        choices = resp.get("choices") or []
        answer = bot._or_extract_text(choices[0].get("message") or "").strip() if choices else ""
        if answer:
            bot._record_quota_usage("munito", model, service=True)
            return answer
    except Exception as exc:
        log.warning("[history] Summary via munito/%s failed: %s", model, exc)
    return ""
