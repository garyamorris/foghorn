"""Nodes of ``eval_runner``: ``plan`` -> ``Send`` x N -> ``run_scenario`` -> ``aggregate`` (defer).

* ``plan``          sync (threadpool): expand the suite, derive the eval run id, clamp the concurrency
* ``run_scenario``  async map branch: run the REAL commander on a derived child thread through the SDK, answer its interrupts
                    with the approver oracle, score the report against the truth (the ONE place that reads truth), emit
                    ``eval.progress`` with the finished case
* ``aggregate``     async, ``defer=True``: scorecard, baseline comparison, Store writes, ``eval.finished``, ``GraphResult``
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.types import Overwrite, Send

from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit
from foghorn.contracts.inputs import GraphResult
from foghorn.contracts.meta import M_EVAL_RUN_ID
from foghorn.data import api as data
from foghorn.evals import scoring
from foghorn.evals.evaluators import comms_judge, evaluate_scenario
from foghorn.flows.eval_runner import client as sdk
from foghorn.flows.eval_runner.context import EvalContext
from foghorn.flows.eval_runner.oracle import ApproverOracle
from foghorn.flows.eval_runner.plan import plan_suite
from foghorn.flows.eval_runner.state import BranchInput, EvalState
from foghorn.logging import get_logger

log = get_logger("eval_runner")

# per-process bookkeeping, keyed by (event loop, eval run): the semaphore that bounds in-flight commander runs and the set of
# finished scenarios behind `done`. Sets are idempotent, so a retried branch cannot inflate the progress counter.
_SEMS: dict[tuple[int, str], asyncio.Semaphore] = {}
_DONE: dict[tuple[int, str], set[str]] = {}


def _key(eval_run_id: str) -> tuple[int, str]:
    return id(asyncio.get_running_loop()), eval_run_id


def _semaphore(eval_run_id: str, limit: int) -> asyncio.Semaphore:
    return _SEMS.setdefault(_key(eval_run_id), asyncio.Semaphore(max(1, limit)))


def _forget(eval_run_id: str) -> None:
    _SEMS.pop(_key(eval_run_id), None)
    _DONE.pop(_key(eval_run_id), None)


def _lean(case: dict[str, Any]) -> dict[str, Any]:
    """The case as the dashboard persists it: without the (large) raw commander report."""
    return {k: v for k, v in case.items() if k != "report"}


def _forwarded(ctx: EvalContext) -> dict[str, Any]:
    """Run-context keys that change the commander's behaviour and are therefore passed on to every child run."""
    extra: dict[str, Any] = {}
    if not ctx.memory_enabled:
        extra["memory_enabled"] = False
    if ctx.chaos:
        extra["chaos"] = ctx.chaos
    return extra


def derive_eval_run_id(state: EvalState, config: RunnableConfig) -> str:
    """The dashboard's id when it started us; else the thread metadata the dispatcher sets; else derived from the thread."""
    if state.get("eval_run_id"):
        return str(state["eval_run_id"])
    meta = config.get("metadata") or {}
    if meta.get(M_EVAL_RUN_ID):
        return str(meta[M_EVAL_RUN_ID])
    tid = str((config.get("configurable") or {}).get("thread_id") or "local")
    return f"evr_{tid.replace('-', '')[:8]}"


# ------------------------------------------------------------------------------------------------------------- plan
def plan(state: EvalState, runtime: Runtime[EvalContext]) -> dict[str, Any]:
    config = get_config()
    ctx = runtime.context or EvalContext()
    client = sdk.make_client()
    sdk.assert_can_delegate(client)
    suite = state.get("suite", "core16")
    profile = state.get("profile") or ctx.model_profile
    eval_run_id = derive_eval_run_id(state, config)
    tasks = plan_suite(suite, state.get("scenario_ids"), state.get("max_scenarios"))
    concurrency = sdk.effective_concurrency(state.get("concurrency"), client)
    emit(
        "progress",
        f"eval:{eval_run_id}:plan",
        label=f"{suite}: {len(tasks)} scenarios, concurrency {concurrency}",
        pct=0.0,
    )
    emit_event(
        "eval.progress",
        idempotency_key=f"eval.progress:{eval_run_id}:plan",
        payload={"eval_run_id": eval_run_id, "done": 0, "total": len(tasks), "suite": suite, "profile": profile},
        correlation_id=eval_run_id,
    )
    return {
        "eval_run_id": eval_run_id,
        "profile": profile,
        "concurrency": concurrency,
        "plan": tasks,
        "parent_thread": str((config.get("configurable") or {}).get("thread_id") or ""),
        "commander": sdk.client_kind(client),
    }


def fan_out(state: EvalState) -> list[Send] | str:
    """One ``Send`` per scenario (a ``Send`` branch per row in Studio's trace); nothing to do -> straight to the reduce."""
    tasks = state.get("plan") or []
    if not tasks:
        return "aggregate"
    return [
        Send(
            "run_scenario",
            BranchInput(
                task=t,
                eval_run_id=state["eval_run_id"],
                profile=state["profile"],
                concurrency=state["concurrency"],
                total=len(tasks),
                parent_thread=state.get("parent_thread", ""),
            ),
        )
        for t in tasks
    ]


# ------------------------------------------------------------------------------------------------------ run_scenario
def _bundle(sid: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Public scenario record + its events (blocking file / sqlite reads: call from a thread)."""
    return data.get_scenario(sid), data.scenario_events(sid)


def _score(task: dict[str, Any], run: sdk.ScenarioRun) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Score one run against the ground truth. THE place (with the evaluators) allowed to call ``get_truth``."""
    if run.report is None:
        return scoring.failed_case(
            task,
            run.error or run.status,
            thread_id=run.thread_id,
            run_id=run.run_id,
            incident_id=run.incident_id,
            steps=run.steps,
        ), None
    truth = data.get_truth(task["scenario_id"])
    scores = evaluate_scenario({"scenario_id": task["scenario_id"]}, {"report": run.report}, truth)
    case = scoring.build_case(
        task,
        truth,
        run.report,
        scores,
        incident_id=run.incident_id,
        thread_id=run.thread_id,
        run_id=run.run_id,
        steps=run.steps,
    )
    case["report"] = run.report  # kept in the graph output (the safety suite needs it); stripped from lifecycle events
    return case, truth


async def run_scenario(state: BranchInput, runtime: Runtime[EvalContext]) -> dict[str, Any]:
    ctx = runtime.context or EvalContext()
    t = state["task"]
    sid, eid = t["scenario_id"], state["eval_run_id"]
    client = sdk.make_client()
    emit("progress", f"eval:{eid}:{sid}:start", agent="run_scenario", label=f"{sid}: queued", pct=0.0)
    async with _semaphore(eid, state["concurrency"]):
        emit("progress", f"eval:{eid}:{sid}:run", agent="run_scenario", label=f"{sid}: running the commander", pct=0.1)
        try:
            public, events = await asyncio.to_thread(_bundle, sid)
            oracle = ApproverOracle(sid, await asyncio.to_thread(data.get_truth, sid), public)
            run = await sdk.drive_scenario(
                client,
                {**public, **t},
                eval_run_id=eid,
                profile=state["profile"],
                oracle=oracle,
                max_steps=ctx.max_steps,
                timeout_s=ctx.scenario_timeout_s,
                parent_thread=state.get("parent_thread") or None,
                events=events,
                extra_context=_forwarded(ctx),
            )
        except Exception as exc:  # a broken scenario is a failed case, not a failed evaluation
            log.warning("eval_scenario_failed", scenario_id=sid, error=repr(exc))
            run = sdk.ScenarioRun(sid, "", status="error", error=f"{type(exc).__name__}: {exc}")
    case, truth = await asyncio.to_thread(_score, t, run)
    if truth is not None and run.report is not None:
        judged = await comms_judge({"scenario_id": sid}, {"report": run.report}, truth, instructions=ctx.judge_prompt)
        for s in judged:  # informational: an LLM judge is never part of the pass criteria
            case["scores"][s.key] = s.score
            case["notes"][s.key] = s.comment
    done_set = _DONE.setdefault(_key(eid), set())
    done_set.add(sid)
    done, total = len(done_set), state["total"]
    emit(
        "progress",
        f"eval:{eid}:{sid}:done",
        agent="run_scenario",
        label=f"{sid}: score {case['score']:.2f}",
        pct=done / max(total, 1),
    )
    emit("metric", f"eval:{eid}:{sid}:score", agent="run_scenario", name=f"{sid}.score", value=case["score"])
    emit_event(
        "eval.progress",
        idempotency_key=f"eval.progress:{eid}:{sid}",
        severity="high" if case["gate_trips"] else "info",
        payload={"eval_run_id": eid, "done": done, "total": total, "case": _lean(case)},
        correlation_id=eid,
        scenario_id=sid,
    )
    return {"cases": [case]}


# -------------------------------------------------------------------------------------------------------- aggregate
async def _load_baseline(
    store: Any, suite: str, profile: str, baseline_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Explicit id: Store baselines, then Store reports (a previous eval run). Default: the Store's (suite, profile) baseline,
    then the committed ``datasets/evals/baseline_<profile>.json`` (what CI uses)."""
    if store is not None:
        keys = [baseline_id] if baseline_id else [f"{suite}:{profile}"]
        for ns in (scoring.BASELINE_NS, scoring.REPORT_NS):
            for key in keys:
                item = await store.aget(ns, str(key))
                if item is not None:
                    return dict(item.value), str(key)
    if not baseline_id or baseline_id in (profile, f"baseline_{profile}"):
        file = await asyncio.to_thread(scoring.load_baseline_file, profile, suite)
        if file:
            return file, f"baseline_{profile}"
    return None, baseline_id


async def aggregate(state: EvalState, runtime: Runtime[EvalContext]) -> dict[str, Any]:
    eid, suite, profile = state["eval_run_id"], state.get("suite", "core16"), state["profile"]
    order = {t["scenario_id"]: i for i, t in enumerate(state.get("plan") or [])}
    cases = sorted(state.get("cases") or [], key=lambda c: order.get(c["scenario_id"], 10**6))
    card = scoring.scorecard(cases, suite=suite, profile=profile)
    card["eval_run_id"], card["label"], card["commander"] = eid, state.get("label"), state.get("commander", "real")
    baseline, bid = await _load_baseline(runtime.store, suite, profile, state.get("baseline_id"))
    if baseline and baseline.get("commander") not in (None, card["commander"]):
        card["baseline_skipped"] = (
            f"baseline {bid} was generated by a {baseline.get('commander')} commander, this run used {card['commander']}"
        )
        baseline = None
    scoring.apply_baseline(card, cases, baseline, bid)
    if state.get("baseline_id") and baseline is None and "baseline_skipped" not in card:
        card["baseline_missing"] = state["baseline_id"]
    record = scoring.baseline_from(cases, card, commander=card["commander"], run_id=eid)
    if runtime.store is not None:
        await runtime.store.aput(scoring.REPORT_NS, eid, record, index=False)
        if state.get("save_baseline"):
            await runtime.store.aput(scoring.BASELINE_NS, f"{suite}:{profile}", record, index=False)
            await runtime.store.aput(scoring.BASELINE_NS, eid, record, index=False)
    failed = scoring.run_failed(card) or bool(card["regressions"])
    verdict = (
        f"HARD GATE TRIPPED {sorted(card['gate_trips'])}"
        if card["gate_trips"]
        else f"REGRESSED {card['regressions']}"
        if card["regressions"]
        else "ok"
    )
    summary = (
        f"{profile} on {suite}: {card['passed']}/{card['n']} passed, mean score {card['mean_score']}, "
        f"safety {card['gate']['safety']}, no_crash {card['gate']['no_crash']} - {verdict}"
    )
    emit("progress", f"eval:{eid}:finished", agent="aggregate", label=summary, pct=1.0)
    emit_event(
        "eval.finished",
        idempotency_key=f"eval.finished:{eid}",
        severity="high" if failed else "info",
        payload={
            "eval_run_id": eid,
            "summary": card,
            "cases": [_lean(c) for c in cases],
            "status": "failed" if failed else "ok",
        },
        correlation_id=eid,
    )
    _forget(eid)
    result: GraphResult = {
        "graph_id": "eval_runner",
        "status": "failed" if failed else "ok",
        "event_id": None,
        "correlation_id": eid,
        "summary": summary,
        "artifacts": [
            {"kind": "report", "name": "scorecard", "ref": eid, "data": card},
            {
                "kind": "table",
                "name": "cases",
                "ref": eid,
                "data": [
                    {
                        "scenario_id": c["scenario_id"],
                        "score": c["score"],
                        "passed": c["passed"],
                        "gate_trips": c["gate_trips"],
                    }
                    for c in cases
                ],
            },
        ],
        "metrics": {
            "mean_score": float(card["mean_score"] or 0.0),
            "pass_rate": float(card["pass_rate"]),
            "safety": float(card["gate"]["safety"]),
            "no_crash": float(card["gate"]["no_crash"]),
            "regressions": float(len(card["regressions"])),
        },
        "child_threads": [str(c["thread_id"]) for c in cases if c.get("thread_id")],
    }
    return {"summary": card, "result": result, "cases": Overwrite(cases)}
