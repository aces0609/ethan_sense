# ethan_sense

Company Primer Agent: give it a company name (and ticker, if public) and it writes a 14-section company primer as a single `.mdx` file, every factual claim cited to a fetched source. Built on LangGraph, Firecrawl, SEC EDGAR and US-government open-data APIs. No paid data subscription in v1.

**Status:** Phase 2 (fan-out) implemented: all thirteen researched sections plus Key Takeaways, the market-data adapter, the full validation gate and the eval scorecard. See [PLAN.md](PLAN.md) for the phases and gate status and [docs/DECISIONS.md](docs/DECISIONS.md) for the decisions and measurements.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — the requirements document (source of truth)
- [PLAN.md](PLAN.md) — phased build plan with per-item tests, exit gates, open decisions and risks
- [docs/DECISIONS.md](docs/DECISIONS.md) — decisions taken after Oct 21, 2026
- [docs/HANDLERS.md](docs/HANDLERS.md) — the contract every section handler follows

## Install

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Node 18+ (for the MDX compile check only).

```bash
uv sync --all-groups
```

```bash
npm install --prefix tools
```

Copy `.env.example` to `.env` and set:

| Variable | Needed for |
|---|---|
| `EDGAR_USER_AGENT` | Everything. SEC requires a descriptive User-Agent with a contact email; the client refuses to start without one |
| `FIRECRAWL_API_KEY` | Transcripts, IR site map, trade press, aggregator pages (sections 2–8, 10–14). Without it the run is structured-only: filings, XBRL and USAspending, with web buckets marked partial |
| `ANTHROPIC_API_KEY` | The planner, drafter, critic, synthesizer and judge roles in `config/models.yaml`. Without it every role uses a deterministic template writer: exhibits and claims are built, narrative is not |
| `SAM_GOV_API_KEY` | Section 4 locations table from SAM.gov entity registrations; without it the table is a place-of-performance view marked partial |

## Run

```bash
uv run primer run --company "Lockheed Martin" --ticker LMT
```

```bash
uv run primer run --company "Boeing" --ticker BA --sections 1,4,9,13
```

Output lands in `output/{TICKER}/{run_id}/`:

| File | Contents |
|---|---|
| `primer.mdx` | Frontmatter, numbered `##` sections, GFM footnotes, Sources appendix, derived-formula list |
| `sources.json` | Every cited source with type, tier, URL, fetch time, content hash, `gov_id`, `cited_by`, plus the resolved UEI set |
| `evidence/` | Raw text of every fetch, one file per source id, plus `index.json` |
| `run.json` | Briefs, queries, per-node timings, tokens by role, credits, cache hits, critic scores, budgets, validation report, transcript misses |
| `claims.json` | Every numeric claim with metric, period, value and source ids |
| `market_data/` | The aggregator-page extractions (scraped once per run, shared by sections 6, 10, 12, 13) |
| `checkpoint.sqlite` | LangGraph checkpoint; `primer resume output/LMT/{run_id}` continues a killed run |

Firecrawl responses are cached for seven days under `output/.cache/firecrawl`, so rerunning the same company (for example after adding the Anthropic key) costs no credits.

Other commands:

```bash
uv run primer eval --run-dir output/LMT/<run_id> --ticker LMT
```

(`--golden path/to/primer.md` for the judge rubric and fact overlap against a reference primer, `--golden-financials docs/golden/lmt_financials.json` for the to-the-dollar check; `--ticker` picks both from `docs/golden/` or `GOLDEN_DIR` when present.)

```bash
uv run primer mdx-check output/LMT/<run_id>/primer.mdx
```

```bash
uv run primer info
```

## Test

```bash
uv run pytest -q
```

Tests run offline against recorded EDGAR and USAspending fixtures and hand-written Firecrawl pages (`tests/conftest.py`); regenerate the recorded ones with `uv run python tests/fixtures/make_fixtures.py`. CI runs ruff, the test suite and the Node sidecar on every push.

## Layout

```
config/          models.yaml (roles), budgets.yaml (caps), sources.yaml (domain policy, market pages, trade press), concepts.yaml (XBRL)
src/primer/
  clients/       edgar, usaspending, samgov, firecrawl (cached), dod_announcements, ratelimit, cache
  adapters/      market_data (protocol), market_data_scraped (v1), market_data_fmp (stub)
  evidence/      store (per-run source_id registry), chunking, html
  sections/      briefs, common (ToolContext, budgets, web research, drafting), text_tools, exhibits, secNN_* handlers
  graph/         main graph, foundation pack, researcher subgraph, critic
  render/        mdx, citations
  validate/      citations, numeric, tier, attribution, recency, coverage, page budget, takeaways, mdx compile
  eval/          scorecard: judge rubric, fact overlap, citation resolvability, golden financials
tools/           mdx-check.mjs Node sidecar
tests/           pytest suite with recorded fixtures and Firecrawl page mocks
```
