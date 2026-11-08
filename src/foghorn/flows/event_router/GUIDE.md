---
graph_id: event_router
title: Event Router
summary: The single ingress for events - validate, dedupe through the Store ledger, enrich in parallel, classify with explainable rules (LLM only for ambiguous signals), then open, join, ignore or escalate a storm.
features: [Send fan-out, custom reducer, RetryPolicy(retry_on), CachePolicy(key_func), error_handler, conditional-edge labels, Store ledger, delegate (background child thread), context_schema + editable prompt, nostream structured output]
studio_steps:
  - "Open event_router and pick a sample input (S01 alert). The form comes from RouterInput; the graph shows labelled edges (reject, skip_enrichment, open_incident, join_incident, ignore, escalate_storm)."
  - "Run it. In the trace expand the three parallel enrich tasks (topology, oncall, changes): each is a Send, merged by a custom reducer; the second run of the same component shows cached tasks."
  - "Open the finished thread: result.artifacts has the triage table, the ordered decision_trace (which rule fired and why) and the classification. result.metrics.noise_probability is the router's noise estimate."
  - "Find the child thread: result.child_threads holds the incident_commander thread (id = det_thread_id(incident_commander, incident_id)); its metadata carries foghorn_incident_id, foghorn_correlation_id, foghorn_scenario_id and foghorn_event_id."
  - "Run the second sample (a neighbour alert): the router JOINS the incident (incident.updated) and starts no second commander."
  - "Run the S04 maintenance sample: the maintenance window found in the change feed suppresses the alert. Run it with context chaos=tools to watch RetryPolicy heal the first attempt of each enrich branch."
  - "Time travel: open the thread history, pick the checkpoint before decide, edit ledger.maintenance_windows (or ledger.open_incidents) and fork - the same event takes a different route."
  - "Gear on node classify (assistant Context): edit classify_prompt, or switch model_profile to naive / cautious / gullible and run the unknown-component sample to see how the fallback verdict changes."
sample_inputs:
  - name: "S01 error-rate alert: opens an incident and starts the commander"
    input: {"event": {"attributes": {}, "causation_id": null, "correlation_id": null, "dedupe_key": "alert:checkout-service:error_rate_high", "id": "evt_S01_0013", "idempotency_key": null, "occurred_at": "2026-11-05T14:07:00Z", "payload": {"alert_id": "al_S01_0002", "for_s": 180, "labels": {"kind": "service", "team": "commerce", "tier": "0"}, "metric": "error_rate", "rule": "error_rate_high", "threshold": 0.02, "value": 0.09441}, "received_at": null, "schema_version": "foghorn.event/1", "severity": "high", "source": "alertmanager", "subject": {"component": "checkout-service", "environment": "prod"}, "trace": {"scenario_id": "S01", "seq": 13, "stream": null}, "type": "alert.fired"}}
  - name: "S01 second alert on a neighbour: joins the incident (run the first sample before this one)"
    input: {"event": {"attributes": {}, "causation_id": null, "correlation_id": null, "dedupe_key": "alert:edge-gateway:error_rate_high", "id": "evt_S01_0016", "idempotency_key": null, "occurred_at": "2026-11-05T14:08:00Z", "payload": {"alert_id": "al_S01_0005", "for_s": 180, "labels": {"kind": "service", "team": "platform", "tier": "0"}, "metric": "error_rate", "rule": "error_rate_high", "threshold": 0.02, "value": 0.07104}, "received_at": null, "schema_version": "foghorn.event/1", "severity": "high", "source": "alertmanager", "subject": {"component": "edge-gateway", "environment": "prod"}, "trace": {"scenario_id": "S01", "seq": 16, "stream": null}, "type": "alert.fired"}}
  - name: "S04 alert inside a maintenance window: suppressed"
    input: {"event": {"attributes": {}, "causation_id": null, "correlation_id": null, "dedupe_key": "alert:orders-db:p95_latency_high", "id": "evt_S04_0014", "idempotency_key": null, "occurred_at": "2026-11-02T03:10:00Z", "payload": {"alert_id": "al_S04_0003", "for_s": 180, "labels": {"kind": "datastore", "team": "data-infra", "tier": "0"}, "metric": "p95_ms", "rule": "p95_latency_high", "threshold": 40.0, "value": 63.14}, "received_at": null, "schema_version": "foghorn.event/1", "severity": "high", "source": "alertmanager", "subject": {"component": "orders-db", "environment": "prod"}, "trace": {"scenario_id": "S04", "seq": 14, "stream": null}, "type": "alert.fired"}}
  - name: "S04 benign synthetic-probe flapping: ignored"
    input: {"event": {"attributes": {}, "causation_id": null, "correlation_id": null, "dedupe_key": "alert:email-provider:synthetic_probe_failed", "id": "evt_S04_0002", "idempotency_key": null, "occurred_at": "2026-11-02T01:39:00Z", "payload": {"alert_id": "al_S04_0001", "for_s": 0, "labels": {"kind": "external", "team": "messaging", "tier": "2"}, "metric": "error_rate", "rule": "synthetic_probe_failed", "threshold": 0.15, "value": 0.23323}, "received_at": null, "schema_version": "foghorn.event/1", "severity": "low", "source": "alertmanager", "subject": {"component": "email-provider", "environment": "prod"}, "trace": {"scenario_id": "S04", "seq": 2, "stream": null}, "type": "alert.fired"}}
  - name: "Unknown component: ambiguous, asks the fast model (tagged nostream)"
    input: {"event": {"schema_version": "foghorn.event/1", "id": "evt_demo_unknown", "type": "alert.fired", "source": "alertmanager", "severity": "high", "occurred_at": "2026-11-05T14:07:00Z", "subject": {"component": "mystery-service", "environment": "prod"}, "payload": {"rule": "mystery_rule", "value": 0.4, "threshold": 0.1}, "dedupe_key": "alert:mystery-service:mystery_rule", "trace": {"scenario_id": "S01"}, "attributes": {}}}
  - name: "Invalid envelope: rejected with a failed result"
    input: {"event": {"type": "alert.fired", "bogus": true}}
---
# event_router

One run handles one event (`RouterInput {event}`, thread id = `det_thread_id("event_router", event_id)`). The dispatcher
reads `values.result` after the run and emits `triage.completed` from it.

```
START -> validate --reject--> finish
            | ok
         ledger --skip_enrichment--> classify          (non-signals, re-deliveries)
            | Send x3
         enrich (topology | oncall | changes) -> classify -> decide
                                                               |-- open_incident -> finish
                                                               |-- join_incident -> finish
                                                               |-- ignore        -> finish
                                                               `-- escalate_storm -> finish
```

## Decision rules (first match wins) - `flows/event_router/rules.py`

| # | Rule | decision | route |
|---|---|---|---|
| 1 | maintenance window covers the component (change feed or ledger) | `suppress_maintenance` | ignore |
| 2 | same `dedupe_key` seen < 300 s ago (sliding) | `dedupe` (result status `duplicate`) | ignore |
| 3 | severity `info` | `ignore_info` | ignore |
| 4 | `synthetic_probe_failed` with no corroborating alert on the component in 600 s | `suppress_flapping` | ignore |
| 5 | an open incident covers the component or a call-graph neighbour | `attach` | join_incident |
| 6 | `customer.report` nothing explains | `escalate_human` (SEV3, flagged for a person) | open_incident |
| 7 | severity `low` | `ignore_info` | ignore |
| 8 | otherwise (ambiguous signals ask the fast model, see below) | `open_incident` | open_incident |

Storm override: the 8th distinct alert within 60 virtual seconds routes to `escalate_storm` (a background
`storm_correlator` run on a pinned thread); further openings while the storm is open are folded into it.

## Severity matrix

| event severity | tier-0 service | other tier 0 (datastore, queue, external) | tier 1 | tier 2 |
|---|---|---|---|---|
| critical | SEV1 | SEV2 | SEV2 | SEV2 |
| high | SEV2 | SEV2 | SEV3 | SEV3 |
| medium | SEV3 | SEV3 | SEV3 | SEV4 |

The rules reproduce the 400 labelled triage cases (D8) exactly. An incident stays joinable for 60 virtual minutes and its reach
grows by one ring of joined neighbours. A later, louder signal raises the incident severity (`incident.updated`).

## Ambiguous signals and the LLM fallback
Unknown component, or an alert rule the table has never seen, makes the signal ambiguous. Only then does `classify` call
`structured(get_chat_model("fast"), RouteDecision)` tagged `nostream`. Offline it is a Script that reads the facts it was given;
profiles differ (competent follows the evidence, naive is alarmist, cautious hands it to a human, gullible obeys text found in
the payload). If the model fails or `use_llm_fallback` is off, the severity matrix is used (fail safe).

## Resilience
* `enrich` retries `ConnectionError`, `TimeoutError`, `sqlite3.OperationalError`, `httpx.TransportError` (3 attempts); a branch that still fails goes to its `error_handler`
  and the run carries on (`metrics.enrichment_degraded`).
* `CachePolicy(key_func)`: scenario + component + 5-minute virtual bucket. The cache is global across threads, hence the scenario in the key.
* Re-delivery of the same event id is a `duplicate`; a restarted run reuses the ledgered incident, so exactly one commander run exists per incident.
* Not a blocking call anywhere: tools run in sync nodes (threads); the SDK delegation is awaited.
