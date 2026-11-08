"""Unit: the fake itself. If these fail, every other test is meaningless."""

from __future__ import annotations

import asyncio
import warnings

import pytest
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from foghorn.models import offline as o

warnings.filterwarnings("ignore")


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"


class Verdict(BaseModel):
    ok: bool
    score: float = Field(ge=0, le=1)
    reasons: list[str]


def test_stock_fakes_cannot_do_tools_or_structured_output():
    """Why we write our own: the langchain_core fakes raise NotImplementedError for both."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    m = GenericFakeChatModel(messages=iter(["x"]))
    with pytest.raises(NotImplementedError):
        m.bind_tools([lookup])
    with pytest.raises(NotImplementedError):
        m.with_structured_output(Verdict)


def test_structured_output_default_synthesis_validates_against_schema():
    out = o.OfflineChatModel().with_structured_output(Verdict).invoke("anything")
    assert isinstance(out, Verdict) and 0 <= out.score <= 1


def test_structured_output_include_raw_and_parse_error_captured():
    bad = o.Script().when("bad", o.schema("Verdict"), o.say("not json at all"))
    res = o.OfflineChatModel(script=bad).with_structured_output(Verdict, include_raw=True).invoke("x")
    assert res["parsed"] is None and res["parsing_error"] is not None and res["raw"].content == "not json at all"


def test_rules_key_on_state_not_call_order_so_replay_is_stable():
    s = o.Script().when("r", o.human("refund"), o.say("R")).when("o", o.human("order"), o.say("O"))
    m = o.OfflineChatModel(script=s)
    answers = [m.invoke([HumanMessage(t)]).content for t in ("refund", "order", "refund", "refund")]
    assert answers == ["R", "O", "R", "R"]  # a call-counter script would have run out / drifted


def test_scripted_tool_call_is_validated_against_bound_tools():
    s = o.Script().when("typo", o.human("x"), o.call_tool("looku", order_id="1"))
    bound = o.OfflineChatModel(script=s).bind_tools([lookup])
    with pytest.raises(o.OfflineScriptError, match="looku"):
        bound.invoke("x")
    s2 = o.Script().when("missing", o.human("x"), o.call_tool("lookup"))
    with pytest.raises(o.OfflineScriptError, match="required"):
        o.OfflineChatModel(script=s2).bind_tools([lookup]).invoke("x")


@pytest.mark.parametrize("native", [True, False], ids=["ProviderStrategy", "ToolStrategy"])
def test_create_agent_tool_loop_and_structured_response_both_strategies(native):
    m = o.OfflineChatModel(native_structured_output=native)
    agent = create_agent(m, [lookup], response_format=ToolStrategy(Verdict) if not native else Verdict)
    res = agent.invoke({"messages": [("user", "check order 5")]})
    assert isinstance(res["structured_response"], Verdict)
    assert any(getattr(x, "tool_calls", None) for x in res["messages"])  # it really used the tool first


def test_sync_stream_yields_tokens_toolcall_fragments_and_usage():
    s = o.Script().when("t", o.human("go"), o.call_tool("lookup", order_id="A-17"))
    chunks = list(o.OfflineChatModel(script=s).bind_tools([lookup]).stream("go"))
    merged = chunks[0]
    for c in chunks[1:]:
        merged = merged + c
    assert isinstance(merged, AIMessageChunk)
    assert merged.tool_calls[0]["args"] == {"order_id": "A-17"}  # fragments re-assemble to valid JSON
    assert sum(1 for c in chunks if c.tool_call_chunks and c.tool_call_chunks[0]["args"]) >= 2  # really fragmented
    assert merged.usage_metadata and merged.usage_metadata["total_tokens"] > 0


def test_async_stream_text_is_token_by_token():
    s = o.Script().when("t", o.human("hi"), o.say("one two three"))

    async def go():
        return [c.content async for c in o.OfflineChatModel(script=s).astream("hi") if c.content]

    assert asyncio.run(go()) == ["one ", "two ", "three"]


def test_token_delay_uses_asyncio_sleep_not_blocking():
    """A blocking sleep would trip blockbuster under `langgraph dev`; this must yield to the loop."""
    s = o.Script().when("t", o.human("hi"), o.say("a b c"))
    m = o.OfflineChatModel(script=s, token_delay_s=0.01)

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.002)
                ticks += 1

        t = asyncio.create_task(ticker())
        _ = [c async for c in m.astream("hi")]
        t.cancel()
        return ticks

    assert asyncio.run(go()) >= 3  # the loop kept running while the model "streamed"


def test_scenario_and_node_visible_to_rules_via_config_and_run_metadata():
    from langchain_core.messages import AIMessage

    seen = {}

    def spy(c: o.Call):
        seen.update(scenario=c.scenario, node=c.node, thread=c.thread_id)
        return o.say("ok")

    m = o.OfflineChatModel(script=o.Script().when("spy", lambda c: True, spy))
    # direct (non-graph) invocation: only `metadata` reaches the model. Inside a graph node `configurable` and
    # runtime.context also work (see tests/graphs/test_triage_graph.py::test_scenario_reaches_model_in_graph).
    m.invoke(
        "hi", config={"metadata": {"langgraph_node": "triage", "offline_scenario": "angry_customer", "thread_id": "T1"}}
    )
    assert seen == {"scenario": "angry_customer", "node": "triage", "thread": "T1"}
    assert isinstance(m.invoke("hi"), AIMessage)


def test_synthesize_covers_schema_shapes_deterministically():
    from typing import Literal

    class Inner(BaseModel):
        n: int = Field(ge=5, le=9)
        flag_needs_revision: bool
        ok: bool

    class Wide(BaseModel):
        kind: Literal["a", "b"]
        inner: Inner
        tags: list[str]
        maybe: str | None = None
        when: str = Field(json_schema_extra={"format": "date-time"})
        score: float = Field(ge=0, le=1)
        mapping: dict[str, int] = {}

    schema = Wide.model_json_schema()
    a, b = o.synthesize(schema, "seed"), o.synthesize(schema, "seed")
    assert a == b  # deterministic
    parsed = Wide.model_validate(a)
    assert parsed.inner.flag_needs_revision is False and parsed.inner.ok is True and 5 <= parsed.inner.n <= 9
    assert o.synthesize(schema, "other")["kind"] in ("a", "b")


def test_default_reply_paths_and_script_fallback_recording():
    m = o.OfflineChatModel(role="fast")
    assert m.invoke("hello there").content.startswith("[offline:fast] hello there")
    forced = m.bind_tools([lookup], tool_choice="lookup").invoke("x")
    assert forced.tool_calls[0]["name"] == "lookup"
    after_tool = m.bind_tools([lookup]).invoke(
        [
            HumanMessage("x"),
            forced,
            __import__("langchain_core.messages", fromlist=["ToolMessage"]).ToolMessage(
                "42", tool_call_id=forced.tool_calls[0]["id"], name="lookup"
            ),
        ]
    )
    assert "Based on the tool results" in after_tool.content
    assert list(m.script.matched)[-3:] == ["<fallback>"] * 3 and len(m.script.calls) >= 3
    err = o.Script().when("e", o.human("x"), o.fail(TimeoutError("llm timeout")))
    with pytest.raises(TimeoutError):
        o.OfflineChatModel(script=err).invoke("x")  # failure injection for retry/fallback tests
    m2 = o.OfflineChatModel(
        script=o.Script().when(
            "m", o.human("x"), lambda c: __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage("raw")
        )
    )
    assert m2.invoke("x").content == "raw"
    with pytest.raises(o.OfflineScriptError):
        o.Script().when("bad", o.human("x"), lambda c: 42).decide(
            o.OfflineChatModel()._make_call([HumanMessage("x")], None, {})
        )


def test_hash_embeddings_are_deterministic_unit_vectors_and_rank_by_overlap():
    import math

    from foghorn.embeddings import DIMS, hash_embed

    a, b = hash_embed("refunds by bank transfer"), hash_embed("refund via bank transfer")
    c = hash_embed("peanut allergy")
    assert (
        len(a) == DIMS
        and abs(math.sqrt(sum(x * x for x in a)) - 1) < 1e-9
        and a == hash_embed("refunds by bank transfer")
    )
    dot = lambda u, v: sum(x * y for x, y in zip(u, v, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)
