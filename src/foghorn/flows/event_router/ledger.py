"""The event ledger: the router's memory in the LangGraph ``Store`` (namespace ``("ledger", "events")``).

One namespace, four item kinds (all values are plain JSON and carry ``kind``; ``index=False`` so no embeddings):

``seen``      key ``dk:<dedupe_key>``     last time a dedupe key was seen (sliding window), with the latest alert event
``event``     key ``evt:<event_id>``      the event was fully processed (idempotent re-delivery marker + decision)
``incident``  key ``inc:<incident_id>``   an incident the router opened (components, severity, join window)
``maintenance`` key ``mnt:<component>:<id>``  a maintenance window announced through the change feed
``storm``     key ``storm:<key>``         a declared alert storm (open until ``until``)

``store=None`` (a graph run without a store) degrades to "no memory": every read is empty, every write a no-op.
Open-incident correlation is race-guarded inside one process by :func:`ledger_lock`.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from langgraph.store.base import BaseStore

from foghorn.flows.event_router import rules
from foghorn.memory.api import NS_LEDGER

NS: tuple[str, ...] = (NS_LEDGER, "events")
_SEARCH_LIMIT = 2000
_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def ledger_lock() -> asyncio.Lock:
    """One lock per event loop: serialises check-then-open sections of concurrent runs in this process."""
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    """The part of an alert envelope worth keeping in the ledger (storm correlation re-reads it)."""
    keep = ("schema_version", "id", "type", "source", "severity", "occurred_at", "subject", "payload", "dedupe_key")
    out = {k: event.get(k) for k in keep if k in event}
    out["trace"] = event.get("trace") or {}
    return out


class Ledger:
    def __init__(self, store: BaseStore | None) -> None:
        self.store = store

    # -- generic -------------------------------------------------------------------------------------------------
    async def _list(self, kind: str) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        items = await self.store.asearch(NS, filter={"kind": kind}, limit=_SEARCH_LIMIT)
        return [dict(i.value) for i in items]

    async def _get(self, key: str) -> dict[str, Any] | None:
        if self.store is None:
            return None
        item = await self.store.aget(NS, key)
        return dict(item.value) if item is not None else None

    async def _put(self, key: str, value: dict[str, Any]) -> None:
        if self.store is not None:
            await self.store.aput(NS, key, value, index=False)

    # -- processed events ----------------------------------------------------------------------------------------
    async def processed(self, event_id: str) -> dict[str, Any] | None:
        return await self._get(f"evt:{event_id}")

    async def record_processed(self, event_id: str, summary: dict[str, Any]) -> None:
        await self._put(f"evt:{event_id}", {"kind": "event", "event_id": event_id, **summary})

    # -- dedupe keys ---------------------------------------------------------------------------------------------
    async def last_seen(self, key: str) -> dict[str, Any] | None:
        return await self._get(f"dk:{key}")

    async def record_seen(self, event: dict[str, Any], t: int, *, decision: str) -> None:
        key = event.get("dedupe_key")
        if not key:
            return
        prev = await self.last_seen(str(key))
        first = prev["event_id"] if (prev and decision == "dedupe") else event["id"]
        await self._put(
            f"dk:{key}",
            {
                "kind": "seen",
                "key": str(key),
                "t": t,
                "event_id": first,
                "last_event_id": event["id"],
                "component": (event.get("subject") or {}).get("component"),
                "type": event.get("type"),
                "severity": event.get("severity"),
                "occurrences": int((prev or {}).get("occurrences", 0)) + 1,
                "event": compact_event(event) if event.get("type") == "alert.fired" else None,
            },
        )

    async def recent_keys(self, now_s: int, horizon_s: int = 900) -> list[dict[str, Any]]:
        out = []
        for v in await self._list("seen"):
            age = now_s - int(v["t"])
            if age < horizon_s:
                out.append({"key": v["key"], "age_s": age, "event_id": v["event_id"], "component": v.get("component")})
        return sorted(out, key=lambda r: (r["age_s"], r["key"]))

    async def burst(self, now_s: int, within_s: int = rules.STORM_WITHIN_S) -> list[dict[str, Any]]:
        """Alert events (latest per dedupe key) seen in the last ``within_s`` seconds, oldest first."""
        rows = [
            v
            for v in await self._list("seen")
            if v.get("type") == "alert.fired" and v.get("event") and 0 <= now_s - int(v["t"]) <= within_s
        ]
        rows.sort(key=lambda v: (int(v["t"]), v["key"]))
        return [dict(v["event"]) for v in rows]

    # -- incidents -----------------------------------------------------------------------------------------------
    async def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        return await self._get(f"inc:{incident_id}")

    async def put_incident(self, item: dict[str, Any]) -> None:
        await self._put(f"inc:{item['id']}", {"kind": "incident", **item})

    async def open_incidents(self, now_s: int) -> list[dict[str, Any]]:
        return sorted(
            (
                i
                for i in await self._list("incident")
                if i.get("status", "open") == "open" and int(i["opened_at"]) <= now_s < int(i["expires_at"])
            ),
            key=lambda i: (int(i["opened_at"]), i["id"]),
        )

    # -- maintenance / storms ------------------------------------------------------------------------------------
    async def put_maintenance(self, window: dict[str, Any]) -> None:
        await self._put(f"mnt:{window['component']}:{window['id']}", {"kind": "maintenance", **window})

    async def maintenance_windows(self) -> list[dict[str, Any]]:
        return await self._list("maintenance")

    async def put_storm(self, key: str, item: dict[str, Any]) -> None:
        await self._put(f"storm:{key}", {"kind": "storm", "key": key, **item})

    async def get_storm(self, key: str) -> dict[str, Any] | None:
        return await self._get(f"storm:{key}")

    async def extend_storm(self, key: str, until: int) -> None:
        """A folded storm member keeps the storm open: it ends after STORM_COOLDOWN_S of quiet."""
        cur = await self.get_storm(key)
        if cur is not None and until > int(cur["until"]):
            await self.put_storm(key, {**{k: v for k, v in cur.items() if k != "key"}, "until": until})

    async def storm_open(self, now_s: int) -> dict[str, Any] | None:
        live = [s for s in await self._list("storm") if int(s["started_at"]) <= now_s < int(s["until"])]
        return max(live, key=lambda s: int(s["started_at"])) if live else None

    # -- the facts bundle the pure rules consume ------------------------------------------------------------------
    async def facts(self, event: dict[str, Any], now_s: int) -> dict[str, Any]:
        comp = (event.get("subject") or {}).get("component")
        opened = await self.open_incidents(now_s)
        # an incident opened by THIS event (a restart after a failed commander start) is not "somebody else's incident"
        own = next((i for i in opened if i.get("event_id") == event.get("id")), None)
        incidents = [
            {
                "id": i["id"],
                "components": sorted({*i["components"], *i.get("joined_components", [])}),
                "severity": i["severity"],
            }
            for i in opened
            if i is not own
        ]
        windows = [
            {"component": w["component"], "start": w["start"], "end": w["end"]}
            for w in await self.maintenance_windows()
            if comp is not None and w.get("component") == comp
        ]
        keys = await self.recent_keys(now_s)
        burst = await self.burst(now_s)
        burst_keys = {str(b.get("dedupe_key")) for b in burst}
        if event.get("type") == "alert.fired" and event.get("dedupe_key"):
            burst_keys.add(str(event["dedupe_key"]))
        storm = await self.storm_open(now_s)
        return {
            "open_incidents": incidents,
            "maintenance_windows": windows,
            "recent_alert_keys": keys,
            "burst_count": len(burst_keys),
            "storm": storm,
            "own_incident": {"id": own["id"], "severity": own["severity"]} if own else None,
        }
