"""
test_lumen_images.py — юнит-тесты на lumen_images.py: автоматический выбор модели
генерации изображений по содержимому промпта (_pick_image_model), заменивший ручной
выбор через убранную команду /imgmodel (см. README, "Automatic model routing").

lumen_images.py не зависит от Telegram/рантайм-состояния бота (тот же принцип, что и
у lumen_formatting.py/lumen_router_config.py — см. их тестовые файлы), поэтому
тестируется здесь напрямую (import lumen_images), без импорта bot.py.

Запуск:
    pytest test_lumen_images.py -v
"""
import asyncio

import lumen_images
import pytest


# ─────────────────────────── _pick_image_model ───────────────────────────


@pytest.mark.parametrize(("prompt", "expected"), [
    ("нарисуй девушку в стиле аниме", "flux-anime"),
    ("draw a chibi character", "flux-anime"),
    ("manga style portrait", "flux-anime"),
    ("нарисуй дракона в фэнтезийном замке", "dreamshaper"),
    ("concept art of an elf wizard", "dreamshaper"),
    ("рыцарь на фоне волшебного леса", "dreamshaper"),
    ("сделай фотореалистичный портрет кота", "flux-realism"),
    ("realistic photo of a mountain", "flux-realism"),
    ("нарисуй кота как на фото", "flux-realism"),
    ("быстрый набросок логотипа", "turbo"),
    ("quick sketch of a car", "turbo"),
    ("космическая станция на орбите Земли", lumen_images.DEFAULT_POLLINATIONS_IMAGE_MODEL),
    ("кот на подоконнике", lumen_images.DEFAULT_POLLINATIONS_IMAGE_MODEL),
    ("", lumen_images.DEFAULT_POLLINATIONS_IMAGE_MODEL),
    # Стилевой сигнал важнее просьбы "побыстрее": аниме проверяется раньше черновика.
    ("быстро нарисуй аниме-девушку", "flux-anime"),
    ("АНИМЕ ДЕВУШКА", "flux-anime"),
])
def test_pick_image_model(prompt, expected):
    assert lumen_images._pick_image_model(prompt) == expected


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

def _fake_image_session(body_chunks, *, status=200, headers=None, captured=None):
    class FakeContent:
        def iter_chunked(self, _n):
            async def _gen():
                for chunk in body_chunks:
                    yield chunk
            return _gen()

    class FakeResp:
        def __init__(self):
            self.status = status
            self.headers = headers or {}
            self.content = FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def get(self, url, timeout=None):
            if captured is not None:
                captured["timeout"] = timeout
            return FakeResp()

    return FakeSession()


def test_pollinations_generate_accepts_jpeg_by_magic_bytes():
    # Регрессия AUD-E-001: body[:4] никогда не равен 3-байтному b"\xff\xd8\xff",
    # поэтому JPEG без image/* Content-Type отвергался как "не-изображение".
    session = _fake_image_session(
        [b"\xff\xd8\xff\xe0" + b"\x00" * 100],
        headers={"Content-Type": "application/octet-stream"},
    )
    body = asyncio.run(lumen_images._pollinations_generate(session, "flux", "кот"))
    assert body[:3] == b"\xff\xd8\xff"


def test_pollinations_generate_refuses_oversized_body(monkeypatch):
    # Чтение без капа складывало в память тело любого размера: Content-Length
    # врёт, поток перепроверяем по факту. Кап ужимаем, иначе тест лил бы 30 МБ.
    monkeypatch.setattr(lumen_images, "POLLINATIONS_IMAGE_MAX_BYTES", 100)
    big = b"\x89PNG" + b"\x00" * 200
    with pytest.raises(RuntimeError):
        asyncio.run(lumen_images._pollinations_generate(
            _fake_image_session([big], headers={"Content-Type": "image/png"}), "flux", "кот"))
    with pytest.raises(RuntimeError):
        asyncio.run(lumen_images._pollinations_generate(
            _fake_image_session(
                [b"\x89PNG" + b"\x00" * 10],
                headers={"Content-Type": "image/png", "Content-Length": "1000000"},
            ), "flux", "кот"))


def test_pollinations_generate_timeout_uses_given_budget():
    # Таймаут попытки — остаток DRAW_TOTAL_BUDGET_SEC, а не фиксированные 90с.
    captured = {}
    session = _fake_image_session([b"\x89PNG" + b"\x00" * 10], captured=captured)
    asyncio.run(lumen_images._pollinations_generate(session, "flux", "кот", timeout_sec=7.5))
    assert captured["timeout"].total == 7.5
    assert captured["timeout"].sock_connect == 12.0
