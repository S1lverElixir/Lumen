"""
lumen_admin.py — HTTP-слой: FastAPI-приложение, секрет-гейты, admin/diag/export.
Связь с bot.py — только через отложенный `import bot` внутри функций.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import logging
import os
import time
from datetime import datetime
from typing import Any

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lumen_state_storage import _serialize_chat_state

log = logging.getLogger("bot")

# Схемы FastAPI выключены намеренно: /docs, /redoc и /openapi.json на публичном
# Space описывали все эндпоинты и их параметры бесплатно (аудит 26.09.2026).
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

def _check_bearer_token(request: Request, expected: str) -> bool:
    """Только Bearer-заголовок: секрет в URL светится в логах/истории/Referer (CWE-598)."""
    auth_header = request.headers.get("Authorization", "")
    provided = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    # Сравнение в байтах: str-вариант compare_digest падает TypeError на не-ASCII.
    return bool(expected) and bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())

def _check_admin_key(request: Request) -> bool:
    import bot
    return _check_bearer_token(request, bot.ADMIN_PANEL_KEY)

def _log_denied(request: Request, what: str) -> None:
    """Попытка без верного ключа видна в логах: раньше брутфорс /admin_keys или
    /export_state не оставлял следов, а ответ был 200 с телом {"error": ...}
    (аудит 26.09.2026)."""
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
    """Отпечаток: последние символы для сверки между рестартами, воспользоваться нельзя."""
    if not value:
        return "<empty>"
    return "…" + value[-6:] if len(value) > 6 else "…" + value

def _check_bot_token_auth(request: Request) -> bool:
    """Как _check_admin_key, но мастер-секрет BOT_TOKEN; только для /admin_keys (иначе круг)."""
    import bot
    return _check_bearer_token(request, bot.BOT_TOKEN)

@app.get("/")
async def healthcheck() -> dict[str, Any]:
    # Только in-memory готовность (bot/client), без сети — healthcheck обязан быть дешёвым.
    import bot
    ready = bot.bot is not None and bot.client is not None
    return {"status": "ok" if ready else "starting", "ready": ready}

@app.get("/admin_keys")
async def get_admin_keys(request: Request) -> Any:
    """Полные секреты только по Bearer BOT_TOKEN (не ADMIN_PANEL_KEY — иначе круг); раньше светились в логах."""
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
    # Токен в URL не отдаём: ссылка с botTOKEN светилась бы в истории браузера
    # и логах прокси (AUD-D-001). Токен владелец берёт у @BotFather.
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

# Апдейт Telegram маленький: всё сверх капа отклоняем до разбора.
WEBHOOK_MAX_BODY_BYTES = 512 * 1024
# Фоновых задач апдейтов держим ограниченно: переполнение просим повторить.
WEBHOOK_MAX_INFLIGHT_TASKS = 100
# Отклонения по секрету/капу гроздьями логируем суммарно, а не по одному.
_WEBHOOK_DENIED_LOG_INTERVAL_SEC = 60.0
_webhook_denied_count = 0
_webhook_denied_last_log = 0.0


def _log_webhook_denied(reason: str) -> None:
    """Троттлинг отказов: каждая строка в SimpleQueue-лог без края, гроздья — одной."""
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
    # Пустой секрет невалиден, сравнение в байтах держит не-ASCII без TypeError.
    if not bot.WEBHOOK_SECRET or not token or not hmac.compare_digest(token.encode(), bot.WEBHOOK_SECRET.encode()):
        _log_webhook_denied("invalid secret token")
        return {"ok": False}
    # Тело апдейта маленькое: отсекаем мусор по заголовку до чтения.
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > WEBHOOK_MAX_BODY_BYTES:
                _log_webhook_denied(f"body larger than the {WEBHOOK_MAX_BODY_BYTES} byte cap")
                return {"ok": False}
        except ValueError:
            pass
    # Очередь фоновых задач ограничена: переполнение просим повторить позже.
    if len(bot._inflight_tasks) >= WEBHOOK_MAX_INFLIGHT_TASKS:
        log.warning("[webhook] Too many in-flight updates (%d), asking Telegram to retry", len(bot._inflight_tasks))
        return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    try:
        raw_body = await request.body()
        if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
            # Заголовок соврал или его не было: сырые байты сверх капа не разбираем.
            _log_webhook_denied(f"body larger than the {WEBHOOK_MAX_BODY_BYTES} byte cap")
            return {"ok": False}
        body = _json.loads(raw_body)
        if bot.bot is not None:
            bot._track_inflight_task(asyncio.create_task(bot._process_raw_update(body)))
        else:
            # 503 вместо 200: пусть Telegram повторит апдейт, а не считает дроп успехом (AUD-E-003).
            log.warning("[webhook] Bot not initialized yet, asking Telegram to retry")
            return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    except Exception as exc:
        log.warning("[webhook] Failed to process incoming update: %s", exc)
    return {"ok": True}

async def probe_url(session: Any, url: str, *, timeout_sec: float = 6.0, redact: str = "") -> dict[str, Any]:
    """Один GET-зонд: та же логика и тот же формат результата для /diag и /selftest."""
    start = time.time()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
            return {"status": resp.status, "elapsed_sec": round(time.time() - start, 2), "ok": True}
    except Exception as exc:
        elapsed = round(time.time() - start, 2)
        exc_str = str(exc) or repr(exc) or type(exc).__name__
        if redact:
            exc_str = exc_str.replace(redact, "<TOKEN>")
        return {"error": exc_str, "elapsed_sec": elapsed, "ok": False}

@app.get("/diag")
async def network_diagnostics(request: Request) -> dict[str, Any]:
    """Проверяет исходящую сетевую доступность различных хостов из контейнера.
    Открой в браузере чтобы увидеть, что реально заблокировано на исходящих
    соединениях из HF Spaces, а что доступно."""
    if not _check_admin_key(request):
        _log_denied(request, "GET /diag")
        return _forbidden()
    import bot
    bot_token = bot.BOT_TOKEN
    targets = {
        "telegram_api": "https://api.telegram.org",
        "telegram_file_api": "https://api.telegram.org/bot" + (bot_token[:6] if bot_token else "x") + "/getMe",
        # Проверяем реально настроенный прокси, а не забытый хардкод: иначе "всё ок" при мёртвом адресе.
        "configured_tg_proxy": bot.TELEGRAM_API_BASE_URL + "/bot" + (bot_token[:6] if bot_token else "x") + "/getMe",
        "cloudflare_dot_com": "https://www.cloudflare.com",
        # Голый workers.dev — ненадёжный сигнал; смотреть на configured_tg_proxy.
        "cloudflare_workers_dev_root": "https://workers.dev",
        "deno_deploy": "https://deno.com",
        # Лишние хостинги убраны: сигнал нужен только по реально используемым (workers/deno).
        "google_generic": "https://www.google.com",
        "gemini_api": "https://generativelanguage.googleapis.com",
        "huggingface": "https://huggingface.co",
        "openrouter": "https://openrouter.ai",
        "groq_api": "https://api.groq.com",
        "tikwm": "https://www.tikwm.com",
        "pollinations": "https://image.pollinations.ai",
        "upstash": bot.UPSTASH_REDIS_REST_URL if bot.USE_UPSTASH else "https://upstash.com",
    }
    results: dict[str, Any] = {}
    session = await bot._get_http_session()
    # Общий бюджет вместо последовательных 14×6с (~84с висящей диагностики):
    # зонды идут параллельно, хвост обрезается бюджетом (AUD-F-001).
    diag_budget = float(os.getenv("DIAG_TOTAL_BUDGET_SEC", "25"))

    async def _probe(name: str, url: str) -> tuple[str, dict[str, Any]]:
        return name, await probe_url(session, url, timeout_sec=6.0, redact=bot_token or "")

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
    return {"diagnostics": results, "telegram_api_base_configured": bot.TELEGRAM_API_BASE_URL}

@app.get("/export_state")
async def export_state(request: Request) -> dict[str, Any]:
    """Ручной бэкап для cron: эфемерный диск и тир Upstash без реплики; гейт ADMIN_PANEL_KEY."""
    if not _check_admin_key(request):
        _log_denied(request, "GET /export_state")
        return _forbidden()
    import bot
    # Снимок живых структур: экспорт отдавал ссылки на мутабельные объекты —
    # запись между возвратом и сериализацией ответа давала несогласованный бэкап.
    chats = {str(cid): _serialize_chat_state(state) for cid, state in list(bot.chat_state.items())}
    # Квота вложенная (счётчики per-model) — только deepcopy отцепляет её целиком.
    quota = copy.deepcopy(dict(bot.GLOBAL_QUOTA))
    return {
        "exported_at": datetime.now().isoformat(),
        "chats": chats,
        "global_quota": quota,
    }
