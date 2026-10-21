"""Section 4, Key Programs and Contracts: the strictest section (requirements §6 row 4, §7
"Program and location sourcing rule").

plan   -> the structured calls (USAspending discovery by name, awards by UEI, award details,
          agency mix by FY, SAM.gov entity walk) and web-query templates for the GAO/SAR and
          DoD-announcement buckets; the {program} placeholders are filled in collect once the
          programs are known from the award records.
collect-> entity resolution by UEI (never by name): the name search only discovers the UEI
          set, every award figure then comes from the UEI-filtered calls. Programs are grouped
          from award descriptions using a keyword list derived from the 10-K Item 1 text and
          the designations in the records themselves; an award whose description names no
          program is reported as unassigned, never guessed.
draft  -> program table, agency-mix table, locations table (SAM.gov CAGE view, or the
          reduced place-of-performance view without a key), claims for every number, and the
          narrative (model when configured, else the template digest).

Gate: when the 10-K discloses no US-government revenue the section is not_applicable.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from primer.clients import dod_announcements as dod
from primer.config import section_budget, sources_config
from primer.sections import common
from primer.sections.common import ToolContext
from primer.sections.exhibits_sec04 import (
    NO_FINDING,
    agency_mix_table,
    locations_table,
    program_table,
    reduced_locations_table,
    slug,
)
from primer.sections.style import fmt_money
from primer.sections.text_tools import (
    norm_company,
    segment_names_from_text,
    sentences,
)
from primer.state import Brief, Claim, SectionDraft

SECTION = 4
FY_SPAN = 5  # awards window: FY(fy-5) .. FY(fy)
MIX_YEARS = 3
MAX_PROGRAMS = 8
MAX_AWARD_DETAILS = 8
GAO_PROGRAMS = 3
DOD_PROGRAMS = 2
DOD_SCRAPES = 3
GAO_SCRAPES = 4

GATE_REASON = "the 10-K discloses no US-government revenue"
ROLE_PRIME = "prime (USAspending prime award)"
ROLE_UNCONFIRMED = "not confirmed"

PROGRAM_RULE = (
    "Every contract value, ceiling, obligation, period of performance and awarding agency must "
    "cite a government record (USAspending, SAM.gov, a DoD contract announcement, GAO). Obligated "
    "(USAspending award total), ceiling/potential (USAspending base and all options) and announced "
    "(DoD announcement) are three different numbers: name which one every figure is and never add "
    "or compare them as if they were the same. State a prime or subcontract role only when a cited "
    "award record names it; otherwise write 'role: not confirmed'. Program status language "
    "('on schedule', 'Nunn-McCurdy breach', 'rebaselined', 'cost growth') may only be taken from "
    "the cited gao.gov, defense.gov or comptroller sources, with the report date; trade press is "
    "context only. Do not infer a program from an award description that does not name it."
)

STATUS_RE = re.compile(
    r"(nunn-mccurdy|breach|rebaselin|re-baselin|on schedule|behind schedule|ahead of schedule|"
    r"schedule (?:delay|slip)|delay(?:ed|s)?\b|slip(?:ped|s)?\b|cost growth|cost increase|"
    r"unit cost|overr[au]n|exceed(?:ed|s)? (?:its|the) (?:cost|schedule)|within budget)",
    re.I,
)
GAO_REPORT = re.compile(r"\bGAO-\d{2}-\d{3,7}[A-Z]{0,3}\b")
PUBLISHED = re.compile(r"Published:?\s*([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})")

DESIGNATION_WITH_NAME = re.compile(
    r"\b([A-Z]{1,4}-\d{1,3}[A-Z]{0,2})((?:\s+(?:[A-Z][a-z]+|[IVX]{1,4}|[A-Z]\d))*)"
)
LIST_LEAD = re.compile(r"\b(?:including|such as|programs?\s+(?:include|includes|included))\b", re.I)
ACRONYM = re.compile(r"(?<![A-Z0-9-])([A-Z][A-Z0-9]{2,7})(?![A-Z0-9-])")
ROMAN = re.compile(r"[IVX]{1,5}")
DEFINED = re.compile(r"([A-Z][A-Za-z0-9 ,&/-]{3,80}?)\s*\(([A-Z][A-Z0-9]{1,7})\)")
PARENTHESISED = re.compile(r"([^()]{3,120}?)\s*\(([A-Z][A-Z0-9]{1,7})\)")
NAME_TOKEN = re.compile(
    r"(?:[A-Z][a-z]+|[A-Z0-9][A-Z0-9&-]*[A-Z0-9]|[A-Z]|[IVX]{1,4}|\d{1,3}[A-Z]?)"
)

GENERIC_ACRONYMS = {
    "AIR",
    "SEA",
    "LAND",
    "ARMY",
    "NAVY",
    "USAF",
    "USN",
    "DOW",
    "LRIP",
    "IDIQ",
    "OEM",
    "R&D",
    "IRAD",
    "USA",
    "US",
    "DOD",
    "NASA",
    "FMS",
    "LLC",
    "INC",
    "CORP",
    "NYSE",
    "SEC",
    "GAAP",
    "CEO",
    "CFO",
    "COVID",
    "EPS",
    "ERISA",
    "GDP",
    "ESG",
    "NATO",
    "EU",
    "UK",
    "IRS",
    "FAA",
    "DCMA",
    "DCAA",
    "FAR",
    "DFARS",
    "CAS",
    "MRO",
    "RDT",
    "ITAR",
    "EAR",
    "OTA",
    "FRP",
    "EMD",
    "AAC",
    "UCA",
    "OT",
    "IGF",
    "TAS",
    "FY",
    "CY",
    "USMC",
    "USCG",
    "USSF",
    "LOT",
    "MOD",
    "ROR",
    "REP",
    "CLIN",
    "SOW",
    "PWS",
    "ECP",
    "PEO",
    "JPO",
    "IT",
    "AI",
    "NGI",
    "HQ",
    "ASC",
}
GENERIC_WORDS = {
    "command",
    "control",
    "system",
    "systems",
    "support",
    "services",
    "engineering",
    "logistics",
    "training",
    "sustainment",
    "production",
    "mission",
    "program",
    "programs",
    "contract",
    "modernization",
    "integration",
    "government",
    "congress",
    "europe",
    "asia",
    "africa",
    "canada",
    "australia",
    "japan",
    "korea",
    "israel",
    "poland",
    "germany",
    "italy",
    "taiwan",
    "saudi",
    "arabia",
    "united",
    "kingdom",
    "states",
    "america",
    "american",
    "pacific",
    "atlantic",
    "middle",
    "east",
    "north",
    "south",
    "west",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "note",
    "item",
    "part",
    "table",
    "board",
    "company",
    "corporation",
    "department",
    "defense",
    "defence",
    "army",
    "navy",
    "air",
    "force",
    "marine",
    "corps",
    "space",
    "products",
    "international",
    "commercial",
    "federal",
    "national",
    "security",
    "agency",
    "office",
    "missile",
    "missiles",
    "aircraft",
    "the",
    "other",
}
GENERIC_DESIGNATIONS = {"COVID", "SARS", "ISO", "AS", "ASC", "FAS", "SFAS", "IRC", "K", "Q"}


# ---- fiscal-year helpers ---------------------------------------------------------------


def federal_fy(d: date) -> int:
    """The federal fiscal year in progress on d (FY runs Oct 1 to Sep 30)."""
    return d.year + 1 if d.month >= 10 else d.year


def latest_complete_fy(d: date) -> int:
    return federal_fy(d) - 1


def _window(ctx: ToolContext) -> tuple[int, int]:
    hi = latest_complete_fy(ctx.as_of)
    return hi - FY_SPAN, hi


# ---- gate ------------------------------------------------------------------------------


def _gated(ctx: ToolContext) -> bool:
    gr = (ctx.foundation or {}).get("gov_revenue") or {}
    return not gr.get("disclosed")


# ---- plan ------------------------------------------------------------------------------


def _discovery_names(ctx: ToolContext) -> list[str]:
    names: list[str] = []
    for n in [ctx.co, ctx.entity.name, *ctx.entity.aliases]:
        n = " ".join((n or "").split())
        if n and n.upper() not in {x.upper() for x in names}:
            names.append(n)
    return names[:4]


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    if _gated(ctx):
        return []
    lo, hi = _window(ctx)
    names = _discovery_names(ctx)
    items: list[dict[str, Any]] = [
        {
            "tool": "usaspending.spending_by_award",
            "args": {
                "names": names,
                "fy_from": lo,
                "fy_to": hi,
                "limit": 100,
                "sort": "Award Amount",
            },
            "why": "UEI discovery: the recipient-name search only finds the UEI set (requirements §7: resolution by UEI, never by name)",
        },
        {
            "tool": "usaspending.spending_by_award",
            "args": {
                "recipients": "uei_set",
                "fy_from": lo,
                "fy_to": hi,
                "limit": 100,
                "sort": "Award Amount",
            },
            "why": "prime awards by UEI, FY-5 to date, sorted by value; programs grouped from the descriptions",
        },
        {
            "tool": "usaspending.award",
            "args": {"programs": MAX_AWARD_DETAILS},
            "why": "per-award record with gov_id; ceiling = base_and_all_options; place of performance city",
        },
        {
            "tool": "usaspending.spending_by_category",
            "args": {"category": "awarding_subagency", "fys": [hi - MIX_YEARS + 1, hi]},
            "why": "agency mix by federal fiscal year",
        },
        {
            "tool": "samgov.walk_parent",
            "args": {
                "legal_name": ctx.entity.name,
                "filter": f"active, place of performance on an award FY{lo}-FY{hi}",
            },
            "why": "locations by CAGE"
            if ctx.samgov is not None
            else "skipped: no SAM_GOV_API_KEY (reduced place-of-performance view)",
        },
        {
            "tool": "dod.announcements",
            "args": {"programs": DOD_PROGRAMS, "scrapes": DOD_SCRAPES},
            "why": "latest announced awards keyed by contract number, joined to USAspending PIIDs",
        },
    ]
    for q in (
        "{program} GAO report",
        "{program} selected acquisition report",
        "{program} Nunn-McCurdy OR rebaseline OR schedule",
    ):
        items.append(
            {
                "tool": "firecrawl.search",
                "query_template": q,
                "bucket": "gao",
                "why": "program status only from GAO/SAR/DoD sources",
            }
        )
    items.append(
        {
            "tool": "firecrawl.search",
            "query_template": "{co} contract award Contracts {program} defense.gov OR war.gov",
            "bucket": "dod",
            "why": "DoD daily contract announcements",
        }
    )
    return items


# ---- collect ----------------------------------------------------------------------------


def resolve_uei_set(
    rows: list[dict[str, Any]], legal_name: str
) -> tuple[list[str], dict[str, str]]:
    """UEIs whose recipient name normalises to the company name, starts with it, or looks like
    a subsidiary (first word of the company name leads the recipient name)."""
    me = norm_company(legal_name)
    first = me.split()[0] if me else ""
    ueis: dict[str, float] = {}
    names: dict[str, str] = {}
    for r in rows:
        uei = (r.get("Recipient UEI") or "").strip()
        name = r.get("Recipient Name") or ""
        if not uei:
            continue
        n = norm_company(name)
        keep = bool(n) and (
            n == me or n.startswith(me + " ") or (bool(first) and n.split()[0] == first)
        )
        if not keep:
            continue
        ueis[uei] = ueis.get(uei, 0.0) + float(r.get("Award Amount") or 0)
        names.setdefault(uei, name)
    ordered = [u for u, _ in sorted(ueis.items(), key=lambda kv: -kv[1])]
    return ordered, {u: names[u] for u in ordered}


def _parent_uei(
    uei_set: list[str], uei_names: dict[str, str], legal_name: str, details: list[dict[str, Any]]
) -> str | None:
    for d in details:
        p = ((d.get("recipient") or {}).get("parent_recipient_uei") or "").strip()
        if p and p in uei_set:
            return p
    me = norm_company(legal_name)
    for u in uei_set:  # ordered by award total
        if norm_company(uei_names.get(u, "")) == me:
            return u
    return uei_set[0] if uei_set else None


def program_keywords(
    item1: str,
    company_name: str,
    segment_names: list[str] | None = None,
    stop: set[str] | None = None,
) -> dict[str, list[str]]:
    """{canonical program: [match terms]} from the 10-K Item 1 text: military designations with
    their names (F-35 Lightning II), all-caps acronyms that are not segment abbreviations or
    generic terms (THAAD, HIMARS), and capitalised names in 'including / such as / programs
    include' lists (Black Hawk, Aegis, Trident II D5). Paragraphs about competition are skipped.
    No company-specific list: everything comes from the filing text."""
    out: dict[str, list[str]] = {}
    me = set(norm_company(company_name).split()) | {s.lower() for s in (stop or set()) if s}
    segs = {s.lower() for s in (segment_names or segment_names_from_text(item1))}

    def add(canon: str, *terms: str) -> None:
        canon = canon.strip()
        if not canon:
            return
        lst = out.setdefault(canon, [])
        for t in (canon, *terms):
            t = " ".join(t.split()).upper()
            if t and t not in lst and len(t) >= 3:
                lst.append(t)

    paras = [p for p in re.split(r"\n\s*\n", item1) if "compet" not in p.lower()]
    text = "\n\n".join(paras)
    for m in DESIGNATION_WITH_NAME.finditer(text):
        desig, name = m.group(1), " ".join(m.group(2).split())
        if re.fullmatch(r"[A-Z]{1,4}-\d{1,3}[A-Z]{0,2}", desig) is None:
            continue
        if desig.split("-")[0] in GENERIC_DESIGNATIONS:
            continue
        aliases = [name] if name and name.lower() not in GENERIC_WORDS else []
        add(desig, *aliases)
    defined = {acr: phrase.strip() for phrase, acr in DEFINED.findall(text)}
    for acr in ACRONYM.findall(text):
        if acr in GENERIC_ACRONYMS or acr in out or re.search(r"\d", acr) or acr.lower() in me:
            continue
        if ROMAN.fullmatch(acr):
            continue
        phrase = defined.get(acr, "")
        if phrase and any(s in phrase.lower() for s in segs if s):
            continue  # segment abbreviation (MFC, RMS)
        add(acr, *([phrase] if phrase and len(phrase.split()) <= 6 else []))
    for m in LIST_LEAD.finditer(text):
        tail = text[m.end() : m.end() + 400]
        tail = re.split(r"[.;\n]", tail, maxsplit=1)[0]
        for item in re.split(r",|\band\b|\bor\b", tail):
            toks = item.strip(" .:").split()
            while toks and toks[0].lower() in ("the", "a", "an", "our"):
                toks = toks[1:]
            # keep the leading run of capitalised tokens
            run: list[str] = []
            for t in toks:
                if NAME_TOKEN.fullmatch(t) and not (t.lower() in GENERIC_WORDS and not run):
                    run.append(t)
                else:
                    break
            while run and run[-1].lower() in GENERIC_WORDS:
                run.pop()
            if not run or len(run) > 4:
                continue
            name = " ".join(run)
            if not re.search(r"[A-Za-z]", name):
                continue
            if all(t.lower() in me or t.lower() in GENERIC_WORDS for t in run):
                continue
            if any(norm_company(name) == s for s in segs):
                continue
            desig = next((t for t in run if re.fullmatch(r"[A-Z]{1,4}-\d{1,3}[A-Z]{0,2}", t)), None)
            if desig:
                if desig.split("-")[0] in GENERIC_DESIGNATIONS:
                    continue
                rest = " ".join(t for t in run if t != desig)
                add(desig, *([rest] if rest else []))
            elif len(name) >= 3 and not re.fullmatch(r"[A-Z]{1,2}", name):
                add(name)
    # 'GPS' and 'GPS III' are one program: the longer name absorbs the shorter one's terms
    for short in sorted(out, key=len):
        longer = next(
            (k for k in out if k != short and k.upper().startswith(short.upper() + " ")), None
        )
        if longer and short in out:
            for t in out[short]:
                if t not in out[longer]:
                    out[longer].append(t)
            del out[short]
    return out


def _term_regex(term: str) -> re.Pattern[str]:
    if re.fullmatch(r"[A-Z]{1,4}-\d{1,3}", term):
        return re.compile(rf"(?<![A-Z0-9]){re.escape(term)}[A-Z]{{0,2}}(?![A-Z0-9-])")
    return re.compile(rf"(?<![A-Z0-9]){re.escape(term)}(?![A-Z0-9])")


def learn_aliases(descriptions: list[str], keywords: dict[str, list[str]]) -> dict[str, list[str]]:
    """Acronyms the records define themselves: 'F-35 LIGHTNING II JOINT STRIKE FIGHTER (JSF)'
    teaches JSF -> F-35. Generic acquisition acronyms (LRIP, UCA) are never learned."""
    learned: dict[str, set[str]] = {}
    for d in descriptions:
        up = (d or "").upper()
        for phrase, acr in PARENTHESISED.findall(up):
            if acr in GENERIC_ACRONYMS or len(acr) < 2:
                continue
            hits = {
                prog
                for prog, terms in keywords.items()
                if any(_term_regex(t).search(phrase) for t in terms)
            }
            if len(hits) == 1:
                learned.setdefault(hits.pop(), set()).add(acr)
    out = {k: list(v) for k, v in keywords.items()}
    for prog, acrs in learned.items():
        for a in sorted(acrs):
            if all(a not in terms for terms in out.values()):
                out[prog].append(a)
    return out


def assign_program(description: str | None, keywords: dict[str, list[str]]) -> str | None:
    """Earliest-matching program term in the description; longer term wins a tie."""
    up = (description or "").upper()
    if not up:
        return None
    best: tuple[int, int, str] | None = None
    for prog, terms in keywords.items():
        for t in terms:
            m = _term_regex(t).search(up)
            if m and (best is None or (m.start(), -len(t)) < (best[0], -best[1])):
                best = (m.start(), len(t), prog)
    return best[2] if best else None


def group_programs(
    awards: list[dict[str, Any]], keywords: dict[str, list[str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(programs sorted by obligated desc, unassigned awards)."""
    groups: dict[str, dict[str, Any]] = {}
    unassigned: list[dict[str, Any]] = []
    for a in awards:
        prog = assign_program(a.get("Description"), keywords)
        if not prog:
            unassigned.append(a)
            continue
        g = groups.setdefault(prog, {"program": prog, "awards": []})
        g["awards"].append(a)
    programs = []
    for g in groups.values():
        aw = sorted(g["awards"], key=lambda r: -float(r.get("Award Amount") or 0))
        top = aw[0]
        by_agency: dict[str, float] = {}
        for r in aw:
            k = r.get("Awarding Sub Agency") or r.get("Awarding Agency") or "n/a"
            by_agency[k] = by_agency.get(k, 0.0) + float(r.get("Award Amount") or 0)
        customers = [k for k, _ in sorted(by_agency.items(), key=lambda kv: -kv[1])][:2]
        starts = [r.get("Start Date") for r in aw if r.get("Start Date")]
        ends = [r.get("End Date") for r in aw if r.get("End Date")]
        programs.append(
            {
                "program": g["program"],
                "awards": aw,
                "award_count": len(aw),
                "obligated": sum(float(r.get("Award Amount") or 0) for r in aw),
                "customer": "; ".join(customers),
                "role": ROLE_PRIME,
                "vehicle": top.get("Contract Award Type") or "n/a",
                "piid": top.get("Award ID"),
                "generated_internal_id": top.get("generated_internal_id"),
                "pop_start": min(starts) if starts else None,
                "pop_end": max(ends) if ends else None,
                "obligated_sids": sorted({r["source_id"] for r in aw if r.get("source_id")}),
                "gov_ids": [r.get("generated_internal_id") or r.get("Award ID") for r in aw[:5]],
                "pop_states": sorted(
                    {
                        r.get("Place of Performance State Code")
                        for r in aw
                        if r.get("Place of Performance State Code")
                    }
                ),
                "ceiling": None,
                "ceiling_sid": None,
                "status": None,
                "announced": None,
            }
        )
    programs.sort(key=lambda p: -p["obligated"])
    return programs, unassigned


def _ensure_record(
    ctx: ToolContext, resp: dict[str, Any], title: str, gov_id: str, seen: dict[str, str]
) -> str | None:
    """The client registers every response; when two calls collapse into one record (same URL,
    no gov_id) register a distinct record so each table cell cites the payload it came from."""
    sid = resp.get("source_id")
    if sid and sid not in seen:
        seen[sid] = gov_id
        return sid
    if sid and seen.get(sid) == gov_id:
        return sid
    payload = {k: v for k, v in resp.items() if k != "source_id"}
    rec = ctx.store.register(
        "```json\n" + json.dumps(payload, indent=1)[:200_000] + "\n```",
        type="api:usaspending",
        publisher="USAspending.gov",
        title=title,
        url="https://www.usaspending.gov/search",
        tier=1,
        gov_id=gov_id,
        meta={"section": SECTION},
        dedupe_key=f"usaspending|sec4|{gov_id}|{ctx.store.as_of.isoformat()}",
    )
    seen[rec.source_id] = gov_id
    return rec.source_id


def _clean_markdown(text: str) -> str:
    """Drop images, link targets, share buttons and headings so sentences split cleanly."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"(?m)^\s*#+.*$", " ", t)
    t = re.sub(
        r"Skip to (?:main content|Highlights)|Share (?:on|by) [A-Za-z ]+|Published:[^.]*\.", " ", t
    )
    return re.sub(r"[ \t]+", " ", t)


def _host_ok(url: str, hosts: list[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def gao_finding(ctx: ToolContext, sid: str, terms: list[str]) -> dict[str, Any] | None:
    """Status sentence naming the program and a status phrase, from a program_status_hosts page."""
    if not ctx.store.has(sid):
        return None
    rec = ctx.store.get(sid)
    hosts = list(sources_config().get("program_status_hosts", []))
    if not _host_ok(rec.url, hosts):
        return None
    text = ctx.store.text(sid)
    body = _clean_markdown(text)
    # GAO product pages: the "What GAO Found" block is the finding; prefer it
    i = body.find("What GAO Found")
    if i >= 0:
        body = body[i + len("What GAO Found") :]
    found = None
    for s in sentences(body):
        if (
            40 <= len(s) <= 500
            and STATUS_RE.search(s)
            and any(_term_regex(t).search(s.upper()) for t in terms)
        ):
            found = s
            break
    if found is None:
        # the program may be named in the heading or a neighbouring sentence; accept a status
        # sentence when the page as a whole names the program and the sentence names DOD/program
        if any(_term_regex(t).search((rec.title or "").upper()) for t in terms):
            for s in sentences(body):
                if (
                    40 <= len(s) <= 500
                    and STATUS_RE.search(s)
                    and re.search(r"\b(program|aircraft|missile|contractor|DOD|Department)\b", s)
                ):
                    found = s
                    break
    if not found:
        return None
    report = GAO_REPORT.search(text)
    pub = rec.published_date.isoformat() if rec.published_date else None
    if not pub:
        m = PUBLISHED.search(text)
        d = dod.parse_ap_date(m.group(1)) if m else None
        pub = d.isoformat() if d else None
    return {
        "text": " ".join(found.split())[:220].rstrip(".,;") + ("…" if len(found) > 220 else ""),
        "sids": [sid],
        "date": pub,
        "report": report.group(0) if report else None,
        "publisher": rec.publisher,
    }


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    if _gated(ctx):
        return {"not_applicable": True}
    us = ctx.usaspending
    if us is None:
        return {"error": "USAspending client not available"}
    lo, hi = _window(ctx)
    res: dict[str, Any] = {"window": [lo, hi], "notes": [], "uei_set": [], "uei_names": {}}
    seen: dict[str, str] = {}

    # 1. UEI discovery by name (the only name-based call) ---------------------------------
    names = _discovery_names(ctx)
    disc = us.spending_by_award(names, lo, hi, limit=100, sort="Award Amount")
    disc_sid = _ensure_record(
        ctx,
        disc,
        f"USAspending recipient-name discovery FY{lo}-FY{hi}: {', '.join(names)}",
        f"discovery:{'|'.join(names)}",
        seen,
    )
    uei_set, uei_names = resolve_uei_set(disc.get("results") or [], ctx.entity.name)
    res.update({"uei_set": uei_set, "uei_names": uei_names, "discovery_sid": disc_sid})
    ctx.runlog.meta["uei_set"] = list(uei_set)
    ctx.runlog.meta["uei_names"] = dict(uei_names)
    if not uei_set:
        res["notes"].append("no UEI resolved from the USAspending recipient search")
        return res

    # 2. Prime awards by UEI ------------------------------------------------------------------
    awards: list[dict[str, Any]] = []
    page_sids: list[str] = []
    for page in (1, 2):
        resp = us.spending_by_award(uei_set, lo, hi, limit=100, page=page, sort="Award Amount")
        sid = _ensure_record(
            ctx,
            resp,
            f"USAspending prime awards by UEI FY{lo}-FY{hi} (page {page})",
            f"awards_by_uei:p{page}:{','.join(uei_set[:6])}",
            seen,
        )
        page_sids.append(sid or "")
        rows = resp.get("results") or []
        for r in rows:
            awards.append({**r, "source_id": sid})
        if len(rows) < 100 or not (resp.get("page_metadata") or {}).get("hasNext"):
            break
    res["awards_page_sids"] = [s for s in page_sids if s]
    res["award_count"] = len(awards)
    if not awards:
        res["notes"].append("USAspending returned no prime awards for the UEI set")
        return res

    # 3. Programs from the records -------------------------------------------------------------
    item1, tenk_sid = ctx.item_text("1")
    keywords = program_keywords(
        item1, ctx.entity.name, stop={ctx.entity.ticker or "", *ctx.entity.aliases}
    )
    keywords = learn_aliases([a.get("Description") or "" for a in awards], keywords)
    programs, unassigned = group_programs(awards, keywords)
    res["keywords"] = keywords
    res["tenk_sid"] = tenk_sid
    res["unassigned"] = {
        "count": len(unassigned),
        "obligated": sum(float(a.get("Award Amount") or 0) for a in unassigned),
        "examples": [(a.get("Description") or "")[:60] for a in unassigned if a.get("Description")][
            :3
        ],
        "sids": sorted({a["source_id"] for a in unassigned if a.get("source_id")}),
    }

    # 4. Award details (gov_id per award; ceiling; place-of-performance city) --------------------
    details: list[dict[str, Any]] = []
    for p in programs[:MAX_AWARD_DETAILS]:
        gid = p.get("generated_internal_id")
        if not gid:
            continue
        try:
            d = us.award(gid)
        except Exception as e:  # noqa: BLE001 - one bad award must not sink the section
            ctx.log(f"award detail {gid}: {e!r}")
            continue
        # the record must belong to this program (a stale cache or a redirect is not evidence)
        piids = {dod.normalize_contract_number(a.get("Award ID")) for a in p["awards"]}
        if dod.normalize_contract_number(d.get("piid")) not in piids:
            ctx.log(
                f"award detail {gid}: record piid {d.get('piid')!r} is not a {p['program']} award; skipped"
            )
            continue
        d["_program"] = p["program"]
        details.append(d)
        if d.get("source_id"):
            p["ceiling"] = d.get("base_and_all_options")
            p["ceiling_sid"] = d["source_id"]
            p["ceiling_piid"] = d.get("piid")
            p["detail_sid"] = d["source_id"]
            p["obligated_sids"] = sorted(set(p["obligated_sids"]) | {d["source_id"]})
            p["gov_ids"] = [
                d.get("generated_unique_award_id") or gid,
                *[g for g in p["gov_ids"] if g != gid],
            ]
    res["details"] = [
        {
            "program": d.get("_program"),
            "source_id": d.get("source_id"),
            "piid": d.get("piid"),
            "generated_unique_award_id": d.get("generated_unique_award_id"),
            "base_and_all_options": d.get("base_and_all_options"),
            "total_obligation": d.get("total_obligation"),
            "pop_city": ((d.get("place_of_performance") or {}).get("city_name") or "").upper()
            or None,
            "pop_state": ((d.get("place_of_performance") or {}).get("state_code") or "").upper()
            or None,
        }
        for d in details
    ]
    parent_uei = _parent_uei(uei_set, uei_names, ctx.entity.name, details)
    res["parent_uei"] = parent_uei
    ctx.runlog.meta["parent_uei"] = parent_uei

    # 5. Agency mix by FY ----------------------------------------------------------------------
    mix: dict[int, dict[str, Any]] = {}
    for fy in range(hi - MIX_YEARS + 1, hi + 1):
        try:
            resp = us.spending_by_category("awarding_subagency", uei_set, fy, fy, limit=10)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"spending_by_category FY{fy}: {e!r}")
            mix[fy] = {"source_id": None, "rows": []}
            continue
        rows = [
            {
                "name": r.get("name"),
                "slug": r.get("subagency_slug") or slug(r.get("name") or ""),
                "amount": float(r.get("amount") or 0),
            }
            for r in (resp.get("results") or [])
            if r.get("name")
        ]
        sid = (
            _ensure_record(
                ctx,
                resp,
                f"USAspending obligations by awarding sub-agency FY{fy} for the UEI set",
                f"awarding_subagency:FY{fy}:{','.join(uei_set[:6])}",
                seen,
            )
            if rows
            else None
        )
        mix[fy] = {"source_id": sid, "rows": rows}
    res["mix"] = mix

    # 6. SAM.gov entity walk --------------------------------------------------------------------
    res["sam"] = {"available": ctx.samgov is not None, "entities": []}
    if ctx.samgov is not None and parent_uei:
        try:
            ents = ctx.samgov.walk_parent(parent_uei, ctx.entity.name)
            for e in ents:
                reg = e.get("entityRegistration") or {}
                addr = (e.get("coreData") or {}).get("physicalAddress") or {}
                res["sam"]["entities"].append(
                    {
                        "uei": reg.get("ueiSAM"),
                        "cage": reg.get("cageCode"),
                        "name": reg.get("legalBusinessName"),
                        "status": reg.get("registrationStatus"),
                        "expiry": reg.get("registrationExpirationDate"),
                        "city": (addr.get("city") or "").upper() or None,
                        "state": (addr.get("stateOrProvinceCode") or "").upper() or None,
                        "source_id": e.get("source_id"),
                    }
                )
        except Exception as e:  # noqa: BLE001
            ctx.log(f"samgov walk_parent: {e!r}")
            res["sam"]["error"] = repr(e)[:200]
            res["notes"].append(
                "SAM.gov entity walk failed; locations shown from award places of performance"
            )
    elif ctx.samgov is None:
        res["notes"].append("SAM.gov registrations not available (no API key)")

    # 7. Web research: GAO/SAR status, DoD announcements, context --------------------------------
    hosts = tuple(sources_config().get("program_status_hosts", ["gao.gov"]))
    top = programs[:GAO_PROGRAMS]
    gao_queries = [f"{p['program']} GAO report" for p in top]
    if top:
        gao_queries.append(
            f"{top[0]['program']} selected acquisition report OR Nunn-McCurdy OR rebaseline"
        )
    gao_hits = (
        ctx.web_research(
            gao_queries,
            type="gov_report",
            require_domains=hosts,
            prefer_domains=("gao.gov",),
            max_scrapes=GAO_SCRAPES,
        )
        if top
        else []
    )
    res["gao"] = []
    for p in programs:
        for h in gao_hits:
            sid = h.get("source_id")
            if not sid:
                continue
            f = gao_finding(ctx, sid, keywords.get(p["program"], [p["program"]]))
            if f:
                p["status"] = f
                res["gao"].append({"program": p["program"], **f})
                break
    res["gao_pages"] = [h.get("source_id") for h in gao_hits if h.get("source_id")]

    dod_queries = [
        f"{ctx.co} {p['program']} contract award Contracts war.gov OR defense.gov"
        for p in programs[:DOD_PROGRAMS]
    ]
    dod_hits = (
        ctx.web_research(
            dod_queries,
            type="dod_announcement",
            require_domains=("defense.gov", "war.gov"),
            max_scrapes=DOD_SCRAPES,
        )
        if dod_queries
        else []
    )
    company_terms = [ctx.co, ctx.entity.name, *ctx.entity.aliases]
    dod_rows: list[dict[str, Any]] = []
    dod_pages = [h for h in dod_hits if h.get("source_id")]
    for h in dod_pages:
        rec = ctx.store.get(h["source_id"])
        dod_rows.extend(
            dod.parse_announcements(
                ctx.store.text(h["source_id"]),
                company_terms,
                title=rec.title,
                url=rec.url,
                program_terms=keywords,
                source_id=h["source_id"],
            )
        )
    dod_path = "regex"
    if (
        not dod_rows
        and dod_pages
        and section_budget(SECTION).get("use_agent")
        and ctx.firecrawl is not None
    ):
        try:
            data = ctx.firecrawl.agent(
                f"Extract every contract award paragraph that names {ctx.co} from these DoD daily contract announcement pages: contract number, recipient, announced amount in USD, contract type, description, work locations, expected completion, contracting activity, announcement date, page url.",
                dod.AGENT_SCHEMA,
                urls=[h["url"] for h in dod_pages],
                timeout_s=240,
            )
            dod_rows = dod.rows_from_agent(data, {h["url"]: h["source_id"] for h in dod_pages})
            dod_path = "agent"
        except Exception as e:  # noqa: BLE001
            ctx.log(f"dod agent: {e!r}")
            dod_path = "agent_failed"
    elif not dod_rows and dod_pages:
        dod_path = "regex_empty"
    res["dod"] = {"path": dod_path, "pages": [h["source_id"] for h in dod_pages], "rows": dod_rows}
    _join_announcements(programs, dod_rows)

    extra = [
        i.get("query")
        for i in plan_items
        if i.get("tool") == "firecrawl.search" and i.get("query") and not i.get("bucket")
    ]
    if extra and ctx.budget.used_searches < ctx.budget.searches:
        ctx.web_research(
            [q for q in extra if q][:2],
            type="news",
            prefer_domains=tuple(ctx.brief.trade_press if ctx.brief else ()),
            max_scrapes=2,
        )

    res["programs"] = [
        {k: v for k, v in p.items() if k != "awards"}
        | {"piids": [a.get("Award ID") for a in p["awards"]], "pop_rows": _pop_rows(p["awards"])}
        for p in programs
    ]
    return res


def _pop_rows(awards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "state": a.get("Place of Performance State Code"),
            "city_code": a.get("Place of Performance City Code"),
            "country": a.get("Place of Performance Country Code"),
            "source_id": a.get("source_id"),
            "piid": a.get("Award ID"),
        }
        for a in awards
    ]


def _join_announcements(programs: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    """Latest DoD announcement per program: joined by normalised contract number to a program's
    PIIDs, or by the program name in the announcement paragraph (a DoD record naming it)."""
    for p in programs:
        piids = {dod.normalize_contract_number(a.get("Award ID")) for a in p["awards"]}
        cands = [
            r
            for r in rows
            if (r.get("piid") and r["piid"] in piids) or p["program"] in (r.get("programs") or [])
        ]
        cands = [r for r in cands if r.get("amount_usd") is not None and r.get("source_id")]
        if not cands:
            continue
        best = sorted(cands, key=lambda r: (r.get("announcement_date") or "", r["amount_usd"]))[-1]
        p["announced"] = {
            "amount_usd": best["amount_usd"],
            "sids": [best["source_id"]],
            "date": best.get("announcement_date"),
            "contract_number": best.get("contract_number"),
            "joined_by": "piid" if best.get("piid") in piids else "program name in announcement",
            "contracting_activity": best.get("contracting_activity"),
            "completion": best.get("completion"),
            "work_locations": best.get("work_locations"),
        }


# ---- draft -------------------------------------------------------------------------------


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    if _gated(ctx) or tool_results.get("not_applicable"):
        return common.not_applicable_draft(brief, GATE_REASON, ctx)
    programs: list[dict[str, Any]] = list(tool_results.get("programs") or [])
    if tool_results.get("error") or not tool_results.get("award_count"):
        reason = (
            tool_results.get("error")
            or "; ".join(tool_results.get("notes") or [])
            or "no USAspending awards fetched"
        )
        return common.unavailable_draft(brief, reason, ctx)
    lo, hi = tool_results.get("window") or _window(ctx)
    window = f"FY{lo}-FY{hi}"
    notes: list[str] = list(tool_results.get("notes") or [])
    claims: list[Claim] = []
    sids: list[str] = []
    page_sids = list(tool_results.get("awards_page_sids") or [])
    tenk_sid = tool_results.get("tenk_sid")

    # exhibits
    shown = programs[:MAX_PROGRAMS]
    ex_prog = program_table(shown, window)
    mix = {int(k): v for k, v in (tool_results.get("mix") or {}).items()}
    ex_mix = agency_mix_table(mix)
    loc_rows, loc_caption, sam_view = _location_rows(tool_results, programs, ctx)
    if sam_view:
        ex_loc = locations_table(loc_rows, window)
    else:
        ex_loc = reduced_locations_table(loc_rows, tenk_sid)
    for ex in (ex_prog, ex_mix, ex_loc):
        claims.extend(ex.claims)
        sids.extend(ex.source_ids)
        notes.extend(ex.notes)

    # coverage
    cited_rows = sum(1 for p in shown if p.get("obligated_sids"))
    coverage = "full" if (cited_rows >= 3 and ex_mix.present and ex_loc.present) else "partial"
    if cited_rows < 3:
        notes.append(f"only {cited_rows} program rows with USAspending citations")
    if not ex_mix.present:
        notes.append("agency-mix table empty")
    if not ex_loc.present:
        notes.append("no location row could be tied to an award place of performance")
    if not sam_view:
        coverage = "partial"
    if not tool_results.get("gao"):
        notes.append("no GAO/SAR finding fetched for the top programs")
    d = tool_results.get("dod") or {}
    if d.get("path") in ("regex_empty", "agent_failed"):
        notes.append(f"DoD announcement parse empty ({d['path']})")
    elif not d.get("pages"):
        notes.append("no DoD contract announcement page fetched")
    un = tool_results.get("unassigned") or {}
    if un.get("count"):
        notes.append(
            f"{un['count']} award(s) totalling ${fmt_money(un.get('obligated'))}m obligated carry descriptions that name no program "
            f"(e.g. {'; '.join(repr(x) for x in un.get('examples') or [])}) and are not assigned"
        )
    uei_set = tool_results.get("uei_set") or []
    notes.append(
        f"entity resolved to {len(uei_set)} UEI(s): {', '.join(uei_set)}; parent UEI {tool_results.get('parent_uei') or 'n/a'}"
    )
    if coverage == "full":
        notes.insert(0, "brief satisfied")

    # exhibit markdown with captions (captions cite the records they describe)
    page_cite = "".join(f" [{s}]" for s in page_sids)
    parts = [
        "### Program table",
        "",
        f"*Prime awards to the resolved UEI set with activity in {window}, grouped by the program named in the award description{page_cite}. "
        "Obligated is the USAspending award total to date; ceiling is USAspending base and all options for the largest award; "
        "the announced value is the latest DoD daily contract announcement and is a third, separate number. "
        "Status text comes only from the cited GAO/SAR/DoD source; otherwise the cell says so.*",
        "",
        ex_prog.markdown or "_no program could be named from an award description_",
        "",
        "### Agency mix",
        "",
        f"*Prime contract obligations by awarding sub-agency, USD millions, federal fiscal years (Oct-Sep), top {5} per year.*",
        "",
        ex_mix.markdown or "_no agency-mix data returned_",
        "",
        "### Locations",
        "",
        f"*{loc_caption}*",
        "",
        ex_loc.markdown or "_no location could be tied to an award place of performance_",
    ]
    exhibit_md = "\n".join(parts)

    # narrative
    evidence = list(
        dict.fromkeys(
            [
                *page_sids,
                *[p.get("detail_sid") for p in shown if p.get("detail_sid")],
                *tool_results.get("gao_pages", []),
                *d.get("pages", []),
                *[r["sam_sid"] for r in loc_rows if r.get("sam_sid")],
                *(m.get("source_id") for m in mix.values() if m.get("source_id")),
                tenk_sid,
            ]
        )
    )
    evidence = [s for s in evidence if s]
    facts = {
        "uei_set": uei_set,
        "programs": [
            {
                k: p.get(k)
                for k in (
                    "program",
                    "customer",
                    "role",
                    "piid",
                    "obligated",
                    "ceiling",
                    "award_count",
                    "obligated_sids",
                    "ceiling_sid",
                    "status",
                    "announced",
                )
            }
            for p in shown
        ],
        "agency_mix": {f"FY{fy}": m.get("rows", [])[:5] for fy, m in mix.items()},
        "locations": loc_rows[:12],
    }
    keywords = [p["program"] for p in shown]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts,
        exhibit_md=exhibit_md,
        extra_rules=PROGRAM_RULE,
        critic_notes=critic_notes,
        keywords=keywords,
    ) or common.template_digest(
        ctx, evidence, _lead_sentences(tool_results, shown, loc_rows, sam_view, window, mix)
    )

    caveat = common.caveat_line(
        f"programs are grouped from USAspending award descriptions for the UEI set resolved from the recipient search ({len(uei_set)} UEIs); "
        f"obligated figures are USAspending award totals to date for awards with activity in {window}, DoD actions post about 90 days late, "
        f"announced values are DoD announcements and ceilings are USAspending base-and-all-options, none of which are added together{page_cite}"
    )
    return common.finalize_draft(
        brief,
        ctx,
        caveat=caveat,
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=ex_prog.present and (ex_mix.present or ex_loc.present),
        claims=[c for c in claims if c and all(ctx.store.has(s) for s in c.source_ids)],
        coverage=coverage,
        notes=notes,
        exhibit_title=None,
    )


def _location_rows(
    tool_results: dict[str, Any], programs: list[dict[str, Any]], ctx: ToolContext
) -> tuple[list[dict[str, Any]], str, bool]:
    """SAM.gov view: active registrations whose physical city+state is an award place of
    performance (from the award records fetched). Reduced view (no key): the places of
    performance themselves, cross-checked against 10-K Item 2."""
    lo, hi = tool_results.get("window") or _window(ctx)
    window = f"FY{lo}-FY{hi}"
    details = tool_results.get("details") or []
    pop: dict[tuple[str, str], dict[str, Any]] = {}
    for d in details:
        if d.get("pop_city") and d.get("pop_state"):
            key = (d["pop_city"], d["pop_state"])
            e = pop.setdefault(key, {"programs": {}})
            e["programs"].setdefault(d.get("program") or "n/a", set()).add(d.get("source_id"))
    state_only: dict[str, dict[str, set[str]]] = {}
    for p in programs:
        for r in p.get("pop_rows") or []:
            st = r.get("state")
            if st and not any(k[1] == st for k in pop) and r.get("source_id"):
                state_only.setdefault(st, {}).setdefault(p["program"], set()).add(r["source_id"])
    sam = tool_results.get("sam") or {}
    page_sids = list(tool_results.get("awards_page_sids") or [])
    if sam.get("available") and not sam.get("error"):
        rows = []
        for e in sam.get("entities") or []:
            status = (e.get("status") or "").upper()
            if status not in ("ACTIVE", "A"):
                continue
            key = (e.get("city") or "", e.get("state") or "")
            if key not in pop:
                continue
            rows.append(
                {
                    "cage": e.get("cage"),
                    "uei": e.get("uei"),
                    "name": e.get("name"),
                    "city": e["city"],
                    "state": e["state"],
                    "expiry": e.get("expiry"),
                    "sam_sid": e.get("source_id"),
                    "programs": [
                        {"program": prog, "sids": sorted(s for s in sids if s)}
                        for prog, sids in pop[key]["programs"].items()
                    ],
                }
            )
        rows = [r for r in rows if r.get("sam_sid") and r.get("cage")]
        caption = (
            f"active SAM.gov registrations under parent UEI {tool_results.get('parent_uei')} with award activity (place of performance) in {window}"
            f"; a registration without an award place of performance in the fetched records is omitted, not absent"
            + "".join(f" [{s}]" for s in page_sids)
        )
        return rows, caption, True
    item2, tenk_sid = ctx.item_text("2")
    rows = []
    for (city, st), e in pop.items():
        rows.append(
            {
                "place": f"{city.title()}, {st}",
                "named_in_10k": bool(item2) and city.lower() in item2.lower(),
                "programs": [
                    {"program": prog, "sids": sorted(s for s in sids if s)}
                    for prog, sids in e["programs"].items()
                ],
            }
        )
    for st, progs in sorted(state_only.items()):
        rows.append(
            {
                "place": f"{st} (city not in the award record)",
                "named_in_10k": False,
                "programs": [
                    {"program": prog, "sids": sorted(sids)} for prog, sids in progs.items()
                ],
            }
        )
    caption = (
        f"place-of-performance view; SAM.gov registrations not available (no API key); places of performance on awards with activity in {window}, "
        f"cross-checked against the 10-K Item 2 Properties text; no CAGE is asserted"
        + "".join(f" [{s}]" for s in page_sids)
    )
    return rows, caption, False


def _lead_sentences(
    tool_results: dict[str, Any],
    shown: list[dict[str, Any]],
    loc_rows: list[dict[str, Any]],
    sam_view: bool,
    window: str,
    mix: dict[int, dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    page_cite = "".join(f" [{s}]" for s in tool_results.get("awards_page_sids") or [])
    uei_set = tool_results.get("uei_set") or []
    if shown:
        tops = "; ".join(
            f"{p['program']} (${fmt_money(p['obligated'])} million obligated across {p['award_count']} award{'s' if p['award_count'] != 1 else ''}, {p['customer'].split(';')[0]})"
            for p in shown[:3]
        )
        out.append(
            f"USAspending prime awards to the {len(uei_set)} resolved UEIs with activity in {window} ({tool_results.get('award_count')} awards on the first page) group into {len(tool_results.get('programs') or [])} named programs, led by {tops}{page_cite}."
        )
    fys = [fy for fy in sorted(mix) if mix[fy].get("rows") and mix[fy].get("source_id")]
    if fys:
        fy = fys[-1]
        rows = mix[fy]["rows"][:3]
        out.append(
            f"In FY{fy} obligations by awarding sub-agency were led by "
            + ", ".join(f"{r['name']} (${fmt_money(r['amount'])} million)" for r in rows)
            + f" [{mix[fy]['source_id']}]."
        )
    for g in (tool_results.get("gao") or [])[:2]:
        tail = ", ".join(x for x in (g.get("report"), g.get("date")) if x)
        out.append(
            f'{g["publisher"]} on {g["program"]}: "{g["text"]}"'
            + (f" ({tail})" if tail else "")
            + "".join(f" [{s}]" for s in g["sids"])
            + "."
        )
    for p in shown:
        a = p.get("announced")
        if a:
            out.append(
                f"The latest DoD contract announcement fetched for {p['program']} is ${fmt_money(a['amount_usd'])} million on {a.get('date') or 'n.d.'}, contract {a.get('contract_number') or 'n/a'}"
                + (
                    f", {a['contracting_activity']} contracting"
                    if a.get("contracting_activity")
                    else ""
                )
                + "".join(f" [{s}]" for s in a["sids"])
                + "."
            )
            break
    if loc_rows:
        if sam_view:
            sam_cite = "".join(f" [{r['sam_sid']}]" for r in loc_rows[:4])
            out.append(
                f"{len(loc_rows)} active SAM.gov registration(s) under the parent UEI have a physical address that is an award place of performance in {window}: "
                + ", ".join(
                    f"{r['city'].title()}, {r['state']} (CAGE {r['cage']})" for r in loc_rows[:4]
                )
                + sam_cite
                + "."
            )
        else:
            out.append(
                f"Without a SAM.gov key the locations view lists {len(loc_rows)} place(s) of performance from the award records{page_cite}."
            )
    un = tool_results.get("unassigned") or {}
    if un.get("count") and un.get("sids"):
        out.append(
            f"{un['count']} award(s) on the page carry descriptions that name no program and stay unassigned rather than inferred"
            + "".join(f" [{s}]" for s in un["sids"])
            + "."
        )
    return out


__all__ = [
    "NO_FINDING",
    "ROLE_PRIME",
    "ROLE_UNCONFIRMED",
    "assign_program",
    "collect",
    "draft",
    "federal_fy",
    "group_programs",
    "learn_aliases",
    "plan",
    "program_keywords",
    "resolve_uei_set",
]
