"""test_search_trigger_table.py — the “would Claude search?” table: when the bot must use the web.

It protects against three regressions measured on 11.11.2026. (1) Missed searches:
“has the finale come out,” “what is the key rate,” and “when does the season finale
come out” did not trigger a search at all — the model answered from memory and
guessed. (2) False positives: “remember, we discussed repairs yesterday?” was treated
as a fresh question because of “yesterday,” so an unnecessary search ran and the
answer was marked “without internet.” (3) Missed language forms: group screenshots
from 11.11.2026 showed “when did Fable 5.2 come out” and “when did the claude Fable 5.2
model come out” without a search because the form list relied on trigger words.
The rules are now structural (verb root, name+version, event class), and the table
also checks the reverse: math, recipes, and questions about the assistant itself must
not trigger a web search.

The test is deterministic: classifier only, no network.
Run:
    pytest tests/test_search_trigger_table.py -v
"""
import pytest

from lumen_message_core import _wants_web_search

# Wording in languages users actually see: the table must
# work not only for ru/en (an external audit on 28.10.2026 caught exactly this).
SEARCHES = [
    "кто сейчас возглавляет Центральный банк России",
    "какая ключевая ставка в россии",
    "ключевая ставка ЦБ",
    "сколько стоит bitcoin прямо сейчас",
    "курс евро к рублю сегодня",
    "какая сейчас погода в Минске",
    "последние новости Беларуси",
    "что нового в пятом сезоне, вышел ли финал",
    "какой процессор лучше купить для игр в 2026",
    "погугли, что происходит с ДЦ Яндекса",
    "когда выйдет новый iphone",
    "кто выиграл последний матч НБА",
    "во сколько рассвет сегодня в Москве",
    "who is the current secretary general of the UN",
    "latest AI news",
    "what is the price of gold today",
    "when does the season finale come out",
    "has the finale aired",
    "weather in Warsaw tomorrow",
    "aktuelle Nachrichten aus Berlin",
    "prix de l'or aujourd'hui",
    "noticias de hoy en España",
    "какі навіны ў Беларусі сёння",
    # Group screenshots 11.11.2026: "when was it released" was not in the list of forms, so the bot
    # answered without searching. Detection is now structural (root, version, question class).
    "когда вышла Fable 5.2",
    "когда вышла модель claude Fable 5.2",
    "когда вышел iPhone 17",
    "когда вышла Windows 12",
    "выйдет ли сезон 3 The Bear",
    "Fable 5.2 уже вышла?",
    "GTA 7",
    "есть ли анонс Cyberpunk 3",
    "поступил ли в продажу Pixel 10",
    "when was GPT-6 released",
    "is there a new CUDA version",
    "анонсирована ли Llama 5",
    "презентация Gemini 4 когда",
    "открылся ли новый ТЦ «Авиапарк»",
    "что нового в обновлении 3.2",
    "правда ли, что вышел S26",
    "существует ли модель Fable 6",
    # eval/search_trigger_corpus.py corpus 11.11.2026: these seven phrasings
    # exposed cases the agent's structural rules still missed (7 misses out of 35).
    "кто выиграл Лигу наций",
    "кто главный в NVIDIA",
    "кто канцлер Германии",
    "обновление прошивки на телефоне вышло",
    "сколько эпизодов во втором сезоне",
    "забастовка актёров закончилась",
    "расскажи про Silent Hill f",
]

NO_SEARCH = [
    "почему небо голубое",
    "напиши функцию на python, которая считает факториал",
    "переведи на английский: спасибо за помощь",
    "что такое фотосинтез простыми словами",
    "придумай имя для рыжего кота",
    "как приготовить пасту карбонара",
    "исправь ошибку в этом коде",
    "расскажи анекдот про программистов",
    "какая ты модель?",
    "нарисуй кота в шляпе",
    "объясни, что такое инфляция",
    "сколько будет 245 на 13",
    "составь план тренировки на неделю",
    "сравни python и java по синтаксису",
    # A link to a previous conversation is a question for chat memory, not the web (11.11.2026).
    "помнишь, мы вчера обсуждали ремонт?",
    "как мы раньше решили, какой город выбираем?",
    "do you remember what we talked about yesterday",
    # Mandatory false positives from structural rules (owner requirement 11.11.2026).
    "объясни, что такое митохондрия",
    "сколько будет 45*17",
    "напиши стих про осень",
    "переведи на немецкий: как дела",
    "исправь код",
    "что такое REST API",
    "почему небо синее",
    "расскажи про Вторую мировую",
    "как приготовить борщ",
    "какая у тебя версия",
    "ты на каком модели работаешь",
    "реши уравнение 3x + 7 = 22",
    "что такое OOM в Kubernetes",
]


@pytest.mark.parametrize("text", SEARCHES)
def test_fresh_information_questions_want_web_search(text):
    assert _wants_web_search(text) is True


@pytest.mark.parametrize("text", NO_SEARCH)
def test_knowledge_and_memory_questions_do_not_want_web_search(text):
    assert _wants_web_search(text) is False


def test_empty_text_never_searches():
    # Empty text must not trigger search and consume the message budget.
    assert _wants_web_search("") is False

def test_followup_question_searches_only_when_there_is_a_topic():
    # "and who came before her" has nothing to search for on its own: without a previous question, the bot
    # does not run a pointless search; with history, it searches by topic (measurement 11.11.2026).
    follow_up = "а кто был до неё"
    assert _wants_web_search(follow_up) is False
    assert _wants_web_search(follow_up, previous="кто сейчас возглавляет Центральный банк России") is True


def test_followup_predicate_recognizes_pronouns_only():
    from lumen_search import is_anaphoric_followup
    assert is_anaphoric_followup("а у него как?") is True
    assert is_anaphoric_followup("а какая погода в Минске") is False
    assert is_anaphoric_followup("какой процессор лучше купить для игр") is False
