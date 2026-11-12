// live.js: runs a REAL LangGraph graph from this page (POST /threads, POST /threads/{id}/runs/stream) and animates the events.
// All structure comes from GET /assistants/{id}/graph?xray=true; all data from the stream (tasks / checkpoints / custom / updates).
import { API, FONT, MONO, jpost, loadAssistants, loadSchemas, buildGraph, createView, textW } from './lg.js';

// ============================================================================================================
// EDIT ME: scenarios (caption texts, inputs, how each interrupt is answered, what the state cards show)
//   caption `at` triggers: start | node:<id> | fan:<id> | fandone:<id> | cond:<src>><dst> | edge:<src>><dst> | loop:<id> | interrupt | interrupt:<id> | resume | end
//   {{double braces}} = gold highlight. {n} = fan-out size, {ckpts} = checkpoints so far, {thread} = thread id.
// ============================================================================================================
const SCENARIOS = {
  casting: {
    graph: 'casting_call',
    speed: 1.1,
    input: () => ({}),
    fan: { audition: { cols: 8, rowKey: 'role', reducer: 'auditions' } },
    channels: [
      { k: 'candidate_ids', w: 1.0, fmt: (v) => (v.candidate_ids ? `${v.candidate_ids.length} puppets` : null) },
      { k: 'auditions', w: 1.35, tag: 'reducer: operator.add', fmt: (v, L) => (L.count.audition !== undefined ? `${L.count.audition} / ${L.total.audition}` : v.auditions && v.auditions.length ? `${v.auditions.length} / ${v.auditions.length}` : null) },
      { k: 'scores', w: 0.95, fmt: (v) => (v.scores ? `${Object.keys(v.scores).length} roles × ${Object.keys(Object.values(v.scores)[0] || {}).length}` : null) },
      { k: 'proposed', w: 2.0, fmt: (v) => (v.proposed ? Object.values(v.proposed).join(' · ') : null) },
      { k: 'cast', w: 2.0, fmt: (v) => (v.cast ? Object.values(v.cast).join(' · ') : null) },
    ],
    answer: (kind, p) => ({ cast: p.proposed }),
    captions: [
      { at: 'start', text: 'A run = a {{thread_id}} plus a stream of events from the LangGraph server', min: 2.6 },
      { at: 'node:load', soft: true, text: '{{node}} = a plain function: it reads the shared state and returns updates', min: 2.8 },
      { at: 'fan:audition', text: '{{Send API}}: one node, {n} parallel tasks (8 puppets × 3 roles)', min: 3.2 },
      { at: 'fandone:audition', text: '{{reducer}} merges the {n} results into one list: auditions = operator.add', min: 3.2 },
      { at: 'node:rank', soft: true, text: 'rank is deferred: it runs once, after every audition has landed', min: 2.4 },
      { at: 'node:recommend', soft: true, text: 'Madame recommends a cast: this node calls the language model', min: 2.4 },
      { at: 'interrupt', text: '{{interrupt()}}: state is checkpointed under this thread_id; nothing runs until a resume', min: 3.4 },
      { at: 'resume', text: '{{Command(resume=…)}} wakes the same thread exactly where it stopped', min: 2.6 },
      { at: 'end', text: 'Finished: {ckpts} checkpoints saved, one per step', min: 2.4 },
    ],
  },
  writers: {
    graph: 'writers_room',
    speed: 1.5,
    input: () => ({}),
    fan: {},
    groupFan: { quill: { n: 3 } },
    groupSub: '× 3 via Send',
    channels: [
      { k: 'round', w: 0.8, fmt: (v) => (v.round !== undefined ? String(v.round) : null) },
      { k: 'drafts', w: 1.65, tag: 'reducer: operator.add', fmt: (v, L) => (L.count.quill !== undefined ? `${L.count.quill} / ${L.total.quill}` : v.drafts ? `${v.drafts.length} drafts` : null) },
      { k: 'shortlist', w: 1.2, fmt: (v) => (v.shortlist ? `${v.shortlist.length} titles` : null) },
      { k: 'chosen_id', w: 1.9, fmt: (v) => v.chosen_id || null },
      { k: 'script', w: 2.6, fmt: (v) => (v.script && v.script.title ? v.script.title : null) },
    ],
    answer: (kind, p) => ({ draft_id: p.drafts[0].draft_id }),
    captions: [
      { at: 'start', text: 'writers_room: three heron quills draft in parallel, then a judge scores them', min: 2.8 },
      { at: 'fan:quill', text: '{{Send API}}: the quill subgraph runs ×{n} in parallel, one thread of work per heron', min: 3.2 },
      { at: 'node:quill:self_check', text: '{{conditional edge}} inside the subgraph: self_check can loop back to draft', min: 3.0 },
      { at: 'fandone:quill', text: '{{reducer}} merges the three drafts into one list: drafts = operator.add', min: 2.8 },
      { at: 'node:judge', soft: true, text: 'judge is deferred: it waits for all three quills, then builds the shortlist', min: 2.6 },
      { at: 'cond:judge>pick_draft', text: '{{conditional edge}}: loop back to revise, or continue to pick_draft', min: 3.6 },
      { at: 'interrupt', text: '{{interrupt()}}: the graph stops and the checkpoint holds the drafts until a human picks', min: 3.2 },
      { at: 'resume', text: '{{Command(resume=…)}} carries the pick back in and the run continues', min: 2.4 },
      { at: 'end', text: 'Finished: {ckpts} checkpoints saved, one per step', min: 2.4 },
    ],
  },
  performance: {
    graph: 'performance',
    speed: 1.6,
    input: () => ({}),
    fan: { react: { cols: 6 } },
    channels: [
      { k: 'beat', w: 1.1, fmt: (v) => (v.show && v.show.beat !== undefined ? `${Math.min(v.show.beat, 4) + (v.show.beat >= 5 ? 1 : 1)} of 5` : null), label: 'show.beat' },
      { k: 'warmth', w: 1.0, fmt: (v) => (v.show && v.show.warmth !== undefined ? String(v.show.warmth) : null), label: 'show.warmth' },
      { k: 'chaos', w: 1.0, fmt: (v) => (v.show && v.show.chaos !== undefined ? String(v.show.chaos) : null), label: 'show.chaos' },
      { k: 'reactions', w: 1.5, tag: 'reducer: operator.add', fmt: (v, L) => (L.count.react !== undefined ? String((L.base.react || 0) + L.count.react) : v.reactions ? String(v.reactions.length) : null) },
      { k: 'mishaps', w: 1.0, fmt: (v) => (v.show && v.show.mishaps ? String(v.show.mishaps.length) : null), label: 'show.mishaps' },
      { k: 'pending', w: 2.3, fmt: (v, L) => L.pending || null, label: 'interrupt value' },
    ],
    answer: (kind, p) => ({ option: (p.options[1] || p.options[0]).id, cue: null, text: null }),
    captions: [
      { at: 'start', text: 'performance: one looping graph runs the whole show, beat after beat', min: 2.6 },
      { at: 'node:direct', soft: true, text: 'direct: the supervisor node decides what happens in this beat', min: 2.6 },
      { at: 'fan:react', text: '{{Send API}} again: the paper audience reacts, {n} parallel tasks every beat', min: 3.0 },
      { at: 'fandone:react', text: '{{reducer}}: reactions = operator.add appends {n} new reactions per beat', min: 2.8 },
      { at: 'cond:tally>direct', text: '{{conditional edge}}: loop back to direct for the next beat, or continue to curtain_call', min: 3.4 },
      { at: 'interrupt', text: '{{interrupt()}}: a mishap freezes the stage; state is checkpointed and nothing runs until a resume', min: 3.2 },
      { at: 'resume', text: '{{Command(resume=…)}}: the whisper from the Prompter\'s box wakes the run exactly where it stopped', min: 3.2 },
      { at: 'loop:direct', text: 'every step is a {{checkpoint}}: any beat can be replayed later (time travel)', hold: 3.6, once: true },
      { at: 'end', text: 'Finished: {ckpts} checkpoints saved, one per step', min: 2.4 },
    ],
  },
  almanac: {
    graph: 'almanac',
    speed: 1.15,
    input: () => ({ tour_id: 'viz-' + Math.random().toString(36).slice(2, 8) }),
    fan: {},
    channels: [
      { k: 'noted', w: 1.0, fmt: (v) => (v.noted ? `${v.noted.length} legends` : null) },
      { k: 'Store: put', w: 1.1, fmt: (v, L) => (L.custom['memory.written'] ? `${L.custom['memory.written']} items` : null), label: 'Store: put' },
      { k: 'candidates', w: 1.1, fmt: (v) => (v.candidates ? String(v.candidates.length) : null) },
      { k: 'chosen', w: 1.8, fmt: (v) => (v.chosen ? v.chosen.join(', ') || '(none)' : null) },
      { k: 'traditions', w: 1.4, fmt: (v) => (v.traditions ? v.traditions.map((t) => t.title || t.calamity).join(', ') : null) },
      { k: 'Store: search', w: 1.2, fmt: (v, L) => (L.custom['memory.recalled'] ? `${L.custom['memory.recalled']} hits` : null), label: 'Store: search' },
    ],
    answer: (kind, p) => ({ enshrine: [p.candidates[0].calamity] }),
    captions: [
      { at: 'start', text: 'almanac: tonight\'s calamities become long-term memory in the {{Store}}', min: 2.8 },
      { at: 'node:ingest', text: 'ingest writes every mishap into the Store (put) and indexes it for {{semantic search}}', min: 3.4 },
      { at: 'cond:detect>curate', text: '{{conditional edge}}: curate only when a calamity struck twice, otherwise straight to recall', min: 3.4 },
      { at: 'interrupt', text: '{{interrupt()}}: the Prompter chooses which calamity becomes a Tradition', min: 3.0 },
      { at: 'resume', text: '{{Command(resume=…)}} carries the choice in and enshrine writes it to the Store', min: 2.8 },
      { at: 'node:recall', text: 'recall: {{semantic search}} over the Store finds memories relevant to the next town', min: 3.4 },
      { at: 'end', text: 'Finished: {ckpts} checkpoints saved, one per step', min: 2.4 },
    ],
  },
};
SCENARIOS.generic = {
  graph: null, input: () => ({}), fan: {}, dynamic: true, channels: [],
  answer: (kind, p) => (p && p.kind === 'confirm_cast' ? { cast: p.proposed } : p && p.drafts ? { draft_id: p.drafts[0].draft_id } : p && p.options ? { option: p.options[0].id, cue: null, text: null } : p && p.candidates ? { enshrine: [] } : p && p.choices ? { choice: p.choices[0].id } : { approve: true }),
  captions: [
    { at: 'start', text: 'A run = a {{thread_id}} plus a stream of events from the LangGraph server', min: 2.6 },
    { at: 'interrupt', text: '{{interrupt()}}: state is checkpointed under this thread_id; nothing runs until a resume', min: 3.4 },
    { at: 'resume', text: '{{Command(resume=…)}} wakes the same thread exactly where it stopped', min: 2.6 },
    { at: 'end', text: 'Finished: {ckpts} checkpoints saved, one per step', min: 2.4 },
  ],
};
// How long things take on screen (ms at speed=1). The server finishes in milliseconds; the page paces it so humans can follow.
const PACE = { edge: 650, startDwell: 560, endDwell: 400, chip: 80, chipLater: 34, ckpt: 70, interruptWait: 2800, resumeShow: 1700, intro: 1500, hold: 2000, minRun: 1000 };
const LOOP_FACTOR = [1, 0.55, 0.34, 0.26, 0.22, 0.2];

// ============================================================================================================
const Q = new URLSearchParams(location.search);
const SCEN_KEY = Q.get('scenario') || { casting_call: 'casting', writers_room: 'writers', performance: 'performance', almanac: 'almanac' }[Q.get('graph')] || (Q.get('graph') ? 'generic' : 'casting');
const SC = SCENARIOS[SCEN_KEY];
const GRAPH = Q.get('graph') || SC.graph || 'casting_call';
const URL_SPEED = Math.max(0.2, parseFloat(Q.get('speed')) || 1);
const SPEED = URL_SPEED * (SC.speed || 1);
const CAPS_ON = Q.get('captions') !== '0';
const CONCEPT = Q.get('concept') === '1';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms / SPEED));
const sleepU = (ms) => new Promise((r) => setTimeout(r, ms / URL_SPEED)); // human-reading waits: not sped up by the scenario factor
const $ = (id) => document.getElementById(id);
const NSV = 'http://www.w3.org/2000/svg';

window.__events = [];
window.__captions = [];
window.__done = false;
window.__state = {};
const T0 = performance.now();
const now = () => (performance.now() - T0) / 1000;
const mark = (name, data) => { try { if (window.rec && window.rec.mark) window.rec.mark(name, data); } catch (e) { /* not recording */ } };

let G, V, A, runToken = 0;

// ---------------------------------------------------------------------------------------------------- page chrome
function buildChrome() {
  $('gid').textContent = GRAPH;
  document.title = `live ${GRAPH}`;
  // legend
  const lg = $('legend');
  lg.innerHTML = `<svg width="340" height="30" viewBox="0 0 340 30" style="display:block"><defs><marker id="lgm" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="12" markerHeight="12" markerUnits="userSpaceOnUse" orient="auto"><path d="M0.5,0.8 L9.5,5 L0.5,9.2 L2.8,5 z" fill="#9aa7c0"/></marker></defs>
    <path d="M2 15 L40 15" stroke="#9aa7c0" stroke-width="3" marker-end="url(#lgm)" fill="none"/><text x="50" y="16" fill="#9aa7c0" font-size="17" font-weight="600" dominant-baseline="central">edge</text>
    <path d="M112 15 L156 15" stroke="#9aa7c0" stroke-width="3" stroke-dasharray="9 7" marker-end="url(#lgm)" fill="none"/><text x="170" y="16" fill="#9aa7c0" font-size="17" font-weight="600" dominant-baseline="central">conditional edge</text></svg>`;
}
function setStatus(kind, text) {
  const s = $('status');
  s.className = `pill ${kind}`;
  s.textContent = text;
}
function buildStrip() {
  const strip = $('strip');
  strip.innerHTML = '';
  for (const c of SC.channels) {
    const d = document.createElement('div');
    d.className = 'card' + (c.tag ? ' tagged' : ''); d.dataset.k = c.k; d.style.flex = `${c.w} 1 0`;
    d.innerHTML = `<div class="ck">${c.label || c.k}</div><div class="cv dim">—</div>${c.tag ? `<div class="ct">${c.tag}</div>` : ''}`;
    strip.appendChild(d);
  }
}
const LIVE = { count: {}, total: {}, base: {}, custom: {}, pending: null };
function renderCards(values, flash = true) {
  for (const c of SC.channels) {
    const card = document.querySelector(`.card[data-k="${CSS.escape(c.k)}"]`);
    if (!card) continue;
    let t = null;
    try { t = c.fmt(values || {}, LIVE); } catch (e) { t = null; }
    const cv = card.querySelector('.cv');
    const txt = t === null || t === undefined ? '—' : String(t);
    if (cv.textContent !== txt) {
      cv.textContent = txt;
      const avail = card.clientWidth - 32;
      let fs = card.classList.contains('tagged') ? 23 : 26;
      while (fs > 17 && textW(txt, fs, 800) > avail) fs -= 1;
      cv.style.fontSize = fs + 'px';
      cv.classList.toggle('dim', t === null || t === undefined);
      if (flash && t !== null) { card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash'); }
    }
  }
}
// ---- caption strip: a caption stays at least `min` seconds; newer ones replace it as soon as that has passed; soft ones are dropped when busy
const Cap = {
  queue: [], showing: null, shownAt: 0, fired: new Set(), timer: null,
  fire(at, vars = {}) {
    if (!CAPS_ON) return;
    for (const d of SC.captions.filter((c) => c.at === at)) {
      if (d.once !== false && this.fired.has(at)) continue;
      this.fired.add(at);
      this.queue.push({ ...d, vars });
    }
    this.pump();
  },
  fmt(d) {
    const v = { n: '', ckpts: STATE.ckpts, thread: STATE.thread || '', ...d.vars };
    return d.text.replace(/\{(\w+)\}/g, (m, k) => (v[k] !== undefined ? v[k] : m));
  },
  pump() {
    if (this.timer) return;
    const tick = () => {
      this.timer = null;
      const t = performance.now();
      if (this.queue.length > 1) { const hard = this.queue.filter((c) => !c.soft); if (hard.length && hard.length < this.queue.length) this.queue = hard; }
      if (this.queue.length > 2) this.queue.splice(0, this.queue.length - 2);
      const min = this.showing ? (this.showing.soft ? 1.4 : (this.showing.min || 2.6)) * 1000 / URL_SPEED : 0;
      if (this.queue.length && (!this.showing || t - this.shownAt >= min)) this.show(this.queue.shift());
      if (this.queue.length) this.timer = setTimeout(tick, 100);
    };
    tick();
  },
  show(d) {
    const text = this.fmt(d);
    this.showing = d;
    this.shownAt = performance.now();
    const el = $('cap');
    el.classList.remove('in'); void el.offsetWidth;
    el.querySelector('.ctext').innerHTML = text.replace(/\{\{(.+?)\}\}/g, '<b>$1</b>');
    el.classList.add('in');
    const rec = { t: +now().toFixed(2), at: d.at, text: text.replace(/\{\{|\}\}/g, '') };
    window.__captions.push(rec);
    mark('caption', rec);
  },
};

// ---- checkpoint timeline
const TL = {
  ticks: [], W: 1232, x0: 128, x1: 850,
  init() {
    const s = $('tl');
    s.innerHTML = '';
    this.svg = s;
    const mk = (tag, a, p = s) => { const e = document.createElementNS(NSV, tag); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); p.appendChild(e); return e; };
    this.mk = mk;
    mk('line', { x1: this.x0 - 8, x2: this.x1, y1: 22, y2: 22, stroke: '#2b364f', 'stroke-width': 2 });
    const t = mk('text', { x: 0, y: 22, fill: '#9aa7c0', 'font-size': 17, 'font-weight': 600, 'dominant-baseline': 'central' }); t.textContent = 'checkpoints';
    this.g = mk('g', {});
    this.labels = mk('g', {});
    this.ticker = [mk('text', { x: 876, y: 15, fill: '#9aa7c0', 'font-size': 17, 'dominant-baseline': 'central', style: `font-family:${MONO}` }), mk('text', { x: 876, y: 38, fill: '#e8eefc', 'font-size': 17, 'dominant-baseline': 'central', style: `font-family:${MONO}` })];
    this.ticks = []; this.lines = ['', ''];
  },
  log(text) { this.lines = [this.lines[1], text]; this.ticker[0].textContent = this.lines[0]; this.ticker[1].textContent = this.lines[1]; },
  add(kind, info = {}) {
    this.ticks.push({ kind, info });
    this.layout();
  },
  layout() {
    const n = this.ticks.length;
    const gaps = this.ticks.filter((t) => t.kind === 'pause' || t.kind === 'resume').length * 34;
    const step = Math.min(15, (this.x1 - this.x0 - gaps) / Math.max(1, n));
    this.g.innerHTML = ''; this.labels.innerHTML = '';
    let x = this.x0;
    this.ticks.forEach((t, i) => {
      if (t.kind === 'resume') x += 34;
      const last = i === n - 1;
      const col = t.kind === 'pause' ? '#ffb938' : last ? '#4aa8ff' : t.kind === 'resume' ? '#f4c95d' : '#3f78b3';
      const h = t.kind === 'pause' || t.kind === 'resume' ? 34 : 24;
      this.mk('rect', { x: x - 2, y: 22 - h / 2, width: 4, height: h, rx: 2, fill: col, opacity: last ? 1 : 0.9 }, this.g);
      if (t.kind === 'pause' || t.kind === 'resume') {
        const tx = this.mk('text', { x: x, y: 47, fill: col, 'font-size': 16, 'font-weight': 700, 'dominant-baseline': 'central', 'text-anchor': 'middle' }, this.labels);
        tx.textContent = t.kind === 'pause' ? 'interrupt' : 'resume';
      }
      x += step;
      if (t.kind === 'pause') x += 34;
    });
  },
};

// ---------------------------------------------------------------------------------------------------- run state
const STATE = { thread: null, ckpts: 0, values: {}, runs: {}, lastDone: ['__start__'], seenEdge: new Set(), interrupted: null, ended: false, runNo: 1 };
function nodeRec(id) { return (STATE.runs[id] = STATE.runs[id] || { active: 0, done: 0, total: 0, runs: 0, startedAt: 0, chipOf: new Map(), fanId: null }); }

function nodeIdFor(ns, name) {
  if (ns.length && G.groups.has(ns[0].node)) return `${ns[0].node}:${name}`;
  return name;
}
function findEdge(dstId, groupName) {
  const cand = G.edges.filter((e) => (groupName ? e.toGroup === groupName : e.dst === dstId && !e.toGroup) && e.d);
  // prefer an edge whose source finished most recently
  for (const s of STATE.lastDone) {
    const e = cand.find((x) => x.src === s);
    if (e) return e;
  }
  return null;
}

// ---------------------------------------------------------------------------------------------------- SSE
async function* sseStream(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'text/event-stream' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`stream ${res.status}`);
  const rd = res.body.getReader(); const dec = new TextDecoder();
  let buf = '', ev = null, data = [];
  for (;;) {
    const { value, done } = await rd.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.search(/\r\n|\n|\r/)) >= 0) {
      const m = /^(\r\n|\n|\r)/.exec(buf.slice(i));
      const line = buf.slice(0, i); buf = buf.slice(i + m[0].length);
      if (line === '') { if (ev !== null || data.length) yield { event: ev || 'message', data: data.join('\n') }; ev = null; data = []; }
      else if (line.startsWith('event:')) ev = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
  }
}
function splitName(name) {
  const i = name.indexOf('|');
  if (i < 0) return { mode: name, ns: [] };
  return { mode: name.slice(0, i), ns: name.slice(i + 1).split('|').filter(Boolean).map((s) => { const j = s.indexOf(':'); return j < 0 ? { node: s, taskId: '' } : { node: s.slice(0, j), taskId: s.slice(j + 1) }; }) };
}

// ---------------------------------------------------------------------------------------------------- playback queue
class Queue {
  constructor() { this.items = []; this.waiter = null; this.closed = false; }
  push(it) { this.items.push(it); if (this.waiter) { const w = this.waiter; this.waiter = null; w(); } }
  close() { this.closed = true; if (this.waiter) { const w = this.waiter; this.waiter = null; w(); } }
  async next() {
    while (!this.items.length) { if (this.closed) return null; await new Promise((r) => { this.waiter = r; }); }
    return this.items.shift();
  }
}

/** Reads one HTTP stream, turns frames into queue items (coalescing a burst of identical task starts into one 'start' item). */
async function readRun(tid, body, q, token) {
  const interrupts = [];
  let pend = null;
  const flush = () => { if (pend) { q.push({ type: 'start', ...pend }); pend = null; } };
  const stream = sseStream(`${API}/threads/${tid}/runs/stream`, { assistant_id: A.id, stream_mode: ['values', 'updates', 'tasks', 'checkpoints', 'custom'], stream_subgraphs: true, stream_resumable: true, on_disconnect: 'continue', multitask_strategy: 'enqueue', ...body });
  for await (const ev of stream) {
    if (token !== runToken) return interrupts;
    const { mode, ns } = splitName(ev.event);
    let d = null;
    try { d = ev.data ? JSON.parse(ev.data) : null; } catch (e) { d = ev.data; }
    window.__events.push({ mode, ns: ns.map((n) => n.node).join('|'), name: d && d.name, t: +now().toFixed(3) });
    if (mode === 'tasks' && d && d.name && !d.name.startsWith('__')) {
      const isEnd = 'result' in d || ('error' in d && !('input' in d));
      if (!isEnd) {
        const key = ns.map((n) => n.node).join('|') + '/' + d.name;
        if (pend && pend.key === key) { pend.n++; pend.taskIds.push(d.id); pend.inputs.push(d.input); }
        else { flush(); pend = { key, node: d.name, ns, n: 1, taskIds: [d.id], inputs: [d.input] }; }
      } else {
        flush();
        const its = Array.isArray(d.interrupts) ? d.interrupts : [];
        for (const it of its) interrupts.push(it);
        q.push({ type: 'end', node: d.name, ns, taskId: d.id, result: d.result, error: d.error, interrupts: its });
      }
      continue;
    }
    if (ns.length && mode !== 'custom') continue; // sub-graph checkpoints / updates / values: not drawn, must not split a burst of task starts
    if (mode === 'updates' || mode === 'values' || mode === 'metadata') continue;
    flush();
    if (mode === 'checkpoints' && ns.length === 0 && d) q.push({ type: 'ckpt', id: d.config && d.config.configurable && d.config.configurable.checkpoint_id, step: d.metadata && d.metadata.step, source: d.metadata && d.metadata.source, next: d.next || [], values: d.values || {} });
    else if (mode === 'custom' && d) q.push({ type: 'custom', kind: d.kind, agent: d.agent, data: d.data, ns });
    else if (mode === 'error') q.push({ type: 'error', message: typeof d === 'string' ? d : (d && (d.message || d.error)) || 'error' });
  }
  flush();
  return interrupts;
}

// ---------------------------------------------------------------------------------------------------- the player (consumer)
const loopFactor = (runs) => LOOP_FACTOR[Math.min(runs, LOOP_FACTOR.length - 1)];

async function playStart(it) {
  const inner = it.ns.length > 0;
  const isGroupTask = !inner && G.groups.has(it.node);
  const gname = isGroupTask ? it.node : null;
  const nid = isGroupTask ? null : nodeIdFor(it.ns, it.node);
  const rec = nodeRec(isGroupTask ? 'g:' + gname : nid);
  const resuming = !isGroupTask && STATE.interrupted === nid;
  const lf = loopFactor(rec.runs);
  // edge into the node
  let edge = null;
  if (!resuming) {
    if (isGroupTask) edge = findEdge(null, gname);
    else if (inner) edge = findEdge(nid, null) && STATE.lastDone.includes(findEdge(nid, null).src) ? findEdge(nid, null) : null;
    else edge = findEdge(nid, null);
  }
  const first = !rec.runs;
  if (rec.runs && rec.done >= rec.total) { rec.active = 0; rec.done = 0; rec.total = 0; rec.chipOf.clear(); }
  rec.runs += resuming ? 0 : 1;
  if (resuming) { V.tag(nid, 'wait', null); V.tag(nid, 'resume', null); STATE.interrupted = null; }
  if (STATE.lastDone.length === 1 && STATE.lastDone[0] === '__start__') V.setNode('__start__', 'done');
  if (edge && !(rec.active > 0)) {
    V.setEdge(edge.id, 'active');
    if (edge.cond) Cap.fire(`cond:${edge.src}>${edge.dst}`, {});
    Cap.fire(`edge:${edge.src}>${edge.dst}`, {});
    await V.travel(edge.id, PACE.edge * lf * (inner ? 0.7 : 1));
    V.setEdge(edge.id, 'done');
    if (V.edges[edge.id]) { /* keep dashed look */ }
  }
  rec.active += it.n; rec.total += it.n; rec.startedAt = performance.now();
  const key = isGroupTask ? 'g:' + gname : nid;
  if (isGroupTask) V.setGroup(gname, 'running');
  else V.setNode(nid, 'running');
  // fan-out chips and badges
  const fanCfg = !isGroupTask && SC.fan && SC.fan[nid];
  if (isGroupTask) {
    if (SC.groupFan && SC.groupFan[gname]) { showGroupFan(gname, it.n, it.taskIds); Cap.fire(`fan:${gname}`, { n: it.n }); }
  } else if (it.n > 1) {
    V.badge(nid, 'n', `×${rec.total}`, { fill: '#f4c95d' });
    if (fanCfg) {
      const cols = Math.min(fanCfg.cols || it.n, it.n);
      const labels = fanCfg.rowKey ? Array.from({ length: Math.ceil(it.n / cols) }, (_, r) => (it.inputs[r * cols] || {})[fanCfg.rowKey] || '') : null;
      V.setFan(nid, it.n, cols, { rowLabels: labels });
      rec.fanId = nid;
      it.taskIds.forEach((id, i) => rec.chipOf.set(id, i));
      for (let i = 0; i < it.n; i++) V.chipState(nid, i, 'run');
      V.fanFoot(nid, `0 / ${it.n} tasks`);
      LIVE.total[nid] = it.n; LIVE.count[nid] = 0; LIVE.base[nid] = (STATE.values.reactions || []).length;
      renderCards(STATE.values, false);
      Cap.fire(`fan:${nid}`, { n: it.n });
    }
  }
  if (rec.runs >= 2 && !isGroupTask && !inner) V.badge(nid, 'loop', `↻ ${rec.runs}`, { slot: 'tl', fill: '#2a4a73', color: '#cfe6ff', size: 17, h: 26 });
  if (inner && it.n > 1) V.badge(nid, 'n', `×${it.n}`, { fill: '#f4c95d' });
  if (!isGroupTask) {
    Cap.fire(`node:${nid}`, {});
    if (rec.runs >= 2) Cap.fire(`loop:${nid}`, {});
  }
  STATE.seenEdge.add(key);
  await sleep(PACE.startDwell * lf);
}

function showGroupFan(gname, n, taskIds) {
  const gb = G.groups.get(gname).box;
  const id = '__grp_' + gname;
  V.clearFan(id);
  // three chips under the group's left part
  const cs = V.G.opt.chip, cg = V.G.opt.chipGap;
  const w = n * (cs + cg) - cg;
  const gEl = document.createElementNS(NSV, 'g');
  gEl.setAttribute('transform', `translate(${gb.x + 22},${gb.y + gb.h + 14})`);
  V.gOver.appendChild(gEl);
  const chips = [];
  for (let i = 0; i < n; i++) { const r = document.createElementNS(NSV, 'rect'); r.setAttribute('class', 'chip run'); r.setAttribute('x', i * (cs + cg)); r.setAttribute('y', 0); r.setAttribute('width', cs); r.setAttribute('height', cs); r.setAttribute('rx', 5); gEl.appendChild(r); chips.push(r); }
  const t = document.createElementNS(NSV, 'text');
  t.setAttribute('x', w + 12); t.setAttribute('y', cs / 2 + 1); t.setAttribute('fill', '#9aa7c0'); t.setAttribute('font-size', 18); t.setAttribute('font-weight', 600); t.setAttribute('dominant-baseline', 'central'); t.setAttribute('style', `font-family:${MONO}`);
  t.textContent = `0 / ${n} quills`;
  gEl.appendChild(t);
  V.fans[id] = { g: gEl, chips, foot: t, n, cols: n, done: 0 };
  const rec = nodeRec('g:' + gname);
  rec.fanId = id;
  taskIds.forEach((tid, i) => rec.chipOf.set(tid, i));
  rec.groupName = gname;
  LIVE.total[gname] = n; LIVE.count[gname] = 0;
  renderCards(STATE.values, false);
}

async function playEnd(it) {
  const inner = it.ns.length > 0;
  const isGroupTask = !inner && G.groups.has(it.node);
  const nid = isGroupTask ? null : nodeIdFor(it.ns, it.node);
  const key = isGroupTask ? 'g:' + it.node : nid;
  const rec = nodeRec(key);
  const lf = loopFactor(Math.max(0, rec.runs - 1));
  // honest minimum on-screen "running" time (the server may finish a node in 2 ms; humans need a second)
  const el = performance.now() - rec.startedAt;
  const needed = (PACE.minRun * lf * (rec.fanId ? 0.25 : inner ? 0.5 : 1)) / SPEED;
  if (el < needed) await new Promise((r) => setTimeout(r, needed - el));
  rec.active = Math.max(0, rec.active - 1); rec.done += 1;
  const fanId = rec.fanId;
  if (fanId && V.fans[fanId]) {
    const idx = rec.chipOf.get(it.taskId);
    if (idx !== undefined) V.chipState(fanId, idx, 'done');
    const f = V.fans[fanId]; f.done++;
    const lbl = isGroupTask ? 'quills' : 'tasks';
    V.fanFoot(fanId, `${f.done} / ${f.n} ${lbl}`);
    const cnt = isGroupTask ? it.node : nid;
    LIVE.count[cnt] = f.done;
    renderCards(STATE.values, false);
    await sleep(rec.runs <= 1 ? PACE.chip : PACE.chipLater);
  }
  if (!(it.interrupts && it.interrupts.length)) {
    const mine = isGroupTask ? G.groups.get(it.node).members : [nid];
    STATE.lastDone = [...mine, ...STATE.lastDone.filter((x) => !mine.includes(x))].slice(0, 12);
  }
  if (rec.active > 0) return;
  // the whole group of tasks of this node has finished
  const interrupted = it.interrupts && it.interrupts.length;
  if (isGroupTask) V.setGroup(it.node, 'done');
  else V.setNode(nid, interrupted ? 'interrupted' : 'done');
  if (interrupted) { STATE.interrupted = nid; return; }
  if (rec.fanId && rec.total > 1) {
    Cap.fire(`fandone:${isGroupTask ? it.node : nid}`, { n: rec.total });
    const out = G.edges.find((e) => (isGroupTask ? G.groups.get(it.node).members.includes(e.src) && !G.groups.get(it.node).members.includes(e.dst) : e.src === nid) && !e.back);
    const red = SC.channels.find((c) => c.tag && (c.k === (isGroupTask ? 'drafts' : (nid === 'audition' ? 'auditions' : nid === 'react' ? 'reactions' : ''))));
    if (red && nid === 'react') { const rr = STATE.runs[nid]; V.fanFoot(nid, `reactions += ${rec.total}`, true); void rr; }
    else if (out && red && V.edges[out.id]) { V.edgeLabel(out.id, 'reducer ' + red.tag.replace('reducer: ', '= ')); setTimeout(() => V.edgeLabel(out.id, null), 2600 / SPEED); }
    await sleep(PACE.endDwell * 2);
  } else await sleep(PACE.endDwell * lf);
}

function dynamicChannels(values) {
  const keys = Object.keys(values).filter((k) => values[k] !== null && values[k] !== undefined && k !== 'messages').slice(0, 5);
  if (!keys.length) return;
  SC.channels = keys.map((k) => ({ k, w: 1, fmt: (v) => { const x = v[k]; if (x === undefined) return null; if (Array.isArray(x)) return `${x.length} items`; if (x && typeof x === 'object') return `${Object.keys(x).length} keys`; return String(x).slice(0, 30); } }));
  buildStrip();
}
function ckptStep(it) {
  STATE.ckpts += 1;
  if (SC.dynamic && !SC.channels.length) dynamicChannels(it.values || {});
  STATE.values = { ...STATE.values, ...it.values };
  TL.add('ckpt');
  TL.log(`checkpoint #${STATE.ckpts} · next: ${(it.next || []).filter((x, i, a) => a.indexOf(x) === i).join(', ') || 'end'}`);
  // pending-write counters give way to the checkpointed value
  renderCards(STATE.values);
}

async function playPause(it) {
  const p = it.interrupts[0].value || {};
  const nid = STATE.interrupted;
  setStatus('amber', 'WAITING FOR A HUMAN');
  V.tag(nid, 'wait', 'waiting for a human', { icon: 'pause' });
  TL.add('pause');
  TL.log(`interrupt() · ${p.kind || 'value'}`);
  if (p.mishap) { LIVE.pending = p.mishap.title; renderCards(STATE.values); }
  Cap.fire('interrupt', {}); Cap.fire(`interrupt:${nid}`, {});
  await sleepU(PACE.interruptWait * (STATE.runNo > 1 ? 0.62 : 1));
  const val = SC.answer(p.kind, p);
  const body = { command: { resume: Object.fromEntries(it.interrupts.map((i) => [i.id, val])) } };
  let txt = JSON.stringify(val);
  if (txt.length > 52) txt = txt.slice(0, 50) + '…';
  V.tag(nid, 'wait', null);
  V.tag(nid, 'resume', `Command(resume=${txt})`, { icon: 'play', fill: '#10233d', color: '#f4c95d', stroke: '#f4c95d', mono: true, size: 18 });
  setStatus('blue', 'RESUMING');
  TL.add('resume');
  TL.log(`Command(resume=…) → same thread_id`);
  STATE.runNo += 1;
  Cap.fire('resume', {});
  await sleepU(PACE.resumeShow * (STATE.runNo > 2 ? 0.7 : 1));
  LIVE.pending = null;
  setStatus('blue', 'RUNNING');
  return body;
}

async function player(q, out) {
  for (;;) {
    const it = await q.next();
    if (!it) break;
    if (it.type === 'start') await playStart(it);
    else if (it.type === 'end') await playEnd(it);
    else if (it.type === 'ckpt') { ckptStep(it); await sleep(PACE.ckpt); }
    else if (it.type === 'custom') {
      LIVE.custom[it.kind] = (LIVE.custom[it.kind] || 0) + 1;
      if (!['progress', 'note'].includes(it.kind) && !/^(audience|cast\.audition)/.test(it.kind)) TL.log(`custom › ${it.kind}`);
      if (/^memory\./.test(it.kind)) { renderCards(STATE.values, true); await sleep(80); }
    } else if (it.type === 'pause') { const body = await playPause(it); it.deferred.resolve(body); }
    else if (it.type === 'error') { setStatus('red', 'ERROR'); TL.log('error: ' + it.message); }
    else if (it.type === 'finish') {
      // final edge to END
      const last = STATE.lastDone.find((s) => G.edges.some((e) => e.src === s && e.dst === '__end__'));
      const e = G.edges.find((x) => x.src === last && x.dst === '__end__');
      if (e) { V.setEdge(e.id, 'active'); await V.travel(e.id, PACE.edge * 0.8); V.setEdge(e.id, 'done'); }
      V.setNode('__end__', 'done');
      break;
    }
  }
}

// ---------------------------------------------------------------------------------------------------- run
async function runOnce() {
  const token = ++runToken;
  Object.assign(STATE, { thread: null, ckpts: 0, values: {}, runs: {}, lastDone: ['__start__'], seenEdge: new Set(), interrupted: null, ended: false, runNo: 1 });
  Object.assign(LIVE, { count: {}, total: {}, base: {}, custom: {}, pending: null });
  Cap.fired = new Set(); Cap.queue = []; Cap.showing = null;
  window.__events.length = 0; window.__captions.length = 0; window.__done = false;
  V.reset(); TL.init(); buildStrip(); $('cap').querySelector('.ctext').innerHTML = ''; $('cap').classList.remove('in');
  setStatus('grey', 'STARTING');
  await sleep(PACE.intro);
  const thread = await jpost(`${API}/threads`, { metadata: { graph: GRAPH, source: 'viz' } });
  STATE.thread = thread.thread_id;
  $('tid').textContent = STATE.thread;
  setStatus('blue', 'RUNNING');
  TL.log(`POST /threads → thread created`);
  Cap.fire('start', {});
  mark('run-start', { thread: STATE.thread });
  const q = new Queue();
  const out = {};
  const consumer = player(q, out);
  let body = { input: SC.input() };
  try {
    for (let round = 0; round < 8; round++) {
      TL.log(round === 0 ? 'POST …/runs/stream (input)' : 'POST …/runs/stream (command: resume)');
      const interrupts = await readRun(STATE.thread, body, q, token);
      if (token !== runToken) return;
      const pending = [];
      const seen = new Set();
      for (const i of interrupts) if (!seen.has(i.id)) { seen.add(i.id); pending.push(i); }
      if (!pending.length) break;
      let resolve; const deferred = { promise: new Promise((r) => { resolve = r; }) }; deferred.resolve = resolve;
      q.push({ type: 'pause', interrupts: pending, deferred });
      body = await deferred.promise;
    }
    q.push({ type: 'finish' });
    q.close();
    await consumer;
    if (token !== runToken) return;
    setStatus('green', 'DONE');
    TL.log(`run finished · ${STATE.ckpts} checkpoints`);
    Cap.fire('end', {});
    await sleep(PACE.hold);
  } catch (e) {
    console.error(e);
    setStatus('red', 'ERROR');
    $('cap').querySelector('.ctext').textContent = String(e && e.message || e);
    $('cap').classList.add('in');
  }
  mark('run-end', { checkpoints: STATE.ckpts });
  window.__state = { thread: STATE.thread, checkpoints: STATE.ckpts, captions: window.__captions.length };
  window.__done = true;
}
window.__restart = () => { window.__done = false; return runOnce(); };

// ---------------------------------------------------------------------------------------------------- concept mode (scene H opening)
async function runConcept() {
  setStatus('grey', 'THE GRAPH');
  $('tid').textContent = 'not started';
  const strip = $('strip');
  // real channel names + types from the API's state schema
  let props = {};
  try { const sch = await loadSchemas(A.id); props = (sch.state_schema && sch.state_schema.properties) || {}; } catch (e) { /* offline */ }
  const typeOf = (p) => !p ? '' : p.type === 'array' ? 'list' + (p.items && p.items.type === 'object' ? '[dict]' : p.items && p.items.type === 'string' ? '[str]' : '') : p.type === 'object' ? 'dict' : p.type === 'string' ? 'str' : p.type === 'integer' ? 'int' : p.type === 'number' ? 'float' : p.type || '';
  strip.innerHTML = '';
  const chans = ['candidate_ids', 'auditions', 'scores', 'proposed', 'cast'];
  for (const k of chans) {
    const d = document.createElement('div');
    d.className = 'card' + (k === 'auditions' ? ' tagged' : ''); d.dataset.k = k; d.style.flex = `${k === 'auditions' ? 1.4 : 1.1} 1 0`;
    d.innerHTML = `<div class="ck">${k}</div><div class="cv dim">${typeOf(props[k]) || '—'}</div>${k === 'auditions' ? '<div class="ct">reducer: operator.add</div>' : ''}`;
    strip.appendChild(d);
  }
  TL.init(); TL.log('concept view · casting_call');
  await sleep(700);
  const over = $('over');
  const gb = $('g').getBoundingClientRect();
  const P = (id, side = 'c') => { const n = G.nodes.get(id); return { x: gb.left + n.cx, y: gb.top + n.cy + (side === 'b' ? n.h / 2 : side === 't' ? -n.h / 2 : 0) }; };
  const E = (id) => { const e = V.edges[id]; const p = e.path.getPointAtLength(e.len * 0.5); return { x: gb.left + p.x, y: gb.top + p.y }; };
  const card = (k) => document.querySelector(`.card[data-k="${k}"]`).getBoundingClientRect();
  const sb = $('strip').getBoundingClientRect();
  const callouts = [
    { text: 'node', sub: 'a plain function', target: P('audition', 'b'), at: { x: P('audition').x, y: 226 }, hi: () => { V.setNode('audition', 'running'); } },
    { text: 'edge', sub: 'what runs next', target: { x: E('audition>rank').x + 55, y: E('audition>rank').y + 4 }, at: { x: 950, y: 226 }, hi: () => { V.setEdge('audition>rank', 'active'); } },
    { text: 'conditional edge', sub: 'chosen at run time', target: { x: E('load>audition').x + 4, y: E('load>audition').y + 4 }, at: { x: 400, y: 226 }, hi: () => { V.setEdge('load>audition', 'active'); V.setEdge('confirm_cast>done', 'active'); } },
    { text: 'shared state', sub: 'typed channels every node reads', target: { x: card('candidate_ids').left + 80, y: sb.bottom + 4 }, at: { x: 250, y: 640 }, hi: () => { $('strip').classList.add('hi'); } },
    { text: 'reducer', sub: 'merges parallel updates', target: { x: card('auditions').left + 150, y: sb.bottom + 4 }, at: { x: 640, y: 640 }, hi: () => { document.querySelector('.card[data-k="auditions"]').classList.add('hi'); V.edgeLabel('audition>rank', 'reducer = operator.add'); } },
  ];
  const sv = document.createElementNS(NSV, 'svg');
  sv.setAttribute('width', 1280); sv.setAttribute('height', 720); sv.setAttribute('viewBox', '0 0 1280 720');
  over.appendChild(sv);
  for (let i = 0; i < callouts.length; i++) {
    const c = callouts[i];
    c.hi();
    const g = document.createElementNS(NSV, 'g'); g.setAttribute('class', 'co');
    const w1 = textW(c.text, 32, 800), w2 = textW(c.sub, 19, 600);
    const w = Math.max(w1, w2) + 40, h = 74;
    const x = c.at.x - w / 2, y = c.at.y - h / 2;
    // leader from the label's nearest edge to the target
    const sx = Math.max(x + 30, Math.min(x + w - 30, c.target.x)), sy = c.target.y > c.at.y ? y + h : y;
    const line = document.createElementNS(NSV, 'path');
    const mx = (sx + c.target.x) / 2;
    line.setAttribute('d', `M${sx} ${sy} C${sx} ${(sy + c.target.y) / 2} ${c.target.x} ${(sy + c.target.y) / 2} ${c.target.x} ${c.target.y}`);
    line.setAttribute('stroke', '#f4c95d'); line.setAttribute('stroke-width', 3.5); line.setAttribute('fill', 'none'); line.setAttribute('stroke-linecap', 'round');
    const dot = document.createElementNS(NSV, 'circle'); dot.setAttribute('cx', c.target.x); dot.setAttribute('cy', c.target.y); dot.setAttribute('r', 7); dot.setAttribute('fill', '#f4c95d');
    const box = document.createElementNS(NSV, 'rect'); box.setAttribute('x', x); box.setAttribute('y', y); box.setAttribute('width', w); box.setAttribute('height', h); box.setAttribute('rx', 14); box.setAttribute('fill', '#1b1606'); box.setAttribute('stroke', '#f4c95d'); box.setAttribute('stroke-width', 3);
    const t1 = document.createElementNS(NSV, 'text'); t1.setAttribute('x', c.at.x); t1.setAttribute('y', y + 29); t1.setAttribute('text-anchor', 'middle'); t1.setAttribute('fill', '#f4c95d'); t1.setAttribute('font-size', 32); t1.setAttribute('font-weight', 800); t1.setAttribute('dominant-baseline', 'central'); t1.textContent = c.text;
    const t2 = document.createElementNS(NSV, 'text'); t2.setAttribute('x', c.at.x); t2.setAttribute('y', y + 57); t2.setAttribute('text-anchor', 'middle'); t2.setAttribute('fill', '#e8dfc4'); t2.setAttribute('font-size', 19); t2.setAttribute('font-weight', 600); t2.setAttribute('dominant-baseline', 'central'); t2.textContent = c.sub;
    g.append(line, dot, box, t1, t2);
    sv.appendChild(g);
    mark('callout', { text: c.text, sub: c.sub });
    window.__captions.push({ t: +now().toFixed(2), at: 'concept', text: `${c.text}: ${c.sub}` });
    await sleep(1500);
  }
  await sleep(1300);
  window.__done = true;
}

// ---------------------------------------------------------------------------------------------------- boot
(async function boot() {
  buildChrome();
  try {
    const as = await loadAssistants();
    A = as[GRAPH];
    if (!A) throw new Error('no such graph: ' + GRAPH);
    G = await buildGraph(GRAPH, as, { W: 1232, H: 400, fan: Object.fromEntries(Object.entries(SC.fan || {}).map(([k, v]) => [k, { n: k === 'audition' ? 24 : k === 'react' ? 12 : v.n || 3, cols: v.cols || 3 }])) });
    V = createView($('g'), G, { groupSub: SC.groupSub });
    window.__G = G;
    buildStrip(); TL.init();
    if (CONCEPT) { document.body.classList.add('concept'); await runConcept(); return; }
    await runOnce();
  } catch (e) {
    console.error(e);
    $('cap').querySelector('.ctext').textContent = 'Cannot reach the LangGraph server at ' + API + ' (' + (e && e.message) + ')';
    $('cap').classList.add('in');
    window.__error = String(e);
    window.__done = true;
  }
})();
