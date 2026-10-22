"""Section 8, Management and Governance (requirements §6 row 8).

plan   -> DEF 14A and 10-K Item 10 from the foundation pack; SC 13D/G and Form 4 counts
          from the submissions index (trailing 12 months); web queries (CEO/CFO changes,
          board independence, litigation and regulators, activists)
collect-> reads the proxy and Item 10 text, counts the ownership filings (registered as
          one tier-1 index record when any exist), runs web research within budget
draft  -> officer table and ownership-concentration bullets (deterministic), governance
          claims, narrative (model when configured, else the template digest)
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from primer.llm import invoke_structured
from primer.sections import private
from primer.sections.common import (
    ToolContext,
    caveat_line,
    draft_section,
    expand_queries,
    finalize_draft,
    fmt_date,
    md_table,
    template_digest,
    unavailable_draft,
)
from primer.sections.exhibits_sec08 import (
    Officer,
    governance_exhibit,
    governance_facts,
    officer_rows,
    ownership_rows,
)
from primer.sections.text_tools import exec_officer_rows, exec_officer_section, paragraphs_with
from primer.state import Brief, SectionDraft


class _OfficerRow(BaseModel):
    name: str
    age: int | None = None
    title: str


class _OfficerList(BaseModel):
    officers: list[_OfficerRow] = Field(default_factory=list)


def _officers_from_model(ctx, tenk_text, tenk_sid, proxy_text, proxy_sid):
    """When neither filing carries a table layout the deterministic parser reads (Boeing's
    proxy lists officers in prose), ask the cheap model to extract name, age and title from
    the registered filing text only; rows cite that filing."""
    for text, sid, label in ((proxy_text, proxy_sid, "DEF 14A"), (tenk_text, tenk_sid, "10-K")):
        if not sid or not text:
            continue
        paras = paragraphs_with(text, ["executive officer"], window=2, max_paragraphs=6, min_len=60)
        paras += paragraphs_with(text, ["chief executive officer"], window=1, max_paragraphs=4)
        blob = "\n\n".join(dict.fromkeys(paras))[:30000]
        if len(blob) < 500:
            continue
        out = invoke_structured(
            "query_planner",
            _OfficerList,
            "Extract the company's current executive officers (name, age if stated, title) from "
            "the filing excerpt. Only people the excerpt names as executive officers; never guess. "
            "Return an empty list when the excerpt has none.",
            blob,
            ctx.runlog,
        )
        rows = [o for o in (out.officers if out else []) if o.name and o.title]
        if len(rows) >= 2:
            return [
                Officer(
                    o.name.strip(),
                    str(o.age) if o.age else "n/a",
                    o.title.strip()[:160],
                    sid,
                    label,
                )
                for o in rows[:20]
            ], label
    return [], ""


def _draft_private(
    brief: Brief,
    res: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None,
) -> SectionDraft:
    """Private path: no DEF 14A, no 10-K Item 10, no SC 13D/G or Form 4. The section says so,
    lists the people an SEC Form D names (executive officers, directors, promoters) and
    narrates the leadership pages found by web search."""
    store = ctx.store
    pages = [p["sid"] for p in res.get("pages", []) if store.has(p["sid"])]
    pages += [s for s in ctx.foundation_sources("press_release") if s not in pages]
    forms = ((ctx.foundation.get("private") or {}).get("form_d") or {}).get("filings") or []
    rows = []
    form_sids = []
    for f in forms:
        sid = f.get("source_id")
        if not sid or not store.has(sid):
            continue
        for person in f.get("related") or []:
            rels = ", ".join(person.get("relationships") or []) or "relationship not stated"
            rows.append([person["name"] + f" [{sid}]", rels, fmt_date(f.get("filed"))])
            form_sids.append(sid)
    if not pages and not rows:
        return unavailable_draft(
            brief,
            "a private company files no proxy statement or Form 10-K and no page naming its "
            "leaders was retrieved",
            ctx,
        )
    exhibit_md = ""
    if rows:
        exhibit_md = md_table(["Name", "Relationship per Form D", "Form D filed"], rows[:20], 99)
    notes = [
        "no proxy statement or 10-K exists for a private company, so board independence, "
        "beneficial ownership and executive pay are not available and leaders appear only "
        "where a cited page or Form D names them",
        *(res.get("notes") or []),
    ]
    evidence = list(dict.fromkeys([*form_sids, *pages]))
    pbrief = brief.model_copy(
        update={
            "required_exhibit": "people named in SEC Form D filings, when any were found",
            "structured_first": [],
        }
    )
    narrative = draft_section(
        ctx,
        pbrief,
        evidence_sids=evidence,
        exhibit_md=exhibit_md,
        extra_rules=(
            "This is a private company with no proxy statement or 10-K. Name a leader only when "
            "a cited page names that person in that role, and say the officer list is not "
            "taken from a filing. Litigation, investigations and funding backers only from "
            "the cited pages."
        ),
        critic_notes=critic_notes,
        keywords=["chief executive", "founder", "board", "investor", "lawsuit"],
    ) or template_digest(ctx, evidence, [])
    return finalize_draft(
        brief,
        ctx,
        caveat=caveat_line(
            "a private company files no proxy statement or Form 10-K; the people named here "
            "come from SEC Form D notices and from the web pages cited, not from a filed "
            "officer list, and ownership percentages and pay are not available"
        ),
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=bool(rows),
        claims=[],
        coverage="partial",
        notes=notes,
        exhibit_title="People named in SEC Form D filings" if rows else None,
    )


OWNERSHIP_FORMS = ["SC 13G", "SC 13G/A", "SC 13D", "SC 13D/A"]
INSIDER_FORMS = ["4", "4/A"]


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "tool": "foundation.filing_text",
            "args": {"kinds": ["DEF 14A", "10-K Item 10"]},
            "why": brief.structured_first[0] if brief.structured_first else "proxy statement",
        }
    ]
    if ctx.entity.cik:
        items.append(
            {
                "tool": "edgar.filings",
                "args": {
                    "cik": ctx.entity.cik,
                    "forms": OWNERSHIP_FORMS + INSIDER_FORMS,
                    "since": (ctx.as_of - timedelta(days=365)).isoformat(),
                },
                "why": "SC 13D/G and Form 4 counts from the submissions index",
            }
        )
    for q in expand_queries(brief, ctx):
        items.append(
            {
                "tool": "firecrawl.search",
                "query": q,
                "why": "management, litigation and activist news",
            }
        )
    return items


def _ownership_filings(ctx: ToolContext) -> dict[str, Any]:
    since = ctx.as_of - timedelta(days=365)
    out: dict[str, Any] = {"sc13": [], "form4": [], "sid": None}
    if not ctx.entity.cik:
        return out
    try:
        sc13 = [
            r
            for r in ctx.edgar.filings(ctx.entity.cik, forms=OWNERSHIP_FORMS, since=since)
            if r.filing_date <= ctx.as_of
        ]
        f4 = [
            r
            for r in ctx.edgar.filings(ctx.entity.cik, forms=INSIDER_FORMS, since=since)
            if r.filing_date <= ctx.as_of
        ]
    except Exception as e:
        ctx.log(f"submissions index for ownership filings: {e!r}")
        return out
    out["sc13"] = [r.model_dump(mode="json") for r in sc13]
    out["form4"] = [r.model_dump(mode="json") for r in f4]
    if sc13 or f4:
        cik = ctx.entity.cik
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        rec = ctx.store.register(
            f"# EDGAR submissions index — ownership filings for CIK {cik}\n\n"
            f"Window: {since.isoformat()} to {ctx.as_of.isoformat()}\n\n"
            f"SC 13D/G filings: {len(sc13)}\nForm 4 filings: {len(f4)}\n\n```json\n"
            + json.dumps(
                {
                    "sc13": [
                        {
                            "form": r.form,
                            "filed": r.filing_date.isoformat(),
                            "accession": r.accession,
                        }
                        for r in sc13
                    ],
                    "form4": [
                        {
                            "form": r.form,
                            "filed": r.filing_date.isoformat(),
                            "accession": r.accession,
                        }
                        for r in f4[:50]
                    ],
                },
                indent=1,
            )
            + "\n```\n",
            type="api:edgar_submissions",
            publisher="SEC EDGAR",
            title=f"Submissions index: SC 13D/G and Form 4 filings, trailing 12 months to {ctx.as_of}",
            url=url,
            published_date=ctx.as_of,
            tier=1,
            gov_id=cik,
            meta={"cik": cik, "sc13_count": len(sc13), "form4_count": len(f4)},
            dedupe_key=f"submissions_ownership|{cik}|{ctx.as_of.isoformat()}",
        )
        out["sid"] = rec.source_id
    return out


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    out: dict[str, Any] = {"proxy_sid": None, "tenk_sid": None, "pages": [], "notes": []}
    proxies = ctx.foundation_sources("DEF 14A")
    if proxies:
        out["proxy_sid"] = proxies[0]
    # the officer list comes from a named 10-K section: Part I "Information about our
    # Executive Officers" or Item 10 (whichever heading is followed by officer rows), and
    # the proxy only as the fallback in `officer_rows`
    item10, sid10 = ctx.item_text("10")
    tenks = ctx.foundation_sources("10-K")
    whole = ctx.store.text(tenks[0]) if tenks else ""
    found = exec_officer_section(whole) if whole else None
    if found and (not sid10 or len(exec_officer_rows(item10)) < 3):
        item10, sid10 = found[0], tenks[0]
        out["notes"].append(f"officer list taken from the 10-K '{found[1]}' section")
    elif not sid10 and tenks:
        sid10, item10 = tenks[0], whole
        out["notes"].append(
            "10-K executive-officer heading not found; officer table searched in the whole 10-K"
        )
    out["tenk_sid"], out["tenk_text"] = sid10, item10
    out["ownership_filings"] = _ownership_filings(ctx)
    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    queries = queries[: max(0, ctx.budget.searches - ctx.budget.used_searches)]
    rows = ctx.web_research(queries, type="news") if queries else []
    out["notes"].extend(ctx.budget.notes)  # the draft node rebuilds the context; keep the notes
    out["pages"] = [
        {"sid": r["source_id"], "url": r["url"], "query": r.get("query")}
        for r in rows
        if r.get("source_id")
    ]
    return out


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    res = tool_results or {}
    if private.is_private(ctx.entity):
        return _draft_private(brief, res, ctx, critic_notes)
    proxy_sid, tenk_sid = res.get("proxy_sid"), res.get("tenk_sid")
    if not proxy_sid and not tenk_sid:
        reason = res.get("error") or "neither a DEF 14A nor a 10-K is in the foundation pack"
        return unavailable_draft(brief, str(reason)[:200], ctx)
    proxy_text = ctx.store.text(proxy_sid) if proxy_sid and ctx.store.has(proxy_sid) else ""
    tenk_text = res.get("tenk_text") or ""
    officers, basis = officer_rows(tenk_text, tenk_sid, proxy_text, proxy_sid)
    if len(officers) < 3:
        extra, src = _officers_from_model(ctx, tenk_text, tenk_sid, proxy_text, proxy_sid)
        if extra:
            officers, basis = extra, src
    holders = ownership_rows(proxy_text, proxy_sid) if proxy_sid else []
    facts = governance_facts(proxy_text, proxy_sid)
    filing_dates = {
        s: fmt_date(ctx.store.get(s).published_date)
        for s in (proxy_sid, tenk_sid)
        if s and ctx.store.has(s)
    }
    ex = governance_exhibit(
        officers=officers, holders=holders, facts=facts, filing_dates=filing_dates
    )

    own = res.get("ownership_filings") or {}
    sc13, f4, own_sid = own.get("sc13") or [], own.get("form4") or [], own.get("sid")
    pages = [p["sid"] for p in res.get("pages", []) if ctx.store.has(p["sid"])]
    evidence = [s for s in [proxy_sid, tenk_sid, own_sid, *pages] if s and ctx.store.has(s)]

    coverage = "full" if (ex.present and holders and proxy_sid) else "partial"
    notes = list(ex.notes) + list(res.get("notes", []))
    if not proxy_sid:
        notes.append("no DEF 14A in the foundation pack; ownership and pay not available")
    if not sc13:
        notes.append("no SC 13D/G in the trailing 12 months of the submissions index")
    if not f4:
        notes.append("no Form 4 in the trailing 12 months of the submissions index")

    officer_src = next((o for o in officers), None)
    list_date = filing_dates.get(officer_src.source_id, "n.d.") if officer_src else "n.d."
    caveat = caveat_line(
        f"The officer list is as of the {basis} filed {list_date}; later appointments or departures "
        "appear only in 8-K Item 5.02 filings and news. Ownership percentages are beneficial ownership "
        "as reported in the proxy table, which is not voting power"
    )

    # lead sentences for the keyless path
    leads: list[str] = []
    if officers:
        o = officers[0]
        leads.append(
            f"The {basis} lists {len([x for x in officers if x.source_label == basis])} executive officers, led by {o.name}, {o.age}, {o.title} [{o.source_id}]."
        )
    if facts.board_sentence and facts.source_id:
        leads.append(
            f"The board has {facts.board_size} directors, {facts.board_independent} of them independent under the exchange's listing standards [{facts.source_id}]."
        )
    if holders:
        top = [h for h in holders if not h.is_group][:3]
        if top:
            leads.append(
                "The largest beneficial owners in the proxy table are "
                + ", ".join(f"{h.holder} ({h.pct:.1f}%)" for h in top)
                + f" [{top[0].source_id}]."
            )
    if facts.ceo_pay_usd and facts.source_id:
        leads.append(
            f"CEO total compensation was ${facts.ceo_pay_usd / 1e6:,.1f} million"
            + (f" in {facts.ceo_pay_year}" if facts.ceo_pay_year else "")
            + (
                f", and the say-on-pay vote received {facts.say_on_pay_pct:.0f}% support"
                if facts.say_on_pay_pct is not None
                else ""
            )
            + f" [{facts.source_id}]."
        )
    if own_sid:
        leads.append(
            f"The submissions index shows {len(sc13)} SC 13D/G and {len(f4)} Form 4 filings in the trailing 12 months [{own_sid}]."
        )
    else:
        leads.append(
            "The submissions index shows no SC 13D/G or Form 4 filing in the trailing 12 months, so no activist or insider-transaction record is cited."
        )

    facts_json = [c.model_dump() for c in ex.claims]
    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts_json,
        exhibit_md=ex.officers_md + "\n\n" + ex.ownership_md,
        extra_rules=(
            "State the filing date of the officer list. Keep beneficial ownership distinct from voting "
            "power. Litigation, investigations and activist matters only from the cited pages."
        ),
        critic_notes=critic_notes,
        keywords=["independent", "beneficial", "compensation", "lawsuit", "investigation"],
    )
    if narrative is None:
        narrative = template_digest(ctx, evidence, leads)

    exhibit_md = ""
    if ex.officers_md:
        exhibit_md += ex.officers_md
    if ex.ownership_md:
        exhibit_md += (
            ("\n\n" if exhibit_md else "") + "**Ownership concentration**\n\n" + ex.ownership_md
        )
    return finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=ex.present,
        claims=ex.claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Executive officers and ownership",
    )
