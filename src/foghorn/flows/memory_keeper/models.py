"""Schemas of the memory keeper (pydantic at the edges only; state is plain JSON)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LessonCandidate(BaseModel):
    text: str
    category: str | None = None
    confidence: float = Field(0.6, ge=0.0, le=1.0)


class Distillation(BaseModel):
    """Structured output of the lesson-distiller model."""

    lessons: list[LessonCandidate] = Field(default_factory=list)
    incident_summary: str = ""
