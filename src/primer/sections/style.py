"""House-style prompt fragments and formatting helpers (requirements §3 editorial pattern).

Also the prose-hygiene vocabulary (review 2026-10-21 §8, §9 item 8): the words of the
pipeline ("the exhibit", "structured facts", "scrape cap", "the fetched evidence") never
reach the reader. The drafter is told so in HOUSE_STYLE and `common.scrub_pipeline_vocabulary`
enforces it deterministically on every draft.
"""

from __future__ import annotations

import re

HOUSE_STYLE = """You write one section of a company primer for an analyst who will read it critically.
Rules:
- Open with one caveat line when applicable (listing status, ownership concentration, data gaps).
- Then short subsections with declarative sentences. No first person. No investment recommendation,
  no price prediction stated as your own view.
- Every sentence that states a fact ends with one or more citation tokens of the form [src_0012].
  You may cite ONLY the source ids given to you. Never invent an id.
- A view may be attributed to a named firm only if a cited source's text names that firm.
- Numbers: prefer the structured tier-1 source; label derived figures as derived with the formula.
- When two sources disagree on a number, keep both and say so in a line starting "> Conflict:".
- Do not restate the same fact from several sources as if it were several facts.
- Write for the reader, never about the pipeline. Do not use the words "the exhibit", "structured
  facts", "structured extract", "structured data", "scrape cap", "search cap", "section budget",
  "planned web queries", "the fetched/captured/supplied evidence", "the evidence excerpt" or
  "query". Say "the table below", "the 10-K", "the Q2 call", "the sources reviewed". Research
  limits belong only in the coverage line, which is written for you; do not describe them.
- A "### Perspectives" list holds only views from parties other than the company (trade press,
  government auditors, peers, rating agencies, brokers named by a cited source). Management's own
  statements and Wikipedia are not perspectives; if no such second-party view is in the evidence,
  omit the Perspectives list entirely.
"""

CITATION_TOKEN = r"\[(src_\d{4})\]"

# pipeline vocabulary → reader language (applied to prose, never to the coverage line)
PIPELINE_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bthe exhibit's\b", re.I), "the table's"),
    (
        re.compile(r"\bthe exhibits?\b(?= (?:below|above|that follows|that will follow))", re.I),
        "the table",
    ),
    (re.compile(r"\bthe exhibits?\b", re.I), "the table below"),
    (re.compile(r"\bthe structured (?:facts|extract|data|record)s?\b", re.I), "the source data"),
    (re.compile(r"\bstructured (?:facts|extract)s?\b", re.I), "source data"),
    (
        re.compile(
            r"\bthe (?:captured|fetched|supplied|available|retrieved|gathered) (?:evidence|excerpts?|text)\b",
            re.I,
        ),
        "the sources reviewed",
    ),
    (re.compile(r"\b(?:captured|fetched|supplied|retrieved) evidence\b", re.I), "sources reviewed"),
    (re.compile(r"\bthe evidence excerpts?\b", re.I), "the source text"),
    (
        re.compile(r"\bthe (?:10-K|10-Q|filing|proxy|transcript|release) excerpt\b", re.I),
        lambda m: m.group(0).replace(" excerpt", ""),
    ),
    (re.compile(r"\bin evidence\b", re.I), "in the sources reviewed"),
    (re.compile(r"\bthe evidence\b", re.I), "the sources reviewed"),
    (re.compile(r"\bwas fetched\b", re.I), "was reviewed"),
    (
        re.compile(r"\bfetched (?:source|page|document)s?\b", re.I),
        lambda m: m.group(0).replace("fetched", "reviewed"),
    ),
)

# a sentence carrying any of these is a research note, not prose: it moves to the coverage line
PIPELINE_MOVE = re.compile(
    r"scrape cap|search cap|section budget|planned web quer|web quer(?:y|ies) (?:were |was )?dropped|"
    r"query[_ ]planner|firecrawl|research stopped|credits? (?:cap|budget)|the (?:drafter|critic|planner)\b|"
    r"\bsection (?:search|scrape) cap\b",
    re.I,
)


def fmt_money(value: float | None, unit: str = "USD", scale: str = "millions") -> str:
    if value is None:
        return "n/a"
    if unit == "USD/shares":
        return f"{value:,.2f}"
    if unit == "shares":
        return f"{value / 1e6:,.1f}M"
    if unit == "pure":
        return f"{value:,.3f}"
    if scale == "millions":
        return f"{value / 1e6:,.0f}"
    if scale == "billions":
        return f"{value / 1e9:,.2f}"
    return f"{value:,.0f}"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def coverage_line(status: str, detail: str) -> str:
    return f"> Coverage: {status} — {detail}"


def conflict_line(metric: str, pairs: list[tuple[str, str]]) -> str:
    parts = " and ".join(f"{v} {cite}" for v, cite in pairs)
    return f"> Conflict: {metric} reported as {parts}; sources differ."
