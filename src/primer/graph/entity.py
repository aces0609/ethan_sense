"""Entity-resolution helpers behind `resolve_entity` (PLAN 3.4 private path, 3.8 ambiguity).

Pure functions over the EDGAR client, so each rule is testable without the graph:

- `name_hits` searches the SEC ticker file by name, retrying once without the legal suffix
  ("Acme Orbital Inc" -> "acme orbital"), so a missing suffix match does not send a listed
  company down the private path.
- `needs_choice` is the ambiguity rule: two or more hits where the best score is under twice
  the second (the old rule raised ValueError here; the graph now interrupts instead).
- `candidates_payload` is the interrupt payload and `resolve_choice` turns the answer (a CIK,
  a ticker, a candidate dict or "private") back into a match.
- `private_entity` builds the private-path Entity: no CIK, aliases from the name, sector tag
  "default".
"""

from __future__ import annotations

import re
from typing import Any

from primer.clients.edgar import EXCHANGE_TICKERS_URL, EdgarClient, pad_cik
from primer.graph.interrupts import ENTITY_CHOICE, PRIVATE_ANSWER
from primer.sections.text_tools import norm_company
from primer.state import Entity, Inputs

CANDIDATE_LIMIT = 5
LEGAL_SUFFIX = re.compile(
    r"\b(corp|corporation|inc|incorporated|co|company|ltd|plc|holdings?)\b\.?", re.I
)
PRIVATE_WORDS = {"private", "none", "0", "no", "n"}


def short_name(name: str) -> str:
    """The name without legal suffixes ('Lockheed Martin Corp' -> 'Lockheed Martin')."""
    return re.sub(r"\s+", " ", LEGAL_SUFFIX.sub("", name)).strip(" ,")


def name_hits(edgar: EdgarClient, name: str) -> list[dict[str, Any]]:
    """SEC ticker-file matches for a company name, best score first."""
    hits = edgar.search_company_name(name)
    if not hits:
        stripped = norm_company(name)
        if stripped and stripped != re.sub(r"[^a-z0-9 ]", "", name.lower()).strip():
            hits = edgar.search_company_name(stripped)
    return hits


def needs_choice(hits: list[dict[str, Any]]) -> bool:
    return len(hits) > 1 and hits[0]["score"] < 2 * hits[1]["score"]


def candidates_payload(hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": ENTITY_CHOICE,
        "candidates": [
            {
                "cik": h["cik"],
                "ticker": h.get("ticker"),
                "title": h.get("title"),
                "exchange": h.get("exchange"),
                "score": round(float(h.get("score", 0.0)), 4),
            }
            for h in hits[:CANDIDATE_LIMIT]
        ],
    }


def lookup_cik(edgar: EdgarClient, cik: str) -> dict[str, Any]:
    """Ticker-file row for a CIK; a bare {"cik": ...} when the CIK is not listed."""
    want = pad_cik(cik)
    data = edgar._get(EXCHANGE_TICKERS_URL)
    fields = data.get("fields", [])
    for row in data.get("data", []):
        rec = dict(zip(fields, row, strict=False))
        if pad_cik(rec["cik"]) == want:
            return {
                "cik": want,
                "ticker": rec.get("ticker"),
                "title": rec.get("name"),
                "exchange": rec.get("exchange"),
            }
    return {"cik": want, "ticker": None, "title": None, "exchange": None}


def resolve_choice(
    answer: Any, hits: list[dict[str, Any]], edgar: EdgarClient
) -> dict[str, Any] | None:
    """The match chosen by an interrupt answer; None means 'private path'.

    A CIK or ticker among the candidates picks that candidate; one outside them is looked up
    in the ticker file. Anything else is an error, never a silent default."""
    if isinstance(answer, dict):
        answer = answer.get("cik") or answer.get("ticker")
    text = str(answer if answer is not None else "").strip()
    if not text:
        raise ValueError("empty answer to the entity choice; pass a CIK, a ticker or 'private'")
    if text.lower() in PRIVATE_WORDS or text.lower() == PRIVATE_ANSWER:
        return None
    if text.isdigit():
        cik = pad_cik(text)
        for h in hits:
            if h["cik"] == cik:
                return h
        return lookup_cik(edgar, cik)
    for h in hits:
        if str(h.get("ticker", "")).upper() == text.upper():
            return h
    found = edgar.resolve_ticker(text)
    if found:
        return found
    raise ValueError(f"{answer!r} is neither a candidate CIK nor a ticker in the SEC file")


def private_entity(inputs: Inputs) -> Entity:
    """Entity for a company with no SEC filings: no CIK or ticker, aliases from the name,
    sector tag "default" (the planner can refine it)."""
    short = short_name(inputs.company_name)
    aliases = {short} if short and short != inputs.company_name else set()
    return Entity(
        name=inputs.company_name,
        ticker=None,
        cik=None,
        is_public=False,
        sector_tag="default",
        aliases=sorted(aliases),
        private=True,
    )


def sec_match_for_record(
    edgar: EdgarClient, inputs: Inputs, runlog: Any = None
) -> dict[str, Any] | None:
    """The SEC match `--private` overrides, for run.json. Reads only the ticker file (never
    submissions or companyfacts); a failed read is a note, not an error."""
    try:
        if inputs.ticker:
            return edgar.resolve_ticker(inputs.ticker)
        if inputs.cik:
            return lookup_cik(edgar, inputs.cik)
        hits = name_hits(edgar, inputs.company_name)
        return hits[0] if hits else None
    except Exception as e:  # noqa: BLE001 - the record is informational
        if runlog is not None:
            runlog.error(f"resolve_entity: could not read the SEC ticker file for --private: {e!r}")
        return None


def apply_sec_match(
    ent: Entity, match: dict[str, Any], subs: dict[str, Any], given_name: str
) -> Entity:
    """Fill an Entity from a ticker-file match and the submissions document."""
    from primer.config import sector_for_sic

    ent.cik = match["cik"]
    ent.ticker = ent.ticker or match.get("ticker")
    ent.name = subs.get("name") or match.get("title") or given_name
    ent.is_public = True
    ent.exchange = match.get("exchange") or (subs.get("exchanges") or [None])[0]
    ent.fiscal_year_end = subs.get("fiscalYearEnd") or None
    ent.sic = str(subs.get("sic") or "") or None
    ent.state_of_incorporation = subs.get("stateOfIncorporation") or None
    ent.sector_tag = sector_for_sic(ent.sic)
    aliases = {given_name}
    for fn in subs.get("formerNames", []) or []:
        if fn.get("name"):
            aliases.add(fn["name"])
    aliases.add(short_name(ent.name))
    ent.aliases = sorted(a for a in aliases if a and a != ent.name)
    return ent
