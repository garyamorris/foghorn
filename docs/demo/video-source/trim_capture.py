#!/usr/bin/env python3
"""trim_capture.py: merge the real capture dirs into captures/real/ keeping only the threads the video uses (not the Studio-driven runs),
so the animated visuals can be re-rendered from the repo without a server, a model or an API key."""
import json, os, shutil, sys
SRC = ['work/real_cap']
DST = 'captures/real'
KEEP_GRAPHS = {'event_router', 'incident_commander', 'postmortem_pipeline', 'memory_keeper', 'change_gate', 'shift_handoff', 'storm_correlator', 'ops_copilot', 'rca_tournament', 'eval_runner'}
os.makedirs(DST, exist_ok=True)
shutil.copytree(os.path.join(SRC[0], 'structure'), os.path.join(DST, 'structure'), dirs_exist_ok=True)
threads = []
n = 0
with open(os.path.join(DST, 'events.jsonl'), 'w') as out:
    off = 0.0
    for d in SRC:
        evs = [json.loads(l) for l in open(os.path.join(d, 'events.jsonl'))]
        # studio-driven threads stream non-resumable debug/partial events: drop any thread whose modes are all studio-style
        bad = set()
        modes = {}
        for e in evs:
            modes.setdefault(e['thread_id'], set()).add(e['event'].split('|')[0])
        for t, ms in modes.items():
            if ms & {'debug', 'messages/partial'} and 'tasks' not in ms:
                bad.add(t)
        t0 = min(e['t'] for e in evs)
        for e in evs:
            if e['thread_id'] in bad or e['graph_id'] not in KEEP_GRAPHS:
                continue
            e = dict(e, t=round(e['t'] - t0 + off, 3), src=os.path.basename(d))
            out.write(json.dumps(e, separators=(',', ':')) + '\n'); n += 1
        for th in json.load(open(os.path.join(d, 'threads.json'))):
            if th['thread_id'] not in bad:
                threads.append(dict(th, src=os.path.basename(d)))
        off += max(e['t'] for e in evs) - t0 + 5
json.dump(threads, open(os.path.join(DST, 'threads.json'), 'w'), indent=1)
print(n, 'events ->', DST, round(os.path.getsize(os.path.join(DST, 'events.jsonl')) / 1e6, 1), 'MB')
