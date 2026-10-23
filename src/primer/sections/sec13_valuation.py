"""Section 13, Valuation (requirements §6 row 13; PLAN 2.5 item 2).

plan    -> quote and shares from the market-data adapter, latest balance-sheet instants and
           the latest full fiscal year from XBRL, consensus and price targets from the
           aggregator pages, the same XBRL derivation for each peer, then the web queries
collect -> runs them; every XBRL accession is registered as a tier-1 source (shared with
           section 9 through the same dedupe key), every page by the adapter
draft   -> LTM table (derived multiples with formulas), NTM table (separate, consensus),
           price-target range table (one row per aggregator), peer table (LTM only);
           narrative by the strong model when configured, else the template digest

When no market-data adapter exists (Firecrawl not configured) the price-dependent rows are
n/a, the XBRL-only rows still render and coverage is partial with a note saying why.
"""

from __future__ import annotations

import json
from typing import Any

from primer.adapters.market_data_scraped import xbrl_shares
from primer.clients.edgar import AnnualFinancials, EdgarClient, FactValue
from primer.evidence.store import EvidenceStore
from primer.sections import private
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
from primer.sections.exhibits_sec13 import (
    LABELS,
    Val,
    derive,
    ltm_table,
    ntm_table,
    peer_table,
    targets_table,
)
from primer.sections.sec09_financials import register_accessions
from primer.sections.style import fmt_money
from primer.state import Brief, Entity, SectionDraft

NO_ADAPTER_NOTE = "no market-data adapter (Firecrawl not configured)"
PEER_CAP = 5
FY_METRICS = (
    "revenue",
    "operating_income",
    "depreciation_amortization",
    "net_income",
    "eps_diluted",
    "free_cash_flow",
    "operating_cash_flow",
    "capex",
)
INSTANT_METRICS = (
    "cash",
    "short_term_investments",
    "cash_and_short_term_investments",
    "long_term_debt",
    "short_term_debt",
)


# ---- shared XBRL helpers (also used by sections 10 and 11) ---------------------------------


def register_instants(
    inst: dict[str, FactValue],
    entity: Entity,
    store: EvidenceStore,
    edgar: EdgarClient,
    known: dict[str, str] | None = None,
) -> dict[str, str]:
    """One tier-1 source record per filing accession the balance-sheet instants came from.
    The dedupe key is the one section 9 uses, so a 10-K accession shares its record."""
    out = dict(known or {})
    by_accn: dict[str, list[FactValue]] = {}
    for fv in inst.values():
        by_accn.setdefault(fv.accession, []).append(fv)
    api_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{entity.cik}.json"
    for accn, fvs in by_accn.items():
        if accn in out:
            continue
        facts = [
            {
                "metric": fv.metric,
                "concept": f"{fv.taxonomy}:{fv.concept}",
                "value": fv.value,
                "unit": fv.unit,
                "period": fv.period_label,
                "end": fv.end.isoformat(),
                "frame": fv.frame,
            }
            for fv in fvs
        ]
        form, filed = fvs[0].form, fvs[0].filed
        content = (
            f"# EDGAR XBRL companyfacts — {form} filed {filed.isoformat()} (accession {accn})\n\n"
            f"Source API: {api_url}\n\nSelected balance-sheet instants reported in this filing:\n\n```json\n"
            + json.dumps(facts, indent=1)
            + "\n```\n"
        )
        rec = store.register(
            content,
            type="api:edgar_xbrl",
            publisher="SEC EDGAR",
            title=f"XBRL companyfacts, {form} filed {filed.isoformat()} (accession {accn})",
            url=edgar.accession_index_url(entity.cik, accn),  # type: ignore[arg-type]
            published_date=filed,
            tier=1,
            gov_id=accn,
            meta={"api_url": api_url, "cik": entity.cik, "form": form, "fact_count": len(facts)},
            dedupe_key=f"edgar_xbrl|{entity.cik}|{accn}",
        )
        out[accn] = rec.source_id
    return out


def fy_vals(
    fin: AnnualFinancials, sids: dict[str, str], fy: int, metrics=FY_METRICS
) -> dict[str, Val]:
    """Val per metric for one fiscal year: reported facts cite their accession, derived
    facts (free cash flow) cite the operands and carry the formula."""
    out: dict[str, Val] = {}
    for m in metrics:
        fv = fin.facts.get(m, {}).get(fy)
        unit = "USD/shares" if m == "eps_diluted" else "USD"
        if fv is not None:
            out[m] = Val(
                fv.value,
                [s for s in [sids.get(fv.accession)] if s],
                f"FY{fy}",
                f"XBRL {fv.form} (FY{fy})",
                unit,
                f"{fv.taxonomy}:{fv.concept}",
            )
            continue
        dv = fin.derived.get(m, {}).get(fy)
        if dv is not None and m == "free_cash_flow":
            ops = [fin.facts.get(k, {}).get(fy) for k in ("operating_cash_flow", "capex")]
            op_sids = sorted({sids[o.accession] for o in ops if o and o.accession in sids})
            out[m] = Val(
                dv,
                op_sids,
                f"FY{fy}",
                "derived",
                unit,
                "derived",
                "operating cash flow − capital expenditures",
            )
    return out


def instant_vals(inst: dict[str, FactValue], sids: dict[str, str]) -> dict[str, Val]:
    out: dict[str, Val] = {}
    for m in INSTANT_METRICS:
        fv = inst.get(m)
        if fv is None:
            continue
        out[m] = Val(
            fv.value,
            [s for s in [sids.get(fv.accession)] if s],
            fv.period_label,
            f"XBRL {fv.form} balance-sheet instant",
            "USD",
            f"{fv.taxonomy}:{fv.concept}",
        )
    return out


def _shares_val(d: Any) -> Val | None:
    """The share count, labelled by where it came from: the adapter tries the XBRL cover page
    first and notes "page value used" when it fell back to a scraped statistics page."""
    if d is not None and "page value used" in (getattr(d, "note", None) or ""):
        v = _datum_val(d, "scraped statistics page (no XBRL cover-page count)", "shares")
        if v:
            v.concept = PAGE_FALLBACK_CONCEPT
        return v
    return _datum_val(d, "XBRL dei cover page", "shares", "dei:EntityCommonStockSharesOutstanding")


def _datum_val(d: Any, basis: str, unit: str, concept: str | None = None) -> Val | None:
    if d is None or not getattr(d, "ok", False):
        return None
    return Val(
        float(d.value),
        [d.source_id],
        d.period or fmt_date(d.as_of),
        basis,
        unit,
        concept,
    )


PAGE_FALLBACK_CONCEPT = "page:shares_outstanding"  # a page value used after XBRL was unavailable


# ---- handler ------------------------------------------------------------------------------


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    if private.is_private(ctx.entity):
        return private.plan_items(13, brief, ctx)
    tk, cik, as_of = ctx.entity.ticker, ctx.entity.cik, ctx.as_of.isoformat()
    peers = [p.get("ticker") for p in (ctx.foundation.get("peers") or [])[:PEER_CAP]]
    items: list[dict[str, Any]] = [
        {
            "tool": "market.quote",
            "args": {"ticker": tk},
            "why": "price from the Stooq close on the as-of date",
        },
        {
            "tool": "market.shares_outstanding",
            "args": {"ticker": tk},
            "why": "XBRL dei cover-page share count (tier 1)",
        },
        {
            "tool": "edgar.latest_instants",
            "args": {"cik": cik, "as_of": as_of},
            "why": "cash and debt from the latest 10-Q/10-K instant",
        },
        {
            "tool": "edgar.annual_financials",
            "args": {"cik": cik, "as_of": as_of},
            "why": "FY operating income, D&A, EPS, revenue, FCF for the LTM multiples",
        },
        {
            "tool": "market.consensus",
            "args": {"ticker": tk},
            "why": "NTM consensus EPS and revenue (separate table)",
        },
        {
            "tool": "market.price_targets",
            "args": {"ticker": tk},
            "why": "price-target range, one row per aggregator",
        },
        {
            "tool": "edgar.peer_multiples",
            "args": {"peers": peers, "as_of": as_of},
            "why": "same derivation for the section-5 peer set",
        },
    ]
    for q in expand_queries(brief, ctx)[: ctx.budget.searches]:
        items.append({"tool": "firecrawl.search", "query": q, "why": "narrative evidence only"})
    return items


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    ent, store, edgar = ctx.entity, ctx.store, ctx.edgar
    if private.is_private(ent):
        return private.collect_press(ctx, 13, plan_items, private.SECTION_KINDS[13])
    if not ent.cik:
        return {"error": "no CIK: company is not an SEC filer, no XBRL to derive multiples from"}
    out: dict[str, Any] = {"market_notes": []}
    fin = edgar.annual_financials(ent.cik, fiscal_year_end=ent.fiscal_year_end, as_of=ctx.as_of)
    sids = register_accessions(fin, ent, store, edgar)
    inst = edgar.latest_instants(ent.cik, ctx.as_of)
    sids = register_instants(inst, ent, store, edgar, sids)
    out.update({"financials": fin, "instants": inst, "accession_source_ids": sids})

    tk = ent.ticker or ""
    market = ctx.market
    if market is None:
        out["market_notes"].append(NO_ADAPTER_NOTE)
        out["shares"] = xbrl_shares(edgar, ent.cik, store, ctx.as_of)
    else:
        for key, fn in (
            ("quote", lambda: market.quote(tk)),
            ("shares", lambda: market.shares_outstanding(tk)),
            ("consensus", lambda: market.consensus(tk)),
            ("targets", lambda: market.price_targets(tk)),
            ("tipranks", lambda: market.tipranks(tk) if hasattr(market, "tipranks") else None),
        ):
            try:
                out[key] = fn()
            except Exception as e:  # one broken page never sinks the section
                ctx.log(f"market.{key} failed: {e!r}")
                out["market_notes"].append(f"market data {key}: {e!r}"[:160])
        q = out.get("quote")
        if q is not None and q.notes:
            out["market_notes"].extend(q.notes)

    peers = (ctx.foundation.get("peers") or [])[:PEER_CAP]
    out["peers"] = [_peer_row(ctx, p) for p in peers]

    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    out["web"] = ctx.web_research(
        queries, type="news", prefer_domains=tuple(ctx.brief.trade_press) if ctx.brief else ()
    )
    # budget notes live on this node's context only; carry them to the drafter explicitly
    out["market_notes"].extend(n for n in ctx.budget.notes if n not in out["market_notes"])
    return out


def _peer_row(ctx: ToolContext, peer: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": peer.get("name"),
        "ticker": peer.get("ticker"),
        "cik": peer.get("cik"),
        "vals": {},
        "fy": None,
        "error": None,
    }
    vals: dict[str, Val] = {}
    if not peer.get("cik"):
        # a rival the 10-K names that has no SEC filings (Airbus, BAE): n/a cells, never a
        # page-sourced substitute
        row["error"] = peer.get("note") or "no SEC filings"
        return row
    pent = Entity(name=peer.get("name") or "", ticker=peer.get("ticker"), cik=peer.get("cik"))
    try:
        fin = ctx.edgar.annual_financials(pent.cik, as_of=ctx.as_of)  # type: ignore[arg-type]
        if fin.fiscal_years:
            sids = register_accessions(fin, pent, ctx.store, ctx.edgar)
            inst = ctx.edgar.latest_instants(pent.cik, ctx.as_of)  # type: ignore[arg-type]
            sids = register_instants(inst, pent, ctx.store, ctx.edgar, sids)
            fy = fin.fiscal_years[-1]
            row["fy"] = fy
            vals.update(fy_vals(fin, sids, fy))
            vals.update(instant_vals(inst, sids))
            sh = xbrl_shares(ctx.edgar, pent.cik, ctx.store, ctx.as_of)
            if sh:
                v = _datum_val(
                    sh, "XBRL dei cover page", "shares", "dei:EntityCommonStockSharesOutstanding"
                )
                if v:
                    vals["shares"] = v
        else:
            row["error"] = "no annual XBRL facts"
    except Exception as e:
        row["error"] = f"companyfacts unavailable ({type(e).__name__})"
        ctx.log(f"peer {peer.get('ticker')} XBRL: {e!r}")
    if ctx.market is not None and peer.get("ticker"):
        try:
            hist = ctx.market.price_history(peer["ticker"])
            v = _datum_val(hist.close_as_of, "Stooq close", "USD/shares")
            if v:
                vals["price"] = v
            elif hist.notes:
                row["error"] = row["error"] or "; ".join(hist.notes)[:120]
        except Exception as e:
            row["error"] = row["error"] or f"price unavailable ({type(e).__name__})"
    elif ctx.market is None:
        row["error"] = row["error"] or NO_ADAPTER_NOTE
    row["vals"] = derive(vals, row["fy"] or ctx.fy) if vals else {}
    return row


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    if private.is_private(ctx.entity):
        return private.draft_press_section(
            brief, ctx, tool_results, section=13, critic_notes=critic_notes
        )
    fin: AnnualFinancials | None = tool_results.get("financials")
    inst: dict[str, FactValue] = tool_results.get("instants") or {}
    # a company with no 10-K yet is valued on its price, share count and 10-Q balance sheet
    if fin is None or (not fin.fiscal_years and not inst):
        return unavailable_draft(
            brief, tool_results.get("error") or "no annual XBRL facts available", ctx
        )
    sids: dict[str, str] = tool_results["accession_source_ids"]
    annual = bool(fin.fiscal_years)
    fy = fin.fiscal_years[-1] if annual else ctx.fy
    quote = tool_results.get("quote")
    shares = tool_results.get("shares")
    consensus = tool_results.get("consensus")
    notes: list[str] = list(tool_results.get("market_notes") or [])

    vals: dict[str, Val] = {}
    vals.update(fy_vals(fin, sids, fy))
    vals.update(instant_vals(inst, sids))
    price = _datum_val(quote.price, "Stooq close", "USD/shares") if quote is not None else None
    if price:
        vals["price"] = price
    sv = _shares_val(shares)
    if sv:
        vals["shares"] = sv
    vals = derive(vals, fy)
    has_price = "price" in vals

    ltm = ltm_table(vals, 13, has_price=has_price)
    ntm = ntm_table(vals.get("price"), vals.get("enterprise_value"), consensus, 13)
    targets = targets_table([tool_results.get("targets"), tool_results.get("tipranks")], 13)
    subject = {
        "name": ctx.co.title() if ctx.co.isupper() else ctx.co,
        "ticker": ctx.entity.ticker,
        "vals": vals,
        "fy": fy,
        "subject": True,
    }
    peers = peer_table([subject, *tool_results.get("peers", [])], 13)

    parts = [ltm.markdown]
    if ntm.present:
        parts.append(ntm.markdown)
    if targets.present:
        parts.append(targets.markdown)
    parts.append(peers.markdown)
    exhibit_md = "\n\n".join(p for p in parts if p)
    claims = [c for c in ltm.claims + ntm.claims + targets.claims + peers.claims if c]
    for ex in (ntm, targets):
        notes.extend(ex.notes)

    web = tool_results.get("web") or []
    web_sids = [r["source_id"] for r in web if r.get("source_id")]
    # first-appearance order, not set order: a set of strings iterates differently per process
    page_sids = [
        s
        for s in dict.fromkeys(c.source_ids[0] for c in claims if c.source_ids)
        if ctx.store.has(s)
    ]
    evidence = list(dict.fromkeys([*page_sids, *web_sids]))

    coverage = (
        "full" if (targets.present and peers.present and ltm.present and has_price) else "partial"
    )
    if not has_price:
        notes.append("share price unavailable: price-dependent multiples are n/a")
    if not targets.present:
        notes.append("price-target table unavailable")
    if not peers.present:
        notes.append("peer multiples not computable: " + "; ".join(peers.notes or ["no peer data"]))

    if not annual:
        notes.append(
            "no fiscal year of EBITDA or revenue is filed yet (no annual report), so earnings multiples are n/a"
        )
    caveat = _caveat(ctx, vals, fy, quote, shares, consensus, annual)
    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=[c.model_dump() for c in claims],
        exhibit_md=exhibit_md,
        extra_rules=(
            "Label every multiple as derived with its formula; never put LTM and NTM figures in one "
            "sentence as if comparable; name an aggregator (StockAnalysis, TipRanks, Yahoo Finance) and the "
            "page date whenever a target or consensus figure is mentioned; never average targets across pages."
        ),
        critic_notes=critic_notes,
        keywords=["price target", "valuation", "multiple"],
    ) or template_digest(ctx, evidence, _lead_sentences(ctx, vals, fy, tool_results))
    return finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=ltm.present,
        claims=claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Valuation exhibits",
    )


def _caveat(
    ctx: ToolContext,
    vals: dict[str, Val],
    fy: int,
    quote: Any,
    shares: Any,
    consensus: Any,
    annual: bool = True,
) -> str:
    price = vals.get("price")
    bits = []
    if price:
        bits.append(f"the share price is the Stooq close on {price.period}")
    else:
        bits.append("no share price was available, so price-dependent multiples are n/a")
    sh = vals.get("shares")
    if sh:
        if sh.concept == PAGE_FALLBACK_CONCEPT:
            bits.append(
                f"shares outstanding are the count on a scraped statistics page as of {sh.period}, "
                "because the filings carry no XBRL cover-page count"
            )
        else:
            bits.append(f"shares outstanding are the XBRL cover-page count as of {sh.period}")
    bits.append(
        "market cap, EV (market cap + total debt − cash − short-term investments)"
        + (f", EBITDA (FY{fy})" if annual else "")
        + " and every multiple are derived in-agent with the formulas shown; LTM and NTM figures are kept in separate tables"
        + (
            ""
            if annual
            else "; the company has not yet filed an annual report, so no fiscal year of earnings exists"
        )
    )
    if consensus is not None and consensus.source_id:
        bits.append(
            f"consensus estimates and price targets come from scraped aggregator pages dated {fmt_date(consensus.as_of)} and are not averaged across pages"
        )
    return caveat_line("; ".join(bits))


def _lead_sentences(
    ctx: ToolContext, vals: dict[str, Val], fy: int, tr: dict[str, Any]
) -> list[str]:
    def cite(v: Val | None) -> str:
        return "".join(f" [{s}]" for s in (v.sids if v else []) if s)

    out: list[str] = []
    p, sh, mc = vals.get("price"), vals.get("shares"), vals.get("market_cap")
    if p and sh and mc:
        out.append(
            f"At the Stooq close of ${p.value:,.2f} on {p.period}{cite(p)} and {fmt_money(sh.value, 'shares')} shares outstanding on the XBRL cover page{cite(sh)}, "
            f"market capitalisation is ${fmt_money(mc.value)} million (derived: price × shares)."
        )
    elif p is None:
        out.append(
            "No share price was available in this run, so market cap, EV and the price-based multiples are not computed."
        )
    ev, eb, td, nd = (
        vals.get("enterprise_value"),
        vals.get("ebitda"),
        vals.get("total_debt"),
        vals.get("net_debt"),
    )
    if td and nd:
        sti = vals.get("short_term_investments")
        held = (
            f"${fmt_money(vals['cash'].value)} million of cash{cite(vals['cash'])}"
            if vals.get("cash")
            else ""
        )
        if sti:
            held += f" and ${fmt_money(sti.value)} million of short-term investments{cite(sti)}"
        out.append(
            f"Total debt at {td.period} was ${fmt_money(td.value)} million (derived: long-term plus short-term debt){cite(td)}"
            + (f" against {held}" if held else "")
            + f", for net debt of ${fmt_money(nd.value)} million (derived: {nd.note}){cite(nd)}."
        )
    if eb:
        out.append(
            f"EBITDA for FY{fy} is ${fmt_money(eb.value)} million (derived: operating income plus depreciation and amortization){cite(eb)}."
        )
    if ev and eb and vals.get("ev_ebitda_ltm_ratio"):
        out.append(
            f"Enterprise value of ${fmt_money(ev.value)} million (derived: {ev.note}){cite(ev)} "
            f"implies {vals['ev_ebitda_ltm_ratio'].value:.1f}x FY{fy} EBITDA (derived)."
        )
    pe, pf = vals.get("pe_ltm_ratio"), vals.get("p_fcf_ltm_ratio")
    if pe or pf:
        bits = []
        if pe:
            bits.append(f"{pe.value:.1f}x FY{fy} diluted EPS{cite(vals.get('eps_diluted'))}")
        if pf:
            bits.append(f"{pf.value:.1f}x FY{fy} free cash flow{cite(vals.get('free_cash_flow'))}")
        out.append("The shares trade at " + " and ".join(bits) + " (derived).")
    fcf = vals.get("free_cash_flow")
    if fcf and not pf:
        out.append(
            f"Free cash flow in FY{fy} was ${fmt_money(fcf.value)} million (derived: operating cash flow minus capex){cite(fcf)}."
        )
    for t in (tr.get("targets"), tr.get("tipranks")):
        if t is not None and t.source_id and t.mean is not None and t.mean.ok:
            lo = f"${t.low.value:,.0f}" if (t.low and t.low.ok) else "n/a"
            hi = f"${t.high.value:,.0f}" if (t.high and t.high.ok) else "n/a"
            n = f" from {t.n_analysts} analysts" if t.n_analysts else ""
            out.append(
                f"{t.publisher} shows a mean price target of ${t.mean.value:,.2f} (low {lo}, high {hi}){n} as of {fmt_date(t.as_of)} [{t.source_id}]."
            )
    c = tr.get("consensus")
    if c is not None and c.source_id and c.eps_next is not None and c.eps_next.ok and p:
        out.append(
            f"Consensus EPS for {c.eps_next.period} is ${c.eps_next.value:,.2f} on the Yahoo Finance analysis page dated {fmt_date(c.as_of)} [{c.source_id}], "
            f"a forward P/E of {p.value / c.eps_next.value:.1f}x (derived: price ÷ consensus EPS)."
        )
    peers = tr.get("peers") or []
    missing = [
        r.get("ticker") or r.get("name") or "?"
        for r in peers
        if not r.get("vals", {}).get("ev_ebitda_ltm_ratio")
    ]
    if peers:
        out.append(
            f"The peer set from the 10-K competition text is {', '.join(r.get('ticker') or r.get('name') or '?' for r in peers)}"
            + (
                f"; LTM multiples could not be derived for {', '.join(missing)} and are shown as n/a rather than taken from an aggregator page."
                if missing
                else "."
            )
        )
    return out


__all__ = ["plan", "collect", "draft", "register_instants", "fy_vals", "instant_vals", "LABELS"]
