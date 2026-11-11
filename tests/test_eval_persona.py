"""Сверка набора вопросов замера персоны (eval/persona_questions.json) с рубрикой PROMPT_EVAL.md.

Зачем тест: вопросы и их рубрика живут в разных файлах, и без сверки номера
разъезжаются — оценка ставится не по тому критерию.

Что ломает тест: новый или удалённый вопрос без правки рубрики, или вопрос без поля expect.

Почему существующие тесты не ловят: это данные, а не код, и проверяет их только этот файл.

Тесты на реплику Gemma-identity-обмена в eval/persona_models.py удалены 05.11.2026 вместе
с самой Gemma из маршрута: боевой функции, с которой сверялась копия, больше нет.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_persona_questions_cover_eval_rubric():
    # Набор вопросов замера должен совпадать с рубрикой PROMPT_EVAL.md по номерам.
    import json

    data = json.loads((REPO / "eval" / "persona_questions.json").read_text(encoding="utf-8"))
    ids = sorted(q["id"] for q in data["questions"])
    assert ids == list(range(1, 26))
    for question in data["questions"]:
        assert question.get("expect"), f"вопрос {question['id']} без ожидания"


def test_multilingual_questions_cover_every_supported_language():
    # Ветка замера «каждый ответ как у Claude» на всех языках: без сверки
    # язык тихо выпадает из проверки, а владелец считает его проверенным.
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
def test_answer_script_check_follows_the_question_language():
    # Детерминированная сверка языка ответа (замер 12.11.2026 по всем
    # SUPPORTED_LANGS): ответ не на языке человека — это провал паритета,
    # и судья его ловит не всегда. Отдельная проверка имени бота: «Нет, я
    # Lumen.» — русский ответ, а не смешение алфавитов.
    sys.path.insert(0, str(REPO / "eval"))
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
def test_sanitize_no_proxy_drops_bracketed_ipv6_only():
    # Глобальный NO_PROXY владельца с «[::1]» ломает любой httpx-вызов замера
    # (судья Gemini падал с InvalidURL: Invalid port, 12.11.2026). Чистка
    # обязана снять только скобочные записи и оставить обычные правила.
    sys.path.insert(0, str(REPO / "eval"))
    import persona_models

    # На Windows имена переменных регистронезависимы: сначала чистим обе,
    # иначе pop("no_proxy") снял бы только что заданный NO_PROXY.
    os.environ.pop("no_proxy", None)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,[::1],.example.com"
    touched = persona_models.sanitize_no_proxy()
    assert touched == ["NO_PROXY"]
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,.example.com"
    assert persona_models.sanitize_no_proxy() == []
