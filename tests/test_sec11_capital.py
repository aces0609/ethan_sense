"""Section 11 (Capital Structure and Liquidity) handler on fixtures: debt table with
definition-labelled leverage rows, maturities and ratings from the filing text, capital
returns shared with section 9, and the keyless path."""

from __future__ import annotations

import re

from primer.evidence.store import EvidenceStore
from primer.sections.exhibits_sec11 import find_maturities, find_ratings, find_revolver
from tests.test_sec13_valuation import (
    assert_budget_ok,
    assert_citations_resolve,
    market_jsons,
    market_pages,
    mock_stooq,
    run_section,
    table_blocks,
)

FITCH_PAGE = (
    "# Fitch Affirms Lockheed Martin at 'A'; Outlook Stable\n\n"
    "Fitch Ratings - Chicago - 12 Aug 2026: Fitch Ratings has affirmed Lockheed Martin Corporation's "
    "Long-Term Issuer Default Rating (IDR) and senior unsecured notes at 'A'. The Rating Outlook is Stable. "
    "Fitch calculates EBITDA leverage of 2.3x on its own definition.\n"
)


def test_capital_structure_with_filings_and_rating_page(edgar_mock, firecrawl_mock, output_dir):
    mock_stooq(edgar_mock)
    fc = firecrawl_mock(
        jsons=market_jsons(),
        pages={**market_pages(), "fitchratings.com/research/lmt": FITCH_PAGE},
        search={
            "credit rating": [
                {
                    "url": "https://www.fitchratings.com/research/lmt",
                    "title": "Fitch Affirms Lockheed Martin at 'A'; Outlook Stable",
                    "description": "",
                }
            ]
        },
    )
    draft, st = run_section(11)
    store = EvidenceStore.for_run(st["run_dir"])
    md = draft["markdown"]
    assert draft["exhibit_present"] and draft["coverage"] == "full"
    assert_citations_resolve(draft, store)
    assert_budget_ok(draft, 11)

    claims = {(c["metric"], c["period"]): c for c in draft["claims"]}
    assert claims[("cash", "2025-12-31")]["value"] == 4_121e6
    td = claims[("total_debt", "2025-12-31")]
    assert td["value"] == 21_700e6 and td["concept"] == "derived" and td["note"]
    assert claims[("net_debt", "2025-12-31")]["value"] == 21_700e6 - 4_121e6
    gl = claims[("gross_leverage", "FY2025")]
    assert gl["unit"] == "x" and abs(gl["value"] - 21_700 / (7_731 + 1_687)) < 1e-9
    nl = claims[("net_leverage", "FY2025")]
    assert abs(nl["value"] - (21_700 - 4_121) / (7_731 + 1_687)) < 1e-9
    ic = claims[("interest_coverage_ratio", "FY2025")]
    assert abs(ic["value"] - 7_731 / 1_118) < 1e-9

    # leverage row names carry their definition
    debt_block = next(b for b in table_blocks(md) if "Gross debt / EBITDA" in b)
    gross_row = next(line for line in debt_block.splitlines() if "Gross debt / EBITDA" in line)
    assert "derived: total debt ÷ (FY operating income + FY D&A)" in gross_row
    net_row = next(line for line in debt_block.splitlines() if "Net debt / EBITDA" in line)
    assert "derived: net debt ÷" in net_row
    assert "Interest coverage (derived: FY operating income ÷ FY interest expense)" in debt_block

    # revolver from the 10-Q text, cited to the 10-Q
    rv = claims[("undrawn_revolver", "2026-06-28")]
    assert rv["value"] == 3.0e9
    assert store.get(rv["source_ids"][0]).type == "10-Q"

    # maturities parsed from the 10-K debt note
    assert claims[("notes_due_2030", "2030")]["value"] == 750e6
    assert claims[("notes_due_2055", "2055")]["value"] == 1.25e9
    mat_block = next(b for b in table_blocks(md) if "4.75%" in b)
    assert "2030" in mat_block and "2055" in mat_block

    # ratings: one row per agency, each cited to a source whose text names agency + rating
    rat_block = next(b for b in table_blocks(md) if "Moody's" in b and "Fitch" in b)
    for line in rat_block.splitlines()[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        agency, rating = cells[0], cells[1]
        sid = re.search(r"\[(src_\d{4})\]", rating).group(1)
        text = store.text(sid)
        assert agency.split("'")[0] in text and rating.split(" [")[0] in text, line
    assert "| Fitch | A [" in rat_block
    fitch_sids = {
        re.search(r"\[(src_\d{4})\]", line).group(1)
        for line in rat_block.splitlines()
        if line.startswith("| Fitch")
    }
    assert any(store.get(s).type == "rating_agency" for s in fitch_sids)

    # capital returns share metric names, values and accession sources with section 9
    dv = claims[("dividends_paid", "FY2025")]
    assert (
        dv["value"] == 3_131e6
        and dv["source_ids"]
        and store.get(dv["source_ids"][0]).gov_id == "0001628280-26-004195"
    )
    assert claims[("buybacks", "FY2025")]["value"] == 3_000e6
    assert "Caveat:" in md
    assert any(call[0] == "search" for call in fc.calls)


def test_capital_structure_keyless(edgar_mock, output_dir):
    draft, st = run_section(11)
    store = EvidenceStore.for_run(st["run_dir"])
    md = draft["markdown"]
    assert draft["exhibit_present"]
    assert draft["coverage"] == "full"  # filings and XBRL alone satisfy the required exhibit
    assert "Firecrawl not configured" in md
    assert_citations_resolve(draft, store)
    assert all(store.get(s).tier == 1 for c in draft["claims"] for s in c["source_ids"])
    metrics = {c["metric"] for c in draft["claims"]}
    assert {
        "cash",
        "total_debt",
        "net_debt",
        "gross_leverage",
        "net_leverage",
        "undrawn_revolver",
    } <= metrics
    rat_block = next(b for b in table_blocks(md) if "Moody's" in b)
    assert "A3" in rat_block and "A-" in rat_block and "Fitch" in rat_block
    assert draft["budget"]["searches"] == "0/3"


def test_text_parsers():
    assert find_revolver(
        "We have a $3.0 billion revolving credit facility that expires in 2029."
    ) == (
        3.0e9,
        "$3.0 billion revolving credit facility",
    )
    assert (
        find_revolver(
            "We have $3.0 billion of available capacity under our revolving credit facility."
        )[0]
        == 3.0e9
    )
    assert find_revolver("no facility here") is None
    mats = find_maturities(
        "including $750 million of 4.75% notes due 2030 and $1.25 billion of 5.25% notes due 2055. "
        "We repaid $500 million of 3.55% notes at maturity."
    )
    assert [(m["amount"], m["coupon"], m["year"]) for m in mats] == [
        (750e6, 4.75, 2030),
        (1.25e9, 5.25, 2055),
    ]
    r = find_ratings("Our senior unsecured debt is rated A- (S&P), A3 (Moody's) and A (Fitch).")
    assert {(x["agency"], x["rating"]) for x in r} == {
        ("S&P", "A-"),
        ("Moody's", "A3"),
        ("Fitch", "A"),
    }
    r = find_ratings("Our long-term debt is rated A- by S&P and A3 by Moody's.")
    assert {(x["agency"], x["rating"]) for x in r} == {("S&P", "A-"), ("Moody's", "A3")}
    r = find_ratings(
        "Fitch Ratings has affirmed the senior unsecured notes at 'A'. The Rating Outlook is Stable."
    )
    assert {(x["agency"], x["rating"]) for x in r} == {("Fitch", "A")}
    assert find_ratings("A rating agency commented on the sector.") == []
