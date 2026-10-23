"""A company with no 10-K yet (SpaceX, listed June 2026) is read from its IPO prospectus and 10-Qs.

On 2026-10-23 the SPCX eval entry came out with seven unavailable sections because the foundation
pack required a 10-K and the financial sections required annual XBRL. The fallback chunks the
prospectus under the 10-K item numbers, builds interim columns from 10-Q facts, and labels the
document honestly.
"""

from __future__ import annotations

from datetime import date

from primer.clients.interim import interim_financials
from primer.evidence.chunking import chunk_by_item, chunk_prospectus
from primer.evidence.store import EvidenceStore
from primer.graph.foundation import _prospectus_fallback
from primer.runlog import RunLog
from primer.sections.exhibits import financial_tables
from primer.sections.style import relabel_annual_document
from primer.state import Entity, FilingRef

AS_OF = date(2026, 10, 23)


def _para(word: str, n: int = 60) -> str:
    return " ".join([word] * n)


def _prospectus() -> str:
    toc = "\n".join(
        [
            "TABLE OF CONTENTS",
            "Prospectus summary ........ 1",
            "Risk factors ........ 20",
            "Business ........ 120",
            "RISK FACTORS .......... 20",
        ]
    )
    body = []
    for title in (
        "PROSPECTUS SUMMARY",
        "RISK FACTORS",
        "USE OF PROCEEDS",
        "MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS",
        "BUSINESS",
        "MANAGEMENT",
        "EXECUTIVE COMPENSATION",
        "UNDERWRITING",
        "INDEX TO FINANCIAL STATEMENTS",
    ):
        body += [title, _para(title.split()[0].lower(), 90), ""]
    return "PROSPECTUS\nBlah\n" + toc + "\n\n" + "\n".join(body)


def test_prospectus_sections_carry_the_10k_item_numbers():
    chunks = chunk_by_item(_prospectus())
    by = {c.item: c for c in chunks}
    assert {"S", "1A", "7", "1", "10", "11", "8"} <= set(by)
    assert "business" in by["1"].text.lower() and by["1"].title == "Business"
    # a section that is not read still ends the one before it
    assert "use of proceeds" not in by["1A"].text.lower().split("\n", 1)[1]
    assert chunk_prospectus("no headings here " * 100) == []


def test_a_10k_is_still_chunked_by_item():
    tenk = "\n".join(f"Item {n}. Heading {n}\n{_para('text', 120)}" for n in ("1", "1A", "7", "8"))
    assert [c.item for c in chunk_by_item(tenk)] == ["1", "1A", "7", "8"]


def test_own_10k_wording_follows_the_prospectus_but_a_peers_does_not():
    md = (
        "Segments come from 10-K Item 1 [src_0001]. Risks are in 10-K Item 1A. "
        "The 10-K names Boeing; peer 10-K Item 1 [src_0002] names Airbus."
    )
    out = relabel_annual_document(md)
    assert "IPO prospectus Business section [src_0001]" in out
    assert "IPO prospectus Risk Factors" in out
    assert "The IPO prospectus names Boeing" in out
    assert "peer 10-K Item 1 [src_0002]" in out


class _FakeEdgar:
    def __init__(self, refs, html):
        self.refs, self.html = refs, html

    def filings(self, cik, forms=None, limit=None, since=None):
        return [r for r in self.refs if not forms or r.form in forms]

    def get_text(self, url):
        return (
            "<html><body>"
            + "".join(f"<p>{ln}</p>" for ln in self.html.split("\n"))
            + "</body></html>"
        )


def _ref(form, filed):
    return FilingRef(
        form=form,
        filing_date=filed,
        accession=f"0001-26-{form[:3]}",
        primary_document="p.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{form}/p.htm",
        index_url="https://www.sec.gov/x",
    )


def test_the_foundation_registers_the_prospectus_under_the_10k_keys(tmp_path):
    store = EvidenceStore(tmp_path / "run", as_of=AS_OF)
    ent = Entity(name="NEW CO", ticker="NEW", cik="0000000001", is_public=True)
    edgar = _FakeEdgar(
        [_ref("S-1", date(2026, 5, 20)), _ref("424B4", date(2026, 6, 12))], _prospectus()
    )
    found: dict = {"filings": {}, "sources": {}, "chunks": {}}
    _prospectus_fallback(edgar, store, ent, AS_OF, found, RunLog(tmp_path / "run"))
    assert found["annual_basis"]["form"] == "424B4"  # the final prospectus beats the S-1
    sid = found["sources"]["10-K"][0]
    assert store.get(sid).type == "prospectus" and store.get(sid).tier == 1
    assert {i["item"] for i in found["chunks"]["10-K"][0]["items"]} >= {"1", "1A", "7"}
    assert found["filings"]["10-K"] == []


def _fact(end, val, start=None, accn="0001-26-000001", filed="2026-10-22", form="10-Q"):
    row = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": 2026}
    if start:
        row["start"] = start
    return row


def _facts():
    def node(*rows):
        return {"units": {"USD": list(rows)}}

    rev = node(
        _fact("2025-09-30", 5576e6, "2025-07-01"),
        _fact("2025-09-30", 13714e6, "2025-01-01"),
        _fact("2026-06-30", 7814e6, "2026-04-01", accn="0001-26-000002", filed="2026-08-04"),
        _fact("2026-09-30", 14575e6, "2026-07-01"),
        _fact("2026-09-30", 27083e6, "2026-01-01"),
    )
    ni = node(
        _fact("2025-09-30", -1054e6, "2025-07-01"),
        _fact("2026-06-30", -541e6, "2026-04-01", accn="0001-26-000002", filed="2026-08-04"),
        _fact("2026-09-30", 4404e6, "2026-07-01"),
    )
    cash = node(
        _fact("2025-12-31", 24747e6),
        _fact("2026-09-30", 51294e6),
    )
    return {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": rev,
                "NetIncomeLoss": ni,
                "CashAndCashEquivalentsAtCarryingValue": cash,
            }
        }
    }


def test_interim_columns_come_from_10q_facts_and_are_labelled_by_period():
    fin = interim_financials(_facts(), "0001181412", AS_OF)
    assert fin.interim and fin.fiscal_year_end_phrase == "interim periods from 10-Q filings"
    labels = list(fin.labels.values())
    assert "3M to Sep 2026" in labels and "9M to Sep 2026" in labels
    cur = next(k for k, v in fin.labels.items() if v == "3M to Sep 2026")
    ytd = next(k for k, v in fin.labels.items() if v == "9M to Sep 2026")
    assert fin.value("revenue", cur) == 14575e6 and fin.value("revenue", ytd) == 27083e6
    assert fin.value("net_income", cur) == 4404e6
    # the balance sheet at the last fiscal year end is its own column
    fye = next(k for k, v in fin.labels.items() if v == "Balance sheet Dec 2025")
    assert fin.value("cash", fye) == 24747e6
    # a period no 10-Q reports stays empty, never estimated
    assert fin.value("revenue", fye) is None


def test_interim_tables_use_the_period_labels_not_fy(tmp_path):
    fin = interim_financials(_facts(), "0001181412", AS_OF)
    sids = {a: f"src_{i:04d}" for i, a in enumerate(fin.accessions, start=1)}
    ex = financial_tables(fin, sids, section=9)
    assert "3M to Sep 2026" in ex.markdown and "| FY" not in ex.markdown
    assert "Balance sheet (period end of each column)" in ex.markdown
    assert ex.claims and all(not c.period.startswith("FY") for c in ex.claims)


def test_no_quarterly_facts_means_no_columns():
    fin = interim_financials({"facts": {"us-gaap": {}}}, "0001181412", AS_OF)
    assert fin.fiscal_years == []


def test_a_page_share_count_is_labelled_and_exempt_from_the_tier_rule(tmp_path):
    """SpaceX's filings carry no XBRL cover-page share count, so the adapter falls back to a
    statistics page. The primer must say so, and the tier rule must not flag it."""
    from types import SimpleNamespace

    from primer.sections.sec13_valuation import PAGE_FALLBACK_CONCEPT, _shares_val
    from primer.validate.rules import check_tier

    store = EvidenceStore(tmp_path / "run", as_of=AS_OF)
    page = store.register(
        "13,580 million shares", type="market_data", publisher="Stats", title="t",
        url="https://stats.example.com/spcx", tier=2,
    )  # fmt: skip
    d = SimpleNamespace(
        ok=True,
        value=13.58e9,
        source_id=page.source_id,
        period="2026-10-23",
        as_of=AS_OF,
        note=" XBRL dei share count unavailable; page value used",
    )
    v = _shares_val(d)
    assert v.concept == PAGE_FALLBACK_CONCEPT and "scraped" in v.basis
    claim = {
        "metric": "shares_outstanding",
        "period": "2026-10-23",
        "section": 13,
        "source_ids": [page.source_id],
        "concept": v.concept,
    }
    assert check_tier([claim], store)["ok"]
    assert not check_tier([{**claim, "concept": "dei:EntityCommonStockSharesOutstanding"}], store)[
        "ok"
    ]


def test_technology_acronyms_are_not_competitors():
    from primer.sections.text_tools import competitor_candidates

    text = (
        "We compete with Amazon and Boeing in launch. We also compete in AGI, LLM and GPU markets, "
        "where Anthropic and OpenAI compete with us.\n\n" * 3
    )
    names = competitor_candidates(text, "Space Exploration Technologies Corp")
    assert "AGI" not in names and "LLM" not in names and "GPU" not in names
    assert "Amazon" in names and "Boeing" in names


def test_our_own_caveats_survive_the_relabelling():
    """The relabel turns "10-K" into the prospectus label, so a caveat that says the company has
    no 10-K must not use that word ("has not filed a IPO prospectus yet" shipped once)."""
    import inspect

    from primer.sections import interim_draft, sec11_capital, sec13_valuation

    for mod in (interim_draft, sec11_capital, sec13_valuation):
        src = inspect.getsource(mod)
        for line in src.splitlines():
            if "not yet filed" in line or "has not filed" in line:
                assert "10-K" not in line, line


def test_a_bill_is_not_a_firm_in_the_attribution_check(tmp_path):
    """ "A draft FY2027 NDAA said 100 B-21s may be insufficient" failed attribution because the cited
    article spells out the National Defense Authorization Act."""
    from primer.validate.rules import check_attribution

    store = EvidenceStore(tmp_path / "run", as_of=AS_OF)
    rec = store.register(
        "The House Armed Services Committee's National Defense Authorization Act draft says 100 B-21s may be insufficient.",
        type="news", publisher="Paper", title="t", url="https://news.example.com/a", tier=2,
    )  # fmt: skip
    md = f"- **House Armed Services Committee:** a draft FY2027 NDAA said 100 B-21s may be insufficient [{rec.source_id}]."
    assert check_attribution({3: md}, store)["ok"]
