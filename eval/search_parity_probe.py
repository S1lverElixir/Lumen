"""Live external-search probe: whether the new layer finds an answer to a question requiring fresh data.

Measures not “whether snippets arrived,” but “whether the block contains an answer”: each case
has required tokens. No keys or secrets; network access is to public sources.
Run: python eval/search_parity_probe.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lumen_search  # noqa: E402

# (question, required tokens in the block, whether we expect at least one result)
CASES = [
    ("Привет! Подскажи, пожалуйста, а кто сейчас возглавляет Центральный банк России?",
     ["набиуллина"], True),
    ("какая ключевая ставка ЦБ России", ["ставк"], True),
    ("сколько сейчас стоит bitcoin", ["usd"], True),
    ("курс евро к рублю сегодня", ["eur"], True),
    ("какая сейчас погода в Минске", ["°c"], True),
    ("кто сейчас канцлер Германии", ["канцлер"], True),
    ("последние новости Беларуси сегодня", ["беларус"], True),
    ("what is the latest official unemployment rate in the US", ["unemploy"], True),
    ("какой процессор лучше купить для игр в 2026", ["процессор"], True),
    # Cases where an honest answer is “the sources do not contain this”: silence is correct here.
    ("кто такая Абдулманова Угона", [], False),
]

_TIMEOUT_SEC = 10.0


async def _one(question: str, tokens: list[str], expect_results: bool) -> tuple[bool, str]:
    started = time.monotonic()
    items = await lumen_search.fetch_web_snippets(question, timeout_sec=_TIMEOUT_SEC)
    block = lumen_search.format_snippets_for_prompt(items)
    blob = block.lower()
    elapsed = time.monotonic() - started
    missing = [token for token in tokens if token not in blob]
    if expect_results and not items:
        return False, f"ПУСТО за {elapsed:.1f}с"
    if missing:
        return False, f"нет {missing} в блоке {len(block)} символов за {elapsed:.1f}с"
    return True, f"{len(items)} источников, блок {len(block)} символов, {elapsed:.1f}с"


async def main() -> int:
    passed = 0
    for question, tokens, expect_results in CASES:
        ok, detail = await _one(question, tokens, expect_results)
        passed += int(ok)
        print(f"{'OK  ' if ok else 'MISS'} {question[:58]!r} — {detail}")
    print(f"\nИтого: {passed}/{len(CASES)}")
    # Nonzero exit code on MISS: otherwise a probe failure is invisible (review 17.11.2026).
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
