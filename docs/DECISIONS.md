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
| Annual-fact selection rules | companyfacts tags `fy`/`fp` with the filing's fiscal year, not the period's, so periods are identified by start/end dates (350–380 days for durations; instants within 7 days of an annual period end found in the data, never the declared FYE month/day, which EDGAR gets wrong for some filers); latest filing wins on restatements and the earlier value is kept as `restated_from`. A 10-K fact tagged with a quarter-length period that starts the day after a 10-Q nine-month period ends and exceeds it is taken as the fiscal year (L3Harris FY2025 revenue) and carries a `note`; without the 10-Q fact it stays missing | `src/primer/clients/edgar.py` |
| Fiscal-year label | The filing's own `fy` (DocumentFiscalYearFocus) labels its current period (its latest 350–380 day period end); a comparative is `fy - round(years back)`; the current-period filing wins a conflict, then the later filing. That follows each company's naming (L3Harris's year ended 2026-01-02 is FY2025, CACI's ended 2025-06-30 is FY2025). The month rule (`fiscal_year_label`: January/February year ends label the prior year) is only the fallback for a filing with no usable `fy` (outside the period end year or the year before) | `edgar.build_fiscal_calendar`, `edgar.fiscal_year_label` |
| Fiscal-year end shown in text | From the latest annual period end: `MM/DD` when the window's period ends sit within 3 days of each other, else `52/53-week year ending YYYY-MM-DD`; the entity's declared `fiscalYearEnd` is a fallback only | `AnnualFinancials.fiscal_year_end`, `edgar.describe_fiscal_year_end` |
| Quarter label | The nearest calendar quarter end labels a quarter end (2026-01-02 is Q4-2025, 2025-03-28 is Q1-2025, 2026-01-30 is Q4-2025), the same answer as the release label (last calendar quarter end before the release date) | `edgar.quarter_label` |
| Sections not implemented emit `unavailable` with a coverage blockquote | Requirements §2: never silently dropped; numbering stays stable | `src/primer/graph/researcher.py` |
| Subgraph wrapped in a function node | Keeps parent-state writes to the six shared channels and makes checkpoint resume node-granular (a failed section reruns, not the run) | `src/primer/graph/main.py` |
| USAspending and SAM.gov clients built in Phase 1 without a consumer | SAM.gov personal-key quota makes fixture recording slow; starting early de-risks Phase 2 section 4 | `src/primer/clients/` |
| Sources appendix prints bare URLs, not `<url>` | MDX parses `<https://…>` as JSX and the compile check fails; GFM autolinks a bare URL | `src/primer/render/mdx.py` |
| Fiscal-year window set by core metrics, then all metrics pruned to it | Otherwise long-history concepts (revenue back to FY2008) leak into restatement notes and accession lists | `EdgarClient.annual_financials` |
| DEI cover-page facts accepted up to 120 days after an annual period end | `EntityCommonStockSharesOutstanding` is dated at the cover page, not the FYE, so the balance-sheet instant filter dropped it; labelled by the latest annual period end on or before it | `EdgarClient._select_annual` |

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

## 2026-10-21 — Anthropic for now, OpenAI stays the production path

| Decision | Why | Where |
|---|---|---|
| `PRIMER_MODEL_PROVIDER` env var overrides `provider` in `config/models.yaml` | The OpenAI balance ran out mid-run; the user supplied an Anthropic key for now but wants OpenAI to remain the configured production path, so the override lives in `.env`, not in config | `src/primer/config.py: active_provider`, `.env.example` |
| Anthropic temperature unset too | Claude 5.5 models return `temperature is deprecated for this model`; the provider block now carries `temperature: null` like OpenAI, and the models.yaml comment says when to set 0 | `config/models.yaml` |
| SAM.gov personal key and a fresh Firecrawl key added to `.env` | Unlocks the section 4 CAGE locations table; the first Firecrawl key's free tier was spent on the keyless runs | `.env` (gitignored) |

## 2026-10-22 — Phase 2 gate: final measurement (Anthropic models, SAM.gov, judge rubric)

Provider for this pass: `PRIMER_MODEL_PROVIDER=anthropic` (strong `claude-opus-5-5`, cheap `claude-haiku-5-5`); OpenAI stays the configured production path (commit d3ad926). SAM.gov personal key present; Firecrawl on a fresh free-tier key with pages served from the 7-day cache. The AlphaSense reference primers were extracted to `docs/golden/{lmt,ba}.md` (licensed, gitignored) for the judge and the fact-overlap score.

| Measure | LMT (4e16a602) | BA (0ba2487c) |
|---|---|---|
| Wall-clock | 2.8 min | 4.4 min |
| Model tokens, input / output | 313k / 72k | 422k / 100k |
| Tokens by role (in + out) | drafter 243k, critic 75k, synthesizer 31k, query planner 20k, planner 17k | drafter 316k, critic 92k, synthesizer 61k, query planner 36k, planner 17k |
| Model calls | planner 1, query planner 13, drafter 13, critic 13, synthesizer 1 | planner 1, query planner 20, drafter 18, critic 18, synthesizer 2 |
| Firecrawl credits / cache hits | 8 / 109 | 4 / 136 |
| SAM.gov calls (cached after the first run) | 10 (one per UEI) | 10 |
| Coverage full / partial / unavailable | 9 / 4 / 0 | 7 / 6 / 0 |
| Deterministic critic, min / mean | 4.0 / 4.6 | 3.67 / 4.4 |
| Judge rubric (Claude Opus vs the AlphaSense section), min / mean | 3.17 / 3.97 | 3.0 / 3.76 |
| All sections ≥ 3/5 | yes | yes |
| Fact overlap with the reference (amounts / percentages / names / programs) | 31% / 48% / 7% / 69%, mean 39% | 39% / 23% / 7% / 50%, mean 30% |
| Section 9 vs golden table | 12 of 12 exact | (no golden table) |
| Validation gate | passed | passed (re-validated after the attribution stoplist fix; same drafts) |
| Key Takeaways | 7 of 7 model bullets kept | 7 of 7 kept |

Decisions and notes from this pass:

| # | Decision | Why | Where |
|---|---|---|---|
| D2 revised | Per-primer token cap 600k (was 400k); no second critic pass once 70% is spent; synthesizer and judge exempt | A full pass with two critic iterations needs ~450–520k on Claude Opus, which writes longer drafts and critic notes than the OpenAI pass (~340k); at 400k the synthesizer was skipped and two sections kept template digests | `config/budgets.yaml`, `llm.py`, `graph/researcher.py` |
| SAM.gov registrations by UEI, one call each | Personal keys page at 10 records (the name walk returned 400) and the UEI set is ordered by award total, so ten lookups cover the registrations that matter within the per-primer budget | `clients/samgov.py`, `sections/sec04_programs.py` |
| GAO findings must be from the last ten years and name the program in the page title | A 1983 F/A-18 report is a government source but not the program's latest finding; the looser fallback had attached the F-35 finding to the F-22 row | `sec04_programs.py: gao_finding` |
| Rivals without SEC filings stay in the peer list without a ticker | Airbus and BAE are the reference's main Boeing peers; they get "named in the 10-K, no SEC filings" rows and n/a multiples instead of being dropped | `graph/foundation.py: unresolved_peers` |
| Officer table from the model when no table layout parses | Boeing's proxy lists officers in prose; the cheap model extracts name, age and title from the registered filing text only, cited to that filing | `sec08_governance.py` |
| Outlook tables need their own heading | Boeing's release has no guidance table; the loose match parsed segment revenue lines as guidance and produced bogus conflicts | `exhibits_sec06.py` |

Fact-overlap caveat: the names family counts every two-word capitalised phrase in the reference (575 for LMT), most of them broker names and headline fragments the public web does not carry, so 7% is the expected floor rather than a defect; amounts, percentages and programs are the comparable families. Judge scores carry `accuracy_vs_reference`, so they are a notch lower than the deterministic critic.

Credits and quota at the end of the pass: Firecrawl 999 → ~975 on the new key (the old key's 1,000 were spent across the keyless and OpenAI passes); SAM.gov 21 of the 30-a-day self-imposed cap.

## 2026-10-22 — Review §9 fixes 6 and 7: section budgets, brief targets, management answers

| # | Decision | Why | Where |
|---|---|---|---|
| Fix 6a | Sections 3, 5, 6, 12 and 14 get roughly double their search, scrape and credit caps (searches 4→8 / 3→6, scrapes 6→12, credits 20→40 / 16→32); section 4, the foundation pack and the defaults are unchanged | The Phase 2 runs spent 2 to 170 Firecrawl credits of the 420 per-primer cap while those sections reported "planned web queries dropped at the section search cap" and "scrape cap 6 reached": the section caps bound long before the primer cap did. Section credits now sum to 323 for a 13-section run; with foundation 30 and market data 40 the total is 393, still under 420 (arithmetic in the yaml comment) | `config/budgets.yaml`; the three handler tests that pinned "0/4", "0/3", "0/6" now read the new caps |
| Fix 6b | The review §4.2 targets are asked for explicitly in the briefs: section 3 adds sixth-generation / next-generation platform awards (`{co} sixth-generation fighter`, `{co} NGAD OR F-47 OR F/A-XX`) and production-system changes (`{co} shadow factory OR production line OR rate break`); section 5 adds peer backlog/fleet comparison, share by segment and anchor/launch customers; section 6 adds dated production-rate and delivery/output targets; section 8 adds labour agreements (`{co} union contract ratified OR strike {fy}`) and regulator/court/settlement actions, ordered ahead of the board-independence and activist queries because section 8 keeps its cap of 3; section 12 adds broker rationale as reported in news (`{co} analyst price target methodology`, `{co} upgrade OR downgrade reason`); section 14 adds certifications/approvals, deliveries and contract conversions. Each of these sections now also carries template sub-questions naming the same targets, so the planner model and the drafter see them even when the planner rewrites the query list | The free web carries these facts (Boeing's sixth-generation fighter positions, shadow factories, anchor customers, the SPEEA ratification, Q3 deliveries; Lockheed's PAC-3 conversion) and the runs did not fetch them because the generic templates never asked. Templates stay generic placeholders (`{co}`, `{peer}`, `{fy}`), not company wording; the program designations in the section 3 query are the sector's, not a company's. Template queries stay first in each list so the Firecrawl cache serves reruns; the planner's queries follow for whatever budget remains | `src/primer/sections/briefs.py` (`sub_questions` are now formatted with the same placeholders as the queries) |
| Fix 7 | Section 12 pairs each analyst question with the management answer that follows it in the same transcript: the next speaker turn(s) by a company officer, stopped at the operator, the asker or another analyst, trimmed to whole sentences within ~120 words. The officer is named as the transcript names them, with the title from the speaker header (Motley Fool form) or from the call's opening ("Evan Scott, our Chief Financial Officer"), and the pair cites the same transcript source id. The bull/bear table cell carries the answer after an em-dash ("— What management said (officer): …"), the questions list carries it as a sub-bullet under the question, the heading reads "Analyst questions and what management said", and the drafter receives `management_answers` with a rule to use them when weighing each point and never to present one as a broker view. A question whose wording is neutral takes its bull/bear lean from the answer | The review found section 12 was a rating-change log plus questions "as asked"; the answer is in the same transcript and is the closest free proxy to broker debate. Attribution is unchanged: a firm is still named only where the cited source names it, and management's words are labelled as the company's statement, not a view | `src/primer/sections/exhibits_sec12.py` (`Debate.answer`, `Debate.answered_by`, `_answer_after`, `_officer_title`, `answer_text`, `answer_line`), `src/primer/sections/sec12_thesis.py`; tests `test_parse_qa_pairs_question_with_management_answer`, `test_thesis_exhibit_carries_the_answer_line` |

Not done in this pass: the planner merge in `graph/main.py` still caps a brief at 10 queries (templates first), so sections whose templates now hold 6–7 queries leave the planner 3–4 slots; and `check_attribution` ignores table rows, so the answer line in a cell is bound by the handler (same source id as the question), not by the validator.

## 2026-10-22 — Phase 3 part A: the review fixes (PLAN §3, review §9 items 1–9)

Runs: Lockheed `output/LMT/2026-10-22-e4b82356`, Boeing `output/BA/2026-10-22-fe3fba8a`, both on the Anthropic models via `PRIMER_MODEL_PROVIDER=anthropic`, as-of 2026-10-22 (Lockheed's Q3-2026 release of 21 Oct is therefore in scope, which moves backlog and guidance away from the reference's 21 Oct figures and lowers Lockheed's amount overlap).

| Measure | LMT Phase 2 | LMT Phase 3 | BA Phase 2 | BA Phase 3 |
|---|---|---|---|---|
| Validation gate | passed | passed | passed | passed |
| Judge min / mean section score | 3.17 / 3.97 | 3.17 / 3.92 | 3.0 / 3.76 | 3.17 / 3.86 |
| Fact overlap with reference | 38.8% | 32.7% | 29.8% | 34.0% |
| Sources undated | 50 of 109 (46%) | 22 of 124 (18%) | 52 of 104 (50%) | 26 of 114 (23%) |
| "Not in the evidence" sentences contradicted elsewhere | 30+ | 0 | 23 | 0 |
| Cross-references written by the reconcile node | 0 | 2 | 0 | 2 |
| `> Conflict:` lines | 9 | 13 | 7 | 9 |
| Broken exhibits printed | 3 | 0 (3 dropped, prose kept, coverage partial) | 5 | 0 (3 dropped) |
| Boeing net debt FY2025 (reference 24,698) | — | — | 43,177 | 24,698 |
| Boeing employees (10-K: ~182,000) | — | — | 3,200 | 182,000 |
| Wall-clock | 2.8 min | 4.7 min | 4.4 min | 7.1 min |
| Model tokens | 386k | 508k | 522k | 622k |
| Firecrawl credits (7-day cache warm) | — | 10 | — | 18 |

| Decision | Why | Where |
|---|---|---|
| Reconcile node after the researcher waves (`graph/reconcile.py`) | Review §6: 19 cross-section contradictions, mostly a section denying a fact another section had. Deterministic registry plus a cheap-model matcher (`reconciler` role); denials become cross-references, point-in-time attributes (share price, market cap, employees, officers, revolver, dividend) become conflict lines | `src/primer/graph/reconcile.py`, `graph/main.py` |
| Guidance ranges, consensus targets and backlog never raise prose conflicts; claim-vs-claim pairs never conflict in the reconcile node | The first Phase 3 run paired segment-profit guidance with FCF guidance, a dividend total with a per-share rate and FY2025 backlog with Q3-2026 backlog; values an order of magnitude apart are also rejected (0.4–2.5× band). Same-metric claims stay the numeric gate's job | `reconcile.py: CONFLICT_ATTRS, _values_conflict, deterministic_conflicts` |
| Exhibit validator drops a broken table whole, keeps the prose, marks `partial`, says why | Review §5 items 1, 7, 8: a wrong table misleads more than a missing one. Row-level repair is the next refinement | `src/primer/validate/exhibits.py`, critic wiring |
| Section 14 runs first; sections 3, 6, 8, 10, 12 receive its dated events | Review §4.3: in-window facts fetched for one section never reached the others | `graph/main.py: fan_out, propagate_recent`, `sections/common.py: recent_events` |
| Net debt and EV net short-term investments; no YTD-subtraction EPS or attributable income | Review §5 item 2 (Boeing net debt off by $18bn), item 5 | `config/concepts.yaml`, `clients/edgar.py`, exhibits 9/11/13 |
| Source dates from URL and metadata; SEC archive documents typed by form; index pages refused; USAspending cited by award or API request; API records dated by the as-of date | Review §7 | `evidence/store.py`, `clients/firecrawl.py`, `clients/usaspending.py`, `clients/samgov.py` |
| Employee count, customer split, officer table from named 10-K passages; union, subsidiary and "U.S."-glued sentences judged by the text around each match | Review §5 items 3, 6; Boeing's "total workforce was approximately 182,000" and "Spirit employs approximately 15,000 people" | `sections/text_tools.py` |
| Structured output via `method="json_schema"` | Claude 5.5 rejects forced tool calling, which had silently sent the planner, reconciler and judge to the template path | `src/primer/llm.py` |
| Section 12 page budget 2.5 (was 2.0); management answers capped at 90 words and 6 questions | The AlphaSense thesis sections run about 4 pages; the drafter also now gets a hard word limit | `config/budgets.yaml`, `exhibits_sec12.py`, `sections/common.py` |
| Rating actions classified by rating before action | A Sell with a raised target was listed as a bull point | `exhibits_sec12.py: classify_rating` |
| Attribution check skips pronouns, quoted company officers and title-prefixed persons (checked by surname) | "He said", "Taiclet said", "Vice President Jonathan Root said" are not broker attributions | `validate/rules.py` |
| Per-section search and scrape caps roughly doubled for sections 3, 5, 6, 12, 14; review §4.2 targets added to the briefs | Credits were not the binding constraint; every narrative section reported dropped queries | `config/budgets.yaml`, `sections/briefs.py` |

Known limits carried into Phase 3 part B: dropped exhibits are not repaired (3 per primer: products-by-segment, peer positioning, ownership); 9–10 denials per primer stay unmatched; the model-side reconciler still raises period-different backlog and guidance conflicts; section 12's exhibit scores 3.17 on the judge (rating dump plus Q&A, not a thesis); the SAM.gov personal quota was spent by 21 Oct runs, so the locations tables came from cache; the broker layer is unchanged (review §9 item 10).



## 2026-10-22 — Phase 3 part B: hardening, verified live

Scope decision: the tool is used on defense companies only, so the defense configuration is the default for every company (trade press, sector queries, peer names), the technology, pharma and industrial settings were removed, and the eval set's Apple and Pfizer entries became Northrop Grumman and L3Harris. Private companies and unfamiliar SIC codes no longer fall through to an empty configuration.

### What was verified live, and how

| Item | Live check | Result |
|---|---|---|
| 3.2 resume | Lockheed run killed with `taskkill /T /F` 195 s in (all 13 sections started, no primer yet), then `primer resume` | Finished; only section 12 (in flight at the kill) ran again, the other twelve ran once; `run.json` accounting cumulative (`resumed: 1`, 411.6 s total) |
| 3.3 replay | Northrop Grumman run (live model calls and APIs), then `primer replay` in a fresh process, no network | `primer.mdx`, `sources.json` and `claims.json` identical; 0 missed lookups, 0 missed model calls, 0 network attempts; exit 0. The recording is about 53 MB per run |
| 3.1 cache | Warm-cache runs | 8 to 14 Firecrawl credits for a full Lockheed run against 100 to 220 cold |
| 3.6 policy | Lockheed run | 458 URLs allowed, 5 limited, 3 denied, 3 blocked by robots.txt, all counted in `meta.policy` |
| 3.4 private path, 3.8 ambiguity prompt | Offline tests only (24 and 19 tests) | Not run live: both Firecrawl keys ran out of credits (0 and 10 of 1,000 left) after the L3Harris and Northrop runs |

### Defects the live runs found (each fixed, with a regression test)

| Defect | Cause | Fix |
|---|---|---|
| L3Harris: FY2023 missing, FY2022 mislabelled, FY2025 revenue empty | Fiscal years labelled from EDGAR's declared year-end month, wrong for 52/53-week filers (and for CACI, which EDGAR lists as December); FY2025 revenue tagged with a 90-day period | Labels from each filing's own `fy` and the data's period ends; the 90-day fact repaired from the nine-month 10-Q fact; display "52/53-week year ending 2026-01-02" |
| Replay not exact (60 missed model calls) | Section 13 iterated a set of source ids, so prompt order differed per process | `dict.fromkeys`; `tests/test_determinism.py` scans the source for the pattern |
| Replay not exact (second cause) | `sources.yaml` edited after the run started; the planner prompt differed | Every run snapshots `config/*.yaml` and the code commit; replay and resume read the run's own snapshot; the CLI warns when the code differs |
| Replay not exact (third cause) | A replayed failure printed differently from the original (`RecordedError(...)` against `HTTPStatusError(...)`), and that text is embedded in the primer and in prompts | `RecordedError` reprs and prints as the original exception |
| L3Harris section 3 "unavailable: no Item 1" | The Item chunker required headings at the start of a line; L3Harris renders them in table cells and only Item 16 was found | Pipes allowed around headings; all 16 items now chunk |
| L3Harris attribution failure | The sentence splitter cut "CFO Kenneth L. Bedingfield said ..." after the initial; citations were read per sentence | Citations read per line; initials and "U.S." kept in one sentence; titled names checked by surname |
| Section 12 over its page budget in every run | Each analyst question printed twice (narrative, then an "unanswered" list with answers) | The list holds only questions management deferred or left unanswered |
| Northrop section 4 "unavailable" | USAspending returns 503 for a text search over 31 recipient identifiers (Northrop resolves to 31 UEIs; Lockheed's 17 passed) | Requests retry transient 5xx and 429 (5 attempts, about 53 s of backoff); recipient sets over 8 identifiers are batched and merged by amount (awards) or summed (categories); a failed batch is skipped and logged |
| Junk programs "AND", "SPACE", "SYSTEMS" in section 4 | All-caps heading lines in the 10-K matched the acronym pattern | Heading lines dropped before acronym extraction; generic-word lists extended |
| Section 3 listed the company against itself | The peer fallback names included the company | Filtered |

### Decisions

| Decision | Why | Where |
|---|---|---|
| Config is an input to a run | It shapes prompts and frontmatter; a replay must use the config the run used | `replay.snapshot_config`, `config.use_config_dir`, `PRIMER_CONFIG_DIR` |
| A replay compares the code commit | Misses can come from code changes, not nondeterminism | `inputs.json` `code`, CLI note |
| Fiscal year follows the filing's own `fy`, never EDGAR's declared month | The declared month is wrong for several filers | `clients/edgar.py` |
| USAspending recipient sets batched at 8 | One request over many OR terms times out | `clients/usaspending.py` `MAX_TERMS` |
| The shared recorded-fixture USAspending mock disables batching | It ignores the request filter, so batches would double-count | `tests/conftest.py` |

Known limits after part B: the SAM.gov personal quota was spent by the day's runs (locations come from cache or the place-of-performance view); Northrop's B-21 awards are largely classified, so B-21 appears in prose rather than as a table row; sections 3, 5, 6, 8, 10 and 14 stay `partial` on the free tier of web research; the private path and the ambiguity prompt are tested offline only; `eval-set --run` has not been executed.

## 2026-10-22: L3Harris recency failure (stale officer conflict)

The L3Harris run failed the recency check because reconcile printed a CEO "conflict" in
section 14 between William Brown (a 2019 merger press release, section 7) and the 2026
appointments, and the conflict note's citation to the 2019 source counted as section 14
evidence. Two fixes: a CEO/CFO pair whose sources are more than a year apart is a past
officer, not a conflict (`_historical_officer_pair`), and `check_recency` ignores the
`> Conflict:` note, which cites other sections' sources by design. The SpaceX private-path
rerun is still pending Firecrawl credits (balance 0 until the 2026-11-22 renewal).

## 2026-10-23: plain `.md` output, no Node toolchain

The primer is written as `primer.md` instead of `primer.mdx`. The content was already plain
GitHub-flavored markdown (no JSX, no imports), so only the extension and the gate changed. The
Node sidecar that compiled the file with `@mdx-js/mdx` is gone, along with the `tools/` folder,
the CI Node steps and the `mdx_compile` gate check. The gate keeps the frontmatter and citation
checks, and `primer md-check` replaces `primer mdx-check`. Runs made earlier keep their
`primer.mdx`: `primer_path()` reads either name, so replay and eval-set still work on them.

## 2026-10-23: SpaceX eval entry moved to the public path

SpaceX listed in June 2026 (SPCX, CIK 1181412), so the forced-private run made the judge flag
contradictions with its own IPO citations (judge minimum 2.83). The entry now runs publicly.
That run exposed a gap: a newly listed company has no 10-K and no annual XBRL, only an S-1/424B4
and 10-Qs, so seven sections came out unavailable and section 4 not_applicable (judge minimum
1.83). The entry fails on purpose until the foundation pack can fall back to the prospectus.
