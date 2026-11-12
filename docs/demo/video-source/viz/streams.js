// streams.js: "five ways LangGraph streams a run". REAL events of one incident_commander thread (viz/streams_data.json, made by
// make_streams_data.py from work/<cap>/events.jsonl) replayed in recorded order into five lanes (one per stream mode) while a slim
// timeline shows that they all arrive interleaved over ONE connection. Pure function of time: ?seek=S, ?fit=S, see fhlib.js.
import { play, getJSON, h, esc, clamp, lerp, easeOut, easeInOut, ph, q } from './fhlib.js';

const DURATION = 30, INTRO = 1.7, OUTRO = 2.6, PAUSE = 1.0, FIRST_HOLD = 0.5;
const RESUME_WEIGHT = 2.6; // the resumed run (checkpoints) gets a larger share of the time, so it starts earlier
const SPOT_DIM = 0.45, SPOT_FADE = 0.25; // ?spot=<lane>@<sec>,...: other lanes dim to 45 %, 0.25 s transitions (output seconds)
const MODES = [
  { id: 'updates', color: '#8aa0ff', cap: 'State delta each node returns. A UI turns it into a state diff.' },
  { id: 'messages', color: '#4fd1c5', cap: 'LLM tokens as they stream. A UI types them out in the war room.' },
  { id: 'custom', color: '#f6a560', cap: 'Typed app events from the graph. A UI fills the hypothesis board.' },
  { id: 'tasks', color: '#cf8ae8', cap: 'Every node start and finish. A UI draws the run timeline.' },
  { id: 'checkpoints', color: '#7bd88a', cap: 'A saved state each superstep. Studio time-travels through them.' },
];
const KIND = { handoff: '#f6a560', 'evidence.found': '#7ab8ff', 'hypothesis.update': '#cf8ae8', 'policy.check': '#6fd884', progress: '#a7b3c4', decision: '#f2c14e', 'plan.proposed': '#4fd1c5', artifact: '#f48fb1', 'memory.recalled': '#b9a7ff', note: '#a7b3c4' };
const SLOT = 25;
const rgb = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };

// ------------------------------------------------------------------------------------------------ JSON snippets (trimmed, readable)
// richest structure first; strings are only cut below ~18 chars after keys / depth have been given up
const COMBOS = [[7, 3, 4], [5, 3, 4], [4, 2, 4], [3, 2, 4], [3, 2, 3], [2, 2, 3], [2, 1, 3], [1, 1, 2]];
const SLEN = [72, 58, 46, 38, 30, 24, 19];
function toks(v, P, d) {
  const [S, A, K, D] = P;
  if (v === null || v === undefined) return [['nl', 'null']];
  if (typeof v === 'boolean') return [['nl', String(v)]];
  if (typeof v === 'number') return [['n', Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000)]];
  if (typeof v === 'string') {
    if (v.startsWith('…')) return [['p', v]];
    let s = v.replace(/\s+/g, ' ');
    if (s.length > S) s = s.slice(0, S - 1) + '…';
    return [['s', JSON.stringify(s)]];
  }
  const out = [];
  if (Array.isArray(v)) {
    if (d >= D) return [['p', v.length ? '[…]' : '[]']];
    out.push(['p', '[']);
    v.slice(0, A + 1).forEach((x, i) => {
      if (i) out.push(['p', ', ']);
      if (i >= A) { out.push(['p', '…']); return; }
      out.push(...toks(x, P, d + 1));
    });
    out.push(['p', ']']);
    return out;
  }
  const ks = Object.keys(v);
  if (d >= D) return [['p', ks.length ? '{…}' : '{}']];
  out.push(['p', '{']);
  let n = 0;
  for (const k of ks) {
    if (n) out.push(['p', ', ']);
    if (k === '…') { out.push(['p', '…']); break; }
    if (n >= K) { out.push(['p', '…']); break; }
    out.push(['k', JSON.stringify(k)], ['p', ': '], ...toks(v[k], P, d + 1));
    n++;
  }
  out.push(['p', '}']);
  return out;
}
const plen = (t) => t.reduce((s, x) => s + x[1].length, 0);
function snippet(v, limit) {
  let last = null;
  for (let ci = 0; ci < COMBOS.length; ci++) {
    const [K, A, D] = COMBOS[ci];
    const lens = ci === COMBOS.length - 1 ? [...SLEN, 14, 11, 8] : SLEN;
    for (const S of lens) {
      const t = toks(v, [S, A, K, D], 0);
      if (plen(t) <= limit) return t;
      last = t;
    }
  }
  // hard cut
  let acc = 0; const out = [];
  for (const [c, x] of last) { if (acc + x.length > limit - 1) { out.push([c, x.slice(0, Math.max(0, limit - 1 - acc))], ['p', '\u2026']); break; } out.push([c, x]); acc += x.length; }
  return out;
}
const tokHTML = (t) => t.map(([c, x]) => `<span class="${c}">${esc(x)}</span>`).join('');
const sh = (s, n = 13) => (s && s.length > n ? s.slice(0, n) + '…' : s || '');

// ------------------------------------------------------------------------------------------------ one event -> one lane line
const CH = 9.03; // px per mono char at 15px
const FEED_W = 1280 - 40 - 296 - 14;
const pillW = (s) => s.length * 8.2 + 17;
function lineFor(e) {
  const v = e.v;
  let ns = e.ns ? e.ns + ' ›' : '';
  let pill = '', pc = '#fff', body;
  if (e.mode === 'tasks') {
    pill = v.phase; pc = v.phase === 'start' ? '#cf8ae8' : '#a7b3c4';
    const obj = v.phase === 'start' ? { id: v.id, name: v.name, triggers: v.triggers, input: v.input } : { id: v.id, name: v.name, ...(v.error ? { error: v.error } : {}), ...(v.result && Object.keys(v.result).length ? { result: v.result } : {}), ...(v.interrupts ? { interrupts: ['…'] } : {}) };
    body = (lim) => snippet(obj, lim);
  } else if (e.mode === 'updates') {
    const intr = v && v.__interrupt__;
    pill = intr ? 'interrupt' : ''; pc = intr ? '#ffaa72' : '#8aa0ff';
    body = (lim) => snippet(v, lim);
  } else if (e.mode === 'custom') {
    pill = v.kind; pc = KIND[v.kind] || '#b0b8cb'; ns = v.agent ? v.agent + ' ›' : '';
    body = (lim) => snippet(v.data, lim);
  } else if (e.mode === 'checkpoints') {
    pill = 'checkpoint'; pc = '#7bd88a';
    body = (lim) => snippet({ checkpoint_id: sh(v.id, 13), step: v.step, next: v.next, parent: sh(v.parent, 13) }, lim);
  }
  const used = (pill ? pillW(pill) + 9 : 0) + (ns ? ns.length * CH + 9 : 0);
  const lim = Math.floor((FEED_W - used - 6) / CH);
  return `${pill ? `<span class="pl" style="--pc:${pc}">${esc(pill)}</span>` : ''}${ns ? `<span class="ns">${esc(ns)}</span>` : ''}<span class="tx">${tokHTML(body(lim))}</span>`;
}
function groupLine(g, nVis, hot) {
  const first = g.evs[0].v, ns = g.ns ? g.ns + ' ›' : '';
  const isTool = first.type === 'tool', isCall = !isTool && first.tool_calls && first.tool_calls.length;
  let pill, body;
  if (isTool) { pill = 'tool result'; const c = first.content || ''; body = `<span class="k">${esc(first.tool || 'tool')}</span><span class="p"> → </span><span class="s">${esc(c.replace(/\s+/g, ' '))}</span>`; }
  else if (isCall) { pill = 'tool call'; body = tokHTML(snippet({ tool_calls: first.tool_calls.map((c) => ({ name: c.name, args: c.args })) }, 84)); }
  else {
    pill = g.ns || first.node || 'model';
    const vis = g.evs.slice(0, nVis);
    let full = vis.map((x) => x.v.content).join('');
    const lastChunk = vis[vis.length - 1].v.content;
    const used = pillW(pill) + 9 + (ns ? ns.length * CH + 9 : 0) + (g.evs[g.evs.length - 1].v.skipped ? 84 : 0);
    const room = Math.floor((FEED_W - used - 24) / CH);
    let cut = !!(g.evs[0].v.before);
    if (full.length > room) { full = full.slice(full.length - room); const sp = full.indexOf(' '); if (sp > 0 && sp < 14) full = full.slice(sp + 1); cut = true; }
    let head = full, tail = '';
    if (hot && lastChunk && full.endsWith(lastChunk)) { head = full.slice(0, full.length - lastChunk.length); tail = lastChunk; }
    const done = nVis === g.evs.length;
    const sk = done && g.evs[g.evs.length - 1].v.skipped ? `<span class="dim"> +${g.evs[g.evs.length - 1].v.skipped} chunks</span>` : '';
    body = `<span class="p">${cut ? '…' : ''}</span><span class="s">"${esc(head)}</span><span class="hot s">${esc(tail)}</span><span class="s">${done && !sk ? '"' : ''}</span>${sk}`;
  }
  const used = pillW(pill) + 9 + (ns ? ns.length * CH + 9 : 0);
  void used;
  return `<span class="pl" style="--pc:#4fd1c5">${esc(pill)}</span>${ns && !(!isTool && !isCall) ? `<span class="ns">${esc(ns)}</span>` : (ns && isTool ? `<span class="ns">${esc(ns)}</span>` : '')}<span class="tx">${body}</span>`;
}

async function main() {
  const D = await getJSON(q.get('data') || 'streams_data.json'); // ?data=streams_data_rehearsal.json = the offline development run
  const EV = D.events, N = EV.length;
  // ---- first appearance of each mode, scene starts, pacing weights
  const firstOf = {};
  EV.forEach((e, i) => { if (firstOf[e.mode] === undefined) firstOf[e.mode] = i; });
  const sceneStart = D.scenes.map((s) => s.from);
  const resumeScene = D.scenes.findIndex((s) => s.name === 'resume');
  // ?spot=updates@8.5,messages@11.2,...  (output seconds, after ?fit): spotlight one lane until the next entry
  const fitQ = parseFloat(q.get('fit'));
  const warpQ = fitQ > 0 ? fitQ / DURATION : 1;
  const SPOT = (q.get('spot') || '').split(',').map((x) => { const m = /^\s*([a-z]+)\s*@\s*([0-9.]+)\s*$/.exec(x); return m && MODES.some((l) => l.id === m[1]) ? { lane: m[1], t: parseFloat(m[2]) } : null; }).filter(Boolean).sort((a, b) => a.t - b.t);
  const spotT = SPOT.map((x) => x.t / warpQ); // default seconds
  let earlyEnd = -1; // index of the 2nd checkpoint of the resumed run
  { let n = 0; EV.forEach((e, i) => { if (resumeScene >= 0 && i >= sceneStart[resumeScene] && e.mode === 'checkpoints' && ++n === 2 && earlyEnd < 0) earlyEnd = i; }); }
  const weight = (e, i) => {
    const v = e.v, prev = EV[i - 1];
    if (e.mode === 'messages') return v.type === 'AIMessageChunk' && !(v.tool_calls && v.tool_calls.length) ? 0.075 : 0.3;
    let w = { tasks: 0.2, updates: 0.28, custom: 0.3, checkpoints: 0.62 }[e.mode];
    if (prev && Math.abs(e.t - prev.t) < 0.004 && prev.mode === e.mode) w *= 0.55;
    if (e.mode === 'custom') { if (v.kind === 'handoff') w = 1.0; else if (v.kind === 'decision') w = 0.5; else if (v.kind === 'hypothesis.update') w = 0.22; }
    if (e.mode === 'updates' && v && v.__interrupt__) w = 1.9;
    if (sceneStart.includes(i)) w += 0.25;
    if (resumeScene >= 0 && i >= sceneStart[resumeScene]) w *= i <= earlyEnd ? 0.7 : RESUME_WEIGHT; // first checkpoints arrive quickly, the rest of the resumed run takes the time
    return w;
  };
  const W = EV.map(weight);
  const holds = EV.map((e, i) => (firstOf[e.mode] === i ? FIRST_HOLD : 0));
  const budget = DURATION - INTRO - OUTRO - PAUSE;
  const f = (budget - holds.reduce((a, b) => a + b, 0)) / W.reduce((a, b) => a + b, 0);
  const T0 = [], DW = [];
  let t = INTRO, pauseStart = null, pauseEnd = null;
  EV.forEach((e, i) => {
    if (resumeScene >= 0 && i === sceneStart[resumeScene]) { pauseStart = t; t += PAUSE; pauseEnd = t; }
    T0[i] = t;
    DW[i] = W[i] * f + holds[i];
    t += DW[i];
  });
  const END = t;
  window.__pacing = { f: +f.toFixed(3), budget, events: N, end: +END.toFixed(2), t0: T0.map((x) => +x.toFixed(2)) };
  const gapSecs = resumeScene > 0 ? +(EV[sceneStart[resumeScene]].t - EV[sceneStart[resumeScene] - 1].t).toFixed(1) : 0;

  // ---- DOM: lanes
  const lanesEl = document.getElementById('lanes');
  const LANE_TOP = 148, LANE_H = 82, LANE_GAP = 5;
  const lanes = MODES.map((m, k) => {
    const el = h('div', { class: 'lane', style: `top:${LANE_TOP + k * (LANE_H + LANE_GAP)}px;--lc:${m.color}` });
    const bar = h('div', { class: 'bar' });
    const nm = h('div', { class: 'nm' }, m.id);
    const cap = h('div', { class: 'cap' }, m.cap);
    const lab = h('div', { class: 'lab' }, nm, cap);
    const feed = h('div', { class: 'feed' });
    const lns = [0, 1, 2, 3].map(() => { const l = h('div', { class: 'ln' }); feed.append(l); return l; });
    const wait = h('div', { class: 'ln wait' }, 'waiting for the first event…');
    wait.style.transform = `translateY(${2 * SLOT}px)`;
    feed.append(wait);
    el.append(bar, lab, feed);
    lanesEl.append(el);
    return { ...m, el, bar, nm, cap, lns, wait, idxs: EV.map((e, i) => (e.mode === m.id ? i : -1)).filter((i) => i >= 0) };
  });
  const laneOf = Object.fromEntries(lanes.map((l) => [l.id, l]));
  // messages: group chunks by (ns, message id)
  const groups = new Map();
  EV.forEach((e, i) => { if (e.mode !== 'messages') return; const key = e.ns + '|' + e.v.id; if (!groups.has(key)) groups.set(key, { key, ns: e.ns, evs: [], idx: [] }); const g = groups.get(key); g.evs.push(e); g.idx.push(i); });
  const glist = [...groups.values()].sort((a, b) => a.idx[0] - b.idx[0]);
  const lineCache = new Map();

  // ---- strip
  const tick = document.getElementById('sTicks');
  const sceneGaps = D.scenes.map((s, k) => (k === 0 ? 0 : k === resumeScene ? 40 : 12));
  const AREA = 1280 - 40 - 28;
  const per = (AREA - sceneGaps.reduce((a, b) => a + b, 0)) / N;
  const X = [];
  { let x = 0; D.scenes.forEach((s, k) => { x += sceneGaps[k]; for (let i = s.from; i <= s.to; i++) { X[i] = x; x += per; } }); }
  const ticks = EV.map((e, i) => { const d = h('div', { class: 'tk', style: `left:${X[i].toFixed(1)}px;width:${Math.max(2, per - 2.6).toFixed(1)}px` }); tick.insertBefore(d, document.getElementById('sHeadMark')); return d; });
  const mark = document.getElementById('sHeadMark');
  const isInt = EV.map((e) => e.mode === 'updates' && e.v && e.v.__interrupt__ !== undefined);
  const sceneLabels = D.scenes.map((s, k) => {
    const L = ['A', 'B', 'C', 'D'][k] || String(k + 1);
    const nsp = (/to (\d+) specialists/.exec(s.caption) || [])[1] || 2;
    const V = {
      handoff: [`supervisor + ${nsp} specialists`, `supervisor + ${nsp}`, ''],
      rca: ['rca fan-out (Send)', 'rca fan-out', 'rca', ''],
      interrupt: ['fan-out + interrupt', 'interrupt', ''],
      resume: [`resumed${gapSecs ? ` ${gapSecs} s later` : ''}`, 'resumed', ''],
    }[s.name] || [s.name, ''];
    const nextX = k + 1 < D.scenes.length ? X[D.scenes[k + 1].from] - 10 : AREA;
    const room = nextX - X[s.from];
    const txt = V.find((v) => 12 + v.length * 8.3 <= room) ?? '';
    const d = h('div', { class: 'sl' + (s.name === 'resume' ? ' pause' : ''), style: `left:${X[s.from].toFixed(1)}px` }, h('b', {}, L), txt);
    document.getElementById('sScenes').append(d);
    return d;
  });
  const sScene = document.getElementById('sScene'), sNow = document.getElementById('sNow'), sCnt = document.getElementById('sCnt');
  const hsrc = document.getElementById('hsrc');
  hsrc.innerHTML = `${N} real events sampled from one incident_commander thread, in recorded order`;
  const strip = document.getElementById('strip');
  const hdr = document.getElementById('hdr');

  // ---- beats
  const beats = [{ t: 0, name: 'title' }, { t: INTRO - 0.2, name: 'one_connection', label: 'five lanes, one connection' }];
  MODES.forEach((m) => { const i = firstOf[m.id]; if (i !== undefined) beats.push({ t: T0[i], name: 'mode_' + m.id, label: m.id }); });
  D.scenes.forEach((s) => beats.push({ t: T0[s.from] - (s.name === 'resume' ? PAUSE : 0), name: 'scene_' + s.name, label: s.caption }));
  if (pauseStart !== null) beats.push({ t: pauseStart, name: 'paused_for_human' }, { t: pauseEnd, name: 'resumed' });
  beats.push({ t: END, name: 'outro' });
  beats.sort((a, b) => a.t - b.t);

  const nowHTML = (e) => {
    const m = laneOf[e.mode], v = e.v;
    let txt = '';
    if (e.mode === 'tasks') txt = `${v.phase} ${e.ns ? e.ns + ' › ' : ''}${v.name}`;
    else if (e.mode === 'updates') txt = (v && v.__interrupt__) ? '__interrupt__' : (e.ns ? e.ns + ' › ' : '') + Object.keys(v)[0];
    else if (e.mode === 'custom') txt = v.kind;
    else if (e.mode === 'messages') txt = (e.ns || v.node) + (v.type === 'tool' ? ' tool result' : v.tool_calls && v.tool_calls.length ? ' tool call' : ' token chunk');
    else txt = `step ${v.step}`;
    return `<b style="background:${rgb(m.color, 0.2)};color:${m.color}">${m.id}</b>${esc(txt)}`;
  };
  const st = { cur: -2, scene: -2, pause: null };
  const sceneOf = (i) => EV[i].scene;

  function setLine(ln, key, html) { if (ln._k !== key) { ln._k = key; ln.innerHTML = html; } }
  const OPS = [0, 1, 0.8, 0.58, 0];
  function slide(lns, count, newestT, t) {
    const p = easeOut((t - newestT) / 0.22);
    for (let j = 0; j < 4; j++) {
      const ln = lns[j];
      if (j >= count) { ln.style.opacity = 0; continue; }
      ln.style.transform = `translateY(${((2 - j) + (1 - p)) * SLOT}px)`;
      ln.style.opacity = lerp(OPS[j], OPS[j + 1], p).toFixed(3);
    }
  }

  function render(tt) {
    const t = tt;
    // intro: header, strip and lanes fade in
    const a0 = ph(t, 0.0, 0.5);
    hdr.style.opacity = a0; strip.style.opacity = ph(t, 0.25, 0.5);
    let cur = -1;
    for (let i = 0; i < N; i++) { if (T0[i] <= t) cur = i; else break; }
    const inPause = pauseStart !== null && t >= pauseStart && t < pauseEnd;
    // ---- strip
    if (st.cur !== cur) {
      st.cur = cur;
      for (let i = 0; i < N; i++) {
        const tk = ticks[i];
        if (i <= cur) { tk.style.background = isInt[i] ? '#ffaa72' : laneOf[EV[i].mode].color; tk.style.opacity = i === cur ? 1 : 0.9; tk.className = 'tk' + (i === cur ? ' cur' : ''); tk.style.setProperty('--tc', laneOf[EV[i].mode].color); }
        else { tk.style.background = ''; tk.style.opacity = 1; tk.className = 'tk'; }
      }
    }
    if (cur >= 0) {
      const prevX = cur > 0 ? X[cur - 1] + per / 2 : X[cur];
      const x = lerp(prevX, X[cur] + per / 2, easeOut((t - T0[cur]) / 0.14));
      mark.style.left = (x - 0).toFixed(1) + 'px'; mark.style.opacity = 1;
    } else mark.style.opacity = 0;
    const sc = inPause ? -1 : t >= END + 0.3 ? -3 : cur >= 0 ? sceneOf(cur) : -2;
    if (st.scene !== sc || st.pause !== inPause) {
      st.scene = sc; st.pause = inPause;
      if (sc === -3) sScene.textContent = 'five modes interleaved on one connection: a client picks the ones it needs';
      if (inPause) sScene.textContent = `paused: the graph is waiting for a human (interrupt)${gapSecs ? `, ${gapSecs} s in the recording` : ''}`;
      else if (sc === -3) { /* set above */ }
      else if (sc >= 0) sScene.textContent = D.scenes[sc].caption;
      else sScene.textContent = `stream_mode = [ updates, messages, custom, tasks, checkpoints ]`;
      sScene.style.color = inPause ? '#ffaa72' : '';
    }
    if (inPause) sNow.innerHTML = `<b style="background:${rgb('#ffaa72', 0.2)};color:#ffaa72">interrupt</b>run 1 ends here`;
    else if (cur >= 0) {
      if (st.nowKey !== cur) { st.nowKey = cur; sNow.innerHTML = nowHTML(EV[cur]); }
    } else sNow.textContent = '';
    if (inPause) st.nowKey = -5;
    sCnt.textContent = cur >= 0 ? `${cur + 1} / ${N}` : `0 / ${N}`;
    sceneLabels.forEach((d, k) => { d.style.opacity = (cur >= 0 && sceneOf(cur) >= k) || t >= END ? 1 : 0.28; });
    // ---- lanes
    // spotlight weights per lane (smooth 0.25 s hand-over between consecutive spot entries)
    const spW = {};
    let spotOn = false;
    if (SPOT.length) {
      MODES.forEach((m) => { spW[m.id] = 0; });
      let k = -1;
      for (let i = 0; i < spotT.length; i++) if (spotT[i] <= t) k = i;
      if (k >= 0) {
        spotOn = true;
        const p = easeInOut((t - spotT[k]) / (SPOT_FADE / warpQ));
        spW[SPOT[k].lane] += p;
        if (k > 0) spW[SPOT[k - 1].lane] += 1 - p;
        else MODES.forEach((m) => { if (m.id !== SPOT[0].lane) spW[m.id] = 0; });
      }
    }
    const firstSpot = SPOT.length ? null : MODES.find((m) => firstOf[m.id] !== undefined && t >= T0[firstOf[m.id]] && t < T0[firstOf[m.id]] + FIRST_HOLD + 0.25);
    const outro = ph(t, END + 0.2, 0.5);
    lanes.forEach((L, li) => {
      const la = ph(t, 0.35 + li * 0.12, 0.4);
      const idxs = L.idxs;
      let k = 0; while (k < idxs.length && T0[idxs[k]] <= t) k++;
      const lastT = k > 0 ? T0[idxs[k - 1]] : -9;
      const flash = clamp(1 - (t - lastT) / 0.6);
      // outro: a soft pulse runs down the five lanes
      const pulse = outro > 0 && outro < 1 ? Math.exp(-Math.pow((outro * 6 - li - 0.5) / 0.9, 2)) * 0.8 : 0;
      const sp = spotOn ? clamp(spW[L.id]) : 0;
      const dim = spotOn ? lerp(SPOT_DIM, 1, sp) : firstSpot && firstSpot.id !== L.id ? 0.55 : 1;
      const hl = clamp(Math.max(flash + pulse, sp));
      L.el.style.opacity = (la * dim).toFixed(3);
      L.el.style.borderColor = hl > 0.03 ? rgb(L.color, 0.28 + 0.62 * hl) : '';
      L.el.style.background = hl > 0.02 ? `linear-gradient(90deg, ${rgb(L.color, 0.07 * clamp(flash + pulse * 0.8) + 0.11 * sp)}, rgba(19,24,35,1) 72%)` : '';
      L.el.style.boxShadow = sp > 0.02 ? `0 0 ${(18 * sp).toFixed(1)}px ${(1 + 2 * sp).toFixed(1)}px ${rgb(L.color, 0.34 * sp)}` : '';
      L.el.style.zIndex = sp > 0.5 ? 3 : 1;
      L.cap.style.opacity = Math.max(k > 0 ? ph(t, T0[idxs[0]], 0.5) : 0, sp);
      L.nm.style.opacity = Math.max(k > 0 ? 1 : 0.55, sp);
      L.wait.style.opacity = k > 0 ? 0 : 1;
      if (L.id === 'messages') {
        const vis = glist.filter((g) => T0[g.idx[0]] <= t);
        const showN = Math.min(4, vis.length);
        const newest = vis[vis.length - 1];
        const latestEv = k > 0 ? idxs[k - 1] : -1;
        for (let j = 0; j < 4; j++) {
          const g = vis[vis.length - 1 - j];
          if (!g) continue;
          let nVis = 0; while (nVis < g.idx.length && T0[g.idx[nVis]] <= t) nVis++;
          const isLatest = g.idx[nVis - 1] === latestEv && t - T0[latestEv] < 0.45;
          setLine(L.lns[j], g.key + ':' + nVis + ':' + (isLatest ? 1 : 0), groupLine(g, nVis, isLatest));
        }
        if (newest) slide(L.lns, showN, T0[newest.idx[0]], t);
        else slide(L.lns, 0, 0, t);
      } else {
        for (let j = 0; j < 4; j++) {
          const ei = idxs[k - 1 - j];
          if (k - 1 - j < 0) continue;
          let html = lineCache.get(ei);
          if (html === undefined) { html = lineFor(EV[ei]); lineCache.set(ei, html); }
          setLine(L.lns[j], ei, html);
        }
        slide(L.lns, Math.min(4, k), k > 0 ? T0[idxs[k - 1]] : 0, t);
      }
    });
  }
  play({ duration: DURATION, beats, render, extra: { page: 'streams', spot: SPOT, events: N, scenes: D.scenes.map((s) => ({ name: s.name, t: +T0[s.from].toFixed(2) })), source: D.source, thread_id: D.thread_id, run_ids: D.run_ids } });
}
main();
