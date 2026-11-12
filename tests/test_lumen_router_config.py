"""
test_lumen_router_config.py — юнит-тесты на lumen_router_config.py: конфигурация моделей
(GEMINI_MODELS, GEMINI_TTS_MODELS), эвристики "это тяжёлый запрос?"/"нужна свежая
информация?"/"просят поиск в вебе" (_looks_like_heavy_query/_looks_like_freshness_query/
_looks_like_search_request), построение маршрута для одного сообщения (_build_route:
провайдеры munito и gemini, выключатель _munito_active, extras запроса _munito_payload_extras,
автовыбор усилия pick_reasoning_effort/agent_max_searches/gemini_thinking_for и карта
возможностей моделей _MUNITO_CAPS),
фильтры исчерпанной квоты и карантина моделей (_skip_exhausted/_is_quota_exhausted) и
реестр ID для детекта утечки личности (_KNOWN_MODEL_IDS_FOR_LEAK_DETECTION).

Снятые провайдеры (OpenRouter, Groq-чат, Gemma) из маршрута убраны 05.11.2026 вместе с
их реестром здоровья и order-списками — от них осталась только проверка, что их ID
не возвращаются в выборку и не исчезают из детекта утечек.

Часть разбиения test_bot_helpers.py по модулям — см. test_lumen_formatting.py про общий
принцип. lumen_router_config.py — чистые данные и функции принятия решения о маршруте без
единого обращения к Telegram/Gemini/Munito API, поэтому тестируется здесь напрямую
(import lumen_router_config), без импорта bot.py. Логгер внутри lumen_router_config.py
намеренно называется "bot" (см. комментарий в самом модуле) — поэтому caplog.at_level(...,
logger="bot") ниже продолжает работать так же, как и раньше, независимо от того, что этот
файл не импортирует bot.py вообще (состояние квоты и карантина тесты берут всё же из bot).

Запуск:
    pytest test_lumen_router_config.py -v
"""
import pytest

import lumen_router_config
from datetime import date



# ─────────────────────────── автоматический выбор модели (роутер) ───────────────────────────
# /model и /provider удалены целиком — тесты на _gemini_display_fields/_or_display_fields/
# _should_reveal_real_model_names/_check_public_model_names_configured удалены вместе с ними
# (см. README/историю изменений). Ниже — тесты на роутер, который их заменил.

def test_looks_like_heavy_query_detects_code_and_analysis_requests():
    assert lumen_router_config._looks_like_heavy_query("напиши функцию на питоне для сортировки списка") is True
    assert lumen_router_config._looks_like_heavy_query("```\nprint(1)\n```") is True
    assert lumen_router_config._looks_like_heavy_query("проанализируй этот текст подробно") is True
    assert lumen_router_config._looks_like_heavy_query("а" * 700) is True
    assert lumen_router_config._looks_like_heavy_query("1? 2? 3?") is True


def test_looks_like_heavy_query_false_for_simple_messages():
    assert lumen_router_config._looks_like_heavy_query("привет") is False
    assert lumen_router_config._looks_like_heavy_query("сколько будет 2+2") is False
    assert lumen_router_config._looks_like_heavy_query("") is False


def test_looks_like_heavy_query_detects_tests_bots_and_debug_requests():
    # Расширение эвристики (сентябрь 2026): юнит-тесты, боты/сайты, разбор кода
    # и поиск багов — тоже тяжёлые задачи, раньше уходили в лёгкую цепочку.
    assert lumen_router_config._looks_like_heavy_query("напиши тест для функции сортировки") is True
    assert lumen_router_config._looks_like_heavy_query("напиши бота для телеграмма") is True
    assert lumen_router_config._looks_like_heavy_query("разбери этот код построчно") is True
    assert lumen_router_config._looks_like_heavy_query("найди баг в скрипте") is True
    assert lumen_router_config._looks_like_heavy_query("объясни код выше") is True
    assert lumen_router_config._looks_like_heavy_query("напиши эссе про осень") is True
    assert lumen_router_config._looks_like_heavy_query("какая погода") is False
    assert lumen_router_config._looks_like_heavy_query("расскажи анекдот") is False


def test_looks_like_freshness_query_detects_current_info_needs():
    assert lumen_router_config._looks_like_freshness_query("кто сейчас президент Франции") is True
    assert lumen_router_config._looks_like_freshness_query("какая сегодня погода в Москве") is True
    assert lumen_router_config._looks_like_freshness_query("сколько стоит биткоин") is True
    assert lumen_router_config._looks_like_freshness_query("последние новости про ИИ") is True


def test_looks_like_freshness_query_false_for_timeless_questions():
    assert lumen_router_config._looks_like_freshness_query("столица Франции") is False
    assert lumen_router_config._looks_like_freshness_query("объясни теорию относительности") is False


def test_looks_like_freshness_query_detects_schedule_and_forecast():
    # Расширение эвристики (сентябрь 2026): расписание/афиша/прогноз без слова
    # "погода" раньше не считались нуждой в свежей информации.
    assert lumen_router_config._looks_like_freshness_query("расписание электричек") is True
    assert lumen_router_config._looks_like_freshness_query("дай прогноз на выходные") is True
    assert lumen_router_config._looks_like_freshness_query("афиша кино на неделю") is True
    assert lumen_router_config._looks_like_freshness_query("любимый цвет") is False


def test_looks_like_freshness_query_detects_buying_advice():
    # Прод-кейс 17.09.2026: "какой проц лучше всего брать под ртх 5060" ушёл
    # лёгким маршрутом в слабую модель — та ответила протухшими знаниями
    # (RTX 5060 "ещё не объявлена") вместо поиска. Советы по покупке всегда
    # про цены/наличие/новинки — им нужен живой поиск.
    assert lumen_router_config._looks_like_freshness_query("какой проц лучше всего брать под ртх 5060") is True
    assert lumen_router_config._looks_like_freshness_query("что лучше купить для игр") is True
    assert lumen_router_config._looks_like_freshness_query("какой телефон лучше взять") is True


def test_looks_like_freshness_query_detects_date_dependent_astronomy():
    # Eval 22.10.2026: «во сколько рассвет в Москве» не считался свежим — уезжал
    # обычным текстом в Gemma без поиска и отвечал по памяти (Q6 провал).
    assert lumen_router_config._looks_like_freshness_query("во сколько рассвет в Москве") is True
    assert lumen_router_config._looks_like_freshness_query("когда закат солнца") is True
    assert lumen_router_config._looks_like_freshness_query("восход солнца в Сочи") is True
    assert lumen_router_config._looks_like_freshness_query("фаза луны") is True
    assert lumen_router_config._looks_like_freshness_query("лунное затмение") is True
    assert lumen_router_config._looks_like_freshness_query("люблю ночное небо") is False


def test_looks_like_heavy_query_detects_bare_compare():
    # Прод-кейс 17.09.2026: "сравни конкурентов" без "и/с" между объектами
    # пролетал мимо тяжёлой ветки — сравнение ехало на слабую модель.
    assert lumen_router_config._looks_like_heavy_query("сравни конкурентов") is True
    assert lumen_router_config._looks_like_heavy_query("сравни iphone и samsung") is True


def test_looks_like_freshness_query_detects_english():
    # Внешний аудит: детект свежести был только русским при DEFAULT_LANG=en —
    # "latest news" уходило слабым моделям без поиска.
    assert lumen_router_config._looks_like_freshness_query("What is the latest news about NVIDIA?") is True
    assert lumen_router_config._looks_like_freshness_query("weather in Berlin today") is True
    assert lumen_router_config._looks_like_freshness_query("how much does it cost") is True
    assert lumen_router_config._looks_like_freshness_query("just chatting about life") is False


def test_looks_like_freshness_query_ignores_substrings():
    # Ревью ветки: голый now ловил know/snow — высокочастотные ложняки на обычной болтовне.
    assert lumen_router_config._looks_like_freshness_query("I know kung fu") is False
    assert lumen_router_config._looks_like_freshness_query("snow here") is False
    assert lumen_router_config._looks_like_freshness_query("groundbreaking discovery") is False


def test_looks_like_freshness_query_detects_multilingual():
    # Аудит 28.10.2026: бот говорит на 25 языках, а детект свежести был RU/EN —
    # остальные языки уезжали обычным текстом без даты и внешнего поиска.
    assert lumen_router_config._looks_like_freshness_query("¿Cuánto cuesta el iPhone hoy?") is True
    assert lumen_router_config._looks_like_freshness_query("Wie ist das Wetter heute?") is True
    assert lumen_router_config._looks_like_freshness_query("Quelles sont les actualités aujourd'hui ?") is True
    assert lumen_router_config._looks_like_freshness_query("Qual è il prezzo oggi?") is True
    assert lumen_router_config._looks_like_freshness_query("Qual é a previsão para amanhã?") is True
    assert lumen_router_config._looks_like_freshness_query("Wat is het nieuws vandaag?") is True
    assert lumen_router_config._looks_like_freshness_query("Jaka jest pogoda dziś?") is True
    assert lumen_router_config._looks_like_freshness_query("Bugün hava nasıl?") is True
    assert lumen_router_config._looks_like_freshness_query("ما هو سعر الدولار اليوم؟") is True
    assert lumen_router_config._looks_like_freshness_query("आज समाचार क्या हैं?") is True
    assert lumen_router_config._looks_like_freshness_query("Berapa harga emas hari ini?") is True
    assert lumen_router_config._looks_like_freshness_query("Giá vàng hôm nay bao nhiêu?") is True
    assert lumen_router_config._looks_like_freshness_query("วันนี้ราคาทองเท่าไหร่") is True
    assert lumen_router_config._looks_like_freshness_query("قیمت دلار امروز چقدر است؟") is True
    assert lumen_router_config._looks_like_freshness_query("آج موسم کیسا ہے؟") is True
    assert lumen_router_config._looks_like_freshness_query("Bugun ob-havo qanday?") is True
    assert lumen_router_config._looks_like_freshness_query("আজ আবহাওয়া কেমন?") is True
    assert lumen_router_config._looks_like_freshness_query("Ano ang balita ngayon?") is True
    assert lumen_router_config._looks_like_freshness_query("Yini izindaba namuhla?") is True
    assert lumen_router_config._looks_like_freshness_query("Які навіны сёння?") is True
    assert lumen_router_config._looks_like_freshness_query("Яка погода сьогодні?") is True
    assert lumen_router_config._looks_like_freshness_query("Бүгін ауа райы қандай?") is True


def test_looks_like_freshness_query_multilingual_ignores_common_words():
    # Тот же аудит: слова «время/снова/конец/утро» есть почти в каждой бытовой
    # фразе — их в детекте нет, иначе поиск включался бы на болтовне.
    assert lumen_router_config._looks_like_freshness_query("me gusta el tiempo libre") is False
    assert lumen_router_config._looks_like_freshness_query("no meu tempo livre") is False
    assert lumen_router_config._looks_like_freshness_query("Ik kom weer terug") is False
    assert lumen_router_config._looks_like_freshness_query("son derece güzel") is False
    assert lumen_router_config._looks_like_freshness_query("Sa panahong ito") is False
    assert lumen_router_config._looks_like_freshness_query("Guten Morgen") is False
    # Ревью 28.10.2026: без границ «завтра» ловил «завтрак», а «हाल/कल»
    # срабатывали внутри हालांकि/कल्पना (матры не входят в \w).
    assert lumen_router_config._looks_like_freshness_query("что приготовить на завтрак") is False
    assert lumen_router_config._looks_like_freshness_query("позавтракать вместе") is False
    assert lumen_router_config._looks_like_freshness_query("the actual reason is simple") is False
    assert lumen_router_config._looks_like_freshness_query("हालांकि यह ठीक है") is False
    assert lumen_router_config._looks_like_freshness_query("कल्पना कीजिए") is False
    assert lumen_router_config._looks_like_freshness_query("আজাদি") is False
    assert lumen_router_config._looks_like_freshness_query("что будет завтра") is True


def test_looks_like_search_request_detects_explicit_tool_asks():
    # Прод 11.10.2026: "найди с веб поиском" без слов свежести уходило на модели
    # без поиска — явная просьба искать теперь тоже ведёт на свежий маршрут.
    assert lumen_router_config._looks_like_search_request("погугли что с ДЦ Яндекса") is True
    assert lumen_router_config._looks_like_search_request("найди инфу с помощью веб-поиска") is True
    assert lumen_router_config._looks_like_search_request("ищи через поиск, что там с дата-центрами") is True
    assert lumen_router_config._looks_like_search_request("search the web for Yandex data centers") is True
    assert lumen_router_config._looks_like_search_request("поищи в интернете свежие новости") is True


def test_looks_like_search_request_ignores_code_searches():
    # Голые "найди/ищи" без упоминания поиска — про код, а не про веб: иначе
    # "найди баг" гнал бы тяжёлый разбор кода на поисковый маршрут.
    assert lumen_router_config._looks_like_search_request("найди баг в скрипте") is False
    assert lumen_router_config._looks_like_search_request("найди ошибку") is False
    assert lumen_router_config._looks_like_search_request("привет") is False
    assert lumen_router_config._looks_like_search_request("") is False


def test_looks_like_heavy_query_detects_english():
    # Внешний аудит: "write a Python parser" пролетал в light-ветку.
    assert lumen_router_config._looks_like_heavy_query("write a Python parser for csv") is True
    assert lumen_router_config._looks_like_heavy_query("fix this bug, it crashes on empty input") is True
    assert lumen_router_config._looks_like_heavy_query("compare iphone and samsung") is True
    assert lumen_router_config._looks_like_heavy_query("just saying hello") is False


def test_looks_like_heavy_query_ignores_substrings_and_handles_plurals():
    # Ревью ветки: prove ловил improve, solve — resolve; "write two scripts" пролетал мимо.
    assert lumen_router_config._looks_like_heavy_query("please improve my text") is False
    assert lumen_router_config._looks_like_heavy_query("lets resolve this conflict") is False
    assert lumen_router_config._looks_like_heavy_query("write two scripts for backup") is True


# ─────────── состав маршрута: Munito головой, цепочки Gemini резервом ───────────
# OpenRouter, Groq-чат и Gemma из маршрута убраны 05.11.2026: текст и картинки
# обслуживает Munito, Gemini — автоматический резерв; ссылки, YouTube и видео/аудио
# остаются на Gemini, потому что Munito их физически не принимает.

def _munito_mode(monkeypatch, *, disabled=False, key="fake-munito-key-123"):
    """Выключатель — данные роутера (считываются при импорте), ключ — bot.py: патчить надо оба места."""
    import bot
    monkeypatch.setattr(lumen_router_config, "_MUNITO_DISABLED", disabled)
    monkeypatch.setattr(bot, "MUNITO_API_KEY", key)


def _route_for(**kwargs):
    return lumen_router_config._build_route(
        needs_youtube=kwargs.get("needs_youtube", False),
        needs_website=kwargs.get("needs_website", False),
        media_mime=kwargs.get("media_mime"),
        is_heavy=kwargs.get("is_heavy", False),
        needs_freshness=kwargs.get("needs_freshness", False),
    )


@pytest.mark.parametrize("kwargs, reserve", [
    ({"is_heavy": False, "needs_freshness": False}, lumen_router_config.GEMINI_HEAVY_CHAIN),
    ({"is_heavy": True, "needs_freshness": False}, lumen_router_config.GEMINI_HEAVY_CHAIN),
    ({"is_heavy": False, "needs_freshness": True}, lumen_router_config.GEMINI_SEARCH_CHAIN),
    ({"is_heavy": True, "needs_freshness": True}, lumen_router_config.GEMINI_SEARCH_CHAIN),
])
def test_build_route_text_goes_to_munito_with_gemini_reserve(monkeypatch, kwargs, reserve):
    # Резерв теперь безусловный (MUNITO_KEEP_FALLBACKS удалён): бесплатный тариф
    # Munito отвечает 503 "no free capacity", и без цепочки Gemini это молчание бота.
    # Обычный и тяжёлый текст резервируются одной тяжёлой цепочкой — лёгкой не было.
    _munito_mode(monkeypatch)
    route = _route_for(**kwargs)
    n = len(lumen_router_config._MUNITO_ORDER)
    assert route[:n] == [("munito", m) for m in lumen_router_config._MUNITO_ORDER]
    assert route[n:] == [("gemini", m) for m in reserve]
    assert route[0] == ("munito", "deepseek-ai/deepseek-v4.1-flash-1017")


@pytest.mark.parametrize("needs_freshness", [False, True])
def test_build_route_image_goes_to_munito_unless_it_needs_search(monkeypatch, needs_freshness):
    # Vision голов Munito проверен 05.11.2026 живым вызовом, а живого поиска у него
    # нет — "покажи картинку и найди, что это за новость" целиком уходит в Gemini.
    _munito_mode(monkeypatch)
    route = _route_for(media_mime="image/png", needs_freshness=needs_freshness)
    n = len(lumen_router_config._MUNITO_ORDER)
    if needs_freshness:
        assert route == [("gemini", m) for m in lumen_router_config.GEMINI_SEARCH_CHAIN]
    else:
        assert route[:n] == [("munito", m) for m in lumen_router_config._MUNITO_ORDER]
        assert route[n:] == [("gemini", m) for m in lumen_router_config.GEMINI_HEAVY_CHAIN]


@pytest.mark.parametrize("kwargs, chain", [
    ({"media_mime": "video/mp4"}, lumen_router_config.GEMINI_HEAVY_CHAIN),
    ({"media_mime": "audio/ogg"}, lumen_router_config.GEMINI_HEAVY_CHAIN),
    ({"media_mime": "video/mp4", "needs_freshness": True}, lumen_router_config.GEMINI_SEARCH_CHAIN),
    ({"needs_website": True}, lumen_router_config.GEMINI_LINK_CHAIN),
    ({"needs_youtube": True}, lumen_router_config.GEMINI_LINK_CHAIN),
    ({"needs_website": True, "needs_freshness": True}, lumen_router_config.GEMINI_LINK_SEARCH_CHAIN),
    ({"needs_youtube": True, "needs_freshness": True}, lumen_router_config.GEMINI_LINK_SEARCH_CHAIN),
])
def test_build_route_gemini_only_shapes(monkeypatch, kwargs, chain):
    # Munito принимает только текст и картинки: эскалировать ссылки и видео некуда,
    # поэтому они Gemini-only даже при активном Munito.
    _munito_mode(monkeypatch)
    route = _route_for(**kwargs)
    assert route == [("gemini", m) for m in chain]
    assert all(p == "gemini" for p, _ in route)


@pytest.mark.parametrize("disabled, key", [
    (True, "fake-munito-key-123"),
    (False, ""),
])
def test_build_route_without_active_munito_is_gemini_only(monkeypatch, disabled, key):
    # Два независимых способа выключить провайдер: флаг и удаление ключа из Space.
    # Маршрут обязан целиком вернуться на Gemini, а не остаться без Munito-головы.
    _munito_mode(monkeypatch, disabled=disabled, key=key)
    route = _route_for()
    assert route and all(p == "gemini" for p, _ in route)
    assert route[0] == ("gemini", "gemini-2.5-flash")
    assert all(p == "gemini" for p, _ in _route_for(media_mime="image/jpeg"))


@pytest.mark.parametrize("disabled, key, expected", [
    (False, "fake-munito-key-123", True),
    (True, "fake-munito-key-123", False),
    (False, "", False),
    (True, "", False),
])
def test_munito_active_requires_key_and_enabled_flag(monkeypatch, disabled, key, expected):
    # Выключатель, а не включатель: если _munito_active перестанет смотреть на флаг,
    # MUNITO_ENABLED=0 не вернёт трафик с Munito (503 на каждое сообщение), а если
    # перестанет смотреть на ключ — маршрут уйдёт в провайдер, которого нет в Space.
    _munito_mode(monkeypatch, disabled=disabled, key=key)
    assert lumen_router_config._munito_active() is expected
    assert bool(lumen_router_config._munito_route()) is expected


@pytest.mark.parametrize("kwargs", [
    {},
    {"is_heavy": True},
    {"needs_freshness": True},
    {"media_mime": "image/jpeg"},
    {"media_mime": "video/mp4"},
    {"needs_website": True},
    {"needs_youtube": True},
])
def test_build_route_never_returns_retired_providers(monkeypatch, kwargs):
    # Ключей OpenRouter и Groq-чат в проде больше нет, Gemma вычеркнута из
    # GEMINI_MODELS: вернувшийся в цепочку ID дал бы 401/404 в самом конце маршрута,
    # когда живые попытки уже потрачены.
    _munito_mode(monkeypatch)
    route = _route_for(**kwargs)
    assert route
    assert {p for p, _ in route} <= {"munito", "gemini"}
    live = set(lumen_router_config.GEMINI_MODELS) | set(lumen_router_config._MUNITO_ORDER)
    for _, model_id in route:
        assert model_id in live, f"{model_id} в цепочке, но не в конфиге моделей"
        assert not model_id.endswith(":free")


@pytest.mark.parametrize(("value", "expected"), [
    (None, {"reasoning_effort": "low"}),
    ("", {}),
    ("minimal", {"reasoning_effort": "minimal"}),
])
def test_munito_payload_extras_default_low_and_absent_when_unset(monkeypatch, value, expected):
    # protected поведение: по умолчанию запросы уходят с минимальным размышлением
    # (замер 05.11.2026: ~0.2s вместо ~1s) и поле НЕ отправляется, когда переменная
    # пустая — Munito отвечает 400 на незнакомый параметр, так что глушить эксперимент
    # должно можно без правки кода.
    if value is None:
        monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("MUNITO_REASONING_EFFORT", value)
    assert lumen_router_config._munito_payload_extras() == expected


# ─────────────────────────── новые модели Gemini (3.6 Flash / 3.5 Flash-Lite) ───────────────────────────

def test_new_gemini_models_present_and_prioritized():
    # Аудит 17.08.2026: 3.7 Flash (GA 13.08) сменил 3.6 в роли флагмана.
    assert "gemini-3.7-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.6-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.5-flash-lite" in lumen_router_config.GEMINI_MODELS
    # 08.10.2026: линейка 3.x в цепочках (жива, лимиты есть), но дефолт и голова —
    # на 2.5-flash: единственная, кто отвечал в проде 05–07.10.2026, и единственная
    # с квотой search grounding.
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-2.5-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-2.5-flash"
    # Обновлено (24.07.2026) вместе с реордером GEMINI_SEARCH_CHAIN — см. комментарий
    # там же: реальная квота на search grounding подтверждена только у Gemini 2.5.
    assert lumen_router_config.GEMINI_SEARCH_CHAIN[0] == "gemini-2.5-flash"


def test_gemini_3_flash_preview_restored_after_official_docs_confirmation():
    # ИСТОРИЯ: удалена 17.08.2026 (ошибочный вывод — источники были Vertex AI
    # Enterprise Agent Platform и сторонний GitHub Copilot changelog, ни один не
    # офиц. страница модели самого Gemini API). ВОССТАНОВЛЕНА 22.08.2026: владелец
    # прислал скриншот ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview —
    # "Last updated 2026-08-18 UTC", живая таблица возможностей, Model code
    # gemini-3-flash-preview. Первоисточник перевешивает прежний (неверный) вывод.
    # 08.10.2026: вся линейка 3.x снова убрана из цепочек — на этот раз прод
    # 05.10.2026 (HTTP 503 разом на пяти моделях). Конфиг в GEMINI_MODELS оставлен:
    # возврат — одной правкой цепочек, если дашборд покажет их живыми.
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_HEAVY_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_SEARCH_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_LINK_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_LINK_SEARCH_CHAIN
    # 08.10.2026 (пересмотр после скриншота дашборда): модель жива, у неё есть
    # лимит 5 RPM / 250K TPM / 20 RPD, и официальный список моделей Google
    # (обновлён 06.10.2026) подтверждает код gemini-3-flash-preview. Продовые 503
    # были временным отказом, поэтому откат снятия — вернуть в цепочки, а отход
    # обеспечивает остывка по "unavailable" (_mark_model_unavailable).
    chain = lumen_router_config.GEMINI_HEAVY_CHAIN
    assert chain.index("gemini-3.5-flash") < chain.index("gemini-3-flash-preview")
    # 05.11.2026: Gemma вычеркнута из GEMINI_MODELS, поэтому хвост цепочки теперь
    # не на ней, а на последней живой 3.x-модели.
    assert chain[-1] == "gemini-3-flash-preview"


def test_gemini_3_flash_preview_quota_config_matches_dashboard():
    # Страница модели говорит "Search grounding: Supported"/"Grounding with Google
    # Maps: Supported" — модель технически умеет. Но реальная бесплатная КВОТА на
    # оба (что и кодируют эти два флага) по дашборду AI Studio — 0/0, тот же паттерн,
    # что и у всей остальной линейки 3.x (см. заметку про бакеты в начале GEMINI_MODELS).
    conf = lumen_router_config.GEMINI_MODELS["gemini-3-flash-preview"]
    assert conf.get("search_grounding") is False
    assert conf.get("map_grounding") is False
    assert conf.get("url_context") is True
    assert conf.get("stream") is True


def test_gemini_3_7_flash_config_matches_3_6_flash_pattern():
    # Gemini 3.7 Flash делит тот же бакет квоты search/map grounding ("Gemini 3"),
    # что и вся линейка 3.x — подтверждено по дашборду AI Studio 17 августа 2026
    # (0/0 на оба инструмента, тот же паттерн, что уже подтверждён для 3.6 Flash).
    conf = lumen_router_config.GEMINI_MODELS["gemini-3.7-flash"]
    assert conf.get("search_grounding") is False
    assert conf.get("map_grounding") is False
    assert conf.get("url_context") is True
    assert conf.get("stream") is True
    # Числа с дашборда были доступны сразу при добавлении (та же цифра, что и у
    # уже подтверждённой 3.6 Flash) — не должна попадать под "quota_unconfirmed".
    assert not conf.get("quota_unconfirmed")


def test_check_unconfirmed_model_quotas_no_warnings_once_all_models_confirmed(caplog):
    # РЕГРЕССИЯ (24.07.2026): gemini-3.6-flash и gemini-3.5-flash-lite были
    # подтверждены по реальному дашборду AI Studio (см. комментарии в GEMINI_MODELS
    # в bot.py), флаг quota_unconfirmed снят у обеих. Раньше этот тест проверял, что
    # именно эти две модели ЕЩЁ вызывают предупреждение (see git history) — теперь,
    # когда обе подтверждены, предупреждений быть не должно вообще ни у одной модели.
    # Если этот тест начнёт падать — значит либо quota_unconfirmed вернули по ошибке,
    # либо добавили новую неподтверждённую модель (тогда тест нужно обновить под
    # новую модель, а не просто "починить").
    import logging
    with caplog.at_level(logging.WARNING, logger="bot"):
        lumen_router_config._check_unconfirmed_model_quotas()
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == []


# ─────────── снятые ID остаются в детекте утечки личности ───────────
# Реестр здоровья OpenRouter (_OR_MODEL_HEALTH), его order-списки и Gemma-цепочка
# удалены из роутера 05.11.2026 вместе с самими провайдерами. Из всей этой истории
# остаётся одно требование: дословный ID снятой модели по-прежнему нельзя видеть в
# ответе — модель может назвать себя прежним слагом, и детект ловит его только если
# ID лежит в _KNOWN_MODEL_IDS_FOR_LEAK_DETECTION. Даты снятия каждого — в git-истории
# (404/403, платная отсечка, порча текста на калибровке).

_RETIRED_MODEL_IDS = (
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "qwen/qwen3-coder:free",
    "tencent/hy3:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "inclusionai/ling-3.0-flash:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "z-ai/glm-5.2:free",
    "dots-studio/dots-3-note-preview:free",
    "qwen/qwen3.8-27b:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
)


@pytest.mark.parametrize("model_id", _RETIRED_MODEL_IDS)
def test_retired_model_ids_stay_in_leak_detection_registry(model_id):
    # Падает, если кто-то "почистил" реестр под актуальный каталог провайдера:
    # снятая модель перестаёт выбираться, но её ID всё ещё может прийти в тексте
    # ответа, и без строки в реестре скраб её не увидит.
    assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert model_id not in lumen_router_config._MUNITO_ORDER


def test_live_munito_model_ids_are_known_to_leak_detection():
    # Munito отвечает вместо OpenRouter с ноября 2026: без его ID в реестре бот
    # назвал бы хоста и модель в первом же ответе про своё устройство.
    for model_id in lumen_router_config._MUNITO_ORDER:
        assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


# ─────────────────── TEXT_MODEL_ORDER переименован, алиас сохранён ───────────────────

def test_text_model_order_alias_still_works():
    assert lumen_router_config.TEXT_MODEL_ORDER is lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert "nvidia/nemotron-3-super-120b-a12b:free" in lumen_router_config.TEXT_MODEL_ORDER


# ─────────────────── GEMINI_TTS_MODELS централизованы ───────────────────

def test_gemini_tts_models_centralized_in_router_config():
    assert lumen_router_config.GEMINI_TTS_MODELS == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]


# ─────────────────── линейка Gemini 3.x в цепочках ───────────────────
# Снятые слаги OpenRouter из аудита 17.09.2026 переехали в _RETIRED_MODEL_IDS выше:
# роутер их больше не выбирает вовсе, поэтому здесь только позиции моделей Gemini.


def test_gemini_3_8_flash_heads_heavy_chain_and_default():
    # 08.10.2026 (пересмотр после скриншота дашборда AI Studio): линейка 3.x жива,
    # лимиты у неё есть (5 RPM / 250K TPM / 20 RPD), официальный список модерий
    # Google (обновлён 06.10.2026) подтверждает коды. Продовые 503 05.10.2026 — временный
    # отказ, поэтому 3.8 остаётся в цепочках вторым резервом после 2.5-flash
    # (единственной, кто реально отвечал в проде и у кого есть квота поиска).
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_MODELS
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-2.5-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-2.5-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[1] == "gemini-3.8-flash"
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_SEARCH_CHAIN
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_LINK_CHAIN
    for alive in ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
        assert alive in lumen_router_config.GEMINI_HEAVY_CHAIN
        assert alive in lumen_router_config.GEMINI_SEARCH_CHAIN
    conf = lumen_router_config.GEMINI_MODELS["gemini-3.8-flash"]
    assert conf.get("search_grounding") is False
    assert conf.get("map_grounding") is False
    assert conf.get("url_context") is True
    assert not conf.get("quota_unconfirmed")


def test_registry_and_default_chain_have_no_silent_aliases():
    # Аудит 26.09.2026: GEMINI_DEFAULT_CHAIN был АЛИАСОМ GEMINI_HEAVY_CHAIN (тот же
    # объект списка), а "openrouter/free" дублировался в реестре.
    assert lumen_router_config.GEMINI_DEFAULT_CHAIN is not lumen_router_config.GEMINI_HEAVY_CHAIN
    assert lumen_router_config.GEMINI_DEFAULT_CHAIN == lumen_router_config.GEMINI_HEAVY_CHAIN
    registry = lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert len(registry) == len(set(registry)), "в реестре моделей есть дубли"


# ─────────────── исчерпавшая квота больше не тратит попытку (аудит 26.09.2026) ───────────────

def test_munito_route_skips_models_marked_exhausted(monkeypatch):
    # Метку 429 вешают на провайдера "munito"; без фильтра каждое сообщение
    # платило бы попыткой модели, у которой квота кончилась ещё вчера.
    import bot
    _munito_mode(monkeypatch)
    monkeypatch.setattr(lumen_router_config, "_MUNITO_ORDER", ["probe/exhausted", "probe/alive"])
    bot.GLOBAL_QUOTA.setdefault("munito", {})["probe/exhausted"] = {"used": 0, "exhausted_at": 123.0}
    assert [m for _, m in lumen_router_config._munito_route()] == ["probe/alive"]


def test_gemini_route_skips_exhausted_and_cooling_models():
    # Короткая остывка минутного 429 (cooldown_until) считается наравне с суточной
    # меткой: иначе маршрут тратит попытку на модель, которая откажет снова.
    import bot
    bot.GLOBAL_QUOTA.setdefault("gemini", {})["gemini-probe-dead"] = {"used": 0, "exhausted_at": 123.0}
    bot.GLOBAL_QUOTA.setdefault("gemini", {})["gemini-probe-cooling"] = {"used": 0, "cooldown_until": float("inf")}
    route = lumen_router_config._gemini_route(["gemini-probe-dead", "gemini-probe-cooling", "gemini-3.8-flash"])
    assert [m for _, m in route] == ["gemini-3.8-flash"]


def test_route_never_becomes_empty_when_every_model_is_exhausted(monkeypatch):
    # Пустой маршрут = мгновенный отказ вместо внятного сообщения о лимите, поэтому
    # по одному кандидату на провайдера остаётся — это и потолок лишней платы.
    import bot
    _munito_mode(monkeypatch)
    for mid in lumen_router_config._MUNITO_ORDER:
        bot.GLOBAL_QUOTA.setdefault("munito", {})[mid] = {"used": 0, "exhausted_at": 123.0}
    for mid in lumen_router_config.GEMINI_HEAVY_CHAIN:
        bot.GLOBAL_QUOTA.setdefault("gemini", {})[mid] = {"used": 0, "exhausted_at": 123.0}
    route = _route_for()
    assert route == [("munito", lumen_router_config._MUNITO_ORDER[0]),
                     ("gemini", lumen_router_config.GEMINI_HEAVY_CHAIN[0])]


def test_skip_exhausted_returns_single_first_allowed_variant():
    # Все мёртвы — один первый вариант, а не вся цепочка: иначе каждое сообщение
    # платило HTTP-вызовом за каждую модель цепочки при исчерпанной квоте.
    import bot
    dead = ["probe/dead1", "probe/dead2", "probe/dead3"]
    for mid in dead:
        bot.GLOBAL_QUOTA.setdefault("munito", {})[mid] = {"used": 0, "exhausted_at": 123.0}
    assert lumen_router_config._skip_exhausted("munito", dead) == ["probe/dead1"]
    assert lumen_router_config._skip_exhausted("munito", []) == []


def test_successful_usage_clears_exhausted_mark(monkeypatch):
    # Метка снимается успешным ответом, иначе после сброса суток или починки
    # провайдера модель навсегда выпадала бы из роутинга.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    bot.GLOBAL_QUOTA.setdefault("munito", {})[head] = {"used": 3, "exhausted_at": 123.0}
    bot._record_quota_usage("munito", head)
    assert lumen_router_config._munito_route()[0] == ("munito", head)


def test_all_gemini_models_declare_url_context_explicitly():
    # Аудит A2-11: дефолт True в conf.get давал инструмент молча — теперь ключ
    # прописан явно у каждой модели (поведение то же, намерение видно).
    missing = [m for m, c in lumen_router_config.GEMINI_MODELS.items() if "url_context" not in c]
    assert missing == []


def test_freshness_year_window_tracks_current_year():
    # Аудит A2-13: зашитый 202[6-9] протухал в 2030 — окно строится от текущей даты.
    assert str(date.today().year) in lumen_router_config._FRESHNESS_QUERY_RE.pattern
    assert lumen_router_config._looks_like_freshness_query("вспомним 2039 год") is False


def test_quota_exhausted_logs_broken_state_loudly(monkeypatch, caplog):
    # Аудит A2-10: битое состояние квоты раньше давало молчаливый fail-open.
    import bot

    class _BrokenQuota(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(bot, "GLOBAL_QUOTA", _BrokenQuota())
    with caplog.at_level("WARNING", logger="bot"):
        assert lumen_router_config._is_quota_exhausted("munito", "m") is False
    assert "Quota state unreadable" in caplog.text


def test_quarantine_triggers_after_three_consecutive_bad_and_skips_model(caplog, monkeypatch):
    # Три плохих подряд — карантин до конца суток: роутер объезжает модель.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    with caplog.at_level("WARNING", logger="bot"):
        for _ in range(3):
            bot._record_model_outcome("munito", head, bad=True)
    assert bot._is_quarantined("munito", head) is True
    assert "[quarantine]" in caplog.text
    assert bot._quarantine_status() == [("munito", head, 3)]
    assert [m for _, m in lumen_router_config._munito_route()] == lumen_router_config._MUNITO_ORDER[1:]


def test_quarantine_needs_bad_answers_from_more_than_one_chat(monkeypatch):
    # Аудит run-2: счётчик карантина не знал про чат, и три пустых ответа из одного
    # чата выключали головную модель для всех остальных до конца квотных суток.
    import bot
    _munito_mode(monkeypatch)
    head = lumen_router_config._MUNITO_ORDER[0]
    lumen_router_config._QUARANTINE.clear()
    tokens = []
    try:
        for _ in range(4):
            tokens.append(bot._LOG_CHAT.set("chat=1"))
            bot._record_model_outcome("munito", head, bad=True)
        assert bot._is_quarantined("munito", head) is False
        for chat in ("chat=2", "chat=3"):
            tokens.append(bot._LOG_CHAT.set(chat))
            bot._record_model_outcome("munito", head, bad=True)
        assert bot._is_quarantined("munito", head) is True
    finally:
        for token in reversed(tokens):
            bot._LOG_CHAT.reset(token)
        lumen_router_config._QUARANTINE.clear()


def test_quarantine_never_empties_route_and_success_resets(caplog):
    # Последняя доступная модель не карантинится де-факто: пустой маршрут не возвращаем.
    import bot
    for _ in range(5):
        bot._record_model_outcome("munito", "only-model", bad=True)
    assert bot._is_quarantined("munito", "only-model") is True
    assert lumen_router_config._skip_exhausted("munito", ["only-model"]) == ["only-model"]
    bot._record_model_outcome("munito", "only-model", bad=False)
    assert bot._is_quarantined("munito", "only-model") is False
    assert bot._quarantine_status() == []


def test_quarantine_resets_on_quota_day_change(monkeypatch):
    # Карантин только до конца суток квоты: новый день — чистый счётчик.
    import bot
    monkeypatch.setattr(bot, "_current_quota_day", lambda: "2099-01-01")
    for _ in range(3):
        bot._record_model_outcome("munito", "day-model", bad=True)
    assert bot._is_quarantined("munito", "day-model") is True
    monkeypatch.setattr(bot, "_current_quota_day", lambda: "2099-01-02")
    assert bot._is_quarantined("munito", "day-model") is False
    assert bot._quarantine_status() == []


def test_quarantine_threshold_reads_env(monkeypatch):
    # N из env: при 2 карантин наступает раньше (дефолт 3 проверен выше).
    import bot
    monkeypatch.setenv("MODEL_QUARANTINE_BAD_LIMIT", "2")
    assert lumen_router_config._quarantine_threshold() == 2
    bot._record_model_outcome("munito", "env-model", bad=True)
    assert bot._is_quarantined("munito", "env-model") is False
    bot._record_model_outcome("munito", "env-model", bad=True)
    assert bot._is_quarantined("munito", "env-model") is True


def test_quarantine_window_expires_and_stats_agree_with_routing(monkeypatch):
    # Окно вместо суток: после истечения модель снова в маршруте, и /stats не
    # показывает её карантинной (ревью 08.11.2026 нашло расхождение status и is_quarantined).
    import time
    import bot
    lumen_router_config._QUARANTINE.clear()
    monkeypatch.setattr(lumen_router_config, "QUARANTINE_WINDOW_SEC", 0.5)
    try:
        for i in (1, 2, 3):
            token = bot._LOG_CHAT.set(f"chat={i}")
            try:
                bot._record_model_outcome("munito", "window-model", bad=True)
            finally:
                bot._LOG_CHAT.reset(token)
        assert bot._is_quarantined("munito", "window-model") is True
        assert bot._quarantine_status() == [("munito", "window-model", 3)]
        time.sleep(0.6)
        assert bot._is_quarantined("munito", "window-model") is False
        assert bot._quarantine_status() == []
    finally:
        lumen_router_config._QUARANTINE.clear()


# ─────────────────────────── автовыбор усилия и возможностей моделей ───────────────────────────

@pytest.mark.parametrize(("text", "kwargs", "expected"), [
    # Регрессия 12.11.2026: плоский MUNITO_REASONING_EFFORT=low давал glm-5.3-flash
    # неверную арифметику (64 вместо 60), а на «подумай как следует» усилие не
    # менялось вовсе. Здесь проверяется именно переключение по смыслу запроса.
    ("привет", {}, "low"),
    ("спасибо!", {}, "low"),
    ("сколько будет 27*43", {}, "medium"),
    ("кто такие викинги", {}, "medium"),
    ("напиши функцию парсера", {"is_heavy": True}, "high"),
    ("спроектируй архитектуру и докажи выбор", {}, "xhigh"),
    ("сравни все три варианта по шагам", {}, "xhigh"),
    ("какая сейчас цена билета", {"needs_freshness": True}, "medium"),
    # Просьба коротко не имеет права опускаться до minimal: на нём glm ошибался.
    ("коротко: сколько будет 27*43", {}, "low"),
    ("реши задачу ещё раз, ты не прав", {}, "medium"),
    ("реши задачу", {"corrections": 2}, "medium"),
    ("объясни", {"history_len": 60}, "medium"),
])
def test_pick_reasoning_effort_switches_on_request_shape(text, kwargs, expected):
    assert lumen_router_config.pick_reasoning_effort(text, **kwargs) == expected


def test_pick_reasoning_effort_never_returns_unknown_level():
    # Неизвестный уровень = 400 от провайдера или молчаливое отсутствие поля.
    for text in ("", "?", "докажи", "а он точно не в клубе?", "x" * 5000):
        assert lumen_router_config.pick_reasoning_effort(text) in lumen_router_config.EFFORT_LEVELS


def test_bump_effort_clamps_at_both_ends():
    assert lumen_router_config._bump_effort("xhigh", 3) == "xhigh"
    assert lumen_router_config._bump_effort("low", -3) == "minimal"
    assert lumen_router_config._bump_effort("мусор", 1) == "low"


@pytest.mark.parametrize(("model_id", "param", "expected"), [
    # 400 на reasoning_effort у gemma-4-12b и pplx-decider-v1.2 — замер 12.11.2026;
    # тест падает, если кто-то поставит такую модель в цепочку без замера.
    ("deepseek-ai/deepseek-v4.1-flash-1017", "reasoning_effort", True),
    ("zai-org/glm-5.3-flash", "tools", True),
    ("google/gemma-4-12b", "reasoning_effort", False),
    ("perplexity-ai/pplx-decider-v1.2", "reasoning_effort", False),
    # tools у этих двух не замеряли, поэтому он у них не снят: снято только то,
    # по чему есть подтверждённый 400.
    ("google/gemma-4-12b", "tools", True),
    ("неизвестная/модель", "reasoning_effort", True),
    (None, "reasoning_effort", True),
])
def test_munito_model_supports_only_measured_capabilities(model_id, param, expected):
    # Неизвестной модели поле остаётся (прежнее поведение): иначе новая голова
    # цепочки молча теряет усилие. Снято только то, что замерено как 400.
    assert lumen_router_config._munito_model_supports(model_id, param) is expected


def test_every_munito_chain_model_has_capability_record():
    # Модель без записи = усилие и tools для неё молча выключаются: либо замерь и
    # добавь, либо не ставь в цепочку (правило датированных подтверждений).
    for model_id in lumen_router_config._MUNITO_ORDER:
        assert model_id in lumen_router_config._MUNITO_CAPS, model_id
        assert lumen_router_config._MUNITO_CAPS[model_id]["reasoning_effort"] is True
        assert lumen_router_config._MUNITO_CAPS[model_id]["tools"] is True


@pytest.mark.parametrize(("value", "effort", "expected"), [
    (None, None, {"reasoning_effort": "low"}),      # прежний дефолт без аргументов
    (None, "high", {"reasoning_effort": "high"}),   # автовыбор, переменная не задана
    ("", "high", {}),                               # пустая = выключатель поля
    ("minimal", "high", {"reasoning_effort": "minimal"}),  # пин важнее автовыбора
])
def test_munito_payload_extras_precedence(monkeypatch, value, effort, expected):
    if value is None:
        monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("MUNITO_REASONING_EFFORT", value)
    assert lumen_router_config._munito_payload_extras(effort) == expected


def test_munito_payload_extras_drops_field_for_unsupported_model(monkeypatch):
    monkeypatch.delenv("MUNITO_REASONING_EFFORT", raising=False)
    assert lumen_router_config._munito_payload_extras("high", "google/gemma-4-12b") == {}
    assert lumen_router_config._munito_payload_extras("high", "zai-org/glm-5.3-flash") == {"reasoning_effort": "high"}


@pytest.mark.parametrize(("effort", "expected"), [
    ("low", 0), ("medium", 1), ("high", 2), ("xhigh", 2), (None, 1),
])
def test_agent_max_searches_follows_effort(monkeypatch, effort, expected):
    monkeypatch.delenv("AGENT_MAX_SEARCH_STEPS", raising=False)
    assert lumen_router_config.agent_max_searches(effort) == expected


def test_agent_max_searches_env_override(monkeypatch):
    # Владелец обязан уметь глушить цикл поиска цифрой, без правки кода.
    monkeypatch.setenv("AGENT_MAX_SEARCH_STEPS", "0")
    assert lumen_router_config.agent_max_searches("xhigh") == 0
    monkeypatch.setenv("AGENT_MAX_SEARCH_STEPS", "3")
    assert lumen_router_config.agent_max_searches("medium") == 3
    monkeypatch.setenv("AGENT_MAX_SEARCH_STEPS", "не число")
    assert lumen_router_config.agent_max_searches("medium") == 1


@pytest.mark.parametrize(("model_id", "effort", "expected"), [
    ("gemini-3.8-flash", "low", ("thinking_level", "low")),
    ("gemini-3.8-flash", "xhigh", ("thinking_level", "high")),
    ("gemini-2.5-flash", "low", ("thinking_budget", 0)),
    ("gemini-2.5-flash", "high", ("", None)),   # дефолт модели: калибровка 18.08.2026
    ("gemini-3.8-flash", None, ("", None)),     # EFFORT_AUTO=0 — прежняя эвристика снаружи
    ("gemini-unknown", "high", ("", None)),
])
def test_gemini_thinking_for_maps_effort(model_id, effort, expected):
    assert lumen_router_config.gemini_thinking_for(model_id, effort) == expected


def test_recent_corrections_counts_only_short_user_turns():
    history = [
        {"role": "user", "content": "объясни про шляпы"},
        {"role": "assistant", "content": "…"},
        {"role": "user", "content": "ты не прав, перепроверь"},
        {"role": "user", "content": "в статье приводится фраза «ты не прав был по отношению к…» " * 6},
    ]
    assert lumen_router_config.recent_corrections(history) == 1
    assert lumen_router_config.recent_corrections([]) == 0
