"""Tool-backed evidence for one candidate root (the I/O part of the map step)."""

from __future__ import annotations

from typing import Any

from foghorn.data.access import ScenarioNotFound
from foghorn.flows.event_router import topology
from foghorn.tools import api as tools_api
from foghorn.tools.context import ToolContext, ToolError

ANOMALOUS_Z = 4.0
FALLBACK_METRIC = "error_rate"


def gather(
    scenario: str | None, as_of: str | None, cand: dict[str, Any], comps: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[list[Any]]]:
    """-> (evidence summary, series points).  ``(None, [])`` when there is nothing to look at (zones, no scenario)."""
    root = cand["root"]
    if cand["kind"] == "zone" or not scenario or not topology.known(root):
        return None, []
    worst = (comps.get(root) or {}).get("worst") or {}
    metric = str(worst.get("metric") or FALLBACK_METRIC)
    ctx = ToolContext(scenario_id=scenario, as_of=as_of)
    try:
        try:
            m = tools_api.query_metric(ctx, root, metric, 30)
        except ToolError:
            metric = FALLBACK_METRIC
            m = tools_api.query_metric(ctx, root, metric, 30)
    except (ToolError, ScenarioNotFound):
        return None, []
    z = m.get("max_z")
    evidence = {
        "metric": metric,
        "max_z": z,
        "z_score": m.get("z_score"),
        "onset_estimate": m.get("onset_estimate"),
        "trend": m.get("trend"),
        "baseline": (m.get("baseline") or {}).get("mean"),
        "anomalous": bool(z is not None and z >= ANOMALOUS_Z),
    }
    return evidence, [list(p) for p in m["points"]][-60:]
