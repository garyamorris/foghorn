"""Scenarios, replay control, launch (events + direct), fault injector, datasets, evals, memory."""

from __future__ import annotations

import asyncio

from foghorn.contracts.meta import det_thread_id
from tests.unit.events.fakes import scenario_events


async def wait_replay(api):
    await asyncio.wait_for(api.rt.replay.session.task, 5)


async def test_list_get_and_truth_is_hidden_unless_requested(api):
    items = (await api.get("/api/scenarios")).json()
    assert items["available"] is True and [s["scenario_id"] for s in items["items"]] == ["S01", "S02"]
    assert [s["scenario_id"] for s in (await api.get("/api/scenarios?tier=2")).json()["items"]] == ["S02"]
    await api.post("/api/faults", json={"category": "bad_deploy", "component": "checkout-service"})
    assert [s["scenario_id"] for s in (await api.get("/api/scenarios")).json()["items"]] == [
        "S01",
        "S02",
    ]  # generated ones are opt-in
    assert "X-0001" in [
        s["scenario_id"] for s in (await api.get("/api/scenarios?include_generated=true")).json()["items"]
    ]
    s = (await api.get("/api/scenarios/S01")).json()
    assert "truth" not in s
    assert (await api.get("/api/scenarios/S01?reveal_truth=true")).json()["truth"] == {"root_cause": "reco-4412"}
    r = await api.get("/api/scenarios/S99")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_data_layer_unavailable_is_a_503_not_a_crash(api):
    api.data.ok = False
    for url in ("/api/scenarios", "/api/scenarios/S01", "/api/datasets", "/api/streams", "/api/services"):
        r = await api.get(url)
        assert r.status_code == 503 and r.json()["error"]["code"] == "data_unavailable", url
    r = await api.post("/api/scenarios/S01/launch")
    assert r.status_code == 503
    assert (await api.get("/api/health")).json()["components"]["data_api"] == "unavailable"
    assert (await api.get("/api/faults")).json()["categories"] == []  # tolerated
    assert (await api.get("/api/events")).status_code == 200  # everything that does not need data keeps working


async def test_launch_events_mode_publishes_the_scenarios_events_with_run_options(api):
    api.data.events["S01"] = scenario_events("S01", 4)
    r = await api.post(
        "/api/scenarios/S01/launch",
        json={
            "speed": 600,
            "profile": "naive",
            "approval_policy": "strict",
            "breakpoints": ["approval_gate"],
            "token_delay_ms": 5,
            "memory_enabled": False,
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert (
        body["launch_id"] == "lau_0001"
        and body["mode"] == "events"
        and body["events_total"] == 4
        and body["speed"] == 600.0
    )
    assert (
        body["correlation_id"] == "corr_S01_lau_0001"
        and body["incident_id"] is None
        and body["replay"]["state"] == "running"
    )
    assert body["run"] == {"model_profile": "naive", "approval_policy": "strict"}
    await wait_replay(api)
    await api.idle()
    evs = (await api.get("/api/events?scenario_id=S01&type=alert.fired,deploy.finished&order=asc")).json()["items"]
    assert [e["id"] for e in evs] == [f"evt_S01_{i:05d}" for i in range(1, 5)] and all(
        e["correlation_id"] == "corr_S01_lau_0001" for e in evs
    )
    run = api.client.of("runs.create")[0]
    assert run["context"] == {
        "model_profile": "naive",
        "approval_policy": "strict",
        "token_delay_ms": 5,
        "breakpoints": ["approval_gate"],
        "memory_enabled": False,
        "scenario_id": "S01",
    }
    assert (await api.get("/api/replay")).json()["state"] == "finished"


async def test_launch_while_running_conflicts_unless_replace(api):
    api.data.events["S01"] = scenario_events("S01", 3, step_s=3600)
    assert (await api.post("/api/scenarios/S01/launch", json={"speed": 1})).status_code == 202
    again = await api.post("/api/scenarios/S01/launch", json={"speed": 1})
    assert again.status_code == 409 and again.json()["error"]["code"] == "replay_active"
    ok = await api.post("/api/scenarios/S01/launch", json={"speed": 600, "replace": True})
    assert ok.status_code == 202 and ok.json()["launch_id"] == "lau_0002"
    await wait_replay(api)


async def test_launch_validates_run_options(api):
    for bad in (
        {"profile": "genius"},
        {"approval_policy": "yolo"},
        {"speed": 9000},
        {"breakpoints": ["a b; drop"]},
        {"mode": "sideways"},
        {"token_delay_ms": -1},
    ):
        r = await api.post("/api/scenarios/S01/launch", json=bad)
        assert r.status_code == 422, bad
    assert (await api.post("/api/scenarios/NOPE/launch")).status_code == 404


async def test_launch_direct_mode_opens_an_incident_and_starts_the_commander(api):
    r = await api.post("/api/scenarios/S01/launch", json={"mode": "direct", "profile": "cautious", "speed": 60})
    assert r.status_code == 202
    b = r.json()
    assert (
        b["mode"] == "direct"
        and b["incident_id"] == "INC-D0001"
        and b["replay"] is None
        and b["correlation_id"] == "corr_S01_lau_0001"
    )
    assert (
        b["dispatch"]["graph_id"] == "incident_commander"
        and b["dispatch"]["priority"] == "P1"
        and b["run"]["model_profile"] == "cautious"
    )
    await api.idle()
    run = api.client.of("runs.create")[0]
    assert run["assistant_id"] == "incident_commander" and run["multitask_strategy"] == "reject"
    inp = run["input"]
    assert (
        inp["scenario_id"] == "S01"
        and inp["incident"]["id"] == "INC-D0001"
        and inp["incident"]["severity"] == "SEV2"
        and inp["as_of"] == "2026-11-05T10:49:00Z"
    )
    assert inp["correlation_id"] == b["correlation_id"] and len(inp["trigger_event_ids"]) == 1
    assert run["context"]["model_profile"] == "cautious"
    md = api.client.of("threads.create")[0]["metadata"]
    assert (
        md["foghorn_incident_id"] == "INC-D0001"
        and md["foghorn_kind"] == "incident"
        and md["foghorn_scenario_id"] == "S01"
        and md["foghorn_trigger"] == "ui"
    )
    inc = (await api.get("/api/incidents/INC-D0001")).json()
    assert (
        inc["status"] == "open"
        and inc["scenario_id"] == "S01"
        and inc["thread_id"] == det_thread_id("incident_commander", "INC-D0001")
    )
    assert (await api.get("/api/dispatches?incident_id=INC-D0001")).json()["total"] == 1


async def test_replay_endpoints_control_a_session(api):
    api.data.events["S01"] = scenario_events("S01", 6, step_s=60)
    assert (await api.get("/api/replay")).json() == {"state": "idle", "session": None}
    r = await api.post("/api/replay", json={"scenario_id": "S01", "speed": 1})
    assert r.status_code == 202 and r.json()["session"]["state"] == "running"
    assert (await api.post("/api/replay", json={"scenario_id": "S01"})).status_code == 409
    p = (await api.post("/api/replay/pause")).json()
    assert p["state"] == "paused" and p["session"]["state"] == "paused"
    clk = (await api.get("/api/clock")).json()
    assert clk["mode"] == "replay" and clk["paused"] is True and clk["replay"]["session"]["name"] == "S01"
    await asyncio.sleep(
        0.05
    )  # an event already in flight when pause arrived is allowed to finish; then the session is quiet
    pos = (await api.get("/api/replay")).json()["session"]["position"]
    st = (await api.post("/api/replay/step")).json()
    await asyncio.sleep(0.05)
    assert (await api.get("/api/replay")).json()["session"]["position"] == pos + 1 and st["state"] == "paused"
    assert (await api.post("/api/replay/speed", json={"speed": 600})).json()["session"]["speed"] == 600
    assert (await api.post("/api/replay/speed", json={"speed": 0})).status_code == 422
    assert (await api.post("/api/replay/resume")).json()["state"] == "running"
    await wait_replay(api)
    assert (await api.get("/api/replay")).json()["state"] == "finished"
    assert (await api.post("/api/replay/pause")).status_code == 409
    again = await api.post(
        "/api/replay", json={"stream": "stream_quiet_hour", "speed": 600, "loop": False, "jitter_s": 3}
    )
    assert again.status_code == 202 and again.json()["session"]["kind"] == "stream"
    await wait_replay(api)
    assert (await api.post("/api/replay/stop")).json()["state"] == "finished"
    for bad in (
        {},
        {"stream": "a", "scenario_id": "b"},
        {"scenario_id": "NOPE"},
        {"stream": "stream_quiet_hour", "speed": 1000},
    ):
        assert (await api.post("/api/replay", json=bad)).status_code in {400, 404, 422}, bad


async def test_streams_and_services_come_from_the_data_layer(api):
    assert (await api.get("/api/streams")).json()["items"][0]["name"] == "stream_quiet_hour"
    assert (await api.get("/api/services")).json() == {"items": [{"id": "checkout-service"}]}


async def test_fault_injector_registers_a_scenario_and_optionally_launches_it(api):
    r = await api.post(
        "/api/faults",
        json={
            "category": "bad_deploy",
            "component": "checkout-service",
            "magnitude": 0.7,
            "red_herrings": 2,
            "seed": 7,
        },
    )
    assert r.status_code == 201
    b = r.json()
    assert (
        b["scenario_id"] == "X-0001"
        and b["launch"] is None
        and api.data.generated == [("bad_deploy", "checkout-service", 0.7, 2, 7)]
    )
    lst = (await api.get("/api/faults")).json()
    assert (
        lst["items"][0]["scenario_id"] == "X-0001"
        and lst["items"][0]["category"] == "bad_deploy"
        and lst["items"][0]["seed"] == 7
        and lst["categories"] == ["bad_deploy", "flag_ramp"]
    )
    assert (await api.get("/api/scenarios/X-0001")).json()["title"].startswith("bad_deploy")
    launched = await api.post(
        "/api/faults",
        json={
            "category": "flag_ramp",
            "component": "pricing-service",
            "launch": True,
            "speed": 600,
            "run": {"profile": "gullible"},
        },
    )
    assert (
        launched.status_code == 201
        and launched.json()["launch"]["scenario_id"] == "X-0002"
        and launched.json()["launch"]["run"]["model_profile"] == "gullible"
    )
    await wait_replay(api)
    for bad in (
        {"category": "x"},
        {"category": "x", "component": "y", "magnitude": 99},
        {"category": "x", "component": "y", "red_herrings": -1},
    ):
        assert (await api.post("/api/faults", json=bad)).status_code == 422


async def test_datasets_browser(api):
    assert (await api.get("/api/datasets")).json() == {"items": [{"name": "scenarios", "count": 2}], "available": True}
    page = (await api.get("/api/datasets/scenarios?offset=1&limit=1")).json()
    assert page["total"] == 2 and page["offset"] == 1 and len(page["items"]) == 1
    assert page["truth_hidden"] is True
    shown = (await api.get("/api/datasets/scenarios?reveal_truth=true&scenario_id=S01")).json()
    assert shown["truth_hidden"] is False and shown["scenario_id"] == "S01"
    assert (await api.get("/api/datasets/nope")).status_code == 404
    assert (await api.get("/api/datasets/scenarios?limit=0")).status_code == 422


async def test_start_eval_creates_a_run_and_routes_eval_requested_to_eval_runner(api):
    r = await api.post(
        "/api/evals", json={"suite": "core16", "profile": "naive", "concurrency": 2, "label": "baseline"}
    )
    assert r.status_code == 202
    b = r.json()
    run = b["eval_run"]
    assert (
        run["eval_run_id"] == "evr_0001"
        and run["status"] == "requested"
        and run["profile"] == "naive"
        and run["label"] == "baseline"
        and run["event_id"] == b["event"]["event_id"]
    )
    assert b["event"]["status"] == "routed" and b["event"]["dispatches"][0]["graph_id"] == "eval_runner"
    await api.idle()
    call = api.client.of("runs.create")[0]
    assert call["assistant_id"] == "eval_runner" and call["input"] == {
        "suite": "core16",
        "profile": "naive",
        "concurrency": 2,
        "eval_run_id": "evr_0001",
        "label": "baseline",
    }
    assert call["metadata"]["foghorn_eval_run_id"] == "evr_0001" and call["metadata"]["foghorn_kind"] == "eval"
    got = (await api.get("/api/evals/evr_0001")).json()
    assert got["thread_id"] and got["suite"] == "core16"
    assert (await api.get("/api/evals")).json()["items"][0]["eval_run_id"] == "evr_0001"
    assert (await api.get("/api/evals/evr_9")).status_code == 404
    assert (await api.post("/api/evals", json={"suite": "custom"})).status_code == 422
    assert (await api.post("/api/evals", json={"suite": "nope"})).status_code == 422
    assert (await api.post("/api/evals", json={"profile": "nope"})).status_code == 422


async def test_memory_search_namespaces_and_item(api):
    await api.client.store.put_item(["incidents", "checkout-service"], "inc-1", {"text": "rollback fixed it"})
    await api.client.store.put_item(["prefs", "team"], "p1", {"text": "prefer rollback"})
    ns = (await api.get("/api/memory/namespaces")).json()["items"]
    assert ["prefs", "team"] in ns and ["incidents", "checkout-service"] in ns
    hits = (await api.get("/api/memory/search?q=rollback&namespace=incidents&limit=5")).json()
    assert (
        hits["query"] == "rollback"
        and [h["key"] for h in hits["items"]] == ["inc-1"]
        and hits["items"][0]["score"] == 0.5
    )
    assert len((await api.get("/api/memory/search?q=rollback")).json()["items"]) == 2
    assert (await api.get("/api/memory/item?namespace=prefs.team&key=p1")).json()["value"] == {
        "text": "prefer rollback"
    }
    assert (await api.get("/api/memory/item?namespace=prefs.team&key=nope")).status_code == 404

    async def down(*_a, **_k):
        raise RuntimeError("store down")

    api.client.store.search_items = down  # type: ignore[method-assign]
    assert (await api.get("/api/memory/search?q=x")).status_code == 503
