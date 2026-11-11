"""
test_bot_streaming.py — Стриминг: куски, точки ожидания, пейсинг, rich-правки.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import asyncio
import bot
import lumen_router_config
import lumen_search
import lumen_streaming
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


def test_stream_wait_caps_chunk_timeout_by_remaining_budget():
    # Регрессия (аудит 26.09.2026): кусок стрима ждал STREAM_CHUNK_TIMEOUT_SEC даже
    # после исчерпания бюджета маршрута — маршрут вылезал за ROUTE_TOTAL_BUDGET_SEC.
    assert lumen_streaming._stream_wait(bot, None) == bot.STREAM_CHUNK_TIMEOUT_SEC
    far = time.monotonic() + 300.0
    assert lumen_streaming._stream_wait(bot, far) == bot.STREAM_CHUNK_TIMEOUT_SEC
    near = time.monotonic() + 3.0
    assert 0 < lumen_streaming._stream_wait(bot, near) <= 3.0
    # Истёкший бюджет — быстрый отказ, а не ожидание в 30с.
    assert lumen_streaming._stream_wait(bot, time.monotonic() - 10.0) <= 0.5


def _fake_gemini_stream(pieces=None, *, raises=None, hang_sec=0.0):
    """Мок generate_content_stream по контракту google-genai 2.27.0:
    async def, вызов надо ждать (пример из SDK:
    `async for chunk in await client.aio.models.generate_content_stream(...)`)."""
    async def _stream(*, model, contents, config=None):
        async def gen():
            if hang_sec:
                await asyncio.sleep(hang_sec)
            if raises is not None:
                raise raises
            for piece in (pieces or []):
                yield SimpleNamespace(text=piece)
        return gen()
    return _stream


def test_gemini_stream_timeout_covers_a_hanging_first_chunk():
    # Настоящая защита: сетевое ожидание происходит на __anext__, и его ловит
    # _stream_wait под каждый кусок. Раньше проверялось только создание корутины —
    # сценарий, невозможный в проде (см. докстринг _fake_gemini_stream).
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream(hang_sec=30)
    original_client = bot.client
    original_cap = bot.STREAM_CHUNK_TIMEOUT_SEC
    bot.client = fake_client
    bot.STREAM_CHUNK_TIMEOUT_SEC = 0.2
    try:
        async def _drain():
            async for _ in lumen_streaming._gemini_stream_pieces("m", [], None):
                pass
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_drain())
    finally:
        bot.client = original_client
        bot.STREAM_CHUNK_TIMEOUT_SEC = original_cap


def test_gemini_stream_call_is_awaited():
    # Контракт SDK 2.27.0: generate_content_stream это async def, обёртка обязана
    # ждать вызов. Синхронный мок (старое поведение) даёт TypeError при await.
    import inspect
    from google.genai.models import AsyncModels
    assert inspect.iscoroutinefunction(AsyncModels.generate_content_stream)
    calls = []

    async def async_only(*, model, contents, config=None):
        calls.append(model)
        async def gen():
            yield SimpleNamespace(text="ok")
        return gen()

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = async_only
    original_client = bot.client
    bot.client = fake_client
    try:
        async def _drain():
            return [p async for p in lumen_streaming._gemini_stream_pieces("m1", [], None)]
        assert asyncio.run(_drain()) == ["ok"]
        assert calls == ["m1"]
    finally:
        bot.client = original_client


def test_try_gemini_streaming_happy_path_accumulates_and_finalizes():
    chat_id = 999101

    fake_stream = _fake_gemini_stream(["Привет", ", как ", "дела?"])

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

    fake_stream = _fake_gemini_stream(["Привет! ", "На самом деле я работаю ", "на базе Gemini от Google."])

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

    fake_stream_raises = _fake_gemini_stream(raises=RuntimeError("boom before any content"))

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

    fake_stream = _fake_gemini_stream([long_piece])

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


def test_munito_stream_pieces_parses_sse_chunks():
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
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-key"
    try:
        async def collect():
            pieces = []
            async for piece in bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", [{"role": "user", "content": "hi"}]):
                pieces.append(piece)
            return pieces
        pieces = asyncio.run(collect())
        assert pieces == ["Привет", ", мир"]
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_raises_on_http_error_status():
    fake_resp = _FakeSSEResponse([], status=500)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-key"
    try:
        async def collect():
            async for _ in bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", []):
                pass
        with pytest.raises(bot.MunitoAPIError):
            asyncio.run(collect())
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_caps_http_error_body_read():
    # Кап на тело ошибки: read-all буферизовал бы весь многумегабайтный ответ
    # провайдера (аудит run-2). Проверяем сам запрошенный размер, а не длину лога:
    # сообщение всё равно режется до 300 байт и кап не различало бы.
    seen = []

    class _Content:
        async def read(self, n=-1):
            seen.append(n)
            return b"x" * 200000

    class _Resp:
        status = 500
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, *args, **kwargs):
            return _Resp()

    async def fake_get_http_session():
        return _Session()

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-key"
    try:
        async def collect():
            async for _ in bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", []):
                pass
        with pytest.raises(bot.MunitoAPIError) as exc:
            asyncio.run(collect())
        assert seen == [65536]
        assert len(str(exc.value)) < 400
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_raises_on_midstream_error_chunk():
    # РЕГРЕССИЯ (аудит стриминга): бэкенд может упасть УЖЕ ПОСЛЕ старта генерации —
    # HTTP-статус к этому моменту давно 200 (стрим открыт), и ошибка приходит не
    # кодом ответа, а прямо внутри SSE-чанка: {"error": {...}} вместо {"choices":
    # [...]}. Раньше это тихо пропускалось как "пустой чанк" (choices нет ->
    # continue) — пользователь получал молча укороченный ответ без единого намёка
    # на причину. Первый кусок ("Начало") должен успеть уйти до ошибки — проверяем,
    # что она поднимается уже ПОСЛЕ частичного контента, а не глушится.
    lines = [
        'data: {"choices":[{"delta":{"content":"Начало"}}]}\n'.encode("utf-8"),
        'data: {"error":{"message":"Provider returned error","code":502}}\n'.encode("utf-8"),
    ]
    fake_resp = _FakeSSEResponse(lines)
    fake_session = _FakeSessionForSSE(fake_resp)

    async def fake_get_http_session():
        return fake_session

    original_get_session = bot._get_http_session
    original_key = bot.MUNITO_API_KEY
    bot._get_http_session = fake_get_http_session
    bot.MUNITO_API_KEY = "fake-key"
    try:
        collected = []

        async def collect_partial():
            agen = bot._munito_stream_pieces("deepseek-ai/deepseek-v4.1-flash-1017", [{"role": "user", "content": "hi"}])
            async for piece in agen:
                collected.append(piece)

        with pytest.raises(bot.MunitoAPIError) as exc_info:
            asyncio.run(collect_partial())
        assert collected == ["Начало"]
        assert exc_info.value.status_code == 502
    finally:
        bot._get_http_session = original_get_session
        bot.MUNITO_API_KEY = original_key


def test_munito_stream_pieces_without_key_fails_fast(monkeypatch):
    # Ключа нет — провайдер обязан отказаться до единого сетевого вызова: без этого
    # каждая попытка маршрута висела бы до таймаута соединения.
    async def must_not_be_called():
        raise AssertionError("no session without an API key")

    monkeypatch.setattr(bot, "MUNITO_API_KEY", "")
    monkeypatch.setattr(bot, "_get_http_session", must_not_be_called)

    async def collect():
        async for _ in bot._munito_stream_pieces("m", []):
            pass

    with pytest.raises(bot.MunitoAPIError):
        asyncio.run(collect())


def test_munito_stream_pieces_sends_reasoning_effort(monkeypatch):
    # protected: SSE-путь Munito несёт то же reasoning_effort, что и обычный текст.
    # Регрессия: поле добавили только в ask_munito_text — тогда посимвольный ответ
    # снова сжигает бюджет на размышление и тормозит.
    captured = {}

    class _RecordingSSESession(_FakeSessionForSSE):
        def post(self, url, **kwargs):
            captured.update(kwargs)
            return self._resp

    lines = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b"data: [DONE]\n"]

    async def _fake_session():
        return _RecordingSSESession(_FakeSSEResponse(lines))

    async def collect(gen):
        return [piece async for piece in gen]

    monkeypatch.setattr(bot, "MUNITO_API_KEY", "fake-key")
    monkeypatch.setattr(bot, "_get_http_session", _fake_session)
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "low")
    assert asyncio.run(collect(bot._munito_stream_pieces("m", [{"role": "user", "content": "hi"}]))) == ["ok"]
    assert captured["json"]["reasoning_effort"] == "low"

    captured.clear()
    monkeypatch.setenv("MUNITO_REASONING_EFFORT", "")
    asyncio.run(collect(bot._munito_stream_pieces("m", [{"role": "user", "content": "hi"}])))
    assert "reasoning_effort" not in captured["json"]


def test_streaming_history_goes_through_summarizing_trim(monkeypatch):
    # Поведенческая пара grep-теста test_streaming_history_uses_summarizing_trim:
    # длинная история в стриминге идёт через _trim_history с саммари,
    # а не молчаливым срезом.
    chat_id = 999981
    state = bot.get_state(chat_id)
    state["history"] = [{"role": "user", "content": f"q{i}"} for i in range(120)]

    async def fake_pieces():
        yield "готово"

    calls = {}

    async def fake_trim(hist, state=None):
        calls["n"] = calls.get("n", 0) + 1
        hist[:] = hist[-10:]

    monkeypatch.setattr(bot, "_trim_history", fake_trim)
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "вопрос", incoming, provider="munito", model_id="probe-trim",
            piece_agen=fake_pieces(),
        ))
        assert answer == "готово"
        assert calls.get("n", 0) >= 1
    finally:
        bot.chat_state.pop(chat_id, None)


@pytest.mark.parametrize("deadline_mode", ["none", "expired"])
def test_sse_pieces_hanging_post_entry_times_out(deadline_mode, monkeypatch):
    # Регрессия A4-01: вход в session.post шёл с total=None без общего лимита —
    # зависший провайдер держал per-chat lock вечно. Теперь заголовки ждут не
    # дольше попытки маршрута, висящий вход быстро даёт TimeoutError.
    class _HangingPostCM:
        def __init__(self):
            self.exited = False

        async def __aenter__(self):
            await asyncio.sleep(3600)
            return self

        async def __aexit__(self, *args):
            self.exited = True
            return False

    class _HangingPostSession:
        def __init__(self, cm):
            self._cm = cm

        def post(self, *args, **kwargs):
            return self._cm

    monkeypatch.setattr(bot, "ROUTE_MODEL_TIMEOUT_SEC", 0.2)
    cm = _HangingPostCM()
    session = _HangingPostSession(cm)
    deadline = None if deadline_mode == "none" else time.monotonic() - 1.0

    async def collect():
        async for _ in lumen_streaming._sse_pieces(
            session, "http://example.test/chat", {}, {},
            err_cls=bot.MunitoAPIError, provider_label="Munito", deadline=deadline,
        ):
            pass

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(collect(), timeout=5.0))
    elapsed = time.monotonic() - started
    # Старый код висел все 5с внешнего wait_for; новый обрывает вход за ~0.2-0.5с.
    assert elapsed < 2.5, f"вход в POST ждал мимо лимита попытки: {elapsed:.1f}с"
    assert cm.exited


def test_try_munito_streaming_happy_path_accumulates_and_finalizes(monkeypatch):
    chat_id = 999105

    async def fake_stream_pieces(model_id, messages, *, deadline=None):
        for piece in ["Привет", ", как ", "дела?"]:
            yield piece

    incoming = _FakeIncomingMessage(chat_id)
    monkeypatch.setattr(bot, "_munito_stream_pieces", fake_stream_pieces)
    answer, placeholder = asyncio.run(bot._try_munito_streaming(chat_id, "Привет!", incoming, "deepseek-ai/deepseek-v4.1-flash-1017"))
    assert answer == "Привет, как дела?"
    assert placeholder is None
    assert incoming.sent[0].edits[-1][1] == bot.ParseMode.HTML
    history = bot.chat_state[chat_id]["history"]
    assert history[-1] == {"role": "assistant", "content": "Привет, как дела?"}
    assert bot.GLOBAL_QUOTA["munito"]["deepseek-ai/deepseek-v4.1-flash-1017"]["used"] >= 1


def test_streaming_abandons_hung_first_chunk_within_limit(monkeypatch):
    # Висящий первый кусок — TimeoutError и плейсхолдер дальше по цепочке (раньше предела не было — лок держался до 30с/навсегда).
    #
    # Патчим lumen_streaming, а НЕ bot: _run_streaming_reply читает имя из своего
    # модуля, и прошлый тест патчил bot._model_first_chunk_limit вхолостую — он
    # честно ждал штатные 25с и проходил, потому что граница была 30с
    # (враждебное ревью 27.09.2026). Теперь предел жёстче выставленного лимита.
    chat_id = 999301

    async def hanging_pieces():
        await asyncio.sleep(3600)
        yield "never arrives"

    monkeypatch.setattr(lumen_streaming, "_model_first_chunk_limit", lambda key, floor: 0.05)
    incoming = _FakeIncomingMessage(chat_id)
    try:
        started = time.monotonic()
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-x",
            piece_agen=hanging_pieces(),
        ))
        elapsed = time.monotonic() - started
        assert answer is None
        assert placeholder is incoming.sent[0]
        # 0.05с лимита + небольшой запас на ввод-вывод фейков. Прежние 30с
        # пропускали настоящие 25с ожидания, то есть тест ничего не проверял.
        assert elapsed < 5, f"тест ждёт настоящего таймаута, а не подменённого: {elapsed:.1f}с"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_respects_route_deadline(monkeypatch):
    # Grace-окно живого стрима в проде 20 с; в тесте окно уменьшаем, иначе
    # он спит 20 секунд, а проверяется именно застой куска.
    monkeypatch.setattr(bot, "STREAM_ROUTE_STALL_GRACE_SEC", 0.3)
    # Внешний аудит: капающий по куску стрим жил мимо ROUTE_TOTAL_BUDGET_SEC и держал lock.
    chat_id = 999309

    async def slow_drip_pieces():
        yield "начало"
        await asyncio.sleep(3600)
        yield "никогда"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        started = time.monotonic()
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=slow_drip_pieces(), deadline=time.monotonic() - 1.0,
        ))
        elapsed = time.monotonic() - started
        # Бюджет уже прошёл: либо плейсхолдер дальше, либо финал с пометкой — но не вечное ожидание.
        assert placeholder is not None or answer is not None
        # Висящий второй кусок (3600с) обязан оборваться бюджетом, а не зависнуть.
        assert elapsed < 30, f"стрим ждал висящий кусок мимо дедлайна: {elapsed:.1f}с"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_first_chunk_limit_capped_by_route_deadline(monkeypatch):
    # Что защищает: ожидание первого куска не выходит за бюджет маршрута.
    # Регрессия: предел модели (по замерам до 75с) игнорировал дедлайн, и стрим
    # держал лок сверх бюджета (аудит 27.10.2026).
    import lumen_streaming
    seen = {}

    async def fake_feedback(piece_agen, placeholder, *, first_chunk_limit, first_task=None, status=None, wait_started=None):
        seen["limit"] = first_chunk_limit
        if first_task is not None:
            yield await first_task
        async for piece in piece_agen:
            yield piece

    async def one_piece():
        yield "готово"

    monkeypatch.setattr(lumen_streaming, "_pieces_with_waiting_feedback", fake_feedback)
    monkeypatch.setattr(lumen_streaming, "_model_first_chunk_limit", lambda key, floor: 3600)
    chat_id = 999318
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-w",
            piece_agen=one_piece(), deadline=time.monotonic() + 1,
        ))
        assert answer == "готово"
        assert seen["limit"] <= 1.1
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_starts_model_request_before_placeholder():
    # Что защищает: запрос к модели стартует до отправки "…", а не после неё.
    # Регрессия: плейсхолдер первым — модель простаивает лишний круг к Telegram
    # (0.1-0.3с на каждый стриминговый ответ).
    chat_id = 999319
    order = []

    async def pieces():
        order.append("model")
        yield "готово"

    class _Msg(_FakeIncomingMessage):
        async def reply(self, text, **kwargs):
            # Настоящий reply — сетевой вызов: даёт задаче первого куска стартовать.
            await asyncio.sleep(0)
            order.append("placeholder")
            return await super().reply(text, **kwargs)

    incoming = _Msg(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-w",
            piece_agen=pieces(),
        ))
        assert answer == "готово"
        assert order[:2] == ["model", "placeholder"]
    finally:
        bot.chat_state.pop(chat_id, None)


def _streaming_rate_limit_quotas(chat_id, model_id, error_text):
    async def pieces_429():
        raise bot.MunitoAPIError(error_text, status_code=429)
        yield ""
    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id=model_id,
            piece_agen=pieces_429(),
        ))
        assert answer is None
        return dict(bot.GLOBAL_QUOTA["munito"][model_id])
    finally:
        bot.chat_state.pop(chat_id, None)


# «Суточный» текст тут тоже munito-429: провайдер отдаёт оконные лимиты
# (200 запросов за 30 секунд), и держать модель заблокированной до утра нечем —
# суточную метку ставит только ветка Gemini (см. gemini-тест ниже по файлу).
@pytest.mark.parametrize("error_text", ["Rate limit reached", "free-models-per-day limit reached"])
def test_streaming_rate_limit_only_cools_down(error_text):
    # Регрессия (враждебное ревью 27.09.2026): всплеск 429 ставил суточную метку, и
    # модель выпадала из роута до полуночи — снять метку было нечем, потому что
    # заведомо мёртвую модель не зовут. Теперь это короткая остывка.
    provider = bot.GLOBAL_QUOTA.setdefault("munito", {})
    had_model = "probe-y" in provider
    old_model = dict(provider.get("probe-y", {}))
    try:
        entry = _streaming_rate_limit_quotas(999311, "probe-y", error_text)
        assert entry["exhausted_at"] is None
        assert entry["cooldown_until"] > time.time()
        assert lumen_router_config._is_quota_exhausted("munito", "probe-y") is True
        # По истечении остывки модель возвращается в роут сама, без смены суток.
        provider["probe-y"]["cooldown_until"] = time.time() - 1.0
        assert lumen_router_config._is_quota_exhausted("munito", "probe-y") is False
    finally:
        if had_model:
            provider["probe-y"] = old_model
        else:
            provider.pop("probe-y", None)


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
    assert len(placeholder.edits) == 3
    import lumen_status
    assert all(text[0] in lumen_status._TRIO_FRAMES and "Thinking" in text
               for text, _ in placeholder.edits)
    # Плейсхолдер уже показывает кадр 0 (треугольник): тикер обязан начать со второго,
    # иначе первая же правка повторяет тот же символ (ревью 28.10.2026).
    assert placeholder.edits[0][0].startswith(lumen_status._TRIO_FRAMES[1])
    assert [text[0] for text, _ in placeholder.edits] == list(lumen_status._TRIO_FRAMES[1:4])
    # Код языка не должен утечь в chat_state строковым ключом (прод 28.10.2026).
    assert "en" not in bot.chat_state


def test_streaming_fast_path_shows_no_dots_frames():
    # Мгновенно ответившая модель: тикер гасится до первого кадра (2.5с тишины
    # не наступает) — в правках только контент, никаких "." / "..".
    chat_id = 999302

    async def instant_pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=instant_pieces(),
        ))
        assert answer == "Привет"
        for text, _ in incoming.sent[0].edits:
            assert text not in bot._DOTS_FRAMES
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_plain_text_adds_subtle_thought_line():
    # Мгновенный ответ (<1 с) activity-строку не получает — как в Claude
    # (ревью 11.11.2026); «✻ Thought for Ns» появляется только когда модель
    # реально думала секунду и больше (см. медленный вариант ниже).
    chat_id = 999321

    async def pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(),
        ))
        assert answer == "Привет"
        final_text, parse_mode = incoming.sent[0].edits[-1]
        assert parse_mode == bot.ParseMode.HTML
        assert "✻" not in final_text
        assert final_text.strip() == "Привет"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_slow_first_chunk_puts_thought_line_on_first_edit():
    # Регрессия на п.8/п.11 ревью 11.11.2026: заголовок «✻ Thought for Ns»
    # обязан стоять с ПЕРВОЙ правки текста и не появляться только в последней
    # (так же живёт activity-строка у Claude). Старый код ставил его лишь в
    # финальной правке — здесь первый content-edit не имел заголовка.
    chat_id = 999322

    async def pieces():
        await asyncio.sleep(1.05)
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-slow",
            piece_agen=pieces(),
        ))
        assert answer == "Привет"
        # Кадр 0 с заголовком — первая HTML-правка: до неё в пузыре только
        # тикер «Думаю…», который текст ещё не показывал.
        html_edits = [t for t, pm in incoming.sent[0].edits if pm == bot.ParseMode.HTML]
        # Два HTML-кадра: промежуточный с заголовком (маляр) и финальный compose.
        # Старый код давал единственный HTML-кадр — финал, заголовок только в нём.
        assert len(html_edits) >= 2
        assert html_edits[0].startswith("<i>✻ Thought for")
        assert "Привет" in html_edits[0]
        assert incoming.sent[0].edits[-1][0].startswith("<i>✻ Thought for")
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_final_split_sends_missing_continuation(monkeypatch):
    # 4×1000 + 200 символов: инкрементальное разбиение видело один кусок,
    # финальное дало два; раньше последняя правка затирала ответ хвостом.
    chat_id = 999324

    class _RecordingBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            message = _FakeSentMessage()
            self.sent.append((kwargs, message))
            return message

    fake_bot = _RecordingBot()
    monkeypatch.setattr(bot, "bot", fake_bot)

    async def pieces():
        for _ in range(4):
            yield "а" * 1000
        yield "б" * 200

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "длинный", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(),
        ))
        assert answer and answer.startswith("а" * 1000) and answer.endswith("б" * 200)
        assert len(fake_bot.sent) == 1
        first_edit = incoming.sent[0].edits[-1][0]
        tail_text = fake_bot.sent[0][0]["text"]
        assert first_edit.startswith("а")
        assert tail_text.endswith("б")
        # Оба сообщения вместе покрывают весь ответ, а не только хвост.
        assert len(first_edit) + len(tail_text) == len(answer)
        # Досланный хвост уже финальный: повторная правка получила бы
        # "not modified" и заменила HTML на голый текст (аудит 28.10.2026).
        assert fake_bot.sent[0][1].edits == []
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_resplit_shifted_boundary_re_edits_shown_message(monkeypatch):
    # Границы _split_text_chunks зависят от всего текста (склейка соседей по
    # итоговому HTML), поэтому рост ответа сдвигал границу УЖЕ показанного куска,
    # старый код дописывал за этим правку финализированного сообщения, и видимый
    # текст перетекал между пузырями (аудит 01.11.2026; ревью 11.11.2026 п.5
    # против: граница замораживается один раз, пересчитывается только хвост).
    from lumen_formatting import _md_to_html, _split_text_chunks

    chat_id = 999326
    base = "Линия **bold** текст. " * 150 + "\n\n" + "y" * 300 + "\n\n" + "z" * 300
    tail = "И ещё немного текста, который дописала модель. "

    class _RecordingBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            message = _FakeSentMessage()
            self.sent.append((kwargs, message))
            return message

    monkeypatch.setattr(bot, "bot", _RecordingBot())

    async def pieces():
        yield base
        yield tail * 5

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "длинный", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(),
        ))
        final = _split_text_chunks(answer, bot.TG_MAX_LEN)
        assert len(final) > 1
        # На первом куске граница была левее — иначе тест ничего не проверяет.
        frozen_boundary = _split_text_chunks(base, bot.TG_MAX_LEN)
        assert len(frozen_boundary) > 1, "первое разбиение обязано дать >1 куска"
        assert len(frozen_boundary[0]) < len(final[0])
        shown_first = incoming.sent[0].edits[-1][0]
        # Первый пузырь остался на замороженной границе: old-код переставил бы
        # его на финальную (сдвинутую) границу.
        assert shown_first == _md_to_html(frozen_boundary[0])
        # Покрытие не потерялось: хвост ушёл в продолжения, последнее показанное
        # сообщение заканчивается фразой модели; «…» среди продолжений не слан.
        cont_texts = [str(kw.get("text", "")) for kw, _m in bot.bot.sent]
        assert cont_texts and "…" not in cont_texts
        last_shown = cont_texts[-1] or ""
        for _kw, msg in bot.bot.sent:
            if msg.edits:
                last_shown = msg.edits[-1][0]
        assert "который дописала модель" in last_shown
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_stores_clean_history_text_not_full_prompt():
    # Аудит 28.10.2026: в историю уходил ai_prompt со сниппетами и датой;
    # сохраняем только чистый запрос пользователя.
    chat_id = 999323

    async def pieces():
        yield "Ответ"

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "полный промпт со сниппетами", incoming,
            provider="munito", model_id="probe-y", piece_agen=pieces(),
            history_text="чистый вопрос",
        ))
        assert answer == "Ответ"
        history = bot.chat_state[chat_id]["history"]
        assert history[-2] == {"role": "user", "content": "чистый вопрос"}
        assert history[-1] == {"role": "assistant", "content": "Ответ"}
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_reuses_status_bubble_instead_of_sending_new_placeholder():
    # Живой статус уже в чате: стрим пишет ответ в него, второго «…» нет.
    chat_id = 999320

    class _FakeStatus:
        def __init__(self):
            self.message = _FakeSentMessage()
            self.finished = False

        async def finish(self):
            self.finished = True

        def paint_header_md(self):
            # Стрим нарисовал бы под этой строкой тело ответа; здесь ответ
            # мгновенный, секунд 0 — строки нет (ревью 11.11.2026).
            return ""

        def compose_answer(self, answer_md):
            return ("<blockquote expandable>✻ Thought for 1s</blockquote>\n\n" + answer_md,
                    "✻ Thought for 1s\n\n" + answer_md)

    async def pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    status = _FakeStatus()
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=pieces(), status=status,
        ))
        assert answer == "Привет"
        assert incoming.sent == []  # второй пузырь не отправлялся
        assert status.finished is True
        final_edit = status.message.edits[-1][0]
        assert "Привет" in final_edit
        assert "Thought for" in final_edit
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
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-z",
            piece_agen=whitespace_pieces(),
        ))
        assert answer is None
        assert placeholder is incoming.sent[0]
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_reveal_follows_arrival_pace_not_full_dump():
    # Куски капают постепенно (10 × 20 симв. с паузами): первая правка обязана показать
    # ЧАСТЬ ответа, а не весь текст разом — часы показа идут от первого куска.
    chat_id = 999308
    full = "x" * 200

    async def paced_pieces():
        for _ in range(10):
            await asyncio.sleep(0.05)
            yield "x" * 20

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-pace",
            piece_agen=paced_pieces(),
        ))
        assert answer == full
        first_edit = incoming.sent[0].edits[0][0]
        assert 0 < len(first_edit) < len(full)
    finally:
        bot.chat_state.pop(chat_id, None)


def test_try_munito_streaming_returns_none_on_early_failure(monkeypatch):
    chat_id = 999106

    async def fake_stream_pieces_raises(model_id, messages, *, deadline=None):
        raise RuntimeError("boom before any content")
        yield ""  # делает функцию async-генератором (недостижимо)

    incoming = _FakeIncomingMessage(chat_id)
    monkeypatch.setattr(bot, "_munito_stream_pieces", fake_stream_pieces_raises)
    answer, placeholder = asyncio.run(bot._try_munito_streaming(chat_id, "Привет!", incoming, "deepseek-ai/deepseek-v4.1-flash-1017"))
    assert answer is None
    assert placeholder is incoming.sent[0]
    assert incoming.sent[0].deleted is False
    assert chat_id not in bot.chat_state or not bot.chat_state[chat_id].get("history")


def test_run_streaming_reply_reuses_given_placeholder():
    # Резервная модель после раннего сбоя стрима пишет в тот же пузырёк:
    # второй «Думаю…» в чате не появляется (прод 28.10.2026).
    chat_id = 999105

    async def pieces():
        yield "Привет"

    incoming = _FakeIncomingMessage(chat_id)
    reused = _FakeSentMessage()

    async def must_not_send(*args, **kwargs):
        raise AssertionError("новый пузырёк не нужен: передан готовый")

    original_sender = bot._answer_sender
    bot._answer_sender = must_not_send
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет", incoming, provider="munito", model_id="probe-m",
            piece_agen=pieces(), reuse_placeholder=reused,
        ))
        assert answer == "Привет" and placeholder is None
        assert incoming.sent == []
        assert reused.edits
    finally:
        bot._answer_sender = original_sender
        bot.chat_state.pop(chat_id, None)


def test_run_streaming_reply_paces_reveal_for_burst_instead_of_dumping_full_text():
    import lumen_typing_pace
    chat_id = 999110
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)

    # Реалистичная имитация бэкенда, который не стримит токен-в-токен, а отдаёт
    # весь ответ ОДНИМ куском (см. докстринг lumen_typing_pace.py про то, почему
    # это обычное дело для бесплатных моделей с общим слагом).
    long_text = "Слово " * 80  # ~480 символов одним SSE-куском

    fake_stream = _fake_gemini_stream([long_text])

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)

    original_client = bot.client
    bot.client = fake_client
    try:
        answer, placeholder = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        assert placeholder is None
        partial_edits = [text for text, parse_mode in incoming.sent[0].edits if parse_mode is bot.ParseMode.HTML]
        # Хотя бы одна промежуточная правка должна была показать ЧАСТЬ текста, а
        # не весь ответ разом — иначе пейсинг не сработал (регрессия на исходную
        # проблему: "скачками по 15-20 слов" вместо плавного набора). С ревью
        # 11.11.2026 п.4 промежуточные кадры идут HTML с закрытой разметкой, а
        # не голым текстом, поэтому фильтр по HTML, а не по None.
        assert any(0 < len(p) < len(long_text) for p in partial_edits)
        assert all("**" not in p for p in partial_edits)
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

    fake_stream = _fake_gemini_stream(["Привет", ", мир!"])

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
    # искусственных пауз ограничено STREAM_TYPING_MAX_CATCHUP_SEC — реальная
    # скорость ответа не должна страдать ради красивости набора текста.
    import lumen_typing_pace
    chat_id = 999112
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC  # намеренно "медленная" модель

    long_text = "Буква " * 500  # ~3000 символов, одним куском, ниже TG_MAX_LEN

    fake_stream = _fake_gemini_stream([long_text])

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
        tick_ceiling = max(1, int(bot.STREAM_TYPING_MAX_CATCHUP_SEC / bot.STREAM_TYPING_TICK_SEC))
        assert len(sleep_calls) <= tick_ceiling
        # Длинный хвост идёт ровно, а не восьмью прыжками: тиков заметно больше.
        assert len(sleep_calls) > 8, sleep_calls
    finally:
        bot._typing_sleep = original_sleep
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_run_streaming_reply_catchup_edits_respect_min_interval(monkeypatch):
    # Довывод правится тем же троттлингом, что основной цикл: тик короче
    # интервала правок, серия быстро ловила бы 429.
    import lumen_typing_pace
    import time
    chat_id = 999113
    pace_key = lumen_typing_pace.speed_key("gemini", bot.DEFAULT_GEMINI_MODEL)
    original_ema = lumen_typing_pace._speed_ema.pop(pace_key, None)
    lumen_typing_pace._speed_ema[pace_key] = lumen_typing_pace.MIN_CHARS_PER_SEC
    monkeypatch.setattr(bot, "STREAM_EDIT_MIN_INTERVAL_SEC", 0.4)
    monkeypatch.setattr(bot, "STREAM_TYPING_TICK_SEC", 0.3)
    monkeypatch.setattr(bot, "_typing_sleep", asyncio.sleep)
    long_text = "Буква " * 500
    fake_stream = _fake_gemini_stream([long_text])
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)
    edit_ts = []
    orig_tg_call = bot._tg_call

    async def rec_tg_call(method, *args, **kwargs):
        if getattr(method, "__name__", "") == "edit_text":
            edit_ts.append(time.monotonic())
        return await orig_tg_call(method, *args, **kwargs)

    monkeypatch.setattr(bot, "_tg_call", rec_tg_call)
    original_client = bot.client
    bot.client = fake_client
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert answer == long_text.strip()
        # Последняя правка финальная терминальная (один вызов, вне троттлинга
        # по дизайну) — меряем промежутки прогрессивных правок.
        gaps = [b - a for a, b in zip(edit_ts, edit_ts[1:-1])]
        assert gaps, "expected progressive edits during streaming"
        assert min(gaps) >= 0.35
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        if original_ema is not None:
            lumen_typing_pace._speed_ema[pace_key] = original_ema
        else:
            lumen_typing_pace._speed_ema.pop(pace_key, None)


def test_send_text_pauses_between_chunks(monkeypatch):
    # Куски уходят с паузой против флуд-контроля за серию подряд.
    from types import SimpleNamespace
    sleeps = []

    async def rec_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bot, "_typing_sleep", rec_sleep)

    class _Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace()

    monkeypatch.setattr(bot, "bot", _Bot())
    incoming = _FakeIncomingMessage(999114)
    text = "слово " * 800
    chunks = lumen_streaming._split_text_chunks(text, bot.TG_MAX_LEN)
    assert len(chunks) > 1
    try:
        asyncio.run(bot._send_text(incoming, text))
        assert sleeps == [bot.STREAM_TYPING_TICK_SEC] * (len(chunks) - 1)
    finally:
        bot.chat_state.pop(999114, None)


def test_send_text_keeps_reply_markup_only_on_last_chunk(monkeypatch):
    # Клавиатура должна уйти один раз, с последним куском: reply_markup на
    # каждом чанке дублирует кнопку в чате, поэтому из всех кусков, кроме
    # последнего, kwarg вычищается (lumen_rich._send_text). Существующие
    # send_text-тесты kwarg reply_markup не передают вообще.
    from types import SimpleNamespace
    monkeypatch.setattr(bot, "RICH_MESSAGES_ENABLED", False)
    cont_calls: list[dict] = []

    class _Bot:
        async def send_message(self, **kwargs):
            cont_calls.append(kwargs)
            return SimpleNamespace()

    monkeypatch.setattr(bot, "bot", _Bot())
    incoming = _FakeIncomingMessage(999115)
    reply_calls: list[dict] = []

    async def capture_reply(text, **kwargs):
        reply_calls.append(kwargs)
        return SimpleNamespace()

    incoming.reply = capture_reply
    keyboard = {"inline_keyboard": [[{"text": "ok", "callback_data": "x"}]]}
    text = "слово " * 1600
    try:
        asyncio.run(bot._send_text(incoming, text, reply_markup=keyboard))
    finally:
        bot.chat_state.pop(999115, None)
    assert len(bot._split_text_chunks(text, bot.TG_MAX_LEN)) >= 3
    assert len(cont_calls) >= 2
    # Первый кусок уходит через message.reply, продолжения — через bot.send_message.
    assert "reply_markup" not in reply_calls[0]
    assert "reply_markup" not in cont_calls[0]
    assert cont_calls[-1].get("reply_markup") == keyboard


def test_gemini_streaming_daily_429_marks_exhausted_like_text_path():
    # Стриминг опознавал суточную квоту чужим предикатом и ставил минутную
    # остывку вместо метки до утра.
    chat_id = 999116
    model = bot.DEFAULT_GEMINI_MODEL
    fake_stream = _fake_gemini_stream(raises=RuntimeError("429 Quota exceeded for the day"))
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = fake_stream
    incoming = _FakeIncomingMessage(chat_id)
    original_client = bot.client
    bot.client = fake_client
    bot.GLOBAL_QUOTA.setdefault("gemini", {}).pop(model, None)
    try:
        answer, _ = asyncio.run(bot._try_gemini_streaming(chat_id, "Привет!", incoming, model))
        assert answer is None
        entry = bot.GLOBAL_QUOTA["gemini"][model]
        assert entry["exhausted_at"] is not None
        assert lumen_router_config._is_quota_exhausted("gemini", model) is True
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)
        bot.GLOBAL_QUOTA["gemini"].pop(model, None)


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


def test_streaming_limits_chunk_resplits(monkeypatch):
    # Аудит A4-11: _split_text_chunks на каждый кусок давал O(n²) в loop —
    # теперь пересчёт только при заметном приросте текста.
    real_split = lumen_streaming._split_text_chunks
    calls = {"n": 0}

    def _counting_split(text, limit):
        calls["n"] += 1
        return real_split(text, limit)

    monkeypatch.setattr(lumen_streaming, "_split_text_chunks", _counting_split)
    chat_id = 999971

    async def many_pieces():
        for i in range(30):
            yield f"кусок{i:02d} " + "x" * 60

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-split",
            piece_agen=many_pieces(),
        ))
        assert answer is not None and "кусок29" in answer
        assert calls["n"] < 30, f"разбиение на каждый кусок: {calls['n']}"
    finally:
        bot.chat_state.pop(chat_id, None)


def test_streaming_falls_back_to_reply_when_edits_die(monkeypatch):
    # Аудит A4-12: сообщение снесли посреди стрима — серия неуспешных правок
    # ведёт к досылке ответа новым сообщением, а не к истории под невидимый текст.
    chat_id = 999972
    incoming = _FakeIncomingMessage(chat_id)

    async def many_big_pieces():
        for i in range(5):
            yield f"часть{i} " + "y" * 5000

    async def _failing_edits_tg_call(method, *args, **kwargs):
        if getattr(method, "__name__", "") in ("reply", "send_message"):
            return await incoming.reply("…")
        return None

    async def send_message(*args, **kwargs):
        return await incoming.reply("…")

    sent_fallback = {}

    async def _recorder_fallback(message, text, **kwargs):
        sent_fallback["text"] = text

    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_message=send_message))
    monkeypatch.setattr(bot, "_tg_call", _failing_edits_tg_call)
    monkeypatch.setattr(bot, "_safe_reply", _recorder_fallback)
    try:
        answer, _ = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="dead:edit",
            piece_agen=many_big_pieces(),
        ))
        assert answer is not None and "часть4" in answer
        assert "часть4" in sent_fallback.get("text", "")
        history = bot.chat_state[chat_id]["history"]
        assert history[-1]["content"] == answer
    finally:
        bot.chat_state.pop(chat_id, None)



def test_stream_search_marker_never_reaches_the_screen_and_keeps_the_bubble():
    # Скрины из группы 11.11.2026: «когда вышла Fable 5.2» отвечали по памяти,
    # потому что классификатор формулировки не узнаёт их. Решение отдано модели
    # маркером [[SEARCH]] — он не имеет права мелькнуть на экране даже покусочно,
    # а пузырёк «Думаю…» должен дожить до второго прохода со сниппетами.
    chat_id = 999151
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = _fake_gemini_stream(
        ["[[SE", "ARCH]]", " этот хвост не дойдёт"])
    incoming = _FakeIncomingMessage(chat_id)
    original_client = bot.client
    bot.client = fake_client
    try:
        with pytest.raises(lumen_search._SearchRequested) as caught:
            asyncio.run(bot._try_gemini_streaming(chat_id, "когда вышла Fable 5.2", incoming, bot.DEFAULT_GEMINI_MODEL))
        assert incoming.sent[0].edits == []
        assert caught.value.placeholder is incoming.sent[0]
        assert bot.chat_state[chat_id].get("history") == []
    finally:
        bot.client = original_client
        bot.chat_state.pop(chat_id, None)


def test_live_stream_is_not_cut_by_the_route_budget(monkeypatch):
    # РЕГРЕССИЯ на прод 11.11.2026: 5700-символьный ответ оборвался на 35-й
    # секунде с пометкой «Соединение прервалось», потому что стрим проверялся
    # общим 40-секундным бюджетом маршрута. Пока куски прибывают, ответ должен
    # дойти до конца.
    chat_id = 999311
    monkeypatch.setattr(bot, "STREAM_ROUTE_MAX_SEC", 30)

    async def steady_drip_pieces():
        for n in range(30):
            yield "кусочек%02d " % n
            await asyncio.sleep(0.05)

    incoming = _FakeIncomingMessage(chat_id)
    try:
        answer, placeholder = asyncio.run(bot._run_streaming_reply(
            chat_id, "Привет!", incoming, provider="munito", model_id="probe-y",
            piece_agen=steady_drip_pieces(), deadline=time.monotonic() - 1.0,
        ))
        assert answer is not None and placeholder is None
        assert "кусочек29" in answer, answer
        assert not any("interrupted" in (t or "").lower() for t, _ in incoming.sent[0].edits)
    finally:
        bot.chat_state.pop(chat_id, None)
