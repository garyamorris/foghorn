# NOTE: deliberately NO ``from __future__ import annotations`` in this module. ``@entrypoint`` reads the annotation of its
# first parameter at decoration time to build the input schema (Studio's form) and the output/save channel types;
# postponed (string) annotations make pydantic fail with "LangGraphInput is not fully defined".
"""The ``postmortem_pipeline`` workflow, written as plain Python (Functional API).

    load_incident -> build_timeline -> draft -> review -> (revise <= 2) -> HUMAN REVIEW (interrupt) -> publish

* Studio draws this as ONE node (gotcha 28); the value is the task timeline (``updates`` entries ``{task: result}`` and
  the ``tasks`` stream), custom events and token streaming of the summary. The input is ONE TypedDict parameter so the
  Studio form is an object, not a bare array.
* ``previous`` carries the revision history across runs on the same thread (one thread per incident); the value
  returned to the caller differs from what is saved (``entrypoint.final``).
* Everything with a side effect (``publish``, lifecycle events) happens AFTER the interrupt and is idempotent: on resume
  this function runs again from line 1 while finished tasks are replayed from the checkpoint.
"""

from typing import Any

from langgraph.config import get_config
from langgraph.func import entrypoint
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.contracts.inputs import Artifact, GraphResult
from foghorn.contracts.interrupts import Decision, build_interrupt
from foghorn.flows.postmortem_pipeline.checks import check_postmortem
from foghorn.flows.postmortem_pipeline.context import PostmortemContext
from foghorn.flows.postmortem_pipeline.models import (
    EDITABLE_FIELDS,
    MAX_REVISIONS,
    Memo,
    Postmortem,
    PostmortemOutput,
    PostmortemRunInput,
    RevisionRecord,
)
from foghorn.flows.postmortem_pipeline.render import render_markdown
from foghorn.flows.postmortem_pipeline.streams import emit_safe
from foghorn.flows.postmortem_pipeline.tasks import build_timeline, draft, load_incident, publish, review


def apply_edits(pm: dict[str, Any], edits: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge a reviewer's ``Decision.edits`` into the draft. Each key is validated on its own; bad ones are reported, not fatal."""
    out, applied, rejected = dict(pm), [], []
    for k, v in edits.items():
        if k not in EDITABLE_FIELDS:
            rejected.append(k)
            continue
        try:
            out = Postmortem.model_validate({**out, k: v}).model_dump(mode="json")
            applied.append(k)
        except ValueError:
            rejected.append(k)
    return out, applied, rejected


def _thread_id() -> str | None:
    try:
        return str(get_config()["configurable"].get("thread_id"))
    except (RuntimeError, KeyError):
        return None


def _result(
    *, status: str, summary: str, pm: dict[str, Any], markdown: str, ref: str | None, metrics: dict[str, float]
) -> GraphResult:
    return GraphResult(
        graph_id="postmortem_pipeline",
        status=status,  # type: ignore[typeddict-item]
        event_id=None,
        correlation_id=None,
        summary=summary,
        artifacts=[
            Artifact(kind="report", name=f"postmortem-{pm['incident_id']}-r{pm['revision']}.md", ref=ref, data=markdown)
        ],
        metrics=metrics,
        child_threads=[],
    )


async def postmortem_pipeline(
    inp: PostmortemRunInput, *, previous: Memo | None, runtime: Runtime[PostmortemContext]
) -> entrypoint.final[PostmortemOutput, Memo]:
    ctx = runtime.context or PostmortemContext()
    incident_id = inp["incident_id"]
    thread_id = _thread_id()
    memo: Memo = previous or {"incident_id": incident_id, "revisions": [], "last_published": None, "feedback": []}
    revision = len(memo["revisions"]) + 1
    max_rounds = max(0, min(int(ctx.max_revisions), MAX_REVISIONS))
    emit_safe(
        "progress",
        f"pm:{incident_id}:r{revision}:start",
        agent="postmortem_pipeline",
        label=f"postmortem revision {revision}",
        pct=2,
    )

    incident = await load_incident(dict(inp))
    facts = await build_timeline({**incident, "feedback": memo["feedback"]})

    # draft -> review -> revise, bounded: the loop cannot exceed MAX_REVISIONS even if the context asks for more
    pm = await draft(facts, {"round": 0, "revision": revision, "issues": [], "prompt": ctx.draft_prompt})
    verdict = await review(facts, pm, {"round": 0, "prompt": ctx.review_prompt})
    rounds = 0
    while not verdict["ok"] and rounds < max_rounds:
        rounds += 1
        todo = [i for i in verdict["issues"] if i["severity"] == "blocker"]
        pm = await draft(facts, {"round": rounds, "revision": revision, "issues": todo, "prompt": ctx.draft_prompt})
        verdict = await review(facts, pm, {"round": rounds, "prompt": ctx.review_prompt})
    open_issues = verdict["issues"]
    markdown = render_markdown(pm, open_issues=open_issues)
    emit_safe(
        "note",
        f"pm:{incident_id}:r{revision}:reviewed",
        agent="postmortem_pipeline",
        text=verdict["summary"] or "review complete",
    )

    # ---- human review. Everything above is replayed from checkpoints on resume; everything below runs once per resume
    approval_id = f"pm_{incident_id}_r{revision}"
    n_blockers = len(blockers_of(open_issues))
    summary_line = (
        f"Draft revision {revision} for {incident_id}: {len(pm['action_items'])} action items, "
        f"{rounds} revise round(s), {n_blockers} open blocking issue(s)."
    )
    emit_event(
        "approval.requested",
        idempotency_key=f"approval.requested:{approval_id}",
        component=pm.get("component"),
        payload={
            "approval_id": approval_id,
            "kind": "edit",
            "thread_id": thread_id,
            "title": f"Review postmortem {incident_id}",
            "summary": summary_line,
            "risk": "low",
            "incident_id": incident_id,
        },
        scenario_id=pm.get("scenario_id"),
    )
    raw = interrupt(
        build_interrupt(
            "edit",
            approval_id=approval_id,
            title=f"Review postmortem {incident_id}",
            summary=summary_line,
            risk="low",
            incident_id=incident_id,
            evidence_refs=[e["ref"] for e in pm["timeline"] if e.get("ref")][:8],
            proposed={"postmortem": pm, "markdown": markdown, "open_issues": open_issues, "review_rounds": rounds},
            options=["approve", "edit", "reject"],
        ),
        response_schema=Decision,
    )
    decision = raw if isinstance(raw, Decision) else Decision.model_validate(raw)
    emit_event(
        "approval.decided",
        idempotency_key=f"approval.decided:{approval_id}",
        component=pm.get("component"),
        payload={
            "approval_id": approval_id,
            "decision": decision.decision,
            "decided_by": decision.decided_by,
            "thread_id": thread_id,
            "incident_id": incident_id,
        },
        scenario_id=pm.get("scenario_id"),
    )

    edited: list[str] = []
    ignored: list[str] = []
    status = "ok"
    record_status = "published"
    publication: dict[str, Any] | None = None
    if decision.decision in ("approve", "edit"):
        if decision.decision == "edit" and decision.edits:
            pm, edited, ignored = apply_edits(pm, decision.edits)
        pm["status"] = "edited" if edited else "approved"
        open_issues = [i.model_dump(mode="json") for i in check_postmortem(pm, facts)]
        markdown = render_markdown(pm, open_issues=open_issues)
        history = [*memo["revisions"], _record(revision, "published", decision, rounds, open_issues, pm)]
        publication = await publish(pm, {"history": history, "decided_by": decision.decided_by, "thread_id": thread_id})
        summary = f"Postmortem {incident_id} revision {revision} published ({pm['status']} by {decision.decided_by})."
        new_memo: Memo = {"incident_id": incident_id, "revisions": history, "last_published": pm, "feedback": []}
    else:  # reject (or an unsupported "ask"): nothing is published; the note steers the next draft on this thread
        status, record_status = "skipped", "rejected" if decision.decision == "reject" else "skipped"
        note = decision.note or decision.question or "no reason given"
        summary = f"Postmortem {incident_id} revision {revision} not published ({decision.decision} by {decision.decided_by}): {note}"
        history = [*memo["revisions"], _record(revision, record_status, decision, rounds, open_issues, pm)]
        new_memo = {
            "incident_id": incident_id,
            "revisions": history,
            "last_published": memo["last_published"],
            "feedback": [note],
        }

    metrics = {
        "revision": float(revision),
        "review_rounds": float(rounds),
        "open_blockers": float(len(blockers_of(open_issues))),
        "open_issues": float(len(open_issues)),
        "timeline_entries": float(len(pm["timeline"])),
        "action_items": float(len(pm["action_items"])),
        "owned_action_items": float(sum(1 for a in pm["action_items"] if a.get("owner"))),
        "edited_fields": float(len(edited)),
        "ignored_edits": float(len(ignored)),
    }
    ref = f"store:{'/'.join(publication['ns'])}/{publication['key']}" if publication else None
    output: PostmortemOutput = {
        "result": _result(status=status, summary=summary, pm=pm, markdown=markdown, ref=ref, metrics=metrics),
        "postmortem": pm,
        "markdown": markdown,
        "review": {**verdict, "issues": open_issues, "rounds": rounds},
        "revision": revision,
    }
    return entrypoint.final(value=output, save=new_memo)


def blockers_of(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in issues if i.get("severity") == "blocker"]


def _record(
    revision: int, status: str, decision: Decision, rounds: int, issues: list[dict[str, Any]], pm: dict[str, Any]
) -> RevisionRecord:
    return {
        "revision": revision,
        "status": status,  # type: ignore[typeddict-item]
        "decided_by": decision.decided_by,
        "note": decision.note,
        "review_rounds": rounds,
        "open_issues": len(issues),
        "title": pm["title"],
    }


__all__ = ["PostmortemContext", "apply_edits", "postmortem_pipeline"]
