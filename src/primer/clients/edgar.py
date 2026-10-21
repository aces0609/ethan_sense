"""SEC EDGAR client: tickers, submissions, companyfacts, full-text search (requirements §4 tier 1).

Fair access: one process-wide TokenBucket at 10 req/s and a mandatory descriptive
User-Agent. Responses are cached per calendar day under output/.cache/edgar.

Annual-fact selection (requirements §6 row 9 "common failure"):
- Only facts from annual forms (10-K and variants) count.
- Duration concepts: the period must be 350-380 days (a full fiscal year), which drops
  cumulative and quarterly values that share the same end date.
- Instant concepts: the end date must land on the fiscal-year-end month/day.
- Fiscal-year label = year of the period end, except that a fiscal year ending in
  January or February is labelled with the prior calendar year.
- When several filings report the same period (the FY2024 10-K and the FY2025 10-K both
  carry FY2024), the most recently filed wins and any earlier differing value is
  recorded as `restated_from`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from primer import __version__
from primer.clients.cache import DiskCache
from primer.clients.ratelimit import TokenBucket
from primer.config import concepts_config, edgar_user_agent, output_root
from primer.runlog import RunLog
from primer.state import FilingRef

SEC_BUCKET = TokenBucket(rate_per_sec=8, capacity=8)  # under the 10 req/s cap

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EXCHANGE_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANYCONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json"
)
FULLTEXT_URL = "https://efts.sec.gov/LatestSearch/"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/"


def pad_cik(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


@dataclass
class FactValue:
    metric: str
    concept: str
    taxonomy: str
    value: float
    unit: str
    fiscal_year: int
    period_label: str
    start: date | None
    end: date
    accession: str
    form: str
    filed: date
    frame: str | None = None
    restated_from: dict[str, Any] | None = None

    @property
    def filing_index_url(self) -> str:
        return ""  # filled by client via accession_index_url


@dataclass
class AnnualFinancials:
    cik: str
    fiscal_year_end: str
    fiscal_years: list[int]
    facts: dict[str, dict[int, FactValue]] = field(default_factory=dict)  # metric -> fy -> fact
    derived: dict[str, dict[int, float]] = field(default_factory=dict)
    missing: dict[str, list[int]] = field(default_factory=dict)
    accessions: dict[str, dict[str, Any]] = field(default_factory=dict)  # accn -> {form, filed, fy}

    def value(self, metric: str, fy: int) -> float | None:
        fv = self.facts.get(metric, {}).get(fy)
        if fv is not None:
            return fv.value
        return self.derived.get(metric, {}).get(fy)


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None = None,
        runlog: RunLog | None = None,
        client: httpx.Client | None = None,
        offline: bool = False,
        cache_root: Path | None = None,
    ) -> None:
        self.user_agent = user_agent or edgar_user_agent()
        self.runlog = runlog
        self.offline = offline
        self.http = client or httpx.Client(
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "X-Client": f"ethan_sense/{__version__}",
            },
            timeout=60.0,
            follow_redirects=True,
        )
        self.cache = DiskCache(
            cache_root or output_root(), "edgar", ttl_seconds=24 * 3600, offline=offline
        )

    # ---- low level -------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any] | None = None, as_json: bool = True) -> Any:
        key = url + ("?" + httpx.QueryParams(params or {}).__str__() if params else "")
        key += "|" + date.today().isoformat()

        def fetch() -> Any:
            SEC_BUCKET.acquire()
            if self.runlog:
                self.runlog.count("edgar_requests")
            r = self.http.get(url, params=params)
            if r.status_code == 403:
                raise RuntimeError(
                    f"EDGAR 403 for {url}: check EDGAR_USER_AGENT and rate (fair-access policy)"
                )
            r.raise_for_status()
            return r.json() if as_json else r.text

        return self.cache.get_or_fetch(key, fetch)

    def get_text(self, url: str) -> str:
        return self._get(url, as_json=False)

    # ---- entity resolution -----------------------------------------------------------

    def resolve_ticker(self, ticker: str) -> dict[str, Any] | None:
        tk = ticker.upper().split(".")[0].replace("-", ".")
        data = self._get(EXCHANGE_TICKERS_URL)
        fields = data.get("fields", [])
        for row in data.get("data", []):
            rec = dict(zip(fields, row, strict=False))
            if str(rec.get("ticker", "")).upper() == tk:
                return {
                    "cik": pad_cik(rec["cik"]),
                    "ticker": rec["ticker"],
                    "title": rec["name"],
                    "exchange": rec.get("exchange"),
                }
        return None

    def search_company_name(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self._get(EXCHANGE_TICKERS_URL)
        fields = data.get("fields", [])
        needle = re.sub(r"[^a-z0-9 ]", "", name.lower())
        hits = []
        for row in data.get("data", []):
            rec = dict(zip(fields, row, strict=False))
            title = re.sub(r"[^a-z0-9 ]", "", str(rec.get("name", "")).lower())
            if needle and needle in title:
                hits.append(
                    {
                        "cik": pad_cik(rec["cik"]),
                        "ticker": rec["ticker"],
                        "title": rec["name"],
                        "exchange": rec.get("exchange"),
                        "score": len(needle) / max(1, len(title)),
                    }
                )
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    # ---- submissions -----------------------------------------------------------------

    def submissions(self, cik: str) -> dict[str, Any]:
        return self._get(SUBMISSIONS_URL.format(cik=pad_cik(cik)))

    @staticmethod
    def accession_index_url(cik: str, accession: str) -> str:
        nodash = accession.replace("-", "")
        return ARCHIVE_URL.format(cik_int=int(cik), accn_nodash=nodash) + f"{accession}-index.htm"

    def filings(
        self,
        cik: str,
        forms: list[str] | None = None,
        limit: int | None = None,
        since: date | None = None,
    ) -> list[FilingRef]:
        subs = self.submissions(cik)
        recent = subs.get("filings", {}).get("recent", {})
        n = len(recent.get("accessionNumber", []))
        out: list[FilingRef] = []
        for i in range(n):
            form = recent["form"][i]
            if forms and form not in forms:
                continue
            fdate = date.fromisoformat(recent["filingDate"][i])
            if since and fdate < since:
                continue
            accn = recent["accessionNumber"][i]
            nodash = accn.replace("-", "")
            primary = recent.get("primaryDocument", [None] * n)[i] or None
            base = ARCHIVE_URL.format(cik_int=int(cik), accn_nodash=nodash)
            rd = recent.get("reportDate", [""] * n)[i]
            out.append(
                FilingRef(
                    form=form,
                    filing_date=fdate,
                    report_date=date.fromisoformat(rd) if rd else None,
                    accession=accn,
                    primary_document=primary,
                    url=base + primary if primary else base,
                    index_url=base + f"{accn}-index.htm",
                    description=recent.get("primaryDocDescription", [None] * n)[i] or None,
                )
            )
            if limit and len(out) >= limit:
                break
        return out

    # ---- XBRL ------------------------------------------------------------------------

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self._get(COMPANYFACTS_URL.format(cik=pad_cik(cik)))

    def annual_financials(
        self,
        cik: str,
        fiscal_year_end: str | None = None,
        fiscal_years: int | None = None,
        as_of: date | None = None,
    ) -> AnnualFinancials:
        cfg = concepts_config()
        facts = self.companyfacts(cik)
        fye = fiscal_year_end or self._fye_from_submissions(cik) or "1231"
        n_years = fiscal_years or int(cfg.get("fiscal_years", 4))
        annual_forms = set(cfg.get("annual_forms", ["10-K"]))
        as_of = as_of or date.today()

        result = AnnualFinancials(cik=pad_cik(cik), fiscal_year_end=fye, fiscal_years=[])
        all_fys: set[int] = set()

        for _stmt, metrics in cfg["statements"].items():
            for metric, spec in metrics.items():
                chosen: dict[int, FactValue] = {}
                for concept in spec.get("concepts", []):
                    taxonomy = spec.get("taxonomy", "us-gaap")
                    picks = self._select_annual(
                        facts, taxonomy, concept, spec["kind"], fye, annual_forms, as_of
                    )
                    # fallback concepts only fill years the earlier concepts did not cover
                    for fy, fv in picks.items():
                        if fy not in chosen:
                            fv.metric = metric
                            chosen[fy] = fv
                if chosen:
                    result.facts[metric] = chosen
                    if metric in GATE_YEAR_METRICS:
                        all_fys.update(chosen.keys())

        # the fiscal-year window is set by the core P&L/cash-flow metrics, then every
        # metric is pruned to that window so restatement notes and accessions stay in scope
        fys = sorted(all_fys, reverse=True)[:n_years]
        result.fiscal_years = sorted(fys)
        keep = set(fys)
        for metric in list(result.facts):
            result.facts[metric] = {fy: fv for fy, fv in result.facts[metric].items() if fy in keep}
            if not result.facts[metric]:
                del result.facts[metric]
        for chosen in result.facts.values():
            for fv in chosen.values():
                result.accessions.setdefault(
                    fv.accession,
                    {"form": fv.form, "filed": fv.filed.isoformat(), "fy_label": fv.fiscal_year},
                )

        # derived metrics
        for _stmt, metrics in cfg["statements"].items():
            for metric, spec in metrics.items():
                formula = spec.get("derive")
                if not formula:
                    continue
                for fy in result.fiscal_years:
                    if result.facts.get(metric, {}).get(fy) is not None:
                        continue
                    val = _eval_formula(formula, lambda m, fy=fy: result.value(m, fy))
                    if val is not None:
                        result.derived.setdefault(metric, {})[fy] = val

        for _stmt, metrics in cfg["statements"].items():
            for metric in metrics:
                missing = [fy for fy in result.fiscal_years if result.value(metric, fy) is None]
                if missing:
                    result.missing[metric] = missing
        return result

    def _fye_from_submissions(self, cik: str) -> str | None:
        try:
            return self.submissions(cik).get("fiscalYearEnd") or None
        except Exception:
            return None

    @staticmethod
    def _select_annual(
        facts: dict[str, Any],
        taxonomy: str,
        concept: str,
        kind: str,
        fye: str,
        annual_forms: set[str],
        as_of: date,
    ) -> dict[int, FactValue]:
        node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
        if not node:
            return {}
        fye_month, fye_day = int(fye[:2]), int(fye[2:])
        by_period: dict[date, list[tuple[dict[str, Any], str]]] = {}
        for unit, entries in node.get("units", {}).items():
            for e in entries:
                if e.get("form") not in annual_forms:
                    continue
                end = date.fromisoformat(e["end"])
                if end > as_of:
                    continue
                if kind == "duration":
                    if not e.get("start"):
                        continue
                    start = date.fromisoformat(e["start"])
                    if not (350 <= (end - start).days <= 380):
                        continue
                else:
                    if e.get("start"):
                        continue
                    if taxonomy == "dei":
                        # cover-page facts (shares outstanding) are dated after the FYE;
                        # accept up to 120 days past a fiscal-year end and label by that FYE
                        fye_date = _fye_on_or_before(end, fye_month, fye_day)
                        if not (0 <= (end - fye_date).days <= 120):
                            continue
                        period_end = fye_date
                    else:
                        # balance-sheet instants must sit on the fiscal-year end (a few days of slack)
                        if abs(end.month - fye_month) > 0 or abs(end.day - fye_day) > 7:
                            continue
                        period_end = end
                    by_period.setdefault(period_end, []).append((e, unit))
                    continue
                by_period.setdefault(end, []).append((e, unit))

        out: dict[int, FactValue] = {}
        for end, items in by_period.items():
            items.sort(key=lambda t: t[0].get("filed", ""), reverse=True)
            latest, unit = items[0]
            restated = None
            for older, _ in items[1:]:
                if older.get("val") != latest.get("val"):
                    restated = {
                        "value": older.get("val"),
                        "accession": older.get("accn"),
                        "filed": older.get("filed"),
                    }
                    break
            fy = fiscal_year_label(end, fye_month)
            fv = FactValue(
                metric="",
                concept=concept,
                taxonomy=taxonomy,
                value=float(latest["val"]),
                unit=unit,
                fiscal_year=fy,
                period_label=f"FY{fy}",
                start=date.fromisoformat(latest["start"]) if latest.get("start") else None,
                end=end,
                accession=latest["accn"],
                form=latest["form"],
                filed=date.fromisoformat(latest["filed"]),
                frame=latest.get("frame"),
                restated_from=restated,
            )
            # if two period-ends map to the same FY label (52/53-week years), keep the later
            if fy not in out or out[fy].end < end:
                out[fy] = fv
        return out

    # ---- full-text search ------------------------------------------------------------

    def fulltext_search(
        self,
        query: str,
        forms: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        cik: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query}
        if forms:
            params["forms"] = ",".join(forms)
        if start or end:
            params["dateRange"] = "custom"
            params["startdt"] = (start or date(2001, 1, 1)).isoformat()
            params["enddt"] = (end or date.today()).isoformat()
        if cik:
            params["ciks"] = pad_cik(cik)
        if self.runlog:
            self.runlog.query("edgar_fulltext", query, forms=forms)
        return self._get(FULLTEXT_URL, params=params)


GATE_YEAR_METRICS = ("revenue", "net_income", "operating_cash_flow", "capex", "operating_income")


def fiscal_year_label(end: date, fye_month: int) -> int:
    if fye_month <= 2:
        return end.year - 1
    return end.year


def _fye_on_or_before(d: date, fye_month: int, fye_day: int) -> date:
    """The most recent fiscal-year-end date on or before d."""
    day = min(fye_day, 28) if fye_month == 2 else fye_day
    candidate = date(d.year, fye_month, day)
    if candidate > d:
        candidate = date(d.year - 1, fye_month, day)
    return candidate


def _eval_formula(formula: str, lookup) -> float | None:
    """Evaluate 'a - b' / 'a + b' style formulas over metric names; None if any operand missing."""
    tokens = re.findall(r"[a-z_]+|[+\-]", formula)
    total: float | None = None
    sign = 1.0
    for t in tokens:
        if t == "+":
            sign = 1.0
        elif t == "-":
            sign = -1.0
        else:
            v = lookup(t)
            if v is None:
                return None
            total = (total or 0.0) + sign * float(v)
    return total


def filing_primary_url(cik: str, accession: str, primary_document: str) -> str:
    nodash = accession.replace("-", "")
    return ARCHIVE_URL.format(cik_int=int(cik), accn_nodash=nodash) + primary_document


def trailing_months(as_of: date, months: int) -> date:
    return as_of - timedelta(days=int(months * 30.44))


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None
