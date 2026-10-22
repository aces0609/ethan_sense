"""Section 13 exhibits (requirements §6 row 13, PLAN 2.5 item 2).

Four deterministic tables, every cell cited and every derived figure labelled with its
formula: the LTM multiples table (XBRL + Stooq close), the NTM table (consensus from the
Yahoo analysis page) kept strictly separate from the LTM table, the price-target range
table (one row per aggregator, never averaged) and the peer multiples table (LTM only, the
same derivation applied to each peer's XBRL; missing peer data renders as n/a).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from primer.sections.common import claim, md_table
from primer.sections.exhibits import ExhibitResult
from primer.sections.style import fmt_money
from primer.state import Claim


@dataclass
class Val:
    """One exhibit value with its provenance; `concept="derived"` carries the formula in note."""

    value: float | None
    sids: list[str] = field(default_factory=list)
    period: str = ""
    basis: str = ""
    unit: str = "USD"
    concept: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


NET_DEBT_FORMULA = "total debt − cash − short-term investments"
NO_STI_SUFFIX = " (no short-term investments fact reported; 0 assumed)"


def net_debt_formula(has_short_term_investments: bool) -> str:
    """The net-debt formula as printed wherever net debt appears (sections 9, 11, 13 and the
    derived-figures appendix); says when the filer reports no short-term investments."""
    return NET_DEBT_FORMULA + ("" if has_short_term_investments else NO_STI_SUFFIX)


FORMULAS: dict[str, str] = {
    "market_cap": "share price × shares outstanding",
    "total_debt": "long-term debt + short-term debt (latest balance-sheet instant)",
    "enterprise_value": "market cap + total debt − cash − short-term investments",
    "net_debt": NET_DEBT_FORMULA,
    "short_term_investments": "cash, equivalents and short-term investments − cash",
    "ebitda": "FY operating income + FY depreciation and amortization",
    "ev_ebitda_ltm_ratio": "enterprise value ÷ EBITDA (latest full fiscal year)",
    "pe_ltm_ratio": "share price ÷ FY diluted EPS",
    "p_fcf_ltm_ratio": "market cap ÷ FY free cash flow",
    "ev_sales_ltm_ratio": "enterprise value ÷ FY revenue",
    "pe_ntm_ratio": "share price ÷ consensus EPS for the next fiscal year",
    "ev_sales_ntm_ratio": "enterprise value ÷ consensus revenue for the next fiscal year",
}

LABELS: dict[str, str] = {
    "price": "Share price",
    "shares": "Shares outstanding",
    "market_cap": "Market capitalisation",
    "cash": "Cash and equivalents",
    "short_term_investments": "Short-term investments and marketable securities",
    "long_term_debt": "Long-term debt",
    "short_term_debt": "Short-term debt and current maturities",
    "total_debt": "Total debt",
    "net_debt": "Net debt",
    "enterprise_value": "Enterprise value (EV)",
    "revenue": "Revenue",
    "operating_income": "Operating income",
    "depreciation_amortization": "Depreciation and amortization",
    "ebitda": "EBITDA",
    "net_income": "Net income",
    "eps_diluted": "Diluted EPS",
    "free_cash_flow": "Free cash flow",
    "ev_ebitda_ltm_ratio": "EV/EBITDA (LTM)",
    "pe_ltm_ratio": "P/E (LTM)",
    "p_fcf_ltm_ratio": "P/FCF (LTM)",
    "ev_sales_ltm_ratio": "EV/Sales (LTM)",
}

LTM_ROWS = (
    "price",
    "shares",
    "market_cap",
    "cash",
    "short_term_investments",
    "long_term_debt",
    "short_term_debt",
    "total_debt",
    "net_debt",
    "enterprise_value",
    "revenue",
    "operating_income",
    "depreciation_amortization",
    "ebitda",
    "net_income",
    "eps_diluted",
    "free_cash_flow",
    "ev_ebitda_ltm_ratio",
    "pe_ltm_ratio",
    "p_fcf_ltm_ratio",
    "ev_sales_ltm_ratio",
)

PRICE_DEPENDENT = (
    "market_cap",
    "enterprise_value",
    "ev_ebitda_ltm_ratio",
    "pe_ltm_ratio",
    "p_fcf_ltm_ratio",
    "ev_sales_ltm_ratio",
)


def _sids(*vals: Val | None) -> list[str]:
    out: list[str] = []
    for v in vals:
        if v:
            out.extend(s for s in v.sids if s and s not in out)
    return out


def _derived(
    key: str, value: float | None, period: str, unit: str, *operands: Val | None
) -> Val | None:
    if value is None or any(o is None or not o.ok for o in operands):
        return None
    return Val(
        value,
        _sids(*operands),
        period,
        "derived",
        unit,
        concept="derived",
        note=FORMULAS[key],
    )


def _div(a: Val | None, b: Val | None) -> float | None:
    if a is None or b is None or not a.ok or not b.ok or not b.value:
        return None
    return a.value / b.value  # type: ignore[operator]


def short_term_investments_val(v: dict[str, Val]) -> Val | None:
    """Short-term investments and marketable securities: the reported fact when the filer
    has one, else the combined cash-and-short-term-investments fact minus cash (derived),
    else None (the caller nets cash only and says so)."""
    sti = v.get("short_term_investments")
    if sti and sti.ok:
        return sti
    combined, cash = v.get("cash_and_short_term_investments"), v.get("cash")
    if combined and combined.ok and cash and cash.ok and combined.value >= cash.value:  # type: ignore[operator]
        return _derived(
            "short_term_investments",
            combined.value - cash.value,  # type: ignore[operator]
            combined.period,
            "USD",
            combined,
            cash,
        )
    return None


def derive(v: dict[str, Val], fy: int) -> dict[str, Val]:
    """Add market cap, total debt, net debt, EV, EBITDA and the LTM multiples to the inputs
    (price, shares, cash, short_term_investments, long_term_debt, short_term_debt, revenue,
    operating_income, depreciation_amortization, eps_diluted, free_cash_flow). Net debt and
    EV net cash and short-term investments; a filer with no short-term-investments fact nets
    cash only and the formula note says so. Other missing operands leave the derived value
    absent, never substituted."""
    out = dict(v)
    fyl = f"FY{fy} (latest full year)"
    price, shares = out.get("price"), out.get("shares")
    if price and shares and price.ok and shares.ok:
        out["market_cap"] = _derived(
            "market_cap", price.value * shares.value, price.period, "USD", price, shares
        )  # type: ignore[operator]
    lt, st, cash = out.get("long_term_debt"), out.get("short_term_debt"), out.get("cash")
    if lt and lt.ok:
        st_v = st.value if (st and st.ok) else 0.0
        out["total_debt"] = Val(
            lt.value + st_v,  # type: ignore[operator]
            _sids(lt, st),
            lt.period,
            "derived",
            "USD",
            concept="derived",
            note=FORMULAS["total_debt"]
            + ("" if (st and st.ok) else "; no current-debt fact, 0 assumed"),
        )
    sti = short_term_investments_val(out)
    if sti:
        out["short_term_investments"] = sti
    else:
        out.pop("short_term_investments", None)
    sti_v = sti.value if sti else 0.0
    td = out.get("total_debt")
    if td and cash and cash.ok:
        nd = _derived("net_debt", td.value - cash.value - sti_v, td.period, "USD", td, cash, sti)  # type: ignore[operator]
        if nd is None:  # sti is None: nets cash only, and the note says so
            nd = _derived("net_debt", td.value - cash.value, td.period, "USD", td, cash)  # type: ignore[operator]
            nd.note = net_debt_formula(False)  # type: ignore[union-attr]
        out["net_debt"] = nd
    mc = out.get("market_cap")
    if mc and td and cash and cash.ok:
        ev_value = mc.value + td.value - cash.value - sti_v  # type: ignore[operator]
        ev = _derived("enterprise_value", ev_value, mc.period, "USD", mc, td, cash, sti)
        if ev is None:
            ev = _derived("enterprise_value", ev_value, mc.period, "USD", mc, td, cash)
            ev.note = FORMULAS["enterprise_value"] + NO_STI_SUFFIX  # type: ignore[union-attr]
        out["enterprise_value"] = ev
    oi, da = out.get("operating_income"), out.get("depreciation_amortization")
    if oi and da and oi.ok and da.ok:
        out["ebitda"] = _derived("ebitda", oi.value + da.value, fyl, "USD", oi, da)  # type: ignore[operator]
    ev, eb = out.get("enterprise_value"), out.get("ebitda")
    out_rows = {
        "ev_ebitda_ltm_ratio": (ev, eb),
        "pe_ltm_ratio": (price, out.get("eps_diluted")),
        "p_fcf_ltm_ratio": (mc, out.get("free_cash_flow")),
        "ev_sales_ltm_ratio": (ev, out.get("revenue")),
    }
    for key, (a, b) in out_rows.items():
        r = _div(a, b)
        if r is not None:
            out[key] = _derived(key, r, fyl, "x", a, b)
    return {k: x for k, x in out.items() if x is not None}


def fmt_val(v: Val | None) -> str:
    if v is None or not v.ok:
        return "n/a"
    x = float(v.value)  # type: ignore[arg-type]
    if v.unit == "x":
        s = f"{x:,.1f}x"
    elif v.unit == "USD/shares":
        s = f"{x:,.2f}"
    elif v.unit == "shares":
        s = fmt_money(x, "shares")
    elif v.unit == "pct":
        s = f"{x:.1f}%"
    else:
        s = fmt_money(x)
    return s + "".join(f" [{sid}]" for sid in v.sids if sid)


def _claim(section: int, metric: str, v: Val, period: str | None = None) -> Claim | None:
    return claim(
        section,
        metric,
        v.value,
        period or v.period,
        v.sids,
        unit=v.unit,
        concept=v.concept,
        note=v.note,
    )


# ---- LTM table --------------------------------------------------------------------------


def ltm_table(vals: dict[str, Val], section: int = 13, has_price: bool = True) -> ExhibitResult:
    rows: list[list[Any]] = []
    claims: list[Claim] = []
    used: set[str] = set()
    for key in LTM_ROWS:
        v = vals.get(key)
        label = LABELS[key]
        if v is None:
            if key in PRICE_DEPENDENT and not has_price:
                rows.append([label + " — derived", "n/a (no quote)", "", "derived"])
            elif key in PRICE_DEPENDENT or key in ("total_debt", "net_debt", "ebitda"):
                rows.append([label + " — derived", "n/a", "", "derived"])
            elif key == "short_term_investments":
                rows.append([label, "n/a (no XBRL fact; 0 assumed in net debt and EV)", "", "XBRL"])
            continue
        if v.concept == "derived":
            label += f" — derived: {v.note}"
        rows.append([label, fmt_val(v), v.period, v.basis])
        used.update(s for s in v.sids if s)
        metric = (
            "share_price" if key == "price" else "shares_outstanding" if key == "shares" else key
        )
        c = _claim(section, metric, v)
        if c:
            claims.append(c)
    md = (
        "**LTM multiples** (USD millions unless noted; derived rows computed in-agent)\n\n"
        + md_table(["Metric", "Value", "Period", "Basis"], rows)
    )
    formulas = [
        f"- {LABELS[k]}: {FORMULAS[k]}"
        for k in (
            "market_cap",
            "total_debt",
            "net_debt",
            "enterprise_value",
            "ebitda",
            "ev_ebitda_ltm_ratio",
            "pe_ltm_ratio",
            "p_fcf_ltm_ratio",
            "ev_sales_ltm_ratio",
        )
    ]
    md += "\n\nFormulas (all derived in-agent):\n\n" + "\n".join(formulas)
    present = any(k in vals for k in ("ebitda", "net_debt", "free_cash_flow"))
    return ExhibitResult(md, claims, sorted(used), present, [])


# ---- NTM table (never merged with the LTM table) --------------------------------------------


def ntm_table(
    price: Val | None, ev: Val | None, consensus: Any, section: int = 13
) -> ExhibitResult:
    """Forward P/E and EV/Sales NTM from the Yahoo consensus for the next fiscal year."""
    if consensus is None:
        return ExhibitResult("", [], [], False, ["no consensus page"])
    rows: list[list[Any]] = []
    claims: list[Claim] = []
    used: set[str] = set()
    as_of = consensus.as_of.isoformat() if consensus.as_of else "n.d."
    page = f"Yahoo Finance analysis page, {as_of}"

    def pick(nxt: Any, cur: Any) -> Any:
        if nxt is not None and nxt.ok:
            return nxt
        if cur is not None and cur.ok:
            return cur
        return None

    eps = pick(consensus.eps_next, consensus.eps_current)
    rev = pick(consensus.revenue_next, consensus.revenue_current)
    if eps is not None:
        v = Val(eps.value, [eps.source_id], eps.period or "", page, "USD/shares", "consensus")
        rows.append([f"Consensus EPS {v.period}", fmt_val(v), v.period, page])
        claims.append(_claim(section, "eps_consensus", v))
        used.add(eps.source_id)
        if price and price.ok:
            r = _div(price, v)
            if r is not None:
                d = Val(
                    r,
                    _sids(price, v),
                    v.period,
                    "derived",
                    "x",
                    "derived",
                    FORMULAS["pe_ntm_ratio"],
                )
                rows.append(
                    [
                        f"Forward P/E ({v.period}) — derived: {d.note}",
                        fmt_val(d),
                        d.period,
                        "derived",
                    ]
                )
                claims.append(_claim(section, "pe_ntm_ratio", d))
    if rev is not None:
        v = Val(rev.value, [rev.source_id], rev.period or "", page, "USD", "consensus")
        rows.append([f"Consensus revenue {v.period}", fmt_val(v), v.period, page])
        claims.append(_claim(section, "revenue_consensus", v))
        used.add(rev.source_id)
        if ev and ev.ok:
            r = _div(ev, v)
            if r is not None:
                d = Val(
                    r,
                    _sids(ev, v),
                    v.period,
                    "derived",
                    "x",
                    "derived",
                    FORMULAS["ev_sales_ntm_ratio"],
                )
                rows.append(
                    [
                        f"EV/Sales NTM ({v.period}) — derived: {d.note}",
                        fmt_val(d),
                        d.period,
                        "derived",
                    ]
                )
                claims.append(_claim(section, "ev_sales_ntm_ratio", d))
    if consensus.n_analysts:
        rows.append(
            [
                "Analysts in consensus",
                f"{consensus.n_analysts} [{consensus.source_id}]",
                as_of,
                page,
            ]
        )
    if not rows:
        return ExhibitResult("", [], [], False, ["consensus page returned no estimates"])
    md = (
        "**NTM (forward) multiples** — consensus estimates from the cited aggregator page; "
        "kept separate from the LTM table above\n\n"
        + md_table(["Metric", "Value", "Period", "Basis"], rows)
    )
    return ExhibitResult(md, [c for c in claims if c], sorted(s for s in used if s), True, [])


# ---- price-target range table -------------------------------------------------------------


def targets_table(targets: list[Any], section: int = 13) -> ExhibitResult:
    """One row per aggregator page (publisher, low, mean, high, analysts, buy/hold/sell,
    as-of date), each cited to its page. Values are never averaged across pages."""
    rows: list[list[Any]] = []
    claims: list[Claim] = []
    used: set[str] = set()
    notes: list[str] = []
    for t in targets:
        if t is None or not t.source_id:
            continue
        pub = (t.publisher or "aggregator").strip()
        slug = pub.lower().replace(" ", "_")
        as_of = t.as_of.isoformat() if t.as_of else "n.d."
        cite = f" [{t.source_id}]"
        cells: list[str] = []
        any_val = False
        for name, d in (("low", t.low), ("mean", t.mean), ("high", t.high)):
            if d is not None and d.ok:
                any_val = True
                cells.append(f"{d.value:,.2f}{cite}")
                claims.append(
                    claim(
                        section,
                        f"price_target_{name}_{slug}",
                        d.value,
                        as_of,
                        [t.source_id],
                        unit="USD/shares",
                        concept="aggregator consensus",
                        note=f"{pub} page, as of {as_of}",
                    )
                )
            else:
                cells.append("n/a")
        if not any_val:
            notes.append(f"{pub}: {'; '.join(t.notes) if t.notes else 'no targets on the page'}")
            continue
        used.add(t.source_id)
        n = f"{t.n_analysts}{cite}" if t.n_analysts is not None else "n/a"
        if t.n_analysts is not None:
            claims.append(
                claim(
                    section,
                    f"analyst_count_{slug}",
                    t.n_analysts,
                    as_of,
                    [t.source_id],
                    unit="count",
                )
            )
        bhs = (
            f"{t.buy if t.buy is not None else 'n/a'} / {t.hold if t.hold is not None else 'n/a'} / "
            f"{t.sell if t.sell is not None else 'n/a'}{cite}"
            if any(x is not None for x in (t.buy, t.hold, t.sell))
            else "n/a"
        )
        rating = f"{t.consensus_rating}{cite}" if t.consensus_rating else "n/a"
        rows.append([pub, *cells, n, bhs, rating, as_of])
    if not rows:
        return ExhibitResult("", [], [], False, notes or ["no price-target page available"])
    md = (
        "**Price-target range** (USD per share; one row per aggregator, cited to that page; not averaged)\n\n"
        + md_table(
            [
                "Publisher",
                "Low",
                "Mean",
                "High",
                "Analysts",
                "Buy / Hold / Sell",
                "Consensus",
                "As of",
            ],
            rows,
        )
    )
    return ExhibitResult(md, [c for c in claims if c], sorted(used), True, notes)


# ---- peer multiples table (LTM only) --------------------------------------------------------


PEER_COLS = (
    ("price", "Price"),
    ("market_cap", "Market cap"),
    ("enterprise_value", "EV"),
    ("ebitda", "EBITDA (FY)"),
    ("ev_ebitda_ltm_ratio", "EV/EBITDA"),
    ("pe_ltm_ratio", "P/E"),
    ("ev_sales_ltm_ratio", "EV/Sales"),
)


def peer_table(rows_in: list[dict[str, Any]], section: int = 13) -> ExhibitResult:
    """rows_in: [{name, ticker, vals: {key: Val}, fy, error, subject: bool}]. One LTM table;
    a peer whose XBRL or price is missing shows n/a cells, never a page-sourced substitute."""
    rows: list[list[Any]] = []
    claims: list[Claim] = []
    used: set[str] = set()
    notes: list[str] = []
    computed = 0
    for r in rows_in:
        vals: dict[str, Val] = r.get("vals") or {}
        tk = (r.get("ticker") or "").lower()
        cells: list[str] = []
        for key, _label in PEER_COLS:
            v = vals.get(key)
            cells.append(fmt_val(v))
            if v is not None and v.ok:
                used.update(s for s in v.sids if s)
                if not r.get("subject"):
                    metric = f"peer_{tk}_" + ("share_price" if key == "price" else key)
                    c = _claim(section, metric, v)
                    if c:
                        claims.append(c)
        if any(vals.get(k) for k in ("ev_ebitda_ltm_ratio", "pe_ltm_ratio", "ev_sales_ltm_ratio")):
            if not r.get("subject"):
                computed += 1
        elif not r.get("subject"):
            notes.append(f"{r.get('ticker')}: {r.get('error') or 'XBRL or price unavailable'}")
        period = f"FY{r['fy']}" if r.get("fy") else "n/a"
        rows.append([f"{r.get('name')} ({r.get('ticker')})", *cells, period])
    if not rows:
        return ExhibitResult("", [], [], False, ["no peers in the foundation pack"])
    md = (
        "**Peer multiples, LTM only** (USD millions; price = Stooq close; every multiple derived "
        "from the peer's own XBRL with the formulas above; n/a = data not available)\n\n"
        + md_table(["Company", *[lbl for _, lbl in PEER_COLS], "Fiscal year"], rows)
    )
    return ExhibitResult(md, claims, sorted(used), computed > 0, notes)
