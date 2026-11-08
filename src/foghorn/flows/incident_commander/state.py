"""Commander state: flat, plain-JSON and fork-friendly (docs/design/03 section 7.4 item 4).

Editable by a human in Studio (``update_state`` goes through the reducers below): ``hypotheses[0]`` (F1),
``incident.severity`` (F2: changes the approvers required), ``chosen_action`` (breakpoint demo), ``as_of`` (F4: the time
fence). Replay from ``approval_gate`` with another decision is F3.

Keys shared with the specialist sub-agents (``scenario_id``, ``as_of``, ``applied_actions``, ``evidence``, ``agent_runs``)
use idempotent / last-wins reducers so parallel branches and sub-agents never double count or collide.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from langchain.agents import AgentState
from typing_extensions import TypedDict

from foghorn.contracts.inputs import ApprovalRecord, CommanderReport, GraphResult, IncidentRef
from foghorn.flows.rca_tournament.facts import merge_by_id
from foghorn.flows.rca_tournament.state import last_wins
from foghorn.tools.context import add_actions
from foghorn.tools.lc import ToolState


def union_list(old: list[str] | None, new: list[str] | None) -> list[str]:
    """Reducer for flag lists: order-preserving union (parallel branches both add flags)."""
    return list(dict.fromkeys([*(old or []), *(new or [])]))


class State(TypedDict, total=False):
    # --- input (CommanderInput) ----------------------------------------------------------------------------------------------
    scenario_id: Annotated[str, last_wins]
    incident: IncidentRef
    as_of: Annotated[str | None, last_wins]
    alerts: list[dict[str, Any]]
    correlation_id: str | None
    trigger_event_ids: list[str]
    # --- the world the tools read -------------------------------------------------------------------------------------------
    applied_actions: Annotated[list[dict[str, Any]], add_actions]
    # --- bootstrap ---------------------------------------------------------------------------------------------------------
    scenario_meta: dict[str, Any]
    budget: dict[str, int]
    focus: list[str]
    settled: bool
    memory_hits: list[dict[str, Any]]
    memory_recalled: list[str]
    prefs: list[dict[str, Any]]
    directives: dict[str, Any]
    # --- investigation -----------------------------------------------------------------------------------------------------
    evidence: Annotated[list[dict[str, Any]], merge_by_id]
    agent_runs: Annotated[list[dict[str, Any]], merge_by_id]
    candidates: list[str]
    hypotheses: list[dict[str, Any]]
    red_herrings: list[dict[str, Any]]
    route: dict[str, Any]
    supervisor_hops: int
    rca_runs: int
    clarified: bool
    qa_log: Annotated[list[dict[str, Any]], merge_by_id]
    # --- decision ----------------------------------------------------------------------------------------------------------
    diagnosis: dict[str, Any]
    plan: list[dict[str, Any]]
    proposed: Annotated[list[dict[str, Any]], merge_by_id]
    chosen_action: dict[str, Any]
    policy: dict[str, Any]
    approvals: Annotated[list[ApprovalRecord], merge_by_id]
    approved: dict[str, Any]
    gate_next: str
    pending_question: dict[str, Any]
    reviewed: list[str]
    rejected: list[str]
    replans: int
    edits: int
    asks: int
    verifies: int
    executed: Annotated[list[dict[str, Any]], merge_by_id]
    verification: dict[str, Any]
    # --- communication ---------------------------------------------------------------------------------------------------
    comms_draft: str
    comms: dict[str, Any]
    # --- outcome -----------------------------------------------------------------------------------------------------------
    flags: Annotated[list[str], union_list]
    interrupts_seen: Annotated[list[str], operator.add]
    memory_written: list[str]
    recovered: bool | None
    report: CommanderReport
    result: GraphResult


class CommanderIO(TypedDict):
    """Output schema (``CommanderOutput`` of the contracts, re-declared so the graph module owns its schema)."""

    result: GraphResult
    report: CommanderReport


class SpecialistState(ToolState):
    """State of the five ``create_agent`` specialists: ``ToolState`` (messages, scenario_id, as_of, applied_actions) plus
    the shared evidence/run keys. Anything else a specialist writes stays private to it."""

    evidence: Annotated[list[dict[str, Any]], merge_by_id]
    agent_runs: Annotated[list[dict[str, Any]], merge_by_id]


__all__ = ["AgentState", "CommanderIO", "SpecialistState", "State", "union_list"]
