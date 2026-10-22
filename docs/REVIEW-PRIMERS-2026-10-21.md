# Review: generated primers vs the AlphaSense references

Date: 2026-10-21. Reviewer: Claude (editorial read of both primers in full, plus two claim-by-claim comparisons against fact digests of the AlphaSense reports).

| Primer | Generated run | Reference |
|---|---|---|
| Lockheed Martin | `output/LMT/2026-10-21-4e16a602/primer.mdx` (14 sections, 109 sources, 1,238 citation marks) | AlphaSense Company Primer, 42 pp., 143 sources |
| Boeing | `output/BA/2026-10-21-0ba2487c/primer.mdx` (14 sections, 104 sources, 1,010 citation marks) | AlphaSense Company Primer, 37 pp., 141 sources |

The reference texts live at `docs/golden/lmt.md` and `docs/golden/ba.md` (not committed; licensed).

## 1. Verdict

The pipeline does what the requirements asked of the *data* half of a primer and does it better than the reference: every hard financial number the two documents share agrees to the dollar, the derived figures carry their formulas, leverage carries its definition, and the generated primers flag disagreements with `> Conflict:` lines where the references silently print contradictory broker numbers. Sections 9, 11 and 13 are production-grade.

It does not yet do the *analysis* half. The references are roughly half broker research by citation count, and that layer maps to nothing in the generated primers except rating and target actions scraped from MarketBeat and StockAnalysis, plus verbatim analyst questions. This is the known v1 limit, disclosed in the frontmatter, and the paid swap is the only real fix. The reviewer's eye should rest instead on the three things the pipeline *could* do better with the sources it already has:

1. **Cross-section consistency.** The requirements made this the headline improvement over AlphaSense. Each primer has 7 to 9 real conflict lines, but 8 (Boeing) and 11 (Lockheed) further cross-section contradictions go unflagged, and several are of the form "X is not in the evidence" in one section while another section prints X from a fetched source.
2. **Auto-built exhibits.** Three tables per primer are broken or mislabelled, and one (Lockheed section 8 "Ownership concentration") presents incentive-plan metric weights as percentages of shares beneficially owned, which would mislead a reader.
3. **Recency inside the window.** Facts from the three weeks before the as-of date (SPEEA ratification on Oct 1, Q3 deliveries on Oct 13, MAX-7 certification in August) were fetched for one section and not propagated to the sections that needed them.

Measured against the Phase 2 exit gate, both primers pass. Measured against "an analyst would edit this rather than rewrite it", Lockheed is close and Boeing is a section or two short, mostly because its M&A and governance exhibits failed to build.

## 2. Scorecard

| Measure | Lockheed Martin | Boeing |
|---|---|---|
| Validation gate (8 checks) | passed | passed |
| Judge rubric vs reference, min / mean section score (model) | 3.17 / 3.97 | 3.0 / 3.76 |
| Fact overlap with reference (harness) | 39% | 30% |
| Shared financial cells matching exactly | 36 of 36 (FY2022–FY2025 P&L, cash flow, balance sheet; Q2-2026) | 20 of 20 on revenue, EBITDA, OCF, capex, FCF, debt, cash; net debt differs in all four years (see §5) |
| Reference's 25 most distinctive facts: present / partial / missing | 2 / 12 / 11 | 4 / 11 / 10 |
| Numbers that disagree with the reference | 2 minor (Rapid Solutions announce date, PAC-3 base rate 600 vs 620) | net debt and EV (definitional), MAX-10 backlog share, next 737 rate step (52 vs 53) |
| `> Conflict:` lines, all real | 9 | 7 |
| Cross-section contradictions without a conflict line | 11 | 8 |
| Required exhibits broken or missing | 3 (products table, ownership table, peer-list positioning) | 5 (products table, deal table missing, officer and ownership tables missing, targets-vs-consensus table) |
| Sources: tier 1 / tier 2 | 63 / 46 | 59 / 45 |
| Sources undated ("n.d.") | 50 of 109 | 52 of 104 |
| Wall-clock, tokens | 2.8 min, 386k | 4.4 min, 522k |

## 3. Where the generated primers are stronger than the references

- **Financial statements.** Neither reference has an income statement or balance sheet table; both generated primers have four-year P&L, cash flow, balance sheet and ratio tables with every cell cited to the filing accession and restatements noted.
- **Government contracting.** USAspending obligations by program and agency, SAM.gov registrations, DoD announcement values kept separate from obligations and ceilings, GAO report numbers with dates. The references have none of this.
- **Primary-document events.** 8-K-dated revolver terms and covenants, annual-meeting vote counts, pension annuity buy-outs, officer pay packages. The Boeing primer's Malave package and Citibank revolver covenants, and Lockheed's $2.25bn revolver replacement, are absent from the references.
- **Honest reconciliation.** Leverage rows carry their definition ("net debt at 30 June 2026 ÷ FY2025 EBITDA"); the Boeing reference prints 15.26x, >4.5x and 6x from three sources with no reconciliation. The Lockheed reference reports net leverage as 1.2x, 1.6x and ~2.0x the same way.
- **Second-hand attribution is labelled.** Every broker name in both generated primers is tagged "(MarketBeat)", "per StockAnalysis" or "asked on the call". No sentence attributes a view to a firm without naming the carrier. The attribution validator is doing its job.
- **Things the references got wrong that the generated primers avoid.** The Boeing reference contradicts itself twenty times (42 vs 47 per month across four brokers, two Bloomberg consensus rows, 30,000 vs 33,000 strikers, $24.3bn + $10bn vs $18.2bn + $5.7bn raises). The generated Boeing primer has none of those because it has one source per fact; where it has two it says so.

## 4. What is missing relative to the references

### 4.1 The broker layer (structural; paid swap)

Everything the references build their argument from: unit-rate forecasts (UBS PAC-3 620 → 2,050), capex paths (Jefferies $2.3bn/$2.75bn), target methodologies (Seaport 23x 2028 FCF, RBC 19.4x on $28.33, Morgan Stanley 18x 2028 P/FCF), sum-of-the-parts, peer forward multiples, FCF forecasts by house, expert-call quotes. The generated primers have rating actions and targets without reasoning. Section 12 in both is therefore a rating-change log plus a list of analyst questions, not a thesis.

### 4.2 Program and operating colour the free web does carry but the run did not fetch

Lockheed: Vectis CCA, JATM/Australia export, Hypersonics SIL, Sikorsky/Terran deal terms, LM Ventures, FMS and budget context (none anywhere). Boeing: F/A-XX and F-47 (nothing on either sixth-generation fighter), shadow factories, Renton wing bottleneck, Ryanair as the MAX-10 anchor customer, the door-plug incident, the FAA safety plan, ACSAA, the 2018–19 accidents, the Caremark dismissal, Q3-2026 deliveries. These are in trade press and Boeing's own releases. The coverage lines explain why: "12 planned web queries dropped at the section search cap" (section 3, both primers), "11 dropped" (Lockheed section 5), "research stopped at the section budget (scrape cap 6 reached)" in five sections of each. Each run spent 2 to 170 Firecrawl credits against a 420-credit budget; the per-section caps bind long before the per-primer cap does.

### 4.3 Recency within the window

- Boeing section 8 says the SPEEA agreements "were scheduled to expire in October 2026"; they were ratified on Oct 1, and section 12 itself cites "the averted engineers' strike".
- Boeing section 3 says "No certification or first-delivery date appears in the evidence" for the 737-7; section 14 reports its FAA approval in early August.
- Boeing section 3: "Boeing published third-quarter 2026 deliveries on October 13, 2026, but the evidence excerpt does not contain the figures" (the cited source is a newsroom index page).
- Lockheed section 12 lists "When the PAC-3 and THAAD multiyear contracts will be signed" as an open question; section 6 reports THAAD signed in Q2 and section 14 reports PAC-3 converted in July.

## 5. Errors in the generated primers

Ranked by how much they would mislead a reader.

| # | Primer, section | Error | Cause |
|---|---|---|---|
| 1 | LMT §8 | "Ownership concentration" lists Sales 20.0%, FCF 40.0%, Financial 70.0% … "of shares beneficially owned". These are the proxy's incentive-plan metric weights. | Exhibit builder matched the wrong proxy table |
| 2 | BA §9, §11, §13 | Net debt $43,177m vs reference $24,698m (FY2025); EV $190.6bn vs $176.4bn; EV/EBITDA 30.6x vs 26x | Net debt nets cash only; Boeing holds ~$18bn of short-term investments. Needs `ShortTermInvestments` / `MarketableSecuritiesCurrent` in `concepts.yaml` |
| 3 | BA §5 | "46% of FY2025 revenue as U.S. Government share". The 10-K sentence describes non-U.S. customers including FMS. The primer flags it in a conflict line but still places it in the SWOT Strengths column | Structured label inverted the sentence |
| 4 | LMT §1, §13 | "the lowest EV/EBITDA among the computed peers" while its own peer table shows NOC at 14.7x below LMT's 15.3x; prose also says NOC and RTX multiples "were not completed" while the table prints them | Prose drafted before the peer table finished |
| 5 | BA §10 | Q4-2025 diluted EPS 10.73 and net earnings 8,220 derived as FY minus nine-month YTD; the release says 10.23. Quarterly EPS cannot be derived by subtracting YTD EPS | Derivation method |
| 6 | BA §2 | "FY2025 employee count of 3,200" (the IAM 837 St. Louis headcount); §8 says ~182,000 | Regex picked the wrong sentence; the primer suspects it but does not raise a conflict |
| 7 | LMT §3, BA §3 | Products table "Key metric" cells: "35 type aircraft", "3 missiles", "130 aircraft", "1 Deliveries (Any forward-looking statement…)" | Regex residue from program names and footnote markers |
| 8 | LMT §5, BA §5 | Peer-list positioning cells contain 10-K cover-page check-box text and truncated boilerplate | Chunker took the first sentence of the filing, not Item 1 |
| 9 | LMT §10 | Backlog column assigns $179bn to Q4-2025 and $194bn to Q2-2025 (both one quarter off); flagged in a conflict line but the table and `claims.json` keep the wrong values | Period labelling in the KPI extractor |
| 10 | LMT §6, BA §6 | "FY2026 consensus revenue is $81.0 billion ." with the footnote marker stripped (same for EPS; Boeing's −$1.10 / $97.5bn is uncited twice) | Citation token dropped in the targets-table drafter |
| 11 | LMT §7 | Rapid Solutions announce date 2025-06-26 is the completion date (reference: 2025-04-23); exhibit has one row though Ultra Maritime ($3.45bn, pending) is discussed in prose | Deal extractor keyed on the press release it found |

## 6. Internal consistency: the unflagged contradictions

The requirements asked for a numeric-consistency pass over (metric, period) groups. It works for numbers that share a metric name. It does not catch the pattern that dominates both primers: one section declaring a fact absent while another section has it.

**Lockheed (11):** FY2026 FCF guidance "over $7 billion" (§6) vs "$7.0–7.2 billion" (§1, §10); §6 "No management EPS figure is in evidence" vs §10 EPS guidance $29.95–30.65; §6 "No numeric capex guidance" vs §10 OCF and FCF ranges that imply it; §11 revolver "not found" vs §14 $3.0bn → $2.25bn; §11 dividend "not in the captured evidence" vs §12 $3.60; §3 "No PAC-3 production rate is disclosed" vs §12/§14 600 → 2,000; §12 PAC-3/THAAD multiyear timing as an open question vs §6/§14 signed; §2 "the 10-K excerpt does not describe the MFC, RMS or Space segments" vs §5/§6 program lists from the same 10-K; §2 "does not quantify the split of sales" vs §5 72%/63%; four consensus-target values ($618.86, $628.21, $627.10, $613.94) across §1, §5, §12, §13 with only two pairwise conflict lines; market cap $122.9bn (§2, undated) vs $127.3bn (§13).

**Boeing (8):** MAX-10 "nearly a quarter" of 737 backlog (§1, §3) vs "roughly 30%" (§12); next 737 step 52 (§6) vs 53 (§3); share price $192.28 (§13) vs $189.98 (§12); employees 3,200 (§2) vs ~182,000 (§8, §12); 737-7 certification absent (§3) vs approved (§14); SPEEA pending (§8) vs averted (§12); revolver "n/a" (§11) vs $3.0bn 364-day facility (§14) and the Q4 call's "$10 billion of revolving credit facilities, all undrawn" sitting unused in fetched evidence; Spirit assumed debt ~$3.6bn (§7) vs ~$4bn (§11).

## 7. Sourcing and citation hygiene

- **Half the sources are undated.** 50 of 109 and 52 of 104 carry "n.d.", including every GAO report, every DoD announcement, both Yahoo-hosted transcripts and all rating-agency pages, even where the date is in the URL or the body text ("GAO-25-107632 (2025-09-03)").
- **SEC documents typed as "news".** Lockheed's CFO-transition 8-K appears as `News • www.sec.gov • n.d. • "lmt-20250416"`; Boeing's Spirit announcement 8-K exhibit as `News • www.sec.gov • n.d. • "https://…/d838733dex991.htm"`; both 10-Ks are duplicated under a second "news" id. This misstates the tier in `sources.json`.
- **Non-reproducible citations.** USAspending search pulls cite `https://www.usaspending.gov/search` (five footnotes, identical URL); Boeing's newsroom index page `[^4]` carries five facts including the MAX-10 certification; Lockheed `[^70]` cites a Fitch page "whose content was not captured".
- **Concentration.** Boeing section 12 rests 87 citation marks on 9 sources, more than 60 of them on one MarketBeat page; section 9 in both rests on 4 XBRL records (acceptable, but the critic's "distinct sources" score should weight this).
- **Attribution rule holds.** No sentence of the form "Firm expects…" is backed by a non-firm source in either primer.
- **Sources appendix formatting.** Titles with embedded newlines and tabs (`[^2]`, `[^5]`, `[^33]`, `[^34]`, `[^49]–[^51]`, `[^78]`, `[^80]` in Boeing); GAO entries put the report title in the publisher field.

## 8. Prose and structure

- **Pipeline language leaks into the document.** "The exhibit's '1 Deliveries' entries … should not be read as delivery counts", "The structured extract records…", "research stopped at the section budget (scrape cap 6 reached)", "9 planned web queries dropped at the section search cap", a transcript timestamp inside a table ("Kelly Ortberg President and CEO 0:04:03"), scraped page chrome in event logs ("Follow your favorite stocksCREATE FREE ACCOUNT", "Type topic Tags Categories Search Log In"). An analyst can live with the coverage lines; the rest should not reach the page.
- **"Not in the evidence" as filler.** 23 (Boeing) and 30+ (Lockheed) sentences of the form "does not show / cannot be sourced / no … appears in the evidence". Several are false within the document (§6 above).
- **Perspectives blocks that are not perspectives.** "Wikipedia: presents Taiclet's chairman title as dating from March 2021"; "The Motley Fool: notes that sales growth normalized … was 5%" (that is management's statement). The house-style template forces a Perspectives block into every section whether or not a second-party view exists.
- **Targets-vs-consensus tables** (§6, both) are four pasted call sentences labelled "Prepared remarks" with consensus "n/a"; only Lockheed's sales row is a real target/consensus pair.
- **Bull/bear tables** (§12, both) are MarketBeat rating actions sorted into two columns plus 10-K risk sentences and, in Lockheed's case, a Pillar 2 tax sentence as a "bull" point. They contain no argued case.
- **Event logs** (§14) are mostly 8-K filing rows whose "Implication" cell is the filing's first sentence; the caveat says the column "is not an assessment" while several cells are assessments ("Delivery throughput, not approvals, is now the main constraint on cash conversion").
- **Repetition.** The segment description appears twice (§2, §3); $715bn backlog appears in five Boeing sections; FY2025 operating income, D&A, EBITDA and FCF rows are reprinted in §9, §11 and §13.
- **Key Takeaways** are faithful to the sections but omit M&A, management, capital allocation and recent developments entirely; Boeing's bullet 7 carries ten footnotes for one sentence.
- **What reads well.** §9 and §11 narrative in both; Lockheed §4 (the ratio-within-award framing, the agency-mix direction, the explicit prime/sub rule); Boeing §4 (Eagle Crest ceiling vs $343,740 obligated); the caveat lines, which are specific and honest throughout.

## 9. Recommended fixes, in priority order

1. **Cross-section reconciliation node** between fan-out and assemble. Collect every "not in evidence / cannot be confirmed" sentence and every (entity, attribute) claim per section into a registry; when another section has the attribute from a fetched source, replace the denial with a cross-reference or raise a conflict line. This single change would have fixed 15 of the 19 unflagged contradictions in §6.
2. **Exhibit validators.** Reject any table row whose segment is "n/a" when the 10-K names the segment, any cell matching cover-page or forward-looking boilerplate, any "Key metric" that is a numeral stripped from a program name, and any ownership table whose labels are not shareholder names. Mark the exhibit `partial` and keep the prose rather than printing the broken table.
3. **Net debt and EV** must net short-term investments and marketable securities; add the concepts with fallbacks to `concepts.yaml` and print the formula. Drop YTD-subtraction for per-share and attributable figures; take Q4 EPS from the release.
4. **Dates and types on sources.** Parse dates from URLs and page metadata before writing "n.d."; type anything under `sec.gov/Archives` by its form; never register an index or listing page as a source; cite USAspending pulls by the award URL or the API request, not the search page.
5. **Propagate section 14 forward.** Run section 14 before 3, 6, 8, 10, 12 (it is cheap) and pass its dated events into their briefs so recency inside the window is enforced, not just checked.
6. **Raise the per-section search caps** for sections 3, 5, 6, 12 and 14. Credits are not the constraint: the runs used under half the per-primer budget while every narrative section reports dropped queries. Target the program-colour and trade-press facts in §4.2 explicitly in the briefs (sixth-generation fighters, competitor fleet comparisons, regulator actions, labour agreements).
7. **Mine the earnings-call answers, not only the questions.** Section 12's "unanswered questions" list quotes the question and stops; management's answer is in the same transcript and is the closest free proxy to broker debate.
8. **Strip pipeline vocabulary from prose** ("the exhibit", "structured facts", "scrape cap", "fetched"); keep it in the coverage blockquote only. Drop the Perspectives block when no second-party view exists.
9. **Employee count, customer split and officer table** should come from named 10-K items (Human Capital, segment note, Part I executive officers) rather than regex over the whole filing.
10. **Broker layer.** No v1 fix. The FMP or AlphaSense transcript and consensus endpoints are the first paid swap, as the requirements already say; until then the frontmatter disclosure is correct and should stay.

## 10. Files

- Comparison working notes (claim by claim, with footnotes): scratchpad `golden/lmt_comparison.md`, `golden/ba_comparison.md`; fact digests of the references: `golden/lmt_digest.md`, `golden/ba_digest.md`. These are local scratch files, not in the repo.
- Eval scorecards: `output/eval/scorecard-2026-10-21-4e16a602.json`, `output/eval/scorecard-2026-10-21-0ba2487c.json`.
