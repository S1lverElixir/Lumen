"""
test_bot_agent_turn.py — сквозные тесты агентного хода в _handle_message_core.

Что защищают: с 12.11.2026 свежие источники собирает цикл, где запросы пишет сама
модель (lumen_agent.run_search_turn), а прежнее поведение — один внешний поиск по
сырой фразе человека — остаётся резервом. Тесты ниже проверяют ровно те грани,
где новая надстройка может или потерять поиск вовсе (обрыв цикла), или сделать
лишнюю работу (выключатель, отсутствие источников), или соврать про усилие.

Запуск:
    pytest tests/test_bot_agent_turn.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

import bot
import lumen_agent
from tests.bot_test_helpers import _FakeStatus


def _run_core(message, monkeypatch, *, captured=None):
    """Прогон _handle_message_core с захватом и промпта, и kwargs маршрута."""

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        if captured is not None:
            captured["prompt"] = ai_prompt
            captured["route"] = route
            captured["kwargs"] = kwargs
            # Даём шанс фоновым задачам (typing-индикатор) добежать до конца.
            await asyncio.sleep(0)
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
    # Голова текстового маршрута live — Munito: без ключа роутер отдаёт весь текст
    # Gemini, а цикл планирования живёт именно у Munito.
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
    # Резервный поиск по сырой фразе не запускается, когда цикл уже принёс источники:
    # второй fetch на том же сообщении стоил бы ещё один сетевой круг впустую.
    assert seen["fetch"] == []
    assert "главный тренер сборной Беларуси 2026" in captured["prompt"]
    assert "https://example.org/a" in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_broken_search_loop_falls_back_to_plain_fetch(rate_guard_setup, monkeypatch):
    # Регрессия, которую нельзя допустить: падение надстройки лишает человека поиска
    # вовсе (пометка «без живого поиска» на вопросе, где источники есть).
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
    # Без головы Munito цикл не запустился бы и без выключателя: ключ обязателен,
    # иначе assert «цикл не звали» тривиален.
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
    # WEB_SEARCH_ENABLED=0 обязан глушить и цикл: иначе модель продолжала бы дёргать
    # планирующие вызовы, когда внешний поиск запрещён владельцем.
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
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "")   # без пина: проверяем автовыбор
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
    # Это не свежий вопрос, поэтому поиска нет вовсе; главное — effort=None и
    # цикл не звался: EFFORT_AUTO=0 обязан возвращать прежнюю плоскую схему.
    assert captured["kwargs"]["effort"] is None
    assert seen["loop"] == 0
    bot.chat_state.pop(123, None)


def test_gemini_only_route_does_not_run_munito_loop(rate_guard_setup, monkeypatch):
    # Ссылку и YouTube читает только Gemini: планирующий вызов Munito там нечем
    # заменить, и цикл не имеет права его подставлять.
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
    # Регрессия старого механизма: на маркер [[SEARCH]] бот искал сырой фразой,
    # то есть ровно теми словами, на которых модель уже сказала «не знаю». Теперь
    # просьба модели уходит в тот же цикл, и запросы пишет она.
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
        await asyncio.sleep(0)
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
    # Регрессия трейса: два «поискал · нашёл N» в одном пузыре (ревью 12.11.2026).
    # Живой этап показывает запрос модели, закрытый этап — один на весь ответ.
    from types import SimpleNamespace

    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    fake_status = _FakeStatus()

    async def fake_start(reply_to, lang, key, *, timed=False, **kwargs):
        fake_status.started = (key, timed)
        return fake_status

    async def fake_turn(**kw):
        # Цикл показывает живой запрос и НЕ печатает закрытый этап: один раз на
        # ответ его закрывает вызывающий код.
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
    # Если цикл упал, закрытый этап обязан появиться из резервной ветки — ровно
    # один раз и с числом источников резервного поиска.
    message = rate_guard_setup()
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")

    async def boom(**kwargs):
        raise RuntimeError("planning exploded")

    async def fake_fetch(query, *, timeout_sec=None, max_results=None, context=""):
        return [{"title": "T", "url": "https://example.org/1", "publisher": "example.org"},
                {"title": "T2", "url": "https://example.org/2", "publisher": "demo.by"}]

    seen = {"fetch": []}

    async def spy_fetch(query, **kw):
        seen["fetch"].append(query)
        return await fake_fetch(query, **kw)

    captured = {}
    monkeypatch.setattr(lumen_agent, "run_search_turn", boom)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", spy_fetch)
    _run_core(message, monkeypatch, captured=captured)

    assert seen["fetch"] == [message.text]
    assert "https://example.org/1" in captured["prompt"]
    bot.chat_state.pop(123, None)


def test_group_chat_gets_the_agent_status_bubble(rate_guard_setup, monkeypatch):
    # Решение владельца 12.11.2026: претензии по слабому поиску пришли скриншотами
    # из группы, поэтому живой этап обязан быть и там, где чат не личный.
    message = rate_guard_setup()
    message.chat = SimpleNamespace(id=123, type=bot.ChatType.GROUP)
    message.text = "кто сейчас главный тренер сборной Беларуси?"
    # Фоновое сообщение группы бот не отвечает вовсе: адресованность нужна,
    # иначе тест мерял бы ранний выход, а не пузырь.
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
    # Гостям тишина: их ответ уходит через answerGuestQuery, где правок не делают.
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


def test_outbound_search_daily_budget_stops_everything():
    # После дневного потолка бот перестаёт ходить в источники и отвечает с честной
    # пометкой; и цикл, и прежняя ветка идут через ту же границу.
    import lumen_search

    async def scenario():
        lumen_search.reset_search_counters_for_tests()
        for _ in range(lumen_search.WEB_SEARCH_DAILY_LIMIT):
            lumen_search.note_search_call()
        assert lumen_search.search_daily_budget_left() is False
        exhausted = await lumen_search.fetch_web_snippets("кто тренер сборной")
        lumen_search.reset_search_counters_for_tests()
        assert lumen_search.search_daily_budget_left() is True
        return exhausted

    assert asyncio.run(scenario()) == []


def test_search_query_placeholder_exists_in_every_language():
    # Новый ключ статуса подставляет {query}: пакет без плейсхолдера показал бы
    # «Ищу в интернете» без запроса, и тест полноты таблиц этого не поймает.
    from lumen_lang import SUPPORTED_LANGS, t

    for lang in SUPPORTED_LANGS:
        rendered = t(lang, "progress_search_query", query="курс рубля")
        assert "курс рубля" in rendered, lang
        assert "{query}" not in rendered, lang
