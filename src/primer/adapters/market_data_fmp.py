"""Financial Modeling Prep adapter stub (requirements §4 paid-upgrade table, PLAN 2.6).

Same protocol as the scraped adapter so the swap is `market_data.adapter: fmp` in
config/sources.yaml plus FMP_API_KEY in .env. Not implemented in v1: every method returns a
partial result that says so, and never raises, so a misconfigured swap degrades to
`partial` fields rather than a failed run. Endpoints to wire when the swap is made:
  /stable/quote, /stable/analyst-estimates, /stable/price-target-consensus,
  /stable/grades-historical, /stable/historical-price-eod/full, /stable/earnings.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from primer.adapters.market_data import (
    AnalystCoverage,
    Consensus,
    Datum,
    PriceHistory,
    PriceTargets,
    Quote,
    RatingChange,
    Snapshot,
)

NOTE = "FMP adapter is a stub in v1 (set market_data.adapter: scraped, or implement FMPMarketData)"


class FMPMarketData:
    name = "fmp"

    def __init__(self, *, store: Any, runlog: Any, edgar: Any, cik: str | None, as_of: date):
        self.store = store
        self.runlog = runlog
        self.edgar = edgar
        self.cik = cik
        self.as_of = as_of
        self.api_key = os.environ.get("FMP_API_KEY", "")
        self.calls = 0

    def _datum(self, label: str) -> Datum:
        return Datum(None, label=label, partial=True, note=NOTE)

    def quote(self, ticker: str) -> Quote:
        return Quote(ticker=ticker, price=self._datum("price"), partial=True, notes=[NOTE])

    def shares_outstanding(self, ticker: str) -> Datum:
        from primer.adapters.market_data_scraped import xbrl_shares

        if self.edgar and self.cik:
            d = xbrl_shares(self.edgar, self.cik, self.store, self.as_of)
            if d:
                return d
        return self._datum("shares_outstanding")

    def consensus(self, ticker: str) -> Consensus:
        return Consensus(ticker=ticker, partial=True, notes=[NOTE])

    def price_targets(self, ticker: str) -> PriceTargets:
        return PriceTargets(ticker=ticker, partial=True, notes=[NOTE])

    def rating_changes(self, ticker: str, months: int = 12) -> list[RatingChange]:
        return []

    def price_history(self, ticker: str, as_of: date | None = None) -> PriceHistory:
        return PriceHistory(ticker=ticker, partial=True, notes=[NOTE])

    def analyst_coverage(self, ticker: str) -> AnalystCoverage:
        return AnalystCoverage(ticker=ticker, partial=True, notes=[NOTE])

    def snapshot(self, ticker: str) -> Snapshot:
        return Snapshot(ticker=ticker, partial=True, notes=[NOTE])

    def summary(self) -> dict[str, Any]:
        return {"adapter": self.name, "configured": bool(self.api_key), "note": NOTE}
