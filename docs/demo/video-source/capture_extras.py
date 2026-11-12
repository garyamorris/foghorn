#!/usr/bin/env python3
"""capture_extras.py: scripted, real runs of the graphs that the dashboard hero run does not cover. Run it while tap.py is recording
the same server; every run is created resumable with all stream modes, so the tap joins and timestamps it.

    python3 capture_extras.py http://127.0.0.1:2031 gate|crash|copilot|all

gate    change_gate "gauntlet": question -> ask -> edit -> reject (revise loop) -> approve -> four-eyes (two PARALLEL interrupts, resumed with
        one {interrupt_id: decision} map). Human answers are scripted but go through the same Command(resume=...) path as the dashboard.
crash   shift_handoff with crash_after=reconcile: the run FAILS, the thread keeps its checkpoints, we re-run it with input=None
        and it completes from the checkpoint (reconcile executes twice, everything else once).
copilot ops_copilot create_agent chat with tool calls on scenario S10.
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.parse
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:2031"
U = urllib.parse.urlparse(BASE)
MODES = ["updates", "messages-tuple", "custom", "values", "tasks", "checkpoints"]


def api(method, path, body=None, timeout=120):
    c = http.client.HTTPConnection(U.hostname, U.port, timeout=timeout)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw) if raw else None
    except Exception:
        return r.status, raw


def create_thread(meta=None):
    s, t = api("POST", "/threads", {"metadata": meta or {}})
    return t["thread_id"]


def start_run(tid, graph, *, input=None, command=None, context=None, config=None, durability=None):
    body = {"assistant_id": graph, "stream_mode": MODES, "stream_subgraphs": True, "stream_resumable": True, "on_disconnect": "continue", "multitask_strategy": "reject"}
    if input is not None:
        body["input"] = input
    if command is not None:
        body["command"] = command
    if context:
        body["context"] = context
    if config:
        body["config"] = config
    if durability:
        body["durability"] = durability
    s, r = api("POST", f"/threads/{tid}/runs", body)
    if s >= 300:
        raise SystemExit(f"run create failed {s}: {r}")
    return r["run_id"]


def wait_run(tid, rid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s, r = api("GET", f"/threads/{tid}/runs/{rid}")
        if r and r.get("status") not in ("pending", "running"):
            return r["status"]
        time.sleep(0.5)
    return "timeout"


def interrupts(tid):
    s, t = api("GET", f"/threads/{tid}")
    out = []
    for lst in (t.get("interrupts") or {}).values():
        out.extend(lst)
    return out


def pause(sec):
    time.sleep(sec)


def run_gate():
    print("== change_gate gauntlet")
    tid = create_thread({"video": "change_gate"})
    change = {"id": "CHG-1001", "kind": "deploy", "component": "checkout-service", "description": "Deploy checkout 2026.11.05.2 (14 commits)", "risk": "medium", "requested_by": "a.dev", "environment": "prod", "diff": {"rollback_plan": "redeploy 2026.11.04.9"}}
    rid = start_run(tid, "change_gate", input={"change": change, "mode": "gauntlet"})
    seen = []
    # scripted human: answer whatever the graph asks, in the order the gauntlet asks it
    plan_approval = ["ask", "edit", "reject", "approve"]
    for step in range(40):
        st = wait_run(tid, rid)
        its = interrupts(tid)
        if not its:
            print("   run ended:", st)
            break
        kinds = [i["value"].get("kind") for i in its]
        print("   pause:", kinds)
        pause(2.2)  # a human reading the card
        if len(its) > 1:  # four-eyes: resume every pending interrupt in one Command
            resume = {i["id"]: {"decision": "approve", "decided_by": f"reviewer-{n+1}", "note": "looks good"} for n, i in enumerate(its)}
        else:
            i = its[0]
            k = i["value"].get("kind")
            if k == "question":
                resume = {i["id"]: {"answer": "Yes, the flag ramp was intended; the unexpected timeouts started later.", "decided_by": "oncall"}}
            elif k == "edit":
                resume = {i["id"]: {"decision": "edit", "decided_by": "oncall", "edits": {"description": "Deploy checkout 2026.11.05.2 behind the canary flag"}, "note": "narrow the blast radius"}}
            else:
                d = plan_approval.pop(0) if plan_approval else "approve"
                resume = {i["id"]: {"decision": d, "decided_by": "oncall", "note": {"ask": "", "edit": "ship it behind the canary flag", "reject": "rollback plan is too vague", "approve": "ok"}.get(d, ""), "question": "Which region is the canary in?" if d == "ask" else "", "edits": {"description": "Deploy checkout 2026.11.05.2 behind the canary flag"} if d == "edit" else {}}}
        rid = start_run(tid, "change_gate", command={"resume": resume})
    print("   thread", tid)


def run_crash():
    print("== shift_handoff crash and recovery")
    tid = create_thread({"video": "shift_handoff"})
    rid = start_run(tid, "shift_handoff", input={"window_hours": 8, "crash_after": "reconcile"}, durability="sync")
    st = wait_run(tid, rid)
    print("   first run:", st)
    pause(3.0)
    rid = start_run(tid, "shift_handoff", input=None, durability="sync")
    st = wait_run(tid, rid)
    print("   second run (resume from checkpoint):", st)
    print("   thread", tid)


def run_storm():
    print("== storm_correlator (S12 zone partition: 22 alerts -> one cluster)")
    import yaml  # repo venv: .venv/bin/python
    text = open("/Users/garyamorris/Developer/repos/langgraph-app/src/foghorn/flows/storm_correlator/GUIDE.md").read().split("\n---\n")[0]
    sample = yaml.safe_load(text.split("---\n", 1)[-1] if text.startswith("---") else text)["sample_inputs"][0]["input"]
    tid = create_thread({"video": "storm_correlator"})
    rid = start_run(tid, "storm_correlator", input=sample)
    print("   ", wait_run(tid, rid), "thread", tid)


def run_copilot():
    print("== ops_copilot")
    tid = create_thread({"video": "ops_copilot"})
    rid = start_run(tid, "ops_copilot", input={"messages": [{"role": "user", "content": "Why is checkout slow?"}], "scenario_id": "S10"})
    st = wait_run(tid, rid)
    print("   ", st, "interrupts:", [i["value"].get("kind") for i in interrupts(tid)])
    print("   thread", tid)


if __name__ == "__main__":
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    if which in ("gate", "all"):
        run_gate()
    if which in ("crash", "all"):
        run_crash()
    if which in ("storm", "all"):
        run_storm()
    if which in ("copilot", "all"):
        run_copilot()
