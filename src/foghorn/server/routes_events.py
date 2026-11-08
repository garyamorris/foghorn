"""Events API: ingest, list/get, trace, links, dispatches, presets/inject, webhooks and the SSE stream."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from foghorn.contracts.events import SEVERITY_ORDER
from foghorn.events.pipeline import IngestResult, decorate_event, dispatch_summary
from foghorn.events.service import EventsRuntime
from foghorn.events.sse import CHANNELS, EventFilter, stream_frames
from foghorn.events.vclock import parse_iso, to_iso
from foghorn.server.deps import ApiError, authorize_ingest, get_rt, require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
# webhook-style ingest routes authenticate themselves (HMAC or bearer) instead of the blanket dependency
ingest_router = APIRouter(prefix="/api")
RT = Annotated[EventsRuntime, Depends(get_rt)]


def csv(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def respond(res: IngestResult) -> JSONResponse:
    headers = {"Retry-After": str(res.retry_after)} if res.retry_after else None
    status = res.http_status
    return JSONResponse(res.body(), status_code=status, headers=headers)


# ----------------------------------------------------------------------------------------------------- ingest
async def _ingest_body(request: Request, rt: EventsRuntime, idempotency_key: str | None) -> IngestResult:
    body = await authorize_ingest(request, rt)
    return await rt.bus.ingest(
        body, origin="api", idempotency_key=idempotency_key, raw_body=body.decode("utf-8", "replace")
    )


@ingest_router.post("/events")
async def post_event(request: Request, rt: RT, idempotency_key: Annotated[str | None, Header()] = None) -> JSONResponse:
    return respond(await _ingest_body(request, rt, idempotency_key))


@ingest_router.post("/webhooks/generic")
async def webhook_generic(
    request: Request, rt: RT, idempotency_key: Annotated[str | None, Header()] = None
) -> JSONResponse:
    body = await authorize_ingest(request, rt)
    try:
        data = json.loads(body)
    except ValueError:
        return respond(
            await rt.bus.ingest(
                body, origin="api", idempotency_key=idempotency_key, raw_body=body.decode("utf-8", "replace")
            )
        )
    if isinstance(data, dict) and "schema_version" not in data and "type" in data:
        known = {
            "id",
            "type",
            "source",
            "severity",
            "occurred_at",
            "subject",
            "payload",
            "dedupe_key",
            "idempotency_key",
            "correlation_id",
            "causation_id",
            "trace",
            "attributes",
        }
        data = {
            **{k: v for k, v in data.items() if k in known},
            "payload": {**(data.get("payload") or {}), **{k: v for k, v in data.items() if k not in known}},
        }
        data.setdefault("source", "webhook")
    return respond(
        await rt.bus.ingest(
            data, origin="api", idempotency_key=idempotency_key, raw_body=body.decode("utf-8", "replace")
        )
    )


_AM_SEVERITY = {
    "page": "critical",
    "critical": "critical",
    "high": "high",
    "error": "high",
    "warning": "medium",
    "warn": "medium",
    "medium": "medium",
    "low": "low",
    "info": "info",
}


def alertmanager_envelopes(doc: Any) -> list[dict[str, Any]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("alerts"), list):
        raise ApiError(422, "schema_invalid", "expected an Alertmanager v4 webhook body with an `alerts` list")
    out: list[dict[str, Any]] = []
    for a in doc["alerts"]:
        if not isinstance(a, dict):
            raise ApiError(422, "schema_invalid", "every entry of `alerts` must be an object")
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        firing = str(a.get("status", doc.get("status", "firing"))).lower() != "resolved"
        name = str(labels.get("alertname") or "unknown_alert")
        comp = labels.get("service") or labels.get("component") or labels.get("job") or labels.get("instance")
        starts, ends = a.get("startsAt"), a.get("endsAt")
        stamp = starts if firing else (ends if ends and not str(ends).startswith("0001") else starts)
        try:
            occurred = to_iso(parse_iso(str(stamp))) if stamp and not str(stamp).startswith("0001") else None
        except ValueError:
            occurred = None
        status = "firing" if firing else "resolved"
        out.append(
            {
                "type": "alert.fired" if firing else "alert.resolved",
                "source": "alertmanager",
                "severity": _AM_SEVERITY.get(str(labels.get("severity", "")).lower(), "medium"),
                "occurred_at": occurred,
                "subject": {
                    "component": comp,
                    "environment": str(labels.get("environment") or labels.get("env") or "prod"),
                },
                "payload": {
                    "rule": name,
                    "summary": ann.get("summary") or ann.get("description"),
                    "labels": labels,
                    "annotations": ann,
                    "fingerprint": a.get("fingerprint"),
                    "generator_url": a.get("generatorURL"),
                    "group_key": doc.get("groupKey"),
                    "receiver": doc.get("receiver"),
                },
                "dedupe_key": f"alert:{comp}:{name}",
                "idempotency_key": f"am:{a.get('fingerprint') or name + ':' + str(comp)}:{starts}:{status}",
            }
        )
    return out


@ingest_router.post("/webhooks/alertmanager")
async def webhook_alertmanager(request: Request, rt: RT) -> JSONResponse:
    body = await authorize_ingest(request, rt)
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise ApiError(422, "schema_invalid", "body is not valid JSON") from exc
    envelopes = alertmanager_envelopes(doc)
    results: list[dict[str, Any]] = []
    counts = {"accepted": 0, "duplicates": 0, "suppressed": 0, "rejected": 0}
    for env in envelopes:
        res = await rt.bus.ingest(env, origin="api")
        if res.http_status == 429:
            payload = {"error": res.error, "received": len(envelopes), **counts, "results": results}
            return JSONResponse(payload, status_code=429, headers={"Retry-After": str(res.retry_after or 5)})
        results.append({"event_id": res.event_id, "status": res.status})
        if res.status == "duplicate":
            counts["duplicates"] += 1
        elif res.status == "suppressed":
            counts["suppressed"] += 1
        elif res.http_status >= 400:
            counts["rejected"] += 1
        else:
            counts["accepted"] += 1
    return JSONResponse({"received": len(envelopes), **counts, "results": results}, status_code=202)


@ingest_router.post("/webhooks/deploybot")
async def webhook_deploybot(
    request: Request, rt: RT, idempotency_key: Annotated[str | None, Header()] = None
) -> JSONResponse:
    body = await authorize_ingest(request, rt)
    try:
        doc = json.loads(body)
        etype = str(doc["event"])
        service = str(doc["service"])
        if not service:
            raise ValueError("empty service")
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(422, "schema_invalid", "expected a deploybot body with `event` and `service`") from exc
    if etype not in {"deploy.started", "deploy.finished", "deploy.rolled_back"}:
        raise ApiError(422, "schema_invalid", f"unsupported deploybot event {etype!r}")
    env = {
        "type": etype,
        "source": "deploybot",
        "severity": "medium" if etype == "deploy.rolled_back" else "info",
        "occurred_at": doc.get("at"),
        "subject": {"component": service, "environment": str(doc.get("environment") or "prod")},
        "payload": {
            k: doc.get(k) for k in ("version", "deploy_id", "status", "actor", "commit") if doc.get(k) is not None
        },
        "idempotency_key": idempotency_key or f"deploybot:{doc.get('deploy_id') or doc.get('version')}:{etype}",
    }
    return respond(await rt.bus.ingest(env, origin="api"))


# ------------------------------------------------------------------------------------------------------ presets
def preset_templates() -> dict[str, dict[str, Any]]:
    comp = "checkout-service"
    return {
        "fire_alert": {
            "label": "Fire alert",
            "envelope": {
                "type": "alert.fired",
                "source": "ui.inject",
                "severity": "high",
                "subject": {"component": comp},
                "payload": {
                    "rule": "error_rate_high",
                    "value": 0.071,
                    "threshold": 0.02,
                    "for_s": 180,
                    "summary": "checkout error rate 7.1%",
                },
            },
        },
        "resolve_alert": {
            "label": "Resolve alert",
            "envelope": {
                "type": "alert.resolved",
                "source": "ui.inject",
                "severity": "info",
                "subject": {"component": comp},
                "payload": {"rule": "error_rate_high"},
                "dedupe_key": f"alert:{comp}:error_rate_high",
            },
        },
        "deploy_finished": {
            "label": "Deploy finished",
            "envelope": {
                "type": "deploy.finished",
                "source": "deploybot",
                "severity": "info",
                "subject": {"component": comp},
                "payload": {"version": "reco-4412", "deploy_id": "dep-ui-1", "status": "success"},
            },
        },
        "deploy_rolled_back": {
            "label": "Deploy rolled back",
            "envelope": {
                "type": "deploy.rolled_back",
                "source": "deploybot",
                "severity": "medium",
                "subject": {"component": comp},
                "payload": {"version": "reco-4412", "deploy_id": "dep-ui-1"},
            },
        },
        "flag_changed": {
            "label": "Flag changed",
            "envelope": {
                "type": "flag.changed",
                "source": "feature-flags",
                "severity": "info",
                "subject": {"component": "pricing-service"},
                "payload": {"flag": "new_pricing_engine", "from": "10%", "to": "50%"},
            },
        },
        "maintenance_start": {
            "label": "Maintenance window starts",
            "envelope": {
                "type": "maintenance.window.started",
                "source": "ui.inject",
                "severity": "info",
                "subject": {"component": comp},
                "payload": {"reason": "planned database failover"},
            },
        },
        "maintenance_end": {
            "label": "Maintenance window ends",
            "envelope": {
                "type": "maintenance.window.ended",
                "source": "ui.inject",
                "severity": "info",
                "subject": {"component": comp},
                "payload": {},
            },
        },
        "customer_report": {
            "label": "Customer report",
            "envelope": {
                "type": "customer.report",
                "source": "ui.inject",
                "severity": "medium",
                "subject": {"component": comp},
                "payload": {"summary": "customers cannot check out", "rule": "customer_reports"},
            },
        },
        "slo_burn": {
            "label": "SLO burn",
            "envelope": {
                "type": "slo.burn_rate",
                "source": "ui.inject",
                "severity": "high",
                "subject": {"component": comp},
                "payload": {"rule": "checkout_availability_slo", "burn_rate": 14.4},
            },
        },
        "poison_alert": {
            "label": "Poison alert (always fails -> DLQ)",
            "envelope": {
                "type": "alert.fired",
                "source": "ui.inject",
                "severity": "medium",
                "subject": {"component": comp},
                "payload": {"rule": "poison_demo"},
                "attributes": {"chaos": "poison"},
            },
        },
    }


@router.get("/events/presets")
async def get_presets() -> dict[str, Any]:
    return {"items": [{"preset": k, **v} for k, v in preset_templates().items()]}


@router.post("/events/inject")
async def inject(
    request: Request, rt: RT, body: dict[str, Any], idempotency_key: Annotated[str | None, Header()] = None
) -> JSONResponse:
    templates = preset_templates()
    if body.get("preset"):
        if body["preset"] not in templates:
            raise ApiError(422, "schema_invalid", f"unknown preset {body['preset']!r}", {"presets": sorted(templates)})
        env = json.loads(json.dumps(templates[body["preset"]]["envelope"]))
    elif isinstance(body.get("envelope"), dict):
        env = dict(body["envelope"])
    else:
        raise ApiError(422, "schema_invalid", "give `preset` or `envelope`")
    env["source"] = "ui.inject"
    if body.get("component"):
        env.setdefault("subject", {})["component"] = body["component"]
        if isinstance(env.get("dedupe_key"), str):
            env["dedupe_key"] = f"alert:{body['component']}:{(env.get('payload') or {}).get('rule')}"
    if body.get("severity"):
        env["severity"] = body["severity"]
    if isinstance(body.get("payload"), dict):
        env["payload"] = {**(env.get("payload") or {}), **body["payload"]}
    for k in ("idempotency_key", "correlation_id"):
        if body.get(k):
            env[k] = body[k]
    return respond(
        await rt.bus.ingest(env, origin="api", idempotency_key=idempotency_key or env.get("idempotency_key"))
    )


# ------------------------------------------------------------------------------------------------------ reading
async def decorated(rt: EventsRuntime, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_event = await rt.db.run(rt.db.dispatches_for_events, [r["id"] for r in rows])
    return [decorate_event(r, by_event.get(r["id"], [])) for r in rows]


@router.get("/events")
async def list_events(
    rt: RT,
    type: str | None = None,
    component: str | None = None,
    severity: str | None = None,
    min_severity: str | None = None,
    status: str | None = None,
    source: str | None = None,
    scenario_id: str | None = None,
    correlation_id: str | None = None,
    q: str | None = None,
    since: int | None = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    order: str = "desc",
) -> dict[str, Any]:
    if order not in {"asc", "desc"}:
        raise ApiError(400, "bad_request", "order must be asc or desc")
    severities = csv(severity)
    if min_severity:
        if min_severity not in SEVERITY_ORDER:
            raise ApiError(400, "bad_request", f"unknown severity {min_severity!r}")
        severities = [
            s
            for s, r in SEVERITY_ORDER.items()
            if r >= SEVERITY_ORDER[min_severity] and (not severities or s in severities)
        ]
    rows = await rt.db.run(
        rt.db.list_events,
        types=csv(type),
        component=component,
        severities=severities,
        statuses=csv(status),
        source=source,
        scenario_id=scenario_id,
        correlation_id=correlation_id,
        q=q,
        since=since,
        before=before,
        limit=limit,
        order=order,
    )
    latest = await rt.db.run(rt.db.latest_seq)
    items = await decorated(rt, rows)
    return {
        "items": items,
        "latest_seq": latest,
        "next_before": min((r["seq"] for r in rows), default=None) if len(rows) >= limit else None,
    }


def _links(rt: EventsRuntime, dispatches: list[dict[str, Any]]) -> dict[str, Any]:
    with_thread = [d for d in dispatches if d.get("thread_id")]
    last = with_thread[-1] if with_thread else None
    studio = rt.studio_links(last["thread_id"] if last else None)
    return {
        "dispatches": [{k: v for k, v in d.items() if k != "plan"} for d in dispatches],
        "thread_id": last["thread_id"] if last else None,
        "run_id": last.get("run_id") if last else None,
        "incident_id": next((d["incident_id"] for d in reversed(dispatches) if d.get("incident_id")), None),
        "studio_base_url": studio["base_url"],
        "studio_thread_url": studio["thread_url"],
        "api_url": f"{rt.settings.public_base_url.rstrip('/')}/threads/{last['thread_id']}" if last else None,
    }


@router.get("/events/{event_id}")
async def get_event(rt: RT, event_id: str) -> dict[str, Any]:
    row = await rt.db.run(rt.db.get_event, event_id)
    if row is None:
        raise ApiError(404, "not_found", f"event {event_id!r} not found")
    dispatches = (await rt.db.run(rt.db.dispatches_for_events, [event_id]))[event_id]
    out = decorate_event(row, dispatches)
    out["links"] = {"event_id": event_id, **_links(rt, dispatches)}
    return out


@router.get("/links/event/{event_id}")
async def event_links(rt: RT, event_id: str) -> dict[str, Any]:
    row = await rt.db.run(rt.db.get_event, event_id)
    if row is None:
        raise ApiError(404, "not_found", f"event {event_id!r} not found")
    dispatches = (await rt.db.run(rt.db.dispatches_for_events, [event_id]))[event_id]
    return {"event_id": event_id, **_links(rt, dispatches)}


@router.get("/trace/{event_id}")
async def trace(rt: RT, event_id: str) -> dict[str, Any]:
    def _sync() -> dict[str, Any] | None:
        root_id, chain = rt.db.causal_chain(event_id)
        if not chain:
            return None
        ids = [e["id"] for e, _ in chain]
        by_event = rt.db.dispatches_for_events(ids)
        corr = next((e["correlation_id"] for e, _ in chain if e.get("correlation_id")), None)
        inc = None
        for e, _ in chain:
            iid = (e.get("payload") or {}).get("incident_id")
            if iid and (inc := rt.db.get_incident(iid)):
                break
        if inc is None and corr:
            row = rt.db.query_one("SELECT incident_id FROM incidents WHERE correlation_id = ?", (corr,))
            inc = rt.db.get_incident(row["incident_id"]) if row else None
        return {
            "root_id": root_id,
            "focus_id": event_id,
            "chain": [{**decorate_event(e, by_event.get(e["id"], [])), "depth": d} for e, d in chain],
            "edges": [{"from": e["causation_id"], "to": e["id"]} for e, _ in chain if e.get("causation_id") in ids],
            "dispatches": [{k: v for k, v in d.items() if k != "plan"} for i in ids for d in by_event.get(i, [])],
            "incident": inc,
        }

    out = await rt.db.run(_sync)
    if out is None:
        raise ApiError(404, "not_found", f"event {event_id!r} not found")
    return out


@router.get("/dispatches")
async def list_dispatches(
    rt: RT,
    status: str | None = None,
    event_id: str | None = None,
    graph_id: str | None = None,
    incident_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows, total = await rt.db.run(
        rt.db.list_dispatches,
        statuses=csv(status),
        event_id=event_id,
        graph_id=graph_id,
        incident_id=incident_id,
        limit=limit,
        offset=offset,
    )
    return {"items": [{k: v for k, v in r.items() if k != "plan"} for r in rows], "total": total}


# ---------------------------------------------------------------------------------------------------------- SSE
@router.get("/stream")
async def stream(
    request: Request,
    rt: RT,
    channels: str | None = None,
    since: int | None = None,
    types: str | None = None,
    min_severity: str | None = None,
    component: str | None = None,
    correlation_id: str | None = None,
    scenario_id: str | None = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    chans = frozenset(csv(channels)) or frozenset(CHANNELS)
    unknown = chans - set(CHANNELS)
    if unknown:
        raise ApiError(400, "bad_request", f"unknown channels {sorted(unknown)}", {"channels": list(CHANNELS)})
    if min_severity and min_severity not in SEVERITY_ORDER:
        raise ApiError(400, "bad_request", f"unknown severity {min_severity!r}")
    resume = since
    if last_event_id is not None and last_event_id.strip().lstrip("-").isdigit():
        resume = int(last_event_id)
    filt = EventFilter(tuple(csv(types)), min_severity, component, correlation_id, scenario_id)
    sub = rt.hub.subscribe(chans, filt)  # subscribe BEFORE reading the backlog: nothing can fall in the gap
    latest = await rt.db.run(rt.db.latest_seq)
    severities = [s for s, r in SEVERITY_ORDER.items() if min_severity and r >= SEVERITY_ORDER[min_severity]]

    async def backlog(after: int, limit: int) -> list[dict[str, Any]]:
        rows = await rt.db.run(
            rt.db.list_events,
            types=filt.types,
            component=component,
            severities=severities,
            correlation_id=correlation_id,
            scenario_id=scenario_id,
            since=after,
            limit=limit,
            order="asc",
        )
        return await decorated(rt, rows)

    hello = {"server_time": rt.now_iso(), "latest_seq": latest, "resumed_from": resume}
    gen = stream_frames(
        rt.hub,
        sub,
        hello=hello,
        backlog=backlog,
        since=resume,
        heartbeat_s=rt.es.sse_heartbeat_s,
        page=rt.es.sse_replay_page,
    )
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(_encode(gen), media_type="text/event-stream", headers=headers)


async def _encode(frames: Any) -> Any:
    try:
        async for frame in frames:
            yield frame.encode("utf-8")
    except asyncio.CancelledError:
        raise
    finally:
        await frames.aclose()


__all__ = ["dispatch_summary", "ingest_router", "router"]
