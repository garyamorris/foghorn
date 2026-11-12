#!/usr/bin/env python3
"""facts.py: derive the numbers the narration quotes from the recorded runs (so the script says only what the recordings show)."""
import json, collections, sys
def load(d):
    return [json.loads(l) for l in open(f'{d}/events.jsonl')]
out = {}
for cap in ('work/real_cap', 'work/real_extras'):
    ev = load(cap)
    threads = collections.defaultdict(list)
    for e in ev: threads[(e['graph_id'], e['thread_id'])].append(e)
    for (g, tid), es in sorted(threads.items(), key=lambda kv: kv[1][0]['t']):
        if g == 'event_router' and len(es) < 40:
            continue
        runs = collections.OrderedDict()
        for e in es: runs.setdefault(e['run_id'], []).append(e)
        modes = collections.Counter(e['event'].split('|')[0] for e in es)
        top_tasks = collections.OrderedDict()
        sends = collections.Counter()
        inflight = collections.Counter(); peak = collections.Counter()
        for e in es:
            if e['event'].startswith('tasks') and isinstance(e['data'], dict):
                n = e['data'].get('name'); ns = e['event'].partition('|')[2].split(':')[0]
                key = (ns + '/' if ns else '') + n
                if 'result' in e['data'] or 'error' in e['data'] and e['data'].get('error') is not None or ('result' in e['data']):
                    inflight[key] -= 1
                else:
                    inflight[key] += 1; peak[key] = max(peak[key], inflight[key]); top_tasks.setdefault(key, round(e['t'], 1))
        intr = []
        for e in es:
            if e['event'].startswith('updates') and isinstance(e['data'], dict) and '__interrupt__' in e['data']:
                for it in e['data']['__interrupt__']:
                    v = it.get('value', {}); intr.append((round(e['t'], 1), v.get('kind'), (v.get('title') or '')[:60]))
        kinds = collections.Counter(e['data'].get('kind') for e in es if e['event'].startswith('custom') and isinstance(e['data'], dict))
        out[f'{g}:{tid[:8]}'] = {'graph': g, 'runs': len(runs), 't0': es[0]['t'], 't1': es[-1]['t'], 'modes': dict(modes), 'tasks_first_seen': dict(top_tasks), 'parallel_peak>1': {k: v for k, v in peak.items() if v > 1}, 'interrupts': intr, 'custom_kinds': dict(kinds)}
json.dump(out, open('work/facts.json', 'w'), indent=1)
for k, v in out.items():
    print('==', k, f"runs={v['runs']} t={v['t0']:.1f}-{v['t1']:.1f} ({v['t1']-v['t0']:.1f}s)")
    print('   modes', v['modes'])
    print('   parallel>1', v['parallel_peak>1'])
    print('   interrupts', v['interrupts'])
    print('   custom', v['custom_kinds'])
    print('   tasks', list(v['tasks_first_seen'].items())[:40])
