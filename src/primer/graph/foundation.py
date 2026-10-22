"""foundation_pack (requirements §5 node 3, PLAN 2.1): fetch once, share with every researcher.

Contents, all registered in the evidence store before any model sees them:
- latest 10-K, latest two 10-Qs, latest DEF 14A (EDGAR primary documents, text-extracted,
  chunked by Item heading on demand via ToolContext.chunks)
- 8-K index for the trailing 12 months (refs only; section 14 fetches what it cites)
- last 4 earnings releases: the EX-99 exhibit of each Item 2.02 8-K (tier 1, sec.gov)
- last 4 earnings-call transcripts: Motley Fool first, then the other transcript hosts in
  config/sources.yaml, via Firecrawl /search + /scrape; an IR webcast PDF via /parse when
  a search hit is a PDF on the company's own site. Misses are recorded in run.json
  (`foundation.transcripts.missed`) because a miss rate over 20% is the trigger for the
  FMP transcript upgrade (requirements §4).
- IR site map via Firecrawl /map (presentations, news, events links) for sections 6 and 14
- the fiscal-year label, the section-4 gate (does the 10-K disclose US-government
  revenue?) and the peer list parsed from the 10-K competition paragraphs (sections 5, 13)

The dict returned is small (handles and labels); researchers read text from the store.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from primer.clients.edgar import (
    EdgarClient,
    describe_fiscal_year_end,
    fiscal_year_label,
    trailing_months,
)
from primer.clients.firecrawl import FirecrawlClient, FirecrawlError
from primer.config import foundation_budget, sources_config
from primer.evidence.chunking import chunk_by_item
from primer.evidence.html import html_to_text
from primer.evidence.store import EvidenceStore
from primer.runlog import RunLog
from primer.sections.text_tools import (
    competitor_candidates,
    gov_revenue_disclosure,
    norm_company,
)
from primer.state import Entity, FilingRef, Inputs, PrimerState

FORM_TYPES = {"10-K": "10-K", "10-Q": "10-Q", "DEF 14A": "DEF14A"}
WEBSITE_RE = re.compile(r"\b(?:https?://)?(www\.[a-z0-9\-]+\.(?:com|net|org|aero))\b", re.I)


def prev_quarter_end(d: date) -> date:
    """Last calendar-quarter end strictly before d."""
    q_end_month = ((d.month - 1) // 3) * 3  # 0,3,6,9 -> previous quarter's end month
    if q_end_month == 0:
        return date(d.year - 1, 12, 31)
    last_day = {3: 31, 6: 30, 9: 30}[q_end_month]
    return date(d.year, q_end_month, last_day)


def quarter_label_for_release(report_date: date) -> str:
    qe = prev_quarter_end(report_date)
    return f"Q{(qe.month - 1) // 3 + 1}-{qe.year}"


def _register_filing(
    edgar: EdgarClient, store: EvidenceStore, ent: Entity, ref: FilingRef, kind: str, title: str
) -> str | None:
    if not ref.primary_document:
        return None
    html = edgar.get_text(ref.url)
    text = html_to_text(html)
    rec = store.register(
        text,
        type=FORM_TYPES.get(kind, kind),
        publisher=ent.name,
        title=title,
        url=ref.url,
        published_date=ref.filing_date,
        tier=1,
        gov_id=ref.accession,
        meta={
            "form": ref.form,
            "cik": ent.cik,
            "chars": len(text),
            "report_date": str(ref.report_date),
        },
        dedupe_key=f"filing|{ref.accession}",
    )
    return rec.source_id


def resolve_peers(edgar: EdgarClient, candidates: list[str], self_cik: str | None, limit: int = 6):
    """Match competitor names from the 10-K against the SEC ticker file; exact match on the
    suffix-stripped name, or a one-word-longer title for multi-word names."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in candidates:
        n = norm_company(name)
        if not n or len(n) < 3:
            continue
        hits = edgar.search_company_name(n, limit=5)
        for h in hits:
            t = norm_company(h["title"])
            if h["cik"] == self_cik or h["cik"] in seen:
                continue
            generic_first = n.split()[0] in {
                "north",
                "south",
                "east",
                "west",
                "american",
                "united",
                "national",
                "international",
                "global",
                "new",
                "general",
                "the",
            }
            ok = t == n or (
                not generic_first
                and len(n.split()) >= 2
                and t.startswith(n)
                and len(t.split()) <= len(n.split()) + 1
            )
            if ok:
                seen.add(h["cik"])
                out.append(
                    {"name": h["title"], "ticker": h["ticker"], "cik": h["cik"], "mention": name}
                )
                break
        if len(out) >= limit:
            break
    return out


def unresolved_peers(candidates: list[str], resolved: list[dict], limit: int = 3) -> list[dict]:
    """Rivals the 10-K names that have no SEC ticker (Airbus, BAE): kept as peers without a
    CIK so the peer table can list them as 'named in the 10-K; no SEC filings'. Only names
    that appear at least twice (or are multi-word) qualify."""
    taken = {norm_company(r.get("mention", "")) for r in resolved}
    taken |= {norm_company(r.get("name", "")) for r in resolved}
    counts: dict[str, int] = {}
    for c in candidates:
        n = norm_company(c)
        if not n or n in taken or n.split()[0] in {"north", "south", "united", "the", "general"}:
            continue
        counts[c] = counts.get(c, 0) + 1
    out = []
    for name, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
        if cnt < 2 and len(name.split()) < 2:
            continue
        if len(name) < 4:
            continue
        out.append(
            {"name": name, "ticker": None, "cik": None, "mention": name, "note": "no SEC filings"}
        )
        if len(out) >= limit:
            break
    return out


def _company_site(tenk_text: str) -> str | None:
    m = WEBSITE_RE.search(tenk_text[:60000])
    if not m:
        return None
    host = m.group(1).lower()
    if "sec.gov" in host:
        return None
    return host


def _classify_links(links: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"presentations": [], "news": [], "events": [], "other": []}
    for u in links:
        low = u.lower()
        if low.endswith(".pdf") or any(
            k in low for k in ("presentation", "investor-day", "slides", "deck")
        ):
            out["presentations"].append(u)
        elif any(k in low for k in ("news", "press", "release")):
            out["news"].append(u)
        elif any(k in low for k in ("event", "webcast", "earnings")):
            out["events"].append(u)
        else:
            out["other"].append(u)
    return out


_CORP_WORDS = re.compile(
    r"\b(corp|corporation|inc|incorporated|co|company|ltd|plc|holdings?|group|the|de)\b",
    re.I,
)


def company_tokens(ent: Entity) -> tuple[list[str], str | None]:
    """(name tokens, ticker) that identify the company in a title, URL or page: each name or
    alias with legal suffixes and punctuation removed, spaces squeezed out ("L3HARRIS
    TECHNOLOGIES, INC. /DE/" -> "l3harristechnologies" and "l3harris"), kept when 4+ characters;
    a token is also the name's first word when that word is distinctive (6+ characters)."""
    toks: set[str] = set()
    for n in [ent.name, *ent.aliases]:
        cleaned = _CORP_WORDS.sub(" ", re.sub(r"[^A-Za-z0-9 ]", " ", n or ""))
        words = [w for w in cleaned.split() if w]
        compact = "".join(words).lower()
        if len(compact) >= 4:
            toks.add(compact)
        if words and len(words[0]) >= 6:
            toks.add(words[0].lower())
    return sorted(toks), (ent.ticker or "").lower() or None


def names_company(text: str, ent: Entity) -> bool:
    """True when `text` (a title, URL or the head of a page) names the company: a name token as a
    substring of the text with spaces and punctuation removed, or the ticker as a whole word.
    A transcript search for L3Harris's Q2 2026 call returned Deere's, and it was accepted
    (2026-10-22): the picker checked the host, the quarter and the year but never the company."""
    toks, ticker = company_tokens(ent)
    squeezed = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    if any(t in squeezed for t in toks):
        return True
    if ticker and re.search(rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])", (text or "").lower()):
        return True
    return False


def _pick_transcript(
    hits: list[dict[str, Any]], hosts: list[str], qn: str, yr: str, ent: Entity | None = None
):
    """First hit on a transcript host (priority order) whose title names the quarter and year
    and, when `ent` is given, the company."""
    for host in hosts:
        fallback = None
        for h in hits:
            dom = h.get("domain", "")
            if not (dom == host or dom.endswith("." + host)):
                continue
            if ent is not None and not names_company(
                f"{h.get('title') or ''} {h.get('url') or ''}", ent
            ):
                continue
            title = (h.get("title") or "").lower().replace(" ", "")
            url = h.get("url", "").lower()
            if "transcript" not in title and "transcript" not in url:
                continue
            if yr not in title and yr not in url:
                continue
            if qn.lower() in title or qn.lower() in url:
                return h
            fallback = fallback or h
        if fallback:
            return fallback
    return None


def build_foundation(state: PrimerState) -> dict[str, Any]:
    inputs = Inputs.model_validate(state["inputs"])
    ent = Entity.model_validate(state["entity"])
    runlog = get_runlog(state["run_dir"])
    as_of = date.fromisoformat(state["as_of"])
    store = EvidenceStore.for_run(state["run_dir"], as_of=as_of)
    found: dict[str, Any] = {
        "offline": inputs.offline,
        "filings": {},
        "sources": {},
        "chunks": {},
        "quarters": [],
        "transcripts": {"found": 0, "missed": [], "requested": 0},
        "ir": {},
        "fy": None,
        "fiscal_year_end": None,
        "gov_revenue": {},
        "peers": [],
    }
    if ent.private:
        return {"foundation": _private_foundation(inputs, ent, store, as_of, found, runlog)}
    if not ent.cik:
        found["note"] = "not an SEC filer; foundation pack empty"
        runlog.meta["foundation"] = {"note": found["note"]}
        return {"foundation": found}

    edgar = EdgarClient(runlog=runlog, offline=inputs.offline)
    with runlog.timed("foundation_pack"):
        _filings(edgar, store, ent, as_of, found, runlog)
        _releases(edgar, store, ent, as_of, found, runlog)
        _transcripts_and_ir(store, ent, as_of, found, runlog, inputs.offline)
    runlog.meta["foundation"] = {
        "filings": {k: len(v) for k, v in found["filings"].items()},
        "sources": {k: len(v) for k, v in found["sources"].items()},
        "quarters": [q["label"] for q in found["quarters"]],
        "transcripts": found["transcripts"],
        "ir": {k: len(v) for k, v in found["ir"].get("links", {}).items()}
        | {"site": found["ir"].get("site")},
        "fy": found["fy"],
        "gov_revenue": found["gov_revenue"],
        "peers": [p["ticker"] for p in found["peers"]],
    }
    runlog.write()
    return {"foundation": found}


def _private_foundation(inputs, ent, store, as_of, found, runlog) -> dict[str, Any]:
    """Foundation pack of a company with no SEC filings (PLAN 3.4): no 10-K, 10-Q or proxy and
    no SEC filing call except the Form D search. Private-placement filings go under the
    `private` key, the company site map under `ir`, press pages under `sources`."""
    from primer.sections.private import build_private_foundation

    found["note"] = "private company: no 10-K, 10-Q or proxy; Form D, company site and press only"
    with runlog.timed("foundation_pack"):
        try:
            build_private_foundation(inputs, ent, store, as_of, found, runlog, _classify_links)
        except Exception as e:  # noqa: BLE001 - a failed discovery is a coverage note
            runlog.error(f"private foundation: {e!r}"[:300])
            found.setdefault("private", {"notes": []})["notes"].append(
                f"private foundation pack incomplete: {e!r}"[:200]
            )
    priv = found.get("private") or {}
    fd = priv.get("form_d") or {}
    runlog.meta["foundation"] = {
        "note": found["note"],
        "private": {
            "form_d": {
                "source": fd.get("source"),
                "filings": len(fd.get("filings") or []),
                "failures": fd.get("failures", 0),
                "queries": fd.get("queries", []),
            },
            "site": priv.get("site"),
            "press_hits": len(priv.get("press") or []),
            "notes": priv.get("notes", []),
        },
        "sources": {k: len(v) for k, v in found["sources"].items()},
    }
    runlog.write()
    return found


def _filings(edgar, store, ent, as_of, found, runlog) -> None:
    want = {"10-K": 1, "10-Q": 2, "DEF 14A": 1}
    for form, n in want.items():
        refs = [
            r for r in edgar.filings(ent.cik, forms=[form], limit=n + 4) if r.filing_date <= as_of
        ][:n]
        found["filings"][form] = [r.model_dump(mode="json") for r in refs]
        sids: list[str] = []
        for r in refs:
            try:
                title = {
                    "10-K": f"Annual Report (10-K) for fiscal year ended {r.report_date}",
                    "10-Q": f"Quarterly Report (10-Q) for quarter ended {r.report_date}",
                    "DEF 14A": f"Proxy Statement (DEF 14A) filed {r.filing_date}",
                }[form]
                sid = _register_filing(edgar, store, ent, r, form, title)
                if sid:
                    sids.append(sid)
            except Exception as e:  # one bad document does not sink the pack
                runlog.error(f"foundation {form} fetch {r.accession}: {e!r}")
        found["sources"][form] = sids
        if form in ("10-K", "10-Q") and sids:
            found["chunks"][form] = []
            for sid in sids:
                chunks = chunk_by_item(store.text(sid))
                found["chunks"][form].append(
                    {
                        "source_id": sid,
                        "items": [
                            {"item": c.item, "title": c.title[:80], "chars": len(c.text)}
                            for c in chunks
                        ],
                    }
                )
    eightks = [
        r
        for r in edgar.filings(ent.cik, forms=["8-K"], since=trailing_months(as_of, 15))
        if r.filing_date <= as_of
    ]
    found["filings"]["8-K"] = [r.model_dump(mode="json") for r in eightks]
    edgar.companyfacts(ent.cik)  # warm the cache once for every researcher

    tenk = found["filings"]["10-K"][0] if found["filings"]["10-K"] else None
    # the fiscal calendar comes from the facts; EDGAR's declared year-end month is only the
    # fallback for a filing with no fiscal-year tag
    try:
        calendar = edgar.fiscal_calendar(ent.cik, as_of, ent.fiscal_year_end)
    except Exception as e:  # noqa: BLE001 - the label degrades to the month rule
        runlog.error(f"foundation fiscal calendar: {e!r}")
        calendar = None
    if calendar is not None and calendar.ends:
        found["fiscal_year_end"] = describe_fiscal_year_end(calendar.ends[-4:])
    if tenk and tenk.get("report_date"):
        report_date = date.fromisoformat(tenk["report_date"])
        period_end = calendar.instant_period(report_date) if calendar is not None else None
        if calendar is not None and period_end is not None:
            found["fy"] = calendar.label(period_end)
        else:
            fye_month = int((ent.fiscal_year_end or "1231")[:2])
            found["fy"] = fiscal_year_label(report_date, fye_month)
    else:
        found["fy"] = as_of.year - 1
    if found["sources"].get("10-K"):
        text = store.text(found["sources"]["10-K"][0])
        disclosed, sentence, pct = gov_revenue_disclosure(text)
        found["gov_revenue"] = {
            "disclosed": disclosed,
            "sentence": sentence,
            "pct": pct,
            "source_id": found["sources"]["10-K"][0],
        }
        item1 = next((c.text for c in chunk_by_item(text) if c.item == "1"), text[:200000])
        try:
            cands = competitor_candidates(item1, ent.name)
            found["peers"] = resolve_peers(edgar, cands, ent.cik)
            found["peers"] += unresolved_peers(cands, found["peers"])
        except Exception as e:
            runlog.error(f"foundation peers: {e!r}")
        found["ir"]["site"] = _company_site(text)


def _releases(edgar, store, ent, as_of, found, runlog) -> None:
    """Earnings releases: EX-99 exhibits of Item 2.02 8-Ks, newest first, up to four."""
    quarters: list[dict[str, Any]] = []
    for ref in found["filings"].get("8-K", []):
        if len(quarters) >= 4:
            break
        if "2.02" not in (ref.get("items") or ""):
            continue
        rdate = date.fromisoformat(ref.get("report_date") or ref["filing_date"])
        label = quarter_label_for_release(rdate)
        if any(q["label"] == label for q in quarters):
            continue
        q = {
            "label": label,
            "report_date": rdate.isoformat(),
            "accession": ref["accession"],
            "release_sid": None,
            "transcript_sid": None,
        }
        try:
            url = edgar.exhibit_url(ent.cik, ref["accession"])
            if url:
                text = html_to_text(edgar.get_text(url))
                rec = store.register(
                    text,
                    type="earnings_release",
                    publisher=ent.name,
                    title=f"Earnings release {label.replace('-', ' ')} (8-K exhibit 99, filed {ref['filing_date']})",
                    url=url,
                    published_date=date.fromisoformat(ref["filing_date"]),
                    tier=1,
                    gov_id=ref["accession"],
                    meta={"form": "8-K EX-99", "quarter": label, "cik": ent.cik},
                    dedupe_key=f"release|{ref['accession']}",
                )
                q["release_sid"] = rec.source_id
        except Exception as e:
            runlog.error(f"foundation release {ref['accession']}: {e!r}")
        quarters.append(q)
    found["quarters"] = quarters
    found["sources"]["earnings_release"] = [q["release_sid"] for q in quarters if q["release_sid"]]


def _transcripts_and_ir(store, ent, as_of, found, runlog, offline: bool) -> None:
    budget = foundation_budget()
    fc = FirecrawlClient(
        store, runlog=runlog, credit_cap=int(budget.get("credits", 30)), offline=offline
    )
    found["sources"]["transcript"] = []
    found["sources"]["ir_map"] = []
    if not fc.available:
        found["transcripts"] = {
            "found": 0,
            "missed": [q["label"] for q in found["quarters"]],
            "requested": len(found["quarters"]),
            "note": "Firecrawl not configured",
        }
        return
    hosts = list(sources_config().get("transcript_hosts", ["fool.com"]))
    co = ent.aliases[0] if ent.aliases else ent.name
    site = found["ir"].get("site")
    searches = scrapes = 0
    missed: list[str] = []
    for q in found["quarters"]:
        if searches >= int(budget.get("searches", 5)):
            missed.append(q["label"])
            continue
        qn, yr = q["label"].split("-")
        chosen = None
        for query in (
            f"{co} {qn} {yr} earnings call transcript",
            f"{co} {qn} {yr} earnings call transcript Motley Fool",
        ):
            if searches >= int(budget.get("searches", 5)):
                break
            try:
                hits = fc.search(query, limit=6, section_hint="foundation")
                searches += 1
            except FirecrawlError as e:
                runlog.error(f"foundation transcript search {q['label']}: {e}")
                break
            chosen = _pick_transcript(hits, hosts, qn, yr, ent)
            if chosen:
                break
        if chosen is None and site:
            for h in hits:
                if site.replace("www.", "") in h.get("domain", "") and h["url"].lower().endswith(
                    ".pdf"
                ):
                    chosen = h
                    break
        if chosen is None or scrapes >= int(budget.get("scrapes", 10)):
            missed.append(q["label"])
            continue
        try:
            if chosen["url"].lower().endswith(".pdf"):
                res = fc.parse(chosen["url"], type="earnings_call", section_hint="foundation")
            else:
                res = fc.scrape(chosen["url"], type="earnings_call", section_hint="foundation")
            scrapes += 1
            sid = res.get("source_id")
            head = (res.get("markdown") or "")[:6000]
            if sid and len(res.get("markdown", "")) > 2000 and not names_company(head, ent):
                runlog.error(
                    f"foundation transcript {q['label']}: {chosen['url']} does not name "
                    f"{ent.name}; not used"
                )
                missed.append(q["label"])
            elif sid and len(res.get("markdown", "")) > 2000:
                q["transcript_sid"] = sid
                rec = store.get(sid)
                rec.meta["quarter"] = q["label"]
                found["sources"]["transcript"].append(sid)
            else:
                missed.append(q["label"])
        except FirecrawlError as e:
            runlog.error(f"foundation transcript scrape {q['label']}: {e}")
            missed.append(q["label"])
    found["transcripts"] = {
        "found": len(found["sources"]["transcript"]),
        "missed": missed,
        "requested": len(found["quarters"]),
        "miss_rate": round(len(missed) / len(found["quarters"]), 2) if found["quarters"] else None,
    }
    # IR site map
    links: list[str] = []
    if site:
        bare = site.replace("www.", "")
        for cand in (f"https://investors.{bare}", f"https://{site}/investors"):
            try:
                got = fc.map(cand, search="investor presentation news events", limit=150)
            except FirecrawlError as e:
                runlog.error(f"foundation ir map {cand}: {e}")
                continue
            if got:
                links = got
                found["ir"]["root"] = cand
                break
    if links:
        classified = _classify_links(links)
        found["ir"]["links"] = classified
        rec = store.register(
            f"# IR site map for {ent.name}\n\nRoot: {found['ir'].get('root')}\n\n"
            + "\n".join(f"- {u}" for u in links),
            type="web",
            publisher=urlparse(found["ir"]["root"]).netloc,
            title=f"Investor relations site map ({len(links)} links)",
            url=found["ir"]["root"],
            published_date=as_of,
            tier=2,
            meta={"ir_map": True, "counts": {k: len(v) for k, v in classified.items()}},
            dedupe_key=f"ir_map|{found['ir']['root']}|{as_of.isoformat()}",
        )
        found["sources"]["ir_map"] = [rec.source_id]
    else:
        found["ir"]["links"] = {"presentations": [], "news": [], "events": [], "other": []}


def get_runlog(run_dir: str) -> RunLog:
    from primer.runlog import get_runlog as _g

    return _g(run_dir)


__all__ = ["build_foundation", "quarter_label_for_release", "prev_quarter_end", "resolve_peers"]
