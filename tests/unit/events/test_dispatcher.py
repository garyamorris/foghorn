"""Dispatcher: event -> thread -> run correlation, concurrency caps, retry/backoff, resume-from-checkpoint, DLQ."""

from __future__ import annotations

import asyncio

import pytest

from foghorn.contracts.meta import det_thread_id
from foghorn.events.dispatcher import DispatchError
from tests.unit.events.fakes import interrupt

ALERT = {
    "type": "alert.fired",
    "severity": "high",
    "subject": {"component": "checkout-service"},
    "payload": {"rule": "error_rate_high"},
    "trace": {"scenario_id": "S10"},
    "correlation_id": "corr_S10",
}


async def test_dispatch_creates_the_thread_with_flat_metadata_and_a_streaming_run(env):
    res = await env.ingest(**ALERT)
    await env.idle()
    tid = det_thread_id("event_router", res.event_id)
    create = env.client.of("threads.create")[0]
    assert create["thread_id"] == tid and create["graph_id"] == "event_router"
    md = create["metadata"]
    assert (
        md["foghorn_event_id"] == res.event_id
        and md["foghorn_correlation_id"] == "corr_S10"
        and md["foghorn_scenario_id"] == "S10"
    )
    assert md["foghorn_trigger"] == "event" and md["foghorn_kind"] == "triage" and md["foghorn_graph"] == "event_router"
    assert (
        env.client.threads.items[tid]["metadata"]["foghorn_event_id"] == res.event_id
    )  # merged onto the thread (threads.update)
    run = env.client.of("runs.create")[0]
    assert run["thread_id"] == tid and run["assistant_id"] == "event_router"
    assert run["input"]["event"]["id"] == res.event_id and run["input"]["event"]["type"] == "alert.fired"
    assert (
        run["stream_resumable"] is True
        and run["stream_subgraphs"] is True
        and "messages-tuple" in run["stream_mode"]
        and "tools" not in run["stream_mode"]
    )
    assert run["context"]["model_profile"] == "competent" and run["context"]["scenario_id"] == "S10"
    assert (
        run["multitask_strategy"] == "enqueue"
        and run["on_completion"] == "keep"
        and run["metadata"]["foghorn_dispatch_id"] == res.dispatches[0]["id"]
    )
    d = await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert (
        d["status"] == "succeeded"
        and d["run_id"] == "run-001"
        and d["thread_id"] == tid
        and d["attempts"] == 1
        and d["finished_at"]
    )


async def test_run_lifecycle_events_form_a_causal_chain_with_the_trigger(env):
    env.client.threads.items.clear()
    env.client.script = lambda *_a: {
        "status": "success",
        "values": {"result": {"graph_id": "event_router", "status": "ok", "summary": "opened INC-1"}},
    }
    res = await env.ingest(**ALERT)
    await env.idle()
    types = [e["type"] for e in await env.rt.db.run(env.rt.db.list_events, order="asc")]
    assert types[:1] == ["alert.fired"] and {"run.started", "run.finished", "triage.completed"} <= set(types)
    started = (await env.rt.db.run(env.rt.db.list_events, types=["run.started"]))[0]
    assert started["causation_id"] == res.event_id and started["correlation_id"] == "corr_S10"
    fin = (await env.rt.db.run(env.rt.db.list_events, types=["run.finished"]))[0]
    assert fin["payload"]["summary"] == "opened INC-1"
    tri = (await env.rt.db.run(env.rt.db.list_events, types=["triage.completed"]))[0]
    assert tri["payload"]["result"]["summary"] == "opened INC-1"
    root, chain = await env.rt.db.run(env.rt.db.causal_chain, res.event_id)
    assert root == res.event_id and {e["type"] for e, _d in chain} >= {"run.started", "run.finished"}


async def test_global_cap_per_graph_cap_and_one_active_run_per_thread(make_env):
    env = await make_env(max_runs=2, per_graph_max_runs={"event_router": 1})
    gate = asyncio.Event()
    started: list[str] = []

    async def create_slow(thread_id, assistant_id, **kw):
        started.append(thread_id)
        await gate.wait()
        return {"run_id": f"r{len(started)}", "thread_id": thread_id}

    env.client.runs.create = create_slow  # type: ignore[method-assign]
    env.client.runs.get = lambda tid, rid: asyncio.sleep(0, {"run_id": rid, "status": "success"})  # type: ignore[method-assign,assignment]
    for i in range(4):
        await env.ingest(type="customer.report", severity="low", payload={"n": i})
    env.rt.dispatcher.pump()
    await asyncio.sleep(0.05)
    assert env.rt.dispatcher.running_total == 1 and len(started) == 1  # per-graph cap 1 for event_router
    gate.set()
    await env.idle()
    assert len(started) == 4


async def test_global_cap_limits_concurrency_across_graphs(make_env):
    env = await make_env(max_runs=2, per_graph_max_runs={})
    gate = asyncio.Event()
    live = {"now": 0, "max": 0}

    async def create_slow(thread_id, assistant_id, **kw):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await gate.wait()
        live["now"] -= 1
        return {"run_id": f"r-{thread_id[:6]}", "thread_id": thread_id}

    env.client.runs.create = create_slow  # type: ignore[method-assign]
    env.client.runs.get = lambda tid, rid: asyncio.sleep(0, {"run_id": rid, "status": "success"})  # type: ignore[method-assign,assignment]
    for i in range(5):
        await env.ingest(type="customer.report", severity="low", payload={"n": i})
    env.rt.dispatcher.pump()
    await asyncio.sleep(0.05)
    assert live["max"] == 2
    gate.set()
    await env.idle()
    assert live["max"] == 2


async def test_a_thread_never_has_two_active_runs(make_env):
    env = await make_env(max_runs=4)
    gate = asyncio.Event()
    active: dict[str, int] = {}
    peak = {"v": 0}

    async def create_slow(thread_id, assistant_id, **kw):
        active[thread_id] = active.get(thread_id, 0) + 1
        peak["v"] = max(peak["v"], active[thread_id])
        await gate.wait()
        active[thread_id] -= 1
        return {"run_id": "r", "thread_id": thread_id}

    env.client.runs.create = create_slow  # type: ignore[method-assign]
    env.client.runs.get = lambda tid, rid: asyncio.sleep(0, {"run_id": rid, "status": "success"})  # type: ignore[method-assign,assignment]
    # two dispatches that target the same thread (same incident id -> by_incident thread)
    for sev in ("SEV1", "SEV2"):
        await env.ingest(
            type="incident.resolved", payload={"incident_id": "INC-9", "severity": sev}, idempotency_key=f"res-{sev}"
        )
    env.rt.dispatcher.pump()
    await asyncio.sleep(0.05)
    assert env.rt.dispatcher.running_total == 1
    gate.set()
    await env.idle()
    assert peak["v"] == 1


async def test_transient_run_create_failure_retries_with_backoff_then_succeeds(make_env):
    env = await make_env(backoff_s=[1.0, 4.0, 16.0])
    env.client.fail_create = 2
    res = await env.ingest(**ALERT)
    await env.idle()
    d = await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert d["status"] == "succeeded" and d["attempts"] == 3
    assert [s for s in env.wall.sleeps if s >= 1] == [1.0, 4.0]  # 1 s then 4 s backoff
    failed = await env.rt.db.run(env.rt.db.list_events, types=["run.failed"], order="asc")
    assert [e["payload"]["will_retry"] for e in failed] == [True, True]


async def test_retries_exhausted_land_in_the_dlq_then_operator_retry_redispatches_exactly_once(env):
    env.client.fail_create = 3
    res = await env.ingest(**ALERT)
    await env.idle()
    d = await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert d["status"] == "dead" and d["attempts"] == 3
    rows, total = await env.rt.db.run(env.rt.db.list_dead_letters)
    assert (
        total == 1
        and rows[0]["reason"] == "run_create_failed"
        and rows[0]["attempts"] == 3
        and rows[0]["event_id"] == res.event_id
    )
    assert rows[0]["envelope"]["type"] == "alert.fired" and "runs.create failed" in rows[0]["last_error"]
    ev = await env.rt.db.run(env.rt.db.get_event, res.event_id)
    assert ev["status"] == "dlq"
    out = await env.rt.dispatcher.retry_dead_letter(rows[0]["id"])
    assert out["status"] == "retried" and out["dispatch"]["generation"] == 1 and out["dispatch"]["id"] != d["id"]
    await env.idle()
    d2 = await env.rt.db.run(env.rt.db.get_dispatch, out["dispatch"]["id"])
    assert d2["status"] == "succeeded" and d2["parent_dispatch_id"] == d["id"]
    with pytest.raises(DispatchError) as e:
        await env.rt.dispatcher.retry_dead_letter(rows[0]["id"])  # exactly once
    assert e.value.status == 409
    assert len(env.client.of("runs.create")) == 4  # 3 failed attempts + the single operator retry
    assert (await env.rt.db.run(env.rt.db.get_event, res.event_id))["status"] == "routed"
    types = {e["type"] for e in await env.rt.db.run(env.rt.db.list_events)}
    assert {"dlq.added", "dlq.retried"} <= types


async def test_failed_run_is_retried_as_a_resume_from_the_last_checkpoint(env):
    calls: list[dict] = []

    def script(tid, aid, kw, n):
        calls.append(kw)
        return {"status": "error", "error": "node exploded"} if n == 1 else {"status": "success"}

    env.client.script = script
    res = await env.ingest(**ALERT)
    await env.idle()
    first, second = env.client.of("runs.create")
    assert (
        first["input"] is not None and second["input"] is None
    )  # input=None => continue from the last good checkpoint
    assert second["thread_id"] == first["thread_id"]  # same thread (durable execution)
    d = await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert d["status"] == "succeeded" and d["attempts"] == 2 and d["mode"] == "resume"


async def test_run_that_keeps_failing_is_dead_lettered_with_the_failing_checkpoint(env):
    env.client.script = lambda *_a: {"status": "error", "error": "kaboom"}
    res = await env.ingest(**ALERT)
    await env.idle()
    rows, _ = await env.rt.db.run(env.rt.db.list_dead_letters)
    assert (
        rows[0]["reason"] == "run_failed" and rows[0]["checkpoint_id"] == "cp-1" and "kaboom" in rows[0]["last_error"]
    )
    assert rows[0]["run_id"] and rows[0]["thread_id"] == det_thread_id("event_router", res.event_id)
    out = await env.rt.dispatcher.retry_dead_letter(rows[0]["id"])
    assert out["dispatch"]["mode"] == "resume"  # an operator retry of a failed run also resumes the thread


async def test_poison_events_never_succeed_and_are_flagged(env):
    res = await env.ingest(**{**ALERT, "attributes": {"chaos": "poison"}})
    await env.idle()
    assert env.client.of("runs.create") == []  # the run was never even created
    rows, _ = await env.rt.db.run(env.rt.db.list_dead_letters)
    assert rows[0]["reason"] == "poison" and rows[0]["poison"] is True and rows[0]["attempts"] == 3
    out = await env.rt.dispatcher.retry_dead_letter(
        rows[0]["id"]
    )  # an operator retry drops the chaos flag: a clean run
    await env.idle()
    assert (await env.rt.db.run(env.rt.db.get_dispatch, out["dispatch"]["id"]))["status"] == "succeeded"
    assert res.event_id


async def test_fail_once_chaos_recovers_on_the_second_attempt_and_slow_chaos_delays(env):
    r1 = await env.ingest(**{**ALERT, "attributes": {"chaos": "fail_once"}, "payload": {"rule": "a"}})
    r2 = await env.ingest(
        **{**ALERT, "attributes": {"chaos": "slow"}, "payload": {"rule": "b"}, "subject": {"component": "other"}}
    )
    await env.idle()
    d1 = await env.rt.db.run(env.rt.db.get_dispatch, r1.dispatches[0]["id"])
    d2 = await env.rt.db.run(env.rt.db.get_dispatch, r2.dispatches[0]["id"])
    assert (d1["status"], d1["attempts"]) == ("succeeded", 2) and (d2["status"], d2["attempts"]) == ("succeeded", 1)
    assert env.rt.es.chaos_slow_s in env.wall.sleeps


async def test_interrupted_run_frees_its_slot_and_is_reported_not_succeeded(env):
    tid = None

    def script(thread_id, aid, kw, n):
        nonlocal tid
        tid = thread_id
        return {"status": "interrupted_thread", "interrupts": [interrupt("i-1", approval_id="apr_1")]}

    env.client.script = script
    res = await env.ingest(**ALERT)
    await env.idle()
    d = await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert d["status"] == "interrupted"
    assert env.rt.dispatcher.running_total == 0
    assert [
        e["payload"]["interrupt_ids"] for e in await env.rt.db.run(env.rt.db.list_events, types=["run.interrupted"])
    ] == [["i-1"]]
    # the approvals projection was reconciled even though the "graph" never emitted approval.requested
    apr = await env.rt.db.run(env.rt.db.get_approval, "apr_1")
    assert apr["status"] == "pending" and apr["thread_id"] == tid


async def test_dispatches_survive_a_restart_queued_work_is_requeued(make_env, tmp_path):
    env = await make_env()
    res = await env.ingest(**ALERT)  # queued but the dispatcher never ran
    assert env.rt.queues.total_depth == 1
    await env.rt.stop()
    env2 = await make_env()
    assert env2.rt.queues.total_depth == 0
    await env2.rt.dispatcher.recover()
    assert env2.rt.queues.total_depth == 1
    await env2.rt.dispatcher.recover()  # idempotent: not queued twice
    assert env2.rt.queues.total_depth == 1
    await env2.idle()
    d = await env2.rt.db.run(env2.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert d["status"] == "succeeded"


async def test_adopts_runs_that_were_in_flight_before_the_restart(make_env):
    env = await make_env()
    res = await env.ingest(**ALERT)
    d = res.dispatches[0]
    tid = d["thread_id"]
    env.client.threads.items[tid] = {"thread_id": tid, "status": "idle", "metadata": {}, "interrupts": {}, "values": {}}
    env.client.runs.items["run-x"] = {
        "run_id": "run-x",
        "thread_id": tid,
        "status": "success",
        "error": None,
        "polls": 1,
    }
    await env.rt.db.run(env.rt.db.update_dispatch, d["id"], status="running", run_id="run-x")
    env.rt.queues.reset()
    await env.rt.dispatcher.recover()
    await env.idle()
    assert (await env.rt.db.run(env.rt.db.get_dispatch, d["id"]))["status"] == "succeeded"
    assert env.client.of("runs.create") == []  # adopted, not restarted


async def test_dlq_discard_is_audited_and_not_repeatable(env):
    r = await env.rt.bus.ingest("{broken", origin="api")
    out = await env.rt.dispatcher.discard_dead_letter(r.dead_letter_id, by="gary", reason="known junk")
    assert out["status"] == "discarded" and out["resolved_by"] == "gary" and out["resolution"] == "known junk"
    audit = await env.rt.db.run(env.rt.db.list_events, types=["dlq.discarded"])
    assert audit[0]["payload"]["by"] == "gary"
    with pytest.raises(DispatchError):
        await env.rt.dispatcher.discard_dead_letter(r.dead_letter_id, by="gary", reason=None)
    with pytest.raises(DispatchError) as e:
        await env.rt.dispatcher.discard_dead_letter("dlq_9999999", by="x", reason=None)
    assert e.value.status == 404


async def test_dlq_retry_of_schema_invalid_reingests_the_stored_body(env):
    r = await env.rt.bus.ingest("{broken", origin="api")
    with pytest.raises(DispatchError) as e:
        await env.rt.dispatcher.retry_dead_letter(r.dead_letter_id)
    assert e.value.status == 422
    dl = await env.rt.db.run(env.rt.db.get_dead_letter, r.dead_letter_id)
    assert dl["status"] == "open"
    await env.rt.db.run(env.rt.db.update_dead_letter, dl["id"], raw_body='{"type":"customer.report","payload":{}}')
    out = await env.rt.dispatcher.retry_dead_letter(dl["id"])
    assert out["status"] == "retried" and out["event_id"]


async def test_dlq_retry_of_no_route_reevaluates_the_rules(env):
    bad = await env.ingest(type="made.up.type")
    with pytest.raises(DispatchError) as e:
        await env.rt.dispatcher.retry_dead_letter(bad.dead_letter_id)
    assert e.value.code == "no_route" and e.value.status == 422


async def test_ambient_events_run_statelessly(env):
    await env.ingest(type="customer.report", payload={}, attributes={"ambient": True})
    await env.idle()
    run = env.client.of("runs.create")[0]
    assert run["on_completion"] == "delete"
