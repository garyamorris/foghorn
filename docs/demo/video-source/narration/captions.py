#!/usr/bin/env python3
"""captions.py: word-accurate transcripts and captions for the narration (the "better transcriptions").

    python3 captions.py words [--only A,B]    transcribe every narration/audio/<id>.wav with OpenAI whisper-1 (word timestamps)
                                              -> narration/words.json, and print a heard-vs-script report (mispronunciations show up as
                                              script words the transcriber did not hear)
    python3 captions.py cues                  align the SCRIPT text (ground truth) to the heard word times and cut caption cues
                                              (<= 2 lines of <= 42 chars, 1.0-5.0 s, broken at sentence ends / commas first)
                                              -> narration/cues.json  {scene: [{start, end, text}]}  (times relative to the scene wav)
    python3 captions.py srt PLAN.json OUT.srt [--vtt OUT.vtt]
                                              join the cues with the scene start times of a build plan (build_video2 writes <out>.plan.json)

The caption TEXT always comes from script.json (so it is spelled correctly, with the right punctuation); only the TIMES come from the
transcriber. Script words that were not matched get interpolated times.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tts  # noqa: E402  (reuses api_key() and the CA bundle handling; never prints the key)

SCRIPT = os.path.join(HERE, "script.json")
AUDIO = os.path.join(HERE, "audio")
WORDS = os.path.join(HERE, "words.json")
CUES = os.path.join(HERE, "cues.json")
LINE_MAX, LINES, MIN_DUR, MAX_DUR = 42, 2, 1.0, 5.0


def norm(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower().replace("-", " "))


def transcribe_words(path: str) -> list[dict]:
    data = open(path, "rb").read()
    b = uuid.uuid4().hex
    parts = []
    for k, v in (("model", "whisper-1"), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word"), ("language", "en")):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(tts.API + "/audio/transcriptions", method="POST", data=b"".join(parts))
    req.add_header("Authorization", "Bearer " + tts.api_key())
    req.add_header("Content-Type", f"multipart/form-data; boundary={b}")
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=180, context=tts.SSL_CTX).read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"transcription failed HTTP {exc.code}: {exc.read()[:200]!r}")
    return [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in j.get("words", [])]


def cmd_words(only: set[str] | None) -> None:
    scenes = {s["id"]: s["text"] for s in json.load(open(SCRIPT))["scenes"]}
    out = json.load(open(WORDS)) if os.path.exists(WORDS) else {}
    for sid, text in scenes.items():
        if only and sid not in only:
            continue
        ws = transcribe_words(os.path.join(AUDIO, f"{sid}.wav"))
        out[sid] = ws
        heard = set(norm(" ".join(w["word"] for w in ws)))
        missing = [w for w in norm(text) if w not in heard]
        dur = ws[-1]["end"] if ws else 0
        print(f"  {sid}: {len(ws)} words heard, ends {dur:.1f}s, script words not heard: {missing or 'none'}")
    json.dump(out, open(WORDS, "w"), indent=1)


def align(script_text: str, heard: list[dict], dur: float) -> list[tuple[str, float, float]]:
    """Return [(script_word_as_written, start, end)] with times from the transcript where matched, interpolated elsewhere."""
    sw = script_text.split()
    skeys = [(" ".join(norm(w)) or w.lower()) for w in sw]
    hkeys = [" ".join(norm(w["word"])) for w in heard]
    sm = difflib.SequenceMatcher(a=skeys, b=hkeys, autojunk=False)
    start = [None] * len(sw)
    end = [None] * len(sw)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                start[i1 + k], end[i1 + k] = heard[j1 + k]["start"], heard[j1 + k]["end"]
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                start[i1 + k], end[i1 + k] = heard[j1 + k]["start"], heard[j1 + k]["end"]
    # interpolate the gaps
    i = 0
    while i < len(sw):
        if start[i] is None:
            j = i
            while j < len(sw) and start[j] is None:
                j += 1
            t0 = end[i - 1] if i > 0 else 0.0
            t1 = start[j] if j < len(sw) else dur
            n = j - i
            for k in range(n):
                start[i + k] = t0 + (t1 - t0) * k / n
                end[i + k] = t0 + (t1 - t0) * (k + 1) / n
            i = j
        else:
            i += 1
    return [(sw[k], start[k], end[k]) for k in range(len(sw))]


def make_cues(aligned: list[tuple[str, float, float]]) -> list[dict]:
    cues, cur = [], []

    def text_of(ws):
        return " ".join(w for w, _, _ in ws)

    def flush():
        nonlocal cur
        if not cur:
            return
        cues.append({"start": round(cur[0][1], 3), "end": round(cur[-1][2], 3), "text": wrap(text_of(cur))})
        cur = []

    cap = LINE_MAX * LINES
    for k, w in enumerate(aligned):
        cur.append(w)
        t = text_of(cur)
        dur = cur[-1][2] - cur[0][1]
        word = w[0]
        sentence_end = word.endswith((".", "?", "!"))
        soft = word.endswith((",", ":", ";"))
        nxt_len = len(aligned[k + 1][0]) + 1 if k + 1 < len(aligned) else 0
        too_long = len(t) + nxt_len > cap
        if sentence_end and dur >= MIN_DUR:
            flush()
        elif (soft and (len(t) > cap * 0.55 or dur >= 2.6)) or too_long or dur >= MAX_DUR:
            flush()
    flush()
    # merge cues that are too short into their neighbour
    merged: list[dict] = []
    for c in cues:
        if merged and (c["end"] - c["start"] < MIN_DUR) and len((merged[-1]["text"] + " " + c["text"]).replace("\n", " ")) <= cap:
            merged[-1]["end"] = c["end"]
            merged[-1]["text"] = wrap((merged[-1]["text"] + " " + c["text"]).replace("\n", " "))
        else:
            merged.append(c)
    return merged


def wrap(text: str) -> str:
    if len(text) <= LINE_MAX:
        return text
    words = text.split()
    best, best_cost = None, 1e9
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) > LINE_MAX or len(b) > LINE_MAX:
            continue
        cost = abs(len(a) - len(b))
        if cost < best_cost:
            best, best_cost = f"{a}\n{b}", cost
    return best or text


def cmd_cues() -> None:
    scenes = {s["id"]: s["text"] for s in json.load(open(SCRIPT))["scenes"]}
    words = json.load(open(WORDS))
    out = {}
    for sid, text in scenes.items():
        heard = words[sid]
        dur = heard[-1]["end"] if heard else 0
        out[sid] = make_cues(align(text, heard, dur))
        print(f"  {sid}: {len(out[sid])} cues")
    json.dump(out, open(CUES, "w"), indent=1, ensure_ascii=False)


def ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def cmd_srt(plan_path: str, out_srt: str, out_vtt: str | None) -> None:
    plan = json.load(open(plan_path))
    cues = json.load(open(CUES))
    rows = []
    for sc in plan["scenes"]:
        sid = sc["id"]
        if sid not in cues:
            continue
        off = sc.get("vo_start")
        if off is None:
            continue
        for c in cues[sid]:
            rows.append((off + c["start"], off + c["end"], c["text"]))
    with open(out_srt, "w", encoding="utf-8") as f:
        for n, (a, b, t) in enumerate(rows, 1):
            f.write(f"{n}\n{ts_srt(a)} --> {ts_srt(b)}\n{t}\n\n")
    if out_vtt:
        with open(out_vtt, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for a, b, t in rows:
                f.write(f"{ts_srt(a).replace(',', '.')} --> {ts_srt(b).replace(',', '.')}\n{t}\n\n")
    print(f"wrote {len(rows)} cues -> {out_srt}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("words")
    s.add_argument("--only")
    sub.add_parser("cues")
    s = sub.add_parser("srt")
    s.add_argument("plan")
    s.add_argument("out")
    s.add_argument("--vtt")
    a = ap.parse_args()
    if a.cmd == "words":
        cmd_words(set(a.only.split(",")) if a.only else None)
    elif a.cmd == "cues":
        cmd_cues()
    else:
        cmd_srt(a.plan, a.out, a.vtt)
