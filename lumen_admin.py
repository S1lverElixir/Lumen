"""
lumen_admin.py — HTTP-слой бота: FastAPI-приложение, секрет-гейты и
admin/diag/export эндпоинты (вынесено из bot.py, P2 аудита).

Связи с рантаймом bot.py — ТОЛЬКО через отложенный `import bot` внутри функций
(модульного цикла нет: bot.py импортирует этот модуль, а не наоборот).
bot.py реэкспортирует имена — `bot.webhook_handler`, `bot.healthcheck`,
`bot.WEBHOOK_SECRET` и т.п. в тестах и main() не менялись.
"""
from __future__ import annotations

import asyncio
import contextlib
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

app = FastAPI()

def _check_bearer_token(request: Request, expected: str) -> bool:
    """Только Bearer-заголовок: секрет в URL светится в логах/истории/Referer (CWE-598)."""
    auth_header = request.headers.get("Authorization", "")
    provided = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)

def _check_admin_key(request: Request) -> bool:
    import bot
    return _check_bearer_token(request, bot.ADMIN_PANEL_KEY)

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
async def get_admin_keys(request: Request) -> dict[str, str]:
    """Полные секреты только по Bearer BOT_TOKEN (не ADMIN_PANEL_KEY — иначе круг); раньше светились в логах."""
    if not _check_bot_token_auth(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <BOT_TOKEN> header"}
    import bot
    return {"webhook_secret": bot.WEBHOOK_SECRET, "admin_panel_key": bot.ADMIN_PANEL_KEY}

@app.get("/webhook_url")
async def get_webhook_url(request: Request) -> dict[str, str]:
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
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

@app.post("/webhook")
async def webhook_handler(request: Request) -> Any:
    import bot
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token, bot.WEBHOOK_SECRET):
        log.warning("[webhook] Rejected request with invalid secret token")
        return {"ok": False}
    try:
        body = await request.json()
        if bot.bot is not None:
            bot._track_inflight_task(asyncio.create_task(bot._process_raw_update(body)))
        else:
            # 503 вместо 200: пусть Telegram повторит апдейт, а не считает дроп успехом (AUD-E-003).
            log.warning("[webhook] Bot not initialized yet, asking Telegram to retry")
            return JSONResponse(status_code=503, content={"ok": False, "retry": True})
    except Exception as exc:
        log.warning("[webhook] Failed to process incoming update: %s", exc)
    return {"ok": True}

@app.get("/diag")
async def network_diagnostics(request: Request) -> dict[str, Any]:
    """Проверяет исходящую сетевую доступность различных хостов из контейнера.
    Открой в браузере чтобы увидеть, что реально заблокировано на исходящих
    соединениях из HF Spaces, а что доступно."""
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
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
        start = time.time()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                return name, {"status": resp.status, "elapsed_sec": round(time.time() - start, 2), "ok": True}
        except Exception as exc:
            elapsed = round(time.time() - start, 2)
            exc_str = str(exc) or repr(exc) or type(exc).__name__
            if bot_token:
                exc_str = exc_str.replace(bot_token, "<TOKEN>")
            return name, {"error": exc_str, "elapsed_sec": elapsed, "ok": False}

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
        await asyncio.gather(*pending)
    return {"diagnostics": results, "telegram_api_base_configured": bot.TELEGRAM_API_BASE_URL}

@app.get("/export_state")
async def export_state(request: Request) -> dict[str, Any]:
    """Ручной бэкап для cron: эфемерный диск и тир Upstash без реплики; гейт ADMIN_PANEL_KEY."""
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
    import bot
    return {
        "exported_at": datetime.now().isoformat(),
        "chats": {str(cid): _serialize_chat_state(state) for cid, state in bot.chat_state.items()},
        "global_quota": bot.GLOBAL_QUOTA,
    }
