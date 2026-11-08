"""World semantics: series shape, alert/metric consistency, propagation vs declared truth, evidence resolvable, difficulty."""

from __future__ import annotations

import json
import re
import statistics

import pytest

from foghorn.data import access, api, build, catalog, store
from foghorn.data import alerts as alerts_mod
from foghorn.data.util import DEFAULT_SEED, to_epoch
from foghorn.data.world import build_world

SIDS = [f"S{i:02d}" for i in range(1, 17)]


@pytest.fixture(scope="module")
def worlds():
    return build.curated_worlds(DEFAULT_SEED)


def test_series_layout_142_series_135_points(worlds):
    for sid, w in worlds.items():
        assert len(w.series) == 142, sid
        assert all(len(v) == 135 and len(w.base[k]) == 135 for k, v in w.series.items())
        assert w.times[0] == w.t0 and w.times[-1] - w.times[0] == 134 * 60
        assert to_epoch(w.spec.onset) - w.t0 == 90 * 60  # onset-90m .. onset+45m
    with store.connect_ro(access.ensure_telemetry()) as c:
        assert c.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 16 * 142


def test_metric_bounds_and_units(worlds):
    for w in worlds.values():
        for (cid, m), vals in w.series.items():
            md = catalog.METRICS[m]
            assert min(vals) >= md.lo and (md.hi is None or max(vals) <= md.hi), (w.spec.scenario_id, cid, m)


def test_alerts_are_exactly_the_rules_evaluated_over_the_series(worlds):
    """Alert/metric consistency: re-running the 14 rules over the stored series reproduces the alerts, nothing more."""
    assert len(alerts_mod.RULES) == 14
    for sid, w in worlds.items():
        again = alerts_mod.evaluate_world(w.series, w.times)
        key = lambda a: (a["fired_at_s"], a["component"], a["rule"])  # noqa: E731
        assert [key(a) for a in again] == [key(a) for a in w.alerts], sid
        for a in w.alerts:
            vals = w.series[(a["component"], a["metric"])]
            rule = alerts_mod.RULES_BY_NAME[a["rule"]]
            i = w.times.index(a["fired_at_s"])
            need = a["for_s"] // 60
            thr = a["threshold"]
            span = vals[i - need : i + 1]
            assert len(span) == need + 1
            assert all((v > thr) if rule.op == ">" else (v < thr) for v in span), (sid, a["alert_id"])
            assert a["value"] == vals[i]
            if a["resolved_at_s"] is not None:
                j = w.times.index(a["resolved_at_s"])
                assert not ((vals[j] > thr) if rule.op == ">" else (vals[j] < thr))


def test_alert_semantics_and_labels(worlds):
    s = worlds["S10"]
    first = min(s.alerts, key=lambda a: a["fired_at_s"] if a["label"] != "noise_flapping" else 10**12)
    assert to_epoch(s.spec.onset) < first["fired_at_s"] <= to_epoch(s.meta["as_of"])
    for w in worlds.values():
        for a in w.alerts:
            if a["label"] == "noise_flapping":
                assert a["rule"] == "synthetic_probe_failed" and a["for_s"] == 0
            if a["label"] in ("root", "symptom"):
                assert a["component"] in w.truth["affected"]
    flapping = sum(1 for w in worlds.values() for a in w.alerts if a["label"] == "noise_flapping")
    assert flapping >= 10  # benign flapping exists and is labelled
    s4 = worlds["S04"]
    assert {a["label"] for a in s4.alerts if a["component"] == "orders-db"} == {"noise_maintenance"}
    assert len(worlds["S12"].alerts) >= 50
    assert len({a["component"] for a in worlds["S12"].alerts if a["label"] != "noise_flapping"}) == 11
    total = sum(len(w.alerts) for w in worlds.values())
    assert 200 <= total <= 400


SIGNAL = ("error_rate", "p95_ms", "saturation_pct", "hit_ratio", "consumer_lag", "disk_pct", "mem_pct")
#: declared-affected components whose effect is real but too small/quiet for the generic "moved" test
QUIET = {
    "S11": {"checkout-service"},
    "S12": {"event-bus", "orders-db", "sessions-cache"},
    "S13": {"payments-service", "orders-service"},
}


def _moved(w) -> set[str]:
    out = set()
    for (cid, m), vals in w.series.items():
        if m not in SIGNAL:
            continue
        mean = statistics.fmean(vals[:30])
        std = max(statistics.pstdev(vals[:30]), 0.03 * abs(mean), 1e-9)
        dev = max(abs(v - mean) for v in vals[90:])
        material = dev >= (8.0 if m in ("disk_pct", "mem_pct") else 0.25 * abs(mean))
        if dev / std >= 6 and material:
            out.add(cid)
    return out


@pytest.mark.parametrize("sid", SIDS)
def test_declared_affected_set_matches_the_telemetry(worlds, sid):
    """Truth is declared, never inferred - and the generated telemetry must agree with it."""
    w = worlds[sid]
    affected, moved = set(w.truth["affected"]), _moved(w)
    herrings = {c["component"] for c in w.changes if c["role"] == "herring"}
    herrings |= {h["id"].split("/")[0] for h in w.truth["red_herrings"] if h["kind"] == "metric"}
    assert affected - moved <= QUIET.get(sid, set()), (sid, sorted(affected - moved))
    unexplained = moved - affected - herrings - {"email-provider", "payment-provider"}  # providers: benign probe blips
    assert not unexplained, (sid, sorted(unexplained))
    for r in w.truth["red_herrings"]:
        if r["kind"] == "change":
            comp = next(c["component"] for c in w.changes if c["change_id"] == r["id"])
            assert comp not in affected


@pytest.mark.parametrize("sid", SIDS)
def test_key_evidence_is_resolvable(worlds, sid):
    w = worlds[sid]
    ids = {c["change_id"] for c in w.changes}
    for e in w.truth["key_evidence"]:
        if e["kind"] == "change":
            assert e["ref"] in ids
        elif e["kind"] == "metric":
            comp, metric = e["ref"].split("/")
            assert (comp, metric) in w.series
        else:
            svc, _, pat = e["ref"].partition(":")
            assert any(ln.service == svc and pat.lower() in ln.message.lower() for ln in w.logs), e
    for h in w.truth["red_herrings"]:
        assert h["id"] in ids or h["kind"] == "metric"
    rc = w.truth["root_cause"]
    if rc and rc["change_id"]:
        assert rc["change_id"] in ids


def test_difficulty_traps_are_present(worlds):
    # S09: the root-cause deploy is 3 days old (outside the default 4h window)
    w9 = worlds["S09"]
    root = next(c for c in w9.changes if c["role"] == "root")
    assert (to_epoch(w9.spec.onset) - root["ts"]) / 86400 == pytest.approx(3.0, abs=0.05)
    assert w9.spec.lookback_days_needed == 3
    # S10: the red-herring deploy is the NEAREST preceding change before the flag change
    w10 = worlds["S10"]
    flag = next(c for c in w10.changes if c["change_id"] == "cfg-7f21")
    herring = next(c for c in w10.changes if c["change_id"] == "chg-reco-4412")
    assert (
        herring["ts"] < flag["ts"]
        and herring["component"] == "recommendation-service"
        and flag["ts"] - herring["ts"] == 60
    )
    # S07: the email provider latency spike is a metric red herring
    assert worlds["S07"].truth["red_herrings"][0]["kind"] == "metric"
    # S13: flat metrics and no logs after the onset for the gap components
    w13 = worlds["S13"]
    for comp in ("payments-service", "orders-service"):
        assert len(set(w13.series[(comp, "error_rate")][90:])) == 1
        assert not [ln for ln in w13.logs if ln.service == comp and ln.ts >= to_epoch(w13.spec.onset)]
    # S15: hostile text lives in a log line, flagged by the untrusted envelope
    inj = [ln for ln in worlds["S15"].logs if ln.kind == "inj"]
    assert inj and "IGNORE PREVIOUS INSTRUCTIONS" in inj[0].message and inj[0].service == "cart-service"
    # S14 happens in the change freeze, S11/S05 outside it
    from foghorn.data.util import in_freeze

    assert (
        in_freeze(to_epoch(worlds["S14"].spec.onset))
        and not in_freeze(to_epoch(worlds["S11"].spec.onset))
        and not in_freeze(to_epoch(worlds["S05"].spec.onset))
    )
    # S02 steps exactly at midnight
    assert worlds["S02"].spec.onset.endswith("T00:00:00Z")


def test_propagation_respects_the_call_graph(worlds):
    w = worlds["S10"]
    e = {k: v for k, v in w.series.items() if k[1] == "error_rate"}
    peak = {c: max(v[90:]) for (c, _m), v in e.items()}
    assert peak["pricing-service"] > peak["cart-service"] > peak["checkout-service"] > peak["edge-gateway"] > 0.03
    assert peak["recommendation-service"] < 0.02 and peak["web-frontend"] < 0.02  # not on the failing path
    # callers degrade AFTER callees
    first_bad = {
        c: next(i for i, x in enumerate(v) if i >= 90 and x > 0.02) for (c, _m), v in e.items() if peak[c] > 0.03
    }
    assert (
        first_bad["pricing-service"]
        <= first_bad["cart-service"]
        <= first_bad["checkout-service"]
        <= first_bad["edge-gateway"]
    )


def test_worlds_are_independent_of_each_other_and_of_dict_order(worlds):
    again = build_world(worlds["S05"].spec)
    assert json.dumps(again.truth, sort_keys=True) == json.dumps(worlds["S05"].truth, sort_keys=True)
    assert again.series == worlds["S05"].series and again.alerts == worlds["S05"].alerts


def test_api_customer_impact_numbers(worlds):
    assert 0.06 <= worlds["S10"].truth["customer_impact"]["checkout_error_rate_peak"] <= 0.09
    assert api.get_truth("S10")["customer_impact"] == worlds["S10"].truth["customer_impact"]


def test_no_unresolved_template_placeholders_anywhere(worlds):
    for sid, w in worlds.items():
        for ln in w.logs:
            assert "{" not in ln.message and "}" not in ln.message, (sid, ln.service, ln.message)
        for rp in w.reports:
            assert "{" not in rp["text"], (sid, rp["text"])
        for c in w.changes:
            assert "{" not in c["summary"], (sid, c["summary"])
        assert not re.search(r"\{\w+\}", json.dumps(w.events)), sid
