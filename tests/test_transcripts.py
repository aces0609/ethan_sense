"""A transcript must belong to the company it is filed under.

On 2026-10-22 the search for L3Harris's Q2 2026 call returned Deere & Company's Q2 2026
transcript; the picker checked the host, the word "transcript", the year and the quarter but never
the company, so Deere's earnings call was cited in L3Harris's sections 6 and 12.
"""

from __future__ import annotations

from primer.graph.foundation import _pick_transcript, company_tokens, names_company
from primer.state import Entity

LHX = Entity(
    name="L3HARRIS TECHNOLOGIES, INC. /DE/",
    ticker="LHX",
    cik="0000202058",
    is_public=True,
    aliases=["L3Harris Technologies"],
)
LMT = Entity(
    name="LOCKHEED MARTIN CORP", ticker="LMT", aliases=["LOCKHEED MARTIN", "Lockheed Martin"]
)
RTX = Entity(name="RTX Corp", ticker="RTX", aliases=[])

DEERE_Q2 = {
    "title": "Deere (DE) Q2 2026 Earnings Call Transcript | The Motley Fool",
    "url": "https://www.fool.com/earnings/call-transcripts/2026/05/21/deere-de-q2-2026-earnings-call-transcript/",
    "domain": "fool.com",
}
LHX_Q1 = {
    "title": "L3Harris (LHX) Q1 2026 Earnings Transcript | The Motley Fool",
    "url": "https://www.fool.com/earnings/call-transcripts/2026/04/30/l3harris-lhx-q1-2026-earnings-transcript/",
    "domain": "fool.com",
}


def test_company_tokens_strip_legal_suffixes_and_keep_the_distinctive_name():
    toks, ticker = company_tokens(LHX)
    assert "l3harris" in toks and ticker == "lhx"
    assert not any(t in ("inc", "de", "corp") for t in toks)
    toks_l, _ = company_tokens(LMT)
    assert "lockheedmartin" in toks_l and "lockheed" in toks_l


def test_names_company_by_name_token_or_whole_word_ticker():
    assert names_company(LHX_Q1["title"], LHX)
    assert names_company("https://fool.com/x/l3harris-lhx-q4-2025-earnings-call-transcript/", LHX)
    assert not names_company(DEERE_Q2["title"] + " " + DEERE_Q2["url"], LHX)
    # a short ticker matches only as a whole word, never inside another word
    assert names_company("RTX (RTX) Q2 2026 Earnings Call Transcript", RTX)
    assert not names_company(
        "Bank of America (BAC) Q2 2026 Earnings Call Transcript",
        Entity(name="BOEING CO", ticker="BA"),
    )
    assert names_company(
        "Boeing (BA) Q2 2026 Earnings Call Transcript", Entity(name="BOEING CO", ticker="BA")
    )


def test_picker_rejects_another_companys_transcript_for_the_same_quarter():
    hosts = ["fool.com"]
    # only Deere published Q2 2026: L3Harris has no Q2 transcript, so nothing is picked
    assert _pick_transcript([DEERE_Q2], hosts, "Q2", "2026", LHX) is None
    # both present: the L3Harris one is picked for its own quarter
    assert _pick_transcript([DEERE_Q2, LHX_Q1], hosts, "Q1", "2026", LHX) is LHX_Q1
    # without an entity the old behaviour is unchanged (callers that do not pass one)
    assert _pick_transcript([DEERE_Q2], hosts, "Q2", "2026") is DEERE_Q2


def test_a_scraped_page_that_does_not_name_the_company_is_not_a_transcript_of_it():
    deere_head = "Deere & Company (DE) Q2 2026 Earnings Call. Operator: Good morning. John May: ..."
    lhx_head = "L3Harris Technologies (LHX) Q1 2026 Earnings Call. Operator: ... Chris Kubasik: ..."
    assert not names_company(deere_head, LHX)
    assert names_company(lhx_head, LHX)
