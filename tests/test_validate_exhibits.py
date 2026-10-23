"""Review 2026-10-21 §9 item 2: exhibit validators, one test per rejection pattern, the
drop-and-mark-partial behaviour, and the critic and gate wiring."""

from __future__ import annotations

from datetime import date

from primer.evidence.store import EvidenceStore
from primer.graph.researcher import validate_exhibits
from primer.runlog import RunLog
from primer.state import Claim, SectionDraft
from primer.validate.exhibits import (
    apply_exhibit_check,
    check_exhibits,
    check_exhibits_gate,
    parse_exhibits,
)
from primer.validate.gate import run_all

PRODUCTS = (
    "*Caveat: products from the 10-K.*\n\n"
    "### Segment backbone\n\n"
    "Aeronautics builds the F-35 and the C-130 [src_0001]. Missiles and Fire Control makes the PAC-3 [src_0001].\n\n"
    "### Products and programs by segment (10-K Item 1)\n\n"
    "| Segment | Product or program | Status / role (10-K language) | Key metric | Source |\n"
    "|---|---|---|---|---:|\n"
    "| Aeronautics [src_0001] | F-16 [src_0001] | described in 10-K [src_0001] | n/a | [src_0001] |\n"
    "| n/a | {product} | described in 10-K | {metric} |[src_0001][src_0017] |\n\n"
    "> Coverage: full — brief satisfied."
)

# the same draft with only the failing row: no clean row is left to keep
PRODUCTS_ALL_BAD = PRODUCTS.replace(
    "| Aeronautics [src_0001] | F-16 [src_0001] | described in 10-K [src_0001] | n/a | [src_0001] |\n",
    "",
)


def _rules(md: str, **kw) -> set[str]:
    return {p["rule"] for p in check_exhibits(md, **kw)}


def test_segment_na_when_text_names_the_segment():
    md = PRODUCTS.format(product="F-35", metric="n/a")
    probs = check_exhibits(md)
    assert [p["rule"] for p in probs] == ["segment_na"]
    assert "Aeronautics" in probs[0]["reason"] and probs[0]["cell"] == "F-35"
    # a product the prose never places in a segment is not rejected
    assert not check_exhibits(PRODUCTS.format(product="THAAD", metric="n/a"))
    # segment names may also come from the caller (the 10-K list)
    md2 = PRODUCTS.format(product="PAC-3", metric="n/a").replace("Aeronautics [src_0001]", "n/a")
    assert not check_exhibits(md2)
    assert "segment_na" in _rules(md2, segments=["Missiles and Fire Control"])


def test_key_metric_numeral_stripped_from_program_name():
    for metric in (
        "35 type aircraft (If you just look at the Far East theater, the Chinese are building literally hundreds of F-35 type aircraft...) [src_0017]",
        "3 missiles (We use PAC-3 missiles for a wide range of targets...) [src_0017]",
        "130 aircraft (We resumed deliveries for the F-16 and increased output of C-130 aircraft...) [src_0017]",
    ):
        md = PRODUCTS.format(product="THAAD", metric=metric)
        probs = check_exhibits(md)
        assert [p["rule"] for p in probs] == ["bare_key_metric"], metric
        assert "program name" in probs[0]["reason"]
    md = PRODUCTS.format(
        product="THAAD",
        metric="1 Deliveries (Any forward-looking statement speaks only as of the date on which it is made...) [src_0017]",
    )
    rules = _rules(md)
    assert rules == {"boilerplate"}  # the cover-page rule fires first on the same cell
    md = PRODUCTS.format(product="THAAD", metric="1 Deliveries [src_0017]")
    assert _rules(md) == {"bare_key_metric"}
    # a real count survives
    md = PRODUCTS.format(
        product="THAAD",
        metric="191 deliveries (Lockheed delivered 191 F-35s in 2025...) [src_0017]",
    )
    assert not check_exhibits(md)


def test_cover_page_and_forward_looking_boilerplate_cells():
    md = (
        "### Peer list\n\n| Peer | Ticker | Positioning (one line) | Source |\n|---|---|---|---|\n"
        "| L3HARRIS | LHX | Yes ☑ No ☐ Indicate by check mark if the registrant is not required to file reports pursuant to Section 13 or Section 15(d) of the Act [src_0039] | peer 10-K [src_0039] |\n"
        "| RTX | RTX | A diversified aerospace and defense company [src_0040] | peer 10-K [src_0040] |"
    )
    probs = check_exhibits(md)
    assert [p["rule"] for p in probs] == ["boilerplate"] and probs[0]["row"] == 0
    md2 = "| Metric | Value |\n|---|---|\n| Outlook | Any forward-looking statement speaks only as of its date [src_0007] |"
    assert _rules(md2) == {"boilerplate"}


def test_ownership_labels_must_be_people_or_institutions():
    block = (
        "### Executive officers and ownership\n\n"
        "| Name | Age | Title | Source |\n|---|---|---|---|\n"
        "| James D. Taiclet [src_0001] | 65 [src_0001] | Chairman [src_0001] | 10-K [src_0001] |\n\n"
        "**Ownership concentration**\n\n"
        "- Sales: 20.0% of shares beneficially owned [src_0015]\n"
        "- Free Cash Flow: 40.0% of shares beneficially owned [src_0015]\n"
        "- Overall Payout Factor: 47.0% of shares beneficially owned [src_0015]\n"
        "- Percentages are beneficial ownership as reported in the proxy table [src_0015]\n\n"
        "> Coverage: full — officer list taken from the 10-K."
    )
    probs = check_exhibits(block)
    assert {p["rule"] for p in probs} == {"ownership_label"}
    assert {p["cell"] for p in probs} >= {"Sales", "Free Cash Flow", "Overall Payout Factor"}
    good = (
        block.replace("Sales:", "State Street Corporation:")
        .replace("Free Cash Flow:", "The Vanguard Group:")
        .replace(
            "Overall Payout Factor:",
            "All directors and executive officers as a group (24 persons):",
        )
    )
    assert not check_exhibits(good)
    table = (
        "| Holder | Shares | % |\n|---|---:|---:|\n| BlackRock, Inc. [src_0015] | 16,900,000 [src_0015] | 7.3% [src_0015] |\n"
        "| ROIC [src_0015] | n/a | 25.0% [src_0015] |"
    )
    probs = check_exhibits(table)
    assert [p["rule"] for p in probs] == ["ownership_label"] and probs[0]["cell"] == "ROIC"


def test_transcript_timestamps_and_page_chrome_anywhere_in_a_table():
    md = (
        "| Source | Statement | Date |\n|---|---|---|\n"
        "| Prepared remarks [src_0008] | Kelly Ortberg President and CEO 0:04:03 In fact, on 737-7, testing is done [src_0008] | 2026-07-28 [src_0008] |"
    )
    assert _rules(md) == {"timestamp"}
    md = (
        "| Date | Event | Source type | Implication | Source |\n|---|---|---|---|---|\n"
        "| 2026-10-20 [src_0095] | Boeing wins FAA approval [src_0095] | news [src_0095] | Follow your favorite stocksCREATE FREE ACCOUNT The FAA on Tuesday approved [src_0095] | [src_0095] |\n"
        "| 2026-10-20 [src_0101] | Lockheed invests [src_0101] | news [src_0101] | Type topic Tags Categories Search Log In Lockheed Martin is investing $300 million [src_0101] | [src_0101] |\n"
        "| 2026-09-15 [src_0102] | PAC-3 contract [src_0102] | press release [src_0102] | The Army awarded a contract [src_0102] | [src_0102] |"
    )
    probs = check_exhibits(md)
    assert [p["rule"] for p in probs] == ["page_chrome", "page_chrome"]
    assert [p["row"] for p in probs] == [0, 1]


def test_a_failing_row_is_dropped_and_the_rest_of_the_table_is_kept():
    """One bad cell used to take the whole table with it (SpaceX's event log lost every row to
    one 8-K cover-page cell). Only the failing rows go; the table stays when a row survives."""
    md = PRODUCTS.format(product="F-35", metric="n/a")
    chk = apply_exhibit_check(md, "full")
    assert not chk.ok and chk.tables_left == 1
    assert chk.dropped[0]["kind"] == "rows" and chk.dropped[0]["rows_dropped"] == 1
    out = chk.markdown
    assert "| n/a | F-35 |" not in out and "| Aeronautics [src_0001] | F-16" in out
    assert "Aeronautics builds the F-35 and the C-130 [src_0001]" in out  # prose kept
    assert out.endswith(
        "> Coverage: partial — exhibit 'Products and programs by segment (10-K Item 1)': 1 row "
        "dropped by the exhibit validator: segment is n/a for F-35 while the text places it in Aeronautics."
    )
    assert not check_exhibits(out)  # what is left passes the gate's second look


def test_failing_exhibit_is_dropped_prose_kept_coverage_partial():
    md = PRODUCTS_ALL_BAD.format(product="F-35", metric="n/a")
    chk = apply_exhibit_check(md, "full", segments=["Aeronautics"])
    assert not chk.ok and chk.tables_left == 0
    assert chk.dropped[0]["exhibit"] == "Products and programs by segment (10-K Item 1)"
    out = chk.markdown
    assert "| Segment |" not in out and "### Products and programs by segment" not in out
    assert "Aeronautics builds the F-35 and the C-130 [src_0001]" in out  # prose kept
    assert out.endswith(
        "> Coverage: partial — exhibit 'Products and programs by segment (10-K Item 1)' dropped by the "
        "exhibit validator: segment is n/a for F-35 while the text places it in Aeronautics."
    )
    # a clean draft is returned untouched
    clean = PRODUCTS.format(product="THAAD", metric="n/a")
    assert apply_exhibit_check(clean, "full").markdown == clean
    # a draft without a coverage line gets one; an unavailable section keeps its status
    no_cov = md.rsplit("\n\n> Coverage", 1)[0]
    assert (
        "> Coverage: partial — exhibit"
        in apply_exhibit_check(no_cov, "full", segments=["Aeronautics"]).markdown
    )
    assert (
        "> Coverage: unavailable"
        in apply_exhibit_check(
            no_cov + "\n\n> Coverage: unavailable — x.", "unavailable", segments=["Aeronautics"]
        ).markdown
    )


def test_parse_exhibits_titles_tables_and_ownership_block():
    md = (
        "**Income statement** (USD millions)\n\n| Metric | FY2025 |\n|---|---:|\n| Revenue | 75,048 [src_0001] |\n\n"
        "**Ownership concentration**\n\n- State Street Corporation: 15.6% of shares beneficially owned [src_0015]\n\n"
        "> Coverage: full — brief satisfied."
    )
    ex = parse_exhibits(md)
    assert [e.kind for e in ex] == ["table", "ownership"]
    assert ex[0].title == "Income statement" and ex[0].rows == [["Revenue", "75,048"]]
    assert ex[1].labels == ["State Street Corporation"]


def test_critic_wiring_drops_exhibit_and_marks_partial(tmp_path):
    draft = SectionDraft(
        section=3,
        title="Products, Services and Technology",
        markdown=PRODUCTS_ALL_BAD.format(product="F-35", metric="n/a"),
        coverage="full",
        exhibit_present=True,
    )
    runlog = RunLog(tmp_path)
    out = validate_exhibits(draft, ["Aeronautics"], runlog)
    assert out.coverage == "partial" and out.exhibit_present is False
    assert "| Segment |" not in out.markdown
    assert any(n.startswith("exhibit 'Products and programs by segment") for n in out.critic_notes)
    assert runlog.validation["exhibits"]["3"]["dropped"][0]["rules"] == ["segment_na"]
    # a dropped ownership block takes its claims with it; the officer table and its claim stay
    own = SectionDraft(
        section=8,
        title="Management and Governance",
        markdown=(
            "| Name | Age | Title | Source |\n|---|---|---|---|\n| James D. Taiclet [src_0001] | 65 [src_0001] | CEO [src_0001] | 10-K [src_0001] |\n\n"
            "**Ownership concentration**\n\n- ROIC: 25.0% of shares beneficially owned [src_0015]\n\n"
            "> Coverage: full — brief satisfied."
        ),
        coverage="full",
        exhibit_present=True,
        claims=[
            Claim(
                section=8,
                metric="ownership_pct_roic",
                value=25.0,
                unit="pct",
                period="2026",
                source_ids=["src_0015"],
            ),
            Claim(
                section=8,
                metric="officer_count",
                value=1,
                unit="count",
                period="2026",
                source_ids=["src_0001"],
            ),
        ],
    )
    out8 = validate_exhibits(own, [], runlog)
    assert [c.metric for c in out8.claims] == ["officer_count"]
    assert (
        out8.exhibit_present
        and out8.coverage == "partial"
        and "| James D. Taiclet" in out8.markdown
    )


def test_gate_flags_remaining_exhibit_problems(tmp_path):
    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 21))
    md = {
        5: "| Peer | Positioning |\n|---|---|\n| LHX | Indicate by check mark if the registrant is a shell company |",
        9: "| Metric | FY2025 |\n|---|---:|\n| Revenue | 75,048 |",
    }
    gate = check_exhibits_gate(md)
    assert not gate["ok"] and [p["section"] for p in gate["problems"]] == [5]
    rep = run_all(
        md_text="---\ncompany: X\n---\n\nbody",
        sections_md=md,
        claims=[],
        coverage={5: "full", 9: "full"},
        requested=[5, 9],
        store=store,
        as_of=date(2026, 10, 21),
    )
    assert rep.checks["exhibits"]["ok"] is False and rep.passed is False
    assert any("exhibit validator (boilerplate)" in n for n in rep.section_notes[5])
    assert 9 not in rep.section_notes
