"""Review 2026-10-21 §9 item 1: the cross-section reconcile node.

Two fixture drafts: section 11 denies the revolver (prose sentence and a table cell),
section 14 states the $3.0 billion 364-day revolving credit agreement with a source. The
denial becomes a cross-reference. Two sections with different share prices get a conflict
line in the later section. Everything is recorded for run.json.
"""

from __future__ import annotations

from datetime import date

from primer.evidence.store import EvidenceStore
from primer.graph.main import reconcile
from primer.graph.reconcile import (
    build_registry,
    classify,
    extract_denials,
    reconcile_drafts,
)
from primer.runlog import RunLog


def _store(tmp_path):
    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 21))
    ids = []
    for i in range(6):
        rec = store.register(
            f"document {i}",
            type="8-K" if i < 3 else "news",
            publisher="LOCKHEED MARTIN CORP" if i < 3 else "Paper",
            title=f"doc {i}",
            url=f"https://www.sec.gov/Archives/edgar/data/936468/{i}/d.htm"
            if i < 3
            else f"https://news.example.com/{i}",
            tier=1 if i < 3 else 2,
        )
        ids.append(rec.source_id)
    return store, ids


def _drafts(ids):
    s0, s1, s2, s3, s4, s5 = ids
    sec11 = (
        "*Caveat: balance-sheet figures are the latest XBRL instant.*\n\n"
        "### Liquidity\n\n"
        f"Lockheed Martin reported cash of $3,791 million at 28 June 2026 [{s0}]. "
        "The revolving credit facility is not in the captured evidence. "
        "The current quarterly dividend rate is not in the captured evidence.\n\n"
        "### Debt\n\n"
        "| Item | Amount | Maturity |\n|---|---:|---|\n"
        f"| Senior notes | $20,538 million [{s0}] | 2027–2063 [{s0}] |\n"
        "| Undrawn revolving credit facility | n/a (not found in the filing text) | n/a |\n\n"
        "> Coverage: partial — brief satisfied."
    )
    sec14 = (
        "*Caveat: the log covers the trailing 12 months.*\n\n"
        f"On December 5, 2025, the company entered a $3.0 billion 364-day revolving credit agreement maturing December 4, 2026 [{s1}]. "
        f"On August 24, 2026, it replaced that facility with a $2.25 billion revolver [{s2}].\n\n"
        "> Coverage: full — brief satisfied."
    )
    sec12 = (
        "*Caveat: ratings are second-hand.*\n\n"
        f"The share price closed at $189.98 on 2026-10-20 per MarketBeat [{s3}]. "
        f"The quarterly dividend is $3.60 per share [{s4}].\n\n"
        "> Coverage: full — brief satisfied."
    )
    sec13 = (
        "*Caveat: multiples are computed in-agent.*\n\n"
        f"The share price used is $192.28 at the close of 2026-10-21 [{s5}].\n\n"
        "> Coverage: full — brief satisfied."
    )
    return {
        11: {"section": 11, "markdown": sec11, "coverage": "partial"},
        12: {"section": 12, "markdown": sec12, "coverage": "full"},
        13: {"section": 13, "markdown": sec13, "coverage": "full"},
        14: {"section": 14, "markdown": sec14, "coverage": "full"},
    }


def test_denial_extraction_and_classification():
    md = (
        "No PAC-3 production rate is disclosed in the evidence. "
        "The revolver is not in the captured evidence. "
        "Which CAGE codes map to it cannot be confirmed from the fetched evidence. "
        "The 10-K excerpt does not quantify the split of sales.\n"
        "| Undrawn revolving credit facility | n/a (not found in the filing text) |\n"
        "| Backlog | Q1-2026: not available in the sources |"
    )
    dens = extract_denials(3, md)
    attrs = {(d.where, d.attribute) for d in dens}
    assert ("prose", "production_rate") in attrs
    assert ("prose", "revolver") in attrs
    assert ("cell", "revolver") in attrs
    assert ("cell", "backlog") in attrs
    pac3 = next(d for d in dens if d.attribute == "production_rate")
    assert pac3.entity == "PAC3"
    assert classify("quarterly dividend") == "dividend"
    assert classify("FY2026 free cash flow guidance") == "guidance_fcf"
    assert classify("consensus price target") == "consensus_target"
    assert classify("market capitalization") == "market_cap"


def test_denial_becomes_cross_reference_to_section_with_the_fact(tmp_path):
    store, ids = _store(tmp_path)
    drafts = _drafts(ids)
    runlog = RunLog(tmp_path / "run")
    changed, record = reconcile_drafts(drafts, [], store, runlog)
    assert 11 in changed
    md11 = changed[11]["markdown"]
    # the prose denial is replaced by the cross-reference carrying section 14's citation
    assert "revolving credit facility is not in the captured evidence" not in md11
    assert (
        f"See section 14 (Recent Developments): On December 5, 2025, the company entered a $3.0 billion "
        f"364-day revolving credit agreement maturing December 4, 2026 [{ids[1]}]" in md11
    )
    # the table cell denial is replaced too, with the short form
    assert "n/a (not found in the filing text)" not in md11
    assert f"| Undrawn revolving credit facility | see section 14 [{ids[1]}] | n/a |" in md11
    # the dividend denial finds section 12's $3.60
    assert "dividend rate is not in the captured evidence" not in md11
    assert (
        f"See section 12 (Investment Thesis and Debates): The quarterly dividend is $3.60 per share [{ids[4]}]"
        in md11
    )
    # the prose around it is untouched
    assert f"cash of $3,791 million at 28 June 2026 [{ids[0]}]" in md11
    xrefs = {(x["section"], x["attribute"], x["from_section"]) for x in record["cross_references"]}
    assert (11, "revolver", 14) in xrefs and (11, "dividend", 12) in xrefs
    assert record["mode"] == "deterministic" and record["denials"] >= 3
    assert runlog.to_dict()["reconciliation"]["cross_references"]


def test_two_share_prices_produce_a_conflict_line_in_the_later_section(tmp_path):
    store, ids = _store(tmp_path)
    drafts = _drafts(ids)
    changed, record = reconcile_drafts(drafts, [], store, None)
    assert 13 in changed
    md13 = changed[13]["markdown"]
    assert md13.startswith(
        "*Caveat: multiples are computed in-agent.*\n\n> Conflict: share price reported as $189.98"
    )
    assert f"[{ids[3]}] (section 12) and $192.28 [{ids[5]}] (section 13); sources differ" in md13
    assert "> Conflict" not in changed.get(12, {}).get("markdown", drafts[12]["markdown"])
    conf = [c for c in record["conflicts"] if c["attribute"] == "share_price"]
    assert conf and conf[0]["sections"] == [12, 13] and conf[0]["placed_in"] == 13
    # the same values are not conflicted twice on a rerun
    again, _ = reconcile_drafts({**drafts, **changed}, [], store, None)
    assert again.get(13, {}).get("markdown", md13).count("> Conflict: share price") == 1


def test_numeric_claims_join_the_registry_and_answer_denials(tmp_path):
    store, ids = _store(tmp_path)
    drafts = {
        2: {
            "section": 2,
            "markdown": "GlobalData lists the company without a market capitalization figure; "
            "no market cap appears in the evidence.\n\n> Coverage: partial — x.",
            "coverage": "partial",
        },
        13: {
            "section": 13,
            "markdown": f"Market cap is in the table [{ids[5]}].",
            "coverage": "full",
        },
    }
    claims = [
        {
            "section": 13,
            "metric": "market_cap",
            "value": 127.3e9,
            "unit": "USD",
            "period": "2026-10-21",
            "source_ids": [ids[5]],
        }
    ]
    reg = build_registry(drafts, claims, lambda s: 2)
    assert any(f.kind == "claim" and f.attribute == "market_cap" for f in reg.facts)
    changed, record = reconcile_drafts(drafts, claims, store, None)
    assert (
        "See section 13 (Valuation): market capitalization $127.30 billion for 2026-10-21"
        in changed[2]["markdown"]
    )
    assert f"[{ids[5]}]" in changed[2]["markdown"]


def test_reconcile_node_merges_changed_drafts_and_records(tmp_path):
    store, ids = _store(tmp_path)
    state = {
        "run_dir": str(tmp_path / "run"),
        "as_of": "2026-10-21",
        "drafts": _drafts(ids),
        "claims": [],
    }
    out = reconcile(state)
    assert set(out["drafts"]) >= {11, 13}
    assert out["reconciliation"]["cross_references"]
    assert (tmp_path / "run" / "run.json").exists()
