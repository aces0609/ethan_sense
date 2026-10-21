"""Gate 3: numeric consistency. Claims grouped by (metric, period); values that differ by
more than the tolerance are conflicts to flag in the later section, never silently reconciled."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from primer.config import budgets_config


def _tolerance(metric: str) -> float:
    tol = budgets_config().get("numeric_tolerance", {})
    if metric.endswith(("margin", "pct_revenue", "ratio", "leverage")):
        return float(tol.get("ratio", 0.02))
    if metric.endswith(("count", "headcount", "units")):
        return float(tol.get("count", 0.0))
    return float(tol.get("default", 0.005))


def check_numeric(claims: list[dict[str, Any]]) -> dict:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        groups[(c["metric"], c["period"])].append(c)
    conflicts = []
    for (metric, period), rows in groups.items():
        vals = sorted({round(float(r["value"]), 6) for r in rows})
        if len(vals) < 2:
            continue
        lo, hi = vals[0], vals[-1]
        denom = max(abs(hi), abs(lo), 1e-9)
        if abs(hi - lo) / denom > _tolerance(metric):
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
