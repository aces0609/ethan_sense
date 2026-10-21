"""USAspending.gov API v2 client (requirements §4 tier 1, government contracting).

No key, no published quota. DoD actions land ~90 days late; the DoD daily contract
announcements feed covers the gap (Phase 2). Every response is registered as a tier-1
source with the award identifier in gov_id so the validator can check it.

Entity resolution is by UEI, never by name (requirements §7 program rule); the
recipient autocomplete is only used to find the UEI set from a name.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx

from primer.clients.cache import DiskCache
from primer.config import output_root
from primer.evidence.store import EvidenceStore
from primer.runlog import RunLog

API = "https://api.usaspending.gov/api/v2"
CONTRACT_TYPES = [
    "A",
    "B",
    "C",
    "D",
]  # BPA call, purchase order, delivery order, definitive contract
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]

AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
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


def fiscal_year_bounds(fy: int) -> tuple[date, date]:
    """US federal FY runs Oct 1 (fy-1) to Sep 30 (fy)."""
    return date(fy - 1, 10, 1), date(fy, 9, 30)


class USAspendingClient:
    def __init__(
        self,
        store: EvidenceStore | None = None,
        runlog: RunLog | None = None,
        client: httpx.Client | None = None,
        offline: bool = False,
    ) -> None:
        self.store = store
        self.runlog = runlog
        self.http = client or httpx.Client(timeout=120.0)
        self.cache = DiskCache(output_root(), "usaspending", ttl_seconds=24 * 3600, offline=offline)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        key = f"POST {path} {json.dumps(body, sort_keys=True)}|{date.today().isoformat()}"

        def fetch() -> dict[str, Any]:
            if self.runlog:
                self.runlog.count("usaspending_requests")
            r = self.http.post(f"{API}{path}", json=body)
            r.raise_for_status()
            return r.json()

        return self.cache.get_or_fetch(key, fetch)

    def _get(self, path: str) -> dict[str, Any]:
        key = f"GET {path}|{date.today().isoformat()}"

        def fetch() -> dict[str, Any]:
            if self.runlog:
                self.runlog.count("usaspending_requests")
            r = self.http.get(f"{API}{path}")
            r.raise_for_status()
            return r.json()

        return self.cache.get_or_fetch(key, fetch)

    def _register(
        self, title: str, url: str, payload: Any, gov_id: str | None, meta: dict | None = None
    ):
        if not self.store:
            return None
        return self.store.register(
            "```json\n" + json.dumps(payload, indent=1)[:200_000] + "\n```",
            type="api:usaspending",
            publisher="USAspending.gov",
            title=title,
            url=url,
            tier=1,
            gov_id=gov_id,
            meta=meta or {},
            dedupe_key=f"usaspending|{title}|{gov_id}|{self.store.as_of.isoformat()}",
        )

    # ---- recipients ------------------------------------------------------------------

    def recipient_autocomplete(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        resp = self._post("/autocomplete/recipient/", {"search_text": name, "limit": limit})
        return resp.get("results", [])

    def recipient_profile(self, recipient_id: str) -> dict[str, Any]:
        return self._get(f"/recipient/{recipient_id}/")

    # ---- awards ----------------------------------------------------------------------

    def spending_by_award(
        self,
        uei_or_names: list[str],
        fy_from: int,
        fy_to: int,
        award_types: list[str] | None = None,
        limit: int = 100,
        page: int = 1,
        sort: str = "Award Amount",
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        start, _ = fiscal_year_bounds(fy_from)
        _, end = fiscal_year_bounds(fy_to)
        filters: dict[str, Any] = {
            "recipient_search_text": uei_or_names,
            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
            "award_type_codes": award_types or CONTRACT_TYPES,
        }
        if keywords:
            filters["keywords"] = keywords
        body = {
            "filters": filters,
            "fields": AWARD_FIELDS,
            "limit": limit,
            "page": page,
            "sort": sort,
            "order": "desc",
            "subawards": False,
        }
        if self.runlog:
            self.runlog.query("usaspending_spending_by_award", json.dumps(filters))
        resp = self._post("/search/spending_by_award/", body)
        rec = self._register(
            f"USAspending prime awards FY{fy_from}-FY{fy_to} for {', '.join(uei_or_names)} (page {page})",
            "https://www.usaspending.gov/search",
            resp,
            gov_id=None,
            meta={"fy_from": fy_from, "fy_to": fy_to, "page": page},
        )
        resp["source_id"] = rec.source_id if rec else None
        return resp

    def award(self, generated_internal_id: str) -> dict[str, Any]:
        resp = self._get(f"/awards/{generated_internal_id}/")
        url = f"https://www.usaspending.gov/award/{generated_internal_id}"
        rec = self._register(
            f"USAspending award {resp.get('piid') or generated_internal_id}: {str(resp.get('description') or '')[:80]}",
            url,
            resp,
            gov_id=generated_internal_id,
            meta={"piid": resp.get("piid")},
        )
        resp["source_id"] = rec.source_id if rec else None
        return resp

    def spending_by_category(
        self, category: str, uei_or_names: list[str], fy_from: int, fy_to: int, limit: int = 20
    ) -> dict[str, Any]:
        """category: awarding_agency | awarding_subagency | naics | psc | ..."""
        start, _ = fiscal_year_bounds(fy_from)
        _, end = fiscal_year_bounds(fy_to)
        body = {
            "filters": {
                "recipient_search_text": uei_or_names,
                "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
                "award_type_codes": CONTRACT_TYPES,
            },
            "limit": limit,
            "page": 1,
        }
        resp = self._post(f"/search/spending_by_category/{category}/", body)
        rec = self._register(
            f"USAspending spending by {category} FY{fy_from}-FY{fy_to} for {', '.join(uei_or_names)}",
            "https://www.usaspending.gov/search",
            resp,
            gov_id=None,
            meta={"category": category},
        )
        resp["source_id"] = rec.source_id if rec else None
        return resp
