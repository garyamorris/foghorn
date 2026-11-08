"""``rca_tournament``: Send map-reduce over competing root-cause hypotheses.

    START -> gather -> plan --Send x N--> score_hypothesis (async, per-Send timeout) --> rank (defer=True) -> report -> END

Standalone it takes ``RcaInput`` and gathers its own evidence; embedded in the incident commander (``embedded=True``) it
receives the commander's evidence through the shared state keys and ends at ``rank``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foghorn.contracts.inputs import RcaInput
from foghorn.flows.rca_tournament import nodes
from foghorn.flows.rca_tournament.state import RcaOutput, RcaState


@dataclass
class RcaContext:
    model_profile: str = (
        "competent"  # competent | naive: how the tournament weighs evidence (naive blames the nearest change)
    )
    scenario_id: str | None = None
    # run-context keys the dispatcher can send to any graph; the tournament accepts and ignores them
    as_of: str | None = None
    approval_policy: str = "standard"
    breakpoints: list[str] = field(default_factory=list)
    memory_enabled: bool = True
    chaos: str | None = None
    token_delay_ms: int = 0


def build_graph(
    checkpointer: Any = None, store: Any = None, *, embedded: bool = False
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g: StateGraph[Any, Any, Any, Any]
    if embedded:
        g = StateGraph(RcaState, context_schema=RcaContext)
    else:
        g = StateGraph(RcaState, context_schema=RcaContext, input_schema=RcaInput, output_schema=RcaOutput)
    g.add_node("gather", nodes.gather, metadata={"kind": "tools", "llm": False})
    g.add_node("plan", nodes.plan, destinations=("score_hypothesis", "rank"), metadata={"kind": "map"})
    g.add_node(
        "score_hypothesis", cast(Any, nodes.score_hypothesis), metadata={"kind": "worker", "fan_out": "per hypothesis"}
    )
    g.add_node("rank", nodes.rank, defer=True, metadata={"kind": "reduce"})
    g.add_edge(START, "gather")
    g.add_edge("gather", "plan")
    g.add_edge("score_hypothesis", "rank")
    if embedded:
        g.add_edge("rank", END)
    else:
        g.add_node("report", nodes.report)
        g.add_edge("rank", "report")
        g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer, store=store, name="rca_tournament")
