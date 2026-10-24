"""Section 12, Investment Thesis and Debates (requirements §6 row 12).

plan   -> earnings-call transcripts from the foundation pack (Q&A), rating changes and
          analyst coverage from the market-data adapter, then the web queries
collect-> parses nothing yet: records the transcript ids, pulls the adapter rows (the
          MarketBeat/TipRanks pages are scraped once per run by the adapter), runs web
          research within budget
draft  -> bull vs bear table and the analyst-questions list, each question paired with
          the management answer that follows it in the same transcript (review 2026-10-21
          §9 fix 7); every firm attribution is backed by a cited source whose text names
          the firm; claims for rating counts and price targets; narrative (model when
          configured, else the template digest)
"""

from __future__ import annotations

from typing import Any

from primer.sections.common import (
    ToolContext,
    caveat_line,
    draft_section,
    expand_queries,
    finalize_draft,
    template_digest,
    unavailable_draft,
)
from primer.sections.exhibits_sec12 import (
    company_points,
    page_points,
    parse_qa,
    rating_points,
    thesis_exhibit,
)
from primer.state import Brief, SectionDraft


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "tool": "foundation.transcripts",
            "args": {"quarters": [q["label"] for q in ctx.quarters() if q.get("transcript_sid")]},
            "why": brief.structured_first[0] if brief.structured_first else "earnings-call Q&A",
        }
    ]
    if ctx.entity.ticker:
        items.append(
            {
                "tool": "market_data.rating_changes",
                "args": {"ticker": ctx.entity.ticker, "months": 12},
                "why": "rating changes with firm and date (MarketBeat page)",
            }
        )
        items.append(
            {
                "tool": "market_data.analyst_coverage",
                "args": {"ticker": ctx.entity.ticker},
                "why": "bound on which firms may be named",
            }
        )
    for q in expand_queries(brief, ctx):
        items.append(
            {
                "tool": "firecrawl.search",
                "query": q,
                "why": "analyst notes, short reports and debates in news",
            }
        )
    return items


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    out: dict[str, Any] = {
        "transcripts": [],
        "rating_changes": [],
        "coverage_firms": [],
        "coverage_sid": None,
        "pages": [],
        "notes": [],
        "market": False,
    }
    seen: set[str] = set()
    for q in ctx.quarters():
        sid = q.get("transcript_sid")
        if sid and ctx.store.has(sid) and sid not in seen:
            seen.add(sid)
            out["transcripts"].append({"sid": sid, "quarter": q["label"]})
    for sid in ctx.foundation_sources("transcript"):
        if sid not in seen:
            seen.add(sid)
            out["transcripts"].append(
                {"sid": sid, "quarter": str(ctx.store.get(sid).meta.get("quarter") or "n.d.")}
            )
    if not out["transcripts"]:
        out["notes"].append("no earnings-call transcript in the foundation pack")

    tk = ctx.entity.ticker
    if ctx.market is None or not tk:
        out["notes"].append(
            "no market-data adapter (Firecrawl not configured); rating changes unavailable"
        )
    else:
        out["market"] = True
        try:
            out["rating_changes"] = list(ctx.market.rating_changes(tk, months=12))
        except Exception as e:
            ctx.log(f"rating_changes: {e!r}")
            out["notes"].append("rating-change page could not be read")
        try:
            cov = ctx.market.analyst_coverage(tk)
            out["coverage_firms"] = list(cov.firms)
            out["coverage_sid"] = cov.source_id
        except Exception as e:
            ctx.log(f"analyst_coverage: {e!r}")

    queries = [i["query"] for i in plan_items if i.get("tool") == "firecrawl.search"]
    queries = queries[: max(0, ctx.budget.searches - ctx.budget.used_searches)]
    rows = ctx.web_research(queries, type="news") if queries else []
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
    debates = []
    for t in res.get("transcripts", []):
        if ctx.store.has(t["sid"]):
            debates.extend(parse_qa(ctx.store.text(t["sid"]), t["quarter"], t["sid"]))
    changes = res.get("rating_changes") or []
    texts = {
        rc.source_id: ctx.store.text(rc.source_id)
        for rc in changes
        if getattr(rc, "source_id", None) and ctx.store.has(rc.source_id)
    }
    rating, rating_claims, skipped = rating_points(changes, texts)
    rating_sid = next(
        (rc.source_id for rc in changes if getattr(rc, "source_id", None) in texts), None
    )
    news = []
    for p in res.get("pages", []):
        if not ctx.store.has(p["sid"]):
            continue
        rec = ctx.store.get(p["sid"])
        if rec.access != "full":
            continue
        news.extend(
            page_points(
                ctx.store.text(p["sid"]),
                p["sid"],
                rec.published_date,
                [ctx.entity.name, *ctx.entity.aliases],
                ticker=ctx.entity.ticker,
            )
        )
    item7, sid7 = ctx.item_text("7")
    item1a, sid1a = ctx.item_text("1A")
    company = company_points(item7, sid7, item1a, sid1a)

    ex = thesis_exhibit(
        debates=debates,
        rating=rating,
        rating_claims=rating_claims,
        news=news,
        company=company,
        rating_count=len(changes) if res.get("market") and rating_sid else None,
        rating_sid=rating_sid,
    )
    evidence = [t["sid"] for t in res.get("transcripts", [])]
    evidence += [rating_sid] if rating_sid else []
    evidence += [p["sid"] for p in res.get("pages", [])]
    evidence += [s for s in (sid7, sid1a) if s]
    evidence = [s for s in dict.fromkeys(evidence) if s and ctx.store.has(s)]
    if not evidence:
        return unavailable_draft(
            brief,
            res.get("error") or "no transcript, rating page, news page or 10-K text available",
            ctx,
        )

    notes = list(res.get("notes", []))
    if skipped:
        notes.append(
            f"{len(skipped)} rating rows dropped because the page text does not name the firm"
        )
    if res.get("transcripts") and not debates:
        notes.append("transcript found but no analyst question with a named firm could be parsed")
    answered = [d for d in debates if d.answer]
    if debates and not answered:
        notes.append(
            "analyst questions parsed but no management answer followed them in the transcript"
        )
    coverage = "full" if (debates and ex.present) else "partial"
    firms_bound = sorted({d.firm for d in debates} | set(res.get("coverage_firms") or []))

    caveat = caveat_line(
        "No sell-side research or expert-call transcripts were available; firm names appear only where a "
        "cited transcript or page names the firm, rating actions are as listed on the aggregator page on "
        "the date shown, analyst questions are quoted as asked, not as views, and management answers are "
        "the company's statements on the same call, quoted from the same transcript"
    )
    leads: list[str] = []
    for d in debates[:4]:
        leads.append(f'On the {d.quarter} call, {d.firm} asked: "{d.question}" [{d.source_id}].')
        if d.answer:
            leads.append(f'{d.answered_by or "Management"} replied: "{d.answer}" [{d.source_id}].')
    if rating_sid and res.get("market"):
        named = [p for p in rating if p.kind == "rating"]
        s = f"The MarketBeat ratings page lists {len(changes)} rating actions in the trailing 12 months"
        if named:
            s += f", including {named[0].text}"
        leads.append(s + f" [{rating_sid}].")
    for p in news[:2]:
        leads.append(f"{p.text} [{p.source_id}].")
    if not leads and company:
        leads.append(f"{company[0].text} [{company[0].source_id}].")

    narrative = draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=[c.model_dump() for c in ex.claims]
        + [{"firms_named_in_sources": firms_bound}]
        + [
            {
                "management_answers": [
                    {
                        "firm": d.firm,
                        "call": d.quarter,
                        "question": d.question,
                        "answered_by": d.answered_by,
                        "answer": d.answer,
                        "source_id": d.source_id,
                    }
                    for d in answered
                ]
            }
        ],
        exhibit_md=ex.markdown + "\n\n" + ex.questions_md,
        extra_rules=(
            "Attribute a view to a firm only when the cited source names that firm (the list "
            "firms_named_in_sources is the bound). Analyst questions are debates, not views; say who asked. "
            "management_answers are the company's own statements from the same transcript: use them to "
            "weigh each bull and bear point, name the officer as the transcript names them, and never "
            "present an answer as a broker view. Never invent a broker rating or target."
        ),
        critic_notes=critic_notes,
        keywords=["question", "answer", "upgrade", "downgrade", "target", "bull", "bear"],
    )
    if narrative is None:
        narrative = template_digest(ctx, evidence, leads)
    exhibit_md = (
        ex.markdown
        + "\n\n**Analyst questions and what management said (as asked and answered on the calls)**\n\n"
        + ex.questions_md
    )
    if not ex.present:
        # no bull or bear point, no question: a table of "no sourced item" says nothing
        exhibit_md = ""
        notes.append("no sourced bull or bear point and no analyst question for the table")
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
        exhibit_title="Bull and bear points, with the open questions" if ex.present else None,
    )
