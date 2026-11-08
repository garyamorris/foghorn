"""Replay engine: publishes a stream's or scenario's events through ``EventBus.ingest`` on the VIRTUAL clock.

* Speed 1x-600x (any ``0 < x <= 600``): the virtual clock advances ``speed`` seconds per real second and each event is
  released when virtual time reaches its ``occurred_at``. Per-component order is preserved (events are time-ordered;
  optional ``jitter_s`` perturbs only DELIVERY order across components, deterministically seeded).
* pause / resume / step / speed / stop; ``loop`` restarts the stream and keeps virtual time moving forward.
* Respects backpressure: while the queues are above high-water the engine pauses the virtual clock and waits for
  low-water instead of flooding (a 429 from ingest is retried the same way).
* Event ids are ``evt_<stream>_<seq>``; the k-th re-run of the same stream/scenario appends ``_r<k>`` so a stream can be
  replayed many times without being swallowed by delivery idempotency.
* ``sleep`` and the wall clock are injectable so tests drive it with a fake clock in microseconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from foghorn import clock
from foghorn.events.config import EventsSettings
from foghorn.events.dataport import DataPort
from foghorn.events.db import StateDB
from foghorn.events.dispatcher import DispatchError
from foghorn.events.pipeline import EventBus
from foghorn.events.vclock import VirtualClock, parse_iso, to_iso
from foghorn.logging import get_logger

log = get_logger("events.replay")

Sleep = Callable[[float], Awaitable[None]]
ACTIVE = {"running", "paused"}


class _Session:
    def __init__(
        self,
        sid: str,
        kind: str,
        name: str,
        speed: float,
        loop: bool,
        events: list[tuple[datetime, datetime, dict[str, Any]]],
        launch_id: str | None,
        correlation_id: str | None,
        run_k: int,
    ) -> None:
        self.id, self.kind, self.name, self.speed, self.loop = sid, kind, name, speed, loop
        self.events = events
        self.launch_id, self.correlation_id, self.run_k = launch_id, correlation_id, run_k
        self.state = "running"
        self.position = 0
        self.loop_index = 0
        self.virtual_at: str | None = None
        self.started_at: str | None = None
        self.bp_paused = False
        self.error: str | None = None
        self.user_paused = False
        self.prev_speed = 1.0  # clock speed / pause state to give back when the session ends
        self.prev_paused = False
        self.step_budget = 0
        self.resume_evt = asyncio.Event()
        self.resume_evt.set()
        self.task: asyncio.Task[None] | None = None
        self.span = (max(e[0] for e in events) - min(e[0] for e in events)) if events else timedelta(0)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "kind": self.kind,
            "name": self.name,
            "speed": self.speed,
            "loop": self.loop,
            "position": self.position,
            "total": len(self.events),
            "loop_index": self.loop_index,
            "virtual_at": self.virtual_at,
            "started_at": self.started_at,
            "backpressure_paused": self.bp_paused,
            "launch_id": self.launch_id,
            "error": self.error,
        }


class ReplayEngine:
    def __init__(
        self,
        *,
        db: StateDB,
        es: EventsSettings,
        bus: EventBus,
        vclock: VirtualClock,
        data: DataPort,
        sleep: Sleep = asyncio.sleep,
        wall: Callable[[], datetime] | None = None,
    ) -> None:
        self.db, self.es, self.bus, self.vclock, self.data = db, es, bus, vclock, data
        self._sleep = sleep
        self._wall = wall or clock.now
        self.session: _Session | None = None

    # --- views -----------------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.session is not None and self.session.state in ACTIVE

    def status(self) -> dict[str, Any]:
        s = self.session
        return {"state": s.state if s else "idle", "session": s.public() if s else None}

    # --- control ---------------------------------------------------------------------------------------------------
    async def start(
        self,
        *,
        stream: str | None = None,
        scenario_id: str | None = None,
        speed: float | None = None,
        loop: bool = False,
        start_at: str | None = None,
        jitter_s: float = 0.0,
        replace: bool = False,
        run_ctx: dict[str, Any] | None = None,
        mode: str = "events",
    ) -> dict[str, Any]:
        if bool(stream) == bool(scenario_id):
            raise DispatchError("bad_request", "give exactly one of `stream` / `scenario_id`", 400)
        spd = float(speed if speed is not None else self.es.replay_speed)
        if not 0 < spd <= 600:
            raise DispatchError("bad_request", "speed must be in (0, 600]", 400)
        if self.active:
            if not replace:
                raise DispatchError("replay_active", "a replay session is already running (use replace=true)", 409)
            await self.stop()
        kind, name = ("scenario", str(scenario_id)) if scenario_id else ("stream", str(stream))
        raw = await asyncio.to_thread(self._load, kind, name)
        events = self._prepare(raw, start_at, jitter_s, seed=f"{kind}:{name}")
        if not events:
            raise DispatchError("not_found", f"{kind} {name!r} has no events to replay", 404)
        run_k = await self.db.run(self.db.next_n, f"replay_runs:{name}")
        sid = f"rep_{await self.db.run(self.db.next_n, 'replay_session'):04d}"
        ctx = dict(run_ctx or {})
        ctx.setdefault("scenario_id", name if kind == "scenario" else None)
        launch = await self.db.run(
            self.db.insert_launch,
            scenario_id=name,
            mode=mode,
            speed=spd,
            context={k: v for k, v in ctx.items() if v is not None},
            created_at=to_iso(self._wall(), ms=True),
            events_total=len(events),
            correlation_id=None,
        )
        corr = f"corr_{name}_{launch['id']}" if kind == "scenario" else None
        await self.db.run(self.db.update_launch, launch["id"], correlation_id=corr, replay_id=sid)
        sess = _Session(sid, kind, name, spd, loop, events, launch["id"], corr, run_k)
        sess.started_at = to_iso(self._wall(), ms=True)
        sess.prev_speed, sess.prev_paused = self.vclock.speed, self.vclock.paused
        self.vclock.set(virtual_now=events[0][0], speed=spd, paused=False)
        sess.virtual_at = to_iso(events[0][0])
        self.session = sess
        await self.bus.emit(
            "replay.started",
            idempotency_key=f"replay.started:{sid}",
            payload={
                "session_id": sid,
                "kind": kind,
                "name": name,
                "speed": spd,
                "events": len(events),
                "launch_id": launch["id"],
            },
            scenario_id=name if kind == "scenario" else None,
            correlation_id=corr,
        )
        sess.task = asyncio.create_task(self._run(sess), name=f"replay-{sid}")
        return sess.public()

    def _load(self, kind: str, name: str) -> list[dict[str, Any]]:
        if kind == "scenario":
            return list(self.data.scenario_events(name))
        return list(self.data.iter_stream(name))

    def _prepare(
        self, raw: list[dict[str, Any]], start_at: str | None, jitter_s: float, *, seed: str
    ) -> list[tuple[datetime, datetime, dict[str, Any]]]:
        timed = [(parse_iso(str(e["occurred_at"])), i, e) for i, e in enumerate(raw) if e.get("occurred_at")]
        timed.sort(key=lambda x: (x[0], x[1]))
        if start_at:
            cut = parse_iso(start_at)
            timed = [t for t in timed if t[0] >= cut]
        if jitter_s > 0 and timed:
            rng = random.Random(seed)  # noqa: S311 - deterministic delivery jitter, not cryptography
            last: dict[str | None, datetime] = {}
            keyed: list[tuple[datetime, int, datetime, dict[str, Any]]] = []
            for t, i, e in timed:
                comp = (e.get("subject") or {}).get("component")
                dt = t + timedelta(seconds=rng.uniform(0, jitter_s))
                dt = max(dt, last.get(comp, dt))  # never reorder events of the same component
                last[comp] = dt
                keyed.append((dt, i, t, e))
            keyed.sort(key=lambda x: (x[0], x[1]))
            return [(dt, t, e) for dt, _i, t, e in keyed]
        return [(t, t, e) for t, _i, e in timed]

    async def pause(self) -> dict[str, Any]:
        s = self._need()
        if s.state == "running":
            s.user_paused, s.state = True, "paused"
            s.resume_evt.clear()
            self.vclock.pause()
        return s.public()

    async def resume(self) -> dict[str, Any]:
        s = self._need()
        if s.state == "paused":
            s.user_paused, s.state = False, "running"
            s.resume_evt.set()
            if not s.bp_paused:
                self.vclock.resume()
        return s.public()

    async def step(self) -> dict[str, Any]:
        s = self._need()
        if s.state == "running":
            await self.pause()
        if s.state == "paused" and s.position < len(s.events):
            s.step_budget += 1
            s.resume_evt.set()
        return s.public()

    async def set_speed(self, speed: float) -> dict[str, Any]:
        if not 0 < speed <= 600:
            raise DispatchError("bad_request", "speed must be in (0, 600]", 400)
        s = self._need()
        s.speed = float(speed)
        self.vclock.set(speed=float(speed))
        return s.public()

    async def stop(self) -> dict[str, Any]:
        s = self.session
        if s is None:
            return {"state": "idle", "session": None}
        if s.state in ACTIVE:
            s.state = "stopped"
            s.resume_evt.set()
            if s.task:
                s.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await s.task
            self.vclock.set(
                speed=s.prev_speed, paused=s.prev_paused
            )  # also covers a task cancelled before its first step
            await self.bus.emit(
                "replay.finished",
                idempotency_key=f"replay.finished:{s.id}",
                payload={"session_id": s.id, "state": "stopped", "position": s.position},
                correlation_id=s.correlation_id,
            )
        return {"state": s.state, "session": s.public()}

    async def wait_done(self, timeout: float = 30.0) -> None:  # noqa: ASYNC109
        s = self.session
        if s and s.task:
            await asyncio.wait_for(asyncio.shield(s.task), timeout)

    def _need(self) -> _Session:
        if self.session is None or self.session.state not in ACTIVE:
            raise DispatchError("conflict", "no active replay session", 409)
        return self.session

    # --- the loop --------------------------------------------------------------------------------------------------
    async def _run(self, s: _Session) -> None:
        try:
            while True:
                n = len(s.events)
                while s.position < n:
                    deliver, occurred, raw = s.events[s.position]
                    stepping = await self._gate(s)
                    shift = (s.span + timedelta(seconds=1)) * s.loop_index
                    await self._wait_until(s, deliver + shift, stepping)
                    await self._publish(s, raw, occurred + shift)
                    s.position += 1
                    s.virtual_at = to_iso(deliver + shift)
                if not s.loop:
                    break
                s.loop_index += 1
                s.position = 0
            s.state = "finished"
            await self.bus.emit(
                "replay.finished",
                idempotency_key=f"replay.finished:{s.id}",
                payload={"session_id": s.id, "state": "finished", "events": len(s.events), "loops": s.loop_index},
                correlation_id=s.correlation_id,
            )
        except asyncio.CancelledError:
            if s.state in ACTIVE:
                s.state = "stopped"
            raise
        except Exception as exc:
            log.error("replay_failed", session=s.id, error=repr(exc))
            s.state, s.error = "failed", repr(exc)[:300]
        finally:
            # a finished/stopped replay hands the clock back at its last position: it must not keep racing at 600x
            # (the scheduler would fire a shift handoff every 48 real seconds)
            self.vclock.set(speed=s.prev_speed, paused=s.prev_paused)

    async def _gate(self, s: _Session) -> bool:
        """Wait while paused / backpressured. Returns True when this event was released by ``step``."""
        while True:
            if s.state == "stopped":
                raise asyncio.CancelledError
            if s.user_paused:
                if s.step_budget > 0:
                    s.step_budget -= 1
                    return True
                s.resume_evt.clear()
                await s.resume_evt.wait()
                continue
            if self.bus.queues.bp_active:
                await self._bp_wait(s)
                continue
            return False

    async def _bp_wait(self, s: _Session) -> None:
        s.bp_paused = True
        was_paused = self.vclock.paused
        self.vclock.pause()
        try:
            await self.bus.queues.clear_event.wait()
        finally:
            s.bp_paused = False
            if not was_paused and not s.user_paused:
                self.vclock.resume()

    async def _wait_until(self, s: _Session, target: datetime, stepping: bool) -> None:
        if stepping:
            if self.vclock.now() < target:
                self.vclock.set(virtual_now=target)
            return
        # Idle-gap compression: no operator watches 90 quiet virtual minutes at 60x. When the next event is farther
        # away than `replay_idle_gap_s` virtual seconds, jump the virtual clock to just before it (event order and
        # timestamps are untouched; only the dead air is skipped). 0 disables.
        cap = float(self.es.replay_idle_gap_s)
        if cap > 0 and (target - self.vclock.now()).total_seconds() > cap:
            self.vclock.set(virtual_now=target - timedelta(seconds=cap))
        while True:
            now = self.vclock.now()
            if now >= target:
                return
            delay = min((target - now).total_seconds() / max(self.vclock.speed, 1e-9), 0.25)
            await self._sleep(max(delay, 0.0005))
            if s.user_paused or self.bus.queues.bp_active:
                stepping = await self._gate(s)
                if stepping:
                    self.vclock.set(virtual_now=max(target, self.vclock.now()))
                    return

    async def _publish(self, s: _Session, raw: dict[str, Any], occurred: datetime) -> None:
        trace = dict(raw.get("trace") or {})
        seq = trace.get("seq") or (s.position + 1)
        suffix = (f"_r{s.run_k}" if s.run_k > 1 else "") + (f"_L{s.loop_index}" if s.loop_index else "")
        base_id = raw.get("id") or f"evt_{s.name}_{int(seq):05d}"
        e = dict(raw)
        e["id"] = f"{base_id}{suffix}"
        e["source"] = raw.get("source") or f"replay:{s.name}"
        ik = raw.get("idempotency_key")
        e["idempotency_key"] = f"{ik}{suffix}" if ik else e["id"]
        e["occurred_at"] = to_iso(occurred, ms=occurred.microsecond != 0)
        trace["seq"] = seq
        if s.kind == "stream":
            trace.setdefault("stream", s.name)
        else:
            trace.setdefault("scenario_id", s.name)
        e["trace"] = trace
        attrs = dict(raw.get("attributes") or {})
        attrs["launch_id"] = s.launch_id
        e["attributes"] = attrs
        if not e.get("correlation_id") and s.correlation_id:
            e["correlation_id"] = s.correlation_id
        for _ in range(200):
            res = await self.bus.ingest(e, origin="replay")
            if res.http_status != 429:
                if res.http_status >= 400:
                    log.warning("replay_event_rejected", session=s.id, status=res.http_status, error=res.error)
                return
            await self._bp_wait(s)
            await self._sleep(0.001)
        raise RuntimeError("queues never drained; replay gave up")
