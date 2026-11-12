#!/usr/bin/env python3
"""Extract the incident_commander window that streams.html replays, from a recorded run.

    python3 viz/make_streams_data.py [capdir] [--out viz/streams_data.json]
    capdir (relative to docs/demo/video-source, default captures/real; work/rehearsal_cap = the offline development run)

Input : <capdir>/events.jsonl + threads.json (see viz/BRIEF.md). Output: only REAL events of ONE incident_commander thread (the
first one with two runs: the run that paused at an interrupt and the run that resumed it), in recorded order, in four contiguous
slices ("scenes"), trimmed so the page can show them as short JSON snippets:

  A  hop 1: supervisor narration -> handoff -> specialists running in parallel (tool calls, interleaved token chunks, evidence)
  B  rca_tournament: Send fan-out of hypotheses, hypothesis.update, decision
  C  supervisor hands off to comms_drafter + remediation_planner (parallel), policy checks, approval interrupt (end of run 1)
  D  the resumed run (run 2): checkpoint per superstep, execute / verify / close, token chunks

Thinning (never reordering): `values`/`metadata` events are not stream modes of interest and are dropped; the analyst middleware
tasks (Analyst.before_model / after_agent) are dropped; a long token-chunk burst keeps its first KEEP chunks (+ a "skipped" count);
repeated evidence / policy / fan-out events are capped. Every kept event carries `i` = its line number in events.jsonl.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
CAP = _args[0] if _args else "captures/real"
SRC = ROOT / CAP
OUT = HERE / "streams_data.json"
if "--out" in sys.argv:
    OUT = Path(sys.argv[sys.argv.index("--out") + 1])
    OUT = OUT if OUT.is_absolute() else ROOT / OUT
KEEP_CHUNKS = 7
MIDDLEWARE = {"Analyst.before_model", "Analyst.after_agent"}
MODES = ("updates", "messages", "custom", "tasks", "checkpoints")


NOISE = {"additional_kwargs", "response_metadata", "invalid_tool_calls", "usage_metadata", "id", "tool_call_chunks", "chunk_position", "example"}


def text_of(content):
    """LangChain message content is a string, or (OpenAI Responses API) a list of blocks: keep only the visible text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def clean(o):
    """Drop LangChain message boilerplate so the interesting keys (type, content, tool_calls) survive trimming."""
    if isinstance(o, dict):
        if "type" in o and "content" in o and o.get("type") in ("ai", "tool", "human", "AIMessageChunk", "system"):
            o = {k: v for k, v in o.items() if k not in NOISE and not (k == "name" and v is None) and not (k == "tool_calls" and not v) and k != "tool_call_id" and k != "status" and k != "artifact"}
            o["content"] = text_of(o.get("content"))
            if o.get("content") == "" and o.get("tool_calls"):
                o.pop("content")
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(x) for x in o]
    return o


def trim(o, depth=0, sl=64, arr=3, keys=7, maxd=5):
    """Deep-trim a JSON value for display: long strings cut with an ellipsis, long lists/dicts truncated with a marker."""
    if depth == 0:
        o = clean(o)
    if isinstance(o, str):
        return o if len(o) <= sl else o[: sl - 1] + "…"
    if isinstance(o, (int, float, bool)) or o is None:
        return o
    if depth >= maxd:
        return "…"
    if isinstance(o, list):
        out = [trim(x, depth + 1, sl, arr, keys, maxd) for x in o[:arr]]
        if len(o) > arr:
            out.append(f"…+{len(o) - arr}")
        return out
    if isinstance(o, dict):
        out = {}
        for k, v in list(o.items())[:keys]:
            out[k] = trim(v, depth + 1, sl, arr, keys, maxd)
        if len(o) > keys:
            out["…"] = f"+{len(o) - keys} keys"
        return out
    return str(o)


def load():
    ev = []
    for i, line in enumerate(open(SRC / "events.jsonl")):
        e = json.loads(line)
        e["i"] = i
        ev.append(e)
    return ev


def pick_thread(threads):
    cands = [t for t in threads if t["graph_id"] == "incident_commander"]
    two = [t for t in cands if len(t.get("runs", [])) >= 2]
    return (two or cands)[0]


def split_event(name):
    mode, _, ns = name.partition("|")
    return mode, ns.split(":")[0] if ns else ""


def compact(e):
    """-> display record or None."""
    mode, ns = split_event(e["event"])
    d = e["data"]
    if mode not in MODES:
        return None
    base = {"i": e["i"], "t": round(e["t"], 3), "mode": mode, "ns": ns}
    if mode == "tasks":
        if "result" in d or "error" in d and "input" not in d:
            if d.get("name") in MIDDLEWARE:
                return None
            base["v"] = {"phase": "done", "name": d["name"], "id": d["id"][:8], "error": d.get("error"),
                         "result": trim(d.get("result"), sl=48, arr=2, keys=4, maxd=3), "interrupts": len(d.get("interrupts") or [])}
        else:
            if d.get("name") in MIDDLEWARE:
                return None
            inp = d.get("input")
            base["v"] = {"phase": "start", "name": d["name"], "id": d["id"][:8], "triggers": d.get("triggers"),
                         "input": trim(inp, sl=40, arr=2, keys=4, maxd=2)}
        return base
    if mode == "updates":
        if isinstance(d, dict) and d and all(k in MIDDLEWARE for k in d):
            return None
        base["v"] = trim(d, sl=56, arr=2, keys=3, maxd=5)
        return base
    if mode == "custom":
        base["v"] = {"kind": d.get("kind"), "agent": d.get("agent"), "key": d.get("key"),
                     "data": trim(d.get("data"), sl=60, arr=2, keys=5, maxd=3)}
        return base
    if mode == "messages":
        msg, meta = d[0], d[1] if len(d) > 1 else {}
        v = {"node": meta.get("langgraph_node"), "id": (msg.get("id") or "")[-8:], "type": msg.get("type"),
             "content": text_of(msg.get("content"))}
        if msg.get("tool_calls"):
            v["tool_calls"] = trim(msg["tool_calls"], sl=40, arr=2, keys=3, maxd=3)
        if msg.get("type") == "tool":
            v["tool"] = msg.get("name")
            v["content"] = (v["content"] or "")[:200]
        base["v"] = v
        return base
    if mode == "checkpoints":
        cfg = (d.get("config") or {}).get("configurable", {})
        par = (d.get("parent_config") or {}).get("configurable", {})
        base["v"] = {"id": cfg.get("checkpoint_id"), "parent": par.get("checkpoint_id"),
                     "step": (d.get("metadata") or {}).get("step"), "next": d.get("next"),
                     "nkeys": len(d.get("values") or {}), "tasks": [t.get("name") for t in (d.get("tasks") or [])][:3]}
        return base
    return None


def find(evs, pred, start=0):
    for k in range(start, len(evs)):
        if pred(evs[k]):
            return k
    return None


def is_text_chunk(c):
    v = c["v"]
    return c["mode"] == "messages" and v.get("type") == "AIMessageChunk" and not any((tc or {}).get("name") for tc in (v.get("tool_calls") or [])) and bool(v.get("content"))


def select_chunks(recs):
    """Which token chunks of a scene to show -> {rec index: {"before": n_chunks_skipped_before, "skipped": n_after}}.
    Concurrent message streams (parallel specialists) are shown as one common time window, so their chunks stay interleaved exactly as
    recorded; a lone stream shows its first KEEP_CHUNKS chunks (an opening <tag ...> markup of the model output is skipped)."""
    groups = {}
    for k, c in enumerate(recs):
        if is_text_chunk(c):
            groups.setdefault((c["ns"], c["v"]["id"]), []).append(k)
    if not groups:
        return {}
    iv = {g: (recs[ks[0]]["t"], recs[ks[-1]]["t"]) for g, ks in groups.items()}
    # moment with the most concurrently streaming groups
    pts = sorted({t for a, b in iv.values() for t in (a, b)})
    best_n, w0 = 0, None
    for t in pts:
        n = sum(1 for a, b in iv.values() if a <= t < b or (a == b == t))
        if n > best_n:
            best_n, w0 = n, t
    chosen = {}
    if best_n >= 2:
        active = [g for g, (a, b) in iv.items() if a <= w0 < b]
        # every concurrent stream contributes its first KEEP_CHUNKS chunks from the common start w0: recorded order keeps them interleaved
        for g in active:
            chosen[g] = [k for k in groups[g] if recs[k]["t"] >= w0][:KEEP_CHUNKS]
    for g, ks in groups.items():
        if g in chosen:
            continue
        # skip the chunks of an opening <tag ...> and keep the next KEEP_CHUNKS
        i0, buf = 0, ""
        if recs[ks[0]]["v"]["content"].startswith("<"):
            for j, k in enumerate(ks):
                buf += recs[k]["v"]["content"]
                if ">" in buf:
                    i0 = j + 1
                    break
        chosen[g] = ks[i0 : i0 + KEEP_CHUNKS]
    out = {}
    for g, sel in chosen.items():
        ks = groups[g]
        if not sel:
            continue
        before = ks.index(sel[0])
        after = len(ks) - 1 - ks.index(sel[-1])
        for k in sel:
            out[k] = {}
        out[sel[0]]["before"] = before
        out[sel[-1]]["skipped"] = after
    return out


def main():
    threads = json.load(open(SRC / "threads.json"))
    th = pick_thread(threads)
    run_ids = [r["run_id"] for r in th["runs"]][:2]  # run 1 + the run that resumed after the interrupt
    all_ev = load()
    E = [e for e in all_ev if e["thread_id"] == th["thread_id"] and e["run_id"] in run_ids]

    def top(e, mode, phase=None, name=None):
        m, ns = split_event(e["event"])
        if m != mode or ns:
            return False
        d = e["data"]
        if mode == "tasks":
            ph = "done" if ("result" in d) else "start"
            return (phase is None or ph == phase) and (name is None or d.get("name") == name)
        return True

    def cust(e, kind):
        return split_event(e["event"])[0] == "custom" and isinstance(e["data"], dict) and e["data"].get("kind") == kind

    scenes = []
    # A: first supervisor start -> the 2nd supervisor start (hop 1: the parallel specialists have all finished)
    a0 = find(E, lambda e: top(e, "tasks", "start", "supervisor"))
    a1 = find(E, lambda e: top(e, "tasks", "start", "supervisor"), a0 + 1)
    specialists = {e["data"]["name"] for e in E[a0:a1] if top(e, "tasks", "start") and e["data"]["name"] != "supervisor"}
    nspec = len(specialists)
    scenes.append(("handoff", f"hop 1: the supervisor hands off to {nspec} specialists that run in parallel", a0, a1 - 1))
    # B: rca_tournament start .. done
    b0 = find(E, lambda e: top(e, "tasks", "start", "rca_tournament"))
    b1 = find(E, lambda e: top(e, "tasks", "done", "rca_tournament"), b0)
    scenes.append(("rca", "rca_tournament: a Send fan-out of hypothesis tasks, scored in parallel", b0, b1))
    # C: the next supervisor hop (hands off to comms_drafter + remediation_planner) .. end of run 1 (the interrupt)
    c0 = find(E, lambda e: top(e, "tasks", "start", "supervisor"), b1)
    if c0 is None:
        c0 = find(E, lambda e: top(e, "tasks", "start", "policy_check"))
    r2 = find(E, lambda e: e["run_id"] == run_ids[-1]) if len(run_ids) > 1 else None
    c1 = (r2 - 1) if r2 else len(E) - 1
    scenes.append(("interrupt", "supervisor fans out again, policy checks pass, then the graph pauses for a human (interrupt)", c0, c1))
    # D: resumed run until the report artifact + the checkpoint that follows it
    if r2 is not None:
        art = find(E, lambda e: cust(e, "artifact"), r2)
        d1 = find(E, lambda e: split_event(e["event"])[0] == "checkpoints", art) if art is not None else None
        d1 = d1 if d1 is not None else len(E) - 1
        scenes.append(("resume", "resumed after approval: a checkpoint every superstep, then close", r2, d1))

    out_events, out_scenes = [], []
    for name, caption, lo, hi in scenes:
        seg = E[lo : hi + 1]
        recs = [c for c in (compact(e) for e in seg) if c is not None]
        keep_chunks = select_chunks(recs)
        kept, n_for = [], {}
        for ri, c in enumerate(recs):
            v = c["v"]
            mode = c["mode"]
            if mode == "messages":
                if is_text_chunk(c):
                    if ri not in keep_chunks:
                        continue
                    c["v"].update(keep_chunks[ri])
                else:
                    has_calls = any((tc or {}).get("name") for tc in (v.get("tool_calls") or []))
                    if v.get("type") == "AIMessageChunk" and not has_calls:
                        continue  # empty chunk (tool-call args fragments, reasoning blocks, end-of-message marker)
                    key = (name, "msg", c["ns"], "tool" if v.get("type") == "tool" else "call")
                    n_for[key] = n_for.get(key, 0) + 1
                    if n_for[key] > 2:
                        continue
            # caps on repetitive events
            if mode == "custom":
                k = v["kind"]
                cap = {"evidence.found": 1, "policy.check": 2, "series": 0, "metric": 1, "progress": 1, "hypothesis.update": 4}.get(k, 99)
                key = (name, k, None if k == "progress" else v.get("agent"))
                n_for[key] = n_for.get(key, 0) + 1
                if n_for[key] > cap:
                    continue
            if mode == "tasks" and c["ns"]:
                key = (name, "task", c["ns"], v.get("name"), v["phase"])
                n_for[key] = n_for.get(key, 0) + 1
                if n_for[key] > (6 if v.get("name") == "score_hypothesis" else 2):
                    continue
            if mode == "tasks" and v.get("name") == "score_hypothesis" and v["phase"] == "done":
                key = (name, "sh_done")
                n_for[key] = n_for.get(key, 0) + 1
                if n_for[key] > 2:
                    continue
            if mode == "updates" and c["ns"] and isinstance(v, dict):
                if all(x is None for x in v.values()):
                    continue  # a subgraph node that returned no state change (e.g. {"gather": null})
                key = (name, "upd", c["ns"], next(iter(v)))
                n_for[key] = n_for.get(key, 0) + 1
                if n_for[key] > 2:
                    continue
            if mode == "updates" and isinstance(v, dict) and "score_hypothesis" in v:
                key = (name, "sh_upd")
                n_for[key] = n_for.get(key, 0) + 1
                if n_for[key] > 2:
                    continue
            c["scene"] = len(out_scenes)
            kept.append(c)
        out_scenes.append({"name": name, "caption": caption, "from": len(out_events), "n": len(kept)})
        out_events.extend(kept)

    for k, s in enumerate(out_scenes):
        s["to"] = s["from"] + s["n"] - 1
    counts = {m: sum(1 for e in out_events if e["mode"] == m) for m in MODES}
    doc = {"source": f"{CAP}/events.jsonl", "graph_id": "incident_commander", "thread_id": th["thread_id"],
           "run_ids": run_ids, "scenes": out_scenes, "counts": counts, "events": out_events}
    OUT.write_text(json.dumps(doc, separators=(",", ":")))
    print("scenes:", [(s["name"], s["n"]) for s in out_scenes], "counts:", counts, "total:", len(out_events))
    print("t range:", out_events[0]["t"], out_events[-1]["t"], "bytes:", OUT.stat().st_size, "->", OUT)


if __name__ == "__main__":
    main()
