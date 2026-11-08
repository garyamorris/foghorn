"""Schema validity: every generated event is a valid ``EventEnvelope.to_state()`` dict; streams and their labels line up."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foghorn.contracts.events import ALL_TYPES, SEVERITY_ORDER, EventEnvelope
from foghorn.data import api

SIDS = [f"S{i:02d}" for i in range(1, 17)]


def _roundtrip(e: dict) -> None:
    env = EventEnvelope.model_validate(e)
    assert env.to_state() == e  # the exact state-dict shape (all keys, None included)
    assert env.type in ALL_TYPES and env.severity in SEVERITY_ORDER


@pytest.mark.parametrize("sid", SIDS)
def test_scenario_events_validate_and_are_ordered(sid):
    events = api.scenario_events(sid)
    assert events
    for e in events:
        _roundtrip(e)
        assert e["trace"]["scenario_id"] == sid and e["received_at"] is None and e["subject"]["environment"] == "prod"
    ids = [e["id"] for e in events]
    assert ids == [f"evt_{sid}_{n:04d}" for n in range(1, len(ids) + 1)]
    times = [e["occurred_at"] for e in events]
    assert times == sorted(times)
    window = api.get_scenario(sid)["world"]["window"]
    assert all(window[0] <= t < window[1] for t in times)


def test_alert_events_carry_the_rule_payload():
    ev = [e for e in api.scenario_events("S10") if e["type"] == "alert.fired"]
    assert ev
    for e in ev:
        p = e["payload"]
        assert {"alert_id", "rule", "metric", "value", "threshold", "for_s", "labels"} <= set(p)
        assert e["dedupe_key"] == f"alert:{e['subject']['component']}:{p['rule']}"
    types = {e["type"] for e in api.scenario_events("S10")}
    assert {"flag.changed", "deploy.finished", "alert.fired"} <= types


def test_s04_has_a_maintenance_window_and_s13_only_customer_reports():
    t4 = [e["type"] for e in api.scenario_events("S04")]
    assert "maintenance.window.started" in t4 and "maintenance.window.ended" in t4
    t13 = {e["type"] for e in api.scenario_events("S13")}
    assert "customer.report" in t13 and not (t13 & {"healthcheck.failed", "slo.burn_rate"})
    reports = [e for e in api.scenario_events("S13") if e["type"] == "customer.report"]
    assert len(reports) >= 8 and all(r["payload"]["text"] for r in reports)


@pytest.mark.parametrize("name", ["stream_quiet_hour", "stream_workday", "stream_storm"])
def test_streams_are_valid_ordered_and_labelled(name):
    info = next(s for s in api.list_streams() if s["name"] == name)
    events = list(api.iter_stream(name))
    labels = list(api.iter_stream_labels(name))
    assert len(events) == len(labels) == info["events"]
    assert [e["id"] for e in events] == [lab["id"] for lab in labels]
    times = [e["occurred_at"] for e in events]
    assert times == sorted(times) and times[0] >= info["start"] and times[-1] < info["end"]
    poison_ids = {lab["id"] for lab in labels if lab["poison"]}
    for e, lab in zip(events, labels, strict=True):
        assert e["trace"]["stream"] == name and e["trace"]["seq"] == int(e["id"].rsplit("_", 1)[1])
        if e["id"] in poison_ids and lab.get("poison_kind") == "malformed":
            with pytest.raises(ValidationError):
                EventEnvelope.model_validate(e)
        else:
            _roundtrip(e)
        assert lab["expected_route"] in {"triage", "storm", "ledger", "maintenance", "handoff", "resolution", "none"}
    assert len({e["id"] for e in events}) == len(events)


def test_stream_scenario_events_keep_their_real_times():
    s10 = {e["id"]: e for e in api.scenario_events("S10")}
    wd = [e for e in api.iter_stream("stream_workday") if e["trace"]["scenario_id"] == "S10"]
    assert len(wd) == len(s10)
    assert {e["occurred_at"] for e in wd} == {e["occurred_at"] for e in s10.values()}
    assert {e["trace"]["scenario_id"] for e in api.iter_stream("stream_workday")} == {None, "S10", "S01"}


def test_storm_stream_semantics():
    events = list(api.iter_stream("stream_storm"))
    labels = list(api.iter_stream_labels("stream_storm"))
    assert sum(lab["poison"] for lab in labels) == 3
    assert sorted(lab["poison_kind"] for lab in labels if lab["poison"]) == ["handler", "malformed", "malformed"]
    handler = next(e for e, lab in zip(events, labels, strict=True) if lab.get("poison_kind") == "handler")
    assert handler["attributes"]["chaos"] == "poison"
    decisions = [lab["expected_decision"] for lab in labels]
    routed = [d for d in decisions if d not in (None, "dedupe")]
    assert 0 < len(routed) <= 80  # B2: 650 in -> at most 80 routed deliveries
    assert decisions.count("dedupe") > 300
    assert any(lab["storm"] and lab["expected_route"] == "storm" for lab in labels)
    assert sum(1 for e in events if e["severity"] == "critical") > 0  # P0 events exist and are not deduped away
    first_crit = next(
        lab
        for e, lab in zip(events, labels, strict=True)
        if e["severity"] == "critical" and lab["expected_decision"] != "dedupe"
    )
    assert first_crit["expected_priority"] in {"P0", "P1"}


def test_quiet_hour_contains_the_maintenance_noise_of_s04():
    labels = {lab["id"]: lab for lab in api.iter_stream_labels("stream_quiet_hour")}
    s4 = [e for e in api.iter_stream("stream_quiet_hour") if e["trace"]["scenario_id"] == "S04"]
    assert any(labels[e["id"]]["expected_decision"] == "suppress_maintenance" for e in s4)
