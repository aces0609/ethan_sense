"""Run every gate check and decide pass / needs_review, with per-section notes for retries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from primer.evidence.store import EvidenceStore
from primer.validate.citations import check_citations, check_source_tokens_left
from primer.validate.exhibits import check_exhibits_gate
from primer.validate.frontmatter import check_frontmatter
from primer.validate.numeric import check_numeric
from primer.validate.rules import (
    check_attribution,
    check_coverage,
    check_page_budget,
    check_recency,
    check_tier,
)


@dataclass
class ValidationReport:
    passed: bool
    checks: dict[str, Any]
    section_notes: dict[int, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "section_notes": {str(k): v for k, v in self.section_notes.items()},
        }


def run_all(
    *,
    md_text: str,
    sections_md: dict[int, str],
    claims: list[dict[str, Any]],
    coverage: dict[int, str],
    requested: list[int],
    store: EvidenceStore,
    as_of: date,
) -> ValidationReport:
    checks: dict[str, Any] = {
        "citations": check_citations(md_text),
        "tokens": check_source_tokens_left(md_text),
        "frontmatter": check_frontmatter(md_text),
        "numeric": check_numeric(claims),
        "tier": check_tier(claims, store),
        "attribution": check_attribution(sections_md, store),
        "recency": check_recency(sections_md, store, as_of),
        "coverage": check_coverage(coverage, requested),
        "page_budget": check_page_budget(sections_md),
        "exhibits": check_exhibits_gate(sections_md),
    }
    notes: dict[int, list[str]] = {}
    undisclosed = []
    for conf in checks["numeric"]["conflicts"]:
        later_md = sections_md.get(int(conf["later_section"]), "")
        label = conf["metric"].replace("_", " ")
        if "> Conflict:" in later_md and label in later_md:
            conf["disclosed"] = True
            continue
        conf["disclosed"] = False
        undisclosed.append(conf)
        notes.setdefault(conf["later_section"], []).append(
            f"numeric conflict on {conf['metric']} {conf['period']}: add a '> Conflict:' line citing both sources"
        )
    checks["numeric"]["undisclosed"] = len(undisclosed)
    checks["numeric"]["ok"] = not undisclosed
    for f in checks["tier"]["flagged"]:
        notes.setdefault(f["section"], []).append(
            f"{f['metric']} {f['period']} cites only tier-{f['tiers']} sources; cite companyfacts"
        )
    for p in checks["attribution"]["problems"]:
        notes.setdefault(p["section"], []).append(f"attribution to {p['firm']}: {p['reason']}")
    for o in checks["page_budget"]["over"]:
        notes.setdefault(o["section"], []).append(f"cut to {o['budget']} pages (now ~{o['pages']})")
    for p in checks["exhibits"]["problems"]:
        notes.setdefault(p["section"], []).append(
            f"exhibit '{p['exhibit']}' fails the exhibit validator ({p['rule']}): {p['reason']}"
        )

    hard = [
        "citations",
        "tokens",
        "frontmatter",
        "numeric",
        "tier",
        "attribution",
        "recency",
        "coverage",
        "page_budget",
        "exhibits",
    ]
    passed = all(checks[k]["ok"] for k in hard)
    return ValidationReport(passed=passed, checks=checks, section_notes=notes)
