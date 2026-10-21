# ethan_sense

Company Primer Agent: give it a company name (and ticker, if public) and it writes a 14-section company primer as a single `.mdx` file, every factual claim cited to a fetched source. Built on LangGraph, Firecrawl, SEC EDGAR and US-government open-data APIs. No paid data subscription in v1.

Status: planning. No code yet.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — the requirements document (source of truth)
- [PLAN.md](PLAN.md) — phased build plan with per-item tests, exit gates, open decisions and risks
- `docs/DECISIONS.md` — decisions taken after Oct 21, 2026 (created with the first one)

## Planned shape

```
primer run --ticker LMT            # writes output/LMT/{run_id}/primer.mdx, sources.json, evidence/, run.json
primer replay --run-id … --offline # reproduce a primer from its evidence folder, no network
primer eval --ticker LMT           # score against a golden reference primer
```

Phase 1 ends when a Lockheed Martin run produces a valid `.mdx` with sections 1 and 9 whose FY2022–FY2025 financials match the reference to the dollar, every number citing EDGAR companyfacts. See PLAN.md §1.
