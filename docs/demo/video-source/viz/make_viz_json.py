"""Collect durations + caption timings of every viz recording into V/viz_out/viz.json (times are seconds from the start of each mp4)."""
import json, os, datetime as dt
V = "/private/tmp/claude-501/-Users-garyamorris--claude-spares-spare-atCWLt/582aca5a-4121-49b5-9502-a211be2e784a/scratchpad/video"
B = "http://127.0.0.1:8799"
PAGES = {
    "concept": f"{B}/live_graph.html?graph=casting_call&concept=1",
    "live_casting": f"{B}/live_graph.html?graph=casting_call&scenario=casting",
    "live_writers": f"{B}/live_graph.html?graph=writers_room&scenario=writers",
    "live_performance": f"{B}/live_graph.html?graph=performance&scenario=performance",
    "live_almanac": f"{B}/live_graph.html?graph=almanac&scenario=almanac",
    "trace": f"{B}/trace.html",
    "overview": f"{B}/overview.html",
}
out = {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "note": "t = seconds from the start of the mp4; captions are the burned-in caption strip texts (concept = call-outs, trace = step labels, overview = card reveal order)", "files": {}}
for name, url in PAGES.items():
    f = f"{V}/viz_out/{name}.mp4"
    if not os.path.exists(f):
        continue
    meta = json.load(open(f + ".meta.json"))
    marks = json.load(open(f + ".markers.json"))
    caps = []
    ev = {}
    for m in marks:
        d = m.get("data") or {}
        if m["name"] == "caption":
            caps.append({"t": round(m["t"], 2), "text": d.get("text", ""), "trigger": d.get("at")})
        elif m["name"] == "callout":
            caps.append({"t": round(m["t"], 2), "text": f'{d.get("text")}: {d.get("sub")}', "trigger": "callout"})
        elif m["name"] == "step":
            caps.append({"t": round(m["t"], 2), "text": d.get("text", ""), "trigger": "step"})
        elif m["name"] == "card":
            caps.append({"t": round(m["t"], 2), "text": d.get("id", ""), "trigger": "card"})
        elif m["name"] in ("run-start", "run-end", "trace-start", "trace-end"):
            ev[m["name"]] = round(m["t"], 2)
            if d.get("thread"): ev["thread_id"] = d["thread"]
            if "checkpoints" in d: ev["checkpoints"] = d["checkpoints"]
    out["files"][name] = {"file": f"viz_out/{name}.mp4", "duration": round(meta["duration"], 2), "size": f'{meta["width"]}x{meta["height"]}@{meta["fps"]}', "url": url, "events": ev, "captions": caps}
json.dump(out, open(f"{V}/viz_out/viz.json", "w"), indent=1, ensure_ascii=False)
for n, v in out["files"].items():
    print(f'{n:18s} {v["duration"]:6.2f}s  {len(v["captions"])} captions')
