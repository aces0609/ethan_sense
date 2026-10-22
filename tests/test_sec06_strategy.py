"""Section 6 handler on fixtures: management targets vs consensus with a date on every
target row, guidance claims parsed from the release Outlook table, consensus from the
mocked Yahoo page, the investor-day deck from the IR site map, and the keyless path."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.sections.exhibits_sec06 import parse_release_outlook
from primer.sections.sec06_strategy import pick_presentation
from primer.state import Inputs
from tests.conftest import FAKE_RELEASE_HTML, _hit

WEB = Path(__file__).parent / "fixtures" / "web"
SECTION = 6
DECK_URL = "https://investors.lockheedmartin.com/static-files/investor-day-2025.pdf"


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
    return rows[1:]


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


def test_parse_release_outlook_reads_ranges_and_year():
    from primer.evidence.html import html_to_text

    out = parse_release_outlook(html_to_text(FAKE_RELEASE_HTML))
    assert out.fy == 2026 and out.prior_label == "April 2026"
    by = {r.metric: r for r in out.rows}
    assert by["guidance_sales"].low == 79_750e6 and by["guidance_sales"].high == 81_750e6
    assert by["guidance_sales"].prior_low == 77_500e6
    assert by["guidance_eps"].low == 29.95 and by["guidance_eps"].unit == "USD/shares"
    assert by["guidance_fcf"].high == 7_200e6 and by["guidance_ocf"].low == 9_200e6


def test_pick_presentation_prefers_investor_pdf():
    links = [
        "https://investors.example.com/events/webcast-2024",
        "https://investors.example.com/static-files/q1-2026-slides.pdf",
        DECK_URL,
    ]
    assert pick_presentation(links, date(2026, 10, 21), 2025) == DECK_URL


def test_sec06_targets_vs_consensus_with_dates(edgar_mock, firecrawl_mock):
    transcript = _hit(
        "https://www.fool.com/earnings/call-transcripts/2026/07/23/lockheed-martin-lmt-q2-2026-earnings-call-transcript/",
        "Lockheed Martin (LMT) Q2 2026 Earnings Call Transcript",
    )
    pac3 = _hit(
        "https://aviationweek.com/defense/missile-defense/lockheed-pushes-pac-3-mse-output",
        "Lockheed pushes PAC-3 MSE output toward 750 a year as RTX scales Patriot radars",
    )
    fc = firecrawl_mock(
        search={
            "investor day": [pac3],
            "long-term targets": [pac3],
            "capacity expansion": [pac3],
            "q2 2026 earnings call transcript": [transcript],
        },
        pages={
            "investor-day-2025.pdf": _page("lmt_investor_day_2025.md"),
            "fool.com": _page("transcript_q2_2026.md"),
            "aviationweek.com": _page("aviationweek_lmt_pac3.md"),
            "finance.yahoo.com": "# Lockheed Martin Corporation (LMT) Analyst Estimates\n\nAnalysis tab.\n",
        },
        jsons={"finance.yahoo.com": json.loads(_page("yahoo_analysis_LMT.json"))},
        maps={
            "investors.lockheedmartin.com": [
                DECK_URL,
                "https://investors.lockheedmartin.com/news-releases",
                "https://investors.lockheedmartin.com/events-and-presentations",
            ]
        },
    )
    st = _state(_inputs())
    assert DECK_URL in st["foundation"]["ir"]["links"]["presentations"]
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    md = draft["markdown"]
    rel_sid = st["foundation"]["sources"]["earnings_release"][0]
    k_sid = st["foundation"]["sources"]["10-K"][0]
    tr_sid = st["foundation"]["sources"]["transcript"][0]

    assert draft["exhibit_present"]
    rows = _table_rows(md, "### Management targets vs consensus (dated)")
    assert len(rows) >= 5
    for r in rows:
        assert re.search(r"\d{4}-\d{2}-\d{2} \[src_\d{4}\]", r[2]), (
            f"target row without a date: {r}"
        )
        assert re.search(r"\[src_\d{4}\]", r[1])
    sales = next(r for r in rows if r[0].startswith("Sales (FY2026 outlook"))
    assert (
        "$79,750M to $81,750M" in sales[1] and "prior $77,500M to $80,000M, April 2026" in sales[1]
    )
    assert sales[2] == f"2026-07-23 [{rel_sid}]"
    assert "$80,500M" in sales[3] and "Yahoo Finance, 2026-10-21" in sales[3]
    assert sales[4].startswith("+0.3%") and "derived" in sales[4]
    eps = next(r for r in rows if r[0].startswith("Diluted earnings per share (FY2026 outlook"))
    assert "$29.95 to $30.65" in eps[1] and "$30.20" in eps[3]
    mdna = next(r for r in rows if "10-K MD&A outlook" in r[0])
    assert mdna[2] == f"2026-01-29 [{k_sid}]"
    remarks = [r for r in rows if r[0] == "Prepared remarks (earnings call)"]
    assert remarks and all(r[2] == f"2026-07-23 [{tr_sid}]" for r in remarks)
    assert any("650 interceptors" in r[1] for r in remarks)

    lo, hi = (
        _claim(draft, "guidance_sales_low", "FY2026"),
        _claim(draft, "guidance_sales_high", "FY2026"),
    )
    assert lo["value"] == 79_750e6 and hi["value"] == 81_750e6 and lo["unit"] == "USD"
    assert lo["source_ids"] == [rel_sid] and "2026-07-23" in lo["note"]
    assert _claim(draft, "guidance_eps_low", "FY2026")["value"] == 29.95
    assert _claim(draft, "guidance_eps_high", "FY2026")["unit"] == "USD/shares"
    cons = _claim(draft, "consensus_revenue", "FY2026")
    assert cons["value"] == 80.5e9 and "finance.yahoo.com" in store.get(cons["source_ids"][0]).url
    assert _claim(draft, "consensus_eps", "FY2026")["value"] == 30.2
    gap = _claim(draft, "guidance_sales_vs_consensus_gap_pct", "FY2026")
    assert gap and gap["concept"] == "derived" and abs(gap["value"] - 0.31) < 0.02

    deck = [r for r in store.records() if r.type == "presentation"]
    assert deck and deck[0].url == DECK_URL and deck[0].source_id in draft["source_ids"]
    assert any(kind == "scrape" and u == DECK_URL for kind, u in fc.calls)
    assert draft["coverage"] == "full"
    searches = [q for kind, q in fc.calls if kind == "search" and "transcript" not in q.lower()]
    # six template queries fill the section 6 cap of 6 (2026-10-22); the rate-target query runs
    assert len(searches) == 6 and any("production rate target" in q for q in searches)


def test_sec06_keyless_path_targets_from_release_and_mdna(edgar_mock):
    st = _state(_inputs())
    draft = _run(st, SECTION)
    store = _store(st)
    _check_contract(draft, store, SECTION)
    md = draft["markdown"]
    assert draft["exhibit_present"]
    rows = _table_rows(md, "### Management targets vs consensus (dated)")
    assert rows and all(re.search(r"\d{4}-\d{2}-\d{2} \[src_\d{4}\]", r[2]) for r in rows)
    assert all(r[3] == "n/a" for r in rows), "no market-data adapter without Firecrawl"
    assert _claim(draft, "guidance_sales_low", "FY2026")["value"] == 79_750e6
    assert _claim(draft, "guidance_sales_high", "FY2026")["value"] == 81_750e6
    assert _claim(draft, "consensus_revenue") is None
    assert draft["coverage"] == "partial"
    tail = md.split("> Coverage:")[-1]
    assert "Firecrawl" in tail and "consensus" in tail
    assert draft["budget"]["searches"] == "0/6" and draft["budget"]["scrapes"] == "0/12"
