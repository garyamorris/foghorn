#!/usr/bin/env python3
"""make_manifest.py: write manifest.json for build_video2.py from a declarative per-scene spec.

Scene lengths come from the narration (durations.json); callouts are placed at the moment a PHRASE is spoken (word times from
narration/words.json, the word-level transcript of the actual audio), so on-screen labels land exactly on the words they annotate.

    python3 make_manifest.py [out=manifest.json]
Footage: raw/hero_real.mp4 (dashboard, real model), raw/studio_*.mp4 (LangGraph Studio live), viz_out/*.mp4 (animated visuals driven by
the recorded runs; rendered by record_viz.py with fit = scene length).
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DUR = json.load(open(os.path.join(HERE, "narration/durations.json")))
WORDS = json.load(open(os.path.join(HERE, "narration/words.json")))
PB, PA = 0.35, 0.45  # pad before / after narration per scene


def norm(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def phrase_time(sid: str, phrase: str, nth: int = 0) -> float:
    """Seconds from the start of the NARRATION where `phrase` begins (matched on the transcribed words); falls back to 0."""
    ws = WORDS[sid]
    toks = [norm(w["word"]) for w in ws]
    toks = [t[0] if t else "" for t in toks]
    want = norm(phrase)
    hits = [i for i in range(len(toks) - len(want) + 1) if toks[i : i + len(want)] == want]
    if len(hits) <= nth:
        print(f"WARN: phrase {phrase!r} not found in scene {sid}", file=sys.stderr)
        return 0.0
    return float(ws[hits[nth]]["start"])


def V(path, i=None, o=None, speed="fit", **kw):
    d = {"type": "video", "path": path, "speed": speed}
    if i is not None:
        d["in"] = i
    if o is not None:
        d["out"] = o
    d.update(kw)
    return d


def FREEZE(path, t, move="zoom-in", weight=1.0, **kw):
    return {"type": "video", "path": path, "in": t, "freeze": True, "move": move, "speed": "fit", "weight": weight, **kw}


def callouts(sid: str, items):
    out = []
    for phrase, text, dur, *rest in items:
        nth = rest[0] if rest else 0
        out.append({"at": round(PB + phrase_time(sid, phrase, nth) - 0.15, 2), "dur": dur, "text": text, "style": "tech", "anchor": "tr"})
    return out


def clip_dur(path: str) -> float:
    import subprocess
    out = subprocess.run([os.path.join(HERE, "rec/vtool"), "info", os.path.join(HERE, path)], capture_output=True, text=True).stdout
    m = re.search(r"duration ([\d.]+) s", out)
    return float(m.group(1)) if m else 20.0


HERO = "raw/hero_real.mp4"
SL = "raw/studio_live_real.mp4"
SG = "raw/studio_graphs.mp4"

SCENES = [
    # id, tag, segments, callouts
    ("A", "Foghorn", [V(HERO, 1.0, 8.7, speed=1.0), FREEZE(HERO, 8.4, "zoom-in", 1.0)],
     [("Foghorn", "Foghorn · AI incident command", 4.5), ("workflows", "10 LangGraph graphs · 1 deployment", 4.5)]),
    ("B", "The ten graphs", [V("viz_out/overview.mp4", 0, None)],
     [("event router", "event_router · the front door", 3.5), ("incident commander", "incident_commander · the big one", 3.5)]),
    ("C", "Event pipeline", [V(HERO, 8.7, 26.4)],
     [("validates", "validate · dedupe · route · queue", 4.5), ("dispatches", "dispatcher → LangGraph run", 4.5)]),
    ("D", "Graph · event_router", [FREEZE("viz_out/replay_router.mp4", 0.15, "none", weight=10.0), V("viz_out/replay_router.mp4", 0, None)],
     [("state machine", "StateGraph · nodes + edges", 4.5), ("three parallel", "Send fan-out · enrich ×3", 4.5), ("retry policy", "RetryPolicy · CachePolicy", 4.5), ("hands it", "Command → incident_commander", 4.5)]),
    ("E", "Graph · incident_commander", [V("viz_out/replay_commander_a.mp4", 0, None), V(HERO, 36.0, 52.0)],
     [("command", "Command(goto) · parallel routing", 4.5), ("subgraph", "specialists = compiled subgraphs", 4.5), ("token by token", "stream mode: messages", 4.5)]),
    ("F", "Graph · rca_tournament", [V("viz_out/replay_rca16.mp4", 0, None), V(HERO, 45.7, 52.5)],
     [("mapreduce", "Send map-reduce", 4.0), ("reducer", "reducer merges the scores", 4.0), ("at once", "× 16 in parallel", 4.0)]),
    ("G", "Human in the loop", [V("viz_out/replay_commander_b1.mp4", 0, None, weight=11.5), FREEZE("viz_out/replay_commander_b1.mp4", clip_dur("viz_out/replay_commander_b1.mp4") - 0.2, "none", weight=6.5),
                                 V(HERO, 79.0, 86.0, weight=7.0), V("viz_out/replay_commander_b2.mp4", 0, None, weight=9.0)],
     [("policy check", "policy_check · 6 rules", 4.0), ("calls interrupt", "interrupt() · state saved to a checkpoint", 5.5), ("Command with", "Command(resume=…) · same thread", 5.0)]),
    ("H", "Streaming", [V("viz_out/streams.mp4", 0, None)], []),
    ("I", "Graph · postmortem_pipeline", [FREEZE("viz_out/replay_postmortem.mp4", 0.1, "none", weight=7.0), V("viz_out/replay_postmortem.mp4", 0, None), V(HERO, 106.1, 113.2), V(HERO, 120.1, 126.0)],
     [("Functional API", "Functional API · @entrypoint / @task", 5.0), ("another interrupt", "interrupt() · human review", 4.0), ("semantic search", "Store · semantic search", 4.0)]),
    ("J", "Graph · storm_correlator", [V("viz_out/replay_storm.mp4", 0, None)],
     [("fan out", "Send ×14 → reduce → one cluster", 5.0)]),
    ("K", "Graph · change_gate", [V("viz_out/replay_gate.mp4", 0, None)],
     [("four eyes", "parallel interrupts · resume by id", 5.0)]),
    ("L", "Graph · shift_handoff", [V("viz_out/replay_crash.mp4", 0, None)],
     [("crash", "durable execution", 3.5), ("no input", "resume from the checkpoint", 4.0)]),
    ("M", "LangGraph Studio", [V(SG, 2.5, 10.2, weight=5.5), V(SL, 6.3, 14.4, weight=5.5), V(SL, 22.3, 55.6, weight=5.5), V(SL, 60.4, 71.5, weight=7.0)],
     [("studio", "LangGraph Studio · local server", 4.5), ("nodes light up", "live node execution", 4.0), ("response form", "form generated from the response schema", 5.0)]),
    ("N", "LangSmith trace", [V("viz_out/trace.mp4", 0, None)],
     [("LangSmith", "LangSmith · every run is a trace", 4.5)]),
    ("O", "Evaluations", [V("viz_out/evals.mp4", 0, None)], []),
    ("P", "Foghorn", [FREEZE("viz_out/overview.mp4", clip_dur("viz_out/overview.mp4") - 0.7, "zoom-out")], []),
]


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "manifest.json")
    scenes = []
    for sid, tag, segs, cos in SCENES:
        scenes.append({"id": sid, "segments": segs, "callouts": callouts(sid, cos), "pad_before": PB, "pad_after": PA})
    m = {
        "width": 1920, "height": 1080, "fps": 30, "narration_dir": "narration/audio",
        "preset": "Match Source – Adaptive Low Bitrate", "max_mb": 90, "audio_kbps": 128,
        "small": {"width": 1280, "height": 720, "max_mb": 40, "audio_kbps": 96},
        "xfade": 0.3, "narration_db": -3.0, "captions": True, "caption_style": {"font": "Inter", "size": 34, "background": "#000000b0", "margin": 0.05},
        "title": {"text": "Foghorn", "sub": "how ten LangGraph graphs run an incident", "seconds": 3.5,
                  "bg": {"type": "video", "path": HERO, "in": 1.0, "freeze": True}, "dim": -90},
        "outro": {"text": "Run it yourself", "sub": "make dev  ·  localhost:2026/app  ·  LangGraph Studio", "seconds": 3.5,
                  "bg": {"type": "video", "path": HERO, "in": 131.0, "freeze": True}, "dim": -90},
        "scenes": scenes,
    }
    json.dump(m, open(out, "w"), indent=1, ensure_ascii=False)
    total = sum(DUR[s[0]] + PB + PA for s in SCENES)
    print(f"wrote {out}: {len(scenes)} scenes, ~{total + 7:.0f}s incl. cards")


if __name__ == "__main__":
    main()
