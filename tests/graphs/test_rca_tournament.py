"""The RCA tournament: Send map-reduce over hypotheses, standalone and as the commander's subgraph."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from foghorn.data import api as data
from foghorn.flows.rca_tournament import scoring as sc
from foghorn.flows.rca_tournament.build import RcaContext, build_graph
from foghorn.flows.rca_tournament.facts import Facts
from foghorn.flows.rca_tournament.gather import gather_evidence

pytestmark = pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
SCENARIOS = [f"S{i:02d}" for i in range(1, 17)]


def graph() -> Any:
    return build_graph(checkpointer=InMemorySaver())


@pytest.mark.parametrize("sid", SCENARIOS)
async def test_standalone_tournament_finds_the_root_cause(sid):
    g = graph()
    out = await g.ainvoke({"scenario_id": sid}, {"configurable": {"thread_id": f"rca-{sid}"}}, context=RcaContext())
    t = data.get_truth(sid)["root_cause"]
    top = out["hypotheses"][0]
    assert out["result"]["graph_id"] == "rca_tournament" and out["result"]["status"] == "ok"
    if (
        t is not None and t["category"] == "telemetry_gap"
    ):  # S13: nobody alerts and the telemetry is gone; one of the two blind services
        assert top["category"] == "telemetry_gap" and top["component"] in {"payments-service", "orders-service"}
    elif t is None:  # S04, planned maintenance noise
        assert (top["component"], top["category"]) == ("orders-db", "planned_maintenance")
    else:
        assert (top["component"], top["category"]) == (t["component"], t["category"]), [
            h["label"] for h in out["hypotheses"][:3]
        ]
    assert top["rank"] == 1 and [h["rank"] for h in out["hypotheses"]] == list(range(1, len(out["hypotheses"]) + 1))
    assert all(h["id"] and h["for"] is not None and h["against"] is not None for h in out["hypotheses"])


async def test_red_herrings_are_scored_and_rejected():
    out = await graph().ainvoke(
        {"scenario_id": "S10"}, {"configurable": {"thread_id": "rca-s10"}}, context=RcaContext()
    )
    by = {h["id"]: h for h in out["hypotheses"]}
    reco = by["h:recommendation-service:bad_deploy"]
    assert reco["score"] < 0.1 and any("normal" in a or "not on" in a for a in reco["against"]), (
        "the innocent deploy was examined"
    )
    assert any(rh["component"] == "recommendation-service" for rh in out["red_herrings"])
    assert out["hypotheses"][0]["id"] == "h:pricing-service:config_change"


async def test_naive_profile_blames_the_nearest_change():
    out = await graph().ainvoke(
        {"scenario_id": "S10"}, {"configurable": {"thread_id": "rca-naive"}}, context=RcaContext(model_profile="naive")
    )
    assert (
        out["hypotheses"][0]["component"] == "recommendation-service"
        and out["hypotheses"][0]["category"] == "bad_deploy"
    )
    assert out["red_herrings"] == []


async def test_named_candidates_win_a_tie_between_blind_components():
    out = await graph().ainvoke(
        {"scenario_id": "S13", "candidates": ["payments-service"]},
        {"configurable": {"thread_id": "rca-s13"}},
        context=RcaContext(),
    )
    assert (
        out["hypotheses"][0]["component"] == "payments-service" and out["hypotheses"][0]["category"] == "telemetry_gap"
    )


async def test_candidates_and_evidence_can_be_supplied():
    ev, as_of, calls = gather_evidence("S05", None)
    assert calls > 5 and as_of > data.default_as_of("S05")
    out = await graph().ainvoke(
        {"scenario_id": "S05", "as_of": as_of, "evidence": ev, "candidates": ["orders-db"]},
        {"configurable": {"thread_id": "rca-given"}},
        context=RcaContext(),
    )
    assert out["hypotheses"][0]["component"] == "orders-db"


async def test_streams_hypothesis_updates_from_parallel_workers():
    g = graph()
    events: list[dict[str, Any]] = []
    steps: list[str] = []
    async for mode, payload in g.astream(
        {"scenario_id": "S10"},
        {"configurable": {"thread_id": "rca-stream"}},
        context=RcaContext(),
        stream_mode=["custom", "updates"],
    ):
        if mode == "custom":
            events.append(payload)
        else:
            steps += list(payload)
    hyp = [e for e in events if e["kind"] == "hypothesis.update"]
    assert len(hyp) >= 3 and all(
        e["agent"] == "rca_tournament" and "for" in e["data"] and "against" in e["data"] for e in hyp
    )
    assert len({e["key"] for e in hyp}) == len(hyp), "one deterministic key per hypothesis"
    assert steps.count("score_hypothesis") == len(hyp) and steps.index("rank") > max(
        i for i, s in enumerate(steps) if s == "score_hypothesis"
    )
    assert {e["kind"] for e in events} >= {"progress", "note", "decision"}


def test_graph_shape_defer_and_send():
    g = graph()
    drawn = g.get_graph()
    assert {"gather", "plan", "score_hypothesis", "rank", "report"} <= set(drawn.nodes)
    assert drawn.nodes["rank"].metadata and drawn.nodes["rank"].metadata["kind"] == "reduce"
    assert ("plan", "score_hypothesis") in {(e.source, e.target) for e in drawn.edges}
    embedded = build_graph(embedded=True)
    assert "report" not in embedded.get_graph().nodes and set(embedded.get_output_jsonschema()["properties"]) >= {
        "hypotheses",
        "red_herrings",
    }
    assert set(g.get_output_jsonschema()["properties"]) == {"result", "hypotheses", "red_herrings"}
    assert "scenario_id" in g.get_input_jsonschema()["properties"]
    props = g.get_context_jsonschema()["properties"]
    for k in (
        "scenario_id",
        "as_of",
        "model_profile",
        "approval_policy",
        "breakpoints",
        "memory_enabled",
        "chaos",
        "token_delay_ms",
    ):
        assert k in props


async def test_per_send_timeout_leaves_a_ranking(monkeypatch):
    """A worker that times out must not sink the tournament: the deferred reducer ranks whatever answered."""
    from foghorn.flows.rca_tournament import nodes

    real = nodes.sc.score_hypothesis

    def slow(f: Facts, component: str, category: str) -> sc.Scored:
        if component == "catalog-service":
            import time

            time.sleep(0.01)
        return real(f, component, category)

    monkeypatch.setattr(nodes.sc, "score_hypothesis", slow)
    out = await asyncio.wait_for(
        graph().ainvoke({"scenario_id": "S05"}, {"configurable": {"thread_id": "rca-slow"}}, context=RcaContext()), 30
    )
    assert out["hypotheses"][0]["component"] == "orders-db"


def test_scoring_rules_are_explainable():
    from tests.graphs.test_incident_commander_units import facts_for

    f = facts_for("S01")
    top = sc.score_hypothesis(f, "checkout-service", "bad_deploy")
    wrong = sc.score_hypothesis(f, "checkout-service", "cert_expiry")
    assert top.score > 0.8 > wrong.score
    assert any(r and r.startswith("change:chg-check") for _w, _t, r in top.for_), (
        "the cause is cited by its evidence ref"
    )
    assert top.as_hypothesis()["label"] == "bad_deploy on checkout-service"
    assert sc.applicable("orders-db", "db_pool_exhaustion") and not sc.applicable("orders-db", "bad_deploy")
    assert (
        sc.role_of("sessions-cache") == "cache"
        and sc.role_of("event-bus") == "queue"
        and sc.role_of("payment-provider") == "external"
    )
