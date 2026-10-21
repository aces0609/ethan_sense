"""Section 10 (Operating Performance and Benchmarking) handler on fixtures: release parsing,
KPI vs consensus with derived variance, segment margins from the 10-K, management outlook,
and the no-adapter path."""

from __future__ import annotations

import re

from primer.evidence.html import html_to_text
from primer.evidence.store import EvidenceStore
from primer.sections.exhibits_sec10 import norm_quarter, parse_release
from tests.conftest import FAKE_RELEASE_HTML
from tests.test_sec13_valuation import (
    assert_budget_ok,
    assert_citations_resolve,
    market_jsons,
    market_pages,
    mock_stooq,
    run_section,
    table_blocks,
)

NEWS_PAGE = (
    "# Lockheed Martin tops Q2 estimates, lifts outlook\n\n"
    "Lockheed Martin reported second-quarter EPS of $7.94 versus the $7.62 analysts expected, "
    "and raised its 2026 sales outlook to $79.75-$81.75 billion. Record backlog of $230 billion.\n"
)


def test_parse_release_rows():
    p = parse_release(html_to_text(FAKE_RELEASE_HTML))
    assert p["columns"] == ["Q2-2026", "Q2-2025"]
    assert p["rows"]["revenue"] == [20_100e6, 18_155e6]
    assert p["rows"]["eps_diluted"] == [7.94, 1.46]
    assert p["rows"]["net_income"] == [1_830e6, 342e6]
    assert p["rows"]["operating_cash_flow"] == [3_200e6, 201e6]
    assert p["rows"]["segment_operating_profit"] == [2_150e6, 570e6]
    assert p["backlog"][0] == 230e9
    assert p["outlook_year"] == 2026 and str(p["release_date"]) == "2026-07-23"
    sales = next(o for o in p["outlook"] if o["metric"] == "sales")
    assert (sales["cur_low"], sales["cur_high"], sales["prior_low"], sales["prior_high"]) == (
        79_750e6,
        81_750e6,
        77_500e6,
        80_000e6,
    )
    assert sales["cur_label"] == "Current Update" and sales["prior_label"] == "April 2026"
    eps = next(o for o in p["outlook"] if o["metric"] == "eps")
    assert (eps["cur_low"], eps["cur_high"]) == (29.95, 30.65) and eps["unit"] == "USD/shares"
    segs = {s["segment"]: s["value"] for s in p["segment_sales"]}
    assert segs.get("Aeronautics") == 8.1e9 and segs.get("MFC") == 3.9e9
    assert norm_quarter("Q2 2026") == "Q2-2026" and norm_quarter("6/30/2026") == "Q2-2026"
    assert norm_quarter("2026-09-28") == "Q3-2026" and norm_quarter("junk") is None


def test_operating_with_market_adapter(edgar_mock, firecrawl_mock, output_dir):
    mock_stooq(edgar_mock)
    fc = firecrawl_mock(
        jsons=market_jsons(),
        pages={**market_pages(), "defensenews.com/lmt-q2": NEWS_PAGE},
        search={
            "results vs expectations": [
                {
                    "url": "https://www.defensenews.com/lmt-q2",
                    "title": "Lockheed Martin tops Q2 estimates",
                    "description": "",
                }
            ]
        },
    )
    draft, st = run_section(10)
    store = EvidenceStore.for_run(st["run_dir"])
    md = draft["markdown"]
    assert draft["exhibit_present"] and draft["coverage"] == "full"
    assert_citations_resolve(draft, store)
    assert_budget_ok(draft, 10)
    claims = {(c["metric"], c["period"]): c for c in draft["claims"]}

    # KPI table: EPS actual cited to a tier-1 source, consensus to the dated Yahoo page, variance derived
    kpi_block = next(b for b in table_blocks(md) if "Consensus EPS" in b)
    q2 = next(line for line in kpi_block.splitlines() if line.startswith("| Q2-2026"))
    assert "7.94 [src_" in q2 and "7.62 [src_" in q2 and "2026-10-21" in q2
    eps = claims[("eps_diluted", "Q2-2026")]
    assert eps["value"] == 7.94 and all(store.get(s).tier == 1 for s in eps["source_ids"])
    rev = claims[("revenue", "Q2-2026")]
    assert rev["value"] == 20_100e6 and all(store.get(s).tier == 1 for s in rev["source_ids"])
    cons = claims[("eps_consensus", "Q2-2026")]
    assert cons["value"] == 7.62 and store.get(cons["source_ids"][0]).type == "market_data"
    var = claims[("eps_variance_pct", "Q2-2026")]
    assert (
        var["concept"] == "derived"
        and var["note"]
        and abs(var["value"] - (7.94 - 7.62) / 7.62 * 100) < 1e-9
    )
    assert claims[("eps_surprise_pct", "Q2-2026")]["value"] == 4.2
    assert claims[("backlog", "Q2-2026")]["value"] == 230e9

    # segment margins derived per segment per FY from the 10-K
    seg_block = next(b for b in table_blocks(md) if "Aeronautics" in b and "FY2025" in b)
    assert "| Segment | FY2025 | FY2024 | FY2023 |" in seg_block
    m = claims[("segment_margin_aeronautics", "FY2025")]
    assert (
        m["concept"] == "derived"
        and m["unit"] == "pure"
        and abs(m["value"] - 2_900 / 30_200) < 1e-9
    )
    assert store.get(m["source_ids"][0]).type == "10-K"
    assert claims[("segment_sales_space", "FY2023")]["value"] == 12_471e6

    # management outlook: FY2026 from the release dated 2026-07-23
    g = claims[("guidance_sales_low", "FY2026")]
    assert g["value"] == 79_750e6 and "2026-07-23" in g["note"]
    assert claims[("guidance_eps_high", "FY2026")]["value"] == 30.65
    assert claims[("guidance_sales_low_prior", "FY2026")]["value"] == 77_500e6
    assert "Management outlook, FY2026" in md and "release dated 2026-07-23" in md
    for c in draft["claims"]:
        if c["concept"] == "derived":
            assert c["note"], c["metric"]
    assert "Caveat:" in md
    assert any(call[0] == "search" for call in fc.calls)
    # each (metric, period) group is single-valued: no self-conflicts between XBRL and the release
    groups: dict[tuple[str, str], set[float]] = {}
    for c in draft["claims"]:
        groups.setdefault((c["metric"], c["period"]), set()).add(round(c["value"], 6))
    assert all(len(v) == 1 for v in groups.values())


def test_operating_without_adapter(edgar_mock, output_dir):
    draft, st = run_section(10)
    store = EvidenceStore.for_run(st["run_dir"])
    md = draft["markdown"]
    assert draft["coverage"] == "partial"
    assert "no market-data adapter (Firecrawl not configured)" in md
    assert draft["exhibit_present"]
    assert_citations_resolve(draft, store)
    kpi_block = next(b for b in table_blocks(md) if "Consensus EPS" in b)
    q2 = next(line for line in kpi_block.splitlines() if line.startswith("| Q2-2026"))
    assert "7.94 [src_" in q2 and "n/a (no adapter)" in q2
    assert not any(c["metric"] in ("eps_consensus", "eps_variance_pct") for c in draft["claims"])
    assert all(store.get(s).tier == 1 for c in draft["claims"] for s in c["source_ids"])
    assert re.search(r"\| Aeronautics \| .*FY", md) or "Aeronautics" in md
    assert draft["budget"]["searches"] == "0/3"
