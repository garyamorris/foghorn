"""Ingest routes: POST /api/events, webhooks (HMAC, size cap, rate limit, Alertmanager fan-out), presets."""

from __future__ import annotations

import json

import pytest

from foghorn.server.deps import sign

ALERT = {
    "type": "alert.fired",
    "severity": "high",
    "subject": {"component": "checkout-service"},
    "payload": {"rule": "error_rate_high"},
}


def raw(obj) -> bytes:
    return json.dumps(obj).encode()


# ------------------------------------------------------------------------------------------------ POST /api/events
async def test_post_event_accepts_and_returns_routing_outcome(api):
    r = await api.event(**ALERT)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "routed" and body["event_id"] == "evt_0000001" and body["seq"] == 1
    assert body["dispatches"][0] | {"id": "x", "thread_id": "x"} == {
        "id": "x",
        "route": "r-signal",
        "kind": "run",
        "graph_id": "event_router",
        "priority": "P1",
        "status": "queued",
        "attempts": 0,
        "thread_id": "x",
        "run_id": None,
        "incident_id": None,
        "generation": 0,
    }
    got = (await api.get("/api/events/evt_0000001")).json()
    assert got["type"] == "alert.fired" and got["links"]["event_id"] == "evt_0000001"


async def test_idempotency_key_header_and_duplicate_delivery_is_200(api):
    h = {"Idempotency-Key": "k-1"}
    first = await api.http.post("/api/events", json=ALERT, headers=h)
    second = await api.http.post("/api/events", json=ALERT, headers=h)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json() == {"status": "duplicate", "event_id": first.json()["event_id"]}
    assert (await api.get("/api/events")).json()["latest_seq"] == 1


async def test_invalid_envelope_is_422_with_error_shape_and_a_dlq_entry(api):
    r = await api.post("/api/events", json={"type": "alert.fired", "severity": "nuclear"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert (
        err["code"] == "schema_invalid"
        and "severity" in err["message"]
        and err["details"]["dead_letter_id"] == "dlq_0000001"
    )
    assert (await api.get("/api/dlq")).json()["items"][0]["reason"] == "schema_invalid"
    r2 = await api.http.post("/api/events", content=b"{broken", headers={"content-type": "application/json"})
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "schema_invalid"
    r3 = await api.post("/api/events", json={"nope": 1})  # missing `type`
    assert r3.status_code == 422


async def test_backpressure_is_429_with_retry_after(api):
    api.rt.queues._active = True  # noqa: SLF001
    r = await api.event(type="customer.report", severity="low", payload={})
    assert r.status_code == 429 and r.headers["retry-after"] == str(api.rt.es.retry_after_s)
    assert r.json()["error"]["code"] == "backpressure"
    assert (
        await api.event(type="alert.fired", severity="critical", subject={"component": "c"}, payload={"rule": "r"})
    ).status_code == 202


async def test_conflicting_event_id_is_409(api):
    assert (await api.event(type="customer.report", id="same", payload={})).status_code == 202
    r = await api.event(type="customer.report", id="same", source="other", payload={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


# --------------------------------------------------------------------------------------------- webhook security
async def test_signature_present_but_wrong_is_always_401(api):
    body = raw(ALERT)
    r = await api.http.post("/api/events", content=body, headers={"X-Foghorn-Signature": "sha256=bad"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_signature"
    assert (await api.get("/api/events")).json()["items"] == []


async def test_valid_signature_is_accepted(api):
    body = raw(ALERT)
    secret = api.rt.settings.webhook_secret.get_secret_value()
    r = await api.http.post("/api/events", content=body, headers={"X-Foghorn-Signature": sign(body, secret)})
    assert r.status_code == 202
    other = raw({**ALERT, "payload": {"rule": "x"}})
    assert (
        await api.http.post("/api/events", content=other, headers={"X-Foghorn-Signature": sign(body, secret)})
    ).status_code == 401  # signature of a different body


async def test_signature_can_be_required_and_a_bearer_token_is_an_alternative(api, monkeypatch):
    monkeypatch.setenv("FOGHORN_WEBHOOK_REQUIRE_SIGNATURE", "true")
    api.rt.es.webhook_require_signature = True
    body = raw(ALERT)
    r = await api.http.post("/api/events", content=body)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    secret = api.rt.settings.webhook_secret.get_secret_value()
    assert (
        await api.http.post("/api/events", content=body, headers={"X-Foghorn-Signature": sign(body, secret)})
    ).status_code == 202
    monkeypatch.setenv("FOGHORN_DASHBOARD_API_TOKEN", "tok")
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    r2 = await api.http.post(
        "/api/events", content=raw({**ALERT, "payload": {"rule": "b"}}), headers={"Authorization": "Bearer tok"}
    )
    assert r2.status_code == 202  # valid bearer satisfies the requirement
    assert (
        await api.http.post("/api/events", content=body, headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_body_size_cap_declared_and_chunked(api):
    cap = api.rt.settings.webhook_max_body_bytes
    big = b"x" * (cap + 1)
    r = await api.http.post("/api/events", content=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    async def gen():  # chunked: no content-length, the cap must still hold while reading
        for _ in range(3):
            yield b"y" * (cap // 2)

    r2 = await api.http.post("/api/webhooks/alertmanager", content=gen())
    assert r2.status_code == 413
    assert (await api.get("/api/events")).json()["items"] == []


async def test_rate_limit_is_429_with_retry_after(api, monkeypatch):
    monkeypatch.setenv("FOGHORN_WEBHOOK_RATE_PER_MIN", "2")
    from foghorn.settings import get_settings

    get_settings.cache_clear()
    codes = [(await api.event(type="customer.report", payload={"n": i})).status_code for i in range(4)]
    assert codes == [202, 202, 429, 429]
    r = await api.event(type="customer.report", payload={"n": 9})
    assert r.headers["retry-after"] == "30" and r.json()["error"]["code"] == "rate_limited"


# ------------------------------------------------------------------------------------------------ alertmanager
AM = {
    "version": "4",
    "groupKey": '{}:{alertname="error_rate_high"}',
    "status": "firing",
    "receiver": "foghorn",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "error_rate_high", "service": "checkout-service", "severity": "critical"},
            "annotations": {"summary": "errors 7%"},
            "startsAt": "2026-11-05T10:49:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "ab12",
            "generatorURL": "http://prom/x",
        },
        {
            "status": "firing",
            "labels": {"alertname": "latency_p95", "job": "pricing-service", "severity": "warning"},
            "annotations": {},
            "startsAt": "2026-11-05T10:49:30Z",
            "fingerprint": "cd34",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "disk_full", "instance": "db-1", "severity": "page"},
            "annotations": {},
            "startsAt": "2026-11-05T09:00:00Z",
            "endsAt": "2026-11-05T10:50:00Z",
            "fingerprint": "ef56",
        },
    ],
}


async def test_alertmanager_v4_fans_out_into_one_envelope_per_alert(api):
    r = await api.post("/api/webhooks/alertmanager", json=AM)
    assert r.status_code == 202
    body = r.json()
    assert (body["received"], body["accepted"], body["duplicates"], body["suppressed"], body["rejected"]) == (
        3,
        3,
        0,
        0,
        0,
    )
    assert [x["status"] for x in body["results"]] == ["routed", "routed", "routed"]
    rows = (await api.get("/api/events?order=asc")).json()["items"]
    assert [r["type"] for r in rows] == ["alert.fired", "alert.fired", "alert.resolved"]
    a, b, c = rows
    assert (
        a["source"] == "alertmanager"
        and a["severity"] == "critical"
        and a["component"] == "checkout-service"
        and a["occurred_at"] == "2026-11-05T10:49:00Z"
    )
    assert (
        a["payload"]["rule"] == "error_rate_high"
        and a["payload"]["fingerprint"] == "ab12"
        and a["payload"]["generator_url"] == "http://prom/x"
        and a["payload"]["summary"] == "errors 7%"
    )
    assert (
        a["dedupe_key"] == "alert:checkout-service:error_rate_high"
        and a["idempotency_key"] == "am:ab12:2026-11-05T10:49:00Z:firing"
    )
    assert (
        b["severity"] == "medium" and b["component"] == "pricing-service"
    )  # warning -> medium, job label as component
    assert (
        c["severity"] == "critical" and c["occurred_at"] == "2026-11-05T10:50:00Z" and c["component"] == "db-1"
    )  # resolved at endsAt
    # Alertmanager re-sends the same group on its repeat interval: idempotent
    again = (await api.post("/api/webhooks/alertmanager", json=AM)).json()
    assert again["duplicates"] == 3 and again["accepted"] == 0
    assert (await api.get("/api/events")).json()["latest_seq"] == 3


async def test_alertmanager_burst_is_deduped_semantically_and_rejects_non_v4(api):
    burst = {
        **AM,
        "alerts": [
            {**AM["alerts"][0], "fingerprint": f"fp{i}", "startsAt": f"2026-11-05T10:49:{i:02d}Z"} for i in range(5)
        ],
    }
    body = (await api.post("/api/webhooks/alertmanager", json=burst)).json()
    assert body["accepted"] == 1 and body["suppressed"] == 4
    for bad in ({"version": "4"}, {"alerts": "nope"}, {"alerts": [1]}):
        r = await api.post("/api/webhooks/alertmanager", json=bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "schema_invalid"
    r = await api.http.post("/api/webhooks/alertmanager", content=b"not json")
    assert r.status_code == 422


async def test_alertmanager_stops_with_429_and_partial_results_under_backpressure(api):
    orig = api.rt.bus.ingest
    n = {"i": 0}

    async def pushback(raw_, **kw):
        n["i"] += 1
        if n["i"] == 2:
            from foghorn.events.pipeline import IngestResult

            return IngestResult(
                429,
                "backpressure",
                retry_after=7,
                error={"code": "backpressure", "message": "slow down", "details": None},
            )
        return await orig(raw_, **kw)

    api.rt.bus.ingest = pushback  # type: ignore[method-assign]
    r = await api.post("/api/webhooks/alertmanager", json=AM)
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert r.json()["accepted"] == 1 and len(r.json()["results"]) == 1


async def test_alertmanager_requires_a_valid_signature_when_present(api):
    body = raw(AM)
    assert (
        await api.http.post("/api/webhooks/alertmanager", content=body, headers={"X-Foghorn-Signature": "sha256=00"})
    ).status_code == 401
    secret = api.rt.settings.webhook_secret.get_secret_value()
    assert (
        await api.http.post(
            "/api/webhooks/alertmanager", content=body, headers={"X-Foghorn-Signature": sign(body, secret)}
        )
    ).status_code == 202


# ----------------------------------------------------------------------------------------- deploybot / generic
async def test_deploybot_webhook(api):
    r = await api.post(
        "/api/webhooks/deploybot",
        json={
            "event": "deploy.finished",
            "service": "pricing-service",
            "version": "4412",
            "environment": "prod",
            "deploy_id": "dep-9",
            "status": "success",
            "actor": "ci",
            "commit": "ab12",
            "at": "2026-11-05T10:40:00Z",
        },
    )
    assert r.status_code == 202 and r.json()["status"] == "recorded"
    ev = (await api.get("/api/events/" + r.json()["event_id"])).json()
    assert (
        ev["source"] == "deploybot"
        and ev["subject"]["component"] == "pricing-service"
        and ev["payload"]
        == {"version": "4412", "deploy_id": "dep-9", "status": "success", "actor": "ci", "commit": "ab12"}
    )
    assert ev["idempotency_key"] == "deploybot:dep-9:deploy.finished" and ev["occurred_at"] == "2026-11-05T10:40:00Z"
    dup = await api.post(
        "/api/webhooks/deploybot", json={"event": "deploy.finished", "service": "pricing-service", "deploy_id": "dep-9"}
    )
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    rb = await api.post(
        "/api/webhooks/deploybot",
        json={"event": "deploy.rolled_back", "service": "pricing-service", "deploy_id": "dep-10"},
    )
    assert (await api.get("/api/events/" + rb.json()["event_id"])).json()["severity"] == "medium"
    for bad in ({"event": "deploy.exploded", "service": "x"}, {"service": "x"}, {"event": "deploy.started"}):
        r = await api.post("/api/webhooks/deploybot", json=bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "schema_invalid", bad


async def test_generic_webhook_accepts_envelopes_and_a_flat_shorthand(api):
    r = await api.post(
        "/api/webhooks/generic",
        json={
            "type": "customer.report",
            "severity": "medium",
            "subject": {"component": "cart-service"},
            "payload": {"summary": "cart broken"},
        },
    )
    assert r.status_code == 202
    flat = await api.post(
        "/api/webhooks/generic",
        json={"type": "customer.report", "component": "x", "ticket": "T-1", "payload": {"a": 1}},
    )
    ev = (await api.get("/api/events/" + flat.json()["event_id"])).json()
    assert ev["payload"] == {"a": 1, "component": "x", "ticket": "T-1"} and ev["source"] == "webhook"
    assert (await api.http.post("/api/webhooks/generic", content=b"zzz")).status_code == 422


# ------------------------------------------------------------------------------------------------------ presets
async def test_presets_list_and_inject(api):
    presets = (await api.get("/api/events/presets")).json()["items"]
    names = {p["preset"] for p in presets}
    assert {
        "fire_alert",
        "resolve_alert",
        "deploy_finished",
        "flag_changed",
        "maintenance_start",
        "poison_alert",
    } <= names
    assert all("envelope" in p and "label" in p for p in presets)
    r = await api.post(
        "/api/events/inject",
        json={"preset": "fire_alert", "component": "cart-service", "severity": "critical", "payload": {"value": 0.5}},
    )
    assert r.status_code == 202
    ev = (await api.get("/api/events/" + r.json()["event_id"])).json()
    assert ev["source"] == "ui.inject" and ev["component"] == "cart-service" and ev["severity"] == "critical"
    assert (
        ev["payload"]["rule"] == "error_rate_high"
        and ev["payload"]["value"] == 0.5
        and ev["dedupe_key"] == "alert:cart-service:error_rate_high"
    )
    twice = [
        await api.post("/api/events/inject", json={"preset": "customer_report", "idempotency_key": "demo-1"})
        for _ in range(2)
    ]
    assert [t.status_code for t in twice] == [202, 200]  # the dedupe-by-idempotency-key demo
    raw_env = await api.post(
        "/api/events/inject", json={"envelope": {"type": "flag.changed", "payload": {"flag": "f"}}}
    )
    assert raw_env.status_code == 202
    assert (await api.post("/api/events/inject", json={"preset": "nope"})).status_code == 422
    assert (await api.post("/api/events/inject", json={})).status_code == 422
    poison = await api.post("/api/events/inject", json={"preset": "poison_alert"})
    await api.idle()
    assert (await api.get("/api/dlq")).json()["items"][0]["reason"] == "poison" and poison.status_code == 202


@pytest.mark.parametrize(
    "preset",
    [
        "fire_alert",
        "resolve_alert",
        "deploy_finished",
        "deploy_rolled_back",
        "flag_changed",
        "maintenance_start",
        "maintenance_end",
        "customer_report",
        "slo_burn",
        "poison_alert",
    ],
)
async def test_every_preset_is_a_valid_envelope(api, preset):
    r = await api.post("/api/events/inject", json={"preset": preset})
    assert r.status_code == 202, r.text
