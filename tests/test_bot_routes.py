"""test_bot_routes.py — LLM routing: error classification, ask_gemini/ask_munito, _run_route.

Split from test_bot.py (P2 audit); shared fakes are in bot_test_helpers.py.
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
    # Guard for A8-04: explicit HTTP/status patterns must still be recognized.
    assert bot._error_status(_FakeExc(text), text) == expected


@pytest.mark.parametrize("text", [
    "429 токенов",
    "лимит исчерпан: 429 запросов",
    "500 попыток",
    "no numbers here",
])
def test_error_status_ignores_bare_numbers(text):
    # Regression A8-04: a bare number without a template is not a status ("429 tokens" produced a false 429).
    assert bot._error_status(_FakeExc(text), text) is None


def test_gemini_error_msg_rate_limit():
    # REGRESSION (technical-debt audit): this previously checked "model via /model"—
    # the /model command was removed long ago (see README, "Automatic model routing"),
    # and suggesting it in the error text was a direct disservice to the user.
    # _gemini_error_msg/_or_error_msg now use shared provider-neutral
    # templates (see _MODEL_ERROR_MESSAGES) without mentioning nonexistent commands.
    exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    msg = bot._gemini_error_msg(exc, "gemini-3.5-flash")
    assert "/model" not in msg and "/provider" not in msg
    assert msg == bot._model_error_text("rate_limit")


def test_user_mime_label_never_becomes_markup():
    # A client-side MIME type in error text must not render as a link or tag
    # (confirmed run-2 finding, low), but ordinary types with parameters are alive.
    import lumen_formatting
    import lumen_routes
    label = lumen_routes._user_mime_label
    assert label("[Google](https://evil.example)") == "неизвестный тип"
    assert label("image/<b>x") == "неизвестный тип"
    assert label("тип/файл") == "неизвестный тип"
    assert label(None) == "неизвестный тип"
    assert label("application/json; charset=utf-8") == "application/json; charset=utf-8"
    assert label("application/pdf") == "application/pdf"
    rendered = lumen_formatting._md_to_html(f"Тип вложения '{label('[Google](https://evil.example)')}' не поддерживается.")
    assert "<a " not in rendered and "<b>" not in rendered


def test_gemini_error_msg_value_error_passthrough():
    # Only UserFacingInputError is used in ask_gemini as a ready
    # user text; all other ValueError errors use the common template.
    assert bot._gemini_error_msg(bot.UserFacingInputError("кастомная ошибка"), "gemini-3.5-flash") == "кастомная ошибка"
    assert bot._gemini_error_msg(ValueError("/secret/path model-xyz"), "gemini-3.5-flash") != "/secret/path model-xyz"


def test_gemini_error_msg_all_models_exhausted():
    # REGRESSION (tech-debt audit): this previously checked '/provider'; the command
    # removed, with no actual way to switch manually to the reserve provider
    # no longer exists, so offering it in the error text was a mistake.
    exc = bot.GeminiAllModelsExhaustedError(["gemini-3.5-flash", "gemini-2.5-flash"])
    msg = bot._gemini_error_msg(exc, "gemini-3.5-flash")
    assert "/provider" not in msg
    assert "limit" in msg.lower()
    assert "ліміт" in bot._gemini_error_msg(exc, "gemini-3.5-flash", "uk").lower()


def test_or_error_msg_rate_limit():
    # REGRESSION (technical debt audit): "/provider" was removed from the text (the command was deleted),
    # and the wording "fallback provider" was also removed—with automatic
    # the OpenRouter router often turns out to be FIRST, not a reserve candidate.
    exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    msg = bot._or_error_msg(exc)
    assert "/provider" not in msg and "резервн" not in msg.lower()
    assert msg == bot._model_error_text("rate_limit")


def test_or_error_msg_unavailable():
    exc = _FakeStatusExc("model not found", status_code=404)
    msg = bot._or_error_msg(exc)
    assert "/provider" not in msg and "резервн" not in msg.lower()
    assert msg == bot._model_error_text("unavailable")


def test_model_error_text_shared_between_providers():
    # Single source of truth for error text (see the technical-debt audit) — Gemini and
    # OpenRouter should show IDENTICAL text for the same error class,
    # rather than inconsistent wording in two places.
    gem_exc = _FakeStatusExc("resource_exhausted", status_code=429)
    or_exc = _FakeStatusExc("rate limit exceeded", status_code=429)
    assert bot._gemini_error_msg(gem_exc, "gemini-3.5-flash") == bot._or_error_msg(or_exc, "text")


def test_user_facing_error_texts_hide_models_and_stay_informal():
    # The single Lumen legend (September 2026): user texts must not
    # mention “models” in the plural and address the person as “you” — see
    # IDENTITY and ADDRESS in system_prompt.py.
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
    # Regression covering the entire purpose of the output filter: if the system prompt is nevertheless bypassed
    # through injection, and the model revealed a real identity — neither the user nor the history
    # in the chat must not see/save the original (leaked) text, only the fallback.
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
    # REGRESSION (24.07.2026): previously the fallback option for an empty model_chain
    # a hardcoded literal for a dead model. Replace the order with our own list:
    # the test fails if the default is restored as a literal instead of a read from the head
    # _MUNITO_ORDER (Munito has text and vision in one chain).
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
    # Key requirement: if the entire first-provider route failed,
    # the router should try the reserve with a DIFFERENT provider rather than give up immediately.
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
        # The last route error is raised; its text is mapped for the user
        # _route_error_reply_text, so the last provider is what matters.
        with pytest.raises(RuntimeError, match="тоже сломано"):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
        assert tried == ["munito", "gemini"]
    finally:
        bot.ask_munito_text = original_munito_text
        bot.ask_gemini = original_ask_gemini


def test_shared_history_between_gemini_and_munito(monkeypatch):
    # Regression for the requirement "shared memory between Gemini and the text provider,
    # so the switch between models is not noticeable" — previously each
    # the provider had its own SEPARATE history (gemini_history/or_history).
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
        # Both records must be in ONE and the same history, not in separate
        assert "Первый вопрос (через Gemini)" in contents
        assert "Ответ от Gemini" in contents
        assert "Второй вопрос (через Munito)" in contents
        assert "Ответ от Munito" in contents
        assert len(history) == 4
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_ask_gemini_retries_without_tools_on_malformed_function_call():
    # Regression test: after extracting _build_gemini_call_config, the kwargs variable
    # that this retry path relied on was removed; retry_gconfig now
    # is built via gconfig.model_copy(update={"tools": None}).
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
        # the first call uses tools (url_context is enabled in DEFAULT_GEMINI_MODEL)
        assert getattr(call_configs[0], "tools", None)
        # the second (after retry) — now without tools
        assert getattr(call_configs[1], "tools", None) is None
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_extract_gemini_answer_skips_retry_when_route_budget_spent():
    # A retry after a broken call must not wait the full TELEGRAM_AI_TIMEOUT on top of
    # spent budget; otherwise the route holds the chat lock for
    # ROUTE_TOTAL_BUDGET_SEC.
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
        # A nonempty "[Response blocked...]" used to be returned here, which
        # ask_gemini accepted as success (hostile review 27.09.2026)—now empty,
        # and the route moves to the next model.
        assert ans == ""
    finally:
        bot.client = original_client


def test_extract_gemini_answer_returns_empty_for_blocked_response():
    # A block (SAFETY) is not an answer: an empty string advances the chain,
    # rather than slipping the user a Russian technical stub (audit 27.10.2026).
    resp = _FakeGeminiResponse(text="", candidates=[_FakeCandidate(finish_reason="SAFETY")])
    ans = asyncio.run(bot._extract_gemini_answer_text(
        resp, model_id="gemini-3.8-flash", call_contents=[], gconfig=None,
    ))
    assert ans == ""


def test_extract_gemini_answer_retries_without_tools_on_tool_only_response():
    # Production 07.10.2026: the model returned only function_call without text, and the user saw "[Tool call: ...]".
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
    # The retry after a broken call failed itself (rather than merely exceeding the
    # budget), or
    # returned empty — return empty so the route moves to the next model.
    # Previously, the user received "[Response blocked...]" as a finished answer.
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
    # Integrate reorder into _run_route: a model with a measured 100s
    # ask is already called with a reordered chain.
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
    # Prod case 17.09.2026: the user twice saw the literal "Empty
    # response" — an empty response must advance the chain rather than go to the chat.
    # A provider placeholder (non-empty string) is caught by the same condition:
    # the 05.11.2026 backup sample — 7 such answers in one dialog.
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
    # Backup sample from 05.11.2026: previous providers wrote None to provenance —
    # could not be tied to a model, and an output-cap truncation was invisible everywhere.
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
    # Prod case from the 05.11.2026 backup: the user's entire answer was 39 characters
    # "User Safety: safe" from the head model; a nonempty string passed the check.
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


def test_chain_recomputes_reasoning_effort_after_model_fallback(monkeypatch):
    # Measurement 12.11.2026: google/gemma-4-12b returns 400 for reasoning_effort, while
    # four text heads accept it. A field computed once for the entire
    # route, it would go both ways: on gemma — a guaranteed 400, on
    # head, producing an answer with no effort at all. Therefore extras are recalculated per model
    # the attempt (extra_payload_fn), and this test locks in the recalculation.
    import lumen_routes
    from lumen_router_config import _munito_payload_extras

    monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    gemma = "google/gemma-4-12b"
    head = "deepseek-ai/deepseek-v4.1-flash-1017"
    seen: dict[str, dict] = {}

    async def fake_request(payload, deadline):
        seen[payload["model"]] = payload
        if payload["model"] == gemma:
            raise bot.MunitoAPIError("HTTP 400 'reasoning_effort' is not supported", status_code=400)
        return {"choices": [{"message": {"content": "живой ответ"}}]}

    try:
        answer, used = asyncio.run(lumen_routes._chat_completion_chain(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            [gemma, head], gemma, request_fn=fake_request, provider="munito",
            extra_payload_fn=lambda model: _munito_payload_extras("medium", model),
        ))
    finally:
        # Real model IDs do not remain in the shared test-session state.
        lumen_router_config._QUARANTINE.pop(("munito", gemma), None)
        lumen_router_config._QUARANTINE.pop(("munito", head), None)
        bot.GLOBAL_QUOTA.setdefault("munito", {}).pop(gemma, None)
        bot.GLOBAL_QUOTA.setdefault("munito", {}).pop(head, None)

    assert (answer, used) == ("живой ответ", head)
    assert set(seen) == {gemma, head}, "обе модели обязаны получить свой payload"
    assert "reasoning_effort" not in seen[gemma], "gemma получила бы 400 на это поле"
    assert seen[head]["reasoning_effort"] == "medium", "после откатa поле обязано пересчитаться"


def test_chain_empty_responses_feed_quarantine_counter(monkeypatch):
    # Three consecutive empty responses from one head trigger quarantine (a real regression: a dead head
    # would chew on every attempt until the end of the day instead of backing off after the third).
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
    # Junk is delivered as before (without substitution), but counts toward the counter: three such —
    # and the head is in quarantine.
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
    # The same mush, but the tokens were already in the request: the model repeated after
    # by a user — that is an echo; do not feed quarantine (otherwise anyone could disable models).
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
    # Regression (audit 26.09.2026): every attempt waited the full ROUTE_MODEL_TIMEOUT_SEC,
    # so the route held the chat lock for 40+22s instead of the stated 40s.
    assert bot._attempt_timeout(bot, None) == bot.ROUTE_MODEL_TIMEOUT_SEC
    assert bot._attempt_timeout(bot, time.monotonic() + 300.0) == bot.ROUTE_MODEL_TIMEOUT_SEC
    tight = bot._attempt_timeout(bot, time.monotonic() + 4.0)
    assert 0.5 <= tight <= 4.0
    # An expired budget means a fast failure, not another 22 s of waiting.
    assert bot._attempt_timeout(bot, time.monotonic() - 5.0) <= 1.0


def test_ask_gemini_attempt_respects_short_budget(monkeypatch):
    # Short route budget: the main attempt must not consume the entire remainder
    # entirely — otherwise the answer arrives after ROUTE_TOTAL_BUDGET_SEC.
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
        # The attempt was cut off by the remaining budget, and the route stopped immediately —
        # main thing is that it did not go far beyond the declared budget.
        elapsed = time.monotonic() - started
        assert elapsed < 3, f"маршрут с бюджетом 0.6с обязан остановиться быстро: {elapsed:.1f}с"
        assert calls == ["m1"]
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_is_gemini_daily_quota_recognizes_google_per_day_metric():
    # Google writes the daily quota without a space in the metric name
    # (GenerateRequestsPerDayPerProjectPerModel); without a perday token it
    # would look like a minute-level burst and receive a short cooldown.
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
    # Production 28.10.2026: the stream disappeared entirely when the primary model failed
    # before the first chunk, — the reserves replied with a regular call. Now the stream
    # tried on them too, reusing the same bubble.
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
        # The wait timer is shared across the route: switching to the reserve does not reset it to 0.
        assert calls[0][2] is not None and calls[0][2] == calls[1][2]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_run_route_falls_back_from_failed_munito_stream_to_non_streaming():
    chat_id = 999306

    async def fake_munito_stream_fail(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return None, None  # an early failure without a placeholder (for example, message.reply itself
        # failed)

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
        # An ordinary call traverses the entire block: a model whose stream failed earlier
        # after display, retry without streaming; otherwise a provider's broken SSE left
        # would leave the chat with no response at all (audit 28.10.2026).
        assert calls[0] == ["deepseek-ai/deepseek-v4.1-flash-1017", "deepseek-ai/deepseek-v4-flash"]
    finally:
        bot._try_munito_streaming = original_stream
        bot.ask_munito_text = original_munito_text


def test_run_route_reuses_stream_placeholder_when_fallback_succeeds():
    # Regression for a real bug found during testing: previously, after a failed
    # during streaming, the "…" placeholder was immediately deleted, while the next model sent
    # an entirely new message — visually it looked like "the dots disappeared, then
    # an answer appeared out of nowhere in a single block.” The placeholder must now
    # be reused (edited) by the reserve model's final response.
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
        # sent=True means the answer has already been "delivered" by editing the placeholder,
        # rather than through a separate new _safe_reply in _handle_message_core.
        assert sent is True
        assert placeholder.deleted is False
        assert placeholder.edits[-1][0] == "Ответ от резервной модели"
    finally:
        bot._try_munito_streaming = original_stream
        bot.ask_munito_text = original_munito_text


def test_run_route_reuse_has_no_thought_block_in_private_chat(monkeypatch):
    # A plain response after an early stream failure also remains without a synthetic
    # lines like "Thinking Ns"; the activity block is needed only for real stages.
    chat_id = 999322
    placeholder = _FakeSentMessage()
    incoming = _FakeIncomingMessage(chat_id)

    async def fake_stream_fail(cid, prompt, message, model_id, deadline=None, status=None, **kwargs):
        return None, placeholder

    async def fake_text(cid, prompt, model_chain, deadline=None, **kwargs):
        await asyncio.sleep(1.05)
        return "Ответ от резервной модели"

    monkeypatch.setattr(bot, "_try_munito_streaming", fake_stream_fail)
    monkeypatch.setattr(bot, "ask_munito_text", fake_text)
    route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017"),
             ("munito", "deepseek-ai/deepseek-v4-flash")]
    ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=incoming, allow_stream=True))
    assert ans == "Ответ от резервной модели"
    assert sent is True
    final_text = placeholder.edits[-1][0]
    assert final_text == "Ответ от резервной модели"
    assert "Thought for" not in final_text
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
    # Models WITH system_instruction (not no_system) do not go through the fake
    # branch at all
    # Identity fields travel in the body, not the headers: contents
    # must remain untouched, while the full text goes through system_instruction, as before.
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
    # Google daily quota marks the model until morning and moves the route to the next one.
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
    # Regression (hostile review 27.09.2026): ANY Gemini 429 set a daily
    # marker, and one burst of the per-minute limit removed the model from the route until midnight.
    # The per-minute limit should cause only a short cooldown.
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
        # The cooldown expires on its own — no need to wait a day.
        entry["cooldown_until"] = time.time() - 1.0
        assert lumen_router_config._is_quota_exhausted("gemini", "gemini-3.6-flash") is False
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)


def test_chat_completion_chain_cools_down_model_on_429():
    # A non-streaming text provider must write cooldown after 429—a mirror
    # streaming, otherwise _is_quota_exhausted does not see it and the model is hammered again.
    # Munito limits are windowed, so use cooldown rather than a daily mark.
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
    # Prod 05.10.2026: Gemini 3.x models returned 503 and retried on every
    # message, up to 22 seconds and several wasted attempts. 503 is not a quota, but
    # the model on QUOTA_RATE_LIMIT_COOLDOWN_SEC must be allowed through. It fails on an old
    # code where "unavailable" left no marks. Production 05.11.2026: Munito had the same 503
    # "no free capacity" on the free tier.
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
    # The same scenario on Gemini's text path: a 503 from the chain head must
    # keep the cooldown; otherwise the model retries on every request.
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
    # Only minute-long spikes: the daily quota was not exhausted — the last 429
    # is passed through as-is, rather than an empty "everything exhausted".
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
    # The previous version used blocking time.sleep in an async fake and the same
    # the same response text for both models: the timeout never fired, and the fallback did not
    # was present, yet the test still passed (adversarial review 27.09.2026). Now the fake
    # is asynchronous, the texts differ, and both calls and timing are checked.
    chat_id = 999012
    calls = []
    original_timeout = bot.ROUTE_MODEL_TIMEOUT_SEC

    async def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            await asyncio.sleep(5.0)
        # Neutral texts without model names: a model name in the answer triggers
        # a personal-data leak scrubber and replaces the response with a stub.
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
    # Both a windowed 429 and an apparently permanent 403 continue to the next
    # of the model: exactly one attempt per model, regardless of error classification.
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
    # The whole chain failed—the LAST model's error propagates upward (its mapper
    # _route_error_reply_text), not the first one: the user must see the status
    # fallback, not the head. Regression: returning the first error would show 'model
    # unavailable" when the fallback was alive and failed for another reason.
    async def fake_request(payload, deadline):
        raise bot.MunitoAPIError(f"сбой {payload['model']}", status_code=500)

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    with pytest.raises(bot.MunitoAPIError, match="сбой probe/model-b"):
        asyncio.run(lumen_routes._chat_completion_chain(
            messages, ["probe/model-a", "probe/model-b"], "probe/model-a",
            request_fn=fake_request, provider="munito",
        ))


def test_ask_munito_multimodal_sends_all_album_images_not_just_first(monkeypatch):
    # External audit: only the first photo in an album was sent; the remaining 2-9 were silently lost.
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
    # Patch the OpenAI-compatible Munito before the Gemini model, while leaving the /draw chain unchanged.
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
    # Video only, with no images — nothing to send to Munito: an error sends the route to Gemini.
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
    # Defense in depth for the text provider: the key must not appear in error text.
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
    # Product boundaries as of 10.2026: reverting to "minimal filters" while allowing explicit content would pass the old checks.
    for gone in (
        "minimal filters",
        "You may discuss adult topics, sexual content",
    ):
        assert gone not in system_prompt.SYSTEM_PROMPT


def test_get_system_prompt_header_english():
    assert "CURRENT TIME INFORMATION" in bot.get_system_prompt()


def test_ask_munito_text_success_records_munito_quota(monkeypatch):
    # A successful response is charged to the "munito" quota, as with other providers:
    # /stats and /diag read exactly this.
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
    # What this protects: the munito provider in _run_route, on both the regular
    # path (ask_munito_text)
    # and in the streaming probe (_streamable/_try_stream). Regression: without a text path
    # ("munito", …), all ordinary text would fall through to the Gemini reserve and burn its daily
    # quota, while without "_streamable" the chat lost character-by-character output on Munito.
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
    # protected behavior: low-effort reaches the request body on the text path.
    # Regression: a field added in one place (streaming) and lost in another would mean
    # ordinary answers still consume reasoning tokens, while an empty answer from Qwen
    # is returned.
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
    # Without a key, return a clear error instead of making a network call, and do
    # not expose the API response
    # raw to the user (it may contain anything, including external data).
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
    # 08.10.2026: the model_attempts/model_failures counters show the real success rate
    # of the route (previously, answers were split into attempts manually from logs).
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
    # The common loop must not spend a second attempt if the route budget has expired.
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
    # A service call is not charged to quota: /stats and exhausted triggers concern only answers to people.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "  привет мир  "}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    bot.GLOBAL_QUOTA.setdefault("groq", {}).pop("whisper-large-v3-turbo", None)
    try:
        assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) == "привет мир"
        assert "whisper-large-v3-turbo" not in bot.GLOBAL_QUOTA.get("groq", {})
    finally:
        bot.GLOBAL_QUOTA.get("groq", {}).pop("whisper-large-v3-turbo", None)


def test_record_quota_usage_service_flag_skips_counter():
    # An ordinary call is counted; an internal call is not (summaries/transcription).
    bot.GLOBAL_QUOTA.setdefault("groq", {}).pop("svc-probe-model", None)
    try:
        bot._record_quota_usage("groq", "svc-probe-model", service=True)
        assert "svc-probe-model" not in bot.GLOBAL_QUOTA.get("groq", {})
        bot._record_quota_usage("groq", "svc-probe-model")
        assert bot.GLOBAL_QUOTA["groq"]["svc-probe-model"]["used"] >= 1
    finally:
        bot.GLOBAL_QUOTA.get("groq", {}).pop("svc-probe-model", None)


def test_record_quota_usage_clears_cooldown_and_exhausted_marks():
    # A successful response clears both the daily mark and the cooldown; otherwise a model that once
    # that hit the limit would keep the mark even after live answers.
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
    # An empty transcription counts as failure: the caller follows the previous path.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "   "}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", "audio/ogg", 123)) is None


def test_transcribe_audio_truncates_huge_transcript(monkeypatch):
    # An hour of speech without trimming would hit the limits of models farther down the route.
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
    # Deleting the Groq chat does not disable key-leak protection: Whisper logs text
    # a network exception, and without the replacement GROQ_API_KEY would get in there (it is in logs
    # can be read by anyone — Spaces hosting, aggregators).
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
    # Audit A4-03: Whisper waited for the full ROUTE_MODEL_TIMEOUT_SEC outside the route budget.
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
    # A2-12 audit: transcription waited for the remainder of the entire budget and starved the model —
    # now a separate smaller limit on top of the remainder.
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
    # Audit A4-05: history was silently sliced instead of using _trim_history with a summary.
    # Regression: summaries get cut (an expensive call) — and then long turns disappear without a trace.
    import lumen_chat_state
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
        for i in range(205)
    ]
    try:
        asyncio.run(bot.ask_munito_text(chat_id, "новый вопрос", model_chain=["probe/m1"]))
        state = bot.chat_state[chat_id]
        history = state["history"]
        assert len(history) == bot.HISTORY_SUMMARIZE_KEEP
        assert state["memory_summary"].startswith("итог: погода и коты")
        assert state["memory_pending"] == []
        assert history[-2:] == [
            {"role": "user", "content": "новый вопрос"},
            {"role": "assistant", "content": answer},
        ]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_openai_turn_messages_carry_summary_authors_and_past_photo(monkeypatch):
    # Memory for the model: the summary in the system prompt, author/time for replies,
    # the past photo as a separate reply — the model sees it without the word “photo.”
    chat_id = 999812
    state = bot.get_state(chat_id)
    now = time.time()
    state["history"] = [
        {"role": "user", "content": "лови кота", "sender": "@m1st", "sender_id": 7, "ts": now - 60},
        {"role": "assistant", "content": "Кот хорош."},
        {"role": "user", "content": "", "sender": "@m1st", "sender_id": 7, "ts": now - 30,
         "media": [{"file_id": "p1", "mime": "image/jpeg"}]},
        {"role": "assistant", "content": "На фото рыжий кот."},
    ]
    state["memory_summary"] = "Договорились про кота."
    fetched = []

    async def fake_fetch(fid, mime):
        fetched.append(fid)
        return b"img", "image/jpeg"

    monkeypatch.setattr(bot, "_fetch_media", fake_fetch)
    try:
        msgs = asyncio.run(bot._build_openai_turn_messages(chat_id, "а какой окрас?", "probe/m1"))
        assert "LONG-TERM MEMORY" in msgs[0]["content"]
        assert "Договорились про кота." in msgs[0]["content"]
        assert any("[@m1st, " in m["content"] for m in msgs if isinstance(m.get("content"), str))
        assert any("[фото]" in m["content"] for m in msgs if isinstance(m.get("content"), str))
        assert any(
            isinstance(m.get("content"), list)
            and any(part.get("type") == "image_url" for part in m["content"])
            for m in msgs
        )
        assert fetched == ["p1"]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_openai_turn_messages_respects_attach_past_and_skip(monkeypatch):
    # An explicit "show a photo" reference already travels as the current attachment: a duplicate of the previous photo
    # do not attach them; also skip other people's file_id values from the current request.
    chat_id = 999815
    state = bot.get_state(chat_id)
    now = time.time()
    state["history"] = [
        {"role": "user", "content": "первое", "sender": "@m1st", "sender_id": 7, "ts": now - 30,
         "media": [{"file_id": "p1", "mime": "image/jpeg"}]},
        {"role": "user", "content": "второе", "sender": "@m1st", "sender_id": 7, "ts": now - 20,
         "media": [{"file_id": "p2", "mime": "image/jpeg"}]},
        {"role": "user", "content": "третье", "sender": "@m1st", "sender_id": 7, "ts": now - 10,
         "media": [{"file_id": "p3", "mime": "image/jpeg"}]},
    ]
    fetched = []

    async def fake_fetch(fid, mime):
        fetched.append(fid)
        return b"img", "image/jpeg"

    monkeypatch.setattr(bot, "_fetch_media", fake_fetch)
    try:
        asyncio.run(bot._build_openai_turn_messages(
            chat_id, "что на фото?", "probe/m1",
            history_meta={"attach_past": False, "skip_file_ids": ["p3"]},
        ))
        assert fetched == []
        fetched.clear()
        asyncio.run(bot._build_openai_turn_messages(
            chat_id, "что на фото?", "probe/m1",
            history_meta={"attach_past": True, "skip_file_ids": ["p3"]},
        ))
        assert fetched == ["p1", "p2"]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_trimmed_summary_is_accepted_by_the_next_request(monkeypatch):
    # The summary is not lost between truncation and the next request: it is kept in the system.
    import lumen_chat_state
    chat_id = 999816
    state = bot.get_state(chat_id)
    state["history"] = [
        {"role": "user", "content": f"старое {i}"} for i in range(205)
    ]

    async def fake_summarize(text):
        return "Люди: m1st. Факты: кот рыжий."

    monkeypatch.setattr(lumen_chat_state, "_summarize_text", fake_summarize)
    try:
        asyncio.run(bot._trim_history(state["history"], state))
        msgs = asyncio.run(bot._build_openai_turn_messages(chat_id, "новый вопрос", "probe/m1"))
        assert "Люди: m1st. Факты: кот рыжий." in msgs[0]["content"]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_gemini_call_config_includes_memory_summary():
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    _, gconfig = bot._build_gemini_call_config(
        bot.DEFAULT_GEMINI_MODEL, contents, memory_summary="Люди: m1st.",
    )
    assert "LONG-TERM MEMORY OF THIS CHAT" in gconfig.system_instruction
    assert "Люди: m1st." in gconfig.system_instruction


@pytest.mark.parametrize("mode", ["text", "vision"])
def test_munito_keeps_media_note_out_of_history(monkeypatch, mode):
    # The marker is one-time (like ask_gemini): history gets clean text, otherwise old
    # markers will surface on subsequent turns. The vision branch has its own history write line,
    # so we check both inputs.
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
    # Audit A4-06: cancellation mid-answer left "…" in the chat forever.
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
    # Regression: _run_route ignored its head argument; Gemini therefore failed
    #     over to itself.
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
    # New, paired with the previous one: a failed call does not count.
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
    # What it protects: the route executes blocks in their assembled order, and blocks
    # from one provider are not merged into one call (audit 27.10.2026: merging
    # moved the fallback ahead of an independent provider and consumed its
    # budget.
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
    # What it protects: the user's daily Gemini limit. Regression: a Munito answer
    # would count as Gemini, and a few ordinary messages would lock voice/video
    # and links for a day (the 27.10.2026 audit concerned Gemma; since 05.11.2026 the same meaning
    # at Munito: it does not spend Google's quota).
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
    # What it protects: the accounting funnel after a processed message (quota
    # provider + user_daily + daily /stats counters in one pass).
    # Regression: a silent no-count after refactoring (the wrong model, a service flag not
    # there, a lost call) — /stats shows permanent zeros even though people use the bot.
    # Old tests mock the entire _run_route (they do not check usage) or check
    # only the ask level, without user_daily/stats.
    from collections import deque
    message = rate_guard_setup()
    message.text = "привет, как дела?"
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    # Streaming in tests without network: suppress attempts, then use the regular text path.
    monkeypatch.setattr(bot, "_try_gemini_streaming", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(bot, "_try_munito_streaming", AsyncMock(return_value=(None, None)))

    async def fake_munito_request(path, method="GET", *, json_body=None, deadline=None, **kwargs):
        return {"choices": [{"message": {"content": "Привет! Как сам?"}}]}

    # The key includes Munito: without it, _build_route sends the entire text to Gemini, and usage
    # of the text provider would stop being checked.
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
        # 05.11.2026: the Gemini daily limit grows only for Gemini answers.
        assert entry["gemini"] == 0
        after = bot._stats_entry()
        assert after["messages_received"] - before.get("messages_received", 0) == 1
        assert after["answers_sent"] - before.get("answers_sent", 0) == 1
        # Munito answers with the head: the reserve was not called, and there are no switches.
        assert after.get("fallbacks", 0) == before.get("fallbacks", 0)
        assert after.get("all_failed", 0) == before.get("all_failed", 0)
    finally:
        bot.chat_state.pop(123, None)


def test_run_route_counts_fallback_and_total_failure(monkeypatch):
    # What this protects: counters of fallback switches and total route failure.
    # Regression: a provider refusal is not counted (a forgotten call in except), so
    # /stats
    # lies about reliability. Old tests checked only the fallback/exception itself.
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
    # The last block's error propagates upward: _route_error_reply_text maps it.
    with pytest.raises(RuntimeError, match="и резерв сломан"):
        asyncio.run(bot._run_route(chat_id, "привет", route, incoming, allow_stream=False))
    after = bot._stats_entry()
    assert after["fallbacks"] - mid.get("fallbacks", 0) == 1
    assert after["all_failed"] - mid.get("all_failed", 0) == 1
    assert after.get("answers_sent", 0) == mid.get("answers_sent", 0)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777013", None)
    bot.chat_state.pop(chat_id, None)


def test_answers_are_counted_once_per_update(monkeypatch):
    # Audit 16.11.2026: a retry after [[SEARCH]], a freshness retry, and
    # auto-continuation re-enter _run_route, and every successful pass
    # added +1 to the daily limit. 30 messages were consumed in 15.
    from types import SimpleNamespace

    chat_id = 999308
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777014)
    monkeypatch.setattr(bot, "ask_munito_text", AsyncMock(return_value="ok"))
    route = [("munito", "probe/m1")]
    saved_stats = dict(bot.GLOBAL_QUOTA.get("stats") or {})

    async def scenario() -> None:
        # Both passes run in one task, as in production: contextvar lives in the context
        # of the handler, rather than in a separate asyncio.run.
        token = bot._ANSWER_COUNTED.set(False)
        try:
            await bot._run_route(chat_id, "раз", route, incoming)
            await bot._run_route(chat_id, "два", route, incoming)
        finally:
            bot._ANSWER_COUNTED.reset(token)

    try:
        asyncio.run(scenario())
        entry = bot.GLOBAL_QUOTA.get("user_daily", {}).get("777014") or {}
        assert entry.get("total") == 1
        assert (bot.GLOBAL_QUOTA.get("stats") or {}).get("answers_sent") == saved_stats.get("answers_sent", 0) + 1
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop("777014", None)
        bot.GLOBAL_QUOTA["stats"] = saved_stats
        bot.chat_state.pop(chat_id, None)


def test_concurrent_text_messages_share_one_daily_slot(rate_guard_setup, monkeypatch):
    # R2-09: separate chats could both pass the last-slot peek before either recorded success.
    uid = 456
    first = rate_guard_setup()
    second = rate_guard_setup()
    second.chat.id = 124
    first.text = second.text = "привет"
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: False)
    calls = []

    async def scenario() -> None:
        async def fake_route(chat_id, ai_prompt, route, message, **kwargs):
            calls.append(chat_id)
            await asyncio.sleep(0)
            bot._record_user_daily(uid)
            return "ok", True

        monkeypatch.setattr(bot, "_run_route", fake_route)
        await asyncio.gather(
            bot._handle_message_core(first, skip_rate_limit=True),
            bot._handle_message_core(second, skip_rate_limit=True),
        )

    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    try:
        asyncio.run(scenario())
        assert len(calls) == 1
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 1
        bot._safe_reply.assert_awaited_once()
        assert "Daily message limit" in bot._safe_reply.await_args.args[1]
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)
        bot.chat_state.pop(124, None)


def test_text_success_consumes_reserved_total_and_gemini_once(rate_guard_setup, monkeypatch):
    # The existing deep success hook must consume the reservation, not add a second total.
    message = rate_guard_setup()
    message.text = "привет"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: False)

    async def fake_route(chat_id, ai_prompt, route, incoming, **kwargs):
        bot._record_user_daily(uid, gemini=True)
        bot._record_user_daily(uid, gemini=True)
        return "ok", True

    monkeypatch.setattr(bot, "_run_route", fake_route)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    try:
        asyncio.run(bot._handle_message_core(message, skip_rate_limit=True))
        entry = bot._user_daily_peek(uid)
        assert entry is not None
        assert (entry["total"], entry["gemini"]) == (1, 1)
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)


def test_sequential_direct_core_calls_consume_one_reservation_each(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "привет"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 2)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: False)

    async def fake_route(chat_id, ai_prompt, route, incoming, **kwargs):
        if not bot._ANSWER_COUNTED.get():
            bot._ANSWER_COUNTED.set(True)
            bot._record_user_daily(uid)
        return "ok", True

    monkeypatch.setattr(bot, "_run_route", fake_route)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    token = bot._ANSWER_COUNTED.set(True)
    try:
        async def scenario():
            await bot._handle_message_core(message, skip_rate_limit=True)
            await bot._handle_message_core(message, skip_rate_limit=True)

        asyncio.run(scenario())
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 2
    finally:
        bot._ANSWER_COUNTED.reset(token)
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)


def test_text_model_failure_refunds_reservation(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "привет"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: False)

    async def fail_route(*args, **kwargs):
        raise RuntimeError("probe model failure")

    monkeypatch.setattr(bot, "_run_route", fail_route)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    try:
        asyncio.run(bot._handle_message_core(message, skip_rate_limit=True))
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0
        replacement = bot._reserve_user_daily(uid, total=1)
        assert replacement is not None
        bot._refund_user_daily(uid, reservation=replacement, total=1)
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)


def test_text_cancellation_refunds_reservation(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "привет"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: False)

    async def scenario() -> None:
        started = asyncio.Event()

        async def wait_forever(*args, **kwargs):
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(bot, "_run_route", wait_forever)
        task = asyncio.create_task(bot._handle_message_core(message, skip_rate_limit=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    try:
        asyncio.run(scenario())
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0
        replacement = bot._reserve_user_daily(uid, total=1)
        assert replacement is not None
        bot._refund_user_daily(uid, reservation=replacement, total=1)
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)


def test_denied_text_reservation_skips_model_and_search(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "что нового сегодня"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: True)
    reserve_calls = []

    def deny_reservation(user_id, *, total):
        reserve_calls.append((user_id, total))
        return None

    model = AsyncMock()
    search = AsyncMock()
    monkeypatch.setattr(bot, "_reserve_user_daily", deny_reservation)
    monkeypatch.setattr(bot, "_run_route", model)
    monkeypatch.setattr("lumen_message_core._gather_web_evidence", search)
    monkeypatch.setattr("lumen_message_core.fetch_web_snippets", search)
    before = dict(bot._stats_entry())
    try:
        asyncio.run(bot._handle_message_core(message, skip_rate_limit=True))
        assert reserve_calls == [(uid, 1)]
        model.assert_not_awaited()
        search.assert_not_awaited()
        bot._safe_reply.assert_awaited_once()
        assert "Daily message limit" in bot._safe_reply.await_args.args[1]
        after = bot._stats_entry()
        assert after["messages_received"] == before["messages_received"]
        assert after["daily_limit_denials"] == before["daily_limit_denials"] + 1
    finally:
        bot.chat_state.pop(123, None)


def test_text_capacity_refusal_is_not_reported_as_personal_limit(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "привет"
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr(bot, "_reserve_user_daily", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "_user_daily_admission_blocked", lambda _uid: True)
    model = AsyncMock()
    monkeypatch.setattr(bot, "_run_route", model)
    try:
        asyncio.run(bot._handle_message_core(message, skip_rate_limit=True))
        model.assert_not_awaited()
        bot._safe_reply.assert_awaited_once()
        assert bot._safe_reply.await_args.args[1] == bot._model_error_text(
            "fallback", bot._chat_lang(message.chat.id),
        )
    finally:
        bot.chat_state.pop(123, None)


def test_text_reservation_without_running_task_skips_paid_work(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "привет"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: False)
    monkeypatch.setattr("lumen_quotas._bind_user_daily_reservation", lambda *args: False)
    model = AsyncMock()
    monkeypatch.setattr(bot, "_run_route", model)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    try:
        asyncio.run(bot._handle_message_core(message, skip_rate_limit=True))
        model.assert_not_awaited()
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0
        bot._safe_reply.assert_awaited_once()
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)


def test_text_early_return_after_reservation_refunds(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "что нового сегодня"
    uid = message.from_user.id
    monkeypatch.setattr(bot, "DAILY_USER_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(bot, "_resolve_incoming_media", AsyncMock(return_value=("", None)))
    monkeypatch.setattr("lumen_message_core.web_search_is_enabled", lambda: True)
    monkeypatch.setattr("lumen_message_core.StatusMessage.start", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "lumen_message_core._gather_web_evidence",
        AsyncMock(return_value=([], [], False, {})),
    )
    monkeypatch.setattr(bot, "_stream_stop_requested", lambda chat_id: True)
    monkeypatch.setattr(bot, "_consume_stream_stop", lambda chat_id: None)
    model = AsyncMock()
    monkeypatch.setattr(bot, "_run_route", model)
    bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
    try:
        asyncio.run(bot._handle_message_core(message, skip_rate_limit=True))
        model.assert_not_awaited()
        entry = bot._user_daily_peek(uid)
        assert entry is not None and entry["total"] == 0
    finally:
        bot.GLOBAL_QUOTA.get("user_daily", {}).pop(str(uid), None)
        bot.chat_state.pop(123, None)


def test_daily_limit_denial_counted_and_refused(rate_guard_setup, monkeypatch):
    # What it protects: the counter of refusals due to the daily limit, along with the refusal itself.
    # Regression: a refusal exists but the counter does not grow; /stats lies. Old tests
    # check only refusal texts and fallback to the reserve, but not the counter.
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
    # What it protects: the only calibration point for Gemini limits based on logs
    # (usage_metadata.prompt_token_count/candidates_token_count/total_token_count).
    # Regression: silent loss of usage during the ask_gemini refactor — counters would disappear from
    # logs, there is nothing to calibrate limits against. Old tests checked only text/history.
    # Without usage from the API, the [usage] line is not written, so the log is not cluttered with empty entries.
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
    # What it protects: the limit calibration point for the text provider (field
    # usage.prompt_tokens/completion_tokens/total_tokens: the shared loop
    # _chat_completion_chain). Regression: usage was lost during loop changes. Old
    # tests mock responses without usage and do not look for the [usage] line.
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


def test_run_route_empty_stream_after_stop_does_not_spend_slot(monkeypatch):
    # Audit 16.11.2026: stopping before the first chunk returned an empty response, but
    # the daily-limit slot had already been deducted.
    chat_id = 999305
    placeholder = _FakeSentMessage()
    recorded = []

    async def fake_stream(cid, prompt, message, model_id, **kwargs):
        return "", placeholder

    def fake_record(user_key, gemini=False):
        recorded.append((user_key, gemini))

    monkeypatch.setattr(bot, "_try_munito_streaming", fake_stream)
    monkeypatch.setattr(bot, "_record_user_daily", fake_record)
    try:
        route = [("munito", "deepseek-ai/deepseek-v4.1-flash-1017")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == ""
        assert recorded == []
    finally:
        bot.chat_state.pop(chat_id, None)
