"""Offline replay of a recorded run (PLAN 3.3, requirements §8 Reproducibility).

How a replay reproduces a run:

1. `intake` calls `start_recording`, which saves the run's inputs and the environment flags
   that change behaviour (`inputs.json`) and switches the record layer on
   (`primer.clients.cache`, `primer.llm`): every cache hit/put and every model outcome is
   copied to `run_dir/replay/`.
2. `replay_primer(run_dir)` makes a sibling folder `{run_id}-replay`, copies the original
   `evidence/` into it (so `EvidenceStore` dedupes to the same `src_NNNN` ids that appear in
   model prompts), rebuilds `Inputs` from `inputs.json` with the original `as_of_date` and
   `replay=True, replay_of=<original>`, mirrors the environment (which keys were present, so
   the same clients exist), turns replay mode on (DiskCache reads only the recording, model
   calls come from the recording, `httpx` raises `OfflineViolation`) and runs the normal graph.
3. `compare_runs` diffs the two primers, sources and claims.

`Inputs.offline` of the replay: the original's value, or True when the original had a
Firecrawl key. Offline clients fail with their own offline errors (`FirecrawlError("offline
and not cached")`) which every caller already handles; a keyless original keeps
`offline=False` so the same clients are absent as in the original. One run per process.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from primer import __version__
from primer.clients import cache
from primer.render.markdown import primer_path
from primer.state import Inputs

INPUTS_FILE = "inputs.json"
_RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}(?:-replay\d*)?")


# ---- recording (called from intake) ---------------------------------------------------------


def environment_flags() -> dict[str, bool]:
    """What the environment provides that changes which clients and branches a run takes."""
    from primer.config import llm_available

    return {
        "llm": llm_available(),
        "firecrawl_key": bool(os.environ.get("FIRECRAWL_API_KEY", "").strip()),
        "samgov_key": bool(os.environ.get("SAM_GOV_API_KEY", "").strip()),
    }


def start_recording(run_dir: str | Path, inputs: Inputs, run_id: str | None = None) -> None:
    """Persist the inputs and environment flags and, unless this run is itself a replay,
    switch the record layer on for it. Idempotent."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id or run_dir.name,
        "inputs": inputs.model_dump(mode="json"),
        "env": environment_flags(),
        "generator_version": __version__,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if not inputs.replay:
        payload["code"] = code_version()
        snapshot_config(run_dir)
    (run_dir / INPUTS_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if not inputs.replay:
        cache.activate(run_dir, "record")


def code_version() -> dict[str, Any]:
    """Git commit and whether the source tree had uncommitted changes when the run started, so a
    replay can say whether a mismatch could come from the code rather than from nondeterminism."""
    import subprocess

    from primer.config import REPO_ROOT

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO_ROOT), *args],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    status = git("status", "--porcelain", "--", "src", "config")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(status)}


def snapshot_config(run_dir: Path) -> None:
    """Copy config/*.yaml into run_dir/config: config is an input to a run (it shapes the
    prompts), so an exact replay reads this copy, not whatever the repo holds later."""
    from primer.config import CONFIG_DIR

    dest = Path(run_dir) / "config"
    dest.mkdir(parents=True, exist_ok=True)
    for f in Path(CONFIG_DIR).glob("*.yaml"):
        shutil.copy2(f, dest / f.name)


def read_inputs(run_dir: str | Path) -> tuple[Inputs, dict[str, Any]]:
    """(Inputs, env flags) saved by `start_recording`."""
    path = Path(run_dir) / INPUTS_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found: the run predates the record/replay layer or never reached intake"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return Inputs.model_validate(data["inputs"]), dict(data.get("env") or {})


# ---- the replay session ---------------------------------------------------------------------


@contextmanager
def replay_environment(env: dict[str, Any]) -> Iterator[None]:
    """Make the process environment look like the recorded run's: the same provider key
    presence (a placeholder when the recording used a model but this machine has no key, blank
    when it did not) and the same Firecrawl / SAM.gov key presence. Restored on exit."""
    from primer.config import active_provider, models_config

    saved: dict[str, str | None] = {}

    def setenv(name: str, value: str) -> None:
        saved.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    cfg = models_config()
    providers = cfg.get("providers") or {}
    if env.get("llm"):
        if active_provider() is None:
            name = os.environ.get("PRIMER_MODEL_PROVIDER", "").strip() or cfg.get("provider")
            key_env = (providers.get(name) or {}).get("key_env")
            if key_env:
                setenv(key_env, "replay-offline")
    else:
        for spec in providers.values():
            if spec.get("key_env"):
                setenv(spec["key_env"], "")
    if env.get("firecrawl_key") is False:
        setenv("FIRECRAWL_API_KEY", "")
    if env.get("samgov_key") is False:
        setenv("SAM_GOV_API_KEY", "")
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


@contextmanager
def replay_session(original: Path, replay_dir: Path, env: dict[str, Any]) -> Iterator[None]:
    from primer.config import use_config_dir

    snapshot = Path(original) / "config"
    if snapshot.is_dir():
        use_config_dir(snapshot)
    with replay_environment(env):
        cache.activate(original, "replay", log_dir=replay_dir, env=env)
        try:
            yield
        finally:
            cache.deactivate()
            if snapshot.is_dir():
                use_config_dir(None)


@contextmanager
def session_for_resume(run_dir: Path) -> Iterator[bool]:
    """Re-activate the right layer before a killed run resumes: replay mode for a replay run,
    record mode (appending to the same recording) otherwise. Yields True for a replay."""
    run_dir = Path(run_dir)
    try:
        inputs, env = read_inputs(run_dir)
    except FileNotFoundError:
        yield False
        return
    if inputs.replay and inputs.replay_of:
        with replay_session(Path(inputs.replay_of), run_dir, env):
            yield True
        return
    from primer.config import use_config_dir

    snapshot = run_dir / "config"
    if snapshot.is_dir():
        use_config_dir(snapshot)
    cache.activate(run_dir, "record")
    try:
        yield False
    finally:
        if snapshot.is_dir():
            use_config_dir(None)


def _replay_dir_for(original: Path) -> Path:
    base = original.with_name(f"{original.name}-replay")
    if not base.exists():
        return base
    n = 2
    while original.with_name(f"{original.name}-replay{n}").exists():
        n += 1
    return original.with_name(f"{original.name}-replay{n}")


def replay_primer(run_dir: str | Path) -> dict[str, Any]:
    """Re-run a recorded primer from its recording: no network, no model. Returns the
    run_primer result plus `original`, `replay_dir` and the replay counters."""
    from primer.graph.main import run_primer
    from primer.runlog import get_runlog

    original = Path(run_dir)
    inputs, env = read_inputs(original)
    if inputs.replay and inputs.replay_of:  # replaying a replay replays the original
        original = Path(inputs.replay_of)
        inputs, env = read_inputs(original)
    if not (original / "replay").is_dir():
        raise FileNotFoundError(f"{original / 'replay'} not found: nothing was recorded")
    if not (original / "evidence" / "index.json").exists():
        raise FileNotFoundError(f"{original / 'evidence' / 'index.json'} not found")

    replay_dir = _replay_dir_for(original)
    replay_dir.mkdir(parents=True)
    shutil.copytree(original / "evidence", replay_dir / "evidence")

    run_id = replay_dir.name
    offline = bool(inputs.offline or env.get("firecrawl_key", True))
    replay_inputs = inputs.model_copy(
        update={"offline": offline, "replay": True, "replay_of": str(original)}
    )
    rl = get_runlog(replay_dir)
    rl.meta["replay_of"] = str(original)
    with replay_session(original, replay_dir, env):
        try:
            result = run_primer(replay_inputs, run_id=run_id, run_dir=replay_dir)
        except Exception as e:  # noqa: BLE001 - a replay that dies on a missing recording still reports
            rl.error(f"replay failed: {e!r}")
            rl.meta["status"] = "replay_failed"
            rl.write()
            result = {
                "run_id": run_id,
                "run_dir": str(replay_dir),
                "md_path": None,
                "error": repr(e),
            }
    counters = get_runlog(replay_dir).counters
    try:
        recorded = (
            json.loads((original / INPUTS_FILE).read_text(encoding="utf-8")).get("code") or {}
        )
    except (OSError, ValueError):
        recorded = {}
    now = code_version()
    result.update(
        {
            "config_snapshot": (original / "config").is_dir(),
            "code_recorded": recorded,
            "code_now": now,
            "code_changed": bool(
                recorded.get("dirty")
                or now.get("dirty")
                or recorded.get("commit") != now.get("commit")
            ),
            "original": str(original),
            "replay_dir": str(replay_dir),
            "replay_misses": int(counters.get("replay_misses", 0)),
            "replay_llm_misses": int(counters.get("replay_llm_misses", 0)),
            "offline_violations": int(counters.get("offline_violations", 0)),
            "missed_keys": list(rl.meta.get("replay_missed_keys", [])),
            "missed_llm": list(rl.meta.get("replay_llm_missed", [])),
            "violation_urls": list(rl.meta.get("offline_violations", [])),
        }
    )
    return result


# ---- comparison -----------------------------------------------------------------------------


def _normalize_md(text: str) -> list[str]:
    """Lines of a primer without the parts that differ between runs by design: the run_id and
    trace_id frontmatter lines and the run ids on 'Generated by' lines."""
    out: list[str] = []
    fence = 0
    for line in text.splitlines():
        if line.strip() == "---" and fence < 2:
            fence += 1
            out.append(line)
            continue
        if fence == 1 and line.startswith(("run_id:", "trace_id:")):
            continue
        if "Generated by" in line:
            line = _RUN_ID.sub("<run_id>", line)
        out.append(line)
    return out


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _source_rows(sources: Any) -> list[tuple[str, str, str]]:
    rows = (sources or {}).get("sources", []) if isinstance(sources, dict) else []
    return sorted(
        (r.get("source_id", ""), r.get("url", ""), r.get("content_hash", "")) for r in rows
    )


def compare_runs(original: str | Path, replay: str | Path) -> dict[str, Any]:
    """Diff the replayed run against the original: primer.md (ignoring run ids), sources.json
    (ids, URLs, hashes) and claims.json (order-insensitive), plus the replay counters."""
    original, replay = Path(original), Path(replay)
    a = _normalize_md(primer_path(original).read_text(encoding="utf-8"))
    b_path = primer_path(replay)
    b = _normalize_md(b_path.read_text(encoding="utf-8")) if b_path.exists() else []
    diff = list(difflib.unified_diff(a, b, "original", "replay", lineterm="", n=0))
    changed = [d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))]

    sa, sb = (
        _source_rows(_load_json(original / "sources.json")),
        _source_rows(_load_json(replay / "sources.json")),
    )
    sources_diff = [f"- {r}" for r in sa if r not in sb] + [f"+ {r}" for r in sb if r not in sa]

    def claims(p: Path) -> list[str]:
        return sorted(json.dumps(c, sort_keys=True) for c in (_load_json(p) or []))

    ca, cb = claims(original / "claims.json"), claims(replay / "claims.json")
    claims_diff = [f"- {c}" for c in ca if c not in cb] + [f"+ {c}" for c in cb if c not in ca]

    run = _load_json(replay / "run.json") or {}
    counters = run.get("counters", {})
    meta = run.get("meta", {})
    misses = int(counters.get("replay_misses", 0))
    llm_misses = int(counters.get("replay_llm_misses", 0))
    violations = int(counters.get("offline_violations", 0))
    primer_identical = bool(a) and a == b
    return {
        "primer_identical": primer_identical,
        "diff_lines": len(changed),
        "diff_head": diff[:20],
        "sources_identical": sa == sb,
        "sources_diff": sources_diff[:20],
        "claims_identical": ca == cb,
        "claims_diff": claims_diff[:20],
        "replay_misses": misses,
        "replay_llm_misses": llm_misses,
        "offline_violations": violations,
        "missed_keys": list(meta.get("replay_missed_keys", [])),
        "missed_llm": list(meta.get("replay_llm_missed", [])),
        "violation_urls": list(meta.get("offline_violations", [])),
        "ok": primer_identical and misses == 0 and llm_misses == 0 and violations == 0,
    }
