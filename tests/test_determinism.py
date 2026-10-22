"""Static guard for the bug that broke exact replay on 2026-10-22.

Python randomises the iteration order of a set of strings per process (PYTHONHASHSEED). Section 13
built its evidence list with `[s for s in {c.source_ids[0] for c in claims}]`, so the model prompt
listed sources in a different order on every run, the evidence digest's size cap cut different
sources, and an offline replay in a new process found no recording for the prompt. Anything that
turns a set of strings into an ordered output (a prompt, a table, a list that is cited) must go
through `sorted(...)` or `dict.fromkeys(...)` (first-appearance order).

This test scans the source for the risky shapes. Mark a deliberate exception with
`# deterministic-ok` on the same line.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "primer"

# `for x in {…}` / `in {a for …}` / `in set(…)` / `list(set(…))` / `tuple(set(…))` / `[*set(…)]`
RISKY = [
    re.compile(r"\bfor\s+\w+(?:,\s*\w+)*\s+in\s+\{(?![^}]*:)"),  # a set literal or comprehension
    re.compile(r"\bin\s+set\("),
    re.compile(r"\b(?:list|tuple)\(\s*set\("),
    re.compile(r"\[\s*\*\s*set\("),
    re.compile(r"\[\s*\*\s*\{"),
]


def _risky_lines() -> list[str]:
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0] if "deterministic-ok" not in line else ""
            if not code.strip():
                continue
            if any(rx.search(code) for rx in RISKY):
                hits.append(f"{path.relative_to(SRC.parent.parent)}:{n}: {line.strip()}")
    return hits


def test_no_iteration_over_an_unordered_set_in_source():
    hits = _risky_lines()
    assert not hits, (
        "iterating a set of strings gives a different order in every process, which breaks "
        "exact replay (use sorted(...) or dict.fromkeys(...), or add '# deterministic-ok'):\n"
        + "\n".join(hits)
    )


def test_the_guard_catches_the_section_13_pattern():
    bad = "page_sids = [s for s in {c.source_ids[0] for c in claims if c.source_ids}]"
    assert any(rx.search(bad) for rx in RISKY)
    assert not any(
        rx.search("page_sids = [s for s in dict.fromkeys(c.source_ids[0] for c in claims)]")
        for rx in RISKY
    )
    assert not any(rx.search("for s in sorted({c for c in claims})") for rx in RISKY)


def test_no_control_characters_in_the_source_tree():
    """A '\b' written through a shell heredoc becomes a backspace character inside the regex
    and the pattern silently never matches (found on 2026-10-22 in three places: the
    subsidiary filter of the employee counter, two officer-detection patterns in the
    attribution check, and the new corp-suffix filter). Source and config must be free of them."""
    import re as _re

    bad = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    root = SRC.parent.parent
    hits = []
    for sub in ("src", "tests", "config", "tools"):
        for path in sorted((root / sub).rglob("*")):
            if path.suffix not in {".py", ".yaml", ".mjs", ".toml"} or "node_modules" in path.parts:
                continue
            if path.name == "test_determinism.py":
                continue  # this file spells the character class out
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if bad.search(line):
                    hits.append(f"{path.relative_to(root)}:{n}")
    assert not hits, "control characters in source: " + ", ".join(hits)
