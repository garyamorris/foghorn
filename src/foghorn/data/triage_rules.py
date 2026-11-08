"""The reference triage policy: the ground truth behind D8 ``triage_cases`` and the stream label sidecars.

Pure and deterministic: ``reference_triage(event, ctx) -> expected``.  A deterministic router graph that implements
exactly these rules scores 1.0 on D8; it is also importable by the router/triage owner as the specification.

Decision enum: ``open_incident | attach | dedupe | suppress_maintenance | suppress_flapping | escalate_human |
ignore_info``.  Rules are evaluated in this order (first match wins):

1. maintenance window covers the component at ``occurred_at``            -> suppress_maintenance
2. same ``dedupe_key`` seen < 300 s ago                                  -> dedupe
3. severity == info                                                      -> ignore_info
4. ``synthetic_probe_failed`` without a corroborating alert on the component in the last 600 s -> suppress_flapping
5. an open incident covers the component or one call-graph neighbour     -> attach
6. ``customer.report`` that nothing explains                             -> escalate_human (SEV3)
7. severity == low                                                       -> ignore_info
8. otherwise                                                             -> open_incident, severity by (event severity, tier):
   critical -> SEV1 (tier 0 service) else SEV2;  high -> SEV2 (tier 0) else SEV3;  medium -> SEV3 (tier <= 1) else SEV4
"""

from __future__ import annotations

from typing import Any

from foghorn.data import catalog
from foghorn.data.util import to_epoch

DEDUPE_WINDOW_S = 300
CORROBORATION_WINDOW_S = 600
TRIAGED_TYPES = frozenset({"alert.fired", "customer.report", "slo.burn_rate", "healthcheck.failed"})
PRIORITY_BY_SEVERITY = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3", "info": "P3"}
SEV_PRIORITY = {"SEV1": "P0", "SEV2": "P1", "SEV3": "P2", "SEV4": "P3"}


def neighbours(component: str | None) -> set[str]:
    if not component or component not in catalog.COMPONENTS:
        return set()
    return {component, *catalog.callers_of(component), *catalog.callees_of(component)}


def _severity_for(event_sev: str, component: str | None) -> str:
    c = catalog.COMPONENTS.get(component or "")
    tier = c.tier if c else 2
    tier0_service = bool(c and c.tier == 0 and c.kind == "service")
    if event_sev == "critical":
        return "SEV1" if tier0_service else "SEV2"
    if event_sev == "high":
        return "SEV2" if tier == 0 else "SEV3"
    return "SEV3" if tier <= 1 else "SEV4"


def reference_triage(event: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    comp = (event.get("subject") or {}).get("component")
    sev = event.get("severity", "info")
    ts = to_epoch(event["occurred_at"])
    key = event.get("dedupe_key")
    rule = (event.get("payload") or {}).get("rule")

    def out(
        decision: str, severity: str, reason: str, attach_to: str | None = None, dedupe_of: str | None = None
    ) -> dict[str, Any]:
        prio = SEV_PRIORITY.get(severity) or PRIORITY_BY_SEVERITY.get(sev, "P3")
        return {
            "decision": decision,
            "severity": severity,
            "reason_code": reason,
            "attach_to": attach_to,
            "dedupe_of": dedupe_of,
            "priority": prio,
        }

    for w in ctx.get("maintenance_windows", []):
        if w["component"] == comp and to_epoch(w["start"]) <= ts <= to_epoch(w["end"]):
            return out("suppress_maintenance", "none", "in_maintenance_window")
    for rk in ctx.get("recent_alert_keys", []):
        if rk["key"] == key and rk["age_s"] < DEDUPE_WINDOW_S:
            return out("dedupe", "none", "duplicate_within_window", dedupe_of=rk.get("event_id"))
    if sev == "info":
        return out("ignore_info", "none", "info_severity")
    if rule == "synthetic_probe_failed":
        corroborated = any(
            rk["key"] != key and rk.get("component") == comp and rk["age_s"] < CORROBORATION_WINDOW_S
            for rk in ctx.get("recent_alert_keys", [])
        )
        if not corroborated:
            return out("suppress_flapping", "none", "uncorroborated_probe_failure")
    near = neighbours(comp)
    for inc in ctx.get("open_incidents", []):
        if near & set(inc["components"]):
            return out("attach", inc.get("severity", "SEV3"), "open_incident_covers_component", attach_to=inc["id"])
    if event["type"] == "customer.report":
        return out("escalate_human", "SEV3", "customer_report_unexplained")
    if sev == "low":
        return out("ignore_info", "none", "low_severity_noise")
    severity = _severity_for(sev, comp)
    reason = (
        "tier0_error_budget_burn"
        if (comp in catalog.COMPONENTS and catalog.COMPONENTS[comp].tier == 0)
        else "alert_requires_response"
    )
    return out("open_incident", severity, reason)


def expected_route(event: dict[str, Any]) -> str:
    """Which part of the pipeline an event belongs to (coarse; independent of the decision)."""
    t = event["type"]
    if t in TRIAGED_TYPES:
        return "triage"
    if t == "alert.resolved":
        return "resolution"
    if t.startswith(("deploy.", "config.", "flag.", "migration.")):
        return "ledger"
    if t.startswith("maintenance."):
        return "maintenance"
    if t == "system.cron.tick":
        return "handoff" if (event.get("payload") or {}).get("job") == "shift_handoff" else "none"
    return "none"
