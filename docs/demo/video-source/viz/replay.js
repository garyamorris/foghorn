// replay.js: replays a REAL recorded LangGraph run (work/<capture>/events.jsonl + structure/<graph>.json) as an animation.
// Everything on screen is a pure function of the recorded data and the animation clock `t` (no randomness).
import { loadCapture, buildScript, applyFit, T, tickerVisible, isHook } from './replay_data.js';
import { layoutGraph, textW, FS, MONO, SANS } from './replay_layout.js';

const P = new URLSearchParams(location.search);
const NS = 'http://www.w3.org/2000/svg';
const PAL = ['#8aa0ff', '#4fd1c5', '#f6a560', '#cf8ae8', '#7bd88a', '#f48fb1', '#a7b3c4', '#f2c14e'];
const AMBER = '#f2c14e';
const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
function el(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
const short = (id, n = 8) => String(id || '').slice(0, n);

const VW = 912, VH = 472, SMIN = 0.96, SMAX = 1.06;

// ======================================================================================================== boot
async function boot() {
  const cap = P.get('cap') || '../work/rehearsal_cap';
  const data = await loadCapture(cap, P);
  if (!data.structure) throw new Error(`no structure/${data.graph}.json in ${cap}`);
  const script = buildScript(data.events, P, data.structure);
  const fit = P.get('fit') ? parseFloat(P.get('fit')) : 0;
  applyFit(script, fit);

  // functional-API chips: every distinct (task name, occurrence) in order of first appearance
  const chipPlan = planChips(script, data.structure);
  const layout = layoutGraph(data.structure, script.fan, data.graph, { fnH: chipPlan.H });
  assignHues(layout);
  layout.chipHue = new Map(chipPlan.chips.map((c) => [c.name, c.hue]));
  const scene = buildScene(layout, script, chipPlan);
  layout.__edgeIdx = scene.edgeIdx;
  const sim = new Sim(script, layout, chipPlan);
  const simCam = new Sim(script, layout, chipPlan);
  const camera = buildCamera(script, layout, simCam);
  const wf = buildWaterfall(script, layout);
  const beatsAuto = autoBeats(script, layout);
  const ui = buildUI(data, script, layout, scene);
  const userBeats = await loadBeats(P.get('beats'), script, layout);

  window.__meta = {
    duration: +script.duration.toFixed(3), graph: data.graph, thread: data.threadId,
    runs: script.runs.map((r) => ({ index: r.index, run_id: r.runId, resume: r.resume, start: +r.a0.toFixed(3), end: +r.a1.toFixed(3), interrupted: r.interrupted })),
    beats: beatsAuto.map((b) => ({ t: +b.t.toFixed(3), label: b.label })),
    captions: userBeats.map((b) => ({ t: +b.t.toFixed(3), text: b.text, hold: b.hold })),
    tasks: script.tasks.map((t) => ({ name: t.name, start: +t.start.toFixed(3), end: +t.end.toFixed(3), ns: t.ns || undefined, run: t.run, status: t.status })),
    warp: script.k,
  };

  const R = { data, script, layout, scene, sim, camera, wf, ui, userBeats };
  window.__R = R;
  let t0 = null, seekT = P.get('seek') != null ? parseFloat(P.get('seek')) : null, manual = null;
  window.__seek = (t) => { manual = t; frame(t); };
  window.__render = frame;
  function frame(t) {
    t = clamp(t, 0, script.duration);
    sim.advance(t);
    renderAll(R, t);
  }
  if (seekT != null && !Number.isNaN(seekT)) { manual = seekT; }
  frame(manual != null ? manual : 0);
  window.__ready = true;
  const tick = (now) => {
    if (manual != null) { frame(manual); requestAnimationFrame(tick); return; }
    if (t0 == null) t0 = now;
    const t = (now - t0) / 1000;
    frame(t);
    if (t >= script.duration) { if (!window.__done) { window.__done = true; } frame(script.duration); return; }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ======================================================================================================== helpers
function nodeIdOf(it) { return it.ns ? `${it.ns}:${it.name}` : it.name; }

function planChips(script, structure) {
  const real = structure.graph.nodes.filter((n) => !/^__/.test(n.id) && !n.id.includes(':'));
  const functional = real.length === 1;
  const entry = functional ? real[0].id : null;
  const chips = [];
  if (functional) {
    const seen = new Set();
    const occ = new Map();
    for (const it of script.items) {
      if (it.type === 'run') occ.clear();
      if (it.type !== 'start' || it.ns || it.name === entry) continue;
      const k = (occ.get(it.name) || 0); occ.set(it.name, k + 1);
      const key = `${it.name}#${k}`;
      if (seen.has(key)) continue;
      seen.add(key); chips.push({ key, name: it.name, w: Math.ceil(textW(it.name, FS, 600) + 34), h: 34 });
    }
  }
  const names = [...new Set(chips.map((c) => c.name))];
  chips.forEach((c) => { c.hue = PAL[(names.indexOf(c.name) + 1) % PAL.length]; });
  // flow layout inside the entry box (W=880)
  const W = 880, padX = 18, gap = 30; let x = padX, y = 66, rows = 1;
  for (const c of chips) {
    if (x + c.w > W - padX && x > padX) { x = padX; y += 54; rows++; }
    c.x = x; c.y = y; x += c.w + gap;
  }
  return { functional, entry, chips, H: functional ? Math.max(150, y + 34 + 18) : 0, byKey: new Map(chips.map((c) => [c.key, c])) };
}

function assignHues(layout) {
  const tops = [...layout.nodes.values()].filter((n) => !n.container && n.kind !== 'start' && n.kind !== 'end')
    .sort((a, b) => a.cx - b.cx || a.cy - b.cy);
  const hue = new Map();
  tops.forEach((n, i) => hue.set(n.id, PAL[i % PAL.length]));
  for (const n of layout.nodes.values()) {
    n.hue = n.container ? hue.get(n.container) : hue.get(n.id) || '#7ab8ff';
  }
}

// ======================================================================================================== scene (SVG)
function buildScene(layout, script, chipPlan) {
  const gCont = $('gCont'), gEdgeTop = $('gEdge'), gEdgeIn = $('gEdgeIn'), gNode = $('gNode'), gTrail = $('gTrail'), gOver = $('gOver');
  const S = { nodes: new Map(), conts: new Map(), edges: new Map(), edgeIdx: new Map(), trailPool: [], bubble: null, chips: new Map(), fn: null };
  // containers
  for (const c of layout.containers.values()) {
    const hue = layout.nodes.get(c.id).hue;
    const g = el('g', { class: 'cont', style: `--h:${hue}`, transform: `translate(${c.x.toFixed(1)},${c.y.toFixed(1)})` }, gCont);
    el('rect', { class: 'glow', x: -3, y: -3, width: c.w + 6, height: c.h + 6, rx: 15 }, g);
    el('rect', { class: 'box', x: 0, y: 0, width: c.w, height: c.h, rx: 12 }, g);
    const t = el('text', { class: 'ttl', x: c.titleX, y: c.h / 2 + 1, 'dominant-baseline': 'central' }, g); t.textContent = c.label;
    const chk = el('g', { class: 'chk', transform: `translate(${c.w - 6},4)` }, g);
    el('circle', { r: 9 }, chk); el('path', { d: 'M-4 0 L-1 3 L4 -3' }, chk);
    S.conts.set(c.id, { g, st: '' });
  }
  // edges
  for (const e of layout.edges) {
    const cls = `edge${e.cond ? ' cond' : ''}${e.ghost ? ' ghost' : ''}`;
    const g = el('g', { class: cls }, e.container ? gEdgeIn : gEdgeTop);
    el('path', { class: 'base', d: e.d }, g);
    const used = el('path', { class: 'used', d: e.d }, g);
    const arrow = (pt) => { const a = el('path', { class: 'arr', d: 'M0 0 L-10 -5.5 L-10 5.5 Z', transform: `translate(${pt.x.toFixed(1)},${pt.y.toFixed(1)}) rotate(${(pt.ang * 180 / Math.PI).toFixed(1)})` }, g); return a; };
    arrow(e.end);
    if (e.bi) { const s = e.start; el('path', { class: 'arr', d: 'M0 0 L-10 -5.5 L-10 5.5 Z', transform: `translate(${s.x.toFixed(1)},${s.y.toFixed(1)}) rotate(${((s.ang + Math.PI) * 180 / Math.PI).toFixed(1)})` }, g); }
    if (e.label && e.labelPos) {
      const w = Math.ceil(textW(e.label, 16, 600, SANS)) + 16;
      const lg = el('g', { transform: `translate(${e.labelPos.x.toFixed(1)},${e.labelPos.y.toFixed(1)})` }, g);
      el('rect', { class: 'lblbg', x: -w / 2, y: -11, width: w, height: 22, rx: 11 }, lg);
      const tx = el('text', { class: 'lbl', x: 0, y: 1 }, lg); tx.textContent = e.label;
    }
    const rec = { g, e, used, len: 0, on: false, d: e.d };
    S.edges.set(e.id, rec);
    S.edgeIdx.set(`${e.src}>${e.dst}`, { rec, rev: false });
    if (e.bi && e.rev) S.edgeIdx.set(`${e.rev.src}>${e.rev.dst}`, { rec, rev: true });
  }
  for (const rec of S.edges.values()) { try { rec.len = rec.used.getTotalLength(); } catch (err) { rec.len = 100; } }
  // nodes
  for (const n of layout.nodes.values()) {
    if (n.kind === 'container') continue;
    const base = { style: `--h:${n.hue || '#7ab8ff'}`, transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})` };
    if (n.kind === 'start' || n.kind === 'end') {
      const g = el('g', { ...base, class: `term ${n.kind}` }, gNode);
      el('circle', { class: 'dot', cx: n.w / 2, cy: n.h / 2, r: 8 }, g);
      el('circle', { class: 'in', cx: n.w / 2, cy: n.h / 2, r: n.kind === 'end' ? 4 : 0.01 }, g);
      S.nodes.set(n.id, { g, n, sig: '' });
      continue;
    }
    if (n.kind === 'hook') {
      const g = el('g', { ...base, class: 'hook' }, gNode);
      el('circle', { class: 'glow', cx: n.w / 2, cy: n.h / 2, r: 11 }, g);
      el('circle', { class: 'dot', cx: n.w / 2, cy: n.h / 2, r: 7 }, g);
      S.nodes.set(n.id, { g, n, sig: '' });
      continue;
    }
    if (layout.functional && n.id === chipPlan.entry) continue; // drawn below
    const g = el('g', { ...base, class: 'node' }, gNode);
    el('rect', { class: 'glow', x: -3, y: -3, width: n.w + 6, height: n.h + 6, rx: 11 }, g);
    el('rect', { class: 'body', x: 0, y: 0, width: n.w, height: n.h, rx: 8 }, g);
    el('rect', { class: 'tint', x: 0, y: 0, width: n.w, height: n.h, rx: 8 }, g);
    const inline = n.fan > 1 && n.pipMode === 'inline';
    const tl = el('text', { class: 'lbl', x: inline ? 15 : n.w / 2, y: (n.fan > 1 && n.pipMode === 'below' ? 19 : n.h / 2) + 1, 'text-anchor': inline ? 'start' : 'middle', 'dominant-baseline': 'central' }, g);
    tl.textContent = n.label;
    const pips = [];
    if (n.fan > 1) {
      const pg = el('g', {}, g);
      const pw = n.pipW;
      const px0 = inline ? n.w - 14 - pw : (n.w - pw) / 2;
      const ps = n.pipSize || 14, pgp = n.pipGap == null ? 4 : n.pipGap;
      const py = inline ? (n.h - ps) / 2 : n.h - ps - 8;
      for (let i = 0; i < n.fan; i++) pips.push(el('rect', { class: 'pip', x: px0 + i * (ps + pgp), y: py, width: ps, height: ps, rx: ps > 12 ? 3 : 2, 'data-s': 'idle' }, pg));
      const bg = el('g', { class: 'bdg', transform: inline ? `translate(${Math.min(n.w - 30, 22)},${n.h + 1})` : `translate(${n.w - 46},1)` }, g);
      el('rect', { x: -19, y: -11, width: 38, height: 22, rx: 11 }, bg);
      const bt = el('text', { x: 0, y: 1 }, bg); bt.textContent = `×${n.fan}`;
      pips.bdg = bg; pips.bt = bt;
    }
    const chk = el('g', { class: 'chk', transform: `translate(${n.w - 6},3)` }, g);
    el('circle', { r: 9 }, chk); el('path', { d: 'M-4 0 L-1 3 L4 -3' }, chk);
    const pse = el('g', { class: 'pse', transform: `translate(${n.w - 6},3)` }, g);
    el('circle', { r: 11 }, pse); el('rect', { x: -5, y: -6, width: 3.5, height: 12, rx: 1 }, pse); el('rect', { x: 1.5, y: -6, width: 3.5, height: 12, rx: 1 }, pse);
    const er = el('g', { class: 'errm', transform: `translate(${n.w - 6},3)` }, g);
    el('circle', { r: 10 }, er); el('path', { d: 'M-4 -4 L4 4 M4 -4 L-4 4' }, er);
    const cn = el('g', { class: 'cnt', transform: `translate(20,0)` }, g);
    el('rect', { x: -19, y: -10, width: 38, height: 20, rx: 10 }, cn);
    const ct = el('text', { x: 0, y: 1 }, cn);
    S.nodes.set(n.id, { g, n, sig: '', pips, cnt: ct, bdgOn: false });
  }
  // functional entry + chips
  if (layout.functional && chipPlan.entry) {
    const n = layout.nodes.get(chipPlan.entry);
    const g = el('g', { class: 'fnent', style: `--h:${n.hue || PAL[0]}`, transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})` }, gNode);
    el('rect', { class: 'glow', x: -3, y: -3, width: n.w + 6, height: n.h + 6, rx: 15 }, g);
    el('rect', { class: 'box', x: 0, y: 0, width: n.w, height: n.h, rx: 12 }, g);
    const t = el('text', { class: 'ttl', x: 18, y: 30 }, g); t.textContent = n.label;
    const st = el('text', { class: 'sub', x: 18 + textW(n.label, 17, 700) + 14, y: 30 }, g); st.textContent = '@entrypoint · each @task call grows a chip below';
    const chk = el('g', { class: 'chk', style: 'opacity:0', transform: `translate(${n.w - 24},24)` }, g);
    el('circle', { r: 11 }, chk); el('path', { d: 'M-5 0 L-1.5 4 L5 -4' }, chk);
    const cg = el('g', {}, g);
    S.fn = { g, n, chk, cg };
    S.nodes.set(n.id, { g, n, sig: '', fn: true });
    chipPlan.chips.forEach((c, i) => {
      const cgp = el('g', { class: 'chip', style: `--h:${c.hue}`, transform: `translate(${c.x},${c.y})`, opacity: 0 }, cg);
      el('rect', { class: 'glow', x: -3, y: -3, width: c.w + 6, height: c.h + 6, rx: 11 }, cgp);
      el('rect', { class: 'body', x: 0, y: 0, width: c.w, height: c.h, rx: 8 }, cgp);
      const tx = el('text', { class: 'lbl', x: c.w / 2 - 4, y: c.h / 2 + 1, 'text-anchor': 'middle', 'dominant-baseline': 'central' }, cgp); tx.textContent = c.name;
      const ck = el('g', { class: 'chk', transform: `translate(${c.w - 4},3)` }, cgp);
      el('circle', { r: 8 }, ck); el('path', { d: 'M-3.5 0 L-1 2.5 L3.5 -3' }, ck);
      // connector from the previous chip
      let conn = null;
      if (i > 0) {
        const p = chipPlan.chips[i - 1];
        if (p.y === c.y) conn = el('path', { class: 'chipc', d: `M${p.x + p.w + 3},${p.y + 17} L${c.x - 4},${c.y + 17}`, opacity: 0 }, cg);
        else conn = el('path', { class: 'chipc', d: `M${p.x + p.w / 2},${p.y + 36} L${p.x + p.w / 2},${p.y + 44} L${c.x + c.w / 2},${p.y + 44} L${c.x + c.w / 2},${c.y - 2}`, opacity: 0 }, cg);
      }
      S.chips.set(c.key, { g: cgp, conn, sig: '' });
    });
  }
  return S;
}

// ======================================================================================================== simulation state (event-sourced)
class Sim {
  constructor(script, layout, chipPlan) { this.script = script; this.layout = layout; this.chipPlan = chipPlan; this.reset(); }
  reset() {
    this.i = 0; this.a = -1;
    this.nodes = new Map();      // id -> state
    this.exits = new Map();
    this.trails = [];
    this.used = new Map();       // edge id -> first used a
    this.ticker = [];
    this.lanes = new Map();
    this.chips = new Map();
    this.occ = new Map();
    this.facts = { evidence: new Set(), tools: 0, steps: 0, started: 0, done: 0, lead: null, polPass: 0, polFail: 0, decision: '', artifacts: 0, memory: 0, handoffs: 0 };
    this.waveOpen = false;
    this.status = 'idle'; this.run = -1; this.runStartA = 0;
    this.interrupt = null; this.resume = null; this.error = null; this.handoff = null; this.ckptSeen = '';
    this.lastIntNode = '';
  }
  ns(id) {
    let n = this.nodes.get(id);
    if (!n) { n = { st: 'idle', running: new Set(), wave: 0, waveDone: 0, startN: 0, doneN: 0, runs: 0, lastStart: -1e9, lastDone: -1e9, pips: [], pipOf: new Map(), cached: false }; this.nodes.set(id, n); }
    return n;
  }
  advance(t) {
    if (t < this.a) this.reset();
    const items = this.script.items;
    while (this.i < items.length && items[this.i].a <= t) this.apply(items[this.i++]);
    this.a = t;
  }
  trail(from, to, a) {
    const hit = this.layout.__edgeIdx.get(`${from}>${to}`);
    if (!hit) return;
    const id = hit.rec.e.id + (hit.rev ? '<' : '>');
    if (!this.used.has(hit.rec.e.id)) this.used.set(hit.rec.e.id, a);
    if (this.trails.some((tr) => tr.key === id && Math.abs(tr.a0 - a) < 0.05)) return;
    this.trails.push({ key: id, rec: hit.rec, rev: hit.rev, a0: a, hue: this.layout.nodes.get(to)?.hue || '#8aa0ff' });
    if (this.trails.length > 40) this.trails.splice(0, this.trails.length - 40);
  }
  apply(it) {
    const L = this.layout, F = this.facts;
    switch (it.type) {
      case 'run': {
        this.run = it.run; this.runStartA = it.a; this.waveOpen = false;
        if (it.resume) {
          const afterErr = !!(this.error && !this.interrupt);
          const nodes = afterErr ? [this.error.node] : (this.interrupt ? this.interrupt.nodes : [this.lastIntNode]);
          this.resume = { a0: it.a, node: nodes[0], nodes, afterError: afterErr, ckpt: this.interrupt ? this.interrupt.ckpt : this.ckptSeen };
          for (const nid of nodes) { const rn = this.nodes.get(nid); if (rn && (rn.st === 'interrupted' || rn.st === 'error')) rn.st = 'running'; }
          this.interrupt = null; this.error = null; this.status = 'running';
        }
        else { this.exits.clear(); this.exits.set('__start__', { a: it.a, used: null }); this.status = 'running'; }
        this.occ.clear();
        break;
      }
      case 'start': {
        F.started++;
        const id = nodeIdOf(it);
        const cp = this.chipPlan;
        if (cp.functional && !it.ns && it.name !== cp.entry) { this.chipStart(it); break; }
        if (!L.nodes.has(id)) break;
        const n = this.ns(id);
        if (n.running.size === 0) { n.runs++; n.wave = 0; n.waveDone = 0; n.pips = []; n.pipOf = new Map(); }
        n.running.add(it.tid); n.wave++; n.startN++; n.st = 'running'; n.lastStart = it.a;
        n.pipOf.set(it.tid, n.pips.length); n.pips.push('running');
        if (!it.ns && n.runs >= 1 && L.containers.has(id)) { for (const m of L.containers.get(id).members) { const s = this.ns(m); s.st = 'idle'; s.running.clear(); s.pips = []; s.pipOf = new Map(); s.runs = 0; s.wave = 0; s.waveDone = 0; } }
        for (const [s, x] of this.exits) {
          if (s === id || it.a - x.a > 2.6) continue;
          if (x.used != null && it.a - x.used > 0.35) continue;
          this.trail(s, id, it.a);
          if (this.layout.__edgeIdx.has(`${s}>${id}`) && x.used == null) x.used = it.a;
        }
        this.exits.delete(id);
        if (!it.ns) {
          if (!this.waveOpen) { F.steps++; this.waveOpen = true; }
        }
        if (it.calls) F.tools += it.calls.length;
        if (this.interrupt && this.interrupt.nodes.includes(id)) this.interrupt = null;
        break;
      }
      case 'done': {
        const id = nodeIdOf(it);
        const cp = this.chipPlan;
        if (cp.functional && !it.ns && it.name !== cp.entry) { this.chipDone(it); F.done++; break; }
        F.done++;
        if (!L.nodes.has(id)) break;
        const n = this.ns(id);
        n.running.delete(it.tid); n.doneN++; n.waveDone++;
        const pi = n.pipOf.get(it.tid); if (pi != null) n.pips[pi] = it.nint ? 'paused' : 'done';
        n.lastDone = it.a;
        if (n.running.size === 0) n.st = it.err ? 'error' : it.nint ? 'interrupted' : 'done';
        this.exits.set(id, { a: it.a, used: null });
        if (!it.ns) this.waveOpen = false;
        if (it.err) { this.status = 'error'; this.error = { a0: it.a, node: id, text: it.err }; }
        break;
      }
      case 'interrupt': {
        const p = (it.payloads && it.payloads[0]) || {};
        this.interrupt = { a0: it.a, node: it.node, nodes: it.nodes || [it.node], title: p.title || it.name, titles: (it.payloads || []).map((x) => x && x.title).filter(Boolean), summary: p.summary || '', kind: p.kind || '', risk: p.risk || '', ckpt: it.ckpt, n: it.nint, options: p.options || [] };
        this.lastIntNode = it.node; this.ckptSeen = it.ckpt || this.ckptSeen;
        this.status = 'interrupted';
        break;
      }
      case 'ckpt': { if (it.next && it.next.length) this.ckptSeen = it.id; break; }
      case 'custom': this.custom(it); break;
      case 'msg': {
        let ln = this.lanes.get(it.lane);
        if (!ln || ln.mid !== it.mid) { ln = { lane: it.lane, mid: it.mid, text: '', a0: it.a, aLast: it.a, seq: (ln ? ln.seq : 0) + 1, first: ln ? ln.first : it.a }; this.lanes.set(it.lane, ln); }
        ln.text += it.text; ln.aLast = it.a;
        break;
      }
      case 'tool': {
        let ln = this.lanes.get(it.lane);
        if (!ln || ln.mid !== it.mid) { ln = { lane: it.lane, mid: it.mid, text: '', a0: it.a, aLast: it.a, seq: (ln ? ln.seq : 0) + 1, first: ln ? ln.first : it.a }; this.lanes.set(it.lane, ln); }
        const args = Object.entries(it.args || {}).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(', ');
        ln.text += `${ln.text && !ln.text.endsWith('\n') ? '\n' : ''}‣ ${it.name}(${clip(args, 38)})\n`; ln.aLast = it.a;
        break;
      }
      default: break;
    }
  }
  chipKey(it) {
    const k = this.occ.get(it.name) || 0; this.occ.set(it.name, k + 1); return `${it.name}#${k}`;
  }
  chipStart(it) {
    const key = this.chipKey(it);
    this.chipByTid = this.chipByTid || new Map(); this.chipByTid.set(`${it.run}|${it.tid}`, key);
    let c = this.chips.get(key);
    if (!c) { c = { st: 'running', a0: it.a, replay: false }; this.chips.set(key, c); }
    else { c.st = this.script.runs.find((r) => r.index === it.run)?.resume ? 'cached' : 'running'; c.replay = true; c.a0 = it.a; }
  }
  chipDone(it) {
    const key = this.chipByTid && this.chipByTid.get(`${it.run}|${it.tid}`);
    const c = key && this.chips.get(key);
    if (c) { c.st = it.err ? 'error' : 'done'; }
  }
  custom(it) {
    const F = this.facts;
    const d = it.data || {};
    const group = it.group || [d];
    // facts
    if (it.kind === 'evidence.found') for (const g of group) F.evidence.add(g.id || g.ref || Math.random());
    else if (it.kind === 'hypothesis.update') { for (const g of group) if (!F.lead || g.score >= F.lead.score) F.lead = { label: g.label || g.id, score: g.score }; }
    else if (it.kind === 'policy.check') { for (const g of group) { if (g.result === 'pass' || g.status === 'pass') F.polPass++; else F.polFail++; } }
    else if (it.kind === 'decision') F.decision = d.what || '';
    else if (it.kind === 'artifact') F.artifacts++;
    else if (it.kind === 'memory.recalled' || it.kind === 'memory.written') F.memory++;
    else if (it.kind === 'handoff') {
      F.handoffs++;
      const node = this.layout.nodes.has(it.agent) ? it.agent : (it.ns && this.layout.nodes.has(it.ns) ? it.ns : null);
      this.handoff = { a0: it.a, to: d.to || '', reason: d.reason || '', node };
    }
    if (it.hidden || !tickerVisible(it)) return;
    this.ticker.push({ a: it.a, it, id: this.ticker.length });
    if (this.ticker.length > 8) this.ticker.shift();
  }
  focus(t) {
    // entities the camera should keep in view: running nodes (their container for inner nodes), the node that just finished
    const ids = [];
    for (const [id, n] of this.nodes) {
      const box = this.layout.nodes.get(id);
      if (!box) continue;
      const hot = n.st === 'running' || n.st === 'interrupted' || (n.st === 'done' && t - n.lastDone < 0.75 && n.lastDone >= 0);
      if (!hot) continue;
      if (box.kind === 'hook') { if (box.container) ids.push([box.container, n.lastStart]); continue; }
      ids.push([box.container || id, n.lastStart]);
    }
    return ids;
  }
}

// ======================================================================================================== camera
function buildCamera(script, layout, sim) {
  const B = layout.bounds;
  const worldW = B.x1 - B.x0, worldH = B.y1 - B.y0;
  const margin = 26;
  const fitAll = Math.min(VW / (worldW + 40), VH / (worldH + 40));
  const clampView = (v) => {
    let { cx, cy, s, oy } = v;
    if (worldW * s + 40 <= VW) cx = (B.x0 + B.x1) / 2; else cx = clamp(cx, B.x0 + VW / (2 * s) - 30 / s, B.x1 - VW / (2 * s) + 30 / s);
    if (worldH * s + 40 <= VH) cy = (B.y0 + B.y1) / 2 - oy / s; else cy = clamp(cy, B.y0 + (VH / 2 - oy) / s - 30 / s, B.y1 - (VH / 2 + oy) / s + 30 / s);
    return { cx, cy, s, oy };
  };
  const cam = { static: false, kfs: [], init: null, fitAll, B };
  if (fitAll >= SMIN || P.get('cam') === 'all') {
    const s = P.get('cam') === 'all' ? fitAll : clamp(fitAll, SMIN, 1.15);
    cam.static = true;
    cam.init = clampView({ cx: (B.x0 + B.x1) / 2, cy: (B.y0 + B.y1) / 2, s, oy: 0 });
    return cam;
  }
  const boxOf = (ids) => {
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const id of ids) { const b = layout.nodes.get(id); if (!b) continue; x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.w); y1 = Math.max(y1, b.y + b.h); }
    return x0 > x1 ? null : { x0, y0, x1, y1 };
  };
  // initial view: start dot + first nodes
  const first = [...layout.nodes.values()].filter((n) => n.kind !== 'start' && n.kind !== 'end' && !n.container).sort((a, b) => a.cx - b.cx).slice(0, 2).map((n) => n.id);
  const startBox = boxOf(first);
  const fitBox = (bx, pad) => {
    const w = bx.x1 - bx.x0 + 2 * pad, h = bx.y1 - bx.y0 + 2 * pad;
    const s = clamp(Math.min(VW / w, VH / h), SMIN, SMAX);
    return { cx: (bx.x0 + bx.x1) / 2 + 36 / s, cy: (bx.y0 + bx.y1) / 2, s, oy: 0 };
  };
  cam.init = clampView({ ...fitBox(startBox, 80), cx: (startBox.x0 + startBox.x1) / 2 + 150 });
  const kfs = cam.kfs;
  const at = (t) => camAt(cam, t);
  let cur = cam.init, lastIds = [];
  const step = 0.1;
  for (let t = 0; t <= script.endA + 0.2; t += step) {
    sim.advance(t);
    const foc = sim.focus(t);
    const oy = (sim.interrupt || (sim.resume && t - sim.resume.a0 < 2.4)) ? 58 : 0;
    let ids = foc.map((f) => f[0]);
    if (!ids.length) { if (cur.oy === oy) continue; ids = lastIds; if (!ids.length) continue; }
    // hub context: a running node with many outgoing edges (supervisor) is framed together with its neighbours when that fits
    if (foc.length) {
      const ctx = new Set(ids);
      for (const id of ids) {
        const nb = layout.nodes.get(id);
        if (!nb || nb.container) continue;
        const outs = layout.edges.filter((e) => e.src === id && layout.nodes.get(e.dst) && layout.nodes.get(e.dst).x - (nb.x + nb.w) < 260 && layout.nodes.get(e.dst).x > nb.x);
        if (outs.length >= 3) for (const e of outs) ctx.add(e.dst);
      }
      const cb = boxOf([...ctx]);
      if (cb && (cb.x1 - cb.x0 + 36) * SMIN <= VW && (cb.y1 - cb.y0 + 36) * SMIN <= VH) ids = [...ctx];
    }
    const bx = boxOf(ids);
    if (!bx) continue;
    // contained in the current view (with margin)?
    const v = cur;
    const sx0 = (bx.x0 - v.cx) * v.s + VW / 2, sx1 = (bx.x1 - v.cx) * v.s + VW / 2;
    const sy0 = (bx.y0 - v.cy) * v.s + VH / 2 + v.oy, sy1 = (bx.y1 - v.cy) * v.s + VH / 2 + v.oy;
    const inside = sx0 >= margin && sx1 <= VW - margin && sy0 >= (oy ? 150 : margin) && sy1 <= VH - margin;
    if (inside && v.oy === oy) { lastIds = ids; continue; }
    // too big for the view at SMIN: keep the most recently started entity visible
    let target = bx;
    const w = bx.x1 - bx.x0 + 100, h = bx.y1 - bx.y0 + 100;
    if (w * SMIN > VW || h * SMIN > VH) {
      const latest = foc.slice().sort((p, q) => q[1] - p[1])[0];
      const lb = latest && boxOf([latest[0]]);
      if (lb) target = lb;
    }
    let nv = fitBox(target, target === bx && ids.length > 1 ? 18 : 64);
    nv.oy = oy;
    nv = clampView(nv);
    const dist = Math.hypot((nv.cx - v.cx) * nv.s, (nv.cy - v.cy) * nv.s);
    if (dist < 6 && Math.abs(Math.log(nv.s / v.s)) < 0.03 && nv.oy === v.oy) { lastIds = ids; continue; }
    const from = at(t);
    kfs.push({ a0: t, dur: clamp(0.55 + dist / 900, 0.6, 1.1), from, to: nv });
    cur = nv; lastIds = ids;
  }
  sim.reset();
  return cam;
}
function camAt(cam, t) {
  let v = cam.init;
  for (const k of cam.kfs) {
    if (t < k.a0) break;
    const p = clamp((t - k.a0) / k.dur, 0, 1);
    const e = ease(p);
    v = { cx: k.from.cx + (k.to.cx - k.from.cx) * e, cy: k.from.cy + (k.to.cy - k.from.cy) * e, s: Math.exp(Math.log(k.from.s) + (Math.log(k.to.s) - Math.log(k.from.s)) * e), oy: k.from.oy + (k.to.oy - k.from.oy) * e };
    if (p < 1) return v;
  }
  return v;
}

// ======================================================================================================== waterfall
function buildWaterfall(script, layout) {
  const svg = $('wf');
  const W = 914, H = 50, x0 = 112, x1 = W - 8, top = 3, bot = H - 3;
  const dur = script.duration;
  const X = (a) => x0 + (a / dur) * (x1 - x0);
  const keep = script.tasks.filter((t) => !t.ns || (script.fan.get(t.node) || 1) > 1);
  keep.sort((a, b) => a.start - b.start || a.end - b.end);
  const rows = [];
  for (const t of keep) {
    let r = rows.findIndex((last) => last <= t.start - 0.01);
    if (r < 0) { r = rows.length; rows.push(0); }
    rows[r] = Math.max(t.end, t.start + 0.05); t.row = r;
  }
  const R = Math.max(1, rows.length);
  const rh = clamp((bot - top) / R, 3, 8), bh = Math.max(2.5, rh - 1.5);
  const bg = el('rect', { x: x0 - 4, y: 0, width: x1 - x0 + 8, height: H, rx: 6, fill: '#0f1320', stroke: '#1e2536' }, svg);
  const bars = [];
  const hueOf = (t) => { if (layout.chipHue && layout.chipHue.has(t.name)) return layout.chipHue.get(t.name); const b = layout.nodes.get(t.node) || layout.nodes.get(t.ns) || layout.nodes.get(t.name); return (b && b.hue) || '#7ab8ff'; };
  // pause bands
  const bands = [];
  for (const r of script.runs) {
    if (r.resume) {
      const prev = script.runs.find((q) => q.index === r.index - 1);
      if (prev) bands.push(el('rect', { x: X(prev.a1), y: 0, width: Math.max(3, X(r.a0) - X(prev.a1)), height: H, fill: r.afterError ? '#ff8b8f' : AMBER, opacity: 0.30 }, svg));
    }
  }
  for (const t of keep) {
    const rect = el('rect', { x: X(t.start), y: top + t.row * rh, width: 0, height: bh, rx: 2, fill: hueOf(t), opacity: 0 }, svg);
    bars.push({ t, rect, w: -1, X });
  }
  const cursor = el('rect', { x: x0, y: 0, width: 2, height: H, fill: '#fff', opacity: 0.85 }, svg);
  return { bars, X, cursor, x0, x1, bands };
}

// ======================================================================================================== UI (header / panels)
function buildUI(data, script, layout, scene) {
  const meta = data.threadMeta && data.threadMeta.metadata || {};
  const inc = meta.foghorn_incident_id || meta.foghorn_event_id || '';
  $('gname').textContent = data.graph;
  $('gmeta').textContent = `${inc ? inc + ' · ' : ''}thread ${short(data.threadId, 6)}${script.runs.length > 1 ? ' · ' + script.runs.length + ' runs' : ''}`;
  // state strip facts (only those the data can show)
  const has = (pred) => script.items.some(pred);
  const facts = [{ id: 'status', label: 'status' }, { id: 'steps', label: 'supersteps' }];
  const opt = [];
  if (has((i) => i.type === 'custom' && i.kind === 'evidence.found')) opt.push({ id: 'evidence', label: 'evidence' });
  if (has((i) => i.type === 'start' && i.name === 'tools')) opt.push({ id: 'tools', label: 'tool calls' });
  if (has((i) => i.type === 'custom' && i.kind === 'hypothesis.update')) opt.push({ id: 'lead', label: 'leading', wide: true });
  if (has((i) => i.type === 'custom' && i.kind === 'policy.check')) opt.push({ id: 'policy', label: 'policy' });
  opt.push({ id: 'tasks', label: 'tasks' });
  if (has((i) => i.type === 'custom' && i.kind === 'decision')) opt.push({ id: 'decision', label: 'decision', wide: true });
  if (has((i) => i.type === 'custom' && i.kind === 'artifact')) opt.push({ id: 'artifacts', label: 'artifacts' });
  for (const f of opt) if (facts.length < 5) facts.push(f);
  const fc = $('facts');
  for (const f of facts) {
    const d = document.createElement('div'); d.className = 'fact' + (f.wide ? ' wide' : ''); d.innerHTML = `<span class="k">${f.label}</span><span class="v">-</span>`;
    fc.appendChild(d); f.el = d; f.v = d.lastChild; f.sig = '';
  }
  if (!has((i) => i.type === 'msg')) { $('tokp').style.display = 'none'; $('tickerp').classList.add('big'); }
  // minimap
  const mini = $('mini');
  const B = layout.bounds;
  const sc = Math.min(190 / (B.x1 - B.x0), 34 / (B.y1 - B.y0));
  const mm = { svg: mini, sc, B, nodes: new Map(), view: null };
  for (const n of layout.nodes.values()) {
    if (n.kind === 'start' || n.kind === 'end' || n.kind === 'hook') continue;
    if (n.container) continue;
    const r = el('rect', { x: ((n.x - B.x0) * sc).toFixed(1), y: ((n.y - B.y0) * sc).toFixed(1), width: Math.max(2, n.w * sc).toFixed(1), height: Math.max(2, n.h * sc).toFixed(1), rx: 1, fill: '#3a4560' }, mini);
    mm.nodes.set(n.id, r);
  }
  mm.view = el('rect', { fill: 'none', stroke: '#fff', 'stroke-width': 1.2, rx: 2 }, mini);
  return { tickN: has((i) => i.type === 'msg') ? 4 : 7, facts, mm, tick: { sig: '', el: $('ticker') }, toks: { sig: '', el: $('toks'), lanes: new Map() }, card: { el: $('card'), key: '' }, bub: { el: $('bub'), key: '', on: false }, cap: { el: $('cap'), key: '' }, stxt: $('stxt'), status: $('status'), vig: $('vig'), world: $('world') };
}

async function loadBeats(url, script, layout) {
  if (!url) return [];
  let list = [];
  try { list = await (await fetch(url)).json(); } catch (err) { console.warn('beats: cannot load', url, err); return []; }
  const out = [];
  for (const b of list) {
    const t = resolveBeat(String(b.at), script);
    if (t == null) { console.warn('beats: unresolved selector', b.at); continue; }
    out.push({ t, text: String(b.text || ''), hold: b.hold != null ? +b.hold : 3, at: b.at });
  }
  return out.sort((a, b) => a.t - b.t);
}

function resolveBeat(sel, script) {
  const parts = sel.split(':');
  const kind = parts[0];
  if (kind === 't') return parseFloat(parts[1]);
  if (kind === 'interrupt') { const n = parts[1] ? +parts[1] : 1; const l = script.items.filter((i) => i.type === 'interrupt'); return l[n - 1] ? l[n - 1].a : null; }
  if (kind === 'resume') { const n = parts[1] ? +parts[1] : 1; const l = script.runs.filter((r) => r.resume); return l[n - 1] ? l[n - 1].a0 : null; }
  if (kind === 'custom') {
    const n = parts.length > 2 && /^\d+$/.test(parts[parts.length - 1]) ? +parts.pop() : 1;
    const k = parts.slice(1).join(':');
    const l = script.items.filter((i) => i.type === 'custom' && i.kind === k);
    return l[n - 1] ? l[n - 1].a : null;
  }
  if (kind === 'task') {
    let n = 1;
    if (/^\d+$/.test(parts[parts.length - 1])) n = +parts.pop();
    const which = parts.pop();
    const name = parts.slice(1).join(':');
    const l = script.tasks.filter((t) => t.name === name || t.node === name).sort((a, b) => a.start - b.start);
    if (!l[n - 1]) return null;
    return which === 'done' ? l[n - 1].end : l[n - 1].start;
  }
  return null;
}

function autoBeats(script, layout) {
  const out = [];
  const seen = new Set();
  for (const r of script.runs) out.push({ t: r.a0, label: r.resume ? 'resume' : `run:${r.index + 1}:start` });
  for (const t of script.tasks) {
    if (t.ns) continue;
    const k = `task:${t.name}:start`;
    if (!seen.has(k)) { seen.add(k); out.push({ t: t.start, label: k }); }
    const k2 = `task:${t.name}:done`;
    if (!seen.has(k2)) { seen.add(k2); out.push({ t: t.end, label: k2 }); }
  }
  const kc = new Map();
  for (const it of script.items) {
    if (it.type === 'custom' && !it.hidden) { const n = (kc.get(it.kind) || 0) + 1; kc.set(it.kind, n); if (n <= 3) out.push({ t: it.a, label: `custom:${it.kind}${n > 1 ? ':' + n : ''}` }); }
    if (it.type === 'interrupt') out.push({ t: it.a, label: 'interrupt' });
  }
  // fan-outs
  for (const [id, n] of script.fan) if (n > 1) { const t = script.tasks.find((x) => x.node === id); if (t) out.push({ t: t.start, label: `fanout:${id}:x${n}` }); }
  out.push({ t: script.duration, label: 'end' });
  return out.sort((a, b) => a.t - b.t);
}

// ======================================================================================================== render
const KIND = {
  'handoff': ['handoff', '#8aa0ff'], 'evidence.found': ['evidence', '#4fd1c5'], 'hypothesis.update': ['hypothesis', '#cf8ae8'], 'policy.check': ['policy', '#7bd88a'],
  'plan.proposed': ['plan', '#f6a560'], 'decision': ['decision', '#f2c14e'], 'memory.recalled': ['recalled', '#f48fb1'], 'memory.written': ['memory', '#f48fb1'],
  'progress': ['progress', '#a7b3c4'], 'artifact': ['artifact', '#7ab8ff'], 'note': ['note', '#a7b3c4'], 'metric': ['metric', '#a7b3c4'], 'log': ['tool', '#f6a560'],
};
function fmtCustom(it) {
  const d = it.data || {}, g = it.group;
  const more = it.count > 1 ? ` <span class="x">+${it.count - 1} more</span>` : '';
  switch (it.kind) {
    case 'handoff': return `<b>to ${esc(d.to)}</b> <span class="x">${esc(clip(d.reason, 90))}</span>`;
    case 'evidence.found': return `<b>${esc(clip(d.id || d.ref, 40))}</b>${more} <span class="x">${esc(clip(d.summary, 70))}</span>`;
    case 'hypothesis.update': {
      const best = (g || [d]).reduce((p, q) => (q.score > p.score ? q : p));
      return `<b>${esc(clip(best.label || best.id, 38))}</b> ${Math.round((best.score || 0) * 100)}%${more}`;
    }
    case 'policy.check': {
      const all = g || [d]; const bad = all.filter((x) => (x.result || x.status) !== 'pass');
      return all.length > 1 ? `<b>${all.length} checks</b> ${bad.length ? esc(bad.map((x) => x.rule).join(', ') + ' failed') : 'all pass'} <span class="x">${esc(clip(all[0].rule, 12))}…</span>` : `<b>${esc(d.rule)}</b> ${esc(d.result)} <span class="x">${esc(clip(d.text, 60))}</span>`;
    }
    case 'plan.proposed': return `<b>${esc((d.actions || []).map((a) => `${a.action} ${a.target}`).join(', '))}</b> <span class="x">${esc(clip((d.actions || [])[0] && (d.actions[0].risk + ' risk'), 30))}</span>`;
    case 'decision': return `<b>${esc(clip(d.what, 60))}</b> <span class="x">${esc(clip(d.why, 60))}</span>`;
    case 'memory.recalled': return `<b>${esc(d.key)}</b> <span class="x">${esc(clip(d.summary, 70))}</span>`;
    case 'memory.written': return `<b>${esc([].concat(d.ns || []).join('/'))}/${esc(d.key)}</b>`;
    case 'artifact': return `<b>${esc(d.name)}</b> <span class="x">${esc(d.kind)}</span>`;
    case 'progress': return `${esc(clip(d.label, 90))}`;
    case 'note': return esc(clip(d.text, 90));
    case 'metric': return `<b>${esc(d.name)}</b> ${esc(d.value)} ${esc(d.unit || '')}`;
    case 'log': return `<b>${esc(clip(d.text, 70))}</b>`;
    default: return esc(clip(JSON.stringify(d), 80));
  }
}

function renderAll(R, t) {
  const { sim, layout, scene, ui, script, camera, wf } = R;
  const live = sim.status;
  const finished = t >= script.endA - 0.01 && live !== 'interrupted' && live !== 'error';
  R.finished = finished;
  // ---- camera
  const v = camAt(camera, t);
  const tx = VW / 2 - v.cx * v.s, ty = VH / 2 + v.oy - v.cy * v.s;
  R.view = v;
  const wsig = `${tx.toFixed(1)},${ty.toFixed(1)},${v.s.toFixed(4)}`;
  if (ui.worldSig !== wsig) { ui.world.setAttribute('transform', `translate(${tx.toFixed(2)},${ty.toFixed(2)}) scale(${v.s.toFixed(4)})`); ui.worldSig = wsig; }

  // ---- nodes
  for (const [id, rec] of scene.nodes) {
    const n = sim.nodes.get(id);
    if (rec.n.kind === 'start' || rec.n.kind === 'end') {
      const lit = rec.n.kind === 'start' ? sim.status !== 'idle' : R.finished;
      const sig = lit ? 'l' : 'n';
      if (sig !== rec.sig) { rec.g.setAttribute('class', `term ${rec.n.kind}${lit ? ' lit' : ''}`); rec.sig = sig; }
      continue;
    }
    if (rec.fn) {
      const st = n ? n.st : 'idle';
      const sig = st;
      if (sig !== rec.sig) { rec.g.setAttribute('class', `fnent ${st}`); scene.fn.chk.style.opacity = st === 'done' ? 1 : 0; rec.sig = sig; }
      continue;
    }
    const st = n ? n.st : 'idle';
    const pips = n ? n.pips.join('') : '';
    const sig = `${st}|${n ? n.runs : 0}|${n ? n.wave : 0}|${n ? n.waveDone : 0}|${pips}`;
    if (sig === rec.sig) continue;
    rec.sig = sig;
    if (rec.n.kind === 'hook') { rec.g.setAttribute('class', `hook ${st === 'running' ? 'running' : st === 'done' ? 'done' : ''}`); continue; }
    const multi = n && n.runs >= 2;
    rec.g.setAttribute('class', `node ${st}${multi ? ' multi' : ''}${rec.n.fan > 1 ? ' fan' : ''}`);
    if (multi) rec.cnt.textContent = `↻${n.runs}`;
    if (rec.n.fan > 1) {
      for (let i = 0; i < rec.pips.length; i++) rec.pips[i].setAttribute('data-s', (n && n.pips[i]) || 'idle');
      const w = n ? n.wave : 0, d = n ? n.waveDone : 0;
      rec.pips.bt.textContent = !w ? `×${rec.n.fan}` : d === 0 ? `×${w}` : `${d}/${w}`;
      rec.pips.bdg.setAttribute('class', `bdg${w && d === w ? ' full' : ''}`);
    }
  }
  // containers
  for (const [id, rec] of scene.conts) {
    const n = sim.nodes.get(id);
    const st = n ? n.st : 'idle';
    if (st !== rec.st) { rec.g.setAttribute('class', `cont ${st}`); rec.st = st; }
  }
  // functional chips
  if (scene.fn) {
    for (const [key, rec] of scene.chips) {
      const c = sim.chips.get(key);
      const st = c ? c.st : 'hidden';
      const flash = c && c.replay && t - c.a0 < 0.9 ? 'flash' : '';
      const sig = st + flash;
      if (sig === rec.sig) continue;
      rec.sig = sig;
      rec.g.setAttribute('opacity', st === 'hidden' ? 0 : 1);
      rec.g.setAttribute('class', `chip ${st === 'hidden' ? '' : st}${flash ? ' flash' : ''}`);
      if (rec.conn) rec.conn.setAttribute('opacity', st === 'hidden' ? 0 : 1);
    }
  }
  // ---- edges (used) + trails
  for (const [id, rec] of scene.edges) {
    const ua = sim.used.get(id);
    const on = ua != null && t >= ua + 0.35;
    if (on !== rec.on) { rec.on = on; const cl = rec.g.getAttribute('class').replace(/ on\b/, ''); rec.g.setAttribute('class', on ? cl + ' on' : cl); }
  }
  const pool = scene.trailPool;
  let k = 0;
  for (const tr of sim.trails) {
    const p = (t - tr.a0) / 0.62;
    if (p < 0 || p > 1) continue;
    let slot = pool[k];
    if (!slot) { slot = { g: el('path', { class: 'trailg' }, $('gTrail')), c: el('path', { class: 'trail' }, $('gTrail')), d: '' }; pool[k] = slot; }
    k++;
    const L = tr.rec.len || 100, Lc = Math.min(70, L * 0.45);
    if (slot.d !== tr.rec.d) { slot.g.setAttribute('d', tr.rec.d); slot.c.setAttribute('d', tr.rec.d); slot.d = tr.rec.d; }
    const e = ease(p);
    const off = tr.rev ? -L + e * (L + Lc) : Lc - e * (L + Lc);
    for (const x of [slot.g, slot.c]) { x.setAttribute('stroke', tr.hue); x.setAttribute('stroke-dasharray', `${Lc.toFixed(1)} ${(L + Lc + 10).toFixed(1)}`); x.setAttribute('stroke-dashoffset', off.toFixed(1)); x.style.display = ''; }
  }
  for (; k < pool.length; k++) { pool[k].g.style.display = 'none'; pool[k].c.style.display = 'none'; }

  // ---- handoff bubble
  renderBubble(R, t);
  // ---- header / status / cards
  const stClass = live === 'interrupted' ? 'int' : live === 'done' ? 'done' : live === 'error' ? 'err' : 'run';
  let label = live === 'interrupted' ? 'WAITING FOR HUMAN' : live === 'error' ? 'CRASHED' : live === 'done' ? 'DONE' : live === 'idle' ? 'READY' : 'RUNNING';
  if (live === 'running' && sim.resume && t - sim.resume.a0 < 6) label = sim.resume.afterError ? 'RE-RUN' : 'RESUMED';
  if (finished) label = 'DONE';
  if (script.empty) label = 'NO RECORDED RUN';
  if (ui.stxt.textContent !== label) ui.stxt.textContent = label;
  const cls = finished ? 'done' : stClass;
  if (ui.status.className !== cls) ui.status.className = cls;
  const vk = sim.interrupt ? 'on' : (sim.error && sim.status === 'error') ? 'on err' : '';
  if (ui.vigOn !== vk) { ui.vig.className = vk; ui.vigOn = vk; }
  renderCard(R, t);
  // ---- panels
  renderFacts(R, t);
  renderTicker(R, t);
  renderTokens(R, t);
  renderWaterfall(R, t);
  renderMini(R, t, v);
  renderCaption(R, t);
}

function renderBubble(R, t) {
  const { sim, layout, ui } = R;
  const h = sim.handoff, b = ui.bub;
  if (!h || t - h.a0 > 3.0 || t < h.a0) { if (b.on !== false) { b.el.style.opacity = 0; b.on = false; } return; }
  const key = `${h.a0}|${h.to}`;
  const v = R.view;
  const cands = String(h.to).split(',').map((x) => x.trim()).filter((x) => layout.nodes.has(x));
  if (h.node) cands.push(h.node);
  if (!cands.length) { b.el.style.opacity = 0; b.on = false; return; }
  const scr = (bx) => ({ x0: (bx.x - v.cx) * v.s + VW / 2, x1: (bx.x + bx.w - v.cx) * v.s + VW / 2, y0: (bx.y - v.cy) * v.s + VH / 2 + v.oy, y1: (bx.y + bx.h - v.cy) * v.s + VH / 2 + v.oy });
  let pick = cands.find((id) => { const r = scr(layout.nodes.get(id)); return r.x0 > 10 && r.x1 < VW - 10 && r.y0 > 10 && r.y1 < VH - 10; }) || cands[0];
  const box = layout.nodes.get(pick);
  if (b.key !== key) {
    b.key = key;
    b.el.querySelector('.l1').textContent = `\u2192 ${clip(h.to, 40)}`;
    b.el.querySelector('.l2').textContent = clip(h.reason, 80);
    b.el.style.setProperty('--h', (layout.nodes.get(cands[0]) || box).hue || '#8aa0ff');
    b.w = b.el.offsetWidth; b.h = b.el.offsetHeight;
  }
  const r = scr(box);
  let x = clamp((r.x0 + r.x1) / 2 - b.w / 2, 8, VW - b.w - 8);
  let y = r.y0 - b.h - 10;
  if (y < 6) y = r.y1 + 12;
  y = clamp(y, 6, VH - b.h - 6);
  b.el.style.transform = `translate(${x.toFixed(1)}px,${y.toFixed(1)}px)`;
  const age = t - h.a0;
  b.el.style.opacity = clamp(Math.min(age / 0.25, (3.0 - age) / 0.5), 0, 1).toFixed(2); b.on = true;
}

function renderCard(R, t) {
  const { sim, ui } = R;
  const c = ui.card;
  let key = '', cls = '', html = null;
  if (sim.interrupt) {
    const i = sim.interrupt;
    key = `int|${i.a0}`;
    const multi = i.n > 1;
    html = {
      ct: '\u23f8  interrupt() \u2014 state saved to a checkpoint \u2014 waiting for a human',
      cm: multi ? `${i.n} interrupts at once (parallel)` : i.title,
      cs: multi ? i.titles.join('  |  ') : [i.kind, i.risk ? `risk ${i.risk}` : '', i.summary].filter(Boolean).join(' \u00b7 '),
      ck: `<span class="stamp">checkpoint ${esc(short(i.ckpt, 13) || 'saved')}</span><span>thread paused here; nothing runs until ${multi ? 'a resume answers them' : 'a resume arrives'}</span>`,
    };
    cls = 'on';
  } else if (sim.error && sim.status === 'error') {
    const e = sim.error;
    key = `err|${e.a0}`;
    html = {
      ct: `\u2716  ${e.node} failed \u2014 the run stops, the last checkpoint is safe`,
      cm: clip(e.text.split(':')[0] || 'error', 60),
      cs: clip(e.text.replace(/^[^:]*:\s*/, ''), 150),
      ck: '<span class="stamp">checkpoint kept</span><span>earlier steps are saved and will not run again</span>',
    };
    cls = 'on err';
  } else if (sim.resume && t - sim.resume.a0 < 2.6 && t >= sim.resume.a0) {
    const r = sim.resume;
    key = `res|${r.a0}`;
    html = r.afterError ? {
      ct: '\u25b6 Re-run from the last checkpoint \u2014 same thread, input=None',
      cm: r.node || 'the failed node',
      cs: 'only the failed step runs again; completed steps are read back from the checkpoint',
      ck: `<span class="stamp">checkpoint ${esc(short(r.ckpt, 13) || 'saved')}</span><span>new run on the same thread_id</span>`,
    } : {
      ct: '\u25b6 Command(resume=\u2026) \u2014 same thread, continues from the checkpoint',
      cm: r.nodes && r.nodes.length > 1 ? r.nodes[0] + ` (+${r.nodes.length - 1} parallel)` : (r.node || 'the paused node'),
      cs: 're-runs from the checkpoint; interrupt() now returns the human\u2019s answer',
      ck: `<span class="stamp">checkpoint ${esc(short(r.ckpt, 13) || 'saved')}</span><span>new run on the same thread_id</span>`,
    };
    cls = 'on res';
  }
  if (c.key !== key) {
    c.key = key;
    if (html) { c.el.querySelector('.ct').textContent = html.ct; c.el.querySelector('.cm').textContent = html.cm; c.el.querySelector('.cs').textContent = html.cs; c.el.querySelector('.cs').style.display = html.cs ? '' : 'none'; c.el.querySelector('.ck').innerHTML = html.ck; }
    c.el.className = cls;
  }
}

function renderFacts(R, t) {
  const { sim, ui, script } = R;
  const F = sim.facts;
  for (const f of ui.facts) {
    let txt = '', cl = '';
    switch (f.id) {
      case 'status': txt = sim.status === 'interrupted' ? 'paused' : sim.status === 'error' ? 'crashed' : sim.status === 'idle' ? 'ready' : (R.finished ? 'done' : (sim.resume && t - sim.resume.a0 < 6 ? (sim.resume.afterError ? 're-run' : 'resumed') : 'running')); cl = txt === 'paused' ? 'amber' : txt === 'crashed' ? 'err' : txt === 'done' ? 'ok' : 'hot'; break;
      case 'steps': txt = String(F.steps); break;
      case 'tasks': txt = `${F.done}/${F.started}`; break;
      case 'evidence': txt = String(F.evidence.size); break;
      case 'tools': txt = String(F.tools); break;
      case 'lead': txt = F.lead ? `${clip(String(F.lead.label).split(' on ')[0], 16)} ${Math.round(F.lead.score * 100)}%` : '-'; cl = F.lead ? 'hot' : ''; break;
      case 'policy': txt = F.polFail ? `${F.polPass} pass, ${F.polFail} fail` : `${F.polPass} pass`; break;
      case 'decision': txt = F.decision ? clip(F.decision.replace(/ ->.*/, ''), 18) : '-'; break;
      case 'artifacts': txt = String(F.artifacts); break;
      default: break;
    }
    const sig = txt + cl;
    if (sig !== f.sig) { f.sig = sig; f.v.textContent = txt; f.el.className = `fact${f.wide ? ' wide' : ''} ${cl}`; }
  }
}

function renderTicker(R, t) {
  const { sim, ui } = R;
  const list = sim.ticker.slice(-(ui.tickN || 4));
  const sig = list.map((e) => `${e.a}:${t - e.a < 0.5 ? 'f' : ''}`).join('|') + `|${sim.status}`;
  if (sig === ui.tick.sig) return;
  ui.tick.sig = sig;
  ui.tick.el.innerHTML = list.map((e) => {
    const [pill, col] = KIND[e.it.kind] || [e.it.kind, '#a7b3c4'];
    let c = col;
    if (e.it.kind === 'policy.check' && ((e.it.group || [e.it.data]).some((x) => (x.result || x.status) !== 'pass'))) c = '#ff8b8f';
    return `<div class="tk${t - e.a < 0.5 ? ' fresh' : ''}"><span class="pill" style="--pc:${c}">${esc(pill)}</span><span class="tt">${fmtCustom(e.it)}</span></div>`;
  }).join('') || '<div class="empty">no custom events yet</div>';
}

function laneHue(R, lane) {
  const n = R.layout.nodes.get(lane);
  if (n && n.hue) return n.hue;
  let h = 0; for (const ch of lane) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PAL[h % PAL.length];
}
function renderTokens(R, t) {
  const { sim, ui } = R;
  const act = [...sim.lanes.values()].filter((l) => t - l.aLast < 3.2 && l.text).sort((a, b) => a.a0 - b.a0).slice(-3);
  const key = act.map((l) => l.lane + l.mid).join('|');
  const T2 = ui.toks;
  if (key !== T2.sig) {
    T2.sig = key; T2.lanes.clear();
    T2.el.innerHTML = '';
    if (!act.length) T2.el.innerHTML = '<div class="empty">waiting for model output…</div>';
    for (const l of act) {
      const d = document.createElement('div'); d.className = 'lane'; d.style.setProperty('--lc', laneHue(R, l.lane));
      d.innerHTML = `<div class="ln">${esc(l.lane)}</div><div class="lt"><div class="lx"></div></div>`;
      T2.el.appendChild(d);
      T2.lanes.set(l.lane + l.mid, { lx: d.querySelector('.lx'), len: -1, caret: -1 });
    }
  }
  const newest = act.length ? act.reduce((p, q) => (q.aLast > p.aLast ? q : p)) : null;
  for (const l of act) {
    const r = T2.lanes.get(l.lane + l.mid);
    if (!r) continue;
    const streaming = l === newest && t - l.aLast < 1.1;
    const sg = l.text.length * 2 + (streaming ? 1 : 0);
    if (sg === r.len) continue;
    r.len = sg;
    const txt = l.text.length > 420 ? '…' + l.text.slice(-420) : l.text;
    r.lx.innerHTML = esc(txt).replace(/‣ ([\w.\-]+)\(([^)\n]*)\)/g, '<span class="tool">‣ $1</span>($2)') + (streaming ? '<span class="caret"></span>' : '');
  }
}

function renderWaterfall(R, t) {
  const { wf } = R;
  for (const b of wf.bars) {
    const tk = b.t;
    let w = 0, op = 0;
    if (t >= tk.start) { w = Math.max(2, wf.X(Math.min(t, tk.end)) - wf.X(tk.start)); op = t >= tk.end ? 0.9 : 1; }
    const q = Math.round(w * 2) / 2;
    if (q !== b.w) { b.w = q; b.rect.setAttribute('width', q); b.rect.setAttribute('opacity', q > 0 ? op : 0); if (tk.status === 'interrupted' && t >= tk.end) b.rect.setAttribute('fill', AMBER); }
  }
  wf.cursor.setAttribute('x', (wf.X(t) - 1).toFixed(1));
}

function renderMini(R, t, v) {
  const { ui, camera, sim } = R;
  const m = ui.mm;
  if (camera.static) { if (m.svg.style.display !== 'none') m.svg.style.display = 'none'; return; }
  if (m.svg.style.display !== 'block') m.svg.style.display = 'block';
  const x = (v.cx - VW / (2 * v.s) - m.B.x0) * m.sc, y = (v.cy - (VH / 2 + v.oy) / v.s - m.B.y0) * m.sc;
  m.view.setAttribute('x', x.toFixed(1)); m.view.setAttribute('y', y.toFixed(1));
  m.view.setAttribute('width', (VW / v.s * m.sc).toFixed(1)); m.view.setAttribute('height', (VH / v.s * m.sc).toFixed(1));
  for (const [id, r] of m.nodes) {
    const n = sim.nodes.get(id);
    const hot = n && (n.st === 'running');
    const fill = n ? (n.st === 'running' ? R.layout.nodes.get(id).hue : n.st === 'interrupted' ? AMBER : n.st === 'done' ? '#5b6b8f' : '#3a4560') : '#3a4560';
    if (r.__f !== fill) { r.setAttribute('fill', fill); r.__f = fill; }
  }
  // containers: tint by the container node state too
  for (const c of R.layout.containers.values()) {
    const n = sim.nodes.get(c.id);
  }
}

function renderCaption(R, t) {
  const { userBeats, ui } = R;
  let cur = null;
  for (const b of userBeats) if (t >= b.t && t < b.t + b.hold) cur = b;
  const key = cur ? `${cur.t}|${cur.text}` : '';
  if (key !== ui.cap.key) {
    ui.cap.key = key;
    ui.cap.el.textContent = cur ? cur.text : '';
    ui.cap.el.className = cur && P.get('caps') !== '0' ? 'on' : '';
  }
}

// ======================================================================================================== go
(async () => {
  try {
    // edge index must be attached to layout before Sim is created: Sim.trail() reads layout.__edgeIdx
    await boot();
  } catch (err) {
    console.error(err);
    document.body.insertAdjacentHTML('beforeend', `<pre style="position:absolute;left:20px;top:100px;color:#ff8b8f;font:15px monospace;white-space:pre-wrap;width:900px">${esc(err && err.stack || err)}</pre>`);
    window.__error = String(err);
    window.__done = true;
  }
})();
