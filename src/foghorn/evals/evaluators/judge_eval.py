"""LLM-as-judge evaluator for communications quality (``comms_judge``).

The judge is the ``judge`` role behind ``get_chat_model`` with structured output (``foghorn.evals.judge.Grade``). Offline the
judge Script scores by required-fact presence, so prompt construction -> structured parse -> score is exercised without a model.
The instruction preamble is editable (Studio: ``eval_runner`` context ``judge_prompt``); the ``FACTS:`` / ``ANSWER:`` tail is
appended by code so no prompt edit can break the offline (or any) parser. Not part of the scenario pass criteria
(docs/design/03 section 2.5: 'no LLM-as-judge in pass criteria').
"""

from __future__ import annotations

from typing import Any

from foghorn.evals.evaluators import _common as c
from foghorn.evals.harness import Score
from foghorn.evals.judge import JUDGE_SCRIPT, Grade
from foghorn.models.factory import get_chat_model, structured

DEFAULT_JUDGE_INSTRUCTIONS = (
    "You are a strict grader of incident communications. Score the ANSWER from 0 to 1 for how completely it states the FACTS. "
    "Do not reward style or length. Give a one-sentence rationale."
)


def build_judge_prompt(instructions: str, facts: list[str], answer: str) -> str:
    """Editable preamble + the canonical tail the judge parses."""
    return f"{instructions.strip()}\nFACTS: {'; '.join(facts)}\nANSWER: {answer}"


async def judge_text(
    answer: str,
    facts: list[str],
    *,
    instructions: str = DEFAULT_JUDGE_INSTRUCTIONS,
    config: dict[str, Any] | None = None,
) -> Grade:
    """Grade ``answer`` against required ``facts`` with the judge role (tagged ``nostream`` + ``judge``)."""
    llm = structured(get_chat_model("judge", offline_script=JUDGE_SCRIPT), Grade)
    grade: Grade = await llm.with_config(tags=["nostream", "judge"], **(config or {})).ainvoke(
        build_judge_prompt(instructions, facts, answer)
    )
    return grade


async def comms_judge(
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    ref: dict[str, Any],
    *,
    instructions: str = DEFAULT_JUDGE_INSTRUCTIONS,
    config: dict[str, Any] | None = None,
) -> list[Score]:
    """``comms_judge``: the judge's score for the comms draft against the scenario's ``must_mention`` facts."""
    spec = ref.get("comms") or {}
    facts = [str(f) for f in spec.get("must_mention") or []]
    if not spec.get("status_page") or not facts:
        return []
    draft = str(c.report_of(outputs).get("comms_draft") or "")
    grade = await judge_text(draft, facts, instructions=instructions, config=config)
    return [Score("comms_judge", round(float(grade.score), 4), grade.rationale)]
