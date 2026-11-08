"""State of the handoff pipeline: FLAT and meaningful after every node, so Studio's "edit node state" and time travel are
useful (edit ``incidents`` after ``collect_incidents`` and fork, or edit ``report_md`` before ``publish``).

``audit`` is a reducer-backed log. It uses ``DeltaChannel`` (beta): checkpoints store only the delta of each step instead of
the whole growing list; langgraph 1.2.20 replays the ancestor writes through :func:`append_audit` to rebuild the value
(verified in-process and on ``langgraph dev``). ``append_audit`` is batching-invariant as ``DeltaChannel`` requires.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from langgraph.channels import DeltaChannel
from langgraph.types import Overwrite
from typing_extensions import TypedDict

from foghorn.contracts.inputs import GraphResult

NODES = ("collect_incidents", "collect_approvals", "compute_metrics", "reconcile", "render_report", "publish")


def append_audit(state: list[dict[str, Any]] | None, writes: Sequence[Any]) -> list[dict[str, Any]]:
    """Append each written entry (or list of entries); an ``Overwrite`` replaces the log (Studio state edits)."""
    out = list(state or [])
    for w in writes:
        if isinstance(w, Overwrite):
            out = list(w.value)
        elif isinstance(w, list):
            out.extend(w)
        else:
            out.append(w)
    return out


class HandoffState(TypedDict, total=False):
    # --- input -------------------------------------------------------------------------------------------------
    shift_end: str
    window_hours: int
    crash_after: str | None
    # --- collect_incidents -------------------------------------------------------------------------------------
    window_start: str
    source: str  # "sdk" (threads found) | "data_layer" (nothing tracked: alert history only)
    incidents: list[dict[str, Any]]
    alert_history: list[dict[str, Any]]
    # --- collect_approvals -------------------------------------------------------------------------------------
    approvals: list[dict[str, Any]]
    # --- compute_metrics / reconcile / render_report / publish --------------------------------------------------
    metrics: dict[str, float]
    findings: list[dict[str, Any]]
    report_md: str
    handoff: dict[str, Any]
    audit: Annotated[list[dict[str, Any]], DeltaChannel(append_audit)]
    result: GraphResult


class HandoffOutput(TypedDict):
    result: GraphResult
    handoff: dict[str, Any]
    report_md: str
