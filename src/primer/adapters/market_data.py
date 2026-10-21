"""MarketDataAdapter protocol (requirements §4 "free market-data pages", PLAN 2.6).

Every value an adapter returns is a Datum: the number, its unit, the date it was observed,
the source_id of the page it came from and a `partial` flag when the page could not be read.
Adapters never average across aggregators; a caller that wants a second opinion asks for
the second page explicitly and cites it separately. Shares outstanding prefer XBRL (the
dei cover-page fact) and only fall back to a page value, labelled as such.

Implementations:
- market_data_scraped.ScrapedMarketData — v1, Firecrawl /scrape with JSON schemas over
  Yahoo Finance, StockAnalysis, MarketBeat, TipRanks, Finviz; Stooq CSV by direct GET.
- market_data_fmp.FMPMarketData — stub with the same protocol; the first paid swap.

`get_adapter(...)` reads config/sources.yaml `market_data.adapter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable


@dataclass
class Datum:
    value: float | None
    unit: str = "USD"
    as_of: date | None = None
    source_id: str | None = None
    label: str = ""
    period: str | None = None
    partial: bool = False
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None and self.source_id is not None


@dataclass
class Quote:
    ticker: str
    price: Datum
    currency: str = "USD"
    market_cap: Datum | None = None  # page value; the valuation section derives its own
    pe_ltm: Datum | None = None
    pe_forward: Datum | None = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class EarningsSurprise:
    quarter: str
    eps_estimate: float | None
    eps_actual: float | None
    surprise_pct: float | None
    source_id: str | None
    as_of: date | None


@dataclass
class Consensus:
    ticker: str
    fy_current: str | None = None  # "FY2026"
    fy_next: str | None = None
    eps_current: Datum | None = None
    eps_next: Datum | None = None
    revenue_current: Datum | None = None
    revenue_next: Datum | None = None
    n_analysts: int | None = None
    next_quarter_label: str | None = None
    eps_next_quarter: Datum | None = None
    revenue_next_quarter: Datum | None = None
    earnings_history: list[EarningsSurprise] = field(default_factory=list)
    source_id: str | None = None
    as_of: date | None = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class PriceTargets:
    ticker: str
    mean: Datum | None = None
    median: Datum | None = None
    high: Datum | None = None
    low: Datum | None = None
    n_analysts: int | None = None
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    consensus_rating: str | None = None
    source_id: str | None = None
    publisher: str | None = None
    as_of: date | None = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class RatingChange:
    date: date | None
    firm: str
    action: str  # upgrade | downgrade | initiated | reiterated | target raised | target lowered
    rating: str | None = None
    rating_prior: str | None = None
    target: float | None = None
    target_prior: float | None = None
    analyst: str | None = None
    source_id: str | None = None
    publisher: str | None = None


@dataclass
class Snapshot:
    """Finviz-style snapshot multiples (secondary; the valuation section derives its own)."""

    ticker: str
    pe: Datum | None = None
    forward_pe: Datum | None = None
    ev_ebitda: Datum | None = None
    p_fcf: Datum | None = None
    short_float_pct: Datum | None = None
    insider_own_pct: Datum | None = None
    inst_own_pct: Datum | None = None
    source_id: str | None = None
    as_of: date | None = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class PricePoint:
    date: date
    close: float


@dataclass
class PriceHistory:
    ticker: str
    points: list[PricePoint] = field(default_factory=list)
    close_as_of: Datum | None = None
    high_52w: Datum | None = None
    low_52w: Datum | None = None
    source_id: str | None = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class AnalystCoverage:
    ticker: str
    firms: list[str] = field(default_factory=list)
    source_id: str | None = None
    as_of: date | None = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class MarketDataAdapter(Protocol):
    name: str

    def quote(self, ticker: str) -> Quote: ...

    def shares_outstanding(self, ticker: str) -> Datum: ...

    def consensus(self, ticker: str) -> Consensus: ...

    def price_targets(self, ticker: str) -> PriceTargets: ...

    def rating_changes(self, ticker: str, months: int = 12) -> list[RatingChange]: ...

    def price_history(self, ticker: str, as_of: date | None = None) -> PriceHistory: ...

    def analyst_coverage(self, ticker: str) -> AnalystCoverage: ...

    def snapshot(self, ticker: str) -> Snapshot: ...

    def summary(self) -> dict[str, Any]: ...


def get_adapter(
    *,
    store: Any,
    runlog: Any,
    run_dir: Any,
    firecrawl: Any | None,
    edgar: Any | None,
    cik: str | None,
    exchange: str | None,
    as_of: date,
    offline: bool = False,
) -> MarketDataAdapter | None:
    """Build the configured adapter; None when its prerequisites are missing."""
    from primer.config import sources_config

    name = str(sources_config().get("market_data", {}).get("adapter", "scraped")).lower()
    if name == "fmp":
        from primer.adapters.market_data_fmp import FMPMarketData

        return FMPMarketData(store=store, runlog=runlog, edgar=edgar, cik=cik, as_of=as_of)
    if firecrawl is None or not firecrawl.available:
        return None
    from primer.adapters.market_data_scraped import ScrapedMarketData

    return ScrapedMarketData(
        store=store,
        runlog=runlog,
        run_dir=run_dir,
        firecrawl=firecrawl,
        edgar=edgar,
        cik=cik,
        exchange=exchange,
        as_of=as_of,
        offline=offline,
    )
