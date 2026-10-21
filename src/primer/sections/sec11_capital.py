"""Section 11, Capital Structure and Liquidity (requirements §6 row 11; PLAN 2.5 item 5).

plan    -> latest balance-sheet instants and the latest full fiscal year from XBRL, the 10-Q
           debt note and liquidity paragraphs, the 10-K liquidity paragraphs and debt note,
           then three rating/financing/buyback web queries
collect -> runs them; XBRL accessions registered as tier-1 sources (shared with sections 9
           and 13 through the dedupe key); rating sentences read from the filings and from
           the fetched pages
draft   -> debt and liquidity table (leverage rows always carry their definition), maturity
           list, credit-rating table (cited to a source whose text names the agency and the
           rating), capital-returns table; narrative by the model when configured, else the
           template digest
"""

from __future__ import annotations

from typing import Any

from primer.clients.edgar import AnnualFinancials, FactValue
from primer.sections.common import (
    ToolContext,
    caveat_line,
    draft_section,
    finalize_draft,
    fmt_date,
    template_digest,
    unavailable_draft,
)
from primer.sections.exhibits_sec11 import (
    capital_returns_table,
    debt_table,
    find_maturities,
    find_ratings,
    find_revolver,
    maturity_table,
    ratings_table,
)
from primer.sections.exhibits_sec13 import Val
from primer.sections.sec09_financials import register_accessions
from primer.sections.sec13_valuation import fy_vals, register_instants
from primer.sections.style import fmt_money
from primer.sections.text_tools import paragraphs_with
from primer.state import Brief, SectionDraft

WEB_QUERIES = (
    "{co} credit rating Moody's OR S&P OR Fitch",
    "{co} notes offering {fy}",
    "{co} share repurchase authorization",
)
FY_METRICS = (
    "operating_income",
    "depreciation_amortization",
    "interest_expense",
    "operating_cash_flow",
    "capex",
    "free_cash_flow",
    "dividends_paid",
    "buybacks",
)
INSTANTS = ("cash", "long_term_debt", "short_term_debt", "total_assets", "stockholders_equity")


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    cik, as_of = ctx.entity.cik, ctx.as_of.isoformat()
    items: list[dict[str, Any]] = [
        {
            "tool": "edgar.latest_instants",
            "args": {"cik": cik, "as_of": as_of},
            "why": "cash, debt, assets, equity from the latest 10-Q/10-K instant",
        },
        {
            "tool": "edgar.annual_financials",
            "args": {"cik": cik, "as_of": as_of},
            "why": "FY D&A, operating income, OCF, dividends, buybacks, interest expense",
        },
        {
            "tool": "foundation.10-Q",
            "args": {"items": ["1", "2"], "keywords": ["debt", "revolving", "rating", "maturit"]},
            "why": brief.structured_first[0] if brief.structured_first else "10-Q debt note",
        },
        {
            "tool": "foundation.10-K",
            "args": {"items": ["7", "8"], "keywords": ["liquidity", "notes due", "rating"]},
            "why": "10-K liquidity paragraphs and debt note",
        },
    ]
    subs = {"co": ctx.co, "fy": ctx.fy}
    for q in WEB_QUERIES[: ctx.budget.searches]:
        items.append(
            {
                "tool": "firecrawl.search",
                "query": q.format(**subs),
                "why": "rating-agency and financing press releases",
            }
        )
    return items


def _windows(ctx: ToolContext) -> dict[str, Any]:
    """Debt, revolver, rating and maturity paragraphs from the foundation filings, with the
    source id and period label of each filing."""
    out: dict[str, Any] = {"texts": []}
    tenq_rd = (ctx.foundation_filings("10-Q") or [{}])[0].get("report_date")
    tenk_rd = (ctx.foundation_filings("10-K") or [{}])[0].get("report_date")
    for kind, items, period in (("10-Q", ("1", "2"), tenq_rd), ("10-K", ("7", "8"), tenk_rd)):
        for item in items:
            text, sid = ctx.item_text(item, kind=kind)
            if not text or not sid:
                continue
            paras: dict[str, list[str]] = {}
            for key, kws in (
                ("debt", ["debt"]),
                ("revolving", ["revolving"]),
                ("rating", ["rat"]),
                ("maturity", ["maturit"]),
                ("notes_due", ["notes due"]),
                ("liquidity", ["liquidity"]),
            ):
                paras[key] = paragraphs_with(text, kws, window=0, max_paragraphs=4)
            out["texts"].append(
                {
                    "kind": kind,
                    "item": item,
                    "source_id": sid,
                    "period": period or fmt_date(ctx.store.get(sid).published_date),
                    "text": text,
                    "paragraphs": paras,
                }
            )
    return out


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    ent, store, edgar = ctx.entity, ctx.store, ctx.edgar
    if not ent.cik:
        return {"error": "no CIK: company is not an SEC filer, no XBRL debt or cash facts"}
    fin = edgar.annual_financials(ent.cik, fiscal_year_end=ent.fiscal_year_end, as_of=ctx.as_of)
    sids = register_accessions(fin, ent, store, edgar)
    inst = edgar.latest_instants(ent.cik, ctx.as_of)
    sids = register_instants(inst, ent, store, edgar, sids)
    out: dict[str, Any] = {"financials": fin, "instants": inst, "accession_source_ids": sids}
    out.update(_windows(ctx))

    # revolver, maturities and ratings from the filings (10-Q first, then 10-K)
    ratings: list[dict[str, Any]] = []
    for t in out["texts"]:
        label = f"{t['kind']} Item {t['item']}"
        date = t["period"]
        if "revolver" not in out:
            rv = find_revolver(t["text"])
            if rv:
                out["revolver"] = (rv[0], rv[1], t["source_id"], date)
        if "maturities" not in out:
            mats = find_maturities(t["text"])
            if mats:
                out["maturities"] = (mats, t["source_id"])
        for r in find_ratings(t["text"]):
            ratings.append({**r, "source_id": t["source_id"], "source_label": label, "date": date})

    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    web = ctx.web_research(
        queries,
        type="rating_agency",
        prefer_domains=("moodys.com", "spglobal.com", "fitchratings.com"),
    )
    out["web"] = web
    # budget notes live on this node's context only; carry them to the drafter explicitly
    out["notes"] = list(ctx.budget.notes)
    for row in web:
        sid = row.get("source_id")
        if not sid or not store.has(sid):
            continue
        rec = store.get(sid)
        for r in find_ratings(store.text(sid)):
            ratings.append(
                {
                    **r,
                    "source_id": sid,
                    "source_label": f"{rec.publisher}: {rec.title[:80]}",
                    "date": fmt_date(rec.published_date),
                }
            )
    out["ratings"] = ratings
    return out


def _vals(
    fin: AnnualFinancials, inst: dict[str, FactValue], sids: dict[str, str], fy: int
) -> dict[str, Val]:
    v = fy_vals(fin, sids, fy, FY_METRICS)
    for m in INSTANTS:
        fv = inst.get(m)
        if fv is None:
            continue
        v[m] = Val(
            fv.value,
            [s for s in [sids.get(fv.accession)] if s],
            fv.period_label,
            f"XBRL {fv.form} balance-sheet instant",
            "USD",
            f"{fv.taxonomy}:{fv.concept}",
        )
    lt, st, cash = v.get("long_term_debt"), v.get("short_term_debt"), v.get("cash")
    if lt:
        st_v = st.value if st else 0.0
        v["total_debt"] = Val(
            lt.value + st_v,  # type: ignore[operator]
            sorted(set(lt.sids) | set(st.sids if st else [])),
            lt.period,
            "derived",
            "USD",
            "derived",
            "long-term debt + short-term debt"
            + ("" if st else " (no current-debt fact; 0 assumed)"),
        )
    td = v.get("total_debt")
    if td and cash:
        v["net_debt"] = Val(
            td.value - cash.value,
            sorted(set(td.sids) | set(cash.sids)),
            td.period,
            "derived",
            "USD",
            "derived",
            "total debt − cash",
        )  # type: ignore[operator]
    oi, da = v.get("operating_income"), v.get("depreciation_amortization")
    if oi and da:
        v["ebitda"] = Val(
            oi.value + da.value,
            sorted(set(oi.sids) | set(da.sids)),
            f"FY{fy}",
            "derived",
            "USD",
            "derived",
            "FY operating income + FY depreciation and amortization",
        )  # type: ignore[operator]
    eb = v.get("ebitda")
    if eb and eb.value:
        if td:
            v["gross_leverage"] = Val(
                td.value / eb.value,
                sorted(set(td.sids) | set(eb.sids)),
                f"FY{fy}",
                "derived",
                "x",
                "derived",
                "total debt ÷ (FY operating income + FY D&A)",
            )  # type: ignore[operator]
        nd = v.get("net_debt")
        if nd:
            v["net_leverage"] = Val(
                nd.value / eb.value,
                sorted(set(nd.sids) | set(eb.sids)),
                f"FY{fy}",
                "derived",
                "x",
                "derived",
                "net debt ÷ (FY operating income + FY D&A)",
            )  # type: ignore[operator]
    ie = v.get("interest_expense")
    if oi and ie and ie.value:
        v["interest_coverage_ratio"] = Val(
            oi.value / ie.value,
            sorted(set(oi.sids) | set(ie.sids)),
            f"FY{fy}",
            "derived",
            "x",
            "derived",
            "FY operating income ÷ FY interest expense",
        )  # type: ignore[operator]
    return v


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    fin: AnnualFinancials | None = tool_results.get("financials")
    if fin is None or not fin.fiscal_years:
        return unavailable_draft(
            brief, tool_results.get("error") or "no annual XBRL facts available", ctx
        )
    sids: dict[str, str] = tool_results["accession_source_ids"]
    inst: dict[str, FactValue] = tool_results.get("instants") or {}
    fy = fin.fiscal_years[-1]
    vals = _vals(fin, inst, sids, fy)

    debt = debt_table(vals, tool_results.get("revolver"), fy, 11)
    mats = tool_results.get("maturities")
    maturities = maturity_table(mats[0], mats[1], 11) if mats else maturity_table([], "", 11)
    ratings = ratings_table(tool_results.get("ratings") or [])
    returns = capital_returns_table(fin, sids, 11)

    parts = [debt.markdown]
    for ex in (maturities, ratings, returns):
        if ex.present:
            parts.append(ex.markdown)
    exhibit_md = "\n\n".join(parts)
    claims = [c for ex in (debt, maturities, returns) for c in ex.claims if c]
    notes = [
        *(tool_results.get("notes") or []),
        *(n for ex in (maturities, ratings) for n in ex.notes),
    ]

    texts = tool_results.get("texts") or []
    web_sids = [r["source_id"] for r in (tool_results.get("web") or []) if r.get("source_id")]
    evidence = list(
        dict.fromkeys(
            [*debt.source_ids, *[t["source_id"] for t in texts], *ratings.source_ids, *web_sids]
        )
    )
    coverage = "full" if (debt.present and "gross_leverage" in vals and texts) else "partial"
    if "gross_leverage" not in vals:
        notes.append("leverage not computable (missing operating income, D&A or debt facts)")

    caveat = _caveat(vals, fy, inst)
    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=[c.model_dump() for c in claims],
        exhibit_md=exhibit_md,
        extra_rules=(
            "Every leverage figure must state its definition in the same sentence (e.g. 'total debt ÷ FY EBITDA'); "
            "a credit rating may be stated only with the agency name and a citation to a source whose text carries it; "
            "do not mix balance-sheet dates."
        ),
        critic_notes=critic_notes,
        keywords=["debt", "revolving", "rated", "notes due", "repurchase"],
    ) or template_digest(ctx, evidence, _lead_sentences(vals, fy, tool_results))
    return finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=debt.present,
        claims=claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Capital structure exhibits",
    )


def _caveat(vals: dict[str, Val], fy: int, inst: dict[str, FactValue]) -> str:
    d = (
        vals["cash"].period
        if vals.get("cash")
        else (vals["long_term_debt"].period if vals.get("long_term_debt") else "n/a")
    )
    form = next((fv.form for fv in inst.values()), "10-Q/10-K")
    return caveat_line(
        f"balance-sheet figures are the latest XBRL instant ({form}, {d}); EBITDA, leverage and interest coverage are derived "
        f"in-agent for FY{fy} with the definitions in the row names; revolver capacity, maturities and ratings are read from the "
        f"filing text and cited to it; rating-agency pages are second-hand unless the agency's own release was fetched"
    )


def _lead_sentences(vals: dict[str, Val], fy: int, tr: dict[str, Any]) -> list[str]:
    def cite(v: Val | None) -> str:
        return "".join(f" [{s}]" for s in (v.sids if v else []) if s)

    out: list[str] = []
    cash, td, nd = vals.get("cash"), vals.get("total_debt"), vals.get("net_debt")
    if cash and td:
        s = f"At {cash.period} the company held ${fmt_money(cash.value)} million of cash{cite(cash)} against ${fmt_money(td.value)} million of total debt (derived: long-term plus short-term debt){cite(td)}"
        if nd:
            s += f", for net debt of ${fmt_money(nd.value)} million (derived){cite(nd)}"
        out.append(s + ".")
    gl, nl, eb = vals.get("gross_leverage"), vals.get("net_leverage"), vals.get("ebitda")
    if gl and eb:
        s = f"Gross leverage is {gl.value:.1f}x, defined as total debt divided by FY{fy} EBITDA of ${fmt_money(eb.value)} million (derived: operating income plus D&A){cite(eb)}"
        if nl:
            s += f"; net debt to EBITDA on the same definition is {nl.value:.1f}x (derived)"
        out.append(s + ".")
    ic = vals.get("interest_coverage_ratio")
    if ic:
        out.append(
            f"Interest coverage is {ic.value:.1f}x, defined as FY{fy} operating income divided by interest expense (derived){cite(vals.get('interest_expense'))}."
        )
    rv = tr.get("revolver")
    if rv:
        out.append(
            f"The filing states a ${fmt_money(rv[0])} million revolving credit facility (“{rv[1]}”) [{rv[2]}]."
        )
    mats = tr.get("maturities")
    if mats:
        items, sid = mats
        bits = [
            f"${fmt_money(i['amount'])} million of {i['coupon']:.2f}% notes due {i['year']}"
            for i in items[:4]
        ]
        out.append("Disclosed note maturities include " + "; ".join(bits) + f" [{sid}].")
    seen = set()
    for r in tr.get("ratings") or []:
        if r["agency"] in seen:
            continue
        seen.add(r["agency"])
        out.append(
            f"{r['agency']} rates the senior unsecured debt {r['rating']} according to the cited text [{r['source_id']}]."
        )
    dv, bb = vals.get("dividends_paid"), vals.get("buybacks")
    if dv or bb:
        bits = []
        if dv:
            bits.append(f"${fmt_money(dv.value)} million of dividends{cite(dv)}")
        if bb:
            bits.append(f"${fmt_money(bb.value)} million of share repurchases{cite(bb)}")
        out.append(f"Capital returned to shareholders in FY{fy}: " + " and ".join(bits) + ".")
    return out
