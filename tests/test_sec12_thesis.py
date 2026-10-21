"""Section 12 handler on the fixtures: a Q2 2026 transcript naming Melius Research, Morgan
Stanley and Bernstein; a MarketBeat ratings page (JSON + page text); a news page with a
Bernstein downgrade. The attribution validator must pass on the draft."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import HANDLERS, build_researcher
from primer.sections.exhibits_sec12 import classify_rating, page_points, parse_qa
from primer.state import Inputs, SectionDraft
from primer.validate.rules import check_attribution

from .conftest import _hit

WEB = Path(__file__).parent / "fixtures" / "web"
AS_OF = date(2026, 10, 21)
TRANSCRIPT_URL = "https://www.fool.com/earnings/call-transcripts/2026/07/23/lockheed-martin-lmt-q2-2026-earnings-call-transcript/"

MARKETBEAT_JSON = {
    "consensus_rating": "Moderate Buy",
    "consensus_price_target": 540.0,
    "high_price_target": 600.0,
    "low_price_target": 470.0,
    "number_of_ratings": 18,
    "ratings": [
        {
            "date": "6/12/2026",
            "brokerage": "Morgan Stanley",
            "action": "Upgrade",
            "rating": "Overweight",
            "prior_rating": "Equal Weight",
            "price_target": 560.0,
            "prior_price_target": 510.0,
        },
        {
            "date": "4/24/2026",
            "brokerage": "RBC Capital",
            "action": "Boost Target",
            "rating": "Outperform",
            "price_target": 575.0,
            "prior_price_target": 550.0,
        },
        {
            "date": "1/30/2026",
            "brokerage": "Bernstein",
            "action": "Downgrade",
            "rating": "Market Perform",
            "prior_rating": "Outperform",
            "price_target": 480.0,
            "prior_price_target": 520.0,
        },
        {
            "date": "11/5/2025",
            "brokerage": "Phantom Capital",
            "action": "Upgrade",
            "rating": "Buy",
            "price_target": 999.0,
        },
        {
            "date": "3/1/2024",
            "brokerage": "Old House",
            "action": "Downgrade",
            "rating": "Sell",
            "price_target": 300.0,
        },
    ],
}
TIPRANKS_JSON = {
    "average_price_target": 545.0,
    "number_of_analysts": 12,
    "analysts": [
        {"name": "K. Liwag", "firm": "Morgan Stanley", "rating": "Buy", "price_target": 560.0}
    ],
}


def _state(sections: list[int]):
    inputs = Inputs(
        company_name="Lockheed Martin", ticker="LMT", as_of_date=AS_OF, sections=sections
    )
    run_id, run_dir = new_run(inputs)
    st = {"inputs": inputs.model_dump(mode="json"), "run_id": run_id, "run_dir": str(run_dir)}
    for node in (intake, resolve_entity, foundation_pack, plan_sections):
        st.update(node(st))
    return st


def _run(st, section: int) -> tuple[SectionDraft, EvidenceStore]:
    HANDLERS.pop(section, None)  # another test may have cached a "not implemented" stub
    out = build_researcher().invoke(
        {
            "run_dir": st["run_dir"],
            "as_of": st["as_of"],
            "entity": st["entity"],
            "foundation": st["foundation"],
            "brief": st["plan"][section],
            "iteration": 0,
            "critic_notes": [],
        }
    )
    return SectionDraft.model_validate(out["drafts"][section]), EvidenceStore.for_run(
        st["run_dir"], as_of=AS_OF
    )


def _common_checks(draft: SectionDraft, store: EvidenceStore, section: int) -> None:
    for c in draft.claims:
        assert c.section == section and c.source_ids and all(store.has(s) for s in c.source_ids), c
    for sid in set(re.findall(r"\[(src_\d{4})\]", draft.markdown)):
        assert store.has(sid), sid
    b = section_budget(section)
    used_s, cap_s = draft.budget["searches"].split("/")
    used_p, cap_p = draft.budget["scrapes"].split("/")
    assert int(used_s) <= int(cap_s) == int(b["searches"])
    assert int(used_p) <= int(cap_p) == int(b["scrapes"])


def test_parse_qa_inline_and_fool_formats():
    inline = (WEB / "lmt_q2_2026_transcript.md").read_text(encoding="utf-8")
    d = parse_qa(inline, "Q2-2026", "src_0009")
    assert [(x.firm, x.analyst) for x in d] == [
        ("Melius Research", "Scott Micas"),
        ("Morgan Stanley", "Kristine Liwag"),
        ("Bernstein", "Douglas Harned"),
    ]
    assert d[0].question.startswith("Can you talk about the F-35 Lot 20")
    assert d[1].question.startswith("On PAC-3 capacity") and d[1].question.endswith("?")
    fool = (
        "**Operator**\n\nOur first question comes from the line of Jane Doe with Acme Securities. Please proceed.\n\n"
        "**Jane Doe** -- *Acme Securities -- Analyst*\n\nThanks. Good morning. How should we think about backlog conversion next year?\n\n"
        "**Jim Taiclet** -- *Chairman and CEO*\n\nGood question, Jane.\n"
    )
    f = parse_qa(fool, "Q1-2026", "src_0010")
    assert (
        len(f) == 1
        and f[0].firm == "Acme Securities"
        and f[0].question.startswith("How should we think")
    )
    assert (
        classify_rating("Upgrade", "Overweight") == "bull"
        and classify_rating("Downgrade", None) == "bear"
    )
    assert classify_rating("Reiterated", "Hold") == "open"


def test_page_points_name_the_firm_only_from_the_page():
    text = (WEB / "lmt_news_analyst.md").read_text(encoding="utf-8")
    pts = page_points(
        text, "src_0020", date(2026, 9, 3), ["Lockheed Martin Corp", "Lockheed Martin"]
    )
    sides = {(p.firm, p.side) for p in pts}
    assert ("Bernstein", "bear") in sides and ("Morgan Stanley", "bull") in sides
    assert all(p.firm.lower() in text.lower() for p in pts)
    assert not any("Lockheed" in (p.firm or "") for p in pts)


def test_section12_with_firecrawl(edgar_mock, firecrawl_mock, output_dir):
    fc = firecrawl_mock(
        search={
            "Q2 2026 earnings call transcript": [
                _hit(TRANSCRIPT_URL, "Lockheed Martin (LMT) Q2 2026 Earnings Call Transcript")
            ],
            "bear case": [
                _hit(
                    "https://www.defensenews.com/industry/2026/09/03/lockheed-bernstein/",
                    "Lockheed shares slip after Bernstein downgrade",
                )
            ],
            "upgrade OR downgrade": [
                _hit(
                    "https://www.defensenews.com/industry/2026/09/03/lockheed-bernstein/",
                    "Lockheed shares slip after Bernstein downgrade",
                )
            ],
        },
        pages={
            "fool.com": (WEB / "lmt_q2_2026_transcript.md").read_text(encoding="utf-8"),
            "marketbeat.com": (WEB / "lmt_marketbeat_ratings.md").read_text(encoding="utf-8"),
            "tipranks.com": "# LMT forecast\n\nMorgan Stanley analyst Kristine Liwag rates the stock Buy with a $560 target.\n",
            "lockheed-bernstein": (WEB / "lmt_news_analyst.md").read_text(encoding="utf-8"),
        },
        jsons={"marketbeat.com": MARKETBEAT_JSON, "tipranks.com": TIPRANKS_JSON},
    )
    st = _state([12])
    assert st["foundation"]["transcripts"]["found"] == 1
    draft, store = _run(st, 12)
    _common_checks(draft, store, 12)
    assert draft.exhibit_present and draft.coverage == "full"
    md = draft.markdown
    transcript_sid = st["foundation"]["sources"]["transcript"][0]

    # attribution: every firm-attributed cell cites a source whose text names the firm
    assert check_attribution({12: md}, store)["ok"]
    cells = [c for ln in md.splitlines() if ln.startswith("|") for c in ln.split("|")[1:-1]]
    bullets = [ln for ln in md.splitlines() if ln.startswith("- ")]
    for firm in ("Melius Research", "Morgan Stanley", "Bernstein", "RBC Capital"):
        named = [c for c in cells + bullets if firm in c]
        assert named, firm
        for c in named:
            sids = re.findall(r"\[(src_\d{4})\]", c)
            assert sids and any(firm.lower() in store.text(s).lower() for s in sids), c
    # rating actions sit in the table; the open F-35 question is a debate, not a view
    assert any(
        "Morgan Stanley: upgrade to Overweight from Equal Weight target $560 from $510 (2026-06-12, MarketBeat)"
        in c
        for c in cells
    )
    assert any("RBC Capital: boost target" in c and "target $575 from $550" in c for c in cells)
    assert not any("Melius Research" in c for c in cells)
    # unanswered questions carry the firm and the transcript citation
    assert "- Melius Research (Q2-2026 call): Can you talk about the F-35 Lot 20" in md
    assert f"[{transcript_sid}]" in md
    # a MarketBeat row whose firm is absent from the page text is dropped; stale rows filtered by date
    assert "Phantom Capital" not in md and "Old House" not in md
    bear_cells = [
        ln.split("|")[2] for ln in md.splitlines() if ln.startswith("|") and "Bernstein" in ln
    ]
    assert any("Downgrade" in c or "downgraded" in c for c in bear_cells)
    metrics = {c.metric: c for c in draft.claims}
    assert (
        metrics["rating_change_count"].unit == "count"
        and metrics["rating_change_count"].period == "trailing-12m"
    )
    assert metrics["price_target_morgan_stanley"].value == 560.0 and "2026-06-12" in (
        metrics["price_target_morgan_stanley"].note or ""
    )
    assert "price_target_phantom_capital" not in metrics
    assert (
        sum(1 for c in fc.calls if c[0] == "search" and "transcript" not in c[1])
        <= section_budget(12)["searches"]
    )
    assert "No sell-side research" in draft.caveat


def test_section12_keyless_path(edgar_mock, output_dir):
    st = _state([12])
    draft, store = _run(st, 12)
    _common_checks(draft, store, 12)
    md = draft.markdown
    assert draft.coverage == "partial"
    assert "no market-data adapter" in md and "Firecrawl not configured" in md
    assert "no earnings-call transcript" in md
    # the table still stands on company-stated outlook and risk sentences from the 10-K
    assert draft.exhibit_present
    assert "Company outlook (10-K MD&A)" in md and "Company-disclosed risk (10-K Item 1A)" in md
    assert "- no sourced item (no earnings-call transcript was available)" in md
    assert check_attribution({12: md}, store)["ok"]
    assert not any(c.metric.startswith("price_target") for c in draft.claims)
