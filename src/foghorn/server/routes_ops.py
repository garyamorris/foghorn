"""Operations API: health, config, summary, clock, queues, dead letters, admin."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from foghorn.events.service import EventsRuntime
from foghorn.events.vclock import MAX_SPEED, parse_iso
from foghorn.server.deps import ApiError, get_rt, require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
RT = Annotated[EventsRuntime, Depends(get_rt)]


@router.get("/health")
async def health(rt: RT) -> dict[str, Any]:
    return rt.health()


@router.get("/config")
async def config(rt: RT) -> dict[str, Any]:
    return rt.config_snapshot()


@router.get("/summary")
async def summary(rt: RT) -> dict[str, Any]:
    return await rt.summary()


# ---------------------------------------------------------------------------------------------------------- clock
class ClockBody(BaseModel):
    virtual_now: str | None = None
    speed: float | None = Field(None, gt=0, le=MAX_SPEED)
    paused: bool | None = None


@router.get("/clock")
async def get_clock(rt: RT) -> dict[str, Any]:
    return rt.clock_snapshot()


@router.post("/clock")
async def set_clock(rt: RT, body: ClockBody) -> dict[str, Any]:
    try:
        vn = parse_iso(body.virtual_now) if body.virtual_now else None
    except ValueError as exc:
        raise ApiError(422, "schema_invalid", "virtual_now must be an ISO-8601 timestamp") from exc
    rt.vclock.set(virtual_now=vn, speed=body.speed, paused=body.paused)
    await rt.db.run(rt.db.kv_set, "clock", rt._clock_record())
    snap = rt.clock_snapshot()
    rt.hub.publish("clock", snap)
    return snap


# --------------------------------------------------------------------------------------------------------- queues
@router.get("/queues")
async def queues(rt: RT) -> dict[str, Any]:
    return await rt.queue_stats()


# ----------------------------------------------------------------------------------------------------------- DLQ
@router.get("/dlq")
async def list_dlq(
    rt: RT,
    status: str = "open",
    reason: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    if status not in {"open", "retried", "discarded", "all"}:
        raise ApiError(400, "bad_request", "status must be open|retried|discarded|all")
    rows, total = await rt.db.run(rt.db.list_dead_letters, status=status, reason=reason, limit=limit, offset=offset)
    return {"items": rows, "total": total}


@router.get("/dlq/{dlq_id}")
async def get_dlq(rt: RT, dlq_id: str) -> dict[str, Any]:
    row = await rt.db.run(rt.db.get_dead_letter, dlq_id)
    if row is None:
        raise ApiError(404, "not_found", f"dead letter {dlq_id!r} not found")
    return row


@router.post("/dlq/{dlq_id}/retry", status_code=202)
async def retry_dlq(rt: RT, dlq_id: str) -> dict[str, Any]:
    return await rt.dispatcher.retry_dead_letter(dlq_id)


class DiscardBody(BaseModel):
    reason: str | None = None
    by: str = "operator"


@router.post("/dlq/{dlq_id}/discard")
async def discard_dlq(rt: RT, dlq_id: str, body: DiscardBody | None = None) -> dict[str, Any]:
    b = body or DiscardBody()
    row = await rt.dispatcher.discard_dead_letter(dlq_id, by=b.by, reason=b.reason)
    return {"status": "discarded", "dead_letter": row}


# --------------------------------------------------------------------------------------------------------- admin
class ResetBody(BaseModel):
    confirm: str


@router.post("/admin/reset")
async def reset(rt: RT, body: ResetBody) -> dict[str, Any]:
    if body.confirm != "reset":
        raise ApiError(400, "bad_request", 'send {"confirm": "reset"} to confirm')
    if rt.replay.active:
        await rt.replay.stop()
    deleted = await rt.db.run(rt.db.reset)
    rt.queues.reset()
    rt.bus.storm.reset()
    rt.vclock.set(virtual_now=rt.wall_now(), speed=1.0, paused=False)
    return {"status": "reset", "deleted": deleted}


class SettingsBody(BaseModel):
    """Operator-switchable defaults. Every field is optional (partial update); unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")
    chaos_mode: bool | None = None
    default_model_profile: Literal["competent", "naive", "cautious", "gullible"] | None = None
    default_approval_policy: Literal["strict", "standard", "auto_low_risk"] | None = None
    default_token_delay_ms: int | None = Field(None, ge=0, le=2000)


@router.get("/admin/settings")
@router.get("/settings", include_in_schema=False)
async def get_settings_view(rt: RT) -> dict[str, Any]:
    return rt.runtime_settings()


@router.put("/admin/settings")
@router.put("/settings", include_in_schema=False)
@router.post("/settings", include_in_schema=False)
async def put_settings(rt: RT, body: SettingsBody) -> dict[str, Any]:
    return await rt.update_runtime_settings(body.model_dump(exclude_none=True))


class AmbientBody(BaseModel):
    enabled: bool


@router.post("/admin/ambient")
async def ambient(rt: RT, body: AmbientBody) -> dict[str, Any]:
    enabled = await rt.ambient.set_enabled(body.enabled)
    return {"ambient_enabled": enabled}
