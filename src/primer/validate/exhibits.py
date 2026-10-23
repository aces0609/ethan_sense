"""Exhibit validators (review 2026-10-21 §9 item 2).

The auto-built tables are deterministic regex work over filing text, and the review found
three broken tables per primer: product rows whose "Key metric" is a numeral stripped from
a program name ("35 type aircraft" from "F-35 type aircraft"), peer rows filled with
10-K cover-page check-box text, an "Ownership concentration" block whose labels are
incentive-plan metric weights, transcript timestamps and scraped page chrome inside event
logs, and segment columns reading "n/a" while the prose names the segment.

`check_exhibits` inspects a section draft and lists the problems; `apply_exhibit_check`
drops every failing exhibit (its table or bullet block plus the title line directly above
it), keeps the prose, turns the coverage into `partial` and says in the coverage
blockquote which exhibit was dropped and why. The critic runs it on every draft; the gate
runs the inspection again over the assembled sections so nothing slips past a retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CITE = re.compile(r"\s*\[(?:src_\d{4}|\^\d+)\]")
COVERAGE_LINE = re.compile(r"^> Coverage: (?P<status>\w+)(?: — (?P<detail>.*?))?\.?\s*$")

BOILERPLATE = re.compile(
    r"Indicate by check mark|forward-looking statements?|Any forward-looking|Yes ☑|No ☐|"
    r"pursuant to Section 13 or (?:Section )?15\(d\)|speaks only as of the date|safe harbor|"
    r"well-known seasoned issuer|shell company \(as defined",
    re.I,
)
TIMESTAMP = re.compile(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}(?![\d:])")
CHROME_PHRASES = (
    "create free account",
    "follow your favorite stocks",
    "type topic tags categories",
    "skip to main content",
    "search log in",
    "sign in to continue",
    "subscribe now",
    "accept all cookies",
    "menu key points",
    "key points menu",
)
CHROME = re.compile("|".join(re.escape(p) for p in CHROME_PHRASES), re.I)

# "35 type aircraft (…F-35 type aircraft…)": a numeral, a noun, an optional quoted fragment
KEY_METRIC_CELL = re.compile(
    r"^(?P<n>\d{1,4})\s+(?P<noun>[A-Za-z][A-Za-z /-]{0,30}?)\s*(?:\((?P<quote>.*)\)|$)"
)

# ownership labels that are plan metrics, not holders
METRIC_LABEL = re.compile(
    r"^(?:sales|revenue|net sales|free cash flow|fcf|financial|strategic(?: & operational)?|"
    r"operational|plan\b[^a-z]*|relative tsr|tsr|roic|roe|eps|overall payout factor|payout factor|"
    r"payout|operating (?:profit|margin|income)|cash flow|bookings|backlog|segment operating profit|"
    r"margin|total|threshold|target|maximum|weight(?:ing)?|metric|performance|adjusted)\b",
    re.I,
)
INSTITUTION = re.compile(
    r"\b(?:inc|corp|corporation|group|llc|lp|l\.p\.|plc|ltd|limited|capital|advisors|advisers|"
    r"management|investors|investments|partners|trust|fund|funds|bank|asset|associates|holdings|"
    r"company|co|sa|ag|nv|se|street|blackrock|vanguard|fidelity|fmr|geode|wellington|invesco|"
    r"northern|dodge|cox|rowe|norges|ubs|morgan|jpmorgan|goldman|schwab|franklin|amundi|allianz|"
    r"mfs|prudential|nuveen|ameriprise|berkshire|as a group|directors|officers|family|foundation|"
    r"estate|pension|retirement|insurance|securities|research)\b",
    re.I,
)
PERSON = re.compile(
    r"^(?:[A-Z][a-z'\-]+|[A-Z]\.)(?:\s+(?:[A-Z][a-z'\-]+|[A-Z]\.|Jr\.?|Sr\.?|III|II|IV|de|van|von|da|la))+"
    r"\s*,?\s*(?:Jr\.?|Sr\.?|III|II|IV)?$"
)
OWNERSHIP_HEADER = re.compile(r"holder|owner|shareholder|investor", re.I)
SEGMENT_HEADER = re.compile(r"^\s*segment", re.I)
PRODUCT_HEADER = re.compile(r"product|program|name|item", re.I)
KEY_METRIC_HEADER = re.compile(r"key metric", re.I)
OWNERSHIP_BLOCK = re.compile(r"^\*\*Ownership concentration\*\*")


@dataclass
class Exhibit:
    """One table or bullet block in a draft, with the title line directly above it."""

    kind: str  # "table" | "ownership"
    title: str
    start: int  # first line index to drop (title line when attached)
    end: int  # last line index (inclusive)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)  # citation tokens stripped
    labels: list[str] = field(default_factory=list)  # ownership bullet labels


@dataclass
class ExhibitCheck:
    ok: bool
    problems: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    markdown: str
    tables_left: int


def _clean(cell: str) -> str:
    return CITE.sub("", cell).strip()


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_exhibits(markdown: str) -> list[Exhibit]:
    lines = markdown.splitlines()
    out: list[Exhibit] = []
    i = 0
    n = len(lines)

    def title_above(idx: int) -> tuple[str, int]:
        """(title, start index) for the heading or bold line directly above idx."""
        j = idx - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0:
            ln = lines[j].strip()
            if ln.startswith("###"):
                return ln.lstrip("#").strip(), j
            if ln.startswith("**") and len(ln) < 160:
                return ln.strip("*").split("**")[0].strip(), j
        return "", idx

    while i < n:
        ln = lines[i]
        if ln.startswith("|"):
            j = i
            while j + 1 < n and lines[j + 1].startswith("|"):
                j += 1
            block = lines[i : j + 1]
            headers = _split_row(block[0]) if block else []
            rows = [
                [_clean(c) for c in _split_row(r)]
                for r in block[2:]
                if not re.fullmatch(r"\|[\s:\-|]+\|", r.strip())
            ]
            title, start = title_above(i)
            out.append(Exhibit("table", title or "table", start, j, headers, rows))
            i = j + 1
            continue
        if OWNERSHIP_BLOCK.match(ln.strip()):
            j = i
            labels: list[str] = []
            while j + 1 < n and (
                lines[j + 1].lstrip().startswith("- ") or not lines[j + 1].strip()
            ):
                nxt = lines[j + 1].lstrip()
                if not nxt.strip():
                    # stop at a blank line followed by something that is not a bullet
                    if j + 2 < n and not lines[j + 2].lstrip().startswith("- "):
                        break
                    j += 1
                    continue
                j += 1
                body = _clean(nxt[2:])
                if ":" in body:
                    labels.append(body.split(":", 1)[0].strip())
            out.append(Exhibit("ownership", "Ownership concentration", i, j, labels=labels))
            i = j + 1
            continue
        i += 1
    return out


def _prose(markdown: str) -> str:
    return "\n".join(
        ln
        for ln in markdown.splitlines()
        if not ln.startswith(("|", ">", "#", "**")) and ln.strip()
    )


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s.strip()]


def _is_holder_label(label: str) -> bool:
    lab = label.strip().rstrip(":")
    if not lab or METRIC_LABEL.match(lab):
        return False
    return bool(INSTITUTION.search(lab) or PERSON.match(lab))


def _bare_key_metric(cell: str) -> str | None:
    m = KEY_METRIC_CELL.match(cell)
    if not m:
        return None
    n, noun, quote = m.group("n"), m.group("noun").strip(), m.group("quote") or ""
    if BOILERPLATE.search(quote):
        return "key metric is a footnote marker next to boilerplate"
    if re.search(rf"[A-Za-z0-9]-{re.escape(n)}\b", quote):
        return f"key metric '{n} {noun}' is a numeral stripped from a program name"
    if re.match(r"type aircraft", noun, re.I):
        return f"key metric '{n} {noun}' is a numeral stripped from a program name"
    if len(n) == 1 and noun[:1].isupper() and noun.split()[0].lower() in ("deliveries", "delivery"):
        return f"key metric '{n} {noun}' is a footnote marker, not a count"
    return None


def _segment_problems(ex: Exhibit, prose: str, segments: list[str]) -> list[dict[str, Any]]:
    seg_col = next((k for k, h in enumerate(ex.headers) if SEGMENT_HEADER.match(h)), None)
    if seg_col is None:
        return []
    prod_col = next(
        (k for k, h in enumerate(ex.headers) if k != seg_col and PRODUCT_HEADER.search(h)), None
    )
    known = {s.strip() for s in segments if s and s.strip()}
    for r in ex.rows:
        if seg_col < len(r) and r[seg_col].lower() not in ("n/a", "", "none"):
            known.add(r[seg_col])
    known = {s for s in known if len(s) > 2}
    if not known or prod_col is None:
        return []
    sents = _sentences(prose)
    problems = []
    for idx, r in enumerate(ex.rows):
        if seg_col >= len(r) or r[seg_col].lower() != "n/a" or prod_col >= len(r):
            continue
        prod = r[prod_col]
        if not prod or prod.lower() == "n/a":
            continue
        prod_rx = re.compile(rf"(?<![\w-]){re.escape(prod)}(?![\w-])")
        for s in sents:
            if not prod_rx.search(s):
                continue
            named = next((seg for seg in known if re.search(rf"\b{re.escape(seg)}\b", s)), None)
            if named:
                problems.append(
                    {
                        "exhibit": ex.title,
                        "row": idx,
                        "cell": prod,
                        "rule": "segment_na",
                        "reason": f"segment is n/a for {prod} while the text places it in {named}",
                    }
                )
                break
    return problems


def check_exhibits(markdown: str, *, segments: list[str] | None = None) -> list[dict[str, Any]]:
    """Every cell-level problem in the draft's exhibits: [{exhibit, row, cell, rule, reason}]."""
    problems: list[dict[str, Any]] = []
    prose = _prose(markdown)
    for ex in parse_exhibits(markdown):
        if ex.kind == "ownership":
            for lab in ex.labels:
                if not _is_holder_label(lab):
                    problems.append(
                        {
                            "exhibit": ex.title,
                            "row": ex.labels.index(lab),
                            "cell": lab,
                            "rule": "ownership_label",
                            "reason": f"'{lab}' is not a person or institution name",
                        }
                    )
            continue
        km_col = next((k for k, h in enumerate(ex.headers) if KEY_METRIC_HEADER.search(h)), None)
        is_ownership = any(OWNERSHIP_HEADER.search(h) for h in ex.headers[:1])
        for idx, row in enumerate(ex.rows):
            for k, cell in enumerate(row):
                if not cell:
                    continue
                if BOILERPLATE.search(cell):
                    problems.append(
                        {
                            "exhibit": ex.title,
                            "row": idx,
                            "cell": cell[:80],
                            "rule": "boilerplate",
                            "reason": "cell holds cover-page or forward-looking boilerplate",
                        }
                    )
                    continue
                if TIMESTAMP.search(cell):
                    problems.append(
                        {
                            "exhibit": ex.title,
                            "row": idx,
                            "cell": cell[:80],
                            "rule": "timestamp",
                            "reason": "cell holds a transcript timestamp",
                        }
                    )
                    continue
                if CHROME.search(cell):
                    problems.append(
                        {
                            "exhibit": ex.title,
                            "row": idx,
                            "cell": cell[:80],
                            "rule": "page_chrome",
                            "reason": "cell holds scraped page chrome",
                        }
                    )
                    continue
                if km_col is not None and k == km_col:
                    why = _bare_key_metric(cell)
                    if why:
                        problems.append(
                            {
                                "exhibit": ex.title,
                                "row": idx,
                                "cell": cell[:80],
                                "rule": "bare_key_metric",
                                "reason": why,
                            }
                        )
                        continue
                if is_ownership and k == 0 and not _is_holder_label(cell):
                    problems.append(
                        {
                            "exhibit": ex.title,
                            "row": idx,
                            "cell": cell[:80],
                            "rule": "ownership_label",
                            "reason": f"'{cell[:60]}' is not a person or institution name",
                        }
                    )
        problems.extend(_segment_problems(ex, prose, list(segments or [])))
    return problems


def _rewrite_coverage(lines: list[str], status: str, extra: list[str]) -> list[str]:
    """Set the coverage blockquote's status and append the dropped-exhibit notes."""
    for i in range(len(lines) - 1, -1, -1):
        m = COVERAGE_LINE.match(lines[i].strip())
        if not m:
            continue
        old_status = m.group("status")
        new_status = old_status if old_status in ("unavailable", "not_applicable") else status
        detail = (m.group("detail") or "").strip()
        parts = [d for d in [detail] if d and d != "brief satisfied"] + extra
        lines[i] = f"> Coverage: {new_status} — {'; '.join(parts)}."
        return lines
    body = [ln for ln in lines]
    while body and not body[-1].strip():
        body.pop()
    body += ["", f"> Coverage: {status} — {'; '.join(extra)}."]
    return body


def _bad_row_lines(ex: Exhibit, probs: list[dict[str, Any]], lines: list[str]) -> set[int] | None:
    """Line indexes of the failing rows of a table when the table can keep its other rows:
    every problem names a row, and at least one row has none. None means drop the whole
    exhibit (a bullet block, a problem with no row, or a table with no clean row left)."""
    if ex.kind != "table" or any(not isinstance(p.get("row"), int) for p in probs):
        return None
    first = next((i for i in range(ex.start, ex.end + 1) if lines[i].startswith("|")), None)
    if first is None:
        return None
    row_lines = [
        k
        for k in range(first + 2, ex.end + 1)
        if not re.fullmatch(r"\|[\s:\-|]+\|", lines[k].strip())
    ]
    bad = {p["row"] for p in probs}
    if not bad or max(bad) >= len(row_lines) or len(bad) >= len(row_lines):
        return None
    return {row_lines[r] for r in bad}


def apply_exhibit_check(
    markdown: str, coverage: str, *, segments: list[str] | None = None
) -> ExhibitCheck:
    """Drop every exhibit with a problem, keep the prose, mark coverage partial."""
    problems = check_exhibits(markdown, segments=segments)
    exhibits = parse_exhibits(markdown)
    if not problems:
        return ExhibitCheck(True, [], [], markdown, sum(1 for e in exhibits if e.kind == "table"))
    by_title: dict[str, list[dict[str, Any]]] = {}
    for p in problems:
        by_title.setdefault(p["exhibit"], []).append(p)
    lines = markdown.splitlines()
    drop: set[int] = set()
    dropped: list[dict[str, Any]] = []
    for ex in exhibits:
        probs = by_title.get(ex.title)
        if not probs:
            continue
        reasons = list(dict.fromkeys(p["reason"] for p in probs))
        bad_lines = _bad_row_lines(ex, probs, lines)
        if bad_lines is not None:
            # some rows are fine: drop the bad rows, keep the table
            dropped.append(
                {
                    "exhibit": ex.title,
                    "kind": "rows",
                    "rules": sorted({p["rule"] for p in probs}),
                    "reasons": reasons,
                    "cells": [p["cell"] for p in probs][:6],
                    "rows_dropped": len(bad_lines),
                }
            )
            drop.update(bad_lines)
            continue
        dropped.append(
            {
                "exhibit": ex.title,
                "kind": ex.kind,
                "rules": sorted({p["rule"] for p in probs}),
                "reasons": reasons,
                "cells": [p["cell"] for p in probs][:6],
            }
        )
        drop.update(range(ex.start, ex.end + 1))
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    # collapse runs of blank lines left by the removal
    out: list[str] = []
    for ln in kept:
        if not ln.strip() and out and not out[-1].strip():
            continue
        out.append(ln)
    extra = [
        (
            f"exhibit '{d['exhibit']}': {d['rows_dropped']} row{'s' if d['rows_dropped'] != 1 else ''} "
            f"dropped by the exhibit validator: {'; '.join(d['reasons'][:3])}"
            if d["kind"] == "rows"
            else f"exhibit '{d['exhibit']}' dropped by the exhibit validator: {'; '.join(d['reasons'][:3])}"
        )
        for d in dropped
    ]
    status = coverage if coverage in ("unavailable", "not_applicable") else "partial"
    out = _rewrite_coverage(out, status, extra)
    md = "\n".join(out).strip()
    left = sum(1 for e in parse_exhibits(md) if e.kind == "table")
    return ExhibitCheck(False, problems, dropped, md, left)


def check_exhibits_gate(
    sections_md: dict[int, str], segments: dict[int, list[str]] | None = None
) -> dict[str, Any]:
    """Gate form: {ok, problems: [{section, exhibit, cell, rule, reason}]} over every section."""
    problems = []
    for sec, md in sections_md.items():
        for p in check_exhibits(md, segments=(segments or {}).get(sec)):
            problems.append({"section": sec, **p})
    return {"ok": not problems, "problems": problems}
