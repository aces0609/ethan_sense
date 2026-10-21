"""Section researcher subgraph (requirements §5): plan_queries → call_tools → compress_and_draft → critic.

The same graph is compiled for every section; a handler registry maps section ids to
plan/collect/draft functions. Phase 1 registers section 9. A section without a handler
returns an `unavailable` draft with a coverage blockquote, never a silent drop.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langgraph.graph import END, START, StateGraph

from primer.clients.edgar import EdgarClient
from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph import critic as critic_mod
from primer.runlog import get_runlog
from primer.sections import sec09_financials
from primer.state import SECTION_TITLES, Brief, Entity, ResearcherState, SectionDraft

HANDLERS: dict[int, Any] = {9: sec09_financials}


def _ctx(state: ResearcherState):
    run_dir = state["run_dir"]
    runlog = get_runlog(run_dir)
    store = EvidenceStore(run_dir, as_of=date.fromisoformat(state["as_of"]))
    offline = bool(state.get("foundation", {}).get("offline"))
    edgar = EdgarClient(runlog=runlog, offline=offline)
    return runlog, store, edgar


def plan_queries(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    entity = Entity.model_validate(state["entity"])
    handler = HANDLERS.get(brief.section)
    if handler is None:
        return {"plan_items": []}
    items = handler.plan(brief, entity, date.fromisoformat(state["as_of"]))
    runlog = get_runlog(state["run_dir"])
    runlog.plan.setdefault("queries", {})[str(brief.section)] = items
    return {"plan_items": items}


def call_tools(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    entity = Entity.model_validate(state["entity"])
    handler = HANDLERS.get(brief.section)
    if handler is None:
        return {"tool_results": {}}
    runlog, store, edgar = _ctx(state)
    with runlog.timed(f"sec{brief.section}.call_tools"):
        try:
            results = handler.collect(state.get("plan_items", []), entity, store, edgar, runlog)
        except Exception as e:  # a failed section does not fail the run
            runlog.error(f"section {brief.section} collect: {e!r}")
            results = {"error": repr(e)}
    return {"tool_results": results, "evidence_refs": [r.source_id for r in store.records()]}


def compress_and_draft(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    entity = Entity.model_validate(state["entity"])
    handler = HANDLERS.get(brief.section)
    runlog, store, _ = _ctx(state)
    if handler is None:
        d = SectionDraft(
            section=brief.section,
            title=SECTION_TITLES[brief.section],
            markdown=f"> Coverage: unavailable — section {brief.section} researcher is not implemented in this build.",
            coverage="unavailable",
        )
    else:
        with runlog.timed(f"sec{brief.section}.draft"):
            d = handler.draft(
                brief,
                entity,
                state.get("tool_results", {}),
                store,
                runlog,
                state.get("critic_notes"),
            )
    d.iterations = int(state.get("iteration", 0)) + 1
    return {"draft": d.model_dump(mode="json"), "iteration": d.iterations}


def critic(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    draft = SectionDraft.model_validate(state["draft"])
    runlog = get_runlog(state["run_dir"])
    sc = critic_mod.score(
        draft, brief, float(section_budget(brief.section).get("pages", 1.5)), runlog
    )
    draft.critic_notes = sc.notes
    runlog.critic_scores[str(brief.section)] = {**sc.model_dump(), "mean": round(sc.mean, 2)}
    out: dict[str, Any] = {
        "draft": draft.model_dump(mode="json"),
        "critic_notes": sc.notes,
        "critic_scores": {brief.section: {**sc.model_dump(), "mean": round(sc.mean, 2)}},
    }
    max_loops = int(section_budget(brief.section).get("critic_loops", 2))
    if sc.passed or draft.iterations >= max_loops or draft.coverage == "unavailable":
        out.update(
            {
                "drafts": {brief.section: draft.model_dump(mode="json")},
                "claims": [c.model_dump(mode="json") for c in draft.claims],
                "coverage": {brief.section: draft.coverage},
            }
        )
        runlog.coverage[str(brief.section)] = draft.coverage
    return out


def _after_critic(state: ResearcherState) -> str:
    draft = SectionDraft.model_validate(state["draft"])
    section = draft.section
    sc = state.get("critic_scores", {}).get(section) or {}
    max_loops = int(section_budget(section).get("critic_loops", 2))
    if sc.get("passed") or draft.iterations >= max_loops or draft.coverage == "unavailable":
        return END
    return "plan_queries"


def build_researcher():
    g = StateGraph(ResearcherState)
    g.add_node("plan_queries", plan_queries)
    g.add_node("call_tools", call_tools)
    g.add_node("compress_and_draft", compress_and_draft)
    g.add_node("critic", critic)
    g.add_edge(START, "plan_queries")
    g.add_edge("plan_queries", "call_tools")
    g.add_edge("call_tools", "compress_and_draft")
    g.add_edge("compress_and_draft", "critic")
    g.add_conditional_edges("critic", _after_critic, {END: END, "plan_queries": "plan_queries"})
    return g.compile()
