"""Review 2026-10-21 §9 item 5: section 14 runs before sections 3, 6, 8, 10 and 12, and its
dated events (date, event, source_id) ride in those sections' researcher payloads.

The node functions are tested directly on a synthetic state, then the compiled main graph
is run with a recording stand-in for the researcher subgraph to prove the real ordering.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

from langgraph.types import Send

from primer.evidence.store import EvidenceStore
from primer.graph import main as main_mod
from primer.graph.main import (
    _after_propagate,
    events_from_draft,
    fan_out,
    late_sections,
    new_run,
    propagate_recent,
    run_primer,
)
from primer.sections.common import recent_events_block
from primer.state import Inputs

EVENT_LOG = (
    "*Caveat: the log covers the trailing 12 months.*\n\n"
    "### Event log (trailing 12 months, newest first)\n\n"
    "| Date | Event | Source type | Implication (source wording) | Source |\n"
    "|---|---|---|---|---|\n"
    "| 2026-08-28 [{a}] | 8-K Item 1.01: material definitive agreement [{a}] | 8-K (SEC) [{a}] | "
    "On August 24, 2026, Lockheed Martin entered into a new 364-Day Revolving Credit Agreement [{a}] | [{a}] |\n"
    "| 2025-12-09 [{b}] | 8-K Item 1.01: material definitive agreement [{b}] | 8-K (SEC) [{b}] | "
    "On December 5, 2025, the company entered a $3.0 billion 364-day revolver [{b}] | [{b}] |\n\n"
    "> Coverage: full — brief satisfied."
)


def _plan(sections):
    return {s: {"section": s, "title": f"S{s}"} for s in sections}


def _state(sections, **extra):
    return {
        "run_dir": "x",
        "as_of": "2026-10-21",
        "entity": {"name": "Lockheed Martin"},
        "foundation": {},
        "plan": _plan(sections),
        **extra,
    }


def test_first_wave_holds_back_the_sections_that_read_section_14():
    assert late_sections(_plan([2, 3, 6, 8, 9, 10, 12, 13, 14])) == [3, 6, 8, 10, 12]
    assert late_sections(_plan([3, 6, 9])) == []  # no section 14: nothing to wait for
    sends = fan_out(_state([2, 3, 6, 9, 14]))
    assert all(isinstance(s, Send) and s.node == "section_researcher" for s in sends)
    assert sorted(s.arg["brief"]["section"] for s in sends) == [2, 9, 14]
    assert all(s.arg["recent_events"] == [] for s in sends)
    # without section 14 in the plan every section goes in the first wave
    assert sorted(s.arg["brief"]["section"] for s in fan_out(_state([3, 6, 9]))) == [3, 6, 9]


def test_events_are_lifted_from_section_14_and_sent_with_the_second_wave():
    md = EVENT_LOG.format(a="src_0101", b="src_0102")
    events = events_from_draft(md)
    assert [e["date"] for e in events] == ["2026-08-28", "2025-12-09"]
    assert events[0]["source_id"] == "src_0101" and events[0]["event"].startswith("8-K Item 1.01")
    assert "364-Day Revolving Credit Agreement" in events[0]["detail"]

    st = _state([2, 3, 6, 9, 14], drafts={2: {}, 9: {}, 14: {"markdown": md}})
    upd = propagate_recent(st)
    assert upd["recent_wave"] == 1 and upd["recent_events"] == events
    st.update(upd)
    sends = _after_propagate(st)
    assert sorted(s.arg["brief"]["section"] for s in sends) == [3, 6]
    for s in sends:
        assert s.arg["recent_events"] == events
        assert {"date", "event", "source_id"} <= set(s.arg["recent_events"][0])
    # after the second wave everything is drafted: the graph moves on to reconcile
    st["drafts"].update({3: {}, 6: {}})
    st.update(propagate_recent(st))
    assert st["recent_wave"] == 2 and _after_propagate(st) == "reconcile"
    # a retry pass (wave 3) never re-sends the late sections
    st["drafts"].pop(3)
    st.update(propagate_recent(st))
    assert _after_propagate(st) == "reconcile"


def test_main_graph_runs_section_14_before_the_late_sections(edgar_mock, output_dir, monkeypatch):
    inputs = Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=[3, 6, 9, 14],
    )
    run_id, run_dir = new_run(inputs)
    store = EvidenceStore.for_run(run_dir, as_of=date(2026, 10, 21))
    a = store.register(
        "8-K text",
        type="8-K",
        publisher="LOCKHEED MARTIN CORP",
        title="8-K",
        url="https://www.sec.gov/Archives/edgar/data/936468/1/a.htm",
        tier=1,
    ).source_id
    b = store.register(
        "8-K text 2",
        type="8-K",
        publisher="LOCKHEED MARTIN CORP",
        title="8-K",
        url="https://www.sec.gov/Archives/edgar/data/936468/2/b.htm",
        tier=1,
    ).source_id
    calls: list[tuple[int, list]] = []
    lock = threading.Lock()

    class FakeResearcher:
        def invoke(self, payload):
            sec = int(payload["brief"]["section"])
            with lock:
                calls.append((sec, list(payload.get("recent_events") or [])))
            md = (
                EVENT_LOG.format(a=a, b=b)
                if sec == 14
                else f"Section {sec} text [{a}].\n\n> Coverage: full — ok."
            )
            return {
                "drafts": {
                    sec: {
                        "section": sec,
                        "title": "t",
                        "markdown": md,
                        "coverage": "full",
                        "claims": [],
                        "source_ids": [a],
                        "exhibit_present": True,
                    }
                },
                "claims": [],
                "coverage": {sec: "full"},
            }

    monkeypatch.setattr(main_mod, "RESEARCHER", FakeResearcher())
    result = run_primer(inputs, run_id=run_id, run_dir=run_dir)
    order = [sec for sec, _ in calls]
    assert sorted(order) == [3, 6, 9, 14]
    first_wave = {i for i, (sec, _) in enumerate(calls) if sec in (9, 14)}
    second_wave = {i for i, (sec, _) in enumerate(calls) if sec in (3, 6)}
    assert max(first_wave) < min(second_wave)
    by_sec = dict(calls)
    assert by_sec[9] == [] and by_sec[14] == []
    assert by_sec[3][0]["date"] == "2026-08-28" and by_sec[3][0]["source_id"] == a
    assert by_sec[6] == by_sec[3]
    assert Path(result["md_path"]).exists()


def test_recent_events_block_for_the_drafter(tmp_path):
    from primer.clients.edgar import EdgarClient
    from primer.runlog import RunLog
    from primer.sections.common import SectionBudget, ToolContext
    from primer.state import Entity

    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 21))
    sid = store.register(
        "x", type="8-K", publisher="LMT", title="8-K", url="https://www.sec.gov/a.htm"
    ).source_id
    runlog = RunLog(tmp_path / "run")
    ctx = ToolContext(
        section=6,
        run_dir=tmp_path / "run",
        as_of=date(2026, 10, 21),
        entity=Entity(name="Lockheed Martin"),
        foundation={},
        store=store,
        edgar=EdgarClient(runlog=runlog, offline=True),
        runlog=runlog,
        budget=SectionBudget.for_section(6),
        recent_events=[
            {
                "date": "2026-08-28",
                "event": "8-K Item 1.01",
                "detail": "new revolver",
                "source_id": sid,
            },
            {
                "date": "2026-09-01",
                "event": "unknown source",
                "detail": "",
                "source_id": "src_9999",
            },
        ],
    )
    block = recent_events_block(ctx)
    assert block == f"- 2026-08-28: 8-K Item 1.01 — new revolver [{sid}]"
