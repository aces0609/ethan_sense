"""Interim financials from 10-Q XBRL, for a company that has not filed a 10-K yet (a recent IPO).

`EdgarClient.annual_financials` reads annual forms only, so a newly listed company comes out with
no fiscal years. This module builds the same `AnnualFinancials` object from the facts its 10-Qs
carry, with the columns being reporting periods instead of fiscal years:

    3M to the latest quarter end and the quarter before it, each with its prior-year quarter,
    the year-to-date period of the latest 10-Q with its prior-year counterpart, and the balance
    sheet at the last fiscal year end.

Columns are keyed by a running integer (1 = oldest) and named by `AnnualFinancials.labels`, so
the section table builders print them without knowing they are not fiscal years. Every value is
a real filed fact with its accession; a period no 10-Q reports stays "n/a", never estimated.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from primer.clients.edgar import (
    QUARTERLY_FORMS,
    AnnualFinancials,
    FactValue,
    _days,
    _eval_formula,
    pad_cik,
)
from primer.config import concepts_config

SLACK = 4  # days of slack on a period boundary (52/53-week calendars)
INTERIM_NOTE = "interim periods from 10-Q filings"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _near(a: date, b: date) -> bool:
    return abs((a - b).days) <= SLACK


def _year_back(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year - 1, day=28)


def _entries(facts: dict[str, Any], taxonomy: str, concept: str, as_of: date):
    node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not node:
        return
    for unit, rows in node.get("units", {}).items():
        for e in rows:
            if e.get("form") not in QUARTERLY_FORMS or not e.get("end") or not e.get("filed"):
                continue
            if _d(e["end"]) > as_of or _d(e["filed"]) > as_of:
                continue
            yield e, unit


def _latest(rows: list[tuple[dict[str, Any], str]]) -> tuple[dict[str, Any], str] | None:
    return max(rows, key=lambda t: t[0].get("filed", "")) if rows else None


def _quarter_ends(facts: dict[str, Any], cfg: dict[str, Any], as_of: date) -> list[date]:
    """Period ends of the three-month facts the 10-Qs carry, newest first (revenue and net
    income decide: a quarter with neither is not a reporting period)."""
    ends: set[date] = set()
    for metric in ("revenue", "net_income"):
        spec = cfg["statements"]["income_statement"][metric]
        for concept in spec["concepts"]:
            for e, _unit in _entries(facts, "us-gaap", concept, as_of):
                if e.get("start") and 80 <= _days(e) <= 100:
                    ends.add(_d(e["end"]))
    return sorted(ends, reverse=True)


def _fact(
    metric: str,
    concept: str,
    taxonomy: str,
    pick: tuple[dict[str, Any], str],
    key: int,
    label: str,
) -> FactValue:
    e, unit = pick
    return FactValue(
        metric=metric,
        concept=concept,
        taxonomy=taxonomy,
        value=float(e["val"]),
        unit=unit,
        fiscal_year=key,
        period_label=label,
        start=_d(e["start"]) if e.get("start") else None,
        end=_d(e["end"]),
        accession=e["accn"],
        form=e["form"],
        filed=_d(e["filed"]),
        frame=e.get("frame"),
    )


def interim_financials(
    facts: dict[str, Any], cik: str, as_of: date, fiscal_year_end: str | None = None
) -> AnnualFinancials:
    """The interim view described in the module docstring; no columns when no 10-Q reports one."""
    cfg = concepts_config()
    result = AnnualFinancials(
        cik=pad_cik(cik),
        fiscal_year_end=fiscal_year_end or "",
        fiscal_years=[],
        interim=True,
    )
    qends = _quarter_ends(facts, cfg, as_of)
    if not qends:
        return result
    e1 = qends[0]
    e2 = next((q for q in qends[1:] if 60 <= (e1 - q).days <= 130), None)

    # (label, kind, end, prior_year_of) columns, oldest first
    cols: list[dict[str, Any]] = []

    def add(label: str, kind: str, end: date) -> None:
        cols.append({"label": label, "kind": kind, "end": end})

    def q_label(end: date) -> str:
        return f"3M to {end.strftime('%b %Y')}"

    # the year-to-date start comes from the latest 10-Q's longest revenue period
    ytd_start: date | None = None
    ytd_days = 0
    rev_spec = cfg["statements"]["income_statement"]["revenue"]
    for concept in rev_spec["concepts"]:
        for e, _unit in _entries(facts, "us-gaap", concept, as_of):
            if e.get("start") and _near(_d(e["end"]), e1) and _days(e) > 100:
                if _days(e) > ytd_days:
                    ytd_days, ytd_start = _days(e), _d(e["start"])
    months = round(ytd_days / 30.4) if ytd_days else 0
    ytd_label = f"{months}M to {e1.strftime('%b %Y')}" if months else ""

    if e2:
        add(q_label(_year_back(e2)), "flow", _year_back(e2))
    add(q_label(_year_back(e1)), "flow", _year_back(e1))
    if ytd_start:
        add(f"{months}M to {_year_back(e1).strftime('%b %Y')}", "ytd", _year_back(e1))
        add(
            f"Balance sheet {(ytd_start - timedelta(days=1)).strftime('%b %Y')}",
            "instant",
            ytd_start - timedelta(days=1),
        )
    if e2:
        add(q_label(e2), "flow", e2)
    add(q_label(e1), "flow", e1)
    if ytd_start:
        add(ytd_label, "ytd", e1)

    for i, col in enumerate(cols, start=1):
        col["key"] = i
    result.fiscal_years = [c["key"] for c in cols]
    result.labels = {c["key"]: c["label"] for c in cols}
    # a quarter column also carries the balance sheet at its end
    for c in cols:
        c["instant_end"] = c["end"] if c["kind"] in ("flow", "instant") else None

    for _stmt, metrics in cfg["statements"].items():
        for metric, spec in metrics.items():
            taxonomy = spec.get("taxonomy", "us-gaap")
            if taxonomy == "dei":
                continue
            chosen: dict[int, FactValue] = {}
            for concept in spec.get("concepts", []):
                rows = list(_entries(facts, taxonomy, concept, as_of))
                for c in cols:
                    if c["key"] in chosen:
                        continue
                    pick = None
                    if spec["kind"] == "duration" and c["kind"] in ("flow", "ytd"):
                        want = (80, 100) if c["kind"] == "flow" else (101, 290)
                        match = [
                            (e, u)
                            for e, u in rows
                            if e.get("start")
                            and _near(_d(e["end"]), c["end"])
                            and want[0] <= _days(e) <= want[1]
                        ]
                        if c["kind"] == "ytd":
                            # the year-to-date period, not an earlier quarter's, is the longest
                            longest = max((_days(e) for e, _ in match), default=0)
                            match = [(e, u) for e, u in match if _days(e) == longest]
                        pick = _latest(match)
                    elif spec["kind"] == "instant" and c["instant_end"] is not None:
                        match = [
                            (e, u)
                            for e, u in rows
                            if not e.get("start") and _near(_d(e["end"]), c["instant_end"])
                        ]
                        pick = _latest(match)
                    if pick:
                        chosen[c["key"]] = _fact(
                            metric, concept, taxonomy, pick, c["key"], c["label"]
                        )
            if chosen:
                result.facts[metric] = chosen

    for chosen in result.facts.values():
        for fv in chosen.values():
            result.accessions.setdefault(
                fv.accession,
                {"form": fv.form, "filed": fv.filed.isoformat(), "fy_label": fv.period_label},
            )

    # derived rows (gross profit, free cash flow, total debt, net debt) per column
    for _stmt, metrics in cfg["statements"].items():
        for metric, spec in metrics.items():
            formula = spec.get("derive")
            if not formula:
                continue
            optional = tuple(spec.get("optional") or ())
            for key in result.fiscal_years:
                if result.facts.get(metric, {}).get(key) is not None:
                    continue
                val = _eval_formula(
                    formula, lambda m, key=key: result.value(m, key), optional=optional
                )
                if val is not None:
                    result.derived.setdefault(metric, {})[key] = val
    # a column with no fact at all is dropped
    live = [k for k in result.fiscal_years if any(k in v for v in result.facts.values())]
    result.fiscal_years = live
    result.labels = {k: result.labels[k] for k in live}
    result.fiscal_year_end_date = e1
    return result
