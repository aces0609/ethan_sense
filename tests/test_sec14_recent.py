"""Section 14 handler on the fixtures: eight 8-Ks fall in the trailing 12 months of the
submissions fixture (all served as the fake 8-K text); an IR site map with dated news
links; news hits whose pages carry no date. The recency validator must pass."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import HANDLERS, build_researcher
from primer.sections.exhibits_sec14 import Event, date_in_url, eightk_event, event_log
from primer.state import Inputs, SectionDraft
from primer.validate.rules import check_recency

from .conftest import FAKE_8K_HTML, _hit

WEB = Path(__file__).parent / "fixtures" / "web"
AS_OF = date(2026, 10, 21)
IR_ROOT = "https://investors.lockheedmartin.com"
IR_LINKS = [
    f"{IR_ROOT}/news-releases/news-release-details/2026-09-15-lockheed-martin-pac-3-contract",
    f"{IR_ROOT}/news-releases/news-release-details/2025-03-01-lockheed-martin-old-release",
    f"{IR_ROOT}/news-releases/news-release-details/lockheed-martin-undated-release",
    f"{IR_ROOT}/events-and-presentations/2026/investor-day-2026.pdf",
]


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


def _table_rows(md: str) -> list[list[str]]:
    rows = []
    for ln in md.splitlines():
        if ln.startswith("| 20") and "[src_" in ln:
            rows.append([c.strip() for c in ln.split("|")[1:-1]])
    return rows


def test_eightk_event_and_url_dates():
    from primer.evidence.html import html_to_text

    text = html_to_text(FAKE_8K_HTML)
    event, sentence = eightk_event(text, "1.01,9.01")
    assert event.startswith("8-K Item 1.01: material definitive agreement")
    assert sentence.startswith(
        "On July 15, 2026, Lockheed Martin Corporation entered into a definitive agreement"
    )
    event, sentence = eightk_event(text, "8.01")
    assert "other events" in event and "$3.0 billion of share repurchases" in sentence
    event, sentence = eightk_event(
        text, "2.02,9.01"
    )  # item absent from the text: label plus first body sentence
    assert "results of operations" in event and sentence and not sentence.isupper()
    assert date_in_url(IR_LINKS[0]) == date(2026, 9, 15) and date_in_url(IR_LINKS[2]) is None
    assert date_in_url("https://x.com/2026/07/15/story") == date(2026, 7, 15)


def test_event_log_applies_recency_and_sorts_newest_first():
    ev = [
        Event(AS_OF - timedelta(days=400), "old", "news", "x", "src_0001", "news"),
        Event(date(2026, 1, 29), "8-K Item 2.02", "8-K (SEC)", "results", "src_0002", "8-K"),
        Event(date(2026, 8, 28), "8-K Item 1.01", "8-K (SEC)", "deal", "src_0003", "8-K"),
    ]
    log = event_log(ev, AS_OF, undated=["Some undated page"])
    assert [e.date for e in log.events] == [date(2026, 8, 28), date(2026, 1, 29)]
    assert (
        log.eightk_rows == 2
        and log.claims[0].metric == "event_count_trailing_12m"
        and log.claims[0].value == 2
    )
    assert any("outside the trailing 12 months" in n for n in log.notes)
    assert any("undated pages not in the log: Some undated page" in n for n in log.notes)
    assert "src_0001" not in log.markdown


def test_section14_with_firecrawl(edgar_mock, firecrawl_mock, output_dir):
    fc = firecrawl_mock(
        search={
            "news": [
                _hit(
                    "https://www.defensenews.com/industry/2026/10/01/lockheed-news/",
                    "Lockheed news item",
                )
            ],
            "announces": [
                _hit(
                    "https://www.breakingdefense.com/2026/09/lockheed-announces/",
                    "Lockheed announces",
                )
            ],
        },
        pages={
            "2026-09-15-lockheed-martin-pac-3-contract": (
                WEB / "lmt_ir_release_2026-09-15.md"
            ).read_text(encoding="utf-8")
        },
        maps={"investors.lockheedmartin.com": IR_LINKS},
    )
    st = _state([14])
    assert st["foundation"]["ir"]["links"]["news"]
    draft, store = _run(st, 14)
    _common_checks(draft, store, 14)
    md = draft.markdown
    assert draft.exhibit_present and draft.coverage == "full"
    assert check_recency({14: md}, store, AS_OF)["ok"]

    rows = _table_rows(md)
    dates = [r[0][:10] for r in rows]
    assert dates == sorted(dates, reverse=True)
    eightk_rows = [r for r in rows if "8-K (SEC)" in r[2]]
    assert len(eightk_rows) >= 3
    for r in eightk_rows:
        sid = re.search(r"\[(src_\d{4})\]", r[4]).group(1)
        rec = store.get(sid)
        assert (
            rec.type == "8-K"
            and rec.tier == 1
            and rec.gov_id
            and rec.published_date >= AS_OF - timedelta(days=365)
        )
    assert any("Item 1.01" in r[1] and "Ultra Maritime" in r[3] for r in eightk_rows)
    ir_rows = [r for r in rows if "press release (IR site)" in r[2]]
    assert len(ir_rows) == 1 and ir_rows[0][0].startswith("2026-09-15") and "PAC-3" in ir_rows[0][1]
    # the 2025 IR link is outside the window and never scraped; undated pages are noted, not cited
    assert not any(c[0] == "scrape" and "2025-03-01" in c[1] for c in fc.calls)
    assert not any(c[0] == "scrape" and "undated-release" in c[1] for c in fc.calls)
    assert "undated pages not in the log" in md
    assert not any(
        "defensenews" in store.get(s).url for s in set(re.findall(r"\[(src_\d{4})\]", md))
    )
    metrics = {c.metric: c for c in draft.claims}
    assert (
        metrics["event_count_trailing_12m"].value == len(rows)
        and metrics["event_count_trailing_12m"].unit == "count"
    )
    assert "recency rule" in draft.caveat
    assert (
        sum(1 for c in fc.calls if c[0] == "search" and "transcript" not in c[1])
        <= section_budget(14)["searches"]
    )


def test_section14_keyless_path(edgar_mock, output_dir):
    st = _state([14])
    draft, store = _run(st, 14)
    _common_checks(draft, store, 14)
    md = draft.markdown
    assert draft.exhibit_present and draft.coverage == "full"
    assert check_recency({14: md}, store, AS_OF)["ok"]
    rows = _table_rows(md)
    assert len(rows) >= 3 and all("8-K (SEC)" in r[2] for r in rows)
    assert "Firecrawl not configured" in md and "no IR site map" in md
    assert draft.budget["searches"].startswith("0/") and draft.budget["scrapes"].startswith("0/")
