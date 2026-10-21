"""Run accounting written to run.json (requirements §2 outputs, §8 cost and logging).

One RunLog per run_dir, shared by every node and client in the process via get_runlog().
Secrets are redacted on write.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REGISTRY: dict[str, RunLog] = {}
_REG_LOCK = threading.Lock()

SECRET_ENV = ("ANTHROPIC_API_KEY", "FIRECRAWL_API_KEY", "SAM_GOV_API_KEY", "LANGSMITH_API_KEY")


def redact(text: str) -> str:
    for name in SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) > 6:
            text = text.replace(val, f"<{name}>")
    # api_key query params
    text = re.sub(r"(api_key=)[^&\s\"']+", r"\1<redacted>", text)
    return text


class RunLog:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.started_at = datetime.now(UTC)
        self.counters: dict[str, int] = defaultdict(int)
        self.tokens: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0})
        self.timings: dict[str, float] = {}
        self.queries: list[dict[str, Any]] = []
        self.plan: dict[str, Any] = {}
        self.coverage: dict[str, str] = {}
        self.critic_scores: dict[str, Any] = {}
        self.validation: dict[str, Any] = {}
        self.errors: list[str] = []
        self.meta: dict[str, Any] = {}
        self._lock = threading.Lock()

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] += n

    def add_tokens(self, role: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.tokens[role]["input"] += input_tokens
            self.tokens[role]["output"] += output_tokens

    def query(self, tool: str, query: str, **extra: Any) -> None:
        with self._lock:
            self.queries.append(
                {"ts": datetime.now(UTC).isoformat(), "tool": tool, "query": redact(query), **extra}
            )

    @contextmanager
    def timed(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.timings[name] = self.timings.get(name, 0.0) + (time.perf_counter() - t0)

    def error(self, msg: str) -> None:
        with self._lock:
            self.errors.append(redact(msg))

    def to_dict(self) -> dict[str, Any]:
        elapsed = (datetime.now(UTC) - self.started_at).total_seconds()
        return {
            "started_at": self.started_at.isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "meta": self.meta,
            "counters": dict(self.counters),
            "tokens": {k: dict(v) for k, v in self.tokens.items()},
            "timings_seconds": {k: round(v, 2) for k, v in self.timings.items()},
            "queries": self.queries,
            "plan": self.plan,
            "coverage": self.coverage,
            "critic_scores": self.critic_scores,
            "validation": self.validation,
            "errors": self.errors,
        }

    def write(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / "run.json"
        text = json.dumps(self.to_dict(), indent=2, default=str)
        path.write_text(redact(text), encoding="utf-8")
        return path


def get_runlog(run_dir: str | Path) -> RunLog:
    key = str(Path(run_dir).resolve())
    with _REG_LOCK:
        if key not in _REGISTRY:
            _REGISTRY[key] = RunLog(Path(run_dir))
        return _REGISTRY[key]
