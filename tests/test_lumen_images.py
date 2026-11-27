"""test_lumen_images.py — unit tests for lumen_images.py: request contracts for both
image providers (Cloudflare primary, Pollinations fallback), parsing their errors and
selecting the response format.

lumen_images.py does not depend on Telegram or the bot's runtime state (the same principle as
lumen_formatting.py/lumen_router_config.py — see their test files), so it is
tested here directly (import lumen_images), without importing bot.py.

Run:
    pytest test_lumen_images.py -v
"""
import asyncio
import base64
import json
import urllib.parse
from types import SimpleNamespace

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
    """Logs both the URL and the request body: half of the audit findings are parameters
that silently stopped being passed (seed, safe, nologo). The response list
is returned in order — for the \"first request rejected, retry with a different payload\" branch."""

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


# ─────────────────────────── provider selection ───────────────────────────


def test_cloudflare_is_primary_only_when_configured(monkeypatch):
    # A typo/empty secret in env must switch the chain to Pollinations, not
    # a guaranteed error on the first provider.
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_API_TOKEN", "x")
    assert lumen_images.image_provider_names() == ("pollinations",)
    monkeypatch.setattr(lumen_images, "CLOUDFLARE_ACCOUNT_ID", "acct")
    assert lumen_images.image_provider_names() == ("cloudflare", "pollinations")


# ─────────────────────────── Pollinations contract ───────────────────────────


@pytest.mark.parametrize("prompt, expected", [
    ("кот 16:9", (1344, 768)),
    ("16х9 пейзаж", (1344, 768)),
    ("вертикальная картинка", (768, 1344)),
    ("portrait of a woman", (1024, 1024)),
    ("встреча в 13х45", (1024, 1024)),
    ("квадрат", (1024, 1024)),
    ("кот", (1024, 1024)),
    ("широкий спектр", (1024, 1024)),
    ("встреча в 13:45", (1024, 1024)),
    # Latin "x" instead of Cyrillic "kha" (U+0445): the marker was searched for only in Cyrillic,
    # and "16x9" silently became a square (review 10.11.2026).
    ("город 16x9", (1344, 768)),
    # Digit boundaries are mandatory: "13x45" contains "3x4".
    ("встреча в 13x45", (1024, 1024)),
    # Format from human wording and from the artifact type (parity 10.11.2026).
    ("котик 21:9", (1344, 576)),
    ("картинка 3:2", (1248, 832)),
    ("постер 3х2", (1248, 832)),
    ("портрет 4:5", (896, 1120)),
    ("widescreen cat", (1344, 768)),
    ("cat in tall format", (768, 1344)),
    ("панорамный закат", (1344, 768)),
    ("девушка в полный рост", (768, 1344)),
    ("постер с котом", (832, 1248)),
    ("обложка книги", (832, 1248)),
    ("обои на телефон с котом", (768, 1344)),
    ("сторис с котом", (768, 1344)),
    ("баннер для сайта", (1344, 768)),
    ("превью для youtube", (1344, 768)),
    ("логотип кофейни", (1024, 1024)),
    # The artifact default does not override an explicit aspect ratio.
    ("постер 16:9", (1344, 768)),
    # Single adjectives describe the scene, not the frame.
    ("wide smile", (1024, 1024)),
    ("tall hat on the table", (1024, 1024)),
    # Find the format in the artifact by word boundaries: "imposter" and "posterior view"
    # not posters; "a cat telling stories" and "fisherman with reels" describe
    # the plot, not a frame (review 10.11.2026).
    ("imposter among us", (1024, 1024)),
    ("posterior view of a car", (1024, 1024)),
    ("a cat telling stories", (1024, 1024)),
    ("a building with 5 stories", (1024, 1024)),
    ("fisherman with reels", (1024, 1024)),
    ("instagram stories with a cat", (768, 1344)),
    ("banner for a shop", (1344, 768)),
    # Latin variants of markers that were absent: 4x5 and 5x4 silently
    # became a square, as did "wallpaper for desktop" (review 10.11.2026).
    ("logo 4x5", (896, 1120)),
    ("аватарка 5x4", (1120, 896)),
    ("wallpaper for desktop", (1344, 768)),
    ("portrait format cat", (896, 1120)),
])
def test_detect_image_size(prompt, expected):
    # Use the same proportions as the ChatGPT generator; "wide range" must not become 16:9.
    assert lumen_images.detect_image_size(prompt) == expected


def _jpeg_header(width, height):
    """Minimal JPEG header: APP0, then SOF0 with dimensions — enough
    for the dimension parser without third-party libraries."""
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
    # 4:3 and 3:4 — the nearest aspect ratio, not "almost square" (review 10.11.2026).
    assert lumen_images.size_for_reference((1200, 900)) == (1152, 864)
    assert lumen_images.size_for_reference((900, 1200)) == (864, 1152)
    assert lumen_images.size_for_reference(None) == (1024, 1024)


def test_cloudflare_generation_passes_requested_size(cf_configured):
    captured = {}
    asyncio.run(cf_configured._cloudflare_text_to_image(
        _cf_ok_session(captured), "кот", width=1344, height=768))
    fields = {name: value for name, value, _ in captured["data"].fields}
    assert fields["width"] == "1344" and fields["height"] == "768"


def test_pollinations_url_carries_seed_safe_and_nologo(monkeypatch):
    # Measured 09.11.2026: without a seed, Pollinations returns seed=42 and the
    # byte-for-byte same
    # the file on a prompt retry, with the NSFW safe filter disabled.
    captured = {}
    asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_image_resp(), captured), "cat"))
    url = captured["url"]
    assert "safe=true" in url and "nologo=true" in url and "enhance=false" in url
    query = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert int(query["seed"][0]) > 0
    # R7-14 (review 17.11.2026): comparing two legitimate draws could normally collide;
    # patch the secrets seam in lumen_images and verify that every call gets a fresh
    # randbelow and puts exactly that value +1 in the URL, with no dependence on
    # luck.
    draws = iter([11, 22])
    monkeypatch.setattr(lumen_images, "secrets", SimpleNamespace(randbelow=lambda n: next(draws)))
    seeds = []
    for _ in range(2):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_image_resp(), captured), "cat"))
        seeds.append(int(urllib.parse.parse_qs(captured["url"].split("?", 1)[1])["seed"][0]))
    assert seeds == [12, 23]


def test_pollinations_truncates_prompt_to_cap():
    captured = {}
    prompt = "big cat " * 120
    asyncio.run(lumen_images._pollinations_text_to_image(
        _FakeSession(_image_resp(), captured), prompt, timeout_sec=7.5))
    path = captured["url"].split("/prompt/", 1)[1].split("?", 1)[0]
    assert path == urllib.parse.quote(prompt[:lumen_images.IMAGE_PROMPT_MAX_CHARS], safe="")
    assert "%20" in path
    assert captured["timeout"].total == 7.5
    assert captured["timeout"].sock_connect == 12.0


def test_pollinations_refuses_untranslated_cyrillic_prompt():
    # sana receives Cyrillic as '?' during URL parsing and draws random
    # (proof 09.11.2026); the request still consumed the daily budget.
    captured = {}
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(
            _FakeSession(_image_resp(), captured), "нарисуй кота"))
    assert "url" not in captured


def test_pollinations_free_limit_402_is_rate_limit_with_retry_after():
    # Actual Pollinations response on 09.11.2026: HTTP 402 FREE_LIMIT, 1 image per 45
    # seconds per IP. Previously, 402 matched no branch and the user read
    # "rephrase the description" instead of "the service is overloaded".
    body = json.dumps({"error": {"code": "FREE_LIMIT", "message": "Free limit reached: 1 image every 45 seconds per IP"}})
    resp = _FakeResp(status=402, headers={"Content-Type": "application/json", "Retry-After": "35"},
                    text=body, chunks=(body.encode(),))
    with pytest.raises(lumen_images.ImageRateLimitedError) as exc:
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "cat"))
    assert exc.value.retry_after_sec == 35.0
    assert "FREE_LIMIT" in str(exc.value)


def test_pollinations_402_without_retry_after_is_budget():
    body = json.dumps({"error": {"code": "INSUFFICIENT_POLLEN"}})
    resp = _FakeResp(status=402, text=body, chunks=(body.encode(),))
    with pytest.raises(lumen_images.ImageBudgetExhaustedError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "cat"))


def test_pollinations_429_and_500_are_classified_apart():
    with pytest.raises(lumen_images.ImageRateLimitedError):
        asyncio.run(lumen_images._pollinations_text_to_image(
            _FakeSession(_FakeResp(status=429, headers={"Retry-After": "7"})), "cat"))
    with pytest.raises(lumen_images.ImageProviderError) as exc:
        asyncio.run(lumen_images._pollinations_text_to_image(
            _FakeSession(_FakeResp(status=500)), "cat"))
    assert not isinstance(exc.value, lumen_images.ImageRateLimitedError)


# ─────────────────────────── response body ───────────────────────────


def test_accepts_jpeg_by_magic_bytes_when_content_type_lies():
    # Regression AUD-E-001: JPEG without an image/* Content-Type was rejected as a non-image.
    resp = _FakeResp(status=200, headers={"Content-Type": "application/octet-stream"}, chunks=(JPEG,))
    body = asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "cat"))
    assert body[:3] == b"\xff\xd8\xff"


def test_rejects_oversized_body(monkeypatch):
    # Content-Length lies, so recheck the stream as it arrives; tighten the cap,
    # otherwise the test would stream 30 MB.
    monkeypatch.setattr(lumen_images, "IMAGE_MAX_BYTES", 100)
    big = b"\x89PNG" + b"\x00" * 200
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_FakeResp(
            status=200, headers={"Content-Type": "image/png"}, chunks=(big,))), "cat"))
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(_FakeResp(
            status=200, headers={"Content-Type": "image/png", "Content-Length": "1000000"},
            chunks=(b"\x89PNG" + b"\x00" * 10,))), "cat"))


def test_rejects_format_sendphoto_cannot_take():
    # GIF magic passed as an "image", but sendPhoto rejected an animated GIF:
    # the error was visible to the person only as 'could not send'.
    resp = _FakeResp(status=200, headers={"Content-Type": "image/gif"}, chunks=(GIF,))
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "cat"))


def test_image_filename_follows_magic_bytes():
    assert lumen_images.image_filename(JPEG) == "generated.jpg"
    assert lumen_images.image_filename(PNG) == "generated.png"
    assert lumen_images.image_filename(b"RIFF\x00\x00\x00\x00WEBP") == "generated.webp"
    assert lumen_images.image_filename(b"RIFF\x00\x00\x00\x00ANIM") is None
    assert lumen_images.image_filename(GIF) is None


# ─────────────────────────── Cloudflare ───────────────────────────


class _FakeForm:
    """Builds multipart form fields: real aiohttp.FormData encodes the body
    at runtime, while the contract (field names, reference) is checked here."""

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
    # Measurement 10.11.2026: flux-2-klein accepts multipart only; seed and size
    # go as form fields, not JSON.
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
    # Editing with one model: the reference must travel in input_image_0 with
    # signature content-type, otherwise Cloudflare will ignore the image.
    captured = {}
    jpeg = JPEG
    asyncio.run(cf_configured._cloudflare_edit_to_image(_cf_ok_session(captured), "make it green", jpeg))
    fields = {name: (value, kw) for name, value, kw in captured["data"].fields}
    assert fields["prompt"][0] == "make it green"
    assert fields["input_image_0"][0] == jpeg
    assert fields["input_image_0"][1]["content_type"] == "image/jpeg"


def test_cloudflare_edit_sends_two_references(cf_configured):
    # Multi-image: the second reference must be sent in its own input_image_1 field.
    captured = {}
    asyncio.run(cf_configured._cloudflare_edit_to_image(
        _cf_ok_session(captured), "combine these two", b"one", second_reference=b"two"))
    fields = {name: (value, kw) for name, value, kw in captured["data"].fields}
    assert fields["input_image_0"][0] == b"one"
    assert fields["input_image_1"][0] == b"two"


def test_cloudflare_moderation_rejection_is_not_retryable(cf_configured):
    # Measurement 10.11.2026: an NSFW prompt gets 400 code 3030 "output has been
    # flagged". This is not overload: policy must not enable cooldown.
    body = json.dumps({"success": False, "errors": [
        {"code": 3030, "message": "AiError: Your output has been flagged. Please choose another prompt / input image combination"}]}).encode()
    with pytest.raises(lumen_images.ImageProviderError) as exc:
        asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
            status=400, chunks=(body,))), "кот"))
    # The error class is the contract: with bare substring checks, the test would
    # also pass with
    # ordinary ImageProviderError (10.11.2026 review).
    assert isinstance(exc.value, lumen_images.ImageModerationError)
    assert not isinstance(exc.value, (lumen_images.ImageRateLimitedError, lumen_images.ImageBudgetExhaustedError))


def test_cloudflare_rejects_unconfigured_without_request(cf_configured, monkeypatch):
    # Without a key, the request must not reach the network: detect this via an empty captured.
    monkeypatch.setattr(cf_configured, "CLOUDFLARE_API_TOKEN", "")
    captured = {}
    with pytest.raises(lumen_images.ImageProviderError):
        asyncio.run(cf_configured._cloudflare_text_to_image(_cf_ok_session(captured), "кот"))
    assert captured == {}


def test_cloudflare_rejects_non_object_json(cf_configured):
    # Valid JSON, but not an object: [ and 0 passed through as dict and caused
    # AttributeError instead of a classified provider error.
    for payload in (b"[]", b"0", b'"image"'):
        with pytest.raises(lumen_images.ImageProviderError):
            asyncio.run(cf_configured._cloudflare_text_to_image(_FakeSession(_FakeResp(
                status=200, chunks=(payload,))), "кот"))


def test_retry_after_accepts_http_date():
    # Retry-After can be a date rather than seconds under RFC. A bare float() returned None,
    # and a 402 without the FREE_LIMIT code falsely became "budget exhausted until tomorrow".
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=40)
    resp = _FakeResp(status=402, headers={"Retry-After": format_datetime(when)},
                    chunks=(b'{"error":{"code":"RATE","message":"slow down"}}',))
    with pytest.raises(lumen_images.ImageRateLimitedError) as exc:
        asyncio.run(lumen_images._pollinations_text_to_image(_FakeSession(resp), "cat"))
    assert 20 <= exc.value.retry_after_sec <= 60


def test_error_detail_shares_the_body_cap():
    # The error branch read resp.text() without a cap: an external service filled memory
    # a body of any size through exactly the path the fix closed.
    async def detail(chunks):
        resp = _FakeResp(status=402, headers={"Content-Type": "application/json"}, chunks=chunks)
        return await lumen_images._error_detail(resp)

    small = b'{"error":{"code":"FREE_LIMIT","message":"1 image every 45 seconds per IP"}}'
    assert "FREE_LIMIT" in asyncio.run(detail((small,)))
    assert asyncio.run(detail((b'{"error":{"code":"FREE_LIMIT","message":"' + b"x" * 5000 + b'"}',))) == ""
    # A non-error status is not read at all: the body here is valid JSON, otherwise
    # the check would also pass on “always read.”
    assert asyncio.run(lumen_images._error_detail(
        _FakeResp(status=500, chunks=(b'{"error":{"code":"BOOM_MESSAGE"}}',)))) == ""


def test_cloudflare_error_shapes(cf_configured):
    # A 429 mentioning the daily budget means "until tomorrow," not "wait a minute":
    # the confusion would cost every subsequent person a run of empty attempts.
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



def test_cloudflare_moderation_flag_at_http_200_is_terminal(cf_configured):
    # The Workers AI wrapper can return a refusal in the body with HTTP 200: without parsing
    # success:false does not make such a response terminal, and sana would finish drawing the blocked
    # content (review 10.11.2026).
    body = json.dumps({"success": False, "errors": [
        {"code": 3030, "message": "Your output has been flagged"}]}).encode()
    with pytest.raises(lumen_images.ImageModerationError):
        asyncio.run(cf_configured._cloudflare_image_result(200, None, json.loads(body)))


def test_cloudflare_plain_failure_at_http_200_stays_retryable(cf_configured):
    # Alongside it is an ordinary wrapper error: it is NOT moderation, and the chain must
    # leave it active (review 10.11.2026).
    with pytest.raises(lumen_images.ImageProviderError) as exc:
        asyncio.run(cf_configured._cloudflare_image_result(200, None, {"success": False, "error": "boom"}))
    assert not isinstance(exc.value, lumen_images.ImageModerationError)



def test_cloudflare_moderation_flag_in_messages_array_is_terminal(cf_configured):
    # The Workers AI wrapper puts refusal text in messages[]/result, not errors[]:
    # without these fields, detail was empty, the refusal counted as an ordinary error, and the reserve
    # sana completed the blocked content (review 10.11.2026).
    payload = {"success": False, "messages": [{"code": 3030, "message": "Your output has been flagged"}]}
    with pytest.raises(lumen_images.ImageModerationError):
        asyncio.run(cf_configured._cloudflare_image_result(200, None, payload))


def test_error_strings_never_carries_the_image_blob(cf_configured):
    # detail goes to the log and to user-visible text: the base64 image from result must not
    # land neither there nor in the moderation markers (review 10.11.2026).
    blob = "A" * 4096
    assert lumen_images._error_strings({"success": True, "result": {"image": blob}}) == ""
    assert lumen_images._error_strings({"success": True, "result": blob}) == ""
    assert lumen_images._error_strings({"result": "Model not available"}) == "Model not available"
    # result dictionary: keep short captions, not the image or long values.
    assert lumen_images._error_strings({"result": {"image": blob, "note": "busy"}}) == "busy"


@pytest.mark.parametrize("payload", [
    {"errors": 7},
    {"errors": {"code": [], "message": {}}},
    {"error": {"code": [], "message": {}}},
    {"message": []},
    {"messages": 7},
    {"messages": {"code": {}, "message": []}},
    {"result": 7},
    {"result": {"image": "A" * 4096, "note": [], "code": {}, "message": 123}},
])
def test_error_strings_ignores_malformed_containers_and_scalars(payload):
    # R4-09: a provider error must not leak as a TypeError because of a field's type.
    assert lumen_images._error_strings(payload) == ""
    assert len(lumen_images._error_strings(payload, cap=10)) <= 10


def test_error_strings_accepts_single_error_object_and_keeps_detail_cap():
    # An erroneous object instead of errors[] still carries a useful message, but
    # a list in code is not interpolated, and the overall cap stays unchanged.
    detail = "x" * 1000
    assert lumen_images._error_strings(
        {"errors": {"code": [3030], "message": detail}}, cap=17,
    ) == detail[:17]


@pytest.mark.parametrize("payload", [
    {
        "success": False,
        "errors": {"code": [3030], "message": "Your output has been flagged"},
        "messages": 7,
        "result": {"image": "A" * 4096, "note": []},
    },
    {
        "success": False,
        "errors": 7,
        "messages": {"code": {}, "message": "flagged by moderation"},
        "result": 123,
    },
])
def test_malformed_moderation_payload_with_marker_is_terminal(cf_configured, payload):
    # R4-09: a valid marker among corrupted fields must stop the fallback,
    # and base64 from result must appear neither in detail nor in the exception.
    blob = payload.get("result", {}).get("image") if isinstance(payload.get("result"), dict) else None
    with pytest.raises(lumen_images.ImageModerationError) as exc:
        asyncio.run(cf_configured._cloudflare_image_result(500, None, payload))
    if blob:
        assert blob not in str(exc.value)


def test_cloudflare_moderation_flag_in_result_dict_at_odd_status_is_terminal(cf_configured):
    # The Workers AI wrapper carries refusal text in result{} as well, while the relay rewrites
    # status: without both, the failure remained an "ordinary provider error", the chain
    # reached sana, which drew the rejected content (review 10.11.2026).
    payload = {"success": False, "result": {"error": "flagged by moderation"}}
    with pytest.raises(lumen_images.ImageModerationError):
        asyncio.run(cf_configured._cloudflare_image_result(500, None, payload))
