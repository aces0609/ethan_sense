"""Cross-section reconciliation (review 2026-10-21 §6, §9 item 1).

The numeric gate catches two values for one (metric, period). It does not catch the pattern
that dominated both reviewed primers: one section declaring a fact absent ("no PAC-3
production rate is disclosed in the evidence", "revolver: n/a (not found in the filing
text)") while another section prints it from a fetched source. The reconcile node runs
after every section is drafted and before assembly:

1. Registry. From every section draft: (a) the numeric claims already in state, (b) every
   denial sentence or table cell ("no X appears in the evidence", "X is not in the captured
   evidence", "X cannot be confirmed", "not found in the filing text", "not available in the
   sources") with its subject, and (c) simple (entity, attribute, value, source_ids) facts for
   a fixed attribute vocabulary: revolver, dividend, share repurchase authorization, guidance
   for sales / EPS / FCF / capex / operating profit, production rate, certification, employees,
   backlog, consensus target, share price, market cap, CEO and CFO names.
2. Matching. A denial in section A whose attribute (and program entity, where one applies)
   another section B states with a source becomes a cross-reference: the sentence is replaced
   by "See section B (title): <B's sentence>[src]" (a table cell by "see section B [src]").
   Two sections giving different values for one attribute get a "> Conflict:" line in the
   later section. A model (role `reconciler`) is asked for extra matches through
   invoke_structured when one is configured; the deterministic matcher always runs.
3. Record. Every cross-reference, conflict and unmatched denial goes to run.json
   (`reconciliation`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from pydantic import BaseModel, Field

from primer.config import llm_available
from primer.llm import invoke_structured
from primer.sections.style import CITATION_TOKEN
from primer.state import SECTION_TITLES

# ---- vocabulary -------------------------------------------------------------------------------

EVIDENCE_NOUN = (
    r"(?:(?:captured|fetched|supplied|available|retrieved|reviewed|cited)\s+)?"
    r"(?:evidence|sources(?:\s+reviewed)?|source\s+text|excerpts?|filing\s+text|filings?|documents?)"
)
DENIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\b(?:no|neither)\s+(?P<subj>[^,.;:]{{3,120}}?)\s+(?:appears?|is|are|was|were|has been|have been|could be)\s+"
        rf"(?:disclosed|available|found|given|stated|reported|quantified|identified|located)?\s*(?:in|from|anywhere in)\s+(?:the\s+)?{EVIDENCE_NOUN}",
        re.I,
    ),
    re.compile(
        rf"(?P<subj>[^,.;:]{{3,120}}?)\s+(?:is|are|was|were|has been|have been)\s+not\s+"
        rf"(?:in|available in|found in|disclosed in|stated in|given in|reported in|quantified in|present in)\s+(?:the\s+)?{EVIDENCE_NOUN}",
        re.I,
    ),
    re.compile(
        rf"(?P<subj>[^,.;:]{{3,120}}?)\s+(?:does|do|did)\s+not\s+appear\s+(?:in|anywhere in)\s+(?:the\s+)?{EVIDENCE_NOUN}",
        re.I,
    ),
    re.compile(
        r"(?P<subj>[^,.;:]{3,120}?)\s+cannot\s+be\s+(?:confirmed|verified|established)", re.I
    ),
    re.compile(
        r"(?P<subj>[^,.;:]{3,120}?)\s+(?:is|are|was|were)\s+not\s+found\s+in\s+the\s+filing\s+text",
        re.I,
    ),
    re.compile(r"(?P<subj>[^,.;:]{3,120}?)\s*:\s*not\s+available\s+in\s+the\s+sources", re.I),
    re.compile(
        r"(?:does|do|did)\s+not\s+(?:describe|quantify|show|state|disclose|give|contain|name|report|provide)\s+(?P<subj>[^,.;:]{3,120})",
        re.I,
    ),
)
CELL_DENIAL = re.compile(
    r"\b(?:not found in the filing text|not in the (?:captured |fetched |supplied |available )?(?:evidence|sources(?: reviewed)?)|"
    r"cannot be confirmed|not available in the sources|not disclosed in the (?:evidence|sources))\b",
    re.I,
)

ATTRIBUTE_KEYS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "guidance_eps",
        re.compile(
            r"\b(?:eps|earnings per share)\b[^.]{0,60}\b(?:guid|outlook|target|expect|range|figure)|\b(?:guid|outlook|target)[^.]{0,60}\b(?:eps|earnings per share)\b",
            re.I,
        ),
    ),
    (
        "guidance_fcf",
        re.compile(
            r"\b(?:fcf|free cash flow)\b[^.]{0,60}\b(?:guid|outlook|target|expect|range)|\b(?:guid|outlook|target)[^.]{0,60}\b(?:fcf|free cash flow)\b",
            re.I,
        ),
    ),
    (
        "guidance_capex",
        re.compile(
            r"\b(?:capex|capital expenditures?|capital spending)\b[^.]{0,60}\b(?:guid|outlook|target|expect|range|plan)|\b(?:guid|outlook|target)[^.]{0,60}\b(?:capex|capital expenditures?)\b",
            re.I,
        ),
    ),
    (
        "guidance_operating_profit",
        re.compile(
            r"\b(?:segment )?operating (?:profit|income|margin)\b[^.]{0,60}\b(?:guid|outlook|target|expect|range)|\b(?:guid|outlook|target)[^.]{0,60}\boperating (?:profit|income|margin)\b",
            re.I,
        ),
    ),
    (
        "guidance_sales",
        re.compile(
            r"\b(?:sales|revenues?|net sales)\b[^.]{0,60}\b(?:guid|outlook|target|expect|range)|\b(?:guid|outlook|target)[^.]{0,60}\b(?:sales|revenues?)\b",
            re.I,
        ),
    ),
    (
        "revolver",
        re.compile(
            r"\brevolv|\bcredit (?:facility|facilities|agreement|line)s?\b|\bundrawn\b", re.I
        ),
    ),
    ("dividend", re.compile(r"\bdividend", re.I)),
    ("share_repurchase_authorization", re.compile(r"\brepurchase|\bbuy-?back", re.I)),
    (
        "consensus_target",
        re.compile(
            r"\b(?:price target|target price|consensus target|target estimate|analyst target|mean target|average target)\b",
            re.I,
        ),
    ),
    (
        "share_price",
        re.compile(
            r"\b(?:share price|stock price|closing price|closed at|last price|shares closed|price per share)\b",
            re.I,
        ),
    ),
    ("market_cap", re.compile(r"\bmarket cap", re.I)),
    (
        "production_rate",
        re.compile(
            r"\b(?:production rate|build rate|delivery rate|output rate|unit[- ]rate|rate of \d|"
            r"\d[\d,]* (?:units?|aircraft|missiles?|interceptors?|jets?|airplanes?|airframes?) (?:a|per) (?:month|year)|"
            r"(?:a|per) month\b|annual (?:rate|output|production))",
            re.I,
        ),
    ),
    ("certification", re.compile(r"\bcertif|\btype certificate|\bfaa approv", re.I)),
    ("employees", re.compile(r"\b(?:employees?|headcount|workforce|employee count)\b", re.I)),
    ("backlog", re.compile(r"\bbacklog\b", re.I)),
    ("ceo", re.compile(r"\bchief executive\b|\bCEO\b")),
    ("cfo", re.compile(r"\bchief financial\b|\bCFO\b")),
)
ATTRIBUTE_LABEL = {
    "revolver": "revolving credit facility",
    "dividend": "dividend per share",
    "share_repurchase_authorization": "share repurchase authorization",
    "guidance_sales": "sales guidance",
    "guidance_eps": "EPS guidance",
    "guidance_fcf": "free cash flow guidance",
    "guidance_capex": "capex guidance",
    "guidance_operating_profit": "operating profit guidance",
    "production_rate": "production rate",
    "certification": "certification",
    "employees": "employee count",
    "backlog": "backlog",
    "consensus_target": "consensus price target",
    "share_price": "share price",
    "market_cap": "market capitalization",
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
}
PER_SHARE = {"dividend", "consensus_target", "share_price", "guidance_eps"}
TEXT_VALUED = {"certification", "ceo", "cfo"}
PROGRAM_LEVEL = {"production_rate", "certification"}
# attributes whose prose values are point-in-time and comparable across sections; backlog,
# production rates and buyback figures vary by segment, program, period and flow-vs-
# authorization, so prose values of those only answer denials and never raise conflicts
CONFLICT_ATTRS = {
    "share_price",
    "market_cap",
    "employees",
    "ceo",
    "cfo",
    "revolver",
    "dividend",
}
# Guidance ranges and consensus targets are extracted from prose by proximity to a keyword,
# which in the 2026-10-22 runs paired segment-profit guidance with FCF guidance and a single
# broker's target with the low of a range. They answer denials only; same-metric claims are
# the numeric gate's job.
NOT_A_NAME = re.compile(
    r"\b(?:Security|Group|Air|Airlines|Corp|Corporation|Company|Inc|Holdings|Systems|Martin|Boeing|"
    r"Lockheed|Defense|Aerospace|Capital|Street|Fool|Finance|Bank|Board|Committee|Century|Officer)\b"
)
YEAR = re.compile(r"\b(?:FY)?(20\d{2})\b")

PROGRAM = re.compile(
    r"\b(?:[A-Z]{1,4}-\d{1,3}[A-Z]{0,3}(?:\s?(?:MSE|Block\s?\d+))?|\d{3}(?:-\d{1,2}|\s?MAX(?:\s?\d{1,2})?)|"
    r"THAAD|HIMARS|GMLRS|Javelin|Patriot|Aegis|Trident|Dreamliner|Starliner|Apache|Chinook|Osprey)\b"
)
MONEY = re.compile(
    r"\$\s?(?P<a>\d[\d,]*(?:\.\d+)?)(?:\s?(?:–|-|to)\s?\$?\s?(?P<b>\d[\d,]*(?:\.\d+)?))?\s*(?P<scale>billion|million|thousand|bn|mn|m|b|k)?\b",
    re.I,
)
COUNT = re.compile(
    r"(?<![\d.$])(?P<n>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<scale>thousand|million|k|m)?\b", re.I
)
NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z'\-]+){1,2})\b")
SCALE = {
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}
CLAIM_METRIC_ATTRS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("market_cap", re.compile(r"market_cap", re.I)),
    (
        "share_price",
        re.compile(r"share_price|stock_price|close_price|closing_price|last_price", re.I),
    ),
    (
        "consensus_target",
        re.compile(r"price_target|target_price|consensus_target|analyst_target", re.I),
    ),
    ("employees", re.compile(r"employee|headcount", re.I)),
    ("backlog", re.compile(r"^backlog", re.I)),
    ("dividend", re.compile(r"dividend", re.I)),
    ("revolver", re.compile(r"revolv|credit_facility", re.I)),
    ("share_repurchase_authorization", re.compile(r"repurchase|buyback", re.I)),
    ("guidance_eps", re.compile(r"guid[a-z]*_eps|eps_guid", re.I)),
    (
        "guidance_fcf",
        re.compile(r"guid[a-z]*_(?:fcf|free_cash)|fcf_guid|free_cash_flow_guid", re.I),
    ),
    ("guidance_capex", re.compile(r"guid[a-z]*_capex|capex_guid", re.I)),
    ("guidance_operating_profit", re.compile(r"guid[a-z]*_operating|operating_profit_guid", re.I)),
    ("guidance_sales", re.compile(r"guid[a-z]*_(?:sales|revenue)|(?:sales|revenue)_guid", re.I)),
    ("production_rate", re.compile(r"(?:production|build|delivery)_rate|rate_per_month", re.I)),
)


@dataclass
class Fact:
    id: str
    section: int
    attribute: str
    entity: str
    value: Any  # float, (lo, hi) tuple, or str
    text: str  # the sentence or claim line, with citation tokens
    source_ids: list[str]
    kind: str  # "prose" | "claim"
    period: str = ""
    tier: int = 3


@dataclass
class Denial:
    id: str
    section: int
    text: str  # the exact sentence (prose) or cell text (table)
    subject: str
    attribute: str | None
    entity: str
    where: str  # "prose" | "cell"
    row_label: str = ""


@dataclass
class Registry:
    facts: list[Fact] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)


# ---- extraction ---------------------------------------------------------------------------------


def _strip_cites(s: str) -> str:
    return re.sub(CITATION_TOKEN, "", s).strip()


def _prose_sentences(md: str) -> list[str]:
    """Sentences of the prose lines (not tables, blockquotes or headings), tokens kept."""
    out: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith(("|", ">", "#")):
            continue
        s = re.sub(r"^(?:[-*]\s+|\d+\.\s+)", "", s)
        s = s.replace("**", "")
        for part in re.split(r"(?<=[.!?])(?:\s*\[src_\d{4}\])*\s+(?=[A-Z(\"'“])", s):
            part = part.strip()
            if part:
                out.append(part)
    # the split above drops the tokens that followed a period; recover them from the line
    return _reattach_tokens(md, out)


def _reattach_tokens(md: str, sentences: list[str]) -> list[str]:
    fixed = []
    for s in sentences:
        if re.search(CITATION_TOKEN, s):
            fixed.append(s)
            continue
        i = md.find(s)
        if i >= 0:
            tail = md[i + len(s) : i + len(s) + 80]
            m = re.match(r"((?:\s*\[src_\d{4}\])+)", tail)
            if m:
                fixed.append(s + m.group(1))
                continue
        fixed.append(s)
    return fixed


def classify(text: str) -> str | None:
    for attr, rx in ATTRIBUTE_KEYS:
        if not rx.search(text):
            continue
        if attr in ("ceo", "cfo") and re.search(
            r"compensation|\bpay\b|salary|package|award", text, re.I
        ):
            continue  # the vocabulary carries officer names, not pay
        return attr
    return None


def _keyword_span(attr: str, text: str) -> tuple[int, int] | None:
    rx = dict(ATTRIBUTE_KEYS).get(attr)
    m = rx.search(text) if rx else None
    return (m.start(), m.end()) if m else None


def _nearest(
    text: str, span: tuple[int, int] | None, rx: re.Pattern[str], after: int = 110, before: int = 70
):
    """The first match of rx after the keyword, else the last one just before it."""
    if span is None:
        return rx.search(text)
    m = rx.search(text, span[1], min(len(text), span[1] + after))
    if m:
        return m
    last = None
    for m in rx.finditer(text, max(0, span[0] - before), span[0]):
        last = m
    return last


def _years(text: str) -> set[int]:
    return {int(y) for y in YEAR.findall(_strip_cites(text))}


def program_entity(text: str) -> str:
    m = PROGRAM.search(_strip_cites(text))
    return _norm_entity(m.group(0)) if m else "company"


def _norm_entity(s: str) -> str:
    return re.sub(r"[\s\-]+", "", s.upper())


PER_SHARE_MONEY = re.compile(
    r"\$\s?(?P<a>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d,.]?\d)"
    r"(?:\s?(?:–|-|to)\s?\$?\s?(?P<b>\d[\d,]*(?:\.\d+)?)(?![\d,.]?\d))?"
    r"(?!\s*(?:billion|million|bn|mn|m\b|b\b|k\b|thousand)|[–\-])(?P<scale>)",
    re.I,
)
DELTA_BEFORE = re.compile(
    r"\b(?:by|up|down|increase of|decrease of|raised by|cut by|lowered by|reduced by|rose|fell)\s*$",
    re.I,
)
FORMULA_BEFORE = re.compile(
    r"(?:derived as|defined as|computed as|equals?|plus|minus|less|\+|−|-|=|×|/)\s*$", re.I
)
LOWER_BOUND = re.compile(
    r"\b(?:over|above|at least|more than|exceed(?:s|ing)?|in excess of|north of)\s*$", re.I
)
UPPER_BOUND = re.compile(r"\b(?:below|under|less than|up to|no more than|at most)\s*$", re.I)


def _money_value(clean: str, attr: str) -> float | tuple[float, float] | None:
    """The money figure nearest the attribute keyword (after it first, then just before);
    a change ("cut by $500 million") is skipped, and "over $7 billion" is a lower bound."""
    per_share = attr in PER_SHARE
    span = _keyword_span(attr, clean)
    rx = PER_SHARE_MONEY if per_share else MONEY
    m = _nearest(clean, span, rx)
    tries = 0
    while m and DELTA_BEFORE.search(clean[max(0, m.start() - 16) : m.start()]) and tries < 3:
        m = rx.search(clean, m.end())
        tries += 1
    if not m:
        return None
    a = float(m.group("a").replace(",", ""))
    scale = SCALE.get((m.group("scale") or "").lower(), 1.0)
    if per_share and a > 100000:
        return None
    before = clean[max(0, m.start() - 16) : m.start()]
    if m.group("b"):
        b = float(m.group("b").replace(",", ""))
        return (a * scale, b * scale)
    if LOWER_BOUND.search(before):
        return (a * scale, float("inf"))
    if UPPER_BOUND.search(before):
        return (0.0, a * scale)
    return a * scale


def _count_value(clean: str, attr: str, after: re.Pattern[str]) -> float | None:
    """A counted quantity whose following words match `after`, nearest the keyword."""
    span = _keyword_span(attr, clean)
    cands = []
    for m in COUNT.finditer(clean):
        n = float(m.group("n").replace(",", ""))
        n *= SCALE.get((m.group("scale") or "").lower(), 1.0)
        follow = clean[m.end() : m.end() + 40].lower()
        before = clean[max(0, m.start() - 12) : m.start()].lower()
        if re.fullmatch(r"(19|20)\d{2}", m.group("n")) or re.search(
            r"q[1-4]|fy|section|item|\$", before
        ):
            continue
        if after.search(follow):
            dist = abs(m.start() - span[0]) if span else 0
            cands.append((dist, n))
    return min(cands)[1] if cands else None


def _name_value(clean: str, attr: str) -> str | None:
    """An officer name adjacent to the title ('Jim Taiclet, CEO', 'CEO Kelly Ortberg',
    'Scott became chief financial officer'); organisation-like tokens are rejected."""
    title = (
        r"(?:chief executive officer|CEO)" if attr == "ceo" else r"(?:chief financial officer|CFO)"
    )
    name = r"[A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z'\-]+){1,2}"
    pats = (
        rf"(?<![A-Za-z])({name})(?:'s)?,?\s+(?:the\s+|its\s+|as\s+)?(?:president and\s+)?{title}\b",
        rf"\b{title}(?:,|\s+and\s+(?:president|chairman))?\s*({name})",
        rf"(?<![A-Za-z])({name})(?:'s)?\s+(?:was|is|became|will become|has been)\s+(?:named|appointed|elected)?\s*(?:as\s+)?(?:the\s+)?(?:new\s+)?(?:president and\s+)?{title}\b",
    )
    for p in pats:
        m = re.search(p, clean)
        if m and not NOT_A_NAME.search(m.group(1)):
            # "United Airlines CEO Scott Kirby" is a customer's officer, not the company's
            before = clean[max(0, m.start() - 70) : m.start()]
            if THIRD_PARTY_ORG.search(before) or THIRD_PARTY_ORG.search(m.group(0)):
                continue
            return re.sub(r"'s$", "", m.group(1))
    return None


THIRD_PARTY_ORG = re.compile(
    r"\b(Airlines?|Airways|Air Group|Aviation|Agency|Command|Department|Ministry|Bank|Securities|"
    r"Research|Capital|Partners|Ratings|Moody's|Fitch|S&P|customer|supplier|peer|rival|competitor|"
    r"partner|union|IAM|SPEEA)\b"
)


def fact_value(attr: str, text: str) -> Any:
    clean = _strip_cites(text)
    if attr in ("ceo", "cfo"):
        return _name_value(clean, attr)
    if attr == "certification":
        m = re.search(
            r"\b(?:approv\w*|certif\w*|received|granted|amended type cert\w*)\b[^.]{0,80}",
            clean,
            re.I,
        )
        return m.group(0).strip()[:120] if m else None
    if attr == "employees":
        return _count_value(
            clean, attr, re.compile(r"^\s*(?:employees|people|workers|staff|full-time)|employ")
        )
    if attr == "production_rate":
        return _count_value(
            clean,
            attr,
            re.compile(
                r"^\s*(?:units?|missiles?|interceptors?|aircraft|jets?|airplanes?|airframes?|per|a month|a year|(?:a|per) (?:month|year))"
            ),
        )
    return _money_value(clean, attr)


def _is_denial_sentence(s: str) -> bool:
    return any(rx.search(_strip_cites(s)) for rx in DENIAL_PATTERNS)


def extract_denials(section: int, md: str) -> list[Denial]:
    out: list[Denial] = []
    n = 0
    for s in _prose_sentences(md):
        clean = _strip_cites(s)
        subj = None
        for rx in DENIAL_PATTERNS:
            m = rx.search(clean)
            if m:
                subj = m.group("subj").strip()
                break
        if subj is None:
            continue
        subj_l = re.sub(r"^(?:the|a|an|any|its|their)\s+", "", subj, flags=re.I)
        attr = classify(subj_l) or classify(clean)
        entity = program_entity(clean) if attr in PROGRAM_LEVEL else "company"
        n += 1
        out.append(Denial(f"d{section}_{n}", section, s, subj_l[:120], attr, entity, "prose"))
    for line in md.splitlines():
        if not line.startswith("|") or re.fullmatch(r"\|[\s:\-|]+\|", line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = _strip_cites(cells[0])
        for cell in cells[1:]:
            if not CELL_DENIAL.search(cell):
                continue
            attr = classify(label) or classify(cell)
            entity = program_entity(label) if attr in PROGRAM_LEVEL else "company"
            n += 1
            out.append(
                Denial(f"d{section}_{n}", section, cell, label[:120], attr, entity, "cell", label)
            )
    return out


def extract_prose_facts(section: int, md: str, tier_of) -> list[Fact]:
    out: list[Fact] = []
    n = 0
    for s in _prose_sentences(md):
        sids = re.findall(CITATION_TOKEN, s)
        if not sids or _is_denial_sentence(s):
            continue
        clean = _strip_cites(s)
        if re.search(r"\bno\b[^.]{0,40}\b(?:disclosed|available|reported|given)\b", clean, re.I):
            continue
        attr = classify(clean)
        if not attr:
            continue
        if attr == "share_repurchase_authorization" and not re.search(
            r"authoriz|remaining|program", clean, re.I
        ):
            continue  # a buyback flow is not the authorization
        span = _keyword_span(attr, clean)
        if span and FORMULA_BEFORE.search(clean[max(0, span[0] - 24) : span[0]]):
            continue  # "derived as market cap + total debt": the keyword is an operand
        value = fact_value(attr, s)
        if value is None:
            continue
        entity = program_entity(clean) if attr in PROGRAM_LEVEL else "company"
        n += 1
        out.append(
            Fact(
                f"f{section}_{n}",
                section,
                attr,
                entity,
                value,
                s,
                sids,
                "prose",
                tier=min(tier_of(sid) for sid in sids),
            )
        )
    return out


def extract_table_facts(section: int, md: str, tier_of) -> list[Fact]:
    """Facts from exhibit rows whose label names an attribute ("Diluted EPS | $29.95–30.65
    [src]"); they answer denials but never raise conflicts (tables mix periods)."""
    out: list[Fact] = []
    headers: list[str] = []
    n = 0
    for line in md.splitlines():
        if not line.startswith("|"):
            headers = []
            continue
        if re.fullmatch(r"\|[\s:\-|]+\|", line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not headers:
            headers = [_strip_cites(c) for c in cells]
            continue
        label = _strip_cites(cells[0])
        attr = classify(label)
        if not attr or len(cells) < 2:
            continue
        for k, cell in enumerate(cells[1:], start=1):
            sids = re.findall(CITATION_TOKEN, cell)
            if not sids or CELL_DENIAL.search(cell):
                continue
            clean = _strip_cites(cell)
            if attr in ("ceo", "cfo", "certification"):
                value: Any = clean[:120] if clean and clean.lower() != "n/a" else None
            elif attr in ("employees", "production_rate"):
                value = _count_value(f"{label} {clean}", attr, re.compile(r".*"))
            else:
                value = _money_value(f"{label} {clean}", attr)
            if value is None:
                continue
            head = headers[k] if k < len(headers) else ""
            n += 1
            out.append(
                Fact(
                    f"t{section}_{n}",
                    section,
                    attr,
                    program_entity(label) if attr in PROGRAM_LEVEL else "company",
                    value,
                    f"{label}{' (' + head + ')' if head else ''}: {cell}",
                    sids,
                    "table",
                    period=head,
                    tier=min(tier_of(s) for s in sids),
                )
            )
            break  # one value per row: the first cited cell
    return out


def claim_facts(claims: list[dict[str, Any]], tier_of) -> list[Fact]:
    out: list[Fact] = []
    for i, c in enumerate(claims):
        metric = str(c.get("metric", ""))
        attr = next((a for a, rx in CLAIM_METRIC_ATTRS if rx.search(metric)), None)
        if not attr or c.get("value") is None or not c.get("source_ids"):
            continue
        sids = list(c["source_ids"])
        val = float(c["value"])
        text = (
            f"{metric.replace('_', ' ')} {_fmt_value(attr, val)} for {c.get('period', 'n.d.')}"
            + "".join(f" [{s}]" for s in sids)
        )
        out.append(
            Fact(
                f"c{i}",
                int(c["section"]),
                attr,
                "company",
                val,
                text,
                sids,
                "claim",
                period=str(c.get("period", "")),
                tier=min(tier_of(s) for s in sids),
            )
        )
    return out


def build_registry(
    drafts: dict[int, dict[str, Any]], claims: list[dict[str, Any]], tier_of
) -> Registry:
    reg = Registry()
    for sec, d in sorted(drafts.items()):
        md = d.get("markdown") or ""
        if d.get("coverage") in ("unavailable", "not_applicable"):
            continue
        reg.denials.extend(extract_denials(int(sec), md))
        reg.facts.extend(extract_prose_facts(int(sec), md, tier_of))
        reg.facts.extend(extract_table_facts(int(sec), md, tier_of))
    cl = claim_facts(claims, tier_of)
    # a section's claim is its authoritative value: its own prose and table readings of the
    # same attribute (peer figures, formula operands) step aside
    claimed = {(f.section, f.attribute) for f in cl}
    reg.facts = [f for f in reg.facts if (f.section, f.attribute) not in claimed] + cl
    return reg


# ---- matching ------------------------------------------------------------------------------------


def _compatible_entity(attr: str, fact_entity: str, denial_entity: str) -> bool:
    """A program-level denial ("no PAC-3 production rate…") is answered only by a fact about
    that program; a company-level one by any fact of the attribute."""
    if attr in PROGRAM_LEVEL and denial_entity != "company":
        return fact_entity == denial_entity
    return True


def _fmt_value(attr: str, v: Any) -> str:
    if isinstance(v, tuple):
        if v[1] == float("inf"):
            return f"over {_fmt_value(attr, v[0])}"
        if v[0] == 0.0 and v[1] > 0:
            return f"under {_fmt_value(attr, v[1])}"
        return f"{_fmt_value(attr, v[0])}–{_fmt_value(attr, v[1])}"
    if isinstance(v, str):
        return v
    v = float(v)
    if attr in PER_SHARE:
        return f"${v:,.2f}"
    if attr in ("employees", "production_rate"):
        return f"{v:,.0f}"
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.2f} billion".replace(".00 billion", " billion")
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.0f} million"
    return f"${v:,.2f}"


def _values_conflict(attr: str, a: Any, b: Any, tol: float = 0.005) -> bool:
    if attr in ("ceo", "cfo"):
        return bool(a and b) and str(a).split()[-1].lower() != str(b).split()[-1].lower()
    if attr in TEXT_VALUED or a is None or b is None:
        return False

    def rng(v: Any) -> tuple[float, float]:
        if isinstance(v, tuple):
            return (min(v), max(v))
        return (float(v), float(v))

    lo1, hi1 = rng(a)
    lo2, hi2 = rng(b)
    finite = [abs(x) for x in (lo1, hi1, lo2, hi2) if x != float("inf")]
    slack = tol * max([*finite, 1e-9])
    if not (hi1 + slack < lo2 or hi2 + slack < lo1):
        return False
    # A real disagreement is two readings of the same quantity (42 vs 47 per month, 1.2x vs
    # 2.0x). Values an order of magnitude apart are a mis-extraction (a dividend total against
    # a per-share rate, backlog against FCF guidance), not a conflict to print.
    mid1, mid2 = (lo1 + hi1) / 2, (lo2 + hi2) / 2
    if mid1 and mid2 and not (0.4 <= abs(mid1 / mid2) <= 2.5):
        return False
    return True


def deterministic_matches(reg: Registry) -> list[tuple[Denial, Fact]]:
    """For every classified denial, the best fact from another section with the same
    attribute (tier 1 first, then the lower section number)."""
    pairs: list[tuple[Denial, Fact]] = []
    for d in reg.denials:
        if not d.attribute:
            continue
        cands = [
            f
            for f in reg.facts
            if f.attribute == d.attribute
            and f.section != d.section
            and _compatible_entity(d.attribute, f.entity, d.entity)
        ]
        if not cands:
            continue
        cands.sort(key=partial(_match_rank, want=_years(d.text)))
        pairs.append((d, cands[0]))
    return pairs


def _match_rank(f: Fact, want: set[int]) -> tuple:
    """A fact about the denial's own year first (when it names one), then tier-1, then a
    prose sentence over a table row or bare claim, then the earlier section."""
    year_rank = 0 if (want and _years(f.text) & want) else 1
    return (year_rank, f.tier, 0 if f.kind == "prose" else 1, f.section)


def deterministic_conflicts(reg: Registry) -> list[tuple[Fact, Fact]]:
    """Pairs of facts from different sections, same attribute and entity, different values.
    Pairs the numeric gate already covers (two claims on one metric and period) are skipped."""
    out: list[tuple[Fact, Fact]] = []
    seen: set[tuple[str, int, int]] = set()
    by_key: dict[tuple[str, str], list[Fact]] = {}
    for f in reg.facts:
        if f.attribute not in CONFLICT_ATTRS or f.kind == "table":
            continue
        by_key.setdefault((f.attribute, f.entity), []).append(f)
    for (attr, _), facts in by_key.items():
        facts = sorted(facts, key=lambda f: f.section)
        for i, a in enumerate(facts):
            for b in facts[i + 1 :]:
                if a.section == b.section:
                    continue
                if a.kind == "claim" and b.kind == "claim":
                    # same period: the numeric gate covers it; different periods: not a conflict
                    continue
                ya, yb = _years(a.text), _years(b.text)
                if ya and yb and not (ya & yb):
                    continue  # FY2025 guidance against FY2026 guidance is not a conflict
                if not _values_conflict(attr, a.value, b.value):
                    continue
                key = (attr, min(a.section, b.section), max(a.section, b.section))
                if key in seen:
                    continue
                seen.add(key)
                out.append((a, b))
    return out


class _Match(BaseModel):
    denial_id: str
    fact_id: str


class _Conflict(BaseModel):
    fact_id_a: str
    fact_id_b: str


class _Reconciliation(BaseModel):
    matches: list[_Match] = Field(default_factory=list)
    conflicts: list[_Conflict] = Field(default_factory=list)


def llm_matches(reg: Registry, runlog) -> tuple[list[tuple[Denial, Fact]], list[tuple[Fact, Fact]]]:
    """Extra matches from the reconciler model; ids are validated, nothing else is trusted."""
    if not reg.denials or not reg.facts or not llm_available():
        return [], []
    system = (
        "You reconcile the sections of a company primer. You receive DENIALS (a section says a fact is "
        "absent from the evidence) and FACTS (a section states that kind of fact with a source). Return "
        "matches where a fact from another section answers a denial about the same attribute and the same "
        "program or company, and conflicts where two facts from different sections give different values "
        "for the same attribute and entity. Use only the ids given. Return nothing when unsure."
    )
    import json

    user = json.dumps(
        {
            "denials": [
                {
                    "id": d.id,
                    "section": d.section,
                    "attribute": d.attribute,
                    "entity": d.entity,
                    "text": _strip_cites(d.text)[:300],
                }
                for d in reg.denials
            ][:80],
            "facts": [
                {
                    "id": f.id,
                    "section": f.section,
                    "attribute": f.attribute,
                    "entity": f.entity,
                    "value": _fmt_value(f.attribute, f.value),
                    "text": _strip_cites(f.text)[:300],
                }
                for f in reg.facts
            ][:200],
        }
    )
    out = invoke_structured("reconciler", _Reconciliation, system, user, runlog)
    if not out:
        return [], []
    dens = {d.id: d for d in reg.denials}
    facts = {f.id: f for f in reg.facts}
    matches = [
        (dens[m.denial_id], facts[m.fact_id])
        for m in out.matches
        if m.denial_id in dens
        and m.fact_id in facts
        and facts[m.fact_id].section != dens[m.denial_id].section
    ]
    conflicts = [
        (facts[c.fact_id_a], facts[c.fact_id_b])
        for c in out.conflicts
        if c.fact_id_a in facts
        and c.fact_id_b in facts
        and facts[c.fact_id_a].section != facts[c.fact_id_b].section
        and facts[c.fact_id_a].attribute == facts[c.fact_id_b].attribute
    ]
    return matches, conflicts


# ---- application --------------------------------------------------------------------------------


def _xref_text(fact: Fact) -> str:
    title = SECTION_TITLES.get(fact.section, "")
    body = fact.text.strip()
    if fact.kind == "claim":
        body = (
            f"{ATTRIBUTE_LABEL.get(fact.attribute, fact.attribute)} {_fmt_value(fact.attribute, fact.value)}"
            + (f" for {fact.period}" if fact.period else "")
            + "".join(f" [{s}]" for s in fact.source_ids)
        )
    return (
        f"See section {fact.section} ({title}): {body}"
        if title
        else f"See section {fact.section}: {body}"
    )


def _insert_after_caveat(md: str, line: str) -> str:
    if line in md:
        return md
    paras = md.split("\n\n", 1)
    if len(paras) == 2 and paras[0].startswith("*Caveat"):
        return paras[0] + "\n\n" + line + "\n\n" + paras[1]
    return line + "\n\n" + md


def apply(
    drafts: dict[int, dict[str, Any]],
    matches: list[tuple[Denial, Fact]],
    conflicts: list[tuple[Fact, Fact]],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Rewrite the drafts; returns (changed drafts only, record)."""
    md_by_sec = {int(s): d["markdown"] for s, d in drafts.items()}
    changed: set[int] = set()
    record: dict[str, Any] = {"cross_references": [], "conflicts": []}
    done: set[str] = set()
    for d, f in matches:
        if d.id in done:
            continue
        md = md_by_sec.get(d.section, "")
        if d.text not in md:
            continue
        if d.where == "cell":
            repl = f"see section {f.section}" + "".join(f" [{s}]" for s in f.source_ids)
        else:
            repl = _xref_text(f)
        md_by_sec[d.section] = md.replace(d.text, repl, 1)
        changed.add(d.section)
        done.add(d.id)
        record["cross_references"].append(
            {
                "section": d.section,
                "attribute": d.attribute,
                "entity": d.entity,
                "denial": _strip_cites(d.text)[:200],
                "from_section": f.section,
                "fact": _strip_cites(f.text)[:200],
                "source_ids": f.source_ids,
            }
        )
    for a, b in conflicts:
        later = max(a.section, b.section)
        md = md_by_sec.get(later, "")
        label = ATTRIBUTE_LABEL.get(a.attribute, a.attribute)
        va, vb = _fmt_value(a.attribute, a.value), _fmt_value(b.attribute, b.value)
        existing = [ln for ln in md.splitlines() if ln.startswith("> Conflict:")]
        if any(va in ln and vb in ln for ln in existing):
            continue
        ent = "" if a.entity == "company" else f" for {a.entity}"
        line = (
            f"> Conflict: {label}{ent} reported as {va}{''.join(f' [{s}]' for s in a.source_ids)} (section {a.section}) "
            f"and {vb}{''.join(f' [{s}]' for s in b.source_ids)} (section {b.section}); sources differ and are not reconciled."
        )
        md_by_sec[later] = _insert_after_caveat(md, line)
        changed.add(later)
        record["conflicts"].append(
            {
                "attribute": a.attribute,
                "entity": a.entity,
                "sections": [a.section, b.section],
                "values": [va, vb],
                "source_ids": [a.source_ids, b.source_ids],
                "placed_in": later,
            }
        )
    out = {}
    for sec in changed:
        key = sec if sec in drafts else str(sec)
        out[sec] = {**drafts[key], "markdown": md_by_sec[sec]}
    return out, record


def _fact_date(f: Fact, store: Any):
    """The latest published date among a fact's sources (None when none is dated)."""
    dates = []
    for sid in f.source_ids:
        try:
            rec = store.get(sid) if store is not None and store.has(sid) else None
        except Exception:  # noqa: BLE001
            rec = None
        if rec is not None and rec.published_date:
            dates.append(rec.published_date)
    return max(dates) if dates else None


def _historical_officer_pair(a: Fact, b: Fact, store: Any) -> bool:
    """A CEO/CFO named in a source more than a year older than the other source is a past
    officer (a 2019 merger release against a 2026 appointment), not a conflict to print."""
    if a.attribute not in ("ceo", "cfo"):
        return False
    da, db = _fact_date(a, store), _fact_date(b, store)
    return bool(da and db and abs((da - db).days) > 365)


def reconcile_drafts(
    drafts: dict[int, dict[str, Any]],
    claims: list[dict[str, Any]],
    store: Any,
    runlog: Any = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """The node body: registry → deterministic matching (+ model matches when configured)
    → rewritten drafts and the run.json record."""

    def tier_of(sid: str) -> int:
        try:
            return int(store.get(sid).tier) if store is not None and store.has(sid) else 3
        except Exception:  # noqa: BLE001
            return 3

    drafts = {int(k): v for k, v in drafts.items()}
    reg = build_registry(drafts, claims, tier_of)
    matches = deterministic_matches(reg)
    conflicts = deterministic_conflicts(reg)
    mode = "deterministic"
    try:
        extra_m, extra_c = llm_matches(reg, runlog)
    except Exception as e:  # noqa: BLE001 - the model path never fails the run
        if runlog is not None:
            runlog.error(f"reconcile LLM: {e!r}")
        extra_m, extra_c = [], []
    if extra_m or extra_c:
        mode = "llm+deterministic"
        have = {d.id for d, _ in matches}
        matches += [(d, f) for d, f in extra_m if d.id not in have]
        have_c = {
            (min(a.section, b.section), max(a.section, b.section), a.attribute)
            for a, b in conflicts
        }
        conflicts += [
            (a, b)
            for a, b in extra_c
            if (min(a.section, b.section), max(a.section, b.section), a.attribute) not in have_c
        ]
    conflicts = [(a, b) for a, b in conflicts if not _historical_officer_pair(a, b, store)]
    changed, record = apply(drafts, matches, conflicts)
    matched_ids = {d.id for d, _ in matches}
    record.update(
        {
            "mode": mode,
            "denials": len(reg.denials),
            "facts": len(reg.facts),
            "unmatched_denials": [
                {"section": d.section, "attribute": d.attribute, "text": _strip_cites(d.text)[:160]}
                for d in reg.denials
                if d.id not in matched_ids
            ][:60],
        }
    )
    if runlog is not None:
        runlog.reconciliation = record
    return changed, record
