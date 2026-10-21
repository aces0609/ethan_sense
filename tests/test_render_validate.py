"""PLAN 1.9: footnote assignment, citation integrity, numeric conflicts, frontmatter, MDX compile."""

from __future__ import annotations

import shutil

import pytest

from primer.render.citations import assign_footnotes
from primer.validate.citations import check_citations, check_source_tokens_left
from primer.validate.mdx_compile import SIDECAR, check_frontmatter, check_mdx_compiles
from primer.validate.numeric import check_numeric


def test_assign_footnotes_first_citation_order():
    secs = {9: "Revenue [src_0002] and cost [src_0001] [src_0002].", 1: "Takeaway [src_0002]."}
    out, order, cited_by = assign_footnotes(secs)
    assert order == ["src_0002", "src_0001"]
    assert out[1] == "Takeaway[^1]."
    assert out[9] == "Revenue[^1] and cost[^2][^1]."
    assert cited_by == {"src_0002": ["sec-1", "sec-9"], "src_0001": ["sec-9"]}


def test_citation_integrity():
    good = "text[^1] more[^2]\n\n[^1]: a\n[^2]: b\n"
    assert check_citations(good)["ok"]
    bad = "text[^1] more[^99]\n\n[^1]: a\n[^3]: c\n"
    r = check_citations(bad)
    assert not r["ok"] and r["unresolved"] == ["99"] and r["unused"] == ["3"]
    assert not check_source_tokens_left("leftover [src_0004]")["ok"]


def test_numeric_conflict_detection():
    claims = [
        {
            "section": 9,
            "metric": "revenue",
            "value": 71_043e6,
            "period": "FY2024",
            "source_ids": ["src_0001"],
        },
        {
            "section": 13,
            "metric": "revenue",
            "value": 71_000e6,
            "period": "FY2024",
            "source_ids": ["src_0007"],
        },
        {
            "section": 10,
            "metric": "build_rate_count",
            "value": 42,
            "period": "2026-Q2",
            "source_ids": ["src_0008"],
        },
        {
            "section": 12,
            "metric": "build_rate_count",
            "value": 47,
            "period": "2026-Q2",
            "source_ids": ["src_0011"],
        },
    ]
    r = check_numeric(claims)
    assert r["ok"] is False
    metrics = {c["metric"]: c for c in r["conflicts"]}
    assert "build_rate_count" in metrics and metrics["build_rate_count"]["later_section"] == 12
    assert "revenue" not in metrics  # 0.06% apart, inside the 0.5% tolerance


def test_frontmatter_check():
    ok = "---\ncompany: X\nas_of: '2026-10-21'\nrun_id: r\ngenerator_version: '0.1'\ncoverage: {}\nsource_limits: []\ndisclaimer: d\n---\n\nbody\n"
    assert check_frontmatter(ok)["ok"]
    assert not check_frontmatter("# no frontmatter\n")["ok"]
    assert check_frontmatter("---\ncompany: X\n---\n")["missing"]


@pytest.mark.skipif(
    not shutil.which("node") or not (SIDECAR.parent / "node_modules").exists(),
    reason="node sidecar not installed",
)
def test_mdx_compiles_and_rejects_jsx(tmp_path):
    good = tmp_path / "good.mdx"
    good.write_text(
        "---\ncompany: X\n---\n\n## 9. Section\n\nText[^1].\n\n| a | b |\n|---|---:|\n| 1 | 2 |\n\n[^1]: XBRL • SEC • https://www.sec.gov/x_y.htm\n",
        encoding="utf-8",
    )
    assert check_mdx_compiles(good)["ok"] is True
    bad = tmp_path / "bad.mdx"
    bad.write_text("## 9\n\nSee <https://www.sec.gov/>\n", encoding="utf-8")
    assert check_mdx_compiles(bad)["ok"] is False
