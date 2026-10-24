"""Shared machinery for every section handler (requirements §5 researcher subgraph).

A handler module exposes three functions with these signatures:

    plan(brief: Brief, ctx: ToolContext) -> list[dict]        # structured calls first, then web queries
    collect(plan_items: list[dict], ctx: ToolContext) -> dict  # runs them; everything fetched is registered
    draft(brief: Brief, tool_results: dict, ctx: ToolContext, critic_notes: list[str] | None) -> SectionDraft

The ToolContext carries the clients, the evidence store, the foundation pack handles and a
SectionBudget. `web_research` is the one path from a query to a registered page: it runs
Firecrawl searches within the budget, post-filters by domain policy, ranks hits by the
section's preferred domains and the tier-1 host list, and scrapes the top hits. `draft_section`
is the one path from evidence to narrative: the strong model sees only evidence excerpts
labelled with their source ids and cannot cite anything else; `template_digest` is the
keyless fallback.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from primer.clients.edgar import EdgarClient
from primer.clients.firecrawl import BudgetExhausted, FirecrawlClient, FirecrawlError
from primer.clients.samgov import SamGovClient
from primer.clients.usaspending import USAspendingClient
from primer.config import domain_policy, section_budget, tier_for_url
from primer.evidence.chunking import Chunk, chunk_by_item
from primer.evidence.store import EvidenceStore
from primer.llm import invoke_text
from primer.runlog import RunLog
from primer.sections.style import (
    CITATION_TOKEN,
    HOUSE_STYLE,
    PIPELINE_MOVE,
    PIPELINE_REWRITES,
)
from primer.state import Brief, Claim, Entity, SectionDraft, SourceRecord

PDF_RE = re.compile(r"\.pdf($|\?)", re.I)


# ---- budget --------------------------------------------------------------------------


# A site that blocks scraping (anduril.com answers every request with a 500) returns no page and
# no credit is spent on it, so a failure must not use up the section's scrape cap and starve the
# pages that do load. A section still gives up after this many failures.
MAX_FAILED_SCRAPES = 10
# A site that fails twice is skipped for the rest of the section: anduril.com failed on every
# page, and each further URL on it only spent the failure cap.
MAX_FAILS_PER_SITE = 2


@dataclass
class SectionBudget:
    section: int
    searches: int
    scrapes: int
    credits: int
    hits_per_search: int = 8
    used_searches: int = 0
    used_scrapes: int = 0
    failed_scrapes: int = 0  # scrapes that returned nothing; they do not use the scrape cap
    failed_sites: dict[str, int] = field(default_factory=dict)
    exhausted: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def for_section(cls, section: int, private: bool = False) -> SectionBudget:
        b = section_budget(section, private)
        return cls(
            section=section,
            searches=int(b.get("searches", 4)),
            scrapes=int(b.get("scrapes", 8)),
            credits=int(b.get("credits", 24)),
            hits_per_search=int(b.get("hits_per_search", 8)),
        )

    def can_search(self) -> bool:
        if self.used_searches >= self.searches:
            self.exhausted = self.exhausted or f"search cap {self.searches} reached"
            return False
        return True

    @staticmethod
    def _site(url: str) -> str:
        host = (urlparse(url).hostname or url).lower()
        return host[4:] if host.startswith("www.") else host

    def note_failed_scrape(self, url: str) -> None:
        self.failed_scrapes += 1
        site = self._site(url)
        self.failed_sites[site] = self.failed_sites.get(site, 0) + 1

    def site_blocked(self, url: str) -> bool:
        return self.failed_sites.get(self._site(url), 0) >= MAX_FAILS_PER_SITE

    def can_scrape(self) -> bool:
        if self.failed_scrapes >= MAX_FAILED_SCRAPES:
            self.exhausted = self.exhausted or f"{MAX_FAILED_SCRAPES} scrapes failed"
            return False
        if self.used_scrapes >= self.scrapes:
            self.exhausted = self.exhausted or f"scrape cap {self.scrapes} reached"
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "searches": f"{self.used_searches}/{self.searches}",
            "scrapes": f"{self.used_scrapes}/{self.scrapes}",
            "failed_scrapes": self.failed_scrapes,
            "credits_cap": self.credits,
            "exhausted": self.exhausted,
        }


# ---- context -------------------------------------------------------------------------


@dataclass
class ToolContext:
    section: int
    run_dir: Path
    as_of: date
    entity: Entity
    foundation: dict[str, Any]
    store: EvidenceStore
    edgar: EdgarClient
    runlog: RunLog
    budget: SectionBudget
    offline: bool = False
    firecrawl: FirecrawlClient | None = None
    usaspending: USAspendingClient | None = None
    samgov: SamGovClient | None = None
    market: Any | None = None  # MarketDataAdapter, created lazily by graph.researcher
    brief: Brief | None = None
    # section 14's dated events ({date, event, detail, source_id}) for the sections that
    # run after it (3, 6, 8, 10, 12); empty for the first researcher wave
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    _chunks: dict[str, list[Chunk]] = field(default_factory=dict)

    # -- foundation handles ------------------------------------------------------------

    @property
    def fy(self) -> int:
        return int(self.foundation.get("fy") or (self.as_of.year - 1))

    @property
    def co(self) -> str:
        return self.entity.aliases[0] if self.entity.aliases else self.entity.name

    def foundation_sources(self, kind: str) -> list[str]:
        """Source ids the foundation pack registered for a kind: 10-K, 10-Q, DEF 14A,
        earnings_release, transcript, 8-K, ir_map, presentation."""
        return [
            s for s in (self.foundation.get("sources", {}).get(kind) or []) if self.store.has(s)
        ]

    def foundation_filings(self, form: str) -> list[dict[str, Any]]:
        return list(self.foundation.get("filings", {}).get(form) or [])

    def chunks(self, kind: str = "10-K", index: int = 0) -> list[Chunk]:
        """Item-heading chunks of a foundation filing (re-chunked from the evidence text)."""
        key = f"{kind}:{index}"
        if key not in self._chunks:
            sids = self.foundation_sources(kind)
            self._chunks[key] = (
                chunk_by_item(self.store.text(sids[index])) if len(sids) > index else []
            )
        return self._chunks[key]

    def item_text(
        self, item: str, kind: str = "10-K", index: int = 0, max_chars: int | None = None
    ) -> tuple[str, str | None]:
        """(text, source_id) of one Item of a foundation filing; ('', None) when absent."""
        sids = self.foundation_sources(kind)
        if len(sids) <= index:
            return "", None
        for c in self.chunks(kind, index):
            if c.item == item.upper():
                t = c.text
                return (t[:max_chars] if max_chars else t), sids[index]
        return "", None

    def quarters(self) -> list[dict[str, Any]]:
        """Earnings quarters from the foundation pack, newest first:
        {label, report_date, release_sid, transcript_sid, accession}."""
        return list(self.foundation.get("quarters") or [])

    def log(self, msg: str) -> None:
        self.runlog.error(f"section {self.section}: {msg}")

    # -- convenience ------------------------------------------------------------------

    def web_research(self, queries: list[str], **kw: Any) -> list[dict[str, Any]]:
        return web_research(self, queries, **kw)

    def source_line(self, sid: str) -> str:
        r = self.store.get(sid)
        d = r.published_date.isoformat() if r.published_date else "n.d."
        return f"{r.title} ({r.publisher}, {d})"


# ---- recent events from section 14 (review §9 item 5) ---------------------------------------


def recent_events(ctx: ToolContext, limit: int = 20) -> list[dict[str, Any]]:
    """Section 14's dated events, newest first, whose sources exist in the store. Each is
    {date, event, detail, source_id}; empty when this section ran in the first wave."""
    out = []
    for e in ctx.recent_events:
        sid = e.get("source_id")
        if not sid or not ctx.store.has(sid):
            continue
        out.append(e)
    out.sort(key=lambda e: str(e.get("date", "")), reverse=True)
    return out[:limit]


def recent_events_block(ctx: ToolContext, limit: int = 12) -> str:
    """Prompt block for the drafter: the dated events section 14 established, with their
    source ids, so a later section does not call a settled fact pending or absent."""
    evs = recent_events(ctx, limit)
    if not evs:
        return ""
    lines = []
    for e in evs:
        detail = f" — {e['detail']}" if e.get("detail") else ""
        lines.append(f"- {e['date']}: {e['event']}{detail} [{e['source_id']}]")
    return "\n".join(lines)


# ---- web research --------------------------------------------------------------------


def _rank(hit: dict[str, Any], prefer: tuple[str, ...], rank: int) -> float:
    dom = hit.get("domain", "")
    score = 0.0
    if any(dom == p or dom.endswith("." + p) for p in prefer):
        score += 3
    if tier_for_url(hit["url"]) == 1:
        score += 2
    if domain_policy(hit["url"]) == "limited":
        score -= 2
    score -= rank * 0.1
    return -score


def web_research(
    ctx: ToolContext,
    queries: list[str],
    *,
    type: str = "news",
    prefer_domains: tuple[str, ...] | list[str] = (),
    require_domains: tuple[str, ...] | list[str] | None = None,
    tbs: str | None = None,
    max_scrapes: int | None = None,
    scrape: bool = True,
    section_hint: str | None = None,
    exclude_domains: tuple[str, ...] | list[str] = (),
) -> list[dict[str, Any]]:
    """Search, post-filter, rank, scrape the top hits within the section budget.

    Returns hit rows ({url, title, description, domain, query, source_id?}); scraped rows carry
    a source_id. A budget stop is recorded in ctx.budget.exhausted, never raised."""
    fc = ctx.firecrawl
    if fc is None or not fc.available or not queries:
        if fc is None or not fc.available:
            ctx.budget.notes.append("Firecrawl not configured; web research skipped")
        return []
    hint = section_hint or f"sec-{ctx.section}"
    seen: dict[str, dict[str, Any]] = {}
    for q in queries:
        if not ctx.budget.can_search():
            break
        q = q.strip()
        if not q or "{" in q:  # unexpanded template placeholder
            continue
        try:
            hits = fc.search(q, limit=ctx.budget.hits_per_search, tbs=tbs, section_hint=hint)
            ctx.budget.used_searches += 1
        except BudgetExhausted as e:
            ctx.budget.exhausted = str(e)
            break
        except FirecrawlError as e:
            ctx.log(f"search failed for {q!r}: {e}")
            ctx.budget.used_searches += 1
            continue
        for rank, h in enumerate(hits):
            url = h["url"]
            dom = h.get("domain", "")
            if require_domains and not any(
                dom == d or dom.endswith("." + d) for d in require_domains
            ):
                continue
            if any(dom == d or dom.endswith("." + d) for d in exclude_domains):
                continue
            if url not in seen:
                seen[url] = {**h, "query": q, "_rank": _rank(h, tuple(prefer_domains), rank)}
    rows = sorted(seen.values(), key=lambda r: r["_rank"])
    if not scrape:
        return rows
    limit = max_scrapes if max_scrapes is not None else ctx.budget.scrapes
    scraped = 0
    for row in rows:
        if scraped >= limit or not ctx.budget.can_scrape():
            break
        url = row["url"]
        if ctx.budget.site_blocked(url):
            continue  # this site failed twice already
        try:
            if PDF_RE.search(url):
                res = fc.parse(url, type=type, section_hint=hint)
            else:
                res = fc.scrape(url, type=type, section_hint=hint)
            ctx.budget.used_scrapes += 1
            scraped += 1
            if res.get("source_id"):
                row["source_id"] = res["source_id"]
        except BudgetExhausted as e:
            ctx.budget.exhausted = str(e)
            break
        except FirecrawlError as e:
            ctx.log(f"scrape failed for {url}: {e}")
            ctx.budget.note_failed_scrape(url)
            continue
    return rows


def scrape_url(
    ctx: ToolContext, url: str, type: str = "web", section_hint: str | None = None, **kw: Any
) -> str | None:
    """Scrape one known URL within budget; returns the source_id or None."""
    fc = ctx.firecrawl
    if fc is None or not fc.available or not ctx.budget.can_scrape():
        return None
    if ctx.budget.site_blocked(url):
        return None
    try:
        if PDF_RE.search(url):
            res = fc.parse(url, type=type, section_hint=section_hint or f"sec-{ctx.section}", **kw)
        else:
            res = fc.scrape(url, type=type, section_hint=section_hint or f"sec-{ctx.section}", **kw)
        ctx.budget.used_scrapes += 1
        return res.get("source_id")
    except BudgetExhausted as e:
        ctx.budget.exhausted = str(e)
    except FirecrawlError as e:
        ctx.log(f"scrape failed for {url}: {e}")
        ctx.budget.note_failed_scrape(url)
    return None


# ---- evidence for the drafter ------------------------------------------------------------


def excerpt(text: str, max_chars: int, keywords: list[str] | None = None) -> str:
    """A window of the evidence text, centred on the first keyword hit when given."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    if keywords:
        low = text.lower()
        for k in keywords:
            i = low.find(k.lower())
            if i >= 0:
                start = max(0, i - max_chars // 4)
                return text[start : start + max_chars]
    return text[:max_chars]


def evidence_digest(
    ctx: ToolContext,
    source_ids: list[str],
    max_chars_each: int = 2500,
    total_chars: int = 40000,
    keywords: list[str] | None = None,
) -> str:
    """Evidence excerpts labelled by source id, the only text the drafter sees."""
    parts: list[str] = []
    used = 0
    for sid in source_ids:
        if not ctx.store.has(sid):
            continue
        rec = ctx.store.get(sid)
        body = excerpt(ctx.store.text(sid), max_chars_each, keywords)
        block = f"### [{sid}] {rec.title} — {rec.publisher}, {rec.published_date or 'n.d.'} (tier {rec.tier}, {rec.type})\n{body}\n"
        if used + len(block) > total_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def strip_uncited(text: str, allowed: set[str]) -> str:
    """Remove any citation token the drafter invented."""
    return re.sub(
        CITATION_TOKEN, lambda m: m.group(0) if m.group(1) in allowed else "", text
    ).strip()


def cited_ids(text: str) -> list[str]:
    return sorted(set(re.findall(CITATION_TOKEN, text)))


def draft_section(
    ctx: ToolContext,
    brief: Brief,
    *,
    evidence_sids: list[str],
    facts: Any = None,
    exhibit_md: str = "",
    extra_rules: str = "",
    critic_notes: list[str] | None = None,
    keywords: list[str] | None = None,
    role: str = "drafter",
) -> str | None:
    """compress_and_draft: strong model, house style, citing only the listed source ids.
    Returns None when no model is configured (callers fall back to template_digest)."""
    allowed = [s for s in dict.fromkeys(evidence_sids) if ctx.store.has(s)]
    if not allowed and not facts:
        return None
    events_block = recent_events_block(ctx)
    if events_block:
        # the drafter may cite section 14's sources for the events it is told about
        allowed = list(dict.fromkeys([*allowed, *(e["source_id"] for e in recent_events(ctx))]))
    words = int(float(brief.pages) * 380)
    cut = any(
        ("page budget" in n.lower()) or n.lower().startswith("cut to") for n in (critic_notes or [])
    )
    if cut:
        # the previous draft was over the page estimate: write well under it this time
        words = int(float(brief.pages) * 240)
    user = (
        f"Company: {ctx.entity.name} ({ctx.entity.ticker or 'private'}); aliases: {', '.join(brief.aliases or ctx.entity.aliases)}.\n"
        f"As of: {ctx.as_of.isoformat()}. Section {brief.section}: {brief.title}.\n"
        f"Scope: {brief.scope}\nSub-questions: {'; '.join(brief.sub_questions)}\n"
        f"Required exhibit (already built, appended after your text, do not repeat it): {brief.required_exhibit}\n"
        f"Common failure to avoid: {brief.common_failure}\n"
        f"Length: no more than {words} words of prose in total (a hard limit; stop when you reach it); the page budget is "
        f"{brief.pages} pages and the exhibit counts against it. Structure: one italic caveat line starting "
        f"'*Caveat:' then 2-4 short '###' subsections of declarative sentences, then a '### Perspectives' list "
        f"of at most 6 bullets where each bullet names the publisher or firm the view comes from (only if a "
        f"cited source names it).\n"
        f"Citations: end every factual sentence with one or more tokens like [src_0012] drawn ONLY from the "
        f"source ids in the evidence below. Numbers must come from the evidence or the structured facts.\n"
        + (f"Additional rules: {extra_rules}\n" if extra_rules else "")
        + (
            f"Critic notes from the previous iteration, address each: {critic_notes}\n"
            if critic_notes
            else ""
        )
        + (
            f"\nStructured facts (JSON, each with its source ids):\n{json.dumps(facts, default=str)[:12000]}\n"
            if facts
            else ""
        )
        + (
            f"\nExhibit that will follow your text (for context):\n{exhibit_md[:4000]}\n"
            if exhibit_md
            else ""
        )
        + (
            "\nDated events already established in section 14 (Recent Developments), all inside "
            "the trailing 12 months. Treat them as settled facts: cite their source ids where "
            "relevant and never describe one as pending, absent, unconfirmed or scheduled when "
            "it has happened:\n" + events_block + "\n"
            if events_block
            else ""
        )
        + "\nEvidence:\n"
        + evidence_digest(ctx, allowed, keywords=keywords)
    )
    text = invoke_text(role, HOUSE_STYLE, user, ctx.runlog)
    if not text:
        return None
    text = strip_uncited(text, set(allowed))
    # drop any heading the model added for the section itself
    text = re.sub(r"^#{1,2} .*\n", "", text).strip()
    return text or None


def template_digest(
    ctx: ToolContext,
    evidence_sids: list[str],
    lead_sentences: list[str] | None = None,
    max_items: int = 12,
) -> str:
    """Keyless fallback: structured sentences the handler supplied, then the evidence list.
    Produces no narrative; coverage is at best partial."""
    lines: list[str] = []
    if lead_sentences:
        lines.extend(s for s in lead_sentences if s)
    sids = [s for s in dict.fromkeys(evidence_sids) if ctx.store.has(s)]
    if sids:
        lines.append("")
        lines.append("### Evidence gathered (narrative not generated: no model configured)")
        lines.append("")
        for sid in sids[:max_items]:
            lines.append(f"- {ctx.source_line(sid)} [{sid}]")
    return "\n".join(lines).strip()


# ---- prose hygiene (review §9 item 8) ---------------------------------------------------

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?:\s*\[src_\d{4}\])*\s+(?=[A-Z0-9(\"'“])")
PERSPECTIVES_HEADING = re.compile(r"^#{2,5}\s*\**Perspectives\**\s*:?\s*$", re.I)
FIRST_PARTY_TYPES = {
    "earnings_call",
    "transcript",
    "api:edgar_xbrl",
    "api:edgar_submissions",
    "ir_map",
}
SUFFIX_RE = re.compile(
    r"\b(corp|corporation|inc|incorporated|co|company|ltd|plc|holdings?|group|llc|lp|se|ag|nv|sa|the)\b\.?",
    re.I,
)


def _case_preserving(repl):
    """Wrap a rewrite so a match that began a sentence keeps its capital letter."""

    def fn(m: re.Match[str]) -> str:
        out = repl(m) if callable(repl) else repl
        if m.group(0)[:1].isupper() and out[:1].islower():
            out = out[0].upper() + out[1:]
        return out

    return fn


def _split_sentences(text: str) -> list[str]:
    """Sentences of one prose line; each keeps the citation tokens that follow its period."""
    parts: list[str] = []
    last = 0
    for m in SENTENCE_SPLIT.finditer(text):
        parts.append(text[last : m.end()].strip())
        last = m.end()
    tail = text[last:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def scrub_pipeline_vocabulary(text: str) -> tuple[str, list[str]]:
    """Deterministic post-check: pipeline vocabulary never reaches prose.

    Sentences about caps, budgets and dropped queries are removed from the prose and
    returned (the caller puts them in the coverage line); the remaining pipeline words are
    rewritten to reader language. Table rows, blockquotes and headings are left alone."""
    moved: list[str] = []
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("|", ">", "#")):
            out_lines.append(line)
            continue
        indent = line[: len(line) - len(stripped)]
        bullet = ""
        m = re.match(r"([-*]\s+|\d+\.\s+)", stripped)
        if m:
            bullet = m.group(1)
            stripped = stripped[m.end() :]
        kept: list[str] = []
        for s in _split_sentences(stripped):
            if PIPELINE_MOVE.search(s):
                moved.append(re.sub(CITATION_TOKEN, "", s).strip().rstrip("."))
                continue
            for rx, repl in PIPELINE_REWRITES:
                s = rx.sub(_case_preserving(repl), s)
            kept.append(s)
        if not kept:
            continue  # a line that was only research notes disappears
        out_lines.append(indent + bullet + " ".join(kept))
    # collapse blank runs left by removed lines
    cleaned: list[str] = []
    for ln in out_lines:
        if not ln.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip(), moved


def _norm_name(s: str) -> str:
    s = SUFFIX_RE.sub(" ", s.lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def _company_domains(ctx: ToolContext) -> set[str]:
    doms: set[str] = set()
    site = ((ctx.foundation.get("ir") or {}).get("site") or "") if ctx.foundation else ""
    for raw in (site,):
        if not raw:
            continue
        host = re.sub(r"^https?://", "", raw).split("/")[0].lower()
        labels = host.split(".")
        if len(labels) >= 2:
            doms.add(".".join(labels[-2:]))
    return doms


def is_first_party_source(rec: SourceRecord, ctx: ToolContext) -> bool:
    """True for the company's own documents (filings, releases, transcripts, its own site)
    and for Wikipedia: neither is a second-party perspective."""
    url = (rec.url or "").lower()
    pub = rec.publisher or ""
    if "wikipedia.org" in url or "wikipedia" in pub.lower():
        return True
    if rec.type in FIRST_PARTY_TYPES:
        return True
    names = {_norm_name(ctx.entity.name), *(_norm_name(a) for a in ctx.entity.aliases)}
    names = {n for n in names if len(n) >= 4}
    npub = _norm_name(pub)
    if npub and any(n == npub or n in npub or (len(npub) >= 6 and npub in n) for n in names):
        return True
    if ctx.entity.cik and "sec.gov" in url:
        cik = str(int(ctx.entity.cik)) if ctx.entity.cik.isdigit() else ctx.entity.cik
        if re.search(rf"/data/0*{re.escape(cik)}/", url):
            return True
    host = re.sub(r"^https?://", "", url).split("/")[0]
    if any(host == d or host.endswith("." + d) for d in _company_domains(ctx)):
        return True
    return False


PER_PERSON_FIGURE = re.compile(
    r"per[- ](?:employee|head|user|subscriber|customer)|(?:employee|headcount)[- ]based",
    re.I,
)


def drop_unit_mismatch_conflicts(text: str) -> tuple[str, int]:
    """Remove a "> Conflict:" note that sets a per-employee, per-user or per-subscriber figure
    against a company total. Such a figure is a different quantity, so the two cannot conflict
    (SpaceX private run: "profits per employee of -$155,955" against Q3 net income of $4.4bn).
    Per-share notes stay: a consensus EPS against another EPS is a real conflict. Returns the
    text and the number of notes removed."""
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        if (
            line.lstrip().startswith(">")
            and re.match(r">\s*Conflict\b", line.lstrip())
            and PER_PERSON_FIGURE.search(line)
        ):
            dropped += 1
            continue
        kept.append(line)
    if not dropped:
        return text, 0
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), dropped


def drop_first_party_perspectives(narrative: str, ctx: ToolContext) -> tuple[str, bool]:
    """Remove the '### Perspectives' block when no bullet cites a second-party source
    (only the company's own documents or Wikipedia). Returns (narrative, dropped)."""
    lines = narrative.splitlines()
    start = next((i for i, ln in enumerate(lines) if PERSPECTIVES_HEADING.match(ln.strip())), None)
    if start is None:
        return narrative, False
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("#"):
        end += 1
    block = "\n".join(lines[start:end])
    sids = set(re.findall(CITATION_TOKEN, block))
    second_party = any(
        ctx.store.has(s) and not is_first_party_source(ctx.store.get(s), ctx) for s in sids
    )
    if second_party:
        return narrative, False
    kept = lines[:start] + lines[end:]
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, True


# ---- draft assembly -------------------------------------------------------------------


def caveat_line(text: str) -> str:
    text = text.strip().rstrip(".")
    return f"*Caveat: {text}.*"


def md_table(headers: list[str], rows: list[list[Any]], align_right_from: int = 1) -> str:
    def esc(v: Any) -> str:
        s = "n/a" if v is None or v == "" else str(v)
        return s.replace("|", "\\|").replace("\n", " ")

    head = "| " + " | ".join(esc(h) for h in headers) + " |"
    sep = (
        "|"
        + "|".join("---:" if i >= align_right_from else "---" for i in range(len(headers)))
        + "|"
    )
    body = ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def claim(
    section: int,
    metric: str,
    value: float | None,
    period: str,
    source_ids: list[str],
    unit: str = "USD",
    concept: str | None = None,
    note: str | None = None,
) -> Claim | None:
    if value is None:
        return None
    return Claim(
        section=section,
        metric=metric,
        value=float(value),
        unit=unit,
        period=period,
        source_ids=[s for s in source_ids if s],
        concept=concept,
        note=note,
    )


def finalize_draft(
    brief: Brief,
    ctx: ToolContext,
    *,
    caveat: str | None,
    narrative: str,
    exhibit_md: str,
    exhibit_present: bool,
    claims: list[Claim],
    coverage: str,
    notes: list[str] | None = None,
    exhibit_title: str | None = None,
) -> SectionDraft:
    """Assemble caveat + narrative + exhibit + coverage line into a SectionDraft.

    Prose hygiene (review §9 item 8) is applied here so every handler gets it: pipeline
    vocabulary is rewritten to reader language, research-limit sentences move into the
    coverage line, and a Perspectives block with no second-party source is dropped."""
    parts: list[str] = []
    hygiene_notes: list[str] = []
    if caveat and narrative and narrative.lstrip().startswith("*Caveat"):
        # the drafter wrote its own caveat line; the handler's deterministic one wins
        first, _, rest = narrative.lstrip().partition("\n")
        narrative = rest.strip()
    if caveat:
        caveat, moved = scrub_pipeline_vocabulary(caveat)
        hygiene_notes.extend(moved)
    if narrative:
        narrative, moved = scrub_pipeline_vocabulary(narrative)
        hygiene_notes.extend(moved)
        narrative, n_unit = drop_unit_mismatch_conflicts(narrative)
        if n_unit:
            hygiene_notes.append(
                "a per-person figure (per employee, per user) was not set against a company total"
            )
        narrative, dropped_persp = drop_first_party_perspectives(narrative, ctx)
        if dropped_persp:
            hygiene_notes.append(
                "Perspectives omitted: no second-party source (only the company's own documents or Wikipedia)"
            )
    if caveat:
        parts += [caveat, ""]
    if narrative:
        parts += [narrative, ""]
    if exhibit_md:
        if exhibit_title:
            parts += [f"### {exhibit_title}", ""]
        parts += [exhibit_md, ""]
    all_notes = [n for n in (notes or []) if n] + hygiene_notes
    if ctx.budget.exhausted:
        all_notes.append(f"research stopped at the section budget ({ctx.budget.exhausted})")
    if ctx.budget.notes:
        all_notes.extend(ctx.budget.notes)
    if coverage != "full" or all_notes:
        detail = "; ".join(dict.fromkeys(all_notes)) or "brief satisfied"
        parts.append(f"> Coverage: {coverage} — {detail}.")
    md = "\n".join(parts).strip()
    sids = set(cited_ids(md))
    for c in claims:
        sids.update(c.source_ids)
    return SectionDraft(
        section=brief.section,
        title=brief.title,
        markdown=md,
        claims=[c for c in claims if c is not None],
        source_ids=sorted(s for s in sids if ctx.store.has(s)),
        coverage=coverage,  # type: ignore[arg-type]
        caveat=caveat,
        exhibit_present=exhibit_present,
        budget=ctx.budget.to_dict(),
    )


def unavailable_draft(brief: Brief, reason: str, ctx: ToolContext | None = None) -> SectionDraft:
    return SectionDraft(
        section=brief.section,
        title=brief.title,
        markdown=f"> Coverage: unavailable — {reason}.",
        coverage="unavailable",
        budget=ctx.budget.to_dict() if ctx else {},
    )


def not_applicable_draft(brief: Brief, reason: str, ctx: ToolContext | None = None) -> SectionDraft:
    return SectionDraft(
        section=brief.section,
        title=brief.title,
        markdown=f"> Coverage: not_applicable — {reason}.",
        coverage="not_applicable",
        budget=ctx.budget.to_dict() if ctx else {},
    )


def expand_queries(brief: Brief, ctx: ToolContext, **subs: Any) -> list[str]:
    """Fill {co}/{fy}/{tk} and any extra placeholders; drop queries that still hold one."""
    base = {
        "co": ctx.co,
        "tk": ctx.entity.ticker or "",
        "fy": ctx.fy,
        **subs,
    }
    out = []
    for q in brief.web_queries:
        try:
            s = q.format(**base)
        except (KeyError, IndexError):
            continue
        if "{" not in s and s.strip():
            out.append(s.strip())
    return list(dict.fromkeys(out))


# ---- process-wide memo (shared across researcher threads in one run) ----------------------

_MEMO: dict[str, Any] = {}
_MEMO_LOCK = threading.Lock()


def run_memo(run_dir: str | Path, key: str, build):
    """Compute once per run and share between sections (e.g. the market-data pages)."""
    k = f"{Path(run_dir).resolve()}|{key}"
    with _MEMO_LOCK:
        if k in _MEMO:
            return _MEMO[k]
    val = build()
    with _MEMO_LOCK:
        _MEMO.setdefault(k, val)
        return _MEMO[k]


def fmt_date(d: date | str | None) -> str:
    if not d:
        return "n.d."
    if isinstance(d, str):
        return d[:10]
    return d.isoformat()
