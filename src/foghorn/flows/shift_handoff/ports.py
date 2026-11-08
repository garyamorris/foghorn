"""Where the handoff reads the world from. An injectable seam: production talks to the in-process SDK client (threads,
interrupts) and the data layer (alert history); tests install a fake with ``set_source_factory``.

Every blocking data-layer read is wrapped in ``asyncio.to_thread`` (the nodes are ``async def``; gotcha 4).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from foghorn.contracts.meta import M_KIND
from foghorn.data.util import to_epoch, to_iso
from foghorn.logging import get_logger

log = get_logger("handoff.ports")

THREAD_FIELDS = ["thread_id", "status", "metadata", "values", "interrupts", "created_at", "updated_at"]


class ShiftSource(Protocol):
    async def incident_threads(self) -> list[dict[str, Any]]:
        """Commander threads (flat metadata ``foghorn_kind == "incident"``)."""
        ...

    async def interrupted_threads(self) -> list[dict[str, Any]]:
        """Every thread whose status is ``interrupted`` (pending approvals / edits / questions)."""
        ...

    async def alert_history(self, start: str, end: str) -> list[dict[str, Any]]:
        """Alert events (data layer) with ``start < t <= end``: ``{id, t, component, severity, rule, scenario_id}``."""
        ...

    async def scenario_open_time(self, scenario_id: str) -> str | None:
        """Virtual time a scenario's incident is first noticed, when the thread does not say."""
        ...

    async def default_shift_end(self) -> str:
        """Shift end to use when the caller gave none (the native server cron sends only ``window_hours``)."""
        ...


def _alert_rows(start_s: int, end_s: int) -> list[dict[str, Any]]:
    from foghorn.data import api

    rows: list[dict[str, Any]] = []
    for sc in api.list_scenarios():
        sid = sc["scenario_id"]
        for e in api.scenario_events(sid):
            if e["type"] != "alert.fired":
                continue
            t = to_epoch(e["occurred_at"])
            if start_s < t <= end_s:
                rows.append(_alert_row(e, sid))
    for st in api.list_streams():  # streams only matter if their span overlaps the window
        if to_epoch(st["end"]) < start_s or to_epoch(st["start"]) > end_s:
            continue
        for e in api.iter_stream(st["name"]):
            if e["type"] == "alert.fired" and start_s < to_epoch(e["occurred_at"]) <= end_s:
                rows.append(_alert_row(e, st["name"]))
    rows.sort(key=lambda r: (to_epoch(r["t"]), r["id"]))
    return rows


def _alert_row(e: dict[str, Any], origin: str) -> dict[str, Any]:
    p = e.get("payload") or {}
    return {
        "id": str(e.get("id")),
        "t": e["occurred_at"],
        "component": (e.get("subject") or {}).get("component") or "unknown",
        "severity": e.get("severity") or "info",
        "rule": str(p.get("rule") or "alert"),
        "origin": origin,
    }


def _scenario_open(scenario_id: str) -> str | None:
    from foghorn.data import api

    try:
        return str(api.default_as_of(scenario_id))
    except Exception:
        return None


def _latest_scenario_end() -> str:
    """Latest scenario trigger time rounded UP to the next full hour: the demo shift always has alert history behind it."""
    from foghorn.data import api

    latest = max(to_epoch(api.default_as_of(s["scenario_id"])) for s in api.list_scenarios())
    return to_iso((latest // 3600 + 1) * 3600)


class SdkShiftSource:
    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._factory = client_factory

    def _client(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from langgraph_sdk import get_client

        return get_client(url=None)

    async def incident_threads(self) -> list[dict[str, Any]]:
        rows = await self._client().threads.search(
            metadata={M_KIND: "incident"}, limit=200, sort_by="updated_at", sort_order="desc", select=THREAD_FIELDS
        )
        return [dict(r) for r in rows]

    async def interrupted_threads(self) -> list[dict[str, Any]]:
        rows = await self._client().threads.search(status="interrupted", limit=200, select=THREAD_FIELDS)
        return [dict(r) for r in rows]

    async def alert_history(self, start: str, end: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_alert_rows, to_epoch(start), to_epoch(end))

    async def scenario_open_time(self, scenario_id: str) -> str | None:
        return await asyncio.to_thread(_scenario_open, scenario_id)

    async def default_shift_end(self) -> str:
        return await asyncio.to_thread(_latest_scenario_end)


SourceFactory = Callable[[], ShiftSource]
_factory: SourceFactory = SdkShiftSource


def set_source_factory(factory: SourceFactory | None) -> None:
    """Tests inject a fake; ``None`` restores the SDK + data-layer default."""
    global _factory
    _factory = factory or SdkShiftSource


def get_source() -> ShiftSource:
    return _factory()
