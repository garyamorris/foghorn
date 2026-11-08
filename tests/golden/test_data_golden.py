"""Golden snapshots of the generated world. If one drifts the generator or a tool changed behaviour: review, then
``UPDATE_GOLDENS=1 pytest tests/golden/test_data_golden.py`` and read the diff."""

from __future__ import annotations

import json

import pytest

from foghorn.data import access, store
from foghorn.data import api as data
from foghorn.data.util import to_iso
from foghorn.tools import api as tools
from foghorn.tools.context import advance_as_of

from .helpers import assert_golden


@pytest.fixture(autouse=True, scope="module")
def _data_dir(tmp_path_factory: pytest.TempPathFactory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FOGHORN_DATA_DIR", str(tmp_path_factory.mktemp("golden-data")))
        yield


def test_golden_manifest_summary():
    m = access.manifest()
    assert_golden(
        "data_manifest",
        {
            "seed": m["seed"],
            "combined_sha256": m["combined_sha256"],
            "datasets": {k: {"records": v.get("records"), "sha256": v["sha256"]} for k, v in m["datasets"].items()},
        },
    )


def test_golden_scenario_overview():
    out = {}
    with store.connect_ro(access.ensure_telemetry()) as c:
        for s in data.list_scenarios():
            sid = s["scenario_id"]
            t = data.get_truth(sid)
            alerts = c.execute(
                "SELECT fired_at, component, rule, label FROM alerts WHERE scenario_id=? ORDER BY fired_at, component, rule",
                (sid,),
            ).fetchall()
            changes = c.execute(
                "SELECT ts, change_id, kind, component, role FROM changes WHERE scenario_id=? ORDER BY ts, change_id",
                (sid,),
            ).fetchall()
            out[sid] = {
                "tier": s["tier"],
                "title": s["title"],
                "onset": s["world"]["onset"],
                "as_of": s["trigger"]["as_of"],
                "trigger_event": s["trigger"]["alert_event"],
                "stats": s["stats"],
                "root": t["root_cause"]
                and [t["root_cause"]["category"], t["root_cause"]["component"], t["root_cause"]["change_id"]],
                "affected": t["affected"],
                "severity": t["severity"],
                "preferred": t["remediation"]["preferred"],
                "interrupts": [i["kind"] for i in t["expected_interrupts"]],
                "alerts": [[to_iso(a["fired_at"]), a["component"], a["rule"], a["label"]] for a in alerts],
                "changes": [[to_iso(x["ts"]), x["change_id"], x["kind"], x["component"], x["role"]] for x in changes],
            }
    assert_golden("data_scenarios", out)


def test_golden_s10_public_record_and_first_events():
    events = data.scenario_events("S10")
    assert_golden(
        "data_s10_public",
        {
            "scenario": data.get_scenario("S10"),
            "first_events": events[:6],
            "event_types": sorted({e["type"] for e in events}),
        },
    )


def test_golden_tool_outputs_for_s10():
    ctx = {"scenario_id": "S10", "as_of": data.default_as_of("S10"), "applied_actions": []}
    q = tools.query_metric(ctx, "pricing-service", "p95_ms", 8)
    logs = tools.search_logs(ctx, "pricing-service", "TaxClient", "ERROR", 10, 3)
    rec = tools.execute_action(ctx, "revert_flag", "new_pricing_engine")
    later = {**ctx, "as_of": advance_as_of(ctx, 12), "applied_actions": [rec]}
    out = {
        "query_metric": q,
        "compare_baseline": tools.compare_baseline(ctx, "checkout-service", "error_rate"),
        "search_logs": {k: v for k, v in logs.items() if k != "lines"},
        "list_changes": [
            [c["change_id"], c["kind"], c["component"], c["minutes_before_as_of"]]
            for c in tools.list_changes(ctx, 240)["changes"]
        ],
        "blast_radius": tools.blast_radius(ctx, "pricing-service"),
        "execute_action": rec,
        "verify_before": {k: tools.verify_recovery(ctx, "pricing-service")[k] for k in ("status", "recovered")},
        "verify_after": {k: tools.verify_recovery(later, "pricing-service")[k] for k in ("status", "recovered")},
        "pricing_p95_after": tools.query_metric(later, "pricing-service", "p95_ms", 6)["points"],
        "runbooks_for_slow_db": [
            h["id"] for h in tools.search_runbooks("orders-db slow connection timeout cpu 100 percent")["hits"]
        ],
    }
    assert json.loads(json.dumps(out)) == out
    assert_golden("data_tools_s10", out)


def test_golden_fault_injector_is_stable():
    x = data.build_fault_scenario("cache_stampede", "sessions-cache", 0.8, 1, 123)
    t = data.get_truth(x["scenario_id"])
    assert_golden(
        "data_injected_fault",
        {
            "scenario": x,
            "root_cause": t["root_cause"],
            "affected": t["affected"],
            "red_herrings": t["red_herrings"],
            "preferred": t["remediation"]["preferred"],
            "event_types": sorted({e["type"] for e in data.scenario_events(x["scenario_id"])}),
        },
    )
