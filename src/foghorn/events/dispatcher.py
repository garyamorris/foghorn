"""The dispatcher: turns queued dispatch rows into LangGraph runs and watches them to completion.

* Weighted-fair draining of the priority queues with a global run cap, per-graph caps and one active run per thread.
* A dispatch is event -> (thread, run); the correlation is stored in SQLite AND written to the thread's flat metadata
  (``foghorn_event_id`` ...) so Studio threads map back to events.
* Failure handling (docs/design/03 section 3.7): create failures retry with backoff; a run that ends in ``error`` is
  retried as a RESUME from the last good checkpoint (new run on the same thread with ``input=None``) - durable
  execution made visible; after ``max_attempts`` the event lands in the dead-letter queue (poison events flagged).
* Runs that pause at an ``interrupt()`` end as dispatch status ``interrupted`` and free their worker slot; the
  approvals service owns what happens next.
* Never blocks the loop: SQLite goes through ``asyncio.to_thread``; the SDK client is async; sleeping is injectable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from foghorn import clock
from foghorn.events.config import EventsSettings
from foghorn.events.db import StateDB
from foghorn.events.pipeline import EventBus, d_public, envelope_state
from foghorn.events.queues import PriorityQueues, QItem
from foghorn.events.sdk import ClientFactory, flatten_interrupts
from foghorn.events.sse import SSEHub
from foghorn.events.vclock import to_iso
from foghorn.logging import get_logger

log = get_logger("events.dispatcher")

Sleep = Callable[[float], Awaitable[None]]
TERMINAL_RUN = {"success", "error", "timeout", "interrupted"}


class ChaosError(RuntimeError):
    """Deterministic injected failure (``attributes.chaos``)."""


class DispatchError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


class Dispatcher:
    def __init__(
        self,
        *,
        db: StateDB,
        es: EventsSettings,
        bus: EventBus,
        queues: PriorityQueues,
        hub: SSEHub,
        client_factory: ClientFactory,
        sleep: Sleep = asyncio.sleep,
        wall: Callable[[], datetime] | None = None,
    ) -> None:
        self.db, self.es, self.bus, self.queues, self.hub = db, es, bus, queues, hub
        self._client_factory = client_factory
        self._client: Any = None
        self._sleep = sleep
        self._wall = wall or clock.now
        self.running_total = 0
        self.running_by_graph: dict[str, int] = {}
        self.active_threads: set[str] = set()
        self.dispatched_total = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.on_interrupted: Callable[[str], Awaitable[None]] | None = None  # approvals reconcile hook
        self.resume_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        bus.on_enqueue = self.wake

    # --- plumbing ------------------------------------------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def wake(self) -> None:
        self._wake.set()

    def _now_iso(self) -> str:
        return to_iso(self._wall(), ms=True)

    async def start(self) -> None:
        self._stopping = False
        await self.recover()
        self._loop_task = asyncio.create_task(self._loop(), name="foghorn-dispatcher")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _loop(self) -> None:
        while not self._stopping:
            self.pump()
            self._wake.clear()
            await self._wake.wait()

    def pump(self) -> None:
        """Start as many queued dispatches as the caps allow (called by the loop, and directly by tests)."""
        while self.running_total < self.es.max_runs:
            item = self.queues.pop(self._eligible)
            if item is None:
                break
            self._spawn(item)

    def _eligible(self, item: QItem) -> bool:
        cap = self.es.per_graph_max_runs.get(item.graph_id)
        if cap is not None and self.running_by_graph.get(item.graph_id, 0) >= cap:
            return False
        return not (item.thread_id and item.thread_id in self.active_threads)

    def _spawn(self, item: QItem) -> None:
        self.running_total += 1
        self.running_by_graph[item.graph_id] = self.running_by_graph.get(item.graph_id, 0) + 1
        if item.thread_id:
            self.active_threads.add(item.thread_id)
        task = asyncio.create_task(self._execute(item), name=f"dispatch-{item.dispatch_id}")
        self._tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._tasks.discard(t)
            self.running_total -= 1
            self.running_by_graph[item.graph_id] = max(0, self.running_by_graph.get(item.graph_id, 1) - 1)
            if item.thread_id:
                self.active_threads.discard(item.thread_id)
            if not t.cancelled() and t.exception() is not None:
                log.error("dispatch_task_crashed", dispatch_id=item.dispatch_id, error=repr(t.exception()))
            self.wake()

        task.add_done_callback(_done)

    async def run_until_idle(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109 - a polling deadline, not a cancel scope
        """Drive the dispatcher until the queues are empty and nothing is in flight (tests; also a graceful drain)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self.pump()
            pending = [t for t in self._tasks if not t.done()]
            if not pending and self.queues.total_depth == 0 and not self._retry_tasks():
                return
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("dispatcher did not go idle")
            if pending:
                await asyncio.wait(pending, timeout=0.05)
            else:
                await asyncio.sleep(0)

    def _retry_tasks(self) -> list[asyncio.Task[None]]:
        return [t for t in self._tasks if t.get_name().startswith("retry-") and not t.done()]

    # --- recovery ------------------------------------------------------------------------------------------------
    async def recover(self) -> None:
        """After a restart: re-queue queued/retrying work; adopt runs that were in flight."""
        rows, _ = await self.db.run(self.db.list_dispatches, statuses=("queued", "retrying", "running"), limit=500)
        already = {i.dispatch_id for i in self.queues.items()}
        for d in reversed(rows):
            if d["id"] in already:
                continue
            if d["status"] == "running" and d.get("run_id") and d.get("thread_id"):
                self._track(self._watch_and_finish(d), f"adopt-{d['id']}")
                continue
            if d["status"] != "queued":
                d = await self._update(d["id"], status="queued")
            self.queues.put(
                QItem(
                    dispatch_id=d["id"],
                    event_id=d["event_id"],
                    priority=d["priority"],
                    graph_id=d.get("graph_id") or "",
                    thread_id=d.get("thread_id"),
                )
            )
        if rows:
            self.wake()

    def _track(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._tasks.discard(t)
            self.wake()

        task.add_done_callback(_done)
        return task

    # --- helpers ---------------------------------------------------------------------------------------------------
    async def _update(self, dispatch_id: str, **fields: Any) -> dict[str, Any]:
        row = await self.db.run(self.db.update_dispatch, dispatch_id, **fields)
        self.hub.publish("dispatches", d_public(row))
        return row

    async def _emit(
        self, etype: str, d: dict[str, Any], key_suffix: str, payload: dict[str, Any], severity: str = "info"
    ) -> None:
        ev = await self.db.run(self.db.get_event, d["event_id"])
        try:
            await self.bus.emit(
                etype,
                idempotency_key=f"{etype}:{d['id']}:{key_suffix}",
                payload={
                    "dispatch_id": d["id"],
                    "event_id": d["event_id"],
                    "graph_id": d.get("graph_id"),
                    "thread_id": d.get("thread_id"),
                    "run_id": d.get("run_id"),
                    **payload,
                },
                severity=severity,
                component=(ev or {}).get("component"),
                correlation_id=(ev or {}).get("correlation_id"),
                causation_id=d["event_id"],
                scenario_id=(ev or {}).get("scenario_id"),
            )
        except Exception as exc:  # observability must never break dispatching
            log.warning("run_event_emit_failed", type=etype, error=repr(exc))

    # --- executing one dispatch ----------------------------------------------------------------------------------
    async def _execute(self, item: QItem) -> None:
        d = await self.db.run(self.db.get_dispatch, item.dispatch_id)
        if d is None or d["status"] not in {"queued", "retrying"}:
            return
        plan = d["plan"]
        if plan.get("action") == "resume_thread":
            await self._execute_resume_event(d)
            return
        self.dispatched_total += 1
        attempts = d["attempts"] + 1
        d = await self._update(
            d["id"],
            status="running",
            attempts=attempts,
            started_at=d.get("started_at") or self._now_iso(),
            error=None,
            next_attempt_at=None,
        )
        await self._emit("run.started", d, str(attempts), {"attempt": attempts, "mode": d["mode"]})
        try:
            run = await self._start_run(d)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(d, "run_create_failed", repr(exc))
            return
        d = await self._update(d["id"], run_id=run["run_id"])
        await self._watch_and_finish(d)

    async def _start_run(self, d: dict[str, Any]) -> dict[str, Any]:
        plan, es = d["plan"], self.es
        chaos = plan.get("chaos")
        if chaos == "poison":
            raise ChaosError("poison event: dispatch fails on every attempt")
        if chaos == "fail_once" and d["attempts"] == 1:
            raise ChaosError("fail_once: injected first-attempt failure")
        if chaos == "slow":
            await self._sleep(es.chaos_slow_s)
        client = self.client
        thread_id, graph = plan["thread_id"], plan["graph_id"]
        md = dict(plan["thread_metadata"])
        await client.threads.create(thread_id=thread_id, if_exists="do_nothing", graph_id=graph, metadata=md)
        try:  # create(do_nothing) leaves existing metadata untouched: merge ours (event -> thread correlation)
            await client.threads.update(thread_id, metadata=md)
        except Exception as exc:
            log.warning("thread_metadata_update_failed", thread_id=thread_id, error=repr(exc))
        resume = d["mode"] == "resume"
        kwargs: dict[str, Any] = {
            "input": None if resume else plan.get("input"),
            "metadata": {**md, "foghorn_dispatch_id": d["id"]},
            "multitask_strategy": plan.get("multitask_strategy", "enqueue"),
            "on_completion": plan.get("on_completion", "keep"),
            "stream_mode": list(es.run_stream_modes),
            "stream_subgraphs": es.run_stream_subgraphs,
            "stream_resumable": es.run_stream_resumable,
            "if_not_exists": "create",
        }
        ctx = plan.get("context") or {}
        if ctx:
            if es.run_config_mode == "context":
                kwargs["context"] = ctx
            else:
                kwargs["config"] = {"configurable": ctx}
        run = await client.runs.create(thread_id, plan.get("assistant_id") or graph, **kwargs)
        return dict(run)

    async def _poll_run(self, d: dict[str, Any]) -> tuple[str, str | None]:
        """Wait for a run to end. Returns (status, error)."""
        client = self.client
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.es.run_timeout_s
        while True:
            run = await client.runs.get(d["thread_id"], d["run_id"])
            status = str(run.get("status"))
            if status in TERMINAL_RUN:
                return status, (str(run.get("error")) if run.get("error") else None)
            if status not in {"pending", "running"}:
                return status, f"unexpected run status {status!r}"
            if loop.time() > deadline:
                with contextlib.suppress(Exception):
                    await client.runs.cancel(d["thread_id"], d["run_id"])
                return "timeout", f"run exceeded {self.es.run_timeout_s:g}s"
            await self._sleep(self.es.run_poll_interval_s)

    async def _watch_and_finish(self, d: dict[str, Any]) -> None:
        try:
            status, error = await self._poll_run(d)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(d, "run_failed", f"watch failed: {exc!r}")
            return
        if status in {"error", "timeout"}:
            await self._handle_failure(d, "run_failed" if status == "error" else "timeout", error or status)
            return
        try:
            thread = await self.client.threads.get(d["thread_id"])
        except Exception:
            thread = {}
        if status == "interrupted" or thread.get("status") == "interrupted":
            await self._finish_interrupted(d, thread)
        else:
            await self._finish_success(d)

    async def _finish_success(self, d: dict[str, Any]) -> None:
        d = await self._update(d["id"], status="succeeded", finished_at=self._now_iso(), error=None)
        result: dict[str, Any] | None = None
        if d.get("graph_id") == "event_router":
            try:
                state = await self.client.threads.get_state(d["thread_id"])
                r = (state.get("values") or {}).get("result")
                result = r if isinstance(r, dict) else None
            except Exception as exc:
                log.warning("router_result_unavailable", dispatch_id=d["id"], error=repr(exc))
        await self._emit("run.finished", d, "done", {"status": "succeeded", "summary": (result or {}).get("summary")})
        if d.get("graph_id") == "event_router":
            await self._emit("triage.completed", d, "done", _triage_payload(result))

    async def _finish_interrupted(self, d: dict[str, Any], thread: dict[str, Any]) -> None:
        d = await self._update(d["id"], status="interrupted", finished_at=self._now_iso())
        pending = [i["id"] for i in flatten_interrupts(thread)]
        await self._emit("run.interrupted", d, d.get("run_id") or "", {"interrupt_ids": pending})
        if self.on_interrupted and d.get("thread_id"):
            try:
                await self.on_interrupted(d["thread_id"])
            except Exception as exc:
                log.warning("approvals_reconcile_failed", thread_id=d["thread_id"], error=repr(exc))

    async def _handle_failure(self, d: dict[str, Any], cls: str, error: str) -> None:
        plan = d["plan"]
        attempts = d["attempts"]
        maxa = int(plan.get("max_attempts") or self.es.max_attempts)
        poison = plan.get("chaos") == "poison"
        error = error[:500]
        if attempts < maxa and cls != "cancelled":
            backoff = self.es.backoff_s or [1.0]
            delay = float(backoff[min(attempts - 1, len(backoff) - 1)])
            mode = "resume" if cls in {"run_failed", "timeout"} and d.get("run_id") else d["mode"]
            nxt = to_iso(self._wall() + timedelta(seconds=delay), ms=True)
            d = await self._update(d["id"], status="retrying", error=error, next_attempt_at=nxt, mode=mode)
            await self._emit(
                "run.failed",
                d,
                str(attempts),
                {"error": error, "will_retry": True, "retry_in_s": delay, "class": cls},
                "medium",
            )
            item = QItem(
                dispatch_id=d["id"],
                event_id=d["event_id"],
                priority=d["priority"],
                graph_id=d.get("graph_id") or "",
                thread_id=d.get("thread_id"),
            )
            self._track(self._retry_later(item, delay), f"retry-{d['id']}")
            return
        checkpoint_id: str | None = None
        if d.get("thread_id") and cls in {"run_failed", "timeout"}:
            with contextlib.suppress(Exception):
                st = await self.client.threads.get_state(d["thread_id"])
                checkpoint_id = (st.get("checkpoint") or {}).get("checkpoint_id")
        await self._dead_letter(d, "poison" if poison else cls, error, checkpoint_id, poison=poison)

    async def _retry_later(self, item: QItem, delay: float) -> None:
        await self._sleep(delay)
        victims = self.queues.put(item)
        if victims:
            await self.bus.shed(victims)
        self.wake()

    async def _dead_letter(
        self, d: dict[str, Any], reason: str, error: str, checkpoint_id: str | None, *, poison: bool
    ) -> None:
        def _sync() -> dict[str, Any]:
            with self.db.transaction():
                self.db.update_dispatch(d["id"], status="dead", error=error, finished_at=self._now_iso())
                ev = self.db.get_event(d["event_id"])
                self.db.set_event_status(d["event_id"], "dlq", reason)
                return self.db.insert_dead_letter(
                    reason=reason,
                    created_at=self._now_iso(),
                    attempts=d["attempts"],
                    last_error=error,
                    event_id=d["event_id"],
                    dispatch_id=d["id"],
                    thread_id=d.get("thread_id"),
                    run_id=d.get("run_id"),
                    checkpoint_id=checkpoint_id,
                    envelope=envelope_state(ev) if ev else None,
                    poison=poison,
                )

        dl = await self.db.run(_sync)
        row = await self.db.run(self.db.get_dispatch, d["id"])
        if row:
            self.hub.publish("dispatches", d_public(row))
        await self._emit(
            "run.failed",
            row or d,
            "final",
            {"error": error, "will_retry": False, "class": reason, "dead_letter_id": dl["id"]},
            "high",
        )
        await self._emit(
            "dlq.added", row or d, dl["id"], {"dead_letter_id": dl["id"], "reason": reason, "poison": poison}, "high"
        )

    # --- resume via event (rule r-approval-decided) --------------------------------------------------------------
    async def _execute_resume_event(self, d: dict[str, Any]) -> None:
        d = await self._update(d["id"], status="running", attempts=d["attempts"] + 1, started_at=self._now_iso())
        plan = d["plan"]
        try:
            if self.resume_handler is None:
                raise DispatchError("not_ready", "no approvals service wired", 503)
            out = await self.resume_handler(plan)
            await self._update(
                d["id"],
                status="succeeded",
                finished_at=self._now_iso(),
                thread_id=out.get("thread_id"),
                run_id=out.get("run_id"),
            )
        except DispatchError as exc:
            await self._update(
                d["id"], status="failed", error=f"{exc.code}: {exc.message}"[:500], finished_at=self._now_iso()
            )
        except Exception as exc:
            await self._update(d["id"], status="failed", error=repr(exc)[:500], finished_at=self._now_iso())

    # --- runs started elsewhere (approval resume) --------------------------------------------------------------------
    async def track_external_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        graph_id: str,
        event_id: str,
        route: str,
        incident_id: str | None = None,
        priority: str = "P0",
    ) -> dict[str, Any]:
        """Register a run the approvals service created (kind=resume) and watch it like any other dispatch."""
        plan = {
            "graph_id": graph_id,
            "assistant_id": graph_id,
            "thread_id": thread_id,
            "incident_id": incident_id,
            "action": "tracked",
        }

        def _sync() -> dict[str, Any]:
            with self.db.transaction():
                # a resume supersedes the interrupted dispatches that were waiting on this thread
                self.db.execute(
                    "UPDATE dispatches SET status = 'succeeded' WHERE thread_id = ? AND status = 'interrupted'",
                    (thread_id,),
                )
                gen = self.db.next_generation(event_id, route)
                row = self.db.insert_dispatch(
                    event_id=event_id,
                    route=route,
                    priority=priority,
                    plan=plan,
                    generation=gen,
                    kind="resume",
                    mode="command",
                    status="running",
                    enqueued_at=self._now_iso(),
                    run_id=run_id,
                )
                return self.db.update_dispatch(row["id"], attempts=1, started_at=self._now_iso())

        d = await self.db.run(_sync)
        self.hub.publish("dispatches", d_public(d))
        self._track(self._watch_and_finish(d), f"watch-{d['id']}")
        return d

    # --- DLQ actions -------------------------------------------------------------------------------------------------
    async def retry_dead_letter(self, dlq_id: str) -> dict[str, Any]:
        dl = await self.db.run(self.db.get_dead_letter, dlq_id)
        if dl is None:
            raise DispatchError("not_found", f"dead letter {dlq_id!r} not found", 404)
        if dl["status"] != "open":
            raise DispatchError("conflict", f"dead letter already {dl['status']}", 409)
        if dl["reason"] == "schema_invalid":
            res = await self.bus.ingest(dl["raw_body"] or "", origin="api", raw_body=dl["raw_body"])
            if res.http_status >= 400:
                raise DispatchError("schema_invalid", "the stored body is still invalid", 422, res.body())
            await self.db.run(
                self.db.update_dead_letter,
                dlq_id,
                status="retried",
                resolved_at=self._now_iso(),
                resolved_by="retry",
                resolution=res.event_id,
            )
            await self.bus.emit(
                "dlq.retried",
                idempotency_key=f"dlq.retried:{dlq_id}",
                payload={"dead_letter_id": dlq_id, "event_id": res.event_id},
            )
            return {"status": "retried", "dispatch": None, "event_id": res.event_id}
        d = await self._redispatch(dl)
        await self.db.run(
            self.db.update_dead_letter,
            dlq_id,
            status="retried",
            resolved_at=self._now_iso(),
            resolved_by="retry",
            resolution=d["id"] if d else None,
        )
        await self.bus.emit(
            "dlq.retried",
            idempotency_key=f"dlq.retried:{dlq_id}",
            payload={"dead_letter_id": dlq_id, "event_id": dl["event_id"], "dispatch_id": d["id"] if d else None},
            causation_id=dl["event_id"],
        )
        return {"status": "retried", "dispatch": d_public(d) if d else None, "event_id": dl["event_id"]}

    async def _redispatch(self, dl: dict[str, Any]) -> dict[str, Any] | None:
        def _sync() -> dict[str, Any] | None:
            with self.db.transaction():
                old = self.db.get_dispatch(dl["dispatch_id"]) if dl.get("dispatch_id") else None
                if old is None:  # e.g. no_route: evaluate the rules again against the stored event
                    return self.bus.reroute_sync(dl["event_id"])
                gen = self.db.next_generation(old["event_id"], old["route"])
                plan = dict(old["plan"])
                plan["chaos"] = (
                    None if old["plan"].get("chaos") == "poison" else plan.get("chaos")
                )  # an operator retry gets a clean run
                mode = "resume" if dl["reason"] in {"run_failed", "timeout"} and old.get("run_id") else "start"
                row = self.db.insert_dispatch(
                    event_id=old["event_id"],
                    route=old["route"],
                    priority=old["priority"],
                    plan=plan,
                    generation=gen,
                    kind=old["kind"],
                    mode=mode,
                    enqueued_at=self._now_iso(),
                    parent_dispatch_id=old["id"],
                )
                self.db.set_event_status(old["event_id"], "routed", None)
                return row

        d = await self.db.run(_sync)
        if d is None:
            raise DispatchError("no_route", "no routing rule matches this event", 422)
        self.hub.publish("dispatches", d_public(d))
        self.queues.put(
            QItem(
                dispatch_id=d["id"],
                event_id=d["event_id"],
                priority=d["priority"],
                graph_id=d.get("graph_id") or "",
                thread_id=d.get("thread_id"),
            )
        )
        self.wake()
        return d

    async def discard_dead_letter(self, dlq_id: str, *, by: str, reason: str | None) -> dict[str, Any]:
        dl = await self.db.run(self.db.get_dead_letter, dlq_id)
        if dl is None:
            raise DispatchError("not_found", f"dead letter {dlq_id!r} not found", 404)
        if dl["status"] != "open":
            raise DispatchError("conflict", f"dead letter already {dl['status']}", 409)
        row = await self.db.run(
            self.db.update_dead_letter,
            dlq_id,
            status="discarded",
            resolved_at=self._now_iso(),
            resolved_by=by,
            resolution=reason,
        )
        await self.bus.emit(
            "dlq.discarded",
            idempotency_key=f"dlq.discarded:{dlq_id}",
            payload={"dead_letter_id": dlq_id, "by": by, "reason": reason},
            causation_id=dl.get("event_id"),
        )
        return row

    # --- stats ---------------------------------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "in_flight": self.running_total,
            "workers": self.es.max_runs,
            "per_graph": {
                g: {"running": self.running_by_graph.get(g, 0), "cap": c} for g, c in self.es.per_graph_max_runs.items()
            },
            "dispatched_total": self.dispatched_total,
        }


def _triage_payload(result: dict[str, Any] | None) -> dict[str, Any]:
    """``triage.completed`` payload: the full result plus the headline fields the live feed and UI read directly
    (lifted from the router's ``triage`` artifact: decision, severity, route, reason_code)."""
    payload: dict[str, Any] = {"result": result, "status": (result or {}).get("status", "ok")}
    for art in (result or {}).get("artifacts") or []:
        if isinstance(art, dict) and art.get("name") == "triage" and isinstance(art.get("data"), dict):
            data = art["data"]
            for k in ("decision", "severity", "route", "reason_code", "noise_probability"):
                if k in data:
                    payload[k] = data[k]
            break
    return payload
