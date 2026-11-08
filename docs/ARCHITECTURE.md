# Foghorn - architecture and build contract

Foghorn is an AI **incident-command center** for a fictional e-commerce company (Brightcart). It is built so that
every important LangGraph and LangGraph Studio feature has a purposeful home in a product that feels real.
The domain is a vehicle; the point is a multi-graph LangGraph deployment you can *use*, *stream*, *interrupt*,
*fork*, *evaluate* and *inspect in Studio*.

Background research (read the section you need, do not re-derive it): `docs/design/01-studio-platform.md`
(Studio and Agent Server facts, verified live), `02-graph-architecture.md` (LangGraph 1.2 ledger, 38 gotchas,
patterns), `03-product-data-events.md` (domain, datasets, scenarios, events, UI wireframes, demos, acceptance).
The reconciled decisions below win where the documents differ.

## 1. Ground rules (apply to every module)

1. **Offline and deterministic by default.** `FOGHORN_PROVIDER=offline`. No network, no keys, no wall clock, no
   `uuid4`/`random` outside the seeded RNG factory, in tests or in a default `langgraph dev`. Real Claude/OpenAI is
   opt-in (`FOGHORN_PROVIDER=auto|anthropic|openai`) via `foghorn.models.factory.get_chat_model(role)`.
2. **State is plain JSON.** `str` timestamps, no tuples/dataclasses/`datetime`/clients/locks in state; pydantic only
   at graph edges (docs/design/02 gotchas 5-7). Keep model instances, DB handles and clients out of state.
3. **Graph files are thin.** `src/foghorn/graphs/<id>.py` is `from foghorn.flows.<id>.build import build_graph;
   graph = build_graph()` and nothing else (a contract test enforces it). Logic lives in `src/foghorn/flows/<id>/`.
   Export a bare `compile()` - never a graph with its own checkpointer (it crashes the whole server).
4. **No blocking calls on the event loop.** `time.sleep`, sync SQLite/HTTP inside `async def` raise `BlockingError`
   under `langgraph dev`. Use sync `def` nodes/tools (they run in threads) or `asyncio.to_thread`. Never rely on
   `--allow-blocking`.
5. **Side effects after `interrupt()`, and idempotent.** `interrupt()` re-runs the node from line 1 on resume.
   Custom stream events and lifecycle events carry deterministic keys so repeats are harmless.
6. **Every model call goes through `get_chat_model(role)`**; roles are `fast | smart | judge`. Tag internal
   router/judge/structured calls `tags=["nostream"]` so their JSON never reaches the user token stream.
   Offline behaviour is a `Script` of rules over the transcript (see `foghorn/models/offline.py`), selected by the run
   context `model_profile` (`competent | naive | cautious | gullible`). Narration numbers must come from tool
   results, never hard-coded.
7. **Typing and style:** `ruff` clean, `mypy --strict` clean, Python 3.11+ syntax, `from __future__ import
   annotations`. Tests are deterministic, offline, fast. Coverage gate 85 %.
8. **Shared files have one owner (the integrator).** `pyproject.toml`, `langgraph.json`, `settings.py`,
   `tests/conftest.py`, `Makefile`, `.github/**` and everything under `src/foghorn/contracts/`, `common/`, `models/`
   are not edited by feature agents; ask for changes in your final report. Subsystems define their own config
   (`class XSettings(BaseSettings)` with `env_prefix="FOGHORN_"`) in their own package.
9. **Port:** the dev server runs on **2026** (`make dev`), not 2024 (the user keeps another project there).
   Always start with `--n-jobs-per-worker 8`. Studio: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2026`.

## 2. Repository layout and ownership

```
src/foghorn/
  settings.py logging.py clock.py security.py embeddings.py auth.py   integrator (done)
  contracts/   events.py interrupts.py meta.py inputs.py              integrator (done): the shared vocabulary
  common/      stream.py lifecycle.py delegate.py                     integrator (done)
  models/      offline.py factory.py                                  integrator (done): OfflineChatModel + Script DSL
  evals/       harness.py judge.py (done)  evaluators/ runner glue     wave 3
  data/        generator, datasets, scenarios, faults, world          WAVE 1 / agent DATA
  tools/       pure tool API + LangChain tool wrappers                WAVE 1 / agent DATA
  events/      envelope store, routing, dedupe, queues, dispatcher,   WAVE 1 / agent EVENTS
               replay, cron generator, SSE, DLQ
  server/      app.py (FastAPI http.app) + api routers                WAVE 1 / agent EVENTS
  server/static/  the dashboard SPA (no build step)                   WAVE 1 / agent UI
  memory/      Store helpers, namespaces, seeding                     wave 2 / agent MEMORY
  flows/<id>/  implementation of each graph (see section 3)           wave 2
  graphs/<id>.py  thin Studio entry points (done as stubs)            wave 2 replaces the stub body
datasets/      committed, human-diffable datasets + MANIFEST.json      agent DATA
var/           generated data and runtime state (git-ignored)
tests/{unit,graphs,contract,golden,evals,e2e}/
docs/          ARCHITECTURE.md, design/*, STUDIO_GUIDE.md, API.md, DEMO.md
```

## 3. The ten graphs (one deployment, ids = `langgraph.json` keys)

| id | Role in the product | LangGraph features it exists to show | Studio features it lights up |
|---|---|---|---|
| `event_router` | Single ingress for events: validate, dedupe via Store ledger, enrich (retry+cache), classify (rules, LLM fallback), route; delegates to other graphs | `RetryPolicy`, `CachePolicy(key_func)`, conditional edges, `Send` fan-out, `Store`, graph-to-graph `delegate` (child thread, interrupt bubbling), `context_schema` | graph view with branch labels, input form, thread metadata, child threads |
| `incident_commander` | Supervisor multi-agent response: specialists (metrics, logs, change, topology, knowledge) as subgraphs, RCA, remediation plan, policy check, **human approval**, execute, verify, comms, close | hand-rolled supervisor with `Command(goto)`, `create_agent` subgraphs, `Command.PARENT`, dynamic `interrupt(response_schema=)`, static breakpoints, `get_stream_writer`, `messages` token streaming, `update_state`/fork, reducers, `destinations=` | xray subgraph drill-in, interrupts UI, **edit state + fork + rerun**, breakpoints, assistants with editable prompts (`langgraph_type: "prompt"`), chat of tokens |
| `rca_tournament` | Competing root-cause hypotheses scored in parallel; also used as a subgraph of the commander | `Send` map-reduce, custom reducers, `defer=True` reduce node, per-Send timeout | parallel branches in the trace, subgraph xray |
| `storm_correlator` | Collapses an alert storm into causal clusters | `Send` map-reduce, `TimeoutPolicy`, `error_handler`, `CachePolicy`, `set_node_defaults`, map-reduce-reduce loop | error-handler nodes, retried steps, large fan-out trace |
| `change_gate` | Change-approval "HITL gym": approve / edit / reject / ask / four-eyes / parallel interrupts | multiple + parallel `interrupt()`, resume-by-id maps, `Command(resume=)`, `interrupt_before/after`, `response_schema` | interrupt cards, resume with value, multiple pending interrupts |
| `postmortem_pipeline` | Drafts, reviews and publishes a postmortem from an incident thread | **Functional API** `@entrypoint`/`@task`, `previous`, `entrypoint.final`, task retries, calls a compiled graph | one-node view of a functional workflow, task trace |
| `ops_copilot` | Chat copilot for operators with tools, memory and a tool-call review step | `langchain.agents.create_agent` + middleware stack, `ToolRuntime`, structured output, in-tool `interrupt`, Store read/write | **Chat mode**, tool-call rendering, memory |
| `shift_handoff` | Deterministic, durable shift report (no LLM); cron-driven | durable execution (`durability="sync"`), crash recovery from last checkpoint, `tasks`/`checkpoints` streams, breakpoints, `DeltaChannel`/reducers, native server **cron** | checkpoint history, time travel, replay from checkpoint, thread status after crash |
| `memory_keeper` | Learns from closed incidents: writes memories + lessons, consolidates on cron, human curates | `Store` namespaces, **semantic search** (`store.index`), `memory_write` interrupt, cron consolidation | Memory/store view, search |
| `eval_runner` | Runs scenarios through the commander, scores against ground truth, regression-gates vs a baseline | `Send` over scenarios, SDK delegation with concurrency limit, evaluators + LLM-judge, structured output | experiment-style fan-out trace; results in dashboard |

Cross-graph wiring (summary): the **dispatcher** (events subsystem) creates a run on `event_router` for each
accepted event; `event_router` decides, and either handles it or **delegates** to `incident_commander` /
`storm_correlator` / `change_gate`. `incident_commander` uses `rca_tournament` as a subgraph and emits lifecycle
events; closing an incident triggers `postmortem_pipeline` then `memory_keeper`. `eval_runner` drives
`incident_commander` per scenario on derived child threads. `ops_copilot` and `shift_handoff` are user/cron driven.
All inputs are the TypedDicts in `foghorn/contracts/inputs.py`.

## 4. Subsystem contracts

### 4.1 DATA + TOOLS (`foghorn.data`, `foghorn.tools`, `datasets/`)
Design source: `03-product-data-events.md` sections 2.1-2.7. Deterministic, stdlib-only, seeded generator
(`python -m foghorn.data build [--check]`), committed small datasets in `datasets/`, telemetry SQLite built lazily
into `settings.data_dir` (git-ignored, hash-checked against `datasets/MANIFEST.json`).
Public API (all synchronous, thread-safe, JSON-returning, pure reads except where noted):

```python
# foghorn.data.api
list_scenarios(tier: int | None = None) -> list[dict]            # public view: no ground truth
get_scenario(scenario_id: str, *, reveal_truth: bool = False) -> dict
get_truth(scenario_id: str) -> dict                                # ground truth (evals, UI "reveal")
default_as_of(scenario_id: str) -> str                             # onset + detection lag (ISO, virtual)
scenario_events(scenario_id: str) -> list[dict]                    # EventEnvelope.to_state() in virtual-time order
list_streams() -> list[dict]; iter_stream(name: str) -> Iterator[dict]
service_catalog() -> list[dict]
datasets_index() -> list[dict]; read_dataset(name: str, offset: int = 0, limit: int = 50) -> dict
build_fault_scenario(category: str, component: str, magnitude: float, red_herrings: int, seed: int) -> dict  # registers X-nnnn
sweep_specs() -> list[dict]
# foghorn.tools.context
class ToolContext(TypedDict): scenario_id: str; as_of: str | None; applied_actions: list[dict]   # state keys with the same names
# foghorn.tools.api  (every function takes ctx first; honours the as_of time fence; never reads the future)
query_metric(ctx, service, metric, window_minutes=30) -> dict      # incl. points, baseline, z-score, onset estimate
compare_baseline(ctx, service, metric) -> dict
search_logs(ctx, service, pattern="", level=None, window_minutes=30, limit=50) -> dict   # lines are untrusted text
list_changes(ctx, window_minutes=240, component=None, lookback_days=None) -> dict
get_topology(ctx, service) -> dict; blast_radius(ctx, service) -> dict
search_runbooks(query, k=4) -> dict; recall_incidents(query, k=4) -> dict   # lexical corpus search
get_oncall(ctx, component) -> dict
execute_action(ctx, action, target, params=None) -> dict          # simulation: returns outcome; caller records it in applied_actions
verify_recovery(ctx, service) -> dict                             # reads the recovery overlay of applied_actions
# foghorn.tools.lc: LangChain @tool wrappers using ToolRuntime to read scenario_id/as_of/applied_actions from graph STATE
TOOLS_BY_NAME: dict[str, BaseTool]; tools_for(names: Sequence[str]) -> list[BaseTool]
```
Tool outputs that contain log/user text are wrapped with `foghorn.security.wrap_untrusted`.
Recovery is a deterministic overlay of the stored (un-remediated) series driven by `applied_actions` held in graph
state, so forks/replays see consistent worlds. Hostile log lines (prompt injection) exist in scenario S15.

### 4.2 EVENTS + SERVER (`foghorn.events`, `foghorn.server`)
Design source: `03-product-data-events.md` section 3 and 4.3. The custom FastAPI app (`langgraph.json` `http.app`) is
mounted **next to** the built-in Agent Server API:
* `GET /` redirects to `/app/`; the SPA is served under `/app/*` via `StaticFiles` (never mount at `/`: it swallows
  the built-in routes; never shadow `/info`, `/ok`, `/docs`, `/openapi.json`, `/metrics`).
* The app lifespan installs the lifecycle sink (`foghorn.common.lifecycle.install_sink`), opens the state DB
  (SQLite, WAL, via `asyncio.to_thread`/sync helpers - never blocking the loop), starts the dispatcher, scheduler and
  replay engine as background tasks, waits for the server to be ready, then **seeds idempotently** (assistants with
  named configurations, Store seed memories, the disabled native cron). It talks to the Agent Server with the
  in-process SDK client `get_client(url=None)`.
* Event pipeline (single `ingest()`): validate -> idempotency-key check -> semantic dedupe -> routing rules
  (`routing.yaml`) -> priority queue (P0-P3) -> dispatcher creates a run on the target graph (deterministic thread
  id, flat thread metadata from `contracts/meta.py`) -> run/event/thread correlation stored in SQLite and in thread
  metadata. Backpressure (high/low water, 429 + `Retry-After`, shedding), retry with backoff, dead-letter queue with
  retry/discard. SSE feed with `Last-Event-ID` resume. Replay engine at 1x-600x over virtual time.
* REST under `/api/*` (full list in `docs/API.md`, which the EVENTS agent writes FIRST so the UI can build against it):
  events (ingest/list/get/stream/inject), webhooks (alertmanager shape), replay control, scenarios (list/launch/
  fault-inject), incidents, approvals inbox + resume, DLQ, queues/health, datasets browser, evals (start/list/get),
  memory search, graph catalog + Studio guide, clock, config. Anything the built-in API already does (threads, runs,
  streaming, store, assistants) is used **directly by the UI** at the same origin, not re-implemented.
* Per-graph Studio guides are files `src/foghorn/flows/<id>/GUIDE.md` (YAML front matter: `graph_id, title, summary,
  features[], studio_steps[], sample_inputs[{name,input}]`); the server aggregates them for `/api/studio/guide`.
* Auth: custom routes authenticate themselves (`Depends`) when `FOGHORN_DASHBOARD_API_TOKEN` is set; dev default is
  open. Webhook ingest supports optional HMAC (`X-Foghorn-Signature`). No `http.enable_custom_route_auth`.

### 4.3 UI (`src/foghorn/server/static/`)
Design source: `03-product-data-events.md` section 4. No-build vanilla ES modules, no CDN dependency (works
offline), accessible, responsive, light/dark. Talks to (a) `/api/*` and (b) the built-in LangGraph API
(`/threads`, `/runs/stream` with `stream_mode` arrays incl. `messages-tuple`, `custom`, `updates`, `values`, `tasks`,
`checkpoints` and `stream_subgraphs`, `join_stream` reconnect). Must parse SSE frames that end with `\r\n\r\n`.
Views: Mission Control (live event feed + KPIs + queues), Incident Console (token-by-token stream, node timeline,
evidence/hypotheses/series charts), Approvals inbox (forms generated from the interrupt `response_schema`),
Scenarios + Fault injector, Graph Explorer (per-graph structure from `/assistants/{id}/graph?xray=true`, Studio deep
links), Datasets browser, Evaluations, Memory viewer, Studio Guide, Copilot chat panel.

### 4.4 Graph owners (wave 2)
Each flow package exposes `build_graph(checkpointer=None, store=None, **deps) -> CompiledStateGraph` and keeps
`graphs/<id>.py` thin. Each flow ships: `GUIDE.md`, an offline `Script` per model role used, graph tests in
`tests/graphs/` (happy path, every interrupt decision, every `Command`/`Send` branch, one fork/replay, a streaming
assertion, one failure injection), and registers sample inputs. Naming: node names are stable verbs
(`docs/design/03` section 7.4 lists the commander's); reuse `common.stream.emit` and `common.lifecycle.emit_event`.

## 5. Definition of done (gates)
`make check` green (ruff, mypy strict, coverage >= 85 %, contract tests over every graph, `langgraph validate`);
`make e2e` green against a real `langgraph dev` (all ten graphs run through the SDK incl. streaming with
`stream_subgraphs`, an interrupt+resume, custom routes and the dashboard assets, server log free of
`BlockingError`/tracebacks); offline eval gate green (`no_crash == 1.0`, safety `== 1.0`, quality >= baseline);
the dashboard verified in a real browser (events stream in, a scenario runs with visible token streaming, an
approval is resolved, a fork is created); README quick start works from a clean clone in three commands;
`docs/STUDIO_GUIDE.md` and `docs/DEMO.md` exist. Acceptance checklist: `03-product-data-events.md` section 6.

## 6. Commands
```bash
uv sync                      # install (editable package)
make dev                     # langgraph dev on :2026 with --n-jobs-per-worker 8 (offline, tracing off)
make check                   # lint + typecheck + tests with coverage + config validation
make e2e                     # spawns a real server on a free port
python -m foghorn.data build # regenerate datasets (deterministic); --check verifies the committed files
```
