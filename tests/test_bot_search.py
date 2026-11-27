"""test_bot_search.py — end-to-end tests of external search in _handle_message_core.

What they protect: the 11.10.2026 incident — an explicit request to Google with an exhausted
2.5 quota went to models without search and answered from memory. The first test catches
snippet injection into the prompt; the second catches the honest note when external search is
also empty. The quota is disabled selectively (search Gemini only); fetch is mocked at the I/O
boundary.

Run:
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
    # Measurement 11.11.2026: "remember, we discussed repairs yesterday?" was considered fresh
    # a question because of the word "yesterday" — the search started, and the answer received a mark
    # "without the internet". A link to a past conversation is a question to the chat's memory.
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
    # Check the marker against the actual key text, not a made-up substring:
    # "without live web search" does not exist in any language pack.
    assert bot._t(123, "fallback_no_live_search") not in bot._safe_reply.await_args[0][1]
    bot.chat_state.pop(123, None)


def test_bare_time_word_reply_does_not_run_external_search(rate_guard_setup, monkeypatch):
    # Week's corpus (15.11.2026): a bare "yesterday" reply matched the freshness regex—
    # search was triggered by the reaction, not the question.
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
    # A real question with the word "today" still triggers search.
    assert lumen_message_core._wants_web_search("что сегодня идёт по телевизору") is True
    assert lumen_message_core._wants_web_search("вчера") is False
    assert lumen_message_core._wants_web_search("сегодня!") is False
    # Emoji, parentheses, and NBSP do not make a response a question (review 16.11.2026).
    assert lumen_message_core._wants_web_search("вчера 😢") is False
    assert lumen_message_core._wants_web_search("(вчера)") is False
    assert lumen_message_core._wants_web_search("вчера\u00a0?") is False
    # A reply with content next to a time word is not a reaction.
    assert lumen_message_core._wants_web_search("а вчера?") is True


def test_release_status_question_runs_external_search(rate_guard_setup, monkeypatch):
    # Same measurement: "has the finale aired" asks about the current state of affairs; on such
    # search, while the classifier stayed silent, and the model answered from memory.
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
    # Review: with the switch off and an exhausted quota, it silently answered from memory — the note is mandatory.
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
    # Previously, snippets were inserted only when the route had no
    # grounding models; now any responding model must search, including
    # in number, the reserve after a Gemini 429 (production 28.10.2026).
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "Новость", "url": "https://example.com/grounded", "snippet": "Данные"}]

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        # The grounding model answers: the post-hook retry must not mask
        # no prior substitution.
        provenance = kwargs.get("provenance")
        if isinstance(provenance, dict):
            provenance.update(provider="gemini", model="gemini-2.5-flash-lite")
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert "https://example.com/grounded" in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_fresh_query_status_shows_search_then_thinking(rate_guard_setup, monkeypatch):
    # Live status: before search, “Searching the internet”; after — a source counter and
    # "Thinking..." with a timer; the same bubble goes to the route as the answer.
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


def test_status_start_is_deleted_when_pre_route_setup_raises(rate_guard_setup, monkeypatch):
    # Prod 23.11.2026: a link status outlived its request and kept ticking over subsequent ones.
    # The startup task is created before the main try; a prompt-building failure must not
    # leave neither the task nor the bubble in the chat.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    fake_status = _FakeStatus()
    delete_calls = []
    original_delete = fake_status.delete

    async def counted_delete():
        delete_calls.append(True)
        await original_delete()

    fake_status.delete = counted_delete

    async def fake_start(reply_to, lang, key, *, timed=False):
        return fake_status

    def broken_tagged_prompt(*args, **kwargs):
        raise RuntimeError("prompt assembly failed")

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(bot, "_tagged_prompt", broken_tagged_prompt)
    asyncio.run(bot._handle_message_core(message))
    assert fake_status.deleted is True
    assert len(delete_calls) == 1
    bot.chat_state.pop(123, None)


def test_empty_external_search_with_grounded_answer_has_no_note(rate_guard_setup, monkeypatch):
    # Prod 28.10.2026: external search was empty, but Gemini replied with its built-in
    # with search — a "no search" marker would be a lie.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        provenance = kwargs.get("provenance")
        if isinstance(provenance, dict):
            provenance.update(provider="gemini", model="gemini-2.5-flash-lite")
        return "свежий ответ", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert sent_text == "свежий ответ"
    bot.chat_state.pop(123, None)


def test_daily_limit_still_gets_external_search_snippets(rate_guard_setup, monkeypatch):
    # The Gemini daily limit does not disable external search: snippets go to the reserve
    # (Munito), otherwise it answers from memory (the idea behind search is that not
    # only Gemini).
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
    # The neighboring "without live search" note is a separate key (review 17.11.2026).
    assert bot._t(123, "fallback_no_live_search") not in sent_text
    bot.chat_state.pop(123, None)


def test_daily_limit_with_empty_search_keeps_no_search_note(rate_guard_setup, monkeypatch):
    # If external search is also empty, the "no search" marker must remain.
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
    # Audit 28.10.2026: the guest answer goes through answerGuestQuery, not
    # by editing the message: the stream would edit a nonexistent message.
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
    # Date context (eval 22.10.2026): models without live search did not know the current
    # the date and guessed "tomorrow". The date must be sent to the fresh
    # the question and match today's date in Moscow — a hardcoded constant
    # or a format typo will be caught by comparison with the live now.
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
    # Small talk (the zone 2 skip list) gets no date at all; a substantive question
    # freshness words, it still gets a date; the model decides "whether my knowledge
    # is stale"
    # (review 16.11.2026), fresh questions — as before.
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
    # AGENT_MAX_SEARCHES=0 is the owner's kill switch: zone-2 flora must not raise it to one,
    # otherwise muting would work asymmetrically (review 16.11.2026). The "spy + substituted
    # head" form matters: without a head, the cycle would be skipped before the floor, and
    # an AssertionError from the spy would be swallowed by except, leaving an empty test.
    message = rate_guard_setup()
    message.text = "скок стоит терминал старлинк"
    monkeypatch.setenv("AGENT_MAX_SEARCHES", "0")
    monkeypatch.setattr("lumen_message_core._munito_head", lambda route: "deepseek-ai/deepseek-v4.1-flash-1017")
    turn_calls: list = []

    async def spy_turn(**kwargs):
        turn_calls.append(kwargs)
        return SimpleNamespace(snippets=[], queries=[], steps=0, planning_ms=0,
                               searches=0, failed=False, preambles=[], has_evidence=False)

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "Без проверки не скажу", False

    monkeypatch.setattr("lumen_agent.run_search_turn", spy_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert not turn_calls, "килл-свитч AGENT_MAX_SEARCHES=0 обязан глушить и зону 2"
    bot.chat_state.pop(123, None)

def test_anaphoric_followup_searches_with_previous_topic(rate_guard_setup, monkeypatch):
    # Measured 11.11.2026: for "and who came before her," the engine received a pronoun and could not find
    # anything. The continuation topic comes from the previous question in the same chat, and such a
    # the question must also trigger search; otherwise the answer is built from memory.
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


# ── zone 2: the regex was silent — the agent-loop model decides ──

def test_zone2_substantive_message_searches_via_agent_turn(rate_guard_setup, monkeypatch):
    # Prod 15.11.2026: "is theoneprobe available for 1.7.10" — the freshness regex stayed silent,
    # the agent loop was never invoked, and the answer relied on memory. Zone 2: substantive
    # a message without freshness also enters the loop, and the result goes into the
    # prompt.
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

    # The test environment has no Munito key, so the route is built without it:
    # for zone 2, replace the head; otherwise, the loop is silently skipped (head=None).
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
    # A model's refusal to search in zone 2 is normal: the “no live search” mark on
    # on a stale question would be a lie, and a fallback fetch using the raw phrase would be nonsense.
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
    # The marker is checked against the actual key text (review 16.11.2026: the previous
    # the assertion caught a substring absent from every language pack).
    assert not any(bot._t(123, "fallback_no_live_search") in t for t in texts)
    assert not any(bot._t(123, "fallback_no_search") in t for t in texts)
    bot.chat_state.pop(123, None)


def test_zone2_chatter_skips_agent_turn(rate_guard_setup, monkeypatch):
    # Small talk does not invoke the loop: every extra planning call costs a second and tokens.
    message = rate_guard_setup()
    message.text = "спасибо"

    # A recorder, not an exception in the mock: the call was wrapped in except Exception and
    # swallowed, so a "failing" test stayed green (review 17.11.2026).
    calls = []

    async def record_turn(**kwargs):
        calls.append(kwargs)
        return ""

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "Всегда пожалуйста", False

    monkeypatch.setattr("lumen_agent.run_search_turn", record_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert calls == [], "болтовня не должна звать планирующий агент-цикл"
    bot.chat_state.pop(123, None)


# The model itself decides whether to search: the [[SEARCH]] marker


def _stream_turns(monkeypatch, turns):
    """Gemini stream chunks listed for each route pass: the first response is a marker, the second is grounded text. The client is replaced, not the streaming code, so `_run_streaming_reply` follows the production path."""
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
    """Everything sent to Telegram: only _tg_call and _safe_reply lead to the transport."""
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
    """Counters for everything that makes the model strike something from routing:
    a search request is not a refusal by the model, and none of these hooks
    should fire."""
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
    """Prompt and allow_stream of each route pass under the production _run_route."""
    calls: list = []
    real_run_route = bot._run_route

    async def spy(chat_id, ai_prompt, route, message, **kwargs):
        calls.append((ai_prompt, kwargs.get("allow_stream")))
        return await real_run_route(chat_id, ai_prompt, route, message, **kwargs)

    monkeypatch.setattr(bot, "_run_route", spy)
    return calls


def _kill_classifier(monkeypatch):
    # Suppress the word list deliberately: this tests the second mechanism, which must
    # work even where the classifier did not recognize the wording.
    monkeypatch.setattr("lumen_message_core._wants_web_search", lambda text, *, previous="": False)


def test_model_search_marker_retries_with_snippets_and_shows_second_answer(rate_guard_setup, monkeypatch):
    # Group screenshots 11.11.2026: "when did Fable 5.2 come out" received "no such
    # model is absent' without searching. The model now requests search itself with a marker, and the first
    # the pass with a marker must not appear.
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
    # The marker search is empty: the person must still receive an answer, not an empty
    # the bubble, not a service line.
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
    # An ordinary answer must neither wait for search nor lose live typing:
    # the marker is caught only in a response consisting solely of the marker.
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
    # The model emitted the marker and still wrote an answer: the internal token must
    # be truncated instead of going to chat (measurement 11.11.2026 — only the marker saw
    # the "marker only" branch; the admixture in the text was displayed as is).
    import lumen_routes
    assert lumen_routes._raise_if_search_requested("[[SEARCH]] Обычный ответ", "munito", "m-x") == "Обычный ответ"
    assert lumen_routes._raise_if_search_requested("Обычный ответ", "munito", "m-x") == "Обычный ответ"
    with pytest.raises(lumen_routes._SearchRequested):
        lumen_routes._raise_if_search_requested("[[SEARCH]]", "munito", "m-x")
