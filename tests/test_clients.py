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
