"""
test_bot_commands.py — Команды и триггеры: /draw//tts//reset//lang, inline_draw, кнопки-уточнения, rate-guard.

Выделено из test_bot.py (P2 аудита); общие фейки — в bot_test_helpers.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import bot
import lumen_chat_state
import lumen_commands
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
    now = time.time()
    bot.OWNER_ID = 777002
    bot._tg_call = fake_tg_call
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({"quota_day": bot._current_quota_day()})
    bot.chat_state.clear()
    bot.chat_state.update({
        111: {"history": [], "last_activity": now - 3600},
        222: {"history": [], "last_activity": now - 25 * 3600},
        # Запись без метки (старый снимок) — неактивна, а не "только что".
        333: {"history": []},
    })
    try:
        asyncio.run(bot.cmd_stats(incoming))
        assert "Активных чатов (24ч): 1 (всего: 3)" in sent["text"]
    finally:
        bot.OWNER_ID = original_owner
        bot._tg_call = original_tg_call
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real_quota)
        bot.chat_state.clear()
        bot.chat_state.update(real_states)
    # Проф-вид /stats: нули по мёртвым моделям не мусорят, у каждого провайдера итог и остаток лимита.
@pytest.mark.parametrize("or_limit,groq_limit", [(50, 1000), (100, 10)])
def test_cmd_stats_hides_idle_models_and_shows_totals(monkeypatch, or_limit, groq_limit):
    # Проф-вид /stats: нули по мёртвым моделям не мусорят, у каждого провайдера итог и остаток лимита.
    # Параметризация по лимитам: лимиты 50/1000 живут в env, а не в команде —
    # смена тарифа без правки кода обязана менять и вывод.
    monkeypatch.setattr(bot, "OPENROUTER_DAILY_LIMIT", or_limit)
    monkeypatch.setattr(bot, "GROQ_DAILY_LIMIT", groq_limit)
    # Лимит Gemini — только где задан в env: одна модель с лимитом, вторая без.
    monkeypatch.setattr(bot, "GEMINI_DAILY_LIMITS", {"gemini-3.6-flash": 20})
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
            "gemini-other": {"used": 1, "exhausted_at": None},
            "gemini-dead-model": {"used": 0, "exhausted_at": None},
        },
        "openrouter": {"nvidia/x:free": {"used": 5, "exhausted_at": None}},
        "groq": {},
        "quota_day": bot._current_quota_day(),
    })
    try:
        asyncio.run(bot.cmd_stats(incoming))
        text = sent["text"]
        assert "gemini-3.6-flash: 3 / 20" in text
        assert "gemini-other: 1\n" in text
        assert "gemini-dead-model" not in text
        assert "без обращений" in text
        assert "Σ: 4" in text
        assert f"Σ: 5 / {or_limit} (осталось {max(0, or_limit - 5)})" in text
        assert f"Σ: 0 / {groq_limit} (осталось {groq_limit})" in text
        assert len(text) <= bot.TG_MAX_LEN
    finally:
        bot.OWNER_ID = original_owner
        bot._tg_call = original_tg_call
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real_quota)
        bot.chat_state.pop(chat_id, None)
        lumen_chat_state.chat_state.pop(chat_id, None)


def test_cmd_stats_shows_day_counters_webhook_memory_storage_and_fits_limit(monkeypatch):
    # Что защищает: новый блок /stats (сутки/вебхук/память/хранилище) — без теста
    # он молча протухает при рефакторинге команды, а нули в нём не отличить от
    # "счётчики не считаются". Регрессия: счётчики есть в GLOBAL_QUOTA, но команда
    # их не показывает. Старые тесты проверяют только модели и активные чаты.
    import lumen_chat_state as lcs
    chat_id = 999810
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777001)
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    async def fake_webhook_info():
        return SimpleNamespace(pending_update_count=3, last_error_message="")

    monkeypatch.setattr(bot, "OWNER_ID", 777001)
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(get_webhook_info=fake_webhook_info))
    monkeypatch.setattr(lcs, "_LAST_STORAGE_WRITE_TS", 1728000000.0)
    real_quota = dict(bot.GLOBAL_QUOTA)
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({
        "quota_day": bot._current_quota_day(),
        "user_daily": {"1": {"total": 2, "gemini": 1, "tts": 0, "bonus": 0}},
        "stats": {"messages_received": 7, "answers_sent": 5, "all_failed": 1, "fallbacks": 2, "daily_limit_denials": 3},
    })
    try:
        asyncio.run(bot.cmd_stats(incoming))
        text = sent["text"]
        assert "Сборка:" in text
        assert "Пользователей за сутки: 1" in text
        assert "Сообщений получено: 7, ответов отправлено: 5" in text
        assert "Все модели отказали: 1" in text
        assert "переключений на резерв: 2" in text
        assert "отказов по лимиту: 3" in text
        assert "Вебхук: pending=3, ошибок нет" in text
        assert "Память:" in text
        assert "Хранилище: локальный диск, запись:" in text
        assert len(text) <= bot.TG_MAX_LEN
    finally:
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real_quota)
        bot.chat_state.pop(chat_id, None)
        lcs.chat_state.pop(chat_id, None)


def test_cmd_stats_webhook_error_and_failure_paths(monkeypatch):
    # Что защищает: строки вебхука при ошибке Telegram и при недоступности API —
    # без теста сбой getWebhookInfo ронял бы всю команду исключением.
    # Регрессия: необёрнутый await в команде. Старые тесты вебхук не трогают.
    import lumen_chat_state as lcs
    chat_id = 999809
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    def _run_with_bot(fake_bot):
        incoming = _FakeIncomingMessage(chat_id)
        incoming.from_user = SimpleNamespace(id=777001)
        monkeypatch.setattr(bot, "OWNER_ID", 777001)
        monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
        monkeypatch.setattr(bot, "bot", fake_bot)
        real_quota = dict(bot.GLOBAL_QUOTA)
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update({"quota_day": bot._current_quota_day()})
        try:
            asyncio.run(bot.cmd_stats(incoming))
            return sent["text"]
        finally:
            bot.GLOBAL_QUOTA.clear()
            bot.GLOBAL_QUOTA.update(real_quota)
            bot.chat_state.pop(chat_id, None)
            lcs.chat_state.pop(chat_id, None)

    async def _info_with_error():
        return SimpleNamespace(pending_update_count=1, last_error_message="Bad Gateway")

    text = _run_with_bot(SimpleNamespace(get_webhook_info=_info_with_error))
    assert "Вебхук: pending=1, ошибка: Bad Gateway" in text

    async def _info_failing():
        raise RuntimeError("сеть недоступна")

    text = _run_with_bot(SimpleNamespace(get_webhook_info=_info_failing))
    assert "Вебхук: н/д" in text

    text = _run_with_bot(None)
    assert "Вебхук: н/д" in text


def test_cmd_stats_truncation_keeps_html_valid(monkeypatch):
    # Защищает обрезку длинного /stats: рваный тег ронял бы отправку 400 при
    # parse_mode=HTML. Регрессия — наивный срез по символам. Старый тест длины
    # короткий вывод не ловит.
    import lumen_chat_state as lcs
    chat_id = 999808
    incoming = _FakeIncomingMessage(chat_id)
    incoming.from_user = SimpleNamespace(id=777001)
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    async def fake_webhook_info():
        return SimpleNamespace(pending_update_count=0, last_error_message="")

    monkeypatch.setattr(bot, "OWNER_ID", 777001)
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(get_webhook_info=fake_webhook_info))
    real_quota = dict(bot.GLOBAL_QUOTA)
    many = {f"model-{i:04d}-with-long-name": {"used": 1, "exhausted_at": None} for i in range(400)}
    bot.GLOBAL_QUOTA.clear()
    bot.GLOBAL_QUOTA.update({
        "quota_day": bot._current_quota_day(),
        "gemini": dict(many),
        "openrouter": dict(many),
        "groq": dict(many),
    })
    try:
        asyncio.run(bot.cmd_stats(incoming))
        text = sent["text"]
        assert len(text) <= bot.TG_MAX_LEN
        lt, gt = text.rfind("<"), text.rfind(">")
        assert not (lt > gt), "обрезка разорвала HTML-тег"
    finally:
        bot.GLOBAL_QUOTA.clear()
        bot.GLOBAL_QUOTA.update(real_quota)
        bot.chat_state.pop(chat_id, None)
        lcs.chat_state.pop(chat_id, None)


def test_build_version_caches_git_call(monkeypatch):
    # Защищает кеш версии: git-вызов блокирует loop до 5с, без кеша каждый
    # /stats стопает бота. Регрессия — прямой subprocess при каждом вызове.
    import lumen_commands as lc
    monkeypatch.setattr(lc, "_BUILD_VERSION_CACHED", None)
    calls = []

    class _Proc:
        returncode = 0
        stdout = "abc1234\n"

    def fake_run(*args, **kwargs):
        calls.append(1)
        return _Proc()

    monkeypatch.setattr(lc.subprocess, "run", fake_run)
    try:
        assert lc._build_version() == "abc1234"
        assert lc._build_version() == "abc1234"
        assert len(calls) == 1
    finally:
        monkeypatch.setattr(lc, "_BUILD_VERSION_CACHED", None)


def test_match_trigger_prefix_finds_draw_trigger():    assert bot._match_trigger_prefix("нарисуй кота на пляже", bot.DRAW_TRIGGER_PREFIXES) == "нарисуй"


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


def test_match_trigger_prefix_english_triggers():
    # Внешний аудит: /start обещает "draw a cat"/"read this out loud", а триггеры были только русские.
    assert bot._match_trigger_prefix("draw a cat on the beach", bot.DRAW_TRIGGER_PREFIXES) == "draw a"
    assert bot._match_trigger_prefix("generate an image of a castle", bot.DRAW_TRIGGER_PREFIXES) == "generate an image"
    assert bot._match_trigger_prefix("read this out loud please", bot.TTS_TRIGGER_PREFIXES) == "read this out loud"
    assert bot._match_trigger_prefix("voice this message", bot.TTS_TRIGGER_PREFIXES) == "voice this"
    assert bot._match_trigger_prefix("how are you", bot.DRAW_TRIGGER_PREFIXES) is None
    assert bot._match_trigger_prefix("how are you", bot.TTS_TRIGGER_PREFIXES) is None


def test_match_trigger_prefix_rejects_draw_idiom():
    # Ревью ветки: "draw some" ловил идиому "draw conclusions".
    assert bot._match_trigger_prefix("draw some conclusions here", bot.DRAW_TRIGGER_PREFIXES) is None


def test_tts_trigger_this_with_reply_voices_replied_message(rate_guard_setup):
    # Регрессия (сентябрь 2026): "озвучь это" в ответ на сообщение озвучивало
    # само слово "это" — остаток после триггера считался содержанием.
    message = rate_guard_setup()
    message.text = "озвучь это"
    message.reply_to_message = SimpleNamespace(text="текст из реплая", caption=None)
    asyncio.run(bot._handle_message_core(message))
    bot.inline_tts.assert_awaited_once_with(message, "текст из реплая")


def test_draw_failure_replies_when_status_edit_fails(rate_guard_setup, monkeypatch):
    # Внешний аудит: статус снесён до send_photo, правка после падения билась в пустоту.
    message = rate_guard_setup()
    message.text = "/draw cat"
    replied = []

    async def fake_tg_call(method, *args, **kwargs):
        return SimpleNamespace()

    async def fake_edit_quietly(msg, text, **kwargs):
        return False

    async def fake_safe_reply(msg, text, **kwargs):
        replied.append(text)

    async def failing_generate(session, model_id, prompt):
        raise RuntimeError("all image generation models unavailable")

    async def fake_get_http_session():
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "_edit_message_quietly", fake_edit_quietly)
    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    monkeypatch.setattr(bot, "_pollinations_text_to_image", failing_generate)
    monkeypatch.setattr(bot, "_get_http_session", fake_get_http_session)
    # Фикстура rate_guard_setup глушит пайплайны AsyncMock — возвращаем настоящий.
    monkeypatch.setattr(bot, "inline_draw", lumen_commands.inline_draw)
    asyncio.run(bot.cmd_draw(message))
    assert len(replied) == 1
    # Дефолтный язык чата — английский, сверяем смысл, а не конкретный текст.
    assert "unavailable" in replied[0].lower() or "fail" in replied[0].lower()
    bot.chat_state.pop(123, None)


def test_tts_failure_replies_when_status_edit_fails(rate_guard_setup, monkeypatch):
    # То же для озвучки: send_voice упал — пользователь всё равно видит ошибку.
    message = rate_guard_setup()
    message.text = "/tts привет"
    replied = []

    async def fake_tg_call(method, *args, **kwargs):
        return SimpleNamespace()

    async def fake_edit_quietly(msg, text, **kwargs):
        return False

    async def fake_safe_reply(msg, text, **kwargs):
        replied.append(text)

    async def failing_synth(text):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "_edit_message_quietly", fake_edit_quietly)
    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    monkeypatch.setattr(bot, "_gemini_tts_bytes", failing_synth)
    # Фикстура rate_guard_setup глушит пайплайны AsyncMock — возвращаем настоящий.
    monkeypatch.setattr(bot, "inline_tts", lumen_commands.inline_tts)
    asyncio.run(bot.cmd_tts(message))
    assert len(replied) == 1
    bot.chat_state.pop(123, None)


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


def test_cmd_start_respects_rate_limit(monkeypatch):
    # Регрессия (аудит 26.09.2026): /start шёл мимо лимита, потому что его хендлер
    # стоит раньше общего catch-all — в группе спамер получал ответ на каждый вызов.
    called = {}

    class _FakeStartMessage:
        chat = SimpleNamespace(id=2, type=bot.ChatType.PRIVATE)

        async def reply(self, text, **kwargs):
            called["text"] = text
            return SimpleNamespace()

    monkeypatch.setattr(bot, "_reject_rate_limited_message", AsyncMock(return_value=True))
    asyncio.run(bot.cmd_start(_FakeStartMessage()))
    assert called == {}


def test_cmd_start_mentions_every_current_command_and_not_removed_ones(monkeypatch):
    captured = {}

    class _FakeStartMessage:
        chat = SimpleNamespace(id=1, type=bot.ChatType.PRIVATE)

        async def reply(self, text, **kwargs):
            captured["text"] = text
            return SimpleNamespace()

    # /start теперь под лимитом (иначе спамер в группе гонял ответы без ограничения).
    monkeypatch.setattr(bot, "_reject_rate_limited_message", AsyncMock(return_value=False))
    asyncio.run(bot.cmd_start(_FakeStartMessage()))
    text = captured["text"]
    assert "/draw" in text
    assert "/tts" in text
    assert "/reset" in text
    assert "/lang" in text
    assert "/imgmodel" not in text
    # Онбординг: живые примеры вместо голой простыни (чат 1 — дефолт en).
    assert "Try right now" in text
    from lumen_lang import t as _lang_t_direct
    assert "Попробуй прямо сейчас" in _lang_t_direct("ru", "start_text")


@pytest.mark.parametrize("chat_type,is_group", [
    (bot.ChatType.PRIVATE, False),
    (bot.ChatType.GROUP, True),
    (bot.ChatType.SUPERGROUP, True),
])
def test_cmd_start_group_notice_only_in_groups(monkeypatch, chat_type, is_group):
    # Пометка про фон группы (до 100 сообщений уходит провайдерам) терялась бы молча:
    # старые тесты гоняли /start только в личке.
    captured = {}

    class _FakeStartMessage:
        chat = SimpleNamespace(id=9101, type=chat_type)

        async def reply(self, text, **kwargs):
            captured["text"] = text
            return SimpleNamespace()

    monkeypatch.setattr(bot, "_reject_rate_limited_message", AsyncMock(return_value=False))
    try:
        asyncio.run(bot.cmd_start(_FakeStartMessage()))
        text = captured["text"]
        from lumen_lang import t as _lang_t_direct
        notice = _lang_t_direct("en", "group_history_notice")
        if is_group:
            assert notice in text
            assert "100" in text
        else:
            assert notice not in text
    finally:
        bot.chat_state.pop(9101, None)


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
        asyncio.run(bot.inline_draw(incoming, "космическая станция"))
        # 429 от сервиса обрывает ЦЕПОЧКУ: вторая модель не тратит попытку.
        assert attempts == [bot._pick_image_model("космическая станция")]
        # Проверяем ФИНАЛЬНЫЙ текст статуса (то, что реально увидит человек), а не
        # "хоть где-то в правках есть слово" — раньше тест проходил на любой правке.
        final_status_text = incoming.sent[0].edits[-1][0]
        assert final_status_text == bot._t(chat_id, "draw_err_overloaded")
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


def test_match_pick_request_rejects_verb_forms_of_igrat():
    # Аудит A6-6: префикс "игр" ловил глаголы — "кто играет" не запрос игры.
    assert bot.match_pick_request("посоветуй кто играет сегодня") is None


def test_match_pick_request_accepts_game_nouns_and_infinitives():
    # Сторож: существительные и инфинитивы по-прежнему ведут в games.
    assert bot.match_pick_request("придумай игру для компании") == "games"
    assert bot.match_pick_request("посоветуй игры") == "games"
    assert bot.match_pick_request("посоветуй во что поиграть") == "games"


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


def test_pick_callback_foreign_tap_does_not_burn_token(monkeypatch):
    # Регрессия: pop до проверки владельца сжигал кнопку — чужак одним тапом
    # лишал владельца выбора. Теперь чужой тап отклоняется, свой после — работает.
    bot._pending_picks.clear()
    bot._pending_picks["dd44ee55"] = {
        "chat_id": 777, "user_id": 111, "scenario": "film",
        "original": "посоветуй фильм", "expires": time.monotonic() + 300,
        "lang": "ru",
    }
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        q_foreign = _make_pick_query("pick:dd44ee55:0", user_id=999)
        asyncio.run(bot.handle_pick_callback(q_foreign))
        assert any(alert is True for _, alert in q_foreign.answered)
        assert "dd44ee55" in bot._pending_picks
        q_owner = _make_pick_query("pick:dd44ee55:2")
        asyncio.run(bot.handle_pick_callback(q_owner))
        assert "Триллер" in q_owner.message.edits[0][0]
        fake_core.assert_awaited_once()
        assert "dd44ee55" not in bot._pending_picks
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


def test_pick_callback_expired_known_token_reissues_buttons_once(monkeypatch):
    # Протухший, но известный выбор — свежие кнопки вместо стены (один ресенд:
    # токен уже popped, следующий тап упрётся в rec None и покажет expired).
    bot._pending_picks.clear()
    bot._pending_picks["oldtok12"] = {
        "chat_id": 777, "user_id": 111, "scenario": "film",
        "original": "посоветуй фильм", "expires": time.monotonic() - 1,
        "lang": "ru",
    }
    q = _make_pick_query("pick:oldtok12:1")
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        asyncio.run(bot.handle_pick_callback(q))
        assert len(q.message.edits) == 1
        assert "oldtok12" not in bot._pending_picks
        assert len(bot._pending_picks) == 1
        fresh = next(iter(bot._pending_picks))
        assert fresh != "oldtok12"
        # Свежие кнопки идут обычным путём до конца.
        q2 = _make_pick_query(f"pick:{fresh}:2")
        asyncio.run(bot.handle_pick_callback(q2))
        assert "Триллер" in q2.message.edits[0][0]
        fake_core.assert_awaited_once()
        # А вот теперь токен и правда сгорел — дальше честное expired.
        q3 = _make_pick_query(f"pick:{fresh}:0")
        asyncio.run(bot.handle_pick_callback(q3))
        assert any("expired" in (text or "").lower() or "протух" in (text or "").lower() for text, _ in q3.answered)
        fake_core.assert_awaited_once()
    finally:
        bot._pending_picks.clear()


def test_pick_callback_stranger_cannot_reissue_expired_pick(monkeypatch):
    # Регрессия (враждебное ревью 27.09.2026): проверка авторства стояла ПОСЛЕ
    # перевыпуска кнопок, поэтому чужой тап по протухшей кнопке продлевал чужой
    # выбор новыми кнопками. Теперь чужак получает отказ и ничего не возникает.
    bot._pending_picks.clear()
    bot._pending_picks["oldtok99"] = {
        "chat_id": 777, "user_id": 111, "scenario": "film",
        "original": "посоветуй фильм", "expires": time.monotonic() - 1,
        "lang": "ru",
    }
    # user_id=999 — не владелец записи (владелец 111).
    q = _make_pick_query("pick:oldtok99:1", user_id=999)
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        asyncio.run(bot.handle_pick_callback(q))
        assert q.message.edits == [], "чужим нельзя перевыпускать кнопки"
        assert len(bot._pending_picks) == 1, "у чужого тапа не должно появляться записей"
        assert "oldtok99" in bot._pending_picks, "запись владельца остаётся нетронутой"
        # Реплика берётся языком самой записи (здесь lang="ru"), а не языком чата.
        from lumen_lang import t as _lang_t
        expected = _lang_t("ru", "pick_not_yours")
        assert any(text == expected for text, _ in q.answered), (q.answered, expected)
        fake_core.assert_not_awaited()
    finally:
        bot._pending_picks.clear()


def test_pick_callback_ownerless_record_confined_to_its_chat(monkeypatch):
    # Вопрос от поста канала/анонима (from_user пустой): user_id=None, и раньше
    # кнопку мог нажать кто угодно. Теперь выбор живёт только в своём чате.
    bot._pending_picks.clear()
    bot._pending_picks["ownrless1"] = {
        "chat_id": 777, "user_id": None, "scenario": "film",
        "original": "посоветуй фильм", "expires": time.monotonic() + 300,
        "lang": "ru",
    }
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    try:
        q_other = _make_pick_query("pick:ownrless1:0")
        q_other.message.chat = SimpleNamespace(id=999, type=bot.ChatType.PRIVATE)
        asyncio.run(bot.handle_pick_callback(q_other))
        assert any(alert is True for _, alert in q_other.answered)
        assert "ownrless1" in bot._pending_picks
        fake_core.assert_not_awaited()
        q_own = _make_pick_query("pick:ownrless1:2")
        asyncio.run(bot.handle_pick_callback(q_own))
        assert "Триллер" in q_own.message.edits[0][0]
        fake_core.assert_awaited_once()
    finally:
        bot._pending_picks.clear()


def test_callback_handlers_have_data_prefix_filters():
    # Регрессия 25.09.2026: handle_lang_callback висел первым БЕЗ фильтра и по
    # правилу first match wins съедал вообще все callback_query — кнопки pick
    # рисовались, но нажатия до handle_pick_callback не доходили никогда.
    handlers = bot.dp.callback_query.handlers
    by_fn = {h.callback.__name__: h for h in handlers}
    assert "handle_lang_callback" in by_fn and "handle_pick_callback" in by_fn
    assert by_fn["handle_lang_callback"].filters, "lang handler must not be a catch-all"
    assert by_fn["handle_pick_callback"].filters, "pick handler must not be a catch-all"


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


def test_inline_draw_sends_photo_via_tg_call(monkeypatch):
    # Регрессия A8-01: send_photo идёт через _tg_call (breaker/таймаут/RetryAfter), а не напрямую.
    chat_id = 999433
    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    calls = []

    async def fake_pollinations(session, model_id, prompt):
        return b"\x89PNG fake bytes"

    async def fake_tg_call(method, *args, **kwargs):
        calls.append((method, kwargs))
        if "photo" in kwargs:
            return SimpleNamespace()
        return await method(*args, **kwargs)

    direct = AsyncMock()
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_pollinations)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_photo=direct))
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй кота"))
    finally:
        bot.chat_state.pop(chat_id, None)
    assert any("photo" in kw for _, kw in calls)
    send_kwargs = next(kw for _, kw in calls if "photo" in kw)
    assert send_kwargs.get("call_timeout") == bot.TELEGRAM_MEDIA_TIMEOUT
    direct.assert_not_awaited()


def test_inline_draw_send_failure_reports_service_error(monkeypatch):
    # Регрессия A8-01: _tg_call вернул None (сеть/брейкер) — пользователь видит ошибку сервиса.
    chat_id = 999434
    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    replied = []

    async def fake_pollinations(session, model_id, prompt):
        return b"\x89PNG fake bytes"

    async def fake_tg_call(method, *args, **kwargs):
        if "photo" in kwargs:
            return None
        return await method(*args, **kwargs)

    async def fake_edit_quietly(msg, text, **kwargs):
        replied.append(text)
        return True

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(bot, "_pollinations_text_to_image", fake_pollinations)
    monkeypatch.setattr(bot, "_edit_message_quietly", fake_edit_quietly)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_photo=AsyncMock()))
    try:
        asyncio.run(bot.inline_draw(incoming, "нарисуй кота"))
    finally:
        bot.chat_state.pop(chat_id, None)
    assert replied and replied[-1] == bot._t(chat_id, "draw_err_unavailable")
    bot.chat_state.pop(chat_id, None)


def test_inline_tts_sends_voice_via_tg_call(monkeypatch):
    # Регрессия A8-01: send_voice идёт через _tg_call, а не напрямую.
    chat_id = 999435
    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1
    calls = []

    async def fake_tg_call(method, *args, **kwargs):
        calls.append((method, kwargs))
        if "voice" in kwargs:
            return SimpleNamespace()
        return await method(*args, **kwargs)

    async def fake_synth(text):
        return (b"fake-ogg", "speech.ogg", 5)

    direct = AsyncMock()
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", fake_synth)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_voice=direct))
    try:
        asyncio.run(bot.inline_tts(incoming, "привет"))
    finally:
        bot.chat_state.pop(chat_id, None)
    assert any("voice" in kw for _, kw in calls)
    send_kwargs = next(kw for _, kw in calls if "voice" in kw)
    assert send_kwargs.get("call_timeout") == bot.TELEGRAM_MEDIA_TIMEOUT
    direct.assert_not_awaited()


def test_inline_tts_send_failure_raises(monkeypatch):
    # Регрессия A8-01: None от _tg_call — наружу исключение, как раньше при прямом вызове.
    chat_id = 999436
    incoming = _FakeIncomingMessage(chat_id)
    incoming.message_id = 1

    async def fake_tg_call(method, *args, **kwargs):
        if "voice" in kwargs:
            return None
        return await method(*args, **kwargs)

    async def fake_synth(text):
        return (b"fake-ogg", "speech.ogg", 5)

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(lumen_commands, "_synthesize_tts_voice", fake_synth)
    monkeypatch.setattr(bot, "bot", SimpleNamespace(send_voice=AsyncMock(return_value=SimpleNamespace())))
    try:
        with pytest.raises(RuntimeError, match="send_voice"):
            asyncio.run(bot.inline_tts(incoming, "привет"))
    finally:
        bot.chat_state.pop(chat_id, None)


def _make_selftest_message(chat_id, user_id, text="/selftest", group=False):
    incoming = _FakeIncomingMessage(chat_id)
    if group:
        incoming.chat.type = bot.ChatType.GROUP
    incoming.from_user = SimpleNamespace(id=user_id)
    incoming.text = text
    return incoming


def _setup_selftest_fakes(monkeypatch, fail_heads=()):
    # Фейковые пробы голов и сети + захват ответа. Возвращает (calls, fake_tg_call).
    import lumen_commands
    calls = []

    async def fake_head(provider, *, chat_id=None):
        calls.append(provider)
        if provider in fail_heads:
            return False, "boom-500", 0.5
        return True, "", 0.1

    async def fake_session():
        return SimpleNamespace()

    async def fake_probe(session, url, *, timeout_sec=6.0, redact=""):
        return {"status": 200, "elapsed_sec": 0.2, "ok": True}

    async def fake_tg_call(method, *args, **kwargs):
        fake_tg_call.text = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    monkeypatch.setattr(bot, "selftest_llm_head", fake_head)
    monkeypatch.setattr(bot, "_get_http_session", fake_session)
    monkeypatch.setattr(bot, "probe_url", fake_probe)
    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    monkeypatch.setattr(lumen_commands, "_SELFTEST_LAST_RUN_MONOTONIC", 0.0)
    return calls, fake_tg_call


def test_cmd_selftest_denies_non_owner_without_probing(monkeypatch):
    # Чужой не должен даже запускать пробы: иначе любой сливал бы квоту и видел внутрянку.
    monkeypatch.setattr(bot, "OWNER_ID", 111001)
    calls, fake_tg_call = _setup_selftest_fakes(monkeypatch)
    incoming = _make_selftest_message(999701, 222002)
    asyncio.run(bot.cmd_selftest(incoming))
    assert fake_tg_call.text == bot._t(999701, "stats_deny")
    assert calls == []


def test_cmd_selftest_group_only_for_owner(monkeypatch):
    # ID моделей в группы не отдаём: команда в группах только отказывает, проб нет.
    from lumen_router_config import _GROQ_LIGHT_ORDER, _OR_LIGHT_ORDER, GEMINI_DEFAULT_CHAIN
    monkeypatch.setattr(bot, "OWNER_ID", 333001)
    calls, fake_tg_call = _setup_selftest_fakes(monkeypatch)
    incoming = _make_selftest_message(999702, 333001, group=True)
    asyncio.run(bot.cmd_selftest(incoming))
    assert fake_tg_call.text == bot._t(999702, "selftest_group_only")
    assert calls == []
    for mid in list(_GROQ_LIGHT_ORDER) + list(_OR_LIGHT_ORDER) + list(GEMINI_DEFAULT_CHAIN):
        assert mid not in fake_tg_call.text


def test_cmd_selftest_cooldown_blocks_second_run(monkeypatch):
    # Без паузы владелец случайно выжигал бы квоту повторными тапами.
    monkeypatch.setattr(bot, "OWNER_ID", 444001)
    calls, fake_tg_call = _setup_selftest_fakes(monkeypatch)
    asyncio.run(bot.cmd_selftest(_make_selftest_message(999703, 444001)))
    assert calls == ["groq", "openrouter"]
    asyncio.run(bot.cmd_selftest(_make_selftest_message(999703, 444001)))
    assert calls == ["groq", "openrouter"]
    assert fake_tg_call.text == bot._t(999703, "selftest_cooldown", sec=60)


def test_selftest_short_error_redacts_org_id():
    # Прод 05.10.2026: текст Groq-ошибки тащил ID организации — внутрянка даже в личке.
    import lumen_routes
    err = lumen_routes._selftest_short_error(RuntimeError(
        "Request too large for model `qwen/qwen3.8-27b` in organization `org_01m321tq18eqksff96hz17m7es` service tier"))
    assert "org_01m321tq18eqksff96hz17m7es" not in err
    assert "<ORG>" in err


def test_cmd_selftest_default_skips_gemini_and_hides_model_ids(monkeypatch):
    # Дефолт без gemini бережёт самую дефицитную квоту; ID моделей в ответе нет.
    from lumen_router_config import _GROQ_LIGHT_ORDER, _OR_LIGHT_ORDER, GEMINI_DEFAULT_CHAIN
    monkeypatch.setattr(bot, "OWNER_ID", 555001)
    calls, fake_tg_call = _setup_selftest_fakes(monkeypatch)
    asyncio.run(bot.cmd_selftest(_make_selftest_message(999704, 555001)))
    assert calls == ["groq", "openrouter"]
    assert bot._t(999704, "selftest_gemini_skipped") in fake_tg_call.text
    for mid in list(_GROQ_LIGHT_ORDER) + list(_OR_LIGHT_ORDER) + list(GEMINI_DEFAULT_CHAIN):
        assert mid not in fake_tg_call.text


def test_cmd_selftest_with_gemini_arg_probes_gemini(monkeypatch):
    # Явный аргумент включает пробу Gemini.
    monkeypatch.setattr(bot, "OWNER_ID", 666001)
    calls, fake_tg_call = _setup_selftest_fakes(monkeypatch)
    asyncio.run(bot.cmd_selftest(_make_selftest_message(999705, 666001, "/selftest gemini")))
    assert calls == ["groq", "openrouter", "gemini"]
    assert bot._t(999705, "selftest_gemini_skipped") not in fake_tg_call.text


def test_cmd_selftest_reports_failed_head(monkeypatch):
    # Упавшая голова — строка FAIL с причиной, а не молчание или падение команды.
    monkeypatch.setattr(bot, "OWNER_ID", 777001)
    calls, fake_tg_call = _setup_selftest_fakes(monkeypatch, fail_heads=("groq",))
    asyncio.run(bot.cmd_selftest(_make_selftest_message(999706, 777001)))
    assert "Groq: FAIL" in fake_tg_call.text
    assert "boom-500" in fake_tg_call.text
    assert "OpenRouter: OK" in fake_tg_call.text


def test_selftest_llm_head_openrouter_success_targets_head_and_counts_quota(monkeypatch):
    # Честный учёт: успешная проба идёт ровно в голову лёгкого маршрута и +1 в квоту.
    import lumen_model_speed
    import lumen_routes
    from lumen_router_config import _OR_LIGHT_ORDER
    head = _OR_LIGHT_ORDER[0]
    seen = {}

    async def fake_or(path, method="GET", *, json_body=None, deadline=None):
        seen["model"] = (json_body or {}).get("model")
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 4001, "completion_tokens": 2, "total_tokens": 4003}}

    monkeypatch.setattr(bot, "_or_request", fake_or)
    before = ((bot.GLOBAL_QUOTA.get("openrouter") or {}).get(head) or {}).get("used") or 0
    ema_before = dict(lumen_model_speed._latency_ema)
    try:
        ok, detail, elapsed = asyncio.run(lumen_routes.selftest_llm_head("openrouter", chat_id=1))
    finally:
        lumen_model_speed._latency_ema.clear()
        lumen_model_speed._latency_ema.update(ema_before)
    assert ok is True and detail == "" and elapsed >= 0
    assert seen["model"] == head
    after = ((bot.GLOBAL_QUOTA.get("openrouter") or {}).get(head) or {}).get("used") or 0
    assert after == before + 1
    bot.GLOBAL_QUOTA["openrouter"][head]["used"] = before


def test_selftest_llm_head_openrouter_error_returns_false_without_quota(monkeypatch):
    # Неуспех квоту не трогает (как в ask_*): иначе ошибка стоила бы как ответ.
    import lumen_routes
    from lumen_router_config import _OR_LIGHT_ORDER
    head = _OR_LIGHT_ORDER[0]

    async def boom(path, method="GET", *, json_body=None, deadline=None):
        raise RuntimeError("boom-429")

    monkeypatch.setattr(bot, "_or_request", boom)
    before = ((bot.GLOBAL_QUOTA.get("openrouter") or {}).get(head) or {}).get("used") or 0
    ok, detail, elapsed = asyncio.run(lumen_routes.selftest_llm_head("openrouter", chat_id=1))
    assert ok is False and "boom-429" in detail and elapsed >= 0
    after = ((bot.GLOBAL_QUOTA.get("openrouter") or {}).get(head) or {}).get("used") or 0
    assert after == before


def test_cmd_stats_shows_quarantine(monkeypatch):
    # Карантин виден владельцу в /stats: иначе объезд модели выглядел бы магией.
    monkeypatch.setattr(bot, "OWNER_ID", 888001)
    for _ in range(3):
        bot._record_model_outcome("openrouter", "qs-model:free", bad=True)
    incoming = _FakeIncomingMessage(999707)
    incoming.from_user = SimpleNamespace(id=888001)
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    asyncio.run(bot.cmd_stats(incoming))
    assert "Карантин моделей" in sent["text"]
    assert "qs-model:free" in sent["text"]


def _make_ban_message(chat_id, user_id, text, group=False, reply_to=None):
    incoming = _FakeIncomingMessage(chat_id)
    if group:
        incoming.chat.type = bot.ChatType.GROUP
    incoming.from_user = SimpleNamespace(id=user_id)
    incoming.text = text
    incoming.reply_to_message = reply_to
    return incoming


def _capture_replies(monkeypatch):
    sent = {}

    async def fake_tg_call(method, *args, **kwargs):
        sent["text"] = args[0] if args else kwargs.get("text", "")
        return SimpleNamespace()

    monkeypatch.setattr(bot, "_tg_call", fake_tg_call)
    return sent


def test_ban_flow_by_id_unban_and_list(monkeypatch):
    # Полный цикл: бан по ID, список, разбан, повторный разбан — мимо.
    monkeypatch.setattr(bot, "OWNER_ID", 101001)
    sent = _capture_replies(monkeypatch)
    assert bot._is_banned(777002) is False
    asyncio.run(bot.cmd_ban(_make_ban_message(999801, 101001, "/ban 777002")))
    assert sent["text"] == bot._t(999801, "ban_done", user_id=777002)
    assert bot._is_banned(777002) is True
    asyncio.run(bot.cmd_banlist(_make_ban_message(999801, 101001, "/banlist")))
    assert "777002" in sent["text"]
    asyncio.run(bot.cmd_unban(_make_ban_message(999801, 101001, "/unban 777002")))
    assert sent["text"] == bot._t(999801, "unban_done", user_id=777002)
    assert bot._is_banned(777002) is False
    asyncio.run(bot.cmd_unban(_make_ban_message(999801, 101001, "/unban 777002")))
    assert sent["text"] == bot._t(999801, "unban_missing", user_id=777002)
    asyncio.run(bot.cmd_banlist(_make_ban_message(999801, 101001, "/banlist")))
    assert sent["text"] == bot._t(999801, "banlist_empty")


def test_ban_by_reply_and_usage_and_owner_refuse(monkeypatch):
    # Цель ответом на сообщение; мусор вместо ID — подсказка; владельца — отказ.
    monkeypatch.setattr(bot, "OWNER_ID", 102001)
    sent = _capture_replies(monkeypatch)
    replied = SimpleNamespace(from_user=SimpleNamespace(id=555003))
    asyncio.run(bot.cmd_ban(_make_ban_message(999802, 102001, "/ban", reply_to=replied)))
    assert bot._is_banned(555003) is True
    asyncio.run(bot.cmd_ban(_make_ban_message(999802, 102001, "/ban вообще")))
    assert sent["text"] == bot._t(999802, "ban_usage")
    asyncio.run(bot.cmd_ban(_make_ban_message(999802, 102001, "/ban 102001")))
    assert sent["text"] == bot._t(999802, "ban_owner_refuse")
    assert bot._is_banned(102001) is False


def test_ban_denies_non_owner_and_group(monkeypatch):
    # Чужой и группа: только отказ, список не меняется.
    monkeypatch.setattr(bot, "OWNER_ID", 103001)
    sent = _capture_replies(monkeypatch)
    asyncio.run(bot.cmd_ban(_make_ban_message(999803, 999999, "/ban 123")))
    assert sent["text"] == bot._t(999803, "stats_deny")
    asyncio.run(bot.cmd_ban(_make_ban_message(999803, 103001, "/ban 123", group=True)))
    assert sent["text"] == bot._t(999803, "ban_group_only")
    assert bot._is_banned(123) is False


def test_handle_message_ignores_banned_user(monkeypatch):
    # Заблокированный: ни ответа, ни ядра (там контекст группы и счётчики лимитов).
    from unittest.mock import AsyncMock
    monkeypatch.setattr(bot, "OWNER_ID", 104001)
    bot._ban_user(605001)
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)

    def _msg(chat_type, text="привет"):
        return SimpleNamespace(
            media_group_id=None,
            chat=SimpleNamespace(id=999804, type=chat_type),
            from_user=SimpleNamespace(id=605001),
            text=text, caption=None, reply_to_message=None,
        )

    asyncio.run(bot.handle_message(_msg(bot.ChatType.PRIVATE)))
    asyncio.run(bot.handle_message(_msg(bot.ChatType.GROUP)))
    assert fake_core.await_count == 0
    bot._unban_user(605001)
    asyncio.run(bot.handle_message(_msg(bot.ChatType.GROUP)))
    assert fake_core.await_count == 1


def test_banned_list_survives_quota_reload(monkeypatch):
    # Список живёт в том же файле, что квота: рестарт его не сбрасывает.
    import json
    monkeypatch.setattr(bot, "OWNER_ID", 105001)
    bot._ban_user(606001)
    snapshot = json.dumps(dict(bot.GLOBAL_QUOTA), ensure_ascii=False)
    monkeypatch.setattr(bot, "_storage_read_text", lambda *args, **kwargs: snapshot)
    bot.GLOBAL_QUOTA.pop(bot.BANNED_KEY, None)
    assert bot._is_banned(606001) is False
    bot.load_global_quota()
    assert bot._is_banned(606001) is True


def test_pick_callback_ignores_banned_user(monkeypatch):
    # Забаненный не отвечает и кнопками: иначе игнор обходился живыми пиками.
    from unittest.mock import AsyncMock
    monkeypatch.setattr(bot, "OWNER_ID", 106001)
    bot._ban_user(607001)
    fake_core = AsyncMock()
    monkeypatch.setattr(bot, "_handle_message_core", fake_core)
    answered = []

    async def fake_answer(*args, **kwargs):
        answered.append(True)

    query = SimpleNamespace(
        data="pick:unknown-token:0",
        from_user=SimpleNamespace(id=607001),
        message=None,
        answer=fake_answer,
    )
    asyncio.run(bot.handle_pick_callback(query))
    assert fake_core.await_count == 0
    assert answered == [True]


def test_selftest_llm_head_gemini_success_counts_quota_and_outcome(monkeypatch):
    # Проба Gemini как у остальных: успех в квоту и сброс счётчика карантина.
    import lumen_model_speed
    import lumen_routes
    from lumen_router_config import GEMINI_DEFAULT_CHAIN
    head = GEMINI_DEFAULT_CHAIN[0]

    class _FakeModels:
        def __init__(self):
            self.calls = []

        async def generate_content(self, *, model, contents, config=None):
            self.calls.append(model)
            return SimpleNamespace(text="ok")

    fake_models = _FakeModels()
    monkeypatch.setattr(bot, "client", SimpleNamespace(aio=SimpleNamespace(models=fake_models)))
    before = ((bot.GLOBAL_QUOTA.get("gemini") or {}).get(head) or {}).get("used") or 0
    ema_before = dict(lumen_model_speed._latency_ema)
    try:
        ok, detail, elapsed = asyncio.run(lumen_routes.selftest_llm_head("gemini", chat_id=1))
    finally:
        lumen_model_speed._latency_ema.clear()
        lumen_model_speed._latency_ema.update(ema_before)
    assert ok is True and detail == "" and elapsed >= 0
    assert fake_models.calls == [head]
    after = ((bot.GLOBAL_QUOTA.get("gemini") or {}).get(head) or {}).get("used") or 0
    assert after == before + 1
    bot.GLOBAL_QUOTA["gemini"][head]["used"] = before
    assert bot._is_quarantined("gemini", head) is False


def test_selftest_llm_head_gemini_empty_feeds_quarantine(monkeypatch):
    # Пустая проба Gemini тоже плохой исход, как в цепочке.
    import lumen_router_config
    import lumen_routes
    from lumen_router_config import GEMINI_DEFAULT_CHAIN
    head = GEMINI_DEFAULT_CHAIN[0]

    async def generate_empty(*, model, contents, config=None):
        return SimpleNamespace(text="")

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_empty)))
    monkeypatch.setattr(bot, "client", fake_client)
    ok, detail, _elapsed = asyncio.run(lumen_routes.selftest_llm_head("gemini", chat_id=1))
    assert ok is False and "empty response" in detail
    assert lumen_router_config._QUARANTINE.get(("gemini", head), {}).get("bad") == 1


def test_cmd_banlist_cuts_long_output(monkeypatch):
    # Сотни банов не роняют команду лимитом Telegram: режем по строкам.
    monkeypatch.setattr(bot, "OWNER_ID", 107001)
    sent = _capture_replies(monkeypatch)
    for uid in range(200001, 200601):
        bot._ban_user(uid)
    asyncio.run(bot.cmd_banlist(_make_ban_message(999808, 107001, "/banlist")))
    assert len(sent["text"]) <= bot.TG_MAX_LEN
    assert sent["text"].endswith("…")
