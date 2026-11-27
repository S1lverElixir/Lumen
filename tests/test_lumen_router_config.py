"""test_lumen_router_config.py — unit tests for lumen_router_config.py: model configuration
(GEMINI_MODELS, GEMINI_TTS_MODELS), the \"is this a heavy query?\"/\"is fresh
information needed?\"/\"is a web search requested?\" heuristics (_looks_like_heavy_query/_looks_like_freshness_query/
_looks_like_search_request), route construction for one message (_build_route:
the munito and gemini providers, the _munito_active switch, request extras _munito_payload_extras,
automatic effort selection pick_reasoning_effort/agent_max_searches/gemini_thinking_for, and the model
capability map _MUNITO_CAPS),
filters for exhausted quotas and model quarantine (_skip_exhausted/_is_quota_exhausted), and
the ID registry for identity leak detection (_KNOWN_MODEL_IDS_FOR_LEAK_DETECTION).

Removed providers (OpenRouter, Groq chat, Gemma) were taken out of routing on 05.11.2026 along with
their health registry and order lists — the only remaining checks ensure their IDs
are not returned in model selection and do not disappear from leak detection.

Part of the split of test_bot_helpers.py by module — see test_lumen_formatting.py for the general
principle. lumen_router_config.py contains pure data and routing decision functions without
a single call to the Telegram/Gemini/Munito APIs, so it is tested directly here
(import lumen_router_config), without importing bot.py. The logger inside lumen_router_config.py
is intentionally named \"bot\" (see the comment in the module itself) — so caplog.at_level(...,
logger=\"bot\") below continues to work as before, regardless of the fact that this
file does not import bot.py at all (the tests still take quota and quarantine state from bot).

Run:
    pytest test_lumen_router_config.py -v
"""
import pytest

import lumen_router_config
from datetime import date



# ─────────────────── automatic model selection (router) ───────────────────
# /model and /provider were removed entirely, along with tests for _gemini_display_fields/_or_display_fields/.
# _should_reveal_real_model_names/_check_public_model_names_configured were removed along with them
# (see README/change history). Below are tests for the router that replaced them.

def test_looks_like_heavy_query_detects_code_and_analysis_requests():
    assert lumen_router_config._looks_like_heavy_query("напиши функцию на питоне для сортировки списка") is True
    assert lumen_router_config._looks_like_heavy_query("```\nprint(1)\n```") is True
    assert lumen_router_config._looks_like_heavy_query("проанализируй этот текст подробно") is True
    assert lumen_router_config._looks_like_heavy_query("а" * 700) is True
    assert lumen_router_config._looks_like_heavy_query("1? 2? 3?") is True


def test_looks_like_heavy_query_false_for_simple_messages():
    assert lumen_router_config._looks_like_heavy_query("привет") is False
    assert lumen_router_config._looks_like_heavy_query("сколько будет 2+2") is False
    assert lumen_router_config._looks_like_heavy_query("") is False


def test_looks_like_heavy_query_detects_tests_bots_and_debug_requests():
    # Heuristic expansion (September 2026): unit tests, bots/sites, code parsing
    # and bug searches are also heavy tasks; previously they went to the light
    # chain.
    assert lumen_router_config._looks_like_heavy_query("напиши тест для функции сортировки") is True
    assert lumen_router_config._looks_like_heavy_query("напиши бота для телеграмма") is True
    assert lumen_router_config._looks_like_heavy_query("разбери этот код построчно") is True
    assert lumen_router_config._looks_like_heavy_query("найди баг в скрипте") is True
    assert lumen_router_config._looks_like_heavy_query("объясни код выше") is True
    assert lumen_router_config._looks_like_heavy_query("напиши эссе про осень") is True
    assert lumen_router_config._looks_like_heavy_query("какая погода") is False
    assert lumen_router_config._looks_like_heavy_query("расскажи анекдот") is False


def test_looks_like_freshness_query_detects_current_info_needs():
    assert lumen_router_config._looks_like_freshness_query("кто сейчас президент Франции") is True
    assert lumen_router_config._looks_like_freshness_query("какая сегодня погода в Москве") is True
    assert lumen_router_config._looks_like_freshness_query("сколько стоит биткоин") is True
    assert lumen_router_config._looks_like_freshness_query("последние новости про ИИ") is True


def test_looks_like_freshness_query_false_for_timeless_questions():
    assert lumen_router_config._looks_like_freshness_query("столица Франции") is False
    assert lumen_router_config._looks_like_freshness_query("объясни теорию относительности") is False


def test_looks_like_freshness_query_not_triggered_by_lookalikes():
    # Week's corpus (15.11.2026): "pogod-" without an inflection limit matched the imperative
    # "wait", "last-" without a word boundary — "penultimate". Ordinary chatter
    # received search and the "offline" mark.
    assert lumen_router_config._looks_like_freshness_query("Погоди") is False
    assert lumen_router_config._looks_like_freshness_query("ну хз мб только предпоследняя") is False
    # "pohodka" is a friendly form of address, not weather; Ukrainian "pohodi" contains
    # "i" (U+0456) and was left alone.
    assert lumen_router_config._looks_like_freshness_query("мой погодка сказал") is False
    assert lumen_router_config._looks_like_freshness_query("якій погоді") is True
    # Do not lose real weather/latest data.
    assert lumen_router_config._looks_like_freshness_query("какая погода в Минске") is True
    assert lumen_router_config._looks_like_freshness_query("последняя версия Blender") is True


def test_search_planner_skipped_zones_chatter_out_of_agent_turn():
    # Zone 2: the loop runs only for substantive messages. Cases come from
    # the real week's corpus: substantive messages pass, chatter is filtered out.
    assert lumen_router_config.search_planner_skipped("скок стоит терминал старлинк") is False
    assert lumen_router_config.search_planner_skipped("чё за кросс игра") is False
    assert lumen_router_config.search_planner_skipped("расскажи про амурскую калугу") is False
    assert lumen_router_config.search_planner_skipped("спасибо") is True
    assert lumen_router_config.search_planner_skipped("ахахахахахаха") is True
    assert lumen_router_config.search_planner_skipped("ок") is True
    assert lumen_router_config.search_planner_skipped("привет") is True
    assert lumen_router_config.search_planner_skipped("/draw кот") is True
    assert lumen_router_config.search_planner_skipped("") is True
    assert lumen_router_config.search_planner_skipped("😀🔥👍") is True
    # "hi, how are you?" with a suffix is not a full match: let the cycle model decide.
    assert lumen_router_config.search_planner_skipped("привет, как дела?") is False


def test_looks_like_freshness_query_detects_schedule_and_forecast():
    # Heuristic expansion (September 2026): a schedule/event listing/forecast without the word
    # "weather" previously did not count as a need for fresh information.
    assert lumen_router_config._looks_like_freshness_query("расписание электричек") is True
    assert lumen_router_config._looks_like_freshness_query("дай прогноз на выходные") is True
    assert lumen_router_config._looks_like_freshness_query("афиша кино на неделю") is True
    assert lumen_router_config._looks_like_freshness_query("любимый цвет") is False


def test_looks_like_freshness_query_detects_buying_advice():
    # Prod case 17.09.2026: "which percent is best for an RTX 5060" went
    # light route to a weak model, which then answered with stale knowledge
    # (RTX 5060 "not yet announced") instead of searching. Buying advice always
    # about prices, availability, or new releases need live search.
    assert lumen_router_config._looks_like_freshness_query("какой проц лучше всего брать под ртх 5060") is True
    assert lumen_router_config._looks_like_freshness_query("что лучше купить для игр") is True
    assert lumen_router_config._looks_like_freshness_query("какой телефон лучше взять") is True


def test_looks_like_freshness_query_detects_date_dependent_astronomy():
    # Eval 22.10.2026: “what time is sunrise in Moscow” was not considered fresh — it went
    # plain text in Gemma without search and answered from memory (Q6 failed).
    assert lumen_router_config._looks_like_freshness_query("во сколько рассвет в Москве") is True
    assert lumen_router_config._looks_like_freshness_query("когда закат солнца") is True
    assert lumen_router_config._looks_like_freshness_query("восход солнца в Сочи") is True
    assert lumen_router_config._looks_like_freshness_query("фаза луны") is True
    assert lumen_router_config._looks_like_freshness_query("лунное затмение") is True
    assert lumen_router_config._looks_like_freshness_query("люблю ночное небо") is False


def test_looks_like_heavy_query_detects_bare_compare():
    # Production case 17.09.2026: "compare competitors" without "and/with" between the objects
    # slipped past the heavy branch; the comparison was sent to the weak model.
    assert lumen_router_config._looks_like_heavy_query("сравни конкурентов") is True
    assert lumen_router_config._looks_like_heavy_query("сравни iphone и samsung") is True


def test_looks_like_freshness_query_detects_english():
    # External audit: freshness detection was Russian-only even with DEFAULT_LANG=en —
    # "latest news" went to weak models without a search.
    assert lumen_router_config._looks_like_freshness_query("What is the latest news about NVIDIA?") is True
    assert lumen_router_config._looks_like_freshness_query("weather in Berlin today") is True
    assert lumen_router_config._looks_like_freshness_query("how much does it cost") is True
    assert lumen_router_config._looks_like_freshness_query("just chatting about life") is False


def test_looks_like_freshness_query_ignores_substrings():
    # Branch review: bare now matched know/snow, causing frequent false positives in ordinary conversation.
    assert lumen_router_config._looks_like_freshness_query("I know kung fu") is False
    assert lumen_router_config._looks_like_freshness_query("snow here") is False
    assert lumen_router_config._looks_like_freshness_query("groundbreaking discovery") is False


def test_looks_like_freshness_query_detects_multilingual():
    # Audit 28.10.2026: the bot speaks 25 languages, but freshness detection was RU/EN —
    # other languages went out as plain text without a date or web search.
    assert lumen_router_config._looks_like_freshness_query("¿Cuánto cuesta el iPhone hoy?") is True
    assert lumen_router_config._looks_like_freshness_query("Wie ist das Wetter heute?") is True
    assert lumen_router_config._looks_like_freshness_query("Quelles sont les actualités aujourd'hui ?") is True
    assert lumen_router_config._looks_like_freshness_query("Qual è il prezzo oggi?") is True
    assert lumen_router_config._looks_like_freshness_query("Qual é a previsão para amanhã?") is True
    assert lumen_router_config._looks_like_freshness_query("Wat is het nieuws vandaag?") is True
    assert lumen_router_config._looks_like_freshness_query("Jaka jest pogoda dziś?") is True
    assert lumen_router_config._looks_like_freshness_query("Bugün hava nasıl?") is True
    assert lumen_router_config._looks_like_freshness_query("ما هو سعر الدولار اليوم؟") is True
    assert lumen_router_config._looks_like_freshness_query("आज समाचार क्या हैं?") is True
    assert lumen_router_config._looks_like_freshness_query("Berapa harga emas hari ini?") is True
    assert lumen_router_config._looks_like_freshness_query("Giá vàng hôm nay bao nhiêu?") is True
    assert lumen_router_config._looks_like_freshness_query("วันนี้ราคาทองเท่าไหร่") is True
    assert lumen_router_config._looks_like_freshness_query("قیمت دلار امروز چقدر است؟") is True
    assert lumen_router_config._looks_like_freshness_query("آج موسم کیسا ہے؟") is True
    assert lumen_router_config._looks_like_freshness_query("Bugun ob-havo qanday?") is True
    assert lumen_router_config._looks_like_freshness_query("আজ আবহাওয়া কেমন?") is True
    assert lumen_router_config._looks_like_freshness_query("Ano ang balita ngayon?") is True
    assert lumen_router_config._looks_like_freshness_query("Yini izindaba namuhla?") is True
    assert lumen_router_config._looks_like_freshness_query("Які навіны сёння?") is True
    assert lumen_router_config._looks_like_freshness_query("Яка погода сьогодні?") is True
    assert lumen_router_config._looks_like_freshness_query("Бүгін ауа райы қандай?") is True


def test_looks_like_freshness_query_multilingual_ignores_common_words():
    # Same audit: the words "time/again/end/morning" appear in almost every everyday
    # the phrase — they are absent from detection, otherwise chatter would trigger a search.
    assert lumen_router_config._looks_like_freshness_query("me gusta el tiempo libre") is False
    assert lumen_router_config._looks_like_freshness_query("no meu tempo livre") is False
    assert lumen_router_config._looks_like_freshness_query("Ik kom weer terug") is False
    assert lumen_router_config._looks_like_freshness_query("son derece güzel") is False
    assert lumen_router_config._looks_like_freshness_query("Sa panahong ito") is False
    assert lumen_router_config._looks_like_freshness_query("Guten Morgen") is False
    # Review 28.10.2026: without boundaries, "tomorrow" matched "breakfast", while "हाल/कल"
    # fired inside हालांकि/कल्पना (matras are not included in \w).
    assert lumen_router_config._looks_like_freshness_query("что приготовить на завтрак") is False
    assert lumen_router_config._looks_like_freshness_query("позавтракать вместе") is False
    assert lumen_router_config._looks_like_freshness_query("the actual reason is simple") is False
    assert lumen_router_config._looks_like_freshness_query("हालांकि यह ठीक है") is False
    assert lumen_router_config._looks_like_freshness_query("कल्पना कीजिए") is False
    assert lumen_router_config._looks_like_freshness_query("আজাদি") is False
    assert lumen_router_config._looks_like_freshness_query("что будет завтра") is True


def test_looks_like_search_request_detects_explicit_tool_asks():
    # Prod 11.10.2026: "search the web" without freshness words went to models
    # without a search — an explicit request to search now also goes to the fresh route.
    assert lumen_router_config._looks_like_search_request("погугли что с ДЦ Яндекса") is True
    assert lumen_router_config._looks_like_search_request("найди инфу с помощью веб-поиска") is True
    assert lumen_router_config._looks_like_search_request("ищи через поиск, что там с дата-центрами") is True
    assert lumen_router_config._looks_like_search_request("search the web for Yandex data centers") is True
    assert lumen_router_config._looks_like_search_request("поищи в интернете свежие новости") is True


def test_looks_like_search_request_ignores_code_searches():
    # Bare "find/search" without mentioning search — refers to code, not the web: otherwise
    # "find the bug" would send heavy code analysis to the search route.
    assert lumen_router_config._looks_like_search_request("найди баг в скрипте") is False
    assert lumen_router_config._looks_like_search_request("найди ошибку") is False
    assert lumen_router_config._looks_like_search_request("привет") is False
    assert lumen_router_config._looks_like_search_request("") is False


def test_looks_like_heavy_query_detects_english():
    # External audit: "write a Python parser" slipped into the light branch.
    assert lumen_router_config._looks_like_heavy_query("write a Python parser for csv") is True
    assert lumen_router_config._looks_like_heavy_query("fix this bug, it crashes on empty input") is True
    assert lumen_router_config._looks_like_heavy_query("compare iphone and samsung") is True
    assert lumen_router_config._looks_like_heavy_query("just saying hello") is False


def test_looks_like_heavy_query_ignores_substrings_and_handles_plurals():
    # Branch review: prove caught improve, and solve caught resolve; "write two scripts" slipped through.
    assert lumen_router_config._looks_like_heavy_query("please improve my text") is False
    assert lumen_router_config._looks_like_heavy_query("lets resolve this conflict") is False
    assert lumen_router_config._looks_like_heavy_query("write two scripts for backup") is True


# ─────────── route composition: Munito as the head, Gemini chains as reserve ───────────
# OpenRouter, Groq chat, and Gemma were removed from the route on 05.11.2026: text and images
# serves Munito; Gemini is the automatic fallback; links, YouTube, and video/audio
# remain on Gemini, because Munito cannot physically accept them.

# Fixed contract (R7-10): expectations are not derived from the mutable state under test
# Cooldown behavior is asserted explicitly for model IDs, not for
#     provider-level rate limits,
# in production would pass green (review 17.11.2026).
_MUNITO_ORDER_FIXED = [
    "deepseek-ai/deepseek-v4.1-flash-1017",
    "zai-org/glm-5.3-flash",
    "deepseek-ai/deepseek-v4-flash",
]
_GEMINI_HEAVY_FIXED = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
]
_GEMINI_SEARCH_FIXED = [
    "gemini-2.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]


def _munito_mode(monkeypatch, *, disabled=False, key="fake-munito-key-123"):
    """The switch is router data (read at import), the key is bot.py: both places must be patched."""
    import bot
    monkeypatch.setattr(lumen_router_config, "_MUNITO_DISABLED", disabled)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", key)


def _route_for(**kwargs):
    return lumen_router_config._build_route(
        needs_youtube=kwargs.get("needs_youtube", False),
        needs_website=kwargs.get("needs_website", False),
        media_mime=kwargs.get("media_mime"),
        is_heavy=kwargs.get("is_heavy", False),
        needs_freshness=kwargs.get("needs_freshness", False),
    )


@pytest.mark.parametrize("kwargs, reserve", [
    ({"is_heavy": False, "needs_freshness": False}, _GEMINI_HEAVY_FIXED),
    ({"is_heavy": True, "needs_freshness": False}, _GEMINI_HEAVY_FIXED),
    ({"is_heavy": False, "needs_freshness": True}, _GEMINI_SEARCH_FIXED),
    ({"is_heavy": True, "needs_freshness": True}, _GEMINI_SEARCH_FIXED),
])
def test_build_route_text_goes_to_munito_with_gemini_reserve(monkeypatch, kwargs, reserve):
    # Fallback is now unconditional (MUNITO_KEEP_FALLBACKS removed): the free tier
    # Munito returns 503 "no free capacity", and without the Gemini chain this means the bot goes silent.
    # Regular and heavy text share one heavy chain — there was no light chain.
    _munito_mode(monkeypatch)
    route = _route_for(**kwargs)
    assert route == ([("munito", m) for m in _MUNITO_ORDER_FIXED]
                     + [("gemini", m) for m in reserve])


@pytest.mark.parametrize("needs_freshness", [False, True])
def test_build_route_image_goes_to_munito_unless_it_needs_search(monkeypatch, needs_freshness):
    # Munito vision heads were verified by a live call on 05.11.2026, but live search
    # no — "show the image and find which news story it is" goes entirely to Gemini.
    _munito_mode(monkeypatch)
    route = _route_for(media_mime="image/png", needs_freshness=needs_freshness)
    if needs_freshness:
        assert route == [("gemini", m) for m in _GEMINI_SEARCH_FIXED]
    else:
        assert route == ([("munito", m) for m in _MUNITO_ORDER_FIXED]
                         + [("gemini", m) for m in _GEMINI_HEAVY_FIXED])


@pytest.mark.parametrize("kwargs, chain", [
    ({"media_mime": "video/mp4"}, _GEMINI_HEAVY_FIXED),
    ({"media_mime": "audio/ogg"}, _GEMINI_HEAVY_FIXED),
    ({"media_mime": "video/mp4", "needs_freshness": True}, _GEMINI_SEARCH_FIXED),
    ({"needs_website": True}, _GEMINI_HEAVY_FIXED),
    ({"needs_youtube": True}, _GEMINI_HEAVY_FIXED),
    ({"needs_website": True, "needs_freshness": True}, _GEMINI_SEARCH_FIXED),
    ({"needs_youtube": True, "needs_freshness": True}, _GEMINI_SEARCH_FIXED),
])
def test_build_route_gemini_only_shapes(monkeypatch, kwargs, chain):
    # Munito accepts only text and images: links and video cannot be escalated,
    # therefore they are Gemini-only even when Munito is active. LINK cases are pinned by the same
    # heavy/search lists: GEMINI_LINK_CHAIN is a copy of heavy (827-828), and the live
    # divergence between copies will turn red here instead of going unnoticed.
    _munito_mode(monkeypatch)
    route = _route_for(**kwargs)
    assert route == [("gemini", m) for m in chain]


@pytest.mark.parametrize("disabled, key", [
    (True, "fake-munito-key-123"),
    (False, ""),
])
def test_build_route_without_active_munito_is_gemini_only(monkeypatch, disabled, key):
    # Two independent ways to disable a provider: a flag and removing the key from Space.
    # The route must fall back entirely to Gemini, rather than remain without a Munito head.
    _munito_mode(monkeypatch, disabled=disabled, key=key)
    route = _route_for()
    assert route and all(p == "gemini" for p, _ in route)
    assert route[0] == ("gemini", "gemini-3.8-flash")
    assert all(p == "gemini" for p, _ in _route_for(media_mime="image/jpeg"))


@pytest.mark.parametrize("disabled, key, expected", [
    (False, "fake-munito-key-123", True),
    (True, "fake-munito-key-123", False),
    (False, "", False),
    (True, "", False),
])
def test_munito_active_requires_key_and_enabled_flag(monkeypatch, disabled, key, expected):
    # An off switch, not an on switch: if _munito_active stops consulting the flag,
    # MUNITO_ENABLED=0 will not return traffic from Munito (503 on every message), and if
    # stops checking the key, routing will go to a provider that is absent from the Space.
    _munito_mode(monkeypatch, disabled=disabled, key=key)
    assert lumen_router_config._munito_active() is expected
    assert bool(lumen_router_config._munito_route()) is expected


@pytest.mark.parametrize("kwargs", [
    {},
    {"is_heavy": True},
    {"needs_freshness": True},
    {"media_mime": "image/jpeg"},
    {"media_mime": "video/mp4"},
    {"needs_website": True},
    {"needs_youtube": True},
])
def test_build_route_never_returns_retired_providers(monkeypatch, kwargs):
    # OpenRouter keys and Groq chat are no longer in production; Gemma was removed from
    # GEMINI_MODELS: a returned ID would cause 401/404 at the very end of the route,
    # when live attempts have already been spent.
    _munito_mode(monkeypatch)
    route = _route_for(**kwargs)
    assert route
    assert {p for p, _ in route} <= {"munito", "gemini"}
    live = set(lumen_router_config.GEMINI_MODELS) | set(lumen_router_config._MUNITO_ORDER)
    for _, model_id in route:
        assert model_id in live, f"{model_id} в цепочке, но не в конфиге моделей"
        assert not model_id.endswith(":free")


@pytest.mark.parametrize(("value", "expected"), [
    (None, {"reasoning_effort": "low"}),
    ("", {}),
    ("minimal", {"reasoning_effort": "minimal"}),
])
def test_munito_payload_extras_default_low_and_absent_when_unset(monkeypatch, value, expected):
    # protected behavior: by default, requests go out with minimal reasoning
    # (measured 05.11.2026: ~0.2s instead of ~1s), and the field is NOT sent when the variable
    # is empty — Munito returns 400 for an unknown parameter, so silence the experiment
    # must be possible without a code change.
    if value is None:
        monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("MUNITO_REASONING_EFFORT", value)
    assert lumen_router_config._munito_payload_extras() == expected


# new Gemini models (3.6 Flash / 3.5 Flash-Lite)

def test_new_gemini_models_present_and_prioritized():
    # Dashboard 17.11.2026: 3.7 and 3.5 Flash were removed (0/0); 3.8/3.6 Flash are alive,
    # both Flash Lite and 3 Flash.
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.6-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.5-flash-lite" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.7-flash" not in lumen_router_config.GEMINI_MODELS
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-3.8-flash"
    # The search head is the only live one with a search grounding quota (bucket 2.5).
    assert lumen_router_config.GEMINI_SEARCH_CHAIN[0] == "gemini-2.5-flash-lite"


def test_gemini_3_flash_preview_restored_after_official_docs_confirmation():
    # HISTORY: removed 17.08.2026 (an erroneous conclusion — the sources were Vertex AI
    # Enterprise Agent Platform and a third-party GitHub Copilot changelog; neither
    # the official model page of the Gemini API itself). RESTORED 22.08.2026: the owner
    # sent a screenshot of ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview—
    # "Last updated 2026-08-18 UTC", a live capabilities table, Model code
    # gemini-3-flash-preview. The primary source outweighs the previous (incorrect) conclusion.
    # 08.10.2026: the entire 3.x lineup was removed from the chains again — this time production
    # 05.10.2026 (HTTP 503 across five models at once). The configuration in GEMINI_MODELS is retained:
    # return — with a single chain edit, if the dashboard shows them alive.
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_HEAVY_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_SEARCH_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_LINK_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_LINK_SEARCH_CHAIN
    # 08.10.2026 (reassessment after a dashboard screenshot): the model is alive and has
    # limits of 5 RPM / 250K TPM / 20 RPD, and Google's official model list
    # (updated 06.10.2026) confirms the gemini-3-flash-preview model. Production 503s
    # were a temporary failure, so undoing the removal means returning them to the
    # chains, while moving
    # provides cooldown for "unavailable" (_mark_model_unavailable).
    chain = lumen_router_config.GEMINI_HEAVY_CHAIN
    assert chain.index("gemini-3.6-flash") < chain.index("gemini-3-flash-preview")
    # 05.11.2026: Gemma was removed from GEMINI_MODELS, so the chain tail is now
    # not on it, but on the last live 3.x model.
    assert chain[-1] == "gemini-3-flash-preview"


def test_gemini_3_flash_preview_quota_config_matches_dashboard():
    # The model page says "Search grounding: Supported"/"Grounding with Google
    # Maps: Supported” — the model technically can. But the actual free QUOTA for
    # both (as encoded by these two flags) from the AI Studio dashboard — 0/0, the same pattern,
    # as with the rest of the 3.x lineup (see the note about buckets at the start of GEMINI_MODELS).
    conf = lumen_router_config.GEMINI_MODELS["gemini-3-flash-preview"]
    assert conf.get("search_grounding") is False
    assert conf.get("map_grounding") is False
    assert conf.get("url_context") is True
    assert conf.get("stream") is True


def test_gemini_3_7_flash_removed_after_dashboard_retirement():
    # Dashboard 17.11.2026: 3.7 Flash is not in the list (0/0) — removed from the config and chains.
    assert "gemini-3.7-flash" not in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.7-flash" not in lumen_router_config.GEMINI_HEAVY_CHAIN
    assert "gemini-3.7-flash" not in lumen_router_config.GEMINI_SEARCH_CHAIN


def test_check_unconfirmed_model_quotas_no_warnings_once_all_models_confirmed(caplog):
    # REGRESSION (24.07.2026): gemini-3.6-flash and gemini-3.5-flash-lite were
    # confirmed against the real AI Studio dashboard (see the comments in
    # GEMINI_MODELS
    # in bot.py), quota_unconfirmed was cleared for both. Previously, this test checked that
    # these two models STILL emit the warning (see git history); now,
    # when both are confirmed, no model should produce any warnings at all.
    # If this test starts failing, either quota_unconfirmed was restored by mistake,
    # or added a new unconfirmed model (in which case the test must be updated for
    # a new model, not simply "fixing" it).
    import logging
    with caplog.at_level(logging.WARNING, logger="bot"):
        lumen_router_config._check_unconfirmed_model_quotas()
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == []


# ─────────── retired IDs remain in the identity-leak detector ───────────
# The OpenRouter health registry (_OR_MODEL_HEALTH), its order lists and the Gemma chain
# were removed on 05.11.2026 with the providers themselves. One requirement remains: an exact
# removed-model ID must not appear in an answer. The model may call itself by its old slug,
# and detection catches it only when the ID is in _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION.
# Removal dates for each ID are in git history (404/403, paid cutoff, corrupted calibration text).

_RETIRED_MODEL_IDS = (
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "qwen/qwen3-coder:free",
    "tencent/hy3:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "inclusionai/ling-3.0-flash:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "z-ai/glm-5.2:free",
    "dots-studio/dots-3-note-preview:free",
    "qwen/qwen3.8-27b:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
)


@pytest.mark.parametrize("model_id", _RETIRED_MODEL_IDS)
def test_retired_model_ids_stay_in_leak_detection_registry(model_id):
    # Fails if someone "cleaned up" the registry to match the provider's current catalog:
    # a removed model stops being selected, but its ID may still arrive in text
    # the answer, and without a line in the registry the scrub will not see it.
    assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert model_id not in lumen_router_config._MUNITO_ORDER


def test_live_munito_model_ids_are_known_to_leak_detection():
    # Munito has answered instead of OpenRouter since November 2026: without its ID in the registry, the bot
    # would name the host and model in its very first answer about its device.
    for model_id in lumen_router_config._MUNITO_ORDER:
        assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


# ─────────────────── TEXT_MODEL_ORDER renamed, alias preserved ───────────────────

def test_text_model_order_alias_still_works():
    assert lumen_router_config.TEXT_MODEL_ORDER is lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert "nvidia/nemotron-3-super-120b-a12b:free" in lumen_router_config.TEXT_MODEL_ORDER


# ─────────────────── GEMINI_TTS_MODELS centralized ───────────────────

def test_gemini_tts_models_centralized_in_router_config():
    assert lumen_router_config.GEMINI_TTS_MODELS == ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]


# Gemini 3.x model lineup in chains
# Retired OpenRouter slugs from the 17.09.2026 audit moved to _RETIRED_MODEL_IDS above:
# Cooldown also covers a model that is absent from Gemini.


def test_gemini_3_8_flash_heads_heavy_chain_and_default():
    # 08.10.2026 (rechecked after an AI Studio dashboard screenshot): the 3.x
    # lineup is alive,
    # limits (5 RPM / 250K TPM / 20 RPD); the official list of model
    # Google (updated 06.10.2026) confirms the codes. Production 503s on 05.10.2026 were temporary
    # refusal, so 3.8 remains the second fallback after 2.5-flash in the chains
    # (the only one that actually answered in prod and has a search quota).
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_MODELS
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-3.8-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[1] == "gemini-3.6-flash"
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_SEARCH_CHAIN
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_LINK_CHAIN
    for alive in ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite"):
        assert alive in lumen_router_config.GEMINI_HEAVY_CHAIN
        assert alive in lumen_router_config.GEMINI_SEARCH_CHAIN
    conf = lumen_router_config.GEMINI_MODELS["gemini-3.8-flash"]
    assert conf.get("search_grounding") is False
    assert conf.get("map_grounding") is False
    assert conf.get("url_context") is True
    assert not conf.get("quota_unconfirmed")


def test_registry_and_default_chain_have_no_silent_aliases():
    # Audit 26.09.2026: GEMINI_DEFAULT_CHAIN was an ALIAS of GEMINI_HEAVY_CHAIN (the same
    # a list object), and “openrouter/free” was duplicated in the registry.
    assert lumen_router_config.GEMINI_DEFAULT_CHAIN is not lumen_router_config.GEMINI_HEAVY_CHAIN
    assert lumen_router_config.GEMINI_DEFAULT_CHAIN == lumen_router_config.GEMINI_HEAVY_CHAIN
    registry = lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert len(registry) == len(set(registry)), "в реестре моделей есть дубли"


# ─────────────── an exhausted quota no longer consumes an attempt (audit 26.09.2026) ───────────────

def test_munito_route_skips_models_marked_exhausted(monkeypatch):
    # A 429 mark is attached to provider "munito"; without the filter, every message
    # would pay for an attempt on a model whose quota ran out yesterday.
    import bot
    _munito_mode(monkeypatch)
    monkeypatch.setattr(lumen_router_config, "_MUNITO_ORDER", ["probe/exhausted", "probe/alive"])
    bot.GLOBAL_QUOTA.setdefault("munito", {})["probe/exhausted"] = {"used": 0, "exhausted_at": 123.0}
    assert [m for _, m in lumen_router_config._munito_route()] == ["probe/alive"]


def test_gemini_route_skips_exhausted_and_cooling_models():
    # A short cooldown for a minute-level 429 (cooldown_until) counts the same as a daily
    # label: otherwise the route spends an attempt on a model that will refuse again.
    import bot
    bot.GLOBAL_QUOTA.setdefault("gemini", {})["gemini-probe-dead"] = {"used": 0, "exhausted_at": 123.0}
    bot.GLOBAL_QUOTA.setdefault("gemini", {})["gemini-probe-cooling"] = {"used": 0, "cooldown_until": float("inf")}
    route = lumen_router_config._gemini_route(["gemini-probe-dead", "gemini-probe-cooling", "gemini-3.8-flash"])
    assert [m for _, m in route] == ["gemini-3.8-flash"]


def test_route_never_becomes_empty_when_every_model_is_exhausted(monkeypatch):
    # An empty route means an immediate refusal instead of a clear limit message, so
    # one candidate per provider remains, and this is also the ceiling for extra spending.
    import bot
    _munito_mode(monkeypatch)
    for mid in lumen_router_config._MUNITO_ORDER:
        bot.GLOBAL_QUOTA.setdefault("munito", {})[mid] = {"used": 0, "exhausted_at": 123.0}
    for mid in lumen_router_config.GEMINI_HEAVY_CHAIN:
        bot.GLOBAL_QUOTA.setdefault("gemini", {})[mid] = {"used": 0, "exhausted_at": 123.0}
    route = _route_for()
    assert route == [("munito", lumen_router_config._MUNITO_ORDER[0]),
                     ("gemini", lumen_router_config.GEMINI_HEAVY_CHAIN[0])]


def test_skip_exhausted_returns_single_first_allowed_variant():
    # If all are dead, use one first option, not the whole chain; otherwise every
    # message
    # paid with an HTTP call for every model in the chain when quota was exhausted.
    import bot
    dead = ["probe/dead1", "probe/dead2", "probe/dead3"]
    for mid in dead:
        bot.GLOBAL_QUOTA.setdefault("munito", {})[mid] = {"used": 0, "exhausted_at": 123.0}
    assert lumen_router_config._skip_exhausted("munito", dead) == ["probe/dead1"]
    assert lumen_router_config._skip_exhausted("munito", []) == []


def test_successful_usage_clears_exhausted_mark(monkeypatch):
    # A successful response clears the marker; otherwise, after a daily reset or repair
    # provider, the model would be removed from routing forever.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    bot.GLOBAL_QUOTA.setdefault("munito", {})[head] = {"used": 3, "exhausted_at": 123.0}
    bot._record_quota_usage("munito", head)
    assert lumen_router_config._munito_route()[0] == ("munito", head)


def test_all_gemini_models_declare_url_context_explicitly():
    # Audit A2-11: a True default in conf.get silently enabled the tool — now the key
    # specified explicitly for each model (same behavior, visible intent).
    missing = [m for m, c in lumen_router_config.GEMINI_MODELS.items() if "url_context" not in c]
    assert missing == []


def test_freshness_year_window_tracks_current_year():
    # Audit A2-13: the window is built from the current date. Verify by behavior, not
    # by grepping .pattern: a mutant with a hardcoded year passed the grep (review 17.11.2026).
    year = date.today().year
    assert lumen_router_config._looks_like_freshness_query(f"события {year + 1}") is True
    assert lumen_router_config._looks_like_freshness_query(f"события {year + 4}") is False


def test_quota_exhausted_logs_broken_state_loudly(monkeypatch, caplog):
    # A2-10 audit: corrupt quota state previously caused a silent fail-open.
    import bot

    class _BrokenQuota(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(bot, "GLOBAL_QUOTA", _BrokenQuota())
    with caplog.at_level("WARNING", logger="bot"):
        assert lumen_router_config._is_quota_exhausted("munito", "m") is False
    assert "Quota state unreadable" in caplog.text


def test_quarantine_triggers_after_three_consecutive_bad_and_skips_model(caplog, monkeypatch):
    # Three bad results in a row mean quarantine until the end of the day: the router avoids the model.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    with caplog.at_level("WARNING", logger="bot"):
        for _ in range(3):
            bot._record_model_outcome("munito", head, bad=True)
    assert bot._is_quarantined("munito", head) is True
    assert "[quarantine]" in caplog.text
    assert bot._quarantine_status() == [("munito", head, 3)]
    assert [m for _, m in lumen_router_config._munito_route()] == lumen_router_config._MUNITO_ORDER[1:]


def test_quarantine_needs_bad_answers_from_more_than_one_chat(monkeypatch):
    # Audit run-2: the quarantine counter did not account for the chat, and three empty answers from one
    # chat disabled the primary model for everyone else until the end of the quota day.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    lumen_router_config._QUARANTINE.clear()
    tokens = []
    try:
        for _ in range(4):
            tokens.append(bot._LOG_CHAT.set("chat=1"))
            bot._record_model_outcome("munito", head, bad=True)
        assert bot._is_quarantined("munito", head) is False
        for chat in ("chat=2", "chat=3"):
            tokens.append(bot._LOG_CHAT.set(chat))
            bot._record_model_outcome("munito", head, bad=True)
        assert bot._is_quarantined("munito", head) is True
    finally:
        for token in reversed(tokens):
            bot._LOG_CHAT.reset(token)
        lumen_router_config._QUARANTINE.clear()


def test_quarantine_counts_one_person_across_their_chats(monkeypatch):
    # Audit 16.11.2026: quarantine contributions are counted per person, not per chat. Two chats of one
    # earlier gave a person two contributions and together switched the model off for everyone else.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    lumen_router_config._QUARANTINE.clear()
    tokens = []
    try:
        for chat in ("chat=1", "chat=2"):
            tokens.append((bot._LOG_CHAT, bot._LOG_CHAT.set(chat)))
            tokens.append((bot._LOG_USER, bot._LOG_USER.set("user=7")))
            bot._record_model_outcome("munito", head, bad=True)
            bot._record_model_outcome("munito", head, bad=True)
        assert bot._is_quarantined("munito", head) is False
        # Another person in the same chat contributes a third time — the threshold is reached.
        tokens.append((bot._LOG_USER, bot._LOG_USER.set("user=8")))
        bot._record_model_outcome("munito", head, bad=True)
        assert bot._is_quarantined("munito", head) is True
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
        lumen_router_config._QUARANTINE.clear()


def test_quarantine_never_empties_route_and_success_resets(caplog):
    # The last available model is not quarantined de facto: do not return an empty route.
    import bot
    for _ in range(5):
        bot._record_model_outcome("munito", "only-model", bad=True)
    assert bot._is_quarantined("munito", "only-model") is True
    assert lumen_router_config._skip_exhausted("munito", ["only-model"]) == ["only-model"]
    bot._record_model_outcome("munito", "only-model", bad=False)
    assert bot._is_quarantined("munito", "only-model") is False
    assert bot._quarantine_status() == []


def test_quarantine_resets_on_quota_day_change(monkeypatch):
    # Quarantine lasts only until the end of the quota day: a new day means a clean counter.
    import bot
    monkeypatch.setattr(bot, "_current_quota_day", lambda: "2099-01-01")
    for _ in range(3):
        bot._record_model_outcome("munito", "day-model", bad=True)
    assert bot._is_quarantined("munito", "day-model") is True
    monkeypatch.setattr(bot, "_current_quota_day", lambda: "2099-01-02")
    assert bot._is_quarantined("munito", "day-model") is False
    assert bot._quarantine_status() == []


def test_quarantine_threshold_reads_env(monkeypatch):
    # N from env: with 2, quarantine starts earlier (the default of 3 is checked above).
    import bot
    monkeypatch.setenv("MODEL_QUARANTINE_BAD_LIMIT", "2")
    assert lumen_router_config._quarantine_threshold() == 2
    bot._record_model_outcome("munito", "env-model", bad=True)
    assert bot._is_quarantined("munito", "env-model") is False
    bot._record_model_outcome("munito", "env-model", bad=True)
    assert bot._is_quarantined("munito", "env-model") is True


def test_quarantine_window_expires_and_stats_agree_with_routing(monkeypatch):
    # A window instead of a day: after expiry, the model is back in the route, and /stats does not
    # shows it as quarantined (the 08.11.2026 review found a mismatch between status and is_quarantined).
    import time
    import bot
    lumen_router_config._QUARANTINE.clear()
    monkeypatch.setattr(lumen_router_config, "QUARANTINE_WINDOW_SEC", 0.5)
    try:
        for i in (1, 2, 3):
            token = bot._LOG_CHAT.set(f"chat={i}")
            try:
                bot._record_model_outcome("munito", "window-model", bad=True)
            finally:
                bot._LOG_CHAT.reset(token)
        assert bot._is_quarantined("munito", "window-model") is True
        assert bot._quarantine_status() == [("munito", "window-model", 3)]
        time.sleep(0.6)
        assert bot._is_quarantined("munito", "window-model") is False
        assert bot._quarantine_status() == []
    finally:
        lumen_router_config._QUARANTINE.clear()


# ──────────────── automatic effort selection and model capabilities ────────────────

@pytest.mark.parametrize(("text", "kwargs", "expected"), [
    # Regression 12.11.2026: a flat MUNITO_REASONING_EFFORT=low made glm-5.3-flash
    # incorrect arithmetic (64 instead of 60), and on “think properly” the effort is not
    # changed at all. This checks switching based on the meaning of the query.
    ("привет", {}, "low"),
    ("спасибо!", {}, "low"),
    ("сколько будет 27*43", {}, "medium"),
    ("кто такие викинги", {}, "medium"),
    ("напиши функцию парсера", {"is_heavy": True}, "high"),
    ("спроектируй архитектуру и докажи выбор", {}, "xhigh"),
    ("сравни все три варианта по шагам", {}, "xhigh"),
    ("какая сейчас цена билета", {"needs_freshness": True}, "medium"),
    # A request to be brief must not downgrade to minimal: glm made mistakes at that level.
    ("коротко: сколько будет 27*43", {}, "low"),
    # A correction in the message text itself raises the tier: the base here
    # medium (seven words), while 'you are wrong' + 'recheck' trigger an overthink step
    # upward. The former medium label resulted from the “fewer than 40 characters” rule,
    # which dropped the base to low (12.11.2026 grid regression).
    ("реши задачу ещё раз, ты не прав", {}, "high"),
    ("реши задачу", {"corrections": 2}, "medium"),
    ("объясни", {"history_len": 60}, "medium"),
])
def test_pick_reasoning_effort_switches_on_request_shape(text, kwargs, expected):
    assert lumen_router_config.pick_reasoning_effort(text, **kwargs) == expected


@pytest.mark.parametrize(("text", "expected"), [
    # Short small talk stays at low in every language: the decision is based on length, and
    # not by the presence of Russian words (effort_grid.py mapping, 12.11.2026).
    ("สวัสดี", "low"),
    ("gracias amigo", "low"),
    ("تم بخير", "low"),
    # A question without a question mark in any writing system must think longer:
    # previously, "why is the sky blue" and "wat is de hoofdstad" were sent to low.
    ("তুমি কে", "medium"),
    ("आप कौन हैं", "medium"),
    ("คุณเป็นใคร", "medium"),
    ("تو کی هستی", "medium"),
    ("تم کون ہو", "medium"),
    ("deux fois deux font combien", "medium"),
    ("quantos dias você tem", "medium"),
    # An inverted Spanish question mark also marks a question: "¿Que hora es"
    # without
    # the accent in "que" otherwise remained on low (check 12.11.2026).
    ("¿Que hora es", "medium"),
])
def test_pick_reasoning_effort_question_signal_is_multilingual(text, expected):
    assert lumen_router_config.pick_reasoning_effort(text) == expected


def test_ask_words_split_by_script_not_by_trailing_char():
    # In Devanagari and Bengali, vowels and nasal marks are not letters, so
    # "" breaks INSIDE a word. The old split at the last symbol produced false
    # questions: the boundary for the Hindi word “kis” matched “kisi” (“someone’s”), while the substring for “ke”
    # caught "থেকে" ("from"), and an everyday Hindi or Bengali phrase lost the low
    # (blocker from the adversarial review 12.11.2026). The boundary is now checked by
    # the writing system's range on both sides of the word.
    for word in lumen_router_config._ASK_WORDS_INDIC:
        assert lumen_router_config._is_indic(word) is True
        assert word not in lumen_router_config._ASK_WORDS_SPACED
    for word in ("что", "wat", "kim", "کجا", "أين", "siapa", "kailan"):
        assert lumen_router_config._is_indic(word) is False
        assert word in lumen_router_config._ASK_WORDS_SPACED
    # These exact phrases used to become “questions,” even though they were spoken by a person
    # without any question:
    assert lumen_router_config._ASK_INDIC_RE.search("वो किसी के पास है") is None
    assert lumen_router_config._ASK_INDIC_RE.search("कबूतर उड गया") is None
    assert lumen_router_config._ASK_INDIC_RE.search("াজার থেকে আসছি") is None
    assert lumen_router_config._ASK_INDIC_RE.search("কারণ আমি ক্লান্ত") is None
    # But a real question from the same category must be caught:
    assert lumen_router_config._ASK_INDIC_RE.search("কোথায় আছো") is not None
    assert lumen_router_config._ASK_INDIC_RE.search("तुम कहाँ रहते हो") is not None
    assert lumen_router_config._ASK_WORDS_RE.search("wat is de hoofdstad")
    # Cyrillic "ne" as negation must not be replaced with Kazakh Latin "ne".
    assert lumen_router_config.pick_reasoning_effort("это не я") == "low"


def test_leading_and_horizon_signals_do_not_fire_on_ordinary_words():
    # Three review blockers 12.11.2026: a distant horizon without word boundaries, a request
    # "short" from words that mean something else in other languages, and homonymous conjunctions
    # at the start of the phrase.
    assert lumen_router_config.pick_reasoning_effort("Планирую отпуск на месяц") == "low"
    assert lumen_router_config.pick_reasoning_effort(
        "Планета Венера, месяц за месяцем") == "medium"
    assert lumen_router_config.pick_reasoning_effort(
        "составь план тренировок на четыре недели") == "xhigh"
    assert lumen_router_config.pick_reasoning_effort(
        "how does a rapid transit system work") == "medium"
    assert lumen_router_config.pick_reasoning_effort("was ist ein JWT kurz") == "low"
    for small_talk in ("que bom!", "ne bouge pas", "co-founder thing", "https://var.io",
                       "var i = arr", "när du kommer"):
        assert lumen_router_config.pick_reasoning_effort(small_talk) == "low", small_talk
    # MULTILINE: "ok
    # ¿qué es esto?" is one user utterance, not two lines.
    assert lumen_router_config.pick_reasoning_effort("ok\n¿qué es esto?") == "medium"
    # “huh????” is one question with four question marks, not four questions in a column.
    assert lumen_router_config.pick_reasoning_effort("huh????") != "xhigh"


def test_quarantine_window_env_falls_back_instead_of_crashing(monkeypatch):
    # Garbage in QUARANTINE_WINDOW_SEC must not crash module import, and with it
    # and the whole bot. With int() directly, that is exactly what happened: ValueError at startup
    # (verified 12.11.2026 in a separate process with QUARANTINE_WINDOW_SEC=abc).
    monkeypatch.setenv("QUARANTINE_WINDOW_SEC", "abc")
    assert lumen_router_config._quarantine_window_sec() == 900
    monkeypatch.setenv("QUARANTINE_WINDOW_SEC", "5")
    assert lumen_router_config._quarantine_window_sec() == 60, "пол окна обязан остаться"
    monkeypatch.setenv("QUARANTINE_WINDOW_SEC", "120")
    assert lumen_router_config._quarantine_window_sec() == 120
    monkeypatch.delenv("QUARANTINE_WINDOW_SEC", raising=False)
    assert lumen_router_config._quarantine_window_sec() == 900


def test_pick_reasoning_effort_never_returns_unknown_level():
    # Unknown level = 400 from the provider or silent absence of the field.
    for text in ("", "?", "докажи", "а он точно не в клубе?", "x" * 5000):
        assert lumen_router_config.pick_reasoning_effort(text) in lumen_router_config.EFFORT_LEVELS


def test_bump_effort_clamps_at_both_ends():
    assert lumen_router_config._bump_effort("xhigh", 3) == "xhigh"
    assert lumen_router_config._bump_effort("low", -3) == "minimal"
    assert lumen_router_config._bump_effort("мусор", 1) == "low"


@pytest.mark.parametrize(("model_id", "param", "expected"), [
    # 400 on reasoning_effort for gemma-4-12b and pplx-decider-v1.2—measured 12.11.2026;
    # the test fails if someone puts such a model in the chain without measurement.
    ("deepseek-ai/deepseek-v4.1-flash-1017", "reasoning_effort", True),
    ("zai-org/glm-5.3-flash", "tools", True),
    ("google/gemma-4-12b", "reasoning_effort", False),
    ("perplexity-ai/pplx-decider-v1.2", "reasoning_effort", False),
    # tools were not measured for these two, so it was not removed from them; only what
    # for which there is a confirmed 400.
    ("google/gemma-4-12b", "tools", True),
    ("неизвестная/модель", "reasoning_effort", True),
    (None, "reasoning_effort", True),
])
def test_munito_model_supports_only_measured_capabilities(model_id, param, expected):
    # For an unknown model, the field remains (previous behavior): otherwise a new head
    # the chain silently loses effort. Only what was measured as 400 was removed.
    assert lumen_router_config._munito_model_supports(model_id, param) is expected


def test_every_munito_chain_model_has_capability_record():
    # A model with no entry silently disables effort and tools for it: either measure and
    # add it, or keep it out of the chain (dated-evidence rule).
    for model_id in lumen_router_config._MUNITO_ORDER:
        assert model_id in lumen_router_config._MUNITO_CAPS, model_id
        assert lumen_router_config._MUNITO_CAPS[model_id]["reasoning_effort"] is True
        assert lumen_router_config._MUNITO_CAPS[model_id]["tools"] is True


@pytest.mark.parametrize(("value", "effort", "expected"), [
    (None, None, {"reasoning_effort": "low"}),      # the previous default without arguments
    (None, "high", {"reasoning_effort": "high"}),   # automatic selection, the variable is unset
    ("", "high", {}),                               # empty = disable the field
    ("minimal", "high", {"reasoning_effort": "minimal"}),  # the pin is more important than automatic selection
])
def test_munito_payload_extras_precedence(monkeypatch, value, effort, expected):
    if value is None:
        monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("MUNITO_REASONING_EFFORT", value)
    assert lumen_router_config._munito_payload_extras(effort) == expected


def test_munito_payload_extras_drops_field_for_unsupported_model(monkeypatch):
    monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    assert lumen_router_config._munito_payload_extras("high", "google/gemma-4-12b") == {}
    assert lumen_router_config._munito_payload_extras("high", "zai-org/glm-5.3-flash") == {"reasoning_effort": "high"}


@pytest.mark.parametrize(("effort", "expected"), [
    ("low", 0), ("medium", 2), ("high", 2), ("xhigh", 3), (None, 0),
])
def test_agent_max_searches_follows_effort(monkeypatch, effort, expected):
    monkeypatch.delenv("AGENT_MAX_SEARCHES", raising=False)
    assert lumen_router_config.agent_max_searches(effort) == expected


def test_agent_searches_table_names_searches_not_steps():
    # Review revolver: the limit must be counted in searches. If the table again
    # turn out to be steps, one line with three tool_calls consumes "one search".
    assert lumen_router_config._AGENT_SEARCHES_BY_EFFORT["medium"] == 2
    # Assert behavior, not a grep over the docstring (review 17.11.2026).
    assert lumen_router_config.agent_max_searches("xhigh") == 3


def test_unknown_effort_pin_is_warned_but_sent(monkeypatch, caplog):
    # The provider does not reject the garbage value, so at least it is visible in
    # the log.
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "none")
    with caplog.at_level("WARNING", logger="bot"):
        assert lumen_router_config._munito_payload_extras("high") == {"reasoning_effort": "none"}
    assert "not in the provider scale" in caplog.text


def test_agent_max_searches_env_override(monkeypatch):
    # The owner must be able to disable the search loop with a digit, without editing code.
    monkeypatch.setenv("AGENT_MAX_SEARCHES", "0")
    assert lumen_router_config.agent_max_searches("xhigh") == 0
    monkeypatch.setenv("AGENT_MAX_SEARCHES", "3")
    assert lumen_router_config.agent_max_searches("medium") == 3
    monkeypatch.setenv("AGENT_MAX_SEARCHES", "не число")
    assert lumen_router_config.agent_max_searches("medium") == 2


@pytest.mark.parametrize(("model_id", "effort", "expected"), [
    ("gemini-3.8-flash", "low", ("thinking_level", "low")),
    ("gemini-3.8-flash", "xhigh", ("thinking_level", "high")),
    ("gemini-2.5-flash", "low", ("thinking_budget", 0)),
    ("gemini-2.5-flash", "high", ("", None)),   # the model default: calibration 18.08.2026
    ("gemini-3.8-flash", None, ("", None)),     # EFFORT_AUTO=0 — the previous heuristic outside
    ("gemini-unknown", "high", ("", None)),
])
def test_gemini_thinking_for_maps_effort(model_id, effort, expected):
    assert lumen_router_config.gemini_thinking_for(model_id, effort) == expected


def test_recent_corrections_counts_only_short_user_turns():
    history = [
        {"role": "user", "content": "объясни про шляпы"},
        {"role": "assistant", "content": "…"},
        {"role": "user", "content": "ты не прав, перепроверь"},
        {"role": "user", "content": "в статье приводится фраза «ты не прав был по отношению к…» " * 6},
    ]
    assert lumen_router_config.recent_corrections(history) == 1
    assert lumen_router_config.recent_corrections([]) == 0


def test_munito_route_yields_to_gemini_when_daily_ceiling_is_reached(monkeypatch):
    # Audit 16.11.2026: MUNITO_DAILY_LIMIT was only a number in /stats, and the
    # 251st
    # request got 429 with cooldown across all models at once. The cap must
    # move the route to the unconditional Gemini fallback, not into a storm of refusals.
    import bot

    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(lumen_router_config, "_MUNITO_DISABLED", False)
    saved = dict(bot.GLOBAL_QUOTA.get("munito") or {})
    try:
        bot.GLOBAL_QUOTA["quota_day"] = bot._current_quota_day()
        bot.GLOBAL_QUOTA["munito"] = {
            model: {"used": int(bot.MUNITO_DAILY_LIMIT)} for model in lumen_router_config._MUNITO_ORDER
        }
        assert lumen_router_config._munito_route() == []
        bot.GLOBAL_QUOTA["munito"] = {model: {"used": 0} for model in lumen_router_config._MUNITO_ORDER}
        assert lumen_router_config._munito_route()
    finally:
        bot.GLOBAL_QUOTA["munito"] = saved


def test_munito_ceiling_reads_zero_on_a_new_quota_day(monkeypatch):
    # Third audit pass 16.11.2026: the daily-reset throttle did not let the router see
    # a new day, and for the first minute after PT midnight it read the previous day's 250.
    import bot

    real = dict(bot.GLOBAL_QUOTA)
    try:
        bot.GLOBAL_QUOTA["quota_day"] = "2000-01-01"
        bot.GLOBAL_QUOTA["munito"] = {m: {"used": 10 ** 6} for m in lumen_router_config._MUNITO_ORDER}
        assert lumen_router_config._munito_requests_today() == 0
    finally:
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real)


def test_build_route_classifies_pdf_and_sticker(monkeypatch):
    # Completeness critique 16.11.2026: the document/sticker input classes were not checked.
    # PDF is not an image, so it goes to the Gemini chain like video/audio;
    # a sticker is image/webp, an ordinary Munito image with a Gemini fallback.
    _munito_mode(monkeypatch)
    pdf = _route_for(media_mime="application/pdf")
    # An explicit expected list, not a call to the same function: the tautology hid
    # loss of the reserve (review 17.11.2026).
    assert pdf == [("gemini", model) for model in _GEMINI_HEAVY_FIXED]
    sticker = _route_for(media_mime="image/webp")
    assert sticker == ([("munito", m) for m in _MUNITO_ORDER_FIXED]
                       + [("gemini", m) for m in _GEMINI_HEAVY_FIXED])
