"""Starting an ``incident_commander`` run: the ONE place that knows the commander's thread, input and metadata.

Used by ``event_router`` (open_incident) and ``storm_correlator`` (one incident per cluster).  The commander thread id
is pinned to the BUSINESS key (``det_thread_id("incident_commander", incident_id)``): any number of alerts, retries or
correlator runs for one incident share one thread, and ``delegate``'s 409/"already started" handling makes the second
start a no-op.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from foghorn.common.delegate import DelegateResult, delegate
from foghorn.contracts.inputs import CommanderInput
from foghorn.contracts.meta import (
    M_CORRELATION_ID,
    M_EVENT_ID,
    M_INCIDENT_ID,
    M_SCENARIO_ID,
    M_TRIGGER,
    det_thread_id,
    thread_metadata,
)

COMMANDER = "incident_commander"


def commander_thread_id(incident_id: str) -> str:
    return det_thread_id(COMMANDER, incident_id)


def commander_context(
    *,
    scenario_id: str | None,
    model_profile: str,
    approval_policy: str,
    memory_enabled: bool | None = None,
    chaos: str | None = None,
    token_delay_ms: int | None = None,
    breakpoints: list[str] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run context forwarded to the commander (``contracts.meta`` ``CTX_*`` keys; ``None`` values are dropped)."""
    raw: dict[str, Any] = {
        "scenario_id": scenario_id,
        "model_profile": model_profile,
        "approval_policy": approval_policy,
        "memory_enabled": memory_enabled,
        "chaos": chaos,
        "token_delay_ms": token_delay_ms,
        "breakpoints": breakpoints or None,
        "as_of": as_of,
    }
    return {k: v for k, v in raw.items() if v is not None}


def commander_input(
    *,
    scenario_id: str,
    incident_id: str,
    severity: str,
    title: str,
    as_of: str | None,
    alerts: list[dict[str, Any]],
    correlation_id: str,
    trigger_event_ids: list[str],
) -> CommanderInput:
    out: CommanderInput = {
        "scenario_id": scenario_id,
        "incident": {"id": incident_id, "severity": severity, "title": title, "status": "open"},  # type: ignore[typeddict-item]
        "alerts": alerts,
        "correlation_id": correlation_id,
        "trigger_event_ids": trigger_event_ids,
    }
    if as_of:
        out["as_of"] = as_of
    return out


def commander_metadata(
    *, incident_id: str, correlation_id: str, scenario_id: str | None, event_id: str | None
) -> dict[str, Any]:
    return thread_metadata(
        "incident",
        COMMANDER,
        **{
            M_INCIDENT_ID: incident_id,
            M_CORRELATION_ID: correlation_id,
            M_SCENARIO_ID: scenario_id,
            M_EVENT_ID: event_id,
            M_TRIGGER: "delegate",
        },
    )


def parent_config(config: RunnableConfig, fallback_thread: str) -> RunnableConfig:
    """``delegate`` derives the child from the parent thread; runs without a thread (ad hoc ``invoke``) get a stand-in."""
    if (config.get("configurable") or {}).get("thread_id"):
        return config
    return {**config, "configurable": {**(config.get("configurable") or {}), "thread_id": fallback_thread}}


async def start_commander(
    *,
    config: RunnableConfig,
    incident_id: str,
    correlation_id: str,
    severity: str,
    title: str,
    scenario_id: str,
    event_id: str | None,
    as_of: str | None,
    alerts: list[dict[str, Any]],
    trigger_event_ids: list[str],
    context: dict[str, Any],
) -> DelegateResult:
    """Fire-and-forget the commander on its pinned thread (idempotent: a second call joins the first)."""
    return await delegate(
        graph_id=COMMANDER,
        key=incident_id,
        config=parent_config(config, f"router-{event_id or incident_id}"),
        mode="background",
        child_thread_id=commander_thread_id(incident_id),
        input=dict(
            commander_input(
                scenario_id=scenario_id,
                incident_id=incident_id,
                severity=severity,
                title=title,
                as_of=as_of,
                alerts=alerts,
                correlation_id=correlation_id,
                trigger_event_ids=trigger_event_ids,
            )
        ),
        context=context,
        metadata=commander_metadata(
            incident_id=incident_id, correlation_id=correlation_id, scenario_id=scenario_id, event_id=event_id
        ),
    )
