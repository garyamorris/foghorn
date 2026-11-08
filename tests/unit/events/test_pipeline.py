"""EventBus.ingest: validation, idempotency, semantic dedupe, maintenance suppression, routing, backpressure, shedding."""

from __future__ import annotations

import json

from tests.unit.events.fakes import BASE

ALERT = {
    "type": "alert.fired",
    "severity": "high",
    "subject": {"component": "checkout-service"},
    "payload": {"rule": "error_rate_high"},
}


async def test_valid_event_is_stored_routed_and_gets_server_assigned_fields(env):
    res = await env.ingest(**ALERT)
    assert res.http_status == 202 and res.status == "routed"
    ev = res.event
    assert ev["id"] == f"evt_{ev['seq']:07d}" and ev["seq"] == 1
    assert ev["received_at"].endswith("Z") and ev["occurred_at"] == "2026-11-05T10:00:00Z"  # virtual clock default
    assert ev["dedupe_key"] == "alert:checkout-service:error_rate_high"  # default semantic identity
    assert [d["graph_id"] for d in res.dispatches] == ["event_router"] and res.dispatches[0]["status"] == "queued"
    body = res.body()
    assert body["event_id"] == ev["id"] and body["dispatches"][0]["priority"] == "P1"
    stored = await env.rt.db.run(env.rt.db.get_event, ev["id"])
    assert stored["status"] == "routed" and stored["occurrences"] == 1


async def test_invalid_envelope_is_422_and_lands_in_the_dlq_with_the_raw_body(env):
    res = await env.rt.bus.ingest({"type": "alert.fired", "severity": "apocalyptic", "bogus": 1}, origin="api")
    assert res.http_status == 422 and res.status == "invalid"
    assert res.error["code"] == "schema_invalid" and res.error["details"]["dead_letter_id"] == res.dead_letter_id
    dl = await env.rt.db.run(env.rt.db.get_dead_letter, res.dead_letter_id)
    assert (
        dl["reason"] == "schema_invalid"
        and dl["status"] == "open"
        and "apocalyptic" in dl["raw_body"]
        and dl["event_id"] is None
    )
    sys_events = await env.rt.db.run(env.rt.db.list_events, types=["dlq.added"])
    assert len(sys_events) == 1 and sys_events[0]["payload"]["dead_letter_id"] == dl["id"]
    # non-JSON and non-object bodies are rejected the same way, never a 500
    for raw in ("{not json", "[1,2]", b"\xff\xfe", ""):
        r = await env.rt.bus.ingest(raw, origin="api")
        assert r.http_status == 422 and r.dead_letter_id


async def test_internal_invalid_events_do_not_flood_the_dlq(env):
    res = await env.rt.bus.ingest({"type": "x", "nope": 1}, origin="internal")
    assert res.http_status == 422 and res.dead_letter_id is None
    assert await env.rt.db.run(env.rt.db.count_open_dead_letters) == 0


async def test_delivery_idempotency_by_key_and_by_source_id(env):
    a = await env.ingest(**ALERT, idempotency_key="k-1")
    b = await env.ingest(type="alert.fired", severity="low", payload={"other": True}, idempotency_key="k-1")
    assert b.http_status == 200 and b.status == "duplicate" and b.event_id == a.event_id
    assert b.body() == {"status": "duplicate", "event_id": a.event_id}
    assert await env.rt.db.run(env.rt.db.count_events) == 1
    c = await env.ingest(type="customer.report", id="my-id", payload={})
    d = await env.ingest(type="customer.report", id="my-id", payload={})
    assert c.status == "routed" and d.status == "duplicate" and d.event_id == "my-id"
    e = await env.ingest(type="customer.report", id="my-id", source="someone-else", payload={})
    assert e.http_status == 409 and e.error["code"] == "conflict"
    f = await env.rt.bus.ingest(
        {"type": "customer.report", "payload": {}}, origin="api", idempotency_key="hdr-1"
    )  # header wins
    g = await env.rt.bus.ingest(
        {"type": "customer.report", "payload": {}, "idempotency_key": "body-1"}, origin="api", idempotency_key="hdr-1"
    )
    assert f.status == "routed" and g.status == "duplicate"
    await env.idle()
    assert len(env.client.of("runs.create")) == 3  # duplicates never reach a graph


async def test_semantic_dedupe_folds_repeats_inside_the_window_and_counts_occurrences(env):
    first = await env.ingest(**ALERT, occurred_at="2026-11-05T10:00:00Z")
    second = await env.ingest(**ALERT, occurred_at="2026-11-05T10:01:00Z")
    third = await env.ingest(**ALERT, occurred_at="2026-11-05T10:04:30Z")  # 3.5 min after the SECOND (last_seen moves)
    assert (
        second.status == "suppressed"
        and second.reason == "semantic_duplicate"
        and second.duplicate_of == first.event_id
    )
    assert third.status == "suppressed" and third.duplicate_of == first.event_id
    assert second.event["parent_occurrences"] == 2 and third.event["parent_occurrences"] == 3
    parent = await env.rt.db.run(env.rt.db.get_event, first.event_id)
    assert (
        parent["occurrences"] == 3 and parent["last_seen_at"] == "2026-11-05T10:04:30Z" and parent["status"] == "routed"
    )
    late = await env.ingest(**ALERT, occurred_at="2026-11-05T10:20:00Z")  # outside the 300 s window -> new event
    assert late.status == "routed"
    await env.idle()
    assert len(env.client.of("runs.create")) == 2  # 5 alerts in, 2 runs out


async def test_resolution_resets_the_dedupe_window_and_other_keys_do_not_collide(env):
    a = await env.ingest(**ALERT, occurred_at="2026-11-05T10:00:00Z")
    res = await env.ingest(
        type="alert.resolved",
        subject={"component": "checkout-service"},
        payload={"rule": "error_rate_high"},
        occurred_at="2026-11-05T10:01:00Z",
    )
    assert (
        res.status == "routed" and res.event["type"] == "alert.resolved"
    )  # the resolution is never a duplicate of the firing
    again = await env.ingest(**ALERT, occurred_at="2026-11-05T10:02:00Z")
    assert again.status == "routed" and again.event_id != a.event_id  # window was closed by the resolution
    other = await env.ingest(
        type="alert.fired",
        severity="high",
        subject={"component": "cart-service"},
        payload={"rule": "error_rate_high"},
        occurred_at="2026-11-05T10:02:10Z",
    )
    assert other.status == "routed"


async def test_dedupe_does_not_cross_launches(env):
    ev1 = dict(ALERT, occurred_at="2026-11-05T10:00:00Z", attributes={"launch_id": "lau_0001"})
    ev2 = dict(ALERT, occurred_at="2026-11-05T10:00:00Z", attributes={"launch_id": "lau_0002"})
    assert (await env.ingest(**ev1)).status == "routed"
    assert (
        await env.ingest(**ev2)
    ).status == "routed"  # a re-run of the same scenario is not swallowed by the first run
    assert (await env.ingest(**ev2)).status == "suppressed"


async def test_maintenance_window_suppresses_alerts_for_the_component_only_while_active(env):
    start = await env.ingest(
        type="maintenance.window.started", subject={"component": "checkout-service"}, occurred_at="2026-11-05T10:00:00Z"
    )
    assert start.status == "recorded"
    inside = await env.ingest(**ALERT, occurred_at="2026-11-05T10:10:00Z")
    assert inside.status == "suppressed" and inside.reason == "maintenance_window"
    other = await env.ingest(
        type="alert.fired",
        subject={"component": "cart-service"},
        payload={"rule": "x"},
        occurred_at="2026-11-05T10:10:00Z",
    )
    assert other.status == "routed"
    cust = await env.ingest(
        type="customer.report",
        subject={"component": "checkout-service"},
        payload={},
        occurred_at="2026-11-05T10:10:00Z",
    )
    assert cust.status == "routed"  # humans reporting a problem are never silenced
    await env.ingest(
        type="maintenance.window.ended", subject={"component": "checkout-service"}, occurred_at="2026-11-05T10:30:00Z"
    )
    after = await env.ingest(**ALERT, occurred_at="2026-11-05T10:31:00Z")
    assert after.status == "routed"
    before = await env.ingest(
        type="alert.fired",
        subject={"component": "checkout-service"},
        payload={"rule": "early"},
        occurred_at="2026-11-05T09:00:00Z",
    )
    assert before.status == "routed"  # judged on virtual occurred_at, not arrival time


async def test_global_maintenance_window_covers_every_component(env):
    await env.ingest(type="maintenance.window.started", occurred_at="2026-11-05T10:00:00Z")
    r = await env.ingest(
        type="alert.fired", subject={"component": "anything"}, payload={"rule": "r"}, occurred_at="2026-11-05T10:05:00Z"
    )
    assert r.status == "suppressed"


async def test_unknown_types_go_to_the_dlq_as_no_route_and_lifecycle_types_are_recorded(env):
    bad = await env.ingest(type="made.up.type")
    assert bad.status == "dlq" and bad.reason == "no_route" and bad.http_status == 202 and bad.dead_letter_id
    dl = await env.rt.db.run(env.rt.db.get_dead_letter, bad.dead_letter_id)
    assert dl["reason"] == "no_route" and dl["event_id"] == bad.event_id and dl["envelope"]["type"] == "made.up.type"
    rec = await env.ingest(type="action.executed", payload={"action": "restart"})
    assert rec.status == "recorded" and rec.dispatches == []


async def test_change_events_are_ledgered_and_only_reach_the_router_when_an_incident_is_open(env):
    quiet = await env.ingest(type="deploy.finished", subject={"component": "pricing-service"}, payload={"version": "1"})
    assert quiet.status == "recorded" and quiet.dispatches == []
    await env.rt.bus.emit(
        "incident.opened",
        idempotency_key="io-1",
        component="pricing-service",
        payload={"incident_id": "INC-1", "severity": "SEV2", "title": "t"},
    )
    busy = await env.ingest(type="deploy.finished", subject={"component": "pricing-service"}, payload={"version": "2"})
    assert busy.status == "routed" and busy.dispatches[0]["priority"] == "P1"


async def test_storm_declares_one_correlator_run_and_folds_the_rest(env):
    results = []
    for i in range(14):
        results.append(
            await env.ingest(
                type="alert.fired",
                severity="medium",
                subject={"component": f"svc-{i}"},
                payload={"rule": f"r{i}"},
                occurred_at=f"2026-11-05T10:00:{i:02d}Z",
                trace={"scenario_id": "S12"},
            )
        )
    graphs = [[d["graph_id"] for d in r.dispatches] for r in results]
    assert graphs[:7] == [["event_router"]] * 7
    assert graphs[7] == ["storm_correlator", "event_router"]
    assert all(r.status == "suppressed" and r.reason == "storm_member" for r in results[8:])
    await env.idle()
    storm_runs = [r for r in env.client.of("runs.create") if r["assistant_id"] == "storm_correlator"]
    assert (
        len(storm_runs) == 1
        and len(storm_runs[0]["input"]["alerts"]) == 8
        and storm_runs[0]["input"]["scenario_id"] == "S12"
    )


async def test_backpressure_returns_429_for_p2_p3_but_still_accepts_p0_p1(make_env):
    env = await make_env(queue_cap_p0=2, queue_cap_p1=2, queue_cap_p2=4, queue_cap_p3=2, high_water=0.8, low_water=0.5)
    n = 0
    while not env.rt.queues.bp_active:  # nothing drains: the dispatcher is not running
        r = await env.ingest(
            type="customer.report",
            severity="high" if n % 2 else "medium",
            subject={"component": f"s{n}"},
            payload={"n": n},
        )
        assert r.http_status == 202
        n += 1
    assert env.rt.queues.utilization >= 0.8
    low = await env.ingest(type="customer.report", severity="low", payload={})
    assert low.http_status == 429 and low.status == "backpressure" and low.retry_after == env.rt.es.retry_after_s
    assert low.error["code"] == "backpressure"
    assert await env.rt.db.run(env.rt.db.get_event, "evt_9999999") is None
    count = await env.rt.db.run(env.rt.db.count_events)
    assert (await env.ingest(type="customer.report", severity="low", payload={})).http_status == 429
    assert await env.rt.db.run(env.rt.db.count_events) == count  # a refused event is not stored
    crit = await env.ingest(
        type="customer.report", severity="critical", subject={"component": "pager"}, payload={"n": "boom"}
    )
    assert crit.http_status == 202 and crit.dispatches[0]["priority"] == "P0"  # reserved headroom
    internal = await env.rt.bus.ingest({"type": "customer.report", "severity": "low", "payload": {}}, origin="internal")
    assert internal.http_status == 202  # lifecycle/internal traffic is never throttled
    # draining below low water re-opens the door for P2/P3
    while env.rt.queues.bp_active:
        env.rt.queues.pop()
    assert (await env.ingest(type="customer.report", severity="low", payload={})).http_status == 202
    assert env.rt.bus.backpressure_429_total >= 2


async def test_hard_cap_sheds_lowest_priority_oldest_and_records_event_shed(make_env):
    env = await make_env(queue_cap_p0=2, queue_cap_p1=1, queue_cap_p2=1, queue_cap_p3=1, high_water=1.0, low_water=0.0)
    p3 = [
        await env.rt.bus.ingest({"type": "customer.report", "severity": "low", "payload": {"n": i}}, origin="internal")
        for i in range(2)
    ]
    assert p3[0].status == "routed"
    assert p3[1].status == "routed"  # P3 queue is bounded (cap 1): the OLDEST P3 was shed to admit the newer one
    shed = await env.rt.db.run(env.rt.db.list_events, types=["event.shed"])
    assert (
        len(shed) == 1 and shed[0]["payload"]["event_id"] == p3[0].event_id and shed[0]["payload"]["priority"] == "P3"
    )
    victim = await env.rt.db.run(env.rt.db.get_event, p3[0].event_id)
    assert victim["status"] == "shed" and victim["status_reason"] == "queue_hard_cap"
    disp = await env.rt.db.run(env.rt.db.get_dispatch, p3[0].dispatches[0]["id"])
    assert disp["status"] == "dead" and "shed" in disp["error"]
    assert env.rt.queues.shed_total == 1


async def test_shed_result_reports_status_when_the_new_event_itself_cannot_fit(make_env):
    env = await make_env(queue_cap_p0=2, queue_cap_p1=1, queue_cap_p2=1, queue_cap_p3=1, high_water=1.0, low_water=0.0)
    for i in range(3):
        await env.rt.bus.ingest(
            {"type": "customer.report", "severity": "critical", "payload": {"n": i}}, origin="internal"
        )
    await env.rt.bus.ingest({"type": "customer.report", "severity": "high", "payload": {"n": "h"}}, origin="internal")
    await env.rt.bus.ingest({"type": "customer.report", "severity": "medium", "payload": {"n": "m"}}, origin="internal")
    assert env.rt.queues.total_depth == env.rt.queues.total_cap == 5
    res = await env.rt.bus.ingest({"type": "customer.report", "severity": "low", "payload": {}}, origin="internal")
    assert res.status == "shed" and res.reason == "queue_hard_cap"
    ev = await env.rt.db.run(env.rt.db.get_event, res.event_id)
    assert ev["status"] == "shed"
    assert env.rt.queues.total_depth == 5


async def test_event_row_is_decorated_for_the_ui_and_broadcast_on_sse(env):
    sub = env.rt.hub.subscribe(frozenset({"events", "dispatches"}))
    res = await env.ingest(**ALERT)
    msgs = list(sub.buf)
    ev_msg = next(m for m in msgs if m.channel == "events")
    assert ev_msg.seq == res.event["seq"] and ev_msg.data["summary"].startswith("alert.fired HIGH checkout-service: ")
    assert ev_msg.data["dispatches"][0]["graph_id"] == "event_router"
    assert any(m.channel == "dispatches" for m in msgs)
    json.dumps([m.data for m in msgs])  # everything on the wire is JSON-serialisable


async def test_virtual_time_defaults_follow_the_virtual_clock(env):
    env.rt.vclock.set(virtual_now=BASE.replace(hour=15), speed=1)
    res = await env.ingest(type="customer.report", payload={})
    assert res.event["occurred_at"] == "2026-11-05T15:00:00Z"
    res2 = await env.ingest(type="customer.report", payload={}, occurred_at="2026-11-05T12:00:00.250Z")
    assert res2.event["occurred_at"] == "2026-11-05T12:00:00.250Z"


async def test_reroute_after_dlq_applies_current_rules(env):
    bad = await env.ingest(type="made.up.type")
    assert bad.status == "dlq"
    assert await env.rt.db.run(env.rt.bus.reroute_sync, bad.event_id) is None  # still no route
    assert await env.rt.db.run(env.rt.bus.reroute_sync, "evt_nope") is None


async def test_event_seq_reservation_survives_writers_that_bypass_the_ingest_lock(make_env):
    """Regression: ingest reserved its sequence number with a read-only peek, so the dispatcher (which inserts
    `run.*` events without the ingest lock) could take the same number first -> UNIQUE constraint failure under load.
    Reservation is atomic now and an explicit seq can never move the counter backwards."""
    env = await make_env()
    db = env.rt.db

    base = {
        "type": "run.started",
        "source": "foghorn.internal",
        "severity": "info",
        "occurred_at": "2026-11-05T10:00:00Z",
        "received_at": "2026-11-05T10:00:00Z",
        "subject": {"component": None},
        "payload": {},
    }
    r1, r2 = db.next_n("event_seq"), db.next_n("event_seq")  # two ingests reserve numbers
    assert r2 == r1 + 1
    db.insert_event({**base, "seq": r2, "id": "evt_B"})  # the later reservation lands first
    db.insert_event({**base, "seq": r1, "id": "evt_A"})  # old code reset the counter to r1 here
    free = db.insert_event({**base, "id": "evt_C"})  # a writer that bypasses the lock
    assert free["seq"] == r2 + 1, "the counter must not have moved backwards"
    seqs = [row["seq"] for row in db.query("SELECT seq FROM events ORDER BY seq")]
    assert seqs == sorted(set(seqs)), "no duplicate sequence numbers"
