"""PLAN 1.9: footnote assignment, citation integrity, numeric conflicts, frontmatter."""

from __future__ import annotations

from primer.render.citations import assign_footnotes
from primer.validate.citations import check_citations, check_source_tokens_left
from primer.validate.frontmatter import check_frontmatter
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


def test_attribution_check_survives_initials_and_a_citation_at_the_end_of_the_line():
    """'CFO Kenneth L. Bedingfield said ...' was cut after the middle initial, leaving
    'Bedingfield said ...' with no citation although the bullet cites at its end."""
    from datetime import date

    from primer.evidence.store import EvidenceStore
    from primer.validate.rules import _SENTENCE_END, check_attribution

    assert _SENTENCE_END.split(
        "CFO Kenneth L. Bedingfield said capex rises. He reaffirmed it."
    ) == [
        "CFO Kenneth L. Bedingfield said capex rises.",
        "He reaffirmed it.",
    ]
    assert len(_SENTENCE_END.split("The U.S. Government paid. Next.")) == 2

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = EvidenceStore(d, as_of=date(2026, 10, 22))
        rec = store.register(
            "Kenneth L. Bedingfield, CFO, said capex would rise to 2.5% of sales.",
            type="earnings_call",
            publisher="Yahoo Finance",
            title="Q3 call",
            url="https://finance.yahoo.com/call",
        )
        sid = rec.source_id
        cited = f"- CFO Kenneth L. Bedingfield said capex would rise to 2.5% of sales. He reaffirmed guidance.[{sid}]"
        assert check_attribution({12: cited}, store)["ok"]
        uncited = "- CFO Kenneth L. Bedingfield said capex would rise to 2.5% of sales. He reaffirmed guidance."
        res = check_attribution({12: uncited}, store)
        assert not res["ok"] and res["problems"][0]["reason"] == "no citation"
