"""Deterministic text heuristics over filing text (html_to_text output, cells joined by ' | ').

These give the keyless path real exhibits from the 10-K / DEF 14A and give the LLM path
pre-located evidence windows. Each helper is conservative: it returns nothing rather than
a guess, and every return value tells the caller where in the text it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NUM_CELL = re.compile(r"^\(?-?\$?\s*[\d,]+(?:\.\d+)?\)?%?$")
YEAR = re.compile(r"\b(20\d{2})\b")
SUFFIX = re.compile(
    r"\b(corp|corporation|inc|incorporated|co|company|ltd|plc|holdings?|group|limited|sa|nv|ag|se)\b\.?",
    re.I,
)
STOP = {
    "the",
    "and",
    "our",
    "we",
    "us",
    "in",
    "of",
    "for",
    "with",
    "united",
    "states",
    "u.s.",
    "government",
    "department",
    "defense",
    "company",
    "companies",
    "international",
    "other",
    "these",
    "such",
    "see",
    "item",
    "note",
    "net",
    "sales",
    "total",
    "aeronautics",
    "space",
    "systems",
    "products",
    "services",
}


def parse_number(cell: str) -> float | None:
    s = cell.strip()
    if not s or not NUM_CELL.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()$% ").replace(",", "")
    if not s or s in ("-", "—"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


@dataclass
class TableRow:
    label: str
    values: list[float]
    line_no: int


@dataclass
class SummingTable:
    rows: list[TableRow]
    total: TableRow
    columns: list[str] = field(default_factory=list)  # e.g. ["2025", "2024", "2023"]
    start_line: int = 0
    end_line: int = 0


def _row(line: str, line_no: int) -> TableRow | None:
    cells = [c.strip() for c in line.split("|")]
    cells = [c for c in cells if c not in ("", "$", "%", ")", "(")]
    if len(cells) < 3:
        return None
    label_cells = []
    values: list[float] = []
    for c in cells:
        v = parse_number(c)
        if v is None:
            if values:  # text after numbers: footnote marks, skip
                continue
            label_cells.append(c)
        else:
            values.append(v)
    if not label_cells or len(values) < 2:
        return None
    label = " ".join(label_cells).strip(" :")
    if len(label) > 90 or not re.search(r"[A-Za-z]", label):
        return None
    return TableRow(label=label, values=values, line_no=line_no)


def find_summing_tables(
    text: str,
    total_regex: str = r"^\s*total\b",
    min_rows: int = 2,
    max_rows: int = 12,
    tolerance: float = 0.02,
    limit: int = 5,
) -> list[SummingTable]:
    """Runs of consecutive numeric rows whose values sum to the following 'Total' row.
    Column labels are the most recent run of years seen above the table."""
    lines = text.splitlines()
    out: list[SummingTable] = []
    rx = re.compile(total_regex, re.I)
    run: list[TableRow] = []
    last_years: list[str] = []
    for i, line in enumerate(lines):
        years = YEAR.findall(line)
        row = _row(line, i)
        header_like = row is not None and all(1990 <= v <= 2100 for v in row.values)
        if len(years) >= 2 and (row is None or header_like):
            last_years = years[:6]
        if header_like:
            continue
        if row is None:
            if line.strip() and "|" not in line and run:
                run = []  # prose breaks a table
            continue
        if rx.search(row.label) and run:
            ncol = len(row.values)
            cand = [r for r in run if len(r.values) == ncol][-max_rows:]
            # try the longest suffix of the run that sums to the total in every column
            for k in range(len(cand), min_rows - 1, -1):
                seg = cand[-k:]
                ok = True
                for col in range(ncol):
                    s = sum(r.values[col] for r in seg)
                    t = row.values[col]
                    if abs(s - t) > max(abs(t) * tolerance, 1.0):
                        ok = False
                        break
                if ok:
                    out.append(
                        SummingTable(
                            rows=seg,
                            total=row,
                            columns=last_years[:ncol],
                            start_line=seg[0].line_no,
                            end_line=row.line_no,
                        )
                    )
                    break
            run = []
            if len(out) >= limit:
                break
            continue
        run.append(row)
    return out


def segment_table(text: str, segment_names: list[str] | None = None) -> SummingTable | None:
    """The first summing table that looks like segment revenue: 2-8 rows, a 'Total net sales'
    style total, and (when known) row labels that include the segment names."""
    for t in find_summing_tables(text, total_regex=r"^\s*total\b.*(sales|revenue)", max_rows=8):
        if len(t.rows) < 2:
            continue
        if segment_names:
            labels = " ".join(r.label.lower() for r in t.rows)
            hits = sum(1 for s in segment_names if s.lower().split()[0] in labels)
            if hits < max(1, len(segment_names) // 2):
                continue
        return t
    return None


def segment_names_from_text(text: str) -> list[str]:
    """Segment names from phrases like 'four business segments: A, B, C and D'."""
    m = re.search(
        r"(?:business|reportable|operating) segments?\s*[:\-–—]?\s*([A-Z][^.\n]{10,300}?)\.",
        text,
    )
    if not m:
        return []
    raw = re.split(r",\s*|\s+and\s+|;\s*", m.group(1))
    names = []
    for r in raw:
        r = re.sub(r"\s*\(.*?\)", "", r).strip(" .:")
        r = re.sub(r"^(and|the)\s+", "", r, flags=re.I)
        if 2 <= len(r) <= 60 and r[0].isupper() and not r.lower().startswith("we "):
            names.append(r)
    return names[:8]


OFFICER_LINE = re.compile(
    r"^\s*([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,4}(?:,? (?:Jr|Sr|II|III|IV)\.?)?)"
    r"\s*\(age\s+(\d{2})\)[,:]?\s+(.{4,160})$"
)


def exec_officer_rows(text: str) -> list[dict[str, str]]:
    """Rows of an executive-officer list: a table (name | age | title) or the paragraph
    layout 'Name (age NN), Title' used in 10-K Part I 'Information about our Executive
    Officers' sections."""
    out = []
    seen = set()
    for line in text.splitlines():
        m = OFFICER_LINE.match(line.strip())
        if m and 28 <= int(m.group(2)) <= 85:
            name, title = m.group(1).strip(), m.group(3).strip(" .")
            if name not in seen:
                seen.add(name)
                out.append({"name": name, "age": m.group(2), "title": title[:160]})
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 3:
            continue
        name, age = cells[0], cells[1]
        if not re.fullmatch(r"\d{2}", age) or not (28 <= int(age) <= 85):
            continue
        if not re.fullmatch(r"[A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,4}", name):
            continue
        title = " ".join(cells[2:])[:160]
        if name in seen or len(title) < 4:
            continue
        seen.add(name)
        out.append({"name": name, "age": age, "title": title})
    return out[:20]


def risk_factor_headings(item1a: str, limit: int = 15) -> list[str]:
    """Risk-factor headings: sentence-like lines of 40-300 chars that read as a risk."""
    out = []
    for para in re.split(r"\n\s*\n", item1a):
        p = " ".join(para.split())
        if not (40 <= len(p) <= 300) or not p.endswith((".", "!")):
            continue
        if not p[0].isupper() or "|" in p:
            continue
        if (
            re.search(
                r"\b(could|may|might|adversely|risk|depend|subject to|failure|unable|uncertain)\b",
                p,
                re.I,
            )
            and p.count(".") <= 2
        ):
            out.append(p)
        if len(out) >= limit:
            break
    return out


def paragraphs_with(
    text: str, keywords: list[str], window: int = 1, max_paragraphs: int = 6, min_len: int = 80
) -> list[str]:
    """Paragraphs containing all keywords (case-insensitive), with `window` neighbours."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    kws = [k.lower() for k in keywords]
    out: list[str] = []
    for i, p in enumerate(paras):
        low = p.lower()
        if len(p) >= min_len and all(k in low for k in kws):
            lo, hi = max(0, i - window), min(len(paras), i + window + 1)
            block = "\n\n".join(x for x in paras[lo:hi] if x)
            if block not in out:
                out.append(block)
            if len(out) >= max_paragraphs:
                break
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", " ".join(text.split())) if s]


def sentences_with(text: str, keywords: list[str], limit: int = 8) -> list[str]:
    kws = [k.lower() for k in keywords]
    out = []
    for s in sentences(text):
        low = s.lower()
        if all(k in low for k in kws) and 30 <= len(s) <= 400:
            out.append(s)
            if len(out) >= limit:
                break
    return out


MONEY = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(billion|million|thousand|bn|mn|m\b|b\b)?",
    re.I,
)
SCALE = {
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "thousand": 1e3,
}


def money_in(text: str) -> list[tuple[float, str]]:
    """Dollar amounts in a text as (usd value, matched text)."""
    out = []
    for m in MONEY.finditer(text):
        val = float(m.group(1).replace(",", ""))
        scale = SCALE.get((m.group(2) or "").lower(), 1.0)
        out.append((val * scale, m.group(0)))
    return out


GOV_REVENUE = re.compile(
    r"([^.]{0,240}\b(?:U\.?\s?S\.?|United States|federal)\s+[Gg]overnment[^.]{0,240}?(\d{1,3})\s?%[^.]{0,160}\.)",
    re.S,
)


def gov_revenue_disclosure(text: str) -> tuple[bool | None, str | None, float | None]:
    """Does the 10-K disclose US-government revenue as a share of sales? Returns
    (disclosed, sentence, percent). None when the text has no such sentence either way."""
    best: tuple[str, float] | None = None
    for m in GOV_REVENUE.finditer(text):
        sent = " ".join(m.group(1).split())
        if not re.search(r"\b(sales|revenue|revenues)\b", sent, re.I):
            continue
        pcts = [float(x) for x in re.findall(r"(\d{1,3})\s?%", sent)]
        pcts = [x for x in pcts if x <= 100]
        if not pcts:
            continue
        # prefer the percent that qualifies total revenue/sales ("46% of our total revenues"),
        # else the largest figure in the sentence
        tot = re.search(
            r"(\d{1,3})\s?%\s+of\s+(?:our\s+|its\s+|the\s+)?(?:total\s+|consolidated\s+|net\s+)*(?:revenues?|sales)",
            sent,
            re.I,
        )
        pct = float(tot.group(1)) if tot and float(tot.group(1)) <= 100 else max(pcts)
        if best is None or pct > best[1]:
            best = (sent, pct)
    if best:
        return True, best[0][:400], best[1]
    if re.search(
        r"(sales|revenue)[^.]{0,120}\b(U\.?S\.? Government|federal government)\b", text, re.I
    ):
        return True, None, None
    return None, None, None


def norm_company(name: str) -> str:
    s = re.sub(r"^\s*the\s+", "", name.lower())
    s = SUFFIX.sub("", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def competitor_candidates(text: str, self_name: str, limit: int = 40) -> list[str]:
    """Capitalized 1-3 word names near 'compet' in Item 1, excluding the company itself."""
    paras = paragraphs_with(text, ["compet"], window=0, max_paragraphs=8)
    me = norm_company(self_name).split()
    cands: dict[str, int] = {}
    for p in paras:
        for m in re.finditer(r"\b([A-Z][A-Za-z0-9&\-]+(?:\s+[A-Z][A-Za-z0-9&\-]+){0,2})\b", p):
            name = m.group(1)
            n = norm_company(name)
            if not n or n.split()[0] in STOP or (me and n.split()[0] == me[0]):
                continue
            if len(n) < 3:
                continue
            cands[name] = cands.get(name, 0) + 1
    return [k for k, _ in sorted(cands.items(), key=lambda kv: -kv[1])][:limit]


# F-35, PAC-3 MSE, C-130J and slashed designations such as F/A-18 or E/A-18G
DESIGNATION = re.compile(r"\b([A-Z]{1,4}(?:/[A-Z]{1,2})?-\d{1,3}[A-Z]{0,2}(?:\s?[A-Z]{1,4})?)\b")


def program_designations(text: str) -> dict[str, int]:
    """Military-style designations (F-35, PAC-3 MSE, C-130J) with counts."""
    out: dict[str, int] = {}
    for m in DESIGNATION.finditer(text):
        d = m.group(1).strip()
        if re.fullmatch(r"[A-Z]{1,4}(?:/[A-Z]{1,2})?-\d{1,3}[A-Z]{0,2}", d) or " " in d:
            base = re.match(r"[A-Z]{1,4}(?:/[A-Z]{1,2})?-\d{1,3}[A-Z]{0,2}", d).group(0)  # type: ignore[union-attr]
            out[base] = out.get(base, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def years_in(text: str) -> list[int]:
    return sorted({int(y) for y in YEAR.findall(text)})
