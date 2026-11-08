"""Shared helpers for the ingress-side graph tests (event_router, storm_correlator, change_gate).

* ``FakeClient``: a fake LangGraph SDK client with the server's behaviour that matters here: ``runs.create`` with
  ``multitask_strategy="reject"`` on a thread that already has a run answers a 409 conflict.
* ``scenario_event`` / ``alert_events``: real envelopes from the data layer (the TEST may read them; graph code only
  ever sees the public view).
"""

from __future__ import annotations

from typing import Any

from foghorn.common import delegate as delegate_mod
from foghorn.data import api as data_api


class FakeThreads:
    def __init__(self) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}

    async def create(self, **kw: Any) -> dict[str, Any]:
        self.created.setdefault(kw["thread_id"], kw)
        return {"thread_id": kw["thread_id"]}

    async def get_state(self, tid: str) -> dict[str, Any]:
        return self.states.get(tid, {"values": {}, "next": [], "tasks": []})


class FakeRuns:
    def __init__(self, threads: FakeThreads) -> None:
        self.threads = threads
        self.created: list[dict[str, Any]] = []  # accepted runs
        self.rejected: list[str] = []  # thread ids that answered 409
        self.fail_with: BaseException | None = None

    async def create(self, tid: str, gid: str, **kw: Any) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        if any(c["thread_id"] == tid for c in self.created) and kw.get("multitask_strategy") == "reject":
            self.rejected.append(tid)
            raise RuntimeError("HTTP 409 Conflict: thread already has an active run")
        self.created.append({"thread_id": tid, "graph_id": gid, **kw})
        return {"run_id": f"run-{len(self.created)}"}

    async def wait(self, tid: str, gid: str, **kw: Any) -> dict[str, Any]:  # pragma: no cover - await mode unused here
        return {}


class FakeClient:
    def __init__(self) -> None:
        self.threads = FakeThreads()
        self.runs = FakeRuns(self.threads)

    def runs_for(self, graph_id: str) -> list[dict[str, Any]]:
        return [r for r in self.runs.created if r["graph_id"] == graph_id]


def install_fake_client() -> FakeClient:
    client = FakeClient()
    delegate_mod.set_client_factory(lambda: client)
    return client


def uninstall_fake_client() -> None:
    delegate_mod.set_client_factory(None)


def scenario_event(scenario_id: str, event_id: str) -> dict[str, Any]:
    return next(e for e in data_api.scenario_events(scenario_id) if e["id"] == event_id)


TRIAGED = ("alert.fired", "slo.burn_rate", "healthcheck.failed", "customer.report")


def signal_events(scenario_id: str) -> list[dict[str, Any]]:
    """The events routing.yaml sends to the router (r-signal), in virtual-time order."""
    return [e for e in data_api.scenario_events(scenario_id) if e["type"] in (*TRIAGED, "alert.resolved")]


def make_event(
    eid: str,
    *,
    type: str = "alert.fired",
    component: str | None = "checkout-service",
    severity: str = "high",
    rule: str | None = "error_rate_high",
    occurred_at: str = "2026-11-05T14:07:00Z",
    dedupe_key: str | None = None,
    scenario_id: str | None = None,
    payload: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """A valid synthetic ``foghorn.event/1`` envelope (plain-JSON form)."""
    from foghorn.contracts.events import EventEnvelope, Subject, Trace

    body = dict(payload or {})
    if rule is not None:
        body.setdefault("rule", rule)
    return EventEnvelope(
        id=eid,
        type=type,
        source="alertmanager",
        severity=severity,  # type: ignore[arg-type]
        occurred_at=occurred_at,
        subject=Subject(component=component),
        payload=body,
        dedupe_key=dedupe_key if dedupe_key is not None else f"alert:{component}:{rule}",
        correlation_id=correlation_id,
        trace=Trace(scenario_id=scenario_id),
        attributes=attributes or {},
    ).to_state()


async def drive(
    graph: Any, input_: Any, resumes: list[Any], *, thread_id: str = "gate-1", context: Any = None
) -> tuple[Any, list[Any]]:
    """Run to completion, answering each interrupt batch from ``resumes``.  An item may be a plain value, an
    ``{interrupt_id: value}`` map, or a callable receiving the pending interrupts (to build the map from their ids)."""
    from langgraph.types import Command

    cfg = {"configurable": {"thread_id": thread_id}}
    queue = list(resumes)
    seen: list[Any] = []
    out = await graph.ainvoke(input_, cfg, version="v2", context=context)
    while out.interrupts:
        seen.append(list(out.interrupts))
        assert queue, f"no resume left for: {[i.value['approval_id'] for i in out.interrupts]}"
        item = queue.pop(0)
        value = item(out.interrupts) if callable(item) else item
        out = await graph.ainvoke(Command(resume=value), cfg, version="v2", context=context)
    assert not queue, f"unused resumes: {queue}"
    return out.value, seen


def by_role(decisions: dict[str, dict[str, Any]]) -> Any:
    """Four-eyes resume: ``{role: decision}`` -> a builder of ``{interrupt_id: decision}`` for the pending reviewer pair."""

    def build(interrupts: list[Any]) -> dict[str, Any]:
        return {i.id: decisions[i.value["policy"]["roles"][0]] for i in interrupts}

    return build
