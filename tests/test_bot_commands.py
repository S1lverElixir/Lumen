"""
test_bot_commands.py — Команды и триггеры: /draw//tts//reset//lang, inline_draw, кнопки-уточнения, rate-guard.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import bot
import lumen_chat_state
import lumen_limits
import pytest
import time
from tests.bot_test_helpers import (
    _FakeIncomingMessage,
    _make_lang_query,
    _make_pick_query,
)


def test_cmd_logs_flushes_the_listeners_real_handlers_not_root():
    # РЕГРЕССИЯ (аудит логирования): cmd_logs раньше флашил
    # logging.getLogger().handlers — после перехода на QueueHandler/QueueListener
    # там остаётся только сам QueueHandler (его flush() — no-op), реальные
    # file_handler/console_handler живут внутри _LOG_LISTENER. Проверяем, что
    # flush() реально доходит до обработчиков, зарегистрированных в _LOG_LISTENER.
    original_owner = bot.OWNER_ID
    original_listener = bot._LOG_LISTENER
    bot.OWNER_ID = 555001

    class _FakeHandler:
        def __init__(self):
            self.flushed = False
        def flush(self):
            self.flushed = True

    fake_handler = _FakeHandler()

    class _FakeListener:
        handlers = (fake_handler,)

    bot._LOG_LISTENER = _FakeListener()

    incoming = _FakeIncomingMessage(555001)
    incoming.from_user = SimpleNamespace(id=555001)

    async def fake_reply(text, **kwargs):
        return SimpleNamespace()
    incoming.reply = fake_reply

    try:
        asyncio.run(bot.cmd_logs(incoming))
        assert fake_handler.flushed is True
    finally:
        bot.OWNER_ID = original_owner
        bot._LOG_LISTENER = original_listener


def test_cmd_stats_counts_only_recently_active_chats():
    # "Активных" — с активностью за 24ч: молчащий год чат в счёт не идёт, но виден в "всего".
    chat_id = 999812
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777002)
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    original_owner = bot.OWNER_ID
    original_tg_call = bot._tg_call
    real_quota = dict(bot.GLOBAL_QUOTA)
    real_states = dict(bot.chat_state)
    now = time.monotonic()
    bot.OWNER_ID = 777002
    bot._tg_call = fake_tg_call
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({"quota_day": bot._current_quota_day()})
    bot.chat_state.clear()
    bot.chat_state.update({
        111: {"history": [], "last_activity": now - 3600},
        222: {"history": [], "last_activity": now - 25 * 3600},
    })
    try:
        asyncio.run(bot.cmd_stats(incoming))
        assert "Активных чатов (24ч): 1 (всего: 2)" in sent["text"]
    finally:
        bot.OWNER_ID = original_owner
        bot._tg_call = original_tg_call
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real_quota)
        bot.chat_state.clear()
        bot.chat_state.update(real_states)
    # Проф-вид /stats: нули по мёртвым моделям не мусорят, у каждого провайдера итог и остаток лимита.
def test_cmd_stats_hides_idle_models_and_shows_totals():
    # Проф-вид /stats: нули по мёртвым моделям не мусорят, у каждого провайдера итог и остаток лимита.
    chat_id = 999811
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777001)
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    original_owner = bot.OWNER_ID
    original_tg_call = bot._tg_call
    real_quota = dict(bot.GLOBAL_QUOTA)
    bot.OWNER_ID = 777001
    bot._tg_call = fake_tg_call
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({
        "gemini": {
            "gemini-3.6-flash": {"used": 3, "exhausted_at": None},
            "gemini-dead-model": {"used": 0, "exhausted_at": None},
        },
        "openrouter": {"nvidia/x:free": {"used": 5, "exhausted_at": None}},
        "groq": {},
        "quota_day": bot._current_quota_day(),
    })
    try:
        asyncio.run(bot.cmd_stats(incoming))
        text = sent["text"]
        assert "gemini-3.6-flash: 3" in text
        assert "gemini-dead-model" not in text
        assert "без обращений" in text
        assert "Σ: 3" in text
        assert "Σ: 5 / 50 (осталось 45)" in text
        assert "Σ: 0 / 1000 (осталось 1000)" in text
    finally:
        bot.OWNER_ID = original_owner
        bot._tg_call = original_tg_call
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real_quota)
        bot.chat_state.pop(chat_id, None)
        lumen_chat_state.chat_state.pop(chat_id, None)


def test_match_trigger_prefix_finds_draw_trigger():
    assert bot._match_trigger_prefix("нарисуй кота на пляже", bot.DRAW_TRIGGER_PREFIXES) == "нарисуй"


def test_match_trigger_prefix_finds_tts_trigger():
    assert bot._match_trigger_prefix("озвучь этот текст пожалуйста", bot.TTS_TRIGGER_PREFIXES) == "озвучь"


def test_match_trigger_prefix_new_synonyms_work():
    assert bot._match_trigger_prefix("преврати в аудио вот это сообщение", bot.TTS_TRIGGER_PREFIXES) == "преврати в аудио"
    assert bot._match_trigger_prefix("сгенери картинку заката", bot.DRAW_TRIGGER_PREFIXES) == "сгенери картинку"


def test_match_trigger_prefix_no_false_positive_when_trigger_not_at_start():
    # Триггерное слово упоминается, но НЕ в начале сообщения — не должно срабатывать
    assert bot._match_trigger_prefix("объясни, как я мог бы нарисовать домик карандашом", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("что значит слово озвучь на украинском", bot.TTS_TRIGGER_PREFIXES) is None


def test_match_trigger_prefix_no_false_positive_for_unrelated_text():
    assert bot._match_trigger_prefix("привет, как дела?", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("привет, как дела?", bot.TTS_TRIGGER_PREFIXES) is None


def test_match_trigger_prefix_ambiguous_phrases_deliberately_excluded():
    # "хочу картинку"/"сделай картинку" намеренно НЕ триггеры — легко спутать с
    # "хочу картинку тебе показать" или правкой уже присланного фото.
    assert bot._match_trigger_prefix("хочу картинку показать тебе", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("сделай картинку ярче", bot.DRAW_TRIGGER_PREFIXES) is None


def test_match_trigger_prefix_new_voice_and_draw_synonyms():
    # Расширение синонимов (сентябрь 2026) — разговорные варианты тех же просьб.
    assert bot._match_trigger_prefix("зачитай этот абзац", bot.TTS_TRIGGER_PREFIXES) == "зачитай"
    assert bot._match_trigger_prefix("зачитай текст договора", bot.TTS_TRIGGER_PREFIXES) == "зачитай текст"
    assert bot._match_trigger_prefix("прочти вслух мою историю", bot.TTS_TRIGGER_PREFIXES) == "прочти вслух"
    assert bot._match_trigger_prefix("сгенерируй мне изображение замка", bot.DRAW_TRIGGER_PREFIXES) == "сгенерируй мне изображение"
    assert bot._match_trigger_prefix("создай мне картинку с котом", bot.DRAW_TRIGGER_PREFIXES) == "создай мне картинку"


def test_match_trigger_prefix_long_phrase_wins_over_short_prefix():
    # Порядок в списке: "прочти вслух" стоит раньше "прочти" — иначе короткий
    # префикс съест начало ("прочти" вместо "прочти вслух").
    assert bot._match_trigger_prefix("озвучь этот текст пожалуйста", bot.TTS_TRIGGER_PREFIXES) == "озвучь"
    assert bot._match_trigger_prefix("прочти вслух", bot.TTS_TRIGGER_PREFIXES) == "прочти вслух"


def test_strip_reply_marker_treats_demonstratives_as_empty():
    # "это"/"это сообщение" после триггера — указание на реплай, а не буквальный
    # текст. Хвост после пустышки — уже содержание, идёт в работу как есть.
    assert bot._strip_reply_marker("это") == ""
    assert bot._strip_reply_marker("это сообщение") == ""
    assert bot._strip_reply_marker("этот текст пожалуйста") == "этот текст пожалуйста"
    assert bot._strip_reply_marker("кота на пляже") == "кота на пляже"
    # Хвостовая пунктуация маркеру не помеха (найдено код-ревью): "озвучь это."
    # обязано вести себя как "озвучь это", а не как буквальный текст "это.".
    assert bot._strip_reply_marker("это.") == ""
    assert bot._strip_reply_marker("это!") == ""
    assert bot._strip_reply_marker("этот текст, пожалуйста") == "этот текст, пожалуйста"


def test_match_trigger_prefix_requires_word_boundary_and_polite_forms():
    # Найдено код-ревью: чистый startswith без границы слова давал мусор —
    # "прочтите" начиналось с "прочти" (остаток "те..."), "нарисуйка" — с
    # "нарисуй". Теперь после префикса нужны конец строки/пробел/пунктуация.
    assert bot._match_trigger_prefix("нарисуйка", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("прочтите этот текст", bot.TTS_TRIGGER_PREFIXES) == "прочтите"
    assert bot._match_trigger_prefix("прочтите вслух сказку", bot.TTS_TRIGGER_PREFIXES) == "прочтите вслух"
    assert bot._match_trigger_prefix("озвучьте текст", bot.TTS_TRIGGER_PREFIXES) == "озвучьте"
    assert bot._match_trigger_prefix("нарисуйте кота", bot.DRAW_TRIGGER_PREFIXES) == "нарисуйте"


def test_tts_trigger_this_with_reply_voices_replied_message(rate_guard_setup):
    # Регрессия (сентябрь 2026): "озвучь это" в ответ на сообщение озвучивало
    # само слово "это" — остаток после триггера считался содержанием.
    message = rate_guard_setup()
    message.text = "озвучь это"
    message.reply_to_message = SimpleNamespace(text="текст из реплая", caption=None)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_tts.assert_awaited_once_with(message, "текст из реплая")


def test_draw_trigger_this_with_reply_draws_replied_message(rate_guard_setup):
    message = rate_guard_setup()
    message.text = "нарисуй это"
    message.reply_to_message = SimpleNamespace(text="закат над морем", caption=None)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_awaited_once_with(message, "закат над морем")


def test_pick_image_model_detects_anime():
    assert bot._pick_image_model("нарисуй девушку в стиле аниме") == "flux-anime"
    assert bot._pick_image_model("draw a chibi character") == "flux-anime"


def test_pick_image_model_detects_fantasy():
    assert bot._pick_image_model("нарисуй дракона в фэнтезийном замке") == "dreamshaper"
    assert bot._pick_image_model("concept art of an elf wizard") == "dreamshaper"


def test_pick_image_model_detects_realism():
    assert bot._pick_image_model("сделай фотореалистичный портрет кота") == "flux-realism"
    assert bot._pick_image_model("realistic photo of a mountain") == "flux-realism"


def test_pick_image_model_detects_quick_draft():
    assert bot._pick_image_model("быстрый набросок логотипа") == "turbo"


def test_pick_image_model_falls_back_to_default_for_generic_prompt():
    assert bot._pick_image_model("космическая станция на орбите Земли") == bot.DEFAULT_POLLINATIONS_IMAGE_MODEL
    assert bot._pick_image_model("") == bot.DEFAULT_POLLINATIONS_IMAGE_MODEL


def test_pick_image_model_style_keyword_wins_over_quick_keyword():
    # Стилевой сигнал важнее просьбы "побыстрее", если оба есть в одном промпте —
    # см. докстринг _pick_image_model про порядок проверок.
    assert bot._pick_image_model("быстро нарисуй аниме-девушку") == "flux-anime"


def test_imgmodel_command_and_callback_removed():
    # Регрессия на сам факт удаления команды — не должно остаться ни обработчика,
    # ни клавиатуры, ни каталога, которые её обслуживали.
    assert not hasattr(bot, "cmd_imgmodel")
    assert not hasattr(bot, "cb_imgmodel")
    assert not hasattr(bot, "_imgmodel_keyboard")
    assert not hasattr(bot, "_hf_model_catalog")


def test_cmd_start_mentions_every_current_command_and_not_removed_ones():
    captured = {}

    class _FakeStartMessage:
        chat = SimpleNamespace(id=1, type=bot.ChatType.PRIVATE)

        async def reply(self, text, **kwargs):
            captured["text"] = text
            return SimpleNamespace()

    asyncio.run(bot.cmd_start(_FakeStartMessage()))
    text = captured["text"]
    assert "/draw" in text
    assert "/tts" in text
    assert "/reset" in text
    assert "/lang" in text
    assert "/imgmodel" not in text


def test_inline_draw_picks_model_from_prompt_without_touching_chat_state():
    chat_id = 999430
    captured_model = []

    async def fake_pollinations_text_to_image(session, model_id, prompt):
        captured_model.append(model_id)
        return b"\x89PNG fake bytes"

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1

    class _FakePhotoBot:
        async def send_photo(self, **kwargs):
            return SimpleNamespace()

    original_hf = bot._pollinations_text_to_image
    original_bot_obj = bot.bot
    bot._pollinations_text_to_image = fake_pollinations_text_to_image
    bot.bot = _FakePhotoBot()
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй девушку в стиле аниме на пляже"))
        assert captured_model == ["flux-anime"]
        # get_state не должен был получить ключ image_model — авто-роутинг не
        # завязан на состояние чата вообще.
        assert "image_model" not in bot.get_state(chat_id)
    finally:
        bot._pollinations_text_to_image = original_hf
        bot.bot = original_bot_obj
        bot.chat_state.pop(chat_id, None)


def test_inline_draw_stops_fallback_chain_when_time_budget_exceeded():
    # Регрессия на находку код-ревью (28 августа 2026): раньше у /draw не было
    # общего бюджета времени на всю фолбэк-цепочку — при недоступности сервиса
    # генерации бот перебирал бы все 5 моделей POLLINATIONS_IMAGE_MODELS, тратя реальное
    # время пользователя без единого предупреждения. Патчим DRAW_TOTAL_BUDGET_SEC
    # на крошечное значение и делаем первую попытку заведомо дольше него —
    # вторая попытка не должна была вообще начаться.
    chat_id = 999432
    attempts = []

    async def fake_pollinations_text_to_image(session, model_id, prompt):
        attempts.append(model_id)
        await asyncio.sleep(0.05)  # дольше урезанного DRAW_TOTAL_BUDGET_SEC ниже
        raise RuntimeError("503 Service Unavailable")

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1

    original_hf = bot._pollinations_text_to_image
    original_budget = bot.DRAW_TOTAL_BUDGET_SEC
    bot._pollinations_text_to_image = fake_pollinations_text_to_image
    bot.DRAW_TOTAL_BUDGET_SEC = 0.01
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй кота"))
        # Ровно ОДНА попытка — бюджет исчерпался до второй, а не перебор всех 5 моделей.
        assert len(attempts) == 1
        assert "too long" in incoming.sent[0].edits[-1][0].lower()
    finally:
        bot._pollinations_text_to_image = original_hf
        bot.DRAW_TOTAL_BUDGET_SEC = original_budget


def test_inline_draw_falls_back_when_auto_picked_model_fails():
    # Закрывает пробел, найденный при код-ревью: _pick_image_model выбирает модель
    # по содержимому промпта, но до сих пор не было теста на то, что именно ЭТА
    # (авто-подобранная, не дефолтная) модель корректно участвует в существующей
    # fallback-цепочке при сбое — а не только счастливый путь без единой ошибки.
    chat_id = 999431
    attempts = []

    async def fake_pollinations_text_to_image(session, model_id, prompt):
        attempts.append(model_id)
        if model_id == "flux-anime":
            raise RuntimeError("503 Service Unavailable")
        return b"\x89PNG fallback bytes"

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    captured_photo = {}

    class _FakePhotoBot:
        async def send_photo(self, **kwargs):
            captured_photo.update(kwargs)
            return SimpleNamespace()

    original_hf = bot._pollinations_text_to_image
    original_bot_obj = bot.bot
    bot._pollinations_text_to_image = fake_pollinations_text_to_image
    bot.bot = _FakePhotoBot()
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй девушку в стиле аниме на пляже"))
        # Авто-подобранная модель (flux-anime) пробуется ПЕРВОЙ, несмотря на сбой.
        assert attempts[0] == "flux-anime"
        # Реально отправленное изображение — от следующей модели по порядку
        # POLLINATIONS_IMAGE_MODELS, а не от auto-pick, провалившегося с ошибкой.
        assert len(attempts) >= 2
        assert captured_photo["photo"].data == b"\x89PNG fallback bytes"
        # Подпись с названием модели убрана (сентябрь 2026): бот не раскрывает
        # внутреннюю реализацию — ни названий, ни "основная недоступна".
        assert "caption" not in captured_photo
    finally:
        bot._pollinations_text_to_image = original_hf
        bot.bot = original_bot_obj
        bot.chat_state.pop(chat_id, None)


def test_inline_draw_stops_chain_on_service_rate_limit():
    # Реальный прод-инцидент 17.09.2026: ВСЕ 5 моделей вернули HTTP 429 подряд —
    # лимит всего сервиса, а не одной модели. Гонять остаток цепочки бессмысленно:
    # останавливаемся на первой же 429 и честно просим подождать (а не
    # "переформулируйте описание" — при перегрузке это неверный совет).
    chat_id = 999432
    attempts = []

    async def fake_pollinations_text_to_image_always_429(session, model_id, prompt):
        attempts.append(model_id)
        raise RuntimeError("Pollinations.ai HTTP 429")

    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 2

    original_hf = bot._pollinations_text_to_image
    bot._pollinations_text_to_image = fake_pollinations_text_to_image_always_429
    try:
        asyncio.run(bot.inline_draw(incoming, "дикобраз"))
        assert attempts == [bot._pick_image_model("дикобраз")]
        status_texts = [text for text, _ in incoming.sent[0].edits]
        assert any("overloaded" in text for text in status_texts)
    finally:
        bot._pollinations_text_to_image = original_hf
        bot.chat_state.pop(chat_id, None)


def test_draw_command_rejects_exhausted_quota(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "/draw кот"
    monkeypatch.setattr(bot, "_check_and_register_rate_limit", lambda user_id: True)

    asyncio.run(bot.cmd_draw(message))

    bot.inline_draw.assert_not_awaited()
    bot._tg_call.assert_awaited_once()


def test_mixed_commands_share_rate_quota_and_reject_before_work(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    media = AsyncMock()
    monkeypatch.setattr(bot, "_resolve_incoming_media", media)

    async def run():
        message.text = "/draw кот"
        await bot.cmd_draw(message)
        message.text = "/tts привет"
        await bot.cmd_tts(message)
        await bot.cmd_tts(message)
        message.text = "/draw собака"
        await bot.cmd_draw(message)
        message.text = "обычный вопрос"
        await bot._handle_message_core(message)

    asyncio.run(run())
    bot.inline_draw.assert_awaited_once_with(message, "кот")
    bot.inline_tts.assert_awaited_once_with(message, "привет")
    media.assert_not_awaited()
    assert bot._tg_call.await_count == 3
    assert len(lumen_limits.user_rate_limits[456]) == 2


@pytest.mark.parametrize("command", ["draw", "tts"])
def test_blank_commands_do_not_consume_quota(rate_guard_setup, command):
    message = rate_guard_setup()
    message.text = f"/{command}   "
    asyncio.run(getattr(bot, f"cmd_{command}")(message))
    assert lumen_limits.user_rate_limits == {}
    bot._safe_reply.assert_awaited_once()
    bot.inline_draw.assert_not_awaited()
    bot.inline_tts.assert_not_awaited()


@pytest.mark.parametrize("text, handler, content", [
    ("нарисуй кота", "inline_draw", "кота"),
    ("озвучь привет", "inline_tts", "привет"),
])
def test_natural_language_trigger_consumes_one_slot(rate_guard_setup, text, handler, content):
    message = rate_guard_setup()
    message.text = text
    asyncio.run(bot._handle_message_core(message))
    getattr(bot, handler).assert_awaited_once_with(message, content)
    assert len(lumen_limits.user_rate_limits[456]) == 1


def test_match_pick_request_detects_taste_requests():
    assert bot.match_pick_request("посоветуй фильм") == "film"
    assert bot.match_pick_request("порекомендуй интересную книгу") == "books"
    assert bot.match_pick_request("подскажи музыку для тренировки") == "music"
    assert bot.match_pick_request("накидай сериалов") == "series"
    assert bot.match_pick_request("придумай игру для компании") == "games"
    # Английские запросы — тот же детектор (дефолтный язык бота — en).
    assert bot.match_pick_request("recommend a movie") == "film"
    assert bot.match_pick_request("suggest music for training") == "music"


def test_match_pick_request_rejects_detailed_or_unrelated():
    # Длинный запрос с деталями — обычным путём в модель, без кнопок.
    assert bot.match_pick_request("посоветуй фильм про космос, ужасы, 2024 год, длинный список") is None
    assert bot.match_pick_request("привет, как дела?") is None
    assert bot.match_pick_request("нарисуй кота") is None
    assert bot.match_pick_request("") is None
    # Латиница — только по границам слов: "notebook" — не книги.
    assert bot.match_pick_request("recommend a notebook") is None
    # Кириллица — по началу слова: "тигр" — не игры (AUD-E-006), "игру" — игры.
    assert bot.match_pick_request("посоветуй тигра") is None
    assert bot.match_pick_request("придумай игру для компании") == "games"


def test_pick_question_sent_instead_of_ai_route(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "посоветуй фильм"
    fake_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", fake_route)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_draw.assert_not_awaited()
    bot.inline_tts.assert_not_awaited()
    fake_route.assert_not_awaited()
    assert bot._tg_call.await_count == 1
    kwargs = bot._tg_call.await_args[1]
    markup = kwargs.get("reply_markup")
    assert markup is not None
    assert len(markup.inline_keyboard) == 4
    assert all(cb.callback_data.startswith("pick:") for row in markup.inline_keyboard for cb in row)


def test_pick_disabled_flag_goes_to_ai_route(rate_guard_setup, monkeypatch):
    message = rate_guard_setup()
    message.text = "посоветуй фильм"
    monkeypatch.setattr(bot, "PICK_BUTTONS_ENABLED", False)
    fake_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", fake_route)
    asyncio.run(bot._handle_message_core(message))
    fake_route.assert_awaited_once()


def test_pick_resolved_flag_goes_to_ai_route(rate_guard_setup, monkeypatch):
    # Дополненный выбор ("посоветуй фильм (жанр: ...)") всё ещё матчится
    # детектором — флаг _pick_resolved обрывает круг.
    message = rate_guard_setup()
    message.text = "посоветуй фильм (жанр: Триллер)"
    message._pick_resolved = True
    fake_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", fake_route)
    asyncio.run(bot._handle_message_core(message))
    fake_route.assert_awaited_once()


def test_pick_callback_happy_path_runs_augmented_request(monkeypatch):
    bot._pending_picks.clear()
    bot._pending_picks["ab12cd34"] = {
        "chat_id": 777, "user_id": 111, "scenario": "film",
        "original": "посоветуй фильм", "expires": time.monotonic() + 300,
        "lang": "ru",
    }
    q = _make_pick_query("pick:ab12cd34:2")
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        asyncio.run(bot.handle_pick_callback(q))
        assert q.answered and q.answered[0] == (None, False)
        assert len(q.message.edits) == 1
        assert "Триллер" in q.message.edits[0][0]
        fake_core.assert_awaited_once()
        sent_ns = fake_core.await_args[0][0]
        assert sent_ns.text == "посоветуй фильм (жанр: Триллер)"
        assert getattr(sent_ns, "_pick_resolved", False) is True
        assert "ab12cd34" not in bot._pending_picks
    finally:
        bot._pending_picks.clear()


def test_pick_callback_happy_path_english_template(monkeypatch):
    # Запись без языка → язык берётся из чата (дефолт en): шаблон и опции английские.
    bot._pending_picks.clear()
    bot._pending_picks["en12cd34"] = {
        "chat_id": 777, "user_id": 111, "scenario": "film",
        "original": "recommend a movie", "expires": time.monotonic() + 300,
    }
    q = _make_pick_query("pick:en12cd34:2")
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        asyncio.run(bot.handle_pick_callback(q))
        assert len(q.message.edits) == 1
        assert "Thriller" in q.message.edits[0][0]
        fake_core.assert_awaited_once()
        sent_ns = fake_core.await_args[0][0]
        assert sent_ns.text == "recommend a movie (genre: Thriller)"
    finally:
        bot._pending_picks.clear()


def test_pick_callback_second_tap_reports_expired(monkeypatch):
    q = _make_pick_query("pick:deadbeef:0")
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    asyncio.run(bot.handle_pick_callback(q))
    assert any("expired" in (text or "").lower() for text, _ in q.answered)
    fake_core.assert_not_awaited()


def test_pick_callback_wrong_user_rejected(monkeypatch):
    bot._pending_picks.clear()
    bot._pending_picks["cc33dd44"] = {
        "chat_id": 777, "user_id": 111, "scenario": "film",
        "original": "посоветуй фильм", "expires": time.monotonic() + 300,
    }
    q = _make_pick_query("pick:cc33dd44:0", user_id=999)
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        asyncio.run(bot.handle_pick_callback(q))
        assert any(alert is True for _, alert in q.answered)
        fake_core.assert_not_awaited()
    finally:
        bot._pending_picks.clear()


def test_pick_callback_bad_data_answered_quietly():
    for data in ("pick:", "pick:tok:notanint"):
        q = _make_pick_query(data)
        asyncio.run(bot.handle_pick_callback(q))
        assert q.answered


def test_pick_callback_ignores_foreign_callbacks():
    q = _make_pick_query("something-else-entirely")
    asyncio.run(bot.handle_pick_callback(q))
    assert q.answered == []


def test_expired_picks_purged_on_new_pick(rate_guard_setup, monkeypatch):
    bot._pending_picks.clear()
    bot._pending_picks["old"] = {
        "chat_id": 1, "user_id": 1, "scenario": "film",
        "original": "x", "expires": time.monotonic() - 1,
    }
    message = rate_guard_setup()
    message.text = "посоветуй фильм"
    fake_route = AsyncMock(return_value=("ok", False))
    monkeypatch.setattr(bot, "_run_route", fake_route)
    try:
        asyncio.run(bot._handle_message_core(message))
        assert "old" not in bot._pending_picks
        assert len(bot._pending_picks) == 1
    finally:
        bot._pending_picks.clear()


def test_lang_table_covers_all_keys_in_all_languages():
    import lumen_lang
    assert tuple(lumen_lang.SUPPORTED_LANGS) == tuple(sorted(lumen_lang.SUPPORTED_LANGS))
    assert set(lumen_lang.LANG_NAMES) == set(lumen_lang.SUPPORTED_LANGS)
    for key, table in lumen_lang.STRINGS.items():
        for lang in lumen_lang.SUPPORTED_LANGS:
            covered = bool(lumen_lang.LANG_PACKS.get(lang, {}).get(key)) or bool(table.get(lang))
            assert covered, f"missing {key}[{lang}]"
    for lang in lumen_lang.SUPPORTED_LANGS:
        for scenario in ("film", "series", "music", "books", "games"):
            q, opts, tpl = lumen_lang.pick_texts(lang, scenario)
            assert q and len(opts) == 4 and "{original}" in tpl and "{choice}" in tpl


def test_lang_callback_sets_language_and_confirms_in_new_language():
    bot.chat_state.pop(777, None)
    q = _make_lang_query("lang:uk")
    try:
        asyncio.run(bot.handle_lang_callback(q))
        assert bot.get_state(777).get("lang") == "uk"
        assert q.answered and q.answered[0] == (None, False)
        assert len(q.message.edits) == 1
        assert q.message.edits[0][0] == "Мова: Українська."
    finally:
        bot.chat_state.pop(777, None)


def test_lang_callback_denied_without_privileges(monkeypatch):
    async def _deny(chat_type, chat_id, user_id):
        return False
    monkeypatch.setattr(bot, "_is_privileged_in_chat", _deny)
    q = _make_lang_query("lang:uk", chat_type="group")
    asyncio.run(bot.handle_lang_callback(q))
    assert any(alert is True for _, alert in q.answered)
    assert bot.get_state(777).get("lang") != "uk"
    bot.chat_state.pop(777, None)


def test_lang_callback_ignores_foreign_callbacks():
    q = _make_lang_query("something-else-entirely")
    asyncio.run(bot.handle_lang_callback(q))
    assert q.answered == []


def test_lang_menu_lists_languages_alphabetically(monkeypatch):
    import lumen_lang
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123, type=bot.ChatType.PRIVATE),
        reply=AsyncMock(),
    )
    monkeypatch.setattr(bot, "_tg_call", AsyncMock(return_value=None))
    monkeypatch.setattr(bot, "get_state", lambda chat_id: {"lang": "ru"})
    asyncio.run(bot.cmd_lang(message))
    kwargs = bot._tg_call.await_args[1]
    markup = kwargs.get("reply_markup")
    codes = [cb.callback_data.split(":")[1] for row in markup.inline_keyboard for cb in row]
    assert codes == list(lumen_lang.SUPPORTED_LANGS)
    texts = [cb.text for row in markup.inline_keyboard for cb in row]
    assert texts == [f"{lumen_lang.LANG_NAMES[c]}{' ✓' if c == 'ru' else ''}" for c in codes]


def test_command_locales_excludes_filipino_without_iso_639_1_code():
    # Прод-инцидент: "fil" ронял setMyCommands с 400 (Bot API принимает только
    # двухбуквенные ISO 639-1 коды, которых у филиппинского нет).
    assert "fil" not in bot.COMMAND_LOCALES
    assert set(bot.COMMAND_LOCALES) == (set(bot.SUPPORTED_LANGS) - {bot.DEFAULT_LANG, "fil"})


def test_ru_system_messages_use_informal_ty():
    # Стиль бота — на "ты" везде: формальное "вы" в русских строках — баг стиля.
    import lumen_lang
    too_big = lumen_lang.STRINGS["tiktok_too_big"]["ru"]
    assert "Попробуй скачать" in too_big
    assert "Попробуйте" not in too_big
    no_sep = lumen_lang.STRINGS["tiktok_sound_no_separate"]["ru"]
    assert "Пришли, пожалуйста" in no_sep
    assert "Пришлите" not in no_sep
