"""Enrichment branches (topology / oncall / recent changes) over the pure tool API.

Each branch builds a ``ToolContext`` from the event's ``trace.scenario_id`` and its ``occurred_at`` (the time fence), so
nothing after the event can leak in.  Without a scenario (ambient stream events) the topology falls back to the static
service catalog and the other branches report ``available: false``.  Only transient infrastructure errors escape (they
are retried by the node's ``RetryPolicy``); a scenario the data layer does not know degrades quietly.
"""

from __future__ import annotations

from typing import Any

from foghorn.data.access import ScenarioNotFound
from foghorn.data.util import to_epoch
from foghorn.flows.event_router import topology
from foghorn.models.offline import stable_int
from foghorn.tools import api as tools_api
from foghorn.tools.context import ToolContext, ToolError

KINDS = ("topology", "oncall", "changes")
BUCKET_S = 300  # enrichment results are cached per 5 virtual minutes
CHANGE_WINDOW_MIN = 240


def tool_context(scenario_id: str | None, as_of: str | None) -> ToolContext | None:
    if not scenario_id:
        return None
    return ToolContext(scenario_id=scenario_id, as_of=as_of)


def cache_key(arg: dict[str, Any]) -> str:
    """Cache key for an enrichment branch.  The node cache is GLOBAL across threads, so the scenario is part of it."""
    # defensive: graph drawing (Studio, get_graph) calls key_func with the node's STATE view, not a Send payload
    as_of = arg.get("as_of")
    bucket = to_epoch(as_of) // BUCKET_S if isinstance(as_of, str) and as_of else 0
    return f"enrich|{arg.get('kind')}|{arg.get('scenario_id')}|{arg.get('component')}|{bucket}"


def chaos_fails(kind: str, event_id: str, attempt: int, chaos: str | None, attr_chaos: str | None) -> bool:
    """Deterministic, seeded transient-failure injection: only the FIRST attempt fails, so a retry always heals it."""
    if attempt > 1:
        return False
    if attr_chaos == "fail_once":
        return kind == "changes"
    return bool(chaos) and stable_int("enrich-chaos", event_id, kind) % 3 == 0


def _topology(ctx: ToolContext | None, component: str | None) -> dict[str, Any]:
    if not component or not topology.known(component):
        return {"available": False, "source": "none", "reason": "unknown component"}
    cat = topology.catalog()[component]
    base = {
        "available": True,
        "component": component,
        "tier": cat["tier"],
        "kind": cat["kind"],
        "team": cat["team"],
        "criticality": cat["criticality"],
        "zones": list(cat.get("zones", [])),
    }
    if ctx is None:
        blast = topology.static_blast_radius(component)
        return {
            **base,
            "source": "catalog",
            "callers": list(topology.callers_of(component)),
            "callees": list(topology.callees_of(component)),
            "blast": _blast(blast),
        }
    try:
        topo = tools_api.get_topology(ctx, component)
        blast = tools_api.blast_radius(ctx, component)
    except (ToolError, ScenarioNotFound) as exc:
        return {"available": False, "source": "none", "reason": str(exc)}
    return {
        **base,
        "source": "tools",
        "callers": [d["id"] for d in topo["depended_on_by"]],
        "callees": [d["id"] for d in topo["depends_on"]],
        "runbooks": list(topo.get("runbooks", []))[:3],
        "blast": _blast(blast),
    }


def _blast(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_affected": int(b.get("total_affected_if_down", 0)),
        "revenue_path_exposed": bool(b.get("revenue_path_exposed")),
        "customer_facing_exposed": bool(b.get("customer_facing_exposed")),
        "tier0_exposed": list(b.get("tier0_exposed", [])),
    }


def _person(p: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"id": p["id"], "name": p["name"]} if p else None


def _oncall(ctx: ToolContext | None, component: str | None) -> dict[str, Any]:
    if ctx is None or not component or not topology.known(component):
        return {"available": False, "reason": "no scenario telemetry"}
    try:
        o = tools_api.get_oncall(ctx, component)
    except (ToolError, ScenarioNotFound) as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "team": o["team"],
        "primary": _person(o.get("primary")),
        "secondary": _person(o.get("secondary")),
        "incident_commander": _person(o.get("incident_commander")),
        "dba": _person(o.get("dba")),
    }


def _changes(ctx: ToolContext | None, component: str | None) -> dict[str, Any]:
    if ctx is None:
        return {"available": False, "reason": "no scenario telemetry", "recent": [], "maintenance": []}
    try:
        res = tools_api.list_changes(ctx, window_minutes=CHANGE_WINDOW_MIN)
    except (ToolError, ScenarioNotFound) as exc:
        return {"available": False, "reason": str(exc), "recent": [], "maintenance": []}
    near = topology.neighbours(component)
    maintenance, recent = [], []
    for c in res["changes"]:
        if c["kind"] == "maintenance":
            win = c.get("maintenance_window") or {}
            if c["component"] == component:
                maintenance.append(
                    {
                        "change_id": c["change_id"],
                        "component": c["component"],
                        "start": c["ts"],
                        "end": win.get("ends_at"),
                        "active": bool(win.get("active")),
                        "suppress_alerts": bool((c.get("details") or {}).get("suppress_alerts", True)),
                    }
                )
            continue
        recent.append(
            {
                "change_id": c["change_id"],
                "ts": c["ts"],
                "kind": c["kind"],
                "component": c["component"],
                "summary": c["summary"],
                "minutes_before": c["minutes_before_as_of"],
                "on_component": c["component"] == component,
                "near_component": c["component"] in near,
            }
        )
    recent.sort(key=lambda c: (not c["on_component"], not c["near_component"], c["minutes_before"]))
    return {
        "available": True,
        "window_minutes": CHANGE_WINDOW_MIN,
        "count": res["count"],
        "recent": recent[:6],
        "maintenance": maintenance,
    }


def run_branch(kind: str, scenario_id: str | None, as_of: str | None, component: str | None) -> dict[str, Any]:
    ctx = tool_context(scenario_id, as_of)
    if kind == "topology":
        return _topology(ctx, component)
    if kind == "oncall":
        return _oncall(ctx, component)
    if kind == "changes":
        return _changes(ctx, component)
    raise ValueError(f"unknown enrichment branch {kind!r}")
