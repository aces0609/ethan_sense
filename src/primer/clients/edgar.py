"""SEC EDGAR client: tickers, submissions, companyfacts, full-text search (requirements §4 tier 1).

Fair access: one process-wide TokenBucket at 10 req/s and a mandatory descriptive
User-Agent. Responses are cached per calendar day under output/.cache/edgar.

Annual-fact selection (requirements §6 row 9 "common failure"):
- Only facts from annual forms (10-K and variants) count.
- Duration concepts: the period must be 350-380 days (a full fiscal year), which drops
  cumulative and quarterly values that share the same end date.
- The fiscal calendar comes from the facts, never from EDGAR's declared `fiscalYearEnd`
  (it is wrong for some filers: "1231" for CACI, whose year ends in June). The annual
  period ends are the ends of the 350-380 day duration facts in annual forms. Each period
  end is labelled with the filing's own `fy` (DocumentFiscalYearFocus): the filing's
  current period (its latest annual end) carries that `fy`, a comparative period carries
  `fy - round(years back)`. That follows each company's own naming (L3Harris's year ended
  2026-01-02 is FY2025, CACI's year ended 2025-06-30 is FY2025). Only a filing with no
  usable `fy` falls back to the month rule (`fiscal_year_label`).
- Instant concepts: the end date must be within 7 days of an annual period end (52/53-week
  years drift) and takes that period's label. Cover-page (dei) facts are dated 0-120 days
  after a period end and take the label of the latest period end on or before them.
- When several filings report the same period (the FY2024 10-K and the FY2025 10-K both
  carry FY2024), the most recently filed wins and any earlier differing value is
  recorded as `restated_from`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
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
FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/"


def pad_cik(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


# metrics whose Q4 value is never derived as fiscal year minus nine-month YTD (review
# 2026-10-21 §5 item 5): per-share figures are not additive and attributable net income can
# differ between the FY and YTD concepts picked
NO_YTD_DERIVATION = ("eps_diluted", "net_income")


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
    note: str | None = None  # a repair applied to the filed fact (see _repair_short_annual)

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
    fiscal_year_ends: dict[int, date] = field(default_factory=dict)  # fy label -> period end
    fiscal_year_end_date: date | None = None  # latest annual period end in the window
    labels: dict[int, str] = field(default_factory=dict)  # column key -> caption (interim view)
    interim: bool = False  # columns are 10-Q reporting periods, not fiscal years

    @property
    def fiscal_year_end_phrase(self) -> str:
        if self.interim:
            return "interim periods from 10-Q filings"
        """For captions: 'fiscal years ending 12/31' or, when the period ends drift
        (52/53-week years), '52/53-week fiscal years, latest ending 2026-01-02'."""
        if self.fiscal_year_end.startswith("52/53") and self.fiscal_year_end_date:
            return f"52/53-week fiscal years, latest ending {self.fiscal_year_end_date.isoformat()}"
        return f"fiscal years ending {self.fiscal_year_end}"

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

    def filing_index(self, cik: str, accession: str) -> list[dict[str, Any]]:
        """Files in a filing folder (index.json): name, type, size."""
        nodash = accession.replace("-", "")
        data = self._get(ARCHIVE_URL.format(cik_int=int(cik), accn_nodash=nodash) + "index.json")
        return list((data.get("directory") or {}).get("item") or [])

    def exhibit_url(self, cik: str, accession: str, pattern: str = r"ex.?99") -> str | None:
        """URL of the first exhibit whose file name matches pattern (default EX-99, the
        earnings release attached to an Item 2.02 8-K)."""
        rx = re.compile(pattern, re.I)
        items = self.filing_index(cik, accession)
        htm = [
            i
            for i in items
            if rx.search(i.get("name", "")) and i["name"].lower().endswith((".htm", ".html"))
        ]
        htm.sort(key=lambda i: i["name"])
        if not htm:
            return None
        nodash = accession.replace("-", "")
        return ARCHIVE_URL.format(cik_int=int(cik), accn_nodash=nodash) + htm[0]["name"]

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
                    items=(recent.get("items") or [None] * n)[i] or None,
                )
            )
            if limit and len(out) >= limit:
                break
        return out

    # ---- XBRL ------------------------------------------------------------------------

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self._get(COMPANYFACTS_URL.format(cik=pad_cik(cik)))

    def interim_financials(
        self, cik: str, fiscal_year_end: str | None = None, as_of: date | None = None
    ) -> AnnualFinancials:
        """The 10-Q view for a company with no 10-K yet (see primer.clients.interim)."""
        from primer.clients.interim import interim_financials

        return interim_financials(
            self.companyfacts(cik), cik, as_of or date.today(), _declared_mmdd(fiscal_year_end)
        )

    def annual_financials(
        self,
        cik: str,
        fiscal_year_end: str | None = None,
        fiscal_years: int | None = None,
        as_of: date | None = None,
    ) -> AnnualFinancials:
        """Four fiscal years of the concepts in config/concepts.yaml. The fiscal calendar is
        read from the facts (see the module docstring); `fiscal_year_end` (else EDGAR's
        declared value) only supplies the month rule for a filing that carries no `fy`."""
        cfg = concepts_config()
        facts = self.companyfacts(cik)
        n_years = fiscal_years or int(cfg.get("fiscal_years", 4))
        annual_forms = set(cfg.get("annual_forms", ["10-K"]))
        as_of = as_of or date.today()
        declared: list[str] = []

        def fallback_month() -> int:
            if not declared:
                declared.append(fiscal_year_end or self._fye_from_submissions(cik) or "1231")
            return int(declared[0][:2])

        calendar = build_fiscal_calendar(facts, annual_forms, as_of, fallback_month)
        result = AnnualFinancials(
            cik=pad_cik(cik), fiscal_year_end=_declared_mmdd(fiscal_year_end), fiscal_years=[]
        )
        all_fys: set[int] = set()

        for _stmt, metrics in cfg["statements"].items():
            for metric, spec in metrics.items():
                chosen: dict[int, FactValue] = {}
                for concept in spec.get("concepts", []):
                    taxonomy = spec.get("taxonomy", "us-gaap")
                    picks = self._select_annual(
                        facts, taxonomy, concept, spec["kind"], calendar, annual_forms, as_of
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
        _set_fiscal_year_end(result)
        for chosen in result.facts.values():
            for fv in chosen.values():
                result.accessions.setdefault(
                    fv.accession,
                    {"form": fv.form, "filed": fv.filed.isoformat(), "fy_label": fv.fiscal_year},
                )

        # derived metrics (in config order, so a derived operand defined earlier is available)
        for _stmt, metrics in cfg["statements"].items():
            for metric, spec in metrics.items():
                formula = spec.get("derive")
                if not formula:
                    continue
                optional = tuple(spec.get("optional") or ())
                for fy in result.fiscal_years:
                    if result.facts.get(metric, {}).get(fy) is not None:
                        continue
                    val = _eval_formula(
                        formula, lambda m, fy=fy: result.value(m, fy), optional=optional
                    )
                    if val is not None:
                        result.derived.setdefault(metric, {})[fy] = val

        for _stmt, metrics in cfg["statements"].items():
            for metric, spec in metrics.items():
                if spec.get("report_missing", True) is False:
                    continue
                missing = [fy for fy in result.fiscal_years if result.value(metric, fy) is None]
                if missing:
                    result.missing[metric] = missing
        return result

    def fiscal_calendar(
        self, cik: str, as_of: date | None = None, fallback_fye: str | None = None
    ) -> FiscalCalendar:
        """The annual period ends of a filer and their fiscal-year labels (companyfacts)."""
        cfg = concepts_config()
        month = int(fallback_fye[:2]) if fallback_fye and len(fallback_fye) == 4 else 12
        return build_fiscal_calendar(
            self.companyfacts(cik),
            set(cfg.get("annual_forms", ["10-K"])),
            as_of or date.today(),
            lambda: month,
        )

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
        calendar: FiscalCalendar,
        annual_forms: set[str],
        as_of: date,
    ) -> dict[int, FactValue]:
        node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
        if not node:
            return {}
        by_period: dict[date, list[tuple[dict[str, Any], str]]] = {}
        short_annual: list[tuple[dict[str, Any], str]] = []  # 80-100 day facts in annual forms
        nine_month: list[tuple[dict[str, Any], str]] = []  # 10-Q year-to-date facts
        for unit, entries in node.get("units", {}).items():
            for e in entries:
                form = e.get("form")
                if kind == "duration" and form in QUARTERLY_FORMS and e.get("start"):
                    if 260 <= _days(e) <= 290:
                        nine_month.append((e, unit))
                    continue
                if form not in annual_forms:
                    continue
                end = date.fromisoformat(e["end"])
                if end > as_of:
                    continue
                if kind == "duration":
                    if not e.get("start"):
                        continue
                    days = _days(e)
                    if 80 <= days <= 100:
                        short_annual.append((e, unit))
                        continue
                    if not (350 <= days <= 380):
                        continue
                    by_period.setdefault(end, []).append((e, unit))
                else:
                    if e.get("start"):
                        continue
                    if taxonomy == "dei":
                        # cover-page facts (shares outstanding) are dated after the period
                        # end; accept 0-120 days past an annual period end and label by it
                        period_end = calendar.cover_period(end)
                    else:
                        # balance-sheet instants must sit on an annual period end (a few
                        # days of slack: 52/53-week years end on a different day each year)
                        period_end = calendar.instant_period(end)
                    if period_end is None:
                        continue
                    by_period.setdefault(period_end, []).append((e, unit))
        if kind == "duration":
            for e, unit in _repair_short_annual(short_annual, nine_month, by_period, calendar):
                by_period.setdefault(date.fromisoformat(e["end"]), []).append((e, unit))

        out: dict[int, FactValue] = {}
        for period_end, items in by_period.items():
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
            fy = calendar.label(period_end)
            end = period_end if taxonomy == "dei" else date.fromisoformat(latest["end"])
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
                note=latest.get("_note"),
            )
            # if two period-ends map to the same FY label (52/53-week years), keep the later
            if fy not in out or out[fy].end < end:
                out[fy] = fv
        return out

    def latest_instants(
        self, cik: str, as_of: date | None = None, forms: tuple[str, ...] = ("10-Q", "10-K")
    ) -> dict[str, FactValue]:
        """Most recent balance-sheet instant per metric from any 10-Q/10-K (sections 11, 13).
        Period label is the instant date; the latest filing wins per date."""
        cfg = concepts_config()
        facts = self.companyfacts(cik)
        as_of = as_of or date.today()
        out: dict[str, FactValue] = {}
        for metric, spec in cfg["statements"]["balance_sheet"].items():
            if spec.get("kind") != "instant" or spec.get("taxonomy", "us-gaap") == "dei":
                continue
            for concept in spec.get("concepts", []):
                node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
                if not node:
                    continue
                best: tuple[date, str, dict[str, Any], str] | None = None
                for unit, entries in node.get("units", {}).items():
                    for e in entries:
                        if e.get("form") not in forms or e.get("start"):
                            continue
                        end = date.fromisoformat(e["end"])
                        if end > as_of:
                            continue
                        if best is None or (end, e.get("filed", "")) > (best[0], best[1]):
                            best = (end, e.get("filed", ""), e, unit)
                if best:
                    end, _, e, unit = best
                    out[metric] = FactValue(
                        metric=metric,
                        concept=concept,
                        taxonomy="us-gaap",
                        value=float(e["val"]),
                        unit=unit,
                        fiscal_year=end.year,
                        period_label=end.isoformat(),
                        start=None,
                        end=end,
                        accession=e["accn"],
                        form=e["form"],
                        filed=date.fromisoformat(e["filed"]),
                        frame=e.get("frame"),
                    )
                    break
        return out

    def quarterly_series(
        self,
        cik: str,
        metrics: tuple[str, ...] = ("revenue", "net_income", "eps_diluted", "operating_income"),
        n_quarters: int = 8,
        as_of: date | None = None,
        no_ytd_derivation: tuple[str, ...] = NO_YTD_DERIVATION,
    ) -> dict[str, list[FactValue]]:
        """Discrete quarterly values (80-100 day durations) from 10-Q and 10-K facts, newest
        first. Q4 is not reported as a quarter in XBRL; when the fiscal year and the
        nine-month YTD exist, Q4 = FY - YTD is derived and labelled so (section 10) for
        additive flow metrics only. Per-share and attributable figures (`no_ytd_derivation`:
        diluted EPS, net income) are never derived that way: EPS is not additive across
        periods and the review found the subtraction off the release by $0.50; section 10
        takes the Q4 figures from the earnings release or leaves the cell n/a."""
        cfg = concepts_config()
        facts = self.companyfacts(cik)
        as_of = as_of or date.today()
        specs = {m: s for stmt in cfg["statements"].values() for m, s in stmt.items()}
        quarterly_forms = ("10-Q", "10-K", "10-K/A", "10-Q/A")
        out: dict[str, list[FactValue]] = {}
        for metric in metrics:
            spec = specs.get(metric)
            if not spec or spec.get("kind") != "duration":
                continue
            taxonomy = spec.get("taxonomy", "us-gaap")
            rows: dict[date, FactValue] = {}
            ytd: dict[tuple[date, date], tuple[dict[str, Any], str]] = {}
            for concept in spec.get("concepts", []):
                node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
                if not node:
                    continue
                k_short: list[tuple[dict[str, Any], str, date, date]] = []

                for unit, entries in node.get("units", {}).items():
                    for e in entries:
                        if not e.get("start") or e.get("form") not in quarterly_forms:
                            continue
                        start, end = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
                        if end > as_of:
                            continue
                        days = (end - start).days
                        if 80 <= days <= 100:
                            if e["form"].startswith("10-K"):
                                k_short.append((e, unit, start, end))  # checked below
                            else:
                                _put_quarter(rows, metric, taxonomy, concept, e, unit, start, end)
                        elif 260 <= days <= 290 or 350 <= days <= 380:
                            k = (start, end)
                            if k not in ytd or e.get("filed", "") > ytd[k][0].get("filed", ""):
                                ytd[k] = (e, unit)
                # a 10-K fact for a quarter-length period is a fourth quarter unless it is a
                # fiscal-year total tagged with a quarter's period (_repair_short_annual): then
                # it is the fiscal year, from which Q4 is derived below
                for e, unit, start, end in k_short:
                    y_start = _nine_month_start(e, unit, ytd)
                    if y_start is not None:
                        k = (y_start, end)
                        if k not in ytd or e.get("filed", "") > ytd[k][0].get("filed", ""):
                            ytd[k] = (e, unit)
                    else:
                        _put_quarter(rows, metric, taxonomy, concept, e, unit, start, end)
                if rows:
                    break
            # derive Q4 = FY - nine-month YTD where both share a start date (additive flows only)
            for (start, end), (e, unit) in ytd.items():
                if metric in no_ytd_derivation:
                    break
                if not (350 <= (end - start).days <= 380) or end in rows:
                    continue
                nine = [
                    (k, v)
                    for k, v in ytd.items()
                    if k[0] == start and 260 <= (k[1] - k[0]).days <= 290
                ]
                if nine:
                    (_s9, e9), (e9d, _u) = nine[0]
                    rows[end] = FactValue(
                        metric=metric,
                        concept="derived",
                        taxonomy="derived",
                        value=float(e["val"]) - float(e9d["val"]),
                        unit=unit,
                        fiscal_year=int(quarter_label(end).split("-")[1]),
                        period_label=quarter_label(end),
                        start=e9 + timedelta(days=1),
                        end=end,
                        accession=e["accn"],
                        form=e["form"],
                        filed=date.fromisoformat(e["filed"]),
                        frame=None,
                        restated_from={
                            "derived": "fiscal year minus nine-month year-to-date",
                            "ytd_accession": e9d.get("accn"),
                        },
                    )
            out[metric] = [rows[k] for k in sorted(rows, reverse=True)[:n_quarters]]
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


def _default_month() -> int:
    return 12


def quarter_label(end: date) -> str:
    """Calendar-quarter label of a quarter end: the standard quarter end (Mar 31, Jun 30,
    Sep 30, Dec 31) nearest to it, e.g. 2026-06-30 -> Q2-2026. That absorbs the few days a
    52/53-week quarter drifts either way (2026-01-02 -> Q4-2025, 2025-03-28 -> Q1-2025) and
    matches `quarter_label_for_release` (the last standard quarter end before the release)
    for filers whose quarters end a month off (a 2026-01-30 quarter -> Q4-2025)."""
    ends = [
        date(y, m, d)
        for y in (end.year - 1, end.year, end.year + 1)
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
    ]
    nearest = min(ends, key=lambda q: (abs((q - end).days), q))
    return f"Q{(nearest.month - 1) // 3 + 1}-{nearest.year}"


def fiscal_year_label(end: date, fye_month: int) -> int:
    """Month rule: a fiscal year ending in January or February is labelled with the prior
    calendar year. A fallback only (see FiscalCalendar): it needs the declared year-end month,
    which EDGAR gets wrong for some filers, and it mislabels a December 52/53-week year end
    for a filer whose declared month is January."""
    if fye_month <= 2:
        return end.year - 1
    return end.year


QUARTERLY_FORMS = ("10-Q", "10-Q/A")


def _days(e: dict[str, Any]) -> int:
    return (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days


@dataclass
class FiscalCalendar:
    """Annual period ends and their fiscal-year labels, derived from the facts themselves.

    `labels` maps each annual period end (the end of a 350-380 day duration fact in an
    annual form) to a fiscal-year label taken from the filing's own `fy`. Nothing here reads
    EDGAR's declared fiscalYearEnd; `fallback_month` is only called for a filing that has no
    usable `fy`."""

    labels: dict[date, int] = field(default_factory=dict)
    fallback_month: Callable[[], int] = _default_month

    @property
    def ends(self) -> list[date]:
        return sorted(self.labels)

    def label(self, period_end: date) -> int:
        fy = self.labels.get(period_end)
        return fy if fy is not None else fiscal_year_label(period_end, self.fallback_month())

    def instant_period(self, end: date) -> date | None:
        """The annual period end within 7 days of a balance-sheet instant (the nearest)."""
        best = min(self.labels, key=lambda pe: abs((pe - end).days), default=None)
        if best is not None and abs((best - end).days) <= 7:
            return best
        return None

    def cover_period(self, end: date) -> date | None:
        """The latest annual period end on or before a cover-page date, if it is 0-120 days
        earlier (the cover date of a 10-K follows the fiscal year end)."""
        prior = [pe for pe in self.labels if pe <= end]
        if not prior:
            return None
        pe = max(prior)
        return pe if (end - pe).days <= 120 else None


def build_fiscal_calendar(
    facts: dict[str, Any],
    annual_forms: set[str] | tuple[str, ...],
    as_of: date,
    fallback_month: Callable[[], int] = _default_month,
) -> FiscalCalendar:
    """Label every annual period end in companyfacts with the fiscal year its filing calls it.

    Per annual-form accession: `fy` (DocumentFiscalYearFocus, stamped on every fact of the
    filing) is the label of the filing's current period, its latest annual end; a comparative
    period is labelled `fy - round(years back)`. A `fy` that is not the current period end's
    year or the year before (a mis-tagged filing) is ignored and that accession's ends take the
    month rule instead. When accessions disagree about a period end, the filing in which it is
    the current period wins, then the later filing."""
    forms = set(annual_forms)
    per_accn: dict[str, dict[str, Any]] = {}
    for concepts in facts.get("facts", {}).values():
        for node in concepts.values():
            for entries in node.get("units", {}).values():
                for e in entries:
                    if e.get("form") not in forms or not e.get("start") or not e.get("accn"):
                        continue
                    if not 350 <= _days(e) <= 380:
                        continue
                    end = date.fromisoformat(e["end"])
                    if end > as_of or e["end"] > e.get("filed", "9999"):
                        continue  # a period cannot end after the filing that reports it
                    info = per_accn.setdefault(
                        e["accn"], {"fy": Counter(), "filed": e.get("filed", ""), "ends": Counter()}
                    )
                    info["ends"][end] += 1
                    if isinstance(e.get("fy"), int):
                        info["fy"][e["fy"]] += 1
    candidates: dict[date, list[tuple[tuple[int, bool, str], int]]] = {}
    for info in per_accn.values():
        ends: Counter[date] = info["ends"]
        top = max(ends.values())
        current = max(end for end, n in ends.items() if n >= top / 2)
        fy = info["fy"].most_common(1)[0][0] if info["fy"] else None
        usable = fy is not None and current.year - 1 <= fy <= current.year
        for end in ends:
            if usable:
                label = fy - round((current - end).days / 365.25)
                rank = (1, end == current, info["filed"])
            else:
                label = fiscal_year_label(end, fallback_month())
                rank = (0, False, info["filed"])
            candidates.setdefault(end, []).append((rank, label))
    return FiscalCalendar(
        labels={end: max(c)[1] for end, c in candidates.items()}, fallback_month=fallback_month
    )


def _repair_short_annual(
    short_annual: list[tuple[dict[str, Any], str]],
    nine_month: list[tuple[dict[str, Any], str]],
    by_period: dict[date, list[tuple[dict[str, Any], str]]],
    calendar: FiscalCalendar,
) -> list[tuple[dict[str, Any], str]]:
    """Fiscal-year values a filer tagged with a quarter-length period.

    L3Harris's FY2025 10-K carries revenue of $21,865m for 2025-10-04..2026-01-02 (91 days)
    and no 350-380 day revenue fact, although the figure is the whole year. A 10-K fact that
    starts the day after a 10-Q nine-month year-to-date period ends, ends on an annual period
    end that the concept has no annual-length fact for, and is at least as large as that
    year-to-date value (same sign) cannot be the fourth quarter: it is taken as the fiscal-year
    value, with the year-to-date start and a `note`. Without a 10-Q year-to-date fact in
    companyfacts nothing is repaired."""
    out: list[tuple[dict[str, Any], str]] = []
    for e, unit in short_annual:
        end = date.fromisoformat(e["end"])
        if end in by_period or end not in calendar.labels:
            continue
        start = date.fromisoformat(e["start"])
        for y, y_unit in nine_month:
            if y_unit != unit or not 0 <= (start - date.fromisoformat(y["end"])).days <= 5:
                continue
            v, yv = float(e["val"]), float(y["val"])
            if v * yv > 0 and abs(v) >= abs(yv):
                fixed = dict(e)
                fixed["start"] = y["start"]
                fixed["_note"] = (
                    f"filed with a {_days(e)}-day period starting {e['start']}; taken as the "
                    f"fiscal year {y['start']} to {e['end']} because it exceeds the nine-month "
                    f"year-to-date value of {yv:,.0f} (accession {y.get('accn')})"
                )
                out.append((fixed, unit))
                break
    return out


def _put_quarter(
    rows: dict[date, FactValue],
    metric: str,
    taxonomy: str,
    concept: str,
    e: dict[str, Any],
    unit: str,
    start: date,
    end: date,
) -> None:
    """Record a discrete-quarter fact; the later filing wins for the same period end."""
    prev = rows.get(end)
    if prev is None or e.get("filed", "") > prev.filed.isoformat():
        rows[end] = FactValue(
            metric=metric,
            concept=concept,
            taxonomy=taxonomy,
            value=float(e["val"]),
            unit=unit,
            fiscal_year=int(quarter_label(end).split("-")[1]),
            period_label=quarter_label(end),
            start=start,
            end=end,
            accession=e["accn"],
            form=e["form"],
            filed=date.fromisoformat(e["filed"]),
            frame=e.get("frame"),
        )


def _nine_month_start(
    e: dict[str, Any],
    unit: str,
    ytd: dict[tuple[date, date], tuple[dict[str, Any], str]],
) -> date | None:
    """Start of the fiscal year when `e` (a quarter-length 10-K fact) is really the fiscal-year
    total: it starts within 5 days after a nine-month year-to-date period ends and is at least
    as large, same sign (see _repair_short_annual)."""
    start = date.fromisoformat(e["start"])
    for (y_start, y_end), (y, y_unit) in ytd.items():
        if y_unit != unit or not 260 <= (y_end - y_start).days <= 290:
            continue
        if not 0 <= (start - y_end).days <= 5:
            continue
        v, yv = float(e["val"]), float(y["val"])
        if v * yv > 0 and abs(v) >= abs(yv):
            return y_start
    return None


def _declared_mmdd(fye: str | None) -> str:
    return f"{fye[:2]}/{fye[2:]}" if fye and len(fye) == 4 else "n/a"


def _day_gap(a: date, b: date) -> int:
    """Days between the month/day of a and of b, around the year end."""
    try:
        same_year = date(b.year, a.month, a.day)
    except ValueError:  # Feb 29
        same_year = date(b.year, a.month, a.day - 1)
    gap = abs((same_year - b).days)
    return min(gap, 365 - gap)


def _set_fiscal_year_end(result: AnnualFinancials) -> None:
    """Fill the fiscal-year end the section text prints from the period ends of the years in
    the window: 'MM/DD' when they sit on the same day (give or take 3), else the 52/53-week
    wording naming the latest one."""
    ends: dict[int, date] = {}
    for fy in result.fiscal_years:
        for metric in GATE_YEAR_METRICS:
            fv = result.facts.get(metric, {}).get(fy)
            if fv is not None:
                ends[fy] = fv.end
                break
    if not ends:
        return
    result.fiscal_year_ends = ends
    result.fiscal_year_end_date = max(ends.values())
    result.fiscal_year_end = describe_fiscal_year_end(list(ends.values()))


def describe_fiscal_year_end(ends: list[date]) -> str:
    """'MM/DD' of the latest annual period end when the ends sit on the same day (give or
    take 3 days), else '52/53-week year ending YYYY-MM-DD'."""
    latest = max(ends)
    if max(_day_gap(e, latest) for e in ends) > 3:
        return f"52/53-week year ending {latest.isoformat()}"
    return f"{latest.month:02d}/{latest.day:02d}"


def _eval_formula(formula: str, lookup, optional: tuple[str, ...] = ()) -> float | None:
    """Evaluate 'a - b' / 'a + b' style formulas over metric names; None if any required
    operand is missing. Operands named in `optional` count as 0 when missing (net debt nets
    short-term investments when the filer reports them and cash only otherwise)."""
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
                if t in optional:
                    continue
                return None
            total = (total or 0.0) + sign * float(v)
    return total


def formula_operands(formula: str) -> list[str]:
    return re.findall(r"[a-z_]+", formula)


def derived_note(spec: dict[str, Any], lookup) -> str:
    """The formula to print for a derived metric, with any optional operand that was absent
    named ('... (short_term_investments not reported; 0 assumed)')."""
    formula = str(spec.get("derive") or "")
    absent = [op for op in spec.get("optional") or () if lookup(op) is None]
    if absent:
        formula += " (" + ", ".join(absent) + " not reported; 0 assumed)"
    return formula


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
