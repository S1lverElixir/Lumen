"""
test_bot_streaming.py — Стриминг: куски, точки ожидания, пейсинг, rich-правки.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import asyncio
import bot
import pytest
import time
from tests.bot_test_helpers import (
    _FakeIncomingMessage,
    _FakeRichBot,
    _FakeRichFailingBot,
    _FakeRichMessage,
    _FakeSSEResponse,
    _FakeSentMessage,
    _FakeSessionForSSE,
)


def test_try_gemini_streaming_happy_path_accumulates_and_finalizes():
    chat_id = 999101

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            for piece in ["Привет", ", как ", "дела?"]:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == "Привет, как дела?"
        assert placeholder is None  # успех — плейсхолдер уже отредактирован до финального текста
        # финальная правка должна прийти с HTML parse_mode (полная markdown-конвертация)
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_aborts_on_identity_leak_mid_stream():
    # Регрессия на самый чувствительный сценарий: утечка должна обрываться ДО того,
    # как накопленный текст попадёт хоть в один edit_text — иначе пользователь успеет
    # увидеть утёкший текст на экране ещё до финального завершения потока.
    chat_id = 999104

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            for piece in ["Привет! ", "На самом деле я работаю ", "на базе Gemini от Google."]:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Кто ты на самом деле?", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == bot._IDENTITY_LEAK_FALLBACK
        assert placeholder is None
        # Ни в одной показанной пользователю правке НЕ должно быть слова "gemini" —
        # проверяем ВСЕ edit_text вызовы единственного отправленного сообщения, а не
        # только последний, т.к. именно промежуточные правки могли бы "мигнуть" утечкой.
        for shown_text, _parse_mode in incoming.sent[0].edits:
            assert "gemini" not in shown_text.lower()
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": bot._IDENTITY_LEAK_FALLBACK}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_returns_none_on_early_failure():
    chat_id = 999102

    async def fake_stream_raises(*, model, contents, config=None):
        raise RuntimeError("boom before any content")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream_raises
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer is None
        # Плейсхолдер теперь НЕ удаляется на этом уровне — он возвращается
        # вызывающему коду (_run_route), чтобы тот попробовал доправить в него
        # ответ следующей модели по цепочке, а не создавать новое сообщение.
        assert placeholder is incoming.sent[0]
        assert incoming.sent[0].deleted is False
        # история НЕ должна была обновиться — вызывающий код откатится на ask_gemini
        assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_try_gemini_streaming_failed_continuation_does_not_corrupt_first_message():
    # Регрессионный тест на баг, найденный код-ревью: при сбое отправки сообщения-
    # продолжения (текст длиннее лимита Telegram) первое, уже корректно показанное
    # сообщение раньше перезаписывалось чужим (последним) куском текста.
    chat_id = 999103
    long_piece = "А" * (bot.TG_MAX_LEN + 100)  # гарантированно требует второе сообщение

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=long_piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream

    class _FailingBot:
        async def send_message(self, **kwargs):
            return None  # имитируем неудачную отправку продолжения

    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    original_bot = bot.bot
    bot.client = fake_client
    bot.bot = _FailingBot()
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Напиши длинный текст", incoming, bot.DEFAULT_GEMINI_MODEL))
        # Функция должна была вернуть накопленный текст, а не None и не бросить исключение
        assert answer is not None
        assert placeholder is None  # это уже финализированный успех, а не ранний сбой
        # Первое сообщение должно содержать ИМЕННО первый кусок (плюс пометка об обрыве),
        # а НЕ последний/другой кусок текста — это и была суть бага.
        first_msg_final_text = incoming.sent[0].edits[-1][0]
        assert first_msg_final_text.startswith("А")
        assert "couldn't send the rest of the message" in first_msg_final_text
    finally:
        bot.client = original_client
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_openrouter_stream_pieces_parses_sse_chunks():
    lines = [
        'data: {"choices":[{"delta":{"content":"Привет"}}]}\n'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":", мир"}}]}\n'.encode("utf-8"),
        b"data: [DONE]\n",
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        async def collect():
            pieces = []
            async for piece in bot._openrouter_stream_pieces("meta-llama/llama-3.3-70b-instruct:free", [{"role": "user", "content": "hi"}]):
                pieces.append(piece)
            return pieces
        pieces = asyncio.run(collect())
        assert pieces == ["Привет", ", мир"]
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_openrouter_stream_pieces_raises_on_http_error_status():
    fake_resp = _FakeSSEResponse([], status=500)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        async def collect():
            async for _ in bot._openrouter_stream_pieces("meta-llama/llama-3.3-70b-instruct:free", []):
                pass
        with pytest.raises(bot.OpenRouterAPIError):
            asyncio.run(collect())
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_openrouter_stream_pieces_raises_on_midstream_error_chunk():
    # РЕГРЕССИЯ (аудит стриминга): провайдер за OpenRouter может упасть УЖЕ ПОСЛЕ
    # старта генерации — HTTP-статус к этому моменту давно 200 (стрим открыт), и
    # ошибка приходит не кодом ответа, а прямо внутри SSE-чанка:
    # {"error": {...}} вместо {"choices": [...]}. Раньше это тихо пропускалось
    # как "пустой чанк" (choices нет -> continue) — пользователь получал молча
    # укороченный ответ без единого намёка на причину. Первый кусок ("Начало")
    # должен успеть уйти до ошибки — проверяем, что она поднимается уже ПОСЛЕ
    # частичного контента, а не глушится.
    lines = [
        'data: {"choices":[{"delta":{"content":"Начало"}}]}\n'.encode("utf-8"),
        'data: {"error":{"message":"Provider returned error","code":502}}\n'.encode("utf-8"),
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.OPENROUTER_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.OPENROUTER_API_KEY = "fake-key"
    try:
        collected = []

        async def collect_partial():
            agen = bot._openrouter_stream_pieces("meta-llama/llama-3.3-70b-instruct:free", [{"role": "user", "content": "hi"}])
            async for piece in agen:
                collected.append(piece)

        with pytest.raises(bot.OpenRouterAPIError) as exc_info:
            asyncio.run(collect_partial())
        assert collected == ["Начало"]
        assert exc_info.value.status_code == 502
    finally:
        bot._get_http_session = original_get_session
        bot.OPENROUTER_API_KEY = original_key


def test_try_openrouter_streaming_happy_path_accumulates_and_finalizes():
    chat_id = 999105

    async def fake_stream_pieces(model_id, messages):
        for piece in ["Привет", ", как ", "дела?"]:
            yield piece

    incoming = _FakeIncomingMessage(chat_id)
    original_gen = bot._openrouter_stream_pieces
    bot._openrouter_stream_pieces = fake_stream_pieces
    try:
        answer, placeholder = asyncio.run(bot._try_openrouter_streaming(chat_id, "Привет!", incoming, "meta-llama/llama-3.3-70b-instruct:free"))
        assert answer == "Привет, как дела?"
        assert placeholder is None
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
        assert bot.GLOBAL_QUOTA["openrouter"]["meta-llama/llama-3.3-70b-instruct:free"]["used"] >= 1
    finally:
        bot._openrouter_stream_pieces = original_gen
        bot.chat_state.pop(chat_id, None)


def test_streaming_abandons_hung_first_chunk_within_limit(monkeypatch):
    # Зависший первый кусок (бэкенд молчит) — обёртка бросает TimeoutError по
    # FIRST_CHUNK_TIMEOUT_SEC, _run_streaming_reply отдаёт плейсхолдер дальше
    # по цепочке вместо бесконечного ожидания. РАНЬШЕ внешнего предела вообще
    # не было — такой стрим держал лок чата до STREAM_CHUNK_TIMEOUT_SEC внутри
    # генератора (30с) или навсегда при фейковом висящем генераторе.
    chat_id = 999301

    async def hanging_pieces():
        await asyncio.sleep(3600)
        yield "never arrives"

    monkeypatch.setattr(bot, "_model_first_chunk_limit", lambda key, floor: 0.05)
    incoming = _FakeIncomingMessage(chat_id)
    try:
        started = time.monotonic()
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="openrouter", model_id="x:free",
            piece_agen=hanging_pieces(),
        ))
        elapsed = time.monotonic() - started
        assert answer is None
        assert placeholder is incoming.sent[0]
        assert elapsed < 30
    finally:
        bot.chat_state.pop(chat_id, None)


def test_waiting_dots_cycles_frames_then_stops_on_cancel(monkeypatch):
    # Юнит-тест самого тикера: первый кадр только после _DOTS_START_AFTER_SEC,
    # дальше по кадру каждые _DOTS_TICK_SEC; отмена — штатная остановка.
    calls = []

    async def fake_sleep(delay):
        calls.append(delay)
        if len(calls) > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "_dots_sleep", fake_sleep)
    placeholder = _FakeSentMessage()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await bot._tick_waiting_dots(placeholder)

    asyncio.run(run())
    assert calls[0] == bot._DOTS_START_AFTER_SEC
    assert calls[1:] == [bot._DOTS_TICK_SEC] * 3
    assert [text for text, _ in placeholder.edits] == list(bot._DOTS_FRAMES[:3])


def test_streaming_fast_path_shows_no_dots_frames():
    # Мгновенно ответившая модель: тикер гасится до первого кадра (2.5с тишины
    # не наступает) — в правках только контент, никаких "." / "..".
    chat_id = 999302

    async def instant_pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="openrouter", model_id="y:free",
            piece_agen=instant_pieces(),
        ))
        assert answer == "Привет"
        for text, _ in incoming.sent[0].edits:
            assert text not in bot._DOTS_FRAMES
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_whitespace_only_returns_none_not_empty_response():
    # Стрим из одних пробелов — тоже "ничего не прислал": плейсхолдер уезжает
    # дальше по цепочке, а не превращается в "Empty response" для пользователя.
    chat_id = 999305

    async def whitespace_pieces():
        yield "   "

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="openrouter", model_id="z:free",
            piece_agen=whitespace_pieces(),
        ))
        assert answer is None
        assert placeholder is incoming.sent[0]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_try_openrouter_streaming_returns_none_on_early_failure():
    chat_id = 999106

    async def fake_stream_pieces_raises(model_id, messages):
        raise RuntimeError("boom before any content")
        yield ""  # делает функцию async-генератором (недостижимо)

    incoming = _FakeIncomingMessage(chat_id)
    original_gen = bot._openrouter_stream_pieces
    bot._openrouter_stream_pieces = fake_stream_pieces_raises
    try:
        answer, placeholder = asyncio.run(bot._try_openrouter_streaming(chat_id, "Привет!", incoming, "meta-llama/llama-3.3-70b-instruct:free"))
        assert answer is None
        assert placeholder is incoming.sent[0]
        assert incoming.sent[0].deleted is False
        assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")
    finally:
        bot._openrouter_stream_pieces = original_gen
        bot.chat_state.pop(chat_id, None)


def test_run_streaming_reply_paces_reveal_for_burst_instead_of_dumping_full_text():
    import lumen_typing_pace
    chat_id = 999110
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    # Реалистичная имитация бэкенда, который не стримит токен-в-токен, а отдаёт
    # весь ответ ОДНИМ куском (см. докстринг lumen_typing_pace.py про то, почему
    # это обычное дело для бесплатных моделей OpenRouter).
    long_text = "Слово " * 80  # ~480 символов одним SSE-куском

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=long_text)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        assert placeholder is None
        plain_edits = [text for text, parse_mode in incoming.sent[0].edits if parse_mode is None]
        # Хотя бы одна промежуточная правка должна была показать ЧАСТЬ текста, а
        # не весь ответ разом — иначе пейсинг не сработал (регрессия на исходную
        # проблему: "скачками по 15-20 слов" вместо плавного набора).
        assert any(0 < len(p) < len(long_text) for p in plain_edits)
        # Финальная правка — уже HTML с полным текстом, как и раньше.
        assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
        history = bot.chat_state[chat_id]["history"]
        assert history[-1] == {"role": "assistant", "content": long_text.strip()}
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_records_observed_speed_on_success():
    import lumen_typing_pace
    chat_id = 999111
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            for piece in ["Привет", ", мир!"]:
                yield SimpleNamespace(text=piece)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        # После успешного стрима у модели должна появиться собственная запись в
        # EMA — самокалибровка происходит без единой ручной правки таблицы.
        assert pace_key in lumen_typing_pace._speed_ema
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_catchup_never_exceeds_max_ticks_even_for_long_slow_burst():
    # РЕГРЕССИЯ на ключевое требование: сколько бы ни оценивалась скорость модели
    # заниженно и сколько бы символов ни осталось "довыводить" одним куском, число
    # искусственных пауз ограничено STREAM_TYPING_MAX_CATCHUP_TICKS — реальная
    # скорость ответа не должна страдать ради красивости набора текста.
    import lumen_typing_pace
    chat_id = 999112
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC  # намеренно "медленная" модель

    long_text = "Буква " * 500  # ~3000 символов, одним куском, ниже TG_MAX_LEN

    async def fake_stream(*, model, contents, config=None):
        async def gen():
            yield SimpleNamespace(text=long_text)
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    sleep_calls: list[float] = []
    original_sleep = bot._typing_sleep

    async def counting_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    bot._typing_sleep = counting_sleep
    original_client = bot.client
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        assert len(sleep_calls) <= bot.STREAM_TYPING_MAX_CATCHUP_TICKS
    finally:
        bot._typing_sleep = original_sleep
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_rich_send_used_for_final_answer_with_table():
    chat_id = 999401
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    try:
        asyncio.run(bot._safe_reply(incoming, "| A | B |\n|---|---|\n| 1 | 2 |"))
        assert len(bot.bot.rich_sent) == 1
        assert bot.bot.rich_sent[0]["rich_message"].html.startswith("<table bordered>")
        assert incoming.sent == []
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_failure_falls_back_to_legacy_html():
    chat_id = 999402
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichFailingBot()
    try:
        asyncio.run(bot._safe_reply(incoming, "**жирный** текст"))
        assert len(incoming.sent) == 1
        assert incoming.sent[0].edits == []
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_disabled_flag_uses_legacy_path(monkeypatch):
    chat_id = 999403
    incoming = _FakeIncomingMessage(chat_id)
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    try:
        asyncio.run(bot._safe_reply(incoming, "**жирный** текст"))
        assert bot.bot.rich_sent == []
        assert len(incoming.sent) == 1
    finally:
        bot.bot = original_bot
        bot.chat_state.pop(chat_id, None)


def test_rich_edit_used_for_final_message_edit():
    msg = _FakeRichMessage()
    original_bot = bot.bot
    bot.bot = _FakeRichBot()
    try:
        assert asyncio.run(bot._edit_message_quietly(msg, "## Заголовок")) is True
        assert len(bot.bot.rich_edited) == 1
        assert bot.bot.rich_edited[0]["rich_message"].html == "<h3>Заголовок</h3>"
        assert msg.edits == []
    finally:
        bot.bot = original_bot


def test_rich_edit_falls_back_to_legacy_on_failure():
    msg = _FakeRichMessage()
    original_bot = bot.bot
    bot.bot = _FakeRichFailingBot()
    try:
        assert asyncio.run(bot._edit_message_quietly(msg, "**жирный**")) is True
        assert msg.edits and msg.edits[0][0] == "<b>жирный</b>"
    finally:
        bot.bot = original_bot

