"""Incidents list/detail (+ Studio links) and the approvals inbox + resume endpoints."""

from __future__ import annotations

import asyncio

from foghorn.common.lifecycle import emit_event
from tests.unit.events.fakes import interrupt

OK = {"decision": "approve", "decided_by": "gary"}


async def settle(api):
    for _ in range(100):
        if api.rt._lifecycle_q.empty() and api.rt._backlog == 0:  # noqa: SLF001
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)


async def open_incident(api, iid="INC-0001", thread="t-1", sev="SEV2", **kw):
    emit_event(
        "incident.opened",
        idempotency_key=f"open-{iid}",
        component=kw.get("component", "checkout-service"),
        correlation_id=f"corr_{iid}",
        payload={
            "incident_id": iid,
            "severity": sev,
            "title": f"{iid} title",
            "thread_id": thread,
            "scenario_id": "S10",
            "graph_id": "incident_commander",
        },
    )
    await settle(api)


def interrupt_thread(api, thread="t-1", *ids, incident="INC-0001"):
    api.client.interrupt_thread(
        thread,
        *[interrupt(i, incident_id=incident) for i in ids],
        metadata={
            "foghorn_incident_id": incident,
            "foghorn_scenario_id": "S10",
            "foghorn_correlation_id": f"corr_{incident}",
        },
    )


async def test_incident_list_filters_and_studio_link(api):
    await open_incident(api, "INC-0001", "t-1", "SEV2")
    await open_incident(api, "INC-0002", "t-2", "SEV1", component="cart-service")
    emit_event(
        "incident.resolved",
        idempotency_key="res-2",
        component="cart-service",
        payload={"incident_id": "INC-0002", "severity": "SEV1"},
    )
    await settle(api)
    lst = (await api.get("/api/incidents")).json()
    assert (
        lst["total"] == 2
        and [i["incident_id"] for i in lst["items"]] == ["INC-0002", "INC-0001"]
        or {i["incident_id"] for i in lst["items"]} == {"INC-0001", "INC-0002"}
    )
    one = next(i for i in lst["items"] if i["incident_id"] == "INC-0001")
    assert (
        one["severity"] == "SEV2"
        and one["status"] == "open"
        and one["scenario_id"] == "S10"
        and one["component"] == "checkout-service"
        and one["pending_approvals"] == 0
    )
    assert one["studio"]["thread_id"] == "t-1" and one["studio"]["thread_url"] is None
    assert one["studio"]["base_url"] == "https://smith.langchain.com/studio/?baseUrl=http%3A%2F%2F127.0.0.1%3A2026"
    assert [i["incident_id"] for i in (await api.get("/api/incidents?status=open")).json()["items"]] == ["INC-0001"]
    assert [i["incident_id"] for i in (await api.get("/api/incidents?severity=SEV1")).json()["items"]] == ["INC-0002"]
    assert (await api.get("/api/incidents?scenario_id=nope")).json()["total"] == 0
    assert (await api.get("/api/incidents?status=resolved")).json()["items"][0]["resolved_at"]


async def test_incident_detail_joins_events_dispatches_approvals_and_threads(api):
    await api.event(
        type="alert.fired",
        severity="high",
        subject={"component": "checkout-service"},
        payload={"rule": "x"},
        correlation_id="corr_INC-0001",
    )
    await open_incident(api)
    emit_event(
        "approval.requested",
        idempotency_key="ar",
        correlation_id="corr_INC-0001",
        payload={"approval_id": "apr_a", "kind": "approval", "thread_id": "t-1", "title": "do it"},
    )
    await settle(api)
    await api.idle()
    d = (await api.get("/api/incidents/INC-0001")).json()
    assert d["incident_id"] == "INC-0001" and d["pending_approvals"] == 1
    assert [e["type"] for e in d["events"]][:2] == ["alert.fired", "incident.opened"] and "approval.requested" in [
        e["type"] for e in d["events"]
    ]
    assert d["approvals"][0]["approval_id"] == "apr_a" and any(t["thread_id"] == "t-1" for t in d["threads"])
    assert d["links"]["thread_id"] == "t-1" and d["links"]["base_url"].startswith("https://smith.langchain.com/studio/")
    assert (await api.get("/api/incidents/INC-nope")).status_code == 404
    listing = (await api.get("/api/incidents")).json()["items"][0]
    assert listing["pending_approvals"] == 1


async def test_approvals_inbox_lists_live_pending_interrupts(api):
    interrupt_thread(api, "t-1", "i-1", "i-2")
    await open_incident(api)
    r = (await api.get("/api/approvals")).json()
    assert r["scan_ok"] is True and r["scanned_threads"] == 1
    ids = sorted(a["approval_id"] for a in r["items"])
    assert ids == ["apr_i-1", "apr_i-2"]
    a = r["items"][0]
    assert {
        "approval_id",
        "kind",
        "status",
        "thread_id",
        "interrupt_id",
        "incident_id",
        "title",
        "summary",
        "risk",
        "reversible",
        "payload",
        "response_schema",
        "options",
        "requested_at",
    } <= set(a)
    assert a["status"] == "pending" and a["incident_id"] == "INC-0001" and a["response_schema"]["title"] == "Decision"
    assert [x["approval_id"] for x in (await api.get("/api/approvals?kind=question")).json()["items"]] == []
    assert len((await api.get("/api/approvals?incident_id=INC-0001")).json()["items"]) == 2
    one = (await api.get("/api/approvals/apr_i-1")).json()
    assert one["interrupt_id"] == "i-1"
    assert (await api.get("/api/approvals/apr_ghost")).status_code == 404
    api.client.fail_search = True
    degraded = (await api.get("/api/approvals")).json()
    assert degraded["scan_ok"] is False and len(degraded["items"]) == 2


async def test_resume_endpoint_validates_the_interrupt_id_and_the_decision(api):
    interrupt_thread(api, "t-1", "i-1")
    await api.get("/api/approvals")
    stale = await api.post("/api/approvals/apr_i-1/resume", json={"interrupt_id": "ghost", "response": OK})
    assert (
        stale.status_code == 409
        and stale.json()["error"]["code"] == "stale_interrupt"
        and stale.json()["error"]["details"]["pending"] == ["i-1"]
    )
    assert api.client.of("runs.create") == []
    invalid = await api.post(
        "/api/approvals/apr_i-1/resume", json={"interrupt_id": "i-1", "response": {"decision": "maybe"}}
    )
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_response"
    missing = await api.post("/api/approvals/apr_i-1/resume", json={"interrupt_id": "i-1"})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "schema_invalid"
    assert (await api.post("/api/approvals/apr_ghost/resume", json={"response": OK})).status_code == 404
    ok = await api.post("/api/approvals/apr_i-1/resume", json={"interrupt_id": "i-1", "response": OK, "nonce": "ui-1"})
    assert ok.status_code == 202
    b = ok.json()
    assert (
        b["status"] == "resumed"
        and b["thread_id"] == "t-1"
        and b["interrupt_id"] == "i-1"
        and b["run_id"].startswith("run-")
        and b["remaining_interrupts"] == 0
        and b["decision_event_id"].startswith("evt_")
    )
    dup = await api.post("/api/approvals/apr_i-1/resume", json={"interrupt_id": "i-1", "response": OK, "nonce": "ui-1"})
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    assert api.client.of("runs.create")[0]["command"]["resume"]["i-1"]["decision"] == "approve"
    again = await api.post(
        "/api/approvals/apr_i-1/resume",
        json={"interrupt_id": "i-1", "response": {"decision": "reject"}, "nonce": "ui-2"},
    )
    assert again.status_code == 409  # already decided: the interrupt is no longer pending
    hist = (await api.get("/api/approvals?status=decided")).json()["items"]
    assert (
        hist[0]["approval_id"] == "apr_i-1"
        and hist[0]["decided_by"] == "gary"
        and hist[0]["decision"]["decision"] == "approve"
    )
    assert (await api.get("/api/approvals?status=all")).json()["scan_ok"] is True
    ev = (await api.get("/api/events?type=approval.decided")).json()["items"][0]
    assert ev["payload"]["approval_id"] == "apr_i-1" and ev["source"] == "foghorn.internal"


async def test_resume_batch_for_parallel_interrupts(api):
    interrupt_thread(api, "t-1", "i-1", "i-2")
    r = await api.post(
        "/api/approvals/resume-batch",
        json={"thread_id": "t-1", "responses": {"i-1": OK, "i-2": {"decision": "reject", "note": "no"}}, "nonce": "b"},
    )
    assert r.status_code == 202 and sorted(r.json()["interrupt_ids"]) == ["i-1", "i-2"]
    assert len(api.client.of("runs.create")) == 1
    bad = await api.post("/api/approvals/resume-batch", json={"thread_id": "t-1", "responses": {"zzz": OK}})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "stale_interrupt"
    assert (
        await api.post("/api/approvals/resume-batch", json={"thread_id": "t-1", "responses": {}})
    ).status_code == 400
    assert (await api.post("/api/approvals/resume-batch", json={"thread_id": "t-1"})).status_code == 422


async def test_a_thread_with_no_known_graph_cannot_be_resumed(api):
    api.client.threads.items["t-x"] = {
        "thread_id": "t-x",
        "status": "interrupted",
        "metadata": {},
        "values": {},
        "interrupts": {"task": [interrupt("i-x")]},
    }
    r = await api.post("/api/approvals/resume-batch", json={"thread_id": "t-x", "responses": {"i-x": OK}})
    assert r.status_code == 409 and "which graph" in r.json()["error"]["message"]
