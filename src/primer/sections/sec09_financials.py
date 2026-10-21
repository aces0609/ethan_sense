"""Section 9, Historical Financial Performance: the structured-first researcher (requirements §6 row 9).

plan   -> one structured call (companyfacts × concepts.yaml × last 4 FY)
collect-> EdgarClient.annual_financials; one source record per filing accession the
          facts came from, so every table cell cites the 10-K that reported it
draft  -> exhibit tables (deterministic) + narrative (LLM when configured, else template)
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from primer.clients.edgar import AnnualFinancials, EdgarClient
from primer.evidence.store import EvidenceStore
from primer.llm import invoke_text
from primer.runlog import RunLog
from primer.sections.exhibits import financial_tables
from primer.sections.style import HOUSE_STYLE, fmt_money, fmt_pct
from primer.state import Brief, Claim, Entity, SectionDraft

GATE_METRICS = ("revenue", "net_income", "operating_cash_flow", "capex")


def plan(brief: Brief, entity: Entity, as_of: date) -> list[dict[str, Any]]:
    return [
        {
            "tool": "edgar.annual_financials",
            "args": {
                "cik": entity.cik,
                "fiscal_year_end": entity.fiscal_year_end,
                "as_of": as_of.isoformat(),
            },
            "why": brief.structured_first[0] if brief.structured_first else "companyfacts",
        }
    ]


def collect(
    plan_items: list[dict[str, Any]],
    entity: Entity,
    store: EvidenceStore,
    edgar: EdgarClient,
    runlog: RunLog | None,
) -> dict[str, Any]:
    if not entity.cik:
        return {"error": "no CIK: company is not an SEC filer", "financials": None}
    item = plan_items[0]["args"]
    fin = edgar.annual_financials(
        entity.cik,
        fiscal_year_end=item.get("fiscal_year_end"),
        as_of=date.fromisoformat(item["as_of"]),
    )
    accession_sids = _register_accessions(fin, entity, store, edgar)
    return {"financials": fin, "accession_source_ids": accession_sids}


def _register_accessions(
    fin: AnnualFinancials, entity: Entity, store: EvidenceStore, edgar: EdgarClient
) -> dict[str, str]:
    """One source record per filing accession; evidence = the selected facts from that filing."""
    out: dict[str, str] = {}
    api_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{entity.cik}.json"
    for accn, info in fin.accessions.items():
        facts = [
            {
                "metric": fv.metric,
                "concept": f"{fv.taxonomy}:{fv.concept}",
                "value": fv.value,
                "unit": fv.unit,
                "period": fv.period_label,
                "start": fv.start.isoformat() if fv.start else None,
                "end": fv.end.isoformat(),
                "frame": fv.frame,
                "restated_from": fv.restated_from,
            }
            for m in fin.facts.values()
            for fv in m.values()
            if fv.accession == accn
        ]
        content = (
            f"# EDGAR XBRL companyfacts — {info['form']} filed {info['filed']} (accession {accn})\n\n"
            f"Source API: {api_url}\n\nSelected annual facts reported in this filing:\n\n```json\n"
            + json.dumps(facts, indent=1)
            + "\n```\n"
        )
        rec = store.register(
            content,
            type="api:edgar_xbrl",
            publisher="SEC EDGAR",
            title=f"XBRL companyfacts, {info['form']} filed {info['filed']} (accession {accn})",
            url=edgar.accession_index_url(entity.cik, accn),
            published_date=date.fromisoformat(info["filed"]),
            tier=1,
            gov_id=accn,
            meta={
                "api_url": api_url,
                "cik": entity.cik,
                "form": info["form"],
                "fact_count": len(facts),
            },
            dedupe_key=f"edgar_xbrl|{entity.cik}|{accn}",
        )
        out[accn] = rec.source_id
    return out


def draft(
    brief: Brief,
    entity: Entity,
    tool_results: dict[str, Any],
    store: EvidenceStore,
    runlog: RunLog | None,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    fin: AnnualFinancials | None = tool_results.get("financials")
    if fin is None or not fin.fiscal_years:
        reason = tool_results.get("error") or "no annual XBRL facts available"
        return SectionDraft(
            section=9,
            title=brief.title,
            markdown=f"> Coverage: unavailable — {reason}.",
            coverage="unavailable",
            caveat=None,
        )

    sids = tool_results["accession_source_ids"]
    ex = financial_tables(fin, sids, section=9)

    gate_ok = all(
        len([fy for fy in fin.fiscal_years if fin.value(m, fy) is not None]) >= 3
        for m in GATE_METRICS
    )
    coverage = "full" if (ex.present and gate_ok) else "partial"

    caveat = _caveat(entity, fin)
    narrative = _narrative_llm(
        brief, entity, fin, sids, ex.claims, runlog, critic_notes
    ) or _narrative_template(fin, sids)

    parts = [caveat, "", narrative, "", "### Financial summary", "", ex.markdown]
    if ex.notes:
        parts += [
            "",
            "> Coverage: "
            + ("full" if coverage == "full" else "partial")
            + " — "
            + "; ".join(ex.notes)
            + ".",
        ]
    md = "\n".join(parts).strip()
    return SectionDraft(
        section=9,
        title=brief.title,
        markdown=md,
        claims=ex.claims,
        source_ids=sorted(set(ex.source_ids) | set(re.findall(r"\[(src_\d{4})\]", narrative))),
        coverage=coverage,  # type: ignore[arg-type]
        caveat=caveat,
        exhibit_present=ex.present,
    )


def _caveat(entity: Entity, fin: AnnualFinancials) -> str:
    fye = f"{fin.fiscal_year_end[:2]}/{fin.fiscal_year_end[2:]}"
    span = f"FY{fin.fiscal_years[0]}–FY{fin.fiscal_years[-1]}"
    return (
        f"*Caveat: figures are GAAP as reported in SEC XBRL filings for {span} (fiscal year ends {fye}); "
        f"the most recently filed value is used where a period was restated, and free cash flow, net debt and "
        f"margins are derived in-agent with the formulas shown. Segment detail is not in XBRL and is not included here.*"
    )


def _cites(fin: AnnualFinancials, sids: dict[str, str], metric: str, fy: int) -> str:
    fv = fin.facts.get(metric, {}).get(fy)
    if fv and fv.accession in sids:
        return f" [{sids[fv.accession]}]"
    return ""


def _growth(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return a / b - 1


def _narrative_template(fin: AnnualFinancials, sids: dict[str, str]) -> str:
    fys = fin.fiscal_years
    last, prev, first = fys[-1], fys[-2] if len(fys) > 1 else None, fys[0]
    lines: list[str] = []

    rev_l, rev_f = fin.value("revenue", last), fin.value("revenue", first)
    if rev_l is not None:
        g = _growth(rev_l, rev_f)
        cagr = (
            (rev_l / rev_f) ** (1 / (last - first)) - 1
            if (rev_f and last > first and rev_l > 0 and rev_f > 0)
            else None
        )
        s = f"Revenue was ${fmt_money(rev_l)} million in FY{last}{_cites(fin, sids, 'revenue', last)}"
        if rev_f is not None and first != last:
            s += f", against ${fmt_money(rev_f)} million in FY{first}{_cites(fin, sids, 'revenue', first)}"
            if cagr is not None:
                s += f", a {fmt_pct(cagr)} compound annual rate (derived)"
            elif g is not None:
                s += f", {fmt_pct(g)} cumulative (derived)"
        lines.append(s + ".")
    if prev is not None and rev_l is not None and fin.value("revenue", prev) is not None:
        g = _growth(rev_l, fin.value("revenue", prev))
        lines.append(
            f"Year over year, FY{last} revenue changed {fmt_pct(g)} from FY{prev} (derived){_cites(fin, sids, 'revenue', prev)}."
        )

    ni_l = fin.value("net_income", last)
    if ni_l is not None and rev_l:
        lines.append(
            f"Net income was ${fmt_money(ni_l)} million in FY{last}, a {fmt_pct(ni_l / rev_l)} net margin (derived){_cites(fin, sids, 'net_income', last)}."
        )
    oi_l = fin.value("operating_income", last)
    if oi_l is not None and rev_l:
        lines.append(
            f"Operating income was ${fmt_money(oi_l)} million, a {fmt_pct(oi_l / rev_l)} operating margin (derived){_cites(fin, sids, 'operating_income', last)}."
        )

    ocf_l, capex_l = fin.value("operating_cash_flow", last), fin.value("capex", last)
    if ocf_l is not None:
        s = f"Cash from operations was ${fmt_money(ocf_l)} million in FY{last}{_cites(fin, sids, 'operating_cash_flow', last)}"
        if capex_l is not None:
            s += f" against ${fmt_money(capex_l)} million of capital expenditures{_cites(fin, sids, 'capex', last)}, leaving ${fmt_money(ocf_l - capex_l)} million of free cash flow (derived: operating cash flow minus capex)"
        lines.append(s + ".")

    cash_l, debt_l = fin.value("cash", last), fin.value("total_debt", last)
    if cash_l is not None and debt_l is not None:
        lines.append(
            f"At fiscal year end FY{last} the company held ${fmt_money(cash_l)} million of cash{_cites(fin, sids, 'cash', last)} against ${fmt_money(debt_l)} million of total debt (derived: long-term plus current debt){_cites(fin, sids, 'long_term_debt', last)}, for net debt of ${fmt_money(debt_l - cash_l)} million (derived)."
        )
    bb, dv = fin.value("buybacks", last), fin.value("dividends_paid", last)
    if bb is not None or dv is not None:
        s = f"Capital returned to shareholders in FY{last}: "
        bits = []
        if dv is not None:
            bits.append(
                f"${fmt_money(dv)} million of dividends{_cites(fin, sids, 'dividends_paid', last)}"
            )
        if bb is not None:
            bits.append(
                f"${fmt_money(bb)} million of share repurchases{_cites(fin, sids, 'buybacks', last)}"
            )
        lines.append(s + " and ".join(bits) + ".")

    restated = [fv for m in fin.facts.values() for fv in m.values() if fv.restated_from]
    if restated:
        fv = restated[0]
        lines.append(
            f"At least one figure was restated in a later filing: {fv.metric.replace('_', ' ')} for {fv.period_label} is taken from the most recent 10-K{_cites(fin, sids, fv.metric, fv.fiscal_year)} rather than the ${fmt_money(float(fv.restated_from['value']))} million originally reported."
        )
    return "\n\n".join(lines)


def _narrative_llm(
    brief: Brief,
    entity: Entity,
    fin: AnnualFinancials,
    sids: dict[str, str],
    claims: list[Claim],
    runlog: RunLog | None,
    critic_notes: list[str] | None,
) -> str | None:
    facts = [
        {
            "metric": c.metric,
            "period": c.period,
            "value": c.value,
            "unit": c.unit,
            "source_ids": c.source_ids,
            "note": c.note,
        }
        for c in claims
    ]
    user = (
        f"Company: {entity.name} ({entity.ticker}). Section: {brief.title}. Scope: {brief.scope}\n"
        f"Write 4-7 sentences of narrative (no table; the table is appended separately) using ONLY these facts. "
        f"Cite each fact with its source id token like [src_0003]. Label derived figures as derived.\n"
        + (f"Critic notes from the previous iteration: {critic_notes}\n" if critic_notes else "")
        + "Facts:\n"
        + json.dumps(facts, indent=1)
    )
    text = invoke_text("drafter", HOUSE_STYLE, user, runlog)
    if not text:
        return None
    allowed = set(sids.values())
    # strip any citation the model invented
    text = re.sub(r"\[(src_\d{4})\]", lambda m: m.group(0) if m.group(1) in allowed else "", text)
    return text.strip()
