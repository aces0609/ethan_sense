"""Section 14, Recent Developments (requirements §6 row 14).

plan   -> 8-Ks in the trailing 12 months from the foundation index, the IR newsroom links
          from the site map, then the web queries with a one-year recency filter
collect-> fetches up to twelve 8-K primary documents (tier 1, by accession, dated by
          filing date), scrapes up to four dated IR releases, runs web research within the
          remaining scrape budget
draft  -> dated event log newest first (recency rule applied at build time), the
          event-count claim, narrative (model when configured, else the template digest)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from primer.evidence.html import html_to_text
from primer.sections.common import (
    ToolContext,
    caveat_line,
    draft_section,
    expand_queries,
    finalize_draft,
    scrape_url,
    template_digest,
    unavailable_draft,
)
from primer.sections.exhibits_sec14 import (
    Event,
    date_in_url,
    eightk_event,
    event_log,
    first_body_sentence,
)
from primer.state import Brief, SectionDraft

MAX_8K = 12
MAX_IR = 4


def _window(ctx: ToolContext) -> tuple[date, date]:
    return ctx.as_of - timedelta(days=365), ctx.as_of


def _recent_8ks(ctx: ToolContext) -> list[dict[str, Any]]:
    lo, hi = _window(ctx)
    out = []
    for r in ctx.foundation_filings("8-K"):
        fd = date.fromisoformat(str(r["filing_date"])[:10])
        if lo <= fd <= hi:
            out.append(r)
    out.sort(key=lambda r: r["filing_date"], reverse=True)
    return out[:MAX_8K]


def _ir_news_links(ctx: ToolContext) -> list[tuple[str, date]]:
    lo, hi = _window(ctx)
    links = ((ctx.foundation.get("ir") or {}).get("links") or {}).get("news") or []
    dated = []
    for u in links:
        d = date_in_url(u)
        if d and lo <= d <= hi:
            dated.append((u, d))
    dated.sort(key=lambda t: t[1], reverse=True)
    return dated[:MAX_IR]


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    lo, hi = _window(ctx)
    items: list[dict[str, Any]] = [
        {
            "tool": "edgar.8k_documents",
            "args": {
                "accessions": [r["accession"] for r in _recent_8ks(ctx)],
                "since": lo.isoformat(),
                "until": hi.isoformat(),
            },
            "why": brief.structured_first[0]
            if brief.structured_first
            else "8-Ks trailing 12 months",
        },
        {
            "tool": "firecrawl.scrape",
            "args": {"urls": [u for u, _ in _ir_news_links(ctx)]},
            "why": "IR newsroom releases with a date in the URL",
        },
    ]
    for q in expand_queries(brief, ctx):
        items.append(
            {
                "tool": "firecrawl.search",
                "query": q,
                "tbs": "qdr:y",
                "why": "news in the trailing year",
            }
        )
    return items


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    out: dict[str, Any] = {"eightks": [], "ir_pages": [], "pages": [], "notes": []}
    for r in _recent_8ks(ctx):
        try:
            text = html_to_text(ctx.edgar.get_text(r["url"]))
        except Exception as e:
            ctx.log(f"8-K {r['accession']} fetch failed: {e!r}")
            continue
        fd = date.fromisoformat(str(r["filing_date"])[:10])
        rec = ctx.store.register(
            text,
            type="8-K",
            publisher=ctx.entity.name,
            title=f"Current report (8-K) filed {fd}, Items {r.get('items') or 'n/a'}",
            url=r["url"],
            published_date=fd,
            tier=1,
            gov_id=r["accession"],
            meta={
                "form": "8-K",
                "cik": ctx.entity.cik,
                "items": r.get("items"),
                "report_date": r.get("report_date"),
                "chars": len(text),
            },
            dedupe_key=f"filing|{r['accession']}",
        )
        out["eightks"].append(
            {
                "sid": rec.source_id,
                "accession": r["accession"],
                "filing_date": fd.isoformat(),
                "items": r.get("items"),
            }
        )
    for url, d in _ir_news_links(ctx):
        sid = scrape_url(ctx, url, type="press_release", published_date=d)
        if sid:
            out["ir_pages"].append({"sid": sid, "url": url, "date": d.isoformat()})
    if not (ctx.foundation.get("ir") or {}).get("links"):
        out["notes"].append("no IR site map in the foundation pack")
    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    queries = queries[: max(0, ctx.budget.searches - ctx.budget.used_searches)]
    remaining = max(0, ctx.budget.scrapes - ctx.budget.used_scrapes)
    rows = (
        ctx.web_research(queries, type="news", tbs="qdr:y", max_scrapes=remaining)
        if queries
        else []
    )
    out["notes"].extend(ctx.budget.notes)  # the draft node rebuilds the context; keep the notes
    out["pages"] = [{"sid": r["source_id"], "url": r["url"]} for r in rows if r.get("source_id")]
    return out


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    res = tool_results or {}
    events: list[Event] = []
    undated: list[str] = []
    for k in res.get("eightks", []):
        sid = k["sid"]
        if not ctx.store.has(sid):
            continue
        rec = ctx.store.get(sid)
        if not rec.published_date:
            continue
        event, sentence = eightk_event(ctx.store.text(sid), k.get("items"))
        events.append(Event(rec.published_date, event, "8-K (SEC)", sentence, sid, "8-K"))
    for p in res.get("ir_pages", []) + res.get("pages", []):
        sid = p["sid"]
        if not ctx.store.has(sid):
            continue
        rec = ctx.store.get(sid)
        if not rec.published_date:
            undated.append(rec.title)
            continue
        kind = "press" if any(x["sid"] == sid for x in res.get("ir_pages", [])) else "news"
        stype = "press release (IR site)" if kind == "press" else f"news ({rec.publisher})"
        implication = (
            first_body_sentence(ctx.store.text(sid))
            if rec.access == "full"
            else "access limited: headline only"
        )
        events.append(Event(rec.published_date, rec.title[:160], stype, implication, sid, kind))
    ex = event_log(events, ctx.as_of, undated=undated)
    evidence = [e.source_id for e in ex.events]
    fetched = [k["sid"] for k in res.get("eightks", [])] + [
        p["sid"] for p in res.get("ir_pages", []) + res.get("pages", [])
    ]
    if not fetched or not any(ctx.store.has(s) for s in fetched):
        return unavailable_draft(
            brief,
            res.get("error")
            or "no 8-K, IR release or news page could be fetched for the trailing 12 months",
            ctx,
        )

    coverage = "full" if ex.eightk_rows else "partial"
    notes = list(ex.notes) + list(res.get("notes", []))
    if not res.get("eightks"):
        notes.append("no 8-K filed in the trailing 12 months of the submissions index")
    lo, hi = _window(ctx)
    caveat = caveat_line(
        f"The log covers sources dated {lo.isoformat()} to {hi.isoformat()} only (the recency rule); the "
        "implication column restates the source's own sentence and is not an assessment, and 8-K rows are "
        "dated by filing date"
    )
    leads: list[str] = []
    if ex.events:
        leads.append(
            f"{len(ex.events)} dated events are logged between {ex.events[-1].date.isoformat()} and {ex.events[0].date.isoformat()}, {ex.eightk_rows} of them from 8-K filings"
            + "".join(f" [{s}]" for s in evidence[:3])
            + "."
        )
    for e in ex.events[:4]:
        leads.append(
            f"On {e.date.isoformat()} the {e.source_type.split(' (')[0]} reported: {e.implication.rstrip('.')} [{e.source_id}]."
        )
    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=[c.model_dump() for c in ex.claims],
        exhibit_md=ex.markdown,
        extra_rules=(
            "Cite only the sources listed (all dated within the trailing 12 months). Group the events by "
            "theme (deals, results, governance, financing) and quote facts, not press-release adjectives."
        ),
        critic_notes=critic_notes,
        keywords=["announce", "agreement", "approve", "appoint"],
    )
    if narrative is None:
        narrative = template_digest(ctx, evidence, leads)
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
        exhibit_title="Event log (trailing 12 months, newest first)",
    )
