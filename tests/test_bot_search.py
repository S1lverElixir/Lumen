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
from types import SimpleNamespace
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


def test_fresh_query_with_live_gemini_still_gets_external_snippets(rate_guard_setup, monkeypatch):
    # Раньше сниппеты подставлялись только когда в маршруте не оставалось
    # grounding-моделей; теперь искать обязана любая ответившая модель, в том
    # числе резервная после 429 у Gemini (прод 28.10.2026).
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
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


class _FakeStatus:
    def __init__(self):
        self.message = object()
        self.done = []
        self.phases = []
        self.stopped = False
        self.deleted = False

    def add_done(self, key, **kwargs):
        self.done.append((key, kwargs))

    async def set_phase(self, key, *, timed=False):
        self.phases.append((key, timed))

    async def stop(self):
        self.stopped = True

    async def delete(self):
        self.deleted = True


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

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
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

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
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
    # Дневной лимит Gemini не отключает внешний поиск: сниппеты едут дешёвым
    # моделям маршрута, иначе они отвечают по памяти (задумка поиска — искать
    # умеет не только Gemini).
    message = rate_guard_setup()
    message.text = "погугли что происходит с ДЦ Яндекса"
    monkeypatch.setattr(bot, "_user_daily_gemini_exhausted", lambda uid, entry: True)
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
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
    monkeypatch.setattr(bot, "_user_daily_gemini_exhausted", lambda uid, entry: True)
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
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


def test_freshness_prompt_carries_today_date(rate_guard_setup, monkeypatch):
    # Контекст даты (eval 22.10.2026): модели без живого поиска не знали текущую
    # дату и строили «завтра» по догадке. Дата обязана уехать в промпт свежего
    # вопроса и соответствовать сегодняшнему дню в Москве — зашитая константа
    # или опечатка формата поймаются сравнением с живым now.
    message = rate_guard_setup()
    message.text = "какая сегодня погода в Москве"
    captured: dict = {}

    async def fake_fetch(query, *, timeout_sec=None, max_results=None):
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
