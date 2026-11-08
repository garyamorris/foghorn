# Eight guided walkthroughs

Start the server once (`make dev`), open the dashboard at <http://127.0.0.1:2026/app/> and Studio at
<https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2026>. Each walkthrough names the LangGraph or Studio
feature it proves. Per-graph click-paths and copy-paste inputs for Studio are in [STUDIO_GUIDE.md](STUDIO_GUIDE.md).

Tip: the launcher's **Token delay** slider (15-25 ms) makes streaming visible; 0 is instant. The replay skips quiet
stretches automatically, so a scenario's first alert arrives in seconds.

---

## 1. First page: alert to resolution (about 5 min)
**Proves:** event pipeline, graph-to-graph delegation, supervisor + subgraphs, human-in-the-loop, durable resume, the
whole chain of lifecycle events.

1. **Scenarios -> S01 (Checkout 500s after the 14:02 deploy) -> Launch** (profile `competent`, policy `standard`).
2. Watch **Mission Control**: events stream in; `triage.completed`, then `incident.opened`.
3. Open the incident (**Incidents -> INC-...**). The war-room stream narrates token by token: topology, change,
   metrics, logs and knowledge specialists each get a lane with their tool calls; the **hypothesis board** converges on
   `bad_deploy on checkout-service` (about 0.88) and the sparklines appear as agents query metrics.
4. **Approvals**: read the card (risk, blast radius, policy checks incl. "action derived from evidence, not from
   log text"), press **Approve** (`a`).
5. The rollback executes, recovery is **verified**, the incident resolves; the **postmortem pipeline** starts and
   waits for your review (a second approval card).
6. **Open in Studio** on the incident: the thread, its checkpoints and the subgraph structure.

## 2. The red herring, a question, and a fork (about 10 min)
**Proves:** `interrupt` with a `question`, state editing, **fork and re-run from a checkpoint** (time travel).

1. Launch **S10** as `competent`: a recent deploy looks guilty but a flag change is the real cause. The commander
   asks a clarifying **question** (answer it in Approvals), ranks `config_change on pricing-service` first (about
   0.93), and recalls the matching past incident INC-0061 from memory.
2. Launch **S10** again as `naive`: it blames the decoy deploy. Compare the two runs (**Scenarios -> Compare runs**).
3. In Studio (or the dashboard's **Threads / Time travel**): open the competent thread, pick a checkpoint after the `rca_tournament`
   step, **edit `hypotheses[0]`**, fork, and re-run: the plan changes.

## 3. Alert storm, dedupe and the dead-letter queue (about 8 min)
**Proves:** map-reduce (`Send`), reducers, timeouts and error handlers, event idempotency, backpressure, DLQ.

1. Launch **S12** (60 alerts, zone partition): `storm_correlator` fans out one branch per candidate root and merges
   them into **one cluster, one incident** instead of 60.
2. **Mission Control -> Inject event -> Poison alert** goes to the **DLQ** after three attempts; retry or discard it.
3. Post the same alert twice (same `Idempotency-Key`): the second is `duplicate`. Fire a burst to see queue depth and
   429 backpressure.

## 4. The human-in-the-loop gym (about 12 min)
**Proves:** every interrupt style, **parallel interrupts**, resume-by-id, static breakpoints, idempotent effects.

In Studio, run **`change_gate`** with the *Gauntlet* sample input and answer each card: approve / edit (with changes) /
reject (watch the bounded revise loop) / ask a question (the answer streams) / **four-eyes** (two reviewers pending
at once - resume both with one `{interrupt_id: decision}` map). In the **Interrupt** dropdown choose `apply_change`:
the run stops *before* the side effect; Continue applies it exactly once (replaying past it is a no-op).

## 5. Teach it once (about 8 min)
**Proves:** long-term **Store** memory with **semantic search**, a human-curated `memory_write` interrupt, learning loop.

1. Resolve S01 (walkthrough 1), approve the postmortem: `postmortem.published` triggers `memory_keeper`.
2. **Approvals**: a `memory_write` card shows the lesson it wants to remember; save, edit or discard it.
3. **Memory** view (or Studio's long-term memory): search "roll back only the component that changed" - the new
   lesson ranks first. Launch **S16** (a recurring incident): the commander recalls it at `bootstrap`.

## 6. Edit the prompt, prove it with evals (about 10 min)
**Proves:** assistants and context, prompt editing in Studio, the evaluation harness and its hard safety gates.

1. **Evaluations -> Run core16 as `competent`**: 16/16, mean about 0.99. Run it as `naive` (2/16) and `gullible` (the
   S15 prompt-injection case trips the injection gate: it *followed* the hostile log line, and `policy_check`
   stopped it - nothing forbidden executed).
2. In Studio: **Manage Assistants**, click the gear on the `supervisor` node, edit the prompt, save as a new version,
   re-run S01 and compare. (Offline, behaviour is scripted; with `FOGHORN_PROVIDER=auto` the prompt changes real behaviour.)
3. CLI: `python -m foghorn.evals run --suite all --profile competent --check-baseline --assert-thresholds`.

## 7. Ask the copilot (about 6 min)
**Proves:** `create_agent` with a middleware stack, tools, tool-call review, memory, Studio **chat mode**.

Open the copilot (top-bar chat button, or `ops_copilot` in Studio's chat mode). Ask *"why is checkout slow?"*,
*"what changed in the last hour?"*, *"should I roll back?"* - it calls real tools and narrates the results. Ask it to
`propose_action`: a **tool-review** interrupt appears (approve / edit / reject / ask). Tell it a preference
("I want status updates drafted for SEV2") and recall it from a different thread.

## 8. Break glass and crash recovery (about 10 min)
**Proves:** durable execution, crash recovery from a checkpoint, breakpoints, policy guardrails.

1. **Crash recovery:** run **`shift_handoff`** with input `{"window_hours": 8, "crash_after": "reconcile"}`. The run
   fails; the thread keeps its checkpoints. Re-run from the checkpoint before `reconcile` (Studio) or
   `runs.wait(tid, "shift_handoff", input=None, durability="sync")` (SDK): it completes and the audit log shows
   `reconcile` executed twice and every other node once.
2. **Break glass:** launch **S14** (a tempting restart during a change freeze). The commander proposes it; the policy
   gate demands a break-glass approval with a written reason of 20+ characters and a second approver.
3. **Breakpoints:** launch any scenario with `approval_gate` ticked under *Breakpoints*; edit the state while paused.

---

### If something looks stuck
`make dev` must be running with `--n-jobs-per-worker 8`; check **System** in the dashboard (all components should be
green) and `/api/health`. Finished runs keep a replayable stream for two hours (`RESUMABLE_STREAM_TTL_SECONDS`).
