"""Eval datasets: D8 triage cases (and the reference policy behind them), D9 RAG with lexical traps, D10 safety, D11 seeds."""

from __future__ import annotations

from collections import Counter

import pytest

from foghorn.data import api, catalog
from foghorn.data import events as ev
from foghorn.data.triage_rules import expected_route, neighbours, reference_triage
from foghorn.tools import api as tools


def _read(name: str) -> list[dict]:
    return api.read_dataset(name, 0, 500, reveal_truth=True)["records"]


# --- triage ---------------------------------------------------------------------------------------------------------------------------------
def event(
    typ="alert.fired",
    comp="checkout-service",
    sev="high",
    rule="error_rate_high",
    key="alert:checkout-service:error_rate_high",
    at="2026-11-05T10:49:00Z",
):
    return ev.envelope(
        id="evt_x",
        type=typ,
        source="alertmanager",
        severity=sev,
        occurred_at=at,
        component=comp,
        payload={"rule": rule},
        dedupe_key=key,
    )


EMPTY = {"open_incidents": [], "maintenance_windows": [], "recent_alert_keys": []}


def test_triage_cases_cover_every_decision_and_are_self_consistent():
    cases = _read("triage_cases")
    assert len(cases) == 400 and len({c["case_id"] for c in cases}) == 400
    assert Counter(c["expected"]["decision"] for c in cases) == {
        "open_incident": 150,
        "attach": 60,
        "dedupe": 60,
        "suppress_maintenance": 45,
        "suppress_flapping": 35,
        "escalate_human": 25,
        "ignore_info": 25,
    }
    for c in cases:
        a = c["alert"]
        e = ev.envelope(
            id="evt_c",
            type=a["type"],
            source="alertmanager",
            severity=a["severity"],
            occurred_at=a["fired_at"],
            component=a["component"],
            payload={k: v for k, v in a.items() if k in ("rule", "value", "threshold", "metric", "text", "ticket_id")},
            dedupe_key=a["dedupe_key"],
        )
        got = reference_triage(e, c["context"])
        assert {k: got[k] for k in c["expected"]} == c["expected"], c["case_id"]
        assert a["component"] in catalog.COMPONENTS
        if c["expected"]["decision"] == "attach":
            assert c["expected"]["attach_to"] == c["context"]["open_incidents"][0]["id"]
        if c["expected"]["decision"] == "open_incident":
            assert c["expected"]["severity"] in ("SEV1", "SEV2", "SEV3", "SEV4")


def test_reference_triage_rules_in_order():
    maint = {
        **EMPTY,
        "maintenance_windows": [
            {"component": "checkout-service", "start": "2026-11-05T10:00:00Z", "end": "2026-11-05T11:00:00Z"}
        ],
    }
    dup = {
        **EMPTY,
        "recent_alert_keys": [
            {
                "key": "alert:checkout-service:error_rate_high",
                "age_s": 120,
                "event_id": "evt_1",
                "component": "checkout-service",
            }
        ],
    }
    assert reference_triage(event(), EMPTY)["decision"] == "open_incident"
    assert reference_triage(event(), maint)["decision"] == "suppress_maintenance"
    assert (
        reference_triage(event(at="2026-11-05T11:00:01Z"), maint)["decision"] == "open_incident"
    )  # the window has ended
    got = reference_triage(event(), dup)
    assert got["decision"] == "dedupe" and got["dedupe_of"] == "evt_1"
    assert reference_triage(event(), {**dup, **maint})["decision"] == "suppress_maintenance"  # maintenance beats dedupe
    old = {**EMPTY, "recent_alert_keys": [{**dup["recent_alert_keys"][0], "age_s": 300}]}
    assert reference_triage(event(), old)["decision"] == "open_incident"  # the 300 s window is exclusive
    assert reference_triage(event(sev="info"), EMPTY)["decision"] == "ignore_info"
    assert (
        reference_triage(event(sev="low", comp="email-provider", rule="synthetic_probe_failed"), EMPTY)["decision"]
        == "suppress_flapping"
    )
    corroborated = {
        **EMPTY,
        "recent_alert_keys": [
            {
                "key": "alert:email-provider:error_rate_high",
                "age_s": 200,
                "event_id": "evt_2",
                "component": "email-provider",
            }
        ],
    }
    assert (
        reference_triage(
            event(
                sev="low",
                comp="email-provider",
                rule="synthetic_probe_failed",
                key="alert:email-provider:synthetic_probe_failed",
            ),
            corroborated,
        )["decision"]
        != "suppress_flapping"
    )
    inc = {**EMPTY, "open_incidents": [{"id": "INC-D0001", "components": ["payments-service"], "severity": "SEV2"}]}
    got = reference_triage(event(), inc)
    assert got["decision"] == "attach" and got["attach_to"] == "INC-D0001"  # payments is a callee of checkout
    far = {**EMPTY, "open_incidents": [{"id": "INC-D0002", "components": ["email-provider"], "severity": "SEV3"}]}
    assert reference_triage(event(), far)["decision"] == "open_incident"
    report = event(typ="customer.report", sev="medium", rule=None, key="report:x")
    assert (
        reference_triage(report, EMPTY)["decision"] == "escalate_human"
        and reference_triage(report, EMPTY)["severity"] == "SEV3"
    )
    assert reference_triage(event(sev="low"), EMPTY)["decision"] == "ignore_info"
    sev = {
        c: reference_triage(event(sev="critical", comp=c), EMPTY)["severity"]
        for c in ("checkout-service", "recommendation-service")
    }
    assert sev == {"checkout-service": "SEV1", "recommendation-service": "SEV2"}
    assert reference_triage(event(sev="medium", comp="recommendation-service"), EMPTY)["severity"] == "SEV4"
    assert (
        neighbours("orders-db") >= {"orders-db", "payments-service"}
        and neighbours(None) == set()
        and neighbours("nope") == set()
    )


def test_expected_route_vocabulary():
    assert expected_route(event()) == "triage" and expected_route(event(typ="alert.resolved")) == "resolution"
    assert (
        expected_route(event(typ="deploy.finished")) == "ledger"
        and expected_route(event(typ="maintenance.window.started")) == "maintenance"
    )
    cron = ev.envelope(
        id="c",
        type="system.cron.tick",
        source="cron",
        severity="info",
        occurred_at="2026-11-05T00:00:00Z",
        component=None,
        payload={"job": "shift_handoff"},
        dedupe_key=None,
    )
    assert expected_route(cron) == "handoff" and expected_route({**cron, "payload": {"job": "heartbeat"}}) == "none"


# --- RAG ---------------------------------------------------------------------------------------------------------------------------------------
def test_rag_questions_hit_at_3_and_the_six_lexical_traps():
    qa = _read("rag_qa")
    assert len(qa) == 48 and len({q["qid"] for q in qa}) == 48
    traps = [q for q in qa if q["must_not_top1"]]
    assert len(traps) == 6 and all(q["must_not_top1"] == ["RB-017"] for q in traps)
    raw_hits = 0
    for q in qa:
        ids = [h["id"] for h in tools.search_runbooks(q["question"], k=3)["hits"]]
        raw_hits += any(r in ids for r in q["expected_runbooks"])
    assert raw_hits / len(qa) >= 0.9  # D9 / G4: hit@3 >= 0.9 for the raw lexical tool
    for q in traps:  # the trap is real: lexically RB-017 wins ...
        raw = [h["id"] for h in tools.search_runbooks(q["question"], k=3)["hits"]]
        assert raw[0] == "RB-017", (q["qid"], raw)
        # ... and a knowledge agent that reads `never_automatic` and filters it gets the right runbook first
        safe = [h["id"] for h in tools.search_runbooks(q["question"], k=3, exclude_never_automatic=True)["hits"]]
        assert "RB-017" not in safe and any(r in safe for r in q["expected_runbooks"]), (q["qid"], safe)


def test_every_runbook_is_findable_by_its_own_symptoms():
    for rb in api.memory_seed_items("runbooks"):
        meta_title = rb["value"]["title"]
        top = [h["id"] for h in tools.search_runbooks(meta_title, k=3)["hits"]]
        assert rb["key"] in top, (rb["key"], meta_title, top)


# --- safety + seeds + sweep ------------------------------------------------------------------------------------------------------------------------
def test_safety_cases_shape():
    cases = _read("safety_cases")
    assert len(cases) == 16 and [c["case_id"] for c in cases] == [f"SC-{i:02d}" for i in range(1, 17)]
    assert (
        cases[2]["kind"] == "prompt_injection" and cases[2]["input_ref"] == "S15" and cases[2]["vector"] == "log_line"
    )
    assert cases[8]["kind"] == "forbidden_action" and "restart the orders db primary" in cases[8]["input"]
    assert {c["kind"] for c in cases} >= {
        "prompt_injection",
        "forbidden_action",
        "approval_bypass",
        "freeze_violation",
        "pii",
        "comms",
        "scope",
        "secrets",
    }
    assert all(c["must"] and c["must_not"] and (c.get("input") or c.get("input_ref")) for c in cases)
    assert any("flag:prompt_injection_detected" in c["must"] for c in cases)


@pytest.mark.parametrize("kind", ["prefs", "lessons", "people"])
def test_seed_memory_keys_are_unique_per_namespace(kind):
    items = api.memory_seed_items(kind)
    keys = [(tuple(i["namespace"]), i["key"]) for i in items]
    assert len(keys) == len(set(keys)) and items
