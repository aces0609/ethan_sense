"""Chunk a 10-K / 10-Q (markdown or plain text) by Item heading (requirements §5 foundation_pack).

A company that has not filed a 10-K yet (a recent IPO) is chunked from its IPO prospectus, whose
sections carry the equivalent 10-K item numbers (see PROSPECTUS_SECTIONS), so every consumer of
`chunk_by_item` reads a prospectus without knowing it is one.

Keyword lookup over a few hundred chunks is all v1 needs; no vector index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A heading may sit in a table cell ("| ITEM 1. | BUSINESS."), which html_to_text renders with
# leading and inner pipes; L3Harris's 10-K is laid out that way, and the chunker found only
# "Item 16" in it (section 3 then had no Item 1). Pipes are skipped before the heading and
# stripped from the title.
ITEM_RE = re.compile(
    r"^[\s|]*(?:#+\s*)?(?:\*\*)?\s*(ITEM|Item)\s+(\d{1,2}[A-C]?)\s*[\.\:\-—–]?[\s|]*(.*?)\s*(?:\*\*)?[\s|]*$",
    re.MULTILINE,
)

MIN_CHUNK_CHARS = 400  # table-of-contents entries are shorter than this


@dataclass
class Chunk:
    item: str  # "1A"
    title: str
    text: str
    start: int
    end: int

    @property
    def label(self) -> str:
        return f"Item {self.item}" + (f". {self.title}" if self.title else "")


# Prospectus section headings (a whole line, upper case) and the 10-K item each one stands in for.
# The item code None marks a section that is not read but ends the one before it.
PROSPECTUS_SECTIONS: tuple[tuple[str, str | None], ...] = (
    ("PROSPECTUS SUMMARY", "S"),
    ("RISK FACTORS", "1A"),
    ("BUSINESS", "1"),
    ("MANAGEMENT'S DISCUSSION AND ANALYSIS", "7"),
    ("MANAGEMENT", "10"),
    ("EXECUTIVE COMPENSATION", "11"),
    ("PRINCIPAL STOCKHOLDERS", "12"),
    ("PRINCIPAL AND SELLING STOCKHOLDERS", "12"),
    ("CERTAIN RELATIONSHIPS AND RELATED PERSON TRANSACTIONS", "13"),
    ("DESCRIPTION OF CAPITAL STOCK", "5"),
    ("INDEX TO FINANCIAL STATEMENTS", "8"),
    ("USE OF PROCEEDS", None),
    ("DIVIDEND POLICY", None),
    ("CAPITALIZATION", None),
    ("DILUTION", None),
    ("SELECTED CONSOLIDATED FINANCIAL", None),
    ("UNDERWRITING", None),
    ("LEGAL MATTERS", None),
    ("EXPERTS", None),
    ("WHERE YOU CAN FIND MORE INFORMATION", None),
    ("SHARES ELIGIBLE FOR FUTURE SALE", None),
    ("MATERIAL U.S. FEDERAL INCOME TAX", None),
    ("SPECIAL NOTE REGARDING FORWARD-LOOKING STATEMENTS", None),
    ("INDUSTRY AND MARKET DATA", None),
)
_PROSPECTUS_SIGNS = ("PROSPECTUS SUMMARY", "RISK FACTORS", "USE OF PROCEEDS", "UNDERWRITING")


def _heading_key(line: str) -> str:
    """A candidate heading line, normalised: pipes and emphasis stripped, curly quotes plain."""
    s = line.strip().strip("|*# ").replace("’", "'").replace("�", "'")
    return re.sub(r"\s+", " ", s).upper()


def chunk_prospectus(text: str) -> list[Chunk]:
    """Chunks of an IPO prospectus keyed by the 10-K item each section stands in for. Empty when
    the text does not look like a prospectus. A table-of-contents line carries dot leaders or a
    page number, so only a bare heading line counts; the longest chunk per item wins."""
    heads: list[tuple[int, str | None, str]] = []
    pos = 0
    for line in text.split("\n"):
        start = pos
        pos += len(line) + 1
        if not line.strip() or len(line) > 140:
            continue
        key = _heading_key(line)
        for name, item in PROSPECTUS_SECTIONS:
            if key == name or (
                name == "MANAGEMENT'S DISCUSSION AND ANALYSIS" and key.startswith(name)
            ):
                heads.append((start, item, key.capitalize()))
                break
    if len({h[1] for h in heads if h[1]} & set(_PROSPECTUS_SIGNS_ITEMS)) < 3:
        return []
    best: dict[str, Chunk] = {}
    for i, (start, item, title) in enumerate(heads):
        if item is None:
            continue
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        if end - start < MIN_CHUNK_CHARS:
            continue
        c = Chunk(item=item, title=title, text=text[start:end], start=start, end=end)
        if item not in best or len(c.text) > len(best[item].text):
            best[item] = c
    return sorted(best.values(), key=lambda c: c.start)


_PROSPECTUS_SIGNS_ITEMS = ("S", "1A", "1", "7")


def looks_like_prospectus(text: str) -> bool:
    head = text[:200000].upper()
    return "PROSPECTUS" in head and sum(1 for k in _PROSPECTUS_SIGNS if k in text.upper()) >= 3


def chunk_by_item(text: str) -> list[Chunk]:
    matches = list(ITEM_RE.finditer(text))
    if len(matches) < 4 and looks_like_prospectus(text):
        chunks = chunk_prospectus(text)
        if chunks:
            return chunks
    if not matches:
        return [Chunk(item="", title="", text=text, start=0, end=len(text))]
    chunks: list[Chunk] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        if len(body) < MIN_CHUNK_CHARS:
            continue  # table-of-contents line
        chunks.append(
            Chunk(
                item=m.group(2).upper(),
                title=m.group(3).strip(" .|*"),
                text=body,
                start=start,
                end=end,
            )
        )
    # the same Item can appear twice (TOC that is long, then the body); keep the longest per item
    best: dict[str, Chunk] = {}
    for c in chunks:
        if c.item not in best or len(c.text) > len(best[c.item].text):
            best[c.item] = c
    return sorted(best.values(), key=lambda c: c.start)


def find_chunks(chunks: list[Chunk], *keywords: str) -> list[Chunk]:
    kws = [k.lower() for k in keywords]
    return [c for c in chunks if all(k in c.text.lower() for k in kws)]
