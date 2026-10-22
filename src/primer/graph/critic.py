"""Critic rubric (requirements §7): six criteria scored 1-5, written to run.json.

Deterministic checks always run; when an LLM is configured, a strong-model pass adds
notes and can lower (never raise) the deterministic scores.
"""

from __future__ import annotations

import re

from primer.llm import invoke_text
from primer.runlog import RunLog
from primer.sections.style import CITATION_TOKEN
from primer.state import Brief, CriticScore, SectionDraft

# the agent's own recommendation or price view; an analyst's reported "Buy" rating is a
# fact the thesis section must carry, so rating words alone are not forbidden
FORBIDDEN = re.compile(
    r"\b(we recommend|investors should (?:buy|sell)|the stock will (?:rise|fall)|price will|will rise to|will fall to|our view is|I think|I believe|in our opinion)\b",
    re.IGNORECASE,
)
FIRST_PERSON = re.compile(r"\b(I|we|our|us)\b")
QUOTED = re.compile(r"\"[^\"\n]{3,400}\"|\u201c[^\u201d\n]{3,400}\u201d")
# a fact sentence may carry its citation tokens after the terminal punctuation
FACT_SENTENCE = re.compile(
    r"(?:[^.!?\n]|\.(?=\d))*\d(?:[^.!?\n]|\.(?=\d))*[.!?](?!\d)(?:\s*\[src_\d{4}\])*"
)


def score(
    draft: SectionDraft, brief: Brief, pages_budget: float, runlog: RunLog | None = None
) -> CriticScore:
    md = draft.markdown
    prose = "\n".join(
        line for line in md.splitlines() if not line.startswith("|") and not line.startswith(">")
    )
    notes: list[str] = []

    fact_sentences = [s for s in FACT_SENTENCE.findall(prose) if s.strip()]
    cited = [s for s in fact_sentences if re.search(CITATION_TOKEN, s)]
    density = (len(cited) / len(fact_sentences)) if fact_sentences else 1.0
    citation_density = (
        5
        if density >= 0.95
        else 4
        if density >= 0.8
        else 3
        if density >= 0.6
        else 2
        if density >= 0.3
        else 1
    )
    if density < 0.95:
        notes.append(
            f"{len(fact_sentences) - len(cited)} of {len(fact_sentences)} fact sentences lack a citation"
        )

    distinct = len(set(re.findall(CITATION_TOKEN, md)))
    distinct_sources = (
        5
        if distinct >= 4
        else 4
        if distinct == 3
        else 3
        if distinct == 2
        else 2
        if distinct == 1
        else 1
    )
    if distinct < 2:
        notes.append("fewer than two distinct sources")

    exhibit = 5 if draft.exhibit_present else 1
    if not draft.exhibit_present:
        notes.append(f"required exhibit missing: {brief.required_exhibit}")

    brief_coverage = 5 if draft.coverage == "full" else 3 if draft.coverage == "partial" else 1

    style = 5
    if not draft.caveat and draft.coverage != "unavailable":
        style -= 1
        notes.append("no caveat line")
    unquoted = QUOTED.sub(" ", prose)  # quoted company or analyst language is not our voice
    if FIRST_PERSON.search(unquoted):
        style -= 2
        notes.append("first person detected")
    style = max(1, style)

    forbidden = 5
    if FORBIDDEN.search(unquoted):
        forbidden = 1
        notes.append("forbidden content: recommendation or price prediction language")

    words = len(prose.split())
    pages = words / 450
    if pages > pages_budget * 1.5:
        notes.append(f"over page budget: ~{pages:.1f} pages vs {pages_budget} budget (cut)")

    llm_notes = _llm_notes(draft, brief, runlog)
    notes.extend(llm_notes)

    sc = CriticScore(
        section=draft.section,
        brief_coverage=brief_coverage,
        citation_density=citation_density,
        distinct_sources=distinct_sources,
        exhibit=exhibit,
        style=style,
        forbidden_content=forbidden,
        notes=notes,
        passed=False,
    )
    sc.passed = sc.mean >= 3.0 and exhibit >= 3 and forbidden >= 3 and citation_density >= 3
    return sc


def _llm_notes(draft: SectionDraft, brief: Brief, runlog: RunLog | None) -> list[str]:
    system = (
        "You are a strict editor for a company primer. Return at most five short bullet notes, each starting "
        "with '- ', on: brief coverage, citation density, one-fact-many-citations collapse, numbers traceable to "
        "tier-1, style. Return 'OK' if nothing material."
    )
    user = f"Brief: {brief.model_dump_json()}\n\nDraft:\n{draft.markdown[:12000]}"
    text = invoke_text("critic", system, user, runlog)
    if not text or text.strip().upper().startswith("OK"):
        return []
    return [line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- ")][:5]
