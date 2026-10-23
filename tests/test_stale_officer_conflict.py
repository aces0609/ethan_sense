"""A 2019 merger release naming the old CEO must not be printed as a conflict with the 2026
appointment, and a conflict note must not trip section 14's recency rule (L3Harris, 2026-10-22)."""

from __future__ import annotations

from datetime import date

from primer.evidence.store import EvidenceStore
from primer.graph.reconcile import Fact, _historical_officer_pair
from primer.validate.rules import check_recency

AS_OF = date(2026, 10, 22)


def _store(tmp_path):
    store = EvidenceStore(tmp_path / "run", as_of=AS_OF)
    old = store.register(
        "Harris and L3 set closing date",
        type="10-K",
        publisher="www.sec.gov",
        title="merger release",
        url="https://www.sec.gov/Archives/edgar/data/202058/x/merger.htm",
        tier=1,
        published_date=date(2019, 6, 29),
    )
    new = store.register(
        "Mehta appointed CEO",
        type="8-K",
        publisher="www.sec.gov",
        title="8-K",
        url="https://www.sec.gov/Archives/edgar/data/202058/y/8k.htm",
        tier=1,
        published_date=date(2026, 8, 17),
    )
    return store, old.source_id, new.source_id


def _fact(sec, value, sids):
    return Fact(f"f{sec}", sec, "ceo", "company", value, "text", sids, "prose")


def test_officer_named_in_a_source_years_older_is_not_a_conflict(tmp_path):
    store, old, new = _store(tmp_path)
    assert _historical_officer_pair(
        _fact(7, "William Brown", [old]), _fact(14, "Sam Mehta", [new]), store
    )
    assert not _historical_officer_pair(
        _fact(8, "Kimberly Fields", [new]), _fact(14, "Sam Mehta", [new]), store
    )


def test_recency_ignores_the_conflict_note(tmp_path):
    store, old, new = _store(tmp_path)
    md = f"> Conflict: ceo reported as A [{old}] (section 7) and B [{new}] (section 14).\n\n- fact [{new}]"
    assert check_recency({14: md}, store, AS_OF)["ok"]
    assert not check_recency({14: f"- stale [{old}]"}, store, AS_OF)["ok"]
