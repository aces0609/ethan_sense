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


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
