"""Section 6 required exhibit: management targets vs consensus, every target dated.

Sources, in order: the Outlook table of the latest earnings release (rows like
`| Sales | ~$79,750 - $81,750 | $77,500 - $80,000 |`; the outlook year is read from
"{year} financial outlook" in the release), the 10-K MD&A outlook sentence, and the stated
targets in the latest transcript's prepared remarks. Consensus for the current fiscal year
comes from the market-data adapter's Yahoo analysis page, cited to that page with its date;
"n/a" when no adapter is configured. The gap is derived (midpoint vs consensus).

Claims: `guidance_sales_low/high`, `guidance_eps_low/high` (and the other outlook rows under
`guidance_<metric>_low/high`) for FY{outlook year}; `consensus_revenue`, `consensus_eps`.
Section 10 parses the same release table with the same names, so the values agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from primer.sections import text_tools as tt
from primer.sections.common import claim, fmt_date, md_table
from primer.state import Claim

RANGE = re.compile(
    r"~?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(%?)\s*(?:-|–|—|to)\s*~?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(%?)"
)
SINGLE = re.compile(r"^~?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(%?)$")
OUTLOOK_FY = re.compile(r"(20\d{2})\s+(?:financial\s+)?outlook", re.I)
MDNA_RANGE = re.compile(
    r"(net sales|sales|revenues?|diluted earnings per share|earnings per share|free cash flow|"
    r"cash from operations|operating cash flow|capital expenditures)\s+(?:of\s+)?(?:approximately\s+|about\s+)?"
    r"\$(\d[\d,]*(?:\.\d+)?)\s*(billion|million)?\s*(?:to|-|–)\s*\$(\d[\d,]*(?:\.\d+)?)\s*(billion|million)?",
    re.I,
)
METRIC_NAMES = (
    ("segment operating profit", "guidance_segment_operating_profit"),
    ("operating profit", "guidance_operating_profit"),
    ("per share", "guidance_eps"),
    ("eps", "guidance_eps"),
    ("free cash flow", "guidance_fcf"),  # section 10 uses the same names
    ("cash from operations", "guidance_ocf"),
    ("operating cash", "guidance_ocf"),
    ("sales", "guidance_sales"),
    ("revenue", "guidance_sales"),
    ("capital expenditure", "guidance_capex"),
    ("capex", "guidance_capex"),
)


@dataclass
class OutlookRow:
    label: str
    metric: str
    low: float
    high: float
    unit: str  # USD | USD/shares | pct
    prior_low: float | None = None
    prior_high: float | None = None


@dataclass
class ReleaseOutlook:
    fy: int | None
    rows: list[OutlookRow] = field(default_factory=list)
    current_label: str = "current"
    prior_label: str = "prior"


def metric_name(label: str) -> str:
    low = label.lower()
    for key, name in METRIC_NAMES:
        if key in low:
            return name
    return "guidance_" + re.sub(r"[^a-z0-9]+", "_", low).strip("_")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _range(cell: str) -> tuple[float, float, bool] | None:
    c = cell.strip()
    m = RANGE.search(c)
    if m:
        return _num(m.group(1)), _num(m.group(3)), bool(m.group(2) or m.group(4))
    m = SINGLE.match(c)
    if m:
        v = _num(m.group(1))
        return v, v, bool(m.group(2))
    return None


def parse_release_outlook(text: str, default_fy: int | None = None) -> ReleaseOutlook:
    """Rows of the release's Outlook table; the filing reports USD millions except per-share."""
    m = OUTLOOK_FY.search(text)
    out = ReleaseOutlook(fy=int(m.group(1)) if m else default_fy)
    lines = text.splitlines()
    # the heading is "Outlook" or "2026 Financial Outlook" on its own line; a bullet such as
    # "Updates 2026 financial outlook" is not the table
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if re.fullmatch(
                r"\s*\|?\s*(?:20\d{2}\s+)?(?:financial\s+)?outlook\s*(?:\|\s*(?:20\d{2})?\s*)*",
                ln.strip(),
                re.I,
            )
        ),
        None,
    )
    if start is None:
        # a short heading-like line naming the outlook or guidance; a bullet or a prose line
        # that merely mentions "outlook" is not the table (a release without guidance yields
        # no rows rather than revenue lines mistaken for guidance)
        start = next(
            (
                i
                for i, ln in enumerate(lines)
                if len(ln.strip()) <= 60
                and re.search(r"\b(outlook|guidance)\b", ln, re.I)
                and not re.search(r"\b(updates?|raises?|reaffirms?|lowers?|cuts?)\b", ln, re.I)
                and "|" not in ln
            ),
            None,
        )
    if start is None:
        return out
    per_share_scale = 1.0
    for ln in lines[start : start + 40]:
        if "|" not in ln:
            if out.rows and ln.strip():
                break
            continue
        cells = [c.strip() for c in ln.split("|")]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        label = cells[0]
        if re.search(r"in (millions|billions|thousands)", label, re.I):
            if "billion" in label.lower():
                per_share_scale = 1e9
            elif "thousand" in label.lower():
                per_share_scale = 1e3
            else:
                per_share_scale = 1e6
            if len(cells) >= 3:
                out.current_label, out.prior_label = cells[1], cells[2]
            elif len(cells) == 2:
                out.current_label = cells[1]
            continue
        rng = _range(cells[1])
        if rng is None:
            continue
        low, high, is_pct = rng
        per_share = bool(re.search(r"per share|eps", label, re.I))
        unit = "pct" if is_pct else ("USD/shares" if per_share else "USD")
        scale = 1.0 if (is_pct or per_share) else (per_share_scale or 1e6)
        prior = _range(cells[2]) if len(cells) >= 3 else None
        out.rows.append(
            OutlookRow(
                label=label,
                metric=metric_name(label),
                low=low * scale,
                high=high * scale,
                unit=unit,
                prior_low=prior[0] * scale if prior else None,
                prior_high=prior[1] * scale if prior else None,
            )
        )
    return out


def mdna_targets(paragraph: str) -> list[tuple[str, float, float, str, int | None]]:
    """(label, low, high, unit, year) ranges in an MD&A outlook sentence."""
    out = []
    y = re.search(r"\b(?:expect|anticipate)\w*\s+(20\d{2})\b", paragraph, re.I)
    year = int(y.group(1)) if y else None
    for m in MDNA_RANGE.finditer(paragraph):
        label = m.group(1)
        per_share = "per share" in label.lower()
        s1 = tt.SCALE.get((m.group(3) or "").lower(), 1.0)
        s2 = tt.SCALE.get((m.group(5) or m.group(3) or "").lower(), 1.0)
        out.append(
            (
                label,
                _num(m.group(2)) * s1,
                _num(m.group(4)) * s2,
                "USD/shares" if per_share else "USD",
                year,
            )
        )
    return out


def _fmt(v: float, unit: str) -> str:
    if unit == "USD/shares":
        return f"${v:,.2f}"
    if unit == "pct":
        return f"{v:g}%"
    return f"${v / 1e6:,.0f}M"


def _fmt_range(low: float, high: float, unit: str) -> str:
    return _fmt(low, unit) if low == high else f"{_fmt(low, unit)} to {_fmt(high, unit)}"


@dataclass
class TargetsExhibit:
    markdown: str
    claims: list[Claim]
    present: bool
    dated_rows: int
    notes: list[str]
    outlook_fy: int | None


def targets_table(
    *,
    release: ReleaseOutlook | None,
    rel_sid: str | None,
    rel_date: date | str | None,
    mdna: list[tuple[str, float, float, str, int | None]],
    k_sid: str | None,
    k_date: date | str | None,
    remarks: list[str],
    tr_sid: str | None,
    tr_date: date | str | None,
    consensus: Any | None,
    section: int = 6,
) -> TargetsExhibit:
    claims: list[Claim] = []
    rows: list[list[str]] = []
    notes: list[str] = []
    cons_rev = cons_eps = None
    cons_fy = None
    if consensus is not None:
        cons_fy = getattr(consensus, "fy_current", None)
        rc = getattr(consensus, "revenue_current", None)
        ec = getattr(consensus, "eps_current", None)
        if rc is not None and rc.value is not None and rc.source_id:
            cons_rev = rc
            c = claim(
                section,
                "consensus_revenue",
                rc.value,
                cons_fy or "",
                [rc.source_id],
                unit="USD",
                concept="consensus (aggregator page)",
                note=f"Yahoo analysis page, {fmt_date(rc.as_of)}",
            )
            if c:
                claims.append(c)
        if ec is not None and ec.value is not None and ec.source_id:
            cons_eps = ec
            c = claim(
                section,
                "consensus_eps",
                ec.value,
                cons_fy or "",
                [ec.source_id],
                unit="USD/shares",
                concept="consensus (aggregator page)",
                note=f"Yahoo analysis page, {fmt_date(ec.as_of)}",
            )
            if c:
                claims.append(c)
        if cons_rev is None and cons_eps is None:
            notes.append("consensus page could not be read; consensus column is n/a")
    else:
        notes.append("no market-data adapter configured; consensus column is n/a")

    def cons_cell(metric: str, period: str) -> tuple[str, float | None]:
        d = (
            cons_rev
            if metric == "guidance_sales"
            else cons_eps
            if metric == "guidance_eps"
            else None
        )
        if d is None or d.value is None:
            return "n/a", None
        if cons_fy and period and cons_fy != period:
            return f"n/a (consensus page covers {cons_fy})", None
        return (
            f"{_fmt(d.value, d.unit)} [{d.source_id}] (Yahoo Finance, {fmt_date(d.as_of)})",
            d.value,
        )

    def gap_cell(low: float, high: float, cons: float | None, metric: str, period: str) -> str:
        if cons in (None, 0):
            return "n/a"
        mid = (low + high) / 2
        g = (mid - cons) / cons * 100
        c = claim(
            section,
            f"{metric}_vs_consensus_gap_pct",
            g,
            period,
            [],
            unit="pct",
            concept="derived",
            note=f"({metric} midpoint - consensus) / consensus x 100",
        )
        if c:
            claims.append(c)
        return f"{g:+.1f}% (derived: midpoint vs consensus)"

    dated = 0
    if release and rel_sid and release.rows:
        period = f"FY{release.fy}" if release.fy else ""
        for r in release.rows:
            tgt = _fmt_range(r.low, r.high, r.unit)
            if r.prior_low is not None and r.prior_high is not None:
                tgt += f" (prior {_fmt_range(r.prior_low, r.prior_high, r.unit)}, {release.prior_label})"
            cc, cv = cons_cell(r.metric, period)
            rows.append(
                [
                    f"{r.label} ({period} outlook, earnings release)"
                    if period
                    else f"{r.label} (earnings release)",
                    f"{tgt} [{rel_sid}]",
                    f"{fmt_date(rel_date)} [{rel_sid}]",
                    cc,
                    gap_cell(r.low, r.high, cv, r.metric, period),
                ]
            )
            dated += 1
            for suffix, v in (("low", r.low), ("high", r.high)):
                c = claim(
                    section,
                    f"{r.metric}_{suffix}",
                    v,
                    period,
                    [rel_sid],
                    unit=r.unit,
                    concept="management guidance (earnings release outlook table)",
                    note=f"set {fmt_date(rel_date)}; {r.label}",
                )
                if c:
                    claims.append(c)
    elif rel_sid:
        notes.append("latest earnings release has no parseable Outlook table")
    if mdna and k_sid:
        for label, low, high, unit, year in mdna:
            metric = metric_name(label)
            period = f"FY{year}" if year else ""
            cc, cv = cons_cell(metric, period)
            rows.append(
                [
                    f"{label.capitalize()} ({period + ' ' if period else ''}10-K MD&A outlook)",
                    f"{_fmt_range(low, high, unit)} [{k_sid}]",
                    f"{fmt_date(k_date)} [{k_sid}]",
                    cc,
                    gap_cell(
                        low, high, cv, "outlook_10k_" + metric.removeprefix("guidance_"), period
                    ),
                ]
            )
            dated += 1
            for suffix, v in (("low", low), ("high", high)):
                c = claim(
                    section,
                    f"outlook_10k_{metric.removeprefix('guidance_')}_{suffix}",
                    v,
                    period,
                    [k_sid],
                    unit=unit,
                    concept="management outlook (10-K MD&A)",
                    note=f"set {fmt_date(k_date)}; {label}",
                )
                if c:
                    claims.append(c)
    if remarks and tr_sid:
        for s in remarks[:4]:
            rows.append(
                [
                    "Prepared remarks (earnings call)",
                    f"{s[:220].rstrip('.')}. [{tr_sid}]",
                    f"{fmt_date(tr_date)} [{tr_sid}]",
                    "n/a",
                    "n/a",
                ]
            )
            dated += 1
    if not rows:
        return TargetsExhibit(
            "",
            claims,
            False,
            0,
            notes + ["no dated management target found"],
            release.fy if release else None,
        )
    if all(r[0] == "Prepared remarks (earnings call)" for r in rows):
        # every target is a sentence from the call, never a number a consensus could be set
        # against: two columns of n/a say nothing, so the table keeps the target and its date
        md = md_table(
            ["Metric", "Management target", "Date set"],
            [r[:3] for r in rows],
            align_right_from=5,
        )
        notes.append("the targets are statements from the call, so no consensus or gap is computed")
    else:
        md = md_table(
            ["Metric", "Management target", "Date set", "Consensus (current FY)", "Gap (derived)"],
            rows,
            align_right_from=5,
        )
    return TargetsExhibit(md, claims, True, dated, notes, release.fy if release else None)
