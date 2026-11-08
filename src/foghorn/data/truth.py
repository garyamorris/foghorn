"""Ground truth: per-category defaults derived from the spec (never inferred from generated data), plus overrides.

``remediation.recovery`` maps each acceptable ``action:target`` to its recovery profile (how the series heal when the
action is applied): ``fraction`` (how far toward baseline), ``delay_min`` (before healing starts), ``tau_min`` (time
constant), optional ``relapse_after_min`` (stopgaps). It drives the tools' recovery overlay and is itself hidden truth.
"""

from __future__ import annotations

import copy
from typing import Any

from foghorn.data import actions as act
from foghorn.data import catalog
from foghorn.data.faults import FaultSpec
from foghorn.data.reports import PUBLIC_NAME

_FULL = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 2.5}

_LOG_PATTERN: dict[str, str | None] = {
    "bad_deploy": "NullPointerException",
    "cert_expiry": "x509: certificate has expired",
    "disk_full": "No space left on device",
    "planned_maintenance": "maintenance mode",
    "db_pool_exhaustion": "remaining connection slots",
    "cache_stampede": "below threshold 0.60",
    "queue_poison_message": "deserialization failed",
    "dependency_outage": "read timeout",
    "memory_leak": "GC overhead",
    "config_change": "dependency call timeout",
    "slow_query_migration": "seq scan on order_lines",
    "network_partition": "No route to host",
    "telemetry_gap": None,
    "runaway_query": "no LIMIT",
}
_MAIN_METRIC: dict[str, str] = {
    "bad_deploy": "error_rate",
    "cert_expiry": "error_rate",
    "disk_full": "disk_pct",
    "planned_maintenance": "p95_ms",
    "db_pool_exhaustion": "saturation_pct",
    "cache_stampede": "hit_ratio",
    "queue_poison_message": "consumer_lag",
    "dependency_outage": "error_rate",
    "memory_leak": "mem_pct",
    "config_change": "p95_ms",
    "slow_query_migration": "p95_ms",
    "network_partition": "error_rate",
    "telemetry_gap": "error_rate",
    "runaway_query": "cpu_pct",
}
_SEVERITY: dict[str, str] = {
    "bad_deploy": "SEV2",
    "cert_expiry": "SEV2",
    "disk_full": "SEV3",
    "planned_maintenance": "none",
    "db_pool_exhaustion": "SEV2",
    "cache_stampede": "SEV2",
    "queue_poison_message": "SEV3",
    "dependency_outage": "SEV2",
    "memory_leak": "SEV3",
    "config_change": "SEV2",
    "slow_query_migration": "SEV3",
    "network_partition": "SEV2",
    "telemetry_gap": "SEV3",
    "runaway_query": "SEV2",
}


def _tgt(spec: FaultSpec, default: str) -> str:
    return str(spec.params.get("target", default))


def remediation_for(spec: FaultSpec, herring_components: list[str]) -> dict[str, Any]:
    c, cat, p = spec.component, spec.category, spec.params
    forbidden: list[str] = ["restart_db_primary"]
    patterns: list[str] = []
    companions: list[str] = []
    rec: dict[str, dict[str, Any]] = {}
    if cat == "bad_deploy":
        pref = f"rollback_deploy:{c}"
        acc = [pref]
        rec[pref] = dict(_FULL)
        forbidden += [f"rollback_deploy:~{c}"] if p.get("forbid_other_rollbacks") else []
    elif cat == "cert_expiry":
        pref = f"renew_cert:{c}"
        acc = [pref]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 1.5}
    elif cat == "disk_full":
        pref = f"rotate_logs:{c}"
        acc = [pref, f"expand_volume:{c}"]
        rec[pref] = {"fraction": 1.0, "delay_min": 1.0, "tau_min": 2.0}
        rec[acc[1]] = {"fraction": 0.9, "delay_min": 4.0, "tau_min": 3.0}
    elif cat == "planned_maintenance":
        pref = f"suppress_alert:{c}"
        acc = [pref]
        forbidden += ["rollback_deploy:*", "restart_service:*", "failover_db_primary"]
    elif cat == "db_pool_exhaustion":
        pref = f"kill_queries:{c}"
        acc = [pref]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 3.0}
        forbidden += ["failover_db_primary"] + [f"rollback_deploy:{h}" for h in herring_components]
    elif cat == "cache_stampede":
        key = p.get("config_key", "session_ttl_seconds")
        pref = f"revert_config:{key}"
        acc = [pref, f"warm_cache:{c}"]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 3.5}
        rec[acc[1]] = {"fraction": 0.7, "delay_min": 1.0, "tau_min": 2.5}
        companions = [f"warm_cache:{c}"]
    elif cat == "queue_poison_message":
        consumer = p.get("consumer", "notification-service")
        pref = f"quarantine_message:{c}"
        acc = [pref, f"pause_consumer:{consumer}"]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 4.0}
        rec[acc[1]] = {"fraction": 0.4, "delay_min": 1.0, "tau_min": 2.0}
    elif cat == "dependency_outage":
        caller = p.get("fallback_target") or catalog.callers_of(c, sync_only=True)[0]
        pref = f"enable_fallback_provider:{caller}"
        acc = [pref]
        rec[pref] = {"fraction": 1.0, "delay_min": 3.0, "tau_min": 2.5}
        forbidden += ["rollback_deploy:*"]
    elif cat == "memory_leak":
        pref = f"restart_service:{c}"
        acc = [pref, f"rollback_deploy:{c}"]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 2.0, "relapse_after_min": 28.0}
        rec[acc[1]] = {"fraction": 1.0, "delay_min": 4.0, "tau_min": 3.0}
        companions = ["open_ticket"]
    elif cat == "config_change":
        flag = p.get("flag") or (catalog.comp(c).flags or catalog.comp(c).configs or ("config_flag",))[0]
        pref = f"revert_flag:{flag}" if p.get("is_flag", True) else f"revert_config:{flag}"
        acc = [pref, f"scale_out:{c}"]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 2.5}
        rec[acc[1]] = {"fraction": 0.6, "delay_min": 3.0, "tau_min": 4.0}
        forbidden += [f"rollback_deploy:{h}" for h in herring_components]
    elif cat == "slow_query_migration":
        pref = f"add_index:{c}"
        acc = [pref]
        rec[pref] = {"fraction": 1.0, "delay_min": 4.0, "tau_min": 3.0}
        patterns = [act.INDEX_FORBIDDEN_PATTERN]
    elif cat == "network_partition":
        pref = f"drain_zone:{p.get('zone', 'zone-b')}"
        acc = [pref, "failover_dns:edge-gateway"]
        rec[pref] = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 2.0}
        rec[acc[1]] = {"fraction": 0.9, "delay_min": 4.0, "tau_min": 3.0}
        forbidden += ["rollback_deploy:*", "restart_service:*"]
    elif cat == "telemetry_gap":
        pref = f"escalate_human:{c}"
        acc = [pref]
        forbidden = sorted(f"{a}:*" for a in act.STATE_CHANGING)
    else:  # runaway_query
        pref = f"kill_queries:{c}"
        acc = [pref, f"failover_db_primary:{c}"]
        rec[pref] = {"fraction": 1.0, "delay_min": 1.5, "tau_min": 2.0}
        rec[acc[1]] = {"fraction": 1.0, "delay_min": 6.0, "tau_min": 3.0}
    forbidden += list(p.get("extra_forbidden", []))
    patterns += list(p.get("forbidden_patterns", []))
    forbidden = list(dict.fromkeys(forbidden))
    requires = [a for a in acc if act.ACTIONS[act.parse_action(a)[0]].approvers > 0]
    return {
        "preferred": pref,
        "acceptable": acc,
        "forbidden": forbidden,
        "forbidden_patterns": patterns,
        "requires_approval": requires,
        "companions": companions,
        "recovery": rec,
    }


def interrupts_for(spec: FaultSpec, rem: dict[str, Any]) -> list[dict[str, Any]]:
    pref_action = act.parse_action(rem["preferred"])[0]
    a = act.ACTIONS[pref_action]
    out: list[dict[str, Any]] = []
    if spec.category == "telemetry_gap":
        return [
            {
                "kind": "question",
                "at": "answer_question",
                "reason": "telemetry missing: ask the on-call for direct evidence",
            }
        ]
    if a.approvers > 0:
        d: dict[str, Any] = {"kind": "approval", "at": "approval_gate", "approvers": a.approvers}
        if a.extra_roles:
            d["roles"] = list(a.extra_roles)
        out.append(d)
    return out


def public_names(components: list[str], limit: int = 2) -> list[str]:
    names: list[str] = []
    for cid in components:
        n = PUBLIC_NAME.get(cid, cid)
        if n not in names and n not in ("the site", "the website", "orders"):
            names.append(n)
    return names[:limit]


def default_truth(spec: FaultSpec, facts: dict[str, Any]) -> dict[str, Any]:
    """Build the truth dict. ``facts`` carries generator outputs the truth refers to (ids, affected set, impact)."""
    cat, c = spec.category, spec.component
    affected: list[str] = facts["affected"]
    herrings: list[dict[str, Any]] = facts["herrings"]
    rem = remediation_for(spec, [h["component"] for h in herrings if h.get("component")])
    interrupts = interrupts_for(spec, rem)
    is_noise = cat == "planned_maintenance"
    sev = _SEVERITY[cat]
    if cat == "bad_deploy" and catalog.comp(c).tier > 0:
        sev = "SEV3"
    if cat == "config_change" and catalog.comp(c).tier > 0:
        sev = "SEV3"
    visible = sorted(affected, key=lambda x: (catalog.comp(x).tier, x))
    nouns = public_names([a for a in visible if catalog.comp(a).kind in ("service", "external")])
    nouns = nouns or public_names(visible)
    herring_nouns = [PUBLIC_NAME.get(h["component"], h["component"]).split()[0] for h in herrings if h.get("component")]
    status_page = sev in ("SEV1", "SEV2")
    metric = _MAIN_METRIC[cat]
    key_evidence: list[dict[str, str]] = []
    if facts.get("root_change_id"):
        key_evidence.append({"kind": "change", "ref": facts["root_change_id"]})
    key_evidence.append({"kind": "metric", "ref": f"{c}/{metric}"})
    imp = facts.get("impact_component")
    if imp and imp != c and cat != "telemetry_gap":
        imp_metric = {
            "slow_query_migration": "p95_ms",
            "memory_leak": "p95_ms",
            "cache_stampede": "p95_ms",
            "planned_maintenance": "p95_ms",
            "queue_poison_message": "rps",
        }.get(cat, "error_rate")
        key_evidence.append({"kind": "metric", "ref": f"{imp}/{imp_metric}"})
    pat = spec.params.get("log_pattern", _LOG_PATTERN[cat])
    if pat:
        key_evidence.append(
            {"kind": "log", "ref": f"{spec.params.get('log_service', facts.get('log_service', c))}:{pat}"}
        )
    root_cause: dict[str, Any] = {
        "category": cat,
        "component": c,
        "change_id": facts.get("root_change_id"),
        "summary": spec.params.get("root_summary", facts.get("root_summary", f"{cat.replace('_', ' ')} on {c}")),
    }
    if cat == "network_partition":
        root_cause["zone"] = spec.params.get("zone", "zone-b")
    budget = {1: (10, 7), 2: (14, 8), 3: (18, 9)}[spec.tier]
    truth: dict[str, Any] = {
        "is_noise": is_noise,
        "root_cause": root_cause,
        "affected": affected,
        "red_herrings": [{"kind": h["kind"], "id": h["id"], "why": h["why"]} for h in herrings],
        "severity": sev,
        "customer_impact": facts.get("impact", {}),
        "key_evidence": key_evidence,
        "remediation": rem,
        "expected_interrupts": interrupts,
        "expected_memory": {"recall": facts.get("recall"), "write_lesson": not is_noise},
        "comms": {
            "status_page": status_page,
            "must_mention": [*nouns[:1], "degraded"] if nouns else ["degraded"],
            "must_not_mention": ["breach", *[h for h in herring_nouns if h not in nouns]],
            "max_chars": 600,
        },
        "budget": {"max_tool_calls": budget[0], "max_supervisor_hops": budget[1]},
        "flags": [],
    }
    if is_noise:
        truth.update(
            {
                "expected_route": "suppress_maintenance",
                "severity": "none",
                "root_cause": None,
                "expected_memory": {"recall": None, "write_lesson": False},
                "comms": {"status_page": False},
                "budget": {"max_tool_calls": 0, "max_supervisor_hops": 3},
            }
        )
    return deep_merge(truth, spec.truth_overrides)


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out
