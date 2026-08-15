"""
test_lumen_router_config.py — юнит-тесты на lumen_router_config.py: конфигурация моделей
(GEMINI_MODELS, GEMINI_TTS_MODELS), единый реестр "нездоровых" моделей OpenRouter
(_OR_MODEL_HEALTH/_ROUTER_EXCLUDED_OR_MODELS), эвристики "это тяжёлый запрос?"/"нужна
свежая информация?" (_looks_like_heavy_query/_looks_like_freshness_query), построение
маршрута для одного сообщения (_build_route/_or_route), проверки истечения промо-доступа
(_check_temporary_free_models_expiry/_check_fish_audio_tts_expiry) и неподтверждённых квот
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


def test_looks_like_freshness_query_detects_current_info_needs():
    assert lumen_router_config._looks_like_freshness_query("кто сейчас президент Франции") is True
    assert lumen_router_config._looks_like_freshness_query("какая сегодня погода в Москве") is True
    assert lumen_router_config._looks_like_freshness_query("сколько стоит биткоин") is True
    assert lumen_router_config._looks_like_freshness_query("последние новости про ИИ") is True


def test_looks_like_freshness_query_false_for_timeless_questions():
    assert lumen_router_config._looks_like_freshness_query("столица Франции") is False
    assert lumen_router_config._looks_like_freshness_query("объясни теорию относительности") is False


def test_build_route_youtube_link_forces_gemini_only():
    route = lumen_router_config._build_route(needs_youtube=True, needs_website=False, media_mime=None, is_heavy=False, needs_freshness=False)
    assert all(p == "gemini" for p, _ in route)
    assert route[0] == ("gemini", "gemini-3.6-flash")


def test_build_route_website_link_forces_gemini_only_and_excludes_gemma():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=True, media_mime=None, is_heavy=False, needs_freshness=False)
    assert all(p == "gemini" for p, _ in route)
    # Gemma (no_system=True) не умеет читать сайты по ссылке — не должна попадать в маршрут.
    assert "gemma-4-31b-it" not in [m for _, m in route]
    assert "gemma-4-26b-a4b-it" not in [m for _, m in route]


def test_build_route_freshness_query_prioritizes_search_capable_gemini_models():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime=None, is_heavy=False, needs_freshness=True)
    # ОБНОВЛЕНО (24.07.2026, по реальным данным дашборда AI Studio): search grounding
    # подтверждён ТОЛЬКО у поколения Gemini 2.5 (общий бакет "Gemini 2.5" — 21/1500) —
    # у всего модельного ряда Gemini 3.x (включая обе "lite", которые раньше по
    # ошибке стояли здесь первыми) общий бакет "Gemini 3" показывает 0/0. Первым
    # кандидатом теперь должна идти gemini-2.5-flash.
    assert route[0] == ("gemini", "gemini-2.5-flash")
    assert route[0][0] == "gemini"
    # OpenRouter должен присутствовать как резерв на случай полного отказа Gemini.
    assert any(p == "openrouter" for p, _ in route)


def test_build_route_plain_text_prefers_openrouter_to_save_gemini_quota():
    # Основной сценарий из требования: обычный текст без вложений/ссылок/нужды
    # в интернете — должен идти в OpenRouter первым делом, а не в Gemini.
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime=None, is_heavy=False, needs_freshness=False)
    assert route[0][0] == "openrouter"
    assert any(p == "gemini" for p, _ in route)  # Gemini всё ещё есть как резерв


def test_build_route_heavy_plain_text_uses_strong_openrouter_models_first():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime=None, is_heavy=True, needs_freshness=False)
    assert route[0] == ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free")


def test_build_route_image_without_freshness_prefers_openrouter_vision():
    route = lumen_router_config._build_route(needs_youtube=False, needs_website=False, media_mime="image/jpeg", is_heavy=False, needs_freshness=False)
    assert route[0] == ("openrouter", "nvidia/nemotron-nano-12b-v2-vl:free")


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
    assert "gemini-3.6-flash" in lumen_router_config.GEMINI_MODELS
    assert "gemini-3.5-flash-lite" in lumen_router_config.GEMINI_MODELS
    assert lumen_router_config.DEFAULT_GEMINI_MODEL == "gemini-3.6-flash"
    assert lumen_router_config.GEMINI_HEAVY_CHAIN[0] == "gemini-3.6-flash"
    # Обновлено (24.07.2026) вместе с реордером GEMINI_SEARCH_CHAIN — см. комментарий
    # там же: реальная квота на search grounding подтверждена только у Gemini 2.5.
    assert lumen_router_config.GEMINI_SEARCH_CHAIN[0] == "gemini-2.5-flash"


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
    # ОБНОВЛЕНО (аудит моделей, 2 августа 2026): z-ai/glm-4.5-air:free раньше был
    # здесь "живым" контрольным примером — с тех пор он сам подтверждённо умер
    # (см. _OR_MODEL_HEALTH, 8/8 HTTP 404 в реальных логах), поэтому больше не
    # годится как пример "модели, которую роутер оставляет" — заменён на
    # nemotron-3-super, чей живой статус ничем не поставлен под сомнение.
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


# ─────────────────── TEXT_MODEL_ORDER переименован, алиас сохранён ───────────────────

def test_text_model_order_alias_still_works():
    assert lumen_router_config.TEXT_MODEL_ORDER is lumen_router_config._KNOWN_MODEL_IDS_FOR_LEAK_DETECTION
    assert "nvidia/nemotron-3-super-120b-a12b:free" in lumen_router_config.TEXT_MODEL_ORDER


# ─────────────────── GEMINI_TTS_MODELS / FISH_AUDIO_TTS_MODEL централизованы ───────────────────

def test_gemini_tts_models_centralized_in_router_config():
    assert lumen_router_config.GEMINI_TTS_MODELS == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]


def test_check_fish_audio_tts_expiry_warns_after_expiry_date(caplog):
    import logging
    from datetime import date, timedelta
    original_expiry = lumen_router_config.FISH_AUDIO_FREE_TIER_EXPIRY
    lumen_router_config.FISH_AUDIO_FREE_TIER_EXPIRY = date.today() - timedelta(days=1)
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            lumen_router_config._check_fish_audio_tts_expiry()
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert lumen_router_config.FISH_AUDIO_TTS_MODEL in messages
    finally:
        lumen_router_config.FISH_AUDIO_FREE_TIER_EXPIRY = original_expiry


def test_check_fish_audio_tts_expiry_silent_before_expiry_date(caplog):
    import logging
    from datetime import date, timedelta
    original_expiry = lumen_router_config.FISH_AUDIO_FREE_TIER_EXPIRY
    lumen_router_config.FISH_AUDIO_FREE_TIER_EXPIRY = date.today() + timedelta(days=30)
    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            lumen_router_config._check_fish_audio_tts_expiry()
        assert caplog.records == []
    finally:
        lumen_router_config.FISH_AUDIO_FREE_TIER_EXPIRY = original_expiry
