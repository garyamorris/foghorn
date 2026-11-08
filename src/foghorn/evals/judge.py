"""LLM-as-judge with an offline deterministic judge for CI.

The judge is just another role ("judge") behind `get_chat_model`, using structured output. Offline, its
Script scores by overlap between the ANSWER and the required FACTS embedded in the judge prompt, so the
prompt construction -> structured parse -> aggregation -> threshold path is fully exercised without a model.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from foghorn.models import offline as o
from foghorn.models.factory import get_chat_model, structured

JUDGE_PROMPT = """You are a strict grader. Score the ANSWER from 0 to 1 for how completely it states the FACTS.
Do not reward style. Quote nothing; give a one-sentence rationale.
FACTS: {facts}
ANSWER: {answer}"""


class Grade(BaseModel):
    score: float = Field(ge=0, le=1)
    rationale: str


def _offline_grade(call: o.Call) -> Grade:
    m = re.search(r"FACTS: (.*)\nANSWER: (.*)", call.human, re.S)
    facts = [f.strip().lower() for f in (m.group(1) if m else "").split(";") if f.strip()]
    answer = (m.group(2) if m else "").lower()
    hit = [f for f in facts if f in answer]
    return Grade(score=len(hit) / len(facts) if facts else 1.0, rationale=f"{len(hit)}/{len(facts)} facts present")


JUDGE_SCRIPT = o.Script().when("judge:facts", o.schema("Grade"), lambda c: o.struct(_offline_grade(c)))


async def judge_facts(answer: str, facts: list[str], *, config: dict[str, Any] | None = None) -> Grade:
    llm = structured(get_chat_model("judge", offline_script=JUDGE_SCRIPT), Grade)
    # tag so judge calls are excluded from user-facing streams and filterable in LangSmith
    grade: Grade = await llm.with_config(tags=["nostream", "judge"], **(config or {})).ainvoke(
        JUDGE_PROMPT.format(facts="; ".join(facts), answer=answer)
    )
    return grade
