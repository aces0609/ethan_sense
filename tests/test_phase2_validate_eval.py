"""PLAN 2.7 / 2.8 / 2.9: key-takeaways check, conflict blockquotes, attribution, eval harness v1."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from primer.eval.harness import fact_overlap, scorecard, split_golden, split_sections
from primer.evidence.store import EvidenceStore
from primer.graph.main import inject_conflicts, run_primer
from primer.state import Inputs
from primer.validate.gate import run_all
from primer.validate.rules import check_attribution
from primer.validate.takeaways import check_takeaways


def _store(tmp_path):
    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 21))
    a = store.register(
        "Revenue was $75,048 million in 2025. Morgan Stanley maintains an Overweight rating.",
        type="10-K",
        publisher="LMT",
        title="10-K",
        url="https://www.sec.gov/Archives/x.htm",
    )
    b = store.register(
        "Jefferies estimates 2027 EPS of $31.",
        type="news",
        publisher="Paper",
        title="n",
        url="https://news.example.com/a",
    )
    return store, a.source_id, b.source_id


def test_takeaways_rejects_unsupported_claims(tmp_path):
    store, a, b = _store(tmp_path)
    drafts = {9: {"source_ids": [a], "markdown": f"Revenue was $75,048 million [{a}]."}}
    claims = [
        {
            "section": 9,
            "metric": "revenue",
            "value": 75_048e6,
            "period": "FY2025",
            "source_ids": [a],
        }
    ]
    bullets = [
        f"- Revenue reached $75,048 million in FY2025 [{a}]. (Section 9)",
        f"- Revenue reached $80,000 million in FY2025 [{a}]. (Section 9)",  # number not claimed
        f"- Revenue reached $75.0 billion [{b}]. (Section 9)",  # cites a source no section cites
        f"- Revenue reached $75,048 million [{a}]. (Section 4)",  # section not drafted
        f"- Revenue reached $75,048 million [{a}].",  # no tag
    ]
    r = check_takeaways(bullets, drafts, claims, store)
    assert r["kept"] == [bullets[0]]
    reasons = [x["reason"] for x in r["rejected"]]
    assert any("figures" in x for x in reasons)
    assert any("not cited" in x for x in reasons)
    assert any("not drafted" in x for x in reasons)
    assert any("Section N" in x for x in reasons)


def test_conflict_blockquote_lands_in_later_section_and_passes_gate(tmp_path):
    store, a, b = _store(tmp_path)
    claims = [
        {
            "section": 10,
            "metric": "build_rate_count",
            "value": 42,
            "unit": "count",
            "period": "Q2-2026",
            "source_ids": [a],
        },
        {
            "section": 12,
            "metric": "build_rate_count",
            "value": 47,
            "unit": "count",
            "period": "Q2-2026",
            "source_ids": [b],
        },
    ]
    md = {10: f"*Caveat: x.*\n\nRate 42 [{a}].", 12: f"*Caveat: y.*\n\nRate 47 [{b}]."}
    out = inject_conflicts(md, claims)
    assert "> Conflict: build rate count for Q2-2026 reported as 42" in out[12]
    assert "section 10" in out[12] and "section 12" in out[12]
    assert out[12].startswith("*Caveat: y.*\n\n> Conflict:")
    assert "> Conflict" not in out[10]
    # the gate treats a disclosed conflict as passing
    mdx = tmp_path / "p.mdx"
    mdx.write_text("---\ncompany: X\n---\n", encoding="utf-8")
    rep = run_all(
        mdx_text="---\ncompany: X\n---\n\nbody",
        mdx_path=mdx,
        sections_md=out,
        claims=claims,
        coverage={10: "full", 12: "full"},
        requested=[10, 12],
        store=store,
        as_of=date(2026, 10, 21),
    )
    assert rep.checks["numeric"]["ok"] is True and rep.checks["numeric"]["undisclosed"] == 0
    rep2 = run_all(
        mdx_text="---\ncompany: X\n---\n\nbody",
        mdx_path=mdx,
        sections_md=md,
        claims=claims,
        coverage={10: "full", 12: "full"},
        requested=[10, 12],
        store=store,
        as_of=date(2026, 10, 21),
    )
    assert rep2.checks["numeric"]["ok"] is False and 12 in rep2.section_notes


def test_attribution_requires_named_firm_in_cited_source(tmp_path):
    store, a, b = _store(tmp_path)
    ok_md = {
        12: f"Morgan Stanley maintains an Overweight rating [{a}]. Jefferies estimates 2027 EPS of $31 [{b}]."
    }
    assert check_attribution(ok_md, store)["ok"]
    bad_md = {
        12: f"Jefferies estimates 2027 EPS of $31 [{a}]. RBC argues the margin recovers [{b}]."
    }
    r = check_attribution(bad_md, store)
    assert not r["ok"] and {p["firm"] for p in r["problems"]} == {"Jefferies", "RBC"}


GOLDEN = """# Lockheed Martin Company Primer

## Key Takeaways
Lockheed Martin generated $75.0 billion of revenue in 2025 with 72% from the U.S. Government. Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur.

## Historical Financial Performance
Revenue grew from $65,984 million in 2022 to $75,048 million in 2025; net income was $5,017 million in 2025. James Taiclet is chairman. The F-35 and PAC-3 programs dominate. Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.
"""


def test_golden_split_and_fact_overlap():
    g = split_golden(GOLDEN)
    assert set(g) == {1, 9}
    ours = "## 1. Key Takeaways\n\n- Revenue $75,048 million [^1]. (Section 9)\n\n## 9. Historical Financial Performance\n\nRevenue was $75,048 million in FY2025 and $65,984 million in FY2022; net income $5,017 million. James Taiclet leads. F-35 is the largest program.\n"
    assert set(split_sections(ours)) == {1, 9}
    fo = fact_overlap(ours, GOLDEN)
    assert fo["overall"]["amounts"]["golden"] >= 3 and fo["overall"]["amounts"]["found"] >= 3
    assert (
        fo["overall"]["programs"]["found"] == 1
        and "pac-3" in fo["overall"]["programs"]["missing_sample"]
    )
    assert (
        "James Taiclet".lower() in {n.lower() for n in fo["overall"]["names"]["missing_sample"]}
        or fo["overall"]["names"]["found"] >= 1
    )
    assert "9" in fo["sections"]


def test_scorecard_template_mode_on_fixture_run(edgar_mock, output_dir, tmp_path):
    inputs = Inputs(
        company_name="Lockheed Martin", ticker="LMT", as_of_date=date(2026, 10, 21), sections=[1, 9]
    )
    result = run_primer(inputs)
    run_dir = Path(result["run_dir"])
    golden = tmp_path / "lmt.md"
    golden.write_text(GOLDEN, encoding="utf-8")
    sc = scorecard(run_dir, golden, Path("docs/golden/lmt_financials.json"))
    assert sc["judge_mode"] == "template"
    assert sc["section_scores"]["9"]["mean"] >= 3 and sc["all_sections_at_least_3"]
    assert sc["citations"]["ok"] and sc["citations"]["rate"] == 1.0
    assert sc["financials"]["exact_match_rate"] == 1.0
    assert sc["fact_overlap"]["overall"]["amounts"]["found"] >= 2
    assert sc["run"]["status"] in ("ok", "needs_review")
    json.dumps(sc, default=str)
