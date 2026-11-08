"""Placeholder graph used until a flow's real implementation lands. Keeps `langgraph dev` bootable."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class StubState(TypedDict, total=False):
    note: str


def make_stub(graph_id: str) -> Any:
    def stub(state: StubState) -> StubState:
        return {"note": f"{graph_id}: not implemented yet"}

    g = StateGraph(StubState)
    g.add_node("stub", stub)
    g.add_edge(START, "stub")
    g.add_edge("stub", END)
    return g.compile()
