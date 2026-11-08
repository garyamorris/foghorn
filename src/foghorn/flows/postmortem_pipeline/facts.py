"""Pure, synchronous construction of the facts bundle the drafting model works from.

Everything here comes from (a) the commander's ``CommanderReport`` and (b) the pure tools API, both fenced by the
incident's ``as_of`` (nothing after it is ever read). It never touches ground truth: if the commander was wrong, the
postmortem says what the commander concluded and the human reviewer is the safety net.

Blocking SQLite reads live in here, so callers run :func:`build_facts` with ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any

from foghorn.data import api as data_api
from foghorn.data.util import to_epoch
from foghorn.flows.postmortem_pipeline.knowledge import playbook_for
from foghorn.logging import get_logger
from foghorn.tools import api as tools
from foghorn.tools.context import ToolError, advance_as_of

log = get_logger("postmortem.facts")

LOOKBACK_MIN = 120  # how far before the first alert a change still counts as a precursor
ALERT_WINDOW_MIN = 240
MAX_TIMELINE = 14
OWNER_ROLES = ("primary", "secondary", "incident_commander", "dba")


def _later(a: str, b: str) -> str:
    return a if to_epoch(a) >= to_epoch(b) else b


def applied_actions(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Map the report's ``actions_executed`` onto the shape the recovery overlay expects (``applied_at`` + ``id``)."""
    out: list[dict[str, Any]] = []
    for i, a in enumerate((report or {}).get("actions_executed") or []):
        t = a.get("applied_at") or a.get("t_virtual")
        if not t or a.get("outcome", "executed") != "executed":
            continue
        out.append({**a, "applied_at": t, "id": a.get("id") or f"rep_{i}_{a.get('action')}_{a.get('target')}"})
    return out


def _oncall(ctx: dict[str, Any], component: str | None) -> dict[str, Any]:
    if not component:
        return {}
    try:
        raw = tools.get_oncall(ctx, component)
    except (ToolError, KeyError, ValueError):
        return {}
    people: dict[str, Any] = {}
    for role, key in (
        ("primary", "primary"),
        ("secondary", "secondary"),
        ("incident_commander", "incident_commander"),
        ("dba", "dba"),
    ):
        p = raw.get(key)
        if isinstance(p, dict) and p.get("id"):
            people[role] = {"id": p["id"], "name": p.get("name", p["id"]), "roles": p.get("roles", [])}
    return {"team": raw.get("team"), "rota": raw.get("rota"), "people": people}


def _neighbours(ctx: dict[str, Any], component: str | None) -> set[str]:
    if not component:
        return set()
    try:
        topo = tools.get_topology(ctx, component)
    except (ToolError, KeyError, ValueError):
        return {component}
    return {component, *(d["id"] for d in topo["depends_on"]), *(d["id"] for d in topo["depended_on_by"])}


def _impact(
    ctx: dict[str, Any], component: str | None, alerts: list[dict[str, Any]], recovered: bool | None, end_iso: str
) -> dict[str, Any]:
    primary = [a for a in alerts if a["component"] == component] or alerts
    worst: dict[str, Any] | None = None
    for a in primary:
        thr = a.get("threshold") or 0
        ratio = (a["value"] / thr) if thr else 0.0
        if worst is None or ratio > worst["ratio"]:
            worst = {
                "component": a["component"],
                "metric": a["metric"],
                "rule": a["rule"],
                "peak_value": a["value"],
                "threshold": thr,
                "ratio": round(ratio, 2),
            }
    first = min((to_epoch(a["fired_at"]) for a in alerts), default=None)
    resolved = [a["resolved_at"] for a in alerts if a["resolved_at"]]
    duration_min: float | None = None
    if first is not None and recovered:
        last = (
            max((to_epoch(r) for r in resolved), default=to_epoch(end_iso))
            if len(resolved) == len(alerts)
            else to_epoch(end_iso)
        )
        duration_min = round(max(last - first, 0) / 60.0, 1)
    blast: dict[str, Any] = {}
    if component:
        try:
            blast = tools.blast_radius(ctx, component)
        except (ToolError, KeyError, ValueError):
            blast = {}
    return {
        "worst_alert": worst,
        "alerts": len(alerts),
        "components": sorted({a["component"] for a in alerts}),
        "duration_min": duration_min,
        "recovered": bool(recovered),
        "customer_facing": bool(blast.get("customer_facing_exposed")),
        "revenue_path": bool(blast.get("revenue_path_exposed")),
        "affected_if_down": int(blast.get("total_affected_if_down", 0)),
    }


def _timeline(
    alerts: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    executed: list[dict[str, Any]],
    component: str | None,
    related: set[str],
    recovered: bool | None,
    end_iso: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    first_alert = min(alerts, key=lambda a: (to_epoch(a["fired_at"]), a["alert_id"]), default=None)
    for a in alerts:
        is_first = first_alert is not None and a["alert_id"] == first_alert["alert_id"]
        entries.append(
            {
                "t": a["fired_at"],
                "kind": "detection" if is_first else "alert",
                "summary": f"{a['severity']} alert {a['rule']} fired on {a['component']} ({a['metric']} {a['value']} vs threshold {a['threshold']})",
                "ref": f"alert:{a['alert_id']}",
                "key": is_first,
            }
        )
    t_first = to_epoch(first_alert["fired_at"]) if first_alert else None
    for c in changes:
        if c["component"] not in related:
            continue
        if t_first is not None and to_epoch(c["ts"]) < t_first - LOOKBACK_MIN * 60:
            continue  # too old to be a precursor of this incident
        entries.append(
            {
                "t": c["ts"],
                "kind": "change",
                "summary": f"{c['kind']} on {c['component']} by {c['actor']}: {c['summary']}",
                "ref": f"change:{c['change_id']}",
                "key": c["component"] == component,
            }
        )
    for a in executed:
        entries.append(
            {
                "t": a.get("applied_at") or a.get("t_virtual") or end_iso,
                "kind": "action",
                "summary": f"executed {a.get('action')} on {a.get('target')}",
                "ref": f"action:{a.get('action')}:{a.get('target')}",
                "key": True,
            }
        )
    if recovered:
        resolved = [a["resolved_at"] for a in alerts if a["resolved_at"]]
        t_rec = max(resolved, key=to_epoch) if resolved and len(resolved) == len(alerts) else end_iso
        entries.append(
            {
                "t": t_rec,
                "kind": "recovery",
                "summary": f"service recovered{' on ' + component if component else ''}",
                "ref": f"recovery:{component or 'service'}",
                "key": True,
            }
        )
    entries.sort(key=lambda e: (to_epoch(e["t"]), e["ref"]))
    if len(entries) > MAX_TIMELINE:
        keep = [e for e in entries if e["key"]]
        rest = [e for e in entries if not e["key"]][: max(MAX_TIMELINE - len(keep), 0)]
        entries = sorted(keep + rest, key=lambda e: (to_epoch(e["t"]), e["ref"]))
    return entries


def resolve_as_of(scenario_id: str, base: str | None, executed: list[dict[str, Any]]) -> str:
    """The time fence: the commander's own ``as_of`` (or the scenario default), moved 10 virtual minutes past the last executed
    action so the recovery is visible, and never beyond the end of the telemetry window."""
    as_of = base or data_api.default_as_of(scenario_id)
    times = [str(a["applied_at"]) for a in executed if a.get("applied_at")]
    if times:
        last = max(times, key=to_epoch)
        as_of = _later(as_of, advance_as_of({"scenario_id": scenario_id, "as_of": last}, 10))
    return as_of


def build_facts(incident: dict[str, Any]) -> dict[str, Any]:
    """``incident`` = the ``load_incident`` task result. Returns the JSON facts bundle (see module docstring)."""
    report: dict[str, Any] | None = incident.get("report")
    sid = str(incident.get("scenario_id") or "")
    root = (report or {}).get("root_cause")
    executed = applied_actions(report)
    recovered = (report or {}).get("recovered")
    facts: dict[str, Any] = {
        "incident": {
            "id": incident["incident_id"],
            "severity": incident.get("severity") or (report or {}).get("severity") or "SEV3",
            "title": incident.get("title") or "",
            "scenario_id": sid or None,
        },
        "report_available": report is not None,
        "report_source": incident.get("source", "none"),
        "abstained": bool((report or {}).get("abstained")),
        "root_cause": root,
        "flags": list((report or {}).get("flags") or []),
        "actions_executed": [
            {"action": a.get("action"), "target": a.get("target"), "t": a["applied_at"]} for a in executed
        ],
        "actions_proposed": [
            {"action": a.get("action"), "target": a.get("target"), "risk": a.get("risk")}
            for a in (report or {}).get("actions_proposed") or []
        ],
        "approvals": [
            {"id": a.get("id"), "kind": a.get("kind"), "status": a.get("status"), "decided_by": a.get("decided_by")}
            for a in (report or {}).get("approvals") or []
        ],
        "recovered": recovered,
        "memory_recalled": list((report or {}).get("memory_recalled") or []),
        "feedback": list(incident.get("feedback") or []),
        "component": (root or {}).get("component"),
        "category": (root or {}).get("category"),
        "window": {},
        "timeline": [],
        "impact": {},
        "oncall": {},
        "known_owner_ids": [],
        "playbook": {},
        "tool_errors": [],
    }
    if not sid:
        facts["tool_errors"].append("no scenario_id: timeline limited to the commander report")
    else:
        try:
            as_of = resolve_as_of(sid, incident.get("as_of"), executed)
            ctx = {"scenario_id": sid, "as_of": as_of, "applied_actions": executed}
            alerts = tools.list_alerts(ctx, window_minutes=ALERT_WINDOW_MIN)["alerts"]
            component = facts["component"] or (
                min(alerts, key=lambda a: to_epoch(a["fired_at"]))["component"] if alerts else None
            )
            facts["component"] = component
            related = _neighbours(ctx, component)
            # alerts on unrelated services (flapping probes, other teams' noise) are not part of THIS incident
            rel_alerts = [a for a in alerts if a["component"] in related] if component else alerts
            facts["unrelated_alerts"] = len(alerts) - len(rel_alerts)
            alerts = rel_alerts or alerts
            changes = tools.list_changes(ctx, window_minutes=ALERT_WINDOW_MIN)["changes"]
            start = min((a["fired_at"] for a in alerts), key=to_epoch, default=as_of)
            facts["window"] = {"start": start, "end": as_of}
            facts["timeline"] = _timeline(alerts, changes, executed, component, related, recovered, as_of)
            facts["impact"] = _impact(ctx, component, alerts, recovered, as_of)
            facts["oncall"] = _oncall(ctx, component)
            facts["changes_considered"] = len(changes)
        except Exception as exc:
            log.warning("facts_tools_failed", incident=incident["incident_id"], error=repr(exc)[:200])
            facts["tool_errors"].append(f"tools unavailable: {type(exc).__name__}")
    if not facts["window"]:
        facts["window"] = {"start": "", "end": ""}
        facts["timeline"] = [
            {
                "t": a["applied_at"],
                "kind": "action",
                "summary": f"executed {a.get('action')} on {a.get('target')}",
                "ref": f"action:{a.get('action')}:{a.get('target')}",
                "key": True,
            }
            for a in executed
        ]
    people = (facts["oncall"] or {}).get("people", {})
    facts["known_owner_ids"] = sorted({p["id"] for p in people.values()})
    pb = playbook_for(facts["category"])
    comp = facts["component"] or "the affected service"
    facts["playbook"] = {
        "category": facts["category"],
        "label": pb["label"].format(component=comp),
        "factors": list(pb["factors"]),
        "follow_ups": [{**f, "title": f["title"].format(component=comp)} for f in pb["follow_ups"]],
        "lessons": list(pb["lessons"]),
    }
    return facts
