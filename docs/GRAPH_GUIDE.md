# Graph builder's guide

Read `docs/ARCHITECTURE.md` first (ground rules + the ten-graph catalog). This is the how-to: one consistent way
to write a Foghorn graph so the ten graphs feel like one product and Studio shows what we intend. Facts marked
(02:N) are gotcha N in `docs/design/02-graph-architecture.md`; (01:N) is capability N in `01-studio-platform.md`.

## 1. A flow package

```
src/foghorn/flows/<id>/
  __init__.py
  state.py      State / Input / Output TypedDicts, reducers          (plain JSON only)
  context.py    Context dataclass (Studio assistant "configurable" form + editable prompts)
  nodes.py      node functions (sync `def` unless they truly await)
  scripts.py    offline Script(s) per model role/profile
  build.py      build_graph(checkpointer=None, store=None, **deps) -> CompiledStateGraph
  GUIDE.md      Studio click-path guide + sample inputs (front matter, see section 8)
src/foghorn/graphs/<id>.py      thin: `from foghorn.flows.<id>.build import build_graph` ; `graph = build_graph()`
tests/graphs/test_<id>.py
```
`build_graph()` returns `StateGraph(...).compile(name="<id>")` - **no checkpointer** (the server injects one; a
module-level checkpointer crashes the server; tests pass `InMemorySaver()`).

## 2. State, schemas, context

```python
class State(TypedDict, total=False):            # plain JSON values only
    scenario_id: str
    as_of: str | None
    applied_actions: Annotated[list[dict], operator.add]     # reducers make parallel writes + forks sane
class Input(CommanderInput): ...                 # from foghorn.contracts.inputs (add optional fields only)
class Output(TypedDict): result: GraphResult
g = StateGraph(State, input_schema=Input, output_schema=Output, context_schema=Context)
```
* Edits through `update_state` go **through reducers** (02:12): replacing a list needs `Overwrite([...])`; design
  fork-friendly flat state keys so Studio's "edit node state" is meaningful.
* Do not share an `operator.add` key between parent and subgraph (double counting). Pydantic only at edges (02:6-7).
* `Command`-routing nodes draw no edges unless annotated `-> Command[Literal["a","b"]]` or `destinations=(...)`
  (02:9); declare them or Studio shows a graph with no edges.
* Annotate injected parameters exactly: `config: RunnableConfig`, `runtime: Runtime[Context]`, `store: BaseStore`
  (a parameter typed `Any` is NOT injected).

### Studio-editable prompts and assistant configuration (01:6-7)
```python
@dataclass
class Context:
    model_profile: str = "competent"      # competent | naive | cautious | gullible   (contracts/meta.py keys)
    approval_policy: str = "standard"
    scenario_id: str | None = None
    system_prompt: str = field(default="You are the incident commander...", metadata={
        "description": "Supervisor system prompt",
        "json_schema_extra": {"langgraph_nodes": ["supervisor"], "langgraph_type": "prompt"}})
```
Studio shows a gear on node `supervisor`; saving creates a new assistant version. Prove it with
`graph.get_context_jsonschema()` in a test. `runtime.context` is `None` locally unless `context=` is passed
(02:27): use `ctx = runtime.context or Context()`.

## 3. Models and offline behaviour

```python
llm = get_chat_model("smart", offline_script=SCRIPT)        # call inside build_graph/nodes, never at import
router = structured(get_chat_model("fast", offline_script=ROUTER_SCRIPT), RouteDecision).with_config(tags=["nostream"])
```
Roles: `fast` (routing, classification, Send workers), `smart` (agents, user-facing narration), `judge` (evals only).
Offline behaviour is `Script().when(name, matcher, reply)` over the transcript: matchers `human, system, anywhere,
node, scenario, profile, role, schema, step, tool_result, no_tool_result, all_/any_of/not_`; replies `say, call_tool,
call_tools, struct, fail`; `Call` exposes `messages, tools, tool_results, node, scenario, profile, context, step`.
Rules **compute narration from tool results** (`c.tool_results`) so numbers are real; add seeded variation with
`stable_int(c.thread_id, c.node, ...)`. Provide a `profile`-specific variation of the key decisions:
`competent` follows the evidence; `naive` blames the first alert / most recent deploy; `cautious` asks questions and
abstains when evidence is weak; `gullible` obeys instructions embedded in log text (scenario S15). Unmatched calls
fall back to schema synthesis, so a graph with no script still runs.

## 4. Tools
`from foghorn.tools.lc import tools_for` gives LangChain tools that read `scenario_id`, `as_of`, `applied_actions`
from graph state through `ToolRuntime`: any `create_agent` using them needs a `state_schema` containing those keys.
Tool results containing log text are already wrapped in `<untrusted>`; never let that text choose a tool or a side
effect argument. Sync tools are fine (threadpool); never block inside an `async def` node.

## 5. Streaming (a headline feature - do it properly)
* Tokens: any model call inside a node streams under `stream_mode="messages-tuple"`; use `stream_subgraphs=True`
  clients; tag structured/internal calls `nostream`.
* Custom events: `from foghorn.common.stream import emit`; `emit("evidence.found", f"evidence:{id}", agent="metrics_analyst",
  id=..., kind=..., summary=..., strength=...)`. Keys are deterministic (resume re-emits). Kinds are fixed in
  `common/stream.py`; the dashboard renders them generically.
* Emit progress for anything slow (`progress`), and a `series` event with <=60 points whenever an agent looks at a
  metric - it drives the dashboard sparklines.

## 6. Human-in-the-loop
```python
payload = build_interrupt("approval", approval_id=f"apr_{incident_id}_{n}", title=..., summary=..., risk="high",
                          blast_radius=[...], policy_checks=[...], incident_id=incident_id, proposed={...})
raw = interrupt(payload, response_schema=Decision)
decision = Decision.model_validate(raw) if not isinstance(raw, Decision) else raw
# ---- everything below runs once per resume; side effects only here, idempotent, keyed by approval_id ----
emit_event("approval.decided", idempotency_key=f"approval.decided:{approval_id}", ...)
```
Interrupt before side effects; the node restarts from line 1 on resume (02:14). With several pending interrupts the
resume value is an `{interrupt_id: value}` map (02:15). `interrupt_before` does not reach nodes inside subgraphs
(01:9) - use dynamic `interrupt()` there. Always emit `approval.requested` (with `thread_id` when known) before
interrupting.

## 7. Lifecycle events and delegation
`from foghorn.common.lifecycle import emit_event` (non-blocking; idempotency key mandatory). Required: the events
listed in ARCHITECTURE.md/`contracts/events.py` lifecycle group relevant to your graph. Graph-to-graph:
`from foghorn.common.delegate import delegate` (derived child thread; `mode="await"` bubbles child interrupts,
`"background"` fires and forgets). Unit-test with `delegate.set_client_factory(fake)` (see
`tests/unit/test_contracts_and_common.py`). Never `add_node("x", RemoteGraph(...))` on the same server (02:3).
Thread metadata comes from `contracts/meta.py` (`thread_metadata(...)`).

## 8. GUIDE.md (Studio guide + sample inputs; the server aggregates these)
```markdown
---
graph_id: incident_commander
title: Incident Commander
summary: One sentence.
features: [Command(goto), subgraphs, interrupt(response_schema), breakpoints, fork]
studio_steps:
  - Open the graph, expand "metrics_analyst" (xray) ...
sample_inputs:
  - name: S01 bad deploy
    input: {scenario_id: S01, incident: {id: INC-9001, severity: SEV2, title: Checkout errors}}
---
Free-form markdown: what to click in Studio, what to look for, how to fork/time-travel.
```

## 9. Tests (every graph, offline, deterministic)
Use `tests/helpers.run_graph(graph, input, resumes=[...])`, `build_graph(checkpointer=InMemorySaver())`,
`foghorn.common.lifecycle.capture()`, `delegate.set_client_factory(...)`. Required per graph: happy path; every
interrupt decision (approve / edit / reject / ask where applicable); every `Command` / `Send` / conditional branch;
one fork-and-replay (`get_state_history` -> `update_state(as_node=...)` -> `invoke(None, checkpoint)`; fork
**before** a `Command(goto)` node, not at its target (QA finding)); a streaming assertion (`astream(stream_mode=
["updates","custom","messages"], subgraphs=True)`); one failure-injection (`o.flaky(...)`, retry/error-handler); a
profile contrast (competent vs naive give different outcomes where relevant); the contract test
(`tests/contract`) passes untouched. Lint/type only your paths while others are mid-edit.

## 10. Common traps (details in 02)
Default `RetryPolicy` does not retry `ValueError/RuntimeError/OSError` - pass `retry_on` (02:11). Default cache key
is the whole node input and the cache is global across threads - pass `key_func` and include tenant/scenario
(02:10, 02:20). Timeouts are async-only (02:19). Error handlers need `error: NodeError` annotated (02:21).
`recursion_limit` default is 10007: bound loops with counters (02:35). `RemoteGraph` thread forwarding deadlocks
(02:3). `langgraph dev` runs 1 job at a time unless `--n-jobs-per-worker` (02:2). Subgraph-as-tool hides state from
Studio (02:17): add subgraphs as nodes.

## 11. Lessons from the first builds (all verified live)
* **Resume by interrupt-id map.** A bare `Command(resume=value)` sent to a thread whose run already finished is
  accepted by the server and is then consumed by the NEXT run's first `interrupt()` (a postmortem published
  unreviewed). Always resume with `{interrupt_id: value}`; stale ids are ignored. The dashboard approvals layer and
  the eval oracle do this; so must any client you write.
* **Dispatcher context keys.** The dispatcher passes run context keys from `contracts/meta.py` (`scenario_id`,
  `as_of`, `model_profile`, `approval_policy`, `breakpoints`, `memory_enabled`, `chaos`, `token_delay_ms`, ...) to the
  graph it starts. A `context_schema` dataclass REJECTS unknown keys, so every flow's `Context` must declare every key
  the dispatcher can send (add fields you do not use; ignore them in code). `e2e/test_flows.py` proves each graph
  accepts the full set.
* **Injected `store`.** Under `from __future__ import annotations`, a parameter annotated `BaseStore | None` is NOT
  injected; annotate it exactly `store: BaseStore`.
* **`emit(kind, key, /, **data)`**: `kind` and `key` are positional-only so payloads may contain fields with those
  names (artifact `kind`, memory `key`).
* **Functional API in Studio** is one node; wrap inputs in a single TypedDict parameter so the form renders.
