"""Run context of the memory keeper (Studio assistant form) including the editable lesson-distiller prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

DISTILLER_PROMPT = (
    "You distil durable, reusable LESSONS from a published incident postmortem for a team memory. A lesson is one "
    "imperative sentence a future responder can act on without reading the postmortem (what to check or do, and "
    "when). Prefer the postmortem's own lessons and action items; never invent facts; skip generic advice such as "
    "'communicate early'. Also write a two-sentence incident summary. Return at most the requested number of lessons."
)


@dataclass
class KeeperContext:
    # run-context keys the dispatcher always sends (contracts/meta.py); a dataclass rejects unknown keys
    model_profile: str = "competent"
    approval_policy: str = "standard"
    token_delay_ms: int = 0
    breakpoints: list[str] | None = None
    memory_enabled: bool = True
    chaos: str | None = None
    as_of: str | None = None
    scenario_id: str | None = None
    # learning
    max_lessons: int = field(
        default=2, metadata={"description": "At most this many lesson candidates per postmortem (1-5)"}
    )
    reinforce_threshold: float = field(
        default=0.6,
        metadata={
            "description": "Semantic score at or above which a candidate reinforces an existing lesson of the same component"
        },
    )
    # consolidation
    merge_threshold: float = field(
        default=0.8,
        metadata={"description": "Semantic score at or above which two lessons of one component are duplicates"},
    )
    stale_days: int = field(
        default=540, metadata={"description": "Lessons not seen or reinforced for this long are flagged stale"}
    )
    lesson_distiller_prompt: str = field(
        default=DISTILLER_PROMPT,
        metadata={
            "description": "System prompt of the lesson distiller",
            "json_schema_extra": {"langgraph_nodes": ["distill"], "langgraph_type": "prompt"},
        },
    )
