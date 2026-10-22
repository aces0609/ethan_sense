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
    "He",
    "She",
    "They",
    "We",
    "Executives",
    "Officers",
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
    # common sentence subjects that precede "rates", "sees", "expects" without being a firm
    "Production",
    "Delivery",
    "Deliveries",
    "Build",
    "Growth",
    "Margin",
    "Margins",
    "Pricing",
    "Demand",
    "Supply",
    "Inflation",
    "Interest",
    "Program",
    "Programs",
    "Capex",
    "Leverage",
    "Debt",
    "Equity",
    "Boeing",
    "Lockheed",
    "Airbus",
    "Commercial",
    "Defense",
    "Global",
    "Services",
    "Space",
    "Aeronautics",
    "Missiles",
    "Rotary",
}


# Press-reported figures of a private company (PLAN 3.4) and SEC Form D notices carry these
# metric prefixes. They are labelled unaudited and tier-tagged in the table, they are not
# audited statement metrics, so the tier rule does not ask for companyfacts; but they must
# still cite a fetched source.
PRESS_PREFIXES = ("press_", "form_d_")


def check_tier(claims: list[dict[str, Any]], store: EvidenceStore) -> dict:
    """A statement number cited only to tier-2 pages when companyfacts has the concept is flagged.

    Press-reported private-company datapoints (`press_*`, `form_d_*`) are exempt from the tier
    preference; one with no resolvable source id is reported in `press_unsourced` and fails
    the check."""
    statement_metrics = {m for stmt in concepts_config()["statements"].values() for m in stmt}
    flagged = []
    unsourced = []
    for c in claims:
        if c["metric"].startswith(PRESS_PREFIXES):
            sids = c.get("source_ids") or []
            if not sids or not all(store.has(s) for s in sids):
                unsourced.append(
                    {"metric": c["metric"], "period": c["period"], "section": c["section"]}
                )
            continue
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
    return {"ok": not flagged and not unsourced, "flagged": flagged, "press_unsourced": unsourced}


def _is_company_person(name: str, sections_md: dict[int, str]) -> bool:
    """True when `name` (one or two words) matches a person in the section 8 officer table or
    a CEO/CFO sentence anywhere in the primer, so "Ortberg said" is not a firm attribution."""
    words = name.split()
    if len(words) > 3:
        return False
    surname = words[-1].lower()
    text = sections_md.get(8, "")
    officer_rows = re.findall(r"^\| ([A-Z][^|]+?) \|", text, re.M)
    for row in officer_rows:
        row_words = re.sub(r"\[\^\d+\]|\[src_\d{4}\]", "", row).lower().replace(",", " ").split()
        if surname in row_words:
            return True
    joined = " ".join(sections_md.values())
    return bool(
        re.search(
            rf"\b{re.escape(words[-1])}\b[^.]{{0,80}}\b(?:CEO|CFO|COO|Chief|President|Chairman|Executive Vice President)",
            joined,
        )
        or re.search(
            rf"\b(?:CEO|CFO|COO|Chief [A-Za-z ]+Officer|President|Chairman)\b[^.]{{0,40}}\b{re.escape(words[-1])}\b",
            joined,
        )
    )


# a sentence ends at . ! ? followed by space, but not after a single capital initial ("L.") or
# the "U.S." style, which cut "CFO Kenneth L. Bedingfield said ..." in two on 2026-10-22
_SENTENCE_END = re.compile(r"(?<=[.!?])(?<!\b[A-Z]\.)\s+")


def _attribution_units(md: str):
    """(sentence, citations of its whole line): a bullet or paragraph that cites once at the end
    covers the sentences before the citation, as the footnote does in the rendered primer."""
    for line in md.split("\n"):
        if line.lstrip().startswith((">", "|", "#")):
            continue  # blockquotes, table rows and headings are not attributions
        line_sids = re.findall(r"\[(src_\d{4})\]", line)
        for sentence in _SENTENCE_END.split(line):
            yield sentence, line_sids


def check_attribution(sections_md: dict[int, str], store: EvidenceStore) -> dict:
    """'Firm notes/argues…' must cite a source whose text names that firm."""
    problems = []
    for sec, md in sections_md.items():
        for sentence, line_sids in _attribution_units(md):
            if sentence.lstrip().startswith((">", "|", "#")):
                continue  # blockquotes, table rows and headings are not attributions
            m = ATTRIBUTION.search(sentence)
            if not m or m.group(1).split()[0] in NOT_FIRMS:
                continue
            firm = m.group(1)
            if firm.split()[-1] in NOT_FIRMS and len(firm.split()) == 1:
                continue
            if _is_company_person(firm, sections_md):
                continue  # an officer quoted by name is management speaking, not a broker view
            sids = re.findall(r"\[(src_\d{4})\]", sentence) or line_sids
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
            # a person quoted with a title ("Vice President Jonathan Root said") is checked by
            # surname, since the source names the person, not the title phrase
            probe = firm.lower()
            if re.search(
                r"\b(vice president|president|analyst|director|managing|senior|chief|officer|partner|cfo|ceo|coo|cto|chairman|chair)\b",
                probe,
            ):
                probe = firm.split()[-1].lower()
            named = any(probe in store.text(s).lower() for s in sids if store.has(s))
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
    # a "> Conflict:" note cites the other section's sources by design; those are not section
    # 14's own evidence
    md = chr(10).join(
        ln for ln in sections_md.get(14, "").splitlines() if not ln.startswith("> Conflict:")
    )
    for sid in sorted(set(re.findall(r"\[(src_\d{4})\]", md))):
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
