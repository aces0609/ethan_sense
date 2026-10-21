"""PLAN 1.3: EDGAR client on recorded fixtures. LMT FY2022-FY2024 values equal the 10-K figures."""

from __future__ import annotations

from datetime import date

import pytest

from primer.clients.edgar import EdgarClient, fiscal_year_label
from tests.conftest import LMT_CIK

GOLDEN_MILLIONS = {
    "revenue": {2022: 65984, 2023: 67571, 2024: 71043},
    "net_income": {2022: 5732, 2023: 6920, 2024: 5336},
    "operating_cash_flow": {2022: 7802, 2023: 7920, 2024: 6972},
    "capex": {2022: 1670, 2023: 1691, 2024: 1685},
}


@pytest.fixture
def edgar(edgar_mock, output_dir):
    return EdgarClient()


def test_user_agent_required(monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "no-email-here")
    with pytest.raises(RuntimeError):
        EdgarClient()


def test_resolve_ticker(edgar):
    m = edgar.resolve_ticker("LMT")
    assert m["cik"] == LMT_CIK
    assert m["exchange"] == "NYSE"
    assert edgar.resolve_ticker("ZZZZ") is None


def test_search_company_name(edgar):
    hits = edgar.search_company_name("Lockheed")
    assert hits and hits[0]["ticker"] == "LMT"


def test_filings_and_index_url(edgar):
    tenk = edgar.filings(LMT_CIK, forms=["10-K"], limit=1)
    assert len(tenk) == 1 and tenk[0].form == "10-K"
    assert tenk[0].url.startswith("https://www.sec.gov/Archives/edgar/data/936468/")
    assert tenk[0].index_url.endswith("-index.htm")


def test_annual_financials_match_golden(edgar):
    fin = edgar.annual_financials(LMT_CIK, fiscal_year_end="1231", as_of=date(2026, 10, 21))
    assert fin.fiscal_years[-4:] == sorted(fin.fiscal_years)[-4:]
    for metric, years in GOLDEN_MILLIONS.items():
        for fy, millions in years.items():
            assert fin.value(metric, fy) == millions * 1e6, (metric, fy, fin.value(metric, fy))
    # every selected fact carries the accession it came from and all years are in the window
    for metric, by_fy in fin.facts.items():
        for fy, fv in by_fy.items():
            assert fy in fin.fiscal_years, (metric, fy)
            assert fv.accession in fin.accessions
    # derived FCF and the cover-page share count from dei
    assert fin.value("free_cash_flow", 2024) == (6972 - 1685) * 1e6
    assert fin.value("shares_outstanding", 2024) is not None


def test_latest_filing_wins_and_restatement_recorded(edgar):
    fin = edgar.annual_financials(LMT_CIK, fiscal_year_end="1231", as_of=date(2026, 10, 21))
    fv = fin.facts["revenue"][2024]
    # FY2024 revenue is reported in both the FY2024 and FY2025 10-Ks; the later filing wins
    assert fv.filed >= date(2026, 1, 1)
    restated = [f for m in fin.facts.values() for f in m.values() if f.restated_from]
    for f in restated:
        assert f.restated_from["accession"] != f.accession


def test_fiscal_year_label():
    assert fiscal_year_label(date(2024, 12, 31), 12) == 2024
    assert fiscal_year_label(date(2024, 9, 30), 9) == 2024
    assert fiscal_year_label(date(2025, 1, 31), 1) == 2024


def test_requests_are_counted_and_cached(edgar_mock, output_dir):
    from primer.runlog import RunLog

    log = RunLog(output_dir / "r")
    e = EdgarClient(runlog=log)
    e.submissions(LMT_CIK)
    e.submissions(LMT_CIK)
    assert log.counters["edgar_requests"] == 1
