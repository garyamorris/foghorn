"""EventsRuntime bootstrap: wait-for-ready, idempotent seeding that tolerates failure, tickers, health, restart persistence."""

from __future__ import annotations

import asyncio
import contextlib

from foghorn.events.config import EventsSettings
from foghorn.events.dataport import DataUnavailable, RealDataPort
from foghorn.events.seeding import assistant_id, assistant_specs, seed_all
from tests.unit.events.fakes import FakeClient, FakeData


def seeded_assistants(env) -> list[dict]:
    return [a for a in env.client.assistants.items.values() if a["metadata"].get("foghorn_seed")]


def assistant_uuid(env, graph_id: str) -> str:
    return next(
        a["assistant_id"]
        for a in env.client.assistants.items.values()
        if a["graph_id"] == graph_id and a["metadata"].get("created_by") == "system"
    )


async def bootstrap(env) -> None:
    await env.rt._bootstrap()  # noqa: SLF001


async def test_bootstrap_seeds_assistants_memories_and_a_disabled_cron(env):
    await bootstrap(env)
    assert env.rt.agent_state == "ready" and env.rt.seed_state == "done"
    seeded = [a for a in env.client.assistants.items.values() if a["metadata"].get("foghorn_seed")]
    names = {a["name"] for a in seeded}
    assert {
        "commander-competent",
        "commander-naive",
        "commander-cautious",
        "commander-gullible",
        "commander-policy-strict",
        "commander-policy-auto-low-risk",
    } <= names
    assert len(names) >= 8 and len(seeded) == len(assistant_specs())
    assert len(env.client.store.items) == 2
    assert env.rt.seed_report["memories"] == {"seeded": 2, "skipped": 0, "failed": 0}
    cron = env.client.crons.items[0]
    assert (
        cron["assistant_id"] == assistant_uuid(env, "shift_handoff")
        and cron["enabled"] is False
        and cron["schedule"] == env.rt.es.native_cron_schedule
    )
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_seeding_is_idempotent_across_restarts(env):
    await bootstrap(env)
    report1 = dict(env.rt.seed_report)
    await bootstrap(env)
    assert env.rt.seed_report["memories"] == {"seeded": 0, "skipped": 2, "failed": 0}
    assert report1["cron"]["created"] is True and env.rt.seed_report["cron"]["created"] is False
    assert len(env.client.crons.items) == 2 and len(seeded_assistants(env)) == len(
        assistant_specs()
    )  # shift_handoff + memory_keeper consolidate
    assert report1["cron_memory"]["created"] is True and env.rt.seed_report["cron_memory"]["created"] is False
    assert assistant_id("commander-naive") == assistant_id("commander-naive") != assistant_id("commander-competent")
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_seeding_failures_are_logged_and_never_raise(env):
    env.client.fail_assistants = True
    env.client.fail_store = True
    await bootstrap(env)
    assert env.rt.seed_state == "done"  # the steps are isolated: failures are reported, not raised
    assert env.rt.seed_report["assistants"]["failed"] == len(assistant_specs())
    assert env.rt.seed_report["memories"]["failed"] == 2
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_seeding_survives_an_unfinished_data_package(env):
    env.data.ok = False
    await bootstrap(env)
    assert env.rt.seed_state == "done" and env.rt.seed_report["memories"]["unavailable"] == 1
    assert len(seeded_assistants(env)) == len(assistant_specs())
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_a_blowing_up_seeder_cannot_crash_bootstrap(env, monkeypatch):
    import foghorn.events.service as svc

    async def boom(*_a, **_k):
        raise RuntimeError("seed exploded")

    monkeypatch.setattr(svc, "seed_all", boom)
    await bootstrap(env)  # does not raise
    assert env.rt.seed_state == "failed"
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_seed_all_isolates_each_step(env):
    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("no client")

    report = await seed_all(Exploding(), FakeData(), EventsSettings())
    assert set(report) == {"assistants", "memories", "cron", "cron_memory"}  # every step reported something


async def test_bootstrap_when_the_agent_server_never_becomes_ready(env):
    env.client.fail_search = True
    await bootstrap(env)
    assert env.rt.agent_state == "unavailable" and env.rt.seed_state == "failed"
    h = env.rt.health()
    assert h["status"] == "degraded" and h["ready"] is False and h["components"]["agent_server"] == "unavailable"
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_seed_can_be_disabled(make_env):
    env = await make_env(seed_on_start=False)
    await env.rt._bootstrap()  # noqa: SLF001
    assert env.rt.seed_state == "skipped" and not seeded_assistants(env)
    await env.rt.dispatcher.stop()
    await env.rt.scheduler.stop()


async def test_background_tasks_start_and_stop_cleanly(make_env):
    env = await make_env(start=False, start_background=True)
    await env.rt.start()
    for _ in range(100):
        if env.rt.seed_state != "pending":
            break
        await asyncio.sleep(0.01)
    assert (
        env.rt.health()["components"]["dispatcher"] == "running"
        and env.rt.health()["components"]["scheduler"] == "running"
    )
    assert env.rt.health()["ready"] is True and env.rt.health()["status"] == "ok"
    sub = env.rt.hub.subscribe(frozenset({"queue", "clock"}))
    for _ in range(100):
        if {m.channel for m in sub.buf} == {"queue", "clock"}:
            break
        await asyncio.sleep(0.01)
    assert {m.channel for m in sub.buf} == {"queue", "clock"}
    q = next(m.data for m in sub.buf if m.channel == "queue")
    assert set(q["depth"]) == {"P0", "P1", "P2", "P3"} and {"in_flight", "workers", "dlq", "shed", "rate_per_s"} <= set(
        q
    )
    c = next(m.data for m in sub.buf if m.channel == "clock")
    assert {"virtual_now", "speed", "paused", "mode", "wall_now"} <= set(c)
    await env.rt.stop()
    assert env.rt.started is False and not env.rt.db.is_open


async def test_virtual_clock_state_survives_a_restart(make_env):
    env = await make_env()
    env.rt.vclock.set(speed=10, paused=True)
    env.rt.vclock.set(virtual_now=env.rt.vclock.now().replace(hour=14))
    expected = env.rt.vclock.state()
    await env.rt.stop()
    env2 = await make_env()
    assert env2.rt.vclock.state() == expected


async def test_summary_and_queue_stats_shape(env):
    await env.ingest(type="customer.report", severity="medium", payload={})
    s = await env.rt.summary()
    assert (
        s["events_total"] == 1
        and s["queue"]["depth"]["P2"] == 1
        and s["open_incidents"] == 0
        and s["latest_seq"] == 1
        and s["last_eval"] is None
    )
    q = await env.rt.queue_stats()
    assert (
        q["total_depth"] == 1
        and q["backpressure"] is False
        and q["workers"] == env.rt.es.max_runs
        and q["per_graph"]["incident_commander"]["cap"] == 3
    )
    cfg = env.rt.config_snapshot()
    assert cfg["studio"]["base_link"] == "https://smith.langchain.com/studio/?baseUrl=http%3A%2F%2F127.0.0.1%3A2026"
    assert (
        cfg["studio"]["thread_url_template"] is None
        and cfg["auth_required"] is False
        and "incident_commander" in cfg["graph_ids"]
    )
    assert env.rt.studio_links("t-1") == {
        "base_url": cfg["studio"]["base_link"],
        "thread_id": "t-1",
        "thread_url": None,
    }


async def test_studio_thread_url_only_when_configured(make_env):
    env = await make_env(studio_thread_url="{studio}/thread/{thread_id}?baseUrl={base}")
    links = env.rt.studio_links("abc")
    assert links["thread_url"] == "https://smith.langchain.com/studio/thread/abc?baseUrl=http%3A%2F%2F127.0.0.1%3A2026"


async def test_clock_is_a_pure_function_of_the_injected_wall(env):
    env.wall.advance(30)
    assert env.rt.vclock.now_iso() == "2026-11-05T10:00:30Z"


# --------------------------------------------------------------------------------------------- real data port
def test_real_data_port_maps_missing_package_and_functions_to_data_unavailable(monkeypatch):
    import types

    port = RealDataPort()
    monkeypatch.setattr(port, "module_name", "foghorn.data.definitely_not_there")
    assert port.available() is False
    for call in (lambda: port.list_scenarios(), lambda: port.service_catalog(), lambda: port.seed_memories()):
        try:
            call()
        except DataUnavailable:
            pass
        else:
            raise AssertionError("expected DataUnavailable")
    assert "bad_deploy" in port.fault_categories()  # graceful fallback list

    fake = types.ModuleType("fake_data_api")
    fake.list_scenarios = lambda tier=None: [{"scenario_id": "S1", "category": "x"}]  # type: ignore[attr-defined]  # (no include_generated: older data layer)
    fake.get_scenario = lambda sid, reveal_truth=False: (
        {"scenario_id": sid} if sid == "S1" else (_ for _ in ()).throw(KeyError(sid))
    )  # type: ignore[attr-defined]
    fake.default_as_of = lambda sid: "2026-11-05T10:49:00Z"  # type: ignore[attr-defined]
    fake.read_dataset = lambda name, offset=0, limit=50: (
        {"total": 1, "items": [{"namespace": ["a"], "key": "k", "value": {"text": "t"}}]}
        if name == "seed_memories"
        else (_ for _ in ()).throw(KeyError(name))
    )  # type: ignore[attr-defined]
    import sys

    sys.modules["fake_data_api"] = fake
    try:
        monkeypatch.setattr(port, "module_name", "fake_data_api")
        assert port.available() is True and port.list_scenarios() == [
            {"scenario_id": "S1", "category": "x", "features": []}
        ]  # enriched, data-layer keys win
        assert port.fault_categories() == ["x"]
        assert port.commander_input("S1", {"id": "INC-1"}) == {
            "scenario_id": "S1",
            "incident": {"id": "INC-1"},
            "as_of": "2026-11-05T10:49:00Z",
        }
        assert port.seed_memories() == [
            {"namespace": ["a"], "key": "k", "value": {"text": "t"}}
        ]  # falls back to a seed dataset
        try:
            port.get_scenario("nope")
        except KeyError:
            pass
        else:
            raise AssertionError("expected UnknownScenario")
        with contextlib.suppress(DataUnavailable):
            port.scenario_events("S1")
    finally:
        sys.modules.pop("fake_data_api", None)


def test_fake_client_helper_is_sane():
    c = FakeClient()
    assert c.of("runs.create") == []


def test_real_data_port_prefers_the_data_layers_own_names(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("fake_data_api2")
    fake.list_scenarios = lambda tier=None, include_generated=False: [{"scenario_id": "S1", "gen": include_generated}]  # type: ignore[attr-defined]
    fake.memory_seed_items = lambda: [{"namespace": ["runbooks"], "key": "rb", "value": {"text": "t"}}]  # type: ignore[attr-defined]
    fake.read_dataset = lambda name, offset=0, limit=50, reveal_truth=False, scenario_id=None: {
        "total": 0,
        "records": [],
        "truth": reveal_truth,
    }  # type: ignore[attr-defined]
    sys.modules["fake_data_api2"] = fake
    port = RealDataPort()
    try:
        monkeypatch.setattr(port, "module_name", "fake_data_api2")
        assert port.seed_memories() == [{"namespace": ["runbooks"], "key": "rb", "value": {"text": "t"}}]
        assert port.list_scenarios(include_generated=True) == [
            {"scenario_id": "S1", "gen": True, "features": [], "category": None}
        ]
        assert port.read_dataset("x", 0, 5, reveal_truth=True)["truth"] is True
        assert (
            port.fault_categories()
        )  # no fault_categories() and no sibling `faults` module: scenario categories / defaults
    finally:
        sys.modules.pop("fake_data_api2", None)


def test_the_real_data_layer_is_wired_when_it_exists(tmp_path):
    """Smoke test against the DATA engineer's package (skipped while it is incomplete): shapes the server relies on."""
    port = RealDataPort()
    if not port.available():
        import pytest

        pytest.skip("foghorn.data.api not available yet")
    scen = port.list_scenarios()
    assert scen and {"scenario_id", "title", "tier"} <= set(scen[0]) and "truth" not in scen[0]
    sid = scen[0]["scenario_id"]
    events = port.scenario_events(sid)
    assert events and {"type", "occurred_at", "subject", "payload"} <= set(events[0])
    assert port.default_as_of(sid).endswith("Z")
    assert "bad_deploy" in port.fault_categories()
    assert port.commander_input(sid, {"id": "INC-1"})["as_of"]
    seeds = port.seed_memories()
    assert seeds == [] or {"namespace", "key", "value"} <= set(seeds[0])
    import pytest

    with pytest.raises(KeyError):
        port.get_scenario("S99")


async def test_memory_consolidation_cron_is_seeded_disabled(env):
    await bootstrap(env)
    crons = [c for c in env.client.crons.items if c["input"] == {"mode": "consolidate"}]
    assert len(crons) == 1 and crons[0]["enabled"] is False and crons[0]["schedule"] == "0 3 * * *"
