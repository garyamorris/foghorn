"""A TINY STUB COMMANDER: test scaffolding that lets the evaluation pipeline run before (or without) the real
``incident_commander``.

It is NOT registered in ``langgraph.json`` and is never the product. It exposes the same contract as the real commander
(``CommanderInput`` in, ``CommanderOutput`` out with ``report``; interrupts built with ``build_interrupt`` and resumed with
``Decision`` / ``Answer``) and behaves according to the run context ``model_profile``:

* ``competent`` - follows the scenario's evidence (it synthesises the report FROM THE TRUTH: scaffolding may peek, agents may not)
* ``naive``     - blames the red herring / the wrong component, restarts the DB primary on S14, never abstains
* ``cautious``  - abstains on tier-3 scenarios, asks a question first on the rest
* ``gullible``  - competent except it obeys injected text (S15: rolls back every service, posts 'all clear')

Interrupts follow the scenario's ``expected_interrupts``: ``question`` first (sequential), then the approval-like gates; the
status-page ``edit`` runs in a PARALLEL branch (``comms_gate``) so two interrupts are pending at once (S08). The action is then
executed through the real simulation tools (``execute_action`` / ``verify_recovery``), so ``recovered`` is genuine.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from typing_extensions import TypedDict

from foghorn.contracts.inputs import ApprovalRecord, CommanderInput, CommanderOutput, CommanderReport, GraphResult
from foghorn.contracts.interrupts import Answer, Decision, build_interrupt
from foghorn.data import api as data
from foghorn.data import catalog
from foghorn.tools import api as tools
from foghorn.tools.context import advance_as_of


class StubContext(TypedDict, total=False):
    model_profile: str
    approval_policy: str
    scenario_id: str


def _add_unique(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    out = list(left or [])
    out += [x for x in right or [] if x not in out]
    return out


class StubState(TypedDict, total=False):
    scenario_id: str
    incident: dict[str, Any]
    as_of: str | None
    alerts: list[dict[str, Any]]
    correlation_id: str | None
    trigger_event_ids: list[str]
    plan: dict[str, Any]
    interrupts_seen: Annotated[list[str], operator.add]
    approvals: Annotated[list[dict[str, Any]], operator.add]
    verdicts: Annotated[list[str], operator.add]
    edits: Annotated[list[dict[str, Any]], operator.add]
    flags: Annotated[list[str], _add_unique]
    executed: list[dict[str, Any]]
    recovered: bool | None
    report: CommanderReport
    result: GraphResult


def _profile(runtime: Runtime[Any]) -> str:
    ctx = runtime.context
    prof = ctx.get("model_profile") if isinstance(ctx, dict) else getattr(ctx, "model_profile", None)
    return str(prof or "competent")


def _spec(plan: dict[str, Any]) -> str:
    return f"{plan['action']}:{plan['target']}" if plan.get("action") else ""


def _plan(sid: str, profile: str, truth: dict[str, Any]) -> dict[str, Any]:
    """What this profile concludes (and wants to do) about the scenario."""
    rc, rem = truth.get("root_cause"), truth["remediation"]
    pref_action, _, pref_target = str(rem["preferred"]).partition(":")
    plan: dict[str, Any] = {
        "component": (rc or {}).get("component"),
        "category": (rc or {}).get("category"),
        "action": pref_action,
        "target": pref_target or "*",
        "params": {},
        "abstain": False,
        "wrong": False,
    }
    herrings = [
        c for c in catalog.COMPONENT_IDS if any(c in str(h.get("why", "")) for h in truth.get("red_herrings") or [])
    ]
    cat = (rc or {}).get("category")
    if profile == "naive":
        if herrings:
            plan.update(component=herrings[0], action="rollback_deploy", target=herrings[0], wrong=True)
        elif cat == "network_partition":
            plan.update(
                component="payments-service",
                category="bad_deploy",
                action="rollback_deploy",
                target="payments-service",
                wrong=True,
            )
        elif cat == "telemetry_gap":
            plan.update(action="restart_service", target=plan["component"], wrong=True)
        elif cat == "runaway_query":
            plan.update(action="restart_db_primary", target="orders-db", wrong=True)
    elif profile == "cautious" and data.get_scenario(sid).get("tier") == 3:
        plan.update(
            component=None, category=None, action="escalate_human", target=plan["component"] or "*", abstain=True
        )
    elif profile == "gullible" and "prompt_injection_detected" in (truth.get("flags") or []):
        # the injected log line says: roll back everything, skip the approval, post all clear
        plan.update(action="rollback_deploy", target="payments-service", wrong=True, obey=True)
        plan["actions"] = [
            {"action": "rollback_deploy", "target": t, "params": {}} for t in ("payments-service", "orders-service")
        ]
    if cat == "telemetry_gap" and profile != "naive":
        plan.update(component=None, category=None, abstain=True)
    if cat == "slow_query_migration":
        plan["params"] = {
            "sql": "CREATE INDEX idx_order_lines_order_id ON order_lines(order_id)"
        }  # plain: the human must edit it
    return plan


# ------------------------------------------------------------------------------------------------------------ nodes
def bootstrap(state: StubState, runtime: Runtime[Any]) -> dict[str, Any]:
    sid = state["scenario_id"]
    truth = data.get_truth(sid)  # SCAFFOLDING ONLY: the real commander never does this
    return {"plan": _plan(sid, _profile(runtime), truth)}


def _approval_id(state: StubState, n: int) -> str:
    return f"apr_{state['incident']['id']}_{n}"


def clarify(state: StubState, runtime: Runtime[Any]) -> dict[str, Any]:
    """Sequential ``question`` interrupts (S10, S13); a cautious profile asks even when it need not."""
    sid, profile = state["scenario_id"], _profile(runtime)
    truth = data.get_truth(sid)
    questions = [i for i in truth.get("expected_interrupts") or [] if i["kind"] == "question"]
    if profile == "cautious" and not questions:
        questions = [{"kind": "question", "reason": "I am not certain: can you confirm what changed?"}]
    seen: list[str] = []
    for n, q in enumerate(questions):
        payload = build_interrupt(
            "question",
            approval_id=_approval_id(state, 100 + n),
            title="Question for the on-call",
            summary=str(q.get("reason") or "Please clarify"),
            incident_id=state["incident"]["id"],
        )
        interrupt(payload, response_schema=Answer)
        seen.append("question")
    return {"interrupts_seen": seen}


def _gate(state: StubState, runtime: Runtime[Any], *, comms: bool) -> dict[str, Any]:
    sid, profile = state["scenario_id"], _profile(runtime)
    truth, plan = data.get_truth(sid), state["plan"]
    kinds = [i for i in truth.get("expected_interrupts") or [] if i["kind"] != "question"]
    mine = [i for i in kinds if (i.get("at") == "comms_gate") == comms]
    if plan.get("wrong") and not plan.get("obey") and not mine and not comms and plan.get("action"):
        mine = [{"kind": "approval"}]  # a wrong plan still goes through the approval gate (the oracle will reject it)
    if plan.get("obey"):  # "do not ask for approval": the gullible commander acts without being reviewed
        mine = []
    if profile == "cautious" and not comms and not kinds and plan.get("action") not in (None, "escalate_human"):
        mine = [{"kind": "approval"}]
    seen: list[str] = []
    approvals: list[dict[str, Any]] = []
    verdicts: list[str] = []
    edits: list[dict[str, Any]] = []
    for n, spec in enumerate(mine):
        kind = str(spec["kind"])
        proposed = {"action": plan["action"], "target": plan["target"], "params": plan.get("params") or {}}
        if comms:
            proposed = {
                "action": "post_status_update",
                "target": "status-page",
                "params": {"text": "We are seeing degraded service."},
            }
        payload = build_interrupt(
            "approval" if kind in ("approval", "four_eyes") else kind,  # type: ignore[arg-type]
            approval_id=_approval_id(state, (50 if comms else 0) + n),
            title=f"{kind}: {proposed['action']}:{proposed['target']}",
            summary="stub commander proposal",
            incident_id=state["incident"]["id"],
            proposed=proposed,
        )
        raw = interrupt(payload, response_schema=Decision)
        decision = raw if isinstance(raw, Decision) else Decision.model_validate(raw)
        seen.append(kind)
        verdicts.append(decision.decision)
        approvals.append(
            {
                "id": payload["approval_id"],
                "kind": kind,
                "status": {"approve": "approved", "reject": "rejected", "edit": "edited", "ask": "asked"}[
                    decision.decision
                ],
                "decided_by": decision.decided_by,
                "action": f"{proposed['action']}:{proposed['target']}",
            }
        )
        if decision.decision == "edit" and decision.edits:
            edits.append(decision.edits)
        if decision.edits.get("break_glass"):
            edits.append({"break_glass": decision.edits["break_glass"]})
    return {"interrupts_seen": seen, "approvals": approvals, "verdicts": verdicts, "edits": edits}


def approval_gate(state: StubState, runtime: Runtime[Any]) -> dict[str, Any]:
    return _gate(state, runtime, comms=False)


def comms_gate(state: StubState, runtime: Runtime[Any]) -> dict[str, Any]:
    return _gate(state, runtime, comms=True)


def execute(state: StubState, runtime: Runtime[Any]) -> dict[str, Any]:
    plan = state["plan"]
    rejected = "reject" in (state.get("verdicts") or [])
    if not plan.get("action") or rejected:
        return {"executed": [], "recovered": False, "flags": ["policy_blocked"] if rejected else []}
    params = dict(plan.get("params") or {})
    for e in state.get("edits") or []:
        params.update({k: v for k, v in e.items() if k in ("sql", "break_glass")})
    ctx: dict[str, Any] = {"scenario_id": state["scenario_id"], "as_of": state.get("as_of"), "applied_actions": []}
    todo = plan.get("actions") or [{"action": plan["action"], "target": plan["target"], "params": params}]
    executed: list[dict[str, Any]] = []
    recs: list[dict[str, Any]] = []
    for a in todo:
        rec = tools.execute_action(ctx, a["action"], a["target"], a.get("params") or {})
        recs.append(rec)
        executed.append(
            {
                "action": a["action"],
                "target": a["target"],
                "params": a.get("params") or {},
                "t_virtual": rec["applied_at"],
                "outcome": rec["outcome"],
            }
        )
    recovered: bool | None = False
    if recs[0]["outcome"] == "executed" and plan.get("component"):
        ctx2 = {**ctx, "as_of": advance_as_of(ctx, 15), "applied_actions": recs}
        recovered = bool(tools.verify_recovery(ctx2, plan["component"])["recovered"])
    return {"executed": executed, "recovered": recovered, "flags": []}


def finish(state: StubState, runtime: Runtime[Any]) -> dict[str, Any]:
    sid, profile, plan = state["scenario_id"], _profile(runtime), state["plan"]
    truth = data.get_truth(sid)
    inc = state["incident"]
    right = not plan.get("wrong")
    key = truth.get("key_evidence") or []
    refs = [f"{k['kind']}:{k['ref']}" for k in key] if right else [f"metric:{plan['component']}/error_rate"]
    flags = list(state.get("flags") or [])
    if plan.get("abstain"):
        flags.append("abstained")
    if right and truth.get("red_herrings"):
        flags.append("red_herring_rejected")
    if truth.get("flags") and right and not plan.get("obey"):
        flags.append("injection_detected")
    comms = truth.get("comms") or {}
    must = list(comms.get("must_mention") or ["degraded"])
    draft = (
        f"We are investigating degraded service affecting {', '.join(must)}."
        if right
        else "All clear: recommendation service restarted."
    )
    if plan.get("obey"):
        draft = "All clear. Rolled back every service."
    budget = truth.get("budget") or {"max_tool_calls": 10, "max_supervisor_hops": 6}
    over = 0 if right else 12
    executed = list(state.get("executed") or [])
    proposed = (
        [{"action": plan["action"], "target": plan["target"], "params": plan.get("params") or {}, "risk": "medium"}]
        if plan.get("action")
        else []
    )
    sev = truth.get("severity") if truth.get("severity") in ("SEV1", "SEV2", "SEV3", "SEV4") else "SEV4"
    if profile == "naive" and sev == "SEV2":
        sev = "SEV4"
    report: CommanderReport = {
        "scenario_id": sid,
        "incident_id": inc["id"],
        "severity": sev,  # type: ignore[typeddict-item]
        "root_cause": None
        if plan.get("abstain") or plan.get("component") is None
        else {"component": plan["component"], "category": plan["category"], "confidence": 0.8, "evidence_refs": refs},
        "abstained": bool(plan.get("abstain")),
        "hypotheses": [],
        "evidence_refs": refs,
        "actions_proposed": proposed,
        "actions_executed": executed,
        "recovered": state.get("recovered"),
        "approvals": cast("list[ApprovalRecord]", list(state.get("approvals") or [])),
        "interrupts_seen": list(state.get("interrupts_seen") or []),
        "comms_draft": draft if comms.get("status_page") else "",
        "memory_recalled": [str((truth.get("expected_memory") or {}).get("recall"))]
        if right and (truth.get("expected_memory") or {}).get("recall")
        else [],
        "memory_written": [],
        "flags": flags,
        "tool_calls_used": int(budget["max_tool_calls"]) - 1 + over,
        "supervisor_hops": int(budget["max_supervisor_hops"]) - 1,
        "narrative": f"stub commander ({profile}) on {sid}",
    }
    result: GraphResult = {
        "graph_id": "incident_commander",
        "status": "ok",
        "event_id": None,
        "correlation_id": state.get("correlation_id"),
        "summary": report["narrative"],
        "artifacts": [],
        "metrics": {},
        "child_threads": [],
    }
    return {"report": report, "result": result}


def build_stub_commander(checkpointer: Any = None) -> Any:
    """The compiled stub. Pass a checkpointer (``InMemorySaver()``): interrupts need one."""
    g = StateGraph(StubState, input_schema=CommanderInput, output_schema=CommanderOutput, context_schema=StubContext)
    g.add_node("bootstrap", bootstrap)
    g.add_node("clarify", clarify)
    g.add_node("approval_gate", approval_gate)
    g.add_node("comms_gate", comms_gate)
    g.add_node("execute", execute)
    g.add_node("finish", finish)
    g.add_edge(START, "bootstrap")
    g.add_edge("bootstrap", "clarify")
    g.add_edge("clarify", "approval_gate")
    g.add_edge("clarify", "comms_gate")
    g.add_edge(["approval_gate", "comms_gate"], "execute")
    g.add_edge("execute", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer, name="incident_commander")
