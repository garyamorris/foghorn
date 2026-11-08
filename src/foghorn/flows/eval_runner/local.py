"""``LocalSdkClient``: the slice of the LangGraph SDK client that ``eval_runner`` uses, served by in-process compiled graphs.

Used by the tests (a tiny stub commander), the offline CLI (``python -m foghorn.evals run``) and anyone who wants to run an
evaluation without a server. It mirrors the SDK result shapes: ``runs.wait`` returns the output values plus
``__interrupt__: [{"id", "value"}]``; ``threads.get_state`` returns ``{"values", "next", "tasks": [{"interrupts"}]}``.
It also counts concurrent ``runs.wait`` calls so tests can PROVE the concurrency bound.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from langgraph.types import Command


class _Threads:
    def __init__(self, owner: LocalSdkClient) -> None:
        self._o = owner

    async def create(
        self,
        *,
        thread_id: str,
        if_exists: str = "raise",
        graph_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self._o.thread_metadata.setdefault(thread_id, dict(metadata or {}))
        return {"thread_id": thread_id}

    async def get_state(self, thread_id: str) -> dict[str, Any]:
        graph = self._o.graph_of.get(thread_id)
        if graph is None:  # never run: an empty thread
            return {"values": {}, "next": [], "tasks": []}
        snap = await graph.aget_state(self._o.config(thread_id))
        tasks = [
            {"id": t.id, "name": t.name, "interrupts": [{"id": i.id, "value": i.value} for i in t.interrupts]}
            for t in snap.tasks
        ]
        return {"values": dict(snap.values or {}), "next": list(snap.next), "tasks": tasks}


class _Runs:
    def __init__(self, owner: LocalSdkClient) -> None:
        self._o = owner

    async def wait(
        self,
        thread_id: str,
        graph_id: str,
        *,
        input: Any = None,
        command: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        o = self._o
        graph = o.graphs[graph_id]
        o.graph_of[thread_id] = graph
        o.calls.append(
            {"thread_id": thread_id, "graph_id": graph_id, "input": input, "command": command, "context": context}
        )
        o.inflight += 1
        o.max_inflight = max(o.max_inflight, o.inflight)
        try:
            if o.latency_s:
                await asyncio.sleep(o.latency_s)
            payload: Any = Command(resume=command["resume"]) if command is not None else input
            out = await graph.ainvoke(payload, o.config(thread_id), context=dict(context or {}), version="v2")
            o.run_counts[thread_id] = o.run_counts.get(thread_id, 0) + 1
            values = dict(out.value or {})
            if out.interrupts:
                values["__interrupt__"] = [{"id": i.id, "value": i.value} for i in out.interrupts]
            return values
        finally:
            o.inflight -= 1

    async def list(self, thread_id: str, *, limit: int = 10, **_: Any) -> list[dict[str, Any]]:
        n = self._o.run_counts.get(thread_id, 0)
        return [{"run_id": f"run-{thread_id[:8]}-{i}", "status": "success"} for i in range(n, 0, -1)][:limit]


class LocalSdkClient:
    """``LocalSdkClient({"incident_commander": compiled_graph})``. ``compiled_graph`` MUST carry a checkpointer."""

    kind = "stub"

    def __init__(self, graphs: Mapping[str, Any], *, latency_s: float = 0.0) -> None:
        self.graphs = dict(graphs)
        self.latency_s = latency_s
        self.graph_of: dict[str, Any] = {}
        self.thread_metadata: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.run_counts: dict[str, int] = {}
        self.inflight = 0
        self.max_inflight = 0
        self.threads = _Threads(self)
        self.runs = _Runs(self)

    @staticmethod
    def config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}
