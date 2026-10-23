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

A golden primer is optional (PLAN 3.5): without one the judge scores each section against the
cited sources and the rubric only, there is no fact overlap, and a `self_consistency` block
checks the run's own section-9 numbers (balance-sheet identity, FCF = OCF - capex, positive
revenue, consecutive fiscal-year labels).

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
from primer.render.markdown import primer_path
from primer.state import SECTION_TITLES
from primer.validate.citations import check_citations

SCALE = {"USD millions": 1e6, "USD thousands": 1e3, "USD": 1.0}

SECTION_KEYWORDS: dict[int, tuple[str, ...]] = {
    1: ("key takeaway", "takeaway", "executive summary"),
    2: ("company overview", "overview", "company profile", "business description"),
    3: ("products, services", "product", "defense technologies", "technology"),
    4: ("key programs", "program", "contract"),
    5: ("market dynamics", "competitive positioning", "market", "competitive", "competition"),
    6: ("strategy & growth", "strategy", "growth"),
    7: ("acquisition history", "acquisition", "m&a"),
    8: ("corporate governance", "management", "governance", "leadership"),
    9: ("historical financial", "financial performance", "financials"),
    10: ("operating performance", "benchmark", "kpi"),
    11: ("capital structure", "liquidity", "balance sheet", "debt"),
    12: ("investment thesis", "thesis", "debate", "bull vs"),
    13: ("valuation",),
    14: ("recent developments", "recent development", "recent events", "news"),
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

    md = primer_path(run_dir)
    cit = check_citations(md.read_text(encoding="utf-8")) if md.exists() else {"ok": None}
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


RESULT_MARK = "RESULT FOR QUERY"


PAGE_NOISE = re.compile(
    r"^(RESULT FOR QUERY|RESULTS FOR QUERY:?|Page \d+ of \d+|©\d{4}, AlphaSense.*|<!-- page \d+ -->)\s*$"
)


def _split_alphasense(text: str) -> dict[int, str]:
    """AlphaSense export layout (pypdf text). The numbered table of contents lists the section
    titles; each section then starts in the body with the same title, unnumbered, on its own
    line. Page headers ('RESULT FOR QUERY', 'Page N of M', the copyright line) are dropped.
    The unnumbered 'Key Takeaways' and 'Company Overview' sections that precede the numbered
    ones are picked up by their own titles. Titles map to our numbering by keyword."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if not PAGE_NOISE.match(ln.strip())]
    toc_i = next(
        (i for i, ln in enumerate(lines) if ln.strip().upper() == "TABLE OF CONTENTS"), None
    )
    titles: list[str] = []
    body_start = 0
    if toc_i is not None:
        j = toc_i + 1
        while j < len(lines):
            m = re.match(r"^\s*\d{1,2}\.\s+(.+?)\s*$", lines[j])
            if not m:
                break
            titles.append(m.group(1))
            j += 1
        body_start = j
    titles = ["Key Takeaways", "Company Overview", *titles]
    # locate each title's first occurrence in the body as a line start
    found: list[tuple[int, str]] = []
    for title in titles:
        head = title.split(":")[0].strip().lower()
        for i in range(body_start, len(lines)):
            ln = lines[i].strip().lower()
            if ln.startswith(head) and len(ln) <= 100:
                found.append((i, title))
                break
    found.sort()
    out: dict[int, str] = {}
    for n, (i, title) in enumerate(found):
        end_i = found[n + 1][0] if n + 1 < len(found) else len(lines)
        body = "\n".join(lines[i + 1 : end_i]).strip()
        sec = _match_section(title)
        if sec and len(body) > 200:
            out[sec] = (out.get(sec, "") + "\n\n" + body).strip()
    return out


def split_golden(text: str) -> dict[int, str]:
    """A golden primer in any markdown/plain layout: headings (#, ##, or 'N. Title' lines) are
    mapped to our section numbers by keyword; unmatched text is dropped. The AlphaSense
    export layout is recognised by its 'RESULT FOR QUERY' markers."""
    if text.count(RESULT_MARK) >= 3 or "TABLE OF CONTENTS" in text:
        got = _split_alphasense(text)
        if got:
            return got
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
    md = primer_path(run_dir)
    if not md.exists():
        return {"ok": None, "error": "no primer.md"}
    text = md.read_text(encoding="utf-8")
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
        "undated_share": round(
            sum(1 for s in sources.values() if not s.get("published_date")) / n, 3
        )
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
        description="agreement with the reference where both state a fact, or with the cited "
        "sources when no reference is given; 3 when neither is available",
    )
    reason: str = Field(description="one or two sentences")


JUDGE_SYSTEM = (
    "You are grading one section of an automatically generated company primer against the "
    "requirements of an AlphaSense-style primer. Score 1-5 on each criterion: brief coverage "
    "(does it answer what the section is for), citation density (every factual sentence cites), "
    "exhibit (the required table is present and populated), style (caveat line, declarative "
    "prose, named-source perspectives, no first person), forbidden content (5 means no "
    "recommendation and no price prediction stated as the author's own view), and accuracy "
    "versus the reference section when one is given, else versus the excerpts of the cited "
    "sources when those are given (3 when neither). Be strict but fair: a "
    "section that honestly marks a gap as partial should not lose points for the gap itself."
)


def judge_section(
    section: int,
    markdown: str,
    reference: str | None,
    runlog=None,
    sources: str | None = None,
) -> dict[str, Any]:
    """Score one section. With a `reference` the judge compares against it; without one
    (a rubric entry of the eval set) it scores against the rubric and the excerpts of the
    sources the section cites, which is all the evidence the drafter had."""
    user = (
        f"Section {section}: {SECTION_TITLES.get(section, '')}\n\n"
        f"--- Generated section ---\n{markdown[:14000]}\n\n"
        + (
            f"--- Reference section (AlphaSense) ---\n{reference[:10000]}\n"
            if reference
            else "--- No reference section available ---\n"
        )
        + (
            f"\n--- Excerpts of the sources this section cites ---\n{sources[:14000]}\n"
            if sources and not reference
            else ""
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


def section_sources(run_dir: Path, section: int, per_source: int = 1500, total: int = 14000) -> str:
    """Excerpts of the sources section `section` cites (sources.json `cited_by`), headed by
    id, title, publisher and date: the evidence a reference-free judge scores against."""
    sp = Path(run_dir) / "sources.json"
    if not sp.exists():
        return ""
    rows = json.loads(sp.read_text(encoding="utf-8")).get("sources", [])
    parts: list[str] = []
    used = 0
    for r in rows:
        if f"sec-{section}" not in (r.get("cited_by") or []):
            continue
        ep = Path(run_dir) / r.get("evidence_path", "")
        body = ep.read_text(encoding="utf-8", errors="replace")[:per_source] if ep.is_file() else ""
        block = (
            f"### [{r.get('source_id')}] {r.get('title')} — {r.get('publisher')}, "
            f"{r.get('published_date') or 'n.d.'} (tier {r.get('tier')})\n{body}\n"
        )
        if used + len(block) > total:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


# ---- v1: self-consistency (no golden primer needed) --------------------------------------------

IDENTITY_TOLERANCE = 0.005


def _period_year(period: str) -> int | None:
    m = re.fullmatch(r"FY(\d{4})", str(period))
    return int(m.group(1)) if m else None


def self_consistency(run_dir: Path, tolerance: float = IDENTITY_TOLERANCE) -> dict[str, Any]:
    """Internal checks on the run's section-9 claims, for entries with no golden primer:

    - balance sheet: total_assets = total_liabilities + stockholders_equity within 0.5% per FY
      (noncontrolling interest is the usual, small, reason for a gap);
    - free_cash_flow = operating_cash_flow - capex per FY (to the dollar, 0.5 tolerance);
    - revenue is positive in every FY;
    - fiscal-year labels of the annual claims are consecutive.

    A check whose inputs are not in the run is `skipped`, never failed. `ok` is false only when
    a check ran and failed."""
    claims = [c for c in load_claims(Path(run_dir)) if c.get("section") == 9]
    by: dict[str, dict[str, float]] = {}
    for c in claims:
        fy = _period_year(c.get("period", ""))
        if fy is not None:
            by.setdefault(c["metric"], {})[f"FY{fy}"] = float(c["value"])

    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str = "", rows: list[dict[str, Any]] | None = None):
        checks.append({"name": name, "status": status, "detail": detail, "rows": rows or []})

    a, li, eq = (
        by.get(k, {}) for k in ("total_assets", "total_liabilities", "stockholders_equity")
    )
    years = sorted(set(a) & set(li) & set(eq))
    if years:
        bad = []
        for fy in years:
            gap = abs(a[fy] - (li[fy] + eq[fy]))
            rel = gap / abs(a[fy]) if a[fy] else float("inf")
            if rel > tolerance:
                bad.append(
                    {
                        "period": fy,
                        "assets": a[fy],
                        "liabilities_plus_equity": li[fy] + eq[fy],
                        "gap_pct": round(rel * 100, 3),
                    }
                )
        add(
            "balance_sheet_identity",
            "fail" if bad else "pass",
            f"assets = liabilities + equity within {tolerance:.1%} for {len(years)} FY",
            bad,
        )
    else:
        add("balance_sheet_identity", "skipped", "assets, liabilities and equity not all present")

    f, o, cx = (by.get(k, {}) for k in ("free_cash_flow", "operating_cash_flow", "capex"))
    years = sorted(set(f) & set(o) & set(cx))
    if years:
        bad = [
            {"period": fy, "fcf": f[fy], "ocf_minus_capex": o[fy] - cx[fy]}
            for fy in years
            if abs(f[fy] - (o[fy] - cx[fy])) >= 0.5
        ]
        add(
            "fcf_identity",
            "fail" if bad else "pass",
            f"free cash flow = operating cash flow - capex for {len(years)} FY",
            bad,
        )
    else:
        add(
            "fcf_identity",
            "skipped",
            "free cash flow, operating cash flow and capex not all present",
        )

    rev = by.get("revenue", {})
    if rev:
        bad = [{"period": fy, "revenue": v} for fy, v in sorted(rev.items()) if not v > 0]
        add("revenue_positive", "fail" if bad else "pass", f"revenue > 0 for {len(rev)} FY", bad)
    else:
        add("revenue_positive", "skipped", "no revenue claims")

    labels = sorted({int(fy[2:]) for m in by.values() for fy in m})
    if len(labels) >= 2:
        gaps = [
            {"after": f"FY{x}", "next": f"FY{y}"}
            for x, y in zip(labels, labels[1:], strict=False)
            if y - x != 1
        ]
        add(
            "fiscal_year_labels_consecutive",
            "fail" if gaps else "pass",
            f"FY labels {labels[0]}-{labels[-1]}",
            gaps,
        )
    else:
        add("fiscal_year_labels_consecutive", "skipped", "fewer than two fiscal years")

    return {
        "ok": not any(c["status"] == "fail" for c in checks),
        "ran": sum(1 for c in checks if c["status"] != "skipped"),
        "failed": [c["name"] for c in checks if c["status"] == "fail"],
        "checks": checks,
    }


# ---- v1: scorecard -----------------------------------------------------------------------------


def scorecard(
    run_dir: Path,
    golden_primer: Path | None = None,
    golden_financials: Path | None = None,
    sections: list[int] | None = None,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    md = primer_path(run_dir)
    primer_md = md.read_text(encoding="utf-8") if md.exists() else ""
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
            ref = theirs.get(sec)
            src = None if ref else section_sources(run_dir, sec)
            j = judge_section(sec, ours[sec], ref, runlog, src)
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
    result["self_consistency"] = self_consistency(run_dir)
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
