"""The eval set (PLAN 3.5, requirements §7): several companies, one row each.

`config/eval.yaml` lists the companies. For every entry this module finds (or, with
`--run`, produces) a run directory, scores it with `harness.scorecard`, and builds one row:

- the validation gate (pass/fail and the failed checks) and the run status;
- coverage counts: full / partial / unavailable / not_applicable;
- critic mean min/mean (run.json) and judge min/mean (the model judge when configured; for
  `rubric` entries it scores against the cited sources and the rubric, with no reference);
- fact overlap with the golden primer and the financials match to the golden JSON (golden
  entries only); the self-consistency block (rubric entries are held to it);
- sources: undated share and tier-1 share; tokens, Firecrawl credits, wall-clock;
- the `expect` checks from the entry.

`output/eval/eval-set-{date}.json` and `.md` carry the rows; `primer eval-set` exits non-zero
when any evaluated entry fails its gate or its expectations (or nothing was evaluated).

The pipeline is not tuned to a sector: a new company is a new entry in eval.yaml, a new
sector is a sources.yaml change.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from primer.config import CONFIG_DIR, REPO_ROOT, output_root
from primer.eval.harness import scorecard, write_scorecard

COVERAGE_STATUSES = ("full", "partial", "unavailable", "not_applicable")
RUN_FLAGS = ("private", "offline", "debug")
SECTION_KEY = re.compile(r"^section_?(\d{1,2})$")
BOOL_KEYS = ("is_public",)
PASS_FAIL_KEYS = ("gate", "self_consistency")
STRING_KEYS = ("fiscal_year_end", "sector_tag", "status")
NUMBER_KEYS = {
    "min_judge_mean": ("judge_mean", ">="),
    "min_judge_min": ("judge_min", ">="),
    "financials_exact": ("financials_exact", ">="),
    "max_undated_share": ("undated_share", "<="),
    "min_tier1_share": ("tier1_share", ">="),
}


class EvalEntry(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    ticker: str | None = None
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["golden", "rubric"] = "rubric"
    golden: str | None = None
    golden_financials: str | None = None
    flags: dict[str, bool] = Field(default_factory=dict)
    as_of: str | None = None
    sections: str | None = None
    sector: str | None = None
    note: str | None = None
    expect: dict[str, Any] = Field(default_factory=dict)

    @field_validator("flags")
    @classmethod
    def _known_flags(cls, v: dict[str, bool]) -> dict[str, bool]:
        bad = sorted(set(v) - set(RUN_FLAGS))
        if bad:
            raise ValueError(f"unknown flags {bad}; allowed: {list(RUN_FLAGS)}")
        return v

    @field_validator("as_of")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is not None:
            date.fromisoformat(str(v))
        return None if v is None else str(v)

    @model_validator(mode="after")
    def _golden_needs_a_path(self) -> EvalEntry:
        if self.kind == "golden" and not self.golden:
            raise ValueError(f"{self.name}: kind golden needs a golden path")
        return self

    # identity ---------------------------------------------------------------------------

    @property
    def slug(self) -> str:
        """The output folder name `graph.main.new_run` gives this company."""
        return self.ticker.upper() if self.ticker else slugify(self.name)

    @property
    def id(self) -> str:
        return self.ticker.upper() if self.ticker else slugify(self.name)

    def matches(self, token: str) -> bool:
        t = token.strip().lower()
        return t in {
            (self.ticker or "").lower(),
            slugify(self.name),
            self.name.lower(),
            *(slugify(a) for a in self.aliases),
            *(a.lower() for a in self.aliases),
        }


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "company"


class EvalSet(BaseModel):
    defaults: dict[str, Any] = Field(default_factory=dict)
    entries: list[EvalEntry]


def load_eval_set(path: Path | None = None) -> EvalSet:
    p = Path(path) if path else CONFIG_DIR / "eval.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    es = EvalSet.model_validate(data)
    ids = [e.id for e in es.entries]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"duplicate eval-set entries: {dup}")
    return es


def parse_only(only: list[str] | None) -> list[str]:
    """`--only LMT --only BA` and `--only LMT,BA` both work."""
    out: list[str] = []
    for item in only or []:
        out.extend(t.strip() for t in item.split(",") if t.strip())
    return out


def filter_entries(entries: list[EvalEntry], only: list[str] | None) -> list[EvalEntry]:
    tokens = parse_only(only)
    if not tokens:
        return list(entries)
    unknown = [t for t in tokens if not any(e.matches(t) for e in entries)]
    if unknown:
        raise ValueError(f"not in the eval set: {unknown}; entries: {[e.id for e in entries]}")
    return [e for e in entries if any(e.matches(t) for t in tokens)]


# ---- locating runs and references ---------------------------------------------------------


def latest_run_dir(entry: EvalEntry, root: Path | None = None) -> Path | None:
    """The newest run directory under output/{slug}/ (by the mtime of its run.json or
    primer.mdx), or None when the entry has never run."""
    base = (root or output_root()) / entry.slug
    if not base.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for d in base.iterdir():
        if not d.is_dir():
            continue
        stamps = [(d / n).stat().st_mtime for n in ("run.json", "primer.mdx") if (d / n).exists()]
        if stamps and (best is None or max(stamps) > best[0]):
            best = (max(stamps), d)
    return best[1] if best else None


def resolve_reference(rel: str | None) -> Path | None:
    """A golden file path from eval.yaml: GOLDEN_DIR/<name> when that env var is set and has
    the file, else relative to the repo root. None when the file is not there (the references
    are licensed and not committed)."""
    if not rel:
        return None
    gdir = os.environ.get("GOLDEN_DIR")
    if gdir and (Path(gdir) / Path(rel).name).exists():
        return Path(gdir) / Path(rel).name
    p = Path(rel)
    p = p if p.is_absolute() else REPO_ROOT / p
    return p if p.exists() else None


def parse_sections(s: str | None) -> list[int]:
    if not s:
        return list(range(1, 15))
    out: set[int] = set()
    for part in str(s).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return sorted(x for x in out if 1 <= x <= 14)


def default_runner(entry: EvalEntry) -> Path:
    """Execute `run_primer` for one entry (honouring its flags and as_of). Costs tokens and
    Firecrawl credits."""
    from primer.graph.main import run_primer
    from primer.state import Inputs

    inputs = Inputs(
        company_name=entry.name,
        ticker=entry.ticker,
        as_of_date=date.fromisoformat(entry.as_of) if entry.as_of else date.today(),
        sections=parse_sections(entry.sections),
        aliases=list(entry.aliases),
        **entry.flags,
    )
    result = run_primer(inputs)
    return Path(result["run_dir"])


# ---- expectations -------------------------------------------------------------------------


def _as_list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def check_expectations(expect: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate an entry's `expect` block against facts read from the run. One result per
    key: {key, expected, actual, ok}. An unknown key fails (a typo must not pass silently)."""
    out: list[dict[str, Any]] = []
    for key, expected in expect.items():
        actual: Any
        ok: bool
        m = SECTION_KEY.match(key)
        if m:
            actual = (facts.get("coverage") or {}).get(str(int(m.group(1))))
            ok = actual in [str(x) for x in _as_list(expected)]
        elif key == "fiscal_year_end":
            actual = facts.get("fiscal_year_end")
            ok = actual is not None and str(actual).zfill(4) == str(expected).zfill(4)
        elif key in STRING_KEYS:
            actual = facts.get(key)
            ok = actual in [str(x) for x in _as_list(expected)]
        elif key in BOOL_KEYS:
            actual = facts.get(key)
            ok = actual is not None and bool(actual) == bool(expected)
        elif key in PASS_FAIL_KEYS:
            actual = facts.get(key)
            ok = actual == str(expected).lower()
        elif key in NUMBER_KEYS:
            fact, op = NUMBER_KEYS[key]
            actual = facts.get(fact)
            ok = actual is not None and (
                actual >= float(expected) if op == ">=" else actual <= float(expected)
            )
        else:
            out.append(
                {"key": key, "expected": expected, "actual": "unknown expectation", "ok": False}
            )
            continue
        out.append({"key": key, "expected": expected, "actual": actual, "ok": bool(ok)})
    return out


# ---- one row ------------------------------------------------------------------------------


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def coverage_counts(coverage: dict[str, str]) -> dict[str, int]:
    counts = dict.fromkeys(COVERAGE_STATUSES, 0)
    for v in (coverage or {}).values():
        if v in counts:
            counts[v] += 1
    return counts


def _round(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(x, n)


def build_row(
    entry: EvalEntry,
    run_dir: Path,
    defaults: dict[str, Any] | None = None,
    scorecard_fn: Callable[..., dict[str, Any]] = scorecard,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Score one run directory and return the entry's row (see the module docstring)."""
    defaults = defaults or {}
    run_dir = Path(run_dir)
    run = _read_json(run_dir / "run.json") or {}
    meta = run.get("meta") or {}
    golden = resolve_reference(entry.golden)
    fin = resolve_reference(entry.golden_financials)
    notes: list[str] = []
    kind = entry.kind
    if entry.kind == "golden" and golden is None:
        kind = "rubric"
        notes.append(f"golden primer {entry.golden} not found; scored as a rubric entry")
    if entry.golden_financials and fin is None:
        notes.append(f"golden financials {entry.golden_financials} not found")

    failures: list[str] = []
    try:
        sc = scorecard_fn(run_dir, golden, fin)
    except Exception as e:  # a broken run must not stop the rest of the set
        sc = {}
        failures.append(f"scorecard raised {type(e).__name__}: {e}")
        notes.append(traceback.format_exc(limit=3))
    if out_dir and sc:
        write_scorecard(sc, out_dir)

    # validation gate
    validation = run.get("validation") or {}
    passed = validation.get("passed")
    failed_checks = sorted(
        k
        for k, v in (validation.get("checks") or {}).items()
        if isinstance(v, dict) and v.get("ok") is False
    )
    if passed is None:
        failures.append("no validation recorded (the run did not finish?)")
    elif not passed:
        failures.append("validation gate failed: " + (", ".join(failed_checks) or "see run.json"))

    # coverage
    coverage = {str(k): v for k, v in (meta.get("coverage") or run.get("coverage") or {}).items()}
    counts = coverage_counts(coverage)

    # critic
    critic_means = [
        float(v["mean"])
        for v in (run.get("critic_scores") or {}).values()
        if isinstance(v, dict) and v.get("mean") is not None
    ]

    # judge
    judge_mode = sc.get("judge_mode")
    judge_min, judge_mean = sc.get("min_section_mean"), sc.get("mean_section_mean")
    threshold = float(defaults.get("judge_min", 3.0))
    if judge_mode == "llm" and judge_min is not None and judge_min < threshold:
        failures.append(f"judge minimum {judge_min} is below {threshold}")

    # citations / sources
    cit = sc.get("citations") or {}
    if cit.get("ok") is False:
        failures.append("citations do not resolve")

    # golden-only
    fo = (sc.get("fact_overlap") or {}).get("mean_rate") if kind == "golden" else None
    fin_block = sc.get("financials") or {}
    fin_rate = fin_block.get("exact_match_rate")
    if fin_rate not in (None, 1.0):
        failures.append(f"section 9 matches the golden financials at {fin_rate:.0%}")

    # self-consistency
    sc_block = sc.get("self_consistency") or {}
    sc_ok = sc_block.get("ok")
    if sc_ok is False and kind in (defaults.get("require_self_consistency_for") or ["rubric"]):
        failures.append("self-consistency failed: " + ", ".join(sc_block.get("failed") or []))

    # tokens, credits, wall-clock
    tokens = sum(
        int(v.get("input", 0)) + int(v.get("output", 0))
        for v in (run.get("tokens") or {}).values()
        if isinstance(v, dict)
    )
    entity = meta.get("entity") or {}
    facts = {
        "coverage": coverage,
        "fiscal_year_end": entity.get("fiscal_year_end"),
        "sector_tag": entity.get("sector_tag"),
        "is_public": entity.get("is_public"),
        "status": meta.get("status"),
        "gate": None if passed is None else ("pass" if passed else "fail"),
        "self_consistency": None if sc_ok is None else ("pass" if sc_ok else "fail"),
        "judge_mean": judge_mean,
        "judge_min": judge_min,
        "financials_exact": fin_rate,
        "undated_share": cit.get("undated_share"),
        "tier1_share": cit.get("tier1_share"),
    }
    expect = check_expectations(entry.expect, facts)
    for r in expect:
        if not r["ok"]:
            failures.append(f"expected {r['key']} = {r['expected']!r}, got {r['actual']!r}")

    return {
        "id": entry.id,
        "name": entry.name,
        "ticker": entry.ticker,
        "kind": kind,
        "sector": entry.sector,
        "note": entry.note,
        "notes": notes,
        "skipped": False,
        "run_dir": str(run_dir),
        "run_id": meta.get("run_id") or run_dir.name,
        "status": meta.get("status"),
        "gate": {"passed": passed, "failed_checks": failed_checks},
        "coverage": counts,
        "critic": {
            "min": _round(min(critic_means)) if critic_means else None,
            "mean": _round(sum(critic_means) / len(critic_means)) if critic_means else None,
        },
        "judge": {"mode": judge_mode, "min": judge_min, "mean": judge_mean},
        "fact_overlap": fo,
        "financials_match": fin_rate,
        "self_consistency": {"ok": sc_ok, "failed": sc_block.get("failed") or []},
        "sources": {
            "count": cit.get("sources"),
            "undated_share": cit.get("undated_share"),
            "tier1_share": cit.get("tier1_share"),
        },
        "policy": meta.get("policy"),
        "tokens": tokens,
        "credits": (run.get("counters") or {}).get("firecrawl_credits"),
        "wall_clock_seconds": run.get("elapsed_seconds"),
        "expect": expect,
        "ok": not failures,
        "failures": failures,
    }


def skipped_row(entry: EvalEntry, reason: str, failure: bool = False) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.name,
        "ticker": entry.ticker,
        "kind": entry.kind,
        "sector": entry.sector,
        "skipped": True,
        "reason": reason,
        "ok": not failure,
        "failures": [reason] if failure else [],
    }


# ---- the set ------------------------------------------------------------------------------


def evaluate_set(
    entries: list[EvalEntry],
    defaults: dict[str, Any] | None = None,
    *,
    run: bool = False,
    runner: Callable[[EvalEntry], Path] = default_runner,
    scorecard_fn: Callable[..., dict[str, Any]] = scorecard,
    out_dir: Path | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Evaluate each entry's newest run, or with `run=True` execute it first (sequentially:
    entries share rate limits and the Firecrawl plan). Entries with no run are skipped and
    say so."""
    rows: list[dict[str, Any]] = []
    for e in entries:
        run_dir: Path | None
        t0 = time.perf_counter()
        if run:
            log(f"running {e.id} ...")
            try:
                run_dir = runner(e)
            except Exception as ex:  # one company failing must not stop the set
                rows.append(skipped_row(e, f"run raised {type(ex).__name__}: {ex}", failure=True))
                log(f"  {e.id}: run failed: {ex!r}")
                continue
            log(f"  {e.id}: run finished in {time.perf_counter() - t0:.0f}s -> {run_dir}")
        else:
            run_dir = latest_run_dir(e)
            if run_dir is None:
                reason = f"no run under {output_root() / e.slug}; skipped (use --run)"
                rows.append(skipped_row(e, reason))
                log(f"{e.id}: {reason}")
                continue
        rows.append(build_row(e, run_dir, defaults, scorecard_fn, out_dir))
    evaluated = [r for r in rows if not r.get("skipped")]
    failed = [r["id"] for r in rows if not r["ok"]]
    return {
        "date": date.today().isoformat(),
        "mode": "run" if run else "existing",
        "entries": rows,
        "evaluated": len(evaluated),
        "skipped": [r["id"] for r in rows if r.get("skipped") and r["ok"]],
        "failed": failed,
        "ok": bool(evaluated) and not failed,
    }


# ---- report -------------------------------------------------------------------------------


def _fmt(v: Any, nd: int = 2, pct: bool = False) -> str:
    if v is None:
        return "-"
    if pct:
        return f"{v:.0%}"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _minutes(sec: Any) -> str:
    return "-" if sec is None else f"{float(sec) / 60:.1f} min"


def render_markdown(report: dict[str, Any]) -> str:
    head = (
        "| Entry | Kind | Gate | Coverage F/P/U/NA | Critic min/mean | Judge min/mean | "
        "Fact overlap | Fin match | Undated | Tier-1 | Self-consistency | Tokens | Credits | "
        "Time | Expect | Result |"
    )
    lines = [
        f"# Eval set {report['date']}",
        "",
        f"Mode: {report['mode']}. Evaluated {report['evaluated']} of {len(report['entries'])} "
        f"entries; {'PASS' if report['ok'] else 'FAIL'}.",
        "",
        head,
        "|" + "---|" * 16,
    ]
    for r in report["entries"]:
        if r.get("skipped"):
            lines.append(
                f"| {r['id']} | {r['kind']} | - | - | - | - | - | - | - | - | - | - | - | - | - | "
                f"{'FAIL' if not r['ok'] else 'skipped'}: {r['reason']} |"
            )
            continue
        c, cr, j = r["coverage"], r["critic"], r["judge"]
        exp_ok = sum(1 for x in r["expect"] if x["ok"])
        g = r["gate"]["passed"]
        gate = (
            "-" if g is None else ("pass" if g else "FAIL " + ",".join(r["gate"]["failed_checks"]))
        )
        sc_ok = r["self_consistency"]["ok"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{r['id']} ({r['sector'] or '-'})",
                    r["kind"],
                    gate,
                    f"{c['full']}/{c['partial']}/{c['unavailable']}/{c['not_applicable']}",
                    f"{_fmt(cr['min'])}/{_fmt(cr['mean'])}",
                    f"{_fmt(j['min'])}/{_fmt(j['mean'])} ({j['mode'] or '-'})",
                    _fmt(r["fact_overlap"], pct=True),
                    _fmt(r["financials_match"], pct=True),
                    _fmt(r["sources"]["undated_share"], pct=True),
                    _fmt(r["sources"]["tier1_share"], pct=True),
                    "-" if sc_ok is None else ("ok" if sc_ok else "FAIL"),
                    f"{r['tokens']:,}",
                    _fmt(r["credits"]),
                    _minutes(r["wall_clock_seconds"]),
                    f"{exp_ok}/{len(r['expect'])}",
                    "ok" if r["ok"] else "FAIL",
                ]
            )
            + " |"
        )
    problems = [r for r in report["entries"] if r["failures"]]
    if problems:
        lines += ["", "## Failures", ""]
        for r in problems:
            lines.append(f"- **{r['id']}**")
            lines.extend(f"  - {f}" for f in r["failures"])
    notes = [r for r in report["entries"] if r.get("notes") or r.get("note")]
    if notes:
        lines += ["", "## Notes", ""]
        for r in notes:
            for n in [r.get("note"), *(r.get("notes") or [])]:
                if n:
                    lines.append(f"- {r['id']}: {' '.join(str(n).split())}")
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / f"eval-set-{report['date']}.json"
    mp = out_dir / f"eval-set-{report['date']}.md"
    jp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_markdown(report), encoding="utf-8")
    return jp, mp
