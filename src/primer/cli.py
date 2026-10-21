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
    golden: Path = typer.Option(..., "--golden", help="Golden financials JSON"),
    out: Path | None = typer.Option(
        None, "--out", help="Scorecard directory (default output/eval)"
    ),
) -> None:
    """Score a run's section 9 numbers against a golden table."""
    from primer.eval.harness import compare_financials, write_scorecard

    result = compare_financials(run_dir, golden)
    path = write_scorecard(result, out or (output_root() / "eval"))
    summary = {k: v for k, v in result.items() if k != "rows"}
    summary["scorecard"] = str(path)
    typer.echo(json.dumps(summary, indent=2))
    for r in result["rows"]:
        if r.get("status") != "match":
            typer.echo(
                f"  {r['metric']:24s} {r['period']:8s} {r['status']}: golden={r.get('golden')} run={r.get('run')}"
            )
    raise typer.Exit(code=0 if result["exact_match_rate"] in (1.0,) else 1)


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
