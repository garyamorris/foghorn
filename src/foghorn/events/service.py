"""``EventsRuntime``: owns and wires every events component for the lifetime of the app.

Built once per process (``create_app`` lifespan), it:
* opens the SQLite state DB (off the loop), restores the virtual clock,
* installs the lifecycle sink (``foghorn.common.lifecycle.install_sink``): ``sink(env)`` only enqueues onto the loop
  with ``call_soon_threadsafe`` (non-blocking, thread-safe, safe from sync nodes and async nodes alike) and a consumer
  task persists / broadcasts / materialises each event through ``EventBus.ingest`` (deduplicated on ``idempotency_key``),
* starts the dispatcher, scheduler and tickers and a bootstrap task that waits for the Agent Server, then seeds.
  Seeding failures are logged and never crash the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

from foghorn import clock
from foghorn.common import lifecycle
from foghorn.contracts.meta import GRAPH_IDS, MODEL_PROFILES
from foghorn.events.approvals import ApprovalService
from foghorn.events.config import APPROVAL_POLICIES, EventsSettings, state_db_path
from foghorn.events.dataport import DataPort, RealDataPort
from foghorn.events.db import StateDB
from foghorn.events.dispatcher import Dispatcher
from foghorn.events.pipeline import EventBus
from foghorn.events.queues import PriorityQueues
from foghorn.events.replay import ReplayEngine
from foghorn.events.routing import DEFAULT_ROUTING_FILE, RoutingTable
from foghorn.events.scheduler import AmbientGenerator, Scheduler
from foghorn.events.sdk import ClientFactory, default_client_factory
from foghorn.events.seeding import seed_all
from foghorn.events.sse import SSEHub
from foghorn.events.vclock import VirtualClock, to_iso
from foghorn.logging import get_logger
from foghorn.settings import Settings

log = get_logger("events.service")
Sleep = Callable[[float], Awaitable[None]]
MAX_SINK_BACKLOG = 50_000
#: the operator-switchable defaults (``PUT /api/admin/settings``); they apply to runs created AFTER the change
RUNTIME_SETTINGS = ("chaos_mode", "default_model_profile", "default_approval_policy", "default_token_delay_ms")


class EventsRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        es: EventsSettings,
        client_factory: ClientFactory = default_client_factory,
        data: DataPort | None = None,
        sleep: Sleep = asyncio.sleep,
        wall: Callable[[], datetime] | None = None,
        db_path: Path | None = None,
        routing: RoutingTable | None = None,
    ) -> None:
        self.settings, self.es = settings, es
        self.client_factory = client_factory
        self.data: DataPort = data or RealDataPort()
        self._sleep = sleep
        self._wall = wall or clock.now
        self.db = StateDB(db_path or state_db_path(settings.data_dir))
        self.vclock = VirtualClock(self._wall)
        self.hub = SSEHub(es.sse_buffer)
        self.queues = PriorityQueues(
            es.queue_caps,
            weights=es.queue_weights,
            aging_s=es.aging_s,
            high_water=es.high_water,
            low_water=es.low_water,
        )
        self.routing = routing or RoutingTable.load(es.routing_file or DEFAULT_ROUTING_FILE)
        self.bus = EventBus(
            db=self.db,
            es=es,
            vclock=self.vclock,
            queues=self.queues,
            hub=self.hub,
            routing=self.routing,
            wall=self._wall,
        )
        self.dispatcher = Dispatcher(
            db=self.db,
            es=es,
            bus=self.bus,
            queues=self.queues,
            hub=self.hub,
            client_factory=client_factory,
            sleep=sleep,
            wall=self._wall,
        )
        self.approvals = ApprovalService(
            db=self.db, es=es, bus=self.bus, dispatcher=self.dispatcher, client_factory=client_factory, wall=self._wall
        )
        self.replay = ReplayEngine(
            db=self.db, es=es, bus=self.bus, vclock=self.vclock, data=self.data, sleep=sleep, wall=self._wall
        )
        self.scheduler = Scheduler(es=es, bus=self.bus, vclock=self.vclock, sleep=sleep)
        self.ambient = AmbientGenerator(
            es=es, bus=self.bus, vclock=self.vclock, data=self.data, replay=self.replay, sleep=sleep
        )
        self.started_mono = time.monotonic()
        self.started = False
        self.agent_state = "waiting"  # waiting | ready | unavailable
        self.seed_state = "pending"  # pending | done | failed | skipped
        self.seed_report: dict[str, Any] = {}
        self.sink_dropped = 0
        self._backlog = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_q: asyncio.Queue[dict[str, Any]] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._rate_window_start = time.monotonic()

    @property
    def client(self) -> Any:
        """The shared SDK client (in-process in production)."""
        return self.approvals.client

    # --- lifecycle -----------------------------------------------------------------------------------------------
    async def start(self, *, background: bool | None = None) -> None:
        if self.started:
            return
        background = self.es.start_background if background is None else background
        await asyncio.to_thread(self.db.open)
        saved = await self.db.run(self.db.kv_get, "clock", None)
        if isinstance(saved, dict):
            with contextlib.suppress(Exception):
                if saved.get(
                    "replay_active"
                ):  # replay sessions do not survive a restart: do not keep racing at replay speed
                    saved = {**saved, "speed": 1.0}
                self.vclock.restore(saved)
        stored = await self.db.run(self.db.kv_get, "settings", None)
        if isinstance(stored, dict):  # operator overrides survive a restart (they were validated when written)
            for key in RUNTIME_SETTINGS:
                if key in stored:
                    setattr(self.es, key, stored[key])
        self._loop = asyncio.get_running_loop()
        self._lifecycle_q = asyncio.Queue()
        lifecycle.install_sink(self.sink)
        self._spawn(self._lifecycle_consumer(), "foghorn-lifecycle")
        self.started = True
        if background:
            self._spawn(self._bootstrap(), "foghorn-bootstrap")
            self._spawn(self._queue_ticker(), "foghorn-queue-ticker")
            self._spawn(self._clock_ticker(), "foghorn-clock-ticker")
            if self.es.ambient_enabled:
                await self.ambient.set_enabled(True)
        log.info("events_runtime_started", db=str(self.db.path), background=background)

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    async def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        lifecycle.install_sink(None)
        await self.ambient.stop()
        with contextlib.suppress(Exception):
            await self.replay.stop()
        await self.scheduler.stop()
        # give already-enqueued lifecycle events a moment to persist
        if self._lifecycle_q is not None:
            deadline = time.monotonic() + 2.0
            while not self._lifecycle_q.empty() and time.monotonic() < deadline:  # noqa: ASYNC110 - bounded drain
                await asyncio.sleep(0.01)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        await self.dispatcher.stop()
        self.hub.close_all()
        with contextlib.suppress(Exception):
            await self.db.run(self.db.kv_set, "clock", self._clock_record())
        await asyncio.to_thread(self.db.close)

    # --- lifecycle sink --------------------------------------------------------------------------------------------
    def sink(self, env: dict[str, Any]) -> None:
        """``foghorn.common.lifecycle`` sink: enqueue only. Never blocks, never raises, callable from any thread."""
        loop, q = self._loop, self._lifecycle_q
        if loop is None or q is None or loop.is_closed():
            return
        if self._backlog >= MAX_SINK_BACKLOG:
            self.sink_dropped += 1
            return
        self._backlog += 1
        try:
            loop.call_soon_threadsafe(q.put_nowait, env)
        except RuntimeError:  # loop closed between the check and the call
            self._backlog -= 1

    async def _lifecycle_consumer(self) -> None:
        assert self._lifecycle_q is not None
        while True:
            env = await self._lifecycle_q.get()
            self._backlog = max(0, self._backlog - 1)
            try:
                await self.bus.ingest(env, origin="internal")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("lifecycle_ingest_failed", type=env.get("type"), error=repr(exc))

    # --- bootstrap (waits for the Agent Server, then seeds) -------------------------------------------------------------
    async def wait_for_agent(self) -> bool:
        deadline = time.monotonic() + self.es.ready_timeout_s
        client = self.client_factory()
        while time.monotonic() < deadline:
            try:
                await client.assistants.search(limit=1)
                self.agent_state = "ready"
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._sleep(self.es.ready_poll_s)
        self.agent_state = "unavailable"
        return False

    async def _bootstrap(self) -> None:
        try:
            ready = await self.wait_for_agent()
            await self.dispatcher.start()
            await self.scheduler.start()
            if not ready:
                self.seed_state = "failed"
                log.warning("agent_server_not_ready", timeout_s=self.es.ready_timeout_s)
                return
            if not self.es.seed_on_start:
                self.seed_state = "skipped"
            else:
                try:
                    self.seed_report = await seed_all(self.client_factory(), self.data, self.es)
                    self.seed_state = "done"
                except Exception as exc:  # seeding must never take the server down
                    self.seed_state = "failed"
                    log.error("seeding_failed", error=repr(exc))
            try:
                await self.approvals.scan(startup=True)
            except Exception as exc:
                log.warning("approvals_reconcile_failed", error=repr(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.seed_state = "failed"
            log.error("bootstrap_failed", error=repr(exc))

    # --- tickers ---------------------------------------------------------------------------------------------------
    async def _queue_ticker(self) -> None:
        while True:
            await self._sleep(self.es.queue_tick_s)
            try:
                self.hub.publish("queue", await self.queue_stats())
            except Exception as exc:
                log.warning("queue_tick_failed", error=repr(exc))

    async def _clock_ticker(self) -> None:
        n = 0
        while True:
            await self._sleep(self.es.clock_tick_s)
            self.hub.publish("clock", self.clock_snapshot())
            n += 1
            if n % 5 == 0:
                with contextlib.suppress(Exception):
                    await self.db.run(self.db.kv_set, "clock", self._clock_record())

    # --- views -----------------------------------------------------------------------------------------------------
    def _clock_record(self) -> dict[str, Any]:
        return {**self.vclock.state(), "replay_active": self.replay.active}

    def runtime_settings(self) -> dict[str, Any]:
        return {k: getattr(self.es, k) for k in RUNTIME_SETTINGS}

    async def update_runtime_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply (already validated) operator changes, persist them, and return the new values."""
        for key, value in changes.items():
            if key not in RUNTIME_SETTINGS:
                raise KeyError(key)
            setattr(self.es, key, value)
        await self.db.run(self.db.kv_set, "settings", self.runtime_settings())
        return self.runtime_settings()

    def clock_snapshot(self) -> dict[str, Any]:
        return self.vclock.snapshot(replay=self.replay.status() if self.replay.session else None)

    async def queue_stats(self) -> dict[str, Any]:
        counts = await self.db.run(self.db.dispatch_count_by_status)
        dlq = await self.db.run(self.db.count_open_dead_letters)
        snap = self.queues.snapshot()
        snap.update(self.dispatcher.stats())
        snap.update(
            {
                "interrupted": counts.get("interrupted", 0),
                "dlq": dlq,
                "shed": self.queues.shed_total,
                "rate_per_s": self.bus.rate_per_s(),
                "events_total": self.bus.events_total,
                "events_rejected": self.bus.rejected_total,
                "requests_429": self.bus.backpressure_429_total,
                "promoted": self.queues.promoted_total,
            }
        )
        return snap

    async def summary(self) -> dict[str, Any]:
        q = await self.queue_stats()
        open_inc = await self.db.run(self.db.count_open_incidents)
        pending = await self.db.run(self.db.count_pending_approvals)
        evals = await self.db.run(self.db.list_eval_runs, 1)
        last = evals[0] if evals else None
        last_eval = (
            {
                "eval_run_id": last["eval_run_id"],
                "status": last["status"],
                "mean_score": (last.get("summary") or {}).get("mean_score"),
            }
            if last
            else None
        )
        return {
            "open_incidents": open_inc,
            "pending_approvals": pending,
            "events_per_min": float(self.bus.events_in_window(60.0)),
            "events_total": await self.db.run(self.db.count_events),
            "queue": {"depth": q["depth"], "in_flight": q["in_flight"]},
            "dlq_open": q["dlq"],
            "shed_total": q["shed"],
            "last_eval": last_eval,
            "latest_seq": await self.db.run(self.db.latest_seq),
        }

    def health(self) -> dict[str, Any]:
        components = {
            "state_db": "ok" if self.db.is_open else "closed",
            "dispatcher": "running"
            if self.dispatcher._loop_task and not self.dispatcher._loop_task.done()
            else "stopped",
            "scheduler": "running" if self.scheduler.running else "stopped",
            "replay": self.replay.status()["state"],
            "agent_server": self.agent_state,
            "data_api": "ok" if self.data.available() else "unavailable",
            "seeding": self.seed_state,
        }
        degraded = components["state_db"] != "ok" or self.agent_state == "unavailable" or self.seed_state == "failed"
        return {
            "status": "degraded" if degraded else "ok",
            "ready": self.started and self.agent_state == "ready",
            "components": components,
            "uptime_s": round(time.monotonic() - self.started_mono, 1),
        }

    def config_snapshot(self) -> dict[str, Any]:
        s, es = self.settings, self.es
        base = s.public_base_url.rstrip("/")
        from urllib.parse import quote

        return {
            "environment": s.environment,
            "provider": s.resolved_provider(),
            "public_base_url": s.public_base_url,
            "studio": {
                "url": s.studio_url,
                "base_link": f"{s.studio_url}?baseUrl={quote(base, safe='')}",
                "thread_url_template": es.studio_thread_url,
                "thread_url_verified": False,
            },
            "auth_required": s.dashboard_api_token is not None,
            "webhook_signature_required": es.webhook_require_signature,
            "model_profiles": list(MODEL_PROFILES),
            "approval_policies": list(APPROVAL_POLICIES),
            "graph_ids": list(GRAPH_IDS),
            "defaults": {
                "model_profile": es.default_model_profile,
                "approval_policy": es.default_approval_policy,
                "replay_speed": es.replay_speed,
                "token_delay_ms": es.default_token_delay_ms,
                "chaos_mode": es.chaos_mode,
            },
            "events": {
                "queue_caps": es.queue_caps,
                "high_water": es.high_water,
                "low_water": es.low_water,
                "max_runs": es.max_runs,
                "per_graph_max_runs": es.per_graph_max_runs,
                "dedupe_window_s": es.dedupe_window_s,
                "max_attempts": es.max_attempts,
                "backoff_s": es.backoff_s,
                "ambient_enabled": self.ambient.running,
            },
            "limits": {
                "webhook_max_body_bytes": s.webhook_max_body_bytes,
                "webhook_rate_per_min": s.webhook_rate_per_min,
            },
        }

    def studio_links(self, thread_id: str | None = None) -> dict[str, Any]:
        cfg = self.config_snapshot()["studio"]
        tpl = self.es.studio_thread_url
        thread_url = None
        if tpl and thread_id:
            from urllib.parse import quote

            thread_url = tpl.format(
                studio=self.settings.studio_url.rstrip("/"),
                thread_id=thread_id,
                base=quote(self.settings.public_base_url.rstrip("/"), safe=""),
            )
        return {"base_url": cfg["base_link"], "thread_id": thread_id, "thread_url": thread_url}

    def now_iso(self) -> str:
        return to_iso(self._wall(), ms=True)

    def wall_now(self) -> datetime:
        return self._wall()
