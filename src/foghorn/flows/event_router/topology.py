"""Static topology helpers over the public ``service_catalog()`` (tier, kind, call graph).

Used where no scenario telemetry exists (ambient/stream events) and by the pure triage rules; with a scenario the
enrichment branch calls the topology tools instead.  Edges point caller -> callee (``depends_on``).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from foghorn.data import api as data_api
from foghorn.data.catalog import ASYNC_EDGES  # fire-and-forget edges never propagate errors or latency


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict[str, Any]]:
    """component id -> catalog entry (static, immutable reference data)."""
    return {str(c["id"]): c for c in data_api.service_catalog()}


@lru_cache(maxsize=1)
def _callers() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {cid: [] for cid in catalog()}
    for cid, c in catalog().items():
        for dep in c.get("depends_on", []):
            if dep in out:
                out[dep].append(cid)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def known(component: str | None) -> bool:
    return bool(component) and component in catalog()


def callees_of(component: str) -> tuple[str, ...]:
    c = catalog().get(component)
    return tuple(c.get("depends_on", [])) if c else ()


def callers_of(component: str, *, sync_only: bool = False) -> tuple[str, ...]:
    cs = _callers().get(component, ())
    return tuple(c for c in cs if (c, component) not in ASYNC_EDGES) if sync_only else cs


def neighbours(component: str | None) -> frozenset[str]:
    """The component itself plus its direct callers and callees ("one call-graph hop")."""
    if not component or not known(component):
        return frozenset()
    return frozenset({component, *callers_of(component), *callees_of(component)})


def tier_of(component: str | None) -> int | None:
    c = catalog().get(component or "")
    return int(c["tier"]) if c else None


def kind_of(component: str | None) -> str | None:
    c = catalog().get(component or "")
    return str(c["kind"]) if c else None


def zones_of(component: str | None) -> tuple[str, ...]:
    c = catalog().get(component or "")
    return tuple(c.get("zones", [])) if c else ()


def transitive_callers(component: str, *, sync_only: bool = True) -> dict[str, int]:
    """Everything that (transitively) calls ``component`` -> hop distance (1 = direct caller).  Synchronous edges only by
    default: that is how a failure propagates (and what the ``blast_radius`` tool reports)."""
    seen: dict[str, int] = {}
    frontier = [component]
    hop = 0
    while frontier:
        hop += 1
        nxt: list[str] = []
        for node in frontier:
            for caller in callers_of(node, sync_only=sync_only):
                if caller != component and caller not in seen:
                    seen[caller] = hop
                    nxt.append(caller)
        frontier = nxt
    return seen


def static_blast_radius(component: str) -> dict[str, Any]:
    """Offline equivalent of the ``blast_radius`` tool, computed from the catalog only."""
    c = catalog().get(component)
    if c is None:
        return {
            "service": component,
            "total_affected_if_down": 0,
            "revenue_path_exposed": False,
            "customer_facing_exposed": False,
            "tier0_exposed": [],
            "direct_callers": [],
        }
    callers = transitive_callers(component)
    tier0 = sorted(k for k in callers if catalog()[k]["tier"] == 0)
    return {
        "service": component,
        "tier": c["tier"],
        "direct_callers": list(callers_of(component, sync_only=True)),
        "total_affected_if_down": len(callers),
        "tier0_exposed": tier0,
        "revenue_path_exposed": c.get("criticality") == "revenue"
        or any(catalog()[k].get("criticality") == "revenue" for k in callers),
        "customer_facing_exposed": component == "web-frontend" or "web-frontend" in callers,
    }
