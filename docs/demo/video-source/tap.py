#!/usr/bin/env python3
"""tap.py: record every LangGraph run on a server as it happens (the "recorded runs" the video is built from).

    python3 tap.py http://127.0.0.1:2031 capture/run1 [--seconds 600] [--structure]

* Writes capture/<name>/structure/<graph_id>.json  (GET /assistants/{id}/graph?xray=true, the REAL graph structure) once.
* Polls POST /threads/search for new threads; for every run on them joins GET /threads/{t}/runs/{r}/stream with
  Last-Event-ID: -1 (replays from the start, then follows live; a join must not send stream_mode) and appends each SSE event
  to capture/<name>/events.jsonl as {"t": seconds since tap start, "thread_id", "run_id", "graph_id", "event", "data"}.
* Also records thread metadata (graph, scenario, incident) to capture/<name>/threads.json so the viz can label things.
Standard library only (threads + http.client), so it runs anywhere.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import threading
import time
import urllib.parse

lock = threading.Lock()


class Api:
    def __init__(self, base: str):
        u = urllib.parse.urlparse(base)
        self.host, self.port = u.hostname, u.port or 80

    def conn(self, timeout=30):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def req(self, method, path, body=None):
        c = self.conn()
        try:
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
            r = c.getresponse()
            data = r.read()
            return json.loads(data) if data else None
        finally:
            c.close()


def structure(api: Api, outdir: str) -> None:
    os.makedirs(os.path.join(outdir, "structure"), exist_ok=True)
    assistants = api.req("POST", "/assistants/search", {"limit": 50})
    seen = {}
    for a in assistants:
        gid = a["graph_id"]
        if gid in seen or a.get("metadata", {}).get("created_by") != "system":
            continue
        seen[gid] = a["assistant_id"]
        g = api.req("GET", f"/assistants/{a['assistant_id']}/graph?xray=true")
        json.dump({"graph_id": gid, "assistant_id": a["assistant_id"], "description": a.get("description"), "graph": g}, open(os.path.join(outdir, "structure", f"{gid}.json"), "w"), indent=1)
    print("structure:", sorted(seen))


def sse_follow(api: Api, thread_id: str, run_id: str, graph_id: str, t0: float, out) -> None:
    c = api.conn(timeout=900)
    try:
        c.request("GET", f"/threads/{thread_id}/runs/{run_id}/stream?cancel_on_disconnect=false", headers={"Last-Event-ID": "-1", "Accept": "text/event-stream"})
        r = c.getresponse()
        ev, data = None, []
        while True:
            line = r.readline()
            if not line:
                break
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if ev is not None or data:
                    raw = "\n".join(data)
                    try:
                        payload = json.loads(raw) if raw else None
                    except Exception:
                        payload = raw
                    rec = {"t": round(time.monotonic() - t0, 3), "thread_id": thread_id, "run_id": run_id, "graph_id": graph_id, "event": ev, "data": payload}
                    with lock:
                        out.write(json.dumps(rec, default=str) + "\n")
                        out.flush()
                ev, data = None, []
            elif line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
    except Exception as exc:  # noqa: BLE001
        print(f"[tap] stream {thread_id[:8]}/{run_id[:8]} ended: {exc!r}")
    finally:
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("outdir")
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--structure", action="store_true")
    a = ap.parse_args()
    api = Api(a.base)
    os.makedirs(a.outdir, exist_ok=True)
    if a.structure:
        structure(api, a.outdir)
    out = open(os.path.join(a.outdir, "events.jsonl"), "a", encoding="utf-8")
    t0 = time.monotonic()
    known_runs: set[str] = set()
    threads: dict[str, dict] = {}
    # Anything that already exists is ignored: only runs that start after the tap does are recorded.
    seen_stamp: dict[str, tuple] = {}
    for th in api.req("POST", "/threads/search", {"limit": 200}) or []:
        seen_stamp[th["thread_id"]] = (th.get("updated_at"), th.get("status"))
        for r in api.req("GET", f"/threads/{th['thread_id']}/runs") or []:
            known_runs.add(r["run_id"])
    print(f"[tap] ignoring {len(known_runs)} pre-existing runs; recording for {a.seconds:.0f}s")
    workers: list[threading.Thread] = []
    try:
        while time.monotonic() - t0 < a.seconds:
            try:
                ths = api.req("POST", "/threads/search", {"limit": 100, "sort_by": "created_at", "sort_order": "desc"}) or []
            except Exception as exc:  # noqa: BLE001
                print("[tap] search failed", exc)
                time.sleep(1)
                continue
            for th in ths:
                tid = th["thread_id"]
                stamp = (th.get("updated_at"), th.get("status"))
                if seen_stamp.get(tid) == stamp:
                    continue  # nothing changed on this thread since we last listed its runs
                seen_stamp[tid] = stamp
                runs = api.req("GET", f"/threads/{tid}/runs") or []
                for r in runs:
                    if r["run_id"] in known_runs:
                        continue
                    known_runs.add(r["run_id"])
                    gid = (th.get("metadata") or {}).get("graph_id") or r.get("metadata", {}).get("graph_id") or "?"
                    threads.setdefault(tid, {"thread_id": tid, "graph_id": gid, "metadata": th.get("metadata"), "created_at": th.get("created_at"), "runs": []})["runs"].append({"run_id": r["run_id"], "t_seen": round(time.monotonic() - t0, 2), "assistant_id": r.get("assistant_id")})
                    print(f"[tap] +{time.monotonic() - t0:6.1f}s  {gid:20s} thread {tid[:8]} run {r['run_id'][:8]}")
                    w = threading.Thread(target=sse_follow, args=(api, tid, r["run_id"], gid, t0, out), daemon=True)
                    w.start()
                    workers.append(w)
            json.dump(list(threads.values()), open(os.path.join(a.outdir, "threads.json"), "w"), indent=1, default=str)
            time.sleep(0.35)
    except KeyboardInterrupt:
        pass
    print("[tap] stopping; waiting for streams to flush")
    for w in workers:
        w.join(timeout=5)
    out.close()


if __name__ == "__main__":
    main()
