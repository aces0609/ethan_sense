"""Section 9 for a company with no 10-K yet (a recent IPO): 10-Q periods plus the prospectus.

The table is built from 10-Q XBRL (`clients.interim`): three-month and year-to-date columns and
the balance sheet at the last fiscal year end, every cell cited to the 10-Q that reported it. The
full-year figures a prospectus reports (FY2023 to FY2025) are quoted in the text with the
prospectus as the source; they are not tabled because no machine-readable annual series exists
yet. Coverage is always partial: there is no full fiscal year of filed financials.
"""

from __future__ import annotations

import re
from typing import Any

from primer.clients.edgar import AnnualFinancials
from primer.sections import common
from primer.sections.common import ToolContext
from primer.sections.exhibits import financial_tables
from primer.sections.style import fmt_money, fmt_pct
from primer.state import Brief, SectionDraft

INTERIM_RULES = (
    "The company has not yet filed an annual report. The table holds only 10-Q periods (three-month and "
    "year-to-date columns, labelled by period). Full-year FY2023 to FY2025 figures may be quoted "
    "from the IPO prospectus, citing its source id and naming the fiscal year. Never add quarters "
    "together, annualise, or compare a three-month figure with a year-to-date one."
)

KEYWORDS = [
    "revenue",
    "net income",
    "net loss",
    "adjusted EBITDA",
    "operating income",
    "capital expenditures",
    "free cash flow",
    "liquidity",
]


def _cite(fin: AnnualFinancials, sids: dict[str, str], metric: str, key: int) -> str:
    fv = fin.facts.get(metric, {}).get(key)
    sid = sids.get(fv.accession) if fv else None
    return f" [{sid}]" if sid else ""


def _column(fin: AnnualFinancials, kind: str, latest: bool) -> int | None:
    """The key of the newest (or oldest) column whose label starts with `kind` ("3M" or a month
    count) among the columns that carry revenue."""
    keys = [
        k
        for k in fin.fiscal_years
        if fin.labels.get(k, "").startswith(kind) and k in fin.facts.get("revenue", {})
    ]
    return (keys[-1] if latest else keys[0]) if keys else None


def template_narrative(fin: AnnualFinancials, sids: dict[str, str]) -> str:
    """Keyless narrative: the latest quarter against its prior-year quarter, then the
    year-to-date period, each figure cited and each derived figure labelled."""
    lines: list[str] = []
    quarters = [
        k
        for k in fin.fiscal_years
        if fin.labels.get(k, "").startswith("3M") and k in fin.facts.get("revenue", {})
    ]
    if quarters:
        cur = quarters[-1]
        rev = fin.facts["revenue"][cur].value
        line = f"Revenue was ${fmt_money(rev)} million in the {fin.labels[cur]}{_cite(fin, sids, 'revenue', cur)}"
        prior = next(
            (
                k
                for k in quarters
                if fin.labels[k].split()[-1] == str(int(fin.labels[cur].split()[-1]) - 1)
                and fin.labels[k].split()[-2] == fin.labels[cur].split()[-2]
            ),
            None,
        )
        if prior is not None:
            pv = fin.facts["revenue"][prior].value
            if pv:
                line += (
                    f", {fmt_pct(rev / pv - 1)} against ${fmt_money(pv)} million in the "
                    f"{fin.labels[prior]} (derived){_cite(fin, sids, 'revenue', prior)}"
                )
        lines.append(line + ".")
        ni = fin.value("net_income", cur)
        if ni is not None:
            lines.append(
                f"Net income was ${fmt_money(ni)} million in the {fin.labels[cur]}{_cite(fin, sids, 'net_income', cur)}."
            )
    ytd = [
        k
        for k in fin.fiscal_years
        if fin.labels.get(k, "")[:1].isdigit()
        and not fin.labels[k].startswith("3M")
        and k in fin.facts.get("revenue", {})
    ]
    if ytd:
        k = ytd[-1]
        ocf, capex = fin.value("operating_cash_flow", k), fin.value("capex", k)
        if ocf is not None and capex is not None:
            lines.append(
                f"Over the {fin.labels[k]}, cash from operations was ${fmt_money(ocf)} million{_cite(fin, sids, 'operating_cash_flow', k)} "
                f"against ${fmt_money(capex)} million of capital expenditures{_cite(fin, sids, 'capex', k)}, "
                f"free cash flow of ${fmt_money(ocf - capex)} million (derived: operating cash flow minus capex)."
            )
    return "\n\n".join(lines)


def draft_interim_section9(
    brief: Brief,
    ctx: ToolContext,
    tool_results: dict[str, Any],
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    fin: AnnualFinancials = tool_results["financials"]
    sids: dict[str, str] = tool_results["accession_source_ids"]
    ex = financial_tables(fin, sids, section=9)
    prospectus = [s for s in ctx.foundation_sources("10-K") if ctx.store.has(s)]
    evidence = list(dict.fromkeys([*prospectus, *ex.source_ids]))
    facts = [
        {
            "metric": c.metric,
            "period": c.period,
            "value": c.value,
            "unit": c.unit,
            "source_ids": c.source_ids,
            "note": c.note,
        }
        for c in ex.claims
    ]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts or None,
        exhibit_md=ex.markdown,
        extra_rules=INTERIM_RULES,
        critic_notes=critic_notes,
        keywords=KEYWORDS,
    ) or template_narrative(fin, sids)
    basis = (ctx.foundation.get("annual_basis") or {}).get("label", "IPO prospectus")
    caveat = common.caveat_line(
        f"this company has not yet filed an annual report, so no full fiscal year of SEC XBRL exists; the table is GAAP as "
        f"reported in its 10-Q filings, in three-month and year-to-date columns, and full-year figures are quoted "
        f"from the {basis} where the text gives them; free cash flow and gross profit are derived in-agent with "
        f"the formulas shown, and no period is annualised or estimated"
    )
    notes = list(ex.notes)
    notes.append(
        "no fiscal year of filed financials yet; annual comparison is limited to the prospectus text"
    )
    narrative = re.sub(r"\n{3,}", "\n\n", narrative or "").strip()
    return common.finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=ex.markdown,
        exhibit_present=ex.present,
        claims=ex.claims,
        coverage="partial",
        notes=notes,
        exhibit_title="Financial summary",
    )
