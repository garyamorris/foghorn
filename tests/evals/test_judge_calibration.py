"""The comms judge (LLM-as-judge, 'judge' role): deterministic offline, calibrated good = 1.0, bad = 0.0, partial in between."""

from __future__ import annotations

import pytest

from foghorn.data import api as data
from foghorn.evals import evaluators as ev
from foghorn.evals.evaluators import judge_eval
from foghorn.models import offline as o
from tests.evals.fixtures import report

FACTS = ["checkout", "degraded", "customers"]


async def test_calibration_good_bad_partial():
    good = await judge_eval.judge_text("Checkout is degraded and customers may see errors.", FACTS)
    bad = await judge_eval.judge_text("Everything is fine, nothing to report.", FACTS)
    partial = await judge_eval.judge_text("Checkout is slow.", FACTS)
    assert good.score == 1.0
    assert bad.score == 0.0
    assert 0.0 < partial.score < 1.0 and partial.score == pytest.approx(1 / 3)
    assert "1/3" in partial.rationale


async def test_calibration_is_monotonic_in_facts_present():
    scores = [
        (await judge_eval.judge_text(" ".join(FACTS[:n]) or "nothing", FACTS)).score for n in range(len(FACTS) + 1)
    ]
    assert scores == sorted(scores) and scores[0] == 0.0 and scores[-1] == 1.0
    assert len(set(scores)) == len(scores)


async def test_the_scenario_evaluator_grades_the_draft_against_must_mention():
    truth = data.get_truth("S10")  # must_mention: checkout, degraded
    full = await ev.comms_judge({}, {"report": report(comms_draft="Checkout is degraded.")}, truth)
    none = await ev.comms_judge({}, {"report": report(comms_draft="All good.")}, truth)
    half = await ev.comms_judge({}, {"report": report(comms_draft="Checkout is slow.")}, truth)
    assert full[0].key == "comms_judge" and full[0].score == 1.0
    assert none[0].score == 0.0
    assert half[0].score == pytest.approx(0.5)
    assert await ev.comms_judge({}, {"report": report()}, data.get_truth("S03")) == []  # no status page for SEV3


async def test_judge_prompt_is_editable_but_the_parseable_tail_is_not():
    seen: list[str] = []

    def spy(c: o.Call):
        seen.append(c.human)
        return o.struct({"score": 0.25, "rationale": "spy"})

    o.SCRIPT_OVERRIDES["judge"] = o.Script().when("spy", o.schema("Grade"), spy)
    grade = await judge_eval.judge_text("a b", ["x", "y"], instructions="Be harsh. Custom instructions.")
    assert grade.score == 0.25
    assert seen[0].startswith("Be harsh. Custom instructions.")
    assert seen[0].endswith("FACTS: x; y\nANSWER: a b")
    prompt = judge_eval.build_judge_prompt("  hello  ", ["f"], "ans")
    assert prompt == "hello\nFACTS: f\nANSWER: ans"


async def test_judge_calls_are_tagged_nostream():
    """Internal judge JSON must never reach a user's token stream."""
    from langchain_core.callbacks import BaseCallbackHandler

    tags: list[list[str]] = []

    class Spy(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, *, tags=None, **kw):  # type: ignore[no-untyped-def]
            seen_tags.append(list(tags or []))

    seen_tags = tags
    await judge_eval.judge_text("checkout degraded customers", FACTS, config={"callbacks": [Spy()]})
    assert any("nostream" in t and "judge" in t for t in tags)
