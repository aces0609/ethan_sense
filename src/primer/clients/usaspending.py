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
    "Recipient UEI",
    "Recipient DUNS Number",
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


def request_summary(
    filters: dict[str, Any], sort: str | None = None, page: int | None = None
) -> str:
    """A short, reproducible description of a search request for the source title:
    'recipients UEI1, UEI2; 2024-10-01 to 2026-09-30; award types A,B,C,D; keywords F-35'."""
    bits: list[str] = []
    names = filters.get("recipient_search_text") or []
    if names:
        bits.append("recipients " + ", ".join(str(n) for n in names[:4]))
    tp = (filters.get("time_period") or [{}])[0]
    if tp.get("start_date") or tp.get("end_date"):
        bits.append(f"{tp.get('start_date', '?')} to {tp.get('end_date', '?')}")
    if filters.get("award_type_codes"):
        bits.append("award types " + ",".join(filters["award_type_codes"]))
    if filters.get("keywords"):
        bits.append("keywords " + ", ".join(filters["keywords"][:3]))
    if sort:
        bits.append(f"sorted by {sort} desc")
    return "; ".join(bits)


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
            return self._request("POST", f"{API}{path}", json=body)

        return self.cache.get_or_fetch(key, fetch)

    def _get(self, path: str) -> dict[str, Any]:
        key = f"GET {path}|{date.today().isoformat()}"

        def fetch() -> dict[str, Any]:
            return self._request("GET", f"{API}{path}")

        return self.cache.get_or_fetch(key, fetch)

    RETRIES = 5  # attempts per request for a transient failure
    BACKOFF_S = (2.0, 6.0, 15.0, 30.0)  # ~53 s: the 2026-10-22 outage outlasted a 12 s window

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """One USAspending call, retried on a 5xx, a 429 or a transport error: a single 503
        from the API once turned the whole of section 4 into "unavailable" (Northrop Grumman,
        2026-10-22). A 4xx other than 429 is the caller's error and is raised at once. Only the
        final outcome reaches the cache and the run recording."""
        import time

        last: Exception | None = None
        for attempt in range(self.RETRIES):
            if self.runlog:
                self.runlog.count("usaspending_requests")
                if attempt:
                    self.runlog.count("usaspending_retries")
            try:
                r = self.http.request(method, url, **kwargs)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 and e.response.status_code != 429:
                    raise
                last = e
            except (httpx.TransportError, ValueError) as e:  # ValueError: a body that is not JSON
                last = e
            if attempt < self.RETRIES - 1:
                time.sleep(self.BACKOFF_S[min(attempt, len(self.BACKOFF_S) - 1)])
        assert last is not None
        raise last

    def _register(
        self, title: str, url: str, payload: Any, gov_id: str | None, meta: dict | None = None
    ):
        if not self.store:
            return None
        return self.store.register(
            "```json\n" + json.dumps(payload, indent=1)[:200_000] + "\n```",
            type="api:usaspending",
            published_date=self.store.as_of,
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
        if len(uei_or_names) > self.MAX_TERMS:
            resp = self._batched_awards(body, uei_or_names, limit, page, sort)
        else:
            resp = self._post("/search/spending_by_award/", body)
        results = resp.get("results") or []
        # a reproducible citation: the award page when the pull returned one award, else
        # the API request (endpoint URL, the filter summarised in the title and the full
        # body in meta) rather than the interactive search page
        single = results[0].get("generated_internal_id") if len(results) == 1 else None
        if single:
            url = f"https://www.usaspending.gov/award/{single}"
        else:
            url = f"{API}/search/spending_by_award/"
        rec = self._register(
            f"USAspending prime awards FY{fy_from}-FY{fy_to} for {', '.join(uei_or_names)} "
            f"(page {page}; {request_summary(filters, sort, page)})",
            url,
            resp,
            gov_id=single,
            meta={
                "fy_from": fy_from,
                "fy_to": fy_to,
                "page": page,
                "request": {"method": "POST", "path": "/search/spending_by_award/", "body": body},
            },
        )
        resp["source_id"] = rec.source_id if rec else None
        return resp

    MAX_TERMS = 8  # recipient identifiers per request

    def _batched_awards(
        self, body: dict[str, Any], terms: list[str], limit: int, page: int, sort: str
    ) -> dict[str, Any]:
        """The award search for a recipient set too large for one request. USAspending answers a
        text search over many OR terms with a 503 timeout: Northrop Grumman resolves to 31 UEIs
        and failed every attempt on 2026-10-22 where Lockheed Martin's 17 passed. The identifiers
        go out in batches of MAX_TERMS, the rows are merged, de-duplicated by award and ranked by
        the sort column, and the requested page is cut from the merged ranking. A batch that
        fails after its retries is skipped and logged; the pull fails only if every batch does."""
        merged: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        last: Exception | None = None
        batches = [terms[i : i + self.MAX_TERMS] for i in range(0, len(terms), self.MAX_TERMS)]
        for chunk in batches:
            try:
                for pg in range(1, page + 1):
                    sub = {
                        **body,
                        "filters": {**body["filters"], "recipient_search_text": chunk},
                        "page": pg,
                    }
                    r = self._post("/search/spending_by_award/", sub)
                    rows = r.get("results") or []
                    for row in rows:
                        merged.setdefault(
                            row.get("generated_internal_id") or row.get("Award ID"), row
                        )
                    if len(rows) < limit or not (r.get("page_metadata") or {}).get("hasNext"):
                        break
            except Exception as e:  # noqa: BLE001 - one batch failing must not lose the others
                last = e
                failures.append(f"{','.join(chunk)}: {e!r}"[:300])
                if self.runlog:
                    self.runlog.error(f"usaspending batch of {len(chunk)} UEIs failed: {e!r}")
        if failures and len(failures) == len(batches):
            assert last is not None
            raise last
        ranked = sorted(
            merged.values(),
            key=lambda row: (row.get(sort) is not None, row.get(sort) or 0),
            reverse=True,
        )
        if self.runlog:
            self.runlog.count("usaspending_batches", len(batches))
        out: dict[str, Any] = {
            "results": ranked[(page - 1) * limit : page * limit],
            "page_metadata": {
                "page": page,
                "hasNext": len(ranked) > page * limit,
                "batches": len(batches),
            },
        }
        if failures:
            out["batch_errors"] = failures
        return out

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

    def _batched_category(
        self, category: str, body: dict[str, Any], terms: list[str], limit: int
    ) -> dict[str, Any]:
        """spending_by_category for a recipient set too large for one request (see
        `_batched_awards`): per-batch category totals are summed by category name, ranked, and
        cut to `limit`. A batch that fails is skipped and logged; all failing raises."""
        totals: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        last: Exception | None = None
        batches = [terms[i : i + self.MAX_TERMS] for i in range(0, len(terms), self.MAX_TERMS)]
        for chunk in batches:
            sub = {**body, "filters": {**body["filters"], "recipient_search_text": chunk}}
            # ask each batch for more rows than the final cut so a category that is small in
            # every batch but large in total is not lost
            sub["limit"] = max(limit * 3, 30)
            try:
                r = self._post(f"/search/spending_by_category/{category}/", sub)
            except Exception as e:  # noqa: BLE001 - one batch failing must not lose the others
                last = e
                failures.append(f"{','.join(chunk)}: {e!r}"[:300])
                if self.runlog:
                    self.runlog.error(
                        f"usaspending category batch of {len(chunk)} UEIs failed: {e!r}"
                    )
                continue
            for row in r.get("results") or []:
                key = str(row.get("name") or row.get("code") or row.get("id"))
                if key in totals:
                    totals[key]["amount"] = (totals[key].get("amount") or 0) + (
                        row.get("amount") or 0
                    )
                else:
                    totals[key] = dict(row)
        if failures and len(failures) == len(batches):
            assert last is not None
            raise last
        ranked = sorted(totals.values(), key=lambda row: row.get("amount") or 0, reverse=True)
        out: dict[str, Any] = {
            "category": category,
            "results": ranked[:limit],
            "page_metadata": {"page": 1, "hasNext": len(ranked) > limit, "batches": len(batches)},
        }
        if failures:
            out["batch_errors"] = failures
        return out

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
        if len(uei_or_names) > self.MAX_TERMS:
            resp = self._batched_category(category, body, uei_or_names, limit)
        else:
            resp = self._post(f"/search/spending_by_category/{category}/", body)
        rec = self._register(
            f"USAspending spending by {category} FY{fy_from}-FY{fy_to} for {', '.join(uei_or_names)} "
            f"({request_summary(body['filters'])})",
            f"{API}/search/spending_by_category/{category}/",
            resp,
            gov_id=None,
            meta={
                "category": category,
                "request": {
                    "method": "POST",
                    "path": f"/search/spending_by_category/{category}/",
                    "body": body,
                },
            },
        )
        resp["source_id"] = rec.source_id if rec else None
        return resp
