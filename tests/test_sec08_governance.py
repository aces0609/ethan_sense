"""Section 8 handler on the fixtures: four officers in the 10-K Item 10, two in the proxy,
an ownership table (State Street 15.6%, Vanguard 9.2%, BlackRock 7.3%), board and pay
sentences; the submissions fixture holds no SC 13D/G or Form 4."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import HANDLERS, build_researcher
from primer.sections.exhibits_sec08 import governance_facts, ownership_rows
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


def test_ownership_and_facts_parsers():
    text = (
        "Our Board of Directors consists of 12 directors, 11 of whom are independent under NYSE listing standards.\n"
        " | Name and Address of Beneficial Owner | Shares Beneficially Owned | Percent of Class\n"
        " | State Street Corporation | 36,100,000 | 15.6%\n"
        " | The Vanguard Group | 21,200,000 | 9.2%\n"
        " | All directors and executive officers as a group (24 persons) | 640,000 | 0.3%\n"
        "Total compensation for Mr. Taiclet in 2025 was $24.5 million, of which 90% was at-risk. "
        "The 2025 say-on-pay proposal received 94% support. Shares shown reflect sole voting power.\n"
    )
    rows = ownership_rows(text, "src_0003")
    assert [(r.holder, r.pct) for r in rows[:2]] == [
        ("State Street Corporation", 15.6),
        ("The Vanguard Group", 9.2),
    ]
    assert rows[2].is_group and rows[2].pct == 0.3
    f = governance_facts(text, "src_0003")
    assert (f.board_size, f.board_independent) == (12, 11)
    assert f.ceo_pay_usd == 24.5e6 and f.ceo_pay_year == "2025"
    assert f.say_on_pay_pct == 94.0 and f.voting_power_mentioned


def _check_exhibits(draft: SectionDraft, store: EvidenceStore, st) -> None:
    tenk_sid = st["foundation"]["sources"]["10-K"][0]
    proxy_sid = st["foundation"]["sources"]["DEF 14A"][0]
    officer_rows = [
        ln
        for ln in draft.markdown.splitlines()
        if ln.startswith("|") and re.search(r"\| \d{2} \[src_", ln)
    ]
    assert len(officer_rows) >= 3
    assert all(f"[{tenk_sid}]" in ln or f"[{proxy_sid}]" in ln for ln in officer_rows)
    assert any("Taiclet" in ln and "Chief Executive Officer" in ln for ln in officer_rows)
    own = [
        ln
        for ln in draft.markdown.splitlines()
        if ln.startswith("- ") and "beneficially owned" in ln
    ]
    assert any("State Street Corporation: 15.6%" in ln and f"[{proxy_sid}]" in ln for ln in own)
    assert any("Vanguard" in ln and "9.2%" in ln for ln in own)
    metrics = {c.metric: c for c in draft.claims}
    assert metrics["ownership_pct_state_street_corporation"].value == 15.6
    assert metrics["ownership_pct_state_street_corporation"].unit == "pct"
    assert (
        metrics["ceo_total_compensation"].value == 24.5e6
        and metrics["ceo_total_compensation"].unit == "USD"
    )
    assert metrics["board_size"].value == 12 and metrics["board_independent_count"].value == 11
    assert "no SC 13D/G in the trailing 12 months" in draft.markdown
    assert "filed 2026-01-29" in draft.caveat  # the 10-K date of the officer list
    assert "not voting power" in draft.caveat


def test_section8_with_firecrawl(edgar_mock, firecrawl_mock, output_dir):
    fc = firecrawl_mock(
        search={
            "CEO": [
                _hit(
                    "https://www.defensenews.com/industry/2026/05/01/lockheed-ceo/",
                    "Lockheed CEO Taiclet on the outlook",
                ),
                _hit(
                    "https://www.reuters.com/business/aerospace-defense/lockheed-suit-2026/",
                    "Lockheed faces suit",
                ),
            ],
        },
        pages={
            "lockheed-ceo": "# Lockheed CEO Taiclet on the outlook\n\nJim Taiclet said the company expects record missile output in 2026.\n"
        },
    )
    st = _state([8])
    draft, store = _run(st, 8)
    _common_checks(draft, store, 8)
    assert draft.exhibit_present and draft.coverage == "full"
    _check_exhibits(draft, store, st)
    section_searches = [c for c in fc.calls if c[0] == "search" and "transcript" not in c[1]]
    assert len(section_searches) <= section_budget(8)["searches"]
    # the reuters page is fetched but access-limited: headline only, never quoted
    limited = [r for r in store.records() if "reuters.com" in r.url]
    assert limited and limited[0].access == "limited"


def test_section8_keyless_path(edgar_mock, output_dir):
    st = _state([8])
    draft, store = _run(st, 8)
    _common_checks(draft, store, 8)
    assert draft.exhibit_present and draft.coverage == "full"
    _check_exhibits(draft, store, st)
    assert "Firecrawl not configured" in draft.markdown
    assert draft.budget["searches"].startswith("0/")
