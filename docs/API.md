# Foghorn HTTP API (`/api/*`)

Served by the custom FastAPI app (`langgraph.json` `http.app` = `src/foghorn/server/app.py:app`) **next to** the
built-in Agent Server API on the same origin and port (`make dev` -> `http://127.0.0.1:2026`). Threads, runs, run
streams, store, assistants, `/assistants/{id}/graph?xray=true` etc. are **not** re-implemented: the SPA calls the
built-in routes directly. This document covers only what Foghorn adds.

Source of truth for behaviour: `src/foghorn/events/**` and `src/foghorn/server/**`. Event model: `docs/design/03`
section 3. If this file and the code disagree, that is a bug in one of them: fix both.

## 0. Conventions

* JSON in, JSON out, UTF-8. Timestamps are ISO-8601 UTC strings. `occurred_at` / `virtual_now` / `as_of` are
  **virtual** time (the simulated world's clock); `received_at`, `created_at`, `wall_now` are real server time.
* Base path `/api`. Unversioned. Unknown query params are ignored.
* **Errors** always have this shape (never FastAPI's `{"detail": ...}`):

```json
{"error": {"code": "schema_invalid", "message": "payload.rule: Input should be a valid string", "details": null}}
```

| HTTP | `error.code` (non-exhaustive) | When |
|---|---|---|
| 400 | `bad_request` | malformed query/body that is not a schema problem |
| 401 | `unauthorized` | token required and missing/wrong; bad webhook signature (`bad_signature`) |
| 404 | `not_found` | unknown event / incident / approval / dlq id ... |
| 409 | `stale_interrupt`, `replay_active`, `conflict` | resume of an interrupt that is no longer pending; second replay while one runs; event `id` already used by another source; DLQ item already resolved |
| 413 | `payload_too_large` | body over `FOGHORN_WEBHOOK_MAX_BODY_BYTES` (default 256 000) |
| 422 | `schema_invalid`, `invalid_response`, `no_route` | envelope/body fails validation (ingest failures are also written to the DLQ), resume value fails the interrupt's schema, DLQ retry of an event no rule routes |
| 429 | `backpressure`, `rate_limited` | queue above high-water (P2/P3 only) / webhook rate limit; always with `Retry-After: <seconds>` |
| 500 | `internal_error` | a bug; the response never leaks internals |
| 502 | `data_unavailable` | the data layer answered with something unusable |
| 503 | `not_ready`, `data_unavailable` | events runtime / Agent Server not ready yet, Store unreachable / `foghorn.data` not importable |

* **Auth.** If `FOGHORN_DASHBOARD_API_TOKEN` is unset (dev default) everything is open. If set, every `/api/*` route
  requires `Authorization: Bearer <token>` (constant-time compare) and answers `401` + `WWW-Authenticate: Bearer`
  otherwise. Exceptions: `GET /healthz`, `GET /`, `/app/*` are always public. `EventSource` cannot send headers, so
  `GET /api/stream` additionally accepts `?access_token=<token>`; prefer `fetch()` + a stream reader with the header.
  Webhook routes (`/api/webhooks/*`, `POST /api/events`) accept **either** a valid bearer token **or** a valid HMAC
  signature (below); with neither a token nor `FOGHORN_WEBHOOK_REQUIRE_SIGNATURE=true` configured they are open.
* **HMAC (optional, webhooks and `POST /api/events`).** `X-Foghorn-Signature: sha256=<hex>` where hex =
  `HMAC_SHA256(FOGHORN_WEBHOOK_SECRET, raw_request_body)`. A signature header that is present but wrong is always
  `401 bad_signature`. Missing signature is `401` only when `FOGHORN_WEBHOOK_REQUIRE_SIGNATURE=true` (default
  `false`) and no valid bearer token accompanies the call. Body cap 256 KB (`413`), rate limit
  `FOGHORN_WEBHOOK_RATE_PER_MIN` (default 120/min per client IP; `429` + `Retry-After`).
* **Idempotency.** `Idempotency-Key: <string>` header (or body field `idempotency_key`) on any ingest route.
  A second delivery returns `200 {"status":"duplicate","event_id":...}` and does nothing.
* Server-assigned ids: events `evt_0000123`, dispatches `dsp_0000012`, dead letters `dlq_0000003`, incidents are
  minted by the graphs (`INC-...`; `INC-D0001`... if a graph omitted it), approvals `apr_...` (from the interrupt
  payload), eval runs `evr_0001`, launches `lau_0001`.

## 1. Meta

### `GET /` -> `307 /app/`
### `GET /healthz` (public)
```json
{"status": "ok", "provider": "offline"}
```
### `GET /api/health`
```json
{"status": "ok", "ready": true,
 "components": {"state_db": "ok", "dispatcher": "running", "scheduler": "running", "replay": "idle",
                "agent_server": "ready", "data_api": "ok", "seeding": "done"},
 "uptime_s": 812.4}
```
`status` is `ok` | `degraded`. `agent_server` is `waiting` | `ready` | `unavailable`; `seeding` is `pending` |
`done` | `failed`. `data_api` is `ok` | `unavailable` (graceful: scenarios/datasets answer `503 data_unavailable`).

### `GET /api/config` (no secrets)
```json
{"environment": "dev", "provider": "offline", "public_base_url": "http://127.0.0.1:2026",
 "studio": {"url": "https://smith.langchain.com/studio/",
            "base_link": "https://smith.langchain.com/studio/?baseUrl=http%3A%2F%2F127.0.0.1%3A2026",
            "thread_url_template": null, "thread_url_verified": false},
 "auth_required": false, "webhook_signature_required": false,
 "model_profiles": ["competent", "naive", "cautious", "gullible"],
 "approval_policies": ["strict", "standard", "auto_low_risk"],
 "graph_ids": ["event_router", "incident_commander", "..."],
 "defaults": {"model_profile": "competent", "approval_policy": "standard", "replay_speed": 60,
              "token_delay_ms": 0, "chaos_mode": false},
 "events": {"queue_caps": {"P0": 500, "P1": 500, "P2": 1000, "P3": 200}, "high_water": 0.8, "low_water": 0.5,
            "max_runs": 4, "per_graph_max_runs": {"incident_commander": 3, "eval_runner": 1},
            "dedupe_window_s": 300, "max_attempts": 3, "backoff_s": [1, 4, 16], "ambient_enabled": false},
 "limits": {"webhook_max_body_bytes": 256000, "webhook_rate_per_min": 120}}
```
`studio.thread_url_template` is `null` unless `FOGHORN_STUDIO_THREAD_URL` is set (per-thread deep-link format is
unverified): the UI must always offer "Copy thread id" and the base link as fallback.

### `GET /api/summary` (Mission Control KPI tiles, one cheap call)
```json
{"open_incidents": 2, "pending_approvals": 1, "events_per_min": 38.0, "events_total": 1204,
 "queue": {"depth": {"P0": 0, "P1": 1, "P2": 2, "P3": 0}, "in_flight": 1}, "dlq_open": 0, "shed_total": 0,
 "last_eval": {"eval_run_id": "evr_0001", "status": "finished", "mean_score": 0.88}, "latest_seq": 1204}
```
`pending_approvals` is the SQLite projection; use `GET /api/approvals` for the authoritative list.

### `GET /api/clock`  /  `POST /api/clock`
```json
{"virtual_now": "2026-11-05T10:49:12Z", "wall_now": "2026-11-08T14:21:07.318Z", "speed": 60.0,
 "paused": false, "mode": "free", "replay": null}
```
`mode` = `free` (clock runs at `speed`) or `replay` (a replay session drives it). `replay` is the object from
`GET /api/replay` while a session exists, else `null`. `POST /api/clock` body (all optional):
`{"virtual_now": "<iso>", "speed": 10, "paused": false}` -> the new clock object. `speed` must be `> 0` and `<= 600`.

## 2. Events

### Envelope (`foghorn.event/1`) - request body of `POST /api/events`
Exactly `foghorn.contracts.events.EventEnvelope` (unknown fields are rejected with 422):
```json
{"schema_version": "foghorn.event/1", "id": null, "type": "alert.fired", "source": "alertmanager",
 "severity": "high", "occurred_at": "2026-11-05T10:49:00Z", "received_at": null,
 "subject": {"component": "checkout-service", "environment": "prod"},
 "payload": {"rule": "error_rate_high", "value": 0.071, "threshold": 0.02},
 "dedupe_key": "alert:checkout-service:error_rate_high", "idempotency_key": null,
 "correlation_id": null, "causation_id": null,
 "trace": {"stream": null, "seq": null, "scenario_id": null}, "attributes": {"chaos": null}}
```
Only `type` is required. `id` (server assigns `evt_<n>`), `occurred_at` (defaults to the virtual clock now),
`received_at` (always overwritten with real time) are filled in. `severity`: `info|low|medium|high|critical`.
`attributes.chaos` may be `"poison"` (dispatch always fails -> DLQ), `"fail_once"`, `"slow"`.
Default `dedupe_key` for signal events without one: `<type-prefix>:<component>:<payload.rule>` (only when
`payload.rule` exists). `attributes.ambient = true` marks background noise (stateless runs, deleted on completion).

### `POST /api/events`
Query/headers: `Idempotency-Key`, `X-Foghorn-Signature`. Responses:

| Status | Body |
|---|---|
| `202` | accepted, see below |
| `200` | `{"status": "duplicate", "event_id": "evt_0000007"}` (same `idempotency_key` or same `(source, id)`) |
| `422` | `schema_invalid` + `details.dead_letter_id` (raw body kept in the DLQ) |
| `429` | `backpressure` (only if every dispatch this event would create is P2/P3 and the queues are above high-water), `Retry-After` |

```json
{"event_id": "evt_0000431", "seq": 431, "status": "routed", "reason": null, "duplicate_of": null,
 "dispatches": [{"id": "dsp_0000012", "route": "r-signal", "graph_id": "event_router", "priority": "P1",
                 "status": "queued", "thread_id": "d59c7787-97e8-53f1-91dc-041daf3f27c9"}]}
```
`status`: `routed` (>=1 dispatch queued) | `recorded` (stored; nothing to run: lifecycle, change ledger, system) |
`suppressed` (`reason`: `semantic_duplicate` | `maintenance_window` | `storm_member`; `duplicate_of` = the first
event id, only for semantic duplicates) | `shed` (accepted, then dropped at the queue hard cap; an `event.shed` marker
event exists) | `dlq` (`reason`: `no_route`; `dead_letter_id` present). The same words are the event row's `status`
(plus `accepted` transiently); a dispatch that dead-letters later flips its event to `dlq`.
Order of checks: validate -> delivery idempotency -> semantic dedupe -> maintenance windows -> routing rules ->
backpressure -> store. Semantic dedupe: same `dedupe_key` + same `type` within 300 **virtual** seconds of the last
sighting (an `alert.resolved` closes the window); scoped per launch so re-running a scenario is never swallowed.
Maintenance suppression covers `alert.fired`, `slo.burn_rate` and `healthcheck.failed` for the window's component
(window component omitted = everything); `customer.report` is never suppressed.

### `POST /api/events/inject`
UI helper for presets. Body `{"preset": "fire_alert", "component": "checkout-service", "severity": "high",
"payload": {...}, "idempotency_key": null, "correlation_id": null}`; presets: `fire_alert`, `resolve_alert`,
`deploy_finished`, `deploy_rolled_back`, `flag_changed`, `maintenance_start`, `maintenance_end`, `customer_report`,
`slo_burn`, `poison_alert`. Omit `preset` and pass `envelope` for a raw envelope. Same response as `POST /api/events`
(source is `ui.inject`).

`GET /api/events/presets` -> `{"items": [{"preset": "fire_alert", "label": "Fire alert", "envelope": {...}}]}`
(templates the form can pre-fill).

### `GET /api/events`
Query: `type` (csv; `*` globs, e.g. `deploy.*,alert.fired`), `component`, `severity` (csv exact),
`min_severity`, `status` (csv), `source`, `scenario_id`, `correlation_id`, `q` (substring over type, component,
payload text), `since` (seq, exclusive: newer than), `before` (seq, exclusive), `limit` (default 100, max 500),
`order` (`desc` default | `asc`).
```json
{"items": [ <event row> ], "latest_seq": 1204, "next_before": 1105}
```
`next_before` is the smallest `seq` returned (pass as `before` for the next page) or `null` when exhausted.

**Event row** (list, get and SSE all use it):
```json
{"seq": 431, "id": "evt_0000431", "schema_version": "foghorn.event/1", "type": "alert.fired",
 "source": "alertmanager", "severity": "high", "occurred_at": "2026-11-05T10:49:00Z",
 "received_at": "2026-11-08T14:21:07.318Z",
 "subject": {"component": "checkout-service", "environment": "prod"}, "component": "checkout-service",
 "payload": {"rule": "error_rate_high", "value": 0.071}, "dedupe_key": "alert:checkout-service:error_rate_high",
 "idempotency_key": null, "correlation_id": "corr_D0003", "causation_id": null,
 "trace": {"stream": "stream_workday", "seq": 431, "scenario_id": "S10"}, "attributes": {},
 "status": "routed", "status_reason": null, "occurrences": 3, "last_seen_at": "2026-11-05T10:51:10Z",
 "duplicate_of": null, "scenario_id": "S10", "summary": "alert.fired HIGH checkout-service: error_rate_high",
 "dispatches": [ <dispatch summary> ]}
```
`occurrences` counts this event plus later semantic duplicates folded into it (badge "x3"). A suppressed duplicate is
its own row with `status:"suppressed"`, `status_reason` and `duplicate_of`; rows broadcast on SSE for such a duplicate
also carry `parent_occurrences` (the new count on the original) so the feed can update the "x3" badge live.

**Dispatch summary / dispatch row** (`GET /api/dispatches` returns the full row):
```json
{"id": "dsp_0000012", "event_id": "evt_0000431", "route": "r-signal", "kind": "run", "generation": 0,
 "priority": "P1", "status": "running", "attempts": 1, "graph_id": "event_router", "assistant_id": "event_router",
 "thread_id": "d59c7787-...", "run_id": "01a1b916-...", "incident_id": null,
 "enqueued_at": "...", "started_at": "...", "finished_at": null, "next_attempt_at": null, "error": null}
```
`status`: `queued | running | interrupted | succeeded | failed | retrying | dead`. `kind`: `run | resume`.

### `GET /api/events/{event_id}`
Event row plus `"links"` (same as `/api/links/event/{id}`). `404` if unknown.

### `GET /api/trace/{event_id}`
Causal chain: walks `causation_id` up to the root and down through every descendant, and joins dispatches:
```json
{"root_id": "evt_0000400", "focus_id": "evt_0000431",
 "chain": [ {<event row>, "depth": 0}, {<event row>, "depth": 1} ],
 "edges": [{"from": "evt_0000400", "to": "evt_0000431"}],
 "dispatches": [ <dispatch row> ], "incident": {<incident row or null>}}
```

### `GET /api/links/event/{event_id}`
```json
{"event_id": "evt_0000431", "dispatches": [<dispatch row>], "thread_id": "d59c...", "run_id": "01a1...",
 "incident_id": "INC-D0003", "studio_base_url": "https://smith.langchain.com/studio/?baseUrl=http%3A%2F%2F127.0.0.1%3A2026",
 "studio_thread_url": null, "api_url": "http://127.0.0.1:2026/threads/d59c..."}
```
`studio_thread_url` is `null` unless `FOGHORN_STUDIO_THREAD_URL` is configured (see `GET /api/config`).

### `GET /api/dispatches`
Query: `status` (csv), `event_id`, `graph_id`, `incident_id`, `limit` (default 100, max 500), `offset`.
`{"items": [<dispatch row>], "total": 57}` (newest first). Use it for "Active runs" (`status=running,interrupted`).

## 3. Live stream (SSE)

### `GET /api/stream`
`Content-Type: text/event-stream`. Query: `channels` (csv of `events,queue,clock,dispatches`; default all), `since`
(seq; replay events with `seq > since` first), `types` (csv/glob), `min_severity`, `component`, `correlation_id`,
`scenario_id`. Header `Last-Event-ID: <seq>` wins over `since`. Frames end with a blank line (`\n\n`).

| SSE `event:` | `id:` | `data:` | Cadence |
|---|---|---|---|
| `foghorn.hello` | - | `{"server_time": "...", "latest_seq": 1204, "resumed_from": 1100}` | once at connect |
| `foghorn.event` | `seq` | an **event row** (above), includes lifecycle events and `dlq.added`/`event.shed` markers | as they occur |
| `foghorn.dispatch` | - | a **dispatch row** whenever its status changes | as they occur |
| `foghorn.queue` | - | same object as `GET /api/queues` | every 2 s |
| `foghorn.clock` | - | same object as `GET /api/clock` | every 1 s |
| `foghorn.gap` | - | `{"missed_from": 1110, "missed_to": 1190}` | when this client's bounded buffer overflowed (oldest dropped); re-fetch `GET /api/events?since=...` |
| comment `: heartbeat` | - | - | every 15 s |

Reconnect with `Last-Event-ID` replays from SQLite with no gaps and no duplicates (live frames received while the
backlog is replayed are de-duplicated by `seq`). Server-side filters apply to `foghorn.event` only.

## 4. Webhooks

All accept `Idempotency-Key` and optional `X-Foghorn-Signature`; bodies capped at 256 KB; rate limited.

### `POST /api/webhooks/alertmanager`
Prometheus Alertmanager webhook v4: `{"version": "4", "groupKey": "...", "status": "firing", "receiver": "foghorn",
"commonLabels": {...}, "alerts": [{"status": "firing", "labels": {"alertname": "error_rate_high",
"service": "checkout-service", "severity": "critical"}, "annotations": {"summary": "..."},
"startsAt": "2026-11-05T10:49:00Z", "endsAt": "0001-01-01T00:00:00Z", "fingerprint": "ab12"}]}`.
Each alert becomes one envelope (`alert.fired` when `alert.status=="firing"`, else `alert.resolved`; component =
`labels.service|component|job|instance`; severity from `labels.severity` with `page|critical -> critical`,
`high|error -> high`, `warning|medium -> medium`, `info -> info`, default `medium`; `payload` carries
`rule=alertname`, `labels`, `annotations`, `fingerprint`, `generator_url`; `dedupe_key =
alert:<component>:<alertname>`; `idempotency_key = am:<fingerprint>:<startsAt>:<status>`). Response:
```json
{"received": 2, "accepted": 1, "duplicates": 1, "suppressed": 0, "rejected": 0,
 "results": [{"event_id": "evt_0000440", "status": "routed"}, {"event_id": "evt_0000439", "status": "duplicate"}]}
```
`202` normally. If the queues push back mid-batch: `429` + `Retry-After` with the partial `results` (retrying the
whole batch is safe because of the idempotency keys). `422` if the body is not a v4 payload (`alerts` missing/not a
list).

### `POST /api/webhooks/deploybot`
`{"event": "deploy.started|deploy.finished|deploy.rolled_back", "service": "pricing-service", "version": "4412",
"environment": "prod", "deploy_id": "dep-9", "status": "success", "actor": "ci", "commit": "ab12", "at": "<iso>"}`
-> one envelope (`source: "deploybot"`, `idempotency_key: "deploybot:<deploy_id>:<event>"`, severity `info`, or
`medium` for rollbacks). Response = `POST /api/events` response.

### `POST /api/webhooks/generic`
Body is an envelope (same as `POST /api/events`) **or** `{"type": "...", ...}` shorthand where unknown top-level
keys land in `payload`. Response = `POST /api/events` response.

## 5. Replay and scenarios

### Replay session object
```json
{"id": "rep_0001", "state": "running", "kind": "stream", "name": "stream_workday", "speed": 60.0, "loop": false,
 "position": 431, "total": 1800, "loop_index": 0, "virtual_at": "2026-11-05T10:49:12Z",
 "started_at": "2026-11-08T14:20:00.000Z", "backpressure_paused": false, "launch_id": null, "error": null}
```
`state`: `idle|running|paused|finished|stopped|failed`. At most one session at a time.

### `GET /api/replay`  ->  `{"state": "idle", "session": null}` or `{"state": "running", "session": {...}}`
### `GET /api/streams`  ->  `{"items": [{"name": "stream_workday", "events": 1800, ...}], "available": true}` (data layer)
### `POST /api/replay`
Body `{"stream": "stream_workday" | "scenario_id": "S10", "speed": 60, "loop": false, "start_at": null,
"replace": false, "jitter_s": 0, "run": {<run options>}}`. Starts a session on the virtual clock (speeds 1, 10, 60,
600 or any `0 < x <= 600`). `409 replay_active` unless `replace: true`. Returns `202 {"session": {...}}`.
Events keep their per-component order; ids are `evt_<name>_<seq>` (suffix `_r<k>` for the k-th re-run so a stream can
be replayed more than once). The engine pauses itself while the queues are above high-water.
### `POST /api/replay/pause` | `/resume` | `/stop`  ->  `{"state": ..., "session": {...}}`
### `POST /api/replay/step`  -> releases exactly one event (pauses first if running) -> same shape
### `POST /api/replay/speed`  body `{"speed": 600}` -> same shape

### Run options (`run` object; also top level of a launch body)
`{"profile": "competent", "approval_policy": "standard", "breakpoints": ["approval_gate"], "token_delay_ms": 0,
"memory_enabled": true, "chaos": null, "as_of": null}` -> becomes the run `context` for every run created by this
launch (keys from `foghorn.contracts.meta`: `model_profile`, `approval_policy`, `breakpoints`, `token_delay_ms`,
`memory_enabled`, `chaos`, `scenario_id`, `as_of`). Defaults come from `GET /api/config` `defaults`.

### `GET /api/scenarios?tier=1&include_generated=false`
`{"items": [<public scenario view>], "available": true}`: the S01-S16 curated set (plus generated `X-nnnn` ones with
`include_generated=true`). The public view is `foghorn.data.api` 's (`scenario_id, title, summary, tier, stats, trigger,
world` - never ground truth) plus two derived fields:
```json
{"scenario_id": "S10", "title": "Two changes, one culprit", "tier": 3, "summary": "...",
 "category": "config_change",
 "features": ["question", "approval", "memory recall"],
 "stats": {"alerts": 18, "changes": 8, "events": 38, "logs": 946, "reports": 6},
 "trigger": {"alert_event": "evt_S10_0015", "as_of": "2026-11-05T10:48:30Z"}, "world": {"onset": "...", "difficulty": {}}}
```
* `features[]` = chips for what the scenario *exercises*, derived from the scenario spec by a fixed vocabulary:
  `approval`, `two-person approval`, `question`, `break-glass`, `edit`, `memory write`, `four-eyes`, `tool review`,
  `multiple interrupts`, `memory recall`, `injection`, `no-op`, `alert storm`. They never name components, changes or
  remediations.
* `category` = the fault category (`bad_deploy`, `config_change`, ...). It is the *type* of the root cause, i.e. a hint:
  `FOGHORN_SCENARIO_CATEGORY_PUBLIC=false` removes it from the public view (it is still returned with
  `reveal_truth=true`).
`503 data_unavailable` when the data package is not importable.
### `GET /api/scenarios/{scenario_id}?reveal_truth=false`
The scenario dict; `reveal_truth=true` adds the ground truth (`get_truth`). `404` if unknown.
### `POST /api/scenarios/{scenario_id}/launch`
Body `{"speed": 60, "mode": "events", "profile": "competent", "approval_policy": "standard", "breakpoints": [],
"token_delay_ms": 0, "memory_enabled": true, "chaos": null, "as_of": null}` (all optional).
* `mode: "events"` (default): publishes the scenario's events through `ingest()` on the virtual clock (a replay
  session of kind `scenario`); the `event_router` graph decides what to do with them.
* `mode: "direct"`: skips the router: mints an incident id, emits `incident.opened`, and creates one run on
  `incident_commander` with the scenario's `CommanderInput`.
`202`:
```json
{"launch_id": "lau_0003", "scenario_id": "S10", "mode": "events", "events_total": 24, "speed": 60.0,
 "correlation_id": "corr_S10_lau_0003", "incident_id": null, "replay": {<replay session>},
 "run": {"model_profile": "competent", "approval_policy": "standard"}}
```
`409 replay_active` if a session is running (pass `"replace": true` to stop it first). In `mode: "direct"` the response
has `replay: null`, `events_total: 0`, `incident_id: "INC-D0001"` and an extra `dispatch` (the `incident_commander`
dispatch row, priority P1); the commander input is the scenario's `as_of` + `{id, severity: "SEV2", title}` incident ref.
Unknown scenario -> `404`; invalid `profile` / `approval_policy` / `speed` / `breakpoints` -> `422`.

### `POST /api/faults` (fault injector)
Body `{"category": "bad_deploy", "component": "checkout-service", "magnitude": 0.7, "red_herrings": 2, "seed": 7,
"launch": false, "speed": 60, "run": {<run options>}}` -> calls `foghorn.data.api.build_fault_scenario` (registers
`X-nnnn`), records it in the generated-scenario index and optionally launches it:
`{"scenario": {<public view>}, "scenario_id": "X-0001", "launch": null | {<launch response>}}`.
### `GET /api/faults`
`{"items": [{"scenario_id": "X-0001", "category": "bad_deploy", "component": "...", "magnitude": 0.7,
"red_herrings": 2, "seed": 7, "created_at": "..."}], "categories": ["bad_deploy", "..."]}`
`categories` are the 14 categories of `foghorn.data.faults.CATEGORIES` (a built-in copy of that list is used if the data
layer is unavailable; `categories` is `[]` only if both fail). `POST /api/faults` answers `201`. The same fault
arguments always give the same `X-nnnn` (the data layer is deterministic), so re-injecting is idempotent.

### `GET /api/services`  ->  `{"items": [<service_catalog() entries>]}`

## 6. Incidents

**Incident row** (materialised from `incident.*` lifecycle events):
```json
{"incident_id": "INC-D0003", "correlation_id": "corr_D0003", "thread_id": "6c1f2d1e-...", "title": "Checkout errors after flag ramp",
 "severity": "SEV2", "status": "open", "scenario_id": "S10", "component": "checkout-service",
 "opened_at": "...", "updated_at": "...", "resolved_at": null, "root_cause": null,
 "pending_approvals": 1, "studio": {"base_url": "https://smith.langchain.com/studio/?baseUrl=...", "thread_id": "6c1f2d1e-...", "thread_url": null}}
```
`status`: `open|mitigating|monitoring|resolved`. `root_cause` is the graph-provided JSON (or `null`).

### `GET /api/incidents`
Query: `status` (csv), `severity` (csv), `scenario_id`, `limit` (default 100), `offset`. -> `{"items": [...], "total": 5}`
(newest first).
### `GET /api/incidents/{incident_id}`
Incident row + `"events": [<event row>]` (by correlation id / incident id, oldest first), `"dispatches": [...]`,
`"approvals": [<approval row>]`, `"threads": [{"thread_id", "graph_id", "run_id"}]`, `"links"` (Studio base link +
copyable `thread_id`), and the run the console should join:
* `"run_id"`: the newest run on the incident's own thread: a dispatch we created if there is one, otherwise the Agent
  Server's newest run of that thread (the commander is usually started by `event_router` through the SDK, so no dispatch
  row exists); `null` if the thread has no run yet or the Agent Server cannot be asked.
* `"graph_id"`: the graph of that run (falls back to the incident's `graph_id`, normally `incident_commander`).
Join with the built-in `GET /threads/{thread_id}/runs/{run_id}/stream` (`Last-Event-ID: -1` replays from the start).
`404` if unknown.

## 7. Approvals inbox

**Approval row** (a projection of LangGraph interrupts; the thread state is the source of truth):
```json
{"approval_id": "apr_0007", "kind": "approval", "status": "pending", "thread_id": "6c1f2d1e-...",
 "interrupt_id": "3f8a...", "run_id": null, "graph_id": "incident_commander", "incident_id": "INC-D0003",
 "title": "Revert flag new_pricing_engine", "summary": "...", "risk": "medium", "reversible": true,
 "requested_at": "...", "decided_at": null, "decided_by": null, "decision": null,
 "payload": {<interrupt value: contracts.interrupts.build_interrupt(...)>},
 "response_schema": {<JSON Schema of the resume value>}, "options": ["approve", "edit", "reject", "ask"]}
```
`status`: `pending | decided | stale`. `payload` and `response_schema` are present while pending. The dashboard
generates its form from `response_schema` (kinds: `approval`, `edit`, `question`, `break_glass`, `memory_write`,
`tool_review`, `four_eyes`).

### `GET /api/approvals?status=pending`
`status`: `pending` (default; live scan of interrupted threads through the Agent Server, also refreshes the
projection), `decided` (history from SQLite), `all`. Also `incident_id`, `kind`, `limit`.
`{"items": [<approval row>], "scanned_threads": 4, "scan_ok": true}` (`scan_ok:false` -> Agent Server unreachable;
cached projection returned).
### `GET /api/approvals/{approval_id}`  ->  approval row (`404` if unknown)
### `POST /api/approvals/{approval_id}/resume`
Body:
```json
{"interrupt_id": "3f8a...", "response": {"decision": "approve", "decided_by": "gary", "note": ""},
 "nonce": "ui-1731074000"}
```
`response` must satisfy the interrupt's schema (`Decision`, `Answer` or `MemoryDecision` from
`foghorn.contracts.interrupts`; `decided_by` defaults to `"operator"`). `interrupt_id` is **validated against the
thread's currently pending interrupts before anything is resumed**; unknown/stale ids -> `409 stale_interrupt`
(`details.pending: [ids]`), invalid `response` -> `422 invalid_response`. The server then runs
`Command(resume={interrupt_id: response})` on the owning thread and emits `approval.decided`
(idempotent on `(approval_id, nonce)`: repeating a call with the same nonce returns `200 {"status": "duplicate"}`).
`202`:
```json
{"status": "resumed", "approval_id": "apr_0007", "thread_id": "6c1f...", "interrupt_id": "3f8a...", "run_id": "01a1...",
 "decision_event_id": "evt_0000500", "remaining_interrupts": 1}
```
A repeated call with the same `nonce` after the decision was applied returns `200
{"status": "duplicate", "approval_id", "thread_id", "interrupt_id"}` and does nothing. Errors: `404` unknown approval or
thread, `409 stale_interrupt` (interrupt no longer pending), `409 conflict` (thread's graph unknown), `422
invalid_response`, `503 not_ready` (the Agent Server refused the resume; nothing was recorded, the same click can be
retried).
### `POST /api/approvals/resume-batch`
Parallel interrupts on one thread: `{"thread_id": "6c1f...", "responses": {"<interrupt_id>": {<response>}, ...},
"nonce": "..."}` -> one `Command(resume={...})`; same validation (every id must be pending) and errors; `202
{"status": "resumed", "thread_id", "run_id", "interrupt_ids": [...], "decision_event_ids": [...]}`.

## 8. Dead letters, queues

**Dead letter**:
```json
{"id": "dlq_0000003", "reason": "run_failed", "status": "open", "poison": false, "attempts": 3,
 "last_error": "RuntimeError: ...", "event_id": "evt_0000431", "dispatch_id": "dsp_0000012",
 "thread_id": "d59c...", "run_id": "01a1...", "checkpoint_id": "1f0a...", "envelope": {<event or null>},
 "raw_body": null, "created_at": "...", "resolved_at": null, "resolved_by": null, "resolution": null}
```
`reason`: `schema_invalid | no_route | run_create_failed | run_failed | timeout | poison`. `status`: `open | retried | discarded`.
For `schema_invalid` the original text is in `raw_body` (truncated to 64 KB) and `envelope` is `null`.
### `GET /api/dlq?status=open&reason=&limit=100&offset=0`  ->  `{"items": [...], "total": 3}`
### `GET /api/dlq/{id}`  ->  dead letter
### `POST /api/dlq/{id}/retry`
Re-dispatches **exactly once** (new dispatch row, same event id, `generation+1`; a failed run is *resumed from its
last checkpoint*, `input=null`; a poison event's chaos flag is dropped so an operator retry gets a clean run).
`202 {"status": "retried", "dispatch": {<dispatch>}, "event_id": "evt_..."}`. `409 conflict` if already resolved,
`404` unknown. For `schema_invalid` the stored body is re-validated and re-ingested (`dispatch` is `null`; `422
schema_invalid` if still invalid). For `no_route` the rules are re-evaluated (`422 no_route` if still unrouted).
### `POST /api/dlq/{id}/discard`  body `{"reason": "known bad", "by": "gary"}` -> `{"status": "discarded", "dead_letter": {...}}`
Audited: emits a `dlq.discarded` event (`payload.by`, `payload.reason`); `409 conflict` if already resolved.

### `GET /api/queues`
```json
{"depth": {"P0": 0, "P1": 1, "P2": 2, "P3": 0}, "caps": {"P0": 500, "P1": 500, "P2": 1000, "P3": 200},
 "total_depth": 3, "total_cap": 2200, "utilization": 0.0014, "high_water": 0.8, "low_water": 0.5,
 "backpressure": false, "in_flight": 1, "interrupted": 2, "workers": 4,
 "per_graph": {"incident_commander": {"running": 1, "cap": 3}}, "dlq": 0, "shed": 0,
 "rate_per_s": 0.6, "events_total": 1204, "dispatched_total": 311, "oldest_wait_s": 0.4,
 "events_rejected": 0, "requests_429": 0, "promoted": 0}
```
`rate_per_s` is events/s over the last minute; `promoted` counts items aged up a class; `requests_429` counts refusals.

## 9. Datasets, evals, memory

### `GET /api/datasets`  ->  `{"items": [<datasets_index() entries>], "available": true}`
### `GET /api/datasets/{name}?offset=0&limit=50&reveal_truth=false&scenario_id=`
Passes straight through to `foghorn.data.api.read_dataset` (`{"name","format","total","offset","limit","truth_hidden",
"records":[...]}`; `name` may be `telemetry.alerts` etc., `scenario_id` narrows telemetry tables). Ground truth is stripped
unless `reveal_truth=true` (the UI's "Reveal ground truth" toggle). `404` unknown dataset, `503` unavailable.

### Evals
**Eval run**:
```json
{"eval_run_id": "evr_0001", "status": "running", "suite": "core16", "profile": "competent", "label": null,
 "scenario_ids": [], "concurrency": 2, "baseline_id": null, "created_at": "...", "finished_at": null,
 "progress": {"done": 5, "total": 16},
 "evaluators": ["root_cause", "action_safety", "evidence"],
 "cases": [<eval case>],
 "summary": null, "thread_id": "...", "run_id": "...", "event_id": "evt_0000600"}
```
`status`: `requested -> running -> finished`. `evaluators` is derived (the union of the `scores` keys of all cases, in first-seen
order = the matrix columns). `summary` is whatever `eval.finished` sent; the shape the dashboard understands:
`{"mean_score": 0.88, "pass_rate": 0.9, "by_tier": {"1": 0.95}, "by_evaluator": {"root_cause": 0.9},
"gate": {"no_crash": 1.0, "safety": 1.0}, "baseline_id": "evr_0000", "regressions": ["S10"]}` (all keys optional).

**Eval case** - the ONE shape persisted in `eval_runs.cases` (the wave-3 `eval_runner` emits it directly in
`eval.progress.payload.case` and `eval.finished.payload.cases`; the server normalises and upserts by `scenario_id`,
keeping unknown extra keys such as `gate_trips` or `tokens` untouched):
```json
{"scenario_id": "S10",
 "scores": {"root_cause": 1.0, "action_safety": 0.5},
 "score": 0.75, "passed": true,
 "incident_id": "INC-0007", "thread_id": "6c1f2d1e-...", "run_id": "01a1b916-...",
 "expected": {"root_cause": "pricing-service", "severity": "SEV2"},
 "actual":   {"root_cause": "pricing-service", "severity": "SEV3"}}
```
`scores` maps evaluator name -> number (non-numeric entries are dropped); `score` defaults to the mean of `scores` when
omitted; `passed` is `true|false|null`; `incident_id` / `thread_id` / `run_id` / `expected` / `actual` are `null` when
absent (`expected` / `actual` are objects, never strings). A malformed case (not an object, no `scenario_id`) is skipped,
never an error. A later `eval.progress` for the same `scenario_id` replaces the earlier case; once `finished`, a run never
regresses.
### `POST /api/evals`
Body `{"suite": "core16"|"sweep60"|"custom", "scenario_ids": [], "profile": "competent", "concurrency": 2,
"baseline_id": null, "max_scenarios": null, "label": null}` (all optional) -> creates the row, ingests an
`eval.requested` event (routed to `eval_runner`) -> `202 {"eval_run": {...}, "event": {<POST /api/events response>}}`.
### `GET /api/evals?limit=50`  ->  `{"items": [<eval run>]}`
### `GET /api/evals/{eval_run_id}`  ->  eval run (`404` unknown)

### Memory (Store, via the in-process SDK client)
### `GET /api/memory/namespaces?prefix=prefs&max_depth=2`  ->  `{"items": [["prefs", "team-payments"]]}`
### `GET /api/memory/search?q=rollback&namespace=incidents&limit=10`
`namespace` is dot-joined (prefix). `{"items": [{"namespace": ["incidents", "checkout-service"], "key": "inc-0042",
"value": {...}, "score": 0.68, "created_at": "...", "updated_at": "..."}], "query": "rollback"}`.
### `GET /api/memory/item?namespace=prefs.team-payments&key=freeze`  ->  the item (`404` if missing)
Edits/deletes go straight to the built-in `/store/items` routes.

## 10. Graph catalog and Studio guide

### `GET /api/graphs`
```json
{"items": [{"graph_id": "incident_commander", "title": "Incident commander", "role": "Supervisor multi-agent response ...",
            "features": ["Command(goto)", "interrupt(response_schema)"], "has_guide": true,
            "assistants": [{"assistant_id": "...", "name": "commander-competent", "version": 1}],
            "studio": {"base_url": "https://smith.langchain.com/studio/?baseUrl=..."}}],
 "assistants_scan_ok": true}
```
### `GET /api/assistants`  ->  `{"items": [{"assistant_id", "graph_id", "name", "description", "context", "seeded": true}], "scan_ok": true}`
### `GET /api/studio/guide`
Aggregates `src/foghorn/flows/<graph_id>/GUIDE.md` (YAML front matter + markdown body). Missing/invalid files are
tolerated (the catalog entry is returned with `has_guide:false`).
```json
{"studio": {"base_url": "...", "thread_url_template": null},
 "setup": {"title": "Set up", "steps": ["Run `make dev` ...", "Open Studio ..."]},
 "graphs": [{"graph_id": "incident_commander", "title": "...", "summary": "...", "features": ["..."],
             "studio_steps": ["Open the graph selector ..."], "sample_inputs": [{"name": "S10", "input": {"scenario_id": "S10"}}],
             "body": "<markdown after the front matter>", "has_guide": true}],
 "warnings": ["flows/storm_correlator/GUIDE.md: front matter missing"]}
```
`GUIDE.md` front matter keys: `graph_id, title, summary, features[], studio_steps[], sample_inputs[{name,input}]`.

## 11. Admin

### `POST /api/admin/reset`  body `{"confirm": "reset"}`
Deletes events, dispatches, incidents, approvals projection, dead letters, eval runs, launches and resets counters and
the virtual clock. Does **not** touch LangGraph threads/store. `{"status": "reset", "deleted": {"events": 120, ...}}`.
### `GET /api/admin/settings`  /  `PUT /api/admin/settings`
Operator-switchable defaults (the System view). `GET` returns, `PUT` partially updates (any subset; unknown fields and
invalid values are `422`) and returns, the full object:
```json
{"chaos_mode": false, "default_model_profile": "competent", "default_approval_policy": "standard", "default_token_delay_ms": 0}
```
`default_model_profile`: `competent|naive|cautious|gullible`; `default_approval_policy`: `strict|standard|auto_low_risk`;
`default_token_delay_ms`: `0..2000`; `chaos_mode`: when `true`, every run created afterwards gets the run-context key
`chaos: "tools"` (seeded transient tool failures, so `RetryPolicy`/fallback branches become visible in traces) unless the
launch passed its own `chaos`. Precedence per run: launch options > these settings > environment defaults
(`FOGHORN_DEFAULT_MODEL_PROFILE`, `FOGHORN_CHAOS_MODE`, ...). Changes apply to runs created after the call (a queued
dispatch keeps the context it was planned with), persist in the state DB across restarts, and are reflected in
`GET /api/config` `defaults`. Aliases for the SPA: `GET|PUT|POST /api/settings`.
### `POST /api/admin/ambient`  body `{"enabled": true}`  ->  `{"ambient_enabled": true}`
Starts/stops the ambient-noise generator (replays `stream_quiet_hour` in a loop at 60x; off by default; pauses itself
under backpressure).

## 12. Lifecycle events graphs must emit (payload conventions)

Graphs call `foghorn.common.lifecycle.emit_event(type, idempotency_key=..., payload=...)`. The server persists
every emission (deduplicated on `idempotency_key`), broadcasts it on `/api/stream`, and materialises records:

| type | payload keys the server reads | materialises |
|---|---|---|
| `incident.opened` / `incident.updated` | `incident_id`, `severity` (`SEV1..4`), `title`, `status?`, `thread_id?`, `scenario_id?`, `root_cause?` (+ envelope `correlation_id`, `subject.component`) | incident row (upsert; later events never regress `resolved`) |
| `incident.resolved` | same + `resolution?` | status `resolved`; routes `SEV1/SEV2` to `postmortem_pipeline` |
| `approval.requested` | `approval_id`, `kind`, `thread_id`, `interrupt_id?`, `run_id?`, `title?`, `summary?`, `risk?`, `incident_id?` | approval row `pending` |
| `approval.decided` | `approval_id`, `decision`, `decided_by`, `thread_id?`, `interrupt_id?` | approval row `decided` |
| `action.executed` / `action.verified` | `incident_id?`, `action`, `target?`, `outcome?` | event row only |
| `postmortem.published` | `incident_id`, `scenario_id?`, `postmortem` | routes to `memory_keeper` |
| `eval.progress` | `eval_run_id`, `done`, `total`, `case?: <eval case>` (or `cases?: [<eval case>]`) | eval run progress; cases upserted by `scenario_id` |
| `eval.finished` | `eval_run_id`, `summary`, `cases?: [<eval case>]` | eval run `finished` (shapes: section 9, Evals) |
| `memory.recalled` / `memory.written` | `ns`, `key` | event row only |

Dispatcher-emitted: `run.started`, `run.interrupted`, `run.finished`, `run.failed` (`payload.will_retry`), `triage.completed`
(after an `event_router` run, from `values.result`), `dlq.added`, `dlq.retried`, `dlq.discarded` (extension, not in the
shared type list), `event.shed`, `replay.started`, `replay.finished`, `system.cron.tick` (source `cron`). `causation_id` of `run.*` is the triggering event id; `correlation_id` is inherited.

## 13. How events reach graphs (`src/foghorn/events/routing.yaml`)

First match wins, unless a rule says `continue: true`. Priority comes from the rule, else from severity
(`critical->P0, high->P1, medium->P2, low/info->P3`).

| Rule | Matches | Effect |
|---|---|---|
| `r-approval-decided` | `approval.decided` from `ui`/`ui.inject`/`api` sources | resume the thread (P0) |
| `r-maintenance` | `maintenance.window.*` | updates the suppression table |
| `r-storm-member` | `alert.fired` while a storm is open (it stays open until 120 virtual s of quiet) | stored as `suppressed` / `storm_member` |
| `r-storm` | `alert.fired` bursts (>= 8 within 60 virtual s) | `storm_correlator` (P0, `StormInput` with the window's alerts), `continue` |
| `r-signal` | `alert.fired`, `alert.resolved`, `customer.report`, `slo.burn_rate`, `healthcheck.failed` | `event_router` (`RouterInput {event}`) |
| `r-change-ledger` | `deploy.*`, `config.changed`, `flag.changed`, `migration.applied` | stored (change ledger); if an incident is open on the component -> `event_router` |
| `r-postmortem` | `incident.resolved` SEV1/SEV2 | `postmortem_pipeline` (`PostmortemInput`) |
| `r-memory` | `postmortem.published` | `memory_keeper` (`mode: learn`) |
| `r-handoff` | `system.cron.tick` job `shift_handoff` | `shift_handoff` |
| `r-eval` | `eval.requested` | `eval_runner` (`EvalRunnerInput`) |
| `r-change-request` | `change.requested` | `change_gate` (`ChangeGateInput`) |
| `r-record` | lifecycle + system types | stored only |
| `r-fallback` | anything else (only if no rule above produced a step) | dead letter `no_route` |

Rule syntax, input builders and thread-id derivation are documented at the top of `routing.yaml`. Every dispatch gets
a deterministic thread id (`det_thread_id(graph, key)`), flat thread metadata (`foghorn_event_id`,
`foghorn_correlation_id`, `foghorn_incident_id`, `foghorn_scenario_id`, `foghorn_trigger`, `foghorn_eval_run_id`,
`foghorn_kind`, `foghorn_graph`) and run `context` built from the launch's run options (or the defaults); runs stream
`values, updates, messages-tuple, custom, tasks, checkpoints` with `stream_subgraphs` and `stream_resumable`, so the
console can `join` them late. Run failures retry as a resume-from-checkpoint (`input=null`) with backoff 1 s / 4 s / 16 s,
then dead-letter.

## 14. Configuration (environment, prefix `FOGHORN_`)

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_API_TOKEN` | unset | bearer token for every `/api` route (unset = open dev mode) |
| `WEBHOOK_SECRET` / `WEBHOOK_REQUIRE_SIGNATURE` | dev secret / `false` | HMAC secret and whether webhooks must be signed |
| `WEBHOOK_MAX_BODY_BYTES` / `WEBHOOK_RATE_PER_MIN` | 256000 / 120 | ingest body cap and per-client rate limit |
| `DATA_DIR` | `var` | state DB lives at `<DATA_DIR>/state.sqlite` |
| `MAX_RUNS`, `PER_GRAPH_MAX_RUNS` | 4, `{"incident_commander":3,"eval_runner":1}` | dispatcher concurrency |
| `QUEUE_CAP_P0..P3`, `HIGH_WATER`, `LOW_WATER` | 500/500/1000/200, 0.8, 0.5 | queue caps and backpressure thresholds |
| `MAX_ATTEMPTS`, `BACKOFF_S` | 3, `[1,4,16]` | retry policy |
| `DEDUPE_WINDOW_S`, `STORM_COUNT`, `STORM_WITHIN_S`, `STORM_COOLDOWN_S` | 300, 8, 60, 120 | virtual-time windows |
| `CRON_JOBS`, `NATIVE_CRON_SCHEDULE` | `{"shift_handoff":"0 */8 * * *"}` | virtual cron ticks / the disabled native server cron |
| `AMBIENT_ENABLED` | `false` | background noise from `stream_quiet_hour` |
| `STUDIO_THREAD_URL` | unset | per-thread Studio deep-link template (`{studio}`, `{thread_id}`, `{base}`) - unverified format |
| `SEED_ON_START`, `READY_TIMEOUT_S` | `true`, 90 | startup seeding / how long to wait for the Agent Server |
| `ROUTING_FILE` | packaged `routing.yaml` | override the rule table |

## 15. Startup behaviour

The app lifespan opens the state DB, installs the lifecycle sink, then in the background waits for the Agent Server and
seeds idempotently: 11 named assistants (`commander-<profile>` x4, `commander-policy-<policy>` x3, `rca-*`, `copilot-*`
with `metadata.foghorn_seed=true`), Store memories from `foghorn.data.api.memory_seed_items()` (skipped when already
present), and a **disabled** native cron for `shift_handoff`; then it reconciles pending approvals from interrupted
threads. Seeding failures are logged and reported by `GET /api/health` (`components.seeding`), they never stop the server.
CORS: the server defaults are kept; a tiny ASGI middleware strips `Access-Control-Request-Private-Network` from `OPTIONS`
preflights (otherwise starlette answers Studio's private-network preflight with `400`).
