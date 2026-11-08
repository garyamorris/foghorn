"""Run the REAL ``incident_commander`` for one scenario through the (in-process) SDK client, answering its interrupts with the
approver oracle. The client sits behind an injectable factory so tests (and the offline CLI) drive a local stub graph with
the exact same code path as the server.

Deadlock rules (docs/design/02 gotchas 2-3): each scenario runs on a DERIVED CHILD THREAD (never the parent's thread) and every
scenario holds one slot of a bounded semaphore; ``langgraph dev`` must run with ``--n-jobs-per-worker N`` where
``N >= concurrency + 2`` (this run itself holds one job).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph_sdk import get_client

from foghorn.contracts.inputs import CommanderInput
from foghorn.contracts.meta import (
    CTX_APPROVAL_POLICY,
    CTX_MODEL_PROFILE,
    CTX_SCENARIO_ID,
    M_EVAL_RUN_ID,
    M_PARENT_THREAD,
    M_SCENARIO_ID,
    det_thread_id,
    incident_id_for,
    thread_metadata,
)
from foghorn.flows.eval_runner.oracle import ApproverOracle

COMMANDER = "incident_commander"
MAX_CONCURRENCY = 6  # 8 jobs per worker - this run - 1 spare
DEFAULT_CONCURRENCY = 2

ClientFactory = Callable[[], Any]
_factory: ClientFactory | None = None


def set_client_factory(factory: ClientFactory | None) -> None:
    """Tests / the offline CLI inject a client (e.g. ``LocalSdkClient``); ``None`` restores the in-process SDK client."""
    global _factory
    _factory = factory


def make_client() -> Any:
    return _factory() if _factory is not None else get_client(url=None)


def client_kind(client: Any) -> str:
    """'real' for the SDK, 'stub' for a local stand-in (baselines only gate runs of the same kind)."""
    return str(getattr(client, "kind", "real"))


def jobs_per_worker() -> int | None:
    """``N_JOBS_PER_WORKER`` of the hosting ``langgraph dev`` / server process, if it is one."""
    raw = os.environ.get("N_JOBS_PER_WORKER")
    return int(raw) if raw and raw.isdigit() else None


def effective_concurrency(requested: int | None, client: Any | None = None) -> int:
    """Clamp to what the server can run without deadlocking: ``<= n_jobs - 2`` (never above ``MAX_CONCURRENCY``)."""
    want = max(1, int(requested or DEFAULT_CONCURRENCY))
    n = jobs_per_worker()
    cap = MAX_CONCURRENCY if n is None else max(1, min(MAX_CONCURRENCY, n - 2))
    if client is not None and client_kind(client) != "real":
        cap = MAX_CONCURRENCY
    return min(want, cap)


def assert_can_delegate(client: Any) -> None:
    """Fail fast and clearly instead of hanging: a server with a single job slot cannot run a child run at all."""
    n = jobs_per_worker()
    if client_kind(client) == "real" and n is not None and n < 2:
        raise RuntimeError(
            f"eval_runner needs at least 2 job slots (found N_JOBS_PER_WORKER={n}); start the server with --n-jobs-per-worker 8"
        )


@dataclass
class ScenarioRun:
    """What driving one scenario produced (the report is scored later)."""

    scenario_id: str
    thread_id: str
    run_id: str | None = None
    report: dict[str, Any] | None = None
    status: str = "ok"  # ok | error | step_limit | timeout
    error: str | None = None
    steps: int = 0  # interrupt rounds answered
    incident_id: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)


def commander_input(
    scenario: dict[str, Any], eval_run_id: str, events: list[dict[str, Any]] | None = None
) -> CommanderInput:
    """The commander's input exactly as the router would build it: a neutral SEV3 placeholder (the commander decides the real
    severity - the truth must not leak), the public title and the triggering alert."""
    sid = scenario["scenario_id"]
    incident_id = incident_id_for(f"{eval_run_id}/{sid}")
    inp: CommanderInput = {
        "scenario_id": sid,
        "incident": {
            "id": incident_id,
            "severity": "SEV3",
            "title": str(scenario.get("title") or sid),
            "status": "open",
        },
        "correlation_id": f"{eval_run_id}/{sid}",
    }
    trigger = (scenario.get("trigger") or {}).get("alert_event")
    alert = next((e for e in events or [] if e.get("id") == trigger), None)
    if alert:
        inp["alerts"] = [alert]
        inp["trigger_event_ids"] = [str(alert["id"])]
    return inp


def _pending(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for t in state.get("tasks") or [] for i in (t.get("interrupts") or [])]


async def drive_scenario(
    client: Any,
    scenario: dict[str, Any],
    *,
    eval_run_id: str,
    profile: str,
    oracle: ApproverOracle,
    max_steps: int = 8,
    timeout_s: float = 240.0,
    parent_thread: str | None = None,
    events: list[dict[str, Any]] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> ScenarioRun:
    """Start (or continue) the commander on its derived child thread and resolve every interrupt through ``oracle``.

    Idempotent on restart (a retried / resumed ``eval_runner`` node re-enters here): a finished child is reused, a child that
    is waiting on interrupts is continued, nothing is started twice.
    """
    sid = scenario["scenario_id"]
    tid = det_thread_id(COMMANDER, eval_run_id, sid)
    run = ScenarioRun(sid, tid, incident_id=incident_id_for(f"{eval_run_id}/{sid}"))
    md = thread_metadata(
        "eval", COMMANDER, **{M_EVAL_RUN_ID: eval_run_id, M_SCENARIO_ID: sid, M_PARENT_THREAD: parent_thread}
    )
    ctx = {CTX_MODEL_PROFILE: profile, CTX_APPROVAL_POLICY: "standard", CTX_SCENARIO_ID: sid, **(extra_context or {})}

    async def _go() -> None:
        await client.threads.create(thread_id=tid, if_exists="do_nothing", graph_id=COMMANDER, metadata=md)
        state = await client.threads.get_state(tid)
        values = dict(state.get("values") or {})
        pending = _pending(state)
        if values.get("report") and not state.get("next") and not pending:
            out: dict[str, Any] = values  # finished earlier: reuse
        elif pending:
            out = {**values, "__interrupt__": pending}
        else:
            out = await client.runs.wait(
                tid, COMMANDER, input=commander_input(scenario, eval_run_id, events), context=ctx, metadata=md
            )
        while out.get("__interrupt__"):
            if run.steps >= max_steps:
                run.status, run.error = "step_limit", f"still interrupted after {max_steps} answers"
                return
            answers: dict[str, Any] = {}
            for itr in out["__interrupt__"]:
                payload = itr.get("value") or {}
                answer = oracle.decide(payload if isinstance(payload, dict) else {}).model_dump(mode="json")
                answers[str(itr.get("id") or itr.get("interrupt_id") or "")] = answer
            run.steps += 1
            out = await client.runs.wait(tid, COMMANDER, command={"resume": answers}, context=ctx)
        report = out.get("report")
        if not report:
            report = ((await client.threads.get_state(tid)).get("values") or {}).get("report")
        if not isinstance(report, dict):
            run.status, run.error = "error", "the commander finished without a report"
            return
        run.report = report

    try:
        await asyncio.wait_for(_go(), timeout=timeout_s)
    except TimeoutError:
        run.status, run.error = "timeout", f"scenario exceeded {timeout_s:.0f}s"
    except Exception as exc:  # a crashed scenario is a failed CASE, never a failed eval run
        run.status, run.error = "error", f"{type(exc).__name__}: {exc}"
    run.decisions = list(oracle.log)
    run.run_id = await _first_run_id(client, tid)
    return run


async def _first_run_id(client: Any, tid: str) -> str | None:
    """The run that started the scenario (oldest on the thread); best effort, only used for the dashboard link."""
    lister = getattr(client.runs, "list", None)
    if lister is None:
        return None
    try:
        runs = await lister(tid, limit=50)
    except Exception:
        return None
    return str(runs[-1]["run_id"]) if runs else None
