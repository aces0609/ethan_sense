"""Section 8 exhibits: officer table and ownership-concentration bullets (requirements §6 row 8).

Officer rows come from `text_tools.exec_officer_rows` over 10-K Item 10 and the DEF 14A;
ownership rows from the proxy's beneficial-ownership table (a holder cell, a share-count
cell and a percent cell on one line); board size, independence, CEO pay and say-on-pay
from the proxy sentences that name them. Every parsed number is a Claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from primer.sections.common import claim, md_table
from primer.sections.text_tools import exec_officer_rows
from primer.state import Claim

PCT_CELL = re.compile(r"^(\d{1,2}(?:\.\d{1,2})?)\s?%$")
SHARES_CELL = re.compile(r"^\d{1,3}(?:,\d{3}){1,4}$")
HEADER_WORDS = ("name", "beneficial owner", "percent", "shares", "class")
BOARD_RE = (
    re.compile(r"(\d{1,2}) directors?,?\s+(\d{1,2}) of whom (?:are|is) independent", re.I),
    re.compile(
        r"(\d{1,2}) of (?:our|the) (\d{1,2}) (?:current )?directors (?:are|is) independent", re.I
    ),
    re.compile(
        r"(\d{1,2}) of (?:our|the) (\d{1,2}) director nominees (?:are|is) independent", re.I
    ),
)
MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?", re.I)
PCT_IN_TEXT = re.compile(r"(\d{1,3}(?:\.\d)?)\s?%")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


@dataclass
class Officer:
    name: str
    age: str
    title: str
    source_id: str
    source_label: str


@dataclass
class Holder:
    holder: str
    pct: float
    shares: str | None
    source_id: str
    is_group: bool = False
    voting_pct: float | None = None  # combined voting power, when a dual-class table gives it
    pct_basis: str = "shares"  # "Class A shares" when the table's first percentage is Class A's


@dataclass
class GovernanceFacts:
    board_size: int | None = None
    board_independent: int | None = None
    board_sentence: str | None = None
    ceo_pay_usd: float | None = None
    ceo_pay_year: str | None = None
    ceo_pay_sentence: str | None = None
    say_on_pay_pct: float | None = None
    say_on_pay_year: str | None = None
    say_on_pay_sentence: str | None = None
    voting_power_mentioned: bool = False
    source_id: str | None = None


@dataclass
class GovernanceExhibit:
    officers_md: str
    ownership_md: str
    claims: list[Claim]
    source_ids: list[str]
    officers: list[Officer]
    holders: list[Holder]
    facts: GovernanceFacts
    present: bool
    notes: list[str] = field(default_factory=list)


def officer_rows(
    tenk_text: str, tenk_sid: str | None, proxy_text: str, proxy_sid: str | None
) -> tuple[list[Officer], str]:
    """Officers from the 10-K when it lists at least three, else from the proxy; the other
    document fills in names the chosen one lacks. Returns (rows, basis label)."""
    r10k = exec_officer_rows(tenk_text) if tenk_sid else []
    rpx = exec_officer_rows(proxy_text) if proxy_sid else []
    if len(r10k) >= 3 or not rpx:
        primary, psid, plabel = r10k, tenk_sid, "10-K"
        secondary, ssid, slabel = rpx, proxy_sid, "DEF 14A"
    else:
        primary, psid, plabel = rpx, proxy_sid, "DEF 14A"
        secondary, ssid, slabel = r10k, tenk_sid, "10-K"
    out: list[Officer] = []
    seen: set[str] = set()
    for rows, sid, label in ((primary, psid, plabel), (secondary, ssid, slabel)):
        if not sid:
            continue
        for r in rows:
            key = re.sub(r"[^a-z]", "", r["name"].lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(Officer(r["name"], r["age"], r["title"], sid, label))
    return out, plabel


def ownership_rows(proxy_text: str, proxy_sid: str, min_pct: float = 1.0) -> list[Holder]:
    out: list[Holder] = []
    seen: set[str] = set()
    # a dual-class table prints Class A %, Class B %, then combined voting power, before and after
    # the offering: the third percentage is the voting power (SpaceX: Mr. Musk 12.3%, 93.6%, 85.1%)
    dual_class = "combined voting power" in proxy_text.lower()
    for line in proxy_text.splitlines():
        if "|" not in line or "%" not in line:
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        pct = None
        pcts: list[float] = []
        for c in cells:
            m = PCT_CELL.match(c.replace(" ", ""))
            if m:
                pcts.append(float(m.group(1)))
        if pcts:
            pct = pcts[0]
        if pct is None:
            continue
        name = next(
            (
                c
                for c in cells
                if re.search(r"[A-Za-z]{3}", c)
                and not PCT_CELL.match(c.replace(" ", ""))
                and not SHARES_CELL.match(c)
            ),
            None,
        )
        if not name or any(h in name.lower() for h in HEADER_WORDS):
            continue
        shares = next((c for c in cells if SHARES_CELL.match(c)), None)
        name = re.sub(r"\s*\(\d+\)|\*+", "", name).strip(" .")
        is_group = bool(
            re.search(r"\b(as a group|directors and (?:executive )?officers)\b", name, re.I)
        )
        if pct < min_pct and not is_group:
            continue
        key = re.sub(r"[^a-z]", "", name.lower())
        if key in seen:
            continue
        seen.add(key)
        voting = pcts[2] if dual_class and len(pcts) >= 6 else None
        basis = (
            "Class A shares" if voting is not None and "class a" in proxy_text.lower() else "shares"
        )
        out.append(Holder(name, pct, shares, proxy_sid, is_group, voting, basis))
    return out


def _windows(flat: str, keyword: str, before: int = 120, after: int = 260, limit: int = 12):
    """Text windows around each occurrence of keyword: (text before, text after)."""
    low = flat.lower()
    start = 0
    n = 0
    while n < limit:
        i = low.find(keyword, start)
        if i < 0:
            break
        yield flat[max(0, i - before) : i], flat[i : i + after]
        start = i + len(keyword)
        n += 1


def governance_facts(proxy_text: str, proxy_sid: str | None) -> GovernanceFacts:
    """Board size and independence, CEO total compensation and say-on-pay support from the
    proxy text. Windows around the keyword are used rather than sentence splitting because
    proxy sentences carry abbreviations ('Mr. Taiclet') that break the splitter."""
    f = GovernanceFacts(source_id=proxy_sid)
    if not proxy_sid:
        return f
    flat = " ".join(proxy_text.split())
    for rx in BOARD_RE:
        m = rx.search(flat)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if rx is BOARD_RE[0]:
                f.board_size, f.board_independent = a, b
            else:
                f.board_independent, f.board_size = a, b
            f.board_sentence = flat[max(0, m.start() - 80) : m.end() + 80]
            break
    for before, after in _windows(flat, "total compensation"):
        m = MONEY_RE.search(after)
        if not m:
            continue
        v = float(m.group(1).replace(",", ""))
        scale = {"million": 1e6, "billion": 1e9}.get((m.group(2) or "").lower(), 1.0)
        if v * scale < 1e5:
            continue
        f.ceo_pay_usd, f.ceo_pay_sentence = v * scale, (before + after)[:400]
        y = YEAR_RE.search(after[: m.start()]) or YEAR_RE.search(before)
        f.ceo_pay_year = y.group(1) if y else None
        break
    for kw in ("say-on-pay", "say on pay"):
        for before, after in _windows(flat, kw, before=60, after=160):
            m = PCT_IN_TEXT.search(after)
            if m and float(m.group(1)) <= 100:
                f.say_on_pay_pct, f.say_on_pay_sentence = float(m.group(1)), (before + after)[:300]
                y = YEAR_RE.search(before + after[: m.start()])
                f.say_on_pay_year = y.group(1) if y else None
                break
        if f.say_on_pay_pct is not None:
            break
    f.voting_power_mentioned = "voting power" in flat.lower()
    return f


def governance_exhibit(
    *,
    officers: list[Officer],
    holders: list[Holder],
    facts: GovernanceFacts,
    filing_dates: dict[str, str],
    section: int = 8,
) -> GovernanceExhibit:
    claims: list[Claim] = []
    used: list[str] = []
    notes: list[str] = []

    rows = []
    for o in officers:
        c = f" [{o.source_id}]"
        when = filing_dates.get(o.source_id, "n.d.")
        rows.append([o.name + c, o.age + c, o.title + c, f"{o.source_label} filed {when}{c}"])
        if o.source_id not in used:
            used.append(o.source_id)
    officers_md = (
        md_table(["Name", "Age", "Title", "Source"], rows, align_right_from=99) if rows else ""
    )

    bullets = []
    period = filing_dates.get(facts.source_id or "", "n.d.")
    for h in holders:
        c = f" [{h.source_id}]"
        shares = f" ({h.shares} shares)" if h.shares else ""
        label = "directors and executive officers as a group" if h.is_group else h.holder
        voting = (
            f" and {h.voting_pct:.1f}% of combined voting power" if h.voting_pct is not None else ""
        )
        bullets.append(
            f"- {label}: {h.pct:.1f}% of {h.pct_basis} beneficially owned{shares}{voting}{c}"
        )
        slug = re.sub(r"[^a-z0-9]+", "_", h.holder.lower()).strip("_")[:40]
        if h.is_group:
            slug = "directors_and_officers_group"
        k = claim(
            section,
            f"ownership_pct_{slug}",
            h.pct,
            period,
            [h.source_id],
            unit="pct",
            note="beneficial ownership per the proxy table",
        )
        if k:
            claims.append(k)
        if h.voting_pct is not None:
            kv = claim(
                section,
                f"voting_power_pct_{slug}",
                h.voting_pct,
                period,
                [h.source_id],
                unit="pct",
                note="combined voting power per the ownership table",
            )
            if kv:
                claims.append(kv)
        if h.source_id not in used:
            used.append(h.source_id)
    if bullets and facts.voting_power_mentioned:
        bullets.append(
            "- Percentages are beneficial ownership as reported in the proxy table; the filing "
            "separately describes voting power, which is not the same measure"
            + (f" [{facts.source_id}]" if facts.source_id else "")
        )
    ownership_md = "\n".join(bullets)

    sid = facts.source_id
    if sid:
        if facts.board_size is not None:
            k = claim(section, "board_size", facts.board_size, period, [sid], unit="count")
            if k:
                claims.append(k)
        if facts.board_independent is not None:
            k = claim(
                section,
                "board_independent_count",
                facts.board_independent,
                period,
                [sid],
                unit="count",
            )
            if k:
                claims.append(k)
        if facts.ceo_pay_usd is not None:
            k = claim(
                section,
                "ceo_total_compensation",
                facts.ceo_pay_usd,
                f"FY{facts.ceo_pay_year}" if facts.ceo_pay_year else period,
                [sid],
                unit="USD",
                note="total compensation as stated in the proxy",
            )
            if k:
                claims.append(k)
        if facts.say_on_pay_pct is not None:
            k = claim(
                section,
                "say_on_pay_support_pct",
                facts.say_on_pay_pct,
                f"FY{facts.say_on_pay_year}" if facts.say_on_pay_year else period,
                [sid],
                unit="pct",
            )
            if k:
                claims.append(k)
        if claims and sid not in used:
            used.append(sid)
    if not rows:
        notes.append("no executive-officer table parsed from the 10-K Item 10 or the proxy")
    if not holders:
        notes.append("no beneficial-ownership table parsed from the proxy")
    return GovernanceExhibit(
        officers_md=officers_md,
        ownership_md=ownership_md,
        claims=claims,
        source_ids=used,
        officers=officers,
        holders=holders,
        facts=facts,
        present=bool(rows),
        notes=notes,
    )
