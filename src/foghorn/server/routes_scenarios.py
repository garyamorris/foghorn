"""Replay control, scenarios (list / launch), the fault injector, streams and the service catalog."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from foghorn.contracts.meta import (
    CTX_APPROVAL_POLICY,
    CTX_AS_OF,
    CTX_BREAKPOINTS,
    CTX_CHAOS,
    CTX_MEMORY_ENABLED,
    CTX_MODEL_PROFILE,
    CTX_TOKEN_DELAY_MS,
    M_CORRELATION_ID,
    M_EVENT_ID,
    M_INCIDENT_ID,
    M_SCENARIO_ID,
    M_TRIGGER,
    MODEL_PROFILES,
    det_thread_id,
    thread_metadata,
)
from foghorn.events.config import APPROVAL_POLICIES
from foghorn.events.dataport import DataUnavailable, UnknownScenario
from foghorn.events.routing import run_context
from foghorn.events.service import EventsRuntime
from foghorn.server.deps import ApiError, get_rt, require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
RT = Annotated[EventsRuntime, Depends(get_rt)]


class RunOptions(BaseModel):
    profile: str | None = None
    approval_policy: str | None = None
    breakpoints: list[str] = Field(default_factory=list)
    token_delay_ms: int | None = Field(None, ge=0, le=2000)
    memory_enabled: bool | None = None
    chaos: str | None = None
    as_of: str | None = None

    def context(self) -> dict[str, Any]:
        if self.profile and self.profile not in MODEL_PROFILES:
            raise ApiError(422, "schema_invalid", f"profile must be one of {list(MODEL_PROFILES)}")
        if self.approval_policy and self.approval_policy not in APPROVAL_POLICIES:
            raise ApiError(422, "schema_invalid", f"approval_policy must be one of {list(APPROVAL_POLICIES)}")
        bad = [b for b in self.breakpoints if not b.replace("_", "").isalnum()]
        if bad:
            raise ApiError(422, "schema_invalid", f"invalid breakpoint node names: {bad}")
        raw = {
            CTX_MODEL_PROFILE: self.profile,
            CTX_APPROVAL_POLICY: self.approval_policy,
            CTX_BREAKPOINTS: self.breakpoints or None,
            CTX_TOKEN_DELAY_MS: self.token_delay_ms,
            CTX_MEMORY_ENABLED: self.memory_enabled,
            CTX_CHAOS: self.chaos,
            CTX_AS_OF: self.as_of,
        }
        return {k: v for k, v in raw.items() if v is not None}


class ReplayBody(BaseModel):
    stream: str | None = None
    scenario_id: str | None = None
    speed: float | None = Field(None, gt=0, le=600)
    loop: bool = False
    start_at: str | None = None
    replace: bool = False
    jitter_s: float = Field(0.0, ge=0, le=3600)
    run: RunOptions = Field(default_factory=RunOptions)


class LaunchBody(RunOptions):
    speed: float | None = Field(None, gt=0, le=600)
    mode: Literal["events", "direct"] = "events"
    replace: bool = False
    run: RunOptions | None = None


class FaultBody(BaseModel):
    category: str
    component: str
    magnitude: float = Field(0.5, ge=0, le=10)
    red_herrings: int = Field(0, ge=0, le=10)
    seed: int = 0
    launch: bool = False
    speed: float | None = Field(None, gt=0, le=600)
    run: RunOptions = Field(default_factory=RunOptions)


async def call_data(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a (blocking) data-layer call off the loop, mapping its failures onto API errors."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except DataUnavailable as exc:
        raise ApiError(503, "data_unavailable", str(exc)[:300]) from exc
    except UnknownScenario as exc:
        raise ApiError(404, "not_found", f"unknown: {exc.args[0] if exc.args else ''}") from exc
    except KeyError as exc:
        raise ApiError(404, "not_found", f"unknown: {exc.args[0] if exc.args else ''}") from exc
    except ValueError as exc:
        raise ApiError(422, "schema_invalid", str(exc)[:300]) from exc


# ----------------------------------------------------------------------------------------------------- replay
@router.get("/replay")
async def replay_status(rt: RT) -> dict[str, Any]:
    return rt.replay.status()


@router.post("/replay", status_code=202)
async def replay_start(rt: RT, body: ReplayBody) -> dict[str, Any]:
    if body.scenario_id:  # fail fast with 404 instead of a half-started session
        await call_data(rt.data.get_scenario, body.scenario_id)
    session = await rt.replay.start(
        stream=body.stream,
        scenario_id=body.scenario_id,
        speed=body.speed,
        loop=body.loop,
        start_at=body.start_at,
        replace=body.replace,
        jitter_s=body.jitter_s,
        run_ctx=body.run.context(),
    )
    return {"session": session}


@router.post("/replay/pause")
async def replay_pause(rt: RT) -> dict[str, Any]:
    await rt.replay.pause()
    return rt.replay.status()


@router.post("/replay/resume")
async def replay_resume(rt: RT) -> dict[str, Any]:
    await rt.replay.resume()
    return rt.replay.status()


@router.post("/replay/step")
async def replay_step(rt: RT) -> dict[str, Any]:
    await rt.replay.step()
    return rt.replay.status()


class SpeedBody(BaseModel):
    speed: float = Field(gt=0, le=600)


@router.post("/replay/speed")
async def replay_speed(rt: RT, body: SpeedBody) -> dict[str, Any]:
    await rt.replay.set_speed(body.speed)
    return rt.replay.status()


@router.post("/replay/stop")
async def replay_stop(rt: RT) -> dict[str, Any]:
    await rt.replay.stop()
    return rt.replay.status()


@router.get("/streams")
async def streams(rt: RT) -> dict[str, Any]:
    items = await call_data(rt.data.list_streams)
    return {"items": items, "available": True}


@router.get("/services")
async def services(rt: RT) -> dict[str, Any]:
    return {"items": await call_data(rt.data.service_catalog)}


# --------------------------------------------------------------------------------------------------- scenarios
def _public_view(rt: EventsRuntime, scenario: dict[str, Any]) -> dict[str, Any]:
    """The category is the type of the root cause: operators who want to play detective can hide it (config)."""
    if rt.es.scenario_category_public:
        return scenario
    return {k: v for k, v in scenario.items() if k != "category"}


@router.get("/scenarios")
async def list_scenarios(rt: RT, tier: int | None = None, include_generated: bool = False) -> dict[str, Any]:
    items = await call_data(rt.data.list_scenarios, tier, include_generated=include_generated)
    return {"items": [_public_view(rt, s) for s in items], "available": True}


@router.get("/scenarios/{scenario_id}")
async def get_scenario(rt: RT, scenario_id: str, reveal_truth: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = await call_data(rt.data.get_scenario, scenario_id, reveal_truth=reveal_truth)
    return out if reveal_truth else _public_view(rt, out)


async def launch_scenario(
    rt: RT, scenario_id: str, *, speed: float | None, mode: str, replace: bool, opts: RunOptions
) -> dict[str, Any]:
    scenario = await call_data(rt.data.get_scenario, scenario_id)
    ctx = opts.context()
    if mode == "direct":
        return await _launch_direct(rt, scenario_id, scenario, speed, ctx)
    session = await rt.replay.start(scenario_id=scenario_id, speed=speed, replace=replace, run_ctx=ctx, mode="events")
    launch = await rt.db.run(rt.db.get_launch, session["launch_id"])
    return {
        "launch_id": session["launch_id"],
        "scenario_id": scenario_id,
        "mode": "events",
        "events_total": session["total"],
        "speed": session["speed"],
        "correlation_id": (launch or {}).get("correlation_id"),
        "incident_id": None,
        "replay": session,
        "run": {
            k: v
            for k, v in run_context({}, {**ctx, "scenario_id": scenario_id}, rt.es).items()
            if k in {"model_profile", "approval_policy"}
        },
    }


async def _launch_direct(
    rt: EventsRuntime, scenario_id: str, scenario: dict[str, Any], speed: float | None, ctx: dict[str, Any]
) -> dict[str, Any]:
    spd = float(speed or rt.es.replay_speed)
    n = await rt.db.run(rt.db.next_n, "incident")
    incident_id = f"INC-D{n:04d}"
    launch = await rt.db.run(
        rt.db.insert_launch,
        scenario_id=scenario_id,
        mode="direct",
        speed=spd,
        context={**ctx, "scenario_id": scenario_id},
        created_at=rt.now_iso(),
        events_total=0,
        correlation_id=None,
    )
    corr = f"corr_{scenario_id}_{launch['id']}"
    thread_id = det_thread_id("incident_commander", incident_id)
    incident = {
        "id": incident_id,
        "severity": str(scenario.get("severity") or "SEV2"),
        "title": str(scenario.get("title") or scenario_id),
        "status": "open",
    }
    res = await rt.bus.emit(
        "incident.opened",
        idempotency_key=f"incident.opened:direct:{launch['id']}",
        payload={
            "incident_id": incident_id,
            "severity": incident["severity"],
            "title": incident["title"],
            "status": "open",
            "thread_id": thread_id,
            "scenario_id": scenario_id,
            "graph_id": "incident_commander",
        },
        severity="high",
        correlation_id=corr,
        scenario_id=scenario_id,
    )
    assert res.event_id is not None
    cmd_input = await call_data(rt.data.commander_input, scenario_id, incident)
    cmd_input.update({"correlation_id": corr, "trigger_event_ids": [res.event_id]})
    event = {
        "id": res.event_id,
        "correlation_id": corr,
        "trace": {"scenario_id": scenario_id},
        "attributes": {"launch_id": launch["id"]},
        "source": "ui.inject",
        "type": "incident.opened",
    }
    md = thread_metadata(
        "incident",
        "incident_commander",
        **{
            M_EVENT_ID: res.event_id,
            M_CORRELATION_ID: corr,
            M_INCIDENT_ID: incident_id,
            M_SCENARIO_ID: scenario_id,
            M_TRIGGER: "ui",
        },
    )
    plan = {
        "graph_id": "incident_commander",
        "assistant_id": "incident_commander",
        "thread_id": thread_id,
        "input": cmd_input,
        "thread_metadata": md,
        "context": run_context(event, {**ctx, "scenario_id": scenario_id}, rt.es),
        "multitask_strategy": "reject",
        "on_completion": "keep",
        "incident_id": incident_id,
        "trigger": "ui",
        "chaos": None,
    }
    d = await rt.bus.create_dispatch(res.event_id, "direct-launch", "P1", plan)
    await rt.db.run(rt.db.update_launch, launch["id"], correlation_id=corr, incident_id=incident_id)
    return {
        "launch_id": launch["id"],
        "scenario_id": scenario_id,
        "mode": "direct",
        "events_total": 0,
        "speed": spd,
        "correlation_id": corr,
        "incident_id": incident_id,
        "replay": None,
        "dispatch": {k: v for k, v in d.items() if k != "plan"},
        "run": {k: plan["context"][k] for k in ("model_profile", "approval_policy") if k in plan["context"]},
    }


@router.post("/scenarios/{scenario_id}/launch", status_code=202)
async def launch(rt: RT, scenario_id: str, body: LaunchBody | None = None) -> dict[str, Any]:
    b = body or LaunchBody()
    return await launch_scenario(rt, scenario_id, speed=b.speed, mode=b.mode, replace=b.replace, opts=b.run or b)


# ------------------------------------------------------------------------------------------------------- faults
@router.post("/faults", status_code=201)
async def inject_fault(rt: RT, body: FaultBody) -> dict[str, Any]:
    scenario: dict[str, Any] = await call_data(
        rt.data.build_fault_scenario, body.category, body.component, body.magnitude, body.red_herrings, body.seed
    )
    sid = str(scenario.get("scenario_id") or scenario.get("id") or "")
    if not sid:
        raise ApiError(502, "data_unavailable", "build_fault_scenario returned no scenario id")
    await rt.db.run(
        rt.db.record_generated,
        sid,
        category=body.category,
        component=body.component,
        magnitude=body.magnitude,
        red_herrings=body.red_herrings,
        seed=body.seed,
        created_at=rt.now_iso(),
    )
    launched = None
    if body.launch:
        launched = await launch_scenario(rt, sid, speed=body.speed, mode="events", replace=False, opts=body.run)
    return {"scenario": scenario, "scenario_id": sid, "launch": launched}


@router.get("/faults")
async def list_faults(rt: RT) -> dict[str, Any]:
    items = await rt.db.run(rt.db.list_generated)
    try:
        cats = await asyncio.to_thread(rt.data.fault_categories)
    except DataUnavailable:
        cats = []
    return {"items": items, "categories": cats}
