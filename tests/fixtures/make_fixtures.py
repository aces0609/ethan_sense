"""Record trimmed fixtures for the test suite from the free APIs (EDGAR, USAspending).

Run once (needs EDGAR_USER_AGENT; uses the on-disk cache when present):
    uv run python tests/fixtures/make_fixtures.py            # everything
    uv run python tests/fixtures/make_fixtures.py edgar      # or: usaspending

EDGAR: tests/fixtures/edgar/{company_tickers_exchange,submissions_LMT,companyfacts_LMT}.json,
trimmed to the concepts in config/concepts.yaml and periods ending 2020 or later so the files
stay small enough to commit. The submissions index keeps 10-K, 10-Q, 8-K and DEF 14A rows
(with their `items`) so the foundation pack and sections 7, 8 and 14 can run on it.

USAspending: tests/fixtures/usaspending/*.json, the Lockheed responses section 4 consumes:
the name-discovery award page, the UEI-filtered award page, awarding-subagency categories
for two fiscal years, and one award detail (trimmed to the fields the handler reads).

Firecrawl pages are never recorded here: credits are limited and the aggregator pages change
layout, so handler tests use hand-written markdown with `firecrawl_mock`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from primer.clients.edgar import EXCHANGE_TICKERS_URL, EdgarClient
from primer.config import concepts_config

HERE = Path(__file__).parent
EDGAR_DIR = HERE / "edgar"
USA_DIR = HERE / "usaspending"
KEEP_TICKERS = {"LMT", "BA", "NOC", "RTX", "GD"}
USA_API = "https://api.usaspending.gov/api/v2"
AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Recipient UEI",
    "recipient_id",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Description",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Contract Award Type",
    "NAICS",
    "PSC",
    "Place of Performance City Code",
    "Place of Performance State Code",
    "Place of Performance Country Code",
    "generated_internal_id",
    "Last Modified Date",
    "Base Obligation Date",
]


def record_edgar() -> None:
    EDGAR_DIR.mkdir(parents=True, exist_ok=True)
    edgar = EdgarClient()

    tickers = edgar._get(EXCHANGE_TICKERS_URL)
    fields = tickers["fields"]
    rows = [
        r for r in tickers["data"] if dict(zip(fields, r, strict=False))["ticker"] in KEEP_TICKERS
    ]
    (EDGAR_DIR / "company_tickers_exchange.json").write_text(
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
    (EDGAR_DIR / "submissions_LMT.json").write_text(json.dumps(subs_out), encoding="utf-8")

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
    (EDGAR_DIR / "companyfacts_LMT.json").write_text(
        json.dumps({"cik": facts["cik"], "entityName": facts["entityName"], "facts": out_facts}),
        encoding="utf-8",
    )
    for p in sorted(EDGAR_DIR.glob("*.json")):
        print(p.name, p.stat().st_size, "bytes")


def record_usaspending() -> None:
    USA_DIR.mkdir(parents=True, exist_ok=True)
    http = httpx.Client(timeout=120.0, headers={"User-Agent": "ethan_sense fixtures"})

    def post(path: str, body: dict) -> dict:
        r = http.post(USA_API + path, json=body)
        r.raise_for_status()
        return r.json()

    tp = [{"start_date": "2021-10-01", "end_date": "2026-09-30"}]
    base = {
        "fields": AWARD_FIELDS,
        "page": 1,
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }
    name_page = post(
        "/search/spending_by_award/",
        {
            "filters": {
                "recipient_search_text": ["LOCKHEED MARTIN"],
                "time_period": tp,
                "award_type_codes": ["A", "B", "C", "D"],
            },
            "limit": 60,
            **base,
        },
    )
    (USA_DIR / "spending_by_award_name_LMT.json").write_text(
        json.dumps(name_page, indent=1), encoding="utf-8"
    )
    ueis = [u for u in dict.fromkeys(r["Recipient UEI"] for r in name_page["results"]) if u][:3]
    uei_page = post(
        "/search/spending_by_award/",
        {
            "filters": {
                "recipient_search_text": ueis,
                "time_period": tp,
                "award_type_codes": ["A", "B", "C", "D"],
            },
            "limit": 40,
            **base,
        },
    )
    (USA_DIR / "spending_by_award_uei_LMT.json").write_text(
        json.dumps(uei_page, indent=1), encoding="utf-8"
    )
    for fy in (2025, 2026):
        cat = post(
            "/search/spending_by_category/awarding_subagency/",
            {
                "filters": {
                    "recipient_search_text": ueis,
                    "time_period": [{"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30"}],
                    "award_type_codes": ["A", "B", "C", "D"],
                },
                "limit": 10,
                "page": 1,
            },
        )
        (USA_DIR / f"spending_by_category_subagency_FY{fy}_LMT.json").write_text(
            json.dumps(cat, indent=1), encoding="utf-8"
        )
    gid = next(
        r["generated_internal_id"]
        for r in uei_page["results"]
        if r["Description"] and "F-35" in r["Description"]
    )
    detail = http.get(f"{USA_API}/awards/{gid}/").json()
    keep_keys = (
        "id",
        "generated_unique_award_id",
        "piid",
        "type",
        "type_description",
        "description",
        "total_obligation",
        "base_and_all_options",
        "base_exercised_options",
        "period_of_performance",
        "place_of_performance",
        "awarding_agency",
        "funding_agency",
        "recipient",
        "naics_hierarchy",
        "psc_hierarchy",
        "date_signed",
        "latest_transaction_contract_data",
    )
    keep = {k: detail.get(k) for k in keep_keys}
    if keep.get("latest_transaction_contract_data"):
        ltcd = keep["latest_transaction_contract_data"]
        keep["latest_transaction_contract_data"] = {
            k: v
            for k, v in ltcd.items()
            if k
            in (
                "idv_type_description",
                "type_of_contract_pricing_description",
                "major_program",
                "program_acronym",
                "product_or_service_code",
                "product_or_service_description",
                "naics",
                "naics_description",
                "solicitation_id",
                "parent_award_id",
                "dod_claimant_program_description",
            )
        }
    if keep.get("recipient"):
        keep["recipient"] = {
            k: v
            for k, v in keep["recipient"].items()
            if k
            in (
                "recipient_name",
                "recipient_uei",
                "recipient_hash",
                "parent_recipient_name",
                "parent_recipient_uei",
                "location",
            )
        }
    (USA_DIR / "award_detail_LMT.json").write_text(json.dumps(keep, indent=1), encoding="utf-8")
    for p in sorted(USA_DIR.glob("*.json")):
        print(p.name, p.stat().st_size, "bytes")


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "edgar"):
        record_edgar()
    if which in ("all", "usaspending"):
        record_usaspending()


if __name__ == "__main__":
    main()
