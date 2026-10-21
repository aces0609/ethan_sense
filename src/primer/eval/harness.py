"""Eval harness (PLAN 1.11 v0 + 2.9 v1).

v0 — `compare_financials`: a run's section-9 claims against a golden financial table (JSON,
USD millions), exact to the dollar.

v1 — `scorecard`: per-section judge-model rubric against the golden primer (markdown; the
AlphaSense reference, kept outside the repo per D6), fact-overlap scoring for the facts a
reader would check (deals, officers, programs, dollar amounts, percentages), and the
citation-resolvability rate. Without a model key the judge falls back to the deterministic
critic scores recorded in run.json and says so (`judge: template`).

Golden primer sections are matched to ours by heading keywords, since the AlphaSense
numbering differs (see PLAN.md §7).

`primer eval --run-dir … [--golden lmt.md] [--golden-financials lmt_financials.json]` writes
output/eval/scorecard-{run_id}.json.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from primer.config import llm_available
from primer.llm import invoke_structured
from primer.state import SECTION_TITLES
from primer.validate.citations import check_citations

SCALE = {"USD millions": 1e6, "USD thousands": 1e3, "USD": 1.0}

SECTION_KEYWORDS: dict[int, tuple[str, ...]] = {
    1: ("takeaway", "summary"),
    2: ("overview", "company profile", "business description"),
    3: ("product", "service", "technology"),
    4: ("program", "contract"),
    5: ("market", "competitive", "competition", "industry"),
    6: ("strategy", "growth"),
    7: ("acquisition", "m&a", "deal"),
    8: ("management", "governance", "leadership", "board"),
    9: ("historical financial", "financial performance", "financials"),
    10: ("operating performance", "benchmark", "kpi"),
    11: ("capital structure", "liquidity", "balance sheet", "debt"),
    12: ("thesis", "debate", "bull", "bear"),
    13: ("valuation",),
    14: ("recent development", "recent events", "news"),
}


# ---- v0 ----------------------------------------------------------------------------------


def load_claims(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / "claims.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def compare_financials(run_dir: Path, golden_path: Path, section: int = 9) -> dict[str, Any]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    scale = SCALE.get(golden.get("unit", "USD millions"), 1e6)
    claims = [c for c in load_claims(run_dir) if c["section"] == section]
    by_key = {(c["metric"], c["period"]): c for c in claims}

    rows = []
    matched = checked = unfilled = 0
    for metric, periods in golden.get("metrics", {}).items():
        for period, gval in periods.items():
            if gval is None:
                unfilled += 1
                rows.append({"metric": metric, "period": period, "status": "golden unfilled"})
                continue
            checked += 1
            c = by_key.get((metric, period))
            if c is None:
                rows.append(
                    {"metric": metric, "period": period, "golden": gval, "status": "missing in run"}
                )
                continue
            got = c["value"]
            exact = abs(got - gval * scale) < 0.5
            matched += int(exact)
            rows.append(
                {
                    "metric": metric,
                    "period": period,
                    "golden": gval,
                    "run": got / scale,
                    "status": "match" if exact else "MISMATCH",
                    "source_ids": c["source_ids"],
                    "concept": c.get("concept"),
                }
            )

    mdx = run_dir / "primer.mdx"
    cit = check_citations(mdx.read_text(encoding="utf-8")) if mdx.exists() else {"ok": None}
    cited = all(bool(r.get("source_ids")) for r in rows if r.get("status") in ("match", "MISMATCH"))
    return {
        "golden": str(golden_path),
        "run_dir": str(run_dir),
        "checked": checked,
        "matched": matched,
        "unfilled": unfilled,
        "exact_match_rate": round(matched / checked, 4) if checked else None,
        "every_number_cited": cited,
        "citations_resolve": cit.get("ok"),
        "rows": rows,
    }


# ---- v1: sections ---------------------------------------------------------------------------


def split_sections(markdown: str) -> dict[int, str]:
    """Our primer: '## N. Title' headings. Returns {section: body}."""
    out: dict[int, str] = {}
    parts = re.split(r"^## (\d{1,2})\. .*$", markdown, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        out[int(parts[i])] = parts[i + 1].strip()
    return out


def split_golden(text: str) -> dict[int, str]:
    """A golden primer in any markdown/plain layout: headings (#, ##, or 'N. Title' lines) are
    mapped to our section numbers by keyword; unmatched text is dropped."""
    lines = text.splitlines()
    heads: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if (
            re.match(r"^#{1,3}\s+\S", s)
            or re.match(r"^\d{1,2}[.)]\s+[A-Z]", s)
            or (3 < len(s) < 80 and s.isupper() and re.search(r"[A-Z]{3,}", s))
        ):
            heads.append((i, re.sub(r"^#+\s*|^\d{1,2}[.)]\s*", "", s)))
    out: dict[int, str] = {}
    for n, (i, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i + 1 : end]).strip()
        sec = _match_section(title)
        if sec and len(body) > 200:
            out[sec] = (out.get(sec, "") + "\n\n" + body).strip()
    return out


def _match_section(title: str) -> int | None:
    t = title.lower()
    best = None
    for sec, kws in SECTION_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                score = len(kw)
                if best is None or score > best[1]:
                    best = (sec, score)
    return best[0] if best else None


# ---- v1: fact overlap -------------------------------------------------------------------------

MONEY = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(billion|million|bn|mn|m\b|b\b|k\b)?",
    re.I,
)
PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[A-Z]\.|&|of|and|de))*\s+[A-Z][a-z]+)\b")
DESIG = re.compile(r"\b([A-Z]{1,4}-\d{1,3}[A-Z]{0,2})\b")
STOP_NAMES = {
    "Key Takeaways",
    "Company Overview",
    "Recent Developments",
    "Free Cash",
    "Net Income",
    "Operating Income",
    "Fiscal Year",
    "United States",
    "Department of",
    "The Company",
}


def _scale(unit: str | None) -> float:
    u = (unit or "").lower()
    return {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6, "k": 1e3}.get(
        u, 1.0
    )


def fact_sets(text: str) -> dict[str, set[str]]:
    money = set()
    for m in MONEY.finditer(text):
        v = float(m.group(1).replace(",", "")) * _scale(m.group(2))
        if v >= 1e5:
            money.add(f"{v:.3g}")
    pct = {f"{float(p):g}" for p in PCT.findall(text)}
    names = set()
    for m in NAME.finditer(text):
        n = m.group(1).strip()
        if n in STOP_NAMES or len(n) < 6 or n.split()[0] in ("The", "In", "On", "At", "For"):
            continue
        names.add(n)
    desigs = set(DESIG.findall(text))
    return {"amounts": money, "percentages": pct, "names": names, "programs": desigs}


def fact_overlap(primer_md: str, golden_text: str, per_section: bool = True) -> dict[str, Any]:
    """Share of the golden primer's facts that appear in ours, by family, overall and per
    matched section. Names are compared case-insensitively on the normalised string."""

    def norm(s: set[str]) -> set[str]:
        return {x.lower() for x in s}

    def score(p: str, g: str) -> dict[str, Any]:
        pf, gf = fact_sets(p), fact_sets(g)
        out: dict[str, Any] = {}
        for fam in pf:
            gs, ps = norm(gf[fam]), norm(pf[fam])
            hit = gs & ps
            out[fam] = {
                "golden": len(gs),
                "found": len(hit),
                "rate": round(len(hit) / len(gs), 3) if gs else None,
                "missing_sample": sorted(gs - ps)[:8],
            }
        return out

    result: dict[str, Any] = {"overall": score(primer_md, golden_text)}
    if per_section:
        ours, theirs = split_sections(primer_md), split_golden(golden_text)
        result["sections"] = {
            str(s): score(ours.get(s, ""), theirs[s]) for s in sorted(theirs) if s in ours
        }
        result["golden_sections_matched"] = sorted(theirs)
    rates = [v["rate"] for v in result["overall"].values() if v["rate"] is not None]
    result["mean_rate"] = round(sum(rates) / len(rates), 3) if rates else None
    return result


# ---- v1: citations ---------------------------------------------------------------------------


def citation_resolvability(run_dir: Path) -> dict[str, Any]:
    mdx = run_dir / "primer.mdx"
    if not mdx.exists():
        return {"ok": None, "error": "no primer.mdx"}
    text = mdx.read_text(encoding="utf-8")
    cit = check_citations(text)
    sources = {}
    sp = run_dir / "sources.json"
    if sp.exists():
        sources = {s["source_id"]: s for s in json.loads(sp.read_text(encoding="utf-8"))["sources"]}
    evidence_present = sum(1 for s in sources.values() if (run_dir / s["evidence_path"]).exists())
    with_url = sum(1 for s in sources.values() if s.get("url", "").startswith("http"))
    n = len(sources)
    return {
        "ok": bool(cit["ok"]) and (evidence_present == n),
        "footnote_refs": cit["refs"],
        "footnote_defs": cit["defs"],
        "unresolved": cit["unresolved"],
        "unused": cit["unused"],
        "sources": n,
        "evidence_files_present": evidence_present,
        "sources_with_url": with_url,
        "rate": round(evidence_present / n, 3) if n else None,
        "tier1_share": round(sum(1 for s in sources.values() if s.get("tier") == 1) / n, 3)
        if n
        else None,
    }


# ---- v1: judge rubric --------------------------------------------------------------------------


class RubricScore(BaseModel):
    brief_coverage: int = Field(ge=1, le=5)
    citation_density: int = Field(ge=1, le=5)
    exhibit: int = Field(ge=1, le=5)
    style: int = Field(ge=1, le=5)
    forbidden_content: int = Field(
        ge=1, le=5, description="5 = none; 1 = recommendation or own price view"
    )
    accuracy_vs_reference: int = Field(
        ge=1,
        le=5,
        description="agreement with the reference where both state a fact; 3 when no reference",
    )
    reason: str = Field(description="one or two sentences")


JUDGE_SYSTEM = (
    "You are grading one section of an automatically generated company primer against the "
    "requirements of an AlphaSense-style primer. Score 1-5 on each criterion: brief coverage "
    "(does it answer what the section is for), citation density (every factual sentence cites), "
    "exhibit (the required table is present and populated), style (caveat line, declarative "
    "prose, named-source perspectives, no first person), forbidden content (5 means no "
    "recommendation and no price prediction stated as the author's own view), and accuracy "
    "versus the reference section when one is given (3 when none). Be strict but fair: a "
    "section that honestly marks a gap as partial should not lose points for the gap itself."
)


def judge_section(
    section: int, markdown: str, reference: str | None, runlog=None
) -> dict[str, Any]:
    user = (
        f"Section {section}: {SECTION_TITLES.get(section, '')}\n\n"
        f"--- Generated section ---\n{markdown[:14000]}\n\n"
        + (
            f"--- Reference section (AlphaSense) ---\n{reference[:10000]}\n"
            if reference
            else "--- No reference section available ---\n"
        )
    )
    out = invoke_structured("judge", RubricScore, JUDGE_SYSTEM, user, runlog)
    if out is None:
        return {}
    d = out.model_dump()
    vals = [
        d[k]
        for k in (
            "brief_coverage",
            "citation_density",
            "exhibit",
            "style",
            "forbidden_content",
            "accuracy_vs_reference",
        )
    ]
    d["mean"] = round(sum(vals) / len(vals), 2)
    d["judge"] = "llm"
    return d


def judge_from_runlog(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Template fallback: the critic's deterministic scores already in run.json."""
    rp = run_dir / "run.json"
    if not rp.exists():
        return {}
    run = json.loads(rp.read_text(encoding="utf-8"))
    out = {}
    for sec, sc in (run.get("critic_scores") or {}).items():
        out[str(sec)] = {
            "brief_coverage": sc.get("brief_coverage"),
            "citation_density": sc.get("citation_density"),
            "exhibit": sc.get("exhibit"),
            "style": sc.get("style"),
            "forbidden_content": sc.get("forbidden_content"),
            "accuracy_vs_reference": 3,
            "mean": sc.get("mean"),
            "judge": "template",
            "reason": "deterministic critic score from run.json (no judge model configured)",
        }
    return out


# ---- v1: scorecard -----------------------------------------------------------------------------


def scorecard(
    run_dir: Path,
    golden_primer: Path | None = None,
    golden_financials: Path | None = None,
    sections: list[int] | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    mdx = run_dir / "primer.mdx"
    primer_md = mdx.read_text(encoding="utf-8") if mdx.exists() else ""
    ours = split_sections(primer_md)
    golden_text = (
        golden_primer.read_text(encoding="utf-8", errors="replace") if golden_primer else None
    )
    theirs = split_golden(golden_text) if golden_text else {}

    judge: dict[str, dict[str, Any]] = {}
    mode = "llm" if llm_available() else "template"
    if mode == "llm":
        from primer.runlog import get_runlog

        runlog = get_runlog(run_dir)
        for sec in sorted(ours):
            if sections and sec not in sections:
                continue
            j = judge_section(sec, ours[sec], theirs.get(sec), runlog)
            if j:
                judge[str(sec)] = j
    if not judge:
        judge = judge_from_runlog(run_dir)
        mode = "template"

    means = [v["mean"] for v in judge.values() if v.get("mean") is not None]
    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "golden_primer": str(golden_primer) if golden_primer else None,
        "golden_financials": str(golden_financials) if golden_financials else None,
        "judge_mode": mode,
        "sections_scored": sorted(int(k) for k in judge),
        "section_scores": judge,
        "min_section_mean": min(means) if means else None,
        "mean_section_mean": round(sum(means) / len(means), 2) if means else None,
        "all_sections_at_least_3": bool(means) and min(means) >= 3.0,
        "citations": citation_resolvability(run_dir),
    }
    if golden_text:
        result["fact_overlap"] = fact_overlap(primer_md, golden_text)
    if golden_financials:
        fin = compare_financials(run_dir, golden_financials)
        result["financials"] = {k: v for k, v in fin.items() if k != "rows"}
        result["financials"]["rows"] = [r for r in fin["rows"] if r.get("status") != "match"]
    rp = run_dir / "run.json"
    if rp.exists():
        run = json.loads(rp.read_text(encoding="utf-8"))
        result["run"] = {
            "elapsed_seconds": run.get("elapsed_seconds"),
            "credits": (run.get("counters") or {}).get("firecrawl_credits"),
            "tokens": run.get("tokens"),
            "coverage": run.get("coverage"),
            "status": (run.get("meta") or {}).get("status"),
        }
    return result


def write_scorecard(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"scorecard-{Path(result['run_dir']).name}.json"
    p.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return p
