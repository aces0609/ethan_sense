"""Firecrawl client (requirements §4 tier 2) over the v2 HTTP API.

Why httpx instead of the SDK: recorded fixtures with respx stay trivial and the SDK
surface has changed between majors. Every result is registered in the EvidenceStore
before it is returned, so a researcher can only cite what was fetched.

Credits are estimated from the pricing in requirements §4 and corrected from the
response when Firecrawl reports them. A process-wide semaphore keeps concurrent
requests under the plan limit (the free plan allows 2); 429s back off and retry.

Responses are cached on disk for 7 days under output/.cache/firecrawl, keyed by endpoint
and body, so a rerun of the same company (tuning, resume, the LLM pass after a keyless
pass) spends no credits. Cache hits are counted separately in run.json.

Two caps: a per-client cap (the section budget) and a per-run cap read from
budgets.yaml per_primer.credits against the RunLog counter; either raises
BudgetExhausted so the section stops and is marked partial.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx

from primer.clients.cache import CacheMiss, DiskCache
from primer.clients.ratelimit import TokenBucket
from primer.clients.robots import record_policy, robots_allowed
from primer.config import budgets_config, domain_policy, output_root, tier_for_url
from primer.evidence.store import (
    EvidenceStore,
    clean_title,
    date_from_metadata,
    date_from_url,
    is_index_page,
)
from primer.runlog import RunLog
from primer.state import SourceRecord

API = "https://api.firecrawl.dev/v2"
_SEM = threading.Semaphore(int(os.environ.get("FIRECRAWL_CONCURRENCY", "2")))
# the free plan allows 40 requests per minute across endpoints; stay under it process-wide
_RPM = int(os.environ.get("FIRECRAWL_RPM", "30"))
_BUCKET = TokenBucket(rate_per_sec=_RPM / 60.0, capacity=3)

CREDITS = {"search_per_10": 2, "scrape": 1, "scrape_json": 5, "map": 1, "parse_per_page": 1}

# page metadata that survives for an access=limited source: the headline, the publisher and
# the dates, never the page description or any body text
_LIMITED_META_KEYS = (
    "title",
    "ogTitle",
    "ogSiteName",
    "statusCode",
    "sourceURL",
    "publishedTime",
    "article:published_time",
    "datePublished",
    "modifiedTime",
)


def _scrub_limited(resp: dict[str, Any]) -> dict[str, Any]:
    """A /scrape response reduced to what an access=limited source may keep on disk."""
    data = resp.get("data")
    if not isinstance(data, dict):
        return resp
    meta = data.get("metadata") or {}
    kept = {k: meta[k] for k in _LIMITED_META_KEYS if k in meta}
    title = str(kept.get("title") or kept.get("ogTitle") or "").strip()
    return {
        **{k: v for k, v in resp.items() if k != "data"},
        "data": {"markdown": f"# {title}\n" if title else "", "metadata": kept},
    }


def _scrub_search(resp: dict[str, Any]) -> dict[str, Any]:
    """A /search response with the excerpts and page bodies of access=limited hits removed."""
    data = resp.get("data")
    hits = data.get("web") if isinstance(data, dict) else data
    if not isinstance(hits, list) or not any(
        isinstance(h, dict) and h.get("url") and domain_policy(h["url"]) == "limited" for h in hits
    ):
        return resp

    def clean(h: Any) -> Any:
        if not (isinstance(h, dict) and h.get("url") and domain_policy(h["url"]) == "limited"):
            return h
        meta = h.get("metadata") or {}
        out = {k: v for k, v in h.items() if k not in ("markdown", "description", "metadata")}
        out["metadata"] = {k: meta[k] for k in _LIMITED_META_KEYS if k in meta}
        if h.get("markdown"):
            # keep the hit registrable (headline only) so a cached search behaves like a fresh one
            out["markdown"] = f"# {h.get('title') or meta.get('title') or ''}\n"
        return out

    cleaned = [clean(h) for h in hits]
    if isinstance(data, dict):
        return {**resp, "data": {**data, "web": cleaned}}
    return {**resp, "data": cleaned}


class FirecrawlError(RuntimeError):
    pass


class BudgetExhausted(FirecrawlError):
    pass


class FirecrawlClient:
    def __init__(
        self,
        store: EvidenceStore,
        runlog: RunLog | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        credit_cap: int | None = None,
        offline: bool = False,
        cache_ttl_seconds: float = 7 * 24 * 3600,
        use_cache: bool = True,
    ) -> None:
        self.store = store
        self.runlog = runlog
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")
        self.http = client or httpx.Client(timeout=120.0)
        self.credit_cap = credit_cap
        self.credits_used = 0
        self.offline = offline
        self.cache = (
            DiskCache(output_root(), "firecrawl", ttl_seconds=cache_ttl_seconds, offline=offline)
            if use_cache
            else None
        )
        self._lock = threading.Lock()
        self.run_cap = int(budgets_config().get("per_primer", {}).get("credits", 0) or 0)

    @property
    def available(self) -> bool:
        return bool(self.api_key) or self.offline

    # ---- low level -------------------------------------------------------------------

    def _check_caps(self, estimate: int) -> None:
        if self.credit_cap is not None and self.credits_used + estimate > self.credit_cap:
            raise BudgetExhausted(
                f"section credit cap {self.credit_cap} reached ({self.credits_used} used)"
            )
        if self.run_cap and self.runlog:
            used = self.runlog.counters.get("firecrawl_credits", 0)
            if used + estimate > self.run_cap:
                raise BudgetExhausted(f"primer credit cap {self.run_cap} reached ({used} used)")

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        retries: int = 5,
        cacheable: bool = True,
        scrub: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return (response json, cached). `scrub` rewrites the response before it is written
        to the disk cache (access=limited pages keep their headline there, never the body)."""
        key = f"POST {path} {json.dumps(body, sort_keys=True)}"
        if self.cache and cacheable:
            try:
                return self.cache.get(key), True
            except CacheMiss:
                if self.offline:
                    raise FirecrawlError(f"offline and not cached: {path} {body}") from None
        if not self.api_key:
            raise FirecrawlError("FIRECRAWL_API_KEY is not set")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        delay = 2.0
        for attempt in range(retries):
            _BUCKET.acquire()
            with _SEM:
                if self.runlog:
                    self.runlog.count("firecrawl_requests")
                try:
                    r = self.http.post(f"{API}{path}", json=body, headers=headers)
                except httpx.HTTPError as e:
                    if attempt < retries - 1:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise FirecrawlError(f"{path}: {e!r}") from e
            if r.status_code == 429 and attempt < retries - 1:
                wait = delay
                try:
                    wait = float(r.json().get("retry_after_seconds") or delay) + 1.0
                except (ValueError, AttributeError):
                    pass
                if self.runlog:
                    self.runlog.count("firecrawl_429")
                time.sleep(min(max(wait, delay), 90.0))
                delay *= 2
                continue
            if r.status_code in (502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise FirecrawlError(f"{path} -> {r.status_code}: {r.text[:300]}")
            data = r.json()
            if self.cache and cacheable and data.get("success", True):
                self.cache.put(key, scrub(data) if scrub else data)
            return data, False
        raise FirecrawlError(f"{path}: rate limited after {retries} attempts")

    def _get(self, path: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = self.http.get(f"{API}{path}", headers=headers)
        if r.status_code >= 400:
            raise FirecrawlError(f"{path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _spend(
        self, estimate: int, response: dict[str, Any] | None = None, cached: bool = False
    ) -> None:
        if cached:
            if self.runlog:
                self.runlog.count("firecrawl_cache_hits")
            return
        reported = None
        if response:
            data = response.get("data")
            reported = response.get("creditsUsed") or (
                data.get("creditsUsed") if isinstance(data, dict) else None
            )
        n = int(reported) if reported else estimate
        with self._lock:
            self.credits_used += n
        if self.runlog:
            self.runlog.count("firecrawl_credits", n)

    # ---- domain policy (requirements §8 Paywalls and terms) -------------------------------

    def _robots_block(self, url: str) -> str | None:
        """None when `url` may be fetched; else the reason it may not. A block is counted as
        `firecrawl_robots_blocked` and listed in run.json meta.blocked; it is not an error."""
        ok, reason = robots_allowed(url, runlog=self.runlog, offline=self.offline)
        if ok:
            return None
        if self.runlog:
            self.runlog.count("firecrawl_robots_blocked")
        record_policy(self.runlog, url, "robots_blocked", reason)
        return reason

    def _filter_urls(self, urls: list[str]) -> list[str]:
        """Drop denied and robots-disallowed URLs before they are handed to Firecrawl."""
        keep = []
        for u in urls:
            if domain_policy(u) == "deny":
                record_policy(self.runlog, u, "denied")
            elif self._robots_block(u) is None:
                keep.append(u)
        return keep

    # ---- registration helpers --------------------------------------------------------

    def _register_page(
        self,
        url: str,
        markdown: str,
        metadata: dict[str, Any],
        type: str,
        section_hint: str | None,
        published_date: date | None = None,
    ) -> SourceRecord | None:
        policy = domain_policy(url)
        if policy == "deny":
            return None
        if is_index_page(url, markdown):
            # a newsroom / presentations / investors listing is navigation, not evidence:
            # the pages it links to are the sources (review 2026-10-21 §7)
            if self.runlog:
                self.runlog.count("firecrawl_index_pages_refused")
            return None
        title = clean_title(metadata.get("title") or metadata.get("ogTitle") or url)
        publisher = metadata.get("ogSiteName") or urlparse(url).netloc
        published = published_date or date_from_metadata(metadata) or date_from_url(url)
        access = "limited" if policy == "limited" else "full"
        content = (
            markdown
            if access == "full"
            else f"# {title}\n\n(access: limited; cited by headline and URL only)\n"
        )
        return self.store.register(
            content,
            type=type,
            publisher=str(publisher)[:120],
            title=str(title)[:200],
            url=url,
            published_date=published,
            tier=tier_for_url(url),
            access=access,
            meta={
                "firecrawl": True,
                "section_hint": section_hint,
                "status": metadata.get("statusCode"),
                # page dates the fetcher saw, so the store can fill a missing published_date
                **{
                    k: str(metadata[k])[:40]
                    for k in (
                        "publishedTime",
                        "article:published_time",
                        "datePublished",
                        "modifiedTime",
                    )
                    if metadata.get(k)
                },
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
        est = CREDITS["search_per_10"] * max(1, -(-limit // 10)) + (limit if scrape else 0)
        self._check_caps(est)
        if self.runlog:
            self.runlog.query("firecrawl_search", query, limit=limit, scrape=scrape)
        resp, cached = self._post("/search", body, scrub=_scrub_search)
        data = resp.get("data", {})
        hits = data.get("web", data) if isinstance(data, dict) else data
        hits = hits or []
        self._spend(
            CREDITS["search_per_10"] * max(1, -(-limit // 10)) + (len(hits) if scrape else 0),
            resp,
            cached,
        )
        out = []
        for h in hits:
            url = h.get("url")
            if not url:
                continue
            policy = domain_policy(url)
            if policy == "deny":
                record_policy(self.runlog, url, "denied")
                continue
            if scrape and h.get("markdown") and self._robots_block(url):
                # the page body came back with the search; a disallowed page is neither
                # registered nor surfaced (the block is counted and listed in run.json)
                continue
            record_policy(self.runlog, url, "limited" if policy == "limited" else "allowed")
            row = {
                "url": url,
                "title": h.get("title"),
                # a search excerpt of a paywalled page is body text: headline and URL only
                "description": "" if policy == "limited" else (h.get("description") or "")[:500],
                "domain": urlparse(url).netloc.lower(),
            }
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
        json_prompt: str | None = None,
        section_hint: str | None = None,
        only_main: bool = True,
        pdf: bool = False,
        published_date: date | None = None,
    ) -> dict[str, Any]:
        """Scrape one URL to markdown (and optional schema-typed JSON); registers it."""
        policy = domain_policy(url)
        if policy == "deny":
            record_policy(self.runlog, url, "denied")
            raise FirecrawlError(f"denied by domain policy: {url}")
        blocked = self._robots_block(url)
        if blocked:
            # not fetched and not an error: the caller sees no source and carries on
            return {
                "source_id": None,
                "markdown": "",
                "json": None,
                "metadata": {},
                "cached": False,
                "blocked": blocked,
            }
        formats: list[Any] = ["markdown"]
        if json_schema:
            fmt: dict[str, Any] = {"type": "json", "schema": json_schema}
            if json_prompt:
                fmt["prompt"] = json_prompt
            formats.append(fmt)
        body: dict[str, Any] = {"url": url, "formats": formats, "onlyMainContent": only_main}
        if pdf:
            body["parsers"] = ["pdf"]
        est = CREDITS["scrape_json"] if json_schema else CREDITS["scrape"]
        self._check_caps(est)
        if self.runlog:
            self.runlog.query("firecrawl_scrape", url, json=bool(json_schema), pdf=pdf)
        resp, cached = self._post(
            "/scrape", body, scrub=_scrub_limited if policy == "limited" else None
        )
        self._spend(est, resp, cached)
        data = resp.get("data", {})
        rec = self._register_page(
            url,
            data.get("markdown", ""),
            data.get("metadata", {}),
            type,
            section_hint,
            published_date=published_date,
        )
        record_policy(self.runlog, url, "limited" if policy == "limited" else "allowed")
        if policy == "limited":
            # fetched for the headline only: the body never leaves this method, so no caller
            # can pass paywalled text to a drafter (the store holds the stub, see _register_page)
            meta = data.get("metadata", {}) or {}
            return {
                "source_id": rec.source_id if rec else None,
                "markdown": "",
                "json": None,
                "metadata": {k: meta[k] for k in _LIMITED_META_KEYS if k in meta},
                "cached": cached,
                "access": "limited",
            }
        return {
            "source_id": rec.source_id if rec else None,
            "markdown": data.get("markdown", ""),
            "json": data.get("json"),
            "metadata": data.get("metadata", {}),
            "cached": cached,
        }

    def parse(
        self, url: str, type: str = "web", section_hint: str | None = None, **kw: Any
    ) -> dict[str, Any]:
        """PDF/deck parsing. The v2 API parses PDFs through /scrape with the pdf parser
        (one credit per page); kept as a separate method so call sites read like the plan."""
        return self.scrape(url, type=type, section_hint=section_hint, pdf=True, **kw)

    def map(self, url: str, search: str | None = None, limit: int = 200) -> list[str]:
        body: dict[str, Any] = {"url": url, "limit": limit}
        if search:
            body["search"] = search
        self._check_caps(CREDITS["map"])
        if self.runlog:
            self.runlog.query("firecrawl_map", url, search=search)
        resp, cached = self._post("/map", body)
        self._spend(CREDITS["map"], resp, cached)
        links = resp.get("links") or (
            resp.get("data", {}).get("links") if isinstance(resp.get("data"), dict) else None
        )
        links = links or []
        return [link["url"] if isinstance(link, dict) else link for link in links]

    def agent(
        self,
        prompt: str,
        schema: dict[str, Any],
        urls: list[str] | None = None,
        timeout_s: int = 300,
        estimate_credits: int = 20,
    ) -> dict[str, Any]:
        """Structured extraction without fixed URLs (replacement for /extract). Polls to completion."""
        body: dict[str, Any] = {"prompt": prompt, "schema": schema}
        if urls:
            urls = self._filter_urls(urls)
            if not urls:
                return {}  # every URL was denied or robots-disallowed: nothing to extract from
            body["urls"] = urls
        self._check_caps(estimate_credits)
        if self.runlog:
            self.runlog.query("firecrawl_agent", prompt, urls=urls)
        key = f"POST /agent {json.dumps(body, sort_keys=True)}"
        if self.cache:
            try:
                data = self.cache.get(key)
                self._spend(0, cached=True)
                return data
            except CacheMiss:
                if self.offline:
                    raise FirecrawlError("offline and agent result not cached") from None
        start, _ = self._post("/agent", body, cacheable=False)
        job_id = start.get("id")
        if not job_id:
            self._spend(estimate_credits, start)
            data = start.get("data", start)
            if self.cache:
                self.cache.put(key, data)
            return data
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = self._get(f"/agent/{job_id}")
            if st.get("status") in ("completed", "failed", "cancelled"):
                self._spend(int(st.get("creditsUsed") or estimate_credits))
                if st.get("status") != "completed":
                    raise FirecrawlError(f"agent job {job_id} {st.get('status')}")
                data = st.get("data", {})
                if self.cache:
                    self.cache.put(key, data)
                return data
            time.sleep(5)
        raise FirecrawlError(f"agent job {job_id} timed out after {timeout_s}s")
