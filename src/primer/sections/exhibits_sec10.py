"""Section 10 exhibits (requirements §6 row 10, PLAN 2.5 item 4).

A small row parser for the earnings-release text (html_to_text output: cells joined by
' | '): the summary results table (quarter columns), the outlook table (ranges), the backlog
sentence and the segment-sales sentences. Then the deterministic tables: quarterly results,
KPI vs consensus (actuals from XBRL or the release, consensus from the Yahoo earnings-history
rows, variance derived), segment margins from the 10-K summing tables, and management
outlook rows cited to the release with its date.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from primer.sections.common import claim, md_table
from primer.sections.exhibits import ExhibitResult
from primer.sections.exhibits_sec13 import Val, fmt_val
from primer.sections.style import fmt_money, fmt_pct
from primer.sections.text_tools import SummingTable
from primer.state import Claim

QUARTER_CELL = re.compile(
    r"^(?:Q([1-4])\s*(?:FY)?\s*(20\d{2})|(\d{1,2})/(\d{1,2})/(20\d{2})|(20\d{2})-(\d{2})-(\d{2}))$"
)
MONEY_CELL = re.compile(r"^~?\$?\s*\(?(-?[\d,]+(?:\.\d+)?)\)?$")
RANGE_CELL = re.compile(r"^~?\$?\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|—|to)\s*~?\$?\s*([\d,]+(?:\.\d+)?)$")
BACKLOG_RE = re.compile(
    r"backlog\s+(?:of|was|at|totaled|totalled|reached)?\s*(?:a\s+record\s+)?\$\s?(\d+(?:\.\d+)?)\s*(billion|million)",
    re.I,
)
OUTLOOK_YEAR_RE = re.compile(r"\b(20\d{2})\s+(?:financial\s+)?(?:outlook|guidance)\b", re.I)
DATELINE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})\b"
)
SEGMENT_SALES_RE = re.compile(
    r"\b([A-Z][A-Za-z&]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z]+|\s+[A-Z][A-Za-z]+){0,3})\s+(?:net\s+)?sales\b((?:[^.|\n]|\.(?=\d))*)",
)
LEVEL_AMOUNT_RE = re.compile(
    r"\b(to|of|were|was|totaled|totalled|reached)\s+\$\s?(\d+(?:\.\d+)?)\s*(billion|million)", re.I
)
ANY_AMOUNT_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(billion|million)", re.I)
SCALE = {"billion": 1e9, "million": 1e6}

RESULT_METRICS: dict[str, tuple[str, str]] = {
    # release row label (lower, prefix match) -> (claim metric, unit)
    "net sales": ("revenue", "USD"),
    "sales": ("revenue", "USD"),
    "revenue": ("revenue", "USD"),
    "business segment operating profit": ("segment_operating_profit", "USD"),
    "segment operating profit": ("segment_operating_profit", "USD"),
    "operating profit": ("operating_income", "USD"),
    "operating income": ("operating_income", "USD"),
    "net earnings": ("net_income", "USD"),
    "net income": ("net_income", "USD"),
    "diluted earnings per share": ("eps_diluted", "USD/shares"),
    "diluted eps": ("eps_diluted", "USD/shares"),
    "earnings per share": ("eps_diluted", "USD/shares"),
    "cash from operations": ("operating_cash_flow", "USD"),
    "net cash provided by operating activities": ("operating_cash_flow", "USD"),
    "free cash flow": ("free_cash_flow", "USD"),
    "backlog": ("backlog", "USD"),
}
GUIDANCE_METRICS: dict[str, str] = {
    "net sales": "sales",
    "sales": "sales",
    "revenue": "sales",
    "business segment operating profit": "segment_operating_profit",
    "segment operating profit": "segment_operating_profit",
    "diluted earnings per share": "eps",
    "diluted eps": "eps",
    "earnings per share": "eps",
    "cash from operations": "ocf",
    "free cash flow": "fcf",
}
RESULT_LABELS = {
    "revenue": "Net sales",
    "segment_operating_profit": "Segment operating profit",
    "operating_income": "Operating income",
    "net_income": "Net earnings",
    "eps_diluted": "Diluted EPS",
    "operating_cash_flow": "Cash from operations",
    "free_cash_flow": "Free cash flow",
    "backlog": "Backlog",
}


def qlabel(end: date) -> str:
    """Calendar-quarter label of a period end: 2026-06-30 -> Q2-2026. A 52/53-week quarter
    that ends in the first days of the next calendar quarter belongs to the quarter just
    ended (2026-01-02 -> Q4-2025). (edgar.quarter_label rounds standard quarter ends up by
    one quarter, so this module labels XBRL quarterly facts itself.)"""
    if end.day <= 7 and end.month in (1, 4, 7, 10):
        end = end.replace(day=1) - timedelta(days=1)
    return f"Q{(end.month - 1) // 3 + 1}-{end.year}"


def norm_quarter(s: str | None) -> str | None:
    """'Q2 2026' / '6/30/2026' / '2026-06-30' / 'Jun 30, 2026' -> 'Q2-2026'."""
    if not s:
        return None
    t = " ".join(s.replace("FY", "").split()).strip()
    m = QUARTER_CELL.match(t)
    if m:
        if m.group(1):
            return f"Q{m.group(1)}-{m.group(2)}"
        if m.group(3):
            return qlabel(date(int(m.group(5)), int(m.group(3)), int(m.group(4))))
        return qlabel(date(int(m.group(6)), int(m.group(7)), int(m.group(8))))
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return qlabel(datetime.strptime(t, fmt).date())
        except ValueError:
            continue
    m = re.search(
        r"\b(first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:fiscal\s+)?(20\d{2})", t, re.I
    )
    if m:
        q = ("first", "second", "third", "fourth").index(m.group(1).lower()) + 1
        return f"Q{q}-{m.group(2)}"
    return None


def _money(cell: str) -> float | None:
    m = MONEY_CELL.match(cell.strip())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return -v if cell.strip().startswith("(") or cell.strip().startswith("$(") else v


def _range(cell: str) -> tuple[float, float] | None:
    m = RANGE_CELL.match(cell.strip())
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
    except ValueError:
        return None


def _match_label(label: str, table: dict[str, Any]) -> Any:
    low = label.lower().strip(" :*")
    low = re.sub(r"\s*\(.*?\)", "", low)
    best = None
    for key, val in table.items():
        if low.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, val)
    return best[1] if best else None


def parse_release(text: str) -> dict[str, Any]:
    """Summary results columns/rows, outlook ranges, backlog, release date, segment sales."""
    out: dict[str, Any] = {
        "columns": [],
        "rows": {},
        "outlook": [],
        "outlook_columns": [],
        "outlook_year": None,
        "backlog": None,
        "release_date": None,
        "segment_sales": [],
    }
    m = DATELINE_RE.search(text)
    if m:
        try:
            out["release_date"] = datetime.strptime(" ".join(m.groups()), "%B %d %Y").date()
        except ValueError:
            pass
    m = OUTLOOK_YEAR_RE.search(text)
    if m:
        out["outlook_year"] = int(m.group(1))
    m = BACKLOG_RE.search(text)
    if m:
        out["backlog"] = (float(m.group(1)) * SCALE[m.group(2).lower()], m.group(0))

    mode: str | None = None
    last_prose = ""
    per_share_scale_note = "per share" in text.lower()
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith("|"):
            last_prose = s.lower()
            if "outlook" in last_prose or "guidance" in last_prose:
                mode = "outlook_pending"
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        cells = [c for c in cells if c not in ("", "$")]
        if len(cells) < 2:
            continue
        label, rest = cells[0], cells[1:]
        qcols = [norm_quarter(c) for c in rest]
        if all(qcols) and not out["columns"]:
            out["columns"] = qcols
            mode = "results"
            continue
        if (
            mode == "outlook_pending"
            and any(_range(c) for c in rest) is False
            and not any(_money(c) for c in rest)
        ):
            out["outlook_columns"] = rest
            mode = "outlook"
            continue
        if mode in ("outlook_pending", "outlook") and any(_range(c) for c in rest):
            key = _match_label(label, GUIDANCE_METRICS)
            if key:
                ranges = [_range(c) for c in rest]
                per_share = "per share" in label.lower() or key == "eps"
                scale = 1.0 if per_share else 1e6
                cur = ranges[0]
                prior = ranges[1] if len(ranges) > 1 else None
                out["outlook"].append(
                    {
                        "label": label,
                        "metric": key,
                        "unit": "USD/shares" if per_share else "USD",
                        "cur_low": cur[0] * scale if cur else None,
                        "cur_high": cur[1] * scale if cur else None,
                        "prior_low": prior[0] * scale if prior else None,
                        "prior_high": prior[1] * scale if prior else None,
                        "cur_label": out["outlook_columns"][0]
                        if out["outlook_columns"]
                        else "current",
                        "prior_label": out["outlook_columns"][1]
                        if len(out["outlook_columns"]) > 1
                        else "prior",
                    }
                )
            mode = "outlook"
            continue
        if mode == "results" and out["columns"]:
            spec = _match_label(label, RESULT_METRICS)
            if not spec:
                continue
            metric, unit = spec
            vals = [_money(c) for c in rest[: len(out["columns"])]]
            if not any(v is not None for v in vals):
                continue
            scale = 1.0 if unit == "USD/shares" else 1e6
            out["rows"].setdefault(metric, [None if v is None else v * scale for v in vals])
    for m in SEGMENT_SALES_RE.finditer(text):
        name, clause = m.group(1).strip(), m.group(2)
        if name.lower() in ("net", "total", "consolidated", "second quarter", "the", "reported"):
            continue
        if any(w in name.lower() for w in ("quarter", "year", "today", "reports", "increase")):
            continue
        # the level ("to $8.1 billion", "were $4.6 billion"), never the change ("increased $650 million")
        lm = LEVEL_AMOUNT_RE.search(clause)
        if lm:
            amount, unit = lm.group(2), lm.group(3)
        else:
            am = ANY_AMOUNT_RE.search(clause)
            if not am or re.search(
                r"\b(increased|decreased|up|down|rose|fell|grew)\b", clause[: am.start()], re.I
            ):
                continue
            amount, unit = am.group(1), am.group(2)
        end = (lm or am).end()  # type: ignore[union-attr]
        quoted = m.group(0)[: m.start(2) - m.start(0) + end]
        out["segment_sales"].append(
            {"segment": name, "value": float(amount) * SCALE[unit.lower()], "text": quoted[:160]}
        )
    del per_share_scale_note
    return out


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ---- tables -------------------------------------------------------------------------------


def results_table(quarters: list[dict[str, Any]], section: int = 10) -> ExhibitResult:
    """quarters: [{label, vals: {metric: Val}}] newest first. Every cell cites its source."""
    metrics = [m for m in RESULT_LABELS if any(q["vals"].get(m) for q in quarters)]
    if not metrics or not quarters:
        return ExhibitResult("", [], [], False, ["no quarterly results parsed"])
    rows = []
    claims: list[Claim] = []
    used: set[str] = set()
    for q in quarters:
        cells = []
        for m in metrics:
            v = q["vals"].get(m)
            cells.append(fmt_val(v))
            if v and v.ok:
                used.update(v.sids)
                c = claim(
                    section,
                    m,
                    v.value,
                    q["label"],
                    v.sids,
                    unit=v.unit,
                    concept=v.concept,
                    note=v.note,
                )
                if c:
                    claims.append(c)
        rows.append([q["label"], *cells])
    md = (
        "**Quarterly results** (USD millions except per share; XBRL where available, else the earnings release)\n\n"
        + md_table(["Quarter", *[RESULT_LABELS[m] for m in metrics]], rows)
    )
    return ExhibitResult(md, claims, sorted(used), True, [])


def kpi_vs_consensus_table(
    quarters: list[dict[str, Any]],
    consensus: dict[str, dict[str, Any]],
    section: int = 10,
    has_market: bool = True,
) -> ExhibitResult:
    """Rows per quarter: revenue actual, consensus revenue, EPS actual, consensus EPS (Yahoo
    earnings-history, dated), variance (derived), surprise % as the page reports it."""
    if not quarters:
        return ExhibitResult("", [], [], False, ["no quarterly actuals"])
    rows = []
    claims: list[Claim] = []
    used: set[str] = set()
    matched = 0
    for q in quarters:
        rev, eps = q["vals"].get("revenue"), q["vals"].get("eps_diluted")
        cons = consensus.get(q["label"]) or {}
        sid = cons.get("source_id")
        as_of = cons.get("as_of")
        cite = f" [{sid}]" if sid else ""
        est = cons.get("eps_estimate")
        c_eps = (
            f"{est:,.2f}{cite}"
            if est is not None and sid
            else ("n/a" if has_market else "n/a (no adapter)")
        )
        c_rev_v = cons.get("revenue_estimate")
        c_rev = (
            f"{fmt_money(c_rev_v)}{cite}"
            if c_rev_v is not None and sid
            else ("n/a" if has_market else "n/a (no adapter)")
        )
        var = "n/a"
        if eps and eps.ok and est not in (None, 0) and sid:
            v = (eps.value - est) / abs(est) * 100  # type: ignore[operator]
            var = f"{v:+.1f}%" + "".join(f" [{s}]" for s in [*eps.sids, sid])
            claims.append(
                claim(
                    section,
                    "eps_variance_pct",
                    v,
                    q["label"],
                    [*eps.sids, sid],
                    unit="pct",
                    concept="derived",
                    note="(EPS actual − consensus EPS) ÷ |consensus EPS| × 100",
                )
            )
            claims.append(
                claim(
                    section,
                    "eps_consensus",
                    est,
                    q["label"],
                    [sid],
                    unit="USD/shares",
                    concept="aggregator consensus",
                    note=f"Yahoo Finance earnings history, as of {as_of}",
                )
            )
            if c_rev_v is not None:
                claims.append(
                    claim(
                        section,
                        "revenue_consensus",
                        c_rev_v,
                        q["label"],
                        [sid],
                        unit="USD",
                        concept="aggregator consensus",
                        note=f"Yahoo Finance, as of {as_of}",
                    )
                )
            used.add(sid)
            matched += 1
        sp = cons.get("surprise_pct")
        surprise = f"{sp:+.1f}%{cite}" if sp is not None and sid else "n/a"
        if sp is not None and sid:
            claims.append(
                claim(
                    section,
                    "eps_surprise_pct",
                    sp,
                    q["label"],
                    [sid],
                    unit="pct",
                    concept="aggregator",
                    note="surprise % as reported on the page",
                )
            )
        for v in (rev, eps):
            if v and v.ok:
                used.update(v.sids)
        rows.append(
            [q["label"], fmt_val(rev), c_rev, fmt_val(eps), c_eps, var, surprise, as_of or "n/a"]
        )
    md = (
        "**KPI vs consensus** (USD millions except per share; actuals cite XBRL or the release, consensus cites the "
        "aggregator page and its date; variance derived)\n\n"
        + md_table(
            [
                "Quarter",
                "Revenue actual",
                "Consensus revenue",
                "EPS actual",
                "Consensus EPS",
                "EPS variance (derived)",
                "Surprise % (page)",
                "Consensus as of",
            ],
            rows,
        )
    )
    notes = (
        [] if (matched or not has_market) else ["no consensus rows matched the reported quarters"]
    )
    return ExhibitResult(md, [c for c in claims if c], sorted(used), matched > 0, notes)


def table_years(text: str, table: SummingTable, fallback_fy: int) -> list[str]:
    """Column labels: the nearest line above the table with enough year tokens, else FY
    labels descending from the filing's fiscal year."""
    ncol = len(table.total.values)
    lines = text.splitlines()
    for i in range(table.start_line - 1, max(-1, table.start_line - 10), -1):
        years = re.findall(r"\b(20\d{2})\b", lines[i])
        if len(years) >= ncol:
            return [f"FY{y}" for y in years[:ncol]]
    return [f"FY{fallback_fy - k}" for k in range(ncol)]


def segment_margin_table(
    sales: SummingTable | None,
    profit: SummingTable | None,
    columns: list[str],
    sid: str | None,
    section: int = 10,
) -> ExhibitResult:
    if sales is None or profit is None or not sid:
        return ExhibitResult(
            "",
            [],
            [],
            False,
            ["segment sales and operating-profit tables not both found in the 10-K"],
        )
    by_name = {r.label.lower(): r for r in profit.rows}
    rows = []
    claims: list[Claim] = []
    any_row = False
    for sr in sales.rows:
        pr = by_name.get(sr.label.lower())
        if pr is None:
            pr = next(
                (
                    r
                    for r in profit.rows
                    if r.label.lower().split()[0] == sr.label.lower().split()[0]
                ),
                None,
            )
        if pr is None:
            continue
        cells = []
        s = slug(sr.label)
        for i, col in enumerate(columns):
            if i >= len(sr.values) or i >= len(pr.values) or not sr.values[i]:
                cells.append("n/a")
                continue
            margin = pr.values[i] / sr.values[i]
            cells.append(
                f"{fmt_money(sr.values[i] * 1e6)} / {fmt_money(pr.values[i] * 1e6)} / {fmt_pct(margin)} [{sid}]"
            )
            claims.append(
                claim(
                    section,
                    f"segment_sales_{s}",
                    sr.values[i] * 1e6,
                    col,
                    [sid],
                    concept="10-K segment table",
                )
            )
            claims.append(
                claim(
                    section,
                    f"segment_operating_profit_{s}",
                    pr.values[i] * 1e6,
                    col,
                    [sid],
                    concept="10-K segment table",
                )
            )
            claims.append(
                claim(
                    section,
                    f"segment_margin_{s}",
                    margin,
                    col,
                    [sid],
                    unit="pure",
                    concept="derived",
                    note="segment operating profit ÷ segment net sales (10-K segment tables)",
                )
            )
            any_row = True
        rows.append([sr.label, *cells])
    if not any_row:
        return ExhibitResult(
            "", [], [], False, ["segment rows did not align between the sales and profit tables"]
        )
    tot_cells = []
    for i, col in enumerate(columns):
        if i < len(sales.total.values) and i < len(profit.total.values) and sales.total.values[i]:
            m = profit.total.values[i] / sales.total.values[i]
            tot_cells.append(
                f"{fmt_money(sales.total.values[i] * 1e6)} / {fmt_money(profit.total.values[i] * 1e6)} / {fmt_pct(m)} [{sid}]"
            )
            claims.append(
                claim(
                    section,
                    "segment_margin_total",
                    m,
                    col,
                    [sid],
                    unit="pure",
                    concept="derived",
                    note="total segment operating profit ÷ total net sales",
                )
            )
        else:
            tot_cells.append("n/a")
    rows.append([f"{sales.total.label} / {profit.total.label}", *tot_cells])
    md = (
        "**Segment margins** (USD millions; each cell = segment net sales / segment operating profit / margin, "
        "margin derived as operating profit ÷ net sales from the 10-K segment tables)\n\n"
        + md_table(["Segment", *columns], rows)
    )
    return ExhibitResult(md, [c for c in claims if c], [sid], True, [])


def guidance_table(
    outlook: list[dict[str, Any]],
    sid: str,
    release_date: date | None,
    period: str,
    section: int = 10,
) -> ExhibitResult:
    if not outlook or not sid:
        return ExhibitResult("", [], [], False, ["no outlook table in the latest release"])
    rows = []
    claims: list[Claim] = []
    d = release_date.isoformat() if release_date else "n.d."
    for o in outlook:
        per_share = o["unit"] == "USD/shares"

        def f(v: float | None, per_share: bool = per_share) -> str:
            if v is None:
                return "n/a"
            return f"{v:,.2f}" if per_share else fmt_money(v)

        cur = (
            f"{f(o['cur_low'])} – {f(o['cur_high'])} [{sid}]" if o["cur_low"] is not None else "n/a"
        )
        prior = (
            f"{f(o['prior_low'])} – {f(o['prior_high'])} [{sid}]"
            if o["prior_low"] is not None
            else "n/a"
        )
        rows.append([o["label"], cur, prior, f"{o['cur_label']} (release dated {d})"])
        for bound in ("low", "high"):
            v = o[f"cur_{bound}"]
            if v is not None:
                claims.append(
                    claim(
                        section,
                        f"guidance_{o['metric']}_{bound}",
                        v,
                        period,
                        [sid],
                        unit=o["unit"],
                        concept="management outlook",
                        note=f"{o['cur_label']}, release dated {d}",
                    )
                )
            pv = o[f"prior_{bound}"]
            if pv is not None:
                claims.append(
                    claim(
                        section,
                        f"guidance_{o['metric']}_{bound}_prior",
                        pv,
                        period,
                        [sid],
                        unit=o["unit"],
                        concept="management outlook (prior)",
                        note=f"prior outlook {o['prior_label']}, as restated in the release dated {d}",
                    )
                )
    md = (
        f"**Management outlook, {period}** (USD millions except per share; as stated in the release dated {d}; "
        "these are management's ranges, not consensus)\n\n"
        + md_table(["Metric", "Current outlook", "Prior outlook", "Basis"], rows)
    )
    return ExhibitResult(md, [c for c in claims if c], [sid], True, [])


def val_from_fact(fv: Any, sid: str | None, basis: str) -> Val:
    unit = "USD/shares" if fv.metric == "eps_diluted" else "USD"
    return Val(
        fv.value,
        [s for s in [sid] if s],
        qlabel(fv.end),
        basis,
        unit,
        f"{fv.taxonomy}:{fv.concept}",
    )
