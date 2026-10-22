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


NAME = r"[A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,4}(?:,? (?:Jr|Sr|II|III|IV)\.?)?"
OFFICER_LINE = re.compile(r"^\s*(" + NAME + r")\s*\(age\s+(\d{2})\)[,:]?\s+(.{4,160})$")
# 'Robert K. Ortberg, 65, has been President and Chief Executive Officer since August 2024.'
OFFICER_PROSE = re.compile(
    r"^\s*(" + NAME + r"),\s+(?:age\s+)?(\d{2}),\s+"
    r"(?:has\s+(?:been|served\s+as)|is|was|became|serves\s+as|joined[^.]{0,80}?\bas)?\s*"
    r"(?:the\s+|our\s+|a\s+)?(?:Company's\s+)?([A-Z][^.;]{3,160}?)"
    r"(?=\s+(?:since|from|effective|beginning|in\s+(?:19|20)\d{2}|of\s+(?:the\s+)?(?:Company|Corporation|Registrant))\b"
    r"|,\s+(?:a\s+(?:position|role)|which|where|and\s+(?:has|had|was)|having)\b|[.;]|$)"
)
TITLE_TAIL = re.compile(r"\s+(?:since|from|effective|beginning)\s+[^.;|]*$", re.I)
OFFICER_HEADINGS = re.compile(
    r"Information\s+[Aa]bout\s+[Oo]ur\s+Executive\s+Officers|Executive\s+Officers\s+of\s+the\s+(?:Registrant|Company)"
    r"|Our\s+Executive\s+Officers|Information\s+[Aa]bout\s+Executive\s+Officers|Executive\s+Officers\b",
)
EMPLOYEE_SENTENCE = re.compile(r"\b(employees|employed|workforce|people|team members)\b", re.I)
EMPLOYEE_PREFERRED = re.compile(
    r"(?:approximately|about|roughly|nearly|over|more than|total of)\s+([\d,]{4,})\s+"
    r"(?:full[- ]time\s+|regular\s+|active\s+|total\s+)?(?:employees|people|team members|workers)"
    r"|(?:total\s+)?(?:workforce|headcount)\s+(?:was|of|totaled|totalled|numbered)\s+"
    r"(?:approximately|about|roughly|nearly|over|more than)?\s*([\d,]{4,})",
    re.I,
)
# a subsidiary or acquired unit counted on its own is not the company-wide figure
SUBSIDIARY_WORDS = re.compile(
    r"\b(subsidiary|subsidiaries|acquired|acquisition|joint venture|Spirit employs|segment employs)\b",
    re.I,
)
EMPLOYEE_ANY = re.compile(
    r"([\d,]{4,})\s+(?:full[- ]time\s+|regular\s+|active\s+|total\s+)?(?:employees|people|team members|workers)"
    r"|(?:employed|employs?|workforce of|headcount of)\s+(?:approximately|about|roughly|nearly|over|more than)?\s*([\d,]{4,})",
    re.I,
)
UNION_WORDS = re.compile(
    r"\b(union|unions|local\s+\d+|represented by|collective bargaining|bargaining unit|IAM|SPEEA|UAW|"
    r"machinists|guild|labor agreement|labour agreement|strike)\b",
    re.I,
)


def _parse_count(s: str) -> int | None:
    try:
        v = int(s.replace(",", ""))
    except ValueError:
        return None
    return v if 1_000 <= v <= 5_000_000 else None


def human_capital_passage(item1: str, max_chars: int = 20000) -> str:
    """The Human Capital / employees passage of 10-K Item 1: from a 'Human Capital' or
    'Employees' heading to the next heading-like line, else the whole Item 1 text."""
    m = re.search(
        r"(?im)^(?:#+\s*)?(?:\*\*)?\s*(?:Human Capital(?: Resources| Management)?|Employees(?: and Human Capital)?|Our (?:People|Workforce)|Workforce)\s*(?:\*\*)?\s*$",
        item1,
    )
    if not m:
        m = re.search(r"Human Capital", item1)
    if not m:
        return item1
    start = m.start()
    tail = item1[start : start + max_chars]
    nxt = re.search(r"(?m)^\s*(?:Item\s+\d{1,2}[A-C]?\b|Item 1A)", tail[200:])
    if nxt:
        tail = tail[: 200 + nxt.start()]
    return tail


def employee_count(item1: str) -> tuple[int, str] | None:
    """(count, sentence) for the company-wide employee count: read from the Human Capital
    passage of Item 1 (whole text as a fallback), the sentence that names employees with the
    largest number, preferring 'approximately N employees'. Sentences about a union local,
    a bargaining unit or a strike are never used (review 2026-10-21 §5 item 6)."""
    for scope in (human_capital_passage(item1), item1):
        preferred: list[tuple[int, str]] = []
        any_: list[tuple[int, str]] = []
        for s in sentences_safe(scope):
            if not EMPLOYEE_SENTENCE.search(s):
                continue

            # "U.S." keeps the splitter from ending a sentence, so a workforce sentence can
            # be glued to the union sentence after it: judge each match by the text around
            # it (review 2026-10-21 §5 item 6), not by the whole sentence.
            def clean(m: re.Match, s: str = s) -> bool:
                window = s[max(0, m.start() - 90) : m.end() + 60]
                return not (UNION_WORDS.search(window) or SUBSIDIARY_WORDS.search(window))

            for m in EMPLOYEE_PREFERRED.finditer(s):
                v = _parse_count(m.group(1) or m.group(2) or "")
                if v and clean(m):
                    preferred.append((v, s))
            for m in EMPLOYEE_ANY.finditer(s):
                v = _parse_count(m.group(1) or m.group(2) or "")
                if v and clean(m):
                    any_.append((v, s))
        for cands in (preferred, any_):
            if cands:
                v, s = max(cands, key=lambda t: t[0])
                return v, s[:400]
    return None


def exec_officer_section(text: str, window: int = 15000) -> tuple[str, str] | None:
    """(section text, heading) of the executive-officer list in a 10-K: the 'Information
    about our Executive Officers' heading (Part I) or the Item 10 heading whose following
    window yields the most officer rows; None when no heading is followed by any."""
    best: tuple[int, str, str] | None = None
    for m in OFFICER_HEADINGS.finditer(text):
        chunk = text[m.start() : m.start() + window]
        n = len(exec_officer_rows(chunk))
        if n and (best is None or n > best[0]):
            best = (n, chunk, m.group(0))
    if best is None:
        return None
    return best[1], best[2]


def exec_officer_rows(text: str) -> list[dict[str, str]]:
    """Rows of an executive-officer list in any of the layouts filers use: a table
    (name | age | title, or 'name, age | title'), the paragraph layout 'Name (age NN), Title'
    and the prose layout 'Name, NN, has been Title since ...' (10-K Part I 'Information
    about our Executive Officers', Item 10, or the proxy)."""
    out = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        m = OFFICER_LINE.match(line)
        if m and 28 <= int(m.group(2)) <= 85:
            name, title = m.group(1).strip(), m.group(3).strip(" .")
            if name not in seen:
                seen.add(name)
                out.append({"name": name, "age": m.group(2), "title": title[:160]})
            continue
        if "|" not in line:
            m = OFFICER_PROSE.match(line)
            if m and 28 <= int(m.group(2)) <= 85:
                name, title = m.group(1).strip(), m.group(3).strip(" ,.")
                if name not in seen and len(title) >= 4:
                    seen.add(name)
                    out.append({"name": name, "age": m.group(2), "title": title[:160]})
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2:
            continue
        name, age = cells[0], cells[1] if len(cells) > 2 else ""
        rest = cells[2:]
        nm = re.fullmatch(r"(" + NAME + r"),\s*(?:age\s+)?(\d{2})", name)
        if nm:  # 'Name, 57 | Title'
            name, age, rest = nm.group(1), nm.group(2), cells[1:]
        if not re.fullmatch(r"\d{2}", age) or not (28 <= int(age) <= 85):
            continue
        if not re.fullmatch(NAME, name):
            continue
        title = " ".join(rest)
        first = re.split(r"(?<=[a-z0-9])\.\s+(?=[A-Z])", title, maxsplit=1)[0]
        title = TITLE_TAIL.sub("", first).strip(" .")[:160]
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


ABBREV = re.compile(
    r"\b(U\.S|U\.K|U\.N|D\.C|E\.U|Mr|Mrs|Ms|Dr|Jr|Sr|Inc|Corp|Co|Ltd|No|vs|e\.g|i\.e|St|Lt|Gen|Col|Adm)\.(?=\s)",
    re.I,
)


def sentences_safe(text: str) -> list[str]:
    """Sentence split that keeps 'U.S. Government', 'Mr. Taiclet', 'Inc. and' in one
    sentence: abbreviation periods are shielded before the split and restored after."""
    shielded = ABBREV.sub(lambda m: m.group(0)[:-1] + "\x00", " ".join(text.split()))
    return [s.replace("\x00", ".") for s in sentences(shielded)]


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


NON_US = re.compile(
    r"\b(non-?\s?U\.?\s?S\.?|international|foreign|outside (?:of )?the (?:U\.?\s?S\.?|United States)|"
    r"non-?\s?domestic|export)\b",
    re.I,
)
US_GOV = re.compile(
    r"\b(?:U\.?\s?S\.?|United States|federal)\s+[Gg]overnment\b|\bDo[Dd]\b|\bDepartment of Defense\b"
)
PCT_TOKEN = re.compile(r"(\d{1,3}(?:\.\d)?)\s?%")
CUSTOMER_HEADINGS = re.compile(
    r"(?im)^(?:#+\s*)?(?:\*\*)?\s*(?:Customers?|Major Customers?|Concentration(?:s)? of (?:Credit )?Risk|"
    r"Concentration|Segment (?:Information|Reporting)|Note \d{1,2}\s*[-–—]?\s*(?:Segment|Concentration)[^\n]*|"
    r"Disaggregation of Revenue|Revenue by Customer)\s*(?:\*\*)?\s*$"
)


def _pct_clause(sent: str, pos: int) -> str:
    """The clause a percent token belongs to: text from the previous clause break to the
    token (abbreviation periods such as 'U.S.' do not count as breaks)."""
    head = ABBREV.sub(lambda m: m.group(0)[:-1] + "\x00", sent)[:pos]
    cut = max(head.rfind(";"), head.rfind(", and "), head.rfind(". "))
    return (head[cut + 1 :] if cut >= 0 else head).replace("\x00", ".")


def customer_split(text: str) -> dict:
    """US-government and international shares of revenue from the 10-K, read from the
    customer / concentration / segment passages first and the whole text as a fallback.
    A percent whose clause names non-U.S., international or foreign customers is the
    international share even when the clause also mentions the U.S. government (FMS
    routed through it), never the government share (review 2026-10-21 §5 item 3).
    Returns {us_gov_pct, us_gov_sentence, international_pct, international_sentence,
    passage} with None where nothing is found."""
    out: dict = {
        "us_gov_pct": None,
        "us_gov_sentence": None,
        "international_pct": None,
        "international_sentence": None,
        "passage": None,
    }
    scopes: list[tuple[str, str]] = []
    for m in CUSTOMER_HEADINGS.finditer(text):
        scopes.append(
            (" ".join(m.group(0).split()).strip(" *#"), text[m.start() : m.start() + 12000])
        )
    scopes.append(("10-K", text))
    for label, scope in scopes:
        gov: list[tuple[float, str]] = []
        intl: list[tuple[float, str]] = []
        for sent in sentences_safe(scope):
            if not re.search(r"\b(sales|revenue|revenues)\b", sent, re.I):
                continue
            if not (US_GOV.search(sent) or NON_US.search(sent)):
                continue
            if len(sent) > 600:
                continue
            for pm in PCT_TOKEN.finditer(sent):
                pct = float(pm.group(1))
                if pct > 100:
                    continue
                clause = _pct_clause(sent, pm.start())
                after = sent[pm.end() : pm.end() + 80]
                if NON_US.search(clause) or re.match(
                    r"\s+(?:of[^.]{0,40}?)?(?:from|to)\s+(?:non-?\s?U\.?S\.?|international|foreign)",
                    after,
                    re.I,
                ):
                    intl.append((pct, sent))
                elif US_GOV.search(clause) or re.match(
                    r"\s+(?:of[^.]{0,60}?)?(?:from|to|with)\s+(?:the\s+)?(?:U\.?\s?S\.?|United States|federal)\s+[Gg]overnment",
                    after,
                ):
                    gov.append((pct, sent))
        if gov or intl:
            if gov:
                out["us_gov_pct"], out["us_gov_sentence"] = max(gov, key=lambda t: t[0])
                out["us_gov_sentence"] = out["us_gov_sentence"][:400]
            if intl:
                # the share of all sales, not the DoD sub-share: largest figure in its clause
                out["international_pct"], out["international_sentence"] = max(
                    intl, key=lambda t: t[0]
                )
                out["international_sentence"] = out["international_sentence"][:400]
            out["passage"] = label
            return out
    return out


def gov_revenue_disclosure(text: str) -> tuple[bool | None, str | None, float | None]:
    """Does the 10-K disclose US-government revenue as a share of sales? Returns
    (disclosed, sentence, percent) from `customer_split`; (True, None, None) when the text
    ties sales to the U.S. Government without a percent; None when it says nothing."""
    split = customer_split(text)
    if split["us_gov_pct"] is not None:
        return True, split["us_gov_sentence"], split["us_gov_pct"]
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
