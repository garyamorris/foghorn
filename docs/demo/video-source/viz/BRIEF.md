# Foghorn walkthrough video: the animated LangGraph visuals (brief shared by every viz page)

We are making a ~5 minute explainer video for developer friends: "how Foghorn (a 10-graph LangGraph incident-command app) executes
in LangGraph". An earlier video (for a different app, "Probably a Tragedy") exists; its page sources were copied into this
folder as REFERENCE ONLY: `lg.js` (graph layout/drawing engine fed by `GET /assistants/{id}/graph?xray=true`), `live.js`,
`live_graph.html`, `overview.js/html`, `trace.js/html`, `viz.css`. They are tied to that game: reuse ideas and the layout code
where it helps, but build pages for Foghorn. The user's complaints about the previous video, which this one must fix:
"show the animation of the running application", "more visualisation of the LangGraph, and if possible showing the LangGraph
running, always make sure the graph is clear and the traces readable".

## Rendering environment (important)
* Pages are loaded by `docs/demo/video-source/rec/rec` = an offscreen WKWebView (Safari engine) recorder: viewport **1280x720 CSS px**
  (captured at 1920x1080, devicePixelRatio 1.5), real-time 30 fps wall clock. So: plain ES modules, SVG + CSS animation +
  `requestAnimationFrame`, **no network, no CDN, no libraries, no build step**. Everything relative to the page; load data with
  `fetch()` from the same folder tree. Serve the folder with `python3 -m http.server 8765 --directory
  docs/demo/video-source` (from the repo root) and open `http://127.0.0.1:8765/viz/<page>.html?...` (not `file://`).
* Screenshot a page at time T (use this constantly to check your work, then LOOK at the PNG with the Read tool):
  `rec/rec --url 'http://127.0.0.1:8765/viz/replay.html?...' --shot work/x.png --after 6`
  Record a clip: `rec/rec --url URL --out work/x.mp4 --director viz/director_wait.js --max-seconds 90` (director_wait.js waits for
  `window.__done === true` then calls `rec.done()`; look at it and `rec/README.md`/`rec/director_lib.js` for the API). Extract frames
  from a clip: `rec/vtool frame work/x.mp4 <seconds> work/f.png 1280`.
  Another Claude session on this machine also uses `rec`; only touch your own files; use ports 8765-8769 for your http.server.
* House style: the Foghorn dashboard's dark theme (see `src/foghorn/server/static/css/tokens.css`), mono for ids, **text never
  smaller than 15 CSS px on screen**, strong contrast, generous spacing, no clutter. Titles/captions inside the page are small
  helper labels only; the narration and subtitles are added later by the video editor, so leave the bottom ~110 px of the frame
  free of important content (subtitle safe area) unless told otherwise.
* Every page must: set `window.__done = true` when its animation finishes; expose `window.__meta` (duration in seconds and a list
  of named beats with times) so the editor can align narration; accept `?seek=<seconds>` to render a still frame of the animation
  at that time and stop (for screenshots); accept `?fit=<seconds>` to warp the whole animation to a target duration.

## The data: REAL recorded runs
`work/rehearsal_cap/` (offline model, used for development; the final capture from a real OpenAI-model run will have the same
shape: `work/real_cap/`). Produced by `tap.py`, which joins every run's SSE stream on a LangGraph Agent Server.
* `structure/<graph_id>.json`: `{graph_id, assistant_id, description, graph:{nodes:[{id,...}], edges:[{source,target,conditional?,data?}]}}`
  from `GET /assistants/{id}/graph?xray=true`. 10 graphs: event_router, incident_commander, rca_tournament, storm_correlator,
  change_gate, postmortem_pipeline (Functional API: only 3 nodes; its `@task`s appear only as `tasks` events), ops_copilot,
  shift_handoff, memory_keeper, eval_runner. With xray, subgraph nodes appear as `parent:child` ids
  (e.g. `metrics_analyst:model`, `metrics_analyst:tools`, `rca_tournament:score_hypothesis`).
* `events.jsonl`: one JSON per line `{t, thread_id, run_id, graph_id, event, data}`. `t` = seconds since the tap started
  (wall clock). `event` is the SSE event name, optionally with a subgraph namespace after `|`
  (e.g. `tasks|metrics_analyst:f24917c7-...`, `custom|rca_tournament:<uuid>`). Modes present:
  * `tasks`: `{id,name,input,triggers}` when a node/task starts, `{id,name,error,result,interrupts}` when it finishes.
    This is the authoritative node-execution signal (including parallel `Send` instances: several tasks with the same `name`).
  * `updates`: `{node_name: state_update}`; a human pause shows as `{"__interrupt__":[{"value":{...interrupt payload...},"id":...}]}`.
  * `values`: full state after each superstep (large; show a few keys only).
  * `custom`: structured app events `{v,kind,key,agent,t_virtual,data}`; kinds: `progress`, `handoff`, `evidence.found`,
    `hypothesis.update`, `policy.check`, `plan.proposed`, `decision`, `memory.recalled`, `memory.written`, `artifact`, `series`, `metric`, `note`.
  * `messages`: `[AIMessageChunk{content,tool_calls,...}, {metadata: langgraph_node, ...}]` token chunks of the LLM narration.
  * `checkpoints`: StateSnapshot-like dict (checkpoint id, parent, next, created_at) per superstep.
  * `metadata`: `{run_id, attempt}` first event of each run; a thread's second run (a later `run_id`) is the run that **resumed after an
    interrupt** (`Command(resume=...)`).
* `threads.json`: threads with graph ids; `event_router` has many threads (one per ingested event); the commander thread has two runs
  (before/after the human approval); `postmortem_pipeline` also pauses for human review.
* Dev helper: `python3 -c` one-liners are fine; examples of what the data looks like are in `tap.py`'s docstring and the files themselves.

## What "good" looks like
A viewer who has never used LangGraph should be able to follow: which node is running now, what ran before, what is running in
parallel (Send fan-out), where the graph paused for a human (interrupt + checkpoint), where it resumed, what state changed.
The graph must be clearly laid out (no overlapping labels or crossing mess), nodes labelled with real node names, edges real.
Keep animation purposeful: highlight, pulse, count badges, short-lived trails; nothing flickers.
