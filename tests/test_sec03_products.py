"""Section 3 handler on fixtures: product/program table from the fake 10-K Item 1 with status
and key metric (release / transcript prepared remarks), competitor comparison in the
narrative, trade-press queries per top segment, and the keyless path."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.sections.exhibits_sec03 import products_in
from primer.state import Inputs
from tests.conftest import _hit

WEB = Path(__file__).parent / "fixtures" / "web"
SECTION = 3


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
        if set("".join(cells)) <= set("-: ") or cells[0] in ("Segment",):
            continue
        rows.append(cells)
    return rows


def _inputs() -> Inputs:
    return Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=[SECTION],
    )


def test_products_in_reads_lists_and_designations():
    p = (
        "Aeronautics is engaged in the development and sustainment of advanced military aircraft. "
        "Major programs include the F-35 Lightning II, C-130 Hercules, F-16 Fighting Falcon and F-22 Raptor."
    )
    assert products_in(p)[:4] == [
        "F-35 Lightning II",
        "C-130 Hercules",
        "F-16 Fighting Falcon",
        "F-22 Raptor",
    ]
    p2 = "MFC provides air and missile defense systems, including the PAC-3 and THAAD programs, and precision strike weapons such as GMLRS and HIMARS."
    assert set(products_in(p2)) >= {"PAC-3", "THAAD", "GMLRS", "HIMARS"}


def test_sec03_product_table_and_competitor_comparison(edgar_mock, firecrawl_mock):
    f35 = _hit(
        "https://breakingdefense.com/2026/01/lockheed-delivered-110-f-35s-in-2025/",
        "Lockheed delivered 110 F-35s in 2025 as TR-3 software backlog cleared",
    )
    pac3 = _hit(
        "https://aviationweek.com/defense/missile-defense/lockheed-pushes-pac-3-mse-output",
        "Lockheed pushes PAC-3 MSE output toward 750 a year as RTX scales Patriot radars",
    )
    transcript = _hit(
        "https://www.fool.com/earnings/call-transcripts/2026/07/23/lockheed-martin-lmt-q2-2026-earnings-call-transcript/",
        "Lockheed Martin (LMT) Q2 2026 Earnings Call Transcript",
    )
    fc = firecrawl_mock(
        search={
            "product roadmap": [f35],
            "new product launch": [pac3],
            "technology differentiation": [f35, pac3],
            "aeronautics 2025": [f35],
            "q2 2026 earnings call transcript": [transcript],
        },
        pages={
            "breakingdefense.com": _page("breakingdefense_lmt_f35_2025.md"),
            "aviationweek.com": _page("aviationweek_lmt_pac3.md"),
            "fool.com": _page("transcript_q2_2026.md"),
        },
    )
    st = _state(_inputs())
    assert st["foundation"]["transcripts"]["found"] == 1
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    md = draft["markdown"]
    k_sid = st["foundation"]["sources"]["10-K"][0]
    tr_sid = st["foundation"]["sources"]["transcript"][0]

    assert draft["exhibit_present"]
    rows = _table_rows(md, "### Products and programs by segment")
    assert len(rows) >= 4
    assert all(f"[{k_sid}]" in r[4] for r in rows), "every row must cite the 10-K"
    by_product = {r[1]: r for r in rows}
    assert {"F-35 Lightning II", "PAC-3", "Black Hawk", "GPS III"} <= set(by_product)
    assert by_product["F-35 Lightning II"][0] == "Aeronautics"
    assert "sustainment" in by_product["F-35 Lightning II"][2]
    assert by_product["PAC-3"][0] == "Missiles and Fire Control"
    # key metric from the transcript prepared remarks, cited to the transcript
    assert "48 F-35 aircraft" in by_product["F-35 Lightning II"][3]
    assert f"[{tr_sid}]" in by_product["F-35 Lightning II"][3]
    metric = next(
        c for c in draft["claims"] if c["metric"] == "product_key_metric_f_35_lightning_ii"
    )
    assert metric["value"] == 48 and metric["unit"] == "count" and metric["source_ids"] == [tr_sid]
    # competitor comparison in the narrative (template path): a scraped page names a peer
    prose = md.split("### Products and programs")[0]
    assert re.search(r"against .*Boeing", prose) or re.search(r"against .*RTX", prose)
    assert "On competition, the 10-K states" in prose
    assert draft["coverage"] == "full"
    searches = [q for kind, q in fc.calls if kind == "search" and "transcript" not in q.lower()]
    assert len(searches) == 4 and any("Aeronautics 2025" in q for q in searches)


def test_sec03_keyless_path_builds_table_from_10k_alone(edgar_mock):
    st = _state(_inputs())
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    assert draft["exhibit_present"]
    rows = _table_rows(draft["markdown"], "### Products and programs by segment")
    k_sid = st["foundation"]["sources"]["10-K"][0]
    assert len(rows) >= 4 and all(f"[{k_sid}]" in r[4] for r in rows)
    assert draft["coverage"] == "partial"
    assert "Firecrawl" in draft["markdown"].split("> Coverage:")[-1]
    assert draft["budget"]["searches"] == "0/4"
