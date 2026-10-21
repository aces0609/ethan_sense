"""source_id tokens → GFM footnote markers, numbered in first-citation order."""

from __future__ import annotations

import re

TOKEN = re.compile(r"\s?\[(src_\d{4})\]")


def assign_footnotes(
    sections: dict[int, str],
) -> tuple[dict[int, str], list[str], dict[str, list[str]]]:
    """Return (rewritten sections, ordered source_ids, cited_by: source_id -> ['sec-9', ...])."""
    order: list[str] = []
    index: dict[str, int] = {}
    cited_by: dict[str, list[str]] = {}
    out: dict[int, str] = {}

    for sec in sorted(sections):
        md = sections[sec]

        def repl(m: re.Match, sec=sec) -> str:
            sid = m.group(1)
            if sid not in index:
                order.append(sid)
                index[sid] = len(order)
            cited_by.setdefault(sid, [])
            tag = f"sec-{sec}"
            if tag not in cited_by[sid]:
                cited_by[sid].append(tag)
            return f"[^{index[sid]}]"

        out[sec] = TOKEN.sub(repl, md)
    return out, order, cited_by


def strip_tokens(md: str) -> str:
    return TOKEN.sub("", md)
