"""Nodes of the handoff pipeline. Rules every node follows (they are what makes crash recovery and time travel boring):

* ``async def`` and Store/SDK access through the injected ``store`` / the ``ShiftSource`` port - never blocking the loop;
* IDEMPOTENT: the update is a pure function of the state it receives plus the sources, so re-running a node after a crash
  or a fork rewrites identical values; the only side effects are in ``publish`` (an upsert on a business key and an event
  with an idempotency key);
* FLAT, meaningful state keys per node (see ``state.py``);
* the optional ``crash_after`` demo: the named node computes its update, then raises ONCE (the crash is recorded in the
  Store ``("effects",)`` namespace keyed by thread and node, so a resumed or forked run does not crash again).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import get_runtime
from langgraph.store.base import BaseStore

from foghorn.common.lifecycle import emit_event
from foghorn.contracts.inputs import Artifact, GraphResult
from foghorn.data.util import to_epoch, to_iso
from foghorn.flows.postmortem_pipeline.streams import emit_safe
from foghorn.flows.shift_handoff.context import HandoffContext
from foghorn.flows.shift_handoff.metrics import (
    approval_from_thread,
    compute_metrics,
    in_window,
    incident_from_thread,
    reconcile,
)
from foghorn.flows.shift_handoff.ports import get_source
from foghorn.flows.shift_handoff.render import render_handoff
from foghorn.flows.shift_handoff.state import NODES, HandoffState
from foghorn.logging import get_logger
from foghorn.memory.api import NS_EFFECTS, claim_effect, remember

log = get_logger("handoff.nodes")


class SimulatedCrash(RuntimeError):
    """Raised by ``crash_after``; deliberately NOT retryable (no retry policy is set on these nodes)."""


_LOCAL_CRASHES: set[str] = set()  # only used when no Store is injected (bare unit tests)


def _thread_id(config: RunnableConfig) -> str:
    return str(config.get("configurable", {}).get("thread_id") or "no-thread")


async def _begin(node: str, config: RunnableConfig, store: BaseStore) -> None:
    """Count executions per (thread, node) in the Store: the proof that a resumed run did not redo finished nodes."""
    if store is None:
        return
    key = f"exec:{_thread_id(config)}:{node}"
    item = await store.aget((NS_EFFECTS,), key)
    n = int(item.value.get("executions", 0)) + 1 if item else 1
    await store.aput((NS_EFFECTS,), key, {"text": f"{node} executed {n}x", "node": node, "executions": n}, index=False)


async def _maybe_crash(node: str, state: HandoffState, config: RunnableConfig, store: BaseStore) -> None:
    if state.get("crash_after") != node:
        return
    key = f"crash:{_thread_id(config)}:{node}"
    if store is None:
        first = key not in _LOCAL_CRASHES
        _LOCAL_CRASHES.add(key)
    else:
        first = await claim_effect(store, key, {"node": node})
    if first:
        raise SimulatedCrash(
            f"simulated crash in {node!r} (thread {_thread_id(config)}): resume from the last checkpoint "
            "(invoke with input=None / Studio 'Re-run from checkpoint')"
        )


def _audit(node: str, note: str) -> dict[str, Any]:
    return {"node": node, "note": note}


# ------------------------------------------------------------------------------------------------ collect_incidents
async def collect_incidents(state: HandoffState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    node = "collect_incidents"
    await _begin(node, config, store)
    ctx = get_runtime(HandoffContext).context or HandoffContext()
    src = get_source()
    shift_end = state.get("shift_end") or await src.default_shift_end()
    hours = int(state.get("window_hours") or ctx.default_window_hours)
    window_start = to_iso(to_epoch(shift_end) - hours * 3600)
    emit_safe(
        "progress", f"handoff:{shift_end}:collect_incidents", agent=node, label="collecting incident threads", pct=10
    )

    notes: list[str] = []
    try:
        threads = await src.incident_threads()
    except Exception as exc:  # the server may be mid-restart: the handoff still renders from the data layer
        log.warning("incident_threads_failed", error=repr(exc)[:200])
        threads, notes = [], [f"incident threads unavailable ({type(exc).__name__})"]
    latest: dict[str, dict[str, Any]] = {}
    for t in threads:
        inc = incident_from_thread(t)
        if inc is not None:
            latest.setdefault(inc["incident_id"], inc)  # threads arrive newest first
    for inc in latest.values():
        if not inc.get("opened_at") and inc.get("scenario_id"):
            inc["opened_at"] = await src.scenario_open_time(str(inc["scenario_id"]))
    incidents = sorted(
        (i for i in latest.values() if in_window(i, window_start, shift_end)),
        key=lambda i: (i["severity"], i["incident_id"]),
    )
    try:
        alerts = await src.alert_history(window_start, shift_end)
    except Exception as exc:
        log.warning("alert_history_failed", error=repr(exc)[:200])
        alerts, notes = [], [*notes, f"alert history unavailable ({type(exc).__name__})"]
    source = "sdk" if incidents else "data_layer"
    note = f"{len(incidents)} incident(s) from {source}; {len(alerts)} alert(s) in {window_start} -> {shift_end}"
    update: dict[str, Any] = {
        "shift_end": shift_end,
        "window_hours": hours,
        "window_start": window_start,
        "source": source,
        "incidents": incidents,
        "alert_history": alerts,
        "audit": [_audit(node, "; ".join([note, *notes]))],
    }
    await _maybe_crash(node, state, config, store)
    return update


# ------------------------------------------------------------------------------------------------ collect_approvals
async def collect_approvals(state: HandoffState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    node = "collect_approvals"
    await _begin(node, config, store)
    emit_safe(
        "progress",
        f"handoff:{state['shift_end']}:collect_approvals",
        agent=node,
        label="collecting pending approvals",
        pct=30,
    )
    try:
        threads = await get_source().interrupted_threads()
        note_extra = ""
    except Exception as exc:
        log.warning("interrupted_threads_failed", error=repr(exc)[:200])
        threads, note_extra = [], f" (thread search unavailable: {type(exc).__name__})"
    approvals = sorted(
        (a for t in threads for a in approval_from_thread(t)), key=lambda a: (a["approval_id"], a["thread_id"])
    )
    update = {"approvals": approvals, "audit": [_audit(node, f"{len(approvals)} pending approval(s){note_extra}")]}
    await _maybe_crash(node, state, config, store)
    return update


# ------------------------------------------------------------------------------------------------- compute_metrics
async def compute_metrics_node(state: HandoffState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    node = "compute_metrics"
    await _begin(node, config, store)
    metrics = compute_metrics(
        state.get("incidents") or [],
        state.get("approvals") or [],
        state.get("alert_history") or [],
        shift_end=state["shift_end"],
        window_hours=state["window_hours"],
    )
    emit_safe(
        "metric",
        f"handoff:{state['shift_end']}:open",
        agent=node,
        name="incidents_open",
        value=metrics["incidents_open"],
    )
    emit_safe(
        "metric",
        f"handoff:{state['shift_end']}:approvals",
        agent=node,
        name="approvals_pending",
        value=metrics["approvals_pending"],
    )
    update = {
        "metrics": metrics,
        "audit": [
            _audit(
                node,
                f"{int(metrics['incidents_open'])} open, {int(metrics['incidents_resolved'])} resolved, {int(metrics['alerts_total'])} alerts",
            )
        ],
    }
    await _maybe_crash(node, state, config, store)
    return update


# --------------------------------------------------------------------------------------------------------- reconcile
async def reconcile_node(state: HandoffState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    node = "reconcile"
    await _begin(node, config, store)
    ctx = get_runtime(HandoffContext).context or HandoffContext()
    findings = reconcile(
        state.get("incidents") or [],
        state.get("approvals") or [],
        state.get("alert_history") or [],
        shift_end=state["shift_end"],
        stale_sev1_minutes=ctx.stale_sev1_minutes,
    )
    for f in findings:
        emit_safe("note", f"handoff:{state['shift_end']}:{f['id']}", agent=node, text=f["text"])
    update = {"findings": findings, "audit": [_audit(node, f"{len(findings)} finding(s)")]}
    await _maybe_crash(node, state, config, store)
    return update


# ----------------------------------------------------------------------------------------------------- render_report
async def render_report(state: HandoffState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    node = "render_report"
    await _begin(node, config, store)
    audit = [*(state.get("audit") or []), _audit(node, "markdown rendered")]
    report = render_handoff({**state, "audit": audit})
    emit_safe(
        "artifact",
        f"handoff:{state['shift_end']}:report",
        agent=node,
        name=f"shift-handoff-{state['shift_end']}",
        kind="text",
        ref=None,
    )
    update = {"report_md": report, "audit": [audit[-1]]}
    await _maybe_crash(node, state, config, store)
    return update


# --------------------------------------------------------------------------------------------------------- publish
async def publish(state: HandoffState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    node = "publish"
    await _begin(node, config, store)
    tid = _thread_id(config)
    shift_end, hours = state["shift_end"], state["window_hours"]
    hid = "handoff-" + shift_end.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z") + f"-{hours}h"
    open_ids = [i["incident_id"] for i in state.get("incidents") or [] if i["status"] != "resolved"]
    pending_ids = [a["approval_id"] for a in state.get("approvals") or []]
    await _maybe_crash(
        node, state, config, store
    )  # BEFORE the side effects: a crash here leaves nothing half-published

    executions: dict[str, int] = {}
    if store is not None:
        for n in NODES:
            item = await store.aget((NS_EFFECTS,), f"exec:{tid}:{n}")
            executions[n] = int(item.value["executions"]) if item else 0
    first = await claim_effect(store, f"handoff.publish:{hid}", {"handoff_id": hid, "thread_id": tid})
    value = {
        "text": state["report_md"],
        "handoff_id": hid,
        "shift_end": shift_end,
        "window_start": state["window_start"],
        "window_hours": hours,
        "source": state["source"],
        "metrics": state["metrics"],
        "open_incidents": open_ids,
        "pending_approvals": pending_ids,
        "provenance": {"thread_id": tid, "event_id": None, "incident_id": None, "written_by": "shift_handoff"},
    }
    stored = await remember(store, "handoffs", hid, value)
    emit_event(
        "handoff.published",
        idempotency_key=f"handoff.published:{hid}",
        payload={
            "handoff_id": hid,
            "shift_end": shift_end,
            "window_hours": hours,
            "source": state["source"],
            "metrics": state["metrics"],
            "open_incidents": open_ids,
            "pending_approvals": pending_ids,
            "thread_id": tid,
            "store": {"namespace": ["handoffs"], "key": hid},
        },
    )
    if stored:
        emit_safe("memory.written", f"handoff:{hid}:memory", agent=node, ns=["handoffs"], key=hid)
    m = state["metrics"]
    summary = (
        f"Handoff {hid}: {int(m['incidents_open'])} open, {int(m['incidents_resolved'])} resolved, "
        f"{int(m['approvals_pending'])} pending approval(s), {int(m['alerts_total'])} alert(s) in the last {hours} h."
    )
    handoff = {
        "handoff_id": hid,
        "namespace": ["handoffs"],
        "key": hid,
        "stored": stored,
        "executions": executions,
        "first": first,
    }
    result = GraphResult(
        graph_id="shift_handoff",
        status="ok" if first else "duplicate",
        event_id=None,
        correlation_id=None,
        summary=summary,
        artifacts=[Artifact(kind="report", name=f"{hid}.md", ref=f"store:handoffs/{hid}", data=state["report_md"])],
        metrics=dict(m),
        child_threads=[],
    )
    emit_safe("progress", f"handoff:{hid}:published", agent=node, label="handoff published", pct=100)
    return {
        "handoff": handoff,
        "result": result,
        "audit": [_audit(node, f"stored {stored}, first publication {first}")],
    }
