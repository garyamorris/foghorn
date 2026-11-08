"""The public data API (ARCHITECTURE.md 4.1): shapes, truth guarding, dataset browsing, CLI."""

from __future__ import annotations

import json

import pytest

from foghorn.data import api, build, paths
from foghorn.data.__main__ import main
from foghorn.data.access import ScenarioNotFound

SIDS = [f"S{i:02d}" for i in range(1, 17)]


def test_public_views_never_contain_truth():
    for s in api.list_scenarios():
        assert "truth" not in s and "fault_args" not in s
        assert {"scenario_id", "tier", "title", "summary", "world", "trigger"} <= set(s)
    assert "truth" not in api.get_scenario("S10")
    full = api.get_scenario("S10", reveal_truth=True)
    assert full["truth"] == api.get_truth("S10") and full["truth"]["root_cause"]["component"] == "pricing-service"
    blob = json.dumps(api.list_scenarios())
    for secret in ("rollback_deploy", "root_cause", "red_herring", "forbidden", "key_evidence"):
        assert secret not in blob


def test_returned_objects_are_copies():
    api.get_truth("S01")["affected"].append("x")
    assert "x" not in api.get_truth("S01")["affected"]
    api.service_catalog()[0]["id"] = "mutated"
    assert api.service_catalog()[0]["id"] == "web-frontend"


def test_unknown_scenario():
    for fn in (api.get_scenario, api.get_truth, api.default_as_of, api.scenario_events):
        with pytest.raises(ScenarioNotFound):
            fn("S99")


def test_scenario_summaries_do_not_spoil_the_cause():
    for s in api.list_scenarios():
        cat = api.get_truth(s["scenario_id"])["root_cause"]
        if cat:
            assert cat["category"] not in json.dumps(s)


def test_datasets_index_and_browsing():
    idx = api.datasets_index()
    names = {d["name"] for d in idx}
    assert {
        "service_catalog",
        "runbooks",
        "policies",
        "past_incidents",
        "scenarios",
        "telemetry",
        "triage_cases",
        "rag_qa",
        "safety_cases",
        "sweep_specs",
    } <= names
    for d in idx:
        page = (
            api.read_dataset(d["name"], 0, 3)
            if d["name"] != "telemetry"
            else api.read_dataset("telemetry.alerts", 0, 3)
        )
        assert page["records"] and page["total"] >= len(page["records"]), d["name"]
        assert page["offset"] == 0 and page["limit"] == 3
    assert next(d for d in idx if d["name"] == "scenarios")["has_truth"] is True
    assert next(d for d in idx if d["name"] == "service_catalog")["has_truth"] is False
    with pytest.raises(KeyError):
        api.read_dataset("nope")
    with pytest.raises(KeyError):
        api.read_dataset("telemetry.nope")


def test_truth_is_hidden_until_revealed():
    hidden = api.read_dataset("scenarios", 0, 16)
    assert hidden["truth_hidden"] and all("truth" not in r for r in hidden["records"])
    blob = json.dumps(hidden)
    for secret in ("telemetry_gap_components", "lookback_days_needed", '"red_herrings"'):
        assert secret not in blob
    shown = api.read_dataset("scenarios", 0, 2, reveal_truth=True)
    assert not shown["truth_hidden"] and "truth" in shown["records"][0]
    assert "expected" not in api.read_dataset("triage_cases", 0, 1)["records"][0]
    assert "expected" in api.read_dataset("triage_cases", 0, 1, reveal_truth=True)["records"][0]
    assert "must_not" not in api.read_dataset("safety_cases", 0, 1)["records"][0]
    assert "expected_decision" not in api.read_dataset("stream_workday_labels", 0, 1)["records"][0]
    alert = api.read_dataset("telemetry.alerts", 0, 1)["records"][0]
    assert alert["fired_at"].endswith("Z")
    assert (
        "label" not in alert and "label" in api.read_dataset("telemetry.alerts", 0, 1, reveal_truth=True)["records"][0]
    )
    assert "kind" not in api.read_dataset("telemetry.logs", 0, 1)["records"][0]
    assert "role" not in api.read_dataset("telemetry.changes", 0, 1)["records"][0]


def test_dataset_paging():
    p1 = api.read_dataset("past_incidents", 0, 50)
    p2 = api.read_dataset("past_incidents", 50, 50)
    p3 = api.read_dataset("past_incidents", 100, 50)
    ids = [r["id"] for r in p1["records"] + p2["records"] + p3["records"]]
    assert ids == [f"INC-{n:04d}" for n in range(1, 121)] and p1["total"] == 120 and len(p3["records"]) == 20
    series = api.read_dataset("telemetry.series", 0, 2, scenario_id="S10")
    assert series["total"] == 142 and len(series["records"][0]["vals"]) == 135 and "base" not in series["records"][0]
    assert api.read_dataset("runbooks", 0, 1)["records"][0]["id"] == "RB-001"
    assert "text" in api.read_dataset("policies", 0, 1)["records"][0]
    assert api.read_dataset("rotas")["total"] == 8


def test_fault_categories():
    assert api.fault_categories()[0] == "bad_deploy" and len(api.fault_categories()) == 14


def test_streams_listing():
    streams = {s["name"]: s for s in api.list_streams()}
    assert set(streams) == {"stream_quiet_hour", "stream_workday", "stream_storm"}
    assert streams["stream_storm"]["storm"] and streams["stream_workday"]["scenarios"] == ["S10", "S01"]
    first = next(api.iter_stream("stream_quiet_hour"))
    assert first["id"] == "evt_qh_0001"
    with pytest.raises(KeyError):
        next(api.iter_stream("nope"))


def test_memory_seed_items():
    items = api.memory_seed_items()
    by = {}
    for it in items:
        by.setdefault(it["namespace"][0], []).append(it)
        assert it["value"]["text"]
    assert {k: len(v) for k, v in by.items()} == {
        "incidents": 120,
        "runbooks": 24,
        "prefs": 14,
        "lessons": 30,
        "people": 8,
    }
    assert {it["key"] for it in api.memory_seed_items("incidents")} >= {"INC-0061", "INC-0087"}
    assert all(
        it["namespace"][0] == "incidents" and len(it["namespace"]) == 2 for it in api.memory_seed_items("incidents")
    )


def test_service_catalog_records():
    cat = {c["id"]: c for c in api.service_catalog()}
    assert cat["checkout-service"]["depends_on"] == [
        "cart-service",
        "payments-service",
        "orders-service",
        "inventory-service",
    ]
    assert cat["orders-db"]["engine"] == "postgres-15" and cat["orders-db"]["slo"]["p95_ms"] == 40
    assert cat["checkout-service"]["slo"] == {"availability": 99.95, "p95_ms": 400}


def test_cli_check_list_and_only(tmp_path, capsys):
    assert main(["build", "--check"]) == 0
    assert "ok: regenerated hashes match" in capsys.readouterr().out
    assert main(["list"]) == 0
    assert "combined" in capsys.readouterr().out
    out = tmp_path / "ds"
    assert main(["build", "--only", "d9,d10", "--out", str(out), "--no-telemetry"]) == 0
    assert (out / "evals" / "rag_qa.jsonl").exists() and not (out / "scenarios").exists()
    man = json.loads((out / "MANIFEST.json").read_text())
    assert set(man["datasets"]) == {"rag_qa", "safety_cases"}
    # drift is reported with a non-zero exit code
    (out / "evals" / "safety_cases.jsonl").write_text("[]\n")
    assert main(["build", "--check", "--only", "d10", "--out", str(out)]) == 1
    assert "safety_cases" in capsys.readouterr().err
    with pytest.raises(ValueError, match="unknown dataset"):
        main(["build", "--only", "dZ", "--out", str(out)])


def test_cli_builds_telemetry_into_a_chosen_data_dir(tmp_path, capsys):
    assert main(["build", "--only", "d6", "--out", str(tmp_path / "ds"), "--data-dir", str(tmp_path / "dd")]) == 0
    assert (tmp_path / "dd" / "telemetry.sqlite").stat().st_size > 1_000_000
    assert "telemetry:" in capsys.readouterr().out


def test_lazy_telemetry_build_and_rebuild_on_hash_mismatch(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("FOGHORN_DATA_DIR", str(tmp_path))
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    db = paths.telemetry_path()
    assert not db.exists()
    t = time.perf_counter()
    api.scenario_events("S01")
    assert db.exists() and time.perf_counter() - t < 2.5  # first lazy build of the telemetry
    stamp = db.stat().st_mtime_ns
    api.scenario_events("S02")
    assert db.stat().st_mtime_ns == stamp  # hash-checked and reused
    db.write_bytes(b"corrupt")
    assert len(api.scenario_events("S01")) > 5  # corrupt/stale -> rebuilt transparently
    from foghorn.data import access, store

    assert store.db_meta(db)["built_for"] == access.expected_telemetry_sha()


def test_datasets_are_generated_on_demand_when_the_repo_copy_is_absent(tmp_path, monkeypatch):
    """A wheel install has no ``datasets/``: the first call builds them deterministically (same hashes as the committed ones)."""
    monkeypatch.setenv("FOGHORN_DATASETS_DIR", str(tmp_path / "ds"))
    monkeypatch.setenv("FOGHORN_DATA_DIR", str(tmp_path / "var"))
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    assert len(api.service_catalog()) == 20
    built = json.loads((tmp_path / "ds" / "MANIFEST.json").read_text())
    committed = json.loads((paths.repo_root() / "datasets" / "MANIFEST.json").read_text())
    assert built["combined_sha256"] == committed["combined_sha256"]
    assert api.default_as_of("S10") == "2026-11-05T10:48:30Z"


def test_a_stale_but_valid_database_is_rebuilt(tmp_path, monkeypatch):
    """A telemetry DB built for an older MANIFEST (e.g. before the generator changed) is replaced, not trusted."""
    from foghorn.data import access, store
    from foghorn.settings import get_settings

    monkeypatch.setenv("FOGHORN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    worlds = build.curated_worlds()
    digest = store.combine_digests({sid: store.rows_digest(store.rows_for_world(w)) for sid, w in worlds.items()})
    store.write_db(paths.telemetry_path(), worlds, digest, built_for="an-older-manifest")
    assert store.db_meta(paths.telemetry_path())["built_for"] == "an-older-manifest"
    assert len(api.scenario_events("S10")) > 20
    assert store.db_meta(paths.telemetry_path())["built_for"] == access.expected_telemetry_sha()
