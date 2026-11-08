"""Input and result schemas of every graph. These are the ``input_schema`` / ``output_schema`` building blocks.

Rules (docs/design/02, gotchas 6-7): plain JSON types only in anything checkpointed (``str`` timestamps, never
``datetime``; no tuples; no dataclasses nested in pydantic state). Fields may be ADDED by graph owners (optional
only) but never renamed or removed: the dispatcher, the eval runner and the dashboard build these inputs.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired

from typing_extensions import TypedDict

Sev = Literal["SEV1", "SEV2", "SEV3", "SEV4"]


class IncidentRef(TypedDict):
    id: str  # INC-xxxx
    severity: Sev
    title: str
    status: NotRequired[Literal["open", "mitigating", "monitoring", "resolved"]]


class Artifact(TypedDict):
    kind: Literal["text", "table", "report", "link", "diff", "series"]
    name: str
    ref: str | None
    data: Any


class GraphResult(TypedDict):
    """Every handler graph exposes ``result: GraphResult`` in its output schema; the dispatcher stores it."""

    graph_id: str
    status: Literal["ok", "needs_approval", "failed", "skipped", "duplicate"]
    event_id: str | None
    correlation_id: str | None
    summary: str
    artifacts: list[Artifact]
    metrics: dict[str, float]
    child_threads: list[str]


# --- graph inputs ----------------------------------------------------------------------------------------------------
class RouterInput(TypedDict):
    event: dict[str, Any]  # EventEnvelope.to_state()


class CommanderInput(TypedDict):
    scenario_id: str
    incident: IncidentRef
    as_of: NotRequired[str | None]  # ISO virtual time; None => scenario's default "now" (onset + detection lag)
    alerts: NotRequired[list[dict[str, Any]]]  # EventEnvelope.to_state() of the triggering alerts
    correlation_id: NotRequired[str | None]
    trigger_event_ids: NotRequired[list[str]]


class RcaInput(TypedDict):
    scenario_id: str
    as_of: NotRequired[str | None]
    evidence: NotRequired[list[dict[str, Any]]]
    candidates: NotRequired[list[str]]  # component ids; empty => derive from topology around alerts


class StormInput(TypedDict):
    scenario_id: str
    alerts: list[dict[str, Any]]
    as_of: NotRequired[str | None]
    window_minutes: NotRequired[int]


class ChangeRequest(TypedDict):
    id: str
    kind: Literal["deploy", "flag", "config", "migration", "scale"]
    component: str
    description: str
    risk: Literal["low", "medium", "high", "critical"]
    requested_by: str
    environment: NotRequired[str]
    diff: NotRequired[dict[str, Any]]


class ChangeGateInput(TypedDict):
    change: ChangeRequest
    mode: NotRequired[Literal["standard", "gauntlet"]]  # gauntlet exercises every interrupt style in one run
    requested_at: NotRequired[str]  # ISO virtual time of the request (freeze-window checks)
    scenario_id: NotRequired[str | None]
    incident_id: NotRequired[str | None]


class PostmortemInput(TypedDict):
    incident_id: str
    scenario_id: str
    source_thread_id: NotRequired[str | None]  # the commander thread to read state/history from


class ShiftHandoffInput(TypedDict, total=False):
    shift_end: str  # ISO virtual time
    window_hours: int
    crash_after: str | None  # node name to raise in once, to demo crash recovery from the last checkpoint


class MemoryKeeperInput(TypedDict, total=False):
    mode: Literal["learn", "consolidate"]
    incident_id: str
    scenario_id: str
    postmortem: dict[str, Any]


class EvalRunnerInput(TypedDict, total=False):
    suite: Literal["core16", "sweep60", "custom"]
    scenario_ids: list[str]
    profile: Literal["competent", "naive", "cautious", "gullible"]
    concurrency: int
    baseline_id: str | None
    max_scenarios: int | None
    eval_run_id: str  # the dashboard's evr_NNNN (the eval row the events layer created)
    label: str | None


# --- incident_commander output ---------------------------------------------------------------------------------------
class Hypothesis(TypedDict):
    id: str
    component: str
    category: str
    score: float
    evidence: list[
        str
    ]  # evidence refs: "metric:checkout-service/error_rate", "change:chg-check-9247", "log:...", "memory:les-001"


class ApprovalRecord(TypedDict):
    id: str
    kind: str  # InterruptKind
    status: Literal["pending", "approved", "rejected", "edited", "asked", "answered"]
    decided_by: str | None
    action: str | None  # "rollback_deploy:checkout-service"


class RootCauseClaim(TypedDict):
    component: str
    category: str
    confidence: float
    evidence_refs: list[str]


class CommanderReport(TypedDict):
    """What evaluators, the postmortem pipeline and the dashboard read. Lives in the commander's state under
    ``report`` (so ``threads.get_state(...)["values"]["report"]`` works) and is also in its output schema."""

    scenario_id: str
    incident_id: str
    severity: Sev
    root_cause: RootCauseClaim | None  # None when the commander abstained
    abstained: bool
    hypotheses: list[Hypothesis]
    evidence_refs: list[str]
    actions_proposed: list[dict[str, Any]]  # {action, target, params, risk}
    actions_executed: list[dict[str, Any]]  # {action, target, params, t_virtual, outcome}
    recovered: bool | None
    approvals: list[ApprovalRecord]
    interrupts_seen: list[str]  # InterruptKind values in the order raised
    comms_draft: str
    memory_recalled: list[str]
    memory_written: list[str]
    flags: list[
        str
    ]  # injection_detected | abstained | policy_blocked | budget_exceeded | replanned | red_herring_rejected
    tool_calls_used: int
    supervisor_hops: int
    narrative: str


class CommanderOutput(TypedDict):
    result: GraphResult
    report: CommanderReport
