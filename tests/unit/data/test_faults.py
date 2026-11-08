"""Runtime fault injector: determinism, labelling, validation, persistence under data_dir, all 14 categories."""

from __future__ import annotations

import json

import pytest

from foghorn.contracts.events import EventEnvelope
from foghorn.data import api, paths, store
from foghorn.data import scenarios as scen
from foghorn.data.access import ScenarioNotFound
from foghorn.data.faults import CATEGORIES, make_plan, profile, validate_spec
from foghorn.data.world import build_world

#: a valid (category, component) per category
CASES = {
    "bad_deploy": "cart-service",
    "cert_expiry": "payments-service",
    "disk_full": "search-service",
    "planned_maintenance": "orders-db",
    "db_pool_exhaustion": "orders-db",
    "cache_stampede": "sessions-cache",
    "queue_poison_message": "event-bus",
    "dependency_outage": "email-provider",
    "memory_leak": "catalog-service",
    "config_change": "checkout-service",
    "slow_query_migration": "orders-db",
    "network_partition": "edge-gateway",
    "telemetry_gap": "payments-service",
    "runaway_query": "orders-db",
}


def test_cases_cover_all_categories():
    assert set(CASES) == set(CATEGORIES) and len(CATEGORIES) == 14


@pytest.mark.parametrize("category", CATEGORIES)
def test_every_category_builds_a_labelled_world(category):
    comp = CASES[category]
    x = api.build_fault_scenario(category, comp, 0.7, 1, 11)
    sid = x["scenario_id"]
    assert sid.startswith("X-") and "truth" not in x and "fault_args" not in x
    t = api.get_truth(sid)
    assert t["root_cause"]["category"] == category if not t["is_noise"] else category == "planned_maintenance"
    assert t["remediation"]["preferred"] and t["affected"]
    assert scen.check_actions(t) == []
    events = api.scenario_events(sid)
    assert events and all(EventEnvelope.model_validate(e).to_state() == e for e in events)
    assert x["trigger"]["as_of"] > x["world"]["onset"]
    assert api.default_as_of(sid) == x["trigger"]["as_of"]
    assert (paths.generated_dir() / f"{sid}.json").exists() and (paths.generated_dir() / f"{sid}.sqlite").exists()


def test_injector_is_deterministic_and_idempotent(tmp_path, monkeypatch):
    a = api.build_fault_scenario("memory_leak", "search-service", 0.6, 2, 5)
    b = api.build_fault_scenario("memory_leak", "search-service", 0.6, 2, 5)
    assert a == b
    # a different data dir builds the identical scenario id and world (deterministic from its arguments)
    monkeypatch.setenv("FOGHORN_DATA_DIR", str(tmp_path))
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    c = api.build_fault_scenario("memory_leak", "search-service", 0.6, 2, 5)
    assert c == a
    assert api.get_truth(c["scenario_id"])["root_cause"]["component"] == "search-service"
    # different arguments -> different scenario
    d = api.build_fault_scenario("memory_leak", "search-service", 0.6, 2, 6)
    assert d["scenario_id"] != c["scenario_id"]


def test_world_digest_is_reproducible_from_the_spec():
    spec = scen.generated_spec("X-7777", "db_pool_exhaustion", "orders-db", 0.8, 2, 3)
    w1, w2 = build_world(spec), build_world(spec)
    assert store.rows_digest(store.rows_for_world(w1)) == store.rows_digest(store.rows_for_world(w2))


def test_red_herrings_are_off_the_failing_path():
    x = api.build_fault_scenario("db_pool_exhaustion", "orders-db", 0.7, 2, 21)
    t = api.get_truth(x["scenario_id"])
    assert len(t["red_herrings"]) == 2
    from foghorn.tools import api as tools

    ch = tools.list_changes({"scenario_id": x["scenario_id"]}, 240)["changes"]
    herring_ids = {h["id"] for h in t["red_herrings"]}
    comps = {c["component"] for c in ch if c["change_id"] in herring_ids}
    assert comps and not (comps & set(t["affected"]))
    assert any(f"rollback_deploy:{c}" in t["remediation"]["forbidden"] for c in comps)


@pytest.mark.parametrize(
    ("args", "msg"),
    [
        (("nonsense", "orders-db", 0.5, 0, 1), "unknown fault category"),
        (("bad_deploy", "no-such", 0.5, 0, 1), "unknown component"),
        (("bad_deploy", "orders-db", 0.5, 0, 1), "cannot target"),
        (("cache_stampede", "orders-db", 0.5, 0, 1), "targets sessions-cache"),
        (("bad_deploy", "cart-service", 0.0, 0, 1), "magnitude"),
        (("bad_deploy", "cart-service", 0.5, 9, 1), "red_herrings"),
    ],
)
def test_injector_rejects_bad_arguments(args, msg):
    with pytest.raises(ValueError, match=msg):
        api.build_fault_scenario(*args)


def test_unknown_generated_scenario_is_not_found():
    with pytest.raises(ScenarioNotFound):
        api.get_scenario("X-0000")
    with pytest.raises(ScenarioNotFound):
        api.scenario_events("X-9999")
    with pytest.raises(ScenarioNotFound):
        api.get_truth("S99")


def test_list_scenarios_can_include_generated_ones():
    x = api.build_fault_scenario("disk_full", "shipping-service", 0.5, 0, 2)
    assert all(not s["scenario_id"].startswith("X-") for s in api.list_scenarios())
    assert x["scenario_id"] in {s["scenario_id"] for s in api.list_scenarios(include_generated=True)}
    assert all("truth" not in s for s in api.list_scenarios(include_generated=True))
    assert {s["scenario_id"] for s in api.list_scenarios(tier=1)} == {"S01", "S02", "S03", "S04"}


def test_id_collision_probes_to_the_next_free_id(tmp_path):
    first = api.build_fault_scenario("bad_deploy", "pricing-service", 0.5, 0, 77)
    # plant a different fault under the id the next call would want
    wanted = scen.generated_id("bad_deploy", "user-profile", 0.5, 0, 77, "0|None")
    src = paths.generated_dir()
    for suffix in (".json", ".sqlite"):
        (src / f"{wanted}{suffix}").write_bytes((src / f"{first['scenario_id']}{suffix}").read_bytes())
    rec = json.loads((src / f"{wanted}.json").read_text())
    rec["scenario_id"] = wanted
    (src / f"{wanted}.json").write_text(json.dumps(rec))
    second = api.build_fault_scenario("bad_deploy", "user-profile", 0.5, 0, 77)
    assert second["scenario_id"] != wanted
    assert api.build_fault_scenario("bad_deploy", "user-profile", 0.5, 0, 77) == second


def test_effect_profiles():
    from foghorn.data.faults import Effect

    ramp = Effect("a", "m", "add", 1.0, 0.0, 4.0)
    assert [profile(ramp, t) for t in (-1, 0, 2, 4, 9)] == [0.0, 0.0, 0.5, 1.0, 1.0]
    trap = Effect("a", "m", "add", 1.0, 0.0, 2.0, "trap", 3.0)
    assert profile(trap, 4) == 1.0 and profile(trap, 6) == 0.5 and profile(trap, 8) == 0.0
    assert profile(Effect("a", "m", "add", 1.0, 5.0, 1.0, "pulse"), 5.0) == 1.0
    assert profile(Effect("a", "m", "add", 1.0, 5.0, 1.0, "pulse"), 6.0) == 0.0
    assert 0.9 < profile(Effect("a", "m", "add", 1.0, 0.0, 2.0, "saw", 10.0), 30.0) <= 1.0
    assert profile(Effect("a", "m", "add", 1.0, 0.0, 2.0, "decay"), 2.0) == pytest.approx(0.3679, abs=1e-3)


def test_validate_spec_and_plan_for_every_category():
    for cat, comp in CASES.items():
        validate_spec(cat, comp)
        spec = scen.generated_spec("X-0001", cat, comp, 0.7, 0, 1)
        assert make_plan(spec).effects
