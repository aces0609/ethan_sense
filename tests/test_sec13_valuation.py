"""Section 13 (Valuation) handler on fixtures: derived multiples, LTM/NTM separation,
dated price targets, peer table with n/a cells, and the no-adapter path."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from httpx import Response

from primer.config import section_budget
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.state import Inputs

MARKET = Path(__file__).parent / "fixtures" / "market"
STOOQ_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2025-10-20,455.0,460.0,452.0,458.10,1200000\n"
    "2026-04-15,440.0,446.0,438.0,441.30,1300000\n"
    "2026-10-16,470.0,475.0,468.0,472.10,1000000\n"
    "2026-10-19,472.0,480.0,470.0,478.55,1100000\n"
    "2026-10-20,478.0,481.0,475.0,480.25,1050000\n"
)


def _fx(name: str) -> dict:
    return json.loads((MARKET / name).read_text(encoding="utf-8"))


def market_jsons() -> dict[str, dict]:
    return {
        "finance.yahoo.com/quote/LMT/analysis": _fx("yahoo_analysis_LMT.json"),
        "stockanalysis.com/stocks/LMT/forecast": _fx("stockanalysis_forecast_LMT.json"),
        "stockanalysis.com/stocks/LMT/statistics": _fx("stockanalysis_statistics_LMT.json"),
        "tipranks.com/stocks/LMT/forecast": _fx("tipranks_forecast_LMT.json"),
        "finviz.com/quote.ashx?t=LMT": _fx("finviz_LMT.json"),
        "marketbeat.com/stocks/NYSE/LMT": _fx("marketbeat_LMT.json"),
    }


def market_pages() -> dict[str, str]:
    return {
        "finance.yahoo.com/quote/LMT/analysis": "# Lockheed Martin Corporation (LMT) Analysis\n\nAnalyst estimates table.\n",
        "stockanalysis.com/stocks/LMT/forecast": "# Lockheed Martin (LMT) Stock Forecast\n\nPrice target table.\n",
        "stockanalysis.com/stocks/LMT/statistics": "# Lockheed Martin (LMT) Statistics\n\nStatistics table.\n",
        "tipranks.com/stocks/LMT/forecast": "# LMT Stock Forecast | TipRanks\n\nConsensus table.\n",
        "finviz.com/quote.ashx?t=LMT": "# LMT Lockheed Martin Corp Finviz\n\nSnapshot.\n",
        "marketbeat.com/stocks/NYSE/LMT": "# Lockheed Martin (LMT) Forecast | MarketBeat\n\nRatings.\n",
    }


def mock_stooq(router) -> None:
    # the adapter upper-cases the ticker in the URL (s=LMT.us); peers get no route → n/a
    router.get(url__regex=r"(?i)^https://stooq\.com/q/d/l/\?s=lmt\.us&i=d$").mock(
        return_value=Response(200, text=STOOQ_CSV)
    )


def _state(inputs: Inputs) -> dict:
    run_id, run_dir = new_run(inputs)
    st = {"inputs": inputs.model_dump(mode="json"), "run_id": run_id, "run_dir": str(run_dir)}
    for node in (intake, resolve_entity, foundation_pack, plan_sections):
        st.update(node(st))
    return st


def run_section(section: int, sections: list[int] | None = None) -> tuple[dict, dict]:
    inputs = Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=sections or [section],
    )
    st = _state(inputs)
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
    return out["drafts"][section], st


def table_blocks(md: str) -> list[str]:
    """Each markdown table as one block (consecutive lines starting with '|')."""
    blocks, cur = [], []
    for line in md.splitlines():
        if line.startswith("|"):
            cur.append(line)
        elif cur:
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def assert_citations_resolve(draft: dict, store) -> None:
    for sid in set(re.findall(r"\[(src_\d{4})\]", draft["markdown"])):
        assert store.has(sid), sid
    for c in draft["claims"]:
        assert c["source_ids"], c["metric"]
        for sid in c["source_ids"]:
            assert store.has(sid), (c["metric"], sid)


def assert_budget_ok(draft: dict, section: int) -> None:
    b = section_budget(section)
    used_s, cap_s = (int(x) for x in draft["budget"]["searches"].split("/"))
    used_c, cap_c = (int(x) for x in draft["budget"]["scrapes"].split("/"))
    assert used_s <= cap_s == int(b["searches"]) and used_c <= cap_c == int(b["scrapes"])


def test_valuation_with_market_adapter(edgar_mock, firecrawl_mock, output_dir):
    mock_stooq(edgar_mock)
    fc = firecrawl_mock(
        jsons=market_jsons(),
        pages={
            **market_pages(),
            "defensenews.com/lmt-targets": "# Analysts lift Lockheed Martin price targets after Q2\n\nMorgan Stanley raised its target to $560 after the quarter.\n",
        },
        search={
            "price target": [
                {
                    "url": "https://www.defensenews.com/lmt-targets",
                    "title": "Analysts lift Lockheed Martin price targets after Q2",
                    "description": "",
                }
            ]
        },
    )
    draft, st = run_section(13)
    from primer.evidence.store import EvidenceStore

    store = EvidenceStore.for_run(st["run_dir"])
    md = draft["markdown"]
    assert draft["exhibit_present"]
    assert_citations_resolve(draft, store)
    assert_budget_ok(draft, 13)

    claims = {(c["metric"], c["period"]): c for c in draft["claims"]}
    # derived multiples: price 480.25 × 230,080,240 shares; EV = mc + 21,700 − 4,121 ($m)
    mc = claims[("market_cap", "2026-10-20")]
    assert abs(mc["value"] - 480.25 * 230_080_240) < 1
    ev = next(v for (m, _p), v in claims.items() if m == "enterprise_value")
    assert abs(ev["value"] - (mc["value"] + 21_700e6 - 4_121e6)) < 1
    eb = next(v for (m, _p), v in claims.items() if m == "ebitda")
    assert eb["value"] == 7_731e6 + 1_687e6 and eb["period"].startswith("FY2025")
    evx = next(v for (m, _p), v in claims.items() if m == "ev_ebitda_ltm_ratio")
    assert abs(evx["value"] - ev["value"] / eb["value"]) < 1e-9
    pe = next(v for (m, _p), v in claims.items() if m == "pe_ltm_ratio")
    assert abs(pe["value"] - 480.25 / 21.49) < 1e-9
    for c in draft["claims"]:
        if c["concept"] == "derived":
            assert c["note"], c["metric"]
    for m in (
        "market_cap",
        "enterprise_value",
        "ebitda",
        "ev_ebitda_ltm_ratio",
        "pe_ltm_ratio",
        "p_fcf_ltm_ratio",
        "ev_sales_ltm_ratio",
        "pe_ntm_ratio",
        "ev_sales_ntm_ratio",
    ):
        assert any(c["metric"] == m and c["concept"] == "derived" for c in draft["claims"]), m
    assert "derived" in md and "share price × shares outstanding" in md

    # LTM and NTM never share a table
    blocks = table_blocks(md)
    ltm_block = next(b for b in blocks if "EV/EBITDA (LTM)" in b)
    ntm_block = next(b for b in blocks if "Forward P/E" in b)
    assert (
        ltm_block is not ntm_block
        and "Forward P/E" not in ltm_block
        and "EV/EBITDA (LTM)" not in ntm_block
    )
    fpe = next(v for (m, _p), v in claims.items() if m == "pe_ntm_ratio")
    assert fpe["period"] == "FY2027" and abs(fpe["value"] - 480.25 / 32.8) < 1e-9

    # price targets: one row per aggregator, dated, cited, never averaged
    tgt_block = next(b for b in blocks if "StockAnalysis" in b and "TipRanks" in b)
    assert "2026-10-21" in tgt_block
    sa = claims[("price_target_mean_stockanalysis", "2026-10-21")]
    tr = claims[("price_target_mean_tipranks", "2026-10-21")]
    assert sa["value"] == 520.0 and tr["value"] == 515.5 and sa["source_ids"] != tr["source_ids"]
    assert not any(abs(c["value"] - (520.0 + 515.5) / 2) < 0.01 for c in draft["claims"])
    for c in draft["claims"]:
        if c["metric"].startswith("price_target_"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["period"])

    # peer table: a row per foundation peer; peers without companyfacts show n/a, no substitute
    peer_block = next(b for b in blocks if "Peer" in b or "(BA)" in b)
    peers = [p["ticker"] for p in st["foundation"]["peers"]]
    assert peers == ["BA", "GD", "NOC", "RTX"]
    for tk in peers:
        row = next(line for line in peer_block.splitlines() if f"({tk})" in line)
        assert row.count("n/a") >= 5, row
    assert "(LMT)" in peer_block
    assert not any(c["metric"].startswith("peer_") for c in draft["claims"])
    assert draft["coverage"] == "partial"
    assert "Caveat:" in md and "Stooq close on 2026-10-20" in md and "cover-page" in md
    assert any(call[0] == "search" for call in fc.calls)


def test_valuation_without_adapter_renders_xbrl_rows(edgar_mock, output_dir):
    draft, st = run_section(13)
    from primer.evidence.store import EvidenceStore

    store = EvidenceStore.for_run(st["run_dir"])
    md = draft["markdown"]
    assert draft["coverage"] == "partial"
    assert "no market-data adapter (Firecrawl not configured)" in md
    assert draft["exhibit_present"]
    assert_citations_resolve(draft, store)
    metrics = {c["metric"] for c in draft["claims"]}
    assert {
        "ebitda",
        "net_debt",
        "free_cash_flow",
        "total_debt",
        "cash",
        "shares_outstanding",
    } <= metrics
    assert not ({"market_cap", "enterprise_value", "pe_ltm_ratio"} & metrics)
    ltm_block = next(b for b in table_blocks(md) if "EBITDA" in b)
    assert "n/a (no quote)" in ltm_block and "EV/EBITDA (LTM)" in ltm_block
    assert all(store.get(s).tier == 1 for c in draft["claims"] for s in c["source_ids"])
    assert draft["budget"]["searches"] == "0/2"
