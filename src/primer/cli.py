"""CLI: primer run | resume | eval | md-check."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer

from primer import __version__
from primer.config import REPO_ROOT, output_root

app = typer.Typer(help=f"Company Primer Agent {__version__}", no_args_is_help=True)


def _parse_sections(s: str | None) -> list[int]:
    if not s:
        return list(range(1, 15))
    out: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return sorted(x for x in out if 1 <= x <= 14)


@app.command()
def run(
    company: str = typer.Option(..., "--company", "-c", help="Company name (free text)"),
    ticker: str | None = typer.Option(
        None, "--ticker", "-t", help="Ticker, exchange-qualified if ambiguous"
    ),
    as_of: str | None = typer.Option(None, "--as-of", help="YYYY-MM-DD; defaults to today"),
    sections: str | None = typer.Option(None, "--sections", "-s", help="e.g. '1,9' or '1-14'"),
    debug: bool = typer.Option(False, "--debug", help="Pause for review before export"),
    offline: bool = typer.Option(False, "--offline", help="Replay from cache/evidence only"),
    private: bool = typer.Option(
        False,
        "--private",
        help="Treat the company as private even if the SEC has a match: Form D, press and web "
        "research only, sections 9, 11 and 13 press-reported and unaudited",
    ),
    cik: str | None = typer.Option(
        None, "--cik", help="SEC CIK; answers the entity prompt for an ambiguous name"
    ),
    alias: list[str] = typer.Option(
        [],
        "--alias",
        help="Another name for the company, repeatable (for a private company pass the legal "
        "name as --company, which is how filings and USAspending know it, and the everyday "
        "name here, e.g. --company 'Space Exploration Technologies Corp' --alias SpaceX)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Approve plan and review prompts without asking (also the default when stdin is "
        "not a TTY); an ambiguous company name then fails, listing the candidates",
    ),
) -> None:
    """Generate a primer. Writes output/{ticker-or-slug}/{run_id}/primer.md and friends."""
    from primer.graph.interrupts import InterruptRequired, make_cli_prompter
    from primer.graph.main import run_primer
    from primer.state import Inputs

    inputs = Inputs(
        company_name=company,
        ticker=ticker,
        as_of_date=date.fromisoformat(as_of) if as_of else date.today(),
        sections=_parse_sections(sections),
        debug=debug,
        offline=offline,
        private=private,
        cik=cik,
        aliases=list(alias),
    )
    try:
        result = run_primer(inputs, on_interrupt=make_cli_prompter(yes=yes))
    except InterruptRequired as e:  # only for an interrupt the prompter does not answer
        typer.echo(str(e).encode("ascii", "replace").decode("ascii"), err=True)
        raise typer.Exit(code=2) from e
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def resume(run_dir: Path = typer.Argument(..., help="output/{slug}/{run_id}")) -> None:
    """Resume a killed run from its SQLite checkpoint: only the unfinished sections run again,
    and run.json keeps counting (meta.resumed, section_runs.N)."""
    from primer.graph.main import resume_primer

    typer.echo(json.dumps(resume_primer(run_dir), indent=2, default=str))


def _console_safe(text: str) -> str:
    """The Windows console may not encode the primer's punctuation; never let that crash."""
    import sys

    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(enc, "replace").decode(enc, "replace")


@app.command()
def replay(
    run_dir: Path = typer.Argument(..., help="output/{slug}/{run_id} of a recorded run"),
    diff: bool = typer.Option(
        True,
        "--diff/--no-diff",
        help="Compare the replayed primer, sources and claims with the original",
    ),
) -> None:
    """Replay a recorded run offline: every cache read, model call and fetch comes from
    {run_dir}/replay/ (a network call raises), evidence ids are kept, and the result is
    written to {run_dir}-replay. Exit 0 only when the primer is identical (with --diff) and
    nothing was missing from the recording."""
    from primer.replay import compare_runs, replay_primer

    result = replay_primer(run_dir)
    typer.echo(f"replayed {result['original']} -> {result['replay_dir']}")
    if not result.get("config_snapshot"):
        typer.echo(
            "note: this run has no config snapshot, so the replay used today's config/*.yaml; "
            "a config change since the run makes the model prompts differ"
        )
    if result.get("code_changed"):
        typer.echo(
            "note: the code differs from the version that made the recording "
            f"(recorded {str((result.get('code_recorded') or {}).get('commit'))[:10]}"
            f"{' + uncommitted edits' if (result.get('code_recorded') or {}).get('dirty') else ''}, "
            f"now {str((result.get('code_now') or {}).get('commit'))[:10]}"
            f"{' + uncommitted edits' if (result.get('code_now') or {}).get('dirty') else ''}); "
            "misses can come from the code, not from nondeterminism"
        )
    if result.get("error"):
        typer.echo(_console_safe(f"replay failed: {result['error'][:300]}"))
    if diff:
        rep = compare_runs(result["original"], result["replay_dir"])
    else:
        rep = {
            "primer_identical": None,
            "replay_misses": result["replay_misses"],
            "replay_llm_misses": result["replay_llm_misses"],
            "offline_violations": result["offline_violations"],
            "missed_keys": result["missed_keys"],
            "missed_llm": result["missed_llm"],
            "violation_urls": result["violation_urls"],
            "ok": not result.get("error")
            and not (
                result["replay_misses"]
                or result["replay_llm_misses"]
                or result["offline_violations"]
            ),
        }
    if diff:
        if rep["primer_identical"]:
            typer.echo("primer.md: identical")
        else:
            typer.echo(f"primer.md: {rep['diff_lines']} differing lines; first 20 diff lines:")
            for line in rep["diff_head"]:
                typer.echo(_console_safe(line))
        typer.echo(f"sources.json: {'identical' if rep['sources_identical'] else 'DIFFERS'}")
        for line in rep["sources_diff"][:5]:
            typer.echo(_console_safe("  " + line))
        typer.echo(f"claims.json: {'identical' if rep['claims_identical'] else 'DIFFERS'}")
        for line in rep["claims_diff"][:5]:
            typer.echo(_console_safe("  " + line[:200]))
    typer.echo(
        f"replay_misses={rep['replay_misses']} replay_llm_misses={rep['replay_llm_misses']} "
        f"offline_violations={rep['offline_violations']}"
    )
    for key in rep.get("missed_keys", []):
        typer.echo(_console_safe(f"  missed: {key}"))
    for key in rep.get("missed_llm", []):
        typer.echo(_console_safe(f"  missed model call: {key}"))
    for key in rep.get("violation_urls", []):
        typer.echo(_console_safe(f"  network attempt: {key}"))
    raise typer.Exit(code=0 if rep["ok"] else 1)


@app.command()
def eval(
    run_dir: Path = typer.Option(..., "--run-dir"),
    golden: Path | None = typer.Option(
        None, "--golden", help="Golden primer (markdown/text) for the judge rubric and fact overlap"
    ),
    golden_financials: Path | None = typer.Option(
        None, "--golden-financials", help="Golden financials JSON (section 9, to the dollar)"
    ),
    ticker: str | None = typer.Option(
        None,
        "--ticker",
        help="Pick docs/golden/{ticker}.md and {ticker}_financials.json by default",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Scorecard directory (default output/eval)"
    ),
) -> None:
    """Score a run: judge rubric per section (>= 3/5 gate), fact overlap with the golden
    primer, citation resolvability, section 9 to-the-dollar check. Writes a scorecard."""
    import os

    from primer.eval.harness import scorecard, write_scorecard

    gdir = Path(os.environ.get("GOLDEN_DIR", REPO_ROOT / "docs" / "golden"))
    if ticker:
        tk = ticker.lower()
        golden = golden or (gdir / f"{tk}.md" if (gdir / f"{tk}.md").exists() else None)
        golden_financials = golden_financials or (
            gdir / f"{tk}_financials.json" if (gdir / f"{tk}_financials.json").exists() else None
        )
    result = scorecard(run_dir, golden, golden_financials)
    path = write_scorecard(result, out or (output_root() / "eval"))
    summary = {
        "judge_mode": result["judge_mode"],
        "sections_scored": result["sections_scored"],
        "min_section_mean": result["min_section_mean"],
        "mean_section_mean": result["mean_section_mean"],
        "all_sections_at_least_3": result["all_sections_at_least_3"],
        "citations": {
            k: v
            for k, v in result["citations"].items()
            if k in ("ok", "rate", "unresolved", "sources", "tier1_share", "undated_share")
        },
        "self_consistency": {
            k: (result.get("self_consistency") or {}).get(k) for k in ("ok", "failed")
        },
        "fact_overlap_mean": (result.get("fact_overlap") or {}).get("mean_rate"),
        "financials_exact_match_rate": (result.get("financials") or {}).get("exact_match_rate"),
        "run": result.get("run"),
        "scorecard": str(path),
    }
    typer.echo(json.dumps(summary, indent=2, default=str))
    for sec, sc in sorted(result["section_scores"].items(), key=lambda kv: int(kv[0])):
        # the Windows console may not encode the judge's punctuation; never let that crash
        reason = sc.get("reason", "")[:100].encode("ascii", "replace").decode("ascii")
        typer.echo(f"  section {sec:>2}: mean {sc.get('mean')}  {reason}")
    ok = result["all_sections_at_least_3"] and (result["citations"].get("ok") is not False)
    if result.get("financials") and result["financials"].get("exact_match_rate") not in (None, 1.0):
        ok = False
    if golden is None and (result.get("self_consistency") or {}).get("ok") is False:
        ok = False  # no reference to compare with: the run's own numbers must hold together
    raise typer.Exit(code=0 if ok else 1)


@app.command("eval-set")
def eval_set(
    run: bool = typer.Option(
        False,
        "--run",
        help="Execute run_primer for each entry first (sequential; costs tokens and credits)",
    ),
    only: list[str] | None = typer.Option(
        None, "--only", help="Ticker or name slug; repeat the option or comma-separate"
    ),
    config: Path | None = typer.Option(
        None, "--config", help="Eval set (default config/eval.yaml)"
    ),
    out: Path | None = typer.Option(None, "--out", help="Report directory (default output/eval)"),
) -> None:
    """Score the eval set (config/eval.yaml): one row per company with the gate, coverage,
    critic and judge scores, fact overlap, financial match, source quality, cost and the
    entry's expectations. Without --run it evaluates the newest existing run of each entry.
    Writes output/eval/eval-set-{date}.json and .md; exits 1 when any entry fails."""
    from primer.eval.eval_set import (
        evaluate_set,
        filter_entries,
        load_eval_set,
        write_report,
    )

    es = load_eval_set(config)
    try:
        entries = filter_entries(es.entries, only)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from None
    out_dir = out or (output_root() / "eval")
    report = evaluate_set(
        entries, es.defaults, run=run, out_dir=out_dir, log=lambda s: typer.echo(s, err=True)
    )
    jp, mp = write_report(report, out_dir)
    for r in report["entries"]:
        if r.get("skipped"):
            typer.echo(f"  {r['id']:<8} {'FAIL' if not r['ok'] else 'skipped'}  {r['reason']}")
            continue
        j = r["judge"]
        typer.echo(
            f"  {r['id']:<8} {'ok  ' if r['ok'] else 'FAIL'} gate={r['gate']['passed']} "
            f"judge={j['min']}/{j['mean']} ({j['mode']}) cov={r['coverage']}"
        )
        for f in r["failures"]:
            typer.echo("           - " + f.encode("ascii", "replace").decode("ascii"))
    typer.echo(f"report: {jp}\n        {mp}")
    raise typer.Exit(code=0 if report["ok"] else 1)


@app.command("md-check")
def md_check(path: Path = typer.Argument(...)) -> None:
    """Check a primer.md: frontmatter keys and citation integrity."""
    from primer.validate.citations import check_citations
    from primer.validate.frontmatter import check_frontmatter

    text = path.read_text(encoding="utf-8")
    out = {"frontmatter": check_frontmatter(text), "citations": check_citations(text)}
    typer.echo(json.dumps(out, indent=2))
    raise typer.Exit(code=0 if out["frontmatter"]["ok"] and out["citations"]["ok"] else 1)


@app.command()
def info() -> None:
    """Show config and which optional services are configured."""
    import os

    from primer.config import llm_available

    typer.echo(
        json.dumps(
            {
                "version": __version__,
                "repo": str(REPO_ROOT),
                "output": str(output_root()),
                "llm": llm_available(),
                "firecrawl": bool(os.environ.get("FIRECRAWL_API_KEY")),
                "samgov": bool(os.environ.get("SAM_GOV_API_KEY")),
                "edgar_user_agent": bool(os.environ.get("EDGAR_USER_AGENT")),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
