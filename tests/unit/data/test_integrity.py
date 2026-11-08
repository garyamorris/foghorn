"""Referential integrity and the acceptance counts (A2, A3): every id referenced anywhere exists."""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from foghorn.data import actions as act
from foghorn.data import api, catalog, docs, paths, people
from foghorn.data import scenarios as scen
from foghorn.data.faults import CATEGORIES
from foghorn.data.util import DEFAULT_SEED

DS = paths.repo_root() / "datasets"
SIDS = [f"S{i:02d}" for i in range(1, 17)]


def jl(rel: str) -> list[dict]:
    return [json.loads(line) for line in (DS / rel).read_text().splitlines() if line.strip()]


def test_catalog_shape():
    assert catalog.validate_catalog() == []
    assert len(catalog.COMPONENTS) == 20
    kinds = Counter(c.kind for c in catalog.COMPONENTS.values())
    assert kinds == {"service": 15, "datastore": 2, "queue": 1, "external": 2}
    series = sum(len(c.metrics) for c in catalog.COMPONENTS.values())
    assert series == 142  # 7 core x 20 + hit_ratio + consumer_lag
    assert (
        len(catalog.COMPONENTS["sessions-cache"].metrics) == 8
        and "hit_ratio" in catalog.COMPONENTS["sessions-cache"].metrics
    )
    assert "consumer_lag" in catalog.COMPONENTS["event-bus"].metrics
    # the call graph is acyclic
    for cid in catalog.COMPONENT_IDS:
        assert cid not in catalog.transitive_callees(cid, sync_only=False)
    # shared database fan-in (the S05/S11/S14 fault domain)
    assert set(catalog.callers_of("orders-db")) >= {
        "orders-service",
        "inventory-service",
        "shipping-service",
        "user-profile",
        "payments-service",
    }


def test_service_catalog_dataset_matches_code():
    recs = api.service_catalog()
    assert [r["id"] for r in recs] == list(catalog.COMPONENT_IDS)
    rb_ids = {r["id"] for r in docs.runbook_records()}
    for r in recs:
        assert set(r["runbooks"]) <= rb_ids
        assert set(r["depends_on"]) <= set(catalog.COMPONENT_IDS)
        assert r["oncall"] in {x["id"] for x in people.ROTAS}


def test_counts_match_the_acceptance_table():
    idx = {d["name"]: d for d in api.datasets_index()}
    assert idx["service_catalog"]["records"] == 20
    assert idx["runbooks"]["records"] == 24 and len(list((DS / "reference/runbooks").glob("RB-*.md"))) == 24
    assert idx["policies"]["records"] == 8
    assert idx["past_incidents"]["records"] == 120
    assert idx["scenarios"]["records"] == 16
    assert idx["triage_cases"]["records"] == 400
    assert idx["rag_qa"]["records"] >= 48
    assert idx["safety_cases"]["records"] == 16
    assert idx["sweep_specs"]["records"] == 60
    assert (
        idx["stream_workday"]["records"] >= 1800
        and idx["stream_quiet_hour"]["records"] == 240
        and idx["stream_storm"]["records"] == 650
    )
    assert (idx["memory_prefs"]["records"], idx["memory_lessons"]["records"], idx["people"]["records"]) == (14, 30, 8)
    assert len(act.ACTIONS) == 22
    assert len({d["id"] for d in api.datasets_index() if d["id"].startswith("d")}) == 12  # D1..D12 all present


def test_scenario_suite_is_complete():
    recs = {sid: api.get_scenario(sid, reveal_truth=True) for sid in SIDS}
    assert Counter(r["tier"] for r in recs.values()) == {1: 4, 2: 6, 3: 6}
    assert {r["truth"]["root_cause"]["category"] for r in recs.values() if r["truth"]["root_cause"]} == set(
        CATEGORIES
    ) - {"planned_maintenance"} | set()
    cats = Counter((r["truth"]["root_cause"] or {"category": "planned_maintenance"})["category"] for r in recs.values())
    assert set(cats) == set(CATEGORIES) and cats["bad_deploy"] == 2 and cats["cache_stampede"] == 2
    required = {
        "is_noise",
        "root_cause",
        "affected",
        "red_herrings",
        "severity",
        "customer_impact",
        "key_evidence",
        "remediation",
        "expected_interrupts",
        "expected_memory",
        "comms",
        "budget",
        "flags",
    }
    for sid, r in recs.items():
        assert required <= set(r["truth"]), sid
        assert {"preferred", "acceptable", "forbidden", "forbidden_patterns", "requires_approval"} <= set(
            r["truth"]["remediation"]
        )
        assert r["world"]["window"][0] < r["world"]["onset"] < r["world"]["window"][1]
        assert r["trigger"]["as_of"] > r["world"]["onset"]


def test_every_action_and_component_referenced_by_scenarios_exists():
    for sid in SIDS:
        t = api.get_truth(sid)
        assert scen.check_actions(t) == [], sid
        for c in t["affected"]:
            assert c in catalog.COMPONENTS
        rc = t["root_cause"]
        if rc:
            assert rc["component"] in catalog.COMPONENTS
        assert set(t["remediation"]["recovery"]) <= set(t["remediation"]["acceptable"])
        assert set(t["remediation"]["requires_approval"]) <= set(t["remediation"]["acceptable"])
        for h in t["red_herrings"]:
            assert h["id"]


def test_runbooks_reference_known_actions_components_and_policies():
    action_ids = set(act.ACTIONS)
    for p in sorted((DS / "reference/runbooks").glob("RB-*.md")):
        meta, body = docs.parse_markdown(p.read_text())
        assert meta["id"] == p.stem
        assert {a["id"] for a in meta["actions"]} <= action_ids, p.name
        assert set(meta["avoid"]) <= action_ids
        assert set(meta["applies_to"]) <= set(catalog.COMPONENT_IDS)
        mentioned = set(re.findall(r"`([a-z_]+)`", body)) & action_ids
        assert mentioned <= {a["id"] for a in meta["actions"]} | set(meta["avoid"]), (p.name, mentioned)
        assert mentioned, p.name  # every runbook tells the reader which action to take
        for rid in re.findall(r"RB-\d{3}", body):
            assert (DS / f"reference/runbooks/{rid}.md").exists()
    assert len(list((DS / "reference/policies").glob("POL-*.md"))) == 8
    pol5, _ = docs.parse_markdown(next((DS / "reference/policies").glob("POL-005*")).read_text())
    listed = {a for lvl in pol5["risk_levels"].values() for a in lvl["examples"]}
    assert listed <= set(act.ACTIONS) and pol5["never_automatic"] == ["restart_db_primary"]
    for a in act.ACTIONS.values():  # the approval matrix is generated from the action catalogue
        if a.risk in pol5["risk_levels"] and not a.never_automatic:
            assert a.id in pol5["risk_levels"][a.risk]["examples"]


def test_past_incidents_reference_real_things():
    incs = jl("reference/past_incidents.jsonl")
    ids = [i["id"] for i in incs]
    assert ids == [f"INC-{n:04d}" for n in range(1, 121)]
    rb = {p.stem for p in (DS / "reference/runbooks").glob("RB-*.md")}
    for i in incs:
        assert i["root_cause_component"] in catalog.COMPONENTS and set(i["components"]) <= set(catalog.COMPONENT_IDS)
        assert i["root_cause_category"] in CATEGORIES and i["runbook"] in rb
        for r in i["resolution"]:
            a, t = act.parse_action(r)
            assert a in act.ACTIONS and act.known_target(t), r
    # IDs and dates are monotonic; anchors recalled by S10 / S16 exist and are older than every scenario
    dates = [i["opened_at"] for i in incs]
    assert dates == sorted(dates)
    onsets = [api.get_scenario(s)["world"]["onset"] for s in SIDS]
    assert max(dates) < min(onsets)
    assert next(i for i in incs if i["id"] == "INC-0087")["opened_at"] == "2026-01-26T09:04:00Z"
    for sid in SIDS:
        rec = api.get_truth(sid)["expected_memory"]["recall"]
        assert rec is None or rec in ids


def test_seed_memories_and_rotas():
    lessons = jl("seed/memory_lessons.jsonl")
    assert lessons[6]["key"] == "les-007" and lessons[6]["value"]["from_incident"] == "INC-0087"
    incs = {i["id"] for i in jl("reference/past_incidents.jsonl")}
    assert {x["value"]["from_incident"] for x in lessons} <= incs
    for r in jl("seed/memory_prefs.jsonl") + lessons + jl("seed/people.jsonl"):
        assert r["value"]["text"] and r["namespace"] and r["key"]
    for r in people.ROTAS:
        assert len(people.rota_members(r["id"])) >= 2
    a = people.oncall_at("rota-dba", "2026-11-05T10:00:00Z")
    b = people.oncall_at("rota-dba", "2026-11-12T10:00:00Z")
    assert a["primary"]["id"] != b["primary"]["id"] and a["secondary"]["id"] == b["primary"]["id"]


@pytest.mark.parametrize("sid", SIDS)
def test_trigger_event_exists_and_is_after_onset(sid):
    rec = api.get_scenario(sid)
    events = {e["id"]: e for e in api.scenario_events(sid)}
    assert rec["trigger"]["alert_event"] in events
    assert events[rec["trigger"]["alert_event"]]["occurred_at"] > rec["world"]["onset"]
    assert api.default_as_of(sid) == rec["trigger"]["as_of"]


def test_sweep_specs_cover_every_category_and_expand():
    specs = api.sweep_specs()
    assert len(specs) == 60 and len({s["spec_id"] for s in specs}) == 60
    assert {s["fault"]["category"] for s in specs} == set(CATEGORIES)
    per = Counter(s["fault"]["category"] for s in specs)
    assert min(per.values()) >= 4
    for s in specs[:14]:  # one per category expands to a fully labelled world
        x = api.build_fault_scenario(**s["build_args"])
        assert (
            x["scenario_id"].startswith("X-")
            and api.get_truth(x["scenario_id"])["root_cause"] is not None
            or s["fault"]["category"] == "planned_maintenance"
        )


def test_default_seed_constant():
    assert json.loads((DS / "MANIFEST.json").read_text())["seed"] == DEFAULT_SEED == 20261108
