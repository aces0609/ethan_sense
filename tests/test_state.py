"""PLAN 1.2: two parallel Send branches writing different draft keys merge without clobbering."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from primer.state import PrimerState, dict_merge


def test_dict_merge_reducer():
    assert dict_merge({9: "a"}, {4: "b"}) == {9: "a", 4: "b"}
    assert dict_merge(None, {1: "x"}) == {1: "x"}
    assert dict_merge({1: "old"}, {1: "new"}) == {1: "new"}


def test_parallel_branches_merge_drafts():
    def fan(state):
        return [Send("branch", {"section": s}) for s in (4, 9, 13)]

    def branch(payload):
        s = payload["section"]
        return {
            "drafts": {s: {"section": s, "markdown": f"sec {s}"}},
            "claims": [
                {"section": s, "metric": "m", "value": s, "period": "FY1", "source_ids": []}
            ],
            "coverage": {s: "full"},
        }

    def start(state):
        return {}

    g = StateGraph(PrimerState)
    g.add_node("start", start)
    g.add_node("branch", branch)
    g.add_node("join", lambda s: {})
    g.add_edge(START, "start")
    g.add_conditional_edges("start", fan, ["branch"])
    g.add_edge("branch", "join")
    g.add_edge("join", END)
    out = g.compile().invoke({"inputs": {}})
    assert set(out["drafts"]) == {4, 9, 13}
    assert len(out["claims"]) == 3
    assert out["coverage"] == {4: "full", 9: "full", 13: "full"}
