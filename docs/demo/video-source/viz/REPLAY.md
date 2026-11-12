# replay.html: replay a recorded Foghorn run

`viz/replay.html` (+ `replay.js`, `replay_data.js`, `replay_layout.js`, `replay.css`) replays a real recorded run
(`<cap>/events.jsonl` + `<cap>/structure/<graph>.json`) as an animation of LangGraph execution. Everything on screen is a
pure function of the recorded data and the animation clock (no randomness). 1280x720, no libraries, no network.

    http://127.0.0.1:8765/viz/replay.html?cap=../captures/real&graph=incident_commander&thread=22b4ce55&fit=60

## URL params
| param | meaning |
|---|---|
| `cap` | capture folder, relative to the page (default `../work/rehearsal_cap`) |
| `graph` | graph_id (default `incident_commander`, else the first graph in the capture). If the graph has no recorded run the structure is shown alone ("NO RECORDED RUN"). |
| `thread` | thread_id prefix. Default: the thread of `graph` with the most events (ties: earliest). |
| `run` | `auto` (default) = first run plus every following run that continues after an `interrupt` or an error (pause + resume are both shown); `all` = every run of the thread; `N`, `N,M`, `N-M` = run indices (0-based). |
| `fit` | seconds. Uniformly warps the whole animation to this duration (the 1.5 s end hold is included). Holds (interrupt pause, minimum node durations) scale with it too. |
| `seek` | render the frame at this animation second and stop (stills). `window.__seek(t)` does the same at runtime. |
| `from`, `to` | crop by capture time `t` (seconds in events.jsonl). |
| `caps` | `0` hides helper captions (default 1). |
| `beats` | URL (relative to the page) of a JSON list `[{"at": "<selector>", "text": "...", "hold": 3}]`: bottom-left helper captions. `hold` is in output seconds. |
| `cam` | `all` = debug: static camera fitting the whole graph (text gets small). |

## Beat selectors (`at`)
* `task:<name>:start` / `task:<name>:done`: first run of that task; optional trailing `:n` picks the n-th (1-based), e.g. `task:draft:start:2`. `<name>` may be an inner node (`model`, `metrics_analyst:model`).
* `custom:<kind>` / `custom:<kind>:n`: n-th custom event of that kind (`handoff`, `evidence.found`, `hypothesis.update`, `policy.check`, `plan.proposed`, `decision`, `memory.recalled`, ...).
* `interrupt` / `interrupt:n`: the n-th pause (first node that stops for a human, or crashes).
* `resume` / `resume:n`: the start of the n-th run that continues after an interrupt or error.
* `t:<seconds>`: animation time.

## Window globals
* `window.__meta = {duration, graph, thread, runs:[{index,run_id,resume,start,end,interrupted}], beats:[{t,label}], captions:[...], tasks:[{name,start,end,ns,run,status}], warp}` is set before the animation starts. `beats` are automatic and use the selector syntax above (plus `run:N:start`, `resume`, `fanout:<node>:x<N>`, `end`).
* `window.__done = true` after the animation plus 1.5 s hold. `window.__seek(t)`. `window.__ready`, `window.__error` (set if loading failed).

## What is drawn
* Graph from the real xray structure: left-to-right layered layout with per-graph column hints (`HINTS` in `replay_layout.js`), subgraphs (`parent:child`) as labelled containers, conditional edges dashed with route labels, long/back edges as faint "lanes", `__start__`/`__end__` as dots, `__error_handler__*` nodes dropped, specialist middleware hooks (`before_model`/`after_agent`) as unlabelled dots inside each container, Functional-API entrypoints as a box that grows `@task` chips (replayed cached tasks flash dashed blue on resume).
* Node states: idle, running (pulse, in the node's hue; the waterfall bar uses the same hue), done (check), interrupted (amber, pause icon, amber frame, card with title + checkpoint stamp), error (red). `Send` fan-out = pips + count badge (`x6` -> `2/6` -> `6/6`). `custom:handoff` = bubble `-> to` + reason.
* Camera follows the running nodes (never below 0.96 scale so text stays >= 15 px; minimap in the header when the graph is wider than the view). Panels: state strip, custom-event ticker, LLM token streams (<= 3 lanes, caret), task waterfall (bottom of graph area; amber/red band = paused / crashed).
* Time warp: capture gaps > 1.2 s become 0.45 s; every task stays visible >= 0.42 s (0.30 s inner, 0.12 s hooks); parallel results tick off 0.15 s apart; ticker lines >= 0.3 s apart; token chunks >= 45 ms apart (a single message never streams longer than 3.6 s); interrupt hold 3.1 s; then `fit` scales everything uniformly.

## Known limits
* Layout is tuned for the 10 Foghorn graphs (column hints); an unknown graph falls back to automatic layering, which is cruder.
* `change_gate` (many conditional back edges) and the top-level `incident_commander` downstream chain draw their long edges as faint lanes; they are readable only while the camera is zoomed in (by design).
* Hook nodes (`before_model`, `after_agent`) have no text label. The resume value (`Command(resume=...)`) is not in the stream, so the card does not show it.
* The default `run=auto` shows the first episode only; the dev capture contains a second, duplicated batch of runs on the same thread ids (use `run=all` to see them).
* A very large `fit` over a short capture stretches the pause too (uniform warp, by design).
