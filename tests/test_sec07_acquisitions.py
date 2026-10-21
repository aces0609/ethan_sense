"""Section 7 handler on the fixtures: the fake 8-K / 10-K / 10-Q carry an Ultra Maritime
$1.5 billion agreement, a Terran Orbital $450 million acquisition and a Sikorsky Commercial
$200 million divestiture; the EDGAR full-text mock returns two 8-K hits."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import HANDLERS, build_researcher
from primer.sections.exhibits_sec07 import acquisition_table, extract_deals
from primer.state import Inputs, SectionDraft

from .conftest import _hit

WEB = Path(__file__).parent / "fixtures" / "web"
AS_OF = date(2026, 10, 21)


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
    draft = SectionDraft.model_validate(out["drafts"][section])
    return draft, EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)


def _common_checks(draft: SectionDraft, store: EvidenceStore, section: int) -> None:
    for c in draft.claims:
        assert c.section == section and c.source_ids, c
        assert all(store.has(s) for s in c.source_ids), c
    for sid in set(re.findall(r"\[(src_\d{4})\]", draft.markdown)):
        assert store.has(sid), sid
    b = section_budget(section)
    used_s, cap_s = draft.budget["searches"].split("/")
    used_p, cap_p = draft.budget["scrapes"].split("/")
    assert int(used_s) <= int(cap_s) == int(b["searches"])
    assert int(used_p) <= int(cap_p) == int(b["scrapes"])


def _row(markdown: str, needle: str) -> str:
    rows = [ln for ln in markdown.splitlines() if ln.startswith("|") and needle in ln]
    assert rows, f"no table row with {needle!r}"
    return rows[0]


def test_extract_deals_patterns():
    text = (
        "Note 3 – Acquisitions. On July 15, 2026 we entered into a definitive agreement to acquire "
        "Ultra Maritime for approximately $1.5 billion in cash. In 2024 we acquired Terran Orbital "
        "Corporation for approximately $450 million, which is included in our Space segment. In 2022 "
        "we completed the divestiture of our Sikorsky Commercial business for $200 million."
    )
    deals = extract_deals(text, "src_0001", "10-K", date(2026, 1, 29))
    by = {d.target: d for d in deals}
    assert by["Ultra Maritime"].value_usd == 1.5e9 and by["Ultra Maritime"].status == "announced"
    assert (
        by["Ultra Maritime"].date == "2026-07-15" and by["Ultra Maritime"].date_basis == "sentence"
    )
    assert by["Terran Orbital Corporation"].status == "completed"
    assert (
        by["Terran Orbital Corporation"].date == "2024"
        and by["Terran Orbital Corporation"].segment == "Space"
    )
    assert "included in our Space segment" in by["Terran Orbital Corporation"].rationale
    assert (
        by["Sikorsky Commercial"].status == "divested"
        and by["Sikorsky Commercial"].value_usd == 2e8
    )
    ex = acquisition_table(deals)
    assert ex.present and ex.filing_rows == 3
    assert {c.metric for c in ex.claims} == {
        "deal_value_ultra_maritime",
        "deal_value_terran_orbital",
        "deal_value_sikorsky_commercial",
    }


def test_conflicting_values_keep_both_claims():
    a = extract_deals(
        "We agreed to acquire Acme Systems for $1.0 billion.", "src_0001", "8-K", None
    )
    b = extract_deals("Lockheed acquired Acme Systems for $1.2 billion.", "src_0002", "press", None)
    ex = acquisition_table(a + b)
    assert len(ex.rows) == 1 and len(ex.rows[0].values) == 2
    assert len([c for c in ex.claims if c.metric == "deal_value_acme_systems"]) == 2
    assert any("sources differ" in n for n in ex.notes)


def test_section7_with_firecrawl(edgar_mock, firecrawl_mock, output_dir):
    fc = firecrawl_mock(
        search={
            "acquisitions list": [
                _hit(
                    "https://news.lockheedmartin.com/2026-07-15-Lockheed-Martin-to-Acquire-Ultra-Maritime",
                    "Lockheed Martin to Acquire Ultra Maritime",
                )
            ],
            "acquires": [
                _hit(
                    "https://www.defensenews.com/industry/2026/07/15/lockheed-ultra/",
                    "Lockheed buys Ultra",
                )
            ],
        },
        pages={"Ultra-Maritime": (WEB / "lmt_press_ultra_maritime.md").read_text(encoding="utf-8")},
    )
    st = _state([7])
    draft, store = _run(st, 7)
    _common_checks(draft, store, 7)
    assert draft.exhibit_present and draft.coverage == "full"

    row = _row(draft.markdown, "Ultra Maritime")
    assert "$1.5 billion" in row and "announced" in row and "2026-07-15" in row
    value_cell = row.split("|")[3]
    sid = re.search(r"\[(src_\d{4})\]", value_cell).group(1)
    rec = store.get(sid)
    assert rec.tier == 1 and rec.type in ("8-K", "10-K")
    assert "8-K [src_" in row  # the full-text 8-K hit is among the row's sources
    assert "Terran Orbital" in draft.markdown and "divested" in draft.markdown
    assert any(c.metric == "deal_value_ultra_maritime" and c.value == 1.5e9 for c in draft.claims)
    # the press page was scraped and sits in the evidence list; searches stayed inside the cap
    assert any(call[0] == "scrape" and "Ultra-Maritime" in call[1] for call in fc.calls)
    section_searches = [c for c in fc.calls if c[0] == "search" and "transcript" not in c[1]]
    assert len(section_searches) <= section_budget(7)["searches"]
    assert "[src_" in draft.markdown and draft.caveat and draft.caveat.startswith("*Caveat:")


def test_section7_keyless_path(edgar_mock, output_dir):
    st = _state([7])
    draft, store = _run(st, 7)
    _common_checks(draft, store, 7)
    assert draft.exhibit_present and draft.coverage == "full"
    row = _row(draft.markdown, "Ultra Maritime")
    assert "$1.5 billion" in row
    assert "Firecrawl not configured" in draft.markdown
    assert draft.budget["searches"].startswith("0/")
    eightks = [s for s in draft.source_ids if store.get(s).type == "8-K"]
    assert eightks and all(store.get(s).gov_id for s in eightks)
