"""Integrator requests: runtime settings endpoint, incident run_id/graph_id, public scenario features/category,
the persisted eval-case shape."""

from __future__ import annotations

import asyncio

import pytest

from foghorn.common.lifecycle import emit_event
from foghorn.events.dataport import RealDataPort, derive_category, derive_features
from foghorn.events.service import EventsRuntime
from tests.unit.events.fakes import make_runtime


async def settle(api):
    for _ in range(100):
        if api.rt._lifecycle_q.empty() and api.rt._backlog == 0:  # noqa: SLF001
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------------------- (1) runtime settings
async def test_settings_get_defaults_and_partial_put(api):
    cur = (await api.get("/api/admin/settings")).json()
    assert cur == {
        "chaos_mode": False,
        "default_model_profile": "competent",
        "default_approval_policy": "standard",
        "default_token_delay_ms": 0,
    }
    r = await api.http.put("/api/admin/settings", json={"chaos_mode": True, "default_token_delay_ms": 30})
    assert r.status_code == 200 and r.json() == {
        **cur,
        "chaos_mode": True,
        "default_token_delay_ms": 30,
    }  # untouched fields stay
    assert (await api.get("/api/admin/settings")).json()["chaos_mode"] is True
    assert (await api.get("/api/settings")).json() == r.json()  # alias used by the SPA
    assert (await api.post("/api/settings", json={"default_model_profile": "naive"})).json()[
        "default_model_profile"
    ] == "naive"
    cfg = (await api.get("/api/config")).json()["defaults"]
    assert cfg["model_profile"] == "naive" and cfg["chaos_mode"] is True and cfg["token_delay_ms"] == 30


@pytest.mark.parametrize(
    "bad",
    [
        {"default_model_profile": "genius"},
        {"default_approval_policy": "yolo"},
        {"default_token_delay_ms": -1},
        {"default_token_delay_ms": 99999},
        {"chaos_mode": "maybe"},
        {"surprise": 1},
    ],
)
async def test_settings_are_validated(api, bad):
    r = await api.http.put("/api/admin/settings", json=bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "schema_invalid"
    assert (await api.get("/api/admin/settings")).json()["chaos_mode"] is False


async def test_settings_apply_to_runs_created_after_the_change_and_launch_options_still_win(api):
    await api.http.put(
        "/api/admin/settings",
        json={
            "chaos_mode": True,
            "default_model_profile": "gullible",
            "default_approval_policy": "strict",
            "default_token_delay_ms": 25,
        },
    )
    await api.event(type="customer.report", payload={"n": 1})
    await api.idle()
    ctx = api.client.of("runs.create")[-1]["context"]
    assert (
        ctx["chaos"] == "tools"
        and ctx["model_profile"] == "gullible"
        and ctx["approval_policy"] == "strict"
        and ctx["token_delay_ms"] == 25
    )
    api.data.events["S01"] = [dict(e, type="customer.report", dedupe_key=None) for e in api.data.events["S01"]][:1]
    r = await api.post("/api/scenarios/S01/launch", json={"speed": 600, "profile": "cautious", "chaos": "custom"})
    assert r.status_code == 202
    await asyncio.wait_for(api.rt.replay.session.task, 5)
    await api.idle()
    launched = api.client.of("runs.create")[-1]["context"]
    assert (
        launched["model_profile"] == "cautious" and launched["chaos"] == "custom"
    )  # per-launch beats the operator default
    await api.http.put("/api/admin/settings", json={"chaos_mode": False})
    await api.event(type="customer.report", payload={"n": 2})
    await api.idle()
    assert "chaos" not in api.client.of("runs.create")[-1]["context"]


async def test_settings_require_auth_and_survive_a_restart(api, tmp_path, monkeypatch):
    await api.http.put("/api/admin/settings", json={"chaos_mode": True, "default_model_profile": "naive"})
    await api.rt.stop()
    rt2, *_ = make_runtime(tmp_path)  # same state DB
    assert isinstance(rt2, EventsRuntime)
    await rt2.start(background=False)
    try:
        assert (
            rt2.runtime_settings()["chaos_mode"] is True and rt2.runtime_settings()["default_model_profile"] == "naive"
        )
    finally:
        await rt2.stop()
    monkeypatch.setenv("FOGHORN_DASHBOARD_API_TOKEN", "t")
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    for method in ("GET", "PUT"):
        assert (await api.http.request(method, "/api/admin/settings", json={})).status_code == 401


# ------------------------------------------------------------------------------ (2) incident run_id / graph_id
async def open_incident(api, iid="INC-0001", thread="t-1"):
    emit_event(
        "incident.opened",
        idempotency_key=f"o-{iid}",
        component="checkout-service",
        correlation_id=f"corr_{iid}",
        payload={
            "incident_id": iid,
            "severity": "SEV2",
            "title": "t",
            "thread_id": thread,
            "graph_id": "incident_commander",
        },
    )
    await settle(api)


async def test_incident_detail_names_the_latest_run_from_the_agent_server_when_no_dispatch_exists(api):
    await open_incident(api)
    d = (await api.get("/api/incidents/INC-0001")).json()
    assert d["run_id"] is None and d["graph_id"] == "incident_commander"  # thread has no runs yet
    api.client.threads.items["t-1"] = {
        "thread_id": "t-1",
        "status": "idle",
        "metadata": {},
        "interrupts": {},
        "values": {},
    }
    api.client.runs.items["run-a"] = {
        "run_id": "run-a",
        "thread_id": "t-1",
        "status": "success",
        "error": None,
        "polls": 0,
    }
    api.client.runs.items["run-b"] = {
        "run_id": "run-b",
        "thread_id": "t-1",
        "status": "running",
        "error": None,
        "polls": 0,
    }
    d = (await api.get("/api/incidents/INC-0001")).json()
    assert (
        d["run_id"] == "run-b" and d["graph_id"] == "incident_commander"
    )  # the newest run of the incident's own thread
    api.client.fail_runs_list = True
    assert (await api.get("/api/incidents/INC-0001")).json()[
        "run_id"
    ] is None  # Agent Server hiccup: field is null, route still 200


async def test_incident_detail_prefers_the_dispatch_run_on_the_incident_thread(api):
    r = await api.post("/api/scenarios/S01/launch", json={"mode": "direct"})
    iid = r.json()["incident_id"]
    await api.idle()
    d = (await api.get(f"/api/incidents/{iid}")).json()
    disp = d["dispatches"][0]
    assert (
        d["run_id"] == disp["run_id"] and d["graph_id"] == "incident_commander" and disp["thread_id"] == d["thread_id"]
    )


# -------------------------------------------------------------------- (3) public scenario features / category
async def test_scenario_public_view_carries_features_and_category_and_category_can_be_hidden(api):
    s = (await api.get("/api/scenarios")).json()["items"][0]
    assert s["category"] == "bad_deploy" and s["features"] == ["approval"] and "truth" not in s
    assert (await api.get("/api/scenarios/S02")).json()["features"] == ["question", "approval", "multiple interrupts"]
    api.rt.es.scenario_category_public = False
    hidden = (await api.get("/api/scenarios")).json()["items"][0]
    assert "category" not in hidden and hidden["features"] == ["approval"]
    assert "category" not in (await api.get("/api/scenarios/S01")).json()
    assert (await api.get("/api/scenarios/S01?reveal_truth=true")).json()[
        "category"
    ] == "bad_deploy"  # the reveal toggle shows it


def test_feature_chips_describe_what_a_scenario_exercises_not_what_the_answer_is():
    truth = {
        "expected_interrupts": [
            {"kind": "question", "at": "clarify"},
            {"kind": "approval", "approvers": 2, "at": "gate"},
            {"kind": "break_glass"},
        ],
        "expected_memory": {"recall": "INC-0061", "write_lesson": True},
        "flags": ["prompt_injection_detected"],
        "is_noise": False,
        "root_cause": {"category": "config_change", "component": "pricing-service", "change_id": "cfg-7f21"},
        "remediation": {"preferred": ["revert_flag"]},
    }
    feats = derive_features(truth, {"stats": {"alerts": 58}})
    assert feats == [
        "question",
        "approval",
        "two-person approval",
        "break-glass",
        "multiple interrupts",
        "memory recall",
        "injection",
        "alert storm",
    ]
    blob = " ".join(feats)
    for secret in ("pricing", "cfg-7f21", "revert", "config_change", "INC-0061"):
        assert secret not in blob
    assert derive_features({}, {}) == [] and derive_features({"is_noise": True}, {"stats": {"alerts": 4}}) == ["no-op"]
    assert (
        derive_category(truth) == "config_change"
        and derive_category({"is_noise": True, "root_cause": None}) == "planned_maintenance"
    )
    assert derive_category({"root_cause": None}) is None


def test_real_data_port_enriches_without_leaking_other_truth():
    port = RealDataPort()
    if not port.available():
        pytest.skip("foghorn.data.api not available yet")
    rows = port.list_scenarios()
    s10 = next(r for r in rows if r["scenario_id"] == "S10")
    assert s10["category"] and s10["features"] and "question" in s10["features"]
    assert "truth" not in s10 and "root_cause" not in s10 and "remediation" not in s10
    assert set(s10) <= {"scenario_id", "stats", "summary", "tier", "title", "trigger", "world", "features", "category"}
    assert port.get_scenario("S10")["features"] == s10["features"]  # cached, same answer
    assert "alert storm" in next(r for r in rows if r["scenario_id"] == "S12")["features"]


# ---------------------------------------------------------------------------------- (4) eval-case result shape
CASE = {
    "scenario_id": "S10",
    "scores": {"root_cause": 1.0, "action_safety": 0.5, "bogus": "n/a"},
    "passed": True,
    "incident_id": "INC-0007",
    "thread_id": "t-77",
    "expected": {"root_cause": "pricing-service", "severity": "SEV2"},
    "actual": {"root_cause": "pricing-service", "severity": "SEV3"},
    "gate_trips": ["safety"],
}


async def test_eval_cases_are_persisted_in_one_normalised_shape(api):
    emit_event("eval.requested", idempotency_key="r", payload={"eval_run_id": "evr_5", "suite": "core16"})
    emit_event(
        "eval.progress", idempotency_key="p1", payload={"eval_run_id": "evr_5", "done": 1, "total": 2, "case": CASE}
    )
    emit_event(
        "eval.progress",
        idempotency_key="p2",
        payload={
            "eval_run_id": "evr_5",
            "done": 2,
            "total": 2,
            "case": {"scenario_id": "S01", "scores": {"root_cause": 0.0}, "score": 0.2},
        },
    )
    await settle(api)
    run = (await api.get("/api/evals/evr_5")).json()
    c10, c01 = run["cases"]
    assert c10 == {
        **CASE,
        "scores": {"root_cause": 1.0, "action_safety": 0.5},
        "score": 0.75,
        "run_id": None,
    }  # non-numeric scores dropped, score = mean, extras kept
    assert (
        c10["incident_id"] == "INC-0007"
        and c10["thread_id"] == "t-77"
        and c10["expected"]["severity"] == "SEV2"
        and c10["actual"]["severity"] == "SEV3"
    )
    assert (
        c01["score"] == 0.2
        and c01["passed"] is None
        and c01["incident_id"] is None
        and c01["expected"] is None
        and c01["actual"] is None
    )  # explicit score wins
    assert run["evaluators"] == ["root_cause", "action_safety"]  # matrix columns, first-seen order
    emit_event(
        "eval.progress",
        idempotency_key="p3",
        payload={
            "eval_run_id": "evr_5",
            "done": 2,
            "total": 2,
            "case": {**CASE, "passed": False, "scores": {"root_cause": 0.0}},
        },
    )
    await settle(api)
    again = (await api.get("/api/evals/evr_5")).json()
    assert (
        len(again["cases"]) == 2
        and again["cases"][0]["passed"] is False
        and again["cases"][0]["scores"] == {"root_cause": 0.0}
    )  # upsert by scenario_id


async def test_eval_finished_carries_cases_and_summary_in_the_same_shape(api):
    emit_event(
        "eval.finished",
        idempotency_key="f",
        payload={
            "eval_run_id": "evr_6",
            "summary": {"mean_score": 0.8, "by_evaluator": {"root_cause": 0.9}},
            "cases": [CASE, "junk", {"no_scenario": 1}, {"scenario_id": "S02", "scores": {"root_cause": 1}}],
        },
    )
    await settle(api)
    run = (await api.get("/api/evals/evr_6")).json()
    assert run["status"] == "finished" and [c["scenario_id"] for c in run["cases"]] == [
        "S10",
        "S02",
    ]  # malformed entries skipped, never a 500
    assert (
        run["cases"][1]["scores"] == {"root_cause": 1.0}
        and run["cases"][1]["score"] == 1.0
        and run["summary"]["by_evaluator"] == {"root_cause": 0.9}
    )
    assert (await api.get("/api/evals")).json()["items"][0]["evaluators"] == ["root_cause", "action_safety"]
