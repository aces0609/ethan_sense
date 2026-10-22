"""Private-company path (PLAN 3.4; requirements §1 "private and foreign companies", §4
"Private companies", §6 rows 9, 11 and 13).

A company with no SEC financial statements has no XBRL, no 10-K and no market quote. This
module is the one place that knows how to say so honestly:

- **Form D discovery** (`discover_form_d`, used by the foundation pack): EDGAR full-text search
  for private-placement filings by the company name; when EDGAR refuses (403 from some
  networks) a Firecrawl search of `"{company}" "Form D" site:sec.gov` whose sec.gov hits are
  registered as tier-1 sources. A failure is counted and becomes a coverage note, never an error.
- **Press-reported datapoints** (`Datapoint`, `extract_datapoints`): dated, cited, deterministic
  regex extraction over fetched pages (revenue, run rate, net income, EBITDA, funding rounds,
  valuation, debt financing, tender offers, backlog, headcount), plus the drafter when a model is
  configured. A model-proposed datapoint is kept only when its quote is a verbatim passage of a
  registered page and carries the stated number.
- **The section helpers** (`plan_items`, `collect_press`, `draft_press_section`) that sections 9,
  10, 11 and 13 call on the private path: a table "Press-reported figures (unaudited)" with every
  row cited and labelled with its source tier, claims whose metrics start with `press_` (so the
  tier rule and the numeric gate do not treat them as audited statement metrics), coverage
  `partial` when at least one datapoint is cited and `unavailable` otherwise, and a one-line
  blockquote that says what was found and what was not.

Nothing here invents, interpolates or derives a number: no multiple is computed (there is no cited
price and share count), no price target and no consensus is emitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from primer.clients.edgar import EdgarClient, pad_cik
from primer.clients.firecrawl import FirecrawlClient, FirecrawlError
from primer.config import foundation_budget, llm_available, sources_config, trade_press
from primer.evidence.store import EvidenceStore
from primer.llm import invoke_structured
from primer.runlog import RunLog
from primer.sections import common
from primer.sections.common import ToolContext
from primer.sections.text_tools import money_in, norm_company, sentences_safe
from primer.state import Brief, Claim, Entity, SectionDraft

PRESS_PREFIX = "press_"
FORM_D_PREFIX = "form_d_"
TABLE_TITLE = "Press-reported figures (unaudited)"
SOURCE_LIMIT_DEFAULT = (
    "Private company: no SEC financial statements; financial figures are press-reported "
    "and unaudited"
)
MAX_FORM_D = 5
MAX_ROWS = 14
SENTENCE_MAX = 600

# section -> datapoint kinds the section reports on the private path
SECTION_KINDS: dict[int, tuple[str, ...]] = {
    9: ("revenue", "run_rate", "net_income", "ebitda"),
    10: ("revenue", "run_rate", "backlog", "headcount"),
    11: ("funding", "debt", "tender", "valuation", "form_d"),
    13: ("valuation", "funding", "tender", "form_d"),
}
DEFAULT_QUERIES: dict[int, list[str]] = {
    9: ["{co} annual revenue", "{co} revenue profit growth"],
    10: ["{co} backlog contracts customers", "{co} revenue growth employees"],
    11: [
        "{co} funding round valuation",
        "{co} debt financing credit facility",
        "{co} tender offer valuation",
    ],
    13: ["{co} funding round valuation", "{co} tender offer valuation"],
}
EXHIBIT_TITLES = {
    9: TABLE_TITLE,
    10: "Press-reported operating figures (unaudited)",
    11: TABLE_TITLE,
    13: "Funding rounds and valuation (press-reported, unaudited)",
}


# ---- entity and config helpers -----------------------------------------------------------


def is_private(entity: Entity | dict[str, Any] | None) -> bool:
    """True on the private path (`Entity.private`), false for every SEC filer."""
    if entity is None:
        return False
    if isinstance(entity, dict):
        return bool(entity.get("private"))
    return bool(entity.private)


def private_cfg() -> dict[str, Any]:
    return dict(sources_config().get("private_path") or {})


def source_limit() -> str:
    return str(sources_config().get("private_source_limit") or SOURCE_LIMIT_DEFAULT)


def private_queries(section: int, ctx: ToolContext) -> list[str]:
    """The section's private-path web queries from config/sources.yaml (`private_path.queries`),
    with {co} and {fy} filled in."""
    raw = (private_cfg().get("queries") or {}).get(section) or DEFAULT_QUERIES.get(section, [])
    out = []
    for q in raw:
        try:
            s = str(q).format(co=ctx.co, fy=ctx.fy, tk="")
        except (KeyError, IndexError):
            continue
        if s.strip():
            out.append(s.strip())
    return list(dict.fromkeys(out))


def company_names(entity: Entity) -> list[str]:
    """Lower-cased name variants a sentence must contain to be about the company."""
    raw = [entity.name, *entity.aliases]
    out: list[str] = []
    for n in raw:
        for v in (n, norm_company(n)):
            v = " ".join((v or "").lower().split())
            if len(v) >= 4 and v not in out:
                out.append(v)
    return out


# ---- datapoints --------------------------------------------------------------------------


@dataclass
class Datapoint:
    kind: str  # revenue, run_rate, net_income, ebitda, funding, valuation, debt, tender, backlog, headcount, form_d_*
    metric: str  # claim metric: press_* (press) or form_d_* (SEC private-placement notice)
    label: str  # table label
    value: float
    unit: str  # USD or count
    period: str  # FY2025, Q2-2025, 2025-03-12 or 2025-03
    basis: str  # reported | estimated | issuer-filed
    source_id: str
    quote: str = ""
    method: str = "regex"  # regex | model | form_d
    extra_sources: list[str] = field(default_factory=list)

    @property
    def source_ids(self) -> list[str]:
        return [self.source_id, *self.extra_sources]


MONEY_RX = re.compile(
    r"(?<![\w$])(?:US|USD)?\s?\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
    r"(?:\s?(billion|bn|million|mm|mn|thousand|[bmk]))?(?![\w])",
    re.I,
)
SCALE = {
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mm": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}
MONTHS = "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
MONTH_NUM = {
    m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)
}
DATE_FULL = re.compile(rf"\b({MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I)
DATE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})\.?,?\s+(20\d\d)\b", re.I)
DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
DATE_MY = re.compile(rf"\b({MONTHS})\.?,?\s+(20\d\d)\b", re.I)

TRIGGERS: dict[str, re.Pattern[str]] = {
    k: re.compile(v, re.I)
    for k, v in {
        "tender": r"tender offers?|secondary (?:sale|share sale|offering|transaction)|employee (?:share|stock) sale",
        "valuation": r"valu(?:ed|ation|es|ing)|post-money|pre-money",
        "run_rate": r"run[- ]?rate|annuali[sz]ed|annual recurring revenue|\bARR\b",
        "net_income": r"net (?:income|profit|loss(?:es)?)|loss(?:es)? of|profit(?:able)?\b",
        "ebitda": r"EBITDA",
        "revenue": r"revenues?|sales|turnover",
        "funding": r"raised|raises|raising|secured|funding|financing round|Series [A-H]\b|seed round|equity (?:investment|round|financing)|investment of|invested",
        "debt": r"credit (?:facility|line|agreement)|term loan|revolv\w*|(?:senior|convertible|secured|unsecured) notes?|debt financing|loans?\b|bonds?\b|notes offering|borrowing",
        "backlog": r"backlog|bookings|contracted revenue|order book",
    }.items()
}
KIND_ORDER = (
    "tender",
    "valuation",
    "run_rate",
    "net_income",
    "ebitda",
    "revenue",
    "funding",
    "debt",
    "backlog",
)
DEBT_AFTER = re.compile(
    r"^\s*(?:of |in |as )?(?:\w+[ -]){0,2}(?:debt|loans?|notes?|credit|bonds?|borrowing|facility)",
    re.I,
)
LOSS_WORDS = re.compile(r"net loss|loss(?:es)? of|burn", re.I)
FORWARD = re.compile(
    r"\b(?:expects?|expected|expecting|projects?|projected|projecting|forecasts?|forecasted|"
    r"plans? to|planning to|planned|aims?|aiming|targets?|targeting|could|would|might|will|"
    r"guidance|anticipates?|anticipated|hopes?|looking to|seeks?|seeking|in talks|in discussions|"
    r"is raising|considering|potentially|on track|poised|slated|set to|intends?|proposed|"
    r"hopes)\b|(?-i:\bmay\b)",
    re.I,
)
ESTIMATE_WORDS = re.compile(
    r"\b(?:estimated|estimates?|reportedly|according to (?:sources|people|a person|people familiar)|"
    r"sources (?:said|say|familiar)|people familiar|unconfirmed|undisclosed sources)\b",
    re.I,
)
PRONOUN_START = re.compile(r"^\W*(?:the (?:company|startup|firm|business|group)|it|its)\b", re.I)
HEADCOUNT_RX = re.compile(
    r"(?<![\w$.,])(\d{1,3}(?:,\d{3})+|\d{2,6})\s+(?:full-time\s+|permanent\s+)?"
    r"(?:employees|staff|workers|team members|people)\b",
    re.I,
)
SERIES_RX = re.compile(r"\b(Series [A-H]\d?|seed|pre-seed)\b", re.I)
DEBT_TYPE_RX = re.compile(
    r"revolving credit facility|credit facility|credit line|term loan|convertible notes?|"
    r"senior notes?|secured notes?|unsecured notes?|debt financing|loan|bonds?",
    re.I,
)
PERIOD_OK = re.compile(r"^(?:FY20\d\d|Q[1-4]-20\d\d|20\d\d(?:-\d\d){0,2})$")
QUARTER_WORD = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def _clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#]+", " ", text)
    return text


def money_tokens(sentence: str) -> list[tuple[float, int, int]]:
    """Dollar amounts in a sentence as (usd, start, end). An amount needs a scale word or
    letter (or at least $1,000,000 written out); a range ("$2 billion to $3 billion") is
    skipped entirely because neither end is the figure."""
    raw: list[tuple[float, int, int]] = []
    for m in MONEY_RX.finditer(sentence):
        num = float((m.group(1) + (m.group(2) or "")).replace(",", ""))
        suffix = (m.group(3) or "").lower()
        val = num * SCALE.get(suffix, 1.0)
        if not suffix and val < 1_000_000:
            continue
        raw.append((val, m.start(), m.end()))
    out = []
    for i, tok in enumerate(raw):
        before = sentence[raw[i - 1][2] : tok[1]] if i else ""
        after = sentence[tok[2] : raw[i + 1][1]] if i + 1 < len(raw) else ""
        if re.fullmatch(r"\s*(?:to|-|–|—|and|or)\s*", before) or re.fullmatch(
            r"\s*(?:to|-|–|—|and|or)\s*", after
        ):
            continue
        out.append(tok)
    return out


def classify_money(sentence: str, start: int, end: int) -> str | None:
    """The kind of figure a dollar amount is, by the nearest trigger phrase (a trigger before
    the amount beats one after it by six characters)."""
    best: tuple[int, int, str] | None = None
    for rank, kind in enumerate(KIND_ORDER):
        for t in TRIGGERS[kind].finditer(sentence):
            if t.end() <= start:
                d = start - t.end()
            elif t.start() >= end:
                d = (t.start() - end) + 6
            else:
                continue
            if d <= 60 and (best is None or (d, rank) < (best[0], best[1])):
                best = (d, rank, kind)
    if best is None:
        return None
    kind = best[2]
    if kind == "funding" and DEBT_AFTER.match(sentence[end : end + 30]):
        return "debt"
    return kind


def _month(name: str) -> int:
    return MONTH_NUM[name[:3].lower()]


Candidate = tuple[int, int, str]  # (start, end, normalised value)


def date_candidates(sentence: str) -> list[Candidate]:
    """Every date a sentence states, as (start, end, ISO date or YYYY-MM)."""
    out: list[Candidate] = []
    taken: list[tuple[int, int]] = []

    def add(m: re.Match[str], value: str) -> None:
        if any(m.start() < e and s < m.end() for s, e in taken):
            return
        taken.append((m.start(), m.end()))
        out.append((m.start(), m.end(), value))

    for m in DATE_FULL.finditer(sentence):
        add(m, f"{m.group(3)}-{_month(m.group(1)):02d}-{int(m.group(2)):02d}")
    for m in DATE_DMY.finditer(sentence):
        add(m, f"{m.group(3)}-{_month(m.group(2)):02d}-{int(m.group(1)):02d}")
    for m in DATE_ISO.finditer(sentence):
        add(m, m.group(0))
    for m in DATE_MY.finditer(sentence):
        add(m, f"{m.group(2)}-{_month(m.group(1)):02d}")
    return out


def period_candidates(sentence: str, published: date | None, as_of: date | None) -> list[Candidate]:
    """Every fiscal year or quarter a sentence names, as (start, end, 'FY2025' | 'Q2-2025')."""
    out: list[Candidate] = []
    for m in re.finditer(r"\bFY\s?'?(20\d\d)\b", sentence, re.I):
        out.append((m.start(), m.end(), f"FY{m.group(1)}"))
    for m in re.finditer(r"\bQ([1-4])\s*(?:of\s+)?(?:fiscal\s+)?(20\d\d)\b", sentence, re.I):
        out.append((m.start(), m.end(), f"Q{m.group(1)}-{m.group(2)}"))
    for m in re.finditer(
        r"\b(first|second|third|fourth)[- ]quarter(?: of)?\s+(?:fiscal\s+)?(20\d\d)\b",
        sentence,
        re.I,
    ):
        out.append((m.start(), m.end(), f"Q{QUARTER_WORD[m.group(1).lower()]}-{m.group(2)}"))
    year_rx = (
        re.compile(
            r"\b(?:in|for|during|of|from|through|since)\s+(?:fiscal\s+|calendar\s+)?"
            r"(?:the (?:full )?year\s+)?(20\d\d)\b(?!\s*[-–]\s*\d)",
            re.I,
        ),
        re.compile(r"\b(20\d\d)\s+(?:annual\s+)?(?:revenues?|sales|net|EBITDA)\b", re.I),
    )
    for rx in year_rx:
        for m in rx.finditer(sentence):
            year = int(m.group(1))
            if as_of is None or year <= as_of.year:
                out.append((m.start(), m.end(), f"FY{year}"))
    if published:
        for m in re.finditer(r"\blast year\b", sentence, re.I):
            out.append((m.start(), m.end(), f"FY{published.year - 1}"))
    return out


def nearest(cands: list[Candidate], start: int, end: int) -> str | None:
    """The candidate value closest to the dollar amount at [start, end)."""
    best: tuple[int, str] | None = None
    for s, e, val in cands:
        d = 0 if (s < end and start < e) else (start - e if e <= start else s - end)
        if best is None or d < best[0]:
            best = (d, val)
    return best[1] if best else None


def _label(kind: str, sentence: str, value: float) -> str:
    if kind == "revenue":
        return "Revenue"
    if kind == "run_rate":
        return "Revenue run rate (annualized)"
    if kind == "net_income":
        return "Net loss" if value < 0 else "Net income"
    if kind == "ebitda":
        return "EBITDA"
    if kind == "valuation":
        if re.search(r"post-money", sentence, re.I):
            return "Valuation (post-money)"
        if re.search(r"pre-money", sentence, re.I):
            return "Valuation (pre-money)"
        return "Valuation"
    if kind == "funding":
        s = SERIES_RX.search(sentence)
        return f"Funding raised ({s.group(1)})" if s else "Funding raised"
    if kind == "debt":
        d = DEBT_TYPE_RX.search(sentence)
        return f"Debt financing ({d.group(0).lower()})" if d else "Debt financing"
    if kind == "tender":
        return "Tender offer or secondary sale"
    if kind == "backlog":
        return "Backlog or contracted value"
    if kind == "headcount":
        return "Employees"
    return kind


def _metric(kind: str) -> str:
    return {
        "revenue": "press_revenue",
        "run_rate": "press_revenue_run_rate",
        "net_income": "press_net_income",
        "ebitda": "press_ebitda",
        "valuation": "press_valuation",
        "funding": "press_funding_raised",
        "debt": "press_debt_financing",
        "tender": "press_tender_valuation",
        "backlog": "press_backlog",
        "headcount": "press_employee_count",
    }[kind]


FLOW_KINDS = {"revenue", "run_rate", "net_income", "ebitda"}


def extract_datapoints(
    text: str,
    source_id: str,
    *,
    kinds: tuple[str, ...] | list[str],
    names: list[str],
    published: date | None,
    title: str = "",
    as_of: date | None = None,
) -> list[Datapoint]:
    """Deterministic extraction over one fetched page.

    A sentence qualifies when it names the company (or opens with "the company" / "it" right
    after a sentence that does) and carries no forward-looking or in-talks language. Each
    dollar amount is classified by its nearest trigger phrase; an amount with no stated
    period or date falls back to the page's own date, and a page with neither yields nothing:
    an undated number is never emitted."""
    if not text or not names:
        return []
    want = set(kinds)
    out: list[Datapoint] = []
    seen: set[tuple[str, float, str]] = set()
    title_named = any(n in title.lower() for n in names)
    prev_named = title_named
    for raw in sentences_safe(_clean_markdown(text)):
        sent = " ".join(raw.split())
        if not sent or len(sent) > SENTENCE_MAX:
            prev_named = False
            continue
        low = sent.lower()
        named = any(n in low for n in names)
        about = named or (prev_named and PRONOUN_START.match(sent) is not None)
        prev_named = named or (prev_named and about)
        if not about or FORWARD.search(sent):
            continue
        basis = "estimated" if ESTIMATE_WORDS.search(sent) else "reported"
        dates = date_candidates(sent)
        periods = period_candidates(sent, published, as_of)
        pub_s = published.isoformat() if published else None
        for val, s, e in money_tokens(sent):
            kind = classify_money(sent, s, e)
            if kind is None or kind not in want:
                continue
            if kind == "net_income" and LOSS_WORDS.search(sent):
                val = -abs(val)
            date_s = nearest(dates, s, e)
            if kind in FLOW_KINDS:
                period = nearest(periods, s, e) or date_s or pub_s
            else:
                period = date_s or pub_s
            if not period:
                continue
            key = (kind, val, period)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Datapoint(
                    kind=kind,
                    metric=_metric(kind),
                    label=_label(kind, sent, val),
                    value=val,
                    unit="USD",
                    period=period,
                    basis=basis,
                    source_id=source_id,
                    quote=sent[:300],
                )
            )
        if "headcount" in want:
            for hm in HEADCOUNT_RX.finditer(sent):
                n = float(hm.group(1).replace(",", ""))
                period = nearest(dates, hm.start(), hm.end()) or pub_s
                if not period or n < 10:
                    continue
                key = ("headcount", n, period)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    Datapoint(
                        "headcount",
                        _metric("headcount"),
                        _label("headcount", sent, n),
                        n,
                        "count",
                        period,
                        basis,
                        source_id,
                        sent[:300],
                    )
                )
    return out


# ---- model-assisted extraction (kept only when verifiable) ---------------------------------


class _ModelItem(BaseModel):
    kind: Literal[
        "revenue",
        "run_rate",
        "net_income",
        "ebitda",
        "valuation",
        "funding",
        "debt",
        "tender",
        "backlog",
        "headcount",
    ]
    value_usd: float = Field(description="the amount in US dollars (a count for headcount)")
    period: str = Field(description="FY2025, Q2-2025, YYYY-MM-DD or YYYY-MM")
    basis: Literal["reported", "estimated"] = "reported"
    source_id: str
    quote: str = Field(description="the sentence from the source text that states the figure")


class _ModelItems(BaseModel):
    items: list[_ModelItem] = Field(default_factory=list)


def _norm_ws(s: str) -> str:
    return " ".join(_clean_markdown(s).split()).lower()


def verify_model_item(item: _ModelItem, page_text: str, names: list[str], kinds: set[str]) -> bool:
    """A model-proposed datapoint stands only if its quote is a passage of the registered page,
    names the company or follows one that does, carries the stated number, has no forward-
    looking language and uses a well-formed period."""
    if item.kind not in kinds or not PERIOD_OK.match(item.period):
        return False
    quote = " ".join(item.quote.split())
    in_page = quote.lower() in _norm_ws(page_text) or quote in page_text
    if len(quote) < 20 or not in_page:
        return False
    if FORWARD.search(quote) or not any(n in quote.lower() for n in names):
        return False
    if item.kind == "headcount":
        return any(
            abs(float(m.group(1).replace(",", "")) - item.value_usd) <= 0.5
            for m in HEADCOUNT_RX.finditer(quote)
        )
    return any(
        abs(v - abs(item.value_usd)) <= 0.005 * abs(item.value_usd)
        for v, _, _ in money_tokens(quote)
    )


def model_datapoints(
    ctx: ToolContext, source_ids: list[str], kinds: tuple[str, ...]
) -> list[Datapoint]:
    """The drafter reads the dollar-bearing sentences of the fetched pages and proposes
    datapoints; each is verified against the page text before it is kept."""
    if not llm_available() or not source_ids:
        return []
    names = company_names(ctx.entity)
    blocks: list[str] = []
    pages: dict[str, str] = {}
    for sid in source_ids:
        if not ctx.store.has(sid):
            continue
        text = ctx.store.text(sid)
        pages[sid] = text
        sents = [
            " ".join(s.split())
            for s in sentences_safe(_clean_markdown(text))
            if "$" in s and len(s) <= SENTENCE_MAX
        ][:40]
        if sents:
            rec = ctx.store.get(sid)
            blocks.append(
                f"[{sid}] {rec.title} ({rec.published_date or 'n.d.'})\n- " + "\n- ".join(sents)
            )
    if not blocks:
        return []
    system = (
        "You extract dated, press-reported financial datapoints about one private company from "
        "sentences of fetched web pages. Report only figures the sentence states as having "
        "happened or been reported (never plans, targets, talks or forecasts) and only about the "
        "named company. Copy the sentence verbatim into `quote`. Never compute, convert, "
        "combine or infer a number; if unsure, leave the item out."
    )
    user = (
        f"Company: {ctx.entity.name}; aliases: {', '.join(ctx.entity.aliases) or 'none'}.\n"
        f"Allowed kinds: {', '.join(k for k in kinds if k != 'form_d')}.\n"
        "Period: FY2025 / Q2-2025 for revenue, income and EBITDA; the event date YYYY-MM-DD "
        "(or YYYY-MM) for funding, valuation, debt, tender, backlog and headcount.\n\n"
        + "\n\n".join(blocks)[:30000]
    )
    try:
        out = invoke_structured("drafter", _ModelItems, system, user, ctx.runlog)
    except Exception as e:  # noqa: BLE001 - the deterministic pass already ran
        ctx.log(f"private model extraction failed: {e!r}")
        return []
    if not out:
        return []
    kept: list[Datapoint] = []
    want = set(kinds)
    for it in out.items:
        page = pages.get(it.source_id)
        if page is None or not verify_model_item(it, page, names, want):
            ctx.runlog.count("private_model_datapoints_rejected")
            continue
        val = (
            -abs(it.value_usd)
            if it.kind == "net_income" and LOSS_WORDS.search(it.quote)
            else it.value_usd
        )
        kept.append(
            Datapoint(
                it.kind,
                _metric(it.kind),
                _label(it.kind, it.quote, val),
                val,
                "count" if it.kind == "headcount" else "USD",
                it.period,
                it.basis,
                it.source_id,
                it.quote[:300],
                method="model",
            )
        )
    return kept


def merge_datapoints(dps: list[Datapoint]) -> list[Datapoint]:
    """One row per (kind, value, period); further sources for the same figure are kept as
    extra citations. Order is stable: first seen wins."""
    out: dict[tuple[str, float, str], Datapoint] = {}
    for d in dps:
        key = (d.kind, d.value, d.period)
        if key in out:
            if d.source_id != out[key].source_id and d.source_id not in out[key].extra_sources:
                out[key].extra_sources.append(d.source_id)
            continue
        out[key] = d
    return list(out.values())


def sort_datapoints(dps: list[Datapoint]) -> list[Datapoint]:
    order = {k: i for i, k in enumerate(("revenue", "run_rate", "net_income", "ebitda"))}
    return sorted(
        dps,
        key=lambda d: (
            0 if d.kind in order else 1,
            order.get(d.kind, 0),
            _neg(d.period),
        ),
    )


def _neg(period: str) -> tuple[int, ...]:
    """Sort key putting the latest period first (FY2025, Q2-2025 and dates compare by year,
    then finer parts)."""
    nums = [int(n) for n in re.findall(r"\d+", period)]
    return tuple(-n for n in nums)


# ---- claims and tables -------------------------------------------------------------------


def fmt_usd(v: float) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        s = f"{a / 1e9:,.2f}".rstrip("0").rstrip(".") + " billion"
    elif a >= 1e6:
        s = f"{a / 1e6:,.1f}".rstrip("0").rstrip(".") + " million"
    else:
        s = f"{a:,.0f}"
    return f"{sign}${s}"


def fmt_value(d: Datapoint) -> str:
    return f"{d.value:,.0f}" if d.unit == "count" else fmt_usd(d.value)


def source_tier(store: EvidenceStore, sid: str) -> int:
    return store.get(sid).tier if store.has(sid) else 2


def tier_label(store: EvidenceStore, d: Datapoint) -> str:
    tiers = sorted({source_tier(store, s) for s in d.source_ids if store.has(s)})
    if d.kind.startswith("form_d"):
        return "1 (SEC filing, issuer-filed)"
    if tiers == [1]:
        return "1 (government or SEC page)"
    return f"{tiers[0] if tiers else 2} (press / open web)"


def _cite(sids: list[str]) -> str:
    return "".join(f" [{s}]" for s in dict.fromkeys(sids) if s)


def period_text(d: Datapoint) -> str:
    if d.kind in FLOW_KINDS and re.match(r"^(FY|Q)", d.period):
        return d.period
    return f"reported {d.period}" if d.kind in FLOW_KINDS else d.period


def press_claims(section: int, dps: list[Datapoint], store: EvidenceStore) -> list[Claim]:
    claims = []
    for d in dps:
        sids = [s for s in d.source_ids if store.has(s)]
        if not sids:
            continue
        c = common.claim(
            section,
            d.metric,
            d.value,
            d.period,
            sids,
            unit=d.unit,
            concept="press_reported" if d.metric.startswith(PRESS_PREFIX) else "form_d_reported",
            note=f"unaudited; {d.basis}; source tier {tier_label(store, d)}; {d.label}",
        )
        if c:
            claims.append(c)
    return claims


def press_table(dps: list[Datapoint], store: EvidenceStore) -> str:
    rows = []
    for d in dps[:MAX_ROWS]:
        sids = [s for s in d.source_ids if store.has(s)]
        if not sids:
            continue
        rec = store.get(sids[0])
        rows.append(
            [
                d.label,
                fmt_value(d) + _cite(sids),
                period_text(d),
                d.basis,
                tier_label(store, d),
                f"{rec.publisher}, {common.fmt_date(rec.published_date)}",
            ]
        )
    if not rows:
        return ""
    return common.md_table(
        ["Metric", "Value", "Period / date", "Basis", "Source tier", "Source"], rows, 99
    )


def rounds_table(dps: list[Datapoint], store: EvidenceStore) -> str:
    """Section 13: one row per (source, date) with the amount raised and the valuation side
    by side. No multiple is computed."""
    groups: dict[tuple[str, str], list[Datapoint]] = {}
    for d in dps:
        if d.kind not in ("funding", "valuation", "tender", "form_d"):
            continue
        groups.setdefault((d.source_id, d.period), []).append(d)
    rows = []
    for (sid, period), items in sorted(groups.items(), key=lambda kv: _neg(kv[0][1]))[:MAX_ROWS]:
        if not store.has(sid):
            continue
        amount = next((d for d in items if d.kind in ("funding", "tender")), None)
        sold = next((d for d in items if d.metric == "form_d_amount_sold"), None)
        offering = next((d for d in items if d.metric == "form_d_total_offering"), None)
        val = next((d for d in items if d.kind == "valuation"), None)
        if offering or sold:
            event = "Form D private placement" + (
                f" (offering up to {fmt_usd(offering.value)})" if offering else ""
            )
            amt_cell = (fmt_usd(sold.value) + " sold" + _cite(sold.source_ids)) if sold else "n/a"
        elif amount:
            event = amount.label.replace("Funding raised", "Funding round").replace(
                "Tender offer or secondary sale", "Tender offer / secondary sale"
            )
            amt_cell = fmt_usd(amount.value) + _cite(amount.source_ids)
        else:
            event = "Valuation reported"
            amt_cell = "n/a"
        val_cell = (fmt_usd(val.value) + _cite(val.source_ids)) if val else "n/a"
        lead = items[0]
        rec = store.get(sid)
        rows.append(
            [
                period,
                event,
                amt_cell,
                val_cell,
                "issuer-filed" if lead.kind == "form_d" else lead.basis,
                tier_label(store, lead),
                f"{rec.publisher}, {common.fmt_date(rec.published_date)}",
            ]
        )
    if not rows:
        return ""
    return common.md_table(
        ["Date", "Event", "Amount raised", "Valuation", "Basis", "Source tier", "Source"],
        rows,
        99,
    )


# ---- Form D ----------------------------------------------------------------------------


def _form_d_num(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    return float(m.group(0).replace(",", "")) if m and "indefinite" not in raw.lower() else None


def parse_form_d(text: str) -> dict[str, Any]:
    """Offering fields of a Form D, from the XML (primary_doc.xml) or its rendered page."""

    def xml(tag: str) -> str | None:
        m = re.search(rf"<{tag}>\s*(?:<value>)?\s*([^<]+?)\s*(?:</value>)?\s*</{tag}>", text)
        return m.group(1) if m else None

    def page(label: str) -> str | None:
        m = re.search(rf"{label}\s*[:|]?\s*(\$?\s*[\w,.]+)", text, re.I)
        return m.group(1) if m else None

    offering = xml("totalOfferingAmount") or page("Total Offering Amount")
    sold = xml("totalAmountSold") or page("Total Amount Sold")
    first = xml("dateOfFirstSale")
    issuer = xml("entityName") or xml("issuerName")
    return {
        "issuer": issuer,
        "total_offering": _form_d_num(offering),
        "total_sold": _form_d_num(sold),
        "first_sale": first if first and re.match(r"20\d\d-\d\d-\d\d", first) else None,
        "related": _related_persons(text),
    }


def _related_persons(text: str) -> list[dict[str, Any]]:
    """Executive officers, directors and promoters a Form D names (XML form only)."""
    out = []
    for blk in re.findall(r"<relatedPersonInfo>(.*?)</relatedPersonInfo>", text, re.S):
        first = re.search(r"<firstName>\s*([^<]+?)\s*</firstName>", blk)
        last = re.search(r"<lastName>\s*([^<]+?)\s*</lastName>", blk)
        rels = re.findall(r"<relationship>\s*([^<]+?)\s*</relationship>", blk)
        if last:
            name = f"{first.group(1) + ' ' if first else ''}{last.group(1)}"
            out.append({"name": name, "relationships": rels})
    return out


def _filing_hit_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for h in ((data or {}).get("hits") or {}).get("hits") or []:
        src = h.get("_source") or {}
        _id = str(h.get("_id") or "")
        accn, _, fname = _id.partition(":")
        ciks = src.get("ciks") or []
        form = str(src.get("form_type") or src.get("file_type") or "D")
        if not accn or not ciks or not form.upper().startswith("D"):
            continue  # only Form D (and D/A); a full-text hit for any other form is not one
        if len(accn) == 18 and "-" not in accn:
            accn = f"{accn[:10]}-{accn[10:12]}-{accn[12:]}"
        rows.append(
            {
                "accession": accn,
                "cik": pad_cik(ciks[0]),
                "filename": fname or "primary_doc.xml",
                "filed": src.get("file_date"),
                "form": form,
                "names": " ".join(src.get("display_names") or []),
                "description": src.get("file_description") or "",
            }
        )
    return rows


def _register_form_d(
    edgar: EdgarClient, store: EvidenceStore, row: dict[str, Any], doc_text: str | None
) -> dict[str, Any]:
    fields = parse_form_d(doc_text or "")
    issuer = fields["issuer"] or re.sub(r"\s*\(CIK.*", "", row["names"]).strip() or "issuer"
    filed = date.fromisoformat(row["filed"]) if row.get("filed") else None
    index_url = edgar.accession_index_url(row["cik"], row["accession"])
    lines = [
        f"# Form D notice of exempt offering: {issuer}",
        "",
        f"SEC accession {row['accession']}; form {row['form']}; filed {row.get('filed') or 'n.d.'}.",
        f"Total offering amount: {fields['total_offering'] if fields['total_offering'] is not None else 'not stated'}",
        f"Total amount sold: {fields['total_sold'] if fields['total_sold'] is not None else 'not stated'}",
        f"Date of first sale: {fields['first_sale'] or 'not stated'}",
        "",
        "These figures are issuer-reported in an SEC notice of an exempt offering; they are not "
        "audited financial statements.",
    ]
    if doc_text:
        lines += ["", "```xml", doc_text[:6000], "```"]
    rec = store.register(
        "\n".join(lines),
        type="Form D",
        publisher="SEC EDGAR",
        title=f"Form D notice of exempt offering, {issuer}, filed {row.get('filed') or 'n.d.'}",
        url=index_url,
        published_date=filed,
        tier=1,
        gov_id=row["accession"],
        meta={"form": "D", "cik": row["cik"], **fields},
        dedupe_key=f"form_d|{row['accession']}",
    )
    return {
        "source_id": rec.source_id,
        "accession": row["accession"],
        "cik": row["cik"],
        "filed": row.get("filed"),
        "issuer": issuer,
        "total_offering": fields["total_offering"],
        "total_sold": fields["total_sold"],
        "first_sale": fields["first_sale"],
        "related": fields["related"],
    }


def _names_match(name: str, haystack: str) -> bool:
    n = norm_company(name)
    return bool(n) and n in norm_company(haystack)


def discover_form_d(
    edgar: EdgarClient,
    store: EvidenceStore,
    fc: FirecrawlClient | None,
    ent: Entity,
    as_of: date,
    runlog: RunLog,
    *,
    search_budget: int = 1,
    scrape_budget: int = 3,
) -> dict[str, Any]:
    """Private-placement filings for the company: EDGAR full-text first, then a Firecrawl
    search of sec.gov. Every EDGAR/Firecrawl call is wrapped; failures are counted
    (`private_form_d_failures` in run.json) and reported as coverage notes."""
    res: dict[str, Any] = {
        "source": None,
        "filings": [],
        "failures": 0,
        "notes": [],
        "queries": [],
    }
    years = int(private_cfg().get("form_d_years", 5))
    query = f'"{ent.name}"'
    res["queries"].append(f"edgar full-text {query} forms D")
    rows: list[dict[str, Any]] | None = None
    try:
        data = edgar.fulltext_search(
            query, forms=["D"], start=as_of - timedelta(days=365 * years), end=as_of
        )
        rows = [r for r in _filing_hit_rows(data) if _names_match(ent.name, r["names"])]
        res["source"] = "edgar_fulltext"
    except Exception as e:  # noqa: BLE001 - 403 from this network is expected
        res["failures"] += 1
        runlog.count("private_form_d_failures")
        runlog.error(f"private Form D: EDGAR full-text search failed: {e!r}"[:300])
        res["notes"].append("EDGAR full-text search for Form D was refused or failed")
    if rows:
        for row in rows[:MAX_FORM_D]:
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/"
                f"{row['accession'].replace('-', '')}/{row['filename']}"
            )
            try:
                doc = edgar.get_text(url)
            except Exception as e:  # noqa: BLE001
                res["failures"] += 1
                runlog.count("private_form_d_failures")
                runlog.error(f"private Form D: document fetch failed {url}: {e!r}"[:300])
                doc = None
            res["filings"].append(_register_form_d(edgar, store, row, doc))
    elif rows is not None:
        res["notes"].append("EDGAR full-text search found no Form D filing under the company name")
    if rows is None:  # EDGAR failed: look for sec.gov pages through web search
        if fc is None or not fc.available:
            res["notes"].append("no Firecrawl fallback configured for Form D discovery")
        else:
            _form_d_via_search(fc, store, ent, res, runlog, search_budget, scrape_budget)
    return res


def _form_d_via_search(
    fc: FirecrawlClient,
    store: EvidenceStore,
    ent: Entity,
    res: dict[str, Any],
    runlog: RunLog,
    search_budget: int,
    scrape_budget: int,
) -> None:
    q = f'"{ent.name}" "Form D" site:sec.gov'
    res["queries"].append(q)
    try:
        hits = fc.search(q, limit=8, section_hint="foundation")
    except FirecrawlError as e:
        res["failures"] += 1
        runlog.count("private_form_d_failures")
        runlog.error(f"private Form D: web search failed: {e}"[:300])
        res["notes"].append("Form D web search failed")
        return
    res["source"] = "firecrawl_search"
    sec_hits = [
        h
        for h in hits
        if h.get("domain", "") == "sec.gov" or h.get("domain", "").endswith(".sec.gov")
    ]
    if not sec_hits:
        res["notes"].append("web search found no sec.gov Form D page for the company")
        return
    for h in sec_hits[:scrape_budget]:
        try:
            page = fc.scrape(h["url"], type="Form D", section_hint="foundation")
        except FirecrawlError as e:
            res["failures"] += 1
            runlog.count("private_form_d_failures")
            runlog.error(f"private Form D: scrape failed {h['url']}: {e}"[:300])
            continue
        sid, md = page.get("source_id"), page.get("markdown") or ""
        if not sid or not _names_match(ent.name, md + " " + (h.get("title") or "")):
            continue
        fields = parse_form_d(md)
        rec = store.get(sid)
        res["filings"].append(
            {
                "source_id": sid,
                "accession": rec.gov_id,
                "cik": None,
                "filed": rec.published_date.isoformat() if rec.published_date else None,
                "issuer": fields["issuer"] or ent.name,
                "total_offering": fields["total_offering"],
                "total_sold": fields["total_sold"],
                "first_sale": fields["first_sale"],
                "related": fields["related"],
            }
        )


def form_d_datapoints(ctx: ToolContext) -> list[Datapoint]:
    """Offering and amount-sold figures of the Form D filings the foundation pack found."""
    out: list[Datapoint] = []
    info = ((ctx.foundation.get("private") or {}).get("form_d") or {}).get("filings") or []
    for f in info:
        sid = f.get("source_id")
        if not sid or not ctx.store.has(sid):
            continue
        period = f.get("first_sale") or f.get("filed")
        if not period:
            continue
        for key, metric, label in (
            ("total_offering", "form_d_total_offering", "Form D total offering amount"),
            ("total_sold", "form_d_amount_sold", "Form D amount sold"),
        ):
            if f.get(key) is not None:
                out.append(
                    Datapoint(
                        "form_d", metric, label, float(f[key]), "USD", str(period)[:10],
                        "issuer-filed", sid, method="form_d",
                    )
                )  # fmt: skip
    return out


# ---- foundation pack (private) ------------------------------------------------------------

GENERIC_TAIL = {
    "industries",
    "technologies",
    "technology",
    "systems",
    "labs",
    "holdings",
    "group",
    "international",
    "enterprises",
    "solutions",
    "aerospace",
    "defense",
    "space",
    "dynamics",
    "robotics",
    "aviation",
}


def site_from_hits(hits: list[dict[str, Any]], names: list[str]) -> str | None:
    """The company's own domain among search hits: the registrable label equals the whole
    name ("acmeorbital" for Acme Orbital) or the first word when the rest is generic
    ("anduril" for Anduril Industries). A looser match would adopt another company's site."""
    for name in names:
        toks = norm_company(name).split()
        if not toks:
            continue
        joined = "".join(toks)
        first_ok = all(t in GENERIC_TAIL for t in toks[1:]) and len(toks[0]) >= 4
        for h in hits:
            host = (h.get("domain") or urlparse(h.get("url", "")).netloc).lower()
            labels = host.removeprefix("www.").split(".")
            if len(labels) < 2:
                continue
            label = re.sub(r"[^a-z0-9]", "", labels[-2] if len(labels[-1]) > 2 else labels[-3:][0])
            if label == joined or (first_ok and label == toks[0]):
                return host
    return None


def build_private_foundation(
    state_inputs: Any,
    ent: Entity,
    store: EvidenceStore,
    as_of: date,
    found: dict[str, Any],
    runlog: RunLog,
    classify_links,
) -> None:
    """Foundation pack for a company with no SEC filings: Form D filings, the company site and
    its press pages. Adds `private`, `ir` and `sources` keys to `found`; every call is wrapped
    and a failure is a coverage note."""
    offline = bool(state_inputs.offline)
    budget = foundation_budget()
    fc = FirecrawlClient(
        store, runlog=runlog, credit_cap=int(budget.get("credits", 30)), offline=offline
    )
    fc_ok = fc.available
    edgar = EdgarClient(runlog=runlog, offline=offline)
    priv: dict[str, Any] = {"notes": [], "press": [], "site": None}
    found["private"] = priv
    found["sources"].setdefault("Form D", [])
    found["sources"].setdefault("press_release", [])
    found["sources"].setdefault("ir_map", [])
    found["ir"] = {
        "site": None,
        "links": {"presentations": [], "news": [], "events": [], "other": []},
    }

    form_d = discover_form_d(
        edgar,
        store,
        fc if fc_ok else None,
        ent,
        as_of,
        runlog,
        search_budget=1,
        scrape_budget=min(3, int(budget.get("scrapes", 10))),
    )
    priv["form_d"] = form_d
    found["sources"]["Form D"] = [f["source_id"] for f in form_d["filings"] if f.get("source_id")]
    priv["notes"].extend(form_d["notes"])

    if not fc_ok:
        priv["notes"].append("Firecrawl not configured: no company site, press or news pages")
        found["transcripts"] = {"found": 0, "missed": [], "requested": 0, "note": "private company"}
        return
    names = [ent.name, *ent.aliases]
    co = ent.aliases[0] if ent.aliases else ent.name
    try:
        hits = fc.search(f"{co} official website", limit=8, section_hint="foundation")
        priv["site"] = site_from_hits(hits, names)
    except FirecrawlError as e:
        runlog.error(f"private foundation site search: {e}"[:300])
        priv["notes"].append("company site search failed")
    site = priv["site"]
    found["ir"]["site"] = site
    if site:
        links: list[str] = []
        for cand in (f"https://{site}",):
            try:
                links = fc.map(cand, search="news press releases announcements", limit=150)
            except FirecrawlError as e:
                runlog.error(f"private foundation map {cand}: {e}"[:300])
                continue
            if links:
                found["ir"]["root"] = cand
                break
        if links:
            classified = classify_links(links)
            found["ir"]["links"] = classified
            rec = store.register(
                f"# Site map for {ent.name}\n\nRoot: {found['ir'].get('root')}\n\n"
                + "\n".join(f"- {u}" for u in links),
                type="web",
                publisher=urlparse(found["ir"]["root"]).netloc,
                title=f"Company site map ({len(links)} links)",
                url=found["ir"]["root"],
                published_date=as_of,
                tier=2,
                meta={"ir_map": True, "private": True},
                dedupe_key=f"ir_map|{found['ir']['root']}|{as_of.isoformat()}",
            )
            found["sources"]["ir_map"] = [rec.source_id]
    else:
        priv["notes"].append("no company site identified")
    try:
        hits = fc.search(f"{co} announces press release", limit=8, section_hint="foundation")
    except FirecrawlError as e:
        runlog.error(f"private foundation press search: {e}"[:300])
        priv["notes"].append("press search failed")
        hits = []
    scrapes = 0
    cap = max(0, int(budget.get("scrapes", 10)) - 3)
    for h in hits:
        priv["press"].append({"url": h["url"], "title": h.get("title")})
        if scrapes >= min(2, cap):
            continue
        try:
            page = fc.scrape(h["url"], type="press_release", section_hint="foundation")
        except FirecrawlError as e:
            runlog.error(f"private foundation press scrape {h['url']}: {e}"[:300])
            continue
        scrapes += 1
        sid = page.get("source_id")
        if sid and any(
            n.lower() in (page.get("markdown") or "").lower() for n in names if len(n) >= 4
        ):
            found["sources"]["press_release"].append(sid)
    found["transcripts"] = {"found": 0, "missed": [], "requested": 0, "note": "private company"}


# ---- section plumbing ------------------------------------------------------------------


def plan_items(section: int, brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    """Plan for a private-path section: no SEC call, the section's web queries."""
    items: list[dict[str, Any]] = [
        {
            "tool": "foundation.private",
            "args": {"form_d": True, "press": True},
            "why": "Form D filings and press pages from the private foundation pack",
        }
    ]
    for q in private_queries(section, ctx):
        items.append(
            {"tool": "firecrawl.search", "query": q, "why": "press-reported figures (unaudited)"}
        )
    return items


def _score_row(row: dict[str, Any], names: list[str], kinds: tuple[str, ...]) -> float:
    text = f"{row.get('title') or ''} {row.get('description') or ''}"
    low = text.lower()
    score = 0.0
    if any(n in low for n in names):
        score += 2
    if money_in(text):
        score += 2
    if any(TRIGGERS[k].search(text) for k in kinds if k in TRIGGERS):
        score += 1
    if row.get("_rank") is not None:
        score -= float(row["_rank"]) * 0.01  # web_research's own ranking (lower is better)
    return -score


def collect_press(
    ctx: ToolContext, section: int, plan: list[dict[str, Any]], kinds: tuple[str, ...]
) -> dict[str, Any]:
    """Run the section's searches inside its budget, scrape the most promising hits, and
    extract datapoints from every page held for the company. No SEC call is made."""
    queries = [i["query"] for i in plan if i.get("tool") == "firecrawl.search" and i.get("query")]
    # run the queries the section's search cap allows, most important first; a dropped
    # query is not a budget stop
    queries = queries[: max(0, ctx.budget.searches - ctx.budget.used_searches)]
    rows = ctx.web_research(
        queries,
        type="news",
        scrape=False,
        prefer_domains=tuple(trade_press(ctx.entity.sector_tag)),
    )
    names = company_names(ctx.entity)
    scored = sorted(rows, key=lambda r: _score_row(r, names, kinds))
    page_sids: list[str] = []
    for row in scored:
        if not ctx.budget.can_scrape():
            break
        sid = common.scrape_url(ctx, row["url"], type="news")
        if sid and sid not in page_sids:
            page_sids.append(sid)
            row["source_id"] = sid
    for sid in ctx.foundation_sources("press_release"):
        if sid not in page_sids:
            page_sids.append(sid)
    dps: list[Datapoint] = []
    for sid in page_sids:
        if not ctx.store.has(sid):
            continue
        rec = ctx.store.get(sid)
        if rec.access == "limited":
            continue
        dps.extend(
            extract_datapoints(
                ctx.store.text(sid),
                sid,
                kinds=kinds,
                names=names,
                published=rec.published_date,
                title=rec.title,
                as_of=ctx.as_of,
            )
        )
    dps.extend(model_datapoints(ctx, page_sids, kinds))
    if "form_d" in kinds:
        dps.extend(form_d_datapoints(ctx))
    fd = (ctx.foundation.get("private") or {}).get("form_d") or {}
    return {
        "datapoints": merge_datapoints(dps),
        "page_sids": page_sids,
        "hits": len(rows),
        "searches": ctx.budget.used_searches,
        "notes": list(dict.fromkeys(ctx.budget.notes)),
        "foundation_notes": list((ctx.foundation.get("private") or {}).get("notes") or []),
        "form_d_failures": int(fd.get("failures", 0)),
        "form_d_found": len(fd.get("filings") or []),
    }


KIND_NAMES = {
    "revenue": "revenue",
    "run_rate": "revenue run rate",
    "net_income": "net income or loss",
    "ebitda": "EBITDA",
    "funding": "funding round",
    "valuation": "valuation",
    "debt": "debt financing",
    "tender": "tender offer or secondary sale",
    "backlog": "backlog",
    "headcount": "headcount",
    "form_d": "Form D filing",
}
CAVEATS = {
    9: "no SEC financial statements exist for this private company; every figure below is press-reported and unaudited, dated, and tied to the page that states it, and no figure is estimated, interpolated or derived here",
    10: "no filings, earnings releases or consensus exist for this private company; operating figures are press-reported and unaudited, dated, and tied to the page that states them",
    11: "no balance sheet or debt note exists for this private company; funding, debt and tender figures are press-reported or taken from SEC Form D notices, unaudited, and no leverage or net debt is computed",
    13: "no market quote or share count exists for this private company; the table lists press-reported rounds and valuations with their dates and source tiers, and no multiple, price target or consensus is computed",
}


def found_and_missing(dps: list[Datapoint], kinds: tuple[str, ...]) -> tuple[list[str], list[str]]:
    grouped: dict[str, list[Datapoint]] = {}
    for d in dps:
        grouped.setdefault(d.kind, []).append(d)
    found = []
    for k in kinds:
        if k in grouped:
            ds = grouped[k]
            periods = ", ".join(dict.fromkeys(d.period for d in ds[:3]))
            n_src = len({s for d in ds for s in d.source_ids})
            found.append(f"{KIND_NAMES[k]} ({periods}, {n_src} source{'s' if n_src != 1 else ''})")
    missing = [KIND_NAMES[k] for k in kinds if k not in grouped]
    return found, missing


def coverage_note(dps: list[Datapoint], kinds: tuple[str, ...], tr: dict[str, Any]) -> str:
    found, missing = found_and_missing(dps, kinds)
    bits = []
    if found:
        bits.append("found: " + "; ".join(found))
    if missing:
        bits.append("not found: " + ", ".join(missing))
    bits.append(
        "no SEC financial statements exist, so no figure is audited and none is interpolated"
    )
    if "form_d" in kinds:
        if tr.get("form_d_failures"):
            bits.append(
                f"Form D discovery had {tr['form_d_failures']} failed lookup(s), so filings may be missing"
            )
        elif not tr.get("form_d_found"):
            bits.append("no Form D filing was found")
    return "; ".join(bits)


def unavailable_reason(tr: dict[str, Any], kinds: tuple[str, ...]) -> str:
    wanted = ", ".join(KIND_NAMES[k] for k in kinds)
    pages = len(tr.get("page_sids") or [])
    base = (
        f"not found: {wanted}; {pages} page{'s' if pages != 1 else ''} reviewed from "
        f"{tr.get('searches', 0)} search{'es' if tr.get('searches', 0) != 1 else ''}"
    )
    if "form_d" in kinds:
        base += (
            f"; Form D discovery had {tr['form_d_failures']} failed lookup(s)"
            if tr.get("form_d_failures")
            else "; no Form D filing was found"
        )
    return (
        base
        + "; no SEC financial statements exist for a private company, so no figure is estimated or derived"
    )


def _private_brief(brief: Brief, section: int) -> Brief:
    """The brief the drafter sees on the private path: the exhibit it is told about is the one
    that is actually built."""
    return brief.model_copy(
        update={
            "required_exhibit": EXHIBIT_TITLES[section],
            "common_failure": (
                "presenting a press number as audited, computing a multiple or a net debt without "
                "a cited price and share count, or filling a gap with an estimate"
            ),
            "structured_first": [],
        }
    )


PRIVATE_RULES = (
    "This is a private company with no SEC financial statements. State only the figures listed "
    "in the structured facts, each with its period or date and its source id, and call them "
    "press-reported and unaudited. Do not compute growth rates, margins, multiples, net debt "
    "or implied values, do not estimate anything missing, and do not state a price target or "
    "consensus. Say plainly what could not be found."
)


def lead_sentences(dps: list[Datapoint], store: EvidenceStore) -> list[str]:
    out = []
    for d in dps[:8]:
        sids = [s for s in d.source_ids if store.has(s)]
        if not sids:
            continue
        when = d.period if re.match(r"^(FY|Q)", d.period) else f"on {d.period}"
        out.append(f"{d.label} was {fmt_value(d)} ({d.basis}, {when}; unaudited){_cite(sids)}.")
    return out


def draft_press_section(
    brief: Brief,
    ctx: ToolContext,
    tool_results: dict[str, Any],
    *,
    section: int,
    critic_notes: list[str] | None = None,
    narrative_without_data: bool = False,
) -> SectionDraft:
    """The shared draft for sections 9, 10, 11 and 13 on the private path.

    Coverage is `partial` when at least one datapoint is cited and `unavailable` otherwise
    (section 10 may stay `partial` on cited news pages alone: `narrative_without_data`)."""
    kinds = SECTION_KINDS[section]
    store = ctx.store
    dps: list[Datapoint] = [
        d
        for d in sort_datapoints(list(tool_results.get("datapoints") or []))
        if any(store.has(s) for s in d.source_ids)
    ]
    pages = [s for s in (tool_results.get("page_sids") or []) if store.has(s)]
    if not dps and not (narrative_without_data and pages):
        return common.unavailable_draft(brief, unavailable_reason(tool_results, kinds), ctx)

    table = rounds_table(dps, store) if section == 13 else press_table(dps, store)
    claims = press_claims(section, dps, store)
    notes = [coverage_note(dps, kinds, tool_results)]
    notes.extend(tool_results.get("notes") or [])
    # a page that yielded no datapoint is not evidence of anything: only sources behind a
    # figure are listed (section 10 may narrate from news pages when no figure was found)
    evidence = list(
        dict.fromkeys([*(s for d in dps for s in d.source_ids), *([] if dps else pages)])
    )
    facts = [
        {
            "label": d.label,
            "metric": d.metric,
            "value": d.value,
            "unit": d.unit,
            "period": d.period,
            "basis": d.basis,
            "source_tier": tier_label(store, d),
            "source_ids": d.source_ids,
            "quote": d.quote,
        }
        for d in dps
    ]
    pbrief = _private_brief(brief, section)
    narrative = common.draft_section(
        ctx,
        pbrief,
        evidence_sids=evidence,
        facts=facts or None,
        exhibit_md=table,
        extra_rules=PRIVATE_RULES,
        critic_notes=critic_notes,
        keywords=["revenue", "valuation", "funding", "raised", "backlog", "employees"],
    ) or common.template_digest(ctx, evidence, lead_sentences(dps, store))
    return common.finalize_draft(
        brief,
        ctx,
        caveat=common.caveat_line(CAVEATS[section]),
        narrative=narrative,
        exhibit_md=table,
        exhibit_present=bool(table),
        claims=claims,
        coverage="partial",
        notes=notes,
        exhibit_title=EXHIBIT_TITLES[section] if table else None,
    )


def web_source_ids(ctx: ToolContext, tool_results: dict[str, Any]) -> list[str]:
    """Registered pages a web-only section fetched: `web` / `pages` / `page_sids` rows of a
    handler's tool results (dicts carrying `source_id` or `sid`), plus the press pages of the
    private foundation pack."""
    sids: list[str] = []
    for key in ("web", "pages", "page_sids"):
        for row in tool_results.get(key) or []:
            sid = row if isinstance(row, str) else (row.get("source_id") or row.get("sid"))
            if sid and ctx.store.has(sid) and sid not in sids:
                sids.append(sid)
    for sid in ctx.foundation_sources("press_release"):
        if sid not in sids:
            sids.append(sid)
    return sids


def draft_web_only(
    brief: Brief,
    ctx: ToolContext,
    tool_results: dict[str, Any],
    *,
    missing: str,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    """Draft for a section whose normal source is a 10-K (sections 2, 3, 5): on the private path
    it is written from the pages web research fetched, with no exhibit and coverage `partial`,
    or `unavailable` when no page was fetched. `missing` names what a private company lacks
    ("10-K"); the caveat and the coverage line read "no {missing} exists"."""
    pages = web_source_ids(ctx, tool_results)
    note = (
        f"private company: no {missing} exists, so this section rests on the press and web "
        "pages cited and its table is not built"
    )
    if not pages:
        return common.unavailable_draft(brief, f"{note}; no page was retrieved", ctx)
    pbrief = brief.model_copy(
        update={"required_exhibit": f"none: no {missing} exists for a private company"}
    )
    narrative = common.draft_section(
        ctx,
        pbrief,
        evidence_sids=pages,
        extra_rules=(
            "This is a private company with no SEC filings. Use only the cited pages; say when "
            "a fact is the company's own claim; do not fill gaps from memory."
        ),
        critic_notes=critic_notes,
    ) or common.template_digest(ctx, pages, [])
    return common.finalize_draft(
        brief,
        ctx,
        caveat=common.caveat_line(
            f"no {missing} exists for this private company, so every statement below rests on "
            "the web and press pages cited"
        ),
        narrative=narrative,
        exhibit_md="",
        exhibit_present=False,
        claims=[],
        coverage="partial",
        notes=[note, *(tool_results.get("notes") or [])],
    )
