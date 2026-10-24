"""Section 7, Key Acquisition History (requirements §6 row 7).

plan   -> EDGAR full-text search for merger/acquisition 8-Ks (trailing five years), the
          10-K acquisitions note and the 10-Q business-combinations note, then the web
          queries (press releases, M&A news)
collect-> fetches the top 8-K primary documents (tier 1, registered by accession), reads
          the foundation notes, runs web research within budget; optionally the Firecrawl
          /agent gap-filler when budgets.yaml sets `use_agent` for section 7
draft  -> acquisition table (deterministic regex rows, one claim per value), narrative
          (model when configured, else the template digest), caveat and coverage
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from primer.clients.edgar import filing_primary_url
from primer.config import section_budget
from primer.evidence.html import html_to_text
from primer.sections.common import (
    ToolContext,
    caveat_line,
    draft_section,
    expand_queries,
    finalize_draft,
    fmt_date,
    template_digest,
    unavailable_draft,
)
from primer.sections.exhibits_sec07 import (
    Deal,
    acquisition_table,
    extract_deals,
    fmt_value,
    undisclosed_deals,
)
from primer.sections.text_tools import paragraphs_with
from primer.state import Brief, SectionDraft

FULLTEXT_HITS = 6
FULLTEXT_YEARS = 5


class _AgentDeal(BaseModel):
    target: str = Field(description="Name of the acquired or divested company or business")
    date: str | None = Field(None, description="Announcement date, ISO format if known")
    value_usd: float | None = Field(None, description="Deal value in US dollars")
    status: str | None = Field(None, description="announced, completed or divested")
    segment: str | None = Field(None, description="Business segment the target joins")
    rationale: str | None = Field(None, description="One clause from the release stating why")


class _AgentDeals(BaseModel):
    deals: list[_AgentDeal] = Field(default_factory=list)


def _fulltext_query(ctx: ToolContext) -> str:
    return f'"{ctx.co}" AND ("merger agreement" OR "acquisition")'


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if ctx.entity.cik:
        items.append(
            {
                "tool": "edgar.fulltext_search",
                "args": {
                    "query": _fulltext_query(ctx),
                    "forms": ["8-K"],
                    "start": (ctx.as_of - timedelta(days=365 * FULLTEXT_YEARS)).isoformat(),
                    "end": ctx.as_of.isoformat(),
                    "cik": ctx.entity.cik,
                },
                "why": brief.structured_first[0] if brief.structured_first else "8-K deals",
            }
        )
    items.append(
        {
            "tool": "foundation.notes",
            "args": {"items": ["10-K acquisitions note", "10-Q business combinations note"]},
            "why": "business-combinations notes from the foundation pack",
        }
    )
    for q in expand_queries(brief, ctx):
        items.append({"tool": "firecrawl.search", "query": q, "why": "press releases and M&A news"})
    return items


def _register_8k(
    ctx: ToolContext, accession: str, filename: str, src: dict[str, Any]
) -> str | None:
    cik = ctx.entity.cik or ""
    url = filing_primary_url(cik, accession, filename)
    text = html_to_text(ctx.edgar.get_text(url))
    fdate = src.get("file_date")
    rec = ctx.store.register(
        text,
        type="8-K",
        publisher=ctx.entity.name,
        title=f"Current report (8-K) filed {fdate}: {(src.get('file_description') or 'merger/acquisition')[:120]}",
        url=url,
        published_date=date.fromisoformat(fdate) if fdate else None,
        tier=1,
        gov_id=accession,
        meta={
            "form": "8-K",
            "cik": cik,
            "fulltext_query": _fulltext_query(ctx),
            "chars": len(text),
        },
        dedupe_key=f"filing|{accession}",
    )
    return rec.source_id


DEAL_ITEMS = ("1.01", "2.01")  # entry into a material agreement; completion of an acquisition


def _deal_8ks_from_index(ctx: ToolContext, out: dict[str, Any]) -> None:
    """EDGAR full-text search is refused for this client (403) or returns nothing, so the deal
    8-Ks are picked from the filing index instead: Item 1.01 or 2.01 filings of the last five
    years, newest first. The documents are read exactly as search hits would be."""
    since = ctx.as_of - timedelta(days=365 * FULLTEXT_YEARS)
    try:
        refs = ctx.edgar.filings(ctx.entity.cik, forms=["8-K"], since=since)
    except Exception as e:  # noqa: BLE001 - the section falls back to notes and press
        ctx.log(f"8-K index failed: {e!r}")
        return
    deals = [
        r
        for r in refs
        if r.filing_date <= ctx.as_of
        and r.primary_document
        and any(c in (r.items or "").split(",") for c in DEAL_ITEMS)
    ]
    deals.sort(key=lambda r: r.filing_date, reverse=True)
    for r in deals[:FULLTEXT_HITS]:
        src = {
            "file_date": r.filing_date.isoformat(),
            "file_description": f"Item {r.items}",
        }
        try:
            sid = _register_8k(ctx, r.accession, r.primary_document, src)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"8-K {r.accession} fetch failed: {e!r}")
            continue
        if sid:
            out["filings"].append(
                {
                    "sid": sid,
                    "kind": "8-K",
                    "date": r.filing_date.isoformat(),
                    "accession": r.accession,
                }
            )
    if out["filings"]:
        out["notes"].append(
            "8-Ks were chosen by item code (1.01, 2.01) from the filing index, not by text search"
        )


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    out: dict[str, Any] = {"filings": [], "pages": [], "agent": None, "notes": []}
    ft = next((i for i in plan_items if i.get("tool") == "edgar.fulltext_search"), None)
    if ft is not None:
        a = ft["args"]
        try:
            res = ctx.edgar.fulltext_search(
                a["query"],
                forms=a.get("forms"),
                start=date.fromisoformat(a["start"]),
                end=date.fromisoformat(a["end"]),
                cik=a.get("cik"),
            )
            hits = ((res or {}).get("hits") or {}).get("hits") or []
        except Exception as e:
            ctx.log(f"fulltext search failed: {e!r}")
            hits = []
            out["notes"].append("EDGAR full-text search failed")
        out["fulltext_hits"] = len(hits)
        for h in hits[:FULLTEXT_HITS]:
            _id = str(h.get("_id") or "")
            if ":" not in _id:
                continue
            accession, filename = _id.split(":", 1)
            src = h.get("_source") or {}
            try:
                sid = _register_8k(ctx, accession, filename, src)
            except Exception as e:
                ctx.log(f"8-K {accession} fetch failed: {e!r}")
                continue
            if sid:
                out["filings"].append(
                    {
                        "sid": sid,
                        "kind": "8-K",
                        "date": src.get("file_date"),
                        "accession": accession,
                    }
                )
    if not out["filings"] and ctx.entity.cik:
        _deal_8ks_from_index(ctx, out)
    # foundation notes: 10-K acquisitions note, 10-Q business-combinations note
    item8, sid8 = ctx.item_text("8")
    if not sid8:
        sids = ctx.foundation_sources("10-K")
        if sids:
            item8, sid8 = ctx.store.text(sids[0]), sids[0]
    if sid8:
        paras = paragraphs_with(item8, ["acqui"], window=0, max_paragraphs=8) or paragraphs_with(
            item8, ["divest"], window=0, max_paragraphs=4
        )
        out["filings"].append(
            {
                "sid": sid8,
                "kind": "10-K",
                "date": fmt_date(ctx.store.get(sid8).published_date),
                "text": "\n\n".join(paras),
            }
        )
    for i, sid in enumerate(ctx.foundation_sources("10-Q")):
        item1, s = ctx.item_text("1", kind="10-Q", index=i)
        text = item1 if s else ctx.store.text(sid)
        paras = paragraphs_with(text, ["business combination"], window=0, max_paragraphs=4)
        paras = paras or paragraphs_with(text, ["acqui"], window=0, max_paragraphs=4)
        if paras:
            out["filings"].append(
                {
                    "sid": sid,
                    "kind": "10-Q",
                    "date": fmt_date(ctx.store.get(sid).published_date),
                    "text": "\n\n".join(paras),
                }
            )
    # web: press releases and M&A news
    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    queries = queries[: max(0, ctx.budget.searches - ctx.budget.used_searches)]
    rows = ctx.web_research(queries, type="news") if queries else []
    out["notes"].extend(ctx.budget.notes)  # the draft node rebuilds the context; keep the notes
    for r in rows:
        if r.get("source_id"):
            out["pages"].append({"sid": r["source_id"], "url": r["url"], "kind": "press"})
    # optional /agent gap-filler (off unless budgets.yaml sets sections.7.use_agent)
    if ctx.firecrawl and section_budget(7).get("use_agent") and out["pages"]:
        urls = [p["url"] for p in out["pages"]][:6]
        try:
            data = ctx.firecrawl.agent(
                f"List every acquisition or divestiture by {ctx.co} described on these pages with "
                "target, announcement date, value in USD, status, segment and one rationale clause.",
                schema=_AgentDeals.model_json_schema(),
                urls=urls,
                estimate_credits=10,
            )
            parsed = _AgentDeals.model_validate(data if isinstance(data, dict) else {})
            rec = ctx.store.register(
                "# Firecrawl /agent extraction (acquisitions)\n\nURLs:\n"
                + "\n".join(f"- {u}" for u in urls)
                + "\n\n```json\n"
                + json.dumps(parsed.model_dump(), indent=1)
                + "\n```\n",
                type="api:firecrawl_agent",
                publisher="Firecrawl agent over company press releases",
                title=f"Structured deal extraction for {ctx.co} ({len(parsed.deals)} rows)",
                url=urls[0],
                published_date=ctx.as_of,
                tier=2,
                meta={"urls": urls, "agent": True},
                dedupe_key=f"agent|sec7|{ctx.entity.cik or ctx.co}|{ctx.as_of.isoformat()}",
            )
            out["agent"] = {"sid": rec.source_id, "deals": parsed.model_dump()["deals"]}
        except Exception as e:  # the gap-filler never fails the section
            ctx.log(f"agent extraction failed: {e!r}")
            out["notes"].append("Firecrawl /agent gap-filler failed")
    return out


def _deals_from_results(ctx: ToolContext, results: dict[str, Any]) -> list[Deal]:
    deals: list[Deal] = []
    for f in results.get("filings", []):
        sid = f["sid"]
        if not ctx.store.has(sid):
            continue
        text = f.get("text") if f.get("text") is not None else ctx.store.text(sid)
        rec = ctx.store.get(sid)
        deals.extend(extract_deals(text, sid, f["kind"], rec.published_date))
    for p in results.get("pages", []):
        sid = p["sid"]
        if not ctx.store.has(sid):
            continue
        rec = ctx.store.get(sid)
        if rec.access != "full":
            continue
        kind = "press" if any(k in rec.url for k in ("press", "news", "release")) else "news"
        page_text = ctx.store.text(sid)
        deals.extend(extract_deals(page_text, sid, kind, rec.published_date, limit=6))
        if ctx.entity.private:
            # most acquisitions by a private company state no price: keep them, value undisclosed
            deals.extend(
                undisclosed_deals(
                    page_text,
                    sid,
                    kind,
                    # an encyclopedia page carries no date of its own for the deals it lists
                    None if "wikipedia.org" in (rec.url or "") else rec.published_date,
                    [ctx.entity.name, ctx.co, *ctx.entity.aliases],
                )
            )
    agent = results.get("agent")
    if agent and ctx.store.has(agent["sid"]):
        for d in agent.get("deals", []):
            if not d.get("target") or not d.get("value_usd"):
                continue
            deals.append(
                Deal(
                    target=str(d["target"]),
                    value_usd=float(d["value_usd"]),
                    status=str(d.get("status") or "announced").lower(),
                    date=str(d.get("date") or "n.d.")[:10],
                    date_basis="sentence" if d.get("date") else "none",
                    segment=str(d.get("segment") or "n/a"),
                    rationale=str(d.get("rationale") or "n/a")[:160],
                    sentence="",
                    source_id=agent["sid"],
                    source_kind="agent",
                )
            )
    return deals


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    results = tool_results or {}
    if results.get("error") and not results.get("filings"):
        return unavailable_draft(brief, f"collect failed: {results['error']}"[:200], ctx)
    deals = _deals_from_results(ctx, results)
    ex = acquisition_table(deals, section=7)
    all_sids = [f["sid"] for f in results.get("filings", [])] + [
        p["sid"] for p in results.get("pages", [])
    ]
    all_sids = [s for s in dict.fromkeys(all_sids) if ctx.store.has(s)]
    if not all_sids:
        return unavailable_draft(brief, "no 8-K, 10-K note or press page could be fetched", ctx)

    coverage = "full" if ex.filing_rows else "partial"
    notes = list(ex.notes) + list(results.get("notes", []))
    if not ex.present:
        notes.append(
            "no acquisition or divestiture sentence with a dollar value matched in the fetched text"
        )
    elif not ex.filing_rows:
        notes.append("deal rows come from press pages only; no 8-K or 10-K note sentence matched")
    n_8k = sum(1 for f in results.get("filings", []) if f["kind"] == "8-K")
    if results.get("fulltext_hits") is not None:
        notes.append(
            f"EDGAR full-text search returned {results['fulltext_hits']} 8-K hits, {n_8k} fetched"
        )

    caveat = caveat_line(
        "Deal rows are extracted by pattern from 8-K, 10-K/10-Q note and press text; values are as "
        "stated at announcement (cash consideration where the source says so), not adjusted for "
        "closing terms, and rationale cells quote the source wording rather than assess the deal"
    )
    facts = [c.model_dump() for c in ex.claims]
    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=all_sids,
        facts=facts,
        exhibit_md=ex.markdown,
        extra_rules="Do not restate the table; explain the pattern of deals (segments, size, cadence) and the status of pending ones.",
        critic_notes=critic_notes,
        keywords=["acqui", "divest", "merger"],
    )
    if narrative is None:
        leads = []
        for r in ex.rows[:6]:
            v, sid, _k = r.values[0]
            when = r.date if r.date_basis in ("sentence", "year") else f"{r.date} ({r.date_basis})"
            leads.append(
                f"{r.target}: {r.status}, {when}, {fmt_value(v)}"
                + (f", {r.segment} segment" if r.segment != "n/a" else "")
                + f" [{sid}]."
            )
        if not leads:
            leads.append(
                f"No acquisition or divestiture with a stated value was found in the {len(all_sids)} documents read for the trailing five years"
                + "".join(f" [{s}]" for s in all_sids[:3])
                + "."
            )
        narrative = template_digest(ctx, all_sids, leads)
    return finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=ex.markdown,
        exhibit_present=ex.present,
        claims=ex.claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Acquisitions and divestitures",
    )
