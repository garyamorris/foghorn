"""The commander's nodes. Stable names (docs/design/03 section 7.4 item 3): bootstrap, supervisor, the five specialists
(compiled subgraphs, see ``specialists``), rca_tournament (compiled subgraph), clarify_change_intent, remediation_planner,
policy_check, approval_gate, answer_question, execute_action, verify_recovery, comms_drafter, comms_gate, close_incident.

Conventions (docs/GRAPH_GUIDE.md): model calls go through ``get_chat_model`` (structured / routing calls are tagged
``nostream``); side effects happen AFTER the last ``interrupt()`` of a node and are idempotent (``claim_effect`` / deterministic
action ids / lifecycle idempotency keys); blocking data access runs in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Mapping
from typing import Any, Literal, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command, Send, interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit
from foghorn.contracts.interrupts import Answer, Decision, PolicyCheck, build_interrupt
from foghorn.contracts.meta import M_EVAL_RUN_ID
from foghorn.data import api as data
from foghorn.data import catalog
from foghorn.flows.incident_commander import chaos, comms
from foghorn.flows.incident_commander import investigation as inv
from foghorn.flows.incident_commander import policy as pol
from foghorn.flows.incident_commander import scripts as scr
from foghorn.flows.incident_commander.context import Context, ctx_of
from foghorn.flows.incident_commander.directives import parse_directives
from foghorn.flows.incident_commander.planning import (
    action_record,
    assess_severity,
    diagnosis_query,
    fingerprint,
    label,
    propose_actions,
    root_cause_claim,
)
from foghorn.flows.incident_commander.report import build_report, build_result
from foghorn.flows.incident_commander.schemas import PlanProposal, Route
from foghorn.flows.incident_commander.state import State
from foghorn.flows.rca_tournament.facts import Facts, evidence_from_envelope, make, merge_by_id
from foghorn.memory.api import MemoryHit, claim_effect, get_prefs, memory_ref, recall
from foghorn.models.factory import get_chat_model, structured
from foghorn.models.offline import OfflineChatModel
from foghorn.tools import api
from foghorn.tools.context import advance_as_of

MAX_EDITS = 3
MEMORY_MIN_SCORE = 0.33
MEMORY_EVIDENCE_MIN = 0.25
MAX_ASKS = 4
MAX_VERIFY_WAITS = 2
VERIFY_WAIT_MIN = 8.0  # the telemetry window ends 45 virtual minutes after the onset: do not spend it all waiting
SEVERITY_EVENT = {"SEV1": "critical", "SEV2": "high", "SEV3": "medium", "SEV4": "low"}
ENTRY_NODES = ("metrics_analyst", "logs_analyst", "change_analyst", "topology_analyst", "knowledge_agent")


# --- helpers -----------------------------------------------------------------------------------------------------------------
@functools.cache
def _smart_script() -> Any:
    return scr.make_smart_script()


@functools.cache
def _fast_script() -> Any:
    return scr.make_fast_script()


def llm(role: Literal["fast", "smart"], ctx: Context) -> BaseChatModel:
    model = get_chat_model(role, offline_script=_smart_script() if role == "smart" else _fast_script())
    if ctx.token_delay_ms and isinstance(model, OfflineChatModel):
        model = model.model_copy(update={"token_delay_s": ctx.token_delay_ms / 1000.0})
    return model


def thread_of(config: RunnableConfig) -> str | None:
    t = (config.get("configurable") or {}).get("thread_id")
    return str(t) if t else None


def tool_ctx(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": state["scenario_id"],
        "as_of": state.get("as_of"),
        "applied_actions": list(state.get("applied_actions") or []),
    }


def facts_of(state: Mapping[str, Any]) -> Facts:
    return Facts.from_evidence(state.get("evidence"), state.get("as_of"))


def inc_of(state: Mapping[str, Any]) -> dict[str, Any]:
    """The incident record as a plain dict (it is a TypedDict in the schema, a dict at runtime)."""
    return dict(cast(Mapping[str, Any], state.get("incident") or {}))


def severity_of(state: Mapping[str, Any]) -> str:
    return str(inc_of(state).get("severity", "SEV3"))


def incident_id(state: Mapping[str, Any]) -> str:
    return str(inc_of(state).get("id") or f"INC-{state.get('scenario_id')}")


def lifecycle(state: Mapping[str, Any], config: RunnableConfig, type_: str, ikey: str, /, **payload: Any) -> None:
    inc = inc_of(state)
    sev = str(inc.get("severity", "SEV3"))
    # Runs started by `eval_runner` are SIMULATIONS (thread metadata carries foghorn_eval_run_id). Their lifecycle
    # events are recorded but must never trigger real downstream work (a postmortem pipeline per scored scenario piled
    # up pending reviews and spent model money): routing.yaml `r-eval-incident` records events flagged `simulated`.
    eval_run_id = (config.get("metadata") or {}).get(M_EVAL_RUN_ID)
    if eval_run_id:
        payload = {"simulated": True, "eval_run_id": eval_run_id, **payload}
    emit_event(
        type_,
        idempotency_key=f"{type_}:{incident_id(state)}:{ikey}",
        component=payload.pop("component", None),
        severity=cast(Any, SEVERITY_EVENT.get(sev, "medium")),
        payload={
            "incident_id": incident_id(state),
            "severity": sev,
            "title": inc.get("title"),
            "scenario_id": state.get("scenario_id"),
            "thread_id": thread_of(config),
            **payload,
        },
        correlation_id=state.get("correlation_id"),
        scenario_id=state.get("scenario_id"),
        occurred_at=state.get("as_of"),
    )


def add_flag(flags: list[str], *new: str) -> list[str]:
    return list(dict.fromkeys([*flags, *new]))


def memory_evidence(hit: MemoryHit) -> dict[str, Any]:
    v = hit["value"]
    ns = hit["ns"]
    kind = ns[0]
    lessons = v.get("lessons") or ([hit["text"]] if kind == "lessons" else [])
    return make(
        "memory",
        memory_ref(hit),
        f"{kind[:-1] if kind.endswith('s') else kind} {hit['key']}: {hit['text'][:120]}",
        min(1.0, float(hit["score"] or 0.0)),
        "memory",
        None,
        key=hit["key"],
        store_kind=kind,
        ns=ns,
        score=float(hit["score"] or 0.0),
        component=v.get("root_cause_component") or (ns[1] if len(ns) > 1 else None),
        category=v.get("root_cause_category") or v.get("category"),
        resolution=list(v.get("resolution") or []),
        lessons=list(lessons)[:3],
    )


# --- bootstrap ---------------------------------------------------------------------------------------------------------------
async def bootstrap(
    state: State, config: RunnableConfig, runtime: Runtime[Context], store: BaseStore
) -> dict[str, Any]:
    """Load the scenario context, recall memory, apply team preferences, set the budget. No model call."""
    ctx = ctx_of(runtime.context)
    sid = str(state.get("scenario_id") or ctx.scenario_id)
    meta = await asyncio.to_thread(data.get_scenario, sid)
    incident = dict(state.get("incident") or {"id": f"INC-{sid}", "severity": "SEV3", "title": meta["title"]})
    incident.setdefault("title", meta["title"])
    incident["status"] = incident.get("status") or "open"
    as_of = state.get("as_of") or ctx.as_of or str(meta["trigger"]["as_of"])
    tier = int(meta["tier"])
    ev = [e for a in state.get("alerts") or [] if (e := evidence_from_envelope(a))]
    f = Facts.from_evidence(ev)
    focus = inv.focus_components(f)
    out: dict[str, Any] = {
        "scenario_id": sid,
        "incident": incident,
        "as_of": as_of,
        "scenario_meta": {"title": meta["title"], "summary": meta["summary"], "tier": tier, "as_of": as_of},
        "budget": inv.budget_for(tier),
        "focus": focus,
        "supervisor_hops": 0,
        "directives": parse_directives(ctx.supervisor_prompt, ctx.planner_prompt, ctx.analyst_prompt).__dict__
        | {"raw": list(parse_directives(ctx.supervisor_prompt, ctx.planner_prompt, ctx.analyst_prompt).raw)},
        "replans": 0,
        "edits": 0,
        "asks": 0,
        "verifies": 0,
        "memory_written": [],
        "recovered": None,
    }
    memory_ev: list[dict[str, Any]] = []
    recalled: list[str] = []
    prefs: list[dict[str, Any]] = []
    if ctx.memory_enabled and store is not None:
        rules = " ".join(str(a.get("payload", {}).get("rule", "")) for a in state.get("alerts") or [])
        query = f"{incident['title']}. {meta['summary']} {rules} {' '.join(focus)}"
        for hit in await recall(store, query, kinds=("incidents", "lessons"), k=4):
            if float(hit["score"] or 0) < MEMORY_EVIDENCE_MIN:
                continue
            e = memory_evidence(hit)
            memory_ev.append(e)  # weaker hits still count as corroboration of a recurring pattern ...
            if float(hit["score"] or 0) < MEMORY_MIN_SCORE:
                continue
            recalled.append(e["ref"])  # ... but only the clearly relevant ones are reported as recalled
            emit(
                "memory.recalled",
                f"memory:{'/'.join(hit['ns'])}:{hit['key']}",
                agent="bootstrap",
                ns="/".join(hit["ns"]),
                key=hit["key"],
                score=round(float(hit["score"] or 0), 3),
                summary=hit["text"][:140],
            )
            lifecycle(
                state,
                config,
                "memory.recalled",
                hit["key"],
                ns="/".join(hit["ns"]),
                key=hit["key"],
                score=round(float(hit["score"] or 0), 3),
            )
        near = {c for x in focus for c in (x, *catalog.transitive_callees(x))}
        for hit in await get_prefs(store):
            rule = hit["value"].get("rule") or {}
            if rule.get("scope") in (None, *near) or rule.get("scope") in ("add_index", "kill_queries"):
                prefs.append({"key": hit["key"], "rule": rule, "text": hit["text"]})
    out["evidence"] = [*ev, *memory_ev]
    out["memory_hits"] = [{"key": e["data"]["key"], "score": e["data"]["score"]} for e in memory_ev]
    out["memory_recalled"] = recalled
    out["prefs"] = prefs
    out["flags"] = []
    lifecycle(
        {**state, "incident": incident, "as_of": as_of},
        config,
        "incident.updated",
        "bootstrap",
        status="open",
        component=focus[0] if focus else None,
    )
    emit(
        "progress",
        "bootstrap",
        agent="bootstrap",
        label=f"{sid} loaded: tier {tier}, budget {out['budget']['max_tool_calls']} tool calls / {out['budget']['max_hops']} hops",
        pct=0.05,
    )
    return out


# --- supervisor --------------------------------------------------------------------------------------------------------------
def enforce_route(route: dict[str, Any], d: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Code-enforced invariants, whatever the model said: budgets, no planning without a diagnosis, valid agents/services."""
    flags: list[str] = []
    b = d["budget"]
    r = {
        **route,
        "agents": [a for a in route.get("agents", []) if a in inv.AGENTS],
        "services": [s for s in route.get("services", []) if s in catalog.COMPONENTS],
    }
    nxt = r["next"]
    has_hyps = bool(d["hypotheses"])
    if nxt == "investigate":
        r["agents"] = [a for a in dict.fromkeys(r["agents"]) if d["ran"].get(a, 0) < 2]
        if not r["agents"]:
            nxt = "rca_tournament"
            r["reason"] = "Nothing left to investigate; ranking the hypotheses."
        elif b["hops_left"] <= 2 or b["calls_left"] <= 0:
            nxt = "rca_tournament"
            flags.append("budget_exceeded")
            r["reason"] = "Investigation budget is spent; ranking the hypotheses on the evidence I have."
    cut_short = (
        (b["hops_left"] <= 2 or b["calls_left"] <= 0) and not d["ran"].get("logs_analyst") and d["profile"] != "naive"
    )
    if nxt == "rca_tournament" and cut_short and not has_hyps:
        flags.append("budget_exceeded")  # the budget, not the evidence, ended the investigation
    if nxt == "remediation_planner" and not has_hyps:
        nxt = "rca_tournament"
        r["reason"] = "No diagnosis yet; running the root-cause tournament first."
    if nxt == "rca_tournament" and has_hyps:
        nxt = "remediation_planner"
    if nxt == "rca_tournament" and d["rca_runs"] >= 1 and not has_hyps:
        nxt = "remediation_planner"  # the tournament found nothing to rank: plan with a placeholder diagnosis (abstain + escalate)
        r["placeholder"] = True
    if nxt == "clarify_change_intent" and (d["asked"] or not d["ask"]):
        nxt = "remediation_planner"
    r["next"] = nxt
    return r, list(dict.fromkeys(flags))


def diagnose(state: State, ctx: Context, d: dict[str, Any]) -> dict[str, Any]:
    """The diagnosis the plan stands on: root-cause claim (or abstention), severity, flags. Pure given the state."""
    f = facts_of(state)
    hyps = state.get("hypotheses") or []
    top = hyps[0]
    dirs = parse_directives(ctx.supervisor_prompt, ctx.planner_prompt, ctx.analyst_prompt)
    prof = ctx.model_profile
    threshold = 0.7 if prof == "cautious" else 0.45
    weak = top["score"] < threshold
    thin = bool(dirs.require_evidence) and d["evidence_count"] < dirs.require_evidence
    abstained = top["category"] == "telemetry_gap" or weak or thin
    sev, why = assess_severity(f, severity_of(state), top["category"])
    for pref in state.get("prefs") or []:  # team severity caps ("notification incidents are SEV3 unless ...")
        rule = pref.get("rule") or {}
        if rule.get("scope") == top["component"] and rule.get("severity_cap") and sev < str(rule["severity_cap"]):
            sev, why = str(rule["severity_cap"]), f"{why}; capped by team preference {pref['key']}"
    flags: list[str] = []
    if abstained:
        flags.append("abstained")
    if state.get("red_herrings"):
        flags.append("red_herring_rejected")
    if f.injections:
        flags.append("injection_detected")
    refs = list(top.get("evidence", []))
    return {
        "diagnosis": {
            "root_cause": root_cause_claim(top, abstained, float(top["score"]), refs),
            "abstained": abstained,
            "confidence": top["score"],
            "top": top["id"],
            "severity": sev,
            "severity_why": why,
            "reason": "telemetry is blind"
            if top["category"] == "telemetry_gap"
            else ("weak evidence" if weak else ("fewer than the required evidence items" if thin else "")),
        },
        "flags": flags,
    }


async def supervisor(
    state: State, config: RunnableConfig, runtime: Runtime[Context]
) -> Command[
    Literal[
        "metrics_analyst",
        "logs_analyst",
        "change_analyst",
        "topology_analyst",
        "knowledge_agent",
        "rca_tournament",
        "clarify_change_intent",
        "remediation_planner",
        "comms_drafter",
    ]
]:
    ctx = ctx_of(runtime.context)
    hops = int(state.get("supervisor_hops") or 0) + 1
    dirs = parse_directives(ctx.supervisor_prompt, ctx.planner_prompt, ctx.analyst_prompt)
    d = inv.build_digest({**state, "supervisor_hops": hops}, ctx.model_profile, {"raw": list(dirs.raw)})
    router = structured(llm("fast", ctx), Route).with_config(tags=["nostream"])
    proposal: Route = await router.ainvoke(
        [SystemMessage(content=ctx.supervisor_prompt), HumanMessage(content=scr.wrap("state", d))]
    )
    route, flags = enforce_route(proposal.model_dump(), d)
    if route.pop("placeholder", False):
        focus = (d["focus"] or ["unknown"])[0]
        state = {
            **state,
            "hypotheses": [
                {
                    "id": f"h:{focus}:telemetry_gap",
                    "component": focus,
                    "category": "telemetry_gap",
                    "score": 0.1,
                    "evidence": [],
                    "label": f"not enough evidence about {focus}",
                    "for": [],
                    "against": ["the tournament had nothing to rank"],
                }
            ],
        }
        placeholder = state["hypotheses"]
    narr = await llm("smart", ctx).ainvoke(
        [
            SystemMessage(content=ctx.supervisor_prompt),
            HumanMessage(
                content='<narrate kind="supervisor">' + scr.wrap("state", d) + scr.wrap("route", route) + "</narrate>"
            ),
        ]
    )
    route["narration"] = str(narr.text)
    emit(
        "handoff",
        f"handoff:{hops}",
        agent="supervisor",
        t_virtual=state.get("as_of"),
        to=", ".join(route["agents"]) or route["next"],
        reason=route["reason"],
    )
    update: dict[str, Any] = {"supervisor_hops": hops, "route": route}
    if "placeholder" in locals():
        update["hypotheses"] = placeholder
    if route["next"] == "rca_tournament":
        update["rca_runs"] = int(state.get("rca_runs") or 0) + 1
    if flags:
        update["flags"] = flags
    if route["next"] == "investigate":
        sends = []
        tasks = {agent: inv.build_task(agent, route, d) for agent in route["agents"]}
        as_of = state.get("as_of")
        wait = max((float(t.get("settle_minutes") or 0) for t in tasks.values()), default=0.0)
        if wait:  # the incident is only minutes old: let the signal firm up (virtual time) before everybody measures
            as_of = await asyncio.to_thread(advance_as_of, tool_ctx(state), wait)
            update.update({"as_of": as_of, "settled": True})
            emit(
                "progress",
                f"wait:{hops}",
                agent="supervisor",
                t_virtual=as_of,
                label=f"waiting {wait:.0f} virtual minutes for the signal to firm up (now {as_of[11:16]})",
                pct=0.1,
            )
        for agent, task in tasks.items():
            msg = HumanMessage(
                content=f"TASK {agent} (hop {hops}). Investigate within your tool budget and report numbers.\n{scr.wrap('task', task)}"
            )
            sends.append(
                Send(
                    agent,
                    {
                        "messages": [msg],
                        "scenario_id": state["scenario_id"],
                        "as_of": as_of,
                        "applied_actions": list(state.get("applied_actions") or []),
                        "evidence": [],
                        "agent_runs": [],
                    },
                )
            )
        return Command(goto=sends, update=update)
    if route["next"] == "remediation_planner":
        diag = diagnose(state, ctx, d)
        sev = diag["diagnosis"]["severity"]
        incident = {**(state.get("incident") or {}), "severity": sev, "status": "mitigating"}
        update.update(
            {
                "diagnosis": diag["diagnosis"],
                "incident": incident,
                "flags": add_flag(flags, *diag["flags"]),
                "candidates": [],
            }
        )
        lifecycle(
            {**state, "incident": incident},
            config,
            "incident.updated",
            "diagnosed",
            status="mitigating",
            root_cause=diag["diagnosis"]["root_cause"],
            component=(diag["diagnosis"]["root_cause"] or {}).get("component"),
        )
        emit(
            "decision",
            f"decision:diagnosis:{hops}",
            agent="supervisor",
            what=("abstain: " + diag["diagnosis"]["reason"])
            if diag["diagnosis"]["abstained"]
            else f"root cause: {state['hypotheses'][0]['label']}",
            why=diag["diagnosis"]["severity_why"],
        )
        return Command(goto=["remediation_planner", "comms_drafter"], update=update)
    return Command(goto=route["next"], update=update)


# --- clarify_change_intent -----------------------------------------------------------------------------------------------------
def question_text(ask: dict[str, Any], state: Mapping[str, Any]) -> str:
    if ask["reason"] == "change_intent":
        return (
            f"{ask['what']} was ramped {ask['from']}->{ask['to']} at {ask['at']} by {ask['actor']} ({ask['change_id']}), "
            f"while {ask['rival']} landed within minutes. Was the ramp intended? If it was, should I still revert it?"
        )
    if ask["reason"] == "telemetry_blind":
        flat = sorted(facts_of(state).flat())
        return f"Telemetry for {', '.join(flat) or ask['component']} has been flat for several minutes, so I cannot see their health. Can you confirm their state out-of-band (dashboards, on-call) before I do anything?"
    return f"I believe this is {str(ask.get('category', '')).replace('_', ' ')} on {ask['component']} with {float(ask.get('score', 0)):.0%} confidence. Do you agree, or is there context I am missing?"


async def clarify_change_intent(state: State, config: RunnableConfig, runtime: Runtime[Context]) -> dict[str, Any]:
    """A ``question`` interrupt: the evidence of an intended change (or the visibility of the service) is ambiguous."""
    d = inv.build_digest(cast(dict[str, Any], state), ctx_of(runtime.context).model_profile)
    ask = d["ask"] or {
        "reason": "confirm_diagnosis",
        "component": (state.get("hypotheses") or [{"component": "unknown"}])[0]["component"],
    }
    q = question_text(ask, state)
    qid = f"q_{incident_id(state)}_{len(state.get('qa_log') or [])}"
    emit("note", f"question:{qid}", agent="clarify_change_intent", text=q)
    lifecycle(
        state,
        config,
        "approval.requested",
        qid,
        approval_id=qid,
        kind="question",
        title="Question from the incident commander",
        summary=q,
        risk="low",
        component=ask["component"],
    )
    payload = build_interrupt(
        "question",
        approval_id=qid,
        title="Question from the incident commander",
        summary=q,
        risk="low",
        incident_id=incident_id(state),
        evidence_refs=(state.get("hypotheses") or [{}])[0].get("evidence", [])[:6],
        proposed={"question": q, "reason": ask["reason"]},
    )
    ans = Answer.model_validate(interrupt(payload, response_schema=Answer))
    lifecycle(
        state,
        config,
        "approval.decided",
        qid,
        approval_id=qid,
        kind="question",
        decision="answered",
        decided_by=ans.decided_by,
    )
    note = make(
        "answer",
        f"answer:{qid}",
        f"{ans.decided_by} answered: {ans.answer[:120]}",
        0.3,
        "clarify_change_intent",
        state.get("as_of"),
        question=q,
        answer=ans.answer,
        by=ans.decided_by,
        reason=ask["reason"],
    )
    return {
        "clarified": True,
        "qa_log": [{"id": qid, "q": q, "a": ans.answer, "by": ans.decided_by, "reason": ask["reason"]}],
        "approvals": [
            {"id": qid, "kind": "question", "status": "answered", "decided_by": ans.decided_by, "action": None}
        ],
        "interrupts_seen": ["question"],
        "evidence": [note],
    }


# --- remediation_planner -------------------------------------------------------------------------------------------------------
def planner_digest(
    state: State, ctx: Context, hyp: dict[str, Any], options: list[dict[str, Any]], f: Facts
) -> dict[str, Any]:
    diag = state.get("diagnosis") or {}
    return {
        "incident": {"id": incident_id(state), "severity": severity_of(state)},
        "profile": ctx.model_profile,
        "hypothesis": {k: hyp[k] for k in ("id", "component", "category", "score")},
        "options": options,
        "rejected": list(state.get("rejected") or []),
        "answers": [x["a"] for x in state.get("qa_log") or [] if x.get("reason") != "telemetry_blind"],
        "untrusted_excerpts": [f"<untrusted>{i['excerpt']}</untrusted>" for i in f.injections][:2],
        "recent_deploys": sorted({c["component"] for c in f.changes if c["kind"] == "deploy"}),
        "memory": [m["id"] for m in f.memory][:3],
        "severity": diag.get("severity"),
        "severity_why": diag.get("severity_why", ""),
        "abstained": bool(diag.get("abstained")),
    }


def map_proposal(prop: PlanProposal, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_label = {label(o): o for o in options}
    out: list[dict[str, Any]] = []
    for a in prop.actions:
        got = by_label.get(f"{a.action}:{a.target}")
        if got is not None:
            out.append({**got, "rationale": a.rationale or got["rationale"]})
        else:
            out.append(
                action_record(
                    a.action,
                    a.target,
                    a.params,
                    a.rationale or "proposed by the planner",
                    source="untrusted_text" if a.basis == "instruction" else "evidence",
                    secondary=a.action in ("open_ticket", "warm_cache"),
                )
            )
    return list({x["id"]: x for x in out}.values())


async def remediation_planner(
    state: State, config: RunnableConfig, runtime: Runtime[Context], store: BaseStore
) -> dict[str, Any]:
    ctx = ctx_of(runtime.context)
    diag = state.get("diagnosis") or {}
    top = (state.get("hypotheses") or [])[0]
    replans = int(state.get("replans") or 0)
    update: dict[str, Any] = {}
    if (
        ctx.memory_enabled and store is not None
    ):  # memory-guided planning: recall by the diagnosis, not just the symptoms
        query = diagnosis_query(facts_of(state), top, list(state.get("red_herrings") or []))
        extra = []
        for hit in await recall(store, query, kinds=("incidents", "lessons"), k=3):
            if float(hit["score"] or 0) < MEMORY_MIN_SCORE:
                continue
            e = memory_evidence(hit)
            extra.append(e)
            emit(
                "memory.recalled",
                f"memory:{'/'.join(hit['ns'])}:{hit['key']}",
                agent="remediation_planner",
                ns="/".join(hit["ns"]),
                key=hit["key"],
                score=round(float(hit["score"] or 0), 3),
                summary=hit["text"][:140],
            )
            lifecycle(
                state,
                config,
                "memory.recalled",
                hit["key"],
                ns="/".join(hit["ns"]),
                key=hit["key"],
                score=round(float(hit["score"] or 0), 3),
            )
        if extra:
            update["evidence"] = extra
            update["memory_recalled"] = list(
                dict.fromkeys([*(state.get("memory_recalled") or []), *[e["ref"] for e in extra]])
            )
    f = Facts.from_evidence(merge_by_id(state.get("evidence"), update.get("evidence")), state.get("as_of"))
    dirs = parse_directives(ctx.supervisor_prompt, ctx.planner_prompt, ctx.analyst_prompt)
    hyp = top
    escalate_only = bool(diag.get("abstained")) or replans > ctx.max_replans
    if escalate_only:
        hyp = {**top, "category": "telemetry_gap"}
    options = propose_actions(
        f, hyp, dirs, rejected=list(state.get("rejected") or []), prefs=list(state.get("prefs") or [])
    )
    if not options:
        options = [
            action_record(
                "escalate_human",
                top["component"],
                {"reason": "no safe automated action remains"},
                "no safe automated action remains; a human takes over",
            )
        ]
    for o_ in options:
        if o_["action"] == "kill_queries" and any(
            "list_sessions_first" in str(p.get("rule")) for p in state.get("prefs") or []
        ):
            o_["rationale"] += " (team rule: list the longest sessions and their owners first)"
    d = planner_digest(state, ctx, hyp, options, f)
    planner = structured(llm("smart", ctx), PlanProposal).with_config(tags=["nostream"])
    prop: PlanProposal = await planner.ainvoke(
        [SystemMessage(content=ctx.planner_prompt), HumanMessage(content=scr.wrap("state", d))]
    )
    plan = map_proposal(prop, options)
    narr = await llm("smart", ctx).ainvoke(
        [
            SystemMessage(content=ctx.planner_prompt),
            HumanMessage(content='<narrate kind="plan">' + scr.wrap("state", d) + "</narrate>"),
        ]
    )
    chosen = next((a for a in plan if not a.get("secondary")), plan[0])
    emit(
        "plan.proposed",
        f"plan:{replans}:{len(state.get('rejected') or [])}",
        agent="remediation_planner",
        t_virtual=state.get("as_of"),
        actions=[{"action": a["action"], "target": a["target"], "risk": a["risk"]} for a in plan],
        narration=str(narr.text)[:300],
    )
    update.update(
        {
            "plan": plan,
            "chosen_action": chosen,
            "proposed": plan,
            "flags": add_flag(
                [], *(["escalated"] if chosen["action"] == "escalate_human" and replans > ctx.max_replans else [])
            ),
        }
    )
    return update


# --- policy_check --------------------------------------------------------------------------------------------------------------
async def policy_check(
    state: State, config: RunnableConfig, runtime: Runtime[Context]
) -> Command[Literal["approval_gate", "comms_gate", "remediation_planner"]]:
    """Guardrail node: every proposed action against the policies of the data layer (forbidden actions, freeze, matrix)."""
    ctx = ctx_of(runtime.context)
    f = facts_of(state)
    hyps = state.get("hypotheses") or []
    top = hyps[0] if hyps else None
    diag = state.get("diagnosis") or {}
    sev = severity_of(state)
    plan: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    pol_by_id: dict[str, dict[str, Any]] = {}
    scope_h = {**top, "category": "telemetry_gap"} if (top and diag.get("abstained")) else top
    for a in state.get("plan") or []:
        res = pol.evaluate_action(a, facts=f, hypothesis=scope_h, severity=sev, as_of=str(state.get("as_of")))
        pol_by_id[a["id"]] = res
        for c in res["checks"]:
            emit(
                "policy.check",
                f"policy:{a['id']}:{c['id']}",
                agent="policy_check",
                rule=c["id"],
                result=c["status"],
                text=f"{res['label']}: {c['text']}",
            )
        mark = {
            **a,
            "blocked": res["verdict"] == "block",
            "block_reason": res["reason"] if res["verdict"] == "block" else None,
            "policy_verdict": res["verdict"],
        }
        (blocked if mark["blocked"] else plan).append(mark)
    blocked_flags = ["policy_blocked"] if blocked else []
    if any(a.get("source") not in pol.TRUSTED_SOURCES for a in blocked):
        blocked_flags.append("injection_followed")  # the model obeyed text found in a log; the gate stopped it
    replans = int(state.get("replans") or 0)
    prim = next((a for a in plan if not a.get("secondary")), None)
    update: dict[str, Any] = {"plan": plan + blocked, "proposed": plan + blocked, "flags": blocked_flags}
    if prim is None:
        # nothing safe remains: replan (bounded), then escalate to a human
        if replans < ctx.max_replans:
            rej = [label(a) for a in blocked]
            update.update(
                {
                    "replans": replans + 1,
                    "rejected": [*(state.get("rejected") or []), *rej],
                    "flags": add_flag(blocked_flags, "replanned"),
                }
            )
            return Command(goto="remediation_planner", update=update)
        esc = action_record(
            "escalate_human",
            (top or {"component": "unknown"})["component"],
            {"reason": "every proposed action was blocked by policy"},
            "every proposed action was blocked by policy; a human takes over",
        )
        res = pol.evaluate_action(esc, facts=f, hypothesis=scope_h, severity=sev, as_of=str(state.get("as_of")))
        update.update(
            {
                "plan": [esc],
                "chosen_action": esc,
                "policy": res,
                "proposed": [esc],
                "flags": add_flag(blocked_flags, "escalated"),
            }
        )
        return Command(goto=cast(Any, _gate_targets(state)), update=update)
    update.update({"chosen_action": prim, "policy": pol_by_id[prim["id"]], "plan": plan + blocked})
    return Command(goto=cast(Any, _gate_targets(state)), update=update)


def _gate_targets(state: State) -> list[str]:
    """The human gates open TOGETHER: the action approval and (first pass only) the status-page review, so the dashboard shows
    both cards at once and one resume-by-id map answers them."""
    return ["approval_gate", *(["comms_gate"] if (state.get("comms") or {}).get("status") == "drafted" else [])]


# --- approval_gate -------------------------------------------------------------------------------------------------------------
def _apply_edits(action: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    out = {**action, "params": dict(action.get("params") or {})}
    if isinstance(edits.get("params"), dict):
        out["params"].update(edits["params"])
    if "sql" in edits:
        out["params"]["sql"] = edits["sql"]
    if edits.get("target"):
        out["target"] = str(edits["target"])
    out["id"] = "pa_" + fingerprint(out)[:8]
    return out


def _break_glass(dec: Decision) -> dict[str, str] | None:
    bg: dict[str, Any] = dec.edits["break_glass"] if isinstance(dec.edits.get("break_glass"), dict) else {}
    reason = str(bg.get("reason") or dec.edits.get("reason") or dec.note or "")
    second = str(bg.get("second_approver") or dec.edits.get("second_approver") or "")
    if len(reason) >= 20 and second:
        return {"reason": reason, "second_approver": second}
    return None


def _payload_for(
    state: State,
    action: dict[str, Any],
    policy: dict[str, Any],
    kind: str,
    apr_id: str,
    n: int,
    i: int,
    edit_note: str = "",
) -> dict[str, Any]:
    qa = state.get("qa_log") or []
    summary = action["rationale"] + (" " + edit_note if edit_note else "")
    if qa:
        summary += " | " + " | ".join(f"Q: {x['q'][:80]} A: {x['a'][:120]}" for x in qa[-2:])
    roles = policy["roles"]
    role = roles[i] if i < len(roles) else (roles[-1] if roles else "service_oncall")
    callers = (
        sorted(
            catalog.transitive_callers(action["target"]),
            key=lambda c: (catalog.transitive_callers(action["target"])[c], c),
        )
        if action["target"] in catalog.COMPONENTS
        else []
    )
    return build_interrupt(
        cast(Any, kind),
        approval_id=apr_id,
        title=f"{action['action']}:{action['target']}",
        summary=summary,
        risk=policy["risk"],
        reversible=policy["reversible"],
        blast_radius=[action["target"], *callers[:6]],
        policy_checks=[PolicyCheck(**c) for c in policy["checks"]],
        approvers_required=n,
        roles=roles if n == 1 else [role],
        incident_id=incident_id(state),
        evidence_refs=list(action.get("refs", []))[:8],
        proposed={
            "action": action["action"],
            "target": action["target"],
            "params": action.get("params") or {},
            "rationale": action["rationale"],
            "fingerprint": fingerprint(action),
            "approver": i + 1,
            "secondary": [label(a) for a in state.get("plan") or [] if a.get("secondary") and not a.get("blocked")],
            **(
                {
                    "required": {
                        "note": "reason of at least 20 characters",
                        "edits.second_approver": "name of the second approver",
                    }
                }
                if kind == "break_glass"
                else {}
            ),
        },
        options=["approve", "reject", "ask"]
        if kind == "break_glass"
        else (["approve", "edit"] if kind == "edit" else None),
    )


GATE_TARGETS = ("execute_action", "remediation_planner", "policy_check", "answer_question", "close_incident")


def _go(goto: str, update: dict[str, Any]) -> dict[str, Any]:
    """The gate routes through a CONDITIONAL EDGE reading ``gate_next`` (not ``Command(goto)``): a state edit made at the
    breakpoint before ``execute_action`` (``update_state(as_node="approval_gate")``) re-evaluates the router on the edited
    state and the thread still continues to ``execute_action`` (docs/design/02 gotcha 9 / the QA fork finding)."""
    return {**update, "gate_next": goto}


def route_gate(state: State) -> str:
    nxt = str(state.get("gate_next") or "close_incident")
    return nxt if nxt in GATE_TARGETS else "close_incident"


async def approval_gate(state: State, config: RunnableConfig, runtime: Runtime[Context]) -> dict[str, Any]:
    """Dynamic ``interrupt(response_schema=Decision)``. Honours ``approval_policy``; approve / edit / reject / ask; SEV1 or
    high-risk actions need two approvers (two sequential interrupts); break-glass for freeze-gated actions. Everything after an
    ``interrupt()`` re-runs on resume, so ids are deterministic and events carry idempotency keys."""
    ctx = ctx_of(runtime.context)
    action, policy = state["chosen_action"], state["policy"]
    inc = incident_id(state)
    sev = severity_of(state)
    dirs = parse_directives(ctx.supervisor_prompt, ctx.planner_prompt, ctx.analyst_prompt)
    base = f"apr_{inc}_{int(state.get('replans') or 0)}_{int(state.get('edits') or 0)}_{int(state.get('asks') or 0)}"
    replans = int(state.get("replans") or 0)
    seen: list[str] = []
    records: list[dict[str, Any]] = []
    approvers: list[str] = []
    bg: dict[str, str] | None = None
    verdict = policy["verdict"]
    reviewed = list(state.get("reviewed") or [])
    from foghorn.data import actions as act_mod

    spec = act_mod.ACTIONS.get(action["action"])
    review = (
        bool(spec and spec.needs_params)
        and fingerprint(action) not in reviewed
        and ctx.approval_policy != "auto_low_risk"
    )
    if verdict == "edit" or review:
        need, why, kind, n = True, "the parameters of this action need a human review before it can run", "edit", 1
    else:
        need, why = pol.needs_human(ctx.approval_policy, dirs.risk, policy, ctx.model_profile, sev)
        kind = "break_glass" if policy["break_glass"] else "approval"
        n = max(1, int(policy["approvers_required"])) if need else 0
    if not need:
        rec = {
            "id": f"{base}_auto",
            "kind": "approval",
            "status": "approved",
            "decided_by": "policy:auto",
            "action": label(action),
        }
        lifecycle(
            state,
            config,
            "approval.decided",
            rec["id"],
            approval_id=rec["id"],
            kind="approval",
            decision="approve",
            decided_by="policy:auto",
            reason=why,
        )
        return _go(
            "execute_action",
            {
                "approvals": [rec],
                "approved": {
                    "fingerprint": fingerprint(action),
                    "id": action["id"],
                    "approvers": ["policy:auto"],
                    "bg": None,
                },
            },
        )
    for i in range(n):
        apr_id = f"{base}_a{i + 1}"
        kind_i = "four_eyes" if (i > 0 and kind == "approval") else kind
        payload = _payload_for(state, action, policy, kind_i, apr_id, n, i)
        lifecycle(
            state,
            config,
            "approval.requested",
            apr_id,
            approval_id=apr_id,
            kind=kind_i,
            title=payload["title"],
            summary=payload["summary"],
            risk=policy["risk"],
            component=action["target"],
        )
        dec = Decision.model_validate(interrupt(payload, response_schema=Decision))
        if (
            i == 0
        ):  # `interrupts_seen` lists decision points: the second signature of a four-eyes approval is not a new one
            seen.append(kind)
        status = {"approve": "approved", "reject": "rejected", "edit": "edited", "ask": "asked"}[dec.decision]
        records.append(
            {"id": apr_id, "kind": kind_i, "status": status, "decided_by": dec.decided_by, "action": label(action)}
        )
        lifecycle(
            state,
            config,
            "approval.decided",
            apr_id,
            approval_id=apr_id,
            kind=kind_i,
            decision=dec.decision,
            decided_by=dec.decided_by,
            note=dec.note,
        )
        common = {"approvals": records, "interrupts_seen": seen}
        if dec.decision == "reject":
            rej = [*(state.get("rejected") or []), label(action)]
            if replans >= ctx.max_replans:
                return _go(
                    "remediation_planner",
                    {**common, "rejected": rej, "replans": replans + 1, "flags": ["replanned", "escalated"]},
                )
            return _go(
                "remediation_planner",
                {
                    **common,
                    "rejected": rej,
                    "replans": replans + 1,
                    "flags": ["replanned"],
                    "qa_log": [
                        {
                            "id": f"fb_{apr_id}",
                            "q": f"Rejected {label(action)}",
                            "a": dec.note or "rejected",
                            "by": dec.decided_by,
                            "reason": "rejection_feedback",
                        }
                    ],
                },
            )
        if dec.decision == "edit":
            if int(state.get("edits") or 0) >= MAX_EDITS:
                return _go(
                    "remediation_planner",
                    {
                        **common,
                        "rejected": [*(state.get("rejected") or []), label(action)],
                        "replans": replans + 1,
                        "flags": ["replanned"],
                    },
                )
            edited = _apply_edits(action, dec.edits)
            plan = [edited if a["id"] == action["id"] else a for a in state.get("plan") or []]
            return _go(
                "policy_check",
                {
                    **common,
                    "plan": plan,
                    "chosen_action": edited,
                    "edits": int(state.get("edits") or 0) + 1,
                    "reviewed": [*reviewed, fingerprint(edited)],
                },
            )
        if dec.decision == "ask":
            if int(state.get("asks") or 0) >= MAX_ASKS:
                return _go(cast(Any, "approval_gate"), {**common, "asks": int(state.get("asks") or 0) + 1})
            return _go(
                "answer_question",
                {
                    **common,
                    "asks": int(state.get("asks") or 0) + 1,
                    "pending_question": {
                        "question": dec.question or dec.note or "Why this action?",
                        "approval_id": apr_id,
                        "by": dec.decided_by,
                    },
                },
            )
        approvers.append(dec.decided_by)
        if kind == "break_glass":
            bg = _break_glass(dec)
            if bg is None:
                return _go(
                    "remediation_planner",
                    {
                        **common,
                        "rejected": [*(state.get("rejected") or []), label(action)],
                        "replans": replans + 1,
                        "flags": ["replanned", "policy_blocked"],
                    },
                )
    if (
        kind == "edit" and verdict == "edit" and int(state.get("edits") or 0) + 1 >= MAX_EDITS
    ):  # kept failing the gate: give up on this action
        return _go(
            "remediation_planner",
            {
                "approvals": records,
                "interrupts_seen": seen,
                "rejected": [*(state.get("rejected") or []), label(action)],
                "replans": replans + 1,
                "flags": ["replanned", "policy_blocked"],
            },
        )
    if kind == "edit":  # reviewed and kept as-is: the plan goes back through the policy gate
        return _go(
            "policy_check",
            {
                "approvals": records,
                "interrupts_seen": seen,
                "edits": int(state.get("edits") or 0) + 1,
                "reviewed": [*reviewed, fingerprint(action)],
            },
        )
    approved = {"fingerprint": fingerprint(action), "id": action["id"], "approvers": approvers, "bg": bg}
    flags = ["four_eyes_same_person"] if len(set(approvers)) < len(approvers) else []
    return _go("execute_action", {"approvals": records, "interrupts_seen": seen, "approved": approved, "flags": flags})


async def answer_question(state: State, config: RunnableConfig, runtime: Runtime[Context]) -> dict[str, Any]:
    """The human asked a question on an approval card: answer it from the evidence, then the gate asks again."""
    ctx = ctx_of(runtime.context)
    q = state.get("pending_question") or {"question": "?"}
    action, policy = state["chosen_action"], state["policy"]
    hyps = state.get("hypotheses") or []
    blast = None
    if action["target"] in catalog.COMPONENTS:
        callers = catalog.transitive_callers(action["target"])
        blast = {"total": len(callers), "tier0": [c for c in callers if catalog.COMPONENTS[c].tier == 0]}
    payload = {
        "question": q["question"],
        "action": {k: action[k] for k in ("action", "target")},
        "policy": {k: policy[k] for k in ("risk", "reversible")},
        "blast": blast,
        "hypothesis": {k: hyps[0][k] for k in ("component", "category", "score")} if hyps else None,
    }
    msg = await llm("smart", ctx).ainvoke(
        [SystemMessage(content=ctx.supervisor_prompt), HumanMessage(content=scr.wrap("answer", payload))]
    )
    text = str(msg.text)
    emit("note", f"answer:{q.get('approval_id')}", agent="answer_question", text=text)
    return {
        "qa_log": [
            {
                "id": f"qa_{q.get('approval_id')}",
                "q": q["question"],
                "a": text,
                "by": "incident_commander",
                "reason": "approver_question",
            }
        ],
        "approvals": [
            {
                "id": f"ans_{q.get('approval_id')}",
                "kind": "approval",
                "status": "answered",
                "decided_by": "incident_commander",
                "action": label(action),
            }
        ],
        "pending_question": {},
    }


# --- execute_action ------------------------------------------------------------------------------------------------------------
async def execute_action(
    state: State, config: RunnableConfig, runtime: Runtime[Context], store: BaseStore
) -> Command[Literal["verify_recovery", "policy_check", "remediation_planner", "close_incident"]]:
    """Runs AFTER the interrupt, idempotently. The action that runs is exactly the one approved; an edit made at a
    breakpoint sends it back through the policy gate and a fresh approval."""
    ctx = ctx_of(runtime.context)
    action = state["chosen_action"]
    approved = state.get("approved") or {}
    if approved.get("fingerprint") != fingerprint(action):
        if int(state.get("edits") or 0) >= MAX_EDITS:
            return Command(goto="close_incident", update={"flags": ["policy_blocked", "escalated"]})
        emit(
            "note",
            f"edited:{action['id']}",
            agent="execute_action",
            text="the action changed after its approval: back to the policy gate",
        )
        edited = {
            **action,
            "id": "pa_" + fingerprint(action)[:8],
        }  # an edited action is a NEW proposal (own id, own audit row)
        plan = [edited, *[a for a in state.get("plan") or [] if a["id"] != action["id"] and a.get("secondary")]]
        return Command(goto="policy_check", update={"edits": int(state.get("edits") or 0) + 1, "plan": plan})
    inc = incident_id(state)
    todo = [action] + [
        a for a in state.get("plan") or [] if a.get("secondary") and not a.get("blocked") and a["id"] != action["id"]
    ]
    chaos.maybe_fail(ctx.chaos, thread_of(config), "execute_action", fingerprint(action))
    recs: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    applied = list(state.get("applied_actions") or [])
    primary_ok = True
    for i, a in enumerate(todo):
        params = dict(a.get("params") or {})
        if a["action"] == action["action"] and approved.get("bg"):
            params["break_glass"] = approved["bg"]
        rec = await asyncio.to_thread(
            api.execute_action, {**tool_ctx(state), "applied_actions": applied}, a["action"], a["target"], params
        )
        first = await claim_effect(store, f"{inc}:{a['action']}:{a['target']}", {"record": rec["id"]})
        recs.append(rec)
        done.append(
            {
                "id": rec["id"],
                "action": a["action"],
                "target": a["target"],
                "params": params,
                "t_virtual": rec["applied_at"],
                "outcome": rec["outcome"],
                "ok": rec["ok"],
                "reason": rec["reason"],
                "risk": rec.get("risk"),
            }
        )
        if rec["ok"]:
            applied.append(rec)
        lifecycle(
            state,
            config,
            "action.executed",
            rec["id"],
            action=a["action"],
            target=a["target"],
            outcome=rec["outcome"],
            reason=rec["reason"],
            first_claim=first,
            component=a["target"],
        )
        emit(
            "decision",
            f"executed:{rec['id']}",
            agent="execute_action",
            t_virtual=rec["applied_at"],
            what=f"{a['action']}:{a['target']} -> {rec['outcome']}",
            why=rec["reason"],
        )
        if i == 0 and not rec["ok"]:
            primary_ok = False
            break
    update: dict[str, Any] = {"applied_actions": [r for r in recs if r["ok"]], "executed": done}
    if not primary_ok:
        replans = int(state.get("replans") or 0)
        update.update(
            {
                "flags": ["policy_blocked", "replanned"],
                "rejected": [*(state.get("rejected") or []), label(action)],
                "replans": replans + 1,
            }
        )
        return Command(goto="remediation_planner" if replans < ctx.max_replans else "close_incident", update=update)
    spec_kind = action.get("kind")
    if spec_kind in ("escalation", "comms", "ticket"):
        return Command(goto="close_incident", update=update)
    return Command(goto="verify_recovery", update=update)


# --- verify_recovery -----------------------------------------------------------------------------------------------------------
def verify_targets(f: Facts, state: State) -> list[str]:
    """What must be healthy again: the blamed component (its callers when it is an external provider) AND the components that
    alerted: verifying only the blamed one would 'recover' a wrong diagnosis."""
    top = (state.get("hypotheses") or [{}])[0]
    comp = top.get("component")
    out: list[str] = []

    def add(c: str) -> None:
        if c not in catalog.COMPONENTS or c in out:
            return
        if catalog.COMPONENTS[c].kind == "external":
            out.extend(x for x in catalog.callers_of(c, sync_only=True)[:1] if x not in out)
        else:
            out.append(c)

    if comp:
        add(comp)
    for c in inv.focus_components(f):
        add(c)
    if "checkout-service" in f.anomalies():
        add("checkout-service")
    return out[:4]


async def verify_recovery(
    state: State, config: RunnableConfig, runtime: Runtime[Context]
) -> Command[Literal["remediation_planner", "close_incident"]]:
    """Wait (virtual time), then check the services; failure loops back to replanning (bounded)."""
    ctx = ctx_of(runtime.context)
    f = facts_of(state)
    targets = verify_targets(f, state)
    chaos.maybe_fail(ctx.chaos, thread_of(config), "verify_recovery", ",".join(targets))
    as_of = str(state.get("as_of"))
    results: list[dict[str, Any]] = []
    status = "unknown"
    for wait in range(MAX_VERIFY_WAITS + 1):
        as_of = await asyncio.to_thread(
            advance_as_of, {**tool_ctx(state), "as_of": as_of}, VERIFY_WAIT_MIN if wait == 0 else VERIFY_WAIT_MIN * 0.75
        )
        results = []
        for svc in targets:
            r = await asyncio.to_thread(api.verify_recovery, {**tool_ctx(state), "as_of": as_of}, svc)
            results.append(r)
        sts = {r["status"] for r in results}
        status = (
            "recovered"
            if sts <= {"recovered", "healthy"}
            else ("unknown" if "unknown" in sts else ("recovering" if "recovering" in sts else "degraded"))
        )
        emit(
            "progress",
            f"verify:{wait}",
            agent="verify_recovery",
            t_virtual=as_of,
            label=f"verifying {', '.join(targets)}: {status}",
            pct=min(1.0, (wait + 1) / (MAX_VERIFY_WAITS + 1)),
        )
        if status != "recovering":
            break
    recovered = True if status == "recovered" else (None if status == "unknown" else False)
    lifecycle(
        {**state, "as_of": as_of},
        config,
        "action.verified",
        f"{status}:{len(state.get('applied_actions') or [])}",
        action=label(state["chosen_action"]),
        target=state["chosen_action"]["target"],
        outcome=status,
        services=targets,
        recovered=recovered,
    )
    update: dict[str, Any] = {
        "as_of": as_of,
        "recovered": recovered,
        "verification": {
            "status": status,
            "services": targets,
            "checks": [{"service": r["service"], "status": r["status"], "reason": r["reason"]} for r in results],
        },
        "verifies": int(state.get("verifies") or 0) + 1,
    }
    if recovered is False:
        replans = int(state.get("replans") or 0)
        update.update(
            {
                "rejected": [*(state.get("rejected") or []), label(state["chosen_action"])],
                "replans": replans + 1,
                "flags": ["replanned"],
            }
        )
        if replans < ctx.max_replans:
            return Command(goto="remediation_planner", update=update)
        update["flags"] = ["replanned", "escalated"]
    return Command(goto="close_incident", update=update)


# --- comms ---------------------------------------------------------------------------------------------------------------------
async def comms_drafter(
    state: State, config: RunnableConfig, runtime: Runtime[Context]
) -> Command[Literal["close_incident"]]:
    ctx = ctx_of(runtime.context)
    sev = severity_of(state)
    diag = state.get("diagnosis") or {}
    f = facts_of(state)
    if not comms.required(sev) or diag.get("abstained"):
        return Command(goto="close_incident", update={"comms": {"status": "not_required", "severity": sev}})
    terms = comms.affected_terms(f)
    payload = {
        "terms": terms,
        "severity": sev,
        "profile": ctx.model_profile,
        "recovered": False,
        "untrusted_excerpts": [f"<untrusted>{i['excerpt']}</untrusted>" for i in f.injections][:2],
    }
    msg = await llm("smart", ctx).ainvoke(
        [
            SystemMessage(
                content="You draft the public status-page update. Describe what customers see, never internal systems or causes; no all clear before recovery is verified; at most 600 characters."
            ),
            HumanMessage(content=scr.wrap("comms", payload)),
        ]
    )
    return Command(
        goto="close_incident",
        update={"comms_draft": str(msg.text).strip(), "comms": {"status": "drafted", "severity": sev, "terms": terms}},
    )


async def comms_gate(
    state: State, config: RunnableConfig, runtime: Runtime[Context]
) -> Command[Literal["close_incident"]]:
    """POL-002 lint, then publish. A human edit interrupt is raised for SEV1, an external-dependency outage (the status page IS the
    mitigation), strict approval policy, or a draft that had to be rewritten."""
    ctx = ctx_of(runtime.context)
    draft = str(state.get("comms_draft") or "")
    c = state.get("comms") or {}
    sev = str(c.get("severity", "SEV2"))
    diag = state.get("diagnosis") or {}
    top = (state.get("hypotheses") or [{}])[0]
    terms = list(c.get("terms") or [])
    violations = comms.lint_status(draft)
    flags: list[str] = []
    rewritten = False
    if violations:
        emit(
            "policy.check",
            "policy:comms:POL-002",
            agent="comms_gate",
            rule="POL-002",
            result="fail",
            text="status draft rejected: " + "; ".join(violations),
        )
        draft = comms.sanitize_status(draft, terms, sev)
        flags.append("policy_blocked")
        rewritten = True
    else:
        emit(
            "policy.check",
            "policy:comms:POL-002",
            agent="comms_gate",
            rule="POL-002",
            result="pass",
            text="status draft is factual, short and free of internal names",
        )
    ask = (
        ctx.approval_policy == "strict"
        or ctx.model_profile == "cautious"
        or sev == "SEV1"
        or top.get("category") == "dependency_outage"
        or rewritten
    )
    seen: list[str] = []
    records: list[dict[str, Any]] = []
    status = "published"
    if ask:
        apr_id = f"cmm_{incident_id(state)}"
        proposed = {
            "action": "post_status_update",
            "target": "*",
            "params": {"text": draft},
            "text": draft,
            "max_chars": comms.MAX_CHARS,
            "lint": violations,
        }
        payload = build_interrupt(
            "edit",
            approval_id=apr_id,
            title="Status page update",
            summary="Review the public update before it is published (POL-002: factual, no internal names, at most 600 characters).",
            risk="low",
            incident_id=incident_id(state),
            proposed=proposed,
            options=["approve", "edit", "reject"],
        )
        lifecycle(
            state,
            config,
            "approval.requested",
            apr_id,
            approval_id=apr_id,
            kind="edit",
            title="Status page update",
            summary=draft[:200],
            risk="low",
        )
        dec = Decision.model_validate(interrupt(payload, response_schema=Decision))
        seen.append("edit")
        records.append(
            {
                "id": apr_id,
                "kind": "edit",
                "status": {"approve": "approved", "reject": "rejected", "edit": "edited", "ask": "asked"}[dec.decision],
                "decided_by": dec.decided_by,
                "action": "post_status_update:*",
            }
        )
        lifecycle(
            state,
            config,
            "approval.decided",
            apr_id,
            approval_id=apr_id,
            kind="edit",
            decision=dec.decision,
            decided_by=dec.decided_by,
        )
        if dec.decision == "reject":
            status = "withheld"
        elif dec.decision == "edit":
            new = next(
                (str(dec.edits[k]) for k in ("text", "message", "comms_draft") if dec.edits.get(k)), None
            ) or str((dec.edits.get("params") or {}).get("text") or "")
            if new:
                draft = new
    update: dict[str, Any] = {"approvals": records, "interrupts_seen": seen, "flags": flags}
    if status == "published" and comms.lint_status(draft):  # a human edit must pass the lint too
        draft = comms.sanitize_status(draft, terms, sev)
        update["flags"] = add_flag(flags, "policy_blocked")
    if status == "published":
        rec = await asyncio.to_thread(api.execute_action, tool_ctx(state), "post_status_update", "*", {"text": draft})
        lifecycle(
            state,
            config,
            "action.executed",
            rec["id"],
            action="post_status_update",
            target="status-page",
            outcome=rec["outcome"],
            component=None,
        )
        update["applied_actions"] = [rec] if rec["ok"] else []
        update["executed"] = [
            {
                "id": rec["id"],
                "action": "post_status_update",
                "target": "*",
                "params": {"text": draft},
                "t_virtual": rec["applied_at"],
                "outcome": rec["outcome"],
                "ok": rec["ok"],
                "reason": rec["reason"],
                "risk": "low",
            }
        ]
    update["comms_draft"] = draft
    update["comms"] = {**c, "status": status, "published": draft if status == "published" else ""}
    del diag
    return Command(goto="close_incident", update=update)


# --- close_incident ------------------------------------------------------------------------------------------------------------
async def close_incident(state: State, config: RunnableConfig, runtime: Runtime[Context]) -> dict[str, Any]:
    """Deferred join of the act and communicate branches: builds the ``report`` and the ``result``."""
    ctx = ctx_of(runtime.context)
    report = build_report(dict(state))
    msg = await llm("smart", ctx).ainvoke(
        [
            SystemMessage(content=ctx.supervisor_prompt),
            HumanMessage(content='<narrate kind="close">' + scr.wrap("report", dict(report)) + "</narrate>"),
        ]
    )
    report["narrative"] = str(msg.text)
    inc = {**(state.get("incident") or {})}
    recovered = state.get("recovered")
    noise = (state.get("diagnosis") or {}).get("root_cause", {}) or {}
    resolved = recovered is True or (
        recovered is None
        and noise.get("category") == "planned_maintenance"
        and any(a.get("ok") for a in state.get("executed") or [])
    )
    inc["status"] = "resolved" if resolved else "monitoring"
    rc = report["root_cause"]
    st = {**state, "incident": inc}
    if resolved:
        lifecycle(
            st,
            config,
            "incident.resolved",
            "closed",
            status="resolved",
            resolution=report["narrative"],
            root_cause=rc,
            component=(cast(dict[str, Any], rc) if rc else {}).get("component"),
        )
    else:
        lifecycle(
            st,
            config,
            "incident.updated",
            "closed",
            status="monitoring",
            needs_human=True,
            root_cause=rc,
            component=(cast(dict[str, Any], rc) if rc else {}).get("component"),
        )
    emit(
        "artifact",
        "artifact:report",
        agent="close_incident",
        name="commander_report",
        kind="report",
        ref=report["incident_id"],
    )
    return {"report": report, "result": build_result(dict(state), report), "incident": inc}
