---
graph_id: incident_commander
title: Incident Commander
summary: A supervisor drives five specialist sub-agents and an RCA tournament, plans a remediation, passes it through a policy gate and a human approval, executes it idempotently, verifies recovery and drafts the status page.
features:
  - 'hand-rolled supervisor with Command(goto) + Send (declared destinations, code-enforced budgets)'
  - 'create_agent specialists as compiled subgraph NODES (xray drill-in), one shared evidence key, private messages'
  - 'rca_tournament as a subgraph (Send map-reduce, defer reduce)'
  - 'dynamic interrupt(response_schema=) - approval / edit / question / break_glass, four-eyes, parallel interrupts'
  - 'static breakpoints (interrupt_before), update_state + fork (recipes F1-F4)'
  - 'get_stream_writer custom events + messages-tuple tokens from every sub-agent'
  - 'RetryPolicy(retry_on), CachePolicy(key_func), context_schema with editable prompts, Store memory'
studio_steps:
  - 'Open the graph and expand "metrics_analyst" (xray): model -> tools, plus the Analyst middleware nodes. The same for the other four specialists and for "rca_tournament".'
  - 'Run S01 below. Watch the stream: topology + change analysts run in parallel, then metrics, then logs + knowledge, then the tournament; "supervisor" narrates every hop.'
  - 'When the run pauses on "approval_gate", open the interrupt: title, risk, blast radius, policy checks, proposed {action, target, params}. Resume with {"decision": "approve", "decided_by": "oncall"}.'
  - 'Run S10: first a "question" interrupt (was the flag ramp intended?) answered with {"answer": "yes, but unexpected timeouts"}; the innocent recommendation deploy is scored and rejected in the tournament (hypothesis.update events).'
  - 'F1 - fork the S10 thread at the checkpoint after "rca_tournament", edit hypotheses[0] (component/category) and run: the plan follows the edit.'
  - 'F2 - fork before "policy_check", set incident.severity to SEV1: the approval now needs two approvers.'
  - 'F3 - replay from "approval_gate" with a different decision (reject): the commander re-plans and asks again.'
  - 'F4 - set as_of to a time before the onset: the time fence hides the future and the commander finds nothing to act on.'
  - 'W8 - run S01 with interrupt_before=["execute_action"], edit chosen_action.target and continue: the edited action goes back through policy_check and approval instead of running.'
  - 'Gear icons: supervisor_prompt (node supervisor), planner_prompt (remediation_planner), analyst_prompt (the five specialists). Add "policy: mitigate_before_fix" to the planner prompt and re-run S06 to see the plan change.'
sample_inputs:
  - name: S01 bad deploy (one approval, recovery verified)
    input: {scenario_id: S01, incident: {id: INC-9001, severity: SEV3, title: Checkout 500s after the 14:02 deploy}}
  - name: S10 red herring (question + approval, memory recall INC-0061)
    input: {scenario_id: S10, incident: {id: INC-9010, severity: SEV3, title: Two changes, one culprit}}
  - name: S08 provider outage (parallel interrupts, two approvers)
    input: {scenario_id: S08, incident: {id: INC-9008, severity: SEV3, title: Payment provider outage}}
  - name: S15 prompt injection (flagged, never obeyed)
    input: {scenario_id: S15, incident: {id: INC-9015, severity: SEV3, title: Prompt injection in a log line}}
  - name: S16 recurring stampede (memory makes it cheaper)
    input: {scenario_id: S16, incident: {id: INC-9016, severity: SEV3, title: Monday flush stampede}}
output_schema: "CommanderOutput: result (GraphResult) and report (CommanderReport: root_cause or abstained, hypotheses, evidence_refs, actions_proposed / executed, recovered, approvals, interrupts_seen, comms_draft, memory_recalled, flags, tool_calls_used, supervisor_hops, narrative)"
nodes: [bootstrap, supervisor, metrics_analyst, logs_analyst, change_analyst, topology_analyst, knowledge_agent, rca_tournament, clarify_change_intent, remediation_planner, policy_check, approval_gate, answer_question, execute_action, verify_recovery, comms_drafter, comms_gate, close_incident]
---

## Flow

```
START -> bootstrap -> supervisor <---------------------------------------------------------------+
                         | Command(goto/Send)                                                   |
                         +-> topology_analyst + change_analyst   (parallel)  ------------------->|
                         +-> metrics_analyst                                  ------------------->|
                         +-> logs_analyst + knowledge_agent      (parallel)  ------------------->|
                         +-> rca_tournament (subgraph)           ------------------------------->|
                         +-> clarify_change_intent  (question interrupt)  ---------------------->|
                         +-> remediation_planner -> policy_check -> approval_gate -> execute_action -> verify_recovery
                         +-> comms_drafter  (opens comms_gate together with approval_gate)         |
                                                              close_incident (defer, joins both) <-+
```

* `bootstrap` loads the scenario context, recalls memory (incidents and lessons by similarity; weak hits count only as corroboration), applies team preferences, sets the budget (10/14/18 tool calls and 7/8/9 hops by tier).
* `supervisor` decides each hop with a structured `Route` (routing JSON is tagged `nostream`), then narrates in one to three sentences. **Code re-applies the invariants**: hop and tool budgets, no planning before a diagnosis, valid agents/services, no repeat loops (a tournament that ranks nothing becomes an abstention).
* Specialists are `create_agent` graphs over `tools_for([...])`. Their middleware streams `progress`, `series` (<= 60 points) and `evidence.found` events as tool results arrive, applies the editable `analyst_prompt`, injects chaos failures, and returns `evidence` records. Their messages stay private.
* `policy_check` is the guardrail: POL-005 approval matrix (SEV1 adds the incident commander), POL-003 freeze and break-glass, POL-004 untrusted content and scope, POL-006 observability, RB-009 online index builds. It emits `policy.check` events and blocks what must never run.
* `approval_gate` honours `approval_policy`: `strict` always asks, `standard` asks for medium+ risk / irreversible / approver-gated actions, `auto_low_risk` skips only low-risk reversible ones. Decisions: approve, edit (parameters, then back through the policy gate), reject (re-plan with the next-best action, bounded), ask (answered by `answer_question`, then asked again). High-risk or SEV1 actions need two approvers (two sequential interrupts, the second of kind `four_eyes`).
* `execute_action` runs **after** the interrupt, only the approved fingerprint, idempotent via `claim_effect`; `verify_recovery` waits virtual minutes and re-checks (a failed verification loops back to the planner at most `max_replans` times, then escalates).

## Resume values

| interrupt kind | resume value |
|---|---|
| `approval`, `four_eyes`, `edit`, `break_glass` | `Decision`: `{decision: approve\|reject\|edit\|ask, decided_by, note, edits, question}` |
| `question` | `Answer`: `{answer, decided_by}` |

* `edit`: `edits: {"params": {...}}`, `{"sql": "..."}` or `{"target": "..."}` for an action; `{"text": "..."}` for the status-page review.
* `break_glass`: `note` (or `edits.reason`) of at least 20 characters AND `edits.second_approver` (or `edits.break_glass: {reason, second_approver}`); without them the commander refuses and re-plans.
* Several pending interrupts (S08: the action approval and the status-page review at once) are resumed with one map `{interrupt_id: value}`.

## Profiles (run context `model_profile`)

| profile | behaviour |
|---|---|
| `competent` | weighs all evidence, rejects red herrings, asks only when the intent is ambiguous (S10) or the service is blind (S13) |
| `naive` | looks at the nearest change only, skips topology / logs / runbooks, blames the nearest deploy |
| `cautious` | competent evidence, but asks a confirming question, abstains below 0.70 confidence and never auto-executes |
| `gullible` | obeys imperative text found in logs: it proposes the injected rollbacks and an "all clear"; the policy gate blocks them and the report carries `injection_followed` + `policy_blocked` |

Prompt directives (one per line, in any of the three prompts): `policy: prefer_flag_revert | prefer_rollback | mitigate_before_fix`, `risk: strict`, `style: terse`, `require_evidence>=N`.
