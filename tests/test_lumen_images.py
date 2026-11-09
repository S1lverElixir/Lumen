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
    выдаётся по порядку — для ветки «первый запрос отвергли, повтор другой пайлоад»."""

    def __init__(self, resp, captured=None):
        self.resps = list(resp) if isinstance(resp, (list, tuple)) else [resp]
        self.captured = captured if captured is not None else {}
        self.calls: list[dict[str, Any]] = []

    def _take(self, method, url, payload, timeout, headers):
        self.calls.append({"method": method, "url": url, "payload": payload, "timeout": timeout, "headers": headers})
        self.captured.update(method=method, url=url, payload=payload, timeout=timeout, headers=headers)
        return self.resps.pop(0) if len(self.resps) > 1 else self.resps[0]

    def get(self, url, timeout=None, headers=None):
        return self._take("GET", url, None, timeout, headers)

    def post(self, url, *, json=None, timeout=None, headers=None):
        return self._take("POST", url, json, timeout, headers)


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


@pytest.fixture
def cf_configured(monkeypatch):
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_ACCOUNT_ID", "acct-id")
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_API_TOKEN", "cf-token")
    return lumen_images


def _cf_ok_session(captured=None):
    return _FakeSession(_FakeResp(status=200, chunks=(json.dumps(
        {"success": True, "result": {"image": base64.b64encode(JPEG).decode()}}).encode(),)), captured)


def test_cloudflare_posts_prompt_and_seed(cf_configured):
    captured = {}
    asyncio.run(cf_configured._cloudflare_text_to_image(_cf_ok_session(captured), "кот"))
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.cloudflare.com/client/v4/accounts/acct-id/ai/run/@cf/black-forest-labs/flux-1-schnell"
    assert captured["headers"]["Authorization"] == "Bearer cf-token"
    assert captured["payload"]["prompt"] == "кот"
    assert captured["payload"]["seed"] > 0


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


def test_cloudflare_retries_without_seed_when_payload_rejected(cf_configured):
    # Схема модели знает только prompt/steps, страница модели в примерах шлёт и seed.
    # Если Cloudflare отвергнет лишее 400-м, повтор голым prompt обязан спасти фичу,
    # а не оставить пользователя с «ошибкой генерации» на каждый /draw.
    rejected = _FakeResp(status=400, chunks=(json.dumps({"success": False, "errors": [
        {"code": 10000, "message": "Invalid input: seed is not a supported parameter"}]}).encode(),))
    ok = _FakeResp(status=200, chunks=(json.dumps(
        {"success": True, "result": {"image": base64.b64encode(JPEG).decode()}}).encode(),))
    captured = {}
    session = _FakeSession([rejected, ok], captured)
    body = asyncio.run(cf_configured._cloudflare_text_to_image(session, "кот"))
    assert body[:3] == b"\xff\xd8\xff"
    assert len(session.calls) == 2
    assert "seed" in session.calls[0]["payload"]
    assert "seed" not in session.calls[1]["payload"]


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
