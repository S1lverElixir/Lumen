"""
test_bot_search.py — сквозные тесты внешнего поиска в _handle_message_core.

Что защищают: инцидент 11.10.2026 — явная просьба "погугли" при мёртвой квоте
2.5 шла на модели без поиска и отвечала по памяти. Первый тест ловит подклейку
сниппетов в промпт, второй — честную пометку, когда и внешний поиск пуст.
Квота гасится точечно (только поисковые Gemini), fetch мокается на границе I/O.

Запуск:
    pytest tests/test_bot_search.py -v
"""
import asyncio
import pytest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import bot
import lumen_message_core
from tests.bot_test_helpers import _FakeStatus, _run_core_capturing_prompt

_DEAD_SEARCH_QUOTA = {("gemini", "gemini-2.5-flash"), ("gemini", "gemini-2.5-flash-lite")}


def _kill_search_quota(monkeypatch):
    import lumen_router_config
    monkeypatch.setattr(
        lumen_router_config,
        "_is_quota_exhausted",
        lambda provider, model_id: (provider, model_id) in _DEAD_SEARCH_QUOTA,
    )


def test_explicit_search_request_gets_snippets_without_live_search(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    _kill_search_quota(monkeypatch)

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        assert "ДЦ" in query
        return [{"title": "Новость", "url": "https://example.com/n", "snippet": "Свежие данные"}]

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert "Web search results" in prompt
    assert "https://example.com/n" in prompt
    bot.chat_state.pop(123, None)


def test_past_chat_reference_does_not_run_external_search(rate_guard_setup, monkeypatch):
    # Замер 11.11.2026: «помнишь, мы вчера обсуждали ремонт?» считалось свежим
    # вопросом из-за слова «вчера» — поиск запускался и ответ получал пометку
    # «без интернета». Ссылка на прошлый разговор — это вопрос к памяти чата.
    message = rate_guard_setup()
    message.text = "помнишь, мы вчера обсуждали ремонт? что ты советовал про плитку"
    _kill_search_quota(monkeypatch)
    calls = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        calls.append(query)
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert calls == []
    # Пометку сверяем с фактическим текстом ключа, а не выдуманной подстрокой:
    # «without live web search» ни в одном языковом пакете не существует.
    assert bot._t(123, "fallback_no_live_search") not in bot._safe_reply.await_args[0][1]
    bot.chat_state.pop(123, None)


def test_bare_time_word_reply_does_not_run_external_search(rate_guard_setup, monkeypatch):
    # Корпус недели (15.11.2026): голая реплика «вчера» ловилась регексом свежести —
    # поиск запускался на реакцию, не на вопрос.
    message = rate_guard_setup()
    message.text = "вчера"
    _kill_search_quota(monkeypatch)
    calls = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        calls.append(query)
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert calls == []
    bot.chat_state.pop(123, None)


def test_bare_time_word_guard_does_not_hide_real_fresh_questions():
    # Настоящий вопрос со словом «сегодня» по-прежнему ищется.
    assert lumen_message_core._wants_web_search("что сегодня идёт по телевизору") is True
    assert lumen_message_core._wants_web_search("вчера") is False
    assert lumen_message_core._wants_web_search("сегодня!") is False
    # Эмодзи, скобки и NBSP не делают реакцию вопросом (ревью 16.11.2026).
    assert lumen_message_core._wants_web_search("вчера 😢") is False
    assert lumen_message_core._wants_web_search("(вчера)") is False
    assert lumen_message_core._wants_web_search("вчера\u00a0?") is False
    # Реплика с содержанием рядом со словом времени — не реакция.
    assert lumen_message_core._wants_web_search("а вчера?") is True


def test_release_status_question_runs_external_search(rate_guard_setup, monkeypatch):
    # Тот же замер: «вышёл ли финал» — вопрос о текущем состоянии дела, на таком
    # ищут, а классификатор молчал, и модель отвечала по памяти.
    message = rate_guard_setup()
    message.text = "что нового в пятом сезоне очень странных дел, вышел ли финал"
    seen = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen.append(query)
        return [{"title": "Финал вышел", "url": "https://example.com/final", "snippet": "2026"}]

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert seen
    assert "Web search results" in prompt
    bot.chat_state.pop(123, None)


def test_empty_fallback_search_marks_reply_without_live_search(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    _kill_search_quota(monkeypatch)

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert "Answering without the web" in sent_text
    bot.chat_state.pop(123, None)


def test_disabled_search_marks_reply_without_live_search(rate_guard_setup, monkeypatch):
    # Ревью: выключенный свитч при мёртвой квоте молча отвечал по памяти — пометка обязана быть.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    _kill_search_quota(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        raise AssertionError("fetch must not run while disabled")

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert "Answering without the web" in sent_text
    bot.chat_state.pop(123, None)


def test_fresh_query_with_live_gemini_still_gets_external_snippets(rate_guard_setup, monkeypatch):
    # Раньше сниппеты подставлялись только когда в маршруте не оставалось
    # grounding-моделей; теперь искать обязана любая ответившая модель, в том
    # числе резервная после 429 у Gemini (прод 28.10.2026).
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "Новость", "url": "https://example.com/grounded", "snippet": "Данные"}]

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        # Отвечает grounding-модель: пост-хок ретрай не должен маскировать
        # отсутствие предварительной подстановки.
        provenance = kwargs.get("provenance")
        if isinstance(provenance, dict):
            provenance.update(provider="gemini", model="gemini-2.5-flash")
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert "https://example.com/grounded" in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_fresh_query_status_shows_search_then_thinking(rate_guard_setup, monkeypatch):
    # Живой статус: до поиска «Ищу в интернете», после — счётчик источников и
    # «Думаю…» с таймером; тот же пузырь уходит в маршрут под ответ.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    fake_status = _FakeStatus()
    captured: dict = {}

    async def fake_start(reply_to, lang, key, *, timed=False):
        captured["initial"] = (key, timed)
        return fake_status

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "T", "url": "https://example.com/1", "snippet": "S"},
                {"title": "T2", "url": "https://example.com/2", "snippet": "S2"}]

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["status"] = kwargs.get("status")
        return "ok", False

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert captured["initial"] == ("progress_searching", False)
    assert fake_status.done == [("progress_search_done", {"count": 2})]
    assert fake_status.phases == [("progress_thinking", True)]
    assert captured["status"] is fake_status
    assert fake_status.stopped is True
    bot.chat_state.pop(123, None)


def test_empty_external_search_with_grounded_answer_has_no_note(rate_guard_setup, monkeypatch):
    # Прод 28.10.2026: внешний поиск пуст, но ответила Gemini со встроенным
    # поиском — пометка «без поиска» была бы ложью.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        provenance = kwargs.get("provenance")
        if isinstance(provenance, dict):
            provenance.update(provider="gemini", model="gemini-2.5-flash")
        return "свежий ответ", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert sent_text == "свежий ответ"
    bot.chat_state.pop(123, None)


def test_daily_limit_still_gets_external_search_snippets(rate_guard_setup, monkeypatch):
    # Дневной лимит Gemini не отключает внешний поиск: сниппеты едут резерву
    # (Munito), иначе он отвечает по памяти (задумка поиска — искать умеет не
    # только Gemini).
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_user_daily_gemini_exhausted", lambda uid, entry: True)
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "Новость", "url": "https://example.com/daily", "snippet": "Свежие данные"}]

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        captured["allow_stream"] = kwargs.get("allow_stream")
        assert all(p != "gemini" for p, _ in route)
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert "https://example.com/daily" in captured["prompt"]
    assert captured["allow_stream"] is True
    sent_text = bot._safe_reply.await_args[0][1]
    assert "without web search" not in sent_text
    bot.chat_state.pop(123, None)


def test_daily_limit_with_empty_search_keeps_no_search_note(rate_guard_setup, monkeypatch):
    # Если и внешний поиск пуст — пометка «без поиска» обязана остаться.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_user_daily_gemini_exhausted", lambda uid, entry: True)
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["allow_stream"] = kwargs.get("allow_stream")
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert sent_text.startswith("Answering without web search")
    assert captured["allow_stream"] is False
    bot.chat_state.pop(123, None)


def test_guest_message_never_streams(rate_guard_setup, monkeypatch):
    # Аудит 28.10.2026: гостевой ответ уходит через answerGuestQuery, а не
    # правкой сообщения — стрим редактировал бы несуществующее сообщение.
    message = rate_guard_setup()
    message.text = "привет"
    message.guest_query_id = "g1"
    monkeypatch.setattr(bot, "is_guest_message", lambda m: True)
    captured: dict = {}

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["allow_stream"] = kwargs.get("allow_stream")
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert captured["allow_stream"] is False
    bot.chat_state.pop(123, None)


def test_freshness_prompt_carries_today_date(rate_guard_setup, monkeypatch):
    # Контекст даты (eval 22.10.2026): модели без живого поиска не знали текущую
    # дату и строили «завтра» по догадке. Дата обязана уехать в промпт свежего
    # вопроса и соответствовать сегодняшнему дню в Москве — зашитая константа
    # или опечатка формата поймаются сравнением с живым now.
    message = rate_guard_setup()
    message.text = "какая сегодня погода в Москве"
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    prompt = captured["prompt"]
    assert "[Context: today is" in prompt
    assert "(Europe/Moscow)" in prompt
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    assert f"{now.day} {now.strftime('%B')} {now.year}" in prompt
    bot.chat_state.pop(123, None)


def test_non_freshness_prompt_has_no_date_context(rate_guard_setup, monkeypatch):
    # Болтовня (skip-лист зоны 2) дату не получает вовсе; содержательный вопрос
    # без слов свежести дату получает — модель решает «устарело ли моё знание»
    # (ревью 16.11.2026), свежие вопросы — как и раньше.
    message = rate_guard_setup()
    message.text = "спасибо"
    captured: dict = {}

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert "[Context: today is" not in captured["prompt"]

    message2 = rate_guard_setup()
    message2.text = "объясни, как работает рекурсия"
    captured2: dict = {}

    async def fake_run_route2(chat_id, ai_prompt, route, message, **kwargs):
        captured2["prompt"] = ai_prompt
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route2)
    asyncio.run(bot._handle_message_core(message2))
    assert "[Context: today is" in captured2["prompt"]
    bot.chat_state.pop(123, None)


def test_zone2_respects_agent_max_searches_zero_kill_switch(rate_guard_setup, monkeypatch):
    # AGENT_MAX_SEARCHES=0 — килл-свитч владельца: флор зоны 2 не имеет права
    # поднимать его до единицы, иначе глушение работало бы асимметрично
    # (ревью 16.11.2026).
    message = rate_guard_setup()
    message.text = "скок стоит терминал старлинк"
    monkeypatch.setenv("AGENT_MAX_SEARCHES", "0")

    async def fail_turn(**kwargs):
        raise AssertionError("зона 2 не должна обходить килл-свитч владельца")

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "Без проверки не скажу", False

    monkeypatch.setattr("lumen_agent.run_search_turn", fail_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    bot.chat_state.pop(123, None)

def test_anaphoric_followup_searches_with_previous_topic(rate_guard_setup, monkeypatch):
    # Замер 11.11.2026: на «а кто был до неё» движок получало местоимение и не находил
    # ничего. Тема продолжения — из прошлого вопроса того же чата, и сам такой
    # вопрос тоже обязан запускать поиск, иначе ответ строится по памяти.
    message = rate_guard_setup()
    message.text = "а кто был до неё"
    state = {"history": [{"role": "user", "content": "кто сейчас возглавляет Центральный банк России"}]}
    monkeypatch.setattr(bot, "get_state", lambda chat_id: state)
    seen = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen.append((query, context))
        return [{"title": "Предшественник", "url": "https://ru.wikipedia.org/wiki/x", "snippet": "с 2013"}]

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert seen, "поиск на продолжении свежего вопроса обязан запускаться"
    assert "Центральный банк" in seen[0][1]
    assert "Web search results" in prompt
    bot.chat_state.pop(123, None)


# ── зона 2: регекс молчал — решает модель агент-цикла ──

def test_zone2_substantive_message_searches_via_agent_turn(rate_guard_setup, monkeypatch):
    # Прод 15.11.2026: «есть ли theoneprobe на 1.7.10» — регекс свежести молчал,
    # агент-цикл не звался вовсе, ответ уходил по памяти. Зона 2: содержательное
    # сообщение без свежести тоже идёт в цикл, найденное попадает в промпт.
    message = rate_guard_setup()
    message.text = "скок стоит терминал старлинк"
    captured: dict = {}
    turn_calls: list = []

    class _Turn:
        snippets = [{"title": "Starlink Standard Kit", "url": "https://www.starlink.com/",
                     "snippet": "$349", "publisher": "starlink.com", "date": "", "kind": "web"}]
        queries = ["Starlink Standard Kit price"]
        steps = 1
        planning_ms = 500
        searches = 1
        failed = False
        preambles: list = []
        has_evidence = True

    async def fake_turn(**kwargs):
        turn_calls.append(kwargs)
        return _Turn()

    fetched: list = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        fetched.append(query)
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        return "ок", False

    # В тестовом окружении ключа Munito нет и маршрут собирается без него:
    # для зоны 2 подменяем голову, иначе цикл молча пропускается (head=None).
    monkeypatch.setattr("lumen_message_core._munito_head", lambda route: "deepseek-ai/deepseek-v4.1-flash-1017")
    monkeypatch.setattr("lumen_agent.run_search_turn", fake_turn)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert turn_calls, "агент-цикл зоны 2 обязан запускаться на содержательном вопросе без свежести"
    assert "Web search results" in captured["prompt"]
    assert "Search queries already executed" in captured["prompt"]
    assert fetched == [], "в зоне 2 фолбэк-фетч по сырой фразе не делается"
    bot.chat_state.pop(123, None)


def test_zone2_model_decline_is_normal_no_no_search_note(rate_guard_setup, monkeypatch):
    # Отказ модели искать в зоне 2 — норма: пометка «без live-поиска» на
    # несвежем вопросе была бы ложью, фолбэк-фетч по сырой фразе — болтовнёй.
    message = rate_guard_setup()
    message.text = "скок стоит терминал старлинк"

    class _Turn:
        snippets: list = []
        queries: list = []
        steps = 0
        planning_ms = 400
        searches = 0
        failed = False
        preambles: list = []
        has_evidence = False

    async def fake_turn(**kwargs):
        return _Turn()

    async def fake_fetch(query, **kwargs):
        raise AssertionError("фолбэк-фетч в зоне 2 не запускается")

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "Терминал Starlink стоит около $349", False

    monkeypatch.setattr("lumen_message_core._munito_head", lambda route: "deepseek-ai/deepseek-v4.1-flash-1017")
    monkeypatch.setattr("lumen_agent.run_search_turn", fake_turn)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    texts = _outgoing_texts()
    assert any("около $349" in t for t in texts), "ответ обязан доехать без поиска"
    # Пометка сверяется с фактическим текстом ключей (ревью 16.11.2026: прежний
    # ассерт ловил подстроку, которой ни в одном языковом пакете нет).
    assert not any(bot._t(123, "fallback_no_live_search") in t for t in texts)
    assert not any(bot._t(123, "fallback_no_search") in t for t in texts)
    bot.chat_state.pop(123, None)


def test_zone2_chatter_skips_agent_turn(rate_guard_setup, monkeypatch):
    # Болтовня не зовёт цикл: каждый лишний планирующий вызов — секунда и токены.
    message = rate_guard_setup()
    message.text = "спасибо"

    async def fail_turn(**kwargs):
        raise AssertionError("агент-цикл не должен зваться на болтовне")

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "Всегда пожалуйста", False

    monkeypatch.setattr("lumen_agent.run_search_turn", fail_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    bot.chat_state.pop(123, None)


# ── решение о поиске принимает сама модель: маркер [[SEARCH]] ──


def _stream_turns(monkeypatch, turns):
    """Куски Gemini-стрима по списку на каждый проход маршрута: первый ответ —
    маркер, второй — заземлённый текст. Подменяется клиент, а не код стрима,
    чтобы `_run_streaming_reply` шёл боевым путём."""
    queue = [list(t) for t in turns]

    async def _stream(*, model, contents, config=None):
        pieces = queue.pop(0) if queue else []

        async def gen():
            for piece in pieces:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _stream
    monkeypatch.setattr(bot, "client", fake_client)


def _outgoing_texts():
    """Всё, что ушло в Telegram: к транспорту ведут только _tg_call и _safe_reply."""
    out = []
    for call in bot._tg_call.await_args_list:
        for value in list(call.args[1:]) + list(call.kwargs.values()):
            if isinstance(value, str):
                out.append(value)
    for call in bot._safe_reply.await_args_list:
        for value in call.args[1:]:
            if isinstance(value, str):
                out.append(value)
    return out


def _routing_penalty_spies(monkeypatch):
    """Счётчики всего, что вычёркивает модель из роутинга: просьба поиска — не
    её отказ, ни один из этих хуков дёрнут быть не должен."""
    import lumen_routes
    marks: list = []
    monkeypatch.setattr(bot, "_mark_rate_limited", lambda provider, model_id: marks.append("rate_limited"))
    monkeypatch.setattr(bot, "_mark_quota_exhausted", lambda provider, model_id: marks.append("quota_exhausted"))
    monkeypatch.setattr(lumen_routes, "_mark_model_unavailable",
                        lambda provider, model_id, exc: marks.append("unavailable"))

    def _outcome(provider, model_id, bad=False):
        if bad:
            marks.append("quarantine")

    monkeypatch.setattr(bot, "_record_model_outcome", _outcome)
    stats: list = []
    monkeypatch.setattr(bot, "_record_stats_event", lambda name: stats.append(name))
    return marks, stats


def _route_spy(monkeypatch):
    """Промпт и allow_stream каждого прохода маршрута под боевым _run_route."""
    calls: list = []
    real_run_route = bot._run_route

    async def spy(chat_id, ai_prompt, route, message, **kwargs):
        calls.append((ai_prompt, kwargs.get("allow_stream")))
        return await real_run_route(chat_id, ai_prompt, route, message, **kwargs)

    monkeypatch.setattr(bot, "_run_route", spy)
    return calls


def _kill_classifier(monkeypatch):
    # Список слов глушим намеренно: проверяется второй механизм, он обязан
    # работать и там, где классификатор формулировку не узнал.
    monkeypatch.setattr("lumen_message_core._wants_web_search", lambda text, *, previous="": False)


def test_model_search_marker_retries_with_snippets_and_shows_second_answer(rate_guard_setup, monkeypatch):
    # Скрины из группы 11.11.2026: на «когда вышла Fable 5.2» был ответ «такой
    # модели нет» без поиска. Модель теперь просит поиск сама маркером, и первый
    # проход с маркером не имеет права показаться.
    message = rate_guard_setup()
    message.text = "когда вышла Fable 5.2"
    _kill_classifier(monkeypatch)
    _stream_turns(monkeypatch, [["[[SE", "ARCH]]"], ["Fable 5.2 вышла 3 ноября 2026"]])
    seen_queries: list = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen_queries.append(query)
        return [{"title": "Релиз", "url": "https://example.com/r", "snippet": "3 ноября 2026"}]

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    routes = _route_spy(monkeypatch)
    marks, stats = _routing_penalty_spies(monkeypatch)
    asyncio.run(bot._handle_message_core(message))

    assert seen_queries == ["когда вышла Fable 5.2"]
    assert len(routes) == 2, "дополнительный проход должен быть ровно один"
    assert "[Web search results" in routes[1][0]
    assert routes[1][1] is True, "на втором проходе человек ждёт живой набор"
    texts = _outgoing_texts()
    assert texts and not any("[[SE" in t for t in texts)
    assert any("Fable 5.2 вышла 3 ноября 2026" in t for t in texts)
    assert marks == [] and "model_failures" not in stats
    bot.chat_state.pop(123, None)


def test_model_search_marker_without_snippets_does_not_lose_the_answer(rate_guard_setup, monkeypatch):
    # Поиск по маркеру пуст: человек всё равно обязан получить ответ, а не пустой
    # пузырь и не служебную строку.
    message = rate_guard_setup()
    message.text = "когда вышла Fable 5.2"
    _kill_classifier(monkeypatch)
    _stream_turns(monkeypatch, [["[[SEARCH]]"], ["Точной даты у меня нет, не проверял"]])

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return []

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    routes = _route_spy(monkeypatch)
    marks, stats = _routing_penalty_spies(monkeypatch)
    asyncio.run(bot._handle_message_core(message))

    assert len(routes) == 2
    assert "No web search is available" in routes[1][0]
    texts = _outgoing_texts()
    assert texts and not any("[[SE" in t for t in texts)
    assert any("Точной даты у меня нет" in t for t in texts)
    assert marks == [] and "model_failures" not in stats
    bot.chat_state.pop(123, None)


def test_answer_without_marker_streams_and_never_searches(rate_guard_setup, monkeypatch):
    # Обычный ответ не должен ни дождаться поиска, ни лишиться живой печати:
    # маркер ловится только в ответе, который состоит из маркера.
    message = rate_guard_setup()
    message.text = "объясни, как работает рекурсия"
    _kill_classifier(monkeypatch)
    _stream_turns(monkeypatch, [["Рекурсия — ", "функция, которая зовёт себя сама"]])

    async def fake_fetch(query, **kwargs):
        raise AssertionError("внешний поиск на обычном вопросе запускать нельзя")

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    routes = _route_spy(monkeypatch)
    asyncio.run(bot._handle_message_core(message))

    assert len(routes) == 1
    texts = _outgoing_texts()
    assert any("функция, которая зовёт себя сама" in t for t in texts)
    bot.chat_state.pop(123, None)


def test_search_marker_never_reaches_the_chat_inside_an_answer():
    # Модель поставила маркер и всё равно написала ответ: служебный знак обязан
    # срезаться, а не уезжать в чат (замер 11.11.2026 — маркер видела только
    # ветка «только маркер», примесь в тексте показывалась как есть).
    import lumen_routes
    assert lumen_routes._raise_if_search_requested("[[SEARCH]] Обычный ответ", "munito", "m-x") == "Обычный ответ"
    assert lumen_routes._raise_if_search_requested("Обычный ответ", "munito", "m-x") == "Обычный ответ"
    with pytest.raises(lumen_routes._SearchRequested):
        lumen_routes._raise_if_search_requested("[[SEARCH]]", "munito", "m-x")
