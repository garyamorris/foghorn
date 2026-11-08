"""storm_correlator: Send map (one task per candidate root) -> bounded merge loop -> deferred finalize -> one incident per cluster."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from foghorn.common import lifecycle
from foghorn.contracts.meta import det_thread_id
from foghorn.data import api as data_api
from foghorn.flows.event_router import topology
from foghorn.flows.event_router.build import build_graph as build_router
from foghorn.flows.event_router.ledger import Ledger
from foghorn.flows.storm_correlator import clustering as cl
from foghorn.flows.storm_correlator import evidence
from foghorn.flows.storm_correlator.build import build_graph
from foghorn.tools import api as tools_api
from tests.graphs.support_ingress import FakeClient, install_fake_client, make_event, uninstall_fake_client

SIGNALS = ("alert.fired", "slo.burn_rate", "healthcheck.failed")
SCENARIOS = [f"S{i:02d}" for i in range(1, 17)]


@pytest.fixture
def client():
    c = install_fake_client()
    yield c
    uninstall_fake_client()


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def graph(store, client):
    return build_graph(checkpointer=InMemorySaver(), store=store)


_n = 0


def storm_alerts(sid: str) -> list[dict[str, Any]]:
    return [e for e in data_api.scenario_events(sid) if e["type"] in SIGNALS]


async def correlate(graph, alerts, *, scenario="S12", ctx=None, **extra) -> dict[str, Any]:
    global _n
    _n += 1
    cfg = {"configurable": {"thread_id": f"storm-{_n}"}}
    return await graph.ainvoke({"scenario_id": scenario, "alerts": alerts, **extra}, cfg, context=ctx)  # type: ignore[no-any-return]


def commander_runs(client: FakeClient) -> list[dict[str, Any]]:
    return client.runs_for("incident_commander")


def clusters_of(out: dict[str, Any]) -> list[dict[str, Any]]:
    return next(a for a in out["result"]["artifacts"] if a["name"] == "clusters")["data"]  # type: ignore[no-any-return]


# ================================================================================ pure clustering
def test_normalize_keeps_one_alert_per_dedupe_key_and_ignores_noise():
    alerts = [
        make_event("a1", occurred_at="2026-11-05T10:00:00Z"),
        make_event(
            "a2", occurred_at="2026-11-05T10:01:00Z", severity="critical"
        ),  # same key: severity wins, time = first
        make_event("p1", component="email-provider", severity="low", rule="synthetic_probe_failed"),
        make_event("i1", component="cart-service", severity="info", rule="error_rate_high"),
        make_event("u1", component="mystery-service"),
        make_event("r1", type="alert.resolved", severity="info"),
        make_event("c1", type="customer.report", rule=None, severity="medium"),
    ]
    kept, ignored = cl.normalize(alerts)
    assert len(kept) == 1 and kept[0]["severity"] == "critical" and kept[0]["n"] == 2
    assert kept[0]["t"] < cl.normalize([alerts[1]])[0][0]["t"]
    reasons = {i["id"]: i["reason"] for i in ignored}
    assert (
        reasons["p1"].startswith("benign")
        and reasons["i1"].startswith("benign")
        and "unknown component" in reasons["u1"]
    )
    assert "not a signal" in reasons["r1"] and "not a signal" in reasons["c1"]


def test_candidates_cover_alerting_components_hidden_dependencies_and_pure_zones():
    kept, _ = cl.normalize(storm_alerts("S12"))
    comps = cl.summarise(kept)
    ids = {c["id"] for c in cl.candidates(comps)}
    assert "zone:zone-b" in ids and "zone:zone-a" not in ids  # zone-a also hosts services that are quiet
    assert {"dependency:orders-db", "dependency:sessions-cache"} <= ids and "component:cart-service" in ids
    s06, _ = cl.normalize(storm_alerts("S06"))
    assert not any(c["kind"] == "zone" for c in cl.candidates(cl.summarise(s06)))  # purity gate: not a zone problem


def test_score_is_coverage_purity_and_onset():
    kept, _ = cl.normalize(storm_alerts("S01"))
    comps = cl.summarise(kept)
    total = sum(c["weight"] for c in comps.values())
    c = cl.analyze(
        {"id": "component:checkout-service", "kind": "component", "root": "checkout-service", "label": "x"},
        comps,
        total,
    )
    assert (
        c["members"] == ["checkout-service", "edge-gateway", "web-frontend"]
        and c["coverage"] == 1.0
        and c["purity"] == 1.0
    )
    assert c["onset"] == 1.0 and c["score"] == pytest.approx(0.55 + 0.25 + 0.20)
    late = cl.analyze(
        {"id": "component:web-frontend", "kind": "component", "root": "web-frontend", "label": "x"}, comps, total
    )
    assert late["score"] < c["score"] and late["coverage"] < 1.0


def _cluster(
    cid: str, members: list[str], score: float, level: int = 0, sup: str | None = None, degraded: bool = False
) -> dict[str, Any]:
    return {"id": cid, "members": members, "score": score, "level": level, "superseded_by": sup, "degraded": degraded}


def test_cluster_reducer_is_idempotent_commutative_and_supersession_is_monotonic():
    a = {"x": _cluster("x", ["a"], 0.5), "y": _cluster("y", ["b"], 0.4)}
    b = {"x": _cluster("x", ["a", "c"], 0.6, level=1), "y": _cluster("y", ["b"], 0.4, sup="x")}
    c = {"x": _cluster("x", ["a"], 0.9, degraded=True), "z": _cluster("z", ["d"], 0.2)}
    for l, r in itertools.permutations([a, b, c], 2):
        assert cl.merge_clusters(cl.merge_clusters(l, r), r) == cl.merge_clusters(l, r)  # idempotent
    m = cl.merge_clusters(cl.merge_clusters(a, b), c)
    assert m == cl.merge_clusters(cl.merge_clusters(c, a), b)  # order independent
    assert m["x"]["level"] == 1 and m["x"]["members"] == ["a", "c"]  # higher level beats higher score
    assert m["y"]["superseded_by"] == "x"
    assert cl.merge_clusters(m, {"y": _cluster("y", ["b"], 0.99)})["y"]["superseded_by"] == "x"  # monotonic
    assert cl.merge_clusters(None, None) == {} and cl.merge_clusters({"q": a["x"]}, None) == {"q": a["x"]}


def test_overlapping_clusters_merge_into_the_best_one_and_unrelated_ones_do_not():
    kept, _ = cl.normalize(storm_alerts("S12"))
    comps = cl.summarise(kept)
    total = sum(c["weight"] for c in comps.values())
    clusters: dict[str, Any] = {}
    for cand in cl.candidates(comps):
        a = cl.analyze(cand, comps, total)
        clusters = cl.merge_clusters(clusters, {a["id"]: a})
    [group] = cl.plan_merges(clusters)
    assert len(group) == len(clusters) and "zone:zone-b" in group
    clusters = cl.merge_clusters(clusters, cl.merge_group([clusters[i] for i in group], comps, total))
    assert cl.plan_merges(clusters) == []
    live = [c for c in clusters.values() if not c["superseded_by"]]
    assert (
        [c["id"] for c in live] == ["zone:zone-b"]
        and live[0]["level"] == 1
        and len(live[0]["absorbed"]) == len(group) - 1
    )


def test_finalize_makes_disjoint_clusters_and_drops_what_is_subsumed_or_too_small():
    chain = [
        make_event(f"a{i}", component=c, rule="error_rate_high")
        for i, c in enumerate(["payments-service", "checkout-service", "edge-gateway", "web-frontend"])
    ]
    other = [
        make_event(f"b{i}", component=c, rule="p95_latency_high")
        for i, c in enumerate(["catalog-service", "search-service"])
    ]
    kept, _ = cl.normalize(chain + other)
    comps = cl.summarise(kept)
    total = sum(c["weight"] for c in comps.values())
    clusters = {}
    for cand in cl.candidates(comps):
        a = cl.analyze(cand, comps, total)
        clusters = cl.merge_clusters(clusters, {a["id"]: a})
    final, dropped, unexplained = cl.finalize(clusters, comps, total)
    members = [m for c in final for m in c["members"]]
    assert len(members) == len(set(members)) and unexplained == []  # disjoint, and everything is explained
    assert {c["root"] for c in final} == {"payments-service", "catalog-service"}
    assert any("higher-scoring" in d["reason"] for d in dropped)
    tiny = cl.finalize(
        {
            "x": cl.analyze(
                {"id": "component:web-frontend", "kind": "component", "root": "web-frontend", "label": "x"},
                comps,
                total * 50,
            )
        },
        comps,
        total * 50,
    )
    assert tiny[0] == [] and "storm weight" in tiny[1][0]["reason"]


@pytest.mark.parametrize("sid", SCENARIOS)
def test_every_scenarios_alerts_cluster_into_disjoint_clusters_that_explain_every_alerting_component(sid):
    kept, _ = cl.normalize(storm_alerts(sid))
    comps = cl.summarise(kept)
    total = sum(c["weight"] for c in comps.values())
    clusters: dict[str, Any] = {}
    for cand in cl.candidates(comps):
        a = cl.analyze(cand, comps, total)
        clusters = cl.merge_clusters(clusters, {a["id"]: a})
    for _ in range(4):
        groups = cl.plan_merges(clusters)
        if not groups:
            break
        for g in groups:
            clusters = cl.merge_clusters(clusters, cl.merge_group([clusters[i] for i in g], comps, total))
    final, _, unexplained = cl.finalize(clusters, comps, total)
    members = [m for c in final for m in c["members"]]
    assert len(members) == len(set(members)) and set(members) | set(unexplained) == set(comps)
    assert len(final) <= 2 and all(0 < c["score"] <= 1 for c in final)


def test_evidence_is_none_without_a_scenario_or_for_zones_and_confirms_an_excursion_otherwise():
    kept, _ = cl.normalize(storm_alerts("S01"))
    comps = cl.summarise(kept)
    cand = {"id": "component:checkout-service", "kind": "component", "root": "checkout-service", "label": "x"}
    assert evidence.gather(None, None, cand, comps) == (None, [])
    assert evidence.gather("S01", None, {**cand, "kind": "zone"}, comps) == (None, [])
    ev, points = evidence.gather("S01", "2026-11-05T14:07:00Z", cand, comps)
    assert ev and ev["anomalous"] and ev["max_z"] >= 4 and 0 < len(points) <= 60
    assert evidence.gather("S01", "2026-11-05T14:07:00Z", {**cand, "root": "not-a-service"}, comps) == (None, [])


# ================================================================================ the graph: S12
async def test_s12_collapses_into_one_cluster_one_incident_one_pinned_commander(graph, client):
    alerts = storm_alerts("S12")
    assert len(alerts) >= 8
    with lifecycle.capture() as events:
        out = await correlate(graph, alerts)
    res = out["result"]
    [c] = clusters_of(out)
    assert res["graph_id"] == "storm_correlator" and res["status"] == "ok"
    assert c["id"] == "zone:zone-b" and c["kind"] == "zone" and len(c["members"]) == 11 and c["severity"] == "SEV1"
    assert c["score"] > 0.95 and c["coverage"] == 1.0 and len(c["absorbed"]) == 13 and not c["degraded"]
    assert (
        res["metrics"]["clusters"] == 1 and res["metrics"]["candidates"] == 14 and res["metrics"]["merge_levels"] == 1
    )
    assert (
        res["metrics"]["failed_branches"] == 0
        and res["metrics"]["degraded"] == 0
        and res["metrics"]["alerts"] == len(alerts)
    )

    [opened] = [e for e in events if e["type"] == "incident.opened"]
    inc = opened["payload"]["incident_id"]
    assert (
        inc == c["incident_id"]
        and opened["payload"]["cluster"]["root"] == "zone-b"
        and opened["payload"]["severity"] == "SEV1"
    )
    [run] = commander_runs(client)
    assert run["thread_id"] == det_thread_id("incident_commander", inc) and res["child_threads"] == [run["thread_id"]]
    assert (
        run["input"]["incident"]["id"] == inc
        and run["input"]["scenario_id"] == "S12"
        and run["multitask_strategy"] == "reject"
    )
    assert run["input"]["alerts"] and {a["subject"]["component"] for a in run["input"]["alerts"]} == set(c["members"])
    md = client.threads.created[run["thread_id"]]["metadata"]
    assert (
        md["foghorn_incident_id"] == inc
        and md["foghorn_correlation_id"] == c["correlation_id"]
        and md["foghorn_scenario_id"] == "S12"
    )
    assert run["context"]["scenario_id"] == "S12"


async def test_the_flood_in_stream_storm_collapses_the_same_way(graph, client):
    labels = {lab["id"]: lab for lab in data_api.iter_stream_labels("stream_storm")}
    flood = [e for e in data_api.iter_stream("stream_storm") if labels[e["id"]]["storm"] and e["type"] == "alert.fired"]
    assert len(flood) > 400
    out = await correlate(
        graph, flood, scenario="stream_storm"
    )  # a stream name, but every alert carries trace.scenario_id S12
    [c] = clusters_of(out)
    assert c["id"] == "zone:zone-b" and len(c["members"]) == 11
    assert out["result"]["metrics"]["alerts"] == len(flood) and out["result"]["metrics"]["distinct_alerts"] < len(flood)
    assert len(commander_runs(client)) == 1 and commander_runs(client)[0]["input"]["scenario_id"] == "S12"
    ignored = next(a for a in out["result"]["artifacts"] if a["name"] == "ignored")["data"]
    assert ignored  # the benign provider-probe flapping inside the flood


async def test_send_fan_out_one_task_per_candidate_then_a_bounded_merge_loop_then_a_deferred_finalize(graph):
    seen: list[str] = []
    levels: list[int] = []
    cfg = {"configurable": {"thread_id": "fan-1"}}
    async for chunk in graph.astream({"scenario_id": "S12", "alerts": storm_alerts("S12")}, cfg, stream_mode="updates"):
        for node, upd in chunk.items():
            if node != "__metadata__":
                seen.append(node)
            if node == "reduce_level":
                levels.append(upd["level"])
    assert seen.count("analyze_candidate") == 14 and seen.count("merge_group") == 1
    assert levels == [1, 2]  # reduce ran twice: once with a group to merge, once to confirm nothing overlaps
    assert seen.index("finalize") > max(i for i, n in enumerate(seen) if n in ("analyze_candidate", "merge_group"))
    assert seen[-1] == "report" and seen.count("open_cluster_incident") == 1


async def test_the_merge_loop_is_bounded_by_its_counter_and_finalize_still_yields_disjoint_clusters(graph):
    cfg = {"configurable": {"thread_id": "bound-1"}}
    out = await graph.ainvoke(
        {"scenario_id": "S12", "alerts": storm_alerts("S12")},
        cfg,
        context={"max_merge_levels": 0, "dispatch_commander": False},
    )
    st = (await graph.aget_state(cfg)).values
    assert st["bound_hit"] is True and st["level"] == 1 and out["result"]["metrics"]["merge_levels"] == 0
    [c] = clusters_of(out)
    assert (
        c["id"] == "zone:zone-b" and c["absorbed"] == []
    )  # not merged, but greedy claiming kept the partition disjoint
    assert any("higher-scoring" in d["reason"] for d in st["dropped"])


async def test_two_independent_problems_become_two_clusters_two_incidents_two_commanders(graph, client):
    chain = [
        make_event(f"a{i}", component=c, rule="error_rate_high", scenario_id="S01", occurred_at="2026-11-05T14:07:00Z")
        for i, c in enumerate(["checkout-service", "edge-gateway"])
    ]
    other = [
        make_event(f"b{i}", component=c, rule="error_rate_high", scenario_id="S01", occurred_at="2026-11-05T14:07:00Z")
        for i, c in enumerate(["email-provider", "notification-service"])
    ]
    with lifecycle.capture() as events:
        out = await correlate(graph, chain + other, scenario="S01")
    cs = clusters_of(out)
    assert {c["root"] for c in cs} == {"checkout-service", "email-provider"} and len(
        {c["incident_id"] for c in cs}
    ) == 2
    assert [len(c["members"]) for c in cs] == [2, 2]
    runs = commander_runs(client)
    assert len(runs) == 2 and {r["thread_id"] for r in runs} == {
        det_thread_id("incident_commander", c["incident_id"]) for c in cs
    }
    assert [e["type"] for e in events].count("incident.opened") == 2


async def test_no_signal_means_a_skipped_result_and_no_incident(graph, client):
    out = await correlate(
        graph,
        [make_event("p", component="email-provider", severity="low", rule="synthetic_probe_failed")],
        scenario="S04",
    )
    assert (
        out["result"]["status"] == "skipped" and out["result"]["metrics"]["clusters"] == 0 and not client.runs.created
    )
    empty = await correlate(graph, [], scenario="S04")
    assert empty["result"]["status"] == "skipped" and "no clusterable signal" in empty["result"]["summary"]


async def test_dry_run_clusters_but_opens_nothing(graph, client):
    with lifecycle.capture() as events:
        out = await correlate(graph, storm_alerts("S12"), ctx={"dispatch_commander": False})
    assert out["result"]["metrics"]["clusters"] == 1 and not client.runs.created
    assert [e["type"] for e in events].count(
        "incident.opened"
    ) == 1  # the incident is real, only the commander is skipped
    assert (
        next(a for a in out["result"]["artifacts"] if a["name"] == "incident_candidates")["data"][0]["commander"]
        == "skipped:dry_run"
    )


async def test_alerts_without_telemetry_are_clustered_statically_and_get_no_commander(graph, client):
    alerts = [
        make_event(f"a{i}", component=c, scenario_id=None)
        for i, c in enumerate(["checkout-service", "edge-gateway", "web-frontend"])
    ]
    out = await correlate(graph, alerts, scenario="stream_x")
    [c] = clusters_of(out)
    assert c["root"] == "checkout-service"
    assert (
        next(a for a in out["result"]["artifacts"] if a["name"] == "incident_candidates")["data"][0]["commander"]
        == "skipped:no_scenario"
    )
    assert not client.runs.created


# ================================================================================ adoption / merging
async def test_the_router_and_the_correlator_agree_on_one_incident_for_a_storm(client, store):
    """The router opens an incident from the first alerts, escalates the burst, the correlator adopts that incident."""
    router = build_router(checkpointer=InMemorySaver(), store=store)
    storm = build_graph(checkpointer=InMemorySaver(), store=store)
    alerts = storm_alerts("S12")
    with lifecycle.capture() as events:
        for e in alerts[:12]:
            await router.ainvoke({"event": e}, {"configurable": {"thread_id": f"r-{e['id']}"}})
        [storm_run] = client.runs_for("storm_correlator")
        out = await storm.ainvoke(
            storm_run["input"], {"configurable": {"thread_id": "adopt"}}, context=storm_run["context"]
        )
    opened = [e for e in events if e["type"] == "incident.opened"]
    assert len(opened) == 1  # THE incident is the router's; the correlator did not open a second one
    inc = opened[0]["payload"]["incident_id"]
    [cinfo] = next(a for a in out["result"]["artifacts"] if a["name"] == "incident_candidates")["data"]
    assert cinfo["incident_id"] == inc and cinfo["adopted"] is True
    adopt = [e for e in events if e["type"] == "incident.updated" and e["payload"].get("cluster")]
    assert (
        adopt
        and adopt[0]["payload"]["incident_id"] == inc
        and "zone-b" in adopt[0]["payload"]["title"]
        or "Alert storm" in adopt[0]["payload"]["title"]
    )
    assert len(commander_runs(client)) == 1 and commander_runs(client)[0]["thread_id"] == det_thread_id(
        "incident_commander", inc
    )
    # later alerts on any cluster member now join that incident instead of opening a new one
    late = await router.ainvoke(
        {
            "event": make_event(
                "late",
                component="pricing-service",
                rule="cpu_saturated",
                severity="medium",
                scenario_id="S12",
                occurred_at="2026-11-06T11:50:00Z",
            )
        },
        {"configurable": {"thread_id": "late"}},
    )
    assert late["route"] == "join_incident" and late["triage"]["attach_to"] == inc


async def test_pre_storm_incidents_are_merged_into_the_earliest_one(graph, client, store):
    lg = Ledger(store)
    now = 1_793_000_000
    for i, (comp, opened) in enumerate(
        [("cart-service", now - 90), ("payments-service", now - 60), ("pricing-service", now - 30)]
    ):
        await lg.put_incident(
            {
                "id": f"INC-P{i}",
                "correlation_id": f"c{i}",
                "components": [comp],
                "severity": "SEV2",
                "opened_at": opened,
                "expires_at": now + 3000,
                "status": "open",
                "event_id": f"e{i}",
            }
        )
    alerts = storm_alerts("S12")
    from foghorn.data.util import to_iso

    with lifecycle.capture() as events:
        out = await correlate(graph, alerts, as_of=to_iso(now))
    [info] = next(a for a in out["result"]["artifacts"] if a["name"] == "incident_candidates")["data"]
    assert info["incident_id"] == "INC-P0" and info["adopted"] and info["merged"] == ["INC-P1", "INC-P2"]
    merged = [e for e in events if e["type"] == "incident.updated" and e["payload"].get("merged_into")]
    assert sorted(e["payload"]["incident_id"] for e in merged) == ["INC-P1", "INC-P2"]
    assert all(e["payload"]["title"].startswith("[merged into INC-P0]") for e in merged)
    assert [i["id"] for i in await lg.open_incidents(now)] == ["INC-P0"]
    assert (await lg.get_incident("INC-P0"))["severity"] == "SEV1"  # raised to the cluster's severity
    assert [r["thread_id"] for r in commander_runs(client)] == [det_thread_id("incident_commander", "INC-P0")]


async def test_a_rerun_of_the_same_storm_is_idempotent(graph, client):
    alerts = storm_alerts("S12")
    with lifecycle.capture() as events:
        a = await correlate(graph, alerts)
        b = await correlate(graph, alerts)
    assert clusters_of(a)[0]["incident_id"] == clusters_of(b)[0]["incident_id"]
    assert len(commander_runs(client)) == 1 and len(client.runs.rejected) == 1  # the second start answered 409 = joined
    assert [e["type"] for e in events].count("incident.opened") == 1


# ================================================================================ failure injection
async def test_chaos_branch_failure_goes_to_the_error_handler_and_degrades_the_result(graph, client):
    out = await correlate(graph, storm_alerts("S12"), ctx={"chaos": "branch"})
    res = out["result"]
    failures = next(a for a in res["artifacts"] if a["name"] == "failures")["data"]
    assert len(failures) == 1 and "chaos: branch" in failures[0]["error"] and res["metrics"]["failed_branches"] == 1
    assert res["metrics"]["degraded"] == 1 and "degraded" in res["summary"]
    [c] = clusters_of(out)
    assert c["id"] == "zone:zone-b" and len(c["members"]) == 11  # the right answer survives a lost branch
    assert len(commander_runs(client)) == 1


async def test_chaos_choice_of_the_failing_branch_is_deterministic(graph):
    picks = set()
    for _ in range(2):
        out = await correlate(graph, storm_alerts("S12"), ctx={"chaos": "tools", "dispatch_commander": False})
        picks.add(next(a for a in out["result"]["artifacts"] if a["name"] == "failures")["data"][0]["candidate"])
    assert len(picks) == 1


async def test_chaos_failing_branch_that_is_the_best_cluster_still_degrades_gracefully(graph):
    probe = await correlate(graph, storm_alerts("S12"), ctx={"chaos": "branch", "dispatch_commander": False})
    victim = next(a for a in probe["result"]["artifacts"] if a["name"] == "failures")["data"][0]["candidate"]
    assert victim  # whichever branch the seeded chaos picked, degraded static scoring replaced it
    cfg = {"configurable": {"thread_id": "deg-1"}}
    await graph.ainvoke(
        {"scenario_id": "S12", "alerts": storm_alerts("S12")},
        cfg,
        context={"chaos": "branch", "dispatch_commander": False},
    )
    st = (await graph.aget_state(cfg)).values
    assert st["clusters"][victim]["degraded"] is True or st["clusters"][victim]["superseded_by"]


async def test_slow_branch_hits_the_per_send_timeout_and_is_handled(graph):
    out = await correlate(
        graph, storm_alerts("S12"), ctx={"chaos": "slow", "branch_timeout_s": 0.2, "dispatch_commander": False}
    )
    failures = next(a for a in out["result"]["artifacts"] if a["name"] == "failures")["data"]
    assert len(failures) == 1 and "NodeTimeoutError" in failures[0]["error"] and "timeout" in failures[0]["error"]
    assert out["result"]["metrics"]["clusters"] == 1


async def test_transient_branch_failure_is_retried_and_heals(graph, monkeypatch):
    attempts: list[str] = []
    orig = evidence.gather
    monkeypatch.setattr(evidence, "gather", lambda *a: (attempts.append(a[2]["id"]), orig(*a))[1])
    out = await correlate(graph, storm_alerts("S12"), ctx={"chaos": "transient", "dispatch_commander": False})
    assert out["result"]["metrics"]["failed_branches"] == 0 and out["result"]["metrics"]["degraded"] == 0
    assert len(attempts) == 14  # every branch ran its tools exactly once: the failed first attempt never reached them


async def test_a_failing_incident_open_is_reported_but_does_not_lose_the_cluster(graph, client):
    client.runs.fail_with = ConnectionError("agent server down")
    out = await correlate(graph, storm_alerts("S12"), ctx={"dispatch_commander": True})
    [info] = next(a for a in out["result"]["artifacts"] if a["name"] == "incident_candidates")["data"]
    assert info["commander"] == "failed" and "ConnectionError" in info["error"]
    assert out["result"]["metrics"]["failed_branches"] == 1 and out["result"]["metrics"]["clusters"] == 1


# ================================================================================ cache
async def test_branch_results_are_cached_across_threads_keyed_by_the_alert_set(graph, monkeypatch):
    calls = {"n": 0}
    orig = tools_api.query_metric

    def spy(ctx, service, metric, window_minutes=30):
        calls["n"] += 1
        return orig(ctx, service, metric, window_minutes)

    monkeypatch.setattr(tools_api, "query_metric", spy)
    kw = {"ctx": {"dispatch_commander": False}}
    await correlate(graph, storm_alerts("S12"), **kw)
    first = calls["n"]
    assert (
        first == 13
    )  # every component / dependency branch looks at one metric; the zone branch has nothing to look at
    await correlate(graph, storm_alerts("S12"), **kw)  # another thread, same alerts: all 14 branches are cache hits
    assert calls["n"] == first
    await correlate(graph, storm_alerts("S12")[:-3], **kw)  # a different alert set is a different key
    assert calls["n"] > first


# ================================================================================ streaming / shapes
async def test_streaming_progress_notes_series_and_decisions_during_the_fan_out(graph):
    kinds: dict[str, list[str]] = {}
    nodes = []
    async for ns, mode, chunk in graph.astream(
        {"scenario_id": "S12", "alerts": storm_alerts("S12")},
        {"configurable": {"thread_id": "st-1"}},
        stream_mode=["updates", "custom"],
        subgraphs=True,
    ):
        if mode == "custom":
            kinds.setdefault(chunk["kind"], []).append(chunk["key"])
        else:
            nodes.extend(k for k in chunk if not k.startswith("__"))
    assert {"progress", "note", "series", "decision", "handoff"} <= set(kinds)
    assert len(kinds["note"]) >= 14 and len(set(kinds["note"])) == len(
        kinds["note"]
    )  # one deterministic key per candidate
    assert len(kinds["series"]) == 13  # every component / dependency branch plots its metric (not the zone)
    assert nodes[0] == "partition" and nodes[-1] == "report"


def test_series_events_respect_the_60_point_limit_and_context_schema_is_complete():
    g = build_graph()
    props = g.get_context_jsonschema()["properties"]
    for key in (
        "model_profile",
        "approval_policy",
        "scenario_id",
        "as_of",
        "token_delay_ms",
        "breakpoints",
        "memory_enabled",
        "chaos",
        "branch_timeout_s",
        "max_merge_levels",
        "dispatch_commander",
    ):
        assert key in props
    assert "alerts" in g.get_input_jsonschema()["properties"] and "result" in g.get_output_jsonschema()["properties"]
    _, points = evidence.gather(
        "S12",
        "2026-11-06T11:43:30Z",
        {"id": "c", "kind": "component", "root": "edge-gateway", "label": "x"},
        {"edge-gateway": {"worst": {"metric": "error_rate"}}},
    )
    assert 0 < len(points) <= 60


def test_graph_shape_node_defaults_cache_and_names():
    g = build_graph()
    assert g.name == "storm_correlator"
    nodes = g.get_graph().nodes
    assert {
        "partition",
        "analyze_candidate",
        "reduce_level",
        "merge_group",
        "finalize",
        "open_cluster_incident",
        "report",
    } <= set(nodes)
    assert nodes["finalize"].metadata and nodes["finalize"].metadata.get("defer") is True
    assert g.nodes["analyze_candidate"].cache_policy is not None and g.nodes["partition"].cache_policy is None
    assert (
        g.nodes["partition"].retry_policy and g.nodes["analyze_candidate"].retry_policy
    )  # set_node_defaults reached every node
    assert topology.known("orders-db")


async def test_fork_the_alert_set_at_the_first_checkpoint_and_replay_past_the_incident_open_idempotently(graph, client):
    cfg = {"configurable": {"thread_id": "fork-s"}}
    alerts = storm_alerts("S12")
    out = await graph.ainvoke({"scenario_id": "S12", "alerts": alerts}, cfg)
    inc = clusters_of(out)[0]["incident_id"]
    history = [s async for s in graph.aget_state_history(cfg)]
    # replay from just before the incident open: the claim in the ledger / 409 join keeps it to ONE commander run
    before_open = next(s for s in history if s.next == ("open_cluster_incident",))
    again = await graph.ainvoke(None, before_open.config)
    assert clusters_of(again)[0]["incident_id"] == inc and len(commander_runs(client)) == 1
    # fork at the very first checkpoint with only the checkout chain's alerts
    first = next(s for s in history if s.next == ("partition",))
    chain = [a for a in alerts if a["subject"]["component"] in ("checkout-service", "edge-gateway", "web-frontend")]
    fork = await graph.aupdate_state(first.config, {"alerts": chain})
    forked = await graph.ainvoke(None, fork, context={"dispatch_commander": False})
    assert clusters_of(forked)[0]["root"] == "checkout-service" and len(clusters_of(forked)[0]["members"]) == 3


def test_static_topology_matches_the_blast_radius_tool_and_skips_async_edges():
    for svc in ("checkout-service", "orders-db", "notification-service", "sessions-cache"):
        tool = tools_api.blast_radius({"scenario_id": "S01"}, svc)
        static = topology.static_blast_radius(svc)
        assert static["total_affected_if_down"] == tool["total_affected_if_down"], svc
        assert (
            sorted(static["tier0_exposed"]) == sorted(tool["tier0_exposed"])
            and static["revenue_path_exposed"] == tool["revenue_path_exposed"]
        )
        assert static["customer_facing_exposed"] == tool["customer_facing_exposed"]
    # orders-service -> notification-service is fire-and-forget: it never propagates a failure upward
    assert "orders-service" not in topology.transitive_callers("notification-service")
    assert "orders-service" in topology.callers_of("notification-service") and "orders-service" in topology.neighbours(
        "notification-service"
    )
    assert topology.static_blast_radius("nope")["total_affected_if_down"] == 0
