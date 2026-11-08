"""Node functions of ``change_gate``: assess_risk, policy_gate, clarify, break_glass, request_approval, edit_change,
revise_change, answer_question, four_eyes, reviewer, collect_reviews, apply_change, finish.

Every ``interrupt()`` re-runs its node from line 1 on resume (docs/design/02 gotcha 14): everything before it is
idempotent (deterministic ids, ``emit`` keys, lifecycle idempotency keys) and every side effect comes after the last
interrupt (``apply_change`` is its own node, guarded by ``claim_effect``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command, Send, interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit
from foghorn.contracts.events import Severity
from foghorn.contracts.inputs import GraphResult
from foghorn.contracts.interrupts import Answer, Decision, PolicyCheck, build_interrupt
from foghorn.data import api as data_api
from foghorn.data.access import ScenarioNotFound
from foghorn.data.util import to_epoch
from foghorn.flows.change_gate import policy as pol
from foghorn.flows.change_gate.context import GateContext
from foghorn.flows.change_gate.scripts import answer_script
from foghorn.flows.change_gate.state import GateState
from foghorn.flows.event_router import topology
from foghorn.memory.api import claim_effect
from foghorn.models.factory import get_chat_model
from foghorn.tools import api as tools_api
from foghorn.tools.context import ToolContext, ToolError

#: edits a reviewer may make (anything else is ignored and reported)
EDITABLE = ("description", "risk", "environment", "diff")
RISKS = ("low", "medium", "high", "critical")
CHALLENGES = ["ask", "edit", "reject"]  # gauntlet: each must be exercised once before an approval is accepted
SEVERITY: dict[str, Severity] = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}


def _ctx(runtime: Runtime[GateContext]) -> GateContext:
    return runtime.context or GateContext()


def _thread(config: RunnableConfig) -> str | None:
    tid = (config.get("configurable") or {}).get("thread_id")
    return str(tid) if tid else None


def _as_decision(raw: Any) -> Decision:
    return raw if isinstance(raw, Decision) else Decision.model_validate(raw)


def _as_answer(raw: Any) -> Answer:
    return raw if isinstance(raw, Answer) else Answer.model_validate(raw)


def _requested(
    *,
    aid: str,
    kind: str,
    title: str,
    summary: str,
    risk: str,
    state: GateState,
    config: RunnableConfig,
    payload: dict[str, Any],
) -> None:
    """approval.requested (with thread_id) BEFORE the interrupt; idempotent on the approval id."""
    emit_event(
        "approval.requested",
        idempotency_key=f"approval.requested:{aid}",
        component=(state.get("change") or {}).get("component"),
        severity=SEVERITY.get(risk, "medium"),
        payload={
            "approval_id": aid,
            "kind": kind,
            "thread_id": _thread(config),
            "title": title,
            "summary": summary,
            "risk": risk,
            "incident_id": state.get("incident_id"),
            "graph_id": "change_gate",
            "change_id": (state.get("change") or {}).get("id"),
            "approvers_required": payload["policy"]["approvers_required"],
            "roles": payload["policy"]["roles"],
            "options": payload["options"],
            "payload": payload,
        },
        correlation_id=state.get("incident_id"),
        scenario_id=state.get("scenario_id"),
        occurred_at=state.get("requested_at"),
    )
    emit("note", f"awaiting:{aid}", agent="gate", text=f"waiting for {kind}: {title}")


def _decided(
    *, aid: str, kind: str, decision: str, by: str, note: str, state: GateState, config: RunnableConfig
) -> None:
    """approval.decided AFTER the interrupt returns; idempotent on the approval id."""
    emit_event(
        "approval.decided",
        idempotency_key=f"approval.decided:{aid}",
        component=(state.get("change") or {}).get("component"),
        payload={
            "approval_id": aid,
            "kind": kind,
            "decision": decision,
            "decided_by": by,
            "note": note,
            "thread_id": _thread(config),
            "graph_id": "change_gate",
        },
        correlation_id=state.get("incident_id"),
        scenario_id=state.get("scenario_id"),
        occurred_at=state.get("requested_at"),
    )
    emit("decision", f"decided:{aid}", agent="gate", what=f"{kind}: {decision} by {by}", why=note[:120] or "no note")


def _card(state: GateState) -> dict[str, Any]:
    c, r = state["change"], state["risk"]
    return {
        "change": c,
        "risk": {k: r[k] for k in ("score", "tier", "factors", "rollback_plan")},
        "mode": state.get("mode", "standard"),
        "qa": state.get("qa") or [],
        "edits": state.get("edits", 0),
        "revisions": state.get("revisions", 0),
        "hint": state.get("hint"),
    }


# ------------------------------------------------------------------------------------------------------ assess_risk
def assess_risk(state: GateState, runtime: Runtime[GateContext]) -> dict[str, Any]:
    ctx = _ctx(runtime)
    change = dict(state["change"])
    first = "original" not in state
    mode = state.get("mode") or "standard"
    requested_at = state.get("requested_at") or ctx.as_of or pol.DEFAULT_REQUESTED_AT
    scenario = state.get("scenario_id") or ctx.scenario_id
    freeze = pol.freeze_window(to_epoch(requested_at)) is not None
    risk = pol.assess(change, requested_at=requested_at, mode=mode, in_freeze=freeze)
    emit(
        "metric",
        f"risk:{change['id']}:{state.get('turn', 0)}",
        agent="assess_risk",
        name="risk_score",
        value=risk["score"],
        unit="points",
    )
    emit(
        "note",
        f"risk-note:{change['id']}:{state.get('turn', 0)}",
        agent="assess_risk",
        text=f"risk {risk['tier']} ({risk['score']}): " + "; ".join(risk["factors"][:4]),
    )
    update: dict[str, Any] = {
        "risk": risk,
        "mode": mode,
        "requested_at": requested_at,
        "scenario_id": scenario,
        "status": "pending",
    }
    if first:
        update["original"] = change
        update["remaining"] = list(CHALLENGES) if mode == "gauntlet" else []
    return update


# ----------------------------------------------------------------------------------------------------- policy_gate
def policy_gate(
    state: GateState, runtime: Runtime[GateContext]
) -> Command[Literal["finish", "clarify", "break_glass", "apply_change", "request_approval"]]:
    ctx = _ctx(runtime)
    change, risk = state["change"], state["risk"]
    p = pol.evaluate(
        change,
        risk,
        requested_at=state["requested_at"],
        mode=state.get("mode", "standard"),
        approval_policy=ctx.approval_policy,
        clarified=bool(state.get("clarified")),
    )
    for chk in p["checks"]:
        emit(
            "policy.check",
            f"policy:{change['id']}:{chk['id']}:{state.get('turn', 0)}",
            agent="policy_gate",
            rule=chk["id"],
            result=chk["status"],
            text=chk["text"],
        )
    update: dict[str, Any] = {"policy": p}
    if p["blocked"]:
        return Command(update={**update, "status": "blocked", "hint": p["block_reason"]}, goto="finish")
    if p["clarify"]:
        return Command(update=update, goto="clarify")
    if p["break_glass"] and not state.get("break_glass_done"):
        return Command(update=update, goto="break_glass")
    if p["path"] == "auto":
        decision = {
            "turn": state.get("turn", 0),
            "kind": "policy",
            "decision": "auto_approve",
            "decided_by": "policy",
            "note": f"{risk['tier']} risk needs no approver (POL-005)",
        }
        return Command(update={**update, "decisions": [decision]}, goto="apply_change")
    return Command(update=update, goto="request_approval")


# ---------------------------------------------------------------------------------------------------------- clarify
def clarify(state: GateState, config: RunnableConfig) -> Command[Literal["assess_risk"]]:
    """A ``question`` interrupt: the graph asks the requester for what it needs (the rollback plan)."""
    change, risk, turn = state["change"], state["risk"], state.get("turn", 0)
    aid = f"qst_{change['id']}_{turn}"
    gauntlet = state.get("mode") == "gauntlet"
    title = "What is the rollback plan?" if not gauntlet else "Gauntlet 1: answer a question from the gate"
    summary = (
        f"{risk['tier']}-risk {change['kind']} on {change['component']} has no rollback plan on record. "
        "Describe how to undo it (the answer is attached to the change and the risk is re-assessed)."
    )
    payload = build_interrupt(
        "question",
        approval_id=aid,
        title=title,
        summary=summary,
        risk=risk["tier"],
        incident_id=state.get("incident_id"),
        proposed={"question": "What is the rollback plan?", **_card(state)},
        policy_checks=[PolicyCheck(**c) for c in state["policy"]["checks"]],
        approvers_required=0,
    )
    _requested(
        aid=aid,
        kind="question",
        title=title,
        summary=summary,
        risk=risk["tier"],
        state=state,
        config=config,
        payload=payload,
    )
    answer = _as_answer(interrupt(payload, response_schema=Answer))
    _decided(
        aid=aid,
        kind="question",
        decision="answered",
        by=answer.decided_by,
        note=answer.answer,
        state=state,
        config=config,
    )
    diff = dict(change.get("diff") or {})
    if answer.answer.strip():
        diff["rollback_plan"] = answer.answer.strip()
    new_change = {**change, "diff": diff}
    rec = {
        "turn": turn,
        "kind": "question",
        "decision": "answered",
        "decided_by": answer.decided_by,
        "note": answer.answer,
        "approval_id": aid,
    }
    return Command(
        update={"change": new_change, "clarified": True, "turn": turn + 1, "decisions": [rec]}, goto="assess_risk"
    )


# ------------------------------------------------------------------------------------------------------ break_glass
def break_glass(state: GateState, config: RunnableConfig) -> Command[Literal["request_approval", "finish"]]:
    change, risk, turn = state["change"], state["risk"], state.get("turn", 0)
    aid = f"bg_{change['id']}_{turn}"
    title = f"Break-glass: {change['kind']} on {change['component']} during the change freeze"
    min_chars = int((pol.policies().get("POL-003", {}).get("break_glass") or {}).get("reason_min_chars", 20))
    summary = f"POL-003: this needs break-glass. Approve with a written reason of at least {min_chars} characters."
    payload = build_interrupt(
        "break_glass",
        approval_id=aid,
        title=title,
        summary=summary,
        risk=risk["tier"],
        reversible=False,
        blast_radius=risk["blast_radius"][:12],
        policy_checks=[PolicyCheck(**c) for c in state["policy"]["checks"]],
        approvers_required=2,
        roles=["service_oncall", "incident_commander"],
        incident_id=state.get("incident_id"),
        proposed=_card(state),
        options=["approve", "reject"],
    )
    _requested(
        aid=aid,
        kind="break_glass",
        title=title,
        summary=summary,
        risk=risk["tier"],
        state=state,
        config=config,
        payload=payload,
    )
    d = _as_decision(interrupt(payload, response_schema=Decision))
    _decided(aid=aid, kind="break_glass", decision=d.decision, by=d.decided_by, note=d.note, state=state, config=config)
    rec = {
        "turn": turn,
        "kind": "break_glass",
        "decision": d.decision,
        "decided_by": d.decided_by,
        "note": d.note,
        "approval_id": aid,
    }
    if d.decision == "approve" and len(d.note.strip()) >= min_chars:
        return Command(update={"break_glass_done": True, "turn": turn + 1, "decisions": [rec]}, goto="request_approval")
    why = "break-glass reason too short" if d.decision == "approve" else "break-glass refused"
    return Command(
        update={"status": "blocked", "hint": why, "turn": turn + 1, "decisions": [{**rec, "note": f"{why}: {d.note}"}]},
        goto="finish",
    )


# ------------------------------------------------------------------------------------------------- request_approval
def request_approval(
    state: GateState, config: RunnableConfig, runtime: Runtime[GateContext]
) -> Command[
    Literal["apply_change", "four_eyes", "edit_change", "revise_change", "answer_question", "request_approval"]
]:
    ctx = _ctx(runtime)
    change, risk, p = state["change"], state["risk"], state["policy"]
    turn = state.get("turn", 0)
    approved_by = list(state.get("approved_by") or [])
    aid = f"apr_{change['id']}_{turn}"
    remaining = list(state.get("remaining") or [])
    can_ask = int(state.get("questions", 0)) < ctx.max_questions
    options = ["approve", "edit", "reject"] + (["ask"] if can_ask else [])
    seq = ""
    if p["path"] == "approval" and p["approvers_required"] > 1:
        seq = f" (approver {len(approved_by) + 1} of {p['approvers_required']})"
    gauntlet = state.get("mode") == "gauntlet"
    title = (
        f"Gauntlet - still to complete: {', '.join(remaining)}"
        if gauntlet and remaining
        else f"Approve {change['kind']} on {change['component']}?{seq}"
    )
    summary = (
        f"{change['description']} | risk {risk['tier']} ({risk['score']}) | requested by {change['requested_by']}"
        + (f" | {state['hint']}" if state.get("hint") else "")
    )
    payload = build_interrupt(
        "approval",
        approval_id=aid,
        title=title,
        summary=summary,
        risk=risk["tier"],
        reversible=change["kind"] != "migration",
        blast_radius=risk["blast_radius"][:12],
        policy_checks=[PolicyCheck(**c) for c in p["checks"]],
        approvers_required=p["approvers_required"],
        roles=p["roles"],
        incident_id=state.get("incident_id"),
        proposed={**_card(state), "remaining_challenges": remaining},
        options=options,
    )
    _requested(
        aid=aid,
        kind="approval",
        title=title,
        summary=summary,
        risk=risk["tier"],
        state=state,
        config=config,
        payload=payload,
    )
    d = _as_decision(interrupt(payload, response_schema=Decision))
    _decided(aid=aid, kind="approval", decision=d.decision, by=d.decided_by, note=d.note, state=state, config=config)
    rec = {
        "turn": turn,
        "kind": "approval",
        "decision": d.decision,
        "decided_by": d.decided_by,
        "note": d.note,
        "approval_id": aid,
    }
    base: dict[str, Any] = {"turn": turn + 1, "last_decision": d.model_dump(), "hint": None, "decisions": [rec]}
    if d.decision in remaining:
        base["remaining"] = [x for x in remaining if x != d.decision]

    if d.decision == "ask":
        if not can_ask:
            return Command(update={**base, "hint": "question budget used: decide now"}, goto="request_approval")
        return Command(update={**base, "questions": int(state.get("questions", 0)) + 1}, goto="answer_question")
    if d.decision == "edit":
        if int(state.get("edits", 0)) >= ctx.max_edits:
            return Command(
                update={**base, "hint": f"edit budget ({ctx.max_edits}) used: approve or reject"},
                goto="request_approval",
            )
        return Command(update=base, goto="edit_change")
    if d.decision == "reject":
        return Command(update=base, goto="revise_change")
    # approve
    if remaining:
        left = ", ".join(remaining)
        return Command(
            update={**base, "hint": f"Gauntlet: not yet - still to complete: {left}"}, goto="request_approval"
        )
    if d.decided_by != "operator" and d.decided_by in approved_by:
        return Command(
            update={**base, "hint": f"{d.decided_by} already approved: a different approver must sign"},
            goto="request_approval",
        )
    approved_by.append(d.decided_by)
    base["approved_by"] = approved_by
    if p["path"] == "four_eyes":
        return Command(update=base, goto="four_eyes")
    if len(approved_by) < int(p["approvers_required"]):
        return Command(update=base, goto="request_approval")
    return Command(update=base, goto="apply_change")


# ----------------------------------------------------------------------------------------------------- edit_change
def _apply_edits(change: dict[str, Any], edits: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    new = dict(change)
    applied: list[str] = []
    ignored: list[str] = []
    for k, v in edits.items():
        if k not in EDITABLE or (k == "risk" and v not in RISKS):
            ignored.append(k)
        elif k == "diff" and isinstance(v, dict):
            new["diff"] = {**(change.get("diff") or {}), **v}
            applied.append(k)
        elif k != "diff":
            new[k] = v
            applied.append(k)
        else:
            ignored.append(k)
    return new, applied, ignored


def edit_change(state: GateState, config: RunnableConfig) -> Command[Literal["assess_risk"]]:
    """The edit path.  Edits that came with the approval decision are applied directly; otherwise an ``edit``
    interrupt asks for them.  Either way the change is re-assessed."""
    change, turn = state["change"], state.get("turn", 0)
    last = state.get("last_decision") or {}
    edits = dict(last.get("edits") or {})
    by = str(last.get("decided_by") or "operator")
    rec_extra: list[dict[str, Any]] = []
    if not edits:
        aid = f"edt_{change['id']}_{turn}"
        title = f"Edit {change['kind']} on {change['component']}"
        summary = f"Change any of {', '.join(EDITABLE)}; the change is re-assessed and comes back for approval."
        payload = build_interrupt(
            "edit",
            approval_id=aid,
            title=title,
            summary=summary,
            risk=state["risk"]["tier"],
            policy_checks=[PolicyCheck(**c) for c in state["policy"]["checks"]],
            approvers_required=0,
            incident_id=state.get("incident_id"),
            proposed={**_card(state), "editable": list(EDITABLE)},
            options=["edit", "reject"],
        )
        _requested(
            aid=aid,
            kind="edit",
            title=title,
            summary=summary,
            risk=state["risk"]["tier"],
            state=state,
            config=config,
            payload=payload,
        )
        d = _as_decision(interrupt(payload, response_schema=Decision))
        _decided(aid=aid, kind="edit", decision=d.decision, by=d.decided_by, note=d.note, state=state, config=config)
        rec_extra.append(
            {
                "turn": turn,
                "kind": "edit",
                "decision": d.decision,
                "decided_by": d.decided_by,
                "note": d.note,
                "approval_id": aid,
            }
        )
        edits, by = (dict(d.edits) if d.decision == "edit" else {}), d.decided_by
        turn += 1
    new, applied, ignored = _apply_edits(change, edits)
    log = {
        "turn": turn,
        "by": by,
        "applied": applied,
        "ignored": ignored,
        "before": {k: change.get(k) for k in applied},
        "after": {k: new.get(k) for k in applied},
    }
    emit(
        "note",
        f"edit:{change['id']}:{turn}",
        agent="edit_change",
        text=f"{by} edited {', '.join(applied) or 'nothing'}"
        + (f" (ignored: {', '.join(ignored)})" if ignored else ""),
    )
    return Command(
        update={
            "change": new,
            "edits": int(state.get("edits", 0)) + 1,
            "turn": turn,
            "edit_log": [log],
            "decisions": rec_extra,
            "approved_by": [],
        },
        goto="assess_risk",
    )


# --------------------------------------------------------------------------------------------------- revise_change
def revise_change(state: GateState, runtime: Runtime[GateContext]) -> Command[Literal["assess_risk", "finish"]]:
    """The reject path: a bounded revise loop.  Each rejection earns a safer variant (canary first, one level off the
    declared risk) that is re-assessed and put back to the reviewers; the next rejection after the bound is final."""
    ctx = _ctx(runtime)
    change = state["change"]
    last = state.get("last_decision") or {}
    note = str(last.get("note") or "")
    revisions = int(state.get("revisions", 0)) + 1
    if revisions > ctx.max_revisions:
        return Command(
            update={
                "status": "rejected",
                "revisions": revisions,
                "hint": f"rejected {revisions} times: final ({note or 'no note'})",
            },
            goto="finish",
        )
    diff = dict(change.get("diff") or {})
    diff["rollout"] = "canary 10%"
    diff["revision_note"] = note[:200]
    declared = str(change.get("risk") or "medium")
    lowered = RISKS[max(0, RISKS.index(declared) - 1)] if declared in RISKS else declared
    revised = {
        **change,
        "risk": lowered,
        "diff": diff,
        "description": f"{change['description']} [revision {revisions}: canary 10% first]",
    }
    emit(
        "note",
        f"revise:{change['id']}:{revisions}",
        agent="revise_change",
        text=f"rejected ({note or 'no note'}): revision {revisions} adds a canary and lowers declared risk {declared} -> {lowered}",
    )
    return Command(
        update={
            "change": revised,
            "revisions": revisions,
            "hint": f"revision {revisions}: {note or 'rejected'}",
            "approved_by": [],
        },
        goto="assess_risk",
    )


# ------------------------------------------------------------------------------------------------ answer_question
def _grounding(state: GateState, question: str) -> dict[str, Any]:
    """Run the grounding tools (blocking SQLite/lexical work: called in a thread)."""
    change = state["change"]
    comp = change["component"]
    scenario = state.get("scenario_id")
    ctx: ToolContext | None = None
    if scenario:
        try:
            data_api.get_scenario(scenario)
            ctx = ToolContext(scenario_id=scenario, as_of=state.get("requested_at"))
        except (ScenarioNotFound, KeyError):
            ctx = None
    out: dict[str, Any] = {}
    try:
        out["blast_radius"] = (
            tools_api.blast_radius(ctx, comp) if ctx and topology.known(comp) else topology.static_blast_radius(comp)
        )
    except (ToolError, ScenarioNotFound):
        out["blast_radius"] = topology.static_blast_radius(comp)
    query = f"{change['kind']} {comp} {question}"
    books = tools_api.search_runbooks(query, k=2)["hits"]
    out["search_runbooks"] = {
        "hits": [
            {
                "id": b["id"],
                "title": b["title"],
                "actions": [{"id": a["id"], "risk": a["risk"]} for a in b.get("actions", [])[:3]],
            }
            for b in books
        ]
    }
    past = tools_api.recall_incidents(query, k=2)["hits"]
    out["recall_incidents"] = {
        "hits": [
            {"id": p["id"], "title": p["title"], "mttr_min": p["mttr_min"], "resolution": p["resolution"]} for p in past
        ]
    }
    if ctx is not None:
        try:
            ch = tools_api.list_changes(ctx, window_minutes=240)
            out["list_changes"] = {"count": ch["count"], "window_minutes": ch["window_minutes"]}
        except (ToolError, ScenarioNotFound):
            pass
    return out


async def answer_question(state: GateState, runtime: Runtime[GateContext]) -> dict[str, Any]:
    """The ask path: a ``smart`` model answers the reviewer, grounded in tool results; the approval card comes back."""
    ctx = _ctx(runtime)
    last = state.get("last_decision") or {}
    question = str(last.get("question") or last.get("note") or "What could go wrong?").strip()
    change = state["change"]
    tools = await asyncio.to_thread(_grounding, state, question)
    calls = [{"name": n, "args": {"query": question}, "id": f"call_{n}", "type": "tool_call"} for n in tools]
    messages: list[Any] = [
        SystemMessage(ctx.answer_prompt),
        HumanMessage(
            f"question={question}\nchange_id={change['id']}\nkind={change['kind']}\ncomponent={change['component']}\n"
            f"risk={state['risk']['tier']}\nrollback_plan={(change.get('diff') or {}).get('rollback_plan', 'none')}"
        ),
        AIMessage(content="", tool_calls=calls),
        *[
            ToolMessage(content=json.dumps(v, default=str)[:6000], tool_call_id=f"call_{n}", name=n)
            for n, v in tools.items()
        ],
    ]
    llm = get_chat_model("smart", offline_script=answer_script())
    reply = await llm.ainvoke(messages)
    answer = str(reply.content).strip()
    qa = {
        "turn": state.get("turn", 0),
        "question": question,
        "asked_by": last.get("decided_by", "operator"),
        "answer": answer,
        "sources": sorted(tools),
    }
    emit("note", f"answer:{change['id']}:{state.get('turn', 0)}", agent="answer_question", text=answer[:300])
    return {"qa": [qa], "hint": f"answer to your question: {answer[:200]}"}


# ------------------------------------------------------------------------------------------------------- four-eyes
def four_eyes(state: GateState) -> Command[Literal["reviewer"]]:
    """Fan out ONE ``reviewer`` task per role: both interrupt at the same time (resume with an {interrupt_id: value} map)."""
    p = state["policy"]
    turn = state.get("turn", 0)
    return Command(
        goto=[
            Send(
                "reviewer",
                {
                    "role": role,
                    "turn": turn,
                    "change": state["change"],
                    "risk": state["risk"],
                    "policy": p,
                    "incident_id": state.get("incident_id"),
                    "scenario_id": state.get("scenario_id"),
                    "requested_at": state.get("requested_at"),
                    "card": _card(state),
                },
            )
            for role in p["roles"][:2]
        ]
    )


def reviewer(arg: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    change, risk, role, turn = arg["change"], arg["risk"], arg["role"], arg["turn"]
    aid = f"fe_{change['id']}_{turn}_{role}"
    title = f"Four-eyes ({role}): {change['kind']} on {change['component']}"
    summary = f"Critical change {change['id']}: two independent reviewers must both approve. You are the {role}."
    payload = build_interrupt(
        "four_eyes",
        approval_id=aid,
        title=title,
        summary=summary,
        risk=risk["tier"],
        reversible=change["kind"] != "migration",
        blast_radius=risk["blast_radius"][:12],
        policy_checks=[PolicyCheck(**c) for c in arg["policy"]["checks"]],
        approvers_required=2,
        roles=[role],
        incident_id=arg.get("incident_id"),
        proposed=arg["card"],
        options=["approve", "reject"],
    )
    st: GateState = {
        "change": change,
        "incident_id": arg.get("incident_id"),
        "scenario_id": arg.get("scenario_id"),
        "requested_at": str(arg.get("requested_at") or ""),
    }
    _requested(
        aid=aid,
        kind="four_eyes",
        title=title,
        summary=summary,
        risk=risk["tier"],
        state=st,
        config=config,
        payload=payload,
    )
    d = _as_decision(interrupt(payload, response_schema=Decision))
    _decided(aid=aid, kind="four_eyes", decision=d.decision, by=d.decided_by, note=d.note, state=st, config=config)
    return {
        "reviews": [
            {
                "approval_id": aid,
                "turn": turn,
                "role": role,
                "decision": d.decision,
                "decided_by": d.decided_by,
                "note": d.note,
            }
        ]
    }


def collect_reviews(state: GateState) -> Command[Literal["apply_change", "revise_change"]]:
    """Deferred fan-in: both reviewers approve (as two distinct explicit identities) or the change goes back."""
    turn = state.get("turn", 0)
    rv = [r for r in state.get("reviews", []) if r["turn"] == turn]
    names = [r["decided_by"] for r in rv if r["decided_by"] != "operator"]
    same_person = (
        len(names) == 2 and len(set(names)) == 1
    )  # explicit identities only; the default "operator" is unverified
    approved = len(rv) >= 2 and all(r["decision"] == "approve" for r in rv) and not same_person
    recs = [
        {
            "turn": turn,
            "kind": "four_eyes",
            "decision": r["decision"],
            "decided_by": r["decided_by"],
            "note": r["note"],
            "approval_id": r["approval_id"],
            "role": r["role"],
        }
        for r in rv
    ]
    if approved:
        return Command(
            update={"turn": turn + 1, "decisions": recs, "approved_by": [r["decided_by"] for r in rv]},
            goto="apply_change",
        )
    why = (
        "four-eyes needs two DIFFERENT reviewers"
        if same_person
        else "; ".join(f"{r['role']}: {r['decision']} {r['note']}".strip() for r in rv if r["decision"] != "approve")
    )
    last = {"decision": "reject", "decided_by": "four_eyes", "note": why}
    return Command(update={"turn": turn + 1, "decisions": recs, "last_decision": last}, goto="revise_change")


# ------------------------------------------------------------------------------------------------------ apply_change
async def apply_change(state: GateState, store: BaseStore) -> dict[str, Any]:
    """The only side effect (a simulation), strictly AFTER every interrupt, idempotent on the change id."""
    change = state["change"]
    claimed = await claim_effect(
        store, f"change:{change['id']}", {"component": change["component"], "kind": change["kind"]}
    )
    if not claimed:
        emit("note", f"apply:{change['id']}", agent="apply_change", text="already applied: skipped (idempotent)")
        return {"status": "duplicate", "applied": {"change_id": change["id"], "outcome": "already_applied"}}
    record = {
        "change_id": change["id"],
        "kind": change["kind"],
        "component": change["component"],
        "environment": change.get("environment", "prod"),
        "outcome": "applied",
        "t_virtual": state.get("requested_at"),
        "diff": change.get("diff") or {},
    }
    emit_event(
        "action.executed",
        idempotency_key=f"action.executed:change:{change['id']}",
        component=change["component"],
        payload={
            "incident_id": state.get("incident_id"),
            "action": f"{change['kind']}:{change['component']}",
            "target": change["component"],
            "outcome": "applied",
            "change_id": change["id"],
        },
        correlation_id=state.get("incident_id"),
        scenario_id=state.get("scenario_id"),
        occurred_at=state.get("requested_at"),
    )
    emit(
        "note",
        f"applied:{change['id']}",
        agent="apply_change",
        text=f"applied {change['kind']} {change['id']} on {change['component']} (simulation)",
    )
    return {"status": "applied", "applied": record}


# ----------------------------------------------------------------------------------------------------------- finish
def finish(state: GateState) -> dict[str, Any]:
    change, status = state["change"], state.get("status", "pending")
    risk, decisions = state.get("risk") or {}, state.get("decisions") or []
    summary = {
        "applied": f"applied {change['kind']} {change['id']} on {change['component']} after {len([d for d in decisions if d['kind'] != 'policy'])} human decision(s)",
        "duplicate": f"{change['id']} was already applied: nothing done",
        "rejected": f"{change['id']} rejected: {state.get('hint') or 'no approval'}",
        "blocked": f"{change['id']} blocked: {state.get('hint') or 'policy'}",
    }.get(status, f"{change['id']} ended as {status}")
    result: GraphResult = {
        "graph_id": "change_gate",
        "status": {"applied": "ok", "duplicate": "duplicate"}.get(status, "skipped"),  # type: ignore[typeddict-item]
        "event_id": None,
        "correlation_id": state.get("incident_id"),
        "summary": summary,
        "artifacts": [
            {
                "kind": "diff",
                "name": "change",
                "ref": change["id"],
                "data": {"original": state.get("original"), "final": change},
            },
            {"kind": "table", "name": "risk", "ref": None, "data": risk},
            {"kind": "table", "name": "policy", "ref": None, "data": state.get("policy") or {}},
            {"kind": "table", "name": "decisions", "ref": None, "data": decisions},
            {"kind": "table", "name": "qa", "ref": None, "data": state.get("qa") or []},
            {"kind": "table", "name": "edits", "ref": None, "data": state.get("edit_log") or []},
            {"kind": "table", "name": "reviews", "ref": None, "data": state.get("reviews") or []},
        ],
        "metrics": {
            "risk_score": float(risk.get("score", 0.0)),
            "human_decisions": float(len([d for d in decisions if d["kind"] != "policy"])),
            "edits": float(state.get("edits", 0)),
            "revisions": float(state.get("revisions", 0)),
            "questions": float(state.get("questions", 0)),
            "applied": 1.0 if status == "applied" else 0.0,
        },
        "child_threads": [],
    }
    return {"result": result, "status": status}
