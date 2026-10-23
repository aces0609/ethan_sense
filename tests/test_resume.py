"""PLAN 3.2: a killed run resumes at the failed section, across processes, with cumulative
accounting; and run.json itself survives a kill.

The "kill" is a researcher stand-in that raises for section 5 once, after the sibling sections
finished (their writes are in the checkpoint by then). A new process is simulated by dropping
the in-memory RunLog and EvidenceStore registries, so everything the resume needs has to come
from the run folder.

Empirical findings this file pins down (langgraph 1.2.16, langgraph-checkpoint-sqlite 3.1.2):

- when one of several parallel `Send` tasks fails, the sqlite checkpointer keeps the writes of
  the siblings that already finished, and a resume re-runs only the failed task;
- the section researcher subgraph (`main.RESEARCHER`, compiled without a checkpointer) inherits
  the parent's checkpointer when it is invoked inside the node, so a re-entered section resumes
  at the subgraph node that was running: `section_runs.N` (counted in plan_queries) therefore
  stays 1 when the kill came after plan_queries.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from primer import replay as replay_mod
from primer import runlog as runlog_mod
from primer.clients import cache
from primer.evidence import store as store_mod
from primer.graph import main as main_mod
from primer.graph.main import new_run, resume_primer, run_primer
from primer.runlog import RunLog, get_runlog
from primer.state import Inputs

SECTIONS = [1, 2, 5, 9]
RESEARCHED = {2, 5, 9}


class KilledOnce:
    """Stands in for the section researcher subgraph (`main.RESEARCHER`). Section `fail_on`
    runs for real on its first call and then raises, as if the process died right after it, but
    only once the other sections' results were handed back to LangGraph."""

    def __init__(
        self, real, fail_on: int = 5, wait_for: set[int] | None = None, when: str = "after"
    ) -> None:
        self.real = real
        self.fail_on = fail_on
        self.when = (
            when  # "after": the section finished inside the subgraph; "before": it never started
        )
        self.wait_for = set(wait_for if wait_for is not None else RESEARCHED - {fail_on})
        self.calls: Counter[int] = Counter()
        self.finished: set[int] = set()
        self.lock = threading.Lock()

    def invoke(self, payload, *args, **kwargs):
        sec = int(payload["brief"]["section"])
        with self.lock:
            self.calls[sec] += 1
            first = self.calls[sec] == 1
        out = (
            None
            if (first and self.when == "before" and sec == self.fail_on)
            else self.real.invoke(payload, *args, **kwargs)
        )
        if sec == self.fail_on and first:
            deadline = time.time() + 60
            while time.time() < deadline:
                with self.lock:
                    if self.wait_for <= self.finished:
                        break
                time.sleep(0.05)
            time.sleep(1.0)  # let LangGraph commit the siblings' writes to the checkpoint
            raise RuntimeError("simulated kill during section 5")
        with self.lock:
            self.finished.add(sec)
        return out


def _inputs(sections=None) -> Inputs:
    return Inputs(
        company_name="Lockheed Martin",
        ticker="LMT",
        as_of_date=date(2026, 10, 21),
        sections=sections or SECTIONS,
    )


def _new_process() -> None:
    """Forget everything a fresh process would not have: the live RunLogs (their run.json is
    what the old process autosaved) and the EvidenceStore singletons."""
    runlog_mod.flush_all()  # what the 15 s autosave / atexit write does
    runlog_mod._REGISTRY.clear()
    store_mod._REGISTRY.clear()


@pytest.fixture(autouse=True)
def _no_leaked_session():
    cache.deactivate()
    yield
    cache.deactivate()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("fresh_process", [True, False], ids=["new-process", "same-process"])
def test_killed_run_resumes_and_only_the_failed_section_runs_again(
    edgar_mock, output_dir, monkeypatch, fresh_process, when
):
    inputs = _inputs()
    run_id, run_dir = new_run(inputs)
    flaky = KilledOnce(main_mod.RESEARCHER, when=when)
    monkeypatch.setattr(main_mod, "RESEARCHER", flaky)

    with pytest.raises(RuntimeError, match="simulated kill"):
        run_primer(inputs, run_id=run_id, run_dir=run_dir)
    assert dict(flaky.calls) == {2: 1, 5: 1, 9: 1}
    assert not (run_dir / "primer.md").exists()
    assert (run_dir / "checkpoint.sqlite").exists() and (run_dir / "inputs.json").exists()
    before = get_runlog(run_dir)
    edgar_before = before.counters["edgar_requests"]
    assert edgar_before > 0
    if fresh_process:
        _new_process()

    result = resume_primer(run_dir)

    # LangGraph kept the finished siblings' writes: only section 5 executed a second time
    assert dict(flaky.calls) == {2: 1, 5: 2, 9: 1}
    md = Path(result["md_path"])
    assert md.exists() and md.parent == run_dir
    text = md.read_text(encoding="utf-8")
    for n in SECTIONS:
        assert f"## {n}. " in text
    v = result["validation"]
    assert v["checks"]["citations"]["ok"] and v["passed"]

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["counters"]["section_runs.2"] == 1
    # the researcher subgraph resumes from its own checkpoint, so a section that had already
    # passed plan_queries is not planned a second time; one killed before it started is
    # planned once, in the resumed process
    assert run["counters"]["section_runs.5"] == 1
    assert run["counters"]["section_runs.9"] == 1
    assert result["section_runs"] == {"2": 1, "5": 1, "9": 1}
    assert run["meta"]["resumed"] == 1 and result["resumed"] == 1
    assert run["meta"]["status"] in ("ok", "needs_review")
    # accounting is cumulative: nothing the first process counted was lost
    assert run["counters"]["edgar_requests"] >= edgar_before
    assert run["timings_seconds"]["total"] > 0
    assert "5" in run["coverage"] and "9" in run["coverage"]


class Killed(BaseException):
    """What a SIGTERM / hard stop looks like to the code: nothing in the pipeline catches it."""


def test_a_kill_inside_a_section_resumes_at_the_node_that_was_running(
    edgar_mock, output_dir, monkeypatch
):
    from primer.sections import sec05_market

    counting = KilledOnce(main_mod.RESEARCHER, fail_on=0)  # never raises: it only counts
    monkeypatch.setattr(main_mod, "RESEARCHER", counting)
    real_collect = sec05_market.collect
    collects: Counter[int] = Counter()

    def collect(plan_items, ctx):
        collects[ctx.section] += 1
        if collects[ctx.section] == 1:
            deadline = time.time() + 60  # let the siblings finish and commit first
            while time.time() < deadline and not (RESEARCHED - {5}) <= counting.finished:
                time.sleep(0.05)
            time.sleep(1.0)
            raise Killed()
        return real_collect(plan_items, ctx)

    monkeypatch.setattr(sec05_market, "collect", collect)
    inputs = _inputs()
    run_id, run_dir = new_run(inputs)
    with pytest.raises(Killed):
        run_primer(inputs, run_id=run_id, run_dir=run_dir)
    assert dict(counting.calls) == {2: 1, 5: 1, 9: 1}
    _new_process()

    result = resume_primer(run_dir)
    assert dict(counting.calls) == {2: 1, 5: 2, 9: 1}  # the siblings were not re-run
    assert collects[5] == 2  # the killed work ran again ...
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    # ... but the section was not planned again: its subgraph resumed at call_tools
    assert {k: v for k, v in run["counters"].items() if k.startswith("section_runs.")} == {
        "section_runs.2": 1,
        "section_runs.5": 1,
        "section_runs.9": 1,
    }
    assert Path(result["md_path"]).exists() and run["meta"]["resumed"] == 1


def test_resume_keeps_recording_so_the_resumed_run_can_still_be_replayed(
    edgar_mock, output_dir, monkeypatch
):
    inputs = _inputs()
    run_id, run_dir = new_run(inputs)
    flaky = KilledOnce(main_mod.RESEARCHER)
    monkeypatch.setattr(main_mod, "RESEARCHER", flaky)
    with pytest.raises(RuntimeError):
        run_primer(inputs, run_id=run_id, run_dir=run_dir)
    _new_process()
    assert cache.mode() == "record"  # still the old process's session ...
    cache.deactivate()  # ... which a real new process would not have
    resume_primer(run_dir)
    assert cache.mode() == "record"  # resume switched the record layer back on

    monkeypatch.setattr(main_mod, "RESEARCHER", flaky.real)
    n_requests = edgar_mock.calls.call_count
    result = replay_mod.replay_primer(run_dir)
    assert edgar_mock.calls.call_count == n_requests
    report = replay_mod.compare_runs(run_dir, result["replay_dir"])
    assert report["ok"], (report["diff_head"], report["missed_keys"])


def test_a_killed_replay_resumes_in_replay_mode(edgar_mock, output_dir, monkeypatch):
    inputs = _inputs()
    run_id, run_dir = new_run(inputs)
    run_primer(inputs, run_id=run_id, run_dir=run_dir)  # the original, uninterrupted

    flaky = KilledOnce(main_mod.RESEARCHER)
    monkeypatch.setattr(main_mod, "RESEARCHER", flaky)
    killed = replay_mod.replay_primer(run_dir)  # the replay dies in section 5
    assert "simulated kill" in killed["error"]
    replay_dir = Path(killed["replay_dir"])
    assert cache.mode() is None  # replay_primer cleaned up after itself

    n_requests = edgar_mock.calls.call_count
    _new_process()
    result = resume_primer(replay_dir)
    assert edgar_mock.calls.call_count == n_requests  # the resumed replay stayed offline
    assert dict(flaky.calls) == {2: 1, 5: 2, 9: 1}
    assert Path(result["md_path"]).exists()
    report = replay_mod.compare_runs(run_dir, replay_dir)
    assert report["primer_identical"], report["diff_head"]
    assert report["replay_misses"] == 0 and report["offline_violations"] == 0


# ---- run.json survives a kill ---------------------------------------------------------------


def test_runlog_state_is_loaded_back_by_get_runlog(tmp_path):
    rl = RunLog(tmp_path / "run")
    rl.count("edgar_requests", 7)
    rl.count("section_runs.5")
    rl.add_tokens("drafter", 100, 40)
    rl.query("firecrawl.search", "lockheed backlog", section=5)
    rl.error("section 5: boom")
    rl.plan["briefs"] = {"5": {"scope": "market"}}
    rl.coverage["9"] = "full"
    rl.critic_scores["9"] = {"mean": 4.5}
    rl.meta.update({"run_id": "r1", "ticker": "LMT", "status": "needs_review"})
    with rl.timed("resolve_entity"):
        pass
    rl.write()
    assert not list((tmp_path / "run").glob("*.tmp"))

    runlog_mod._REGISTRY.pop(str((tmp_path / "run").resolve()), None)
    back = get_runlog(tmp_path / "run")
    assert back is not rl and back.loaded_from_disk
    assert back.counters["edgar_requests"] == 7 and back.counters["section_runs.5"] == 1
    assert back.tokens["drafter"] == {"input": 100, "output": 40}
    assert back.queries[0]["query"] == "lockheed backlog"
    assert back.errors == ["section 5: boom"]
    assert back.plan == {"briefs": {"5": {"scope": "market"}}}
    assert back.coverage == {"9": "full"} and back.critic_scores == {"9": {"mean": 4.5}}
    assert "resolve_entity" in back.timings
    assert back.meta["resumed"] == 1 and back.meta["run_id"] == "r1"
    assert "status" not in back.meta and back.meta["previous_status"] == "needs_review"
    back.count("edgar_requests")
    assert back.counters["edgar_requests"] == 8  # cumulative from here on
    back.mark_resumed()  # the load already counted this resume
    assert back.meta["resumed"] == 1
    back.mark_resumed()  # a second in-process resume is a second resume
    assert back.meta["resumed"] == 2
    assert back.to_dict()["elapsed_seconds"] >= 0


def test_run_json_is_autosaved_and_survives_a_hard_kill(tmp_path):
    run_dir = tmp_path / "killed"
    script = textwrap.dedent(
        f"""
        import os, time
        from primer import runlog
        runlog.AUTOSAVE_SECONDS = 0.1
        os.makedirs({str(run_dir)!r}, exist_ok=True)  # new_run() made it in a real run
        rl = runlog.get_runlog({str(run_dir)!r})
        rl.count("edgar_requests", 3)
        rl.meta["ticker"] = "LMT"
        deadline = time.time() + 20
        while not os.path.exists({str(run_dir / "run.json")!r}) and time.time() < deadline:
            time.sleep(0.05)
        os._exit(1)  # no atexit, no finally: a kill -9
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 1, proc.stderr
    saved = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert saved["counters"] == {"edgar_requests": 3} and saved["meta"]["ticker"] == "LMT"
    back = get_runlog(run_dir)  # what the resuming process sees
    assert back.counters["edgar_requests"] == 3 and back.meta["resumed"] == 1


def test_finished_runs_are_not_rewritten_by_the_autosave(tmp_path):
    rl = get_runlog(tmp_path / "done")
    rl.meta["status"] = "ok"
    rl.write()
    before = (tmp_path / "done" / "run.json").read_text(encoding="utf-8")
    rl.count("late", 1)
    runlog_mod.flush_all()
    assert (tmp_path / "done" / "run.json").read_text(encoding="utf-8") == before
    (tmp_path / "going").mkdir()
    unfinished = get_runlog(tmp_path / "going")
    unfinished.count("x")
    runlog_mod.flush_all()
    assert json.loads((tmp_path / "going" / "run.json").read_text(encoding="utf-8"))[
        "counters"
    ] == {"x": 1}
