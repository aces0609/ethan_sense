"""Section researcher subgraph (requirements §5): plan_queries → call_tools → compress_and_draft → critic.

One graph, compiled once, run per section with a different brief. A handler module per
section (REGISTRY) supplies plan/collect/draft; the subgraph supplies the ToolContext, the
budget, the query-planner model call, exception isolation (a failed section is marked
unavailable, the run continues) and the critic loop (max `critic_loops`, from budgets.yaml).

Why not a free ReAct tool loop: every section's evidence buckets are known in advance
(requirements §6), so a planned batch of structured calls plus a bounded set of searches is
cheaper, reproducible and easier to budget. The critic loop is the iteration: its notes feed
plan_queries on the next pass as targeted gaps. Recorded in docs/DECISIONS.md.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from primer.adapters.market_data import get_adapter
from primer.clients.edgar import EdgarClient
from primer.clients.firecrawl import FirecrawlClient
from primer.clients.samgov import SamGovClient
from primer.clients.usaspending import USAspendingClient
from primer.config import section_budget, trade_press
from primer.evidence.store import EvidenceStore
from primer.graph import critic as critic_mod
from primer.llm import invoke_structured, tokens_fraction
from primer.runlog import get_runlog
from primer.sections.common import (
    SectionBudget,
    ToolContext,
    run_memo,
    unavailable_draft,
)
from primer.sections.text_tools import segment_names_from_text
from primer.state import SECTION_TITLES, Brief, Entity, ResearcherState, SectionDraft
from primer.validate.exhibits import apply_exhibit_check

REGISTRY: dict[int, str] = {
    2: "primer.sections.sec02_overview",
    3: "primer.sections.sec03_products",
    4: "primer.sections.sec04_programs",
    5: "primer.sections.sec05_market",
    6: "primer.sections.sec06_strategy",
    7: "primer.sections.sec07_acquisitions",
    8: "primer.sections.sec08_governance",
    9: "primer.sections.sec09_financials",
    10: "primer.sections.sec10_operating",
    11: "primer.sections.sec11_capital",
    12: "primer.sections.sec12_thesis",
    13: "primer.sections.sec13_valuation",
    14: "primer.sections.sec14_recent",
}
HANDLERS: dict[int, Any] = {}


def handler_for(section: int) -> Any | None:
    """Import the section handler lazily; None when the module does not exist yet."""
    if section in HANDLERS:
        return HANDLERS[section]
    path = REGISTRY.get(section)
    if not path:
        return None
    try:
        mod = importlib.import_module(path)
    except ModuleNotFoundError as e:
        if e.name and e.name.startswith("primer.sections"):
            return None  # not cached: a later build (or test) may provide the module
        raise
    HANDLERS[section] = mod
    return mod


def build_context(state: ResearcherState, section: int) -> ToolContext:
    run_dir = Path(state["run_dir"])
    runlog = get_runlog(run_dir)
    as_of = date.fromisoformat(state["as_of"])
    store = EvidenceStore.for_run(run_dir, as_of=as_of)
    foundation = state.get("foundation", {}) or {}
    offline = bool(foundation.get("offline"))
    entity = Entity.model_validate(state["entity"])
    budget = SectionBudget.for_section(section)
    if state.get("budget"):
        b = state["budget"]
        budget.used_searches = int(b.get("used_searches", 0))
        budget.used_scrapes = int(b.get("used_scrapes", 0))
        budget.exhausted = b.get("exhausted")
        budget.notes = list(b.get("notes") or [])
    edgar = EdgarClient(runlog=runlog, offline=offline)
    firecrawl = FirecrawlClient(store, runlog=runlog, credit_cap=budget.credits, offline=offline)
    if not firecrawl.available:
        firecrawl = None
    usaspending = USAspendingClient(store, runlog=runlog, offline=offline)
    samgov = SamGovClient(store, runlog=runlog, offline=offline)
    if not samgov.available and not offline:
        samgov = None
    brief = Brief.model_validate(state["brief"]) if state.get("brief") else None
    ctx = ToolContext(
        section=section,
        run_dir=run_dir,
        as_of=as_of,
        entity=entity,
        foundation=foundation,
        store=store,
        edgar=edgar,
        runlog=runlog,
        budget=budget,
        offline=offline,
        firecrawl=firecrawl,
        usaspending=usaspending,
        samgov=samgov,
        brief=brief,
        recent_events=list(state.get("recent_events") or []),
    )
    if entity.ticker:
        # one adapter per run; pages are scraped once and shared between sections
        ctx.market = run_memo(
            run_dir,
            "market_data_adapter",
            lambda: get_adapter(
                store=store,
                runlog=runlog,
                run_dir=run_dir,
                firecrawl=FirecrawlClient(store, runlog=runlog, offline=offline)
                if (firecrawl or offline)
                else None,
                edgar=edgar,
                cik=entity.cik,
                exchange=entity.exchange,
                as_of=as_of,
                offline=offline,
            ),
        )
    return ctx


class _QueryPlan(BaseModel):
    queries: list[str] = Field(description="3-8 concrete web search queries")


def llm_queries(brief: Brief, ctx: ToolContext, critic_notes: list[str] | None) -> list[str]:
    """query_planner role (cheap model): concrete searches from the brief, aliases and sector
    trade press; critic notes become targeted gap queries. Template path returns []."""
    system = (
        "You plan web searches for one section of a company primer. Return 3-8 concrete search "
        "queries (no site: operators; put publisher names in the query text when a trade-press "
        "domain is preferred). Use the company's common name and aliases, the latest fiscal year, "
        "and address any critic notes as targeted gap queries first."
    )
    user = json.dumps(
        {
            "company": ctx.entity.name,
            "aliases": brief.aliases or ctx.entity.aliases,
            "ticker": ctx.entity.ticker,
            "fiscal_year": ctx.fy,
            "section": brief.title,
            "scope": brief.scope,
            "sub_questions": brief.sub_questions,
            "template_queries": brief.web_queries,
            "preferred_domains": brief.trade_press,
            "critic_notes": critic_notes or [],
            "max_queries": max(3, min(8, ctx.budget.searches)),
        }
    )
    out = invoke_structured("query_planner", _QueryPlan, system, user, ctx.runlog)
    if not out:
        return []
    return [q.strip() for q in out.queries if q.strip()][: ctx.budget.searches]


def plan_queries(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    ctx = build_context(state, brief.section)
    handler = handler_for(brief.section)
    if handler is None:
        return {"plan_items": [], "budget": ctx.budget.__dict__ | {"notes": []}}
    try:
        items = handler.plan(brief, ctx)
    except Exception as e:
        ctx.log(f"plan failed: {e!r}")
        items = []
    notes = state.get("critic_notes") or []
    extra = llm_queries(brief, ctx, notes) if (ctx.firecrawl or ctx.offline) else []
    for q in extra:
        items.append({"tool": "firecrawl.search", "query": q, "why": "query_planner"})
    ctx.runlog.plan.setdefault("queries", {})[str(brief.section)] = items
    return {"plan_items": items, "budget": _budget_state(ctx)}


def _budget_state(ctx: ToolContext) -> dict[str, Any]:
    return {
        "used_searches": ctx.budget.used_searches,
        "used_scrapes": ctx.budget.used_scrapes,
        "exhausted": ctx.budget.exhausted,
        "notes": list(dict.fromkeys(ctx.budget.notes)),
    }


def call_tools(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    ctx = build_context(state, brief.section)
    handler = handler_for(brief.section)
    if handler is None:
        return {"tool_results": {}}
    if int(state.get("iteration", 0)) > 0 and state.get("tool_results"):
        # critic loop: the budgets are spent in the first pass, so a second collect would
        # return less evidence than the first; the draft is rewritten against the same
        # evidence with the critic's notes (planned gap queries are recorded in run.json)
        return {"tool_results": state["tool_results"], "budget": _budget_state(ctx)}
    before = set(ctx.store.ids())
    with ctx.runlog.timed(f"sec{brief.section}.call_tools"):
        try:
            results = handler.collect(state.get("plan_items", []), ctx)
        except Exception as e:  # a failed section does not fail the run
            ctx.log(f"collect: {e!r}")
            results = {"error": repr(e)}
    new_ids = [s for s in ctx.store.ids() if s not in before]
    ctx.runlog.counters[f"sec{brief.section}_searches"] = ctx.budget.used_searches
    ctx.runlog.counters[f"sec{brief.section}_scrapes"] = ctx.budget.used_scrapes
    return {"tool_results": results, "evidence_refs": new_ids, "budget": _budget_state(ctx)}


def compress_and_draft(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    ctx = build_context(state, brief.section)
    handler = handler_for(brief.section)
    if handler is None:
        d = unavailable_draft(
            brief, f"section {brief.section} researcher is not implemented in this build", ctx
        )
    else:
        with ctx.runlog.timed(f"sec{brief.section}.draft"):
            try:
                d = handler.draft(
                    brief, state.get("tool_results", {}) or {}, ctx, state.get("critic_notes")
                )
            except Exception as e:
                ctx.log(f"draft: {e!r}")
                d = unavailable_draft(brief, f"draft failed: {e!r}"[:300], ctx)
    if d.title != SECTION_TITLES[brief.section]:
        d.title = SECTION_TITLES[brief.section]
    d.iterations = int(state.get("iteration", 0)) + 1
    return {"draft": d.model_dump(mode="json"), "iteration": d.iterations}


def _segments(state: ResearcherState) -> list[str]:
    """Segment names from the foundation 10-K, computed once per run (for the exhibit
    validator's "segment n/a while the text names it" rule)."""
    run_dir = Path(state["run_dir"])

    def build() -> list[str]:
        try:
            store = EvidenceStore.for_run(run_dir, as_of=date.fromisoformat(state["as_of"]))
            sids = (state.get("foundation") or {}).get("sources", {}).get("10-K") or []
            for sid in sids:
                if store.has(sid):
                    return segment_names_from_text(store.text(sid)[:400_000])
        except Exception:  # noqa: BLE001 - a missing 10-K only weakens one validator rule
            pass
        return []

    return list(run_memo(run_dir, "segment_names", build))


def validate_exhibits(draft: SectionDraft, segments: list[str], runlog) -> SectionDraft:
    """Drop broken exhibits before the critic scores the draft (review §9 item 2): the prose
    is kept, coverage becomes partial and the coverage line names the dropped exhibit."""
    chk = apply_exhibit_check(draft.markdown, draft.coverage, segments=segments)
    if chk.ok:
        return draft
    draft.markdown = chk.markdown
    if draft.coverage == "full":
        draft.coverage = "partial"
    if chk.tables_left == 0:
        draft.exhibit_present = False
    # the claims a dropped exhibit produced leave with it (they are not in the text any more)
    rules = {r for d in chk.dropped for r in d["rules"]}
    prefixes = tuple(
        p
        for rule, p in (
            ("ownership_label", "ownership_pct_"),
            ("bare_key_metric", "product_key_metric_"),
        )
        if rule in rules
    )
    if prefixes:
        draft.claims = [c for c in draft.claims if not c.metric.startswith(prefixes)]
    draft.critic_notes = list(draft.critic_notes) + [
        f"exhibit '{d['exhibit']}' dropped: {'; '.join(d['reasons'][:2])}" for d in chk.dropped
    ]
    runlog.validation.setdefault("exhibits", {})[str(draft.section)] = {
        "dropped": chk.dropped,
        "problems": chk.problems[:20],
    }
    return draft


def critic(state: ResearcherState) -> dict[str, Any]:
    brief = Brief.model_validate(state["brief"])
    draft = SectionDraft.model_validate(state["draft"])
    runlog = get_runlog(state["run_dir"])
    draft = validate_exhibits(draft, _segments(state), runlog)
    sc = critic_mod.score(
        draft, brief, float(section_budget(brief.section).get("pages", 1.5)), runlog
    )
    draft.critic_notes = sc.notes
    entry = {**sc.model_dump(), "mean": round(sc.mean, 2), "budget": draft.budget}
    runlog.critic_scores[str(brief.section)] = entry
    out: dict[str, Any] = {
        "draft": draft.model_dump(mode="json"),
        "critic_notes": sc.notes,
        "critic_scores": {brief.section: entry},
    }
    if _final(draft, sc.passed, runlog):
        out.update(
            {
                "drafts": {brief.section: draft.model_dump(mode="json")},
                "claims": [c.model_dump(mode="json") for c in draft.claims],
                "coverage": {brief.section: draft.coverage},
            }
        )
        runlog.coverage[str(brief.section)] = draft.coverage
    return out


def _final(draft: SectionDraft, passed: bool, runlog=None) -> bool:
    max_loops = int(section_budget(draft.section).get("critic_loops", 2))
    return (
        passed
        or draft.iterations >= max_loops
        or draft.coverage in ("unavailable", "not_applicable")
        or os.environ.get("PRIMER_NO_CRITIC_LOOP") == "1"
        # a second pass redrafts the whole section; keep the last 30% of the token cap for
        # the sections still on their first pass and for key_takeaways
        or (runlog is not None and tokens_fraction(runlog) >= 0.7)
    )


def _after_critic(state: ResearcherState) -> str:
    draft = SectionDraft.model_validate(state["draft"])
    sc = state.get("critic_scores", {}).get(draft.section) or {}
    runlog = get_runlog(state["run_dir"])
    return END if _final(draft, bool(sc.get("passed")), runlog) else "plan_queries"


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


def section_trade_press(entity: Entity) -> list[str]:
    return trade_press(entity.sector_tag)
