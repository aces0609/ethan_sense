"""Federal contract awards by awarding agency, over the last 1, 3 and 5 years (section 15).

Pure functions over contract *actions*: SAM.gov's Contract Awards API (the successor of FPDS)
returns one record per action, a base award or a modification, each with the dollars obligated by
that action, the date it was signed and the awarding department and sub-tier. Summing
`actionObligation` over the actions signed inside a window gives the net obligations of the
window (a de-obligation counts negative), which is what USAspending reports for the same awards.

Nothing here talks to the network. `parse_actions` reads SAM.gov records, `from_usaspending`
reads USAspending rows into the same shape, `by_agency` and `top_contracts` aggregate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

WINDOWS = (1, 3, 5)  # years

# Department codes (FPDS / SAM.gov) -> the short names people use.
DEPT_SHORT = {
    "9700": "DoD",
    "8900": "DOE",
    "7000": "DHS",
    "1500": "DOJ",
    "1200": "USDA",
    "1300": "Commerce",
    "3600": "VA",
    "4700": "GSA",
    "8000": "NASA",
    "2000": "Treasury",
    "1900": "State",
    "7500": "HHS",
    "6900": "DOT",
    "9100": "Education",
    "1400": "Interior",
    "1600": "Labor",
    "4900": "NSF",
    "6800": "EPA",
    "2800": "SSA",
}
# DoD sub-tiers: the Military Departments by name, so "DEPT OF THE NAVY" reads "Navy".
SERVICE_WORDS = (
    ("NAVY", "Navy"),
    ("AIR FORCE", "Air Force"),
    ("ARMY", "Army"),
    ("MARINE", "Marine Corps"),
    ("SPACE FORCE", "Space Force"),
)
ABBREV = re.compile(r"\(([A-Z][A-Z0-9&/\-]{1,9})\)")


def agency_label(dept_code: str, dept_name: str, sub_code: str, sub_name: str) -> str:
    """ "DoD – Navy", "DoD – SOCOM", "DHS – CBP", "DOE": the department's short name, then the
    sub-tier when it is a different body (a sub-tier that repeats the department adds nothing)."""
    dept = DEPT_SHORT.get(str(dept_code)) or _title(dept_name) or "Unknown agency"
    sub_name = (sub_name or "").strip()
    if not sub_name or str(sub_code) == str(dept_code) or _same(sub_name, dept_name):
        return dept
    upper = sub_name.upper()
    for word, short in SERVICE_WORDS:
        if word in upper:
            return f"{dept} – {short}"
    m = ABBREV.search(sub_name)
    if m:
        return f"{dept} – {m.group(1)}"
    return f"{dept} – {_title(sub_name)}"


def _same(a: str, b: str) -> bool:
    def key(s: str) -> str:
        return re.sub(r"[^a-z]", "", s.lower().replace("department", "").replace("dept", ""))

    return bool(key(a)) and key(a) == key(b)


def _title(name: str) -> str:
    """ "ENERGY, DEPARTMENT OF" -> "Energy"; "U.S. CUSTOMS AND BORDER PROTECTION" -> title case."""
    s = (name or "").strip()
    if not s:
        return ""
    m = re.match(r"^(.*?),\s*DEPARTMENT OF$", s, re.I)
    if m:
        s = m.group(1)
    s = re.sub(r"^DEPT\.? OF( THE)?\s+", "", s, flags=re.I)
    small = {"of", "and", "the", "for"}
    words = s.lower().split()
    return " ".join(
        w.upper() if "." in w else (w if (w in small and i) else w.capitalize())
        for i, w in enumerate(words)
    )  # "u.s." stays "U.S."


@dataclass
class Action:
    """One contract action (a base award or a modification) from the awards data."""

    piid: str
    modification: str
    signed: date
    dept_code: str
    dept_name: str
    sub_code: str
    sub_name: str
    obligation: float  # dollars this action obligated (negative for a de-obligation)
    total_value: float  # the contract's base-and-all-options value as of this action
    description: str = ""
    kind: str = "AWARD"  # AWARD | IDV
    completion: date | None = None
    state: str = ""

    @property
    def label(self) -> str:
        return agency_label(self.dept_code, self.dept_name, self.sub_code, self.sub_name)


def _date(value: Any) -> date | None:
    if not value:
        return None
    s = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_actions(summary: list[dict[str, Any]]) -> list[Action]:
    """SAM.gov `awardSummary` records -> actions. A record without a signing date is dropped:
    it cannot be placed in a window."""
    out: list[Action] = []
    for rec in summary or []:
        cid = rec.get("contractId") or {}
        core = rec.get("coreData") or {}
        det = rec.get("awardDetails") or {}
        org = (core.get("federalOrganization") or {}).get("contractingInformation") or {}
        dept, sub = org.get("contractingDepartment") or {}, org.get("contractingSubtier") or {}
        dates = det.get("dates") or {}
        dollars = det.get("dollars") or {}
        totals = det.get("totalContractDollars") or {}
        signed = _date(dates.get("dateSigned"))
        piid = str(cid.get("piid") or "").strip()
        if not signed or not piid:
            continue
        out.append(
            Action(
                piid=piid,
                modification=str(cid.get("modificationNumber") or ""),
                signed=signed,
                dept_code=str(dept.get("code") or (cid.get("subtier") or {}).get("code") or ""),
                dept_name=str(dept.get("name") or ""),
                sub_code=str(sub.get("code") or ""),
                sub_name=str(sub.get("name") or ""),
                obligation=_num(dollars.get("actionObligation")),
                total_value=max(
                    _num(totals.get("totalBaseAndAllOptionsValue")),
                    _num(dollars.get("baseAndAllOptionsValue")),
                ),
                description=_clean_text(
                    (det.get("productOrServiceInformation") or {}).get(
                        "descriptionOfContractRequirement"
                    )
                ),
                kind=str(core.get("awardOrIDV") or "AWARD"),
                completion=_date(dates.get("ultimateCompletionDate")),
                state=str(
                    ((core.get("principalPlaceOfPerformance") or {}).get("state") or {}).get("code")
                    or ""
                ),
            )
        )
    return out


def window_start(as_of: date, years: int) -> date:
    """The first day of a window ending `as_of`: exactly `years` years back, exclusive of the day
    a year earlier (a window of one year holds 365 or 366 days of actions)."""
    try:
        return as_of.replace(year=as_of.year - years) + timedelta(days=1)
    except ValueError:  # 29 February
        return as_of.replace(year=as_of.year - years, day=28) + timedelta(days=1)


@dataclass
class AgencyRow:
    label: str
    dept_code: str
    sub_code: str
    obligated: dict[int, float] = field(default_factory=dict)  # years -> USD
    actions: int = 0  # actions inside the longest window
    contracts: int = 0  # distinct contracts with an action inside the longest window


def by_agency(actions: list[Action], as_of: date) -> list[AgencyRow]:
    """Net obligations per awarding agency inside each window, largest five-year total first."""
    starts = {y: window_start(as_of, y) for y in WINDOWS}
    longest = max(WINDOWS)
    rows: dict[tuple[str, str], AgencyRow] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for a in actions:
        if a.signed > as_of or a.signed < starts[longest]:
            continue
        key = (a.dept_code, a.sub_code)
        row = rows.setdefault(
            key, AgencyRow(a.label, a.dept_code, a.sub_code, {y: 0.0 for y in WINDOWS})
        )
        for y in WINDOWS:
            if a.signed >= starts[y]:
                row.obligated[y] += a.obligation
        row.actions += 1
        seen.setdefault(key, set()).add(a.piid)
    for key, row in rows.items():
        row.contracts = len(seen[key])
    return sorted(rows.values(), key=lambda r: (-r.obligated[longest], r.label))


@dataclass
class Contract:
    agency: str
    piid: str
    description: str
    obligated: dict[int, float]  # years -> USD obligated by actions inside the window
    total_value: float  # base and all options, latest known
    first_signed: date | None
    last_signed: date | None
    completion: date | None
    kind: str
    amount_to_date: float | None = None  # USAspending's award amount: not split by window
    source_id: str = ""  # the record the row came from, when it has its own


def top_contracts(
    actions: list[Action], as_of: date, per_agency: int = 3, agencies: int = 8
) -> dict[str, list[Contract]]:
    """The largest contracts of each of the top agencies, ranked by what they obligated inside the
    five-year window. A contract is one PIID under one awarding agency; its actions are summed."""
    starts = {y: window_start(as_of, y) for y in WINDOWS}
    longest = max(WINDOWS)
    top = [r.label for r in by_agency(actions, as_of)[:agencies]]
    groups: dict[tuple[str, str], list[Action]] = {}
    for a in actions:
        if a.signed > as_of or a.signed < starts[longest] or a.label not in top:
            continue
        groups.setdefault((a.label, a.piid), []).append(a)
    out: dict[str, list[Contract]] = {label: [] for label in top}
    for (label, piid), acts in groups.items():
        acts.sort(key=lambda x: x.signed)
        latest = acts[-1]
        desc = next((x.description for x in reversed(acts) if x.description), "")
        out[label].append(
            Contract(
                agency=label,
                piid=piid,
                description=desc,
                obligated={
                    y: sum(x.obligation for x in acts if x.signed >= starts[y]) for y in WINDOWS
                },
                total_value=max(x.total_value for x in acts),
                first_signed=acts[0].signed,
                last_signed=latest.signed,
                completion=latest.completion,
                kind=latest.kind,
            )
        )
    for label in out:
        out[label].sort(key=lambda c: (-c.obligated[longest], c.piid))
        out[label] = out[label][:per_agency]
    return out


DOD_HINT = re.compile(
    r"navy|air force|army|marine|space force|special operations|defense|missile defense|"
    r"cyber command|transportation command",
    re.I,
)
DHS_BODIES = (
    ("customs and border protection", "DHS – CBP"),
    ("immigration and customs enforcement", "DHS – ICE"),
    ("transportation security administration", "DHS – TSA"),
    ("coast guard", "DHS – Coast Guard"),
    ("federal emergency management", "DHS – FEMA"),
)


# USAspending's agency names -> the short names used in the table
NAME_SHORT = {
    "national aeronautics and space administration": "NASA",
    "national oceanic and atmospheric administration": "Commerce – NOAA",
    "department of commerce": "Commerce",
    "department of veterans affairs": "VA",
    "general services administration": "GSA",
    "department of state": "State",
    "department of health and human services": "HHS",
    "department of justice": "DOJ",
    "department of transportation": "DOT",
    "department of agriculture": "USDA",
    "department of the interior": "Interior",
    "department of homeland security": "DHS",
    "department of the treasury": "Treasury",
    "national science foundation": "NSF",
    "environmental protection agency": "EPA",
}


def _clean_text(v: Any) -> str:
    """A description on one line: SAM.gov descriptions carry real newlines and the two characters
    backslash-n typed as a line break."""
    return " ".join(str(v or "").replace(chr(92) + "n", " ").split())


def usaspending_label(name: str) -> str:
    """The same labels as `agency_label`, from a USAspending awarding sub-agency name."""
    n = (name or "").strip()
    up = n.upper()
    for word, short in SERVICE_WORDS:
        if word in up:
            return f"DoD – {short}"
    if "SPECIAL OPERATIONS COMMAND" in up:
        return "DoD – SOCOM"
    if DOD_HINT.search(n):
        return f"DoD – {n}"
    low = n.lower()
    if low in ("department of energy", "energy, department of"):
        return "DOE"
    if low in NAME_SHORT:
        return NAME_SHORT[low]
    for needle, label in DHS_BODIES:
        if needle in low:
            return label
    return _title(n) or "Unknown agency"


def from_usaspending(
    categories: dict[int, list[dict[str, Any]]],
    awards: list[dict[str, Any]],
    agencies: int = 8,
    per_agency: int = 3,
) -> tuple[list[AgencyRow], dict[str, list[Contract]]]:
    """The agency rows and contract lists from USAspending: obligations by awarding sub-agency for
    each window, and the largest awards active in the five-year window (their amount to date,
    which USAspending does not split by window)."""
    rows: dict[str, AgencyRow] = {}
    for years, results in categories.items():
        for r in results:
            label = usaspending_label(str(r.get("name") or ""))
            row = rows.setdefault(label, AgencyRow(label, "", "", {y: 0.0 for y in WINDOWS}))
            row.obligated[years] = row.obligated.get(years, 0.0) + _num(r.get("amount"))
    longest = max(WINDOWS)
    ordered = sorted(rows.values(), key=lambda r: (-r.obligated[longest], r.label))
    top = [r.label for r in ordered[:agencies]]
    contracts: dict[str, list[Contract]] = {label: [] for label in top}
    for a in awards:
        label = usaspending_label(
            str(a.get("Awarding Sub Agency") or a.get("Awarding Agency") or "")
        )
        if label not in contracts or len(contracts[label]) >= per_agency:
            continue
        contracts[label].append(
            Contract(
                agency=label,
                piid=str(a.get("Award ID") or ""),
                description=_clean_text(a.get("Description")),
                obligated={},
                total_value=0.0,
                first_signed=_date(a.get("Start Date")),
                last_signed=None,
                completion=_date(a.get("End Date")),
                kind="AWARD",
                amount_to_date=_num(a.get("Award Amount")),
                source_id=str(a.get("source_id") or ""),
            )
        )
    return ordered, contracts


def total_row(rows: list[AgencyRow]) -> dict[int, float]:
    return {y: sum(r.obligated[y] for r in rows) for y in WINDOWS}
