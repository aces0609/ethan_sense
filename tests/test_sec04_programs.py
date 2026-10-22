"""Section 4 handler: the §7 program rule on recorded USAspending fixtures, a hand-written
SAM.gov entity walk, a GAO page and a DoD daily contract announcement page (offline)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from httpx import Response

from primer.clients.dod_announcements import normalize_contract_number, parse_announcements
from primer.config import section_budget
from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.runlog import get_runlog
from primer.sections import sec04_programs as s4
from primer.sections.common import cited_ids
from primer.state import Inputs

AS_OF = date(2026, 10, 21)

DOD_MD = """# Contracts For Oct. 15, 2026

## NAVY

Lockheed Martin Corp., Lockheed Martin Aeronautics Co., Fort Worth, Texas, is awarded a $1,234,567,890 fixed-price-incentive-firm modification (P00012) to a previously awarded contract (N00019-25-C-0070). This modification adds scope to procure long lead materials, parts, components and associated support for F-35 Lot 20 aircraft for the Air Force, Marine Corps, Navy, non-Department of Defense participants and Foreign Military Sales customers. Work will be performed in Fort Worth, Texas (60%); El Segundo, California (25%); and Warton, United Kingdom (15%), and is expected to be completed in June 2029. Fiscal 2026 aircraft procurement (Air Force) funds in the amount of $500,000,000 will be obligated at the time of award, none of which will expire at the end of the current fiscal year. The Naval Air Systems Command, Patuxent River, Maryland, is the contracting activity.

Raytheon Co., Tucson, Arizona, is awarded a $220,000,000 firm-fixed-price contract for Standard Missile-6 production. Work will be performed in Tucson, Arizona (100%), and is expected to be completed in December 2028. The Naval Sea Systems Command, Washington, D.C., is the contracting activity (N00024-26-C-5401).

## ARMY

Lockheed Martin Corp., Grand Prairie, Texas, was awarded a $9.8 billion fixed-price-incentive contract for Patriot Advanced Capability-3 (PAC-3) Missile Segment Enhancement missiles. Bids were solicited via the internet with one received. Work will be performed in Grand Prairie, Texas, with an estimated completion date of Sept. 30, 2029. Fiscal 2026 missile procurement, Army funds in the amount of $2,100,000,000 were obligated at the time of the award. U.S. Army Contracting Command, Redstone Arsenal, Alabama, is the contracting activity (W31P4Q-26-C-0001).
"""

GAO_MD = """# F-35 Joint Strike Fighter: Program Continues to Encounter Production Issues and Modernization Delays

GAO-26-107123 Published: May 20, 2026. Publicly Released: May 20, 2026.

## What GAO Found

The F-35 program's Block 4 modernization effort continues to experience cost growth and schedule delays, and the program office now expects Block 4 to be completed in 2031 rather than 2026. Production of Technology Refresh 3 hardware improved in 2025 but deliveries remained behind the contract schedule. GAO recommends that the Department of Defense report the full cost of Block 4 to Congress.
"""

TRADE_MD = "# Pentagon weighs F-35 Lot 20 deal\n\nBreaking Defense reports the F-35 is on schedule, sources say.\n"


def _state(inputs: Inputs) -> dict:
    run_id, run_dir = new_run(inputs)
    st = {"inputs": inputs.model_dump(mode="json"), "run_id": run_id, "run_dir": str(run_dir)}
    for node in (intake, resolve_entity, foundation_pack, plan_sections):
        st.update(node(st))
    return st


def _invoke(st: dict) -> dict:
    out = build_researcher().invoke(
        {
            "run_dir": st["run_dir"],
            "as_of": st["as_of"],
            "entity": st["entity"],
            "foundation": st["foundation"],
            "brief": st["plan"][4],
            "iteration": 0,
            "critic_notes": [],
        }
    )
    return out["drafts"][4]


def _inputs() -> Inputs:
    return Inputs(company_name="Lockheed Martin", ticker="LMT", as_of_date=AS_OF, sections=[4])


def _sam_entity(uei, cage, name, city, state, status="Active", parent="ZFN2JJXBLZT3"):
    return {
        "entityRegistration": {
            "ueiSAM": uei,
            "cageCode": cage,
            "legalBusinessName": name,
            "registrationStatus": status,
            "registrationExpirationDate": "2027-03-01",
        },
        "coreData": {
            "physicalAddress": {"city": city, "stateOrProvinceCode": state},
            "entityHierarchyInformation": {
                "immediateParentEntity": {"ueiSAM": parent},
                "ultimateParentEntity": {"ueiSAM": parent},
            },
        },
    }


SAM_ENTITIES = [
    _sam_entity("G4KDGE4JFFK7", "1AB23", "LOCKHEED MARTIN CORPORATION", "FORT WORTH", "TX"),
    _sam_entity("XFJMYSYFJEK4", "2CD45", "LOCKHEED MARTIN CORPORATION", "ORLANDO", "FL"),
    _sam_entity("ZFN2JJXBLZT3", "3EF67", "LOCKHEED MARTIN CORP", "BETHESDA", "MD"),
    _sam_entity(
        "OLDUEI000001", "4GH89", "LOCKHEED MARTIN CORPORATION", "FORT WORTH", "TX", status="Expired"
    ),
    _sam_entity(
        "OTHERUEI0001",
        "5IJ01",
        "LOCKHEED MARTIN CORPORATION",
        "FORT WORTH",
        "TX",
        parent="SOMEONEELSE1",
    ),
]


def _web(firecrawl_mock, usaspending_mock, sam_entities=None):
    """respx tries routers in start order and only a strict router falls through; the
    permissive usaspending router starts before the Firecrawl one, so the Firecrawl (and
    SAM.gov) routes are registered on it too."""
    gao = {
        "url": "https://www.gao.gov/products/gao-26-107123",
        "title": "F-35 Joint Strike Fighter: Program Continues to Encounter Production Issues",
    }
    trade = {
        "url": "https://breakingdefense.com/2026/10/f-35-lot-20",
        "title": "Pentagon weighs F-35 Lot 20 deal",
    }
    dodh = {
        "url": "https://www.defense.gov/News/Contracts/Contract/Article/4312345/",
        "title": "Contracts For Oct. 15, 2026",
    }
    fc = firecrawl_mock(
        search={
            "GAO report": [trade, gao],
            "selected acquisition report": [gao],
            "contract award Contracts": [dodh, trade],
        },
        pages={"gao.gov": GAO_MD, "defense.gov": DOD_MD, "breakingdefense.com": TRADE_MD},
    )
    usaspending_mock.route(host="api.firecrawl.dev").mock(side_effect=fc)
    if sam_entities is not None:

        def by_uei(request):
            # the handler looks registrations up one UEI at a time (personal keys page at 10)
            uei = request.url.params.get("ueiSAM")
            rows = [e for e in sam_entities if e["entityRegistration"]["ueiSAM"] == uei]
            return Response(200, json={"totalRecords": len(rows), "entityData": rows})

        usaspending_mock.get("https://api.sam.gov/entity-information/v3/entities").mock(
            side_effect=by_uei
        )
    return fc


def _rows(md: str, heading: str) -> list[list[str]]:
    block = md.split(f"### {heading}", 1)[1].split("###", 1)[0]
    lines = [ln for ln in block.splitlines() if ln.startswith("|")]
    return [[c.strip() for c in ln.strip("|").split(" | ")] for ln in lines[2:]]


# ---- (a) the gate ---------------------------------------------------------------------------


def test_not_applicable_when_10k_discloses_no_government_revenue(
    edgar_mock, usaspending_mock, output_dir
):
    st = _state(_inputs())
    st["foundation"]["gov_revenue"] = {"disclosed": None}
    d = _invoke(st)
    assert d["coverage"] == "not_applicable"
    assert "discloses no US-government revenue" in d["markdown"]
    assert not d["claims"] and not d["exhibit_present"]
    assert len(usaspending_mock.calls) == 0


# ---- (b) the full path ----------------------------------------------------------------------


def test_full_path_program_rule(
    edgar_mock, usaspending_mock, firecrawl_mock, output_dir, monkeypatch
):
    monkeypatch.setenv("SAM_GOV_API_KEY", "sam-test")
    fc = _web(firecrawl_mock, usaspending_mock, SAM_ENTITIES)
    st = _state(_inputs())
    foundation_calls = len(fc.calls)  # the foundation pack's own transcript searches
    d = _invoke(st)
    section_calls = fc.calls[foundation_calls:]
    sam_calls = sum(1 for c in usaspending_mock.calls if "api.sam.gov" in str(c.request.url))
    assert 1 <= sam_calls <= 10  # one call per UEI, within the per-primer SAM.gov budget
    store = EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)
    md = d["markdown"]

    assert d["coverage"] == "full", md
    assert d["exhibit_present"]
    assert d["caveat"] and md.startswith("*Caveat:")

    # every citation token and every claim source id resolves to a registered record
    for sid in cited_ids(md):
        assert store.has(sid), sid
    assert d["claims"]
    for c in d["claims"]:
        assert c["source_ids"] and all(store.has(s) for s in c["source_ids"]), c
        assert c["metric"] == c["metric"].lower()

    # program table: >= 3 rows, each cell cites a USAspending record (page record or a gov_id record)
    prog_rows = _rows(md, "Program table")
    assert len(prog_rows) >= 3
    assert any(r[0] == "F-35" for r in prog_rows)
    for r in prog_rows:
        obligated_sids = re.findall(r"\[(src_\d{4})\]", r[4])
        assert obligated_sids, r
        recs = [store.get(s) for s in obligated_sids]
        assert all(x.type == "api:usaspending" and x.tier == 1 for x in recs)
        assert any(x.gov_id for x in recs) or any(x.meta.get("page") for x in recs)
        # role only from an award record: prime for USAspending rows, never 'sub'
        assert r[2] in (s4.ROLE_PRIME, s4.ROLE_UNCONFIRMED)
        assert "sub" not in r[2]
        # status cells cite only program_status_hosts pages
        if r[8] != s4.NO_FINDING:
            sids = re.findall(r"\[(src_\d{4})\]", r[8])
            assert sids and all("gao.gov" in store.get(s).url for s in sids), r[8]
    f35 = next(r for r in prog_rows if r[0] == "F-35")
    assert "cost growth" in f35[8] and "GAO-26-107123" in f35[8] and "2026-05-20" in f35[8]
    assert "N00019-25-C-0070" in f35[7] and "2026-10-15" in f35[7]
    assert "defense.gov" in store.get(re.findall(r"\[(src_\d{4})\]", f35[7])[0]).url
    # ceiling comes from the award detail (base_and_all_options), obligated from the page
    assert "30,398" in f35[5]
    detail_sid = re.findall(r"\[(src_\d{4})\]", f35[5])[0]
    assert store.get(detail_sid).gov_id.startswith("CONT_AWD_")
    # trade press never cited anywhere in the section
    assert not any("breakingdefense" in store.get(s).url for s in cited_ids(md))

    # claims: obligated / ceiling / announced are separate metrics
    metrics = {c["metric"]: c for c in d["claims"]}
    assert metrics["obligated_f_35"]["period"] == "FY2021-FY2026"
    assert metrics["ceiling_f_35"]["value"] == pytest.approx(30397830432.16)
    assert (
        metrics["announced_f_35"]["value"] == pytest.approx(1234567890)
        and metrics["announced_f_35"]["period"] == "2026-10-15"
    )
    assert metrics["obligated_f_35"]["value"] != metrics["ceiling_f_35"]["value"]

    # agency mix from the FY2025 / FY2026 fixtures; FY2024 returned nothing and is noted
    mix_rows = _rows(md, "Agency mix")
    assert any(r[0] == "Department of the Army" for r in mix_rows)
    army = metrics["obligations_department_of_the_army"]
    assert (
        army["period"] == "FY2026"
        and army["value"] == pytest.approx(13854559035.55)
        and army["unit"] == "USD"
    )
    assert (
        "FY2025, USD m" in md
        and "FY2026, USD m" in md
        and "FY2024" not in md.split("### Agency mix")[1].split("###")[0].split("|")[0]
    )
    assert "FY2024: no USAspending obligations returned" in md
    mix_sid = army["source_ids"][0]
    assert "13854559035.55" in store.text(mix_sid)  # the cited record holds the number

    # locations: only the active Fort Worth registration (its city/state is an award place of performance)
    loc_rows = _rows(md, "Locations")
    assert [r[0].split(" [")[0] for r in loc_rows] == ["1AB23"]
    assert "FORT WORTH, TX" in loc_rows[0][2] and "F-35" in loc_rows[0][4]
    assert "ORLANDO" not in md and "BETHESDA" not in md and "4GH89" not in md and "5IJ01" not in md
    sam_sid = re.findall(r"\[(src_\d{4})\]", loc_rows[0][0])[0]
    assert (
        store.get(sam_sid).type == "api:samgov"
        and store.get(sam_sid).gov_id == "UEI:G4KDGE4JFFK7;CAGE:1AB23"
    )
    assert (
        "active SAM.gov registrations under parent UEI ZFN2JJXBLZT3 with award activity (place of performance) in FY2021-FY2026"
        in md
    )
    assert metrics["active_registration_count"]["value"] == 1

    # UEI set resolution by name is only used for discovery; it is written to run.json meta
    runlog = get_runlog(st["run_dir"])
    assert "G4KDGE4JFFK7" in runlog.meta["uei_set"] and "G9CZJMSCRR28" in runlog.meta["uei_set"]
    assert runlog.meta["parent_uei"] == "ZFN2JJXBLZT3"
    assert "entity resolved to 15 UEI(s)" in md

    # budget within config/budgets.yaml
    b = section_budget(4)
    used_s, cap_s = (int(x) for x in d["budget"]["searches"].split("/"))
    used_p, cap_p = (int(x) for x in d["budget"]["scrapes"].split("/"))
    assert used_s <= cap_s == b["searches"] and used_p <= cap_p == b["scrapes"]
    assert sum(1 for c in section_calls if c[0] == "search") == used_s <= b["searches"]
    assert sum(1 for c in section_calls if c[0] == "scrape") == used_p <= b["scrapes"]
    assert not any(c[0] == "agent" for c in fc.calls)  # use_agent: false
    assert "research stopped at the section budget" not in md

    # unassigned awards are reported, not inferred
    assert "name no program" in md


# ---- (c) no SAM.gov key: reduced view, partial -----------------------------------------------


def test_no_sam_key_reduced_locations_view(
    edgar_mock, usaspending_mock, firecrawl_mock, output_dir
):
    _web(firecrawl_mock, usaspending_mock)
    st = _state(_inputs())
    d = _invoke(st)
    md = d["markdown"]
    store = EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)
    assert d["coverage"] == "partial"
    assert "SAM.gov registrations not available (no API key)" in md
    assert "place-of-performance view" in md
    loc_rows = _rows(md, "Locations")
    fw = next(r for r in loc_rows if r[0].startswith("Fort Worth, TX"))
    assert "named in 10-K Item 2" in fw[1] and "F-35" in fw[2]
    tenk = re.findall(r"\[(src_\d{4})\]", fw[1])[0]
    assert "Item 2" in store.text(tenk)
    assert not any(c["metric"] == "active_registration_count" for c in d["claims"])
    assert all("CAGE" not in cell for r in loc_rows for cell in r)  # no CAGE invented
    assert "no CAGE is asserted" in md
    for sid in cited_ids(md):
        assert store.has(sid)


# ---- unit: the DoD announcement parser ------------------------------------------------------


def test_parse_dod_announcements_sample():
    rows = parse_announcements(
        DOD_MD,
        ["Lockheed Martin"],
        title="Contracts For Oct. 15, 2026",
        program_terms={"F-35": ["F-35"], "PAC-3": ["PAC-3"]},
    )
    assert [r["contract_number"] for r in rows] == ["N00019-25-C-0070", "W31P4Q-26-C-0001"]
    navy, army = rows
    assert navy["amount_usd"] == 1234567890 and navy["modification"] == "P00012"
    assert (
        navy["piid"] == "N0001925C0070"
        and normalize_contract_number("N00019-25-C-0070") == "N0001925C0070"
    )
    assert navy["service"] == "NAVY" and navy["announcement_date"] == "2026-10-15"
    assert navy["contracting_activity"].startswith(
        "Naval Air Systems Command, Patuxent River, Maryland"
    )
    assert navy["completion"] == "June 2029"
    assert navy["work_locations"][0] == {"place": "Fort Worth, Texas", "pct": 60.0}
    assert (
        len(navy["work_locations"]) == 3
        and navy["work_locations"][2]["place"] == "Warton, United Kingdom"
    )
    assert navy["obligated_at_award_usd"] == 500000000
    assert navy["contract_type"] == "fixed-price-incentive-firm"
    assert navy["programs"] == ["F-35"] and "F-35 Lot 20" in navy["description"]
    assert (
        army["amount_usd"] == 9.8e9 and army["service"] == "ARMY" and army["programs"] == ["PAC-3"]
    )
    assert army["contracting_activity"].startswith("U.S. Army Contracting Command")
    # the Raytheon paragraph is not a company row
    assert all("Raytheon" not in (r["recipient"] or "") for r in rows)
    assert parse_announcements(DOD_MD, ["Raytheon"])[0]["contract_number"] == "N00024-26-C-5401"


# ---- unit: program grouping is conservative ---------------------------------------------------


def test_program_keywords_and_assignment_are_conservative():
    item1 = (
        "Item 1. Business\n\nWe operate in two business segments: Aeronautics and Missiles and Fire Control (MFC).\n\n"
        "Major programs include the F-35 Lightning II, C-130 Hercules and F-22 Raptor. MFC provides air and missile "
        "defense systems, including the PAC-3 and THAAD programs, and precision strike weapons such as GMLRS and HIMARS.\n\n"
        "We compete with The Boeing Company, Northrop Grumman Corporation and RTX Corporation. COVID-19 affected 2021.\n"
    )
    kw = s4.program_keywords(item1, "LOCKHEED MARTIN CORP", stop={"LMT"})
    assert set(kw) == {"F-35", "C-130", "F-22", "PAC-3", "THAAD", "GMLRS", "HIMARS"}
    assert (
        "LIGHTNING II" in kw["F-35"]
        and "MFC" not in kw
        and "Boeing" not in kw
        and "COVID-19" not in kw
    )
    kw = s4.learn_aliases(
        ["F-35 LIGHTNING II JOINT STRIKE FIGHTER (JSF) LOW RATE INITIAL PRODUCTION (LRIP) LOT 6"],
        kw,
    )
    assert "JSF" in kw["F-35"] and all("LRIP" not in t for t in kw.values())
    assert s4.assign_program("JSF LRIP LOT II - AAC", kw) == "F-35"
    assert s4.assign_program("F-35A LRIP 15 USAF AIRCRAFT LONG LEAD", kw) == "F-35"
    assert s4.assign_program("LRIP LOT 12 ADVANCE ACQUISITION CONTRACT", kw) is None  # not inferred
    assert s4.assign_program("PRODUCTION SUSTAINMENT SERVICES", kw) is None
    uei, names = s4.resolve_uei_set(
        [
            {"Recipient UEI": "A", "Recipient Name": "LOCKHEED MARTIN CORP", "Award Amount": 5},
            {
                "Recipient UEI": "B",
                "Recipient Name": "LOCKHEED MARTIN SERVICES, LLC",
                "Award Amount": 9,
            },
            {
                "Recipient UEI": "C",
                "Recipient Name": "MARTIN MARIETTA MATERIALS",
                "Award Amount": 50,
            },
        ],
        "LOCKHEED MARTIN CORP",
    )
    assert uei == ["B", "A"] and "C" not in names
    assert (
        s4.federal_fy(date(2026, 10, 21)) == 2027
        and s4.latest_complete_fy(date(2026, 9, 30)) == 2025
    )
