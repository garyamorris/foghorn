"""Interrupt payloads and resume schemas: the human-in-the-loop contract between graphs, the dashboard and Studio.

Every ``interrupt()`` in Foghorn passes ``build_interrupt(...)`` as its value and a matching pydantic model as
``response_schema`` (langgraph 1.2 validates the resume value against it; invalid input leaves the interrupt
answerable). The dashboard generates its approve/edit/reject/ask forms from the schema, so adding a field here
changes the UI without UI code.

Remember (docs/design/02, gotcha 14): ``interrupt()`` re-runs the node from line 1 on resume. Side effects must
come AFTER the interrupt and be idempotent; custom stream events before it need deterministic ``key`` s.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

InterruptKind = Literal["approval", "edit", "question", "break_glass", "memory_write", "tool_review", "four_eyes"]
Risk = Literal["low", "medium", "high", "critical"]
PAYLOAD_VERSION = 1


class PolicyCheck(BaseModel):
    id: str
    status: Literal["pass", "warn", "fail"]
    text: str


class Decision(BaseModel):
    """Resume value for approval-like interrupts (approval, break_glass, four_eyes, tool_review)."""

    decision: Literal["approve", "reject", "edit", "ask"]
    decided_by: str = "operator"
    note: str = ""
    edits: dict[str, Any] = Field(default_factory=dict)  # used when decision == "edit"
    question: str = ""  # used when decision == "ask"


class Answer(BaseModel):
    """Resume value for a question interrupt (an agent asks the human for information)."""

    answer: str
    decided_by: str = "operator"


class MemoryDecision(BaseModel):
    """Resume value for memory_write interrupts: a human curates what the system will remember."""

    decision: Literal["save", "edit", "discard"]
    text: str | None = None
    decided_by: str = "operator"


RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "approval": Decision,
    "edit": Decision,
    "break_glass": Decision,
    "four_eyes": Decision,
    "tool_review": Decision,
    "question": Answer,
    "memory_write": MemoryDecision,
}


def build_interrupt(
    kind: InterruptKind,
    *,
    approval_id: str,
    title: str,
    summary: str,
    risk: Risk = "medium",
    reversible: bool = True,
    blast_radius: list[str] | None = None,
    policy_checks: list[PolicyCheck] | None = None,
    approvers_required: int = 1,
    roles: list[str] | None = None,
    incident_id: str | None = None,
    evidence_refs: list[str] | None = None,
    proposed: dict[str, Any] | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """The first argument to ``interrupt()``. Plain JSON only (it is checkpointed and streamed)."""
    default_options = ["answer"] if kind == "question" else ["approve", "edit", "reject", "ask"]
    return {
        "v": PAYLOAD_VERSION,
        "kind": kind,
        "approval_id": approval_id,
        "title": title,
        "summary": summary,
        "risk": risk,
        "reversible": reversible,
        "blast_radius": blast_radius or [],
        "policy": {
            "checks": [c.model_dump() for c in policy_checks or []],
            "approvers_required": approvers_required,
            "roles": roles or [],
        },
        "context": {"incident_id": incident_id, "evidence_refs": evidence_refs or []},
        "proposed": proposed or {},
        "options": options or default_options,
    }


def response_schema_for(kind: InterruptKind) -> type[BaseModel]:
    return RESPONSE_SCHEMAS[kind]
