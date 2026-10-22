"""Сверка реплики Gemma-промпта в eval/persona_models.py с боевой функцией бота.

Зачем тест: замер персоны (eval/persona_models.py) повторяет фейковый
identity-обмен для Gemma — у неё нет канала system_instruction, и бот передаёт
персону обычным текстом в начале диалога. Реплика лежит в другом файле, и без
сверки она рано или поздно разойдётся с боевой версией: замер будет мерить уже
не тот запрос, который уходит в прод.

Что ломает тест: любое изменение текста/структуры identity-обмена в
lumen_routes._build_gemma_identity_contents (заметка, чеклист, напоминание
перед ответом) — реплика в eval/ перестанет совпадать, и тест упадёт.

Почему существующие тесты не ловят: тесты бота проверяют поведение Gemma-пути
(системный промпт есть, инструментов нет), но ничего не связывает копию в
eval/ с оригиналом — это разные файлы, между которыми нет ссылки.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest
from google.genai import types

import bot
import lumen_errors
import lumen_routes

REPO = Path(__file__).resolve().parent.parent


def _load_persona_models():
    spec = importlib.util.spec_from_file_location("persona_models", REPO / "eval" / "persona_models.py")
    module = importlib.util.module_from_spec(spec)
    # dataclass в модуле ищет свой класс через sys.modules — без регистрации падает
    sys.modules["persona_models"] = module
    spec.loader.exec_module(module)
    return module


class _FixedDatetime(datetime):
    """Время заморожено: оба варианта текста содержат текущую дату и время."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 22, 12, 0, 0)


@pytest.fixture
def persona_models(monkeypatch):
    module = _load_persona_models()
    monkeypatch.setattr(lumen_errors, "datetime", _FixedDatetime)
    monkeypatch.setattr(lumen_routes, "datetime", _FixedDatetime)
    monkeypatch.setattr(module, "datetime", _FixedDatetime)
    return module


def _texts(contents):
    return [part.parts[0].text for part in contents]


def test_gemma_identity_contents_matches_bot(persona_models):
    contents = [types.Content(role="user", parts=[types.Part.from_text(text="привет")])]
    mine = persona_models.gemma_identity_contents("gemma-4-31b-it", contents)
    theirs = bot._build_gemma_identity_contents("gemma-4-31b-it", contents)
    assert _texts(mine) == _texts(theirs)


def test_gemma_identity_contents_reminder_matches_bot_for_long_history(persona_models):
    # Длинная история (>12 сообщений) добавляет напоминание перед ответом —
    # именно эту ветку копия тоже должна повторять.
    contents = [
        types.Content(role="user" if i % 2 == 0 else "model",
                      parts=[types.Part.from_text(text=f"реплика {i}")])
        for i in range(13)
    ]
    mine = persona_models.gemma_identity_contents("gemma-4-31b-it", contents)
    theirs = bot._build_gemma_identity_contents("gemma-4-31b-it", contents)
    assert _texts(mine) == _texts(theirs)


def test_persona_questions_cover_eval_rubric():
    # Набор вопросов замера должен совпадать с рубрикой PROMPT_EVAL.md по номерам.
    import json

    data = json.loads((REPO / "eval" / "persona_questions.json").read_text(encoding="utf-8"))
    ids = sorted(q["id"] for q in data["questions"])
    assert ids == list(range(1, 26))
    for question in data["questions"]:
        assert question.get("expect"), f"вопрос {question['id']} без ожидания"
