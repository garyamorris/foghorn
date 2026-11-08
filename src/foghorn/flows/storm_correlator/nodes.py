"""Node functions of ``storm_correlator``: partition -> analyze_candidate (Send xN) -> reduce_level <-> merge_group
(Send xM, bounded loop) -> finalize (defer) -> open_cluster_incident (Send per cluster) -> report (defer)."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeError
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command, Send

from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit
from foghorn.contracts.inputs import GraphResult
from foghorn.contracts.meta import incident_id_for
from foghorn.data import api as data_api
from foghorn.data.access import ScenarioNotFound
from foghorn.data.util import to_epoch, to_iso
from foghorn.flows.event_router import rules, topology
from foghorn.flows.event_router.commander import commander_context, commander_thread_id, parent_config, start_commander
from foghorn.flows.event_router.ledger import Ledger, ledger_lock
from foghorn.flows.storm_correlator import clustering, evidence
from foghorn.flows.storm_correlator.context import StormContext
from foghorn.flows.storm_correlator.state import StormState
from foghorn.models.offline import stable_int

CHAOS_MODES = ("branch", "slow", "transient")


def _ctx(runtime: Runtime[StormContext]) -> StormContext:
    return runtime.context or StormContext()


def _real_scenario(scenario_id: str | None) -> str | None:
    """A scenario id the data layer knows (tools are usable), else None (a stream name, 'unknown', ...)."""
    if not scenario_id:
        return None
    try:
        data_api.get_scenario(scenario_id)
    except (ScenarioNotFound, KeyError):
        return None
    return scenario_id


def _attempt(runtime: Runtime[Any]) -> int:
    info = getattr(runtime, "execution_info", None)
    return int(getattr(info, "node_attempt", 1) or 1)


def cache_key(arg: dict[str, Any]) -> str:
    """Per candidate and alert-set signature; the node cache is global, so scenario and signature are in the key."""
    cand = arg.get("cand") or {}
    return f"storm|{arg.get('scenario')}|{cand.get('id')}|{arg.get('sig')}|{arg.get('as_of')}"


# ----------------------------------------------------------------------------------------------------- partition
async def partition(state: StormState, runtime: Runtime[StormContext]) -> Command[Any]:
    ctx = _ctx(runtime)
    alerts = list(state.get("alerts") or [])
    kept, ignored = clustering.normalize(alerts)
    comps = clustering.summarise(kept)
    total = sum(c["weight"] for c in comps.values())
    cands = clustering.candidates(comps)
    scenario = _real_scenario(state.get("scenario_id")) or _real_scenario(
        next(
            iter(
                Counter(
                    (a.get("trace") or {}).get("scenario_id")
                    for a in alerts
                    if (a.get("trace") or {}).get("scenario_id")
                ).most_common(1)
            ),
            (None, 0),
        )[0]
    )
    as_of = state.get("as_of") or ctx.as_of or (to_iso(max(a["t"] for a in kept)) if kept else None)
    sig = hashlib.sha1("|".join(f"{a['key']}@{a['t']}" for a in kept).encode()).hexdigest()[:12]  # noqa: S324
    chaos_target = None
    if ctx.chaos and cands:
        chaos_target = cands[stable_int("storm-chaos", scenario, sig) % len(cands)]["id"]
    emit(
        "progress",
        f"partition:{sig}",
        agent="partition",
        label=f"{len(alerts)} alerts -> {len(kept)} distinct on {len(comps)} components, {len(cands)} candidate roots",
        pct=10,
    )
    if ignored:
        emit(
            "note", f"ignored:{sig}", agent="partition", text=f"{len(ignored)} alert(s) ignored as noise or non-signals"
        )
    update: dict[str, Any] = {
        "distinct": kept,
        "ignored": ignored,
        "comps": comps,
        "total_weight": total,
        "candidates": cands,
        "chaos_target": chaos_target,
        "scenario": scenario,
        "as_of": as_of,
        "level": 0,
        "clusters": {},
    }
    if not cands:
        return Command(update=update, goto="report")
    sends = [
        Send(
            "analyze_candidate",
            {
                "cand": c,
                "comps": comps,
                "total": total,
                "scenario": scenario,
                "as_of": as_of,
                "sig": sig,
                "chaos": ctx.chaos,
                "chaos_target": chaos_target,
                "branch_timeout_s": ctx.branch_timeout_s,
            },
            timeout=ctx.branch_timeout_s,  # per-Send timeout: enforced for async nodes only (02:19)
        )
        for c in cands
    ]
    return Command(update=update, goto=sends)


# ------------------------------------------------------------------------------------------------ analyze_candidate
async def analyze_candidate(arg: dict[str, Any], runtime: Runtime[StormContext]) -> dict[str, Any]:
    cand = arg["cand"]
    if arg.get("chaos") and cand["id"] == arg.get("chaos_target"):
        mode = str(arg["chaos"])
        if mode == "slow":
            await asyncio.sleep(max(1.0, float(arg["branch_timeout_s"]) * 20))  # outlives the Send timeout
        elif mode == "transient":
            if _attempt(runtime) == 1:
                raise ConnectionError(f"chaos: transient failure analysing {cand['id']}")
        else:
            raise RuntimeError(f"chaos: branch {cand['id']} failed")
    # the tools read SQLite: never on the event loop (BlockingError under `langgraph dev`)
    ev, points = await asyncio.to_thread(evidence.gather, arg.get("scenario"), arg.get("as_of"), cand, arg["comps"])
    cluster = clustering.analyze(cand, arg["comps"], arg["total"], evidence=ev)
    if points:
        emit(
            "series",
            f"series:{cand['id']}",
            agent="analyze_candidate",
            service=cand["root"],
            metric=(ev or {}).get("metric"),
            points=points,
            baseline=(ev or {}).get("baseline"),
            onset=(ev or {}).get("onset_estimate"),
        )
    emit(
        "note",
        f"candidate:{cand['id']}",
        agent="analyze_candidate",
        text=(
            f"{cand['id']}: explains {len(cluster['members'])}/{len(arg['comps'])} alerting components, "
            f"coverage {cluster['coverage']:.0%}, purity {cluster['purity']:.0%}, score {cluster['score']:.2f}"
        ),
    )
    return {"clusters": {cluster["id"]: cluster}}


async def analysis_failed(arg: dict[str, Any], error: NodeError) -> Command[Any]:
    """Error handler for ONE failed/timed-out branch: score it from the static catalog instead (degraded) and carry on."""
    cand = arg["cand"]
    cluster = clustering.analyze(cand, arg["comps"], arg["total"], degraded=True)
    emit(
        "log",
        f"candidate-failed:{cand['id']}",
        agent="analyze_candidate",
        level="warn",
        text=f"{cand['id']} degraded: {type(error.error).__name__}: {error.error}",
    )
    # an error handler does NOT trigger the failed node's outgoing edge: route on explicitly, or a lone failure stalls the graph
    return Command(
        update={
            "clusters": {cluster["id"]: cluster},
            "failures": [{"candidate": cand["id"], "error": f"{type(error.error).__name__}: {error.error}"}],
        },
        goto="reduce_level",
    )


# ------------------------------------------------------------------------------------------------------ reduce_level
async def reduce_level(state: StormState, runtime: Runtime[StormContext]) -> Command[Any]:
    """Reduce step of the map-reduce-reduce loop: merge groups go out as Sends until nothing overlaps or the counter bites."""
    ctx = _ctx(runtime)
    level = int(state.get("level", 0)) + 1
    clusters = state.get("clusters") or {}
    groups = clustering.plan_merges(clusters)
    emit(
        "progress",
        f"reduce:{level}",
        agent="reduce_level",
        label=f"reduce level {level}: {len(groups)} merge group(s) over {len([c for c in clusters.values() if not c.get('superseded_by')])} live cluster(s)",
        pct=min(30 + level * 15, 80),
    )
    if not groups or level > ctx.max_merge_levels:
        return Command(update={"level": level, "bound_hit": bool(groups)}, goto="finalize")
    return Command(
        update={"level": level},
        goto=[
            Send(
                "merge_group",
                {
                    "ids": g,
                    "clusters": [clusters[i] for i in g],
                    "comps": state["comps"],
                    "total": state["total_weight"],
                    "level": level,
                },
                timeout=ctx.branch_timeout_s,
            )
            for g in groups
        ],
    )


async def merge_group(arg: dict[str, Any]) -> dict[str, Any]:
    updates = clustering.merge_group(arg["clusters"], arg["comps"], arg["total"])
    best = next(c for c in updates.values() if not c.get("superseded_by"))
    emit(
        "note",
        f"merge:{arg['level']}:{best['id']}",
        agent="merge_group",
        text=f"level {arg['level']}: {len(arg['ids']) - 1} cluster(s) absorbed into {best['id']} ({len(best['members'])} components)",
    )
    return {"clusters": updates}


async def merge_failed(arg: dict[str, Any], error: NodeError) -> Command[Any]:
    """A failed merge leaves the group unmerged; the reduce loop retries it until its counter bites, and ``finalize``
    still produces disjoint clusters by greedy claiming."""
    failure = {"candidate": "merge:" + ",".join(arg.get("ids", [])), "error": repr(error.error)}
    return Command(update={"failures": [failure]}, goto="reduce_level")


# --------------------------------------------------------------------------------------------------------- finalize
def _rep_key(m: str, comps: dict[str, dict[str, Any]]) -> tuple[float, int, str]:
    tier = topology.tier_of(m)
    return (
        clustering.SEV_WEIGHT.get((comps[m].get("worst") or {}).get("severity", "low"), 0),
        -(tier if tier is not None else 9),
        m,
    )


def _cluster_severity(cluster: dict[str, Any], comps: dict[str, dict[str, Any]]) -> str:
    rep = max(cluster["members"], key=lambda m: _rep_key(m, comps))
    return rules.severity_for((comps[rep].get("worst") or {}).get("severity", "medium"), rep)


def _title(c: dict[str, Any]) -> str:
    n = len(c["members"])
    if c["kind"] == "zone":
        return f"Alert storm: {n} components alerting in {c['root']} (common cause)"
    if c["kind"] == "dependency":
        return f"Alert storm: {n} components share dependency {c['root']}"
    if c["root"] in c["members"]:
        return f"Alert storm: {c['root']} and {n - 1} dependent component(s)" if n > 1 else f"Alert storm: {c['root']}"
    return f"Alert storm: {n} components behind {c['root']}"


async def finalize(state: StormState) -> Command[Any]:
    """Deferred: runs once every map/merge task is done.  Greedy disjoint partition, then one Send per cluster."""
    comps, total = state["comps"], state["total_weight"]
    final, dropped, unexplained = clustering.finalize(state.get("clusters") or {}, comps, total)
    scope = state.get("scenario") or state.get("scenario_id") or "ambient"
    first = min((a["t"] for a in state.get("distinct", [])), default=0)
    for c in final:
        c["severity"] = _cluster_severity(c, comps)
        c["title"] = _title(c)
        c["correlation_id"] = f"storm_{scope}_{c['root']}_{first // 600}"
        c["incident_id"] = incident_id_for(c["correlation_id"])
        c["alert_ids"] = sorted(a["id"] for a in state.get("distinct", []) if a["component"] in c["members"])
        emit(
            "decision",
            f"cluster:{c['id']}",
            agent="finalize",
            what=f"cluster {c['id']}: {len(c['members'])} components, {c['severity']}",
            why=f"score {c['score']:.2f} (coverage {c['coverage']:.0%}, purity {c['purity']:.0%})",
        )
    update: dict[str, Any] = {"final": final, "dropped": dropped, "unexplained": unexplained}
    if not final:
        return Command(update=update, goto="report")
    return Command(
        update=update,
        goto=[
            Send(
                "open_cluster_incident",
                {
                    "cluster": c,
                    "scenario": state.get("scenario"),
                    "scenario_id": state.get("scenario_id"),
                    "as_of": state.get("as_of"),
                    "alerts": [a for a in state.get("alerts", []) if a.get("id") in set(c["alert_ids"])],
                    "now_s": to_epoch(state["as_of"]) if state.get("as_of") else first,
                },
            )
            for c in final
        ],
    )


# ------------------------------------------------------------------------------------------- open_cluster_incident
async def open_cluster_incident(
    arg: dict[str, Any], config: RunnableConfig, store: BaseStore, runtime: Runtime[StormContext]
) -> dict[str, Any]:
    """ONE incident per cluster: adopt the router's incident when it already covers the cluster, else open a new one."""
    ctx = _ctx(runtime)
    c = arg["cluster"]
    members = set(c["members"])
    lg = Ledger(store)
    now_s = int(arg["now_s"])
    incident_id = c["incident_id"]
    adopted = False
    merged: list[dict[str, Any]] = []
    async with ledger_lock():
        replay = await lg.get_incident(incident_id)
        reach = members | ({c["root"]} if c["kind"] != "zone" else set())
        overlapping = [
            i
            for i in await lg.open_incidents(now_s)
            if (set(i["components"]) | set(i.get("joined_components", []))) & reach and i["id"] != incident_id
        ]
        # the EARLIEST incident that already covers the cluster becomes THE incident; the other pre-storm incidents
        # (opened before the burst was recognised) are marked merged and labelled in the feed
        primary = replay or (overlapping[0] if overlapping else None)
        merged = [i for i in overlapping if primary is None or i["id"] != primary["id"]]
        for other in merged:
            await lg.put_incident({**other, "status": "merged", "merged_into": (primary or {}).get("id", incident_id)})
        if primary is not None:
            adopted = True
            incident_id = primary["id"]
            sev = c["severity"] if rules.more_severe(c["severity"], primary["severity"]) else primary["severity"]
            await lg.put_incident(
                {
                    **primary,
                    "components": sorted({*primary["components"], *members}),
                    "severity": sev,
                    "title": c["title"],
                    "cluster": c["id"],
                }
            )
            severity = sev
        else:
            severity = c["severity"]
            await lg.put_incident(
                {
                    "id": incident_id,
                    "correlation_id": c["correlation_id"],
                    "components": sorted(members),
                    "severity": severity,
                    "opened_at": now_s,
                    "expires_at": now_s + rules.INCIDENT_JOIN_WINDOW_S,
                    "status": "open",
                    "scenario_id": arg.get("scenario"),
                    "event_id": None,
                    "thread_id": commander_thread_id(incident_id),
                    "title": c["title"],
                    "occurrences": len(c["alert_ids"]),
                    "event_ids": c["alert_ids"][:25],
                    "cluster": c["id"],
                }
            )
    for other in merged:  # one feed entry per merged incident, outside the lock
        emit_event(
            "incident.updated",
            idempotency_key=f"incident.updated:{other['id']}:merged:{incident_id}",
            component=(other["components"] or [None])[0],
            severity="low",
            payload={
                "incident_id": other["id"],
                "title": f"[merged into {incident_id}] {other.get('title', '')}".strip(),
                "merged_into": incident_id,
                "thread_id": commander_thread_id(other["id"]),
                "scenario_id": arg.get("scenario"),
            },
            correlation_id=other.get("correlation_id"),
            scenario_id=arg.get("scenario"),
            occurred_at=arg.get("as_of"),
        )
    thread_id = commander_thread_id(incident_id)
    payload = {
        "incident_id": incident_id,
        "severity": severity,
        "title": c["title"],
        "status": "open",
        "thread_id": thread_id,
        "scenario_id": arg.get("scenario"),
        "graph_id": "incident_commander",
        "cluster": {k: c[k] for k in ("id", "kind", "root", "members", "score", "coverage", "purity")},
    }
    emit_event(
        "incident.updated" if adopted else "incident.opened",
        idempotency_key=f"incident.{'updated' if adopted else 'opened'}:{incident_id}:storm:{c['id']}",
        component=c["root"] if topology.known(c["root"]) else (c["members"][0] if c["members"] else None),
        severity=rules.SEV_TO_EVENT_SEVERITY.get(severity, "high"),  # type: ignore[arg-type]
        payload=payload,
        correlation_id=c["correlation_id"],
        scenario_id=arg.get("scenario"),
        occurred_at=arg.get("as_of"),
    )
    commander = "skipped:dry_run" if not ctx.dispatch_commander else "skipped:no_scenario"
    children: list[str] = []
    scenario = arg.get("scenario")
    if ctx.dispatch_commander and scenario:
        started = await start_commander(
            config=parent_config(config, f"storm-{c['correlation_id']}"),
            incident_id=incident_id,
            correlation_id=c["correlation_id"],
            severity=severity,
            title=c["title"],
            scenario_id=scenario,
            event_id=c["alert_ids"][0] if c["alert_ids"] else None,
            as_of=arg.get("as_of"),
            alerts=arg["alerts"],
            trigger_event_ids=c["alert_ids"][:25],
            context=commander_context(
                scenario_id=scenario,
                model_profile=ctx.model_profile,
                approval_policy=ctx.approval_policy,
                memory_enabled=ctx.memory_enabled,
                chaos=ctx.chaos if ctx.chaos == "tools" else None,
                token_delay_ms=ctx.token_delay_ms,
                breakpoints=ctx.breakpoints,
            ),
        )
        commander = "joined" if (started["values"] or adopted) else "started"
        children.append(started["child_thread_id"])
        emit(
            "handoff",
            f"handoff:{incident_id}:{c['id']}",
            agent="open_cluster_incident",
            to="incident_commander",
            reason=f"{severity} {c['title']}",
            thread_id=started["child_thread_id"],
        )
    return {
        "incidents": [
            {
                "cluster": c["id"],
                "incident_id": incident_id,
                "severity": severity,
                "adopted": adopted,
                "merged": [m["id"] for m in merged],
                "commander": commander,
                "thread_id": thread_id,
            }
        ],
        "child_threads": children,
    }


async def incident_failed(arg: dict[str, Any], error: NodeError) -> Command[Any]:
    """One cluster's incident could not be opened/started: report it, keep the other clusters' incidents."""
    c = arg["cluster"]
    emit("log", f"incident-failed:{c['id']}", agent="open_cluster_incident", level="error", text=repr(error.error))
    return Command(
        update={
            "incidents": [
                {
                    "cluster": c["id"],
                    "incident_id": c["incident_id"],
                    "severity": c["severity"],
                    "adopted": False,
                    "merged": [],
                    "commander": "failed",
                    "error": f"{type(error.error).__name__}: {error.error}",
                }
            ],
            "failures": [{"candidate": f"incident:{c['id']}", "error": repr(error.error)}],
        },
        goto="report",
    )


# ---------------------------------------------------------------------------------------------------------- report
async def report(state: StormState) -> dict[str, Any]:
    final = state.get("final") or []
    failures = state.get("failures") or []
    incidents = state.get("incidents") or []
    n_alerts = len(state.get("alerts") or [])
    degraded = bool(failures) or any(c.get("degraded") for c in final)
    if not state.get("distinct"):
        summary = f"no clusterable signal in {n_alerts} alert(s) ({len(state.get('ignored') or [])} ignored)"
        status = "skipped"
    else:
        names = ", ".join(f"{c['root']} ({len(c['members'])})" for c in final) or "none"
        summary = f"{n_alerts} alerts -> {len(final)} cluster(s): {names}" + (
            f"; degraded ({len(failures)} failed step(s))" if degraded else ""
        )
        status = "ok"
    result: GraphResult = {
        "graph_id": "storm_correlator",
        "status": status,  # type: ignore[typeddict-item]
        "event_id": None,
        "correlation_id": final[0]["correlation_id"] if final else None,
        "summary": summary,
        "artifacts": [
            {
                "kind": "table",
                "name": "clusters",
                "ref": None,
                "data": [
                    {
                        k: c[k]
                        for k in (
                            "id",
                            "kind",
                            "root",
                            "members",
                            "severity",
                            "title",
                            "incident_id",
                            "correlation_id",
                            "score",
                            "coverage",
                            "purity",
                            "alerts",
                            "degraded",
                            "absorbed",
                        )
                    }
                    for c in final
                ],
            },
            {"kind": "table", "name": "incident_candidates", "ref": None, "data": incidents},
            {"kind": "table", "name": "ignored", "ref": None, "data": state.get("ignored") or []},
            {"kind": "table", "name": "failures", "ref": None, "data": failures},
            {"kind": "table", "name": "dropped", "ref": None, "data": state.get("dropped") or []},
        ],
        "metrics": {
            "alerts": float(n_alerts),
            "distinct_alerts": float(len(state.get("distinct") or [])),
            "components": float(len(state.get("comps") or {})),
            "candidates": float(len(state.get("candidates") or [])),
            "clusters": float(len(final)),
            "merge_levels": float(max(int(state.get("level", 0)) - 1, 0)),
            "failed_branches": float(len(failures)),
            "degraded": 1.0 if degraded else 0.0,
            "coverage": float(sum(c["coverage"] for c in final)),
        },
        "child_threads": list(state.get("child_threads") or []),
    }
    return {"result": result}
