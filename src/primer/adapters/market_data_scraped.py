"""v1 market-data adapter: free aggregator pages scraped with Firecrawl JSON schemas.

Pages (config/sources.yaml `market_data`): Yahoo Finance analysis tab, StockAnalysis
forecast and statistics, MarketBeat forecast (rating changes), TipRanks forecast, Finviz
quote, Stooq daily CSV (direct GET, no Firecrawl). Each page is scraped once per run and
shared by sections 6, 10, 12 and 13 through a process memo persisted under
run_dir/market_data/, so a resumed run re-reads the JSON instead of spending credits.

Rules (requirements §4): every value is cited to its page and dated; nothing is averaged
across aggregators; a page that cannot be read marks its fields `partial` and the caller
decides what to do, never a silent substitution; shares outstanding come from XBRL (dei
cover-page fact) first and from a page only when XBRL has none, labelled as such.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from primer.adapters.market_data import (
    AnalystCoverage,
    Consensus,
    Datum,
    EarningsSurprise,
    PriceHistory,
    PricePoint,
    PriceTargets,
    Quote,
    RatingChange,
    Snapshot,
)
from primer.clients.cache import DiskCache
from primer.clients.firecrawl import FirecrawlClient, FirecrawlError
from primer.config import market_data_budget, output_root, sources_config

# ---- extraction schemas (Firecrawl /scrape json format) ------------------------------------


class _Surprise(BaseModel):
    quarter: str | None = Field(None, description="Quarter label, e.g. 'Q2 2026' or '6/30/2026'")
    eps_estimate: float | None = None
    eps_actual: float | None = None
    surprise_pct: float | None = Field(None, description="Surprise in percent, e.g. 4.2")


class YahooAnalysis(BaseModel):
    current_price: float | None = None
    current_year_label: str | None = Field(None, description="e.g. 'Current Year (2026)'")
    next_year_label: str | None = None
    eps_estimate_current_year: float | None = None
    eps_estimate_next_year: float | None = None
    revenue_estimate_current_year: str | None = Field(None, description="as shown, e.g. '79.1B'")
    revenue_estimate_next_year: str | None = None
    number_of_analysts: int | None = None
    next_quarter_label: str | None = None
    eps_estimate_next_quarter: float | None = None
    revenue_estimate_next_quarter: str | None = None
    price_target_low: float | None = None
    price_target_average: float | None = None
    price_target_high: float | None = None
    earnings_history: list[_Surprise] = Field(default_factory=list)


class StockAnalysisForecast(BaseModel):
    price_target_average: float | None = None
    price_target_high: float | None = None
    price_target_low: float | None = None
    number_of_analysts: int | None = None
    consensus_rating: str | None = None
    strong_buy: int | None = None
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    strong_sell: int | None = None
    revenue_this_year: str | None = Field(None, description="as shown, e.g. '79.14B'")
    revenue_next_year: str | None = None
    eps_this_year: float | None = None
    eps_next_year: float | None = None
    this_year_label: str | None = Field(None, description="fiscal year label, e.g. '2026'")
    next_year_label: str | None = None


class StockAnalysisStatistics(BaseModel):
    current_price: float | None = None
    market_cap: str | None = Field(None, description="as shown, e.g. '108.5B'")
    enterprise_value: str | None = None
    shares_outstanding: str | None = Field(None, description="as shown, e.g. '232.1M'")
    pe_ratio: float | None = None
    forward_pe: float | None = None
    ev_ebitda: float | None = None
    price_52w_high: float | None = None
    price_52w_low: float | None = None
    dividend_yield_pct: float | None = None


class _Rating(BaseModel):
    date: str | None = None
    brokerage: str | None = None
    analyst: str | None = None
    action: str | None = Field(
        None, description="upgrade, downgrade, initiated, reiterated, boost target, lower target"
    )
    rating: str | None = None
    prior_rating: str | None = None
    price_target: float | None = None
    prior_price_target: float | None = None


class MarketBeatRatings(BaseModel):
    consensus_rating: str | None = None
    consensus_price_target: float | None = None
    high_price_target: float | None = None
    low_price_target: float | None = None
    number_of_ratings: int | None = None
    ratings: list[_Rating] = Field(default_factory=list)


class _TipAnalyst(BaseModel):
    name: str | None = None
    firm: str | None = None
    rating: str | None = None
    price_target: float | None = None
    date: str | None = None


class TipRanksForecast(BaseModel):
    average_price_target: float | None = None
    high_price_target: float | None = None
    low_price_target: float | None = None
    number_of_analysts: int | None = None
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    consensus: str | None = None
    analysts: list[_TipAnalyst] = Field(default_factory=list)


class FinvizSnapshot(BaseModel):
    price: float | None = None
    pe: float | None = None
    forward_pe: float | None = None
    ev_ebitda: float | None = None
    p_fcf: float | None = None
    short_float_pct: float | None = None
    insider_own_pct: float | None = None
    inst_own_pct: float | None = None
    target_price: float | None = None
    market_cap: str | None = None


PAGES: dict[str, tuple[type[BaseModel], str]] = {
    "yahoo": (
        YahooAnalysis,
        "Extract the analysis-tab figures exactly as displayed: current price, EPS and revenue estimates for the current and next fiscal year with their labels, analyst count, price-target low/average/high, and the earnings-history table (quarter, EPS estimate, EPS actual, surprise %). Use null for anything not shown.",
    ),
    "stockanalysis_forecast": (
        StockAnalysisForecast,
        "Extract the analyst forecast figures as displayed: price-target average/high/low, number of analysts, consensus rating and rating counts, revenue and EPS forecasts for this year and next with their fiscal-year labels. Null when absent.",
    ),
    "stockanalysis_statistics": (
        StockAnalysisStatistics,
        "Extract the statistics as displayed (keep abbreviations like 108.5B): price, market cap, enterprise value, shares outstanding, PE, forward PE, EV/EBITDA, 52-week high and low, dividend yield. Null when absent.",
    ),
    "marketbeat": (
        MarketBeatRatings,
        "Extract the consensus rating and price targets and every row of the analyst ratings history table (date, brokerage, analyst, action, rating, prior rating, price target, prior price target). Null when absent.",
    ),
    "tipranks": (
        TipRanksForecast,
        "Extract the consensus price target (average/high/low), analyst count, buy/hold/sell counts, consensus label, and any named analysts with firm, rating, price target and date that are visible without login. Null when absent.",
    ),
    "finviz": (
        FinvizSnapshot,
        "Extract the snapshot table values: Price, P/E, Forward P/E, EV/EBITDA, P/FCF, Short Float %, Insider Own %, Inst Own %, Target Price, Market Cap (as displayed). Null when absent.",
    ),
}

_MEMO: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
SUFFIX = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}


def parse_abbrev(s: str | float | None) -> float | None:
    """'108.5B' -> 1.085e11; '$1,234.5' -> 1234.5; None/'-' -> None."""
    if s is None:
        return None
    if isinstance(s, int | float):
        return float(s)
    t = s.strip().replace("$", "").replace(",", "").replace("USD", "").strip()
    if not t or t in ("-", "—", "N/A", "n/a"):
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([TBMKtbmk])?", t)
    if not m:
        return None
    return float(m.group(1)) * SUFFIX.get(m.group(2).upper() if m.group(2) else "", 1.0)


def _fy_label(label: str | None, fallback_year: int) -> str:
    if label:
        m = re.search(r"(20\d{2})", label)
        if m:
            return f"FY{m.group(1)}"
    return f"FY{fallback_year}"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%m/%d/%y"):
        try:
            from datetime import datetime

            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def xbrl_shares(edgar: Any, cik: str, store: Any, as_of: date) -> Datum | None:
    """Latest dei:EntityCommonStockSharesOutstanding on or before as_of, registered as a
    tier-1 source (one record per filing accession)."""
    try:
        facts = edgar.companyfacts(cik)
    except Exception:
        return None
    node = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not node:
        return None
    best = None
    for unit, entries in node.get("units", {}).items():
        for e in entries:
            end = date.fromisoformat(e["end"])
            if end > as_of:
                continue
            if best is None or (end, e.get("filed", "")) > (best[0], best[1]):
                best = (end, e.get("filed", ""), e, unit)
    if not best:
        return None
    end, _, e, unit = best
    url = edgar.accession_index_url(cik, e["accn"])
    rec = store.register(
        f"# EDGAR XBRL dei cover-page facts — {e.get('form')} filed {e.get('filed')} (accession {e['accn']})\n\n"
        f"Source API: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json\n\n```json\n"
        + json.dumps({"concept": "dei:EntityCommonStockSharesOutstanding", **e}, indent=1)
        + "\n```\n",
        type="api:edgar_xbrl",
        publisher="SEC EDGAR",
        title=f"XBRL dei cover page, {e.get('form')} filed {e.get('filed')} (accession {e['accn']})",
        url=url,
        published_date=date.fromisoformat(e["filed"]) if e.get("filed") else None,
        tier=1,
        gov_id=e["accn"],
        meta={
            "cik": cik,
            "form": e.get("form"),
            "concept": "dei:EntityCommonStockSharesOutstanding",
        },
        dedupe_key=f"edgar_dei|{cik}|{e['accn']}",
    )
    return Datum(
        float(e["val"]),
        unit="shares",
        as_of=end,
        source_id=rec.source_id,
        label="shares outstanding (XBRL dei cover page)",
        period=end.isoformat(),
    )


class ScrapedMarketData:
    name = "scraped"

    def __init__(
        self,
        *,
        store: Any,
        runlog: Any,
        run_dir: str | Path,
        firecrawl: FirecrawlClient,
        edgar: Any | None,
        cik: str | None,
        exchange: str | None,
        as_of: date,
        offline: bool = False,
        http: httpx.Client | None = None,
    ) -> None:
        self.store = store
        self.runlog = runlog
        self.run_dir = Path(run_dir)
        self.dir = self.run_dir / "market_data"
        self.edgar = edgar
        self.cik = cik
        self.exchange = (exchange or "NYSE").upper()
        self.as_of = as_of
        self.offline = offline
        self.http = http or httpx.Client(timeout=60.0, follow_redirects=True)
        self.urls = dict(sources_config().get("market_data", {}))
        # own client so the market-data credit cap is enforced independently of sections
        self.fc = FirecrawlClient(
            store,
            runlog=runlog,
            api_key=firecrawl.api_key,
            client=firecrawl.http,
            credit_cap=int(market_data_budget().get("credits", 40)),
            offline=offline,
        )
        self.failures: dict[str, str] = {}

    # ---- page memo ------------------------------------------------------------------

    def _url(self, page: str, ticker: str) -> str:
        tpl = self.urls.get(page, "")
        return tpl.format(tk=ticker.upper(), exchange=self.exchange)

    def _page(self, page: str, ticker: str) -> dict[str, Any]:
        """{json, source_id, as_of, error} for one page, scraped once per run."""
        key = f"{self.run_dir.resolve()}|{page}|{ticker.upper()}"
        with _LOCK:
            if key in _MEMO:
                return _MEMO[key]
        path = self.dir / f"{page}_{ticker.upper()}.json"
        result: dict[str, Any] | None = None
        if path.exists():
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                if result.get("source_id") and not self.store.has(result["source_id"]):
                    result = None
            except json.JSONDecodeError:
                result = None
        if result is None:
            result = self._scrape(page, ticker)
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        with _LOCK:
            _MEMO.setdefault(key, result)
            return _MEMO[key]

    def _scrape(self, page: str, ticker: str) -> dict[str, Any]:
        model, prompt = PAGES[page]
        url = self._url(page, ticker)
        try:
            res = self.fc.scrape(
                url,
                type="market_data",
                json_schema=model.model_json_schema(),
                json_prompt=prompt,
                section_hint="market_data",
                only_main=False,
            )
        except FirecrawlError as e:
            self.failures[page] = str(e)
            if self.runlog:
                self.runlog.error(f"market_data {page}: {e}")
            return {
                "json": None,
                "source_id": None,
                "as_of": self.as_of.isoformat(),
                "error": str(e),
            }
        data = res.get("json")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict) or not any(v not in (None, [], "") for v in data.values()):
            self.failures[page] = "page returned no structured fields"
            return {
                "json": None,
                "source_id": res.get("source_id"),
                "as_of": self.as_of.isoformat(),
                "error": "no structured fields",
            }
        try:
            parsed = model.model_validate(data).model_dump()
        except Exception as e:  # schema drift on the aggregator page
            self.failures[page] = f"schema: {e}"
            parsed = None
        return {
            "json": parsed,
            "source_id": res.get("source_id"),
            "as_of": self.as_of.isoformat(),
            "error": None if parsed else "schema mismatch",
        }

    def _datum(self, page: dict[str, Any], value: Any, label: str, unit: str = "USD", period=None):
        v = parse_abbrev(value) if isinstance(value, str) else value
        return Datum(
            None if v is None else float(v),
            unit=unit,
            as_of=date.fromisoformat(page["as_of"]),
            source_id=page.get("source_id"),
            label=label,
            period=period,
            partial=v is None,
            note=page.get("error"),
        )

    # ---- protocol ---------------------------------------------------------------------

    def quote(self, ticker: str) -> Quote:
        """Price from the Stooq close on the as-of date (direct CSV); StockAnalysis statistics
        for the page-reported market cap and multiples. The two are cited separately."""
        hist = self.price_history(ticker)
        stats = self._page("stockanalysis_statistics", ticker)
        j = stats.get("json") or {}
        price = hist.close_as_of or Datum(
            None, label="price", partial=True, note="no price history"
        )
        if price.value is None and j.get("current_price"):
            price = self._datum(stats, j.get("current_price"), "price (StockAnalysis page)")
        q = Quote(
            ticker=ticker,
            price=price,
            market_cap=self._datum(stats, j.get("market_cap"), "market cap (page)"),
            pe_ltm=self._datum(stats, j.get("pe_ratio"), "P/E LTM (page)", unit="x"),
            pe_forward=self._datum(stats, j.get("forward_pe"), "forward P/E (page)", unit="x"),
            partial=price.value is None,
        )
        if stats.get("error"):
            q.notes.append(f"stockanalysis statistics: {stats['error']}")
        if hist.notes:
            q.notes.extend(hist.notes)
        return q

    def shares_outstanding(self, ticker: str) -> Datum:
        if self.edgar and self.cik:
            d = xbrl_shares(self.edgar, self.cik, self.store, self.as_of)
            if d:
                return d
        stats = self._page("stockanalysis_statistics", ticker)
        j = stats.get("json") or {}
        d = self._datum(stats, j.get("shares_outstanding"), "shares outstanding (page)", "shares")
        d.note = (d.note or "") + " XBRL dei share count unavailable; page value used"
        return d

    def consensus(self, ticker: str) -> Consensus:
        y = self._page("yahoo", ticker)
        j = y.get("json") or {}
        fy = self.as_of.year
        c = Consensus(
            ticker=ticker,
            source_id=y.get("source_id"),
            as_of=date.fromisoformat(y["as_of"]),
            partial=bool(y.get("error")),
        )
        if y.get("error"):
            c.notes.append(f"yahoo analysis: {y['error']}")
        c.fy_current = _fy_label(j.get("current_year_label"), fy)
        c.fy_next = _fy_label(j.get("next_year_label"), fy + 1)
        c.eps_current = self._datum(
            y, j.get("eps_estimate_current_year"), "consensus EPS", "USD/shares", c.fy_current
        )
        c.eps_next = self._datum(
            y, j.get("eps_estimate_next_year"), "consensus EPS", "USD/shares", c.fy_next
        )
        c.revenue_current = self._datum(
            y, j.get("revenue_estimate_current_year"), "consensus revenue", "USD", c.fy_current
        )
        c.revenue_next = self._datum(
            y, j.get("revenue_estimate_next_year"), "consensus revenue", "USD", c.fy_next
        )
        c.n_analysts = j.get("number_of_analysts")
        c.next_quarter_label = j.get("next_quarter_label")
        c.eps_next_quarter = self._datum(
            y,
            j.get("eps_estimate_next_quarter"),
            "consensus EPS next quarter",
            "USD/shares",
            c.next_quarter_label,
        )
        c.revenue_next_quarter = self._datum(
            y,
            j.get("revenue_estimate_next_quarter"),
            "consensus revenue next quarter",
            "USD",
            c.next_quarter_label,
        )
        for h in j.get("earnings_history") or []:
            c.earnings_history.append(
                EarningsSurprise(
                    quarter=str(h.get("quarter") or ""),
                    eps_estimate=h.get("eps_estimate"),
                    eps_actual=h.get("eps_actual"),
                    surprise_pct=h.get("surprise_pct"),
                    source_id=y.get("source_id"),
                    as_of=c.as_of,
                )
            )
        return c

    def price_targets(self, ticker: str) -> PriceTargets:
        """StockAnalysis forecast page is the primary target table; Yahoo and MarketBeat are
        available through consensus()/rating_changes() and cited separately."""
        p = self._page("stockanalysis_forecast", ticker)
        j = p.get("json") or {}
        t = PriceTargets(
            ticker=ticker,
            source_id=p.get("source_id"),
            publisher="StockAnalysis",
            as_of=date.fromisoformat(p["as_of"]),
            partial=bool(p.get("error")),
        )
        if p.get("error"):
            t.notes.append(f"stockanalysis forecast: {p['error']}")
        t.mean = self._datum(p, j.get("price_target_average"), "mean price target")
        t.high = self._datum(p, j.get("price_target_high"), "high price target")
        t.low = self._datum(p, j.get("price_target_low"), "low price target")
        t.n_analysts = j.get("number_of_analysts")
        t.buy = (
            (j.get("strong_buy") or 0) + (j.get("buy") or 0)
            if (j.get("buy") is not None or j.get("strong_buy") is not None)
            else None
        )
        t.hold = j.get("hold")
        t.sell = (
            (j.get("sell") or 0) + (j.get("strong_sell") or 0)
            if (j.get("sell") is not None or j.get("strong_sell") is not None)
            else None
        )
        t.consensus_rating = j.get("consensus_rating")
        return t

    def rating_changes(self, ticker: str, months: int = 12) -> list[RatingChange]:
        p = self._page("marketbeat", ticker)
        j = p.get("json") or {}
        cutoff = self.as_of - timedelta(days=int(months * 30.44))
        out: list[RatingChange] = []
        for r in j.get("ratings") or []:
            d = _parse_date(r.get("date"))
            if d and d < cutoff:
                continue
            if d and d > self.as_of:
                continue
            firm = (r.get("brokerage") or "").strip()
            if not firm:
                continue
            out.append(
                RatingChange(
                    date=d,
                    firm=firm,
                    action=(r.get("action") or "").strip().lower() or "rating",
                    rating=r.get("rating"),
                    rating_prior=r.get("prior_rating"),
                    target=r.get("price_target"),
                    target_prior=r.get("prior_price_target"),
                    analyst=r.get("analyst"),
                    source_id=p.get("source_id"),
                    publisher="MarketBeat",
                )
            )
        out.sort(key=lambda r: r.date or date.min, reverse=True)
        return out

    def tipranks(self, ticker: str) -> PriceTargets:
        p = self._page("tipranks", ticker)
        j = p.get("json") or {}
        t = PriceTargets(
            ticker=ticker,
            source_id=p.get("source_id"),
            publisher="TipRanks",
            as_of=date.fromisoformat(p["as_of"]),
            partial=bool(p.get("error")),
        )
        if p.get("error"):
            t.notes.append(f"tipranks: {p['error']}")
        t.mean = self._datum(p, j.get("average_price_target"), "mean price target")
        t.high = self._datum(p, j.get("high_price_target"), "high price target")
        t.low = self._datum(p, j.get("low_price_target"), "low price target")
        t.n_analysts = j.get("number_of_analysts")
        t.buy, t.hold, t.sell = j.get("buy"), j.get("hold"), j.get("sell")
        t.consensus_rating = j.get("consensus")
        return t

    def price_history(self, ticker: str, as_of: date | None = None) -> PriceHistory:
        """Stooq daily CSV by direct GET (cached one day); close on/before as_of and 52-week range."""
        as_of = as_of or self.as_of
        url = self._url("stooq_csv", ticker)
        h = PriceHistory(ticker=ticker)
        cache = DiskCache(output_root(), "stooq", ttl_seconds=24 * 3600, offline=self.offline)

        def fetch() -> str:
            if self.runlog:
                self.runlog.count("stooq_requests")
            r = self.http.get(url, headers={"User-Agent": "Mozilla/5.0 (ethan_sense primer)"})
            r.raise_for_status()
            return r.text

        try:
            text = cache.get_or_fetch(f"{url}|{date.today().isoformat()}", fetch)
        except Exception as e:
            h.partial = True
            h.notes.append(f"stooq: {e}")
            self.failures["stooq"] = str(e)
            return h
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines or not lines[0].lower().startswith("date"):
            h.partial = True
            h.notes.append("stooq: unexpected CSV")
            self.failures["stooq"] = "unexpected CSV"
            return h
        rec = self.store.register(
            f"# Stooq daily prices {ticker.upper()}.US\n\nSource: {url}\n\n```csv\n"
            + "\n".join(lines[-300:])
            + "\n```\n",
            type="market_data",
            publisher="Stooq",
            title=f"Daily price history {ticker.upper()} (Stooq CSV)",
            url=url,
            published_date=as_of,
            tier=2,
            meta={"rows": len(lines) - 1},
            dedupe_key=f"stooq|{ticker.upper()}|{as_of.isoformat()}",
        )
        h.source_id = rec.source_id
        for ln in lines[1:]:
            cells = ln.split(",")
            if len(cells) < 5:
                continue
            try:
                d = date.fromisoformat(cells[0])
                close = float(cells[4])
            except ValueError:
                continue
            if d <= as_of:
                h.points.append(PricePoint(d, close))
        if not h.points:
            h.partial = True
            h.notes.append("stooq: no rows on or before as_of")
            return h
        last = h.points[-1]
        h.close_as_of = Datum(
            last.close,
            "USD",
            last.date,
            rec.source_id,
            "closing price (Stooq)",
            last.date.isoformat(),
        )
        year = [p for p in h.points if p.date >= as_of - timedelta(days=365)]
        if year:
            hi = max(year, key=lambda p: p.close)
            lo = min(year, key=lambda p: p.close)
            h.high_52w = Datum(
                hi.close, "USD", hi.date, rec.source_id, "52-week high close (Stooq)"
            )
            h.low_52w = Datum(lo.close, "USD", lo.date, rec.source_id, "52-week low close (Stooq)")
        return h

    def analyst_coverage(self, ticker: str) -> AnalystCoverage:
        """Firms named on the MarketBeat and TipRanks pages (the IR coverage page is scraped by
        section 12 when it exists); bounds who may be attributed."""
        firms: list[str] = []
        sid = None
        for r in self.rating_changes(ticker, months=24):
            if r.firm not in firms:
                firms.append(r.firm)
            sid = sid or r.source_id
        tp = self._page("tipranks", ticker)
        for a in (tp.get("json") or {}).get("analysts") or []:
            f = (a.get("firm") or "").strip()
            if f and f not in firms:
                firms.append(f)
        cov = AnalystCoverage(
            ticker=ticker, firms=firms, source_id=sid or tp.get("source_id"), as_of=self.as_of
        )
        cov.partial = not firms
        return cov

    def snapshot(self, ticker: str) -> Snapshot:
        p = self._page("finviz", ticker)
        j = p.get("json") or {}
        s = Snapshot(
            ticker=ticker,
            source_id=p.get("source_id"),
            as_of=date.fromisoformat(p["as_of"]),
            partial=bool(p.get("error")),
        )
        if p.get("error"):
            s.notes.append(f"finviz: {p['error']}")
        s.pe = self._datum(p, j.get("pe"), "P/E (Finviz)", "x")
        s.forward_pe = self._datum(p, j.get("forward_pe"), "forward P/E (Finviz)", "x")
        s.ev_ebitda = self._datum(p, j.get("ev_ebitda"), "EV/EBITDA (Finviz)", "x")
        s.p_fcf = self._datum(p, j.get("p_fcf"), "P/FCF (Finviz)", "x")
        s.short_float_pct = self._datum(
            p, j.get("short_float_pct"), "short float % (Finviz)", "pct"
        )
        s.insider_own_pct = self._datum(
            p, j.get("insider_own_pct"), "insider ownership % (Finviz)", "pct"
        )
        s.inst_own_pct = self._datum(
            p, j.get("inst_own_pct"), "institutional ownership % (Finviz)", "pct"
        )
        return s

    def summary(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "credits_used": self.fc.credits_used,
            "failures": dict(self.failures),
            "pages": sorted(p.name for p in self.dir.glob("*.json")) if self.dir.exists() else [],
        }


def datum_dict(d: Datum | None) -> dict[str, Any] | None:
    return asdict(d) if d else None
