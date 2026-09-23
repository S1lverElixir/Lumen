"""
test_lumen_router_config.py — юнит-тесты на lumen_router_config.py: конфигурация моделей
(GEMINI_MODELS, GEMINI_TTS_MODELS), единый реестр "нездоровых" моделей OpenRouter
(_OR_MODEL_HEALTH/_ROUTER_EXCLUDED_OR_MODELS), эвристики "это тяжёлый запрос?"/"нужна
свежая информация?" (_looks_like_heavy_query/_looks_like_freshness_query), построение
маршрута для одного сообщения (_build_route/_or_route), проверки истечения промо-доступа
(_check_temporary_free_models_expiry) и неподтверждённых квот
(_check_unconfirmed_model_quotas).

Часть разбиения test_bot_helpers.py по модулям — см. test_lumen_formatting.py про общий
принцип. lumen_router_config.py — чистые данные и функции принятия решения о маршруте без
единого обращения к Telegram/Gemini/OpenRouter API, поэтому тестируется здесь напрямую
(import lumen_router_config), без импорта bot.py. Логгер внутри lumen_router_config.py
намеренно называется "bot" (см. комментарий в самом модуле) — поэтому caplog.at_level(...,
logger="bot") ниже продолжает работать так же, как и раньше, независимо от того, что этот
файл не импортирует bot.py вообще.

Запуск:
    pytest test_lumen_router_config.py -v
"""
import lumen_router_config



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


def test_looks_like_heavy_query_detects_bare_compare():
    # Прод-кейс 17.09.2026: "сравни конкурентов" без "и/с" между объектами
    # пролетал мимо тяжёлой ветки — сравнение ехало на слабую модель.
    assert lumen_router_config._looks_like_heavy_query("сравни конкурентов") is True
    assert lumen_router_config._looks_like_heavy_query("сравни iphone и samsung") is True


def test_build_route_youtube_link_forces_gemini_only():
    route = lumen_router_config._build_route(needs_youtube=True, needs_website=False, media_mime=None, is_heavy=False, needs_freshness=False)
    assert all(p == "gemini" for p, _ in route)
    # ОБНОВЛЕНО (аудит моделей, 17.09.2026): флагман сменился с 3.7 на 3.8 Flash.
    assert route[0] == ("gemini", "gemini-3.8-flash")


def test_build_route_website_link_forces_gemini_only_and_excludes_gemma():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=True, media_mime=None, is_heavy=False, needs_freshness=False)
    assert all(p == "gemini" for p, _ in route)
    # Gemma (no_system=True) не умеет читать сайты по ссылке — не должна попадать в маршрут.
    assert "gemma-4-31b-it" not in [m for _, m in route]
    assert "gemma-4-26b-a4b-it" not in [m for _, m in route]


def test_build_route_freshness_query_prioritizes_search_capable_gemini_models():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime=None, is_heavy=False, needs_freshness=True)
    # Search grounding подтверждён только у 2.5 (дашборд 24.07.2026; у 3.x бакет 0/0) — первой идёт 2.5-flash.
    assert route[0] == ("gemini", "gemini-2.5-flash")
    assert route[0][0] == "gemini"
    # OpenRouter должен присутствовать как резерв на случай полного отказа Gemini.
    assert any(p == "openrouter" for p, _ in route)
    # Groq — последним резервом (ответ по знаниям, если легли оба).
    assert route[-1][0] == "groq"


def test_build_route_plain_text_prefers_groq_to_save_quotas():
    # Обычный текст — сначала Groq (1000/день против 50 у OpenRouter, калибровка 21.09.2026),
    # дальше OpenRouter, Gemini — резервом.
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime=None, is_heavy=False, needs_freshness=False)
    assert route[0] == ("groq", "qwen/qwen3.8-27b")
    assert any(p == "openrouter" for p, _ in route)
    assert any(p == "gemini" for p, _ in route)  # Gemini всё ещё есть как резерв


def test_build_route_heavy_plain_text_uses_strong_openrouter_models_first():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime=None, is_heavy=True, needs_freshness=False)
    assert route[0] == ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")


def test_build_route_image_without_freshness_prefers_openrouter_vision():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime="image/jpeg", is_heavy=False, needs_freshness=False)
    # ОБНОВЛЕНО (аудит моделей, 17.09.2026): nano-12b-v2-vl пропал из бесплатного
    # каталога — головой стала gemma-4-31b-it.
    assert route[0] == ("openrouter", "google/gemma-4-31b-it:free")


def test_build_route_video_attachment_forces_gemini_even_without_freshness():
    # Видео/аудио — OpenRouter физически не может принять такое вложение
    # (только base64-изображения), поэтому маршрут должен быть Gemini-only,
    # даже если поиск не нужен.
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime="video/mp4", is_heavy=False, needs_freshness=False)
    assert all(p == "gemini" for p, _ in route)


def test_build_route_image_with_freshness_forces_gemini_search_chain():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime="image/png", is_heavy=False, needs_freshness=True)
    assert route[0][0] == "gemini"
    # См. обновлённый GEMINI_SEARCH_CHAIN (24.07.2026) — реальная квота на search
    # grounding подтверждена только у Gemini 2.5, не у 3.x lite-моделей.
    assert route[0][1] == "gemini-2.5-flash"


def test_or_route_excludes_uncensored_and_dead_models():
    # cognitivecomputations/dolphin-mistral...:free (uncensored, раньше доступна
    # только владельцу через /provider), qwen/qwen3-coder:free (подтверждённо снята
    # провайдером) и tencent/hy3:free (временное промо истекло 21.07.2026) роутер
    # никогда не должен выбирать сам.
    route = lumen_router_config._or_route([
        "meta-llama/llama-3.3-70b-instruct:free",
        "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
        "qwen/qwen3-coder:free",
        "tencent/hy3:free",
    ])
    ids = [m for _, m in route]
    assert "cognitivecomputations/dolphin-mistral-24b-venice-edition:free" not in ids
    assert "qwen/qwen3-coder:free" not in ids
    assert "tencent/hy3:free" not in ids
    assert "meta-llama/llama-3.3-70b-instruct:free" in ids


# ─────────────────────────── новые модели Gemini (3.6 Flash / 3.5 Flash-Lite) ───────────────────────────

def test_gemma_models_both_have_no_search_flag():
    # РЕГРЕССИЯ (найдено при перепроверке конфига 24.07.2026): у gemma-4-31b-it
    # "no_search": True стоял с самого начала, а у gemma-4-26b-a4b-it — отсутствовал.
    # Без него _build_gemini_call_config по умолчанию (search_grounding/url_context
    # по умолчанию True при отсутствии ключа) пытался бы включить google_search И
    # url_context для модели, которая (как и любая Gemma) их не поддерживает —
    # реальный риск ошибки API на каждый вызов этой модели.
    assert lumen_router_config.GEMINI_MODELS["gemma-4-31b-it"].get("no_search") is True
    assert lumen_router_config.GEMINI_MODELS["gemma-4-26b-a4b-it"].get("no_search") is True


def test_new_gemini_models_present_and_prioritized():
    # Аудит 17.08.2026: 3.7 Flash (GA 13.08) сменил 3.6 в роли флагмана.
    assert "gemini-3.7-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.6-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.5-flash-lite" in lumen_router_config.GEMINI_MODELS
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-3.8-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[1] == "gemini-3.7-flash"
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
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_HEAVY_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_SEARCH_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_LINK_CHAIN
    assert "gemini-3-flash-preview" in lumen_router_config.GEMINI_LINK_SEARCH_CHAIN
    # Позиция — после 3.5 Flash по номеру версии (3, 3.1, 3.5, 3.6, 3.7), перед
    # lite-вариантами.
    chain = lumen_router_config.GEMINI_HEAVY_CHAIN
    assert chain.index("gemini-3.5-flash") < chain.index("gemini-3-flash-preview") < chain.index("gemini-3.5-flash-lite")


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
    assert not conf.get("no_system")


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


# ─────────────────── мёртвая модель qwen3-next-80b исключена из роутера ───────────────────
# Регрессия на реальный найденный при калибровке случай: qwen/qwen3-next-80b-a3b-
# instruct:free возвращала HTTP 404 на 100% попыток (провайдер снял бесплатный
# слаг) — модель должна быть полностью исключена из автоматического выбора.

def test_dead_qwen3_next_model_excluded_from_router():
    # Аудит 02.08.2026: glm-4.5-air сам умер (8/8 HTTP 404) — живым примером стал nemotron-3-super.
    assert "qwen/qwen3-next-80b-a3b-instruct:free" in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    route = lumen_router_config._or_route(["qwen/qwen3-next-80b-a3b-instruct:free", "nvidia/nemotron-3-super-120b-a12b:free"])
    ids = [m for _, m in route]
    assert "qwen/qwen3-next-80b-a3b-instruct:free" not in ids
    assert "nvidia/nemotron-3-super-120b-a12b:free" in ids


def test_or_light_order_no_longer_starts_with_dead_or_worst_offender_models():
    # qwen3-next (мёртвая модель) убрана из списка вообще; gpt-oss-20b и
    # nemotron-3-nano-30b-a3b (подтверждённые случаи порчи текста при калибровке)
    # понижены и не должны стоять первыми.
    assert "qwen/qwen3-next-80b-a3b-instruct:free" not in lumen_router_config._OR_LIGHT_ORDER
    assert lumen_router_config._OR_LIGHT_ORDER[0] not in {"openai/gpt-oss-20b:free", "nvidia/nemotron-3-nano-30b-a3b:free"}


def test_nemotron_nano_9b_v2_excluded_after_calibration_run():
    # РЕГРЕССИЯ (калибровочный прогон против Claude Sonnet 5, 18 августа 2026):
    # раньше стояла ПЕРВОЙ в _OR_LIGHT_ORDER и реально отвечала на 82% лёгких
    # текстовых запросов сессии — каждый развёрнутый ответ содержал грубую порчу
    # текста (случайные фрагменты слов на десятке не относящихся к разговору
    # языков внутри русских предложений, см. полный разбор в _OR_MODEL_HEALTH).
    assert "nvidia/nemotron-nano-9b-v2:free" in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    assert "nvidia/nemotron-nano-9b-v2:free" not in lumen_router_config._OR_LIGHT_ORDER
    assert lumen_router_config._OR_MODEL_HEALTH["nvidia/nemotron-nano-9b-v2:free"].reason
    # Замена контрольной "живой" модели: inclusionai/ling-3.0-flash:free сама
    # подтверждённо умерла 22 августа 2026 (см. test_ling_3_0_flash_excluded_after_
    # paid_tier_cutover ниже) — заменена на nemotron-3.5-lightning, чей живой
    # статус ничем не поставлен под сомнение.
    route = lumen_router_config._or_route(["nvidia/nemotron-nano-9b-v2:free", "nvidia/nemotron-3.5-lightning:free"])
    assert [m for _, m in route] == ["nvidia/nemotron-3.5-lightning:free"]


# ─────────────────── единый реестр "нездоровых" моделей OpenRouter (аудит техдолга) ───────────────────
# Раньше "эта модель сейчас плохая" отслеживалось тремя независимыми механизмами
# (_TEMPORARY_FREE_MODELS/_ROUTER_EXCLUDED_OR_MODELS/точечные вычёркивания из
# order-списков) — тесты ниже закрепляют, что теперь единственный источник
# правды — _OR_MODEL_HEALTH, а всё остальное вычисляется из него.

def test_router_excluded_or_models_is_derived_from_health_registry():
    assert lumen_router_config._ROUTER_EXCLUDED_OR_MODELS == frozenset(lumen_router_config._OR_MODEL_HEALTH.keys())


def test_model_health_registry_contains_all_three_known_incidents():
    for model_id in (
        "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
        "qwen/qwen3-coder:free",
        "tencent/hy3:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
    ):
        assert model_id in lumen_router_config._OR_MODEL_HEALTH
        assert lumen_router_config._OR_MODEL_HEALTH[model_id].reason


def test_check_temporary_free_models_expiry_warns_using_registry_reason(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="bot"):
        lumen_router_config._check_temporary_free_models_expiry()
    messages = "\n".join(r.getMessage() for r in caplog.records)
    # qwen3-coder/hy3 промо давно истекло (даты в прошлом) — предупреждение должно
    # включать причину прямо из реестра, а не отдельный захардкоженный текст.
    assert "qwen/qwen3-coder:free" in messages
    assert "tencent/hy3:free" in messages


def test_model_health_note_without_promo_expiry_is_permanent_exclusion():
    # qwen3-next и dolphin-mistral сняты НЕ по истечении промо-акции (нет даты) —
    # они не должны попадать в предупреждение об истёкшем промо вообще.
    for model_id in (
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    ):
        assert lumen_router_config._OR_MODEL_HEALTH[model_id].promo_expiry is None


# ─────────────── inclusionai/ling-3.0-flash:free — снята с бесплатного тира (22.08.2026) ───────────────
# ПОДТВЕРЖДЕНО ПО РЕАЛЬНЫМ ЛОГАМ ПРОДА HF Spaces (22 августа 2026): стояла головой
# _OR_LIGHT_ORDER, две попытки подряд получили идентичный HTTP 404 "This model is
# unavailable for free... use this slug instead: inclusionai/ling-3.0-flash" — тот
# же паттерн платного перевода, что и у z-ai/glm-4.5-air/meta-llama/llama-3.2-3b.

def test_ling_3_0_flash_excluded_after_paid_tier_cutover():
    assert "inclusionai/ling-3.0-flash:free" in lumen_router_config._OR_MODEL_HEALTH
    assert "inclusionai/ling-3.0-flash:free" in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    assert "inclusionai/ling-3.0-flash:free" not in lumen_router_config._OR_LIGHT_ORDER
    route = lumen_router_config._or_route(["inclusionai/ling-3.0-flash:free", "nvidia/nemotron-3.5-lightning:free"])
    assert [m for _, m in route] == ["nvidia/nemotron-3.5-lightning:free"]


def test_ling_3_0_flash_still_flagged_for_leak_detection():
    # Мёртвая модель остаётся в списке для утечки идентичности (см. докстринг
    # TEXT_MODEL_ORDER в lumen_router_config.py) — её ID всё ещё нельзя допускать
    # в ответ, даже если роутер её больше не выбирает.
    assert "inclusionai/ling-3.0-flash:free" in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


# ─────────────────── переупорядочивание _OR_LIGHT_ORDER (аудит моделей, 22.08.2026) ───────────────────
# После исключения ling-3.0-flash (см. выше) nemotron-3-nano-30b-a3b унаследовала
# бы место головы по одному лишь порядку — но у неё уже 3 подтверждённых
# калибровкой инцидента порчи текста И анонсированная дата снятия провайдером
# (24 августа 2026, через 2 дня от аудита) — намеренно понижена, а не оставлена
# головой прямо перед вероятным исчезновением. nemotron-3.5-lightning (ни одного
# инцидента за 5 дней в списке, тот же надёжный modельный ряд nvidia, что и
# nemotron-3-super/-ultra в heavy) — новая голова.

def test_nemotron_3_5_lightning_promoted_to_light_order_head():
    assert lumen_router_config._OR_LIGHT_ORDER[0] == "nvidia/nemotron-3.5-lightning:free"


def test_nemotron_3_nano_30b_a3b_excluded_after_removal_date_passed():
    # Аудит 17.09.2026: дата снятия прошла + слаг пропал из каталога — модель переехала в _OR_MODEL_HEALTH.
    assert "nvidia/nemotron-3-nano-30b-a3b:free" in lumen_router_config._OR_MODEL_HEALTH
    assert "nvidia/nemotron-3-nano-30b-a3b:free" in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    assert "nvidia/nemotron-3-nano-30b-a3b:free" not in lumen_router_config._OR_LIGHT_ORDER
    assert "nvidia/nemotron-3-nano-30b-a3b:free" not in lumen_router_config._SCHEDULED_OR_REMOVALS


def test_or_light_order_ends_with_generic_reserve():
    assert lumen_router_config._OR_LIGHT_ORDER[-1] == "openrouter/free"


# ── Новые модели OpenRouter (аудит 17.08.2026, Top Weekly free): вышли за неделю до аудита, калибровку не проходили — добавлены последними, не выше проверенных.

def test_new_or_models_registered_for_leak_detection():
    assert "nvidia/nemotron-3.5-lightning:free" in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert "dots-studio/dots-3-note-preview:free" in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


def test_dots3_note_preview_stays_last_before_reserve_in_heavy_order():
    # 22.08.2026: некалиброванные новички — перед dots-3-note-preview (та ближе к снятию, остаётся последней перед резервом).
    assert lumen_router_config._OR_HEAVY_ORDER[-1] == "openrouter/free"
    assert lumen_router_config._OR_HEAVY_ORDER[-2] == "dots-studio/dots-3-note-preview:free"


def test_new_or_models_not_accidentally_health_excluded():
    # Ни одна из двух новых моделей не должна была случайно попасть в реестр
    # "нездоровых" — обе живые, просто пока некалиброванные.
    assert "nvidia/nemotron-3.5-lightning:free" not in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    assert "dots-studio/dots-3-note-preview:free" not in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    route = lumen_router_config._or_route(["nvidia/nemotron-3.5-lightning:free", "dots-studio/dots-3-note-preview:free"])
    assert [m for _, m in route] == ["nvidia/nemotron-3.5-lightning:free", "dots-studio/dots-3-note-preview:free"]


# ── Четыре coding-модели (аудит 22.08.2026): после проверенных калибровкой, перед dots-3-note-preview (та ближе к снятию).

def test_new_heavy_models_present_and_healthy():
    new_models = (
        "z-ai/glm-5.2:free", "poolside/laguna-s-2.1:free",
        "cohere/north-mini-code:free", "poolside/laguna-xs-2.1:free",
    )
    for model_id in new_models:
        assert model_id in lumen_router_config._OR_HEAVY_ORDER
        assert model_id not in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
        assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


def test_new_heavy_models_ordered_before_expiring_dots3_note_preview():
    order = lumen_router_config._OR_HEAVY_ORDER
    dots_idx = order.index("dots-studio/dots-3-note-preview:free")
    for model_id in ("z-ai/glm-5.2:free", "poolside/laguna-s-2.1:free", "cohere/north-mini-code:free", "poolside/laguna-xs-2.1:free"):
        assert order.index(model_id) < dots_idx


def test_or_heavy_order_still_headed_by_calibrated_flagships():
    # Уже проверенные калибровкой сильные модели остаются головой — новые
    # некалиброванные модели добавлены строго после них, не выше.
    assert lumen_router_config._OR_HEAVY_ORDER[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert lumen_router_config._OR_HEAVY_ORDER[1] == "nvidia/nemotron-3-super-120b-a12b:free"


# ─────────────────── анонсированные ("Going away <дата>") даты снятия моделей ───────────────────
# НЕ то же самое, что _OR_MODEL_HEALTH — сам факт будущей даты на карточке
# OpenRouter недостаточен, чтобы исключить модель из роутинга прямо сейчас (см.
# принцип "не удаляй по спекуляции" в докстринге _OR_MODEL_HEALTH), но и полностью
# игнорировать анонс нельзя (см. tencent/hy3 — истечение промо было замечено
# только постфактум). _check_scheduled_removals_due закрывает этот пробел.

def test_scheduled_removals_registered_for_dots3_preview_only():
    # Аудит 17.09.2026: nano-30b-a3b умер и переехал в реестр — в будущих снятиях только dots-3-note-preview.
    assert "nvidia/nemotron-3-nano-30b-a3b:free" not in lumen_router_config._SCHEDULED_OR_REMOVALS
    assert lumen_router_config._SCHEDULED_OR_REMOVALS["dots-studio/dots-3-note-preview:free"].isoformat() == "2026-09-30"


def test_check_scheduled_removals_due_warns_after_date_passed(caplog):
    import logging
    from datetime import date, timedelta
    original = dict(lumen_router_config._SCHEDULED_OR_REMOVALS)
    lumen_router_config._SCHEDULED_OR_REMOVALS.clear()
    lumen_router_config._SCHEDULED_OR_REMOVALS["some-provider/some-model:free"] = date.today() - timedelta(days=1)
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            lumen_router_config._check_scheduled_removals_due()
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "some-provider/some-model:free" in messages
    finally:
        lumen_router_config._SCHEDULED_OR_REMOVALS.clear()
        lumen_router_config._SCHEDULED_OR_REMOVALS.update(original)


def test_check_scheduled_removals_due_silent_before_date(caplog):
    import logging
    from datetime import date, timedelta
    original = dict(lumen_router_config._SCHEDULED_OR_REMOVALS)
    lumen_router_config._SCHEDULED_OR_REMOVALS.clear()
    lumen_router_config._SCHEDULED_OR_REMOVALS["some-provider/some-model:free"] = date.today() + timedelta(days=30)
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            lumen_router_config._check_scheduled_removals_due()
        assert caplog.records == []
    finally:
        lumen_router_config._SCHEDULED_OR_REMOVALS.clear()
        lumen_router_config._SCHEDULED_OR_REMOVALS.update(original)


def test_check_scheduled_removals_due_silent_once_already_health_excluded(caplog):
    # Модель, уже перешедшая в _OR_MODEL_HEALTH (подтверждена мёртвой), не должна
    # больше давать "перепроверь" — проверка уже сделана, реестр уже отражает результат.
    import logging
    from datetime import date
    original = dict(lumen_router_config._SCHEDULED_OR_REMOVALS)
    lumen_router_config._SCHEDULED_OR_REMOVALS.clear()
    lumen_router_config._SCHEDULED_OR_REMOVALS["inclusionai/ling-3.0-flash:free"] = date(2026, 1, 1)
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            lumen_router_config._check_scheduled_removals_due()
        assert caplog.records == []
    finally:
        lumen_router_config._SCHEDULED_OR_REMOVALS.clear()
        lumen_router_config._SCHEDULED_OR_REMOVALS.update(original)


# ─────────────────── TEXT_MODEL_ORDER переименован, алиас сохранён ───────────────────

def test_text_model_order_alias_still_works():
    assert lumen_router_config.TEXT_MODEL_ORDER is lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert "nvidia/nemotron-3-super-120b-a12b:free" in lumen_router_config.TEXT_MODEL_ORDER


# ─────────────────── GEMINI_TTS_MODELS / FISH_AUDIO_TTS_MODEL централизованы ───────────────────

def test_gemini_tts_models_centralized_in_router_config():
    assert lumen_router_config.GEMINI_TTS_MODELS == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]


# ─────────────────── аудит моделей, 17 сентября 2026 ───────────────────
# Живой каталог OpenRouter через публичный /models API (24 бесплатные модели):
# 5 слагов пропали из бесплатных (gpt-oss-20b/120b, lfm-2.5-1.2b-thinking,
# nano-30b-a3b, nano-12b-v2-vl), 8 новых добавлены в хвосты цепочек
# (некалиброванные — см. комментарий в lumen_router_config.py), Gemini 3.8 Flash
# добавлен головой по дашборду AI Studio владельца.

_DEAD_SEPT_2026 = (
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
)

_NEW_SEPT_2026 = (
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "liquid/lfm-2.5-2.6b:free",
)


def test_sept_2026_dead_models_excluded_but_kept_for_leak_detection():
    for model_id in _DEAD_SEPT_2026:
        assert model_id in lumen_router_config._OR_MODEL_HEALTH
        assert model_id in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
        assert model_id not in lumen_router_config._OR_LIGHT_ORDER
        assert model_id not in lumen_router_config._OR_HEAVY_ORDER
        assert model_id not in lumen_router_config._OR_VISION_ORDER
        assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


def test_sept_2026_new_light_models_after_proven_head_before_reserve():
    order = lumen_router_config._OR_LIGHT_ORDER
    assert order[0] == "nvidia/nemotron-3.5-lightning:free"
    assert order[-1] == "openrouter/free"
    for model_id in (
        "nex-agi/nex-n2.5-mini:free",
        "inclusionai/ling-3.0-flash-sante:free", "inclusionai/ling-3.0-flash-fin:free",
        "liquid/lfm-2.5-2.6b:free",
    ):
        assert model_id in order
        assert model_id not in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
        assert model_id in lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION


def test_inkling_small_excluded_after_agentic_harness_refusal():
    # Вечер 17.09.2026, первый прод-трафик нового кода: inkling-small ответил
    # отказом "only available on agentic harnesses" — чат-запросы не обслуживает.
    assert "thinkingmachines/inkling-small:free" in lumen_router_config._OR_MODEL_HEALTH
    assert "thinkingmachines/inkling-small:free" in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS
    assert "thinkingmachines/inkling-small:free" not in lumen_router_config._OR_LIGHT_ORDER
    # nex-mini тем же вечером подтверждён живьём — занял его место вторым.
    assert lumen_router_config._OR_LIGHT_ORDER[1] == "nex-agi/nex-n2.5-mini:free"


def test_nemotron_super_demoted_below_ultra_after_second_mush_incident():
    # Вечер 17.09.2026: super выдал "кашу" прямо в проде (второй инцидент после
    # калибровочного) — понижен под ultra без инцидентов, но не исключён.
    order = lumen_router_config._OR_HEAVY_ORDER
    assert order[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert order[1] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert "nvidia/nemotron-3-super-120b-a12b:free" not in lumen_router_config._ROUTER_EXCLUDED_OR_MODELS


def test_sept_2026_new_heavy_and_vision_models_placed():
    heavy = lumen_router_config._OR_HEAVY_ORDER
    assert "thinkingmachines/inkling:free" in heavy
    assert "nex-agi/nex-n2.5-pro:free" in heavy
    assert heavy.index("thinkingmachines/inkling:free") < heavy.index("dots-studio/dots-3-note-preview:free")
    vision = lumen_router_config._OR_VISION_ORDER
    assert vision[0] == "google/gemma-4-31b-it:free"
    assert "inclusionai/ling-3.0-flash-vl:free" in vision


def test_gemini_3_8_flash_heads_heavy_chain_and_default():
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_MODELS
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-3.8-flash"
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_SEARCH_CHAIN
    assert "gemini-3.8-flash" in lumen_router_config.GEMINI_LINK_CHAIN
    conf = lumen_router_config.GEMINI_MODELS["gemini-3.8-flash"]
    assert conf.get("search_grounding") is False
    assert conf.get("map_grounding") is False
    assert conf.get("url_context") is True
    assert not conf.get("quota_unconfirmed")


def test_fish_audio_disabled_after_free_tier_gone():
    # Зеркало снято с бесплатного каталога OpenRouter — первая попытка в inline_tts
    # пропускается флагом, сама функция оставлена для тестов/возврата.
    assert lumen_router_config.FISH_AUDIO_ENABLED is False
