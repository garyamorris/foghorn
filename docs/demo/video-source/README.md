# How the Foghorn walkthrough video was made

`foghorn-walkthrough.mp4` (1080p) and `foghorn-walkthrough_720p.mp4` (messaging copy) are attached to the
[`walkthrough-v1` GitHub Release](https://github.com/garyamorris/langgraph-app/releases/tag/walkthrough-v1) (not committed; the build writes them to
`../`, which git ignores). `../foghorn-walkthrough.srt/.vtt` are the captions (also burned into the video).
Everything on screen comes from **recorded runs of the real app on a real model** (OpenAI, `FOGHORN_PROVIDER=openai`); nothing is mocked.

| Stage | What | Where |
|---|---|---|
| 1. Recording server | clean `langgraph dev` on :2031 (fresh data dir, tracing to LangSmith project `foghorn-video`) | `work/start_server.sh` |
| 2. Tap | joins every run's SSE stream with `Last-Event-ID: -1`, writes timestamped events (all stream modes, subgraph namespaces) + the real graph structure | `tap.py` -> `captures/real/` (trimmed by `trim_capture.py`, committed) |
| 3. Dashboard + Studio footage | an offscreen WKWebView recorder (`rec/rec`, built from `rec/rec.swift`) drives the real UI like a person: launch S01, watch the war room, approve, postmortem, memory; Studio live run of `incident_commander` through a local HTTPS proxy (`rec/tlsproxy.py`) | `rec/director_hero.js`, `rec/director_studio_*.js` -> `raw/` |
| 4. Scripted extra runs | change_gate gauntlet (parallel four-eyes interrupts), shift_handoff crash + resume, storm_correlator, ops_copilot | `capture_extras.py` |
| 5. Animated graph visuals | pages that replay the recorded runs on the real graph structure: `replay.html` (nodes, Send fan-out, subgraphs, interrupt/resume, ticker, token stream, waterfall), `streams.html`, `overview.html`, `trace.html` (real LangSmith run data), `evals.html` | `viz/` (see `viz/*.md`), rendered by `record_viz.py` -> `viz_out/` |
| 6. Narration | script written from the recorded numbers (`facts.py`), OpenAI `gpt-4o-mini-tts` voice `marin` | `narration/script.json`, `tts.py` |
| 7. Transcription | every narration clip is transcribed back with word timestamps (`whisper-1`); caption text comes from the script, times from the transcript; on-screen labels are placed at the moment their phrase is spoken | `narration/captions.py`, `make_manifest.py` |
| 8. Assembly | FilmCraft driven through its control server: timeline, callouts, caption track burned in, bitrate-fitted export | `build_video2.py` |

Rebuild (the recordings in `raw/` and `viz_out/` are not committed: ~300 MB):

```bash
python3 make_manifest.py                       # manifest.json from the per-scene spec + narration timings
python3 record_viz.py                          # animated visuals from captures/real (no server or API key needed)
FILMCRAFT_CONTROL_PORT=7979 python3 build_video2.py manifest.json ../foghorn-walkthrough.mp4 --small --joins
```
Narration needs `OPENAI_API_KEY` (about 10 cents): `python3 narration/tts.py all && python3 narration/captions.py words && python3 narration/captions.py cues`.
