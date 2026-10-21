"""Eval harness v0: compare a run's financial claims to a golden table (Phase 1 gate).

Golden file format (USD millions unless `unit` says otherwise):
{
  "ticker": "LMT",
  "unit": "USD millions",
  "metrics": {"revenue": {"FY2022": 65984, ...}, "net_income": {...}, ...},
  "provenance": "..."
}
Values are compared to the dollar after scaling (millions → USD); `null` entries are
skipped and reported as unfilled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from primer.validate.citations import check_citations

SCALE = {"USD millions": 1e6, "USD thousands": 1e3, "USD": 1.0}


def load_claims(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / "claims.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def compare_financials(run_dir: Path, golden_path: Path, section: int = 9) -> dict[str, Any]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    scale = SCALE.get(golden.get("unit", "USD millions"), 1e6)
    claims = [c for c in load_claims(run_dir) if c["section"] == section]
    by_key = {(c["metric"], c["period"]): c for c in claims}

    rows = []
    matched = checked = unfilled = 0
    for metric, periods in golden.get("metrics", {}).items():
        for period, gval in periods.items():
            if gval is None:
                unfilled += 1
                rows.append({"metric": metric, "period": period, "status": "golden unfilled"})
                continue
            checked += 1
            c = by_key.get((metric, period))
            if c is None:
                rows.append(
                    {"metric": metric, "period": period, "golden": gval, "status": "missing in run"}
                )
                continue
            got = c["value"]
            exact = abs(got - gval * scale) < 0.5
            matched += int(exact)
            rows.append(
                {
                    "metric": metric,
                    "period": period,
                    "golden": gval,
                    "run": got / scale,
                    "status": "match" if exact else "MISMATCH",
                    "source_ids": c["source_ids"],
                    "concept": c.get("concept"),
                }
            )

    mdx = run_dir / "primer.mdx"
    cit = check_citations(mdx.read_text(encoding="utf-8")) if mdx.exists() else {"ok": None}
    cited = all(bool(r.get("source_ids")) for r in rows if r.get("status") in ("match", "MISMATCH"))
    return {
        "golden": str(golden_path),
        "run_dir": str(run_dir),
        "checked": checked,
        "matched": matched,
        "unfilled": unfilled,
        "exact_match_rate": round(matched / checked, 4) if checked else None,
        "every_number_cited": cited,
        "citations_resolve": cit.get("ok"),
        "rows": rows,
    }


def write_scorecard(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"scorecard-{Path(result['run_dir']).name}.json"
    p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return p
