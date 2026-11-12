# trace.html: one real Foghorn run as an animated LangSmith trace

Files: `viz/trace.html` (+ `trace.js`), data `viz/trace_data_hero.json` (32 KB), fetcher `viz/make_trace_data.py`.
Extra dataset `viz/trace_data_five.json` (see "Other data" below).

## Data (real, read-only from LangSmith)
Project `foghorn-video`, hero thread `22b4ce55-d28a-5cfd-a521-daa1c1761802` (`incident_commander`, INC-33721). Two LangSmith traces carry
`metadata.thread_id`: trace 1 `01a1bd42` (91 runs, 26.9 s, ends `interrupted` at `approval_gate`) and trace 2 `01a1bd43` (7 runs, 1.74 s,
the resume). Human wait between them: 31.7 s. Thread totals: 98 runs, 16 LLM calls, 18,978 tokens, $0.0512, 28.7 s of compute.

    cd /Users/garyamorris/Developer/repos/langgraph-app
    .venv/bin/python -I docs/demo/video-source/viz/make_trace_data.py            # needs LANGSMITH_API_KEY in the env (never printed/written)
    .venv/bin/python -I docs/demo/video-source/viz/make_trace_data.py --thread <thread_id> --out <file.json>

Two API calls per thread (root runs by `metadata.thread_id`, then `list_runs(trace_id=...)` per trace; the SDK returns inputs/outputs).
Everything on the page comes from that: run names/types, start/end offsets, token counts, cost, model, tool args/results, prompt and reply
excerpts, the interrupt payload (`GraphInterrupt` error text) and the resume decision (`approval_gate` outputs in trace 2). The script
folds noise so the page only draws what matters: `Analyst.*` middleware, `RunnableSequence`, `_check_refusal`, `PydanticToolsParser` are
folded into their parent; `X > X` subgraph wrappers show once; `model > Analyst.awrap_model_call > ChatOpenAI` is one LLM row `model`;
`tools > Analyst.awrap_tool_call > tool` is the tool row and parallel calls of one tool become `query_metric x4`. It prints a check
(sum of LLM-run tokens == root-run tokens) and the footer on the page says "37 of 91 runs shown".
Note: in this run the supervisor routed to **3** of the 5 specialists (metrics, logs, change), so the page says "3 specialists in
parallel" (computed from the data: siblings that start together and both run > 400 ms). Model names are as traced (`gpt-6-luna` for the
routing calls with a forced `Route` tool call, `gpt-6-sol` for narration and specialists).

## Page parameters
`trace.html?data=trace_data_hero.json&fit=<seconds>&seek=<s>&focus=<name>`
* `fit=S` warps the whole animation to S seconds (`window.__meta.duration` and beat times are in output seconds). Default 29.1 s.
* `seek=S` draws one still frame at S (after the warp) and stops (`__done = true` immediately).
* `focus=<row name>[#k]` (or `thread`): opens that row's detail card (LLM / tool rows, e.g. `focus=query_metric`, `focus=ChatOpenAI#4`,
  `focus=search_logs`) and, without `seek`, shows the frame where it is on screen.
* `data=file.json` another file produced by `make_trace_data.py` (1 or 2 traces).
Recorder: under `rec` the animation clock is `rec.t()` (seconds since the recording started), so a clip starts at t=0 and is not clipped
by the page-load/settle delay. `window.__meta = {duration, default_duration, warp, beats:[{t,label}], totals:{...}, thread_id, traces}` is
set before the animation starts; `window.__done = true` after the final frame has been held 1.5 s.

## What it shows (default pace, seconds)
0 root run appears, tree grows, cursor sweeps the waterfall (time axis x; one bar per row; chain/LLM/tool colours) · 2.1 zoom card:
the supervisor's routing LLM call (model, latency, tokens in/out/reasoning, cost, prompt system+tail, reply = forced `Route` tool call
with agents/services/reason) · 5.8 three specialists in parallel (teal band), finished subtrees collapse into their parent bar (the LLM
spans stay visible inside it) · 8.1 zoom card: a specialist's LLM call asking for 5 tool calls · 10.9 smaller card: `query_metric x4`
(args, result head/tail) · 16.2 `rca_tournament` (gather, plan, score_hypothesis, rank, 5 ms in total) · the interrupt at the approval
gate (amber, with the interrupt payload) · 21.4 thread card: both traces on one axis, hatched 31.7 s human wait (compressed), labelled
interrupt / resume, short thread id `22b4ce55…1761802` · 25.5 trace 2 plays with larger rows (resume, approved by on-call) and the final
report text · hold 1.5 s. Top strip: running totals (runs, LLM calls, tokens, cost, wall time + human wait); under it a persistent thread
timeline with the playhead. Time-warp: sweeps run at 1x to 5x of real time (`KF` keyframes in `trace.js`), holds park the clock for cards.
Text is >= 15 CSS px; nothing important below y = 606 (subtitle safe area).

## Verified (this session)
    cd /Users/garyamorris/Developer/repos/langgraph-app && python3 -m http.server 8767 --directory docs/demo/video-source &
    docs/demo/video-source/rec/rec --url 'http://127.0.0.1:8767/viz/trace.html?seek=12' --shot docs/demo/video-source/work/trace_a.png --after 2
    docs/demo/video-source/rec/rec --url 'http://127.0.0.1:8767/viz/trace.html' --out docs/demo/video-source/work/trace/trace_clip.mp4 --director docs/demo/video-source/viz/director_wait.js --max-seconds 60
    docs/demo/video-source/rec/vtool frame docs/demo/video-source/work/trace/trace_clip.mp4 12.5 /tmp/f.png 1280
Stills checked at seek 0.3 1.2 2.1 3.6 6.4 7.4 9.3 10 12 13.5 15.5 17.8 19.5 21.5 23 24 25.5 26 27 28.9; `focus=` variants; `fit=40`;
real-time clip 30 s, 0 console errors, 82 repeated frames of 905 (recorder load).

## Other data
`trace_data_five.json`: thread `ad7d4546-9db6-5dd8-9daf-6b12d3405900` (INC with all five specialists in parallel; one trace, no resume
was run for it). Use `trace.html?data=trace_data_five.json`; the tree scrolls because five expanded specialists need > 18 rows, and the
thread-card / resume scenes are skipped (single trace).
