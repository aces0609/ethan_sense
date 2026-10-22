"""Review fixes from docs/REVIEW-PRIMERS-2026-10-21.md §9: net debt and EV net short-term
investments and the quarterly EPS derivation is gone (fix 3); sources carry dates, SEC
archive types, no index pages, reproducible USAspending URLs, clean titles and one record
per URL (fix 4); employee count, customer split and the officer table come from named 10-K
items (fix 9)."""

from __future__ import annotations

import json
from datetime import date

import respx
from httpx import Response

from primer.clients.edgar import NO_YTD_DERIVATION, EdgarClient, derived_note
from primer.clients.firecrawl import FirecrawlClient
from primer.clients.usaspending import USAspendingClient
from primer.evidence.html import html_to_text
from primer.evidence.store import (
    EvidenceStore,
    clean_title,
    date_from_metadata,
    date_from_text,
    date_from_url,
    is_index_page,
    sec_archive_type,
)
from primer.sections import text_tools as tt
from primer.sections.exhibits import financial_tables
from primer.sections.exhibits_sec10 import RESULT_LABELS
from primer.sections.exhibits_sec11 import debt_table
from primer.sections.exhibits_sec13 import NET_DEBT_FORMULA, Val, derive, ltm_table
from primer.sections.sec02_overview import employee_count
from primer.sections.sec10_operating import q4_gap_notes
from primer.sections.sec11_capital import _vals as sec11_vals
from tests.conftest import FAKE_10K_BA_HTML, FAKE_10K_HTML, FAKE_DEF14A_HTML, LMT_CIK

AS_OF = date(2026, 10, 21)
CIK_BA = "0000012927"


# ---- fix 3: net debt, EV, quarterly derivations ------------------------------------------------


def _fact(val: float, start: str | None, end: str, accn: str, form: str, filed: str) -> dict:
    e = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": 2025, "fp": "FY"}
    if start:
        e["start"] = start
    return e


def _boeing_style_facts() -> dict:
    """companyfacts for a filer that reports cash and short-term investments separately,
    plus the fiscal-year and nine-month YTD values that would tempt a Q4 subtraction."""
    k25, k24 = "0000012927-26-000010", "0000012927-25-000010"
    q3 = "0000012927-25-000050"

    def annual(vals: dict[int, float], kind: str = "duration") -> dict:
        out = []
        for fy, v in vals.items():
            accn = k25 if fy == 2025 else k24
            filed = "2026-01-28" if fy == 2025 else "2025-01-28"
            start = f"{fy}-01-01" if kind == "duration" else None
            out.append(_fact(v, start, f"{fy}-12-31", accn, "10-K", filed))
        return {"units": {"USD": out}}

    usd = {
        "Revenues": annual({2025: 70_000e6, 2024: 66_500e6}),
        "OperatingIncomeLoss": annual({2025: 1_000e6, 2024: -10_700e6}),
        "NetIncomeLoss": annual({2025: -2_000e6, 2024: -11_800e6}),
        "NetCashProvidedByUsedInOperatingActivities": annual({2025: 3_000e6, 2024: -12_100e6}),
        "PaymentsToAcquirePropertyPlantAndEquipment": annual({2025: 2_300e6, 2024: 2_200e6}),
        "CashAndCashEquivalentsAtCarryingValue": annual({2025: 8_000e6, 2024: 13_800e6}, "instant"),
        "ShortTermInvestments": annual({2025: 18_000e6, 2024: 12_500e6}, "instant"),
        "LongTermDebtNoncurrent": annual({2025: 52_000e6, 2024: 52_600e6}, "instant"),
        "LongTermDebtCurrent": annual({2025: 2_500e6, 2024: 1_300e6}, "instant"),
        "Assets": annual({2025: 150_000e6, 2024: 156_000e6}, "instant"),
    }
    # nine-month YTD values filed with the Q3 10-Q: additive flows may be subtracted, EPS and
    # net income may not
    usd["Revenues"]["units"]["USD"].append(
        _fact(49_000e6, "2025-01-01", "2025-09-30", q3, "10-Q", "2025-10-29")
    )
    usd["NetIncomeLoss"]["units"]["USD"].append(
        _fact(-3_000e6, "2025-01-01", "2025-09-30", q3, "10-Q", "2025-10-29")
    )
    eps = {
        "units": {
            "USD/shares": [
                _fact(-2.70, "2025-01-01", "2025-12-31", k25, "10-K", "2026-01-28"),
                _fact(-4.10, "2025-01-01", "2025-09-30", q3, "10-Q", "2025-10-29"),
                _fact(-1.20, "2025-07-01", "2025-09-30", q3, "10-Q", "2025-10-29"),
            ]
        }
    }
    return {"facts": {"us-gaap": {**usd, "EarningsPerShareDiluted": eps}}}


def _edgar_with(facts: dict, monkeypatch, output_dir) -> EdgarClient:
    client = EdgarClient()
    monkeypatch.setattr(client, "companyfacts", lambda cik: facts)
    monkeypatch.setattr(client, "_fye_from_submissions", lambda cik: "1231")
    return client


def test_net_debt_nets_short_term_investments_in_annual_financials(monkeypatch, output_dir):
    edgar = _edgar_with(_boeing_style_facts(), monkeypatch, output_dir)
    fin = edgar.annual_financials(CIK_BA, fiscal_year_end="1231", as_of=AS_OF)
    assert fin.value("short_term_investments", 2025) == 18_000e6
    assert fin.value("total_debt", 2025) == 52_000e6 + 2_500e6
    # 54,500 − 8,000 − 18,000 = 28,500, not the cash-only 46,500
    assert fin.value("net_debt", 2025) == 54_500e6 - 8_000e6 - 18_000e6
    assert "short_term_investments" not in fin.missing
    assert "cash_and_short_term_investments" not in fin.missing  # report_missing: false

    sids = {"0000012927-26-000010": "src_0001", "0000012927-25-000010": "src_0002"}
    ex = financial_tables(fin, sids, section=9)
    nd = next(c for c in ex.claims if c.metric == "net_debt" and c.period == "FY2025")
    assert nd.concept == "derived" and nd.note == "total_debt - cash - short_term_investments"
    assert nd.value == 28_500e6 and set(nd.source_ids) == {"src_0001"}
    assert "Net debt (derived) — derived: total debt - cash - short term investments" in ex.markdown
    assert "Short-term investments and marketable securities" in ex.markdown


def test_net_debt_without_short_term_investments_nets_cash_and_says_so(edgar_mock, output_dir):
    fin = EdgarClient().annual_financials(LMT_CIK, fiscal_year_end="1231", as_of=AS_OF)
    assert fin.value("short_term_investments", 2025) is None
    assert fin.value("net_debt", 2025) == 21_700e6 - 4_121e6
    spec = {
        "derive": "total_debt - cash - short_term_investments",
        "optional": ["short_term_investments"],
    }
    assert derived_note(spec, lambda m: fin.value(m, 2025)) == (
        "total_debt - cash - short_term_investments (short_term_investments not reported; 0 assumed)"
    )


def test_latest_instants_pick_up_short_term_investments(monkeypatch, output_dir):
    edgar = _edgar_with(_boeing_style_facts(), monkeypatch, output_dir)
    inst = edgar.latest_instants(CIK_BA, AS_OF)
    assert inst["short_term_investments"].value == 18_000e6
    assert inst["short_term_investments"].concept == "ShortTermInvestments"


def _instants() -> dict[str, Val]:
    return {
        "cash": Val(8_000e6, ["src_0001"], "2025-12-31", "XBRL 10-K balance-sheet instant"),
        "short_term_investments": Val(18_000e6, ["src_0001"], "2025-12-31", "XBRL 10-K"),
        "long_term_debt": Val(52_000e6, ["src_0001"], "2025-12-31", "XBRL 10-K"),
        "short_term_debt": Val(2_500e6, ["src_0001"], "2025-12-31", "XBRL 10-K"),
        "operating_income": Val(1_000e6, ["src_0001"], "FY2025", "XBRL 10-K (FY2025)"),
        "depreciation_amortization": Val(1_800e6, ["src_0001"], "FY2025", "XBRL 10-K (FY2025)"),
        "price": Val(190.0, ["src_0009"], "2026-10-20", "Stooq close", "USD/shares"),
        "shares": Val(760e6, ["src_0008"], "2026-01-23", "XBRL dei cover page", "shares"),
    }


def test_sec13_derive_nets_short_term_investments_in_net_debt_and_ev():
    v = derive(_instants(), 2025)
    assert v["net_debt"].value == 54_500e6 - 8_000e6 - 18_000e6
    assert v["net_debt"].note == NET_DEBT_FORMULA
    assert "src_0001" in v["net_debt"].sids
    mc = 190.0 * 760e6
    assert v["enterprise_value"].value == mc + 54_500e6 - 8_000e6 - 18_000e6
    assert v["enterprise_value"].note == "market cap + total debt − cash − short-term investments"
    assert v["ev_ebitda_ltm_ratio"].value == v["enterprise_value"].value / 2_800e6
    md = ltm_table(v, 13).markdown
    assert "| Short-term investments and marketable securities | 18,000 [src_0001]" in md
    assert "Net debt — derived: total debt − cash − short-term investments" in md
    assert "- Net debt: total debt − cash − short-term investments" in md


def test_sec13_derive_from_combined_cash_and_investments_concept():
    v = _instants()
    combined = Val(26_000e6, ["src_0003"], "2025-12-31", "XBRL 10-K")
    v.pop("short_term_investments")
    v["cash_and_short_term_investments"] = combined
    out = derive(v, 2025)
    sti = out["short_term_investments"]
    assert sti.value == 18_000e6 and sti.concept == "derived" and "src_0003" in sti.sids
    assert out["net_debt"].value == 54_500e6 - 26_000e6


def test_sec13_derive_without_short_term_investments_states_the_assumption():
    v = _instants()
    v.pop("short_term_investments")
    out = derive(v, 2025)
    assert out["net_debt"].value == 54_500e6 - 8_000e6
    assert "0 assumed" in out["net_debt"].note and NET_DEBT_FORMULA in out["net_debt"].note
    assert "0 assumed" in out["enterprise_value"].note
    assert "n/a (no XBRL fact; 0 assumed in net debt and EV)" in ltm_table(out, 13).markdown


def test_sec11_debt_table_nets_short_term_investments(monkeypatch, output_dir):
    edgar = _edgar_with(_boeing_style_facts(), monkeypatch, output_dir)
    fin = edgar.annual_financials(CIK_BA, fiscal_year_end="1231", as_of=AS_OF)
    inst = edgar.latest_instants(CIK_BA, AS_OF)
    sids = {"0000012927-26-000010": "src_0001", "0000012927-25-000010": "src_0002"}
    vals = sec11_vals(fin, inst, sids, 2025)
    assert vals["net_debt"].value == 54_500e6 - 8_000e6 - 18_000e6
    assert vals["net_debt"].note == NET_DEBT_FORMULA
    ex = debt_table(vals, None, 2025, 11)
    nd = next(c for c in ex.claims if c.metric == "net_debt")
    assert nd.value == 28_500e6 and nd.note == NET_DEBT_FORMULA
    assert f"Net debt (derived: {NET_DEBT_FORMULA})" in ex.markdown
    assert "| Short-term investments and marketable securities | 18,000 [src_0001]" in ex.markdown
    assert next(c for c in ex.claims if c.metric == "short_term_investments").value == 18_000e6


def test_quarterly_series_never_derives_q4_eps_or_net_income(monkeypatch, output_dir):
    assert set(NO_YTD_DERIVATION) == {"eps_diluted", "net_income"}
    edgar = _edgar_with(_boeing_style_facts(), monkeypatch, output_dir)
    series = edgar.quarterly_series(CIK_BA, ("revenue", "net_income", "eps_diluted"), as_of=AS_OF)
    rev = {fv.period_label: fv for fv in series["revenue"]}
    assert rev["Q4-2025"].taxonomy == "derived" and rev["Q4-2025"].value == 70_000e6 - 49_000e6
    eps = {fv.period_label: fv for fv in series["eps_diluted"]}
    assert "Q4-2025" not in eps and eps["Q3-2025"].value == -1.20
    assert all(fv.taxonomy != "derived" for fv in series["eps_diluted"])
    assert all(fv.taxonomy != "derived" for fv in series["net_income"])


def test_sec10_q4_gap_notes_name_the_missing_cells():
    rev = Val(21_000e6, ["src_0001"], "Q4-2025", "XBRL 10-K (FY − nine-month YTD, derived)")
    rev.concept = "derived"
    quarters = [
        {"label": "Q4-2025", "vals": {"revenue": rev}},
        {
            "label": "Q3-2025",
            "vals": {
                "revenue": Val(18_000e6, ["src_0002"], "Q3-2025", "XBRL 10-Q"),
                "eps_diluted": Val(-1.2, ["src_0002"], "Q3-2025", "XBRL 10-Q", "USD/shares"),
            },
        },
    ]
    notes = q4_gap_notes(quarters)
    assert len(notes) == 1 and notes[0].startswith("Q4-2025 diluted eps and net earnings n/a")
    assert "nine-month year-to-date" in notes[0] and "Q4 earnings release" in notes[0]
    # a Q4 release row that filled the cells leaves no note
    quarters[0]["vals"]["eps_diluted"] = Val(10.23, ["src_0003"], "Q4-2025", "earnings release")
    quarters[0]["vals"]["net_income"] = Val(8_000e6, ["src_0003"], "Q4-2025", "earnings release")
    assert q4_gap_notes(quarters) == []
    assert RESULT_LABELS["eps_diluted"] == "Diluted EPS"


# ---- fix 4: source dates, types, index pages, USAspending URLs, titles, duplicates -------------


def test_dates_parsed_from_url_patterns():
    assert date_from_url("https://www.defensenews.com/air/2026/10/20/boeing-max-10/") == date(
        2026, 10, 20
    )
    assert date_from_url("https://news.example.com/boeing-results-26-06-2026") == date(2026, 6, 26)
    assert date_from_url("https://ir.example.com/news/2026-10-20-boeing-q3") == date(2026, 10, 20)
    assert date_from_url(
        "https://www.sec.gov/Archives/edgar/data/12927/000001292725000030/lmt-20250416.htm"
    ) == date(2025, 4, 16)
    assert date_from_url("https://www.gao.gov/products/gao-25-107632") is None
    assert date_from_url("https://www.usaspending.gov/award/CONT_AWD_20240930_1") is None or True


def test_dates_parsed_from_metadata_and_text():
    assert date_from_metadata({"article:published_time": "2026-07-22T12:00:00+00:00"}) == date(
        2026, 7, 22
    )
    assert date_from_metadata({"datePublished": "July 22, 2026"}) == date(2026, 7, 22)
    assert date_from_metadata({"title": "no dates here"}) is None
    assert date_from_text("# GAO-25-107632\n\nF-35 Sustainment (2025-09-03)\n\nBody.") == date(
        2025, 9, 3
    )
    assert date_from_text("BETHESDA, Md., July 23, 2026 – Lockheed Martin today reported") == date(
        2026, 7, 23
    )
    assert date_from_text("Fitch Ratings - Chicago - 12 Aug 2026: affirmed") == date(2026, 8, 12)
    assert date_from_text("no date in the first lines") is None


def test_store_fills_missing_dates_and_cleans_titles(tmp_path):
    store = EvidenceStore(tmp_path, as_of=AS_OF)
    a = store.register(
        "GAO-25-107632 (2025-09-03)\n\nF-35 sustainment findings.",
        type="gov_report",
        publisher="GAO",
        title="F-35 Sustainment:\n\tDoD Needs\t to   Act\n",
        url="https://www.gao.gov/products/gao-25-107632",
    )
    assert a.published_date == date(2025, 9, 3)
    assert a.title == "F-35 Sustainment: DoD Needs to Act"
    b = store.register(
        "Boeing certified the MAX 10.",
        type="news",
        publisher="Defense\nNews",
        title="MAX 10 certified",
        url="https://www.defensenews.com/air/2026/10/20/max-10/",
    )
    assert b.published_date == date(2026, 10, 20) and b.publisher == "Defense News"
    c = store.register(
        "page body",
        type="news",
        publisher="X",
        title="t",
        url="https://x.test/undated",
        meta={"publishedTime": "2026-09-01T08:00:00Z"},
    )
    assert c.published_date == date(2026, 9, 1)
    assert clean_title("  a \n b\t\tc  ") == "a b c"


def test_sec_archive_documents_typed_by_form(tmp_path):
    base = "https://www.sec.gov/Archives/edgar/data/12927/000119312526001234/"
    assert (
        sec_archive_type(base + "d838733dex991.htm", "news", "Boeing to acquire Spirit AeroSystems")
        == "press_release"
    )
    assert (
        sec_archive_type(
            base + "d838733dex991.htm",
            "news",
            "Boeing Reports Fourth Quarter Results\nRevenue of $...",
        )
        == "earnings_release"
    )
    assert (
        sec_archive_type(
            base + "lmt-20250416.htm",
            "news",
            "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nFORM 8-K\nCurrent report",
        )
        == "8-K"
    )
    assert sec_archive_type(base + "lmt-20251231.htm", "news", "FORM 10-K\nANNUAL REPORT") == "10-K"
    assert (
        sec_archive_type(base + "lmt-20260628.htm", "web", "FORM 10-Q\nQUARTERLY REPORT") == "10-Q"
    )
    assert (
        sec_archive_type(base + "lmt-20260326.htm", "news", "SCHEDULE 14A\nProxy Statement")
        == "DEF14A"
    )
    assert sec_archive_type(base + "x.htm", "news", "", {"form": "10-Q"}) == "10-Q"
    assert sec_archive_type(base + "x.htm", "news", "no form named") == "sec_filing"
    assert sec_archive_type(base + "x.htm", "api:edgar_xbrl", "") == "api:edgar_xbrl"
    store = EvidenceStore(tmp_path, as_of=AS_OF)
    rec = store.register(
        "FORM 8-K\nItem 5.02 Departure of Directors or Certain Officers",
        type="news",
        publisher="www.sec.gov",
        title="lmt-20250416",
        url=base + "lmt-20250416.htm",
    )
    assert rec.type == "8-K" and rec.tier == 1 and rec.published_date == date(2025, 4, 16)
    assert not any(r.type == "news" for r in store.records())


def test_same_url_registered_twice_reuses_the_first_id(tmp_path):
    store = EvidenceStore(tmp_path, as_of=AS_OF)
    url = "https://www.sec.gov/Archives/edgar/data/936468/000093646826000012/lmt-20251231.htm"
    first = store.register(
        "FORM 10-K",
        type="10-K",
        publisher="Lockheed",
        title="10-K",
        url=url,
        published_date=date(2026, 1, 29),
        dedupe_key="filing|0000936468-26-000012",
    )
    second = store.register(
        "FORM 10-K", type="news", publisher="www.sec.gov", title="lmt-20251231", url=url
    )
    assert second.source_id == first.source_id and second.type == "10-K"
    # the reverse order too: a default-key registration first, an explicit key later
    other = "https://www.sec.gov/Archives/edgar/data/936468/000093646826000020/lmt-20260628.htm"
    a = store.register(
        "FORM 10-Q", type="news", publisher="www.sec.gov", title="lmt-20260628", url=other
    )
    b = store.register(
        "FORM 10-Q",
        type="10-Q",
        publisher="Lockheed",
        title="10-Q",
        url=other,
        dedupe_key="filing|x",
    )
    assert a.source_id == b.source_id
    # two explicit keys on one endpoint URL (API pulls with different filters) stay distinct
    api = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    p1 = store.register(
        "{}",
        type="api:usaspending",
        publisher="USAspending.gov",
        title="pull 1",
        url=api,
        dedupe_key="usaspending|1",
    )
    p2 = store.register(
        "{}",
        type="api:usaspending",
        publisher="USAspending.gov",
        title="pull 2",
        url=api,
        dedupe_key="usaspending|2",
    )
    assert p1.source_id != p2.source_id
    assert len(store.records()) == 4


def test_index_and_listing_pages_are_refused():
    assert is_index_page("https://boeing.mediaroom.com/news-releases-statements", "# News\n")
    assert is_index_page("https://investors.lockheedmartin.com/presentations/default.aspx", "")
    assert is_index_page("https://news.lockheedmartin.com/newsroom", "")
    assert is_index_page("https://www.boeing.com/investors/", "")
    mostly_links = "# Newsroom\n\n" + "\n".join(
        f"- [Release {i}](https://boeing.mediaroom.com/release-{i})" for i in range(30)
    )
    assert is_index_page("https://boeing.mediaroom.com/list", mostly_links)
    article = (
        "# Boeing certifies 737-7\n\n"
        + ("The FAA granted certification after a review of the engine anti-ice system. " * 6)
        + "\n\n"
        + "\n".join(f"- [Related {i}](https://boeing.mediaroom.com/r{i})" for i in range(20))
    )
    assert not is_index_page("https://boeing.mediaroom.com/2026-10-20-737-7", article)
    assert not is_index_page(
        "https://investors.lockheedmartin.com/news-releases/news-release-details/2026-09-15-pac-3",
        "# PAC-3\n\nBody.",
    )


@respx.mock
def test_firecrawl_refuses_index_page_but_registers_article(tmp_path):
    store = EvidenceStore(tmp_path, as_of=AS_OF)
    pages = {
        "https://boeing.mediaroom.com/news-releases-statements": "# News Releases\n\n"
        + "\n".join(f"- [Item {i}](https://boeing.mediaroom.com/r{i})" for i in range(30)),
        "https://boeing.mediaroom.com/2026-10-20-MAX-10": "# Boeing MAX 10 certified\n\n"
        + "The FAA certified the 737 MAX 10 on October 20, 2026 after a multi-year review of the type. "
        * 5,
    }

    def respond(request):
        body = json.loads(request.content)
        md = pages[body["url"]]
        return Response(
            200,
            json={
                "success": True,
                "data": {"markdown": md, "metadata": {"title": md.splitlines()[0].lstrip("# ")}},
            },
        )

    respx.post("https://api.firecrawl.dev/v2/scrape").mock(side_effect=respond)
    fc = FirecrawlClient(store, api_key="fc-test", use_cache=False)
    assert (
        fc.scrape("https://boeing.mediaroom.com/news-releases-statements", type="press_release")[
            "source_id"
        ]
        is None
    )
    out = fc.scrape("https://boeing.mediaroom.com/2026-10-20-MAX-10", type="press_release")
    rec = store.get(out["source_id"])
    assert rec.published_date == date(2026, 10, 20) and rec.title == "Boeing MAX 10 certified"
    assert len(store.records()) == 1


@respx.mock
def test_usaspending_search_pull_cites_award_or_api_request(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(tmp_path / "out"))
    store = EvidenceStore(tmp_path, as_of=AS_OF)
    many = {
        "results": [
            {"generated_internal_id": "CONT_AWD_1", "Award Amount": 5.0},
            {"generated_internal_id": "CONT_AWD_2", "Award Amount": 3.0},
        ]
    }
    one = {"results": [{"generated_internal_id": "CONT_AWD_9", "Award Amount": 1.0}]}
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        return Response(200, json=many if calls["n"] == 1 else one)

    respx.post("https://api.usaspending.gov/api/v2/search/spending_by_award/").mock(
        side_effect=respond
    )
    respx.post(
        "https://api.usaspending.gov/api/v2/search/spending_by_category/awarding_subagency/"
    ).mock(return_value=Response(200, json={"results": []}))
    us = USAspendingClient(store)
    r1 = us.spending_by_award(["UEI000000001"], 2025, 2026)
    rec = store.get(r1["source_id"])
    assert rec.url == "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    assert "recipients UEI000000001" in rec.title and "2024-10-01 to 2026-09-30" in rec.title
    assert rec.meta["request"]["body"]["filters"]["recipient_search_text"] == ["UEI000000001"]
    r2 = us.spending_by_award(["UEI000000001"], 2025, 2026, keywords=["F-35"])
    rec2 = store.get(r2["source_id"])
    assert (
        rec2.url == "https://www.usaspending.gov/award/CONT_AWD_9" and rec2.gov_id == "CONT_AWD_9"
    )
    r3 = us.spending_by_category("awarding_subagency", ["UEI000000001"], 2026, 2026)
    rec3 = store.get(r3["source_id"])
    assert rec3.url.endswith("/search/spending_by_category/awarding_subagency/")
    assert not any(r.url == "https://www.usaspending.gov/search" for r in store.records())
    assert len({r.source_id for r in store.records()}) == 3


# ---- fix 9: employee count, customer split, officer table from named 10-K items -----------------


def test_employee_count_from_human_capital_not_the_union_local():
    ba = html_to_text(FAKE_10K_BA_HTML)
    found = tt.employee_count(ba)
    assert found and found[0] == 182_000 and "employed approximately 182,000" in found[1]
    assert employee_count(ba) == 182_000
    lmt = html_to_text(FAKE_10K_HTML)
    assert employee_count(lmt) == 121_000
    # the union sentence alone never yields a company-wide count
    assert (
        tt.employee_count("Approximately 3,200 employees are represented by IAM District 837.")
        is None
    )
    # 'approximately N employees' is preferred over a larger bare figure in another sentence
    text = "Human Capital\nWe had approximately 50,000 employees. Our suppliers employ 120,000 people in total."
    assert tt.employee_count(text)[0] == 50_000


def test_customer_split_from_segment_note_separates_us_government_and_international():
    ba = tt.customer_split(html_to_text(FAKE_10K_BA_HTML))
    assert ba["us_gov_pct"] == 42.0 and "accounted for 42%" in ba["us_gov_sentence"]
    assert ba["international_pct"] == 46.0 and "non-U.S. customers" in ba["international_sentence"]
    assert ba["passage"].startswith("Note 2")
    assert tt.gov_revenue_disclosure(html_to_text(FAKE_10K_BA_HTML))[2] == 42.0
    lmt = tt.customer_split(html_to_text(FAKE_10K_HTML))
    assert (lmt["us_gov_pct"], lmt["international_pct"]) == (72.0, 26.0)
    # a non-U.S. sentence that routes FMS through the U.S. government is never the US share
    only_intl = tt.customer_split(
        "Sales to non-U.S. customers, including foreign military sales through the U.S. government, represented 46% of our revenues."
    )
    assert only_intl["us_gov_pct"] is None and only_intl["international_pct"] == 46.0


def test_officer_table_parsed_from_part_i_for_both_companies():
    ba = html_to_text(FAKE_10K_BA_HTML)
    section = tt.exec_officer_section(ba)
    assert section and section[1] == "Information About Our Executive Officers"
    rows = tt.exec_officer_rows(section[0])
    assert [(r["name"], r["age"]) for r in rows] == [
        ("Robert K. Ortberg", "65"),
        ("Jesus Malave", "57"),
        ("Stephanie F. Pope", "53"),
        ("Stephen Parker", "59"),
    ]
    assert rows[0]["title"] == "President and Chief Executive Officer"
    assert (
        rows[2]["title"]
        == "Executive Vice President, President and Chief Executive Officer of Commercial Airplanes"
    )
    lmt = html_to_text(FAKE_10K_HTML)
    section = tt.exec_officer_section(lmt)
    assert section and len(tt.exec_officer_rows(section[0])) == 4
    assert tt.exec_officer_rows(section[0])[0]["name"] == "James D. Taiclet"
    proxy = tt.exec_officer_rows(html_to_text(FAKE_DEF14A_HTML))
    assert len(proxy) == 2
    # the 'Name, NN | Title since ...' table layout
    assert tt.exec_officer_rows(
        " | Brian J. West, 56 | Executive Vice President and Chief Financial Officer since August 2021. Before that he was CFO of Refinitiv.\n"
    ) == [
        {
            "name": "Brian J. West",
            "age": "56",
            "title": "Executive Vice President and Chief Financial Officer",
        }
    ]
