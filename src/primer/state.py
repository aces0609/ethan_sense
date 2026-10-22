"""PrimerState and the Pydantic models shared by every node (requirements §5, §7).

Reducers:
- evidence_refs, claims, errors, queries: append-only (operator.add)
- drafts, coverage, critic_scores: dict-merge so parallel Send branches do not clobber
"""

from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

CoverageStatus = Literal["full", "partial", "unavailable", "not_applicable"]
SourceTier = Literal[1, 2, 3]

SECTION_TITLES: dict[int, str] = {
    1: "Key Takeaways",
    2: "Company Overview",
    3: "Products, Services and Technology",
    4: "Key Programs and Contracts",
    5: "Market and Competitive Position",
    6: "Strategy and Growth",
    7: "Key Acquisition History",
    8: "Management and Governance",
    9: "Historical Financial Performance",
    10: "Operating Performance and Benchmarking",
    11: "Capital Structure and Liquidity",
    12: "Investment Thesis and Debates",
    13: "Valuation",
    14: "Recent Developments",
}

RESEARCHED_SECTIONS: tuple[int, ...] = tuple(range(2, 15))


def dict_merge(left: dict | None, right: dict | None) -> dict:
    out = dict(left or {})
    out.update(right or {})
    return out


class Inputs(BaseModel):
    company_name: str
    ticker: str | None = None
    as_of_date: date = Field(default_factory=date.today)
    sections: list[int] = Field(default_factory=lambda: list(range(1, 15)))
    debug: bool = False
    offline: bool = False


class Entity(BaseModel):
    name: str
    ticker: str | None = None
    cik: str | None = None  # 10-digit zero-padded
    exchange: str | None = None
    is_public: bool = False
    fiscal_year_end: str | None = None  # "1231"
    sic: str | None = None
    sector_tag: str = "default"
    aliases: list[str] = Field(default_factory=list)
    uei_set: list[str] = Field(default_factory=list)
    state_of_incorporation: str | None = None


class FilingRef(BaseModel):
    form: str
    filing_date: date
    report_date: date | None = None
    accession: str  # with dashes
    primary_document: str | None = None
    url: str
    index_url: str
    description: str | None = None
    items: str | None = None  # 8-K item codes, e.g. "2.02,9.01"


class SourceRecord(BaseModel):
    """One fetched item (requirements §7 source record)."""

    source_id: str
    type: str
    tier: SourceTier
    publisher: str
    title: str
    url: str
    published_date: date | None = None
    fetched_at: datetime
    content_hash: str
    evidence_path: str
    access: Literal["full", "limited"] = "full"
    gov_id: str | None = None
    cited_by: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class Claim(BaseModel):
    """A numeric claim the numeric-consistency validator can group on."""

    section: int
    metric: str  # normalized, e.g. "revenue", "net_income"
    value: float
    unit: str = "USD"
    period: str  # "FY2024", "Q2-2026", "2026-06-30"
    source_ids: list[str]
    concept: str | None = None
    note: str | None = None


class Brief(BaseModel):
    """Research brief for one section (requirements §6)."""

    section: int
    title: str
    scope: str
    source_buckets: list[str]  # priority order
    pages: float
    sub_questions: list[str]
    structured_first: list[str] = Field(default_factory=list)
    web_queries: list[str] = Field(default_factory=list)
    required_exhibit: str
    stop_condition: str
    common_failure: str | None = None
    aliases: list[str] = Field(default_factory=list)  # entity aliases appended to query batches
    trade_press: list[str] = Field(default_factory=list)  # sector trade-press domains to prefer
    planner: str = "template"  # "template" or the model that expanded the brief


class SectionDraft(BaseModel):
    section: int
    title: str
    markdown: str  # cites with [src_XXXX] tokens, no footnote numbers yet
    claims: list[Claim] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    coverage: CoverageStatus = "partial"
    caveat: str | None = None
    exhibit_present: bool = False
    critic_notes: list[str] = Field(default_factory=list)
    iterations: int = 0
    budget: dict[str, Any] = Field(default_factory=dict)  # searches/scrapes/credits used vs cap


class CriticScore(BaseModel):
    section: int
    brief_coverage: int
    citation_density: int
    distinct_sources: int
    exhibit: int
    style: int
    forbidden_content: int
    notes: list[str] = Field(default_factory=list)
    passed: bool

    @property
    def mean(self) -> float:
        vals = [
            self.brief_coverage,
            self.citation_density,
            self.distinct_sources,
            self.exhibit,
            self.style,
            self.forbidden_content,
        ]
        return sum(vals) / len(vals)


class PrimerState(TypedDict, total=False):
    inputs: dict[str, Any]
    run_id: str
    run_dir: str
    as_of: str
    entity: dict[str, Any]
    foundation: dict[str, Any]  # handles: filing refs, companyfacts source id, chunk index
    plan: dict[int, dict[str, Any]]
    evidence_refs: Annotated[list[str], operator.add]
    drafts: Annotated[dict[int, dict[str, Any]], dict_merge]
    claims: Annotated[list[dict[str, Any]], operator.add]
    coverage: Annotated[dict[int, str], dict_merge]
    critic_scores: Annotated[dict[int, dict[str, Any]], dict_merge]
    errors: Annotated[list[str], operator.add]
    validation: dict[str, Any]
    retries: Annotated[dict[int, int], dict_merge]
    mdx_path: str
    key_takeaways: str
    # section 14's dated events ({date, event, detail, source_id}), set by propagate_recent
    # after the first researcher wave and passed to the sections that read them
    recent_events: list[dict[str, Any]]
    recent_wave: int  # how many times propagate_recent has run (1 = second wave pending)
    reconciliation: dict[str, Any]  # cross-section reconciliation record (also in run.json)


class ResearcherState(TypedDict, total=False):
    """Input/output of the section researcher subgraph (one per Send)."""

    run_dir: str
    as_of: str
    entity: dict[str, Any]
    foundation: dict[str, Any]
    brief: dict[str, Any]
    critic_notes: list[str]
    iteration: int
    recent_events: list[dict[str, Any]]  # from section 14, for sections 3, 6, 8, 10, 12
    budget: dict[str, Any]
    plan_items: list[dict[str, Any]]
    tool_results: dict[str, Any]
    draft: dict[str, Any]
    drafts: Annotated[dict[int, dict[str, Any]], dict_merge]
    claims: Annotated[list[dict[str, Any]], operator.add]
    coverage: Annotated[dict[int, str], dict_merge]
    critic_scores: Annotated[dict[int, dict[str, Any]], dict_merge]
    evidence_refs: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
