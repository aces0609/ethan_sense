"""Gate 1: every [^n] resolves to a definition; every definition is cited at least once."""

from __future__ import annotations

import re

REF = re.compile(r"\[\^(\d+)\](?!:)")
DEF = re.compile(r"^\[\^(\d+)\]:", re.MULTILINE)


def check_citations(mdx: str) -> dict:
    refs = set(REF.findall(mdx))
    defs = set(DEF.findall(mdx))
    unresolved = sorted(refs - defs, key=int)
    unused = sorted(defs - refs, key=int)
    return {
        "ok": not unresolved and not unused,
        "refs": len(refs),
        "defs": len(defs),
        "unresolved": unresolved,
        "unused": unused,
    }


def check_source_tokens_left(mdx: str) -> dict:
    """No raw [src_XXXX] tokens may survive rendering."""
    left = re.findall(r"\[src_\d{4}\]", mdx)
    return {"ok": not left, "leftover_tokens": left[:10]}
