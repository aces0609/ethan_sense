"""SAM.gov Entity Management v3 and Get Opportunities v2 clients (requirements §4, §8).

The personal API key allows tens of calls per day, so:
- a persistent DailyQuota (output/.samgov_quota.json) is shared across runs and refuses
  calls past SAM_GOV_DAILY_CAP (default 30);
- every response is cached for 7 days under output/.cache/samgov;
- the opportunities window is enforced at <= 1 year (the API rejects more).

Entity walk: v3 has no "children of parent UEI" filter that this client relies on, so
`walk_parent` searches by legal business name and keeps registrations whose immediate or
ultimate parent UEI matches. Large primes have hundreds of registrations; page and cache.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

import httpx

from primer.clients.cache import DiskCache
from primer.clients.ratelimit import DailyQuota
from primer.config import output_root
from primer.evidence.store import EvidenceStore
from primer.runlog import RunLog

ENTITY_URL = "https://api.sam.gov/entity-information/v3/entities"
OPPS_URL = "https://api.sam.gov/opportunities/v2/search"
AWARDS_URL = "https://api.sam.gov/contract-awards/v1/search"
# the sections an awards pull needs; the awardee block is the bulk of a record and is left out
AWARD_SECTIONS = "contractId,coreData,awardDetails"
PAGE = 100  # the most records a page can hold
# The API's daily limit is 10 requests for a personal key with no SAM.gov role and 1,000 for a key
# whose owner has a role or a system account (docs: open.gsa.gov/api/contract-awards). A key's
# limit is set on the SAM.gov side; SAM_GOV_DAILY_CAP tells this client what to expect.
DEFAULT_DAILY_CAP = 10
# Calls kept back for the awards pull (a probe, an extract request and its polls) while the
# daily cap is small; an entity lookup in section 4 may not spend them.
AWARDS_RESERVE = 5


class SamQuotaError(RuntimeError):
    """SAM.gov refused the call: the key's daily quota is spent (HTTP 429). `next_access` is the
    time the server says the key works again, when it says."""

    def __init__(self, message: str, next_access: str | None = None) -> None:
        super().__init__(message)
        self.next_access = next_access


class SamGovClient:
    def __init__(
        self,
        store: EvidenceStore | None = None,
        runlog: RunLog | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        daily_cap: int | None = None,
        offline: bool = False,
    ) -> None:
        self.store = store
        self.runlog = runlog
        self.api_key = api_key or os.environ.get("SAM_GOV_API_KEY", "")
        self.http = client or httpx.Client(timeout=120.0)
        root = output_root()
        self.cache = DiskCache(root, "samgov", ttl_seconds=7 * 24 * 3600, offline=offline)
        cap = daily_cap or int(os.environ.get("SAM_GOV_DAILY_CAP", str(DEFAULT_DAILY_CAP)))
        self.quota = DailyQuota(root / ".samgov_quota.json", cap)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        key = f"{url}?{json.dumps(params, sort_keys=True)}"

        def fetch() -> dict[str, Any]:
            if not self.api_key:
                raise RuntimeError("SAM_GOV_API_KEY is not set")
            self.quota.take(1)
            if self.runlog:
                self.runlog.count("samgov_calls")
            r = self.http.get(url, params={**params, "api_key": self.api_key})
            if r.status_code == 429:
                try:
                    nxt = r.json().get("nextAccessTime")
                except ValueError:
                    nxt = None
                raise SamQuotaError(
                    "SAM.gov 429: daily quota exhausted on the server side"
                    + (f" (works again {nxt})" if nxt else ""),
                    nxt,
                )
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
            type="api:samgov",
            published_date=self.store.as_of,
            publisher="SAM.gov",
            title=title,
            url=url,
            tier=1,
            gov_id=gov_id,
            meta=meta or {},
            dedupe_key=f"samgov|{url}|{gov_id}|{self.store.as_of.isoformat()}",
        )

    # ---- entities --------------------------------------------------------------------

    def entities(
        self,
        uei: str | None = None,
        cage: str | None = None,
        legal_name: str | None = None,
        registration_status: str | None = "A",
        page: int = 0,
        size: int = 10,
    ) -> dict[str, Any]:
        """One page of registrations. Personal API keys are limited to 10 records per page
        (the API answers 400 "Size Cannot Exceed 10 Records" above that)."""
        params: dict[str, Any] = {"page": page, "size": size}
        if uei:
            params["ueiSAM"] = uei
        if cage:
            params["cageCode"] = cage
        if legal_name:
            params["legalBusinessName"] = legal_name
        if registration_status:
            params["registrationStatus"] = registration_status
        if self.runlog:
            self.runlog.query("samgov_entities", json.dumps(params))
        resp = self._get(ENTITY_URL, params)
        for ent in resp.get("entityData", []):
            reg = ent.get("entityRegistration", {})
            core = ent.get("coreData", {})
            addr = core.get("physicalAddress", {})
            rec = self._register(
                f"SAM.gov entity {reg.get('legalBusinessName')} UEI {reg.get('ueiSAM')} CAGE {reg.get('cageCode')} ({addr.get('city')}, {addr.get('stateOrProvinceCode')})",
                f"https://sam.gov/entity/{reg.get('ueiSAM')}/coreData",
                ent,
                gov_id=f"UEI:{reg.get('ueiSAM')};CAGE:{reg.get('cageCode')}",
                meta={
                    "uei": reg.get("ueiSAM"),
                    "cage": reg.get("cageCode"),
                    "registration_status": reg.get("registrationStatus"),
                    "expiration": reg.get("registrationExpirationDate"),
                },
            )
            ent["source_id"] = rec.source_id if rec else None
        return resp

    def walk_parent(
        self, parent_uei: str, legal_name: str, max_pages: int = 5
    ) -> list[dict[str, Any]]:
        """Registrations whose immediate or ultimate parent is parent_uei (active only)."""
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            resp = self.entities(legal_name=legal_name, page=page)
            ents = resp.get("entityData", [])
            for ent in ents:
                hier = ent.get("coreData", {}).get("entityHierarchyInformation", {})
                parents = {
                    (hier.get("immediateParentEntity") or {}).get("ueiSAM"),
                    (hier.get("ultimateParentEntity") or {}).get("ueiSAM"),
                    ent.get("entityRegistration", {}).get("ueiSAM"),
                }
                if parent_uei in parents:
                    out.append(ent)
            if len(ents) < 10:
                break
        return out

    def entities_by_uei(self, ueis: list[str], max_calls: int = 10) -> list[dict[str, Any]]:
        """One registration per UEI, one call each, within max_calls (the per-primer SAM.gov
        budget). Resolution by UEI, never by name (requirements §7)."""
        out: list[dict[str, Any]] = []
        for uei in ueis[:max_calls]:
            if self.quota.remaining() <= 0:
                break
            resp = self.entities(uei=uei, registration_status=None, size=1)
            out.extend(resp.get("entityData", []))
        return out

    # ---- opportunities ---------------------------------------------------------------

    # ---- contract awards -----------------------------------------------------------

    @staticmethod
    def _awards_params(ueis: list[str], start: date, end: date) -> dict[str, Any]:
        """Up to 100 UEIs go in one parameter, joined by ~ (the API's OR)."""
        return {
            "awardeeUniqueEntityId": "~".join(ueis[:100]),
            "dateSigned": f"[{start.strftime('%m/%d/%Y')},{end.strftime('%m/%d/%Y')}]",
        }

    def contract_awards(
        self,
        ueis: list[str],
        start: date,
        end: date,
        page: int = 0,
        limit: int = PAGE,
        sections: str | None = AWARD_SECTIONS,
    ) -> dict[str, Any]:
        """One page of contract actions signed in [start, end] for the awardee UEIs. `offset` is
        a page index. Returns {awardSummary, totalRecords, limit, offset}."""
        params = self._awards_params(ueis, start, end)
        params.update({"limit": limit, "offset": page})
        if sections:
            params["includeSections"] = sections
        if self.runlog:
            self.runlog.query("samgov_awards", json.dumps(params))
        return self._get(AWARDS_URL, params)

    def entity_call_budget(self, per_primer: int) -> int:
        """How many calls an entity lookup may spend: the per-primer budget, less the calls
        kept for the awards pull when the daily cap is small. A key with a high cap
        (a SAM.gov role: 1,000 a day) needs no reserve."""
        if self.quota.cap > 4 * AWARDS_RESERVE:
            return per_primer
        return max(0, min(per_primer, self.quota.remaining() - AWARDS_RESERVE))

    def awards_remaining_calls(self) -> int:
        return self.quota.remaining()

    def pull_contract_awards(
        self,
        ueis: list[str],
        start: date,
        end: date,
        max_calls: int,
        extract: bool = True,
        polls: int = 3,
        wait: float = 20.0,
    ) -> dict[str, Any]:
        """Every contract action signed in [start, end] for the UEIs, or an exception.

        One probe page reads `totalRecords`. When the pages fit in `max_calls` (and in what is
        left of the daily quota) they are fetched one by one; otherwise the asynchronous JSON
        extract is requested and polled (it carries up to a million records for two or three
        requests). Anything short of every record raises: a partial list would give wrong totals,
        so the caller falls back to another source instead. Returns {records, total, method,
        calls}."""
        calls = 1
        probe = self.contract_awards(ueis, start, end, page=0, limit=PAGE)
        total = int(probe.get("totalRecords") or 0)
        records = list(probe.get("awardSummary") or [])
        pages = -(-total // PAGE)
        budget = min(max_calls, self.awards_remaining_calls() + calls)
        if total <= len(records):
            return {"records": records, "total": total, "method": "pages", "calls": calls}
        if pages <= budget:
            for page in range(1, pages):
                resp = self.contract_awards(ueis, start, end, page=page, limit=PAGE)
                calls += 1
                records.extend(resp.get("awardSummary") or [])
            if len(records) < total:
                raise RuntimeError(f"SAM.gov returned {len(records)} of {total} actions")
            return {"records": records, "total": total, "method": "pages", "calls": calls}
        if not extract:
            raise RuntimeError(
                f"SAM.gov holds {total} actions ({pages} pages); {budget} calls are available"
            )
        records = self._extract(ueis, start, end, polls, wait)
        if len(records) < total:
            raise RuntimeError(f"SAM.gov extract held {len(records)} of {total} actions")
        return {"records": records, "total": total, "method": "extract", "calls": calls + 1 + polls}

    def _extract(
        self, ueis: list[str], start: date, end: date, polls: int, wait: float
    ) -> list[dict[str, Any]]:
        """The asynchronous JSON extract: a request with format=json answers with a download URL
        and token; the file (a zip holding one JSON file) is fetched once it is ready. Cached
        whole, so a rerun costs nothing. `emailId=No` is required next to `format` (the API
        rejects one without the other) and keeps the link from being emailed."""
        import io
        import time
        import zipfile

        params = self._awards_params(ueis, start, end)
        params.update({"format": "json", "emailId": "No", "includeSections": AWARD_SECTIONS})
        key = f"extract:{AWARDS_URL}?{json.dumps(params, sort_keys=True)}"

        def fetch() -> dict[str, Any]:
            first = self._get_uncached(AWARDS_URL, params)
            url = str(first.get("presignedUrl") or "")
            if not url:
                raise RuntimeError(f"SAM.gov extract gave no download link: {first}")
            url = url.replace("REPLACE_WITH_API_KEY", self.api_key)
            for i in range(polls):
                time.sleep(wait * (i + 1))  # the file takes a few seconds to generate
                self.quota.take(1)
                if self.runlog:
                    self.runlog.count("samgov_calls")
                r = self.http.get(url)
                if r.status_code == 429:
                    raise SamQuotaError("SAM.gov 429 while downloading the extract")
                if r.status_code == 200:
                    raw = r.content
                    try:
                        if raw[:2] == b"PK":
                            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                                raw = z.read(z.namelist()[0])
                        body = json.loads(raw)
                    except (ValueError, zipfile.BadZipFile, IndexError, KeyError):
                        body = {}
                    if isinstance(body, dict) and "awardSummary" in body:
                        return {"awardSummary": body["awardSummary"]}
                    if isinstance(body, list):
                        return {"awardSummary": body}
            raise RuntimeError("SAM.gov extract file was not ready after the polls")

        return list(self.cache.get_or_fetch(key, fetch).get("awardSummary") or [])

    def _get_uncached(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.quota.take(1)
        if self.runlog:
            self.runlog.count("samgov_calls")
        r = self.http.get(url, params={**params, "api_key": self.api_key})
        if r.status_code == 429:
            raise SamQuotaError("SAM.gov 429: daily quota exhausted on the server side")
        r.raise_for_status()
        return r.json()

    def opportunities(
        self,
        q: str | None = None,
        posted_from: date | None = None,
        posted_to: date | None = None,
        ptype: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        posted_to = posted_to or date.today()
        posted_from = posted_from or (posted_to - timedelta(days=364))
        if (posted_to - posted_from).days > 365:
            raise ValueError("SAM.gov opportunities window cannot exceed one year")
        params: dict[str, Any] = {
            "postedFrom": posted_from.strftime("%m/%d/%Y"),
            "postedTo": posted_to.strftime("%m/%d/%Y"),
            "limit": limit,
            "offset": offset,
        }
        if q:
            params["q"] = q
        if ptype:
            params["ptype"] = ptype  # a=award notice, r=sources sought, o=solicitation, ...
        if self.runlog:
            self.runlog.query("samgov_opportunities", json.dumps(params))
        resp = self._get(OPPS_URL, params)
        for opp in resp.get("opportunitiesData", []):
            rec = self._register(
                f"SAM.gov notice {opp.get('noticeId')}: {opp.get('title')}",
                opp.get("uiLink") or f"https://sam.gov/opp/{opp.get('noticeId')}/view",
                opp,
                gov_id=opp.get("noticeId"),
                meta={"type": opp.get("type"), "posted": opp.get("postedDate")},
            )
            opp["source_id"] = rec.source_id if rec else None
        return resp
