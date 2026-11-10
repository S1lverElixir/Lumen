"""Матрица маршрутов рисования: какие фразы становятся картинкой, а какие нет.

Защищает паритет с ChatGPT (10.11.2026): просьбу нарисовать должно видно и без
триггера в первом слове, но вопрос или служебный глагол не должны выдавать
ненужную генерацию. Краснеет, если сузить триггеры, снять обвязку вместе с
местоимением правки или перепутать «сделай картинку» с «сделай эту картинку».
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot
from lumen_message_parse import (
    DRAW_TRIGGER_PREFIXES,
    EDIT_TRIGGER_PREFIXES,
    REPEAT_TRIGGER_PREFIXES,
    _is_new_artifact_request,
    _match_draw_intent,
    _match_trigger_with_fillers,
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
    "сделай обложку для телеграм-канала",
    "сделай плакат с китом",
    "сделай наклейку с молнией",
    "сделай аватарку с лисом",
    # Название артефакта своими словами: местоимение правки не должно
    # читаться внутри слов «талисман», «телефон», «телеграм».
    "придумай талисман для олимпиады",
    "хочу обои на телефон с космосом",
    "нужен макет визитки",
    "хочу стикерпак с котами",
    "придумай эмблему для клуба",
    "накидай баннер для сайта",
    "накидай мемов с котом",
    "нужна открытка на день рождения",
    "хочу превью для видео",
    "давай картинку с цитатой",
    "можешь сделать картинку с котом",
    "сгенерируй мне аватарку",
    "design a logo for a bakery",
    "make me a sticker of a cat",
    "I need a poster for a concert",
    "give me an illustration of a dragon",
    "create a wallpaper with mountains",
    "can you make an image of a castle",
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
    "что такое стикерпак",
    "как сделать логотип в фотошопе",
    "покажи обои на рабочий стол",
    "пришли картинку которую рисовал",
    "сколько стоит сделать логотип",
    "объясни что такое превью",
    "design me a workout plan",
    "give me a list of movies",
    "make me a coffee",
    "how do I draw a cat",
    "show me the picture you saved",
]


def _intent(text):
    low = text.lower().strip()
    if _match_trigger_with_fillers(low, DRAW_TRIGGER_PREFIXES)[0]:
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
    "make the sky stormier",
    "make it darker",
    "remove the background",
    "add a hat to the cat",
])
def test_edit_phrases_stay_edit(text):
    low = text.lower().strip()
    assert _match_trigger_with_fillers(low, EDIT_TRIGGER_PREFIXES)[0], text
    assert _intent(text) != "draw", text


@pytest.mark.parametrize("text", [
    "ещё вариант", "еще варианты", "другой вариант кота", "another version",
])
def test_repeat_phrases_stay_repeat(text):
    low = text.lower().strip()
    assert _match_trigger_with_fillers(low, REPEAT_TRIGGER_PREFIXES)[0], text


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


# ── Разбор просьбы без названия артефакта и местоименной («нарисуй это»).
# Без этих тестов шаг обращения к модели мог выпасть из пути, а «нарисуй это»
# снова уезжало провайдеру словом «это» и рисовало мусор (паритет 10.11.2026).
_RESOLVED = "A ginger cat sitting on the moon above a blue sea, soft light, wide shot."


def _ctx_env(monkeypatch, rate_guard_setup, text, answer):
    message = rate_guard_setup()
    message.text = text
    calls = []

    def _sys(msgs):
        first = (msgs or [{}])[0]
        return first.get("content", "") if isinstance(first, dict) else str(getattr(first, "content", ""))

    def _usr(msgs):
        second = (msgs or [{}, {}])[1]
        return second.get("content", "") if isinstance(second, dict) else str(getattr(second, "content", ""))

    async def fake_munito(path, method="GET", *, json_body=None, deadline=None):
        if "Decide what the user wants drawn" in _sys(json_body.get("messages") if isinstance(json_body, dict) else None):
            calls.append(_usr(json_body["messages"]))
        return {"choices": [{"message": {"content": answer}}]}

    state = {"history": [
        {"role": "user", "sender": "SilverElixir", "content": "кот рыжий сидит на луне"},
        {"role": "assistant", "content": "интересный образ"},
    ]}
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_munito_request", fake_munito)
    monkeypatch.setattr(bot, "get_state", lambda chat_id: state)
    message._ctx_calls = calls
    return message


def test_anaphoric_draw_resolves_from_history(rate_guard_setup, monkeypatch):
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это", _RESOLVED)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_awaited_once()
    assert bot.inline_draw.await_args.args[1] == _RESOLVED
    # История чата ушла в вопрос модели — иначе разрешать было бы нечем.
    assert "кот рыжий сидит на луне" in message._ctx_calls[0]


def test_anaphoric_draw_without_context_asks_for_description(rate_guard_setup, monkeypatch):
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это", "NONE")
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()
    bot._safe_reply.assert_awaited_once()
    assert "/draw" in bot._safe_reply.await_args.args[1]


def test_scene_request_without_artifact_word_draws_resolved_text(rate_guard_setup, monkeypatch):
    message = _ctx_env(monkeypatch, rate_guard_setup, "хочу увидеть кота на луне", _RESOLVED)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_awaited_once()
    assert bot.inline_draw.await_args.args[1] == _RESOLVED


def test_scene_request_answered_none_stays_text(rate_guard_setup, monkeypatch):
    message = _ctx_env(monkeypatch, rate_guard_setup, "хочу увидеть кота на луне", "NONE")
    run_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", run_route)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()
    # Ветка вызывалась реально (иначе «не рисуем» тривиально верно) и сообщение
    # доехало до текстовой модели (ревью покрытия 10.11.2026).
    assert len(message._ctx_calls) == 1
    run_route.assert_awaited()


def test_question_about_imagining_does_not_call_the_model(rate_guard_setup, monkeypatch):
    # «как представить себе кота» — это вопрос, а не заказ: лишний вызов модели
    # и тем более картинка тут были бы багом.
    message = _ctx_env(monkeypatch, rate_guard_setup, "как представить себе кота на луне", _RESOLVED)
    asyncio.run(bot._handle_message_core(message))
    assert message._ctx_calls == []
    bot.inline_draw.assert_not_awaited()


def test_anaphoric_draw_with_photo_edits_that_photo(rate_guard_setup, monkeypatch):
    # С приложенным фото «нарисуй это» правит фото, а не рисует по словам из
    # истории: иначе вложение молча игнорировалось.
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это", _RESOLVED)
    photo = SimpleNamespace(file_id="f1", file_unique_id="u1", width=10, height=10, file_size=10)
    message.photo = [photo]
    ref = (b"\xff\xd8jpeg", "image/jpeg")

    async def fake_refs(msg, state, *, is_private, implicit):
        return [ref]

    monkeypatch.setattr(bot, "_resolve_edit_references", fake_refs)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_awaited_once()
    assert bot.inline_draw.await_args.kwargs.get("reference") == [ref]
    assert message._ctx_calls == []


# ── Маршруты по находкам слепого ревью (10.11.2026): обвязка, местоимение,
# заказ артефакта поверх правки, число вариантов из фразы человека.
def test_core_polite_prefix_keeps_the_subject(rate_guard_setup, monkeypatch):
    # «please draw a cat in a cat» теряло «please draw» дважды: до модели
    # доходило «in a hat» (ревью 10.11.2026).
    message = rate_guard_setup()
    message.text = "please draw a cat in a hat"
    prompts = []

    async def fake_draw(msg, prompt, **kwargs):
        prompts.append(prompt)
        return True

    monkeypatch.setattr(bot, "inline_draw", fake_draw)
    asyncio.run(bot._handle_message_core(message))
    assert prompts == ["a cat in a hat"], prompts


def test_core_repeat_after_filler_repeats_the_subject(rate_guard_setup, monkeypatch):
    # «ну ещё вариант кота»: хвост повтора режется из тела без обвязки,
    # иначе сдвиг на «ну » съедал буквы сюжета (ревью 10.11.2026).
    message = rate_guard_setup()
    message.text = "ну ещё вариант кота"
    state = {"last_draw_prompt": "A ginger cat.", "last_drawn_at": time.time(), "last_drawn_user": 456}
    monkeypatch.setattr(bot, "get_state", lambda cid: state)
    drawn = []

    async def fake_variants(msg, prompt, *, variants=1, reference=None, prepared=None):
        drawn.append((prompt, variants))
        return None

    monkeypatch.setattr(bot, "inline_draw_variants", fake_variants)
    asyncio.run(bot._handle_message_core(message))
    assert drawn == [("кота", 1)], drawn


def test_core_artifact_word_over_photo_is_an_edit(rate_guard_setup, monkeypatch):
    # «сделай картинку ярче» с отреплайченным фото — правка: заказ артефакта
    # не имеет права уводить её в новую генерацию (ревью 10.11.2026).
    message = rate_guard_setup()
    message.text = "сделай картинку ярче"
    message.reply_to_message = SimpleNamespace(
        message_id=7, photo=[SimpleNamespace(file_id="fid7", file_unique_id="u7", width=8, height=8, file_size=8)],
        text=None, caption=None, from_user=SimpleNamespace(id=456),
    )
    resolved = []

    async def fake_refs(msg, state, *, is_private, implicit):
        resolved.append(implicit)
        return (b"\xff\xd8jpeg", "image/jpeg")

    monkeypatch.setattr(bot, "_resolve_edit_references", fake_refs)
    drawn = []

    async def fake_draw(msg, prompt, **kwargs):
        drawn.append((prompt, kwargs.get("reference")))
        return True

    monkeypatch.setattr(bot, "inline_draw", fake_draw)
    asyncio.run(bot._handle_message_core(message))
    assert resolved, "правки не было: ветка ушла в генерацию"
    assert drawn and drawn[0][1] is not None, drawn


def test_core_variants_count_comes_from_human_phrase(rate_guard_setup, monkeypatch):
    # Резолвер мог вернуть «A ginger cat, 3 variants»: серия из чужого текста
    # стоила бы человеку три слота, тогда как человек просил один кадр
    # (ревью 10.11.2026).
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это",
                       "A ginger cat on the moon, 3 variants, photorealistic.")
    asyncio.run(bot._handle_message_core(message))
    assert bot.inline_draw.await_count == 1, bot.inline_draw.await_args_list
    assert bot.inline_draw.await_args.args[1].startswith("A ginger cat")


def test_core_variants_count_survives_anaphora(rate_guard_setup, monkeypatch):
    # А когда о вариантах попросил сам человек, их терять нельзя.
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это, 3 варианта",
                       "A ginger cat on the moon, photorealistic.")
    asyncio.run(bot._handle_message_core(message))
    assert bot.inline_draw.await_count == 3, bot.inline_draw.await_args_list
