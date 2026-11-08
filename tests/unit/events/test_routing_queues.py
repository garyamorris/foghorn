"""routing.yaml rules (first-match-with-continue, priorities, input builders) and the priority queues."""

from __future__ import annotations

from typing import Any

import pytest

from foghorn.contracts.meta import M_CORRELATION_ID, M_EVENT_ID, M_KIND, M_TRIGGER, det_thread_id
from foghorn.events.config import EventsSettings
from foghorn.events.queues import PriorityQueues, QItem
from foghorn.events.routing import (
    DEFAULT_ROUTING_FILE,
    RoutingError,
    RoutingTable,
    StormTracker,
    rule_matches,
    run_context,
)


class Lookup:
    def __init__(
        self, *, open_incident: bool = False, storm: StormTracker | None = None, launch: dict[str, Any] | None = None
    ) -> None:
        self.open_incident = open_incident
        self.storm = storm or StormTracker(120, True)
        self.launch = launch or {}

    def open_incident_on(self, component):
        return {"incident_id": "INC-1"} if self.open_incident else None

    def in_storm(self, event):
        return self.storm.in_storm(event)

    def storm_window_hit(self, event, count_ge, within_s):
        return self.storm.hit(event, count_ge, within_s)

    def storm_alerts(self):
        return self.storm.alerts()

    def launch_context(self, event):
        return self.launch


def ev(type_: str = "alert.fired", *, severity="high", at="2026-11-05T10:00:00Z", component="checkout-service", **kw):
    base = {
        "id": kw.pop("id", "evt_1"),
        "type": type_,
        "source": "alertmanager",
        "severity": severity,
        "occurred_at": at,
        "subject": {"component": component, "environment": "prod"},
        "payload": {},
        "attributes": {},
        "trace": {},
        "correlation_id": None,
        "dedupe_key": None,
    }
    base.update(kw)
    return base


@pytest.fixture(scope="module")
def table() -> RoutingTable:
    return RoutingTable.load(DEFAULT_ROUTING_FILE)


ES = EventsSettings()


def plan(table, event, **lookup):
    return table.plan(event, Lookup(**lookup), ES)


def test_packaged_routing_file_is_valid_and_ends_with_the_fallback(table):
    ids = [r.id for r in table.rules]
    assert ids[-1] == "r-fallback" and len(ids) == len(set(ids))
    assert table.rule("r-deploy-gate") is not None and table.rule("r-deploy-gate").enabled is False  # type: ignore[union-attr]


def test_signals_go_to_the_router_graph_with_severity_priority(table):
    steps = plan(table, ev(severity="critical"))
    assert [s.rule_id for s in steps] == ["r-signal"]
    s = steps[0]
    assert s.action == "dispatch" and s.priority == "P0"
    p = s.plan
    assert p["graph_id"] == "event_router" and p["assistant_id"] == "event_router"
    assert p["input"] == {"event": ev(severity="critical")}
    assert p["thread_id"] == det_thread_id("event_router", "evt_1")
    md = p["thread_metadata"]
    assert md[M_EVENT_ID] == "evt_1" and md[M_KIND] == "triage" and md[M_TRIGGER] == "event"
    assert p["context"]["model_profile"] == "competent" and p["context"]["approval_policy"] == "standard"


@pytest.mark.parametrize(
    ("sev", "prio"), [("critical", "P0"), ("high", "P1"), ("medium", "P2"), ("low", "P3"), ("info", "P3")]
)
def test_priority_from_severity(table, sev, prio):
    assert plan(table, ev(severity=sev))[0].priority == prio


def test_first_match_with_continue_ledger_then_attach_only_when_incident_open(table):
    deploy = ev("deploy.finished", severity="info")
    closed = plan(table, deploy)
    assert [(s.rule_id, s.action) for s in closed] == [
        ("r-change-ledger", "ledger")
    ]  # continue -> attach rule does not match
    opened = plan(table, deploy, open_incident=True)
    assert [(s.rule_id, s.action) for s in opened] == [("r-change-ledger", "ledger"), ("r-change-attach", "dispatch")]
    assert opened[1].priority == "P1" and opened[1].plan["graph_id"] == "event_router"


def test_lifecycle_system_and_unknown_types(table):
    assert [s.action for s in plan(table, ev("incident.opened"))] == ["record"]
    assert [s.action for s in plan(table, ev("run.started"))] == ["record"]
    assert [s.action for s in plan(table, ev("dlq.added"))] == ["record"]
    s = plan(table, ev("something.weird"))[0]
    assert (s.rule_id, s.action, s.reason) == ("r-fallback", "dead_letter", "no_route")
    assert [s.action for s in plan(table, ev("maintenance.window.started"))] == ["update_suppression_table"]


def test_postmortem_only_for_sev1_and_sev2(table):
    sev2 = plan(
        table,
        ev(
            "incident.resolved",
            payload={"incident_id": "INC-7", "severity": "SEV2", "scenario_id": "S10", "thread_id": "t-1"},
        ),
    )
    assert sev2[0].rule_id == "r-postmortem" and sev2[0].priority == "P2"
    assert sev2[0].plan["input"] == {"incident_id": "INC-7", "scenario_id": "S10", "source_thread_id": "t-1"}
    assert sev2[0].plan["thread_id"] == det_thread_id(
        "postmortem_pipeline", "INC-7"
    )  # one postmortem thread per incident
    sev3 = plan(table, ev("incident.resolved", payload={"incident_id": "INC-8", "severity": "SEV3"}))
    assert [s.action for s in sev3] == ["record"]


def test_downstream_builders_match_contract_inputs(table):
    mem = plan(
        table,
        ev(
            "postmortem.published", payload={"incident_id": "INC-7", "scenario_id": "S10", "postmortem": {"title": "t"}}
        ),
    )[0]
    assert mem.plan["graph_id"] == "memory_keeper" and mem.plan["input"] == {
        "mode": "learn",
        "incident_id": "INC-7",
        "scenario_id": "S10",
        "postmortem": {"title": "t"},
    }
    ho = plan(table, ev("system.cron.tick", payload={"job": "shift_handoff"}, at="2026-11-05T16:00:00Z"))[0]
    assert ho.plan["graph_id"] == "shift_handoff" and ho.plan["input"] == {
        "shift_end": "2026-11-05T16:00:00Z",
        "window_hours": 8,
    }
    assert plan(table, ev("system.cron.tick", payload={"job": "other"}))[0].action == "record"
    evl = plan(
        table,
        ev("eval.requested", payload={"eval_run_id": "evr_1", "suite": "core16", "profile": "naive", "concurrency": 2}),
    )[0]
    assert evl.plan["graph_id"] == "eval_runner" and evl.plan["input"] == {
        "suite": "core16",
        "profile": "naive",
        "concurrency": 2,
        "eval_run_id": "evr_1",
    }
    assert (
        evl.plan["thread_id"] == det_thread_id("eval_runner", "evr_1")
        and evl.plan["thread_metadata"]["foghorn_eval_run_id"] == "evr_1"
    )
    chg = plan(
        table,
        ev(
            "change.requested",
            payload={
                "change": {
                    "id": "c1",
                    "kind": "flag",
                    "component": "x",
                    "description": "d",
                    "risk": "low",
                    "requested_by": "me",
                }
            },
        ),
    )[0]
    assert chg.plan["graph_id"] == "change_gate" and chg.plan["input"]["change"]["id"] == "c1"


def test_resume_rule_only_for_ui_sources(table):
    from_ui = plan(table, ev("approval.decided", source="ui", payload={"approval_id": "a"}))
    assert from_ui[0].action == "resume_thread" and from_ui[0].priority == "P0"
    internal = plan(table, ev("approval.decided", source="foghorn.internal", payload={"approval_id": "a"}))
    assert internal[0].action == "record"  # the dashboard's own decisions never resume twice


def test_storm_detector_declares_once_then_folds_members():
    t = RoutingTable.load(DEFAULT_ROUTING_FILE)
    look = Lookup(storm=StormTracker(120, True))
    steps_per_event = []
    for i in range(10):
        e = ev(id=f"evt_{i}", at=f"2026-11-05T10:00:{i:02d}Z")
        steps_per_event.append([(s.rule_id, s.action) for s in t.plan(e, look, ES)])
    assert all(s == [("r-signal", "dispatch")] for s in steps_per_event[:7])
    assert steps_per_event[7] == [
        ("r-storm", "dispatch"),
        ("r-signal", "dispatch"),
    ]  # 8th alert in 60 s: storm + normal triage
    assert steps_per_event[8] == [("r-storm-member", "suppress")] == steps_per_event[9]
    storm_plan = t.plan(ev(id="evt_x", at="2026-11-05T10:05:00Z"), look, ES)  # cooldown over -> normal again
    assert storm_plan[0].rule_id == "r-signal"


def test_a_continuing_burst_stays_one_storm_until_it_goes_quiet():
    t = RoutingTable.load(DEFAULT_ROUTING_FILE)
    look = Lookup(storm=StormTracker(120, True))
    declared = 0
    for i in range(60):  # 60 alerts, one every 5 s (5 minutes): far longer than one 120 s cooldown
        e = ev(id=f"e{i}", at=f"2026-11-05T10:{(i * 5) // 60:02d}:{(i * 5) % 60:02d}Z")
        declared += any(s.rule_id == "r-storm" for s in t.plan(e, look, ES))
    assert declared == 1
    quiet = t.plan(ev(id="late", at="2026-11-05T10:30:00Z"), look, ES)  # 25 quiet minutes later: back to normal
    assert [s.rule_id for s in quiet] == ["r-signal"]


def test_storm_input_carries_the_window_of_alerts():
    t = RoutingTable.load(DEFAULT_ROUTING_FILE)
    look = Lookup(storm=StormTracker(120, True))
    last = None
    for i in range(8):
        last = t.plan(ev(id=f"e{i}", at=f"2026-11-05T10:00:{i:02d}Z", trace={"scenario_id": "S12"}), look, ES)
    storm = next(s for s in last if s.rule_id == "r-storm")  # type: ignore[union-attr]
    assert storm.priority == "P0" and storm.plan["graph_id"] == "storm_correlator"
    assert storm.plan["input"]["scenario_id"] == "S12" and len(storm.plan["input"]["alerts"]) == 8


def test_run_context_prefers_launch_values_and_drops_empty_ones():
    ctx = run_context(
        ev(trace={"scenario_id": "S10"}),
        {"model_profile": "naive", "breakpoints": ["approval_gate"], "memory_enabled": False},
        ES,
    )
    assert (
        ctx["model_profile"] == "naive" and ctx["breakpoints"] == ["approval_gate"] and ctx["memory_enabled"] is False
    )
    assert ctx["scenario_id"] == "S10" and "chaos" not in ctx and ctx["approval_policy"] == "standard"


def test_ambient_events_run_statelessly_and_chaos_is_a_dispatcher_concern(table):
    s = plan(table, ev(source="replay:stream_quiet_hour", attributes={"ambient": True, "chaos": "poison"}))[0]
    assert s.plan["on_completion"] == "delete" and s.plan["thread_metadata"][M_TRIGGER] == "replay"
    assert s.plan["chaos"] == "poison" and "chaos" not in s.plan["context"]
    assert M_CORRELATION_ID not in s.plan["thread_metadata"]  # None values are dropped


def test_rule_matching_primitives():
    t = RoutingTable.from_dict(
        {
            "rules": [
                {
                    "id": "r",
                    "when": {"payload.a.b": [1, 2], "component": "svc-*", "severity": ["high"]},
                    "action": "record",
                }
            ]
        }
    )
    r = t.rules[0]
    good = ev(component="svc-9", payload={"a": {"b": 2}})
    assert rule_matches(r, good, Lookup())
    assert not rule_matches(r, ev(component="other", payload={"a": {"b": 2}}), Lookup())
    assert not rule_matches(r, ev(component="svc-9", payload={"a": {"b": 3}}), Lookup())
    with pytest.raises(RoutingError):
        rule_matches(
            RoutingTable.from_dict({"rules": [{"id": "x", "when": {"nope": 1}, "action": "record"}]}).rules[0],
            good,
            Lookup(),
        )


@pytest.mark.parametrize(
    "doc",
    [
        {"version": 2, "rules": [{"id": "a", "action": "record"}]},
        {"rules": []},
        {"rules": [{"id": "a", "action": "record"}, {"id": "a", "action": "record"}]},
        {"rules": [{"id": "a"}]},
        {"rules": [{"id": "a", "action": "record", "route": {"graph": "event_router", "input": "router"}}]},
        {"rules": [{"id": "a", "action": "explode"}]},
        {"rules": [{"id": "a", "route": {"graph": "nope", "input": "router"}}]},
        {"rules": [{"id": "a", "route": {"graph": "event_router", "input": "nope"}}]},
        {"rules": [{"id": "a", "route": {"graph": "event_router", "input": "router", "thread": "weird"}}]},
        {"rules": [{"id": "a", "action": "record", "priority": "P9"}]},
    ],
)
def test_routing_table_validation(doc):
    with pytest.raises(RoutingError):
        RoutingTable.from_dict(doc)


# ------------------------------------------------------------------------------------------------------- queues
def item(i, prio, graph="g", thread=None, enq=0.0):
    return QItem(dispatch_id=f"d{i}", event_id=f"e{i}", priority=prio, graph_id=graph, thread_id=thread, enqueued=enq)


CAPS = {"P0": 500, "P1": 500, "P2": 1000, "P3": 200}


def test_weighted_fair_drain_is_8_4_2_1_and_never_starves_p3():
    q = PriorityQueues(CAPS, clock=lambda: 0.0)
    for i in range(40):
        for p in ("P0", "P1", "P2", "P3"):
            q.put(item(f"{p}{i}", p))
    first_cycle = [q.pop().priority for _ in range(15)]  # type: ignore[union-attr]
    assert (
        first_cycle.count("P0") == 8
        and first_cycle.count("P1") == 4
        and first_cycle.count("P2") == 2
        and first_cycle.count("P3") == 1
    )
    assert first_cycle[:8] == ["P0"] * 8  # strict order inside a round


def test_pop_respects_eligibility_and_returns_none_when_blocked():
    q = PriorityQueues(CAPS, clock=lambda: 0.0)
    q.put(item(1, "P0", graph="eval_runner"))
    q.put(item(2, "P3", graph="incident_commander"))
    got = q.pop(lambda i: i.graph_id != "eval_runner")
    assert got is not None and got.dispatch_id == "d2"
    assert q.pop(lambda i: False) is None and q.total_depth == 1


def test_aging_promotes_waiting_items_one_class_at_a_time():
    now = [0.0]
    q = PriorityQueues(CAPS, aging_s=30, clock=lambda: now[0])
    q.put(item("old", "P3", enq=0.0))
    for i in range(3):
        q.put(item(f"n{i}", "P1", enq=100.0))
    now[0] = 100.0
    q._promote()  # noqa: SLF001
    assert q.depth()["P3"] == 0 and q.depth()["P2"] == 1 and q.promoted_total == 1
    assert q.oldest_wait_s() == 100.0


def test_backpressure_hysteresis_and_would_exceed():
    q = PriorityQueues({"P0": 2, "P1": 2, "P2": 4, "P3": 2}, high_water=0.8, low_water=0.5, clock=lambda: 0.0)
    n = 0
    while not q.bp_active:
        q.put(item(n, "P2" if n % 2 else "P0"))
        n += 1
    assert q.total_depth >= 8 and q.would_exceed("P2") and q.would_exceed("P3")
    assert not q.would_exceed("P0") and not q.would_exceed("P1")  # reserved headroom
    assert not q.clear_event.is_set()
    while q.total_depth > 6:
        assert q.bp_active  # hysteresis: still throttling between low (0.5) and high (0.8) water
        q.pop()
    assert q.total_depth == 6 and q.bp_active
    q.pop()  # 5/10 == low water -> clears
    assert not q.bp_active and q.clear_event.is_set()


def test_hard_cap_sheds_oldest_lowest_priority_first_and_p0_last():
    q = PriorityQueues({"P0": 2, "P1": 2, "P2": 2, "P3": 2}, clock=lambda: 0.0)
    for i in range(2):
        for p in ("P0", "P1", "P2", "P3"):
            q.put(item(f"{p}-{i}", p, enq=float(i)))
    assert q.total_depth == 8
    shed = q.put(item("new-p0", "P0"))
    assert [v.dispatch_id for v in shed] == ["dP3-0"]  # oldest of the LOWEST class goes first
    shed = q.put(item("new-p1", "P1"))
    assert [v.dispatch_id for v in shed] == ["dP3-1"]
    shed = q.put(item("new-p0b", "P0"))
    assert [v.dispatch_id for v in shed] == ["dP2-0"]
    assert q.depth()["P0"] == 4  # P0 grows by borrowing, it is never the victim while lower classes hold work
    assert q.shed_total == 3


def test_incoming_low_priority_is_itself_shed_when_everything_else_is_more_important():
    q = PriorityQueues({"P0": 2, "P1": 1, "P2": 1, "P3": 1}, clock=lambda: 0.0)
    for i in range(3):
        q.put(item(f"p0-{i}", "P0"))  # P0 borrows past its nominal cap
    q.put(item("p1", "P1"))
    q.put(item("p2", "P2"))
    assert q.total_depth == q.total_cap == 5 and q.depth()["P3"] == 0
    shed = q.put(item("late-p3", "P3"))
    assert [v.dispatch_id for v in shed] == [
        "dlate-p3"
    ] and q.total_depth == 5  # the newcomer, never a more important item


def test_low_classes_are_individually_bounded():
    q = PriorityQueues({"P0": 10, "P1": 10, "P2": 2, "P3": 2}, clock=lambda: 0.0)
    shed: list[QItem] = []
    for i in range(4):
        shed += q.put(item(i, "P3", enq=float(i)))
    assert [v.dispatch_id for v in shed] == ["d0", "d1"] and q.depth()["P3"] == 2


def test_remove_and_reset():
    q = PriorityQueues(CAPS, clock=lambda: 0.0)
    q.put(item(1, "P1"))
    assert q.remove("d1") and not q.remove("d1")
    q.put(item(2, "P2"))
    q.reset()
    assert q.total_depth == 0 and q.snapshot()["backpressure"] is False
