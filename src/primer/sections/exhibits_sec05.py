"""Section 5 required exhibits: a sourced SWOT table and a peer list.

SWOT rule: every cell is a sentence lifted from a fetched source with its citation;
Strengths from the 10-K Item 1 positioning/competition sentences and the US-government
revenue share sentence; Weaknesses and Threats from Item 1A risk-factor lines (concentration
and dependence language goes to Weaknesses, the rest to Threats); Opportunities from the
MD&A outlook paragraph and the latest release ("backlog", "demand", "capacity"). A quadrant
with nothing sourced reads "no sourced item"; nothing is invented.

Peer list: the peers the foundation pack resolved from the 10-K competition paragraph, with
the first sentence of the peer's own 10-K Item 1 when it was fetched (cited to that filing),
else "named as a competitor in the 10-K" cited to the company's 10-K.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from primer.sections import text_tools as tt
from primer.sections.common import md_table

NO_ITEM = "no sourced item"
RISK_WORDS = re.compile(
    r"\b(could|may|might|adversely|risk|depend|subject to|failure|unable|uncertain|disrupt)\b",
    re.I,
)
WEAKNESS_WORDS = re.compile(
    r"\b(depend|dependent|reliance|rely|concentrat|significant portion|substantial portion|"
    r"single|limited number|cost overrun)\b",
    re.I,
)
OPPORTUNITY_WORDS = re.compile(
    r"\b(backlog|demand|capacity|growth|grow|expand|increase|opportunit|award|record)\b", re.I
)


@dataclass
class Swot:
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)
    threats: list[str] = field(default_factory=list)

    def cells(self) -> list[str]:
        return [*self.strengths, *self.weaknesses, *self.opportunities, *self.threats]


def risk_lines(item1a: str, limit: int = 12) -> list[str]:
    """Risk-factor headings/sentences from Item 1A: `text_tools.risk_factor_headings` plus a
    looser pass for 40-400 char paragraphs that read as a risk (abbreviations like U.S. add
    full stops that the strict helper rejects)."""
    out = list(tt.risk_factor_headings(item1a, limit=limit))
    for para in re.split(r"\n\s*\n", item1a):
        p = " ".join(para.split())
        if not (40 <= len(p) <= 400) or not p[0].isupper() or "|" in p:
            continue
        if p.lower().startswith(("item", "lorem")) or not RISK_WORDS.search(p):
            continue
        if p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _cited(sentences: list[str], sid: str | None, limit: int) -> list[str]:
    if not sid:
        return []
    out = []
    for s in sentences:
        s = " ".join(s.split()).strip()
        if s and s not in out:
            out.append(f"{s[:300].rstrip('.')}. [{sid}]")
        if len(out) >= limit:
            break
    return out


def build_swot(
    item1: str,
    item1a: str,
    k_sid: str | None,
    gov_sentence: str | None,
    outlook_paras: list[str],
    release_text: str,
    rel_sid: str | None,
    per_quadrant: int = 3,
) -> Swot:
    sw = Swot()
    strengths: list[str] = []
    if gov_sentence:
        strengths.append(gov_sentence)
    for kws in (["leading"], ["largest"], ["principally engaged"], ["compet"], ["international"]):
        for s in tt.sentences_with(item1, kws, limit=2):
            if s not in strengths and not s.lower().startswith("item"):
                strengths.append(s)
    sw.strengths = _cited(strengths, k_sid, per_quadrant)

    risks = risk_lines(item1a)
    weak = [r for r in risks if WEAKNESS_WORDS.search(r)]
    threat = [r for r in risks if r not in weak]
    sw.weaknesses = _cited(weak, k_sid, per_quadrant)
    sw.threats = _cited(threat, k_sid, per_quadrant)

    opps: list[tuple[str, str | None]] = []
    for p in outlook_paras:
        for s in tt.sentences(p):
            if OPPORTUNITY_WORDS.search(s) and re.search(
                r"\bexpect|\bgrow|\bcapacity|\bdemand", s, re.I
            ):
                opps.append((s, k_sid))
    for s in tt.sentences_with(release_text, ["backlog"], limit=2):
        opps.append((s, rel_sid))
    for kw in ("demand", "capacity"):
        for s in tt.sentences_with(release_text, [kw], limit=1):
            opps.append((s, rel_sid))
    seen: set[str] = set()
    for s, sid in opps:
        if not sid or s in seen or s.lower().startswith("lorem"):
            continue
        seen.add(s)
        sw.opportunities.append(f"{s[:300].rstrip('.')}. [{sid}]")
        if len(sw.opportunities) >= per_quadrant:
            break
    return sw


def swot_markdown(sw: Swot) -> str:
    n = max(1, len(sw.strengths), len(sw.weaknesses), len(sw.opportunities), len(sw.threats))

    def col(items: list[str]) -> list[str]:
        return [items[i] if i < len(items) else NO_ITEM for i in range(n)]

    cols = [col(sw.strengths), col(sw.weaknesses), col(sw.opportunities), col(sw.threats)]
    rows = [[cols[c][i] for c in range(4)] for i in range(n)]
    return md_table(
        ["Strengths", "Weaknesses", "Opportunities", "Threats"], rows, align_right_from=4
    )


def peer_markdown(peers: list[dict[str, Any]], k_sid: str | None) -> str:
    rows = []
    for p in peers:
        if p.get("item1_sid") and p.get("positioning"):
            pos = f"{p['positioning']} [{p['item1_sid']}]"
            src = f"peer 10-K Item 1 [{p['item1_sid']}]"
        else:
            pos = f"named as a competitor in the 10-K [{k_sid}]" if k_sid else NO_ITEM
            src = f"10-K competition paragraph [{k_sid}]" if k_sid else NO_ITEM
        rows.append([p.get("name") or "n/a", p.get("ticker") or "n/a", pos, src])
    return md_table(
        ["Peer", "Ticker", "Positioning (one line)", "Source"], rows, align_right_from=4
    )
