# 01 - LangGraph Studio & Server (Platform) Expert Report

Scope: what the finished `foghorn` app must contain so LangGraph Studio and the Agent Server surface light up, plus the traps found by running a toy multi-graph project against the **installed** versions. Date of research: 2026-11-08.

Installed (verified in `.venv`): langgraph 1.2.20, langgraph-api 0.17.1, langgraph-cli 0.4.42, langgraph-runtime-inmem 0.37.1, langgraph-sdk 0.4.14, langchain 1.4.9, langchain-core 1.7.6, langsmith 0.14.17, starlette 1.10.0, fastapi 0.162.2, blockbuster 1.5.29. Docker not used.

Evidence labels used on every claim:

| Tag | Meaning |
|---|---|
| `[VL]` | VERIFIED-LIVE: observed against `langgraph dev` on port 2101 (toy project) |
| `[VS]` | VERIFIED-SOURCE: read in `.venv/lib/python3.12/site-packages/{langgraph_api,langgraph_cli,langgraph_sdk,langgraph_runtime_inmem}` |
| `[VD]` | VERIFIED-DOCS: seen on docs.langchain.com (fetched 2026-11-08). Note docs are ahead of installed 0.17.1 in places (MCP origin check, MCP output format) |
| `[U]` | UNVERIFIED: not reproducible here (Studio UI itself needs a LangSmith login; paid/LangSmith calls were deliberately avoided) |

Toy project (ephemeral, scratch): `/private/tmp/claude-501/-Users-garyamorris--claude-spares-spare-lRCPrl/a9a93820-8d04-4501-adf4-c6aae828bd1f/scratchpad/studio-expert/toy/` (8 graphs, FastAPI `http.app`, auth, fake embeddings; configs `langgraph.{,b,c,d1,d2,e,h}.json`). Key snippets are copied into Appendix A so nothing depends on it.

---

## 0. Ten facts that decide the design

1. **`http.app` routes win over built-ins**: order is `/ok,/metrics,/docs,/openapi.json` (never shadowable) -> **your routes** -> `/` and `/info` (shadowable) -> protected built-ins (`/assistants,/threads,/runs,/store,/mcp,/a2a,/ui`). So the dashboard can own `/`, but **never `app.mount("/", StaticFiles(...))`** (it swallows every built-in API route: POST `/assistants/search` -> 405) and **never shadow `/info`** (Studio feature detection reads it). `[VS][VL]`
2. `langgraph dev` runs **one** concurrent run by default (`N_JOBS_PER_WORKER=1`), although `--help` says "Default: 10". Two 4 s runs on different threads took 5 s + 9 s. Use `--n-jobs-per-worker 10`. Every run also pays ~1 s queue latency (trivial run = 1.0 s wall). `[VL][VS]`
3. **Relative paths in langgraph.json resolve against the process CWD, not the config dir**; `.langgraph_api/` (all persistence) is also created in CWD. Always run from the directory holding `langgraph.json`. `[VL]`
4. **Editing `langgraph.json` needs a manual restart** (new graph not picked up after 7 s; only `.py` edits hot-reload, and a reload re-runs your lifespan). `[VL]`
5. **Persistence works across restarts** (threads, checkpoints, assistants incl. user-created, crons, store items and vectors) but is flushed every 10 s: graceful SIGINT flushes; SIGKILL loses up to ~10 s. Crons persist too and keep firing on every restart until deleted. `[VL]`
6. **TTLs are inert in dev**: `store.ttl` and `checkpointer.ttl` are accepted but never enforced (store sweeper is a no-op; `sweep_ttl` returns `(0, 0)`). Keep them in config for prod parity, never demo them. `[VS][VL]`
7. **Custom auth fails open** for any `(resource, action)` with no handler, and in `langgraph dev` **any request with header `x-auth-scheme: langsmith` is accepted as `StudioUser` without checking `x-api-key`** (bogus key accepted). Studio relies on this locally. Add a default-deny `@auth.on` that lets `StudioUser` through. `[VS][VL]`
8. `http.cors` **replaces** the default CORS config entirely. `{"allow_origins":[...]}` alone leaves `allow_methods=GET`, so POST preflights from Studio get `400 Disallowed CORS method`, and `content-location`/`x-pagination-*` stop being exposed. Always specify `allow_methods:["*"]`, `allow_headers:["*"]`, `expose_headers`. `[VL]`
9. With starlette 1.10 + langgraph-api 0.17.1, a CORS preflight carrying `Access-Control-Request-Private-Network: true` returns **400 "Disallowed CORS private-network"** (starlette `allow_private_network` defaults False; `http.cors` has no key to set it; unknown keys are dropped). Whether current browsers send that header is `[U]`; a 10-line ASGI middleware fixes it (Appendix A.3). `[VL]`
10. A graph's blocking call on the event loop (`time.sleep`, sync sockets, sqlite, `os.scandir`...) raises `BlockingError` in `langgraph dev` (surfaces to the API as `{"__error__":{"error":"BlockingError","message":"An internal error occurred"}}`; real text only in server log). File `read/write`, `os.stat`, `os.listdir` are deliberately allowed. `--allow-blocking` disables the check. Sync `def` nodes are exempt (run in a thread). `[VL][VS]`

---

## 1. STUDIO / SERVER CAPABILITY INVENTORY

"Lights up when" = exact requirement in code/config. "Demo" = one-liner for Gary. Studio UI behaviour is `[VD]` or `[U]` (cannot log in to LangSmith here); the API contract that Studio consumes is `[VL]`.

### 1.1 Capability table

| # | Capability | Lights up when (exact) | Gary demos it by | Evidence |
|---|---|---|---|---|
| 1 | **Multiple graphs / assistants picker** | One `graphs` entry per graph; value `"./path.py:obj"` or `{"path":..., "description":...}`. Each graph auto-creates a *system assistant* with deterministic id `uuid5(UUID('6ba7b821-9dad-11d1-80b4-00c04fd430c8'), graph_id)` (stable across restarts/machines). Assistant `name` = compiled graph `name` (set `.compile(name="support_agent")`), falling back to graph_id. `description` comes from langgraph.json. | Open Studio, switch graph in the top-left selector; `POST /assistants/search` lists all | `[VL]` ids/names/descriptions; picker `[VD]` |
| 2 | **Graph view with conditional/dynamic edges** | Static edges: `add_edge`. Dynamic edges only draw when declared: `add_conditional_edges(..., path_map)`, node return annotation `-> Command[Literal["a","b"]]`, or `add_node(..., destinations=("a","b"))`. `Send` shows as a conditional edge. Node `metadata={...}` is returned in graph JSON (`nodes[].metadata`). | Show approval graph with `human_review -> publish/revise` drawn | `[VL]` (graph JSON) |
| 3 | **Subgraph xray / drill-in** | Add the *compiled* subgraph as a node: `parent.add_node("review", review_graph)`. API: `GET /assistants/{uuid}/graph?xray=true` (or `xray=1`) returns nodes `review:critique`, `review:polish`; `GET /assistants/{uuid}/subgraphs?recurse=true` returns `{"review": {input_schema,...}}`; state drill-in `GET /threads/{id}/state?subgraphs=true` fills `tasks[].state` with subgraph values/`next`. A subgraph only *called inside* a function node is not statically drawable `[U]`. | Click the `review` node, expand into critique/polish | `[VL]` |
| 4 | **Threads** (list, history, state, copy) | Checkpointer is provided by the server automatically (all toy graphs compiled with none and were fully checkpointed/time-travelable); passing your own to `compile()` is standard-advised against `[VD]`, not tested. `POST /threads/search` supports `metadata`, `status` (idle/busy/interrupted/error), `values`, `select`, `extract` (`{"last":"values.messages[-1].content"}`), `sort_by`. `POST /threads/{id}/copy` clones. There is **no** `/runs/search` (404); list runs per thread. | Threads dropdown; "+ New thread"; dashboard thread table via `extract` | `[VL]` |
| 5 | **Input form from input schema** | `StateGraph(State, input_schema=In, output_schema=Out)`; schemas served at `GET /assistants/{uuid}/schemas` (`input_schema,output_schema,state_schema,config_schema,context_schema`). Studio renders the form from the input/state schema; "View Raw" = JSON. **Chat mode only works when state includes/extends `MessagesState`** `[VD]`. Use narrow typed inputs for non-chat graphs (also becomes the MCP tool schema). | Run `pipeline` from a 2-field form (`topic`, `ask`) | `[VL]` schemas, `[VD]` UI |
| 6 | **Assistants + configurable form** | `StateGraph(State, context_schema=Ctx)` with `Ctx` a dataclass/Pydantic/TypedDict. Server returns both `config_schema` and `context_schema` (same JSON Schema, title = class name). Create variants: `POST /assistants {graph_id,name,context,description}`; edit -> new version (`PATCH`), versions via `POST /assistants/{id}/versions`, activate via `POST /assistants/{id}/latest {"version":n}`. A run may override: `"context": {...}`. | "Manage Assistants": create "pirate-chat", set Active, run | `[VL]` API, `[VD]` UI |
| 7 | **Prompt editing in node (gear icon)** | Field metadata in the context/config schema: `langgraph_nodes: ["call_model"]` (nodes the field belongs to) + `langgraph_type: "prompt"`. Pydantic: `Field(default=..., json_schema_extra={"langgraph_nodes":[...],"langgraph_type":"prompt"})`. Dataclass: `field(default=..., metadata={"description":..., "json_schema_extra":{...}})`. Model picker hint: `{"__template_metadata__":{"kind":"llm"}}`. Server flattens these keys into the property of `context_schema`/`config_schema` (verified). Studio then shows a gear on node `call_model`; Save -> update current or create new assistant version. Playground: thread -> "View LLM Runs" on a node -> open in Playground (needs LangSmith traces). | Click gear on `call_model`, edit prompt, Save as new assistant version, rerun | `[VL]` schema output; gear/Playground `[VD]` |
| 8 | **Interrupts & resume** | `value = interrupt(payload, response_schema=PydanticModelOrJsonSchema)` in a node; resume with run body `{"command":{"resume": <value>}}` on the same thread. Server surfaces: run output `__interrupt__:[{value,id,response_schema}]`, `thread.status == "interrupted"`, `thread.interrupts` (keyed by task id), `state.tasks[].interrupts`. `response_schema` arrives as JSON Schema (usable for dashboard forms). Interrupts inside subgraphs bubble to the root thread; resume from root works. | Reject once (loops through `revise`), approve second time | `[VL]`; Studio resume UI "Continue" `[VD]` |
| 9 | **Static breakpoints** | Run body `interrupt_before` / `interrupt_after` (`["node"]` or `"*"`); Studio "Interrupt" menu. Applies to **root-graph nodes only**: `interrupt_before:["polish"]` (a node inside a subgraph) did not stop. For breakpoints inside subgraphs use dynamic `interrupt()` or compile the subgraph with `interrupt_before=`. | Pause before `draft`, edit state, Continue | `[VL]` |
| 10 | **State edit / fork / re-run / time travel** | Needs only checkpoints. `GET /threads/{id}/history`; `POST /threads/{id}/state {"values":{...},"checkpoint":{...},"as_node":"draft"}` returns a new `checkpoint`; then run with `{"checkpoint": <that>}` and `input` omitted/null. Resuming a past checkpoint forks history. Meaningful per-node state keys + reducers make the forks readable. | Edit `draft` at step 1, re-run, show fork in history | `[VL]`; "Edit node state / Fork / Re-run from here" `[VD]` |
| 11 | **Streaming modes in Studio/SDK** | Run body `stream_mode` list of: `values, updates, messages-tuple, messages, custom, events, debug, tasks, checkpoints`; `stream_subgraphs:true` for nested events (event name gets `\|<ns>` suffix). See 1.2. Studio has a streaming toggle next to Submit `[VD]`. | Watch tokens + custom progress events live | `[VL]` |
| 12 | **Reconnectable streams** | Create run with `stream_resumable:true`; `GET /threads/{t}/runs/{r}/stream` (+`Last-Event-ID`). ids look like `1794103563184-0`; `-1` replays from start even after the run ended (TTL `RESUMABLE_STREAM_TTL_SECONDS`=120 s). No header + finished run = 0 events. Stream responses carry `Content-Location`. | Kill the dashboard tab mid-run, reload, stream resumes | `[VL]` |
| 13 | **Double texting** | Run body `multitask_strategy`: `reject` (409), `enqueue` (default; waits), `interrupt` (old run -> `interrupted`, state kept), `rollback` (old run deleted, state reverted). | "Send again while running" button with a strategy dropdown | `[VL]`; semantics `[VD]` |
| 14 | **Store / long-term memory viewer** | Server store endpoints `PUT/GET/DELETE /store/items`, `POST /store/items/search`, `POST /store/namespaces`; in graphs use `runtime.store` / `store: BaseStore` injected param. Semantic search needs `store.index` (see 2.1). Studio "Manage long-term memory" `[VD]` UI. Item `index:false` (or `index:[fields]`) per put. | Seed memories, search "outdoor hiking" with scores | `[VL]` API |
| 15 | **LangSmith trace panel / "View LLM runs" / Playground** | `LANGSMITH_API_KEY` present when the server starts. With only that key set, tracing auto-enables (`LANGSMITH_TRACING` defaults true) and `/info` reports `flags.langsmith:true`. Per-run project/example: run body `langsmith_tracer:{project_name,example_id}`. To stay offline set `LANGSMITH_TRACING=false` (put it in langgraph.json `env`). | Open trace link from a node | `[VS]`; I ran with the key unset, `/info.flags.langsmith=false` `[VL]`; UI `[VD]` |
| 16 | **Datasets / experiments / add-to-dataset** | All Studio+LangSmith (browser calls LangSmith, runs graph via this server). Needs: dataset whose inputs match the assistant **input schema**; optional evaluators; server reachable. "Add to dataset" on a thread selects nodes (input/output per node) and creates dataset per assistant+node. Server hook: `langsmith_tracer.example_id`. | "Run experiment" over a seeded dataset | `[VD]` only; not run (would call LangSmith) |
| 17 | **Webhooks** | Run/cron body `"webhook": "<url>"`; payload = run object + `status`, `values`, `webhook_sent_at`, `error`. Config `webhooks` (2.1). Local receivers need `url.disable_loopback:false` **and** `url.require_https:false`. | Dashboard shows "run completed" feed | `[VL]` |
| 18 | **Crons** | `POST /runs/crons` (stateless, `on_run_completed:"keep"\|"delete"`) or `POST /threads/{id}/runs/crons`; `schedule` 5-field UTC (installed also accepts `timezone`); `PATCH {"enabled":false}`; `DELETE`. Works in `langgraph dev`; persisted across restarts. | Create `* * * * *` cron, watch new threads each minute, delete it | `[VL]` |
| 19 | **MCP server** | Automatic `POST /mcp` (Streamable HTTP, JSON only). `tools/list` = one tool per assistant: name = normalised assistant **name**, description = langgraph.json `description`, `inputSchema` = graph **input** schema; `tools/call` = stateless `runs.wait`, result text is **Python `repr()`** of the output (docs claim JSON for >=0.18). `GET /mcp`=405, no sessions. `http.disable_mcp` turns it off. | `curl` tools/list; add to Claude Code as MCP server | `[VL][VS]` |
| 20 | **A2A server** | Automatic `GET /a2a/{assistant_uuid}/.well-known/agent-card.json`, `POST /a2a/{assistant_uuid}` JSON-RPC `message/send` (also `message/stream`, `tasks/get`, `tasks/cancel`). Chat-style graphs take `text` parts into `messages`; others take `data` parts. Root `/.well-known/agent-card.json` needs `?assistant_id=`. Push notifications not implemented. `http.disable_a2a`. | Fetch agent card, send message | `[VL]` |
| 21 | **Custom HTTP app (dashboard)** | `http.app` -> Starlette/FastAPI instance. Lifespan merged with server's; routes appear in `/openapi.json`; middleware wraps built-ins too. | Dashboard at `http://127.0.0.1:2101/` beside Studio | `[VL]` |
| 22 | **Custom auth + Studio** | `auth.path` -> `langgraph_sdk.Auth()` with `@auth.authenticate` (params injectable: `request, body, user, path, method, scopes, path_params, query_params, headers, authorization, scope`) and `@auth.on...` handlers (`threads.create/create_run/read/update/delete/search`, `assistants.*`, `crons.*`, `store`). Handler returns filter dict (applied to metadata), `True/None`, or `False`. Studio user: `isinstance(ctx.user, Auth.types.StudioUser)`. | Alice/Bob thread isolation + Studio sees all | `[VL]` |
| 23 | **Graph factories with `ServerRuntime`** | `async def make_graph(config: RunnableConfig, runtime: ServerRuntime)` (0/1/2 params; contextmanager allowed). `runtime.access_context` in `threads.create_run \| threads.update \| threads.read \| assistants.read`; `runtime.user`, `runtime.store`, `runtime.execution_runtime.context`. Topology must be identical in all contexts. Keep factories cheap (server warns >100 ms). | Per-user tool set in same topology | `[VL]` |
| 24 | **Server info inside nodes** | `runtime.server_info` -> `ServerInfo(assistant_id, graph_id, user)`; `runtime.execution_info` (thread/run/checkpoint ids, attempt). `None` outside the server. | Show assistant id in a node output | `[VL]` |
| 25 | **Request headers -> config** | `http.configurable_headers.includes: ["x-user-*"]` -> header `x-user-name: gary` appears at `config["configurable"]["x-user-name"]`. `x-api-key` excluded by default; non-matching headers dropped. | Pass `x-user-name` from dashboard | `[VL]` |
| 26 | **Node resilience controls (library)** | `add_node(name, fn, retry_policy=, cache_policy=, timeout=TimeoutPolicy(run_timeout=, idle_timeout=, refresh_on="auto"\|"heartbeat"), error_handler=fn, trace_policy=, defer=, metadata=, destinations=)`; `runtime.heartbeat()`; `compile(transformers=...)`; `DeltaChannel`. | Failing node with retry then error_handler | `[VS]` signatures only; not run |
| 27 | **Generative UI** | `langgraph.json` `"ui": {"name": "./path/ui.tsx"}` + Python `push_ui_message(name, props, message=, id=, merge=)` + state key `ui` with `ui_message_reducer`; served via `POST /ui/{graph_id}`. In dev the server spawns `npx -y @langchain/langgraph-ui@latest watch` at startup (downloads from npm; if it exits the **server exits** `sys.exit(1)`). | Weather card rendered in chat | `[VS][VD]`; **not run** (executes a downloaded npm package) |
| 28 | **Protocol v2 thread event stream** | `POST /threads/{t}/stream/events {"channels":[...]}` (SSE; WebSocket variant exists) + `POST /threads/{t}/commands {"id":1,"method":"run.start","params":{"assistant_id":...,"input":...}}`. Channels `values,updates,messages,tools,lifecycle,input,tasks,custom,custom:<name>`. Graph module may export `stream_transformers()` for custom channels. | One multiplexed stream per thread for the dashboard | `[VL]` partial: `values`, `lifecycle`, `messages` (content-block events) arrived; `updates/tasks/custom` produced 0 events in my probe `[U]` why |
| 29 | **SDK typed stream v2** | `client.runs.stream(..., version="v2")` yields dicts `{type, ns, interrupts, data}` instead of `StreamPart(event, data)`. | Python/JS clients | `[VL]` |
| 30 | **Dev ergonomics** | `langgraph dev --tunnel` (Cloudflare tunnel for Safari/Brave localhost blocking), `--debug-port` (debugpy), `--studio-url`, `--server-log-level`, `--ssl-certfile/--ssl-keyfile`. Studio URL printed at boot: `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2101`. | - | `[VS]` flags; URL `[VL]`; tunnel `[U]` |

### 1.2 Streaming modes (what each emits on the wire) `[VL]`

| `stream_mode` value | SSE `event:` | `data:` shape (observed) |
|---|---|---|
| (always first) | `metadata` | `{"run_id","attempt"}` |
| `values` | `values` | full state after each step (first one echoes input) |
| `updates` | `updates` | `{node: partial_update}` |
| `messages-tuple` | `messages` | `[AIMessageChunk, {metadata incl. langgraph_node}]`, one per token (~20 ms/token with fake model) |
| `messages` (legacy) | `messages/metadata`, `messages/partial` (observed; a `messages/complete` event exists in the protocol but did not appear in my 1-node run) | cumulative message objects (each partial contains the whole text so far: 10 partials for a 10-word reply) |
| `custom` | `custom` | whatever `get_stream_writer()(obj)` wrote (any JSON) |
| `events` | `events` | LangChain v2 events (`on_chain_start`, ...), 16 for a 1-node graph |
| `tasks` | `tasks` | task start/result records |
| `checkpoints` | `checkpoints` | checkpoint snapshots (config with `checkpoint_id`) |
| `debug` | `debug` | `{"step","timestamp","type":"checkpoint"\|"task"...,"payload"}` |
| with `stream_subgraphs:true` | `custom\|review:<task_uuid>`, `updates\|review:<task_uuid>` | same payloads, namespaced |

### 1.3 Built-in HTTP surface (from live `/openapi.json`, installed 0.17.1)

`GET /ok,/docs,/openapi.json,/metrics,/info,/` | `POST /assistants`, `/assistants/search`, `/assistants/count`; `GET|PATCH|DELETE /assistants/{id}`; `GET /assistants/{id}/graph|schemas|subgraphs|subgraphs/{ns}`; `POST /assistants/{id}/versions|latest` | `POST /threads`, `/threads/search|count|prune`; `GET|PATCH|DELETE /threads/{id}`; `GET|POST /threads/{id}/state` (+ `/state/checkpoint`, `GET /state/{checkpoint_id}`); `GET|POST /threads/{id}/history`; `POST /threads/{id}/copy`; `GET /threads/{id}/storage|stream`; `POST /threads/{id}/stream/events|commands` | `POST /threads/{id}/runs[/stream|/wait|/crons]`, `GET /threads/{id}/runs`, `GET|DELETE /threads/{id}/runs/{rid}`, `/runs/{rid}/join|stream|cancel` | `POST /runs`, `/runs/stream`, `/runs/wait`, `/runs/batch`, `/runs/cancel` | `POST /runs/crons[/search|/count]`, `GET|PATCH|DELETE /runs/crons/{id}` | `PUT|GET|DELETE /store/items`, `POST /store/items/search`, `/store/namespaces` | `POST|GET|DELETE /mcp` | `/a2a/{assistant_id}` (+ agent-card routes) | `POST /ui/{graph_id}`, static `/ui/*` | `GET /profile` only with `FF_PYSPY_PROFILING_ENABLED`.

---

## 2. `langgraph.json` COMPLETE KEY REFERENCE (installed CLI 0.4.42 / API 0.17.1)

Sources: `langgraph_cli/schemas.py` (TypedDicts), `langgraph_cli/config.py::_KNOWN_CONFIG_KEYS` and `validate_config`, `langgraph_api/config/schemas.py` (what the server actually parses). The CLI TypedDict is *narrower* than the API schema; "API-only" marks keys the server accepts that the CLI type hints omit. Unknown keys: `langgraph validate` prints a warning (incl. `$schema`, harmless) but dev still starts. `validate_config` **rebuilds the dict and silently drops anything not in its list** (which is why `disable_persistence` can never reach `run_server`). No JSON-schema file ships in the package. `[VS]` unless noted.

### 2.1 Key table

| Key | Type / allowed values | Notes (what it really does) |
|---|---|---|
| `graphs` | `{id: "path.py:obj" \| {path, description}}` | **Required.** obj = `StateGraph`, compiled graph, `@entrypoint`, or `(async) contextmanager/function` factory taking `()`, `(config)`, `(runtime: ServerRuntime)` or both. Module-level `stream_transformers()` is also harvested. `.ts/.js` paths => Node (unsupported in `dev` of this CLI). |
| `dependencies` | `list[str]` | `"."` / local dirs / PyPI / git (no userinfo in URLs). In `dev` only local dirs are added to `sys.path`; installs ignored. Mutually exclusive with `source` (uv). Required (non-empty) when `python_version` set and no `source`. |
| `source` | `{kind:"uv", root, package?}` | Deploy from `pyproject.toml`+`uv.lock`; requires `python_version`; forbids `dependencies`. **Gary's pyproject has `[tool.uv] package = false` -> `langgraph dockerfile` fails: "cannot be deployed"** `[VL]`. |
| `python_version` | `"3.11"`..`"3.13"` (major.minor only) | patch rejected; min 3.11. Default 3.11. |
| `node_version` | `"20"`+ | Presence makes `dev` refuse (JS in-mem unsupported). Omit. |
| `api_version` | `"0.5"` etc. | Pins server image version (docker only). |
| `base_image`, `image_distro`, `pip_installer`, `pip_config_file`, `keep_pkg_tools`, `dockerfile_lines`, `_INTERNAL_docker_tag` | see schemas | Docker/image only. `image_distro` in `debian\|wolfi\|bookworm` (bullseye rejected). `pip_installer` in `auto\|pip\|uv`. `keep_pkg_tools`: `true` or subset of `pip,setuptools,wheel`. |
| `env` | `{K:V}` or `".env"` path | Applied via `patch_environment` **before** server imports; **overrides** the shell's values; cannot override CLI-managed vars (`LANGGRAPH_*`, `N_JOBS_PER_WORKER`, `DATABASE_URI`...). Path is relative to CWD. |
| `store` | `{index?, ttl?, path?}` | `index:{dims:int, embed:str, fields:list[str]\|null}`: `embed` is `"provider:model"` (needs `langchain` + provider pkg) **or** `"./file.py:callable"` (string must contain `.py:`; callable takes `list[str]` -> `list[list[float]]`, sync or async, or an `Embeddings` instance). `dims` must equal output length. `ttl:{refresh_on_read, default_ttl(min), sweep_interval_minutes}` **inert in dev**. `path` (API-only) = custom `BaseStore` instead of built-in (disables TTL sweeper, vector search up to you). `[VL]` for index; NumPy missing => pure-python vector ops warning, add `numpy`. |
| `checkpointer` | `{backend?:"default"\|"custom"\|"mongo", path?, uri?, ttl?, serde?}` | `backend` is API-only (inferred: `path` => custom). `ttl:{strategy:"delete"\|"keep_latest", default_ttl(min), sweep_interval_minutes, sweep_limit}` **inert in dev** `[VS]`. `serde:{allowed_json_modules, allowed_msgpack_modules, pickle_fallback}` allowlists for deserialising custom types. `path` = async context manager yielding a `BaseCheckpointSaver`. |
| `auth` | `{path, disable_studio_auth?, allow_langsmith_api_keys?, openapi?, cache?}` | `path` "./f.py:auth". `disable_studio_auth:true` closes the `x-auth-scheme: langsmith` bypass (then Studio cannot authenticate locally `[U]` how to feed it a token). `allow_langsmith_api_keys` (API-only; needs `LANGGRAPH_AUTH_TYPE=langsmith`). `openapi:{securitySchemes, security, paths}` merged into spec. `cache:{cache_keys, ttl_seconds, max_size}` caches auth decisions. |
| `encryption` | `{path}` | `langgraph_sdk.Encryption()` instance for at-rest encryption of blobs/metadata. Not tested. |
| `priority` | `{path}` | `langgraph_sdk.Priority()` assign-function for thread run priority. Python only. Not tested. |
| `http.app` | `"./f.py:app"` | Starlette/FastAPI **instance** (TypeError otherwise). Loaded at import; keep slow init out. |
| `http.mount_prefix` | `"/api"` (no trailing slash; `/noauth*` reserved) | Mounts *everything including your app* under the prefix; `/`, `/ok`, `/metrics` stay at root. Studio baseUrl must include prefix (printed). `[VL]` Don't use locally. |
| `http.cors` | `{allow_origins, allow_origin_regex, allow_methods, allow_headers, allow_credentials, expose_headers, max_age}` | **Replaces defaults** (default = origins `*`, credentials false, methods/headers `*`, expose `x-pagination-total,x-pagination-next,content-location`). Extra keys (e.g. `allow_private_network`) silently dropped `[VL]`. Env override: `CORS_ALLOW_ORIGINS`. |
| `http.configurable_headers` / `logging_headers` | `{includes, excludes}` (aliases `include`,`exclude`), fnmatch-style `*` | Headers -> `config.configurable`; `excludes` win. Defaults exclude `x-api-key, x-tenant-id, x-service-key`. `[VL]` |
| `http.middleware_order` | `"middleware_first"` (default) \| `"auth_first"` | `auth_first` re-applies custom middleware per-route *after* auth (so it no longer wraps CORS globally). |
| `http.enable_custom_route_auth` | bool | Applies auth to **all** custom routes incl. `/` and static files (browser page loads then 401). Default false = custom routes public. `[VL]` |
| `http.disable_assistants/threads/runs/store/mcp/a2a/ui/webhooks/meta` | bool | `disable_meta` removes `/info,/metrics,/docs,/openapi.json`, makes `/ok` shallow; `disable_webhooks` = runs still execute, no callback; `disable_runs` also kills event-streaming v2 and MCP calls. `disable_event_streaming` is read by `api/__init__.py` but absent from the schema (likely dropped) `[U]`. |
| `webhooks` | `{env_prefix?, url?, headers?, allowed_fields?}` | `env_prefix` default `LG_WEBHOOK_`. `headers` values may use `${{ env.LG_WEBHOOK_X }}`; **missing env var = startup ValueError**. `allowed_fields` subset of `run_id, thread_id, assistant_id, created_at, updated_at, metadata, kwargs, multitask_strategy, status, run_started_at, run_ended_at, webhook_sent_at, values, error`. `url:{require_https, allowed_domains(*.x), allowed_ports, max_url_length(2048 when block given), disable_loopback(default true), disable_private_ips(default false)}` - **when a `url` block is present `require_https` flips to true unless you set it false** `[VS][VL]`. |
| `ui` | `{name: "./file.tsx"}` | Generative UI bundles (needs Node/npx at runtime in dev). |
| `ui_config` | dict | Accepted by `validate_config` and forwarded (`LANGGRAPH_UI_CONFIG`) but absent from TypedDicts/docs. |
| *(not honoured)* `disable_persistence` | - | `cli.py` reads it from the config **after** `validate_config` dropped it -> always False. No CLI flag. Only workaround: delete `.langgraph_api/` or launch from a temp CWD. |

### 2.2 `langgraph dev` flags and defaults `[VS][VL]`

`--host 127.0.0.1 --port 2024 --config langgraph.json --no-reload --no-browser --n-jobs-per-worker (help says 10; real default 1) --allow-blocking --debug-port N --wait-for-client --studio-url URL --tunnel --server-log-level WARNING --ssl-certfile/--ssl-keyfile`. Port busy => silently picks a free port ("using port X instead"). `--env-file` does not exist (use `env` key). Other commands: `validate` (works offline), `dockerfile` (works offline), `build/up/deploy` (Docker/cloud), `new`.

Useful env vars (set via `env` or shell; `[VS]`): `N_JOBS_PER_WORKER`, `BG_JOB_ISOLATED_LOOPS` (one loop per run), `BG_JOB_TIMEOUT_SECS` (86400), `BG_JOB_MAX_RETRIES` (3), `RESUMABLE_STREAM_TTL_SECONDS` (120), `CRON_SCHEDULER_SLEEP_TIME` (5), `FF_CRONS_ENABLED`, `FF_V2_EVENT_STREAMING` (true), `LANGGRAPH_NO_VERSION_CHECK=true` (skips PyPI check), `LANGSMITH_TRACING`, `LANGSMITH_PROJECT`, `LANGGRAPH_AUTH_TYPE`, `CORS_ALLOW_ORIGINS`, `MOUNT_PREFIX`, `HTTP_MAX_REQUEST_BODY_BYTES`, `LANGGRAPH_THREAD_HISTORY_MAX_STATE_BYTES`, `A2A_A2UI_ENABLED`.

### 2.3 Recommended `langgraph.json` for this app

Adapt the module paths to the final package layout (placeholders `langgraph_app`). The exact JSON below (extracted programmatically from this file) passes `langgraph validate`, generates a Dockerfile with `langgraph dockerfile` (needs every referenced `.py` file to exist, else `FileNotFoundError: Could not find local module`), and each of the `http/store/auth/webhooks/checkpointer` blocks round-trips through langgraph-api's pydantic schemas with **no key dropped** `[VL]` (with `LG_WEBHOOK_SECRET` exported; webhooks validation reads it at parse time).

```json
{
  "python_version": "3.12",
  "dependencies": ["."],
  "graphs": {
    "assistant":  {"path": "./src/langgraph_app/graphs/assistant.py:graph",  "description": "Streaming chat agent with editable system prompt and tools"},
    "research":   {"path": "./src/langgraph_app/graphs/research.py:graph",   "description": "Map-reduce research workflow with nested review subgraph"},
    "approval":   {"path": "./src/langgraph_app/graphs/approval.py:graph",   "description": "Human-in-the-loop approval workflow"},
    "memory":     {"path": "./src/langgraph_app/graphs/memory.py:graph",     "description": "Long-term semantic memory agent"},
    "ops":        {"path": "./src/langgraph_app/graphs/ops.py:graph",        "description": "Scheduled/webhook-driven operations workflow"}
  },
  "env": ".env",
  "store": {
    "index": {"dims": 256, "embed": "./src/langgraph_app/memory/embeddings.py:embed", "fields": ["text"]},
    "ttl":   {"refresh_on_read": true, "default_ttl": 10080, "sweep_interval_minutes": 60}
  },
  "checkpointer": {
    "ttl": {"strategy": "keep_latest", "default_ttl": 43200, "sweep_interval_minutes": 60, "sweep_limit": 1000}
  },
  "auth": {
    "path": "./src/langgraph_app/auth.py:auth",
    "cache": {"cache_keys": ["authorization"], "ttl_seconds": 60, "max_size": 100}
  },
  "http": {
    "app": "./src/langgraph_app/web/app.py:app",
    "cors": {
      "allow_origins": ["https://smith.langchain.com", "http://127.0.0.1:2024", "http://localhost:2024", "http://localhost:3000"],
      "allow_origin_regex": "^https://([a-z0-9-]+\\.)?smith\\.langchain\\.com$",
      "allow_methods": ["*"], "allow_headers": ["*"], "allow_credentials": false,
      "expose_headers": ["content-location", "x-pagination-total", "x-pagination-next"],
      "max_age": 600
    },
    "configurable_headers": {"includes": ["x-user-*", "x-tenant-id"], "excludes": ["*key*", "*token*", "authorization", "cookie"]},
    "logging_headers": {"excludes": ["authorization", "x-api-key", "cookie"]},
    "enable_custom_route_auth": false
  },
  "webhooks": {
    "env_prefix": "LG_WEBHOOK_",
    "url": {"require_https": false, "disable_loopback": false, "max_url_length": 2048},
    "headers": {"x-webhook-secret": "${{ env.LG_WEBHOOK_SECRET }}"},
    "allowed_fields": ["run_id", "thread_id", "assistant_id", "status", "values", "error", "run_started_at", "run_ended_at"]
  },
  "image_distro": "wolfi"
}
```

Rationale / required companions:
- `.env.example` MUST define `LG_WEBHOOK_SECRET=dev-secret` and `LANGSMITH_TRACING=false` (a missing header env var aborts startup; with `LANGSMITH_API_KEY` present tracing auto-enables). Put `.env` in `.gitignore` (already). `env` string path is read relative to CWD and overrides the shell.
- `embed` = one callable that returns deterministic hashed vectors of length 256 offline, or a real model when keys exist (e.g. OpenAI `text-embedding-3-small` with `dimensions=256`). `dims` is baked at index creation; changing it requires wiping `.langgraph_api/store*.pckl`.
- `auth` is optional for the first milestone; if enabled, ship the default-deny + StudioUser handlers (Appendix A.4) and keep `disable_studio_auth` unset locally.
- Keep `middleware_order` unset (default) if you use the PNA middleware (A.3).
- Do not add `mount_prefix`, `ui` (until generative UI is deliberately built), `source` (until `package = false` is removed and a `[build-system]` exists; `dependencies:["."]` also needs an installable project: the generated image runs `uv pip install -e .` in each dependency dir).
- Run command: `cd <repo root> && uv run langgraph dev --port 2024 --n-jobs-per-worker 10 --no-browser` (CWD must hold `langgraph.json`).

---

## 3. LIVE EXPERIMENT RESULTS (port 2101, `langgraph dev`, in-memory)

Harness: `start.sh` wrapped `env -u LANGSMITH_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY LANGGRAPH_NO_VERSION_CHECK=true LANGSMITH_TRACING=false langgraph dev --config <cfg> --port 2101 --no-browser`. All models were an offline fake streaming `BaseChatModel`. No LangSmith, no paid calls. One accidental webhook POST to `https://example.com/hook` (my default-policy probe was accepted as valid) carried a fake `"hi"` run; nothing sensitive.

### (a) Multi-graph, schemas, xray `[VL]`

```
POST /assistants/search {"limit":20} -> 7 system assistants, e.g.
 {"assistant_id":"eb6db400-e3c8-5d06-a834-015cb89efe69","graph_id":"chat","name":"chat",
  "description":"Streaming chat with prompt-editable context","metadata":{"created_by":"system"},"version":1,"config":{},"context":{}}
 graph "agent" (create_agent(name="support_agent")) -> name "support_agent"; graphs without description -> description null
GET /assistants/{uuid}/schemas -> {graph_id,input_schema,output_schema,state_schema,config_schema,context_schema}
 config_schema == context_schema ==
 {"title":"Context","type":"object","properties":{
   "system_prompt":{"default":"You are a helpful assistant.","description":"System prompt for the chat model",
                    "langgraph_nodes":["call_model"],"langgraph_type":"prompt","title":"System Prompt","type":"string"},
   "model":{"__template_metadata__":{"kind":"llm"},"default":"fake/streamer-1","title":"Model","type":"string"},
   "tone":{"default":"neutral","title":"Tone","type":"string"}}}
GET /assistants/pipeline/graph        -> nodes [__start__,plan,worker,review,summarize,__end__]; plan->worker conditional:true
GET /assistants/{uuid}/graph?xray=true-> nodes also review:critique, review:polish; edges worker->review:critique, review:polish->summarize
GET /assistants/{uuid}/subgraphs      -> {"review":{input_schema,output_schema,state_schema,config_schema,context_schema}}
GET /assistants/approval/graph        -> human_review->publish/revise drawn (from Command[Literal[...]] return annotation)
```
Gotchas: `/graph` accepts the **graph id** as assistant id, but `/schemas`, `/subgraphs`, `GET /assistants/{id}` require the **UUID** (422 "Invalid assistant ID: must be a UUID"). Stable UUIDs are `uuid5`, so a dashboard can precompute them or look them up with `/assistants/search`. Creating an assistant with only `context` mirrors it into `config.configurable` (compat). **Assistant version rollback is incomplete**: `POST /assistants/{id}/latest {"version":1}` restores `config/metadata/name/description` but **not `context`** (`Assistants.set_latest` in inmem ops), so after rollback `GET` shows `version:1` with the *new* context and runs still use the new prompt `[VL][VS]`. If Studio's "mark Active" on an old version must work for context-based prompts, test it in the UI `[U]` (config-based `configurable` rolls back correctly).

### (b) Custom FastAPI app beside the built-ins; CORS; mount_prefix `[VL]`

Config: `"http": {"app": "./src/webapp.py:app"}`. Results:

| Request | Result |
|---|---|
| `GET /` | my dashboard HTML (shadowed built-in health) |
| `GET /info` when I defined it | my JSON (**breaks Studio feature flags - removed**); built-in returns `{"version","langgraph_py_version","flags":{assistants,crons,langsmith,...},"host":{...}}` |
| `GET /ok`, `/docs`, `/openapi.json`, `/metrics` | always built-in even if my app defines them (my `/ok` and `/docs` handlers ignored) |
| `POST /runs/cancel` (my handler) | **my handler won** (user routes precede protected built-ins) |
| `GET /static/index.html` (`app.mount("/static", StaticFiles)`) | 200 |
| `/openapi.json` | includes my routes (`info.title` becomes my FastAPI title) |
| response header `x-toy-mw` (BaseHTTPMiddleware) | present on built-in API responses too |
| lifespan | merged: built-in lifespan first (graphs loaded), then mine; `get_client()` loopback works inside it (seeded store item + assistant + even ran a graph, 0.03 s) |
| **`app.mount("/", StaticFiles(..., html=True))`** | `/ok /docs` fine but `POST /assistants/search`=405, `POST /threads`=405, `GET /info`=404, `GET /assistants/chat/graph`=404: **the SPA mount swallows the whole API** |

mount_prefix `"/lg"`: printed `Studio UI: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2101/lg`; `/lg/` dashboard 200, `/lg/assistants/search` ok, `/lg/api/ping` ok, but `/assistants/search` and `/api/ping` 404; `/`, `/ok`, `/metrics` remain at root. Absolute fetches like `fetch('/assistants/search')` in the dashboard break under a prefix.

CORS: default = `Access-Control-Allow-Origin: *`, methods `DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT,QUERY`, `max-age 600`, credentials not allowed with `*`.
- Explicit `http.cors` with `allow_origins:["https://smith.langchain.com", "http://localhost:3000"]`, credentials true, methods/headers `*`: preflight from smith origin returns `allow-origin: https://smith.langchain.com`, `allow-credentials: true`; unlisted origin gets no allow-origin header.
- **Minimal `{"allow_origins":["https://smith.langchain.com"]}`: preflight POST -> `400 Disallowed CORS method`; response header list lacks `content-location`.**
- **Preflight with `Access-Control-Request-Private-Network: true` -> `400 Disallowed CORS private-network`** with default *and* explicit config, even though langgraph-api's `PrivateNetworkMiddleware` adds `Access-Control-Allow-Private-Network: true` (starlette 1.10's `CORSMiddleware(allow_private_network=False)` rejects first). `allow_private_network` inside `http.cors` is silently dropped. A pure-ASGI user middleware that strips that request header on `OPTIONS` fixes it (Appendix A.3: 400 -> 200 with `access-control-allow-private-network: true`). Requires default `middleware_order`. Whether Chrome/Edge still send the header from `https://smith.langchain.com` -> `127.0.0.1` is `[U]` (docs/issue #3261 history says they did).

**Making dashboard and Studio coexist (recommended):** no `mount_prefix`; `GET /` returns `index.html`; static assets under `/app/*`; SPA fallback via an explicit `GET /app/{path:path}` route; dashboard talks to the same-origin built-in API (no CORS needed) or to a thin BFF under `/api/*`; leave CORS default (or the full explicit block in 2.3); do not define `/info`; add the PNA middleware. Studio then connects to `http://127.0.0.1:<port>` unchanged `[VL]` (API contract); the browser part of Studio is `[U]`.

### (c) Streaming: custom events, tokens, subgraphs, reconnect `[VL]`

`POST /threads/{id}/runs/stream` body `{"assistant_id":"pipeline","input":{"topic":"t"},"stream_mode":["updates","custom"],"stream_subgraphs":true}` produced:

```
metadata  {"run_id":"...","attempt":1}
custom    {"event":"plan.created","items":["t-0","t-1","t-2"]}
updates   {"plan":{"items":[...]}}
custom    {"event":"worker.done","item":"t-0"} (x3, parallel Send workers)
updates   {"worker":{"results":["done:t-0"]}} (x3)
custom|review:01f9d9e9-21dc-9581-38e0-d4251e03c682   {"event":"critique.started","n":3}
updates|review:01f9d9e9-...                          {"critique":{"verdict":"ok"}}
custom|review:... {"event":"polish.done"}   updates|review:... {"polish":{"verdict":"ok+polished"}}
updates   {"review":{"results":[...6 items...],"verdict":"ok+polished"}}
updates   {"summarize":{"summary":"6 results, verdict=ok+polished"}}
```
- `get_stream_writer()` works in `async def` nodes and inside the nested subgraph; namespace suffix `|review:<task_id>`.
- **Trap**: parent and subgraph shared key `results: Annotated[list, operator.add]`; the subgraph returns its full state, so the parent reducer re-adds the inputs -> 3 results became 6 ("6 results"). Use a different output key or a non-additive reducer at the boundary, or give the subgraph an `output_schema`.
- Chat with `context:{"system_prompt":"Be terse"}` + `stream_mode:["messages-tuple","values","updates"]`: 1 `metadata`, 2 `values`, 7 `messages` (token chunks of `AIMessageChunk` + metadata), 1 `updates`. The `context` override is applied per run.
- Reconnect (`stream_resumable:true`): started a run via `POST /threads/{t}/runs`, joined with `GET .../runs/{r}/stream`, dropped after 4 events, reconnected with `Last-Event-ID: 1794103563228-0` -> got the remaining 13 events; after completion, `Last-Event-ID: -1` or `0` replays all 17; no header after completion = 0 events; non-resumable run can only be joined live.
- `GET .../runs/{r}/join` blocks until done and returns final values. SDK `runs.stream(..., version="v2")` returns `{type,ns,interrupts,data}` parts.
- Protocol v2: `POST /threads/{t}/stream/events {"channels":["values","lifecycle","messages",...]}` + `POST /threads/{t}/commands {"id":1,"method":"run.start","params":{...}}` -> `{"type":"success","id":1,"result":{"run_id":...},"meta":{"applied_through_seq":0}}`; SSE `event:` = channel, `id:` = seq; `messages` carries content-block events (`message-start`, ...). `agent.getTree` with no active run -> `no_such_run`. `updates/tasks/custom` channels yielded nothing for my graphs `[U]`.

### (d) Interrupt + resume through the API `[VL]`

```
POST /threads/{t}/runs/wait {"assistant_id":"approval","input":{"request":"ship v2"}}
 -> {"request":...,"draft":"DRAFT for: ship v2","log":["drafted"],
     "__interrupt__":[{"value":{"kind":"approval","draft":"...","question":"Approve this draft?"},"id":"bd093a...","response_schema":null}]}
GET /threads/{t}          -> status "interrupted", interrupts {"<task_id>":[{"id","value"}]}
GET /threads/{t}/state    -> next ["human_review"], tasks[0].interrupts[...], checkpoint.checkpoint_id, parent_checkpoint
POST .../runs/wait {"assistant_id":"approval","command":{"resume":{"approved":false}}} -> loops revise -> interrupts again
POST .../runs/wait {... "command":{"resume":{"approved":true}}} -> "...published", thread status idle
GET /threads/{t}/history  -> 7 checkpoints (step -1 ... 5), metadata.source input|loop
POST /threads/{t}/state {"values":{"draft":"EDITED BY HUMAN"},"checkpoint":<cp>,"as_node":"draft"} -> {"checkpoint":{...new id}}
POST .../runs/wait {"assistant_id":"approval","checkpoint":<new cp>} -> re-runs from the edit, interrupts at human_review again (fork)
interrupt_before:["draft"] -> returns {"request":"bp","log":[]}; state.next ["draft"]; continue with input:null
```
With `interrupt(..., response_schema=Decision)` (Pydantic): `"response_schema":{"properties":{"approved":{"type":"boolean"},"comment":{"default":"","type":"string"}},"required":["approved"],"title":"Decision","type":"object"}`. Interrupt inside subgraph: run output contains `__interrupt__`; `state` shows `next:["review"]`; `state?subgraphs=true` -> `tasks[0].state.values` = subgraph values and `.next:["polish"]`; resume from the root thread works. The interrupted thread survived a restart and resumed `[VL]`.

### (e) Store API with semantic index `[VL]`

Config `"store":{"index":{"dims":8,"embed":"./src/embed.py:embed","fields":["text"]},"ttl":{...}}`, `embed(texts)->list[list[float]]` (hashed bag-of-words, local).

```
PUT    /store/items {"namespace":["memories","u1"],"key":"a","value":{"text":"..."},"index":false?, "ttl":minutes?} -> 204
GET    /store/items?namespace=memories.u1&key=a -> {"namespace":[...],"key","value","created_at","updated_at"}   (namespace is dot-joined)
POST   /store/items/search {"namespace_prefix":["memories"],"query":"outdoor hiking","filter":{...},"limit":3,"offset":0}
        -> {"items":[{"namespace","key","value","created_at","updated_at","score":0.68}, ...]}  (score only with query)
POST   /store/namespaces {"prefix":["memories"],"max_depth":2} -> {"namespaces":[["memories","u1"]]}
DELETE /store/items  (JSON body {"namespace","key"}) -> 204
```
`index:false` items are excluded from vector results but returned by plain search. In graph: `await runtime.store.aput(("memories", uid), key, {...})`, `await runtime.store.asearch(ns, query=..., limit=3)` (hits have `.score`). Startup log warns "NumPy not found... pure Python vector ops" -> add `numpy`. **TTL not enforced**: item put with `ttl:0.02` min (1.2 s) was still readable at +6 s. Store contents + vectors survive restarts (`store.pckl`, `store.vectors.pckl`).

### (f) Crons, webhooks, MCP, A2A - what works in-memory `[VL]`

| Feature | In `langgraph dev`? | Detail |
|---|---|---|
| Crons | **Yes** | `POST /runs/crons {"assistant_id":"pipeline","schedule":"* * * * *","input":{...},"on_run_completed":"keep","metadata":{...}}` -> cron with `next_run_date` (UTC, minute boundary, scheduler polls every 5 s). Fired at 02:08:02, created a new thread (`keep`), next_run advanced. Thread cron: `POST /threads/{id}/runs/crons`. `PATCH /runs/crons/{id} {"enabled":false}`. `POST /runs/crons/search|count`. **Persisted across restarts and kept firing during my other tests until `DELETE`d**; with auth enabled, persisted crons logged `404 Thread or assistant not found`. |
| Webhooks | **Yes, but policy blocks local receivers by default** | Defaults reject `/relative` ("Loopback webhooks ... disabled by default"), `http://127.0.0.1:...` ("Webhook host blocked: loopback IP range"), `http://localhost:...` (422). With `webhooks.url={"require_https":false,"disable_loopback":false}` both relative and absolute loopback URLs worked; payload keys filtered by `allowed_fields`; header `x-hook-secret` templated from `${{ env.LG_WEBHOOK_SECRET }}` (env set through langgraph.json `env`). A run paused at interrupt reports `status: success` `[VD]`. |
| MCP `/mcp` | **Yes (limited)** | `initialize` -> `{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"LangGraph","version":"0.17.1"}}`; `tools/list` -> one tool per assistant: name from assistant name (`support_agent`, `chat`, ...), `inputSchema` props = graph input schema (`messages` for chat; `topic,items,results,verdict,summary` for pipeline); `tools/call` -> `{"content":[{"type":"text","text":"{'topic': 'mcp', ...}"}]}` (Python repr!). `Accept` must include json or event-stream. `GET`=405. No origin check at 0.17.1 (docs describe one for >=0.18). Interrupting graphs return `__interrupt__` in the repr text. |
| A2A | **Yes** | `GET /a2a/{uuid}/.well-known/agent-card.json` -> `{"name","description","url","supportedInterfaces":[{"protocolBinding":"JSONRPC","protocolVersion":"1.0"}],"capabilities":[...],"skills":[{... "metadata":{"inputSchema":...}}]}`. `POST /a2a/{uuid}` `{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hello a2a"}],"messageId":"m1","kind":"message"}}}` -> `result.kind:"task"`, `id:"<contextId>:<runId>"`, `history`, `status.state:"completed"`, `artifacts`. Non-chat graph with `{"kind":"data","data":{...}}` works. Creates a normal thread with `metadata.a2a`. |
| Needs full platform / LangSmith | - | Docker `up/build`, `deploy`, Postgres-only behaviours (TTL sweeps, pg checkpointer/store durability, multi-worker queue), LangSmith traces/datasets/experiments/playground/feedback keys, one-click deploy, no-code agent builder `[VD]`. |

### (g) Custom auth and how Studio authenticates `[VL][VS]`

Toy `auth.py` (Bearer tokens `tok-alice`/`tok-bob`; `@auth.on` default deny; owner stamping; store namespace guard):

| Request | Result |
|---|---|
| `GET /ok`, `/`, `/api/ping`, `/static/*`, `/openapi.json`, `/docs` (custom routes, `enable_custom_route_auth` unset) | 200 public |
| `POST /assistants/search` no/bad token | 401 `{"detail":"missing bearer token"}` / `"bad token"` |
| alice creates thread; bob `GET` it | 404; bob `/threads/search` -> `[]` (metadata `owner` filter) |
| store put in own namespace / other namespace | 204 / 403 with my message |
| `POST /runs/crons` as alice (only default-deny matched) | 403 `Forbidden` |
| header `x-auth-scheme: langsmith`, no token | **200, authenticated as `StudioUser("langgraph-studio-user")`**; sees all threads; bogus `x-api-key` still accepted |
| `GET /api/run-sse/pipeline` (BFF using `get_client()` loopback) with no credentials | **runs the graph** (loopback transport uses `root_path=/noauth` = auth bypass) |
| `memory` graph run as alice | `server_info.user` populated (`runtime.server_info.user.identity`) |
| D2: `enable_custom_route_auth:true` | `/`, `/api/ping`, `/static/*` -> 401 without token; with token `request.user.identity == "alice"`; `/ok`, `/docs`, `/openapi.json` stay public |
| D2: `disable_studio_auth:true` | `x-auth-scheme: langsmith` -> 401 |

How it works: `CustomAuthBackend` (a) when `x-auth-scheme: langsmith` and `disable_studio_auth` is false: in `langgraph dev` (`LANGSMITH_LANGGRAPH_API_VARIANT=local_dev` + `LANGGRAPH_AUTH_TYPE=noop`) uses `StudioNoopAuthBackend` (accept everyone); on the platform (`LANGGRAPH_AUTH_TYPE=langsmith`) it validates `x-api-key` against LangSmith; (b) otherwise runs your `@auth.authenticate`. Handlers resolve most-specific-first (resource+action, resource, action, global); **unhandled pairs are allowed** (startup logs a warning listing them). `/ui` GET asset requests skip auth. Websocket handshake counts as `GET`. `allow_langsmith_api_keys` lets LangSmith keys (header `x-api-key`) coexist with Bearer tokens `[VD]` (not run).

Design consequence for the dashboard: use Bearer tokens (not cookies) so CORS stays simple; dashboard HTML/static are public unless `enable_custom_route_auth`; any BFF route using `get_client()` must do its own authentication.

### (h) Blocking-call behaviour `[VL][VS]`

- Default `langgraph dev`: `blockbuster` is activated by the queue (process-wide, applies to any code on a running event loop incl. custom routes). `async` node calling `time.sleep(0.01)` => run fails: result `{"__error__":{"error":"BlockingError","message":"An internal error occurred"}}`; server log contains "Blocking call to time.sleep ... Dev-only override: run 'langgraph dev --allow-blocking'".
- `open(...).read(10)` in an async node did **not** fail (file/stat/listdir reads/writes were removed from the blocklist "because people use them so often"). Sync `def` nodes can block freely (thread executor).
- Still guarded (45 functions): `time.sleep`, `socket.socket.{connect,send,sendall,recv*,accept}`, `ssl.SSLSocket.*`, `sqlite3.*`, `os.{scandir,getcwd,mkdir,rename,replace,unlink,read,write,symlink,link,readlink,sendfile,statvfs,access}`, `builtins.input`, `threading.Lock.acquire_lock`, `io.BufferedRandom.read/write`.
- `--allow-blocking`: same run succeeded; server logs a warning. Not available in prod; fix with `asyncio.to_thread` or async clients.
- `--n-jobs-per-worker N`: sets worker slots (log "Starting N background workers"). With 5: three concurrent 4 s runs finished in 4.8 s total; default (1): 5 s then 9 s. `BG_JOB_ISOLATED_LOOPS=true` gives each run its own loop (isolates blocking, not tested).

### (i) What persists across restarts `[VL]`

`.langgraph_api/` in CWD holds `.langgraph_checkpoint.{1,2,3}.pckl`, `.langgraph_ops.pckl` (assistants, threads, runs, crons, versions), `.langgraph_retry_counter.pckl`, `store.pckl`, `store.vectors.pckl`. Flush loop every 10 s (+ on graceful shutdown). After SIGINT+restart: 13 threads, 2 crons, 9 assistants (user-created `pirate-chat` kept; system ones re-registered, same ids), 6 store items + working vector search, interrupted thread resumed. After true SIGKILL of all server processes (`kill -9` parent + reload child) store writes made <10 s earlier were **lost**. Trap: `pkill -9 -f "port 2101"` only kills the reloader parent; the uvicorn child (`multiprocessing.spawn`) survives, keeps the port, and a new `langgraph dev` silently moves to another port. Use SIGINT (flushes) and kill by process group. `disable_persistence` cannot be set (see 2.1); delete `.langgraph_api` for a clean demo reset. Thread/run TTL and store TTL do nothing locally.

### (j) Other behaviours worth knowing `[VL]`

- **Double texting**, slow 2-step graph, second run fired 1.5 s into the first: `reject` -> 409 `"Thread is already running a task. Wait for it to finish or choose a different multitask strategy."`; `enqueue` -> first 5.0 s, second 7.5 s, log has both runs' steps; `interrupt` -> first run ends `interrupted` (log `[]`, step 1 hadn't committed), second runs from kept state; `rollback` -> first run removed (only 1 run remains), second runs clean.
- Trivial run latency ~1.0 s (queue poll) with 1 slot, ~0.8 s with 5.
- `POST /threads/search` `extract` returned `{"extracted":{"last":"<content>"}}` per thread; `select` limits columns (`thread_id,created_at,updated_at,state_updated_at,metadata,config,status,values,interrupts`); `status` filter works.
- Graph factory `make_graph(config, runtime: ServerRuntime)` as `@asynccontextmanager`: ran with `runtime.access_context == "threads.create_run"`; `/schemas` and `/graph` calls invoke it with `assistants.read`.
- `x-user-name: gary` + `configurable_headers.includes:["x-user-*"]` reached `config["configurable"]["x-user-name"]`; `x-api-key` and unlisted `x-other` did not.
- Node `metadata={"studio_hint":...}` appears in `GET /assistants/{id}/graph` as `nodes[].metadata`.
- Runs that fail in user code return `{"__error__": {...}}` from `/runs/wait` with HTTP 200.
- Version check thread hits PyPI at startup unless `LANGGRAPH_NO_VERSION_CHECK=true`.

---

## 4. STUDIO SHOWCASE CHECKLIST (ranked by wow / effort)

Wow: H/M/L. Effort: S/M/L. "Feature" = the code that must exist.

| Rank | Demo in Studio (+ dashboard) | Required code/config feature | Wow | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | Token-streaming chat in **Chat mode** (tool calls toggle) | state extends `MessagesState`; node calls model via `ainvoke` (streams via callbacks); fake model with `_astream` offline; `create_agent` variant | H | S | `[VL]` stream, `[VD]` chat |
| 2 | **Subgraph drill-in** (research -> review subgraph, 2 levels) | compiled subgraph added as node; unique node names; distinct output keys | H | S | `[VL]` |
| 3 | **Edit prompt via gear icon**, save as new assistant version, rerun, compare | `context_schema` dataclass with `langgraph_nodes`+`langgraph_type:"prompt"`; node reads `runtime.context` | H | S | `[VL]` schema, `[VD]` UI |
| 4 | **Human approval**: pause, edit draft, Continue / reject loop | `interrupt()` with `response_schema`; `Command(goto=)` with `Literal` annotation | H | M | `[VL]` |
| 5 | **Time travel**: edit state at a checkpoint, fork, compare branches | server checkpointer; many small nodes; informative state keys | H | S | `[VL]` |
| 6 | **Parallel map-reduce** visible as concurrent tasks + live custom progress | `Send` fan-out + `get_stream_writer()` events + reducers | H | S | `[VL]` |
| 7 | **Dashboard at `/` on the same server** (threads, runs, live stream, interrupt forms, store explorer) | `http.app` FastAPI: `GET /`, `/app/*`, optional `/api/*` BFF; never mount at `/`; no `/info` | H | M | `[VL]` |
| 8 | **Semantic memory** + Studio memory view (remember/recall across threads and users) | `store.index` with local `embed` callable; namespaces `("memories", user_id)`; `runtime.store`; numpy | H | M | `[VL]` |
| 9 | **Auth**: Alice/Bob isolation, Studio sees everything | `auth.py`, default-deny `@auth.on`, `StudioUser` pass-through, store namespace guard | H | M | `[VL]` |
| 10 | **Double-texting** switcher | slow cancellable graph; dashboard sends `multitask_strategy`; `--n-jobs-per-worker >=2` | M | S | `[VL]` |
| 11 | **Reconnectable stream** (reload tab mid-run) | `stream_resumable:true`, `Last-Event-ID`, `Content-Location` exposed | M | S | `[VL]` |
| 12 | **Cron** pipeline with visible history + delete button | `POST /runs/crons` `on_run_completed:"keep"`; seed disabled; document UTC; delete on cleanup script | M | S | `[VL]` |
| 13 | **Webhook** feed (run completion events) | `webhooks.url` loosened for dev, `/hooks/*` receiver route, secret header via `LG_WEBHOOK_*` | M | S | `[VL]` |
| 14 | **MCP tools** (`/mcp`) consumed by Claude Code/other agents | graph `description`s; **narrow typed `input=` schemas** (not `MessagesState`) on tool-like graphs | M | S | `[VL]` |
| 15 | **A2A agent card** + `message/send` | compiled `name` + `description`; chat-style or `data` input | M | S | `[VL]` |
| 16 | **Persistence/restart demo**: interrupted thread resumes after `Ctrl-C` + restart | nothing; document SIGINT vs SIGKILL, wipe command | M | S | `[VL]` |
| 17 | **Static breakpoints** via Studio "Interrupt" menu on any root node | granular root-level nodes (not hidden in subgraphs) | M | S | `[VL]` |
| 18 | **LangSmith trace panel + Playground** (opt-in) | `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT` in `.env`; tags/metadata on runs (`config={"tags":[...],"metadata":{...}}`) | H | S | `[VS]`; UI `[VD]`/`[U]` |
| 19 | **Datasets & experiments**: seed a dataset matching the input schema, "Run experiment", "Add node to dataset" | `langsmith` client seed script, evaluators; input schema == dataset inputs; opt-in | H | M | `[VD]` |
| 20 | **Node resilience**: retry then error_handler, per-node `TimeoutPolicy`, `CachePolicy` visible in traces | `add_node(retry_policy=, timeout=, error_handler=, cache_policy=)` | M | M | `[VS]` |
| 21 | **Per-user graph factory** (`ServerRuntime`) + `runtime.server_info` in output | factory graph, same topology in every access context | M | M | `[VL]` |
| 22 | **Protocol v2 thread stream** powering the dashboard live view | `POST /threads/{id}/stream/events` + `/commands` | M | M | `[VL]` partial |
| 23 | **Generative UI** weather/approval card in chat | `ui` key + `push_ui_message` + `ui` state reducer; needs Node + npx fetch | H | L | `[VS][VD]` not run |
| 24 | **Streaming modes explorer** in dashboard (values/updates/messages/custom/debug/tasks) | server modes; show per-mode panes | M | S | `[VL]` |
| 25 | **Header-driven config** (`x-user-*` -> `configurable`) in a node | `http.configurable_headers.includes` | L | S | `[VL]` |

---

## 5. TRAPS FOR THE IMPLEMENTER (ranked by likelihood x pain)

1. `app.mount("/", StaticFiles)` kills the API; shadowing `/info` breaks Studio flags; mount static under `/app`, `/static`.
2. Default worker concurrency is 1 (despite help text). Dashboard + Studio + cron will queue behind each other; run with `--n-jobs-per-worker 10` and bake it into the Makefile/launch script.
3. `http.cors` replaces defaults (methods become GET-only). Provide the full block (2.3).
4. Starlette 1.x rejects PNA preflights (400); langgraph-api cannot configure it. Use the middleware (A.3) or leave browsers to retry; keep default `middleware_order`.
5. Auth fails open + Studio bypass header: ship default-deny, `StudioUser` allowance, and do not expose the dev port beyond 127.0.0.1. Loopback `get_client()` skips auth.
6. Webhook defaults block `localhost`; adding a `webhooks.url` block silently flips `require_https` to true; `${{ env.* }}` header refs abort startup if unset.
7. Relative paths + `.langgraph_api` + `.env` are CWD-relative; `langgraph.json` edits need a manual restart; `.py` edits restart the process and re-run your lifespan (seed idempotently, e.g. `if_exists="do_nothing"`).
8. Crons persist across restarts and keep spending; give the dashboard a cron delete button and a reset script (`rm -rf .langgraph_api` while stopped).
9. Subgraph with shared additive reducer keys double-counts; `interrupt_before` does not reach nodes inside subgraphs.
10. Assistant version rollback does not restore `context` (inmem); prompt editing via `context_schema` therefore does not round-trip on "mark active" in dev `[VS][VL]`; Studio behaviour `[U]`.
11. `time.sleep`/sync HTTP in async code => `BlockingError` hidden behind "An internal error occurred" in the API; read the server log. Use `httpx.AsyncClient`, `asyncio.to_thread`.
12. Store/thread TTL never fire locally; do not build demos on them; semantic search quality with the fake embedder is arbitrary (hash buckets), so demo with exact-ish phrases or use a small real embedding when keys exist.
13. `graph_id` works as assistant id only on `/graph` and run endpoints; `/schemas`, `/subgraphs`, `GET /assistants/{id}` need the UUID.
14. MCP returns Python `repr()` text and has no sessions/GET; A2A cards use the assistant UUID.
15. `LANGSMITH_API_KEY` in the shell auto-enables tracing for every `langgraph dev` run (control-plane key defaults to it); set `LANGSMITH_TRACING=false` in `.env` for offline demos.
16. Generative UI in dev shells out to `npx @langchain/langgraph-ui@latest` and exits the whole server if it dies.
17. `[tool.uv] package = false` in `pyproject.toml` blocks `source.kind: "uv"` and makes `pip install -e .` in the image unreliable; add `[build-system]` + a src layout before a Docker/deploy milestone.
18. `pkill -9 -f "port N"` leaves the reload child alive; stale server answers on the old port while a new one moves to a random port.

## 6. NOT VERIFIED / NOT RUN

- Anything inside the Studio web UI itself (needs a LangSmith login): exact controls, whether it sends `Access-Control-Request-Private-Network`, how it authenticates when `disable_studio_auth:true`, memory viewer layout, assistant "mark active" behaviour with `context`.
- LangSmith-dependent features (traces, Playground, datasets, experiments, feedback keys, `langsmith_tracer.example_id` end to end): not invoked to avoid network/paid calls.
- Generative UI end to end (`ui` key / npx bundler) - would download and execute an npm package.
- `--tunnel` (Cloudflare), `--debug-port`, `--ssl-*`, `encryption`, `priority`, custom `checkpointer.path`/`store.path`, `BG_JOB_ISOLATED_LOOPS`, `/threads/prune`, `middleware_order: auth_first`, `stream_transformers` custom channels, `TimeoutPolicy`/`error_handler` runtime behaviour, `strict_resume`, `durability` modes.
- Docker flows (`up`, `build`) - daemon not running; only `langgraph dockerfile` (offline) was generated.
- Docs vs installed drift: docs describe MCP origin check/JSON results/HITL for langgraph-api >= 0.18.0; installed is 0.17.1 (repr results, no origin check).

---

## Appendix A - copy-paste snippets (all exercised in the toy project)

### A.1 Context with Studio prompt metadata (dataclass) `[VL]`

```python
from dataclasses import dataclass, field
from langgraph.graph import StateGraph, MessagesState
from langgraph.runtime import Runtime

@dataclass(kw_only=True)
class Context:
    system_prompt: str = field(
        default="You are a helpful assistant.",
        metadata={"description": "System prompt for the chat model",
                  "json_schema_extra": {"langgraph_nodes": ["call_model"], "langgraph_type": "prompt"}})
    model: str = field(default="fake/streamer-1",
        metadata={"json_schema_extra": {"__template_metadata__": {"kind": "llm"}}})

async def call_model(state: MessagesState, runtime: Runtime[Context]) -> dict: ...

builder = StateGraph(MessagesState, context_schema=Context)
graph = builder.compile(name="chat")      # name becomes the assistant name
```
Pydantic equivalent: `system_prompt: str = Field(default=..., json_schema_extra={"langgraph_nodes":["call_model"],"langgraph_type":"prompt"})`.

### A.2 Dashboard app skeleton that coexists with the API `[VL]`

```python
import contextlib, pathlib
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph_sdk import get_client          # url=None => in-process loopback (NO auth)

STATIC = pathlib.Path(__file__).parent / "static"

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    client = get_client()                      # graphs are already loaded here; runs also work
    await client.assistants.create(graph_id="chat", assistant_id="<fixed-uuid>", name="seeded",
                                   if_exists="do_nothing", context={"system_prompt": "Seeded"})
    yield

app = FastAPI(title="langgraph-app dashboard", lifespan=lifespan)

@app.get("/")                                   # shadows built-in health root: OK
async def index(): return FileResponse(STATIC / "index.html")

app.mount("/app", StaticFiles(directory=STATIC), name="app")   # NEVER mount at "/"
# do NOT define /info, /ok, /docs, /openapi.json, /metrics
```

### A.3 PNA preflight workaround middleware `[VL]` (default `middleware_order` only)

```python
class StripPNA:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in scope["headers"]
                                if k != b"access-control-request-private-network"]
        await self.app(scope, receive, send)
app.add_middleware(StripPNA)    # langgraph-api's PrivateNetworkMiddleware then adds Allow-Private-Network: true
```

### A.4 Auth skeleton `[VL]`

```python
from langgraph_sdk import Auth
auth = Auth()

@auth.authenticate
async def authenticate(authorization: str | None) -> Auth.types.MinimalUserDict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Auth.exceptions.HTTPException(status_code=401, detail="missing bearer token")
    user = TOKENS.get(authorization[7:])           # e.g. {"tok-alice": "alice"}
    if not user: raise Auth.exceptions.HTTPException(status_code=401, detail="bad token")
    return {"identity": user, "display_name": user.title(), "permissions": ["threads:rw"]}

studio = lambda ctx: isinstance(ctx.user, Auth.types.StudioUser)

@auth.on                                           # default deny (everything unhandled fails open otherwise)
async def default_deny(ctx, value): return studio(ctx)

@auth.on.threads.create
@auth.on.threads.create_run
async def stamp(ctx, value):
    if studio(ctx): return {}
    value.setdefault("metadata", {})["owner"] = ctx.user.identity
    return {"owner": ctx.user.identity}

@auth.on.threads.read
@auth.on.threads.update
@auth.on.threads.delete
@auth.on.threads.search
async def own(ctx, value): return {} if studio(ctx) else {"owner": ctx.user.identity}

@auth.on.assistants
async def assistants(ctx, value): return True      # system assistants have no owner metadata
```
Also add handlers for `crons`, `store` as needed (cron create is denied by the default handler).

### A.5 Offline fake streaming chat model (messages-tuple tokens) `[VL]`

`BaseChatModel` subclass implementing `_generate`, `_stream` (call `run_manager.on_llm_new_token(tok, chunk=chunk)`) and `_astream` (same, awaited). A node that does `await model.ainvoke(msgs)` inside a graph run streams tokens to `messages`/`messages-tuple` consumers because the stream handler is attached to the callback manager. For `create_agent`, override `bind_tools` to return `self` and emit `tool_calls` on the first turn (worked with `langchain.agents.create_agent(model, tools, system_prompt=, name=)`; xray shows `model <-> tools`).

### A.6 Useful one-liners

```bash
curl -s -XPOST :2024/assistants/search -H 'content-type: application/json' -d '{"limit":50}'
curl -s -N -XPOST :2024/threads/$T/runs/stream -H 'content-type: application/json' \
  -d '{"assistant_id":"research","input":{"topic":"x"},"stream_mode":["updates","custom","messages-tuple"],"stream_subgraphs":true}'
curl -s -XPOST :2024/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# graceful stop (flushes .langgraph_api): kill -INT <langgraph dev pid>
```
