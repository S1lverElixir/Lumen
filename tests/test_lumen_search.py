"""
test_lumen_search.py — юнит-тесты на lumen_search.py: парсер lite-выдачи DuckDuckGo,
блок сниппетов в промпт, детект живого поиска в маршруте, килл-свитч и фолбэк
fetch на [] без сети.

Что защищают: инцидент 11.10.2026 — свежий запрос при мёртвой квоте 2.5 отвечал
по памяти, будто искал. Парсер тестируется статичной фикстурой (без сети), fetch —
стабом сессии (граница I/O, не перереализация поведения).

Запуск:
    pytest tests/test_lumen_search.py -v
"""
import asyncio
from types import SimpleNamespace

import aiohttp
import lumen_search


_DDG_FIXTURE = """
<html><body><table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews&amp;rut=abc">Example news</a></td></tr>
<tr><td class='result-snippet'>Fresh snippet text here</td></tr>
<tr><td><a rel="nofollow" href="https://direct.example.org/page">Direct <b>title</b></a></td></tr>
<tr><td class='result-snippet'>Second snippet</td></tr>
<tr><td><a rel="nofollow" href="/internal/path">Internal link</a></td></tr>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">Example news again</a></td></tr>
<tr><td class='result-snippet'>Duplicate url snippet</td></tr>
</table></body></html>
"""


def test_parse_ddg_lite_html_unwraps_redirect_and_pairs_snippets():
    out = lumen_search.parse_ddg_lite_html(_DDG_FIXTURE)
    assert out[0]["url"] == "https://example.com/news"
    assert out[0]["title"] == "Example news"
    assert out[0]["snippet"] == "Fresh snippet text here"
    assert out[1]["url"] == "https://direct.example.org/page"
    assert out[1]["title"] == "Direct title"
    # Внутренняя ссылка и дубль целевого URL отсечены.
    assert all("duckduckgo.com" not in item["url"] for item in out)
    assert len({item["url"] for item in out}) == len(out) == 2


def test_parse_ddg_lite_html_empty_page_gives_empty_list():
    assert lumen_search.parse_ddg_lite_html("") == []
    assert lumen_search.parse_ddg_lite_html("<html></html>") == []


def test_format_snippets_for_prompt_marks_data_and_skips_empties():
    block = lumen_search.format_snippets_for_prompt([
        {"title": "T", "url": "https://example.com", "snippet": "S"},
        {"title": "", "url": "", "snippet": ""},
    ])
    assert "untrusted" in block and "not instructions" in block
    assert "https://example.com" in block
    assert lumen_search.format_snippets_for_prompt([]) == ""
    assert lumen_search.format_snippets_for_prompt([{"title": "", "url": "", "snippet": ""}]) == ""


def test_format_snippets_for_prompt_cuts_on_whole_lines():
    # Ревью: срез на 1500 символах рвал URL посередине — режем по целым строкам.
    long_snippets = [
        {"title": f"Title {i} " + "x" * 400, "url": f"https://example.com/{i}", "snippet": "y" * 400}
        for i in range(10)
    ]
    block = lumen_search.format_snippets_for_prompt(long_snippets)
    assert len(block) <= lumen_search._BLOCK_MAX_CHARS
    assert block.endswith(")")
    assert "https://example.com/9" not in block


def test_route_has_live_search_follows_grounding_flags():
    assert lumen_search.route_has_live_search([("groq", "qwen/qwen3.8-27b")]) is False
    assert lumen_search.route_has_live_search([("gemini", "gemini-3.8-flash")]) is False
    assert lumen_search.route_has_live_search([("gemini", "gemini-2.5-flash")]) is True
    assert lumen_search.route_has_live_search([
        ("groq", "qwen/qwen3.8-27b"), ("gemini", "gemini-2.5-flash-lite"),
    ]) is True


def test_web_search_kill_switch_and_limits(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")
    assert lumen_search.web_search_is_enabled() is False
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
    assert lumen_search.web_search_is_enabled() is True
    monkeypatch.setenv("WEB_SEARCH_TIMEOUT_SEC", "мусор")
    assert lumen_search.web_search_timeout_sec() == 8.0
    monkeypatch.setenv("WEB_SEARCH_MAX_RESULTS", "3")
    assert lumen_search.web_search_max_results() == 3


class _StubResp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._text

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")


class _StubSession:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._resp

    def get(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _stub_aiohttp(resp=None, exc=None):
    return SimpleNamespace(
        ClientSession=lambda *args, **kwargs: _StubSession(resp, exc),
        ClientTimeout=aiohttp.ClientTimeout,
    )


class _RoutedSession:
    """Разные страницы по URL: DDG пустой, RSS-источники — со своими фикстурами."""

    def __init__(self, pages):
        self._pages = pages
        self.requested = []

    def _resp(self, url):
        self.requested.append(url)
        return _StubResp(200, self._pages.get(url, "<html></html>"))

    def post(self, url, *args, **kwargs):
        return self._resp(url)

    def get(self, url, *args, **kwargs):
        return self._resp(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


_RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel>
<item><title>Свежая новость</title><link>https://example.com/fresh</link>
<description>Текст свежей новости</description></item>
<item><title>Дубль</title><link>https://example.com/fresh</link>
<description>Тот же адрес</description></item>
<item><title>Без ссылки</title><description>мусор</description></item>
</channel></rss>"""


def test_parse_rss_snippets_reads_items_and_drops_duplicates():
    out = lumen_search.parse_rss_snippets(_RSS_FIXTURE)
    assert out == [{"title": "Свежая новость", "url": "https://example.com/fresh",
                    "snippet": "Текст свежей новости"}]
    assert lumen_search.parse_rss_snippets("<rss><broken") == []


def test_fetch_web_snippets_falls_back_to_rss_when_ddg_returns_empty(monkeypatch):
    # Прод 28.10.2026: DuckDuckGo с адресов Space отдавал страницу без выдачи,
    # и бот отвечал «поиск ничего не дал» — RSS-фолбэк обязан дать сниппеты.
    session = _RoutedSession({
        lumen_search._BING_SEARCH_URL: _RSS_FIXTURE,
        lumen_search._GOOGLE_NEWS_URL: "",
    })
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *args, **kwargs: session,
                        ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("свежие новости"))
    assert [item["url"] for item in out] == ["https://example.com/fresh"]
    assert lumen_search._BING_SEARCH_URL in session.requested


def test_fetch_web_snippets_parses_stubbed_page(monkeypatch):
    monkeypatch.setattr(lumen_search, "aiohttp", _stub_aiohttp(_StubResp(200, _DDG_FIXTURE)))
    out = asyncio.run(lumen_search.fetch_web_snippets("что с ДЦ Яндекса"))
    assert [item["url"] for item in out] == ["https://example.com/news", "https://direct.example.org/page"]


def test_fetch_web_snippets_fails_open_on_network_error(monkeypatch):
    # Любая сетевая ошибка — пустой список, вызывающий код ставит честную пометку, а не падает.
    monkeypatch.setattr(lumen_search, "aiohttp", _stub_aiohttp(exc=ConnectionError("down")))
    assert asyncio.run(lumen_search.fetch_web_snippets("что с ДЦ Яндекса")) == []
    assert asyncio.run(lumen_search.fetch_web_snippets("   ")) == []


def test_answered_with_live_search_checks_actual_model_not_route():
    # Прод 12.10.2026: в плане маршрута граундинг-модель была, но она умерла
    # на квоте и ответила модель без поиска — проверять надо ответившую.
    assert lumen_search.answered_with_live_search({"provider": "gemini", "model": "gemini-2.5-flash"}) is True
    assert lumen_search.answered_with_live_search({"provider": "gemini", "model": "gemini-3.8-flash"}) is False
    assert lumen_search.answered_with_live_search({"provider": "groq", "model": "anything"}) is False
    assert lumen_search.answered_with_live_search({"provider": "gemini", "model": None}) is False
    assert lumen_search.answered_with_live_search({}) is False
    assert lumen_search.answered_with_live_search(None) is False
