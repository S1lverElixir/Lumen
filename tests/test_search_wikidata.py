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


class _MappedWikidataSession:
    """Граница I/O: поиск отвечает по термину (регистронезависимо), утверждения — по Qid."""

    def __init__(self, searches, claims, labels=None):
        self.searches = {k.lower(): v for k, v in searches.items()}
        self.claims = claims
        self.labels = labels or {"Q28946": "Эльвира Набиуллина"}
        self.requested = []

    def _body(self, url, params):
        action = (params or {}).get("action")
        if action == "wbsearchentities":
            hits = self.searches.get(((params or {}).get("search") or "").lower(), [])
            return json.dumps({"search": [{"id": q} for q in hits]})
        if action == "wbgetentities":
            ids = [q for q in (params or {}).get("ids", "").split("|") if q]
            return json.dumps({"entities": {
                qid: {"labels": {"ru": {"value": self.labels.get(qid, qid)},
                                 "en": {"value": self.labels.get(qid, qid)}}} for qid in ids}})
        qid = (url or "").rsplit("/", 1)[-1].removesuffix(".json")
        return json.dumps({"entities": {qid: {"claims": self.claims.get(qid, {})}}})

    def get(self, url, params=None, headers=None, timeout=None):
        self.requested.append((params or {}).get("search") or url)
        session = self

        class _Ctx:
            async def __aenter__(self):
                class _Resp:
                    status = 200

                    async def text(self):
                        return session._body(url, params)

                    def raise_for_status(self):
                        pass

                return _Resp()

            async def __aexit__(self, *args):
                return False

        return _Ctx()


def _leader_claims(target="Q28946", start="+2013-06-24T00:00:00Z"):
    return {"P488": [{
        "rank": "normal",
        "mainsnak": {"datavalue": {"value": {"id": target}}},
        "qualifiers": {"P580": [{"datavalue": {"value": start}}]},
    }]}


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


def test_entity_candidates_cover_case_compound_and_of():
    # Каждый кандидат ловит свой класс промаха замера 11.11.2026: падеж, составной
    # хвост, выброшенные «of»/«the». Полный вариант остаётся первым — он точнее.
    compound = lumen_search._entity_candidates(
        "кто сейчас возглавляет Центральный банк России и какая ключевая ставка")
    assert compound[0] == "Центральный банк России ключевая ставка"
    assert "Центральный банк России" in compound
    assert "Central Bank of Russia" in lumen_search._entity_candidates(
        "who is the head of the Central Bank of Russia")
    assert "Великоб" in lumen_search._entity_candidates("премьер-министр Великобритании сейчас")
    assert len(compound) <= lumen_search._WD_MAX_SEARCHES


def test_wikidata_navigates_around_declined_term():
    # Прод 11.11.2026: индексу unknown «банка россии», а срез «банк росси» даёт
    # Q806651 — слой обязан дойти до среза, а не останавливаться на падежной форме.
    lumen_search.clear_reference_cache()
    session = _MappedWikidataSession(
        {"банка россии": [], "банк росси": ["Q806651"]},
        {"Q806651": _leader_claims()},
    )
    out = _run(session, query="руководитель Банка России")
    assert out and "Набиуллина" in out[0]["snippet"]


def test_wikidata_cuts_compound_question_tail():
    # Прод 11.11.2026: «...и какая ключевая ставка» — второй факт, индекс с ним
    # не находит ничего; хвост после союза отрезается отдельным кандидатом.
    lumen_search.clear_reference_cache()
    session = _MappedWikidataSession(
        {"центральный банк россии": ["Q806651"]},
        {"Q806651": _leader_claims()},
    )
    out = _run(session, query="кто сейчас возглавляет Центральный банк России и какая ключевая ставка")
    assert out and out[0]["date"] == "2013-06-24"


def test_wikidata_keeps_prepositions_in_english_names():
    # Прод 11.11.2026: «Central Bank Russia» индекс не знает, «Central Bank of
    # Russia» — знает; предлоги имени не выбрасываются.
    lumen_search.clear_reference_cache()
    session = _MappedWikidataSession(
        {"central bank of russia": ["Q806651"]},
        {"Q806651": _leader_claims()},
        labels={"Q28946": "Elvira Nabiullina"},
    )
    out = _run(session, query="who is the head of the Central Bank of Russia")
    assert out and "Nabiullina" in out[0]["snippet"]


def test_wikidata_takes_entity_with_leader_claim_not_first_hit():
    # Прод 11.11.2026: «великобритании» даёт новостный заголовок без утверждений —
    # брать надо Q с открытым P488/P6/P169, а не первый попавшийся.
    lumen_search.clear_reference_cache()
    session = _MappedWikidataSession(
        {"цб россии": ["Q99018474", "Q806651"]},
        {"Q806651": _leader_claims()},
    )
    out = _run(session, query="кто глава ЦБ России")
    assert out and out[0]["url"] == "https://www.wikidata.org/wiki/Q806651"


def test_wikidata_respects_deadline_without_requests():
    # Кандидатный цикл не имеет права запускать сетевые запросы после deadline:
    # бюджет сообщения общий, а кандидатов теперь до восьми (замер 11.11.2026).
    lumen_search.clear_reference_cache()
    session = _MappedWikidataSession({"цб россии": ["Q806651"]}, {"Q806651": _leader_claims()})

    async def scenario():
        import time
        return await lumen_search._wikidata_leader_snippets(
            session, "кто глава ЦБ России", 3, time.monotonic() - 1)

    assert asyncio.run(scenario()) == []
    assert session.requested == [], "прошедший deadline — ни одного обращения"
