"""Key Takeaways check (PLAN 2.7): a bullet is rejected when it introduces a claim that is not
in any section's claims list, cites a source no section cites, or points at a section that
was not drafted.

A "claim" here is a number: every figure in the bullet (other than years, section numbers
and quarter labels) must match a claim value in the claims list under one of the display
scalings (units, millions, billions, percent, multiples) within 0.6%, or appear verbatim in
the text of a source the bullet cites.
"""

from __future__ import annotations

import re
from typing import Any

from primer.sections.style import CITATION_TOKEN

NUMBER = re.compile(r"(?<![\w.])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(?![\w])")
SECTION_TAG = re.compile(r"\(Section (\d{1,2})\)[.\s]*$")
SKIP = re.compile(r"\b(?:Q[1-4]|FY|fiscal|20\d{2}|19\d{2}|Section)\b", re.I)
SCALES = (1.0, 1e-6, 1e-9, 1e-3, 100.0, 1e-2)


def _numbers(text: str) -> list[float]:
    out = []
    for m in NUMBER.finditer(text):
        raw = m.group(1)
        ctx = text[max(0, m.start() - 8) : m.end() + 8]
        if re.fullmatch(r"(19|20)\d{2}", raw) or SKIP.search(ctx) and re.fullmatch(r"\d{4}", raw):
            continue
        if re.search(r"(?:Q|Section |FY)\s*$", text[: m.start()]):
            continue
        out.append(float(raw.replace(",", "")))
    return out


def _matches_claim(n: float, values: list[float]) -> bool:
    for v in values:
        for s in SCALES:
            x = v * s
            if abs(x - n) <= max(abs(x) * 0.006, 0.05 if abs(n) < 10 else 0.5):
                return True
    return False


def check_takeaways(
    bullets: list[str],
    drafts: dict[int, dict[str, Any]],
    claims: list[dict[str, Any]],
    store: Any,
) -> dict[str, Any]:
    """Return {ok, kept, rejected: [{bullet, reason}]}."""
    values = [float(c["value"]) for c in claims if c.get("value") is not None]
    cited_all: set[str] = set()
    for d in drafts.values():
        cited_all.update(d.get("source_ids") or [])
        cited_all.update(re.findall(CITATION_TOKEN, d.get("markdown") or ""))
    kept: list[str] = []
    rejected: list[dict[str, str]] = []
    for b in bullets:
        text = b.strip()
        m = SECTION_TAG.search(text)
        if not m:
            rejected.append({"bullet": text, "reason": "no '(Section N)' tag"})
            continue
        sec = int(m.group(1))
        if sec not in {int(k) for k in drafts}:
            rejected.append({"bullet": text, "reason": f"section {sec} not drafted"})
            continue
        sids = re.findall(CITATION_TOKEN, text)
        bad = [s for s in sids if s not in cited_all]
        if bad:
            rejected.append({"bullet": text, "reason": f"cites {bad} not cited by any section"})
            continue
        body = SECTION_TAG.sub("", re.sub(CITATION_TOKEN, "", text))
        unmatched = []
        for n in _numbers(body):
            if _matches_claim(n, values):
                continue
            raw = f"{n:,.0f}" if float(n).is_integer() else f"{n:g}"
            if any(
                store.has(s)
                and (
                    raw in store.text(s) or f"{n:g}" in store.text(s) or f"{n:.1f}" in store.text(s)
                )
                for s in sids
            ):
                continue
            unmatched.append(n)
        if unmatched:
            rejected.append(
                {
                    "bullet": text,
                    "reason": f"figures not in any claims list or cited source: {unmatched[:4]}",
                }
            )
            continue
        kept.append(text)
    return {"ok": not rejected, "kept": kept, "rejected": rejected, "n_in": len(bullets)}
