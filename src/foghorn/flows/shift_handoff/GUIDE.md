---
graph_id: shift_handoff
title: Shift handoff
summary: A deterministic, durable, LLM-free pipeline that writes the shift report (open incidents, pending approvals, KPIs, findings). Built to show crash recovery from the last checkpoint, time travel, breakpoints and the tasks/checkpoints streams.
features:
  - durable execution (durability="sync") and crash recovery from the last checkpoint
  - idempotent nodes + a crash-once switch (crash_after) recorded in the Store
  - tasks / checkpoints / updates / custom stream modes
  - get_state_history, fork with update_state(as_node), Re-run from checkpoint
  - static breakpoint (interrupt_before publish)
  - DeltaChannel audit log (reducer-backed, beta)
  - native server cron (seeded disabled) and manual runs
studio_steps:
  - "Open shift_handoff. The graph is a straight line: collect_incidents, collect_approvals, compute_metrics, reconcile, render_report, publish. No model is involved, so every run is reproducible."
  - "CRASH DEMO. Pick the sample input 'Crash in reconcile' (crash_after: reconcile) and submit. The run FAILS in reconcile with SimulatedCrash and the thread status becomes error. Open the thread history: the last checkpoint is the one after compute_metrics (next = reconcile); the audit list in the state shows the three finished nodes only."
  - "RECOVER. Re-run from that last checkpoint (Studio: the checkpoint's 'Re-run from here' / retry action; SDK: runs.wait(thread_id, 'shift_handoff', input=None, durability='sync')). The run completes. reconcile ran TWICE (the crash and the retry), every other node once: see result.metrics / output.handoff.executions. The crash is on record in the Store (namespace effects, key crash:<thread>:reconcile) so it does not fire again. The Studio button labels are UNVERIFIED in the UI; the SDK/HTTP call is verified against langgraph dev."
  - "TIME TRAVEL. In the history pick the checkpoint whose next is publish, edit report_md in the state editor, save it as render_report output and run on: the published handoff contains your edit (a fork on a new branch; the original branch stays in the history)."
  - "BREAKPOINT. Open the interrupt/breakpoint selector, tick publish and run: the thread pauses with next = publish and nothing is stored yet. Continue to publish."
  - "STREAMS. Run with stream modes tasks + checkpoints + updates + custom: 12 task events (start and finish per node), 8 checkpoints, 6 updates and the progress/metric/note/artifact custom events."
  - "CRON. The events layer seeds a DISABLED native cron for this graph (input {window_hours: 8}); enable it from the System view. Virtual-time ticks (system.cron.tick job shift_handoff) route here too."
sample_inputs:
  - name: Handoff for the last 8 hours (uses the data layer's default shift end)
    input: {window_hours: 8}
  - name: Crash in reconcile (crash-once demo)
    input: {window_hours: 8, crash_after: reconcile}
  - name: Crash in publish (nothing half-published)
    input: {window_hours: 8, crash_after: publish}
  - name: A specific shift end, short window
    input: {shift_end: "2026-11-05T16:00:00Z", window_hours: 2}
---
## What it does

```
collect_incidents -> collect_approvals -> compute_metrics -> reconcile -> render_report -> publish
```

| node | reads | writes (flat state) |
|---|---|---|
| `collect_incidents` | commander threads via `threads.search(metadata={"foghorn_kind": "incident"})`; the data layer's alert history | `shift_end`, `window_hours`, `window_start`, `source`, `incidents`, `alert_history` |
| `collect_approvals` | `threads.search(status="interrupted")`, one row per pending interrupt | `approvals` |
| `compute_metrics` | state only | `metrics` (open by severity, resolved, MTTR, oldest open, pending approvals, alerts, ...) |
| `reconcile` | state only | `findings` (stale approvals, failed commander runs, long-open SEV1, alerts with no incident) |
| `render_report` | state only | `report_md` |
| `publish` | state only | Store `("handoffs",)` upsert, event `handoff.published`, `handoff`, `result` |

* **Never empty.** The native cron sends only `window_hours`, so `shift_end` falls back to the data layer (latest scenario
  trigger rounded up to the hour). When the server knows no incident threads the report is built from the data layer's
  alert history (`Incident source: data_layer`).
* **Idempotent.** Every node's update is a pure function of its input state plus the sources. The only side effects are in
  `publish`: an upsert keyed `handoff-<shift_end>-<hours>h` and an event with idempotency key `handoff.published:<id>`. A
  second run for the same shift returns `status: duplicate`.
* **`crash_after=<node>`** makes that node compute its update and then raise `SimulatedCrash` ONCE per (thread, node). For
  `publish` the crash happens before the side effects. Always run it with `durability="sync"` so the checkpoint before the
  failed node is certainly stored.
* **`audit`** is a `DeltaChannel` (beta) with a batching-invariant reducer (`append_audit`): checkpoints store only the delta of
  each step and `get_state_history` rebuilds the list (verified in-process and on `langgraph dev`: 0, 0, 1, 2, ... entries
  per checkpoint). `update_state({"audit": [...]})` appends; `Overwrite([...])` replaces. If a future langgraph release
  changes the channel, swap `DeltaChannel(append_audit)` for `Annotated[list, append_audit_reducer]` in `state.py`.

## Crash-recovery demo over the SDK / HTTP API (verified against `langgraph dev`)

```python
from langgraph_sdk import get_client

c = get_client(url="http://127.0.0.1:2026")
t = await c.threads.create()
tid = t["thread_id"]
# 1. fails in reconcile; raise_error=False returns {"__error__": ...} instead of raising
await c.runs.wait(
    tid, "shift_handoff", input={"window_hours": 8, "crash_after": "reconcile"}, durability="sync", raise_error=False
)
(await c.threads.get(tid))["status"]  # "error"
st = await c.threads.get_state(tid)
st["next"]  # ["reconcile"]; audit has 3 entries
await c.threads.get_history(tid)  # checkpoint steps 3, 2, 1, 0, -1 (newest first)
# 2. resume from the last checkpoint: input=None
out = await c.runs.wait(tid, "shift_handoff", input=None, durability="sync")
out["handoff"]["executions"]  # {'collect_incidents': 1, ..., 'reconcile': 2, 'render_report': 1, 'publish': 1}
(await c.threads.get(tid))["status"]  # "idle"
```

Fork before `publish`: take the history entry with `next == ["publish"]`, then
`c.threads.update_state(tid, {"report_md": "..."}, as_node="render_report", checkpoint=entry["checkpoint"])` and
`c.runs.wait(tid, "shift_handoff", input=None, checkpoint=updated["checkpoint"])`. Static breakpoint:
`c.runs.wait(tid, "shift_handoff", input=..., interrupt_before=["publish"])`, inspect, then `input=None` to continue.

## Notes

* All nodes are `async def`; the data-layer reads run in `asyncio.to_thread` (no `BlockingError`).
* Sources are injectable: `foghorn.flows.shift_handoff.ports.set_source_factory(fake)` (tests use it).
* Without a Store (bare unit runs) the crash record falls back to an in-process set and `executions` is empty.
