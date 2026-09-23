"""
test_bot_routes.py — Маршрутизация LLM: классификация ошибок, ask_gemini/ask_openrouter, _run_route.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
import asyncio
import bot
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


def test_classify_model_error_rate_limit_by_status():
    assert bot._classify_model_error(429, "") == "rate_limit"


def test_classify_model_error_rate_limit_by_text():
    assert bot._classify_model_error(None, "quota exceeded") == "rate_limit"


def test_classify_model_error_paid():
    assert bot._classify_model_error(402, "") == "paid"


def test_classify_model_error_forbidden():
    assert bot._classify_model_error(403, "") == "forbidden"


def test_classify_model_error_unavailable():
    assert bot._classify_model_error(404, "") == "unavailable"


def test_classify_model_error_other_for_unknown():
    assert bot._classify_model_error(500, "some random error") == "other"


def test_next_fallback_model_skips_tried():
    assert bot._next_fallback_model({"a"}, ["a", "b", "c"]) == "b"


def test_next_fallback_model_all_tried_returns_none():
    assert bot._next_fallback_model({"a", "b", "c"}, ["a", "b", "c"]) is None


def test_next_fallback_model_none_tried_returns_first():
    assert bot._next_fallback_model(set(), ["a", "b", "c"]) == "a"


def test_error_status_reads_status_code_attribute():
    assert bot._error_status(_FakeExc("x", status_code=429), "x") == 429


def test_error_status_extracts_three_digit_code_from_text():
    exc = _FakeExc("Error 503: unavailable")
    assert bot._error_status(exc, "Error 503: unavailable") == 503


def test_error_status_returns_none_when_no_code_found():
    exc = _FakeExc("no numbers here")
    assert bot._error_status(exc, "no numbers here") is None


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
    # ValueError используется в ask_gemini как готовый пользовательский текст
    # (например про неподдерживаемый тип вложения) — должен вернуться как есть.
    exc = ValueError("кастомная ошибка")
    assert bot._gemini_error_msg(exc, "gemini-3.5-flash") == "кастомная ошибка"


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


def test_ask_openrouter_text_empty_model_chain_fallback_is_not_dead_model():
    # РЕГРЕССИЯ (24.07.2026): раньше запасным вариантом на случай пустого model_chain
    # в ask_openrouter_text было "meta-llama/llama-3.3-70b-instruct:free" — та же
    # модель, что подтверждённо снята провайдером с бесплатного тира (см. README,
    # HTTP 404 "unavailable for free") и по этой же причине уже исключена из
    # _OR_LIGHT_ORDER/_OR_HEAVY_ORDER. Проверяем, что дефолт теперь ссылается на
    # актуальный _OR_LIGHT_ORDER, а не на захардкоженную мёртвую модель.
    chat_id = 999401

    calls = []

    async def fake_or_fallback(messages, trial_models, primary_model_id, **kwargs):
        calls.append(trial_models)
        return "ответ", trial_models[0]

    original = bot._or_chat_completion_with_fallback
    bot._or_chat_completion_with_fallback = fake_or_fallback
    try:
        asyncio.run(bot.ask_openrouter_text(chat_id, "привет", model_chain=[]))
        assert calls[0] == [bot._OR_LIGHT_ORDER[0]]
        assert "meta-llama/llama-3.3-70b-instruct:free" not in calls[0]
    finally:
        bot._or_chat_completion_with_fallback = original
        bot.chat_state.pop(chat_id, None)


def test_run_route_falls_back_to_second_provider_when_first_fully_fails():
    # Ключевое требование: если весь маршрут первого провайдера отказал —
    # роутер должен попробовать резерв в ДРУГОМ провайдере, а не сдаваться сразу.
    chat_id = 999301

    async def failing_or_text(*args, **kwargs):
        raise bot.OpenRouterAPIError("всё сломано", status_code=500)

    async def fake_ask_gemini(cid, prompt, media=None, youtube_url=None, model_chain=None, deadline=None):
        return "Ответ от Gemini (резерв)"

    original_or_text = bot.ask_openrouter_text
    original_ask_gemini = bot.ask_gemini
    bot.ask_openrouter_text = failing_or_text
    bot.ask_gemini = fake_ask_gemini
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("gemini", "gemini-3.1-flash-lite")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
        assert ans == "Ответ от Gemini (резерв)"
        assert sent is False
    finally:
        bot.ask_openrouter_text = original_or_text
        bot.ask_gemini = original_ask_gemini


def test_run_route_raises_when_both_providers_fail():
    chat_id = 999302

    async def failing_or_text(*args, **kwargs):
        raise bot.OpenRouterAPIError("сломано", status_code=500)

    async def failing_gemini(*args, **kwargs):
        raise RuntimeError("тоже сломано")

    original_or_text = bot.ask_openrouter_text
    original_ask_gemini = bot.ask_gemini
    bot.ask_openrouter_text = failing_or_text
    bot.ask_gemini = failing_gemini
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("gemini", "gemini-3.1-flash-lite")]
        with pytest.raises(Exception):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=False))
    finally:
        bot.ask_openrouter_text = original_or_text
        bot.ask_gemini = original_ask_gemini


def test_shared_history_between_gemini_and_openrouter():
    # Регрессия на требование "общая память между Gemini и OpenRouter, чтобы не
    # чувствовалось переключение между моделями" — раньше у каждого провайдера
    # была своя ОТДЕЛЬНАЯ история (gemini_history/or_history).
    chat_id = 999201

    def fake_generate_content(*, model, contents, config=None):
        return _FakeGeminiResponse(text="Ответ от Gemini")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client

    async def fake_or_fallback(messages, trial_models, primary_model_id, **kwargs):
        return "Ответ от OpenRouter", trial_models[0]

    original_or_fallback = bot._or_chat_completion_with_fallback
    bot._or_chat_completion_with_fallback = fake_or_fallback

    try:
        asyncio.run(bot.ask_gemini(chat_id, "Первый вопрос (через Gemini)"))
        asyncio.run(bot.ask_openrouter_text(chat_id, "Второй вопрос (через OpenRouter)", model_chain=["meta-llama/llama-3.3-70b-instruct:free"]))

        history = bot.chat_state[chat_id]["history"]
        contents = [h["content"] for h in history]
        # Обе записи должны быть в ОДНОЙ и той же истории, а не в раздельных
        assert "Первый вопрос (через Gemini)" in contents
        assert "Ответ от Gemini" in contents
        assert "Второй вопрос (через OpenRouter)" in contents
        assert "Ответ от OpenRouter" in contents
        assert len(history) == 4
    finally:
        bot.client = original_client
        bot._or_chat_completion_with_fallback = original_or_fallback
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


def test_run_route_reorders_slow_head_down(monkeypatch):
    # Интеграция reorder в _run_route: модель с измеренными 100с уходит вниз,
    # ask вызывается уже с переупорядоченной цепочкой.
    import lumen_model_speed

    seen = []

    async def fake_ask(chat_id, prompt, model_chain, *, deadline=None):
        seen.append(list(model_chain))
        return "ok"

    monkeypatch.setattr(bot, "ask_openrouter_text", fake_ask)
    lumen_model_speed._latency_ema.clear()
    lumen_model_speed.record_response(
        lumen_model_speed.speed_key("openrouter", "slow:free"), total_sec=100.0)
    incoming = _FakeIncomingMessage(999303)
    try:
        ans, sent = asyncio.run(bot._run_route(
            999303, "Привет", [("openrouter", "slow:free"), ("openrouter", "fast:free")], incoming))
        assert (ans, sent) == ("ok", False)
        assert seen[0][0] == "fast:free"
    finally:
        bot.chat_state.pop(999303, None)
        lumen_model_speed._latency_ema.clear()


def test_or_empty_response_falls_through_to_next_model(monkeypatch):
    # Прод-кейс 17.09.2026: пользователь дважды увидел буквальное "Empty
    # response" — пустой ответ обязан двигать цепочку дальше, а не идти в чат.
    calls = []

    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        calls.append(model)
        if model == "m1:free":
            return {"choices": []}
        return {"choices": [{"message": {"content": "живой ответ"}}]}

    monkeypatch.setattr(bot, "_or_request", fake_or_request)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    answer, used = asyncio.run(bot._or_chat_completion_with_fallback(messages, ["m1:free", "m2:free"], "m1:free"))
    assert (answer, used) == ("живой ответ", "m2:free")
    assert calls == ["m1:free", "m2:free"]


def test_gemini_empty_response_falls_through_to_next_model(monkeypatch):
    chat_id = 999304
    extracts = ["", "хороший ответ"]

    async def fake_extract(resp, *, model_id, call_contents, gconfig):
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


def test_run_route_streams_openrouter_when_route_head_is_openrouter():
    chat_id = 999305

    async def fake_or_stream(cid, prompt, message, model_id):
        return "Стримленный ответ от OpenRouter", None

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("ask_openrouter_text не должен вызываться, если стрим уже сработал")

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream
    bot.ask_openrouter_text = must_not_be_called
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("gemini", "gemini-3.5-flash-lite")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Стримленный ответ от OpenRouter"
        assert sent is True
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_run_route_falls_back_from_failed_openrouter_stream_to_non_streaming():
    chat_id = 999306

    async def fake_or_stream_fail(cid, prompt, message, model_id):
        return None, None  # ранний сбой без плейсхолдера (например, message.reply сам не удался)

    calls = []

    async def fake_or_text(cid, prompt, model_chain, deadline=None):
        calls.append(model_chain)
        return "Ответ без стрима"

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream_fail
    bot.ask_openrouter_text = fake_or_text
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("openrouter", "openai/gpt-oss-20b:free")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ без стрима"
        assert sent is False
        # Модель, для которой стрим не удался, не должна пере-пробоваться внутри
        # обычного вызова — экономим время (см. философию "1 попытка на модель").
        assert calls[0] == ["openai/gpt-oss-20b:free"]
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_run_route_tries_groq_head_with_stream_fallback_to_text():
    # Groq-подключение 21.09.2026: голова-groq стримится через _try_groq_streaming, при раннем сбое — обычный ask_groq_text без пере-пробы упавшей модели.
    chat_id = 999703

    async def fake_groq_stream_fail(cid, prompt, message, model_id):
        return None, None

    calls = []

    async def fake_groq_text(cid, prompt, model_chain, deadline=None):
        calls.append(model_chain)
        return "Ответ Groq без стрима"

    original_stream = bot._try_groq_streaming
    original_groq_text = bot.ask_groq_text
    bot._try_groq_streaming = fake_groq_stream_fail
    bot.ask_groq_text = fake_groq_text
    try:
        route = [("groq", "qwen/qwen3.8-27b"), ("groq", "openai/gpt-oss-120b")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ Groq без стрима"
        assert sent is False
        assert calls[0] == ["openai/gpt-oss-120b"]
    finally:
        bot._try_groq_streaming = original_stream
        bot.ask_groq_text = original_groq_text


def test_run_route_reuses_stream_placeholder_when_fallback_succeeds():
    # Регрессия на реальный найденный при тестировании баг: раньше при неудачном
    # стриме плейсхолдер "…" тут же удалялся, а следующая модель отправляла
    # совсем новое сообщение — визуально выглядело как "точки исчезли, потом
    # из ниоткуда появился ответ одним блоком". Теперь плейсхолдер должен
    # переиспользоваться (редактироваться) финальным ответом резервной модели.
    chat_id = 999307
    placeholder = _FakeSentMessage()

    async def fake_or_stream_fail_with_placeholder(cid, prompt, message, model_id):
        return None, placeholder

    async def fake_or_text(cid, prompt, model_chain, deadline=None):
        return "Ответ от резервной модели"

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream_fail_with_placeholder
    bot.ask_openrouter_text = fake_or_text
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("openrouter", "openai/gpt-oss-20b:free")]
        ans, sent = asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert ans == "Ответ от резервной модели"
        # sent=True означает, что ответ уже "доставлен" через правку плейсхолдера,
        # а не через отдельный новый _safe_reply в _handle_message_core.
        assert sent is True
        assert placeholder.deleted is False
        assert placeholder.edits[-1][0] == "Ответ от резервной модели"
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_run_route_deletes_orphaned_placeholder_when_whole_route_fails():
    chat_id = 999308
    placeholder = _FakeSentMessage()

    async def fake_or_stream_fail_with_placeholder(cid, prompt, message, model_id):
        return None, placeholder

    async def failing_or_text(*args, **kwargs):
        raise bot.OpenRouterAPIError("всё сломано", status_code=500)

    original_stream = bot._try_openrouter_streaming
    original_or_text = bot.ask_openrouter_text
    bot._try_openrouter_streaming = fake_or_stream_fail_with_placeholder
    bot.ask_openrouter_text = failing_or_text
    try:
        route = [("openrouter", "meta-llama/llama-3.3-70b-instruct:free"), ("openrouter", "openai/gpt-oss-20b:free")]
        with pytest.raises(Exception):
            asyncio.run(bot._run_route(chat_id, "привет", route, message=None, allow_stream=True))
        assert placeholder.deleted is True
    finally:
        bot._try_openrouter_streaming = original_stream
        bot.ask_openrouter_text = original_or_text


def test_build_gemini_call_config_skips_all_grounding_tools_for_gemma():
    # Прямая проверка на уровне _build_gemini_call_config: для no_search-модели
    # (обе Gemma) итоговый config не должен включать вообще ни один
    # grounding/url_context инструмент, независимо от прочих флагов в конфиге.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    _, gconfig = bot._build_gemini_call_config("gemma-4-26b-a4b-it", contents)
    assert not getattr(gconfig, "tools", None)


def test_build_gemini_call_config_no_system_model_includes_full_system_prompt():
    # РЕГРЕССИЯ (аудит системного промпта, 10 августа 2026): раньше для no_system-моделей
    # (Gemma) фейковый первый обмен содержал ТОЛЬКО короткий захардкоженный список из 8
    # пунктов — ни строчки из настоящего SYSTEM_PROMPT (system_prompt.py). Gemma полностью
    # пропускала БЛАГОПОЛУЧИЕ И ЗДОРОВЬЕ ПОЛЬЗОВАТЕЛЯ (протокол при сообщении о суициде/
    # самоповреждении — телефон доверия), АВТОРСКИЕ ПРАВА, ОБЪЕКТИВНОСТЬ И НЕПРЕДВЗЯТОСТЬ и
    # т.д. Теперь get_system_prompt(model_id) подставляется в основу этого сообщения —
    # проверяем, что и не встречающиеся в коротком чеклисте разделы теперь реально там есть.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    call_contents, _ = bot._build_gemini_call_config("gemma-4-26b-a4b-it", contents)
    first_user_text = call_contents[0].parts[0].text
    # Системный промпт единый на английском (с сентября 2026).
    assert "8-800-2000-122" in first_user_text
    assert "USER WELLBEING" in first_user_text
    assert "COPYRIGHT" in first_user_text
    assert "EVENHANDEDNESS" in first_user_text
    # Короткий проверенный на практике чеклист (личность/дата/защита от инъекций)
    # сохранён поверх полного промпта, а не заменён им.
    assert "Your name is Lumen" in first_user_text


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


def test_build_gemini_call_config_skips_thinking_override_for_gemma():
    # Gemma не поддерживает thinking_config вообще — no_system уже исключает её.
    contents = [bot.types.Content(role="user", parts=[bot.types.Part.from_text(text="привет")])]
    _, gconfig = bot._build_gemini_call_config("gemma-4-26b-a4b-it", contents)
    assert gconfig is None or gconfig.thinking_config is None


def test_ask_gemini_falls_back_to_next_model_on_quota_exhausted():
    chat_id = 999010
    calls = []

    class _QuotaExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise _QuotaExc("resource_exhausted")
        return _FakeGeminiResponse(text="Ответ от второй модели")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ от второй модели"
        assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
        # Модель, отдавшая 429, должна быть помечена исчерпанной (влияет на будущий роутинг).
        assert bot.GLOBAL_QUOTA["gemini"]["gemini-3.6-flash"]["exhausted_at"] is not None
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop("gemini-3.6-flash", None)


def test_ask_gemini_raises_all_models_exhausted_when_entire_chain_429s():
    chat_id = 999011

    class _QuotaExc(Exception):
        status_code = 429

    def fake_generate_content(*, model, contents, config=None):
        raise _QuotaExc("resource_exhausted")

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


def test_ask_gemini_falls_back_to_next_model_on_timeout():
    chat_id = 999012
    calls = []
    original_timeout = bot.ROUTE_MODEL_TIMEOUT_SEC

    def fake_generate_content(*, model, contents, config=None):
        calls.append(model)
        if model == "gemini-3.6-flash":
            time.sleep(0.5)  # 10-кратный запас над ROUTE_MODEL_TIMEOUT_SEC ниже
        return _FakeGeminiResponse(text="Ответ от второй модели")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(side_effect=fake_generate_content)
    original_client = bot.client
    bot.client = fake_client
    bot.ROUTE_MODEL_TIMEOUT_SEC = 0.05
    try:
        answer = asyncio.run(bot.ask_gemini(chat_id, "Привет", model_chain=["gemini-3.6-flash", "gemini-2.5-flash"]))
        assert answer == "Ответ от второй модели"
        assert calls[0] == "gemini-3.6-flash"
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


def test_or_chat_completion_with_fallback_switches_model_on_rate_limit():
    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        if model == "model-a":
            raise bot.OpenRouterAPIError("rate limit exceeded", status_code=429)
        return {"choices": [{"message": {"content": "ответ от model-b"}}]}

    original = bot._or_request
    bot._or_request = fake_or_request
    try:
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        answer, used = asyncio.run(bot._or_chat_completion_with_fallback(messages, ["model-a", "model-b"], "model-a"))
        assert answer == "ответ от model-b"
        assert used == "model-b"
    finally:
        bot._or_request = original


def test_or_chat_completion_with_fallback_switches_model_on_permanent_looking_error():
    # Даже "постоянная" на вид ошибка (403 forbidden) не должна обрывать переход
    # к следующей модели — при attempts_per_model=1 переход к следующей модели
    # происходит независимо от классификации (см. комментарий в самой функции).
    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        if model == "model-a":
            raise bot.OpenRouterAPIError("forbidden", status_code=403)
        return {"choices": [{"message": {"content": "ответ от model-b"}}]}

    original = bot._or_request
    bot._or_request = fake_or_request
    try:
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        answer, used = asyncio.run(bot._or_chat_completion_with_fallback(messages, ["model-a", "model-b"], "model-a"))
        assert answer == "ответ от model-b"
        assert used == "model-b"
    finally:
        bot._or_request = original


def test_ask_openrouter_multimodal_empty_model_chain_fallback_is_current_vision_order():
    # РЕГРЕССИЯ (аудит техдолга): раньше здесь стоял захардкоженный литерал
    # "nvidia/nemotron-nano-12b-v2-vl:free" — тот же класс бага, что уже был найден
    # и исправлен в ask_openrouter_text (см. test_ask_openrouter_text_empty_model_chain_
    # fallback_is_not_dead_model выше). Теперь дефолт ссылается на _OR_VISION_ORDER[0].
    chat_id = 999410
    calls = []

    async def fake_or_fallback(messages, trial_models, primary_model_id, **kwargs):
        calls.append(trial_models)
        return "ответ", trial_models[0]

    original = bot._or_chat_completion_with_fallback
    bot._or_chat_completion_with_fallback = fake_or_fallback
    try:
        asyncio.run(bot.ask_openrouter_multimodal(chat_id, "привет", (b"fake", "image/jpeg"), "photo.jpg", model_chain=[]))
        assert calls[0] == [bot._OR_VISION_ORDER[0]]
    finally:
        bot._or_chat_completion_with_fallback = original
        bot.chat_state.pop(chat_id, None)


def test_route_error_reply_text_youtube_takes_priority_over_exception_type():
    exc = bot.OpenRouterAPIError("boom", status_code=500)
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

    or_exc = bot.OpenRouterAPIError("boom", status_code=500)
    assert bot._route_error_reply_text(or_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._or_error_msg(or_exc, "text")

    other_exc = RuntimeError("что-то сломалось")
    assert bot._route_error_reply_text(other_exc, "gemini-3.6-flash", youtube_url_to_analyze=None) == bot._gemini_error_msg(other_exc, "gemini-3.6-flash")


def test_or_chat_completion_with_fallback_no_longer_accepts_attempts_per_model():
    # attempts_per_model убран целиком (был мёртвым кодом — см. аудит техдолга):
    # единственное реальное значение всегда было 1, поэтому внутренний повторный
    # цикл никогда не делал вторую итерацию.
    import inspect
    sig = inspect.signature(bot._or_chat_completion_with_fallback)
    assert "attempts_per_model" not in sig.parameters


def test_or_request_scrubs_api_key_from_network_exception_message():
    class _FakeSessionRaisingWithKeyInMessage:
        def request(self, *args, **kwargs):
            raise RuntimeError("connection failed, headers were: Authorization: Bearer fake-secret-or-key-123")

    async def fake_get_http_session():
        return _FakeSessionRaisingWithKeyInMessage()

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-secret-or-key-123"
    try:
        with pytest.raises(bot.OpenRouterAPIError) as exc_info:
            asyncio.run(bot._or_request("chat/completions", "POST", json_body={"model": "x"}))
        assert "fake-secret-or-key-123" not in str(exc_info.value)
        assert "<KEY>" in str(exc_info.value)
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_warns_on_dead_model_pattern(caplog):
    # РЕГРЕССИЯ (аудит техдолга): раньше проверялась только голова (index 0) списка
    # навсегда — теперь проверяемая модель зависит от дня года (day-of-year % len),
    # чтобы за N дней проверить весь список ценой тех же 3 запросов/сутки, что и
    # раньше (см. докстринг _probe_or_model_liveness). Тест вычисляет ожидаемую
    # модель той же формулой, что и сама функция, вместо того чтобы полагаться на
    # фиксированный index 0.
    import logging
    from datetime import date
    expected_model = bot._OR_LIGHT_ORDER[date.today().timetuple().tm_yday % len(bot._OR_LIGHT_ORDER)]

    async def fake_or_request(path, method="GET", *, json_body=None):
        model = json_body["model"]
        if model == expected_model:
            raise bot.OpenRouterAPIError("This model is unavailable for free. use another slug", status_code=404)
        return {"choices": [{"message": {"content": "pong"}}]}

    original_request = bot._or_request
    original_key = bot.OPENROUTER_API_KEY
    bot._or_request = fake_or_request
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot._probe_or_model_liveness())
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert expected_model in messages
        assert "_OR_LIGHT_ORDER" in messages
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_rotates_by_day_of_year():
    # Проверяем саму формулу ротации напрямую (не только эффект в одном из трёх
    # списков, как в тесте выше) — на день N должен пробоваться models[N % len(models)].
    from datetime import date
    calls = []

    async def fake_or_request(path, method="GET", *, json_body=None):
        calls.append(json_body["model"])
        return {"choices": [{"message": {"content": "pong"}}]}

    original_request = bot._or_request
    original_key = bot.OPENROUTER_API_KEY
    bot._or_request = fake_or_request
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        asyncio.run(bot._probe_or_model_liveness())
        day_idx = date.today().timetuple().tm_yday
        assert calls == [
            bot._OR_LIGHT_ORDER[day_idx % len(bot._OR_LIGHT_ORDER)],
            bot._OR_HEAVY_ORDER[day_idx % len(bot._OR_HEAVY_ORDER)],
            bot._OR_VISION_ORDER[day_idx % len(bot._OR_VISION_ORDER)],
        ]
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_silent_when_all_alive(caplog):
    import logging

    async def fake_or_request(path, method="GET", *, json_body=None):
        return {"choices": [{"message": {"content": "pong"}}]}

    original_request = bot._or_request
    original_key = bot.OPENROUTER_API_KEY
    bot._or_request = fake_or_request
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            asyncio.run(bot._probe_or_model_liveness())
        assert caplog.records == []
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_probe_or_model_liveness_noop_without_api_key():
    original_key = bot.OPENROUTER_API_KEY
    bot.OPENROUTER_API_KEY = ""
    called = []

    async def fake_or_request(*args, **kwargs):
        called.append(1)
        return {}

    original_request = bot._or_request
    bot._or_request = fake_or_request
    try:
        asyncio.run(bot._probe_or_model_liveness())
        assert called == []
    finally:
        bot._or_request = original_request
        bot.OPENROUTER_API_KEY = original_key


def test_system_prompt_en_keeps_key_guards():
    import system_prompt
    for guard in (
        "You are Lumen",
        "@SilverElixir",
        "CRITICALLY IMPORTANT",
        "8-800-2000-122",
        "EMOJI USAGE RULE",
        "NON-REMOVABLE BOUNDARIES",
    ):
        assert guard in system_prompt.SYSTEM_PROMPT


def test_get_system_prompt_header_english():
    assert "CURRENT TIME INFORMATION" in bot.get_system_prompt()


def test_ask_groq_text_success_records_groq_quota(monkeypatch):
    # Groq-подключение 21.09.2026: успешный ответ пишется в историю и в квоту "groq", как у остальных провайдеров.
    from collections import deque
    chat_id = 999701
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})

    async def fake_groq_request(path, method="GET", *, json_body=None):
        assert json_body["model"] == "qwen/qwen3.8-27b"
        return {"choices": [{"message": {"content": "Канберра."}}]}

    monkeypatch.setattr(bot, "_groq_request", fake_groq_request)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    recorded = {}
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model: recorded.setdefault("v", (provider, model)))
    answer = asyncio.run(bot.ask_groq_text(chat_id, "Столица Австралии?", model_chain=["qwen/qwen3.8-27b"]))
    assert answer == "Канберра."
    assert recorded["v"] == ("groq", "qwen/qwen3.8-27b")


def test_ask_groq_text_falls_back_to_next_model(monkeypatch):
    # Одна попытка на модель: первая падает — идём на вторую, а не ретраим.
    from collections import deque
    chat_id = 999702
    monkeypatch.setattr(bot, "get_state", lambda cid: {"history": [], "ctx": deque()})
    seen = []

    async def fake_groq_request(path, method="GET", *, json_body=None):
        seen.append(json_body["model"])
        if json_body["model"] == "qwen/qwen3.8-27b":
            raise bot.GroqAPIError("overloaded", status_code=503)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(bot, "_groq_request", fake_groq_request)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model: None)
    answer = asyncio.run(bot.ask_groq_text(chat_id, "привет", model_chain=["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]))
    assert answer == "ok"
    assert seen == ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]


def test_groq_request_requires_key(monkeypatch):
    # Без GROQ_API_KEY — понятная ошибка, а не сетевой вызов в никуда.
    monkeypatch.setattr(bot, "GROQ_API_KEY", "")
    with pytest.raises(bot.GroqAPIError):
        asyncio.run(bot._groq_request("chat/completions", "POST", json_body={"model": "x"}))


def test_groq_request_scrubs_api_key_from_network_exception_message(monkeypatch):
    # Тот же defense-in-depth, что у _or_request: ключ не должен светиться в тексте ошибок.
    class _FakeSessionRaisingWithKey:
        def request(self, *args, **kwargs):
            raise RuntimeError("connection failed, headers: Bearer fake-groq-key-123")

    async def fake_get_http_session():
        return _FakeSessionRaisingWithKey()

    monkeypatch.setattr(bot, "_get_http_session", fake_get_http_session)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-groq-key-123")
    with pytest.raises(bot.GroqAPIError) as exc_info:
        asyncio.run(bot._groq_request("chat/completions", "POST", json_body={"model": "x"}))
    assert "fake-groq-key-123" not in str(exc_info.value)
    assert "<KEY>" in str(exc_info.value)


def test_route_error_reply_text_maps_groq_error():
    # Ошибка Groq — пользовательский текст через общую классификацию, без сырого API.
    exc = bot.GroqAPIError("Rate limit reached", status_code=429)
    text = bot._route_error_reply_text(exc, "qwen/qwen3.8-27b", youtube_url_to_analyze=None, lang="en")
    assert "Rate limit reached" not in text
    assert text.strip() != ""


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


def test_transcribe_audio_success_records_quota(monkeypatch):
    # Groq Whisper отдал текст — пишем расход groq/whisper и возвращаем текст.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "  привет мир  "}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    recorded = {}
    monkeypatch.setattr(bot, "_record_quota_usage", lambda provider, model: recorded.setdefault("v", (provider, model)))
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", 123)) == "привет мир"
    assert recorded["v"] == ("groq", "whisper-large-v3-turbo")


def test_transcribe_audio_empty_result_is_none(monkeypatch):
    # Пустая расшифровка — как неудача: вызывающий код идёт прежним путём.
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({"text": "   "}))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", 123)) is None


def test_transcribe_audio_no_key_or_oversize_skips_network(monkeypatch):
    async def must_not_be_called():
        raise AssertionError("no network without key or for oversize audio")

    monkeypatch.setattr(bot, "_get_http_session", must_not_be_called)
    monkeypatch.setattr(bot, "GROQ_API_KEY", "")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", 123)) is None
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "VOICE_TRANSCRIBE_MAX_BYTES", 4)
    assert asyncio.run(bot._transcribe_audio(b"too-big-bytes", 123)) is None


def test_transcribe_audio_http_error_is_none(monkeypatch):
    monkeypatch.setattr(bot, "_get_http_session", _fake_groq_audio_session({}, status=503))
    monkeypatch.setattr(bot, "GROQ_API_KEY", "fake-key")
    assert asyncio.run(bot._transcribe_audio(b"ogg-bytes", 123)) is None

