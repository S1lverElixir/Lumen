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
import asyncio

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
    assert lumen_images._pick_image_model("космическая станция на орбите Земли") == lumen_images.DEFAULT_POLLINATIONS_IMAGE_MODEL
    assert lumen_images._pick_image_model("кот на подоконнике") == lumen_images.DEFAULT_POLLINATIONS_IMAGE_MODEL


def test_pick_image_model_empty_prompt_returns_default():
    assert lumen_images._pick_image_model("") == lumen_images.DEFAULT_POLLINATIONS_IMAGE_MODEL


def test_pick_image_model_style_keyword_wins_over_quick_keyword():
    # Стилевой сигнал важнее просьбы "побыстрее", если оба есть в одном промпте —
    # аниме проверяется раньше черновика/скетча (см. докстринг _pick_image_model).
    assert lumen_images._pick_image_model("быстро нарисуй аниме-девушку") == "flux-anime"


def test_pick_image_model_case_insensitive():
    assert lumen_images._pick_image_model("АНИМЕ ДЕВУШКА") == "flux-anime"


def test_pick_image_model_result_always_a_known_model():
    # Регрессия на класс ошибок "эвристика вернула ID, которого нет в каталоге" —
    # неважно, какой промпт, результат обязан быть валидным ключом POLLINATIONS_IMAGE_MODELS.
    prompts = [
        "нарисуй кота", "аниме", "фэнтези дракон", "реалистичное фото гор",
        "быстрый скетч", "", "случайный текст без ключевых слов вообще",
    ]
    for p in prompts:
        assert lumen_images._pick_image_model(p) in lumen_images.POLLINATIONS_IMAGE_MODELS


# ─────────────────────────── _pollinations_generate ───────────────────────────

def test_pollinations_generate_accepts_jpeg_by_magic_bytes():
    # Регрессия AUD-E-001: body[:4] никогда не равен 3-байтному b"\xff\xd8\xff",
    # поэтому JPEG без image/* Content-Type отвергался как "не-изображение".
    class FakeResp:
        status = 200
        headers = {"Content-Type": "application/octet-stream"}

        async def read(self):
            return b"\xff\xd8\xff\xe0" + b"\x00" * 100

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def get(self, url, timeout=None):
            return FakeResp()

    body = asyncio.run(lumen_images._pollinations_generate(FakeSession(), "flux", "кот"))
    assert body[:3] == b"\xff\xd8\xff"
