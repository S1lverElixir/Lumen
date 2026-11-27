"""Cross-check the persona measurement questions (eval/persona_questions.json) against the PROMPT_EVAL.md rubric.

Why this test: questions and their rubric live in separate files; without a cross-check, their numbers drift and the wrong criterion is scored.

What breaks it: adding or removing a question without updating the rubric, or a question missing the expect field.

Why existing tests miss it: this is data, not code, and only this file checks it.

Tests for the Gemma identity-exchange replica in eval/persona_models.py were removed on 05.11.2026 along with Gemma itself: the production function the replica was checked against no longer exists.
"""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_persona_questions_cover_eval_rubric():
    # The benchmark question set must match the PROMPT_EVAL.md rubric by number.
    import json

    data = json.loads((REPO / "eval" / "persona_questions.json").read_text(encoding="utf-8"))
    ids = sorted(q["id"] for q in data["questions"])
    assert ids == list(range(1, 26))
    for question in data["questions"]:
        assert question.get("expect"), f"вопрос {question['id']} без ожидания"


def test_multilingual_questions_cover_every_supported_language():
    # The measurement branch "every answer like Claude" across all languages: without comparison
    # the language silently drops out of the check, while the owner considers it verified.
    import json

    import lumen_lang

    data = json.loads((REPO / "eval" / "persona_questions_multilingual.json").read_text(encoding="utf-8"))
    ids = sorted(q["id"] for q in data["questions"])
    assert ids == list(range(1, len(data["questions"]) + 1))
    assert {q["lang"] for q in data["questions"]} == set(lumen_lang.SUPPORTED_LANGS), \
        sorted({q["lang"] for q in data["questions"]} ^ set(lumen_lang.SUPPORTED_LANGS))
    for question in data["questions"]:
        assert question.get("expect"), f"вопрос {question['id']} без ожидания"
        assert question.get("turns"), f"вопрос {question['id']} без текста"
def test_answer_script_check_follows_the_question_language(monkeypatch):
    # Deterministic check of the answer language (12.11.2026 measurement across all
    # SUPPORTED_LANGS): answering in a language other than the user’s is a parity failure,
    # the judge does not always catch it. A separate bot-name check: "No, I
    # Lumen." — a Russian response, not a mixture of alphabets.
    # syspath_prepend cleans up after itself: the eval path does not leak into neighboring tests (F-167).
    monkeypatch.syspath_prepend(str(REPO / "eval"))
    import persona_models

    assert persona_models.answer_script_ok("ru", "Нет, я Lumen.") is True
    assert persona_models.answer_script_ok("ru", "No, I am Lumen.") is False
    assert persona_models.answer_script_ok("th", "กรุงเทพฯ เป็นเมืองหลวงของไทย") is True
    assert persona_models.answer_script_ok("th", "Bangkok is the capital of Thailand") is False
    assert persona_models.answer_script_ok("ar", "العاصمة هي كانبيرا") is True
    assert persona_models.answer_script_ok("hi", "नई दिल्ली भारत की राजधानी है") is True
    assert persona_models.answer_script_ok("hi", "New Delhi is the capital") is False
    assert persona_models.answer_script_ok("vi", "Thủ đô của Úc là Canberra.") is True
    assert persona_models.answer_script_ok("pl", "Stolicą Australii jest Canberra.") is True
    assert persona_models.answer_script_ok("kk", "Австралияның астанасы — Канберра.") is True
    assert persona_models.answer_script_ok("en", "The capital of Australia is Canberra.") is True
    assert persona_models.answer_script_ok("ru", "") is False
def test_sanitize_no_proxy_drops_bracketed_ipv6_only(monkeypatch):
    # The owner's global NO_PROXY containing "[::1]" breaks any httpx call used for measurement
    # (the Gemini judge failed with InvalidURL: Invalid port, 12.11.2026). Cleanup
    # must remove only parenthesized entries and leave ordinary rules.
    # syspath_prepend cleans up after itself: the eval path does not leak into neighboring tests (F-167).
    monkeypatch.syspath_prepend(str(REPO / "eval"))
    import persona_models

    # monkeypatch saves the original value and restores it during teardown (F-167).
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1],.example.com")
    touched = persona_models.sanitize_no_proxy()
    assert touched == ["NO_PROXY"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,.example.com"
    assert persona_models.sanitize_no_proxy() == []
def test_style_flags_catches_the_real_manner_defects(monkeypatch):
    # A cheap deterministic grid for the response: a protocol marker in the text,
    # a string of emoji instead of the requested 1-2, a list where prose was requested,
    # a term removed by polishing. This catches what the judge misses.
    # syspath_prepend rolls back after itself: the eval path does not leak into neighboring tests (F-167).
    monkeypatch.syspath_prepend(str(REPO / "eval"))
    import persona_models

    assert persona_models.style_flags({"id": 11, "style": {"keep_terms": ["for", "while"]}},
                                      "الفرق بين for و while واضح") == []
    assert "term-lost:while" in persona_models.style_flags(
        {"id": 11, "style": {"keep_terms": ["for", "while"]}}, "разница в for")
    assert "search-marker" in persona_models.style_flags({"id": 1}, "[[SEARCH]]")
    assert "emoji-4>2" in persona_models.style_flags({"id": 5}, "С днём 🎉🎂✨🎈 рождения")
    assert "emoji-0<asked" in persona_models.style_flags(
        {"id": 5, "style": {"emoji_min": 1}}, "С днём рождения")
    assert "bullets-6" in persona_models.style_flags(
        {"id": 3, "style": {"bullets_max": 2}}, "\n".join("- пункт %d" % i for i in range(6)))
    assert "no-table" in persona_models.style_flags({"id": 8, "style": {"table": True}}, "обычный текст")
    assert persona_models.style_flags({"id": 13, "style": {"table": True}}, "| a | b |\n|---|---|") == []
    assert "no-math" in persona_models.style_flags({"id": 15, "style": {"math": True}}, "площадь равна пи r в квадрате")
    assert persona_models.style_flags({"id": 15, "style": {"math": True}}, "S = $\\pi r^2$, \u221a3 \u2248 1,73") == []
    assert "questions-0<asked" in persona_models.style_flags({"id": 17, "style": {"questions_min": 1}}, "Вот скрипт.")
    assert any(f.startswith("len-") for f in persona_models.style_flags({"id": 6, "style": {"chars_max": 5}}, "длинный ответ"))
    assert "empty" in persona_models.style_flags({"id": 2}, "   ")


def test_judge_score_parser_rejects_missing_or_ambiguous_scores(monkeypatch):
    # R3-10 (review 27.11.2026): prose containing a digit or a missing score used to
    # fabricate a measurement; only an explicit integer in the 0..2 JSON object is valid.
    monkeypatch.syspath_prepend(str(REPO / "eval"))
    import persona_models

    assert persona_models.parse_score('{"score": 0, "reason": "fail"}') == (0, "fail")
    assert persona_models.parse_score('{"score": "2"}') == (2, "")
    for raw in (
        '{"reason": "criterion 1 missing"}', "not JSON, criterion 2 mentioned",
        "true", "1.9", '{"score": true}', '{"score": 1.9}', '{"score": 3}',
        '[]', '{"score": -1}', 'prefix {"score": 2} suffix',
        'Here is the score:\n{"score": 1, "reason": "ok"}\nDone.',
        '```json\n{"score": 2}\n```', '{"score": 2}{"score": 1}',
    ):
        assert persona_models.parse_score(raw)[0] is None, raw
