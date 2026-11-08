"""Deterministic triage rules: explainable, testable, no I/O.

``decide(event, facts)`` is a pure function.  ``facts`` has the same shape as the reference triage context in
``datasets/evals/triage_cases.jsonl`` (``open_incidents``, ``maintenance_windows``, ``recent_alert_keys``) so the 400
labelled cases can be fed straight in.  The first matching rule wins and the full trace of rules that were
considered is returned (``trace``), which the graph streams as ``decision`` events.

Rule order (first match wins)            decision              route
 1 maintenance window covers component   suppress_maintenance   ignore
 2 same dedupe_key seen < 300 s ago      dedupe                 ignore   (result status ``duplicate``)
 3 severity == info                      ignore_info            ignore
 4 synthetic probe, uncorroborated       suppress_flapping      ignore   (benign flapping of external probes)
 5 open incident covers the component    attach                 join_incident
   or a call-graph neighbour
 6 customer.report that nothing explains escalate_human         open_incident (SEV3, needs a human)
 7 severity == low                       ignore_info            ignore
 8 otherwise                             open_incident          open_incident (severity from the matrix below)
Storm override: a real alert that is the 8th distinct alert within 60 s -> route ``escalate_storm``; further real
alerts while that storm is open are folded into it (``storm_member``) unless they attach to an existing incident.

Severity matrix (event loudness x component tier; ``tier0 service`` = tier 0 and kind service)
 event severity | tier-0 service | other tier 0 | tier 1 | tier 2
 critical       | SEV1           | SEV2         | SEV2   | SEV2
 high           | SEV2           | SEV2         | SEV3   | SEV3
 medium / other | SEV3           | SEV3         | SEV3   | SEV4
"""

from __future__ import annotations

from typing import Any

from foghorn.data.util import to_epoch
from foghorn.flows.event_router import topology

DEDUPE_WINDOW_S = 300
CORROBORATION_WINDOW_S = 600
INCIDENT_JOIN_WINDOW_S = 3600
STORM_COUNT = 8
STORM_WITHIN_S = 60
STORM_COOLDOWN_S = 120

TRIAGED_TYPES = frozenset({"alert.fired", "customer.report", "slo.burn_rate", "healthcheck.failed"})
STORM_SIGNAL_TYPES = frozenset({"alert.fired", "slo.burn_rate", "healthcheck.failed"})
CHANGE_TYPES = frozenset(
    {"deploy.started", "deploy.finished", "deploy.rolled_back", "config.changed", "flag.changed", "migration.applied"}
)
MAINTENANCE_TYPES = frozenset({"maintenance.window.started", "maintenance.window.ended"})
#: the 14 alert rules the data layer fires (a unit test guards this list against drift)
KNOWN_RULES = frozenset(
    {
        "error_rate_high",
        "error_rate_critical",
        "p95_latency_high",
        "p95_latency_degraded",
        "cpu_saturated",
        "memory_high",
        "disk_usage_high",
        "pool_saturation_high",
        "traffic_drop",
        "cache_hit_ratio_low",
        "consumer_lag_high",
        "synthetic_probe_failed",
        "healthcheck_failed",
        "slo_burn_rate_fast",
    }
)
SEV_PRIORITY = {"SEV1": "P0", "SEV2": "P1", "SEV3": "P2", "SEV4": "P3"}
PRIORITY_BY_SEVERITY = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3", "info": "P3"}
SEV_ORDER = {"SEV1": 1, "SEV2": 2, "SEV3": 3, "SEV4": 4}
SEV_TO_EVENT_SEVERITY = {"SEV1": "critical", "SEV2": "high", "SEV3": "medium", "SEV4": "low"}

Route = str  # open_incident | join_incident | ignore | escalate_storm
DECISION_ROUTE: dict[str, Route] = {
    "open_incident": "open_incident",
    "escalate_human": "open_incident",
    "attach": "join_incident",
    "dedupe": "ignore",
    "suppress_maintenance": "ignore",
    "suppress_flapping": "ignore",
    "ignore_info": "ignore",
}


def severity_for(event_severity: str, component: str | None) -> str:
    """The documented severity matrix."""
    tier = topology.tier_of(component)
    tier0_service = tier == 0 and topology.kind_of(component) == "service"
    if event_severity == "critical":
        return "SEV1" if tier0_service else "SEV2"
    if event_severity == "high":
        return "SEV2" if tier == 0 else "SEV3"
    return "SEV3" if (tier is not None and tier <= 1) else "SEV4"


def more_severe(a: str, b: str) -> bool:
    """True when incident severity ``a`` is strictly more severe than ``b``."""
    return SEV_ORDER.get(a, 9) < SEV_ORDER.get(b, 9)


def is_ambiguous(event: dict[str, Any]) -> bool:
    """Signals the rule table cannot score on its own (unknown component, or an alert rule it has never seen)."""
    if event.get("type") not in TRIAGED_TYPES:
        return False
    comp = (event.get("subject") or {}).get("component")
    if not topology.known(comp):
        return True
    rule = (event.get("payload") or {}).get("rule")
    return event.get("type") != "customer.report" and rule is not None and rule not in KNOWN_RULES


def noise_probability(decision: str, event: dict[str, Any], *, corroborated: bool = False) -> float:
    """Heuristic probability (0..1) that the event is noise, documented so it is explainable and testable."""
    sev = event.get("severity", "info")
    base = {"critical": 0.04, "high": 0.12, "medium": 0.3, "low": 0.75, "info": 0.93}.get(sev, 0.5)
    fixed = {
        "dedupe": 0.99,
        "suppress_maintenance": 0.97,
        "suppress_flapping": 0.93,
        "ignore_info": max(base, 0.85),
        "attach": min(base, 0.1),
        "escalate_human": 0.3,
    }
    p = fixed.get(decision, base)
    if decision == "open_incident" and corroborated:
        p *= 0.5
    return round(max(0.01, min(0.99, p)), 3)


def _out(
    decision: str,
    severity: str,
    reason: str,
    *,
    attach_to: str | None = None,
    dedupe_of: str | None = None,
    event: dict[str, Any],
    trace: list[str],
) -> dict[str, Any]:
    prio = SEV_PRIORITY.get(severity) or PRIORITY_BY_SEVERITY.get(event.get("severity", "info"), "P3")
    return {
        "decision": decision,
        "route": DECISION_ROUTE[decision],
        "severity": severity,
        "reason_code": reason,
        "attach_to": attach_to,
        "dedupe_of": dedupe_of,
        "priority": prio,
        "trace": trace,
    }


def decide(event: dict[str, Any], facts: dict[str, Any], *, llm: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply the ordered rules.  ``llm`` is the fallback verdict (only consulted for ambiguous events at rule 8)."""
    comp = (event.get("subject") or {}).get("component")
    sev = event.get("severity", "info")
    ts = to_epoch(event["occurred_at"])
    key = event.get("dedupe_key")
    rule = (event.get("payload") or {}).get("rule")
    trace: list[str] = []

    def out(decision: str, severity: str, reason: str, **kw: Any) -> dict[str, Any]:
        return _out(decision, severity, reason, event=event, trace=trace, **kw)

    for w in facts.get("maintenance_windows", []):
        if w["component"] == comp and to_epoch(w["start"]) <= ts <= to_epoch(w["end"]):
            trace.append(f"1 maintenance window {w['start']}..{w['end']} covers {comp}")
            return out("suppress_maintenance", "none", "in_maintenance_window")
    trace.append("1 no maintenance window covers the component")
    for rk in facts.get("recent_alert_keys", []):
        if rk["key"] == key and rk["age_s"] < DEDUPE_WINDOW_S:
            trace.append(f"2 dedupe_key seen {rk['age_s']}s ago (< {DEDUPE_WINDOW_S}s)")
            return out("dedupe", "none", "duplicate_within_window", dedupe_of=rk.get("event_id"))
    trace.append("2 dedupe_key not seen in the window")
    if sev == "info":
        trace.append("3 severity is info")
        return out("ignore_info", "none", "info_severity")
    trace.append("3 severity is above info")
    if rule == "synthetic_probe_failed":
        corroborated = any(
            rk["key"] != key and rk.get("component") == comp and rk["age_s"] < CORROBORATION_WINDOW_S
            for rk in facts.get("recent_alert_keys", [])
        )
        if not corroborated:
            trace.append("4 synthetic probe failed with no corroborating alert on the component in 600s")
            return out("suppress_flapping", "none", "uncorroborated_probe_failure")
    trace.append("4 not an uncorroborated synthetic probe")
    near = topology.neighbours(comp)
    for inc in facts.get("open_incidents", []):
        if near & set(inc["components"]):
            trace.append(f"5 open incident {inc['id']} covers {comp} or a call-graph neighbour")
            return out(
                "attach",
                inc.get("severity", "SEV3"),
                "open_incident_covers_component",
                attach_to=inc["id"],
            )
    trace.append("5 no open incident covers the component")
    if event["type"] == "customer.report":
        trace.append("6 customer report that nothing explains")
        return out("escalate_human", "SEV3", "customer_report_unexplained")
    trace.append("6 not a customer report")
    if sev == "low":
        trace.append("7 severity is low")
        return out("ignore_info", "none", "low_severity_noise")
    trace.append("7 severity is above low")
    if llm is not None:
        verdict = str(llm.get("decision", "ignore"))
        trace.append(f"8 ambiguous signal: language-model fallback says {verdict}")
        if verdict == "open_incident":
            return out("open_incident", str(llm.get("severity") or "SEV3"), "llm_fallback")
        if verdict == "escalate_human":
            return out("escalate_human", str(llm.get("severity") or "SEV3"), "llm_fallback")
        return out("ignore_info", "none", "llm_fallback_noise")
    severity = severity_for(sev, comp)
    tier = topology.tier_of(comp)
    reason = "tier0_error_budget_burn" if tier == 0 else "alert_requires_response"
    trace.append(f"8 open an incident: {sev} on tier-{tier} component -> {severity}")
    return out("open_incident", severity, reason)
