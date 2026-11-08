"""Read side: events list/filters/get/trace/links, dispatches, summary, config, clock, queues, health, DLQ."""

from __future__ import annotations

import asyncio

from foghorn.common.lifecycle import emit_event


async def seed(api):
    await api.event(
        type="alert.fired",
        severity="critical",
        subject={"component": "checkout-service"},
        payload={"rule": "error_rate_high"},
        trace={"scenario_id": "S10"},
        correlation_id="corr_1",
    )
    await api.event(type="alert.fired", severity="low", subject={"component": "cart-service"}, payload={"rule": "slow"})
    await api.event(
        type="deploy.finished", severity="info", subject={"component": "pricing-service"}, payload={"version": "9"}
    )
    await api.event(
        type="customer.report",
        severity="medium",
        subject={"component": "checkout-service"},
        payload={"summary": "cannot pay"},
        correlation_id="corr_1",
    )


async def test_events_list_filters_and_pagination(api):
    await seed(api)
    all_ = (await api.get("/api/events")).json()
    assert [e["seq"] for e in all_["items"]] == [4, 3, 2, 1] and all_["latest_seq"] == 4 and all_["next_before"] is None
    assert [e["seq"] for e in (await api.get("/api/events?type=alert.*")).json()["items"]] == [2, 1]
    assert [e["seq"] for e in (await api.get("/api/events?type=deploy.*,customer.report")).json()["items"]] == [4, 3]
    assert [e["seq"] for e in (await api.get("/api/events?component=checkout-service")).json()["items"]] == [4, 1]
    assert [e["seq"] for e in (await api.get("/api/events?severity=critical,low")).json()["items"]] == [2, 1]
    assert [e["seq"] for e in (await api.get("/api/events?min_severity=medium")).json()["items"]] == [4, 1]
    assert [e["seq"] for e in (await api.get("/api/events?correlation_id=corr_1&order=asc")).json()["items"]] == [1, 4]
    assert [e["seq"] for e in (await api.get("/api/events?scenario_id=S10")).json()["items"]] == [1]
    assert [e["seq"] for e in (await api.get("/api/events?q=cannot pay")).json()["items"]] == [4]
    assert [e["seq"] for e in (await api.get("/api/events?status=recorded")).json()["items"]] == [3]
    assert [e["seq"] for e in (await api.get("/api/events?source=test&since=2")).json()["items"]] == [4, 3]
    page = (await api.get("/api/events?limit=2")).json()
    assert [e["seq"] for e in page["items"]] == [4, 3] and page["next_before"] == 3
    nxt = (await api.get(f"/api/events?limit=2&before={page['next_before']}")).json()
    assert [e["seq"] for e in nxt["items"]] == [2, 1]
    assert (await api.get("/api/events?order=sideways")).status_code == 400
    assert (await api.get("/api/events?min_severity=huge")).status_code == 400
    assert (await api.get("/api/events?limit=0")).status_code == 422


async def test_event_rows_carry_summary_and_dispatch_summaries(api):
    await seed(api)
    row = (await api.get("/api/events/evt_0000001")).json()
    assert (
        row["summary"] == "alert.fired CRITICAL checkout-service: error_rate_high"
        and row["component"] == "checkout-service"
    )
    assert row["dispatches"][0]["graph_id"] == "event_router" and row["dispatches"][0]["priority"] == "P0"
    assert (
        row["status"] == "routed"
        and row["occurrences"] == 1
        and row["duplicate_of"] is None
        and row["trace"]["scenario_id"] == "S10"
    )
    assert (await api.get("/api/events/evt_nope")).status_code == 404
    assert (await api.get("/api/events/evt_nope")).json()["error"]["code"] == "not_found"


async def test_links_and_dispatches_after_the_run_started(api):
    await seed(api)
    await api.idle()
    links = (await api.get("/api/links/event/evt_0000001")).json()
    assert links["thread_id"] and links["run_id"].startswith("run-") and links["dispatches"][0]["status"] == "succeeded"
    assert links["dispatches"][0]["run_id"] == links["run_id"]
    assert links["studio_base_url"].startswith(
        "https://smith.langchain.com/studio/?baseUrl=http%3A%2F%2F127.0.0.1%3A2026"
    )
    assert (
        links["studio_thread_url"] is None and links["api_url"] == f"http://127.0.0.1:2026/threads/{links['thread_id']}"
    )
    empty = (await api.get("/api/links/event/evt_0000003")).json()
    assert empty["thread_id"] is None and empty["dispatches"] == []
    assert (await api.get("/api/links/event/evt_nope")).status_code == 404
    d = (await api.get("/api/dispatches?status=succeeded")).json()
    assert d["total"] == 3 and all("plan" not in x for x in d["items"])
    assert (await api.get("/api/dispatches?graph_id=event_router&event_id=evt_0000001")).json()["total"] == 1
    assert (await api.get("/api/dispatches?status=running,interrupted")).json()["items"] == []


async def test_trace_walks_the_causal_chain_both_ways(api):
    await seed(api)
    await api.idle()
    t = (await api.get("/api/trace/evt_0000001")).json()
    assert t["root_id"] == "evt_0000001" and t["focus_id"] == "evt_0000001"
    types = [e["type"] for e in t["chain"]]
    assert types[0] == "alert.fired" and "run.started" in types and "run.finished" in types
    assert [e["depth"] for e in t["chain"]][0] == 0 and all(e["depth"] == 1 for e in t["chain"][1:])
    assert {"from": "evt_0000001", "to": t["chain"][1]["id"]} in t["edges"]
    assert t["dispatches"][0]["graph_id"] == "event_router"
    child = t["chain"][1]["id"]
    from_child = (await api.get(f"/api/trace/{child}")).json()
    assert from_child["root_id"] == "evt_0000001" and from_child["focus_id"] == child  # walks UP to the root too
    assert (await api.get("/api/trace/evt_nope")).status_code == 404


async def test_trace_joins_the_incident(api):
    emit_event(
        "incident.opened",
        idempotency_key="o",
        component="c",
        correlation_id="corr_z",
        payload={"incident_id": "INC-0007", "severity": "SEV2", "title": "t"},
    )
    for _ in range(50):
        if await api.rt.db.run(api.rt.db.get_incident, "INC-0007"):
            break
        await asyncio.sleep(0.01)
    t = (await api.get("/api/trace/evt_0000001")).json()
    assert t["incident"]["incident_id"] == "INC-0007"


async def test_summary_config_clock_queues_health(api):
    await seed(api)
    s = (await api.get("/api/summary")).json()
    assert (
        s["events_total"] == 4
        and s["queue"]["depth"] == {"P0": 1, "P1": 0, "P2": 1, "P3": 1}
        and s["dlq_open"] == 0
        and s["latest_seq"] == 4
    )
    cfg = (await api.get("/api/config")).json()
    assert (
        cfg["model_profiles"] == ["competent", "naive", "cautious", "gullible"]
        and cfg["events"]["queue_caps"]["P3"] == 200
    )
    assert "token" not in str(cfg).lower().replace("token_delay", "") and "secret" not in str(cfg).lower()  # no secrets
    q = (await api.get("/api/queues")).json()
    assert (
        q["depth"]["P0"] == 1
        and q["caps"] == {"P0": 500, "P1": 500, "P2": 1000, "P3": 200}
        and q["backpressure"] is False
        and q["total_cap"] == 2200
    )
    h = (await api.get("/api/health")).json()
    assert (
        h["components"]["state_db"] == "ok"
        and h["components"]["data_api"] == "ok"
        and h["status"] in {"ok", "degraded"}
    )
    c = (await api.get("/api/clock")).json()
    assert (
        c["virtual_now"] == "2026-11-05T10:00:00Z"
        and c["speed"] == 1.0
        and c["paused"] is False
        and c["mode"] == "free"
        and c["replay"] is None
    )
    set_ = (
        await api.post("/api/clock", json={"virtual_now": "2026-11-06T00:00:00Z", "speed": 60, "paused": True})
    ).json()
    assert set_["virtual_now"] == "2026-11-06T00:00:00Z" and set_["speed"] == 60 and set_["paused"] is True
    assert (await api.post("/api/clock", json={"speed": 601})).status_code == 422
    assert (await api.post("/api/clock", json={"virtual_now": "yesterday"})).status_code == 422
    assert (await api.get("/healthz")).json() == {"status": "ok", "provider": "offline"}


async def test_dlq_list_get_retry_discard(api):
    await api.http.post("/api/events", content=b"{bad", headers={"content-type": "application/json"})
    await api.event(type="made.up")
    lst = (await api.get("/api/dlq")).json()
    assert lst["total"] == 2 and {i["reason"] for i in lst["items"]} == {"schema_invalid", "no_route"}
    assert (await api.get("/api/dlq?reason=no_route")).json()["total"] == 1
    assert (await api.get("/api/dlq?status=bogus")).status_code == 400
    one = (await api.get("/api/dlq/dlq_0000002")).json()
    assert (
        one["reason"] == "no_route"
        and one["envelope"]["type"] == "made.up"
        and one["status"] == "open"
        and one["poison"] is False
    )
    assert (await api.get("/api/dlq/dlq_9")).status_code == 404
    r = await api.post("/api/dlq/dlq_0000002/retry")
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_route"
    d = await api.post("/api/dlq/dlq_0000002/discard", json={"reason": "junk", "by": "gary"})
    assert (
        d.status_code == 200 and d.json()["status"] == "discarded" and d.json()["dead_letter"]["resolved_by"] == "gary"
    )
    assert (await api.post("/api/dlq/dlq_0000002/discard")).status_code == 409
    assert (await api.get("/api/dlq")).json()["total"] == 1 and (await api.get("/api/dlq?status=all")).json()[
        "total"
    ] == 2
    assert (await api.post("/api/dlq/dlq_nope/retry")).status_code == 404


async def test_dlq_retry_requeues_a_failed_dispatch(api):
    api.client.fail_create = 3
    await api.event(type="customer.report", payload={})
    await api.idle()
    item = (await api.get("/api/dlq")).json()["items"][0]
    assert item["reason"] == "run_create_failed"
    r = await api.post(f"/api/dlq/{item['id']}/retry")
    assert r.status_code == 202 and r.json()["dispatch"]["generation"] == 1
    await api.idle()
    assert (await api.get("/api/dispatches?status=succeeded")).json()["total"] == 1
    assert (await api.get(f"/api/dlq/{item['id']}")).json()["status"] == "retried"


async def test_admin_reset_requires_confirmation_and_clears_state(api):
    await seed(api)
    assert (await api.post("/api/admin/reset", json={"confirm": "yes"})).status_code == 400
    out = (await api.post("/api/admin/reset", json={"confirm": "reset"})).json()
    assert out["status"] == "reset" and out["deleted"]["events"] == 4
    assert (await api.get("/api/events")).json()["items"] == [] and api.rt.queues.total_depth == 0
    first = await api.event(type="customer.report", payload={})
    assert first.json()["event_id"] == "evt_0000001"


async def test_ambient_toggle(api):
    assert (await api.post("/api/admin/ambient", json={"enabled": True})).json() == {"ambient_enabled": True}
    assert (await api.post("/api/admin/ambient", json={"enabled": False})).json() == {"ambient_enabled": False}
