# Decisions log

Decisions taken after the Oct 21, 2026 requirements document. Each entry says what was decided, why, and where it lands. Open decisions live in PLAN.md §6.

## 2026-10-21 — Phase 1 build

| # | Decision | Why | Where |
|---|---|---|---|
| D1 (default applied) | Model roles default to Anthropic (`claude-opus-5-5` strong, `claude-haiku-5-5` cheap) via `init_chat_model`; every role falls back to a deterministic template writer when no provider key is set | Phase 1's only researched section is structured-first, so the tables and claims are built without an LLM and the gate can run keyless; the LLM adds narrative and critic notes when configured | `config/models.yaml`, `src/primer/llm.py` |
| D4 (default applied) | MDX compile check via a Node sidecar (`tools/mdx-check.mjs`, `@mdx-js/mdx` + remark-gfm + remark-frontmatter) | Reference implementation; GFM tables and footnotes need remark-gfm; without Node the check reports `skipped`, never a false pass | `src/primer/validate/mdx_compile.py` |
| D5 (default applied) | Tracing: LangSmith when `LANGSMITH_TRACING=true`; otherwise `run.json` carries queries, timings, tokens, credits | No tracing backend required to run | `src/primer/runlog.py` |
| D6 | Golden primers are not committed; `docs/golden/` holds only derived tables (JSON) | Licensing of the AlphaSense documents is unknown | `docs/golden/lmt_financials.json` |
| Firecrawl via httpx, not the SDK | Recorded fixtures with respx stay trivial; the SDK surface has changed across majors | `src/primer/clients/firecrawl.py` |
| One XBRL source record per filing accession, not per fact | A 4-year table would otherwise carry ~100 footnotes; the evidence file lists every selected fact from that filing, and each claim records its concept | `src/primer/sections/sec09_financials.py` |
| Annual-fact selection rules | companyfacts tags `fy`/`fp` with the filing's fiscal year, not the period's, so periods are identified by start/end dates (350–380 days for durations, FYE month/day for instants); latest filing wins on restatements and the earlier value is kept as `restated_from` | `src/primer/clients/edgar.py` |
| Fiscal-year label | Year of the period end, except FYE in January or February labels the prior calendar year | `edgar.fiscal_year_label` |
| Sections not implemented emit `unavailable` with a coverage blockquote | Requirements §2: never silently dropped; numbering stays stable | `src/primer/graph/researcher.py` |
| Subgraph wrapped in a function node | Keeps parent-state writes to the six shared channels and makes checkpoint resume node-granular (a failed section reruns, not the run) | `src/primer/graph/main.py` |
| USAspending and SAM.gov clients built in Phase 1 without a consumer | SAM.gov personal-key quota makes fixture recording slow; starting early de-risks Phase 2 section 4 | `src/primer/clients/` |
| Sources appendix prints bare URLs, not `<url>` | MDX parses `<https://…>` as JSX and the compile check fails; GFM autolinks a bare URL | `src/primer/render/mdx.py` |
| Fiscal-year window set by core metrics, then all metrics pruned to it | Otherwise long-history concepts (revenue back to FY2008) leak into restatement notes and accession lists | `EdgarClient.annual_financials` |
| DEI cover-page facts accepted up to 120 days after a fiscal-year end | `EntityCommonStockSharesOutstanding` is dated at the cover page, not the FYE, so the balance-sheet instant filter dropped it | `EdgarClient._select_annual` |

## 2026-10-21 — Phase 1 gate measurement

Live run: `primer run --company "Lockheed Martin" --ticker LMT --sections 1,9 --as-of 2026-10-21`, no model key, no Firecrawl calls.

| Measure | Value |
|---|---|
| Wall-clock, cold cache | 3.1 s |
| Wall-clock, warm cache | 1.4 s |
| EDGAR requests (cold) | 4 (ticker file, submissions, companyfacts, 10-K document) |
| Firecrawl credits | 0 |
| Model tokens | 0 (template writer) |
| Golden match, FY2022–FY2024 × 4 metrics | 12 of 12 exact |
| Golden FY2025 | unfilled; XBRL gives revenue 75,048, net income 5,017, OCF 8,557, capex 1,649 (USD millions) from the 10-K filed 2026-01-29 |
| Validation gate | passed (citations, tokens, frontmatter, MDX compile, numeric, tier, attribution, recency, coverage, page budget) |
| Critic score, section 9 | passed; distinct sources 4, exhibit present |

The 30-minute and $10 budgets are not exercised by a structured-only section; Phase 2 measures them.

## 2026-10-21 — Phase 2 build

| # | Decision | Why | Where |
|---|---|---|---|
| D2 (default applied) | Per-primer caps: 400k tokens, 420 Firecrawl credits, 10 SAM.gov calls, 30 minutes; per-section search/scrape/credit caps sized so 13 sections sum to ~250 credits | The Firecrawl free plan carries 1,000 credits a month and allows 2 concurrent requests; two primers plus tuning must fit | `config/budgets.yaml`, `FirecrawlClient` caps |
| D3 | First section-4 eval ticker is Lockheed Martin (F-35, PAC-3, THAAD); Boeing is the second | Phase 1 fixtures and the golden table already cover LMT | PLAN.md §2 status |
| Planned batches instead of a free ReAct tool loop | Every section's buckets are known from §6, so structured calls plus a bounded query batch is cheaper, reproducible and budgetable; the critic loop supplies iteration (its notes become targeted gap queries in `plan_queries`) | `graph/researcher.py` |
| Firecrawl responses cached on disk for 7 days | A rerun of the same company (tuning, resume, the LLM pass after a keyless pass) must not spend credits; cache hits are counted separately in run.json | `clients/firecrawl.py`, `output/.cache/firecrawl` |
| `/parse` implemented as `/scrape` with the PDF parser | The v2 API parses PDFs through /scrape (`parsers: ["pdf"]`); the method name keeps the plan's vocabulary | `FirecrawlClient.parse` |
| EvidenceStore is a per-run singleton | Thirteen researcher threads registering into separate store instances would assign the same next source id | `EvidenceStore.for_run` |
| Earnings releases come from EDGAR (8-K Item 2.02, exhibit 99) not the IR site | Tier 1, free, stable URLs, and the quarter label derives from the report date; the IR newsroom is used only for the recent-developments log | `graph/foundation.py` |
| UEI set discovered from a USAspending name search, then every call filters by UEI | The recipient autocomplete endpoint returns no UEI; the award-search `Recipient UEI` field does; the resolved set is written to sources.json and run.json | `sections/sec04_programs.py` |
| Market-data pages scraped once per run and shared | Sections 6, 10, 12, 13 need the same six pages; a process memo persisted under `run_dir/market_data/` keeps credits at ~35 per run and makes resume free | `adapters/market_data_scraped.py` |
| Price from the Stooq close on the as-of date; multiples derived in-agent | Direct CSV GET costs no credits and is dated; aggregator multiples are shown only as secondary rows | `ScrapedMarketData.quote`, section 13 |
| Numeric conflicts are injected by `assemble` as `> Conflict:` blockquotes in the later section and the gate passes when disclosed | Requirements §2/§7: flag, never reconcile; the retry path only fires for an undisclosed conflict | `graph/main.py: inject_conflicts`, `validate/gate.py` |
| Key Takeaways bullets validated against the claims list | A bullet is dropped when its figures match no claim (under any display scaling) and no cited source text, when it cites a source no section cites, or when its section was not drafted | `validate/takeaways.py` |
| Judge rubric falls back to the deterministic critic scores | `primer eval` must run keyless; the scorecard says `judge: template` so a 3/5 gate claim is never made from the fallback | `eval/harness.py` |
| Golden primers matched to our sections by heading keywords | AlphaSense numbering differs (PLAN.md §7) | `eval/harness.py: split_golden` |

## 2026-10-21 — Phase 2 gate measurement (keyless)

Live runs of `primer run --company "Lockheed Martin" --ticker LMT` and `--company Boeing --ticker BA`, all 14 sections, no model key (template drafter and critic), no SAM.gov key, Firecrawl free plan (1,000 credits a month, 2 concurrent requests, 40 requests a minute).

| Measure | LMT, cold cache | BA, cold cache | LMT, warm cache | BA, warm cache |
|---|---|---|---|---|
| Wall-clock | 3.5 min (207 s) | 6.8 min (409 s) | 20 s | 29 s |
| Firecrawl credits | 93 | 169 | 0 | 6 |
| Firecrawl 429 retries | 0 (before the per-minute limiter) | 43 | 0 | 1 |
| EDGAR requests | 23 | 25 | 1 | 2 |
| USAspending requests | 1 | 10 | 0 | 0 |
| Cited sources (tier 1) | 44 (33) | 73 (50) | 115 (65) | 107 (60) |
| Coverage full / partial / unavailable | 4 / 8 / 1 | 5 / 7 / 0 | 9 / 4 / 0 | 7 / 6 / 0 |
| Critic mean, min / average | 3.0 / 3.88 | 3.0 / 3.97 | 3.33 / 4.24 | 3.33 / 4.10 |
| Validation gate | needs_review (page budget) | passed | passed | passed |
| Transcripts found | 4 of 4 | 4 of 4 | 4 of 4 | 4 of 4 |
| Model tokens | 0 | 0 | 0 | 0 |

The cold rows are the first runs before the fixes in commit 2efe7b4 (rate limiter, UEI field, officer and deal parsers, GAO cleaning); the warm rows are the final code on cached pages. A fresh company on the final code is expected to cost 150–200 credits and 4–7 minutes, dominated by the 30 requests-a-minute limiter; the 30-minute budget has a wide margin, so `budgets.yaml` was left at its initial per-section caps (sum ≈ 250 credits + 25 foundation + 35 market data) rather than raised.

What the keyless gate does and does not show:

- No `unavailable` section for either company; every section carries its required exhibit and claims, and the eight gate checks pass (citations, MDX compile, numeric with disclosed conflicts, tier, attribution, recency, coverage, page budget).
- The remaining `partial` flags are honest: SAM.gov registrations need `SAM_GOV_API_KEY` (locations are a place-of-performance view cross-checked to 10-K Item 2); EDGAR full-text search returns 403 from this network for every query shape tried (section 7 falls back to the 10-K/10-Q notes and press pages); the small per-section web budgets drop planned queries; Boeing's 10-K has no officer table in the parsed layouts and no outlook table in its release.
- The judge rubric is the deterministic critic score (`judge: template`). The ≥ 3/5 criterion against the AlphaSense reference, the LLM-planned briefs, the narrative prose and the LLM critic notes are untested until `ANTHROPIC_API_KEY` is set; `primer eval --ticker LMT` then scores each section with the judge model, and the Firecrawl cache makes that rerun free of credits.
- Credits used in this phase: 1,025 → 580 remaining at the end of the day (foundation probes, four cold runs, debugging runs).

## 2026-10-21 — Model provider switched to OpenAI

| Decision | Why | Where |
|---|---|---|
| `config/models.yaml` now has a `provider` switch with per-provider `strong`/`cheap` models and key/endpoint env vars; roles map to a tier | The user supplied an OpenAI key; Anthropic stays configured as the alternative, and a proxy or gateway is a one-line `.env` change via `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` (standard endpoints by default) | `config/models.yaml`, `src/primer/config.py: active_provider, provider_base_url, role_model`, `src/primer/llm.py` |
| OpenAI models: `gpt-5.4` for strong roles, `gpt-5.4-mini` for cheap roles | Highest-tier general models the key lists; verified with a live call | `config/models.yaml` |
| Temperature unset for OpenAI roles | GPT-5-family reasoning models reject a non-default temperature; the requirements' determinism target (temperature 0) holds only for the Anthropic provider, so reproducibility rests on logged plans, queries and evidence | `config/models.yaml` comment |
| Tests blank the provider keys instead of popping them | `primer.config` calls `load_dotenv(override=False)` on import, which restored a popped key from `.env` and sent the whole suite to the live model (one run cost real tokens before this was caught) | `tests/conftest.py` |

## 2026-10-21 — Phase 2 gate measurement (model pass, OpenAI gpt-5.4 / gpt-5.4-mini)

Provider switched to OpenAI in eccfbc8 (the key the user supplied). Firecrawl pages came from the 7-day cache, so these runs spent 2 credits each; USAspending and EDGAR were cached too.

| Measure | LMT, first model run (52aed6df) | LMT, final (98db3eb0) | BA (932c57d4) |
|---|---|---|---|
| Wall-clock | 6.5 min | 2.8 min | 1.1 min to the synthesizer, then the OpenAI account ran out of credits; resumed from the checkpoint in 10 s |
| Model tokens, input / output | 358k / 44k (cap hit, 20 calls skipped incl. the synthesizer) | 308k / 36k | not measured: the crash lost the first process's run.json |
| Tokens by role | drafter 298k, critic 78k, synthesizer 0 | drafter 222k, critic 59k, synthesizer 40k, query planner 12k, planner 10k | — |
| Model calls | planner 1, query planner 24, drafter 24, critic 24 | planner 1, query planner 18, drafter 18, critic 18, synthesizer 2 | sections drafted before the quota error; Key Takeaways from the template |
| Critic mean, min / average | 3.33 / 4.13 (deterministic) | 4.0 / 4.70 | 4.17 / 4.33 (resumed sections only) |
| Coverage full / partial / unavailable | 7 / 6 / 0 | 9 / 4 / 0 | 9 / 4 / 0 |
| Validation gate | needs_review (page budget) | passed | passed |
| Key Takeaways | template (synthesizer skipped at the cap) | 7 of 7 model bullets kept by the claims check | template |
| Cited sources (tier 1) | — | 94 (63) | 89 (57) |

What changed between the two Lockheed runs (commit e6a9235): the per-primer token cap is enforced per call with the synthesizer exempt and no second critic pass once 70% is spent; the critic's sentence splitter keeps trailing citation tokens and decimals (the first run scored every model sentence as uncited because the citation followed the full stop); quoted company language is not first person and a reported analyst rating is not a recommendation; the drafter honours the cut instruction and caps Perspectives at six bullets; template queries stay first in each brief so the Firecrawl cache serves reruns (the first model run spent 98 credits on planner-written queries).

Still open after this pass:

- The judge rubric against the AlphaSense reference has not run with a model: the OpenAI balance was exhausted before `primer eval`, and the scorecards for both runs say `judge: template` (deterministic critic). The AlphaSense primers are also not on disk (D6), so fact overlap was not measured. `primer eval --run-dir output/LMT/2026-10-21-98db3eb0 --ticker LMT --golden <path to the reference primer>` is the command once credits and the reference exist.
- Boeing needs one clean model run end to end for its own measurement (about 300k tokens, ~2 credits).
- Cost per primer in dollars depends on the OpenAI price list for gpt-5.4; at the measured 310k input / 36k output tokens it is well under the $10 target at any plausible rate.
- A model-call failure now degrades to the template path and disables further calls for the process (`llm_disabled` in run.json) instead of crashing the run.
