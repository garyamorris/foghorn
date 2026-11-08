"""Studio guides of the three ingress graphs: parse like the server does, every sample input runs, the dispatcher's input
builders feed the graphs, and graph code never reaches for ground truth, the wall clock or randomness."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from foghorn.events.routing import INPUT_BUILDERS, BuildCtx
from foghorn.flows.change_gate.build import build_graph as build_gate
from foghorn.flows.event_router.build import build_graph as build_router
from foghorn.flows.storm_correlator.build import build_graph as build_storm
from foghorn.server.catalog import guide_entry
from tests.graphs.support_ingress import install_fake_client, make_event, scenario_event, uninstall_fake_client

FLOWS = Path(__file__).resolve().parents[2] / "src" / "foghorn" / "flows"
MINE = ("event_router", "storm_correlator", "change_gate")


@pytest.fixture
def client():
    c = install_fake_client()
    yield c
    uninstall_fake_client()


@pytest.mark.parametrize("gid", MINE)
def test_guide_front_matter_parses_with_the_server_loader(gid):
    entry, warning = guide_entry(gid, FLOWS)
    assert warning is None and entry["has_guide"] and entry["graph_id"] == gid
    assert entry["title"] and len(entry["summary"]) > 40 and len(entry["features"]) >= 5
    assert len(entry["studio_steps"]) >= 5 and len(entry["sample_inputs"]) >= 2 and "## " in entry["body"]
    assert all(isinstance(s["input"], dict) and s["name"] for s in entry["sample_inputs"])


async def test_every_router_sample_runs_and_behaves_as_documented(client):
    samples = guide_entry("event_router", FLOWS)[0]["sample_inputs"]
    g = build_router(checkpointer=InMemorySaver(), store=InMemoryStore())
    outs = []
    for i, s in enumerate(samples):
        outs.append(await g.ainvoke(s["input"], {"configurable": {"thread_id": f"g-{i}"}}))
    routes = [o["route"] for o in outs]
    assert routes == ["open_incident", "join_incident", "ignore", "ignore", "open_incident", "reject"]
    assert [o["triage"].get("decision") for o in outs][2:4] == ["suppress_maintenance", "suppress_flapping"]
    assert outs[4]["triage"]["via"] == "llm" and outs[5]["result"]["status"] == "failed"
    assert (
        len(client.runs_for("incident_commander")) == 2
    )  # S01 alert + the fallback-approved unknown component (S01 trace)


async def test_every_storm_sample_runs_and_clusters_as_documented(client):
    samples = guide_entry("storm_correlator", FLOWS)[0]["sample_inputs"]
    g = build_storm(checkpointer=InMemorySaver(), store=InMemoryStore())
    roots = []
    for i, s in enumerate(samples):
        out = await g.ainvoke(s["input"], {"configurable": {"thread_id": f"g-{i}"}})
        clusters = next(a for a in out["result"]["artifacts"] if a["name"] == "clusters")["data"]
        roots.append([c["root"] for c in clusters])
    assert roots == [["zone-b"], ["checkout-service"]]


async def test_every_change_gate_sample_reaches_its_documented_first_control_point():
    samples = guide_entry("change_gate", FLOWS)[0]["sample_inputs"]
    expected = ["approval", "question", "question", "approval", "break_glass", None]
    assert len(samples) == len(expected)
    for i, (s, first) in enumerate(zip(samples, expected, strict=True)):
        g = build_gate(checkpointer=InMemorySaver(), store=InMemoryStore())
        out = await g.ainvoke(s["input"], {"configurable": {"thread_id": f"g-{i}"}}, version="v2")
        got = out.interrupts[0].value["kind"] if out.interrupts else None
        assert got == first, s["name"]
        if first is None:
            assert out.value["status"] == "applied"  # the auto-approved flag flip


async def test_the_dispatchers_input_builders_feed_the_graphs(client):
    ev = scenario_event("S01", "evt_S01_0013")
    router_in = INPUT_BUILDERS["router"](BuildCtx(event=ev))
    out = await build_router(checkpointer=InMemorySaver(), store=InMemoryStore()).ainvoke(
        router_in, {"configurable": {"thread_id": "b1"}}
    )
    assert out["route"] == "open_incident"

    alerts = [
        make_event(f"a{i}", component=c, scenario_id="S01")
        for i, c in enumerate(["checkout-service", "edge-gateway", "web-frontend"])
    ]
    storm_in = INPUT_BUILDERS["storm"](BuildCtx(event=alerts[0], storm_alerts=alerts))
    sout = await build_storm(checkpointer=InMemorySaver(), store=InMemoryStore()).ainvoke(
        storm_in, {"configurable": {"thread_id": "b2"}}
    )
    assert sout["result"]["metrics"]["clusters"] == 1

    # a bare `change.requested` event (no `change` object) still yields a runnable ChangeGateInput
    req = make_event(
        "chg-evt",
        type="change.requested",
        severity="info",
        component="pricing-service",
        rule=None,
        payload={"description": "ramp flag to 50%", "kind": "flag", "risk": "medium", "requested_by": "a.dev"},
    )
    gate_in = INPUT_BUILDERS["change_gate"](BuildCtx(event=req))
    gout = await build_gate(checkpointer=InMemorySaver(), store=InMemoryStore()).ainvoke(
        gate_in, {"configurable": {"thread_id": "b3"}}, version="v2"
    )
    assert gout.interrupts and gout.interrupts[0].value["proposed"]["change"]["id"] == "chg-evt"


PATTERNS = {
    "ground truth": re.compile(r"get_truth|reveal_truth|\.truth\b|data\.truth|data import truth"),
    "wall clock / randomness": re.compile(
        r"datetime\.now|time\.time\(|time\.sleep|uuid4|import random|from random|clock\.now"
    ),
    "blocking http": re.compile(r"requests\.|urllib|httpx\.(get|post)\("),
}


@pytest.mark.parametrize("what", sorted(PATTERNS))
def test_graph_code_of_the_ingress_flows_is_free_of_truth_clock_and_randomness(what):
    offenders = []
    for name in MINE:
        for path in sorted((FLOWS / name).glob("*.py")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if PATTERNS[what].search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, offenders


def test_the_three_graph_files_are_thin_importers():
    for name in MINE:
        text = (FLOWS.parent / "graphs" / f"{name}.py").read_text()
        assert f"from foghorn.flows.{name}.build import build_graph" in text and text.rstrip().endswith(
            "graph = build_graph()"
        )


@pytest.mark.parametrize(
    ("builder", "name"),
    [(build_router, "event_router"), (build_storm, "storm_correlator"), (build_gate, "change_gate")],
)
def test_run_context_accepts_every_key_the_dispatcher_sends(builder, name):
    """``context_schema(**context)`` raises on unknown keys: the dispatcher's whole run context must be accepted."""
    from foghorn.events.config import EventsSettings
    from foghorn.events.routing import run_context

    ev = make_event("c1", scenario_id="S01")
    ctx = run_context(
        ev,
        {
            "model_profile": "naive",
            "approval_policy": "strict",
            "breakpoints": ["x"],
            "memory_enabled": False,
            "chaos": "tools",
            "as_of": "2026-11-05T14:00:00Z",
            "token_delay_ms": 5,
        },
        EventsSettings(),
    )
    assert {
        "model_profile",
        "approval_policy",
        "scenario_id",
        "breakpoints",
        "memory_enabled",
        "chaos",
        "as_of",
        "token_delay_ms",
    } <= set(ctx)
    schema_cls = builder().context_schema
    assert schema_cls is not None and schema_cls(**ctx)
