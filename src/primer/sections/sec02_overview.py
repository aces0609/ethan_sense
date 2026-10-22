"""Section 2, Company Overview (requirements §6 row 2).

plan    -> the foundation 10-K Item 1, the segment table, the entity record and the DEF 14A
           ownership table, then the three template web queries
collect -> web research within the section budget (the structured sources are already in
           the store from the foundation pack)
draft   -> segment revenue exhibit (3 FY, every cell cited), employee-count claim from the
           Item 1 Human Capital passage, the customer split (US-government and international
           shares) from the customer / concentration passage, the listing/ownership caveat
           the examples open with, narrative (LLM or template)
"""

from __future__ import annotations

import re
from typing import Any

from primer.sections import common, private
from primer.sections import text_tools as tt
from primer.sections.common import ToolContext
from primer.sections.exhibits_sec02 import dropped_note, segment_revenue_exhibit, strip_item_heading
from primer.state import Brief, SectionDraft

SECTION = 2

HQ_RES = [
    re.compile(
        r"(?:corporate\s+)?headquarter(?:s|ed)\s+(?:is|are)?\s*(?:located\s+)?(?:in|at)\s+"
        r"([A-Z][A-Za-z.\- ]+?,\s*[A-Z][A-Za-z.\- ]+?)(?=[.,;)]|\s+\d|\s+and\b|\n)"
    ),
    re.compile(
        r"([A-Z][A-Za-z.\- ]+?,\s*[A-Z][A-Za-z.\- ]+?)\s+\d{5}(?:-\d{4})?[^\n]{0,80}?"
        r"principal executive offices",
        re.I,
    ),
    re.compile(
        r"principal executive offices\s+(?:are\s+)?(?:located\s+)?(?:in|at)\s+"
        r"(?:[\d\w.,\- ]+?,\s*)?([A-Z][A-Za-z.\- ]+?,\s*[A-Z][A-Za-z.\- ]+?)(?=[.,;)]|\s+\d|\n)"
    ),
]
PCT_CELL = re.compile(r"^\d{1,2}(?:\.\d+)?\s?%$")


# ---- structured readers ------------------------------------------------------------


def employee_count(text: str) -> int | None:
    """Company-wide employee count from the Human Capital passage of Item 1 (the sentence
    naming employees with the largest number, 'approximately N employees' preferred, union
    sentences excluded); see `text_tools.employee_count`."""
    found = tt.employee_count(text)
    return found[0] if found else None


def headquarters(text: str) -> str | None:
    """City, State from the 10-K: the properties/HQ sentence or the cover-page address."""
    for rx in HQ_RES:
        for window in (text[:40000], text):
            m = rx.search(window)
            if m:
                hq = " ".join(m.group(1).split()).strip(" ,.")
                if 5 <= len(hq) <= 60:
                    return hq
    return None


def ownership_rows(def14a_text: str, limit: int = 3) -> list[dict[str, Any]]:
    """Largest holders from the proxy's beneficial-ownership table: rows with a percent cell,
    excluding the directors-and-officers group line."""
    lines = def14a_text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if re.search(r"security ownership|beneficial own", line, re.I):
            start = i
            break
    out: list[dict[str, Any]] = []
    for line in lines[start : start + 120]:
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 2:
            continue
        pct = next((c for c in cells if PCT_CELL.match(c)), None)
        name = cells[0]
        if not pct or not re.match(r"[A-Z]", name):
            continue
        low = name.lower()
        if low.startswith(("all directors", "all executive", "name")) or "as a group" in low:
            continue
        out.append({"holder": name[:80], "pct": float(pct.rstrip("% "))})
    out.sort(key=lambda r: -r["pct"])
    return out[:limit]


# ---- handler -----------------------------------------------------------------------


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {"tool": "foundation.item_text", "args": {"item": "1"}, "why": "10-K Item 1 business"},
        {
            "tool": "foundation.segment_table",
            "args": {"items": ["7", "8", "10-K"]},
            "why": "segment revenue table (3 FY)",
        },
        {
            "tool": "entity",
            "args": {"fields": ["exchange", "state_of_incorporation", "fiscal_year_end"]},
            "why": "listing and incorporation",
        },
        {"tool": "foundation.def14a", "args": {"table": "ownership"}, "why": "largest holders"},
    ]
    for q in common.expand_queries(brief, ctx):
        items.append({"tool": "firecrawl.search", "query": q, "why": "overview context (gap fill)"})
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
    tenk = ctx.foundation_sources("10-K")
    if not tenk:
        return common.unavailable_draft(brief, "no 10-K in the foundation pack", ctx)
    k_sid = tenk[0]
    k_text = ctx.store.text(k_sid)
    item1, _ = ctx.item_text("1")
    ex = segment_revenue_exhibit(ctx, SECTION)
    notes = list(tool_results.get("notes") or []) + list(ex.notes)

    emp = employee_count(item1) if item1 else None
    if emp is None:
        emp = employee_count(k_text)
    hq = headquarters(k_text)
    def14a = ctx.foundation_sources("DEF 14A")
    holders = ownership_rows(ctx.store.text(def14a[0])) if def14a else []
    web_sids = [w["source_id"] for w in tool_results.get("web") or [] if w.get("source_id")]

    claims = list(ex.claims)
    emp_claim = common.claim(
        SECTION,
        "employee_count",
        emp,
        f"FY{ctx.fy}",
        [k_sid],
        unit="count",
        concept="10-K Item 1 (Human Capital)",
    )
    if emp_claim:
        claims.append(emp_claim)
    split = tt.customer_split(k_text)
    for key, metric in (
        ("us_gov_pct", "us_government_revenue_pct"),
        ("international_pct", "international_revenue_pct"),
    ):
        c = common.claim(
            SECTION,
            metric,
            split.get(key),
            f"FY{ctx.fy}",
            [k_sid],
            unit="pct",
            concept=f"10-K {split.get('passage') or 'text'} (customer split)",
            note=(split.get(key.replace("_pct", "_sentence")) or "")[:200] or None,
        )
        if c:
            claims.append(c)

    caveat = _caveat(ctx, holders, def14a[0] if def14a else None, ex.present)
    lead = _lead_sentences(ctx, item1, k_sid, k_text, ex, emp, hq, holders, def14a)
    facts = [c.model_dump(mode="json") for c in claims] + [
        {"fact": "headquarters", "value": hq, "source_ids": [k_sid]},
        {
            "fact": "customer split (10-K customer / concentration passage)",
            "value": {k: v for k, v in split.items() if v is not None},
            "source_ids": [k_sid],
        },
        {
            "fact": "largest holders (DEF 14A)",
            "value": holders,
            "source_ids": def14a[:1],
        },
        {
            "fact": "listing",
            "value": {
                "exchange": ctx.entity.exchange,
                "ticker": ctx.entity.ticker,
                "state_of_incorporation": ctx.entity.state_of_incorporation,
                "fiscal_year_end": ctx.foundation.get("fiscal_year_end")
                or ctx.entity.fiscal_year_end,
            },
            "source_ids": [],
        },
    ]
    evidence = [k_sid, *def14a[:1], *web_sids]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts,
        exhibit_md=ex.markdown,
        extra_rules=(
            "Open the caveat with the listing venue, ticker and the largest holders named in the "
            "proxy. Describe the segments from the 10-K, not from memory; web pages are context only."
        ),
        critic_notes=critic_notes,
        keywords=["segment", "headquarters", "employees", "business"],
    ) or common.template_digest(ctx, evidence, lead)

    web_ok = tool_results.get("searched", 0) > 0 and not any(
        "Firecrawl not configured" in n for n in notes
    )
    coverage = "full" if (ex.present and web_ok and not ctx.budget.exhausted) else "partial"
    if ex.present and not web_ok:
        notes.append("web buckets (IR site, trade press) not read")
    return common.finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=ex.markdown,
        exhibit_present=ex.present,
        claims=claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Segment revenue (3 fiscal years, from the 10-K segment table)",
    )


def _caveat(
    ctx: ToolContext, holders: list[dict[str, Any]], def_sid: str | None, exhibit: bool
) -> str:
    e = ctx.entity
    bits = []
    listing = f"{ctx.co} is listed on {e.exchange or 'an exchange not resolved'}"
    if e.ticker:
        listing += f" as {e.ticker}"
    if e.state_of_incorporation:
        listing += f" and incorporated in {e.state_of_incorporation}"
    bits.append(listing)
    if holders and def_sid:
        bits.append(
            "the largest holders reported in the proxy are "
            + ", ".join(f"{h['holder']} ({h['pct']:.1f}%)" for h in holders)
            + f" [{def_sid}]"
        )
    else:
        bits.append("no beneficial-ownership table could be read from a proxy statement")
    if exhibit:
        bits.append(
            "segment revenue is as reported in the 10-K segment table (USD millions) and the "
            "share-of-total rows are derived"
        )
    else:
        bits.append("no segment revenue table could be read from the 10-K")
    return common.caveat_line("; ".join(bits))


def _lead_sentences(
    ctx: ToolContext,
    item1: str,
    k_sid: str,
    k_text: str,
    ex: Any,
    emp: int | None,
    hq: str | None,
    holders: list[dict[str, Any]],
    def14a: list[str],
) -> list[str]:
    cite = f" [{k_sid}]"
    out: list[str] = []
    first = next(
        (
            s
            for s in tt.sentences(strip_item_heading(item1 or k_text))
            if len(s) >= 60 and re.search(r"\b(is|are)\b", s) and not s.lower().startswith("item")
        ),
        None,
    )
    if first:
        out.append(f"The 10-K describes the company as follows: {first[:300]}{cite}")
    segs = ex.table.segment_names
    if segs:
        out.append(f"The 10-K reports {len(segs)} business segments: {', '.join(segs)}{cite}")
    if hq:
        out.append(f"Headquarters are in {hq}{cite}")
    if emp:
        out.append(
            f"The company had approximately {emp:,} employees at fiscal year end FY{ctx.fy}{cite}"
        )
    split = tt.customer_split(k_text)
    bits = []
    if split.get("us_gov_pct") is not None:
        bits.append(f"{split['us_gov_pct']:.0f}% of sales from the U.S. Government")
    if split.get("international_pct") is not None:
        bits.append(f"{split['international_pct']:.0f}% from international customers")
    if bits:
        out.append(f"The 10-K's customer disclosure puts {' and '.join(bits)} in FY{ctx.fy}{cite}")
    e = ctx.entity
    if e.exchange and e.ticker and e.exchange.upper() in k_text.upper():
        out.append(f"The common stock is listed on {e.exchange} under the ticker {e.ticker}{cite}")
    if ex.present and ex.table.table is not None:
        t, cols = ex.table.table, ex.table.columns
        tot = t.total.values[0]
        parts = [
            f"{r.label} ${r.values[0]:,.0f} million ({r.values[0] / tot * 100:.1f}% of total, derived)"
            for r in t.rows
            if tot
        ]
        out.append(
            f"Segment revenue in {cols[0]}: "
            + ", ".join(parts)
            + f"; total ${tot:,.0f} million{cite}"
        )
    if holders and def14a:
        out.append(
            "The largest beneficial holders named in the proxy are "
            + ", ".join(f"{h['holder']} ({h['pct']:.1f}%)" for h in holders)
            + f" [{def14a[0]}]"
        )
    return [s.rstrip(".") + "." for s in out]
