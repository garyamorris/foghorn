"""Incidents, approvals inbox, evals, memory search and the datasets browser."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from foghorn.events.pipeline import decorate_event
from foghorn.events.service import EventsRuntime
from foghorn.logging import get_logger
from foghorn.server.deps import ApiError, get_rt, require_auth
from foghorn.server.routes_events import csv
from foghorn.server.routes_scenarios import call_data

log = get_logger("web.work")
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
RT = Annotated[EventsRuntime, Depends(get_rt)]


# -------------------------------------------------------------------------------------------------- incidents
def _incident_view(rt: EventsRuntime, row: dict[str, Any], pending: int = 0) -> dict[str, Any]:
    out = dict(row)
    out["pending_approvals"] = pending
    out["studio"] = rt.studio_links(row.get("thread_id"))
    return out


@router.get("/incidents")
async def list_incidents(
    rt: RT,
    status: str | None = None,
    severity: str | None = None,
    scenario_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    def _sync() -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        rows, total = rt.db.list_incidents(
            statuses=csv(status), severities=csv(severity), scenario_id=scenario_id, limit=limit, offset=offset
        )
        pend = {r["incident_id"]: 0 for r in rows}
        for a in rt.db.list_approvals(statuses=("pending",), limit=1000):
            if a.get("incident_id") in pend:
                pend[a["incident_id"]] += 1
        return rows, total, pend

    rows, total, pend = await rt.db.run(_sync)
    return {"items": [_incident_view(rt, r, pend.get(r["incident_id"], 0)) for r in rows], "total": total}


async def _latest_run(
    rt: EventsRuntime, inc: dict[str, Any], dispatches: list[dict[str, Any]]
) -> tuple[str | None, str | None]:
    """The newest run of the incident's own thread (what the console joins): a dispatch we created if there is one,
    else ask the Agent Server (graphs such as the commander are started by ``event_router`` through the SDK, so there is
    no dispatch row for them)."""
    thread_id = inc.get("thread_id")
    own = next((d for d in dispatches if d.get("run_id") and thread_id and d.get("thread_id") == thread_id), None)
    if own:
        return own["run_id"], own.get("graph_id")
    if thread_id:
        try:
            runs = await rt.client.runs.list(thread_id, limit=5)
            if runs:
                newest = max(runs, key=lambda r: str(r.get("created_at") or ""))
                return str(newest["run_id"]), inc.get("graph_id")
        except Exception as exc:
            log.info("incident_run_lookup_failed", thread_id=thread_id, error=repr(exc)[:200])
    any_run = next((d for d in dispatches if d.get("run_id")), None)
    return (any_run["run_id"], any_run.get("graph_id")) if any_run else (None, None)


@router.get("/incidents/{incident_id}")
async def get_incident(rt: RT, incident_id: str) -> dict[str, Any]:
    def _sync() -> dict[str, Any] | None:
        inc = rt.db.get_incident(incident_id)
        if inc is None:
            return None
        corr = inc.get("correlation_id")
        evs = rt.db.list_events(correlation_id=corr, limit=500, order="asc") if corr else []
        seen = {e["id"] for e in evs}
        for e in rt.db.list_events(q=incident_id, limit=500, order="asc"):  # events that only mention the incident id
            if e["id"] not in seen and (e.get("payload") or {}).get("incident_id") == incident_id:
                evs.append(e)
        evs.sort(key=lambda e: e["seq"])
        by_event = rt.db.dispatches_for_events([e["id"] for e in evs])
        disp, _ = rt.db.list_dispatches(incident_id=incident_id, limit=200)
        approvals = rt.db.list_approvals(incident_id=incident_id, limit=200)
        return {
            "incident": inc,
            "events": [decorate_event(e, by_event.get(e["id"], [])) for e in evs],
            "dispatches": disp,
            "approvals": approvals,
        }

    data = await rt.db.run(_sync)
    if data is None:
        raise ApiError(404, "not_found", f"incident {incident_id!r} not found")
    inc = data["incident"]
    pending = sum(1 for a in data["approvals"] if a["status"] == "pending")
    threads: dict[str, dict[str, Any]] = {}
    for d in data["dispatches"]:
        if d.get("thread_id"):
            threads[d["thread_id"]] = {
                "thread_id": d["thread_id"],
                "graph_id": d.get("graph_id"),
                "run_id": d.get("run_id"),
            }
    if inc.get("thread_id") and inc["thread_id"] not in threads:
        threads[inc["thread_id"]] = {"thread_id": inc["thread_id"], "graph_id": inc.get("graph_id"), "run_id": None}
    run_id, run_graph = await _latest_run(rt, inc, data["dispatches"])
    out = _incident_view(rt, inc, pending)
    out.update(
        {
            "events": data["events"],
            "dispatches": [{k: v for k, v in d.items() if k != "plan"} for d in data["dispatches"]],
            "approvals": data["approvals"],
            "threads": list(threads.values()),
            "run_id": run_id,
            "graph_id": run_graph or inc.get("graph_id"),
            "links": rt.studio_links(inc.get("thread_id")),
        }
    )
    return out


# --------------------------------------------------------------------------------------------------- approvals
@router.get("/approvals")
async def list_approvals(
    rt: RT,
    status: Literal["pending", "decided", "all"] = "pending",
    incident_id: str | None = None,
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    scan_ok, scanned = True, 0
    if status == "pending":
        res = await rt.approvals.pending()
        items = [
            a
            for a in res.items
            if (not incident_id or a.get("incident_id") == incident_id) and (not kind or a.get("kind") == kind)
        ]
        scan_ok, scanned = res.ok, res.scanned_threads
        items = items[:limit]
    else:
        statuses = ("decided",) if status == "decided" else ()
        items = await rt.db.run(
            rt.db.list_approvals, statuses=statuses, incident_id=incident_id, kind=kind, limit=limit
        )
    return {"items": items, "scanned_threads": scanned, "scan_ok": scan_ok}


class ResumeBody(BaseModel):
    interrupt_id: str | None = None
    response: dict[str, Any]
    nonce: str | None = Field(None, max_length=200)
    decided_by: str | None = Field(None, max_length=100)


class BatchBody(BaseModel):
    thread_id: str
    responses: dict[str, dict[str, Any]]
    nonce: str | None = Field(None, max_length=200)
    decided_by: str | None = Field(None, max_length=100)


@router.post("/approvals/resume-batch", status_code=202)
async def resume_batch(rt: RT, body: BatchBody) -> dict[str, Any]:
    return await rt.approvals.resume_batch(body.thread_id, body.responses, nonce=body.nonce, decided_by=body.decided_by)


@router.get("/approvals/{approval_id}")
async def get_approval(rt: RT, approval_id: str) -> dict[str, Any]:
    row = await rt.approvals.get(approval_id)
    if row is None:
        raise ApiError(404, "not_found", f"approval {approval_id!r} not found")
    return row


@router.post("/approvals/{approval_id}/resume")
async def resume_approval(rt: RT, approval_id: str, body: ResumeBody) -> JSONResponse:
    out = await rt.approvals.resume(
        approval_id,
        interrupt_id=body.interrupt_id,
        response=body.response,
        nonce=body.nonce,
        decided_by=body.decided_by,
    )
    return JSONResponse(out, status_code=200 if out["status"] == "duplicate" else 202)


# ------------------------------------------------------------------------------------------------------- evals
class EvalBody(BaseModel):
    suite: Literal["core16", "sweep60", "custom"] = "core16"
    scenario_ids: list[str] = Field(default_factory=list)
    profile: Literal["competent", "naive", "cautious", "gullible"] = "competent"
    concurrency: int | None = Field(None, ge=1, le=8)
    baseline_id: str | None = None
    max_scenarios: int | None = Field(None, ge=1)
    label: str | None = Field(None, max_length=100)


@router.post("/evals", status_code=202)
async def start_eval(rt: RT, body: EvalBody) -> dict[str, Any]:
    if body.suite == "custom" and not body.scenario_ids:
        raise ApiError(422, "schema_invalid", "suite=custom needs scenario_ids")
    row = await rt.db.run(
        rt.db.create_eval_run,
        created_at=rt.now_iso(),
        suite=body.suite,
        profile=body.profile,
        label=body.label,
        scenario_ids=body.scenario_ids,
        concurrency=body.concurrency,
        baseline_id=body.baseline_id,
    )
    payload = {
        "eval_run_id": row["eval_run_id"],
        "suite": body.suite,
        "profile": body.profile,
        "scenario_ids": body.scenario_ids,
        "baseline_id": body.baseline_id,
        "label": body.label,
    }
    if body.concurrency:
        payload["concurrency"] = body.concurrency
    if body.max_scenarios:
        payload["max_scenarios"] = body.max_scenarios
    res = await rt.bus.ingest(
        {
            "type": "eval.requested",
            "source": "api",
            "severity": "info",
            "payload": payload,
            "idempotency_key": f"eval.requested:{row['eval_run_id']}",
        },
        origin="internal",
    )
    thread_id = next((d["thread_id"] for d in res.dispatches if d.get("thread_id")), None)
    await rt.db.run(rt.db.update_eval_run, row["eval_run_id"], event_id=res.event_id, thread_id=thread_id)
    fresh = await rt.db.run(rt.db.get_eval_run, row["eval_run_id"])
    return {"eval_run": fresh, "event": res.body()}


@router.get("/evals")
async def list_evals(rt: RT, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, Any]:
    return {"items": await rt.db.run(rt.db.list_eval_runs, limit)}


@router.get("/evals/{eval_run_id}")
async def get_eval(rt: RT, eval_run_id: str) -> dict[str, Any]:
    row = await rt.db.run(rt.db.get_eval_run, eval_run_id)
    if row is None:
        raise ApiError(404, "not_found", f"eval run {eval_run_id!r} not found")
    return row


# ------------------------------------------------------------------------------------------------------ memory
def _ns(text: str | None) -> list[str]:
    return [p for p in (text or "").split(".") if p]


async def _store(call: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await call()
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(503, "not_ready", f"store unavailable: {exc!r}"[:300]) from exc


@router.get("/memory/namespaces")
async def memory_namespaces(
    rt: RT, prefix: str | None = None, max_depth: Annotated[int | None, Query(ge=1, le=6)] = None
) -> dict[str, Any]:
    res = await _store(
        lambda: rt.client.store.list_namespaces(prefix=_ns(prefix) or None, max_depth=max_depth, limit=200)
    )
    return {"items": res.get("namespaces", []) if isinstance(res, dict) else list(res)}


@router.get("/memory/search")
async def memory_search(
    rt: RT, q: str | None = None, namespace: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 10
) -> dict[str, Any]:
    res = await _store(lambda: rt.client.store.search_items(_ns(namespace), query=q, limit=limit))
    items = res.get("items", []) if isinstance(res, dict) else list(res)
    return {"items": items, "query": q}


@router.get("/memory/item")
async def memory_item(rt: RT, namespace: str, key: str) -> dict[str, Any]:
    try:
        item = await rt.client.store.get_item(_ns(namespace), key)
    except Exception as exc:
        raise ApiError(404, "not_found", f"memory item not found: {exc!r}"[:200]) from exc
    if not item:
        raise ApiError(404, "not_found", "memory item not found")
    out: dict[str, Any] = dict(item)
    return out


# ---------------------------------------------------------------------------------------------------- datasets
@router.get("/datasets")
async def datasets(rt: RT) -> dict[str, Any]:
    return {"items": await call_data(rt.data.datasets_index), "available": True}


@router.get("/datasets/{name}")
async def dataset(
    rt: RT,
    name: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    reveal_truth: bool = False,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = await call_data(
        rt.data.read_dataset, name, offset, limit, reveal_truth=reveal_truth, scenario_id=scenario_id
    )
    return out


__all__ = ["asyncio", "router"]
