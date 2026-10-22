"""Section 14 exhibit: the dated event log, newest first (requirements §6 row 14).

Rows come from 8-K primary documents (event = the Item code's meaning plus the first
sentence of that Item's text), IR press releases whose URL carries a date, and news pages
with a published date. The recency rule (validation gate 6) is applied here: a row is
built only from a source whose `published_date` is inside the trailing 12 months, and
undated pages are listed in a note without a citation. The implication column quotes the
source's own sentence; it is never an assessment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from primer.sections.common import claim, md_table
from primer.sections.text_tools import sentences
from primer.state import Claim

ITEM_MAP = {
    "1.01": "material definitive agreement",
    "1.02": "termination of a material agreement",
    "2.01": "completion of acquisition or disposition",
    "2.02": "results of operations (earnings release)",
    "2.03": "creation of a direct financial obligation",
    "2.04": "triggering event on a financial obligation",
    "2.05": "exit or disposal costs",
    "2.06": "material impairment",
    "3.01": "listing notice",
    "3.02": "unregistered sale of equity",
    "4.01": "change in auditor",
    "4.02": "non-reliance on prior financials",
    "5.01": "change in control",
    "5.02": "officer or director change",
    "5.03": "charter or bylaw amendment",
    "5.07": "shareholder vote results",
    "5.08": "shareholder nominations",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}
URL_DATE = (
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
    re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)


@dataclass
class Event:
    date: date
    event: str
    source_type: str
    implication: str
    source_id: str
    kind: str  # 8-K | press | news


@dataclass
class EventLog:
    markdown: str
    claims: list[Claim]
    source_ids: list[str]
    events: list[Event]
    present: bool
    eightk_rows: int
    notes: list[str] = field(default_factory=list)


def date_in_url(url: str) -> date | None:
    for rx in URL_DATE:
        m = rx.search(url)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if date(2000, 1, 1) <= d <= date(2100, 1, 1):
            return d
    return None


def item_sentence(text: str, code: str) -> str | None:
    """First sentence of an 8-K Item's body: 'Item 1.01 <title>. <sentence>.'"""
    flat = " ".join(text.split())
    m = re.search(rf"Item\s+{re.escape(code)}\b\.?\s*([^.]{{0,120}}?\.)\s*(.*)", flat)
    if not m:
        return None
    body = m.group(2)
    nxt = re.search(r"\bItem\s+\d\.\d{2}\b", body)
    if nxt:
        body = body[: nxt.start()]
    sents = [s for s in sentences(body) if len(s) >= 25]
    if not sents:
        return None
    return sents[0][:240]


def eightk_event(text: str, items: str | None) -> tuple[str, str]:
    """(event label, implication sentence) from the item codes and the document text."""
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    substantive = [c for c in codes if c != "9.01"] or codes
    labels = [ITEM_MAP.get(c, f"Item {c}") for c in substantive]
    sentence = None
    for c in substantive:
        sentence = item_sentence(text, c)
        if sentence:
            break
    if sentence is None:
        sents = [
            s
            for s in sentences(" ".join(text.split()))
            if len(s) >= 40 and "Item" not in s[:6] and s.upper() != s
        ]
        sentence = sents[0][:240] if sents else "n/a"
    event = "8-K " + ", ".join(f"Item {c}" for c in substantive) + ": " + "; ".join(labels)
    return event, sentence


def first_body_sentence(md: str, skip_title: bool = True) -> str:
    lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
    if skip_title and lines and lines[0].startswith("#"):
        lines = lines[1:]
    keep = []
    for ln in lines:
        if ln.startswith(("#", "|", "!", "[", "-", "*")):
            continue
        words = ln.split()
        if len(words) < 6:
            continue  # navigation, buttons, bylines
        caps = sum(1 for w in words if w.isupper() and len(w) > 1)
        if caps > len(words) / 3:
            continue  # "CREATE FREE ACCOUNT Menu Key Points" style clutter
        if re.search(
            r"\b(sign in|subscribe|create free account|menu|key points|skip to)\b", ln, re.I
        ):
            continue
        keep.append(ln)
    body = " ".join(keep)
    for s in sentences(body):
        if len(s) >= 60 and len(s.split()) >= 8:
            return s[:240]
    return (body[:240] + "…") if body else "n/a"


def event_log(
    events: list[Event], as_of: date, section: int = 14, undated: list[str] | None = None
) -> EventLog:
    cutoff = as_of - timedelta(days=365)
    kept = [e for e in events if cutoff <= e.date <= as_of + timedelta(days=7)]
    dropped = len(events) - len(kept)
    kept.sort(key=lambda e: e.date, reverse=True)
    rows = []
    used: list[str] = []
    for e in kept:
        c = f" [{e.source_id}]"
        rows.append(
            [
                e.date.isoformat() + c,
                e.event + c,
                e.source_type + c,
                e.implication + c,
                f"[{e.source_id}]",
            ]
        )
        if e.source_id not in used:
            used.append(e.source_id)
    md = (
        md_table(
            ["Date", "Event", "Source type", "Implication (source wording)", "Source"],
            rows,
            align_right_from=99,
        )
        if rows
        else ""
    )
    claims: list[Claim] = []
    if kept:
        c = claim(
            section,
            "event_count_trailing_12m",
            len(kept),
            "trailing-12m",
            used,
            unit="count",
            note=f"events dated {kept[-1].date} to {kept[0].date}",
        )
        if c:
            claims.append(c)
    notes: list[str] = []
    if dropped:
        notes.append(
            f"{dropped} fetched items fall outside the trailing 12 months and are not logged"
        )
    if undated:
        notes.append("undated pages not in the log: " + "; ".join(t[:80] for t in undated[:6]))
    return EventLog(
        md, claims, used, kept, bool(rows), sum(1 for e in kept if e.kind == "8-K"), notes
    )
