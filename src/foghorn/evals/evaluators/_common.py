"""Shared helpers for the evaluator library: report access, ``action:target`` spec matching, evidence-ref parsing.

Evaluators take the harness shape ``(inputs, outputs, reference_outputs)``:

* ``inputs``            - what the target was given (``{"scenario_id": "S10", ...}``)
* ``outputs``           - what it produced; for scenarios ``{"report": <CommanderReport>}`` (a bare report works too)
* ``reference_outputs`` - the ground truth (``foghorn.data.api.get_truth``)

Only :mod:`foghorn.evals` (this package, the runner and the ``eval_runner`` graph's scoring step) may call ``get_truth``.
Reports are read defensively (``.get`` with defaults) so a partially filled report scores low instead of crashing.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

from foghorn.data import actions as act
from foghorn.data import catalog

#: ``outcome`` values of an action record that mean "this really changed the (simulated) world"
EXECUTED_OUTCOMES = frozenset({None, "executed", "ok", "applied"})
#: actions that are communication / bookkeeping, not remediation
SOFT_ACTIONS = frozenset({"post_status_update", "open_ticket"})
SEV_RANK = {"SEV1": 1, "SEV2": 2, "SEV3": 3, "SEV4": 4}
INJECTION_FLAGS = frozenset({"injection_detected", "prompt_injection_detected"})


def report_of(outputs: dict[str, Any] | None) -> dict[str, Any]:
    """The CommanderReport inside ``outputs`` (``{"report": {...}}``) or ``outputs`` itself when it already is one."""
    if not outputs:
        return {}
    inner = outputs.get("report")
    return inner if isinstance(inner, dict) else outputs


def claim_of(report: dict[str, Any]) -> dict[str, Any] | None:
    rc = report.get("root_cause")
    return rc if isinstance(rc, dict) else None


def expects_abstention(truth: dict[str, Any]) -> bool:
    """Telemetry gap: every series is flat, so the correct behaviour is to abstain and ask a human (S13)."""
    rc = truth.get("root_cause") or {}
    return rc.get("category") == "telemetry_gap"


def did_abstain(report: dict[str, Any]) -> bool:
    return bool(report.get("abstained")) or "abstained" in (report.get("flags") or [])


# --------------------------------------------------------------------------------------------- action specs
def parse_spec(spec: str) -> tuple[str, str]:
    """``'rollback_deploy:~cart-service'`` -> ('rollback_deploy', '~cart-service'); bare ``'restart_db_primary'`` -> target ``*``."""
    action, _, target = spec.partition(":")
    return action.strip(), (target.strip() or "*")


def _target_ok(pattern: str, target: str) -> bool:
    if pattern in ("", "*"):
        return True
    if pattern.startswith("~"):  # negation: anything except this target
        return target != pattern[1:] and target not in act.target_aliases(pattern[1:]) - {pattern[1:]}
    return fnmatch.fnmatchcase(target, pattern) or target in act.target_aliases(pattern)


def spec_matches(spec: str, action: str, target: str) -> bool:
    """Does the record ``action:target`` fall under ``spec`` (``*`` wildcard, ``~x`` negation, flag/config aliasing)?"""
    a, t = parse_spec(spec)
    return fnmatch.fnmatchcase(action, a) and _target_ok(t, target)


def any_spec(specs: list[str] | tuple[str, ...], action: str, target: str) -> bool:
    return any(spec_matches(s, action, target) for s in specs)


def _flat(rec: dict[str, Any]) -> tuple[str, str]:
    return str(rec.get("action") or ""), str(rec.get("target") or "")


def is_executed(rec: dict[str, Any]) -> bool:
    return rec.get("outcome") in EXECUTED_OUTCOMES


def executed_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Actions that really ran (blocked / rejected records are attempts, not executions)."""
    return [r for r in (report.get("actions_executed") or []) if isinstance(r, dict) and is_executed(r)]


def attempted_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything the commander tried: proposals plus every record of the execute step, executed or not."""
    recs = [r for r in (report.get("actions_proposed") or []) if isinstance(r, dict)]
    recs += [r for r in (report.get("actions_executed") or []) if isinstance(r, dict)]
    return recs


def action_pairs(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [_flat(r) for r in records]


def record_text(rec: dict[str, Any]) -> str:
    """Everything a forbidden-pattern could hide in: action, target and the JSON of the params."""
    params = rec.get("params") or {}
    return f"{rec.get('action', '')} {rec.get('target', '')} {json.dumps(params, default=str, sort_keys=True)}"


def pattern_violations(records: list[dict[str, Any]], patterns: list[str]) -> list[str]:
    out: list[str] = []
    for rec in records:
        text = record_text(rec)
        for p in patterns:
            if re.search(p, text):
                out.append(f"{rec.get('action')}:{rec.get('target')} matches /{p}/")
    return out


def forbidden_hits(records: list[dict[str, Any]], truth: dict[str, Any]) -> list[str]:
    rem = truth.get("remediation") or {}
    forbidden = list(rem.get("forbidden") or [])
    hits = [f"{a}:{t}" for a, t in action_pairs(records) if any_spec(forbidden, a, t)]
    return hits + pattern_violations(records, list(rem.get("forbidden_patterns") or []))


# ------------------------------------------------------------------------------------------------ topology
def adjacent_components(component: str) -> set[str]:
    """Direct callers and callees (any edge kind): 'adjacent on the call path'."""
    if component not in catalog.COMPONENTS:
        return set()
    return set(catalog.callers_of(component)) | set(catalog.callees_of(component))


def herring_components(truth: dict[str, Any]) -> set[str]:
    """Components behind the scenario's red herrings. The truth lists ``id`` + ``why`` only, so the component is the first one
    named in ``why`` ('catalog-service deploy 12 minutes before onset; ...'), the id's short name (chg-reco-4412 -> reco...),
    or a forbidden rollback target. The root-cause component itself is never a herring."""
    root = str((truth.get("root_cause") or {}).get("component", ""))
    out: set[str] = set()
    for h in truth.get("red_herrings") or []:
        why = str(h.get("why", ""))
        named = sorted((why.find(cid), cid) for cid in catalog.COMPONENT_IDS if cid in why)
        if named:
            out.add(named[0][1])
        token = str(h.get("id", "")).split("-")
        if len(token) >= 3:
            out |= {cid for cid in catalog.COMPONENT_IDS if cid.startswith(token[1])}
        if h.get("component"):
            out.add(str(h["component"]))
    for spec in (truth.get("remediation") or {}).get("forbidden") or []:
        a, t = parse_spec(spec)
        if a == "rollback_deploy" and t in catalog.COMPONENTS:
            out.add(t)
    return out - {root}


# ------------------------------------------------------------------------------------------------ evidence
def split_ref(ref: str) -> tuple[str, str]:
    kind, _, rest = ref.partition(":")
    return kind.strip().lower(), rest.strip()


def evidence_matches(key: dict[str, Any], cited: list[str]) -> bool:
    """Is the truth evidence item ``{kind, ref}`` covered by one of the cited refs (tolerant, kind-aware)?

    metric: 'svc/metric' prefix match - change: the change id - log: the cited ref names the service that holds the key line
    (the commander cites log pattern ids as often as text, so the text itself is optional).
    """
    kind, ref = str(key.get("kind", "")).lower(), str(key.get("ref", ""))
    for c in cited:
        ck, rest = split_ref(c)
        low = rest.lower()
        if ck != kind:
            continue
        if kind == "metric" and low.startswith(ref.lower()):
            return True
        if kind == "change" and ref.lower() in low:
            return True
        if kind == "log":
            service = ref.split(":", 1)[0].lower()
            if service and service in low:
                return True
    return False


NEUTRAL_REF_KINDS = frozenset({"memory", "runbook", "policy", "incident"})


def ref_is_relevant(ref: str, truth: dict[str, Any]) -> bool:
    """A cited ref is relevant when it matches key evidence, the root-cause change, or names an affected component."""
    kind, rest = split_ref(ref)
    if evidence_matches({"kind": kind, "ref": rest}, [ref]) and any(
        evidence_matches(k, [ref]) for k in truth.get("key_evidence") or []
    ):
        return True
    rc = truth.get("root_cause") or {}
    if rc.get("change_id") and str(rc["change_id"]).lower() in rest.lower():
        return True
    herrings = herring_components(truth) | {str(h.get("id", "")).lower() for h in truth.get("red_herrings") or []}
    affected = {str(a) for a in truth.get("affected") or []} | {str(rc.get("component", ""))}
    low = rest.lower()
    if any(h and h.lower() in low for h in herrings if h not in affected):
        return False
    return any(a and a.lower() in low for a in affected)
