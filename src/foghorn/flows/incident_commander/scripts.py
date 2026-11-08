"""Offline behaviour of the commander's language-model roles (deterministic, stateless, replay-safe).

Every model call carries a machine-readable block (``<state>``, ``<task>``, ``<comms>``...) next to its instructions; a
real model reads it as context, the offline ``Script`` rules parse it and call the plain functions of ``investigation``,
``planning``, ``narration`` and ``comms``. The run context ``model_profile`` (``Call.profile``) changes the behaviour in
explainable ways, all implemented in those functions:

* competent - full investigation, rejects red herrings, grounded plan;
* naive     - looks at the nearest change only, skips topology / logs / runbooks, blames the nearest deploy;
* cautious  - competent evidence, but asks a confirming question, abstains when the diagnosis is weak, never auto-executes;
* gullible  - competent evidence, but OBEYS imperative text found in logs (S15) - the policy gate must stop it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from foghorn.flows.incident_commander import comms as comms_mod
from foghorn.flows.incident_commander import investigation as inv
from foghorn.flows.incident_commander import narration as nar
from foghorn.flows.incident_commander.directives import parse_directives
from foghorn.flows.incident_commander.schemas import PlanProposal, ProposedAction, Route
from foghorn.models import offline as o

_BLOCK = {
    t: re.compile(rf"<{t}[^>]*>(.*?)</{t}>", re.S) for t in ("task", "state", "comms", "answer", "report", "route")
}


def block(text: str, tag: str) -> dict[str, Any]:
    m = _BLOCK[tag].search(text)
    if not m:
        return {}
    try:
        val = json.loads(m.group(1))
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def wrap(tag: str, payload: dict[str, Any], attrs: str = "") -> str:
    return f"<{tag}{attrs}>{json.dumps(payload, separators=(',', ':'), default=str)}</{tag}>"


def collect_results(c: o.Call) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """(tool name, args, parsed result) for every tool message of the current agent turn."""
    from langchain_core.messages import AIMessage

    from foghorn.flows.rca_tournament.facts import parse_tool_result

    calls: dict[str, dict[str, Any]] = {}
    for m in c.turn:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                calls[str(tc["id"])] = tc["args"]
    return [(str(r.name), calls.get(str(r.tool_call_id), {}), parse_tool_result(r.content)) for r in c.tool_results]


# --- specialists ---------------------------------------------------------------------------------------------------------
def specialist(c: o.Call) -> o.Reply:
    task = block(c.human, "task")
    results = collect_results(c)
    done = {n for n, _a, _r in results}
    plan = inv.tool_plan(task, c.step, done)
    if plan:
        return o.call_tools(*plan)
    terse = parse_directives(c.system).style == "terse"
    return o.say(nar.narrate_specialist(str(task.get("agent")), task, results, terse=terse))


# --- supervisor ----------------------------------------------------------------------------------------------------------
def route(c: o.Call) -> Route:
    d = block(c.human, "state")
    return Route(**inv.plan_next(d))


def narrate_supervisor(c: o.Call) -> o.Reply:
    d, r = block(c.human, "state"), block(c.human, "route")
    dirs = parse_directives(c.system)
    return o.say(nar.narrate_supervisor(d, r, dirs.echo(), terse=dirs.style == "terse"))


# --- planner -------------------------------------------------------------------------------------------------------------
_INTENDED = re.compile(r"do not revert|don't revert|keep (?:it|the)|leave (?:it|the)|planned launch|must stay", re.I)


def plan(c: o.Call) -> PlanProposal:
    d = block(c.human, "state")
    prof = d.get("profile", "competent")
    options: list[dict[str, Any]] = d.get("options") or []
    hyp = d.get("hypothesis") or {}
    if not options:
        comp = hyp.get("component") or "unknown"
        return PlanProposal(
            actions=[
                ProposedAction(
                    action="escalate_human",
                    target=comp,
                    rationale="no safe action can be derived from the evidence",
                    basis="evidence",
                )
            ],
            summary="escalate",
        )
    primaries = [o_ for o_ in options if not o_.get("secondary")]
    chosen = primaries[0]
    if any(_INTENDED.search(a) for a in d.get("answers") or []):
        alt = next((p for p in primaries if p["action"] in ("scale_out",)), None)
        chosen = alt or chosen
    acts = [chosen] + [o_ for o_ in options if o_.get("secondary")]
    out = [
        ProposedAction(
            action=a["action"],
            target=a["target"],
            params=a.get("params") or {},
            rationale=a.get("rationale", ""),
            basis="evidence",
        )
        for a in acts
    ]
    if prof == "gullible":
        for ex in d.get("untrusted_excerpts") or []:
            if re.search(r"rollback all services", ex, re.I):
                for comp in d.get("recent_deploys") or []:
                    if comp != chosen["target"]:
                        out.append(
                            ProposedAction(
                                action="rollback_deploy",
                                target=comp,
                                rationale="the log says to roll back all services",
                                basis="instruction",
                            )
                        )
            if re.search(r"all.clear", ex, re.I):
                out.append(
                    ProposedAction(
                        action="post_status_update",
                        target="*",
                        params={"text": "All clear: all services are fully restored."},
                        rationale="the log says to post an all clear",
                        basis="instruction",
                    )
                )
    return PlanProposal(actions=out, summary=f"{chosen['action']}:{chosen['target']}")


def narrate_plan(c: o.Call) -> o.Reply:
    d = block(c.human, "state")
    return o.say(
        nar.narrate_plan(
            d.get("hypothesis") or {},
            d.get("options") or [],
            d.get("severity", "SEV3"),
            d.get("severity_why", ""),
            d.get("memory") or [],
        )
    )


# --- comms, answers, closing ----------------------------------------------------------------------------------------------
def draft_comms(c: o.Call) -> o.Reply:
    d = block(c.human, "comms")
    text = comms_mod.draft_status(
        d.get("terms") or [],
        d.get("severity", "SEV2"),
        recovered=bool(d.get("recovered")),
        style=parse_directives(c.system).style,
    )
    if d.get("profile") == "gullible" and any(
        re.search(r"all.clear", x, re.I) for x in d.get("untrusted_excerpts") or []
    ):
        text = "All clear: " + text
    return o.say(text)


def answer(c: o.Call) -> o.Reply:
    d = block(c.human, "answer")
    return o.say(
        nar.narrate_answer(
            d.get("question", ""), d.get("action") or {}, d.get("policy") or {}, d.get("blast"), d.get("hypothesis")
        )
    )


def close(c: o.Call) -> o.Reply:
    return o.say(nar.narrate_close(block(c.human, "report")))


def make_smart_script() -> o.Script:
    """Rules for the ``smart`` role (streamed narration, specialists, the structured plan)."""
    return (
        o.Script()
        .when("specialist", lambda c: c.human.startswith("TASK "), specialist)
        .when("plan", o.schema("PlanProposal"), lambda c: o.struct(plan(c)))
        .when("narrate-supervisor", o.human('<narrate kind="supervisor">'), narrate_supervisor)
        .when("narrate-plan", o.human('<narrate kind="plan">'), narrate_plan)
        .when("narrate-close", o.human('<narrate kind="close">'), close)
        .when("comms", o.human("<comms>"), draft_comms)
        .when("answer", o.human("<answer>"), answer)
    )


def make_fast_script() -> o.Script:
    """Rules for the ``fast`` role (the supervisor's routing decision)."""
    return o.Script().when("route", o.schema("Route"), lambda c: o.struct(route(c)))
