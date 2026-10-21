"""Firecrawl client (requirements §4 tier 2) over the v2 HTTP API.

Why httpx instead of the SDK: recorded fixtures with respx stay trivial and the SDK's
surface has changed between majors. Every result is registered in the EvidenceStore
before it is returned, so a researcher can only cite what was fetched.

Credits are estimated from the pricing in requirements §4 and corrected from the
response when Firecrawl reports them. A process-wide semaphore keeps concurrent
requests under the plan limit; 429s back off and retry.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx

from primer.config import domain_policy, tier_for_url
from primer.evidence.store import EvidenceStore
from primer.runlog import RunLog
from primer.state import SourceRecord

API = "https://api.firecrawl.dev/v2"
_SEM = threading.Semaphore(int(os.environ.get("FIRECRAWL_CONCURRENCY", "5")))

CREDITS = {"search_per_10": 2, "scrape": 1, "scrape_json": 5, "map": 1, "parse_per_page": 1}


class FirecrawlError(RuntimeError):
    pass


class FirecrawlClient:
    def __init__(
        self,
        store: EvidenceStore,
        runlog: RunLog | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        credit_cap: int | None = None,
    ) -> None:
        self.store = store
        self.runlog = runlog
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")
        self.http = client or httpx.Client(timeout=120.0)
        self.credit_cap = credit_cap
        self.credits_used = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ---- low level -------------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any], retries: int = 4) -> dict[str, Any]:
        if not self.api_key:
            raise FirecrawlError("FIRECRAWL_API_KEY is not set")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        delay = 2.0
        for attempt in range(retries):
            with _SEM:
                if self.runlog:
                    self.runlog.count("firecrawl_requests")
                r = self.http.post(f"{API}{path}", json=body, headers=headers)
            if r.status_code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise FirecrawlError(f"{path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise FirecrawlError(f"{path}: rate limited after {retries} attempts")

    def _get(self, path: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = self.http.get(f"{API}{path}", headers=headers)
        if r.status_code >= 400:
            raise FirecrawlError(f"{path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _spend(self, estimate: int, response: dict[str, Any] | None = None) -> None:
        reported = None
        if response:
            reported = (
                response.get("creditsUsed") or (response.get("data") or {}).get("creditsUsed")
                if isinstance(response.get("data"), dict)
                else response.get("creditsUsed")
            )
        n = int(reported) if reported else estimate
        with self._lock:
            self.credits_used += n
            if self.credit_cap is not None and self.credits_used > self.credit_cap:
                raise FirecrawlError(f"credit cap {self.credit_cap} exceeded")
        if self.runlog:
            self.runlog.count("firecrawl_credits", n)

    # ---- registration helpers --------------------------------------------------------

    def _register_page(
        self, url: str, markdown: str, metadata: dict[str, Any], type: str, section_hint: str | None
    ) -> SourceRecord | None:
        policy = domain_policy(url)
        if policy == "deny":
            return None
        title = metadata.get("title") or metadata.get("ogTitle") or url
        publisher = metadata.get("ogSiteName") or urlparse(url).netloc
        pub = metadata.get("publishedTime") or metadata.get("article:published_time")
        published = None
        if pub:
            try:
                published = date.fromisoformat(str(pub)[:10])
            except ValueError:
                published = None
        access = "limited" if policy == "limited" else "full"
        content = (
            markdown
            if access == "full"
            else f"# {title}\n\n(access: limited; cited by headline and URL only)\n"
        )
        return self.store.register(
            content,
            type=type,
            publisher=publisher,
            title=title,
            url=url,
            published_date=published,
            tier=tier_for_url(url),
            access=access,
            meta={
                "firecrawl": True,
                "section_hint": section_hint,
                "status": metadata.get("statusCode"),
            },
        )

    # ---- endpoints -------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        scrape: bool = False,
        type: str = "news",
        section_hint: str | None = None,
        tbs: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ranked hits; when scrape=True each hit is registered and carries source_id."""
        body: dict[str, Any] = {"query": query, "limit": limit}
        if tbs:
            body["tbs"] = tbs
        if scrape:
            body["scrapeOptions"] = {"formats": ["markdown"]}
        if self.runlog:
            self.runlog.query("firecrawl_search", query, limit=limit, scrape=scrape)
        resp = self._post("/search", body)
        data = resp.get("data", {})
        hits = data.get("web", data) if isinstance(data, dict) else data
        hits = hits or []
        self._spend(
            CREDITS["search_per_10"] * max(1, -(-limit // 10)) + (len(hits) if scrape else 0), resp
        )
        out = []
        for h in hits:
            url = h.get("url")
            if not url or domain_policy(url) == "deny":
                continue
            row = {"url": url, "title": h.get("title"), "description": h.get("description")}
            if scrape and h.get("markdown"):
                rec = self._register_page(
                    url, h["markdown"], h.get("metadata", {}), type, section_hint
                )
                if rec:
                    row["source_id"] = rec.source_id
            out.append(row)
        return out

    def scrape(
        self,
        url: str,
        type: str = "web",
        json_schema: dict[str, Any] | None = None,
        section_hint: str | None = None,
        only_main: bool = True,
    ) -> dict[str, Any]:
        """Scrape one URL to markdown (and optional schema-typed JSON); registers it."""
        if domain_policy(url) == "deny":
            raise FirecrawlError(f"denied by domain policy: {url}")
        formats: list[Any] = ["markdown"]
        if json_schema:
            formats.append({"type": "json", "schema": json_schema})
        body = {"url": url, "formats": formats, "onlyMainContent": only_main}
        if self.runlog:
            self.runlog.query("firecrawl_scrape", url)
        resp = self._post("/scrape", body)
        self._spend(CREDITS["scrape_json"] if json_schema else CREDITS["scrape"], resp)
        data = resp.get("data", {})
        rec = self._register_page(
            url, data.get("markdown", ""), data.get("metadata", {}), type, section_hint
        )
        return {
            "source_id": rec.source_id if rec else None,
            "markdown": data.get("markdown", ""),
            "json": data.get("json"),
            "metadata": data.get("metadata", {}),
        }

    def map(self, url: str, search: str | None = None, limit: int = 200) -> list[str]:
        body: dict[str, Any] = {"url": url, "limit": limit}
        if search:
            body["search"] = search
        resp = self._post("/map", body)
        self._spend(CREDITS["map"], resp)
        links = resp.get("links") or resp.get("data", {}).get("links") or []
        return [link["url"] if isinstance(link, dict) else link for link in links]

    def agent(
        self,
        prompt: str,
        schema: dict[str, Any],
        urls: list[str] | None = None,
        timeout_s: int = 600,
    ) -> dict[str, Any]:
        """Structured extraction without fixed URLs (replacement for /extract). Polls to completion."""
        body: dict[str, Any] = {"prompt": prompt, "schema": schema}
        if urls:
            body["urls"] = urls
        if self.runlog:
            self.runlog.query("firecrawl_agent", prompt)
        start = self._post("/agent", body)
        job_id = start.get("id")
        if not job_id:
            self._spend(10, start)
            return start.get("data", start)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = self._get(f"/agent/{job_id}")
            if st.get("status") in ("completed", "failed", "cancelled"):
                self._spend(int(st.get("creditsUsed") or 10))
                if st.get("status") != "completed":
                    raise FirecrawlError(f"agent job {job_id} {st.get('status')}")
                return st.get("data", {})
            time.sleep(5)
        raise FirecrawlError(f"agent job {job_id} timed out")
