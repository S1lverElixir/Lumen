"""
test_lumen_images.py — юнит-тесты на lumen_images.py: контракт запросов к обоим
провайдерам картинок (Cloudflare основной, Pollinations резерв), разбор их ошибок и
выбор формата ответа.

lumen_images.py не зависит от Telegram/рантайм-состояния бота (тот же принцип, что и
у lumen_formatting.py/lumen_router_config.py — см. их тестовые файлы), поэтому
тестируется здесь напрямую (import lumen_images), без импорта bot.py.

Запуск:
    pytest test_lumen_images.py -v
"""
import asyncio
import base64
import json
import urllib.parse

from typing import Any

import lumen_images
import pytest

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 120
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120
GIF = b"GIF89a" + b"\x00" * 120


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunked(self, _n):
        async def _gen():
            for chunk in self._chunks:
                yield chunk
        return _gen()


class _FakeResp:
    def __init__(self, *, status=200, headers=None, chunks=(b"",), text=""):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks)
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Логирует и URL, и тело запроса: половина находок аудита — это параметры,
    которые молча перестали передаваться (seed, safe, nologo). Список ответов
    выдаётся по порядку — для ветки «первый запрос отвергли, повтор другой пейлоад»."""

    def __init__(self, resp, captured=None):
        self.resps = list(resp) if isinstance(resp, (list, tuple)) else [resp]
        self.captured = captured if captured is not None else {}
        self.calls: list[dict[str, Any]] = []

    def _take(self, method, url, payload, timeout, headers, data=None):
        self.calls.append({"method": method, "url": url, "payload": payload, "timeout": timeout,
                           "headers": headers, "data": data})
        self.captured.update(method=method, url=url, payload=payload, timeout=timeout,
                             headers=headers, data=data)
        return self.resps.pop(0) if len(self.resps) > 1 else self.resps[0]

    def get(self, url, timeout=None, headers=None):
        return self._take("GET", url, None, timeout, headers)

    def post(self, url, *, json=None, data=None, timeout=None, headers=None):
        return self._take("POST", url, json, timeout, headers, data)


def _image_resp():
    return _FakeResp(status=200, headers={"Content-Type": "image/jpeg"}, chunks=(JPEG,))


# ─────────────────────────── выбор провайдера ───────────────────────────


def test_cloudflare_is_primary_only_when_configured(monkeypatch):
    # Опечатка/пустой секрет в env обязан уводить цепочку на Pollinations, а не
    # давать гарантированную ошибку на первом провайдере.
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_API_TOKEN", "x")
    assert lumen_images.image_provider_names() == ("pollinations",)
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_ACCOUNT_ID", "acct")
    assert lumen_images.image_provider_names() == ("cloudflare", "pollinations")


# ─────────────────────────── контракт Pollinations ───────────────────────────


@pytest.mark.parametrize("prompt, expected", [
    ("кот 16:9", (1344, 768)),
    ("16х9 пейзаж", (1344, 768)),
    ("вертикальная картинка", (768, 1344)),
    ("portrait photo", (768, 1344)),
    ("квадрат", (1024, 1024)),
    ("кот", (1024, 1024)),
    ("широкий спектр", (1024, 1024)),
    ("встреча в 13:45", (1024, 1024)),
])
def test_detect_image_size(prompt, expected):
    # Пропорции как в генераторе ChatGPT; "широкий спектр" не должен стать 16:9.
    assert lumen_images.detect_image_size(prompt) == expected


def _jpeg_header(width, height):
    """Минимальный JPEG-заголовок: APP0, затем SOF0 с размерами — достаточно
    для парсера размеров без сторонних библиотек."""
    return (
        bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10]) + bytes(14)
        + bytes([0xFF, 0xC0, 0x00, 0x11, 0x08])
        + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes(9)
    )


def test_image_dimensions_reads_headers_without_pillow():
    assert lumen_images.image_dimensions(_jpeg_header(800, 450)) == (800, 450)
    png = (
        bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        + bytes([0x00, 0x00, 0x00, 0x0D]) + b"IHDR"
        + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    )
    assert lumen_images.image_dimensions(png) == (640, 480)
    assert lumen_images.image_dimensions(b"not an image") is None


def test_size_for_reference_follows_orientation():
    assert lumen_images.size_for_reference((1280, 720)) == (1344, 768)
    assert lumen_images.size_for_reference((720, 1280)) == (768, 1344)
    assert lumen_images.size_for_reference((800, 800)) == (1024, 1024)
    assert lumen_images.size_for_reference(None) == (1024, 1024)


def test_cloudflare_generation_passes_requested_size(cf_configured):
    captured = {}
    asyncio.run(cf_configured._cloudflare_text_to_image(
        _cf_ok_session(captured), "кот", width=1344, height=768))
    fields = {name: value for name, value, _ in captured["data"].fields}
    assert fields["width"] == "1344" and fields["height"] == "768"


def test_pollinations_url_carries_seed_safe_and_nologo():
    # Замер 09.11.2026: без seed Pollinations отдаёт seed=42 и байт-в-байт тот же
    # файл на повторе промпта, без safe фильтр NSFW выключен.
    captured = {}
    asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_image_resp(), captured), "кот"))
    url = captured["url"]
    assert "safe=true" in url and "nologo=true" in url and "enhance=false" in url
    query = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert int(query["seed"][0]) > 0
    asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_image_resp(), captured), "кот"))
    assert int(urllib.parse.parse_qs(captured["url"].split("?", 1)[1])["seed"][0]) != int(query["seed"][0])


def test_pollinations_truncates_prompt_to_cap():
    captured = {}
    asyncio.run(lumen_images._pollinations_text_to_image(
        _FakeSession(_image_resp(), captured), "д" * 900, timeout_sec=7.5))
    path = captured["url"].split("/prompt/", 1)[1].split("?", 1)[0]
    assert path == urllib.parse.quote("д" * lumen_images.IMAGE_PROMPT_MAX_CHARS, safe="")
    assert captured["timeout"].total == 7.5
    assert captured["timeout"].sock_connect == 12.0


def test_pollinations_free_limit_402_is_rate_limit_with_retry_after():
    # Реальный ответ Pollinations 09.11.2026: HTTP 402 FREE_LIMIT, 1 картинка в 45
    # секунд на IP. Раньше 402 не попадал ни в одну ветку и человек читал
    # «переформулируй описание» вместо «сервис перегружен».
    body = json.dumps({"error": {"code": "FREE_LIMIT", "message": "Free limit reached: 1 image every 45 seconds per IP"}})
    resp = _FakeResp(status=402, headers={"Content-Type": "application/json", "Retry-After": "35"},
                    text=body, chunks=(body.encode(),))
    with pytest.raises(lumen_images.ImageRateLimitedError) as exc:
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "кот"))
    assert exc.value.retry_after_sec == 35.0
    assert "FREE_LIMIT" in str(exc.value)


def test_pollinations_402_without_retry_after_is_budget():
    body = json.dumps({"error": {"code": "INSUFFICIENT_POLLEN"}})
    resp = _FakeResp(status=402, text=body, chunks=(body.encode(),))
    with pytest.raises(lumen_images.ImageBudgetExhaustedError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "кот"))


def test_pollinations_429_and_500_are_classified_apart():
    with pytest.raises(lumen_images.ImageRateLimitedError):
        asyncio.run(lumen_images._pollinations_text_to_image(
            _FakeSession(_FakeResp(status=429, headers={"Retry-After": "7"})), "кот"))
    with pytest.raises(lumen_images.ImageProviderError) as exc:
        asyncio.run(lumen_images._pollinations_text_to_image(
            _FakeSession(_FakeResp(status=500)), "кот"))
    assert not isinstance(exc.value, lumen_images.ImageRateLimitedError)


# ─────────────────────────── тело ответа ───────────────────────────


def test_accepts_jpeg_by_magic_bytes_when_content_type_lies():
    # Регрессия AUD-E-001: JPEG без image/* Content-Type отвергался как не-изображение.
    resp = _FakeResp(status=200, headers={"Content-Type": "application/octet-stream"}, chunks=(JPEG,))
    body = asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "кот"))
    assert body[:3] == b"\xff\xd8\xff"


def test_rejects_oversized_body(monkeypatch):
    # Content-Length врёт, поэтому поток перепроверяем по факту; кап ужимаем,
    # иначе тест лил бы 30 МБ.
    monkeypatch.setattr(lumen_images, "IMAGE_MAX_BYTES", 100)
    big = b"\x89PNG" + b"\x00" * 200
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_FakeResp(
            status=200, headers={"Content-Type": "image/png"}, chunks=(big,))), "кот"))
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_FakeResp(
            status=200, headers={"Content-Type": "image/png", "Content-Length": "1000000"},
            chunks=(b"\x89PNG" + b"\x00" * 10,))), "кот"))


def test_rejects_format_sendphoto_cannot_take():
    # GIF-магия проходила как «картинка», а sendPhoto анимированный GIF отклонял:
    # ошибку человек видел только как «не удалось отправить».
    resp = _FakeResp(status=200, headers={"Content-Type": "image/gif"}, chunks=(GIF,))
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "кот"))


def test_image_filename_follows_magic_bytes():
    assert lumen_images.image_filename(JPEG) == "generated.jpg"
    assert lumen_images.image_filename(PNG) == "generated.png"
    assert lumen_images.image_filename(b"RIFF\x00\x00\x00\x00WEBP") == "generated.webp"
    assert lumen_images.image_filename(b"RIFF\x00\x00\x00\x00ANIM") is None
    assert lumen_images.image_filename(GIF) is None


# ─────────────────────────── Cloudflare ───────────────────────────


class _FakeForm:
    """Собирает поля multipart-формы: реальный aiohttp.FormData кодирует тело
    в рантайме, а контракт (имена полей, референс) проверяем здесь."""

    def __init__(self):
        self.fields = []

    def add_field(self, name, value, **kwargs):
        self.fields.append((name, value, kwargs))


@pytest.fixture
def cf_configured(monkeypatch):
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_ACCOUNT_ID", "acct-id")
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(lumen_images.aiohttp, "FormData", _FakeForm)
    return lumen_images


def _cf_ok_session(captured=None):
    return _FakeSession(_FakeResp(status=200, chunks=(json.dumps(
        {"success": True, "result": {"image": base64.b64encode(JPEG).decode()}}).encode(),)), captured)


def test_cloudflare_generation_is_multipart_with_prompt(cf_configured):
    # Замер 10.11.2026: flux-2-klein принимает только multipart; seed и размер
    # уходят полями формы, а не JSON.
    captured = {}
    session = _cf_ok_session(captured)
    asyncio.run(cf_configured._cloudflare_text_to_image(session, "кот"))
    assert captured["method"] == "POST"
    assert "/ai/run/@cf/black-forest-labs/flux-2-klein-4b" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer cf-token"
    fields = {name: value for name, value, _ in captured["data"].fields}
    assert fields["prompt"] == "кот"
    assert fields["width"] == "1024" and fields["height"] == "1024"
    assert int(fields["seed"]) > 0
    assert "input_image_0" not in fields
    assert len(session.calls) == 1


def test_cloudflare_edit_sends_reference_as_input_image(cf_configured):
    # Правка одной моделью: референс обязан уехать полем input_image_0 с
    # сигнатурным content-type, иначе Cloudflare проигнорирует картинку.
    captured = {}
    jpeg = JPEG
    asyncio.run(cf_configured._cloudflare_edit_to_image(_cf_ok_session(captured), "make it green", jpeg))
    fields = {name: (value, kw) for name, value, kw in captured["data"].fields}
    assert fields["prompt"][0] == "make it green"
    assert fields["input_image_0"][0] == jpeg
    assert fields["input_image_0"][1]["content_type"] == "image/jpeg"


def test_cloudflare_moderation_rejection_is_not_retryable(cf_configured):
    # Замер 10.11.2026: NSFW-промт получает 400 code 3030 "output has been
    # flagged". Это не перегрузка: политика не должна включать cooldown.
    body = json.dumps({"success": False, "errors": [
        {"code": 3030, "message": "AiError: Your output has been flagged. Please choose another prompt / input image combination"}]}).encode()
    with pytest.raises(lumen_images.ImageProviderError) as exc:
        asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
            status=400, chunks=(body,))), "кот"))
    assert not isinstance(exc.value, (lumen_images.ImageRateLimitedError, lumen_images.ImageBudgetExhaustedError))


def test_cloudflare_rejects_unconfigured_without_request(cf_configured, monkeypatch):
    # Без ключа запрос обязан не уходить в сеть: ловим это по пустому captured.
    monkeypatch.setattr(cf_configured, "CLOUDFLARE_API_TOKEN", "")
    captured = {}
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(cf_configured._cloudflare_text_to_image(_cf_ok_session(captured), "кот"))
    assert captured == {}


def test_cloudflare_rejects_non_object_json(cf_configured):
    # Валидный JSON, но не объект: [ и 0 проходили дальше как dict и давали
    # AttributeError вместо классифицированной ошибки провайдера.
    for payload in (b"[]", b"0", b'"image"'):
        with pytest.raises(lumen_images.ImageProviderError):
            asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
                status=200, chunks=(payload,))), "кот"))


def test_retry_after_accepts_http_date():
    # Retry-After по RFC бывает датой, а не секундами. Голое float() давало None, и
    # 402 без кода FREE_LIMIT ложно становился «бюджет выбран до завтра».
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=40)
    resp = _FakeResp(status=402, headers={"Retry-After": format_datetime(when)},
                    chunks=(b'{"error":{"code":"RATE","message":"slow down"}}',))
    with pytest.raises(lumen_images.ImageRateLimitedError) as exc:
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "кот"))
    assert 20 <= exc.value.retry_after_sec <= 60


def test_error_detail_shares_the_body_cap():
    # Ошибочная ветка читала resp.text() без капа: чужой сервис заливал в память
    # тело любого размера ровно тем путём, который правка закрывала.
    async def detail(chunks):
        resp = _FakeResp(status=402, headers={"Content-Type": "application/json"}, chunks=chunks)
        return await lumen_images._error_detail(resp)

    small = b'{"error":{"code":"FREE_LIMIT","message":"1 image every 45 seconds per IP"}}'
    assert "FREE_LIMIT" in asyncio.run(detail((small,)))
    assert asyncio.run(detail((b'{"error":{"code":"FREE_LIMIT","message":"' + b"x" * 5000 + b'"}',))) == ""
    # Не-ошибочный статус не читается вообще: тело здесь валидный JSON, иначе
    # проверка проходила бы и на «читаем всегда».
    assert asyncio.run(lumen_images._error_detail(
        _FakeResp(status=500, chunks=(b'{"error":{"code":"BOOM_MESSAGE"}}',)))) == ""


def test_cloudflare_error_shapes(cf_configured):
    # 429 с текстом про дневный бюджет — это «до завтра», а не «подожди минуту»:
    # путаница стоила бы каждому следующему человеку пустых попыток подряд.
    budget_body = json.dumps({"success": False, "errors": [
        {"code": 10025, "message": "You have used all of your free daily allocation"}]}).encode()
    with pytest.raises(lumen_images.ImageBudgetExhaustedError):
        asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
            status=429, chunks=(budget_body,))), "кот"))
    with pytest.raises(lumen_images.ImageRateLimitedError):
        asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
            status=429, headers={"Retry-After": "10"}, chunks=(b'{"success":false,"errors":[]}',))), "кот"))
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
            status=200, chunks=(json.dumps({"success": True, "result": {}}).encode(),))), "кот"))
