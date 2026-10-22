# Section handlers: the contract

Every researched section (2–14) is one module under `src/primer/sections/` named in
`graph/researcher.py: REGISTRY` (for example `sec04_programs.py`). The researcher subgraph
imports it lazily; a missing module yields an `unavailable` section, never a crash.

## Three functions

```python
def plan(brief: Brief, ctx: ToolContext) -> list[dict]
def collect(plan_items: list[dict], ctx: ToolContext) -> dict
def draft(brief: Brief, tool_results: dict, ctx: ToolContext, critic_notes: list[str] | None) -> SectionDraft
```

- `plan` returns the structured calls first, then web queries as
  `{"tool": "firecrawl.search", "query": "...", "why": "..."}` items. Expand templates with
  `common.expand_queries(brief, ctx, **extra_placeholders)`. The subgraph appends extra
  query items from the cheap `query_planner` model when a key is set.
- `collect` runs the structured calls (EDGAR, USAspending, SAM.gov, the market-data
  adapter, the foundation pack) and then `ctx.web_research([...queries...], ...)` for the
  web items. Everything fetched is registered in `ctx.store` by the clients themselves; the
  dict you return may hold Python objects (it never leaves the process).
- `draft` builds the exhibit deterministically (a `md_table` with a citation token after
  every value), the claims list, then the narrative: `common.draft_section(...)` when a model
  is configured, else `common.template_digest(...)`. Assemble with `common.finalize_draft`.

## ToolContext (`sections/common.py`)

| Attribute / method | What it gives |
|---|---|
| `ctx.entity`, `ctx.as_of`, `ctx.fy`, `ctx.co` | Entity (cik, ticker, aliases, sector_tag, exchange), as-of date, latest fiscal year label, common name |
| `ctx.store` | `EvidenceStore.for_run` singleton: `register`, `get`, `text`, `has`, `search_text` |
| `ctx.edgar` | `annual_financials`, `latest_instants`, `quarterly_series`, `filings`, `fulltext_search`, `exhibit_url`, `get_text`, `companyfacts`, `accession_index_url` |
| `ctx.firecrawl` | `FirecrawlClient` (None when no key and not offline). Prefer `ctx.web_research` / `common.scrape_url` so the budget is enforced |
| `ctx.usaspending`, `ctx.samgov` | USAspending client (always); SAM.gov client or None (no key) |
| `ctx.market` | `MarketDataAdapter` or None: `quote`, `shares_outstanding`, `consensus`, `price_targets`, `rating_changes`, `price_history`, `analyst_coverage`, `snapshot`; the scraped adapter also has `tipranks` |
| `ctx.foundation_sources(kind)` | source ids for `10-K`, `10-Q`, `DEF 14A`, `earnings_release`, `transcript`, `ir_map` |
| `ctx.item_text("1A")` | `(text, source_id)` of a 10-K Item (`kind="10-Q"`, `index=1` for the second 10-Q) |
| `ctx.chunks(kind)` | Item chunks of a foundation filing |
| `ctx.quarters()` | `[{label: "Q2-2026", report_date, accession, release_sid, transcript_sid}]`, newest first |
| `ctx.foundation["ir"]["links"]` | `{presentations, news, events, other}` URL lists from the IR site map |
| `ctx.foundation["peers"]` | `[{name, ticker, cik, mention}]` parsed from the 10-K competition text |
| `ctx.foundation["gov_revenue"]` | `{disclosed, sentence, pct, source_id}` from the 10-K |
| `ctx.budget` | `SectionBudget`: `can_search`, `can_scrape`, counters, `exhausted`, `notes` |
| `ctx.log(msg)` | records an error line in run.json without failing the section |

Filing text comes from `html_to_text` (`evidence/html.py`): table cells are joined with
` | `. `sections/text_tools.py` has the heuristics that read it: `find_summing_tables`,
`segment_table`, `segment_names_from_text`, `exec_officer_rows`, `risk_factor_headings`,
`paragraphs_with`, `sentences_with`, `money_in`, `gov_revenue_disclosure`,
`program_designations`.

## Rules every handler must keep

1. **No citation without a fetch.** The only citation form is the token `[src_0012]` for an
   id that exists in `ctx.store`. `draft_section` strips anything else; your template path
   must only emit ids you hold. Never pass raw URLs or pasted text to a model.
2. **Numbers prefer tier 1.** Statement figures come from XBRL (`ctx.edgar`), not from a
   news page, and carry the companyfacts source id. The tier validator flags the rest.
3. **Every number in an exhibit is a Claim** (`common.claim(section, metric, value, period,
   source_ids, unit, concept, note)`); metrics are snake_case and shared across sections
   where they mean the same thing (`revenue`, `net_income`, `total_debt`, `cash`, `eps_diluted`,
   `free_cash_flow`, `backlog`, `market_cap`, `enterprise_value`). Derived values use
   `concept="derived"` and put the formula in `note`. Ratios end in `_margin`, `_ratio`,
   `_leverage` or `_pct`; counts end in `_count`.
4. **Budgets are config.** Use `ctx.web_research` and `common.scrape_url`; they stop at the
   section caps in `config/budgets.yaml` and set `ctx.budget.exhausted`, which
   `finalize_draft` turns into a `> Coverage: partial` note. Never loop over searches.
5. **Coverage is honest.** `full` only when the required exhibit is populated and the brief's
   first source bucket was read; otherwise `partial`; `unavailable` only when nothing could be
   fetched. Section 4 uses `common.not_applicable_draft` when the 10-K discloses no
   US-government revenue (`ctx.foundation["gov_revenue"]["disclosed"]` is falsy).
6. **House style.** `finalize_draft(caveat=common.caveat_line("..."), ...)`; narrative first,
   exhibit under a `###` heading, coverage line last. No first person, no recommendations.
   Perspectives attributed to a firm only when a cited source's text names the firm.
7. **Conflicts stay visible.** When two sources disagree, keep both claims; `assemble` renders
   the `> Conflict:` blockquote. Do not pick one silently.
8. **Section 4 program rule** (requirements §7): every value in the program, location and
   agency tables cites a government record whose source has a `gov_id`; `role: not confirmed`
   when no record names prime/sub; obligated, ceiling and announced values are labelled;
   status language only from gao.gov / defense.gov / comptroller PDFs.
9. **Recent events reach the sections that need them.** Section 14 runs in the first wave;
   sections 3, 6, 8, 10 and 12 run in a second wave with its dated event log on
   `ctx.recent_events` (`common.recent_events(ctx)` returns `{date, event, detail, source_id}`
   rows; `common.recent_events_block(ctx)` is the prompt fragment `draft_section` adds
   automatically, and those source ids are citable). A settled event is never described as
   pending or absent.
10. **Prose is for the reader.** `finalize_draft` runs `common.scrub_pipeline_vocabulary`
    (rewrites "the exhibit", "structured facts", "the fetched evidence"; moves sentences about
    search or scrape caps into the coverage line) and `common.drop_first_party_perspectives`
    (a `### Perspectives` list survives only when a bullet cites a second-party source: trade
    press, GAO, a peer filing, a rating agency; the company's own documents and Wikipedia do
    not count). Do not rely on it to clean a template path that is sloppy by design.
11. **Exhibits are validated before scoring.** The critic runs `validate/exhibits.py`
    (`apply_exhibit_check`): a table with "n/a" segments the prose names, cover-page or
    forward-looking boilerplate, numerals stripped from program names, ownership labels that
    are not people or institutions, transcript timestamps or scraped page chrome is dropped
    whole, its claims are pruned, coverage becomes `partial`, and the coverage line says why.
    The prose is kept. Build the exhibit so it passes rather than counting on the drop.
12. **Cross-section reconciliation** (`graph/reconcile.py`) runs after the second wave: a
    sentence saying a fact is absent from the sources is replaced with a cross-reference when
    another section has it, and two sections giving different values for a point-in-time
    attribute get a `> Conflict:` line. Write denials in the standard forms ("no X appears in
    the sources reviewed", "X cannot be confirmed") so the matcher can read them.

## Tests

One test module per handler under `tests/`, offline, on `edgar_mock` plus `firecrawl_mock(...)`
and `usaspending_mock` from `tests/conftest.py`. The fake 10-K / 10-Q / DEF 14A / release /
8-K documents there already carry segment tables, officers, risk factors, a debt note, an
acquisitions note, guidance and quarterly results. A handler test runs the real subgraph:

```python
from primer.graph.researcher import build_researcher
from primer.graph.main import new_run, intake, resolve_entity, foundation_pack, plan_sections

def _state(inputs):
    run_id, run_dir = new_run(inputs)
    st = {"inputs": inputs.model_dump(mode="json"), "run_id": run_id, "run_dir": str(run_dir)}
    for node in (intake, resolve_entity, foundation_pack, plan_sections):
        st.update(node(st))
    return st

out = build_researcher().invoke({"run_dir": st["run_dir"], "as_of": st["as_of"], "entity": st["entity"],
                                 "foundation": st["foundation"], "brief": st["plan"][N], "iteration": 0, "critic_notes": []})
draft = out["drafts"][N]
```

Assert: the exhibit is present, every claim has source ids that exist in the store, no
`[src_` token points at an unknown id, coverage is `full`/`partial` as expected, and the
budget counters stayed within `config/budgets.yaml`. Keep fixtures small and hand-written for
Firecrawl pages (credits are limited); record real responses only for free APIs
(`tests/fixtures/make_fixtures.py`).
