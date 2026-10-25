"""Section 15, Federal Contract Awards by Agency: what each awarding agency obligated to the company
over the last 12 months, 3 years and 5 years, and the largest contracts behind each total.

plan   -> the calls the collector will make (UEI discovery, the SAM.gov awards pull, and the
          USAspending fallback), recorded for run.json.
collect-> resolves the company's UEIs from USAspending (cached, the same call section 4 makes),
          then pulls every contract action signed in the five-year window from SAM.gov's Contract
          Awards API: page by page when the quota allows, through the asynchronous JSON extract
          when it does not. A partial pull is never summed. When SAM.gov cannot supply the whole
          window (no key, the daily quota spent, an error), the same figures come from USAspending,
          which carries the same Federal Procurement Data System actions, and the section says so.
draft  -> two tables (obligations by agency and window; the largest contracts of the top agencies),
          a claim for every figure, and a short narrative (the model when configured, else a
          template digest).

The figures are net obligations by the date an action was signed. DoD actions signed in the last
90 days are not public on either source.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from primer.clients.samgov import SamQuotaError
from primer.config import section_budget
from primer.sections import common
from primer.sections.awards import (
    WINDOWS,
    AgencyRow,
    Contract,
    by_agency,
    from_usaspending,
    parse_actions,
    top_contracts,
    total_row,
    window_start,
)
from primer.sections.common import ToolContext
from primer.sections.sec04_programs import _discovery_names, _window, resolve_uei_set
from primer.state import Brief, Claim, SectionDraft

SECTION = 15
TABLE_AGENCIES = 12  # rows in the agency table; the rest are summed into one "other" row
CONTRACT_AGENCIES = 8  # agencies whose largest contracts are listed
PER_AGENCY = 3
EVIDENCE_ACTIONS = 3000  # actions kept in the evidence record (the largest by amount)
WINDOW_LABEL = {1: "Last 12 months", 3: "Last 3 years", 5: "Last 5 years"}
WINDOW_PERIOD = {1: "12m", 3: "3y", 5: "5y"}


# ---- formatting ---------------------------------------------------------------------------


def _m(v: float | None) -> str:
    """USD millions: one decimal below 100, none above, parentheses never (a de-obligation is -)."""
    if v is None:
        return "n/a"
    x = v / 1e6
    text = f"{x:,.1f}" if abs(x) < 100 else f"{x:,.0f}"
    return "0.0" if text == "-0.0" else text


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "agency"


def _d(v: date | None) -> str:
    return v.isoformat() if v else "n/a"


# ---- plan ---------------------------------------------------------------------------------


def plan(brief: Brief, ctx: ToolContext) -> list[dict[str, Any]]:
    start5 = window_start(ctx.as_of, 5)
    lo, hi = _window(ctx)
    return [
        {
            "tool": "usaspending.spending_by_award",
            "args": {"names": _discovery_names(ctx), "fy_from": lo, "fy_to": hi},
            "why": "the company's UEIs (the same cached call section 4 makes)",
        },
        {
            "tool": "samgov.contract_awards",
            "args": {"signed_from": start5.isoformat(), "signed_to": ctx.as_of.isoformat()},
            "why": "every contract action signed in the five-year window, by awarding agency",
        },
        {
            "tool": "usaspending.obligations_by_category",
            "args": {"category": "awarding_subagency", "windows": list(WINDOWS)},
            "why": "fallback when SAM.gov cannot supply the whole window",
        },
    ]


# ---- collect ------------------------------------------------------------------------------


def _register_sam_evidence(
    ctx: ToolContext, ueis: list[str], actions: list, start: date, pull: dict[str, Any]
) -> str | None:
    """One source record for the pull: the query, and the actions as compact rows (the largest
    by amount when there are more than the cap), so every figure in the tables can be re-added."""
    ranked = sorted(actions, key=lambda a: -abs(a.obligation))
    rows = [
        [a.piid, a.modification, a.signed.isoformat(), a.label, round(a.obligation, 2)]
        for a in ranked[:EVIDENCE_ACTIONS]
    ]
    payload = {
        "source": "SAM.gov Contract Awards API v1",
        "query": {
            "awardeeUniqueEntityId": "~".join(ueis[:100]),
            "dateSigned": f"[{start.strftime('%m/%d/%Y')},{ctx.as_of.strftime('%m/%d/%Y')}]",
        },
        "total_records": pull.get("total"),
        "method": pull.get("method"),
        "actions_listed": len(rows),
        "actions_total": len(actions),
        "columns": ["piid", "modification", "date_signed", "awarding_agency", "action_obligation"],
        "actions": rows,
    }
    rec = ctx.store.register(
        "```json\n" + json.dumps(payload) + "\n```",
        type="api:samgov",
        published_date=ctx.as_of,
        publisher="SAM.gov",
        title=(
            f"SAM.gov contract awards for {', '.join(ueis[:4])}"
            f"{' and others' if len(ueis) > 4 else ''}, signed {start.isoformat()} to "
            f"{ctx.as_of.isoformat()} ({len(actions)} actions)"
        ),
        url="https://api.sam.gov/contract-awards/v1/search",
        tier=1,
        gov_id="UEI:" + "~".join(ueis[:6]),
        meta={"actions": len(actions), "method": pull.get("method"), "ueis": ueis[:100]},
        dedupe_key=f"samgov|awards|{'~'.join(ueis)}|{start.isoformat()}|{ctx.as_of.isoformat()}",
    )
    return rec.source_id


def _fallback(ctx: ToolContext, ueis: list[str], res: dict[str, Any]) -> None:
    """The same figures from USAspending, window by window, plus the largest awards."""
    us = ctx.usaspending
    assert us is not None
    cats: dict[int, list[dict[str, Any]]] = {}
    sids: dict[int, str | None] = {}
    for y in WINDOWS:
        resp = us.obligations_by_category(
            "awarding_subagency", ueis, window_start(ctx.as_of, y), ctx.as_of, limit=40
        )
        cats[y] = resp.get("results") or []
        sids[y] = resp.get("source_id")
    awards: list[dict[str, Any]] = []
    award_sids: list[str] = []
    for page in (1, 2):
        resp = us.awards_in_window(ueis, window_start(ctx.as_of, 5), ctx.as_of, page=page)
        rows = resp.get("results") or []
        awards.extend({**r, "source_id": resp.get("source_id")} for r in rows)
        if resp.get("source_id"):
            award_sids.append(resp["source_id"])
        if len(rows) < 100:
            break
    rows, contracts = from_usaspending(cats, awards, CONTRACT_AGENCIES, PER_AGENCY)
    res.update(
        basis="usaspending",
        rows=rows,
        contracts=contracts,
        sids={y: s for y, s in sids.items() if s},
        award_sids=award_sids,
    )


def collect(plan_items: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    us, sam = ctx.usaspending, ctx.samgov
    if us is None:
        return {"error": "USAspending client not available"}
    res: dict[str, Any] = {"notes": [], "basis": None, "rows": [], "contracts": {}}
    lo, hi = _window(ctx)
    names = _discovery_names(ctx)
    disc = us.spending_by_award(names, lo, hi, limit=100, sort="Award Amount")
    ueis, uei_names = resolve_uei_set(
        disc.get("results") or [], ctx.entity.name, strict=ctx.entity.private
    )
    res.update(uei_set=ueis, uei_names=uei_names)
    if not ueis:
        res["none"] = "no recipient UEI resolved from the USAspending recipient search"
        return res

    start5 = window_start(ctx.as_of, 5)
    actions = None
    pull: dict[str, Any] = {}
    if sam is None or not sam.available:
        res["notes"].append("no SAM.gov API key is configured")
    else:
        calls = int(section_budget(SECTION).get("samgov_calls", 8))
        try:
            pull = sam.pull_contract_awards(ueis, start5, ctx.as_of, max_calls=calls)
            actions = parse_actions(pull["records"])
        except SamQuotaError as e:
            when = f"; it works again {e.next_access}" if e.next_access else ""
            res["notes"].append(f"the SAM.gov daily quota is spent{when}")
        except Exception as e:  # noqa: BLE001 - the section falls back to USAspending
            ctx.log(f"SAM.gov awards pull failed: {e!r}")
            res["notes"].append(f"the SAM.gov pull did not complete ({str(e)[:120]})")
        if actions is not None and not actions:
            res["notes"].append("SAM.gov returned no contract actions for these UEIs")
            actions = None

    if actions:
        sid = _register_sam_evidence(ctx, ueis, actions, start5, pull)
        rows = by_agency(actions, ctx.as_of)
        res.update(
            basis="samgov",
            rows=rows,
            contracts=top_contracts(actions, ctx.as_of, PER_AGENCY, CONTRACT_AGENCIES),
            sids={y: sid for y in WINDOWS},
            award_sids=[sid] if sid else [],
            sam_total=pull.get("total"),
            sam_method=pull.get("method"),
            sam_calls=pull.get("calls"),
        )
    else:
        try:
            _fallback(ctx, ueis, res)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"USAspending awards fallback failed: {e!r}")
            res["error"] = f"neither SAM.gov nor USAspending supplied the awards ({e!r})"[:240]
    return res


# ---- exhibits -----------------------------------------------------------------------------


def _cite(sids: dict[int, str | None], years: int) -> str:
    sid = sids.get(years)
    return f"[{sid}]" if sid else ""


def agency_table(
    rows: list[AgencyRow], sids: dict[int, str | None], section: int = SECTION
) -> tuple[str, list[Claim]]:
    claims: list[Claim] = []
    shown, rest = rows[:TABLE_AGENCIES], rows[TABLE_AGENCIES:]
    body: list[list[str]] = []

    def line(label: str, obligated: dict[int, float], contracts: int | None, actions: int | None):
        cells = [label]
        for y in WINDOWS:
            cells.append(f"{_m(obligated[y])} {_cite(sids, y)}".strip())
        cells.append("n/a" if contracts is None else f"{contracts:,}")
        cells.append("n/a" if actions is None else f"{actions:,}")
        body.append(cells)

    for r in shown:
        line(r.label, r.obligated, r.contracts or None, r.actions or None)
        for y in WINDOWS:
            c = common.claim(
                section,
                f"awards_obligated_{_slug(r.label)}",
                r.obligated[y],
                WINDOW_PERIOD[y],
                [sids.get(y) or ""],
                concept="net obligations by date signed",
                note=f"{r.label}; actions signed in the last {y} year{'s' if y > 1 else ''}",
            )
            if c:
                claims.append(c)
    if rest:
        other = {y: sum(r.obligated[y] for r in rest) for y in WINDOWS}
        line(f"Other ({len(rest)} agencies)", other, None, None)
    tot = total_row(rows)
    counted = all(r.actions for r in rows)  # the SAM.gov path counts actions; USAspending does not
    body.append(
        [
            "**All agencies**",
            *[f"**{_m(tot[y])}** {_cite(sids, y)}".strip() for y in WINDOWS],
            f"{sum(r.contracts for r in rows):,}" if counted else "n/a",
            f"{sum(r.actions for r in rows):,}" if counted else "n/a",
        ]
    )
    for y in WINDOWS:
        c = common.claim(
            section,
            "awards_obligated_total",
            tot[y],
            WINDOW_PERIOD[y],
            [sids.get(y) or ""],
            concept="derived",
            note=f"sum of the agencies above, last {y} year{'s' if y > 1 else ''}",
        )
        if c:
            claims.append(c)
    md = common.md_table(
        [
            "Awarding agency",
            "Last 12 months, USD m",
            "Last 3 years, USD m",
            "Last 5 years, USD m",
            "Contracts (5 yr)",
            "Actions (5 yr)",
        ],
        body,
        align_right_from=1,
    )
    return md, claims


def contract_table(
    contracts: dict[str, list[Contract]],
    basis: str,
    sids: dict[int, str | None],
    award_sids: list[str],
    section: int = SECTION,
) -> tuple[str, list[Claim]]:
    claims: list[Claim] = []
    body: list[list[str]] = []
    sid5 = sids.get(5) or (award_sids[0] if award_sids else "")
    for label, items in contracts.items():
        for c in items:
            cite = f"[{c.source_id or sid5}]" if sid5 or c.source_id else ""
            what = (c.description or "n/a")[:90]
            if basis == "samgov":
                body.append(
                    [
                        label,
                        f"{c.piid} {cite}".strip(),
                        what,
                        f"{_m(c.obligated[5])} {cite}".strip(),
                        _m(c.obligated[1]),
                        _m(c.total_value) if c.total_value else "n/a",
                        _d(c.last_signed),
                    ]
                )
                for y in (5,):
                    k = common.claim(
                        section,
                        f"contract_obligated_{_slug(c.piid)}",
                        c.obligated[y],
                        WINDOW_PERIOD[y],
                        [c.source_id or sid5],
                        concept="net obligations by date signed",
                        note=f"{label}; {c.piid}",
                    )
                    if k:
                        claims.append(k)
            else:
                body.append(
                    [
                        label,
                        f"{c.piid} {cite}".strip(),
                        what,
                        f"{_m(c.amount_to_date)} {cite}".strip(),
                        _d(c.first_signed),
                        _d(c.completion),
                    ]
                )
                k = common.claim(
                    section,
                    f"contract_amount_{_slug(c.piid)}",
                    c.amount_to_date,
                    "to-date",
                    [c.source_id or sid5],
                    concept="USAspending award amount to date",
                    note=f"{label}; {c.piid}",
                )
                if k:
                    claims.append(k)
    if basis == "samgov":
        headers = [
            "Awarding agency",
            "Contract (PIID)",
            "What it buys",
            "Obligated, 5 yr, USD m",
            "of which last 12 months",
            "Contract value (base + options), USD m",
            "Last action signed",
        ]
        right = 3
    else:
        headers = [
            "Awarding agency",
            "Contract (PIID)",
            "What it buys",
            "Award amount to date, USD m",
            "Start",
            "End",
        ]
        right = 3
    return common.md_table(headers, body, align_right_from=right), claims


# ---- draft --------------------------------------------------------------------------------

RULES = (
    "Describe only the agency totals and contracts in the structured facts. Name each agency "
    "exactly as the table labels it and say which window a figure belongs to (last 12 months, "
    "3 years, 5 years). The windows overlap: never add one to another. Do not rank the company "
    "against anyone, forecast, or call a figure a revenue: these are federal obligations by "
    "awarding agency, which differ from what the company reports as revenue."
)


def _lead_sentences(rows: list[AgencyRow], sids: dict[int, str | None], basis: str) -> list[str]:
    out: list[str] = []
    tot = total_row(rows)
    s5 = sids.get(5)
    if tot[5]:
        out.append(
            f"Federal agencies obligated ${_m(tot[5])} million over the last five years and "
            f"${_m(tot[1])} million over the last 12 months across {len(rows)} awarding agencies"
            f"{f' [{s5}]' if s5 else ''}."
        )
    for r in rows[:3]:
        out.append(
            f"{r.label} obligated ${_m(r.obligated[5])} million over five years and "
            f"${_m(r.obligated[1])} million in the last 12 months"
            f"{f' [{s5}]' if s5 else ''}."
        )
    return out


def draft(
    brief: Brief,
    tool_results: dict[str, Any],
    ctx: ToolContext,
    critic_notes: list[str] | None = None,
) -> SectionDraft:
    res = tool_results or {}
    if res.get("error"):
        return common.unavailable_draft(brief, str(res["error"])[:200], ctx)
    rows: list[AgencyRow] = res.get("rows") or []
    if not rows:
        reason = res.get("none") or "no federal contract awards were found for this company"
        return common.not_applicable_draft(
            brief, f"{reason}; the last five years hold no awards to tabulate", ctx
        )
    basis = res.get("basis") or "usaspending"
    sids: dict[int, str | None] = res.get("sids") or {}
    award_sids: list[str] = res.get("award_sids") or []
    contracts: dict[str, list[Contract]] = res.get("contracts") or {}

    agency_md, claims = agency_table(rows, sids)
    contract_md, contract_claims = contract_table(contracts, basis, sids, award_sids)
    claims += contract_claims
    exhibit_md = (
        "**Net obligations by awarding agency, by date signed** (USD millions; the windows "
        "overlap)\n\n"
        + agency_md
        + "\n\n**Largest contracts of the top agencies**"
        + (
            " (five-year window)\n\n"
            if basis == "samgov"
            else " (award amount to date; USAspending does not split an award by window)\n\n"
        )
        + contract_md
    )

    evidence = list(dict.fromkeys([*[s for s in sids.values() if s], *award_sids]))
    facts = [
        {
            "metric": c.metric,
            "window": c.period,
            "value": c.value,
            "unit": c.unit,
            "source_ids": c.source_ids,
            "note": c.note,
        }
        for c in claims
    ]
    narrative = common.draft_section(
        ctx,
        brief,
        evidence_sids=evidence,
        facts=facts or None,
        exhibit_md=exhibit_md,
        extra_rules=RULES,
        critic_notes=critic_notes,
        keywords=["awarded", "obligat", "contract", "agency"],
    ) or common.template_digest(ctx, evidence, _lead_sentences(rows, sids, basis))

    source_name = "SAM.gov's Contract Awards API" if basis == "samgov" else "USAspending"
    caveat_bits = [
        f"figures are net obligations by the date each contract action was signed, from {source_name}",
        "a de-obligation counts as negative and the windows overlap",
        "DoD actions signed in the last 90 days are not yet public",
        "these are federal obligations, not the company's reported revenue",
    ]
    notes = list(res.get("notes") or [])
    if basis == "usaspending":
        caveat_bits[0] = (
            "figures are net obligations by the date each contract action was signed, from "
            "USAspending, which carries the same Federal Procurement Data System actions as SAM.gov"
        )
        notes.insert(
            0, "SAM.gov could not supply the whole five-year window, so USAspending is the source"
        )
    elif res.get("sam_method") == "extract":
        notes.append("the actions came from SAM.gov's asynchronous extract")
    coverage = "full" if (basis == "samgov" and not notes) else "partial"
    return common.finalize_draft(
        brief,
        ctx,
        caveat=common.caveat_line("; ".join(caveat_bits)),
        narrative=narrative,
        exhibit_md=exhibit_md,
        exhibit_present=True,
        claims=claims,
        coverage=coverage,
        notes=notes,
        exhibit_title="Contract obligations by awarding agency",
    )
