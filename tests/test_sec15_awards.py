"""Section 15, Federal Contract Awards by Agency, end to end on mocked SAM.gov and USAspending."""

from __future__ import annotations

import re
from datetime import date

import pytest
from httpx import Response

from primer.evidence.store import EvidenceStore
from primer.graph.main import foundation_pack, intake, new_run, plan_sections, resolve_entity
from primer.graph.researcher import build_researcher
from primer.sections.common import cited_ids
from primer.state import Inputs

AS_OF = date(2026, 10, 21)
SAM_SEARCH = "https://api.sam.gov/contract-awards/v1/search"

NAVY = ("1700", "DEPT OF THE NAVY")
AIR = ("5700", "DEPT OF THE AIR FORCE")
ARMY = ("2100", "DEPT OF THE ARMY")


def _record(piid, signed, amount, dept=("9700", "DEPT OF DEFENSE"), sub=NAVY, desc="", total=None):
    return {
        "contractId": {"piid": piid, "modificationNumber": "0"},
        "coreData": {
            "awardOrIDV": "AWARD",
            "federalOrganization": {
                "contractingInformation": {
                    "contractingDepartment": {"code": dept[0], "name": dept[1]},
                    "contractingSubtier": {"code": sub[0], "name": sub[1]},
                }
            },
        },
        "awardDetails": {
            "dates": {
                "dateSigned": f"{signed} 00:00:00",
                "ultimateCompletionDate": "2029-06-30 00:00:00",
            },
            "dollars": {"actionObligation": amount},
            "totalContractDollars": {"totalBaseAndAllOptionsValue": total or amount},
            "productOrServiceInformation": {"descriptionOfContractRequirement": desc},
        },
    }


ENERGY = ("8900", "ENERGY, DEPARTMENT OF")
RECORDS = [
    _record("N00019-25-C-0070", "2025-12-01", 300e6, desc="F-35 LOT 20 LONG LEAD", total=1.2e9),
    _record("N00019-25-C-0070", "2023-03-01", 200e6),
    _record("N00024-22-C-5401", "2022-04-01", 50e6, desc="SM-6 PRODUCTION"),
    _record("FA8620-26-C-0001", "2026-02-01", 100e6, sub=AIR, desc="CLASSIFIED SUPPORT"),
    _record("DE-NA0003525", "2024-01-01", 20e6, dept=ENERGY, sub=ENERGY, desc="LAB OPERATIONS"),
    _record("W31P4Q-21-C-0009", "2021-12-01", 10e6, sub=ARMY, desc="PAC-3"),
    _record("W31P4Q-15-C-0001", "2019-01-01", 999e6, sub=ARMY),  # outside five years
]


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
            "brief": st["plan"][15],
            "iteration": 0,
            "critic_notes": [],
        }
    )
    return out["drafts"][15]


def _inputs() -> Inputs:
    return Inputs(company_name="Lockheed Martin", ticker="LMT", as_of_date=AS_OF, sections=[15])


def _claim(draft: dict, metric: str, period: str) -> float | None:
    for c in draft["claims"]:
        if c["metric"] == metric and c["period"] == period:
            return c["value"]
    return None


def _sam(usaspending_mock, status=200, records=None, total=None):
    seen: list[dict] = []

    def respond(request):
        seen.append(dict(request.url.params))
        if status == 429:
            return Response(
                429, json={"code": "900804", "nextAccessTime": "2026-Oct-22 00:00:00+0000 UTC"}
            )
        recs = RECORDS if records is None else records
        return Response(200, json={"totalRecords": str(total or len(recs)), "awardSummary": recs})

    usaspending_mock.get(SAM_SEARCH).mock(side_effect=respond)
    return seen


def test_agency_totals_for_three_windows_come_from_sam_gov(
    edgar_mock, usaspending_mock, output_dir, monkeypatch
):
    monkeypatch.setenv("SAM_GOV_API_KEY", "sam-test")
    seen = _sam(usaspending_mock)
    st = _state(_inputs())
    d = _invoke(st)
    store = EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)
    md = d["markdown"]

    assert d["coverage"] == "full", md
    assert d["exhibit_present"] and "### Contract obligations by awarding agency" in md
    # the pull asked for the five-year window of signing dates, awardee UEIs joined by ~
    assert seen and re.fullmatch(r"\[10/22/2021,10/21/2026\]", seen[0]["dateSigned"])
    assert seen[0]["awardeeUniqueEntityId"]
    # DoD by service, DOE as one agency, labelled the way the people who read it say them
    for label in ("DoD – Navy", "DoD – Air Force", "DoD – Army", "DOE"):
        assert f"| {label} |" in md, label
    # windows are rolling and by date signed
    assert _claim(d, "awards_obligated_dod_navy", "12m") == 300e6
    assert _claim(d, "awards_obligated_dod_navy", "3y") == 300e6
    assert _claim(d, "awards_obligated_dod_navy", "5y") == 550e6  # 300 + 200 + 50
    assert _claim(d, "awards_obligated_dod_air_force", "12m") == 100e6
    assert _claim(d, "awards_obligated_doe", "12m") == 0.0
    assert _claim(d, "awards_obligated_doe", "3y") == 20e6
    assert _claim(d, "awards_obligated_total", "5y") == 680e6  # the 2019 action is outside
    assert _claim(d, "awards_obligated_total", "12m") == 400e6
    # the largest contract of the Navy is the one that obligated most in five years
    block = md.split("**Largest contracts of the top agencies**", 1)[1]
    navy_rows = [ln for ln in block.splitlines() if ln.startswith("| DoD – Navy")]
    assert "N00019-25-C-0070" in navy_rows[0] and "F-35 LOT 20 LONG LEAD" in navy_rows[0]
    assert "N00024-22-C-5401" in navy_rows[1]
    # every citation and claim resolves to a registered SAM.gov record
    for sid in cited_ids(md):
        assert store.has(sid), sid
    assert all(store.get(s).type == "api:samgov" for c in d["claims"] for s in c["source_ids"])
    assert "not yet public" in d["caveat"] and "not the company's reported revenue" in d["caveat"]


def test_usaspending_stands_in_when_sam_gov_has_no_key(
    edgar_mock, usaspending_mock, output_dir, monkeypatch
):
    monkeypatch.delenv("SAM_GOV_API_KEY", raising=False)
    st = _state(_inputs())
    d = _invoke(st)
    md = d["markdown"]
    assert d["coverage"] == "partial", md
    assert "from USAspending" in d["caveat"]
    assert "no SAM.gov API key is configured" in md
    assert "SAM.gov could not supply the whole five-year window" in md
    assert "Award amount to date" in md  # a USAspending award is not split by window
    assert _claim(d, "awards_obligated_total", "5y") is not None
    store = EvidenceStore.for_run(st["run_dir"], as_of=AS_OF)
    assert all(store.get(s).type == "api:usaspending" for c in d["claims"] for s in c["source_ids"])


def test_a_spent_sam_gov_quota_falls_back_and_says_when_it_returns(
    edgar_mock, usaspending_mock, output_dir, monkeypatch
):
    monkeypatch.setenv("SAM_GOV_API_KEY", "sam-test")
    _sam(usaspending_mock, status=429)
    d = _invoke(_state(_inputs()))
    assert d["coverage"] == "partial"
    assert "the SAM.gov daily quota is spent; it works again 2026-Oct-22" in d["markdown"]
    assert "from USAspending" in d["caveat"]


def test_no_resolved_recipient_means_not_applicable(
    edgar_mock, usaspending_mock, output_dir, monkeypatch
):
    from primer.sections import sec15_awards

    monkeypatch.setattr(sec15_awards, "resolve_uei_set", lambda rows, name, strict=False: ([], {}))
    d = _invoke(_state(_inputs()))
    assert d["coverage"] == "not_applicable"
    assert "no recipient UEI resolved" in d["markdown"] and not d["claims"]


@pytest.mark.parametrize("n", [1, 3])
def test_the_section_is_registered_and_has_a_brief(n):
    from primer.graph.researcher import REGISTRY
    from primer.sections.briefs import brief_for
    from primer.state import RESEARCHED_SECTIONS, SECTION_TITLES

    assert REGISTRY[15] == "primer.sections.sec15_awards" and 15 in RESEARCHED_SECTIONS
    assert SECTION_TITLES[15] == "Federal Contract Awards by Agency"
    b = brief_for(15, co="Anduril", fy=2025)
    assert "SAM.gov" in " ".join(b.source_buckets) and b.web_queries == []
