"""Shared fixtures: recorded EDGAR responses served through respx; output dir under tmp_path."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import respx
from httpx import Response

os.environ.setdefault("EDGAR_USER_AGENT", "ethan_sense-tests/0.1 (tests@example.com)")
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests always exercise the template path

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
LMT_CIK = "0000936468"

FAKE_10K_HTML = """<html><head><title>lmt-20251231</title><style>.x{}</style></head><body>
<div style="display:none"><ix:header>hidden xbrl header text that must not appear</ix:header></div>
<div><p><b>Item 1. Business</b></p>
<p>Lockheed Martin Corporation is a global security and aerospace company. {filler}</p>
<p>We operate in four business segments: Aeronautics, Missiles and Fire Control, Rotary and Mission Systems, and Space.</p>
<p><b>Item 1A. Risk Factors</b></p><p>Risks related to the F-35 program. {filler}</p>
<p><b>Item 2. Properties</b></p><p>Fort Worth, Texas and Marietta, Georgia. {filler}</p>
<table><tr><td>Segment</td><td>Sales</td></tr><tr><td>Aeronautics</td><td>29,000</td></tr></table>
</div></body></html>""".replace("{filler}", "Lorem ipsum dolor sit amet. " * 40)


@pytest.fixture(scope="session")
def fixture_json():
    def load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def edgar_mock(fixture_json):
    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.sec.gov/files/company_tickers_exchange.json").mock(
            return_value=Response(200, json=fixture_json("company_tickers_exchange.json"))
        )
        router.get(f"https://data.sec.gov/submissions/CIK{LMT_CIK}.json").mock(
            return_value=Response(200, json=fixture_json("submissions_LMT.json"))
        )
        router.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{LMT_CIK}.json").mock(
            return_value=Response(200, json=fixture_json("companyfacts_LMT.json"))
        )
        router.get(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/936468/.*\.htm$").mock(
            return_value=Response(200, text=FAKE_10K_HTML)
        )
        yield router


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    out = tmp_path / "output"
    monkeypatch.setenv("PRIMER_OUTPUT_DIR", str(out))
    return out
