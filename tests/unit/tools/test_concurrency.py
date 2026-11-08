"""Thread safety: the tools run in server threads. No shared mutable state, per-call read-only SQLite connections."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from foghorn.data import api as data
from foghorn.data import paths
from foghorn.tools import api
from foghorn.tools.context import advance_as_of


def _work(i: int) -> str:
    sid = f"S{(i % 16) + 1:02d}"
    ctx = {"scenario_id": sid, "as_of": advance_as_of({"scenario_id": sid}, i % 7)}
    root = (data.get_truth(sid)["root_cause"] or {"component": "orders-db"})["component"]
    out = [
        api.query_metric(ctx, root, "error_rate", 20),
        api.search_logs(ctx, root, "", "WARN+", 20, 20),
        api.list_changes(ctx, 240),
        api.list_alerts(ctx),
        api.verify_recovery(ctx, root),
        api.search_runbooks("database slow"),
    ]
    return json.dumps(out, sort_keys=True)


def test_parallel_calls_equal_serial_calls():
    serial = [_work(i) for i in range(48)]
    with ThreadPoolExecutor(max_workers=12) as ex:
        parallel = list(ex.map(_work, range(48)))
    assert parallel == serial


def test_concurrent_first_use_builds_one_valid_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FOGHORN_DATA_DIR", str(tmp_path))
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    assert not paths.telemetry_path().exists()
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = list(ex.map(lambda _: len(data.scenario_events("S10")), range(8)))
    assert len(set(sizes)) == 1 and sizes[0] > 20
    assert paths.telemetry_path().exists() and not list(tmp_path.glob("telemetry.sqlite.tmp*"))


def test_concurrent_fault_injection_is_consistent():
    def inject(_: int) -> str:
        return data.build_fault_scenario("disk_full", "shipping-service", 0.55, 1, 404)["scenario_id"]

    with ThreadPoolExecutor(max_workers=6) as ex:
        ids = list(ex.map(inject, range(6)))
    assert len(set(ids)) == 1
    assert len(data.scenario_events(ids[0])) > 5
