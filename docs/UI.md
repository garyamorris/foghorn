# Foghorn dashboard (SPA)

A no-build, vanilla ES-module single-page app served by the custom FastAPI app at **`/app/`** (Starlette `StaticFiles(html=True)`
mount; files live in `src/foghorn/server/static/`). No CDN, no npm packages at runtime, no external fonts or network assets: it works fully
offline. Light and dark themes (OS preference plus a manual toggle), responsive down to phone width, keyboard accessible, reduced-motion
aware, tabular numerals for metrics.

```
static/
  index.html                  shell: loads css/*.css, js/theme-init.js (no theme flash), js/main.js (module)
  css/tokens.css              design tokens (spacing, type, light/dark colours, severity palette)
  css/base.css components.css views.css
  js/main.js                  top bar, nav, hash router (lazy views), health/clock/approval badges, toasts
  js/api.js                   THE ONLY module that knows endpoint paths (+ stream helpers, Studio link builders)
  js/live.js                  one shared SSE connection to /api/stream (feed ring buffer, queue, clock, dispatches)
  js/mock.js, js/mock/*       dev-only mock server (?mock=1 / ?mock=force), never loaded otherwise
  js/core/                    pure logic (imports in browser AND Node) + DOM helpers
  js/views/                   one module per view (lazy imported)
tests/ui/                     Node tests: `node --test tests/ui/`
```

## Run it

`make dev` (port 2026) and open `http://127.0.0.1:2026/app/`. `?api=http://host:port` points the SPA at another origin (stored in
`localStorage`; `?api=` clears it). If `FOGHORN_DASHBOARD_API_TOKEN` is set, use the lock icon in the top bar to store the bearer token.

### Mock mode (dev only)

* `?mock=1`: **fallback**: every request goes to the real server first; if the endpoint answers 404/405/5xx (or the network fails) the mock
  answers instead. Real wiring always wins when the endpoint exists.
* `?mock=force`: everything is simulated, including a live event bus, scripted incident runs (S10: tokens per agent lane with subgraph
  namespaces, tool calls, `series`/`evidence.found`/`hypothesis.update`/`plan.proposed`/`policy.check`/`memory.*`, an approval interrupt with a
  `response_schema`, resume, recovery) and a copilot with a `tool_review` interrupt.
* The mock speaks the real wire format (`\r\n\r\n` SSE frames, `Content-Location`, `Last-Event-ID`), so the real parser, reducer and reconnect
  code run unchanged. A `MOCK`/`MOCK*` pill is shown in the top bar. Nothing in the mock is imported unless the query parameter is present.

## Views and the endpoints they depend on

Built-in = the Agent Server API on the same origin. `/api/*` = `docs/API.md`.

| View (route) | Depends on |
|---|---|
| Top bar (all pages) | `GET /api/config`, `/api/health`, `/api/summary`, `GET /api/stream` (SSE: `foghorn.event|queue|clock|dispatch|gap|hello`), `GET /api/events` (backfill), `GET /api/approvals?status=pending` (badge + toast on `approval.requested`) |
| Mission Control `#/` , `#/events/:id` | `/api/stream`, `/api/events`, `/api/events/{id}`, `/api/trace/{id}`, `/api/links/event/{id}`, `/api/approvals`, `/api/dispatches`, `/api/queues` (via SSE), `/api/dlq` + `/api/dlq/{id}/retry|discard`, `/api/replay` + `/api/replay/{pause,resume,step,speed,stop}`, `/api/streams`, `/api/clock` (GET/POST), `/api/admin/ambient`, `/api/services` |
| Incidents `#/incidents` | `/api/incidents`, `/api/scenarios`, `POST /api/scenarios/{id}/launch` |
| Incident Console `#/incidents/:id`, `#/runs/:threadId` | `/api/incidents/{id}`; built-in `GET /threads/{id}/state`, `GET /threads/{id}/runs`, `GET /threads/{id}/runs/{rid}/stream` (join, `Last-Event-ID`), `GET /threads/{id}/runs/{rid}`, `POST /threads/{id}/runs/{rid}/cancel`; resume via `POST /api/approvals/{id}/resume` (+ `/resume-batch`), falling back to `POST /threads/{id}/runs` with `command.resume` |
| Approvals `#/approvals[/:id]` | `/api/approvals` (`pending`, `decided`), `/api/approvals/{id}`, `POST /api/approvals/{id}/resume`, `POST /api/approvals/resume-batch` |
| Scenarios `#/scenarios/{launcher,inject,compare}` | `/api/scenarios`, `/api/scenarios/{id}?reveal_truth=true`, `POST /api/scenarios/{id}/launch`, `/api/events/presets`, `POST /api/events` (+ `Idempotency-Key`), `/api/incidents`, built-in `GET /assistants/{graph}/graph`, `GET /threads/{id}/state` |
| Fault injector `#/faults` | `GET/POST /api/faults`, `/api/services`, `POST /api/scenarios/{id}/launch`, `/api/scenarios/{id}?reveal_truth=true` |
| Graph Explorer `#/graphs[/:id]` | `/api/graphs`, `/api/assistants`, `/api/studio/guide` (sample inputs); built-in `POST /assistants/search`, `GET /assistants/{id}/graph?xray=true`, `GET /assistants/{id}/schemas`, `POST /threads`, `POST /threads/{id}/runs`, `POST /threads/search` |
| Threads / Time travel `#/threads[/:id]` | built-in `POST /threads/search`, `GET /threads/{id}`, `POST /threads/{id}/history`, `POST /threads/{id}/state` (fork), `POST /threads/{id}/runs` with `checkpoint`, `POST /threads/{id}/copy` |
| Datasets `#/datasets[/:name]` | `/api/datasets`, `/api/datasets/{name}?offset&limit`, `/api/scenarios/{id}?reveal_truth=true`, `POST /api/replay` |
| Evaluations `#/evals[/:id]` | `/api/evals` (GET/POST), `/api/evals/{id}`, `eval.progress|finished` events over SSE, `/api/scenarios` (tiers) |
| Memory `#/memory/...` | `/api/memory/namespaces`, `/api/memory/search`, built-in `POST /store/items/search`, `PUT/DELETE /store/items`; live `memory.written` events |
| Studio Guide `#/guide[/:graph]` | `/api/studio/guide`, `/api/config` (Studio base URL) |
| System `#/system` | `/api/health`, `/api/queues` (+ SSE), `/api/dlq`, `/api/clock`, `/api/admin/{reset,ambient}`, `/api/config`, built-in `POST /runs/crons/search`, `PATCH /runs/crons/{id}` |
| Copilot panel (top bar) | `/api/graphs` (assistants), built-in `POST /threads`, `POST /threads/{id}/runs/stream` (reconnect: `GET .../runs/{rid}/stream`), `GET /threads/{id}/state`, `POST /threads/{id}/runs/{rid}/cancel` |

`js/api.js` holds every path (`paths.*`) and the typed wrappers (`api.*`). A backend change is a one-file edit.

## How streaming works

* `core/sse.js`: incremental parser: `\r\n`, `\n`, bare `\r`, frames split at any byte (even between `\r` and `\n`), multi-line `data:`,
  comments (heartbeats), `id`/`event`/`retry`, BOM.
* `core/stream.js`: `fetch` + `ReadableStream` client (so POST bodies and `Last-Event-ID` work): exponential backoff with jitter, idle watchdog,
  retry only on retryable statuses, `buildRequest` to switch to the **join** endpoint on reconnect, `shouldReconnect` to stop when the run is done.
* `api.streamRun` starts a run with `stream_mode=[values, updates, messages-tuple, custom, tasks, checkpoints]`, `stream_subgraphs`,
  `stream_resumable`, `on_disconnect=continue`; on a drop it rejoins `GET /threads/{t}/runs/{r}/stream` with the last event id.
  `api.joinRun` attaches to a run in flight (late join replays from the start while the server keeps it, about two minutes).
* `core/reducer.js` folds parts into one run model: lanes per agent (namespace-aware: `messages|metrics_analyst:<task>`), tool-call cards,
  timeline from `tasks`/`updates`, interrupts from `values`/`updates`/`tasks`, checkpoints, and the custom kinds. **Custom events dedupe by
  `key`**: an identical repeat (a resumed node re-emitting) is dropped; the same key with different data is an upsert (a series refreshed after
  remediation). Memory is bounded: ring buffers (raw 800, notes 300, feed 600) and a pruned seen-key map.
* Rendering is coalesced with `requestAnimationFrame`; the stream pane keeps itself pinned to the bottom unless the user scrolls up; the stream and
  the Mission Control feed are `role="log"` live regions (the feed announces a summary at most every 5 s, not every row).

## Security posture

Log lines and tool output contain prompt injection **by design** (scenario S15). Nothing untrusted is ever interpreted as HTML: the DOM builder
(`core/dom.js`) turns strings into text nodes, the Markdown renderer never passes raw HTML through and rewrites `javascript:`/`data:` links to `#`, JSON
is rendered through text nodes. There is no `innerHTML`, `eval` or inline event handler in the app (grep-verified; the mock includes a hostile log line
on purpose). `localStorage` is only used in `try/catch` for light state (theme, persona, token, last copilot thread, recent launches).

## Interrupt forms

`core/schemaform.js` turns a JSON Schema (pydantic output: `$ref/$defs`, `allOf`, `anyOf [X, null]`, `Literal` enums, free-form `dict`) into a
form model, collects and validates the value, and builds the resume payload. `views/forms.js` renders it. The action enum (`decision`) becomes
the buttons (approve / edit / reject / ask, save / edit / discard, answer); conditional fields (`edits`, `question`, `text`) appear with their action.
Client-side extras: break-glass needs a note of at least 20 characters on approve. Resume always sends an `{interrupt_id: value}` map
(`buildResume`); parallel interrupts on a thread are staged one by one and sent together with **Resume all**. Keyboard: `a`, `r`, `e`, `?`.

## Tests

```bash
node --test tests/ui/          # 111 tests, node: built-ins only (a directory argument works through tests/ui/index.js)
node --test tests/ui/*.test.mjs
```

Covered: SSE parser, stream client (fake fetch: chunking, reconnect with `Last-Event-ID`, join request, backoff, retry classes, close), run reducer
and key dedupe, interrupt-schema form generator, formatters, hash router, ring buffer, SVG chart math, graph layout (cycles, long edges, xray
groups, collapse), JSON diff / state-update builder, event feed logic, eval comparison, Markdown safety, storage fallbacks.

## Known gaps

* Browser-verified against the live `langgraph dev` only with the **stub graphs** that exist today (event pipeline, launch, approvals scan, thread
  history, **fork**, graph structure and schemas, SSE bus, run stream and join all work for real). Token streaming, custom events and interrupts from
  the real graphs were verified against the mock, which mirrors the documented contracts (`common/stream.py`, `contracts/interrupts.py`).
* Studio per-thread deep links are best effort (`studio.thread_url_template` else `<studio>/thread/<id>?baseUrl=`); "Copy thread id" and the base link
  are always offered.
* Service heat is derived from unresolved `alert.fired` events in the live feed (there is no metrics-at-virtual-now endpoint).
* Dataset tables are generic (columns inferred from the first records); only scenarios (truth reveal), streams (replay) and markdown-bearing records
  get special treatment. No telemetry chart for scenarios.
* Evaluations: the scenario x evaluator matrix needs `cases[].scores`; without it the table shows score and pass/fail only.
* No persona-based role gating: the persona only fills `decided_by`.

## Backend needs

None are blocking. Nice to have:

1. **Runtime settings endpoint** (`POST /api/settings`) for chaos mode, default model profile and token delay (System view shows config read-only;
   chaos and token delay are per launch today).
2. `GET /api/incidents/{id}` could include the latest `run_id` so the console can join without listing runs.
3. Scenario public view could carry `features[]` and `category` (the launcher derives chips from `world.difficulty` and `stats` until then).
4. `cases[]` in eval runs: `scores` (per evaluator), `incident_id`, `thread_id`, `expected`/`actual` unlock the matrix and drill-in.
