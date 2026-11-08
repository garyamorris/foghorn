"""An eval run is a simulation: its incidents must not spawn real downstream work (postmortems, memory learning).

Found on a real-model run: scoring 16 scenarios left 10 pending postmortem reviews in the Approvals inbox and spent
model money on 20 postmortem runs. The commander flags events from eval threads `simulated`; routing records them.
"""

from __future__ import annotations

import pytest

from foghorn.common import lifecycle as lc
from foghorn.contracts.meta import M_EVAL_RUN_ID
from foghorn.events.routing import DEFAULT_ROUTING_FILE, RoutingTable
from foghorn.flows.incident_commander.nodes import lifecycle as commander_lifecycle


def _state() -> dict:
    return {"scenario_id": "S01", "incident": {"id": "INC-1", "severity": "SEV2", "title": "t"}, "correlation_id": "c"}


def test_eval_threads_mark_resolved_events_simulated():
    with lc.capture() as events:
        commander_lifecycle(_state(), {"metadata": {M_EVAL_RUN_ID: "evr_0001"}}, "incident.resolved", "closed")
    p = events[0]["payload"]
    assert p["simulated"] is True and p["eval_run_id"] == "evr_0001" and p["severity"] == "SEV2"


def test_normal_threads_are_not_marked_simulated():
    with lc.capture() as events:
        commander_lifecycle(_state(), {"metadata": {}}, "incident.resolved", "closed")
    assert "simulated" not in events[0]["payload"]


@pytest.fixture(scope="module")
def table() -> RoutingTable:
    return RoutingTable.load(DEFAULT_ROUTING_FILE)


def _resolved(**payload: object) -> dict:
    return {
        "id": "evt_1",
        "type": "incident.resolved",
        "source": "foghorn.internal",
        "severity": "high",
        "subject": {"component": "checkout-service"},
        "payload": {"severity": "SEV2", "incident_id": "INC-1", **payload},
        "attributes": {},
        "trace": {},
    }


def test_real_resolved_sev2_incident_gets_a_postmortem_but_a_simulated_one_does_not(table):
    from tests.unit.events.test_routing_queues import plan

    real = plan(table, _resolved())
    sim = plan(table, _resolved(simulated=True, eval_run_id="evr_1"))
    assert [s.plan["graph_id"] for s in real if s.action == "dispatch"] == ["postmortem_pipeline"]
    assert not [s for s in sim if s.action == "dispatch"], "a simulated (eval) incident must not start any run"
