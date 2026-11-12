#!/usr/bin/env python3
"""Assemble the "Probably a Tragedy" walkthrough (v2: real video footage + OpenAI-TTS narration) in FilmCraft through its
localhost JSON-lines control server.  v1 (build_video.py, stills + `say`) is untouched; this file is self-contained.

USAGE
    python3 build_video2.py manifest.json out.mp4 [options]
      --plan-only         print the plan table (scene, start, length, narration, segments, warnings) and exit; no FilmCraft needed
      --scenes A,C,F      build only these scene ids (fast iteration; title/outro cards are kept unless --no-cards)
      --frames N          also write N evenly spaced 1080p PNG frames of the finished *sequence* + contact sheet to out/frames_<stem>/
      --at 12.5,60,61.2   also write PNG frames of the sequence at these times (seconds) to out/frames_<stem>/at_<t>.png
      --joins             sample frames around every scene boundary / segment cut of the sequence and report black/flash problems
      --no-cards          leave out the title/outro cards (with --scenes)
      --no-export         build the timeline (and frames/joins) but skip the MP4 export
      --small             also export the small 1280x720 messaging copy  <stem>_720p.mp4   (manifest "small" section; default budget 28 MB)
      --max-mb N          override the size budget of the main export (manifest "max_mb", default 60); 0 = use the preset as is
      --preset NAME       override the manifest's base export preset
      --bitrate-mode M    vbr1Pass (default) | cbr.  (vbr2Pass overshot the budget and scored ~6 dB lower PSNR in tests; crf is not size-predictable)
      --no-faststart      skip the lossless avconvert remux that moves the MP4 index to the front
      --keep-project      leave the project open in FilmCraft;  a <out>.fcproj is always saved next to the output
    Also writes <out>.plan.json (scene start/narration times) that `verify_mp4.py --joins/--sync` uses.

MANIFEST (JSON; relative paths resolve against the manifest's folder)
{
  "width": 1920, "height": 1080, "fps": 30,
  "narration_dir": "narration/audio",         # optional: a scene without voiceover_wav uses <dir>/<id>.wav when that file exists
  "preset": "Match Source – Adaptive Low Bitrate",   # base export preset (H.264 + AAC 1080p30)
  "max_mb": 60, "audio_kbps": 128,            # main export is bitrate-fitted to the budget (re-exports once or twice if the first try is over)
  "small": {"width": 1280, "height": 720, "max_mb": 28, "audio_kbps": 96},     # --small copy (or null)
  "xfade": 0.35,                              # default cross-dissolve between consecutive segments/scenes, seconds (0 = hard cuts)
  "narration_db": -3.0,                       # gain of the narration track A1 (mono wav -> both channels; -3 dB = ~-16 LUFS stereo programme)
  "music": null, "music_db": -22,             # optional quiet bed on A2
  "title": CARD | null, "outro": CARD | null, # first / last segment
  "scenes": [ SCENE, ... ]
}
SCENE
  { "id": "C",
    "voiceover_wav": "narration/audio/C.wav",  # pre-made narration; scene length = pad_before + narration + pad_after
    "seconds": 6,                              # only used when there is no narration (or as a minimum length)
    "pad_before": 0.4, "pad_after": 0.6,       # defaults 0.4 / 0.6
    "xfade": 0.35,                             # dissolve INTO this scene's first segment (default: manifest xfade)
    "segments": [ SEGMENT, ... ],              # play back to back, short cross-dissolves between them, tiling the whole scene
    "callouts": [ {"at": 2.0, "dur": 4.0, "text": "Send API · fan-out x8", "style": "tech"|"caption"|"title",
                   "sub": "optional second line (title style only)", "anchor": "bl|bc|br|tl|tc|tr"} ],
    "tag": "GRAPH: casting_call" | null }      # small gold chip, top-left, for the whole scene
SEGMENT (video)
  { "type": "video", "path": "raw/casting.mp4", "in": 12.0, "out": 48.0,   # source seconds ("out" omitted = end of file)
    "speed": "fit" | 1.0 | 2.5,                # "fit" (default): stretch/compress to its share of the scene; a number = fixed speed
    "weight": 1.0,                             # share of the flexible time (default = source length, so equal speed for all fit videos)
    "ramp": [1, 5],                            # optional with "fit": relative speed profile start->end (6 steps), keeps in/out and total length
    "crop": [x0,y0,x1,y1],                     # optional region (fractions 0..1) that is zoomed to fill the frame
    "fit": "auto|fill|contain", "focus": [0.5,0.5], "move": "none|zoom-in|zoom-out|pan-left|pan-right",
    "freeze": true,                            # use the single frame at "in" as a still (for cards / stills-from-footage)
    "volume": 0 }                              # accepted for compatibility: native audio is NEVER placed (narration only)
  Retiming: "fit" speed = source length / allotted time, clamped to 0.5x-8x.  >8x needed -> the source range is trimmed (tail cut) and a
  warning printed;  <0.5x -> plays at 0.5x then holds the last frame (warning).  Fixed-speed segments take (out-in)/speed seconds; fit
  segments share what is left of the narration length.  Gaps/overruns of an all-fixed scene are absorbed by the last segment (hold / trim).
SEGMENT (still)
  { "type": "still", "path": "shots/a.jpg", "seconds": 4 (fixed) | "weight": 1.0 (share), "move": "zoom-in|zoom-out|pan-left|pan-right|none",
    "focus": [0.5,0.5], "crop": [x0,y0,x1,y1], "fit": "auto|fill|contain" }
CARD (title / outro)
  { "text": "Probably a Tragedy", "sub": "a LangGraph walkthrough", "seconds": 4, "voiceover_wav": null, "pad_before": .., "pad_after": ..,
    "bg": SEGMENT,      # the bg segment (video, freeze or still) sits under big centred text
    "dim": -90 }        # brightness of the bg (0 .. -100; -90 = about half as bright); applied as an effect on the bg clip itself

NOTES
  * Callouts: bold Inter 46 px white on a dark translucent rounded box; 'tech' adds a gold accent bar; 'caption' is plain; they fade in/out
    (8 frames).  Overlapping callouts at the same anchor stack automatically.  Keep text <= ~45 characters (auto-shrinks beyond).
  * Native video audio is dropped at placement time; narration is A1 (clip per scene), music (optional) A2.
  * Keyframe times are SEQUENCE ticks (254016000000/s).  Dissolves are centred on the cut, so they never change scene timing;
    clips that run out of source at a cut simply hold their first/last frame through the dissolve (no black).

HOW IT WORKS (quirks learned the hard way)
  * engine.execute needs {"command": id, "params": {...}} -- params at the top level open a native dialog and freeze FilmCraft.  If it
    stops answering, the script kills and relaunches FilmCraft once, then gives up.
  * tracks (graphics `track` is 0-based): V1 dark backdrop (created AFTER the footage: clip.insertFrameHoldSegment ripples every sync-locked
    track and would split it), V2 footage/stills, V3 title bands / dims, V4-V6 callout lanes, V7 tag chip, V8/V9 big title text.
  * video: timeline.place {sourceIn, duration (ticks), audio:false} at 1x, then clip.speedDuration {speed:percent} (absolute, 800 = 8x) shrinks
    the clip to source/speed; the source range is unchanged.  Placement is chronological, so a temporary 1x clip never overwrites a clip we need.
  * Motion: video is placed at 100% of its native pixels, so scale = fill/contain % is set explicitly (1280x720 source -> 150 %).
  * Tail holds use clip.insertFrameHoldSegment (splits the clip: head, hold, 1-frame remainder); motion is then applied to every clip in range.
  * export.quick takes {preset, path, wait, settings:{...}}: bitrate / frame size / audio bitrate go in `settings` (flat keys are ignored).
    wait:true gives up after ~60 s ("timeout") so exports are started with wait:false and polled through jobs.list (4 min 1080p ~ 2 min).
    The video bitrate is chosen from the size budget: kbps = (max_mb*8000/seconds - audio_kbps) * 0.90 with vbr1Pass (lands within ~5 %);
    if the file is over budget it is re-exported at a corrected bitrate (up to 2 retries).  Presets carry 320 kbps AAC; we override to 128.
  * Cards dim their bg clip with a brightness effect (not an overlay or opacity: those flash during the dissolve into the card) and put a
    soft dark band behind the text.  Mid-scene 'title' callouts use a fading dim overlay instead (no cut involved).
  * A callout is ONE graphic clip with layers [rounded box, gold bar, text] (text measured with graphics.list, then box sized to it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FILMCRAFT_CONTROL_PORT", "7878"))
FILMCRAFT = os.path.expanduser("~/Applications/FilmCraft.app/Contents/MacOS/FilmCraft")
TICKS = 254016000000  # per second

# graphics track indexes (0-based) = V1..V9
BACK, MAIN, DIM, CALL0, CALL1, CALL2, CHIP, TITLE1, TITLE2 = range(9)
N_VIDEO_TRACKS = 9
GOLD = "#f2c14e"
INK = "#07050b"
FONT = "Inter"
MIN_SPEED, MAX_SPEED = 0.5, 8.0
STILL_WEIGHT_DEFAULT = 1.0
RAMP_STEPS = 6


class FilmCraftError(RuntimeError):
    pass


class FilmCraftHang(FilmCraftError):
    pass


WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- control client
def rpc(method: str, params: dict | None = None, timeout: float = 60) -> dict:
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s.settimeout(timeout)
        s.sendall((json.dumps({"method": method, "params": params or {}, "id": 1}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        s.close()
    except socket.timeout as exc:
        raise FilmCraftHang(
            f"FilmCraft did not answer `{method}` within {timeout:.0f}s -- a native dialog is probably open "
            "(or a command was given params at the top level). Run: pkill -f 'FilmCraft.app/Contents/MacOS/FilmCraft' and retry."
        ) from exc
    except OSError as exc:
        raise FilmCraftError(f"cannot reach FilmCraft's control server on port {PORT}: {exc}") from exc
    return json.loads(buf)


def ex(command: str, params: dict | None = None, timeout: float = 90):
    r = rpc("engine.execute", {"command": command, "params": params or {}}, timeout)
    if not r.get("ok"):
        raise FilmCraftError(f"{command} {json.dumps(params or {})[:300]} failed: {r.get('error')}")
    return r.get("result")


def launch_filmcraft() -> None:
    data = os.path.join(HERE, "fcdata")
    os.makedirs(data, exist_ok=True)
    log = open(os.path.join(HERE, "fc.out"), "ab")
    subprocess.Popen(
        [FILMCRAFT, "--empty", "--no-recover", "--control", str(PORT), "--data-dir", data],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
            time.sleep(1.0)
            return
        except OSError:
            time.sleep(0.5)
    raise FilmCraftError("FilmCraft did not open its control port after launch")


def ensure_filmcraft() -> None:
    """Make sure FilmCraft is running and answering; relaunch it once if it is absent or hung."""
    for _attempt in (1, 2):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
            up = True
        except OSError:
            up = False
        if up:
            try:
                r = rpc("engine.execute", {"command": "state.inspect", "params": {}}, timeout=8)
                if "ok" in r:
                    return
            except FilmCraftHang:
                print("FilmCraft is hung (native dialog?) -- restarting it", file=sys.stderr)
        subprocess.run(["pkill", "-f", "FilmCraft.app/Contents/MacOS/FilmCraft"], check=False)
        time.sleep(1.5)
        launch_filmcraft()
    raise FilmCraftError("could not get a responsive FilmCraft")


# ---------------------------------------------------------------- media probing (no FilmCraft needed)
def path_of(base: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def image_size(path: str) -> tuple[int, int]:
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"pixelWidth: (\d+)", out).group(1)), int(re.search(r"pixelHeight: (\d+)", out).group(1))


def audio_duration(path: str) -> float:
    out = subprocess.run(["afinfo", path], capture_output=True, text=True, check=True).stdout
    return float(re.search(r"estimated duration: ([\d.]+) sec", out).group(1))


def _boxes(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[i:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr:
            return
        yield typ.decode("latin1"), i + hdr, i + size
        i += size


def video_info(path: str) -> dict:
    """{width, height, duration} of an MP4/MOV by walking its boxes (reads only the moov box)."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        total = fh.tell()
        pos = 0
        moov = None
        while pos + 8 <= total:
            fh.seek(pos)
            head = fh.read(16)
            size, typ = struct.unpack(">I4s", head[:8])
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", head[8:16])[0]
                hdr = 16
            elif size == 0:
                size = total - pos
            if typ == b"moov":
                fh.seek(pos + hdr)
                moov = fh.read(size - hdr)
                break
            if size < hdr:
                break
            pos += size
    if moov is None:
        raise FilmCraftError(f"{path}: no moov box (not an MP4/MOV?)")
    w = h = 0
    dur = 0.0
    for typ, a, b in _boxes(moov, 0, len(moov)):
        if typ == "mvhd":
            ver = moov[a]
            if ver == 1:
                ts, d = struct.unpack(">IQ", moov[a + 20:a + 32])
            else:
                ts, d = struct.unpack(">II", moov[a + 12:a + 20])
            dur = d / ts
        elif typ == "trak":
            for t2, a2, b2 in _boxes(moov, a, b):
                if t2 == "tkhd":
                    tw, th = struct.unpack(">II", moov[b2 - 8:b2])
                    if tw and th and not w:
                        w, h = tw >> 16, th >> 16
    if not w:
        raise FilmCraftError(f"{path}: no video track found")
    return {"width": w, "height": h, "duration": dur}


# ---------------------------------------------------------------- planning
class Timeline:
    def __init__(self, width: int, height: int, fps: int):
        self.W, self.H, self.fps = width, height, fps
        self.tpf = TICKS // fps  # ticks per frame

    def frames(self, seconds: float) -> int:
        return max(1, round(seconds * self.fps))

    def ticks(self, frames: int) -> int:
        return int(frames * self.tpf)

    def secs_ticks(self, seconds: float) -> int:
        return int(round(seconds * TICKS))


def fmt_t(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def freeze_still(src: str, t: float) -> str:
    """The frame of a video at t seconds as a PNG (needs FilmCraft; cached)."""
    key = hashlib.sha1(f"{os.path.abspath(src)}|{os.path.getmtime(src)}|{t:.3f}".encode()).hexdigest()[:12]
    return os.path.join(HERE, "out", "freeze", f"{os.path.splitext(os.path.basename(src))[0]}_{key}.png")


def plan_scene_segments(sc: dict, total_f: int, tl: Timeline) -> list[dict]:
    """Turn a scene's segment list into pieces with relative start frames that tile [0, total_f) exactly."""
    segs = sc["segments"]
    n = len(segs)
    fps = tl.fps
    alloc: list[int | None] = [None] * n
    weight = [0.0] * n
    vid_w = []
    for i, s in enumerate(segs):
        if s["type"] == "video":
            src = s["out"] - s["in"]
            if s.get("freeze"):
                s["type"] = "still"  # converted to a still (frame exported later); treated like a still here
                s.setdefault("move", "zoom-in")
                if s.get("seconds"):
                    alloc[i] = tl.frames(s["seconds"])
                else:
                    weight[i] = float(s.get("weight", -1))
                continue
            sp = s.get("speed", "fit")
            if sp != "fit":
                sp = float(sp)
                if not (0.1 <= sp <= 20):
                    raise FilmCraftError(f"scene {sc['id']} segment {i}: speed {sp} out of range 0.1-20")
                alloc[i] = max(1, round(src / sp * fps))
            else:
                weight[i] = float(s.get("weight", src))
                vid_w.append(weight[i])
        else:
            if s.get("seconds"):
                alloc[i] = tl.frames(float(s["seconds"]))
            else:
                weight[i] = float(s.get("weight", -1))
    still_default = (sum(vid_w) / len(vid_w)) if vid_w else STILL_WEIGHT_DEFAULT
    weight = [w if w >= 0 else still_default for w in weight]
    fixed = sum(a for a in alloc if a is not None)
    flex = [i for i in range(n) if alloc[i] is None]
    if flex:
        pool = max(total_f - fixed, len(flex) * max(1, fps // 2))
        wsum = sum(weight[i] for i in flex) or 1.0
        acc, prev = 0.0, 0
        for k, i in enumerate(flex):
            acc += weight[i]
            cum = pool if k == len(flex) - 1 else round(pool * acc / wsum)
            alloc[i] = max(1, cum - prev)
            prev = cum
    total = sum(alloc)
    if total != total_f:
        diff = total_f - total
        last = n - 1
        if flex:  # rounding / pool floor: absorb in the last flexible piece
            last = flex[-1]
        if abs(diff) > fps * 0.3 and not flex:
            warn(f"scene {sc['id']}: segments fill {total / fps:.1f}s but the scene is {total_f / fps:.1f}s -> "
                 f"{'holding' if diff > 0 else 'trimming'} the last segment by {abs(diff) / fps:.1f}s")
        elif abs(diff) > fps * 0.3 and flex:
            warn(f"scene {sc['id']}: flexible segments squeezed (narration shorter than fixed-speed content by {-diff / fps:.1f}s)")
        alloc[last] = max(1, alloc[last] + diff)
        # if the last piece collapsed, shave earlier ones
        deficit = sum(alloc) - total_f
        j = n - 2
        while deficit > 0 and j >= 0:
            take = min(deficit, alloc[j] - 1)
            alloc[j] -= take
            deficit -= take
            j -= 1
    pieces, cursor = [], 0
    for i, s in enumerate(segs):
        dur_f = alloc[i]
        base = {"seg": i, "start_f": cursor, "dur_f": dur_f, "spec": s}
        if s["type"] == "still":
            pieces.append({**base, "kind": "still"})
        else:
            src_in, src_out = s["in"], s["out"]
            src_f = (src_out - src_in) * fps
            ramp = s.get("ramp") if s.get("speed", "fit") == "fit" else None
            if ramp:
                a, b = float(ramp[0]), float(ramp[1])
                rel = [a + (b - a) * (k + 0.5) / RAMP_STEPS for k in range(RAMP_STEPS)]
                src_part = [(src_out - src_in) * r / sum(rel) for r in rel]
                cum_t, cum_s = 0, src_in
                for k in range(RAMP_STEPS):
                    end_t = dur_f if k == RAMP_STEPS - 1 else round(dur_f * (k + 1) / RAMP_STEPS)
                    d = max(1, end_t - cum_t)
                    sp = src_part[k] * fps / d
                    spc = min(MAX_SPEED, max(MIN_SPEED, sp))
                    if spc != sp:
                        warn(f"scene {sc['id']} segment {i} ramp step {k + 1}: speed {sp:.2f}x clamped to {spc:.2f}x (source range will not be fully used)")
                    pieces.append({**base, "kind": "video", "start_f": cursor + cum_t, "dur_f": d, "src_in": cum_s,
                                   "src_out": cum_s + src_part[k], "speed": spc, "hold_f": 0, "chunk": k, "chunks": RAMP_STEPS,
                                   "seg_start_f": cursor, "seg_dur_f": dur_f})
                    cum_t += d
                    cum_s += src_part[k]
            else:
                sp = src_f / dur_f
                hold = 0
                if sp > MAX_SPEED:
                    new_len = MAX_SPEED * dur_f / fps
                    warn(f"scene {sc['id']} segment {i} ({os.path.basename(s['path'])} {src_in:.1f}-{src_out:.1f}s): needs {sp:.1f}x to fit "
                         f"{dur_f / fps:.1f}s -> trimmed the tail to {new_len:.1f}s of source at 8x")
                    src_out, sp = src_in + new_len, MAX_SPEED
                elif sp < MIN_SPEED:
                    play = round(src_f / MIN_SPEED)
                    hold = dur_f - play
                    warn(f"scene {sc['id']} segment {i} ({os.path.basename(s['path'])} {src_in:.1f}-{src_out:.1f}s): only {src_out - src_in:.1f}s of "
                         f"source for {dur_f / fps:.1f}s -> plays at 0.5x then holds the last frame for {hold / fps:.1f}s")
                    sp = MIN_SPEED
                    dur_f = play
                pieces.append({**base, "kind": "video", "dur_f": dur_f, "src_in": src_in, "src_out": src_out, "speed": sp,
                               "hold_f": hold, "chunk": 0, "chunks": 1, "seg_start_f": cursor, "seg_dur_f": alloc[i]})
        cursor += alloc[i]
    return pieces


def build_plan(m: dict, base: str, only: set[str] | None, no_cards: bool = False) -> dict:
    W, H, fps = m.get("width", 1920), m.get("height", 1080), int(m.get("fps", 30))
    tl = Timeline(W, H, fps)
    ndir = m.get("narration_dir")
    entries = []
    if m.get("title") and not no_cards:
        entries.append({"id": "title", "kind": "card", **m["title"]})
    for sc in m["scenes"]:
        if only and sc["id"] not in only:
            continue
        entries.append({"kind": "scene", **sc})
    if m.get("outro") and not no_cards:
        entries.append({"id": "outro", "kind": "card", **m["outro"]})
    t = 0
    media_info: dict[str, dict] = {}

    def vinfo(p):
        if p not in media_info:
            if not os.path.exists(p):
                raise FilmCraftError(f"missing media file: {p}")
            media_info[p] = video_info(p)
        return media_info[p]

    for sc in entries:
        pb, pa = sc.get("pad_before", 0.4), sc.get("pad_after", 0.6)
        vo = sc.get("voiceover_wav")
        if not vo and ndir and sc["kind"] == "scene" and os.path.exists(path_of(base, os.path.join(ndir, sc["id"] + ".wav"))):
            vo = os.path.join(ndir, sc["id"] + ".wav")
        sc["vo_abs"] = path_of(base, vo) if vo else None
        vo_len = 0.0
        if sc["vo_abs"]:
            if not os.path.exists(sc["vo_abs"]):
                raise FilmCraftError(f"scene {sc['id']}: missing narration file {sc['vo_abs']}")
            vo_len = audio_duration(sc["vo_abs"])
        length = max(sc.get("seconds", 0) or 0, pb + vo_len + pa if vo_len else 0) or 4.0
        sc["pb"], sc["pa"], sc["vo_len"] = pb, pa, vo_len
        sc["start_f"], sc["dur_f"] = t, tl.frames(length)
        t += sc["dur_f"]
        segs = [dict(s) for s in (sc.get("segments") or ([sc["bg"]] if sc.get("bg") else []))]
        for s in segs:
            s.setdefault("type", "video" if str(s.get("path", "")).lower().endswith((".mp4", ".mov", ".m4v")) else "still")
            s["abs"] = path_of(base, s["path"])
            if s["type"] == "video":
                vi = vinfo(s["abs"])
                s["src_w"], s["src_h"], s["file_dur"] = vi["width"], vi["height"], vi["duration"]
                s["in"] = float(s.get("in", 0.0))
                if s.get("freeze"):
                    s["out"] = s["in"] + 1 / fps
                    if s["in"] >= vi["duration"]:
                        raise FilmCraftError(f"scene {sc['id']}: freeze time {s['in']}s beyond the end of {s['path']} ({vi['duration']:.1f}s)")
                else:
                    out = float(s.get("out", vi["duration"]))
                    if out > vi["duration"] + 0.02:
                        warn(f"scene {sc['id']}: out={out:.2f}s is beyond the end of {os.path.basename(s['abs'])} ({vi['duration']:.2f}s) -> clamped")
                        out = vi["duration"]
                    if s["in"] >= out - 0.1:
                        raise FilmCraftError(f"scene {sc['id']}: bad range in={s['in']} out={out} for {s['path']}")
                    s["out"] = out
            else:
                if not os.path.exists(s["abs"]):
                    raise FilmCraftError(f"missing media file: {s['abs']}")
                s["src_w"], s["src_h"] = image_size(s["abs"])
        sc["segments"] = segs
        sc["pieces"] = plan_scene_segments(sc, sc["dur_f"], tl) if segs else []
    return {"tl": tl, "entries": entries, "total_f": t, "base": base}


def print_plan(plan: dict, vo_total_hint: bool = True) -> None:
    tl, fps = plan["tl"], plan["tl"].fps
    print(f"\n{'scene':7} {'start':>7} {'length':>7} {'narr':>6}  segments")
    for sc in plan["entries"]:
        segs = []
        for p in sc["pieces"]:
            if p["kind"] == "still" or p.get("chunk", 0) > 0:
                if p["kind"] == "still":
                    segs.append(f"still {os.path.basename(p['spec']['abs'])} {p['dur_f'] / fps:.1f}s")
                continue
            s = p["spec"]
            extra = f" +hold {p['hold_f'] / fps:.1f}s" if p["hold_f"] else ""
            ramp = " ramp" if p["chunks"] > 1 else ""
            segs.append(f"{os.path.basename(s['abs'])} {p['src_in']:.1f}-{(s['out'] if p['chunks'] > 1 else p['src_out']):.1f}s "
                        f"-> {(p['seg_dur_f'] if p['chunks'] > 1 else p['dur_f'] + p['hold_f']) / fps:.1f}s (x{p['speed']:.2f}{ramp}){extra}")
        co = len(sc.get("callouts") or [])
        tail = f"  [{co} callouts]" if co else ""
        print(f"{sc['id']:7} {fmt_t(sc['start_f'] / fps):>7} {sc['dur_f'] / fps:6.1f}s {sc['vo_len']:5.1f}s  " + (" | ".join(segs) or "-") + tail)
    print(f"{'total':7} {'':>7} {plan['total_f'] / fps:6.1f}s {sum(sc['vo_len'] for sc in plan['entries']):5.1f}s")


def write_plan_json(plan: dict, out_path: str) -> str:
    fps = plan["tl"].fps
    scenes = []
    for sc in plan["entries"]:
        scenes.append({
            "id": sc["id"], "kind": sc["kind"], "start": sc["start_f"] / fps, "length": sc["dur_f"] / fps,
            "vo_start": (sc["start_f"] + plan["tl"].frames(sc["pb"])) / fps if sc["vo_abs"] else None, "vo_len": sc["vo_len"] or None,
            "vo_wav": sc["vo_abs"],
            "cuts": [p["start_f"] / fps + sc["start_f"] / fps for p in sc["pieces"] if p.get("chunk", 0) == 0 and p["seg"] > 0],
            "callouts": [{"at": sc["start_f"] / fps + c["at"], "dur": c["dur"], "text": c["text"]} for c in sc.get("callouts") or []],
        })
    p = os.path.splitext(os.path.abspath(out_path))[0] + ".plan.json"
    json.dump({"fps": fps, "total": plan["total_f"] / fps, "scenes": scenes}, open(p, "w"), indent=1)
    return p


# ---------------------------------------------------------------- graphics
def fade(clip: int, dur_f: int, frames: int = 8) -> None:
    frames = min(frames, dur_f // 3)
    if frames < 2:
        return
    for edge in ("in", "out"):
        ex("sequence.applyVideoTransition", {"clip": clip, "effect": "cross_dissolve", "frames": frames, "edge": edge})


def text_bounds(clip: int, layer: int = 0) -> list[float]:
    """Absolute [x0, y0, x1, y1] of a text layer (from graphics.list quad)."""
    q = ex("graphics.list", {"clip": clip})["layers"][layer]["quad"]
    return [q[0][0], q[0][1], q[2][0], q[2][1]]


def plate(tl: Timeline, text: str, *, size: int, anchor: str, y_base: float, start_f: int, dur_f: int, track: int,
          color: str = "#ffffff", box: bool = True, accent: bool = False, max_w: float | None = None,
          margin: float | None = None, opacity: int = 80) -> tuple[int, float]:
    """One graphic clip: [rounded dark box, gold accent bar, text].  anchor 'l'|'c'|'r' picks alignment.  Returns (clip, box_height)."""
    W = tl.W
    padx, pady = round(size * 0.74), round(size * 0.44)
    bar_w, bar_gap, bar_in = max(6, round(size * 0.17)), round(size * 0.42), round(size * 0.30)
    left_extra = (bar_in + bar_w + bar_gap - padx) if accent else 0  # extra room the accent bar needs on the left
    margin = margin if margin is not None else W * 0.04
    if anchor == "l":
        x_ref, al = margin + padx + max(0, left_extra), "left"
    elif anchor == "r":
        x_ref, al = W - margin - padx, "right"
    else:
        x_ref, al = W / 2 + (left_extra / 2 if accent else 0), "center"
    r = ex("graphics.newText", {"text": text, "position": [x_ref, y_base], "size": size, "seconds": dur_f / tl.fps, "track": track, "time": tl.ticks(start_f)})
    clip = r["clip"]
    props = {"font": FONT, "font_style": "Bold", "fill_color": color, "align": al, "shadow": False}
    ex("graphics.set", {"clip": clip, "layer": 0, "props": props})
    x0, y0, x1, y1 = text_bounds(clip)
    avail = (max_w or (W - 2 * margin)) - 2 * padx - max(0, left_extra)
    if x1 - x0 > avail:
        size = max(18, int(size * avail / (x1 - x0)))
        ex("graphics.set", {"clip": clip, "layer": 0, "props": {"size": size}})
        x0, y0, x1, y1 = text_bounds(clip)
    bh = 0.0
    if box:
        bx0, bx1 = x0 - padx - max(0, left_extra), x1 + padx
        by0, by1 = y0 - pady, y1 + pady
        bh = by1 - by0
        ex("graphics.newShape", {"shape": "rectangle", "clip": clip, "position": [(bx0 + bx1) / 2, (by0 + by1) / 2], "size": [bx1 - bx0, bh]})
        ex("graphics.sendToBack", {"clip": clip, "layer": 1})
        ex("graphics.set", {"clip": clip, "layer": 0, "props": {"fill_color": INK, "opacity": opacity, "corner_radius": round(size * 0.36)}})
        if accent:
            bar_h = bh - 2 * bar_in
            ex("graphics.newShape", {"shape": "rectangle", "clip": clip, "position": [bx0 + bar_in + bar_w / 2, (by0 + by1) / 2], "size": [bar_w, bar_h]})
            ex("graphics.sendBackward", {"clip": clip, "layer": 2})
            ex("graphics.set", {"clip": clip, "layer": 1, "props": {"fill_color": GOLD, "opacity": 100, "corner_radius": bar_w / 2}})
    return clip, bh


class Callouts:
    """Lane assignment so callouts at the same anchor never overlap (they stack away from the screen edge)."""

    def __init__(self):
        self.busy: dict[str, list[tuple[int, int, int]]] = {}

    def lane(self, anchor: str, a: int, b: int) -> int:
        used = {ln for (s, e, ln) in self.busy.get(anchor, []) if s < b and a < e}
        ln = 0
        while ln in used:
            ln += 1
        self.busy.setdefault(anchor, []).append((a, b, ln))
        return ln


def add_callout(tl: Timeline, co: dict, scene_start_f: int, scene_dur_f: int, lanes: Callouts) -> None:
    fps, W, H = tl.fps, tl.W, tl.H
    a = scene_start_f + tl.frames(co["at"]) if co["at"] > 0 else scene_start_f
    b = min(scene_start_f + scene_dur_f, a + tl.frames(co.get("dur", 3.0)))
    dur_f = b - a
    if dur_f < 6:
        warn(f"callout {co.get('text')!r} at {co['at']}s is outside/too close to the end of its scene -> skipped")
        return
    style = co.get("style", "tech")
    if style == "title":
        big_text(tl, co["text"], co.get("sub"), a, dur_f, dim_opacity=48)
        return
    anchor = co.get("anchor") or ("bc" if style == "caption" else "bl")
    v, h = anchor[0], anchor[1]
    size = 36 if style == "caption" else 34
    size = round(size * H / 1080)
    lane = lanes.lane(anchor, a, b)
    step = round(size * 1.3 + 2 * size * 0.44 + 18)
    margin_v = H * (0.075 if v == "t" else 0.09)
    y_base = (H - margin_v - size * 0.25 - lane * step) if v == "b" else (margin_v + size * 0.95 + lane * step)
    track = (CALL0, CALL1, CALL2)[min(lane, 2)]
    clip, _ = plate(tl, co["text"], size=size, anchor=h, y_base=y_base, start_f=a, dur_f=dur_f, track=track, accent=(style == "tech"),
                    max_w=W * 0.7)
    fade(clip, dur_f)


def big_text(tl: Timeline, text: str, sub: str | None, a: int, dur_f: int, dim_opacity: int = 62) -> None:
    W, H = tl.W, tl.H
    if dim_opacity:  # mid-scene title: dim overlay.  (Cards instead darken their bg clip with a brightness effect so dissolves into them never flash.)
        dim = ex("graphics.newRectangle", {"position": [W / 2, H / 2], "size": [W + 4, H + 4], "seconds": dur_f / tl.fps, "track": DIM, "time": tl.ticks(a)})["clip"]
        ex("graphics.set", {"clip": dim, "props": {"fill_color": INK, "opacity": dim_opacity}})
        fade(dim, dur_f, 10)
    else:  # card: a soft dark band behind the text keeps it legible over bright footage (fades in with the text, so no flash at the cut)
        top = H * 0.50 - round(H / 8.5) * 1.02
        bottom = (H * 0.62 + round(H / 21) * 0.5) if sub else (H * 0.50 + round(H / 8.5) * 0.4)
        band = ex("graphics.newRectangle", {"position": [W / 2, (top + bottom) / 2], "size": [W + 4, bottom - top], "seconds": dur_f / tl.fps, "track": DIM, "time": tl.ticks(a)})["clip"]
        ex("graphics.set", {"clip": band, "props": {"fill_color": INK, "opacity": 62}})
        fade(band, dur_f, 12)
    inner = max(1, dur_f - tl.frames(0.1))
    t1, _ = plate(tl, text, size=round(H / 8.5), anchor="c", y_base=H * 0.50, start_f=a + 2, dur_f=inner, track=TITLE1, box=False, max_w=W - 140, margin=70)
    fade(t1, inner, 10)
    if sub:
        t2, _ = plate(tl, sub, size=round(H / 21), anchor="c", y_base=H * 0.62, start_f=a + 2, dur_f=inner, track=TITLE2, color=GOLD, box=False, max_w=W - 160, margin=80)
        fade(t2, inner, 10)


def add_tag(tl: Timeline, text: str, a: int, dur_f: int) -> None:
    size = round(tl.H / 32)
    clip, _ = plate(tl, text.upper(), size=size, anchor="l", y_base=tl.H * 0.94, start_f=a, dur_f=dur_f, track=CHIP, color=GOLD,
                    margin=tl.W * 0.03, opacity=82)
    fade(clip, dur_f)


# ---------------------------------------------------------------- motion (crop / fit / Ken Burns) for stills and videos
def geometry(W: int, H: int, size: tuple[int, int], seg: dict):
    w, h = size
    x0, y0, x1, y1 = seg.get("crop") or [0, 0, 1, 1]
    rw, rh = (x1 - x0) * w, (y1 - y0) * h
    fit = seg.get("fit", "auto")
    mismatch = abs((rw / rh) / (W / H) - 1) > 0.06
    contain = fit == "contain" or (fit == "auto" and mismatch)
    s = (min(W / rw, H / rh) if contain else max(W / rw, H / rh)) * 100  # percent of native pixels
    fx, fy = seg.get("focus") or [0.5, 0.5]
    ax, ay = (x0 + (x1 - x0) * fx) * w, (y0 + (y1 - y0) * fy) * h
    cx, cy = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h
    pos = [W / 2 + (ax - cx) * s / 100, H / 2 + (ay - cy) * s / 100]
    return s, pos, (ax, ay), contain


def kb_endpoints(move: str, s: float, pos: list, ax: float, w: int, W: int, contain: bool):
    amt = 1.08
    if move == "zoom-in":
        return s, s * amt, pos, pos
    if move == "zoom-out":
        return s * amt, s, pos, pos
    if move in ("pan-left", "pan-right"):
        s2 = s * 1.07
        left_edge = pos[0] - ax * s2 / 100
        right_edge = pos[0] + (w - ax) * s2 / 100
        dx = 0.04 * W if contain else max(0.0, 0.9 * min(-left_edge, right_edge - W, 0.04 * W))
        a, b = (dx, -dx) if move == "pan-left" else (-dx, dx)
        return s2, s2, [pos[0] + a, pos[1]], [pos[0] + b, pos[1]]
    return s, s, pos, pos


def apply_motion(tl: Timeline, clips: list[tuple[int, int, int]], size: tuple[int, int], seg: dict, seg_start_f: int, seg_dur_f: int, default_move: str) -> None:
    """clips: [(clip_id, start_f, dur_f)] that together cover the segment; Ken Burns progress is continuous across them."""
    W, H = tl.W, tl.H
    s, pos, (ax, ay), contain = geometry(W, H, size, seg)
    move = seg.get("move", default_move)
    sa, sb, pa, pb = kb_endpoints(move, s, pos, ax, size[0], W, contain)
    x0, y0, x1, y1 = seg.get("crop") or [0, 0, 1, 1]
    animated = move != "none" and (sa != sb or pa != pb)
    for clip, cs, cd in clips:
        if seg.get("crop"):
            ex("effects.apply", {"clips": [clip], "effect": "crop"})
            for param, v in (("left", x0), ("top", y0), ("right", 1 - x1), ("bottom", 1 - y1)):
                if v > 0:
                    ex("effects.setParam", {"clip": clip, "effect": "crop", "param": param, "value": round(v * 100, 3)})
        ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "anchor", "value": [ax, ay]})
        if not animated:
            ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "position", "value": pa})
            ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "scale", "value": sa})
            continue
        p0 = (cs - seg_start_f) / seg_dur_f
        p1 = (cs + cd - seg_start_f) / seg_dur_f
        lerp = lambda a, b, p: a + (b - a) * p
        t0, t1 = tl.ticks(cs), tl.ticks(cs + cd) - 1
        ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "position", "value": pa})
        for param, va, vb in (("scale", lerp(sa, sb, p0), lerp(sa, sb, p1)),):
            ex("effects.setParam", {"clip": clip, "effect": "motion", "param": param, "value": va, "time": t0, "keyframe": True})
            ex("effects.setParam", {"clip": clip, "effect": "motion", "param": param, "value": vb, "time": t1, "keyframe": True})
        if pa != pb:
            va = [lerp(pa[0], pb[0], p0), lerp(pa[1], pb[1], p0)]
            vb = [lerp(pa[0], pb[0], p1), lerp(pa[1], pb[1], p1)]
            ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "position", "value": va, "time": t0, "keyframe": True})
            ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "position", "value": vb, "time": t1, "keyframe": True})


def clips_in_range(track_idx: int, a_f: int, b_f: int) -> list[tuple[int, int, int]]:
    seq = ex("sequence.inspect")
    out = []
    for it in seq["video"][track_idx]["items"]:
        if it["startFrame"] >= a_f and it["startFrame"] < b_f:
            out.append((it["clip"], it["startFrame"], it["durationFrames"]))
    return sorted(out, key=lambda c: c[1])


def place_piece(tl: Timeline, item: int, p: dict, size: tuple[int, int], start_f: int) -> list[tuple[int, int, int]]:
    """Place one video piece on V2 retimed to dur_f frames (+ tail hold). Returns [(clip, start_f, dur_f)] of the clips created."""
    fps = tl.fps
    src_len = p["src_out"] - p["src_in"]
    clip = ex("timeline.place", {"item": item, "track": "V2", "frame": start_f, "sourceIn": tl.secs_ticks(p["src_in"]),
                                 "duration": tl.secs_ticks(src_len), "audio": False})["clips"][0]
    speed = src_len * fps / p["dur_f"]
    if abs(speed - 1.0) > 1e-9:
        ex("clip.speedDuration", {"clips": [clip], "speed": speed * 100.0, "ripple": False})
    # the engine rounds the retimed length to whole frames; trim/extend the out edge to land exactly on dur_f
    got = next((c for c in clips_in_range(MAIN, start_f, start_f + 1) if c[0] == clip), None)
    if got and got[2] != p["dur_f"]:
        ex("timeline.trim", {"clip": clip, "edge": "out", "mode": "regular", "deltaFrames": p["dur_f"] - got[2]})
    if p["hold_f"] > 0:
        ex("clip.insertFrameHoldSegment", {"clip": clip, "time": tl.ticks(start_f + p["dur_f"] - 1), "seconds": p["hold_f"] / fps})
        return clips_in_range(MAIN, start_f, start_f + p["dur_f"] + p["hold_f"])
    return [(clip, start_f, p["dur_f"])]


# ---------------------------------------------------------------- frames / checks
def contact_sheet(files: list[str], path: str, cols: int = 3, tw: int = 640, aspect: float = 9 / 16) -> None:
    from PIL import Image

    th = int(tw * aspect)
    rows = (len(files) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), "black")
    for i, f in enumerate(files):
        sheet.paste(Image.open(f).convert("RGB").resize((tw, th)), ((i % cols) * tw, (i // cols) * th))
    sheet.save(path)


def frame_stats(path: str) -> tuple[float, float]:
    """(mean luma 0-255, luma std-dev) of an image on a coarse grid."""
    from PIL import Image, ImageStat

    st = ImageStat.Stat(Image.open(path).convert("L").resize((160, 90)))
    return st.mean[0], st.stddev[0]


def check_joins(joins: list[tuple[str, float, float]], grab, outdir: str) -> list[str]:
    """joins: [(label, time_s, half_width_s)].  grab(t, path) writes a PNG.  Reports black frames and brightness flashes."""
    os.makedirs(outdir, exist_ok=True)
    problems = []
    print(f"\n{'join':14} {'t':>8}  luma(before)  luma(cut)  luma(after)   verdict")
    for label, t, hw in joins:
        vals = []
        for dt in (-hw, 0.0, hw):
            path = os.path.join(outdir, f"join_{label}_{t + dt:08.3f}.png")
            grab(max(0.0, t + dt), path)
            vals.append(frame_stats(path))
        (mb, sb), (mc, sc), (ma, sa) = vals
        verdict = "ok"
        if sc < 2.5 and mc < 14 and (mb > 18 or ma > 18):
            verdict = "BLACK/BLANK at the cut"
        elif mc > max(mb, ma) + 22 or mc < min(mb, ma) - 22:
            verdict = f"FLASH (cut luma {mc:.0f} vs {mb:.0f}/{ma:.0f})"
        if verdict != "ok":
            problems.append(f"{label} @ {t:.2f}s: {verdict}")
        print(f"{label:14} {t:8.2f}  {mb:6.1f}/{sb:4.1f}  {mc:6.1f}/{sc:4.1f}  {ma:6.1f}/{sa:4.1f}   {verdict}")
    return problems


# ---------------------------------------------------------------- export
def faststart(out_abs: str) -> None:
    tmp = out_abs[:-4] + ".fs.mp4"
    r = subprocess.run(["avconvert", "--preset", "PresetPassthrough", "--source", out_abs, "--output", tmp, "--replace"], capture_output=True, text=True)
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0.9 * os.path.getsize(out_abs):
        os.replace(tmp, out_abs)
    else:
        print(f"warning: faststart remux failed, keeping the original: {r.stderr.strip()[:200]}", file=sys.stderr)
        if os.path.exists(tmp):
            os.remove(tmp)


def export_fit(out_abs: str, preset: str, dur_s: float, max_mb: float, audio_kbps: int, size: tuple[int, int] | None, do_faststart: bool,
               bitrate_mode: str = "vbr1Pass", burn: bool = False) -> float:
    """Export H.264/AAC, fitting the size budget by choosing the video bitrate (retry up to twice if over)."""
    audio = {"bitrate_kbps": audio_kbps, "codec": "aac", "sample_rate": 48000, "channels": 2, "bits": 16}
    kbps = None
    if max_mb:
        kbps = int((max_mb * 8000 / dur_s - audio_kbps) * 0.90)
    mb = 0.0
    for attempt in range(3):
        settings: dict = {"audio": audio}
        if burn:
            settings["burnCaptions"] = True
        if size:
            settings["frameSize"] = list(size)
        if kbps:
            settings.update({"bitrateKbps": kbps, "adaptiveBitrate": None, "bitrateMode": bitrate_mode, "maxBitrateKbps": int(kbps * 1.6)})
        if os.path.exists(out_abs):
            os.remove(out_abs)
        t0 = time.time()
        # wait:true gives up after ~60 s inside FilmCraft ("timeout"), so start the job and poll jobs.list ourselves
        job = ex("export.quick", {"preset": preset, "path": out_abs, "wait": False, "settings": settings})["job"]
        last_print = t0
        while True:
            j = next((x for x in ex("jobs.list", {}) if x["id"] == job), None)
            if j is None:
                raise FilmCraftError(f"export job {job} vanished from jobs.list")
            if j.get("finished"):
                break
            if time.time() - last_print > 30:
                print(f"    exporting ... {100 * (j.get('progress') or 0):.0f}%")
                last_print = time.time()
            time.sleep(1.0)
        if not j.get("result"):
            raise FilmCraftError(f"export failed: {j.get('status')}")
        took = time.time() - t0
        if not os.path.exists(out_abs):
            raise FilmCraftError(f"export finished but {out_abs} does not exist")
        mb = os.path.getsize(out_abs) / 1e6
        print(f"  export attempt {attempt + 1}: {mb:.1f} MB at {kbps or 'preset'} kbps video ({took:.0f}s)")
        if not max_mb or mb <= max_mb or attempt == 2:
            break
        kbps = int(kbps * (max_mb * 0.96) / mb)
        print(f"  over budget ({max_mb} MB) -> retrying at {kbps} kbps")
    if do_faststart:
        faststart(out_abs)
    info = subprocess.run(["afinfo", out_abs], capture_output=True, text=True).stdout
    dur = re.search(r"estimated duration: ([\d.]+) sec", info)
    mb = os.path.getsize(out_abs) / 1e6
    print(f"exported {out_abs}  {mb:.1f} MB  {float(dur.group(1)):.2f}s" + ("" if not max_mb or mb <= max_mb else f"   OVER the {max_mb} MB budget"))
    return mb


# ---------------------------------------------------------------- build
def build(manifest_path: str, out_path: str, a) -> int:
    base = os.path.dirname(os.path.abspath(manifest_path))
    m = json.load(open(manifest_path))
    only = set(a.scenes.split(",")) if a.scenes else None
    plan = build_plan(m, base, only, a.no_cards)
    tl: Timeline = plan["tl"]
    W, H, fps = tl.W, tl.H, tl.fps
    entries, total_f = plan["entries"], plan["total_f"]
    print_plan(plan)
    plan_json = write_plan_json(plan, out_path)
    srt_path = None
    if m.get("captions") and not a.no_captions:
        srt_path = os.path.splitext(os.path.abspath(out_path))[0] + ".srt"
        subprocess.run([sys.executable, os.path.join(HERE, "narration", "captions.py"), "srt", plan_json, srt_path, "--vtt", os.path.splitext(srt_path)[0] + ".vtt"], check=True)
    if a.plan_only:
        return 0

    # ---- media to import
    media: list[str] = []

    def want(p):
        if p not in media:
            media.append(p)

    for sc in entries:
        for s in sc["segments"]:
            want(s["abs"])
        if sc["vo_abs"]:
            want(sc["vo_abs"])
    music = path_of(base, m["music"]) if m.get("music") else None
    if music:
        want(music)

    ensure_filmcraft()
    try:
        ex("file.closeProject", {"force": True})
    except FilmCraftError:
        pass
    stem = os.path.splitext(os.path.basename(out_path))[0]
    ex("file.newProject", {"name": stem})
    ex("file.newSequence", {"name": "Walkthrough", "width": W, "height": H, "fps": fps, "video": N_VIDEO_TRACKS, "audio": 2})
    imp = ex("file.import", {"paths": media}, timeout=600)
    if imp.get("errors"):
        raise FilmCraftError(f"import errors: {imp['errors']}")
    item_of = dict(zip(media, imp["items"]))

    # freeze segments: export the frame to a PNG and use that as a still
    # (segments flagged freeze were converted to type 'still' during planning; their abs path still points at the video)
    frozen: dict[tuple[str, float], str] = {}
    for sc in entries:
        for s in sc["segments"]:
            if s["type"] == "still" and str(s["abs"]).lower().endswith((".mp4", ".mov", ".m4v")):
                key = (s["abs"], s["in"])
                if key not in frozen:
                    png = freeze_still(s["abs"], s["in"])
                    os.makedirs(os.path.dirname(png), exist_ok=True)
                    if not os.path.exists(png):
                        ex("file.exportFrame", {"path": png, "format": "png", "target": "source", "item": item_of[s["abs"]], "time": tl.secs_ticks(s["in"])}, timeout=60)
                    frozen[key] = png
                    ii = ex("file.import", {"paths": [png]})["items"][0]
                    item_of[png] = ii
                png = frozen[key]
                s["abs"] = png
                s["src_w"], s["src_h"] = image_size(png)

    # ---- main track, chronological
    seg_first_clip: list[tuple[int, int, int, float | None]] = []  # (clip, abs_start_f, abs_dur_f, xfade_s) for each *segment* (not chunk)
    t_build = time.time()
    for sc in entries:
        sstart = sc["start_f"]
        done_segs: dict[int, list[tuple[int, int, int]]] = {}
        seg_meta: dict[int, dict] = {}
        for p in sc["pieces"]:
            s = p["spec"]
            abs_start = sstart + p["start_f"]
            size = (s["src_w"], s["src_h"])
            if p["kind"] == "still":
                clip = ex("timeline.place", {"item": item_of[s["abs"]], "track": "V2", "frame": abs_start, "duration": tl.ticks(p["dur_f"])})["clips"][0]
                clips = [(clip, abs_start, p["dur_f"])]
            else:
                clips = place_piece(tl, item_of[s["abs"]], p, size, abs_start)
            done_segs.setdefault(p["seg"], []).extend(clips)
            seg_meta[p["seg"]] = p
        for sidx, clips in sorted(done_segs.items()):
            p = seg_meta[sidx]
            s = p["spec"]
            size = (s["src_w"], s["src_h"])
            seg_abs_start = sstart + (p["seg_start_f"] if p["kind"] == "video" else p["start_f"])
            seg_dur = sum(c[2] for c in clips)
            apply_motion(tl, clips, size, s, seg_abs_start, seg_dur, "zoom-in" if p["kind"] == "still" else "none")
            if sc["kind"] == "card":  # dim the bg itself (a brightness effect, so dissolves into the card never flash)
                for c in clips:
                    ex("effects.apply", {"clips": [c[0]], "effect": "brightness_contrast"})
                    ex("effects.setParam", {"clip": c[0], "effect": "brightness_contrast", "param": "brightness", "value": float(sc.get("dim", -90))})
            xf = s.get("xfade", sc.get("xfade") if sidx == 0 else None)
            seg_first_clip.append((clips[0][0], seg_abs_start, seg_dur, xf))
        print(f"  placed scene {sc['id']} ({len(sc['pieces'])} pieces)  [{time.time() - t_build:.0f}s]")

    # ---- backdrop (created AFTER the main track: clip.insertFrameHoldSegment is a ripple insert on all sync-locked tracks and would split it)
    bd = ex("graphics.newRectangle", {"position": [W / 2, H / 2], "size": [W + 4, H + 4], "seconds": total_f / fps, "track": BACK, "time": 0})["clip"]
    ex("graphics.set", {"clip": bd, "props": {"fill_color": INK, "opacity": 100}})

    # ---- narration, music
    for sc in entries:
        if sc["vo_abs"]:
            ex("timeline.place", {"item": item_of[sc["vo_abs"]], "track": "A1", "frame": sc["start_f"] + tl.frames(sc["pb"])})
    if any(sc["vo_abs"] for sc in entries):
        # mono narration normalised to ~-16 LUFS lands on BOTH stereo channels at unity (+3 dB on a stereo BS.1770 meter, peaks -0.3 dBFS);
        # -3 dB on A1 makes the stereo programme ~-16 LUFS with peaks ~-3 dBFS
        ex("timeline.setTrack", {"track": "A1", "volumeDb": float(m.get("narration_db", -3.0))})
    if music:
        ex("timeline.place", {"item": item_of[music], "track": "A2", "frame": 0, "duration": tl.ticks(total_f), "video": False})
        ex("timeline.setTrack", {"track": "A2", "volumeDb": float(m.get("music_db", -22))})

    # ---- dissolves between consecutive segments, fades at both ends
    xfade_default = m.get("xfade", 0.35)
    prev_dur = None
    for k, (clip, st, du, xf) in enumerate(seg_first_clip):
        if k > 0:
            secs = xfade_default if xf is None else xf
            frames = min(tl.frames(secs), int(0.9 * min(du, prev_dur))) if secs else 0
            if frames >= 2:
                ex("sequence.applyVideoTransition", {"clip": clip, "effect": "cross_dissolve", "frames": frames, "edge": "in"})
        prev_dur = du
    if seg_first_clip:
        ex("sequence.applyVideoTransition", {"clip": seg_first_clip[0][0], "effect": "cross_dissolve", "frames": 12, "edge": "in"})
        last = clips_in_range(MAIN, 0, total_f)[-1]
        ex("sequence.applyVideoTransition", {"clip": last[0], "effect": "cross_dissolve", "frames": 15, "edge": "out"})

    # ---- text: cards, tags, callouts
    lanes = Callouts()
    for sc in entries:
        sstart, sdur = sc["start_f"], sc["dur_f"]
        gap = tl.frames(0.12)
        if sc["kind"] == "card":
            if sc.get("text"):
                big_text(tl, sc["text"], sc.get("sub"), sstart, sdur, dim_opacity=0)
        else:
            if sc.get("tag"):
                add_tag(tl, sc["tag"], sstart + tl.frames(0.25), sdur - tl.frames(0.25) - gap)
        for co in sc.get("callouts") or []:
            add_callout(tl, co, sstart, sdur, lanes)

    seq = ex("sequence.inspect")
    built_s = seq["durationFrames"] / fps
    print(f"timeline built: {built_s:.2f}s (planned {total_f / fps:.2f}s)  [{time.time() - t_build:.0f}s]")
    if abs(built_s - total_f / fps) > 1.5 / fps:
        warn(f"timeline length {built_s:.2f}s differs from plan {total_f / fps:.2f}s")
    out_abs = os.path.abspath(out_path)
    if srt_path:
        ex("captions.newTrack", {"format": "Subtitle", "name": "Narration", "language": "en"})
        ex("captions.import", {"path": srt_path, "name": "Narration"})
        cs = m.get("caption_style") or {}
        ex("captions.setStyle", {"track": "C1", "font": cs.get("font", "Inter"), "size": cs.get("size", 44), "color": "#ffffff", "background": True,
                                 "backgroundColor": cs.get("background", "#000000b3"), "align": "center", "anchor": "bottom", "margin": cs.get("margin", 0.06),
                                 "outline": 0})
        n_caps = len(ex("captions.list", {"track": "C1"}) or [])
        print(f"captions imported: {n_caps} cues  ({srt_path})")
    ex("file.saveAs", {"path": os.path.splitext(out_abs)[0] + ".fcproj"})

    fdir = os.path.join(os.path.dirname(out_abs), f"frames_{stem}")
    if a.frames or a.at or a.joins:
        shutil.rmtree(fdir, ignore_errors=True)
        os.makedirs(fdir)
    total_s = total_f / fps
    grab = lambda t, path: ex("file.exportFrame", {"path": path, "format": "png", "target": "program", "time": int(round(t * TICKS))}, timeout=60)
    if a.frames:
        files = []
        for i in range(a.frames):
            f = os.path.join(fdir, f"f_{i + 1:02d}.png")
            grab((i + 0.5) / a.frames * total_s, f)
            files.append(f)
        contact_sheet(files, os.path.join(fdir, "sheet.png"), aspect=H / W)
        print(f"frames: {fdir}/sheet.png")
    if a.at:
        for t in [float(x) for x in a.at.split(",")]:
            grab(t, os.path.join(fdir, f"at_{t:08.3f}.png"))
        print(f"frames at {a.at}: {fdir}")
    if a.joins:
        joins = []
        for sc in entries:
            if sc["start_f"] > 0:
                joins.append((f"scene-{sc['id']}", sc["start_f"] / fps, 0.3))
            for p in sc["pieces"]:
                if p["seg"] > 0 and p.get("chunk", 0) == 0 and p["start_f"] > 0:
                    joins.append((f"{sc['id']}-cut{p['seg']}", (sc["start_f"] + p["start_f"]) / fps, 0.3))
        probs = check_joins(joins, grab, os.path.join(fdir, "joins"))
        print("join problems: " + ("none" if not probs else "; ".join(probs)))

    if not a.no_export:
        preset = a.preset or m.get("preset", "Match Source – Adaptive Low Bitrate")
        max_mb = a.max_mb if a.max_mb is not None else m.get("max_mb", 60)
        t0 = time.time()
        export_fit(out_abs, preset, built_s, max_mb, int(m.get("audio_kbps", 128)), None, not a.no_faststart, a.bitrate_mode or m.get("bitrate_mode", "vbr1Pass"), burn=bool(srt_path))
        print(f"  main export took {time.time() - t0:.0f}s")
        if a.small and m.get("small") is not False:
            sm = m.get("small") or {"width": 1280, "height": 720, "max_mb": 28, "audio_kbps": 96}
            small_abs = os.path.splitext(out_abs)[0] + "_720p.mp4"
            t0 = time.time()
            export_fit(small_abs, preset, built_s, sm.get("max_mb", 28), int(sm.get("audio_kbps", 96)), (sm.get("width", 1280), sm.get("height", 720)), not a.no_faststart,
                       a.bitrate_mode or m.get("bitrate_mode", "vbr1Pass"), burn=bool(srt_path))
            print(f"  small export took {time.time() - t0:.0f}s")
    if WARNINGS:
        print(f"\n{len(WARNINGS)} warning(s):")
        for w in WARNINGS:
            print("  - " + w)
    if not a.keep_project:
        try:
            ex("file.closeProject", {"force": True})
        except FilmCraftError:
            pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("out")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--scenes")
    ap.add_argument("--no-cards", action="store_true")
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--at")
    ap.add_argument("--joins", action="store_true")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--small", action="store_true")
    ap.add_argument("--max-mb", type=float, default=None)
    ap.add_argument("--preset")
    ap.add_argument("--bitrate-mode", choices=["vbr1Pass", "cbr"], help="default vbr1Pass (lands within a few %% of the target; vbr2Pass overshot and looked worse in tests)")
    ap.add_argument("--no-faststart", action="store_true")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--keep-project", action="store_true")
    a = ap.parse_args()
    try:
        return build(a.manifest, a.out, a)
    except FilmCraftError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
