"""Offline behaviour of the ``smart`` model that answers a reviewer's question (node ``answer_question``).

The node runs the grounding tools itself (blast radius, runbooks, past incidents, recent changes) and hands their results
to the model as ToolMessages; the rules below compose the answer FROM those results, so every number and runbook id is
real.  Profiles: ``competent`` cites the evidence, ``naive`` waves the change through, ``cautious`` demands a canary.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from foghorn.models.offline import Call, Script, all_, node, profile, tool_result


def tool_json(call: Call, name: str) -> dict[str, Any]:
    for m in call.tool_results:
        if isinstance(m, ToolMessage) and m.name == name:
            try:
                data = json.loads(str(m.content))
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _facts(call: Call) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in call.human.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def _grounded(call: Call) -> str:
    f = _facts(call)
    blast = tool_json(call, "blast_radius")
    books = tool_json(call, "search_runbooks").get("hits", [])
    past = tool_json(call, "recall_incidents").get("hits", [])
    changes = tool_json(call, "list_changes")
    parts: list[str] = []
    total = int(blast.get("total_affected_if_down", 0))
    tier0 = blast.get("tier0_exposed", [])
    parts.append(
        f"If {f.get('component', 'the component')} fails, {total} dependent component(s) are affected"
        + (f", including tier-0: {', '.join(tier0)}" if tier0 else "")
        + (" and the revenue path is exposed." if blast.get("revenue_path_exposed") else ".")
    )
    if books:
        b = books[0]
        parts.append(
            f"Runbook {b['id']} ('{b['title']}') applies; its first action is {b['actions'][0]['id']}."
            if b.get("actions")
            else f"Runbook {b['id']} ('{b['title']}') applies."
        )
    if past:
        p = past[0]
        parts.append(
            f"Closest past incident: {p['id']} ({p['title']}), resolved in {p['mttr_min']} min by {', '.join(p['resolution'])}."
        )
    if changes.get("count"):
        parts.append(f"{changes['count']} other change(s) landed in the last {int(changes['window_minutes'])} minutes.")
    if f.get("rollback_plan") and f["rollback_plan"] != "none":
        parts.append(f"Rollback plan on record: {f['rollback_plan']}.")
    else:
        parts.append("There is no rollback plan on record.")
    return " ".join(parts)


def answer_script() -> Script:
    return (
        Script()
        .when(
            "naive-waves-it-through",
            all_(node("answer_question"), profile("naive")),
            lambda c: f"This looks routine for {_facts(c).get('component', 'this component')}; I would approve it.",
        )
        .when(
            "cautious-demands-a-canary",
            all_(node("answer_question"), profile("cautious"), tool_result("blast_radius")),
            lambda c: _grounded(c) + " I recommend a canary rollout before full exposure.",
        )
        .when("competent-grounded", all_(node("answer_question"), tool_result("blast_radius")), _grounded)
        .when(
            "ungrounded",
            node("answer_question"),
            lambda c: (
                "I could not reach the grounding tools, so I cannot answer from evidence. Please decide with that in mind."
            ),
        )
    )
