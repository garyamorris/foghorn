"""The one event envelope every producer, the bus, the router graph and the UI agree on (`foghorn.event/1`).

Distinct identities (never conflate them):
* ``id``               - this event
* ``idempotency_key``  - a client RETRY of the same delivery
* ``dedupe_key``       - a DIFFERENT event that says the same thing (semantic duplicate)
* ``correlation_id``   - all events about one incident
* ``causation_id``     - the event/step that caused this one

Timestamps are ISO-8601 UTC *strings* (not ``datetime``): checkpointed state must stay plain JSON.
``occurred_at`` is VIRTUAL time (the simulated world's clock); ``received_at`` is real server time.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "foghorn.event/1"

Severity = Literal["info", "low", "medium", "high", "critical"]
SEVERITY_ORDER: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: Ingress signals, change feed, lifecycle (emitted by Foghorn itself) and system events.
SIGNAL_TYPES = frozenset({"alert.fired", "alert.resolved", "slo.burn_rate", "healthcheck.failed", "customer.report"})
CHANGE_TYPES = frozenset(
    {
        "deploy.started",
        "deploy.finished",
        "deploy.rolled_back",
        "config.changed",
        "flag.changed",
        "migration.applied",
        "maintenance.window.started",
        "maintenance.window.ended",
        "change.requested",
    }
)
LIFECYCLE_TYPES = frozenset(
    {
        "triage.completed",
        "incident.opened",
        "incident.updated",
        "incident.resolved",
        "run.started",
        "run.interrupted",
        "run.finished",
        "run.failed",
        "approval.requested",
        "approval.decided",
        "action.executed",
        "action.verified",
        "memory.recalled",
        "memory.written",
        "postmortem.published",
        "eval.requested",
        "eval.progress",
        "eval.finished",
        "handoff.published",
    }
)
SYSTEM_TYPES = frozenset(
    {
        "system.cron.tick",
        "event.suppressed",
        "event.shed",
        "dlq.added",
        "dlq.retried",
        "replay.started",
        "replay.finished",
    }
)
ALL_TYPES = SIGNAL_TYPES | CHANGE_TYPES | LIFECYCLE_TYPES | SYSTEM_TYPES


class Subject(BaseModel):
    model_config = ConfigDict(extra="allow")
    component: str | None = None
    environment: str = "prod"


class Trace(BaseModel):
    """Provenance of simulated events (replay stream position, owning scenario)."""

    model_config = ConfigDict(extra="allow")
    stream: str | None = None
    seq: int | None = None
    scenario_id: str | None = None


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["foghorn.event/1"] = "foghorn.event/1"
    id: str | None = None  # server-assigned on ingest when absent; replay uses evt_<stream>_<seq>
    type: str
    source: str = "ui.inject"  # alertmanager | deploybot | statuspage | feature-flags | cron | replay:<stream> | ui.inject | foghorn.internal
    severity: Severity = "info"
    occurred_at: str | None = None  # VIRTUAL time; server fills from the virtual clock when absent
    received_at: str | None = None  # real time, set by the server
    subject: Subject = Field(default_factory=Subject)
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = None
    idempotency_key: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    trace: Trace = Field(default_factory=Trace)
    attributes: dict[str, Any] = Field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        """Plain-JSON form that is safe to put in graph state / checkpoints."""
        return self.model_dump(mode="json", exclude_none=False)
