"""``build_graph()`` for ``storm_correlator`` (no checkpointer: the server injects one)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from langgraph.cache.base import BaseCache
from langgraph.cache.memory import InMemoryCache
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import CachePolicy, RetryPolicy, TimeoutPolicy

from foghorn.flows.storm_correlator.context import StormContext
from foghorn.flows.storm_correlator.nodes import (
    analysis_failed,
    analyze_candidate,
    cache_key,
    finalize,
    incident_failed,
    merge_failed,
    merge_group,
    open_cluster_incident,
    partition,
    reduce_level,
    report,
)
from foghorn.flows.storm_correlator.state import Input, Output, StormState

#: explicit types: the default policy would NOT retry ValueError/RuntimeError/OSError (docs/design/02 gotcha 11)
TRANSIENT = (ConnectionError, TimeoutError, sqlite3.OperationalError)
NODE_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.05, backoff_factor=2.0, jitter=False, retry_on=TRANSIENT)


def as_node(fn: Callable[..., Any]) -> Any:
    """LangGraph's ``add_node`` overloads do not model nodes that take several injected parameters (config + store +
    runtime); this keeps the registration strictly typed everywhere else."""
    return fn


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: BaseStore | None = None,
    cache: BaseCache[Any] | None = None,
    **_deps: Any,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g = StateGraph(StormState, input_schema=Input, output_schema=Output, context_schema=StormContext)
    # defaults for EVERY node (not inherited by subgraphs): transient-error retries and a generous run timeout (async nodes).
    # cache_policy is deliberately per node: a default would also cache the incident-opening node.
    g.set_node_defaults(retry_policy=NODE_RETRY, timeout=TimeoutPolicy(run_timeout=120.0))
    g.add_node("partition", partition, destinations=("analyze_candidate", "report"), metadata={"stage": "partition"})
    g.add_node(
        "analyze_candidate",
        as_node(analyze_candidate),
        cache_policy=CachePolicy(key_func=cache_key, ttl=900),
        error_handler=analysis_failed,
        metadata={"stage": "map", "fan_out": "one Send per candidate root"},
    )
    g.add_node(
        "reduce_level",
        reduce_level,
        destinations=("merge_group", "finalize"),
        metadata={"stage": "reduce", "loop": "bounded by state.level / context.max_merge_levels"},
    )
    g.add_node("merge_group", as_node(merge_group), error_handler=merge_failed, metadata={"stage": "reduce"})
    g.add_node("finalize", finalize, defer=True, destinations=("open_cluster_incident", "report"))
    g.add_node(
        "open_cluster_incident",
        as_node(open_cluster_incident),
        error_handler=incident_failed,
        metadata={"delegates_to": "incident_commander"},
    )
    g.add_node("report", report)

    g.add_edge(START, "partition")
    g.add_edge("analyze_candidate", "reduce_level")
    g.add_edge("merge_group", "reduce_level")  # closes the map-reduce-reduce loop
    g.add_edge("open_cluster_incident", "report")
    g.add_edge("report", END)
    return g.compile(
        checkpointer=checkpointer,
        store=store,
        cache=cache if cache is not None else InMemoryCache(),
        name="storm_correlator",
    )
