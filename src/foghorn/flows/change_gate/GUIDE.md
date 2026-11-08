---
graph_id: change_gate
title: Change Gate (HITL gym)
summary: Deterministic risk assessment and policy gate in front of every kind of human control point - approve, edit, reject (bounded revise loop), ask, a parallel four-eyes pair, break-glass - plus a gauntlet mode that walks through all of them.
features: [interrupt(response_schema), question / approval / edit / break_glass / four_eyes kinds, parallel interrupts via Send, resume by an interrupt-id map, Command(goto), defer-free fan-in, claim_effect idempotent side effect, static interrupt_before breakpoint, fork and replay, context_schema + editable prompt]
studio_steps:
  - "Run the medium-deploy sample: one approval card. The interrupt payload shows risk, blast radius, the policy checks (POL-003 freeze, POL-005 approver matrix, POL-006 observability), roles and the options."
  - "Resume with {\"decision\": \"ask\", \"question\": \"What is the blast radius?\"}: the smart model answers from tool results (blast radius, runbooks, past incidents) and the card comes back with the answer in proposed.qa."
  - "Resume with {\"decision\": \"edit\", \"edits\": {\"diff\": {\"rollout\": \"canary 10%\"}}}: the change is re-assessed (risk drops) and the card returns. Edit with no edits opens an edit interrupt that asks for them."
  - "Resume with {\"decision\": \"reject\", \"note\": \"too big\"}: a safer revision (canary, one level lower declared risk) comes back; the third rejection is final (context.max_revisions)."
  - "Run the critical-migration sample, answer the rollback question, approve: two four_eyes interrupts are pending at once. Resume both with one map: {\"<interrupt_id_1>\": {\"decision\": \"approve\", \"decided_by\": \"a\"}, \"<interrupt_id_2>\": {\"decision\": \"approve\", \"decided_by\": \"b\"}}."
  - "Run the gauntlet sample and follow the card titles (still to complete: ask, edit, reject): an early approve is refused with a hint."
  - "Breakpoint: in the Interrupt dropdown choose apply_change (or build_graph(breakpoint=True)). After the approval the run pauses BEFORE the side effect; Continue applies it once."
  - "Time travel: fork the first checkpoint, edit change.diff in the state and run - the card is re-shown for the edited change. Replaying past apply_change returns status duplicate (claim_effect)."
sample_inputs:
  - name: "Medium deploy: one approval card (approve / edit / reject / ask)"
    input: {"change": {"id": "CHG-1001", "kind": "deploy", "component": "checkout-service", "description": "Deploy checkout 2026.11.05.2 (14 commits)", "risk": "medium", "requested_by": "a.dev", "environment": "prod", "diff": {"rollback_plan": "redeploy 2026.11.04.9"}}}
  - name: "Gauntlet: walks through question, ask, edit, reject, approve and the four-eyes pair"
    input: {"change": {"id": "CHG-1001", "kind": "deploy", "component": "checkout-service", "description": "Deploy checkout 2026.11.05.2 (14 commits)", "risk": "medium", "requested_by": "a.dev", "environment": "prod", "diff": {"rollback_plan": "redeploy 2026.11.04.9"}}, "mode": "gauntlet"}
  - name: "Critical migration on a tier-0 datastore: question, approval, then two parallel four-eyes reviewers"
    input: {"change": {"id": "CHG-1002", "kind": "migration", "component": "orders-db", "description": "Add column orders.region", "risk": "high", "requested_by": "d.dba", "environment": "prod"}}
  - name: "Peak-trading freeze: tier-0 deploy needs two approvers"
    input: {"change": {"id": "CHG-1003", "kind": "deploy", "component": "checkout-service", "description": "Deploy checkout 2026.11.05.2 (14 commits)", "risk": "medium", "requested_by": "a.dev", "environment": "prod", "diff": {"rollback_plan": "redeploy 2026.11.04.9"}}, "requested_at": "2026-11-05T14:00:00Z"}
  - name: "Break-glass: datastore migration inside the freeze"
    input: {"change": {"id": "CHG-1004", "kind": "migration", "component": "orders-db", "description": "Add index on orders(created_at)", "risk": "medium", "requested_by": "d.dba", "environment": "prod", "diff": {"rollback_plan": "drop the index"}}, "requested_at": "2026-11-05T14:00:00Z"}
  - name: "Low-risk flag flip: auto-approved by policy, no interrupt"
    input: {"change": {"id": "CHG-1005", "kind": "flag", "component": "recommendation-service", "description": "Enable new_ranker for 5% of sessions", "risk": "low", "requested_by": "a.dev", "environment": "staging"}}
---
# change_gate

Input is `ChangeGateInput {change, mode}` plus optional `requested_at` (virtual time, drives the freeze), `scenario_id`, `incident_id`.
The dispatcher routes `change.requested` here.

```
assess_risk -> policy_gate --blocked--------------------------------------------------------> finish
                  |-- clarify (question) -> assess_risk
                  |-- break_glass (freeze + tier-0 datastore) -> request_approval
                  |-- auto (low risk) ------------------------------------> apply_change -> finish
                  `-- request_approval --approve (1 approver)---------------> apply_change
                         |-- ask ----> answer_question -> request_approval
                         |-- edit ---> edit_change (edit interrupt if no edits) -> assess_risk
                         |-- reject -> revise_change (bounded) -> assess_risk | finish
                         `-- approve (critical) -> four_eyes --Send x2--> reviewer (parallel interrupts) -> collect_reviews -> apply_change | revise_change
```

## Risk and policy (`flows/change_gate/policy.py`)
Score = kind (flag 10, scale 15, deploy 20, config 20, migration 35) + tier-0 20 / tier-1 8 + datastore 10 + prod 10 + 2 per dependent (cap 12) + freeze 10 + 10 without a rollback plan - 10 for a
canary. Tiers: low < 35 <= medium < 55 <= high < 75 <= critical; the declared risk is a floor. The approver matrix is read from the POL-005 document
(low 0, medium 1, high 2 in turn, critical = four-eyes; a datastore swaps the incident commander for a dba). `approval_policy=strict` adds a level.
POL-003: inside Mon-Fri 09:00-18:00 UTC tier-0/1 changes need two approvers; a tier-0 datastore migration/scale needs break-glass (reason >= 20 characters); rollbacks are exempt.

## Interrupt kinds exercised
`question` (rollback plan, schema Answer), `approval` (approve / edit / reject / ask, schema Decision), `edit` (change parameters), `break_glass`, `four_eyes` (two simultaneous interrupts,
approve / reject). Every interrupt is preceded by `approval.requested` (with `thread_id`) and followed by `approval.decided`; ids are deterministic (`apr_<change>_<turn>`).

## Side effect
`apply_change` is its own node after the last interrupt. It claims `change:<id>` with `claim_effect` first: a double resume, a second thread or a replay past it is a `duplicate`
and emits nothing. The effect itself is a simulation (`action.executed`).

## Studio-editable
Context `answer_prompt` (gear on `answer_question`), `max_questions`, `max_edits`, `max_revisions`, `approval_policy`, `model_profile` (competent / naive / cautious answers).
