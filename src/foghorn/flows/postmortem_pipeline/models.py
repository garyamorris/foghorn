"""Schemas of the postmortem pipeline.

* pydantic models are used at the EDGES only (structured model output, resume validation); everything that is
  checkpointed or returned from a ``@task`` is a plain JSON ``dict`` (``model_dump(mode="json")``).
* ``PostmortemRunInput`` extends the shared ``PostmortemInput`` with OPTIONAL fields only (contracts/inputs.py rule).
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from foghorn.contracts.inputs import CommanderReport, GraphResult, PostmortemInput

MAX_REVISIONS = 2  # hard bound of the draft -> review -> revise loop (a context value can only lower it)


# --------------------------------------------------------------------------------------------------- graph edges
class PostmortemRunInput(PostmortemInput):
    """What the dispatcher sends (``incident_id``, ``scenario_id``, ``source_thread_id``) plus one optional escape hatch:
    ``report`` lets a caller (a test, Studio, the eval runner) hand over the commander's report directly when the
    commander thread cannot be read."""

    report: NotRequired[CommanderReport | None]


class RevisionRecord(TypedDict):
    """One line of the revision history kept in ``previous`` (the functional API's per-thread memory)."""

    revision: int
    status: Literal["published", "rejected", "skipped"]
    decided_by: str
    note: str
    review_rounds: int
    open_issues: int
    title: str


class Memo(TypedDict):
    """The value ``entrypoint.final(save=...)`` stores; the next run on the same thread receives it as ``previous``."""

    incident_id: str
    revisions: list[RevisionRecord]
    last_published: dict[str, Any] | None
    feedback: list[str]  # reviewer notes from rejections, fed into the next draft as issues to address


class PostmortemOutput(TypedDict):
    result: GraphResult
    postmortem: dict[str, Any]
    markdown: str
    review: dict[str, Any]
    revision: int


# ------------------------------------------------------------------------------------------------ the document
class TimelineEntry(BaseModel):
    t: str
    kind: Literal["detection", "alert", "change", "action", "recovery", "note"] = "note"
    summary: str
    ref: str | None = None
    key: bool = False  # key entries are required in every draft (checked by ``checks.py``)


class ActionItem(BaseModel):
    title: str
    owner: str | None = None  # a person id from the on-call data (``j.okafor``), never a free-text name
    kind: Literal["prevent", "detect", "mitigate", "process"] = "prevent"
    priority: Literal["p0", "p1", "p2"] = "p1"


class PostmortemBody(BaseModel):
    """Structured output of the drafting model (the summary is written by a second, STREAMED call)."""

    title: str
    impact: str
    root_cause: str
    detection: str
    contributing_factors: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    what_went_well: list[str] = Field(default_factory=list)
    what_went_badly: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    lessons_learned: list[str] = Field(default_factory=list)


class Postmortem(PostmortemBody):
    incident_id: str
    severity: str = "SEV3"
    scenario_id: str | None = None
    component: str | None = None
    category: str | None = None
    summary: str = ""
    revision: int = 1
    status: Literal["draft", "approved", "edited"] = "draft"
    window: dict[str, str] = Field(default_factory=dict)  # {"start": iso, "end": iso} in VIRTUAL time


EDITABLE_FIELDS = (
    "title",
    "summary",
    "impact",
    "root_cause",
    "detection",
    "contributing_factors",
    "what_went_well",
    "what_went_badly",
    "action_items",
    "lessons_learned",
    "timeline",
)


# ------------------------------------------------------------------------------------------------- the critic
class Issue(BaseModel):
    id: str
    field: str
    message: str
    severity: Literal["blocker", "warning"] = "blocker"


class Review(BaseModel):
    ok: bool
    issues: list[Issue] = Field(default_factory=list)
    summary: str = ""
