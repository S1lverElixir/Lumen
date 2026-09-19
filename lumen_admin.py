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
    """Общая проверка `Authorization: Bearer <expected>` — единственный легитимный
    способ пройти ЛЮБОЙ из секрет-гейтованных эндпоинтов (/admin_keys, /diag,
    /webhook_url, /export_state). ИСПРАВЛЕНО (аудит техдолга): раньше секреты (и
    BOT_TOKEN, и отдельно ADMIN_PANEL_KEY) читались из query-параметра (?bot_token=.../
    ?key=...) — CWE-598: секрет в URL попадает в access-логи промежуточных прокси/CDN,
    в историю браузера, в заголовок Referer при переходе по внешней ссылке. Query-
    параметр теперь не проверяется вообще — только заголовок. Раньше это были две
    независимые (но идентичные) реализации этой проверки — _check_admin_key и
    _check_bot_token_auth ниже теперь лишь называют разный секрет-кандидат."""
    auth_header = request.headers.get("Authorization", "")
    provided = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)

def _check_admin_key(request: Request) -> bool:
    import bot
    return _check_bearer_token(request, bot.ADMIN_PANEL_KEY)

def _redact_secret(value: str) -> str:
    """Показывает только последние несколько символов секрета — достаточно, чтобы
    владелец мог на глаз подтвердить "да, это тот же секрет, что и в прошлый раз"
    между рестартами, но недостаточно, чтобы кто-то посторонний, увидевший только
    эту урезанную строку в логах/скриншоте, мог им воспользоваться."""
    if not value:
        return "<empty>"
    return "…" + value[-6:] if len(value) > 6 else "…" + value

def _check_bot_token_auth(request: Request) -> bool:
    """Как _check_admin_key, но против BOT_TOKEN — это МАСТЕР-секрет, из которого
    выводятся оба остальных (WEBHOOK_SECRET/ADMIN_PANEL_KEY), гейтует только
    /admin_keys. См. _check_bearer_token выше про саму проверку и почему не
    query-параметр."""
    import bot
    return _check_bearer_token(request, bot.BOT_TOKEN)

@app.get("/")
async def healthcheck() -> dict[str, Any]:
    # ИСПРАВЛЕНО (аудит техдолга, август 2026): раньше здесь безусловно возвращался
    # "ok" даже если bot/client ещё не были инициализированы (main() их создаёт после
    # старта uvicorn) — эндпоинт не отражал вообще ничего о реальном состоянии
    # процесса. Проверка ниже — только in-memory (bot/client is not None), БЕЗ сетевых
    # вызовов к Telegram/Gemini/OpenRouter/Upstash: healthcheck обязан быть дешёвым и
    # быстрым, а не полноценной диагностикой (для неё уже есть /diag).
    import bot
    ready = bot.bot is not None and bot.client is not None
    return {"status": "ok" if ready else "starting", "ready": ready}

@app.get("/admin_keys")
async def get_admin_keys(request: Request) -> dict[str, str]:
    """Отдаёт полные значения WEBHOOK_SECRET/ADMIN_PANEL_KEY по запросу — единственный
    легитимный способ их узнать без печати в логах при каждом старте (см. критическую
    находку код-ревью: полные значения, печатавшиеся в лог на каждом рестарте, могли
    случайно попасть в скриншот/чат наравне с остальными логами). Доступ гейтится САМИМ
    BOT_TOKEN (заголовок Authorization: Bearer <BOT_TOKEN>, см. _check_bot_token_auth —
    ИСПРАВЛЕНО при повторном код-ревью: раньше токен передавался через query-параметр
    ?bot_token=..., что попадало в access-логи/историю браузера, см. комментарий там же),
    а не производным от него ADMIN_PANEL_KEY — иначе получился бы замкнутый круг: чтобы
    узнать ADMIN_PANEL_KEY, нужен был бы ADMIN_PANEL_KEY.
    BOT_TOKEN и так уже известен владельцу напрямую (из секретов HF Spaces/@BotFather),
    его не нужно доставать из логов бота."""
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
        # Раньше здесь был захардкожен URL одного из старых пробных воркеров
        # ("my-tg-proxy...") — /diag проверял чужой, забытый от прошлых экспериментов
        # адрес вместо РЕАЛЬНО настроенного прокси. Из-за этого диагностика однажды
        # ввела в заблуждение: показала "всё ок", хотя реально используемый
        # TELEGRAM_API_BASE_URL был недоступен, а проверялся вообще другой воркер.
        # Теперь проверяем именно то значение, которое бот реально использует для
        # вызовов Telegram API — если сменить прокси через env, /diag сразу тестирует
        # актуальный адрес без правки кода.
        "configured_tg_proxy": bot.TELEGRAM_API_BASE_URL + "/bot" + (bot_token[:6] if bot_token else "x") + "/getMe",
        "cloudflare_dot_com": "https://www.cloudflare.com",
        # Голый апекс-домен workers.dev (без поддомена) сам по себе может не отвечать
        # даже когда конкретные *.workers.dev поддомены (включая ваш прокси) работают
        # нормально — это ненадёжный сигнал "заблокирован ли workers.dev вообще",
        # ориентируйтесь в первую очередь на configured_tg_proxy выше (он теперь
        # бьёт в реалистичный путь /bot.../getMe, а не в голый корень домена —
        # голый корень у самого Telegram может отвечать медленно/зависать, даже
        # когда реальные вызовы API через прокси работают быстро и штатно).
        "cloudflare_workers_dev_root": "https://workers.dev",
        "deno_deploy": "https://deno.com",
        # ponytail: netlify/render/railway/fly.io/supabase/vercel убраны — ни один из
        # этих хостингов проектом не используется (Vercel-прокси заброшен и никогда не
        # работал, см. историю проекта), проверка их доступности не даёт полезного
        # сигнала. cloudflare/deno оставлены — реально задействованы (workers.dev как
        # исторически пробовавшийся вариант прокси, deno.com — текущий активный).
        "google_generic": "https://www.google.com",
        "gemini_api": "https://generativelanguage.googleapis.com",
        "huggingface": "https://huggingface.co",
        "openrouter": "https://openrouter.ai",
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
    """Полный дамп состояния всех чатов + квот одним JSON — на случай ручного бэкапа.

    НАЙДЕНО ПРИ АУДИТЕ ТЕХДОЛГА: без Upstash состояние живёт на эфемерном диске
    контейнера (обнуляется на каждом редеплое); с Upstash — на бесплатном тире без
    какой-либо резервной копии (256 МБ / 500k команд/мес, архивируется через 30 дней
    простоя). Полноценная автоматическая репликация в отдельное облако — отдельная
    инфраструктурная задача с собственными учётными данными, которую нельзя завести
    из кода бота. Это — минимальная практичная замена: владелец может вызвать этот
    эндпоинт по расписанию (curl + cron/GitHub Actions на своей стороне) и держать
    файл в любом месте на своё усмотрение. Гейтится ADMIN_PANEL_KEY, как /diag."""
    if not _check_admin_key(request):
        return {"error": "forbidden — missing or invalid Authorization: Bearer <ADMIN_PANEL_KEY> header"}
    import bot
    return {
        "exported_at": datetime.now().isoformat(),
        "chats": {str(cid): _serialize_chat_state(state) for cid, state in bot.chat_state.items()},
        "global_quota": bot.GLOBAL_QUOTA,
    }
