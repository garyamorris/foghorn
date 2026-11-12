// lg.js: LangGraph structure -> layout -> SVG view. No dependencies, no network except the local LangGraph API.
// Everything drawn comes from GET /assistants/{id}/graph?xray=true (nodes, edges, conditional flags, node metadata).

export const API = (typeof window !== 'undefined' && new URLSearchParams(location.search).get('api')) || 'http://127.0.0.1:2027';
const NS = 'http://www.w3.org/2000/svg';
export const FONT = '-apple-system, "SF Pro Display", "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif';
export const MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
export const COL = { bg: '#0a0e16', panel: '#131a2a', panel2: '#19223a', line: '#2b364f', text: '#eef2fa', muted: '#9aa7c0', dim: '#66748f', blue: '#4aa8ff', green: '#3fe0a0', amber: '#ffb938', gold: '#f4c95d', red: '#ff6b7a', grey: '#5c6a86' };

// ---------------------------------------------------------------------------------------------------------- API
export async function jget(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`GET ${url} -> ${r.status}`);
  return r.json();
}
export async function jpost(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) });
  if (!r.ok) throw new Error(`POST ${url} -> ${r.status}`);
  return r.json();
}
export async function loadAssistants() {
  const list = await jpost(`${API}/assistants/search`, { limit: 50 });
  const m = {};
  for (const a of list) m[a.graph_id] = { id: a.assistant_id, graph_id: a.graph_id, name: a.name, description: a.description || '' };
  return m;
}
export const loadGraph = (assistantId) => jget(`${API}/assistants/${assistantId}/graph?xray=true`);
export const loadSchemas = (assistantId) => jget(`${API}/assistants/${assistantId}/schemas`);

// ---------------------------------------------------------------------------------------------------------- text
let _ctx = null;
export function textW(str, size, weight = 600, family = FONT) {
  if (!_ctx) _ctx = document.createElement('canvas').getContext('2d');
  _ctx.font = `${weight} ${size}px ${family}`;
  return _ctx.measureText(str).width;
}

// ---------------------------------------------------------------------------------------------------------- normalise
/** Raw xray graph -> {nodes: Map, edges: [], groups: Map, dropped: []}. Subgraph nodes ("quill:draft") become group members. */
export function normalizeGraph(raw, o = {}) {
  const shorten = o.shorten || ((s) => s);
  const dropped = [];
  const ids = raw.nodes.map((n) => n.id);
  const groups = new Map();
  for (const n of raw.nodes) {
    const m = /^([^:]+):(.+)$/.exec(n.id);
    if (!m) continue;
    const g = groups.get(m[1]) || { id: m[1], members: [], startId: null, endId: null };
    groups.set(m[1], g);
    if (m[2] === '__start__') g.startId = n.id;
    else if (m[2] === '__end__') g.endId = n.id;
    else g.members.push(n.id);
  }
  const edges = [];
  for (const e of raw.edges) {
    let src = e.source, dst = e.target, toGroup = null, fromGroup = null, skip = false;
    for (const g of groups.values()) {
      if (src === g.startId || src === g.endId || dst === g.endId) skip = true;
      if (dst === g.startId) {
        toGroup = g.id;
        const inner = raw.edges.find((x) => x.source === g.startId);
        dst = inner ? inner.target : g.members[0];
      }
      if (g.members.includes(src) && !g.members.includes(dst) && dst !== g.startId) fromGroup = g.id;
    }
    if (skip) continue;
    edges.push({ id: `${src}>${dst}${toGroup ? '@' + toGroup : ''}`, src, dst, cond: !!e.conditional, toGroup, fromGroup, self: src === dst, back: false });
  }
  const used = new Set();
  for (const e of edges) { used.add(e.src); used.add(e.dst); }
  const nodes = new Map();
  for (const n of raw.nodes) {
    if (/__start__$/.test(n.id) && n.id !== '__start__') continue;
    if (/__end__$/.test(n.id) && n.id !== '__end__') continue;
    if (!used.has(n.id)) { dropped.push(n.id); continue; }
    const gm = /^([^:]+):(.+)$/.exec(n.id);
    const name = n.data?.name || n.id;
    const kind = n.id === '__start__' ? 'start' : n.id === '__end__' ? 'end' : 'node';
    nodes.set(n.id, {
      id: n.id, kind, group: gm ? gm[1] : null, meta: n.metadata || {},
      label: kind === 'start' ? 'START' : kind === 'end' ? 'END' : shorten(gm ? gm[2] : name),
    });
  }
  for (const g of groups.values()) g.members = g.members.filter((m) => nodes.has(m));
  return { nodes, edges, groups, dropped, id: o.id || null };
}

function analyze(G, forceBack = []) {
  const out = new Map();
  for (const id of G.nodes.keys()) out.set(id, []);
  for (const e of G.edges) out.get(e.src)?.push(e);
  const st = new Map();
  const dfs = (u) => {
    st.set(u, 1);
    for (const e of out.get(u) || []) {
      if (e.self) continue;
      const s = st.get(e.dst);
      if (s === 1) e.back = true;
      else if (!s) dfs(e.dst);
    }
    st.set(u, 2);
  };
  const roots = ['__start__', ...G.nodes.keys()].filter((id) => G.nodes.has(id));
  for (const r of roots) if (!st.get(r)) dfs(r);
  for (const e of G.edges) if (forceBack.includes(e.id)) e.back = true;
  // longest-path layering on forward edges
  const layer = new Map();
  for (const id of G.nodes.keys()) layer.set(id, 0);
  const fwd = G.edges.filter((e) => !e.back && !e.self);
  const indeg = new Map();
  for (const id of G.nodes.keys()) indeg.set(id, 0);
  for (const e of fwd) indeg.set(e.dst, indeg.get(e.dst) + 1);
  const q = [...G.nodes.keys()].filter((id) => indeg.get(id) === 0);
  while (q.length) {
    const u = q.shift();
    for (const e of fwd.filter((x) => x.src === u)) {
      layer.set(e.dst, Math.max(layer.get(e.dst), layer.get(u) + 1));
      indeg.set(e.dst, indeg.get(e.dst) - 1);
      if (indeg.get(e.dst) === 0) q.push(e.dst);
    }
  }
  if (G.nodes.has('__end__')) {
    let mx = 0;
    for (const [id, l] of layer) if (id !== '__end__') mx = Math.max(mx, l);
    layer.set('__end__', mx + 1);
  }
  G.layer = layer;
  return G;
}

// ---------------------------------------------------------------------------------------------------------- layout
const DEF = {
  W: 1200, H: 400, marginX: 24, font: 26, nodeH: 58, padX: 22, minW: 90, termH: 44, termFont: 18, minGapSingle: 76, minGap: 46, maxGap: 150, vgap: 34,
  rows: null, fan: {}, order: [], twoLine: 0, k: 1, nodeStroke: 0, termDot: 0, aboveMin: 46, groupPadX: 22, groupTop: 46, groupBottom: 22, chip: 22, chipGap: 4, forceBack: [], maxRows: 3, tabH: 30, rowGapMin: 60,
};

export function layoutGraph(G, opt = {}) {
  const O = { ...DEF, ...opt };
  analyze(G, O.forceBack);
  // sizes
  for (const n of G.nodes.values()) {
    if (n.kind === 'node') {
      n.lines = O.twoLine && n.label.length > O.twoLine && n.label.includes('.') ? [n.label.slice(0, n.label.indexOf('.')), n.label.slice(n.label.indexOf('.'))] : [n.label];
      n.h = n.lines.length > 1 ? Math.round(O.nodeH * 1.5) : O.nodeH;
      n.w = Math.max(O.minW, Math.ceil(Math.max(...n.lines.map((s) => textW(s, O.font))) + 2 * O.padX));
    } else if (O.termDot) {
      n.h = O.termDot; n.w = O.termDot;
    } else {
      n.h = O.termH;
      n.w = Math.ceil(textW(n.label, O.termFont, 700) + 40);
    }
  }
  // columns
  const L = Math.max(...G.layer.values()) + 1;
  const cols = Array.from({ length: L }, (_, i) => ({ i, nodes: [], padL: 0, padR: 0 }));
  for (const [id, l] of G.layer) cols[l].nodes.push(G.nodes.get(id));
  // initial order = declaration order; barycentre sweeps
  const pos = new Map();
  const setPos = () => cols.forEach((c) => c.nodes.forEach((n, i) => pos.set(n.id, c.nodes.length > 1 ? i / (c.nodes.length - 1) : 0.5)));
  setPos();
  const fwdEdges = G.edges.filter((e) => !e.back && !e.self);
  const orderIdx = (id) => { const k = O.order.indexOf(id); return k < 0 ? 999 : k; };
  for (let sweep = 0; sweep < 4; sweep++) {
    for (let i = 1; i < L; i++) {
      const c = cols[i];
      const bary = (n) => { const ps = fwdEdges.filter((e) => e.dst === n.id).map((e) => pos.get(e.src)); return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : pos.get(n.id); };
      c.nodes.sort((a, b) => bary(a) - bary(b) || orderIdx(a.id) - orderIdx(b.id));
      c.nodes.forEach((n, k) => pos.set(n.id, c.nodes.length > 1 ? k / (c.nodes.length - 1) : 0.5));
    }
  }
  for (const c of cols) if (c.nodes.length > 1 && c.nodes.some((n) => O.order.includes(n.id))) c.nodes.sort((a, b) => orderIdx(a.id) - orderIdx(b.id));
  // group padding on first / last member columns
  for (const g of G.groups.values()) {
    if (!g.members.length) continue;
    const ls = g.members.map((m) => G.layer.get(m));
    cols[Math.min(...ls)].padL = Math.max(cols[Math.min(...ls)].padL, O.groupPadX);
    cols[Math.max(...ls)].padR = Math.max(cols[Math.max(...ls)].padR, O.groupPadX);
  }
  for (const c of cols) {
    c.nw = Math.max(...c.nodes.map((n) => n.w));
    c.w = c.nw + c.padL + c.padR;
    c.h = c.nodes.reduce((a, n) => a + n.h, 0) + (c.nodes.length - 1) * O.vgap;
  }
  // rows (snake)
  const AW = O.W - 2 * O.marginX;
  const sumW = (a, b) => cols.slice(a, b).reduce((s, c) => s + c.w, 0);
  let rowsSpec = O.rows;
  if (!rowsSpec) {
    const need1 = sumW(0, L) + (L - 1) * O.minGapSingle;
    if (need1 <= AW) rowsSpec = [L];
    else {
      let best = null;
      for (let R = 2; R <= O.maxRows && !best; R++) {
        // brute-force the split minimising the widest row
        const splits = [];
        const rec = (start, left, acc) => {
          if (left === 1) { splits.push([...acc, L - start]); return; }
          for (let k = 1; k <= L - start - (left - 1); k++) rec(start + k, left - 1, [...acc, k]);
        };
        rec(0, R, []);
        let bw = Infinity, bs = null;
        const groupSpans = [...G.groups.values()].filter((g) => g.members.length).map((g) => { const ls = g.members.map((m) => G.layer.get(m)); return [Math.min(...ls), Math.max(...ls)]; });
        const splitsOk = (sp) => { let acc = 0; for (let t = 0; t < sp.length - 1; t++) { acc += sp[t]; if (groupSpans.some(([lo, hi]) => lo < acc && hi >= acc)) return false; } return true; };
        const okSplits = splits.filter(splitsOk);
        for (const s of (okSplits.length ? okSplits : splits)) {
          let a = 0, mx = 0;
          for (const k of s) { mx = Math.max(mx, sumW(a, a + k) + (k - 1) * O.minGapSingle); a += k; }
          // prefer balanced rows, tie-break on width
          const bal = Math.max(...s) - Math.min(...s);
          const score = mx + bal * 2;
          if (score < bw) { bw = score; bs = s; }
        }
        if (bs) { let a = 0, mx = 0; for (const k of bs) { mx = Math.max(mx, sumW(a, a + k) + (k - 1) * O.minGap); a += k; } if (mx <= AW || R === O.maxRows) best = bs; }
      }
      rowsSpec = best || [L];
    }
  }
  // slots: row 0 left->right, row 1 right->left (below the end of row 0), row 2 left->right ...
  const rowOf = new Array(L), slotOf = new Array(L);
  {
    let slot = 0, dir = 1, a = 0;
    rowsSpec.forEach((k, r) => {
      for (let j = 0; j < k; j++) { rowOf[a + j] = r; slotOf[a + j] = slot; if (j < k - 1) slot += dir; }
      dir = -dir; // the next row starts below the last column of this one
      a += k;
    });
  }
  const minSlot = Math.min(...slotOf);
  for (let i = 0; i < L; i++) slotOf[i] -= minSlot;
  const S = Math.max(...slotOf) + 1;
  const slotW = new Array(S).fill(0);
  for (let i = 0; i < L; i++) slotW[slotOf[i]] = Math.max(slotW[slotOf[i]], cols[i].w);
  const totalSlotW = slotW.reduce((x, y) => x + y, 0);
  let gap = S > 1 ? (AW - totalSlotW) / (S - 1) : 0;
  gap = Math.max(Math.min(gap, O.maxGap), S > 1 ? Math.min(O.minGap, gap) : 0);
  const totalW = totalSlotW + gap * (S - 1);
  const x0 = (O.W - totalW) / 2;
  const slotX = [];
  let xx = x0;
  for (let s = 0; s < S; s++) { slotX.push(xx + slotW[s] / 2); xx += slotW[s] + gap; }
  // vertical bands
  const fanH = (id) => {
    const f = O.fan[id];
    if (!f) return 0;
    const rws = Math.ceil(f.n / f.cols);
    return 12 + rws * (O.chip + O.chipGap) + (f.footer === false ? 0 : 26);
  };
  const hasGroupInRow = (r) => [...G.groups.values()].some((g) => g.members.some((m) => rowOf[G.layer.get(m)] === r));
  const rowsInfo = rowsSpec.map((k, r) => {
    const rc = cols.filter((c, i) => rowOf[i] === r);
    const nodesIn = rc.flatMap((c) => c.nodes);
    const hasBack = G.edges.some((e) => e.back && rowOf[G.layer.get(e.src)] === r && rowOf[G.layer.get(e.dst)] === r);
    return {
      r, h: Math.max(...rc.map((c) => c.h)),
      above: Math.max(O.aboveMin, hasGroupInRow(r) ? O.groupTop + O.tabH / 2 + 4 : 0, hasBack ? 74 * O.k : 0),
      below: Math.max(O.groupBottom + 2, ...nodesIn.map((n) => fanH(n.id)), 14 * O.k),
    };
  });
  const bandSum = rowsInfo.reduce((s, b) => s + b.above + b.h + b.below, 0);
  const R = rowsInfo.length;
  const free = O.H - bandSum;
  const between = R > 1 ? Math.max(0, free / R) : 0;
  let y = R > 1 ? Math.max(0, between / 2) : Math.max(0, free / 2 - 6);
  for (const b of rowsInfo) {
    b.top = y;
    b.cy = y + b.above + b.h / 2;
    y += b.above + b.h + b.below + (R > 1 ? between : 0);
  }
  const overflow = Math.max(0, -free);
  // node coordinates
  for (const c of cols) {
    const b = rowsInfo[rowOf[c.i]];
    const cx = slotX[slotOf[c.i]] + (c.padL - c.padR) / 2;
    let yy = b.cy - c.h / 2;
    for (const n of c.nodes) { n.cx = cx; n.cy = yy + n.h / 2; n.row = rowOf[c.i]; n.slot = slotOf[c.i]; n.col = c.i; yy += n.h + O.vgap; }
  }
  // group boxes
  for (const g of G.groups.values()) {
    const ms = g.members.map((m) => G.nodes.get(m)).filter(Boolean);
    if (!ms.length) continue;
    const x1 = Math.min(...ms.map((n) => n.cx - n.w / 2)) - O.groupPadX;
    const x2 = Math.max(...ms.map((n) => n.cx + n.w / 2)) + O.groupPadX;
    const y1 = Math.min(...ms.map((n) => n.cy - n.h / 2)) - O.groupTop;
    const y2 = Math.max(...ms.map((n) => n.cy + n.h / 2)) + O.groupBottom;
    g.box = { x: x1, y: y1, w: x2 - x1, h: y2 - y1, cx: (x1 + x2) / 2, cy: (y1 + y2) / 2, row: ms[0].row };
  }
  G.rowsSpec = rowsSpec;
  G.size = { W: O.W, H: O.H, overflow };
  G.opt = O;
  routeEdges(G, O);
  return G;
}

// ---------------------------------------------------------------------------------------------------------- routing
function routeEdges(G, O) {
  const K = O.k || 1;
  const box = (n) => ({ x: n.cx - n.w / 2, y: n.cy - n.h / 2, w: n.w, h: n.h, cx: n.cx, cy: n.cy, row: n.row });
  const ports = new Map(); // key "id|side" -> [{e, end, key}]
  const addPort = (id, side, e, end, key) => { const k = id + '|' + side; if (!ports.has(k)) ports.set(k, []); ports.get(k).push({ e, end, key }); };
  const nodesArr = [...G.nodes.values()];
  const GAP = Math.max(2, 4 * K);
  const info = [];
  for (const e of G.edges) {
    const A = G.nodes.get(e.src), B = G.nodes.get(e.dst);
    if (!A || !B) continue;
    const grpA = e.fromGroup ? G.groups.get(e.fromGroup) : null;
    const grpB = e.toGroup ? G.groups.get(e.toGroup) : null;
    const a = box(A), b = grpB ? { ...grpB.box } : box(B);
    const aBox = grpA ? { ...grpA.box, cx: a.cx } : a;
    let type, sa, sb;
    if (e.self) { type = 'self'; sa = 'T'; sb = 'T'; }
    else if (a.row === b.row) {
      const lo = Math.min(a.cx, b.cx), hi = Math.max(a.cx, b.cx);
      const blocked = nodesArr.some((n) => n !== A && n !== B && n.row === a.row && n.cx > lo && n.cx < hi && n.group !== (grpB && grpB.id) && Math.abs(n.cy - a.cy) < n.h / 2 + 10 && !(e.toGroup && n.group === e.toGroup));
      if (e.back) { type = 'over'; sa = 'T'; sb = 'T'; }
      else if (blocked) { type = 'under'; sa = 'B'; sb = 'B'; }
      else if (Math.abs(a.cx - b.cx) < 4 && a.cy !== b.cy) { type = 'v'; sa = b.cy > a.cy ? 'B' : 'T'; sb = b.cy > a.cy ? 'T' : 'B'; }
      else { type = 'h'; sa = b.cx > a.cx ? 'R' : 'L'; sb = b.cx > a.cx ? 'L' : 'R'; }
    } else {
      type = 'v';
      const down = b.row > a.row;
      sa = down ? 'B' : 'T'; sb = down ? 'T' : 'B';
    }
    const rec = { e, A, B, a, b, aBox, type, sa, sb, grpA, grpB };
    info.push(rec);
    if (type === 'self') continue;
    addPort(A.id, sa, e, 'src', type === 'h' ? b.cy : b.cx);
    addPort(B.id + (grpB ? '@g' : ''), sb, e, 'dst', type === 'h' ? a.cy : a.cx);
  }
  // spread ports on a side
  const off = new Map();
  for (const [k, list] of ports) {
    list.sort((p, q) => p.key - q.key);
    const n = list.length;
    const isV = k.endsWith('|L') || k.endsWith('|R');
    const room = isV ? 36 * K : 120 * K;
    const step = Math.min(26 * K, room / n);
    list.forEach((p, i) => off.set(p.e.id + p.end, (i - (n - 1) / 2) * step));
  }
  for (const r of info) {
    const { e, A, B, a, b, aBox, type, sa, sb, grpB } = r;
    const oS = off.get(e.id + 'src') || 0, oD = off.get(e.id + 'dst') || 0;
    let d;
    let mid = null;
    if (type === 'self') {
      const xr = a.x + a.w;
      const xa = xr - 66 * K, xb = xr - 22 * K, yt = a.y;
      d = `M${xa} ${yt} C${xa} ${yt - 52 * K} ${xb} ${yt - 52 * K} ${xb} ${yt - GAP}`;
      mid = { x: (xa + xb) / 2, y: yt - 40 * K };
    } else if (type === 'h') {
      const x1 = sa === 'R' ? a.x + a.w : a.x, x2 = sb === 'L' ? b.x : b.x + b.w;
      const y1 = a.cy + oS, y2 = (grpB ? A.cy : b.cy) + oD;
      const dir = x2 > x1 ? 1 : -1;
      const xe = x2 - dir * GAP;
      const xm = (x1 + xe) / 2;
      d = Math.abs(y1 - y2) < 1 ? `M${x1} ${y1} L${xe} ${y2}` : `M${x1} ${y1} C${xm} ${y1} ${xm} ${y2} ${xe} ${y2}`;
      mid = { x: xm, y: (y1 + y2) / 2 };
    } else if (type === 'v') {
      const down = sa === 'B';
      const y1 = (down ? aBox.y + aBox.h : aBox.y);
      const y2 = down ? b.y : b.y + b.h;
      const x1 = a.cx + oS;
      let x2 = b.cx + oD;
      if (grpB) x2 = Math.max(b.x + 24, Math.min(b.x + b.w - 24, A.cx + oD));
      const ye = y2 + (down ? -GAP : GAP);
      const ym = (y1 + ye) / 2;
      d = Math.abs(x1 - x2) < 1 ? `M${x1} ${y1} L${x2} ${ye}` : `M${x1} ${y1} C${x1} ${ym} ${x2} ${ym} ${x2} ${ye}`;
      mid = { x: (x1 + x2) / 2, y: ym };
    } else if (type === 'under') {
      const yb = Math.max(a.y + a.h, b.y + b.h);
      const dep = (46 + Math.min(34, Math.abs(a.cx - b.cx) * 0.06)) * K;
      const x1 = a.cx + oS, x2 = b.cx + oD;
      d = `M${x1} ${a.y + a.h} C${x1} ${yb + dep} ${x2} ${yb + dep} ${x2} ${b.y + b.h + GAP}`;
      mid = { x: (x1 + x2) / 2, y: yb + dep * 0.75 };
    } else if (type === 'over') {
      const yt = Math.min(a.y, b.y);
      const dep = (40 + Math.min(26, Math.abs(a.cx - b.cx) * 0.08)) * K;
      const x1 = a.cx + oS * 0.6 - 10, x2 = b.cx + oD * 0.6 + 10;
      d = `M${x1} ${a.y} C${x1} ${yt - dep} ${x2} ${yt - dep} ${x2} ${b.y - GAP}`;
      mid = { x: (x1 + x2) / 2, y: yt - dep * 0.75 };
    }
    e.d = d;
    e.mid = mid;
    e.type = type;
  }
}

// ---------------------------------------------------------------------------------------------------------- view
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
};
const STYLE = `
.edge{fill:none;stroke-linecap:round;transition:stroke .25s,opacity .25s}
.edge.idle{stroke:#55627f}
.edge.active{stroke:#4aa8ff;stroke-width:4.5}
.edge.done{stroke:#3fe0a0;opacity:.8}
.edge.cond{stroke-dasharray:10 8}
.edge.active.cond{animation:dash 0.7s linear infinite}
@keyframes dash{to{stroke-dashoffset:-18}}
.node .body{fill:#141c2e;stroke:#5c6a86;stroke-width:3;transition:fill .25s,stroke .25s}
.node.term .body{fill:#10192b;stroke:#3a4763;stroke-width:2.5}
.node.running .body{fill:#12304f;stroke:#4aa8ff}
.node.done .body{fill:#0f2f2a;stroke:#3fe0a0}
.node.interrupted .body{fill:#3a2a0b;stroke:#ffb938}
.node .halo{fill:none;stroke:#4aa8ff;stroke-width:8;opacity:0}
.node.running .halo{animation:halo 1.0s ease-in-out infinite}
.node.interrupted .halo{stroke:#ffb938;animation:halo 1.3s ease-in-out infinite}
@keyframes halo{0%,100%{opacity:.12;stroke-width:5}50%{opacity:.6;stroke-width:12}}
.node text.lbl{fill:#eef2fa;font-weight:600;text-anchor:middle;dominant-baseline:central}
.node.idle text.lbl{fill:#c3cde2}
.node.term text.lbl{fill:#9aa7c0;font-weight:700;letter-spacing:.08em}
.chip{fill:#161f33;stroke:#3a4763;stroke-width:1.6}
.chip.run{fill:#12304f;stroke:#4aa8ff;animation:chip .6s ease-in-out infinite}
.chip.done{fill:#3fe0a0;stroke:#3fe0a0}
@keyframes chip{50%{opacity:.45}}
.group-box{fill:rgba(74,168,255,.035);stroke:#4b5b7c;stroke-width:2.5;stroke-dasharray:3 9;stroke-linecap:round}
.group-box.running{stroke:#4aa8ff;fill:rgba(74,168,255,.07)}
.group-box.done{stroke:#3fe0a0}
.pill-t{font-weight:700;text-anchor:middle;dominant-baseline:central}
.fade-in{animation:fin .45s ease both}
@keyframes fin{from{opacity:0}to{opacity:1}}
`;

/** Build the SVG view for a laid-out graph. Returns a controller used by the live page and the overview. */
export function createView(svg, G, o = {}) {
  const O = { ...G.opt, ...o };
  svg.innerHTML = '';
  svg.setAttribute('viewBox', `0 0 ${G.size.W} ${G.size.H}`);
  svg.setAttribute('xmlns', NS);
  const style = el('style', {}, svg); style.textContent = STYLE;
  const defs = el('defs', {}, svg);
  const mk = (id, color) => {
    const m = el('marker', { id: `ah-${id}`, viewBox: '0 0 10 10', refX: 9.5, refY: 5, markerWidth: O.arrow || 14, markerHeight: O.arrow || 14, markerUnits: 'userSpaceOnUse', orient: 'auto' }, defs);
    el('path', { d: 'M0.5,0.8 L9.5,5 L0.5,9.2 L2.8,5 z', fill: color }, m);
  };
  mk('idle', '#6d7b99'); mk('active', '#4aa8ff'); mk('done', '#3fe0a0'); mk('gold', '#f4c95d');
  const gGroups = el('g', {}, svg), gEdges = el('g', {}, svg), gNodes = el('g', {}, svg), gOver = el('g', {}, svg);
  const V = { svg, G, nodes: {}, edges: {}, groups: {}, fans: {}, tags: {}, gOver, ew: O.edgeW || 3.2 };

  // groups
  for (const g of G.groups.values()) {
    if (!g.box) continue;
    const gg = el('g', {}, gGroups);
    const r = el('rect', { class: 'group-box', x: g.box.x, y: g.box.y, width: g.box.w, height: g.box.h, rx: 24 }, gg);
    const lab = `${g.id}`;
    const tw = textW(lab, O.groupFont || 20, 700, MONO) + 20;
    const tabW = tw + (O.groupSub ? textW(O.groupSub, 16, 600) + 14 : 0);
    const tab = el('g', { transform: `translate(${g.box.x + g.box.w - 18 - tabW},${g.box.y - 15})` }, gg);
    el('rect', { x: 0, y: 0, width: tabW, height: O.tabH || 30, rx: 8, fill: '#0f1626', stroke: '#4b5b7c', 'stroke-width': 2 }, tab);
    const t1 = el('text', { x: 10, y: (O.tabH || 30) / 2, fill: '#c9d4ee', 'font-size': O.groupFont || 20, 'font-weight': 700, 'dominant-baseline': 'central', style: `font-family:${MONO}` }, tab); t1.textContent = lab;
    if (O.groupSub) { const t2 = el('text', { x: tw + 4, y: (O.tabH || 30) / 2, fill: '#9aa7c0', 'font-size': 16, 'font-weight': 600, 'dominant-baseline': 'central' }, tab); t2.textContent = O.groupSub; }
    V.groups[g.id] = { rect: r, tab, box: g.box };
  }
  // edges
  for (const e of G.edges) {
    if (!e.d) continue;
    const p = el('path', { class: `edge idle${e.cond ? ' cond' : ''}`, d: e.d, 'stroke-width': V.ew, 'marker-end': 'url(#ah-idle)' }, gEdges);
    V.edges[e.id] = { e, path: p, len: p.getTotalLength(), state: 'idle', label: null };
  }
  // nodes
  const fs = O.font, tf = O.termFont;
  for (const n of G.nodes.values()) {
    const g = el('g', { class: `node idle${n.kind !== 'node' ? ' term' : ''}`, transform: `translate(${n.cx},${n.cy})` }, gNodes);
    const rx = n.kind === 'node' ? Math.min(16, n.h / 2.4) : n.h / 2;
    const sw = O.nodeStroke;
    const halo = el('rect', { class: 'halo', x: -n.w / 2 - 4, y: -n.h / 2 - 4, width: n.w + 8, height: n.h + 8, rx: rx + 4 }, g);
    const body = el('rect', { class: 'body', x: -n.w / 2, y: -n.h / 2, width: n.w, height: n.h, rx, ...(sw ? { 'stroke-width': sw } : {}) }, g);
    const t = el('text', { class: 'lbl', x: 0, y: 1, 'font-size': n.kind === 'node' ? fs : tf }, g);
    if (n.lines && n.lines.length > 1) {
      n.lines.forEach((s, i) => { const ts = el('tspan', { x: 0, y: (i - (n.lines.length - 1) / 2) * fs * 1.12 + 1 }, t); ts.textContent = s; });
    } else t.textContent = O.termDot && n.kind !== 'node' ? '' : n.label;
    V.nodes[n.id] = { n, g, body, halo, text: t, state: 'idle', badges: {} };
  }

  // ---- node state
  V.setNode = (id, state) => {
    const v = V.nodes[id]; if (!v) return;
    v.state = state;
    v.g.setAttribute('class', `node ${state}${v.n.kind !== 'node' ? ' term' : ''}`);
  };
  V.setGroup = (id, state) => { const v = V.groups[id]; if (v) v.rect.setAttribute('class', `group-box ${state || ''}`); };
  V.setEdge = (id, state) => {
    const v = V.edges[id]; if (!v) return;
    v.state = state;
    v.path.setAttribute('class', `edge ${state}${v.e.cond ? ' cond' : ''}`);
    v.path.setAttribute('marker-end', `url(#ah-${state})`);
    v.path.setAttribute('stroke-width', state === 'active' ? V.ew + 1.3 : V.ew);
  };
  // pill: text on a rounded rect; anchor in node-local coordinates
  const pill = (parent, text, { x = 0, y = 0, fill = '#f4c95d', color = '#1a1304', size = 18, h = 28, padX = 10, anchor = 'middle', mono = false, stroke, icon } = {}) => {
    const w = textW(text, size, 700, mono ? MONO : FONT) + padX * 2 + (icon ? 22 : 0);
    const gx = anchor === 'middle' ? x - w / 2 : anchor === 'end' ? x - w : x;
    const g = el('g', { transform: `translate(${gx},${y - h / 2})` }, parent);
    el('rect', { x: 0, y: 0, width: w, height: h, rx: h / 2, fill, stroke: stroke || 'none', 'stroke-width': 2 }, g);
    if (icon === 'pause') { el('rect', { x: padX, y: h / 2 - 6, width: 4.5, height: 12, rx: 1.5, fill: color }, g); el('rect', { x: padX + 8, y: h / 2 - 6, width: 4.5, height: 12, rx: 1.5, fill: color }, g); }
    if (icon === 'play') el('path', { d: `M${padX},${h / 2 - 7} L${padX + 12},${h / 2} L${padX},${h / 2 + 7} z`, fill: color }, g);
    const t = el('text', { x: padX + (icon ? 22 : 0), y: h / 2 + 0.5, fill: color, 'font-size': size, class: '', 'font-weight': 700, 'dominant-baseline': 'central', style: mono ? `font-family:${MONO}` : '' }, g);
    t.textContent = text;
    return { g, w, h };
  };
  V.pill = pill;
  // badge at the top-right of a node (count, runs)
  V.badge = (id, key, text, o2 = {}) => {
    const v = V.nodes[id]; if (!v) return;
    if (v.badges[key]) { v.badges[key].g.remove(); delete v.badges[key]; }
    if (text === null || text === undefined || text === '') return;
    const slot = o2.slot || 'tr';
    const x = slot === 'tr' ? v.n.w / 2 - 6 : slot === 'tl' ? -v.n.w / 2 + 6 : 0;
    const y = slot === 'bl' || slot === 'br' ? v.n.h / 2 : -v.n.h / 2;
    const anchor = slot === 'tl' || slot === 'bl' ? 'start' : 'end';
    const wrap = el('g', { transform: `translate(${x},${y})` }, v.g);
    const p = pill(wrap, text, { anchor, size: o2.size || 18, h: o2.h || 28, fill: o2.fill || '#f4c95d', color: o2.color || '#1a1304', padX: o2.padX ?? 10, mono: o2.mono, icon: o2.icon });
    p.g.setAttribute('class', 'fade-in');
    v.badges[key] = { g: wrap };
    return p;
  };
  // tag above a node: 'waiting for a human' / Command(resume=...) etc. Lives in the overlay layer (graph coordinates)
  V.tag = (id, key, text, o2 = {}) => {
    const k = id + '|' + key;
    if (V.tags[k]) { V.tags[k].remove(); delete V.tags[k]; }
    if (text === null || text === undefined) return;
    const n = G.nodes.get(id);
    const g = el('g', { class: 'fade-in' }, gOver);
    const size = o2.size || 20, h = o2.h || 34;
    const w = textW(text, size, 700, o2.mono ? MONO : FONT) + 2 * (o2.padX ?? 14) + (o2.icon ? 24 : 0);
    const selfLoop = G.edges.some((e) => e.self && e.src === id);
    let cx = n.cx + (o2.dx || 0) + (selfLoop && !o2.below ? 12 - w / 2 : 0);
    cx = Math.max(w / 2 + 6, Math.min(G.size.W - w / 2 - 6, cx));
    let below = !!o2.below;
    let cy = below ? n.cy + n.h / 2 + 8 + h / 2 : n.cy - n.h / 2 - 12 - h / 2 + (o2.dy || 0);
    const hit = (yy) => [...G.nodes.values()].some((m) => m !== n && Math.abs(m.cx - cx) < m.w / 2 + w / 2 && Math.abs(m.cy - yy) < m.h / 2 + h / 2);
    if (!below && hit(cy)) { const alt = n.cy + n.h / 2 + 8 + h / 2; if (!hit(alt)) { cy = alt; below = true; cx = Math.max(w / 2 + 6, Math.min(G.size.W - w / 2 - 6, n.cx + n.w / 2 + 10 - w / 2)); } }
    const wrap = el('g', { transform: `translate(${cx},${cy})` }, g);
    pill(wrap, text, { x: 0, y: 0, fill: o2.fill || '#ffb938', color: o2.color || '#1a1204', size, h, mono: o2.mono, icon: o2.icon, stroke: o2.stroke, padX: o2.padX ?? 14 });
    V.tags[k] = g;
    return g;
  };
  V.edgeLabel = (id, text, o2 = {}) => {
    const v = V.edges[id]; if (!v) return;
    if (v.label) { v.label.remove(); v.label = null; }
    if (!text) return;
    const g = el('g', { class: 'fade-in' }, gOver);
    const m = v.e.mid;
    const size = o2.size || 17;
    const w = textW(text, size, 700) + 20;
    const vert = v.e.type === 'v';
    let px = m.x + (o2.dx !== undefined ? o2.dx : vert ? w / 2 + 14 : 0);
    if (px + w / 2 > G.size.W - 6) px = m.x - w / 2 - 14;
    px = Math.max(w / 2 + 6, Math.min(G.size.W - w / 2 - 6, px));
    const py = m.y + (o2.dy !== undefined ? o2.dy : vert ? 0 : -52);
    pill(g, text, { x: px, y: py, fill: o2.fill || '#f4c95d', color: o2.color || '#1a1304', size, h: o2.h || 28, mono: o2.mono });
    v.label = g;
  };
  // fan-out chips under a node
  V.setFan = (id, n, cols, o2 = {}) => {
    const v = V.nodes[id]; if (!v) return null;
    V.clearFan(id);
    const cs = O.chip, cg = O.chipGap;
    const rws = Math.ceil(n / cols);
    const labW = o2.rowLabels ? 76 : 0;
    const gw = cols * (cs + cg) - cg;
    const g = el('g', { transform: `translate(${v.n.cx - gw / 2 + (o2.dx || 0)},${v.n.cy + v.n.h / 2 + 16})`, class: 'fade-in' }, gOver);
    const chips = [];
    for (let i = 0; i < n; i++) {
      const r = Math.floor(i / cols), c = i % cols;
      chips.push(el('rect', { class: 'chip', x: c * (cs + cg), y: r * (cs + cg), width: cs, height: cs, rx: 5 }, g));
    }
    const rowLab = [];
    if (o2.rowLabels) for (let r = 0; r < rws; r++) {
      const t = el('text', { x: -10, y: r * (cs + cg) + cs / 2 + 1, fill: '#9aa7c0', 'font-size': 17, 'font-weight': 600, 'text-anchor': 'end', 'dominant-baseline': 'central' }, g);
      t.textContent = o2.rowLabels[r] || ''; rowLab.push(t);
    }
    const foot = el('text', { x: gw / 2, y: rws * (cs + cg) + 14, fill: '#9aa7c0', 'font-size': 18, 'font-weight': 600, 'text-anchor': 'middle', style: `font-family:${MONO}` }, g);
    const f = { g, chips, n, cols, foot, rowLab, done: 0 };
    V.fans[id] = f;
    return f;
  };
  V.clearFan = (id) => { const f = V.fans[id]; if (f) { f.g.remove(); delete V.fans[id]; } };
  V.chipState = (id, i, st) => { const f = V.fans[id]; if (f && f.chips[i]) f.chips[i].setAttribute('class', `chip ${st}`); };
  V.fanFoot = (id, text, gold) => { const f = V.fans[id]; if (f) { f.foot.textContent = text; f.foot.setAttribute('fill', gold ? '#f4c95d' : '#9aa7c0'); } };

  // travelling dot
  const dot = el('g', { opacity: 0 }, gOver);
  el('circle', { r: 15, fill: '#4aa8ff', opacity: 0.3 }, dot);
  el('circle', { r: 8.5, fill: '#bfe0ff', stroke: '#4aa8ff', 'stroke-width': 3 }, dot);
  V.travel = (edgeId, ms = 700) => new Promise((resolve) => {
    const v = V.edges[edgeId];
    if (!v) return resolve();
    const t0 = performance.now();
    dot.setAttribute('opacity', 1);
    const step = (now) => {
      let t = Math.min(1, (now - t0) / ms);
      const k = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      const p = v.path.getPointAtLength(v.len * k);
      dot.setAttribute('transform', `translate(${p.x},${p.y})`);
      if (t < 1) requestAnimationFrame(step);
      else { dot.setAttribute('opacity', 0); resolve(); }
    };
    requestAnimationFrame(step);
  });
  V.reset = () => {
    for (const id of Object.keys(V.nodes)) { V.setNode(id, 'idle'); for (const k of Object.keys(V.nodes[id].badges)) V.badge(id, k, null); }
    for (const id of Object.keys(V.edges)) { V.setEdge(id, 'idle'); V.edgeLabel(id, null); }
    for (const id of Object.keys(V.fans)) V.clearFan(id);
    for (const k of Object.keys(V.tags)) { V.tags[k].remove(); delete V.tags[k]; }
    for (const id of Object.keys(V.groups)) V.setGroup(id, '');
  };
  return V;
}

/** Per-graph layout hints (row splits keep subgraph boxes and loops tidy; everything else is automatic). */
export const HINTS = {
  performance: { rows: [6, 5] },
  writers_room: { rows: [5, 4], order: ['revise', 'pick_draft'] },
};
/** Convenience: fetch + normalise + layout for a graph id. */
export async function buildGraph(graphId, assistants, opt = {}, nopt = {}) {
  const a = assistants[graphId];
  const raw = await loadGraph(a.id);
  const G = normalizeGraph(raw, { id: graphId, ...nopt });
  layoutGraph(G, { ...(opt.hints === false ? {} : HINTS[graphId] || {}), ...opt });
  G.assistant = a;
  return G;
}
