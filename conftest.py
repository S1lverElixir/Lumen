"""
conftest.py — настройка окружения ПЕРЕД импортом bot.py тестами.

bot.py на уровне модуля читает переменные окружения (BOT_TOKEN, GEMINI_API_KEY
и т.д.) и пишет лог-файл по пути из BOT_LOG_PATH (по умолчанию /app/bot.log).
Чтобы тесты можно было гонять где угодно (не только внутри Docker-контейнера
HF Spaces, где /app существует и доступен на запись), здесь выставляются
безопасные заглушки ДО того, как pytest соберёт и импортирует тестовые модули.

Ничего из этого не требует сети и не создаёт реальных Bot()/genai.Client() —
они создаются только внутри main(), которая тестами не вызывается.
"""
import os
import tempfile

os.environ.setdefault("BOT_LOG_PATH", os.path.join(tempfile.gettempdir(), "lumen_test_bot.log"))
os.environ.setdefault("BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import copy
import pytest


@pytest.fixture(autouse=True)
def _bot_global_state_guard():
    """Снимок наиболее часто вручную сохраняемых модульных глобалов bot.py перед
    каждым тестом и восстановление после (аудит техдолга, август 2026).

    Десятки тестов в test_bot_helpers.py вручную сохраняли/восстанавливали
    bot.chat_state/bot.GLOBAL_QUOTA/bot.client/bot.bot в try/finally — рабочий, но
    повторяющийся бойлерплейт и потенциальный источник тонких утечек между тестами
    при росте сьюта (забытый finally молча "протравливает" состояние в следующие
    тесты). Это ДОПОЛНИТЕЛЬНАЯ страховочная сетка, а не замена — существующие тесты
    со своим ручным cleanup продолжают работать точно так же, как раньше (двойное
    восстановление безвредно); новые тесты могут полагаться только на эту фикстуру
    и не писать собственный try/finally для этих четырёх глобалов.

    Намеренно НЕ снимается снимок вообще всего модульного состояния (_dirty_chat_ids,
    TELEGRAM_API_BASE_URL и т.п.) — тесты, которые их трогают, уже сами аккуратно
    восстанавливают именно то, что меняют (см. test_flush_dirty_state_once_* и
    аналогичные); расширять фикстуру на них без реальной необходимости было бы
    накоплением сложности "на будущее", а не решением подтверждённой проблемы."""
    import bot as _bot_module
    chat_state_snapshot = copy.deepcopy(_bot_module.chat_state)
    quota_snapshot = copy.deepcopy(_bot_module.GLOBAL_QUOTA)
    client_snapshot = _bot_module.client
    bot_snapshot = _bot_module.bot
    yield
    _bot_module.chat_state.clear()
    _bot_module.chat_state.update(chat_state_snapshot)
    _bot_module.GLOBAL_QUOTA.clear()
    _bot_module.GLOBAL_QUOTA.update(quota_snapshot)
    _bot_module.client = client_snapshot
    _bot_module.bot = bot_snapshot


@pytest.fixture(autouse=True)
def _instant_typing_pace():
    """Стриминг теперь искусственно "довыводит" остаток уже полученного, но ещё не
    показанного текста (см. STREAM_TYPING_*/_run_streaming_reply в bot.py и
    lumen_typing_pace.py) — несколько await bot._typing_sleep(...) между правками
    сообщения, чтобы визуально было похоже на живой набор текста. В тестах
    реальное ожидание не нужно и заметно замедлило бы весь сьют без единой пользы —
    bot._typing_sleep существует именно как точка подмены (тот же приём, что и
    bot._get_http_session/bot._openrouter_stream_pieces и т.п. в других местах),
    здесь она безусловно патчится на no-op для КАЖДОГО теста, а не только тех,
    что явно тестируют стриминг."""
    import bot as _bot_module
    original_typing_sleep = _bot_module._typing_sleep

    async def _instant_sleep(_seconds: float) -> None:
        return None

    _bot_module._typing_sleep = _instant_sleep
    yield
    _bot_module._typing_sleep = original_typing_sleep


@pytest.fixture(autouse=True)
def _instant_tikwm_throttle():
    """Тот же приём и та же причина, что и у _instant_typing_pace выше, только для
    троттлинга запросов к TikWM API (см. _tikwm_throttle/_fetch_tikwm_media_data в
    lumen_tiktok.py, добавлено при отладке 403-ошибок TikWM 11 августа 2026) —
    _tikwm_last_request_ts персистентен на весь процесс, без сброса между тестами
    накопленное состояние заставляло бы КАЖДЫЙ следующий тест, трогающий TikTok,
    реально ждать до _TIKWM_MIN_INTERVAL_SEC секунд без единой пользы для теста."""
    import lumen_tiktok as _tiktok_module
    original_sleep = _tiktok_module._sleep
    original_last_ts = _tiktok_module._tikwm_last_request_ts

    async def _instant_sleep(_seconds: float) -> None:
        return None

    _tiktok_module._sleep = _instant_sleep
    _tiktok_module._tikwm_last_request_ts = None
    yield
    _tiktok_module._sleep = original_sleep
    _tiktok_module._tikwm_last_request_ts = original_last_ts
