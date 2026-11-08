"""The structured final answer of the copilot: the ops brief (``structured_response``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OpsBrief(BaseModel):
    """A short, citable brief for the on-call: what is wrong, how sure we are, what to do next."""

    headline: str = Field(description="One sentence: what is wrong and where.")
    scenario_id: str | None = Field(default=None, description="The scenario / incident this brief is about.")
    suspected_component: str | None = Field(
        default=None, description="Component id of the suspected origin, if there is one."
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="0-1; low when evidence is thin or telemetry is missing."
    )
    findings: list[str] = Field(
        default_factory=list, description="Facts taken from tool results (numbers must come from tools)."
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Proposed remediations as 'action:target'; none are executed without a human review.",
    )
    open_questions: list[str] = Field(default_factory=list, description="What is still unknown.")
    citations: list[str] = Field(
        default_factory=list,
        description="Evidence refs: metric:svc/metric, change:id, log:svc:pattern, runbook:RB-xxx, memory:key.",
    )
    flags: list[str] = Field(default_factory=list, description="e.g. prompt_injection_detected, telemetry_flatline.")


def wants_brief(text: str) -> bool:
    """A brief is produced when the operator asks for one ('give me an ops brief', 'write the brief')."""
    return "brief" in text.lower()
