"""PLAN 3.4: the private-company path.

A fake private company ("Acme Orbital Inc", no SEC match) and `--private` forced on LMT run
offline on recorded EDGAR JSON and hand-written Firecrawl pages. The assertions are the
honesty rules: no SEC submissions/companyfacts request, sections 9/11/13 `partial` or
`unavailable` with a one-line coverage blockquote, every claim cited, only `press_*` (and
`form_d_*`) metrics, no derived multiple, and the validation gate still passes.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
import yaml
from httpx import Response

from primer.clients.edgar import EdgarClient
from primer.evidence.store import EvidenceStore
from primer.graph.main import (
    foundation_pack,
    intake,
    new_run,
    plan_sections,
    resolve_entity,
    run_primer,
)
from primer.graph.researcher import build_researcher
from primer.render.markdown import render
from primer.sections import private as pv
from primer.state import Entity, Inputs
from primer.validate.rules import check_tier
from tests.conftest import LMT_CIK, _hit

AS_OF = date(2026, 10, 21)
ACME = "Acme Orbital Inc"
ACME_CIK = "0001234567"
ACCESSION = "0001234567-26-000042"
FORM_D_XML = f"""<?xml version="1.0"?>
<edgarSubmission><submissionType>D</submissionType>
<primaryIssuer><cik>{ACME_CIK}</cik><entityName>Acme Orbital Inc.</entityName></primaryIssuer>
<relatedPersonsList>
<relatedPersonInfo><relatedPersonName><firstName>Dana</firstName><lastName>Reyes</lastName></relatedPersonName>
<relatedPersonRelationshipList><relationship>Executive Officer</relationship><relationship>Director</relationship></relatedPersonRelationshipList></relatedPersonInfo>
</relatedPersonsList>
<offeringData><offeringSalesAmounts><totalOfferingAmount>50000000</totalOfferingAmount>
<totalAmountSold>25000000</totalAmountSold></offeringSalesAmounts>
<typesOfSecuritiesOffered><isEquityType>true</isEquityType></typesOfSecuritiesOffered>
<dateOfFirstSale><value>2026-03-01</value></dateOfFirstSale></offeringData></edgarSubmission>"""
FORM_D_HITS = {
    "hits": {
        "total": {"value": 1},
        "hits": [
            {
                "_id": f"{ACCESSION}:primary_doc.xml",
                "_source": {
                    "ciks": ["1234567"],
                    "file_date": "2026-03-05",
                    "form_type": "D",
                    "display_names": [f"Acme Orbital Inc.  (CIK {ACME_CIK})"],
                    "file_description": "Form D",
                },
            }
        ],
    }
}

REVENUE_PAGE = """# Acme Orbital posts record revenue

Published March 3, 2026

Acme Orbital generated $412 million in revenue in 2025, up from $250 million in 2024, the company said. The company raised $300 million in a Series C round on March 3, 2026, at a post-money valuation of $4.5 billion. Acme Orbital expects revenue of $900 million in 2027. The company employs 1,250 people. Acme Orbital is in talks to raise another $1 billion.
"""
LISTICLE_PAGE = """# Space unicorns to watch

Published February 2, 2026

SpaceCo generated $15 billion in revenue in 2025. Rocket Dynamics raised $200 million at a $2 billion valuation on January 5, 2026.
"""
SERIES_C_PAGE = """# Acme Orbital raises $300 million Series C

Published March 3, 2026

Acme Orbital raised $300 million in a Series C round on March 3, 2026, at a post-money valuation of $4.5 billion, according to the announcement.
"""
CREDIT_PAGE = """# Acme Orbital secures credit facility

Published January 14, 2026

Acme Orbital secured a $75 million revolving credit facility on January 14, 2026.
"""
LEADERS_PAGE = """# Acme Orbital leadership

Acme Orbital is led by chief executive officer Dana Reyes, who co-founded the company in 2019.
"""


@pytest.fixture
def private_edgar(fixture_json):
    """EDGAR as seen from a private company: the ticker file, the Form D full-text search and
    the Form D document. Any data.sec.gov request (submissions, companyfacts) is recorded in
    `blocked` and answered 500 so a test can assert it never happens."""
    state = SimpleNamespace(efts_status=200, hits=FORM_D_HITS)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=Response(200, json=fixture_json("company_tickers_exchange.json"))
        )

        def efts(request):
            if state.efts_status != 200:
                return Response(state.efts_status, text="forbidden")
            return Response(200, json=state.hits)

        router.get(url__regex=r"https://efts\.sec\.gov/LATEST/search-index.*").mock(
            side_effect=efts
        )
        router.get(
            "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000042/primary_doc.xml"
        ).mock(return_value=Response(200, text=FORM_D_XML))
        blocked = router.get(url__regex=r"https://data\.sec\.gov/.*").mock(
            return_value=Response(500)
        )
        yield SimpleNamespace(router=router, blocked=blocked, state=state)


def _acme_firecrawl(firecrawl_mock):
    return firecrawl_mock(
        search={
            "annual revenue": [
                _hit(
                    "https://spacenews.com/acme-orbital-revenue",
                    "Acme Orbital posts record revenue",
                    "Acme Orbital generated $412 million in revenue in 2025",
                ),
                _hit("https://news.example.com/space-unicorns", "Space unicorns to watch"),
            ],
            "funding round valuation": [
                _hit(
                    "https://techcrunch.com/2026/03/03/acme-orbital-series-c",
                    "Acme Orbital raises $300 million Series C",
                    "Acme Orbital raised $300 million at a post-money valuation of $4.5 billion",
                )
            ],
            "debt financing": [
                _hit(
                    "https://www.businesswire.com/news/acme-orbital-credit-facility",
                    "Acme Orbital secures credit facility",
                    "a $75 million revolving credit facility",
                )
            ],
            "chief executive": [
                _hit("https://spacenews.com/acme-orbital-leadership", "Acme Orbital leadership")
            ],
            "official website": [
                _hit("https://www.acmeorbital.com/", "Acme Orbital", "Satellites"),
                _hit("https://en.wikipedia.org/wiki/Acme", "Acme"),
            ],
        },
        pages={
            "acme-orbital-revenue": REVENUE_PAGE,
            "space-unicorns": LISTICLE_PAGE,
            "acme-orbital-series-c": SERIES_C_PAGE,
            "acme-orbital-credit-facility": CREDIT_PAGE,
            "acme-orbital-leadership": LEADERS_PAGE,
        },
        maps={
            "acmeorbital.com": ["https://www.acmeorbital.com/news/2026/03/03/series-c-announced"]
        },
    )


def _stage(inputs: Inputs):
    run_id, run_dir = new_run(inputs)
    st = {"inputs": inputs.model_dump(mode="json"), "run_id": run_id, "run_dir": str(run_dir)}
    for node in (intake, resolve_entity, foundation_pack, plan_sections):
        st.update(node(st))
    return st


def _section(st, n):
    out = build_researcher().invoke(
        {
            "run_dir": st["run_dir"],
            "as_of": st["as_of"],
            "entity": st["entity"],
            "foundation": st["foundation"],
            "brief": st["plan"][n],
            "iteration": 0,
            "critic_notes": [],
        }
    )
    return out["drafts"][n]


def _run_json(run) -> dict:
    return json.loads((Path(run["run_dir"]) / "run.json").read_text(encoding="utf-8"))


# ---- entity resolution --------------------------------------------------------------------


def test_name_with_no_sec_match_resolves_to_a_private_entity(private_edgar, output_dir):
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[9]))
    ent = Entity.model_validate(st["entity"])
    assert ent.private and not ent.is_public and ent.cik is None and ent.ticker is None
    assert ent.sector_tag == "default" and ent.aliases == ["Acme Orbital"]
    assert private_edgar.blocked.call_count == 0
    meta = (
        json.loads((Path(st["run_dir"]) / "run.json").read_text(encoding="utf-8"))
        if (Path(st["run_dir"]) / "run.json").exists()
        else None
    )
    assert meta is None or meta["meta"]["entity"]["private"] is True


def test_private_switch_overrides_an_sec_match_and_records_it(edgar_mock, output_dir):
    st = _stage(
        Inputs(
            company_name="Lockheed Martin Corporation",
            ticker="LMT",
            private=True,
            as_of_date=AS_OF,
            sections=[9],
        )
    )
    ent = Entity.model_validate(st["entity"])
    assert ent.private and not ent.is_public and ent.cik is None and ent.ticker is None
    from primer.runlog import get_runlog

    pp = get_runlog(st["run_dir"]).meta["private_path"]
    assert pp["reason"] == "--private" and pp["ticker_ignored"] == "LMT"
    assert pp["sec_match_ignored"]["cik"] == LMT_CIK
    urls = {str(c.request.url) for c in edgar_mock.calls}
    assert not [u for u in urls if "submissions" in u or "companyfacts" in u or "Archives" in u]


# ---- Form D discovery -------------------------------------------------------------------------


def test_form_d_filings_enter_the_foundation_pack(private_edgar, firecrawl_mock, output_dir):
    _acme_firecrawl(firecrawl_mock)
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[11]))
    priv = st["foundation"]["private"]
    fd = priv["form_d"]
    assert fd["source"] == "edgar_fulltext" and fd["failures"] == 0
    f = fd["filings"][0]
    assert f["total_offering"] == 50_000_000 and f["total_sold"] == 25_000_000
    assert f["first_sale"] == "2026-03-01" and f["related"][0]["name"] == "Dana Reyes"
    store = EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)
    rec = store.get(f["source_id"])
    assert rec.tier == 1 and rec.type == "Form D" and rec.gov_id == ACCESSION
    assert st["foundation"]["sources"]["Form D"] == [f["source_id"]]
    # no 10-K, 10-Q or proxy; the company site came from the search results
    assert st["foundation"]["filings"] == {} and st["foundation"]["quarters"] == []
    assert st["foundation"]["ir"]["site"] == "www.acmeorbital.com"
    assert st["foundation"]["ir"]["links"]["news"]
    assert private_edgar.blocked.call_count == 0


def test_form_d_falls_back_to_a_sec_gov_search_when_edgar_returns_403(
    private_edgar, firecrawl_mock, output_dir
):
    private_edgar.state.efts_status = 403
    sec_url = "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000042/xslFormDX01/primary_doc.xml"
    fc = firecrawl_mock(
        search={
            "Form D": [
                _hit(sec_url, "Form D - Acme Orbital Inc."),
                _hit("https://www.crunchbase.com/acme-orbital", "Acme Orbital funding"),
            ]
        },
        pages={
            "xslFormDX01": "# Form D\n\nIssuer: Acme Orbital Inc.\n\nTotal Offering Amount | $50,000,000\n\nTotal Amount Sold | $25,000,000\n",
            "crunchbase": "# Acme Orbital funding\n\nAcme Orbital Inc. raised money.",
        },
    )
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[11]))
    fd = st["foundation"]["private"]["form_d"]
    assert fd["failures"] >= 1 and fd["source"] == "firecrawl_search"
    assert any('"Acme Orbital Inc" "Form D" site:sec.gov' in q for q in fd["queries"])
    store = EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)
    forms = [r for r in store.records() if r.type == "Form D"]
    assert len(forms) == 1 and forms[0].tier == 1 and "sec.gov" in forms[0].url
    assert fd["filings"][0]["total_sold"] == 25_000_000
    # the non-sec.gov hit is never registered as a tier-1 Form D source
    assert all("crunchbase" not in r.url for r in forms)
    assert ("scrape", "https://www.crunchbase.com/acme-orbital") not in fc.calls
    from primer.runlog import get_runlog

    assert get_runlog(st["run_dir"]).counters["private_form_d_failures"] >= 1


def test_form_d_failure_is_a_coverage_note_not_an_error(private_edgar, output_dir):
    private_edgar.state.efts_status = 403  # no Firecrawl key either
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[11]))
    fd = st["foundation"]["private"]["form_d"]
    assert fd["filings"] == [] and fd["failures"] == 1
    assert any("refused or failed" in n for n in fd["notes"])
    d = _section(st, 11)
    assert d["coverage"] == "unavailable"
    assert "1 failed lookup" in d["markdown"]


# ---- extraction rules -------------------------------------------------------------------------


NAMES = pv.company_names(Entity(name=ACME, aliases=["Acme Orbital"]))
ALL_KINDS = tuple(pv.KIND_NAMES) + ("net_income",)


def _extract(text, published=date(2026, 3, 3), title="", kinds=ALL_KINDS):
    return pv.extract_datapoints(
        text, "src_0001", kinds=kinds, names=NAMES, published=published, title=title, as_of=AS_OF
    )


def test_extraction_reads_dated_figures_and_skips_plans_and_other_companies():
    dps = _extract(REVENUE_PAGE)
    got = {(d.kind, d.value, d.period) for d in dps}
    assert ("revenue", 412e6, "FY2025") in got and ("revenue", 250e6, "FY2024") in got
    assert ("funding", 300e6, "2026-03-03") in got and ("valuation", 4.5e9, "2026-03-03") in got
    assert ("headcount", 1250.0, "2026-03-03") in got
    # "expects revenue of $900 million in 2027" and "in talks to raise $1 billion" are not facts
    assert not [d for d in dps if d.value in (900e6, 1e9)]
    # a page about other companies yields nothing for this one
    assert _extract(LISTICLE_PAGE) == []


def test_extraction_labels_and_basis():
    dps = _extract(
        "Acme Orbital reported a net loss of $40 million for FY2025. Acme Orbital's revenue "
        "was estimated at $300 million in 2025. Acme Orbital closed a $5 million term loan on "
        "May 5, 2026. Acme Orbital's run rate is $500 million annualized.",
    )
    by = {d.kind: d for d in dps}
    assert by["net_income"].value == -40e6 and by["net_income"].label == "Net loss"
    assert by["revenue"].basis == "estimated" and by["revenue"].period == "FY2025"
    assert by["debt"].label == "Debt financing (term loan)" and by["debt"].period == "2026-05-05"
    assert by["run_rate"].metric == "press_revenue_run_rate"
    assert all(d.metric.startswith("press_") for d in dps)


def test_a_number_with_no_date_anywhere_is_never_emitted():
    assert _extract("Acme Orbital raised $20 million.", published=None) == []


def test_ranges_and_bare_numbers_are_skipped():
    assert _extract("Acme Orbital raised between $20 million and $30 million in 2025.") == []
    assert _extract("Acme Orbital sold units at $450 each in 2025 revenue.") == []


def test_model_datapoints_must_quote_the_page(monkeypatch):
    good = pv._ModelItem(
        kind="revenue",
        value_usd=412e6,
        period="FY2025",
        source_id="src_0001",
        quote="Acme Orbital generated $412 million in revenue in 2025, up from $250 million in 2024, the company said.",
    )
    invented = good.model_copy(update={"value_usd": 999e6})
    paraphrase = good.model_copy(
        update={"quote": "Acme Orbital made about $412 million last year."}
    )
    forward = good.model_copy(
        update={"quote": "Acme Orbital expects to generate $412 million in revenue in 2027."}
    )
    badperiod = good.model_copy(update={"period": "last year"})
    kinds = {"revenue"}
    for item, ok in (
        (good, True),
        (invented, False),
        (paraphrase, False),
        (forward, False),
        (badperiod, False),
    ):
        assert pv.verify_model_item(item, REVENUE_PAGE, NAMES, kinds) is ok, item


def test_model_assisted_extraction_keeps_only_verified_items(
    private_edgar, firecrawl_mock, output_dir, monkeypatch
):
    _acme_firecrawl(firecrawl_mock)
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[9]))
    from primer.graph.researcher import build_context

    ctx = build_context(
        {
            "run_dir": st["run_dir"],
            "as_of": st["as_of"],
            "entity": st["entity"],
            "foundation": st["foundation"],
            "brief": st["plan"][9],
        },
        9,
    )
    store = ctx.store
    sid = store.register(
        REVENUE_PAGE,
        type="news",
        publisher="spacenews.com",
        title="Acme Orbital posts record revenue",
        url="https://spacenews.com/model-test",
        published_date=date(2026, 3, 3),
    ).source_id
    items = pv._ModelItems(
        items=[
            pv._ModelItem(
                kind="revenue",
                value_usd=412e6,
                period="FY2025",
                source_id=sid,
                quote="Acme Orbital generated $412 million in revenue in 2025, up from $250 million in 2024, the company said.",
            ),
            pv._ModelItem(
                kind="revenue",
                value_usd=777e6,
                period="FY2025",
                source_id=sid,
                quote="Acme Orbital generated $777 million in revenue in 2025.",
            ),
        ]
    )
    monkeypatch.setattr(pv, "llm_available", lambda: True)
    monkeypatch.setattr(pv, "invoke_structured", lambda *a, **k: items)
    out = pv.model_datapoints(ctx, [sid], ("revenue",))
    assert [(d.value, d.method) for d in out] == [(412e6, "model")]


# ---- the tier rule ---------------------------------------------------------------------------


def test_tier_rule_exempts_press_metrics_but_not_statement_metrics(output_dir):
    store = EvidenceStore.for_run(output_dir / "t", as_of=AS_OF)
    web = store.register(
        "x", type="news", publisher="p", title="t", url="https://news.example.com/a", tier=2
    ).source_id
    claim = lambda metric, sids: {  # noqa: E731
        "metric": metric, "period": "FY2025", "section": 9, "source_ids": sids, "value": 1.0,
    }  # fmt: skip
    ok = check_tier([claim("press_revenue", [web])], store)
    assert ok["ok"] and not ok["flagged"] and not ok["press_unsourced"]
    # a statement metric cited only to a tier-2 page is still flagged
    bad = check_tier([claim("revenue", [web])], store)
    assert not bad["ok"] and bad["flagged"][0]["metric"] == "revenue"
    # a press claim must still cite a fetched source
    for sids in ([], ["src_9999"]):
        res = check_tier([claim("press_revenue", sids)], store)
        assert not res["ok"] and res["press_unsourced"][0]["metric"] == "press_revenue"


# ---- sections 9, 11, 13 --------------------------------------------------------------------------


def test_sections_9_11_13_are_partial_with_cited_press_figures(
    private_edgar, firecrawl_mock, usaspending_mock, output_dir
):
    _acme_firecrawl(firecrawl_mock)
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[9, 11, 13]))
    d9, d11, d13 = (_section(st, n) for n in (9, 11, 13))
    for d in (d9, d11, d13):
        assert d["coverage"] == "partial", d["markdown"]
        assert d["exhibit_present"]
        assert d["claims"] and all(c["source_ids"] for c in d["claims"])
        assert all(c["metric"].startswith(("press_", "form_d_")) for c in d["claims"])
        last = d["markdown"].strip().splitlines()[-1]
        assert last.startswith("> Coverage: partial — found: ")
        assert "not found" in last and "no figure is audited" in last
    assert "### Press-reported figures (unaudited)" in d9["markdown"]
    assert "### Press-reported figures (unaudited)" in d11["markdown"]
    assert "### Funding rounds and valuation" in d13["markdown"]
    # section 9 carries the revenue rows with their source tier
    metrics9 = {(c["metric"], c["period"], c["value"]) for c in d9["claims"]}
    assert ("press_revenue", "FY2025", 412e6) in metrics9
    assert "2 (press / open web)" in d9["markdown"]
    # section 11: the credit facility and the Form D notice; section 13: the round and valuation
    assert any(c["metric"] == "press_debt_financing" for c in d11["claims"])
    assert any(c["metric"] == "form_d_amount_sold" for c in d11["claims"])
    assert "1 (SEC filing, issuer-filed)" in d11["markdown"]
    assert any(c["metric"] == "press_valuation" and c["value"] == 4.5e9 for c in d13["claims"])
    assert "$4.5 billion" in d13["markdown"] and "$300 million" in d13["markdown"]
    # nothing derived: no multiple, no net debt, no price target, no consensus (the caveat and
    # coverage lines say so; the table and narrative never contain one)
    for d in (d9, d11, d13):
        body = "\n".join(
            ln for ln in d["markdown"].splitlines() if not ln.startswith(("*Caveat", ">"))
        ).lower()
        for word in ("ev/ebitda", "p/e", "net debt", "price target", "consensus", "multiple"):
            assert word not in body, (word, body)
        assert not any(
            c["metric"] in ("net_debt", "ev_ebitda", "enterprise_value", "market_cap", "pe_ratio")
            for c in d["claims"]
        )
    # every table row cites a source token
    for md in (d9["markdown"], d11["markdown"], d13["markdown"]):
        rows = [ln for ln in md.splitlines() if ln.startswith("| ") and "---" not in ln][1:]
        assert rows and all("[src_" in r for r in rows)


def test_sections_are_unavailable_with_a_one_line_blockquote_when_nothing_is_found(
    private_edgar, firecrawl_mock, output_dir
):
    firecrawl_mock(search={}, pages={})
    private_edgar.state.hits = {"hits": {"total": {"value": 0}, "hits": []}}  # no Form D either
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[9, 11, 13]))
    for n in (9, 11, 13):
        d = _section(st, n)
        assert d["coverage"] == "unavailable" and d["claims"] == [] and not d["exhibit_present"]
        lines = [ln for ln in d["markdown"].splitlines() if ln.strip()]
        assert len(lines) == 1 and lines[0].startswith("> Coverage: unavailable — not found: ")
        assert "no SEC financial statements exist for a private company" in lines[0]


def test_other_companies_numbers_are_not_attributed(private_edgar, firecrawl_mock, output_dir):
    firecrawl_mock(
        search={
            "annual revenue": [_hit("https://news.example.com/space-unicorns", "Space unicorns")]
        },
        pages={"space-unicorns": LISTICLE_PAGE},
    )
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[9]))
    assert _section(st, 9)["coverage"] == "unavailable"


# ---- section 4 and 8 ----------------------------------------------------------------------------


def test_section_4_is_not_applicable_without_prime_awards(
    private_edgar, firecrawl_mock, usaspending_mock, output_dir
):
    firecrawl_mock(search={}, pages={})
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[4]))
    d = _section(st, 4)
    assert d["coverage"] == "not_applicable"
    assert "no prime awards" in d["markdown"]


def test_section_8_names_the_missing_proxy_and_lists_form_d_people(
    private_edgar, firecrawl_mock, output_dir
):
    _acme_firecrawl(firecrawl_mock)
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[8]))
    d = _section(st, 8)
    assert d["coverage"] == "partial"
    assert "no proxy statement or 10-K exists for a private company" in d["markdown"]
    assert "Dana Reyes" in d["markdown"] and "Executive Officer, Director" in d["markdown"]


# ---- whole runs ---------------------------------------------------------------------------------


def _assert_honest_gate(run):
    v = run["validation"]
    ch = v["checks"]
    assert ch["citations"]["ok"] and ch["tokens"]["ok"], ch["citations"]
    assert ch["tier"]["ok"] and not ch["tier"]["press_unsourced"]
    assert ch["numeric"]["ok"] and ch["coverage"]["ok"], ch["coverage"]
    assert v["passed"] is True, {k: c for k, c in ch.items() if c.get("ok") is False}


def test_full_private_run_on_acme_passes_the_gate(
    private_edgar, firecrawl_mock, usaspending_mock, output_dir
):
    _acme_firecrawl(firecrawl_mock)
    inputs = Inputs(company_name=ACME, as_of_date=AS_OF, sections=list(range(1, 15)))
    run = run_primer(inputs)
    run_dir = Path(run["run_dir"])
    assert private_edgar.blocked.call_count == 0  # no submissions, no companyfacts

    md = (run_dir / "primer.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(md.split("---")[1])
    assert fm["is_public"] is False and fm["cik"] is None and fm["ticker"] is None
    assert (
        "Private company: no SEC financial statements; financial figures are press-reported and unaudited"
        in fm["source_limits"]
    )
    assert len(fm["source_limits"]) == 3
    for sec in ("9", "11", "13"):
        assert fm["coverage"][sec] in ("partial", "unavailable")
    assert fm["coverage"]["4"] == "not_applicable"
    assert (
        "## 9. Historical Financial Performance" in md
        and "Press-reported figures (unaudited)" in md
    )
    assert "[src_" not in md

    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    assert claims and all(c["source_ids"] for c in claims)
    fin = [c for c in claims if c["section"] in (9, 11, 13)]
    assert fin and all(c["metric"].startswith(("press_", "form_d_")) for c in fin)

    sources = json.loads((run_dir / "sources.json").read_text(encoding="utf-8"))["sources"]
    assert not [s for s in sources if s["type"] == "api:edgar_xbrl"]
    assert [s for s in sources if s["type"] == "Form D" and s["tier"] == 1]

    meta = _run_json(run)["meta"]
    assert meta["entity"]["private"] is True and meta["private_path"]["sec_match_ignored"] is None
    _assert_honest_gate(run)


def test_private_switch_on_lmt_skips_every_sec_filing_call(
    edgar_mock, firecrawl_mock, usaspending_mock, output_dir
):
    fc = firecrawl_mock(
        search={
            "annual revenue": [
                _hit(
                    "https://news.example.com/lockheed-revenue",
                    "Lockheed Martin revenue",
                    "Lockheed Martin generated $75 billion in revenue in 2025",
                )
            ],
            "funding round valuation": [],
        },
        pages={
            "lockheed-revenue": "# Lockheed Martin revenue\n\nPublished January 28, 2026\n\nLockheed Martin generated $75 billion in revenue in 2025, the company said.",
        },
    )
    inputs = Inputs(
        company_name="Lockheed Martin Corporation",
        ticker="LMT",
        private=True,
        as_of_date=AS_OF,
        sections=[1, 4, 8, 9, 10, 11, 13],
    )
    run = run_primer(inputs)
    run_dir = Path(run["run_dir"])
    urls = [str(c.request.url) for c in edgar_mock.calls]
    assert not [u for u in urls if "submissions" in u or "companyfacts" in u or "Archives" in u]
    meta = _run_json(run)["meta"]
    assert meta["entity"]["is_public"] is False and meta["entity"]["cik"] is None
    assert meta["private_path"]["sec_match_ignored"]["cik"] == LMT_CIK
    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    assert not [c for c in claims if c["metric"] in ("revenue", "net_debt", "ev_ebitda")]
    assert any(c["metric"] == "press_revenue" and c["value"] == 75e9 for c in claims)
    # section 4 is emitted: USAspending shows prime awards to the resolved recipient
    assert run["coverage"][4] in ("full", "partial")
    for n in (9, 11, 13):
        assert run["coverage"][n] in ("partial", "unavailable")
    md = (run_dir / "primer.md").read_text(encoding="utf-8")
    assert "is_public: false" in md and "cik: null" in md
    _assert_honest_gate(run)
    assert fc.calls


def test_public_runs_do_not_carry_the_private_source_limit(edgar_mock, output_dir):
    run = run_primer(
        Inputs(company_name="Lockheed Martin", ticker="LMT", as_of_date=AS_OF, sections=[1, 9])
    )
    md = (Path(run["run_dir"]) / "primer.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(md.split("---")[1])
    assert fm["is_public"] is True and len(fm["source_limits"]) == 2
    assert not any("Private company" in s for s in fm["source_limits"])


def test_render_adds_the_source_limit_only_for_private_entities(output_dir):
    store = EvidenceStore.for_run(output_dir / "r", as_of=AS_OF)
    for private, n in ((True, 3), (False, 2)):
        md, _, _ = render(
            entity={
                "name": "X",
                "ticker": None,
                "cik": None,
                "is_public": False,
                "private": private,
            },
            as_of=AS_OF.isoformat(),
            run_id="r",
            sections_md={2: "text"},
            coverage={2: "partial"},
            claims=[],
            store=store,
            requested=[2],
        )
        assert len(yaml.safe_load(md.split("---")[1])["source_limits"]) == n
    assert EdgarClient  # imported for the fixtures' side effects only


# ---- section 10 and the web-only helper ----------------------------------------------------------


def test_section_10_keeps_news_when_no_figure_is_found(private_edgar, firecrawl_mock, output_dir):
    firecrawl_mock(
        search={
            "backlog contracts customers": [
                _hit("https://spacenews.com/acme-orbital-factory", "Acme Orbital opens a factory")
            ]
        },
        pages={
            "acme-orbital-factory": "# Acme Orbital opens a factory\n\nPublished April 2, 2026\n\nAcme Orbital opened a satellite factory in Denver."
        },
    )
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[10]))
    d = _section(st, 10)
    assert d["coverage"] == "partial" and not d["exhibit_present"] and d["claims"] == []
    assert "[src_" in d["markdown"] and "not found: " in d["markdown"]
    assert "no SEC financial statements exist" in d["markdown"]


def test_queries_beyond_the_section_search_cap_are_not_run(
    private_edgar, firecrawl_mock, output_dir
):
    fc = _acme_firecrawl(firecrawl_mock)
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[9]))
    from primer.config import section_budget

    cap = int(section_budget(9)["searches"])
    before = [c for c in fc.calls if c[0] == "search"]
    d = _section(st, 9)
    searches = [c for c in fc.calls if c[0] == "search"][len(before) :]
    assert 1 <= len(searches) <= cap  # never more than the section's configured search cap
    assert "annual revenue" in searches[0][1]  # the first-priority query runs first
    assert "section budget" not in d["markdown"]


def test_web_only_draft_for_sections_that_normally_read_the_10k(
    private_edgar, firecrawl_mock, output_dir
):
    from primer.graph.researcher import build_context

    firecrawl_mock(search={}, pages={})
    st = _stage(Inputs(company_name=ACME, as_of_date=AS_OF, sections=[2]))
    ctx = build_context(
        {
            "run_dir": st["run_dir"],
            "as_of": st["as_of"],
            "entity": st["entity"],
            "foundation": st["foundation"],
            "brief": st["plan"][2],
        },
        2,
    )
    brief = ctx.brief
    empty = pv.draft_web_only(brief, ctx, {"web": []}, missing="10-K")
    assert empty.coverage == "unavailable" and "no page was retrieved" in empty.markdown
    sid = ctx.store.register(
        REVENUE_PAGE,
        type="news",
        publisher="spacenews.com",
        title="Acme Orbital posts record revenue",
        url="https://spacenews.com/web-only",
        published_date=date(2026, 3, 3),
    ).source_id
    out = pv.draft_web_only(brief, ctx, {"web": [{"source_id": sid}]}, missing="10-K")
    assert out.coverage == "partial" and not out.exhibit_present and out.claims == []
    assert f"[{sid}]" in out.markdown
    assert "no 10-K exists for this private company" in out.markdown
    assert "> Coverage: partial — private company: no 10-K exists" in out.markdown


def test_alias_option_reaches_the_inputs_and_the_entity(monkeypatch, tmp_path):
    """A private company is known to filings and USAspending by its legal name and to people by
    another (Space Exploration Technologies Corp / SpaceX): both must reach the run."""
    from typer.testing import CliRunner

    import primer.graph.main as gm
    from primer.cli import app

    seen = {}

    def fake_run(inputs, run_id=None, run_dir=None, on_interrupt=None):
        seen["inputs"] = inputs
        return {"run_id": "x", "run_dir": str(tmp_path), "md_path": None}

    monkeypatch.setattr(gm, "run_primer", fake_run)
    res = CliRunner().invoke(
        app,
        [
            "run",
            "--company",
            "Space Exploration Technologies Corp",
            "--private",
            "--alias",
            "SpaceX",
            "--alias",
            "Space X",
            "--sections",
            "1",
        ],
    )
    assert res.exit_code == 0, res.output
    assert seen["inputs"].aliases == ["SpaceX", "Space X"] and seen["inputs"].private


def test_one_source_listing_several_rounds_is_not_a_numeric_conflict():
    """A funding-history article dates every round by its own date, so one page stated $12bn, $127bn
    and $350bn for the same (metric, period). Sections 11 and 13 then printed a 10-value "Conflict"
    block (SpaceX private run, 2026-10-21). Two different sources disagreeing still is one."""
    from primer.validate.numeric import check_numeric

    def claim(section, value, sid, metric="press_valuation"):
        return {
            "section": section,
            "metric": metric,
            "value": value,
            "period": "2025-07-31",
            "source_ids": [sid],
        }

    one_page = [claim(s, v, "src_0024") for s in (11, 13) for v in (12e9, 127e9, 350e9, 400e9)]
    assert check_numeric(one_page)["ok"]

    two_pages = [*one_page, claim(13, 410e9, "src_0050")]
    rep = check_numeric(two_pages)
    assert not rep["ok"] and len(rep["conflicts"]) == 1
    vals = {v["value"] for v in rep["conflicts"][0]["values"]}
    # only comparable figures from different sources clash; the $12bn 2015 round does not
    assert {410e9, 400e9} <= vals and not ({12e9, 127e9} & vals)

    # a statement metric from one source still conflicts: that is a real inconsistency
    xbrl = [
        {**claim(9, 1e9, "src_0001", "revenue"), "period": "FY2025"},
        {**claim(10, 2e9, "src_0001", "revenue"), "period": "FY2025"},
    ]
    assert not check_numeric(xbrl)["ok"]


def test_a_conflict_note_lists_each_figure_once():
    from primer.graph.main import inject_conflicts

    claims = [
        {"section": s, "metric": "press_valuation", "value": v, "unit": "USD",
         "period": "2025-07-31", "source_ids": [sid]}
        for s in (11, 13)
        for v, sid in ((400e9, "src_0024"), (410e9, "src_0050"))
    ]  # fmt: skip
    out = inject_conflicts({11: "x", 13: "y"}, claims)
    line = next(ln for ln in out[13].splitlines() if ln.startswith("> Conflict"))
    assert line.count("src_0024") == 1 and line.count("src_0050") == 1
