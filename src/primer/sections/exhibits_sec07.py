"""Section 7 exhibit: the acquisition / divestiture table (requirements §6 row 7).

Deal rows come from deterministic patterns over filing and press text: a verb (acquire,
acquired, acquisition of, divestiture of, sold), a capitalised target name, and a dollar
value with a billion/million scale in the same sentence. The date is the month-day-year in
the sentence, else the year named in the sentence, else the filing or publication date
(labelled). Rows are merged by normalised target name; two sources that disagree on the
value keep both values and both claims so the assembler renders the conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from primer.sections.common import claim, md_table
from primer.sections.text_tools import norm_company
from primer.state import Claim

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_MONTH_NUM = {
    m[:3].lower(): i + 1
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    )
}
NAME = r"([A-Z][\w&'\- ]{2,60}?)"  # no period: a target never spans a sentence
ACQ_RE = re.compile(
    r"\b(agreement to acquire|agreed to acquire|completed the acquisition of|completed its acquisition of"
    r"|acquisition of|acquire|acquired|to purchase|purchased|merger with)\s+(?:all of\s+)?(?:the\s+)?"
    + NAME
    + r"\s+(for|from|in a|in an)\b[^.$]{0,80}?\$\s?([\d][\d.,]*)\s*(billion|million|bn|mn)\b"
)
# amount before the target: "we paid $360 million, in cash, for the acquisition of Rapid Solutions"
PAID_RE = re.compile(
    r"\b(paid|spent|invested)\s+\$\s?(?P<val>[\d][\d.,]*)\s*(?P<scale>billion|million|bn|mn)\b"
    r"[^.$]{0,60}?\b(?:for|on|in)\s+(?:the\s+)?(?:acquisition|purchase)\s+of\s+(?:all of\s+)?(?:the\s+)?"
    r"(?:[A-Z]\w+(?:'s|’s)\s+)?(?P<target>[A-Z][\w&'\-]*(?:\s+[A-Z][\w&'\-]*){0,5})(?:\s+(?:business|segment))?"
)
DIV_RE = re.compile(
    r"\b(divestiture of|divested|sold|sale of|completed the sale of|spun off|spin-off of)\s+"
    r"(?:our |its |the )?"
    + NAME
    + r"\s+(for|to)\b[^.$]{0,80}?\$\s?([\d][\d.,]*)\s*(billion|million|bn|mn)\b"
)
FULL_DATE_RE = re.compile(rf"\b({MONTHS})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})\b")
YEAR_RE = re.compile(r"\b(?:in|during|of)\s+(20\d{2})\b", re.I)
ANY_YEAR_RE = re.compile(r"\b(20\d{2})\b")
SEGMENT_RE = re.compile(r"\b(?:our |the |its )?([A-Z][A-Za-z&, ]{2,40}?)\s+segment\b")
RATIONALE_RE = re.compile(
    r"\b((?:which|that) (?:is|are|will|would|expands?|adds?|strengthens?|brings?|complements?)[^.;]{8,140}"
    r"|to (?:expand|strengthen|add|enhance|broaden|accelerate|support|complement|extend)[^.;]{5,140}"
    r"|expected to [^.;]{5,140})"
)
SCALE = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6}
GENERIC_TARGETS = {"company", "business", "businesses", "shares", "stock", "assets", "notes"}


@dataclass
class Deal:
    target: str
    value_usd: float
    status: str  # announced | completed | divested
    date: str  # ISO date, a year, or "n.d."
    date_basis: str  # "sentence" | "year" | "filing date" | "page date"
    segment: str
    rationale: str
    sentence: str
    source_id: str
    source_kind: str  # 8-K | 10-K | 10-Q | press | news | agent
    consideration: str = ""  # non-cash terms when no dollar value is stated ("389,289,254 shares")


@dataclass
class DealRow:
    key: str
    target: str
    status: str
    date: str
    date_basis: str
    segment: str
    rationale: str
    values: list[tuple[float, str, str]] = field(default_factory=list)  # (usd, sid, kind)
    source_ids: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    consideration: str = ""
    consideration_sid: str = ""  # the source that states it


@dataclass
class AcquisitionExhibit:
    markdown: str
    claims: list[Claim]
    source_ids: list[str]
    rows: list[DealRow]
    present: bool
    filing_rows: int
    notes: list[str]


def _iso_from_match(m: re.Match[str]) -> str | None:
    mon = _MONTH_NUM.get(m.group(1)[:3].lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(2))).isoformat()
    except ValueError:
        return None


def _sentence_at(text: str, start: int, end: int) -> tuple[str, int, str]:
    """(sentence containing [start, end), its start offset, the sentence after it)."""
    left = max(text.rfind(". ", 0, start), text.rfind("? ", 0, start), text.rfind("! ", 0, start))
    left = left + 2 if left >= 0 else 0
    right = text.find(". ", end)
    right = right + 1 if right >= 0 else len(text)
    nxt_end = text.find(". ", right + 1)
    nxt = text[right + 1 : (nxt_end + 1 if nxt_end >= 0 else min(len(text), right + 400))]
    return text[left:right].strip(), left, nxt.strip()


def _clean_target(name: str) -> str:
    name = name.strip(" ,;:'\"-")
    name = re.sub(r"\s+", " ", name)
    name = re.sub(
        r"\b(business|businesses|division|unit|operations)$", "", name, flags=re.I
    ).strip()
    return name


# A merger 8-K names the target with its short name and states the price in shares, never in dollars:
# "... and Anysphere, Inc. (\u201cCursor\u201d) entered into an Agreement and Plan of Merger".
MERGER_RE = re.compile(
    r"and (?P<target>[A-Z][A-Za-z0-9&.,\- ]{2,50}?(?:,? (?:Inc|Corp|Corporation|LLC|Ltd|Limited|"
    r"Holdings|plc|AG|N\.V))\.?)\s*(?:\(|\uff08)[\u201c\"][^\u201d\")]{2,40}[\u201d\"]\)?"
    r"\s*(?:,\s*)?(?:each )?entered into an? (?:Amended and Restated )?(?:Agreement and Plan of Merger|"
    r"merger agreement|Merger Agreement)",
)
SHARES_RE = re.compile(r"(?:an )?aggregate of (?P<n>[\d,]{5,}) shares", re.I)


def merger_deals(
    flat: str, source_id: str, source_kind: str, fallback_date: date | None
) -> list[Deal]:
    """Deals from an 8-K's merger-agreement sentence; the value stays unstated (shares are not
    dollars) and the share consideration, when the filing counts it, goes in its own field."""
    out: list[Deal] = []
    for m in MERGER_RE.finditer(flat):
        target = _clean_target(m.group("target"))
        if not target or norm_company(target) in GENERIC_TARGETS or len(target) < 3:
            continue
        sentence, _start, nxt = _sentence_at(flat, m.start(), m.end())
        done = bool(
            re.search(
                r"Item 2\.01|\bbecame effective\b|\b(?:has|have|was|were) (?:completed|consummated)\b"
                r"|\bcompleted the (?:merger|acquisition)\b",
                flat,
                re.I,
            )
        )
        fd = FULL_DATE_RE.search(sentence)
        if fd and _iso_from_match(fd):
            dt, basis = _iso_from_match(fd) or "n.d.", "sentence"
        elif fallback_date:
            dt, basis = fallback_date.isoformat(), "filing date"
        else:
            dt, basis = "n.d.", "none"
        sh = SHARES_RE.search(flat)
        out.append(
            Deal(
                target=target,
                value_usd=0.0,
                status="completed" if done else "announced",
                date=dt,
                date_basis=basis,
                segment="n/a",
                rationale="n/a",
                sentence=sentence[:400],
                source_id=source_id,
                source_kind=source_kind,
                consideration=f"{sh.group('n')} shares" if sh else "",
            )
        )
    return out


def extract_deals(
    text: str, source_id: str, source_kind: str, fallback_date: date | None, limit: int = 12
) -> list[Deal]:
    """Deterministic deal rows from one text; conservative (no value, no row)."""
    flat = " ".join(text.split())
    out: list[Deal] = []
    seen: set[tuple[str, float]] = set()
    if source_kind == "8-K":
        out.extend(merger_deals(flat, source_id, source_kind, fallback_date))
    for rx, kind in ((ACQ_RE, "acq"), (PAID_RE, "acq"), (DIV_RE, "div")):
        for m in rx.finditer(flat):
            if rx is PAID_RE:
                verb = "completed the acquisition of"
                target = _clean_target(m.group("target"))
                val_txt, scale_txt = m.group("val"), m.group("scale")
            else:
                verb = m.group(1).lower()
                target = _clean_target(m.group(2))
                val_txt, scale_txt = m.group(4), m.group(5)
            if not target or norm_company(target) in GENERIC_TARGETS or len(target) < 3:
                continue
            if target.split()[0].lower() in ("the", "a", "an", "our", "its", "additional"):
                continue
            try:
                value = float(val_txt.replace(",", "")) * SCALE[scale_txt.lower()]
            except (ValueError, KeyError):
                continue
            key = (norm_company(target), value)
            if key in seen:
                continue
            seen.add(key)
            sentence, sent_start, nxt = _sentence_at(flat, m.start(), m.end())
            before = sentence[: max(0, m.start() - sent_start)]  # sentence text before the verb
            after = sentence[max(0, m.end() - sent_start) :]  # sentence text after the value
            if kind == "div":
                status = "divested"
            elif verb.startswith(("agreement", "agreed", "to purchase")) or verb in (
                "acquire",
                "merger with",
            ):
                status = "announced"
            elif verb.startswith("completed") or verb in ("acquired", "purchased"):
                status = "completed"
            else:  # "acquisition of": decide from the sentence
                status = (
                    "completed"
                    if re.search(r"\b(completed|closed|consummated)\b", sentence, re.I)
                    else "announced"
                )
            if re.search(r"\b(expected to close|subject to regulatory|pending)\b", sentence, re.I):
                status = "announced" if status != "divested" else status
            fd = FULL_DATE_RE.search(sentence)
            if fd and _iso_from_match(fd):
                dt, basis = _iso_from_match(fd) or "n.d.", "sentence"
            else:
                yr = YEAR_RE.search(sentence) or ANY_YEAR_RE.search(before)
                if yr:
                    dt, basis = yr.group(1), "year"
                elif fallback_date:
                    dt, basis = (
                        fallback_date.isoformat(),
                        ("filing date" if source_kind in ("8-K", "10-K", "10-Q") else "page date"),
                    )
                else:
                    dt, basis = "n.d.", "none"
            seg = SEGMENT_RE.search(sentence) or SEGMENT_RE.search(nxt)
            segment = seg.group(1).strip() if seg else "n/a"
            rat = RATIONALE_RE.search(after) or RATIONALE_RE.search(nxt)
            rationale = " ".join(rat.group(1).split()).rstrip(",") if rat else "n/a"
            out.append(
                Deal(
                    target=target,
                    value_usd=value,
                    status=status,
                    date=dt,
                    date_basis=basis,
                    segment=segment,
                    rationale=rationale[:160],
                    sentence=sentence[:400],
                    source_id=source_id,
                    source_kind=source_kind,
                )
            )
            if len(out) >= limit:
                return out
    return out


def merge_deals(deals: list[Deal]) -> list[DealRow]:
    """Merge by normalised target; the first (filing) occurrence sets the descriptive cells."""
    rows: dict[str, DealRow] = {}
    for d in deals:
        key = norm_company(d.target)
        r = rows.get(key)
        if r is None:
            r = DealRow(
                key=key,
                target=d.target,
                status=d.status,
                date=d.date,
                date_basis=d.date_basis,
                segment=d.segment,
                rationale=d.rationale,
            )
            rows[key] = r
        else:
            # a more specific date or a non-empty segment/rationale fills a gap
            if r.date_basis != "sentence" and d.date_basis == "sentence":
                r.date, r.date_basis = d.date, d.date_basis
            elif (
                r.date_basis == d.date_basis
                and len(r.date) == len(d.date) == 10
                and d.date < r.date
            ):
                r.date = d.date  # the column is the announce date: the earliest filing says it
            if r.segment == "n/a" and d.segment != "n/a":
                r.segment = d.segment
            if r.rationale == "n/a" and d.rationale != "n/a":
                r.rationale = d.rationale
            if r.status == "announced" and d.status == "completed":
                r.status = "completed"
        if d.consideration and not r.consideration:
            r.consideration, r.consideration_sid = d.consideration, d.source_id
        if d.value_usd > 0 and not any(
            abs(v - d.value_usd) <= 0.005 * max(v, d.value_usd) for v, _, _ in r.values
        ):
            r.values.append((d.value_usd, d.source_id, d.source_kind))
        if d.source_id not in r.source_ids:
            r.source_ids.append(d.source_id)
            r.kinds.append(d.source_kind)
    return sorted(rows.values(), key=lambda r: r.date, reverse=True)


def fmt_value(v: float) -> str:
    if v >= 1e9:
        s = f"{v / 1e9:.2f}".rstrip("0").rstrip(".")
        return f"${s} billion"
    return f"${v / 1e6:,.0f} million"


FILING_KINDS = ("8-K", "10-K", "10-Q")


def acquisition_table(deals: list[Deal], section: int = 7) -> AcquisitionExhibit:
    rows = merge_deals(deals)
    claims: list[Claim] = []
    used: list[str] = []
    notes: list[str] = []
    table: list[list[str]] = []
    filing_rows = 0
    for r in rows:
        first_sid = r.source_ids[0]
        cite = f" [{first_sid}]"
        if any(k in FILING_KINDS for k in r.kinds):
            filing_rows += 1
        date_cell = r.date + (
            f" ({r.date_basis})" if r.date_basis not in ("sentence", "year") else ""
        )
        value_cells = []
        for v, sid, _kind in r.values:
            value_cells.append(f"{fmt_value(v)} [{sid}]")
            period = r.date[:4] if re.match(r"20\d{2}", r.date) else "n.d."
            slug = re.sub(r"[^a-z0-9]+", "_", r.key).strip("_")
            c = claim(
                section,
                f"deal_value_{slug}",
                v,
                period,
                [sid],
                unit="USD",
                note=f"{r.status}; {r.target}",
            )
            if c:
                claims.append(c)
        if not value_cells:
            first = r.consideration_sid or r.source_ids[0]
            value_cells.append(
                f"n/a (consideration: {r.consideration}) [{first}]"
                if r.consideration
                else f"n/a [{first}]"
            )
        elif r.consideration:
            value_cells.append(
                f"consideration: {r.consideration} [{r.consideration_sid or r.source_ids[0]}]"
            )
        if len(r.values) > 1:
            notes.append(f"{r.target}: sources differ on the value (both kept)")
        src_cell = "; ".join(f"{k} [{s}]" for s, k in zip(r.source_ids, r.kinds, strict=False))
        for s in r.source_ids:
            if s not in used:
                used.append(s)
        table.append(
            [
                r.target + cite,
                date_cell + cite,
                " / ".join(value_cells),
                r.status + cite,
                r.segment + (cite if r.segment != "n/a" else ""),
                r.rationale + (cite if r.rationale != "n/a" else ""),
                src_cell,
            ]
        )
    md = (
        md_table(
            [
                "Target",
                "Announce date",
                "Value",
                "Status",
                "Segment",
                "Rationale (source wording)",
                "Source",
            ],
            table,
            align_right_from=99,
        )
        if table
        else ""
    )
    return AcquisitionExhibit(
        markdown=md,
        claims=claims,
        source_ids=used,
        rows=rows,
        present=bool(table),
        filing_rows=filing_rows,
        notes=notes,
    )
