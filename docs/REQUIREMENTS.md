# Company Primer Agent — Requirements Document

Oct 21, 2026 · Ethan

## 1. Purpose and scope

The agent takes a company name (plus ticker, if public) and returns a 14-section company primer as a single `.mdx` file, with every factual claim cited to a fetched source. The target quality bar is the AlphaSense Company Primer (three examples reviewed: SpaceX, Lockheed Martin, Boeing, all dated Oct 21, 2026), rebuilt on public and licensable sources instead of AlphaSense's proprietary broker-research and expert-call corpus.

### In scope for v1

- US-listed public companies with SEC filings (the easy case: XBRL financials, 10-K/10-Q/8-K, proxies, earnings releases all free).
- Private and foreign companies on a degraded path: web research plus whatever structured data exists, with the Historical Financials, Capital Structure and Valuation sections explicitly marked as thin.
- Orchestration in LangGraph (Python), web retrieval via Firecrawl, structured financials via SEC EDGAR and US-government open-data APIs. v1 uses no paid data subscription; every place a paid service would do better is called out in the doc with the recommended service.
- Output: one `.mdx` file plus a machine-readable sources manifest (JSON) and a run log.

### Out of scope for v1

- The AlphaSense 14th section ("Internal Relationship Context" from CRM, Slack, email). Design the graph so it can be added as another section subgraph later.
- Proprietary sell-side research and expert-call transcripts. The agent reports what public coverage says about broker views and labels the gap; it does not pretend to have the notes.
- Interactive chat, follow-up questions, or portfolio-level runs. One company per invocation.
- A rendering site for the MDX. The file must compile under a standard MDX toolchain, but the site is a separate decision (see Open questions).

**Primary user:** an analyst or operator who wants a first-pass primer in under 30 minutes of wall-clock time and will read it critically, follow the citations, and edit. The primer is a research accelerator, not a published opinion, and the MDX should carry that disclaimer in its frontmatter.

## 2. Inputs, outputs, and the MDX contract

### Inputs (CLI args or a JSON request)

| Field | Required | Notes |
|---|---|---|
| `company_name` | yes | Free text; the entity-resolution step disambiguates ("Boeing" vs "Boeing Capital") |
| `ticker` | if public | Exchange-qualified when ambiguous (BA, BA.L) |
| `as_of_date` | no | Defaults to run date; stamps the frontmatter and bounds "recent" to the trailing 12 months |
| `sections` | no | Subset of 1–14 for partial reruns |
| `debug` | no | `false` by default. `true` pauses the graph for approval after the section plan and before export; the default runs straight through, as the AlphaSense agent does |

There is one search budget per section (standard); a deep mode is deferred until standard runs are measured.

### Outputs, written to `./output/{ticker-or-slug}/{run_id}/`

| File | Contents |
|---|---|
| `primer.mdx` | The primer, frontmatter + 14 H2 sections + Sources appendix |
| `sources.json` | Every cited source: id, type, publisher, title, URL, fetch timestamp, content hash, which sections cite it |
| `evidence/` | Raw fetched markdown per source (Firecrawl output) so citations are reproducible |
| `run.json` | Graph trace summary: queries issued, credits spent, tokens, per-section timings, coverage flags |

### The sections, in order

Page budget is what the AlphaSense research plan uses; "Research" says whether the section does its own research.

| # | Section | Pages | Research | Primary evidence |
|---|---|---|---|---|
| 1 | Key Takeaways | 1.0 | No — synthesized last from sections 2–14 | — |
| 2 | Company Overview | 1.5 | Yes | 10-K Item 1, IR site, segment note |
| 3 | Products/Services/Tech | 2.0 | Yes | 10-K Item 1, product pages, press releases, trade press |
| 4 | Key Programs & Contracts | 2.0 | Yes — structured first | USAspending awards, SAM.gov notices and entity records (CAGE/UEI, locations), DoD contract announcements, GAO/SAR program reports, budget justification books |
| 5 | Market/Competitive | 2.0 | Yes | 10-K Item 1A, competitor filings, trade press, analyst commentary in news |
| 6 | Strategy/Growth | 2.0 | Yes | Earnings calls, investor day decks, 10-K MD&A |
| 7 | Key Acquisition History | 1.0 | Yes | 8-K Item 1.01/2.01, press releases, M&A news |
| 8 | Mgmt/Governance | 1.0 | Yes | DEF 14A, 10-K Item 10, SC 13D/G, Form 4, litigation news |
| 9 | Historical Financial Performance | 1.5 | Yes — structured first | XBRL companyfacts (3–4 FY), 10-K statements |
| 10 | Operating Performance & Benchmarking | 2.0 | Yes | Earnings releases, KPIs in 10-Q, consensus from free aggregator pages via Firecrawl |
| 11 | Capital Structure & Liquidity | 1.5 | Yes | 10-Q debt note, credit-rating press releases, 8-K financings |
| 12 | Investment Thesis/Debates | 2.0 | Yes | Earnings-call Q&A, analyst rating news, short reports |
| 13 | Valuation | 1.5 | Yes — structured first | Multiples computed from XBRL plus a free quote; targets and ratings from free aggregator pages; peer set |
| 14 | Recent Developments | 1.0 | Yes | 8-Ks and press releases, trailing 12 months |

Section 4 is emitted for any company whose filings disclose US government revenue (the 10-K segment or concentration note says so); for a company with none it is emitted as a one-line coverage blockquote, so the numbering is stable across primers.

### MDX contract (so the file is useful without a custom renderer)

- Keep it simple: the file is plain GitHub-flavored markdown with a YAML frontmatter block and the `.mdx` extension. No import lines, no JSX, no custom components in v1, so it renders anywhere markdown renders and still compiles as MDX.
- YAML frontmatter: `company`, `ticker`, `cik`, `exchange`, `is_public`, `as_of`, `run_id`, `generator_version`, `coverage` (per-section `full | partial | unavailable | not_applicable`), `source_limits` (a list; v1 always includes "No sell-side research or expert-call transcripts were available; broker views are second-hand from news and rating aggregators" and "Market data scraped from public aggregator pages, not a licensed feed"), `disclaimer`.
- One `##` per section, numbered as above, in order. No heading-id syntax (`{#sec-9}` is MDX-renderer-specific); cross-references are by section number in the text.
- Citations as footnote markers `[^12]` inline and a `## Sources` appendix that mirrors `sources.json` in the AlphaSense citation style: `[12] 10-Q • Lockheed Martin Corp • 23 Jul 26 • "Quarterly Report Q2 2026" • <url>`.
- Each section opens with an upfront caveat line when applicable (the examples all do this: listing status, ownership concentration, data gaps).
- Coverage and conflict notes are blockquotes, not components: `> Coverage: partial — no earnings-call transcript was found for Q2.` and `> Conflict: 737 build rate reported as 42/month [^8] and 47/month [^11]; sources differ.`
- A section the agent cannot support with sources is emitted with the heading and a one-line coverage blockquote, never silently dropped. This matches the examples, where AlphaSense writes "I do not have enough information" rather than inventing.

## 3. What the AlphaSense examples reveal

The three primers (SpaceX 42 pp., Lockheed Martin 39 pp., Boeing 34 pp.) share one editorial pattern and one sourcing pattern, and both are reproducible.

**Editorial pattern.** Every section follows the same shape: an opening claim with a caveat, then subsections, then a run of named-broker bullets ("RBC Capital Markets Perspective: …", "Jefferies Research View: …") that present the same fact from two to five houses. Every sentence carries a numeric citation. Tables appear at fixed points: a 3–4 fiscal-year P&L / cash-flow / balance-sheet table in section 8, an acquisition table (target, date, value, status, rationale) in section 6, a KPI-vs-consensus table in section 9, a broker price-target-and-multiples table in section 12, a SWOT table in section 4. The Key Takeaways section is written last with no research, as a synthesis.

**Sourcing pattern.** Counting the citation lists across the three documents gives roughly this mix:

| Source type | Share of citations | Public substitute available? |
|---|---|---|
| Broker research (RBC, Jefferies, UBS, Morgan Stanley, Evercore, Bernstein, etc.) | ~45% | Partial: analyst views as reported in news, rating/target aggregators, earnings-call Q&A |
| SEC filings (10-K, 10-Q, 8-K, DEF 14A, SC 13G, prospectus, annual report) | ~15% | Yes, free via EDGAR |
| Press releases and earnings releases (INTRM) | ~12% | Yes, company IR sites and wire services |
| News and trade press (Seattle Times, Benzinga, Jane's, AirGuide, regional papers) | ~15% | Yes, via Firecrawl search; some paywalled |
| Expert-call transcripts (AlphaSense/Tegus) | ~8% | No. Nearest proxies: earnings-call Q&A, conference transcripts, trade-press interviews |
| Structured screeners (FactSet M&A screener, Findata, Canalyst) | ~5% | Yes, via EDGAR XBRL; multiples computed in-agent from a free quote |

The research plan that generated the SpaceX primer shows the method: each section gets a brief that names the allowed source buckets ("Using ONLY Broker Research and Expert Transcripts"), a page estimate, and a research flag. Each brief is then expanded into three sub-questions per source bucket, run in parallel, and the hits are read before writing. That is the pattern the LangGraph design below copies.

Two things the examples get wrong that the build should avoid. First, numbers drift between sections: the Boeing primer states the 737 line is stabilizing at 47 per month (RBC, Bernstein) and at 42 per month (Jefferies) in adjacent paragraphs with no reconciliation, and the Lockheed primer reports net leverage as 1.2x, 1.6x and ~2.0x from three houses without noting the definitions differ; the SpaceX primer quotes two different Musk ownership figures (48.4% beneficial, ~82% voting) without saying they measure different things. A numeric-consistency check is a requirement, not a nice-to-have. Second, the broker-bullet format can present four restatements of one fact as four pieces of evidence. The agent should deduplicate claims and count distinct sources, not citations.

## 4. Data sources

Three tiers: structured APIs for anything numeric, Firecrawl for everything on the open web, and, for each gap the free sources cannot close well, the paid service to swap in later. v1 uses nothing that needs a paid subscription. The agent should prefer tier 1 over tier 2 for any number, and record which tier each citation came from so the paid swaps are mechanical: each free source sits behind one adapter interface, and the upgrade is a new adapter, not a new section.

### Tier 1 — Structured, free, authoritative (public companies)

| Source | What it gives the primer | Access | Constraints |
|---|---|---|---|
| SEC EDGAR Submissions API `data.sec.gov/submissions/CIK##########.json` | Full filing index per company: form type, filing and report dates, accession numbers, document URLs | No key; descriptive User-Agent with contact email is mandatory | 10 req/s fair-access cap; ~1,000 most recent filings in the main array |
| EDGAR XBRL companyfacts / companyconcept / frames | Every reported financial fact (revenue, operating income, capex, debt, shares) with period and accession — the backbone of sections 9, 11, 13 | Same | Facts span us-gaap, ifrs-full, dei; large filers return 50k+ facts, so filter by concept list and fiscal period; segment data is often not in XBRL and must come from the 10-K text |
| EDGAR full-text search `efts.sec.gov` | Keyword search across all filings since 2001 (acquisitions, litigation, named executives, covenants) | Same | 10,000-hit cap per query; narrow by form and date |
| `sec.gov/files/company_tickers.json` | Ticker → CIK resolution | Same | Refreshed daily; private companies absent |
| Filing documents (10-K, 10-Q, 8-K, DEF 14A, S-1, SC 13D/G, Form 4) | Narrative: business description, risk factors, MD&A, segment notes, debt notes, executive bios, ownership | Fetch the primary document URL from Submissions; parse with Firecrawl /scrape or /parse (PDF/HTML) | Inline XBRL HTML is large; chunk by Item heading |

### Tier 1 — Government contracting and program data (public, authoritative)

These feed section 4 (Key Programs & Contracts) and the facilities/location view. All are US-government public data, so there is no copyright question, but each has its own access rules.

| Source | What it gives the primer | Access | Constraints |
|---|---|---|---|
| USAspending.gov API v2 — `POST /api/v2/search/spending_by_award/`, `GET /api/v2/awards/{id}/`, recipient profiles, spending_by_category | Every prime award to the company since FY2008: award ID (PIID), obligated and potential value, awarding sub-agency, NAICS/PSC, period of performance, place of performance, description text (program names such as "F-35" or "Sentinel" appear here); recipient roll-ups by agency and year | No key, no login | DoD contract actions publish roughly 90 days late; prime awards only (subcontract work on another prime's program is invisible); recipient names are inconsistent, so resolve by UEI/DUNS and parent recipient, not by name |
| SAM.gov Get Opportunities Public API v2 — `api.sam.gov/opportunities/v2/search` | Solicitations, sources-sought, presolicitations, special notices and award notices that name the program and the awardee; each notice has a stable uiLink for citation | Free personal API key from the SAM.gov Account Details page (issued via api.data.gov) | postedFrom/postedTo are required and the window cannot exceed one year; daily quotas on personal keys are small (reports range from ~10 to ~40 calls/day; system accounts ~1,000), so budget calls per run and cache aggressively; notice descriptions sit behind a secondary URL |
| SAM.gov Entity Management API v3 — `api.sam.gov/entity-information/v3/entities` | The company's registrations: legal business name, UEI, CAGE code, physical and mailing address, registration status, NAICS codes, parent/immediate-parent UEI. One registration per registered location, so walking the parent UEI yields the facility list with addresses | Same key | Same quota family; some fields are restricted to the registrant; a large prime has hundreds of registrations, so page and cache |
| DLA CAGE search | CAGE → facility name and address, including legacy and foreign (NCAGE) codes not in SAM.gov | Web, Firecrawl /scrape | Interactive site; use as a cross-check for CAGEs found in awards, not as the primary enumerator |
| 10-K Item 2 (Properties) and the segment note | The company's own list of major facilities and government-revenue concentration | EDGAR, from the foundation pack | Cross-check for the CAGE-derived location table; the filing is authoritative for "major", SAM.gov is authoritative for "registered" |
| DoD daily contract announcements | Every DoD award over the reporting threshold, same day, with contract number, value, program description, work locations and completion date | Web, Firecrawl /search + /scrape; also an RSS feed | The fastest public source for new awards (no 90-day lag); text only, so parse to a schema with /agent and key each row by contract number so it joins to USAspending later |
| GAO reports — Weapon Systems Annual Assessment and program-specific reports | Cost, schedule and performance status of major programs (F-35 sustainment, Sentinel Nunn-McCurdy breach, etc.), with the official program baseline | Web, PDFs via Firecrawl /parse; GAO also has a public reports API | Program-level, not company-level; the agent must map program → prime contractor from the award data |
| DoD Selected Acquisition Reports (SARs) and budget justification books (comptroller.defense.gov) | Program of record quantities, unit costs, funding by fiscal year (P-1/R-1 exhibits, J-books) | PDFs, Firecrawl /parse | Large documents; extract only the line items for the programs identified from awards |
| Congressional Research Service reports (crsreports.congress.gov) and DOT&E annual report | Plain-language program background, issues for Congress, test status | Web | Secondary to GAO/SAR but excellent for the narrative paragraph |

How these fit together for a program like the F-35 or Sentinel: USAspending identifies the awards and their values and ties them to the company by UEI; a DoD contract announcement gives the same-day detail; a SAM.gov award notice gives a citable notice ID; GAO and the SAR give the program's status and baseline; the budget books give the forward funding. The agent writes the program row only when at least one government primary source supports each number in it.

### Tier 2 — Open web via Firecrawl

| Firecrawl endpoint | Use in this agent | Cost (per Firecrawl pricing pages, verify at build) |
|---|---|---|
| `/search` | Primary discovery tool: query → ranked results with excerpts, optional full-page markdown in the same call | 2 credits per 10 results, +1 per page scraped |
| `/scrape` | Known URLs: IR pages, press releases, filing documents, news articles; returns markdown or schema-typed JSON | 1 credit per page |
| `/parse` | PDFs and decks: investor-day presentations, annual reports, rating-agency reports | Per page; newer endpoint, confirm limits |
| `/map` + `/crawl` | Enumerate a company's IR or newsroom section once, then scrape selectively | Crawl is credit-heavy; use map to pick URLs |
| `/agent` | Open-ended structured extraction without URLs (e.g. "list all acquisitions by X since 2015 with date and value") — the replacement for the deprecated /extract; takes a Pydantic schema | Variable; use for tables, not prose |

Firecrawl ships a Python SDK, a LangChain integration, and an MCP server exposing 12 tools, so the LangGraph nodes can call it directly as tools. The free tier (1,000 credits) is enough to prototype one primer; a standard run will need roughly 300–600 credits (estimate, to be measured in milestone 1).

What Firecrawl should be pointed at, by section:

| Content | Where it lives on the open web | Notes |
|---|---|---|
| Earnings-call transcripts | Motley Fool transcripts, company IR webcasts (often PDF), Seeking Alpha (paywalled), Benzinga, Quartr | The Q&A is the best public proxy for "what the sell-side is debating"; the analyst's firm is named before each question |
| Analyst ratings, price targets, rating changes | MarketBeat, TipRanks, Benzinga Analyst Ratings, Barron's/Reuters coverage of notes | These report what RBC/Jefferies/UBS said and the number; they are secondary sources and must be cited as such |
| Investor presentations, investor-day decks | Company IR site | Best source for strategy and KPI definitions (sections 6, 10) |
| Credit ratings and rating rationales | Moody's, S&P and Fitch press releases (free summaries), company 8-Ks | Section 11 |
| M&A history | Press releases, 8-K Items 1.01/2.01, Reuters/Bloomberg coverage, Wikipedia as an index only | Section 7; values and multiples cross-checked to filings |
| Trade press | Sector-specific (defense: Breaking Defense, Defense News, Jane's; aerospace: Aviation Week, Flight Global) | The sector list should be a config table, not hard-coded |
| Governance and litigation | DEF 14A, court-filing coverage, regulator press releases (FAA, FCC, DoD) | Section 8 |
| Competitors' filings | Same EDGAR path for each named peer | Section 5 benchmarking |

### Tier 2 — Free market-data pages (the v1 substitute for a market-data API)

Sections 10 and 13 need prices, consensus estimates, price targets and ratings. In v1 these come from public pages scraped with Firecrawl, behind one `MarketDataAdapter` interface so the paid swap below is a one-file change. Every number from these pages is cited to the page and dated; the agent never averages across two aggregators.

| Page | What it gives | How | Caveat |
|---|---|---|---|
| Yahoo Finance quote, statistics and analysis tabs (`finance.yahoo.com/quote/{tk}/analysis`) | Price, shares, market cap, LTM and forward P/E, consensus EPS and revenue (current and next FY), mean/high/low target, recommendation trend, earnings beat/miss history | Firecrawl /scrape with JSON schema | Accepted for v1 with eyes open: Yahoo's terms of use restrict automated access and the layout changes often, so this is the first paid swap (FMP) and the frontmatter's `source_limits` says the data came from scraped aggregator pages |
| StockAnalysis.com (`/stocks/{tk}/forecast/`, `/statistics/`, `/financials/`) | Clean tables for forecasts, ratios, multiples and statements; peer lists | /scrape, tables parse well | Secondary source for statements (XBRL stays primary); forecast coverage thinner on small caps |
| MarketBeat analyst-ratings pages | Dated rating changes and target changes with the broker name — the public proxy for "RBC moved to $225" | /scrape | Secondary; cite as "as reported by MarketBeat" |
| TipRanks public pages | Rating counts, consensus target, named-analyst targets (partial without login) | /scrape | Most detail is behind login; use only what renders |
| Finviz quote page | Snapshot multiples (P/E, EV/EBITDA, P/FCF), short interest, insider transactions summary | /scrape | Snapshot only, no history |
| Stooq daily CSV (`stooq.com/q/d/l/?s={tk}.us`) or Yahoo chart endpoint | Price history for the as-of date and 52-week range | Direct GET or /scrape | Keep a fallback; use the 10-K share count from XBRL, not the page's, for EV |
| Company IR "Analyst coverage" page | The list of covering firms and analysts, which bounds who may be attributed in section 12 | /scrape | Not every company publishes one |

Derived multiples (EV, EV/EBITDA, P/FCF) are computed in-agent from XBRL (shares, debt, cash, EBITDA components) and the price above, and the MDX labels them derived with the formula in the Sources appendix, so a reviewer can see that no aggregator's definition was silently adopted.

### Paid upgrade path — recommended, not in v1

Each row is a gap the free sources only partly close. The left columns describe what v1 does; the right columns are the recommended swap and the trigger for making it. These are callouts for the build plan, not v1 dependencies.

| Gap | v1 free method | Weakness of the free method | Recommended paid swap | Trigger to upgrade |
|---|---|---|---|---|
| Consensus estimates, price targets, rating history (sections 10, 12, 13) | Yahoo Finance, StockAnalysis, MarketBeat pages via Firecrawl | Terms-of-use exposure; brittle layouts; no history of consensus revisions; no analyst-level detail | Financial Modeling Prep (consensus, target summary, ratings history, standardized statements, transcripts on higher tiers); alternatives Intrinio, Polygon.io | First production run, or the first time a scraper breaks mid-run |
| Earnings-call transcripts (foundation pack, sections 6, 10, 12) | Motley Fool and company IR webcast PDFs via Firecrawl | Coverage gaps on smaller companies; late posting; PDFs need /parse | FMP transcript endpoint, or Quartr / AlphaSense transcript API | More than ~20% of runs miss a transcript |
| Sell-side research and expert calls (sections 5, 12, 13) | Analyst views as reported in news and rating aggregators; earnings-call Q&A | Second-hand; no price-target rationale; no SOTP detail like the examples | AlphaSense (broker research + expert transcripts), Tegus (expert calls), Visible Alpha (consensus KPIs) | Only if the primer must match the AlphaSense examples claim-for-claim |
| M&A deal terms and multiples (section 7) | 8-Ks and press releases via EDGAR and Firecrawl; /agent to tabulate | Undisclosed values stay undisclosed; no implied multiples | FactSet or S&P Capital IQ M&A screeners; PitchBook for private targets | When deal multiples become a required column |
| Private-company financials and cap tables (sections 9, 11, 13 on the private path) | Form D, news, competitor filings | Mostly unavailable | PitchBook, Crunchbase Pro | When private companies are a regular input |
| Government contracting at volume (section 4) | SAM.gov personal API key; USAspending; DoD announcements | SAM.gov daily quota of tens of calls | SAM.gov system account (free, higher quota, bulk extracts); GovWin IQ or Bloomberg Government for forecasts and competitive intel | More than a few primers per day, or pre-award pipeline questions |
| Peer multiples at scale (section 13) | Repeat the XBRL-plus-quote computation per peer | Slow for large peer sets; non-US peers lack XBRL | FMP or Polygon fundamentals for the peer set | Peer sets over ~8 names or with foreign peers |
| Search and page retrieval | Firecrawl free tier (1,000 credits) | Not enough for more than one or two primers | Firecrawl paid plan (Hobby and up) — the one paid service worth turning on first | Before the second primer |

**Private companies.** No ticker means no EDGAR XBRL. The degraded path uses: Form D filings (private placements) via EDGAR full-text search, state corporate registries, Crunchbase/PitchBook (paid) or their public snippets, press releases, trade press, and competitor 10-Ks that name the company. Sections 9, 11 and 13 are emitted as partial or unavailable with a note on what was found.

## 5. Architecture

Research is parallel per section and writing is serial, which is the pattern LangChain settled on for Open Deep Research after finding that sub-agents writing sections in parallel produced incoherent reports. Here the 13 researched sections map one-to-one onto researcher subgraphs fanned out with LangGraph's `Send`, each returning compressed, cited findings; a final stage writes Key Takeaways from those findings and assembles the MDX.

The top row runs once per company, the middle box runs once per section in parallel, and the bottom row runs once per primer; a validation failure sends one section back through the middle box, not the whole run.

### Nodes in the main graph

1. **intake** — validate inputs, create `run_id`, set `as_of`.
2. **resolve_entity** — ticker → CIK via `company_tickers.json`; name-only input → Firecrawl search plus EDGAR full-text to find the legal entity; sets `is_public`, exchange, fiscal-year end, and a list of known subsidiaries and former names for later query expansion. Ambiguity (two plausible entities) raises an interrupt for the user.
3. **foundation_pack** — fetch once, share with every researcher: latest 10-K, latest two 10-Qs, latest DEF 14A, last 4 earnings releases and transcripts, the IR presentation index, companyfacts filtered to ~40 concepts, and the Submissions index. Chunk by Item heading and store on disk under the run folder (one markdown file per fetched item under `evidence/` plus a JSON index keyed by source id; no database and no vector index in v1, since a 13-researcher run only needs keyword lookup over a few hundred chunks) with source ids assigned at ingest. This step is what keeps 13 researchers from scraping the same 10-K thirteen times.
4. **plan_sections** — one LLM call produces a research brief per section in the AlphaSense style: scope, allowed source buckets, page budget, 3–5 sub-questions, and the structured-data calls to make first. The plan is written into state and into `run.json` so a reviewer can see it before the fan-out (with `debug: true` the graph pauses here on an interrupt for plan approval; the default runs straight through).
5. **fan_out** — a conditional edge returning one `Send("section_researcher", {brief, foundation_refs})` per section in the request.
6. **section_researcher** (subgraph, below).
7. **key_takeaways** — synthesizes up to 7 bullets from the 13 section drafts; no tool calls, matching the example plan.
8. **assemble** — renders frontmatter, sections in order, footnotes, and the Sources appendix; writes `sources.json`.
9. **validate** — citation integrity (every `[^n]` resolves to a fetched source; every source is cited), MDX compiles, numeric-consistency pass (same metric quoted in two sections must match or be flagged), coverage flags set. Failures route back to the offending section with the critic's notes, bounded to two retries.
10. **review** — in `debug: true` runs only, an interrupt with the checkpointer so a human can approve or annotate before export; otherwise a pass-through.
11. **export** — write files; emit run summary.

### Section researcher subgraph (same graph compiled 13 times with different briefs)

- **plan_queries** — turns the brief's sub-questions into concrete tool calls: EDGAR/API calls first for anything numeric, then 3–8 Firecrawl searches with source-bucket hints (`site:` style constraints are not reliable in Firecrawl search, so hints go in the query text and in a post-filter on domain).
- **call_tools** — ReAct loop over the tool set; every fetched page is registered in the evidence store with a stable `source_id` before the model sees it, so citations can only point at real fetches.
- **compress_and_draft** — writes the section in the house style (caveat line, subsections, named-source perspective bullets, the section's required table), citing only `source_id`s. Returns the draft plus a claims list (claim, value, unit, period, source_ids).
- **critic** — a separate model call with a rubric: coverage of the brief, citation density, one-fact-many-citations collapse, numbers traceable to tier-1 where available, page budget. Loops back to plan_queries with targeted gaps, max 2 iterations, then returns.

**State** (`PrimerState`, TypedDict): inputs; entity (cik, exchange, is_public, fy_end, aliases); plan: `dict[section_id, Brief]`; evidence_refs (append-only reducer, `operator.add`); drafts: `dict[section_id, SectionDraft]` (dict-merge reducer so parallel branches do not clobber); claims (append reducer, feeds the numeric check); coverage: `dict[section_id, status]`; errors. Each `Send` carries only its brief and the foundation-pack handles, not the whole state, so researcher context stays small.

**Model roles.** Use a strong model for plan_sections, compress_and_draft and key_takeaways; a cheaper model for plan_queries and the ReAct tool loop; a strong model for critic (it must catch what the drafter missed). Make every role configurable; LangChain's `init_chat_model` keeps this provider-agnostic.

**Alternative worth evaluating in milestone 1:** LangChain's deepagents library (planning tool, filesystem scratchpad, subagents, MIT) is built on LangGraph and would replace the hand-built subgraph with `create_deep_agent(subagents=[...])`. It trades explicit control of the graph for less code. The hand-built graph is the default because the section contract, citation registry and validation gate are easier to enforce as explicit nodes.

**Persistence and observability.** A SQLite checkpointer file in the run folder (`langgraph-checkpoint-sqlite`) so a failed run resumes at the failed section rather than restarting; Postgres is the swap if runs ever share a host. No evidence is shared across runs in v1: each primer fetches its own foundation pack, even for companies that cite each other's filings. LangSmith or OpenTelemetry tracing on by default; per-node token and credit counters written to `run.json`.

## 6. Section research plans

Each brief below is what plan_sections should emit, written in the same form as the AlphaSense research plan: scope, the structured call to make first, the web queries, the required exhibit, and the thing that most often goes wrong. Query strings are templates; `{co}` is the resolved company name, `{tk}` the ticker, `{fy}` the latest fiscal year.

| # | Section | Structured first | Web queries (Firecrawl /search, then /scrape the top hits) | Required exhibit | Common failure |
|---|---|---|---|---|---|
| 2 | Company Overview | 10-K Item 1 and segment note from the foundation pack; dei facts for HQ, FY end, shares | `{co} business overview`, `{co} segments revenue breakdown {fy}`, `{co} company history founded` | Segment revenue table (3 FY) | Writing from memory instead of the 10-K; missing the listing/ownership caveat the examples open with |
| 3 | Products/Services/Tech | 10-K Item 1 product list | `{co} product roadmap`, `{co} new product launch {fy}`, `{co} technology differentiation`, sector trade press by product line | Product/program table with status and key metric | Marketing copy cited as fact; no competitor comparison |
| 4 | Key Programs & Contracts | (a) SAM.gov Entity API: active registrations under the parent UEI, kept only if the CAGE or its address appears as a place of performance on an award in the last five fiscal years → CAGE, address, status; (b) USAspending spending_by_award by recipient UEI, FY-5 to date, sorted by potential value; group by description keywords and PSC to surface programs; (c) USAspending spending_by_category by awarding sub-agency and by FY; (d) SAM.gov Opportunities: award notices and sources-sought naming the top programs, trailing 12 months; (e) DoD contract announcements for the same programs | `{program} GAO report`, `{program} selected acquisition report`, `{program} budget justification {fy}`, `{program} Nunn-McCurdy OR rebaseline OR schedule`, `{co} {program} contract award`, `{co} facility {city} CAGE` | Program table: program, customer, role (prime/sub), contract vehicle and PIID, ceiling and obligated value, period of performance, status and latest GAO/SAR finding; a locations table (active registrations with award activity in the last five FYs): CAGE, facility name, city/state, registration expiry, programs performed there; agency-mix table by FY | Program names inferred from award descriptions without a confirming source; subcontract roles asserted from press alone; a real facility dropped because its registration lapsed or no award names it as place of performance; the table caption states the active-and-five-year filter so the omission is understood; values mixing obligated with ceiling |
| 5 | Market/Competitive | 10-K Item 1A risk factors; peer tickers from the 10-K's own competitor list | `{co} market share`, `{co} competitors`, `{co} vs {peer}`, `{co} moat analysis`, each peer's 10-K Item 1 | SWOT table; peer list with one-line positioning | SWOT invented rather than sourced; peer set chosen by the model instead of by the filing |
| 6 | Strategy/Growth | Latest investor-day deck via IR site /map; MD&A outlook paragraph | `{co} investor day {fy}`, `{co} long-term targets`, `{co} capacity expansion`, `{co} capex plan`, earnings-call prepared remarks | Management targets vs. consensus table | Quoting targets without the date they were set |
| 7 | Acquisition History | EDGAR full-text: `"{co}" AND ("merger agreement" OR "acquisition")` on 8-K; 10-K business-combinations note | `{co} acquisitions list`, `{co} acquires` per year, `{co} divestiture` | Table: target, announce date, value, status, segment, rationale | Missing deal value or multiple; use Firecrawl /agent with a Pydantic schema over the press releases to fill the table |
| 8 | Mgmt/Governance | DEF 14A (board, independence, pay, ownership table); SC 13D/G and Form 4 from Submissions | `{co} CEO`, `{co} CFO appointed`, `{co} board independence`, `{co} lawsuit OR investigation OR regulator {fy}`, `{co} activist` | Officer table; ownership concentration bullets | Stale officer list; confusing beneficial ownership with voting power (the SpaceX example does this) |
| 9 | Historical Financials | companyfacts for ~40 concepts × last 4 FY: Revenues, CostOfRevenue, GrossProfit, OperatingIncomeLoss, NetIncomeLoss, EPS diluted, NetCashProvidedByOperatingActivities, PaymentsToAcquirePPE, Cash, LongTermDebt, StockholdersEquity, shares | Only to explain anomalies: `{co} {fy} charge OR impairment OR restructuring` | P&L, cash-flow and balance-sheet tables (3–4 FY), FCF derived and labelled as derived | Concept-name drift across years (Revenues vs RevenueFromContractWithCustomer…); mixing fiscal and calendar years; non-GAAP figures from releases silently replacing GAAP |
| 10 | Operating Performance & Benchmarking | Last 4 earnings releases and transcripts from the pack; consensus EPS/revenue and the beat/miss history from free aggregator pages (Yahoo Finance analysis tab, StockAnalysis) scraped with Firecrawl | `{co} {quarter} results vs expectations`, `{co} KPI {metric}` for the KPIs named in the release, `{co} guidance raised OR cut` | KPI vs. consensus table (actual, consensus, variance); segment margin table | Treating one blogger's "consensus" as the Street; KPIs defined differently across quarters |
| 11 | Capital Structure & Liquidity | 10-Q debt note and liquidity section; companyfacts debt and cash; credit-rating press releases | `{co} credit rating Moody's OR S&P OR Fitch`, `{co} notes offering {fy}`, `{co} revolving credit facility`, `{co} share repurchase authorization`, `{co} dividend` | Debt and liquidity table (cash, total debt, net debt, leverage with definition); maturity list if disclosed | Leverage quoted from three sources with three definitions and no label |
| 12 | Investment Thesis/Debates | Earnings-call Q&A from the pack (analyst firm is named per question); rating-change history from MarketBeat and TipRanks public pages scraped with Firecrawl | `{co} bull case`, `{co} bear case`, `{co} analyst upgrade OR downgrade {fy}`, `{co} short seller report`, `{co} key debate` | Bull vs. bear two-column table; "unanswered questions" list | Fabricated broker attributions. Rule: a view may be attributed to a named firm only if a fetched source names that firm |
| 13 | Valuation | Computed in-agent: shares and debt from XBRL, price from a free quote page, so market cap, EV, EV/EBITDA and P/E (LTM) are derived and labelled derived; NTM multiples, consensus target high/low/median and rating counts from free aggregator pages; same computation for the peer set from section 5 | `{co} price target`, `{co} sum of the parts`, `{co} valuation multiple vs peers`; rating aggregator pages | Price-target range table; peer multiples table | Targets without dates or firm names; mixing LTM and NTM in one table |
| 14 | Recent Developments | Submissions index filtered to 8-K in the trailing 12 months; IR newsroom via /map; new DoD contract announcements since the last run | `{co} news` with recency where the search API supports it, `{co} announces`, `{co} regulator approval`, per-event follow-ups | Dated event log, newest first, with one-line implication | Recency not enforced; press-release language copied as analysis |
| 1 | Key Takeaways | None | None | Up to 7 bullets, each pointing at the section it summarizes | Written before the sections exist; introduces claims not in the body |

Two cross-cutting rules the plans should encode. Every query batch runs with the company's aliases (former names, major subsidiaries, brand names) appended by resolve_entity, since trade press rarely uses the legal name. And every section's brief states its source buckets in priority order so plan_queries knows when to stop: a section 9 draft built on XBRL does not need five news searches.

## 7. Citation, provenance and quality control

The single hard rule: **the model never writes a citation it did not fetch.** Every page or API response is registered in the evidence store before the model sees it, gets a `source_id`, and the drafter cites by id. The assembler converts ids to `[^n]` footnotes, so a citation the validator cannot resolve is a bug, not a hallucination to be caught later.

### Source record (one row per fetched item, in `sources.json`)

| Field | Example | Purpose |
|---|---|---|
| `source_id` | `src_0042` | Stable handle for the drafter |
| `type` | 10-Q, 8-K, DEF14A, press_release, earnings_call, news, trade_press, rating_agency, api:fmp, api:edgar_xbrl | Drives the citation label and the tier rule |
| `tier` | 1, 2, 3 | Section 4's tiers; numbers must trace to tier 1 or 3 where either exists |
| `publisher`, `title`, `published_date`, `url` | | Rendered in the Sources appendix in the AlphaSense style |
| `fetched_at`, `content_hash`, `evidence_path` | | Reproducibility; the raw markdown is kept under `evidence/` |
| `cited_by` | `["sec-4", "sec-9"]` | Filled by the assembler; unused sources are dropped from the appendix |

### Validation gate (validate node, all must pass or the run is marked `needs_review`)

1. Every `[^n]` in the MDX resolves to a source record; every source record in the appendix is cited at least once.
2. The MDX compiles with `@mdx-js/mdx` (run via a Node sidecar or mdx-py); no raw JSX in prose; frontmatter parses.
3. Numeric consistency: the claims list is grouped by (metric, period); any group with two values that differ by more than a tolerance is flagged in a conflict blockquote inside the later section, not silently reconciled. The examples show why: 42 vs 47 aircraft per month, 1.2x vs 2.0x leverage.
4. Tier rule: a financial statement number cited only to a tier-2 news page when companyfacts has the concept is flagged.
5. Attribution rule: any sentence of the form "Firm notes/argues/estimates…" must cite a source whose text names that firm. This is the most important guard because the examples are built on broker attributions and the public web only has them second-hand.
6. Recency: section 14 cites nothing older than `as_of` minus 12 months; section 13 targets carry a date.
7. Coverage: each section's coverage flag is `full` (brief satisfied, required exhibit present), `partial`, or `unavailable`, and the frontmatter aggregates them.
8. Page budget: a section more than 50% over its page estimate goes back to the critic once with a cut instruction.

### Program and location sourcing rule

Section 4 is held to a stricter standard than the rest of the primer because its claims are checkable against government records:

- Every contract value, ceiling, obligation, period of performance and awarding agency cites a government primary record by identifier: a USAspending `generated_unique_award_id` or PIID, a SAM.gov notice ID, a DoD contract-announcement date and contract number, or a GAO report number. A press release or news article may add context but cannot be the only source for a number.
- Obligated, ceiling (potential) and announced values are three different numbers; the table carries the one it means and the source type says which (USAspending for obligated and potential; DoD announcement for announced).
- Program status language ("on schedule", "Nunn-McCurdy breach", "rebaselined") cites GAO, a SAR, or a DoD statement, with its date; trade-press paraphrases are not enough.
- A prime/sub role is stated only when an award record or a filing names the company in that role; otherwise the row says `role: not confirmed`.
- Every location row cites the SAM.gov entity record that carries the CAGE (registration status and expiration date included) and, where available, the 10-K Item 2 line that names the facility; a location that appears only in a news story is listed separately as unverified.
- Entity resolution is by UEI and CAGE, never by name: the recipient-name field on USAspending is free text and a prime's subsidiaries register separately. The resolved UEI set is written into `sources.json` so a reviewer can see what was treated as "the company".
- Source records for these items carry the government identifier in a dedicated field (`gov_id`) so the validator can check it against the URL, and the MDX footnote prints it: `[27] USAspending • CONT_AWD_… • FY2026 • "Sentinel ICBM — EMD" • <url>`.

### Critic rubric (per section, scored 1–5, written into `run.json`)

Brief coverage; citation density (target: every sentence with a fact has one); distinct-source count (not citation count); exhibit present and populated; style conformance (caveat line, perspective bullets, no first person); forbidden content (no investment recommendation, no price prediction stated as the agent's own view).

### Evaluation harness (built in milestone 1, used every milestone after)

The three AlphaSense primers become golden references. For Lockheed Martin and Boeing, score the agent's output on fact overlap (does it find the same deals, officers, contracts, numbers?), citation resolvability, and a rubric pass by a judge model. SpaceX is the private-to-public edge case. Add two more tickers outside aerospace to avoid overfitting the sector trade-press list.

## 8. Non-functional requirements

| Requirement | Target | How |
|---|---|---|
| Wall-clock time, standard depth | ≤ 30 min per primer | 12 researchers in parallel; foundation pack fetched once; cheaper model for tool loops |
| Cost per primer | Measure in milestone 1; target under $10 in model tokens plus ~300–600 Firecrawl credits | Per-node counters in `run.json`; a hard budget in config that stops a section at its cap and marks it partial |
| SEC fair access | ≤ 10 req/s to sec.gov and data.sec.gov, descriptive User-Agent with email | One shared rate-limited EDGAR client; missing User-Agent gets a 403 and can get the IP blocked |
| Firecrawl concurrency | Stay under the plan's concurrent-request limit | A semaphore shared across researchers; retries with backoff on 429 |
| Caching | Same company within 24 h reuses the foundation pack and all fetched pages | Content-hash cache keyed by URL + date; companyfacts cached per CIK per day |
| Resumability | A failed section does not restart the run | LangGraph SQLite checkpointer stored in the run folder |
| Reproducibility | A run can be replayed from `evidence/` without network | Drafter reads only from the evidence store; a `--offline` flag replays |
| Secrets | API keys from environment or a secrets manager, never in config files or logs | `.env` locally; redact keys in `run.json` |
| Paywalls and terms | Do not bypass paywalls or CAPTCHAs; respect robots.txt where Firecrawl exposes it | A domain allow/deny list in config; paywalled hits are cited by headline and URL only, marked `access: limited` |
| Logging | Every tool call, query, and model call traced | LangSmith or OpenTelemetry; trace id in the MDX frontmatter |
| Determinism | Same inputs, same evidence → materially the same primer | Temperature 0 for drafter and critic; plan and queries logged so a diff is explainable |
| Language | English only in v1 | Foreign filings (e.g. the Japanese interim reports the SpaceX example cites) are out of scope |

SAM.gov is the one tier-1 source with a hard daily quota: a personal API key allows on the order of tens of calls per day, so one primer must fit in roughly 10 Opportunities calls and a paged Entity walk, with every response cached for 7 days. A SAM.gov system account (about 1,000 calls/day plus bulk extracts) is the right answer if the agent will run more than a few primers a day. USAspending has no key and no published quota but DoD data lands about 90 days late, so the DoD contract announcements feed covers the gap.

## 9. Build plan and open questions

Four phases, each ending at a gate that is a measurable test rather than a feature list. No dates are set here; sizing is for the first conversation with whoever builds it.

**Phase 1 — Spine.** EDGAR client (tickers, submissions, companyfacts, full-text), USAspending and SAM.gov clients (shared quota counter), Firecrawl client with the credit counter, evidence store, PrimerState, the main graph with resolve_entity, foundation_pack, and one section researcher (section 9, Historical Financials, because it is structured-first and testable). Output a valid `.mdx` with just sections 1 and 9. **Gate:** Lockheed Martin FY2022–FY2025 revenue, net income, operating cash flow and capex match the example tables to the dollar, every number cites companyfacts, and the run cost is measured.

**Phase 2 — Fan-out.** plan_sections, Send fan-out, the critic loop, all 13 researcher briefs from section 6, the free quote and consensus scrapers behind the market-data adapter for sections 10 and 13, key_takeaways, the full validator. **Gate:** Boeing and Lockheed primers pass the validation gate with no `unavailable` sections; a judge-model rubric scores each section ≥ 3/5 against the AlphaSense reference; under 30 minutes wall-clock.

**Phase 3 — Hardening.** Caching, checkpointer resume, offline replay, the private-company path (SpaceX pre-IPO as the test), two non-aerospace tickers in the eval set, the domain allow/deny list, the deepagents comparison if phase 2 showed the hand-built subgraph is costly to maintain. **Gate:** a killed run resumes at the failed section; the same inputs replayed offline reproduce the primer; the private-company run emits honest partial flags instead of invented financials.

**Phase 4 — Operate.** CLI and a thin HTTP endpoint, scheduled re-runs with a diff against the prior primer (section 14 is the natural delta), the optional review interrupt, and the hook for the internal-relationship section if a CRM source is ever connected. **Gate:** a second person can run it from the README on a new ticker without help.

### Decisions taken Oct 21, 2026 (applied throughout the document above)

| Question | Decision | Where it lands |
|---|---|---|
| Market data for consensus, targets, multiples | Free pages (Yahoo Finance, StockAnalysis, MarketBeat) via Firecrawl for v1; Yahoo accepted with the terms-of-use exposure stated; FMP is the first paid swap | Section 4 free market-data table and paid-upgrade table; frontmatter `source_limits` |
| MDX rendering target | None; plain markdown with frontmatter and an `.mdx` extension, no imports or components | Section 2 MDX contract |
| Broker research / expert calls | Not available for v1; section 12 will be thinner than the examples and the frontmatter says so | Section 1 scope; section 2 frontmatter; paid-upgrade table |
| Search depth | One standard budget per section; deep deferred | Section 2 inputs |
| Evidence store and checkpointer | Files in the run folder; SQLite checkpointer file; no database | Sections 5 and 8 |
| Human approval | Runs straight through by default; `debug: true` pauses after the plan and before export | Section 2 inputs; section 5 nodes 4 and 10 |
| Cross-run evidence reuse | Not in v1 | Section 5 persistence paragraph |
| SAM.gov access | Personal API key for v1 (budget ≈10 Opportunities calls per primer); move to a system account when volume requires | Section 8 SAM.gov paragraph; paid-upgrade table |
| CAGE location scope | Active registrations only, kept when they appear as place of performance on an award in the last five fiscal years | Section 6 row 4; section 7 sourcing rule |

### Still open

- [ ] Model provider and the per-primer token budget that stops a section at its cap.
- [ ] Whether the first primer in the eval set should be Lockheed Martin (F-35, PAC-3, THAAD) or Northrop Grumman (Sentinel, B-21), given the Key Programs section is the newest and least-tested part.

### Sources consulted for this document

- Firecrawl: AI-powered data retrieval patterns — endpoint set, /agent replacing /extract, credit pricing per endpoint
- Firecrawl: best search tools for agents — /search behavior and pricing tiers
- LangChain: Open Deep Research architecture — supervisor + parallel researchers, one-shot report writing
- LangChain Deep Agents overview — planning, subagents, filesystem primitives
- SEC EDGAR APIs — submissions, companyfacts, frames, full-text search, fair-access rate limit
- FMP Price Target Consensus API — consensus targets endpoint
- GSA Get Opportunities Public API v2 — SAM.gov notices, key issuance, one-year posted-date window
- SAM.gov API developer guide (Cleatus) — Entity Management v3 fields (UEI, CAGE, address), personal-key daily quota observations
- USAspending API developer guide (Cleatus) — spending_by_award, award detail endpoint, no-key access, DoD ~90-day reporting lag
- AlphaSense Company Primers for Space Exploration Technologies, Lockheed Martin and Boeing (Oct 21, 2026), attached to the request
