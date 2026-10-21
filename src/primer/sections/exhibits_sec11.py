"""Section 11 exhibits (requirements §6 row 11, PLAN 2.5 item 5).

Debt and liquidity table (cash, total debt, net debt, undrawn revolver, leverage rows that
ALWAYS carry their definition in the row name, interest coverage), the maturity list parsed
from the debt note, the credit-rating table (one row per agency, cited to a source whose text
names the agency and the rating) and the capital-returns table (dividends, buybacks, OCF, FCF
from XBRL with the same metric names and accession source ids section 9 uses).
"""

from __future__ import annotations

import re
from typing import Any

from primer.clients.edgar import AnnualFinancials
from primer.sections.common import claim, md_table
from primer.sections.exhibits import ExhibitResult
from primer.sections.exhibits_sec13 import Val, fmt_val
from primer.sections.style import fmt_money
from primer.state import Claim

GROSS_LEVERAGE_LABEL = "Gross debt / EBITDA (derived: total debt ÷ (FY operating income + FY D&A))"
NET_LEVERAGE_LABEL = "Net debt / EBITDA (derived: net debt ÷ (FY operating income + FY D&A))"
COVERAGE_LABEL = "Interest coverage (derived: FY operating income ÷ FY interest expense)"

REVOLVER_RE = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s*(billion|million)\s+(?:of\s+(?:available|undrawn|unused)\s+(?:capacity|borrowing capacity|commitments?)\s+under\s+(?:our|its|the)\s+)?"
    r"(?:senior\s+)?(?:unsecured\s+)?(?:multi-year\s+)?revolving credit (?:facility|agreement)",
    re.I,
)
NOTES_DUE_RE = re.compile(
    r"\$\s?(\d+(?:[.,]\d+)*)\s*(billion|million)\s+(?:(?:aggregate\s+)?principal\s+amount\s+)?of\s+(?:(?:its|our)\s+)?"
    r"(\d+(?:\.\d+)?)\s?%\s+(?:senior\s+)?(?:unsecured\s+)?notes\s+due\s+(?:in\s+)?(20\d{2})",
    re.I,
)
# agency names match case-insensitively; rating symbols are case-sensitive so the article
# "a" or the word "at" can never be read as a rating
AGENCY = r"(?i:(S&P(?: Global)?(?: Ratings)?|Standard\s*&\s*Poor'?s|Moody'?s(?: Investors Service)?|Fitch(?: Ratings)?))"
RATING = r"(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|Aaa|Aa[123]?|A[123]|Baa[123]?|Ba[123]?|B[123]|Caa[123]?)"
RATING_THEN_AGENCY = re.compile(RATING + r"(?![\w-])\s*(?:\(|(?i:by|from)\s+)\s*" + AGENCY)
AGENCY_QUOTED_RATING = re.compile(AGENCY + r"[^.;]{0,160}?['\"‘’(]" + RATING + r"['\"’)]")
AGENCY_THEN_RATING = re.compile(
    AGENCY
    + r"(?:'s)?\s*(?:\(|:|-|(?i:rating(?: of)?|rates?(?: at)?|at))?\s*"
    + RATING
    + r"(?![\w-])"
)
RATING_WORDS = re.compile(r"\b(rated|rating|ratings|affirm\w*|upgrade\w*|downgrade\w*)\b", re.I)
SCALE = {"billion": 1e9, "million": 1e6}


def _agency(name: str) -> str:
    n = name.lower()
    if "moody" in n:
        return "Moody's"
    if "fitch" in n:
        return "Fitch"
    return "S&P"


def find_ratings(text: str) -> list[dict[str, str]]:
    """(agency, rating, sentence) triples from sentences that mention a rating agency and
    'rated' or 'rating'. Agency-first and rating-first phrasings are both read."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sent in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        if not RATING_WORDS.search(sent) or not re.search(AGENCY, sent):
            continue
        pairs: list[tuple[str, str]] = []
        pairs += [(m.group(2), m.group(1)) for m in RATING_THEN_AGENCY.finditer(sent)]
        pairs += [(m.group(1), m.group(2)) for m in AGENCY_QUOTED_RATING.finditer(sent)]
        pairs += [(m.group(1), m.group(2)) for m in AGENCY_THEN_RATING.finditer(sent)]
        for agency, rating in pairs:
            key = (_agency(agency), rating)
            if key not in seen:
                seen.add(key)
                out.append({"agency": key[0], "rating": key[1], "sentence": sent[:300]})
    return out


def find_revolver(text: str) -> tuple[float, str] | None:
    m = REVOLVER_RE.search(text)
    if not m:
        return None
    return float(m.group(1)) * SCALE[m.group(2).lower()], m.group(0)


def find_maturities(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[float, str, str]] = set()
    for m in NOTES_DUE_RE.finditer(text):
        amt = float(m.group(1).replace(",", "")) * SCALE[m.group(2).lower()]
        key = (amt, m.group(3), m.group(4))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "amount": amt,
                "coupon": float(m.group(3)),
                "year": int(m.group(4)),
                "text": m.group(0),
            }
        )
    return sorted(out, key=lambda r: (r["year"], r["coupon"]))


# ---- tables -------------------------------------------------------------------------------


def debt_table(
    vals: dict[str, Val],
    revolver: tuple[float, str, str, str] | None,
    fy: int,
    section: int = 11,
) -> ExhibitResult:
    """vals: cash, long_term_debt, short_term_debt, total_debt, net_debt, total_assets,
    stockholders_equity, ebitda, operating_income, interest_expense, operating_cash_flow,
    free_cash_flow (Val). revolver: (amount, matched text, source id, period label)."""
    rows: list[list[Any]] = []
    claims: list[Claim] = []
    used: set[str] = set()

    def add(label: str, v: Val | None, metric: str | None, period: str | None = None) -> None:
        if v is None or not v.ok:
            rows.append([label, "n/a", "", ""])
            return
        rows.append([label, fmt_val(v), period or v.period, v.basis])
        used.update(s for s in v.sids if s)
        if metric:
            c = claim(
                section,
                metric,
                v.value,
                period or v.period,
                v.sids,
                unit=v.unit,
                concept=v.concept,
                note=v.note,
            )
            if c:
                claims.append(c)

    add("Cash and equivalents", vals.get("cash"), "cash")
    add("Long-term debt", vals.get("long_term_debt"), "long_term_debt")
    add("Short-term debt and current maturities", vals.get("short_term_debt"), "short_term_debt")
    add(
        "Total debt (derived: long-term debt + short-term debt)",
        vals.get("total_debt"),
        "total_debt",
    )
    add("Net debt (derived: total debt − cash)", vals.get("net_debt"), "net_debt")
    if revolver:
        amt, matched, sid, period = revolver
        rv = Val(amt, [sid], period, f"filing text: “{matched}”", "USD", "text", matched)
        add("Undrawn revolving credit facility (as stated in the filing)", rv, "undrawn_revolver")
    else:
        rows.append(
            ["Undrawn revolving credit facility", "n/a (not found in the filing text)", "", ""]
        )
    add("Total assets", vals.get("total_assets"), "total_assets")
    add("Stockholders' equity", vals.get("stockholders_equity"), "stockholders_equity")
    add(f"FY{fy} operating income", vals.get("operating_income"), "operating_income")
    add(
        f"FY{fy} depreciation and amortization",
        vals.get("depreciation_amortization"),
        "depreciation_amortization",
    )
    add(f"FY{fy} EBITDA (derived: operating income + D&A)", vals.get("ebitda"), "ebitda")
    add(f"FY{fy} interest expense", vals.get("interest_expense"), "interest_expense")
    add(GROSS_LEVERAGE_LABEL, vals.get("gross_leverage"), "gross_leverage", f"FY{fy}")
    add(NET_LEVERAGE_LABEL, vals.get("net_leverage"), "net_leverage", f"FY{fy}")
    add(COVERAGE_LABEL, vals.get("interest_coverage_ratio"), "interest_coverage_ratio", f"FY{fy}")
    add(f"FY{fy} cash from operations", vals.get("operating_cash_flow"), "operating_cash_flow")
    add(
        f"FY{fy} free cash flow (derived: operating cash flow − capex)",
        vals.get("free_cash_flow"),
        "free_cash_flow",
    )
    md = (
        "**Debt and liquidity** (USD millions unless noted; balance-sheet items at the latest XBRL "
        "instant, flows for the latest full fiscal year; leverage rows carry their definition)\n\n"
        + md_table(["Item", "Value", "Period", "Basis"], rows)
    )
    present = bool(vals.get("cash")) and bool(vals.get("total_debt"))
    return ExhibitResult(md, claims, sorted(used), present, [])


def maturity_table(items: list[dict[str, Any]], sid: str, section: int = 11) -> ExhibitResult:
    if not items:
        return ExhibitResult(
            "", [], [], False, ["no note maturities disclosed in the debt note text"]
        )
    rows = []
    claims: list[Claim] = []
    for it in items:
        rows.append(
            [
                f"{fmt_money(it['amount'])} [{sid}]",
                f"{it['coupon']:.2f}% [{sid}]",
                f"{it['year']} [{sid}]",
                it["text"],
            ]
        )
        c = claim(
            section,
            f"notes_due_{it['year']}",
            it["amount"],
            str(it["year"]),
            [sid],
            concept="text",
            note=f"{it['coupon']:.2f}% notes due {it['year']} (debt note)",
        )
        if c:
            claims.append(c)
    md = (
        "**Notes maturities disclosed in the debt note** (USD millions; as stated in the filing)\n\n"
        + md_table(["Principal", "Coupon", "Due", "Filing text"], rows, align_right_from=1)
    )
    return ExhibitResult(md, claims, [sid], True, [])


def ratings_table(found: list[dict[str, Any]]) -> ExhibitResult:
    """found: [{agency, rating, sentence, source_id, source_label, date}] — each row cites the
    source whose own text names the agency and the rating."""
    if not found:
        return ExhibitResult(
            "", [], [], False, ["no credit rating found in the filings or the fetched pages"]
        )
    rows = []
    used: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    for r in found:
        key = (r["agency"], r["rating"], r["source_id"])
        if key in seen:
            continue
        seen.add(key)
        used.add(r["source_id"])
        rows.append(
            [
                r["agency"],
                f"{r['rating']} [{r['source_id']}]",
                r.get("source_label", ""),
                r.get("date", "n.d."),
            ]
        )
    md = "**Credit ratings** (as stated in the cited source)\n\n" + md_table(
        ["Agency", "Rating", "Source", "Date"], rows, align_right_from=4
    )
    return ExhibitResult(md, [], sorted(used), True, [])


def capital_returns_table(
    fin: AnnualFinancials, sids: dict[str, str], section: int = 11, years: int = 3
) -> ExhibitResult:
    fys = fin.fiscal_years[-years:]
    if not fys:
        return ExhibitResult("", [], [], False, ["no annual XBRL facts"])
    metrics = (
        ("dividends_paid", "Dividends paid"),
        ("buybacks", "Share repurchases"),
        ("operating_cash_flow", "Cash from operations"),
        ("free_cash_flow", "Free cash flow (derived: operating cash flow − capex)"),
    )
    claims: list[Claim] = []
    used: set[str] = set()
    rows = []
    for metric, label in metrics:
        cells = []
        for fy in fys:
            fv = fin.facts.get(metric, {}).get(fy)
            if fv is not None and fv.accession in sids:
                sid = sids[fv.accession]
                used.add(sid)
                cells.append(f"{fmt_money(fv.value)} [{sid}]")
                claims.append(
                    Claim(
                        section=section,
                        metric=metric,
                        value=fv.value,
                        unit="USD",
                        period=fv.period_label,
                        source_ids=[sid],
                        concept=f"{fv.taxonomy}:{fv.concept}",
                    )
                )
                continue
            dv = fin.derived.get(metric, {}).get(fy)
            if dv is not None:
                ops = [fin.facts.get(k, {}).get(fy) for k in ("operating_cash_flow", "capex")]
                op_sids = sorted({sids[o.accession] for o in ops if o and o.accession in sids})
                used.update(op_sids)
                cells.append(f"{fmt_money(dv)}" + "".join(f" [{s}]" for s in op_sids))
                claims.append(
                    Claim(
                        section=section,
                        metric=metric,
                        value=dv,
                        unit="USD",
                        period=f"FY{fy}",
                        source_ids=op_sids,
                        concept="derived",
                        note="operating_cash_flow - capex",
                    )
                )
                continue
            cells.append("n/a")
        if any(c != "n/a" for c in cells):
            rows.append([label, *cells])
    # payout: (dividends + buybacks) / FCF, derived
    payout_cells = []
    for fy in fys:
        dv, bb, fcf = (
            fin.value("dividends_paid", fy),
            fin.value("buybacks", fy),
            fin.value("free_cash_flow", fy),
        )
        if dv is None or bb is None or not fcf:
            payout_cells.append("n/a")
            continue
        r = (dv + bb) / fcf
        op_sids = sorted(
            {
                sids[fv.accession]
                for k in ("dividends_paid", "buybacks", "operating_cash_flow", "capex")
                for fv in [fin.facts.get(k, {}).get(fy)]
                if fv is not None and fv.accession in sids
            }
        )
        payout_cells.append(f"{r * 100:.0f}%" + "".join(f" [{s}]" for s in op_sids))
        claims.append(
            Claim(
                section=section,
                metric="shareholder_return_fcf_pct",
                value=r,
                unit="pure",
                period=f"FY{fy}",
                source_ids=op_sids,
                concept="derived",
                note="(dividends_paid + buybacks) / free_cash_flow",
            )
        )
    if any(c != "n/a" for c in payout_cells):
        rows.append(["Dividends + buybacks as % of FCF (derived)", *payout_cells])
    if not rows:
        return ExhibitResult("", [], [], False, ["no capital-return facts in XBRL"])
    md = "**Capital returns** (USD millions; XBRL, same values as section 9)\n\n" + md_table(
        ["Metric", *[f"FY{fy}" for fy in fys]], rows
    )
    return ExhibitResult(md, claims, sorted(used), True, [])
