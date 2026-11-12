// replay_data.js: load a recorded capture, pick graph/thread/runs, normalise the SSE events and build the
// time-warped "script" (a flat, sorted list of records with animation time `a`) that the renderer replays.
// Pure data code: no DOM. Deterministic (no randomness).

export const T = {
  GAP_MAX: 1.2,        // capture gaps longer than this ...
  GAP_SHORT: 0.45,     // ... are compressed to this (animation seconds, before `fit`)
  MIN_TOP: 0.42,       // minimum visible duration of a top-level task
  MIN_INNER: 0.30,     // ... of a subgraph (inner) task
  MIN_HOOK: 0.12,      // ... of tiny middleware hooks (before_model / after_agent)
  AFTER_DONE: 0.10,    // a START that follows a DONE waits this long (the edge trail plays)
  DONE_SPACING: 0.05,  // consecutive DONEs are at least this far apart
  FAN_SPACING: 0.15,   // ... and this far apart for parallel instances of one node (results tick off one by one)
  TICKER: 0.30,        // minimum spacing between visible ticker lines
  TOKEN: 0.045,        // minimum spacing between token chunks (readable streaming pace)
  TOKEN_MAX_MSG: 3.6,  // a single narration never streams longer than this
  INT_HOLD: 3.1,       // how long the interrupt (amber) state is shown before the next run starts
  RESUME_HOLD: 1.0,    // after a resume starts, wait this long before the first node (resume banner time)
  NEWRUN_GAP: 0.9,
  END_HOLD: 1.5,
};

const RE_LINE = /^\{"t": ?([0-9.eE+-]+), ?"thread_id": ?"([^"]*)", ?"run_id": ?"([^"]*)", ?"graph_id": ?"([^"]*)"/;

function fnum(v) { const x = parseFloat(v); return Number.isFinite(x) ? x : null; }

/** Fetch + prescan events.jsonl without JSON-parsing every payload; parse only the chosen thread. */
export async function loadCapture(cap, P) {
  const base = cap.replace(/\/$/, '');
  const [txt, thr] = await Promise.all([
    fetch(`${base}/events.jsonl`).then((r) => { if (!r.ok) throw new Error(`events.jsonl: HTTP ${r.status}`); return r.text(); }),
    fetch(`${base}/threads.json`).then((r) => (r.ok ? r.json() : [])).catch(() => []),
  ]);
  const lines = txt.split('\n');
  const threads = new Map(); // thread_id -> {graph, n, t0, lines:[]}
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (!ln) continue;
    const m = RE_LINE.exec(ln.length > 400 ? ln.slice(0, 400) : ln);
    if (!m) continue;
    let th = threads.get(m[2]);
    if (!th) { th = { id: m[2], graph: m[4], n: 0, t0: fnum(m[1]), lines: [] }; threads.set(m[2], th); }
    th.n++; th.lines.push(i);
  }
  const graphs = [...new Set([...threads.values()].map((t) => t.graph))];
  let graph = P.get('graph');
  const tprefix = P.get('thread');
  let th = null;
  if (tprefix) {
    th = [...threads.values()].find((t) => t.id.startsWith(tprefix) && (!graph || t.graph === graph));
    if (!th) throw new Error(`no thread starting with ${tprefix}`);
    graph = th.graph;
  } else {
    if (!graph) graph = graphs.includes('incident_commander') ? 'incident_commander' : graphs[0];
    const cands = [...threads.values()].filter((t) => t.graph === graph).sort((a, b) => b.n - a.n || a.t0 - b.t0);
    th = cands[0] || null;
    if (!th) console.warn(`no recorded run for graph ${graph} (have: ${graphs.join(', ')}): showing the structure only`);
  }
  const events = th ? th.lines.map((i) => JSON.parse(lines[i])) : [];
  const structure = await fetch(`${base}/structure/${graph}.json`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const meta = th ? (Array.isArray(thr) ? thr : []).find((t) => t.thread_id === th.id) || null : null;
  return { cap: base, graph, threadId: th ? th.id : '(no recorded run)', events, structure, threadMeta: meta, graphs };
}

// ---------------------------------------------------------------------------------------------------- run selection
function splitRuns(events) {
  const runs = new Map();
  for (const e of events) {
    let r = runs.get(e.run_id);
    if (!r) { r = { runId: e.run_id, events: [], t0: e.t, interrupted: false }; runs.set(e.run_id, r); }
    r.events.push(e);
  }
  const list = [...runs.values()].sort((a, b) => a.t0 - b.t0);
  for (const r of list) {
    r.events.sort((a, b) => a.t - b.t); // stable
    for (const e of r.events) {
      const d = e.data;
      if (e.event.startsWith('tasks') && d && !('input' in d) && !('triggers' in d) && Array.isArray(d.interrupts) && d.interrupts.length) r.interrupted = true;
      if (e.event.startsWith('updates') && d && typeof d === 'object' && d.__interrupt__) r.interrupted = true;
      if (e.event.startsWith('tasks') && d && !('input' in d) && !('triggers' in d) && d.error) r.errored = true;
      if (e.event === 'error') r.errored = true;
    }
  }
  return list;
}

function pickRuns(list, spec) {
  if (!spec || spec === 'auto') {
    const out = [list[0]];
    for (let i = 1; i < list.length && (list[i - 1].interrupted || list[i - 1].errored); i++) out.push(list[i]);
    return out;
  }
  if (spec === 'all') return list;
  const idx = new Set();
  for (const part of spec.split(',')) {
    const m = /^(\d+)-(\d+)$/.exec(part.trim());
    if (m) for (let i = +m[1]; i <= +m[2]; i++) idx.add(i);
    else if (/^\d+$/.test(part.trim())) idx.add(+part);
  }
  const out = list.filter((_, i) => idx.has(i));
  return out.length ? out : [list[0]];
}

// ---------------------------------------------------------------------------------------------------- normalise
function errText(e) {
  if (e == null) return '';
  if (typeof e === 'string') return e;
  if (typeof e === 'object') return [e.error || e.name || 'error', e.message].filter(Boolean).join(': ');
  return String(e);
}

function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map((c) => (typeof c === 'string' ? c : c && c.text) || '').join('');
  return '';
}

const HOOK_RE = /\.(before|after)_(model|agent)\b|Middleware\[/;
export const isHook = (name) => HOOK_RE.test(name);

function normalize(runs, P) {
  const from = P.get('from') != null ? parseFloat(P.get('from')) : null;
  const to = P.get('to') != null ? parseFloat(P.get('to')) : null;
  const items = [];
  const msgCount = new Map(); // `${lane}|${msgId}` -> chunk count (for pacing)
  let prevInterrupted = false, prevErrored = false;
  runs.forEach((run, ri) => {
    const evs = run.events.filter((e) => (from == null || e.t >= from) && (to == null || e.t <= to));
    if (!evs.length) { prevInterrupted = run.interrupted; prevErrored = !!run.errored; return; }
    const cont = ri > 0 && (prevInterrupted || prevErrored);
    items.push({ type: 'run', t: evs[0].t, run: ri, runId: run.runId, resume: cont, afterInterrupt: cont, afterError: ri > 0 && prevErrored && !prevInterrupted });
    prevInterrupted = run.interrupted; prevErrored = !!run.errored;
    const lastCustom = new Map();
    const pendingCkpt = [];
    for (const e of evs) {
      const [mode, ...rest] = e.event.split('|');
      const ns = rest.join('|');
      const container = ns ? ns.split('|')[0].split(':')[0] : '';
      const d = e.data;
      const base = { t: e.t, run: ri };
      if (mode === 'tasks' && d && d.name) {
        if ('input' in d || 'triggers' in d) {
          const it = { ...base, type: 'start', tid: d.id, name: d.name, ns: container, depth: ns ? ns.split('|').length : 0 };
          if (d.name === 'tools' && Array.isArray(d.input)) it.calls = d.input.filter((c) => c && c.name).map((c) => ({ name: c.name, args: c.args }));
          items.push(it);
        } else {
          const ints = Array.isArray(d.interrupts) ? d.interrupts : [];
          items.push({ ...base, type: 'done', tid: d.id, name: d.name, ns: container, err: d.error ? errText(d.error) : null, interrupts: ints.map((x) => x && x.value).filter(Boolean), nint: ints.length });
        }
      } else if (mode === 'custom' && d && d.kind) {
        const it = { ...base, type: 'custom', kind: d.kind, key: d.key, agent: d.agent || container, ns: container, data: d.data || {}, count: 1, group: null, hidden: false };
        const gk = `${it.kind}|${it.agent}|${it.ns}`;
        const last = lastCustom.get(gk);
        if (last && it.t - last.t < 0.06 && it.kind !== 'handoff' && it.kind !== 'decision') {
          last.count++; (last.group || (last.group = [last.data])).push(it.data); last.t2 = it.t;
          it.hidden = true;
        } else lastCustom.set(gk, it);
        items.push(it);
      } else if (mode === 'messages' && Array.isArray(d) && d[0]) {
        const chunk = d[0], meta = d[1] || {};
        const ty = chunk.type || '';
        if (ty && !/ai/i.test(ty)) continue;
        const lane = container || meta.langgraph_node || 'llm';
        const txt = textOf(chunk.content);
        const tc = (chunk.tool_calls || []).filter((c) => c && c.name);
        const mid = chunk.id || '';
        if (txt) {
          const key = `${lane}|${mid}`;
          msgCount.set(key, (msgCount.get(key) || 0) + 1);
          items.push({ ...base, type: 'msg', lane, mid, text: txt, node: meta.langgraph_node || '', ns: container });
        }
        for (const c of tc) items.push({ ...base, type: 'tool', lane, mid, name: c.name, args: c.args || {}, ns: container });
      } else if (mode === 'checkpoints' && d && d.config) {
        const cc = d.config.configurable || {};
        const pc = (d.parent_config && d.parent_config.configurable) || {};
        items.push({ ...base, type: 'ckpt', id: cc.checkpoint_id || '', parent: pc.checkpoint_id || '', next: d.next || [], step: d.metadata && d.metadata.step, ns: container });
      }
    }
  });
  for (const it of items) if (it.type === 'msg') it.mcount = msgCount.get(`${it.lane}|${it.mid}`) || 1;
  // stable order: by run, then capture time (items were pushed in capture order already)
  return items;
}

// ---------------------------------------------------------------------------------------------------- schedule (time warp)
export function buildScript(events, P, structure) {
  if (!events.length) {
    return { items: [], runs: [], runsRaw: [], tasks: [], fan: new Map(), endA: 0, structure, empty: true };
  }
  const list = splitRuns(events);
  const runs = pickRuns(list, P.get('run'));
  const items = normalize(runs, P);
  if (!items.length) throw new Error('no events selected');

  let a = 0, prevT = null, lastDoneA = -1e9, lastTicker = -1e9, firstAfterResume = false;
  const startA = new Map();     // `${run}|${tid}` -> {a, name, ns}
  const lastMsgA = new Map();   // lane -> a
  const running = new Map();    // nodeId -> count (concurrency)
  const fan = new Map();        // nodeId -> max concurrency
  const nodeIdOf = (it) => (it.ns ? `${it.ns}:${it.name}` : it.name);
  const lastDoneByNode = new Map();
  const runInfo = [];

  for (const it of items) {
    let gap = 0;
    if (prevT != null) {
      const dt = Math.max(0, it.t - prevT);
      gap = dt > T.GAP_MAX ? T.GAP_SHORT : dt;
    }
    if (it.type === 'run' && runInfo.length) {
      const prev = runInfo[runInfo.length - 1];
      gap = it.afterInterrupt ? T.INT_HOLD : T.NEWRUN_GAP;
      prev.a1 = a;
    }
    let na = a + gap;
    switch (it.type) {
      case 'start': {
        na = Math.max(na, lastDoneA + T.AFTER_DONE);
        if (firstAfterResume) { na = Math.max(na, a + T.RESUME_HOLD); firstAfterResume = false; }
        startA.set(`${it.run}|${it.tid}`, { a: na, name: it.name });
        const id = nodeIdOf(it);
        const c = (running.get(id) || 0) + 1; running.set(id, c);
        if (c > (fan.get(id) || 0)) fan.set(id, c);
        break;
      }
      case 'done': {
        const s = startA.get(`${it.run}|${it.tid}`);
        const md = isHook(it.name) ? T.MIN_HOOK : it.ns ? T.MIN_INNER : T.MIN_TOP;
        if (s) na = Math.max(na, s.a + md);
        const id = nodeIdOf(it);
        const sp = (fan.get(id) || 1) > 1 ? T.FAN_SPACING : T.DONE_SPACING;
        const ld = lastDoneByNode.get(id);
        na = Math.max(na, lastDoneA + T.DONE_SPACING, ld != null && (fan.get(id) || 1) > 1 ? ld + sp : -1e9);
        lastDoneA = na; lastDoneByNode.set(id, na);
        running.set(id, Math.max(0, (running.get(id) || 1) - 1));
        break;
      }
      case 'custom': {
        const vis = tickerVisible(it);
        if (vis) { na = Math.max(na, lastTicker + T.TICKER); lastTicker = na; }
        break;
      }
      case 'msg': {
        const floor = Math.min(T.TOKEN, T.TOKEN_MAX_MSG / Math.max(1, it.mcount));
        const l = lastMsgA.get(it.lane);
        if (l != null) na = Math.max(na, l + floor);
        lastMsgA.set(it.lane, na);
        break;
      }
      case 'run': {
        if (it.resume) firstAfterResume = true;
        break;
      }
      default: break;
    }
    a = na; it.a = a; prevT = it.t;
    if (it.type === 'run') runInfo.push({ index: it.run, runId: it.runId, resume: it.resume, afterError: it.afterError, a0: a, a1: a, interrupted: false, errored: false });
  }
  if (runInfo.length) runInfo[runInfo.length - 1].a1 = a;
  for (const r of runInfo) { r.interrupted = !!runs[r.index] && runs[r.index].interrupted; r.errored = !!runs[r.index] && !!runs[r.index].errored; }

  // interrupt records: derived from DONE tasks that carry interrupts; attach checkpoint ids
  const out = [];
  const ckpts = items.filter((i) => i.type === 'ckpt');
  items.forEach((it, i) => {
    out.push(it);
    if (it.type === 'done' && it.nint) {
      // checkpoint at the pause: latest checkpoint seen in this run that still has `next`, else the next run's first parent
      let ck = null;
      for (const c of ckpts) if (c.run === it.run && c.a <= it.a && c.next && c.next.length) ck = c;
      if (!ck) {
        const nxt = ckpts.find((c) => c.run === it.run + 1);
        if (nxt && nxt.parent) ck = { id: nxt.parent };
      }
      const prevI = [...out].reverse().find((x) => x.type === 'interrupt');
      if (prevI && prevI.run === it.run && it.a - prevI.a < 0.3) { prevI.payloads = prevI.payloads.concat(it.interrupts); prevI.nint += it.nint; prevI.nodes.push(nodeIdOf(it)); }
      else out.push({ type: 'interrupt', t: it.t, a: it.a, run: it.run, node: nodeIdOf(it), nodes: [nodeIdOf(it)], name: it.name, ns: it.ns, payloads: it.interrupts, nint: it.nint, ckpt: ck ? ck.id : '' });
    }
  });

  // tasks list (waterfall + __meta)
  const tasks = [];
  const open = new Map();
  for (const it of out) {
    if (it.type === 'start') {
      const t = { tid: it.tid, name: it.name, ns: it.ns, run: it.run, start: it.a, end: null, node: nodeIdOf(it), status: 'running', cached: false, interrupted: false, error: false };
      open.set(`${it.run}|${it.tid}`, t); tasks.push(t);
    } else if (it.type === 'done') {
      let t = open.get(`${it.run}|${it.tid}`);
      if (!t) { t = { tid: it.tid, name: it.name, ns: it.ns, run: it.run, start: it.a, end: null, node: nodeIdOf(it), status: 'done', cached: false, interrupted: false, error: false }; tasks.push(t); }
      t.end = it.a; t.status = it.err ? 'error' : it.nint ? 'interrupted' : 'done'; t.interrupted = !!it.nint; t.error = !!it.err;
      open.delete(`${it.run}|${it.tid}`);
    }
  }
  for (const t of open.values()) { // started but never finished in this run: cached replay (functional API resume) or cut by `to`
    const ri = runInfo.find((r) => r.index === t.run);
    t.cached = !!(ri && ri.resume);
    t.end = t.cached ? t.start + 0.12 : (ri ? ri.a1 : t.start + 0.2);
    t.status = t.cached ? 'cached' : 'done';
  }

  const endA = out.length ? out[out.length - 1].a : 0;
  return { items: out, runs: runInfo, runsRaw: runs, tasks, fan, endA, structure };
}

// which custom events produce a visible ticker line
export function tickerVisible(it) {
  if (it.hidden) return false;
  if (it.kind === 'series') return false;
  if (it.kind === 'progress' && /\d+\/\d+ tool calls?$/.test(String(it.data.label || ''))) return false;
  return true;
}

/** Scale every record time by k (the `fit` warp) and add the end hold. Returns total duration. */
export function applyFit(script, fit) {
  if (script.empty) { script.k = 1; script.duration = fit || 4; script.endA = 0; return script.duration; }
  const natural = script.endA;
  const k = fit && natural > 0 ? Math.max(0.05, (fit - T.END_HOLD) / natural) : 1;
  for (const it of script.items) it.a *= k;
  for (const t of script.tasks) { t.start *= k; t.end *= k; }
  for (const r of script.runs) { r.a0 *= k; r.a1 *= k; }
  script.k = k;
  script.endA = natural * k;
  script.duration = script.endA + T.END_HOLD;
  return script.duration;
}
