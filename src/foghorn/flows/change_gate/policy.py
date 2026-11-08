"""Risk assessment and policy gate: deterministic, pure, driven by the data layer's policy documents.

Risk score (0-100, higher = riskier), every term is listed in ``risk.factors`` so the reviewer sees why:

  kind        flag 10 | scale 15 | deploy 20 | config 20 | migration 35
  component   tier 0 +20 | tier 1 +8 | datastore +10 | unknown component +10
  environment prod +10 (the default)
  blast       +2 per component that (transitively) calls it, capped at 12
  freeze      +10 inside the change freeze (POL-003)
  rollback    +10 for deploy/config/migration on tier 0-1 without a rollback plan; -10 for a canary rollout
  tiers       low < 35 <= medium < 55 <= high < 75 <= critical;  effective tier = max(computed, declared)

Approver matrix (POL-005, read from the policy document): low 0, medium 1 (service_oncall), high 2
(service_oncall + incident_commander), critical = four-eyes (two reviewers decide in parallel; a datastore change swaps the
incident commander for a dba).  ``strict`` adds one approver level.  POL-003: inside the freeze a tier-0 datastore
migration/scale needs break-glass (a reason of >= 20 characters); rollbacks are always allowed.  POL-006: a component that
cannot be observed is blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from foghorn.contracts.interrupts import PolicyCheck
from foghorn.data import api as data_api
from foghorn.data.util import to_epoch
from foghorn.flows.event_router import topology

KIND_BASE = {"flag": 10, "scale": 15, "deploy": 20, "config": 20, "migration": 35}
TIERS = ("low", "medium", "high", "critical")
THRESHOLDS = (35, 55, 75)  # lower bounds of medium, high, critical
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_REQUESTED_AT = "2026-11-05T03:00:00Z"  # a Thursday, 03:00 UTC: outside the peak-trading freeze
PLAN_KINDS = ("deploy", "config", "migration")


@lru_cache(maxsize=1)
def policies() -> dict[str, dict[str, Any]]:
    """The policy documents by id (POL-001..POL-008) from the public dataset API."""
    return {str(p["id"]): p for p in data_api.read_dataset("policies", 0, 50)["records"]}


def tier_for(score: float) -> str:
    return TIERS[sum(score >= t for t in THRESHOLDS)]


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def has_rollback_plan(change: dict[str, Any]) -> bool:
    diff = change.get("diff") or {}
    return bool(diff.get("rollback_plan") or diff.get("rollback") or diff.get("rollback_to"))


def is_canary(change: dict[str, Any]) -> bool:
    diff = change.get("diff") or {}
    return "canary" in str(diff.get("rollout", "")).lower() or (
        isinstance(diff.get("canary_percent"), int | float) and 0 < float(diff["canary_percent"]) <= 25
    )


def is_rollback(change: dict[str, Any]) -> bool:
    text = str(change.get("description", "")).lower()
    return bool((change.get("diff") or {}).get("rollback") is True or "rollback" in text or "revert" in text)


def freeze_window(epoch: int) -> dict[str, Any] | None:
    """The POL-003 freeze window containing ``epoch`` (UTC only), else None."""
    doc = policies().get("POL-003", {})
    dt = datetime.fromtimestamp(epoch, UTC)
    minute = dt.hour * 60 + dt.minute
    for w in doc.get("windows", []):
        if w.get("tz", "UTC") != "UTC" or DAYS[dt.weekday()] not in w.get("days", []):
            continue
        sh, sm = (int(x) for x in str(w["start"]).split(":"))
        eh, em = (int(x) for x in str(w["end"]).split(":"))
        if sh * 60 + sm <= minute < eh * 60 + em:
            return dict(w)
    return None


def assess(change: dict[str, Any], *, requested_at: str, mode: str, in_freeze: bool) -> dict[str, Any]:
    comp = str(change.get("component") or "")
    kind = str(change.get("kind") or "config")
    cat = topology.catalog().get(comp)
    tier = int(cat["tier"]) if cat else None
    ckind = str(cat["kind"]) if cat else None
    blast = topology.static_blast_radius(comp) if cat else {"total_affected_if_down": 0}
    score = float(KIND_BASE.get(kind, 20))
    factors = [f"{kind} change (+{KIND_BASE.get(kind, 20)})"]
    if cat is None:
        score += 10
        factors.append("component not in the service catalog (+10)")
    else:
        if tier == 0:
            score += 20
            factors.append("tier-0 component (+20)")
        elif tier == 1:
            score += 8
            factors.append("tier-1 component (+8)")
        if ckind == "datastore":
            score += 10
            factors.append("datastore (+10)")
    if str(change.get("environment") or "prod") == "prod":
        score += 10
        factors.append("production (+10)")
    callers = int(blast.get("total_affected_if_down", 0))
    if callers:
        add = min(12, 2 * callers)
        score += add
        factors.append(f"{callers} dependent component(s) (+{add})")
    if in_freeze:
        score += 10
        factors.append("inside the change freeze (+10)")
    plan = has_rollback_plan(change)
    if kind in PLAN_KINDS and tier is not None and tier <= 1 and not plan:
        score += 10
        factors.append("no rollback plan (+10)")
    if is_canary(change):
        score -= 10
        factors.append("canary rollout (-10)")
    score = max(0.0, min(100.0, score))
    computed = tier_for(score)
    declared = str(change.get("risk") or "medium")
    effective = computed if tier_rank(computed) >= tier_rank(declared) else declared
    if effective != computed:
        factors.append(f"declared risk {declared} is higher than the computed {computed}")
    if mode == "gauntlet":
        effective = "critical"
        factors.append("gauntlet mode forces critical so every control point is exercised")
    return {
        "score": round(score, 1),
        "tier": effective,
        "computed_tier": computed,
        "declared": declared,
        "factors": factors,
        "blast_radius": sorted(topology.transitive_callers(comp)) if cat else [],
        "rollback_plan": plan,
        "component_tier": tier,
        "component_kind": ckind,
    }


def evaluate(
    change: dict[str, Any], risk: dict[str, Any], *, requested_at: str, mode: str, approval_policy: str, clarified: bool
) -> dict[str, Any]:
    """The policy gate: freeze, approver matrix, break-glass, observability.  Pure."""
    comp = str(change.get("component") or "")
    kind = str(change.get("kind") or "config")
    tier_name = str(risk["tier"])
    checks: list[PolicyCheck] = []
    blocked, reason = False, None

    if not topology.known(comp):
        blocked, reason = True, f"POL-006: {comp or 'the component'} cannot be observed (not in the service catalog)"
        checks.append(PolicyCheck(id="POL-006", status="fail", text=reason))
    else:
        checks.append(
            PolicyCheck(id="POL-006", status="pass", text=f"{comp} is observable (tier {risk['component_tier']})")
        )

    window = freeze_window(to_epoch(requested_at))
    break_glass = False
    if window is None:
        checks.append(PolicyCheck(id="POL-003", status="pass", text="outside the change freeze"))
    elif is_rollback(change):
        checks.append(
            PolicyCheck(id="POL-003", status="pass", text=f"{window['name']} freeze: rollbacks are always allowed")
        )
    elif (
        risk.get("component_tier") == 0 and risk.get("component_kind") == "datastore" and kind in ("migration", "scale")
    ):
        break_glass = True
        checks.append(
            PolicyCheck(
                id="POL-003",
                status="fail",
                text=f"{window['name']} freeze: a {kind} on a tier-0 datastore needs break-glass (reason >= 20 characters)",
            )
        )
    else:
        checks.append(
            PolicyCheck(
                id="POL-003", status="warn", text=f"{window['name']} freeze is active: incident commander co-signs"
            )
        )

    matrix = policies().get("POL-005", {}).get("risk_levels", {})
    level = "high" if tier_name == "critical" else tier_name
    row = matrix.get(level, {"approvers": {"low": 0, "medium": 1, "high": 2}[level], "role": "service_oncall"})
    approvers = int(row.get("approvers", 0))
    roles = [r for r in str(row.get("role", "")).split("+") if r]
    if approval_policy == "strict":
        approvers = min(2, approvers + 1)
        roles = roles or ["service_oncall"]
    if window is not None and approvers and risk.get("component_tier") is not None and risk["component_tier"] <= 1:
        approvers, roles = max(approvers, 2), ["service_oncall", "incident_commander"]
    path = "auto" if approvers == 0 and mode != "gauntlet" else ("four_eyes" if tier_name == "critical" else "approval")
    if path == "four_eyes":
        approvers = 2
        second = "dba" if risk.get("component_kind") == "datastore" else "incident_commander"
        roles = ["service_oncall", second]
    checks.append(
        PolicyCheck(
            id="POL-005",
            status="pass",
            text=f"{tier_name} risk: {approvers} approver(s) ({', '.join(roles) or 'none'})"
            + (", four-eyes (two reviewers decide in parallel)" if path == "four_eyes" else ""),
        )
    )
    needs_plan = (
        kind in PLAN_KINDS
        and tier_rank(tier_name) >= tier_rank("high")
        and not risk.get("rollback_plan")
        and not clarified
    )
    clarify = (mode == "gauntlet" and not clarified) or needs_plan
    if needs_plan:
        checks.append(
            PolicyCheck(
                id="POL-001", status="warn", text="high-risk change without a rollback plan: asking the requester"
            )
        )
    return {
        "checks": [c.model_dump() for c in checks],
        "freeze": window is not None,
        "freeze_window": window["name"] if window else None,
        "approvers_required": approvers,
        "roles": roles,
        "path": path,
        "break_glass": break_glass,
        "blocked": blocked,
        "block_reason": reason,
        "clarify": clarify,
    }
