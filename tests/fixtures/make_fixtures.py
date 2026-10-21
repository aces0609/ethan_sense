"""Record trimmed EDGAR fixtures for the test suite.

Run once (needs EDGAR_USER_AGENT; uses the on-disk cache when present):
    uv run python tests/fixtures/make_fixtures.py

Writes tests/fixtures/edgar/{company_tickers_exchange,submissions_LMT,companyfacts_LMT}.json,
trimmed to the concepts in config/concepts.yaml and periods ending 2020 or later so the
files stay small enough to commit.
"""

from __future__ import annotations

import json
from pathlib import Path

from primer.clients.edgar import EXCHANGE_TICKERS_URL, EdgarClient
from primer.config import concepts_config

HERE = Path(__file__).parent / "edgar"
HERE.mkdir(parents=True, exist_ok=True)
KEEP_TICKERS = {"LMT", "BA", "NOC", "RTX", "GD"}


def main() -> None:
    edgar = EdgarClient()

    tickers = edgar._get(EXCHANGE_TICKERS_URL)
    fields = tickers["fields"]
    rows = [
        r for r in tickers["data"] if dict(zip(fields, r, strict=False))["ticker"] in KEEP_TICKERS
    ]
    (HERE / "company_tickers_exchange.json").write_text(
        json.dumps({"fields": fields, "data": rows}), encoding="utf-8"
    )

    cik = edgar.resolve_ticker("LMT")["cik"]
    subs = edgar.submissions(cik)
    recent = subs["filings"]["recent"]
    keep_idx = [i for i, f in enumerate(recent["form"]) if f in ("10-K", "10-Q", "8-K", "DEF 14A")][
        :60
    ]
    trimmed = {k: [v[i] for i in keep_idx] for k, v in recent.items() if isinstance(v, list)}
    subs_out = {k: v for k, v in subs.items() if k != "filings"}
    subs_out["filings"] = {"recent": trimmed, "files": []}
    (HERE / "submissions_LMT.json").write_text(json.dumps(subs_out), encoding="utf-8")

    facts = edgar.companyfacts(cik)
    cfg = concepts_config()
    wanted: dict[str, set[str]] = {}
    for metrics in cfg["statements"].values():
        for spec in metrics.values():
            wanted.setdefault(spec.get("taxonomy", "us-gaap"), set()).update(
                spec.get("concepts", [])
            )
    out_facts: dict = {}
    for tax, concepts in wanted.items():
        for concept in concepts:
            node = facts.get("facts", {}).get(tax, {}).get(concept)
            if not node:
                continue
            units = {}
            for unit, entries in node["units"].items():
                kept = [
                    e
                    for e in entries
                    if e.get("end", "") >= "2020-01-01" and e.get("form", "").startswith("10-K")
                ]
                if kept:
                    units[unit] = kept
            if units:
                out_facts.setdefault(tax, {})[concept] = {
                    "label": node.get("label"),
                    "units": units,
                }
    (HERE / "companyfacts_LMT.json").write_text(
        json.dumps({"cik": facts["cik"], "entityName": facts["entityName"], "facts": out_facts}),
        encoding="utf-8",
    )
    for p in sorted(HERE.glob("*.json")):
        print(p.name, p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
