"""lumen_admin.py — HTTP layer: FastAPI app, secret gates, admin/diag/export.
The connection to bot.py is only through a deferred `import bot` inside functions.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import lumen_images
from lumen_state_storage import (
    CHAT_STATE_SCHEMA_VERSION,
    _is_json_integer,
    _serialize_chat_state,
    _validate_chat_snapshot,
)

log = logging.getLogger("bot")

# FastAPI schemas are intentionally disabled: /docs, /redoc, and /openapi.json on a public
# Space described all endpoints and their parameters for free (audit 26.09.2026).
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

def _check_bearer_token(request: Request, expected: str) -> bool:
    """Bearer header only: a secret in the URL appears in logs/history/Referer (CWE-598)."""
    auth_header = request.headers.get("Authorization", "")
    provided = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    # Compare bytes: the str variant of compare_digest raises TypeError on non-ASCII input.
    return bool(expected) and bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())

def _check_admin_key(request: Request) -> bool:
    import bot
    return _check_bearer_token(request, bot.ADMIN_PANEL_KEY)

def _log_denied(request: Request, what: str) -> None:
    """An attempt without the correct key is visible in logs: previously, brute force against
    /admin_keys or /export_state left no trace, and the response was 200 with body
    {"error": ...} (audit 26.09.2026)."""
    log.warning(
        '[admin] Denied %s: no valid Authorization: Bearer <key> from %s',
        what, getattr(getattr(request, "client", None), "host", "?"),
    )

def _forbidden() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "forbidden — missing or invalid Authorization: Bearer <key> header"},
    )

def _redact_secret(value: str) -> str:
    """Fingerprint: the last characters for comparison across restarts; it cannot be used."""
    if not value:
        return "<empty>"
    return "…" + value[-6:] if len(value) > 6 else "…" + value

def _check_bot_token_auth(request: Request) -> bool:
    """Like _check_admin_key, but the master secret is BOT_TOKEN; only for /admin_keys (otherwise circular)."""
    import bot
    return _check_bearer_token(request, bot.BOT_TOKEN)

@app.get("/")
async def healthcheck() -> dict[str, Any]:
    # Only in-memory readiness (bot/client), no network; the healthcheck must be cheap.
    import bot
    ready = bot.bot is not None and bot.client is not None
    return {"status": "ok" if ready else "starting", "ready": ready}

@app.get("/admin_keys")
async def get_admin_keys(request: Request) -> Any:
    """Full secrets only with Bearer BOT_TOKEN (not ADMIN_PANEL_KEY, which would be circular); they previously appeared in logs."""
    if not _check_bot_token_auth(request):
        _log_denied(request, "GET /admin_keys")
        return _forbidden()
    import bot
    return {"webhook_secret": bot.WEBHOOK_SECRET, "admin_panel_key": bot.ADMIN_PANEL_KEY}

@app.get("/webhook_url")
async def get_webhook_url(request: Request) -> Any:
    if not _check_admin_key(request):
        _log_denied(request, "GET /webhook_url")
        return _forbidden()
    space_host = os.getenv("SPACE_HOST", "").strip()
    if not space_host:
        author = os.getenv("SPACE_AUTHOR_NAME", "silverelixir").lower()
        repo = os.getenv("SPACE_REPO_NAME", "lumen").lower()
        space_host = f"{author}-{repo}.hf.space"
    webhook_url = f"https://{space_host}/webhook"
    # Do not expose the token in the URL: a link with botTOKEN would appear in browser history
    # and proxy logs (AUD-D-001). The owner gets the token from @BotFather.
    return {
        "webhook_url": webhook_url,
        "register_url_template": (
            "https://api.telegram.org/bot<TOKEN>/setWebhook"
            f"?url={webhook_url}"
            "&secret_token=<ADMIN_SECRET>"
            "&drop_pending_updates=true"
        ),
        "instruction": "Подставь BOT_TOKEN и WEBHOOK_SECRET вручную и вызови ссылку curl, а не браузером",
    }

# A Telegram update is small: reject anything over the cap before parsing.
WEBHOOK_MAX_BODY_BYTES = 512 * 1024
# Keep background update tasks bounded: on overflow, ask the client to retry.
WEBHOOK_MAX_INFLIGHT_TASKS = 100
# Log secret/cap rejections in aggregate, not one by one.
_WEBHOOK_DENIED_LOG_INTERVAL_SEC = 60.0
_webhook_denied_count = 0
# -1e9, not 0: on a fresh host, monotonic() < 60, and the first rejection
# waited a minute for the log line (review 17.11.2026).
_webhook_denied_last_log = -1e9
# Telegram may deliver the same update_id twice (a retry after a timeout).
# Remember the last N: a duplicate must not produce a second answer, download, or quota charge.
_SEEN_UPDATE_IDS_MAX = 2000
_seen_update_ids: deque[int] = deque()
_seen_update_id_set: set[int] = set()


def _is_duplicate_update(update_id: Any) -> bool:
    if not isinstance(update_id, int):
        return False
    if update_id in _seen_update_id_set:
        return True
    if len(_seen_update_ids) >= _SEEN_UPDATE_IDS_MAX:
        old = _seen_update_ids.popleft()
        _seen_update_id_set.discard(old)
    _seen_update_ids.append(update_id)
    _seen_update_id_set.add(update_id)
    return False


def _log_webhook_denied(reason: str) -> None:
    """Rejection throttling: avoid an unbounded SimpleQueue log with one line per event; collapse bursts into one."""
    global _webhook_denied_count, _webhook_denied_last_log
    now = time.monotonic()
    _webhook_denied_count += 1
    if now - _webhook_denied_last_log >= _WEBHOOK_DENIED_LOG_INTERVAL_SEC:
        log.warning(
            "[webhook] Rejected %d request(s): %s",
            _webhook_denied_count, reason,
        )
        _webhook_denied_count = 0
        _webhook_denied_last_log = now


@app.post("/webhook")
async def webhook_handler(request: Request) -> Any:
    import bot
    import json as _json
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # An empty secret is invalid; byte comparison keeps non-ASCII safe from TypeError.
    if not bot.WEBHOOK_SECRET or not token or not hmac.compare_digest(token.encode(), bot.WEBHOOK_SECRET.encode()):
        _log_webhook_denied("invalid secret token")
        return {"ok": False}
    # State is still loading: return 503 so Telegram retries later instead of creating
    # an empty chat and did not overwrite deleted history (audit 28.10.2026).
    if not getattr(bot, "_state_ready", True):
        log.info("[webhook] State is still loading, asking Telegram to retry")
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    # The update body is small: reject garbage by header before reading.
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > WEBHOOK_MAX_BODY_BYTES:
                _log_webhook_denied(f"body larger than the {WEBHOOK_MAX_BODY_BYTES} byte cap")
                return {"ok": False}
        except ValueError:
            pass
    # The background task queue is bounded: on overflow, ask the user to retry later.
    if len(bot._inflight_tasks) >= WEBHOOK_MAX_INFLIGHT_TASKS:
        log.warning("[webhook] Too many in-flight updates (%d), asking Telegram to retry", len(bot._inflight_tasks))
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    try:
        # Read as a stream and cut at the cap: request.body() pulled in the chunked body
        # entirely, and the limit triggered only after reading (audit 26.10.2026).
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > WEBHOOK_MAX_BODY_BYTES:
                _log_webhook_denied(f"body larger than the {WEBHOOK_MAX_BODY_BYTES} byte cap")
                return {"ok": False}
            chunks.append(chunk)
        body = _json.loads(b"".join(chunks))
        if bot.bot is None:
            # Return 503 instead of 200: let Telegram retry the update rather than treat
            # it as dropped
            # success. Check BEFORE deduplication: otherwise update_id is marked
            # processed, and the repeat arrives as a duplicate and is lost
            # (audit 29.10.2026).
            log.warning("[webhook] Bot not initialized yet, asking Telegram to retry")
            return JSONResponse(status_code=503, content={"ok": False, "retry": True})
        # The cap recheck comes BEFORE deduplication (audit R1-03, 26.11.2026):
        # after a 503 refusal, Telegram will retry the update, and the retry must not burn
        # as a duplicate. Awaits between rechecking, deduplication, and create_task
        # no, so the race window from the 16.11.2026 audit remains closed.
        if len(bot._inflight_tasks) >= WEBHOOK_MAX_INFLIGHT_TASKS:
            log.warning("[webhook] In-flight cap reached after body read (%d), asking Telegram to retry",
                        len(bot._inflight_tasks))
            return JSONResponse(status_code=503, content={"ok": False, "retry": True})
        if isinstance(body, dict) and _is_duplicate_update(body.get("update_id")):
            log.info("[webhook] Duplicate update_id %s ignored", body.get("update_id"))
            return {"ok": True}
        bot._track_inflight_task(asyncio.create_task(bot._process_raw_update(body)))
    except Exception as exc:
        log.warning("[webhook] Failed to process incoming update: %s", exc, exc_info=True)
        # A malformed body is not success: let Telegram retry rather than count a drop
        # processed (audit M2, 10.2026).
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    return {"ok": True}

async def probe_url(
    session: Any, url: str, *, timeout_sec: float = 6.0, redact: str = "",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One GET probe: the same logic and result format for /diag and /selftest.

    ok only for 2xx/3xx: previously, a 401 from the protected proxy and a 403 from TikWM
    were treated as healthy, so diagnostics showed green for a dead route.
    """
    start = time.time()
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout_sec), headers=headers or None,
        ) as resp:
            status = int(resp.status)
            ok = 200 <= status < 400
            if not ok and status == 401:
                # 401 with Telegram JSON means the proxy and API are alive; only the test token
                # truncated; the proxy's own 401 has an "Unauthorized" body and remains red.
                with contextlib.suppress(Exception):
                    body = await resp.content.read(512)
                    if b'"error_code"' in body and b'"description"' in body:
                        ok = True
            return {"status": resp.status, "elapsed_sec": round(time.time() - start, 2), "ok": ok}
    except Exception as exc:
        elapsed = round(time.time() - start, 2)
        exc_str = str(exc) or repr(exc) or type(exc).__name__
        if redact:
            exc_str = exc_str.replace(redact, "<TOKEN>")
        return {"error": exc_str, "elapsed_sec": elapsed, "ok": False}

def _diag_budget_sec() -> float:
    """Lazily through bot._env_number: bare float() returned 500 on garbage in env."""
    import bot
    return bot._env_number("DIAG_TOTAL_BUDGET_SEC", 25, min_value=1)


@app.get("/diag")
async def network_diagnostics(request: Request) -> dict[str, Any]:
    """Checks outbound network access to various hosts from the container.
    Open in a browser to see which outbound connections from HF Spaces are
    actually blocked and which are available."""
    if not _check_admin_key(request):
        _log_denied(request, "GET /diag")
        return _forbidden()
    import bot
    from urllib.parse import quote, urlsplit
    bot_token = bot.BOT_TOKEN
    tikwm_base = bot.TIKWM_API_BASE_URL
    # The direct TikWM host does not receive the secret: only the proxy needs it.
    tikwm_via_proxy = bool(tikwm_base) and urlsplit(tikwm_base).hostname not in {"www.tikwm.com", "tikwm.com"}
    targets = {
        "telegram_api": "https://api.telegram.org",
        "telegram_file_api": "https://api.telegram.org/bot" + (bot_token[:6] if bot_token else "x") + "/getMe",
        # Check the actually configured proxy, not a forgotten hardcoded value; otherwise 'all good' would be reported for a dead address.
        "configured_tg_proxy": bot.TELEGRAM_API_BASE_URL + "/bot" + (bot_token[:6] if bot_token else "x") + "/getMe",
        "cloudflare_dot_com": "https://www.cloudflare.com",
        # A bare workers.dev is an unreliable signal; check configured_tg_proxy.
        "cloudflare_workers_dev_root": "https://workers.dev",
        "deno_deploy": "https://deno.com",
        # Unused hosts removed: a signal is needed only for those actually used (workers/deno).
        "google_generic": "https://www.google.com",
        "gemini_api": "https://generativelanguage.googleapis.com",
        "huggingface": "https://huggingface.co",
        "groq_api": "https://api.groq.com",
        # Actual API path of the search reference layer: the root would return 200 even when
        # unreachable api.php — the same principle as pollinations_models.
        "wikipedia_api": "https://ru.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json",
        # A bare address prefix: the /chat/completions path without a key would return
        # 401/404
        # and painted the live host red.
        "munito_api": urlsplit(bot.MUNITO_BASE_URL)._replace(path="", query="", fragment="").geturl(),
        # /api/ is the same path as for real requests: the proxy root could
        # respond with 200 while the API path itself is broken (review 28.10.2026).
        "tikwm": (tikwm_base + "/api/") if tikwm_via_proxy else "https://www.tikwm.com",
        # /models instead of the endpoint root: the root returned 200 even with dead generation,
        # diagnostics lied green (audit 09.11.2026).
        "pollinations_models": "https://image.pollinations.ai/models",
        # List of account models with a token — the same path as real generations;
        # without Cloudflare configuration, this remains merely a check that the host is reachable.
        # /ai/models/search — the real catalog path (wrangler itself calls it);
        # bare /ai/models returns 404 and would mark a live account as dead.
        "cloudflare_ai": (
            f"{lumen_images.CLOUDFLARE_AI_BASE_URL}/{quote(lumen_images.CLOUDFLARE_ACCOUNT_ID, safe='')}"
            "/ai/models/search?limit=1"
            if lumen_images.cloudflare_configured() else "https://api.cloudflare.com"
        ),
        "upstash": bot.UPSTASH_REDIS_REST_URL if bot.USE_UPSTASH else "https://upstash.com",
    }
    results: dict[str, Any] = {}
    session = await bot._get_http_session()
    # One shared budget instead of sequential 14×6s (~84s of hanging
    # diagnostics):
    # probes run in parallel, and the tail is cut off by the budget (AUD-F-001).
    diag_budget = _diag_budget_sec()
    proxy_headers = (
        {"X-Lumen-Proxy-Secret": bot.LUMEN_PROXY_SECRET} if bot.LUMEN_PROXY_SECRET else {}
    )

    async def _probe(name: str, url: str) -> tuple[str, dict[str, Any]]:
        # Send the secret only to an actually configured proxy: a direct
        # api.telegram.org must not see the header (audit 28.10.2026).
        headers = None
        if name == "configured_tg_proxy" and bot.TELEGRAM_API_BASE_URL != "https://api.telegram.org":
            headers = proxy_headers
        elif name == "cloudflare_ai" and lumen_images.cloudflare_configured():
            # The token is only in the header and only to Cloudflare itself: it is not in the URL,
            # therefore the probe_url error branch does not expose it. A 403 here is not a dead
            # host, while the token lacks permission for the model catalog: generation is still alive.
            headers = {"Authorization": f"Bearer {lumen_images.CLOUDFLARE_API_TOKEN}"}
        return name, await probe_url(
            session, url, timeout_sec=6.0, redact=bot_token or "", headers=headers,
        )

    tasks = {asyncio.create_task(_probe(name, url)): name for name, url in targets.items()}
    done, pending = await asyncio.wait(tasks, timeout=diag_budget)
    for task in done:
        try:
            name, res = task.result()
        except Exception as exc:
            name, res = tasks[task], {"error": str(exc), "elapsed_sec": diag_budget, "ok": False}
        results[name] = res
    for task in pending:
        task.cancel()
        results[tasks[task]] = {"error": "diag budget exceeded", "elapsed_sec": diag_budget, "ok": False}
    with contextlib.suppress(Exception):
        await asyncio.gather(*pending, return_exceptions=True)
    # Storage state lives here too: a "frozen" chat record after a failure
    # of the index is otherwise visible only in the log (16.11.2026 audit).
    import lumen_chat_state as lcs
    state_info = {
        "ready": bool(getattr(bot, "_state_ready", True)),
        "index_unreadable": bool(lcs._state_load_failed),
        "quota_unreadable": bool(lcs._quota_load_failed),
        "persist_frozen_chats": len(lcs._chat_snapshot_unloaded),
    }
    return {"diagnostics": results, "state": state_info,
            "telegram_api_base_configured": bot.TELEGRAM_API_BASE_URL}

def _export_snapshot_incomplete_reason() -> str | None:
    """Return a privacy-safe reason when the live state is known to be partial."""
    import bot
    import lumen_chat_state as lcs

    if lcs._chat_snapshot_unloaded:
        return "unloaded_chat_snapshots"
    if lcs._legacy_migration_pending_ids:
        return "legacy_migration_pending"
    if lcs._pending_chat_deletions:
        return "pending_chat_deletions"
    if lcs._deleting_chat_ids:
        return "chat_deletion_in_flight"
    if lcs._index_extra_ids - set(bot.chat_state):
        return "indexed_chats_not_loaded"
    return None


def _validate_export_snapshot(payload: Any) -> bool:
    """Validate the backup contract before exposing it to the backup workflow."""
    if not isinstance(payload, dict):
        return False
    if (not _is_json_integer(payload.get("export_schema_version"))
            or payload["export_schema_version"] != 1
            or payload.get("complete") is not True):
        return False
    if not isinstance(payload.get("exported_at"), str) or not payload["exported_at"]:
        return False
    chats = payload.get("chats")
    if not isinstance(chats, dict) or not chats or not isinstance(payload.get("global_quota"), dict):
        return False
    if not _is_json_integer(payload.get("chat_count")) or payload["chat_count"] != len(chats):
        return False
    for chat_id, snapshot in chats.items():
        if not isinstance(chat_id, str):
            return False
        try:
            if str(int(chat_id)) != chat_id:
                return False
        except (TypeError, ValueError):
            return False
        if not _validate_chat_snapshot(snapshot, expected_version=CHAT_STATE_SCHEMA_VERSION):
            return False
    from lumen_quotas import _validate_global_quota
    return _validate_global_quota(payload["global_quota"])


@app.get("/export_state")
async def export_state(request: Request) -> Any:
    """Manual backup for cron: ephemeral disk and an Upstash tier without a replica; gated by ADMIN_PANEL_KEY."""
    if not _check_admin_key(request):
        _log_denied(request, "GET /export_state")
        return _forbidden()
    import bot
    if not getattr(bot, "_state_ready", True):
        # A backup taken during loading is empty or partial, while state-backup.yml
        # would have committed it as daily (audit 16.11.2026). Let cron retry.
        log.info("[admin] /export_state requested while state is still loading, asking to retry")
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    import lumen_chat_state as lcs
    if lcs._state_load_failed or lcs._quota_load_failed:
        # Load failure: the snapshot will be empty, and state-backup.yml will commit it
        # like the daily backup (review 17.11.2026). An honest 503 makes cron retry.
        log.warning("[admin] /export_state requested after a failed startup load, asking to retry")
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    incomplete_reason = _export_snapshot_incomplete_reason()
    if incomplete_reason is not None:
        # An incomplete snapshot must not evict the last good backup: some
        # chats are known from the index but absent from memory after quarantine.
        log.warning("[admin] /export_state refused: state is incomplete (%s)", incomplete_reason)
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    try:
        # Snapshot of live structures: export returned references to mutable objects:
        # a write between the return and response serialization produced an inconsistent
        # backup.
        chats = {str(cid): _serialize_chat_state(state) for cid, state in list(bot.chat_state.items())}
        # The quota is nested (per-model counters) — only deepcopy detaches it completely.
        quota = copy.deepcopy(dict(bot.GLOBAL_QUOTA))
        payload = {
            "export_schema_version": 1,
            "complete": True,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "chat_count": len(chats),
            "chats": chats,
            "global_quota": quota,
        }
        json.dumps(payload, allow_nan=False)
    except Exception as exc:
        log.warning("[admin] /export_state could not build a snapshot (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    try:
        valid_snapshot = _validate_export_snapshot(payload)
    except Exception as exc:
        log.warning("[admin] /export_state validation failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    if not valid_snapshot:
        log.warning("[admin] /export_state refused: snapshot failed structural validation")
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    return payload


@app.get("/devtrace")
async def devtrace(request: Request, n: int = 200) -> Any:
    """Tail of the JSONL request/response trace for debugging; gated by ADMIN_PANEL_KEY."""
    if not _check_admin_key(request):
        _log_denied(request, "GET /devtrace")
        return _forbidden()
    import lumen_devtrace as _devtrace
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 200
    # Read the 5 MB tail in a thread: it previously blocked the loop (review 17.11.2026).
    entries = await asyncio.to_thread(_devtrace.read_tail, count)
    return {
        "enabled": _devtrace.enabled(),
        "path": str(_devtrace.trace_path()),
        "count": len(entries),
        "entries": entries,
    }
