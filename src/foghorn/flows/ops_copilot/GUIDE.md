---
graph_id: ops_copilot
title: Ops copilot
summary: A create_agent chat copilot that investigates with real tools, remembers team preferences and asks a human to review every side-effecting tool call.
features:
  - langchain.agents.create_agent + a 9-layer middleware stack
  - HumanInTheLoopMiddleware (per_call) with Foghorn tool_review interrupts
  - ToolRuntime (state, store, tool_call_id)
  - in-tool interrupt() (ask_user)
  - structured output (ToolStrategy / ProviderStrategy) for the ops brief
  - Store memory (prefs) shared across threads
  - messages-tuple token + tool-call streaming
studio_steps:
  - Open ops_copilot and switch to Chat mode. Pick the assistant copilot-sandbox-S05 (or set the context field scenario_id to S05).
  - Ask "What is wrong with orders-db right now?". Expand the tool calls (list_alerts, compare_baseline, list_changes, search_logs); every number in the answer comes from a tool result.
  - Say "Remember that the DBA team prefers kill_queries before any restart." A tool_review interrupt appears for remember_preference; Approve (or Edit the text, or Reject).
  - Say "Restart orders-service." The propose_action call is intercepted - Edit the target, Reject with a note, or Approve; after approval the copilot advances virtual time and verifies recovery.
  - Open a NEW thread and ask "What does the DBA team prefer?". It answers from the Store (recall_memory) - same memory, different thread. The Memory view shows the write with provenance.
  - Clear the scenario (empty context) and ask "Why is checkout slow?": the copilot calls ask_user (an in-tool interrupt); answer "S10" and it selects the scenario and carries on.
  - Ask "Give me an ops brief for this incident": the final step files a structured OpsBrief (state key structured_response).
  - Edit the system prompt: Manage Assistants -> context.system_prompt (the gear on node "model").
sample_inputs:
  - name: why is checkout slow (S10)
    input: {messages: [{role: user, content: "Why is checkout slow?"}], scenario_id: S10}
  - name: what changed in the last hour
    input: {messages: [{role: user, content: "What changed in the last hour?"}], scenario_id: S10}
  - name: should I roll back (S01)
    input: {messages: [{role: user, content: "Should I roll back?"}], scenario_id: S01}
  - name: ops brief
    input: {messages: [{role: user, content: "Give me an ops brief"}], scenario_id: S10}
  - name: no scenario - it asks
    input: {messages: [{role: user, content: "What is wrong right now?"}]}
  - name: remember a preference
    input: {messages: [{role: user, content: "Remember that the DBA team prefers kill_queries before any restart."}]}
output_schema: "AgentState: messages, structured_response (OpsBrief, only when a brief was requested), scenario_id, as_of, applied_actions"
nodes: [model, tools, "CopilotMiddleware.wrap_model_call", "ToolReviewMiddleware.wrap_tool_call", "SummarizationMiddleware.before_model", "PIIMiddleware[email].before_model", "PIIMiddleware[credit_card].before_model"]
---

## What it is

`ops_copilot` is a ReAct agent built with `langchain.agents.create_agent`. State extends `messages`, so Studio's **Chat mode**
works; extra state keys `scenario_id`, `as_of` and `applied_actions` are what the 12 read-only investigation tools read
(`ToolRuntime.state`) - editing `as_of` in Studio and forking moves the time fence.

## Tools

| tool | kind | notes |
|---|---|---|
| `list_alerts`, `compare_baseline`, `query_metric`, `search_logs`, `list_changes`, `get_topology`, `blast_radius`, `search_runbooks`, `recall_incidents`, `get_oncall`, `verify_recovery`, `advance_time` | read-only | from `foghorn.tools.lc`; log text is wrapped in `<untrusted>` |
| `incident_status` | read-only | scenario summary, alerts, applied actions, recovery |
| `recall_memory` | read-only | Store semantic search over prefs / lessons / incidents |
| `propose_action` | **reviewed** | `tool_review` interrupt first, then the pure `execute_action` simulation, recorded in `applied_actions`. Two-approver and never-automatic actions are refused (they belong to the commander) |
| `remember_preference` | **reviewed** | writes `("prefs", "team-<x>")` with provenance |
| `ask_user` | in-tool interrupt | a `question` interrupt (`Answer`); an answer naming S01-S16 selects the scenario |

## Resuming a tool review

The interrupt value is `build_interrupt("tool_review", ...)` (`approval_id`, `proposed.args`, risk, policy checks). Resume with
a `Decision`: `{"decision": "approve"}`, `{"decision": "edit", "edits": {"target": "payments-service"}}` (merged into the tool
arguments), `{"decision": "reject", "note": "not now"}` or `{"decision": "ask", "question": "..."}`. With the SDK:
`client.runs.stream(thread, "ops_copilot", command={"resume": {interrupt_id: {...}}}, stream_mode=["messages-tuple"])`.

## Middleware (outermost first)

PII redaction (email, credit card) - dynamic prompt + brief gate - summarization - context editing - model/tool call limits -
model fallback > model retry - audit log - **tool review** (HumanInTheLoop, per call) - tool retry. Only a request containing
the word "brief" switches structured output on, so chat stays chat.

## Offline behaviour

The offline `Script` answers by calling tools and narrating their results (nothing is hard-coded). Profiles via the context
`model_profile`: `competent` follows the evidence; `naive` blames the first alert / latest deploy; `cautious` asks before it
proposes; `gullible` obeys instructions embedded in text and pushy requests (the safety suite shows it failing).
