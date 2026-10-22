"""Required-exhibit builders (requirements §6 "Required exhibit" column, PLAN 2.5).

This module holds the section 9 financial tables and the shared ExhibitResult; the other
sections' builders live in exhibits_secNN.py (one module per section, so parallel work on
them never collides) and are re-exported at the bottom of this file so `sections.exhibits`
remains the one import path for every table builder.

Every builder returns markdown plus the Claim list the numeric validator groups on. Every
cell cites a source_id; derived rows say so and give the formula.
"""

from __future__ import annotations

from dataclasses import dataclass

from primer.clients.edgar import AnnualFinancials, derived_note, formula_operands
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
            # derived: cite the sources of the operands (through one level of derivation, so
            # net debt cites the debt and cash filings even though total debt is itself derived)
            sids = sorted(
                {
                    accession_source_ids[fin.facts[m][fy].accession]
                    for m in _operand_facts(cfg, spec.get("derive", ""))
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
                    note=derived_note(spec, lambda m, fy=fy: fin.value(m, fy)),
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
            f"**{titles.get(stmt, stmt)}** (USD millions unless noted; {fin.fiscal_year_end_phrase})",
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


def _operand_facts(cfg: dict, formula: str) -> list[str]:
    """Reported metrics a formula rests on, expanding derived operands one level
    (total_debt -> long_term_debt + short_term_debt)."""
    specs = {m: s for stmt in cfg["statements"].values() for m, s in stmt.items()}
    out: list[str] = []
    for op in formula_operands(formula):
        sub = (specs.get(op) or {}).get("derive")
        if sub and not (specs.get(op) or {}).get("concepts"):
            out.extend(formula_operands(sub))
        else:
            out.append(op)
    return list(dict.fromkeys(out))


# ---- per-section builders (PLAN 2.5), resolved lazily so sections.exhibits is the one
# import path without a circular import (the per-section modules import ExhibitResult).

_BUILDERS: dict[str, tuple[str, str]] = {
    "segment_revenue_exhibit": ("exhibits_sec02", "segment_revenue_exhibit"),
    "product_table": ("exhibits_sec03", "product_table"),
    "program_table": ("exhibits_sec04", "program_table"),
    "agency_mix_table": ("exhibits_sec04", "agency_mix_table"),
    "locations_table": ("exhibits_sec04", "locations_table"),
    "reduced_locations_table": ("exhibits_sec04", "reduced_locations_table"),
    "build_swot": ("exhibits_sec05", "build_swot"),
    "swot_markdown": ("exhibits_sec05", "swot_markdown"),
    "peer_markdown": ("exhibits_sec05", "peer_markdown"),
    "targets_table": ("exhibits_sec06", "targets_table"),
    "acquisition_table": ("exhibits_sec07", "acquisition_table"),
    "governance_exhibit": ("exhibits_sec08", "governance_exhibit"),
    "results_table": ("exhibits_sec10", "results_table"),
    "kpi_vs_consensus_table": ("exhibits_sec10", "kpi_vs_consensus_table"),
    "segment_margin_table": ("exhibits_sec10", "segment_margin_table"),
    "guidance_table": ("exhibits_sec10", "guidance_table"),
    "debt_table": ("exhibits_sec11", "debt_table"),
    "maturity_table": ("exhibits_sec11", "maturity_table"),
    "ratings_table": ("exhibits_sec11", "ratings_table"),
    "capital_returns_table": ("exhibits_sec11", "capital_returns_table"),
    "thesis_exhibit": ("exhibits_sec12", "thesis_exhibit"),
    "ltm_table": ("exhibits_sec13", "ltm_table"),
    "ntm_table": ("exhibits_sec13", "ntm_table"),
    "price_targets_table": ("exhibits_sec13", "targets_table"),
    "peer_table": ("exhibits_sec13", "peer_table"),
    "event_log": ("exhibits_sec14", "event_log"),
}

__all__ = ["ExhibitResult", "financial_tables", *_BUILDERS]


def __getattr__(name: str):
    if name in _BUILDERS:
        import importlib

        module, attr = _BUILDERS[name]
        return getattr(importlib.import_module(f"primer.sections.{module}"), attr)
    raise AttributeError(name)
