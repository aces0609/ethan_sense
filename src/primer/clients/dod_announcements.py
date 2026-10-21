"""DoD daily contract announcements (defense.gov/News/Contracts): deterministic parsing.

The pages are fetched through Firecrawl (which registers them in the evidence store); this
module only turns a page's markdown into rows keyed by contract number so section 4 can join
them to USAspending PIIDs (dashes stripped) and cite the page's source id with the contract
number and announcement date as the government identifier (requirements §7 program rule).

`parse_announcements` is a pure function. The optional Firecrawl /agent path (budgets.yaml
`use_agent`) is only a fallback when the regexes find nothing; `rows_from_agent` maps its
output to the same row shape so the handler treats both paths alike.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from primer.sections.text_tools import money_in

CONTRACT_NO = re.compile(r"\(([A-Z0-9]{4,8}-\d{2}-[A-Z]-\d{4}[A-Z0-9]*)\)")
MODIFICATION = re.compile(r"modification\s*\((P[A-Z0-9]{4,6})\)", re.I)
AWARD_VERB = re.compile(
    r"\b(?:is|was|has been|have been|are|were|is being|has)\s+(?:being\s+)?awarded\b", re.I
)
RECIPIENT = re.compile(
    r"^(?P<name>[^,]{3,120}?)(?:,\s*(?P<loc>[^,]{2,60}?,\s*[A-Z][a-zA-Z .]{2,40}?))?,\s*(?:is|was|has been|have been|are|were|is being|has)\s+(?:being\s+)?awarded",
    re.I,
)
CONTRACT_TYPE = re.compile(
    r"\$[\d,.]+(?:\s*(?:million|billion))?\s+([a-z][a-z ,/-]{3,90}?)\s+(?:contract|modification|order|agreement)\b",
    re.I,
)
WORK = re.compile(
    r"Work will be performed (?:in|at|on|primarily in)\s+(.+?)(?:,?\s+(?:and|with)\s+(?:an\s+)?(?:is\s+)?expected|\.\s|\.$)",
    re.I | re.S,
)
LOCATION_PCT = re.compile(
    r"([A-Z][A-Za-z.' -]+(?:,\s*[A-Z][A-Za-z. ]+)?)\s*\((\d{1,3}(?:\.\d+)?)%\)"
)
COMPLETION = re.compile(
    r"expected to be complete[d]? (?:in|by|on)\s+([A-Z][a-z]{2,8}\.?(?:\s+\d{1,2},)?\s+\d{4})",
    re.I,
)
ACTIVITY = re.compile(r"^(?:The\s+)?(.{3,200}?),?\s+is the contracting activity", re.I)
# sentence split that does not break after single-letter abbreviations (U.S., D.C.)
SENTENCE_SPLIT = re.compile(r"(?<=[a-z\)]\.)\s+(?=[A-Z])")
OBLIGATED = re.compile(
    r"in the amount of \$([\d,]+(?:\.\d+)?)(?:\s*(million|billion))?[^.]*?will be obligated", re.I
)
DATE_IN_TITLE = re.compile(r"Contracts?\s+[Ff]or\s+([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})")
SERVICE_HEADING = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?([A-Z][A-Z .&'/-]{2,60}?)(?:\*\*)?\s*$")
MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

AGENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "announcements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "contract_number": {"type": "string"},
                    "recipient": {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "contract_type": {"type": "string"},
                    "description": {"type": "string"},
                    "work_locations": {"type": "string"},
                    "completion": {"type": "string"},
                    "contracting_activity": {"type": "string"},
                    "announcement_date": {"type": "string"},
                    "service": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["contract_number", "recipient", "amount_usd"],
            },
        }
    },
    "required": ["announcements"],
}


def normalize_contract_number(s: str | None) -> str:
    """PIID join key: upper case, dashes and spaces removed (N00019-26-C-0001 -> N0001926C0001)."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def parse_ap_date(text: str | None) -> date | None:
    """AP-style dates used on defense.gov: 'Oct. 15, 2026', 'Sept. 3, 2026', 'June 2, 2026'."""
    if not text:
        return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None


def announcement_date(markdown: str, title: str | None = None) -> date | None:
    for s in (title or "", markdown[:2000]):
        m = DATE_IN_TITLE.search(s)
        if m:
            d = parse_ap_date(m.group(1))
            if d:
                return d
    return None


def _paragraphs(markdown: str) -> list[tuple[str | None, str]]:
    """(service heading in force, paragraph) pairs; headings are the all-caps lines."""
    out: list[tuple[str | None, str]] = []
    service: str | None = None
    for block in re.split(r"\n\s*\n", markdown):
        text = " ".join(block.split())
        if not text:
            continue
        m = SERVICE_HEADING.match(block.strip())
        if m and not re.search(r"\d", text) and len(text) <= 60:
            service = m.group(1).strip()
            continue
        out.append((service, text))
    return out


def _work_locations(text: str) -> list[dict[str, Any]]:
    m = WORK.search(text)
    if not m:
        return []
    span = m.group(1)
    rows = [{"place": p.strip(" ;,"), "pct": float(pct)} for p, pct in LOCATION_PCT.findall(span)]
    if rows:
        return rows
    place = re.split(r",\s+and\s+is\s+expected|\.|;", span)[0].strip(" ,;")
    return [{"place": place, "pct": None}] if place else []


def parse_announcements(
    markdown: str,
    company_terms: list[str] | None = None,
    *,
    title: str | None = None,
    url: str | None = None,
    program_terms: dict[str, list[str]] | None = None,
    source_id: str | None = None,
) -> list[dict[str, Any]]:
    """Rows for every award paragraph that names one of `company_terms` (all paragraphs when
    none are given). Pure: no network, no registration. Each row carries the contract number
    (join key `piid`), the announced amount (USD), the contracting activity, work locations,
    the expected completion and the announcement date from the page title."""
    terms = [t.lower() for t in (company_terms or []) if t and t.strip()]
    ann_date = announcement_date(markdown, title)
    rows: list[dict[str, Any]] = []
    for service, para in _paragraphs(markdown):
        if not AWARD_VERB.search(para):
            continue
        low = para.lower()
        if terms and not any(t in low for t in terms):
            continue
        numbers = CONTRACT_NO.findall(para)
        amounts = money_in(para)
        rec = RECIPIENT.match(para)
        ctype = CONTRACT_TYPE.search(para)
        act = next(
            (
                m
                for s in SENTENCE_SPLIT.split(para)
                if "contracting activity" in s.lower()
                for m in [ACTIVITY.match(s.strip())]
                if m
            ),
            None,
        )
        comp = COMPLETION.search(para)
        mod = MODIFICATION.search(para)
        obl = OBLIGATED.search(para)
        obligated = None
        if obl:
            scale = {"million": 1e6, "billion": 1e9}.get((obl.group(2) or "").lower(), 1.0)
            obligated = float(obl.group(1).replace(",", "")) * scale
        programs: list[str] = []
        for prog, pterms in (program_terms or {}).items():
            if any(
                re.search(rf"(?<![A-Z0-9]){re.escape(t)}(?![A-Z0-9])", para, re.I) for t in pterms
            ):
                programs.append(prog)
        rows.append(
            {
                "contract_number": numbers[0] if numbers else None,
                "contract_numbers": numbers,
                "piid": normalize_contract_number(numbers[0]) if numbers else None,
                "modification": mod.group(1) if mod else None,
                "recipient": (rec.group("name").strip() if rec else None),
                "recipient_location": (
                    rec.group("loc").strip() if rec and rec.group("loc") else None
                ),
                "amount_usd": amounts[0][0] if amounts else None,
                "amount_text": amounts[0][1] if amounts else None,
                "contract_type": ctype.group(1).strip() if ctype else None,
                "service": service,
                "description": _description(para),
                "work_locations": _work_locations(para),
                "completion": comp.group(1) if comp else None,
                "contracting_activity": act.group(1).strip() if act else None,
                "obligated_at_award_usd": obligated,
                "announcement_date": ann_date.isoformat() if ann_date else None,
                "programs": programs,
                "url": url,
                "source_id": source_id,
                "path": "regex",
                "paragraph": para[:1200],
            }
        )
    return rows


def _description(para: str) -> str:
    """The sentence(s) after the award sentence, before the 'Work will be performed' sentence."""
    sents = SENTENCE_SPLIT.split(para)
    body = []
    for s in sents[1:]:
        if re.match(r"(Work will|Fiscal|The .*contracting activity|This contract was)", s):
            break
        body.append(s)
    return " ".join(body)[:400]


def rows_from_agent(
    data: dict[str, Any], url_source_ids: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Map a Firecrawl /agent result (AGENT_SCHEMA) to the parse_announcements row shape."""
    out = []
    for a in (data or {}).get("announcements") or []:
        num = a.get("contract_number")
        url = a.get("url")
        d = parse_ap_date(a.get("announcement_date")) or _iso(a.get("announcement_date"))
        out.append(
            {
                "contract_number": num,
                "contract_numbers": [num] if num else [],
                "piid": normalize_contract_number(num) if num else None,
                "modification": None,
                "recipient": a.get("recipient"),
                "recipient_location": None,
                "amount_usd": float(a["amount_usd"]) if a.get("amount_usd") is not None else None,
                "amount_text": None,
                "contract_type": a.get("contract_type"),
                "service": a.get("service"),
                "description": (a.get("description") or "")[:400],
                "work_locations": [{"place": a.get("work_locations"), "pct": None}]
                if a.get("work_locations")
                else [],
                "completion": a.get("completion"),
                "contracting_activity": a.get("contracting_activity"),
                "obligated_at_award_usd": None,
                "announcement_date": d.isoformat() if d else None,
                "programs": [],
                "url": url,
                "source_id": (url_source_ids or {}).get(url or ""),
                "path": "agent",
                "paragraph": "",
            }
        )
    return out


def _iso(s: str | None) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None
