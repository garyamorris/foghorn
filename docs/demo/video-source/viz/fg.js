// fg.js: Foghorn graph thumbnails. Real structure (GET /assistants/{id}/graph?xray=true, saved as structure/<id>.json) ->
// compact layered (Sugiyama-style) layout -> SVG. No labels (thumbnails): the shapes are the real nodes/edges.
//  * `parent:child` ids (xray subgraphs) are collapsed into ONE group box that shows its inner nodes as a small stack
//  * `__error_handler__x` nodes are drawn as a small red satellite next to x
//  * back edges (loops) are routed upwards and drawn dashed + dim; A->B plus B->A is merged into one double-headed edge
//  * `fan` nodes (Send targets) are drawn as a stack of cards, `hold` nodes (interrupt()) get a pause glyph
import { svgEl } from './fhlib.js';

const GID = /^([^:]+):(.+)$/;
const groupOf = (id) => { const m = GID.exec(id); return m ? m[1] : null; };

// ------------------------------------------------------------------------------------------------ model
export function buildModel(raw, o) {
  const groups = new Map();
  for (const n of raw.nodes) {
    const g = groupOf(n.id);
    if (!g) continue;
    const local = n.id.slice(g.length + 1);
    const gr = groups.get(g) || { id: g, members: [] };
    groups.set(g, gr);
    if (local !== '__start__' && local !== '__end__') gr.members.push(local);
  }
  const nodes = new Map();
  const sats = new Map(); // node id -> error-handler satellite
  for (const n of raw.nodes) {
    if (groupOf(n.id)) continue;
    if (n.id.startsWith('__error_handler__')) { sats.set(n.id.slice('__error_handler__'.length), true); continue; }
    const kind = n.id === '__start__' ? 'start' : n.id === '__end__' ? 'end' : 'node';
    nodes.set(n.id, { id: n.id, kind, fan: (o.fan || []).includes(n.id), hold: (o.hold || []).includes(n.id), sat: false });
  }
  for (const gr of groups.values()) {
    // inner order: by depth along the inner edges (before_model, model, tools, after_agent ...)
    const ie = raw.edges.filter((e) => groupOf(e.source) === gr.id && groupOf(e.target) === gr.id);
    const depth = new Map(gr.members.map((m, i) => [m, 0]));
    for (let k = 0; k < 4; k++) for (const e of ie) {
      const a = e.source.slice(gr.id.length + 1), b = e.target.slice(gr.id.length + 1);
      if (a === '__start__') { depth.set(b, Math.max(depth.get(b) || 0, 0)); continue; }
      if (b === '__end__' || !depth.has(a) || !depth.has(b)) continue;
      if ((depth.get(b) || 0) < (depth.get(a) || 0) + 1 && !(b === gr.members[0])) {
        // ignore loop-backs (b already placed before a)
        if (!(depth.get(b) > 0 && depth.get(b) < depth.get(a))) depth.set(b, Math.min(depth.get(a) + 1, 6));
      }
    }
    const inner = gr.members.slice().sort((a, b) => depth.get(a) - depth.get(b));
    nodes.set(gr.id, { id: gr.id, kind: 'group', inner, fan: (o.fan || []).includes(gr.id), hold: (o.hold || []).includes(gr.id), sat: false });
  }
  for (const id of sats.keys()) if (nodes.has(id)) nodes.get(id).sat = true;
  const map = (id) => groupOf(id) || id;
  const emap = new Map();
  for (const e of raw.edges) {
    const gs = groupOf(e.source), gt = groupOf(e.target);
    if (gs && gs === gt) continue; // internal to a collapsed subgraph
    const s = map(e.source), t = map(e.target);
    if (!nodes.has(s) || !nodes.has(t)) continue;
    const k = s + '\u0000' + t;
    const prev = emap.get(k);
    emap.set(k, { s, t, cond: prev ? prev.cond && !!e.conditional : !!e.conditional, self: s === t });
  }
  return { nodes, edges: [...emap.values()] };
}

// ------------------------------------------------------------------------------------------------ layout
function placeLayer(desired, sep, weight) {
  // minimise sum w*(x-d)^2 subject to x[i]-x[i-1] >= sep[i]: pool-adjacent-violators on the shifted values
  const n = desired.length;
  const off = [0];
  for (let i = 1; i < n; i++) off[i] = off[i - 1] + sep[i];
  const blocks = [];
  for (let i = 0; i < n; i++) {
    blocks.push({ sw: weight[i], sy: weight[i] * (desired[i] - off[i]), cnt: 1 });
    while (blocks.length > 1) {
      const b = blocks[blocks.length - 1], a = blocks[blocks.length - 2];
      if (a.sy / a.sw > b.sy / b.sw) { a.sw += b.sw; a.sy += b.sy; a.cnt += b.cnt; blocks.pop(); } else break;
    }
  }
  const out = [];
  for (const b of blocks) for (let k = 0; k < b.cnt; k++) out.push(b.sy / b.sw);
  return out.map((v, i) => v + off[i]);
}

export function layoutGraph(raw, opt = {}) {
  const O = { orient: 'TB', nw: 26, nh: 13, gw: 38, gh: 30, dot: 9, gapC: 8, gapD: 12, gapDmin: 6, gapDmax: 36, W: 200, H: 200, stretchC: 1.6, order: null, forceBack: [], ...opt };
  const M = buildModel(raw, O);
  const tb = O.orient === 'TB';
  // sizes in abstract coords: c = cross axis, d = depth axis
  const size = (n) => {
    let w, h;
    if (n.kind === 'start' || n.kind === 'end') w = h = O.dot;
    else if (n.kind === 'group') { w = O.gw; h = O.gh; if (!tb) { w = O.gwLR || O.gw; h = O.ghLR || O.gh; } }
    else { w = O.nw; h = O.nh; }
    return tb ? { c: w, d: h, w, h } : { c: h, d: w, w, h };
  };
  const V = new Map(); // layout nodes (real + dummy)
  for (const n of M.nodes.values()) V.set(n.id, { ...n, ...size(n), real: true });

  // ---- adjacency + DFS for back edges
  const out = new Map([...V.keys()].map((k) => [k, []]));
  const edges = M.edges.filter((e) => !e.self).map((e, i) => ({ ...e, i }));
  for (const e of edges) out.get(e.s).push(e);
  const roots = ['__start__', ...[...V.keys()].filter((k) => k !== '__start__')].filter((k) => V.has(k));
  const state = new Map(), disc = new Map();
  let dn = 0;
  const forceBack = new Set((O.forceBack || []).map(([a, b]) => a + '>' + b));
  const dfs = (u) => {
    state.set(u, 1); disc.set(u, dn++);
    for (const e of out.get(u)) {
      if (forceBack.has(e.s + '>' + e.t)) { e.back = true; continue; }
      const st = state.get(e.t);
      if (st === 1) e.back = true;
      else if (!st) dfs(e.t);
    }
    state.set(u, 2);
  };
  for (const r of roots) if (!state.get(r)) dfs(r);
  // merge A->B + B->A (one of them is a back edge): double-headed edge
  const byKey = new Map(edges.map((e) => [e.s + '>' + e.t, e]));
  for (const e of edges) {
    if (!e.back) continue;
    const fwd = byKey.get(e.t + '>' + e.s);
    if (fwd && !fwd.back) { fwd.bidir = true; e.drop = true; }
  }
  const E = edges.filter((e) => !e.drop);
  // ---- layering: longest path over the DAG (back edges reversed)
  const preds = new Map([...V.keys()].map((k) => [k, []]));
  for (const e of E) { const [a, b] = e.back ? [e.t, e.s] : [e.s, e.t]; e.a = a; e.b = b; preds.get(b).push(a); }
  const layer = new Map();
  const lay = (u) => {
    if (layer.has(u)) return layer.get(u);
    layer.set(u, 0);
    let l = 0;
    for (const p of preds.get(u)) l = Math.max(l, lay(p) + 1);
    layer.set(u, l);
    return l;
  };
  for (const k of V.keys()) lay(k);
  // __end__ goes last; pull the terminal node(s) down to the final layer
  let L = Math.max(...layer.values());
  if (V.has('__end__')) { layer.set('__end__', L); L = Math.max(L, layer.get('__end__')); }
  // pull nodes that have successors later toward them (keeps edges short): only nodes with a single predecessor chain
  const succsOf = new Map([...V.keys()].map((k) => [k, []]));
  for (const e of E) succsOf.get(e.a).push(e.b);
  for (let pass = 0; pass < 3; pass++) for (const k of V.keys()) {
    if (k === '__start__' || k === '__end__') continue;
    const ss = succsOf.get(k);
    if (!ss.length || preds.get(k).length) continue; // only pure sources (besides start)
    layer.set(k, Math.min(...ss.map((s) => layer.get(s))) - 1);
  }
  const lmin = Math.min(...layer.values());
  if (lmin < 0) for (const k of layer.keys()) layer.set(k, layer.get(k) - lmin);
  L = Math.max(...layer.values());
  // ---- dummies for edges spanning > 1 layer
  const layers = Array.from({ length: L + 1 }, () => []);
  const keyOrder = [...V.keys()].sort((x, y) => (disc.get(x) ?? 1e9) - (disc.get(y) ?? 1e9));
  if (O.order) for (const [id, v] of Object.entries(O.order)) if (V.has(id)) disc.set(id, v);
  const ordKeys = [...V.keys()].sort((x, y) => (disc.get(x) ?? 1e9) - (disc.get(y) ?? 1e9));
  for (const k of ordKeys) layers[layer.get(k)].push(k);
  let dcount = 0;
  const chains = [];
  for (const e of E) {
    const la = layer.get(e.a), lb = layer.get(e.b);
    const chain = [e.a];
    for (let l = la + 1; l < lb; l++) {
      const id = '~d' + dcount++;
      V.set(id, { id, real: false, c: 2, d: 0, w: 2, h: 2, dummy: true });
      layer.set(id, l);
      layers[l].push(id);
      chain.push(id);
    }
    chain.push(e.b);
    e.chain = chain;
    chains.push(chain);
  }
  // ---- crossing minimisation: barycenter sweeps
  const up = new Map(), down = new Map();
  for (const k of V.keys()) { up.set(k, []); down.set(k, []); }
  for (const e of E) for (let i = 0; i + 1 < e.chain.length; i++) { down.get(e.chain[i]).push(e.chain[i + 1]); up.get(e.chain[i + 1]).push(e.chain[i]); }
  const pos = new Map();
  const reindex = () => layers.forEach((ly) => ly.forEach((k, i) => pos.set(k, i)));
  reindex();
  const countCross = () => {
    let x = 0;
    for (let l = 0; l < L; l++) {
      const es = [];
      for (const k of layers[l]) for (const t of down.get(k)) es.push([pos.get(k), pos.get(t)]);
      for (let i = 0; i < es.length; i++) for (let j = i + 1; j < es.length; j++) if ((es[i][0] - es[j][0]) * (es[i][1] - es[j][1]) < 0) x++;
    }
    return x;
  };
  let best = layers.map((l) => l.slice()), bestX = countCross();
  for (let it = 0; it < 14; it++) {
    const dir = it % 2 === 0 ? 1 : -1;
    const rng = dir === 1 ? [...Array(L).keys()].map((i) => i + 1) : [...Array(L).keys()].map((i) => L - 1 - i);
    for (const l of rng) {
      const nb = dir === 1 ? up : down;
      const bary = new Map();
      layers[l].forEach((k, i) => { const ns = nb.get(k); bary.set(k, ns.length ? ns.reduce((s, n) => s + pos.get(n), 0) / ns.length : i); });
      layers[l].sort((a, b) => bary.get(a) - bary.get(b) || pos.get(a) - pos.get(b));
      layers[l].forEach((k, i) => pos.set(k, i));
    }
    const x = countCross();
    if (x < bestX) { bestX = x; best = layers.map((l) => l.slice()); }
  }
  for (let l = 0; l <= L; l++) layers[l] = best[l];
  if (O.pin) for (const ly of layers) {
    const idx = ly.map((k, i) => (O.pin.includes(k) ? i : -1)).filter((i) => i >= 0);
    if (idx.length < 2) continue;
    const want = idx.map((i) => ly[i]).sort((x, y) => O.pin.indexOf(x) - O.pin.indexOf(y));
    idx.forEach((i, j) => { ly[i] = want[j]; });
  }
  reindex();
  // ---- coordinates. depth first
  const lh = layers.map((ly) => Math.max(0, ...ly.map((k) => V.get(k).d)));
  const sumH = lh.reduce((a, b) => a + b, 0);
  const Wc = tb ? O.W : O.H, Wd = tb ? O.H : O.W;
  let gapD = O.gapD;
  if (L > 0) gapD = Math.max(O.gapDmin, Math.min(O.gapDmax, (Wd - sumH) / L));
  const dpos = [];
  { let y = lh[0] / 2; dpos[0] = y; for (let l = 1; l <= L; l++) { y += lh[l - 1] / 2 + gapD + lh[l] / 2; dpos[l] = y; } }
  // cross axis
  const gapC = O.gapC;
  const cx = new Map();
  layers.forEach((ly) => {
    const sep = ly.map((k, i) => (i ? (V.get(k).c + V.get(ly[i - 1]).c) / 2 + (V.get(k).dummy || V.get(ly[i - 1]).dummy ? 3 : gapC) : 0));
    const total = sep.reduce((a, b) => a + b, 0);
    let x = -total / 2;
    ly.forEach((k, i) => { x += sep[i]; cx.set(k, x); });
  });
  const wOf = (k) => (V.get(k).dummy ? 4 : 1);
  const relax = (l, nb) => {
    const ly = layers[l];
    const desired = ly.map((k) => { const ns = nb.get(k); return ns.length ? ns.reduce((s, n) => s + cx.get(n), 0) / ns.length : cx.get(k); });
    const sep = ly.map((k, i) => (i ? (V.get(k).c + V.get(ly[i - 1]).c) / 2 + (V.get(k).dummy || V.get(ly[i - 1]).dummy ? 3 : gapC) : 0));
    const xs = placeLayer(desired, sep, ly.map((k) => wOf(k)));
    ly.forEach((k, i) => cx.set(k, xs[i]));
  };
  const both = new Map();
  for (const k of V.keys()) both.set(k, [...up.get(k), ...down.get(k)]);
  for (let round = 0; round < 8; round++) {
    for (let l = 1; l <= L; l++) relax(l, up);
    for (let l = L - 1; l >= 0; l--) relax(l, down);
  }
  for (let l = 0; l <= L; l++) relax(l, both);
  // normalise + fit
  let cmin = Infinity, cmax = -Infinity;
  for (const [k, v] of V) { cmin = Math.min(cmin, cx.get(k) - v.c / 2); cmax = Math.max(cmax, cx.get(k) + v.c / 2); }
  let natC = cmax - cmin;
  const natD = dpos[L] + lh[L] / 2;
  let sc = 1;
  // satellites (error handlers) need a little extra room on the cross axis (right side)
  const satPad = [...V.values()].some((v) => v.sat) ? 10 : 0;
  if (natC + satPad > Wc || natD > Wd) sc = Math.min(Wc / (natC + satPad), Wd / natD);
  const stretch = natC * sc + satPad < Wc ? Math.min(O.stretchC, (Wc - satPad) / (natC * sc)) : 1;
  const mid = (cmin + cmax) / 2;
  const C = (k) => (cx.get(k) - mid) * sc * stretch + Wc / 2;
  const D = (l) => dpos[l] * sc + Math.max(0, (Wd - natD * sc) / 2);
  const out2 = { O, sc, tb, L, nodes: [], edges: [], W: O.W, H: O.H, stats: { nodes: M.nodes.size, edges: E.length, layers: L + 1, cross: bestX, scale: +sc.toFixed(2), gapD: +gapD.toFixed(1) } };
  const P = (c, d) => (tb ? [c, d] : [d, c]);
  const nodeInfo = new Map();
  for (const [k, v] of V) {
    if (v.dummy) continue;
    const c = C(k), d = D(layer.get(k));
    const [x, y] = P(c, d);
    const info = { ...M.nodes.get(k), x, y, w: v.w * sc, h: v.h * sc, c, d, dh: v.d * sc, cw: v.c * sc, layer: layer.get(k) };
    nodeInfo.set(k, info);
    out2.nodes.push(info);
  }
  // edges: ports spread along the node's cross size
  const portOut = new Map(), portIn = new Map();
  for (const e of E) {
    (portOut.get(e.chain[0]) || portOut.set(e.chain[0], []).get(e.chain[0])).push(e);
    (portIn.get(e.chain[e.chain.length - 1]) || portIn.set(e.chain[e.chain.length - 1], []).get(e.chain[e.chain.length - 1])).push(e);
  }
  const portOff = (list, e, key, node) => {
    const arr = list.slice().sort((p, q2) => C(p.chain[key === 'out' ? 1 : p.chain.length - 2]) - C(q2.chain[key === 'out' ? 1 : q2.chain.length - 2]));
    const i = arr.indexOf(e);
    const span = Math.min(node.cw - 4, (arr.length - 1) * 5);
    return arr.length > 1 ? (i / (arr.length - 1) - 0.5) * span : 0;
  };
  for (const e of E) {
    const a = nodeInfo.get(e.chain[0]), b = nodeInfo.get(e.chain[e.chain.length - 1]);
    const pts = [];
    pts.push([C(e.a) + portOff(portOut.get(e.a), e, 'out', a), D(layer.get(e.a)) + a.dh / 2]);
    for (let i = 1; i < e.chain.length - 1; i++) pts.push([C(e.chain[i]), D(layer.get(e.chain[i]))]);
    pts.push([C(e.b) + portOff(portIn.get(e.b), e, 'in', b), D(layer.get(e.b)) - b.dh / 2]);
    // path: S-curves with vertical tangents
    let dstr = '';
    const p0 = P(...pts[0]);
    dstr += `M${p0[0].toFixed(1)},${p0[1].toFixed(1)}`;
    for (let i = 0; i + 1 < pts.length; i++) {
      const [c0, d0] = pts[i], [c1, d1] = pts[i + 1];
      const dm = (d0 + d1) / 2;
      const a1 = P(c0, dm), a2 = P(c1, dm), a3 = P(c1, d1);
      dstr += ` C${a1[0].toFixed(1)},${a1[1].toFixed(1)} ${a2[0].toFixed(1)},${a2[1].toFixed(1)} ${a3[0].toFixed(1)},${a3[1].toFixed(1)}`;
    }
    const tipEnd = pts[pts.length - 1], tipStart = pts[0];
    out2.edges.push({ s: e.s, t: e.t, a: e.a, b: e.b, cond: e.cond, back: !!e.back, bidir: !!e.bidir, d: dstr, layerA: layer.get(e.a), layerB: layer.get(e.b), end: P(...tipEnd), start: P(...tipStart), arrowEnd: !e.back || e.bidir, arrowStart: e.back || e.bidir });
  }
  // self loops
  for (const e of M.edges) if (e.self) {
    const n = nodeInfo.get(e.s);
    const r = 5 * sc + 2;
    const c0 = n.c + n.cw / 2, d0 = n.d;
    const a = P(c0, d0 - 3), b = P(c0 + r + 3, d0 - 3), c = P(c0 + r + 3, d0 + 3), d = P(c0, d0 + 3);
    out2.edges.push({ s: e.s, t: e.t, a: e.s, b: e.s, cond: e.cond, self: true, d: `M${a[0]},${a[1]} C${b[0]},${b[1]} ${c[0]},${c[1]} ${d[0]},${d[1]}`, end: d, layerA: n.layer, layerB: n.layer, arrowEnd: true, endDir: tb ? 'left' : 'up' });
  }
  return out2;
}

// ------------------------------------------------------------------------------------------------ drawing
/** Draw into <svg>; returns handles {nodes:[{g, glow, info}], edges:[{path, head, info}], layers} for the page animation. */
export function drawGraph(svg, L, st) {
  const { stroke = '#4b5878', fill = '#1a2030', accent = '#8aa0ff', edge = '#56627f', hold = '#ffaa72', sat = '#ff8b8f' } = st || {};
  svg.setAttribute('viewBox', `0 0 ${L.W} ${L.H}`);
  svg.setAttribute('width', L.W); svg.setAttribute('height', L.H);
  const gE = svgEl('g', {}, svg), gN = svgEl('g', {}, svg);
  const H = { nodes: [], edges: [], layers: L.L + 1 };
  const tb = L.tb;
  const arrow = (g, x, y, dir, color, op = 1) => {
    // dir: 'down','up','right','left' pointing direction
    const a = 5.2, w = 3.1;
    let pts;
    if (dir === 'down') pts = [[x, y], [x - w, y - a], [x + w, y - a]];
    else if (dir === 'up') pts = [[x, y], [x - w, y + a], [x + w, y + a]];
    else if (dir === 'right') pts = [[x, y], [x - a, y - w], [x - a, y + w]];
    else pts = [[x, y], [x + a, y - w], [x + a, y + w]];
    return svgEl('polygon', { points: pts.map((p) => p.map((v) => v.toFixed(1)).join(',')).join(' '), fill: color, opacity: op }, g);
  };
  for (const e of L.edges) {
    const g = svgEl('g', { class: 'edge' }, gE);
    const toEnd = e.b === '__end__' && e.cond;
    const col = e.back ? '#5a6688' : edge;
    const sw = e.back ? 1.1 : e.cond ? 1.3 : 1.9;
    const op = e.back ? 0.42 : toEnd ? 0.35 : e.cond ? 0.7 : 1;
    const path = svgEl('path', { d: e.d, fill: 'none', stroke: col, 'stroke-width': sw, 'stroke-linecap': 'round', opacity: op, ...(e.cond || e.back ? { 'stroke-dasharray': e.back ? '2.5 3' : '4 3' } : {}) }, g);
    const heads = [];
    const fwdDir = tb ? 'down' : 'right', backDir = tb ? 'up' : 'left';
    if (e.self) heads.push(arrow(g, e.end[0], e.end[1], e.endDir === 'left' ? 'left' : 'up', col, op));
    else {
      if (e.arrowEnd) heads.push(arrow(g, e.end[0], e.end[1], fwdDir, col, op));
      if (e.arrowStart) heads.push(arrow(g, e.start[0], e.start[1], backDir, col, op));
    }
    H.edges.push({ g, path, info: e });
  }
  for (const n of L.nodes) {
    const g = svgEl('g', { class: 'node', transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})` }, gN);
    const w = n.w, h = n.h;
    let glow = null;
    if (n.kind === 'start') {
      svgEl('circle', { r: w / 2, fill: '#6fd884', opacity: 0.95 }, g);
      glow = svgEl('circle', { r: w / 2 + 2, fill: 'none', stroke: '#6fd884', 'stroke-width': 2, opacity: 0 }, g);
    } else if (n.kind === 'end') {
      svgEl('circle', { r: w / 2, fill: 'none', stroke: '#b0b8cb', 'stroke-width': 2 }, g);
      svgEl('circle', { r: w / 5, fill: '#b0b8cb' }, g);
      glow = svgEl('circle', { r: w / 2 + 2, fill: 'none', stroke: accent, 'stroke-width': 2, opacity: 0 }, g);
    } else if (n.kind === 'group') {
      if (n.fan) { svgEl('rect', { x: -w / 2 + 4, y: -h / 2 - 4, width: w, height: h, rx: 6, fill: fill, stroke: stroke, 'stroke-width': 1.3, opacity: 0.7 }, g); }
      svgEl('rect', { x: -w / 2, y: -h / 2, width: w, height: h, rx: 6, fill: 'rgba(138,160,255,0.07)', stroke: accent, 'stroke-width': 1.5, 'stroke-dasharray': '' }, g);
      const k = n.inner.length;
      const bh = Math.max(2.5, Math.min(4.5, (h - 8) / (k * 1.6)));
      const gap = k > 1 ? Math.min(4, (h - 8 - k * bh) / (k - 1)) : 0;
      const tot = k * bh + (k - 1) * gap;
      for (let i = 0; i < k; i++) {
        const iw = Math.min(w - 12, tb ? 22 : w - 12);
        svgEl('rect', { x: -iw / 2, y: -tot / 2 + i * (bh + gap), width: iw, height: bh, rx: bh / 2, fill: '#8d97ad', opacity: 0.85 }, g);
      }
      glow = svgEl('rect', { x: -w / 2, y: -h / 2, width: w, height: h, rx: 6, fill: accent, opacity: 0 }, g);
    } else {
      const col = n.hold ? hold : stroke;
      if (n.fan) {
        svgEl('rect', { x: -w / 2 + 6, y: -h / 2 - 6, width: w, height: h, rx: 3.5, fill: fill, stroke: col, 'stroke-width': 1.2, opacity: 0.45 }, g);
        svgEl('rect', { x: -w / 2 + 3, y: -h / 2 - 3, width: w, height: h, rx: 3.5, fill: fill, stroke: col, 'stroke-width': 1.2, opacity: 0.75 }, g);
      }
      svgEl('rect', { x: -w / 2, y: -h / 2, width: w, height: h, rx: 3.5, fill: n.hold ? 'rgba(255,170,114,0.14)' : fill, stroke: col, 'stroke-width': n.hold ? 1.8 : 1.5 }, g);
      if (n.hold) {
        const ph2 = Math.min(h - 5, 7);
        svgEl('rect', { x: -3.4, y: -ph2 / 2, width: 2.2, height: ph2, rx: 0.8, fill: hold }, g);
        svgEl('rect', { x: 1.2, y: -ph2 / 2, width: 2.2, height: ph2, rx: 0.8, fill: hold }, g);
      }
      glow = svgEl('rect', { x: -w / 2, y: -h / 2, width: w, height: h, rx: 3.5, fill: accent, opacity: 0 }, g);
    }
    if (n.sat) {
      const sx = tb ? w / 2 + 6 : 0, sy = tb ? 0 : -h / 2 - 7;
      svgEl('line', { x1: tb ? w / 2 : 0, y1: tb ? 0 : -h / 2, x2: sx + 0, y2: sy, stroke: sat, 'stroke-width': 1.2, 'stroke-dasharray': '2 2', opacity: 0.8 }, g);
      svgEl('circle', { cx: sx + (tb ? 4 : 0), cy: sy - (tb ? 0 : 3), r: 4, fill: 'rgba(255,139,143,0.25)', stroke: sat, 'stroke-width': 1.4 }, g);
    }
    H.nodes.push({ g, glow, info: n });
  }
  return H;
}
