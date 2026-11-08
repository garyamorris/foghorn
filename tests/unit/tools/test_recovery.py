"""The recovery overlay: right action on the right component recovers deterministically; wrong ones leave the world degraded."""

from __future__ import annotations

import pytest

from foghorn.data import api as data
from foghorn.data.util import to_epoch
from foghorn.tools import api, overlay
from foghorn.tools.context import add_actions, advance_as_of

SIDS = [f"S{i:02d}" for i in range(1, 17)]
PARAMS = {
    "S11": {"sql": "CREATE INDEX CONCURRENTLY idx_order_lines_fulfilment ON order_lines (fulfilment_id)"},
    "S14": {
        "break_glass": {"reason": "runaway export pins the primary during peak trading", "second_approver": "p.raman"}
    },
}
RECOVERABLE = [
    s
    for s in SIDS
    if not data.get_truth(s)["is_noise"] and data.get_truth(s)["root_cause"]["category"] != "telemetry_gap"
]


def run(sid: str, action: str, target: str, params: dict | None = None, wait: float = 15.0, svc: str | None = None):
    ctx = {"scenario_id": sid, "as_of": data.default_as_of(sid), "applied_actions": []}
    rec = api.execute_action(ctx, action, target, PARAMS.get(sid, {}) if params is None else params)
    later = {"scenario_id": sid, "as_of": advance_as_of(ctx, wait), "applied_actions": [rec]}
    root = svc or data.get_truth(sid)["root_cause"]["component"]
    return rec, later, api.verify_recovery(later, root)


@pytest.mark.parametrize("sid", RECOVERABLE)
def test_preferred_action_recovers_every_affected_component_within_15_minutes(sid):
    t = data.get_truth(sid)
    action, target = t["remediation"]["preferred"].split(":")
    rec, later, _ = run(sid, action, target)
    assert rec["outcome"] == "executed" and rec["ok"]
    for comp in t["affected"]:
        v = api.verify_recovery(later, comp)
        assert v["status"] in ("recovered", "healthy") and v["recovered"], (
            sid,
            comp,
            v["status"],
            [c for c in v["checks"] if not c["ok"]],
        )
    # the same world, no action: still degraded
    nothing = {**later, "applied_actions": []}
    assert api.verify_recovery(nothing, t["root_cause"]["component"])["status"] == "degraded"


@pytest.mark.parametrize("sid", RECOVERABLE)
def test_a_wrong_action_on_the_root_component_leaves_it_degraded(sid):
    t = data.get_truth(sid)
    root = t["root_cause"]["component"]
    _, _, v = run(sid, "open_ticket", root)
    assert v["status"] == "degraded" and not v["recovered"]
    # a plausible-but-wrong remediation (restart something unrelated) does not help either
    other = "web-frontend" if root != "web-frontend" else "catalog-service"
    _, _, v2 = run(sid, "restart_service", other)
    assert v2["status"] == "degraded"


def test_right_action_wrong_target_does_not_recover():
    _, _, v = run("S01", "rollback_deploy", "edge-gateway", svc="checkout-service")
    assert v["status"] == "degraded"
    _, _, v = run("S10", "rollback_deploy", "recommendation-service", svc="pricing-service")  # the red herring
    assert v["status"] == "degraded"
    _, _, v = run("S05", "restart_service", "orders-service", svc="orders-db")
    assert v["status"] == "degraded"


def test_blocked_and_rejected_actions_do_not_recover():
    rec, later, v = run("S14", "kill_queries", "orders-db", params={})  # freeze + peak: break-glass required
    assert rec["outcome"] == "blocked" and v["status"] == "degraded"
    rec, _, v = run("S05", "restart_db_primary", "orders-db")
    assert rec["outcome"] == "blocked" and v["status"] == "degraded"
    rec, _, v = run("S01", "rollback_deploy", "no-such-service")
    assert rec["outcome"] == "rejected" and v["status"] == "degraded"


def test_alternative_acceptable_actions_recover_partially_or_slowly():
    # S10: scale_out is acceptable but only a partial fix (fraction 0.6)
    _, _, v = run("S10", "scale_out", "pricing-service", wait=25)
    assert v["status"] in ("recovering", "degraded") and not v["recovered"]
    # S07: pausing the consumer stops the retry loop but does not drain the lag
    _, _, v = run("S07", "pause_consumer", "notification-service", wait=25, svc="event-bus")
    assert not v["recovered"]
    # S06: warm_cache alone is a partial fix; both together recover
    sid = "S06"
    ctx = {"scenario_id": sid, "as_of": data.default_as_of(sid), "applied_actions": []}
    recs = [
        api.execute_action(ctx, "revert_config", "session_ttl_seconds", {}),
        api.execute_action(ctx, "warm_cache", "sessions-cache", {}),
    ]
    later = {**ctx, "as_of": advance_as_of(ctx, 15), "applied_actions": recs}
    assert api.verify_recovery(later, "sessions-cache")["status"] == "recovered"


def test_stopgap_restart_relapses_but_rollback_does_not():
    sid = "S09"
    ctx = {"scenario_id": sid, "as_of": data.default_as_of(sid), "applied_actions": []}
    restart = api.execute_action(ctx, "restart_service", "search-service", {})
    soon = {**ctx, "as_of": advance_as_of(ctx, 10), "applied_actions": [restart]}
    assert api.verify_recovery(soon, "search-service")["status"] == "recovered"
    mem_soon = api.query_metric(soon, "search-service", "mem_pct", 3)["latest"]["value"]
    late = {**ctx, "as_of": advance_as_of(ctx, 40), "applied_actions": [restart]}
    mem_late = api.query_metric(late, "search-service", "mem_pct", 3)["latest"]["value"]
    assert mem_late > mem_soon + 5  # the leak is still there: memory climbs back
    rollback = api.execute_action(ctx, "rollback_deploy", "search-service", {})
    late_rb = {**ctx, "as_of": advance_as_of(ctx, 30), "applied_actions": [rollback]}
    assert api.verify_recovery(late_rb, "search-service")["status"] == "recovered"


def test_overlay_is_deterministic_and_leaves_the_past_untouched():
    sid = "S10"
    ctx = {"scenario_id": sid, "as_of": data.default_as_of(sid), "applied_actions": []}
    rec = api.execute_action(ctx, "revert_flag", "new_pricing_engine", {})
    assert rec == api.execute_action(
        ctx, "revert_flag", "new_pricing_engine", {}
    )  # same inputs -> same record (and id)
    later = {**ctx, "as_of": advance_as_of(ctx, 20), "applied_actions": [rec]}
    a = api.query_metric(later, "pricing-service", "p95_ms", 60)
    assert a == api.query_metric(later, "pricing-service", "p95_ms", 60)
    plain = api.query_metric({**later, "applied_actions": []}, "pricing-service", "p95_ms", 60)
    applied_s = to_epoch(rec["applied_at"])
    for (ts, v1), (_, v0) in zip(a["points"], plain["points"], strict=True):
        if to_epoch(ts) <= applied_s:
            assert v1 == v0  # nothing before the action changes
    assert a["points"][-1][1] < plain["points"][-1][1] / 3  # and afterwards it heals
    # an action applied "in the future" of the view (a fork that rewound as_of) has no effect yet
    future = {
        "scenario_id": sid,
        "as_of": data.default_as_of(sid),
        "applied_actions": [{**rec, "applied_at": advance_as_of(ctx, 10)}],
    }
    assert api.query_metric(future, "pricing-service", "p95_ms", 30) == api.query_metric(
        {**future, "applied_actions": []}, "pricing-service", "p95_ms", 30
    ) | {"actions_considered": 1}


def test_log_signatures_stop_after_a_successful_remediation():
    sid = "S10"
    ctx = {"scenario_id": sid, "as_of": data.default_as_of(sid), "applied_actions": []}
    rec = api.execute_action(ctx, "revert_flag", "new_pricing_engine", {})
    end = advance_as_of(ctx, 36)
    before = api.search_logs({"scenario_id": sid, "as_of": end}, "pricing-service", "TaxClient", None, 20, 200)[
        "total_matches"
    ]
    after = api.search_logs(
        {"scenario_id": sid, "as_of": end, "applied_actions": [rec]}, "pricing-service", "TaxClient", None, 20, 200
    )["total_matches"]
    assert before > 0 and after < before / 2


def test_alerts_resolve_after_a_successful_remediation():
    sid = "S01"
    ctx = {"scenario_id": sid, "as_of": data.default_as_of(sid), "applied_actions": []}
    rec = api.execute_action(ctx, "rollback_deploy", "checkout-service", {})
    later = {**ctx, "as_of": advance_as_of(ctx, 30), "applied_actions": [rec]}
    active_with = api.list_alerts(later, window_minutes=135, active_only=True)["alerts"]
    active_without = api.list_alerts({**later, "applied_actions": []}, window_minutes=135, active_only=True)["alerts"]
    assert len(active_without) >= 4 and len(active_with) < len(active_without)
    assert not [a for a in active_with if a["component"] == "checkout-service"]


def test_applied_actions_reducer_is_idempotent():
    a = {"id": "act_1"}
    b = {"id": "act_2"}
    assert add_actions([a], [a, b]) == [a, b] and add_actions(None, [a]) == [a] and add_actions([a, b], None) == [a, b]
    assert add_actions(add_actions([a], [b]), [b, a]) == [a, b]


def test_recovery_profiles_in_truth_are_applied_and_listed():
    t = data.get_truth("S09")["remediation"]
    assert t["recovery"]["restart_service:search-service"]["relapse_after_min"] == 28.0
    entry = overlay.Entry("x", 0, 1.0, 120.0, 150.0, None)
    assert (
        entry.progress(60) == 0.0
        and 0.5 < entry.progress(270) < 1.0
        and entry.progress(2000) == pytest.approx(1.0, abs=1e-3)
    )
    assert overlay.Recovery((entry, entry), frozenset()).factor(270) > entry.progress(
        270
    )  # combined actions: 1 - prod(1 - p)
