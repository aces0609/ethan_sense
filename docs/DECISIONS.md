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
