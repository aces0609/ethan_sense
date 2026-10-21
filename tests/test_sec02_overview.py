"""Section 2 handler on fixtures: segment table from the fake 10-K, employee claim, the
listing/ownership caveat, web context through firecrawl_mock, and the keyless path."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.state import Inputs
from tests.conftest import _hit

WEB = Path(__file__).parent / "fixtures" / "web"
SECTION = 2


def _page(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _state(inputs: Inputs) -> dict:
    run_id, run_dir = new_run(inputs)
    st = {"inputs": inputs.model_dump(mode="json"), "run_id": run_id, "run_dir": str(run_dir)}
    for node in (intake, resolve_entity, foundation_pack, plan_sections):
        st.update(node(st))
    return st


def _run(st: dict, section: int) -> dict:
    out = build_researcher().invoke(
        {
            "run_dir": st["run_dir"],
            "as_of": st["as_of"],
            "entity": st["entity"],
            "foundation": st["foundation"],
            "brief": st["plan"][section],
            "iteration": 0,
            "critic_notes": [],
        }
    )
    return out["drafts"][section]


def _store(st: dict) -> EvidenceStore:
    return EvidenceStore.for_run(st["run_dir"], as_of=date.fromisoformat(st["as_of"]))


def _check_contract(draft: dict, store: EvidenceStore, section: int) -> None:
    md = draft["markdown"]
    tokens = re.findall(r"\[src_[^\]]*\]", md)
    assert tokens, "no citations at all"
    for tok in tokens:
        m = re.fullmatch(r"\[(src_\d{4})\]", tok)
        assert m and store.has(m.group(1)), f"unknown citation token {tok}"
    for c in draft["claims"]:
        assert c["source_ids"] or c["concept"] == "derived", c
        for sid in c["source_ids"]:
            assert store.has(sid), c
    b = section_budget(section)
    for key, cap_key in (("searches", "searches"), ("scrapes", "scrapes")):
        used, cap = (int(x) for x in draft["budget"][key].split("/"))
        assert cap == int(b[cap_key]) and used <= cap, draft["budget"]
    assert draft["caveat"] and draft["caveat"].startswith("*Caveat:")


def _inputs() -> Inputs:
    return Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=[SECTION],
    )


def _claim(draft: dict, metric: str, period: str | None = None) -> dict | None:
    for c in draft["claims"]:
        if c["metric"] == metric and (period is None or c["period"] == period):
            return c
    return None


def test_sec02_segment_table_and_caveat_with_web(edgar_mock, firecrawl_mock):
    overview = _hit(
        "https://www.defensenews.com/industry/2026/03/03/how-lockheed-martin-is-organized/",
        "How Lockheed Martin is organized: four segments, one customer that matters most",
    )
    fc = firecrawl_mock(
        search={
            "business overview": [overview],
            "segments revenue breakdown": [overview],
            "company history founded": [overview],
        },
        pages={"defensenews.com": _page("defensenews_lmt_overview.md")},
    )
    st = _state(_inputs())
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    md = draft["markdown"]

    k_sid = st["foundation"]["sources"]["10-K"][0]
    rec = store.get(k_sid)
    assert rec.type == "10-K" and rec.tier == 1
    assert draft["exhibit_present"]
    assert "| Segment (USD millions) | FY2025 | FY2024 | FY2023 |" in md
    assert f"| Aeronautics | 30,200 [{k_sid}] | 28,900 [{k_sid}] | 27,500 [{k_sid}] |" in md
    assert f"**75,048** [{k_sid}]" in md
    assert "share of total — derived" in md and "40.2%" in md

    seg_claims = {
        c["metric"]
        for c in draft["claims"]
        if c["metric"].startswith("segment_revenue_")
        and not c["metric"].endswith("_pct")
        and c["metric"] != "segment_revenue_total"
    }
    assert len(seg_claims) == 4
    aero = _claim(draft, "segment_revenue_aeronautics", "FY2025")
    assert aero and aero["value"] == 30_200e6 and aero["source_ids"] == [k_sid]
    assert aero["unit"] == "USD" and "millions" in (aero["note"] or "")
    total = _claim(draft, "segment_revenue_total", "FY2025")
    assert total and total["value"] == 75_048e6
    share = _claim(draft, "segment_revenue_share_aeronautics_pct", "FY2025")
    assert share and share["concept"] == "derived" and abs(share["value"] - 40.24) < 0.05
    emp = _claim(draft, "employee_count")
    assert (
        emp and emp["value"] == 121_000 and emp["unit"] == "count" and emp["source_ids"] == [k_sid]
    )

    caveat = draft["caveat"]
    assert "NYSE" in caveat and "LMT" in caveat
    def_sid = st["foundation"]["sources"]["DEF 14A"][0]
    assert "State Street Corporation (15.6%)" in caveat and f"[{def_sid}]" in caveat
    assert "Bethesda, Maryland" in md
    assert draft["coverage"] == "full"
    assert [c for c in fc.calls if c[0] == "search"], "web research did not run"
    assert any(c[0] == "scrape" and "defensenews" in c[1] for c in fc.calls)
    used, cap = (int(x) for x in draft["budget"]["searches"].split("/"))
    assert used == cap == 2


def test_sec02_keyless_path_builds_exhibit_from_filings(edgar_mock):
    st = _state(_inputs())
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    assert draft["exhibit_present"]
    assert draft["coverage"] == "partial"
    assert "Firecrawl" in draft["markdown"].split("> Coverage:")[-1]
    assert draft["budget"]["searches"] == "0/2" and draft["budget"]["scrapes"] == "0/4"
    k_sid = st["foundation"]["sources"]["10-K"][0]
    assert f"| Aeronautics | 30,200 [{k_sid}]" in draft["markdown"]
    assert _claim(draft, "employee_count")["value"] == 121_000
    assert "Evidence gathered" in draft["markdown"]
