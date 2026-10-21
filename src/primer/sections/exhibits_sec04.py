"""Section 4 exhibit builders: program table, agency-mix table, locations tables.

Every cell value carries a citation token for a government record (USAspending page or
award record, SAM.gov entity record, GAO page, DoD announcement page) and every number is a
Claim (requirements §7 program rule). Obligated (USAspending award total), ceiling
(USAspending base_and_all_options) and announced (DoD announcement) are three different
numbers and sit in three columns; the caption says so.
"""

from __future__ import annotations

import re
from typing import Any

from primer.sections.common import claim, md_table
from primer.sections.exhibits import ExhibitResult
from primer.sections.style import fmt_money
from primer.state import Claim

NO_FINDING = "no GAO/SAR finding fetched"
NO_ANNOUNCEMENT = "none fetched"


def slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (s or "").lower())).strip("_")


def cite(sids: list[str] | None) -> str:
    return "".join(f" [{s}]" for s in dict.fromkeys(s for s in (sids or []) if s))


def _money(v: float | None) -> str:
    return "n/a" if v is None else fmt_money(v)


# ---- program table -----------------------------------------------------------------------


def program_table(programs: list[dict[str, Any]], window: str, section: int = 4) -> ExhibitResult:
    """programs: dicts with program, customer, role, vehicle, piid, award_count, obligated,
    obligated_sids, ceiling, ceiling_sid, pop_start, pop_end, pop_sids, status (dict or None:
    text, sids, date, report), announced (dict or None: amount_usd, text, sids, date,
    contract_number), gov_ids (list)."""
    claims: list[Claim] = []
    used: set[str] = set()
    rows: list[list[Any]] = []
    for p in programs:
        s = slug(p["program"])
        obl_sids = list(p.get("obligated_sids") or [])
        used.update(obl_sids)
        claims.append(
            claim(
                section,
                f"obligated_{s}",
                p.get("obligated"),
                window,
                obl_sids,
                note=f"USAspending obligated to date, sum of {p.get('award_count', 1)} prime award(s); "
                f"gov_id: {', '.join(p.get('gov_ids') or [])}",
            )
        )
        ceiling_cell = "n/a"
        if p.get("ceiling") is not None and p.get("ceiling_sid"):
            used.add(p["ceiling_sid"])
            claims.append(
                claim(
                    section,
                    f"ceiling_{s}",
                    p["ceiling"],
                    window,
                    [p["ceiling_sid"]],
                    note=f"USAspending base_and_all_options of award {p.get('ceiling_piid') or p.get('piid')}",
                )
            )
            ceiling_cell = _money(p["ceiling"]) + cite([p["ceiling_sid"]])
        status = p.get("status") or {}
        if status.get("text") and status.get("sids"):
            used.update(status["sids"])
            bits = [status["text"]]
            tail = ", ".join(x for x in (status.get("report"), status.get("date")) if x)
            if tail:
                bits.append(f"({tail})")
            status_cell = " ".join(bits) + cite(status["sids"])
        else:
            status_cell = NO_FINDING
        ann = p.get("announced") or {}
        if ann.get("amount_usd") is not None and ann.get("sids"):
            used.update(ann["sids"])
            claims.append(
                claim(
                    section,
                    f"announced_{s}",
                    ann["amount_usd"],
                    ann.get("date") or window,
                    ann["sids"],
                    note=f"DoD contract announcement; gov_id: {ann.get('contract_number')} {ann.get('date') or ''}".strip(),
                )
            )
            ann_cell = (
                f"${_money(ann['amount_usd'])}m, {ann.get('date') or 'n.d.'}, "
                f"{ann.get('contract_number') or 'no contract number'}" + cite(ann["sids"])
            )
        else:
            ann_cell = NO_ANNOUNCEMENT
        pop_sids = list(p.get("pop_sids") or obl_sids)
        used.update(pop_sids)
        rows.append(
            [
                p["program"],
                str(p.get("customer") or "n/a") + cite(obl_sids),
                p.get("role") or "not confirmed",
                f"{p.get('vehicle') or 'n/a'} {p.get('piid') or ''}".strip() + cite(obl_sids),
                _money(p.get("obligated")) + cite(obl_sids),
                ceiling_cell,
                f"{p.get('pop_start') or 'n.d.'} to {p.get('pop_end') or 'n.d.'}" + cite(pop_sids),
                ann_cell,
                status_cell,
            ]
        )
    headers = [
        "Program",
        "Customer (awarding sub-agency)",
        "Role",
        "Vehicle / largest PIID",
        "Obligated, USD m (USAspending)",
        "Ceiling, USD m (USAspending base+options, award record fetched)",
        "Period of performance",
        "Latest announced award (DoD)",
        "Status / latest GAO-SAR finding",
    ]
    md = md_table(headers, rows, align_right_from=4) if rows else ""
    return ExhibitResult(md, [c for c in claims if c], sorted(used), bool(rows), [])


# ---- agency mix ---------------------------------------------------------------------------


def agency_mix_table(
    mix: dict[int, dict[str, Any]], top_n: int = 5, section: int = 4
) -> ExhibitResult:
    """mix: {fy: {"source_id": sid, "rows": [{name, slug, amount}]}}; FYs with no rows are
    reported in notes and omitted from the columns."""
    claims: list[Claim] = []
    used: set[str] = set()
    notes: list[str] = []
    fys = sorted(mix)
    cols = [fy for fy in fys if mix[fy].get("rows")]
    for fy in fys:
        if fy not in cols:
            notes.append(f"FY{fy}: no USAspending obligations returned for the UEI set")
    if not cols:
        return ExhibitResult("", [], [], False, notes or ["agency mix: no data"])
    order: list[str] = []
    for fy in cols:
        for r in mix[fy]["rows"][:top_n]:
            if r["name"] not in order:
                order.append(r["name"])
    rows: list[list[Any]] = []
    for name in order:
        cells: list[Any] = [name]
        for fy in cols:
            sid = mix[fy].get("source_id")
            hit = next((r for r in mix[fy]["rows"] if r["name"] == name), None)
            if hit is None or not sid:
                cells.append("n/a")
                continue
            used.add(sid)
            claims.append(
                claim(
                    section,
                    f"obligations_{slug(hit.get('slug') or name)}",
                    hit["amount"],
                    f"FY{fy}",
                    [sid],
                    note="USAspending spending_by_category awarding_subagency, prime contract obligations",
                )
            )
            cells.append(_money(hit["amount"]) + cite([sid]))
        rows.append(cells)
    headers = ["Awarding sub-agency", *[f"FY{fy}, USD m" for fy in cols]]
    return ExhibitResult(
        md_table(headers, rows), [c for c in claims if c], sorted(used), True, notes
    )


# ---- locations ----------------------------------------------------------------------------


def locations_table(rows: list[dict[str, Any]], window: str, section: int = 4) -> ExhibitResult:
    """rows: cage, name, city, state, expiry, sam_sid, programs: [{program, sids}]."""
    used: set[str] = set()
    out: list[list[Any]] = []
    for r in rows:
        used.add(r["sam_sid"])
        progs = []
        for p in r.get("programs") or []:
            used.update(p.get("sids") or [])
            progs.append(f"{p['program']}{cite(p.get('sids'))}")
        out.append(
            [
                f"{r.get('cage') or 'n/a'}{cite([r['sam_sid']])}",
                f"{r.get('name') or 'n/a'}{cite([r['sam_sid']])}",
                f"{r.get('city')}, {r.get('state')}{cite([r['sam_sid']])}",
                f"{r.get('expiry') or 'n/a'}{cite([r['sam_sid']])}",
                "; ".join(progs) or "awards at this place of performance name no program",
            ]
        )
    headers = [
        "CAGE",
        "Registered name (SAM.gov)",
        "City, state",
        "Registration expires",
        "Programs performed there (award place of performance)",
    ]
    claims = [
        claim(
            section,
            "active_registration_count",
            len(rows),
            window,
            sorted({r["sam_sid"] for r in rows}),
            unit="count",
            note="active SAM.gov registrations under the parent UEI with award place-of-performance activity",
        )
    ]
    md = md_table(headers, out, align_right_from=len(headers)) if out else ""
    return ExhibitResult(md, [c for c in claims if c and rows], sorted(used), bool(out), [])


def reduced_locations_table(
    rows: list[dict[str, Any]], tenk_sid: str | None, section: int = 4
) -> ExhibitResult:
    """rows: place (city, state or state only), named_in_10k (bool), programs: [{program, sids}]."""
    used: set[str] = set()
    out: list[list[Any]] = []
    for r in rows:
        progs = []
        for p in r.get("programs") or []:
            used.update(p.get("sids") or [])
            progs.append(f"{p['program']}{cite(p.get('sids'))}")
        pop_sids = sorted({s for p in r.get("programs") or [] for s in p.get("sids") or []})
        if r.get("named_in_10k") and tenk_sid:
            used.add(tenk_sid)
            named = f"named in 10-K Item 2{cite([tenk_sid])}"
        else:
            named = "not named in 10-K Item 2"
        out.append(
            [
                f"{r['place']}{cite(pop_sids)}",
                named,
                "; ".join(progs) or "awards name no program",
            ]
        )
    headers = [
        "Place of performance (USAspending)",
        "10-K Item 2 Properties",
        "Programs (awards performed there)",
    ]
    md = md_table(headers, out, align_right_from=len(headers)) if out else ""
    return ExhibitResult(md, [], sorted(used), bool(out), [])
