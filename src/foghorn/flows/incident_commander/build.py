"""``incident_commander``: hand-rolled supervisor over five specialist subgraphs, the RCA tournament, a policy gate, human
approval, idempotent execution, verification and communications.

    START -> bootstrap -> supervisor <-+------------------------------------------------------------------+
                            | Command(goto / Send)                                                        |
                            +--> metrics_analyst | logs_analyst | change_analyst | topology_analyst | knowledge_agent (create_agent subgraphs, parallel)
                            +--> rca_tournament (compiled subgraph, Send map-reduce inside)  ------------+
                            +--> clarify_change_intent (question interrupt)  ----------------------------+
                            +--> remediation_planner -> policy_check -> approval_gate -> execute_action -> verify_recovery --+
                            |                               ^   (reject / edit / ask / failed verification loop back, bounded)  |
                            +--> comms_drafter  (draft; opens comms_gate together with approval_gate via policy_check) -----------+
                                                                                         close_incident (defer) <----------------+
"""

from __future__ import annotations

import hashlib
from typing import Any

from langgraph.cache.memory import InMemoryCache
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import CachePolicy, RetryPolicy

from foghorn.contracts.inputs import CommanderInput
from foghorn.flows.incident_commander import nodes
from foghorn.flows.incident_commander.context import Context
from foghorn.flows.incident_commander.investigation import AGENTS
from foghorn.flows.incident_commander.scripts import make_smart_script
from foghorn.flows.incident_commander.specialists import build_specialists
from foghorn.flows.incident_commander.state import CommanderIO, State
from foghorn.flows.rca_tournament.build import build_graph as build_rca
from foghorn.models.factory import get_chat_model

#: transient failures (flaky tools, provider hiccups) are retried; logic errors are not (docs/GRAPH_GUIDE.md section 10)
RETRY = RetryPolicy(
    max_attempts=3,
    initial_interval=0.01,
    backoff_factor=2.0,
    max_interval=0.1,
    jitter=False,
    retry_on=(ConnectionError, TimeoutError),
)


def analysis_key(inp: Any) -> str:
    """Cache key of a read-only specialist run: the scenario, the virtual time it looks at, the world it sees, the task.
    (The tool results are a pure function of exactly these, so a replay of the same thread is served from the cache.)"""
    if isinstance(inp, dict):
        msgs = inp.get("messages") or []
        text = str(getattr(msgs[0], "content", "")) if msgs else ""
        scope = f"{inp.get('scenario_id')}|{inp.get('as_of')}|{len(inp.get('applied_actions') or [])}"
        return scope + "|" + hashlib.sha1(text.encode()).hexdigest()  # noqa: S324 - cache key, not security
    return hashlib.sha1(repr(inp).encode()).hexdigest()  # noqa: S324


ANALYSIS_CACHE = CachePolicy(key_func=analysis_key, ttl=600)


def as_node(fn: Any) -> Any:
    """``add_node``'s overloads do not model nodes with several injected parameters (config + runtime + store)."""
    return fn


def build_graph(
    checkpointer: Any = None, store: Any = None, *, cache: Any = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    g: StateGraph[State, Context, CommanderInput, CommanderIO] = StateGraph(
        State, context_schema=Context, input_schema=CommanderInput, output_schema=CommanderIO
    )
    specialists = build_specialists(get_chat_model("smart", offline_script=make_smart_script()))
    g.add_node("bootstrap", as_node(nodes.bootstrap), metadata={"kind": "context", "llm": False})
    g.add_node(
        "supervisor",
        as_node(nodes.supervisor),
        destinations=(*AGENTS, "rca_tournament", "clarify_change_intent", "remediation_planner", "comms_drafter"),
        retry_policy=RETRY,
        metadata={"kind": "supervisor", "prompt": "supervisor_prompt"},
    )
    for name in AGENTS:  # compiled create_agent subgraphs as NODES: Studio xray drills into them
        g.add_node(
            name,
            specialists[name],
            retry_policy=RETRY,
            cache_policy=ANALYSIS_CACHE,
            metadata={"kind": "specialist", "prompt": "analyst_prompt"},
        )
    g.add_node("rca_tournament", build_rca(embedded=True), metadata={"kind": "subgraph", "graph": "rca_tournament"})
    g.add_node(
        "clarify_change_intent",
        as_node(nodes.clarify_change_intent),
        metadata={"kind": "human", "interrupt": "question"},
    )
    g.add_node(
        "remediation_planner",
        as_node(nodes.remediation_planner),
        retry_policy=RETRY,
        metadata={"kind": "planner", "prompt": "planner_prompt"},
    )
    g.add_node(
        "policy_check",
        as_node(nodes.policy_check),
        destinations=("approval_gate", "comms_gate", "remediation_planner"),
        metadata={"kind": "guardrail", "llm": False},
    )
    g.add_node(
        "approval_gate",
        as_node(nodes.approval_gate),
        metadata={"kind": "human", "interrupt": "approval|edit|break_glass"},
    )
    g.add_node("answer_question", as_node(nodes.answer_question), retry_policy=RETRY, metadata={"kind": "agent"})
    g.add_node(
        "execute_action",
        as_node(nodes.execute_action),
        destinations=("verify_recovery", "policy_check", "remediation_planner", "close_incident"),
        retry_policy=RETRY,
        metadata={"kind": "effect", "idempotent": True},
    )
    g.add_node(
        "verify_recovery",
        as_node(nodes.verify_recovery),
        destinations=("remediation_planner", "close_incident"),
        retry_policy=RETRY,
        metadata={"kind": "tools"},
    )
    g.add_node(
        "comms_drafter",
        as_node(nodes.comms_drafter),
        destinations=("close_incident",),
        retry_policy=RETRY,
        metadata={"kind": "agent"},
    )
    g.add_node(
        "comms_gate",
        as_node(nodes.comms_gate),
        destinations=("close_incident",),
        metadata={"kind": "human", "interrupt": "edit"},
    )
    g.add_node(
        "close_incident", as_node(nodes.close_incident), defer=True, retry_policy=RETRY, metadata={"kind": "report"}
    )

    g.add_edge(START, "bootstrap")
    g.add_edge("bootstrap", "supervisor")
    for name in (*AGENTS, "rca_tournament", "clarify_change_intent"):
        g.add_edge(name, "supervisor")
    g.add_edge("remediation_planner", "policy_check")
    g.add_conditional_edges("approval_gate", nodes.route_gate, list(nodes.GATE_TARGETS))
    g.add_edge("answer_question", "approval_gate")
    g.add_edge("close_incident", END)
    return g.compile(
        checkpointer=checkpointer,
        store=store,
        cache=cache if cache is not None else InMemoryCache(),
        name="incident_commander",
    )
