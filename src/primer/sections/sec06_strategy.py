"""Section 6, Strategy and Growth (requirements §6 row 6).

plan    -> the newest investor presentation from the IR site map, the 10-K MD&A outlook, the
           latest earnings release Outlook table, the transcript prepared remarks, the
           consensus page; then the template web queries
collect -> scrape the presentation (PDF parse within budget), web research within budget
draft   -> management targets vs consensus table (every target carries the date it was set),
           guidance and consensus claims, narrative (LLM or template)
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from primer.sections import common
from primer.sections import text_tools as tt
from primer.sections.common import ToolContext
from primer.sections.exhibits_sec02 import dropped_note
from primer.sections.exhibits_sec06 import mdna_targets, parse_release_outlook, targets_table
from primer.state import Brief, SectionDraft

SECTION = 6
QA_MARK = re.compile(r"operator:?\**[^\n]{0,200}?question", re.I)


def prepared_remarks(transcript: str) -> str:
    """Text before the first 'Operator: ... question' marker, speaker markers removed."""
    m = QA_MARK.search(transcript)
    text = transcript[: m.start()] if m else transcript
    return re.sub(r"\*\*[^*\n]+\*\*:?\s*", "", text)


def pick_presentation(links: list[str], as_of: date, fy: int) -> str | None:
    """Newest-looking investor-day/presentation URL: prefer PDFs that mention 'investor' or
    the current/fiscal year, then any PDF, then any presentation link."""
    if not links:
        return None

    def score(u: str) -> tuple[int, int, int, int]:
        low = urlparse(u).path.lower()  # the host (investors.example.com) must not count
        years = [int(y) for y in re.findall(r"(20\d{2})", low)]
        newest = max(years) if years else 0
        return (
            int(low.endswith(".pdf") or ".pdf?" in low),
            int("investor" in low),
            int(str(as_of.year) in low or str(fy) in low),
            newest,
        )

    return sorted(links, key=score, reverse=True)[0]


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    links = ((ctx.foundation.get("ir") or {}).get("links") or {}).get("presentations") or []
    deck = pick_presentation(links, ctx.as_of, ctx.fy)
    items: list[dict[str, Any]] = []
    if deck:
        items.append(
            {
                "tool": "firecrawl.parse",
                "args": {"url": deck},
                "why": "latest investor presentation",
            }
        )
    items += [
        {
            "tool": "foundation.item_text",
            "args": {"item": "7", "paragraphs": ["outlook"]},
            "why": "MD&A outlook",
        },
        {
            "tool": "foundation.earnings_release",
            "args": {"table": "Outlook"},
            "why": "guidance ranges with date",
        },
        {
            "tool": "foundation.transcript",
            "args": {"part": "prepared remarks"},
            "why": "stated targets",
        },
        {
            "tool": "market_data.consensus",
            "args": {"ticker": ctx.entity.ticker},
            "why": "consensus for the gap column",
        },
    ]
    for q in common.expand_queries(brief, ctx):
        items.append(
            {"tool": "firecrawl.search", "query": q, "why": "investor day, targets, capacity"}
        )
    return items


def _queries(plan_items: list[dict[str, Any]]) -> list[str]:
    qs = [i.get("query", "") for i in plan_items if i.get("tool") == "firecrawl.search"]
    return [q for q in dict.fromkeys(q.strip() for q in qs) if q]


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    deck_url = next(
        (i["args"]["url"] for i in plan_items if i.get("tool") == "firecrawl.parse"), None
    )
    deck_sid = None
    notes: list[str] = []
    if deck_url:
        deck_sid = common.scrape_url(ctx, deck_url, type="presentation")
        if not deck_sid:
            notes.append("investor presentation could not be fetched")
    else:
        notes.append("no investor presentation link on the IR site map")
    queries = _queries(plan_items)
    remaining = max(0, ctx.budget.searches - ctx.budget.used_searches)
    run = queries[:remaining]
    dropped = len(queries) - len(run)
    prefer = ctx.brief.trade_press if ctx.brief else []
    hits = ctx.web_research(run, type="news", prefer_domains=prefer) if run else []
    notes.extend(ctx.budget.notes)
    if dropped_note(dropped):
        notes.append(dropped_note(dropped) or "")
    if not run and ctx.firecrawl is None:
        notes.append("Firecrawl not configured; web research skipped")
    return {
        "deck_url": deck_url,
        "deck_sid": deck_sid,
        "web": [
            {k: h.get(k) for k in ("url", "title", "domain", "query", "source_id")} for h in hits
        ],
        "notes": list(dict.fromkeys(notes)),
        "searched": len(run),
    }


def _consensus(ctx: ToolContext) -> Any | None:
    if ctx.market is None or not ctx.entity.ticker:
        return None
    try:
        return ctx.market.consensus(ctx.entity.ticker)
    except Exception as e:  # the page is optional; the column reads n/a
        ctx.log(f"consensus unavailable: {e!r}")
        return None


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    tenk = ctx.foundation_sources("10-K")
    k_sid = tenk[0] if tenk else None
    item7, _ = ctx.item_text("7")
    outlook = tt.paragraphs_with(
        item7, ["outlook"], window=0, max_paragraphs=1
    ) or tt.paragraphs_with(item7, ["expect"], window=0, max_paragraphs=1)
    quarters = ctx.quarters()
    q_rel = next(
        (q for q in quarters if q.get("release_sid") and ctx.store.has(q["release_sid"])), None
    )
    q_tr = next(
        (q for q in quarters if q.get("transcript_sid") and ctx.store.has(q["transcript_sid"])),
        None,
    )
    rel_sid = q_rel["release_sid"] if q_rel else None
    tr_sid = q_tr["transcript_sid"] if q_tr else None
    rel_text = ctx.store.text(rel_sid) if rel_sid else ""
    release = parse_release_outlook(rel_text, default_fy=None) if rel_text else None
    if release and release.fy is None and q_rel:
        release.fy = int(q_rel["label"].split("-")[1])
    remarks_text = prepared_remarks(ctx.store.text(tr_sid)) if tr_sid else ""
    remark_sentences = []
    for kws in (["expect"], ["target"], ["capacity"]):
        for s in tt.sentences_with(remarks_text, kws, limit=6):
            if re.search(r"\d", s) and s not in remark_sentences:
                remark_sentences.append(s)
    mdna = mdna_targets(outlook[0]) if outlook else []
    consensus = _consensus(ctx)
    if consensus is not None and not getattr(consensus, "source_id", None):
        consensus = None

    def pub_date(sid: str | None, fallback: str | None = None):
        if sid and ctx.store.has(sid):
            d = ctx.store.get(sid).published_date
            if d:
                return d
        return fallback

    ex = targets_table(
        release=release,
        rel_sid=rel_sid,
        rel_date=pub_date(rel_sid, q_rel["report_date"] if q_rel else None),
        mdna=mdna,
        k_sid=k_sid,
        k_date=pub_date(k_sid),
        remarks=remark_sentences,
        tr_sid=tr_sid,
        tr_date=pub_date(tr_sid, q_tr["report_date"] if q_tr else None),
        consensus=consensus,
        section=SECTION,
    )
    notes = list(tool_results.get("notes") or []) + list(ex.notes)
    deck_sid = (
        tool_results.get("deck_sid") if ctx.store.has(tool_results.get("deck_sid") or "") else None
    )
    web_sids = [w["source_id"] for w in tool_results.get("web") or [] if w.get("source_id")]
    evidence = [s for s in (deck_sid, k_sid, rel_sid, tr_sid, *web_sids) if s]
    if not evidence and not ex.present:
        return common.unavailable_draft(brief, "no strategy sources could be fetched", ctx)

    lead = _lead_sentences(
        ctx,
        ex,
        release,
        rel_sid,
        pub_date(rel_sid),
        outlook,
        k_sid,
        remark_sentences,
        tr_sid,
        deck_sid,
        consensus,
    )
    facts = [c.model_dump(mode="json") for c in ex.claims]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts,
        exhibit_md=ex.markdown,
        extra_rules=(
            "Every management target you quote must carry the date it was set (the release, deck or "
            "call date of its source). Distinguish stated strategy from the company and analyst views."
        ),
        critic_notes=critic_notes,
        keywords=["target", "outlook", "expect", "strategy", "capacity"],
    ) or common.template_digest(ctx, evidence, lead)

    web_ok = tool_results.get("searched", 0) > 0 and not any(
        "Firecrawl not configured" in n for n in notes
    )
    coverage = (
        "full"
        if (ex.present and ex.dated_rows > 0 and deck_sid and web_ok and not ctx.budget.exhausted)
        else "partial"
    )
    if ex.present and not web_ok:
        notes.append("web buckets (investor day coverage, trade press) not read")
    if ex.present and not deck_sid:
        notes.append("investor-day deck bucket not read")
    if ex.present:
        caveat = common.caveat_line(
            "targets are management's own statements as of the date shown for each row; consensus is a "
            "single aggregator page (not a licensed feed) cited with its date, and the gap is derived "
            "from the range midpoint"
        )
    else:
        caveat = common.caveat_line(
            "targets are management's own statements as dated in the text; no dated numeric target "
            "could be tied to a source, so there is no targets-versus-consensus table"
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
        exhibit_title="Management targets vs consensus (dated)",
    )


def _lead_sentences(
    ctx: ToolContext,
    ex: Any,
    release: Any,
    rel_sid: str | None,
    rel_date: Any,
    outlook: list[str],
    k_sid: str | None,
    remarks: list[str],
    tr_sid: str | None,
    deck_sid: str | None,
    consensus: Any | None,
) -> list[str]:
    out: list[str] = []
    if deck_sid:
        rec = ctx.store.get(deck_sid)
        out.append(
            f"The latest investor presentation on the IR site is {rec.title[:120]} ({rec.publisher}, {common.fmt_date(rec.published_date)}) [{deck_sid}]"
        )
        for s in tt.sentences_with(ctx.store.text(deck_sid), ["target"], limit=3):
            out.append(
                f"Presentation statement (page dated {common.fmt_date(rec.published_date)}): {s[:220]} [{deck_sid}]"
            )
    if release and release.rows and rel_sid:
        r0 = release.rows[0]
        out.append(
            f"In the earnings release dated {common.fmt_date(rel_date)}, management set FY{release.fy} {r0.label.lower()} guidance of "
            f"{r0.low / 1e6:,.0f} to {r0.high / 1e6:,.0f} million dollars [{rel_sid}]"
        )
        eps = next((r for r in release.rows if r.metric == "guidance_eps"), None)
        if eps:
            out.append(
                f"The same release guides FY{release.fy} diluted earnings per share to ${eps.low:,.2f} to ${eps.high:,.2f} [{rel_sid}]"
            )
    if outlook and k_sid:
        first = tt.sentences(outlook[0])
        sent = next((s for s in first if "expect" in s.lower()), first[0] if first else "")
        if sent:
            out.append(
                f"The 10-K MD&A outlook (filed {common.fmt_date(ctx.store.get(k_sid).published_date)}) states: {sent[:300]} [{k_sid}]"
            )
    for s in remarks[:3]:
        if tr_sid:
            out.append(f"Prepared remarks on the latest earnings call: {s[:240]} [{tr_sid}]")
    if consensus is not None and getattr(consensus, "revenue_current", None) is not None:
        rc = consensus.revenue_current
        if rc.value is not None and rc.source_id:
            out.append(
                f"Consensus revenue for {consensus.fy_current} on the Yahoo Finance analysis page was ${rc.value / 1e6:,.0f} million as of {common.fmt_date(rc.as_of)} [{rc.source_id}]"
            )
    return [s.rstrip(".") + "." for s in out]
