"""Gate 1: every [^n] resolves to a definition; every definition is cited at least once."""

from __future__ import annotations

import re

ANY = re.compile(r"\[\^(\d+)\](:?)")
DEF = re.compile(r"^\[\^(\d+)\]:", re.MULTILINE)


def find_refs(md: str) -> set[str]:
    """Footnote references: every [^n] except a definition, which is [^n]: at a line start.
    A [^n]: in the middle of a line (a citation followed by a colon in prose) is a reference."""
    refs = set()
    for m in ANY.finditer(md):
        at_line_start = m.start() == 0 or md[m.start() - 1] == "\n"
        if m.group(2) == ":" and at_line_start:
            continue
        refs.add(m.group(1))
    return refs


def check_citations(md: str) -> dict:
    refs = find_refs(md)
    defs = set(DEF.findall(md))
    unresolved = sorted(refs - defs, key=int)
    unused = sorted(defs - refs, key=int)
    return {
        "ok": not unresolved and not unused,
        "refs": len(refs),
        "defs": len(defs),
        "unresolved": unresolved,
        "unused": unused,
    }


def check_source_tokens_left(md: str) -> dict:
    """No raw [src_XXXX] tokens may survive rendering."""
    left = re.findall(r"\[src_\d{4}\]", md)
    return {"ok": not left, "leftover_tokens": left[:10]}
