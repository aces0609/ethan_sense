"""PLAN 1.7 / 1.8 / 1.10 / 1.11: the main graph end to end on fixtures, and the eval harness."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from primer.config import REPO_ROOT
from primer.eval.harness import compare_financials
from primer.graph.main import run_primer
from primer.state import Inputs


def test_phase1_run_on_fixtures(edgar_mock, output_dir):
    inputs = Inputs(
        company_name="Lockheed Martin", ticker="LMT", as_of_date=date(2026, 10, 21), sections=[1, 9]
    )
    result = run_primer(inputs)
    run_dir = Path(result["run_dir"])

    for name in ("primer.md", "sources.json", "run.json", "claims.json", "checkpoint.sqlite"):
        assert (run_dir / name).exists(), name
    assert (run_dir / "evidence" / "index.json").exists()

    md = (run_dir / "primer.md").read_text(encoding="utf-8")
    assert md.startswith("---\n")
    assert "## 1. Key Takeaways" in md and "## 9. Historical Financial Performance" in md
    assert "## Sources" in md
    assert "| Revenue | 65,984[^" in md
    assert "[src_" not in md

    v = result["validation"]
    assert (
        v["checks"]["citations"]["ok"]
        and v["checks"]["numeric"]["ok"]
        and v["checks"]["tier"]["ok"]
    )
    assert v["checks"]["coverage"]["summary"]["Historical Financial Performance"] == "full"
    assert v["passed"] is True

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["counters"]["edgar_requests"] >= 3
    assert "9" in run["critic_scores"] and run["critic_scores"]["9"]["passed"]
    assert run["meta"]["status"] in ("ok", "needs_review")

    sources = json.loads((run_dir / "sources.json").read_text(encoding="utf-8"))
    assert all(s["type"] == "api:edgar_xbrl" and s["tier"] == 1 for s in sources["sources"])

    # every section-9 claim cites a companyfacts record
    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    sec9 = [c for c in claims if c["section"] == 9 and c["concept"] != "derived"]
    assert sec9 and all(c["source_ids"] for c in sec9)

    # eval harness: golden FY2022-FY2024 match to the dollar
    score = compare_financials(run_dir, REPO_ROOT / "docs" / "golden" / "lmt_financials.json")
    assert score["exact_match_rate"] == 1.0 and score["every_number_cited"]


def test_unimplemented_section_is_marked_unavailable_not_dropped(
    edgar_mock, output_dir, monkeypatch
):
    from primer.graph import researcher

    # simulate a build without the section-7 handler: the section is emitted, never dropped
    monkeypatch.setitem(researcher.REGISTRY, 7, "primer.sections.sec07_does_not_exist")
    monkeypatch.delitem(researcher.HANDLERS, 7, raising=False)
    researcher.HANDLERS.pop(7, None)
    inputs = Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=[1, 7, 9],
    )
    result = run_primer(inputs)
    md = Path(result["md_path"]).read_text(encoding="utf-8")
    assert "## 7. Key Acquisition History" in md
    assert "> Coverage: unavailable" in md
    assert result["coverage"][7] == "unavailable"
