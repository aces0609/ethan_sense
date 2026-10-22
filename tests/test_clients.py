"""PLAN 1.5 / 1.6: Firecrawl registration and credits; SAM.gov quota and entity paging; USAspending."""

from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

from primer.clients.firecrawl import FirecrawlClient, FirecrawlError
from primer.clients.ratelimit import DailyQuota, QuotaExceeded
from primer.clients.samgov import SamGovClient
from primer.clients.usaspending import USAspendingClient, fiscal_year_bounds
from primer.evidence.store import EvidenceStore
from primer.runlog import RunLog


@pytest.fixture
def store(tmp_path):
    return EvidenceStore(tmp_path, as_of=date(2026, 10, 21))


@respx.mock
def test_firecrawl_scrape_registers_press_release(store, tmp_path):
    respx.post("https://api.firecrawl.dev/v2/scrape").mock(
        return_value=Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# Lockheed Martin Reports Q2 Results\n\nNet sales of $18.2 billion.",
                    "metadata": {
                        "title": "Lockheed Martin Reports Second Quarter 2026 Results",
                        "ogSiteName": "Lockheed Martin",
                        "publishedTime": "2026-07-22T12:00:00Z",
                        "statusCode": 200,
                    },
                },
            },
        )
    )
    log = RunLog(tmp_path)
    fc = FirecrawlClient(store, runlog=log, api_key="fc-test")
    out = fc.scrape(
        "https://news.lockheedmartin.com/q2-2026", type="press_release", section_hint="sec-10"
    )
    rec = store.get(out["source_id"])
    assert rec.type == "press_release" and rec.tier == 2
    assert rec.published_date == date(2026, 7, 22)
    assert "Net sales" in store.text(rec.source_id)
    assert log.counters["firecrawl_credits"] == 1 and fc.credits_used == 1


def test_firecrawl_denies_blocked_domain(store):
    fc = FirecrawlClient(store, api_key="fc-test")
    with pytest.raises(FirecrawlError):
        fc.scrape("https://seekingalpha.com/article/1")


@respx.mock
def test_firecrawl_search_counts_credits(store):
    respx.post("https://api.firecrawl.dev/v2/search").mock(
        return_value=Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        {"url": "https://a.test/1", "title": "A"},
                        {"url": "https://wsj.com/x", "title": "paywalled"},
                    ]
                },
            },
        )
    )
    fc = FirecrawlClient(store, api_key="fc-test")
    hits = fc.search("lockheed martin f-35 contract", limit=10)
    assert [h["url"] for h in hits] == ["https://a.test/1"]
    assert fc.credits_used == 2


def test_daily_quota_blocks_eleventh_call(tmp_path):
    q = DailyQuota(tmp_path / "quota.json", cap=10)
    for _ in range(10):
        q.take()
    assert q.remaining() == 0
    with pytest.raises(QuotaExceeded):
        q.take()
    # a new client on the same day shares the counter via the file
    assert DailyQuota(tmp_path / "quota.json", cap=10).used_today() == 10


def _entity(uei, parent, name="LOCKHEED MARTIN CORPORATION"):
    return {
        "entityRegistration": {
            "ueiSAM": uei,
            "cageCode": f"C{uei[-3:]}",
            "legalBusinessName": name,
            "registrationStatus": "Active",
            "registrationExpirationDate": "2027-01-01",
        },
        "coreData": {
            "physicalAddress": {"city": "Bethesda", "stateOrProvinceCode": "MD"},
            "entityHierarchyInformation": {
                "immediateParentEntity": {"ueiSAM": parent},
                "ultimateParentEntity": {"ueiSAM": parent},
            },
        },
    }


@respx.mock
def test_samgov_entity_walk_pages_and_registers(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    # personal keys get 10 records per page
    pages = [
        [_entity(f"UEI{i:03d}", "PARENT") for i in range(10)],
        [_entity(f"UEI{i:03d}", "PARENT") for i in range(10, 20)],
        [_entity("UEI020", "PARENT"), _entity("UEI021", "OTHER")],
    ]
    calls = {"n": 0}

    def respond(request):
        page = int(request.url.params.get("page", 0))
        calls["n"] += 1
        return Response(200, json={"totalRecords": 202, "entityData": pages[page]})

    respx.get("https://api.sam.gov/entity-information/v3/entities").mock(side_effect=respond)
    sam = SamGovClient(store, api_key="k", daily_cap=30)
    ents = sam.walk_parent("PARENT", "LOCKHEED MARTIN")
    assert len(ents) == 21 and calls["n"] == 3
    assert sam.quota.used_today() == 3
    assert ents[0]["source_id"] and store.get(ents[0]["source_id"]).gov_id.startswith("UEI:UEI000")


@respx.mock
def test_samgov_entities_by_uei_one_call_each_within_budget(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    calls: list[str] = []

    def respond(request):
        uei = request.url.params.get("ueiSAM")
        calls.append(uei)
        return Response(200, json={"totalRecords": 1, "entityData": [_entity(uei, "PARENT")]})

    respx.get("https://api.sam.gov/entity-information/v3/entities").mock(side_effect=respond)
    sam = SamGovClient(store, api_key="k", daily_cap=30)
    ents = sam.entities_by_uei([f"UEI{i:03d}" for i in range(15)], max_calls=10)
    assert len(ents) == 10 and calls == [f"UEI{i:03d}" for i in range(10)]
    assert sam.quota.used_today() == 10
    assert store.get(ents[3]["source_id"]).gov_id == "UEI:UEI003;CAGE:C003"


def test_samgov_opportunities_window(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    sam = SamGovClient(store, api_key="k")
    with pytest.raises(ValueError):
        sam.opportunities("F-35", posted_from=date(2024, 1, 1), posted_to=date(2026, 1, 1))


def test_fiscal_year_bounds():
    assert fiscal_year_bounds(2026) == (date(2025, 10, 1), date(2026, 9, 30))


@respx.mock
def test_usaspending_award_registers_gov_id(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    respx.get("https://api.usaspending.gov/api/v2/awards/CONT_AWD_1/").mock(
        return_value=Response(
            200,
            json={
                "piid": "FA8611-26-C-0001",
                "description": "F-35 LOT 20 PRODUCTION",
                "total_obligation": 1.0,
            },
        )
    )
    us = USAspendingClient(store)
    a = us.award("CONT_AWD_1")
    rec = store.get(a["source_id"])
    assert rec.tier == 1 and rec.gov_id == "CONT_AWD_1" and rec.type == "api:usaspending"


@respx.mock
def test_usaspending_retries_a_transient_503_and_counts_it(store, tmp_path, monkeypatch):
    """One 503 from USAspending used to make the whole of section 4 'unavailable'."""
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(USAspendingClient, "BACKOFF_S", (0.0, 0.0, 0.0, 0.0))
    route = respx.get("https://api.usaspending.gov/api/v2/awards/CONT_AWD_9/").mock(
        side_effect=[
            Response(503, text="busy"),
            Response(502, text="bad gateway"),
            Response(200, json={"piid": "N00019-26-C-0001", "description": "B-21 LRIP"}),
        ]
    )
    log = RunLog(tmp_path)
    us = USAspendingClient(store, runlog=log)
    a = us.award("CONT_AWD_9")
    assert a["piid"] == "N00019-26-C-0001" and route.call_count == 3
    assert log.counters["usaspending_retries"] == 2 and log.counters["usaspending_requests"] == 3


@respx.mock
def test_usaspending_does_not_retry_a_client_error_and_gives_up_after_four_attempts(
    store, tmp_path, monkeypatch
):
    import httpx

    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(USAspendingClient, "BACKOFF_S", (0.0, 0.0, 0.0, 0.0))
    nf = respx.get("https://api.usaspending.gov/api/v2/awards/CONT_AWD_404/").mock(
        return_value=Response(404, text="no such award")
    )
    down = respx.get("https://api.usaspending.gov/api/v2/awards/CONT_AWD_DOWN/").mock(
        return_value=Response(503, text="down")
    )
    us = USAspendingClient(store)
    with pytest.raises(httpx.HTTPStatusError):
        us.award("CONT_AWD_404")
    assert nf.call_count == 1  # a 404 is the caller's error: no retry
    with pytest.raises(httpx.HTTPStatusError):
        us.award("CONT_AWD_DOWN")
    assert down.call_count == USAspendingClient.RETRIES  # then the failure is raised, not cached


def test_a_replayed_failure_prints_as_the_original_exception_did():
    from primer.clients.cache import RecordedError

    e = RecordedError(
        "replay: recorded failure (HTTPStatusError): Server error '503'",
        err_type="HTTPStatusError",
        original="Server error '503 Service Unavailable' for url 'https://x.test/a'",
    )
    assert (
        repr(e)
        == "HTTPStatusError(\"Server error '503 Service Unavailable' for url 'https://x.test/a'\")"
    )
    assert str(e) == "Server error '503 Service Unavailable' for url 'https://x.test/a'"
    # without the original text it behaves as before
    assert "replay" in str(RecordedError("replay: SAM.gov was not configured"))


def _award_rows(prefix: str, amounts: list[int]) -> list[dict]:
    return [
        {"generated_internal_id": f"{prefix}{i}", "Award ID": f"{prefix}{i}", "Award Amount": a}
        for i, a in enumerate(amounts)
    ]


@respx.mock
def test_usaspending_batches_a_large_recipient_set_and_merges_by_amount(
    store, tmp_path, monkeypatch
):
    """Northrop Grumman resolved to 31 UEIs; one text search over all of them returns a 503."""
    import json as _json

    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(USAspendingClient, "BACKOFF_S", (0.0, 0.0, 0.0, 0.0))
    seen_sizes: list[int] = []

    def respond(request):
        terms = _json.loads(request.content)["filters"]["recipient_search_text"]
        seen_sizes.append(len(terms))
        if len(terms) > 10:
            return Response(503, text="timeout")
        base = int(terms[0][1:])  # "U10" -> 10
        return Response(
            200,
            json={
                "results": _award_rows(f"A{base}-", [base * 1000 + 7, base * 1000 + 3]),
                "page_metadata": {"hasNext": False},
            },
        )

    respx.post("https://api.usaspending.gov/api/v2/search/spending_by_award/").mock(
        side_effect=respond
    )
    terms = [f"U{i}" for i in range(31)]
    us = USAspendingClient(store)
    resp = us.spending_by_award(terms, 2021, 2026, limit=5)
    assert max(seen_sizes) <= USAspendingClient.MAX_TERMS and len(seen_sizes) == 4
    amounts = [r["Award Amount"] for r in resp["results"]]
    assert len(amounts) == 5 and amounts == sorted(amounts, reverse=True)
    assert amounts[0] == 24 * 1000 + 7  # the largest award of the last batch leads the ranking
    assert resp["page_metadata"]["hasNext"] is True and resp["page_metadata"]["batches"] == 4
    assert resp["source_id"]


@respx.mock
def test_usaspending_batched_pull_survives_one_failed_batch_and_fails_if_all_fail(
    store, tmp_path, monkeypatch
):
    import json as _json

    import httpx

    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(USAspendingClient, "BACKOFF_S", (0.0, 0.0, 0.0, 0.0))
    mode = {"fail_first_batch": True, "fail_all": False}

    def respond(request):
        terms = _json.loads(request.content)["filters"]["recipient_search_text"]
        if mode["fail_all"] or (mode["fail_first_batch"] and terms[0] == "U0"):
            return Response(503, text="timeout")
        return Response(
            200,
            json={"results": _award_rows(terms[0], [100]), "page_metadata": {"hasNext": False}},
        )

    respx.post("https://api.usaspending.gov/api/v2/search/spending_by_award/").mock(
        side_effect=respond
    )
    terms = [f"U{i}" for i in range(20)]
    log = RunLog(tmp_path)
    us = USAspendingClient(store, runlog=log)
    resp = us.spending_by_award(terms, 2021, 2026, limit=10)
    assert len(resp["results"]) == 2 and len(resp["batch_errors"]) == 1
    assert any("batch of 8 UEIs failed" in e for e in log.errors)
    mode["fail_all"] = True
    with pytest.raises(httpx.HTTPStatusError):
        us.spending_by_award([f"V{i}" for i in range(20)], 2021, 2026, limit=10)


@respx.mock
def test_usaspending_category_totals_are_summed_across_batches(store, tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(USAspendingClient, "BACKOFF_S", (0.0, 0.0, 0.0, 0.0))

    def respond(request):
        terms = _json.loads(request.content)["filters"]["recipient_search_text"]
        if len(terms) > 10:
            return Response(503, text="timeout")
        return Response(
            200,
            json={
                "results": [
                    {"name": "Department of the Navy", "amount": 100.0},
                    {"name": f"Agency {terms[0]}", "amount": 5.0},
                ]
            },
        )

    respx.post(
        "https://api.usaspending.gov/api/v2/search/spending_by_category/awarding_subagency/"
    ).mock(side_effect=respond)
    us = USAspendingClient(store)
    resp = us.spending_by_category(
        "awarding_subagency", [f"U{i}" for i in range(20)], 2025, 2025, limit=2
    )
    top = resp["results"][0]
    assert top["name"] == "Department of the Navy" and top["amount"] == 300.0  # 3 batches x 100
    assert len(resp["results"]) == 2 and resp["page_metadata"]["batches"] == 3
