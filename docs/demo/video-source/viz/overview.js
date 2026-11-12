// overview.js: all TEN real Foghorn graphs (structure/<id>.json from GET /assistants/{id}/graph?xray=true), revealed in product-story
// order: ingress -> incident response -> human gym -> learning -> operations; then a held grid with a soft highlight sweep.
// Pure function of time (see fhlib.js): ?seek=S draws one frame, ?fit=S warps the animation to S seconds, ?cap=work/rehearsal_cap switches data.
import { play, getJSON, h, svgEl, easeOut, easeOutBack, ph, clamp, lerp, q } from './fhlib.js';
import { layoutGraph, drawGraph } from './fg.js';

const CAP = q.get('cap') || 'captures/real'; // folder (relative to video-source/) holding structure/<graph_id>.json
const DURATION = 14;
const STAGES = [
  { id: 'ingress', label: 'Ingress', color: '#4fd1c5', t: 0.5 },
  { id: 'response', label: 'Incident response', color: '#8aa0ff', t: 2.2 },
  { id: 'gym', label: 'Human gym', color: '#f48fb1', t: 5.5 },
  { id: 'learning', label: 'Learning', color: '#cf8ae8', t: 7.3 },
  { id: 'ops', label: 'Operations', color: '#7bd88a', t: 9.1 },
];
const SWEEP0 = 11.2, SWEEP_D = 2.0, GRID_DONE = 11.0;
const X0 = 21, Y0 = 50, GW = 238, GH = 178, GAP = 12;
const cell = (c, r, cs = 1, rs = 1) => ({ x: X0 + c * (GW + GAP), y: Y0 + r * (GH + GAP), w: GW * cs + GAP * (cs - 1), h: GH * rs + GAP * (rs - 1) });
// Send targets (fan-out, drawn as a stack of cards) and nodes that call interrupt() (pause glyph): read from src/foghorn/flows/*
const COMMANDER_PIN = ['topology_analyst', 'change_analyst', 'metrics_analyst', 'clarify_change_intent', 'remediation_planner', 'comms_drafter', 'logs_analyst', 'knowledge_agent', 'rca_tournament'];
const CARDS = [
  { id: 'event_router', stage: 0, chip: 'Send fan-out, retries, cache', cell: cell(0, 0, 1, 2), t: 0.7, d: 1.3, lay: { orient: 'TB', nw: 38, nh: 17, stretchC: 2.4 }, fan: ['enrich'], hold: [] },
  { id: 'incident_commander', stage: 1, chip: 'supervisor + subgraphs + interrupt', cell: cell(1, 0, 2, 2), t: 2.4, d: 2.0, lay: { orient: 'TB', nw: 32, nh: 14, gw: 38, gh: 30, pin: COMMANDER_PIN }, fan: ['metrics_analyst', 'logs_analyst', 'change_analyst', 'topology_analyst', 'knowledge_agent'], hold: ['clarify_change_intent', 'approval_gate', 'comms_gate'] },
  { id: 'rca_tournament', stage: 1, chip: 'Send map-reduce', cell: cell(3, 0), t: 3.8, d: 1.0, lay: { orient: 'LR', nw: 24, nh: 14 }, fan: ['score_hypothesis'], hold: [] },
  { id: 'storm_correlator', stage: 1, chip: 'map-reduce + timeouts', cell: cell(3, 1), t: 4.6, d: 1.0, lay: { orient: 'LR', nw: 20, nh: 12 }, fan: ['analyze_candidate', 'merge_group', 'open_cluster_incident'], hold: [] },
  { id: 'change_gate', stage: 2, chip: 'human-in-the-loop gym', cell: cell(4, 0, 1, 2), t: 5.7, d: 1.5, lay: { orient: 'TB', nw: 32, nh: 15, stretchC: 2 }, fan: ['reviewer'], hold: ['clarify', 'break_glass', 'request_approval', 'edit_change', 'reviewer'] },
  { id: 'postmortem_pipeline', stage: 2, chip: 'Functional API', cell: cell(0, 2), t: 6.8, d: 0.8, lay: { orient: 'LR', nw: 52, nh: 22 }, fan: [], hold: ['postmortem_pipeline'] },
  { id: 'memory_keeper', stage: 3, chip: 'Store + semantic memory', cell: cell(1, 2), t: 7.5, d: 1.1, lay: { orient: 'LR', nw: 20, nh: 12 }, fan: [], hold: ['confirm_merge', 'curate'] },
  { id: 'eval_runner', stage: 3, chip: 'scores runs vs ground truth', cell: cell(2, 2), t: 8.4, d: 0.9, lay: { orient: 'LR', nw: 26, nh: 14 }, fan: ['run_scenario'], hold: [] },
  { id: 'ops_copilot', stage: 4, chip: 'create_agent + tools', cell: cell(3, 2), t: 9.3, d: 1.1, lay: { orient: 'LR', nw: 15, nh: 11 }, fan: [], hold: [] },
  { id: 'shift_handoff', stage: 4, chip: 'durable cron + crash recovery', cell: cell(4, 2), t: 10.2, d: 0.9, lay: { orient: 'LR', nw: 20, nh: 14 }, fan: [], hold: [] },
];
const hex = (s) => [1, 3, 5].map((i) => parseInt(s.slice(i, i + 2), 16));
const mix = (a, b, t) => { const A = hex(a), B = hex(b); return `rgb(${A.map((v, i) => Math.round(lerp(v, B[i], t))).join(',')})`; };

async function main() {
  const raws = await Promise.all(CARDS.map((c) => getJSON(`../${CAP}/structure/${c.id}.json`)));
  // header
  const st = document.getElementById('stages');
  const stEls = STAGES.map((s, i) => { const e = h('div', { class: 'st', style: `--sc:${s.color}` }, h('i'), `${i + 1} ${s.label}`); st.append(e); return e; });
  const lg = document.getElementById('legend');
  lg.innerHTML = `<span><svg width="26" height="18" viewBox="0 0 26 18"><rect x="7" y="1" width="16" height="9" rx="2.5" fill="#1a2030" stroke="#4b5878" opacity=".6"/><rect x="4" y="4" width="16" height="9" rx="2.5" fill="#1a2030" stroke="#4b5878" opacity=".85"/><rect x="1" y="7" width="16" height="9" rx="2.5" fill="#1a2030" stroke="#4b5878" stroke-width="1.5"/></svg>Send</span>
    <span><svg width="22" height="16" viewBox="0 0 22 16"><rect x="1" y="1" width="20" height="14" rx="3.5" fill="rgba(255,170,114,.14)" stroke="#ffaa72" stroke-width="1.8"/><rect x="7" y="4.5" width="2.2" height="7" rx=".8" fill="#ffaa72"/><rect x="12" y="4.5" width="2.2" height="7" rx=".8" fill="#ffaa72"/></svg>interrupt</span>`;
  const grid = document.getElementById('grid');
  const cards = [];
  CARDS.forEach((c, i) => {
    const S = STAGES[c.stage];
    const el = h('div', { class: 'card', style: `left:${c.cell.x}px;top:${c.cell.y}px;width:${c.cell.w}px;height:${c.cell.h}px;--sc:${S.color}` });
    const ttl = h('div', { class: 'ttl' }, c.id);
    const chip = h('div', { class: 'chip' }, c.chip);
    el.append(ttl, chip);
    grid.append(el);
    const chipBottom = chip.offsetTop + chip.offsetHeight;
    const gx = 10, gy = chipBottom + 6;
    const W = c.cell.w - 20, H = c.cell.h - gy - 8;
    const svg = svgEl('svg', {});
    svg.style.left = gx + 'px'; svg.style.top = gy + 'px';
    el.append(svg);
    const L = layoutGraph(raws[i].graph, { ...c.lay, W, H, fan: c.fan, hold: c.hold });
    const hd = drawGraph(svg, L, { accent: S.color });
    // tall graphs: nodes sorted by layer for the reveal
    cards.push({ c, S, el, ttl, chip, svg, L, hd, gx, gy });
  });
  window.__graphs = cards.map((k) => ({ id: k.c.id, ...k.L.stats }));

  const beats = [{ t: 0, name: 'title' }];
  STAGES.forEach((s) => beats.push({ t: s.t, name: 'stage_' + s.id, label: s.label }));
  CARDS.forEach((c) => beats.push({ t: c.t, name: 'card_' + c.id, label: c.chip }));
  beats.push({ t: GRID_DONE, name: 'grid_complete' }, { t: SWEEP0, name: 'sweep' });
  beats.sort((a, b) => a.t - b.t);

  const ghost = '#1f2638', rest = '#2f3a57';
  function render(t) {
    // stage chips
    let cur = -1;
    STAGES.forEach((s, i) => { if (t >= s.t) cur = i; });
    stEls.forEach((e, i) => { e.className = 'st' + (t >= STAGES[i].t ? ' on' : '') + (i === cur && t < GRID_DONE ? ' cur' : ''); });
    const sweepX = lerp(-160, 1440, easeOut(ph(t, SWEEP0, SWEEP_D) * 0 + ph(t, SWEEP0, SWEEP_D)));
    const sweeping = t >= SWEEP0 && t <= SWEEP0 + SWEEP_D + 0.4;
    for (const k of cards) {
      const { c, S, el, ttl, chip, hd, L } = k;
      const tau = t - c.t;
      if (tau < 0) { el.style.borderColor = ghost; el.style.background = 'rgba(19,24,35,0)'; ttl.style.opacity = 0; chip.style.opacity = 0; k.svg.style.opacity = 0; continue; }
      const p = easeOut(tau / 0.4);
      const hl = 1 - ph(tau, 0.4, 1.4);
      // the sweep passes over the card: border + title brighten
      let sw = 0;
      if (sweeping) { const cx = c.cell.x + c.cell.w / 2; sw = Math.exp(-Math.pow((cx - sweepX) / 190, 2)); }
      el.style.background = `rgba(19,24,35,${(0.94 * p).toFixed(3)})`;
      el.style.borderColor = mix(mix(ghost, rest, p), S.color, clamp(hl * 0.85 + sw * 0.55));
      el.style.boxShadow = sw > 0.02 ? `0 0 ${Math.round(16 * sw)}px rgba(${hex(S.color).join(',')},${(0.22 * sw).toFixed(3)})` : 'none';
      ttl.style.opacity = p; ttl.style.transform = `translateY(${((1 - p) * 8).toFixed(1)}px)`;
      chip.style.opacity = ph(tau, 0.14, 0.35);
      k.svg.style.opacity = 1;
      const span = Math.max(0.2, c.d - 0.55);
      const Lm = Math.max(1, L.L);
      for (const n of hd.nodes) {
        const tn = 0.18 + (n.info.layer / Lm) * span;
        const kk = ph(tau, tn, 0.3);
        const s = 0.5 + 0.5 * easeOutBack(kk);
        n.g.setAttribute('transform', `translate(${n.info.x.toFixed(1)},${n.info.y.toFixed(1)}) scale(${s.toFixed(3)})`);
        n.g.style.opacity = clamp(kk * 1.6);
        if (n.glow) {
          let gl = 0;
          if (sweeping) { const nx = c.cell.x + k.gx + n.info.x; gl = 0.6 * Math.exp(-Math.pow((nx - sweepX) / 120, 2)); }
          n.glow.setAttribute('opacity', gl.toFixed(3));
        }
      }
      for (const e of hd.edges) {
        const te = 0.18 + (e.info.layerA / Lm) * span + 0.14;
        e.g.style.opacity = ph(tau, te, 0.34);
      }
    }
  }
  play({ duration: DURATION, beats, render, extra: { page: 'overview', cards: CARDS.map((c) => c.id), data: CAP } });
}
main();
