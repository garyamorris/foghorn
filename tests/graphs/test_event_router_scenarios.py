"""event_router over the whole data layer: every scenario, the 400 labelled triage cases and the three replay streams.

The oracle is the data layer's own ``reference_triage`` (the specification of the triage policy) driven by a ROLLING
context that applies the router's documented bookkeeping: an opened incident stays joinable for 60 virtual minutes and
its reach grows by one ring of joined neighbours; a recognised storm folds further openings into the correlator run.
TEST code only may read ground truth (``reveal_truth=True``); graph code never does.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from foghorn.contracts.meta import det_thread_id
from foghorn.data import api as data_api
from foghorn.data import events as ev
from foghorn.data.triage_rules import reference_triage
from foghorn.data.util import to_epoch
from foghorn.flows.event_router import rules, topology
from foghorn.flows.event_router.build import build_graph
from tests.graphs.support_ingress import install_fake_client, uninstall_fake_client

SCENARIOS = [f"S{i:02d}" for i in range(1, 17)]
ROUTER_TYPES = ("alert.fired", "slo.burn_rate", "healthcheck.failed", "customer.report")


@pytest.fixture
def client():
    c = install_fake_client()
    yield c
    uninstall_fake_client()


class Oracle:
    """reference_triage + the router's rolling bookkeeping (see module docstring)."""

    def __init__(self) -> None:
        self.incidents: list[dict[str, Any]] = []
        self.recent: dict[str, tuple[int, str, str | None]] = {}
        self.alert_times: dict[str, int] = {}
        self.windows: list[dict[str, Any]] = []
        self.storm_until = -1
        self.opened = 0

    def maintenance(self, e: dict[str, Any]) -> None:
        if e["type"] == "maintenance.window.started":
            p = e["payload"]
            self.windows.append({"component": e["subject"]["component"], "start": p["starts_at"], "end": p["ends_at"]})

    def step(self, e: dict[str, Any]) -> tuple[dict[str, Any], str]:
        ts, comp = to_epoch(e["occurred_at"]), e["subject"]["component"]
        self.incidents = [i for i in self.incidents if i["until"] > ts]
        ctx = {
            "open_incidents": [
                {"id": i["id"], "components": sorted({*i["components"], *i["joined"]}), "severity": i["severity"]}
                for i in self.incidents
            ],
            "maintenance_windows": [w for w in self.windows if to_epoch(w["start"]) <= ts],
            "recent_alert_keys": [
                {"key": k, "age_s": ts - t, "event_id": i, "component": c}
                for k, (t, i, c) in self.recent.items()
                if ts - t < 900
            ],
        }
        exp = reference_triage(e, ctx)
        route = rules.DECISION_ROUTE[exp["decision"]]
        in_signal = e["type"] in rules.STORM_SIGNAL_TYPES and exp["decision"] in (
            "open_incident",
            "escalate_human",
            "attach",
        )
        if e["type"] == "alert.fired":
            self.alert_times[e["dedupe_key"]] = ts
        burst = {k for k, t in self.alert_times.items() if 0 <= ts - t <= 60}
        if in_signal:
            if ts >= self.storm_until and e["type"] == "alert.fired" and len(burst) >= 8:
                route, self.storm_until = "escalate_storm", ts + 120
            elif ts < self.storm_until:
                self.storm_until = max(self.storm_until, ts + 120)
                if route == "open_incident":
                    route = "ignore"
        if route == "open_incident":
            self.opened += 1
            self.incidents.append(
                {
                    "id": f"I{self.opened}",
                    "components": [comp],
                    "joined": set(),
                    "severity": exp["severity"],
                    "until": ts + 3600,
                }
            )
        elif exp["decision"] == "attach":
            inc = next(i for i in self.incidents if i["id"] == exp["attach_to"])
            if comp and any(comp in topology.neighbours(o) for o in inc["components"]):
                inc["joined"].add(comp)
        if e.get("dedupe_key") and e["type"] in ROUTER_TYPES:
            first = (
                self.recent[e["dedupe_key"]][1]
                if (e["dedupe_key"] in self.recent and exp["decision"] == "dedupe")
                else e["id"]
            )
            self.recent[e["dedupe_key"]] = (ts, first, comp)
        return exp, route


async def replay(events: list[dict[str, Any]], oracle: Oracle, *, ctx: dict[str, Any] | None = None):
    store = InMemoryStore()
    graph = build_graph(checkpointer=InMemorySaver(), store=store)
    outs = []
    for e in events:
        if e["type"] in ("maintenance.window.started", "maintenance.window.ended"):
            oracle.maintenance(e)
            expected = None
        elif e["type"] in ROUTER_TYPES:
            expected = oracle.step(e)
        else:
            expected = None
        out = await graph.ainvoke({"event": e}, {"configurable": {"thread_id": f"t-{e['id']}"}}, context=ctx)
        outs.append((e, expected, out))
    return outs


def router_events(sid: str) -> list[dict[str, Any]]:
    keep = (*ROUTER_TYPES, "alert.resolved", "maintenance.window.started", "maintenance.window.ended")
    return [e for e in data_api.scenario_events(sid) if e["type"] in keep]


@pytest.mark.parametrize("sid", SCENARIOS)
async def test_scenario_decisions_follow_the_reference_policy_and_start_one_pinned_commander_per_incident(sid, client):
    outs = await replay(router_events(sid), Oracle())
    opened: dict[str, dict[str, Any]] = {}
    for e, expected, out in outs:
        if expected is None:
            continue
        exp, exp_route = expected
        t = out["triage"]
        assert t["decision"] == exp["decision"], f"{sid} {e['id']} {e['type']}: {t['decision']} != {exp['decision']}"
        assert t["route"] == exp_route, f"{sid} {e['id']}: route {t['route']} != {exp_route}"
        if exp["decision"] in ("open_incident", "escalate_human"):
            assert t["severity"] == exp["severity"]
        if (out.get("incident") or {}).get("opened"):
            opened[out["incident"]["id"]] = out["incident"]
    # exactly one commander run per incident the router opened, on the pinned thread, with the right metadata
    runs = client.runs_for("incident_commander")
    assert len(runs) == len(opened) and not client.runs.rejected
    assert {r["thread_id"] for r in runs} == {det_thread_id("incident_commander", i) for i in opened}
    for r in runs:
        inc = r["input"]["incident"]["id"]
        assert r["thread_id"] == det_thread_id("incident_commander", inc) and r["input"]["scenario_id"] == sid
        md = client.threads.created[r["thread_id"]]["metadata"]
        assert (
            md["foghorn_incident_id"] == inc
            and md["foghorn_scenario_id"] == sid
            and md["foghorn_graph"] == "incident_commander"
        )
        assert r["context"]["scenario_id"] == sid and r["context"]["model_profile"] == "competent"
    # the unconditional join semantics: nothing but an opening alert ever starts a commander
    joins = [out for _, _, out in outs if out["route"] == "join_incident"]
    assert all(out["result"]["child_threads"] == [] for out in joins)


async def test_scenario_incident_counts_for_the_headline_cases(client):
    counts = {}
    for sid in ("S01", "S04", "S12", "S13"):
        client.runs.created.clear()
        client.runs.rejected.clear()
        outs = await replay(router_events(sid), Oracle())
        counts[sid] = (
            len([o for *_, o in outs if (o.get("incident") or {}).get("opened")]),
            len([o for *_, o in outs if o["route"] == "escalate_storm"]),
        )
    assert counts == {"S01": (1, 0), "S04": (0, 0), "S12": (1, 1), "S13": (1, 0)}


async def test_s04_is_all_noise_flapping_probes_and_maintenance_alerts(client):
    outs = await replay(router_events("S04"), Oracle())
    decisions = [o["triage"]["decision"] for *_, o in outs]
    assert set(decisions) <= {"suppress_flapping", "suppress_maintenance", "record"}
    assert decisions.count("suppress_flapping") == 2 and decisions.count("suppress_maintenance") == 2
    assert not client.runs.created


# ----------------------------------------------------------------------------------------- D8 triage cases
def _case_event(c: dict[str, Any]) -> dict[str, Any]:
    a = c["alert"]
    payload = {k: v for k, v in a.items() if k in ("rule", "value", "threshold", "text")}
    return ev.envelope(
        id=c["case_id"],
        type=a["type"],
        source="alertmanager",
        severity=a["severity"],
        occurred_at=a["fired_at"],
        component=a["component"],
        payload=payload,
        dedupe_key=a["dedupe_key"],
    )


def _cases() -> list[dict[str, Any]]:
    return data_api.read_dataset("triage_cases", 0, 500, reveal_truth=True)["records"]


def test_pure_rules_score_100_percent_on_the_400_labelled_triage_cases():
    cases = _cases()
    assert len(cases) == 400
    for c in cases:
        r = rules.decide(_case_event(c), c["context"])
        exp = c["expected"]
        got = {k: r[k] for k in ("decision", "severity", "reason_code", "attach_to", "dedupe_of")}
        assert got == {k: exp[k] for k in got}, c["case_id"]


async def test_the_graph_reproduces_the_triage_cases_with_the_context_seeded_into_the_ledger(client):
    """Same 400 cases through the real graph: the Store ledger is seeded with each case's context."""
    from foghorn.flows.event_router.ledger import Ledger

    wrong = []
    for c in _cases()[:120]:  # 120 spread across every decision class keeps the suite fast
        store = InMemoryStore()
        lg = Ledger(store)
        event = _case_event(c)
        now = to_epoch(event["occurred_at"])
        ctx = c["context"]
        for i in ctx["open_incidents"]:
            await lg.put_incident(
                {
                    "id": i["id"],
                    "components": i["components"],
                    "severity": i["severity"],
                    "opened_at": now - 60,
                    "expires_at": now + 600,
                    "status": "open",
                    "event_id": "seed",
                }
            )
        for n, w in enumerate(ctx["maintenance_windows"]):
            await lg.put_maintenance({"id": f"w{n}", **w})
        for rk in ctx["recent_alert_keys"]:
            await lg._put(
                f"dk:{rk['key']}",
                {
                    "kind": "seen",
                    "key": rk["key"],
                    "t": now - rk["age_s"],
                    "event_id": rk["event_id"],
                    "component": rk["component"],
                    "type": "alert.fired",
                    "occurrences": 1,
                    "event": None,
                },
            )
        graph = build_graph(checkpointer=InMemorySaver(), store=store)
        out = await graph.ainvoke(
            {"event": event}, {"configurable": {"thread_id": c["case_id"]}}, context={"dispatch_commander": False}
        )
        exp = c["expected"]
        got = (out["triage"]["decision"], out["triage"]["severity"], out["triage"].get("attach_to"))
        if got != (exp["decision"], exp["severity"], exp["attach_to"]):
            wrong.append((c["case_id"], got, exp))
    assert not wrong, wrong[:3]


# ---------------------------------------------------------------------------------------------- streams
def _stream(name: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    labels = {lab["id"]: lab for lab in data_api.iter_stream_labels(name)}
    keep = [
        e
        for e in data_api.iter_stream(name)
        if labels[e["id"]]["expected_route"] in ("triage", "storm", "maintenance", "resolution")
        and not labels[e["id"]]["poison"]
    ]
    return keep, labels


@pytest.mark.parametrize("name", ["stream_quiet_hour", "stream_workday", "stream_storm"])
async def test_replay_streams_through_the_router_match_the_rolling_oracle(name, client):
    events, labels = _stream(name)
    outs = await replay(events, Oracle(), ctx={"dispatch_commander": False})
    checked = storms = 0
    for e, expected, out in outs:
        if expected is None:
            continue
        exp, exp_route = expected
        assert out["triage"]["decision"] == exp["decision"], f"{name} {e['id']}"
        assert out["route"] == exp_route, f"{name} {e['id']}"
        checked += 1
        storms += out["route"] == "escalate_storm"
        # the label sidecar agrees on everything the router does not deliberately do differently
        lab = labels[e["id"]]
        if lab["expected_decision"] == "dedupe":
            assert out["triage"]["decision"] == "dedupe" and out["result"]["status"] == "duplicate"
        if lab["expected_decision"] == "suppress_maintenance":
            assert out["triage"]["decision"] == "suppress_maintenance"
    assert checked > 100
    assert storms == (1 if name == "stream_storm" else 0)
    if name == "stream_quiet_hour":
        assert {o["triage"]["decision"] for *_, o in outs if o["triage"]["decision"] != "record"} <= {
            "dedupe",
            "ignore_info",
            "suppress_flapping",
            "suppress_maintenance",
            "escalate_human",
            "open_incident",
            "attach",
        }


async def test_stream_storm_collapses_the_flood_into_one_storm_and_few_incidents(client):
    events, _ = _stream("stream_storm")
    outs = await replay(events, Oracle())
    routes = [o["route"] for *_, o in outs]
    assert routes.count("escalate_storm") == 1 and routes.count("ignore") > 400  # 472 semantic duplicates
    assert len(client.runs_for("storm_correlator")) == 1 and len(client.runs_for("incident_commander")) <= 2
    storm_run = client.runs_for("storm_correlator")[0]
    assert 8 <= len(storm_run["input"]["alerts"]) <= 60
