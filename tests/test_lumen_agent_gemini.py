"""Тесты цикла поиска на резервном Gemini: выключен по умолчанию, function calling, деградация."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bot
import lumen_agent_gemini


def _fake_client(replies):
    state = {"n": 0}

    async def generate_content(*, model, contents, config):
        assert config is not None
        idx = state["n"]
        state["n"] += 1
        if idx >= len(replies):
            raise AssertionError("лишний вызов модели")
        reply = replies[idx]
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    return client, state


async def _build_contents(chat_id, text, *, history_meta=None):
    return []


def test_gemini_loop_is_off_by_default(monkeypatch):
    # Форма function calling подтверждена SDK, но живого прогона не было: фича
    # включается переменной Space, по умолчанию вызовов нет.
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
    # Модель просит web_search — бот исполняет, доказательства возвращаются, второй
    # вызов завершает цикл. Заодно проверяется, что конфиг инструмента собирается
    # из реальных типов SDK.
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    replies = [
        SimpleNamespace(function_calls=[SimpleNamespace(args={"query": "курс доллара"})], text=""),
        SimpleNamespace(function_calls=[], text="готово"),
    ]
    client, state = _fake_client(replies)
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", _build_contents)

    async def fetch(text, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "T", "url": "https://e.com/x", "snippet": "s",
                 "publisher": "e.com", "date": "2026-11-16"}]

    monkeypatch.setattr(lumen_agent_gemini, "fetch_web_snippets", fetch)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="курс?", clean_prompt="курс?", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    # Бюджет поисков исчерпан после первого шага: цикл — производитель доказательств,
    # финальный ответ делает маршрут, отдельного текстового вызова здесь нет.
    assert state["n"] == 1
    assert result.queries == ["курс доллара"]
    assert result.searches == 1 and result.steps == 1
    assert result.snippets and result.snippets[0]["url"] == "https://e.com/x"


def test_gemini_loop_degrades_on_a_failed_call(monkeypatch):
    monkeypatch.setenv("GEMINI_AGENT_TURN_ENABLED", "1")
    client, _ = _fake_client([RuntimeError("boom")])
    monkeypatch.setattr(bot, "client", client)
    monkeypatch.setattr(bot, "_build_gemini_turn_contents", _build_contents)
    result = asyncio.run(lumen_agent_gemini.run_search_turn(
        chat_id=1, user_prompt="x", clean_prompt="x", model_id="gemini-3.8-flash",
        effort="medium", max_searches=1,
    ))
    assert result.failed is True and result.snippets == []
