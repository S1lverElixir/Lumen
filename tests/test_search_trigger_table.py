"""
test_search_trigger_table.py — таблица «искал бы Claude»: когда бот обязан пойти в веб.

Что защищают: два класса регрессий, замеренных 11.11.2026. (1) Пропуск: «вышёл ли
финал», «какая ключевая ставка», «when does the season finale come out» не запускали
поиск вовсе — модель отвечала по памяти и угадывала. (2) Ложное срабатывание:
«помнишь, мы вчера обсуждали ремонт?» считалось свежим вопросом из-за слова
«вчера», поиск гас зря, а ответ получал пометку «без интернета».

Тест детерминированный: только классификатор, без сети.
Запуск:
    pytest tests/test_search_trigger_table.py -v
"""
import pytest

from lumen_message_core import _wants_web_search

# Формулировки на языках, которые реально видит пользователь: таблица обязана
# работать не только для ru/en (внешний аудит 28.10.2026 ловил именно это).
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
    # Ссылка на прошлый разговор — вопрос к памяти чата, а не к вебу (11.11.2026).
    "помнишь, мы вчера обсуждали ремонт?",
    "как мы раньше решили, какой город выбираем?",
    "do you remember what we talked about yesterday",
]


@pytest.mark.parametrize("text", SEARCHES)
def test_fresh_information_questions_want_web_search(text):
    assert _wants_web_search(text) is True


@pytest.mark.parametrize("text", NO_SEARCH)
def test_knowledge_and_memory_questions_do_not_want_web_search(text):
    assert _wants_web_search(text) is False


def test_empty_text_never_searches():
    # Пустой текст не должен гнать в поиск и тратить бюджет сообщения.
    assert _wants_web_search("") is False

def test_followup_question_searches_only_when_there_is_a_topic():
    # «а кто был до неё» само по себе искать нечего: без прошлого вопроса бот
    # не гоняет поиск в пустоту, с прошлым — ищет по теме (замер 11.11.2026).
    follow_up = "а кто был до неё"
    assert _wants_web_search(follow_up) is False
    assert _wants_web_search(follow_up, previous="кто сейчас возглавляет Центральный банк России") is True


def test_followup_predicate_recognizes_pronouns_only():
    from lumen_search import is_anaphoric_followup
    assert is_anaphoric_followup("а у него как?") is True
    assert is_anaphoric_followup("а какая погода в Минске") is False
    assert is_anaphoric_followup("какой процессор лучше купить для игр") is False
