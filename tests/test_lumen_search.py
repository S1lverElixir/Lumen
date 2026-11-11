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
import logging
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


def test_google_news_query_adds_freshness_filters():
    assert lumen_search.google_news_query("главные новости технологий сегодня").endswith(" when:1d")
    assert lumen_search.google_news_query("новости технологий").endswith(" when:7d")
    assert lumen_search.google_news_query("что с ДЦ Яндекса") == "что с ДЦ Яндекса"


def test_parse_rss_snippets_includes_pub_date():
    # Дата публикации нужна, чтобы модель могла подтвердить «за сегодня».
    xml = ("<rss><channel><item><title>T</title><link>https://example.com/n</link>"
           "<description>S</description><pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate>"
           "</item></channel></rss>")
    out = lumen_search.parse_rss_snippets(xml)
    assert out[0]["date"] == "2026-10-28"
    assert out[0]["snippet"] == "S"


def test_fetch_web_snippets_merges_google_news_for_news_query(monkeypatch):
    # Прод 28.10.2026: для новостей DDG отдавал общие страницы без дат, а Google
    # News не спрашивался, потому что DDG уже что-то вернул. Теперь источники
    # смешиваются, Google — первым.
    session = _RoutedSession({
        lumen_search._DDG_LITE_URL: _DDG_FIXTURE,
        lumen_search._GOOGLE_NEWS_URL: _RSS_FIXTURE,
    })
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *args, **kwargs: session,
                        ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("свежие новости"))
    urls = [item["url"] for item in out]
    assert urls[0] == "https://example.com/fresh"
    assert "https://example.com/news" in urls
    assert lumen_search._GOOGLE_NEWS_URL in session.requested


def test_format_snippets_for_prompt_grounds_answers_and_bans_guessing():
    # Прод 28.10.2026: слабая резервная модель гадала имена/должности, хотя
    # нужного факта в сниппетах не было. Промпт обязан запрещать догадки и
    # не отменять живой поиск grounding-модели.
    block = lumen_search.format_snippets_for_prompt(
        [{"title": "T", "url": "https://example.com", "snippet": "S"}]
    )
    assert "live search results" in block
    assert "Do not invent" in block
    assert "say plainly that the sources do not contain it" in block


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
    assert [item["url"] for item in out] == ["https://example.com/fresh"]
    assert out[0]["title"] == "Свежая новость"
    assert out[0]["kind"] == "web"
    assert lumen_search.parse_rss_snippets("<rss><broken") == []


def test_fetch_web_snippets_falls_back_to_rss_when_ddg_returns_empty(monkeypatch, caplog):
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
    with caplog.at_level(logging.INFO, logger="bot"):
        out = asyncio.run(lumen_search.fetch_web_snippets("что с ДЦ Яндекса"))
    assert [item["url"] for item in out] == ["https://example.com/fresh"]
    assert lumen_search._BING_SEARCH_URL in session.requested
    # Успешная выдача логируется источником и числом сниппетов.
    assert "bing_web=1" in caplog.text


def test_fetch_web_snippets_parses_stubbed_page(monkeypatch, caplog):
    monkeypatch.setattr(lumen_search, "aiohttp", _stub_aiohttp(_StubResp(200, _DDG_FIXTURE)))
    with caplog.at_level(logging.INFO, logger="bot"):
        out = asyncio.run(lumen_search.fetch_web_snippets("что с ДЦ Яндекса"))
    assert [item["url"] for item in out] == ["https://example.com/news", "https://direct.example.org/page"]
    assert "duckduckgo=2" in caplog.text


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


# ── Новый слой поиска (замер 11.11.2026): формы запроса, слои источников, цитаты ──

# Реальная страница-аномалия DDG lite: 14 КБ оболочки, ни одной ссылки на выдачу.
_DDG_ANOMALY_FIXTURE = """<!DOCTYPE html><html lang="en"><head><title>DuckDuckGo</title>
<link rel="icon" href="//duckduckgo.com/favicon.ico"/></head><body>
<div class="anomaly-modal">If this persists, please help us understand the problem.</div>
<a class="header-url" href="/lite/">DuckDuckGo</a></body></html>"""

_DDG_HTML_FIXTURE = """<html><body>
<div class="result results_links">
<div class="result__body">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Freview">Лучшие процессоры 2026</a>
<a class="result__snippet" href="https://example.org/review">Тест восьми CPU в играх, лидер — Ryzen 7 9800X3D.</a>
</div></div>
<div class="result results_links">
<div class="result__body">
<a class="result__a" href="https://direct.example.net/a">Второй результат</a>
<a class="result__snippet" href="https://direct.example.net/a">Описание второго.</a>
</div></div></body></html>"""

_GNEWS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Ставку сохранили</title>
<link>https://news.google.com/rss/articles/CBMiEXAMPLE?oc=5</link>
<pubDate>Thu, 05 Nov 2026 14:02:54 GMT</pubDate>
<description>Ставку сохранили Центральный Банк России</description>
<source url="https://www.cbr.ru">Центральный Банк России</source></item>
<item><title>Дубль того же сюжета</title>
<link>https://news.google.com/rss/articles/CBMiEXAMPLE?oc=5</link>
<source url="https://www.cbr.ru">Центральный Банк России</source></item>
</channel></rss>"""

_BING_NEWS_FIXTURE = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title>Коты и ключевая ставка</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;tid=abc&amp;url=https%3a%2f%2flenta.ru%2fnews%2f2026%2d11%2d11%2fsignal%2f&amp;c=123&amp;mkt=ru-ru</link>
<pubDate>Tue, 10 Nov 2026 18:07:00 GMT</pubDate><description>ЦБ сохранил ставку</description></item>
</channel></rss>"""


def test_clean_search_text_drops_greeting_and_politeness():
    # Замер 11.11.2026: «Привет! Подскажи, пожалуйста, а кто возглавляет ЦБ…» уводило
    # движок в Викисловарь и Грамоту по слову «привет» — обращение не должно попадать в запрос.
    cleaned = lumen_search.clean_search_text(
        "Привет! Подскажи, пожалуйста, а кто сейчас возглавляет Центральный банк России?"
    )
    assert "привет" not in cleaned.lower()
    assert "пожалуйста" not in cleaned.lower()
    assert "возглавляет" in cleaned
    assert cleaned.startswith("кто")


def test_plan_search_queries_gives_core_and_keyword_forms():
    forms = lumen_search.plan_search_queries("Слушай, а сколько вообще стоит bitcoin сейчас?")
    assert len(forms) == 2
    assert "bitcoin" in forms[0]
    # Ключевая форма — без пустых слов: на ней ищут энциклопедии и обзоры.
    assert forms[1] == "стоит bitcoin"


def test_parse_ddg_lite_html_anomaly_page_is_not_results():
    # 8 попыток из 8 вернули страницу-аномалию: она не «пустая выдача», а блокировка,
    # и её надо распознать, чтобы не жечь бюджет маршрута.
    assert lumen_search._is_ddg_blocked(_DDG_ANOMALY_FIXTURE) is True
    assert lumen_search.parse_ddg_lite_html(_DDG_ANOMALY_FIXTURE) == []
    assert lumen_search._is_ddg_blocked(_DDG_FIXTURE) is False


def test_parse_ddg_html_full_endpoint_shape():
    # Полный HTML-эндпоинт отдал выдачу там, где lite — капчу (замер 11.11.2026).
    out = lumen_search.parse_ddg_html(_DDG_HTML_FIXTURE)
    assert out[0]["url"] == "https://example.org/review"
    assert out[0]["publisher"] == "example.org"
    assert "Ryzen" in out[0]["snippet"]
    assert len(out) == 2


def test_clean_link_unwraps_bing_apiclick_redirect():
    # В промпте был 200-символьный редирект вместо ссылки на статью.
    target = lumen_search._clean_link(
        "http://www.bing.com/news/apiclick.aspx?ref=FexRss&tid=abc"
        "&url=https%3a%2f%2flenta.ru%2fnews%2f2026%2d11%2d11%2fsignal%2f&c=123&mkt=ru-ru"
    )
    assert target == "https://lenta.ru/news/2026-11-11/signal/"
    assert lumen_search._clean_link("https://example.org/a") == "https://example.org/a"


def test_parse_rss_snippets_uses_publisher_and_date_from_google_news():
    out = lumen_search.parse_rss_snippets(_GNEWS_FIXTURE, kind="news")
    # Claude подписывает источник именем издания, а не доменом: <source> его отдаёт.
    assert out[0]["publisher"] == "Центральный Банк России"
    assert out[0]["date"] == "2026-11-05"
    assert out[0]["kind"] == "news"
    # Дубль по ссылке отсечён, а не вытеснил первый сюжет.
    assert len(out) == 1


def test_parse_rss_snippets_reads_bing_news_dates_and_target_url():
    out = lumen_search.parse_rss_snippets(_BING_NEWS_FIXTURE)
    assert out[0]["url"] == "https://lenta.ru/news/2026-11-11/signal/"
    assert out[0]["date"] == "2026-11-10"


def test_format_block_drops_unusable_redirect_urls_but_keeps_attribution():
    out = lumen_search.parse_rss_snippets(_GNEWS_FIXTURE, kind="news")
    block = lumen_search.format_snippets_for_prompt(out)
    assert "news.google.com" not in block
    assert "Центральный Банк России" in block and "2026-11-05" in block


def test_format_block_does_not_repeat_title_inside_snippet():
    # У Google News description = title + издатель: дубль съедал треть бюджета блока.
    block = lumen_search.format_snippets_for_prompt(
        [{"title": "Ставку сохранили", "url": "https://cbr.ru/x", "snippet": "Ставку сохранили ЦБ",
          "publisher": "cbr.ru", "date": "2026-11-05", "kind": "news"}]
    )
    assert block.count("Ставку сохранили") == 1


def test_rank_prefers_live_numbers_and_drops_stale_articles():
    items = [
        {"title": "Старая статья о цене", "url": "https://binance.com/", "snippet": "цена",
         "publisher": "binance.com", "date": "2022-06-15", "kind": "news"},
        {"title": "Цена BTC", "url": "https://coingecko.com/", "snippet": "73529 USD",
         "publisher": "coingecko.com", "date": "2026-11-11", "kind": "live"},
    ]
    ranked = lumen_search._rank(items, wants_fresh=True, wants_entity=False)
    assert [item["kind"] for item in ranked] == ["live"]


def test_rank_orders_entity_source_before_news_when_asking_who():
    items = [
        {"title": "Новость", "url": "https://a.ru/1", "snippet": "s", "publisher": "a.ru",
         "date": "2026-11-11", "kind": "news"},
        {"title": "Набиуллина", "url": "https://ru.wikipedia.org/wiki/x", "snippet": "s",
         "publisher": "ru.wikipedia.org", "date": "", "kind": "fact"},
    ]
    ranked = lumen_search._rank(items, wants_fresh=True, wants_entity=True)
    assert ranked[0]["publisher"] == "ru.wikipedia.org"


def test_limit_per_publisher_caps_one_source():
    items = [{"title": f"n{i}", "url": f"https://udf.by/{i}", "snippet": "s",
              "publisher": "udf.by", "date": "", "kind": "news"} for i in range(6)]
    assert len(lumen_search._limit_per_publisher(items, cap=2)) == 2


def test_detect_intents_by_shape_of_question():
    assert lumen_search.detect_intents("кто сейчас возглавляет Центральный банк России")["entity"] is True
    assert lumen_search.detect_intents("сколько стоит биткоин")["crypto"] is True
    assert lumen_search.detect_intents("какая сейчас погода в Минске")["weather"] is True
    assert lumen_search.detect_intents("курс евро к рублю сегодня")["fx"] is True
    assert lumen_search.detect_intents("последние новости Беларуси")["news"] is True
    # Болтовня не должна звать ни один слой: лишний источник — это лишние секунды.
    assert not any(lumen_search.detect_intents("спасибо, ты классный").values())


def test_cities_in_text_are_normalized_to_nominative():
    # Геокодер ищет по именительному падежу, человек пишет «в Минске».
    assert lumen_search._cities_in("какая погода в Минске") == ["Минск"]
    assert lumen_search._cities_in("погода в Санкт-Петербурге") == ["Санкт-Петербург"]
    # Город вне таблицы уходит геокодеру как написан: дальше решает API.
    assert lumen_search._cities_in("погода в Новогрудке") == ["Новогрудке"]


def _session_for(pages):
    return _RoutedSession(pages)


def test_fetch_web_snippets_asks_crypto_api_only_for_crypto(monkeypatch):
    seen = []

    def spy_session(*args, **kwargs):
        session = _RoutedSession({lumen_search._COINGECKO_URL: '{"bitcoin":{"usd":73557,"usd_24h_change":-0.18}}'})
        seen.append(session)
        return session

    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=spy_session, ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("сколько сейчас стоит биткоин"))
    assert out[0]["kind"] == "live"
    assert "73557" in out[0]["snippet"] or "bitcoin" in out[0]["snippet"]
    session = seen[0]
    assert lumen_search._COINGECKO_URL in session.requested
    # Википедия по вопросам о цене не спрашивается: там нет живого числа.
    assert lumen_search._WIKI_API.format(lang="ru") not in session.requested


def test_fetch_web_snippets_skips_wikipedia_without_entity_intent(monkeypatch):
    session = _RoutedSession({lumen_search._BING_NEWS_URL: _BING_NEWS_FIXTURE})
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *a, **k: session, ClientTimeout=aiohttp.ClientTimeout),
    )
    asyncio.run(lumen_search.fetch_web_snippets("последние новости техники сегодня"))
    assert lumen_search._WIKI_API.format(lang="ru") not in session.requested


def test_fetch_web_snippets_uses_entity_source_for_who_questions(monkeypatch):
    # Прод-класс 11.11.2026: вопрос «кто возглавляет ЦБ» обязан получить справку по
    # сущности, а не только новостной сюжет про котика.
    session = _RoutedSession({
        lumen_search._WIKI_API.format(lang="ru"):
            '{"query":{"pages":{"1":{"title":"Набиуллина, Эльвира Сахипзадовна",'
            '"extract":"Председатель Центрального банка Российской Федерации с 24 июня 2013 года."}}}}',
    })
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *a, **k: session, ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("кто сейчас возглавляет Центральный банк России"))
    assert out[0]["kind"] == "fact"
    assert "2013" in out[0]["snippet"]


def test_fetch_web_snippets_rejects_irrelevant_wikipedia_pages(monkeypatch):
    # Замер: на «кто возглавляет ЦБ» Википедия сначала отдавала Газпромбанк и Сбербанк.
    session = _RoutedSession({
        lumen_search._WIKI_API.format(lang="ru"):
            '{"query":{"pages":{"1":{"title":"Газпромбанк","extract":"крупнейший банк России"}}}}',
    })
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *a, **k: session, ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("кто возглавляет Центральный банк России"))
    assert all("Газпромбанк" != item["title"] for item in out)


def test_search_api_provider_autodetects_key(monkeypatch):
    for var in ("TAVILY_API_KEY", "BRAVE_API_KEY", "EXA_API_KEY", "SEARCH_API_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    assert lumen_search.search_api_provider() == ""
    monkeypatch.setenv("TAVILY_API_KEY", "x")
    assert lumen_search.search_api_provider() == "tavily"
    monkeypatch.setenv("SEARCH_API_PROVIDER", "brave")
    assert lumen_search.search_api_provider() == "brave"


def test_tavily_provider_used_when_key_present(monkeypatch):
    # При ключе агентский источник важнее скрейпинга: он отдаёт чистые URL и цитаты.
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    session = _RoutedSession({
        "https://api.tavily.com/search":
            '{"results":[{"title":"Title","url":"https://real.example/page",'
            '"content":"Body text"}]}',
    })
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *a, **k: session, ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("последние новости космоса сегодня"))
    assert "https://real.example/page" in [item["url"] for item in out]
    assert lumen_search._DDG_LITE_URL not in session.requested


def test_weather_source_resolves_declined_city(monkeypatch):
    session = _RoutedSession({
        lumen_search._GEOCODE_URL: '{"results":[{"name":"Минск","latitude":53.9,"longitude":27.56}]}',
        lumen_search._WEATHER_URL: '{"current":{"time":"2026-11-11T17:30","temperature_2m":3.7,'
                                  '"precipitation":0,"weather_code":1,"wind_speed_10m":11.2},'
                                  '"current_units":{"temperature_2m":"°C"},'
                                  '"daily":{"temperature_2m_max":[4.6],"temperature_2m_min":[1.9]}}',
    })
    monkeypatch.setattr(
        lumen_search, "aiohttp",
        SimpleNamespace(ClientSession=lambda *a, **k: session, ClientTimeout=aiohttp.ClientTimeout),
    )
    out = asyncio.run(lumen_search.fetch_web_snippets("какая сейчас погода в Минске"))
    live = [item for item in out if item["kind"] == "live"]
    assert live and "3.7°C" in live[0]["snippet"]
