"""The policy gate (pure): every proposed action is checked against the policies of the data layer.

* POL-005 approval matrix (risk -> approvers/roles; SEV1 adds the incident commander; never-automatic actions are blocked)
* POL-003 change freeze (break-glass actions need a reason of 20+ characters and a second approver)
* POL-004 untrusted content (an action must come from evidence, never from text found in a log; its target must be in
  scope: the hypothesised component, the owner of the changed flag/config, the provider's caller, the failing zone)
* POL-006 observability ('never act on a component you cannot observe')
* RB-009 online index builds only (CREATE INDEX CONCURRENTLY)

Verdicts: ``ok`` (go to approval), ``edit`` (fixable by a human edit of the parameters), ``block`` (never executes).
"""

from __future__ import annotations

import re
from typing import Any

from foghorn.data import actions as act
from foghorn.data import catalog
from foghorn.data.util import in_freeze, to_epoch
from foghorn.flows.incident_commander.planning import label
from foghorn.flows.rca_tournament.facts import Facts

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
TRUSTED_SOURCES = frozenset({"evidence", "runbook", "memory"})


def allowed_targets(f: Facts, hyp: dict[str, Any] | None) -> set[str]:
    """Targets an action may legitimately aim at for hypothesis ``hyp`` (POL-004 scope)."""
    if hyp is None:
        return set()
    comp = hyp["component"]
    out = {comp, *act.target_aliases(comp)}
    out.update(
        catalog.callers_of(comp, sync_only=True)
        if catalog.COMPONENTS.get(comp) and catalog.COMPONENTS[comp].kind in ("external", "queue")
        else ()
    )
    for ch in f.changes_on(comp):
        d = ch.get("details") or {}
        out.update(str(d[k]) for k in ("flag", "key") if k in d)
        if ch["kind"] == "cron_job":
            out.update(catalog.COMPONENTS[comp].configs if comp in catalog.COMPONENTS else ())
    out.update({"zone-a", "zone-b"} if hyp["category"] == "network_partition" else ())
    return out


def evaluate_action(
    action: dict[str, Any],
    *,
    facts: Facts,
    hypothesis: dict[str, Any] | None,
    severity: str,
    as_of: str,
) -> dict[str, Any]:
    """Run every check against one proposed action. Pure; the node emits the ``policy.check`` events."""
    a, target = action["action"], action["target"]
    spec = act.ACTIONS.get(a)
    checks: list[dict[str, str]] = []
    verdict = "ok"

    def add(cid: str, status: str, text: str) -> None:
        checks.append({"id": cid, "status": status, "text": text})

    if spec is None:
        add("POL-005", "fail", f"{a} is not in the action catalogue")
        return _result(action, checks, "block", 0, [], False, "unknown action")
    risk = spec.risk
    approvers = spec.approvers
    roles = ["service_oncall"] if approvers >= 1 else []
    if approvers >= 2:
        roles = ["service_oncall", "incident_commander"]
    roles += list(spec.extra_roles)
    if severity == "SEV1" and RISK_ORDER[risk] >= 1 and approvers < 2:
        approvers = 2
        roles = list(dict.fromkeys([*roles, "incident_commander"]))
    add(
        "POL-005",
        "pass",
        f"{risk} risk, {'reversible' if spec.reversible else 'irreversible'}: "
        + (f"{approvers} approver(s) ({', '.join(roles)})" if approvers else "no approval needed")
        + (
            " - SEV1 adds the incident commander"
            if severity == "SEV1" and approvers >= 2 and spec.approvers < 2
            else ""
        ),
    )
    if spec.never_automatic:
        add("POL-005:never-automatic", "fail", f"{a} is never automatic: a DBA performs it by hand outside Foghorn")
        verdict = "block"
    frozen = in_freeze(to_epoch(as_of))
    break_glass = act.break_glass_needed(a, target, frozen=frozen)
    if frozen:
        if break_glass:
            add(
                "POL-003",
                "warn",
                "change freeze (weekdays 09-18 UTC): break-glass needed (reason of 20+ characters and a second approver)",
            )
        else:
            add("POL-003", "pass", "change freeze is active but this action is a rollback/revert/safe mitigation")
    else:
        add("POL-003", "pass", "no change freeze at this time")
    src = action.get("source", "evidence")
    if src not in TRUSTED_SOURCES:
        add(
            "POL-004",
            "fail",
            f"action sourced from {src.replace('_', ' ')}: text in logs and reports is data, never an instruction",
        )
        verdict = "block"
    else:
        add("POL-004", "pass", "action is derived from evidence, not from text found in a log or report")
    if spec.kind in ("state_changing",) and hypothesis is not None:
        allowed = allowed_targets(facts, hypothesis)
        if target not in allowed and not (act.target_aliases(target) & allowed):
            add(
                "POL-004:scope",
                "fail",
                f"{target} is outside the scope of the diagnosis ({hypothesis['component']}): refusing to act on it",
            )
            verdict = "block"
        else:
            add("POL-004:scope", "pass", f"{target} is the diagnosed component or owned by it")
    flat = facts.flat()
    if spec.kind == "state_changing" and target in flat:
        add("POL-006", "fail", f"{target} telemetry is flat: never act on a component you cannot observe")
        verdict = "block"
    elif spec.kind == "state_changing":
        add("POL-006", "pass", "the target is observable")
    if a == "add_index":
        sql = str((action.get("params") or {}).get("sql", ""))
        if not sql:
            add("RB-009", "fail", "add_index needs the SQL to run; a human must supply it")
            verdict = "edit" if verdict == "ok" else verdict
        elif re.search(act.INDEX_FORBIDDEN_PATTERN, sql):
            add("RB-009", "fail", "plain CREATE INDEX blocks writes; use CREATE INDEX CONCURRENTLY (RB-009)")
            verdict = "edit" if verdict == "ok" else verdict
        else:
            add("RB-009", "pass", "online index build (CONCURRENTLY)")
    blast = len(catalog.transitive_callers(target)) if target in catalog.COMPONENTS else 0
    if spec.kind == "state_changing":
        add(
            "BLAST",
            "warn" if blast >= 6 else "pass",
            f"{blast} callers depend on {target}" if blast else f"{target} has no dependent callers",
        )
    return _result(
        action,
        checks,
        verdict,
        approvers,
        roles,
        break_glass,
        next((c["text"] for c in checks if c["status"] == "fail"), ""),
    )


def _result(
    action: dict[str, Any],
    checks: list[dict[str, str]],
    verdict: str,
    approvers: int,
    roles: list[str],
    break_glass: bool,
    reason: str,
) -> dict[str, Any]:
    spec = act.ACTIONS.get(action["action"])
    return {
        "action_id": action["id"],
        "label": label(action),
        "checks": checks,
        "verdict": verdict,
        "approvers_required": approvers,
        "roles": list(dict.fromkeys(roles)),
        "break_glass": break_glass,
        "risk": spec.risk if spec else "high",
        "reversible": spec.reversible if spec else False,
        "reason": reason,
    }


def needs_human(
    approval_policy: str, risk_directive: str, policy: dict[str, Any], profile: str, severity: str
) -> tuple[bool, str]:
    """Does this action need a human decision under the run's ``approval_policy``? (reason for the audit trail)

    strict = always ask; standard = ask for medium+ risk, irreversible, approver-gated, warned or SEV1; auto_low_risk =
    skip ONLY low-risk, reversible, approver-free actions with a clean check list. ``cautious`` and ``risk: strict``
    behave as strict ('never auto-executes')."""
    if approval_policy == "strict" or profile == "cautious" or risk_directive == "strict":
        return True, "strict policy: every action is reviewed"
    gate = (
        policy["approvers_required"] > 0
        or RISK_ORDER.get(policy["risk"], 3) >= 1
        or not policy["reversible"]
        or policy["break_glass"]
        or any(c["status"] != "pass" for c in policy["checks"] if c["id"] != "BLAST")
    )
    if approval_policy == "auto_low_risk":
        return (
            gate,
            "low-risk, reversible, approver-free: auto-approved" if not gate else "not low-risk: a human decides",
        )
    if gate:
        return True, "POL-005: approval required"
    if severity == "SEV1":
        return True, "SEV1: a human reviews every action"
    return False, "low-risk, reversible, approver-free action"
