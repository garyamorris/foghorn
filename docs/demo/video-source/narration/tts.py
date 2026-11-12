#!/usr/bin/env python3
"""OpenAI TTS narration for the "Probably a Tragedy" walkthrough.

The API key is read from the repo's .env *inside this script*; it is never printed, logged or put on a command line.

USAGE (run from anywhere):
    python3 tts.py models                       list tts-ish models the key can see (GET /v1/models)
    python3 tts.py samples [voice ...]          same 2 sentences in several voices -> narration/samples/<voice>.wav
    python3 tts.py scenes [--voice V] [--only A,B] [--force]
                                                synthesise every scene of script.json -> narration/audio/<id>.wav
                                                (raw takes are cached in narration/cache/ by hash of model|voice|instructions|text)
    python3 tts.py normalise                    trim silence, loudness-normalise to ~-16 LUFS, peak <= -1 dBFS,
                                                write narration/durations.json  ({"A": 12.3, ..., "_total": 245.6})
    python3 tts.py all [--voice V]              scenes + normalise
    python3 tts.py check                        re-measure every audio/<id>.wav (duration, peak, LUFS)
    python3 tts.py transcribe [--only A,B]      (optional, ~1c) transcribe the wavs with gpt-4o-mini-transcribe and diff against the script
                                                to catch mispronunciations (written to narration/transcripts.json)

CONFIG: narration/tts_config.json (created on first run) {"model","voice","instructions","fallback_model"}; change "voice" to
re-voice the whole video (then `python3 tts.py all`).  Pronunciation fixes are spelling tweaks in PRONUNCIATION below; every
applied tweak is recorded in narration/pronunciation_log.json.  script.json is never modified.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import ssl
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "..", "..", "..", "..", ".env")  # the repo .env, if any; the OPENAI_API_KEY environment variable wins
SCRIPT = os.path.join(HERE, "script.json")
CACHE = os.path.join(HERE, "cache")
AUDIO = os.path.join(HERE, "audio")
SAMPLES = os.path.join(HERE, "samples")
CONFIG = os.path.join(HERE, "tts_config.json")
API = "https://api.openai.com/v1"
# python.org builds ship without root certificates; verify against the macOS system bundle (or certifi) instead of disabling checks
for _ca in ("/etc/ssl/cert.pem",):
    if os.path.exists(_ca):
        SSL_CTX = ssl.create_default_context(cafile=_ca)
        break
else:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())

DEFAULT_CONFIG = {
    "model": "gpt-4o-mini-tts",
    "fallback_model": "tts-1-hd",
    "voice": "marin",
    "instructions": (
        "Voice: a warm, wry, intelligent British-leaning storyteller explaining something delightful to developer friends. "
        "Pace: unhurried but engaged, never rushed, with natural pauses between ideas and a slightly longer breath at full stops. "
        "Tone: friendly and conversational with a slight smile, dry humour when it is earned, not a corporate announcer. "
        "Pronunciation: say technical terms clearly and precisely (LangGraph, reducer, checkpoint, interrupt, LangSmith)."
    ),
}

# Spelling tweaks applied to the text sent to TTS (not to script.json).  (regex, replacement, reason)
PRONUNCIATION = [
    (r"\bthread id\b", "thread I D", "make sure 'id' is read as the letters, not as Freud's id"),
    (r"\binterrupt id\b", "interrupt I D", "read 'id' as the letters"),
]

SAMPLE_TEXT = (
    "A tour has five towns. Each town loves one kind of play and scorns another, and that taste lives in generated "
    "datasets that the graphs read as they run. First, the casting call: eight puppets audition at once, "
    "which is a fan-out, merged by a reducer."
)
SAMPLE_VOICES = ["marin", "cedar", "fable", "ballad"]


# ---------------------------------------------------------------- plumbing
def api_key() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"].strip()
    if not os.path.exists(ENV_PATH):
        raise SystemExit("OPENAI_API_KEY is not set and no .env found")
    with open(ENV_PATH) as fh:
        for line in fh:
            m = re.match(r"\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise SystemExit("OPENAI_API_KEY not found in .env")


def load_config() -> dict:
    if not os.path.exists(CONFIG):
        json.dump(DEFAULT_CONFIG, open(CONFIG, "w"), indent=2)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.load(open(CONFIG)))
    return cfg


def http(method: str, path: str, body: dict | None = None, raw: bool = False, timeout: float = 180):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + api_key())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        detail = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", detail)  # never echo a key fragment
        raise RuntimeError(f"HTTP {exc.code} on {method} {path}: {detail}") from None
    return data if raw else json.loads(data)


def afinfo_duration(path: str) -> float:
    out = subprocess.run(["afinfo", path], capture_output=True, text=True, check=True).stdout
    return float(re.search(r"estimated duration: ([\d.]+) sec", out).group(1))


def apply_pronunciation(text: str, scene_id: str, log: list) -> str:
    out = text
    for pat, rep, why in PRONUNCIATION:
        new = re.sub(pat, rep, out, flags=re.I)
        if new != out:
            log.append({"scene": scene_id, "pattern": pat, "replacement": rep, "why": why})
            out = new
    return out


def synth(text: str, voice: str, cfg: dict, dest: str, model: str | None = None) -> str:
    """Synthesise (cached) -> 48 kHz 16-bit mono wav at dest. Returns the model actually used."""
    model = model or cfg["model"]
    instr = cfg["instructions"] if model.startswith("gpt-4o") else ""
    key = hashlib.sha1("|".join([model, voice, instr, text]).encode()).hexdigest()[:16]
    os.makedirs(CACHE, exist_ok=True)
    raw = os.path.join(CACHE, f"{key}.wav")
    if not os.path.exists(raw):
        body = {"model": model, "voice": voice, "input": text, "response_format": "wav"}
        if instr:
            body["instructions"] = instr
        t0 = time.time()
        data = http("POST", "/audio/speech", body, raw=True)
        with open(raw + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(raw + ".tmp", raw)
        print(f"  synthesised {os.path.basename(dest)} ({model}/{voice}, {len(text)} chars, {time.time() - t0:.1f}s)")
    else:
        print(f"  cached      {os.path.basename(dest)} ({model}/{voice})")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", raw, dest], check=True)
    return model


# ---------------------------------------------------------------- commands
def cmd_models(_a) -> None:
    ids = sorted(m["id"] for m in http("GET", "/models")["data"])
    print("\n".join(i for i in ids if "tts" in i or "speech" in i or "audio" in i or "transcribe" in i or "whisper" in i))


def pick_model(cfg: dict) -> dict:
    """Use cfg['model'] if the key can see it, else the best tts model listed."""
    ids = {m["id"] for m in http("GET", "/models")["data"]}
    if cfg["model"] in ids:
        return cfg
    for cand in (cfg.get("fallback_model"), "tts-1-hd", "tts-1"):
        if cand and cand in ids:
            print(f"warning: {cfg['model']} unavailable, falling back to {cand}")
            cfg = dict(cfg, model=cand)
            return cfg
    raise SystemExit("no usable TTS model on this key")


def cmd_samples(a) -> None:
    cfg = pick_model(load_config())
    voices = a.voices or SAMPLE_VOICES
    log: list = []
    text = apply_pronunciation(SAMPLE_TEXT, "sample", log)
    for v in voices:
        try:
            synth(text, v, cfg, os.path.join(SAMPLES, f"{v}.wav"))
        except RuntimeError as exc:
            print(f"  voice {v}: {exc}")
    for f in sorted(os.listdir(SAMPLES)):
        if f.endswith(".wav"):
            print(f"  samples/{f}: {afinfo_duration(os.path.join(SAMPLES, f)):.1f}s")


def cmd_scenes(a) -> None:
    cfg = pick_model(load_config())
    voice = a.voice or cfg["voice"]
    scenes = json.load(open(SCRIPT))["scenes"]
    only = set(a.only.split(",")) if a.only else None
    plog: list = []
    os.makedirs(AUDIO, exist_ok=True)
    used = None
    for sc in scenes:
        if only and sc["id"] not in only:
            continue
        text = apply_pronunciation(sc["text"], sc["id"], plog)
        dest = os.path.join(AUDIO, f"{sc['id']}.wav")
        pre = os.path.join(CACHE, "pre_norm", f"{sc['id']}.wav")
        if os.path.exists(pre):  # a fresh take invalidates the saved un-normalised copy
            os.remove(pre)
        used = synth(text, voice, cfg, dest)
    json.dump({"model": used or cfg["model"], "voice": voice, "instructions": cfg["instructions"], "fixes": plog},
              open(os.path.join(HERE, "pronunciation_log.json"), "w"), indent=2)
    print(f"model={used} voice={voice}; pronunciation fixes applied: {len(plog)}")


# ---------------------------------------------------------------- audio maths (pure python, no numpy)
def read_wav(path: str) -> tuple[array.array, int]:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, "expected 16-bit mono"
        sr = w.getframerate()
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        a.byteswap()
    return a, sr


def write_wav(path: str, samples, sr: int) -> None:
    a = array.array("h", samples)
    if sys.byteorder == "big":
        a.byteswap()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(a.tobytes())


def peak_db(a) -> float:
    pk = max(max(a), -min(a)) if len(a) else 0
    return 20 * math.log10(max(pk, 1) / 32768)


def _biquad(x, b0, b1, b2, a1, a2):
    y = array.array("d", bytes(8 * len(x)))
    x1 = x2 = y1 = y2 = 0.0
    for i, s in enumerate(x):
        o = b0 * s + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[i] = o
        x2, x1, y2, y1 = x1, s, y1, o
    return y


def lufs(a, sr: int = 48000) -> float:
    """ITU-R BS.1770 integrated loudness (mono, K-weighted, 400 ms blocks / 75 % overlap, -70 abs gate, -10 LU rel gate)."""
    if sr != 48000:
        raise ValueError("lufs() expects 48 kHz")
    x = array.array("d", (s / 32768.0 for s in a))
    # stage 1: high shelf, stage 2: RLB high-pass (coefficients for 48 kHz from the standard)
    x = _biquad(x, 1.53512485958697, -2.69169618940638, 1.19839281085285, -1.69065929318241, 0.73248077421585)
    x = _biquad(x, 1.0, -2.0, 1.0, -1.99004745483398, 0.99007225036621)
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    if len(x) < blk:
        return -70.0
    sq = array.array("d", (v * v for v in x))
    cs = array.array("d", [0.0]) * (len(sq) + 1)
    acc = 0.0
    for i, v in enumerate(sq):
        acc += v
        cs[i + 1] = acc
    blocks = [(cs[i + blk] - cs[i]) / blk for i in range(0, len(sq) - blk + 1, hop)]
    ldb = lambda z: -0.691 + 10 * math.log10(z) if z > 0 else -200.0
    keep = [z for z in blocks if ldb(z) > -70]
    if not keep:
        return -70.0
    rel = ldb(sum(keep) / len(keep)) - 10
    keep = [z for z in keep if ldb(z) > rel]
    return ldb(sum(keep) / len(keep)) if keep else -70.0


def trim_silence(a, sr: int, head_ms=70, tail_ms=220, thresh_db=-48):
    """Cut leading/trailing near-silence, leaving a little breath, with tiny fades. TTS takes carry ~0.2 s of lead-in."""
    th = int(32768 * 10 ** (thresh_db / 20))
    n = len(a)
    first = next((i for i in range(n) if abs(a[i]) > th), 0)
    last = next((i for i in range(n - 1, -1, -1) if abs(a[i]) > th), n - 1)
    s = max(0, first - int(sr * head_ms / 1000))
    e = min(n, last + 1 + int(sr * tail_ms / 1000))
    out = array.array("h", a[s:e])
    f_in, f_out = int(sr * 0.012), int(sr * 0.06)
    for i in range(min(f_in, len(out))):
        out[i] = int(out[i] * i / f_in)
    for i in range(min(f_out, len(out))):
        out[len(out) - 1 - i] = int(out[len(out) - 1 - i] * i / f_out)
    return out


def apply_gain_limit(a, gain_db: float, ceiling_db: float = -1.2):
    """Gain, then a smooth look-ahead-free peak limiter (instant attack via per-sample gain, 120 ms release) so peaks <= ceiling."""
    g = 10 ** (gain_db / 20)
    ceil = 32768 * 10 ** (ceiling_db / 20)
    out = array.array("h", bytes(2 * len(a)))
    # compute required gain reduction per sample, smooth with a forward-looking min-filter over 2 ms then release
    n = len(a)
    need = array.array("d", [1.0]) * n
    for i, s in enumerate(a):
        v = abs(s) * g
        if v > ceil:
            need[i] = ceil / v
    # spread each reduction 48 samples backward (1 ms look-behind smoothing) and release slowly forward
    rel = math.exp(-1.0 / (0.12 * 48000))
    red = array.array("d", [1.0]) * n
    cur = 1.0
    for i in range(n - 1, -1, -1):  # backward pass: anticipation ramp (attack ~2 ms)
        cur = min(need[i], cur + (1 - cur) * 0.0104 if cur < 1 else 1.0)
        red[i] = cur
    cur = 1.0
    for i in range(n):  # forward pass: release
        cur = min(red[i], 1 - (1 - cur) * rel)
        red[i] = cur
    for i, s in enumerate(a):
        v = s * g * red[i]
        out[i] = max(-32768, min(32767, int(round(v))))
    return out


def cmd_normalise(a) -> None:
    target = -16.0
    ids = [sc["id"] for sc in json.load(open(SCRIPT))["scenes"]]
    results, durs = {}, {}
    for sid in ids:
        src_dir = os.path.join(CACHE, "pre_norm")
        os.makedirs(src_dir, exist_ok=True)
        path = os.path.join(AUDIO, f"{sid}.wav")
        if not os.path.exists(path):
            print(f"  {sid}: missing {path}")
            continue
        pre = os.path.join(src_dir, f"{sid}.wav")
        if not os.path.exists(pre):  # keep the untouched take so normalise is idempotent
            shutil.copy(path, pre)
        x, sr = read_wav(pre)
        x = trim_silence(x, sr)
        l0 = lufs(x, sr)
        gain = target - l0
        y = apply_gain_limit(x, gain)
        l1, pk = lufs(y, sr), peak_db(y)
        write_wav(path, y, sr)
        durs[sid] = round(len(y) / sr, 3)
        results[sid] = (l0, l1, pk, gain)
        print(f"  {sid}: {durs[sid]:6.2f}s  LUFS {l0:6.1f} -> {l1:6.1f}  gain {gain:+5.1f} dB  peak {pk:5.1f} dBFS")
    durs["_total"] = round(sum(v for k, v in durs.items() if k != "_total"), 3)
    json.dump(durs, open(os.path.join(HERE, "durations.json"), "w"), indent=1)
    print(f"total narration {durs['_total']:.1f}s -> durations.json")


def cmd_check(_a) -> None:
    for sid in [sc["id"] for sc in json.load(open(SCRIPT))["scenes"]]:
        p = os.path.join(AUDIO, f"{sid}.wav")
        if not os.path.exists(p):
            print(f"{sid}: MISSING")
            continue
        x, sr = read_wav(p)
        print(f"{sid}: {len(x) / sr:6.2f}s (afinfo {afinfo_duration(p):.2f}s)  sr {sr}  peak {peak_db(x):5.1f} dBFS  LUFS {lufs(x, sr):6.1f}")


def cmd_transcribe(a) -> None:
    """Optional intelligibility check: transcribe each wav and show words in the script that the transcript lacks."""
    import uuid
    scenes = {sc["id"]: sc["text"] for sc in json.load(open(SCRIPT))["scenes"]}
    only = set(a.only.split(",")) if a.only else set(scenes)
    out = {}
    for sid, text in scenes.items():
        if sid not in only:
            continue
        data = open(os.path.join(AUDIO, f"{sid}.wav"), "rb").read()
        b = uuid.uuid4().hex
        parts = []
        for k, v in (("model", "gpt-4o-mini-transcribe"), ("response_format", "json")):
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        req = urllib.request.Request(API + "/audio/transcriptions", method="POST", data=b"".join(parts))
        req.add_header("Authorization", "Bearer " + api_key())
        req.add_header("Content-Type", f"multipart/form-data; boundary={b}")
        try:
            heard = json.loads(urllib.request.urlopen(req, timeout=180, context=SSL_CTX).read())["text"]
        except urllib.error.HTTPError as exc:
            print(f"  {sid}: transcription failed HTTP {exc.code}")
            continue
        norm = lambda s: re.findall(r"[a-z0-9']+", s.lower())
        want, got = norm(text), set(norm(heard))
        missing = [w for w in want if w not in got]
        out[sid] = {"heard": heard, "missing_words": missing}
        print(f"  {sid}: missing from transcript: {missing or 'none'}")
    json.dump(out, open(os.path.join(HERE, "transcripts.json"), "w"), indent=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models")
    s = sub.add_parser("samples")
    s.add_argument("voices", nargs="*")
    for name in ("scenes", "all"):
        s = sub.add_parser(name)
        s.add_argument("--voice")
        s.add_argument("--only")
        s.add_argument("--force", action="store_true")
    sub.add_parser("normalise")
    sub.add_parser("check")
    s = sub.add_parser("transcribe")
    s.add_argument("--only")
    a = ap.parse_args()
    try:
        if a.cmd == "models":
            cmd_models(a)
        elif a.cmd == "samples":
            cmd_samples(a)
        elif a.cmd == "scenes":
            cmd_scenes(a)
        elif a.cmd == "normalise":
            cmd_normalise(a)
        elif a.cmd == "all":
            cmd_scenes(a)
            cmd_normalise(a)
        elif a.cmd == "check":
            cmd_check(a)
        elif a.cmd == "transcribe":
            cmd_transcribe(a)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
