"""The shared vocabulary every graph, the bus and the UI depend on. A bug here is inherited everywhere."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError
from typing_extensions import TypedDict

from foghorn.common import delegate as delegate_mod
from foghorn.common import lifecycle
from foghorn.common.stream import STREAM_KINDS, emit, stream_payload
from foghorn.contracts.events import ALL_TYPES, EventEnvelope
from foghorn.contracts.interrupts import (
    RESPONSE_SCHEMAS,
    Answer,
    Decision,
    PolicyCheck,
    build_interrupt,
    response_schema_for,
)
from foghorn.contracts.meta import GRAPH_IDS, M_GRAPH, M_PARENT_THREAD, det_thread_id, thread_metadata
from tests.helpers import run_graph

# --------------------------------------------------------------------------------------------- events


def test_envelope_roundtrips_as_plain_json_and_forbids_unknown_fields():
    env = EventEnvelope(type="alert.fired", source="alertmanager", severity="high", payload={"rule": "error_rate_high"})
    state = env.to_state()
    assert state["schema_version"] == "foghorn.event/1"
    assert state["subject"] == {"component": None, "environment": "prod"}
    assert EventEnvelope.model_validate(state) == env  # lossless
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({"type": "x", "surprise": 1})


def test_every_documented_event_type_is_registered():
    for t in ("alert.fired", "deploy.finished", "incident.opened", "approval.decided", "dlq.added", "change.requested"):
        assert t in ALL_TYPES


# ----------------------------------------------------------------------------------------------- meta


def test_deterministic_thread_ids_are_stable_forever():
    # Frozen value: changing THREAD_NS or the join format would orphan every deterministic thread.
    assert det_thread_id("incident_commander", "INC-0001") == det_thread_id("incident_commander", "INC-0001")
    assert det_thread_id("incident_commander", "INC-0001") != det_thread_id("event_router", "INC-0001")
    assert det_thread_id("event_router", "evt_1") == "d59c7787-97e8-53f1-91dc-041daf3f27c9"


def test_thread_metadata_is_flat_and_drops_none():
    md = thread_metadata("incident", "incident_commander", foghorn_incident_id="INC-1", foghorn_event_id=None)
    assert md == {"foghorn_kind": "incident", M_GRAPH: "incident_commander", "foghorn_incident_id": "INC-1"}
    assert all(not isinstance(v, dict | list) for v in md.values())


def test_graph_ids_match_langgraph_json():
    import json
    from pathlib import Path

    cfg = json.loads((Path(__file__).resolve().parents[2] / "langgraph.json").read_text())
    assert set(GRAPH_IDS) == set(cfg["graphs"])


# ------------------------------------------------------------------------------------------ interrupts


def test_build_interrupt_is_json_and_carries_generated_form_inputs():
    payload = build_interrupt(
        "approval",
        approval_id="apr_1",
        title="Roll back checkout?",
        summary="error rate 7% since deploy",
        risk="high",
        blast_radius=["checkout-service"],
        policy_checks=[PolicyCheck(id="POL-001", status="warn", text="change freeze in 2h")],
        incident_id="INC-1",
    )
    assert (
        payload["v"] == 1
        and payload["kind"] == "approval"
        and payload["options"] == ["approve", "edit", "reject", "ask"]
    )
    assert payload["policy"]["checks"][0] == {"id": "POL-001", "status": "warn", "text": "change freeze in 2h"}
    import json

    json.dumps(payload)  # checkpointable and streamable


def test_response_schemas_validate_resume_values():
    assert set(RESPONSE_SCHEMAS) >= {"approval", "question", "memory_write", "four_eyes"}
    assert Decision.model_validate({"decision": "approve"}).decided_by == "operator"
    with pytest.raises(ValidationError):
        Decision.model_validate({"decision": "maybe"})
    assert response_schema_for("question") is Answer


class _S(TypedDict, total=False):
    answer: str


async def test_interrupt_with_response_schema_rejects_bad_resume_then_accepts_good_one():
    from langgraph.types import interrupt

    def ask(_: _S) -> _S:
        d = interrupt(build_interrupt("approval", approval_id="a", title="t", summary="s"), response_schema=Decision)
        return {"answer": d.decision if isinstance(d, Decision) else d["decision"]}

    g = StateGraph(_S)
    g.add_node("ask", ask)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    graph = g.compile(checkpointer=InMemorySaver())
    value, seen = await run_graph(graph, {}, resumes=[{"decision": "reject"}])
    assert value["answer"] == "reject" and seen[0].value["kind"] == "approval"


# ---------------------------------------------------------------------------------------- stream events


def test_stream_payload_shape_and_kinds():
    p = stream_payload("evidence.found", "evidence:1", agent="metrics_analyst", id="e1", strength=0.9)
    assert p == {
        "v": 1,
        "kind": "evidence.found",
        "key": "evidence:1",
        "agent": "metrics_analyst",
        "t_virtual": None,
        "data": {"id": "e1", "strength": 0.9},
    }
    assert "series" in STREAM_KINDS and "hypothesis.update" in STREAM_KINDS


def test_emit_outside_a_run_is_a_silent_noop():
    emit("note", "k", text="hello")  # must not raise


async def test_emit_inside_a_run_arrives_on_the_custom_stream():
    def node(_: _S) -> _S:
        emit("progress", "p:1", label="warming up", pct=10)
        return {}

    g = StateGraph(_S)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    graph = g.compile()
    seen = [c async for c in graph.astream({}, stream_mode="custom")]
    assert seen == [stream_payload("progress", "p:1", label="warming up", pct=10)]


# ----------------------------------------------------------------------------------------- lifecycle


def test_lifecycle_capture_collects_envelopes_and_never_raises():
    with lifecycle.capture() as events:
        lifecycle.emit_event(
            "approval.requested",
            idempotency_key="approval.requested:a1",
            component="checkout-service",
            payload={"x": 1},
        )
        lifecycle.emit_event("totally.unknown.type", idempotency_key="k2")  # warns, still delivered
    assert [e["type"] for e in events] == ["approval.requested", "totally.unknown.type"]
    assert events[0]["source"] == "foghorn.internal"
    assert events[0]["idempotency_key"] == "approval.requested:a1"
    assert events[0]["subject"]["component"] == "checkout-service"


def test_a_failing_sink_cannot_break_a_run():
    def boom(_: dict[str, Any]) -> None:
        raise RuntimeError("bus down")

    lifecycle.install_sink(boom)
    try:
        lifecycle.emit_event("incident.opened", idempotency_key="k")  # swallowed + logged
    finally:
        lifecycle.install_sink(None)


# -------------------------------------------------------------------------------------------- delegate


class _FakeThreads:
    def __init__(self) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}

    async def create(self, **kw: Any) -> dict[str, Any]:
        self.created.setdefault(kw["thread_id"], kw)
        return {"thread_id": kw["thread_id"]}

    async def get_state(self, tid: str) -> dict[str, Any]:
        return self.states.get(tid, {"values": {}, "next": [], "tasks": []})


class _FakeRuns:
    def __init__(self, threads: _FakeThreads, *, interrupts_first: bool) -> None:
        self.threads, self.interrupts_first, self.calls, self.created = threads, interrupts_first, [], []

    async def wait(self, tid: str, gid: str, *, input: Any = None, command: Any = None, context: Any = None) -> Any:
        self.calls.append((tid, gid, input, command))
        if command is None and self.interrupts_first:
            irq = [{"id": "i1", "value": {"kind": "approval", "approval_id": "a1"}}]
            self.threads.states[tid] = {
                "values": {"phase": "waiting"},
                "next": ["gate"],
                "tasks": [{"interrupts": irq}],
            }
            return {"phase": "waiting", "__interrupt__": irq}
        self.threads.states[tid] = {"values": {"phase": "done", "ans": 42}, "next": [], "tasks": []}
        return {"phase": "done", "ans": 42}

    async def create(self, tid: str, gid: str, **kw: Any) -> dict[str, Any]:
        if any(c[0] == tid for c in self.created) and kw.get("multitask_strategy") == "reject":
            raise RuntimeError("HTTP 409 Conflict: thread already has an active run")  # what the server answers
        self.created.append((tid, gid, kw))
        return {"run_id": "r1"}


class _FakeClient:
    def __init__(self, *, interrupts_first: bool = False) -> None:
        self.threads = _FakeThreads()
        self.runs = _FakeRuns(self.threads, interrupts_first=interrupts_first)


class _P(TypedDict, total=False):
    ans: Any
    child: str
    runs_before_resume: int


def _parent(client: _FakeClient, mode: str = "await") -> Any:
    async def call(_: _P, config: RunnableConfig) -> _P:
        r = await delegate_mod.delegate(
            graph_id="change_gate",
            key="k1",
            config=config,
            input={"change": "c1"},
            mode=mode,  # type: ignore[arg-type]
            metadata={"foghorn_incident_id": "INC-9"},
        )
        return {"ans": r["values"].get("ans"), "child": r["child_thread_id"]}

    g = StateGraph(_P)
    g.add_node("call", call)
    g.add_edge(START, "call")
    g.add_edge("call", END)
    return g.compile(checkpointer=InMemorySaver())


@pytest.fixture
def fake_client():
    holder: dict[str, _FakeClient] = {}

    def make(**kw: Any) -> _FakeClient:
        holder["c"] = _FakeClient(**kw)
        delegate_mod.set_client_factory(lambda: holder["c"])
        return holder["c"]

    yield make
    delegate_mod.set_client_factory(None)


async def test_delegate_await_returns_child_result_on_a_derived_child_thread(fake_client):
    client = fake_client()
    graph = _parent(client)
    out = await graph.ainvoke({}, {"configurable": {"thread_id": "parent-1"}})
    child = det_thread_id("change_gate", "parent-1", "k1")
    assert out == {"ans": 42, "child": child}
    meta = client.threads.created[child]["metadata"]
    assert (
        meta[M_PARENT_THREAD] == "parent-1"
        and meta[M_GRAPH] == "change_gate"
        and meta["foghorn_incident_id"] == "INC-9"
    )
    assert client.runs.calls[0][2] == {"change": "c1"}


async def test_delegate_bubbles_child_interrupt_to_parent_then_resumes_child_once(fake_client):
    client = fake_client(interrupts_first=True)
    graph = _parent(client)
    cfg = {"configurable": {"thread_id": "parent-2"}}
    first = await graph.ainvoke({}, cfg, version="v2")
    assert first.interrupts and first.interrupts[0].value["approval_id"] == "a1"  # the CHILD's payload, surfaced

    second = await graph.ainvoke(Command(resume={"decision": "approve"}), cfg, version="v2")
    assert not second.interrupts and second.value["ans"] == 42
    starts = [c for c in client.runs.calls if c[3] is None]
    resumes = [c for c in client.runs.calls if c[3] is not None]
    assert len(starts) == 1, "node re-run on resume must NOT start a second child run (idempotent restart)"
    assert resumes[0][3] == {"resume": {"decision": "approve"}}


async def test_delegate_background_fires_once_and_never_waits(fake_client):
    client = fake_client()
    graph = _parent(client, mode="background")
    cfg = {"configurable": {"thread_id": "parent-3"}}
    out = await graph.ainvoke({}, cfg)
    assert out["ans"] is None
    assert len(client.runs.created) == 1 and client.runs.calls == []
    assert client.runs.created[0][2]["multitask_strategy"] == "reject"


async def test_delegate_child_thread_id_override_shares_one_thread_across_parents(fake_client):
    client = fake_client()
    pinned = det_thread_id("incident_commander", "INC-12345")

    def parent(tid: str):
        async def call(_: _P, config: RunnableConfig) -> _P:
            r = await delegate_mod.delegate(
                graph_id="incident_commander",
                key="ignored",
                config=config,
                input={"x": 1},
                mode="background",
                child_thread_id=pinned,
            )
            return {"child": r["child_thread_id"]}

        g = StateGraph(_P)
        g.add_node("call", call)
        g.add_edge(START, "call")
        g.add_edge("call", END)
        return g.compile(checkpointer=InMemorySaver()), {"configurable": {"thread_id": tid}}

    for tid in ("router-run-1", "router-run-2"):
        graph, cfg = parent(tid)
        assert (await graph.ainvoke({}, cfg))["child"] == pinned
    assert len(client.runs.created) == 1, "the second alert must JOIN the existing commander thread, not start another"


def test_stream_payload_allows_data_fields_named_kind_and_key():
    """`artifact`/`evidence.found` carry a `kind` field and `memory.written` a `key` field: positional-only params."""
    p = stream_payload("evidence.found", "evidence:1", id="e1", kind="metric", key="orders-db/error_rate")
    assert p["kind"] == "evidence.found" and p["key"] == "evidence:1"
    assert p["data"] == {"id": "e1", "kind": "metric", "key": "orders-db/error_rate"}


async def test_background_runs_request_a_resumable_full_fidelity_stream(fake_client):
    """The console replays a run's narration through join-stream, which only works if the creator asked for a
    resumable stream with every mode AND subgraph events at creation time (found while driving the real UI)."""
    client = fake_client()
    await _parent(client, mode="background").ainvoke({}, {"configurable": {"thread_id": "parent-stream"}})
    kw = client.runs.created[0][2]
    assert kw["stream_resumable"] is True and kw["stream_subgraphs"] is True
    assert {"messages-tuple", "custom", "updates", "values", "tasks"} <= set(kw["stream_mode"])
