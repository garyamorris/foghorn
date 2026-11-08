"""Run context (Studio assistant "configurable" form) of ``eval_runner``."""

from __future__ import annotations

from dataclasses import dataclass, field

from foghorn.evals.evaluators import DEFAULT_JUDGE_INSTRUCTIONS


@dataclass
class EvalContext:
    model_profile: str = field(
        default="competent",
        metadata={"description": "Default commander model profile when the input does not set `profile`."},
    )
    approval_policy: str = field(
        default="standard", metadata={"description": "Approval policy passed to every commander run."}
    )
    scenario_id: str | None = field(
        default=None, metadata={"description": "Unused: accepted so a dispatcher-built run context loads."}
    )
    as_of: str | None = field(
        default=None, metadata={"description": "Unused: every commander run starts at its scenario's default time."}
    )
    breakpoints: list[str] = field(default_factory=list, metadata={"description": "Unused (dispatcher compatibility)."})
    memory_enabled: bool = field(
        default=True,
        metadata={"description": "Forwarded to every commander run (False = the W5 'without memory' baseline)."},
    )
    chaos: str | None = field(
        default=None, metadata={"description": "Forwarded to every commander run ('tools' = transient tool failures)."}
    )
    token_delay_ms: int = field(default=0, metadata={"description": "Unused: eval runs never pace tokens."})
    max_steps: int = field(
        default=8, metadata={"description": "Hard limit of interrupt rounds the approver oracle answers per scenario."}
    )
    scenario_timeout_s: float = field(
        default=240.0,
        metadata={"description": "Wall-clock limit for one scenario (commander run + all its interrupts)."},
    )
    judge_prompt: str = field(
        default=DEFAULT_JUDGE_INSTRUCTIONS,
        metadata={
            "description": "Instructions of the LLM-as-judge that grades the commander's status-page draft (the FACTS/ANSWER tail is appended by code).",
            "json_schema_extra": {"langgraph_nodes": ["run_scenario"], "langgraph_type": "prompt"},
        },
    )
