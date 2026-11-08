# 02 - Graph architecture, feature ledger and catalog

Author: Core LangGraph Architect. Status: design + verified probes (no app code written).
Scope: what LangGraph 1.2.20 / Studio can really do on THIS machine, which graphs to build, and the contracts between them.

## 0. Reading guide

**Installed (verified with `.venv/bin/pip list`):** langgraph 1.2.20, langgraph-prebuilt 1.1.1, langgraph-checkpoint 4.3.1, langgraph-api 0.17.1 (+ langgraph-runtime-inmem 0.37.1), langgraph-cli 0.4.42, langgraph-sdk 0.4.14, langchain 1.4.9, langchain-core 1.7.6, langchain-anthropic 1.7.10, langchain-openai 1.7.4, langsmith 0.14.17, pydantic 2.14, Python 3.12.13. `langgraph-supervisor`, `langgraph-swarm`, `deepagents`, `numpy` are NOT installed.

**Evidence labels** used on every claim:

| Label | Meaning |
|---|---|
| `SRC file:line` | read in installed source under `.venv/lib/python3.12/site-packages` (authoritative) |
| `RUN pNN/cNN` | I executed a probe and saw the output. `pNN` = local script, `cNN` = client script against a real `langgraph dev` server on :2102 (stopped afterwards). Files are in `$S` (below) |
| `DOCS url` | read on docs.langchain.com / GitHub releases |
| `UNVERIFIED` | inferred or not run; do not build on it without a probe |

`$S` = `/private/tmp/claude-501/-Users-garyamorris--claude-spares-spare-lRCPrl/a9a93820-8d04-4501-adf4-c6aae828bd1f/scratchpad/architect`. Probe index is in Appendix C. The dev-server probe project (4 graph modules, custom FastAPI app, `langgraph.json`) is `$S/devproj`.

**Studio column** in the ledger: `UI` = Studio documents/implements this in its UI (DOCS); `API` = the Agent-Server endpoint Studio drives was exercised by me (RUN), but I did NOT drive the hosted Studio UI itself (it needs a LangSmith login; I did not sign in); `-` = code-only, Studio cannot show it.

**Hard limits of this research**: I never saw the Studio web UI. Every "Studio shows X" below is either DOCS or "the server API that would feed it works".

---

## 1. LangGraph 1.2.20 feature ledger

### 1.1 StateGraph surface

| Feature | Exact API (installed) | Evidence | Studio |
|---|---|---|---|
| Builder | `StateGraph(state_schema, context_schema=None, *, input_schema=None, output_schema=None)`; `config_schema=` is deprecated -> `context_schema` | SRC graph/state.py:216 | API (`/assistants/{id}/schemas` returns input/output/state/config/context schemas) RUN c1 |
| Compile | `.compile(checkpointer=None, *, cache=None, store=None, interrupt_before=None, interrupt_after=None, debug=False, name=None, transformers=None)` | SRC state.py:1177 | - |
| Node | `add_node(node, action=None, *, defer, metadata, input_schema, retry_policy, cache_policy, error_handler, destinations, timeout, trace_policy)` | SRC state.py:667 | metadata/defer visible in `get_graph()` (RUN p2a: `late` shows `{'defer': True}`) |
| Node defaults | `set_node_defaults(*, retry_policy, cache_policy, error_handler, timeout)`; applied at compile; NOT inherited by subgraphs; `cache_policy`/`error_handler` defaults skip error-handler nodes | SRC state.py:272 | - |
| In/out schemas | Input/output schemas trim what callers see. RUN p2a: input props `['topic']`, output `['report']`, internal `log` hidden | RUN p2a | UI: Studio builds the input form from the state/input schema (DOCS use-studio) |
| Node-private input | `add_node(name, fn, input_schema=Priv)` gives a node its own view of state | RUN p10 | - |
| State types | TypedDict (recommended), Pydantic BaseModel, dataclass. Server rendered a Pydantic input schema with `$defs`, enums, `date-time` | RUN c8 | API |
| Reducers | `Annotated[list[T], operator.add]`; custom `(old, new)->value`; same key written by two parallel nodes without reducer -> `InvalidUpdateError: Can receive only one value per step` | RUN p3 | - |
| Overwrite | `from langgraph.types import Overwrite`; node may return `{"k": Overwrite(v)}`; over HTTP the JSON form `{"type": "__overwrite__", "value": v}` works in `update_state` | SRC types.py (class Overwrite); RUN p3 | API |
| DeltaChannel (beta) | `Annotated[list, DeltaChannel(reducer(state, [writes])->state, snapshot_frequency=100)]` stores only deltas in checkpoints; reducer must be batching-invariant; default snapshot every 100 updates (1.2.18) | SRC channels/delta.py; DOCS releases 1.2.18; RUN p6 (blobs 0,0,0,0,13,22 bytes) | - |
| Other channels | `channels/`: binop, last_value, topic, ephemeral_value, untracked_value, named_barrier_value, any_value, delta | SRC langgraph/channels | - |
| Managed values | `from langgraph.managed import RemainingSteps, IsLastStep`; declare `remaining_steps: RemainingSteps` in state; counts down from `recursion_limit`; **default recursion limit is 10007** (`LANGGRAPH_DEFAULT_RECURSION_LIMIT`) | SRC _internal/_config.py:43; RUN p18 | - |
| Messages | `MessagesState`, `add_messages` (merge by id, `RemoveMessage(id)`, `RemoveMessage(id=REMOVE_ALL_MESSAGES)`), `trim_messages`, `filter_messages`, `count_tokens_approximately` | RUN p3 | UI chat mode needs `messages` in state (DOCS studio) |

### 1.2 Runtime, context, injection

| Feature | Exact API | Evidence |
|---|---|---|
| `Runtime[Ctx]` | dataclass: `context`, `store`, `stream_writer`, `heartbeat`, `previous`, `execution_info(checkpoint_id, checkpoint_ns, task_id, thread_id, run_id, node_attempt, node_first_attempt_time)`, `server_info(assistant_id, graph_id, user)`, `control(RunControl)`, props `drain_requested`/`drain_reason` | SRC runtime.py:96-205 |
| Injection | by parameter name/type: `runtime: Runtime[Ctx]`, `config: RunnableConfig`, `store`, `writer`, `previous`; handlers take `error: NodeError` (type annotation REQUIRED, otherwise "missing 1 required positional argument") | SRC _internal/_runnable.py:238-260; RUN p8 |
| Getters | `langgraph.config.get_config()`, `get_store()`, `get_stream_writer()`; `langgraph.runtime.get_runtime()` | SRC config.py |
| `context=` semantics | **local**: `runtime.context` is `None` unless you pass `context=` (even if the dataclass has defaults) -> `ctx.attr` crashes. **server**: always an instance (defaults applied), filled from `context=`, from `config.configurable` keys, or from an assistant preset; `runtime.server_info` populated | RUN p8 (crash), c12 |
| Cooperative drain | `RunControl().request_drain()`; pass `control=` to `stream/invoke`; nodes read `runtime.drain_requested` | SRC runtime.py:60 (not run) |

### 1.3 Control flow

| Feature | Exact API / behaviour | Evidence | Studio |
|---|---|---|---|
| `Command` | `Command(graph=None\|Command.PARENT, update=, resume=, goto=)`; goto = node name, list, `Send`, list of `Send` | SRC types.py:782 | - |
| goto + update + Send | node returns `Command(update=..., goto=[Send("worker", {...}), ...])` | RUN p10 | - |
| `Command.PARENT` | subgraph node returns `Command(graph=Command.PARENT, goto="finalize", update={...})`; parent's state keys merge | RUN p2d (all 3 checkpointer modes) | - |
| Edge drawing for Command routing | edges appear only with `add_node(..., destinations=("a","b"))` or a `-> Command[Literal["a","b"]]` return annotation; with neither, the node draws a lone edge to `__end__` | RUN p14 | API (graph json) |
| `Send` | `Send(node, arg, *, timeout=None)`; arg may differ from graph state; collecting key needs reducer | SRC types.py:786; RUN p8 | - |
| `defer` | `add_node(..., defer=True)` waits until the superstep sequence is about to end (fan-in with uneven branch lengths) | RUN p2a, p8 | - |
| Waiting edge | `add_edge(["a","b"], "c")` joins | RUN p16 | - |
| `interrupt(value, *, response_schema=None)` | first call raises `GraphInterrupt`; on resume the node restarts from line 1 and `interrupt` returns the resume value; **each call gets its own id** (1.2.20); `response_schema` = JSON-schema dict (not validated) or Pydantic/TypedDict/dataclass class (surfaced as JSON schema AND used to validate the resume; invalid -> `ValidationError`, nothing saved, can retry) | SRC types.py:870; RUN p2c | UI: breakpoints + Continue are documented (DOCS use-studio); how an `interrupt()` payload / `response_schema` renders in Studio is UNVERIFIED; API: interrupts appear in `values`/`updates`/thread state (RUN c2) |
| Multiple interrupts in one node | resumes are matched by call order; verified sequence `first?` -> `second?` | RUN p2c | API |
| Parallel interrupts (Send/ToolNode) | N interrupts pending at once; resume with `Command(resume={interrupt_id: value, ...})` (keys are 32-hex ids); a bare value with >1 pending raises `RuntimeError` | SRC pregel/_loop.py:1043; RUN p2c, p4B | API |
| Output with interrupts | `invoke(..., version="v2")` returns `GraphOutput(value, interrupts)`; v1 returns dict with `__interrupt__` | SRC types.py:384; RUN p2c | - |
| Strict resume | config key `__pregel_strict_resume=True` -> `UnknownInterruptError(reason=not_pending/empty_map/ambiguous)`; lenient default silently re-interrupts on a stale id. SDK `runs.create/wait/stream(strict_resume=True)` exists, but on the dev server a stale id was NOT rejected in my probe | SRC _loop.py:1030; RUN p11 (local works), c16 (server: no error) | API |
| Static breakpoints | `compile(interrupt_before=[..], interrupt_after=[..])` or per-run `interrupt_before=`/`interrupt_after=`; continue with `input=None` | RUN c13 (server) | UI (Breakpoints > Interrupt, Continue) DOCS use-studio |

### 1.4 Policies

| Feature | Exact API / behaviour | Evidence |
|---|---|---|
| RetryPolicy | `RetryPolicy(initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, max_attempts=3, jitter=True, retry_on=default_retry_on)`. **`default_retry_on` does NOT retry** `ValueError, TypeError, ArithmeticError, ImportError, LookupError, NameError, SyntaxError, RuntimeError, ReferenceError, StopIteration, StopAsyncIteration, OSError`; it retries `ConnectionError`, 5xx `httpx.HTTPStatusError`/`requests.HTTPError`, and any other exception | SRC _internal/_retry.py; RUN p2b (ValueError ran once despite `max_attempts=2`) |
| CachePolicy | `CachePolicy(key_func=default_cache_key, ttl=None)` + `compile(cache=InMemoryCache())`. Default key = hash of the node's whole input, so any upstream field difference (e.g. a log list) is a miss; supply `key_func`. Hits show up as `{"__metadata__": {"<node>": True}}` in `updates` | RUN p2a (miss), p2b (hit with key_func) |
| TimeoutPolicy | `add_node(..., timeout=0.2 \| TimeoutPolicy(run_timeout, idle_timeout, refresh_on="auto"\|"heartbeat"))`, `Send(node, arg, timeout=)`; raises `NodeTimeoutError` (retryable); **async nodes only** (sync cannot be cancelled); `runtime.heartbeat()` refreshes idle timeout | SRC types.py:211-260; RUN p2b, p8 (`slow:NodeTimeoutError`) |
| error_handler | `add_node(name, fn, error_handler=h)` with `def h(state, error: NodeError) -> dict \| Command`; runs AFTER retries are exhausted; returning `Command(update=, goto=)` continues the graph; handler appears as node `__error_handler__<name>` and its result as an `updates` entry; `set_node_defaults(error_handler=)` for all nodes; works for `Send` branches (failed branch recorded, deferred reducer still ran) | SRC errors.py:190, state.py:855; RUN p2b, p8 |
| Node metadata | `add_node(..., metadata={...})` visible in `graph.get_graph().nodes[n].metadata` | RUN p2a |
| TracePolicy | `add_node(..., trace_policy=TracePolicy(process_inputs=, process_outputs=))`, `omit_payload` helper | SRC types.py:283 (not run) |

### 1.5 Persistence, durability, time travel

| Feature | Exact API / behaviour | Evidence | Studio |
|---|---|---|---|
| Durability | `invoke/stream(..., durability="sync"\|"async"\|"exit")`; checkpoints stored for a 3-node run: sync 5, async 5, exit 1; with `exit`, a failing run still persisted its last good state | RUN p2e | - |
| Crash recovery | after a node raises, `invoke(None, cfg)` re-runs ONLY the failed node; completed siblings in the same superstep are not re-run (sync, async and exit identical in-process) | RUN p16 | API |
| Subgraph persistence | `compile(checkpointer=None)` inherit (ns `child:<task_id>`, state per invocation); `True` (ns `child`, history persists across invocations in the thread); `False` (no checkpoints, interrupts still work and the subgraph restarts on resume). `get_state(cfg, subgraphs=True)` exposes `tasks[i].state` for inherit/True | RUN p2d | UI (xray drill-down; DOCS general subgraph page) |
| History | `get_state_history(cfg)`; `StateSnapshot(values, next, config, metadata{step,source}, tasks, interrupts)` | RUN p2e | UI threads/history |
| Replay | `invoke(None, past_config)` re-executes nodes AFTER that checkpoint (b ran again) | RUN p2e | UI "Re-run from here" DOCS |
| Fork | `update_state(past_config, values, as_node="a")` creates a new checkpoint whose `next` is the successor of `as_node`; running from it continues on a new branch; updates go through reducers | RUN p2e, c13 | UI "Edit node state" + Fork DOCS |
| Bulk | `bulk_update_state(cfg, [[StateUpdate(values, as_node), ...]])` | RUN p2e | - |
| Serde | default JsonPlus msgpack; unregistered custom classes warn "will be blocked in a future version"; `LANGGRAPH_STRICT_MSGPACK=true` blocks; server: `langgraph.json` `checkpointer.serde.allowed_msgpack_modules`, `pickle_fallback`, `allowed_json_modules` | RUN p7; SRC langgraph_cli/schemas.py:127 | - |
| Thread TTL | `langgraph.json` `checkpointer.ttl {strategy: delete\|keep_latest, default_ttl (min), sweep_interval_minutes, sweep_limit}` | SRC schemas.py:109 | - |

### 1.6 Streaming (local engine, SDK, server)

| Feature | Exact API / behaviour | Evidence |
|---|---|---|
| Modes (local) | `values, updates, checkpoints, tasks, debug, messages, custom` in `StreamMode`; **`tools`** also works locally (events `tool-started`/`tool-finished` with `tool_call_id`, `tool_name`, `input`/`output`) | SRC types.py:132; RUN p19 |
| Typed parts | `stream(..., version="v2")` yields `{"type","ns","data"}` (+`interrupts` on values); `invoke(version="v2")` -> `GraphOutput` | SRC types.py:274-361; RUN p2c |
| Multi-mode / subgraphs | `stream_mode=[...]`, `subgraphs=True` -> tuples `(ns, mode, data)` (v1) or typed dict with `ns` (v2); ns segment = `node:<task_id>` (persistent subgraph: `node`); depth joined with `\|` | RUN p2d, p20 |
| Custom | `get_stream_writer()(obj)` in nodes/tools, `runtime.stream_writer`, `ToolRuntime.stream_writer`; works in subgraphs (arrives as `custom\|child:<id>` on the server) | RUN p4D, p20, c2 |
| Tokens | `stream_mode="messages"` -> `(AIMessageChunk, metadata{langgraph_node, langgraph_step, ...})` from ANY chat model invoked in a node (sync or async, py3.12); tag a model `with_config(tags=["nostream"])` to hide it | RUN p13 |
| Resume re-emits | custom events written before an `interrupt()` are emitted again on resume (node restarts) | RUN c2 |
| v3 protocol (beta) | `graph.stream_events(input, cfg, version="v3")` -> `GraphRunStream` with live projections `values/messages/lifecycle/subgraphs` (+`updates/custom/checkpoints/debug/tasks` on request); custom `StreamTransformer` subclass via `compile(transformers=[T])`, non-native projections live in `run.extensions["name"]`; projections are single-consumer live cursors (draining `progress` first left `values`/`lifecycle` empty); `messages` yields `ChatModelStream` with `.text` token deltas | SRC langgraph/stream/*; RUN p5 |
| Server stream modes | `POST /threads/{id}/runs/stream` accepts ONLY `values, messages, messages-tuple, tasks, checkpoints, updates, events, debug, custom` (`tools`/`lifecycle` -> HTTP 422) | RUN c2, openapi.json |
| Server event names | SSE `event:` = `mode` or `mode\|<ns>`; `messages-tuple` arrives as event `messages` with `[chunk, metadata]`; legacy `messages` arrives as `messages/metadata`, `messages/partial`, `messages/complete`; `values`/`updates` carry `__interrupt__` | RUN c2, c3, c14 |
| SDK v3 thread stream | `async with client.threads.stream(assistant_id=...) as thread: await thread.run.start(input=...); thread.messages; thread.interrupted; thread.interrupts; await thread.run.respond(...)`; server routes `POST /threads/{id}/stream/events`, `/commands`, WebSocket; message content arrives as content-block lists; `interrupt.value` was `null` in `thread.interrupts` (payload not carried there) | RUN c7; SRC langgraph_api/api/event_streaming.py |

### 1.7 Store (long-term memory)

| Feature | Exact API / behaviour | Evidence | Studio |
|---|---|---|---|
| Basics | `store.put(ns_tuple, key, value, index=None/False/["field"], ttl=)`, `get`, `search(ns_prefix, query=, filter=, limit=)`, `delete`, `list_namespaces(prefix=, max_depth=)`, async twins, `batch` | RUN p3 | UI "Managing long-term memory" listed (DOCS studio); REST `/store/items`, `/store/items/search`, `/store/namespaces` RUN c4 |
| Semantic search | `InMemoryStore(index={"embed": callable, "dims": N, "fields": ["text"]})`; results carry `.score`; filters accept equality and `$gt`-style operators; query+filter returns un-indexed items with `score=None` | RUN p3 | API |
| TTL | **`InMemoryStore.put(ttl=)` raises `NotImplementedError`**. Dev server accepted `put_item(ttl=1)` and `langgraph.json` `store.ttl {default_ttl, refresh_on_read, sweep_interval_minutes}` exists (enforcement not observed) | RUN p3, c4; SRC schemas.py:9 | - |
| Server index | `langgraph.json` `"store": {"index": {"embed": "./path.py:aembed", "dims": 256, "fields": ["text"]}}`; sync or async embed callables both loaded; server-side search scored items | RUN c4 | API |
| Access in code | `runtime.store`, `get_store()`, `store` kwarg, `ToolRuntime.store`, `create_agent(store=)` | RUN p4D, c4 | - |

### 1.8 Functional API

| Feature | Exact API / behaviour | Evidence | Studio |
|---|---|---|---|
| `@entrypoint(checkpointer=, store=, cache=, context_schema=, cache_policy=, retry_policy=, timeout=)` | injectable kwargs `previous`, `runtime`, `writer`, `store`, `config` | SRC func/__init__.py:437; RUN p8 | graph shows ONE node named after the function (`['wf','__start__','__end__']`); input schema is the raw JSON schema of the single parameter (array for `list[int]`), state schema is `__end__` only (RUN c4) |
| `@task(name=, retry_policy=, cache_policy=, timeout=)` | call returns a future; `.result()`; tasks stream as `updates` entries `{task_name: result}`; task cache is global across threads (second thread with same args did not re-run) | RUN p8 | - |
| `entrypoint.final(value=, save=)` | return value differs from what `previous` sees next call | RUN p10 (`{'this_call':5,'total':5}`, then totals 12, 13) | - |
| Mixed usage | entrypoint/task may call a compiled `StateGraph`; interrupts inside bubble out through tasks (two sequential resumes verified). An entrypoint used directly as a `StateGraph` node receives the state dict, not the field: wrap it in a function | RUN p15 | - |
| Interrupts | `interrupt()` works inside entrypoint and tasks; resume with `Command(resume=)` | RUN p8, c4 | API |

### 1.9 `langchain.agents.create_agent` and middleware (langchain 1.4.9)

`create_agent(model, tools=None, *, system_prompt=None, middleware=(), response_format=None, state_schema=None, context_schema=None, checkpointer=None, store=None, interrupt_before=None, interrupt_after=None, debug=False, name=None, cache=None, transformers=None)` returns a `CompiledStateGraph` (SRC langchain/agents/factory.py:892). Nodes: `model`, `tools`, and one node per middleware hook (e.g. `HumanInTheLoopMiddleware.after_model`, `TodoListMiddleware.after_model`; RUN c1). `AIMessage.name` is set to the agent `name`. `create_agent(checkpointer=True|False|None)` passes straight through as the subgraph persistence mode (ns `agent` for `True`, `agent:<task_id>` for `None`/`False`, RUN p22). State = `AgentState`: `messages`, `jump_to`, `structured_response` + keys contributed by middleware (`todos`, `thread_tool_call_count`, `run_tool_call_count`, `hitl_edited_tool_calls`) (RUN c1 state_props). Studio chat mode applies (state has `messages`).

Middleware that REALLY exists (`langchain.agents.middleware.__all__`, SRC) and what I ran:

| Middleware / hook | Notes | Run? |
|---|---|---|
| `HumanInTheLoopMiddleware(interrupt_on={tool: True \| InterruptOnConfig(allowed_decisions=[...])}, description_prefix, edit_notice, interrupt_mode="batched"\|"per_call")` | decisions `approve`/`edit`/`reject`/`respond`; batched = ONE interrupt `{action_requests, review_configs}` and resume `{"decisions":[{"type":"approve"},{"type":"reject","message":"nope"}]}`; per_call = one interrupt per tool call (`type, tool_call_id, name, args`); edit resume `{"type":"edit","edited_action":{"name","args"}}`; edited call result is prefixed with a notice | RUN p4C, c3 |
| `SummarizationMiddleware(model, trigger=("messages",6)\|TriggerClause\|list, keep=("messages",2), token_counter, summary_prompt, summarizer=)` | replaces history with a human message "Here is a summary of the conversation..." + kept tail; needs a model (fake works) | RUN p4F3 |
| `PIIMiddleware(pii_type, strategy="block"\|"redact"\|"mask"\|"hash", detector=, apply_to_input/output/tool_results)` | built-ins `email, credit_card, ip, mac_address, url`; output `[REDACTED_EMAIL]` | RUN p4F4 |
| `ToolCallLimitMiddleware(tool_name=, thread_limit=, run_limit=, exit_behavior="continue"\|...)`, `ModelCallLimitMiddleware(thread_limit, run_limit, exit_behavior="end"\|"error")` | `end` appends an AI message "Model call limits exceeded: run limit (1/1)" | RUN p4F2 |
| `TodoListMiddleware(system_prompt, tool_description)` | adds `write_todos` tool + `todos` state | RUN p4F5 |
| `ModelFallbackMiddleware(first, *more)` | primary raising `ConnectionError` -> fallback answered | RUN p4F1 |
| decorators `@before_agent @before_model @after_model @after_agent @wrap_model_call @wrap_tool_call @dynamic_prompt @hook_config` | observed order per loop: `before_model -> model -> after_model -> (tool wrap) -> before_model -> ...`; `dynamic_prompt` sees `request.runtime.context` | RUN p4E |
| `ModelRetryMiddleware`, `ToolRetryMiddleware(tools=)`, `ToolErrorMiddleware(on_error)`, `ModelRefusalMiddleware`, `ContextEditingMiddleware(edits=[ClearToolUsesEdit])`, `LLMToolSelectorMiddleware(max_tools, always_include)`, `LLMToolEmulator`, `FilesystemFileSearchMiddleware`, `ShellToolMiddleware`, `ProviderToolSearchMiddleware`, `configure_trace_policy` | exist in SRC (constructor signatures read); NOT run | UNVERIFIED runtime |

Tools: `@tool` functions may take `runtime: ToolRuntime[Ctx]` (fields `state, context, store, stream_writer, tool_call_id, config`) -> RUN p4D. A tool may return `Command(update={"messages": [ToolMessage(..., tool_call_id=runtime.tool_call_id)], ...}, goto="refund_gate", graph=Command.PARENT)` as a hand-off: the agent subgraph stops, the parent jumps to `refund_gate`, parent keys (`route`) and the ToolMessage are merged (RUN p21, code `$S/p21_handoff.py`; the `ToolRuntime` parameter supplies `tool_call_id`). `interrupt()` inside a tool works; the tool body re-executes from its top on resume (side-effect counter went 1 -> 2) (RUN p4A). Structured output: `response_format=PydanticModel` auto-selects a tool-call strategy; result in `structured_response` (RUN p1). `ProviderStrategy` needs a model profile: UNVERIFIED.

`langgraph.prebuilt.create_react_agent` is **deprecated since 1.0** in favour of `create_agent` (SRC chat_agent_executor.py:275). Still exported: `ToolNode`, `tools_condition`, `ValidationNode`, `InjectedState`, `InjectedStore`, `ToolRuntime`, `ToolCallTransformer`. `langchain.mcp` (MCPAdapter, elicitation via interrupt) is beta (SRC).

### 1.10 Supervisor / swarm: libs vs hand-rolled

- `langgraph-supervisor 0.0.31` (PyPI; deps `langgraph>=1.0.2,<2`) builds on the **deprecated** `create_react_agent` and imports private `langgraph._internal` (SRC of the downloaded wheel). `langgraph-swarm 0.1.0` depends on `langchain>=1,<2`. `deepagents 0.7.34` pins `langchain>=1.4.9`, `langchain-core>=1.7.6`, `langchain-anthropic>=1.7.10` plus `langchain-google-genai`; its own middleware stack (filesystem, subagents, skills, memory, permissions). None is installed or needed. **Recommendation: hand-roll** - everything they do is `Command(goto=, update=, graph=PARENT)` + `create_agent` subgraph nodes, which I ran (RUN c5: researcher -> writer -> END with namespaces `messages|researcher:<id>`).
- A hand-rolled supervisor loop where the supervisor node returns `Command(goto=worker)` and workers are compiled `create_agent` graphs added with `add_node("researcher", agent)` works; shared `messages` key flows in/out, agent-private keys do not leak (RUN c5).

### 1.11 Graph-to-graph calls (what works inside `langgraph dev`)

| Pattern | Works? | Evidence |
|---|---|---|
| A. compiled graph as node (`add_node("child", child_graph)`) | Yes; shared keys flow; `Command.PARENT` exits to parent; interrupts bubble; xray shows children | RUN p2d, c2 |
| B. wrapper node that maps state and calls `child.invoke(...)` inside the node | Yes; child is statically discovered (`get_graph(xray=True)` lists `call_inner:model`), `get_state(subgraphs=True)` shows its state, interrupts bubble | RUN p9(2) |
| C. agent invoked inside a TOOL | Runs and streams (ns `tools:<id>`), interrupts bubble, but `get_state(subgraphs=True)` shows NO sub-state and xray cannot see it | RUN p9(1) |
| D. SDK in-process: `get_client(url=None)` + `client.runs.wait/create/stream(thread_id, graph_id, ...)` inside a node | Yes **only if** the server runs more than one job at a time. `langgraph dev` defaults to 1 concurrent job -> deadlock; start with `--n-jobs-per-worker 6` | SRC langgraph_api/cli.py:266; RUN c2 (hang) -> works with flag |
| E. `RemoteGraph("id", client=get_client(url=None))` called explicitly from a node WITHOUT thread config | Yes (stateless run) | RUN c2 (`remote:{...}`) |
| F. `add_node("x", RemoteGraph(...))` against the SAME server | **Deadlocks**: RemoteGraph forwards the parent's `thread_id`; the nested run queues behind the parent on the same thread (`multitask_strategy=enqueue`): runs list showed parent `running` + callee `pending` | SRC pregel/remote.py:804; RUN c5 (hang), c15 (confirmed) |
| G. SDK delegation with derived child thread + interrupt bubbling | Yes: `child_tid = uuid5(parent_thread/"callee")`; `threads.create(thread_id=, if_exists="do_nothing")`; check `threads.get_state(child)` first (idempotent restart), `runs.wait`; if `__interrupt__` -> parent calls `interrupt(child_payload)`, then `runs.wait(child_tid, command={"resume": answer})`. Child thread is a separate row in Studio/threads | RUN c6 (code `$S/devproj/graphs2.py: delegate`) |
| H. Event ingest from a custom FastAPI route using the in-process client | Yes (`/events` -> `runs.create`) | RUN c11 |

### 1.12 Agent Server / `langgraph.json` features relevant here

- `langgraph.json` keys (SRC schemas.py:636): `dependencies, graphs{id: path \| {path, description}}, env, store{index, ttl}, checkpointer{path, ttl, serde}, auth, encryption, priority, http{app, cors, configurable_headers, mount_prefix, disable_*, enable_custom_route_auth, middleware_order}, webhooks{env_prefix, url{require_https, allowed_domains, allowed_ports, max_url_length, disable_loopback}, headers}, ui, python_version, api_version, ...`. A graph entry may be a compiled graph, an `@entrypoint`, or a (async) context-manager/factory taking `RunnableConfig` (RUN c4: `factory` graph read `configurable.mode`).
- Custom routes: `http.app` FastAPI app is mounted next to `/assistants /threads /runs /store`; `GET /dash/ping` and `POST /events` worked (RUN c1, c11). Routes are unauthenticated unless `enable_custom_route_auth`.
- Every graph is auto-exposed as an MCP tool at `/mcp/` (initialize, `tools/list` listed all 16 probe graphs, `tools/call echo` returned the result) and `/a2a/{assistant_id}` exists (RUN c10; A2A not run).
- Assistants = (graph, config/context, name, metadata) presets with versions: `assistants.create("g", context={...}, name=, if_exists="do_nothing")`, `update` -> version 2, `get_versions` (RUN c8). DOCS: Studio "Manage Assistants" modal, Active toggle.
- Threads: `threads.search(status="interrupted", metadata={"kind":"approval"})` returns exactly the interrupted threads; thread object has `interrupts` map `{task_id: [{id, value}]}`, `status`, `metadata`; run metadata merges thread metadata + `graph_id`, `assistant_id`; `threads.count(status=)` (RUN c17). `threads.copy`, `get_history`, `update_state(checkpoint=, as_node=)` (RUN c13).
- Crons: `crons.create("echo", schedule="* * * * *", input=)` / `delete` (RUN c8).
- Webhooks: `runs.create(webhook=URL)` posts the run object on completion; **loopback/relative URLs are blocked by default** ("Webhook host blocked: loopback IP range" / "Loopback webhooks ... disabled by default"); enable internal ones with `"webhooks": {"url": {"disable_loopback": false}}` (RUN c11).
- Run options seen in SDK signatures (SRC): `multitask_strategy reject\|interrupt\|rollback\|enqueue`, `on_disconnect cancel\|continue`, `on_completion delete\|keep`, `if_not_exists create\|reject`, `after_seconds`, `stream_resumable`, `durability`, `strict_resume`, `langsmith_tracing`.
- Dev server flags: `--n-jobs-per-worker`, `--allow-blocking`, `--no-reload`, `--tunnel`, `--studio-url`, `--debug-port`. Studio URL printed as `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:<port>` (SRC langgraph_api/cli.py:306).

### 1.13 What is new in 1.1 / 1.2 worth showing (all present in the install)

`TimeoutPolicy` + per-`Send` timeout (RUN p8); `error_handler` + `set_node_defaults` (RUN p2b); `version="v2"` typed stream parts + `GraphOutput` (RUN p2c); `response_schema` on `interrupt` (RUN p2c); per-call interrupt ids and `strict resume` (1.2.20, DOCS releases); `DeltaChannel` (1.2.18 snapshot default 100); cooperative `RunControl` drain (SRC); v3 streaming + `StreamTransformer` (RUN p5); `trace_policy`/`TracePolicy` (SRC); `stream_mode="tools"` (RUN p19); SDK 0.4.14 `tools`/`lifecycle` stream modes (only on the v3 thread protocol, RUN c2 422) and `threads.stream` (RUN c7); `Interrupt.response_schema` field. LangChain 1.x: `ModelRefusalMiddleware`, `ToolErrorMiddleware`, `ProviderToolSearchMiddleware`, `ContextEditingMiddleware`, HITL `respond` decision and `per_call` mode (SRC).

### 1.14 Studio-visible vs code-only (summary)

- **Visible/interactive (DOCS or API-verified)**: graph structure incl. `destinations`, node metadata; xray subgraph expansion (only for discoverable subgraphs); input form from `input_schema`; assistants/context presets; threads + step history; interrupts, static breakpoints, Continue; edit node state + Fork; Re-run from checkpoint; stream on/off; chat mode (needs `messages`); store/long-term memory; traces (needs LangSmith key); datasets/experiments + prompt iteration (LangSmith cloud only, cannot work offline).
- **Code-only (Studio cannot surface)**: reducers/Overwrite/DeltaChannel internals, retry/cache/timeout/error-handler *configuration* (only effects are visible as repeated steps or `__error_handler__` nodes), durability mode, `RemainingSteps`, middleware internals (only as extra nodes), functional-API tasks (one node), RemoteGraph/SDK delegation (child threads appear as separate threads), v3 projections, custom stream payload shapes (visible as raw custom events at best).

---

## 2. Proposed catalog: "Meridian Ops Desk" - 9 graphs, one deployment

**Story.** Meridian is a small SaaS company's AI-assisted operations desk. Everything that happens (a support ticket, an invoice batch, an alert, a document upload, a finished conversation, an eval request) enters as an **EventEnvelope** at one door (`event_router`). The router de-duplicates, classifies and dispatches to the handler graph that owns that kind of work. Handlers stream progress, ask humans through one shared **approval gate**, and leave behind memories and lessons (`memory_reflection`) that the next run reads. `eval_judge` scores any graph against golden datasets so quality regressions are visible. Fixtures (offline): `data/tickets.jsonl, kb/*.md, ledger/2026-10.json, alerts.jsonl, documents/*.md, eval/*.jsonl`.

```
                        POST /events  or  Studio / dashboard run
                                     |
                              +--------------+      thread_id = uuid5(graph/tenant/key)
                              | event_router |------------------------------------------+
                              +--------------+                                          |
   ticket.* ---> support_supervisor ---(refund)--> approval_gate <--- period_close(adjustments), incident_runbook(sev1), research_agent(HITL tools)
   chat.message > research_agent                      ^ one inbox: threads.search(status="interrupted", metadata.kind="approval")
   document.batch > batch_digest
   invoice.batch_ready > period_close
   alert.raised > incident_runbook
   conversation.ended / feedback.* > memory_reflection ---writes--> Store ("memories"/"lessons") ---read by--> support_supervisor, research_agent
   eval.requested > eval_judge ---SDK runs---> any graph above ---> report + baselines in Store
```

Graph ids (snake_case, = keys in `langgraph.json`): `event_router, support_supervisor, research_agent, batch_digest, approval_gate, period_close, incident_runbook, memory_reflection, eval_judge`.

Conventions for every graph module: `build_graph(checkpointer=None, store=None, cache=None, **deps)` returns the compiled graph; module-level `graph = build_graph()` (NO checkpointer, see Gotcha 1); no I/O or model construction at import; `input_schema`/`output_schema` on every StateGraph; `name=` set to the graph id; outputs end in a `result: GraphResult` (4.2).

---

### 2.1 `event_router` - the single ingress and graph-to-graph hub

**Purpose.** Validate and de-duplicate an event, pick the handler(s) (rules first, LLM fallback only for unknown kinds), dispatch to other graphs (async fire-and-forget by default; `await` mode runs the child and bubbles its interrupts), audit everything.

**Topology**
```
START -> validate --bad--> reject ----------------------------------> finish -> END
            | ok                                                         ^
            v                                                            |
          dedupe --duplicate------------------------------------------->|
            | new                                                        |
            v                                                            |
      classify_rules --unknown--> classify_llm --+                       |
            | known                              |                       |
            v                                    v                       |
          plan_dispatch  (Command goto=[Send("dispatch_one", {...}) ...])|
            v  (fan-out, 1..N)                                           |
        dispatch_one (async; SDK runs.create | wait+interrupt bubbling) -+--> join (defer=True) -> finish
```

**Schemas**
```python
class RouterIn(TypedDict):  event: EventEnvelope            # input_schema (Pydantic model -> rich form in Studio)
class RouterCtx:  # dataclass, context_schema
    tenant: str = "acme"; dispatch_mode: Literal["async","await"] = "async"
    use_llm_fallback: bool = True; sample_eval_pct: int = 10; model: str = "fake"
class RouterState(TypedDict, total=False):
    event: dict                                   # EventEnvelope.model_dump(mode="json")
    duplicate: bool
    route: RouteDecision                          # {handlers: list[str], via: "rules"|"llm", reason: str, confidence: float}
    dispatches: Annotated[list[Dispatch], operator.add]   # {graph_id, thread_id, run_id|None, status, error|None}
    audit: Annotated[list[dict], DeltaChannel(append_batches)]   # append-only journal (beta channel)
    result: GraphResult
class RouterOut(TypedDict):  result: GraphResult; route: RouteDecision; dispatches: list[Dispatch]
```
**Routing table (rules)**: `ticket.created|updated` -> `support_supervisor`; `chat.message` -> `research_agent`; `document.batch` -> `batch_digest`; `invoice.batch_ready` -> `period_close`; `alert.raised` -> `incident_runbook`; `approval.requested` -> `approval_gate`; `conversation.ended|feedback.recorded` -> `memory_reflection` (+ `eval_judge` when `sha1(event.id) % 100 < sample_eval_pct`); `eval.requested` -> `eval_judge`; else LLM fallback (`with_structured_output(RouteDecision)`), else `reject`.

**Nodes/edges.** `validate` (Pydantic; on error `Command(goto="reject")`), `dedupe` (Store ledger `("events","seen",tenant)` key = `event.id`; first writer wins; same-event-same-thread serialisation makes it race-free), `classify_rules` (pure table), `classify_llm` (model tagged `nostream`), `plan_dispatch` (returns `Command(update=..., goto=[Send(...)])`), `dispatch_one` (async; `RetryPolicy(retry_on=(ConnectionError, TimeoutError, httpx.TransportError))`, `timeout=30`, `error_handler` records `status="failed"` instead of failing the router), `join` (`defer=True`), `finish`.

**Tools:** none. **Interrupts:** only in `await` mode: pattern G of 1.11 (`interrupt(child_payload)` then resume child).
**Memory:** `("events","seen",tenant)`, `("router","stats",tenant)`; audit journal in state (DeltaChannel).
**Streaming:** custom `decision` (`{handlers, via}`), `handoff` (`{graph, thread_id}`) per dispatch; `updates` for the dashboard timeline; no token stream (LLM fallback hidden).
**Policies:** `set_node_defaults(retry_policy=..., timeout=30, error_handler=router_failure)`; node `metadata={"stage": "ingress"}`; recommended run durability `sync`.
**LangGraph features owned:** input/output/context schemas, Runtime (`context`, `store`, `execution_info`, `server_info`), `Command(goto/update)` with `destinations`, `Send`, `defer`, `error_handler`, `set_node_defaults`, DeltaChannel, SDK in-process graph-to-graph (pattern D/G), thread metadata, webhooks on completion, custom `http.app` `/events`.
**Studio:** destinations edges; input form from the envelope; parallel Send tasks; Store tab (ledger); assistants as tenant presets.
**Tests (offline).** Inject a `Dispatcher` protocol (`build_graph(dispatcher=InMemoryDispatcher())`) that records `(graph_id, thread_id, input)`; scripted fake model for the fallback; assert idempotency (second envelope with same id -> `duplicate`, zero dispatches), fan-out count, error-handler path (dispatcher raises -> status failed, router completes), thread id determinism. Integration: boot `langgraph dev --n-jobs-per-worker 8` on a test port and POST `/events`.

---

### 2.2 `support_supervisor` - hand-rolled multi-agent supervisor

**Purpose.** Resolve a support ticket: research (KB + order data), check policy, draft a reply, and escalate refunds through the approval gate. Shows the three subgraph persistence modes, handoff tools and a code-enforced supervisor.

**Topology**
```
START -> load_context ----> supervisor <----------------------------------------------+
            (memories,        |  Command(goto=...)  (structured Route; code guards)   |
             lessons)         +--> researcher   (create_agent subgraph, checkpointer=True)  --+
                              +--> policy       (create_agent subgraph, response_format=PolicyVerdict, checkpointer=False) --+
                              +--> drafter      (plain node, LLM token stream) --+
                              +--> refund_gate  (wrapper node -> approval_gate.invoke(); interrupts bubble) --+
                              +--> FINISH -> finalize -> END
 researcher tool `escalate_to_human` returns Command(graph=Command.PARENT, goto="refund_gate", update=...)
```
**Schemas**
```python
class SupIn(TypedDict): ticket: Ticket                        # {id, customer_id, subject, body, plan, order_id|None, priority}
class SupState(MessagesState):                                # messages: add_messages
    ticket: Ticket
    next: str; steps_taken: int; remaining_steps: RemainingSteps
    findings: Annotated[list[Finding], operator.add]          # {source, text, confidence}
    policy: PolicyVerdict | None                              # {allowed: bool, rule_ids: list[str], max_refund: float, reasons: list[str]}
    refund: RefundRequest | None                              # {amount, currency, order_id, reason}
    draft: str | None
    result: GraphResult
class SupCtx:  tenant: str = "acme"; max_steps: int = 8; model: str = "fake"
```
**Supervisor logic (model vs code).** The LLM returns `Route(next: Literal["researcher","policy","drafter","refund_gate","FINISH"], reason)` via `with_structured_output`; **code overrides** it: policy must run before drafter, refund_gate only when `policy.allowed and refund`, hard stop when `steps_taken >= max_steps` or `remaining_steps <= 3` (managed value, RUN p18). All routing is `Command(goto=..., update={"next":..., "steps_taken": n+1})` with `add_node("supervisor", supervisor, destinations=(...))` (edges drawn, RUN p14).
**Tools.** researcher: `search_kb(query)` (Store semantic search), `lookup_order(order_id)`, `lookup_customer(customer_id)`, `escalate_to_human(reason)` (handoff `Command(graph=PARENT)`); policy agent: `get_policy(rule_id)`.
**Interrupts.** From `approval_gate` (bubbled). Optional `interrupt_before=["drafter"]` breakpoint for Studio demos.
**Memory.** Reads `("memories", tenant, customer_id, "semantic")` and `("lessons", tenant, "support_supervisor")` in `load_context` into the system prompt; writes nothing (that is `memory_reflection`).
**Streaming.** `messages-tuple` tokens from `drafter` (and nested agent tokens under `messages|researcher:<id>`), `updates` per node, custom `handoff` events `{from,to,reason}` emitted by the supervisor, `values` for the ticket card. Dashboard must subscribe with `stream_subgraphs=True`.
**Policies.** `RetryPolicy` on LLM nodes (custom `retry_on` for rate limits), `cache_policy` on `lookup_*` wrapped tool nodes (key_func includes tenant), agents carry `ModelCallLimitMiddleware(run_limit=6)`.
**Features owned:** `Command(goto)` supervisor with `destinations`, handoff tool + `Command.PARENT`, subgraph-as-node with all three `checkpointer` modes (researcher `True` accumulates across supervisor loops; policy `False` stateless; approval via inherit wrapper), wrapper-node state mapping (pattern B), `RemainingSteps`, message utilities, assistant presets (tenant/model), token streaming from nested graphs, structured output routing.
**Studio:** chat mode (MessagesState), xray drill-down into `researcher`/`policy`, state edit + fork (e.g. flip `policy.allowed` and re-run `drafter`), breakpoints, stream toggle.
**Tests.** `ScriptedChatModel(responder=...)` for the routing model - pure function of the transcript (RUN p17) - and scripted agents; assert route order, policy-before-draft invariant, cap fires at `max_steps`, handoff reaches `refund_gate`, subgraph state visible via `get_state(subgraphs=True)`.

---

### 2.3 `research_agent` - `create_agent` ReAct agent with a real middleware stack

**Purpose.** Chat-style analyst over KB/docs/ticket history with write actions that need approval; the reference for middleware, `ToolRuntime`, structured output and Studio chat mode. Reused (configured) by `support_supervisor.researcher`.

**Topology** (generated by `create_agent`): `START -> model <-> tools -> END` plus one node per middleware hook (`HumanInTheLoopMiddleware.after_model`, `TodoListMiddleware.after_model`, ...).
**Config**
```python
create_agent(
  model, name="research_agent", context_schema=ResearchCtx,        # {tenant, user_id, model, max_tool_calls}
  state_schema=ResearchState,                                         # AgentState + citations: Annotated[list[Citation], add]
  response_format=ResearchReport,                                     # {answer, citations, confidence}
  tools=[search_kb, search_docs, ticket_history, write_note, send_email, calculator],
  middleware=[
    PIIMiddleware("email", strategy="redact"),                        # input redaction before the model sees it
    MemoryInjection(),                                                # custom @before_agent: Store -> system prompt
    SummarizationMiddleware(model=summ_model, trigger=("tokens", 3000), keep=("messages", 8)),
    TodoListMiddleware(),
    ToolCallLimitMiddleware(run_limit=8), ModelCallLimitMiddleware(run_limit=10, exit_behavior="end"),
    ModelRetryMiddleware(), ToolRetryMiddleware(tools=["search_docs"]), ModelFallbackMiddleware(backup_model),
    AuditTool(),                                                      # custom @wrap_tool_call: emit("log", ...) + timing
    HumanInTheLoopMiddleware(interrupt_on={"send_email": {"allowed_decisions": ["approve","edit","reject"]},
                                           "write_note": True}, interrupt_mode="batched"),
  ])
```
(Order matters; run order of hooks verified in RUN p4E. Put HITL last so edits are applied to the final tool calls.)
**Tools** use `ToolRuntime[ResearchCtx]` for store access, `stream_writer` progress and `state` (RUN p4D). One tool (`wire_funds`-style demo `request_export`) calls `interrupt()` itself to show in-tool interrupts and the re-execution caveat (RUN p4A).
**Interrupts.** HITL batched decisions (approve/edit/reject/respond), parallel in-tool interrupts resumed by id map (RUN p4B).
**Memory.** `("memories", tenant, user_id, "semantic")` read at start (custom middleware); `ticket_history` reads the shared ledger.
**Streaming.** `messages-tuple` tokens, `custom` tool progress `{kind:"progress", key:"search_docs:0"}`, `updates` per hook node; (`stream_mode="tools"` locally only).
**Policies.** per-middleware limits above; `RetryPolicy` on `model` is done by `ModelRetryMiddleware` instead of node policy.
**Features owned:** `create_agent`, every middleware above, `ToolRuntime`, structured output (`response_format`), HITL `per_call` vs `batched`, tool-internal interrupt, `context_schema` + `dynamic_prompt`.
**Studio:** chat mode with "Show tool calls", interrupt/resume, edit `todos` in state.
**Tests.** `ScriptedChatModel(script=[...])` (RUN p1/p4); one test per middleware (PII output string, summarization trigger, limits message, fallback, HITL decisions, hook order trace), tool unit tests with a stub `ToolRuntime`.

---

### 2.4 `batch_digest` - parallel map-reduce (Send) with reducers

**Purpose.** Summarise N documents in parallel, merge, then compose a cited digest; resilient to bad documents.

**Topology**
```
START -> plan --Send x N--> summarize_doc (async, timeout, retry, cache, error_handler)
                                   |  results: summaries (+) / failures (+) / stats (merge)
                                   v
                          reduce_level (defer=True) --(>K summaries)--> Send x M --> combine_group --+
                                   |  (loop, level counter, RemainingSteps guard)  <---------------+
                                   v
                              compose (LLM, token stream) -> quality_gate --retry once--> compose
                                                                   | ok
                                                                   v
                                                               finish -> END
```
**Schemas**
```python
class DigestIn(TypedDict): documents: list[Doc]; focus: str; max_group: int   # Doc {id, title, text}
class DigestState(TypedDict, total=False):
    documents: list[Doc]; focus: str; level: int
    summaries: Annotated[list[DocSummary], keep_best_by_doc_id]   # custom reducer: idempotent under retries/forks
    failures:  Annotated[list[Failure], operator.add]
    stats:     Annotated[dict[str,int], merge_counts]             # custom dict-merge reducer
    digest: str; result: GraphResult
```
**Nodes/policies.** `fan_out = [Send("summarize_doc", {"doc": d, "focus": f}, timeout=20) for d in docs]`; `summarize_doc` async with `RetryPolicy(max_attempts=3, retry_on=(ConnectionError, TimeoutError))`, `CachePolicy(key_func=lambda s: f"{tenant}:{sha1(doc.text)}:{focus}", ttl=3600)` (key includes tenant, see Gotcha 20), `error_handler` appends a `Failure` and returns normally so the deferred `reduce_level` still runs (RUN p8 produced `3 ok / 2 failed`). Optional `compile(transformers=[ProgressTransformer])` for the v3 protocol (UNVERIFIED on the server).
**Interrupts:** none (deliberately - pure parallelism showcase). **Memory:** reads `("lessons", tenant, "batch_digest")` for style rules.
**Streaming.** `custom` `progress {done,total,doc_id}` from each branch (interleaved by design), `messages-tuple` only from `compose` (per-branch LLM tagged `nostream`), `updates` shows cache hits via `__metadata__`.
**Features owned:** `Send` + per-Send timeout, custom reducers, `Overwrite` to reset between reduce levels, `defer`, branch `error_handler`, CachePolicy with `key_func`, TimeoutPolicy, RetryPolicy, map-reduce-reduce loop, v3 transformer (optional).
**Studio:** parallel tasks per step, cached-node markers, fork from one `summarize_doc` input.
**Tests.** responder-keyed fake model (doc id -> summary), async sleep doc for timeout, reducer property tests (idempotent, commutative), assert failure count and that `reduce_level` ran.

---

### 2.5 `approval_gate` - one human control point with four interrupt styles

**Purpose.** Every consequential action (refund, deploy, data export, discount) gets reviewed here. Styles: **approve**, **edit**, **reject**, **question** (reviewer asks, graph answers, reviewer decides again), plus **four-eyes** (two reviewers in parallel).

**Topology**
```
START -> assess (deterministic risk tier) --low--> auto_approve ----------------------+
              | medium                                                               |
              v                                                                      v
          review  (interrupt #1 action; if edit -> interrupt #2 confirm diff)--approve/edit--> apply -> notify -> END
              | question                    | reject                                  ^
              v                             v                                         |
        answer_question -> review        record_reject -> notify                      |
              | high                                                                  |
              +--> Send x2 -> reviewer(role=finance|security) (parallel interrupts) -> collect(defer) -- both approve --+
```
**Schemas**
```python
class ApprovalRequest(BaseModel):          # input_schema (Pydantic at the edge)
    id: str; kind: Literal["refund","deploy","data_export","discount"]; amount: float | None = None; currency: str = "USD"
    requester: str; summary: str; payload: dict[str, Any] = {}; policy_refs: list[str] = []; correlation_id: str | None = None
class ReviewDecision(BaseModel):           # interrupt response_schema
    action: Literal["approve","edit","reject","question"]; note: str = ""; edits: dict[str, Any] | None = None; question: str | None = None
class ApprovalState(TypedDict, total=False):
    request: dict; risk: Risk   # {score:int, tier:"low"|"medium"|"high", factors:list[str]}
    decisions: Annotated[list[Decision], operator.add]; qa: Annotated[list[QA], operator.add]
    status: Literal["pending","auto_approved","approved","edited","rejected"]; final_payload: dict; result: GraphResult
```
**Interrupt payload (what the dashboard renders):** `{"type":"review","request":{...},"risk":{...},"options":["approve","edit","reject","question"],"qa":[...]}` with `response_schema=ReviewDecision`; high tier: two interrupts at once, resume `{id_finance: {...}, id_security: {...}}` (RUN p2c).
**Rules enforced in code:** `apply` runs only after approval and writes an effect record `("effects", tenant)` keyed by `request.id` first (idempotent); loop cap 3 questions; `apply` has a compile-time `interrupt_before` variant (`build_graph(breakpoint=True)`) for Studio breakpoint demos.
**Memory:** `("effects", tenant)` (applied actions), `("approvals","audit",tenant)` (decision log incl. reviewer notes).
**Streaming:** `custom` `decision` events; `values` carries `status`; tokens only from `answer_question`.
**Dashboard inbox:** `threads.search(status="interrupted", metadata={"kind":"approval","tenant":t})`; thread metadata set at creation by whoever creates the thread (router/handlers) (RUN c17). Resume with `Command(resume={interrupt_id: decision})`; always pass the id map, never a bare value (Gotcha 15).
**Features owned:** `interrupt` single / multiple-in-node / parallel with id map, `response_schema` validation (invalid resume retried, RUN p2c), `Command(update, goto)`, `Send`, static breakpoints, update_state/fork (re-decide), Pydantic at the edge with TypedDict inside, thread-metadata inbox.
**Studio:** interrupt payload visible in the thread; Continue; edit node state `review` and Fork to re-decide; breakpoints before `apply`.
**Tests.** `run_graph(graph, input, resumes=[...])` with `InMemorySaver`; one test per style; invalid resume then valid; assert `apply` not executed before approval and executed once on replay; four-eyes needs both.

---

### 2.6 `period_close` - deterministic, durable, no LLM

**Purpose.** Month-end ledger close: load -> validate -> reconcile every account in parallel -> variance check -> propose adjustments -> (breakpoint/approval) -> post -> report. 100% deterministic so the *execution model* is the star: durability, crash recovery, replay, fork, edit state.

**Topology**
```
START -> load_ledger -> validate --invalid--> quarantine -> report
                           | ok
                           v
                 Send x accounts -> reconcile_account (pure) -> collect (defer)
                                                                  v
                                                           variance_check --within tolerance--> post_entries
                                                                  | exceeded                        ^
                                                                  v                                 |
                                                         propose_adjustments --(interrupt_before post_entries)--+
                                                                                                   v
                                                                                                report -> END
```
**Schemas**
```python
class CloseIn(TypedDict): period: str; ledger_id: str; tolerance: float; fail_at: str | None; rates_override: dict[str,float] | None
class CloseState(TypedDict, total=False):
    rows: list[LedgerRow]; recon: Annotated[dict[str, AccountRecon], merge_dict]
    journal: Annotated[list[JournalEntry], DeltaChannel(append_batches)]     # beta; every step appends
    adjustments: list[Adjustment]; posted: list[str]; report: CloseReport; fault_fired: bool; result: GraphResult
```
**Policies.** `set_node_defaults(retry_policy=RetryPolicy(max_attempts=4, retry_on=TransientDbError), timeout=TimeoutPolicy(run_timeout=30))` (note default `retry_on` would NOT retry `RuntimeError`/`ValueError`, Gotcha 11); `load_ledger` `CachePolicy(key_func=ledger fingerprint)`; `error_handler` on `validate` -> `Command(goto="quarantine")`. `fail_at="post_entries"` raises once (guarded by `fault_fired` persisted in state) to demonstrate recovery: `invoke(None, cfg)` re-runs only the failed node (RUN p16). Run with `durability="sync"`; `dry_run` variants use `exit`.
**Streaming.** `custom` progress per account; **this is the graph for `tasks` and `checkpoints` stream modes** (timeline of task start/finish with results, checkpoint ids for fork buttons) - RUN c2 shows both modes on the server.
**Features owned:** durability modes, crash recovery, `get_state_history`, replay, fork with `update_state(as_node=)`, `bulk_update_state`, static breakpoints, DeltaChannel journal, RetryPolicy/TimeoutPolicy defaults, `tasks`/`checkpoints`/`debug` modes, deterministic testing.
**Studio:** history slider, "Edit node state" on `reconcile_account` with new `rates_override` then Fork, breakpoint at `post_entries`, Re-run from here.
**Tests.** No model. Property tests on reconciliation; crash-injection test (`fail_at`) asserting exactly one re-run of the failed node and unchanged `recon`; fork test asserting new branch differs; `exit` vs `sync` checkpoint counts.

---

### 2.7 `incident_runbook` - Functional API workflow

**Purpose.** Alert triage runbook written as plain Python with `@entrypoint`/`@task`: parallel evidence collection, correlation, severity, human-approved remediation, flapping memory across invocations.

```python
@entrypoint(checkpointer=None, context_schema=OpsCtx)          # compiled w/o checkpointer; server injects
def incident_runbook(inp: RunbookIn, *, previous: Memo | None, runtime: Runtime[OpsCtx]) -> RunbookOut:
    m, l, d = fetch_metrics(inp), fetch_logs(inp), fetch_deploys(inp)          # @task, parallel futures, cache_policy ttl, retry_policy
    evidence = correlate(m.result(), l.result(), d.result()).result()          # deterministic scoring
    sev = classify_severity(evidence).result()                                 # @task, optional LLM (structured output)
    plan = propose_remediation(sev, evidence).result()
    decision = (approval_gate.invoke({...}) if sev >= 1                        # call a compiled StateGraph from the entrypoint (RUN p15)
                else interrupt({"type": "approve_remediation", "plan": plan}, response_schema=Decision))
    out = execute_remediation(plan, idempotency_key=inp.alert_id).result() if approved(decision) else skipped()
    memo = bump_occurrences(previous, inp.fingerprint)                         # flapping detector via `previous`
    return entrypoint.final(value=RunbookOut(...), save=memo)
```
**Schemas.** `RunbookIn` (single TypedDict param so Studio's input form is a dict, not a bare array, RUN c4), `OpsCtx{tenant, region, model}`, `Memo{fingerprint, occurrences, last_seen, last_sev}`.
**Interrupts.** In-entrypoint `interrupt` (response_schema) and bubbled `approval_gate` interrupt for sev1 (RUN p15). **Memory:** `previous` (per thread; thread id = alert fingerprint so flapping accumulates) + Store `("incidents", tenant)` timeline.
**Streaming.** `custom` stage events via `get_stream_writer()`; **tasks stream as `updates` entries `{task_name: result}`** (RUN p8) so the dashboard timeline works without nodes; `values` only at the end.
**Features owned:** `@entrypoint`, `@task`, futures/parallelism, `previous` + `entrypoint.final`, task cache/retry, Functional->Graph composition, `context_schema`. **Studio:** shows one node (RUN c4) - honest limitation; value is the dashboard task timeline.
**Tests.** tasks are plain functions - patch with deterministic fixtures from `alerts.jsonl`; drive the entrypoint with `InMemorySaver`; test flapping via two invocations on one thread; interrupt resume paths.

---

### 2.8 `memory_reflection` - writes long-term memory and lessons

**Purpose.** Turn conversations/feedback/eval failures into durable memory: semantic facts, episodic summaries, procedural "lessons" that other graphs load. Also consolidates nightly via cron.

**Topology**
```
START -> load_source (event payload | SDK threads.get_state of a referenced thread)
      -> recall_existing (Store semantic search per candidate)
      -> extract (LLM structured: list[MemoryOp]) -> resolve (deterministic merge/dedupe/conflict)
      -> needs_review? --yes--> human_check (interrupt: approve memory ops) --+
              | no                                                           |
              v                                                              v
            write (put/delete) -> reflect (lessons from low ratings/eval failures) -> finish -> END
```
**Schemas.** `MemIn{tenant, user_id, mode: "extract"|"consolidate"|"reflect", thread_ref: {graph_id, thread_id}|None, transcript: list[Msg]|None, feedback: Feedback|None}`; `MemoryOp{op: "ADD"|"UPDATE"|"DELETE"|"NOOP", kind: "semantic"|"episodic"|"procedural", text, confidence, evidence}`; state keeps `candidates`, `ops` (reducer), `applied`, `pending_review`.
**Store namespaces (tuples, tenant first).** `("memories", tenant, user_id, "semantic")`, `(..., "episodic")` (TTL), `("lessons", tenant, agent_id)`, `("memory_ops", tenant)` (audit; `index=False`). Items `{"text": ..., "kind":..., "confidence":..., "version": n, "source": {...}}` so the configured index `fields=["text"]` embeds them. Dedupe rule: `search(..., limit=3)` best `score >= 0.85` -> UPDATE (bump `version`) else ADD (RUN p3 shows `.score` and filters).
**Interrupts.** `human_check` only for low-confidence or PII-flagged memories (approve/skip per op).
**Streaming.** `custom` one event per applied op; extraction LLM tagged `nostream`.
**Server config.** `langgraph.json` `store.index {embed, dims, fields:["text"]}` and `store.ttl {default_ttl: null, refresh_on_read: true, sweep_interval_minutes: 60}`; nightly `crons.create("memory_reflection", schedule="0 3 * * *", input={"mode":"consolidate", ...})` (cron create/delete RUN c8). Note `InMemoryStore` has no TTL (Gotcha 22) - tests assert TTL via a stub.
**Features owned:** Store semantic search/filter/delete/namespaces/ttl, `Runtime.store`, cron, SDK cross-thread read, structured output, human-in-the-loop on memory writes, `Overwrite` for compaction. **Studio:** Store browser (docs list "Managing long-term memory"), reflect step state edit.
**Tests.** `InMemoryStore(index={"embed": lexical_fake, "dims": 256, "fields": ["text"]})` (RUN p3); scripted extraction; assert dedupe threshold, tenant/user isolation, DELETE, review gate.

---

### 2.9 `eval_judge` - scores other graphs against golden datasets

**Purpose.** Continuous evaluation: run a target graph over a dataset (via SDK), score with deterministic metrics and an LLM judge, compare to a stored baseline, gate on regressions.

**Topology**
```
START -> load_dataset -> plan --Send x N--> run_example (async SDK run on its own thread) -> score_rules -> score_llm
                                                                                                  |
                                              aggregate (defer=True) <----------------------------+
                                                   v
                                         regression_gate --regressed--> interrupt "accept new baseline?" (approve/reject)
                                                   | ok / approved                                  |
                                                   v                                                v
                                              save_report (Store + state) -> END <------------------+
```
**Schemas.** `EvalIn{suite, target_graph_id, dataset: str, judge: "rules"|"llm"|"both", max_examples: int, concurrency: int = 3, auto_resume: dict|None, baseline: str|None, thresholds: dict[str,float]}`; dataset JSONL row `{id, inputs, reference, tags, rubric?, auto_resume?}`; state `examples`, `runs: Annotated[list[RunRecord], add]` (`{example_id, thread_id, status, output, trajectory: list[str], latency_ms}`), `scores: Annotated[list[Score], add]`, `summary`, `regressions`, `result`.
**Scoring.** `score_rules`: exact/contains/F1, route-match (`reference.route` vs observed supervisor route), trajectory subsequence (nodes seen via `stream_mode=["updates"], stream_subgraphs=True`), latency; `score_llm`: structured `Judgement{score:1..5, pass:bool, reasons:list[str]}` with the example rubric (`ScriptedChatModel(responder=...)` offline). Targets that interrupt are auto-resumed from `auto_resume` (HITL inside eval).
**Graph-to-graph.** `run_example` uses SDK pattern D with a derived thread `uuid5(eval_run_id/example_id)` and `durability="exit"`; optional `target_url` switches to `RemoteGraph(graph_id, url=...)` for another deployment (UNVERIFIED cross-deployment). Concurrency is bounded by the `concurrency` field AND by the server's `--n-jobs-per-worker` (must be > concurrency + 1, Gotcha 2).
**Memory.** Baselines `("eval","baselines",suite)`, reports `("eval","reports",suite)`; failures feed `memory_reflection` (mode `reflect`) through a router event.
**Streaming.** `custom` `metric {example_id, metric, value}` and `progress`; final `values`.
**Optional LangSmith bridge.** `langsmith.evaluate(target, data=[Example(...)], evaluators=[...], upload_results=False)` works fully offline when `data` are `Example` objects (plain dicts fail: `'dict' object has no attribute 'modified_at'`) - RUN p12.
**Features owned:** datasets, graph-to-graph via SDK + RemoteGraph, `Send`/`defer`, structured-output judge, regression gate interrupt, Store baselines, trajectory evaluation from nested streams. **Studio:** the offline analogue of "Run experiments over a dataset" (the real feature needs LangSmith datasets, DOCS studio); eval threads inspectable; fork at `score_llm` with an edited rubric.
**Tests.** target = tiny in-process fake graph behind a `TargetRunner` protocol; golden-set tests for the rule scorers; judge responder keyed on example id; regression-gate interrupt resume paths; one integration test through a real dev server.

---

## 3. Coverage matrix

`●` = owns/showcases it (build the canonical example here). `○` = also uses it. Columns: ER event_router, SS support_supervisor, RA research_agent, BD batch_digest, AG approval_gate, PC period_close, IR incident_runbook, MR memory_reflection, EJ eval_judge.

| Feature | ER | SS | RA | BD | AG | PC | IR | MR | EJ |
|---|---|---|---|---|---|---|---|---|---|
| input/output/context schemas | ● | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Runtime (context, store, execution_info, server_info) | ● | ○ | ○ | | | ○ | ○ | ○ | |
| `Command(goto,update)` + `destinations` | ○ | ● | | | ○ | ○ | | | |
| `Command.PARENT` / handoff tool | | ● | | | | | | | |
| `Send` fan-out | ○ | | | ● | ○ | ○ | | | ○ |
| per-`Send` timeout / `TimeoutPolicy` | ○ | | | ● | | ○ | | | |
| custom reducers, `Overwrite` | | | ○ | ● | | ○ | | ○ | ○ |
| `DeltaChannel` (beta) | ○ | | | | | ● | | | |
| `defer` | ○ | | | ● | ○ | ○ | | | ○ |
| node `metadata`, `destinations` drawing | ○ | ● | | ○ | | ○ | | | |
| `RetryPolicy` (+ custom `retry_on`) | ○ | ○ | ○ | ○ | | ● | ○ | | |
| `CachePolicy` (+ `key_func`) | | ○ | | ● | | ○ | ○ | | |
| `error_handler` / `set_node_defaults` | ● | | | ○ | | ○ | | | |
| `interrupt` single (approve) | | ○ | ○ | | ● | | ○ | ○ | ○ |
| multiple interrupts in one node | | | | | ● | | | | |
| parallel interrupts + resume-by-id map | | | ○ | | ● | | | | |
| `response_schema` validation | | | | | ● | | ○ | | |
| static breakpoints | | ○ | | | ○ | ● | | | |
| interrupt inside a tool | | | ● | | | | | | |
| HITL middleware (approve/edit/reject/respond) | | | ● | | | | | | |
| durability modes + crash recovery | | | | | | ● | | | |
| time travel: history/replay/fork/`update_state(as_node)` | | | | | ○ | ● | | | |
| subgraph-as-node, `checkpointer` None/True/False | | ● | | | ○ | | | | |
| wrapper-node subgraph call w/ state mapping | | ● | | | | | ○ | | |
| `RemainingSteps` loop guard | | ● | | ○ | ○ | | | | |
| Functional API `@entrypoint/@task` | | | | | | | ● | | |
| `previous` + `entrypoint.final` | | | | | | | ● | | |
| functional -> graph composition | | | | | | | ● | | |
| `create_agent` + custom middleware | | ○ | ● | | | | | | |
| summarization / PII / limits / fallback / todo | | | ● | | | | | | |
| structured output (`response_format`/`with_structured_output`) | ○ | ○ | ● | | | | ○ | ○ | ○ |
| `ToolRuntime` (state/store/context/writer) | | ○ | ● | | | | | | |
| Store put/get/search | ● | ○ | ○ | ○ | ○ | | ○ | ○ | ○ |
| Store semantic search + filter + ttl config | | ○ | ○ | | | | | ● | |
| messages (`add_messages`, `RemoveMessage`, trim) | | ● | ○ | | | | | | |
| SDK in-process graph-to-graph | ● | | | | | | | ○ | ○ |
| interrupt bubbling across graphs | ● | ○ | | | | | ○ | | ○ |
| `RemoteGraph` (explicit client/url) | | | | | | | | | ● |
| thread-metadata inbox (`status=interrupted`) | | | | | ● | | | | |
| crons | | | | | | | | ● | |
| webhooks (completion callback) + custom `http.app` | ● | | | | | | | | |
| assistants / context presets | ○ | ● | | | | | | | |
| MCP/A2A auto-exposure (server-wide) | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| stream `messages-tuple` tokens | | ● | ○ | ○ | ○ | | | | |
| stream `custom` (uniform `emit`) | ○ | ○ | ○ | ● | ○ | ○ | ○ | ○ | ○ |
| stream `updates` / `values` | ○ | ○ | ○ | ○ | ○ | ○ | ● | ○ | ○ |
| stream `tasks` / `checkpoints` / `debug` | | | | | | ● | | | |
| `subgraphs=True` namespaced streaming | | ● | ○ | | ○ | | | | ○ |
| v3 `StreamTransformer` (beta, local/optional server) | | | | ● | | | | | |
| Pydantic at the edge, TypedDict inside | ○ | ○ | ○ | ○ | ● | | ○ | ○ | ○ |
| datasets / golden-set evaluation, LangSmith offline bridge | | | | | | | | | ● |
| Studio: graph viz + xray | | ● | | | | | | | |
| Studio: chat mode | | ○ | ● | | | | | | |
| Studio: input form / assistants / config | ● | ○ | | | | | | | |
| Studio: edit state + fork + re-run | | ○ | | | ○ | ● | | | |
| Studio: breakpoints + Continue | | ○ | | | ○ | ● | | | |
| Studio: interrupts / resume | | | ○ | | ● | | ○ | | |
| Studio: Store / long-term memory | ○ | | | | | | | ● | |
| Studio: experiments/datasets (cloud-only; offline analogue) | | | | | | | | | ● |

No empty rows. Deliberate non-goals: no `Overwrite`-heavy chat memory graph, no separate swarm graph (same mechanism as the supervisor handoff tool), no `deepagents`.

---

## 4. Cross-graph contracts

### 4.1 Event envelope (entering `event_router`)

```python
class EventEnvelope(BaseModel):
    schema_version: Literal[1] = 1
    id: str                                   # producer-assigned, globally unique -> idempotency key (ULID/UUID)
    type: str                                 # "ticket.created" | "ticket.updated" | "chat.message" | "document.batch"
                                              # | "invoice.batch_ready" | "alert.raised" | "approval.requested"
                                              # | "conversation.ended" | "feedback.recorded" | "eval.requested" | other
    source: str                               # "zendesk-sim", "dashboard", "cron", "eval_judge"
    time: str                                 # ISO-8601 UTC STRING (not datetime; see Gotcha 6)
    tenant: str = "acme"
    subject: str | None = None                # entity id: ticket id, alert fingerprint, session id, period
    actor: str | None = None                  # user id
    correlation_id: str | None = None         # defaults to id; copied to every child run's metadata
    priority: Literal["low","normal","high","urgent"] = "normal"
    data: dict[str, Any] = {}                 # type-specific payload (validated by the handler's input_schema)
```
Ingress paths: (a) `POST /events` custom route -> in-process `runs.create` on the router thread (RUN c11); (b) dashboard/SDK `runs.create(thread, "event_router", input={"event": ...})`; (c) Studio form; (d) MCP `tools/call event_router`.

### 4.2 Shared result and artifact types

```python
class Artifact(TypedDict): kind: Literal["text","table","report","link","diff"]; name: str; ref: str | None; data: Any | None
class GraphResult(TypedDict):
    graph_id: str; status: Literal["ok","needs_approval","failed","skipped","duplicate"]
    event_id: str | None; correlation_id: str | None; summary: str
    artifacts: list[Artifact]; metrics: dict[str, float]; child_threads: list[str]
class ThreadRef(TypedDict): graph_id: str; thread_id: str; run_id: str | None
```
Every handler graph's `output_schema` exposes `result: GraphResult`; the router stores `child_threads` so the dashboard can follow links. Only plain JSON types in shared state (`str` timestamps, `int`, `float`, `bool`, `list`, `dict`, Enum-as-str); Pydantic only at graph edges (Gotcha 6/7).

### 4.3 Naming

- Graph ids snake_case = `langgraph.json` keys = `graph_id` in metadata. Node names: verbs (`dedupe`, `plan_dispatch`), subgraph nodes nouns (`researcher`). Reserved: `__start__`, `__end__`, `__error_handler__<node>`.
- Store namespace tuples: lowercase, tenant second, never user text in the first element: `("events","seen",tenant)`, `("memories",tenant,user_id,kind)`, `("lessons",tenant,agent_id)`, `("effects",tenant)`, `("approvals","audit",tenant)`, `("eval","baselines"|"reports",suite)`, `("incidents",tenant)`, `("router","stats",tenant)`.
- Thread metadata keys: `kind` (`approval|ticket|alert|digest|close|memory|eval|chat|router`), `tenant`, `subject`, `correlation_id`, `event_id`, `requester`.
- Custom stream event: one envelope for all graphs, produced by one helper (verified):
```python
def emit(kind: Literal["progress","log","metric","artifact","handoff","decision"], key: str, **data):
    try: get_stream_writer()({"v": 1, "kind": kind, "key": key, "data": data})
    except RuntimeError: pass            # outside a run (unit tests)
```
graph/node/path are NOT in the payload: clients derive them from the SSE namespace (`custom|inner:<task_id>`), the same way for any depth (`inner:<id>|work:<id>` is the `checkpoint_ns`, RUN p20). `key` is deterministic (`"summarize_doc:<doc_id>"`) so the UI can drop duplicates re-emitted after an interrupt resume (Gotcha 14).

### 4.4 Thread ids, checkpoints, idempotency

- **Deterministic thread ids:** `thread_id = uuid5(APP_NS, f"{graph_id}/{tenant}/{key}")`, with `key` = `event.id` for one-shot work (`batch_digest, memory_reflection, eval_judge, period_close:<period>`), `subject` for conversational/stateful work (`support_supervisor: ticket:<id>`, `research_agent: session`, `incident_runbook: alert fingerprint` so `previous` accumulates), approval id for `approval_gate`. Create with `threads.create(thread_id=, if_exists="do_nothing", metadata={...})`.
- **Same event twice:** a deterministic thread id alone does NOT dedupe - the second `runs.create` created a second run on the same thread (RUN c11). Dedupe = router ledger (`("events","seen",tenant)`) + `multitask_strategy="reject"` on ingestion so concurrent duplicates serialise/409. Handlers called directly (dashboard) accept `idempotency_key` in input and claim it in the first node.
- **Side effects:** only after the last `interrupt()` in their node, keyed by a business key (`request.id`, `alert_id`, `ledger/period`) recorded in `("effects", tenant)` BEFORE acting; never keyed by `task_id`.
- **Checkpoints:** server owns the checkpointer. Modules export graphs compiled WITHOUT one (Gotcha 1); tests inject `InMemorySaver`. Subgraph mode: default inherit; `True` only for `researcher` (accumulating); `False` for stateless `policy`. Durability per run: `sync` for `approval_gate` and `period_close`, default `async` elsewhere, `exit` for eval sub-runs. Thread TTL via `checkpointer.ttl` (e.g. `keep_latest` after 7 days) - config exists (SRC), enforcement not run.
- **Concurrency:** one active run per thread; use `multitask_strategy` explicitly (`reject` for ingestion, `enqueue` for user chat, `interrupt` for "supersede").

### 4.5 How results/approvals flow back

1. Handlers either finish with `result` (status `ok`) or pause with `interrupt(...)` (status shows `interrupted` on the thread).
2. Approval requests created by any graph appear in ONE inbox: `threads.search(status="interrupted", metadata={"kind": "approval"})` (RUN c17) - for refunds the supervisor's thread itself is interrupted (bubbled), so the dashboard resolves the *leaf* interrupt by reading `thread["interrupts"]` (`{task_id: [{id, value}]}`) and resuming the **owning thread** with `Command(resume={interrupt_id: decision})`.
3. When the router runs in `await` mode it surfaces the child's interrupt as its own (pattern G) - the dashboard then resumes the router thread only.
4. Completion notifications: `runs.create(webhook="/dash/hook")` (needs `disable_loopback:false`, RUN c11) or the dashboard keeps a `join_stream`/poll on `runs`.
5. Learning loop: finished conversations/feedback/eval failures raise events (`conversation.ended`, `feedback.recorded`) back into the router -> `memory_reflection`.

### 4.6 Streaming from nested graphs to the dashboard

Subscribe with `client.runs.stream(thread_id, graph_id, input|command, stream_mode=["updates","messages-tuple","custom","values","tasks"], stream_subgraphs=True, stream_resumable=True)`. Event names: `updates`, `updates|researcher:<task_id>`, `messages|<ns>`, `custom|<ns>`, `values`, `tasks|<ns>`, `checkpoints|<ns>` (RUN c2, c5). Rules:
- Split the event name on the first `|`: left = mode, right = namespace path (`node:<task_id>` segments joined by `|`; persistent `checkpointer=True` subgraphs have no `:<task_id>`).
- Tokens: `messages` event data = `[chunk, metadata]`; show only chunks whose `metadata.langgraph_node`/ns match the visible node set; internal LLM calls use `tags=["nostream"]` (RUN p13). `metadata.lc_agent_name` identifies `create_agent` agents.
- Interrupts: `values`/`updates` carry `__interrupt__: [{value, id, response_schema}]` (RUN c2); the terminal `values` event of an interrupted run includes them.
- v3 option: `client.threads.stream(assistant_id=...)` gives typed `messages/tool_calls/subgraphs/interrupted` (RUN c7) but returns `interrupt.value=null` in `thread.interrupts` and content-block message content; treat as optional, beta.
- Per-graph stream menu (what to turn on in the dashboard): router `updates+custom`; supervisor `updates+messages-tuple+custom+values` with subgraphs; research_agent `messages-tuple+custom+updates`; batch_digest `custom+messages-tuple(compose)+updates`; approval_gate `updates+values+custom`; period_close `tasks+checkpoints+custom+updates`; incident_runbook `updates+custom`; memory_reflection `custom+updates`; eval_judge `custom+values`.

---

## 5. Gotchas (all evidence-backed)

1. **A graph compiled with its own checkpointer crashes the whole `langgraph dev` server at startup** (`GraphLoadError ... includes a custom checkpointer (InMemorySaver)`), not just that graph. Export `compile()` without; `build_graph(checkpointer=...)` for tests. (RUN c: own_ckpt probe, log)
2. **`langgraph dev` runs ONE job at a time unless you pass `--n-jobs-per-worker N`** (help text says 10; code uses `1` when omitted: SRC langgraph_api/cli.py:266). Any node that makes an in-process SDK/RemoteGraph run then deadlocks waiting for a worker slot. Start with `--n-jobs-per-worker 8`; eval concurrency + nesting depth must stay below it. (RUN c2 hang -> fixed)
3. **`add_node("x", RemoteGraph(...))` (or any SDK run) against the same server on the parent's thread deadlocks**: RemoteGraph forwards `thread_id` (SRC remote.py:804) and the nested run queues behind the parent on the same thread (`runs.list` showed parent `running`, callee `pending`). Use derived child thread ids (pattern G) or no thread (stateless). (RUN c5, c15)
4. **Blockbuster**: a sync `time.sleep` inside an `async def` node raised `BlockingError: An internal error occurred`; sync nodes are fine (they run in threads); `--allow-blocking` disables the check. Use `asyncio.to_thread` for blocking libs. A tiny `open().read(1)` in an async node did NOT trip it - do not rely on that. (RUN c4)
5. **Non-serialisable state**: with a checkpointer `TypeError: cannot pickle '_thread.lock' object`; on a stateless run the field silently comes back `null`. Keep clients, locks, models, DB handles out of state (closures/context). (RUN c4)
6. **Server checkpoint round-trip**: with `Any`-typed fields date, datetime, set, UUID, Decimal, Enum, dataclass, Pydantic model, bytes, timedelta all survived resume; **tuple became list**. But a Pydantic-typed state with a dataclass containing a `date` failed on resume (`meta.when` came back `[2026, 1, 2]` -> `ValidationError`). Use ISO strings and plain dict/list; Pydantic only at edges. (RUN c8, c9; local round-trip fine RUN p7)
7. A Pydantic output model's `default_factory` field (`at: datetime`) was absent from the REST output of the run (observed, cause not investigated); don't depend on defaulted fields appearing. (RUN c8)
8. Unregistered custom classes in checkpoints log "will be blocked in a future version"; either avoid them or allowlist in `langgraph.json` `checkpointer.serde.allowed_msgpack_modules`; `LANGGRAPH_STRICT_MSGPACK=true` blocks now. (RUN p7)
9. **Nodes that only return `Command(goto=...)` draw no edges** unless `destinations=` or a `-> Command[Literal[...]]` annotation is given. (RUN p14)
10. **Default cache key hashes the entire node input** -> misses whenever any state field differs; supply `key_func`. (RUN p2a vs p2b)
11. **Default `RetryPolicy.retry_on` does not retry `ValueError/TypeError/RuntimeError/OSError/LookupError...`** (SRC _retry.py); a node raising `ValueError` ran once with `max_attempts=2`. Pass explicit exception types. (RUN p2b)
12. **State edits go through reducers**: `update_state({"log": ["edit"]})` on an `operator.add` field appends; replace with `Overwrite([...])` or JSON `{"type":"__overwrite__","value":[...]}`. Studio's Edit-state is built on `update_state` (UNVERIFIED in UI), so list fields need a plan (Overwrite / RemoveMessage). Parallel writes to one key without reducer -> `InvalidUpdateError`. (RUN p3)
13. `Command(resume=...)` needs a checkpointer ("Cannot use Command(resume=...) without checkpointer", SRC _loop.py).
14. **Interrupt = re-run the node from line 1**: side effects and custom stream events before `interrupt()` repeat on resume (tool body executed twice, RUN p4A; `child.progress` event re-emitted, RUN c2). Put effects after, make them idempotent, give events deterministic `key`s.
15. **Resume with id maps**: with >1 pending interrupt a bare value raises `RuntimeError`; map keys must be the 32-hex interrupt ids; a stale/unknown id silently re-interrupts in lenient mode (RUN p11), and the SDK `strict_resume=True` did not reject it on the dev server (RUN c16) -> validate ids against `thread.interrupts` in the dashboard/BFF.
16. `interrupt(response_schema=PydanticModel)` validates the resume value; invalid input raises `ValidationError`, nothing is saved, the interrupt stays answerable. A dict schema is not validated. (RUN p2c)
17. **Agent-as-tool hides the subgraph**: `get_state(subgraphs=True)` showed no sub-state and xray cannot expand; interrupts and streaming still work. Prefer subgraph-as-node or invoke-inside-node for anything you want to inspect in Studio. (RUN p9)
18. **Subgraph checkpointer modes**: `None` -> ns `child:<task_id>` (fresh per invocation); `True` -> ns `child` (persistent history; two calls to the same persistent subgraph in one step would share a namespace - UNVERIFIED); `False` -> no checkpoints, interrupt still resumes by re-running the subgraph. (RUN p2d)
19. **Timeouts are async-only**; a `TimeoutPolicy` on a sync node cannot interrupt it. Retried by default policy (`NodeTimeoutError` is retryable) unless `retry_on` excludes it; combine with `error_handler`. (SRC state.py, RUN p2b/p8)
20. **Task/node cache is global across threads**: `@task(cache_policy=...)` hit for a second thread with identical args (RUN p8). Put `tenant` in the cache key/args or one tenant's result is served to another.
21. **Error handlers need `error: NodeError` annotated** or injection fails ("missing 1 required positional argument"); they run after retries; handler failures fail the run. (RUN p8; SRC state.py)
22. **`InMemoryStore.put(ttl=...)` raises `NotImplementedError`**; the dev server store accepts `ttl` (minutes) but I did not observe expiry. Query+filter returns `index=False` items with `score=None`. (RUN p3, c4)
23. **Store embeddings offline**: `store.index.embed` must be an importable callable (`"./embedding.py:aembed"`) returning vectors of exactly `dims`; both sync and async worked; NumPy missing only warns (pure-Python path). A hashed bag-of-words embedder is lexical, not semantic - fine for deterministic tests only (my `$S/devproj/embed_fake.py`, 256 dims). (RUN c4, p3)
24. **`.langgraph_api/` in the project dir persists threads/store across `langgraph dev` restarts** (flush loop); delete it for clean tests. Do not write logs/data files inside the watched project tree (constant "N changes detected"). (RUN, logs)
25. The dev server posts anonymous usage metadata to `api.smith.langchain.com` at startup even with `LANGSMITH_TRACING=false` (log: "Successfully submitted metadata to LangSmith instance"); `LANGGRAPH_CLI_NO_ANALYTICS=1` only silences the CLI analytics (SRC cli analytics.py:89). I found no opt-out for the server metadata loop (UNVERIFIED that it fails silently offline).
26. **Server rejects `stream_mode` `tools` and `lifecycle` (HTTP 422)**; allowed: `values, messages, messages-tuple, tasks, checkpoints, updates, events, debug, custom`. Legacy `messages` emits `messages/partial|metadata|complete`; use `messages-tuple`. (RUN c2, c14)
27. **`runtime.context` is `None` locally unless `context=` is passed** (even with dataclass defaults) but a populated instance on the server (RUN p8 crash, c12). Helper: `ctx = runtime.context or RouterCtx()`.
28. **Functional API in Studio** = one node; input schema = raw param schema (a `list[int]` param -> array form); wrap inputs in one TypedDict param. Entry-point as a StateGraph node receives the whole state dict, not your param. (RUN c4, p15)
29. **Webhooks to loopback/relative URLs are blocked by default**; internal `/dash/hook` requires `"webhooks":{"url":{"disable_loopback":false}}`. (RUN c11)
30. **Scripted-iterator fakes don't fit a server**: `GenericFakeChatModel(messages=iter([...]))` holds mutable global state (shared across threads, exhausted on replay/fork) and does not implement `bind_tools` (SRC fake_chat_models.py:227) -> unusable with `create_agent`. Use the stateless `ScriptedChatModel` below. A step-indexed `script=[...]` only works when the model sees its own previous AIMessages (agents); plain-node models must use `responder=`.
31. HITL batched mode: rejection ToolMessage was ordered before the executed tool's message; `per_call` mode gave 2 interrupts for 2 calls; interrupt lives in node `HumanInTheLoopMiddleware.after_model`. (RUN p4C, c3)
32. v3 `stream_events` projections are live single-consumer cursors; draining one first leaves the others empty; non-native transformer projections are in `run.extensions[name]`; the API is beta (`LangChainBetaWarning`). (RUN p5)
33. Duplicate ingestion: deterministic thread ids don't dedupe runs (RUN c11) - see 4.4.
34. `RemoteGraph.stream_events(version="v3")` rejects `control, transformers, interrupt_before/after` (SRC remote.py:197).
35. Default recursion limit is 10007; loops need `RemainingSteps` or counters. (SRC, RUN p18)
36. `langsmith.evaluate(data=[dict,...])` fails offline; needs `Example` objects and `upload_results=False`. (RUN p12)
37. Custom `http.app` routes are unauthenticated unless `http.enable_custom_route_auth`; CORS for a separate dashboard origin must be set in `http.cors` (`allow_origins/methods/headers/credentials`). (SRC schemas.py:397,463)
38. Python 3.12: callbacks/stream writer propagate through async nodes automatically (RUN p13); on <3.11 you would have to pass `config` manually - not relevant here.

---

## 6. Minimal fake chat model (verified)

Requirements met (all RUN): works inside `create_agent` (tool loop, `response_format`, middleware incl. HITL/limits/fallback/PII/summarization/todo), `bind_tools`, `with_structured_output`, token streaming (`messages` mode, word tokens + tool-call-arg chunks), replay/fork-safe (pure function of the transcript), picklable, no network. Source: `$S/fakes.py` (copy to `src/meridian/testing/fakes.py`).

```python
from __future__ import annotations
import json, re, uuid
from collections.abc import Iterator, Sequence
from typing import Any
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

def tc(name: str, args: dict | None = None, id: str | None = None) -> dict:
    return {"name": name, "args": args or {}, "id": id or f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}

def _turn_index(messages: list[BaseMessage]) -> int:        # AI turns since the last human message
    n = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage): break
        if isinstance(m, AIMessage): n += 1
    return n

class ScriptedChatModel(BaseChatModel):
    script: list[Any] = Field(default_factory=list)          # reply i = script[i]; str or AIMessage(tool_calls=[tc(...)])
    responder: Any = None                                    # OR callable(messages, bound_tool_names) -> AIMessage | str
    bound_tool_names: list[str] = Field(default_factory=list)
    bound_tool_schemas: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str: return "scripted-fake"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kw: Any) -> Runnable:
        schemas = [convert_to_openai_tool(t) for t in tools]
        return self.model_copy(update={"bound_tool_names": [s["function"]["name"] for s in schemas], "bound_tool_schemas": schemas})

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        out = self.responder(messages, self.bound_tool_names) if self.responder else self.script[min(_turn_index(messages), len(self.script) - 1)]
        msg = AIMessage(content=out) if isinstance(out, str) else out
        return msg if msg.id else msg.model_copy(update={"id": f"run-{uuid.uuid4().hex}"})

    def _generate(self, messages, stop=None, run_manager: CallbackManagerForLLMRun | None = None, **kw) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    def _stream(self, messages, stop=None, run_manager: CallbackManagerForLLMRun | None = None, **kw) -> Iterator[ChatGenerationChunk]:
        msg = self._reply(messages)
        for tok in [p for p in re.split(r"(\s+)", msg.content if isinstance(msg.content, str) else "") if p]:
            ch = ChatGenerationChunk(message=AIMessageChunk(content=tok, id=msg.id))
            if run_manager: run_manager.on_llm_new_token(tok, chunk=ch)
            yield ch
        for i, call in enumerate(msg.tool_calls):               # provider-style: args JSON split over two chunks
            args = json.dumps(call["args"]); half = len(args) // 2
            for piece, first in ((args[:half], True), (args[half:], False)):
                ch = ChatGenerationChunk(message=AIMessageChunk(content="", id=msg.id, tool_call_chunks=[
                    tool_call_chunk(name=call["name"] if first else None, args=piece, id=call["id"] if first else None, index=i)]))
                if run_manager: run_manager.on_llm_new_token("", chunk=ch)
                yield ch
        yield ChatGenerationChunk(message=AIMessageChunk(content="", id=msg.id, chunk_position="last",
              response_metadata={"finish_reason": "tool_calls" if msg.tool_calls else "stop", "model_name": "scripted-fake"}))
```

Usage patterns (each run):
```python
agent = create_agent(ScriptedChatModel(script=[AIMessage(content="Let me add.", tool_calls=[tc("add", {"a": 2, "b": 3}, "c1")]), "The answer is 5."]), tools=[add])
create_agent(ScriptedChatModel(script=[AIMessage(content="", tool_calls=[tc("add", {...}, "c1")]),
                                       AIMessage(content="", tool_calls=[tc("Verdict", {"answer": 5, "confidence": 0.9}, "c2")])]),
             tools=[add], response_format=Verdict)       # structured output = tool call named after the schema class
ScriptedChatModel(responder=my_fn).with_structured_output(Route).invoke(msgs)   # routing/judge models (RUN p17)
ScriptedChatModel(script=["hidden"]).with_config(tags=["nostream"])             # internal LLM calls invisible to token streams (RUN p13)
```
Offline model selection in the app: `get_model(role)` returns `ScriptedChatModel`/responder fakes when `MERIDIAN_MODEL=fake` (default) and `ChatAnthropic`/`ChatOpenAI` only when keys exist; build models inside nodes or `lru_cache` factories, never at import.

---

## Appendix A - `langgraph.json` target shape (keys verified against `langgraph_cli/schemas.py`; the `store.index` + `http.app` + `webhooks` parts were run in `$S/devproj`)

```json
{
  "dependencies": ["."],
  "graphs": {
    "event_router":       {"path": "./src/meridian/graphs/event_router.py:graph",       "description": "Single ingress: dedupe, classify, dispatch"},
    "support_supervisor": {"path": "./src/meridian/graphs/support_supervisor.py:graph", "description": "Hand-rolled multi-agent ticket resolver"},
    "research_agent":     {"path": "./src/meridian/graphs/research_agent.py:graph",     "description": "create_agent + middleware analyst (chat mode)"},
    "batch_digest":       {"path": "./src/meridian/graphs/batch_digest.py:graph",       "description": "Send map-reduce document digest"},
    "approval_gate":      {"path": "./src/meridian/graphs/approval_gate.py:graph",      "description": "HITL: approve/edit/reject/question/four-eyes"},
    "period_close":       {"path": "./src/meridian/graphs/period_close.py:graph",       "description": "Deterministic durable month-end close"},
    "incident_runbook":   {"path": "./src/meridian/graphs/incident_runbook.py:graph",   "description": "Functional API runbook"},
    "memory_reflection":  {"path": "./src/meridian/graphs/memory_reflection.py:graph",  "description": "Writes memories and lessons"},
    "eval_judge":         {"path": "./src/meridian/graphs/eval_judge.py:graph",         "description": "Dataset evaluation + LLM judge"}
  },
  "env": ".env",
  "store": {"index": {"embed": "./src/meridian/memory/embeddings.py:aembed", "dims": 256, "fields": ["text"]},
            "ttl": {"default_ttl": null, "refresh_on_read": true, "sweep_interval_minutes": 60}},
  "checkpointer": {"ttl": {"strategy": "keep_latest", "default_ttl": 10080, "sweep_interval_minutes": 60}},
  "http": {"app": "./src/meridian/server/app.py:app",
           "cors": {"allow_origins": ["http://localhost:5173"], "allow_methods": ["*"], "allow_headers": ["*"]}},
  "webhooks": {"url": {"disable_loopback": false}}
}
```
Start command used by tests/dev: `LANGGRAPH_CLI_NO_ANALYTICS=1 .venv/bin/langgraph dev --port <p> --no-browser --n-jobs-per-worker 8` (keep logs outside the project tree). Note `dependencies: ["."]` with flat modules importable from the config dir worked in my probe (RUN); a `src/` layout needs the package installed into the venv (UNVERIFIED).

## Appendix B - test harness contract (for the test specialist)

- `build_graph(checkpointer=None, store=None, cache=None, **deps)` per graph; `deps` are Protocol objects (`Dispatcher`, `TargetRunner`, clocks) so SDK calls are replaceable in unit tests.
- `run_graph(graph, input, resumes=[...], config=...)`: loops `invoke(version="v2")`, collecting `GraphOutput.interrupts`, feeding `Command(resume=...)` (single value or `{id: value}` map when several), until `interrupts == ()`.
- Fixtures: `InMemorySaver`, `InMemoryStore(index={"embed": fake_embed, "dims": 256, "fields": ["text"]})`, `InMemoryCache`.
- Dev-server smoke (`scripts/verify_server.py`): start on a test port with `--n-jobs-per-worker 8`, create an assistant per graph, run one canonical input each over SDK incl. `stream_subgraphs=True`, resume interrupts, assert thread statuses and MCP `tools/list` contains all 9 ids, stop server.

## Appendix C - probe index (`$S`)

| File | Proves |
|---|---|
| `fakes.py`, `p1_fake_agent.py`, `p17_responder.py`, `p13_tokens.py` | fake model: tool loop, structured output, token streaming, responder routing, `nostream` |
| `p2a_core.py`, `p2b_errors.py` | schemas, context schema, metadata, retry/cache/timeout/error_handler, cache key trap |
| `p2c_interrupts.py`, `p11_strict.py` | parallel/multiple interrupts, id-map resume, response_schema validation, strict resume |
| `p2d_subgraphs.py`, `p9_nested.py`, `p15_fn_graph.py`, `p22_agent_ckpt_modes.py` | subgraph modes/namespaces (also via `create_agent(checkpointer=)`), Command.PARENT, agent-as-tool, invoke-inside-node, functional<->graph |
| `p2e_timetravel.py`, `p16_resume_fail.py` | history, replay, fork, bulk update, durability modes, crash recovery |
| `p3_store_reducers.py`, `p6_delta.py`, `p18_managed.py`, `p14_edges.py`, `p10_misc.py` | Store/semantic/filter/ttl, reducers/Overwrite, message utils, DeltaChannel, RemainingSteps, edge drawing, `entrypoint.final`, `Command` Sends |
| `p4_agents.py`, `p19_tools_mode.py`, `p21_handoff.py` | middleware matrix, tool interrupts, ToolRuntime, hook order, `tools` stream mode, handoff tool `Command(graph=PARENT)` from a create_agent subgraph |
| `p5_v3stream.py`, `p20_emit.py` | v3 streaming + transformer, uniform `emit` envelope + ns format |
| `p7_serde.py`, `p8_func_send.py`, `p12_ls_eval.py` | serde warning, functional API, map-reduce w/ timeouts+handlers, offline langsmith evaluate |
| `devproj/{graphs,graphs2,graphs3,graphs4}.py`, `webapp.py`, `langgraph.json`, `start_dev.sh`; `c1..c17_probe.py` | real `langgraph dev` runs: schemas/xray, SDK streaming (subgraphs), HITL middleware over HTTP, blocking/serde/store/factory, supervisor, RemoteGraph deadlock, delegation pattern, assistants/crons/MCP, context semantics, time travel/breakpoints, webhooks/ingest, thread inbox |

## Appendix D - not verified / open

- Any Studio UI behaviour (form rendering of `response_schema`, how interrupt payloads render, state-edit UX on list reducers, drill-down on `checkpointer=True` subgraphs, how functional-API tasks appear in the thread view). Server APIs behind them were exercised, the UI was not.
- SDK `strict_resume=True` rejection on the server (did not reject in c16); `durability=` passed through `runs.create`; thread/Store TTL expiry; `ProviderStrategy` structured output; middleware not in the run column of 1.9; `RunControl` drain; `stream_events(version="v3")` with `compile(transformers=)` on the server; subgraph namespace collision for `checkpointer=True` called twice in a step; depth-2 server event-name format (`mode|a|b` assumed from `checkpoint_ns`); RemoteGraph against a *different* deployment; A2A endpoint; src-layout import under `langgraph dev`; a real LLM provider (none called, no keys read).
- Network behaviour offline: the dev server contacted `api.smith.langchain.com` during my runs; I did not test with the network cut.
