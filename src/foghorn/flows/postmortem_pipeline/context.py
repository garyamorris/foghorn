"""Run context of the pipeline (Studio "assistant" form). ``context_schema`` works on a Functional API ``@entrypoint`` in
langgraph 1.2.20 (verified: ``get_context_jsonschema()`` is populated and ``runtime.context`` reaches the entrypoint).

The ``langgraph_nodes`` metadata of the two prompts names the ONE node Studio draws for a functional workflow
(``postmortem_pipeline``); whether Studio puts the prompt gear on it is UNVERIFIED in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DRAFT_PROMPT = (
    "You are an SRE writing a BLAMELESS incident postmortem. Use ONLY the facts bundle: the commander's report, the "
    "timeline and the on-call data. Never invent numbers, names or causes. Name the commander's root-cause component, "
    "keep every key timeline entry, quantify impact from the alert data, and give every action item an owner id from "
    "the on-call data. If the root cause is not established, say so."
)
REVIEW_PROMPT = (
    "You are the postmortem reviewer. Judge the draft against the facts bundle: is the root cause the commander's, is "
    "the timeline complete, is the impact quantified, does every action item have an on-call owner, is the tone "
    "blameless. Return issues with stable ids; mark an issue a blocker only if a human should not see the draft yet."
)


@dataclass
class PostmortemContext:
    # run-context keys the dispatcher always sends (contracts/meta.py); a dataclass rejects unknown keys
    model_profile: str = "competent"
    approval_policy: str = "standard"
    token_delay_ms: int = 0
    breakpoints: list[str] | None = None
    memory_enabled: bool = True
    chaos: str | None = None
    as_of: str | None = None
    scenario_id: str | None = None
    # pipeline settings
    max_revisions: int = field(
        default=2, metadata={"description": "Revise rounds after the first draft (hard-capped at 2)"}
    )
    draft_prompt: str = field(
        default=DRAFT_PROMPT,
        metadata={
            "description": "System prompt of the postmortem drafting model",
            "json_schema_extra": {"langgraph_nodes": ["postmortem_pipeline"], "langgraph_type": "prompt"},
        },
    )
    review_prompt: str = field(
        default=REVIEW_PROMPT,
        metadata={
            "description": "System prompt of the postmortem critic (judge role)",
            "json_schema_extra": {"langgraph_nodes": ["postmortem_pipeline"], "langgraph_type": "prompt"},
        },
    )
