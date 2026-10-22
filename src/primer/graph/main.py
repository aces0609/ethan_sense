"""Main primer graph (requirements §5 nodes 1-11).

intake → resolve_entity → foundation_pack → plan_sections → [Send per section] section_researcher
→ key_takeaways → assemble → validate → (retry failing sections, max 2) → review → export

A SQLite checkpointer in the run folder makes a killed run resume at the failed node.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from primer import __version__
from primer.clients.edgar import EdgarClient
from primer.config import llm_available, output_root, role_model, trade_press
from primer.evidence.store import EvidenceStore
from primer.graph.foundation import build_foundation
from primer.graph.researcher import build_researcher
from primer.llm import invoke_structured, invoke_text
from primer.render.mdx import render
from primer.runlog import get_runlog
from primer.sections.briefs import all_briefs
from primer.sections.style import fmt_money
from primer.state import SECTION_TITLES, Brief, Entity, Inputs, PrimerState
from primer.validate import run_all
from primer.validate.numeric import check_numeric
from primer.validate.takeaways import check_takeaways

RESEARCHER = build_researcher()

AEROSPACE_SIC = {
    "3720",
    "3721",
    "3724",
    "3728",
    "3760",
    "3761",
    "3764",
    "3769",
    "3812",
    "3795",
    "3480",
    "3489",
}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "company"


def new_run(inputs: Inputs) -> tuple[str, Path]:
    run_id = f"{inputs.as_of_date.isoformat()}-{uuid.uuid4().hex[:8]}"
    slug = (
        (inputs.ticker or slugify(inputs.company_name)).upper()
        if inputs.ticker
        else slugify(inputs.company_name)
    )
    run_dir = output_root() / slug / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


# ---- nodes ---------------------------------------------------------------------------


def intake(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    runlog = get_runlog(state["run_dir"])
    runlog.meta.update(
        {
            "run_id": state["run_id"],
            "company_name": inputs.company_name,
            "ticker": inputs.ticker,
            "as_of": inputs.as_of_date.isoformat(),
            "sections": inputs.sections,
            "debug": inputs.debug,
            "offline": inputs.offline,
            "generator_version": __version__,
            "llm": llm_available(),
        }
    )
    return {"as_of": inputs.as_of_date.isoformat(), "errors": []}


def resolve_entity(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    runlog = get_runlog(state["run_dir"])
    edgar = EdgarClient(runlog=runlog, offline=inputs.offline)
    ent = Entity(name=inputs.company_name, ticker=inputs.ticker)

    with runlog.timed("resolve_entity"):
        match = None
        if inputs.ticker:
            match = edgar.resolve_ticker(inputs.ticker)
            if match is None:
                runlog.error(f"ticker {inputs.ticker} not found in SEC ticker file")
        else:
            hits = edgar.search_company_name(inputs.company_name)
            if len(hits) == 1 or (len(hits) > 1 and hits[0]["score"] >= 2 * hits[1]["score"]):
                match = hits[0]
            elif len(hits) > 1:
                raise ValueError(
                    "ambiguous company name; candidates: "
                    + "; ".join(f"{h['title']} ({h['ticker']}, CIK {h['cik']})" for h in hits[:5])
                    + ". Pass --ticker."
                )
        if match:
            subs = edgar.submissions(match["cik"])
            ent.cik = match["cik"]
            ent.ticker = ent.ticker or match.get("ticker")
            ent.name = subs.get("name") or match["title"]
            ent.is_public = True
            ent.exchange = match.get("exchange") or (subs.get("exchanges") or [None])[0]
            ent.fiscal_year_end = subs.get("fiscalYearEnd") or None
            ent.sic = str(subs.get("sic") or "") or None
            ent.state_of_incorporation = subs.get("stateOfIncorporation") or None
            ent.sector_tag = "aerospace_defense" if ent.sic in AEROSPACE_SIC else "default"
            aliases = {inputs.company_name}
            for fn in subs.get("formerNames", []) or []:
                if fn.get("name"):
                    aliases.add(fn["name"])
            short = re.sub(
                r"\b(corp|corporation|inc|incorporated|co|company|ltd|plc|holdings?)\b\.?",
                "",
                ent.name,
                flags=re.I,
            )
            aliases.add(re.sub(r"\s+", " ", short).strip(" ,"))
            ent.aliases = sorted(a for a in aliases if a and a != ent.name)
    runlog.meta["entity"] = ent.model_dump(mode="json")
    return {"entity": ent.model_dump(mode="json")}


def foundation_pack(state: PrimerState) -> dict[str, Any]:
    return build_foundation(state)


class _PlannedBrief(BaseModel):
    section: int
    scope: str
    sub_questions: list[str] = Field(description="3-5 sub-questions the section must answer")
    web_queries: list[str] = Field(description="3-8 concrete Firecrawl search queries")
    source_buckets: list[str] = Field(description="allowed source buckets in priority order")
    stop_condition: str


class _SectionPlan(BaseModel):
    briefs: list[_PlannedBrief]


def _expand_with_llm(
    briefs: dict[int, Brief], ent: Entity, found: dict[str, Any], runlog
) -> dict[int, Brief]:
    """plan_sections as one strong-model call (role `planner`): expands the §6 templates
    with the entity's aliases, the sector trade-press table and what the foundation pack
    found. Exhibit, page budget and structured calls stay as templated."""
    system = (
        "You are the research planner for a company primer in the AlphaSense style. For each "
        "section template you receive, return an expanded brief: a sharper scope for this "
        "company, 3-5 sub-questions, 3-8 concrete web search queries that use the company's "
        "common name and aliases (never only the legal name), name the preferred trade-press "
        "publishers in query text where useful, the source buckets in priority order, and a stop "
        "condition. Keep the section numbers. Do not invent facts about the company."
    )
    user = json.dumps(
        {
            "company": ent.name,
            "ticker": ent.ticker,
            "aliases": ent.aliases,
            "sector_trade_press": trade_press(ent.sector_tag),
            "latest_fiscal_year": found.get("fy"),
            "foundation_pack": {
                "quarters": [q["label"] for q in found.get("quarters", [])],
                "transcripts_found": found.get("transcripts", {}).get("found"),
                "gov_revenue_disclosed": (found.get("gov_revenue") or {}).get("disclosed"),
                "peers": [p.get("ticker") for p in found.get("peers", [])],
                "ir_site": (found.get("ir") or {}).get("site"),
            },
            "templates": [b.model_dump(mode="json") for b in briefs.values()],
        },
        default=str,
    )
    out = invoke_structured("planner", _SectionPlan, system, user, runlog)
    if not out:
        return briefs
    model, _ = role_model("planner")
    by_sec = {pb.section: pb for pb in out.briefs}
    for sec, b in briefs.items():
        pb = by_sec.get(sec)
        if not pb:
            continue
        b.scope = pb.scope or b.scope
        b.sub_questions = pb.sub_questions[:5] or b.sub_questions
        # template queries first (stable text, so the Firecrawl cache serves them on a
        # rerun), the planner's queries after them for the budget that remains
        merged = list(dict.fromkeys([*b.web_queries, *pb.web_queries]))
        b.web_queries = merged[:10]
        b.source_buckets = pb.source_buckets or b.source_buckets
        b.stop_condition = pb.stop_condition or b.stop_condition
        b.planner = model
    return briefs


def plan_sections(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    ent = Entity.model_validate(state["entity"])
    runlog = get_runlog(state["run_dir"])
    found = state.get("foundation", {}) or {}
    as_of = date.fromisoformat(state["as_of"])
    fy = int(found.get("fy") or (as_of.year - 1))
    researched = [s for s in inputs.sections if s != 1]
    co = ent.aliases[0] if ent.aliases else ent.name
    briefs = all_briefs(co, fy, researched, ent.aliases)
    tp = trade_press(ent.sector_tag)
    for b in briefs.values():
        b.aliases = list(ent.aliases)
        b.trade_press = tp
    if llm_available():
        with runlog.timed("plan_sections"):
            try:
                briefs = _expand_with_llm(briefs, ent, found, runlog)
            except Exception as e:  # planner failure falls back to the templates
                runlog.error(f"plan_sections LLM: {e!r}")
    plan = {s: b.model_dump(mode="json") for s, b in briefs.items()}
    runlog.plan["briefs"] = {str(k): v for k, v in plan.items()}
    runlog.write()
    if inputs.debug:
        from langgraph.types import interrupt

        decision = interrupt({"message": "Approve the section plan", "plan": plan})
        if decision not in (None, "approve", True):
            return {
                "plan": plan,
                "retries": {s: 0 for s in researched},
                "errors": [f"plan rejected: {decision}"],
            }
    return {"plan": plan, "retries": {s: 0 for s in researched}}


def fan_out(state: PrimerState) -> list[Send]:
    sends = []
    for brief in state.get("plan", {}).values():
        sends.append(
            Send(
                "section_researcher",
                {
                    "run_dir": state["run_dir"],
                    "as_of": state["as_of"],
                    "entity": state["entity"],
                    "foundation": state.get("foundation", {}),
                    "brief": brief,
                    "iteration": 0,
                    "critic_notes": [],
                },
            )
        )
    return sends


def section_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    out = RESEARCHER.invoke(payload)
    keep = {}
    for k in ("drafts", "claims", "coverage", "critic_scores", "evidence_refs", "errors"):
        if k in out and out[k]:
            keep[k] = out[k]
    return keep


def key_takeaways(state: PrimerState) -> dict[str, Any]:
    runlog = get_runlog(state["run_dir"])
    drafts = state.get("drafts", {})
    usable = {s: d for s, d in drafts.items() if d.get("coverage") != "unavailable"}
    if not usable:
        return {"key_takeaways": "> Coverage: unavailable — no section drafts to synthesize."}
    usable = {int(k): v for k, v in usable.items()}

    bullets: list[str] = []
    text = None
    if llm_available():
        system = (
            "Synthesize up to 7 Key Takeaways bullets from the section drafts. Each bullet is one sentence, keeps the "
            "citation tokens [src_XXXX] it relies on, introduces no claim absent from the drafts, and ends with "
            "'(Section N)'. Output only the bullets, each starting with '- '."
        )
        user = "\n\n".join(
            f"## Section {s}\n{d['markdown'][:6000]}" for s, d in sorted(usable.items())
        )
        text = invoke_text("synthesizer", system, user, runlog)
    if text:
        bullets = [line.strip() for line in text.splitlines() if line.strip().startswith("- ")][:7]
    store = EvidenceStore.for_run(state["run_dir"], as_of=date.fromisoformat(state["as_of"]))
    claims = list(state.get("claims", []))
    if bullets:
        chk = check_takeaways(bullets, usable, claims, store)
        runlog.validation["takeaways"] = {
            "rejected": chk["rejected"],
            "kept": len(chk["kept"]),
            "n_in": chk["n_in"],
        }
        bullets = chk["kept"]
    if not bullets:
        per_section = max(2, 7 // len(usable))
        for s, d in sorted(usable.items()):
            prose = [
                p.strip()
                for p in d["markdown"].split("\n\n")
                if p.strip() and not p.startswith(("|", ">", "#", "*Caveat", "**"))
            ]
            for sentence in prose[:per_section]:
                first = re.split(r"(?<=[.!?])\s+", sentence)[0]
                if "[src_" in first:
                    bullets.append(f"- {first} (Section {s})")
            if len(bullets) >= 7:
                break
        bullets = bullets[:7]
        chk = check_takeaways(bullets, usable, claims, store)
        bullets = chk["kept"]
        runlog.validation.setdefault("takeaways", {})["template_rejected"] = chk["rejected"]
    md = (
        "*Synthesized from the sections below; each bullet points at the section it summarizes.*\n\n"
        + "\n".join(bullets)
    )
    return {"key_takeaways": md}


def _fmt_claim_value(v: float, unit: str) -> str:
    if unit in ("USD",) and abs(v) >= 1e5:
        return f"${fmt_money(v)}M"
    if unit in ("pure", "pct", "x") or unit.endswith("ratio"):
        return f"{v:,.2f}{'x' if unit == 'x' else ''}"
    if unit == "shares":
        return f"{v / 1e6:,.1f}M shares"
    return f"{v:,.2f}" if not float(v).is_integer() else f"{v:,.0f}"


def inject_conflicts(sections_md: dict[int, str], claims: list[dict[str, Any]]) -> dict[int, str]:
    """Gate 3 (requirements §7): a (metric, period) quoted with two different values is
    flagged in a '> Conflict:' blockquote inside the later section, never reconciled."""
    report = check_numeric(claims)
    out = dict(sections_md)
    for conf in report["conflicts"]:
        later = int(conf["later_section"])
        if later not in out:
            continue
        rows = conf["values"]
        bits = []
        for r in sorted(rows, key=lambda r: r["section"]):
            cites = "".join(f" [{sid}]" for sid in r["source_ids"]) if r["source_ids"] else ""
            bits.append(
                f"{_fmt_claim_value(float(r['value']), r.get('unit', 'USD'))}{cites} (section {r['section']})"
            )
        line = (
            f"> Conflict: {conf['metric'].replace('_', ' ')} for {conf['period']} reported as "
            + " and ".join(bits)
            + "; sources differ and are not reconciled."
        )
        md = out[later]
        if line in md:
            continue
        paras = md.split("\n\n", 1)
        if len(paras) == 2 and paras[0].startswith("*Caveat"):
            out[later] = paras[0] + "\n\n" + line + "\n\n" + paras[1]
        else:
            out[later] = line + "\n\n" + md
    return out


def assemble(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    as_of = date.fromisoformat(state["as_of"])
    store = EvidenceStore.for_run(state["run_dir"], as_of=as_of)
    sections_md: dict[int, str] = {}
    if 1 in inputs.sections:
        sections_md[1] = state.get("key_takeaways", "> Coverage: unavailable — not synthesized.")
    for s, d in state.get("drafts", {}).items():
        sections_md[int(s)] = d["markdown"]
    coverage = {int(k): v for k, v in state.get("coverage", {}).items()}
    if 1 in inputs.sections:
        coverage[1] = "full" if state.get("key_takeaways", "").count("- ") else "unavailable"
    claims = list(state.get("claims", []))
    sections_md = inject_conflicts(sections_md, claims)
    mdx, cited_by, order = render(
        entity=state["entity"],
        as_of=state["as_of"],
        run_id=state["run_id"],
        sections_md=sections_md,
        coverage=coverage,
        claims=claims,
        store=store,
        requested=sorted(inputs.sections),
    )
    path = Path(state["run_dir"]) / "primer.mdx"
    path.write_text(mdx, encoding="utf-8")
    store.write_sources_json(
        cited_by,
        extra={
            "run_id": state["run_id"],
            "entity": state["entity"],
            "uei_set": state["entity"].get("uei_set", []),
            "footnote_order": order,
        },
    )
    return {"mdx_path": str(path), "coverage": coverage}


def validate(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    as_of = date.fromisoformat(state["as_of"])
    store = EvidenceStore.for_run(state["run_dir"], as_of=as_of)
    runlog = get_runlog(state["run_dir"])
    path = Path(state["mdx_path"])
    sections_md = {int(s): d["markdown"] for s, d in state.get("drafts", {}).items()}
    if 1 in inputs.sections:
        sections_md[1] = state.get("key_takeaways", "")
    sections_md = inject_conflicts(sections_md, list(state.get("claims", [])))
    report = run_all(
        mdx_text=path.read_text(encoding="utf-8"),
        mdx_path=path,
        sections_md=sections_md,
        claims=list(state.get("claims", [])),
        coverage={int(k): v for k, v in state.get("coverage", {}).items()},
        requested=sorted(inputs.sections),
        store=store,
        as_of=as_of,
    )
    rep = report.to_dict()
    rep["takeaways"] = runlog.validation.get("takeaways", {})
    runlog.validation = rep
    runlog.meta["market_data"] = _market_summary(state)
    return {"validation": rep}


def _market_summary(state: PrimerState) -> dict[str, Any]:
    from primer.sections.common import _MEMO

    key = f"{Path(state['run_dir']).resolve()}|market_data_adapter"
    ad = _MEMO.get(key)
    try:
        return ad.summary() if ad else {}
    except Exception:
        return {}


def _after_validate(state: PrimerState):
    report = state.get("validation", {})
    notes = {int(k): v for k, v in report.get("section_notes", {}).items()}
    retries = dict(state.get("retries", {}))
    sends = []
    for sec, sec_notes in notes.items():
        if sec not in state.get("plan", {}) or retries.get(sec, 0) >= 2:
            continue
        sends.append(
            Send(
                "section_researcher",
                {
                    "run_dir": state["run_dir"],
                    "as_of": state["as_of"],
                    "entity": state["entity"],
                    "foundation": state.get("foundation", {}),
                    "brief": state["plan"][sec],
                    "iteration": 0,
                    "critic_notes": sec_notes,
                },
            )
        )
    if sends:
        return sends
    return "review"


def bump_retries(state: PrimerState) -> dict[str, Any]:
    """Runs before a retry fan-out so the counter is checkpointed."""
    report = state.get("validation", {})
    notes = {int(k): v for k, v in report.get("section_notes", {}).items()}
    retries = dict(state.get("retries", {}))
    for sec in notes:
        if sec in retries and retries[sec] < 2:
            retries[sec] += 1
    return {"retries": retries}


def review(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    if inputs.debug:
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "message": "Review primer.mdx before export",
                "mdx_path": state["mdx_path"],
                "validation": state.get("validation"),
            }
        )
        if decision not in (None, "approve", True):
            return {"errors": [f"review rejected: {decision}"]}
    return {}


def export(state: PrimerState) -> dict[str, Any]:
    runlog = get_runlog(state["run_dir"])
    run_dir = Path(state["run_dir"])
    (run_dir / "claims.json").write_text(
        json.dumps(state.get("claims", []), indent=1, default=str), encoding="utf-8"
    )
    report = state.get("validation", {})
    runlog.meta["status"] = "ok" if report.get("passed") else "needs_review"
    runlog.meta["retries"] = {str(k): v for k, v in state.get("retries", {}).items()}
    runlog.meta["coverage"] = {str(k): v for k, v in state.get("coverage", {}).items()}
    runlog.write()
    return {}


# ---- graph ---------------------------------------------------------------------------


def build_main(checkpointer=None):
    g = StateGraph(PrimerState)
    g.add_node("intake", intake)
    g.add_node("resolve_entity", resolve_entity)
    g.add_node("foundation_pack", foundation_pack)
    g.add_node("plan_sections", plan_sections)
    g.add_node("section_researcher", section_researcher)
    g.add_node("key_takeaways", key_takeaways)
    g.add_node("assemble", assemble)
    g.add_node("validate", validate)
    g.add_node("bump_retries", bump_retries)
    g.add_node("review", review)
    g.add_node("export", export)

    g.add_edge(START, "intake")
    g.add_edge("intake", "resolve_entity")
    g.add_edge("resolve_entity", "foundation_pack")
    g.add_edge("foundation_pack", "plan_sections")
    g.add_conditional_edges("plan_sections", fan_out, ["section_researcher"])
    g.add_edge("section_researcher", "key_takeaways")
    g.add_edge("key_takeaways", "assemble")
    g.add_edge("assemble", "validate")
    g.add_conditional_edges("validate", _route_validate, ["bump_retries", "review"])
    g.add_conditional_edges("bump_retries", _after_validate, ["section_researcher", "review"])
    g.add_edge("review", "export")
    g.add_edge("export", END)
    return g.compile(checkpointer=checkpointer)


def _route_validate(state: PrimerState) -> str:
    report = state.get("validation", {})
    notes = {int(k): v for k, v in report.get("section_notes", {}).items()}
    retries = state.get("retries", {})
    retryable = [s for s in notes if s in state.get("plan", {}) and retries.get(s, 0) < 2]
    return "bump_retries" if retryable else "review"


def open_checkpointer(run_dir: Path) -> SqliteSaver:
    conn = sqlite3.connect(str(run_dir / "checkpoint.sqlite"), check_same_thread=False)
    return SqliteSaver(conn)


def run_primer(
    inputs: Inputs, run_id: str | None = None, run_dir: Path | None = None
) -> dict[str, Any]:
    if run_id is None or run_dir is None:
        run_id, run_dir = new_run(inputs)
    runlog = get_runlog(run_dir)
    runlog.meta["run_dir"] = str(run_dir)
    graph = build_main(open_checkpointer(run_dir))
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    init: PrimerState = {
        "inputs": inputs.model_dump(mode="json"),
        "run_id": run_id,
        "run_dir": str(run_dir),
    }
    with runlog.timed("total"):
        final = graph.invoke(init, config=config)
    runlog.write()
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "mdx_path": final.get("mdx_path"),
        "validation": final.get("validation"),
        "coverage": final.get("coverage"),
        "sections": {s: SECTION_TITLES[int(s)] for s in final.get("drafts", {})},
    }


def resume_primer(run_dir: Path) -> dict[str, Any]:
    """Resume a killed run from its checkpoint (same thread_id = run_id)."""
    run_id = run_dir.name
    graph = build_main(open_checkpointer(run_dir))
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    final = graph.invoke(None, config=config)
    get_runlog(run_dir).write()
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "mdx_path": final.get("mdx_path"),
        "validation": final.get("validation"),
    }
