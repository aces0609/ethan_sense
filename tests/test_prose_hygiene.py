"""Review 2026-10-21 §9 item 8: pipeline vocabulary never reaches prose, and a Perspectives
block with no second-party source is dropped."""

from __future__ import annotations

from datetime import date

from primer.clients.edgar import EdgarClient
from primer.evidence.store import EvidenceStore
from primer.runlog import RunLog
from primer.sections.common import (
    SectionBudget,
    ToolContext,
    caveat_line,
    drop_first_party_perspectives,
    finalize_draft,
    is_first_party_source,
    scrub_pipeline_vocabulary,
)
from primer.sections.style import HOUSE_STYLE
from primer.state import Brief, Entity


def _ctx(tmp_path, section: int = 8):
    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 21))
    runlog = RunLog(tmp_path / "run")
    ctx = ToolContext(
        section=section,
        run_dir=tmp_path / "run",
        as_of=date(2026, 10, 21),
        entity=Entity(name="LOCKHEED MARTIN CORP", cik="0000936468", aliases=["Lockheed Martin"]),
        foundation={"ir": {"site": "https://investors.lockheedmartin.com"}},
        store=store,
        edgar=EdgarClient(runlog=runlog, offline=True),
        runlog=runlog,
        budget=SectionBudget.for_section(section),
    )
    return ctx, store


def _sources(store):
    tenk = store.register(
        "10-K text",
        type="10-K",
        publisher="LOCKHEED MARTIN CORP",
        title="10-K",
        url="https://www.sec.gov/Archives/edgar/data/936468/000162828026004195/lmt.htm",
        tier=1,
    )
    release = store.register(
        "release",
        type="news",
        publisher="www.sec.gov",
        title="lmt-8k",
        url="https://www.sec.gov/Archives/edgar/data/936468/0001/d8k.htm",
        tier=1,
    )
    ir = store.register(
        "ir page",
        type="press_release",
        publisher="Lockheed Martin",
        title="IR",
        url="https://news.lockheedmartin.com/2026-09-15-pac-3",
        tier=1,
    )
    wiki = store.register(
        "wiki",
        type="web",
        publisher="Wikipedia",
        title="Lockheed Martin",
        url="https://en.wikipedia.org/wiki/Lockheed_Martin",
        tier=3,
    )
    call = store.register(
        "transcript",
        type="earnings_call",
        publisher="The Motley Fool",
        title="Q2 call",
        url="https://www.fool.com/earnings/call-transcripts/2026/07/23/lmt-q2.aspx",
        tier=2,
    )
    press = store.register(
        "GAO found",
        type="gov_report",
        publisher="GAO",
        title="GAO-25-107632",
        url="https://www.gao.gov/products/gao-25-107632",
        tier=1,
    )
    peer = store.register(
        "peer 10-K",
        type="10-K",
        publisher="L3HARRIS TECHNOLOGIES, INC. /DE/",
        title="10-K",
        url="https://www.sec.gov/Archives/edgar/data/202058/0001/lhx.htm",
        tier=1,
    )
    return {k: v.source_id for k, v in locals().items() if k != "store"}


def test_scrub_rewrites_pipeline_vocabulary_to_reader_language():
    text = (
        'The exhibit\'s "1 Deliveries" entries should not be read as delivery counts [src_0007]. '
        "The structured extract records a $3.0 billion facility [src_0008]. "
        "No PAC-3 production rate appears in the fetched evidence. "
        "The evidence excerpt does not contain the figures [src_0004]. "
        "Its figures are not in the available excerpt, and no Q1-2026 revision is in evidence."
    )
    out, moved = scrub_pipeline_vocabulary(text)
    assert moved == []
    assert out.startswith('The table\'s "1 Deliveries" entries')
    assert "The source data records a $3.0 billion facility [src_0008]." in out
    assert "No PAC-3 production rate appears in the sources reviewed." in out
    assert "The source text does not contain the figures [src_0004]." in out
    assert "not in the sources reviewed, and no Q1-2026 revision is in the sources reviewed." in out
    for bad in ("exhibit", "structured", "fetched", "evidence"):
        assert bad not in out.lower(), bad


def test_scrub_moves_research_limit_sentences_out_of_prose():
    text = (
        "### Growth drivers\n\n"
        "Backlog reached $230 billion [src_0003]. Research stopped at the section budget (scrape cap 6 reached). "
        "12 planned web queries dropped at the section search cap.\n\n"
        "- The company guided to 5% growth [src_0003]. 9 planned web queries were dropped at the search cap.\n"
        "- Only the search cap limited this section.\n\n"
        "| Metric | Value |\n|---|---|\n| scrape cap | kept verbatim in tables |\n\n"
        "> Coverage: partial — research stopped at the section budget (scrape cap 6 reached)."
    )
    out, moved = scrub_pipeline_vocabulary(text)
    assert moved == [
        "Research stopped at the section budget (scrape cap 6 reached)",
        "12 planned web queries dropped at the section search cap",
        "9 planned web queries were dropped at the search cap",
        "Only the search cap limited this section",
    ]
    assert (
        "### Growth drivers\n\nBacklog reached $230 billion [src_0003].\n\n- The company guided to 5% growth [src_0003].\n\n| Metric"
        in out
    )
    assert "| scrape cap | kept verbatim in tables |" in out  # tables untouched
    assert out.endswith(
        "> Coverage: partial — research stopped at the section budget (scrape cap 6 reached)."
    )


def test_first_party_sources_are_recognised(tmp_path):
    ctx, store = _ctx(tmp_path)
    ids = _sources(store)
    first = {k for k, sid in ids.items() if is_first_party_source(store.get(sid), ctx)}
    assert first == {"tenk", "release", "ir", "wiki", "call"}
    assert not is_first_party_source(store.get(ids["press"]), ctx)
    assert not is_first_party_source(store.get(ids["peer"]), ctx)


def test_perspectives_dropped_when_only_company_documents_or_wikipedia(tmp_path):
    ctx, store = _ctx(tmp_path)
    ids = _sources(store)
    narrative = (
        "### Executive officers\n\nScott became CFO in 2025 [{tenk}].\n\n"
        "### Perspectives\n\n"
        "- Lockheed Martin (company release): described Scott as an experienced finance leader [{release}].\n"
        "- Wikipedia: presents Taiclet's chairman title as dating from March 2021 [{wiki}].\n"
        "- The Motley Fool: notes that sales growth normalized was 5% [{call}].\n"
    ).format(**ids)
    out, dropped = drop_first_party_perspectives(narrative, ctx)
    assert (
        dropped
        and "Perspectives" not in out
        and out.endswith(f"Scott became CFO in 2025 [{ids['tenk']}].")
    )
    # one second-party bullet keeps the block
    kept_in = (
        narrative
        + f"- GAO (GAO-25-107632): delivery time frames continued to worsen [{ids['press']}].\n"
    )
    out2, dropped2 = drop_first_party_perspectives(kept_in, ctx)
    assert not dropped2 and out2 == kept_in
    # a peer's 10-K counts as a second party
    peer_in = (
        narrative
        + f"- L3Harris (10-K): names Lockheed Martin as a principal competitor [{ids['peer']}].\n"
    )
    assert not drop_first_party_perspectives(peer_in, ctx)[1]
    # an uncited Perspectives block has no second-party source either
    assert drop_first_party_perspectives(
        "### Facts\n\nx.\n\n### Perspectives\n\n- Someone says so.\n", ctx
    )[1]


def test_finalize_draft_applies_hygiene_and_notes_it(tmp_path):
    ctx, store = _ctx(tmp_path)
    ids = _sources(store)
    brief = Brief(
        section=8,
        title="Management and Governance",
        scope="s",
        source_buckets=["DEF 14A"],
        pages=1.0,
        sub_questions=["q"],
        required_exhibit="officer table",
        stop_condition="done",
    )
    narrative = (
        "### Officers\n\nThe structured facts list nine officers [{tenk}]. "
        "Research stopped at the section budget (scrape cap 5 reached).\n\n"
        "### Perspectives\n\n- Lockheed Martin (company release): praised Scott [{release}].\n"
    ).format(**ids)
    d = finalize_draft(
        brief,
        ctx,
        caveat=caveat_line("officer list from the fetched evidence"),
        narrative=narrative,
        exhibit_md="| Name | Title |\n|---|---|\n| Evan T. Scott [{tenk}] | CFO [{tenk}] |".format(
            **ids
        ),
        exhibit_present=True,
        claims=[],
        coverage="full",
    )
    md = d.markdown
    assert md.startswith("*Caveat: officer list from the sources reviewed.*")
    assert f"The source data list nine officers [{ids['tenk']}]." in md
    body = md.split("> Coverage")[0]
    assert "### Perspectives" not in md and "praised Scott" not in md
    assert "structured facts" not in md and "scrape cap 5" not in body
    assert md.endswith(
        "> Coverage: full — Research stopped at the section budget (scrape cap 5 reached); "
        "Perspectives omitted: no second-party source (only the company's own documents or Wikipedia)."
    )
    assert d.coverage == "full" and d.exhibit_present


def test_house_style_tells_the_drafter():
    assert "the table below" in HOUSE_STYLE and "scrape cap" in HOUSE_STYLE
    assert "omit the Perspectives list" in HOUSE_STYLE
