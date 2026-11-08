"""Approvals: scanning interrupted threads, resume validation (stale/unknown ids), idempotency, batch resume."""

from __future__ import annotations

import pytest

from foghorn.events.dispatcher import DispatchError
from tests.unit.events.fakes import interrupt

OK = {"decision": "approve", "decided_by": "gary"}


def seed(env, tid="t-1", *ids, graph="incident_commander", incident="INC-1"):
    env.client.interrupt_thread(
        tid,
        *[interrupt(i, incident_id=incident) for i in ids],
        graph=graph,
        metadata={"foghorn_incident_id": incident, "foghorn_scenario_id": "S10", "foghorn_correlation_id": "corr_1"},
    )


async def test_scan_lists_each_pending_interrupt_with_payload_and_response_schema(env):
    seed(env, "t-1", "i-1", "i-2")
    env.client.interrupt_thread(
        "t-2",
        {"id": "q-1", "value": {"kind": "question", "approval_id": "apr_q", "title": "Which db?"}},
        graph="change_gate",
    )
    res = await env.rt.approvals.pending()
    assert res.ok and res.scanned_threads == 2 and len(res.items) == 3
    a = next(x for x in res.items if x["approval_id"] == "apr_i-1")
    assert (
        a["kind"] == "approval" and a["status"] == "pending" and a["thread_id"] == "t-1" and a["interrupt_id"] == "i-1"
    )
    assert (
        a["incident_id"] == "INC-1"
        and a["graph_id"] == "incident_commander"
        and a["title"] == "Approve i-1"
        and a["risk"] == "medium"
    )
    assert a["payload"]["kind"] == "approval" and a["options"] == ["approve", "reject"]
    assert "decision" in a["response_schema"]["properties"] and "approve" in str(
        a["response_schema"]
    )  # Decision schema
    q = next(x for x in res.items if x["approval_id"] == "apr_q")
    assert q["kind"] == "question" and "answer" in q["response_schema"]["properties"]


async def test_interrupts_without_an_approval_id_get_a_stable_one(env):
    env.client.interrupt_thread(
        "t-9", {"id": "abcdef0123456789", "value": {"kind": "edit", "title": "t"}}, graph="postmortem_pipeline"
    )
    res = await env.rt.approvals.pending()
    assert res.items[0]["approval_id"] == "apr_abcdef01" and res.items[0]["kind"] == "edit"
    again = await env.rt.approvals.pending()
    assert again.items[0]["approval_id"] == "apr_abcdef01"


async def test_scan_failure_degrades_to_the_cached_projection(env):
    seed(env, "t-1", "i-1")
    await env.rt.approvals.pending()
    env.client.fail_search = True
    res = await env.rt.approvals.pending()
    assert res.ok is False and [a["approval_id"] for a in res.items] == ["apr_i-1"]


async def test_resume_sends_command_resume_keyed_by_interrupt_id_and_emits_approval_decided(env):
    seed(env, "t-1", "i-1")
    env.client.script = lambda *_a: {"status": "success"}
    await env.ingest(
        type="alert.fired", payload={"rule": "x"}
    )  # gives the original dispatch (and its run context) a home
    out = await env.rt.approvals.resume("apr_i-1", interrupt_id="i-1", response=OK, nonce="n-1", decided_by=None)
    assert (
        out["status"] == "resumed"
        and out["thread_id"] == "t-1"
        and out["interrupt_id"] == "i-1"
        and out["remaining_interrupts"] == 0
    )
    call = env.client.of("runs.create")[-1]
    assert call["thread_id"] == "t-1" and call["assistant_id"] == "incident_commander"
    assert call["command"] == {
        "resume": {"i-1": {"decision": "approve", "decided_by": "gary", "note": "", "edits": {}, "question": ""}}
    }
    assert call["stream_resumable"] is True
    ev = await env.rt.db.run(env.rt.db.get_event, out["decision_event_id"])
    assert (
        ev["type"] == "approval.decided"
        and ev["source"] == "foghorn.internal"
        and ev["payload"]["approval_id"] == "apr_i-1"
    )
    assert (
        ev["payload"]["decision"] == "approve"
        and ev["payload"]["decided_by"] == "gary"
        and ev["payload"]["run_id"] == out["run_id"]
    )
    assert ev["correlation_id"] == "corr_1" and ev["trace"]["scenario_id"] == "S10"
    a = await env.rt.db.run(env.rt.db.get_approval, "apr_i-1")
    assert a["status"] == "decided" and a["decided_by"] == "gary"
    await env.idle()
    tracked = (await env.rt.db.run(env.rt.db.list_dispatches, thread_id="t-1"))[0][0]
    assert tracked["kind"] == "resume" and tracked["run_id"] == out["run_id"] and tracked["status"] == "succeeded"


async def test_unknown_stale_and_already_decided_interrupt_ids_are_rejected_before_anything_runs(env):
    seed(env, "t-1", "i-1")
    await env.rt.approvals.pending()
    with pytest.raises(DispatchError) as e:
        await env.rt.approvals.resume(
            "apr_i-1", interrupt_id="not-an-interrupt", response=OK, nonce=None, decided_by=None
        )
    assert (
        e.value.status == 409
        and e.value.code == "stale_interrupt"
        and e.value.details["pending"] == ["i-1"]
        and e.value.details["unknown"] == ["not-an-interrupt"]
    )
    assert env.client.of("runs.create") == []  # nothing was resumed
    await env.rt.approvals.resume("apr_i-1", interrupt_id="i-1", response=OK, nonce="n1", decided_by=None)
    with pytest.raises(DispatchError) as e2:  # a second, different click after the interrupt was consumed
        await env.rt.approvals.resume(
            "apr_i-1", interrupt_id="i-1", response={"decision": "reject"}, nonce="n2", decided_by=None
        )
    assert e2.value.code == "stale_interrupt"
    assert len(env.client.of("runs.create")) == 1


async def test_nonce_makes_a_repeated_decision_idempotent(env):
    seed(env, "t-1", "i-1")
    first = await env.rt.approvals.resume("apr_i-1", interrupt_id="i-1", response=OK, nonce="same", decided_by=None)
    again = await env.rt.approvals.resume("apr_i-1", interrupt_id="i-1", response=OK, nonce="same", decided_by=None)
    assert first["status"] == "resumed" and again["status"] == "duplicate"
    assert len(env.client.of("runs.create")) == 1
    assert len(await env.rt.db.run(env.rt.db.list_events, types=["approval.decided"])) == 1


@pytest.mark.parametrize(
    "bad",
    [{"decision": "maybe"}, {}, {"decision": "edit", "edits": "nope"}, {"decision": "approve", "decided_by": 5}],
)
async def test_response_is_validated_against_the_interrupt_schema(env, bad):
    seed(env, "t-1", "i-1")
    with pytest.raises(DispatchError) as e:
        await env.rt.approvals.resume("apr_i-1", interrupt_id="i-1", response=bad, nonce=None, decided_by=None)
    assert e.value.status == 422 and e.value.code == "invalid_response"
    assert env.client.of("runs.create") == []
    assert (await env.rt.db.run(env.rt.db.get_approval, "apr_i-1"))["status"] == "pending"  # still answerable


async def test_question_and_memory_kinds_use_their_own_schemas(env):
    env.client.interrupt_thread(
        "t-q",
        {"id": "q1", "value": {"kind": "question", "approval_id": "apr_q"}},
        {"id": "m1", "value": {"kind": "memory_write", "approval_id": "apr_m"}},
        graph="x",
    )
    with pytest.raises(DispatchError):
        await env.rt.approvals.resume(
            "apr_q", interrupt_id="q1", response={"decision": "approve"}, nonce=None, decided_by=None
        )  # not an Answer
    out = await env.rt.approvals.resume(
        "apr_q", interrupt_id="q1", response={"answer": "the primary"}, nonce=None, decided_by="gary"
    )
    assert (
        env.client.of("runs.create")[-1]["command"]["resume"]["q1"]["answer"] == "the primary"
        and out["remaining_interrupts"] == 1
    )
    mem = await env.rt.approvals.resume(
        "apr_m", interrupt_id="m1", response={"decision": "discard"}, nonce=None, decided_by=None
    )
    assert mem["remaining_interrupts"] == 0


async def test_unknown_approval_and_unknown_thread(env):
    with pytest.raises(DispatchError) as e:
        await env.rt.approvals.resume("apr_nope", interrupt_id="x", response=OK, nonce=None, decided_by=None)
    assert e.value.status == 404
    await env.rt.db.run(
        env.rt.db.upsert_approval, "apr_ghost", thread_id="t-ghost", interrupt_id="g", status="pending", kind="approval"
    )
    with pytest.raises(DispatchError) as e2:
        await env.rt.approvals.resume("apr_ghost", interrupt_id="g", response=OK, nonce=None, decided_by=None)
    assert e2.value.status == 404 and e2.value.code == "not_found"
    await env.rt.db.run(env.rt.db.upsert_approval, "apr_nothread", status="pending", kind="approval")
    with pytest.raises(DispatchError) as e3:
        await env.rt.approvals.resume("apr_nothread", interrupt_id=None, response=OK, nonce=None, decided_by=None)
    assert e3.value.status == 409


async def test_run_creation_failure_leaves_the_interrupt_pending_and_records_nothing(env):
    seed(env, "t-1", "i-1")
    await env.rt.approvals.pending()
    env.client.fail_create = 1
    with pytest.raises(DispatchError) as e:
        await env.rt.approvals.resume("apr_i-1", interrupt_id="i-1", response=OK, nonce="n", decided_by=None)
    assert e.value.status == 503
    assert await env.rt.db.run(env.rt.db.list_events, types=["approval.decided"]) == []
    out = await env.rt.approvals.resume(
        "apr_i-1", interrupt_id="i-1", response=OK, nonce="n", decided_by=None
    )  # the same click can simply be retried
    assert out["status"] == "resumed"


async def test_batch_resume_answers_parallel_interrupts_with_one_command(env):
    seed(env, "t-1", "i-1", "i-2", "i-3")
    out = await env.rt.approvals.resume_batch(
        "t-1", {"i-1": OK, "i-2": {"decision": "reject", "note": "no"}}, nonce="b1", decided_by="gary"
    )
    assert (
        out["status"] == "resumed"
        and sorted(out["interrupt_ids"]) == ["i-1", "i-2"]
        and len(out["decision_event_ids"]) == 2
    )
    runs = env.client.of("runs.create")
    assert len(runs) == 1 and set(runs[0]["command"]["resume"]) == {"i-1", "i-2"}  # ONE resume mapping, not two runs
    with pytest.raises(DispatchError) as e:
        await env.rt.approvals.resume_batch("t-1", {"i-3": OK, "i-ghost": OK}, nonce=None, decided_by=None)
    assert (
        e.value.code == "stale_interrupt"
        and e.value.details["unknown"] == ["i-ghost"]
        and e.value.details["pending"] == ["i-3"]
    )
    with pytest.raises(DispatchError) as e2:
        await env.rt.approvals.resume_batch("t-1", {}, nonce=None, decided_by=None)
    assert e2.value.status == 400


async def test_decision_delivered_as_an_event_resumes_via_the_routing_rule(env):
    seed(env, "t-1", "i-1")
    await env.rt.approvals.pending()
    res = await env.ingest(
        type="approval.decided",
        source="ui",
        payload={"approval_id": "apr_i-1", "interrupt_id": "i-1", "response": OK, "nonce": "ev-1"},
    )
    assert res.status == "routed" and res.dispatches[0]["kind"] == "resume" and res.dispatches[0]["priority"] == "P0"
    await env.idle()
    d = await env.rt.db.run(env.rt.db.get_dispatch, res.dispatches[0]["id"])
    assert d["status"] == "succeeded"
    assert env.client.of("runs.create")[-1]["command"]["resume"]["i-1"]["decision"] == "approve"
    bad = await env.ingest(type="approval.decided", source="ui", payload={"decision": "approve"})  # no approval_id
    await env.idle()
    assert (await env.rt.db.run(env.rt.db.get_dispatch, bad.dispatches[0]["id"]))["status"] == "failed"


async def test_reconciliation_marks_interrupts_resolved_elsewhere_as_decided_externally(env):
    seed(env, "t-1", "i-1")
    await env.rt.approvals.pending()  # confirmed pending
    env.client.threads.items["t-1"].update(status="idle", interrupts={})  # resumed from Studio
    res = await env.rt.approvals.pending()
    assert res.items == []
    a = await env.rt.db.run(env.rt.db.get_approval, "apr_i-1")
    assert a["status"] == "decided" and a["decided_by"] == "external"


async def test_grace_for_interrupts_not_yet_visible_on_the_thread(env):
    # the graph emits approval.requested BEFORE interrupt(): a scan in between must not declare it decided
    await env.rt.bus.emit(
        "approval.requested",
        idempotency_key="ar",
        payload={"approval_id": "apr_new", "kind": "approval", "thread_id": "t-new"},
    )
    await env.rt.approvals.pending()
    assert (await env.rt.db.run(env.rt.db.get_approval, "apr_new"))["status"] == "pending"
    await env.rt.approvals.scan(startup=True)  # after a restart nothing is mid-flight: stale projections are cleaned up
    assert (await env.rt.db.run(env.rt.db.get_approval, "apr_new"))["status"] == "decided"


async def test_get_scans_when_the_projection_has_not_seen_the_approval_yet(env):
    seed(env, "t-1", "i-1")
    row = await env.rt.approvals.get("apr_i-1")
    assert row is not None and row["status"] == "pending"
    assert await env.rt.approvals.get("apr_none") is None
