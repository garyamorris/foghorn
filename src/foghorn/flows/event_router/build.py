"""``build_graph()`` for ``event_router`` (compiled WITHOUT a checkpointer; the server injects one)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import httpx
from langgraph.cache.base import BaseCache
from langgraph.cache.memory import InMemoryCache
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import CachePolicy, RetryPolicy

from foghorn.flows.event_router import enrich as enrich_mod
from foghorn.flows.event_router.context import RouterContext
from foghorn.flows.event_router.nodes import (
    DECIDE_LABELS,
    after_decide,
    after_validate,
    classify,
    decide,
    enrich,
    enrich_failed,
    escalate_storm,
    fan_out,
    finish,
    ignore,
    join_incident,
    ledger,
    open_incident,
    validate,
)
from foghorn.flows.event_router.state import Input, Output, RouterState

#: errors worth retrying: connection trouble and a locked/busy SQLite file.  The default policy does NOT retry
#: ValueError / RuntimeError / OSError (docs/design/02 gotcha 11), so the types are explicit.
TRANSIENT = (ConnectionError, TimeoutError, sqlite3.OperationalError, httpx.TransportError)
ENRICH_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.05, backoff_factor=2.0, jitter=False, retry_on=TRANSIENT)
ENRICH_CACHE = CachePolicy(key_func=enrich_mod.cache_key, ttl=600)


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
    g = StateGraph(RouterState, input_schema=Input, output_schema=Output, context_schema=RouterContext)
    g.add_node("validate", validate, metadata={"stage": "ingress"})
    g.add_node("ledger", ledger, metadata={"stage": "dedupe"})
    g.add_node(
        "enrich",
        as_node(enrich),
        retry_policy=ENRICH_RETRY,
        cache_policy=ENRICH_CACHE,
        error_handler=enrich_failed,
        metadata={"stage": "enrich", "fan_out": "topology | oncall | changes"},
    )
    g.add_node("classify", classify, metadata={"stage": "classify", "llm": "fallback only (ambiguous signals)"})
    g.add_node("decide", decide, metadata={"stage": "route"})
    g.add_node(
        "open_incident",
        as_node(open_incident),
        destinations=("finish", "join_incident"),
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.2, jitter=False, retry_on=TRANSIENT),
        metadata={"delegates_to": "incident_commander"},
    )
    g.add_node("join_incident", join_incident)
    g.add_node("ignore", ignore)
    g.add_node(
        "escalate_storm",
        as_node(escalate_storm),
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.2, jitter=False, retry_on=TRANSIENT),
        metadata={"delegates_to": "storm_correlator"},
    )
    g.add_node("finish", finish)

    g.add_edge(START, "validate")
    g.add_conditional_edges("validate", after_validate, {"ledger": "ledger", "reject": "finish"})
    g.add_conditional_edges("ledger", fan_out, {"enrich": "enrich", "skip_enrichment": "classify"})
    g.add_edge("enrich", "classify")
    g.add_edge("classify", "decide")
    g.add_conditional_edges(
        "decide",
        after_decide,
        {label: route for route, label in DECIDE_LABELS.items()},
    )
    for terminal in ("join_incident", "ignore", "escalate_storm"):
        g.add_edge(terminal, "finish")
    g.add_edge("finish", END)
    return g.compile(
        checkpointer=checkpointer,
        store=store,
        cache=cache if cache is not None else InMemoryCache(),
        name="event_router",
    )
