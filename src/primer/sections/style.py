"""House-style prompt fragments and formatting helpers (requirements §3 editorial pattern)."""

from __future__ import annotations

HOUSE_STYLE = """You write one section of a company primer for an analyst who will read it critically.
Rules:
- Open with one caveat line when applicable (listing status, ownership concentration, data gaps).
- Then short subsections with declarative sentences. No first person. No investment recommendation,
  no price prediction stated as your own view.
- Every sentence that states a fact ends with one or more citation tokens of the form [src_0012].
  You may cite ONLY the source ids given to you. Never invent an id.
- A view may be attributed to a named firm only if a cited source's text names that firm.
- Numbers: prefer the structured tier-1 source; label derived figures as derived with the formula.
- When two sources disagree on a number, keep both and say so in a line starting "> Conflict:".
- Do not restate the same fact from several sources as if it were several facts.
"""

CITATION_TOKEN = r"\[(src_\d{4})\]"


def fmt_money(value: float | None, unit: str = "USD", scale: str = "millions") -> str:
    if value is None:
        return "n/a"
    if unit == "USD/shares":
        return f"{value:,.2f}"
    if unit == "shares":
        return f"{value / 1e6:,.1f}M"
    if unit == "pure":
        return f"{value:,.3f}"
    if scale == "millions":
        return f"{value / 1e6:,.0f}"
    if scale == "billions":
        return f"{value / 1e9:,.2f}"
    return f"{value:,.0f}"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def coverage_line(status: str, detail: str) -> str:
    return f"> Coverage: {status} — {detail}"


def conflict_line(metric: str, pairs: list[tuple[str, str]]) -> str:
    parts = " and ".join(f"{v} {cite}" for v, cite in pairs)
    return f"> Conflict: {metric} reported as {parts}; sources differ."
