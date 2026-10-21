"""Process-wide token-bucket limiter and a daily quota counter.

- TokenBucket: SEC fair access (<= 10 req/s) shared by every EdgarClient in the process.
- DailyQuota: SAM.gov personal keys allow tens of calls per day; the counter persists to
  disk so separate runs on the same day share the cap.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity or max(1, int(rate_per_sec))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            time.sleep(wait)


class QuotaExceeded(RuntimeError):
    pass


class DailyQuota:
    """Persistent per-day call counter. `path` is shared across runs."""

    def __init__(self, path: Path, cap: int) -> None:
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()

    def _load(self) -> dict[str, int]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def used_today(self) -> int:
        return self._load().get(date.today().isoformat(), 0)

    def remaining(self) -> int:
        return max(0, self.cap - self.used_today())

    def take(self, n: int = 1) -> None:
        with self._lock:
            data = self._load()
            key = date.today().isoformat()
            used = data.get(key, 0)
            if used + n > self.cap:
                raise QuotaExceeded(
                    f"daily quota {self.cap} would be exceeded ({used} used, {n} requested)"
                )
            data = {key: used + n}  # drop older days
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
