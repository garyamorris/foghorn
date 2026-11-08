"""The ingest pipeline: ``EventBus.ingest()`` is the ONE choke point every event passes through.

    validate -> idempotency -> semantic dedupe -> maintenance suppression -> routing rules -> backpressure
             -> durable store (+ dispatch rows + lifecycle projections, one SQLite transaction)
             -> priority queues -> dispatcher

Webhooks, UI injection, replay, the scheduler, the lifecycle sink and the dispatcher's own ``run.*`` events all call
``ingest``; none has a private path to a graph. The synchronous half runs in a worker thread (``asyncio.to_thread``,
so SQLite never touches the event loop) while holding an ``asyncio.Lock`` so identical events racing each other are
serialised and dedupe is exact. The asynchronous half (queueing, shedding, SSE broadcast) runs back on the loop.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from foghorn import clock
from foghorn.contracts.events import SEVERITY_ORDER, EventEnvelope
from foghorn.events.config import EventsSettings
from foghorn.events.db import StateDB, dumps
from foghorn.events.materialize import materialize
from foghorn.events.queues import PriorityQueues, QItem
from foghorn.events.routing import PlanStep, RoutingTable, StormTracker
from foghorn.events.sse import SSEHub
from foghorn.events.vclock import VirtualClock, parse_iso, to_iso
from foghorn.logging import get_logger

log = get_logger("events.pipeline")

ENVELOPE_KEYS = tuple(EventEnvelope.model_fields)
DEDUPE_TYPES = frozenset(
    {
        "alert.fired",
        "slo.burn_rate",
        "healthcheck.failed",
        "customer.report",
        "deploy.started",
        "deploy.finished",
        "deploy.rolled_back",
        "config.changed",
        "flag.changed",
        "migration.applied",
    }
)
MAINTENANCE_SUPPRESSED_TYPES = frozenset({"alert.fired", "slo.burn_rate", "healthcheck.failed"})
RAW_BODY_CAP = 64 * 1024
CHAOS_VALUES = {"poison", "fail_once", "slow"}


# ---------------------------------------------------------------------------------------------------- helpers
def envelope_state(row: dict[str, Any]) -> dict[str, Any]:
    """The ``EventEnvelope.to_state()`` subset of a stored event row (what graphs receive in their input)."""
    return {k: row.get(k) for k in ENVELOPE_KEYS}


def event_summary(ev: dict[str, Any]) -> str:
    p = ev.get("payload") or {}
    detail = (
        p.get("summary") or p.get("title") or p.get("rule") or p.get("action") or p.get("job") or p.get("message") or ""
    )
    comp = ev.get("component") or (ev.get("subject") or {}).get("component") or ""
    text = f"{ev['type']} {str(ev.get('severity', '')).upper()}"
    if comp:
        text += f" {comp}"
    return f"{text}: {detail}" if detail else text


def dispatch_summary(d: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "route",
        "kind",
        "graph_id",
        "priority",
        "status",
        "attempts",
        "thread_id",
        "run_id",
        "incident_id",
        "generation",
    )
    return {k: d.get(k) for k in keys}


def decorate_event(
    row: dict[str, Any], dispatches: list[dict[str, Any]], *, parent_occurrences: int | None = None
) -> dict[str, Any]:
    out = dict(row)
    out["summary"] = event_summary(row)
    out["dispatches"] = [dispatch_summary(d) for d in dispatches]
    if parent_occurrences is not None:
        out["parent_occurrences"] = parent_occurrences
    return out


def default_dedupe_key(ev: dict[str, Any]) -> str | None:
    rule = (ev.get("payload") or {}).get("rule")
    comp = (ev.get("subject") or {}).get("component")
    if ev["type"] in {"alert.fired", "alert.resolved"} and rule:
        return f"alert:{comp}:{rule}"
    if ev["type"] in DEDUPE_TYPES and rule:
        return f"{ev['type']}:{comp}:{rule}"
    return None


def validation_details(exc: ValueError) -> list[dict[str, str]]:
    if isinstance(exc, ValidationError):
        return [
            {"loc": ".".join(str(x) for x in e["loc"]), "msg": str(e["msg"])}
            for e in exc.errors(include_url=False, include_context=False, include_input=False)
        ]
    return [{"loc": "", "msg": str(exc)}]


# ------------------------------------------------------------------------------------------------------ results
@dataclass
class IngestResult:
    http_status: int
    status: str  # routed | recorded | suppressed | shed | dlq | duplicate | invalid | backpressure | conflict
    event: dict[str, Any] | None = None
    reason: str | None = None
    duplicate_of: str | None = None
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    dead_letter_id: str | None = None
    retry_after: int | None = None
    error: dict[str, Any] | None = None

    @property
    def event_id(self) -> str | None:
        return self.event["id"] if self.event else None

    @property
    def ok(self) -> bool:
        return self.http_status < 300

    def body(self) -> dict[str, Any]:
        if self.error is not None:
            return {"error": self.error}
        if self.status == "duplicate":
            return {"status": "duplicate", "event_id": self.event_id}
        out: dict[str, Any] = {
            "event_id": self.event_id,
            "seq": self.event["seq"] if self.event else None,
            "status": self.status,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
            "dispatches": [dispatch_summary(d) for d in self.dispatches],
        }
        if self.dead_letter_id:
            out["dead_letter_id"] = self.dead_letter_id
        return out


@dataclass
class _Txn:
    result: IngestResult
    events: list[tuple[dict[str, Any], int | None]] = field(default_factory=list)  # (decorated row, parent_occurrences)
    new_dispatches: list[dict[str, Any]] = field(default_factory=list)


class _Lookup:
    """RouteLookup over the DB + storm tracker (used only inside the serialised sync half)."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._launch: dict[str, dict[str, Any]] = {}

    def open_incident_on(self, component: str | None) -> dict[str, Any] | None:
        return self.bus.db.open_incident_for_component(component)

    def in_storm(self, event: dict[str, Any]) -> bool:
        return self.bus.storm.in_storm(event)

    def storm_window_hit(self, event: dict[str, Any], count_ge: int, within_s: float) -> bool:
        return self.bus.storm.hit(event, count_ge, within_s)

    def storm_alerts(self) -> list[dict[str, Any]]:
        return self.bus.storm.alerts()

    def launch_context(self, event: dict[str, Any]) -> dict[str, Any]:
        lid = (event.get("attributes") or {}).get("launch_id")
        if not lid:
            return {}
        if lid not in self._launch:
            row = self.bus.db.get_launch(str(lid))
            self._launch[lid] = (row or {}).get("context", {})
        return self._launch[lid]


# ------------------------------------------------------------------------------------------------------- the bus
class EventBus:
    def __init__(
        self,
        *,
        db: StateDB,
        es: EventsSettings,
        vclock: VirtualClock,
        queues: PriorityQueues,
        hub: SSEHub,
        routing: RoutingTable,
        wall: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.es = es
        self.vclock = vclock
        self.queues = queues
        self.hub = hub
        self.routing = routing
        self._wall = wall or clock.now
        self.storm = StormTracker(es.storm_cooldown_s, es.storm_suppress_members)
        self._lock = asyncio.Lock()
        self._lookup = _Lookup(self)
        self.on_enqueue: Callable[[], None] | None = None
        self.events_total = 0
        self.rejected_total = 0
        self.backpressure_429_total = 0
        self._times: deque[float] = deque()

    # --- public API ----------------------------------------------------------------------------------------------
    async def ingest(
        self,
        raw: dict[str, Any] | str | bytes,
        *,
        origin: str = "api",
        idempotency_key: str | None = None,
        raw_body: str | None = None,
    ) -> IngestResult:
        """``origin``: ``api`` (HTTP/webhooks), ``replay`` (scenario/stream/ambient) or ``internal`` (never throttled)."""
        async with self._lock:
            txn = await asyncio.to_thread(self._ingest_sync, raw, origin, idempotency_key, raw_body)
            await self._after_commit(txn)
        return txn.result

    async def emit(
        self,
        type: str,
        *,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
        component: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        scenario_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> IngestResult:
        """Internal lifecycle/system event (source ``foghorn.internal``); never throttled."""
        env = EventEnvelope.model_validate(
            {
                "type": type,
                "source": "foghorn.internal",
                "severity": severity,
                "subject": {"component": component},
                "payload": payload or {},
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "dedupe_key": dedupe_key,
                "trace": {"scenario_id": scenario_id},
            }
        )
        return await self.ingest(env.to_state(), origin="internal")

    def rate_per_s(self, window_s: float = 60.0) -> float:
        return round(self.events_in_window(window_s) / window_s, 3)

    def events_in_window(self, window_s: float = 60.0) -> int:
        now = time.monotonic()
        while self._times and now - self._times[0] > 60.0:
            self._times.popleft()
        return sum(1 for t in self._times if now - t <= window_s)

    # --- sync half (worker thread, serialised) --------------------------------------------------------------------
    def _ingest_sync(
        self, raw: dict[str, Any] | str | bytes, origin: str, idem: str | None, raw_body: str | None
    ) -> _Txn:
        db = self.db
        now = self._wall()
        try:
            data = raw if isinstance(raw, dict) else json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("event must be a JSON object")
            env = EventEnvelope.model_validate(data)
            occurred = parse_iso(env.occurred_at) if env.occurred_at else None
            if env.type in {"", " "}:
                raise ValueError("type must not be empty")
        except ValueError as exc:
            return self._reject_invalid(raw, raw_body, exc, now, origin)

        ev = env.to_state()
        ev["received_at"] = to_iso(now, ms=True)
        occurred_dt = occurred or self.vclock.now()
        ev["occurred_at"] = to_iso(occurred_dt, ms=occurred_dt.microsecond != 0)
        if idem:
            ev["idempotency_key"] = idem
        if ev.get("dedupe_key") is None:
            ev["dedupe_key"] = default_dedupe_key(ev)
        ev["scenario_id"] = (ev.get("trace") or {}).get("scenario_id")
        launch_id = (ev.get("attributes") or {}).get("launch_id")
        ev["launch_id"] = launch_id
        ev["dedupe_scope"] = str(launch_id or "")

        # 1. delivery idempotency ----------------------------------------------------------------------------------
        if ev.get("idempotency_key"):
            prior = db.get_event_by_idempotency(ev["idempotency_key"])
            if prior:
                return _Txn(IngestResult(200, "duplicate", event=prior))
        if env.id:
            prior = db.get_event(env.id)
            if prior:
                if prior["source"] == ev["source"]:
                    return _Txn(IngestResult(200, "duplicate", event=prior))
                err = {
                    "code": "conflict",
                    "message": f"event id {env.id!r} already used by another source",
                    "details": None,
                }
                return _Txn(IngestResult(409, "conflict", error=err))

        # 2. semantic dedupe ----------------------------------------------------------------------------------------
        if ev["type"] in DEDUPE_TYPES and ev.get("dedupe_key"):
            cand = db.find_dedupe_candidate(ev["dedupe_key"], ev["type"], ev["dedupe_scope"])
            if (
                cand
                and abs((occurred_dt - parse_iso(cand["last_seen_at"] or cand["occurred_at"])).total_seconds())
                <= self.es.dedupe_window_s
            ):
                return self._store_suppressed(ev, "semantic_duplicate", duplicate_of=cand, occurred_dt=occurred_dt)

        # 3. maintenance windows ------------------------------------------------------------------------------------
        if ev["type"] in MAINTENANCE_SUPPRESSED_TYPES and self._in_maintenance(ev, occurred_dt):
            return self._store_suppressed(ev, "maintenance_window")

        # 4. routing ------------------------------------------------------------------------------------------------
        # RESERVE the number atomically (not a read-only peek): the dispatcher and lifecycle sink insert events without
        # taking the ingest lock, so a peeked number could be taken by one of them before this event is stored.
        seq = db.next_n("event_seq")
        ev["seq"] = seq
        ev["id"] = env.id or f"evt_{seq:07d}"
        steps = self.routing.plan(envelope_state(ev), self._lookup, self.es)

        # 5. backpressure (api/replay only; internal/lifecycle events are never throttled) ---------------------------------
        runnable = [s for s in steps if s.action in {"dispatch", "resume_thread"}]
        if origin in {"api", "replay"} and runnable and all(self.queues.would_exceed(s.priority) for s in runnable):
            self.backpressure_429_total += 1
            bp_err: dict[str, Any] = {
                "code": "backpressure",
                "message": "queues are above high-water; retry later",
                "details": {"retry_after": self.es.retry_after_s, "depth": self.queues.depth()},
            }
            return _Txn(IngestResult(429, "backpressure", retry_after=self.es.retry_after_s, error=bp_err))

        return self._store_routed(ev, steps, now)

    def _reject_invalid(self, raw: Any, raw_body: str | None, exc: ValueError, now: datetime, origin: str) -> _Txn:
        details = validation_details(exc)
        text = (
            raw_body
            if raw_body is not None
            else (
                raw
                if isinstance(raw, str)
                else dumps(raw)
                if not isinstance(raw, bytes)
                else raw.decode("utf-8", "replace")
            )
        )
        self.rejected_total += 1
        if origin == "internal":  # a broken internal emitter must not fill the DLQ with noise
            log.warning("internal_event_invalid", details=details)
            return _Txn(
                IngestResult(
                    422, "invalid", error={"code": "schema_invalid", "message": details[0]["msg"], "details": details}
                )
            )
        dl = self.db.insert_dead_letter(
            reason="schema_invalid",
            created_at=to_iso(now, ms=True),
            last_error="; ".join(f"{d['loc']}: {d['msg']}" if d["loc"] else d["msg"] for d in details)[:500],
            raw_body=text[:RAW_BODY_CAP],
        )
        txn = _Txn(IngestResult(422, "invalid", dead_letter_id=dl["id"]))
        sysrow = self._system_event(
            "dlq.added", {"dead_letter_id": dl["id"], "reason": "schema_invalid"}, now, severity="medium"
        )
        txn.events.append((sysrow, None))
        msg = "; ".join(f"{d['loc']}: {d['msg']}" if d["loc"] else d["msg"] for d in details)
        txn.result.error = {
            "code": "schema_invalid",
            "message": msg[:500],
            "details": {"errors": details, "dead_letter_id": dl["id"]},
        }
        return txn

    def _in_maintenance(self, ev: dict[str, Any], at: datetime) -> bool:
        for s in self.db.suppressions_for((ev.get("subject") or {}).get("component")):
            start = parse_iso(s["started_at"])
            end = parse_iso(s["ended_at"]) if s["ended_at"] else None
            if start <= at and (end is None or at < end):
                return True
        return False

    def _store_suppressed(
        self,
        ev: dict[str, Any],
        reason: str,
        *,
        duplicate_of: dict[str, Any] | None = None,
        occurred_dt: datetime | None = None,
    ) -> _Txn:
        ev["status"], ev["status_reason"] = "suppressed", reason
        parent_occ: int | None = None
        with self.db.transaction():
            ev["duplicate_of"] = duplicate_of["id"] if duplicate_of else None
            row = self.db.insert_event(ev)  # assigns seq atomically
            if duplicate_of:
                parent_occ = self.db.bump_occurrences(
                    duplicate_of["id"],
                    max(row["occurred_at"], duplicate_of["last_seen_at"] or duplicate_of["occurred_at"], key=parse_iso),
                )
        result = IngestResult(
            202,
            "suppressed",
            event=decorate_event(row, [], parent_occurrences=parent_occ),
            reason=reason,
            duplicate_of=row["duplicate_of"],
        )
        return _Txn(result, events=[(row, parent_occ)])

    def _store_routed(self, ev: dict[str, Any], steps: list[PlanStep], now: datetime) -> _Txn:
        db = self.db
        now_iso = to_iso(now, ms=True)
        status, reason = "recorded", None
        for st in steps:
            if st.action in {"dispatch", "resume_thread"}:
                status, reason = "routed", None
            elif st.action == "dead_letter" and status != "routed":
                status, reason = "dlq", st.reason or "no_route"
            elif st.action == "suppress" and status != "routed":
                status, reason = "suppressed", st.reason
        ev["status"], ev["status_reason"] = status, reason
        txn = _Txn(IngestResult(202, status, reason=reason))
        with db.transaction():
            row = db.insert_event(ev)
            created: list[dict[str, Any]] = []
            for st in steps:
                if st.action == "dispatch":
                    assert st.plan is not None
                    created.append(
                        db.insert_dispatch(
                            event_id=row["id"],
                            route=st.rule_id,
                            priority=st.priority,
                            plan=st.plan,
                            enqueued_at=now_iso,
                        )
                    )
                elif st.action == "resume_thread":
                    plan = {
                        "action": "resume_thread",
                        "graph_id": None,
                        "assistant_id": None,
                        "thread_id": None,
                        **self._resume_plan(row),
                    }
                    created.append(
                        db.insert_dispatch(
                            event_id=row["id"],
                            route=st.rule_id,
                            priority=st.priority,
                            plan=plan,
                            kind="resume",
                            enqueued_at=now_iso,
                        )
                    )
                elif st.action == "update_suppression_table":
                    comp = row.get("component")
                    if row["type"] == "maintenance.window.started":
                        db.open_suppression(comp, row["occurred_at"], row["id"])
                    else:
                        db.close_suppression(comp, row["occurred_at"])
                elif st.action == "dead_letter":
                    dl = db.insert_dead_letter(
                        reason=st.reason or "no_route",
                        created_at=now_iso,
                        event_id=row["id"],
                        envelope=envelope_state(row),
                        last_error=f"no routing rule matched {row['type']!r}",
                    )
                    txn.result.dead_letter_id = dl["id"]
                    txn.events.append(
                        (
                            self._system_event(
                                "dlq.added",
                                {"dead_letter_id": dl["id"], "reason": dl["reason"], "event_id": row["id"]},
                                now,
                                severity="medium",
                                causation_id=row["id"],
                            ),
                            None,
                        )
                    )
            if row["type"] == "alert.resolved" and row.get("dedupe_key"):
                db.close_dedupe_window(
                    row["dedupe_key"], "alert.fired", row.get("dedupe_scope", "") or ev["dedupe_scope"]
                )
            materialize(db, row)
            row = db.get_event(row["id"]) or row  # materialisation may have set a correlation id
        txn.new_dispatches = created
        txn.result.event = decorate_event(row, created)
        txn.result.dispatches = created
        txn.events.insert(0, (row, None))
        return txn

    @staticmethod
    def _resume_plan(row: dict[str, Any]) -> dict[str, Any]:
        p = row.get("payload") or {}
        response = (
            p.get("response")
            if isinstance(p.get("response"), dict)
            else {"decision": p.get("decision"), "decided_by": p.get("decided_by") or "api"}
        )
        return {
            "approval_id": p.get("approval_id"),
            "interrupt_id": p.get("interrupt_id"),
            "response": response,
            "nonce": p.get("nonce") or row["id"],
            "decided_by": p.get("decided_by") or "api",
        }

    def _system_event(
        self,
        type: str,
        payload: dict[str, Any],
        now: datetime,
        *,
        severity: str = "info",
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        occurred = self.vclock.now()
        return self.db.insert_event(
            {
                "type": type,
                "source": "foghorn.internal",
                "severity": severity,
                "occurred_at": to_iso(occurred, ms=occurred.microsecond != 0),
                "received_at": to_iso(now, ms=True),
                "subject": {"component": payload.get("component"), "environment": "prod"},
                "payload": payload,
                "causation_id": causation_id,
                "correlation_id": correlation_id,
                "trace": {},
                "attributes": {},
                "status": "recorded",
            }
        )

    # --- async half (event loop) ------------------------------------------------------------------------------------
    async def _after_commit(self, txn: _Txn) -> None:
        res = txn.result
        if res.http_status >= 400 or res.status == "duplicate":
            for row, _ in txn.events:  # e.g. the dlq.added marker of a rejected body
                self._broadcast(row, None, [])
            return
        self.events_total += 1
        self._times.append(time.monotonic())
        disp_by_event: dict[str, list[dict[str, Any]]] = {}
        for d in txn.new_dispatches:
            disp_by_event.setdefault(d["event_id"], []).append(d)
        shed_victims: list[QItem] = []
        for d in txn.new_dispatches:
            if d["kind"] == "resume" or d["plan"].get("action") == "resume_thread":
                item_graph = ""
            else:
                item_graph = d.get("graph_id") or ""
            shed_victims += self.queues.put(
                QItem(
                    dispatch_id=d["id"],
                    event_id=d["event_id"],
                    priority=d["priority"],
                    graph_id=item_graph,
                    thread_id=d.get("thread_id"),
                )
            )
        for row, parent_occ in txn.events:
            self._broadcast(row, parent_occ, disp_by_event.get(row["id"], []))
        for d in txn.new_dispatches:
            self.hub.publish("dispatches", d_public(d))
        if shed_victims:
            await self.shed(shed_victims, res)
        if txn.new_dispatches and self.on_enqueue:
            self.on_enqueue()

    def _broadcast(self, row: dict[str, Any], parent_occ: int | None, dispatches: list[dict[str, Any]]) -> None:
        self.hub.publish("events", decorate_event(row, dispatches, parent_occurrences=parent_occ), seq=row["seq"])

    async def shed(self, victims: list[QItem], res: IngestResult | None = None) -> None:
        rows = await asyncio.to_thread(self._shed_sync, [v.dispatch_id for v in victims])
        for r in rows:
            self._broadcast(r, None, [])
        if (
            res is not None
            and res.dispatches
            and all(d["id"] in {v.dispatch_id for v in victims} for d in res.dispatches)
        ):
            res.status, res.reason = "shed", "queue_hard_cap"
        for v in victims:
            row = await asyncio.to_thread(self.db.get_dispatch, v.dispatch_id)
            if row:
                self.hub.publish("dispatches", d_public(row))

    def _shed_sync(self, dispatch_ids: list[str]) -> list[dict[str, Any]]:
        now = self._wall()
        out: list[dict[str, Any]] = []
        for did in dispatch_ids:
            with self.db.transaction():
                d = self.db.get_dispatch(did)
                if d is None:
                    continue
                self.db.update_dispatch(
                    did, status="dead", error="shed: queue hard cap", finished_at=to_iso(now, ms=True)
                )
                self.db.set_event_status(d["event_id"], "shed", "queue_hard_cap")
                out.append(
                    self._system_event(
                        "event.shed",
                        {
                            "event_id": d["event_id"],
                            "dispatch_id": did,
                            "priority": d["priority"],
                            "graph_id": d.get("graph_id"),
                        },
                        now,
                        severity="medium",
                        causation_id=d["event_id"],
                    )
                )
        return out

    async def create_dispatch(
        self, event_id: str, route: str, priority: str, plan: dict[str, Any], *, kind: str = "run"
    ) -> dict[str, Any]:
        """Create + enqueue a dispatch that no routing rule produced (direct scenario launch)."""
        d = await asyncio.to_thread(
            self.db.insert_dispatch,
            event_id=event_id,
            route=route,
            priority=priority,
            plan=plan,
            kind=kind,
            enqueued_at=to_iso(self._wall(), ms=True),
        )
        victims = self.queues.put(
            QItem(
                dispatch_id=d["id"],
                event_id=event_id,
                priority=priority,
                graph_id=d.get("graph_id") or "",
                thread_id=d.get("thread_id"),
            )
        )
        self.hub.publish("dispatches", d_public(d))
        if victims:
            await self.shed(victims)
        if self.on_enqueue:
            self.on_enqueue()
        return d

    def reroute_sync(self, event_id: str) -> dict[str, Any] | None:
        """Re-evaluate the routing rules for a stored event (DLQ retry of ``no_route``); new dispatches get generation+1."""
        row = self.db.get_event(event_id)
        if row is None:
            return None
        now_iso = to_iso(self._wall(), ms=True)
        created: dict[str, Any] | None = None
        with self.db.transaction():
            for st in self.routing.plan(envelope_state(row), self._lookup, self.es):
                if st.action == "dispatch" and st.plan is not None:
                    gen = self.db.next_generation(event_id, st.rule_id)
                    d = self.db.insert_dispatch(
                        event_id=event_id,
                        route=st.rule_id,
                        priority=st.priority,
                        plan=st.plan,
                        generation=gen,
                        enqueued_at=now_iso,
                    )
                    created = created or d
            if created:
                self.db.set_event_status(event_id, "routed", None)
        return created

    # --- misc -----------------------------------------------------------------------------------------------------
    def severity_rank(self, name: str) -> int:
        return SEVERITY_ORDER.get(name, 0)


def d_public(d: dict[str, Any]) -> dict[str, Any]:
    """A dispatch row as the API exposes it (the stored plan is internal)."""
    out = {k: v for k, v in d.items() if k != "plan"}
    return out
