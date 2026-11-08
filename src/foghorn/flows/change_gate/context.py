"""Run context of ``change_gate``: every ``CTX_*`` key (the dataclass rejects unknown ones), the loop bounds and the
Studio-editable prompt of the node that answers reviewer questions."""

from __future__ import annotations

from dataclasses import dataclass, field

ANSWER_PROMPT = (
    "You answer a change reviewer's question about a proposed change to Brightcart production. Ground every statement "
    "in the tool results you are given (blast radius, runbooks, past incidents, recent changes); if they do not "
    "cover the question say so. Be brief and concrete. Never approve or reject: the reviewer decides."
)


@dataclass
class GateContext:
    model_profile: str = "competent"
    approval_policy: str = field(
        default="standard",
        metadata={"description": "strict = one extra approver level; standard / auto_low_risk = POL-005."},
    )
    scenario_id: str | None = None
    as_of: str | None = None
    token_delay_ms: int | None = None
    breakpoints: list[str] | None = None
    memory_enabled: bool | None = None
    chaos: str | None = None
    max_questions: int = field(
        default=3, metadata={"description": "How many times a reviewer may Ask before deciding."}
    )
    max_edits: int = field(default=3, metadata={"description": "How many edit-and-reassess rounds are allowed."})
    max_revisions: int = field(
        default=2, metadata={"description": "How many rejections trigger a revision; the next rejection is final."}
    )
    answer_prompt: str = field(
        default=ANSWER_PROMPT,
        metadata={
            "description": "System prompt of the model that answers reviewer questions (node answer_question).",
            "json_schema_extra": {"langgraph_nodes": ["answer_question"], "langgraph_type": "prompt"},
        },
    )
