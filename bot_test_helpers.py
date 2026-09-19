"""
bot_test_helpers.py — общие фейки и харнесы для test_bot_*.py (вынесено из test_bot.py, P2 аудита). Не содержит тестов.
"""
from types import SimpleNamespace
import asyncio
import bot
import lumen_telegram_transport


class _FakeResolveResponse:
    def __init__(self, status: int, url: str):
        self.status = status
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeResolveSession:
    """Мокает session.head()/session.get() для _resolve_tiktok_short."""
    def __init__(self, head_response, get_response):
        self._head_response = head_response
        self._get_response = get_response
        self.get_called = False

    def head(self, url, *args, **kwargs):
        return self._head_response

    def get(self, url, *args, **kwargs):
        self.get_called = True
        return self._get_response


class _FakeExc(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


class _FakeStatusExc(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


class _FakeAdminRequest:
    def __init__(self, headers=None, query_params=None):
        self.headers = headers or {}
        self.query_params = query_params or {}


class _FakeVoiceBot:
    """Минимальная замена aiogram Bot для inline_tts — нужен только send_voice."""
    def __init__(self):
        self.sent_voice: dict | None = None

    async def send_voice(self, **kwargs):
        self.sent_voice = kwargs
        return SimpleNamespace()


class _FakeInlineData:
    def __init__(self, data, mime_type):
        self.data = data
        self.mime_type = mime_type


class _FakePart:
    def __init__(self, inline_data):
        self.inline_data = inline_data


class _FakeTTSContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeTTSCandidate:
    def __init__(self, content):
        self.content = content


class _FakeTTSResponse:
    def __init__(self, candidates):
        self.candidates = candidates


def _fake_tts_response(wav_bytes: bytes) -> "_FakeTTSResponse":
    return _FakeTTSResponse(candidates=[
        _FakeTTSCandidate(_FakeTTSContent(parts=[_FakePart(_FakeInlineData(wav_bytes, "audio/wav"))]))
    ])


def _run_core_capturing_prompt(message, monkeypatch):
    captured = {}

    async def fake_run_route(chat_id, ai_prompt, route, message, **kwargs):
        captured["prompt"] = ai_prompt
        return "ok", False

    monkeypatch.setattr(bot, "_run_route", fake_run_route)
    asyncio.run(bot._handle_message_core(message))
    return captured.get("prompt", "")


class _FakeCandidate:
    def __init__(self, finish_reason="STOP", content=None):
        self.finish_reason = finish_reason
        self.content = content


class _FakeGeminiResponse:
    def __init__(self, text="", candidates=None):
        self._text = text
        self.candidates = candidates or []

    @property
    def text(self):
        return self._text


class _FakeSentMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("parse_mode")))
        return self

    async def delete(self):
        self.deleted = True


class _FakeChat:
    def __init__(self, chat_id, chat_type):
        self.id = chat_id
        self.type = chat_type


class _FakeIncomingMessage:
    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id, bot.ChatType.PRIVATE)
        self.reply_to_message = None
        self.sent: list[_FakeSentMessage] = []

    async def reply(self, text, **kwargs):
        msg = _FakeSentMessage()
        self.sent.append(msg)
        return msg


class _FakeAudioBot:
    """Минимальная замена aiogram Bot для _send_tiktok_music — нужен только send_audio."""
    def __init__(self):
        self.sent_audio: dict | None = None

    async def send_audio(self, **kwargs):
        self.sent_audio = kwargs
        return SimpleNamespace()


def _run_send_tiktok_music(media_data: dict, language_code: str | None = "ru"):
    """Общий harness: мокает скачивание байтов и запись MP3-тегов, возвращает
    (title, performer), которые реально дошли бы до _write_mp3_tags/send_audio."""
    incoming = _FakeIncomingMessage(999601)
    incoming.message_id = 12345
    incoming.from_user = SimpleNamespace(language_code=language_code) if language_code is not None else None

    captured: dict = {}

    async def fake_download(session, url, headers=None):
        return b"fake-bytes"

    def fake_write_tags(path, title, artist, cover):
        captured["title"] = title
        captured["artist"] = artist

    original_download = bot._download_url_bin
    original_write_tags = bot._write_mp3_tags
    original_bot = bot.bot
    bot._download_url_bin = fake_download
    bot._write_mp3_tags = fake_write_tags
    bot.bot = _FakeAudioBot()
    try:
        asyncio.run(bot._send_tiktok_music(None, media_data, incoming, "VideoPosterNickname", {}))
    finally:
        bot._download_url_bin = original_download
        bot._write_mp3_tags = original_write_tags
        bot.bot = original_bot
    return captured.get("title"), captured.get("artist")


class _FakeAsyncLineIter:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeSSEResponse:
    def __init__(self, lines: list[bytes], status: int = 200):
        self.status = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        return b""

    @property
    def content(self):
        return _FakeAsyncLineIter(self._lines)


class _FakeSessionForSSE:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *args, **kwargs):
        return self._resp


class _FakeWebhookRequest:
    def __init__(self, headers: dict | None = None, body: dict | None = None):
        self.headers = headers or {}
        self._body = body or {}

    async def json(self):
        return self._body


async def _run_webhook_handler(req: "_FakeWebhookRequest") -> dict:
    """asyncio.create_task внутри webhook_handler запускает обработку апдейта не
    дожидаясь её завершения — один await asyncio.sleep(0) после вызова даёт
    планировщику шанс выполнить уже запланированную задачу (фейковый обработчик
    ниже не делает собственных await, поэтому этого достаточно для детерминизма)."""
    result = await bot.webhook_handler(req)
    await asyncio.sleep(0)
    return result


class _FakeOwnerBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace()


class _FakeTikTokBot:
    def __init__(self):
        self.sent_videos: list[dict] = []

    async def send_video(self, **kwargs):
        self.sent_videos.append(kwargs)
        return SimpleNamespace()

    async def send_audio(self, **kwargs):
        return SimpleNamespace()


class _FakeTikTokResponse:
    def __init__(self, status=200, json_body=None):
        self.status = status
        self._json_body = json_body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._json_body


class _FakeTikTokSession:
    """Мокает aiohttp.ClientSession для целого handle_tiktok: session.get() отдаёт
    ответ TikWM API. _download_url_bin/_resolve_tiktok_short патчатся отдельно
    (они принимают сессию, но не обязаны реально использовать её методы)."""
    def __init__(self, tikwm_json: dict):
        self._tikwm_json = tikwm_json

    def get(self, url, *args, **kwargs):
        if "tikwm.com/api" in url:
            return _FakeTikTokResponse(200, self._tikwm_json)
        return _FakeTikTokResponse(200, {})


class _FakeTikwmApiResponse:
    def __init__(self, status=200, json_body=None, body_bytes=b""):
        self.status = status
        self._json_body = json_body or {}
        self._body_bytes = body_bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._json_body

    async def read(self):
        return self._body_bytes


class _FakeTikwmApiSession:
    """session.get(url) — очередь заранее заготовленных ответов (по одному на
    каждый вызов, в порядке вызова) плюс список реально запрошенных URL/заголовков —
    тесты ниже проверяют и содержимое ответов, и сам порядок/число обращений, и
    заголовки, с которыми ушёл каждый запрос."""
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.requested_urls: list[str] = []
        self.requested_headers: list[dict] = []

    def get(self, url, *args, **kwargs):
        self.requested_urls.append(url)
        self.requested_headers.append(kwargs.get("headers") or {})
        return self._responses.pop(0) if self._responses else _FakeTikwmApiResponse(status=500)


class _FakeDownloadContent:
    """Мокает resp.content.iter_chunked(n) — асинхронный генератор чанков байт,
    как у настоящего aiohttp.StreamReader."""
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def iter_chunked(self, n: int):
        chunks = self._chunks

        async def _gen():
            for c in chunks:
                yield c
        return _gen()


class _FakeDownloadResponse:
    def __init__(self, chunks: list[bytes], *, status: int = 200, content_length: str | None = None):
        self.status = status
        self.headers = {"Content-Length": content_length} if content_length is not None else {}
        self.content = _FakeDownloadContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeDownloadSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, *args, **kwargs):
        return self._resp


class _FakeProc:
    def __init__(self, *, hang: bool = False, fail_communicate: bool = False):
        self._hang = hang
        self._fail_communicate = fail_communicate
        self.killed = False
        self.kill_count = 0
        self.wait_count = 0
        self.returncode = None

    async def communicate(self):
        if self._fail_communicate:
            raise RuntimeError("pipe broken")
        while self._hang and not self.killed:
            await asyncio.sleep(0.01)
        return b"out", b"err"

    def kill(self):
        self.kill_count += 1
        self.killed = True
        self.returncode = -9
        self._hang = False

    async def wait(self):
        self.wait_count += 1
        return self.returncode


def _run_proxy_middleware(url, *, secret="proxy-secret-abc", bases=("https://proxy.example/fetch/api.telegram.org",), headers=None):
    from multidict import CIMultiDict
    from yarl import URL

    (authenticate,) = lumen_telegram_transport.proxy_auth_middlewares(
        proxy_secret=secret, proxy_base_urls=bases,
    )
    seen = {}

    class _FakeRequest:
        def __init__(self):
            self.url = URL(url)
            self.headers = CIMultiDict(headers or {})

    async def _handler(request):
        seen["sent"] = dict(request.headers)
        info_headers = CIMultiDict(request.headers)

        class _FakeResponse:
            status = 200

            def __init__(self):
                self.request_info = SimpleNamespace(
                    url=request.url, method="GET", headers=info_headers, real_url=request.url,
                )
                self.closed = False

            def close(self):
                self.closed = True

        return _FakeResponse()

    request = _FakeRequest()
    response = asyncio.run(authenticate(request, _handler))
    return request, response, seen


class _FakeQueryMessage:
    def __init__(self, chat_id=777, message_id=55):
        self.chat = SimpleNamespace(id=chat_id, type=bot.ChatType.PRIVATE)
        self.message_id = message_id
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self


def _make_pick_query(data, user_id=111, msg=None):
    q = SimpleNamespace()
    q.data = data
    q.from_user = SimpleNamespace(id=user_id)
    q.message = msg if msg is not None else _FakeQueryMessage()
    q.answered = []

    async def answer(text=None, show_alert=False):
        q.answered.append((text, show_alert))

    q.answer = answer
    return q


class _FakeRichMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False
        self.chat = SimpleNamespace(id=777)
        self.message_id = 42

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("parse_mode")))
        return self


class _FakeRichBot:
    """Бот с рич-методами: фиксирует rich_message.html и возвращает НАСТОЯЩИЙ
    aiogram Message (нужен isinstance-гейт в хелперах)."""
    def __init__(self):
        from aiogram.types import Chat as TgChat
        from aiogram.types import Message as TgMessage
        self.rich_sent = []
        self.rich_edited = []
        self._TgMessage = TgMessage
        self._chat = TgChat(id=777, type="private")

    def _real_message(self, message_id):
        import datetime
        return self._TgMessage(message_id=message_id, date=datetime.datetime.now(), chat=self._chat)

    async def send_rich_message(self, **kwargs):
        self.rich_sent.append(kwargs)
        return self._real_message(43)

    async def edit_message_text(self, **kwargs):
        self.rich_edited.append(kwargs)
        return self._real_message(kwargs.get("message_id", 42))


class _FakeRichFailingBot:
    async def send_rich_message(self, **kwargs):
        raise RuntimeError("rich not supported here")

    async def edit_message_text(self, **kwargs):
        raise RuntimeError("rich not supported here")


def _make_lang_query(data, chat_type=None, user_id=111):
    q = _make_pick_query(data, user_id=user_id)
    q.message.chat.type = chat_type or bot.ChatType.PRIVATE
    return q

