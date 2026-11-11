"""
test_search_wikidata.py — слой руководителей из Wikidata: тема сущности, действующее
назначение, лимит обращений и кэш.

Что защищают: промер 11.11.2026 — «кто сейчас возглавляет Центральный банк России»
не давал имени ни в DDG (страница-аномалия), ни в новостях (сюжеты про ставку),
а индекс сущностей молчал, потому что запросили его полной фразой с глаголом.
Сеть не трогаем: ответы Wikidata отдаёт стаб на границе aiohttp.

Запуск:
    pytest tests/test_search_wikidata.py -v
"""
import asyncio
import json
import types

import lumen_search


class _WikidataSession:
    """Граница I/O: три разных ответа Wikidata на одном хосте — разбор по action."""

    def __init__(self, *, rate_limited=False, expired=False):
        self.rate_limited = rate_limited
        self.expired = expired
        self.requested = []

    def _body(self, params):
        action = (params or {}).get("action")
        if action == "wbsearchentities":
            return json.dumps({"search": [{"id": "Q806651",
                                           "label": "Центральный банк Российской Федерации"}]})
        if action == "wbgetentities":
            return json.dumps({"entities": {"Q28946": {"labels": {"ru": {"value": "Эльвира Набиуллина"}}}}})
        qualifiers = {"P580": [{"datavalue": {"value": "+2013-06-24T00:00:00Z"}}]}
        if self.expired:
            qualifiers["P582"] = [{"datavalue": {"value": "+2026-01-01T00:00:00Z"}}]
        return json.dumps({"entities": {"Q806651": {"claims": {
            "P488": [{
                "rank": "normal",
                "mainsnak": {"datavalue": {"value": {"id": "Q28946"}}},
                "qualifiers": qualifiers,
            }],
        }}}})

    def get(self, url, params=None, headers=None, timeout=None):
        self.requested.append((url, (params or {}).get("action"), (params or {}).get("search")))
        session = self

        class _Ctx:
            async def __aenter__(self):
                class _Resp:
                    status = 429 if session.rate_limited else 200

                    async def text(self):
                        return session._body(params)

                    def raise_for_status(self):
                        if self.status >= 400:
                            raise RuntimeError(f"http {self.status}")

                return _Resp()

            async def __aexit__(self, *args):
                return False

        return _Ctx()


def _run(session, query="кто сейчас возглавляет Центральный банк России"):
    async def scenario():
        import time
        return await lumen_search._wikidata_leader_snippets(session, query, 3, time.monotonic() + 6)

    return asyncio.run(scenario())


def test_entity_topic_drops_role_words():
    # Индекс сущностей ищется по имени организации: с «кто возглавляет»
    # wbsearchentities не находил ничего (замер 11.11.2026).
    assert lumen_search._entity_topic("кто сейчас возглавляет Центральный банк России") == "Центральный банк России"
    assert lumen_search._entity_topic("chairperson of the Federal Reserve") == "Federal Reserve"


def test_wikidata_returns_current_leader_with_start_date():
    lumen_search.clear_reference_cache()
    out = _run(_WikidataSession())
    assert out, "Wikidata-слой обязан дать руководителя"
    assert "Набиуллина" in out[0]["snippet"]
    assert out[0]["date"] == "2013-06-24"
    assert out[0]["kind"] == "fact"
    assert out[0]["url"] == "https://www.wikidata.org/wiki/Q806651"


def test_wikidata_skips_finished_appointments():
    # Действующим считаем только назначение без даты конца, иначе модель назвала бы
    # нынешним председателем того, кто ушёл.
    lumen_search.clear_reference_cache()
    assert _run(_WikidataSession(expired=True)) == []


def test_wikidata_gives_up_on_rate_limit(monkeypatch):
    # 429 от Wikimedia — не повод жевать бюджет сообщения: слой отдаёт пусто сразу.
    lumen_search.clear_reference_cache()
    calls = []
    monkeypatch.setattr(lumen_search, "log", types.SimpleNamespace(info=lambda *args: calls.append(args)))
    assert _run(_WikidataSession(rate_limited=True)) == []
    assert any("rate limited" in str(entry) for entry in calls)


def test_reference_cache_serves_repeated_query():
    # Второй тот же вопрос не должен ходить в Wikimedia: лимит считается обращениями,
    # а не объёмом (замер 11.11.2026).
    lumen_search.clear_reference_cache()
    session = _WikidataSession()
    first = _run(session)
    second = _run(session)
    assert first and second == first
    assert len(session.requested) == 3, session.requested
