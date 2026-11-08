"""Pure functions of the handoff: parse threads, compute KPIs, reconcile sources. No I/O, no clock, no randomness."""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from foghorn.contracts.meta import M_INCIDENT_ID, M_SCENARIO_ID
from foghorn.data.util import to_epoch

SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")
HIGH_ALERT = ("high", "critical")


def _last_action_time(report: dict[str, Any]) -> str | None:
    times = [str(a["t_virtual"]) for a in report.get("actions_executed") or [] if a.get("t_virtual")]
    return max(times, key=to_epoch) if times else None


def incident_from_thread(t: dict[str, Any]) -> dict[str, Any] | None:
    """A commander thread -> a flat incident row. Tolerant: a missing key never raises, an unidentifiable thread is None."""
    md = t.get("metadata") or {}
    vals = t.get("values") or {}
    inc = vals.get("incident") or {}
    report = vals.get("report") or {}
    iid = md.get(M_INCIDENT_ID) or inc.get("id") or report.get("incident_id")
    if not iid:
        return None
    root = report.get("root_cause") or {}
    first_alert = next(
        (a for a in vals.get("alerts") or [] if isinstance(a, dict)), {}
    )  # CommanderInput.alerts (envelopes)
    component = root.get("component") or vals.get("component") or (first_alert.get("subject") or {}).get("component")
    if inc.get("status"):
        status = inc["status"]
    elif report:
        status = "resolved" if report.get("recovered") else "mitigating"
    else:
        status = "open"
    return {
        "incident_id": str(iid),
        "severity": str(inc.get("severity") or report.get("severity") or "SEV3"),
        "status": str(status),
        "title": str(inc.get("title") or ""),
        "component": component,
        "category": root.get("category"),
        "scenario_id": md.get(M_SCENARIO_ID) or vals.get("scenario_id") or report.get("scenario_id"),
        "thread_id": str(t.get("thread_id") or ""),
        "run_status": str(t.get("status") or "idle"),
        "opened_at": vals.get("opened_at"),
        "resolved_at": vals.get("resolved_at") or (_last_action_time(report) if status == "resolved" else None),
        "abstained": bool(report.get("abstained")),
    }


def approval_from_thread(t: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per pending interrupt of an interrupted thread (payloads are ``build_interrupt`` dicts)."""
    md = t.get("metadata") or {}
    out: list[dict[str, Any]] = []
    raw = t.get("interrupts") or {}
    flat: list[dict[str, Any]] = (
        [i for items in raw.values() for i in (items or [])]
        if isinstance(raw, dict)
        else [i for i in raw if isinstance(i, dict)]
    )
    for i in flat:
        v = i.get("value") if isinstance(i, dict) else None
        if not isinstance(v, dict):
            continue
        ctx = v.get("context") or {}
        out.append(
            {
                "approval_id": str(v.get("approval_id") or i.get("id") or ""),
                "kind": str(v.get("kind") or "approval"),
                "title": str(v.get("title") or ""),
                "risk": str(v.get("risk") or "medium"),
                "incident_id": ctx.get("incident_id") or md.get(M_INCIDENT_ID),
                "thread_id": str(t.get("thread_id") or ""),
                "graph": md.get("foghorn_graph"),
            }
        )
    return sorted(out, key=lambda a: (a["approval_id"], a["thread_id"]))


def in_window(inc: dict[str, Any], start: str, end: str) -> bool:
    """Open incidents always belong to the handoff; a resolved one only if it was resolved during the shift (or the
    resolution time is unknown, in which case it is shown rather than silently dropped)."""
    if inc["status"] != "resolved":
        return True
    t = inc.get("resolved_at")
    return True if not t else to_epoch(start) < to_epoch(str(t)) <= to_epoch(end)


def minutes_between(a: str, b: str) -> float:
    return round((to_epoch(b) - to_epoch(a)) / 60.0, 1)


def compute_metrics(
    incidents: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    shift_end: str,
    window_hours: int,
) -> dict[str, float]:
    open_ = [i for i in incidents if i["status"] != "resolved"]
    done = [i for i in incidents if i["status"] == "resolved"]
    m: dict[str, float] = {
        "incidents_total": float(len(incidents)),
        "incidents_open": float(len(open_)),
        "incidents_resolved": float(len(done)),
        "runs_failed": float(sum(1 for i in incidents if i["run_status"] == "error")),
        "approvals_pending": float(len(approvals)),
        "approvals_high_risk": float(sum(1 for a in approvals if a["risk"] in ("high", "critical"))),
        "alerts_total": float(len(alerts)),
        "alerts_high": float(sum(1 for a in alerts if a["severity"] in HIGH_ALERT)),
        "alert_components": float(len({a["component"] for a in alerts})),
        "alerts_per_hour": round(len(alerts) / max(window_hours, 1), 2),
    }
    by_sev = Counter(i["severity"] for i in open_)
    for sev in SEVERITIES:
        m[f"open_{sev.lower()}"] = float(by_sev.get(sev, 0))
    ages = [minutes_between(i["opened_at"], shift_end) for i in open_ if i.get("opened_at")]
    m["oldest_open_min"] = max(ages) if ages else 0.0
    mttr = [
        minutes_between(i["opened_at"], i["resolved_at"])
        for i in done
        if i.get("opened_at") and i.get("resolved_at") and to_epoch(i["resolved_at"]) >= to_epoch(i["opened_at"])
    ]
    m["mttr_min_mean"] = round(statistics.fmean(mttr), 1) if mttr else 0.0
    m["mttr_min_max"] = max(mttr) if mttr else 0.0
    return m


def reconcile(
    incidents: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    shift_end: str,
    stale_sev1_minutes: int,
) -> list[dict[str, Any]]:
    """Cross-check the three sources. Each finding: ``{id, severity: info|warn|alert, text, refs}``."""
    out: list[dict[str, Any]] = []
    by_id = {i["incident_id"]: i for i in incidents}
    for a in approvals:
        inc = by_id.get(str(a["incident_id"]))
        if inc is not None and inc["status"] == "resolved":
            out.append(
                {
                    "id": f"stale_approval:{a['approval_id']}",
                    "severity": "warn",
                    "text": f"{a['kind']} approval {a['approval_id']} is still pending but {inc['incident_id']} is resolved",
                    "refs": [a["thread_id"]],
                }
            )
    for i in incidents:
        if i["run_status"] == "error":
            out.append(
                {
                    "id": f"run_failed:{i['incident_id']}",
                    "severity": "alert",
                    "text": f"the commander run for {i['incident_id']} failed: resume it from its last checkpoint",
                    "refs": [i["thread_id"]],
                }
            )
        if i["status"] != "resolved" and i["severity"] == "SEV1" and i.get("opened_at"):
            age = minutes_between(i["opened_at"], shift_end)
            if age > stale_sev1_minutes:
                out.append(
                    {
                        "id": f"sev1_open_long:{i['incident_id']}",
                        "severity": "alert",
                        "text": f"SEV1 {i['incident_id']} has been open for {age:g} minutes",
                        "refs": [i["thread_id"]],
                    }
                )
    tracked = {i["component"] for i in incidents if i.get("component")}
    loud = Counter(a["component"] for a in alerts if a["severity"] in HIGH_ALERT)
    for comp, n in sorted(loud.items()):
        if comp not in tracked and n >= 2:
            out.append(
                {
                    "id": f"uncovered_alerts:{comp}",
                    "severity": "info",
                    "text": f"{n} high-severity alerts on {comp} have no tracked incident",
                    "refs": [comp],
                }
            )
    if not incidents and not approvals and not alerts:
        out.append(
            {
                "id": "quiet_shift",
                "severity": "info",
                "text": "no incidents, approvals or alerts were found in the window",
                "refs": [],
            }
        )
    return out
