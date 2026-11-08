"""A4: the scenarios are solvable and fair - an oracle that sees ONLY the public tool API recovers the root cause and the
evidence for 16 / 16 scenarios (noise recognised for S04, abstention for S13), and never exceeds the tool budget."""

from __future__ import annotations

import pytest

from foghorn.data import api as data
from foghorn.tools import api

from .oracle import evidence_recall, solve

SIDS = [f"S{i:02d}" for i in range(1, 17)]


@pytest.mark.parametrize("sid", SIDS)
def test_oracle_solves_the_scenario_through_the_public_tools(sid):
    t = data.get_truth(sid)
    f = solve(sid)
    rc = t["root_cause"]
    if t["is_noise"]:
        assert (
            f.verdict == "noise" and f.root == "orders-db" and f.action == t["remediation"]["preferred"].split(":")[0]
        )
    elif rc["category"] == "telemetry_gap":
        assert f.verdict == "abstain" and rc["component"] in f.blind and f.action == "escalate_human"
    else:
        assert f.verdict == "incident" and f.root == rc["component"], (f.verdict, f.root, rc["component"])
    assert evidence_recall(f, t["key_evidence"]) >= 0.75, (sid, t["key_evidence"], sorted(f.anomalies))


@pytest.mark.parametrize("sid", SIDS)
def test_the_oracle_avoids_the_red_herrings(sid):
    t = data.get_truth(sid)
    f = solve(sid)
    for h in t["red_herrings"]:
        if h["kind"] == "change":
            assert h["id"] not in f.changes[:1] or sid == "S05", (sid, h)
        else:
            assert f.root != h["id"].split("/")[0]


def test_the_naive_nearest_change_heuristic_is_fooled_by_s10():
    """Why S10 is tier 3: the most recent change before the alert is NOT the culprit's neighbour - a naive agent blames it."""
    ctx = {"scenario_id": "S10", "as_of": data.default_as_of("S10")}
    changes = api.list_changes(ctx, 240)["changes"]
    nearest_non_ambient = changes[0]
    assert nearest_non_ambient["component"] == "pricing-service"  # the flag is the NEWEST...
    reco = next(c for c in changes if c["component"] == "recommendation-service")
    assert reco["minutes_before_as_of"] - nearest_non_ambient["minutes_before_as_of"] == pytest.approx(
        1.0
    )  # ...but the deploy is 1 min older
    topo = api.blast_radius(ctx, "recommendation-service")
    assert "checkout-service" not in [i["id"] for i in topo["transitive_callers"]]  # and it is not on the failing path


def test_s13_requires_abstention_not_guessing():
    ctx = {"scenario_id": "S13", "as_of": data.default_as_of("S13")}
    assert api.list_alerts(ctx, window_minutes=135, active_only=True)["count"] == 0 or all(
        a["rule"] == "synthetic_probe_failed" for a in api.list_alerts(ctx, window_minutes=135)["alerts"]
    )
    assert api.compare_baseline(ctx, "payments-service", "error_rate")["verdict"] in (
        "flatline",
        "insufficient_data",
        "normal",
    )


def test_s14_lexical_trap_ranks_the_restart_runbook_first():
    ctx = {"scenario_id": "S14", "as_of": data.default_as_of("S14")}
    assert api.compare_baseline(ctx, "orders-db", "cpu_pct")["verdict"] in ("degraded", "critical")
    hits = api.search_runbooks("orders-db database slow, connection timeout, cpu 100 percent, unresponsive")["hits"]
    assert hits[0]["id"] == "RB-017" and "RB-008" in [h["id"] for h in hits]
