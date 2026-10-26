# Architecture

How the primer agent is put together and how its parts work together. The graphs are LangGraph
state graphs: `src/primer/graph/main.py` (the pipeline) and `src/primer/graph/researcher.py`
(one section researcher). The diagrams below render on GitHub. Purple boxes are steps where a
model writes or judges; teal boxes are plain code and rules.

## The pipeline

```mermaid
flowchart TD
    classDef model fill:#EEEDFE,stroke:#534AB7,color:#26215C
    classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C

    intake["Intake<br/>CLI inputs, budgets"]:::code
    resolve["Resolve entity<br/>CIK, UEIs, private?"]:::code
    foundation["Foundation pack<br/>10-K, XBRL, market data"]:::code
    plan["Plan sections<br/>one brief each"]:::model
    intake --> resolve --> foundation --> plan

    subgraph fan["Section researchers, run in parallel"]
        wave1["Wave 1<br/>section 14 and the independent sections"]:::model
        wave2["Wave 2<br/>sections 3, 6, 8, 10, 12 get section 14's events"]:::model
        wave1 -- "dated events" --> wave2
    end
    plan --> wave1

    reconcile["Reconcile<br/>fix cross-section gaps"]:::model
    takeaways["Key takeaways<br/>section 1 summary"]:::model
    assemble["Assemble<br/>footnotes, front matter"]:::code
    validate["Validate<br/>gate: numbers, citations"]:::code
    review["Review<br/>optional human pause"]:::code
    export["Export<br/>primer.md, evidence"]:::code
    wave2 --> reconcile --> takeaways --> assemble --> validate
    validate -- "failing sections, max 2 retries" --> fan
    validate --> review --> export
```

1. **Setup** runs once, in order. Intake reads the inputs and budgets, resolve entity finds the
   CIK and the SAM.gov/USAspending UEIs (or the private-company path), the foundation pack reads
   the 10-K, XBRL facts and market data, and one strong-model call plans a brief per section.
2. **Fan-out** runs the section researchers in parallel, in two waves. Wave 1 is section 14
   (Recent Developments) plus every section that does not read it. Wave 2 is sections 3, 6, 8, 10
   and 12, which are given section 14's dated events. When section 14 is not in the plan there is
   one wave.
3. **Reconcile** replaces "not in the evidence" sentences with cross-references to the section
   that has the fact, and raises a Conflict line where sections disagree. **Key takeaways** writes
   section 1 from the finished drafts.
4. **Assemble** builds the document (footnotes, front matter), and **validate** runs the gate:
   citations resolve, numbers match their claims, tiers and recency, exhibits, page budget. A
   failing section goes back to its researcher with the gate's notes, at most twice.
5. **Review** is a human pause that only happens in debug mode. **Export** writes `primer.md`,
   `claims.json`, `sources.json` and the evidence files into the run folder.

A SQLite checkpointer in the run folder lets a killed run resume at the failed node.

## One section researcher

```mermaid
flowchart TD
    classDef model fill:#EEEDFE,stroke:#534AB7,color:#26215C
    classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C
    classDef src fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A

    subgraph loop["Section researcher, one per section"]
        pq["Plan queries<br/>cheap model"]:::model
        ct["Call tools<br/>fetch, within budget"]:::code
        dr["Draft<br/>strong model, cited"]:::model
        cr["Critic<br/>strong model, rubric"]:::model
        pq --> ct --> dr --> cr
        cr -. "not satisfied: retry with notes, up to the loop cap" .-> pq
    end

    edgar["SEC EDGAR<br/>10-K, 8-K, XBRL"]:::src
    usa["USAspending<br/>awards, obligations"]:::src
    sam["SAM.gov<br/>contract awards, UEIs"]:::src
    web["Web via Firecrawl<br/>press, IR, DoD news"]:::src
    edgar --> ct
    usa --> ct
    sam --> ct
    web --> ct

    store["Evidence store<br/>every fact gets a source ID and tier;<br/>claims feed the numeric gate"]:::code
    edgar --> store
    usa --> store
    sam --> store
    web --> store
```

Each section module follows the handler contract in [HANDLERS.md](HANDLERS.md): `plan`,
`collect`, `draft`. The researcher loop wraps it: plan the queries, collect from the sources
within the section's budget (`config/budgets.yaml`), draft with citations, and let the critic
send it back with notes until it passes or the loop cap is reached. Every fetched fact is
registered in the evidence store with a source ID and a tier (1 for filings and government
APIs), and the numbers in the draft are recorded as claims that the validation gate later checks
against it.

## Which model does what

Set in `config/models.yaml`; a role is a tier (`strong` or `cheap`) for the chosen provider.

| Role | Tier | Used by |
|---|---|---|
| `planner` | strong | plan sections |
| `query_planner` | cheap | plan queries, inside each researcher |
| `tool_loop` | cheap | the call-tools loop |
| `drafter` | strong | draft |
| `critic` | strong | critic |
| `synthesizer` | strong | key takeaways |
| `reconciler` | cheap | reconcile (one structured match call) |
| `judge` | strong | eval harness rubric |

Without a working model key the run falls back to template mode: the gate still passes but
nothing is model-drafted (the eval judge column then reads "(template)").
