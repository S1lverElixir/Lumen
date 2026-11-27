"""test_bot_agent_turn.py — end-to-end tests of the agent turn in _handle_message_core.

What they protect: since 12.11.2026, fresh sources are collected by a loop in which
    the model writes the queries (lumen_agent.run_search_turn), while the previous
    behavior — one external search of the user's raw phrase — remains a fallback.
    The tests below cover the boundaries where the new layer could either lose the
    search entirely (loop termination), do unnecessary work (switch disabled, no
    sources), or misreport effort.

Run:
    pytest tests/test_bot_agent_turn.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

import bot
import lumen_agent
from tests.bot_test_helpers import _FakeStatus


def _run_core(message, monkeypatch, *, captured=None):
    """Runs _handle_message_core while capturing both the prompt and route kwargs."""

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        if captured is not None:
            captured["prompt"] = ai_prompt
            captured["route"] = route
            captured["kwargs"] = kwargs
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))


def _turn(snippets=None, queries=None, failed=False):
    result = lumen_agent.AgentTurnResult()
    result.snippets = list(snippets or [])
    result.queries = list(queries or [])
    result.by_url = {(s.get("url") or f"k{i}"): s for i, s in enumerate(result.snippets)}
    result.searched = bool(result.snippets)
    result.failed = failed
    result.searches = len(result.queries)
    return result


def test_fresh_question_searches_with_model_queries_not_user_words(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    # The live head of the text route is Munito: without a key, the router sends all text
    # Gemini, while the planning loop lives in Munito.
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    seen = {"fetch": [], "loop": 0}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen["fetch"].append(query)
        return [{"title": "Тренер", "url": "https://example.org/a", "snippet": "назначен",
                 "publisher": "example.org"}]

    async def fake_turn(**kwargs):
        seen["loop"] += 1
        assert kwargs["clean_prompt"] == message.text
        assert kwargs["max_searches"] >= 1
        assert kwargs["effort"] in ("medium", "high")
        return _turn(queries=["главный тренер сборной Беларуси 2026"],
                     snippets=[{"title": "Тренер", "url": "https://example.org/a",
                                "snippet": "назначен", "publisher": "example.org"}])

    captured = {}
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    _run_core(message, monkeypatch, captured=captured)

    assert seen["loop"] == 1
    # Fallback search on the raw phrase does not run when the loop already found sources:
    # a second fetch on the same message would cost another wasted network round trip.
    assert seen["fetch"] == []
    assert "главный тренер сборной Беларуси 2026" in captured["prompt"]
    assert "https://example.org/a" in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_broken_search_loop_falls_back_to_plain_fetch(rate_guard_setup, monkeypatch):
    # Regression that must not be allowed: a failure in the add-on deprives the person of search
    # at all (a "no live search" marker on a question that has sources).
    message = rate_guard_setup()
    message.text = "какая сейчас ключевая ставка"
    seen = {"fetch": []}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen["fetch"].append(query)
        return [{"title": "Ставка", "url": "https://example.org/r", "snippet": "16%",
                 "publisher": "example.org"}]

    async def boom(**kwargs):
        raise RuntimeError("planning call failed")

    captured = {}
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(lumen_agent, "run_search_turn", boom)
    _run_core(message, monkeypatch, captured=captured)

    assert seen["fetch"] == ["какая сейчас ключевая ставка"]
    assert "https://example.org/r" in captured["prompt"]
    assert "Search queries already executed" not in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_empty_search_loop_still_runs_the_plain_fetch(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "сколько стоит билет Минск Москва"
    seen = {"fetch": []}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen["fetch"].append(query)
        return [{"title": "Билет", "url": "https://example.org/t", "snippet": "от 900",
                 "publisher": "example.org"}]

    async def empty_turn(**kwargs):
        return _turn(queries=["цена билета Минск Москва"], snippets=[])

    captured = {}
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(lumen_agent, "run_search_turn", empty_turn)
    _run_core(message, monkeypatch, captured=captured)

    assert seen["fetch"] == ["сколько стоит билет Минск Москва"]
    assert "https://example.org/t" in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_kill_switch_returns_to_single_fetch(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "погоде сегодня в Минске верить?"
    # Without the Munito head, the loop would not start even without the switch: the key is required,
    # otherwise the assertion “the loop was not called” is trivial.
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    seen = {"loop": 0, "fetch": 0}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen["fetch"] += 1
        return [{"title": "Погода", "url": "https://example.org/w", "snippet": "+2",
                 "publisher": "example.org"}]

    async def fake_turn(**kwargs):
        seen["loop"] += 1
        return _turn(snippets=[{"url": "https://example.org/x"}])

    monkeypatch.setenv("AGENT_TURN_ENABLED", "0")
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    _run_core(message, monkeypatch)

    assert seen["loop"] == 0
    assert seen["fetch"] == 1
    bot.chat_state.pop(123, None)


def test_search_disabled_switch_skips_the_loop_too(rate_guard_setup, monkeypatch):
    # WEB_SEARCH_ENABLED=0 must disable the loop too: otherwise the model would keep calling
    # planning calls when external search is forbidden by the owner.
    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    seen = {"loop": 0}

    async def fake_turn(**kwargs):
        seen["loop"] += 1
        return _turn(snippets=[{"url": "https://example.org/x"}])

    monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    _run_core(message, monkeypatch)

    assert seen["loop"] == 0
    bot.chat_state.pop(123, None)


@pytest.mark.parametrize(("text", "expected"), [
    ("привет", "low"),
    ("спроектируй архитектуру бота и докажи выбор", "xhigh"),
    ("кто сейчас главный тренер сборной", "medium"),
])
def test_effort_of_the_message_reaches_the_route(rate_guard_setup, monkeypatch, text, expected):
    message = rate_guard_setup()
    message.text = text
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "")   # without a pin: verify automatic selection
    captured = {}
    _run_core(message, monkeypatch, captured=captured)
    assert captured["kwargs"]["effort"] == expected
    bot.chat_state.pop(123, None)


def test_effort_auto_off_passes_none_and_disables_the_loop(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "спроектируй архитектуру бота"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setenv("EFFORT_AUTO", "0")
    seen = {"loop": 0}

    async def fake_turn(**kwargs):
        seen["loop"] += 1
        return _turn(snippets=[{"url": "https://example.org/x"}])

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        raise AssertionError("без автовыбора поиск запускается прежним путём, а не циклом")

    captured = {}
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    _run_core(message, monkeypatch, captured=captured)
    # This is not a fresh question, so there is no search at all; the key points are effort=None and
    # the loop was not called: EFFORT_AUTO=0 must return the previous flat schema.
    assert captured["kwargs"]["effort"] is None
    assert seen["loop"] == 0
    bot.chat_state.pop(123, None)


def test_gemini_only_route_does_not_run_munito_loop(rate_guard_setup, monkeypatch):
    # Only Gemini reads links and YouTube: the planning Munito call has no way to
    # replace, and the loop has no right to substitute it.
    message = rate_guard_setup()
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    message.text = "расскажи про https://example.org/page что там нового"
    seen = {"loop": 0}

    async def fake_turn(**kwargs):
        seen["loop"] += 1
        return _turn(snippets=[{"url": "https://example.org/x"}])

    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    captured = {}
    _run_core(message, monkeypatch, captured=captured)
    assert seen["loop"] == 0
    assert all(provider == "gemini" for provider, _ in captured["route"])
    bot.chat_state.pop(123, None)


def test_search_marker_asks_the_loop_not_the_user_words(rate_guard_setup, monkeypatch):
    # Regression of the old mechanism: on the [[SEARCH]] marker, the bot searched the raw phrase,
    # that is, with the exact words for which the model had already said "I do not know." Now
    # the model's request enters the same loop, and the model writes the queries.
    from lumen_search import _SearchRequested

    message = rate_guard_setup()
    message.text = "когда вышла Fable 5.2"
    monkeypatch.setattr("lumen_message_core._wants_web_search", lambda text, *, previous="": False)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    seen = {"fetch": [], "loop": 0}
    prompts: list[str] = []

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        seen["fetch"].append(query)
        return [{"title": "Релиз", "url": "https://example.org/r", "snippet": "3 ноября 2026"}]

    async def fake_turn(**kwargs):
        seen["loop"] += 1
        return _turn(queries=["Fable 5.2 release date"],
                     snippets=[{"title": "Релиз", "url": "https://example.org/r",
                                "snippet": "3 ноября 2026", "publisher": "example.org"}])

    passes = {"n": 0}

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        prompts.append(ai_prompt)
        passes["n"] += 1
        if passes["n"] == 1:
            raise _SearchRequested(None)
        return "Fable 5.2 вышла 3 ноября 2026", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))

    assert passes["n"] == 2, "после просьбы поиска должен быть ровно один повтор"
    assert seen["loop"] == 1
    assert seen["fetch"] == []
    assert "Fable 5.2 release date" in prompts[1]
    assert "https://example.org/r" in prompts[1]
    assert "[[SEARCH]]" not in prompts[1]
    bot.chat_state.pop(123, None)

def test_search_loop_status_shows_query_then_one_closed_step(rate_guard_setup, monkeypatch):
    # Trace regression: two "searched · found N" lines in one bubble (review 12.11.2026).
    # A live stage shows the model request; a closed stage is one for the whole answer.
    from types import SimpleNamespace

    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    fake_status = _FakeStatus()

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        fake_status.started = (key, timed)
        return fake_status

    async def fake_turn(**kw):
        # The loop shows a live request and does NOT print the closed stage: once per
        # the caller's code closes the answer.
        await fake_status.set_phase("progress_search_query", query="тренер сборной Беларуси")
        return _turn(queries=["тренер сборной Беларуси"],
                     snippets=[{"url": f"https://src{i}.by/a", "publisher": f"src{i}.by"}
                               for i in range(3)])

    async def no_fetch(*a, **kw):
        raise AssertionError("резервный fetch не запускается, когда цикл принёс источники")

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", no_fetch)
    _run_core(message, monkeypatch)

    assert fake_status.started == ("progress_searching", False)
    assert {"query": "тренер сборной Беларуси"} in fake_status.phase_kwargs
    assert fake_status.done == [("progress_search_done", {"count": 3})], "закрытый этап должен быть один"
    assert fake_status.sources == ["src0.by", "src1.by", "src2.by"]
    bot.chat_state.pop(123, None)


def test_search_loop_failure_closes_the_step_once(rate_guard_setup, monkeypatch):
    # If the loop crashed, the closed stage must appear from the fallback branch: exactly
    # once and with the number of fallback search sources.
    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    fake_status = _FakeStatus()

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        fake_status.started = (key, timed)
        return fake_status

    async def boom(**kwargs):
        assert kwargs["status"] is fake_status
        await kwargs["status"].set_phase("progress_search_query", query=message.text)
        raise RuntimeError("planning exploded")

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "T", "url": "https://example.org/1", "publisher": "example.org"},
                {"title": "T2", "url": "https://example.org/2", "publisher": "demo.by"}]

    seen = {"fetch": []}

    async def spy_fetch(query, **kw):
        seen["fetch"].append(query)
        return await fake_fetch(query, **kw)

    captured = {}
    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(lumen_agent, "run_search_turn", boom)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", spy_fetch)
    _run_core(message, monkeypatch, captured=captured)

    assert seen["fetch"] == [message.text]
    assert "https://example.org/1" in captured["prompt"]
    assert fake_status.started == ("progress_searching", False)
    assert {"query": message.text} in fake_status.phase_kwargs
    assert fake_status.done == [("progress_search_done", {"count": 2})], fake_status.done
    bot.chat_state.pop(123, None)


def test_group_chat_gets_the_agent_status_bubble(rate_guard_setup, monkeypatch):
    # Owner decision 12.11.2026: complaints about weak search arrived as screenshots
    # from a group, so the live stage must also run where the chat is not private.
    message = rate_guard_setup()
    message.chat = SimpleNamespace(id=123, type=bot.ChatType.GROUP)
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    # The bot does not answer background group messages at all: addressee detection is needed,
    # otherwise the test would measure the early exit, not the bubble.
    monkeypatch.setattr(bot, "message_mentions_bot", lambda msg: True)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    fake_status = _FakeStatus()

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        fake_status.started = (key, timed)
        return fake_status

    async def fake_turn(**kwargs):
        await fake_status.set_phase("progress_search_query", query="тренер сборной")
        return _turn(queries=["тренер сборной"],
                     snippets=[{"url": "https://a.by/1", "publisher": "a.by"}])

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    captured = {}
    _run_core(message, monkeypatch, captured=captured)

    assert fake_status.started == ("progress_searching", False)
    assert {"query": "тренер сборной"} in fake_status.phase_kwargs
    assert captured["kwargs"]["status"] is fake_status
    bot.chat_state.pop(123, None)


def test_guest_does_not_get_the_bubble(rate_guard_setup, monkeypatch):
    # Silence for guests: their answer goes through answerGuestQuery, where no edits are made.
    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "is_guest_message", lambda msg: True)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    started: list[str] = []

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        started.append(key)
        return _FakeStatus()

    async def fake_turn(**kwargs):
        return _turn(snippets=[{"url": "https://a.by/1", "publisher": "a.by"}])

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    _run_core(message, monkeypatch)
    assert started == []
    bot.chat_state.pop(123, None)


def test_outbound_search_daily_budget_stops_everything(monkeypatch):
    # After the daily cap, the bot stops visiting sources and replies with an honest
    # marker; both the loop and the previous branch go through the same boundary.
    # Network access is mocked: the weakened gate previously called DNS and still stayed green,
    # because network errors also return [] (review 17.11.2026).
    import contextlib
    import lumen_search

    network_calls = []

    @contextlib.asynccontextmanager
    async def no_network():
        network_calls.append(1)
        raise AssertionError("сеть при исчерпанном бюджете")
        yield  # pragma: no cover

    monkeypatch.setattr(lumen_search, "_search_session", no_network)

    async def scenario():
        lumen_search.reset_search_counters_for_tests()
        try:
            for _ in range(lumen_search.WEB_SEARCH_DAILY_LIMIT):
                lumen_search.note_search_call()
            assert lumen_search.search_daily_budget_left() is False
            exhausted = await lumen_search.fetch_web_snippets("кто тренер сборной")
        finally:
            lumen_search.reset_search_counters_for_tests()
        assert lumen_search.search_daily_budget_left() is True
        return exhausted

    assert asyncio.run(scenario()) == []
    assert network_calls == []


def test_search_query_placeholder_exists_in_every_language():
    # The new status key substitutes {query}: a pack without the placeholder would show
    # "Searching the internet" without a request, and the table completeness test
    # will not catch it.
    from lumen_lang import SUPPORTED_LANGS, t

    for lang in SUPPORTED_LANGS:
        rendered = t(lang, "progress_search_query", query="курс рубля")
        assert "курс рубля" in rendered, lang
        assert "{query}" not in rendered, lang


def test_loop_metrics_reach_the_devtrace(rate_guard_setup, monkeypatch, tmp_path):
    # Offline branch analysis is possible only if the trace distinguishes "searched a loop" and
    # "looked for the old path": without these fields, the review could not distinguish one branch from
    # another even when DEV_TRACE is enabled.
    monkeypatch.setenv("DEV_TRACE", "1")
    monkeypatch.setenv("DEV_TRACE_PATH", str(tmp_path / "trace.jsonl"))
    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")

    async def fake_turn(**kwargs):
        result = _turn(queries=["тренер сборной Беларуси"],
                       snippets=[{"url": "https://a.by/1", "publisher": "a.by"},
                                 {"url": "https://a.by/2", "publisher": "a.by"}])
        result.steps = 1
        result.planning_ms = 700
        return result

    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    _run_core(message, monkeypatch)

    import lumen_devtrace

    entries = [e for e in lumen_devtrace.read_tail(10) if e.get("kind") == "answer"]
    assert entries, "след ответа обязан быть записан"
    flags = entries[-1]["flags"]
    assert flags["agent"] is True
    assert flags["agent_steps"] == 1
    assert flags["agent_planning_ms"] == 700
    assert flags["effort"] in ("medium", "high")
    bot.chat_state.pop(123, None)

def test_search_marker_after_evidence_retries_and_reports_one_step(rate_guard_setup, monkeypatch):
    # Regression 12.11.2026: the first pass already brought sources, yet the model still
    # answered [[SEARCH]]. mark_stats was bound only inside the 'sources
    # not yet", so the second pass fell into a service error, while the bubble received
    # two contradictory lines (“found 1” and “found nothing”).
    import lumen_search
    from types import SimpleNamespace

    message = rate_guard_setup()
    message.text = "когда вышла Fable 5.2?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    fake_status = _FakeStatus()
    prompts: list[str] = []
    passes = {"n": 0}

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        fake_status.started = (key, timed)
        return fake_status

    async def fake_turn(**kw):
        return _turn(queries=["Fable 5.2 релиз дата"], snippets=[
            {"title": "Релиз", "url": "https://example.org/r", "snippet": "3 ноября 2026",
             "publisher": "example.org"}])

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        prompts.append(ai_prompt)
        passes["n"] += 1
        if passes["n"] == 1:
            raise lumen_search._SearchRequested(None)
        return "Fable 5.2 вышла 3 ноября 2026", False

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))

    assert passes["n"] == 2, "ретрай после маркера обязан состояться"
    assert fake_status.done.count(("progress_search_done", {"count": 1})) == 1, fake_status.done
    assert ("progress_search_empty", {}) not in fake_status.done, fake_status.done
    assert "[[SEARCH]]" not in prompts[1]
    bot.chat_state.pop(123, None)


def test_exploding_status_object_does_not_cost_the_answer(rate_guard_setup, monkeypatch):
    # The bubble decorates the answer: editing the status must not leave the person
    # without text (review 12.11.2026: status calls in message_core omitted suppress).
    from types import SimpleNamespace

    class _Boom:
        message = object()

        def add_done(self, key, **kwargs):
            raise RuntimeError("status exploded")

        def set_sources(self, names):
            raise RuntimeError("status exploded")

        async def set_phase(self, key, **kwargs):
            raise RuntimeError("status exploded")

        async def set_action(self, text, **kwargs):
            raise RuntimeError("status exploded")

        async def mark_stage_done(self):
            raise RuntimeError("status exploded")

        async def delete(self):
            raise RuntimeError("status exploded")

        async def stop(self):
            raise RuntimeError("status exploded")

    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    passes = {"n": 0}

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        return _Boom()

    async def fake_turn(**kw):
        return _turn(queries=["тренер сборной Беларуси"], snippets=[
            {"title": "Тренер", "url": "https://example.org/a", "snippet": "назначен",
             "publisher": "example.org"}])

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        passes["n"] += 1
        assert "Тренер" in ai_prompt or "example.org" in ai_prompt, "источники обязаны доехать в промпте"
        return "постановили Штейнер", False

    monkeypatch.setattr("lumen_message_core.StatusMessage", SimpleNamespace(start=fake_start))
    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))

    assert passes["n"] == 1, "обработчик обязан дойти до ответа, а не упасть на статусе"
    bot.chat_state.pop(123, None)


def test_spent_daily_search_budget_does_not_spend_planning_calls(rate_guard_setup, monkeypatch):
    # Call cap exhausted: the loop still will not find anything, but it manages to
    # notice up to three planning model calls. Privacy review 12.11.2026.
    message = rate_guard_setup()
    message.text = "какая ключевая ставка ЦБ России сегодня?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr("lumen_message_core.search_daily_budget_left", lambda: 0)
    seen = {"loop": 0, "fetch": 0}

    async def fake_turn(**kw):
        seen["loop"] += 1
        return _turn(queries=["ключевая ставка ЦБ"])

    async def fake_fetch(query, **kw):
        seen["fetch"] += 1
        return []

    monkeypatch.setattr(lumen_agent, "run_search_turn", fake_turn)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    _run_core(message, monkeypatch)

    assert seen["loop"] == 0, "при нулевом бюджете планировать не за чем"
    # One fetch is mandatory (the old branch); a second is possible on a post-hook retry
    # freshness: the loop has nothing to do with it; zero planning calls matter.
    assert seen["fetch"] >= 1, "прежний один поиск остаётся и честно вернёт пустоту"
    bot.chat_state.pop(123, None)


def test_stop_during_evidence_skips_fallback_fetch(rate_guard_setup, monkeypatch):
    # Audit 16.11.2026: /stop during the search phase still triggered a reserve request
    # on the raw phrase (up to 6s and one counted search) before checking the flag.
    message = rate_guard_setup()
    message.text = "какая сейчас ключевая ставка"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    seen = {"fetch": 0}

    async def fake_fetch(*args, **kwargs):
        seen["fetch"] += 1
        return []

    async def empty_turn(**kwargs):
        # Stop arrives during planning: the area is already set (it is set by
        # the update wrapper), the flag is set inside the phase.
        bot._request_stream_stop(message.chat.id)
        return _turn()

    traces = []

    def fake_trace(chat_id, **kwargs):
        traces.append(kwargs.get("error") or "")

    monkeypatch.setattr("lumen_message_core.lumen_devtrace.record_failure", fake_trace)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(lumen_agent, "run_search_turn", empty_turn)
    bot._begin_stream_stop_scope(message.chat.id, None)
    try:
        _run_core(message, monkeypatch, captured={})
        assert seen["fetch"] == 0
        assert bot._stream_stop_requested(message.chat.id) is False
        assert traces and "stopped" in traces[0]
    finally:
        bot._end_stream_stop_scope(message.chat.id)
        bot.chat_state.pop(123, None)
        bot._STREAM_STOPS.pop(message.chat.id, None)


def test_gemini_reserve_loop_gets_phase_deadline(rate_guard_setup, monkeypatch):
    # Audit 16.11.2026: in the Gemini branch, deadline was None, and three loop steps
    # could reach 66 seconds instead of 45. The phase deadline is computed once.
    message = rate_guard_setup()
    message.text = "кто сейчас канцлер Германии?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "")
    import lumen_agent_gemini
    monkeypatch.setattr(lumen_agent_gemini, "gemini_agent_enabled", lambda: True)
    seen = {}

    async def fake_turn(**kwargs):
        seen["deadline"] = kwargs["deadline"]
        return _turn()

    async def no_fetch(*args, **kwargs):
        return []

    monkeypatch.setattr(lumen_agent_gemini, "run_search_turn", fake_turn)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", no_fetch)
    try:
        _run_core(message, monkeypatch, captured={})
        assert seen.get("deadline") is not None
    finally:
        bot.chat_state.pop(123, None)
