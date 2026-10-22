"""CLI: primer run | resume | eval | mdx-check."""

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
) -> None:
    """Generate a primer. Writes output/{ticker-or-slug}/{run_id}/primer.mdx and friends."""
    from primer.graph.main import run_primer
    from primer.state import Inputs

    inputs = Inputs(
        company_name=company,
        ticker=ticker,
        as_of_date=date.fromisoformat(as_of) if as_of else date.today(),
        sections=_parse_sections(sections),
        debug=debug,
        offline=offline,
    )
    result = run_primer(inputs)
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def resume(run_dir: Path = typer.Argument(..., help="output/{slug}/{run_id}")) -> None:
    """Resume a killed run from its SQLite checkpoint."""
    from primer.graph.main import resume_primer

    typer.echo(json.dumps(resume_primer(run_dir), indent=2, default=str))


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
            if k in ("ok", "rate", "unresolved", "sources", "tier1_share")
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
    raise typer.Exit(code=0 if ok else 1)


@app.command("mdx-check")
def mdx_check(path: Path = typer.Argument(...)) -> None:
    """Compile an .mdx with the Node sidecar."""
    from primer.validate.mdx_compile import check_frontmatter, check_mdx_compiles

    text = path.read_text(encoding="utf-8")
    out = {"frontmatter": check_frontmatter(text), "compile": check_mdx_compiles(path)}
    typer.echo(json.dumps(out, indent=2))
    ok = out["frontmatter"]["ok"] and out["compile"]["ok"] is not False
    raise typer.Exit(code=0 if ok else 1)


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
