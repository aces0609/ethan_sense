"""The research briefs from requirements §6, as data.

Phase 1 ships all 13 templates so plan_sections (Phase 2) has them to expand with aliases;
only section 9 is executed by the Phase 1 graph.
"""

from __future__ import annotations

from primer.config import section_budget, sector_brief_extras
from primer.state import SECTION_TITLES, Brief

_TEMPLATES: dict[int, dict] = {
    2: dict(
        scope="What the company is, how it is organized (segments), where it is listed, who controls it.",
        source_buckets=["10-K Item 1 and segment note", "IR site", "trade press"],
        structured_first=[
            "foundation:10-K Item 1",
            "foundation:segment note",
            "edgar:dei facts (HQ, FY end, shares)",
        ],
        web_queries=[
            "{co} business overview",
            "{co} segments revenue breakdown {fy}",
            "{co} company history founded",
        ],
        required_exhibit="Segment revenue table (3 FY)",
        common_failure="Writing from memory instead of the 10-K; missing the listing/ownership caveat.",
    ),
    3: dict(
        scope="Products, services and technology by line, with status and the key metric for each.",
        source_buckets=[
            "10-K Item 1 product list",
            "product pages and press releases",
            "sector trade press",
        ],
        structured_first=["foundation:10-K Item 1"],
        web_queries=[
            "{co} product roadmap",
            "{co} new product launch {fy}",
            "{co} technology differentiation",
        ],
        sub_questions=[
            "Which products and programs does the 10-K list by segment, and what is the status and key metric of each?",
            "Which products have a certification, regulatory approval, first-delivery or launch date within the window?",
        ],
        required_exhibit="Product/program table with status and key metric",
        common_failure="Marketing copy cited as fact; no competitor comparison.",
    ),
    4: dict(
        scope="US-government programs and contracts: program table, locations table, agency mix.",
        source_buckets=[
            "SAM.gov entity records",
            "USAspending awards",
            "DoD contract announcements",
            "GAO/SAR/budget books",
            "trade press (context only)",
        ],
        structured_first=[
            "samgov:entities under parent UEI (active, place-of-performance filter, last 5 FY)",
            "usaspending:spending_by_award by UEI FY-5..now sorted by potential value",
            "usaspending:spending_by_category awarding_subagency by FY",
            "samgov:opportunities award notices for top programs, trailing 12 months",
            "dod:contract announcements for the same programs",
        ],
        web_queries=[
            "{program} GAO report",
            "{program} selected acquisition report",
            "{program} budget justification {fy}",
            "{program} Nunn-McCurdy OR rebaseline OR schedule",
            "{co} {program} contract award",
        ],
        required_exhibit="Program table; locations table (active registrations with award activity in last 5 FY); agency-mix table by FY",
        common_failure="Programs inferred from award text without a confirming source; obligated mixed with ceiling; sub roles asserted from press.",
    ),
    5: dict(
        scope="Market structure, share, competitors and positioning; SWOT from sourced facts.",
        source_buckets=[
            "10-K Item 1A",
            "peer 10-K Item 1",
            "trade press",
            "analyst commentary in news",
        ],
        structured_first=[
            "foundation:10-K Item 1A",
            "edgar:peer tickers from the 10-K competitor list",
        ],
        web_queries=[
            "{co} market share",
            "{co} competitors",
            "{co} vs {peer}",
            "{co} vs {peer} order backlog OR fleet",
            "{co} market share by segment OR product line {fy}",
            "{co} anchor customer OR launch customer OR largest customer",
            "{co} moat analysis",
        ],
        sub_questions=[
            "Who are the competitors the 10-K names, and how does each position against the company?",
            "How do the company's order backlog, fleet or installed base compare with each named peer's, with a dated source?",
            "What market share does the company hold by segment or product line, and who reports it?",
            "Which anchor or launch customers underpin the main product lines, and what have they ordered?",
        ],
        required_exhibit="SWOT table; peer list with one-line positioning",
        common_failure="SWOT invented rather than sourced; peer set chosen by the model instead of the filing.",
    ),
    6: dict(
        scope="Stated strategy, growth drivers, management targets and how they compare to consensus.",
        source_buckets=["investor-day deck", "10-K MD&A outlook", "earnings-call prepared remarks"],
        structured_first=[
            "firecrawl:map IR site for latest investor-day deck",
            "foundation:MD&A outlook",
        ],
        web_queries=[
            "{co} investor day {fy}",
            "{co} long-term targets",
            "{co} capacity expansion",
            "{co} capex plan",
            "{co} production rate target {fy}",
            "{co} delivery OR output target by year",
        ],
        sub_questions=[
            "What strategy and growth drivers does management state, and in which document and on what date?",
            "Which dated production-rate, capacity, delivery or output targets has management set, and for which year?",
            "How do those targets compare with the consensus figures on the aggregator pages?",
            "Which targets were set, moved or withdrawn within the window, and when?",
        ],
        required_exhibit="Management targets vs. consensus table",
        common_failure="Quoting targets without the date they were set.",
    ),
    7: dict(
        scope="Acquisitions and divestitures: target, date, value, status, segment, rationale.",
        source_buckets=[
            "8-K Items 1.01/2.01",
            "10-K business-combinations note",
            "press releases",
            "M&A news",
        ],
        structured_first=[
            'edgar:fulltext "{co}" AND ("merger agreement" OR "acquisition") on 8-K',
            "foundation:business combinations note",
        ],
        web_queries=["{co} acquisitions list", "{co} acquires", "{co} divestiture"],
        required_exhibit="Acquisition table: target, announce date, value, status, segment, rationale",
        common_failure="Missing deal value; use Firecrawl /agent with a schema over press releases.",
    ),
    8: dict(
        scope="Officers, board, independence, pay, ownership concentration, litigation and regulatory matters.",
        source_buckets=[
            "DEF 14A",
            "SC 13D/G and Form 4",
            "regulator press releases",
            "litigation news",
        ],
        structured_first=["foundation:DEF 14A", "edgar:SC 13D/G and Form 4 from submissions"],
        web_queries=[
            "{co} CEO",
            "{co} union contract ratified OR strike {fy}",
            "{co} regulator OR court ruling OR settlement {fy}",
            "{co} CFO appointed",
            "{co} lawsuit OR investigation OR regulator {fy}",
            "{co} board independence",
            "{co} activist",
        ],
        sub_questions=[
            "Who are the officers and directors per the DEF 14A, and which appointments changed within the window?",
            "Which labour agreements were ratified, extended or struck over within the window, and on what date?",
            "Which regulator, court or settlement actions involving the company are dated within the window, and what did each decide?",
            "How concentrated is ownership, and does any holder's voting power differ from its beneficial ownership?",
        ],
        required_exhibit="Officer table; ownership concentration bullets",
        common_failure="Stale officer list; confusing beneficial ownership with voting power.",
    ),
    9: dict(
        scope="Three-to-four fiscal years of P&L, cash flow and balance sheet from XBRL, with derived FCF and margins labelled derived.",
        source_buckets=[
            "EDGAR XBRL companyfacts",
            "10-K statements",
            "news only to explain anomalies",
        ],
        structured_first=["edgar:companyfacts concepts.yaml x last 4 FY"],
        web_queries=["{co} {fy} charge OR impairment OR restructuring"],
        required_exhibit="P&L, cash-flow and balance-sheet tables (3-4 FY); FCF derived and labelled",
        common_failure="Concept-name drift across years; fiscal vs calendar years; non-GAAP replacing GAAP.",
    ),
    10: dict(
        scope="Quarterly operating performance versus consensus; KPIs as the company defines them; segment margins.",
        source_buckets=[
            "earnings releases and transcripts",
            "10-Q KPIs",
            "free aggregator pages (consensus, beat/miss)",
        ],
        structured_first=[
            "foundation:last 4 earnings releases and transcripts",
            "market_data:consensus and beat/miss history",
        ],
        web_queries=[
            "{co} {quarter} results vs expectations",
            "{co} KPI {metric}",
            "{co} guidance raised OR cut",
        ],
        required_exhibit="KPI vs consensus table (actual, consensus, variance); segment margin table",
        common_failure="One blogger's consensus treated as the Street; KPIs defined differently across quarters.",
    ),
    11: dict(
        scope="Debt, liquidity, ratings, maturities, capital returns; leverage with its definition.",
        source_buckets=[
            "10-Q debt note and liquidity section",
            "companyfacts debt and cash",
            "rating-agency press releases",
            "8-K financings",
        ],
        structured_first=["foundation:10-Q debt note", "edgar:companyfacts debt and cash"],
        web_queries=[
            "{co} credit rating Moody's OR S&P OR Fitch",
            "{co} notes offering {fy}",
            "{co} revolving credit facility",
            "{co} share repurchase authorization",
            "{co} dividend",
        ],
        required_exhibit="Debt and liquidity table (cash, total debt, net debt, leverage with definition); maturity list if disclosed",
        common_failure="Leverage quoted from three sources with three definitions and no label.",
    ),
    12: dict(
        scope="Bull and bear cases as debated publicly; rating changes with firm and date; unanswered questions.",
        source_buckets=[
            "earnings-call Q&A",
            "rating-change pages (MarketBeat, TipRanks)",
            "news coverage of analyst notes",
            "short reports",
        ],
        structured_first=["foundation:earnings-call Q&A", "market_data:rating changes"],
        web_queries=[
            "{co} bull case",
            "{co} bear case",
            "{co} analyst upgrade OR downgrade {fy}",
            "{co} analyst price target methodology",
            "{co} upgrade OR downgrade reason",
            "{co} short seller report",
            "{co} key debate",
        ],
        sub_questions=[
            "What did analysts ask on the last four earnings calls, and what did management answer in the same transcript?",
            "Which firms changed a rating or target within the window, and what reason or methodology does the news report for each?",
            "What are the bull and bear arguments as debated publicly, each tied to a source that names the firm or author?",
            "Which questions remain open after management's answers, and what evidence would settle them?",
        ],
        required_exhibit="Bull vs bear two-column table with management answers; analyst questions and answers list",
        common_failure="Fabricated broker attributions: a firm may be named only if a fetched source names it.",
    ),
    13: dict(
        scope="Derived multiples from XBRL and a free quote; consensus targets and ratings from aggregator pages; peer multiples.",
        source_buckets=[
            "companyfacts + quote (derived)",
            "free aggregator pages",
            "peer companyfacts",
        ],
        structured_first=[
            "market_data:quote",
            "edgar:shares, debt, cash, EBITDA components",
            "peers from section 5",
        ],
        web_queries=[
            "{co} price target",
            "{co} sum of the parts",
            "{co} valuation multiple vs peers",
        ],
        required_exhibit="Price-target range table; peer multiples table (LTM and NTM never mixed)",
        common_failure="Targets without dates or firm names; LTM and NTM in one table.",
    ),
    14: dict(
        scope="Dated event log for the trailing 12 months, newest first, one-line implication each.",
        source_buckets=["8-Ks", "IR newsroom", "DoD contract announcements", "news"],
        structured_first=["edgar:submissions 8-K trailing 12 months", "firecrawl:map IR newsroom"],
        web_queries=[
            "{co} news",
            "{co} announces",
            "{co} regulator approval",
            "{co} certification OR approval {fy}",
            "{co} deliveries {fy}",
        ],
        sub_questions=[
            "Which 8-K filings, releases and news items fall inside the trailing 12 months, newest first?",
            "Which certifications or regulator approvals were granted or withheld in the window, and on what date?",
            "What delivery or shipment figures did the company report in the window, by period?",
        ],
        required_exhibit="Dated event log, newest first, with one-line implication",
        common_failure="Recency not enforced; press-release language copied as analysis.",
    ),
}


def brief_for(
    section: int,
    co: str,
    fy: int | None = None,
    aliases: list[str] | None = None,
    sector_tag: str | None = None,
) -> Brief:
    """The §6 template for a section. Sector-specific queries and sub-questions (defense
    programs, pharma pipeline, ...) come from config/sources.yaml `sector_briefs` for
    `sector_tag`, appended after the generic ones, so the generic brief is sector-neutral."""
    t = _TEMPLATES[section]
    budget = section_budget(section)
    subs = {
        "co": co,
        "fy": fy or "",
        "peer": "{peer}",
        "program": "{program}",
        "quarter": "{quarter}",
        "metric": "{metric}",
    }
    extra = sector_brief_extras(sector_tag, section)
    queries = [q.format(**subs).strip() for q in [*t["web_queries"], *extra["web_queries"]]]
    sub_questions = [s.format(**subs) for s in t.get("sub_questions", [])] or [
        f"What does the primary evidence say about: {t['scope']}",
        f"What exhibit data supports: {t['required_exhibit']}",
        f"What is the most common failure to avoid: {t['common_failure']}",
    ]
    sub_questions += [s.format(**subs) for s in extra["sub_questions"]]
    return Brief(
        section=section,
        title=SECTION_TITLES[section],
        scope=t["scope"],
        source_buckets=t["source_buckets"],
        pages=float(budget.get("pages", 1.5)),
        sub_questions=sub_questions,
        structured_first=[s.format(**subs) for s in t["structured_first"]],
        web_queries=queries,
        required_exhibit=t["required_exhibit"],
        stop_condition=f"Stop when the first bucket ({t['source_buckets'][0]}) supports the exhibit and the scope; later buckets only fill gaps.",
        common_failure=t["common_failure"],
    )


def all_briefs(
    co: str,
    fy: int | None,
    sections: list[int],
    aliases: list[str] | None = None,
    sector_tag: str | None = None,
) -> dict[int, Brief]:
    return {s: brief_for(s, co, fy, aliases, sector_tag) for s in sections if s in _TEMPLATES}
