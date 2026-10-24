# Onboarding a company

How to add a company to the primer agent and know the result can be trusted. It is written from
the companies already run (Lockheed Martin, Boeing, Northrop Grumman, L3Harris, SpaceX) and the
faults each one exposed.

The agent is used on defense companies. Most of this applies to any SEC registrant.

## 1. Pick the path

The company's filing status decides which path the run takes. Decide before running.

| Situation | How to tell | What the run does | Typical coverage |
|---|---|---|---|
| Listed, has a 10-K | A ticker and an annual report on EDGAR (Lockheed Martin, Northrop Grumman, L3Harris) | Full path: 10-K, XBRL, proxy, earnings releases, transcripts | Sections 9, 11 and 13 can be full |
| Newly listed, no 10-K yet | A ticker, but EDGAR shows only an S-1 or 424B4 and 10-Qs (SpaceX, June 2026) | The IPO prospectus stands in for the 10-K and the 10-Qs supply the financials | Sections 9, 11 and 13 stay partial by design |
| Private | No SEC registrant, or you want to ignore the listing (`--private`) | Press, Form D and web research only; every figure is press-reported and unaudited | Every financial section partial at best |
| Foreign or no CIK | Not in the SEC ticker file | Treated as private | As private |

A company that has just listed changes path on its first 10-K. Re-run it then.

## 2. Before the first run

1. **Keys.** `.env` (never committed) needs `EDGAR_USER_AGENT` with a contact email, `FIRECRAWL_API_KEY`,
   `SAM_GOV_API_KEY`, and a model key. Production is OpenAI; `PRIMER_MODEL_PROVIDER=anthropic` switches
   to Anthropic. Run `primer info` and check that `llm`, `firecrawl`, `samgov` and `edgar_user_agent`
   are all `true`.
2. **Test the keys, do not assume.** A revoked model key does not stop a run: it falls back to
   template mode, the gate still passes and the judge reads "(template)". Check the judge column
   says `(llm)` after the first run.
3. **Firecrawl credits.** A run uses roughly 15 to 35 credits when its pages are cached and up to
   150 when they are not. Check the balance before a batch.
4. **SAM.gov quota.** Section 15 reads SAM.gov's Contract Awards API. A personal key with no SAM.gov role gets 10 requests a day; a key whose owner has a role, or a system account, gets 1,000. Set `SAM_GOV_DAILY_CAP` to what your key has. When the quota cannot cover the pull the section uses USAspending and says so.
5. **Sector.** The company's SIC code picks its sector tag from `config/sources.yaml`. Defense
   codes are covered. For another sector, add the SIC ranges, the trade-press domains, the
   section briefs and the peer fallbacks there. That is a config change, not code.

## 3. Name the company correctly

The name decides which filings, awards and press pages are found.

- **Public company:** pass the ticker. `primer run --company "Northrop Grumman" --ticker NOC`.
  The output folder is the ticker.
- **Name only:** the SEC ticker file is searched. A clear match proceeds. An ambiguous name
  ("Harris" matches L3Harris and a Harrison Street fund) asks on a terminal, and fails with the
  candidates when there is no terminal. Answer it with `--cik 0000202058` or `--ticker LHX`.
  The folder is the resolved ticker when the match is clear, and the name slug when the name is
  ambiguous or private.
- **Private company:** pass the **legal name** as `--company` and the everyday name as `--alias`
  (`--company "Space Exploration Technologies Corp" --alias SpaceX`). Filings and USAspending know
  the legal name, and trade press uses the everyday one. Using only "SpaceX" finds the press and
  misses the federal awards.
- **A subsidiary or a differently named registrant:** check which entity holds the contracts. USAspending
  records awards under recipient names and UEIs, and the run lists the UEIs it found in section 4.

## 4. Run it in two steps

Smoke test first, then everything.

```bash
uv run primer run --company "New Co" --ticker NEWCO --sections 2,3,9 --yes
```

Sections 2, 3 and 9 exercise the foundation pack, the 10-K or prospectus reading, and the
financial data. If they are wrong, the other eleven will be too. Then:

```bash
uv run primer run --company "New Co" --ticker NEWCO --yes
```

A full run takes five to nine minutes and about 500,000 model tokens. `primer resume RUN_DIR`
continues a run that was killed.

## 5. Read the result before trusting it

Open `output/{TICKER}/{run_id}/` and check these in order.

1. **Validation gate** (`run.json`, `validation.checks`): citations, tokens, frontmatter, numeric,
   tier, attribution, recency, coverage, page budget and exhibits must all be true. A failure names
   the sentence or row.
2. **Coverage in the frontmatter:** no section `unavailable`. A section that is unavailable or not
   applicable on a company you expected to be rich means a foundation problem (step 7).
3. **Foundation record** (`run.json`, `meta.foundation`): the fiscal year, the peers resolved,
   whether `annual_basis` is set (a prospectus run), and the government-revenue disclosure.
4. **Errors and counters** (`run.json`): `errors`, `firecrawl_credits`, `llm_calls_skipped_provider_down`
   (must be zero), `replay_misses` after a replay.
5. **The sections themselves.** Read them. The checklist is in the next section.

## 6. Section checklist

| Section | What to check | Fault seen |
|---|---|---|
| 2 Company Overview | The segment table is present and its total matches the filing | Segment names missing from the business text, so the table was never found |
| 3 Products | Products belong to the right segment; no customers or partners listed as products | A prospectus listed NASA and SES as SpaceX products |
| 4 Programs | The UEI set is the company's, not a namesake's; awards and ceilings are not added together | Wrong recipients when only the everyday name was used |
| 5 Market | Peers are real competitors with SEC filings | An acronym ("AGI") resolved to an unrelated registrant and became the only peer |
| 6 Strategy | Targets are dated; consensus columns are filled or absent, never a wall of n/a | A table of quotes with every consensus cell n/a |
| 7 Acquisitions | Recent deals appear with their 8-K | EDGAR full-text search is refused here, so section 7 read no 8-K until it used the filing index |
| 8 Management | The officer table lists the CFO and the board; ownership has the controlling holder | Only two officers parsed from a prospectus table |
| 9 Financials | Fiscal years are labelled right; 52/53-week filers have four years | L3Harris showed the wrong years until the calendar was read from the facts |
| 10 Operating | Reported results sit next to consensus; segment margins when the filing has a segment table | Segment profit sits in separate per-segment tables in a prospectus |
| 11 Capital | Debt, cash and maturities cite the right balance-sheet date | |
| 12 Thesis | Bull and bear rows are about this company; neutral ratings are not bull | Airbnb and Etsy upgrades in SpaceX's table; "Hold" counted as bull |
| 13 Valuation | Price and share count are dated; multiples are n/a rather than invented when there is no earnings year | A page-scraped share count labelled "XBRL cover page" |
| 14 Recent Developments | Everything inside twelve months; an event table, not boilerplate | An 8-K cover page read as the event |
| 15 Federal Contract Awards | Agency totals for 12 months, 3 and 5 years; the basis line says SAM.gov or USAspending; the top contracts are real PIIDs | A SAM.gov quota spent by earlier calls, so the section fell back to USAspending |

Two cross-checks catch most problems:

- **A transcript belongs to the company.** The run checks this, but confirm the speakers
  are the company's executives. A search once returned another company's earnings call.
- **Every number is cited and tiered.** A filing or XBRL figure beats a web page. Press-reported
  figures on the private path are labelled unaudited.

## 7. When a section is empty or wrong

Work from the cause, not the section.

| Symptom | Likely cause | What to do |
|---|---|---|
| Sections 2, 3, 5, 8, 9, 11 and 13 unavailable on a listed company | It has no 10-K yet, or the foundation could not read the filing | Check EDGAR for the company's filing list. A new listing uses the prospectus fallback automatically |
| Section 4 not applicable | The annual report does not disclose government revenue | Expected for a commercial company. For a defense company, check the disclosure sentence in `meta.foundation` |
| Peers empty | The competition text names no SEC registrants, or sits under a heading the reader missed | Add names to `peer_fallbacks` in `config/sources.yaml` |
| Financial sections wrong year labels | A 52/53-week fiscal year | Check `fiscal_year_end` and the labelled years in section 9 |
| Many `402` or `429` in `errors` | Firecrawl credits or rate limit | Check the balance; rerun the failed sections |
| Judge shows `(template)` | The model key failed | Fix the key and rerun |
| Gate fails on attribution | A sentence names a firm the cited source does not | Read the sentence. Legislation and rulebooks (NDAA, GAAP) are not firms |
| Gate fails on recency | Section 14 cites something older than twelve months | Find the source; the date may be wrong or the item stale |

## 8. Add it to the eval set

An onboarded company should be re-runnable and comparable. Add an entry to `config/eval.yaml`. It
is the only change needed.

```yaml
  - name: New Co
    ticker: NEWCO
    kind: rubric            # golden only when a reference primer exists
    flags: {}               # private: true for the private path
    as_of: "2026-10-24"
    sector: aerospace_defense
    note: >-
      Why this company is in the set: the thing it tests.
    expect:
      sector_tag: aerospace_defense
      fiscal_year_end: "1231"
      section9: [full, partial]
      self_consistency: pass
```

- `kind: golden` compares against a reference primer and a financials JSON. Those reference files
  are licensed: keep them out of git (`docs/golden/*.md` and `*.pdf` are ignored).
- `expect` states what must be true. State only what the company can honestly deliver: a newly
  listed company expects `partial` for section 9, not `full`.
- Run it: `uv run primer eval-set --run --only NEWCO`. It exits 1 when any entry fails.

## 9. Prove it replays

A run that cannot be reproduced cannot be audited.

```bash
uv run primer replay output/NEWCO/<run_id> --diff
```

It must report the primer, sources and claims identical, with no missed lookups and no network
violations. A replay of a run that used the model needs the recording that the run wrote
(`replay/` inside the run folder).

## 10. Definition of done

- [ ] Path chosen and the right flags used (`--private`, `--alias`, `--cik` as needed)
- [ ] Full run passes the validation gate; the judge column says `(llm)`
- [ ] No section unavailable that should not be; partial sections explain why in their coverage line
- [ ] Section checklist read, with no mislabelled figure, off-topic row or other company's page
- [ ] Peers are real; the UEI set is the company's
- [ ] Entry added to `config/eval.yaml` with honest expectations, and `eval-set --only` passes
- [ ] Replay is identical
- [ ] Anything new that broke is a test, not a note

## 11. Keep it current

- Re-run a newly listed company when its first 10-K is filed. It moves from the prospectus path to
  the full path and its financial sections can become full.
- Re-run before using a primer: it reflects the sources on its `as_of` date.
- Keys and credits expire. Check `primer info` and the Firecrawl balance first.

## 12. Worked example: a private company (Anduril)

Anduril Industries has no SEC registrant, so it takes the private path. This is what the
onboarding looked like and what it turned up.

```bash
uv run primer run --company "Anduril Industries, Inc." --alias Anduril --private --yes
```

1. **Preflight.** `primer info`, the Firecrawl balance, and a name search of the SEC ticker file
   (no match, so private is certain).
2. **First run, then read it.** The gate failed on attribution, section 4 had no program table,
   section 7 named one acquisition, and Form D discovery had failed. Each was a fault in the agent,
   not a fact about the company.
3. **What was wrong, and the fix:**

| Symptom | Cause | Fix |
|---|---|---|
| Gate failed on attribution | "Forge's estimated $123.86 billion": the source says "Forge", not "Forge's" | The possessive is stripped before the firm is checked |
| "EDGAR full-text search failed" in every run | The client called a URL EDGAR refuses (`/LatestSearch/`); the working one is `/LATEST/search-index` | Fixed the URL; Form D and section 7's 8-K search now work |
| A Form D from an investment vehicle read as Anduril's raise | The name match was "contains", and vehicles are named after the company ("Anduril Industries Feb 2025 a Series of CGF2021 LLC") | The Form D issuer must equal the company or an alias after legal suffixes |
| Section 4 empty although 200 awards were found | Programs were derived from a 10-K, and a private company has none | Programs come from the words the award descriptions repeat (Roadrunner, CUXS, Lattice, Altius, Towers), minus contracting vocabulary; awards naming none stay unassigned |
| Sections 7 and 8 stopped at five scrapes | Private sections rest on the web alone, and anduril.com failed on every scrape and each failure used the cap | Larger web caps for a private company; a failed scrape no longer uses the cap; a site that fails twice is skipped |
| Section 7 listed one deal | Deals needed a dollar figure, and acquisitions by a private company rarely state one | A sentence with the company as buyer gives a row with the value undisclosed |
| Contract values tabled as valuations and rounds | A $20 billion Army contract ceiling sat near the word "funding" | An amount near contract or award wording is not a round or a valuation |

4. **What a private primer will not have.** Financial statements, a proxy, ownership or pay. Sections
   9, 11 and 13 are press-reported and unaudited, marks diverge widely by source (Anduril: $61 billion
   at Series H, about $90 billion in a tender, $111 billion on one aggregator), and the table shows the
   range rather than choosing one.
5. **Eval entry and replay.** The entry is in `config/eval.yaml` with `private: true` and honest
   expectations (sections 9, 11 and 13 partial or unavailable). Replay of the run was identical with
   no missed lookups.

Expect a private run to need these checks on its first pass: the UEI set (is it the company's?), the
program names (are they products?), the Form D issuer (the company, not a vehicle), and every
valuation row (is the amount a valuation?).
