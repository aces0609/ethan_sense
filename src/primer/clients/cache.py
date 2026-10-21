"""Content cache keyed by URL (+ optional date bucket) with a TTL (requirements §8 Caching).

Stored under output/.cache/{namespace}/{sha256}.json as {"url":..., "stored_at":..., "body":...}.
Offline runs read only from here and raise on a miss.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class CacheMiss(KeyError):
    pass


class DiskCache:
    def __init__(self, root: Path, namespace: str, ttl_seconds: float, offline: bool = False):
        self.dir = root / ".cache" / namespace
        self.ttl = ttl_seconds
        self.offline = offline

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str) -> Any:
        p = self._path(key)
        if not p.exists():
            raise CacheMiss(key)
        rec = json.loads(p.read_text(encoding="utf-8"))
        if not self.offline and time.time() - rec["stored_at"] > self.ttl:
            raise CacheMiss(key)
        return rec["body"]

    def put(self, key: str, body: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps({"key": key, "stored_at": time.time(), "body": body}),
            encoding="utf-8",
        )

    def get_or_fetch(self, key: str, fetch) -> Any:
        try:
            return self.get(key)
        except CacheMiss:
            if self.offline:
                raise CacheMiss(f"offline and not cached: {key}") from None
            body = fetch()
            self.put(key, body)
            return body
