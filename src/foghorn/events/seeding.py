"""Idempotent startup seeding against the running Agent Server (named assistants, Store memories, disabled cron).

Safe to run on every start (``langgraph dev`` re-runs the lifespan on every code reload): assistants use
``if_exists="do_nothing"`` with deterministic ids, memories are only written when absent, the cron is only created
when no cron with our marker exists. Each step is isolated: one failing step never aborts the others, and nothing here
may crash the server (the caller logs the report).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from foghorn.contracts.meta import (
    CTX_APPROVAL_POLICY,
    CTX_MODEL_PROFILE,
    MODEL_PROFILES,
)
from foghorn.events.config import APPROVAL_POLICIES, EventsSettings
from foghorn.events.dataport import DataPort
from foghorn.events.sdk import ClientFactory
from foghorn.logging import get_logger

log = get_logger("events.seeding")

#: namespace for the deterministic ids of the assistants WE create (system assistants keep the server's own ids)
ASSISTANT_NS = uuid.UUID("0b3e8f6a-6a2c-5d1e-9f4b-7c1a5e2d9b10")
CRON_MARKER = {"foghorn_seed": "shift_handoff"}


def assistant_id(name: str) -> str:
    return str(uuid.uuid5(ASSISTANT_NS, name))


def assistant_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for profile in MODEL_PROFILES:
        specs.append(
            {
                "graph_id": "incident_commander",
                "name": f"commander-{profile}",
                "description": f"Incident commander with the offline '{profile}' model profile",
                "context": {CTX_MODEL_PROFILE: profile},
            }
        )
    for policy in APPROVAL_POLICIES:
        specs.append(
            {
                "graph_id": "incident_commander",
                "name": f"commander-policy-{policy.replace('_', '-')}",
                "description": f"Incident commander with approval policy '{policy}'",
                "context": {CTX_MODEL_PROFILE: "competent", CTX_APPROVAL_POLICY: policy},
            }
        )
    for profile in ("competent", "naive"):
        specs.append(
            {
                "graph_id": "rca_tournament",
                "name": f"rca-{profile}",
                "description": f"RCA tournament, '{profile}' profile",
                "context": {CTX_MODEL_PROFILE: profile},
            }
        )
    for profile in ("competent", "cautious"):
        specs.append(
            {
                "graph_id": "ops_copilot",
                "name": f"copilot-{profile}",
                "description": f"Ops copilot, '{profile}' profile",
                "context": {CTX_MODEL_PROFILE: profile},
            }
        )
    return specs


async def seed_assistants(client: Any) -> dict[str, int]:
    created = failed = 0
    for spec in assistant_specs():
        try:
            await client.assistants.create(
                spec["graph_id"],
                assistant_id=assistant_id(spec["name"]),
                name=spec["name"],
                description=spec["description"],
                context=spec["context"],
                metadata={"foghorn_seed": True},
                if_exists="do_nothing",
            )
            created += 1
        except Exception as exc:
            failed += 1
            log.warning("seed_assistant_failed", name=spec["name"], error=repr(exc)[:200])
    return {"ok": created, "failed": failed}


async def seed_memories(client: Any, data: DataPort) -> dict[str, int]:
    try:
        items = await asyncio.to_thread(data.seed_memories)
    except Exception as exc:  # DataUnavailable or anything an unfinished data package raises
        log.info("seed_memories_unavailable", error=repr(exc)[:200])
        return {"seeded": 0, "skipped": 0, "failed": 0, "unavailable": 1}
    seeded = skipped = failed = 0
    for it in items:
        ns, key, value = list(it["namespace"]), str(it["key"]), it["value"]
        try:
            try:
                existing = await client.store.get_item(ns, key)
            except Exception:
                existing = None
            if existing:
                skipped += 1
                continue
            await client.store.put_item(ns, key, value)
            seeded += 1
        except Exception as exc:
            failed += 1
            log.warning("seed_memory_failed", namespace=ns, key=key, error=repr(exc)[:200])
    log.info("seed_memories", seeded=seeded, skipped=skipped, failed=failed)
    return {"seeded": seeded, "skipped": skipped, "failed": failed}


#: the namespace the Agent Server uses for the deterministic id of each graph's system assistant (docs/design/01 #1)
SYSTEM_ASSISTANT_NS = uuid.UUID("6ba7b821-9dad-11d1-80b4-00c04fd430c8")


async def system_assistant_id(client: Any, graph_id: str) -> str:
    """Crons (unlike runs) need the assistant UUID, not the graph id: look the system assistant up, else compute it."""
    try:
        found = await client.assistants.search(graph_id=graph_id, limit=20)
        rows = found.get("assistants", []) if isinstance(found, dict) else list(found)
        system = [a for a in rows if (a.get("metadata") or {}).get("created_by") == "system"]
        if system or rows:
            return str((system or rows)[0]["assistant_id"])
    except Exception as exc:
        log.info("system_assistant_lookup_failed", graph_id=graph_id, error=repr(exc)[:200])
    return str(uuid.uuid5(SYSTEM_ASSISTANT_NS, graph_id))


async def seed_cron(client: Any, es: EventsSettings) -> dict[str, Any]:
    """The native server cron for ``shift_handoff``, created DISABLED (enable it from the System view)."""
    try:
        aid = await system_assistant_id(client, "shift_handoff")
        existing = await client.crons.search(assistant_id=aid, metadata=CRON_MARKER, limit=5)
        if existing:
            return {"created": False, "cron_id": existing[0].get("cron_id")}
        cron = await client.crons.create(
            aid,
            schedule=es.native_cron_schedule,
            input={"window_hours": 8},
            metadata=CRON_MARKER,
            enabled=False,
            on_run_completed="keep",
        )
        return {"created": True, "cron_id": cron.get("cron_id")}
    except Exception as exc:
        log.warning("seed_cron_failed", error=repr(exc)[:200])
        return {"created": False, "error": repr(exc)[:200]}


async def seed_consolidate_cron(client: Any, schedule: str = "0 3 * * *") -> dict[str, Any]:
    """Nightly ``memory_keeper`` consolidation (merge duplicate lessons, reinforce, flag stale), created DISABLED."""
    try:
        aid = await system_assistant_id(client, "memory_keeper")
        existing = await client.crons.search(assistant_id=aid, metadata=CRON_MARKER, limit=5)
        if existing:
            return {"created": False, "cron_id": existing[0].get("cron_id")}
        cron = await client.crons.create(
            aid,
            schedule=schedule,
            input={"mode": "consolidate"},
            metadata=CRON_MARKER,
            enabled=False,
            on_run_completed="keep",
        )
        return {"created": True, "cron_id": cron.get("cron_id")}
    except Exception as exc:
        log.warning("seed_consolidate_cron_failed", error=repr(exc)[:200])
        return {"created": False, "error": repr(exc)[:200]}


async def seed_all(client: Any, data: DataPort, es: EventsSettings) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for name, coro in (
        ("assistants", seed_assistants(client)),
        ("memories", seed_memories(client, data)),
        ("cron", seed_cron(client, es)),
        ("cron_memory", seed_consolidate_cron(client)),
    ):
        try:
            report[name] = await coro
        except Exception as exc:  # defensive: individual seeders already isolate their own failures
            report[name] = {"error": repr(exc)[:200]}
    return report


__all__ = ["ClientFactory", "assistant_id", "assistant_specs", "seed_all"]
