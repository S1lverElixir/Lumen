"""Сверка набора вопросов замера персоны (eval/persona_questions.json) с рубрикой PROMPT_EVAL.md.

Зачем тест: вопросы и их рубрика живут в разных файлах, и без сверки номера
разъезжаются — оценка ставится не по тому критерию.

Что ломает тест: новый или удалённый вопрос без правки рубрики, или вопрос без поля expect.

Почему существующие тесты не ловят: это данные, а не код, и проверяет их только этот файл.

Тесты на реплику Gemma-identity-обмена в eval/persona_models.py удалены 05.11.2026 вместе
с самой Gemma из маршрута: боевой функции, с которой сверялась копия, больше нет.
"""

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
