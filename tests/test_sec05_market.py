"""Section 5 handler on fixtures: a SWOT whose every cell is cited or 'no sourced item', the
peer list from the 10-K competitor paragraph (peer 10-K Item 1 fetched for up to three
peers), government-revenue and peer-count claims, and the keyless path."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from httpx import Response

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.sections.exhibits_sec05 import NO_ITEM
from primer.state import Inputs
from tests.conftest import _hit

WEB = Path(__file__).parent / "fixtures" / "web"
SECTION = 5
PEER_CIKS = {"BA": 12927, "GD": 40533, "NOC": 1133421, "RTX": 101829}


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
    for tok in re.findall(r"\[src_[^\]]*\]", md):
        m = re.fullmatch(r"\[(src_\d{4})\]", tok)
        assert m and store.has(m.group(1)), f"unknown citation token {tok}"
    for c in draft["claims"]:
        assert c["source_ids"] or c["concept"] == "derived", c
        for sid in c["source_ids"]:
            assert store.has(sid), c
    b = section_budget(section)
    for key in ("searches", "scrapes"):
        used, cap = (int(x) for x in draft["budget"][key].split("/"))
        assert cap == int(b[key]) and used <= cap, draft["budget"]
    assert draft["caveat"] and draft["caveat"].startswith("*Caveat:")


def _table_rows(md: str, heading: str) -> list[list[str]]:
    block = md.split(heading, 1)[1]
    rows = []
    for line in block.splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    return rows[1:]  # drop the header


def _inputs() -> Inputs:
    return Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=[SECTION],
    )


def _mock_peer_submissions(edgar_mock, fixture_json) -> None:
    """Peer submissions = the LMT fixture with peer-specific accession numbers, so each
    peer 10-K registers under its own dedupe key and the primary document falls through to
    the fake 10-K route."""
    for cik in PEER_CIKS.values():
        subs = json.loads(json.dumps(fixture_json("submissions_LMT.json")))
        rec = subs["filings"]["recent"]
        rec["accessionNumber"] = [
            a.replace("0001628280-26-004195", f"{cik:010d}-26-004195")
            for a in rec["accessionNumber"]
        ]
        edgar_mock.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").mock(
            return_value=Response(200, json=subs)
        )


def test_sec05_swot_and_peers_with_web(edgar_mock, firecrawl_mock, fixture_json):
    _mock_peer_submissions(edgar_mock, fixture_json)
    vs = _hit(
        "https://www.defensenews.com/industry/2026/05/20/lockheed-vs-rtx-missile-market/",
        "Lockheed vs. RTX: who leads the missile market as budgets shift to munitions",
    )
    pac3 = _hit(
        "https://aviationweek.com/defense/missile-defense/lockheed-pushes-pac-3-mse-output",
        "Lockheed pushes PAC-3 MSE output toward 750 a year as RTX scales Patriot radars",
    )
    fc = firecrawl_mock(
        search={
            "market share": [vs],
            "competitors": [pac3, vs],
            " vs ": [vs],
            "moat analysis": [vs],
        },
        pages={
            "defensenews.com": _page("defensenews_lmt_vs_rtx.md"),
            "aviationweek.com": _page("aviationweek_lmt_pac3.md"),
        },
    )
    st = _state(_inputs())
    peers = {p["ticker"] for p in st["foundation"]["peers"]}
    assert {"BA", "GD", "NOC", "RTX"} <= peers
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    md = draft["markdown"]
    k_sid = st["foundation"]["sources"]["10-K"][0]

    assert draft["exhibit_present"]
    swot = _table_rows(md, "### SWOT (every cell sourced)")
    assert swot and all(len(r) == 4 for r in swot)
    for r in swot:
        for cell in r:
            assert cell == NO_ITEM or re.search(r"\[src_\d{4}\]", cell), cell
    cols = list(zip(*swot, strict=True))
    assert any("72%" in c for c in cols[0]), "strength from the government-revenue sentence"
    assert any("F-35" in c or "depend" in c for c in cols[1]), "weakness from Item 1A"
    assert any("backlog" in c.lower() or "capacity" in c.lower() for c in cols[2])
    assert any("compete" in c or "cybersecurity" in c for c in cols[3])
    assert all(f"[{k_sid}]" in c for c in cols[1] + cols[3] if c != NO_ITEM)

    peer_rows = _table_rows(md, "### Peer list")
    tickers = {r[1] for r in peer_rows}
    assert {"BA", "GD", "NOC", "RTX"} <= tickers
    fetched = [r for r in peer_rows if "peer 10-K Item 1" in r[3]]
    named = [r for r in peer_rows if "named as a competitor in the 10-K" in r[2]]
    assert len(fetched) == 3 and len(named) >= 1, "cap at three peer filings"
    for r in fetched:
        sid = re.search(r"\[(src_\d{4})\]", r[3]).group(1)
        rec = store.get(sid)
        assert rec.type == "10-K" and rec.tier == 1 and rec.gov_id and rec.meta.get("peer")

    gov = next(c for c in draft["claims"] if c["metric"] == "us_government_revenue_pct")
    assert gov["value"] == 72 and gov["unit"] == "pct" and gov["period"] == "FY2025"
    assert gov["source_ids"] == [k_sid]
    pc = next(c for c in draft["claims"] if c["metric"] == "peer_count")
    assert pc["value"] == len(peer_rows) and pc["unit"] == "count"
    assert draft["coverage"] == "full"
    searches = [q for kind, q in fc.calls if kind == "search" and "transcript" not in q.lower()]
    # five generic plus two per-peer templates for two peers (9) stop at the section 5 cap of 8
    assert len(searches) == 8 and any(" vs " in q for q in searches)
    assert any("order backlog OR fleet" in q for q in searches)


def test_sec05_keyless_path_swot_from_filings(edgar_mock):
    st = _state(_inputs())
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    md = draft["markdown"]
    assert draft["exhibit_present"]
    swot = _table_rows(md, "### SWOT (every cell sourced)")
    for r in swot:
        for cell in r:
            assert cell == NO_ITEM or re.search(r"\[src_\d{4}\]", cell), cell
    peer_rows = _table_rows(md, "### Peer list")
    assert {"BA", "GD", "NOC", "RTX"} <= {r[1] for r in peer_rows}
    # no peer submissions mocked: every peer falls back to the 10-K mention, nothing crashes
    assert all("named as a competitor in the 10-K" in r[2] for r in peer_rows)
    assert draft["coverage"] == "partial"
    assert "Firecrawl" in md.split("> Coverage:")[-1]
    assert draft["budget"]["searches"] == "0/8"  # budgets.yaml section 5 (raised 2026-10-22)
