"""Quality fixes found on the SpaceX run of 2026-10-23.

Required tables missing from sections 2, 3, 7 and 14, no peers in section 5, press figures
mislabelled on the private path, and an output folder named after the company instead of the
ticker. Every case below is a shape taken from the real SpaceX prospectus, 8-K or press text.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from primer.evidence.html import (
    is_hard_wrapped,
    normalize_wrapped_document,
    rejoin_leader_tables,
)
from primer.sections import exhibits_sec03 as e3
from primer.sections import text_tools as tt
from primer.state import Inputs

NL = chr(10)

# ---- prospectus text: one cell per line, one printed line per paragraph ------------------------

LEADER_TABLE = NL.join(
    [
        "Revenue disaggregated by type and segment is as follows:",
        "",
        "Year Ended December 31,",
        "",
        "Launch Services ........................",
        "",
        "|",
        "",
        "$2,576",
        "",
        "| |",
        "",
        "$2,584",
        "",
        "|",
        "",
        "Space ........................",
        "",
        "|",
        "",
        "4,086",
        "",
        "| |",
        "",
        "3,796",
        "",
        "|",
        "",
        "Connectivity ........................",
        "",
        "|",
        "",
        "11,387",
        "",
        "| |",
        "",
        "7,599",
        "",
        "|",
        "",
        "AI ........................",
        "",
        "|",
        "",
        "3,201",
        "",
        "| |",
        "",
        "2,620",
        "",
        "|",
        "",
        "Total revenues ........................",
        "",
        "|",
        "",
        "$18,674",
        "",
        "| |",
        "",
        "$14,015",
        "",
        "|",
        "",
        "(1) Includes Starlink Mobile.",
    ]
)


def test_one_cell_per_line_tables_become_pipe_rows():
    out = rejoin_leader_tables(LEADER_TABLE)
    assert "| Space | 4,086 | 3,796 |" in out
    assert "| Total revenues | $18,674 | $14,015 |" in out
    # consecutive rows stay adjacent, so a table parser reads them as one table
    rows = [ln for ln in out.split(NL) if ln.startswith("| ")]
    first = out.split(NL).index(rows[0])
    assert out.split(NL)[first : first + len(rows)] == rows
    assert "(1) Includes Starlink Mobile." in out  # the line after the table survives
    # text with no dot-leader label is returned untouched
    assert rejoin_leader_tables("plain text" + NL + "| a | 1 |") == "plain text" + NL + "| a | 1 |"


def _wrapped_document(sentences: int = 80) -> str:
    """Prose as a prospectus prints it: a blank line after every printed line, a page number
    and 'Table of Contents' between pages."""
    lines = []
    for i in range(sentences):
        words = (f"sentence{i} " + "growth in launch cadence and subscribers " * 8).split()
        text = " ".join(words) + "."
        a, b, c = text[:105], text[105:210], text[210:]
        a = a.rsplit(" ", 1)[0]
        b = text[len(a) + 1 : len(a) + 1 + 100].rsplit(" ", 1)[0]
        c = text[len(a) + 1 + len(b) + 1 :]
        lines += [a, b, c]
        if i % 20 == 19:
            lines += ["89", "Table of Contents"]
    return (NL + NL).join(lines)


def test_a_hard_wrapped_document_is_reflowed_into_paragraphs():
    raw = _wrapped_document()
    assert is_hard_wrapped([ln for ln in raw.split(NL) if ln.strip()])
    out = normalize_wrapped_document(raw)
    paras = [p for p in out.split(NL + NL) if p]
    assert len(paras) < 90  # about one per sentence, not three lines each plus page marks
    assert "Table of Contents" not in out
    assert all(p.rstrip().endswith(".") for p in paras)
    assert not any(p.strip() == "89" for p in paras)


def test_ordinary_filing_text_is_left_alone():
    para = "A normal paragraph of a 10-K that runs on as one long line. " * 6
    text = (NL + NL).join([para] * 5)
    assert normalize_wrapped_document(text) == text
    assert not is_hard_wrapped([para] * 300)


# ---- section 2: segment names and a table that mixes segments with sub-lines --------------------

SEGMENT_SENTENCE = (
    "Segments in Our Vertically-Integrated Innovation Engine We have three reportable segments"
    + NL
    + "in our vertically integrated innovation engine: Space, Connectivity, and AI."
)

MIXED_TABLE = NL.join(
    [
        "| Launch Services | $2,576 | $2,584 | $1,964 |",
        "| Launch & Development | 1,510 | 1,212 | 1,593 |",
        "| Space | 4,086 | 3,796 | 3,557 |",
        "| Consumer | 7,208 | 4,830 | 2,817 |",
        "| Enterprise & Government (1) | 4,179 | 2,769 | 1,052 |",
        "| Connectivity | 11,387 | 7,599 | 3,869 |",
        "| Advertising | 1,844 | 1,728 | 2,323 |",
        "| AI Solutions & Infrastructure | 1,357 | 892 | 638 |",
        "| AI | 3,201 | 2,620 | 2,961 |",
        "| Total revenues | $18,674 | $14,015 | $10,387 |",
    ]
)


def test_segment_names_survive_line_wraps_and_words_before_the_colon():
    assert tt.segment_names_from_text(SEGMENT_SENTENCE) == ["Space", "Connectivity", "AI"]
    # the old shape still reads
    assert tt.segment_names_from_text("We have four business segments: A1, B2, C3 and D4.") == [
        "A1",
        "B2",
        "C3",
        "D4",
    ]


def test_a_revenue_table_of_segments_and_their_sub_lines_is_read_by_segment_rows():
    t = tt.segment_table(MIXED_TABLE, ["Space", "Connectivity", "AI"])
    assert t is not None
    assert [r.label for r in t.rows] == ["Space", "Connectivity", "AI"]
    assert t.rows[1].values == [11387.0, 7599.0, 3869.0]
    assert t.total.values == [18674.0, 14015.0, 10387.0]
    # rows that do not add up to the total are not a segment table
    bad = MIXED_TABLE.replace(
        "| Space | 4,086 | 3,796 | 3,557 |", "| Space | 1,000 | 3,796 | 3,557 |"
    )
    assert tt.segment_table(bad, ["Space", "Connectivity", "AI"]) is None


# ---- section 3: products are the headings of "Our Solutions & Services" -----------------------


def _body(name: str) -> str:
    return (
        f"{name} is described here with enough words to count as a real description. " * 4
    ).strip()


SOLUTIONS = NL.join(
    [
        "Our Solutions & Services",
        "Unparalleled Launch Capability",
        _body("Launch"),
        "Falcon 9 Overview",
        _body("Falcon 9"),
        "Falcon 9",
        "Falcon 9 had completed 620 orbital launches. " + _body("Falcon 9"),
        "Starship Overview",
        _body("Starship") + " It is in development.",
        "Starship Landing Burn",
        "Dragon Cargo Overview",
        _body("Dragon"),
        "Connectivity",
        "Starlink Consumer Broadband",
        _body("Starlink") + " It is in production.",
        "Starlink Mobile",
        _body("Starlink Mobile"),
        "AI",
        "Grok",
        _body("Grok"),
        "X Platform",
        _body("X"),
        "Competition",
        "We compete with others.",
    ]
)


def test_product_headings_group_under_their_segment_and_skip_captions():
    rows = e3.heading_products(SOLUTIONS, ["Space", "Connectivity", "AI"], "src_0001")
    by = [(r.segment, r.product) for r in rows]
    assert ("Space", "Falcon 9") in by and ("Space", "Starship") in by
    assert ("Space", "Dragon Cargo") in by
    assert ("Connectivity", "Starlink Consumer Broadband") in by
    assert ("AI", "Grok") in by and ("AI", "X Platform") in by
    names = [r.product for r in rows]
    assert "Starship Landing Burn" not in names  # a picture caption, no description under it
    assert "Unparalleled Launch Capability" not in names  # a section lead-in, not a product
    assert names.count("Falcon 9") == 1  # "Falcon 9 Overview" and "Falcon 9" are one block
    assert next(r for r in rows if r.product == "Starship").status == "development"


def test_the_product_table_uses_headings_only_when_asked():
    segs = ["Space", "Connectivity", "AI"]
    with_headings = e3.product_table(SOLUTIONS, "src_0001", segs, prefer_headings=True)
    assert with_headings.present and "| AI | Grok |" in with_headings.markdown
    plain = e3.product_table(SOLUTIONS, "src_0001", segs)
    assert "| AI | Grok |" not in plain.markdown  # a 10-K keeps its list-based rows


def test_prospectus_competition_section_is_read_before_the_marketing_text():
    text = NL.join(
        [
            "Our competitive advantage is scale, and we believe our competitive position is strong.",
            "",
            "Competition",
            "",
            "In Space, we compete with United Launch Alliance, Boeing, Lockheed Martin, Blue Origin and "
            "Rocket Lab. Emerging commercial launch providers also compete with us in AGI and GPU markets.",
        ]
    )
    names = tt.competitor_candidates(
        text, "Space Exploration Technologies Corp", heading_first=True
    )
    assert {"Boeing", "Lockheed Martin", "Blue Origin", "Rocket Lab"} <= set(names)
    assert not ({"Emerging", "AGI", "GPU"} & set(names))


# ---- section 14: an 8-K cover page is not an event ---------------------------------------------


def test_8k_cover_page_text_is_never_the_event_sentence():
    from primer.sections.exhibits_sec14 import eightk_event

    cover = (
        "Indicate by check mark whether the registrant is an emerging growth company as defined in "
        "Rule 405 of the Securities Act. "
        "On August 14, 2026 the Company completed the acquisition of Anysphere for consideration of $60 billion."
    )
    event, sentence = eightk_event(cover, "2.01,9.01")
    assert "Indicate by check mark" not in sentence
    only_cover = "Indicate by check mark whether the registrant is a shell company (as defined in Rule 12b-2)."
    _event, sentence = eightk_event(only_cover, "5.07")
    assert sentence == "n/a"


# ---- section 7: deal 8-Ks from the filing index when full-text search is refused --------------


def test_deal_8ks_come_from_the_filing_index_by_item_code(tmp_path):
    from primer.evidence.store import EvidenceStore
    from primer.sections.sec07_acquisitions import _deal_8ks_from_index
    from primer.state import FilingRef

    def ref(accn, filed, items):
        return FilingRef(
            form="8-K",
            filing_date=filed,
            accession=accn,
            primary_document="d.htm",
            url=f"https://www.sec.gov/Archives/edgar/data/1/{accn}/d.htm",
            index_url="https://www.sec.gov/x",
            items=items,
        )

    refs = [
        ref("0001-26-000003", date(2026, 8, 14), "2.01,3.02,9.01"),
        ref("0001-26-000002", date(2026, 7, 1), "5.02"),  # a management change, not a deal
        ref("0001-26-000001", date(2026, 6, 16), "1.01,9.01"),
    ]
    edgar = SimpleNamespace(
        filings=lambda cik, forms=None, since=None, limit=None: refs,
        get_text=lambda url: (
            "<p>The Company completed the acquisition of Target Inc. for cash.</p>"
        ),
    )
    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 23))
    ctx = SimpleNamespace(
        co="NEW CO",
        entity=SimpleNamespace(cik="0000000001", name="NEW CO"),
        as_of=date(2026, 10, 23),
        edgar=edgar,
        store=store,
        log=lambda *_a, **_k: None,
    )
    out: dict = {"filings": [], "notes": []}
    _deal_8ks_from_index(ctx, out)
    assert [f["date"] for f in out["filings"]] == ["2026-08-14", "2026-06-16"]  # newest first
    assert all(f["kind"] == "8-K" and store.get(f["sid"]).tier == 1 for f in out["filings"])
    assert any("item code" in n for n in out["notes"])


# ---- private path: figures that are not what they look like ------------------------------------


def test_press_amounts_that_are_not_revenue_valuation_or_rounds_are_not_classified_so():
    from primer.sections.private import classify_money, money_tokens

    def kinds(sentence):
        return [classify_money(sentence, s, e) for _v, s, e in money_tokens(sentence)]

    assert kinds("SpaceX has revenue per employee of $1,456,500 according to StockAnalysis.") == [
        None
    ]
    assert kinds("SpaceX completed a share buyback of $500 million in 2023.") == [None]
    assert kinds(
        "The IPO raised $75 billion in proceeds, a valuation of about $1.77 trillion."
    ) == ["funding"]
    # the ordinary cases still classify
    assert kinds("SpaceX was valued at $127 billion in 2022.") == ["valuation"]
    assert kinds("SpaceX revenue was $18.7 billion in 2025.") == ["revenue"]
    # a per-month figure does not swallow a later amount in the same sentence
    assert kinds("Starlink ARPU was $81 per month while SpaceX raised $750 million.") == ["funding"]


def test_model_proposed_datapoints_get_the_same_guards():
    from primer.sections.private import _ModelItem, verify_model_item

    page = "SpaceX has revenue per employee of $1,456,500 according to StockAnalysis, a data page."
    item = _ModelItem(
        kind="revenue",
        value_usd=1456500.0,
        period="FY2025",
        basis="reported",
        quote=page,
        source_id="src_0001",
    )
    assert not verify_model_item(item, page, ["spacex"], {"revenue"})
    ok_page = "SpaceX generated revenue of $18,674,000,000 in 2025, according to its prospectus."
    ok = item.model_copy(update={"value_usd": 18674000000.0, "quote": ok_page})
    assert verify_model_item(ok, ok_page, ["spacex"], {"revenue"})


# ---- output folder -----------------------------------------------------------------------------


def test_the_output_folder_is_the_resolved_ticker_not_the_company_name():
    from primer.graph.main import folder_slug

    def lhx(_inputs):
        return {"cik": "0000202058", "ticker": "LHX"}

    # given a ticker: that ticker; given only a CIK or a clear name: what the SEC file lists
    assert folder_slug(Inputs(company_name="Harris", ticker="lhx"), resolve=lhx) == "LHX"
    assert folder_slug(Inputs(company_name="Harris", cik="0000202058"), resolve=lhx) == "LHX"
    assert folder_slug(Inputs(company_name="Harris"), resolve=lhx) == "LHX"
    # no SEC match (private, ambiguous or lookup failure): the company-name slug
    assert folder_slug(Inputs(company_name="Acme Orbital, Inc."), resolve=lambda _i: None) == (
        "acme-orbital-inc"
    )
    # a private run never asks the SEC file, even when the company is listed
    boom = lambda _i: (_ for _ in ()).throw(AssertionError("must not look up"))  # noqa: E731
    assert (
        folder_slug(Inputs(company_name="Space Exploration Technologies Corp", private=True), boom)
        == "space-exploration-technologies-corp"
    )


def test_a_merger_8k_becomes_a_deal_row_with_its_share_consideration():
    """SpaceX's Anysphere merger is on two 8-Ks that state the price in shares, never in dollars,
    so the dollar-pattern extractor produced no row for the deal the section is about."""
    from primer.sections.exhibits_sec07 import acquisition_table, extract_deals

    announce = (
        "Item 1.01. Entry into a Material Definitive Agreement. On June 16, 2026, Space Exploration "
        "Technologies Corp. (the “Company”), X67 Inc., a wholly owned subsidiary of the Company "
        "(“Merger Sub”), and Anysphere, Inc. (“Cursor”) entered into an Agreement and Plan "
        "of Merger (the “Merger Agreement”). The transaction is subject to the satisfaction of "
        "closing conditions."
    )
    done = (
        "Item 2.01 Completion of Acquisition or Disposition of Assets. As previously announced, on "
        "June 16, 2026, the Company, X67 Inc. and Anysphere, Inc. (“Cursor”) entered into an "
        "Agreement and Plan of Merger. On August 14, 2026 the Merger became effective and Cursor's "
        "shares were converted into the right to receive an aggregate of 389,289,254 shares of the "
        "Company's Class A common stock."
    )
    deals = extract_deals(announce, "src_0012", "8-K", date(2026, 6, 16))
    deals += extract_deals(done, "src_0011", "8-K", date(2026, 8, 14))
    assert [d.status for d in deals] == ["announced", "completed"]
    assert all(d.value_usd == 0.0 for d in deals)  # shares are not dollars: no value is invented
    ex = acquisition_table(deals)
    assert len(ex.rows) == 1 and ex.rows[0].status == "completed"
    assert ex.rows[0].date == "2026-06-16"  # the announcement date, not the closing date
    assert "n/a (consideration: 389,289,254 shares) [src_0011]" in ex.markdown
    assert ex.claims == []  # nothing numeric to hold the numeric gate to


def test_the_announce_date_is_the_earliest_filing_and_a_dollar_value_sits_beside_the_shares():
    from primer.sections.exhibits_sec07 import Deal, acquisition_table, extract_deals

    done = (
        "Item 2.01 Completion of Acquisition. On June 16, 2026, the Company and Anysphere, Inc. "
        "(“Cursor”) entered into an Agreement and Plan of Merger. On August 14, 2026 the Merger "
        "became effective for an aggregate of 389,289,254 shares of the Company's Class A common stock."
    )
    announce = (
        "Item 1.01. On June 16, 2026, the Company and Anysphere, Inc. (“Cursor”) entered into an "
        "Agreement and Plan of Merger."
    )
    press = Deal(
        target="Anysphere",
        value_usd=60e9,
        status="completed",
        date="2026-08-14",
        date_basis="page date",
        segment="n/a",
        rationale="n/a",
        sentence="SpaceX closed the $60 billion deal.",
        source_id="src_0063",
        source_kind="news",
    )
    # newest filing first, as the section reads them
    deals = extract_deals(done, "src_0011", "8-K", date(2026, 8, 14))
    deals += extract_deals(announce, "src_0012", "8-K", date(2026, 6, 16))
    deals.append(press)
    row = acquisition_table(deals).rows[0]
    assert row.date == "2026-06-16"
    md = acquisition_table(deals).markdown
    assert "$60 billion [src_0063]" in md and "consideration: 389,289,254 shares [src_0011]" in md
