"""PLAN 1.4: evidence store idempotency, hashing, sources.json; HTML → text; Item chunking."""

from __future__ import annotations

import json
from datetime import date

from primer.evidence.chunking import chunk_by_item, find_chunks
from primer.evidence.html import html_to_text
from primer.evidence.store import EvidenceStore
from tests.conftest import FAKE_10K_HTML


def test_register_is_idempotent_and_hash_tracks_content(tmp_path):
    store = EvidenceStore(tmp_path, as_of=date(2026, 10, 21))
    a = store.register("hello", type="news", publisher="X", title="t", url="https://x.test/a")
    b = store.register("hello", type="news", publisher="X", title="t", url="https://x.test/a")
    assert a.source_id == b.source_id == "src_0001"
    c = store.register("other", type="news", publisher="X", title="t2", url="https://x.test/b")
    assert c.source_id == "src_0002" and c.content_hash != a.content_hash
    assert (tmp_path / "evidence" / "src_0001.md").read_text(encoding="utf-8") == "hello"
    # reload from disk sees the same ids
    again = EvidenceStore(tmp_path, as_of=date(2026, 10, 21))
    assert again.get("src_0002").title == "t2"
    assert (
        again.register("x", type="news", publisher="X", title="t", url="https://x.test/a").source_id
        == "src_0001"
    )


def test_tier_and_sources_json(tmp_path):
    store = EvidenceStore(tmp_path, as_of=date(2026, 10, 21))
    sec = store.register(
        "f", type="10-K", publisher="SEC", title="k", url="https://www.sec.gov/Archives/x.htm"
    )
    news = store.register(
        "n", type="news", publisher="Paper", title="n", url="https://news.example.com/a"
    )
    assert sec.tier == 1 and news.tier == 2
    store.write_sources_json({sec.source_id: ["sec-9"]})
    data = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    assert [s["source_id"] for s in data["sources"]] == [sec.source_id]
    assert data["sources"][0]["cited_by"] == ["sec-9"]


def test_html_to_text_drops_hidden_and_keeps_body():
    text = html_to_text(FAKE_10K_HTML)
    assert "hidden xbrl header" not in text
    assert "global security and aerospace company" in text
    assert "Aeronautics | $ | 30,200" in text


def test_chunk_by_item():
    chunks = chunk_by_item(html_to_text(FAKE_10K_HTML))
    items = [c.item for c in chunks]
    assert items[:3] == ["1", "1A", "2"] and {"7", "8", "10"} <= set(items)
    assert find_chunks(chunks, "cost overruns")[0].item == "1A"


def test_chunk_by_item_reads_headings_laid_out_in_table_cells():
    """L3Harris's 10-K renders headings as '| ITEM 1. | BUSINESS.'; the chunker once found only
    Item 16 in it, so section 3 had no Item 1 (2026-10-22)."""
    body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 12
    text = (
        "| ITEM 1. | BUSINESS. |\n" + body + "\n\n"
        "| ITEM 1A. | RISK FACTORS. |\n" + body + "\n\n"
        "| ITEM 7. | MANAGEMENT'S DISCUSSION |\n" + body + "\n\n"
        "ITEM 8. FINANCIAL STATEMENTS\n" + body + "\n"
    )
    chunks = chunk_by_item(text)
    assert [c.item for c in chunks] == ["1", "1A", "7", "8"]
    assert chunks[0].title.startswith("BUSINESS") and "|" not in chunks[0].title
    assert chunks[2].title.startswith("MANAGEMENT")
