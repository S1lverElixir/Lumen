"""
test_lumen_images.py — юнит-тесты на lumen_images.py: автоматический выбор модели
генерации изображений по содержимому промпта (_pick_image_model), заменивший ручной
выбор через убранную команду /imgmodel (см. README, "Автоматический выбор модели").

lumen_images.py не зависит от Telegram/рантайм-состояния бота (тот же принцип, что и
у lumen_formatting.py/lumen_router_config.py — см. их тестовые файлы), поэтому
тестируется здесь напрямую (import lumen_images), без импорта bot.py.

Запуск:
    pytest test_lumen_images.py -v
"""
import lumen_images


# ─────────────────────────── _pick_image_model ───────────────────────────

def test_pick_image_model_anime():
    assert lumen_images._pick_image_model("нарисуй девушку в стиле аниме") == "flux-anime"
    assert lumen_images._pick_image_model("draw a chibi character") == "flux-anime"
    assert lumen_images._pick_image_model("manga style portrait") == "flux-anime"


def test_pick_image_model_fantasy():
    assert lumen_images._pick_image_model("нарисуй дракона в фэнтезийном замке") == "dreamshaper"
    assert lumen_images._pick_image_model("concept art of an elf wizard") == "dreamshaper"
    assert lumen_images._pick_image_model("рыцарь на фоне волшебного леса") == "dreamshaper"


def test_pick_image_model_realism():
    assert lumen_images._pick_image_model("сделай фотореалистичный портрет кота") == "flux-realism"
    assert lumen_images._pick_image_model("realistic photo of a mountain") == "flux-realism"
    assert lumen_images._pick_image_model("нарисуй кота как на фото") == "flux-realism"


def test_pick_image_model_quick_draft():
    assert lumen_images._pick_image_model("быстрый набросок логотипа") == "turbo"
    assert lumen_images._pick_image_model("quick sketch of a car") == "turbo"


def test_pick_image_model_falls_back_to_default_for_generic_prompt():
    assert lumen_images._pick_image_model("космическая станция на орбите Земли") == lumen_images.DEFAULT_HF_IMAGE_MODEL
    assert lumen_images._pick_image_model("кот на подоконнике") == lumen_images.DEFAULT_HF_IMAGE_MODEL


def test_pick_image_model_empty_prompt_returns_default():
    assert lumen_images._pick_image_model("") == lumen_images.DEFAULT_HF_IMAGE_MODEL


def test_pick_image_model_style_keyword_wins_over_quick_keyword():
    # Стилевой сигнал важнее просьбы "побыстрее", если оба есть в одном промпте —
    # аниме проверяется раньше черновика/скетча (см. докстринг _pick_image_model).
    assert lumen_images._pick_image_model("быстро нарисуй аниме-девушку") == "flux-anime"


def test_pick_image_model_case_insensitive():
    assert lumen_images._pick_image_model("АНИМЕ ДЕВУШКА") == "flux-anime"


def test_pick_image_model_result_always_a_known_model():
    # Регрессия на класс ошибок "эвристика вернула ID, которого нет в каталоге" —
    # неважно, какой промпт, результат обязан быть валидным ключом HF_IMAGE_MODELS.
    prompts = [
        "нарисуй кота", "аниме", "фэнтези дракон", "реалистичное фото гор",
        "быстрый скетч", "", "случайный текст без ключевых слов вообще",
    ]
    for p in prompts:
        assert lumen_images._pick_image_model(p) in lumen_images.HF_IMAGE_MODELS
