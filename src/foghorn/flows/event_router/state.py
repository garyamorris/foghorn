"""State, input and output schemas of ``event_router`` (plain JSON only; pydantic only at the structured-output edge)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from foghorn.contracts.inputs import GraphResult, RouterInput

Input = RouterInput


def merge_enrichment(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Custom reducer for the parallel enrichment branches: union by branch name (idempotent under retries/forks)."""
    return {**(left or {}), **(right or {})}


class RouterState(TypedDict, total=False):
    event: dict[str, Any]  # the validated envelope (EventEnvelope.to_state())
    event_id: str
    scenario_id: str | None
    now_s: int  # occurred_at as epoch seconds (virtual time)
    valid: bool
    errors: list[str]
    ledger: dict[str, Any]  # facts read from the Store ledger (+ redelivery marker)
    enrichment: Annotated[dict[str, Any], merge_enrichment]  # topology / oncall / changes
    degraded: Annotated[list[str], operator.add]  # enrichment branches that failed after retries
    classification: dict[str, Any]
    triage: dict[str, Any]
    route: str
    incident: dict[str, Any] | None
    storm: dict[str, Any] | None
    child_threads: Annotated[list[str], operator.add]
    result: GraphResult


class Output(TypedDict):
    result: GraphResult
    triage: dict[str, Any]
    route: str
    incident: dict[str, Any] | None


class RouteDecision(BaseModel):
    """Structured output of the language-model fallback for ambiguous signals."""

    decision: Literal["open_incident", "escalate_human", "ignore"]
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4", "none"] = "none"
    noise_probability: float = Field(0.5, ge=0, le=1)
    rationale: str = ""
