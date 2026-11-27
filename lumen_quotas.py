"""lumen_quotas.py — daily provider quotas, daily user limits, and /stats counters in one persistent GLOBAL_QUOTA store."""
from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, TypedDict

from lumen_state_storage import _has_finite_json_numbers, _is_json_integer

log = logging.getLogger("bot")


# GLOBAL_QUOTA is shaped by the TypedDict QuotaEntry (annotation only; no runtime change).
class QuotaEntry(TypedDict):
    used: int
    exhausted_at: float | None
    cooldown_until: float | None

GLOBAL_QUOTA: dict[str, Any] = {
    "gemini": {},
    # Reset used/exhausted at LA midnight: otherwise they grow indefinitely and leave "limit exhausted" stuck (calibration 25.07.2026).
    "quota_day": None,
}
# The only provider list with daily counters: a new provider must
# be here, otherwise its counter does not reset on a new day and is not loaded from a snapshot.
# "image" — images (Cloudflare/Pollinations); the dictionary key is the provider name.
_QUOTA_PROVIDERS = ("gemini", "groq", "munito", "image")
# The shared JSON guard defaults to 100k nodes; 20k daily user entries need about 120k.
_MAX_QUOTA_JSON_NODES = 200_000


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # JSON integers are unbounded in Python but not in jq's double representation.
        return False


def _validate_global_quota(raw: Any) -> bool:
    """Reject structurally unusable quota snapshots before they become a backup."""
    if not isinstance(raw, dict) or not _has_finite_json_numbers(raw, max_nodes=_MAX_QUOTA_JSON_NODES):
        return False
    for provider in _QUOTA_PROVIDERS:
        bucket = raw.get(provider, {})
        if not isinstance(bucket, dict):
            return False
        for entry in bucket.values():
            if not isinstance(entry, dict):
                return False
            if "used" in entry and (not _is_json_integer(entry["used"]) or entry["used"] < 0):
                return False
            for field in ("exhausted_at", "cooldown_until"):
                if field in entry and entry[field] is not None and not _is_finite_number(entry[field]):
                    return False
            if "neurons" in entry and (not _is_finite_number(entry["neurons"]) or entry["neurons"] < 0):
                return False
    daily = raw.get(USER_DAILY_KEY, {})
    if not isinstance(daily, dict) or len(daily) > MAX_USER_DAILY_KEYS:
        return False
    for entry in daily.values():
        if not isinstance(entry, dict):
            return False
        for field in ("total", "gemini", "tts", "draw", "bonus"):
            if field in entry and (not _is_json_integer(entry[field]) or entry[field] < 0):
                return False
    stats = raw.get(STATS_KEY, {})
    if not isinstance(stats, dict):
        return False
    if any(not _is_json_integer(value) or value < 0
           for value in stats.values()):
        return False
    import lumen_chat_state as lcs
    banned = raw.get(lcs.BANNED_KEY, {})
    if not isinstance(banned, dict):
        return False
    if any(not _is_finite_number(value) or value < 0 for value in banned.values()):
        return False
    if "quota_day" in raw and raw["quota_day"] is not None and not isinstance(raw["quota_day"], str):
        return False
    if "last_storage_write_ts" in raw and not _is_finite_number(raw["last_storage_write_ts"]):
        return False
    return True


# Recalculate the date no more than once per minute: calling ZoneInfo every time is expensive.
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

# -1e9, not 0: on a fresh runner monotonic() < 60, and the first daily reset
# was skipped, and yesterday's used/exhausted survived until the next minute (review 17.11.2026).
_last_quota_check_monotonic: float = -1e9


def _reset_quota_if_new_day(*, force: bool = False) -> None:
    """Resets used/exhausted_at for ALL models of ALL providers in _QUOTA_PROVIDERS if a new calendar day has begun since the last reset (America/Los_Angeles). Called both lazily (see _quota_entry — on every quota access) and explicitly once an hour from the background loop in _webhook_startup, so the reset does not depend on whether a new message arrives just after midnight."""
    import bot
    global _last_quota_check_monotonic
    now_mono = time.monotonic()
    if not force and _last_quota_check_monotonic and now_mono - _last_quota_check_monotonic < _QUOTA_CHECK_THROTTLE_SEC:
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
                # Neuron is a daily weight, not a lifetime counter; keeping it blocks /draw after rollover.
                if "neurons" in entry:
                    entry["neurons"] = 0.0
    # Per-user counters use the same day: zeros for everyone, bonus survives
    # (reserved for paid extension). Without bonus, records are not needed until the first
    # tomorrow's message — otherwise the dictionary would grow forever.
    bucket = GLOBAL_QUOTA.get(USER_DAILY_KEY)
    if isinstance(bucket, dict):
        kept = {}
        for uid, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            try:
                bonus = int(entry.get("bonus") or 0)
            except (TypeError, ValueError):
                # Garbage in bonus does not crash the daily reset; count it as zero.
                bonus = 0
            if bonus > 0:
                entry["total"] = 0
                entry["gemini"] = 0
                entry["tts"] = 0
                entry["draw"] = 0
                kept[uid] = entry
        GLOBAL_QUOTA[USER_DAILY_KEY] = kept
    # Daily /stats counters follow the same day: reset to zero along with everything else.
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

def _read_global_quota() -> dict[str, Any] | None:
    """Read and validate a quota snapshot without touching live state."""
    import bot
    import lumen_chat_state as lcs
    raw = bot._storage_read_text("lumen:global_quota", lcs.GLOBAL_QUOTA_FILE)
    if raw is None:
        return None
    if not raw:
        raise ValueError("empty quota snapshot")
    loaded = json.loads(raw)
    if not _validate_global_quota(loaded):
        raise ValueError("malformed quota snapshot")
    return loaded


def _apply_loaded_quota(loaded: dict[str, Any]) -> None:
    """Replace live quota buckets with a validated snapshot on the caller's thread."""
    import lumen_chat_state as lcs
    for provider in _QUOTA_PROVIDERS:
        if provider in loaded:
            if isinstance(loaded[provider], dict):
                clean_provider: dict[str, Any] = {}
                for model_id, entry in loaded[provider].items():
                    sanitized = _sanitize_loaded_entry(entry)
                    if sanitized is not None:
                        clean_provider[model_id] = sanitized
                GLOBAL_QUOTA[provider] = clean_provider
            else:
                log.warning("[quota] Skip provider %s: expected dict, got %s", provider, type(loaded[provider]).__name__)
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
            except (TypeError, ValueError, OverflowError):
                continue
        GLOBAL_QUOTA[USER_DAILY_KEY] = clean
    if "quota_day" in loaded:
        GLOBAL_QUOTA["quota_day"] = loaded["quota_day"]
    raw_stats = loaded.get(STATS_KEY)
    if isinstance(raw_stats, dict):
        clean_stats: dict[str, Any] = {}
        for field in _STATS_FIELDS:
            try:
                clean_stats[field] = max(0, int(raw_stats.get(field) or 0))
            except (TypeError, ValueError, OverflowError):
                clean_stats[field] = 0
        GLOBAL_QUOTA[STATS_KEY] = clean_stats
    raw_banned = loaded.get(lcs.BANNED_KEY)
    if isinstance(raw_banned, dict):
        clean_banned: dict[str, Any] = {}
        for uid, ts in raw_banned.items():
            try:
                clean_banned[str(int(uid))] = max(0, int(ts or 0))
            except (TypeError, ValueError, OverflowError):
                continue
        GLOBAL_QUOTA[lcs.BANNED_KEY] = clean_banned
    raw_ts = loaded.get("last_storage_write_ts")
    try:
        ts_value = float(raw_ts)
    except (TypeError, ValueError):
        ts_value = 0.0
    if not isinstance(raw_ts, bool) and math.isfinite(ts_value) and ts_value > 0:
        GLOBAL_QUOTA["last_storage_write_ts"] = ts_value
    # Missing remote buckets are authoritative; recovery merges the live copy afterward.
    for provider in _QUOTA_PROVIDERS:
        if provider not in loaded:
            GLOBAL_QUOTA[provider] = {}
    if USER_DAILY_KEY not in loaded:
        GLOBAL_QUOTA[USER_DAILY_KEY] = {}
    if STATS_KEY not in loaded:
        GLOBAL_QUOTA[STATS_KEY] = {field: 0 for field in _STATS_FIELDS}


def load_global_quota() -> bool:
    """Load a remote quota object and report whether it replaced live state."""
    import bot
    import lumen_chat_state as lcs
    try:
        loaded = _read_global_quota()
        if loaded is None:
            return False
        _apply_loaded_quota(loaded)
        lcs._quota_load_failed = False
        bot._reset_quota_if_new_day(force=True)
    except Exception as exc:
        lcs._quota_load_failed = True
        log.warning("[quota] Failed to load global quota: %s", exc)
        # A malformed snapshot must not mutate live state; transport failures
        # retain the normal lazy daily rollover behavior.
        if not isinstance(exc, ValueError):
            bot._reset_quota_if_new_day()
    return not lcs._quota_load_failed

_quota_retry_last_ts = 0.0
_QUOTA_RETRY_SEC = 60.0
_quota_recovery_read_only = contextvars.ContextVar("quota_recovery_read_only", default=False)


def _sanitize_loaded_entry(entry: Any) -> dict[str, Any] | None:
    """Quota with Infinity/unknown: count the message as used so
    nonempty counters do not trigger quota writes from a second worker (review 17.11.2026)."""
    if not isinstance(entry, dict):
        return None
    clean = dict(entry)
    for field in ("used", "neurons"):
        if field not in clean:
            continue
        try:
            value = float(clean.get(field) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value) or value < 0:
            value = 0.0
        clean[field] = int(value) if field == "used" else value
    for field in ("exhausted_at", "cooldown_until"):
        if field not in clean:
            continue
        try:
            value = float(clean.get(field) or 0)
        except (TypeError, ValueError):
            value = 0.0
        clean[field] = value if math.isfinite(value) and value > 0 else None
    return clean


def _merge_recovered_quota(local: dict[str, Any]) -> bool:
    """Merge outage mutations without exceeding the daily user capacity."""
    import lumen_chat_state as lcs
    # Bans are not tied to days: always merge them, or a ban placed
    # during a failure, disappears when midnight is crossed (adversarial pass 17.11.2026).
    for uid, ts in (local.get(lcs.BANNED_KEY) or {}).items():
        if uid not in (GLOBAL_QUOTA.get(lcs.BANNED_KEY) or {}):
            GLOBAL_QUOTA.setdefault(lcs.BANNED_KEY, {})[uid] = ts
    if local.get("quota_day") != GLOBAL_QUOTA.get("quota_day"):
        return True
    remote_daily = GLOBAL_QUOTA.setdefault(USER_DAILY_KEY, {})
    local_daily = local.get(USER_DAILY_KEY)
    if not isinstance(local_daily, dict):
        local_daily = {}

    def _restore_local() -> None:
        remote_bans = GLOBAL_QUOTA.get(lcs.BANNED_KEY)
        GLOBAL_QUOTA.clear()
        GLOBAL_QUOTA.update(local)
        if isinstance(remote_bans, dict):
            banned = GLOBAL_QUOTA.get(lcs.BANNED_KEY)
            if not isinstance(banned, dict):
                banned = {}
                GLOBAL_QUOTA[lcs.BANNED_KEY] = banned
            for uid, ts in remote_bans.items():
                banned.setdefault(uid, ts)

    protected = {
        uid for bucket in (remote_daily, local_daily)
        for uid, entry in bucket.items()
        if not _user_daily_entry_is_idle(entry)
    }
    if len(protected) > MAX_USER_DAILY_KEYS:
        # Dropping any of these entries would restore a spent daily allowance.
        _restore_local()
        lcs._quota_load_failed = True
        log.warning("[quota] Recovery exceeds daily user capacity; keeping local state")
        return False

    # Reserve all required slots in one pass. Scanning victims for every local
    # user turns a full 20k recovery into quadratic event-loop blocking.
    local_only_used = {
        uid for uid, entry in local_daily.items()
        if uid not in remote_daily and not _user_daily_entry_is_idle(entry)
    }
    drop = max(0, len(remote_daily) + len(local_only_used) - MAX_USER_DAILY_KEYS)
    if drop:
        victims = [
            old for old, value in remote_daily.items()
            if _user_daily_entry_is_idle(value)
        ]
        for old in victims[:drop]:
            remote_daily.pop(old, None)

    for uid, entry in local_daily.items():
        if not isinstance(entry, dict):
            continue
        cur = remote_daily.get(uid)
        if not isinstance(cur, dict):
            if len(remote_daily) >= MAX_USER_DAILY_KEYS:
                if _user_daily_entry_is_idle(entry):
                    continue
                _restore_local()
                lcs._quota_load_failed = True
                log.warning("[quota] Recovery cannot place a used daily entry")
                return False
            remote_daily[uid] = dict(entry)
            continue
        for field in ("total", "gemini", "tts", "draw"):
            try:
                # Sum, not max: while storage writes are disabled during a failure,
                # and local counters — this is usage that is not in the snapshot.
                cur[field] = int(cur.get(field) or 0) + int(entry.get(field) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
        # bonus is a daily allocation, not usage: adding it to the remote
        # would mean issuing the limit twice (adversarial pass 2).
        try:
            cur["bonus"] = max(int(cur.get("bonus") or 0), int(entry.get("bonus") or 0))
        except (TypeError, ValueError, OverflowError):
            pass
    remote_stats = GLOBAL_QUOTA.setdefault(STATS_KEY, {field: 0 for field in _STATS_FIELDS})
    local_stats = local.get(STATS_KEY) or {}
    for field in _STATS_FIELDS:
        try:
            remote_stats[field] = int(remote_stats.get(field) or 0) + int(local_stats.get(field) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
    for provider in _QUOTA_PROVIDERS:
        local_models = local.get(provider) or {}
        if not isinstance(local_models, dict):
            continue
        remote_models = GLOBAL_QUOTA.setdefault(provider, {})
        for model, entry in local_models.items():
            if not isinstance(entry, dict):
                continue
            cur = remote_models.get(model)
            if not isinstance(cur, dict):
                remote_models[model] = dict(entry)
                continue
            for field in ("used", "neurons"):
                if field not in entry and field not in cur:
                    continue
                try:
                    if field == "used":
                        cur[field] = int(cur.get(field) or 0) + int(entry.get(field) or 0)
                    else:
                        cur[field] = float(cur.get(field) or 0.0) + float(entry.get(field) or 0.0)
                except (TypeError, ValueError, OverflowError):
                    continue
            for field in ("exhausted_at", "cooldown_until"):
                try:
                    merged = max(float(cur.get(field) or 0.0), float(entry.get(field) or 0.0))
                except (TypeError, ValueError):
                    continue
                cur[field] = merged if math.isfinite(merged) and merged > 0 else None
    return True


def _quota_recovery_due() -> bool:
    global _quota_retry_last_ts
    now = time.monotonic()
    if _quota_retry_last_ts and now - _quota_retry_last_ts < _QUOTA_RETRY_SEC:
        return False
    _quota_retry_last_ts = now
    return True


def _apply_recovered_quota(loaded: dict[str, Any]) -> bool:
    """Merge a recovered snapshot with mutations made while its read was pending."""
    import bot
    import lumen_chat_state as lcs
    local = copy.deepcopy(GLOBAL_QUOTA)
    if not _validate_global_quota(loaded):
        lcs._quota_load_failed = True
        return False
    _apply_loaded_quota(loaded)
    bot._reset_quota_if_new_day(force=True)
    if not _merge_recovered_quota(local):
        return False
    lcs._quota_load_failed = False
    return True


async def recover_global_quota_async() -> bool:
    """Offload only quota I/O and parsing, then apply and merge on the event loop."""
    import bot
    import lumen_chat_state as lcs
    if not lcs._quota_load_failed:
        return True
    if not _quota_recovery_due():
        return False
    token = _quota_recovery_read_only.set(True)
    try:
        result = await asyncio.to_thread(bot.recover_global_quota)
    except Exception as exc:
        result = exc
    finally:
        _quota_recovery_read_only.reset(token)
    if isinstance(result, Exception):
        lcs._quota_load_failed = True
        log.warning("[quota] Retry failed: %s", result)
        bot._reset_quota_if_new_day()
        return False
    if result is False:
        return False
    if result is None:
        bot._reset_quota_if_new_day()
        return not lcs._quota_load_failed
    if not isinstance(result, dict):
        lcs._quota_load_failed = True
        log.warning("[quota] Retry returned an invalid quota snapshot.")
        return False
    return _apply_recovered_quota(result)


def recover_global_quota() -> Any:
    """Recover synchronously, or only read when called by the async recovery worker."""
    import lumen_chat_state as lcs
    if _quota_recovery_read_only.get():
        try:
            return _read_global_quota()
        except Exception as exc:
            return exc
    if not lcs._quota_load_failed:
        return True
    if not _quota_recovery_due():
        return False
    local = copy.deepcopy(GLOBAL_QUOTA)
    try:
        loaded_snapshot = load_global_quota()
    except Exception as exc:
        log.warning("[quota] Retry failed: %s", exc)
        loaded_snapshot = False
    if loaded_snapshot and not lcs._quota_load_failed:
        _merge_recovered_quota(local)
    return not lcs._quota_load_failed


def save_global_quota() -> None:
    import bot
    import lumen_chat_state as lcs
    if lcs._quota_load_failed:
        # A worker cannot safely merge live counters; the event-loop flush owns recovery.
        log.warning('[quota] Skip quota save: startup load failed, keeping remote data intact.')
        return
    try:
        with lcs._quota_write_lock:
            # Invalidate any periodic payload already queued in to_thread.
            lcs._quota_generation += 1
            started_generation = lcs._quota_generation
            payload = json.dumps(GLOBAL_QUOTA, ensure_ascii=False, allow_nan=False)
            bot._storage_write_text("lumen:global_quota", lcs.GLOBAL_QUOTA_FILE, payload)
            lcs._note_storage_write()
            if lcs._quota_generation == started_generation:
                lcs._quota_dirty = False
    except Exception as exc:
        log.warning("[quota] Failed to save global quota: %s", exc)


def _quota_entry(provider: str, model_id: str) -> QuotaEntry:
    import bot
    bot._reset_quota_if_new_day()
    sub = GLOBAL_QUOTA.setdefault(provider, {})
    return sub.setdefault(model_id, {"used": 0, "exhausted_at": None, "cooldown_until": None})

# How long the model remains silent after a ONE-MINUTE 429 (requests-per-minute limit), before its
# try again. Previously a one-minute spike set the mark until midnight, and the model dropped out
# from the route for the rest of the day (adversarial review 27.09.2026): there was no way to clear the mark,
# because a known-dead model is never called, while a successful answer clears the marker.
# 10 minutes with ample margin survive Munito's 30-second window (200 requests).
QUOTA_RATE_LIMIT_COOLDOWN_SEC = 600.0

def _mark_quota_exhausted(provider: str, model_id: str) -> None:
    """Daily exhausted marker: used only grows on successes; without it, a 429 looked like 0."""
    import bot
    e = bot._quota_entry(provider, model_id)
    e["exhausted_at"] = time.time()
    e["cooldown_until"] = None
    bot.mark_quota_dirty()

def _mark_rate_limited(provider: str, model_id: str) -> None:
    """Per-minute request limit: a short pause instead of a daily mark. The model returns
    to the route automatically after QUOTA_RATE_LIMIT_COOLDOWN_SEC — no need to
    wait a day."""
    import bot
    e = bot._quota_entry(provider, model_id)
    e["cooldown_until"] = time.time() + QUOTA_RATE_LIMIT_COOLDOWN_SEC
    bot.mark_quota_dirty()

def _record_quota_usage(provider: str, model_id: str, *, service: bool = False) -> None:
    """Service calls (history summaries, transcription — service=True) are not written to the quota: otherwise /stats lies, and exhausted triggers fire for things other than answers to people."""
    import bot
    if service:
        log.debug("[quota] Service call %s/%s not counted.", provider, model_id)
        return
    e = bot._quota_entry(provider, model_id)
    e["used"] = int(e.get("used") or 0) + 1
    e["exhausted_at"] = None
    e["cooldown_until"] = None
    bot.mark_quota_dirty()

# ── Daily budget and image-provider pause ──
# The same QuotaEntry as for text models: used on success, exhausted_at until tomorrow,
# cooldown_until for a brief overload. Do not create separate storage.
IMAGE_QUOTA_PROVIDER = "image"
# One budget "unit" is 104.2 Neuron: this is what one klein-4b generation costs
# (80 generations = 8336 Neuron out of 10 000 free). Editing costs more: plus 21 Neuron
# per input megapixel, so a 12 MP photo ≈ 3.4 units (audit 16.11.2026).
IMAGE_UNIT_NEURONS = 104.2


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


def _image_neurons_total() -> float:
    """Neuron spent during the quota day. An old snapshot without a weight is counted by
    number of images: the upgrade must not reset an already spent budget."""
    import bot
    bot._reset_quota_if_new_day()
    sub = GLOBAL_QUOTA.get(IMAGE_QUOTA_PROVIDER)
    if not isinstance(sub, dict):
        return 0.0
    total = 0.0
    for entry in sub.values():
        if not isinstance(entry, dict):
            continue
        try:
            neurons = entry.get("neurons")
            if neurons is None:
                total += max(0, int(entry.get("used") or 0)) * IMAGE_UNIT_NEURONS
            else:
                total += max(0.0, float(neurons))
        except (TypeError, ValueError):
            # Broken neurons must not hide spending: count by the number of images
            # (review 17.11.2026).
            try:
                total += max(0, int(entry.get("used") or 0)) * IMAGE_UNIT_NEURONS
            except (TypeError, ValueError):
                continue
    return total


# R4-04 (audit 26.11.2026): provisional image-budget reservation. The check ran before
# the network and the charge (_note_image_success) after the response, so two concurrent
# requests could pass the last slot together. asyncio is single-threaded, making synchronous
# reserve/release atomic without a lock. Success and refusal use the same weight; the caller
# stores units itself. Pending weight survives a day change: it is not persisted and briefly
# overstates the new day until open attempts release it in their finally blocks.
_pending_image_neurons = 0.0


def _reserve_image_neurons(units: float) -> bool:
    """True — there is still budget for this generation, and the weight is already reserved.
    For non-owners only: the owner has no limit and needs no reserve."""
    global _pending_image_neurons
    import bot
    weight = max(0.0, float(units)) * IMAGE_UNIT_NEURONS
    if _image_neurons_total() + _pending_image_neurons + weight > float(bot.IMAGE_DAILY_NEURONS):
        return False
    _pending_image_neurons += weight
    return True


def _release_image_neurons(units: float) -> None:
    global _pending_image_neurons
    _pending_image_neurons = max(0.0, _pending_image_neurons - max(0.0, float(units)) * IMAGE_UNIT_NEURONS)


def _image_budget_exhausted() -> bool:
    """Shared daily image budget in Neuron: rejection before contacting the provider. Previously, images were counted by item, so a heavy 12 MP edit consumed one third of the budget while remaining one of 80 (audit 16.11.2026). The owner has no limit. R4-04 reserves are accounted for — otherwise a peak wave would start on top of the selected budget."""
    import bot
    return _image_neurons_total() + _pending_image_neurons >= bot.IMAGE_DAILY_NEURONS


def _image_provider_block_reason(provider: str) -> str | None:
    """None — may be retried. \"budget\" — daily budget exhausted (until the next day); \"cooldown\" — recent overload. Snapshot values may be garbage after a corrupt write, so each conversion is wrapped in try/except rather than using a bare float()."""
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


def _note_image_success(provider: str, *, units: float = 1.0) -> None:
    """used++ for /stats and the per-user limit, plus Neuron weight for the provider budget.
The unit is one generation (104.2 Neuron); an edit passes its weight with megapixels."""
    import bot
    entry = bot._quota_entry(IMAGE_QUOTA_PROVIDER, provider)
    try:
        used = int(entry.get("used") or 0)
    except (TypeError, ValueError):
        used = 0
    try:
        neurons = float(entry.get("neurons") or 0.0)
    except (TypeError, ValueError):
        neurons = 0.0
    entry["used"] = used + 1
    entry["neurons"] = neurons + max(0.0, float(units)) * IMAGE_UNIT_NEURONS
    entry["exhausted_at"] = None
    entry["cooldown_until"] = None
    bot.mark_quota_dirty()


def _note_image_rate_limited(provider: str, *, retry_after_sec: float | None = None) -> None:
    """Provider pause: use the service's Retry-After, but not less than IMAGE_PROVIDER_COOLDOWN_SEC."""
    import bot
    entry = bot._quota_entry(IMAGE_QUOTA_PROVIDER, provider)
    try:
        wait = float(retry_after_sec or 0.0)
    except (TypeError, ValueError):
        wait = 0.0
    if not math.isfinite(wait) or wait < 0:
        wait = 0.0
    # Daily cap: a bogus Retry-After must not disable images
    # forever — the quota survives restart (review 17.11.2026).
    wait = min(max(float(bot.IMAGE_PROVIDER_COOLDOWN_SEC), wait), 86400.0)
    entry["cooldown_until"] = time.time() + wait
    bot.mark_quota_dirty()


def _note_image_budget_exhausted(provider: str) -> None:
    import bot
    bot._mark_quota_exhausted(IMAGE_QUOTA_PROVIDER, provider)


# ── Daily counters for /stats (received/answered/refused) ──
# They live inside GLOBAL_QUOTA: the same file/key and the same flush, with no separate entry
# per message. Day boundaries are the same (quota_day, PT).
STATS_KEY = "stats"
_STATS_FIELDS = (
    "messages_received",
    "answers_sent",
    "all_failed",
    "fallbacks",
    "daily_limit_denials",
    # 08.10.2026: every model attempt and every empty response (exception/empty before
    # shown) — they measure the route's actual efficiency instead of guessing from logs.
    "model_attempts",
    "model_failures",
)


def _stats_entry() -> dict[str, Any]:
    """Live daily-counter record (created with zeros on first access)."""
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
    """Increment the name counter by one. Ignore unrelated names: a typo in a call must
    not inflate the persistent dictionary.
    """
    import bot
    if name not in _STATS_FIELDS:
        return
    entry = _stats_entry()
    entry[name] = int(entry.get(name) or 0) + 1
    bot.mark_quota_dirty()

# ── Per-user daily limits (by user_id, not by chat) ──
# Stored inside GLOBAL_QUOTA["user_daily"]: the same persistent file/key and
# the same batch flush once per FLUSH_INTERVAL_SEC — not a separate Upstash write for
# every message. Day boundaries match the quota (quota_day, PT).
USER_DAILY_KEY = "user_daily"
# Daily record cap: there are orders of magnitude fewer legitimate users per day.
MAX_USER_DAILY_KEYS = 20000


def _user_daily_entry_is_idle(value: Any) -> bool:
    """Only entries without usage or bonus can be evicted without restoring quota."""
    if not isinstance(value, dict):
        return False
    for field in ("total", "gemini", "tts", "draw", "bonus"):
        try:
            if int(value.get(field) or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


@dataclass(slots=True)
class _UserDailyReservation:
    """Opaque refund token carrying the quota day and unrefunded amounts."""

    user_id: int
    quota_day: str
    total: int
    tts: int
    draw: int
    claimed: bool = False
    gemini_counted: bool = False
    tts_counted: bool = False


_user_daily_reservation: contextvars.ContextVar[_UserDailyReservation | None] = contextvars.ContextVar(
    "user_daily_reservation", default=None,
)


class _UserDailyCapacityError(RuntimeError):
    """The daily user bucket has no safe slot for another account."""


def _user_daily_entry(user_id: int | None) -> dict[str, Any]:
    """Return a normalized entry, creating one only while the hard cap allows it."""
    import bot
    bot._reset_quota_if_new_day()
    bucket = GLOBAL_QUOTA.setdefault(USER_DAILY_KEY, {})
    key = str(user_id)
    entry = bucket.get(key)
    if not isinstance(entry, dict):
        if len(bucket) >= MAX_USER_DAILY_KEYS:
            # Used counters and bonuses must survive the day; evicting them would
            # restore quota. Only unused entries can make room for a new account.
            drop = max(1, len(bucket) - int(MAX_USER_DAILY_KEYS * 0.9))

            victims = [
                old for old, value in bucket.items()
                if _user_daily_entry_is_idle(value)
            ]
            for old in victims[:drop]:
                bucket.pop(old, None)
        if len(bucket) >= MAX_USER_DAILY_KEYS:
            raise _UserDailyCapacityError
        entry = {}
        bucket[key] = entry
    else:
        # Keep LRU order so the oldest unused entries are reclaimed first.
        bucket[key] = bucket.pop(key)
    for field in ("total", "gemini", "tts", "draw", "bonus"):
        try:
            entry[field] = max(0, int(entry.get(field) or 0))
        except (TypeError, ValueError):
            entry[field] = 0
    return entry


def _user_daily_peek(user_id: int | None) -> dict[str, Any] | None:
    """Read without creating: probing the limit does not create an entry or inflate day_users."""
    import bot
    bot._reset_quota_if_new_day()
    bucket = GLOBAL_QUOTA.get(USER_DAILY_KEY)
    if not isinstance(bucket, dict):
        return None
    entry = bucket.get(str(user_id))
    return entry if isinstance(entry, dict) else None


def _user_daily_admission_blocked(user_id: int | None) -> bool:
    """Report a capacity or integrity refusal separately from a personal limit."""
    import bot
    import lumen_chat_state as lcs
    if user_id is None or bot._is_owner(user_id):
        return False
    if lcs._quota_load_failed:
        return True
    bucket = GLOBAL_QUOTA.get(USER_DAILY_KEY)
    return (
        isinstance(bucket, dict)
        and str(user_id) not in bucket
        and len(bucket) >= MAX_USER_DAILY_KEYS
    )


def _user_daily_limit(user_id: int | None, kind: str, entry: dict[str, Any] | None = None) -> int:
    """Effective limit including the bonus allowance (env base + bonus)."""
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
    """Whether the overall daily limit is exhausted. The owner and an unknown author are
unlimited."""
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
    """Record a successful answer, consuming a bound reservation exactly once."""
    import bot
    if user_id is None or bot._is_owner(user_id):
        return
    reservation = _user_daily_reservation.get()
    if isinstance(reservation, _UserDailyReservation) and reservation.user_id == user_id:
        try:
            entry = _user_daily_entry(user_id)
        except _UserDailyCapacityError:
            log.warning("[quota] Daily user capacity reached before success accounting")
            return
        if not reservation.claimed:
            reservation.claimed = True
            reservation.total = 0
            reservation.tts = 0
        if gemini and not reservation.gemini_counted:
            entry["gemini"] = int(entry.get("gemini") or 0) + 1
            reservation.gemini_counted = True
        if tts and not reservation.tts_counted:
            entry["tts"] = int(entry.get("tts") or 0) + 1
            reservation.tts_counted = True
        bot.mark_quota_dirty()
        return
    try:
        entry = _user_daily_entry(user_id)
    except _UserDailyCapacityError:
        log.warning("[quota] Daily user capacity reached before legacy success accounting")
        return
    entry["total"] = int(entry.get("total") or 0) + 1
    if gemini:
        entry["gemini"] = int(entry.get("gemini") or 0) + 1
    if tts:
        entry["tts"] = int(entry.get("tts") or 0) + 1
    bot.mark_quota_dirty()


def _reserve_user_daily(user_id: int | None, *, total: int = 0, tts: int = 0, draw: int = 0) -> _UserDailyReservation | None:
    """Atomically reserve usage and return the token required to refund it.

    The synchronous check and mutation protect one event-loop process, not replicas.
    """
    import bot
    if user_id is None or bot._is_owner(user_id):
        # Unlimited callers still need a truthy token so existing command flows proceed.
        return _UserDailyReservation(user_id or 0, str(GLOBAL_QUOTA.get("quota_day") or ""), 0, 0, 0)
    # Untrusted quota state cannot safely admit new paid work.
    import lumen_chat_state as lcs
    if lcs._quota_load_failed:
        return None
    # Reservation boundaries cannot inherit the ordinary one-minute rollover delay.
    bot._reset_quota_if_new_day(force=True)
    try:
        entry = _user_daily_entry(user_id)
    except _UserDailyCapacityError:
        return None
    used_total = int(entry.get("total") or 0)
    used_tts = int(entry.get("tts") or 0)
    used_draw = int(entry.get("draw") or 0)
    if total and used_total + total > _user_daily_limit(user_id, "total", entry):
        return None
    if tts and used_tts + tts > _user_daily_limit(user_id, "tts", entry):
        return None
    if draw and used_draw + draw > _user_daily_limit(user_id, "draw", entry):
        return None
    entry["total"] = used_total + total
    if tts:
        entry["tts"] = used_tts + tts
    if draw:
        entry["draw"] = used_draw + draw
    bot.mark_quota_dirty()
    return _UserDailyReservation(user_id, GLOBAL_QUOTA["quota_day"], total, tts, draw)


def _bind_user_daily_reservation(user_id: int | None, reservation: _UserDailyReservation) -> bool:
    """Bind a reservation to the current task and refund it if the task exits unused."""
    if not isinstance(reservation, _UserDailyReservation) or reservation.user_id != (user_id or 0):
        return False
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return False
    if task is None:
        return False
    _user_daily_reservation.set(reservation)

    def _refund_on_done(_task: asyncio.Task) -> None:
        _release_user_daily_reservation(user_id, reservation)

    task.add_done_callback(_refund_on_done)
    return True


def _release_user_daily_reservation(user_id: int | None, reservation: _UserDailyReservation) -> None:
    """Clear the task binding and refund only the reservation's unused remainder."""
    if _user_daily_reservation.get() is reservation:
        _user_daily_reservation.set(None)
    _refund_user_daily(
        user_id, reservation=reservation,
        total=reservation.total, tts=reservation.tts, draw=reservation.draw,
    )


def _refund_user_daily(
    user_id: int | None, *, reservation: _UserDailyReservation,
    total: int = 0, tts: int = 0, draw: int = 0,
) -> None:
    """Refund only the unused remainder of a reservation from the current quota day."""
    import bot
    if user_id is None or bot._is_owner(user_id) or not isinstance(reservation, _UserDailyReservation):
        return
    if not total and not tts and not draw:
        return
    bot._reset_quota_if_new_day(force=True)
    if reservation.user_id != user_id or reservation.quota_day != GLOBAL_QUOTA.get("quota_day"):
        return
    refund_total = min(max(0, total), reservation.total)
    refund_tts = min(max(0, tts), reservation.tts)
    refund_draw = min(max(0, draw), reservation.draw)
    reservation.total -= refund_total
    reservation.tts -= refund_tts
    reservation.draw -= refund_draw
    if not refund_total and not refund_tts and not refund_draw:
        return
    entry = _user_daily_peek(user_id)
    if not isinstance(entry, dict):
        return
    entry["total"] = max(0, int(entry.get("total") or 0) - refund_total)
    if refund_tts:
        entry["tts"] = max(0, int(entry.get("tts") or 0) - refund_tts)
    if refund_draw:
        entry["draw"] = max(0, int(entry.get("draw") or 0) - refund_draw)
    bot.mark_quota_dirty()


def _user_key_for_message(message: Any) -> int | None:
    """The author's user_id or the nearest stable identifier (as with rate limiting)."""
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
    """Hours and minutes until the daily reset (for the global-limit refusal text)."""
    import bot
    try:
        secs = float(bot._quota_day_reset_in_sec())
    except Exception:
        return 0, 0
    return int(secs // 3600), int((secs % 3600) // 60)
