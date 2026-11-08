"""Resilience: retries, fallbacks, limits and error surfacing, driven by failure injection in the offline model."""

from __future__ import annotations

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from typing_extensions import TypedDict

from foghorn.models import offline as o

pytestmark = pytest.mark.asyncio


def model(script: o.Script, role: str = "smart") -> o.OfflineChatModel:
    return o.OfflineChatModel(role=role, script=script)


async def test_model_retry_middleware_recovers_from_transient_failures():
    script = o.Script().when("flaky", lambda c: True, o.flaky(2, TimeoutError("llm timeout"), "recovered"))
    agent = create_agent(
        model(script), [], middleware=[ModelRetryMiddleware(max_retries=2, initial_delay=0.0, jitter=False)]
    )
    out = await agent.ainvoke({"messages": [("user", "hi")]})
    assert out["messages"][-1].content == "recovered" and len(script.calls) == 3


async def test_retry_exhausted_surfaces_as_error_message_not_silent_success():
    script = o.Script().when("dead", lambda c: True, lambda c: o.fail(TimeoutError("down")))
    agent = create_agent(
        model(script),
        [],
        middleware=[ModelRetryMiddleware(max_retries=1, initial_delay=0.0, jitter=False, on_failure="error")],
    )
    with pytest.raises(TimeoutError):
        await agent.ainvoke({"messages": [("user", "hi")]})


async def test_model_fallback_middleware_uses_secondary_model():
    primary = model(o.Script().when("dead", lambda c: True, lambda c: o.fail(ConnectionError("503"))), "smart")
    backup = model(o.Script().when("ok", lambda c: True, o.say("from backup")), "fast")
    agent = create_agent(primary, [], middleware=[ModelFallbackMiddleware(backup)])
    assert (await agent.ainvoke({"messages": [("user", "hi")]}))["messages"][-1].content == "from backup"


async def test_model_call_limit_stops_a_runaway_tool_loop():
    from langchain_core.tools import tool

    @tool
    def ping() -> str:
        """Ping."""
        return "pong"

    loop = o.Script().when("forever", lambda c: True, o.call_tool("ping"))  # model never stops calling tools
    agent = create_agent(model(loop), [ping], middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")])
    out = await agent.ainvoke({"messages": [("user", "go")]}, {"recursion_limit": 50})
    assert len(loop.calls) == 3 and "limit" in out["messages"][-1].content.lower()


async def test_graph_node_retry_policy_with_flaky_node():
    class S(TypedDict, total=False):
        out: str

    attempts = {"n": 0}

    async def node(state: S) -> S:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")
        return {"out": "ok"}

    g = StateGraph(S)
    g.add_node(
        "n",
        node,
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.0, jitter=False, retry_on=ConnectionError),
    )
    g.add_edge(START, "n")
    g.add_edge("n", END)
    assert (await g.compile().ainvoke({}))["out"] == "ok" and attempts["n"] == 3


async def test_recursion_limit_raises_graph_recursion_error():
    from langgraph.errors import GraphRecursionError

    loop = o.Script().when("forever", lambda c: True, o.call_tool("ping"))
    from langchain_core.tools import tool

    @tool
    def ping() -> str:
        """Ping."""
        return "pong"

    with pytest.raises(GraphRecursionError):
        await create_agent(model(loop), [ping]).ainvoke({"messages": [("user", "go")]}, {"recursion_limit": 6})


async def test_structured_error_events_on_custom_stream_instead_of_a_dead_run():
    """Dashboard-friendly failure: the node converts an exception into a typed `custom` event + safe state, so
    the UI can render an error card while the run still completes (and the exception is still logged)."""
    from langgraph.config import get_stream_writer

    class S(TypedDict, total=False):
        out: str
        error: dict

    async def risky(state: S) -> S:
        writer = get_stream_writer()
        try:
            raise TimeoutError("upstream took 61s")
        except TimeoutError as e:
            err = {"type": "error", "code": "upstream_timeout", "retryable": True, "message": str(e)}
            writer(err)  # custom stream event
            return {"error": err, "out": "degraded"}

    g = StateGraph(S)
    g.add_node("risky", risky)
    g.add_edge(START, "risky")
    g.add_edge("risky", END)
    events = [e async for e in g.compile().astream({}, stream_mode=["custom", "values"])]
    custom = [p for m, p in events if m == "custom"]
    assert custom == [{"type": "error", "code": "upstream_timeout", "retryable": True, "message": "upstream took 61s"}]
    assert events[-1][1]["out"] == "degraded"
