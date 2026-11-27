"""Drawing-route matrix: which phrases produce an image and which do not.

Protects parity with ChatGPT (10.11.2026): a request to draw must be recognized even
without a trigger in the first word, but a question or auxiliary verb must not cause
unwanted generation. It fails if triggers are narrowed, the edit scaffolding is removed
along with the edit pronoun, or "make a picture" is confused with "make this picture".
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
    # A wrapper before the verb does not change the meaning of the request.
    "ну нарисуй кота",
    "мне нарисуй кота",
    "и нарисуй ещё лису",
    "пожалуйста, нарисуй закат",
    "давай нарисуй кота",
    # An imperative at the end of the phrase.
    "кот в шляпе, нарисуй",
    # An artifact order without a drawing verb.
    "хочу картинку с котом",
    "нужен логотип кофейни",
    "давай иллюстрацию к статье",
    "придумай маскота для стартапа",
    "накидай вариантов обложки альбома",
    "хочу обои на рабочий стол с горами",
    "сделай аватарку с лисом",
    # Weak verb + new artifact = generation, not editing.
    "сделай картинку кота",
    "сделай мне постер для концерта",
    "сделай обложку для телеграм-канала",
    "сделай плакат с китом",
    "сделай наклейку с молнией",
    "сделай аватарку с лисом",
    # Name the artifact in your own words: an edit pronoun must not
    # be read inside the words "talisman", "telephone", "Telegram".
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
    # Polite forms.
    "ты можешь нарисовать дракона?",
    "можешь нарисовать кота?",
    # English.
    "draw a cat",
    "make me an image of a cat",
    "can you draw a cat?",
    "I want an image of a cat",
    "give me a picture of a cat",
    "picture of a cat sitting on a fence",
    "generate a logo for a coffee shop",
    "create a poster for a jazz night",
    "illustrate this scene",
    "picture of a cat on a fence",
    # There is no request verb, but the artifact is the first word: this is how people ask for a poster,
    # a logo and layout in ChatGPT as well (review 10.11.2026).
    "логотип кофейни", "макет лендинга", "аватарку в круге", "стикерпак с капибарами",
    "визитка для кофейни", "превью для ютуба", "карикатуру на отца", "набросок замка",
    "emblem for a gaming clan", "poster for a gig", "thumbnail for the video",
    "сделай картинку с логотипом кота", "обложку книги про Марса",
]

NOT_DRAW_INTENTS = [
    # Questions and instructions are text, not generation.
    "как нарисовать кота карандашом",
    "почему коты на картинах выглядят грустными",
    "объясни, как художники рисуют перспективу",
    "что такое логотип кофейни",
    "сколько стоит нарисовать портрет",
    "кто нарисовал Мону Лизу",
    "можно ли есть картинку",
    "что на этой картинке",
    # Service verbs for an already existing file.
    "покажи картинку из прошлого запроса",
    "сохрани картинку",
    "пришли фото кота",
    "найди картинку кота в интернете",
    "переведи: нарисуй кота",
    # Nothing to do with images.
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
    # An artifact word at the start, but the phrase is a complaint or a statement about the past:
    # an image is not needed for them (review 10.11.2026).
    "картинка не загружается в браузере", "image not loading", "обложка альбома была лучшей",
    "картинка в галерее не видна", "this poster is ugly", "стикер был классный",
    "логотип не работает на тёмном фоне", "the sticker doesn't work",
]


def _intent(text):
    """The matrix asserts matcher ordering, not core wiring.

    The matrix guards the matchers themselves (F-170, 17.11.2026); end-to-end
    wiring through _handle_message_core is checked by the tests below in this file."""
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
    "ещё картинку", "ещё одну картинку кота",
])
def test_repeat_phrases_stay_repeat(text):
    low = text.lower().strip()
    assert _match_trigger_with_fillers(low, REPEAT_TRIGGER_PREFIXES)[0], text


def test_new_artifact_vs_existing_image():
    # "draw a cat" must not be read as the pronoun "it": this is a new request,
    # rather than an edit (parity review 10.11.2026).
    assert _is_new_artifact_request("сделай картинку кота")
    assert _is_new_artifact_request("сделай мне постер для концерта")
    assert not _is_new_artifact_request("сделай эту картинку ярче")
    assert not _is_new_artifact_request("измени её пропорции")


def test_intent_content_keeps_original_case():
    # Literal text and its original case matter: the provider draws text
    # from the image, not from lowercase text.
    kind, content = _match_draw_intent(
        "хочу открытку с надписью «С Днём Рождения»",
        "Хочу открытку с надписью «С Днём Рождения»",
    )
    assert kind == "draw"
    assert "С Днём Рождения" in content


def test_intent_does_not_eat_first_letter_of_verb():
    # The imperative cut must follow a word boundary: "illustrate this scene"
    # produced "i this scene" (parity review 10.11.2026).
    kind, content = _match_draw_intent("illustrate this scene", "illustrate this scene")
    assert kind == "draw"
    assert content == "this scene"


@pytest.mark.parametrize(("text", "expected"), [
    ("хочу картинку с котом", "хочу картинку с котом"),
    ("сделай картинку кота", "сделай картинку кота"),
    ("нужен логотип кофейни", "нужен логотип кофейни"),
    # A verb from the middle of the phrase is cut: the model receives the plot.
    ("кот в шляпе, нарисуй", "кот в шляпе"),
])
def test_core_routes_natural_requests_to_draw(rate_guard_setup, text, expected):
    # A matcher not wired into message_core is useless: verify that the request
    # actually reaches generation, rather than going to the text model.
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
    # “show the picture from the previous request” refers to an existing file,
    # generation for such a request would be an unnecessary picture in the chat.
    message = rate_guard_setup()
    message.text = "покажи картинку из прошлого запроса"
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()


# ── Parsing a request without an artifact name and with a pronoun ("draw this").
# Without these tests, the model-addressing step could fall out of the path, while “draw this”
# was sent to the provider as the word "this" again and drew garbage (parity 10.11.2026).
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
    # Chat history went into the model's question — otherwise there would be nothing to authorize with.
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


def test_scene_request_strips_variants_phrase_from_resolved_text(rate_guard_setup, monkeypatch):
    # The same F-169 defect on the visual-signal path: a quantity phrase from
    # the resolver's description must not enter the second branch's prompt.
    message = _ctx_env(monkeypatch, rate_guard_setup, "хочу увидеть кота на луне",
                       "A ginger cat on the moon, 3 variants, photorealistic.")
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_awaited_once()
    _prompt = bot.inline_draw.await_args.args[1]
    assert _prompt.startswith("A ginger cat") and "variants" not in _prompt


def test_scene_request_answered_none_stays_text(rate_guard_setup, monkeypatch):
    message = _ctx_env(monkeypatch, rate_guard_setup, "хочу увидеть кота на луне", "NONE")
    run_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", run_route)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()
    # The branch was actually called (otherwise “we do not draw” is trivially true), and the message
    # reached the text model (coverage review 10.11.2026).
    assert len(message._ctx_calls) == 1
    run_route.assert_awaited()


def test_question_about_imagining_does_not_call_the_model(rate_guard_setup, monkeypatch):
    # "how to imagine a cat" is a question, not an order: an extra model call
    # and an image here would be a bug anyway.
    message = _ctx_env(monkeypatch, rate_guard_setup, "как представить себе кота на луне", _RESOLVED)
    asyncio.run(bot._handle_message_core(message))
    assert message._ctx_calls == []
    bot.inline_draw.assert_not_awaited()


def test_anaphoric_draw_with_photo_edits_that_photo(rate_guard_setup, monkeypatch):
    # With an attached photo, “draw this” edits the photo instead of drawing from the words in
    # history: otherwise the attachment was silently ignored.
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


# ── Routes from blind-review findings (10.11.2026): wrapper, pronoun,
# artifact order on top of an edit; take the variant count from the person's phrase.
def test_core_polite_prefix_keeps_the_subject(rate_guard_setup, monkeypatch):
    # "please draw a cat in a cat" lost "please draw" twice: before reaching the model
    # reached "in a hat" (10.11.2026 review).
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
    # "one more cat variant": the repetition tail is cut from the body without the wrapper,
    # otherwise a shift by "well " ate plot letters (review 10.11.2026).
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
    # "make the picture brighter" with a replied-to photo is an edit: an artifact order
    # must not send it into a new generation (review 10.11.2026).
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
    # The resolver could return "A ginger cat, 3 variants": a series from someone else's text
    # would cost the person three slots when they asked for one frame
    # (review 10.11.2026).
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это",
                       "A ginger cat on the moon, 3 variants, photorealistic.")
    asyncio.run(bot._handle_message_core(message))
    assert bot.inline_draw.await_count == 1, bot.inline_draw.await_args_list
    _prompt = bot.inline_draw.await_args.args[1]
    assert _prompt.startswith("A ginger cat")
    # "3 variants" from foreign text must not remain in the prompt (review 17.11.2026).
    assert "3 variants" not in _prompt and "variants" not in _prompt


def test_core_variants_count_survives_anaphora(rate_guard_setup, monkeypatch):
    # And when the user asks for variants themselves, they must not be lost.
    message = _ctx_env(monkeypatch, rate_guard_setup, "нарисуй это, 3 варианта",
                       "A ginger cat on the moon, photorealistic.")
    asyncio.run(bot._handle_message_core(message))
    assert bot.inline_draw.await_count == 3, bot.inline_draw.await_args_list


def test_weak_english_draw_prefix_needs_a_visual_object():
    # External audit 16.11.2026: “draw a table of the results” went to
    # triggered image generation and burned a personal slot. Russian triggers are not restricted.
    from lumen_message_parse import _draw_trigger_is_visual
    assert _draw_trigger_is_visual("draw a", " table of the results") is False
    assert _draw_trigger_is_visual("can you draw", " a table of the results") is False
    assert _draw_trigger_is_visual("please draw", " a comparison of both") is False
    assert _draw_trigger_is_visual("please draw", " a table of the results and a short conclusion") is False
    assert _draw_trigger_is_visual("draw a", " table with the results") is False
    assert _draw_trigger_is_visual("draw a", " comparison of both") is False
    assert _draw_trigger_is_visual("paint", " the elevator scene") is True
    assert _draw_trigger_is_visual("draw a", " cat in a hat") is True
    assert _draw_trigger_is_visual("draw a", " summary card of the stats") is True
    assert _draw_trigger_is_visual("нарисуй", " таблицу сравнения") is True
    assert _draw_trigger_is_visual("sketch", " a list of ideas") is False
