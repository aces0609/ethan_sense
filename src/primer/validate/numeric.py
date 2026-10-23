"""Gate 3: numeric consistency. Claims grouped by (metric, period); values that differ by
more than the tolerance are conflicts to flag in the later section, never silently reconciled."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from primer.config import budgets_config

# Press-reported and Form D datapoints of a private company. A page that lists several rounds
# dates each one by the page's own date when the sentence names none, so one source can state
# many different amounts for the same (metric, period): that is a list, not a disagreement.
PRESS_PREFIXES = ("press_", "form_d_")


def _tolerance(metric: str) -> float:
    tol = budgets_config().get("numeric_tolerance", {})
    if metric.endswith(("margin", "pct_revenue", "ratio", "leverage")):
        return float(tol.get("ratio", 0.02))
    if metric.endswith(("count", "headcount", "units")):
        return float(tol.get("count", 0.0))
    return float(tol.get("default", 0.005))


def _differ(a: float, b: float, metric: str) -> bool:
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom > _tolerance(metric)


def _disagreeing_rows(metric: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that take part in a real conflict. For a press metric two values conflict only
    when they come from different sources; every other metric keeps the plain range rule."""
    if not metric.startswith(PRESS_PREFIXES):
        vals = sorted({round(float(r["value"]), 6) for r in rows})
        if len(vals) >= 2 and _differ(vals[0], vals[-1], metric):
            return rows
        return []
    involved: list[dict[str, Any]] = []
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if set(a.get("source_ids") or ()) & set(b.get("source_ids") or ()):
                continue  # one source naming two amounts is not two sources disagreeing
            va, vb = float(a["value"]), float(b["value"])
            # values an order of magnitude apart are different events (a 2015 round and a 2025
            # estimate on one page), not two readings of one quantity
            comparable = bool(va and vb) and 0.4 <= abs(va / vb) <= 2.5
            if comparable and _differ(va, vb, metric):
                for r in (a, b):
                    if not any(r is x for x in involved):
                        involved.append(r)
    return involved


def check_numeric(claims: list[dict[str, Any]]) -> dict:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        groups[(c["metric"], c["period"])].append(c)
    conflicts = []
    for (metric, period), all_rows in groups.items():
        rows = _disagreeing_rows(metric, all_rows)
        if rows:
            conflicts.append(
                {
                    "metric": metric,
                    "period": period,
                    "values": [
                        {
                            "value": r["value"],
                            "section": r["section"],
                            "source_ids": r["source_ids"],
                        }
                        for r in rows
                    ],
                    "later_section": max(r["section"] for r in rows),
                }
            )
    return {"ok": not conflicts, "groups": len(groups), "conflicts": conflicts}
