"""``build_graph``: one graph, two modes. ``START -> route`` picks ``learn`` or ``consolidate``; both end in ``finish``.

learn:       load_source -> distill -> dedupe -> curate (memory_write interrupts) -> write -> finish
consolidate: scan -> cluster -> (confirm_merge, only when something would be deleted) -> apply -> finish

No checkpointer / store of its own (the server injects both); tests pass an InMemorySaver and an InMemoryStore.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import RetryPolicy, TimeoutPolicy

from foghorn.contracts.inputs import MemoryKeeperInput
from foghorn.flows.memory_keeper import consolidate, learn
from foghorn.flows.memory_keeper.context import KeeperContext
from foghorn.flows.memory_keeper.finish import finish
from foghorn.flows.memory_keeper.state import KeeperOutput, KeeperState

MODEL_RETRY = RetryPolicy(
    max_attempts=3,
    initial_interval=0.1,
    backoff_factor=2.0,
    max_interval=2.0,
    jitter=False,
    retry_on=(TimeoutError, ConnectionError, OSError),
)
MODEL_TIMEOUT = TimeoutPolicy(run_timeout=120.0)


def route(state: KeeperState) -> Literal["load_source", "scan"]:
    mode = state.get("mode") or ("learn" if state.get("postmortem") or state.get("incident_id") else "consolidate")
    return "scan" if mode == "consolidate" else "load_source"


def after_load(state: KeeperState) -> Literal["distill", "finish"]:
    return "distill" if state.get("postmortem") else "finish"


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None, store: BaseStore | None = None, **_deps: Any
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(
        KeeperState, input_schema=MemoryKeeperInput, output_schema=KeeperOutput, context_schema=KeeperContext
    )
    g.add_node("load_source", learn.load_source)
    g.add_node("distill", learn.distill, retry_policy=MODEL_RETRY, timeout=MODEL_TIMEOUT)
    g.add_node("dedupe", learn.dedupe)
    g.add_node("curate", learn.curate)
    g.add_node("write", learn.write)
    g.add_node("scan", consolidate.scan)
    g.add_node("cluster", consolidate.cluster)
    g.add_node("confirm_merge", consolidate.confirm_merge)
    g.add_node("apply", consolidate.apply)
    g.add_node("finish", finish)
    g.add_conditional_edges(START, route, {"load_source": "load_source", "scan": "scan"})
    g.add_conditional_edges("load_source", after_load, {"distill": "distill", "finish": "finish"})
    g.add_edge("distill", "dedupe")
    g.add_edge("dedupe", "curate")
    g.add_edge("curate", "write")
    g.add_edge("write", "finish")
    g.add_edge("scan", "cluster")
    g.add_conditional_edges(
        "cluster", consolidate.needs_confirmation, {"confirm_merge": "confirm_merge", "apply": "apply"}
    )
    g.add_edge("confirm_merge", "apply")
    g.add_edge("apply", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer, store=store, name="memory_keeper")
