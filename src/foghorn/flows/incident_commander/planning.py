"""Remediation planning, severity assessment and the root-cause claim (pure functions, no I/O).

The planner turns the leading hypothesis into concrete ``action:target`` proposals using the runbook the knowledge agent
retrieved, the past incidents / lessons the memory recalled, the team preferences and the prompt directives. It never
reads hidden truth: targets come from the evidence (the flag or config key of the change that caused it, the callers of
a failing provider, the zone the partition logs name, ...).
"""

from __future__ import annotations

import json
import re
from typing import Any

from foghorn.data import actions as act
from foghorn.data import catalog
from foghorn.data.util import hex_id
from foghorn.flows.incident_commander.directives import Directives
from foghorn.flows.rca_tournament.facts import Facts
from foghorn.flows.rca_tournament.scoring import role_of

#: category -> ordered action ids (the first that can be resolved to a target and is allowed wins)
CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "bad_deploy": ("rollback_deploy", "restart_service", "scale_out"),
    "config_change": ("revert_flag", "revert_config", "scale_out"),
    "memory_leak": ("restart_service", "rollback_deploy"),
    "disk_full": ("rotate_logs", "expand_volume"),
    "cert_expiry": ("renew_cert", "restart_service"),
    "db_pool_exhaustion": ("kill_queries", "scale_out"),
    "runaway_query": ("kill_queries", "failover_db_primary"),
    "slow_query_migration": ("add_index",),
    "planned_maintenance": ("suppress_alert",),
    "cache_stampede": ("revert_config", "warm_cache"),
    "queue_poison_message": ("quarantine_message", "pause_consumer"),
    "dependency_outage": ("enable_fallback_provider",),
    "network_partition": ("drain_zone", "failover_dns"),
    "telemetry_gap": ("escalate_human",),
}
#: low-risk companions executed with the primary action (no approval of their own: POL-005 low risk)
PAIRED: dict[str, tuple[str, ...]] = {"memory_leak": ("open_ticket",), "cache_stampede": ("warm_cache",)}
#: the runbook that teaches each category
CATEGORY_RUNBOOK: dict[str, str] = {
    "bad_deploy": "RB-001",
    "config_change": "RB-002",
    "memory_leak": "RB-011",
    "disk_full": "RB-006",
    "cert_expiry": "RB-005",
    "db_pool_exhaustion": "RB-007",
    "runaway_query": "RB-008",
    "slow_query_migration": "RB-009",
    "planned_maintenance": "RB-018",
    "cache_stampede": "RB-010",
    "queue_poison_message": "RB-012",
    "dependency_outage": "RB-013",
    "network_partition": "RB-014",
    "telemetry_gap": "RB-015",
}
CATEGORY_QUERY: dict[str, str] = {
    "bad_deploy": "error_rate jumps right after a deploy new exception rollback",
    "config_change": "feature flag or configuration regression latency after ramp",
    "memory_leak": "memory leak OOM heap climbing slowly restart",
    "disk_full": "disk full no space left on device log volume",
    "cert_expiry": "TLS certificate expired handshake failure",
    "db_pool_exhaustion": "database connection pool saturation too many clients",
    "runaway_query": "long-running queries lock contention kill queries",
    "slow_query_migration": "latency creep after schema migration missing index",
    "planned_maintenance": "planned maintenance alert suppression window",
    "cache_stampede": "cache hit ratio collapse stampede",
    "queue_poison_message": "queue consumer lag poison message",
    "dependency_outage": "third-party provider outage fallback provider",
    "network_partition": "zone network partition no route to host",
    "telemetry_gap": "telemetry gap metrics flatlined missing",
}


def action_record(
    action: str,
    target: str,
    params: dict[str, Any] | None,
    rationale: str,
    *,
    source: str = "evidence",
    refs: list[str] | None = None,
    secondary: bool = False,
) -> dict[str, Any]:
    spec = act.ACTIONS.get(action)
    params = dict(params or {})
    return {
        "id": "pa_" + hex_id(action, target, json.dumps(params, sort_keys=True, default=str), n=8),
        "action": action,
        "target": target,
        "params": params,
        "risk": spec.risk if spec else "high",
        "reversible": spec.reversible if spec else False,
        "approvers": spec.approvers if spec else 2,
        "extra_roles": list(spec.extra_roles) if spec else [],
        "never_automatic": bool(spec and spec.never_automatic),
        "kind": spec.kind if spec else "unknown",
        "rationale": rationale,
        "source": source,
        "refs": refs or [],
        "secondary": secondary,
    }


def fingerprint(a: dict[str, Any]) -> str:
    return hex_id(
        a.get("action"), a.get("target"), json.dumps(a.get("params") or {}, sort_keys=True, default=str), n=12
    )


def label(a: dict[str, Any]) -> str:
    return f"{a['action']}:{a['target']}"


# --- targets ---------------------------------------------------------------------------------------------------------------
def _cause_change(f: Facts, component: str, kinds: tuple[str, ...]) -> dict[str, Any] | None:
    cands = f.changes_on(component, kinds)
    return cands[0] if cands else None  # most recent first


def _config_for_job(f: Facts, component: str, job: str) -> str | None:
    """A cron job changed the world: the config key that schedules it is the revert target (token overlap with its name)."""
    c = catalog.COMPONENTS.get(component)
    if c is None:
        return None
    toks = set(re.findall(r"[a-z]+", job.lower())) - {"job", "weekly", "daily", "nightly"}
    scored = sorted(
        ((len(toks & set(re.findall(r"[a-z]+", k.lower()))), k) for k in (*c.configs, *c.flags)), reverse=True
    )
    return scored[0][1] if scored and scored[0][0] > 0 else None


def zone_of(f: Facts) -> str | None:
    for fams in f.logs.values():
        fam = fams.get("partition")
        if fam and fam.get("zones"):
            return str(fam["zones"][0])
    return None


def resolve_target(
    f: Facts, hyp: dict[str, Any], action: str, memory_targets: dict[str, str]
) -> tuple[str, dict[str, Any], str, list[str]] | None:
    """(target, params, rationale, refs) for ``action`` under hypothesis ``hyp``; None if it cannot be aimed at anything."""
    refs = list(hyp.get("evidence", []))[:4]
    got = _resolve(f, hyp, action, refs)
    mem_t = memory_targets.get(action)
    if got is not None:  # the evidence decides the target; a matching past incident only corroborates it
        if mem_t == got[0]:
            return got[0], got[1], f"{got[2]} (same as the matching past incident)", got[3]
        return got
    if mem_t is not None:  # evidence cannot name a target: fall back on what worked last time
        return mem_t, {}, f"as in the matching past incident ({action}:{mem_t})", refs
    return None


def _resolve(
    f: Facts, hyp: dict[str, Any], action: str, refs: list[str]
) -> tuple[str, dict[str, Any], str, list[str]] | None:
    comp, cat = hyp["component"], hyp["category"]
    if action == "revert_flag":
        ch = _cause_change(f, comp, ("flag_change",))
        if not ch:
            return None
        flag = (ch.get("details") or {}).get("flag")
        return (
            (
                str(flag),
                {},
                f"flag {flag} was ramped {ch['details'].get('from')}->{ch['details'].get('to')}% at {ch['ts'][11:16]} ({ch['actor']})",
                [f"change:{ch['change_id']}", *refs],
            )
            if flag
            else None
        )
    if action == "revert_config":
        ch = _cause_change(f, comp, ("config_change",))
        if ch and (ch.get("details") or {}).get("key"):
            key = str(ch["details"]["key"])
            return (
                key,
                {},
                f"config {key} changed {ch['details'].get('from')}->{ch['details'].get('to')} at {ch['ts'][11:16]}",
                [f"change:{ch['change_id']}", *refs],
            )
        job = _cause_change(f, comp, ("cron_job",))
        if job:
            cfg_key = _config_for_job(f, comp, str((job.get("details") or {}).get("job", job["summary"])))
            if cfg_key:
                return (
                    cfg_key,
                    {},
                    f"job {job['details'].get('job')} ({job['change_id']}) is scheduled by {cfg_key}",
                    [f"change:{job['change_id']}", *refs],
                )
        return None
    if action == "add_index":
        mig = _cause_change(f, comp, ("migration",))
        m = re.search(r"FK (\w+)\.(\w+)", (mig or {}).get("summary", ""))
        if not mig or not m:
            return None
        table, col = m.group(1), m.group(2)
        sql = f"CREATE INDEX CONCURRENTLY idx_{table}_{col} ON {table} ({col})"
        return (
            comp,
            {"sql": sql},
            f"migration {mig['change_id']} added {table}.{col} without an index",
            [f"change:{mig['change_id']}", *refs],
        )
    if action == "enable_fallback_provider":
        callers = catalog.callers_of(comp, sync_only=True)
        return (
            (
                callers[0],
                {"provider": comp},
                f"{comp} is failing for every caller; switch {callers[0]} to the fallback provider",
                refs,
            )
            if callers
            else None
        )
    if action == "pause_consumer":
        stalled = list(catalog.callers_of(comp, sync_only=True))
        return (
            (stalled[0], {}, f"pause {stalled[0]} while the poison message is removed from {comp}", refs)
            if stalled
            else None
        )
    if action == "drain_zone":
        z = zone_of(f)
        return (z, {}, f"logs show {z} cannot reach the shared services", refs) if z else None
    if action == "failover_dns":
        return comp, {}, "fail DNS over to the healthy zone", refs
    if action in ("kill_queries", "failover_db_primary"):
        job = _cause_change(f, comp, ("cron_job",))
        why = (
            f"terminate the runaway queries from {job['details'].get('job')} ({job['change_id']})"
            if job
            else f"free the connections held on {comp}"
        )
        return comp, {}, why, refs
    if action == "warm_cache":
        return comp, {}, f"re-warm {comp} so the misses stop reaching the database", refs
    if action == "open_ticket":
        ch = _cause_change(f, comp, ("deploy",))
        return (
            comp,
            {"summary": f"follow-up: {cat} on {comp}" + (f" (suspect release {ch['change_id']})" if ch else "")},
            "a restart is only a stopgap",
            refs,
        )
    return comp, {}, f"{action.replace('_', ' ')} on {comp} for {cat.replace('_', ' ')}", refs


def memory_resolution(f: Facts, hyp: dict[str, Any]) -> dict[str, str]:
    """Actions (with explicit targets) from a past incident that matches the hypothesis category and component."""
    out: dict[str, str] = {}
    for inc in f.incidents:
        if inc.get("category") == hyp["category"] and inc.get("component") == hyp["component"]:
            for r in inc.get("resolution", []):
                a, t = act.parse_action(str(r))
                if t != "*" and act.known_target(t):
                    out.setdefault(a, t)
    for m in sorted(f.memory, key=lambda x: -(x.get("score") or 0.0)):
        d = m
        if d.get("category") == hyp["category"] and d.get("component") == hyp["component"]:
            for r in d.get("resolution", []):
                a, t = act.parse_action(str(r))
                if t != "*" and act.known_target(t):
                    out.setdefault(a, t)
    return out


def runbook_actions(f: Facts, category: str) -> list[str]:
    rb = CATEGORY_RUNBOOK.get(category)
    for r in f.runbooks:
        if r["runbook"] == rb:
            return [a["id"] for a in r["actions"] if not a.get("never_automatic")]
    return []


def propose_actions(
    f: Facts,
    hyp: dict[str, Any],
    directives: Directives,
    *,
    rejected: list[str] | None = None,
    include_secondary: bool = True,
    prefs: list[dict[str, Any]] | None = None,
    avoid: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Ordered proposals for ``hyp``: the primary action first, then low-risk companions, then alternatives.

    ``rejected`` holds ``action:target`` labels a human rejected or that failed verification: never proposed again."""
    cat = hyp["category"]
    skip = set(rejected or [])
    mem = memory_resolution(f, hyp)
    order = list(CATEGORY_ACTIONS.get(cat, ("escalate_human",)))
    rb = runbook_actions(f, cat)
    if rb:  # the retrieved runbook orders the options; the category defaults fill in what it does not list
        order = [a for a in rb if a in order or a in CATEGORY_ACTIONS.get(cat, ())] + [a for a in order if a not in rb]
    if mem:  # a matching past incident names what worked
        order = [a for a in order if a in mem] + [a for a in order if a not in mem]
    for pref in prefs or []:  # team preferences (Store, namespace prefs): "prefer X over Y" for a scope
        rule = pref.get("rule") or {}
        if rule.get("scope") not in (None, hyp["component"]):
            continue
        want, over = rule.get("prefer"), rule.get("over")
        if (
            want in order
            and (over is None or over in order)
            and (over is None or order.index(want) > order.index(over))
        ):
            order.remove(want)
            order.insert(order.index(over) if over in order else 0, want)
    order = [a for a in order if a not in avoid]
    if directives.policy == "prefer_flag_revert" and "revert_flag" in order:
        order.remove("revert_flag")
        order.insert(0, "revert_flag")
    elif directives.policy == "prefer_rollback" and "rollback_deploy" in order:
        order.remove("rollback_deploy")
        order.insert(0, "rollback_deploy")
    elif directives.policy == "mitigate_before_fix":
        cheap = [a for a in ("warm_cache", "scale_out") if a in order]
        order = cheap + [a for a in order if a not in cheap]
    props: list[dict[str, Any]] = []
    for a in order:
        spec = act.ACTIONS.get(a)
        if spec is None or spec.never_automatic:
            continue
        got = resolve_target(f, hyp, a, mem)
        if got is None:
            continue
        target, params, why, refs = got
        rec = action_record(a, target, params, why, refs=refs)
        if label(rec) in skip:
            continue
        props.append(rec)
    if props and include_secondary:
        primary = props[0]
        companions: list[dict[str, Any]] = []
        for comp_action in PAIRED.get(cat, ()):
            if comp_action == primary["action"]:
                continue
            existing = next((x for x in props if x["action"] == comp_action), None)
            if existing is not None:
                companions.append({**existing, "secondary": True})
                continue
            got = resolve_target(f, hyp, comp_action, mem)
            if got:
                t, p, why, refs = got
                rec = action_record(comp_action, t, p, why, refs=refs, secondary=True)
                if label(rec) not in skip:
                    companions.append(rec)
        taken = {x["id"] for x in companions}
        props = [primary, *companions, *[x for x in props[1:] if x["id"] not in taken]]
    return props


# --- severity ------------------------------------------------------------------------------------------------------------
def assess_severity(f: Facts, fallback: str, category: str | None) -> tuple[str, str]:
    """POL-001: a tier-0 component above 2% errors is SEV2, an unavailable revenue path SEV1, anything else SEV3
    (planned maintenance has no severity, reported as SEV4). Returns (severity, reason)."""
    if category == "planned_maintenance":
        return "SEV4", "planned maintenance has no business impact (POL-001)"
    if category == "telemetry_gap":
        return "SEV3", "no confirmed customer impact: telemetry is blind (POL-001, POL-006)"
    worst = 0.0
    worst_c = None
    for (c, m), d in f.metrics.items():
        comp = catalog.COMPONENTS.get(c)
        if m == "error_rate" and d["material"] and comp is not None and comp.tier == 0:
            peak = float(d.get("peak") or 0.0)
            if peak > worst:
                worst, worst_c = peak, c
    for a in f.alerts:  # an error-budget burn on a tier-0 component counts even if its metric was not queried
        comp = catalog.COMPONENTS.get(a["component"])
        if (
            a.get("metric") == "error_rate"
            and comp is not None
            and comp.tier == 0
            and float(a.get("value") or 0) > worst
        ):
            worst, worst_c = float(a["value"]), a["component"]
    checkout = f.metrics.get(("checkout-service", "error_rate"))
    if checkout and checkout["material"] and float(checkout.get("peak") or 0) >= 0.8:
        return (
            "SEV1",
            f"checkout-service fails {float(checkout['peak']):.0%} of requests: revenue path unavailable (POL-001)",
        )
    if worst_c and worst >= 0.02:
        return "SEV2", f"tier-0 {worst_c} error rate reached {worst:.1%} (POL-001)"
    if f.anomalies():
        return "SEV3", "degradation confined to non-revenue paths (POL-001)"
    return fallback, "no impact evidence yet"


def root_cause_claim(
    hyp: dict[str, Any] | None, abstained: bool, confidence: float, refs: list[str]
) -> dict[str, Any] | None:
    if hyp is None or abstained:
        return None
    return {
        "component": hyp["component"],
        "category": hyp["category"],
        "confidence": round(confidence, 3),
        "evidence_refs": refs,
    }


def role_label(component: str) -> str:
    return role_of(component)


_CATEGORY_WORDS = {
    "bad_deploy": "bad deploy: errors and a new exception right after a release",
    "config_change": "config change: a feature flag ramp changed latency and errors",
    "memory_leak": "memory leak: heap climbing, out of memory restarts",
    "disk_full": "disk full: no space left on device",
    "cert_expiry": "certificate expiry: TLS handshake failures",
    "db_pool_exhaustion": "database connection pool exhausted",
    "runaway_query": "runaway query: long-running unbounded queries",
    "slow_query_migration": "latency creep after a schema migration, missing index",
    "planned_maintenance": "planned maintenance window",
    "cache_stampede": "cache stampede: hit ratio collapse",
    "queue_poison_message": "poison message blocks the queue consumer",
    "dependency_outage": "third-party provider outage",
    "network_partition": "zone network partition",
    "telemetry_gap": "telemetry gap: metrics flat",
}


def diagnosis_query(f: Facts, hyp: dict[str, Any], herrings: list[dict[str, Any]]) -> str:
    """A natural-language description of the DIAGNOSED situation, used to recall similar past incidents and lessons."""
    change = next(iter(f.changes_on(hyp["component"])), None)
    parts = [f"{_CATEGORY_WORDS.get(hyp['category'], hyp['category'])} on {hyp['component']}."]
    if change:
        parts.append(f"{change['kind'].replace('_', ' ')}: {change['summary']}.")
    for h in herrings[:1]:
        parts.append(f"An unrelated change minutes before the onset ({h['component']}) was set aside as a red herring.")
    return " ".join(parts)
