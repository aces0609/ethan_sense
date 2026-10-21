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
        cap = daily_cap or int(os.environ.get("SAM_GOV_DAILY_CAP", "30"))
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
                raise RuntimeError("SAM.gov 429: daily quota exhausted on the server side")
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
        size: int = 100,
    ) -> dict[str, Any]:
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
            if len(ents) < 100:
                break
        return out

    # ---- opportunities ---------------------------------------------------------------

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
