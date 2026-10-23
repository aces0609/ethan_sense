"""robots.txt policy and the run-level domain-policy ledger (requirements §8 Paywalls and terms).

`robots_allowed(url)` answers "may a bot with our user-agent token fetch this URL?" from the
host's robots.txt, fetched at most once per host per day and cached through `DiskCache`
(namespace `robots`, 24 h TTL) so an offline replay sees the same answers as the original run.

Rules:
- 404 (any 4xx) means the host publishes no robots.txt: allow (RFC 9309 treats an
  unavailable file as no restriction).
- a network error or a 5xx is allow plus a counted warning (`robots_fetch_errors`); errors are
  not cached on disk, only remembered in-process so one dead host is not retried per URL.
- tier-1 government and SEC hosts are never checked (we use their APIs, not scraping), nor
  are hosts in `sources.yaml` robots.skip_hosts.
- offline mode never touches the network: a cached robots.txt is honoured, a miss allows.
- `robots.respect: false` in config turns the whole check off.

`urllib.robotparser` implements the original robots.txt convention: it does not expand `*`
or `$` inside paths, so a wildcard rule is matched literally (more permissive than a
wildcard-aware crawler, never stricter).

`record_policy` keeps the per-run ledger (`run.json` meta.policy and meta.blocked): one
decision per URL (allowed, limited, denied, robots_blocked), the last one wins.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from datetime import date
from typing import Any
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from primer.clients.cache import CacheMiss, DiskCache
from primer.config import output_root, sources_config, tier_for_url
from primer.runlog import RunLog

log = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 24 * 3600
FETCH_TIMEOUT_SECONDS = 10.0
DEFAULT_TOKEN = "ethan_sense"

# (output root, origin, day) -> parser; None means "no usable robots.txt, allow everything"
_PARSERS: dict[tuple[str, str, str], robotparser.RobotFileParser | None] = {}
_ORIGIN_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_REG_LOCK = threading.Lock()
# a robots.txt fetch that failed, by the same key as _PARSERS: (error type, message). A later run in
# the same process answers from the memo, so it re-records the failure for its own replay.
_FAILED: dict[tuple[str, str, str], tuple[str, str]] = {}


def robots_config() -> dict[str, Any]:
    cfg = dict(sources_config().get("robots") or {})
    cfg.setdefault("respect", True)
    cfg.setdefault("user_agent_token", DEFAULT_TOKEN)
    cfg.setdefault("skip_hosts", [])
    return cfg


def fetch_user_agent() -> str:
    """`ethan_sense/0.1 (+contact email)`; the email comes from EDGAR_USER_AGENT."""
    import re

    token = str(robots_config()["user_agent_token"])
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", os.environ.get("EDGAR_USER_AGENT", ""))
    contact = f" (+{m.group(0)})" if m else ""
    return f"{token}/0.1{contact}"


def reset_memo() -> None:
    """Forget the in-process parsers (tests; the disk cache is untouched)."""
    with _REG_LOCK:
        _PARSERS.clear()
        _ORIGIN_LOCKS.clear()
        _FAILED.clear()


def _host_matches(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _count(runlog: RunLog | None, key: str) -> None:
    if runlog is not None:
        runlog.count(key)


def _fetch(origin: str, client: httpx.Client | None) -> tuple[int, str]:
    """GET {origin}/robots.txt -> (status, text). Raises httpx.HTTPError or anything the
    transport raises; the caller turns that into allow plus a counted warning."""
    headers = {"User-Agent": fetch_user_agent(), "Accept": "text/plain,*/*;q=0.5"}
    url = f"{origin}/robots.txt"
    if client is not None:
        r = client.get(url, headers=headers, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    else:
        r = httpx.get(url, headers=headers, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    return r.status_code, r.text if r.status_code == 200 else ""


def _parser_for(
    origin: str,
    *,
    client: httpx.Client | None,
    cache: DiskCache,
    offline: bool,
    runlog: RunLog | None,
) -> tuple[robotparser.RobotFileParser | None, str]:
    """(parser or None, note). None means allow everything; the note explains why."""
    key = (str(cache.dir), origin, date.today().isoformat())

    def memo_hit() -> tuple[robotparser.RobotFileParser | None, str]:
        failed = _FAILED.get(key)
        if failed:
            cache.record_failure(f"robots {origin}", *failed)
        else:
            cache.record_hit(f"robots {origin}")
        return _PARSERS[key], "memo"

    with _REG_LOCK:
        if key in _PARSERS:
            return memo_hit()
        lock = _ORIGIN_LOCKS.setdefault(key, threading.Lock())
    with lock:
        with _REG_LOCK:
            if key in _PARSERS:
                return memo_hit()
        parser: robotparser.RobotFileParser | None = None
        note = ""
        try:
            rec = cache.get(f"robots {origin}")
            status, text = int(rec["status"]), str(rec["text"])
        except CacheMiss:
            if offline:
                # replay never goes to the network; no recorded robots.txt means no recorded
                # block (not memoised: an online call later in the process may still fetch)
                return None, "offline and robots.txt not cached"
            try:
                status, text = _fetch(origin, client)
            except Exception as e:  # network error, respx's all-mocked assertion, anything
                _count(runlog, "robots_fetch_errors")
                log.warning("robots.txt fetch failed for %s: %r", origin, e)
                cache.record_failure(f"robots {origin}", type(e).__name__, repr(e))
                with _REG_LOCK:
                    _FAILED[key] = (type(e).__name__, repr(e))
                    _PARSERS[key] = None
                return None, f"robots.txt fetch failed ({type(e).__name__})"
            if status < 500:
                cache.put(f"robots {origin}", {"status": status, "text": text})
            else:
                _count(runlog, "robots_fetch_errors")
                log.warning("robots.txt fetch for %s returned %s", origin, status)
                cache.record_failure(f"robots {origin}", "HTTPStatus", f"HTTP {status}")
                with _REG_LOCK:
                    _FAILED[key] = ("HTTPStatus", f"HTTP {status}")
                    _PARSERS[key] = None
                return None, f"robots.txt fetch failed (HTTP {status})"
        if status == 200:
            parser = robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            note = "robots.txt"
        else:
            note = f"no robots.txt (HTTP {status})"
        with _REG_LOCK:
            _PARSERS[key] = parser
        return parser, note


def robots_allowed(
    url: str,
    user_agent: str | None = None,
    *,
    client: httpx.Client | None = None,
    runlog: RunLog | None = None,
    offline: bool = False,
    cache: DiskCache | None = None,
) -> tuple[bool, str]:
    """May `user_agent` (the robots token; default from config) fetch `url`? -> (allowed, reason)."""
    cfg = robots_config()
    if not cfg.get("respect", True):
        return True, "robots.txt checking disabled in config"
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return True, "not an http(s) URL"
    host = (p.hostname or "").lower()
    if tier_for_url(url) == 1:
        return True, f"robots.txt not checked for tier-1 host {host}"
    if _host_matches(host, [str(h).lower() for h in cfg.get("skip_hosts") or []]):
        return True, f"robots.txt not checked for {host} (skip_hosts)"
    token = user_agent or str(cfg["user_agent_token"])
    origin = f"{p.scheme}://{p.netloc.lower()}"
    cache = cache or DiskCache(
        output_root(), "robots", ttl_seconds=ROBOTS_TTL_SECONDS, offline=offline
    )
    parser, note = _parser_for(origin, client=client, cache=cache, offline=offline, runlog=runlog)
    if parser is None:
        return True, f"allowed: {note}"
    if parser.can_fetch(token, url):
        return True, "allowed by robots.txt"
    return False, f"disallowed by {origin}/robots.txt for user-agent {token}"


# ---- the run-level ledger ------------------------------------------------------------------

DECISIONS = ("allowed", "limited", "denied", "robots_blocked")


class _Ledger:
    """Per-RunLog decisions. `base` holds the counts a resumed run already carried in its
    run.json (the per-URL decisions of the earlier process are gone); `urls` is this process."""

    def __init__(self, runlog: RunLog) -> None:
        prior = runlog.meta.get("policy") or {}
        self.base = {d: int(prior.get(d, 0) or 0) for d in DECISIONS}
        self.urls: dict[str, tuple[str, str | None]] = {}
        self.blocked: list[dict[str, str]] = list(runlog.meta.get("blocked") or [])
        for b in self.blocked:  # already counted in base; a repeat of the URL must not recount
            self.urls[b["url"]] = ("robots_blocked", b.get("reason"))
            self.base["robots_blocked"] = max(0, self.base["robots_blocked"] - 1)


_LEDGERS: weakref.WeakKeyDictionary[RunLog, _Ledger] = weakref.WeakKeyDictionary()
_LEDGER_LOCK = threading.Lock()


def record_policy(
    runlog: RunLog | None, url: str, decision: str, reason: str | None = None
) -> None:
    """Record the domain-policy decision for `url` in run.json: `meta.policy` holds the counts
    over distinct URLs (the last decision for a URL wins), `meta.blocked` lists robots-blocked
    URLs with the reason (not errors: the section carries on without that page)."""
    if runlog is None:
        return
    if decision not in DECISIONS:
        raise ValueError(f"unknown policy decision {decision!r}")
    with _LEDGER_LOCK:
        ledger = _LEDGERS.get(runlog)
        if ledger is None:
            ledger = _LEDGERS[runlog] = _Ledger(runlog)
        # a URL once blocked or denied stays so; a later 'allowed' (search listing) is weaker
        prior = ledger.urls.get(url)
        if prior and prior[0] in ("denied", "robots_blocked") and decision == "allowed":
            return
        ledger.urls[url] = (decision, reason)
        counts = dict(ledger.base)
        for dec, _ in ledger.urls.values():
            counts[dec] += 1
        runlog.meta["policy"] = counts
        blocked = [
            {"url": u, "reason": r or "", "host": urlparse(u).netloc.lower()}
            for u, (dec, r) in ledger.urls.items()
            if dec == "robots_blocked"
        ]
        if blocked:
            runlog.meta["blocked"] = blocked
        else:
            runlog.meta.pop("blocked", None)
