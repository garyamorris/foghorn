"""Evidence gathering without a language model: the standalone tournament (``{"scenario_id": "S05"}``) investigates by
running the commander's own planning functions straight against the pure tool API. The commander's specialists make the
same tool calls through ``create_agent``; this module is what lets ``rca_tournament`` stand on its own.

Sync and CPU/SQLite bound: call it from a sync node or ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any

from foghorn.data import api as data
from foghorn.data import catalog
from foghorn.flows.incident_commander import investigation as inv
from foghorn.flows.rca_tournament.facts import Evidence, Facts, extract_evidence, merge_by_id
from foghorn.tools import api
from foghorn.tools.context import advance_as_of

MAX_ROUNDS = 8


def call_tool(ctx: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The pure-API twin of the LangChain tool of the same name (``advance_time`` is handled by the caller)."""
    a = dict(args)
    if name == "list_alerts":
        return api.list_alerts(
            ctx, a.get("component"), int(a.get("window_minutes", 60)), bool(a.get("active_only", False))
        )
    if name == "query_metric":
        return api.query_metric(ctx, a["service"], a["metric"], int(a.get("window_minutes", 30)))
    if name == "compare_baseline":
        return api.compare_baseline(ctx, a["service"], a["metric"])
    if name == "list_changes":
        return api.list_changes(ctx, int(a.get("window_minutes", 240)), a.get("component"), a.get("lookback_days"))
    if name == "get_topology":
        return api.get_topology(ctx, a["service"])
    if name == "blast_radius":
        return api.blast_radius(ctx, a["service"])
    if name == "search_logs":
        return api.search_logs(
            ctx,
            a["service"],
            a.get("pattern", ""),
            a.get("level"),
            int(a.get("window_minutes", 30)),
            int(a.get("limit", 50)),
        )
    if name == "search_runbooks":
        return api.search_runbooks(a["query"], int(a.get("k", 4)))
    if name == "recall_incidents":
        return api.recall_incidents(a["query"], int(a.get("k", 4)))
    if name == "get_oncall":
        return api.get_oncall(ctx, a["component"])
    raise KeyError(f"no pure-API twin for tool {name!r}")


def gather_evidence(
    scenario_id: str,
    as_of: str | None,
    *,
    profile: str = "competent",
    candidates: list[str] | None = None,
    seed: list[Evidence] | None = None,
) -> tuple[list[Evidence], str, int]:
    """Investigate ``scenario_id`` and return (evidence, final as_of, tool calls used)."""
    meta = data.get_scenario(scenario_id)
    now = as_of or str(meta["trigger"]["as_of"])
    base = {"scenario_id": scenario_id, "as_of": now, "applied_actions": []}
    evidence = merge_by_id([], seed or [])
    calls = 0
    first = api.list_alerts(base, None, 60, False)
    calls += 1
    evidence = merge_by_id(evidence, extract_evidence("list_alerts", {}, first, agent="rca_tournament"))
    focus_seed = list(candidates or [])
    if not focus_seed and not inv.focus_components(Facts.from_evidence(evidence)):
        # no alert names a component (the telemetry may be the problem): probe the revenue-critical tier-0 components
        for c in catalog.COMPONENT_IDS:
            comp = catalog.COMPONENTS[c]
            if comp.tier == 0 and comp.kind == "service":
                res = api.query_metric(base, c, "error_rate", 45)
                calls += 1
                evidence = merge_by_id(evidence, extract_evidence("query_metric", {}, res, agent="rca_tournament"))
        probed = Facts.from_evidence(evidence)
        focus_seed = [*probed.flat(), *probed.anomalies()][:3]
    state: dict[str, Any] = {
        "scenario_id": scenario_id,
        "as_of": now,
        "incident": {"id": "rca", "severity": "SEV3", "title": str(meta["title"])},
        "scenario_meta": {"tier": meta["tier"], "as_of": now},
        "evidence": evidence,
        "agent_runs": [],
        "supervisor_hops": 0,
        "focus": focus_seed,
    }
    for _ in range(MAX_ROUNDS):
        state["supervisor_hops"] += 1
        d = inv.build_digest(state, profile)
        route = inv.plan_next(d)
        if route["next"] != "investigate":
            break
        for agent in route["agents"]:
            task = inv.build_task(agent, route, d)
            settled, n = False, 0
            done: set[str] = set()
            if task.get("settle_minutes"):
                state["as_of"] = advance_as_of({**base, "as_of": state["as_of"]}, float(task["settle_minutes"]))
                state["settled"] = True
                settled = True
            for step in range(3):
                plan = inv.tool_plan(task, step, done)
                if not plan:
                    break
                for name, args in plan:
                    n += 1
                    done.add(name)
                    res = call_tool({**base, "as_of": state["as_of"]}, name, args)
                    state["evidence"] = merge_by_id(state["evidence"], extract_evidence(name, args, res, agent=agent))
            calls += n
            state["agent_runs"].append(
                {
                    "id": f"{agent}:{state['supervisor_hops']}",
                    "agent": agent,
                    "tool_calls": n,
                    "settled": settled,
                    "lookback": bool(task.get("lookback_days")),
                }
            )
    return state["evidence"], str(state["as_of"]), calls + 0
