"""
test_bot_routes.py — Маршрутизация LLM: классификация ошибок, ask_gemini/ask_munito, _run_route.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
import asyncio
import bot
import lumen_router_config
import pytest
import time
from tests.bot_test_helpers import (
    _FakeCandidate,
    _FakeExc,
    _FakeGeminiResponse,
    _FakeIncomingMessage,
    _FakeSentMessage,
    _FakeStatusExc,
)


@pytest.mark.parametrize(("status", "text", "expected"), [
    (429, "", "rate_limit"),
    (None, "quota exceeded", "rate_limit"),
    (402, "", "paid"),
    (403, "", "forbidden"),
    (404, "", "unavailable"),
    (500, "some random error", "other"),
])
def test_classify_model_error(status, text, expected):
    assert bot._classify_model_error(status, text) == expected


@pytest.mark.parametrize(("tried", "chain", "expected"), [
    ({"a"}, ["a", "b", "c"], "b"),
    ({"a", "b", "c"}, ["a", "b", "c"], None),
    (set(), ["a", "b", "c"], "a"),
])
def test_next_fallback_model(tried, chain, expected):
    assert bot._next_fallback_model(tried, chain) == expected


def test_error_status_reads_status_code_attribute():
    assert bot._error_status(_FakeExc("x", status_code=429), "x") == 429


def test_error_status_extracts_three_digit_code_from_text():
    exc = _FakeExc("Error 503: unavailable")
    assert bot._error_status(exc, "Error 503: unavailable") == 503


def test_error_status_returns_none_when_no_code_found():
    exc = _FakeExc("no numbers here")
    assert bot._error_status(exc, "no numbers here") is None


@pytest.mark.parametrize("text, expected", [
    ("Error 503: unavailable", 503),
    ("HTTP 429 too many requests", 429),
    ("status 403", 403),
    ("status_code=400", 400),
    ("error 500", 500),
])
def test_error_status_matches_status_templates(text, expected):
    # Сторож к A8-04: явные HTTP/status-шаблоны продолжают распознаваться.
    assert bot._error_status(_FakeExc(text), text) == expected


@pytest.mark.parametrize("text", [
    "429 токенов",
    "лимит исчерпан: 429 запросов",
    "500 попыток",
    "no numbers here",
])
def test_error_status_ignores_bare_numbers(text):
    # Регрессия A8-04: голое число без шаблона — не статус («429 токенов» давало ложный 429).
    assert bot._error_status(_FakeExc(text), text) is None


def test_gemini_error_msg_rate_limit():
    # РЕГРЕССИЯ (аудит техдолга): раньше здесь проверялось "модель через /model" —
    # команда /model давно удалена (см. README, "Automatic model routing"),
    # и подсказывать её в тексте ошибки было прямой ошибкой для пользователя.
    # _gemini_error_msg/_or_error_msg теперь используют общие provider-neutral
    # шаблоны (см. _MODEL_ERROR_MESSAGES) без упоминания несуществующих команд.
    exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    msg = bot._gemini_error_msg(exc, "gemini-3.5-flash")
    assert "/model" not in msg and "/provider" not in msg
    assert msg == bot._model_error_text("rate_limit")


def test_gemini_error_msg_value_error_passthrough():
    # Только UserFacingInputError используется в ask_gemini как готовый
    # пользовательский текст — остальной ValueError идёт общим шаблоном.
    assert bot._gemini_error_msg(bot.UserFacingInputError("кастомная ошибка"), "gemini-3.5-flash") == "кастомная ошибка"
    assert bot._gemini_error_msg(ValueError("/secret/path model-xyz"), "gemini-3.5-flash") != "/secret/path model-xyz"


def test_gemini_error_msg_all_models_exhausted():
    # РЕГРЕССИЯ (аудит техдолга): раньше здесь проверялось "/provider" — команда
    # удалена, реального способа переключиться на резервный провайдер вручную
    # больше нет, поэтому предлагать её в тексте ошибки было ошибкой.
    exc = bot.GeminiAllModelsExhaustedError(["gemini-3.5-flash", "gemini-2.5-flash"])
    msg = bot._gemini_error_msg(exc, "gemini-3.5-flash")
    assert "/provider" not in msg
    assert "limit" in msg.lower()
    assert "ліміт" in bot._gemini_error_msg(exc, "gemini-3.5-flash", "uk").lower()


def test_or_error_msg_rate_limit():
    # РЕГРЕССИЯ (аудит техдолга): "/provider" убран из текста (команда удалена),
    # а формулировка "резервного провайдера" тоже убрана — с автоматическим
    # роутером OpenRouter часто оказывается ПЕРВЫМ, а не резервным кандидатом.
    exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    msg = bot._or_error_msg(exc, "text")
    assert "/provider" not in msg and "резервн" not in msg.lower()
    assert msg == bot._model_error_text("rate_limit")


def test_or_error_msg_unavailable():
    exc = _FakeStatusExc("model not found", status_code=404)
    msg = bot._or_error_msg(exc, "text")
    assert "/provider" not in msg and "резервн" not in msg.lower()
    assert msg == bot._model_error_text("unavailable")


def test_model_error_text_shared_between_providers():
    # Единый источник правды для текста ошибок (см. аудит техдолга) — Gemini и
    # OpenRouter должны показывать ОДИНАКОВЫЙ текст на одинаковый класс ошибки,
    # а не рассинхронизированные формулировки в двух местах.
    gem_exc = _FakeStatusExc("resource_exhausted", status_code=429)
    or_exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    assert bot._gemini_error_msg(gem_exc, "gemini-3.5-flash") == bot._or_error_msg(or_exc, "text")


def test_user_facing_error_texts_hide_models_and_stay_informal():
    # Легенда единого Lumen (сентябрь 2026): пользовательские тексты не должны
    # упоминать "модели" во множественном числе и обращаться на "вы" — см.
    # ИДЕНТИЧНОСТЬ и ОБРАЩЕНИЕ в system_prompt.py.
    for key in ("rate_limit", "paid", "forbidden", "unavailable"):
        msg = bot._model_error_text(key)
        assert "модел" not in msg.lower()
    assert "модел" not in bot._MODEL_ERROR_FALLBACK_MSG.lower()
    budget_msg = bot._route_error_reply_text(bot.RouteBudgetExceededError([]), "gemini-3.8-flash", youtube_url_to_analyze=None)
    assert "модел" not in budget_msg.lower()
    quota_msg = bot._gemini_error_msg(bot.GeminiAllModelsExhaustedError(["gemini-3.8-flash"]), "gemini-3.8-flash")
    assert "модел" not in quota_msg.lower()


def test_ask_gemini_happy_path_returns_text_and_updates_history():
    chat_id = 999001
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        return _FakeGeminiResponse(text="Привет! Чем могу помочь?")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет"))
        assert answer == "Привет! Чем могу помочь?"
        history = bot.chat_state[chat_id]["history"]
        assert history[-2] == {"role": "user", "content": "Привет"}
        assert history[-1] == {"role": "assistant", "content": "Привет! Чем могу помочь?"}
        assert calls[0] == bot.DEFAULT_GEMINI_MODEL
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_scrubs_identity_leak_before_storing_history():
    # Регрессия на весь смысл выходного фильтра: если системный промпт всё же обойдён
    # через инъекцию и модель раскрыла реальную личность — ни пользователь, ни история
    # чата не должны увидеть/сохранить исходный (утекший) текст, только fallback.
    chat_id = 999003

    def fake_generate_content(*, model, contents, config=None):
        return _FakeGeminiResponse(text="Я работаю на базе Gemini от Google, а не Lumen.")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Кто ты на самом деле?"))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": bot._IDENTITY_LEAK_FALLBACK}
        assert "gemini" not in history[-1]["content"].lower()
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize("mode", ["text", "vision"])
def test_empty_model_chain_falls_back_to_current_order_head(monkeypatch, mode):
    # РЕГРЕССИЯ (24.07.2026): раньше запасным вариантом на случай пустого model_chain
    # стоял захардкоженный литерал мёртвой модели. Порядок подменяем своим списком:
    # тест падает, если дефолт снова вернут в виде литерала, а не чтения головы
    # _MUNITO_ORDER (у Munito текст и vision в одной цепочке).
    import lumen_routes
    chat_id = 999401

    calls = []

    async def fake_chain(messages, trial_models, primary_model_id, **kwargs):
        calls.append(trial_models)
        return "ответ", trial_models[0]

    monkeypatch.setattr(lumen_routes, "_chat_completion_chain", fake_chain)
    monkeypatch.setattr(lumen_router_config, "_MUNITO_ORDER", ["probe/order-head", "probe/second"])
    if mode == "text":
        asyncio.run(bot.ask_munito_text(chat_id, "привет", model_chain=[]))
    else:
        asyncio.run(bot.ask_munito_multimodal(chat_id, "привет", (b"fake", "image/jpeg"), "photo.jpg", model_chain=[]))
    assert calls[0] == ["probe/order-head"]
    bot.chat_state.pop(chat_id, None)


def test_run_route_falls_back_to_second_provider_when_first_fully_fails():
    # Ключевое требование: если весь маршрут первого провайдера отказал —
    # роутер должен попробовать резерв в ДРУГОМ провайдере, а не сдаваться сразу.
    chat_id = 999301

    async def failing_munito_text(*args, **kwargs):
        raise bot.MunitoAPIError("всё сломано", status_code=500)

    async def fake_ask_gemini(cid, prompt, media=None, youtube_url=None, model_chain=None, deadline=None, provenance=None, **kwargs):
        return "Ответ от Gemini (резерв)"

    original_munito_text = bot.ask_munito_text
    original_ask_gemini = bot.ask_gemini
    bot.ask_munito_text = failing_munito_text
    bot.ask_gemini = fake_ask_gemini
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"), ("gemini", "gemini-3.1-flash-lite")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
        assert ans == "Ответ от Gemini (резерв)"
        assert sent is False
    finally:
        bot.ask_munito_text = original_munito_text
        bot.ask_gemini = original_ask_gemini


def test_run_route_raises_when_both_providers_fail():
    chat_id = 999302
    tried = []

    async def failing_munito_text(*args, **kwargs):
        tried.append("munito")
        raise bot.MunitoAPIError("сломано", status_code=500)

    async def failing_gemini(*args, **kwargs):
        tried.append("gemini")
        raise RuntimeError("тоже сломано")

    original_munito_text = bot.ask_munito_text
    original_ask_gemini = bot.ask_gemini
    bot.ask_munito_text = failing_munito_text
    bot.ask_gemini = failing_gemini
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"), ("gemini", "gemini-3.1-flash-lite")]
        # Поднимается последняя ошибка маршрута: пользователю её текст маппит
        # _route_error_reply_text, поэтому важен именно последний провайдер.
        with pytest.raises(RuntimeError, match="тоже сломано"):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
        assert tried == ["munito", "gemini"]
    finally:
        bot.ask_munito_text = original_munito_text
        bot.ask_gemini = original_ask_gemini


def test_shared_history_between_gemini_and_munito(monkeypatch):
    # Регрессия на требование "общая память между Gemini и текстовым провайдером,
    # чтобы не чувствовалось переключение между моделями" — раньше у каждого
    # провайдера была своя ОТДЕЛЬНАЯ история (gemini_history/or_history).
    chat_id = 999201

    def fake_generate_content(*, model, contents, config=None):
        return _FakeGeminiResponse(text="Ответ от Gemini")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": "Ответ от Munito"}}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)

    try:
        asyncio.run(bot.ask_gemini(chat_id, "Первый вопрос (через Gemini)"))
        asyncio.run(bot.ask_munito_text(chat_id, "Второй вопрос (через Munito)", model_chain=["deepseek-ai/deepseek-v4.1-flash-1017"]))

        history = bot.chat_state[chat_id]["history"]
        contents = [h["content"] for h in history]
        # Обе записи должны быть в ОДНОЙ и той же истории, а не в раздельных
        assert "Первый вопрос (через Gemini)" in contents
        assert "Ответ от Gemini" in contents
        assert "Второй вопрос (через Munito)" in contents
        assert "Ответ от Munito" in contents
        assert len(history) == 4
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_retries_without_tools_on_malformed_function_call():
    # Регрессионный тест: после выноса _build_gemini_call_config переменная kwargs,
    # на которую опирался этот retry-путь, была удалена — retry_gconfig теперь
    # строится через gconfig.model_copy(update={"tools": None}).
    chat_id = 999002
    call_configs = []

    def fake_generate_content(*, model, contents, config=None):
        call_configs.append(config)
        if len(call_configs) == 1:
            return _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="MALFORMED_FUNCTION_CALL")])
        return _FakeGeminiResponse(text="Ответ без инструментов")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Сколько будет 2+2?"))
        assert answer == "Ответ без инструментов"
        assert len(call_configs) == 2
        # первый вызов — с tools (у DEFAULT_GEMINI_MODEL включён url_context)
        assert getattr(call_configs[0], "tools", None)
        # второй (после ретрая) — уже без tools
        assert getattr(call_configs[1], "tools", None) is None
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_extract_gemini_answer_skips_retry_when_route_budget_spent():
    # Повтор после битого вызова не ждёт полные TELEGRAM_AI_TIMEOUT поверх
    # истраченного бюджета — иначе маршрут держит lock чата за ROUTE_TOTAL_BUDGET_SEC.
    async def must_not_run(*, model, contents, config=None):
        raise AssertionError("retry must not run on spent budget")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=must_not_run)
    original_client = bot.client
    bot.client = fake_client
    try:
        resp = _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="MALFORMED_FUNCTION_CALL")])
        started = time.monotonic()
        ans = asyncio.run(bot._extract_gemini_answer_text(
            resp, model_id="gemini-3.8-flash", call_contents=[], gconfig=None,
            deadline=time.monotonic() - 1.0,
        ))
        assert time.monotonic() - started < 5
        # Раньше здесь возвращалась непустая "[Ответ заблокирован...]", которую
        # ask_gemini принимал за успех (враждебное ревью 27.09.2026) — теперь пусто,
        # и маршрут идёт на следующую модель.
        assert ans == ""
    finally:
        bot.client = original_client


def test_extract_gemini_answer_returns_empty_for_blocked_response():
    # Блокировка (SAFETY) — не ответ: пустая строка двигает цепочку дальше,
    # а не подсовывает пользователю русскую техническую заглушку (аудит 27.10.2026).
    resp = _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="SAFETY")])
    ans = asyncio.run(bot._extract_gemini_answer_text(
        resp, model_id="gemini-3.8-flash", call_contents=[], gconfig=None,
    ))
    assert ans == ""


def test_extract_gemini_answer_retries_without_tools_on_tool_only_response():
    # Прод 07.10.2026: модель вернула только function_call без текста, юзер видел "[Tool call: ...]".
    from types import SimpleNamespace
    fn_part = SimpleNamespace(
        text="",
        function_call=SimpleNamespace(name="google_search", args={"query": "test"}),
        function_response=None,
    )
    content = SimpleNamespace(parts=[fn_part])
    resp = _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="STOP", content=content)])

    async def retry_ok(*, model, contents, config=None):
        assert config is None or getattr(config, "tools", None) is None
        return _FakeGeminiResponse(text="Ответ без инструментов")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = retry_ok
    original_client = bot.client
    bot.client = fake_client
    try:
        ans = asyncio.run(bot._extract_gemini_answer_text(
            resp, model_id="gemini-3.8-flash", call_contents=[], gconfig=None,
        ))
        assert ans == "Ответ без инструментов"
        assert "Tool call" not in ans
    finally:
        bot.client = original_client


@pytest.mark.parametrize("retry_outcome", ["raises", "empty"])
def test_extract_gemini_answer_failed_retry_goes_to_next_model(retry_outcome):
    # Повтор после битого вызова сам упал (а не просто не влез в бюджет) или
    # вернул пусто — отдаём пусто, чтобы маршрут ушёл на следующую модель.
    # Раньше пользователь получал "[Ответ заблокирован...]" как готовый ответ.
    async def retry_raises(*, model, contents, config=None):
        raise RuntimeError("transient 500 on retry")

    async def retry_empty(*, model, contents, config=None):
        return _FakeGeminiResponse(text="", candidates=[])

    retry_fn = retry_raises if retry_outcome == "raises" else retry_empty
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = retry_fn
    original_client = bot.client
    bot.client = fake_client
    try:
        resp = _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="MALFORMED_FUNCTION_CALL")])
        ans = asyncio.run(bot._extract_gemini_answer_text(
            resp, model_id="gemini-3.8-flash", call_contents=[], gconfig=None,
        ))
        assert ans == ""
    finally:
        bot.client = original_client


def test_run_route_reorders_slow_head_down(monkeypatch):
    # Интеграция reorder в _run_route: модель с измеренными 100с уходит вниз,
    # ask вызывается уже с переупорядоченной цепочкой.
    import lumen_model_speed

    seen = []

    async def fake_ask(chat_id, prompt, model_chain, *, deadline=None, **kwargs):
        seen.append(list(model_chain))
        return "ok"

    monkeypatch.setattr(bot, "ask_munito_text", fake_ask)
    lumen_model_speed._latency_ema.clear()
    lumen_model_speed.record_response(
        lumen_model_speed.speed_key("munito", "slow-model"), total_sec=100.0)
    incoming = _FakeIncomingMessage(999303)
    try:
        ans, sent = asyncio.run(bot._run_route(
            999303, "Привет", [("munito", "slow-model"), ("munito", "deepseek-ai/deepseek-v4-flash")], incoming))
        assert (ans, sent) == ("ok", False)
        assert seen[0][0] == "deepseek-ai/deepseek-v4-flash"
    finally:
        bot.chat_state.pop(999303, None)
        lumen_model_speed._latency_ema.clear()


@pytest.mark.parametrize("empty_payload", [None, "Empty response", "  empty response. ", "No response"])
def test_chain_empty_response_falls_through_to_next_model(monkeypatch, empty_payload):
    # Прод-кейс 17.09.2026: пользователь дважды увидел буквальное "Empty
    # response" — пустой ответ обязан двигать цепочку дальше, а не идти в чат.
    # Плейсхолдер провайдера (непустая строка) ловится тем же условием:
    # выборка бэкапа 05.11.2026 — 7 таких ответов в одном диалоге.
    import lumen_routes
    calls = []

    async def fake_request(payload, deadline):
        model = payload["model"]
        calls.append(model)
        if model == "probe/empty-head":
            if empty_payload is None:
                return {"choices": []}
            return {"choices": [{"message": {"content": empty_payload}}]}
        return {"choices": [{"message": {"content": "живой ответ"}}]}

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    answer, used = asyncio.run(lumen_routes._chat_completion_chain(
        messages, ["probe/empty-head", "probe/good"], "probe/empty-head",
        request_fn=fake_request, provider="munito",
    ))
    assert (answer, used) == ("живой ответ", "probe/good")
    assert calls == ["probe/empty-head", "probe/good"]


def test_munito_provenance_names_model_and_truncation(monkeypatch):
    # Выборка бэкапа 05.11.2026: у прежних провайдеров в provenance писалась None —
    # след нельзя было привязать к модели, а обрыв по потолку вывода не был виден нигде.
    chat_id = 999399

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": "текст до потолка"}, "finish_reason": "length"}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "_record_quota_usage", lambda *args, **kwargs: None)
    prov: dict = {}
    try:
        answer = asyncio.run(bot.ask_munito_text(chat_id, "привет", model_chain=["probe/m1"], provenance=prov))
    finally:
        bot.chat_state.pop(chat_id, None)
    assert answer == "текст до потолка"
    assert prov.get("provider") == "munito"
    assert prov.get("model") == "probe/m1"
    assert prov.get("finish_reason") == "length"


def test_chain_safety_label_only_response_moves_to_next_model(monkeypatch):
    # Прод-кейс из бэкапа 05.11.2026: весь ответ пользователя — 39 символов
    # «User Safety: safe» от модели-головы; непустая строка проходила проверку.
    import lumen_routes

    async def fake_request(payload, deadline):
        if payload["model"] == "probe/empty-head":
            return {"choices": [{"message": {"content": "User Safety: safe\nResponse Safety: safe"}}]}
        return {"choices": [{"message": {"content": "живой ответ"}}]}

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    answer, used = asyncio.run(lumen_routes._chat_completion_chain(
        messages, ["probe/empty-head", "probe/good"], "probe/empty-head",
        request_fn=fake_request, provider="munito",
    ))
    assert (answer, used) == ("живой ответ", "probe/good")


def test_chain_empty_responses_feed_quarantine_counter(monkeypatch):
    # Три пустых подряд в одну голову — карантин (реальная регрессия: мёртвая голова
    # жевала бы каждую попытку до конца суток вместо объезда после третьей).
    import lumen_routes

    async def fake_request(payload, deadline):
        return {"choices": []}

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    for _ in range(3):
        with pytest.raises(RuntimeError, match="empty response"):
            asyncio.run(lumen_routes._chat_completion_chain(
                messages, ["probe/qe"], "probe/qe", request_fn=fake_request, provider="munito"))
    assert bot._is_quarantined("munito", "probe/qe") is True


def test_chain_garbled_response_delivered_but_feeds_quarantine(monkeypatch):
    # Каша доставляется как раньше (без подмены), но в счётчик идёт: три таких —
    # и голова в карантине.
    import lumen_routes
    mush = "результат: " + "dataданные fileфайл testтест " + "продолжение " + "я" * 80
    assert len(mush) >= 100

    async def fake_request(payload, deadline):
        return {"choices": [{"message": {"content": mush}}]}

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    for _ in range(3):
        answer, used = asyncio.run(lumen_routes._chat_completion_chain(
            messages, ["probe/qm"], "probe/qm", request_fn=fake_request, provider="munito"))
        assert (answer, used) == (mush, "probe/qm")
    assert bot._is_quarantined("munito", "probe/qm") is True


def test_chain_echoed_mix_does_not_feed_quarantine(monkeypatch):
    # Тот же mush, но токены уже были в запросе: модель повторила за
    # пользователем — это эхо, карантин не кормим (иначе любой клал бы модели).
    import lumen_routes
    mush = "результат: " + "dataданные fileфайл testтест " + "продолжение " + "я" * 80
    user_text = "повтори за мной: dataданные fileфайл testтест и дальше своими словами"

    async def fake_request(payload, deadline):
        return {"choices": [{"message": {"content": mush}}]}

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    for _ in range(5):
        answer, used = asyncio.run(lumen_routes._chat_completion_chain(
            messages, ["probe/qe2"], "probe/qe2", request_fn=fake_request,
            provider="munito", user_text=user_text))
        assert (answer, used) == (mush, "probe/qe2")
    assert bot._is_quarantined("munito", "probe/qe2") is False


def test_attempt_timeout_caps_by_remaining_route_budget():
    # Регрессия (аудит 26.09.2026): каждая попытка ждала полные ROUTE_MODEL_TIMEOUT_SEC,
    # поэтому маршрут держал лок чата до 40+22с вместо заявленных 40с.
    assert bot._attempt_timeout(bot, None) == bot.ROUTE_MODEL_TIMEOUT_SEC
    assert bot._attempt_timeout(bot, time.monotonic() + 300.0) == bot.ROUTE_MODEL_TIMEOUT_SEC
    tight = bot._attempt_timeout(bot, time.monotonic() + 4.0)
    assert 0.5 <= tight <= 4.0
    # Истёкший бюджет — быстрый отказ, а не ещё 22с ожидания.
    assert bot._attempt_timeout(bot, time.monotonic() - 5.0) <= 1.0


def test_ask_gemini_attempt_respects_short_budget(monkeypatch):
    # Короткий бюджет маршрута: основная попытка не должна съесть весь остаток
    # целиком — иначе ответ приходит после ROUTE_TOTAL_BUDGET_SEC.
    from collections import deque
    chat_id = 999208
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    calls = []

    async def hanging_generate(*, model, contents, config=None):
        calls.append(model)
        await asyncio.sleep(10)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = hanging_generate
    original_client = bot.client
    bot.client = fake_client
    try:
        started = time.monotonic()
        with pytest.raises(bot.RouteBudgetExceededError):
            asyncio.run(bot.ask_gemini(chat_id, "привет", model_chain=["m1", "m2"], deadline=time.monotonic() + 0.6))
        # Попытка обрезалась остатком бюджета, дальше маршрут сразу остановился —
        # главное, он не ушёл далеко за объявленный бюджет.
        elapsed = time.monotonic() - started
        assert elapsed < 3, f"маршрут с бюджетом 0.6с обязан остановиться быстро: {elapsed:.1f}с"
        assert calls == ["m1"]
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_is_gemini_daily_quota_recognizes_google_per_day_metric():
    # Google пишет суточную квоту слитно в имени метрики
    # (GenerateRequestsPerDayPerProjectPerModel) — без токена perday она
    # выглядела бы минутным всплеском и получала бы короткую остывку.
    assert bot._is_gemini_daily_quota(
        "Quota exceeded for quota metric 'GenerateRequestsPerDayPerProjectPerModel'"
    ) is True
    assert bot._is_gemini_daily_quota(
        "RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'GenerateRequestsPerProjectPerModel'"
    ) is False


def test_gemini_empty_response_falls_through_to_next_model(monkeypatch):
    chat_id = 999304
    extracts = ["", "хороший ответ"]

    async def fake_extract(resp, *, model_id, call_contents, gconfig, deadline=None, **kwargs):
        return extracts.pop(0)

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(bot, "_extract_gemini_answer_text", fake_extract)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["m1", "m2"]))
        assert answer == "хороший ответ"
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_run_route_streams_munito_when_route_head_is_munito():
    chat_id = 999305

    async def fake_munito_stream(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return "Стримленный ответ от Munito", None

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("ask_munito_text не должен вызываться, если стрим уже сработал")

    original_stream = bot._try_munito_streaming
    original_munito_text = bot.ask_munito_text
    bot._try_munito_streaming = fake_munito_stream
    bot.ask_munito_text = must_not_be_called
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"), ("gemini", "gemini-3.5-flash-lite")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Стримленный ответ от Munito"
        assert sent is True
    finally:
        bot._try_munito_streaming = original_stream
        bot.ask_munito_text = original_munito_text


def test_run_route_streams_fallback_model_after_head_stream_failure(monkeypatch):
    # Прод 28.10.2026: стрим пропадал целиком, когда головная модель падала
    # до первого куска, — резервные отвечали обычным вызовом. Теперь поток
    # пробуется и на них, с переиспользованием того же пузырька.
    chat_id = 999304
    placeholder = _FakeSentMessage()
    calls = []

    async def fake_stream(cid, prompt, message, model_id, **kwargs):
        calls.append((model_id, kwargs.get("reuse_placeholder"), kwargs.get("wait_started")))
        if model_id == "gemini-3.8-flash":
            return None, placeholder
        return "Ответ со стрима резерва", None

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("обычный вызов не нужен: резервный стрим сработал")

    monkeypatch.setattr(bot, "_try_gemini_streaming", fake_stream)
    monkeypatch.setattr(bot, "_try_munito_streaming", fake_stream)
    monkeypatch.setattr(bot, "ask_gemini", must_not_be_called)
    monkeypatch.setattr(bot, "ask_munito_text", must_not_be_called)
    try:
        route = [("gemini", "gemini-3.8-flash"), ("munito", "deepseek-ai/deepseek-v4.1-flash-1017")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ со стрима резерва"
        assert sent is True
        assert [model for model, _, _ in calls] == ["gemini-3.8-flash", "deepseek-ai/deepseek-v4.1-flash-1017"]
        assert calls[1][1] is placeholder
        # Таймер ожидания общий на маршрут: переход к резерву не откатывает его в 0.
        assert calls[0][2] is not None and calls[0][2] == calls[1][2]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_run_route_falls_back_from_failed_munito_stream_to_non_streaming():
    chat_id = 999306

    async def fake_munito_stream_fail(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return None, None  # ранний сбой без плейсхолдера (например, message.reply сам не удался)

    calls = []

    async def fake_munito_text(cid, prompt, model_chain, deadline=None, **kwargs):
        calls.append(model_chain)
        return "Ответ без стрима"

    original_stream = bot._try_munito_streaming
    original_munito_text = bot.ask_munito_text
    bot._try_munito_streaming = fake_munito_stream_fail
    bot.ask_munito_text = fake_munito_text
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"), ("munito", "deepseek-ai/deepseek-v4-flash")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ без стрима"
        assert sent is False
        # Обычный вызов идёт по всему блоку: модель, у которой стрим упал раньше
        # показа, пробуется без потока — иначе сломанный SSE провайдера оставлял
        # бы чат без ответа вовсе (аудит 28.10.2026).
        assert calls[0] == ["deepseek-ai/deepseek-v4.1-flash-1017", "deepseek-ai/deepseek-v4-flash"]
    finally:
        bot._try_munito_streaming = original_stream
        bot.ask_munito_text = original_munito_text


def test_run_route_reuses_stream_placeholder_when_fallback_succeeds():
    # Регрессия на реальный найденный при тестировании баг: раньше при неудачном
    # стриме плейсхолдер "…" тут же удалялся, а следующая модель отправляла
    # совсем новое сообщение — визуально выглядело как "точки исчезли, потом
    # из ниоткуда появился ответ одним блоком". Теперь плейсхолдер должен
    # переиспользоваться (редактироваться) финальным ответом резервной модели.
    chat_id = 999307
    placeholder = _FakeSentMessage()

    async def fake_munito_stream_fail_with_placeholder(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return None, placeholder

    async def fake_munito_text(cid, prompt, model_chain, deadline=None, **kwargs):
        return "Ответ от резервной модели"

    original_stream = bot._try_munito_streaming
    original_munito_text = bot.ask_munito_text
    bot._try_munito_streaming = fake_munito_stream_fail_with_placeholder
    bot.ask_munito_text = fake_munito_text
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"), ("munito", "deepseek-ai/deepseek-v4-flash")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ от резервной модели"
        # sent=True означает, что ответ уже "доставлен" через правку плейсхолдера,
        # а не через отдельный новый _safe_reply в _handle_message_core.
        assert sent is True
        assert placeholder.deleted is False
        assert placeholder.edits[-1][0] == "Ответ от резервной модели"
    finally:
        bot._try_munito_streaming = original_stream
        bot.ask_munito_text = original_munito_text


def test_run_route_reuse_adds_thought_block_in_private_chat(monkeypatch):
    # Обычный текст после раннего сбоя стрима не должен терять «Thought for Ns»:
    # статуса в этом пути нет, блок строим по времени маршрута.
    chat_id = 999322
    placeholder = _FakeSentMessage()
    incoming = _FakeIncomingMessage(chat_id)

    async def fake_stream_fail(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return None, placeholder

    async def fake_text(cid, prompt, model_chain, deadline=None, **kwargs):
        return "Ответ от резервной модели"

    monkeypatch.setattr(bot, "_try_munito_streaming", fake_stream_fail)
    monkeypatch.setattr(bot, "ask_munito_text", fake_text)
    route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"),
             ("munito", "deepseek-ai/deepseek-v4-flash")]
    ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=incoming, allow_stream=True))
    assert ans == "Ответ от резервной модели"
    assert sent is True
    final_text = placeholder.edits[-1][0]
    assert final_text.startswith("<i>✻ Thought for")
    assert "Ответ от резервной модели" in final_text
    bot.chat_state.pop(chat_id, None)


def test_run_route_deletes_orphaned_placeholder_when_whole_route_fails():
    chat_id = 999308
    placeholder = _FakeSentMessage()

    async def fake_munito_stream_fail_with_placeholder(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return None, placeholder

    async def failing_munito_text(*args, **kwargs):
        raise bot.MunitoAPIError("всё сломано", status_code=500)

    original_stream = bot._try_munito_streaming
    original_munito_text = bot.ask_munito_text
    bot._try_munito_streaming = fake_munito_stream_fail_with_placeholder
    bot.ask_munito_text = failing_munito_text
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"), ("munito", "deepseek-ai/deepseek-v4-flash")]
        with pytest.raises(bot.MunitoAPIError):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert placeholder.deleted is True
    finally:
        bot._try_munito_streaming = original_stream
        bot.ask_munito_text = original_munito_text


def test_build_gemini_call_config_with_system_instruction_model_unaffected():
    # Модели С system_instruction (не no_system) вообще не проходят через ветку fake
    # identity-обмена — регрессия на то, что правка выше не задела этот путь: contents
    # должен остаться нетронутым, а полный текст идёт через system_instruction, как раньше.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    call_contents, gconfig = bot._build_gemini_call_config(bot.DEFAULT_GEMINI_MODEL, contents)
    assert call_contents is contents
    assert "USER WELLBEING" in gconfig.system_instruction


def test_build_gemini_call_config_sets_low_thinking_level_for_gemini_3_on_light_query():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет, как дела?")])]
    _, gconfig = bot._build_gemini_call_config("gemini-3.7-flash", contents)
    assert gconfig.thinking_config.thinking_level == bot.types.ThinkingLevel.LOW


def test_build_gemini_call_config_sets_zero_thinking_budget_for_gemini_2_5_on_light_query():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="столица франции?")])]
    _, gconfig = bot._build_gemini_call_config("gemini-2.5-flash", contents)
    assert gconfig.thinking_config.thinking_budget == 0


def test_build_gemini_call_config_does_not_override_thinking_on_heavy_query():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="напиши функцию для сортировки списка")])]
    _, gconfig = bot._build_gemini_call_config("gemini-3.7-flash", contents)
    assert gconfig.thinking_config is None



def test_ask_gemini_daily_quota_marks_model_exhausted_and_switches():
    # Суточная квота Google помечает модель до утра и уводит маршрут на следующую.
    chat_id = 999010
    calls = []

    class _QuotaExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise _QuotaExc("Quota exceeded for the day: RESOURCE_EXHAUSTED")
        return _FakeGeminiResponse(text="Ответ второй модели")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ второй модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
        assert bot.GLOBAL_QUOTA["gemini"]["gemini-3.6-flash"]["exhausted_at"] is not None
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)


def test_ask_gemini_burst_429_only_cools_down_the_model():
    # Регрессия (враждебное ревью 27.09.2026): ЛЮБОЙ 429 у Gemini ставил суточную
    # метку, и один всплеск минутного лимита убирал модель из роута до полуночи.
    # Минутный лимит должен давать только короткую остывку.
    chat_id = 999013
    calls = []

    class _BurstExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise _BurstExc("RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Generate requests'")
        return _FakeGeminiResponse(text="Ответ второй модели")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ второй модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
        entry = bot.GLOBAL_QUOTA["gemini"]["gemini-3.6-flash"]
        assert entry["exhausted_at"] is None, "минутный всплеск не должен запирать модель до утра"
        assert entry["cooldown_until"] > time.time()
        # Остывка истекает сама — сутки ждать не нужно.
        entry["cooldown_until"] = time.time() - 1.0
        assert lumen_router_config._is_quota_exhausted("gemini", "gemini-3.6-flash") is False
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)


def test_chat_completion_chain_cools_down_model_on_429():
    # Нестриминговый текстовый провайдер обязан писать cooldown после 429 — зеркало
    # стриминга, иначе _is_quota_exhausted его не видит и модель долбят заново.
    # У Munito лимиты оконные, поэтому остывка, а не суточная метка.
    import lumen_routes
    exc_cls = bot.MunitoAPIError

    async def boom_429(payload, deadline):
        raise exc_cls("HTTP 429 rate limit", status_code=429)

    model = "probe/rl-munito"
    provider = "munito"
    bot.GLOBAL_QUOTA.setdefault(provider, {}).pop(model, None)
    try:
        with pytest.raises(exc_cls):
            asyncio.run(lumen_routes._chat_completion_chain(
                [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}],
                [model], model, request_fn=boom_429, provider=provider,
            ))
        entry = bot.GLOBAL_QUOTA[provider][model]
        assert entry["exhausted_at"] is None, "оконный лимит не должен запирать модель до утра"
        assert entry["cooldown_until"] > time.time()
        assert lumen_router_config._is_quota_exhausted(provider, model) is True
    finally:
        bot.GLOBAL_QUOTA[provider].pop(model, None)


def test_chat_completion_chain_cools_down_model_on_unavailable():
    # Прод 05.10.2026: модели линейки Gemini 3.x отдавали 503 и повторялись на каждом
    # сообщении — до 22 секунд и нескольких попыток впустую. 503 не квота, но
    # модель на QUOTA_RATE_LIMIT_COOLDOWN_SEC пропускать надо. Падает на старом
    # коде, где "unavailable" не оставлял метки. Прод 05.11.2026: у Munito те же 503
    # «no free capacity» на бесплатном тарифе.
    import lumen_routes
    exc_cls = bot.MunitoAPIError

    async def boom_503(payload, deadline):
        raise exc_cls("model is currently unavailable", status_code=503)

    model = "probe/unavail-munito"
    provider = "munito"
    bot.GLOBAL_QUOTA.setdefault(provider, {}).pop(model, None)
    try:
        with pytest.raises(exc_cls):
            asyncio.run(lumen_routes._chat_completion_chain(
                [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}],
                [model], model, request_fn=boom_503, provider=provider,
            ))
        assert bot.GLOBAL_QUOTA[provider][model]["cooldown_until"] > time.time()
        assert lumen_router_config._is_quota_exhausted(provider, model) is True
    finally:
        bot.GLOBAL_QUOTA[provider].pop(model, None)


def test_ask_gemini_503_cools_model_down_instead_of_retrying_every_message():
    # Тот же сценарий на текстовом пути Gemini: 503 от головы цепочки обязан
    # оставить остывку, иначе модель долбится заново на каждом обращении.
    chat_id = 999018
    calls = []

    class _Unavailable(Exception):
        status_code = 503

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise _Unavailable("The model is currently unavailable")
        return _FakeGeminiResponse(text="Ответ резервной модели")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ резервной модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
        entry = bot.GLOBAL_QUOTA["gemini"]["gemini-3.6-flash"]
        assert entry["exhausted_at"] is None, "503 — не суточная квота, запирать до утра нельзя"
        assert entry["cooldown_until"] > time.time()
        assert lumen_router_config._is_quota_exhausted("gemini", "gemini-3.6-flash") is True
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)


def test_ask_gemini_raises_all_models_exhausted_when_entire_chain_429s():
    chat_id = 999011

    class _QuotaExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        raise _QuotaExc("Quota exceeded for the day: RESOURCE_EXHAUSTED")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        with pytest.raises(bot.GeminiAllModelsExhaustedError) as exc_info:
            asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert set(exc_info.value.exhausted_models) == {"gemini-3.6-flash", "gemini-2.5-flash"}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash", None)


def test_ask_gemini_minute_429s_do_not_raise_empty_exhausted():
    # Одни минутные всплески: суточного исчерпания не было — последний 429
    # идёт как есть, а не пустой "исчерпано всё".
    chat_id = 999017

    class _Minute429(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        raise _Minute429("RESOURCE_EXHAUSTED: quota exceeded for quota metric Generate requests per minute")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        with pytest.raises(_Minute429):
            asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-2.5-flash", None)


def test_ask_gemini_falls_back_to_next_model_on_timeout():
    # Прежняя версия использовала блокирующий time.sleep в async-фейке и один и тот
    # же текст ответа для обеих моделей: таймаут никогда не срабатывал, фолбэка не
    # было, а тест всё равно проходил (враждебное ревью 27.09.2026). Теперь фейк
    # асинхронный, тексты разные, проверяются оба вызова и время.
    chat_id = 999012
    calls = []
    original_timeout = bot.ROUTE_MODEL_TIMEOUT_SEC

    async def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            await asyncio.sleep(5.0)
        # Нейтральные тексты без имён моделей: имя модели в ответе триггерит
        # скраб утечек личности и подменяет ответ заглушкой.
        return _FakeGeminiResponse(text="Первый ответ" if model == "gemini-3.6-flash" else "Второй ответ")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = fake_generate_content
    original_client = bot.client
    bot.client = fake_client
    bot.ROUTE_MODEL_TIMEOUT_SEC = 0.05
    try:
        started = time.monotonic()
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        elapsed = time.monotonic() - started
        assert answer == "Второй ответ"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
        assert elapsed < 5, f"фолбэк по таймауту обязан уложиться быстрее висящей модели: {elapsed:.1f}с"
    finally:
        bot.client = original_client
        bot.ROUTE_MODEL_TIMEOUT_SEC = original_timeout
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_falls_back_to_next_model_on_generic_error():
    chat_id = 999013
    calls = []

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise RuntimeError("internal error 500")
        return _FakeGeminiResponse(text="Ответ от второй модели")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ от второй модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_raises_when_route_budget_exceeded():
    chat_id = 999014

    def fake_generate_content(*, model, contents, config=None):
        raise RuntimeError("internal error 500")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        past_deadline = time.monotonic() - 1.0
        with pytest.raises(bot.RouteBudgetExceededError):
            asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"], deadline=past_deadline))
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize(("status_code", "message"), [(429, "rate limit exceeded"), (403, "forbidden")])
def test_chain_switches_model_on_provider_error(status_code, message):
    import lumen_routes
    # И оконный 429, и "постоянная" на вид 403 не обрывают переход к следующей
    # модели: ровно одна попытка на модель, независимо от классификации ошибки.
    async def fake_request(payload, deadline):
        if payload["model"] == "probe/model-a":
            raise bot.MunitoAPIError(message, status_code=status_code)
        return {"choices": [{"message": {"content": "ответ от model-b"}}]}

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    answer, used = asyncio.run(lumen_routes._chat_completion_chain(
        messages, ["probe/model-a", "probe/model-b"], "probe/model-a",
        request_fn=fake_request, provider="munito",
    ))
    assert answer == "ответ от model-b"
    assert used == "probe/model-b"


def test_chain_whole_failure_raises_last_model_error():
    import lumen_routes
    # Вся цепочка легла — наверх идёт ошибка ПОСЛЕДНЕЙ модели (её маппит
    # _route_error_reply_text), а не первой: пользователь должен видеть статус
    # резерва, а не голову. Регрессия: возврат первой ошибки показал бы «модель
    # недоступна», когда резерв был жив и падал по другой причине.
    async def fake_request(payload, deadline):
        raise bot.MunitoAPIError(f"сбой {payload['model']}", status_code=500)

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    with pytest.raises(bot.MunitoAPIError, match="сбой probe/model-b"):
        asyncio.run(lumen_routes._chat_completion_chain(
            messages, ["probe/model-a", "probe/model-b"], "probe/model-a",
            request_fn=fake_request, provider="munito",
        ))


def test_ask_munito_multimodal_sends_all_album_images_not_just_first(monkeypatch):
    # Внешний аудит: уходила только первая фотография альбома, остальные 2–9 молча терялись.
    chat_id = 999411
    captured = {}

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        captured["messages"] = json_body["messages"]
        return {"choices": [{"message": {"content": "вижу три фото"}}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "_record_quota_usage", lambda *args, **kwargs: None)
    try:
        media = [(b"one", "image/jpeg"), (b"two", "image/png"), (b"three", "image/jpeg")]
        answer = asyncio.run(bot.ask_munito_multimodal(chat_id, "что на фото?", media, "a.jpg", model_chain=["probe/m"]))
        assert answer == "вижу три фото"
        user_msg = captured["messages"][-1]
        images = [p for p in user_msg["content"] if isinstance(p, dict) and p.get("type") == "image_url"]
        assert len(images) == 3
    finally:
        bot.chat_state.pop(chat_id, None)


def test_ask_munito_multimodal_skips_video_slides_for_gemini(monkeypatch):
    # Видео-слайды OpenAI-формат Munito не принимает — их забирает Gemini-ветка, сюда едут только картинки.
    chat_id = 999412
    captured = {}

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        captured["messages"] = json_body["messages"]
        return {"choices": [{"message": {"content": "вижу фото"}}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "_record_quota_usage", lambda *args, **kwargs: None)
    try:
        media = [(b"vid", "video/mp4"), (b"pic", "image/jpeg")]
        asyncio.run(bot.ask_munito_multimodal(chat_id, "что это?", media, "a.jpg", model_chain=["probe/m"]))
        user_msg = captured["messages"][-1]
        images = [p for p in user_msg["content"] if isinstance(p, dict) and p.get("type") == "image_url"]
        assert len(images) == 1
    finally:
        bot.chat_state.pop(chat_id, None)


def test_ask_munito_multimodal_rejects_video_only_album(monkeypatch):
    # Только видео без картинок — нечего слать в Munito: ошибка уводит маршрут в Gemini.
    chat_id = 999413
    calls = []

    async def must_not_be_called(*args, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"content": "не допустимо"}}]}

    monkeypatch.setattr(bot, "_munito_request", must_not_be_called)
    with pytest.raises(RuntimeError, match="No images"):
        asyncio.run(bot.ask_munito_multimodal(
            chat_id, "что это?", [(b"vid", "video/mp4")], "a.mp4", model_chain=["probe/m"],
        ))
    assert calls == []
    bot.chat_state.pop(chat_id, None)


def test_route_error_reply_text_youtube_takes_priority_over_exception_type():
    exc = bot.MunitoAPIError("boom", status_code=500)
    text = bot._route_error_reply_text(exc, "gemini-3.6-flash", youtube_url_to_analyze="https://youtu.be/x")
    assert "video" in text.lower()
    text_ru = bot._route_error_reply_text(exc, "gemini-3.6-flash", youtube_url_to_analyze="https://youtu.be/x", lang="ru")
    assert "видео" in text_ru.lower()


def test_route_error_reply_text_maps_known_exception_types():
    quota_exc = bot.GeminiAllModelsExhaustedError(["gemini-3.6-flash"])
    assert bot._route_error_reply_text(quota_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._gemini_error_msg(quota_exc, "gemini-3.6-flash")

    budget_exc = bot.RouteBudgetExceededError(["gemini-3.6-flash"])
    assert "overloaded" in bot._route_error_reply_text(budget_exc, "gemini-3.6-flash", youtube_url_to_analyze=None).lower()
    assert "перегруж" in bot._route_error_reply_text(budget_exc, "gemini-3.6-flash", youtube_url_to_analyze=None, lang="ru").lower()

    munito_exc = bot.MunitoAPIError("boom", status_code=500)
    assert bot._route_error_reply_text(munito_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._or_error_msg(munito_exc, "munito")

    other_exc = RuntimeError("что-то сломалось")
    assert bot._route_error_reply_text(other_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._gemini_error_msg(other_exc, "gemini-3.6-flash")


def test_provider_request_scrubs_api_key_from_network_exception_message(monkeypatch):
    # Defense-in-depth у текстового провайдера: ключ не светится в тексте ошибок.
    fake_key = "fake-munito-key-123"

    class _FakeSessionRaisingWithKey:
        def request(self, *args, **kwargs):
            raise RuntimeError(f"connection failed, headers: Bearer {fake_key}")

    async def fake_get_http_session():
        return _FakeSessionRaisingWithKey()

    monkeypatch.setattr(bot, "_get_http_session", fake_get_http_session)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", fake_key)
    with pytest.raises(bot.MunitoAPIError) as exc_info:
        asyncio.run(bot._munito_request("chat/completions", "POST", json_body={"model": "x"}))
    assert fake_key not in str(exc_info.value)
    assert "<KEY>" in str(exc_info.value)


def test_system_prompt_en_keeps_key_guards():
    import system_prompt
    for guard in (
        "You are Lumen",
        "@SilverElixir",
        "CRITICALLY IMPORTANT",
        "8-800-2000-122",
        "EMOJI USAGE RULE",
        "NON-REMOVABLE BOUNDARIES",
        "without moralizing",
        "explicit sexual content",
        "romantic or sexual roleplay",
        "malicious code",
        "prohibited substances",
    ):
        assert guard in system_prompt.SYSTEM_PROMPT
    # Границы продукта 10.2026: возврат к "minimal filters" с разрешением явного контента прошёл бы старые проверки.
    for gone in (
        "minimal filters",
        "You may discuss adult topics, sexual content",
    ):
        assert gone not in system_prompt.SYSTEM_PROMPT


def test_get_system_prompt_header_english():
    assert "CURRENT TIME INFORMATION" in bot.get_system_prompt()


def test_ask_munito_text_success_records_munito_quota(monkeypatch):
    # Успешный ответ пишется в квоту "munito", как у остальных провайдеров:
    # /stats и /diag читают именно её.
    from collections import deque
    chat_id = 999701
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        assert json_body["model"] == "deepseek-ai/deepseek-v4.1-flash-1017"
        return {"choices": [{"message": {"content": "Канберра."}}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    recorded = {}
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model, **kw: recorded.setdefault("v", (provider, model)))
    answer = asyncio.run(bot.ask_munito_text(chat_id, "Столица Австралии?", model_chain=["deepseek-ai/deepseek-v4.1-flash-1017"]))
    assert answer == "Канберра."
    assert recorded["v"] == ("munito", "deepseek-ai/deepseek-v4.1-flash-1017")


def test_ask_munito_text_falls_back_to_next_model_and_is_dispatched(monkeypatch):
    # Что защищает: провайдер munito в _run_route — и в обычном пути (ask_munito_text),
    # и в стрим-пробе (_streamable/_try_stream). Регрессия: без текстового пути
    # ("munito", …) весь обычный текст валился бы в Gemini-резерв и сжигал его дневную
    # квоту, а без "_streamable" чат терял посимвольный вывод на Munito.
    from collections import deque
    chat_id = 999703
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    seen = []

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        seen.append(json_body["model"])
        if json_body["model"] == "m1":
            raise bot.MunitoAPIError("no free capacity", status_code=503)
        return {"choices": [{"message": {"content": "ok"}}]}

    stream_probe = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_try_munito_streaming", stream_probe)
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model, **kw: None)
    answer, _ = asyncio.run(bot._run_route(
        chat_id, "привет", [("munito", "m1"), ("munito", "m2")], message=None, allow_stream=True,
    ))
    assert answer == "ok"
    assert stream_probe.await_count == 2, "стрим-проба обязана пройти по обеим моделям Munito"
    assert seen == ["m1", "m2"], "обычный путь должен дойти до второй модели после 503"


def test_ask_munito_text_sends_reasoning_effort_and_omits_it_when_unset(monkeypatch):
    # protected поведение: low-effort доезжает до тела запроса на текстовом пути.
    # Регрессия: поле заведут в одном месте (стрим) и потеряют в другом — тогда
    # обычные ответы по-прежнему сжигают reasoning-токены, а пустой answer от Qwen
    # возвращается.
    from collections import deque
    bodies = []

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        bodies.append(json_body)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_record_quota_usage", lambda *args, **kwargs: None)
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "low")
    asyncio.run(bot.ask_munito_text(999704, "привет", model_chain=["m1"]))
    assert bodies[0]["reasoning_effort"] == "low"
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "")
    asyncio.run(bot.ask_munito_text(999705, "привет", model_chain=["m1"]))
    assert "reasoning_effort" not in bodies[1]


def test_munito_request_requires_key_and_maps_provider_error(monkeypatch):
    # Без ключа — понятная ошибка вместо сетевого вызова, а ответ API не показывается
    # пользователю сырым (внутри может быть что угодно, включая чужие данные).
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "")
    with pytest.raises(bot.MunitoAPIError):
        asyncio.run(bot._munito_request("chat/completions", "POST", json_body={"model": "x"}))
    text = bot._route_error_reply_text(
        bot.MunitoAPIError("Free tier capacity exhausted", status_code=503),
        "deepseek-ai/deepseek-v4.1-flash-1017", youtube_url_to_analyze=None, lang="en",
    )
    assert "Free tier capacity exhausted" not in text
    assert text.strip()


def test_model_attempt_counters_track_chain_success_and_failure(monkeypatch):
    # 08.10.2026: по счётчикам model_attempts/model_failures виден реальный КПД
    # маршрута (раньше делили ответы на попытки только вручную по логам).
    from collections import deque
    chat_id = 999707
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    calls = {"n": 0}

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise bot.MunitoAPIError("boom", status_code=500)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model, service=False: None)
    before_a = bot._stats_entry().get("model_attempts", 0)
    before_f = bot._stats_entry().get("model_failures", 0)
    assert asyncio.run(bot.ask_munito_text(chat_id, "привет", model_chain=["probe/a", "probe/b"])) == "ok"
    assert bot._stats_entry().get("model_attempts", 0) - before_a == 2
    assert bot._stats_entry().get("model_failures", 0) - before_f == 1


def test_shared_chain_raises_budget_error_without_second_attempt(monkeypatch):
    # Общий цикл не должен тратить вторую попытку, если бюджет маршрута истёк.
    from collections import deque
    chat_id = 999704
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    seen = []

    async def slow_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        seen.append(json_body["model"])
        await asyncio.sleep(0.4)
        return {"choices": []}

    monkeypatch.setattr(bot, "_munito_request", slow_request)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    with pytest.raises(bot.RouteBudgetExceededError):
        asyncio.run(bot.ask_munito_text(chat_id, "привет", model_chain=["probe/m1", "probe/m2"], deadline=time.monotonic() + 0.2))
    assert seen == ["probe/m1"]


def _fake_groq_audio_session(response_json, status=200):
    class _FakeResp:
        def __init__(self):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            return b"{}"

        async def json(self, content_type=None):
            return response_json

    class _FakeSession:
        def post(self, *args, **kwargs):
            return _FakeResp()

    async def fake_get_http_session():
        return _FakeSession()

    return fake_get_http_session


def test_transcribe_audio_success_does_not_pollute_quota(monkeypatch):
    # Служебный вызов — в квоту не пишем: /stats и триггеры exhausted только про ответы людям.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "  привет мир  "}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    bot.GLOBAL_QUOTA.setdefault("groq", {}).pop("whisper-large-v3-turbo", None)
    try:
        assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) == "привет мир"
        assert "whisper-large-v3-turbo" not in bot.GLOBAL_QUOTA.get("groq", {})
    finally:
        bot.GLOBAL_QUOTA.get("groq", {}).pop("whisper-large-v3-turbo", None)


def test_record_quota_usage_service_flag_skips_counter():
    # Обычный вызов считает, служебный — нет (саммари/транскрибация).
    bot.GLOBAL_QUOTA.setdefault("groq", {}).pop("svc-probe-model", None)
    try:
        bot._record_quota_usage("groq", "svc-probe-model", service=True)
        assert "svc-probe-model" not in bot.GLOBAL_QUOTA.get("groq", {})
        bot._record_quota_usage("groq", "svc-probe-model")
        assert bot.GLOBAL_QUOTA["groq"]["svc-probe-model"]["used"] >= 1
    finally:
        bot.GLOBAL_QUOTA.get("groq", {}).pop("svc-probe-model", None)


def test_record_quota_usage_clears_cooldown_and_exhausted_marks():
    # Успешный ответ снимает и суточную метку, и остывку: иначе модель, однажды
    # упёршаяся в лимит, несла бы метку даже после живых ответов.
    bot.GLOBAL_QUOTA.setdefault("groq", {}).pop("cool-probe-model", None)
    try:
        bot._mark_quota_exhausted("groq", "cool-probe-model")
        bot._mark_rate_limited("groq", "cool-probe-model")
        entry = bot.GLOBAL_QUOTA["groq"]["cool-probe-model"]
        assert entry["exhausted_at"] is not None and entry["cooldown_until"] is not None
        bot._record_quota_usage("groq", "cool-probe-model")
        entry = bot.GLOBAL_QUOTA["groq"]["cool-probe-model"]
        assert entry["exhausted_at"] is None and entry["cooldown_until"] is None
    finally:
        bot.GLOBAL_QUOTA.get("groq", {}).pop("cool-probe-model", None)


def test_transcribe_audio_empty_result_is_none(monkeypatch):
    # Пустая расшифровка — как неудача: вызывающий код идёт прежним путём.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "   "}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) is None


def test_transcribe_audio_truncates_huge_transcript(monkeypatch):
    # Час речи без обрезки упёрся бы в лимиты моделей ниже по маршруту.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "слово " * 2000}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    text = asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123))
    assert len(text) <= 4001
    assert text.endswith("…")


def test_transcribe_audio_no_key_or_oversize_skips_network(monkeypatch):
    async def must_not_be_called():
        raise AssertionError("no network without key or for oversize audio")

    monkeypatch.setattr(bot, "_get_http_session", must_not_be_called)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) is None
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "VOICE_TRANSCRIBE_MAX_BYTES", 4)
    assert asyncio.run(bot._transcribe_audio(b"too-big-bytes", "audio/ogg", 123)) is None


def test_transcribe_audio_http_error_is_none(monkeypatch):
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({}, status=503))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) is None


def test_transcribe_audio_scrubs_key_from_failure_log(monkeypatch, caplog):
    # Удаление Groq-чата не снимает защиту от утечки ключа: Whisper пишет в лог текст
    # сетевого исключения, и без подмены туда попал бы GROQ_API_KEY (его в логах
    # читает кто угодно — хостинг Spaces, агрегаторы).
    import logging

    async def raising_session():
        raise RuntimeError("connection failed, headers: Bearer fake-groq-key-123")

    monkeypatch.setattr(bot, "_get_http_session", raising_session)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-groq-key-123")
    with caplog.at_level(logging.WARNING, logger="bot"):
        assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) is None
    lines = [r.getMessage() for r in caplog.records if "Whisper request failed" in r.getMessage()]
    assert len(lines) == 1
    assert "fake-groq-key-123" not in lines[0]
    assert "<KEY>" in lines[0]


def test_transcribe_audio_uses_remaining_budget_not_full_timeout(monkeypatch):
    # Аудит A4-03: Whisper ждал полный ROUTE_MODEL_TIMEOUT_SEC вне бюджета маршрута.
    captured = {}

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            return b"{}"

        async def json(self, content_type=None):
            return {"text": "привет"}

    class _FakeSession:
        def post(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeResp()

    async def fake_get_http_session():
        return _FakeSession()

    monkeypatch.setattr(bot, "_get_http_session", fake_get_http_session)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    text = asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123, deadline=time.monotonic() + 5.0))
    assert text == "привет"
    assert captured["timeout"].total <= 5.0
    assert captured["timeout"].total < bot.ROUTE_MODEL_TIMEOUT_SEC


def test_transcribe_audio_capped_by_own_limit(monkeypatch):
    # Аудит A2-12: транскрибация ждала остаток всего бюджета и морила модель —
    # теперь отдельный меньший лимит поверх остатка.
    import lumen_routes
    captured = {}

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            return b"{}"

        async def json(self, content_type=None):
            return {"text": "привет"}

    class _FakeSession:
        def post(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeResp()

    async def fake_get_http_session():
        return _FakeSession()

    monkeypatch.setattr(bot, "_get_http_session", fake_get_http_session)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    text = asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123, deadline=time.monotonic() + 1000.0))
    assert text == "привет"
    assert captured["timeout"].total <= lumen_routes._TRANSCRIBE_TIMEOUT_SEC
    assert captured["timeout"].total < bot.ROUTE_MODEL_TIMEOUT_SEC


def test_munito_text_trims_history_with_summary_not_silent_cut(monkeypatch):
    # Аудит A4-05: история резалась молчаливым срезом вместо _trim_history с саммари.
    # Регрессия: саммари выпиливают (дорогой вызов) — и дальние ходы исчезают без следа.
    import lumen_chat_state
    from collections import deque
    chat_id = 999811
    answer = "ответ"

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": answer}}]}

    async def fake_summarize(text):
        return "итог: погода и коты"

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(lumen_chat_state, "_summarize_text", fake_summarize)
    state = bot.get_state(chat_id)
    state["history"] = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"сообщение {i}"}
        for i in range(105)
    ]
    state["ctx"] = deque()
    try:
        asyncio.run(bot.ask_munito_text(chat_id, "новый вопрос", model_chain=["probe/m1"]))
        history = bot.chat_state[chat_id]["history"]
        assert len(history) == 81
        assert history[0]["content"].startswith("[Ранее в диалоге]")
        assert "погода" in history[0]["content"]
        assert history[-2:] == [
            {"role": "user", "content": "новый вопрос"},
            {"role": "assistant", "content": answer},
        ]
    finally:
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize("mode", ["text", "vision"])
def test_munito_keeps_media_note_out_of_history(monkeypatch, mode):
    # Пометка одноразовая (как у ask_gemini): в историю — чистый текст, иначе старые
    # пометки всплывут в следующих ходах. У vision-ветки своя строка записи истории,
    # поэтому проверяем оба входа.
    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": "ответ"}}]}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    monkeypatch.setattr(bot, "_record_quota_usage", lambda *args, **kwargs: None)
    prompt = "что на фото?" + bot._NO_MEDIA_NOTE
    chat_id = 999813 if mode == "text" else 999814
    try:
        if mode == "text":
            asyncio.run(bot.ask_munito_text(chat_id, prompt, model_chain=["probe/m1"]))
        else:
            asyncio.run(bot.ask_munito_multimodal(
                chat_id, prompt, (b"pic", "image/jpeg"), "a.jpg", model_chain=["probe/m1"]))
        assert bot.chat_state[chat_id]["history"][-2] == {"role": "user", "content": "что на фото?"}
    finally:
        bot.chat_state.pop(chat_id, None)


def test_run_route_cleans_placeholder_on_cancel(monkeypatch):
    # Аудит A4-06: отмена посреди ответа оставляла "…" в чате навсегда.
    placeholder = _FakeSentMessage()

    async def failed_stream(*args, **kwargs):
        return None, placeholder

    async def cancelled_ask(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(bot, "_try_munito_streaming", failed_stream)
    monkeypatch.setattr(bot, "ask_munito_text", cancelled_ask)
    incoming = _FakeIncomingMessage(999970)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot._run_route(
                999970, "привет",
                [("munito", "probe/m1"), ("munito", "probe/m2")],
                incoming, allow_stream=True,
            ))
        assert placeholder.deleted is True
    finally:
        bot.chat_state.pop(999970, None)


def test_run_route_records_user_daily_on_gemini_success():
    # Новое: _run_route — единственная воронка учёта; успех Gemini растит оба счётчика.
    from types import SimpleNamespace
    chat_id = 999305
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777011)

    async def fake_ask_gemini(cid, prompt, media=None, youtube_url=None, model_chain=None, deadline=None, provenance=None, **kwargs):
        return "Ответ от Gemini"

    original = bot.ask_gemini
    bot.ask_gemini = fake_ask_gemini
    try:
        ans, sent = asyncio.run(bot._run_route(
            chat_id, "привет", [("gemini", "gemini-3.8-flash")], incoming, allow_stream=False,
        ))
        assert ans == "Ответ от Gemini" and sent is False
        entry = bot.GLOBAL_QUOTA.get("user_daily", {}).get("777011")
        assert entry is not None and entry["total"] == 1 and entry["gemini"] == 1
    finally:
        bot.ask_gemini = original
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777011", None)
        bot.chat_state.pop(chat_id, None)


def test_run_route_failure_records_no_user_daily():
    # Новое, пара к предыдущему: неудачный вызов не считается.
    from types import SimpleNamespace
    chat_id = 999306
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777012)

    async def failing_ask(*args, **kwargs):
        raise bot.MunitoAPIError("сломано", status_code=500)

    original = bot.ask_munito_text
    bot.ask_munito_text = failing_ask
    try:
        with pytest.raises(bot.MunitoAPIError):
            asyncio.run(bot._run_route(
                chat_id, "привет", [("munito", "probe/m1")], incoming, allow_stream=False,
            ))
        assert "777012" not in bot.GLOBAL_QUOTA.get("user_daily", {})
    finally:
        bot.ask_munito_text = original
        bot.chat_state.pop(chat_id, None)


def test_run_route_keeps_route_block_order(monkeypatch):
    # Что защищает: маршрут исполняется блоками в том порядке, как собран, а блоки
    # одного провайдера не склеиваются в один вызов (аудит 27.10.2026: склейка
    # уводила резерв вперёд независимого провайдера и съедала бюджет).
    import lumen_model_speed
    calls = []

    async def fake_ask_munito(cid, prompt, model_chain, deadline=None, **kwargs):
        calls.append(("munito", list(model_chain)))
        raise bot.MunitoAPIError("no free capacity", status_code=503)

    async def fake_ask_gemini(cid, prompt, media=None, youtube_url=None, model_chain=None,
                              deadline=None, provenance=None, **kwargs):
        calls.append(("gemini", list(model_chain)))
        if model_chain == ["gemini-2.5-flash"]:
            return "резерв Gemini"
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(bot, "ask_gemini", fake_ask_gemini)
    monkeypatch.setattr(bot, "ask_munito_text", fake_ask_munito)
    route = [
        ("munito", "probe/head"),
        ("gemini", "gemini-3.8-flash"),
        ("munito", "probe/reserve"),
        ("gemini", "gemini-2.5-flash"),
    ]
    lumen_model_speed._latency_ema.clear()
    try:
        ans, sent = asyncio.run(bot._run_route(999310, "привет", route, message=None, allow_stream=False))
        assert ans == "резерв Gemini" and sent is False
        assert calls == [
            ("munito", ["probe/head"]),
            ("gemini", ["gemini-3.8-flash"]),
            ("munito", ["probe/reserve"]),
            ("gemini", ["gemini-2.5-flash"]),
        ]
    finally:
        lumen_model_speed._latency_ema.clear()
        bot.chat_state.pop(999310, None)


def test_run_route_munito_answer_does_not_spend_gemini_user_limit():
    # Что защищает: дневной лимит Gemini у пользователя. Регрессия: ответ Munito
    # засчитался бы как Gemini, и несколько обычных сообщений запирали голос/видео
    # и ссылки на сутки (аудит 27.10.2026 был про Gemma, с 05.11.2026 тот же смысл
    # у Munito: он не тратит квоту Google).
    from types import SimpleNamespace
    chat_id = 999311
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777013)

    async def fake_ask_munito(cid, prompt, model_chain, deadline=None, **kwargs):
        return "Ответ Munito"

    original = bot.ask_munito_text
    bot.ask_munito_text = fake_ask_munito
    try:
        ans, _ = asyncio.run(bot._run_route(
            chat_id, "привет", [("munito", "deepseek-ai/deepseek-v4.1-flash-1017")], incoming,
            allow_stream=False, provenance={},
        ))
        assert ans == "Ответ Munito"
        entry = bot.GLOBAL_QUOTA.get("user_daily", {}).get("777013")
        assert entry is not None
        assert entry["total"] == 1 and entry["gemini"] == 0
    finally:
        bot.ask_munito_text = original
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777013", None)
        bot.chat_state.pop(chat_id, None)


def test_handled_message_grows_provider_quota_user_daily_and_stats(rate_guard_setup, monkeypatch):
    # Что защищает: воронку учёта после обработанного сообщения (квота
    # провайдера + user_daily + суточные счётчики /stats одним проходом).
    # Регрессия: тихий no-count при рефакторинге (не та модель, service-флаг не
    # там, потеря вызова) — /stats показывает вечные нули, хотя ботом пользуются.
    # Старые тесты мокают _run_route целиком (расход не проверяют) или проверяют
    # только ask-уровень без user_daily/stats.
    from collections import deque
    message = rate_guard_setup()
    message.text = "привет, как дела?"
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    # Стриминг в тестах без сети: гасим попытки, дальше обычный текстовый путь.
    monkeypatch.setattr(bot, "_try_gemini_streaming", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(bot, "_try_munito_streaming", AsyncMock(return_value=(None, None)))

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": "Привет! Как сам?"}}]}

    # Ключ включает Munito: без него _build_route отдаёт весь текст Gemini, и расход
    # текстового провайдера перестал бы проверяться.
    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    before = dict(bot._stats_entry())
    try:
        asyncio.run(bot._handle_message_core(message))
        munito_quota = bot.GLOBAL_QUOTA.get("munito", {})
        total_used = sum(int(e.get("used") or 0) for e in munito_quota.values() if isinstance(e, dict))
        assert total_used == 1
        entry = bot.GLOBAL_QUOTA.get("user_daily", {}).get("456")
        assert entry is not None and entry["total"] == 1
        # 05.11.2026: дневной лимит Gemini растёт только на ответы Gemini.
        assert entry["gemini"] == 0
        after = bot._stats_entry()
        assert after["messages_received"] - before.get("messages_received", 0) == 1
        assert after["answers_sent"] - before.get("answers_sent", 0) == 1
        # Munito отвечает головой: резерв не вызывался, переключений нет.
        assert after.get("fallbacks", 0) == before.get("fallbacks", 0)
        assert after.get("all_failed", 0) == before.get("all_failed", 0)
    finally:
        bot.chat_state.pop(123, None)


def test_run_route_counts_fallback_and_total_failure(monkeypatch):
    # Что защищает: счётчики переключений на резерв и полного отказа маршрута.
    # Регрессия: отказ провайдера не считается (забытый вызов в except) — /stats
    # врёт про надёжность. Старые тесты проверяют только сам фолбэк/исключение.
    from types import SimpleNamespace
    chat_id = 999307
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777013)

    async def failing_munito(*args, **kwargs):
        raise bot.MunitoAPIError("503 no free capacity", status_code=503)

    async def ok_gemini(*args, **kwargs):
        return "ok"

    async def failing_gemini(*args, **kwargs):
        raise RuntimeError("и резерв сломан")

    route = [("munito", "probe/m1"), ("gemini", "gemini-3.8-flash")]
    before = dict(bot._stats_entry())
    monkeypatch.setattr(bot, "ask_munito_text", failing_munito)
    monkeypatch.setattr(bot, "ask_gemini", ok_gemini)
    ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, incoming, allow_stream=False))
    assert ans == "ok" and sent is False
    mid = dict(bot._stats_entry())
    assert mid["fallbacks"] - before.get("fallbacks", 0) == 1
    assert mid["answers_sent"] - before.get("answers_sent", 0) == 1
    assert mid.get("all_failed", 0) == before.get("all_failed", 0)

    monkeypatch.setattr(bot, "ask_gemini", failing_gemini)
    # Наверх идёт ошибка последнего блока: её маппит _route_error_reply_text.
    with pytest.raises(RuntimeError, match="и резерв сломан"):
        asyncio.run(bot._run_route(chat_id, "привет", route, incoming, allow_stream=False))
    after = bot._stats_entry()
    assert after["fallbacks"] - mid.get("fallbacks", 0) == 1
    assert after["all_failed"] - mid.get("all_failed", 0) == 1
    assert after.get("answers_sent", 0) == mid.get("answers_sent", 0)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777013", None)
    bot.chat_state.pop(chat_id, None)


def test_daily_limit_denial_counted_and_refused(rate_guard_setup, monkeypatch):
    # Что защищает: счётчик отказов по дневному лимиту вместе с самим отказом.
    # Регрессия: отказ есть, а счётчик не растёт — /stats врёт. Старые тесты
    # проверяют только тексты отказов и фолбэк на резерв, но не счётчик.
    message = rate_guard_setup()
    message.text = "привет, как дела?"
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    uid = 456
    entry = bot._user_daily_entry(uid)
    entry["total"] = bot._user_daily_limit(uid, "total")
    before = dict(bot._stats_entry())
    fake_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", fake_route)
    asyncio.run(bot._handle_message_core(message))
    fake_route.assert_not_awaited()
    after = bot._stats_entry()
    assert after["daily_limit_denials"] - before.get("daily_limit_denials", 0) == 1
    bot._safe_reply.assert_awaited_once()
    bot.chat_state.pop(123, None)


@pytest.mark.parametrize("with_usage", [True, False])
def test_gemini_success_logs_token_usage(caplog, with_usage):
    # Что защищает: единственную точку калибровки лимитов по логам для Gemini
    # (usage_metadata.prompt_token_count/candidates_token_count/total_token_count).
    # Регрессия: тихий дроп usage при рефакторинге ask_gemini — счётчики пропадут из
    # логов, лимиты не на чем калибровать. Старые тесты проверяют только текст/историю.
    # Без usage от API строка [usage] не пишется, лог не засоряется пустышками.
    import logging
    from types import SimpleNamespace
    chat_id = 999901

    def fake_generate_content(*, model, contents, config=None):
        resp = _FakeGeminiResponse(text="Привет!")
        if with_usage:
            resp.usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=20, total_token_count=30)
        return resp

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    try:
        with caplog.at_level(logging.INFO, logger="bot"):
            answer = asyncio.run(bot.ask_gemini(chat_id, "Привет"))
        assert answer == "Привет!"
        usage_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[usage]")]
        if with_usage:
            assert len(usage_lines) == 1
            line = usage_lines[0]
            assert f"chat={chat_id}" in line and "provider=gemini" in line
            assert "prompt=10" in line and "completion=20" in line and "total=30" in line
        else:
            assert usage_lines == []
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        caplog.clear()


def test_munito_success_logs_token_usage_when_api_returns_it(caplog, monkeypatch):
    # Что защищает: точку калибровки лимитов для текстового провайдера (поле
    # usage.prompt_tokens/completion_tokens/total_tokens — общий цикл
    # _chat_completion_chain). Регрессия: потеря usage при правках цикла. Старые
    # тесты мокают ответы без usage и строку [usage] не ищут.
    import logging
    chat_id = 999903

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": "ответ"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}}

    monkeypatch.setattr(bot, "_munito_request", fake_munito_request)
    try:
        with caplog.at_level(logging.INFO, logger="bot"):
            asyncio.run(bot.ask_munito_text(chat_id, "привет", model_chain=["probe/m1"]))
        usage_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[usage]")]
        assert len(usage_lines) == 1
        line = usage_lines[0]
        assert f"chat={chat_id}" in line and "provider=munito" in line and "model=probe/m1" in line
        assert "prompt=11" in line and "completion=22" in line and "total=33" in line
    finally:
        bot.chat_state.pop(chat_id, None)
        caplog.clear()

