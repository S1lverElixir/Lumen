"""Live measurement of zone 2: the model decides for itself where to search when the regex is silent.

The 35/20 corpus measures only the regex. Here the same 20 “do not search” phrasings
go through a real agent-loop planning call: the metric is how many of them
the model ordered a search for. A number above zero means extra planning calls and
network searches for small talk.

The key comes only from the environment or .env; its value is never printed.
Run:  python eval/zone2_probe.py
LUMEN_ENV_DIR specifies where to find .env when running from a worktree.
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
sys.path.insert(0, str(ROOT / "eval"))
ENV_FILE = Path(os.getenv("LUMEN_ENV_DIR", r"C:/Users/egork/Documents/Default Project/Lumen")) / ".env"

BASE_DEFAULT = "https://api.munito.ai/inference/v1"


def _env(name: str, default: str = "") -> str:
    value = (os.getenv(name, "") or "").strip()
    if value:
        return value
    if not ENV_FILE.exists():
        return default
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("\ufeff")
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


async def main() -> None:
    key = _env("MUNITO_API_KEY")
    if not key:
        print("НЕТ КЛЮЧА MUNITO_API_KEY — выхожу")
        return

    import aiohttp

    import bot
    import lumen_agent
    from search_trigger_corpus import SHOULD_NOT_SEARCH
    from lumen_router_config import _MUNITO_ORDER

    base = _env("MUNITO_BASE_URL", BASE_DEFAULT).rstrip("/")
    bot.MUNITO_API_KEY = key
    bot.MUNITO_BASE_URL = base
    stats = {"requests": 0, "errors": 0}

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
        return data

    async def fake_build(chat_id, user_text, model_id, *, history_meta=None, current_parts=None):
        return [{"role": "system", "content": bot.get_system_prompt(model_id)},
                {"role": "user", "content": user_text}]

    bot._munito_request = fake_request
    bot._build_openai_turn_messages = fake_build

    from lumen_router_config import agent_max_searches as _prod_max_searches
    head = _MUNITO_ORDER[0]
    print(f"голова: {head} · зона 2 · вопросов {len(SHOULD_NOT_SEARCH)}")
    false_searches = []
    for question in SHOULD_NOT_SEARCH:
        t0 = time.monotonic()
        turn = await lumen_agent.run_search_turn(
            chat_id=900001, user_prompt=question, clean_prompt=question, model_id=head,
            effort="medium", max_searches=_prod_max_searches("medium"), status=None,
            deadline=time.monotonic() + 45.0, history_meta=None,
        )
        dt = time.monotonic() - t0
        if turn.searches:
            false_searches.append((question, turn.queries))
        print(f"  {dt:5.2f}s поиск={turn.searches} запросы={turn.queries} · {question[:44]}")
        await asyncio.sleep(1.0)

    print("\n=== сводка зоны 2 ===")
    print(f"вопросов {len(SHOULD_NOT_SEARCH)} · лишних поисков {len(false_searches)}")
    for question, queries in false_searches:
        print(f"  ЛОЖНЫЙ: {question} -> {queries}")
    print(f"планирующих вызовов {stats['requests']} · ошибок {stats['errors']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
