"""Живой замер агентного цикла: что модель просит искать, за сколько шагов и что находит.

Меряет не «пришлился ли блок», а поведение надстройки в бою: число планирующих
вызовов, число и формулировки запросов, долинувших источников, время фазы и токены.
Ключ только из окружения или .env, значение нигде не печатается.
Запуск:  python eval/agent_loop_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# .env живёт только в общем checkout: worktree его не получает (gitignore), а
# импорты берутся из worktree, где лежит проверяемый код.
ENV_FILE = Path(os.getenv("LUMEN_ENV_DIR", r"C:/Users/egork/Documents/Default Project/Lumen")) / ".env"

BASE_DEFAULT = "https://api.munito.ai/inference/v1"

# Вопросы, где ответ обязан быть датированным: разные намерения из живой выдачи,
# а не одна тема. Первый блок — то, что ловит детектор свежести.
QUESTIONS = [
    "кто сейчас главный тренер сборной Беларуси по футболу",
    "какая ключевая ставка ЦБ России",
    "сколько стоит билет на поезд Минск Москва",
    "когда вышла Claude Opus 5.5",
    "погода в Гомеле на завтра",
    "курс доллара к российскому рублю сегодня",
    "кто выиграл Лигу наций по футболу",
    "какая сейчас версия Blender",
    "сколько серий в новом сезоне The Bear",
    "кто возглавляет NVIDIA сейчас",
    "какой процессор лучше купить для игр в 2026",
    "последние новости Беларуси за сегодня",
]


def _env(name: str, default: str = "") -> str:
    value = (os.getenv(name, "") or "").strip()
    if value:
        return value
    env_file = ENV_FILE
    if not env_file.exists():
        return default
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("﻿")
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def _payload_stats() -> dict:
    """Сколько запросов и токенов ушло: считаем по обёртке _munito_request."""
    return {"requests": 0, "prompt": 0, "completion": 0, "thinking": 0, "errors": 0}


async def main() -> None:
    key = _env("MUNITO_API_KEY")
    if not key:
        print("НЕТ КЛЮЧА MUNITO_API_KEY — выхожу")
        return

    import aiohttp

    import bot
    import lumen_agent
    from lumen_router_config import _MUNITO_ORDER, agent_max_searches, pick_reasoning_effort

    base = _env("MUNITO_BASE_URL", BASE_DEFAULT).rstrip("/")
    bot.MUNITO_API_KEY = key
    bot.MUNITO_BASE_URL = base
    stats = _payload_stats()

    async def fake_request(path: str, method: str = "GET", *, json_body: dict | None = None, deadline=None):
        stats["requests"] += 1
        timeout = None if deadline is None else max(0.5, deadline - time.monotonic())
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=json_body, timeout=aiohttp.ClientTimeout(total=timeout or 60.0, connect=15.0),
            ) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    stats["errors"] += 1
                    raise bot.MunitoAPIError(f"HTTP {resp.status}: {raw[:160]}", status_code=resp.status)
                data = json.loads(raw)
        usage = data.get("usage") or {}
        stats["prompt"] += int(usage.get("prompt_tokens") or 0)
        stats["completion"] += int(usage.get("completion_tokens") or 0)
        stats["thinking"] += int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
        return data

    async def fake_build(chat_id, user_text, model_id, *, history_meta=None, current_parts=None):
        # Тот же системный текст, что уходит в бой, но без состояния чата и фото:
        # замеряется цикл, а не сборка истории.
        return [{"role": "system", "content": bot.get_system_prompt(model_id)},
                {"role": "user", "content": user_text}]

    bot._munito_request = fake_request
    bot._build_openai_turn_messages = fake_build

    head = _MUNITO_ORDER[0]
    print(f"голова: {head} · хост {base} · вопросов {len(QUESTIONS)}")
    rows = []
    for question in QUESTIONS:
        effort = pick_reasoning_effort(question, needs_freshness=True)
        limit = agent_max_searches(effort)
        t0 = time.monotonic()
        turn = await lumen_agent.run_search_turn(
            chat_id=1, user_prompt=question, clean_prompt=question, model_id=head,
            effort=effort, max_searches=limit, status=None,
            deadline=time.monotonic() + 30.0, history_meta=None,
        )
        dt = time.monotonic() - t0
        rows.append({
            "q": question[:38], "effort": effort, "limit": limit, "sec": round(dt, 2),
            "searches": turn.searches, "queries": turn.queries, "snippets": len(turn.snippets),
            "failed": turn.failed, "preambles": turn.preambles,
        })
        print(f"  {dt:5.2f}s effort={effort:6s} поиск={turn.searches} источников={len(turn.snippets):2d} "
              f"failed={turn.failed!s:5s} запросы={turn.queries}")
        await asyncio.sleep(1.5)

    print("\n=== сводка фазы ===")
    total = sum(r["sec"] for r in rows)
    searched = sum(1 for r in rows if r["searches"])
    with_sources = sum(1 for r in rows if r["snippets"])
    multi = sum(1 for r in rows if r["searches"] > 1)
    print(f"вопросов {len(rows)} · цикл сработал на {searched} · источники есть на {with_sources} "
          f"· больше одного поиска на {multi}")
    print(f"суммарное время фазы {total:.1f}s · среднее {total / len(rows):.2f}s на вопрос")
    print(f"запросов к провайдеру {stats['requests']} · ошибок {stats['errors']} · "
          f"токены in={stats['prompt']} out={stats['completion']} thinking={stats['thinking']}")
    per_request = (stats["prompt"] + stats["completion"]) // max(1, stats["requests"])
    print(f"средний токенов на запрос: {per_request}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
