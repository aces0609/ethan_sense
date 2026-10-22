"""Section 3, Products, Services and Technology (requirements §6 row 3).

plan    -> 10-K Item 1 product paragraphs per segment (foundation), then the three template
           queries plus one trade-press query per top segment by revenue
collect -> web research within the section budget
draft   -> product/program table (status, key metric, every row cites the 10-K), claims for
           numeric key metrics, narrative that names the competitors the 10-K names and any
           scraped page that sets a product against a competitor (the common failure is "no
           competitor comparison"); keyless path builds the table from the 10-K alone
"""

from __future__ import annotations

import re
from typing import Any

from primer.config import peer_fallbacks
from primer.sections import common, private
from primer.sections import text_tools as tt
from primer.sections.common import ToolContext
from primer.sections.exhibits_sec02 import dropped_note, find_segment_table
from primer.sections.exhibits_sec03 import product_table, segment_paragraphs
from primer.state import Brief, SectionDraft

SECTION = 3
QA_MARK = re.compile(r"operator:?\**[^\n]{0,200}?question", re.I)


def prepared_remarks(transcript: str) -> str:
    """Text before the first 'Operator: ... question' marker, speaker markers removed."""
    m = QA_MARK.search(transcript)
    text = transcript[: m.start()] if m else transcript
    return re.sub(r"\*\*[^*\n]+\*\*:?\s*", "", text)


def _segments(ctx: ToolContext) -> list[str]:
    st = find_segment_table(ctx)
    if st.table is not None:
        return [r.label for r in sorted(st.table.rows, key=lambda r: -r.values[0])]
    return st.segment_names


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    segs = _segments(ctx)
    items: list[dict[str, Any]] = [
        {
            "tool": "foundation.item_text",
            "args": {"item": "1", "paragraphs": segs},
            "why": "10-K Item 1 product paragraphs per segment",
        },
        {
            "tool": "foundation.earnings_release",
            "args": {"index": 0},
            "why": "key metrics (deliveries, units) named in the latest release",
        },
    ]
    for q in common.expand_queries(brief, ctx):
        items.append({"tool": "firecrawl.search", "query": q, "why": "product pages and launches"})
    for seg in segs[:2]:
        items.append(
            {
                "tool": "firecrawl.search",
                "query": f"{ctx.co} {seg} {ctx.fy}",
                "why": "sector trade press by product line",
            }
        )
    return items


def _queries(plan_items: list[dict[str, Any]]) -> list[str]:
    qs = [i.get("query", "") for i in plan_items if i.get("tool") == "firecrawl.search"]
    return [q for q in dict.fromkeys(q.strip() for q in qs) if q]


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    queries = _queries(plan_items)
    remaining = max(0, ctx.budget.searches - ctx.budget.used_searches)
    run = queries[:remaining]
    dropped = len(queries) - len(run)
    prefer = ctx.brief.trade_press if ctx.brief else []
    hits = ctx.web_research(run, type="news", prefer_domains=prefer) if run else []
    notes = list(ctx.budget.notes)
    if dropped_note(dropped):
        notes.append(dropped_note(dropped) or "")
    if not run and ctx.firecrawl is None:
        notes.append("Firecrawl not configured; web research skipped")
    return {
        "web": [
            {k: h.get(k) for k in ("url", "title", "domain", "query", "source_id")} for h in hits
        ],
        "notes": list(dict.fromkeys(notes)),
        "searched": len(run),
    }


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    if private.is_private(ctx.entity):
        return private.draft_web_only(
            brief, ctx, tool_results, missing="10-K", critic_notes=critic_notes
        )
    item1, k_sid = ctx.item_text("1")
    if not item1 or not k_sid:
        return common.unavailable_draft(brief, "no 10-K Item 1 in the foundation pack", ctx)
    segs = _segments(ctx)
    quarters = ctx.quarters()
    rel_sid = next((q["release_sid"] for q in quarters if q.get("release_sid")), None)
    rel_text = ctx.store.text(rel_sid) if rel_sid and ctx.store.has(rel_sid) else ""
    tr_sid = next((q["transcript_sid"] for q in quarters if q.get("transcript_sid")), None)
    remarks = prepared_remarks(ctx.store.text(tr_sid)) if tr_sid and ctx.store.has(tr_sid) else ""

    ex = product_table(
        item1,
        k_sid,
        segs,
        release=(rel_text, rel_sid),
        remarks=(remarks, tr_sid),
        fy_label=f"FY{ctx.fy}",
        section=SECTION,
    )
    notes = list(tool_results.get("notes") or []) + list(ex.notes)
    web = [w for w in tool_results.get("web") or [] if w.get("source_id")]
    web_sids = [w["source_id"] for w in web]

    peers = [p["name"] for p in ctx.foundation.get("peers") or []]
    comp_paras = tt.paragraphs_with(item1, ["compet"], window=0, max_paragraphs=2)
    lead = _lead_sentences(ctx, ex, k_sid, segs, comp_paras, peers, web)

    evidence = [k_sid, *([rel_sid] if rel_sid else []), *([tr_sid] if tr_sid else []), *web_sids]
    facts = [c.model_dump(mode="json") for c in ex.claims] + [
        {
            "fact": "products by segment (10-K)",
            "value": [[r.segment, r.product, r.status, r.metric] for r in ex.rows],
            "source_ids": [k_sid],
        },
        {"fact": "competitors named in the 10-K", "value": peers, "source_ids": [k_sid]},
    ]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts,
        exhibit_md=ex.markdown,
        extra_rules=(
            "Compare at least one product line with a named competitor's offering when a cited "
            "source supports it. Attribute product capabilities to the company's own filing or a "
            "trade-press source; never state marketing copy as fact."
        ),
        critic_notes=critic_notes,
        keywords=["program", "product", "deliver", "compet"],
    ) or common.template_digest(ctx, evidence, lead)

    web_ok = tool_results.get("searched", 0) > 0 and not any(
        "Firecrawl not configured" in n for n in notes
    )
    coverage = "full" if (ex.present and web_ok and not ctx.budget.exhausted) else "partial"
    if ex.present and not web_ok:
        notes.append("web buckets (product pages, press releases, trade press) not read")
    caveat = common.caveat_line(
        f"product and program descriptions are the company's own 10-K language{' and the latest earnings release' if rel_sid else ''}; "
        "status reflects the words used in the filing, not an independent programme assessment, and "
        "trade-press pages are context only"
    )
    return common.finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=ex.markdown,
        exhibit_present=ex.present,
        claims=ex.claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Products and programs by segment (10-K Item 1)",
    )


def _lead_sentences(
    ctx: ToolContext,
    ex: Any,
    k_sid: str,
    segs: list[str],
    comp_paras: list[str],
    peers: list[str],
    web: list[dict[str, Any]],
) -> list[str]:
    cite = f" [{k_sid}]"
    out: list[str] = []
    item1, _ = ctx.item_text("1")
    paras = segment_paragraphs(item1, segs)
    for seg in segs:
        prods = [r.product for r in ex.rows if r.segment == seg]
        if prods:
            out.append(f"{seg}: the 10-K names {', '.join(prods[:6])}{cite}")
        elif seg in paras:
            out.append(f"{seg}: {tt.sentences(paras[seg])[0][:220]}{cite}")
    for r in ex.rows:
        if r.metric_value is not None:
            out.append(
                f"Key metric for {r.product}: {r.metric.split(' (')[0]}"
                + "".join(f" [{s}]" for s in r.source_ids)
            )
    if comp_paras:
        first = tt.sentences(comp_paras[0])[0][:300]
        out.append(f"On competition, the 10-K states: {first}{cite}")
    elif peers:
        out.append(f"The 10-K names {', '.join(peers)} as competitors{cite}")
    # competitor comparison from scraped pages (only when the page text names a peer)
    peer_keys = [
        k
        for k in [p.split()[0] for p in peers if p] + peer_fallbacks(ctx.entity.sector_tag)
        if k.lower() not in ctx.co.lower()  # never list the company against itself
    ]
    for w in web:
        sid = w["source_id"]
        if not ctx.store.has(sid):
            continue
        text = ctx.store.text(sid)
        named = [
            k for k in dict.fromkeys(peer_keys) if re.search(r"\b" + re.escape(k) + r"\b", text)
        ]
        if named:
            rec = ctx.store.get(sid)
            out.append(
                f"{rec.publisher} sets {ctx.co} against {', '.join(named[:3])}: {rec.title[:120]} [{sid}]"
            )
    return [s.rstrip(".") + "." for s in out]
