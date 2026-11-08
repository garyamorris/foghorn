---
graph_id: postmortem_pipeline
title: Postmortem pipeline
summary: A Functional API workflow (@entrypoint + @task) that drafts a blameless postmortem from a closed incident, reviews it with a critic, waits for a human decision and publishes it to the Store and the event bus.
features:
  - "Functional API: @entrypoint / @task"
  - previous + entrypoint.final (revision history per thread)
  - task retries with explicit retry_on and TimeoutPolicy
  - interrupt(response_schema=Decision) before any side effect
  - bounded draft-review-revise loop (<= 2)
  - token streaming of the summary + custom progress/artifact/decision/note events
  - context_schema with editable prompts on an entrypoint
studio_steps:
  - "Open the graph selector and pick postmortem_pipeline. Studio draws a Functional API workflow as ONE node (it is named after the function); the value is in the run trace, not in the graph picture."
  - "The input form is generated from the single TypedDict parameter (incident_id, scenario_id, optional source_thread_id and report). Pick a sample input below and Submit."
  - "Watch the stream: custom events (progress, artifact, decision, note) and the tokens of the executive summary. Each @task shows up as an `updates` entry named after the task: load_incident, build_timeline, draft, review, draft, review."
  - "The run stops at the interrupt. Read proposed.markdown (the full draft) and proposed.open_issues. Resume with {\"decision\": \"approve\"}, with {\"decision\": \"edit\", \"edits\": {\"summary\": \"...\"}} or with {\"decision\": \"reject\", \"note\": \"...\"}."
  - "Submit the same input again on the SAME thread: `previous` carries the revision history, the run is revision 2, and a rejection note from run 1 appears in the new draft under \"What went badly\"."
  - "Open the Memory viewer (or /store/items/search) in namespace postmortems/<component>: the published item has the markdown-ready postmortem, the revision history and provenance."
  - "Manage Assistants: duplicate the assistant and edit draft_prompt or review_prompt (they carry langgraph_nodes metadata; whether Studio puts the gear icon on the single functional node is UNVERIFIED in the UI, the Context form always works)."
sample_inputs:
  - name: S01 bad deploy, report handed over in the input
    input:
      incident_id: INC-9001
      scenario_id: S01
      report:
        scenario_id: S01
        incident_id: INC-9001
        severity: SEV2
        root_cause: {component: checkout-service, category: bad_deploy, confidence: 0.93, evidence_refs: ["change:chg-check-9247", "metric:checkout-service/error_rate"]}
        abstained: false
        hypotheses: []
        evidence_refs: []
        actions_proposed: []
        actions_executed: [{action: rollback_deploy, target: checkout-service, params: {}, t_virtual: "2026-11-05T14:10:00Z", outcome: executed}]
        recovered: true
        approvals: []
        interrupts_seen: [approval]
        comms_draft: ""
        memory_recalled: []
        memory_written: []
        flags: []
        tool_calls_used: 7
        supervisor_hops: 4
        narrative: ""
  - name: Read the commander thread (what the dispatcher sends)
    input: {incident_id: INC-0007, scenario_id: S01}
  - name: S13 telemetry gap, commander abstained
    input:
      incident_id: INC-9013
      scenario_id: S13
      report:
        scenario_id: S13
        incident_id: INC-9013
        severity: SEV2
        root_cause: null
        abstained: true
        hypotheses: []
        evidence_refs: []
        actions_proposed: []
        actions_executed: []
        recovered: null
        approvals: []
        interrupts_seen: []
        comms_draft: ""
        memory_recalled: []
        memory_written: []
        flags: [abstained]
        tool_calls_used: 5
        supervisor_hops: 3
        narrative: ""
---
## What it does

```
load_incident -> build_timeline -> draft -> review -> (revise, at most 2 rounds) -> HUMAN REVIEW -> publish
```

* **load_incident** reads the commander thread (`det_thread_id("incident_commander", incident_id)`, or the dispatcher's
  `source_thread_id`) through the in-process SDK client: `threads.get_state(...)["values"]["report"]`. If that fails it
  falls back to the `report` in the input; with neither, the draft honestly says the root cause is not determined.
* **build_timeline** uses the pure tools API (`list_alerts`, `list_changes`, `get_oncall`, `blast_radius`) fenced by the
  incident's `as_of` (never the future), keeps only alerts on the incident's own services (flapping probes elsewhere are
  dropped) and returns the *facts bundle*. It never touches ground truth: if the commander was wrong the postmortem says
  what the commander concluded, and the human reviewer is the safety net.
* **draft** (`smart` role): a structured `PostmortemBody` (tagged `nostream`) and then the executive summary, whose tokens
  stream to the client. Action-item owners come from the on-call data.
* **review** (`judge` role) returns issues; the deterministic checks in `checks.py` (root cause names the commander's
  component, every key timeline ref present, impact quantified, every action item owned by someone on call, blameless
  tone) always run too, so a critic can never wave through a draft that fails them.
* The revise loop is bounded by `MAX_REVISIONS = 2` (the `max_revisions` context value can only lower it).
* **Human review**: `interrupt(build_interrupt("edit", ...), response_schema=Decision)`. `approve` publishes, `edit` merges
  `edits` (any of title, summary, impact, root_cause, detection, contributing_factors, what_went_well, what_went_badly,
  action_items, lessons_learned, timeline; each key validated on its own) then publishes, `reject` publishes nothing and
  keeps the note for the next draft. `ask` is not supported here and behaves like a skipped run.
* **publish** runs AFTER the interrupt: Store upsert `("postmortems", component)` key `pm-<incident_id>` and the lifecycle
  event `postmortem.published` (idempotency key `postmortem.published:<incident>:r<revision>`), whose payload carries the
  whole postmortem so `memory_keeper` can learn from it without a Store read.

## Offline behaviour (no key, no network)

The model scripts compute text from the facts in the prompt (`scripts.py`). With `model_profile=competent` the first draft
forgets the action-item owners, the critic says so, and the revise pass fixes it (the loop converges after one round).
With `naive` the model is deaf to feedback, so the loop runs to its bound and the human sees the open issues in
`proposed.open_issues`. Switch profile with the run context (`{"model_profile": "naive"}`).

## Functional API facts worth knowing

* **One node in Studio.** The input is ONE TypedDict parameter (`PostmortemRunInput`) so the form is an object, not a bare
  array.
* **`from __future__ import annotations` must not be used in `workflow.py`**: `@entrypoint` reads the first parameter's
  annotation at decoration time to build the schema.
* **`context_schema` works on an entrypoint** in langgraph 1.2.20 (`get_context_jsonschema()` is populated and
  `runtime.context` reaches the function), so the prompts are editable in the assistant form.
* On resume the entrypoint function runs again from line 1; finished tasks are replayed from the checkpoint, so only the code
  after the interrupt does new work. Emitted events carry deterministic keys for that reason.
* **A resume sent to a thread that already finished is remembered and consumed by the NEXT run's first `interrupt()`**
  (verified probe). The server-side API rejects such stale resumes (`409 stale_interrupt`); if you drive the graph
  directly, answer interrupts with an `{interrupt_id: value}` map, whose stale ids are ignored.
* `previous` is per thread; the thread id is `det_thread_id("postmortem_pipeline", incident_id)`, so every run for an
  incident extends one revision history.
