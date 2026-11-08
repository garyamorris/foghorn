"""Graph catalog, assistants listing and the aggregated Studio guide."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from foghorn.contracts.meta import GRAPH_IDS
from foghorn.events.service import EventsRuntime
from foghorn.logging import get_logger
from foghorn.server.catalog import CATALOG, SETUP_STEPS, build_guide
from foghorn.server.deps import get_rt, require_auth

log = get_logger("web.graphs")
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
RT = Annotated[EventsRuntime, Depends(get_rt)]


async def scan_assistants(rt: EventsRuntime) -> tuple[list[dict[str, Any]], bool]:
    try:
        raw = await rt.client.assistants.search(limit=200)
    except Exception as exc:
        log.warning("assistants_scan_failed", error=repr(exc))
        return [], False
    rows = raw.get("assistants", []) if isinstance(raw, dict) else list(raw)
    out = []
    for a in rows:
        md = a.get("metadata") or {}
        out.append(
            {
                "assistant_id": a.get("assistant_id"),
                "graph_id": a.get("graph_id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "context": a.get("context") or {},
                "version": a.get("version"),
                "seeded": bool(md.get("foghorn_seed")),
            }
        )
    return out, True


@router.get("/assistants")
async def assistants(rt: RT) -> dict[str, Any]:
    items, ok = await scan_assistants(rt)
    return {"items": items, "scan_ok": ok}


@router.get("/graphs")
async def graphs(rt: RT) -> dict[str, Any]:
    (entries, _warnings), (assistants_list, ok) = await asyncio.gather(
        asyncio.to_thread(build_guide), scan_assistants(rt)
    )
    by_graph: dict[str, list[dict[str, Any]]] = {}
    for a in assistants_list:
        by_graph.setdefault(str(a["graph_id"]), []).append(
            {k: a[k] for k in ("assistant_id", "name", "version", "seeded")}
        )
    studio = rt.studio_links()
    items = [
        {
            "graph_id": e["graph_id"],
            "title": e["title"],
            "role": CATALOG[e["graph_id"]][1],
            "features": e["features"],
            "has_guide": e["has_guide"],
            "assistants": by_graph.get(e["graph_id"], []),
            "studio": {"base_url": studio["base_url"]},
        }
        for e in entries
        if e["graph_id"] in GRAPH_IDS
    ]
    return {"items": items, "assistants_scan_ok": ok}


@router.get("/studio/guide")
async def studio_guide(rt: RT) -> dict[str, Any]:
    entries, warnings = await asyncio.to_thread(build_guide)
    studio = rt.studio_links()
    return {
        "studio": {"base_url": studio["base_url"], "thread_url_template": rt.es.studio_thread_url},
        "setup": {"title": "Set up", "steps": SETUP_STEPS},
        "graphs": entries,
        "warnings": warnings,
    }
