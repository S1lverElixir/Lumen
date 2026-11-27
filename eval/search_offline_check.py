"""Offline check of the search pipeline on cases from the weekly corpus (eval/search_cases.json).

No network: trigger (_wants_web_search), query forms (plan_search_queries),
intents (detect_intents), and wiki signal (_wants_game_wiki). A case where the regex is silent
but a search is needed is not a failure: the agent loop handles it (the model writes the queries);
such cases are marked “model.” A failure is the regex calling for a search where none is needed.
Run: python eval/search_offline_check.py
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import lumen_message_core as M  # noqa: E402
import lumen_search  # noqa: E402

cases = json.loads((ROOT / "eval" / "search_cases.json").read_text(encoding="utf-8"))["cases"]

false_positives: list[str] = []
model_zone: list[str] = []
intent_misses: list[str] = []
wiki_expected: list[int] = []
wiki_by_text: list[int] = []
rows: list[str] = []
for case in cases:
    question = case["question"]
    previous = (case.get("context") or [""])[-1]
    trigger = M._wants_web_search(question, previous=previous)
    expect = case["expect_search"]
    forms = lumen_search.plan_search_queries(question, context=previous)
    intents = lumen_search.detect_intents(question)
    want_intent = case.get("expect_intent")
    got_intent = next((k for k in ("news", "entity", "weather", "fx", "crypto") if intents[k]), None)
    wiki_signal = lumen_search._wants_game_wiki(question)
    if case.get("expect_wiki"):
        wiki_expected.append(case["id"])
        if wiki_signal:
            wiki_by_text.append(case["id"])
    verdict = ""
    if not expect and trigger:
        verdict = "ЛОЖНОЕ СРАБАТЫВАНИЕ"
        false_positives.append(case["id"])
    elif expect and not trigger:
        verdict = "модель"
        model_zone.append(case["id"])
    else:
        verdict = "ок"
    if want_intent and got_intent != want_intent:
        verdict += " интент≠"
        intent_misses.append(case["id"])
    rows.append(
        f"{verdict:<22} id={case['id']:<3} trig={int(trigger)!s:<5} forms={len(forms)} "
        f"intent={got_intent or '-':<7} wiki_sig={int(wiki_signal)} «{question[:52]}»"
    )

print("\n".join(rows))
print(f"\nитого: кейсов {len(cases)}, ложных срабатываний регекса {len(false_positives)}, "
      f"модель-зона (закрыто агентным циклом) {len(model_zone)}, интент-промахи {len(intent_misses)}")
print(f"вики-ожидания: {len(wiki_expected)} (id {wiki_expected}); из них вики-слово в самом "
      f"тексте: {len(wiki_by_text)}"
      + (f" (id {wiki_by_text})" if wiki_by_text else " — остальные закрывает модель агент-цикла "
         "(инструкция добавлять wiki к игровым сущностям), детерминированный слой тут бессилен"))
if false_positives:
    print(f"ложные: {false_positives}")
if model_zone:
    print(f"модель-зона: {model_zone}")
