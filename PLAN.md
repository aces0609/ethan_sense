# ethan_sense — Build Plan

Derived from [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) (Company Primer Agent, Oct 21, 2026). This file turns the four phases in requirements §9 into ordered, testable work items. Section numbers below (§2, §5, …) refer to the requirements document.

**Goal.** `primer run --ticker LMT` writes `output/LMT/{run_id}/primer.mdx`, `sources.json`, `evidence/` and `run.json` in under 30 minutes, with every number traceable to a fetched source and no paid data subscription.

---

## 0. Stack and conventions

| Choice | Decision | Why |
|---|---|---|
| Language / packaging | Python 3.12, `uv`, `pyproject.toml`, src layout (`src/primer/`) | Matches LangGraph; `uv run` keeps commands path-safe |
| Orchestration | LangGraph (hand-built graph + `Send` fan-out), `langgraph-checkpoint-sqlite` | §5 default; deepagents evaluated in Phase 3, not before |
| Models | `langchain.chat_models.init_chat_model`, every role a config string | §5 "Model roles"; provider is an open decision (§9) |
| HTTP | `httpx` with one shared rate limiter per host | SEC 10 req/s, SAM.gov daily quota |
| Schemas | Pydantic v2 for state, briefs, claims, source records, tool outputs | Firecrawl `/agent` and `/scrape` take Pydantic schemas |
| Web retrieval | `firecrawl-py` SDK wrapped in one client with a credit counter | §4 Tier 2 |
| MDX check | Node sidecar: `node tools/mdx-check.mjs primer.mdx` using `@mdx-js/mdx` | §7 gate item 2; mdx-py is the fallback if Node is unwanted |
| Tests | `pytest`, recorded HTTP fixtures (`respx` or `vcrpy`) so unit tests never hit the network | Reproducibility; SEC fair-access |
| Lint | `ruff` (format + lint), `mypy` on `src/primer/state.py` and adapters | Cheap, catches reducer typos in state |
| Secrets | `.env` via `python-dotenv`; never committed; `run.json` redacts | §8 Secrets |
| Tracing | LangSmith when `LANGCHAIN_TRACING_V2=true`, otherwise a local JSONL trace | §8 Logging |

Proposed layout:

```
ethan_sense/
  PLAN.md                      this file
  README.md
  docs/
    REQUIREMENTS.md            source of truth
    DECISIONS.md               running log of decisions after Oct 21
    golden/                    AlphaSense reference primers (not committed if licensed; see §Eval)
  config/
    models.yaml                role → provider/model/temperature
    budgets.yaml               per-section search, credit and token caps
    sources.yaml               domain allow/deny list, sector trade-press table
    concepts.yaml              the ~40 XBRL concepts per statement with fallback names
  src/primer/
    cli.py                     `primer run`, `primer replay --offline`, `primer eval`
    state.py                   PrimerState, Brief, SectionDraft, Claim, SourceRecord
    graph/
      main.py                  intake → resolve_entity → foundation_pack → plan_sections → fan_out → … → export
      researcher.py            plan_queries → call_tools → compress_and_draft → critic
      nodes/                   one module per node
    clients/
      edgar.py                 tickers, submissions, companyfacts, full-text; rate-limited
      usaspending.py
      samgov.py                shared quota counter, 7-day cache
      firecrawl.py             search/scrape/parse/map/agent with credit counter + semaphore
      dod_announcements.py
    adapters/
      market_data.py           MarketDataAdapter protocol
      market_data_scraped.py   Yahoo / StockAnalysis / MarketBeat / TipRanks / Finviz / Stooq (v1)
      market_data_fmp.py       stub; first paid swap
    evidence/
      store.py                 register(), get(), hash, evidence/ writer, sources.json
      chunking.py              10-K Item-heading chunker
    sections/
      briefs.py                the 13 brief templates from §6
      exhibits.py              required-table builders (segment table, financials, program table, …)
      style.py                 house-style prompt fragments (caveat line, perspective bullets)
    render/
      mdx.py                   frontmatter, H2s, footnotes, Sources appendix
      citations.py             source_id → [^n] mapping
    validate/
      citations.py             every [^n] resolves; every source cited
      numeric.py               (metric, period) grouping + tolerance + conflict blockquotes
      attribution.py           "Firm notes…" must cite a source naming Firm
      tier.py                  statement numbers must trace to tier 1 when available
      recency.py               §14 trailing-12-month rule; §13 targets dated
      mdx_compile.py           calls the Node sidecar
    eval/
      harness.py               fact overlap, citation resolvability, judge-model rubric
  tools/
    mdx-check.mjs
  tests/
    fixtures/                  recorded EDGAR / USAspending / SAM.gov / Firecrawl responses
  output/                      gitignored
```

---

## 1. Phase 1 — Spine

**Exit gate (from §9):** a run for Lockheed Martin produces a valid `primer.mdx` containing only sections 1 and 9; FY2022–FY2025 revenue, net income, operating cash flow and capex match the AlphaSense table to the dollar; every number cites a companyfacts source record; `run.json` reports tokens, credits and wall-clock.

Ordered work items. Each has a test that proves it.

### 1.1 Repo bootstrap
- `uv init`, `pyproject.toml` with deps pinned, `ruff`, `pytest`, `.env.example` listing `EDGAR_USER_AGENT`, `FIRECRAWL_API_KEY`, `SAM_GOV_API_KEY`, model-provider keys.
- `.gitignore` for `output/`, `.env`, `*.sqlite`, `node_modules/`.
- CI: GitHub Actions running `ruff`, `pytest` (offline fixtures only).
- Test: `uv run pytest` green on an empty suite; `uv run primer --help` prints.

### 1.2 State and schemas (`state.py`)
- `PrimerState` TypedDict with the reducers from §5: `evidence_refs` (`operator.add`), `drafts` (dict-merge), `claims` (append), `coverage` (dict-merge), `errors` (append).
- Pydantic models: `Inputs`, `Entity` (cik, exchange, is_public, fy_end, aliases, uei_set), `Brief`, `SectionDraft` (markdown, claims, source_ids, coverage), `Claim` (metric, value, unit, period, section, source_ids), `SourceRecord` (§7 fields incl. `tier`, `gov_id`, `cited_by`).
- Test: two fake parallel branches writing different `drafts` keys merge without clobbering.

### 1.3 EDGAR client (`clients/edgar.py`)
- `resolve_ticker(tk) -> cik` from `company_tickers.json` (cached daily).
- `submissions(cik)` with the ~1,000-filing main array; helper to pick latest 10-K, two 10-Qs, DEF 14A, trailing-12-month 8-Ks.
- `companyfacts(cik)` filtered to `config/concepts.yaml`; returns facts keyed by (concept, fy, fp, form) with accession; dedupes restated values by picking the latest filing per period and recording the choice.
- `fulltext_search(query, forms, date_range)` against efts.
- Token-bucket limiter at 10 req/s shared across the process; descriptive `User-Agent` from env, refuses to start without it.
- Concept drift handled by ordered fallback lists in `concepts.yaml` (e.g. `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` → `SalesRevenueNet`).
- Test (recorded fixtures): LMT FY2022–FY2025 revenue/NI/OCF/capex equal the known 10-K values.

### 1.4 Evidence store (`evidence/store.py`)
- `register(payload, meta) -> source_id`: writes `evidence/{source_id}.md`, computes content hash, appends to the in-run index; idempotent on (url, as_of_date).
- The only path by which text reaches a model: the researcher reads chunks via `store.get(source_id)`.
- `write_sources_json(cited_by_map)` drops uncited sources from the appendix but keeps them on disk.
- Test: registering the same URL twice in one run returns the same id; hash changes if content changes.

### 1.5 Firecrawl client (`clients/firecrawl.py`)
- Thin wrappers over `/search`, `/scrape`, `/parse`, `/map`, `/agent`; every result passes through `store.register` before returning.
- Credit counter (estimated from §4 pricing; corrected from response headers if present) and an `asyncio.Semaphore` sized from config.
- Retry with backoff on 429; domain allow/deny post-filter from `config/sources.yaml`; paywalled hits recorded with `access: limited`.
- Test: a recorded `/scrape` of an LMT press release lands in `evidence/` with a source record of `type: press_release`, `tier: 2`.

### 1.6 USAspending and SAM.gov clients
- Built in Phase 1 because the gate for section 4 in Phase 2 depends on recorded fixtures that take time to collect under the SAM.gov quota.
- `usaspending.spending_by_award(uei_set, fy_from, sort="potential")`, `award(id)`, `spending_by_category(uei_set, group_by=["awarding_subagency","fiscal_year"])`.
- `samgov.entities(parent_uei)` paged walk; `samgov.opportunities(query, posted_from, posted_to)` with the one-year window enforced; a persistent daily call counter (`output/.samgov_quota.json`) that refuses past the configured cap and a 7-day response cache.
- Test: quota counter blocks the 11th Opportunities call in a run; entity walk pages correctly on a recorded 3-page fixture.

### 1.7 Main graph skeleton (`graph/main.py`)
- Nodes: `intake`, `resolve_entity`, `foundation_pack`, a hard-coded single `Send` to section 9, `assemble`, `validate` (citation integrity only, for now), `export`.
- `resolve_entity`: ticker path only in Phase 1; name-only path and ambiguity interrupt deferred to Phase 2.
- `foundation_pack`: fetch the Submissions index, companyfacts, latest 10-K; chunk the 10-K by Item heading into the store. 10-Qs, DEF 14A, releases and transcripts are added in Phase 2 when their consumers exist.
- SQLite checkpointer at `output/{slug}/{run_id}/checkpoint.sqlite`.
- Test: graph runs end to end on fixtures and writes all four output files.

### 1.8 Section 9 researcher (`graph/researcher.py` with the §6 row-9 brief)
- `plan_queries`: for section 9 this is a structured-only plan: the concept list × 4 FY; one optional full-text search to explain anomalies.
- `call_tools`: calls `companyfacts`; registers one source record per (concept, period) fact with the accession URL.
- `compress_and_draft`: produces the three tables (P&L, cash flow, balance sheet) from the facts, FCF as a derived row with its formula, the caveat line, and a claims list.
- `critic`: rubric from §7, max 2 loops.
- Test: the draft's claims list contains exactly the table values, each with a companyfacts `source_id`.

### 1.9 Renderer and MDX check (`render/`, `tools/mdx-check.mjs`)
- Frontmatter per §2 contract, including `coverage` and the two mandatory `source_limits` strings.
- `[^n]` assignment in first-citation order; Sources appendix in the AlphaSense label style with `gov_id` printed when present.
- Section 1 (Key Takeaways) written from section 9 alone in Phase 1, so the file has the two-section shape the gate asks for.
- Node sidecar compiles the file with `@mdx-js/mdx`; non-zero exit fails `validate`.
- Test: a fixture MDX with an unresolved `[^99]` fails; the LMT output compiles.

### 1.10 Run accounting (`run.json`)
- Per-node timings, tokens by model role, Firecrawl credits, SAM.gov calls, queries issued, coverage flags, critic scores.
- Keys redacted.
- Gate measurement: record cost and wall-clock for the LMT run in `docs/DECISIONS.md`.

### 1.11 Eval harness v0 (`eval/harness.py`)
- Load a golden primer (LMT) and extract its financial table; compare to the agent's section 9 numbers; report exact-match rate.
- This is the mechanical half of the gate. The judge-model rubric arrives in Phase 2.

**Phase 1 definition of done:** items 1.1–1.11 merged, the LMT run meets the gate, cost and time recorded.

**Status (2026-10-21): done.** Items 1.1–1.11 are implemented and covered by 29 tests on recorded fixtures. The live LMT run (sections 1 and 9) passes the validation gate with FY2022–FY2024 revenue, net income, operating cash flow and capex matching the golden table to the dollar and every cell citing a companyfacts record. Measurements are in [docs/DECISIONS.md](docs/DECISIONS.md). Two caveats: the golden table's FY2025 column is still unfilled pending the AlphaSense figures, and the run so far is keyless (template narrative), so the LLM drafter and critic paths are untested live.

---

## 2. Phase 2 — Fan-out

**Exit gate (from §9):** Boeing and Lockheed primers pass the full validation gate with no `unavailable` sections; a judge-model rubric scores every section ≥ 3/5 against the AlphaSense reference; under 30 minutes wall-clock.

### 2.1 Foundation pack, complete
- Add the two latest 10-Qs, DEF 14A, last 4 earnings releases and transcripts (Motley Fool via `/search`+`/scrape`, IR webcast PDFs via `/parse`), IR presentation index via `/map`.
- Record which transcripts were missed; this number drives the §4 "FMP transcripts" upgrade trigger (>20% misses).

### 2.2 `plan_sections` and the 13 briefs (`sections/briefs.py`)
- One strong-model call that emits a `Brief` per requested section from the §6 templates, expanded with the entity's aliases and the sector trade-press table.
- Briefs state source buckets in priority order and a stop condition.
- Briefs written to `run.json`; `debug: true` interrupts here.

### 2.3 `fan_out` with `Send`
- Conditional edge returning one `Send` per section in `inputs.sections`; each carries only its brief and foundation-pack handles.
- Section 4 gate: emitted as a one-line `not_applicable` blockquote when the 10-K discloses no US-government revenue.

### 2.4 Generic researcher subgraph
- `plan_queries` (cheap model): structured calls first, then 3–8 Firecrawl searches with bucket hints and domain post-filter.
- `call_tools` (cheap model): ReAct loop over EDGAR / USAspending / SAM.gov / Firecrawl / market-data tools; per-section budget from `config/budgets.yaml` stops the loop and marks `partial`.
- `compress_and_draft` (strong model): house style, required exhibit, claims list, `source_id`-only citations.
- `critic` (strong model): rubric scores into `run.json`; max 2 loops; page-budget cut instruction when >50% over.

### 2.5 Section-specific exhibit builders (`sections/exhibits.py`)
Ordered by risk, highest first:
1. **Section 4 program and location tables** — the newest and strictest part (§7 program rule). Join USAspending awards by UEI set → group by description keywords/PSC → candidate programs; DoD announcements parsed with `/agent` to a schema keyed by contract number; SAM.gov entity walk filtered to active registrations that appear as place of performance in the last five FYs; GAO/SAR status only from government PDFs. Every row carries `gov_id`s; `role: not confirmed` when no record names the role.
2. **Section 13 derived multiples** — market cap, EV, EV/EBITDA, P/E from XBRL + quote; formulas printed in the appendix; LTM and NTM never in one table.
3. **Section 7 acquisition table** — EDGAR full-text on 8-K Items 1.01/2.01 + `/agent` over press releases with a Pydantic schema.
4. **Section 10 KPI vs consensus** — consensus and beat/miss from the market-data adapter; actuals from releases.
5. **Section 11 debt table** — leverage always carries its definition label.
6. Sections 2, 3, 5, 6, 8, 12, 14 tables.

### 2.6 Market-data adapter (`adapters/`)
- `MarketDataAdapter` protocol: `quote`, `shares_outstanding` (XBRL preferred), `consensus`, `price_targets`, `rating_changes`, `price_history`, `analyst_coverage`.
- v1 implementation scrapes Yahoo, StockAnalysis, MarketBeat, TipRanks, Finviz, Stooq with `/scrape` JSON schemas; each value cited to its page and dated; never averaged across aggregators.
- A breakage in any scraper marks the dependent fields `partial`, never substitutes another aggregator silently.
- FMP adapter stub with the same protocol so the swap is one config line.

### 2.7 `key_takeaways`
- Strong model, no tools, up to 7 bullets, each referencing the section number it summarizes; validator rejects a bullet whose claim is not in any section's claims list.

### 2.8 Full validator (`validate/`)
- All eight §7 gate checks; failures route back to the offending section with critic notes, max 2 retries, then `needs_review`.
- Numeric check groups claims by normalized `(metric, period)` with per-metric tolerance from config; conflicts rendered as `> Conflict:` blockquotes in the later section.
- Attribution check: regex for "Firm notes/argues/estimates/…" patterns against the cited sources' text.

### 2.9 Eval harness v1
- Judge-model rubric per section against the golden primers; fact-overlap scoring for deals, officers, contracts, numbers; citation-resolvability rate.
- `primer eval --ticker LMT --golden docs/golden/lmt.md` writes a scorecard to `output/eval/`.

### 2.10 Performance
- Concurrency: all 13 researchers in parallel under the Firecrawl semaphore and the SEC limiter; measure wall-clock and credits for BA and LMT; tune `budgets.yaml` until under 30 minutes.

**Status (2026-10-22): done; gate met.** Items 2.1–2.10 are implemented and covered by 74 tests on recorded EDGAR and USAspending fixtures plus hand-written Firecrawl pages: the complete foundation pack (10-Qs, DEF 14A, four earnings releases from 8-K exhibits, four transcripts with miss tracking, IR map, peers incl. rivals without SEC filings, government-revenue gate), `plan_sections` as a planner call with the template fallback, one handler per section 2–14 behind the contract in [docs/HANDLERS.md](docs/HANDLERS.md), the section-4 program, agency-mix and SAM.gov CAGE locations tables with a `gov_id` on every row, the derived-multiples tables (LTM and NTM never mixed), the market-data adapter (scraped v1 plus the FMP stub), the Key Takeaways validator, conflict blockquotes, the full eight-check gate with bounded retries, and the eval scorecard with the judge rubric and fact overlap against the AlphaSense reference. Gate: the Lockheed Martin and Boeing primers pass the validation gate with no `unavailable` section in 2.8 and 4.4 minutes with the models (Claude Opus 5.5 / Haiku 5.5 via `PRIMER_MODEL_PROVIDER=anthropic`; OpenAI gpt-5.4 is the configured production path), the judge model scores every section at 3/5 or better against the reference (LMT min 3.17 / mean 3.97; BA min 3.0 / mean 3.76), fact overlap with the reference is 39% (LMT) and 30% (BA), and section 9 matches the golden table to the dollar. Measurements, token counts and the revised D2 are in [docs/DECISIONS.md](docs/DECISIONS.md). Known limits carried into Phase 3: EDGAR full-text search returns 403 from this network (section 7 relies on filing notes and press), Stooq sits behind a browser check (Yahoo chart endpoint used), the Firecrawl free plan allows 40 requests a minute, and the judge's `accuracy_vs_reference` is a model judgement rather than a fact match.

---

## 3. Phase 3 — Hardening

**Exit gate (from §9):** a killed run resumes at the failed section; the same inputs replayed offline reproduce the primer; the private-company run emits honest `partial`/`unavailable` flags instead of invented financials.

- **3.1 Cache.** Content-hash cache keyed by URL + date under `output/.cache/`; companyfacts per CIK per day; SAM.gov 7-day. Same ticker within 24 h skips the foundation-pack fetch.
- **3.2 Resume.** Kill-test: SIGTERM mid-section, rerun with the same `run_id`, confirm only the unfinished section re-executes.
- **3.3 Offline replay.** `primer replay RUN_DIR`: every cache read and model call comes from the run's own recording (`replay/`), config from its snapshot, evidence ids unchanged; any network call raises; the primer, sources and claims are diffed against the original. Exact on a live Northrop Grumman run.
- **3.4 Private-company path.** `resolve_entity` name-only branch: Firecrawl search + EDGAR full-text (Form D, competitor 10-Ks naming the company). Sections 9, 11, 13 emit `partial`/`unavailable` with a note on what was found. Test on SpaceX pre-IPO against the golden primer.
- **3.5 Eval breadth.** Two non-aerospace tickers added to `primer eval`; sector trade-press table extended via config, not code.
- **3.6 Domain policy.** Allow/deny list enforced in the Firecrawl post-filter; robots.txt respected where exposed; paywalled items cited by headline + URL only.
- **3.7 deepagents comparison.** Only if Phase 2 showed the hand-built subgraph costly to maintain: rebuild one section with `create_deep_agent(subagents=[...])`, compare code size, cost and validator pass rate, record the verdict in `docs/DECISIONS.md`.
- **3.8 Ambiguity interrupt.** `resolve_entity` raises an interrupt when two plausible entities score close; CLI prompts; `debug: true` review interrupt before export.

**Status (2026-10-22): part B done except four live checks.** 3.1 to 3.3, 3.5, 3.6 and 3.8 are implemented and tested (287 tests). 3.2 and 3.3 are verified on live runs: a hard kill resumed with only the in-flight section re-run, and a Northrop Grumman replay reproduced primer, sources and claims exactly with zero misses and zero network attempts. The tool is defense-only, so the eval set is now Lockheed, Boeing, SpaceX, Northrop Grumman and L3Harris, and the defense configuration is the default for every company. The live runs found and fixed ten defects (fiscal years for 52/53-week filers, set-order, config and error-text divergence in replay, table-wrapped Item headings, attribution across initials, duplicated section 12 Q&A, USAspending 503s on large recipient sets, junk program names); the table is in [docs/DECISIONS.md](docs/DECISIONS.md). Not yet verified live because both Firecrawl keys are out of credits: the private-company path on SpaceX (3.4), the ambiguity prompt (3.8), `primer eval-set --run` (3.5), and a full L3Harris rerun to confirm its gate. 3.7 (deepagents comparison) is skipped: nothing in Phases 2 or 3 suggested the hand-built graph is costly to maintain.

**Status (2026-10-22): part A done.** Phase 3 was split after the editorial review ([docs/REVIEW-PRIMERS-2026-10-21.md](docs/REVIEW-PRIMERS-2026-10-21.md)): part A is the review's nine pipeline fixes, part B is items 3.1–3.8 above. Part A is implemented and covered by 120 tests: a cross-section reconcile node, exhibit validators, section 14 feeding the later sections, net debt and EV netting short-term investments, dated and correctly typed sources, 10-K-item extraction for headcount and officers, management answers in section 12, prose hygiene, and raised narrative-section budgets. Both primers pass the full gate with the models; the before/after table and the decisions are in [docs/DECISIONS.md](docs/DECISIONS.md) (Boeing net debt now matches the reference, headcount reads 182,000, undated sources fell from ~48% to ~20%, every "not in the evidence" contradiction is gone). Not yet done: part B (cache test, resume kill-test, offline replay, private-company path on SpaceX, two non-aerospace tickers, domain policy, ambiguity interrupt), repair rather than drop of broken exhibits, and the section 12 thesis exhibit.

---

## 4. Phase 4 — Operate

**Exit gate (from §9):** a second person runs it from the README on a new ticker without help.

- **4.1 CLI polish.** `primer run`, `primer replay`, `primer eval`, `primer diff`; JSON request file as an alternative to flags.
- **4.2 HTTP endpoint.** Thin FastAPI wrapper: `POST /runs` returns `run_id`; `GET /runs/{id}` streams status from the checkpointer; one worker, no queue in v1.
- **4.3 Scheduled re-runs and diff.** `primer diff --ticker LMT --from {run_a} --to {run_b}` reports section 14 deltas, changed claims and new sources.
- **4.4 Internal-relationship hook.** Section 15 slot in `briefs.py` and `fan_out` behind a feature flag; no CRM adapter built.
- **4.5 README.** Install, `.env`, first run, reading `run.json`, where the paid swaps are configured.

---

## 5. Cross-cutting rules to enforce from day one

1. **No citation without a fetch.** Models only ever see `source_id`s from `evidence/store.py`. Code review rejects any prompt that passes raw URLs or pasted text to a drafter.
2. **Numbers prefer tier 1.** `concepts.yaml` and the tier validator are built in Phase 1 so later sections inherit them.
3. **Budgets are config, not code.** `budgets.yaml` holds search count, credits, tokens and page estimate per section.
4. **Everything reproducible.** Recorded fixtures for every client; temperature 0 for drafter and critic; plan and queries in `run.json`.
5. **Secrets never in the repo.** `.env.example` only; `run.json` redaction tested.

---

## 6. Open decisions (need an answer before the item that depends on them)

| # | Decision | Blocks | Proposed default |
|---|---|---|---|
| D1 | Model provider and per-role models | 1.8 drafter, 2.2 planner | Anthropic: strong role `claude-opus-5-5` (or `claude-fable-5-1`), cheap role `claude-haiku-5-5`; configurable in `models.yaml` |
| D2 | Per-primer token budget and per-section cap | 2.4 | Start at 400k tokens per primer, 40k per section research loop; revise after Phase 1 measurement |
| D3 | First eval ticker for section 4: Lockheed (F-35, PAC-3, THAAD) or Northrop (Sentinel, B-21) | 2.5 | Lockheed, because Phase 1 already fixtures LMT and the golden primer exists; add NOC as the second aerospace eval |
| D4 | MDX compile via Node sidecar or mdx-py | 1.9 | Node sidecar (`@mdx-js/mdx` is the reference implementation) |
| D5 | Tracing backend | 1.10 | LangSmith if a key exists, local JSONL otherwise |
| D6 | Golden primers in the repo | 1.11 | Keep under `docs/golden/` only if licensing allows; otherwise a gitignored local path in `.env` |

---

## 7. Known inconsistencies in the requirements to resolve while building

- §2 says "The 13 sections" but lists 14 (13 researched plus Key Takeaways). §8 says "12 researchers in parallel"; §5 says 13. Treat the count as 13 researchers plus one synthesis step.
- §3 "Editorial pattern" refers to tables in sections 8, 6, 9, 12 and 4 using AlphaSense's numbering, which is offset from this document's section table (financials are §9 here, acquisitions §7, KPIs §10, price targets §13, SWOT §5). Use this document's numbering in code.
- §7 gate item 3 mentions a `<Callout type="conflict">` component while §2 forbids JSX in v1. Use the `> Conflict:` blockquote per §2.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| SAM.gov personal-key quota (tens of calls/day) blocks section 4 development | Record fixtures early (1.6); 7-day cache; request a system account in parallel |
| Yahoo/StockAnalysis layout changes break sections 10 and 13 mid-run | Adapter marks fields `partial`; FMP stub ready; terms-of-use exposure stated in frontmatter |
| Firecrawl free tier (1,000 credits) exhausted after one primer | Turn on the Hobby plan before the second primer (§4 upgrade table) |
| XBRL concept drift yields wrong FY comparisons | Ordered fallback lists in `concepts.yaml`; restatement rule in 1.3; Phase 1 gate checks to the dollar |
| Fabricated broker attributions | Attribution validator (2.8) plus the rule in the §6 row-12 brief |
| 30-minute wall-clock missed | Measure in Phase 1; cheap model for tool loops; budgets.yaml tuning in 2.10 |
