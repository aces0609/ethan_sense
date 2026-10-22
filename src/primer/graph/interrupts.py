"""Interrupt handling for the main graph (PLAN 3.8; requirements §5 nodes 2, 4 and 10).

Three nodes pause the graph with `langgraph.types.interrupt`:

- `resolve_entity` -> {"type": "choose_entity", "candidates": [{cik, ticker, title, exchange,
  score}, ...]}. The answer is a CIK or a ticker (or "private" for the private path).
- `plan_sections` (debug) -> {"message": ..., "plan": {...}}. The answer is "approve" or a
  rejection string.
- `review` (debug) -> {"message": ..., "mdx_path": ..., "validation": {...}}. Same answers.

`drive` runs a compiled graph to completion, answering each interrupt through an
`on_interrupt(payload) -> answer` callback. Without a callback it raises `InterruptRequired`,
which carries the payload and the run directory: the checkpoint in that directory holds the
paused run, and `answer_interrupt(run_dir, answer)` resumes it later.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command

ENTITY_CHOICE = "choose_entity"
PRIVATE_ANSWER = "private"
MAX_ROUNDS = 25

OnInterrupt = Callable[[Any], Any]


class InterruptRequired(RuntimeError):
    """The graph paused and no `on_interrupt` callback was given.

    `payload` is the first pending interrupt's value, `payloads` all of them, `run_dir` and
    `run_id` locate the checkpoint. Resume with `answer_interrupt(run_dir, answer)`."""

    def __init__(
        self,
        payload: Any,
        run_dir: str | Path | None = None,
        run_id: str | None = None,
        payloads: list[Any] | None = None,
    ) -> None:
        self.payload = payload
        self.payloads = list(payloads) if payloads is not None else [payload]
        self.run_dir = str(run_dir) if run_dir is not None else None
        self.run_id = run_id
        super().__init__(describe_interrupt(payload, self.run_dir))


def interrupt_kind(payload: Any) -> str:
    """'choose_entity', 'plan', 'review' or 'other'."""
    if isinstance(payload, dict):
        if payload.get("type") == ENTITY_CHOICE:
            return ENTITY_CHOICE
        if "plan" in payload:
            return "plan"
        if "mdx_path" in payload:
            return "review"
    return "other"


def candidate_line(c: dict[str, Any]) -> str:
    score = c.get("score")
    tail = f", score {score:.2f}" if isinstance(score, int | float) else ""
    return f"{c.get('title')} (ticker {c.get('ticker') or 'n/a'}, CIK {c.get('cik')}{tail})"


def describe_interrupt(payload: Any, run_dir: str | None = None) -> str:
    where = f" Run directory: {run_dir}." if run_dir else ""
    kind = interrupt_kind(payload)
    if kind == ENTITY_CHOICE:
        cands = "; ".join(candidate_line(c) for c in payload.get("candidates", []))
        return (
            "The company name matches several SEC registrants: "
            + cands
            + ". Pass --ticker or --cik to choose one (or --private)."
            + where
        )
    if kind == "plan":
        return "The run is waiting for approval of the section plan (debug mode)." + where
    if kind == "review":
        return "The run is waiting for review of primer.mdx before export (debug mode)." + where
    msg = payload.get("message") if isinstance(payload, dict) else str(payload)
    return f"The run is paused on an interrupt: {msg}." + where


def pending_interrupts(result: Any) -> list[Any]:
    """The Interrupt objects in an invoke() result, [] when the graph ran to the end."""
    if isinstance(result, dict):
        return list(result.get("__interrupt__") or [])
    return []


def drive(
    graph: Any,
    first_input: Any,
    config: dict[str, Any],
    *,
    run_dir: str | Path | None = None,
    run_id: str | None = None,
    on_interrupt: OnInterrupt | None = None,
    runlog: Any = None,
) -> dict[str, Any]:
    """Invoke `graph` and keep resuming it while it reports `__interrupt__`.

    Every interrupt and its answer is recorded in run.json (`meta.interrupts`). With no
    callback the first interrupt raises `InterruptRequired` (and run.json says "paused")."""
    result = graph.invoke(first_input, config=config)
    for _ in range(MAX_ROUNDS):
        pending = pending_interrupts(result)
        if not pending:
            return result
        payloads = [p.value for p in pending]
        if on_interrupt is None:
            if runlog is not None:
                runlog.meta["status"] = "paused"
                runlog.meta["pending_interrupt"] = payloads[0]
                runlog.write()
            raise InterruptRequired(payloads[0], run_dir, run_id, payloads)
        answers = [on_interrupt(p) for p in payloads]
        if runlog is not None:
            log = runlog.meta.setdefault("interrupts", [])
            for pl, ans in zip(payloads, answers, strict=True):
                log.append({"kind": interrupt_kind(pl), "answer": ans})
        # one pending interrupt resumes by value; several (parallel branches) by id
        resume = (
            answers[0]
            if len(answers) == 1
            else {p.id: a for p, a in zip(pending, answers, strict=True)}
        )
        result = graph.invoke(Command(resume=resume), config=config)
    raise RuntimeError(f"graph still interrupted after {MAX_ROUNDS} resume rounds")


def answer_interrupt(
    run_dir: str | Path, answer: Any, on_interrupt: OnInterrupt | None = None
) -> dict[str, Any]:
    """Resume a run that raised `InterruptRequired` with `answer` for the pending interrupt.

    Returns the final graph state. Later interrupts go to `on_interrupt` (or raise again)."""
    from primer.graph.main import build_main, open_checkpointer
    from primer.runlog import get_runlog

    run_dir = Path(run_dir)
    run_id = run_dir.name
    graph = build_main(open_checkpointer(run_dir))
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    runlog = get_runlog(run_dir)
    runlog.meta.pop("pending_interrupt", None)
    runlog.meta.pop("status", None)
    final = drive(
        graph,
        Command(resume=answer),
        config,
        run_dir=run_dir,
        run_id=run_id,
        on_interrupt=on_interrupt,
        runlog=runlog,
    )
    runlog.write()
    return final


# ---- command-line prompts ----------------------------------------------------------------


def stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def make_cli_prompter(yes: bool = False, interactive: bool | None = None) -> OnInterrupt:
    """The `on_interrupt` callback of `primer run`.

    Interactive (a TTY and no --yes): candidates, the plan and the review summary are shown
    with typer prompts. With --yes or a non-TTY stdin: plan and review interrupts are
    approved, and an entity choice fails with a message listing the candidates."""
    import typer

    auto = yes or not (stdin_is_tty() if interactive is None else interactive)

    def on_interrupt(payload: Any) -> Any:
        kind = interrupt_kind(payload)
        if kind == ENTITY_CHOICE:
            cands = payload.get("candidates", [])
            if auto:
                typer.echo(describe_interrupt(payload), err=True)
                raise typer.Exit(code=2)
            typer.echo("The name matches more than one SEC registrant:")
            for i, c in enumerate(cands, start=1):
                typer.echo(f"  {i}. {candidate_line(c)}")
            typer.echo("  0. none of these: run the private-company path")
            while True:
                n = typer.prompt("Which company?", type=int)
                if n == 0:
                    return PRIVATE_ANSWER
                if 1 <= n <= len(cands):
                    return cands[n - 1].get("cik") or cands[n - 1].get("ticker")
                typer.echo(f"enter a number from 0 to {len(cands)}")
        if kind == "plan":
            plan = payload.get("plan") or {}
            typer.echo(f"Section plan: {len(plan)} sections")
            for sec, brief in sorted(plan.items(), key=lambda kv: int(kv[0])):
                title = (brief or {}).get("title", "")
                queries = len((brief or {}).get("web_queries") or [])
                typer.echo(f"  {sec:>2}. {title} ({queries} web queries)")
            if auto or typer.confirm("Approve the plan?", default=True):
                return "approve"
            return typer.prompt("Reason for rejecting the plan") or "rejected"
        if kind == "review":
            val = payload.get("validation") or {}
            typer.echo(f"Review {payload.get('mdx_path')}")
            if val:
                typer.echo(f"  validation passed: {val.get('passed')}")
            if auto or typer.confirm("Approve and export?", default=True):
                return "approve"
            return typer.prompt("Reason for rejecting the primer") or "rejected"
        # an interrupt this build does not know: approve when auto, else ask
        msg = payload.get("message") if isinstance(payload, dict) else str(payload)
        if auto or typer.confirm(f"{msg} Continue?", default=True):
            return "approve"
        return "rejected"

    return on_interrupt
