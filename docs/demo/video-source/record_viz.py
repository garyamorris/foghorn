#!/usr/bin/env python3
"""record_viz.py: render the animated visuals (viz/*.html, driven by the recorded runs in captures/real) to viz_out/*.mp4 with rec.

    python3 record_viz.py [--only overview,replay_router] [--list]

Each clip is rendered in real time at 1920x1080/30fps by the offscreen WebKit recorder; `fit` warps the animation to the length the
narration needs (scene length = narration + pads, from narration/durations.json), so the motion is natural instead of retimed afterwards.
Serves the folder on a private port for the duration of the run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DUR = json.load(open(os.path.join(HERE, "narration/durations.json")))
PORT = 8771
CAP = "../captures/real"


def f(scene: str, share: float = 1.0, extra: float = 0.6) -> float:
    return round(DUR[scene] * share + extra, 1)


# name -> (page, params, fit seconds).  Thread prefixes are from captures/real/threads.json.
CLIPS = {
    "overview": ("overview.html", {}, f("B")),
    "replay_router": ("replay.html", {"graph": "event_router", "thread": "4e8af2e0"}, 23.2),  # D: 10 s lead hold + this clip (animation starts at the word 'validated')
    "replay_commander_a": ("replay.html", {"graph": "incident_commander", "thread": "22b4ce55", "from": 0.9, "to": 16.6}, f("E", 0.55)),
    "replay_rca16": ("replay.html", {"graph": "incident_commander", "thread": "ad7d4546", "from": 307.8, "to": 316.2}, f("F", 0.75)),
    "replay_commander_b1": ("replay.html", {"graph": "incident_commander", "thread": "22b4ce55", "from": 24.5, "to": 28.6}, 11.5),  # G: policy -> interrupt (then a held last frame)
    "replay_commander_b2": ("replay.html", {"graph": "incident_commander", "thread": "22b4ce55", "from": 59.4, "to": 62}, 9.0),  # G: Command(resume) -> execute -> verify -> close
    "streams": ("streams.html", {"spot": "updates@8.5,messages@11.2,custom@13.4,tasks@19.6,checkpoints@22.2,messages@24.6,custom@26.4,tasks@28.5"}, 30.7),  # spotlight follows the narration
    "replay_postmortem": ("replay.html", {"graph": "postmortem_pipeline", "thread": "e8a10dad"}, 8.5),  # I: 4.5 s lead hold + this clip
    "replay_storm": ("replay.html", {"graph": "storm_correlator"}, f("J")),
    "replay_gate": ("replay.html", {"graph": "change_gate"}, f("K")),
    "replay_crash": ("replay.html", {"graph": "shift_handoff"}, f("L")),
    "trace": ("trace.html", {"data": "trace_data_hero.json"}, f("N")),
    "evals": ("evals.html", {}, f("O")),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    names = a.only.split(",") if a.only else list(CLIPS)
    if a.list:
        for n in names:
            print(n, CLIPS[n])
        return 0
    os.makedirs(os.path.join(HERE, "viz_out"), exist_ok=True)
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(PORT), "--bind", "127.0.0.1", "--directory", HERE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        for n in names:
            page, params, fit = CLIPS[n]
            q = "&".join([f"{k}={v}" for k, v in {**({"cap": CAP} if page == "replay.html" else {}), **params, "fit": fit}.items()])
            url = f"http://127.0.0.1:{PORT}/viz/{page}?{q}"
            out = os.path.join(HERE, "viz_out", f"{n}.mp4")
            print(f"== {n}: fit {fit}s\n   {url}")
            r = subprocess.run([os.path.join(HERE, "rec/rec"), "--url", url, "--out", out, "--director", os.path.join(HERE, "viz/director_wait.js"), "--tail", "0.4", "--max-seconds", str(int(fit + 60))],
                               capture_output=True, text=True)
            tail = [l for l in r.stderr.splitlines() if "console.error" in l or "WARN" in l or "done:" in l][:6]
            print("   " + "\n   ".join(t[:200] for t in tail))
    finally:
        srv.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
