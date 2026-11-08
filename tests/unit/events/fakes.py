"""Test doubles shared by tests/unit/events and tests/unit/server: a fake LangGraph SDK client, a fake data layer, a fake
wall clock whose ``sleep`` advances time instantly, and a runtime factory. Nothing here touches the network or the repo."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from foghorn.contracts.meta import GRAPH_IDS
from foghorn.events.config import EventsSettings
from foghorn.events.dataport import DataUnavailable, UnknownScenario
from foghorn.events.seeding import SYSTEM_ASSISTANT_NS
from foghorn.events.service import EventsRuntime
from foghorn.settings import Settings

BASE = datetime(2026, 11, 5, 10, 0, 0, tzinfo=UTC)


class FakeWall:
    """Deterministic wall clock. ``await sleep(d)`` advances it by ``d`` and yields to the loop."""

    def __init__(self, start: datetime = BASE) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.t += timedelta(seconds=max(delay, 0))
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


# ------------------------------------------------------------------------------------------------ fake SDK client
class SdkError(Exception):
    pass


class FakeThreads:
    def __init__(self, client: FakeClient) -> None:
        self.c = client
        self.items: dict[str, dict[str, Any]] = {}

    async def create(
        self,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        if_exists: str | None = None,
        graph_id: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.c.calls.append(("threads.create", {"thread_id": thread_id, "metadata": metadata, "graph_id": graph_id}))
        if self.c.fail_threads:
            raise SdkError("threads.create unavailable")
        assert thread_id
        if thread_id not in self.items:
            self.items[thread_id] = {
                "thread_id": thread_id,
                "status": "idle",
                "metadata": dict(metadata or {}),
                "interrupts": {},
                "values": {},
                "updated_at": "2026-11-05T10:00:00Z",
            }
        return copy.deepcopy(self.items[thread_id])

    async def update(self, thread_id: str, *, metadata: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.c.calls.append(("threads.update", {"thread_id": thread_id, "metadata": metadata}))
        self.items[thread_id]["metadata"].update(metadata)
        return copy.deepcopy(self.items[thread_id])

    async def get(self, thread_id: str, **_: Any) -> dict[str, Any]:
        if thread_id not in self.items:
            raise SdkError(f"thread {thread_id} not found")
        return copy.deepcopy(self.items[thread_id])

    async def search(self, *, status: str | None = None, limit: int = 10, **_: Any) -> list[dict[str, Any]]:
        if self.c.fail_search:
            raise SdkError("agent server unavailable")
        out = [copy.deepcopy(t) for t in self.items.values() if status is None or t["status"] == status]
        return out[:limit]

    async def get_state(self, thread_id: str, **_: Any) -> dict[str, Any]:
        t = self.items[thread_id]
        return {
            "values": copy.deepcopy(t.get("values", {})),
            "checkpoint": {"checkpoint_id": t.get("checkpoint_id", "cp-1")},
            "tasks": [],
        }


class FakeRuns:
    def __init__(self, client: FakeClient) -> None:
        self.c = client
        self.items: dict[str, dict[str, Any]] = {}
        self.n = 0

    async def create(self, thread_id: str, assistant_id: str, **kw: Any) -> dict[str, Any]:
        self.c.calls.append(("runs.create", {"thread_id": thread_id, "assistant_id": assistant_id, **kw}))
        if self.c.fail_create > 0:
            self.c.fail_create -= 1
            raise SdkError("runs.create failed")
        self.n += 1
        run_id = f"run-{self.n:03d}"
        outcome = self.c.script(thread_id, assistant_id, kw, self.n)
        status = outcome.get("status", "success")
        thread = self.c.threads.items.setdefault(
            thread_id, {"thread_id": thread_id, "status": "idle", "metadata": {}, "interrupts": {}, "values": {}}
        )
        cmd = kw.get("command")
        if cmd and isinstance(cmd.get("resume"), dict):
            for task, items in list(thread["interrupts"].items()):
                thread["interrupts"][task] = [i for i in items if i["id"] not in cmd["resume"]]
                if not thread["interrupts"][task]:
                    del thread["interrupts"][task]
        if status == "interrupted_thread":
            thread["status"] = "interrupted"
            for iv in outcome["interrupts"]:
                thread["interrupts"].setdefault("task-1", []).append(iv)
            status = "success"
        else:
            thread["status"] = "interrupted" if thread["interrupts"] else "idle"
        if outcome.get("values"):
            thread["values"] = outcome["values"]
        self.items[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "status": status,
            "error": outcome.get("error"),
            "polls": outcome.get("polls", 0),
        }
        return {"run_id": run_id, "thread_id": thread_id, "status": "pending"}

    async def get(self, thread_id: str, run_id: str) -> dict[str, Any]:
        r = self.items[run_id]
        if r["polls"] > 0:
            r["polls"] -= 1
            return {**r, "status": "running"}
        return dict(r)

    async def list(self, thread_id: str, *, limit: int = 10, **_: Any) -> list[dict[str, Any]]:
        if self.c.fail_runs_list:
            raise SdkError("runs.list failed")
        rows = [
            dict(r, created_at=f"2026-11-05T10:00:{i:02d}Z")
            for i, r in enumerate(self.items.values())
            if r["thread_id"] == thread_id
        ]
        return list(reversed(rows))[:limit]

    async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
        self.c.calls.append(("runs.cancel", {"run_id": run_id}))


class FakeAssistants:
    def __init__(self, client: FakeClient) -> None:
        self.c = client
        self.items: dict[str, dict[str, Any]] = {}
        # like the real server: every graph has a deterministic SYSTEM assistant
        for gid in GRAPH_IDS:
            aid = str(uuid.uuid5(SYSTEM_ASSISTANT_NS, gid))
            self.items[aid] = {
                "assistant_id": aid,
                "graph_id": gid,
                "name": gid,
                "context": {},
                "description": None,
                "metadata": {"created_by": "system"},
                "version": 1,
            }

    async def search(self, *, limit: int = 10, graph_id: str | None = None, **_: Any) -> list[dict[str, Any]]:
        if self.c.fail_search:
            raise SdkError("not ready")
        rows = [copy.deepcopy(a) for a in self.items.values() if graph_id is None or a["graph_id"] == graph_id]
        return rows[:limit]

    async def create(
        self, graph_id: str, *, assistant_id: str, name: str, context: dict[str, Any], **kw: Any
    ) -> dict[str, Any]:
        if self.c.fail_assistants:
            raise SdkError("assistants.create failed")
        self.items.setdefault(
            assistant_id,
            {
                "assistant_id": assistant_id,
                "graph_id": graph_id,
                "name": name,
                "context": context,
                "description": kw.get("description"),
                "metadata": kw.get("metadata") or {},
                "version": 1,
            },
        )
        return self.items[assistant_id]


class FakeStore:
    def __init__(self, client: FakeClient) -> None:
        self.c = client
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    async def put_item(self, namespace: list[str], key: str, value: dict[str, Any], **_: Any) -> None:
        if self.c.fail_store:
            raise SdkError("store down")
        self.items[(tuple(namespace), key)] = {"namespace": list(namespace), "key": key, "value": value}

    async def get_item(self, namespace: list[str], key: str, **_: Any) -> dict[str, Any]:
        it = self.items.get((tuple(namespace), key))
        if it is None:
            raise SdkError("404")
        return it

    async def search_items(
        self, namespace_prefix: list[str], /, query: str | None = None, limit: int = 10, **_: Any
    ) -> dict[str, Any]:
        items = [
            dict(v, score=0.5)
            for (ns, _k), v in self.items.items()
            if list(ns[: len(namespace_prefix)]) == list(namespace_prefix)
        ]
        return {"items": items[:limit]}

    async def list_namespaces(self, prefix: list[str] | None = None, **_: Any) -> dict[str, Any]:
        return {"namespaces": [list(ns) for (ns, _k) in self.items]}


class FakeCrons:
    def __init__(self, client: FakeClient) -> None:
        self.c = client
        self.items: list[dict[str, Any]] = []

    async def search(
        self, *, assistant_id: str | None = None, metadata: dict[str, Any] | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        return [c for c in self.items if c["assistant_id"] == assistant_id]

    async def create(self, assistant_id: str, **kw: Any) -> dict[str, Any]:
        cron = {"cron_id": f"cron-{len(self.items) + 1}", "assistant_id": assistant_id, **kw}
        self.items.append(cron)
        return cron


class FakeClient:
    """Stands in for ``langgraph_sdk.get_client(url=None)``. ``script(thread_id, assistant_id, kwargs, n) -> outcome``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_create = 0
        self.fail_threads = False
        self.fail_search = False
        self.fail_assistants = False
        self.fail_store = False
        self.fail_runs_list = False
        self.script: Callable[[str, str, dict[str, Any], int], dict[str, Any]] = lambda *_a: {"status": "success"}
        self.threads = FakeThreads(self)
        self.runs = FakeRuns(self)
        self.assistants = FakeAssistants(self)
        self.store = FakeStore(self)
        self.crons = FakeCrons(self)

    def of(self, name: str) -> list[dict[str, Any]]:
        return [a for n, a in self.calls if n == name]

    def interrupt_thread(
        self,
        thread_id: str,
        *interrupts: dict[str, Any],
        graph: str = "incident_commander",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        md = {"foghorn_graph": graph, "graph_id": graph, **(metadata or {})}
        t = self.threads.items.setdefault(thread_id, {"thread_id": thread_id, "metadata": md, "values": {}})
        t.update(status="interrupted", metadata={**t.get("metadata", {}), **md}, updated_at="2026-11-05T10:05:00Z")
        t["interrupts"] = {"task-1": [dict(i) for i in interrupts]}


def interrupt(iid: str, kind: str = "approval", approval_id: str | None = None, **extra: Any) -> dict[str, Any]:
    value = {
        "v": 1,
        "kind": kind,
        "approval_id": approval_id or f"apr_{iid}",
        "title": f"Approve {iid}",
        "summary": "s",
        "risk": "medium",
        "reversible": True,
        "context": {"incident_id": extra.pop("incident_id", None)},
        "options": ["approve", "reject"],
        **extra,
    }
    return {"id": iid, "value": value}


# ---------------------------------------------------------------------------------------------------- fake data
class FakeData:
    """A tiny implementation of the DataPort protocol (the real data layer is built concurrently)."""

    def __init__(self) -> None:
        self.ok = True
        self.scenarios: dict[str, dict[str, Any]] = {
            "S01": {
                "scenario_id": "S01",
                "title": "Bad deploy",
                "tier": 1,
                "category": "bad_deploy",
                "features": ["approval"],
                "severity": "SEV2",
            },
            "S02": {
                "scenario_id": "S02",
                "title": "Flag ramp",
                "tier": 2,
                "category": "flag_ramp",
                "features": ["question", "approval", "multiple interrupts"],
            },
        }
        self.truth = {"S01": {"root_cause": "reco-4412"}}
        self.events: dict[str, list[dict[str, Any]]] = {"S01": scenario_events("S01", 4)}
        self.streams: dict[str, list[dict[str, Any]]] = {
            "stream_quiet_hour": scenario_events("quiet", 3, stream="stream_quiet_hour"),
            "stream_storm": [],
        }
        self.memories: list[dict[str, Any]] = [
            {"namespace": ["runbooks"], "key": "rb-1", "value": {"text": "restart pods"}},
            {"namespace": ["prefs", "team"], "key": "p-1", "value": {"text": "prefer rollback"}},
        ]
        self.generated: list[tuple[Any, ...]] = []

    def _need(self) -> None:
        if not self.ok:
            raise DataUnavailable("foghorn.data.api is not importable (fake)")

    def available(self) -> bool:
        return self.ok

    def list_scenarios(self, tier: int | None = None, *, include_generated: bool = False) -> list[dict[str, Any]]:
        self._need()
        return [
            s
            for s in self.scenarios.values()
            if (tier is None or s["tier"] == tier) and (include_generated or not s["scenario_id"].startswith("X-"))
        ]

    def get_scenario(self, scenario_id: str, *, reveal_truth: bool = False) -> dict[str, Any]:
        self._need()
        if scenario_id not in self.scenarios:
            raise UnknownScenario(scenario_id)
        out = dict(self.scenarios[scenario_id])
        if reveal_truth:
            out["truth"] = self.truth.get(scenario_id, {})
        return out

    def default_as_of(self, scenario_id: str) -> str:
        self._need()
        return "2026-11-05T10:49:00Z"

    def scenario_events(self, scenario_id: str) -> list[dict[str, Any]]:
        self._need()
        if scenario_id not in self.events:
            raise UnknownScenario(scenario_id)
        return copy.deepcopy(self.events[scenario_id])

    def list_streams(self) -> list[dict[str, Any]]:
        self._need()
        return [{"name": n, "events": len(e)} for n, e in self.streams.items()]

    def iter_stream(self, name: str) -> Iterator[dict[str, Any]]:
        self._need()
        if name not in self.streams:
            raise UnknownScenario(name)
        return iter(copy.deepcopy(self.streams[name]))

    def service_catalog(self) -> list[dict[str, Any]]:
        self._need()
        return [{"id": "checkout-service"}]

    def datasets_index(self) -> list[dict[str, Any]]:
        self._need()
        return [{"name": "scenarios", "count": 2}]

    def read_dataset(
        self, name: str, offset: int = 0, limit: int = 50, *, reveal_truth: bool = False, scenario_id: str | None = None
    ) -> dict[str, Any]:
        self._need()
        if name != "scenarios":
            raise UnknownScenario(name)
        return {
            "name": name,
            "total": 2,
            "offset": offset,
            "truth_hidden": not reveal_truth,
            "scenario_id": scenario_id,
            "items": list(self.scenarios.values())[offset : offset + limit],
        }

    def build_fault_scenario(
        self, category: str, component: str, magnitude: float, red_herrings: int, seed: int
    ) -> dict[str, Any]:
        self._need()
        sid = f"X-{len(self.generated) + 1:04d}"
        self.generated.append((category, component, magnitude, red_herrings, seed))
        self.scenarios[sid] = {
            "scenario_id": sid,
            "title": f"{category} on {component}",
            "tier": 3,
            "category": category,
        }
        self.events[sid] = scenario_events(sid, 2)
        return dict(self.scenarios[sid])

    def fault_categories(self) -> list[str]:
        self._need()
        return ["bad_deploy", "flag_ramp"]

    def commander_input(self, scenario_id: str, incident: dict[str, Any]) -> dict[str, Any]:
        return {"scenario_id": scenario_id, "incident": incident, "as_of": "2026-11-05T10:49:00Z"}

    def seed_memories(self) -> list[dict[str, Any]]:
        self._need()
        return copy.deepcopy(self.memories)


def scenario_events(
    prefix: str, n: int, *, stream: str | None = None, step_s: int = 30, start: datetime = BASE + timedelta(minutes=5)
) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        t = start + timedelta(seconds=step_s * i)
        out.append(
            {
                "schema_version": "foghorn.event/1",
                "id": f"evt_{prefix}_{i + 1:05d}",
                "type": "alert.fired" if i % 2 == 0 else "deploy.finished",
                "source": f"replay:{stream or prefix}",
                "severity": "high" if i % 2 == 0 else "info",
                "occurred_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "subject": {"component": f"svc-{i % 2}", "environment": "prod"},
                "payload": {"rule": f"rule_{i}"},
                "dedupe_key": None,
                "idempotency_key": None,
                "correlation_id": None,
                "causation_id": None,
                "trace": {"stream": stream, "seq": i + 1, "scenario_id": prefix if not stream else None},
                "attributes": {},
            }
        )
    return out


def make_settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(data_dir=tmp_path / "var", environment="test", **kw)


def make_runtime(
    tmp_path: Path,
    *,
    client: FakeClient | None = None,
    data: FakeData | None = None,
    wall: FakeWall | None = None,
    settings: Settings | None = None,
    **es: Any,
) -> tuple[EventsRuntime, FakeClient, FakeData, FakeWall]:
    client = client or FakeClient()
    data = data or FakeData()
    wall = wall or FakeWall()
    defaults: dict[str, Any] = {
        "backoff_s": [0.0, 0.0, 0.0],
        "run_poll_interval_s": 0.01,
        "start_background": False,
        "ready_poll_s": 0.0,
        "ready_timeout_s": 0.05,
    }
    rt = EventsRuntime(
        settings=settings or make_settings(tmp_path),
        es=EventsSettings(**{**defaults, **es}),
        client_factory=lambda: client,
        data=data,  # type: ignore[arg-type]
        sleep=wall.sleep,
        wall=wall.now,
    )
    return rt, client, data, wall
