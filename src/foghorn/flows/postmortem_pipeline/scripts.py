"""Offline behaviour of the three model calls of the pipeline (``smart`` body, ``smart`` summary, ``judge`` review).

The scripts are STATELESS functions of the transcript (docs: models/offline.py): they read the ``<facts>`` bundle the
task put in the human message (built from the commander report and tool results, never from ground truth), compose the
text from it, and the critic runs the real checks of ``checks.py`` over the draft it is shown.

Profiles (``model_profile`` run context):
* ``competent`` (and cautious / gullible, which behave the same here): first draft forgets the action-item owners, the
  revise pass fixes them from the reviewer's issues - the review loop is visible and converges after one round.
* ``naive``: sloppy and deaf to feedback (truncated timeline, no owners, "most recent change" as the cause), so the
  loop runs to its bound and the human sees the open issues.
"""

from __future__ import annotations

import json
import re
from typing import Any

from foghorn.flows.postmortem_pipeline.checks import check_postmortem
from foghorn.flows.postmortem_pipeline.models import PostmortemBody, Review
from foghorn.models.offline import Call, Script, struct

_BLOCK = re.compile(r"<(?P<tag>facts|draft|issues|revision)>(?P<body>.*?)</(?P=tag)>", re.S)


def pack(
    facts: dict[str, Any],
    *,
    revision: int,
    issues: list[dict[str, Any]] | None = None,
    draft: dict[str, Any] | None = None,
) -> str:
    """The human message the tasks send (also what a real model reads)."""
    parts = [f"<revision>{revision}</revision>", f"<facts>{json.dumps(facts, sort_keys=True)}</facts>"]
    if issues:
        parts.append(f"<issues>{json.dumps(issues)}</issues>")
    if draft is not None:
        parts.append(f"<draft>{json.dumps(draft, sort_keys=True)}</draft>")
    return "\n".join(parts)


def unpack(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in _BLOCK.finditer(text):
        tag, body = m.group("tag"), m.group("body")
        out[tag] = int(body) if tag == "revision" else json.loads(body)
    return out


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]


def _impact_text(facts: dict[str, Any]) -> str:
    imp = facts.get("impact") or {}
    w = imp.get("worst_alert")
    parts: list[str] = []
    if w:
        parts.append(
            f"at peak {w['component']} {w['metric']} was {w['peak_value']} against a threshold of {w['threshold']} ({w['ratio']}x)"
        )
    if imp.get("alerts"):
        parts.append(
            f"{imp['alerts']} alert(s) fired across {len(imp['components'])} service(s): {', '.join(imp['components'])}"
        )
    if imp.get("duration_min") is not None:
        parts.append(f"about {imp['duration_min']:g} minutes elapsed from the first alert to recovery")
    elif facts.get("recovered") is not True:
        parts.append("recovery was not confirmed when this draft was written")
    if imp.get("revenue_path"):
        parts.append("the revenue path was exposed")
    elif imp.get("customer_facing"):
        parts.append("customer-facing services were exposed")
    if not parts:
        return "Impact could not be quantified: no alert data was available inside the incident window."
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def _root_cause_text(facts: dict[str, Any], *, naive: bool) -> str:
    root, pb = facts.get("root_cause"), facts.get("playbook") or {}
    if naive:
        changes = [e for e in facts.get("timeline", []) if e["kind"] == "change"]
        if changes:
            return f"Probably the most recent change: {changes[-1]['summary']}."
    if not facts.get("report_available"):
        return "Root cause not determined: the commander report was not available when this draft was written."
    if not root or facts.get("abstained"):
        return "Root cause not determined: the commander abstained because the evidence did not support a confident conclusion."
    refs = ", ".join(root.get("evidence_refs", [])[:4]) or "none recorded"
    return f"The commander concluded the cause was {pb.get('label', root['component'])} (confidence {round(root['confidence'] * 100)} percent). Evidence: {refs}."


def _went_well(facts: dict[str, Any]) -> list[str]:
    out: list[str] = []
    executed = facts.get("actions_executed") or []
    if executed and facts.get("recovered"):
        out.append(
            "Recovery was confirmed after " + ", ".join(f"{a['action']} on {a['target']}" for a in executed) + "."
        )
    elif executed:
        out.append("Mitigation was applied: " + ", ".join(f"{a['action']} on {a['target']}" for a in executed) + ".")
    gated = [a for a in facts.get("approvals") or [] if a.get("status") in {"approved", "edited"}]
    if gated:
        out.append(f"{len(gated)} state-changing step(s) went through human approval before they ran.")
    if facts.get("memory_recalled"):
        out.append(
            f"{len(facts['memory_recalled'])} memory item(s) from past incidents were recalled during the response."
        )
    if "red_herring_rejected" in (facts.get("flags") or []):
        out.append("A distractor change was investigated and correctly rejected.")
    return out or ["The incident was detected, triaged and tracked from the first alert."]


def _went_badly(facts: dict[str, Any], *, naive: bool) -> list[str]:
    out: list[str] = []
    if facts.get("abstained"):
        out.append("The commander abstained, so the root cause had to be settled by the human responders.")
    if facts.get("recovered") is False:
        out.append("Recovery was not confirmed by the verification step.")
    labels = {
        "policy_blocked": "A proposed action was blocked by policy and the plan had to change.",
        "replanned": "The first remediation plan was rejected or failed and the commander replanned.",
        "budget_exceeded": "The commander ran out of tool-call budget before finishing.",
        "injection_detected": "Untrusted log text tried to steer the response and was ignored.",
    }
    out.extend(labels[f] for f in facts.get("flags") or [] if f in labels)
    if not naive:
        out.extend(f"Reviewer feedback from an earlier revision: {n}" for n in facts.get("feedback") or [])
    return out or ["No significant response problems were recorded in the commander report."]


def _action_items(facts: dict[str, Any], *, with_owners: bool) -> list[dict[str, Any]]:
    people = (facts.get("oncall") or {}).get("people", {})
    items: list[dict[str, Any]] = []
    for f in (facts.get("playbook") or {}).get("follow_ups", []):
        owner = (people.get(f["owner_role"]) or {}).get("id") if with_owners else None
        items.append({"title": f["title"], "owner": owner, "kind": f["kind"], "priority": f["priority"]})
    for a in facts.get("actions_executed") or []:
        owner = (people.get("incident_commander") or {}).get("id") if with_owners else None
        items.append(
            {
                "title": f"Review whether {a['action']} on {a['target']} can be a pre-approved runbook step",
                "owner": owner,
                "kind": "mitigate",
                "priority": "p2",
            }
        )
    return items


def compose_body(facts: dict[str, Any], *, revision: int, issues: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """The deterministic stand-in for the drafting model. ``revision`` 0 is the first pass."""
    naive = profile == "naive"
    fixes = {i["id"] for i in issues}
    comp = facts.get("component") or "the affected service"
    sev = facts["incident"]["severity"]
    timeline = facts.get("timeline") or []
    if naive:
        timeline = timeline[:2]
    with_owners = not naive and (revision > 0 or "action_items_owned" in fixes)
    key_changes = [e for e in timeline if e["kind"] == "change" and e["key"]]
    detection = next((e for e in timeline if e["kind"] == "detection"), None)
    factors = list((facts.get("playbook") or {}).get("factors", []))
    factors.extend(f"A change preceded the first alert: {e['summary']}" for e in key_changes[:2])
    body = PostmortemBody(
        title=facts["incident"]["title"] or f"{sev} incident on {comp}",
        impact=_impact_text(facts),
        root_cause=_root_cause_text(facts, naive=naive),
        detection=(
            f"Detected at {detection['t']} by {detection['summary']}."
            if detection
            else "Detection time is not recorded."
        ),
        contributing_factors=factors,
        timeline=[
            {"t": e["t"], "kind": e["kind"], "summary": e["summary"], "ref": e["ref"], "key": e["key"]}
            for e in timeline
        ],
        what_went_well=_went_well(facts),
        what_went_badly=_went_badly(facts, naive=naive),
        action_items=_action_items(facts, with_owners=with_owners),
        lessons_learned=list((facts.get("playbook") or {}).get("lessons", [])),
    )
    return body.model_dump(mode="json")


def _impact_digest(facts: dict[str, Any]) -> str:
    imp = facts.get("impact") or {}
    w = imp.get("worst_alert")
    if not w:
        return "Impact could not be quantified."
    tail = (
        f", recovered after about {imp['duration_min']:g} minutes"
        if imp.get("duration_min") is not None
        else ", recovery not confirmed"
    )
    return f"{w['component']} {w['metric']} reached {w['ratio']}x its alert threshold{tail}."


def compose_summary(facts: dict[str, Any], body: dict[str, Any]) -> str:
    comp = facts.get("component") or "the affected service"
    owned = sum(1 for i in body["action_items"] if i.get("owner"))
    return (
        f"{facts['incident']['severity']} incident {facts['incident']['id']} on {comp}: {_impact_digest(facts)} "
        f"{_first_sentence(body['root_cause'])} "
        f"{len(body['action_items'])} follow-up action(s) were proposed, {owned} with a named owner."
    )


# ---------------------------------------------------------------------------------------------------- the scripts
def _draft_body(c: Call) -> Any:
    got = unpack(c.human)
    return struct(
        compose_body(got["facts"], revision=got.get("revision", 0), issues=got.get("issues", []), profile=c.profile)
    )


def _draft_summary(c: Call) -> str:
    got = unpack(c.human)
    return compose_summary(got["facts"], got["draft"])


def _review(c: Call) -> Any:
    got = unpack(c.human)
    found = check_postmortem(got["draft"], got["facts"])
    blockers = [i for i in found if i.severity == "blocker"]
    verdict = (
        "no blocking issues"
        if not blockers
        else f"{len(blockers)} blocking issue(s): " + ", ".join(i.id for i in blockers)
    )
    return struct(Review(ok=not blockers, issues=found, summary=f"Critic: {verdict}."))


def draft_script() -> Script:
    return (
        Script()
        .when("draft-body", lambda c: c.schema_name == "PostmortemBody", _draft_body)
        .when("draft-summary", lambda c: c.schema_name is None and "<facts>" in c.human, _draft_summary)
    )


def review_script() -> Script:
    return Script().when("review", lambda c: c.schema_name == "Review", _review)
