# Foghorn explainer pages: overview, streams, evals

Serve the folder (repo root): `python3 -m http.server 8768 --directory docs/demo/video-source` and open
`http://127.0.0.1:8768/viz/<page>.html`. All pages are 1280x720 CSS px, offline, dark Foghorn tokens (`fh.css`), text >= 15 px,
bottom ~110 px kept free (content ends at y ~ 600). Shared code: `fhlib.js` (player/easing), `fg.js` (graph layout + drawing, overview only).

## Params (all three pages)
* `?seek=S` draw one still frame at output second S, set `__done=true`, stop (use for `--shot`).
* `?fit=S` warp the whole animation to S seconds (`__meta.duration` and beat times are already scaled).
* real time: the clock starts when `rec` starts recording (it detects the `rec.t()` restart), so the first video frame is t=0;
  `window.__done = true` at the end (use `director_wait.js`). Without `rec` it starts at once.
* `window.__seek(t)` redraws at output time t (debug). `window.__ready` true after the first frame.

* streams only: `?spot=<lane>@<sec>,<lane>@<sec>,...` (output seconds, after `fit`; lanes `updates|messages|custom|tasks|checkpoints`) spotlights that lane (glow, caption lit, others dimmed to 45 %, 0.25 s fades) until the next entry; the last one is held to the end. Echoed as `__meta.spot`. Example (fit 30.7): `spot=updates@8.5,messages@11.2,custom@13.4,tasks@19.6,checkpoints@22.2,messages@24.6,custom@26.4,tasks@28.5`.

## `window.__meta`
`{duration, default_duration, warp, beats:[{t, name, label?}], page, ...}` (seconds, after `fit`).

| page | default | beats (names) | extras |
|---|---|---|---|
| overview.html | 14 s | `title`, `stage_ingress|response|gym|learning|ops`, `card_<graph_id>` x10 (event_router 0.7 s, incident_commander 2.4, rca_tournament 3.8, storm_correlator 4.6, change_gate 5.7, postmortem_pipeline 6.8, memory_keeper 7.5, eval_runner 8.4, ops_copilot 9.3, shift_handoff 10.2), `grid_complete` 11.0, `sweep` 11.2 | `cards`, `data`; `window.__graphs` = layout stats per card |
| streams.html | 30 s | `title`, `one_connection`, `mode_tasks`, `mode_messages`, `mode_custom`, `mode_updates` (each = first event of that mode, ~1.7-5 s), `scene_handoff`, `scene_rca`, `scene_interrupt`, `paused_for_human`, `scene_resume`, `resumed`, `mode_checkpoints` (first checkpoint, after the resume), `outro` | `events`, `scenes[{name,t}]`, `source`, `thread_id`, `run_ids`; `window.__pacing` |
| evals.html | 25 s | `title`, `grid_columns`, `row_competent` 1.2, `row_cautious` 5.0, `row_naive` 8.4, `hard_gates` 11.2, `row_gullible` 12.4, `gullible_s15_trip` 16.2, `policy_check_stopped_it` 16.6, `row_real_model` 19.2, `weakest_evaluators` 20.7, `outro` 22.2 | `profiles[{profile,passed,total,mean_score}]`, `real_model` |

## Data
* overview: `../<cap>/structure/<graph_id>.json`, `?cap=captures/real` (default) or `?cap=work/rehearsal_cap` (identical graphs).
* streams: `viz/streams_data.json`, built by `python3 viz/make_streams_data.py [capdir] [--out file]` (default `captures/real`): the real
  incident_commander thread 22b4ce55 (run 1 = paused at the approval `__interrupt__`, run 2 = resumed): 4 scenes (hop 1: supervisor ->
  3 parallel specialists with interleaved token streams; rca_tournament; supervisor -> comms_drafter + remediation_planner, policy checks,
  interrupt; resumed run with a checkpoint per superstep). Only real events, recorded order; `values`/`metadata`, analyst middleware tasks and
  long token runs are thinned (the lane shows `+N chunks`); every event keeps `i` = its events.jsonl line. Offline run: `?data=streams_data_rehearsal.json`.
* evals: `viz/evals_data.json`, built by `python3 viz/make_evals_data.py` from `datasets/evals/baseline_*.json` + scenario titles/tiers; the
  real-model row (15/16, mean 0.923, gates clean, memory recall 0.50, comms quality 0.69) is the given summary only.

## Verified (server on 8768)
```
rec/rec --url 'http://127.0.0.1:8768/viz/overview.html?seek=12.4' --shot work/viz_overview_end.png --after 1.2
rec/rec --url 'http://127.0.0.1:8768/viz/streams.html?seek=25'    --shot work/viz_streams_resume.png --after 1.2
rec/rec --url 'http://127.0.0.1:8768/viz/evals.html?seek=17'      --shot work/viz_evals_trip.png --after 1.2
rec/rec --url 'http://127.0.0.1:8768/viz/streams.html' --out work/x.mp4 --director viz/director_wait.js --max-seconds 60   # real time, 0 console errors
rec/rec --url 'http://127.0.0.1:8768/viz/evals.html?fit=20' --out work/x.mp4 --director viz/director_wait.js --max-seconds 60
```
