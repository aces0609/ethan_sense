"""Required-exhibit builders. Phase 1: the section 9 financial tables (requirements §6 row 9).

Each builder returns markdown plus the Claim list the numeric validator groups on. Every
cell cites the source_id of the filing (accession) the XBRL fact came from; derived rows
say so and give the formula.
"""

from __future__ import annotations

from dataclasses import dataclass

from primer.clients.edgar import AnnualFinancials
from primer.config import concepts_config
from primer.sections.style import fmt_money, fmt_pct
from primer.state import Claim


@dataclass
class ExhibitResult:
    markdown: str
    claims: list[Claim]
    source_ids: list[str]
    present: bool
    notes: list[str]


def financial_tables(
    fin: AnnualFinancials,
    accession_source_ids: dict[str, str],
    section: int = 9,
    currency: str = "USD",
) -> ExhibitResult:
    cfg = concepts_config()
    fys = fin.fiscal_years
    claims: list[Claim] = []
    used: set[str] = set()
    notes: list[str] = []
    parts: list[str] = []

    if not fys:
        return ExhibitResult("", [], [], False, ["no annual XBRL facts found"])

    header = "| Metric | " + " | ".join(f"FY{fy}" for fy in fys) + " |"
    sep = "|---|" + "---:|" * len(fys)

    def cell(metric: str, fy: int, spec: dict) -> str:
        fv = fin.facts.get(metric, {}).get(fy)
        unit = spec.get("unit", "USD")
        if fv is not None:
            sid = accession_source_ids.get(fv.accession)
            cite = f" [{sid}]" if sid else ""
            if sid:
                used.add(sid)
                claims.append(
                    Claim(
                        section=section,
                        metric=metric,
                        value=fv.value,
                        unit=unit,
                        period=fv.period_label,
                        source_ids=[sid],
                        concept=f"{fv.taxonomy}:{fv.concept}",
                        note=f"restated from {fv.restated_from['value']}"
                        if fv.restated_from
                        else None,
                    )
                )
            return fmt_money(fv.value, unit) + cite
        dv = fin.derived.get(metric, {}).get(fy)
        if dv is not None:
            # derived: cite the sources of the operands
            sids = sorted(
                {
                    accession_source_ids[fin.facts[m][fy].accession]
                    for m in _operands(spec.get("derive", ""))
                    if fy in fin.facts.get(m, {})
                    and fin.facts[m][fy].accession in accession_source_ids
                }
            )
            used.update(sids)
            claims.append(
                Claim(
                    section=section,
                    metric=metric,
                    value=dv,
                    unit=unit,
                    period=f"FY{fy}",
                    source_ids=sids,
                    concept="derived",
                    note=spec.get("derive"),
                )
            )
            return fmt_money(dv, unit) + "".join(f" [{s}]" for s in sids)
        return "n/a"

    titles = {
        "income_statement": "Income statement",
        "cash_flow": "Cash flow",
        "balance_sheet": "Balance sheet (fiscal year end)",
    }
    for stmt, metrics in cfg["statements"].items():
        rows = [
            f"**{titles.get(stmt, stmt)}** (USD millions unless noted; fiscal years ending {fin.fiscal_year_end[:2]}/{fin.fiscal_year_end[2:]})",
            "",
            header,
            sep,
        ]
        any_row = False
        for metric, spec in metrics.items():
            vals = [cell(metric, fy, spec) for fy in fys]
            if all(v == "n/a" for v in vals):
                continue
            any_row = True
            label = spec["label"]
            if spec.get("derive"):
                label += f" — derived: {spec['derive'].replace('_', ' ')}"
            rows.append(f"| {label} | " + " | ".join(vals) + " |")
        if any_row:
            parts.append("\n".join(rows))

    # ratios
    ratio_rows = []
    for key, spec in cfg.get("derived_ratios", {}).items():
        num, den = [s.strip() for s in spec["formula"].split("/")]
        vals = []
        for fy in fys:
            n, d = fin.value(num, fy), fin.value(den, fy)
            if n is None or d in (None, 0):
                vals.append("n/a")
                continue
            r = n / d
            claims.append(
                Claim(
                    section=section,
                    metric=key,
                    value=r,
                    unit="pure",
                    period=f"FY{fy}",
                    source_ids=[],
                    concept="derived",
                    note=spec["formula"],
                )
            )
            vals.append(fmt_pct(r))
        if any(v != "n/a" for v in vals):
            ratio_rows.append(
                f"| {spec['label']} — derived: {spec['formula'].replace('_', ' ')} | "
                + " | ".join(vals)
                + " |"
            )
    if ratio_rows:
        parts.append(
            "\n".join(["**Ratios (derived from the tables above)**", "", header, sep, *ratio_rows])
        )

    for metric, miss in fin.missing.items():
        notes.append(f"{metric}: no XBRL fact for {', '.join(f'FY{fy}' for fy in miss)}")
    restated = [fv for m in fin.facts.values() for fv in m.values() if fv.restated_from]
    if restated:
        notes.append(
            "restated values used (latest filing wins): "
            + "; ".join(
                f"{fv.metric} {fv.period_label} was {fv.restated_from['value']:,} in {fv.restated_from['accession']}"
                for fv in restated[:6]
            )
        )

    return ExhibitResult("\n\n".join(parts), claims, sorted(used), bool(parts), notes)


def _operands(formula: str) -> list[str]:
    import re

    return re.findall(r"[a-z_]+", formula)
