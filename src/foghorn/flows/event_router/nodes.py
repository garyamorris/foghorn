"""Node functions of ``event_router``.  Verbs, stable names (docs/GRAPH_GUIDE.md): validate, ledger, enrich, classify,
decide, open_incident, join_incident, ignore, escalate_storm, finish."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeError
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command, Send
from pydantic import ValidationError

from foghorn.common.delegate import delegate
from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit
from foghorn.contracts.events import EventEnvelope
from foghorn.contracts.inputs import GraphResult
from foghorn.contracts.meta import (
    M_CORRELATION_ID,
    M_EVENT_ID,
    M_SCENARIO_ID,
    M_TRIGGER,
    det_thread_id,
    incident_id_for,
    thread_metadata,
)
from foghorn.data import api as data_api
from foghorn.data.util import to_epoch
from foghorn.flows.event_router import enrich as enrich_mod
from foghorn.flows.event_router import rules, topology
from foghorn.flows.event_router.commander import (
    commander_context,
    commander_thread_id,
    parent_config,
    start_commander,
)
from foghorn.flows.event_router.context import RouterContext
from foghorn.flows.event_router.ledger import Ledger, ledger_lock
from foghorn.flows.event_router.scripts import router_script
from foghorn.flows.event_router.state import RouteDecision, RouterState
from foghorn.logging import get_logger
from foghorn.models.factory import get_chat_model, structured
from foghorn.security import redact

log = get_logger("event_router")

AMBIENT_EPOCH = "2026-01-01T00:00:00Z"  # deterministic stand-in when an event has neither occurred_at nor a scenario
_INC_ID = re.compile(r"^INC-[A-Za-z0-9-]+$")


def _ctx(runtime: Runtime[RouterContext]) -> RouterContext:
    return runtime.context or RouterContext()  # runtime.context is None locally unless context= is passed (02:27)


def _category(event_type: str) -> str:
    if event_type in rules.TRIAGED_TYPES:
        return "signal"
    if event_type == "alert.resolved":
        return "resolution"
    if event_type in rules.CHANGE_TYPES:
        return "change"
    if event_type in rules.MAINTENANCE_TYPES:
        return "maintenance"
    return "other"


def _component(event: dict[str, Any]) -> str | None:
    comp = (event.get("subject") or {}).get("component")
    return str(comp) if comp else None


# ------------------------------------------------------------------------------------------------------- validate
def validate(state: RouterState, runtime: Runtime[RouterContext]) -> dict[str, Any]:
    ctx = _ctx(runtime)
    raw = state.get("event") or {}
    try:
        env = EventEnvelope.model_validate(raw)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc']) or 'event'}: {e['msg']}" for e in exc.errors()][:5]
        emit("log", f"validate:{raw.get('id', 'unknown')}", agent="validate", level="warn", text="; ".join(errors))
        return {"valid": False, "errors": errors, "event_id": str(raw.get("id") or "unknown"), "event": dict(raw)}
    event = env.to_state()
    if not event["id"]:
        digest = hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:12]  # noqa: S324
        event["id"] = f"evt_{digest}"
    scenario_id = (event.get("trace") or {}).get("scenario_id") or ctx.scenario_id
    defaulted = False
    if not event.get("occurred_at"):
        defaulted = True
        event["occurred_at"] = ctx.as_of or (
            data_api.default_as_of(scenario_id) if scenario_id else AMBIENT_EPOCH  # virtual time, never the wall clock
        )
    emit(
        "progress",
        f"validate:{event['id']}",
        agent="validate",
        label=f"envelope valid: {event['type']} on {_component(event) or 'unknown component'}",
        pct=10,
    )
    return {
        "valid": True,
        "errors": [],
        "event": event,
        "event_id": event["id"],
        "scenario_id": scenario_id,
        "now_s": to_epoch(event["occurred_at"]),
        "classification": {"occurred_at_defaulted": defaulted},
    }


def after_validate(state: RouterState) -> str:
    return "ledger" if state.get("valid") else "reject"


# --------------------------------------------------------------------------------------------------------- ledger
async def ledger(state: RouterState, store: BaseStore) -> dict[str, Any]:
    event, now_s = state["event"], state["now_s"]
    lg = Ledger(store)
    done = await lg.processed(state["event_id"])
    facts = await lg.facts(event, now_s)
    facts["redelivery"] = done
    emit(
        "progress",
        f"ledger:{state['event_id']}",
        agent="ledger",
        label=(
            f"ledger: {len(facts['open_incidents'])} open incident(s), {len(facts['recent_alert_keys'])} recent key(s), "
            f"burst of {facts['burst_count']}" + (", re-delivery" if done else "")
        ),
        pct=25,
    )
    return {"ledger": facts}


def fan_out(state: RouterState) -> list[Send] | str:
    """Conditional edge: Send one ``enrich`` task per branch for real signals, skip enrichment for everything else."""
    event = state["event"]
    if (state.get("ledger") or {}).get("redelivery") or event["type"] not in rules.TRIAGED_TYPES:
        return "skip_enrichment"
    return [
        Send(
            "enrich",
            {
                "kind": kind,
                "event_id": state["event_id"],
                "scenario_id": state.get("scenario_id"),
                "as_of": event["occurred_at"],
                "component": _component(event),
                "attr_chaos": (event.get("attributes") or {}).get("chaos"),
            },
        )
        for kind in enrich_mod.KINDS
    ]


# --------------------------------------------------------------------------------------------------------- enrich
def enrich(arg: dict[str, Any], runtime: Runtime[RouterContext]) -> dict[str, Any]:
    """One enrichment branch (runs 3x in parallel via Send).  Sync on purpose: the tools read SQLite."""
    ctx = _ctx(runtime)
    kind = arg["kind"]
    info = getattr(runtime, "execution_info", None)
    attempt = int(getattr(info, "node_attempt", 1) or 1)
    if enrich_mod.chaos_fails(kind, arg["event_id"], attempt, ctx.chaos, arg.get("attr_chaos")):
        raise ConnectionError(f"chaos: simulated {kind} outage on attempt {attempt}")
    data = enrich_mod.run_branch(kind, arg.get("scenario_id"), arg.get("as_of"), arg.get("component"))
    emit("progress", f"enrich:{arg['event_id']}:{kind}", agent="enrich", label=f"{kind} enriched", pct=50)
    return {"enrichment": {kind: data}}


def enrich_failed(arg: dict[str, Any], error: NodeError) -> dict[str, Any]:
    """Error handler: a branch that still fails after its retries degrades instead of failing the router."""
    kind = arg.get("kind", "unknown")
    emit("log", f"enrich-failed:{arg.get('event_id')}:{kind}", agent="enrich", level="warn", text=repr(error.error))
    return {
        "enrichment": {
            kind: {"available": False, "degraded": True, "error": f"{type(error.error).__name__}: {error.error}"}
        },
        "degraded": [kind],
    }


# ------------------------------------------------------------------------------------------------------- classify
def _impact(event: dict[str, Any], topo: dict[str, Any]) -> str:
    if event["type"] == "customer.report":
        return "reported"
    blast = topo.get("blast") or {}
    if blast.get("customer_facing_exposed") and blast.get("revenue_path_exposed"):
        return "direct"
    return "indirect" if int(blast.get("total_affected", 0)) > 0 else "none"


def _llm_prompt(event: dict[str, Any], cls: dict[str, Any], facts: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    text = redact(str(payload.get("text") or payload.get("summary") or ""))[:240].replace("\n", " ")
    lines = [
        f"event_type={event['type']}",
        f"severity={event.get('severity')}",
        f"component={cls['component']}",
        f"known_component={str(cls['known']).lower()}",
        f"tier={cls['tier'] if cls['tier'] is not None else 'unknown'}",
        f"rule={payload.get('rule')}",
        f"customer_impact={cls['customer_impact']}",
        f"open_incidents={len(facts['open_incidents'])}",
        f"text={text}",
    ]
    return "\n".join(lines)


async def _ask_fallback(
    ctx: RouterContext, event: dict[str, Any], cls: dict[str, Any], facts: dict[str, Any]
) -> dict[str, Any] | None:
    llm = structured(get_chat_model("fast", offline_script=router_script()), RouteDecision).with_config(
        tags=["nostream"]  # the JSON verdict must never reach the user's token stream
    )
    try:
        verdict = await llm.ainvoke([SystemMessage(ctx.classify_prompt), HumanMessage(_llm_prompt(event, cls, facts))])
    except Exception as exc:
        log.warning("router_llm_fallback_failed", error=repr(exc))
        return None
    out: dict[str, Any] = verdict.model_dump() if isinstance(verdict, RouteDecision) else dict(verdict)
    return out


async def classify(state: RouterState, runtime: Runtime[RouterContext]) -> dict[str, Any]:
    """Deterministic signals first (alert rule, tier, customer impact, maintenance window); LLM only if ambiguous."""
    ctx = _ctx(runtime)
    event = state["event"]
    comp = _component(event)
    enr = state.get("enrichment") or {}
    topo = enr.get("topology") or {}
    facts = dict(state.get("ledger") or {})
    windows = list(facts.get("maintenance_windows", []))
    for m in (enr.get("changes") or {}).get("maintenance", []):  # windows announced through the change feed
        if m.get("suppress_alerts", True) and m.get("end"):
            windows.append({"component": m["component"], "start": m["start"], "end": m["end"], "source": "changes"})
    facts["maintenance_windows"] = windows

    tier = topo.get("tier") if topo.get("available") else topology.tier_of(comp)
    cls: dict[str, Any] = {
        **(state.get("classification") or {}),
        "category": _category(event["type"]),
        "component": comp,
        "known": topology.known(comp),
        "tier": tier,
        "kind": topo.get("kind") if topo.get("available") else topology.kind_of(comp),
        "rule": (event.get("payload") or {}).get("rule"),
        "rule_known": (event.get("payload") or {}).get("rule") in rules.KNOWN_RULES,
        "customer_impact": _impact(event, topo),
        "in_maintenance": any(w["component"] == comp for w in windows),
        "ambiguous": rules.is_ambiguous(event),
        "enrichment_sources": {k: bool((v or {}).get("available")) for k, v in enr.items()},
        "degraded": list(state.get("degraded") or []),
        "via": "rules",
        "llm": None,
        "reasons": [],
    }
    cls["reasons"].append(f"category={cls['category']} tier={tier} impact={cls['customer_impact']}")
    pre = rules.decide(event, facts) if cls["category"] == "signal" else None
    if cls["ambiguous"] and pre is not None and pre["decision"] == "open_incident":
        if ctx.use_llm_fallback:
            verdict = await _ask_fallback(ctx, event, cls, facts)
            if verdict is not None:
                cls["via"], cls["llm"] = "llm", verdict
                cls["reasons"].append(
                    f"ambiguous signal: fallback verdict {verdict['decision']} ({verdict['rationale']})"
                )
            else:
                cls["reasons"].append("ambiguous signal: language model unavailable, failing safe to the rule matrix")
        else:
            cls["reasons"].append("ambiguous signal: fallback disabled, failing safe to the rule matrix")
    emit(
        "metric",
        f"classify:{state['event_id']}",
        agent="classify",
        name="customer_impact",
        value={"none": 0, "indirect": 1, "reported": 2, "direct": 3}[cls["customer_impact"]],
        unit="level",
    )
    return {"classification": cls, "ledger": facts}


# --------------------------------------------------------------------------------------------------------- decide
def _record_only(reason: str, route: str = "ignore", decision: str = "record") -> dict[str, Any]:
    return {
        "decision": decision,
        "route": route,
        "severity": "none",
        "reason_code": reason,
        "attach_to": None,
        "dedupe_of": None,
        "priority": "P3",
        "trace": [f"non-triage event: {reason}"],
    }


def decide(state: RouterState, runtime: Runtime[RouterContext]) -> dict[str, Any]:
    ctx = _ctx(runtime)
    event, facts, cls = state["event"], state["ledger"], state["classification"]
    comp = _component(event)
    category = cls["category"]
    corroborated = any(
        rk["key"] != event.get("dedupe_key")
        and rk.get("component") == comp
        and rk["age_s"] < rules.CORROBORATION_WINDOW_S
        for rk in facts.get("recent_alert_keys", [])
    )
    own = facts.get("own_incident")
    if own and category == "signal" and not facts.get("redelivery"):
        t = {
            "decision": "open_incident",
            "route": "open_incident",
            "severity": own["severity"],
            "reason_code": "restart_of_open_incident",
            "attach_to": None,
            "dedupe_of": None,
            "priority": rules.SEV_PRIORITY.get(own["severity"], "P2"),
            "trace": [f"this event already opened {own['id']} (run restarted): finish starting its commander"],
        }
    elif facts.get("redelivery"):
        prev = facts["redelivery"]
        t = {
            **_record_only("redelivery", decision="dedupe"),
            "dedupe_of": event["id"],
            "previous": prev.get("decision"),
        }
    elif category == "signal":
        t = rules.decide(event, facts, llm=cls.get("llm"))
    elif category == "change":
        near = topology.neighbours(comp)
        inc = next((i for i in facts.get("open_incidents", []) if near & set(i["components"])), None)
        if inc:
            t = {
                **_record_only("change_attached", "join_incident", "attach"),
                "attach_to": inc["id"],
                "severity": inc["severity"],
            }
            t["trace"] = [f"change {event['type']} on {comp}: open incident {inc['id']} covers the component"]
        else:
            t = _record_only("change_ledgered")
    elif category == "maintenance":
        t = _record_only("maintenance_recorded")
    elif category == "resolution":
        t = _record_only("resolution_recorded")
    else:
        t = _record_only("not_actionable")

    storm_cfg = max(2, int(ctx.storm_count))
    storm_open = facts.get("storm")
    in_signal = event["type"] in rules.STORM_SIGNAL_TYPES and t["decision"] in (
        "open_incident",
        "escalate_human",
        "attach",
    )
    if in_signal and not facts.get("redelivery"):
        if storm_open is None and event["type"] == "alert.fired" and int(facts.get("burst_count", 0)) >= storm_cfg:
            t = {**t, "route": "escalate_storm", "storm": True}
            t["trace"] = [*t["trace"], f"storm: {facts['burst_count']} distinct alerts within {rules.STORM_WITHIN_S}s"]
        elif storm_open is not None:
            t = {**t, "storm": True, "storm_key": storm_open["key"]}
            if t["route"] == "open_incident":
                t = {**t, "route": "ignore", "reason_code": "storm_member"}
                t["trace"] = [*t["trace"], f"storm {storm_open['key']} is open: folded into the correlator run"]
    t["noise_probability"] = rules.noise_probability(t["decision"], event, corroborated=corroborated)
    t["needs_human"] = t["decision"] == "escalate_human" or (cls.get("llm") or {}).get("decision") == "escalate_human"
    t["via"] = cls.get("via", "rules")
    emit(
        "decision",
        f"decision:{state['event_id']}",
        agent="decide",
        what=f"{t['decision']} -> {t['route']}" + (f" {t['severity']}" if t["severity"] != "none" else ""),
        why=t["reason_code"],
    )
    return {"triage": t, "route": t["route"]}


#: route -> the label Studio draws on the conditional edge out of ``decide``
DECIDE_LABELS = {
    "open_incident": "new incident",
    "join_incident": "join existing",
    "ignore": "noise / benign",
    "escalate_storm": "alert storm",
}


def after_decide(state: RouterState) -> str:
    return DECIDE_LABELS[str(state["route"])]


# --------------------------------------------------------------------------------------------- incident plumbing
def _incident_title(event: dict[str, Any]) -> str:
    comp = _component(event) or "unknown component"
    payload = event.get("payload") or {}
    if event["type"] == "customer.report":
        return f"Customer report on {comp}: {redact(str(payload.get('text') or ''))[:80]}".strip()
    rule = payload.get("rule") or event["type"]
    value, threshold = payload.get("value"), payload.get("threshold")
    extra = f" ({value} vs {threshold})" if value is not None and threshold is not None else ""
    return f"{comp}: {rule}{extra}"


def _event_severity(sev: str) -> Any:
    return rules.SEV_TO_EVENT_SEVERITY.get(sev, "medium")


def _forward_context(ctx: RouterContext, scenario_id: str | None) -> dict[str, Any]:
    return commander_context(
        scenario_id=scenario_id,
        model_profile=ctx.model_profile,
        approval_policy=ctx.approval_policy,
        memory_enabled=ctx.memory_enabled,
        chaos=ctx.chaos,
        token_delay_ms=ctx.token_delay_ms,
        breakpoints=ctx.breakpoints,
    )


# ---------------------------------------------------------------------------------------------------- open_incident
async def open_incident(
    state: RouterState, config: RunnableConfig, store: BaseStore, runtime: Runtime[RouterContext]
) -> Command[Any]:
    ctx = _ctx(runtime)
    event, triage, now_s = state["event"], state["triage"], state["now_s"]
    comp = _component(event)
    corr = str(event.get("correlation_id") or f"corr_{event['id']}")
    payload_id = (event.get("payload") or {}).get("incident_id")
    incident_id = str(payload_id) if payload_id and _INC_ID.match(str(payload_id)) else incident_id_for(corr)
    severity = triage["severity"]
    title = _incident_title(event)
    lg = Ledger(store)
    near = topology.neighbours(comp)
    async with ledger_lock():  # the last word on "is there already an incident for this?" (concurrent runs)
        replay = await lg.get_incident(incident_id)
        existing = next((i for i in await lg.open_incidents(now_s) if near & set(i["components"])), None)
        if replay is None and existing is not None:
            joined = {
                **triage,
                "decision": "attach",
                "route": "join_incident",
                "attach_to": existing["id"],
                "severity": existing["severity"],
                "reason_code": "open_incident_covers_component",
                "trace": [*triage["trace"], f"race: incident {existing['id']} was opened meanwhile -> join"],
            }
            return Command(update={"triage": joined, "route": "join_incident"}, goto="join_incident")
        if replay is None:
            await lg.put_incident(
                {
                    "id": incident_id,
                    "correlation_id": corr,
                    "components": [comp] if comp else [],
                    "severity": severity,
                    "opened_at": now_s,
                    "expires_at": now_s + rules.INCIDENT_JOIN_WINDOW_S,
                    "status": "open",
                    "scenario_id": state.get("scenario_id"),
                    "event_id": event["id"],
                    "thread_id": commander_thread_id(incident_id),
                    "title": title,
                    "occurrences": 1,
                    "event_ids": [event["id"]],
                }
            )
    thread_id = commander_thread_id(incident_id)
    emit_event(
        "incident.opened",
        idempotency_key=f"incident.opened:{incident_id}",
        component=comp,
        severity=_event_severity(severity),
        payload={
            "incident_id": incident_id,
            "severity": severity,
            "title": title,
            "status": "open",
            "thread_id": thread_id,
            "scenario_id": state.get("scenario_id"),
            "graph_id": "incident_commander",
            "needs_human": bool(triage.get("needs_human")),
        },
        correlation_id=corr,
        causation_id=event["id"],
        scenario_id=state.get("scenario_id"),
        occurred_at=event["occurred_at"],
    )
    commander = "skipped:dry_run" if not ctx.dispatch_commander else "skipped:no_scenario"
    children: list[str] = []
    scenario_id = state.get("scenario_id")
    if ctx.dispatch_commander and scenario_id:
        started = await start_commander(
            config=config,
            incident_id=incident_id,
            correlation_id=corr,
            severity=severity,
            title=title,
            scenario_id=scenario_id,
            event_id=event["id"],
            as_of=event["occurred_at"],
            alerts=[event],
            trigger_event_ids=[event["id"]],
            context=_forward_context(ctx, scenario_id),
        )
        commander = "joined" if started["values"] else "started"
        children.append(started["child_thread_id"])
        emit(
            "handoff",
            f"handoff:{incident_id}",
            agent="open_incident",
            to="incident_commander",
            reason=f"{severity} {title}",
            thread_id=started["child_thread_id"],
        )
    return Command(
        update={
            "incident": {
                "id": incident_id,
                "severity": severity,
                "title": title,
                "correlation_id": corr,
                "thread_id": thread_id,
                "opened": True,
                "commander": commander,
            },
            "child_threads": children,
        },
        goto="finish",
    )


# --------------------------------------------------------------------------------------------------- join_incident
async def join_incident(state: RouterState, store: BaseStore) -> dict[str, Any]:
    event, triage = state["event"], state["triage"]
    comp = _component(event)
    incident_id = str(triage["attach_to"])
    lg = Ledger(store)
    raised_to: str | None = None
    occurrences = 1
    item: dict[str, Any] | None = None
    severity = str(triage.get("severity") or "SEV3")
    async with ledger_lock():
        item = await lg.get_incident(incident_id)
        if item is not None:
            severity = str(item["severity"])
            if event["type"] in rules.TRIAGED_TYPES and event["type"] != "customer.report":
                candidate = rules.severity_for(str(event.get("severity")), comp)
                if rules.more_severe(candidate, severity):
                    raised_to, severity = candidate, candidate
                    item["severity"] = candidate
            ids = list(item.get("event_ids", []))
            if event["id"] not in ids:
                item["occurrences"] = int(item.get("occurrences", 1)) + 1
                ids.append(event["id"])
            item["event_ids"] = ids[-25:]
            joined = set(item.get("joined_components", []))
            # the incident's reach grows by one ring: only first-ring neighbours of the opening components extend it
            opening = set(item["components"])
            if comp and any(comp in topology.neighbours(o) for o in opening):
                joined.add(comp)
            item["joined_components"] = sorted(joined - opening)
            occurrences = int(item["occurrences"])
            await lg.put_incident(item)
    thread_id = commander_thread_id(incident_id)
    emit_event(
        "incident.updated",
        idempotency_key=f"incident.updated:{incident_id}:{event['id']}",
        component=comp,
        severity=_event_severity(severity),
        payload={
            "incident_id": incident_id,
            "severity": severity,
            "status": "open",
            "thread_id": thread_id,
            "scenario_id": state.get("scenario_id"),
            "joined_event_id": event["id"],
            "joined_type": event["type"],
            "occurrences": occurrences,
            "severity_raised_to": raised_to,
        },
        correlation_id=item.get("correlation_id") if item else None,
        causation_id=event["id"],
        scenario_id=state.get("scenario_id"),
        occurred_at=event["occurred_at"],
    )
    emit(
        "note",
        f"join:{incident_id}:{event['id']}",
        agent="join_incident",
        text=f"{event['type']} on {comp} joined {incident_id}"
        + (f"; severity raised to {raised_to}" if raised_to else ""),
    )
    return {
        "incident": {
            "id": incident_id,
            "severity": severity,
            "thread_id": thread_id,
            "opened": False,
            "joined": True,
            "severity_raised_to": raised_to,
            "occurrences": occurrences,
        }
    }


# --------------------------------------------------------------------------------------------------------- ignore
async def ignore(state: RouterState, store: BaseStore) -> dict[str, Any]:
    event, triage = state["event"], state["triage"]
    if event["type"] == "maintenance.window.started":
        payload = event.get("payload") or {}
        comp = _component(event)
        if comp:
            start = str(payload.get("starts_at") or event["occurred_at"])
            end = str(payload.get("ends_at") or event["occurred_at"])
            await Ledger(store).put_maintenance(
                {
                    "id": str(payload.get("change_id") or event["id"]),
                    "component": comp,
                    "start": start,
                    "end": end,
                    "source": "event",
                }
            )
            emit("note", f"maintenance:{event['id']}", agent="ignore", text=f"maintenance window recorded for {comp}")
    else:
        emit("note", f"ignore:{event['id']}", agent="ignore", text=f"no incident: {triage['reason_code']}")
    return {}


# --------------------------------------------------------------------------------------------------- escalate_storm
async def escalate_storm(
    state: RouterState, config: RunnableConfig, store: BaseStore, runtime: Runtime[RouterContext]
) -> dict[str, Any]:
    ctx = _ctx(runtime)
    event, now_s = state["event"], state["now_s"]
    lg = Ledger(store)
    alerts = await lg.burst(now_s)
    if all(a.get("id") != event["id"] for a in alerts):
        alerts.append(event)
    scope = state.get("scenario_id") or (event.get("trace") or {}).get("stream") or "ambient"
    first = min(to_epoch(a["occurred_at"]) for a in alerts)
    key = f"{scope}:{first // 600}"
    thread_id = det_thread_id("storm_correlator", key)
    await lg.put_storm(
        key,
        {"started_at": now_s, "until": now_s + rules.STORM_COOLDOWN_S, "alerts": len(alerts), "thread_id": thread_id},
    )
    storm = {"key": key, "alerts": len(alerts), "thread_id": thread_id, "correlator": "skipped:dry_run"}
    children: list[str] = []
    if ctx.dispatch_commander:
        started = await delegate(
            graph_id="storm_correlator",
            key=key,
            config=parent_config(config, f"router-{event['id']}"),
            mode="background",
            child_thread_id=thread_id,
            input={"scenario_id": str(scope), "alerts": alerts, "as_of": event["occurred_at"]},
            context=_forward_context(ctx, state.get("scenario_id")),
            metadata=thread_metadata(
                "storm",
                "storm_correlator",
                **{
                    M_EVENT_ID: event["id"],
                    M_CORRELATION_ID: f"storm_{key}",
                    M_SCENARIO_ID: state.get("scenario_id"),
                    M_TRIGGER: "delegate",
                },
            ),
        )
        storm["correlator"] = "joined" if started["values"] else "started"
        children.append(started["child_thread_id"])
        emit(
            "handoff",
            f"handoff:storm:{key}",
            agent="escalate_storm",
            to="storm_correlator",
            reason=f"{len(alerts)} alerts within {rules.STORM_WITHIN_S}s",
            thread_id=thread_id,
        )
    return {"storm": storm, "child_threads": children}


# --------------------------------------------------------------------------------------------------------- finish
_STATUS = {"open_incident": "ok", "join_incident": "ok", "escalate_storm": "ok"}


async def finish(state: RouterState, store: BaseStore) -> dict[str, Any]:
    event = state.get("event") or {}
    if not state.get("valid"):
        result: GraphResult = {
            "graph_id": "event_router",
            "status": "failed",
            "event_id": state.get("event_id"),
            "correlation_id": event.get("correlation_id"),
            "summary": "invalid event envelope: " + "; ".join(state.get("errors") or ["unknown error"]),
            "artifacts": [
                {"kind": "table", "name": "validation_errors", "ref": None, "data": state.get("errors") or []}
            ],
            "metrics": {"valid": 0.0},
            "child_threads": [],
        }
        return {
            "result": result,
            "route": "reject",
            "triage": {"decision": "invalid", "route": "reject"},
            "incident": None,
        }

    triage, cls = state["triage"], state["classification"]
    incident = state.get("incident")
    route = triage["route"]
    if triage["decision"] == "dedupe":
        status = "duplicate"
    elif route in _STATUS:
        status = _STATUS[route]
    else:
        status = "skipped"
    if incident and incident.get("joined") and route == "open_incident":
        route = "join_incident"  # a lost open-race degraded into a join
    correlation = (incident or {}).get("correlation_id") or event.get("correlation_id")

    lg = Ledger(store)
    if triage.get("storm_key"):
        await lg.extend_storm(str(triage["storm_key"]), state["now_s"] + rules.STORM_COOLDOWN_S)
    if event["type"] in rules.TRIAGED_TYPES:
        await lg.record_seen(event, state["now_s"], decision=triage["decision"])
    await lg.record_processed(
        event["id"],
        {"decision": triage["decision"], "route": route, "incident_id": (incident or {}).get("id")},
    )

    if incident and incident.get("opened"):
        summary = (
            f"opened {incident['id']} {incident['severity']}: {incident['title']} (commander {incident['commander']})"
        )
    elif incident and incident.get("joined"):
        summary = f"joined {incident['id']}" + (
            f", severity raised to {incident['severity_raised_to']}" if incident.get("severity_raised_to") else ""
        )
    elif route == "escalate_storm":
        summary = f"alert storm: {(state.get('storm') or {}).get('alerts', 0)} alerts handed to storm_correlator"
    else:
        summary = f"{triage['decision']}: {triage['reason_code']}"
    artifacts: list[Any] = [
        {"kind": "table", "name": "triage", "ref": None, "data": {k: v for k, v in triage.items() if k != "trace"}},
        {"kind": "text", "name": "decision_trace", "ref": None, "data": "\n".join(triage.get("trace", []))},
        {
            "kind": "table",
            "name": "classification",
            "ref": None,
            "data": {
                k: cls.get(k)
                for k in (
                    "category",
                    "component",
                    "tier",
                    "kind",
                    "customer_impact",
                    "ambiguous",
                    "via",
                    "llm",
                    "reasons",
                )
            },
        },
    ]
    if incident:
        artifacts.append({"kind": "link", "name": "incident", "ref": incident["id"], "data": incident})
    storm = state.get("storm")
    if storm:
        artifacts.append({"kind": "link", "name": "storm", "ref": storm["key"], "data": storm})
    metrics = {
        "noise_probability": float(triage.get("noise_probability", 0.0)),
        "burst_count": float((state.get("ledger") or {}).get("burst_count", 0)),
        "enrichment_degraded": float(len(state.get("degraded") or [])),
        "severity_rank": float(rules.SEV_ORDER.get(str((incident or {}).get("severity") or triage["severity"]), 0)),
    }
    result = {
        "graph_id": "event_router",
        "status": status,  # type: ignore[typeddict-item]
        "event_id": event["id"],
        "correlation_id": correlation,
        "summary": summary,
        "artifacts": artifacts,
        "metrics": metrics,
        "child_threads": list(state.get("child_threads") or []),
    }
    return {"result": result, "route": route, "incident": incident}
