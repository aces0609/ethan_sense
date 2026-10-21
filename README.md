# ethan_sense

Company Primer Agent: give it a company name (and ticker, if public) and it writes a 14-section company primer as a single `.mdx` file, every factual claim cited to a fetched source. Built on LangGraph, Firecrawl, SEC EDGAR and US-government open-data APIs. No paid data subscription in v1.

**Status:** Phase 1 (spine) complete. Sections 1 and 9 are produced from SEC XBRL; the other twelve section researchers land in Phase 2. See [PLAN.md](PLAN.md) for the phases and [docs/DECISIONS.md](docs/DECISIONS.md) for the decisions and gate measurements.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — the requirements document (source of truth)
- [PLAN.md](PLAN.md) — phased build plan with per-item tests, exit gates, open decisions and risks
- [docs/DECISIONS.md](docs/DECISIONS.md) — decisions taken after Oct 21, 2026

## Install

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Node 18+ (for the MDX compile check only).

```bash
uv sync --all-groups
```

```bash
npm install --prefix tools
```

Copy `.env.example` to `.env` and set at least `EDGAR_USER_AGENT` (SEC requires a descriptive User-Agent with a contact email; the client refuses to start without one). `ANTHROPIC_API_KEY` turns on the LLM narrative, critic and synthesis roles; without it every role uses a deterministic template writer, which is enough for the structured-first section. `FIRECRAWL_API_KEY` and `SAM_GOV_API_KEY` are used by the Phase 2 researchers.

## Run

```bash
uv run primer run --company "Lockheed Martin" --ticker LMT --sections 1,9
```

Output lands in `output/LMT/{run_id}/`:

| File | Contents |
|---|---|
| `primer.mdx` | Frontmatter, numbered `##` sections, GFM footnotes, Sources appendix |
| `sources.json` | Every cited source with type, tier, URL, fetch time, content hash, `cited_by` |
| `evidence/` | Raw text of every fetch, one file per source id, plus `index.json` |
| `run.json` | Queries, per-node timings, tokens, credits, critic scores, validation report |
| `claims.json` | Every numeric claim with metric, period, value and source ids |
| `checkpoint.sqlite` | LangGraph checkpoint; `primer resume output/LMT/{run_id}` continues a killed run |

Other commands:

```bash
uv run primer eval --run-dir output/LMT/<run_id> --golden docs/golden/lmt_financials.json
```

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

Tests run offline against recorded EDGAR fixtures under `tests/fixtures/edgar/` (regenerate with `uv run python tests/fixtures/make_fixtures.py`). CI runs ruff, the test suite and the Node sidecar on every push.

## Layout

```
config/          models.yaml (roles), budgets.yaml, sources.yaml (domain policy), concepts.yaml (XBRL)
src/primer/
  clients/       edgar, usaspending, samgov, firecrawl, ratelimit, cache
  evidence/      store (source_id registry), chunking, html
  sections/      briefs (all 13 templates), exhibits, style, sec09_financials
  graph/         main graph, researcher subgraph, critic
  render/        mdx, citations
  validate/      citation, numeric, tier, attribution, recency, coverage, page budget, mdx compile
  eval/          golden-table harness
tools/           mdx-check.mjs Node sidecar
tests/           pytest suite with recorded fixtures
```
