"""Structured-output schemas of the commander's internal (``nostream``) model calls."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal["metrics_analyst", "logs_analyst", "change_analyst", "topology_analyst", "knowledge_agent"]


class Route(BaseModel):
    """The supervisor's decision for the next hop. Code validates it (``enforce_route``): the model proposes, the graph disposes."""

    next: Literal["investigate", "rca_tournament", "clarify_change_intent", "remediation_planner"]
    agents: list[AgentName] = Field(
        default_factory=list, description="specialists to run in parallel when next == investigate"
    )
    services: list[str] = Field(default_factory=list, description="component ids the specialists should look at")
    lookback_days: int = Field(0, description="widen the change search (days) for slow leaks")
    reason: str = Field(description="one sentence with the evidence behind the decision")


class ProposedAction(BaseModel):
    action: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    basis: Literal["evidence", "runbook", "memory", "instruction"] = "evidence"


class PlanProposal(BaseModel):
    """The planner's answer: the actions it chose among the grounded options (or, if it deviates, its own)."""

    actions: list[ProposedAction] = Field(default_factory=list)
    summary: str = ""
