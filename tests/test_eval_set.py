"""PLAN 3.5: the eval set (rows from a fixture run, expectations, --only, self-consistency)."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from primer.cli import app
from primer.config import output_root
from primer.eval import eval_set as es
from primer.eval.harness import scorecard, self_consistency
from primer.graph.main import new_run, run_primer
from primer.sections.text_tools import gov_revenue_disclosure
from primer.state import Inputs
from tests.test_sec04_programs import _inputs as sec4_inputs
from tests.test_sec04_programs import _invoke as sec4_invoke
from tests.test_sec04_programs import _state as sec4_state

REPO = Path(__file__).resolve().parents[1]
GOLDEN_FIN = REPO / "docs" / "golden" / "lmt_financials.json"
AS_OF = date(2026, 10, 21)


def _lmt_run() -> Path:
    """A minimal real run: Lockheed Martin fixtures, sections 1 and 9, template mode."""
    r = run_primer(
        Inputs(company_name="Lockheed Martin", ticker="LMT", as_of_date=AS_OF, sections=[1, 9])
    )
    return Path(r["run_dir"])


def _entry(**kw) -> es.EvalEntry:
    base = {"name": "Lockheed Martin", "ticker": "LMT", "kind": "rubric"}
    return es.EvalEntry.model_validate({**base, **kw})


# ---- config ---------------------------------------------------------------------------------


def test_eval_yaml_has_the_five_defense_entries():
    ev = es.load_eval_set()
    by = {e.id: e for e in ev.entries}
    assert list(by) == ["LMT", "BA", "SPCX", "NOC", "LHX"]
    assert [by[i].kind for i in by] == ["golden", "golden", "golden", "rubric", "rubric"]
    assert by["SPCX"].flags == {} and by["SPCX"].ticker == "SPCX"
    assert by["SPCX"].aliases == ["SpaceX"] and by["SPCX"].matches("spacex")
    assert by["SPCX"].golden == "docs/golden/spacex.md"
    assert by["NOC"].expect["fiscal_year_end"] == "1231" and by["NOC"].sector == "aerospace_defense"
    assert by["NOC"].expect["section4"] == ["full", "partial"]
    assert by["LHX"].expect["section9"] == "full" and "52/53-week" in by["LHX"].note
    assert by["LMT"].golden_financials == "docs/golden/lmt_financials.json"
    # every sector an entry expects is a tag sources.yaml knows
    from primer.config import sources_config

    tags = set(sources_config()["sectors"])
    assert {e.expect["sector_tag"] for e in ev.entries if "sector_tag" in e.expect} <= tags


def test_entry_validation_rejects_typos_and_missing_golden():
    with pytest.raises(ValueError):
        _entry(flagz={"private": True})
    with pytest.raises(ValueError):
        _entry(flags={"privat": True})
    with pytest.raises(ValueError):
        _entry(kind="golden")  # no golden path
    with pytest.raises(ValueError):
        _entry(as_of="21/10/2026")


def test_entry_slug_matches_the_output_folder_new_run_creates(output_dir):
    for e in es.load_eval_set().entries:
        inputs = Inputs(company_name=e.name, ticker=e.ticker, sections=[9])
        _, run_dir = new_run(inputs)
        assert run_dir.parent == output_root() / e.slug


def test_load_rejects_duplicate_entries(tmp_path):
    p = tmp_path / "eval.yaml"
    p.write_text("entries:\n  - {name: A, ticker: X}\n  - {name: B, ticker: x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        es.load_eval_set(p)


# ---- --only ---------------------------------------------------------------------------------


def test_only_filters_by_ticker_slug_and_comma_list():
    entries = es.load_eval_set().entries
    assert [e.id for e in es.filter_entries(entries, None)] == [e.id for e in entries]
    assert [e.id for e in es.filter_entries(entries, ["lmt"])] == ["LMT"]
    assert [e.id for e in es.filter_entries(entries, ["BA,NOC"])] == ["BA", "NOC"]
    assert [e.id for e in es.filter_entries(entries, ["spacex", "LHX"])] == [
        "SPCX",
        "LHX",
    ]
    assert [e.id for e in es.filter_entries(entries, ["Northrop Grumman"])] == ["NOC"]
    with pytest.raises(ValueError, match="not in the eval set"):
        es.filter_entries(entries, ["NOPE"])


# ---- expectations ---------------------------------------------------------------------------


def test_expectation_checks_pass_fail_and_unknown():
    facts = {
        "coverage": {"4": "not_applicable", "9": "partial"},
        "fiscal_year_end": "0926",
        "sector_tag": "technology",
        "is_public": True,
        "gate": "pass",
        "self_consistency": "fail",
        "judge_mean": 3.4,
        "undated_share": 0.2,
        "tier1_share": 0.5,
    }
    ok = es.check_expectations(
        {
            "section4": "not_applicable",
            "section9": ["full", "partial"],
            "fiscal_year_end": "0926",
            "sector_tag": "technology",
            "is_public": True,
            "gate": "pass",
            "min_judge_mean": 3.0,
            "max_undated_share": 0.25,
            "min_tier1_share": 0.5,
        },
        facts,
    )
    assert all(r["ok"] for r in ok), ok

    bad = es.check_expectations(
        {
            "section4": "full",
            "section9": "full",
            "section11": "full",  # not in the run at all
            "fiscal_year_end": "1231",
            "sector_tag": "pharma_biotech",
            "is_public": False,
            "self_consistency": "pass",
            "min_judge_mean": 4.0,
            "max_undated_share": 0.1,
            "financials_exact": 1.0,  # no financials in the facts
            "section_4_typo": "full",
            "colour": "blue",
        },
        facts,
    )
    assert [r["key"] for r in bad if r["ok"]] == []
    unknown = {r["key"]: r["actual"] for r in bad if r["actual"] == "unknown expectation"}
    assert set(unknown) == {"section_4_typo", "colour"}


def test_fiscal_year_end_is_compared_as_mmdd_even_if_yaml_dropped_the_zero():
    r = es.check_expectations({"fiscal_year_end": 926}, {"fiscal_year_end": "0926"})
    assert r[0]["ok"]


# ---- rows from a real (fixture) run -----------------------------------------------------------


def test_row_from_a_fixture_run_with_golden_financials(edgar_mock, output_dir, tmp_path):
    run_dir = _lmt_run()
    entry = _entry(
        kind="golden",
        golden=str(tmp_path / "lmt.md"),
        golden_financials=str(GOLDEN_FIN),
        sector="aerospace_defense",
        expect={
            "section9": "full",
            "fiscal_year_end": "1231",
            "sector_tag": "aerospace_defense",
            "is_public": True,
            "gate": "pass",
        },
    )
    (tmp_path / "lmt.md").write_text(
        "# Lockheed Martin Company Primer\n\n## Historical Financial Performance\n"
        "Revenue grew from $65,984 million in 2022 to $75,048 million in 2025; "
        "net income was $5,017 million in 2025. " + "Lorem ipsum dolor sit amet. " * 12,
        encoding="utf-8",
    )
    row = es.build_row(entry, run_dir, {"judge_min": 3.0}, out_dir=tmp_path / "scorecards")

    assert row["ok"], row["failures"]
    assert row["id"] == "LMT" and row["kind"] == "golden" and not row["skipped"]
    assert row["gate"] == {"passed": True, "failed_checks": []}
    assert row["status"] == "ok"
    assert row["coverage"] == {"full": 2, "partial": 0, "unavailable": 0, "not_applicable": 0}
    assert row["critic"]["min"] >= 3 and row["critic"]["mean"] >= row["critic"]["min"]
    assert row["judge"]["mode"] == "template" and row["judge"]["min"] >= 3
    assert row["financials_match"] == 1.0
    assert row["fact_overlap"] is not None  # golden entry
    assert row["sources"]["tier1_share"] == 1.0 and row["sources"]["undated_share"] == 0.0
    assert row["self_consistency"] == {"ok": True, "failed": []}
    assert row["tokens"] == 0 and row["wall_clock_seconds"] > 0
    assert all(x["ok"] for x in row["expect"]) and len(row["expect"]) == 5
    json.dumps(row)
    assert (tmp_path / "scorecards" / f"scorecard-{run_dir.name}.json").exists()


def test_rubric_row_has_no_reference_scores_and_a_self_consistency_block(edgar_mock, output_dir):
    run_dir = _lmt_run()
    row = es.build_row(_entry(expect={"self_consistency": "pass"}), run_dir)
    assert row["kind"] == "rubric" and row["ok"], row["failures"]
    assert row["fact_overlap"] is None and row["financials_match"] is None
    assert row["self_consistency"]["ok"] is True
    sc = scorecard(run_dir)  # no golden primer needed
    assert "fact_overlap" not in sc and "financials" not in sc
    assert sc["self_consistency"]["ran"] == 4 and sc["judge_mode"] == "template"
    assert sc["citations"]["undated_share"] == 0.0


def test_missing_golden_file_degrades_to_a_rubric_entry_with_a_note(edgar_mock, output_dir):
    run_dir = _lmt_run()
    entry = _entry(kind="golden", golden="docs/golden/does_not_exist.md")
    row = es.build_row(entry, run_dir)
    assert row["kind"] == "rubric" and row["ok"]
    assert any("does_not_exist.md" in n for n in row["notes"])


def test_failed_expectation_and_failed_gate_make_the_row_fail(edgar_mock, output_dir):
    run_dir = _lmt_run()
    entry = _entry(expect={"section4": "not_applicable", "fiscal_year_end": "0926"})
    row = es.build_row(entry, run_dir)
    assert not row["ok"]
    assert any("section4" in f and "None" in f for f in row["failures"])
    assert any("fiscal_year_end" in f and "'1231'" in f for f in row["failures"])

    # break the recorded gate
    rp = run_dir / "run.json"
    run = json.loads(rp.read_text(encoding="utf-8"))
    run["validation"]["passed"] = False
    run["validation"]["checks"]["citations"]["ok"] = False
    rp.write_text(json.dumps(run), encoding="utf-8")
    row = es.build_row(_entry(), run_dir)
    assert not row["ok"] and row["gate"]["failed_checks"] == ["citations"]
    assert any("validation gate failed: citations" in f for f in row["failures"])


def test_unfinished_run_has_no_gate_and_fails(tmp_path):
    d = tmp_path / "output" / "LMT" / "2026-10-21-deadbeef"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"meta": {}, "elapsed_seconds": 1}), encoding="utf-8")
    row = es.build_row(_entry(), d)
    assert not row["ok"] and row["gate"]["passed"] is None
    assert any("no validation recorded" in f for f in row["failures"])


def test_rubric_entry_fails_when_its_numbers_do_not_add_up(edgar_mock, output_dir):
    run_dir = _lmt_run()
    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    for c in claims:
        if c["metric"] == "total_assets" and c["period"] == "FY2024":
            c["value"] *= 1.05
    (run_dir / "claims.json").write_text(json.dumps(claims), encoding="utf-8")
    row = es.build_row(_entry(), run_dir, {"require_self_consistency_for": ["rubric"]})
    assert not row["ok"]
    assert row["self_consistency"]["failed"] == ["balance_sheet_identity"]
    # a golden entry is compared with its reference instead
    golden_row = es.build_row(
        _entry(kind="golden", golden=str(GOLDEN_FIN)),
        run_dir,
        {"require_self_consistency_for": ["rubric"]},
    )
    assert golden_row["self_consistency"]["ok"] is False
    assert not any("self-consistency" in f for f in golden_row["failures"])


# ---- self-consistency block -------------------------------------------------------------------


def _claims(tmp_path: Path, rows: list[tuple[str, str, float]]) -> Path:
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    claims = [
        {"section": 9, "metric": m, "period": p, "value": v, "unit": "USD", "source_ids": ["src_1"]}
        for m, p, v in rows
    ]
    claims.append(
        {"section": 4, "metric": "total_assets", "period": "FY2020", "value": -1, "source_ids": []}
    )  # other sections are ignored
    (d / "claims.json").write_text(json.dumps(claims), encoding="utf-8")
    return d


GOOD = [
    ("total_assets", "FY2024", 1000.0),
    ("total_liabilities", "FY2024", 700.0),
    ("stockholders_equity", "FY2024", 298.0),  # 0.2% off: noncontrolling interest
    ("total_assets", "FY2025", 1100.0),
    ("total_liabilities", "FY2025", 800.0),
    ("stockholders_equity", "FY2025", 300.0),
    ("operating_cash_flow", "FY2025", 200.0),
    ("capex", "FY2025", 50.0),
    ("free_cash_flow", "FY2025", 150.0),
    ("revenue", "FY2024", 500.0),
    ("revenue", "FY2025", 520.0),
]


def test_self_consistency_passes_clean_numbers(tmp_path):
    r = self_consistency(_claims(tmp_path, GOOD))
    assert r["ok"] and r["failed"] == [] and r["ran"] == 4


def test_self_consistency_catches_a_planted_balance_sheet_violation(tmp_path):
    rows = [(m, p, v * 1.02 if (m, p) == ("total_assets", "FY2025") else v) for m, p, v in GOOD]
    r = self_consistency(_claims(tmp_path, rows))
    assert not r["ok"] and r["failed"] == ["balance_sheet_identity"]
    bad = next(c for c in r["checks"] if c["name"] == "balance_sheet_identity")["rows"]
    assert [b["period"] for b in bad] == ["FY2025"] and bad[0]["gap_pct"] > 0.5
    # exactly at the line: 0.4% is fine
    rows = [(m, p, v * 1.004 if (m, p) == ("total_assets", "FY2025") else v) for m, p, v in GOOD]
    assert self_consistency(_claims(tmp_path, rows))["ok"]


def test_self_consistency_catches_fcf_revenue_and_label_violations(tmp_path):
    rows = [(m, p, 149.0 if m == "free_cash_flow" else v) for m, p, v in GOOD]
    assert self_consistency(_claims(tmp_path, rows))["failed"] == ["fcf_identity"]

    rows = [(m, p, -5.0 if (m, p) == ("revenue", "FY2024") else v) for m, p, v in GOOD]
    assert self_consistency(_claims(tmp_path, rows))["failed"] == ["revenue_positive"]

    rows = [(m, "FY2027" if p == "FY2025" else p, v) for m, p, v in GOOD]  # FY2025 -> FY2027
    r = self_consistency(_claims(tmp_path, rows))
    assert r["failed"] == ["fiscal_year_labels_consecutive"]
    assert r["checks"][3]["rows"] == [{"after": "FY2024", "next": "FY2027"}]


def test_self_consistency_skips_what_it_cannot_check(tmp_path):
    r = self_consistency(_claims(tmp_path, [("revenue", "FY2025", 10.0)]))
    assert r["ok"] and r["ran"] == 1
    assert [c["status"] for c in r["checks"]] == ["skipped", "skipped", "pass", "skipped"]
    assert self_consistency(tmp_path / "empty")["ran"] == 0  # no claims.json at all


# ---- the set ---------------------------------------------------------------------------------


def _fake_run(entry: es.EvalEntry, root: Path, mtime: float | None = None) -> Path:
    d = root / entry.slug / f"2026-10-21-{abs(hash((entry.id, mtime))) % 10**8:08x}"
    d.mkdir(parents=True)
    p = d / "run.json"
    p.write_text(json.dumps({"meta": {"run_id": d.name}}), encoding="utf-8")
    if mtime:
        os.utime(p, (mtime, mtime))
    return d


def test_latest_run_dir_is_the_newest_by_mtime(output_dir):
    e = _entry()
    assert es.latest_run_dir(e) is None
    old = _fake_run(e, output_root(), time.time() - 1000)
    new = _fake_run(e, output_root(), time.time() - 10)
    (output_root() / "LMT" / "scratch").mkdir()  # a folder with no run.json is ignored
    assert es.latest_run_dir(e) == new != old


def test_without_run_entries_with_no_run_are_skipped_and_say_so(output_dir):
    a, b = _entry(), _entry(name="Boeing", ticker="BA")
    _fake_run(a, output_root())
    msgs: list[str] = []
    rep = es.evaluate_set(
        [a, b],
        scorecard_fn=lambda *a, **k: {},
        log=msgs.append,
    )
    rows = {r["id"]: r for r in rep["entries"]}
    assert rows["BA"]["skipped"] and "no run under" in rows["BA"]["reason"]
    assert any("BA" in m and "skipped" in m for m in msgs)
    assert not rows["LMT"]["skipped"]
    assert rep["skipped"] == ["BA"] and rep["evaluated"] == 1
    # LMT's fake run has no validation, so the set fails; the skip itself does not
    assert rep["failed"] == ["LMT"] and rep["ok"] is False


def test_nothing_evaluated_is_not_a_pass(output_dir):
    rep = es.evaluate_set([_entry()], scorecard_fn=lambda *a, **k: {})
    assert rep["evaluated"] == 0 and rep["ok"] is False and rep["failed"] == []


def test_run_flag_executes_each_entry_in_order_then_evaluates(output_dir):
    entries = [
        _entry(),
        _entry(name="Boeing", ticker="BA"),
        _entry(name="Northrop Grumman", ticker="NOC"),
    ]
    called: list[str] = []

    def runner(e: es.EvalEntry) -> Path:
        called.append(e.id)
        if e.id == "BA":
            raise RuntimeError("boom")
        return _fake_run(e, output_root())

    rep = es.evaluate_set(entries, run=True, runner=runner, scorecard_fn=lambda *a, **k: {})
    assert called == ["LMT", "BA", "NOC"] and rep["mode"] == "run"
    ba = next(r for r in rep["entries"] if r["id"] == "BA")
    assert ba["skipped"] and not ba["ok"] and "run raised RuntimeError: boom" in ba["failures"][0]
    assert {r["id"] for r in rep["entries"] if r["ok"]} == set()  # fake runs have no gate
    assert "BA" in rep["failed"]


def test_default_runner_honours_flags_as_of_and_sections(monkeypatch, output_dir):
    seen: list[Inputs] = []

    def fake_run_primer(inputs, run_id=None, run_dir=None):
        seen.append(inputs)
        return {"run_dir": str(output_root() / "x")}

    monkeypatch.setattr("primer.graph.main.run_primer", fake_run_primer)
    e = _entry(
        name="SpaceX", ticker=None, flags={"private": True}, as_of="2026-10-21", sections="1,9"
    )
    es.default_runner(e)
    i = seen[0]
    assert i.company_name == "SpaceX" and i.ticker is None and i.private is True
    assert i.as_of_date == AS_OF and i.sections == [1, 9]


def test_report_files_and_markdown_table(edgar_mock, output_dir, tmp_path):
    run_dir = _lmt_run()
    lmt = _entry(sector="aerospace_defense", expect={"section9": "full"})
    ba = _entry(name="Boeing", ticker="BA")
    rep = es.evaluate_set([lmt, ba])
    jp, mp = es.write_report(rep, tmp_path / "eval")
    assert jp.name == f"eval-set-{date.today().isoformat()}.json"
    assert mp.name == f"eval-set-{date.today().isoformat()}.md"
    assert json.loads(jp.read_text(encoding="utf-8"))["entries"][0]["run_dir"] == str(run_dir)
    md = mp.read_text(encoding="utf-8")
    assert "| Entry | Kind | Gate | Coverage F/P/U/NA |" in md
    assert "| LMT (aerospace_defense) | rubric | pass | 2/0/0/0 |" in md
    assert "skipped: no run under" in md and "| BA |" in md
    assert rep["ok"] is True and rep["skipped"] == ["BA"]


# ---- the command -----------------------------------------------------------------------------


def _config(tmp_path: Path, expect: dict | None = None) -> Path:
    p = tmp_path / "eval.yaml"
    entries = [
        {"name": "Lockheed Martin", "ticker": "LMT", "kind": "rubric", "expect": expect or {}},
        {"name": "Boeing", "ticker": "BA", "kind": "rubric"},
    ]
    import yaml

    p.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")
    return p


def test_eval_set_command_writes_reports_and_exits_zero(edgar_mock, output_dir, tmp_path):
    _lmt_run()
    res = CliRunner().invoke(
        app, ["eval-set", "--config", str(_config(tmp_path, {"section9": "full"})), "--only", "LMT"]
    )
    assert res.exit_code == 0, res.output
    day = date.today().isoformat()
    assert (output_root() / "eval" / f"eval-set-{day}.json").exists()
    assert (output_root() / "eval" / f"eval-set-{day}.md").exists()
    rep = json.loads((output_root() / "eval" / f"eval-set-{day}.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in rep["entries"]] == ["LMT"]  # --only dropped BA


def test_eval_set_command_exits_nonzero_on_failed_expectation_or_unknown_only(
    edgar_mock, output_dir, tmp_path
):
    _lmt_run()
    cfg = _config(tmp_path, {"section4": "not_applicable"})
    res = CliRunner().invoke(app, ["eval-set", "--config", str(cfg), "--only", "LMT"])
    assert res.exit_code == 1 and "section4" in res.output
    res = CliRunner().invoke(app, ["eval-set", "--config", str(cfg), "--only", "NOPE"])
    assert res.exit_code == 2


def test_eval_set_command_skips_entries_without_a_run(edgar_mock, output_dir, tmp_path):
    _lmt_run()
    res = CliRunner().invoke(app, ["eval-set", "--config", str(_config(tmp_path))])
    assert res.exit_code == 0, res.output
    assert "BA" in res.output and "skipped" in res.output


# ---- sector gate (item 3.5: section 4 for a company with no government revenue) ----------------


def test_section4_for_a_company_with_no_government_revenue_is_one_not_applicable_line(
    edgar_mock, usaspending_mock, output_dir
):
    st = sec4_state(sec4_inputs())
    st["foundation"]["gov_revenue"] = {"disclosed": None}
    d = sec4_invoke(st)
    md = d["markdown"].strip()
    assert d["coverage"] == "not_applicable"
    assert "\n" not in md and md.startswith("> Coverage: not_applicable")
    assert not d["claims"] and len(usaspending_mock.calls) == 0


def test_government_revenue_gate_reads_the_10k_not_the_sector():
    apple_like = (
        "The Company's net sales are derived from iPhone, Mac, iPad, Services and Wearables. "
        "No single customer accounted for 10% or more of total net sales."
    )
    pharma_like = (
        "Revenues from sales to the U.S. government, principally of COVID-19 products, "
        "were 3% of total revenues in 2025."
    )
    assert gov_revenue_disclosure(apple_like)[0] is None
    assert gov_revenue_disclosure(pharma_like)[0] is True
