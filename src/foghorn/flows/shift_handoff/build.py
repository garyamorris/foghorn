"""``build_graph``: the handoff as a linear, durable StateGraph. No checkpointer / store of its own (the server injects both)."""

from __future__ import annotations

import itertools
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from foghorn.contracts.inputs import ShiftHandoffInput
from foghorn.flows.shift_handoff import nodes
from foghorn.flows.shift_handoff.context import HandoffContext
from foghorn.flows.shift_handoff.state import NODES, HandoffOutput, HandoffState


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None, store: BaseStore | None = None, **_deps: Any
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(
        HandoffState, input_schema=ShiftHandoffInput, output_schema=HandoffOutput, context_schema=HandoffContext
    )
    g.add_node("collect_incidents", nodes.collect_incidents)
    g.add_node("collect_approvals", nodes.collect_approvals)
    g.add_node("compute_metrics", nodes.compute_metrics_node)
    g.add_node("reconcile", nodes.reconcile_node)
    g.add_node("render_report", nodes.render_report)
    g.add_node("publish", nodes.publish)
    g.add_edge(START, NODES[0])
    for a, b in itertools.pairwise(NODES):
        g.add_edge(a, b)
    g.add_edge(NODES[-1], END)
    return g.compile(checkpointer=checkpointer, store=store, name="shift_handoff")
