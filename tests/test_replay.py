"""PLAN 3.1 / 3.3: the content cache serves a second run of the same ticker, and a recorded run
replays offline into the same primer.

Everything runs on the LMT fixtures in template mode (provider keys blanked in conftest).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from primer import replay as replay_mod
from primer.clients import cache
from primer.clients.cache import CacheMiss, DiskCache, OfflineViolation, canonical_key
from primer.graph.main import run_primer
from primer.runlog import get_runlog
from primer.state import Inputs
from tests.conftest import _hit

WEB = Path(__file__).parent / "fixtures" / "web"
AS_OF = date(2026, 10, 21)
LATER = date(2026, 11, 5)


@pytest.fixture(autouse=True)
def _no_leaked_session():
    cache.deactivate()
    yield
    cache.deactivate()


def _inputs(sections) -> Inputs:
    return Inputs(company_name="Lockheed Martin", ticker="LMT", as_of_date=AS_OF, sections=sections)


def _advance_clock(monkeypatch, day: date = LATER) -> None:
    """date.today() is the day in every cache key; move it in each module that reads it."""

    class FakeDate(date):
        @classmethod
        def today(cls):
            return day

    for mod in (
        "primer.clients.edgar",
        "primer.clients.usaspending",
        "primer.clients.samgov",
        "primer.clients.robots",
        "primer.adapters.market_data_scraped",
        "primer.evidence.store",
    ):
        monkeypatch.setattr(f"{mod}.date", FakeDate)


def _drop_global_cache(output_dir: Path) -> None:
    import shutil

    shutil.rmtree(output_dir / ".cache", ignore_errors=True)


def _web_mock(firecrawl_mock):
    return firecrawl_mock(
        search={
            "transcript": [
                _hit(
                    "https://www.fool.com/earnings/call-transcripts/2026/07/22/lockheed-martin-lmt-q2-2026-earnings-call-transcript/",
                    "Lockheed Martin (LMT) Q2 2026 Earnings Call Transcript",
                )
            ],
            "market share": [
                _hit(
                    "https://www.defensenews.com/industry/2026/05/20/lockheed-vs-rtx-missile-market/",
                    "Lockheed vs. RTX: who leads the missile market as budgets shift to munitions",
                )
            ],
        },
        pages={
            "fool.com": (WEB / "transcript_q2_2026.md").read_text(encoding="utf-8"),
            "defensenews.com": (WEB / "defensenews_lmt_vs_rtx.md").read_text(encoding="utf-8"),
        },
    )


# ---- the cache layer itself ----------------------------------------------------------------


def test_canonical_key_drops_only_a_trailing_date():
    assert canonical_key("https://x/y?q=1|2026-10-22") == "https://x/y?q=1"
    assert canonical_key('POST /p {"d": "2026-10-22"}') == 'POST /p {"d": "2026-10-22"}'
    assert canonical_key("a|2026-10-22|b") == "a|2026-10-22|b"


def test_record_mode_copies_hits_and_puts_under_the_canonical_key(output_dir, tmp_path):
    run_dir = tmp_path / "run"
    c = DiskCache(output_dir, "edgar", ttl_seconds=60)
    c.put("https://x/a|2026-10-21", {"v": 1})  # before recording: only in the global cache
    cache.activate(run_dir, "record")
    assert c.get("https://x/a|2026-10-21") == {"v": 1}  # a hit is recorded
    c.put("https://x/b|2026-10-21", {"v": 2})  # a put is recorded
    files = sorted(p.name for p in (run_dir / "replay" / "edgar").glob("*.json"))
    assert files == sorted(
        [cache.key_hash("https://x/a") + ".json", cache.key_hash("https://x/b") + ".json"]
    )
    # another day, empty global cache: the replay still finds both
    cache.activate(run_dir, "replay", log_dir=tmp_path / "log")
    other = DiskCache(tmp_path / "elsewhere", "edgar", ttl_seconds=0)
    assert other.get("https://x/a|2026-11-30") == {"v": 1}
    assert other.get_or_fetch("https://x/b|2026-11-30", lambda: pytest.fail("fetched")) == {"v": 2}


def test_replay_miss_is_counted_listed_and_never_fetches(output_dir, tmp_path):
    run_dir, log_dir = tmp_path / "run", tmp_path / "log"
    (run_dir / "replay" / "edgar").mkdir(parents=True)
    cache.activate(run_dir, "replay", log_dir=log_dir)
    c = DiskCache(output_dir, "edgar", ttl_seconds=60)
    with pytest.raises(CacheMiss, match="replay: not recorded"):
        c.get("https://x/missing|2026-10-21")
    with pytest.raises(CacheMiss, match="replay: not recorded"):
        c.get_or_fetch("https://x/missing2|2026-10-21", lambda: pytest.fail("fetched"))
    c.put("https://x/never", {"v": 0})  # a replay never writes the global cache
    assert not (output_dir / ".cache" / "edgar").exists()
    rl = get_runlog(log_dir)
    assert rl.counters["replay_misses"] == 2
    assert rl.meta["replay_missed_keys"] == [
        "edgar: https://x/missing",
        "edgar: https://x/missing2",
    ]


def test_failed_fetches_are_recorded_and_replay_as_failures_not_misses(output_dir, tmp_path):
    run_dir, log_dir = tmp_path / "run", tmp_path / "log"
    cache.activate(run_dir, "record")
    c = DiskCache(output_dir, "usaspending", ttl_seconds=60)

    def boom():
        raise RuntimeError("HTTP 500")

    with pytest.raises(RuntimeError):
        c.get_or_fetch("GET /x|2026-10-21", boom)
    cache.activate(run_dir, "replay", log_dir=log_dir)
    with pytest.raises(CacheMiss, match="recorded failure .RuntimeError."):
        c.get_or_fetch("GET /x|2026-10-22", lambda: pytest.fail("fetched"))
    rl = get_runlog(log_dir)
    assert rl.counters.get("replay_misses", 0) == 0
    assert rl.counters["replay_recorded_errors"] == 1


def test_network_guard_blocks_even_mocked_requests_and_is_removed_on_deactivate(
    edgar_mock, tmp_path
):
    original_send = httpx.Client.send
    cache.activate(tmp_path / "run", "replay", log_dir=tmp_path / "log")
    url = "https://www.sec.gov/files/company_tickers_exchange.json"  # a route edgar_mock serves
    calls_before = edgar_mock.calls.call_count
    with pytest.raises(OfflineViolation, match="offline replay"):
        httpx.Client().get(url)
    assert edgar_mock.calls.call_count == calls_before  # it never reached the transport
    rl = get_runlog(tmp_path / "log")
    assert rl.counters["offline_violations"] == 1
    assert rl.meta["offline_violations"] == [f"GET {url}"]
    cache.deactivate()
    assert httpx.Client.send is original_send or httpx.Client.send.__name__ != "_blocked"
    assert httpx.Client().get(url).status_code == 200


# ---- record then replay a whole run ---------------------------------------------------------


def _run_original(sections, output_dir) -> Path:
    result = run_primer(_inputs(sections))
    return Path(result["run_dir"])


def test_record_then_replay_reproduces_the_primer(edgar_mock, output_dir, monkeypatch):
    orig = _run_original([1, 9], output_dir)
    assert (orig / "inputs.json").exists()
    assert (orig / "replay" / "edgar").is_dir()
    saved = json.loads((orig / "inputs.json").read_text(encoding="utf-8"))
    assert saved["inputs"]["as_of_date"] == "2026-10-21" and saved["inputs"]["sections"] == [1, 9]
    mdx = (orig / "primer.mdx").read_text(encoding="utf-8")
    assert "| Revenue | 65,984[^" in mdx
    network_calls = edgar_mock.calls.call_count
    assert network_calls > 0

    # another day, and the warm global cache is gone: only the recording can serve the replay
    _advance_clock(monkeypatch)
    _drop_global_cache(output_dir)
    result = replay_mod.replay_primer(orig)
    rep_dir = Path(result["replay_dir"])

    assert rep_dir.name == f"{orig.name}-replay" and rep_dir.parent == orig.parent
    assert edgar_mock.calls.call_count == network_calls  # not one request left the process
    assert result["replay_misses"] == 0 and result["offline_violations"] == 0
    assert not (output_dir / ".cache").exists()  # the replay never touched the global cache

    report = replay_mod.compare_runs(orig, rep_dir)
    assert report["primer_identical"], report["diff_head"]
    assert report["sources_identical"], report["sources_diff"]
    assert report["claims_identical"], report["claims_diff"]
    assert report["ok"]

    # run ids differ and nothing else does; the replay primer says which run it replays
    a = mdx
    b = (rep_dir / "primer.mdx").read_text(encoding="utf-8")
    assert a != b and re.sub(r"(run_id|trace_id): \S+\n", "", a) == re.sub(
        r"(run_id|trace_id): \S+\n", "", b
    )
    assert f"run_id: {orig.name}-replay" in b
    # same evidence ids, same files
    ids = lambda d: sorted(  # noqa: E731
        (r["source_id"], r["content_hash"])
        for r in json.loads((d / "evidence" / "index.json").read_text(encoding="utf-8"))
    )
    assert ids(orig) == ids(rep_dir)
    run = json.loads((rep_dir / "run.json").read_text(encoding="utf-8"))
    assert run["meta"]["replay_of"] == str(orig)
    assert run["counters"].get("edgar_requests", 0) == 0


def test_replay_of_a_wider_run_with_web_sources(
    edgar_mock, firecrawl_mock, output_dir, monkeypatch
):
    fc = _web_mock(firecrawl_mock)
    orig = _run_original([1, 2, 5, 9, 14], output_dir)
    assert len(fc.calls) > 0
    n_edgar, n_fc = edgar_mock.calls.call_count, len(fc.calls)
    saved = json.loads((orig / "inputs.json").read_text(encoding="utf-8"))
    assert saved["env"]["firecrawl_key"] is True
    urls = [
        r["url"] for r in json.loads((orig / "evidence" / "index.json").read_text(encoding="utf-8"))
    ]
    assert any("fool.com" in u for u in urls) and any("defensenews.com" in u for u in urls)

    _advance_clock(monkeypatch)
    _drop_global_cache(output_dir)
    result = replay_mod.replay_primer(orig)
    assert (edgar_mock.calls.call_count, len(fc.calls)) == (n_edgar, n_fc)
    report = replay_mod.compare_runs(orig, result["replay_dir"])
    assert report["replay_misses"] == 0, report["missed_keys"]
    assert report["offline_violations"] == 0, report["violation_urls"]
    assert report["primer_identical"], report["diff_head"]
    assert report["sources_identical"], report["sources_diff"]
    assert report["claims_identical"], report["claims_diff"]


def _delete_entry(orig: Path, namespace: str, needle: str) -> None:
    victim = next(
        p
        for p in (orig / "replay" / namespace).glob("*.json")
        if needle in json.loads(p.read_text(encoding="utf-8"))["key"]
    )
    victim.unlink()


def test_replay_in_a_fresh_process_with_another_hash_seed(
    edgar_mock, firecrawl_mock, output_dir, monkeypatch
):
    """The real command, a new interpreter, no respx (any request would raise) and a different
    PYTHONHASHSEED: nothing in the primer may depend on set ordering or in-process state."""
    import os
    import subprocess
    import sys

    _web_mock(firecrawl_mock)
    orig = _run_original([1, 2, 5, 9, 14], output_dir)
    for key in ("PRIMER_OUTPUT_DIR", "EDGAR_USER_AGENT", "FIRECRAWL_API_KEY"):
        assert os.environ.get(key)
    env = {**os.environ, "PYTHONHASHSEED": "4242", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "primer.cli", "replay", str(orig)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "primer.mdx: identical" in proc.stdout and "replay_misses=0" in proc.stdout
    assert (orig.parent / f"{orig.name}-replay" / "primer.mdx").exists()


def test_a_deleted_recording_entry_shows_up_as_a_miss(edgar_mock, output_dir):
    orig = _run_original([1, 9], output_dir)
    _delete_entry(orig, "edgar", "companyfacts")
    network_calls = edgar_mock.calls.call_count

    result = replay_mod.replay_primer(orig)
    assert edgar_mock.calls.call_count == network_calls  # a miss is an error, not a fetch
    assert result["replay_misses"] >= 1
    assert "replay: not recorded" in result["error"]  # the foundation pack needs companyfacts
    report = replay_mod.compare_runs(orig, result["replay_dir"])
    assert not report["ok"] and not report["primer_identical"]
    assert any("companyfacts" in k for k in report["missed_keys"])
    run = json.loads((Path(result["replay_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert run["counters"]["replay_misses"] >= 1
    assert run["meta"]["replay_missed_keys"] and len(run["meta"]["replay_missed_keys"]) <= 20


def test_a_missing_filing_document_is_a_miss_but_the_run_still_completes(edgar_mock, output_dir):
    orig = _run_original([1, 9], output_dir)
    _delete_entry(orig, "edgar", "/Archives/edgar/data/")  # one filing document
    result = replay_mod.replay_primer(orig)
    assert "error" not in result and Path(result["mdx_path"]).exists()
    report = replay_mod.compare_runs(orig, result["replay_dir"])
    assert report["replay_misses"] >= 1 and not report["ok"]


def test_replaying_twice_makes_a_second_folder_and_replaying_a_replay_uses_the_original(
    edgar_mock, output_dir
):
    orig = _run_original([9], output_dir)
    first = Path(replay_mod.replay_primer(orig)["replay_dir"])
    second = Path(
        replay_mod.replay_primer(first)["replay_dir"]
    )  # a replay dir resolves to its origin
    assert first.name == f"{orig.name}-replay" and second.name == f"{orig.name}-replay2"
    assert replay_mod.compare_runs(orig, second)["ok"]


def test_replay_of_an_unrecorded_run_is_refused(output_dir, tmp_path):
    with pytest.raises(FileNotFoundError, match="inputs.json"):
        replay_mod.replay_primer(tmp_path)


# ---- 3.1: the cache ----------------------------------------------------------------------


def test_second_run_of_the_same_ticker_makes_no_edgar_or_firecrawl_requests(
    edgar_mock, firecrawl_mock, output_dir
):
    fc = _web_mock(firecrawl_mock)
    first = run_primer(_inputs([1, 9]))
    n_edgar, n_fc = edgar_mock.calls.call_count, len(fc.calls)
    r1 = get_runlog(first["run_dir"])
    assert n_edgar > 0 and n_fc > 0
    assert r1.counters["edgar_requests"] > 0 and r1.counters["firecrawl_requests"] > 0
    assert r1.counters.get("firecrawl_credits", 0) > 0
    foundation_1 = r1.meta["foundation"]

    second = run_primer(_inputs([1, 9]))  # same ticker, same day: the global cache is warm
    assert second["run_dir"] != first["run_dir"]
    assert edgar_mock.calls.call_count == n_edgar  # zero SEC requests
    assert len(fc.calls) == n_fc  # zero Firecrawl requests
    r2 = get_runlog(second["run_dir"])
    assert r2.counters.get("edgar_requests", 0) == 0
    assert r2.counters.get("firecrawl_requests", 0) == 0
    assert r2.counters.get("firecrawl_credits", 0) == 0  # and zero credits
    assert r2.counters["firecrawl_cache_hits"] > 0
    # the foundation pack is the same pack, served from the cache
    assert r2.meta["foundation"] == foundation_1
    assert r2.meta["foundation"]["transcripts"]["found"] >= 1
    p1 = (Path(first["run_dir"]) / "primer.mdx").read_text(encoding="utf-8")
    p2 = (Path(second["run_dir"]) / "primer.mdx").read_text(encoding="utf-8")
    strip = lambda t: re.sub(r"(run_id|trace_id): \S+\n", "", t)  # noqa: E731
    assert strip(p1) == strip(p2)


def test_cache_entries_expire_but_replay_ignores_the_ttl(output_dir, tmp_path):
    c = DiskCache(output_dir, "firecrawl", ttl_seconds=-1)  # everything is already stale
    run_dir = tmp_path / "run"
    cache.activate(run_dir, "record")
    c.put("POST /search {}", {"success": True})
    with pytest.raises(CacheMiss):
        c.get("POST /search {}")  # stale for a live run
    cache.activate(run_dir, "replay", log_dir=tmp_path / "log")
    assert c.get("POST /search {}") == {"success": True}  # fine for a replay


def test_unmocked_request_in_replay_is_an_offline_violation_not_a_fetch(edgar_mock, tmp_path):
    cache.activate(tmp_path / "run", "replay", log_dir=tmp_path / "log")
    with pytest.raises(OfflineViolation):
        httpx.Client().post("https://api.firecrawl.dev/v2/search", json={})


# ---- model calls ------------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5}


class _Plan(BaseModel):
    answer: str
    n: int = 0


class _FakeLLM:
    calls = 0

    def invoke(self, messages):
        type(self).calls += 1
        return _Resp("hello " + messages[-1].content)

    def with_structured_output(self, schema, **kwargs):
        outer = self

        class _S:
            def invoke(self, messages):
                type(outer).calls += 1
                return {"raw": _Resp(""), "parsed": schema(answer=messages[-1].content, n=7)}

        return _S()


def test_model_calls_are_recorded_and_replayed_without_a_provider(
    output_dir, tmp_path, monkeypatch
):
    from primer import llm
    from primer.runlog import RunLog

    _FakeLLM.calls = 0
    monkeypatch.setattr(llm, "chat", lambda role: None if role == "critic" else _FakeLLM())
    run_dir, log_dir = tmp_path / "run", tmp_path / "log"
    cache.activate(run_dir, "record")
    rl = RunLog(tmp_path / "rl")
    assert llm.invoke_text("drafter", "sys", "u1", rl) == "hello u1"
    plan = llm.invoke_structured("planner", _Plan, "sys", "u2", rl)
    assert plan == _Plan(answer="u2", n=7)
    assert llm.invoke_text("critic", "sys", "u3", rl) is None  # template path: recorded too
    assert _FakeLLM.calls == 2 and rl.tokens["drafter"] == {"input": 10, "output": 5}
    assert len(list((run_dir / "replay" / "llm").glob("*.json"))) == 3
    # a different prompt, role or schema is a different key
    assert llm.llm_key("drafter", "sys", "u1") != llm.llm_key("planner", "sys", "u1")
    assert llm.llm_key("planner", "sys", "u2", "_Plan") != llm.llm_key("planner", "sys", "u2", "X")

    def no_provider(role):
        raise AssertionError("a replay must not build a model")

    monkeypatch.setattr(llm, "chat", no_provider)
    cache.activate(run_dir, "replay", log_dir=log_dir)
    rl2 = RunLog(tmp_path / "rl2")
    assert llm.invoke_text("drafter", "sys", "u1", rl2) == "hello u1"
    assert llm.invoke_structured("planner", _Plan, "sys", "u2", rl2) == plan
    assert llm.invoke_text("critic", "sys", "u3", rl2) is None  # a recorded None is not a miss
    assert not rl2.tokens and not any(k.startswith("llm_calls_") for k in rl2.counters)
    assert rl2.counters["replay_llm_hits"] == 3
    assert get_runlog(log_dir).counters.get("replay_llm_misses", 0) == 0

    assert llm.invoke_text("drafter", "sys", "never asked before", rl2) is None
    assert llm.invoke_structured("planner", _Plan, "sys", "u2 changed", rl2) is None
    log = get_runlog(log_dir)
    assert log.counters["replay_llm_misses"] == 2 and len(log.meta["replay_llm_missed"]) == 2
    assert _FakeLLM.calls == 2


# ---- the CLI --------------------------------------------------------------------------------


def test_cli_replay_exits_0_on_an_identical_replay_and_1_on_a_miss(edgar_mock, output_dir):
    from typer.testing import CliRunner

    from primer.cli import app

    orig = _run_original([1, 9], output_dir)
    res = CliRunner().invoke(app, ["replay", str(orig)])
    assert res.exit_code == 0, res.output
    assert "primer.mdx: identical" in res.output
    assert "replay_misses=0" in res.output and "sources.json: identical" in res.output

    _delete_entry(orig, "edgar", "/Archives/edgar/data/")
    res = CliRunner().invoke(app, ["replay", str(orig), "--no-diff"])
    assert res.exit_code == 1, res.output
    assert "replay_misses=" in res.output and "missed: edgar:" in res.output
    # with the diff: a miss fails the replay even when the primer text came out the same
    res = CliRunner().invoke(app, ["replay", str(orig)])
    assert res.exit_code == 1 and "primer.mdx:" in res.output and "missed: edgar:" in res.output


def test_replay_uses_the_runs_config_snapshot_not_todays_config(
    edgar_mock, output_dir, monkeypatch, tmp_path
):
    """Config shapes the prompts and the frontmatter, so it is an input to a run: editing
    config/*.yaml after a run must not break an exact replay (found on 2026-10-22 when a
    trade-press list edited mid-run made the replay's planner prompt differ)."""
    from primer import config as cfg

    orig = _run_original([1, 9], output_dir)
    snap = orig / "config"
    assert snap.is_dir() and (snap / "sources.yaml").exists() and (snap / "models.yaml").exists()
    saved = json.loads((orig / "inputs.json").read_text(encoding="utf-8"))
    assert "code" in saved and set(saved["code"]) == {"commit", "dirty"}

    # edit today's config: a different frontmatter source_limits line
    edited = tmp_path / "edited_config"
    edited.mkdir()
    for f in cfg.DEFAULT_CONFIG_DIR.glob("*.yaml"):
        (edited / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    text = (edited / "sources.yaml").read_text(encoding="utf-8")
    marker = "Market data scraped from public aggregator pages, not a licensed feed"
    assert marker in text
    (edited / "sources.yaml").write_text(
        text.replace(marker, "CHANGED AFTER THE RUN"), encoding="utf-8"
    )
    cfg.use_config_dir(edited)
    try:
        assert "CHANGED AFTER THE RUN" in "".join(cfg.sources_config()["source_limits"])
        _advance_clock(monkeypatch)
        result = replay_mod.replay_primer(orig)
        assert result["config_snapshot"] is True
        report = replay_mod.compare_runs(orig, Path(result["replay_dir"]))
        assert report["primer_identical"], report["diff_head"]
        assert "CHANGED AFTER THE RUN" not in (orig / "primer.mdx").read_text(encoding="utf-8")
        # the replay put the config in effect back to the one it found, not the repo default
        assert cfg.CONFIG_DIR == cfg.DEFAULT_CONFIG_DIR
    finally:
        cfg.use_config_dir(None)


def test_a_run_without_a_snapshot_is_replayed_with_a_warning_flag(
    edgar_mock, output_dir, monkeypatch
):
    import shutil

    orig = _run_original([1, 9], output_dir)
    shutil.rmtree(orig / "config")
    _advance_clock(monkeypatch)
    result = replay_mod.replay_primer(orig)
    assert result["config_snapshot"] is False and "code_changed" in result
