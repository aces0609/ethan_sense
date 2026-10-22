"""Chunk a 10-K / 10-Q (markdown or plain text) by Item heading (requirements §5 foundation_pack).

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


def chunk_by_item(text: str) -> list[Chunk]:
    matches = list(ITEM_RE.finditer(text))
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
