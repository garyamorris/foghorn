"""``eval_runner``: run scenarios through the commander (Send map), score them against the truth, gate regressions."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from foghorn.flows.eval_runner.context import EvalContext
from foghorn.flows.eval_runner.nodes import aggregate, fan_out, plan, run_scenario
from foghorn.flows.eval_runner.state import BranchInput, EvalInput, EvalOutput, EvalState


def build_graph(checkpointer: Any = None, store: Any = None, **_deps: Any) -> Any:
    """``plan -> Send x N run_scenario -> aggregate(defer)``. No checkpointer unless a test passes one."""
    g = StateGraph(EvalState, input_schema=EvalInput, output_schema=EvalOutput, context_schema=EvalContext)
    g.add_node("plan", plan)
    g.add_node("run_scenario", run_scenario, input_schema=BranchInput)
    g.add_node("aggregate", aggregate, defer=True)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", fan_out, ["run_scenario", "aggregate"])
    g.add_edge("run_scenario", "aggregate")
    g.add_edge("aggregate", END)
    return g.compile(checkpointer=checkpointer, store=store, name="eval_runner")
