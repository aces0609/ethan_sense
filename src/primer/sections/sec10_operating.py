"""Section 10, Operating Performance and Benchmarking (requirements §6 row 10; PLAN 2.5 item 4).

plan    -> the foundation earnings releases and transcripts, XBRL quarterly series, the
           consensus/beat-miss history from the market-data adapter, the 10-K segment tables,
           then the web queries (quarter and KPI placeholders filled from the latest release)
collect -> parses each release (summary table, outlook table, backlog, segment sentences),
           pulls XBRL quarterly actuals (preferred, cited to their accession), the Yahoo
           earnings-history rows, and the 10-K segment summing tables
draft   -> quarterly results table, KPI vs consensus table (variance derived), segment margin
           table (derived per segment per FY), management outlook table; narrative by the
           model when configured, else the template digest

When no market-data adapter exists the consensus columns read n/a and coverage is partial
with the note "no market-data adapter (Firecrawl not configured)".
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from primer.clients.edgar import EdgarClient, FactValue, trailing_months
from primer.evidence.store import EvidenceStore
from primer.sections.common import (
    ToolContext,
    caveat_line,
    draft_section,
    expand_queries,
    finalize_draft,
    fmt_date,
    template_digest,
    unavailable_draft,
)
from primer.sections.exhibits_sec10 import (
    RESULT_LABELS,
    Val,
    guidance_table,
    kpi_vs_consensus_table,
    norm_quarter,
    parse_release,
    results_table,
    segment_margin_table,
    slug,
    table_years,
    val_from_fact,
)
from primer.sections.style import fmt_money
from primer.sections.text_tools import find_summing_tables, sentences_with
from primer.state import Brief, Entity, SectionDraft

NO_ADAPTER_NOTE = "no market-data adapter (Firecrawl not configured)"
XBRL_METRICS = ("revenue", "net_income", "eps_diluted", "operating_income")


def register_quarterly(
    facts: list[FactValue], entity: Entity, store: EvidenceStore, edgar: EdgarClient
) -> dict[str, str]:
    """One tier-1 record per filing accession the quarterly facts came from (same dedupe key
    family as sections 9, 11 and 13, so a 10-K accession shares its record)."""
    out: dict[str, str] = {}
    by_accn: dict[str, list[FactValue]] = {}
    for fv in facts:
        by_accn.setdefault(fv.accession, []).append(fv)
    api_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{entity.cik}.json"
    for accn, fvs in by_accn.items():
        rows = [
            {
                "metric": fv.metric,
                "concept": f"{fv.taxonomy}:{fv.concept}",
                "value": fv.value,
                "unit": fv.unit,
                "period": fv.period_label,
                "start": fv.start.isoformat() if fv.start else None,
                "end": fv.end.isoformat(),
                "derived": fv.restated_from,
            }
            for fv in fvs
        ]
        form, filed = fvs[0].form, fvs[0].filed
        rec = store.register(
            f"# EDGAR XBRL companyfacts — {form} filed {filed.isoformat()} (accession {accn})\n\n"
            f"Source API: {api_url}\n\nQuarterly facts reported in this filing:\n\n```json\n"
            + json.dumps(rows, indent=1)
            + "\n```\n",
            type="api:edgar_xbrl",
            publisher="SEC EDGAR",
            title=f"XBRL companyfacts, {form} filed {filed.isoformat()} (accession {accn})",
            url=edgar.accession_index_url(entity.cik, accn),  # type: ignore[arg-type]
            published_date=filed,
            tier=1,
            gov_id=accn,
            meta={"api_url": api_url, "cik": entity.cik, "form": form, "fact_count": len(rows)},
            dedupe_key=f"edgar_xbrl|{entity.cik}|{accn}",
        )
        out[accn] = rec.source_id
    return out


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    quarters = ctx.quarters()
    latest = (
        quarters[0]["label"].replace("-", " ")
        if quarters
        else f"Q{((ctx.as_of.month - 1) // 3) or 4} {ctx.as_of.year}"
    )
    items: list[dict[str, Any]] = [
        {
            "tool": "foundation.quarters",
            "args": {"quarters": [q["label"] for q in quarters]},
            "why": brief.structured_first[0]
            if brief.structured_first
            else "releases and transcripts",
        },
        {
            "tool": "edgar.quarterly_series",
            "args": {
                "cik": ctx.entity.cik,
                "metrics": list(XBRL_METRICS),
                "n_quarters": 8,
                "as_of": ctx.as_of.isoformat(),
            },
            "why": "XBRL quarterly actuals (preferred over the release for statement lines)",
        },
        {
            "tool": "market.consensus",
            "args": {"ticker": ctx.entity.ticker},
            "why": "consensus EPS/revenue and the beat/miss history (Yahoo analysis page)",
        },
        {
            "tool": "foundation.10-K",
            "args": {
                "items": ["7", "8"],
                "tables": ["segment net sales", "segment operating profit"],
            },
            "why": "segment margin table",
        },
    ]
    for q in expand_queries(brief, ctx, quarter=latest, metric="backlog")[: ctx.budget.searches]:
        items.append(
            {"tool": "firecrawl.search", "query": q, "why": "results-vs-expectations coverage"}
        )
    return items


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    ent, store, edgar = ctx.entity, ctx.store, ctx.edgar
    if not ent.cik:
        return {
            "error": "no CIK: no earnings releases or XBRL quarterly facts for a private company"
        }
    out: dict[str, Any] = {
        "notes": [],
        "releases": [],
        "xbrl": {},
        "xbrl_sids": {},
        "consensus": None,
        "segments": None,
    }

    for q in ctx.quarters():
        sid = q.get("release_sid")
        if not sid or not store.has(sid):
            continue
        parsed = parse_release(store.text(sid))
        parsed["source_id"] = sid
        parsed["quarter"] = q["label"]
        parsed["transcript_sid"] = q.get("transcript_sid")
        out["releases"].append(parsed)

    try:
        series = edgar.quarterly_series(ent.cik, XBRL_METRICS, n_quarters=8, as_of=ctx.as_of)
        cutoff = trailing_months(ctx.as_of, 27)
        facts = [fv for vals in series.values() for fv in vals if fv.end >= cutoff]
        out["xbrl_sids"] = register_quarterly(facts, ent, store, edgar)
        out["xbrl"] = {m: [fv for fv in vals if fv.end >= cutoff] for m, vals in series.items()}
    except Exception as e:
        ctx.log(f"quarterly_series: {e!r}")
        out["notes"].append("XBRL quarterly series unavailable")

    if ctx.market is None:
        out["notes"].append(NO_ADAPTER_NOTE)
    else:
        try:
            out["consensus"] = ctx.market.consensus(ent.ticker or "")
        except Exception as e:
            ctx.log(f"market.consensus failed: {e!r}")
            out["notes"].append(f"consensus page: {e!r}"[:160])

    for item in ("7", "8"):
        text, sid = ctx.item_text(item)
        if not text:
            continue
        sales = find_summing_tables(text, total_regex=r"^\s*total\b.*(sales|revenue)", max_rows=8)
        profit = find_summing_tables(
            text, total_regex=r"^\s*total\b.*operating (profit|income)", max_rows=8
        )
        if sales and profit:
            out["segments"] = {
                "sales": sales[0],
                "profit": profit[0],
                "columns": table_years(text, sales[0], ctx.fy),
                "source_id": sid,
                "item": item,
            }
            break

    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    out["web"] = ctx.web_research(
        queries, type="news", prefer_domains=tuple(ctx.brief.trade_press) if ctx.brief else ()
    )
    out["notes"].extend(n for n in ctx.budget.notes if n not in out["notes"])
    return out


def _quarter_rows(tr: dict[str, Any]) -> list[dict[str, Any]]:
    """Newest-first quarter rows {label, vals}; XBRL wins for statement lines, the release
    fills what XBRL lacks (segment profit, OCF, backlog) and whole quarters XBRL has not reached."""
    rows: dict[str, dict[str, Val]] = {}
    xbrl: dict[str, list[FactValue]] = tr.get("xbrl") or {}
    sids: dict[str, str] = tr.get("xbrl_sids") or {}
    for metric, facts in xbrl.items():
        for fv in facts:
            basis = (
                "XBRL "
                + fv.form
                + (" (FY − nine-month YTD, derived)" if fv.taxonomy == "derived" else "")
            )
            v = val_from_fact(fv, sids.get(fv.accession), basis)
            if fv.taxonomy == "derived":
                v.concept, v.note = (
                    "derived",
                    "fiscal-year value minus nine-month year-to-date value (XBRL; additive flow metrics only)",
                )
            rows.setdefault(v.period, {}).setdefault(metric, v)
    for rel in tr.get("releases") or []:
        cols = rel.get("columns") or [rel["quarter"]]
        for i, label in enumerate(cols):
            if not label:
                continue
            target = rows.setdefault(label, {})
            for metric, values in (rel.get("rows") or {}).items():
                v = values[i] if i < len(values) else None
                if v is None or metric in target:
                    continue
                unit = "USD/shares" if metric == "eps_diluted" else "USD"
                target[metric] = Val(
                    v, [rel["source_id"]], label, "earnings release", unit, "release summary table"
                )
        if rel.get("backlog") and cols[0] and "backlog" not in rows.setdefault(cols[0], {}):
            rows[cols[0]]["backlog"] = Val(
                rel["backlog"][0],
                [rel["source_id"]],
                cols[0],
                "earnings release",
                "USD",
                "release text",
                rel["backlog"][1],
            )

    def key(label: str) -> tuple[int, int]:
        q, y = label.split("-")
        return int(y), int(q[1:])

    return [
        {"label": lbl, "vals": rows[lbl]}
        for lbl in sorted(rows, key=key, reverse=True)
        if rows[lbl]
    ]


def q4_gap_notes(quarters: list[dict[str, Any]]) -> list[str]:
    """Coverage notes for fiscal fourth quarters whose per-share and attributable figures are
    absent: XBRL reports no discrete Q4, the FY-minus-YTD subtraction is not used for diluted
    EPS or net income (review 2026-10-21 §5 item 5), and no Q4 release in the foundation pack
    supplied them, so the cells read n/a."""
    out: list[str] = []
    for q in quarters:
        v = q["vals"]
        rev = v.get("revenue") or v.get("operating_income")
        if rev is None or rev.concept != "derived":
            continue
        missing = [RESULT_LABELS[m].lower() for m in ("eps_diluted", "net_income") if not v.get(m)]
        if missing:
            out.append(
                f"{q['label']} {' and '.join(missing)} n/a: not derived as fiscal year minus "
                "nine-month year-to-date (per-share and attributable figures are not additive) and "
                "no Q4 earnings release in the foundation pack reports them"
            )
    return out


def _consensus_rows(cons: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if cons is None:
        return out
    as_of = fmt_date(cons.as_of)
    for h in cons.earnings_history or []:
        label = norm_quarter(h.quarter)
        if not label or not h.source_id:
            continue
        out.setdefault(
            label,
            {
                "source_id": h.source_id,
                "as_of": as_of,
                "eps_estimate": h.eps_estimate,
                "eps_actual_page": h.eps_actual,
                "surprise_pct": h.surprise_pct,
                "revenue_estimate": None,
            },
        )
    nq = norm_quarter(cons.next_quarter_label)
    if nq and cons.eps_next_quarter is not None and cons.eps_next_quarter.ok:
        out.setdefault(
            nq,
            {
                "source_id": cons.source_id,
                "as_of": as_of,
                "eps_estimate": cons.eps_next_quarter.value,
                "eps_actual_page": None,
                "surprise_pct": None,
                "revenue_estimate": cons.revenue_next_quarter.value
                if (cons.revenue_next_quarter and cons.revenue_next_quarter.ok)
                else None,
            },
        )
    return out


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    if tool_results.get("error") and not tool_results.get("releases"):
        return unavailable_draft(brief, tool_results["error"], ctx)
    releases = tool_results.get("releases") or []
    quarters = _quarter_rows(tool_results)
    has_market = tool_results.get("consensus") is not None
    cons_rows = _consensus_rows(tool_results.get("consensus"))

    results = results_table(quarters, 10)
    kpi = kpi_vs_consensus_table(quarters, cons_rows, 10, has_market=has_market)
    seg = tool_results.get("segments") or {}
    segments = segment_margin_table(
        seg.get("sales"), seg.get("profit"), seg.get("columns") or [], seg.get("source_id"), 10
    )
    latest = releases[0] if releases else None
    guidance = (
        guidance_table(
            latest["outlook"],
            latest["source_id"],
            latest.get("release_date"),
            f"FY{latest['outlook_year'] or (latest.get('release_date') or ctx.as_of).year}",
            10,
        )
        if latest
        else guidance_table([], "", None, "", 10)
    )
    seg_sentences = _segment_sentences(latest) if latest else ("", [])

    parts = [kpi.markdown, results.markdown, segments.markdown]
    if seg_sentences[0]:
        parts.append(seg_sentences[0])
    if guidance.present:
        parts.append(guidance.markdown)
    exhibit_md = "\n\n".join(p for p in parts if p)
    claims = [
        c for ex in (kpi, results, segments, guidance) for c in ex.claims if c
    ] + seg_sentences[1]
    notes = [
        *(tool_results.get("notes") or []),
        *(n for ex in (kpi, segments, guidance) for n in ex.notes),
        *q4_gap_notes(quarters),
    ]

    web_sids = [r["source_id"] for r in (tool_results.get("web") or []) if r.get("source_id")]
    transcript_sids = [r["transcript_sid"] for r in releases if r.get("transcript_sid")]
    evidence = list(
        dict.fromkeys(
            [
                *[r["source_id"] for r in releases],
                *results.source_ids,
                *kpi.source_ids,
                *segments.source_ids,
                *transcript_sids,
                *web_sids,
            ]
        )
    )
    coverage = "full" if (kpi.present and segments.present and results.present) else "partial"
    if not kpi.present and has_market:
        notes.append("consensus columns empty: the aggregator page gave no matching quarters")

    caveat = _caveat(ctx, quarters, tool_results)
    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=[c.model_dump() for c in claims],
        exhibit_md=exhibit_md,
        extra_rules=(
            "Name the aggregator and page date for every consensus figure; never call one page 'the Street'. "
            "Keep KPI definitions as the company states them (backlog, segment operating profit) and say which quarter each number belongs to. "
            "Management outlook ranges are management's, not consensus."
        ),
        critic_notes=critic_notes,
        keywords=["net sales", "backlog", "outlook", "consensus"],
    ) or template_digest(ctx, evidence, _lead_sentences(quarters, cons_rows, tool_results, seg))
    return finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=results.present or kpi.present,
        claims=claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Operating performance exhibits",
    )


def _segment_sentences(rel: dict[str, Any]) -> tuple[str, list[Any]]:
    """Quarterly segment-sales sentences from the latest release, as cited bullets + claims."""
    sid = rel["source_id"]
    label = (rel.get("columns") or [rel["quarter"]])[0] or rel["quarter"]
    sents = sentences_with(rel.get("_text", ""), ["net sales"]) if rel.get("_text") else []
    claims = []
    lines = []
    seen = set()
    for s in rel.get("segment_sales") or []:
        if s["segment"] in seen:
            continue
        seen.add(s["segment"])
        lines.append(
            f"- {s['segment']}: ${fmt_money(s['value'])} million net sales in {label} — “{s['text']}” [{sid}]"
        )
        from primer.sections.common import claim

        c = claim(
            10,
            f"segment_sales_{slug(s['segment'])}",
            s["value"],
            label,
            [sid],
            concept="release text",
        )
        if c:
            claims.append(c)
    for s in sents[:4]:
        lines.append(f"- {s} [{sid}]")
    if not lines:
        return "", []
    return (
        f"**Segment sales in the latest quarter ({label}, as stated in the release)**\n\n"
        + "\n".join(lines),
        claims,
    )


def _caveat(ctx: ToolContext, quarters: list[dict[str, Any]], tr: dict[str, Any]) -> str:
    bits = []
    if quarters:
        bits.append(
            f"quarterly actuals are XBRL where the 10-Q/10-K facts exist and the earnings release otherwise, labelled by calendar quarter of the period end (latest {quarters[0]['label']}); "
            "fourth-quarter revenue and operating income are derived as fiscal year minus nine-month year-to-date where XBRL reports no discrete quarter, "
            "while diluted EPS and net income are never derived that way and come from the Q4 release or read n/a"
        )
    cons = tr.get("consensus")
    if cons is not None and cons.source_id:
        bits.append(
            f"consensus and surprise figures are the Yahoo Finance analysis page as of {fmt_date(cons.as_of)}, one aggregator's compilation rather than a licensed consensus; the variance column is derived in-agent"
        )
    else:
        bits.append(
            "no consensus source was available in this run, so the consensus columns are n/a"
        )
    bits.append(
        "segment margins are derived from the 10-K segment tables (segment operating profit ÷ segment net sales), which exclude unallocated items; KPIs such as backlog are as the company defines them"
    )
    return caveat_line("; ".join(bits))


def _lead_sentences(
    quarters: list[dict[str, Any]],
    cons: dict[str, dict[str, Any]],
    tr: dict[str, Any],
    seg: dict[str, Any],
) -> list[str]:
    def cite(v: Val | None) -> str:
        return "".join(f" [{s}]" for s in (v.sids if v else []) if s)

    out: list[str] = []
    if quarters:
        q = quarters[0]
        v = q["vals"]
        bits = []
        for m in ("revenue", "net_income"):
            if v.get(m):
                bits.append(
                    f"{RESULT_LABELS[m].lower()} of ${fmt_money(v[m].value)} million{cite(v[m])}"
                )
        if v.get("eps_diluted"):
            bits.append(f"diluted EPS of ${v['eps_diluted'].value:,.2f}{cite(v['eps_diluted'])}")
        if bits:
            out.append(f"In {q['label']} the company reported " + ", ".join(bits) + ".")
        c = cons.get(q["label"])
        if c and c.get("eps_estimate") is not None and v.get("eps_diluted"):
            est = c["eps_estimate"]
            var = (v["eps_diluted"].value - est) / abs(est) * 100
            out.append(
                f"Against the Yahoo Finance consensus EPS of ${est:,.2f} for {q['label']} (page dated {c['as_of']}) [{c['source_id']}], the reported EPS was {var:+.1f}% (derived)."
            )
        if v.get("backlog"):
            out.append(
                f"Backlog at the end of {q['label']} was ${fmt_money(v['backlog'].value)} million as stated in the release{cite(v['backlog'])}."
            )
        if v.get("segment_operating_profit"):
            out.append(
                f"Business segment operating profit in {q['label']} was ${fmt_money(v['segment_operating_profit'].value)} million{cite(v['segment_operating_profit'])}."
            )
    if seg and seg.get("sales") and seg.get("profit"):
        sid = seg["source_id"]
        col = (seg.get("columns") or ["latest FY"])[0]
        by = {r.label.lower(): r for r in seg["profit"].rows}
        bits = []
        for r in seg["sales"].rows:
            p = by.get(r.label.lower())
            if p and r.values and r.values[0]:
                bits.append(f"{r.label} {p.values[0] / r.values[0] * 100:.1f}%")
        if bits:
            out.append(
                f"Segment operating margins in {col} (derived from the 10-K segment tables: operating profit ÷ net sales) were "
                + ", ".join(bits)
                + f" [{sid}]."
            )
    rel = (tr.get("releases") or [None])[0]
    if rel and rel.get("outlook"):
        o = next((x for x in rel["outlook"] if x["metric"] == "sales"), rel["outlook"][0])
        d = fmt_date(rel.get("release_date"))
        yr = rel.get("outlook_year") or (rel.get("release_date") or date.today()).year
        if o["cur_low"] is not None:
            f = (
                (lambda x: f"${x:,.2f}")
                if o["unit"] == "USD/shares"
                else (lambda x: f"${fmt_money(x)} million")
            )
            s = f"Management's {yr} outlook in the release dated {d} puts {o['label'].lower()} at {f(o['cur_low'])} to {f(o['cur_high'])}"
            if o["prior_low"] is not None:
                s += f", against {f(o['prior_low'])} to {f(o['prior_high'])} in the prior outlook ({o['prior_label']})"
            out.append(s + f" [{rel['source_id']}].")
    return out
