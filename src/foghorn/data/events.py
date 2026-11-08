"""Serialise a world's changes, alerts and customer reports into ``foghorn.event/1`` envelopes (state dicts).

The dict shape is exactly ``EventEnvelope.to_state()`` (a test round-trips every generated event through the model).
"""

from __future__ import annotations

from typing import Any

from foghorn.data import catalog
from foghorn.data.util import to_iso

_TYPE_ORDER = {
    "maintenance.window.started": 0,
    "deploy.started": 1,
    "deploy.finished": 2,
    "flag.changed": 2,
    "config.changed": 2,
    "migration.applied": 2,
    "customer.report": 3,
    "alert.fired": 4,
    "healthcheck.failed": 4,
    "slo.burn_rate": 4,
    "alert.resolved": 5,
    "maintenance.window.ended": 6,
}


def envelope(
    *,
    id: str | None,
    type: str,
    source: str,
    severity: str,
    occurred_at: str,
    component: str | None,
    payload: dict[str, Any],
    dedupe_key: str | None,
    scenario_id: str | None = None,
    stream: str | None = None,
    seq: int | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    idempotency_key: str | None = None,
    attributes: dict[str, Any] | None = None,
    environment: str = "prod",
) -> dict[str, Any]:
    return {
        "schema_version": "foghorn.event/1",
        "id": id,
        "type": type,
        "source": source,
        "severity": severity,
        "occurred_at": occurred_at,
        "received_at": None,
        "subject": {"component": component, "environment": environment},
        "payload": payload,
        "dedupe_key": dedupe_key,
        "idempotency_key": idempotency_key,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "trace": {"stream": stream, "seq": seq, "scenario_id": scenario_id},
        "attributes": attributes or {},
    }


def alert_payload(a: dict[str, Any]) -> dict[str, Any]:
    c = catalog.COMPONENTS[a["component"]]
    return {
        "alert_id": a["alert_id"],
        "rule": a["rule"],
        "metric": a["metric"],
        "value": a["value"],
        "threshold": a["threshold"],
        "for_s": a["for_s"],
        "labels": {"team": c.team, "tier": str(c.tier), "kind": c.kind},
    }


def change_events(ch: dict[str, Any], t0: int, t_end: int) -> list[tuple[int, str, dict[str, Any], str, str, str]]:
    """-> [(ts, type, payload, source, severity, component)] for one change record."""
    ts, kind, comp, d = ch["ts"], ch["kind"], ch["component"], ch["details"]
    base = {"change_id": ch["change_id"], "actor": ch["actor"], "summary": ch["summary"]}
    out: list[tuple[int, str, dict[str, Any], str, str, str]] = []
    if kind == "deploy":
        out.append(
            (
                ts - 240,
                "deploy.started",
                {**base, "version": d.get("version"), "previous": d.get("previous")},
                "deploybot",
                "info",
                comp,
            )
        )
        out.append(
            (
                ts,
                "deploy.finished",
                {**base, "version": d.get("version"), "previous": d.get("previous"), "status": "succeeded"},
                "deploybot",
                "info",
                comp,
            )
        )
    elif kind == "flag_change":
        out.append(
            (
                ts,
                "flag.changed",
                {**base, **{k: v for k, v in d.items() if k in ("flag", "from", "to")}},
                "feature-flags",
                "info",
                comp,
            )
        )
    elif kind == "config_change":
        out.append(
            (
                ts,
                "config.changed",
                {**base, **{k: v for k, v in d.items() if k in ("key", "from", "to", "flag")}},
                "config-service",
                "info",
                comp,
            )
        )
    elif kind == "migration":
        out.append((ts, "migration.applied", {**base, **d}, "migrator", "info", comp))
    elif kind == "cron_job":
        out.append((ts, "config.changed", {**base, "kind": "cron_job", **d}, "cron", "info", comp))
    elif kind == "cert_rotation":
        out.append(
            (
                ts,
                "config.changed",
                {**base, "kind": "cert_rotation", **d},
                "cert-rotator",
                "low" if d.get("status") == "failed" else "info",
                comp,
            )
        )
    elif kind == "maintenance":
        start = ts
        end = start + int((d.get("end_min", 0) - d.get("start_min", 0)) * 60)
        out.append(
            (
                start,
                "maintenance.window.started",
                {**base, "starts_at": to_iso(start), "ends_at": to_iso(end), "reason": d.get("reason")},
                "calendar",
                "info",
                comp,
            )
        )
        if end < t_end:
            out.append(
                (
                    end,
                    "maintenance.window.ended",
                    {**base, "starts_at": to_iso(start), "ends_at": to_iso(end)},
                    "calendar",
                    "info",
                    comp,
                )
            )
    return [(t, ty, pl, src, sv, cm) for (t, ty, pl, src, sv, cm) in out if t0 <= t < t_end]


def build_events(
    sid: str,
    t0: int,
    t_end: int,
    changes: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw: list[tuple[int, int, str, dict[str, Any], str, str, str, str]] = []
    for ch in changes:
        for ts, ty, pl, src, sv, cm in change_events(ch, t0, t_end):
            raw.append((ts, _TYPE_ORDER[ty], ty, pl, src, sv, cm, f"{ty.split('.')[0]}:{cm}:{ch['change_id']}"))
    for a in alerts:
        ts = a["fired_at_s"]
        ty = a["event_type"]
        raw.append(
            (
                ts,
                _TYPE_ORDER[ty],
                ty,
                alert_payload(a),
                "alertmanager",
                a["severity"],
                a["component"],
                f"alert:{a['component']}:{a['rule']}",
            )
        )
        if a["resolved_at_s"] is not None and a["resolved_at_s"] < t_end:
            raw.append(
                (
                    a["resolved_at_s"],
                    5,
                    "alert.resolved",
                    {**alert_payload(a), "resolved_at": to_iso(a["resolved_at_s"])},
                    "alertmanager",
                    "info",
                    a["component"],
                    f"alert:{a['component']}:{a['rule']}",
                )
            )
    for rp in reports:
        raw.append(
            (
                rp["ts"],
                3,
                "customer.report",
                {k: rp[k] for k in ("ticket_id", "channel", "text", "customer_tier", "order_ref")},
                "support-desk",
                rp["severity"],
                rp.get("component") or "",
                f"report:{rp['ticket_id']}",
            )
        )
    raw.sort(key=lambda x: (x[0], x[1], x[2], x[6], x[7]))
    events: list[dict[str, Any]] = []
    for n, (ts, _o, ty, pl, src, sv, cm, dk) in enumerate(raw, 1):
        events.append(
            envelope(
                id=f"evt_{sid}_{n:04d}",
                type=ty,
                source=src,
                severity=sv,
                occurred_at=to_iso(ts),
                component=cm or None,
                payload=pl,
                dedupe_key=dk,
                scenario_id=sid,
                seq=n,
            )
        )
    return events
