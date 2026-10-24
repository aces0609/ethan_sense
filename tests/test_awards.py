"""Contract awards by awarding agency over 1, 3 and 5 years (section 15): the pure layer."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from primer.sections.awards import (
    Action,
    agency_label,
    by_agency,
    parse_actions,
    top_contracts,
    total_row,
    window_start,
)

AS_OF = date(2026, 10, 24)
FIXTURE = Path(__file__).parent / "fixtures" / "samgov" / "awards_anduril_page.json"


def act(
    piid, signed, amount, dept=("9700", "DEPT OF DEFENSE"), sub=("1700", "DEPT OF THE NAVY"), **kw
):
    return Action(
        piid=piid,
        modification=kw.pop("modification", "0"),
        signed=signed,
        dept_code=dept[0],
        dept_name=dept[1],
        sub_code=sub[0],
        sub_name=sub[1],
        obligation=amount,
        total_value=kw.pop("total_value", amount),
        **kw,
    )


def test_agency_labels_read_like_the_people_who_use_them():
    dod = ("9700", "DEPT OF DEFENSE")
    assert agency_label(*dod, "1700", "DEPT OF THE NAVY") == "DoD – Navy"
    assert agency_label(*dod, "5700", "DEPT OF THE AIR FORCE") == "DoD – Air Force"
    assert agency_label(*dod, "2100", "DEPT OF THE ARMY") == "DoD – Army"
    assert (
        agency_label(*dod, "97ZS", "U.S. SPECIAL OPERATIONS COMMAND (USSOCOM)") == "DoD – USSOCOM"
    )
    assert agency_label(*dod, "97AS", "DEFENSE LOGISTICS AGENCY (DLA)") == "DoD – DLA"
    # a sub-tier that repeats the department adds nothing
    assert agency_label("8900", "ENERGY, DEPARTMENT OF", "8900", "ENERGY, DEPARTMENT OF") == "DOE"
    assert agency_label(
        "7000", "HOMELAND SECURITY, DEPARTMENT OF", "7014", "U.S. CUSTOMS AND BORDER PROTECTION"
    ) == ("DHS – U.S. Customs and Border Protection")
    assert agency_label("9999", "SOME NEW AGENCY", "", "") == "Some New Agency"


def test_windows_start_exactly_n_years_back():
    assert window_start(AS_OF, 1) == date(2025, 10, 25)
    assert window_start(AS_OF, 5) == date(2021, 10, 25)
    assert window_start(date(2028, 2, 29), 1) == date(2027, 3, 1)  # no 29 February a year back


def test_obligations_are_summed_inside_each_window_with_deobligations_negative():
    actions = [
        act("A1", date(2026, 9, 1), 100.0),  # all three windows
        act("A1", date(2025, 10, 25), 50.0),  # the first day of the one-year window
        act("A2", date(2025, 10, 24), 200.0),  # one day too old for one year, inside three
        act("A3", date(2022, 12, 1), 400.0),  # inside five only
        act("A3", date(2024, 1, 1), -25.0),  # a de-obligation, inside three and five
        act("A4", date(2021, 10, 24), 999.0),  # one day outside five years
        act("A5", date(2026, 11, 1), 999.0),  # after as_of
    ]
    rows = by_agency(actions, AS_OF)
    assert len(rows) == 1
    r = rows[0]
    assert r.label == "DoD – Navy"
    assert r.obligated == {1: 150.0, 3: 150.0 + 200.0 - 25.0, 5: 150.0 + 200.0 - 25.0 + 400.0}
    assert r.actions == 5 and r.contracts == 3


def test_agencies_sort_by_five_year_total_and_totals_add_up():
    air = ("5700", "DEPT OF THE AIR FORCE")
    energy = ("8900", "ENERGY, DEPARTMENT OF")
    actions = [
        act("N1", date(2026, 1, 1), 300.0),
        act("F1", date(2026, 1, 1), 900.0, sub=air),
        act("E1", date(2022, 1, 1), 50.0, dept=energy, sub=energy),
    ]
    rows = by_agency(actions, AS_OF)
    assert [r.label for r in rows] == ["DoD – Air Force", "DoD – Navy", "DOE"]
    tot = total_row(rows)
    assert tot == {1: 1200.0, 3: 1200.0, 5: 1250.0}


def test_largest_contracts_are_grouped_by_piid_and_ranked_by_five_year_obligations():
    actions = [
        act("BIG", date(2022, 5, 1), 400.0, total_value=1000.0, description="TOWERS"),
        act("BIG", date(2026, 6, 1), 600.0, total_value=2000.0),  # a later mod: latest value wins
        act("MID", date(2025, 1, 1), 700.0, description="ROADRUNNER"),
        act("SMALL", date(2025, 1, 1), 10.0),
        act("TINY", date(2025, 1, 1), 1.0),
        act("OLD", date(2019, 1, 1), 9999.0),  # outside five years: not a contract of the window
    ]
    out = top_contracts(actions, AS_OF, per_agency=3)
    contracts = out["DoD – Navy"]
    assert [c.piid for c in contracts] == ["BIG", "MID", "SMALL"]  # TINY is the fourth
    big = contracts[0]
    assert big.obligated == {1: 600.0, 3: 600.0, 5: 1000.0}
    assert big.total_value == 2000.0 and big.description == "TOWERS"
    assert big.first_signed == date(2022, 5, 1) and big.last_signed == date(2026, 6, 1)


def test_only_the_top_agencies_get_contract_lists():
    actions = []
    for i, code in enumerate(("2100", "5700", "1700", "97ZS"), start=1):
        actions.append(act(f"P{i}", date(2026, 1, 1), 100.0 * i, sub=(code, f"AGENCY {code}")))
    out = top_contracts(actions, AS_OF, per_agency=1, agencies=2)
    assert len(out) == 2
    assert {c[0].piid for c in out.values()} == {"P4", "P3"}


def test_the_real_sam_gov_record_parses():
    """A record exactly as the Contract Awards API returned it for Anduril (UEI KC3CH2MSK7Q3)."""
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    (a,) = parse_actions(d["awardSummary"])
    assert a.piid == "H92402239P002" and a.modification == "P00009"
    assert a.signed == date(2026, 5, 26) and a.completion == date(2027, 6, 30)
    assert a.obligation == 0.0 and a.total_value == 9233333.0
    assert a.kind == "IDV" and a.state == "CA" and a.description == "PHASE 5"
    assert a.label == "DoD – USSOCOM"


def test_a_record_without_a_signing_date_or_piid_is_dropped():
    rec = {"contractId": {"piid": "X1"}, "awardDetails": {"dates": {}}}
    assert parse_actions([rec, {"awardDetails": {"dates": {"dateSigned": "2026-01-01"}}}]) == []


def test_usaspending_agency_names_get_the_same_short_labels():
    from primer.sections.awards import usaspending_label

    assert usaspending_label("Department of the Navy") == "DoD – Navy"
    assert usaspending_label("Department of the Air Force") == "DoD – Air Force"
    assert usaspending_label("U.S. Special Operations Command") == "DoD – SOCOM"
    assert usaspending_label("Defense Logistics Agency") == "DoD – Defense Logistics Agency"
    assert usaspending_label("Department of Energy") == "DOE"
    assert usaspending_label("National Aeronautics and Space Administration") == "NASA"
    assert usaspending_label("U.S. Customs and Border Protection") == "DHS – CBP"
    assert usaspending_label("Some Small Agency") == "Some Small Agency"


def test_the_usaspending_adapter_builds_rows_and_contracts_without_inventing_windows():
    from primer.sections.awards import from_usaspending

    cats = {
        1: [{"name": "Department of the Navy", "amount": 300.0}],
        3: [{"name": "Department of the Navy", "amount": 900.0}],
        5: [
            {"name": "Department of the Navy", "amount": 1500.0},
            {"name": "Department of Energy", "amount": 40.0},
        ],
    }
    awards = [
        {
            "Award ID": "N1",
            "Award Amount": 5000.0,
            "Description": "F-35 LOT",
            "Awarding Sub Agency": "Department of the Navy",
            "Start Date": "2022-01-01",
            "End Date": "2030-01-01",
            "source_id": "src_0013",
        }
    ]
    rows, contracts = from_usaspending(cats, awards)
    assert [r.label for r in rows] == ["DoD – Navy", "DOE"]
    assert rows[0].obligated == {1: 300.0, 3: 900.0, 5: 1500.0} and rows[1].obligated[1] == 0.0
    c = contracts["DoD – Navy"][0]
    assert c.amount_to_date == 5000.0 and c.obligated == {} and c.source_id == "src_0013"
    assert c.first_signed.isoformat() == "2022-01-01" and c.last_signed is None
