"""State, input and output of ``change_gate`` (plain JSON, flat and fork-friendly: edit ``change`` or ``risk`` in Studio)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired

from typing_extensions import TypedDict

from foghorn.contracts.inputs import ChangeGateInput, GraphResult

Mode = Literal["standard", "gauntlet"]


class Input(ChangeGateInput):
    """``ChangeGateInput`` plus optional fields (add-only, per contracts/inputs.py)."""

    requested_at: NotRequired[str]  # ISO virtual time of the request (drives the change-freeze check)
    scenario_id: NotRequired[str | None]
    incident_id: NotRequired[str | None]


def merge_reviews(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Reducer for the parallel four-eyes reviews: one entry per approval id (a replay overwrites, never duplicates)."""
    by_id = {r["approval_id"]: r for r in (left or [])}
    for r in right or []:
        by_id[r["approval_id"]] = r
    return sorted(by_id.values(), key=lambda r: r["approval_id"])


class GateState(TypedDict, total=False):
    change: dict[str, Any]  # the CURRENT request (edits and revisions replace it)
    original: dict[str, Any]
    mode: str
    scenario_id: str | None
    incident_id: str | None
    requested_at: str
    risk: dict[str, Any]
    policy: dict[str, Any]
    turn: int  # human control points so far (part of every approval id)
    edits: int
    revisions: int
    questions: int
    clarified: bool
    break_glass_done: bool
    approved_by: list[str]  # approvers so far in this round (replaced, not appended, when edits/revisions reset it)
    remaining: list[str]  # gauntlet challenges (ask / edit / reject) not yet completed
    hint: str | None
    last_decision: dict[str, Any]
    qa: Annotated[list[dict[str, Any]], operator.add]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    edit_log: Annotated[list[dict[str, Any]], operator.add]
    reviews: Annotated[list[dict[str, Any]], merge_reviews]
    status: str  # pending | applied | rejected | blocked | duplicate
    applied: dict[str, Any] | None
    result: GraphResult


class Output(TypedDict):
    result: GraphResult
    status: str
    change: dict[str, Any]
