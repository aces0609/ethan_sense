"""Section 12 exhibits: bull vs bear table and the unanswered-questions list (requirements §6 row 12).

Sourced points only. Analyst questions are parsed from earnings-call transcripts (the
operator line names the analyst and firm, or the speaker header carries the firm), and
the management answer that follows each question in the same transcript (the next
speaker turn(s) by a company officer, up to ~120 words) is kept with it, cited to the
same transcript source id; the officer is named as the transcript names them.
Rating changes come from the market-data adapter (MarketBeat page) and from news pages
whose own text names the firm. A firm is attributed only when the cited source's text
names it (`names_firm`), which is what `validate/rules.py: check_attribution` enforces.
Company-stated outlook and risk sentences from the 10-K fill the table as labelled rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from primer.sections.common import claim, md_table
from primer.sections.exhibits_sec07 import FULL_DATE_RE, _iso_from_match
from primer.sections.text_tools import norm_company, risk_factor_headings, sentences, sentences_with
from primer.state import Claim

SPEAKER_INLINE = re.compile(r"^\s*\*\*([^*\n]{2,60}?)\s*:?\s*\*\*\s*:?\s*(.*)$")
SPEAKER_FOOL = re.compile(r"^\s*\*\*([^*\n]{2,60}?)\*\*\s*(?:--|—|–|-)\s*\*?([^*\n]+?)\*?\s*$")
SPEAKER_PLAIN = re.compile(r"^([A-Z][A-Za-z.'\- ]{2,40}):\s+(.+)$")
OPERATOR_INTRO = re.compile(
    r"question (?:comes|is) from (?:the line of )?([A-Z][A-Za-z.'\- ]+?)\s+(?:with|of|from|at)\s+"
    r"([A-Z][A-Za-z&.'\- ]+?)(?=[.,;]|\s+Your\b|\s+Please\b|\s+Go ahead|$)"
)
GREETING = re.compile(
    r"^(good (morning|afternoon|evening)|thanks?|thank you|hi\b|hey\b|hello|congrat|yes\b|yeah|okay|ok\b|great|sure|morning|afternoon)",
    re.I,
)
BULL_KW = (
    "upside",
    "growth",
    "raise",
    "record",
    "capacity",
    "opportunit",
    "tailwind",
    "expand",
    "demand",
    "accelerat",
    "ramp",
    "backlog",
)
BEAR_KW = (
    "risk",
    "pressure",
    "delay",
    "cut",
    "headwind",
    "charge",
    "overrun",
    "concern",
    "decline",
    "loss",
    "slow",
    "intact",
    "overhang",
    "shortfall",
)
FIRM = r"([A-Z][A-Za-z&.'\-]+(?: [A-Z][A-Za-z&.'\-]+){0,3})"
PAGE_ACTION = re.compile(
    rf"\b{FIRM} (upgraded|downgraded|raised|lowered|cut|initiated|reiterated|maintained)\b([^.]{{0,200}})"
)
NOT_FIRM_FIRST = {
    "The",
    "This",
    "It",
    "In",
    "On",
    "At",
    "Shares",
    "Stock",
    "Analysts",
    "Analyst",
    "Wall",
    "A",
    "An",
}
TARGET_IN_TEXT = re.compile(r"(?:price )?target (?:price )?(?:to|of|at) \$([\d,]+(?:\.\d+)?)", re.I)
ANSWER_WORDS = 90  # cap on the management answer kept with a question (page budget, review fix 7)
TITLE_WORDS = ("officer", "president", "chairman", "treasurer", "ceo", "cfo", "coo", "head of")


@dataclass
class Debate:
    firm: str
    analyst: str
    question: str
    quarter: str
    source_id: str
    lean: str  # bull | bear | open
    answer: str | None = None  # management's answer from the same transcript, trimmed
    answered_by: str | None = None  # officer name(s) as the transcript names them


@dataclass
class Point:
    side: str  # bull | bear
    text: str  # cell text without the citation
    source_id: str
    firm: str | None = None
    kind: str = ""  # rating | question | news | company


@dataclass
class ThesisExhibit:
    markdown: str
    questions_md: str
    claims: list[Claim]
    source_ids: list[str]
    points: list[Point]
    debates: list[Debate]
    present: bool
    notes: list[str] = field(default_factory=list)


def names_firm(text: str, firm: str) -> bool:
    return bool(firm) and firm.lower() in text.lower()


# ---- transcript Q&A --------------------------------------------------------------------


def _blocks(md: str) -> list[tuple[str, str, str]]:
    """(speaker, role, text) blocks from a transcript in inline (**Name:** text), Motley Fool
    (**Name** -- *Firm -- Analyst* header line) or plain (Name: text) form."""
    out: list[list[str]] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = SPEAKER_FOOL.match(line)
        if m and not line.rstrip().endswith("**"):
            out.append([m.group(1).strip(), m.group(2).strip(), ""])
            continue
        m = SPEAKER_INLINE.match(line)
        if m:
            out.append([m.group(1).strip(), "", m.group(2).strip()])
            continue
        m = SPEAKER_PLAIN.match(line)
        if m and len(m.group(1).split()) <= 4:
            out.append([m.group(1).strip(), "", m.group(2).strip()])
            continue
        if out:
            out[-1][2] = (out[-1][2] + " " + line.strip()).strip()
    return [(s, r, t) for s, r, t in out]


def _first_question(text: str) -> str | None:
    sents = [s for s in sentences(text) if len(s) >= 20]
    cand = [s for s in sents if not GREETING.match(s)]
    if not cand:
        return None
    for s in cand[:3]:
        if s.endswith("?"):
            return s[:260]
    return cand[0][:260]


def _lean(q: str) -> str:
    low = q.lower()
    b = sum(1 for k in BULL_KW if k in low)
    r = sum(1 for k in BEAR_KW if k in low)
    if b > r:
        return "bull"
    if r > b:
        return "bear"
    return "open"


def _firm_for(speaker: str, role: str, firm_of: dict[str, str]) -> str | None:
    """The analyst's firm from the operator introductions or the speaker header, else None
    (an officer, the operator or an unintroduced speaker)."""
    if "analyst" in role.lower():
        return role.split("--")[0].split("—")[0].strip() or None
    key = speaker.lower()
    if key in firm_of:
        return firm_of[key]
    last = key.split()[-1] if key.split() else ""
    return next((f for k, f in firm_of.items() if last and k.split()[-1] == last), None)


def _trim_words(text: str, max_words: int = ANSWER_WORDS) -> str:
    """Whole sentences up to max_words; a first sentence longer than that is cut with an ellipsis."""
    words = " ".join(text.split())
    if len(words.split()) <= max_words:
        return words
    kept: list[str] = []
    n = 0
    for s in sentences(words):
        k = len(s.split())
        if kept and n + k > max_words:
            break
        if not kept and k > max_words:
            return " ".join(s.split()[:max_words]).rstrip(",;:") + "…"
        kept.append(s)
        n += k
    return " ".join(kept)


def _officer_title(name: str, intro: str) -> str | None:
    """The title the transcript's opening gives an officer ("Evan Scott, our Chief Financial
    Officer" -> "Chief Financial Officer"), matched on the surname so a nickname in the
    introduction (Jim / James) still resolves; None when the opening does not give one."""
    parts = name.split()
    if not parts or not intro:
        return None
    last = re.escape(parts[-1])
    m = re.search(rf"\b[A-Z][a-z]+ {last},?\s+(?:our |the |as )?([^.;]{{3,120}})", intro)
    if not m:
        return None
    title = m.group(1).split(", and ")[0].strip(" ,")
    if len(title) > 70 or not any(w in title.lower() for w in TITLE_WORDS):
        return None
    return title


def _answer_after(
    blocks: list[tuple[str, str, str]],
    start: int,
    asker: str,
    firm_of: dict[str, str],
    intro: str,
) -> tuple[str | None, str | None]:
    """Management's answer to the question in blocks[start]: the following speaker turn(s)
    that are not the operator, the asker or another analyst, up to ANSWER_WORDS words."""
    texts: list[str] = []
    names: list[str] = []
    n = 0
    for speaker, role, text in blocks[start + 1 :]:
        if speaker.lower().startswith("operator") or speaker.lower() == asker.lower():
            break
        if _firm_for(speaker, role, firm_of):
            break
        if not text.strip():
            continue
        title = role.strip() if role and "analyst" not in role.lower() else None
        title = title or _officer_title(speaker, intro)
        label = f"{speaker} ({title})" if title else speaker
        if label not in names:
            names.append(label)
        texts.append(text.strip())
        n += len(text.split())
        if n >= ANSWER_WORDS:
            break
    if not texts:
        return None, None
    return _trim_words(" ".join(texts)), " and ".join(names)


def parse_qa(md: str, quarter: str, source_id: str, max_questions: int = 6) -> list[Debate]:
    """One debate per analyst: the first substantive sentence of their first question, with
    the management answer that follows it in the same transcript when one does."""
    blocks = _blocks(md)
    firm_of: dict[str, str] = {}
    out: list[Debate] = []
    asked: set[str] = set()
    intro_parts: list[str] = []
    for i, (speaker, role, text) in enumerate(blocks):
        if speaker.lower().startswith("operator"):
            for m in OPERATOR_INTRO.finditer(text):
                firm_of[m.group(1).strip().lower()] = m.group(2).strip()
            continue
        if "analyst" in role.lower():
            firm_of[speaker.lower()] = role.split("--")[0].split("—")[0].strip()
        key = speaker.lower()
        firm = _firm_for(speaker, role, firm_of)
        if not firm:
            if not out:
                intro_parts.append(text)  # prepared remarks name the officers and titles
            continue
        if key in asked:
            continue
        q = _first_question(text)
        if not q:
            continue
        asked.add(key)
        answer, who = _answer_after(blocks, i, speaker, firm_of, " ".join(intro_parts))
        lean = _lean(q)
        if lean == "open" and answer:
            lean = _lean(answer)
        out.append(
            Debate(
                firm=firm,
                analyst=speaker,
                question=q,
                quarter=quarter,
                source_id=source_id,
                lean=lean,
                answer=answer,
                answered_by=who,
            )
        )
        if len(out) >= max_questions:
            break
    return out


# ---- rating changes ----------------------------------------------------------------------


def classify_rating(action: str, rating: str | None) -> str:
    """The rating decides the column when it is directional (a Sell with a raised target is
    still a bear point); the action decides only for neutral ratings or no rating."""
    a = (action or "").lower()
    r = (rating or "").lower()
    if any(k in r for k in ("sell", "underweight", "underperform", "negative", "reduce")):
        return "bear"
    if any(k in r for k in ("buy", "overweight", "outperform", "positive", "accumulate")):
        return "bull"
    if any(k in a for k in ("upgrad", "boost", "raise", "increase")):
        return "bull"
    if any(k in a for k in ("downgrad", "lower", "cut", "reduce", "decrease")):
        return "bear"
    return "open"


def rating_points(
    changes: list[Any], texts: dict[str, str], section: int = 12
) -> tuple[list[Point], list[Claim], list[str]]:
    """Points and price-target claims from adapter RatingChange rows whose page names the firm."""
    points: list[Point] = []
    claims: list[Claim] = []
    skipped: list[str] = []
    for rc in changes:
        sid = getattr(rc, "source_id", None)
        firm = getattr(rc, "firm", "") or ""
        if not sid or sid not in texts or not names_firm(texts[sid], firm):
            skipped.append(firm or "unnamed")
            continue
        side = classify_rating(getattr(rc, "action", ""), getattr(rc, "rating", None))
        d = getattr(rc, "date", None)
        when = d.isoformat() if isinstance(d, date) else "n.d."
        bits = [getattr(rc, "action", "rating")]
        if getattr(rc, "rating", None):
            bits.append(
                f"to {rc.rating}"
                + (f" from {rc.rating_prior}" if getattr(rc, "rating_prior", None) else "")
            )
        if getattr(rc, "target", None):
            bits.append(
                f"target ${rc.target:,.0f}"
                + (f" from ${rc.target_prior:,.0f}" if getattr(rc, "target_prior", None) else "")
            )
            slug = re.sub(r"[^a-z0-9]+", "_", firm.lower()).strip("_")[:40]
            c = claim(
                section,
                f"price_target_{slug}",
                float(rc.target),
                when if when != "n.d." else "trailing-12m",
                [sid],
                unit="USD",
                note=f"{firm} {rc.action} on {when} ({getattr(rc, 'publisher', None) or 'rating page'})",
            )
            if c:
                claims.append(c)
        text = (
            f"{firm}: {' '.join(bits)} ({when}, {getattr(rc, 'publisher', None) or 'rating page'})"
        )
        if side in ("bull", "bear"):
            points.append(Point(side=side, text=text, source_id=sid, firm=firm, kind="rating"))
    return points, claims, skipped


def page_points(
    text: str, source_id: str, published: date | None, company_names: list[str]
) -> list[Point]:
    """Upgrade/downgrade sentences on a news page that name the firm in the page's own text."""
    flat = " ".join(text.split())
    me = {norm_company(n).split()[0] for n in company_names if norm_company(n)}
    out: list[Point] = []
    seen: set[str] = set()
    for m in PAGE_ACTION.finditer(flat):
        firm, verb, rest = m.group(1).strip(), m.group(2).lower(), m.group(3)
        words = norm_company(firm).split()
        first = firm.split()[0] if firm.split() else ""
        if not words or first in NOT_FIRM_FIRST or words[0] in me:
            continue
        if verb in ("upgraded", "raised"):
            side = "bull"
        elif verb in ("downgraded", "lowered", "cut"):
            side = "bear"
        else:
            continue
        key = (firm.lower(), verb)
        if key in seen:
            continue
        seen.add(key)
        fd = FULL_DATE_RE.search(rest) or FULL_DATE_RE.search(
            flat[max(0, m.start() - 200) : m.start()]
        )
        when = (_iso_from_match(fd) if fd else None) or (
            published.isoformat() if published else "n.d."
        )
        tgt = TARGET_IN_TEXT.search(rest)
        detail = f"{verb}{(' ' + rest.strip())[:140]}"
        detail = re.sub(r"\s+", " ", detail).rstrip(" ,;")
        out.append(
            Point(
                side=side,
                text=f"{firm}: {detail} ({when}, news)"
                + (f"; target ${tgt.group(1)}" if tgt else ""),
                source_id=source_id,
                firm=firm,
                kind="news",
            )
        )
        if len(out) >= 4:
            break
    return out


def company_points(item7: str, sid7: str | None, item1a: str, sid1a: str | None) -> list[Point]:
    out: list[Point] = []
    if sid7 and item7:
        n = 0
        for s in sentences_with(item7, ["expect"], limit=10):
            if re.search(r"\d", s) and "|" not in s:
                out.append(
                    Point("bull", f"Company outlook (10-K MD&A): {s[:220]}", sid7, None, "company")
                )
                n += 1
            if n >= 2:
                break
    if sid1a and item1a:
        for h in risk_factor_headings(item1a, limit=2):
            out.append(
                Point(
                    "bear",
                    f"Company-disclosed risk (10-K Item 1A): {h[:220]}",
                    sid1a,
                    None,
                    "company",
                )
            )
    return out


# ---- exhibit --------------------------------------------------------------------------------


DEFLECTION = re.compile(
    r"\b(too early|not going to|won't (?:get ahead|provide|comment|give)|don't want to|"
    r"we'll (?:update|come back|get back|provide more|talk more)|will provide more detail|"
    r"in due course|not yet in a position|can't comment|cannot comment|"
    r"haven't (?:given|provided)|decline[sd]? to)\b",
    re.I,
)


def is_deflection(answer: str | None) -> bool:
    """True when the question drew no answer, a one-line reply, or a stated deferral."""
    if not answer:
        return True
    return len(answer.split()) < 12 or bool(DEFLECTION.search(answer))


def answer_text(d: Debate) -> str:
    """'What management said (officer as the transcript names them): answer' for a debate."""
    who = d.answered_by or "management"
    return f"What management said ({who}): {d.answer}"


def answer_line(d: Debate) -> str:
    """The answer appended to a question cell, or '' when the transcript carries none."""
    return f" — {answer_text(d)}" if d.answer else ""


def thesis_exhibit(
    *,
    debates: list[Debate],
    rating: list[Point],
    rating_claims: list[Claim],
    news: list[Point],
    company: list[Point],
    rating_count: int | None,
    rating_sid: str | None,
    section: int = 12,
) -> ThesisExhibit:
    points: list[Point] = list(rating) + list(news)
    for d in debates:
        if d.lean in ("bull", "bear"):
            points.append(
                Point(
                    d.lean,
                    f"{d.firm} asked ({d.quarter} call): {d.question}" + answer_line(d),
                    d.source_id,
                    d.firm,
                    "question",
                )
            )
    points.extend(company)
    bull = [p for p in points if p.side == "bull"]
    bear = [p for p in points if p.side == "bear"]
    rows = []
    for i in range(max(len(bull), len(bear), 1)):
        b = f"{bull[i].text} [{bull[i].source_id}]" if i < len(bull) else "no sourced item"
        r = f"{bear[i].text} [{bear[i].source_id}]" if i < len(bear) else "no sourced item"
        rows.append([b, r])
    md = md_table(["Bull case (sourced)", "Bear case (sourced)"], rows, align_right_from=99)
    # The list holds the questions management did not answer or deferred. Answered questions are
    # already in the debates narrative and in the table, so repeating them with their answers
    # here doubled section 12's length (4.1 pages against a 3.75-page limit on 2026-10-22).
    # One question per firm, newest call first, at most eight.
    seen_firms: set[str] = set()
    picked = []
    for d in debates:
        if d.firm in seen_firms or not is_deflection(d.answer):
            continue
        seen_firms.add(d.firm)
        picked.append(d)
        if len(picked) >= 8:
            break
    qlines: list[str] = []
    for d in picked:
        qlines.append(f"- {d.firm} ({d.quarter} call): {d.question} [{d.source_id}]")
        if d.answer:
            qlines.append(
                f"  - Management deferred ({d.answered_by or 'management'}): "
                f"{_trim_words(d.answer, 40)} [{d.source_id}]"
            )
    if qlines:
        questions_md = "\n".join(qlines)
    elif debates:
        questions_md = "- every question quoted above drew an answer on the call (see the table)"
    else:
        questions_md = "- no sourced item (no earnings-call transcript was available)"
    claims = list(rating_claims)
    if rating_count is not None and rating_sid:
        c = claim(
            section, "rating_change_count", rating_count, "trailing-12m", [rating_sid], unit="count"
        )
        if c:
            claims.append(c)
    used: list[str] = []
    for p in points:
        if p.source_id not in used:
            used.append(p.source_id)
    for d in debates:
        if d.source_id not in used:
            used.append(d.source_id)
    if rating_sid and rating_sid not in used and rating_count is not None:
        used.append(rating_sid)
    present = bool(bull or bear)
    return ThesisExhibit(md, questions_md, claims, used, points, debates, present)
