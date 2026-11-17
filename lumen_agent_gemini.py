"""lumen_agent_gemini.py — агентный поиск на резервном Gemini: модель пишет запросы, бот исполняет web_search."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from google.genai import types

from lumen_agent import (
    AgentTurnResult,
    WEB_SEARCH_TOOL,
    _clean_preamble,
    _parse_arguments,
    _safe_query_text,
)
from lumen_search import (fetch_web_snippets, format_snippets_for_prompt,
                         web_search_max_results, web_search_timeout_sec)

log = logging.getLogger("bot")

# Те же границы, что у цикла Munito: шаг — это планирующий вызов, поиски
# считаются отдельно; планирующий вызов не берёт весь остаток фазы.
_MAX_STEPS = 3
_STEP_CALL_MAX_SEC = 16.0
_MIN_STEP_BUDGET_SEC = 5.0


def gemini_agent_enabled() -> bool:
    """По умолчанию выключено: форма function calling подтверждена SDK, но живой
    прогон на резервной голове ещё не делался. Включается переменной Space."""
    return (os.getenv("GEMINI_AGENT_TURN_ENABLED", "0") or "").strip().lower() in ("1", "true", "on")


def _search_declaration() -> types.FunctionDeclaration:
    """Та же схема инструмента, что у Munito: имя, описание и поля совпадают,
    чтобы промпт и поведение модели не зависели от провайдера."""
    fn = WEB_SEARCH_TOOL["function"]
    return types.FunctionDeclaration(
        name=fn["name"],
        description=fn["description"],
        parameters_json_schema=fn["parameters"],
    )


def _config(effort: str | None, model_id: str) -> types.GenerateContentConfig:
    from lumen_router_config import gemini_thinking_for

    kwargs: dict[str, Any] = {
        "tools": [types.Tool(function_declarations=[_search_declaration()])],
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    param, value = gemini_thinking_for(model_id, effort)
    if param == "thinking_level":
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=value)
    elif param == "thinking_budget":
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=value)
    return types.GenerateContentConfig(**kwargs)


def _call_query(call: Any) -> str:
    args = getattr(call, "args", None)
    if not isinstance(args, dict):
        args = _parse_arguments(args)
    query = args.get("query") or args.get("q") or ""
    return " ".join(query.split())[:200].strip() if isinstance(query, str) else ""


async def run_search_turn(
    *, chat_id: int, user_prompt: str, clean_prompt: str, model_id: str, effort: str | None,
    max_searches: int, status: Any = None, deadline: float | None = None,
    history_meta: dict | None = None,
) -> AgentTurnResult:
    """Цикл на резервном Gemini: модель просит web_search, бот ищет, модель уточняет.
    Доказательства уходят в ответный промпт текстом (как у Munito): transcript с
    function_call без тех же tools головы игнорируют.

    Деградация — инвариант: любой сбой возвращает собранное и failed=True, вызов
    никогда не роняет ответ человеку."""
    import bot

    result = AgentTurnResult()
    if max_searches <= 0 or not gemini_agent_enabled():
        return result
    meta = dict(history_meta or {})
    meta["attach_past"] = False
    try:
        contents = await bot._build_gemini_turn_contents(chat_id, user_prompt, history_meta=meta)
    except Exception as exc:
        log.warning("[agent-gemini] contents build failed: %s", exc.__class__.__name__)
        result.failed = True
        return result

    config = None
    searches_left = max_searches
    while result.steps < _MAX_STEPS and searches_left > 0:
        if bot._stream_stop_requested(chat_id):
            log.info("[agent-gemini] stopped by the user during the evidence phase")
            result.failed = True
            break
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        # Сетевой резерв вычитаем из планирования, как в тракте Munito: иначе
        # планирующий вызов съедал весь бюджет и поиск стартовал за дедлайном
        # (ревью 17.11.2026).
        net_reserve = web_search_timeout_sec()
        if remaining is not None and remaining < _MIN_STEP_BUDGET_SEC + net_reserve:
            result.failed = True
            break
        config = config or _config(effort, model_id)
        timeout = (_STEP_CALL_MAX_SEC if remaining is None
                   else min(_STEP_CALL_MAX_SEC, max(1.0, remaining - net_reserve)))
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                bot.client.aio.models.generate_content(
                    model=model_id, contents=contents, config=config,
                ),
                timeout=max(1.0, timeout),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[agent-gemini] planning call failed (%s): %s", model_id, exc.__class__.__name__)
            result.failed = True
            break
        result.planning_ms += int((time.monotonic() - started) * 1000)
        calls = list(getattr(response, "function_calls", None) or [])
        preamble = _clean_preamble(getattr(response, "text", None) or "")
        if preamble and preamble not in result.preambles:
            result.preambles.append(preamble)
            if status is not None and len(result.preambles) == 1:
                with contextlib.suppress(Exception):
                    await status.set_action(preamble, timed=False)
        if not calls:
            break
        planned = [(call, _call_query(call)) for call in calls][:searches_left]
        planned = [(call, query) for call, query in planned if query]
        if not planned:
            result.failed = True
            break
        if status is not None:
            shown = _safe_query_text(planned[0][1])
            with contextlib.suppress(Exception):
                if shown:
                    await status.set_phase("progress_search_query", query=shown)
                else:
                    await status.set_phase("progress_searching")
        budget_left = float("inf") if deadline is None else max(0.0, deadline - time.monotonic())
        step_budget = min(web_search_timeout_sec(), budget_left)
        per_query = step_budget / len(planned) if len(planned) > 1 else step_budget
        got = await asyncio.gather(
            *(fetch_web_snippets(query, timeout_sec=max(0.5, per_query),
                                 max_results=web_search_max_results(), context=clean_prompt)
              for _call, query in planned),
            return_exceptions=True,
        )
        contents.append(types.Content(role="model", parts=[
            types.Part.from_function_call(name="web_search", args={"query": query})
            for _call, query in planned
        ]))
        contents.append(types.Content(role="user", parts=[
            types.Part.from_function_response(name="web_search", response={
                # Та же рамка недоверенных данных и та же чистка, что у Munito:
                # сырой сниппет мог закрыть фрейм и притащить невидимки
                # (ревью 17.11.2026).
                "results": format_snippets_for_prompt(snippets) or "no results",
            })
            for (_call, query), snippets in zip(planned, [
                found if isinstance(found, list) else [] for found in got
            ], strict=True)
        ]))
        for (_call, query), snippets in zip(planned, [
            found if isinstance(found, list) else [] for found in got
        ], strict=True):
            if query not in result.queries:
                result.queries.append(query)
            for item in snippets:
                url = (item.get("url") or "").strip() or f"no-url-{len(result.by_url)}"
                result.by_url.setdefault(url, item)
            if snippets:
                result.searched = True
        result.steps += 1
        searches_left -= len(planned)
        result.searches += len(planned)
        result.snippets = list(result.by_url.values())

    result.snippets = list(result.by_url.values())
    log.info(
        "[agent-gemini] chat=%s model=%s steps=%d searches=%d queries=%d snippets=%d planning_ms=%d failed=%s",
        chat_id, model_id, result.steps, result.searches, len(result.queries),
        len(result.snippets), result.planning_ms, result.failed,
    )
    return result
