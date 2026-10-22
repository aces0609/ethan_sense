"""Content cache keyed by URL (+ optional date bucket) with a TTL (requirements §8 Caching),
plus the per-run record/replay layer behind `primer replay` (requirements §8 Reproducibility).

Global cache: output/.cache/{namespace}/{sha256}.json as {"key":..., "stored_at":..., "body":...}.
EDGAR and USAspending keys end in `|YYYY-MM-DD` (one fetch per day); Firecrawl keys carry no date
and live 7 days; SAM.gov 7 days.

Record / replay (one run per process; the state is module level):

- `activate(run_dir, "record")`: every `DiskCache.get` hit and every `put` is also written to
  `run_dir/replay/{namespace}/{sha256(canonical_key)}.json`. The canonical key drops the
  trailing `|YYYY-MM-DD`, so a run served entirely from a warm global cache is still fully
  recorded, and a replay on another day finds its entries. Failed fetches inside
  `get_or_fetch`, offline misses and failed Firecrawl POSTs are recorded as error entries so
  the replay reproduces the failure instead of reporting a miss.
- `activate(run_dir, "replay", log_dir=replay_run_dir)`: `DiskCache` reads ONLY from
  `run_dir/replay/{namespace}/` (no TTL, no date, no global cache, never writes); a missing
  entry raises `CacheMiss("replay: not recorded ...")` and is counted in the replay run's
  run.json as `replay_misses`, with the first 20 keys in `meta.replay_missed_keys`. Replay
  also installs a network guard: `httpx.Client.send` / `AsyncClient.send` raise
  `OfflineViolation` (counted as `offline_violations`) so "a network call raises" is
  enforced rather than assumed. `deactivate()` removes the guard.
- Model calls are recorded and replayed by `primer.llm` through `record_json` / `replay_json`
  below (namespace `llm`).

Every network path in the code base goes through a `DiskCache` (see the PLAN.md 3.3 notes for
the ones that only do so indirectly: Firecrawl agent polling, robots.txt 5xx).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MAX_LISTED = 20  # missed keys / violations listed in run.json
_DATE_SUFFIX = re.compile(r"\|\d{4}-\d{2}-\d{2}$")
_FIRECRAWL_API = "https://api.firecrawl.dev/v2"


class CacheMiss(KeyError):
    pass


class RecordedError(CacheMiss):
    """The original run failed to fetch this key (HTTP error, offline miss); the replay fails
    the same way. A CacheMiss subclass so callers that treat a miss as "go fetch" take their
    failure path; not counted as a replay miss."""


class OfflineViolation(RuntimeError):
    """A network request was attempted while an offline replay was active."""


def canonical_key(key: str) -> str:
    """The key without its trailing `|YYYY-MM-DD` date bucket."""
    return _DATE_SUFFIX.sub("", key)


def key_hash(key: str) -> str:
    return hashlib.sha256(canonical_key(key).encode("utf-8")).hexdigest()


# ---- the per-run session -----------------------------------------------------------------


class _Session:
    def __init__(self, run_dir: Path, mode: str, log_dir: Path | None, env: dict | None) -> None:
        self.run_dir = Path(run_dir)
        self.mode = mode
        self.log_dir = Path(log_dir) if log_dir else self.run_dir
        self.env = dict(env or {})
        self.lock = threading.Lock()
        self.missed: list[str] = []
        self.violations: list[str] = []

    # -- files
    def path(self, namespace: str, hashed: str) -> Path:
        return self.run_dir / "replay" / namespace / f"{hashed}.json"

    def write_raw(self, namespace: str, hashed: str, text: str, overwrite: bool) -> None:
        p = self.path(namespace, hashed)
        if not overwrite and p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)

    def runlog(self):
        from primer.runlog import get_runlog

        return get_runlog(self.log_dir)

    # -- accounting
    def miss(self, namespace: str, key: str) -> None:
        rl = self.runlog()
        rl.count("replay_misses")
        with self.lock:
            if len(self.missed) < MAX_LISTED:
                self.missed.append(f"{namespace}: {canonical_key(key)[:300]}")
            rl.meta["replay_missed_keys"] = list(self.missed)

    def violation(self, what: str) -> None:
        rl = self.runlog()
        rl.count("offline_violations")
        with self.lock:
            if len(self.violations) < MAX_LISTED:
                self.violations.append(what[:300])
            rl.meta["offline_violations"] = list(self.violations)

    # -- DiskCache hooks
    def record_body(self, namespace: str, key: str, text: str, overwrite: bool) -> None:
        self.write_raw(namespace, key_hash(key), text, overwrite)

    def record_error(self, namespace: str, key: str, err_type: str, message: str) -> None:
        text = json.dumps(
            {"key": canonical_key(key), "error": {"type": err_type, "message": message[:500]}}
        )
        # never replace a recorded body with an error from a later failed attempt
        p = self.path(namespace, key_hash(key))
        if p.exists():
            try:
                if "error" not in json.loads(p.read_text(encoding="utf-8")):
                    return
            except (OSError, ValueError):
                pass
        self.write_raw(namespace, key_hash(key), text, overwrite=True)

    def replay_get(self, namespace: str, key: str) -> Any:
        if namespace == "samgov" and self.env.get("samgov_key") is False:
            # the recorded run had no SAM.gov key: it never called the API
            raise RecordedError("replay: SAM.gov was not configured in the recorded run")
        p = self.path(namespace, key_hash(key))
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.miss(namespace, key)
            raise CacheMiss(
                f"replay: not recorded ({namespace}) {canonical_key(key)[:200]}"
            ) from None
        err = rec.get("error")
        if err:
            self.runlog().count("replay_recorded_errors")
            raise RecordedError(
                f"replay: recorded failure ({err.get('type')}): {err.get('message')}"
            )
        return rec["body"]


_SESSION: _Session | None = None
_SESSION_LOCK = threading.Lock()


def session() -> _Session | None:
    return _SESSION


def mode() -> str | None:
    """'record', 'replay' or None."""
    s = _SESSION
    return s.mode if s else None


def active_dir() -> Path | None:
    s = _SESSION
    return s.run_dir if s else None


def activate(
    run_dir: str | Path,
    mode: str,
    *,
    log_dir: str | Path | None = None,
    env: dict | None = None,
) -> None:
    """Start recording into, or replaying from, `run_dir/replay/`. One run per process: a new
    call replaces the previous session. For a replay, `run_dir` is the ORIGINAL run (where the
    recording lives) and `log_dir` the replay run whose run.json receives the miss counters."""
    global _SESSION
    if mode not in ("record", "replay"):
        raise ValueError(f"mode must be 'record' or 'replay', not {mode!r}")
    with _SESSION_LOCK:
        _uninstall_guard()
        _SESSION = _Session(Path(run_dir), mode, Path(log_dir) if log_dir else None, env)
        if mode == "replay":
            _install_guard()
        else:
            _install_recorder()


def deactivate() -> None:
    global _SESSION
    with _SESSION_LOCK:
        _uninstall_guard()
        _SESSION = None


# ---- model-call records (used by primer.llm) ---------------------------------------------


def record_json(namespace: str, hashed: str, payload: dict[str, Any]) -> None:
    s = _SESSION
    if s is not None and s.mode == "record":
        try:
            s.write_raw(namespace, hashed, json.dumps(payload, default=str), overwrite=True)
        except OSError:
            pass  # recording never fails a run


def replay_json(namespace: str, hashed: str) -> dict[str, Any] | None:
    """The recorded payload, or None when the replay has no entry (a miss for the caller to
    count)."""
    s = _SESSION
    if s is None or s.mode != "replay":
        return None
    try:
        return json.loads(s.path(namespace, hashed).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def count_llm_miss(what: str) -> None:
    s = _SESSION
    if s is None:
        return
    rl = s.runlog()
    rl.count("replay_llm_misses")
    with s.lock:
        listed = rl.meta.setdefault("replay_llm_missed", [])
        if len(listed) < MAX_LISTED:
            listed.append(what[:300])


# ---- network guard and Firecrawl failure recorder ----------------------------------------

_ORIG: dict[str, Any] = {}


def _install_guard() -> None:
    import httpx

    if "send" in _ORIG:
        return
    _ORIG["send"] = httpx.Client.send
    _ORIG["asend"] = httpx.AsyncClient.send

    def _blocked(self, request, *args, **kwargs):  # noqa: ANN001
        what = f"{request.method} {request.url}"
        s = _SESSION
        if s is not None:
            s.violation(what)
        raise OfflineViolation(f"offline replay: network request attempted: {what}")

    async def _ablocked(self, request, *args, **kwargs):  # noqa: ANN001
        _blocked(self, request)

    httpx.Client.send = _blocked  # type: ignore[method-assign]
    httpx.AsyncClient.send = _ablocked  # type: ignore[method-assign]


def _install_recorder() -> None:
    """Record the failures that never reach a cache `put`, so a replay fails the same way
    instead of reporting a miss: failed Firecrawl POSTs (4xx/5xx after retries, transport
    errors; keyed exactly as FirecrawlClient keys its cache, `POST {path} {sorted json
    body}`) and failed robots.txt GETs (5xx, transport errors; key `robots {origin}`). A
    later successful `put` for the same key overwrites the error entry."""
    import httpx

    if "send" in _ORIG:
        return
    _ORIG["send"] = httpx.Client.send
    _ORIG["asend"] = httpx.AsyncClient.send
    orig = _ORIG["send"]

    def _target(request) -> tuple[str, str] | None:  # noqa: ANN001
        url = str(request.url)
        if request.method == "POST" and url.startswith(_FIRECRAWL_API):
            body = json.loads(request.content or b"{}")
            return (
                "firecrawl",
                f"POST {url[len(_FIRECRAWL_API) :]} {json.dumps(body, sort_keys=True)}",
            )
        if request.method == "GET" and request.url.path == "/robots.txt":
            return "robots", f"robots {request.url.scheme}://{request.url.netloc.decode('ascii')}"
        return None

    def _note(request, status: int | None, message: str) -> None:  # noqa: ANN001
        s = _SESSION
        if s is None or s.mode != "record":
            return
        try:
            target = _target(request)
            if target:
                kind = f"HTTP {status}" if status else "TransportError"
                s.record_error(target[0], target[1], kind, message)
        except Exception:  # noqa: BLE001 - recording never fails a request
            pass

    def _recording_send(self, request, *args, **kwargs):  # noqa: ANN001
        try:
            watched = _target(request) is not None
        except Exception:  # noqa: BLE001
            watched = False
        if not watched:
            return orig(self, request, *args, **kwargs)
        try:
            resp = orig(self, request, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            _note(request, None, f"{type(e).__name__}: {e}")
            raise
        if resp.status_code >= 400:
            try:
                resp.read()
                _note(request, resp.status_code, resp.text[:300])
            except Exception:  # noqa: BLE001
                _note(request, resp.status_code, "")
        return resp

    httpx.Client.send = _recording_send  # type: ignore[method-assign]


def _uninstall_guard() -> None:
    if "send" not in _ORIG:
        return
    import httpx

    httpx.Client.send = _ORIG.pop("send")  # type: ignore[method-assign]
    httpx.AsyncClient.send = _ORIG.pop("asend")  # type: ignore[method-assign]


# ---- the cache -----------------------------------------------------------------------------


class DiskCache:
    def __init__(self, root: Path, namespace: str, ttl_seconds: float, offline: bool = False):
        self.root = root
        self.namespace = namespace
        self.dir = root / ".cache" / namespace
        self.ttl = ttl_seconds
        self.offline = offline

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str) -> Any:
        s = _SESSION
        if s is not None and s.mode == "replay":
            return s.replay_get(self.namespace, key)
        p = self._path(key)
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._offline_miss(s, key)
            raise CacheMiss(key) from None
        rec = json.loads(raw)
        if not self.offline and time.time() - rec["stored_at"] > self.ttl:
            raise CacheMiss(key)
        if s is not None and s.mode == "record":
            try:
                s.record_body(self.namespace, key, raw, overwrite=False)
            except OSError:
                pass
        return rec["body"]

    def _offline_miss(self, s: _Session | None, key: str) -> None:
        # an offline original that failed on a miss fails the same way in the replay
        if s is not None and s.mode == "record" and self.offline:
            try:
                s.record_error(self.namespace, key, "CacheMiss", "offline and not cached")
            except OSError:
                pass

    def put(self, key: str, body: Any) -> None:
        s = _SESSION
        if s is not None and s.mode == "replay":
            return  # a replay never writes the global cache
        self.dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"key": key, "stored_at": time.time(), "body": body})
        self._path(key).write_text(text, encoding="utf-8")
        if s is not None and s.mode == "record":
            try:
                s.record_body(self.namespace, key, text, overwrite=True)
            except OSError:
                pass

    def get_or_fetch(self, key: str, fetch) -> Any:
        s = _SESSION
        if s is not None and s.mode == "replay":
            return self.get(key)  # recorded body, recorded failure or a counted miss: no fetch
        try:
            return self.get(key)
        except CacheMiss:
            if self.offline:
                raise CacheMiss(f"offline and not cached: {key}") from None
            try:
                body = fetch()
            except Exception as e:
                if s is not None and s.mode == "record":
                    try:
                        s.record_error(self.namespace, key, type(e).__name__, str(e))
                    except OSError:
                        pass
                raise
            self.put(key, body)
            return body
