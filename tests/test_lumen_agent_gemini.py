"""Tests for the Gemini fallback search loop: disabled by default, function calling, degradation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bot
from google.genai import types
import lumen_agent_gemini
from lumen_agent import WEB_SEARCH_TOOL
from lumen_search import format_snippets_for_prompt, web_search_max_results


def _fake_client(replies, *, expected_model="gemini-3.8-flash"):
    # The fake must validate the complete request contract. Returning a canned reply
    # regardless of model/contents/config would pass while production sent bad state.
    state = {"n": 0, "calls": []}

    async def generate_content(*, model, contents, config):
        assert model == expected_model
        assert isinstance(contents, list)
        assert len(config.tools) == 1
        declaration = config.tools[0].function_declarations[0]
        assert declaration.name == WEB_SEARCH_TOOL["function"]["name"]
        assert declaration.parameters_json_schema == WEB_SEARCH_TOOL["function"]["parameters"]
        assert declaration.description == WEB_SEARCH_TOOL["function"]["description"]
        assert config.automatic_function_calling.disable is True
        state["calls"].append(SimpleNamespace(
            model=model,
            request_contents=list(contents),
            mutable_contents=contents,
            config=config,
        ))
        idx = state["n"]
        state["n"] += 1
        if idx >= len(replies):
            raise AssertionError("extra Gemini model call")
        reply = replies[idx]
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    return client, state


def _contents_builder():
    seed_text = "seed history"
    initial = [types.Content(role="user", parts=[types.Part.from_text(text=seed_text)])]
    records = []

    async def build(chat_id, text, *extra_parts, history_meta=None):
        contents = list(initial)
        records.append(SimpleNamespace(
            chat_id=chat_id,
            text=text,
            extra_parts=extra_parts,
            history_meta=history_meta,
            contents=contents,
        ))
        return contents

    return build, records, initial[0]


def test_gemini_loop_is_off_by_default(monkeypatch):
    # The function-calling shape is confirmed by the SDK, but there was no live run: the feature
    # is enabled by a Space variable; there are no calls by default.
    monkeypatch.delenv("GEMINI_AGENT_TURN_ENABLED", raising=False)
    state = {"n": 0}

    async def generate_content(**kwargs):
        state["n"] += 1
        return SimpleNamespace(function_calls=[], text="")

    monkeypatch.setattr(bot, "client", SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))))
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    assert state["n"] == 0
    assert result.snippets == [] and result.searches == 0


def test_gemini_loop_executes_a_search_and_returns_evidence(monkeypatch):
    # The loop must send the real model, built history and web_search config, then
    # execute the function call and return evidence.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [
        SimpleNamespace(function_calls=[SimpleNamespace(args={"query": "курс доллара"})], text=""),
        SimpleNamespace(function_calls=[], text="unused after the search budget"),
    ]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        # The model query goes to the search as-is; the answer-side context must be
        # the cleaned prompt, and the search keeps its own budget knobs.
        assert text == "курс доллара"
        assert context == "курс доллара сегодня"
        assert max_results == web_search_max_results()
        assert 0.0 < timeout_sec
        return [{"title": "T", "url": "https://e.com/x", "snippet": "s",
                 "publisher": "e.com", "date": "2026-11-16"}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="а какой сейчас курс доллара?", clean_prompt="курс доллара сегодня",
        model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    request = state["calls"][0]
    assert state["n"] == 1
    assert builds[0].chat_id == 1
    assert builds[0].text == "а какой сейчас курс доллара?"
    assert builds[0].extra_parts == ()
    assert builds[0].history_meta == {"attach_past": False}
    assert request.model == "gemini-3.8-flash"
    assert request.mutable_contents is builds[0].contents
    assert len(request.request_contents) == 1
    assert request.request_contents[0] is seed
    assert request.request_contents[0].parts[0].text == "seed history"
    assert result.queries == ["курс доллара"]
    assert result.searched is True
    assert result.failed is False
    assert result.searches == 1 and result.steps == 1
    assert result.snippets and result.snippets[0]["url"] == "https://e.com/x"


def test_gemini_second_planning_call_carries_the_tool_history(monkeypatch):
    # A follow-up model call must see the first function call and its result; otherwise
    # the agent loop refines a query from an empty conversation.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [
        SimpleNamespace(function_calls=[SimpleNamespace(args={"query": "курс доллара"})], text=""),
        SimpleNamespace(function_calls=[SimpleNamespace(args={"query": "цена нефти"})], text=""),
    ]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": f"result for {text}", "url": f"https://e.com/{text}", "snippet": text}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="рынки", clean_prompt="рынки", model_id="gemini-3.8-flash",
        effort="medium", max_searches=2,
    ))
    first, second = state["calls"]
    history = builds[0].contents
    assert state["n"] == 2
    assert first.mutable_contents is second.mutable_contents is history
    assert len(first.request_contents) == 1
    assert [content.role for content in second.request_contents] == ["user", "model", "user"]
    assert second.request_contents[0] is seed

    first_call = second.request_contents[1].parts[0].function_call
    assert first_call.name == "web_search"
    assert first_call.args == {"query": "курс доллара"}
    first_response = second.request_contents[2].parts[0].function_response
    assert first_response.name == "web_search"
    # Same framed block as production formats: a raw str(snippets) would let an
    # injected snippet close the frame and smuggle invisible characters.
    assert first_response.response["results"] == format_snippets_for_prompt(
        [{"title": "result for курс доллара", "url": "https://e.com/курс доллара",
          "snippet": "курс доллара"}]
    )

    assert len(history) == 5
    assert [content.role for content in history[3:]] == ["model", "user"]
    assert history[3].parts[0].function_call.args == {"query": "цена нефти"}
    assert result.queries == ["курс доллара", "цена нефти"]
    assert result.searches == 2 and result.steps == 2
    assert {snippet["url"] for snippet in result.snippets} == {
        "https://e.com/курс доллара", "https://e.com/цена нефти"
    }


def test_gemini_empty_call_does_not_consume_the_search_budget(monkeypatch):
    # Planning is cleaned before the budget slice: slicing first would leave only
    # the empty call, report a failure and answer with no evidence (review 17.11.2026).
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [SimpleNamespace(function_calls=[
        SimpleNamespace(args={"query": "   "}),
        SimpleNamespace(args={"query": "курс доллара"}),
    ], text="")]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        assert text == "курс доллара"
        return [{"title": "T", "url": "https://e.com/x", "snippet": "s"}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    assert result.failed is False
    assert result.queries == ["курс доллара"]
    assert result.searches == 1
    assert state["n"] == 1


def test_gemini_duplicate_query_is_searched_once(monkeypatch):
    # A repeated query is collapsed: without dedup the same fetch burned two slots
    # of the per-person search budget and the loop stopped early.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [
        SimpleNamespace(function_calls=[
            SimpleNamespace(args={"query": "курс доллара"}),
            SimpleNamespace(args={"query": "курс доллара"}),
        ], text=""),
        SimpleNamespace(function_calls=[], text=""),
    ]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    calls: list[str] = []

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        calls.append(text)
        return [{"title": text, "url": "https://e.com/x", "snippet": "s"}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=2,
    ))
    assert calls == ["курс доллара"]
    assert result.searches == 1
    # The duplicate did not eat the second slot, so one more planning call ran.
    assert state["n"] == 2


def test_gemini_deduplicates_before_budget_slice(monkeypatch):
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [SimpleNamespace(function_calls=[
        SimpleNamespace(args={"query": "курс доллара"}),
        SimpleNamespace(args={"query": "курс доллара"}),
        SimpleNamespace(args={"query": "цена нефти"}),
    ], text=""), SimpleNamespace(function_calls=[], text="")]
    client, state = _fake_client(replies)
    build, _builds, _seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    calls: list[str] = []

    async def fetch(text, **kwargs):
        calls.append(text)
        return []

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=2,
    ))
    assert calls == ["курс доллара", "цена нефти"]
    assert result.searches == 2


def test_gemini_stop_after_planning_skips_search(monkeypatch):
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    stopped = {"now": False}
    state = {"n": 0}

    async def generate_content(**kwargs):
        state["n"] += 1
        stopped["now"] = True
        return SimpleNamespace(function_calls=[SimpleNamespace(args={"query": "курс"})], text="")

    build, _builds, _seed = _contents_builder()
    monkeypatch.setattr(bot, "client", SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))))
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    monkeypatch.setattr(bot, "_stream_stop_requested", lambda chat_id: stopped["now"])
    calls: list[str] = []

    async def fetch(text, **kwargs):
        calls.append(text)
        return []

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    assert result.failed is True
    assert result.searches == 0
    assert calls == []
    assert state["n"] == 1


def test_gemini_unknown_tool_name_never_executes_as_search(monkeypatch):
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [SimpleNamespace(function_calls=[
        SimpleNamespace(name="delete_everything", args={"query": "bad"}),
        SimpleNamespace(name="web_search", args={"query": "valid"}),
    ], text=""), SimpleNamespace(function_calls=[], text="")]
    client, _state = _fake_client(replies)
    build, _builds, _seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    calls: list[str] = []

    async def fetch(text, **kwargs):
        calls.append(text)
        return []

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=2,
    ))
    assert calls == ["valid"]
    assert result.searches == 1


def test_gemini_status_preamble_and_user_stop(monkeypatch):
    # The planning preamble reaches the live status bubble once; a stop pressed during
    # a search must end the loop before the next planning call, degrading honestly.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [
        SimpleNamespace(function_calls=[SimpleNamespace(args={"query": "курс доллара"})],
                        text="Сейчас поищу актуальный курс."),
        SimpleNamespace(function_calls=[], text="unused after the stop"),
    ]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    stopped = {"now": False}
    monkeypatch.setattr(bot, "_stream_stop_requested", lambda chat_id: stopped["now"])

    class _Status:
        def __init__(self):
            self.actions = []
            self.phases = []

        async def set_action(self, text, *, timed=False):
            self.actions.append((text, timed))

        async def set_phase(self, key, **kw):
            self.phases.append((key, kw))

    status = _Status()

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        stopped["now"] = True  # the user pressed stop while this search ran
        return [{"title": text, "url": "https://e.com/x", "snippet": "s"}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=2, status=status,
    ))
    assert result.searches == 1
    assert state["n"] == 1
    assert result.failed is True
    assert len(result.preambles) == 1
    assert status.actions == [(result.preambles[0], False)]
    assert status.phases == [("progress_search_query", {"query": "курс доллара"})]


def test_gemini_expired_deadline_skips_model_calls(monkeypatch):
    # With no usable budget left the loop must not spend a planning call at all:
    # the whole message deadline is already gone.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [SimpleNamespace(function_calls=[], text="")]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1, deadline=0.0,
    ))
    assert result.failed is True
    assert state["n"] == 0
    assert result.snippets == []


def test_gemini_contents_failure_degrades_before_model_calls(monkeypatch):
    # History assembly can fail (state outage); that must degrade to a plain answer,
    # never raise into the reply flow and never reach the model.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    client, state = _fake_client([])

    async def broken_build(*args, **kwargs):
        raise RuntimeError("state unavailable")

    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", broken_build)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    assert result.failed is True
    assert state["n"] == 0
    assert result.snippets == []


def test_gemini_loop_stops_at_the_step_cap(monkeypatch):
    # Rounds are capped independently of the search budget: each planning call costs
    # the message deadline, so the loop must end at _MAX_STEPS.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [
        SimpleNamespace(function_calls=[SimpleNamespace(args={"query": f"запрос {i}"})], text="")
        for i in range(1, 5)
    ]
    client, state = _fake_client(replies)
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": text, "url": f"https://e.com/{text}", "snippet": text}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=4,
    ))
    assert state["n"] == lumen_agent_gemini._MAX_STEPS
    assert result.steps == lumen_agent_gemini._MAX_STEPS
    assert result.searches == lumen_agent_gemini._MAX_STEPS
    assert result.failed is False


def test_gemini_loop_degrades_on_a_failed_call(monkeypatch):
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    client, state = _fake_client([RuntimeError("boom")])
    build, builds, seed = _contents_builder()
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", build)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    request = state["calls"][0]
    assert state["n"] == 1
    assert builds[0].history_meta == {"attach_past": False}
    assert request.model == "gemini-3.8-flash"
    assert request.mutable_contents is builds[0].contents
    assert request.request_contents[0] is seed
    assert result.failed is True
    assert result.snippets == []
    assert result.searches == 0
