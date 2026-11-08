"""``build_graph()`` for ``change_gate`` (no checkpointer: the server injects one; tests pass ``InMemorySaver()``).

Static breakpoint: ``build_graph(breakpoint=True)`` compiles ``interrupt_before=["apply_change"]``; the same pause is
available per run (``runs.create(..., interrupt_before=["apply_change"])`` / Studio's Interrupt dropdown) without a
rebuild.  Resume a breakpoint with ``input=None``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.cache.base import BaseCache
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from foghorn.flows.change_gate.context import GateContext
from foghorn.flows.change_gate.nodes import (
    answer_question,
    apply_change,
    assess_risk,
    break_glass,
    clarify,
    collect_reviews,
    edit_change,
    finish,
    four_eyes,
    policy_gate,
    request_approval,
    reviewer,
    revise_change,
)
from foghorn.flows.change_gate.state import GateState, Input, Output


def as_node(fn: Callable[..., Any]) -> Any:
    """LangGraph's ``add_node`` overloads do not model nodes that take several injected parameters (config + store +
    runtime); this keeps the registration strictly typed everywhere else."""
    return fn


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: BaseStore | None = None,
    cache: BaseCache[Any] | None = None,
    *,
    breakpoint: bool = False,
    **_deps: Any,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(GateState, input_schema=Input, output_schema=Output, context_schema=GateContext)
    g.add_node("assess_risk", assess_risk, metadata={"stage": "assess"})
    g.add_node("policy_gate", policy_gate, metadata={"stage": "policy"})
    g.add_node("clarify", clarify, metadata={"interrupt": "question"})
    g.add_node("break_glass", break_glass, metadata={"interrupt": "break_glass"})
    g.add_node("request_approval", as_node(request_approval), metadata={"interrupt": "approval"})
    g.add_node("edit_change", edit_change, metadata={"interrupt": "edit"})
    g.add_node("revise_change", revise_change, metadata={"loop": "bounded by context.max_revisions"})
    g.add_node("answer_question", answer_question, metadata={"llm": "smart, grounded in tools"})
    g.add_node("four_eyes", four_eyes, metadata={"fan_out": "one reviewer per role"})
    g.add_node("reviewer", as_node(reviewer), metadata={"interrupt": "four_eyes (parallel)"})
    # no defer=True needed: both reviewer tasks belong to ONE superstep, so the fan-in only runs after both have decided
    g.add_node("collect_reviews", collect_reviews, metadata={"fan_in": "both reviewers"})
    g.add_node("apply_change", apply_change, metadata={"side_effect": "after every interrupt, claim_effect-idempotent"})
    g.add_node("finish", finish)

    g.add_edge(START, "assess_risk")
    g.add_edge("assess_risk", "policy_gate")
    g.add_edge("answer_question", "request_approval")
    g.add_edge("reviewer", "collect_reviews")
    g.add_edge("apply_change", "finish")
    g.add_edge("finish", END)
    return g.compile(
        checkpointer=checkpointer,
        store=store,
        cache=cache,
        interrupt_before=["apply_change"] if breakpoint else None,
        name="change_gate",
    )
