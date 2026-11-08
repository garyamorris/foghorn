---
graph_id: memory_keeper
title: Memory keeper
summary: Learns from published postmortems (a human curates every lesson through a memory_write interrupt) and consolidates the lesson memory on a cron. Showcases Store namespaces, semantic search and HITL on memory writes.
features:
  - Store namespaces (lessons, incidents) with provenance
  - semantic search via store.index (near-duplicate recall)
  - interrupt(response_schema=MemoryDecision) save / edit / discard
  - mode switch with conditional entry edges
  - cron consolidation, interrupt only when it would delete
  - context_schema with an editable prompt (lesson distiller)
  - memory.recalled / memory.written lifecycle and stream events
studio_steps:
  - "Open memory_keeper (xray shows two lanes: learn on the left of the conditional entry edge, consolidate on the right). The gear on node distill edits the lesson-distiller prompt (assistant context field lesson_distiller_prompt)."
  - "LEARN. Submit the sample input 'Learn from a published postmortem'. Watch dedupe: a memory.recalled event names the near-duplicate (les-004, score above 0.6) and the candidate becomes a REINFORCE; the second candidate is NEW."
  - "CURATE. The run stops at curate with a memory_write interrupt per candidate (proposed.text, proposed.action, proposed.near_duplicate). Resume with {\"decision\": \"save\"}, {\"decision\": \"edit\", \"text\": \"...\"} or {\"decision\": \"discard\"}. In the dashboard these are the Approvals inbox cards."
  - "VERIFY. Memory viewer (or POST /store/items/search with namespace_prefix [\"lessons\"] and a query): the new lesson is found by meaning (here: by shared words, the offline embedder is hashed bag-of-words), the reinforced seed shows reinforced_by and a higher confidence, and an incident summary appears in namespace incidents/<component>."
  - "RE-RUN the same input on a new thread: nothing is duplicated and nothing is reinforced twice (business keys, reinforced_by membership, version-keyed lifecycle events)."
  - "CONSOLIDATE. Submit {mode: consolidate}. It scans every lesson, finds semantic duplicates inside one component, raises confidence of reinforced lessons, flags lessons not seen for stale_days as stale and writes a report to Store namespace reports/consolidation. It interrupts ONLY if duplicates would be deleted: save merges and deletes, discard keeps them (marked duplicate_of)."
  - "SCHEDULE. The events layer seeds only the shift_handoff cron. To consolidate nightly create one yourself: client.crons.create(assistant_id, schedule='0 3 * * *', input={'mode': 'consolidate'})."
sample_inputs:
  - name: Learn from a published postmortem (reinforces les-004, adds a new lesson)
    input:
      mode: learn
      incident_id: INC-9100
      scenario_id: S06
      postmortem:
        incident_id: INC-9100
        severity: SEV2
        title: Session cache stampede
        component: sessions-cache
        category: cache_stampede
        status: approved
        summary: A cache stampede hit the sessions cache after a TTL change.
        root_cause: Cache entries expired together after a TTL change.
        lessons_learned:
          - "Database load is the symptom: look at the cache hit ratio first, and warm the cache as part of the revert"
          - "Stagger session cache TTLs with random jitter so entries never expire in the same minute"
        timeline: [{kind: action, summary: "executed revert_config on sessions-cache"}]
        window: {start: "2026-11-04T11:00:00Z", end: "2026-11-04T11:40:00Z"}
  - name: Learn from a postmortem stored by postmortem_pipeline (read by incident id)
    input: {mode: learn, incident_id: INC-9001}
  - name: Consolidate the lesson memory (what the cron sends)
    input: {mode: consolidate}
---
## What it does

```
learn:       load_source -> distill -> dedupe -> curate (memory_write interrupts) -> write -> finish
consolidate: scan -> cluster -> (confirm_merge, only when something would be deleted) -> apply -> finish
```

### learn

* **load_source**: the postmortem from the `postmortem` input (the `postmortem.published` event payload carries it) or, if
  absent, from the Store `("postmortems", *)` by `incident_id`. Nothing found: a `skipped` result, not an error.
* **distill** (`smart`, structured `Distillation`, tagged `nostream`): lesson candidates (at most `max_lessons`, default 2) and
  a two-sentence incident summary. Node retry policy with explicit `retry_on` and a `TimeoutPolicy`.
* **dedupe**: semantic recall (`foghorn.memory.api.recall`) in the component's own lesson namespace. A hit at or above
  `reinforce_threshold` (default 0.6) makes the candidate a REINFORCE of that lesson; otherwise it is NEW. Hits from other
  components are reported (`memory.recalled`) but never reinforced.
* **curate**: one `memory_write` interrupt per candidate (`MemoryDecision`). The node restarts from its first line on every
  resume, so it only asks and emits events with idempotency keys; it writes nothing.
* **write**: after the last interrupt. NEW lesson key = `les-<hash of component and normalised text>`; REINFORCE bumps
  `confidence` by 0.05, appends the incident to `reinforced_by` (a second pass with the same incident changes nothing) and
  increments `version`. The incident summary goes to `("incidents", <component>)` under key `inc-<number>`. Every item
  carries `provenance {thread_id, event_id, incident_id, written_by}`. Lifecycle events `memory.written` / `memory.recalled`
  have idempotency keys that include the item version.

### consolidate

Duplicates are found with the Store's own semantic search (score at or above `merge_threshold`, default 0.8) inside one
component namespace and merged by union-find; the survivor is the highest confidence (ties: most reinforcements, then key).
Confidence of a reinforced lesson is raised to `min(0.95, 0.5 + 0.1 x number of source incidents)` (never lowered). A lesson
is stale when its newest date (`last_reinforced_at`, `learned_from_at`, or the date of its `from_incident` in the past
incident dataset) is more than `stale_days` (default 540) before "now" (the context `as_of`, else the newest incident or lesson
date in the memory). Running it twice changes nothing the second time.

## Verified live (`langgraph dev`)

Learn: 2 interrupts, `les-004` reinforced (`reinforced_by: [INC-9100]`, confidence 0.65), a new lesson saved with the
human's edited text, incident summary written; `POST /store/items/search` with `namespace_prefix: ["lessons"]` returned the
new lesson first. Consolidate: first run boosted 1 lesson and flagged stale ones, the second run changed nothing.

## Offline behaviour

The distiller script turns the postmortem's `lessons_learned` into candidates (confidence 0.7 for an approved postmortem,
0.8 for an edited one, 0.6 for a draft). Profiles: `naive` proposes one platitude (confidence 0.4) that the human is expected to
discard; `cautious` keeps only candidates with confidence of at least 0.7.
