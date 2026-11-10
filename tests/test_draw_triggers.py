"""Матрица маршрутов рисования: какие фразы становятся картинкой, а какие нет.

Защищает паритет с ChatGPT (10.11.2026): просьбу нарисовать должно видно и без
триггера в первом слове, но вопрос или служебный глагол не должны выдавать
ненужную генерацию. Краснеет, если сузить триггеры, снять обвязку вместе с
местоимением правки или перепутать «сделай картинку» с «сделай эту картинку».
"""
import asyncio

import pytest

import bot
from lumen_message_parse import (
    DRAW_TRIGGER_PREFIXES,
    EDIT_TRIGGER_PREFIXES,
    REPEAT_TRIGGER_PREFIXES,
    _is_new_artifact_request,
    _match_draw_intent,
    _match_trigger_with_fillers,
    _strip_trigger_fillers,
)

DRAW_INTENTS = [
    # Обвязка перед глаголом не меняет смысл просьбы.
    "ну нарисуй кота",
    "мне нарисуй кота",
    "и нарисуй ещё лису",
    "пожалуйста, нарисуй закат",
    "давай нарисуй кота",
    # Императив в конце фразы.
    "кот в шляпе, нарисуй",
    # Заказ артефакта без глагола рисования.
    "хочу картинку с котом",
    "нужен логотип кофейни",
    "давай иллюстрацию к статье",
    "придумай маскота для стартапа",
    "накидай вариантов обложки альбома",
    "хочу обои на рабочий стол с горами",
    "сделай аватарку с лисом",
    # Слабый глагол + новый артефакт = генерация, а не правка.
    "сделай картинку кота",
    "сделай мне постер для концерта",
    # Вежливые формы.
    "ты можешь нарисовать дракона?",
    "можешь нарисовать кота?",
    # Английский.
    "draw a cat",
    "make me an image of a cat",
    "can you draw a cat?",
    "I want an image of a cat",
    "give me a picture of a cat",
    "picture of a cat sitting on a fence",
    "generate a logo for a coffee shop",
    "create a poster for a jazz night",
    "illustrate this scene",
    "render a cozy cabin in the woods",
]

NOT_DRAW_INTENTS = [
    # Вопросы и инструкции — текст, а не генерация.
    "как нарисовать кота карандашом",
    "почему коты на картинах выглядят грустными",
    "объясни, как художники рисуют перспективу",
    "что такое логотип кофейни",
    "сколько стоит нарисовать портрет",
    "кто нарисовал Мону Лизу",
    "можно ли есть картинку",
    "что на этой картинке",
    # Служебные глаголы про уже существующий файл.
    "покажи картинку из прошлого запроса",
    "сохрани картинку",
    "пришли фото кота",
    "найди картинку кота в интернете",
    "переведи: нарисуй кота",
    # Совсем не про картинки.
    "хочу кофе",
    "кот",
    "make me a sandwich",
    "how do I draw a cat",
    "show me the picture you saved",
]


def _intent(text):
    low = text.lower().strip()
    core = _strip_trigger_fillers(low)[0]
    if _match_trigger_with_fillers(low, core, DRAW_TRIGGER_PREFIXES)[0]:
        return "draw"
    kind, _content = _match_draw_intent(low, text)
    return kind


@pytest.mark.parametrize("text", DRAW_INTENTS)
def test_draw_intent_recognized(text):
    assert _intent(text) == "draw", text


@pytest.mark.parametrize("text", NOT_DRAW_INTENTS)
def test_non_draw_phrases_are_not_draw(text):
    assert _intent(text) != "draw", text


@pytest.mark.parametrize("text", [
    "измени фон на синий",
    "убери шляпу",
    "добавь солнце",
    "сделай темнее",
    "сделай эту картинку ярче",
    "измени её пропорции",
])
def test_edit_phrases_stay_edit(text):
    low = text.lower().strip()
    core = _strip_trigger_fillers(low)[0]
    assert _match_trigger_with_fillers(low, core, EDIT_TRIGGER_PREFIXES)[0], text
    assert _intent(text) != "draw", text


@pytest.mark.parametrize("text", ["ещё вариант", "другой вариант кота", "another version"])
def test_repeat_phrases_stay_repeat(text):
    low = text.lower().strip()
    core = _strip_trigger_fillers(low)[0]
    assert _match_trigger_with_fillers(low, core, REPEAT_TRIGGER_PREFIXES)[0], text


def test_new_artifact_vs_existing_image():
    # «картинку кота» не должно читаться как местоимение «та»: это новый заказ,
    # а не правка (ревью паритета 10.11.2026).
    assert _is_new_artifact_request("сделай картинку кота")
    assert _is_new_artifact_request("сделай мне постер для концерта")
    assert not _is_new_artifact_request("сделай эту картинку ярче")
    assert not _is_new_artifact_request("измени её пропорции")


def test_intent_content_keeps_original_case():
    # Буквальные надписи важны в исходном регистре: провайдер рисует текст
    # с картинки, а не с нижнего регистра.
    kind, content = _match_draw_intent(
        "хочу открытку с надписью «С Днём Рождения»",
        "Хочу открытку с надписью «С Днём Рождения»",
    )
    assert kind == "draw"
    assert "С Днём Рождения" in content


def test_intent_does_not_eat_first_letter_of_verb():
    # Срез императива обязан быть по границе слова: «illustrate this scene»
    # давало «i this scene» (ревью паритета 10.11.2026).
    kind, content = _match_draw_intent("illustrate this scene", "illustrate this scene")
    assert kind == "draw"
    assert content == "this scene"


@pytest.mark.parametrize(("text", "expected"), [
    ("хочу картинку с котом", "хочу картинку с котом"),
    ("сделай картинку кота", "сделай картинку кота"),
    ("нужен логотип кофейни", "нужен логотип кофейни"),
    # Глагол из середины фразы вырезается: в модель уходит сюжет.
    ("кот в шляпе, нарисуй", "кот в шляпе"),
])
def test_core_routes_natural_requests_to_draw(rate_guard_setup, text, expected):
    # Матчер без проводки в message_core бесполезен: проверяем, что просьба
    # реально доезжает до генерации, а не уходит текстовой модели.
    message = rate_guard_setup()
    message.text = text
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_awaited_once()
    assert bot.inline_draw.await_args.args[1] == expected


def test_core_keeps_question_as_text(rate_guard_setup):
    message = rate_guard_setup()
    message.text = "как нарисовать кота карандашом"
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()


def test_core_keeps_service_request_as_text(rate_guard_setup):
    # «покажи картинку из прошлого запроса» — это про уже существующий файл,
    # генерация по такому запросу была бы лишней картинкой в чате.
    message = rate_guard_setup()
    message.text = "покажи картинку из прошлого запроса"
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()
