#!/usr/bin/env python3
"""Decode an exported MP4 and check it against the build plan.  Works for both build_video.py (v1) and build_video2.py output.

usage: python3 verify_mp4.py video.mp4 [N=12] [--at 12.5,60] [--joins] [--sync] [--plan video.plan.json]

  N          write N evenly spaced frames + contact sheet           -> out/mp4frames_<stem>/f_01.png ... sheet.png   (N=0: skip)
  --at t,..  write PNG frames of the *file* at these seconds        -> out/mp4frames_<stem>/at_<t>.png
  --joins    sample frames before / at / after every scene boundary and segment cut listed in the plan; report black frames and flashes
  --sync     decode the audio track (afconvert -> wav) and compare the first speech onset of every narrated scene with the plan
             (planned onset = vo_start + ~70 ms of kept lead-in); also compares total duration with the plan
  --plan P   plan json (default <mp4 without .mp4>.plan.json, written by build_video2.py)
The file is decoded through FilmCraft (placed on a fresh 1920x1080 timeline), so this proves the MP4 itself decodes with video and audio.
"""
import array, math, os, re, subprocess, sys, tempfile, wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_video2 as bv


def audio_onsets(mp4: str, plan: dict) -> None:
    tmp = os.path.join(tempfile.mkdtemp(), "a.wav")
    stereo = tmp[:-4] + "_st.wav"
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", mp4, stereo], capture_output=True, text=True)
    if os.path.exists(stereo):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "narration"))
        import tts
        with wave.open(stereo, "rb") as w:
            st = array.array("h")
            st.frombytes(w.readframes(w.getnframes()))
        left, right = st[0:240 * 48000:2], st[1:240 * 48000:2]  # first 120 s (pure-python loudness is slow)
        ll, lr = tts.lufs(left), tts.lufs(right)
        print(f"audio (stereo): peak L {tts.peak_db(left):.1f} / R {tts.peak_db(right):.1f} dBFS, loudness L {ll:.1f} / R {lr:.1f} LUFS per channel "
              f"-> stereo programme {10 * math.log10(10 ** (ll / 10) + 10 ** (lr / 10)):.1f} LUFS (BS.1770 sum)")
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", mp4, tmp], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        print("audio: afconvert could not decode the file's audio:", r.stderr.strip()[:200])
        return
    with wave.open(tmp, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        a = array.array("h")
        a.frombytes(w.readframes(n))
    dur = n / sr
    win = int(0.01 * sr)
    env = [math.sqrt(sum(x * x for x in a[i:i + win]) / win) / 32768 for i in range(0, len(a) - win, win)]  # 10 ms RMS
    db = [20 * math.log10(max(e, 1e-6)) for e in env]
    print(f"audio: decoded {dur:.2f}s (afconvert mono sum, peak {20 * math.log10(max(max(a), -min(a)) / 32768):.1f} dBFS)")
    print(f"plan total {plan['total']:.2f}s vs audio {dur:.2f}s (diff {dur - plan['total']:+.2f}s)")
    def onset(db_list, t0, t1):
        i0, i1 = max(0, int(t0 * 100)), min(len(db_list), int(t1 * 100))
        return next((i / 100 for i in range(i0, i1) if db_list[i] > -42 and all(d > -50 for d in db_list[i:i + 3])), None)

    def env_db(samples):
        w = int(0.01 * sr)
        return [20 * math.log10(max(math.sqrt(sum(x * x for x in samples[i:i + w]) / w) / 32768, 1e-6)) for i in range(0, len(samples) - w, w)]

    print(f"{'scene':8} {'planned onset':>13} {'measured':>9} {'delta':>7}")
    worst = 0.0
    for sc in plan["scenes"]:
        if not sc.get("vo_start"):
            continue
        ref = array.array("h")
        with wave.open(sc["vo_wav"], "rb") as w:
            ref.frombytes(w.readframes(w.getnframes()))
        ref_on = onset(env_db(ref), 0, 3)  # onset inside the narration file itself, same detector
        plan_on = sc["vo_start"] + (ref_on or 0.0)
        on = onset(db, sc["vo_start"] - 0.3, sc["vo_start"] + 1.2)
        if on is None:
            print(f"{sc['id']:8} {plan_on:12.2f}s {'NONE':>9}   no speech found near the planned start")
            continue
        worst = max(worst, abs(on - plan_on))
        print(f"{sc['id']:8} {plan_on:12.2f}s {on:8.2f}s {on - plan_on:+7.2f}")
    print(f"worst narration onset error: {worst * 1000:.0f} ms  ({'OK' if worst < 0.12 else 'CHECK'})")


def main():
    args = sys.argv[1:]
    mp4 = os.path.abspath(args.pop(0))
    n, at, do_joins, do_sync, plan_path = 12, None, False, False, None
    while args:
        x = args.pop(0)
        if x == "--at":
            at = [float(v) for v in args.pop(0).split(",")]
        elif x == "--joins":
            do_joins = True
        elif x == "--sync":
            do_sync = True
        elif x == "--plan":
            plan_path = args.pop(0)
        else:
            n = int(x)
    stem = os.path.splitext(os.path.basename(mp4))[0]
    plan_path = plan_path or re.sub(r"(_720p)?\.mp4$", "", mp4) + ".plan.json"
    plan = None
    if (do_joins or do_sync) and os.path.exists(plan_path):
        import json
        plan = json.load(open(plan_path))
    elif do_joins or do_sync:
        print(f"no plan file {plan_path}; --joins/--sync need one (build_video2.py writes it next to the output)")
    if plan and do_sync:
        audio_onsets(mp4, plan)

    bv.ensure_filmcraft()
    try:
        bv.ex("file.closeProject", {"force": True})
    except bv.FilmCraftError:
        pass
    bv.ex("file.newProject", {"name": "verify"})
    bv.ex("file.newSequence", {"name": "v", "width": 1920, "height": 1080, "fps": 30, "video": 1, "audio": 1})
    item = bv.ex("file.import", {"paths": [mp4]}, timeout=120)["items"][0]
    clip = bv.ex("timeline.place", {"item": item, "track": "V1", "frame": 0})["clips"][0]
    vi = bv.video_info(mp4)
    print(f"file: {vi['width']}x{vi['height']}  {os.path.getsize(mp4) / 1e6:.1f} MB")
    if vi["width"] != 1920:  # the check sequence is 1920x1080: scale smaller files up to fill it
        bv.ex("effects.setParam", {"clip": clip, "effect": "motion", "param": "scale", "value": 1920 / vi["width"] * 100})
    seq = bv.ex("sequence.inspect")
    dur = seq["duration"] / bv.TICKS
    kinds = {"video": sum(len(t["items"]) for t in seq["video"]), "audio": sum(len(t["items"]) for t in seq["audio"])}
    print(f"decoded: {dur:.2f}s, clips placed: {kinds} (video>0 and audio>0 means both streams decode)")
    if plan:
        print(f"plan total {plan['total']:.2f}s vs file {dur:.2f}s (diff {dur - plan['total']:+.2f}s)")
    out = os.path.join(os.path.dirname(mp4), f"mp4frames_{stem}")
    os.makedirs(out, exist_ok=True)
    grab = lambda t, path: bv.ex("file.exportFrame", {"path": path, "format": "png", "target": "program", "time": int(round(t * bv.TICKS))}, timeout=60)
    if n:
        files = []
        for i in range(n):
            f = os.path.join(out, f"f_{i + 1:02d}.png")
            grab((i + 0.5) / n * dur, f)
            files.append(f)
        bv.contact_sheet(files, os.path.join(out, "sheet.png"))
        print("sheet:", os.path.join(out, "sheet.png"))
    for t in at or []:
        p = os.path.join(out, f"at_{t:08.3f}.png")
        grab(t, p)
        print("frame:", p)
    if plan and do_joins:
        joins = []
        for sc in plan["scenes"]:
            if sc["start"] > 0:
                joins.append((f"scene-{sc['id']}", sc["start"], 0.3))
            for k, c in enumerate(sc["cuts"]):
                joins.append((f"{sc['id']}-cut{k + 1}", c, 0.3))
        probs = bv.check_joins(joins, grab, os.path.join(out, "joins"))
        print("join problems: " + ("none" if not probs else "; ".join(probs)))
    bv.ex("file.closeProject", {"force": True})


main()
