# Generated primers

The latest passing run of each company in the eval set (config/eval.yaml), generated 2026-10-26 with
the model enabled and a green validation gate. Section 15 (federal contract awards) is sourced from
SAM.gov for Lockheed Martin and from USAspending for the others; each section says which.

| File | Company | Run | Judge min / mean |
|---|---|---|---|
| lmt.md | Lockheed Martin | 2026-10-21-2f8af90b | 3.33 / 4.07 |
| ba.md | Boeing | 2026-10-21-8c73eeca | 3.17 / 4.00 |
| spcx.md | SpaceX | 2026-10-23-a741b9ae | 3.50 / 3.99 |
| anduril.md | Anduril Industries (private) | 2026-10-24-f58fe86d | 3.50 / 4.00 |
| noc.md | Northrop Grumman | 2026-10-22-1c5c7102 | 3.50 / 3.95 |
| lhx.md | L3Harris | 2026-10-22-46c10430 | 3.83 / 4.16 |

These are snapshots; the full run folders (evidence, claims, replay) stay in the gitignored
`output/`. Regenerate with `primer run` or `primer eval-set --run`.
