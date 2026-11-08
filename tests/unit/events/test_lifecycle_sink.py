"""The lifecycle sink and the projections it materialises (incidents, approvals, eval runs)."""

from __future__ import annotations

import asyncio
import threading

from foghorn.common import lifecycle
from foghorn.common.lifecycle import emit_event


async def settle(env, n_loops: int = 50) -> None:
    for _ in range(n_loops):
        if env.rt._lifecycle_q.empty() and env.rt._backlog == 0:  # noqa: SLF001
            await asyncio.sleep(0.01)
            if env.rt._lifecycle_q.empty() and env.rt._backlog == 0:  # noqa: SLF001
                return
        await asyncio.sleep(0.01)


async def test_sink_is_installed_and_events_from_sync_and_async_nodes_are_persisted(env):
    assert lifecycle._sink is not None  # noqa: SLF001
    emit_event(
        "incident.opened",
        idempotency_key="io-1",
        component="checkout-service",
        severity="high",
        payload={
            "incident_id": "INC-0001",
            "severity": "SEV2",
            "title": "Checkout down",
            "thread_id": "t-1",
            "scenario_id": "S10",
        },
        correlation_id="corr_S10",
    )
    t = threading.Thread(
        target=lambda: emit_event("action.executed", idempotency_key="ax-1", payload={"action": "revert_flag"})
    )  # a sync node in the threadpool
    t.start()
    t.join()
    await settle(env)
    rows = await env.rt.db.run(env.rt.db.list_events, order="asc")
    assert [r["type"] for r in rows] == ["incident.opened", "action.executed"]
    assert all(r["source"] == "foghorn.internal" and r["status"] == "recorded" for r in rows)


async def test_sink_never_blocks_and_never_raises_even_when_stopped_or_flooded(env):
    import time

    t0 = time.perf_counter()
    for i in range(2000):
        emit_event("memory.recalled", idempotency_key=f"m-{i}", payload={"ns": "x", "key": str(i)})
    assert time.perf_counter() - t0 < 1.0  # enqueue only
    await settle(env, 400)
    assert await env.rt.db.run(env.rt.db.count_events) == 2000
    env.rt._backlog = 50_000  # noqa: SLF001
    env.rt.sink({"type": "memory.recalled"})
    assert env.rt.sink_dropped == 1
    env.rt._backlog = 0  # noqa: SLF001
    await env.rt.stop()
    emit_event(
        "memory.recalled", idempotency_key="after-stop", payload={}
    )  # sink uninstalled: ring buffer only, no error
    env.rt.sink({"type": "x"})  # called directly after stop: ignored


async def test_lifecycle_events_are_deduplicated_on_idempotency_key(env):
    for _ in range(3):  # interrupt() re-runs a node from line 1: the same emission repeats
        emit_event(
            "approval.requested",
            idempotency_key="approval.requested:apr_1",
            payload={"approval_id": "apr_1", "kind": "approval", "thread_id": "t-1"},
        )
    await settle(env)
    assert len(await env.rt.db.run(env.rt.db.list_events, types=["approval.requested"])) == 1


async def test_incident_lifecycle_materialises_one_monotonic_record(env):
    emit_event(
        "incident.opened",
        idempotency_key="o",
        component="checkout-service",
        correlation_id="corr_1",
        payload={
            "incident_id": "INC-0001",
            "severity": "SEV2",
            "title": "Checkout down",
            "thread_id": "t-9",
            "scenario_id": "S10",
            "graph_id": "incident_commander",
        },
    )
    await settle(env)
    inc = await env.rt.db.run(env.rt.db.get_incident, "INC-0001")
    assert (
        inc["status"] == "open"
        and inc["severity"] == "SEV2"
        and inc["component"] == "checkout-service"
        and inc["thread_id"] == "t-9"
    )
    assert (
        inc["correlation_id"] == "corr_1"
        and inc["scenario_id"] == "S10"
        and inc["opened_at"]
        and inc["resolved_at"] is None
    )
    emit_event(
        "incident.updated",
        idempotency_key="u",
        component="checkout-service",
        correlation_id="corr_1",
        payload={"incident_id": "INC-0001", "status": "mitigating", "root_cause": {"component": "pricing-service"}},
    )
    await settle(env)
    inc = await env.rt.db.run(env.rt.db.get_incident, "INC-0001")
    assert (
        inc["status"] == "mitigating"
        and inc["root_cause"] == {"component": "pricing-service"}
        and inc["title"] == "Checkout down"
    )  # None never overwrites
    emit_event(
        "incident.resolved",
        idempotency_key="r",
        component="checkout-service",
        correlation_id="corr_1",
        payload={"incident_id": "INC-0001", "severity": "SEV2"},
    )
    await settle(env)
    emit_event(
        "incident.updated",
        idempotency_key="late",
        component="checkout-service",
        correlation_id="corr_1",
        payload={"incident_id": "INC-0001", "status": "open"},
    )  # a late straggler
    await settle(env)
    inc = await env.rt.db.run(env.rt.db.get_incident, "INC-0001")
    assert inc["status"] == "resolved" and inc["resolved_at"]  # never reopens
    rows, total = await env.rt.db.run(env.rt.db.list_incidents)
    assert total == 1


async def test_incident_without_id_is_minted_and_correlation_is_filled_in(env):
    emit_event("incident.opened", idempotency_key="o1", component="a", payload={"title": "first", "severity": "SEV3"})
    emit_event(
        "incident.opened",
        idempotency_key="o2",
        component="a",
        correlation_id="corr_x",
        payload={"title": "second", "severity": "SEV3"},
    )
    emit_event(
        "incident.updated", idempotency_key="o3", component="a", correlation_id="corr_x", payload={"title": "second v2"}
    )
    await settle(env)
    rows, total = await env.rt.db.run(env.rt.db.list_incidents)
    assert total == 2 and {r["incident_id"] for r in rows} == {"INC-D0001", "INC-D0002"}
    second = next(r for r in rows if r["correlation_id"] == "corr_x")
    assert second["title"] == "second v2"  # same correlation id -> same incident
    first = next(r for r in rows if r["title"] == "first")
    assert first["correlation_id"] == f"corr_{first['incident_id']}"
    ev = (await env.rt.db.run(env.rt.db.list_events, types=["incident.opened"], order="asc"))[0]
    assert ev["correlation_id"] == first["correlation_id"]


async def test_incident_links_dispatches_and_approvals_on_its_thread(env):
    res = await env.ingest(type="alert.fired", severity="high", subject={"component": "c"}, payload={"rule": "x"})
    tid = res.dispatches[0]["thread_id"]
    await env.rt.db.run(env.rt.db.upsert_approval, "apr_5", thread_id=tid, status="pending", kind="approval")
    emit_event(
        "incident.opened",
        idempotency_key="o",
        component="c",
        payload={"incident_id": "INC-0042", "severity": "SEV2", "title": "t", "thread_id": tid},
    )
    await settle(env)
    assert (await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"]))["incident_id"] == "INC-0042"
    assert (await env.rt.db.run(env.rt.db.get_approval, "apr_5"))["incident_id"] == "INC-0042"


async def test_approval_lifecycle_materialises_pending_then_decided_and_never_reopens(env):
    emit_event(
        "incident.opened",
        idempotency_key="io",
        payload={"incident_id": "INC-1", "severity": "SEV2", "title": "t", "thread_id": "t-1"},
    )
    emit_event(
        "approval.requested",
        idempotency_key="ar",
        component="pricing-service",
        payload={
            "approval_id": "apr_7",
            "kind": "approval",
            "thread_id": "t-1",
            "interrupt_id": "i-7",
            "title": "revert flag",
            "risk": "medium",
            "summary": "s",
        },
    )
    await settle(env)
    a = await env.rt.db.run(env.rt.db.get_approval, "apr_7")
    assert (
        a["status"] == "pending"
        and a["incident_id"] == "INC-1"
        and a["title"] == "revert flag"
        and a["risk"] == "medium"
        and a["requested_at"]
    )
    emit_event(
        "approval.decided",
        idempotency_key="ad",
        payload={"approval_id": "apr_7", "decision": "approve", "decided_by": "gary", "thread_id": "t-1"},
    )
    await settle(env)
    a = await env.rt.db.run(env.rt.db.get_approval, "apr_7")
    assert (
        a["status"] == "decided"
        and a["decided_by"] == "gary"
        and a["decision"]["decision"] == "approve"
        and a["payload"] is None
    )
    requested_at = a["requested_at"]
    emit_event(
        "approval.requested",
        idempotency_key="ar-again",
        payload={"approval_id": "apr_7", "kind": "approval", "thread_id": "t-1"},
    )  # a re-run node re-emits with a new key
    await settle(env)
    a = await env.rt.db.run(env.rt.db.get_approval, "apr_7")
    assert a["status"] == "decided" and a["requested_at"] == requested_at
    emit_event(
        "approval.decided",
        idempotency_key="orphan",
        payload={"approval_id": "apr_studio", "decision": "reject", "decided_by": "studio"},
    )  # decided in Studio before we saw the request
    emit_event(
        "approval.decided", idempotency_key="no-id", payload={"decision": "reject"}
    )  # malformed: ignored, still stored as an event
    await settle(env)
    assert (await env.rt.db.run(env.rt.db.get_approval, "apr_studio"))["status"] == "decided"
    assert await env.rt.db.run(env.rt.db.count_pending_approvals) == 0


async def test_eval_lifecycle_materialises_progress_and_final_summary(env):
    emit_event(
        "eval.requested", idempotency_key="er", payload={"eval_run_id": "evr_9", "suite": "core16", "profile": "naive"}
    )
    emit_event(
        "eval.progress",
        idempotency_key="p1",
        payload={
            "eval_run_id": "evr_9",
            "done": 1,
            "total": 2,
            "case": {"scenario_id": "S01", "score": 0.9, "passed": True},
        },
    )
    emit_event(
        "eval.progress",
        idempotency_key="p1b",
        payload={
            "eval_run_id": "evr_9",
            "done": 1,
            "total": 2,
            "case": {"scenario_id": "S01", "score": 0.9, "passed": True},
        },
    )  # repeated case: no duplicate row
    await settle(env)
    r = await env.rt.db.run(env.rt.db.get_eval_run, "evr_9")
    assert (
        r["status"] == "running"
        and r["progress"] == {"done": 1, "total": 2}
        and len(r["cases"]) == 1
        and r["suite"] == "core16"
    )
    emit_event(
        "eval.finished",
        idempotency_key="f",
        payload={
            "eval_run_id": "evr_9",
            "summary": {"mean_score": 0.8, "pass_rate": 0.5},
            "cases": [{"scenario_id": "S01", "score": 0.9}, {"scenario_id": "S02", "score": 0.7}],
        },
    )
    await settle(env)
    r = await env.rt.db.run(env.rt.db.get_eval_run, "evr_9")
    assert (
        r["status"] == "finished"
        and r["summary"]["mean_score"] == 0.8
        and r["progress"] == {"done": 2, "total": 2}
        and r["finished_at"]
    )
    emit_event(
        "eval.progress", idempotency_key="late", payload={"eval_run_id": "evr_9", "done": 1, "total": 2}
    )  # late straggler never regresses
    await settle(env)
    assert (await env.rt.db.run(env.rt.db.get_eval_run, "evr_9"))["status"] == "finished"


async def test_lifecycle_events_that_should_trigger_work_are_routed(env):
    emit_event(
        "incident.resolved",
        idempotency_key="res",
        component="c",
        payload={"incident_id": "INC-3", "severity": "SEV1", "scenario_id": "S10", "thread_id": "t-3"},
    )
    emit_event(
        "postmortem.published",
        idempotency_key="pm",
        payload={"incident_id": "INC-3", "scenario_id": "S10", "postmortem": {"title": "x"}},
    )
    await settle(env)
    await env.idle()
    assistants = [r["assistant_id"] for r in env.client.of("runs.create")]
    assert assistants == ["postmortem_pipeline", "memory_keeper"]
