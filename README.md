# Foghorn

**An AI incident-command center, built as a production-grade, multi-graph LangGraph application.**

Foghorn watches a fictional e-commerce company (Brightcart). Alerts, deploys and flag changes arrive as events; a
router opens incidents; a supervisor of specialist agents investigates, finds the root cause, proposes a fix, **asks
a human to approve it**, executes it, verifies recovery, and hands off to a postmortem pipeline and a memory that
learns. Everything is a LangGraph graph you can open, trace, fork and replay in **LangGraph Studio**, and everything
is driven from a real dashboard that streams what the agents are doing, token by token.

The domain is a vehicle: the point is a deployment that uses LangGraph and Studio's advanced features for real:
ten graphs, supervisor and map-reduce patterns, subgraphs, the Functional API, human-in-the-loop in every style,
durable execution and crash recovery, long-term semantic memory, streaming in every mode, events, datasets with
ground truth, and offline evaluations that gate the build.

It runs **fully offline by default**: no API keys, no network, deterministic, free. Real Claude is opt-in.

## Quick start

```bash
uv sync                  # install (Python 3.12)
make dev                 # langgraph dev on http://127.0.0.1:2026  (offline, tracing off)
open http://127.0.0.1:2026/app/
```

* **Dashboard:** <http://127.0.0.1:2026/app/> - launch a scenario and watch it work.
* **Studio:** <https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2026> (needs a LangSmith login in the
  browser; use Chrome, or `langgraph dev --tunnel` for Safari/Brave).
* Port 2026 is used so it never collides with another project on Studio's usual 2024 (`make dev PORT=2024` to change).

Your first five minutes: open the dashboard -> **Scenarios** -> **S01** -> **Launch**. Within seconds an incident
opens; go to **Approvals**, read the card (risk, blast radius, policy checks), press **Approve**. The rollback runs,
recovery is verified, the incident resolves, and a postmortem draft waits for your review. Then click **Open in
Studio** on the incident. [docs/DEMO.md](docs/DEMO.md) has eight guided walkthroughs.

## What is inside

| Graph | What it does | LangGraph features it showcases |
|---|---|---|
| `event_router` | Single ingress: validate, dedupe, enrich, classify, open or join an incident, start the commander | `Send` fan-out, `RetryPolicy`, `CachePolicy`, Store ledger, graph-to-graph delegation |
| `incident_commander` | Supervisor of 5 specialist agents + RCA, policy check, human approval, execute, verify, comms | `Command(goto)`, subgraphs, `create_agent`, dynamic `interrupt`, breakpoints, fork, editable prompts |
| `rca_tournament` | Competing root-cause hypotheses scored in parallel | `Send` map-reduce, custom reducers, `defer`, per-task timeouts |
| `storm_correlator` | Collapses an alert storm into causal clusters | map-reduce-reduce loop, `TimeoutPolicy`, `error_handler`, `set_node_defaults` |
| `change_gate` | The human-in-the-loop gym: approve / edit / reject / ask / four-eyes / break-glass | parallel interrupts, resume-by-id, static breakpoints, idempotent side effects |
| `postmortem_pipeline` | Drafts, reviews and publishes a postmortem | **Functional API** (`@entrypoint`/`@task`), `previous`, `entrypoint.final` |
| `ops_copilot` | Chat copilot with tools, memory and tool-call review | `create_agent` + middleware stack, `ToolRuntime`, Studio **chat mode** |
| `shift_handoff` | Deterministic, durable shift report on a cron | durable execution, **crash recovery**, `tasks`/`checkpoints` streams, `DeltaChannel` |
| `memory_keeper` | Learns from closed incidents; human curates what it remembers | Store, **semantic search**, `memory_write` interrupt, cron consolidation |
| `eval_runner` | Scores the commander against ground truth, gates regressions | `Send` over scenarios, SDK delegation, LLM-as-judge |

Around the graphs: a **custom FastAPI app** (served by the same `langgraph dev` process) with the event pipeline
(webhooks, dedupe, priority queues, backpressure, dead-letter queue, replay, SSE), the **dashboard**, a
deterministic **data simulator** (20 services, 142 metric series per scenario, logs, changes, alerts, runbooks,
policies, 120 past incidents) and **16 labelled scenarios** with hidden ground truth.

## The four model profiles

The offline "model" reasons over real tool results (metric z-scores, change-vs-onset timing, log signatures,
runbooks, memory), and the profile changes *how*:

| Profile | Behaviour | core16 result |
|---|---|---|
| `competent` | weighs all the evidence, rejects red herrings | 16/16, mean 0.99 |
| `cautious` | asks questions, abstains on weak evidence, never auto-executes | 14/16 |
| `naive` | blames the most recent deploy / first alert | 2/16 |
| `gullible` | obeys instructions hidden in log text (S15 prompt injection) | caught by `policy_check`; fails the injection gate |

Pick a profile per launch in the dashboard, or per assistant in Studio. **Edit a prompt in Studio** (gear icon on a
node), save it as a new assistant version, re-run, and compare the evaluation.

## Real models (optional)

```bash
export ANTHROPIC_API_KEY=...            # or OPENAI_API_KEY
FOGHORN_PROVIDER=auto make dev          # auto: Anthropic key -> Claude, else OpenAI key -> GPT, else offline
```

Roles map to models in `src/foghorn/models/factory.py` (`fast` = Claude Haiku 5.5, `smart` = Sonnet 5.5, `judge` =
Opus 5.5; override with `FOGHORN_MODEL_FAST|SMART|JUDGE`). Tracing to LangSmith is off unless you set
`LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` (`make dev TRACE=1`). Offline behaviour is the default **on purpose**:
real calls cost money and are not deterministic. Real-model mode is not exercised by the offline test suite.

## Quality gates

```bash
make check      # ruff + mypy --strict + 1,300+ tests with a coverage gate (95%) + langgraph.json validation + data check
make e2e        # spawns a real langgraph dev and drives it with the SDK and HTTP: alert -> incident -> approval -> resolved
python -m foghorn.evals run --suite all --profile competent --check-baseline --assert-thresholds   # offline eval gate
node --test tests/ui/                   # dashboard logic tests
```

The evaluation gate scores 16 evaluators (root cause, evidence, red-herring rejection, action correctness, forbidden
action safety, recovery, interrupt conformance, injection resistance, abstention, memory use, comms quality, tool
budget, triage, retrieval, safety cases). A forbidden action executing or an obeyed injection fails the build.

## Repository map

```
src/foghorn/
  graphs/        thin Studio entry points (one file per graph; langgraph.json points here)
  flows/<id>/    each graph's implementation + GUIDE.md (Studio click-path and sample inputs)
  contracts/     the shared vocabulary: event envelope, interrupt payloads, graph inputs, thread metadata
  common/        stream events, lifecycle events, graph-to-graph delegation
  models/        provider factory + the offline model (a scriptable, stateless BaseChatModel)
  memory/        Store helpers (semantic recall, idempotent effects)
  data/ tools/   deterministic simulator and the tools agents call (time-fenced, recovery overlay)
  events/        ingest pipeline, routing.yaml, queues, dispatcher, replay, SSE, approvals, DLQ
  server/        FastAPI app (http.app) + the dashboard (server/static, no build step)
  evals/         harness, 16 evaluators, runner, baselines
datasets/        committed, regenerable datasets + MANIFEST.json     (python -m foghorn.data build --check)
docs/            ARCHITECTURE.md, API.md, STUDIO_GUIDE.md, DEMO.md, GRAPH_GUIDE.md, UI.md, design/
tests/           unit, graphs, contract, golden, evals, e2e, ui
```

## Documentation

* [Walkthrough video](https://github.com/garyamorris/foghorn/releases/tag/walkthrough-v1) - a narrated, captioned video (about 6.5 min, 1080p) that explains how
  the graphs execute, using recorded runs on a real model: the event pipeline, supervisor and subgraphs, `Send` fan-out, interrupt and resume,
  the five stream modes, the Functional API, durable crash recovery, Studio, LangSmith traces and the eval gate. The 1080p and 720p mp4 files are
  attached to that release (they are not in the git history); the `.srt`/`.vtt` captions are in [docs/demo/](docs/demo/). The narration is
  synthesised speech (OpenAI `gpt-4o-mini-tts`); how everything was made, and how to rebuild it, is in
  [docs/demo/video-source/README.md](docs/demo/video-source/README.md).
* [docs/DEMO.md](docs/DEMO.md) - eight guided walkthroughs, each proving a LangGraph / Studio feature.
* [docs/STUDIO_GUIDE.md](docs/STUDIO_GUIDE.md) - per-graph "what to click in Studio" with copy-paste sample inputs.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - how it fits together and the rules every module follows.
* [docs/API.md](docs/API.md) - every `/api/*` endpoint. [docs/GRAPH_GUIDE.md](docs/GRAPH_GUIDE.md) - how to write a graph here.
* [docs/design/](docs/design) - the research behind the design: Studio and Agent Server behaviour verified live,
  the LangGraph 1.2 feature ledger and 38 gotchas, and the product/data/event design.

## Good to know

* `langgraph dev` runs with `--no-reload` and `--n-jobs-per-worker 8` (see the Makefile): reload would reset the
  virtual clock and replay position, and the default of one worker deadlocks graph-to-graph calls.
* Replays skip quiet stretches longer than 120 virtual seconds (`FOGHORN_REPLAY_IDLE_GAP_S`), so a scenario's first
  alert arrives in seconds, not minutes.
* Docker: `langgraph dockerfile -c langgraph.json` and `langgraph build -c langgraph.uv.json` are wired (a locked,
  reproducible image config). They were generated and validated but **not built or run** in development because the
  Docker daemon was not running.
* **Studio verified in a real browser (Chrome, 2026-11-08).** Against `make dev TRACE=1`: the graph selector lists all ten
  graphs; `incident_commander` draws with its subgraph nodes (`rca_tournament` and the specialists); the input form is
  generated from `CommanderInput`; running S01 produced a thread with per-node state and an **interrupt rendered from
  its `response_schema`** (a Decision form with approve / reject / edit / ask and live validation); resuming with
  *approve* continued through `execute_action` and `verify_recovery`; and the **Trace** tab showed the full LangSmith
  trace, including model calls and the nested specialist subgraphs. Not exercised in Studio: forking, the prompt gear,
  the Memory view, and experiments. If Studio shows "Failed to fetch", see the troubleshooting section at the top of
  [docs/STUDIO_GUIDE.md](docs/STUDIO_GUIDE.md) (Chrome's Local Network Access prompt; the author's embedded browser
  pane blocked these calls with `ERR_BLOCKED_BY_CLIENT`).
