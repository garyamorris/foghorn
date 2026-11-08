"""Virtual cron scheduler and the (optional, off-by-default) ambient-noise generator.

* Cron expressions are evaluated against VIRTUAL time, so a 600x replay crosses an 8-hour shift boundary in about
  48 real seconds and ``system.cron.tick`` fires exactly as it would in production. Standard 5-field syntax in UTC:
  ``*``, ``*/n``, ``a``, ``a-b``, ``a-b/n`` and comma lists; day-of-week 0-7 (0 and 7 are Sunday).
* Every tick is an ordinary event (``source: cron``) routed by ``routing.yaml`` (``r-handoff`` -> ``shift_handoff``).
* Ambient noise replays a quiet stream in a loop with timestamps re-based to "now"; it never touches the virtual
  clock, pauses itself under backpressure or while a replay session runs, and is OFF by default so Studio's thread
  list stays clean.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from foghorn.events.config import EventsSettings
from foghorn.events.dataport import DataPort, DataUnavailable
from foghorn.events.pipeline import EventBus
from foghorn.events.replay import ReplayEngine
from foghorn.events.vclock import VirtualClock, parse_iso, to_iso
from foghorn.logging import get_logger

log = get_logger("events.scheduler")
Sleep = Callable[[float], Awaitable[None]]

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, st = part.partition("/")
            if not st.isdigit() or int(st) < 1:
                raise CronError(f"bad step in {text!r}")
            step = int(st)
        if part in {"*", ""}:
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            if not (x.isdigit() and y.isdigit()):
                raise CronError(f"bad range in {text!r}")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = int(part)
            b = hi if step > 1 else a
        else:
            raise CronError(f"bad field {text!r}")
        if a < lo or b > hi or a > b:
            raise CronError(f"value out of range in {text!r}")
        out.update(range(a, b + 1, step))
    return out


class CronExpr:
    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise CronError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
        self.expr = expr
        self.minutes, self.hours, self.doms, self.months, dows = (
            _field(p, *r) for p, r in zip(parts, _RANGES, strict=True)
        )
        self.dows = {d % 7 for d in dows}
        self.dom_star, self.dow_star = parts[2].startswith("*"), parts[4].startswith("*")

    def _day_ok(self, dt: datetime) -> bool:
        dom_ok = dt.day in self.doms
        dow_ok = ((dt.weekday() + 1) % 7) in self.dows  # python: Monday=0 ; cron: Sunday=0
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # standard cron: both restricted -> either may match

    def next_after(self, after: datetime) -> datetime:
        """The first fire time strictly after ``after`` (UTC, minute resolution)."""
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if dt.month not in self.months:
                dt = (dt.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_ok(dt):
                dt = dt.replace(hour=0, minute=0) + timedelta(days=1)
            elif dt.hour not in self.hours:
                dt = dt.replace(minute=0) + timedelta(hours=1)
            elif dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
            else:
                return dt
        raise CronError(f"cron {self.expr!r} never fires")


class Scheduler:
    """Emits ``system.cron.tick`` for each cron boundary the virtual clock crosses."""

    MAX_CATCHUP = 20

    def __init__(
        self, *, es: EventsSettings, bus: EventBus, vclock: VirtualClock, sleep: Sleep = asyncio.sleep
    ) -> None:
        self.es, self.bus, self.vclock = es, bus, vclock
        self._sleep = sleep
        self.jobs: dict[str, CronExpr] = {name: CronExpr(expr) for name, expr in es.cron_jobs.items()}
        self._last: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self.ticks_emitted = 0

    async def start(self) -> None:
        now = self.vclock.now()
        self._last = dict.fromkeys(self.jobs, now)
        self._task = asyncio.create_task(self._loop(), name="foghorn-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("scheduler_tick_failed", error=repr(exc))
            await self._sleep(self.es.scheduler_tick_s)

    async def tick(self) -> int:
        """Emit every due tick (at most ``MAX_CATCHUP`` per job per call). Returns how many were emitted."""
        now = self.vclock.now()
        emitted = 0
        for name, cron in self.jobs.items():
            last = self._last.get(name, now)
            if now < last:  # the clock was set backwards: re-anchor instead of replaying the future
                self._last[name] = now
                continue
            due: list[datetime] = []
            cursor = last
            while len(due) < 10_000:
                nxt = cron.next_after(cursor)
                if nxt > now:
                    break
                due.append(nxt)
                cursor = nxt
            if not due:
                continue
            skipped = max(0, len(due) - self.MAX_CATCHUP)
            for fire in due[-self.MAX_CATCHUP :]:
                iso = to_iso(fire)
                await self.bus.ingest(
                    {
                        "type": "system.cron.tick",
                        "source": "cron",
                        "severity": "info",
                        "occurred_at": iso,
                        "payload": {"job": name, "schedule": cron.expr, "fired_for": iso, "skipped": skipped},
                        "idempotency_key": f"cron:{name}:{iso}",
                    },
                    origin="internal",
                )
                emitted += 1
            self._last[name] = due[-1]
        self.ticks_emitted += emitted
        return emitted


class AmbientGenerator:
    """Background chatter: replays a quiet stream in a loop, re-based to the current virtual time."""

    def __init__(
        self,
        *,
        es: EventsSettings,
        bus: EventBus,
        vclock: VirtualClock,
        data: DataPort,
        replay: ReplayEngine,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.es, self.bus, self.vclock, self.data, self.replay = es, bus, vclock, data, replay
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self.enabled = False
        self.sent = 0
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def set_enabled(self, enabled: bool) -> bool:
        if enabled and not self.running:
            self.enabled = True
            self._task = asyncio.create_task(self._loop(), name="foghorn-ambient")
        elif not enabled:
            self.enabled = False
            if self._task:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
        return self.enabled

    async def stop(self) -> None:
        await self.set_enabled(False)

    async def _loop(self) -> None:
        try:
            events = await asyncio.to_thread(lambda: list(self.data.iter_stream(self.es.ambient_stream)))
        except (DataUnavailable, KeyError) as exc:
            self.last_error = repr(exc)
            log.warning("ambient_unavailable", error=self.last_error)
            self.enabled = False
            return
        timed = sorted(
            ((parse_iso(str(e["occurred_at"])), e) for e in events if e.get("occurred_at")), key=lambda x: x[0]
        )
        if not timed:
            self.enabled = False
            return
        lap = 0
        while True:
            t0 = timed[0][0]
            prev = t0
            for i, (t, raw) in enumerate(timed):
                await self._sleep(max(0.0, (t - prev).total_seconds() / self.es.ambient_speed))
                prev = t
                while self.replay.active or self.bus.queues.bp_active:
                    await self._sleep(0.25)
                    if self.bus.queues.bp_active:
                        await self.bus.queues.clear_event.wait()
                e: dict[str, Any] = dict(raw)
                e["id"] = None
                e["source"] = f"replay:{self.es.ambient_stream}"
                e["occurred_at"] = self.vclock.now_iso()
                e["idempotency_key"] = None
                attrs = dict(raw.get("attributes") or {})
                attrs["ambient"] = True
                e["attributes"] = attrs
                trace = dict(raw.get("trace") or {})
                trace["seq"] = lap * len(timed) + i + 1
                e["trace"] = trace
                await self.bus.ingest(e, origin="replay")
                self.sent += 1
            lap += 1
