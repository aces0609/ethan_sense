"""Run accounting written to run.json (requirements §2 outputs, §8 cost and logging).

One RunLog per run_dir, shared by every node and client in the process via get_runlog().
Secrets are redacted on write.

Resume (requirements §8 Resumability): a RunLog survives a kill. A daemon thread rewrites
run.json every AUTOSAVE_SECONDS (atomically, so a kill mid-write never corrupts it) and
`atexit` writes it once more. `get_runlog(run_dir)` on a directory that already holds a
run.json loads the previous counters, tokens, timings, queries, errors, plan, coverage,
critic scores, validation and meta, and bumps `meta["resumed"]`, so a resumed run's
accounting is cumulative. A run that finished (`meta["status"]` set) is not autosaved again.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REGISTRY: dict[str, RunLog] = {}
_REG_LOCK = threading.Lock()

SECRET_ENV = ("ANTHROPIC_API_KEY", "FIRECRAWL_API_KEY", "SAM_GOV_API_KEY", "LANGSMITH_API_KEY")
AUTOSAVE_SECONDS = 15.0


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
        self.prior_elapsed = 0.0  # seconds spent by earlier processes of a resumed run
        self.counters: dict[str, int] = defaultdict(int)
        self.tokens: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0})
        self.timings: dict[str, float] = {}
        self.queries: list[dict[str, Any]] = []
        self.plan: dict[str, Any] = {}
        self.coverage: dict[str, str] = {}
        self.critic_scores: dict[str, Any] = {}
        self.validation: dict[str, Any] = {}
        self.reconciliation: dict[str, Any] = {}  # cross-section reconcile node record
        self.errors: list[str] = []
        self.meta: dict[str, Any] = {}
        self.loaded_from_disk = False
        self._session_start = datetime.now(UTC)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

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

    def mark_resumed(self) -> None:
        """Bump meta["resumed"] when this instance was not just loaded from disk (an
        in-process resume keeps the live RunLog, so get_runlog did not bump it)."""
        stale = self.meta.pop("status", None)  # unfinished again until export sets it
        if stale and "previous_status" not in self.meta:
            self.meta["previous_status"] = stale
        if self.loaded_from_disk:
            self.loaded_from_disk = False  # get_runlog already counted this resume
            return
        self.meta["resumed"] = int(self.meta.get("resumed", 0) or 0) + 1

    # ---- persistence -------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        elapsed = self.prior_elapsed + (datetime.now(UTC) - self._session_start).total_seconds()
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
            "reconciliation": self.reconciliation,
            "errors": self.errors,
        }

    def _dump(self) -> str:
        # other threads mutate the plain dicts (meta, plan, ...) without our lock; a dump that
        # races with one raises RuntimeError, so retry a few times. The lock is taken with a
        # timeout because a SIGTERM handler may run on the thread that already holds it.
        last: Exception | None = None
        for _ in range(5):
            got = self._lock.acquire(timeout=1.0)
            try:
                return json.dumps(self.to_dict(), indent=2, default=str)
            except RuntimeError as e:
                last = e
            finally:
                if got:
                    self._lock.release()
            time.sleep(0.01)
        raise last  # type: ignore[misc]

    def write(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / "run.json"
        text = redact(self._dump())
        got = self._write_lock.acquire(timeout=2.0)
        try:
            tmp = path.with_name(f"run.json.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp, path)
            except OSError:  # Windows: the target is briefly locked by a reader
                path.write_text(text, encoding="utf-8")
                tmp.unlink(missing_ok=True)
        finally:
            if got:
                self._write_lock.release()
        return path

    def load_previous(self, data: dict[str, Any]) -> None:
        """Merge a previous run.json into this fresh instance (resume across processes)."""
        try:
            self.started_at = datetime.fromisoformat(data["started_at"])
        except (KeyError, TypeError, ValueError):
            pass
        self.prior_elapsed = float(data.get("elapsed_seconds") or 0.0)
        for k, v in (data.get("counters") or {}).items():
            self.counters[k] = int(v)
        for role, v in (data.get("tokens") or {}).items():
            self.tokens[role] = {"input": int(v.get("input", 0)), "output": int(v.get("output", 0))}
        self.timings = {k: float(v) for k, v in (data.get("timings_seconds") or {}).items()}
        self.queries = list(data.get("queries") or [])
        self.plan = dict(data.get("plan") or {})
        self.coverage = dict(data.get("coverage") or {})
        self.critic_scores = dict(data.get("critic_scores") or {})
        self.validation = dict(data.get("validation") or {})
        self.reconciliation = dict(data.get("reconciliation") or {})
        self.errors = list(data.get("errors") or [])
        self.meta = dict(data.get("meta") or {})
        prior_status = self.meta.pop("status", None)  # a resumed run is unfinished until export
        if prior_status:
            self.meta["previous_status"] = prior_status
        self.meta["resumed"] = int(self.meta.get("resumed", 0) or 0) + 1
        self.loaded_from_disk = True


# ---- registry, autosave --------------------------------------------------------------------

_AUTOSAVER: threading.Thread | None = None


def _autosave_loop() -> None:
    while True:
        time.sleep(AUTOSAVE_SECONDS)
        flush_all()


def flush_all() -> None:
    """Write run.json for every live RunLog whose run has not finished. Never raises."""
    got = _REG_LOCK.acquire(timeout=1.0)
    try:
        logs = list(_REGISTRY.values())
    finally:
        if got:
            _REG_LOCK.release()
    for rl in logs:
        try:
            if rl.meta.get("status") or not rl.run_dir.exists():
                continue  # finished (its last write was the final one) or the folder is gone
            rl.write()
        except Exception:  # noqa: BLE001 - accounting must never take a run down
            pass


def _install_signal_flush() -> None:
    """On SIGTERM (and Ctrl+Break on Windows) write run.json, then die the way the signal
    would have killed the process. Only from the main thread and only over the default action,
    so an application's own handler is never replaced."""
    if threading.current_thread() is not threading.main_thread():
        return
    names = ["SIGTERM"] + (["SIGBREAK"] if os.name == "nt" else [])
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) is not signal.SIG_DFL:
                continue

            def handler(signum, frame, _sig=sig):  # noqa: ANN001
                flush_all()
                signal.signal(_sig, signal.SIG_DFL)
                os.kill(os.getpid(), _sig)

            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _ensure_autosaver() -> None:
    global _AUTOSAVER
    if _AUTOSAVER is None or not _AUTOSAVER.is_alive():
        _AUTOSAVER = threading.Thread(target=_autosave_loop, name="runlog-autosave", daemon=True)
        _AUTOSAVER.start()
        atexit.register(flush_all)
        _install_signal_flush()


def get_runlog(run_dir: str | Path) -> RunLog:
    key = str(Path(run_dir).resolve())
    with _REG_LOCK:
        if key not in _REGISTRY:
            rl = RunLog(Path(run_dir))
            prev = Path(run_dir) / "run.json"
            if prev.exists():
                try:
                    rl.load_previous(json.loads(prev.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass  # a corrupt run.json restarts the accounting rather than the run
            _REGISTRY[key] = rl
            _ensure_autosaver()
        return _REGISTRY[key]
