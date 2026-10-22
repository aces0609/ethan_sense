"""Shared fixtures: recorded EDGAR and USAspending responses served through respx, fake filing
documents rich enough for every section handler, a Firecrawl mock factory, and an isolated
output root per test so the repo's output/ and its 7-day Firecrawl cache are never touched.

Handler tests build on these without editing this file: `firecrawl_mock(...)` takes the
search hits, page markdown, JSON extractions and site maps a test needs; `usaspending_mock`
serves the recorded Lockheed fixtures under tests/fixtures/usaspending/.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

os.environ.setdefault("EDGAR_USER_AGENT", "ethan_sense-tests/0.1 (tests@example.com)")
# Tests always exercise the template path. Set the keys to "" rather than popping them:
# primer.config calls load_dotenv (override=False) on import, which would restore a popped
# key from .env, and an empty value is "present" to dotenv but falsy to active_provider().
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ[_key] = ""

FIXTURES = Path(__file__).parent / "fixtures"
EDGAR_FIXTURES = FIXTURES / "edgar"
LMT_CIK = "0000936468"

_FILLER = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 12

FAKE_10K_HTML = """<html><head><title>lmt-20251231</title><style>.x{}</style></head><body>
<div style="display:none"><ix:header>hidden xbrl header text that must not appear</ix:header></div>
<div>
<p>Our website address is www.lockheedmartin.com. Lockheed Martin Corporation [NYSE: LMT].</p>
<p><b>Item 1. Business</b></p>
<p>Lockheed Martin Corporation is a global security and aerospace company principally engaged in the research, design, development, manufacture, integration and sustainment of advanced technology systems, products and services. {filler}</p>
<p>We operate in four business segments: Aeronautics, Missiles and Fire Control (MFC), Rotary and Mission Systems (RMS), and Space. We derived 72% of our total consolidated sales from the U.S. Government in 2025, including 63% from the Department of Defense. International customers accounted for 26% of sales.</p>
<p>Aeronautics is engaged in the research, design, development, manufacture, integration, sustainment, support and upgrade of advanced military aircraft, including combat and air mobility aircraft, unmanned air vehicles and related technologies. Major programs include the F-35 Lightning II, C-130 Hercules, F-16 Fighting Falcon and F-22 Raptor. {filler}</p>
<p>MFC provides air and missile defense systems, including the PAC-3 and THAAD programs, tactical missiles and precision strike weapons such as GMLRS and HIMARS, and fire control systems. {filler}</p>
<p>RMS designs, manufactures, services and supports military and commercial helicopters such as the Black Hawk and CH-53K, surface ships, sea and land-based missile defense systems including Aegis, radar systems and integrated air and missile defense. {filler}</p>
<p>Space is engaged in the research and development, design, engineering and production of satellites, space transportation systems, and strategic, advanced strike and defensive systems, including the Trident II D5 fleet ballistic missile, the Next Generation Interceptor and GPS III satellites. {filler}</p>
<p><b>Competition</b></p>
<p>We compete both domestically and internationally with large global contractors including The Boeing Company, General Dynamics Corporation, Northrop Grumman Corporation, RTX Corporation and L3Harris Technologies, as well as with Airbus SE and BAE Systems plc in international markets. Competition is based on technology, price, past performance and the ability to meet customer schedules. {filler}</p>
<p><b>Human Capital</b></p>
<p>At December 31, 2025 we had approximately 121,000 employees. {filler}</p>
<p><b>Item 1A. Risk Factors</b></p>
<p>We depend heavily on contracts with the U.S. Government for a substantial portion of our business, and changes in the U.S. Government's priorities or budget could adversely affect our business.</p>
<p>{filler}</p>
<p>Our F-35 program represents a significant portion of our sales, and any delays or cost overruns on the program could materially affect our results.</p>
<p>{filler}</p>
<p>We may be unable to compete effectively against our competitors, which could result in lost sales.</p>
<p>{filler}</p>
<p>Our business could be adversely affected by cybersecurity incidents, supply chain disruptions and the loss of key personnel.</p>
<p>{filler}</p>
<p><b>Item 2. Properties</b></p>
<p>Our principal properties include facilities in Fort Worth, Texas (Aeronautics), Grand Prairie, Texas (MFC), Orlando, Florida (MFC and RMS), Stratford, Connecticut (RMS), Owego, New York (RMS), Denver, Colorado (Space) and Sunnyvale, California (Space). Our corporate headquarters is in Bethesda, Maryland. {filler}</p>
<p><b>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</b></p>
<p>Consolidated net sales increased $4.0 billion, or 6%, in 2025 compared to 2024. The increase was primarily due to higher net sales at Aeronautics and MFC. {filler}</p>
<p>Outlook. We expect 2026 net sales of approximately $79.8 billion to $81.8 billion and diluted earnings per share of $29.95 to $30.65. We expect to return substantially all of our free cash flow to shareholders through dividends and share repurchases while increasing capital expenditures to expand missile production capacity. {filler}</p>
<p>Liquidity and Capital Resources. At December 31, 2025 we had $4,121 million of cash and cash equivalents and total debt of $21,700 million. We have a $3.0 billion revolving credit facility that expires in August 2029 with no borrowings outstanding. Our long-term debt is rated A- by S&amp;P and A3 by Moody's. {filler}</p>
<table>
<tr><td>(in millions)</td><td>2025</td><td>2024</td><td>2023</td></tr>
<tr><td>Net sales</td><td></td><td></td><td></td></tr>
<tr><td>Aeronautics</td><td>$</td><td>30,200</td><td>$</td><td>28,900</td><td>$</td><td>27,500</td></tr>
<tr><td>Missiles and Fire Control</td><td></td><td>14,600</td><td></td><td>12,800</td><td></td><td>11,200</td></tr>
<tr><td>Rotary and Mission Systems</td><td></td><td>17,800</td><td></td><td>17,300</td><td></td><td>16,400</td></tr>
<tr><td>Space</td><td></td><td>12,448</td><td></td><td>12,043</td><td></td><td>12,471</td></tr>
<tr><td>Total net sales</td><td>$</td><td>75,048</td><td>$</td><td>71,043</td><td>$</td><td>67,571</td></tr>
<tr><td>Operating profit</td><td></td><td></td><td></td></tr>
<tr><td>Aeronautics</td><td>$</td><td>2,900</td><td>$</td><td>2,500</td><td>$</td><td>2,800</td></tr>
<tr><td>Missiles and Fire Control</td><td></td><td>1,900</td><td></td><td>1,100</td><td></td><td>1,500</td></tr>
<tr><td>Rotary and Mission Systems</td><td></td><td>1,800</td><td></td><td>1,900</td><td></td><td>1,900</td></tr>
<tr><td>Space</td><td></td><td>1,131</td><td></td><td>1,513</td><td></td><td>1,158</td></tr>
<tr><td>Total business segment operating profit</td><td>$</td><td>7,731</td><td>$</td><td>7,013</td><td>$</td><td>7,358</td></tr>
</table>
<p><b>Item 8. Financial Statements and Supplementary Data</b></p>
<p>Note 3 – Acquisitions and Divestitures. On July 15, 2026 we entered into a definitive agreement to acquire Ultra Maritime for approximately $1.5 billion in cash. In 2024 we acquired Terran Orbital Corporation for approximately $450 million, which is included in our Space segment. In 2022 we completed the divestiture of our Sikorsky Commercial business for $200 million. {filler}</p>
<p>Note 10 – Debt. Our long-term debt consists of notes with maturities from 2027 to 2063. In May 2025 we issued $2.0 billion of senior unsecured notes, including $750 million of 4.75% notes due 2030 and $1.25 billion of 5.25% notes due 2055. We had $1,168 million of debt due within one year at December 31, 2025. {filler}</p>
<p><b>Item 10. Directors, Executive Officers and Corporate Governance</b></p>
<p>Information about our Executive Officers</p>
<table>
<tr><td>Name</td><td>Age</td><td>Position</td></tr>
<tr><td>James D. Taiclet</td><td>65</td><td>Chairman, President and Chief Executive Officer</td></tr>
<tr><td>Evan T. Scott</td><td>52</td><td>Chief Financial Officer</td></tr>
<tr><td>Maria A. Ricciardone</td><td>48</td><td>Vice President, Investor Relations and Treasurer</td></tr>
<tr><td>Frank A. St. John</td><td>60</td><td>Chief Operating Officer</td></tr>
</table>
<p>{filler}</p>
<p><b>Item 15. Exhibits</b></p><p>{filler}</p>
</div></body></html>""".replace("{filler}", _FILLER)

FAKE_10Q_HTML = """<html><head><title>lmt-20260628</title></head><body><div>
<p><b>Item 1. Financial Statements</b></p>
<p>Note 1 – Basis of Presentation. {filler}</p>
<p>Note 8 – Debt. At June 28, 2026 our total debt was $21,900 million, of which $1,200 million is due within one year. In March 2026 we repaid $500 million of 3.55% notes at maturity. We have $3.0 billion of available capacity under our revolving credit facility, which matures in August 2029; no borrowings were outstanding. {filler}</p>
<p>Note 11 – Business Combinations. On July 15, 2026 we announced an agreement to acquire Ultra Maritime for approximately $1.5 billion. {filler}</p>
<p><b>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</b></p>
<p>Net sales in the second quarter of 2026 were $20.1 billion, an increase of 11% compared to $18.2 billion in the second quarter of 2025. Backlog at June 28, 2026 was a record $230 billion. {filler}</p>
<p>Liquidity and Capital Resources. Cash and cash equivalents were $2,800 million at June 28, 2026. We paid dividends of $1,566 million and repurchased $1,000 million of common stock in the first half of 2026. Our senior unsecured debt is rated A- (S&amp;P), A3 (Moody's) and A (Fitch). {filler}</p>
<p><b>Item 4. Controls and Procedures</b></p><p>{filler}</p>
</div></body></html>""".replace("{filler}", _FILLER)

FAKE_DEF14A_HTML = """<html><head><title>lmt-20260326</title></head><body><div>
<p>Proxy Statement. Annual Meeting of Stockholders, April 30, 2026.</p>
<p>Our Board of Directors consists of 12 directors, 11 of whom are independent under NYSE listing standards. Mr. Taiclet serves as Chairman and Chief Executive Officer; Daniel F. Akerson serves as Lead Independent Director. {filler}</p>
<p>Security Ownership of Certain Beneficial Owners and Management</p>
<table>
<tr><td>Name and Address of Beneficial Owner</td><td>Shares Beneficially Owned</td><td>Percent of Class</td></tr>
<tr><td>State Street Corporation</td><td>36,100,000</td><td>15.6%</td></tr>
<tr><td>The Vanguard Group</td><td>21,200,000</td><td>9.2%</td></tr>
<tr><td>BlackRock, Inc.</td><td>16,900,000</td><td>7.3%</td></tr>
<tr><td>All directors and executive officers as a group (24 persons)</td><td>640,000</td><td>0.3%</td></tr>
</table>
<p>Executive Compensation. Total compensation for Mr. Taiclet in 2025 was $24.5 million, of which 90% was at-risk. The 2025 say-on-pay proposal received 94% support. {filler}</p>
<p>Executive Officers</p>
<table>
<tr><td>Name</td><td>Age</td><td>Position</td></tr>
<tr><td>James D. Taiclet</td><td>65</td><td>Chairman, President and Chief Executive Officer</td></tr>
<tr><td>Evan T. Scott</td><td>52</td><td>Chief Financial Officer</td></tr>
</table>
<p>{filler}</p>
</div></body></html>""".replace("{filler}", _FILLER)

FAKE_8K_HTML = """<html><body><div>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION. FORM 8-K. Lockheed Martin Corporation.</p>
<p>Item 1.01 Entry into a Material Definitive Agreement. On July 15, 2026, Lockheed Martin Corporation entered into a definitive agreement to acquire Ultra Maritime from Advent International for approximately $1.5 billion in cash, subject to regulatory approvals. The transaction is expected to close in the first half of 2027.</p>
<p>Item 8.01 Other Events. On December 18, 2025 the Board of Directors authorized an additional $3.0 billion of share repurchases and declared a quarterly dividend of $3.45 per share.</p>
<p>Item 9.01 Financial Statements and Exhibits. Exhibit 99.1 Press release.</p>
</div></body></html>"""

FAKE_RELEASE_HTML = """<html><body><div>
<p>Exhibit 99.1</p><p>News Release</p>
<p>Lockheed Martin Reports Second Quarter 2026 Financial Results</p>
<p>Sales increase of 11% to $20.1 billion. Net earnings of $1.8 billion, or $7.94 per share. Cash from operations of $3.2 billion and free cash flow of $2.9 billion. Record backlog of $230 billion. Updates 2026 financial outlook.</p>
<p>BETHESDA, Md., July 23, 2026 – Lockheed Martin Corporation [NYSE: LMT] today reported second quarter 2026 sales of $20.1 billion, compared to $18.2 billion in the second quarter of 2025. Net earnings in the second quarter of 2026 were $1.8 billion, or $7.94 per share, compared to $342 million, or $1.46 per share, in the second quarter of 2025.</p>
<p>Outlook</p>
<table>
<tr><td>(in millions, except per share data)</td><td>Current Update</td><td>April 2026</td></tr>
<tr><td>Sales</td><td>~$79,750 - $81,750</td><td>$77,500 - $80,000</td></tr>
<tr><td>Business segment operating profit</td><td>~$8,500 - $8,700</td><td>$8,425 - $8,675</td></tr>
<tr><td>Diluted earnings per share</td><td>~$29.95 - $30.65</td><td>$29.35 - $30.25</td></tr>
<tr><td>Cash from operations</td><td>~$9,200 - $9,400</td><td>$9,150 - $9,450</td></tr>
<tr><td>Free cash flow</td><td>~$7,000 - $7,200</td><td>$6,500 - $6,800</td></tr>
</table>
<p>Summary Financial Results</p>
<table>
<tr><td>(in millions, except per share data)</td><td>Q2 2026</td><td>Q2 2025</td></tr>
<tr><td>Net sales</td><td>$20,100</td><td>$18,155</td></tr>
<tr><td>Business segment operating profit</td><td>$2,150</td><td>$570</td></tr>
<tr><td>Net earnings</td><td>$1,830</td><td>$342</td></tr>
<tr><td>Diluted earnings per share</td><td>$7.94</td><td>$1.46</td></tr>
<tr><td>Cash from operations</td><td>$3,200</td><td>$201</td></tr>
</table>
<p>Segment Results. Aeronautics net sales in the second quarter of 2026 increased $650 million, or 9%, to $8.1 billion. MFC net sales increased 18% to $3.9 billion. RMS net sales were $4.6 billion. Space net sales were $3.5 billion.</p>
</div></body></html>"""

FAKE_TRANSCRIPT_MD = """# Lockheed Martin (LMT) Q2 2026 Earnings Call Transcript

**Operator:** Good day, and welcome to the Lockheed Martin second quarter 2026 earnings results conference call.

**James Taiclet:** Thank you. We delivered sales of $20.1 billion, up 11%, with record backlog of $230 billion. We are raising our full-year outlook.

**Evan Scott:** Free cash flow was $2.9 billion in the quarter. We now expect 2026 sales of $79.75 billion to $81.75 billion.

**Operator:** Your first question comes from Scott Micas with Melius Research. Your line is open.

**Scott Micas:** Good morning. Can you talk about the F-35 Lot 20 negotiations and the margin outlook for Aeronautics?

**James Taiclet:** Sure, Scott. We expect Lot 20 definitization in the fourth quarter.

**Operator:** Your next question comes from Kristine Liwag with Morgan Stanley.

**Kristine Liwag:** Thanks. On PAC-3 capacity, how fast can you get to 750 per year?

**Evan Scott:** We expect to reach 650 in 2027 and 750 by 2028.

**Operator:** Your next question comes from Douglas Harned with Bernstein.

**Douglas Harned:** On capital deployment, is the 100% free-cash-flow return policy still intact given the capex step-up?
"""


def _usaspending_fixtures() -> dict[str, Any]:
    d = FIXTURES / "usaspending"
    out = {}
    for p in d.glob("*.json"):
        out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    return out


@pytest.fixture(scope="session")
def fixture_json():
    def load(name: str, area: str = "edgar"):
        return json.loads((FIXTURES / area / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture(autouse=True)
def _isolated_output(tmp_path, monkeypatch):
    """Every test writes caches, quota files and runs under its own tmp output root, so the
    repo's output/ (and its 7-day Firecrawl cache) is never touched by the suite."""
    out = tmp_path / "output"
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(out))
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("SAM_GOV_API_KEY", raising=False)
    monkeypatch.setenv("PRIMER_NO_CRITIC_LOOP", "1")
    from primer.clients import firecrawl as _fc

    monkeypatch.setattr(_fc._BUCKET, "rate", 10_000.0)  # no per-minute throttle on mocked calls
    from primer.sections import common

    common._MEMO.clear()
    try:
        from primer.adapters import market_data_scraped

        market_data_scraped._MEMO.clear()
    except ImportError:
        pass
    yield out


@pytest.fixture
def output_dir(_isolated_output):
    return _isolated_output


@pytest.fixture
def edgar_mock(fixture_json):
    """Recorded EDGAR JSON plus fake filing documents for every sec.gov document URL."""
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=Response(200, json=fixture_json("company_tickers_exchange.json"))
        )
        router.get(f"https://data.sec.gov/submissions/CIK{LMT_CIK}.json").mock(
            return_value=Response(200, json=fixture_json("submissions_LMT.json"))
        )
        router.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{LMT_CIK}.json").mock(
            return_value=Response(200, json=fixture_json("companyfacts_LMT.json"))
        )
        router.get(
            url__regex=r"https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/index\.json$"
        ).mock(
            return_value=Response(
                200,
                json={
                    "directory": {
                        "item": [
                            {"name": "ex991.htm", "type": "text.gif", "size": "5000"},
                            {"name": "primary.htm", "type": "text.gif", "size": "9000"},
                        ]
                    }
                },
            )
        )
        router.get(
            url__regex=r"https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/ex99.*\.htm$"
        ).mock(return_value=Response(200, text=FAKE_RELEASE_HTML))
        # route each filing's primary document by its accession folder, from the fixture
        subs = fixture_json("submissions_LMT.json")["filings"]["recent"]
        fakes = {"10-Q": FAKE_10Q_HTML, "DEF 14A": FAKE_DEF14A_HTML, "8-K": FAKE_8K_HTML}
        for i, form in enumerate(subs["form"]):
            html = fakes.get(form)
            if html is None:
                continue
            folder = subs["accessionNumber"][i].replace("-", "")
            router.get(
                url__regex=rf"https://www\.sec\.gov/Archives/edgar/data/\d+/{folder}/.*\.htm$"
            ).mock(return_value=Response(200, text=html))
        router.get(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/.*\.htm$").mock(
            return_value=Response(200, text=FAKE_10K_HTML)
        )
        router.get(url__regex=r"https://efts\.sec\.gov/LatestSearch/.*").mock(
            return_value=Response(
                200,
                json={
                    "hits": {
                        "total": {"value": 2},
                        "hits": [
                            {
                                "_id": "0000936468-26-000050:lmt-8k-ultra.htm",
                                "_source": {
                                    "ciks": ["936468"],
                                    "file_date": "2026-07-16",
                                    "form_type": "8-K",
                                    "display_names": ["LOCKHEED MARTIN CORP (LMT)"],
                                    "file_description": "8-K Item 1.01 Entry into a Material Definitive Agreement — Ultra Maritime",
                                },
                            },
                            {
                                "_id": "0000936468-24-000031:lmt-8k-terran.htm",
                                "_source": {
                                    "ciks": ["936468"],
                                    "file_date": "2024-04-30",
                                    "form_type": "8-K",
                                    "display_names": ["LOCKHEED MARTIN CORP (LMT)"],
                                    "file_description": "8-K Item 2.01 Completion of Acquisition — Terran Orbital",
                                },
                            },
                        ],
                    }
                },
            )
        )
        yield router


def _hit(url: str, title: str, description: str = "") -> dict[str, Any]:
    return {"url": url, "title": title, "description": description}


class FirecrawlRouter:
    """Routes Firecrawl v2 requests to canned responses by query/URL substring.

    search:  {substring of query: [hit dicts]}       (hit = {"url","title","description"})
    pages:   {substring of url: markdown}             (/scrape; markdown returned, page registered)
    jsons:   {substring of url: dict}                 (/scrape with a json format)
    maps:    {substring of url: [links]}              (/map)
    agents:  {substring of prompt: data dict}         (/agent, completes synchronously)
    Unmatched requests return an empty result (not an error) so budget logic is exercised.
    """

    def __init__(self, search=None, pages=None, jsons=None, maps=None, agents=None):
        self.search = search or {}
        self.pages = pages or {}
        self.jsons = jsons or {}
        self.maps = maps or {}
        self.agents = agents or {}
        self.calls: list[tuple[str, Any]] = []

    @staticmethod
    def _match(table: dict[str, Any], key: str):
        best = None
        for sub, val in table.items():
            if sub.lower() in key.lower() and (best is None or len(sub) > len(best[0])):
                best = (sub, val)
        return best[1] if best else None

    def __call__(self, request):
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if path.endswith("/search"):
            hits = self._match(self.search, body.get("query", "")) or []
            self.calls.append(("search", body.get("query")))
            return Response(200, json={"success": True, "data": {"web": hits}})
        if path.endswith("/scrape"):
            url = body.get("url", "")
            self.calls.append(("scrape", url))
            md = self._match(self.pages, url)
            if md is None:
                md = f"# {url}\n\nNo canned page for this URL in the test fixture.\n"
            data: dict[str, Any] = {
                "markdown": md,
                "metadata": {
                    "title": md.splitlines()[0].lstrip("# ").strip()[:120],
                    "statusCode": 200,
                    "sourceURL": url,
                },
            }
            j = self._match(self.jsons, url)
            if j is not None and any(
                isinstance(f, dict) and f.get("type") == "json" for f in body.get("formats", [])
            ):
                data["json"] = j
            return Response(200, json={"success": True, "data": data})
        if path.endswith("/map"):
            links = self._match(self.maps, body.get("url", "")) or []
            self.calls.append(("map", body.get("url")))
            return Response(200, json={"success": True, "links": links})
        if path.endswith("/agent"):
            data = self._match(self.agents, body.get("prompt", "")) or {}
            self.calls.append(("agent", body.get("prompt")))
            return Response(200, json={"success": True, "data": data, "creditsUsed": 10})
        return Response(404, json={"success": False, "error": f"no route for {path}"})


@pytest.fixture
def firecrawl_mock(monkeypatch):
    """Factory: `fc = firecrawl_mock(search={...}, pages={...}, jsons={...}, maps={...})`.
    Sets a test API key and routes api.firecrawl.dev through the router; `fc.calls` records
    every call so tests can assert on budgets."""
    routers: list[tuple[Any, FirecrawlRouter]] = []

    def make(**tables) -> FirecrawlRouter:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
        fr = FirecrawlRouter(**tables)
        mock = respx.mock(assert_all_called=False, assert_all_mocked=True)
        mock.start()
        mock.route(host="api.firecrawl.dev").mock(side_effect=fr)
        routers.append((mock, fr))
        return fr

    yield make
    for mock, _ in routers:
        mock.stop()


@pytest.fixture
def usaspending_mock():
    """Recorded Lockheed responses: name discovery page, UEI-filtered awards, subagency
    categories for FY2025/FY2026, one award detail. Routed by request body / path."""
    fx = _usaspending_fixtures()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:

        def by_award(request):
            body = json.loads(request.content or b"{}")
            names = body.get("filters", {}).get("recipient_search_text", [])
            if any(re.fullmatch(r"[A-Z0-9]{12}", n or "") for n in names):
                return Response(200, json=fx["spending_by_award_uei_LMT"])
            return Response(200, json=fx["spending_by_award_name_LMT"])

        def by_category(request):
            body = json.loads(request.content or b"{}")
            tp = body.get("filters", {}).get("time_period", [{}])[0]
            fy = (tp.get("end_date") or "2026")[:4]
            key = f"spending_by_category_subagency_FY{fy}_LMT"
            return Response(
                200, json=fx.get(key, {"results": [], "category": "awarding_subagency"})
            )

        router.post("https://api.usaspending.gov/api/v2/search/spending_by_award/").mock(
            side_effect=by_award
        )
        router.post(
            url__regex=r"https://api\.usaspending\.gov/api/v2/search/spending_by_category/.*"
        ).mock(side_effect=by_category)
        router.get(url__regex=r"https://api\.usaspending\.gov/api/v2/awards/.*").mock(
            return_value=Response(200, json=fx["award_detail_LMT"])
        )
        yield router


__all__ = [
    "FAKE_10K_HTML",
    "FAKE_10Q_HTML",
    "FAKE_8K_HTML",
    "FAKE_DEF14A_HTML",
    "FAKE_RELEASE_HTML",
    "FAKE_TRANSCRIPT_MD",
    "FirecrawlRouter",
    "LMT_CIK",
    "_hit",
]
