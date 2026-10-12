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

import bot
from tests.bot_test_helpers import _run_core_capturing_prompt

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

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
        assert "ДЦ" in query
        return [{"title": "Новость", "url": "https://example.com/n", "snippet": "Свежие данные"}]

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    prompt = _run_core_capturing_prompt(message, monkeypatch)
    assert "Web search snippets" in prompt
    assert "https://example.com/n" in prompt
    bot.chat_state.pop(123, None)


def test_empty_fallback_search_marks_reply_without_live_search(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    _kill_search_quota(monkeypatch)

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
        return []

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert "without live web search" in sent_text
    bot.chat_state.pop(123, None)
