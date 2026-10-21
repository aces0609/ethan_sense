"""Gates 4-8: tier rule, attribution rule, recency, coverage, page budget."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from primer.config import concepts_config, section_budget
from primer.evidence.store import EvidenceStore
from primer.state import SECTION_TITLES

ATTRIBUTION = re.compile(
    r"\b([A-Z][A-Za-z&.'\-]+(?: [A-Z][A-Za-z&.'\-]+){0,3}) (notes|argues|estimates|expects|forecasts|believes|sees|wrote|writes|says|said|argued|estimated|maintains|rates)\b"
)
NOT_FIRMS = {
    "The",
    "This",
    "It",
    "Management",
    "Company",
    "Net",
    "Revenue",
    "Free",
    "Cash",
    "Operating",
    "Year",
    "At",
    "Capital",
    "Analysts",
    "Consensus",
    "Investors",
    "Guidance",
    "Backlog",
    "Sales",
    "Segment",
    "Coverage",
    "Conflict",
    "Caveat",
    "Section",
    "Table",
    "Note",
    "Item",
    "In",
    "On",
    "For",
    "As",
    "That",
    "These",
    "Those",
    "Each",
    "Both",
    "Street",
    "Wall",
}


def check_tier(claims: list[dict[str, Any]], store: EvidenceStore) -> dict:
    """A statement number cited only to tier-2 pages when companyfacts has the concept is flagged."""
    statement_metrics = {m for stmt in concepts_config()["statements"].values() for m in stmt}
    flagged = []
    for c in claims:
        if c["metric"] not in statement_metrics or not c.get("source_ids"):
            continue
        tiers = {store.get(s).tier for s in c["source_ids"] if store.has(s)}
        if tiers and 1 not in tiers:
            flagged.append(
                {
                    "metric": c["metric"],
                    "period": c["period"],
                    "section": c["section"],
                    "tiers": sorted(tiers),
                }
            )
    return {"ok": not flagged, "flagged": flagged}


def check_attribution(sections_md: dict[int, str], store: EvidenceStore) -> dict:
    """'Firm notes/argues…' must cite a source whose text names that firm."""
    problems = []
    for sec, md in sections_md.items():
        for sentence in re.split(r"(?<=[.!?])\s+|\n", md):
            if sentence.lstrip().startswith((">", "|", "#")):
                continue  # blockquotes, table rows and headings are not attributions
            m = ATTRIBUTION.search(sentence)
            if not m or m.group(1).split()[0] in NOT_FIRMS:
                continue
            firm = m.group(1)
            if firm.split()[-1] in NOT_FIRMS and len(firm.split()) == 1:
                continue
            sids = re.findall(r"\[(src_\d{4})\]", sentence)
            if not sids:
                problems.append(
                    {
                        "section": sec,
                        "firm": firm,
                        "reason": "no citation",
                        "sentence": sentence[:160],
                    }
                )
                continue
            named = any(firm.lower() in store.text(s).lower() for s in sids if store.has(s))
            if not named:
                problems.append(
                    {
                        "section": sec,
                        "firm": firm,
                        "reason": "cited source does not name the firm",
                        "sentence": sentence[:160],
                    }
                )
    return {"ok": not problems, "problems": problems}


def check_recency(sections_md: dict[int, str], store: EvidenceStore, as_of: date) -> dict:
    """Section 14 cites nothing older than as_of minus 12 months."""
    cutoff = as_of - timedelta(days=365)
    stale = []
    md = sections_md.get(14, "")
    for sid in set(re.findall(r"\[(src_\d{4})\]", md)):
        rec = store.get(sid) if store.has(sid) else None
        if rec and rec.published_date and rec.published_date < cutoff:
            stale.append({"source_id": sid, "published": rec.published_date.isoformat()})
    return {"ok": not stale, "stale": stale}


def check_coverage(coverage: dict[int, str], requested: list[int]) -> dict:
    missing = [s for s in requested if s not in coverage]
    unavailable = [s for s, st in coverage.items() if st == "unavailable"]
    return {
        "ok": not missing,
        "missing": missing,
        "unavailable": unavailable,
        "summary": {SECTION_TITLES[s]: coverage.get(s, "missing") for s in requested},
    }


def check_page_budget(sections_md: dict[int, str]) -> dict:
    over = []
    for sec, md in sections_md.items():
        prose = "\n".join(
            ln for ln in md.splitlines() if not ln.startswith("|") and not ln.startswith(">")
        )
        words = len(prose.split())
        pages = words / 450
        budget = float(section_budget(sec).get("pages", 1.5))
        if pages > budget * 1.5:
            over.append({"section": sec, "pages": round(pages, 2), "budget": budget})
    return {"ok": not over, "over": over}
