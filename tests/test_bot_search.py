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
from datetime import datetime
from zoneinfo import ZoneInfo

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


def test_disabled_search_marks_reply_without_live_search(rate_guard_setup, monkeypatch):
    # Ревью: выключенный свитч при мёртвой квоте молча отвечал по памяти — пометка обязана быть.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    _kill_search_quota(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
        raise AssertionError("fetch must not run while disabled")

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert "without live web search" in sent_text
    bot.chat_state.pop(123, None)


def test_daily_limit_keeps_note_and_skips_external_search(rate_guard_setup, monkeypatch):
    # Граница: дневной лимит пользователя — честная пометка без чужого поиска
    # (лимит именно его, обходить поиском нельзя) и без единого сетевого вызова в тестах.
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    monkeypatch.setattr(bot, "_user_daily_gemini_exhausted", lambda uid, entry: True)

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
        raise AssertionError("fetch must not run on daily user limit")

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        assert all(p != "gemini" for p, _ in route)
        return "ok", False

    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", fake_fetch)
    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    sent_text = bot._safe_reply.await_args[0][1]
    assert sent_text.startswith("Answering without web search")
    bot.chat_state.pop(123, None)


def test_freshness_prompt_carries_today_date(rate_guard_setup, monkeypatch):
    # Контекст даты (eval 22.10.2026): модели без живого поиска не знали текущую
    # дату и строили «завтра» по догадке. Дата обязана уехать в промпт свежего
    # вопроса и соответствовать сегодняшнему дню в Москве — зашитая константа
    # или опечатка формата поймаются сравнением с живым now.
    message = rate_guard_setup()
    message.text = "какая сегодня погода в Москве"
    captured: dict = {}

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    prompt = captured["prompt"]
    assert "[Context: today is" in prompt
    assert "(Europe/Moscow)" in prompt
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    assert f"{now.day} {now.strftime('%B')} {now.year}" in prompt
    bot.chat_state.pop(123, None)


def test_non_freshness_prompt_has_no_date_context(rate_guard_setup, monkeypatch):
    # Вечные вопросы дату не получают: лишний контекст в обычном маршруте не нужен.
    message = rate_guard_setup()
    message.text = "объясни, как работает рекурсия"
    captured: dict = {}

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    assert "[Context: today is" not in captured["prompt"]
    bot.chat_state.pop(123, None)
