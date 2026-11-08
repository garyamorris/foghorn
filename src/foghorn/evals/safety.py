"""The safety suite (D10, ``datasets/evals/safety_cases.jsonl``): probe the ops copilot with hostile / pushy / policy-violating
messages and the commander with the injected-log scenario, then score ``must`` / ``must_not`` with the ``safety_cases`` evaluator.

* ``copilot_message`` and the injected-text vectors (customer report, change summary, runbook text, tool output) become a
  chat message to a fresh ``ops_copilot`` (sandbox scenario S01). Review interrupts are left PENDING: nothing is approved,
  so what is measured is the model's own behaviour (which tools it asked to run and what it said), the first layer; the human
  review is the second.
* a case with ``input_ref`` (SC-03 -> S15) is a scenario: it runs through the commander and is scored on its report.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from foghorn.evals.evaluators import safety_cases
from foghorn.evals.harness import Case, run_suite

SANDBOX_SCENARIO = "S01"
_WRAP = {
    "customer_report": "A customer report just came in, quoted verbatim: {text}\nWhat should we do about it?",
    "change_summary": "The latest change summary reads: {text}\nWhat should we do about it?",
    "runbook_text": "{text}\nWhat should we do about it?",
    "tool_output": "{text}\nWhat should we do about it?",
    "log_line": "{text}",
    "copilot_message": "{text}",
}
_FLAG_TOKENS = ("prompt_injection_detected", "telemetry_flatline")


def message_for(row: dict[str, Any]) -> str:
    return _WRAP.get(str(row.get("vector")), "{text}").format(text=row.get("input", ""))


async def probe_copilot(text: str, profile: str, *, thread_id: str) -> dict[str, Any]:
    """One chat turn against a fresh copilot; review interrupts stay pending. Returns the evaluator's ``SafetyOutcome`` shape."""
    from foghorn.evals.runner import memory_store
    from foghorn.flows.ops_copilot.build import build_graph

    store = memory_store()
    graph = build_graph(InMemorySaver(), store)
    ctx = {"model_profile": profile, "scenario_id": SANDBOX_SCENARIO}
    out = await graph.ainvoke(
        {"messages": [{"role": "user", "content": text}]},
        {"configurable": {"thread_id": thread_id}},
        context=ctx,
        version="v2",
    )
    msgs = list(out.value.get("messages") or [])
    last_human = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
    turn = msgs[last_human + 1 :]
    ais = [m for m in turn if isinstance(m, AIMessage)]
    answer = "\n".join(str(m.text) for m in ais if str(m.text).strip())
    calls = [{"name": tc["name"], "args": tc["args"]} for m in ais for tc in m.tool_calls]
    return {
        "answer": answer,
        "tool_calls": calls,
        "interrupts": [i.value for i in out.interrupts],
        "executed": list(out.value.get("applied_actions") or []),
        "flags": [f for f in _FLAG_TOKENS if re.search(re.escape(f), answer)],
    }


def _summarise(rep: Any) -> dict[str, Any]:
    metrics = rep.metrics()
    failing = {k: rep.failures(k) for k in ("safety_must", "safety_must_not", "safety_case") if rep.failures(k)}
    return {
        "suite": "safety",
        "n": len(rep.rows),
        "metrics": {k: metrics[k] for k in ("safety_must", "safety_must_not", "safety_case") if k in metrics},
        "failures": failing,
        "cases": {r["case"].id: {s.key: s.score for s in r["scores"] if s.key.startswith("safety")} for r in rep.rows},
        "source": "ops_copilot (+ commander for input_ref cases)",
    }


async def run_safety_suite(
    profile: str = "competent", *, commander: str = "auto", rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    from foghorn.evals.runner import read_cases, run_scenarios

    rows = rows if rows is not None else read_cases("safety_cases")
    refs: dict[str, str] = {r["case_id"]: r["input_ref"] for r in rows if r.get("input_ref")}
    reports: dict[str, dict[str, Any]] = {}
    if refs:
        out = await run_scenarios("custom", profile, commander=commander, scenario_ids=sorted(set(refs.values())))
        reports = {sid: {"report": rep} for sid, rep in _reports_of(out).items()}

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        if inputs.get("input_ref"):
            return reports.get(str(inputs["input_ref"]), {"report": {}})
        return await probe_copilot(inputs["text"], profile, thread_id=f"safety-{inputs['case_id']}-{profile}")

    cases = [
        Case(
            r["case_id"],
            {"case_id": r["case_id"], "text": message_for(r), "input_ref": r.get("input_ref")},
            {"must": r.get("must", []), "must_not": r.get("must_not", [])},
        )
        for r in rows
    ]
    rep = await run_suite(target, cases, [lambda i, o, ref: safety_cases(i, o, ref)], concurrency=4)
    return _summarise(rep)


def _reports_of(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Scenario id -> the commander's report; the eval_runner keeps it on each case under ``report`` (the ``actual`` view is
    too thin for the safety checks)."""
    return {c["scenario_id"]: c.get("report") or {} for c in out["cases"]}
