"""lumen_state_storage.py handles persistence: the Upstash Redis REST API client,
backend selection (Upstash/file), and serializing one chat into a JSON snapshot.

Write orchestration ("serialize + write + catch") lives in bot.py: tests patch
`bot._storage_write_text` by name, so moving it here would break the patch.
The backend config is passed as a parameter rather than read from module state;
otherwise tests that swap credentials on the fly would stop working.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import urllib.parse
import urllib.request as _urllib_request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("bot")

CHAT_STATE_SCHEMA_VERSION = 2
CHAT_REPLACEMENT_SCHEMA_VERSION = 1
_REPLACEMENT_GENERATION_RE = re.compile(r"[0-9a-f]{32}\Z")
ChatJournalEntry = tuple[str, str | None]


@dataclass(frozen=True)
class StorageConfig:
    """Snapshot of the storage backend configuration for ONE call — rebuilt by the caller
    in bot.py on every call (see the module docstring), rather than cached here, so
    replacing `bot.UPSTASH_REDIS_REST_URL`/`bot._CHATS_DIR` and the like in tests (or,
    eventually, changing configuration without a restart) takes effect immediately,
    with no risk of stale cached values."""
    use_upstash: bool
    upstash_url: str
    upstash_token: str
    chats_dir: Path


# ─────────────────── Upstash Redis REST API client ───────────────────

def _upstash_request(url: str, token: str, command_path: str, *, method: str = "GET", body: bytes | None = None) -> Any:
    """Synchronous request to the Upstash Redis REST API. It deliberately uses urllib.request from the standard library rather than aiohttp or a separate SDK, avoiding a new pip dependency for one integration. Called only from save_*/load_* in bot.py: load_* runs once at startup before traffic, while save_* already runs in a separate thread via asyncio.to_thread (see _flush_dirty_state in bot.py), so this blocking call does not block the event loop."""
    full_url = f"{url}/{command_path}"
    req = _urllib_request.Request(full_url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "text/plain; charset=utf-8")
    with _urllib_request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upstash_set(url: str, token: str, key: str, value: str) -> None:
    _upstash_request(url, token, f"set/{urllib.parse.quote(key, safe='')}", method="POST", body=value.encode("utf-8"))


def _upstash_get(url: str, token: str, key: str) -> str | None:
    result = _upstash_request(url, token, f"get/{urllib.parse.quote(key, safe='')}", method="GET")
    if not isinstance(result, dict) or "result" not in result:
        # A response without a result field is a gateway failure, not "no key": a silent None
        # at startup allowed the first flush to overwrite the remote index (review 17.11.2026).
        raise RuntimeError("upstash response has no result field")
    return result.get("result")


def _upstash_delete(url: str, token: str, key: str) -> None:
    _upstash_request(url, token, f"del/{urllib.parse.quote(key, safe='')}", method="POST")


# single backend branching point

def _storage_write_text(cfg: StorageConfig, key: str, path: Path, text: str) -> None:
    """Single backend branching point: Upstash if configured, otherwise a local file
    (atomically, via .tmp + replace, as before)."""
    if cfg.use_upstash:
        _upstash_set(cfg.upstash_url, cfg.upstash_token, key, text)
        return
    # An external process could remove the chats/ directory; recreate it, or the
    # write fails.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(text)
        # fsync before rename: otherwise a hard kill lost the latest snapshot even though the file
        # had already changed (F-048, 17.11.2026). A filesystem without fsync: suppress.
        with contextlib.suppress(Exception):
            f.flush()
            os.fsync(f.fileno())
    temp_path.replace(path)


def _storage_read_text(cfg: StorageConfig, key: str, path: Path) -> str | None:
    if cfg.use_upstash:
        return _upstash_get(cfg.upstash_url, cfg.upstash_token, key)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _storage_delete_text(cfg: StorageConfig, key: str, path: Path) -> None:
    """Delete a storage entry — required by the per-chat format: when
    _prune_old_chats() in bot.py evicts a chat, its own key/file must actually
    disappear rather than remain orphaned (otherwise Upstash/disk would gradually
    accumulate garbage from long-deleted chats)."""
    if cfg.use_upstash:
        _upstash_delete(cfg.upstash_url, cfg.upstash_token, key)
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


# ─────────────────── per-chat keys/paths and serialization of one chat ───────────────────

def _chat_storage_key(chat_id: int) -> str:
    return f"lumen:chat:{chat_id}"


def _chat_storage_path(cfg: StorageConfig, chat_id: int) -> Path:
    return cfg.chats_dir / f"{chat_id}.json"


def _serialize_chat_state(
    state: dict[str, Any], *, replacement_generation: str | None = None,
) -> dict[str, Any]:
    """Builds a JSON-serializable snapshot of ONE chat, sharing logic between
    persistence and manual export (see /export_state in bot.py).

    Since automatic model routing introduced \"gemini_model\"/
    \"openrouter_text_model\"/\"chat_provider\", these are NO LONGER stored here.
    They used to be explicit user choices through /model and /provider; now the
    provider and model are selected again for every message, so there is no
    reason to store them per chat.
    \"image_model\" was removed for the same reason on August 19, 2026, and
    prompt-based model selection died on 09.11.2026: free Pollinations ignores
    the model parameter (measured /models = [\"sana\"]), so there is nothing to
    keep in state. Old persistent records that still contain these fields
    (created before the corresponding changes) are silently ignored on read;
    see _restore_single_chat in bot.py, which never attempts to read them.
    \"quota\" was removed on August 26, 2026 (technical-debt audit) because the
    field was dead: it was written to ChatState and serialized here but never
    read. Actual quota accounting lives entirely in the module-level
    GLOBAL_QUOTA (bot.py); per-chat quota was never actually used. Old records
    with this field in storage are silently ignored on read, like the other
    removed fields above."""
    # Lazy import: lumen_chat_state imports this module; a top-level import would create a cycle.
    from lumen_memory import MAX_MEMORY_PENDING
    raw_pending = state.get("memory_pending")
    pending = list(raw_pending) if isinstance(raw_pending, list) else []
    snapshot = {
        "schema_version": CHAT_STATE_SCHEMA_VERSION,
        # Unified log: group background, mentions, and bot replies with authors and media links.
        "history": list(state.get("history", [])),
        # The long-term summary and the queue for it survive deployment together with the log.
        "memory_summary": state.get("memory_summary", "") or "",
        "memory_pending": pending[-MAX_MEMORY_PENDING:],
        "recent_media_ids": {
            uid: list(dq) for uid, dq in state.get("recent_media_ids", {}).items()
        },
        # Nicknames survive restarts: without them, after a deploy the bot would not know the authors again.
        "user_names": dict(state.get("user_names", {}) or {}),
        # Wall clock for counting active chats in /stats: monotonic resets
        # on restart and made all chats “active”; time.time() survives it.
        "last_activity": state.get("last_activity", 0.0),
        # The chat's system-message language (see lumen_lang.py, /lang in bot.py).
        # Old snapshots without the field correspond to “en” — see _chat_lang in bot.py.
        "lang": state.get("lang", "en"),
        # Edit/repeat context for the last image: without it, after deployment
        # “add a star” and the “Another option” button cannot find the generation (review 10.11.2026).
        "last_drawn_message_id": state.get("last_drawn_message_id"),
        "last_drawn_at": state.get("last_drawn_at", 0.0),
        "last_drawn_user": state.get("last_drawn_user"),
        "last_draw_prompt": state.get("last_draw_prompt", ""),
        # Versions for “return the previous option”: without them, after deployment, returning
        # replied "no versions" (review 10.11.2026).
        "draw_versions": [
            v for v in list(state.get("draw_versions") or [])[-5:]
            if isinstance(v, dict) and isinstance(v.get("file_id"), str)
        ],
        "draw_redo": [
            v for v in list(state.get("draw_redo") or [])[-5:]
            if isinstance(v, dict) and isinstance(v.get("file_id"), str)
        ],
    }
    if replacement_generation is not None:
        snapshot["replacement_generation"] = replacement_generation
    return snapshot


def _is_replacement_generation(value: Any) -> bool:
    return isinstance(value, str) and _REPLACEMENT_GENERATION_RE.fullmatch(value) is not None


def _validate_replacement_journal(value: Any) -> dict[int, ChatJournalEntry] | None:
    """Parse replacement and deletion intents without coercing IDs or generations."""
    if not isinstance(value, dict) or not _has_finite_json_numbers(value):
        return None
    version = value.get("schema_version")
    if not _is_json_integer(version) or version != CHAT_REPLACEMENT_SCHEMA_VERSION:
        return None
    pending = value.get("pending")
    if not isinstance(pending, dict):
        return None
    result: dict[int, ChatJournalEntry] = {}
    for raw_id, entry in pending.items():
        if not isinstance(raw_id, str):
            return None
        if isinstance(entry, str) and _is_replacement_generation(entry):
            parsed_entry: ChatJournalEntry = ("replacement", entry)
        elif isinstance(entry, dict) and entry == {"kind": "delete"}:
            parsed_entry = ("delete", None)
        else:
            return None
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        if str(chat_id) != raw_id or chat_id in result:
            return None
        result[chat_id] = parsed_entry
    return result


def _is_json_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        try:
            return math.isfinite(value)
        except OverflowError:
            # jq stores numbers as doubles; huge Python integers become infinity there.
            return False
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


_MAX_JSON_DEPTH = 256
_MAX_JSON_NODES = 100_000


def _has_finite_json_numbers(value: Any, *, max_nodes: int = _MAX_JSON_NODES) -> bool:
    """Validate JSON numbers iteratively so hostile depth cannot exhaust the stack."""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > max_nodes:
            return False
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            try:
                if not math.isfinite(item):
                    return False
            except OverflowError:
                return False
        elif isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)
    return True


def _validate_chat_snapshot(snapshot: Any, *, expected_version: int = CHAT_STATE_SCHEMA_VERSION) -> bool:
    """Validate the current per-chat contract before restore or backup export."""
    if not isinstance(snapshot, dict) or not _has_finite_json_numbers(snapshot):
        return False
    if not _is_json_integer(snapshot.get("schema_version")) or snapshot["schema_version"] != expected_version:
        return False
    history = snapshot.get("history")
    pending = snapshot.get("memory_pending")
    # Legacy migration retains model roles and text-only entries.
    if not isinstance(history, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("role"), str)
        and item["role"] in ("user", "assistant", "model")
        and ("content" in item or "text" in item)
        and all(isinstance(item[field], str) for field in ("content", "text") if field in item)
        for item in history
    ):
        return False
    if not isinstance(pending, list) or not all(isinstance(item, dict) for item in pending):
        return False
    media_ids = snapshot.get("recent_media_ids")
    user_names = snapshot.get("user_names")
    if not isinstance(media_ids, dict) or not all(isinstance(value, list) for value in media_ids.values()):
        return False
    if not isinstance(user_names, dict) or not all(isinstance(value, dict) for value in user_names.values()):
        return False
    if not isinstance(snapshot.get("memory_summary"), str) or not isinstance(snapshot.get("lang"), str):
        return False
    if not isinstance(snapshot.get("last_draw_prompt"), str):
        return False
    replacement_generation = snapshot.get("replacement_generation")
    if replacement_generation is not None and not _is_replacement_generation(replacement_generation):
        return False
    for field in ("last_activity", "last_drawn_at"):
        value = snapshot.get(field)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)):
            return False
    for field in ("last_drawn_message_id", "last_drawn_user"):
        value = snapshot.get(field)
        if value is not None and not _is_json_integer(value):
            return False
    for field in ("draw_versions", "draw_redo"):
        versions = snapshot.get(field)
        if (not isinstance(versions, list)
                or not all(isinstance(item, dict) and isinstance(item.get("file_id"), str)
                           for item in versions)):
            return False
    return True


# ─────────────────── date for resetting the daily quota ───────────────────

def _quota_day_reset_in_sec() -> float:
    """Seconds until midnight America/Los_Angeles, when daily counters reset.
    The same timezone as _current_quota_day above."""
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (nxt - now).total_seconds())

_TZ_FALLBACK_LOGGED = False


def _current_quota_day() -> str:
    """Date (ISO, YYYY-MM-DD) used to identify a \"new day\" for quota resets.
    Google resets daily RPD limits at midnight Pacific Time — we use the same
    zone so /stats does not \"reset\" 7-8 hours before or after Google actually
    resets the limit. If timezone data is unavailable in the environment
    (unlikely, but seen in stripped-down Docker images) — quietly fall back
    to UTC: slightly less precise by time of day, but the daily reset still
    happens."""
    global _TZ_FALLBACK_LOGGED
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception as exc:
        # Once per process: otherwise every quota check would write a warning.
        # Fallback shifts the daily reset to 00:00 UTC (03:00 MSK instead of 10:00).
        if not _TZ_FALLBACK_LOGGED:
            _TZ_FALLBACK_LOGGED = True
            log.warning(
                '[quota] ZoneInfo(America/Los_Angeles) unavailable (%s: %s) — quota day falls back to UTC.',
                type(exc).__name__, exc,
            )
        return datetime.now(timezone.utc).date().isoformat()
