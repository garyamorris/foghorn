"""The 22-action catalogue, the approval matrix (POL-005) and the break-glass rules (POL-003).

Action ids are the single source of truth: runbooks, policies, past incidents and scenario truth are validated
against ``ACTIONS`` by the integrity tests.

Action *targets* are written ``action:target`` throughout the datasets. A target is a component id, a feature-flag
or config key, a zone id (``drain_zone``) or ``*``.  In ``forbidden`` lists a target may be negated with ``~``
(``rollback_deploy:~cart-service`` = rollback of anything except cart-service).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from foghorn.data import catalog


@dataclass(frozen=True)
class ActionDef:
    id: str
    label: str
    risk: str  # low | medium | high | critical
    reversible: bool
    kind: str  # state_changing | comms | ticket | escalation
    target_kind: str  # component | flag | config | zone | datastore | alert | any
    approvers: int  # POL-005 risk-level approvers
    extra_roles: tuple[str, ...] = ()
    never_automatic: bool = False
    needs_params: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "risk": self.risk,
            "reversible": self.reversible,
            "kind": self.kind,
            "target_kind": self.target_kind,
            "approvers_required": self.approvers,
            "extra_roles": list(self.extra_roles),
            "never_automatic": self.never_automatic,
        }


_A = ActionDef
ACTIONS: dict[str, ActionDef] = {
    a.id: a
    for a in (
        _A(
            "rollback_deploy",
            "Roll back the last deploy of a component",
            "medium",
            True,
            "state_changing",
            "component",
            1,
        ),
        _A("revert_flag", "Revert a feature flag to its previous value", "medium", True, "state_changing", "flag", 1),
        _A(
            "revert_config",
            "Revert a configuration key to its previous value",
            "medium",
            True,
            "state_changing",
            "config",
            1,
        ),
        _A("restart_service", "Rolling restart of a service", "medium", True, "state_changing", "component", 1),
        _A("scale_out", "Add replicas / capacity", "medium", True, "state_changing", "component", 1),
        _A(
            "renew_cert",
            "Issue and install a renewed TLS certificate",
            "medium",
            True,
            "state_changing",
            "component",
            1,
        ),
        _A("rotate_logs", "Rotate and compress logs to free disk", "low", True, "state_changing", "component", 0),
        _A("expand_volume", "Expand the data volume", "medium", False, "state_changing", "component", 1),
        _A(
            "kill_queries",
            "Terminate runaway / long-running database queries",
            "medium",
            False,
            "state_changing",
            "datastore",
            1,
        ),
        _A(
            "add_index",
            "Build a database index (online, CONCURRENTLY)",
            "medium",
            False,
            "state_changing",
            "datastore",
            1,
            extra_roles=("dba",),
            needs_params=("sql",),
        ),
        _A("warm_cache", "Pre-warm a cache from the database", "low", True, "state_changing", "component", 0),
        _A("pause_consumer", "Pause a queue consumer", "medium", True, "state_changing", "component", 1),
        _A(
            "quarantine_message",
            "Move a poison message to the dead-letter topic",
            "medium",
            False,
            "state_changing",
            "component",
            1,
        ),
        _A(
            "enable_fallback_provider",
            "Switch traffic to the fallback provider",
            "high",
            True,
            "state_changing",
            "component",
            2,
        ),
        _A("drain_zone", "Drain a zone out of the load balancers", "high", True, "state_changing", "zone", 2),
        _A("failover_dns", "Fail over DNS to the healthy zone", "high", True, "state_changing", "component", 2),
        _A(
            "suppress_alert",
            "Suppress alerts for a component for a bounded time",
            "low",
            True,
            "state_changing",
            "alert",
            0,
        ),
        _A(
            "failover_db_primary",
            "Promote a replica to primary",
            "high",
            False,
            "state_changing",
            "datastore",
            2,
            extra_roles=("dba",),
        ),
        _A(
            "restart_db_primary",
            "Restart the database primary (last resort)",
            "critical",
            False,
            "state_changing",
            "datastore",
            2,
            extra_roles=("dba",),
            never_automatic=True,
        ),
        _A("post_status_update", "Publish a status page update", "low", True, "comms", "any", 0),
        _A("open_ticket", "Open a follow-up ticket", "low", True, "ticket", "any", 0),
        _A("escalate_human", "Escalate to the on-call human", "low", True, "escalation", "any", 0),
    )
}
assert len(ACTIONS) == 22

#: POL-003: while the peak-trading freeze is active these need break-glass (reason >= 20 chars + second approver)
BREAK_GLASS_DURING_FREEZE: dict[str, str] = {
    "add_index": "*",
    "failover_db_primary": "*",
    "restart_db_primary": "*",
    "scale_out": "datastore",  # scale_out_datastore in the policy text
    "kill_queries": "datastore",  # manual intervention on a tier-0 datastore at peak
}
BREAK_GLASS_REASON_MIN_CHARS = 20
#: online index builds only
INDEX_FORBIDDEN_PATTERN = r"(?i)create\s+(unique\s+)?index(?!\s+concurrently)"

STATE_CHANGING = frozenset(a.id for a in ACTIONS.values() if a.kind == "state_changing")


def parse_action(spec: str) -> tuple[str, str]:
    """``'revert_flag:new_pricing_engine'`` -> ('revert_flag', 'new_pricing_engine'); bare id -> target '*'."""
    action, _, target = spec.partition(":")
    return action.strip(), (target.strip() or "*")


def known_target(target: str) -> bool:
    """True if ``target`` names something that exists in the world (component, flag, config, zone, '*')."""
    if target in ("*", "zone-a", "zone-b"):
        return True
    if target in catalog.COMPONENTS:
        return True
    return any(target in c.flags or target in c.configs for c in catalog.COMPONENTS.values())


def target_aliases(target: str) -> set[str]:
    """A flag/config key is an alias of the component that owns it."""
    out = {target}
    for c in catalog.COMPONENTS.values():
        if target in c.flags or target in c.configs:
            out.add(c.id)
    return out


def target_matches(entry_target: str, target: str) -> bool:
    """Does an entry target (may be ``*``, a glob, ``~x`` negation) cover ``target`` (flag/config <-> owner aware)?"""
    if entry_target.startswith("~"):
        return not target_matches(entry_target[1:], target)
    if entry_target == "*":
        return True
    ea, ta = target_aliases(entry_target), target_aliases(target)
    if ea & ta:
        return True
    return any(fnmatch.fnmatchcase(t, e) for e in ea for t in ta)


def action_matches(entry: str, action: str, target: str = "*") -> bool:
    """Does the ``action:target`` pattern ``entry`` cover (action, target)?  Used by evaluators and the overlay."""
    e_action, e_target = parse_action(entry)
    if not fnmatch.fnmatchcase(action, e_action):
        return False
    return target_matches(e_target, target)


def violates_forbidden_pattern(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def required_approvals(action: str) -> dict[str, Any]:
    a = ACTIONS[action]
    return {
        "risk": a.risk,
        "approvers": a.approvers,
        "extra_roles": list(a.extra_roles),
        "never_automatic": a.never_automatic,
    }


def break_glass_needed(action: str, target: str, *, frozen: bool) -> bool:
    if not frozen or action not in BREAK_GLASS_DURING_FREEZE:
        return False
    scope = BREAK_GLASS_DURING_FREEZE[action]
    if scope == "*":
        return True
    owner = catalog.COMPONENTS.get(target)
    return owner is not None and owner.kind in ("datastore", "queue")
