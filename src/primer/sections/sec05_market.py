"""Section 5, Market and Competitive Position (requirements §6 row 5).

plan    -> 10-K Item 1A and the competition paragraphs (foundation), the peers the foundation
           resolved from the 10-K, each peer's latest 10-K Item 1 (EDGAR, capped), then the
           template queries plus "{co} vs {peer}" for the first two peers
collect -> peer 10-K fetches (skip on any exception, logged) and web research within budget
draft   -> SWOT table where every cell is a cited sentence or "no sourced item", a peer list
           with one-line positioning, claims for the US-government revenue share and the
           peer count, narrative (LLM or template)
"""

from __future__ import annotations

import time
from typing import Any

from primer.evidence.chunking import chunk_by_item
from primer.evidence.html import html_to_text
from primer.sections import common, private
from primer.sections import text_tools as tt
from primer.sections.common import ToolContext
from primer.sections.exhibits_sec02 import dropped_note, strip_item_heading
from primer.sections.exhibits_sec05 import build_swot, peer_markdown, swot_markdown
from primer.state import Brief, SectionDraft

SECTION = 5
PEER_CAP = 3
PEER_TIME_BUDGET_S = 90.0


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    peers = list(ctx.foundation.get("peers") or [])
    items: list[dict[str, Any]] = [
        {"tool": "foundation.item_text", "args": {"item": "1A"}, "why": "risk factors (W, T)"},
        {
            "tool": "foundation.item_text",
            "args": {"item": "1", "paragraphs": ["compet"]},
            "why": "competition paragraphs (S, peers)",
        },
        {
            "tool": "foundation.peers",
            "args": {"peers": [p.get("ticker") for p in peers]},
            "why": "peer set from the filing, not the model",
        },
    ]
    for p in peers[:PEER_CAP]:
        if p.get("cik"):
            items.append(
                {
                    "tool": "edgar.filings",
                    "args": {"cik": p["cik"], "forms": ["10-K"], "limit": 1},
                    "why": f"{p.get('name')} 10-K Item 1 positioning",
                }
            )
    queries: list[str] = []
    for peer in [p.get("name") or p.get("ticker") for p in peers[:2]] or [None]:
        if peer:
            queries.extend(common.expand_queries(brief, ctx, peer=peer))
        else:
            queries.extend(common.expand_queries(brief, ctx))
    for q in dict.fromkeys(queries):
        items.append({"tool": "firecrawl.search", "query": q, "why": "market share, peers, moat"})
    return items


def _queries(plan_items: list[dict[str, Any]]) -> list[str]:
    qs = [i.get("query", "") for i in plan_items if i.get("tool") == "firecrawl.search"]
    return [q for q in dict.fromkeys(q.strip() for q in qs) if q]


def fetch_peer_item1(ctx: ToolContext, peer: dict[str, Any]) -> dict[str, Any]:
    """Register the peer's latest 10-K (tier 1, gov_id accession) and return its Item 1
    first sentence; raises on any failure (the caller logs and skips)."""
    refs = ctx.edgar.filings(peer["cik"], forms=["10-K"], limit=1)
    if not refs or not refs[0].primary_document:
        raise RuntimeError("no 10-K with a primary document")
    ref = refs[0]
    text = html_to_text(ctx.edgar.get_text(ref.url))
    rec = ctx.store.register(
        text,
        type="10-K",
        publisher=peer.get("name") or peer.get("ticker") or "peer",
        title=f"{peer.get('name')} Annual Report (10-K) for fiscal year ended {ref.report_date}",
        url=ref.url,
        published_date=ref.filing_date,
        tier=1,
        gov_id=ref.accession,
        meta={"form": ref.form, "cik": peer["cik"], "peer": True, "ticker": peer.get("ticker")},
        dedupe_key=f"filing|{ref.accession}",
    )
    item1 = next((c.text for c in chunk_by_item(text) if c.item == "1"), text[:20000])
    first = next(
        (
            s
            for s in tt.sentences(strip_item_heading(item1))
            if len(s) >= 40 and not s.lower().startswith("item") and "|" not in s
        ),
        "",
    )
    return {"item1_sid": rec.source_id, "positioning": first[:220].rstrip(".")}


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    peers = [dict(p) for p in ctx.foundation.get("peers") or []]
    t0 = time.monotonic()
    fetched = 0
    for p in peers:
        if fetched >= PEER_CAP or not p.get("cik"):
            continue
        if time.monotonic() - t0 > PEER_TIME_BUDGET_S:
            ctx.log("peer 10-K fetches stopped at the time budget")
            break
        try:
            p.update(fetch_peer_item1(ctx, p))
            fetched += 1
        except Exception as e:  # a peer filing that cannot be read is skipped, never fatal
            ctx.log(f"peer 10-K for {p.get('ticker')} skipped: {e!r}")
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
        "peers": peers,
        "peers_fetched": fetched,
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
    tenk = ctx.foundation_sources("10-K")
    if not tenk:
        return common.unavailable_draft(brief, "no 10-K in the foundation pack", ctx)
    k_sid = tenk[0]
    item1, _ = ctx.item_text("1")
    item1a, _ = ctx.item_text("1A")
    item7, _ = ctx.item_text("7")
    gov = ctx.foundation.get("gov_revenue") or {}
    quarters = ctx.quarters()
    rel_sid = next((q["release_sid"] for q in quarters if q.get("release_sid")), None)
    rel_text = ctx.store.text(rel_sid) if rel_sid and ctx.store.has(rel_sid) else ""
    outlook = tt.paragraphs_with(
        item7, ["outlook"], window=0, max_paragraphs=1
    ) or tt.paragraphs_with(item7, ["expect"], window=0, max_paragraphs=1)
    peers = [p for p in (tool_results.get("peers") or ctx.foundation.get("peers") or [])]
    peers = [p for p in peers if not p.get("item1_sid") or ctx.store.has(p["item1_sid"])]

    sw = build_swot(
        strip_item_heading(item1),
        item1a,
        k_sid,
        gov.get("sentence") if gov.get("source_id") == k_sid else None,
        outlook,
        rel_text,
        rel_sid,
    )
    swot_md = swot_markdown(sw)
    peer_md = peer_markdown(peers, k_sid)
    exhibit_md = swot_md + "\n\n### Peer list (from the 10-K competition paragraph)\n\n" + peer_md
    present = bool(sw.cells())
    notes = list(tool_results.get("notes") or [])
    if not item1a:
        notes.append("no Item 1A found in the 10-K; weaknesses and threats unsourced")
    if not present:
        notes.append("no sourced SWOT items could be lifted from the filings")
    if not peers:
        notes.append("no peers resolved from the 10-K competition paragraph")
    fetched = int(tool_results.get("peers_fetched") or 0)
    if peers and fetched < min(PEER_CAP, len(peers)):
        notes.append(f"peer 10-K Item 1 read for {fetched} of {len(peers)} peers")

    claims = []
    if gov.get("pct") is not None and gov.get("source_id"):
        c = common.claim(
            SECTION,
            "us_government_revenue_pct",
            float(gov["pct"]),
            f"FY{ctx.fy}",
            [gov["source_id"]],
            unit="pct",
            concept="10-K Item 1 customer concentration",
            note="share of consolidated sales from the U.S. Government as stated in the 10-K",
        )
        if c:
            claims.append(c)
    c = common.claim(
        SECTION,
        "peer_count",
        len(peers),
        f"FY{ctx.fy}",
        [k_sid],
        unit="count",
        concept="derived",
        note="competitors named in the 10-K competition paragraph and resolved to SEC filers",
    )
    if c and peers:
        claims.append(c)

    web_sids = [w["source_id"] for w in tool_results.get("web") or [] if w.get("source_id")]
    peer_sids = [p["item1_sid"] for p in peers if p.get("item1_sid")]
    evidence = [k_sid, *([rel_sid] if rel_sid else []), *peer_sids, *web_sids]
    lead = _lead_sentences(ctx, sw, peers, k_sid, gov)
    facts = [c.model_dump(mode="json") for c in claims] + [
        {"fact": "SWOT (sourced)", "value": sw.__dict__, "source_ids": [k_sid]},
        {
            "fact": "peers",
            "value": [[p.get("name"), p.get("ticker")] for p in peers],
            "source_ids": [k_sid],
        },
    ]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts,
        exhibit_md=exhibit_md,
        extra_rules=(
            "The peer set is the filing's own competitor list; do not add peers. Every SWOT point "
            "in the prose must be traceable to a cited source; say when market share is not disclosed."
        ),
        critic_notes=critic_notes,
        keywords=["compet", "market", "risk"],
    ) or common.template_digest(ctx, evidence, lead)

    web_ok = tool_results.get("searched", 0) > 0 and not any(
        "Firecrawl not configured" in n for n in notes
    )
    coverage = (
        "full" if (present and bool(item1a) and web_ok and not ctx.budget.exhausted) else "partial"
    )
    if present and not web_ok:
        notes.append("web buckets (trade press, analyst commentary) not read")
    caveat = common.caveat_line(
        "SWOT cells are sentences lifted from the company's 10-K, its latest earnings release and "
        "fetched pages, each with its citation; the peer set is the one the 10-K names, and market "
        "share figures appear only where a cited source states them"
    )
    return common.finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=present,
        claims=claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="SWOT (every cell sourced)",
    )


def _lead_sentences(
    ctx: ToolContext, sw: Any, peers: list[dict[str, Any]], k_sid: str, gov: dict[str, Any]
) -> list[str]:
    out: list[str] = []
    if gov.get("pct") is not None and gov.get("source_id"):
        out.append(
            f"The 10-K states that {gov['pct']:.0f}% of consolidated sales came from the U.S. Government in FY{ctx.fy} [{gov['source_id']}]"
        )
    if peers:
        named = ", ".join(f"{p.get('name')} ({p.get('ticker')})" for p in peers)
        out.append(
            f"The 10-K competition paragraph names {len(peers)} SEC-filing peers: {named} [{k_sid}]"
        )
    for p in peers:
        if p.get("item1_sid") and p.get("positioning"):
            out.append(
                f"{p.get('name')} describes itself as: {p['positioning'][:200]} [{p['item1_sid']}]"
            )
    for label, items in (
        ("Strength", sw.strengths),
        ("Weakness", sw.weaknesses),
        ("Opportunity", sw.opportunities),
        ("Threat", sw.threats),
    ):
        if items:
            out.append(f"{label} (sourced): {items[0]}")
    return [s.rstrip(".") + "." for s in out]
