"""Minimal HTML → text for EDGAR filing documents when Firecrawl is not configured.

Inline-XBRL 10-K HTML is several MB; this keeps block structure (newlines on block tags),
drops script/style and hidden ix:header content, and collapses whitespace.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
SKIP_TAGS = {"script", "style", "head", "title"}


VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "col", "wbr", "area", "base", "source"}


class _TextExtractor(HTMLParser):
    """Hidden regions (ix:header, display:none) are tracked by element depth so the
    region ends with its own closing tag, not with the first closing tag of any kind."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._hidden = False
        self._hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if self._hidden:
            if t not in VOID_TAGS:
                self._hidden_depth += 1
            return
        if t in SKIP_TAGS:
            self._skip += 1
        attrd = dict(attrs)
        style = (attrd.get("style") or "").replace(" ", "").lower()
        if t == "ix:header" or "display:none" in style:
            self._hidden = True
            self._hidden_depth = 0
            return
        if t in BLOCK_TAGS:
            self.parts.append("\n")
        if t in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if self._hidden:
            if t in VOID_TAGS:
                return
            if self._hidden_depth == 0:
                self._hidden = False
            else:
                self._hidden_depth -= 1
            return
        if t in SKIP_TAGS and self._skip:
            self._skip -= 1
        if t in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip or self._hidden:
            return
        self.parts.append(data)


LEADER_LABEL = re.compile(r"^(.*?\S)\s*\.{4,}\s*$")
CELL_RULE = re.compile(r"^\|(?:\s\|)*$")
CELL_VALUE = re.compile(r"^\$?\s*\(?-?[\d,]+(?:\.\d+)?\)?%?$|^[—–-]$")


def rejoin_leader_tables(text: str) -> str:
    """Rebuild tables a filing lays out one cell per line.

    An IPO prospectus converted to HTML (SpaceX's 424B4) puts each cell of a statement or segment
    table on its own line: a label with dot leaders, then the values separated by lines holding
    only pipes. No table parser reads that. A label followed by one or more values becomes one
    pipe row, "| Label | 11,387 | 7,599 | 3,869 |", and consecutive rows stay adjacent so they read
    as one table. Text with no dot-leader label is returned unchanged."""
    lines = text.split("\n")
    if not any(LEADER_LABEL.match(ln) for ln in lines):
        return text
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        m = LEADER_LABEL.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j, values = i + 1, []
        while j < n:
            tok = lines[j].strip()
            if not tok or CELL_RULE.match(tok):
                j += 1
            elif CELL_VALUE.match(tok):
                values.append(tok)
                j += 1
            else:
                break
        if not values:
            out.append(lines[i])
            i += 1
            continue
        out.append("| " + m.group(1) + " | " + " | ".join(values) + " |")
        i = j
    return "\n".join(out)


PAGE_MARK = re.compile(r"^(?:Table of Contents|\d{1,3}|F-\d{1,3})$", re.I)
SENTENCE_END = (".", "!", "?", ":", "”", '"', ")")


def is_hard_wrapped(lines: list[str]) -> bool:
    """A document converted line by line from a printed page: most prose lines are about a
    printed line long (95 to 135 characters) and every line is its own paragraph."""
    prose = [ln for ln in lines if len(ln) >= 60 and not ln.startswith("|")]
    if len(prose) < 200:
        return False
    return sum(1 for ln in prose if 95 <= len(ln) <= 135) / len(prose) >= 0.35


def strip_page_marks(lines: list[str]) -> list[str]:
    """Drop the "Table of Contents" lines and the page numbers printed next to them."""
    nonblank = [i for i, ln in enumerate(lines) if ln]
    drop: set[int] = set()
    for pos, i in enumerate(nonblank):
        if lines[i].lower() == "table of contents":
            drop.add(i)
            for q in (pos - 1, pos + 1):  # the page number sits on either side of the mark
                if 0 <= q < len(nonblank) and PAGE_MARK.match(lines[nonblank[q]]):
                    drop.add(nonblank[q])
    return [ln for i, ln in enumerate(lines) if i not in drop]


def reflow_hard_wrapped(text: str) -> str:
    """Join the lines of a hard-wrapped document back into paragraphs.

    A prospectus converted to HTML keeps every printed line as its own paragraph and prints a
    page number and "Table of Contents" between pages, so every paragraph reader (competition
    text, liquidity, officer lists) saw one-line fragments. A line that fills the printed width
    and does not end a sentence, or is followed by a lower-case start, continues into the next.
    Table rows and short lines keep their own lines. The caller decides whether the document is
    hard-wrapped (`normalize_wrapped_document` does)."""
    lines = [ln.strip() for ln in text.split("\n")]
    paras: list[str] = []
    cur: str | None = None
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("|"):
            if cur is not None:
                paras.append(cur)
                cur = None
            paras.append(ln)
            continue
        if cur is None:
            cur = ln
            continue
        wraps = len(cur) >= 90 and not cur.endswith(SENTENCE_END)
        if wraps or ln[:1].islower():
            cur = cur + " " + ln
        else:
            paras.append(cur)
            cur = ln
    if cur is not None:
        paras.append(cur)
    out: list[str] = []
    for i, p in enumerate(paras):
        out.append(p)
        if i + 1 < len(paras) and not (p.startswith("|") and paras[i + 1].startswith("|")):
            out.append("")
    return "\n".join(out)


def normalize_wrapped_document(text: str) -> str:
    """The whole prospectus clean-up, in the order it needs: page marks out (a page number must
    not become a table value), leader-dot tables rebuilt, lines reflowed into paragraphs. A
    document that is not hard-wrapped keeps its text; only dot-leader tables are rebuilt."""
    lines = [ln.strip() for ln in text.split("\n")]
    wrapped = is_hard_wrapped([ln for ln in lines if ln])
    if wrapped:
        text = "\n".join(strip_page_marks(lines))
    text = rejoin_leader_tables(text)
    return reflow_hard_wrapped(text) if wrapped else text


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return normalize_wrapped_document(text.strip())
