// replay_layout.js: Foghorn-specific graph layout for the replay page.
//  * normalises an xray structure (subgraph nodes "parent:child" become labelled containers, __start__/__end__ become dots,
//    __error_handler__ nodes are dropped),
//  * layered left-to-right layout (optional per-graph column hints) with sized nodes (fan-out nodes reserve room for their
//    parallel instance pips), containers laid out recursively,
//  * edge routing: S-curves between neighbouring columns, rounded "lane" routes over/under long and back edges,
//    bidirectional hub edges merged, port spreading, label anchors.
// World units are CSS px at camera scale 1 (the page never draws text below 0.95 scale, so 16px world text stays >= 15px).

export const FS = 16;            // node label font size (world px)
export const MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
export const SANS = '-apple-system, "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif';
const NODE_H = 38, NODE_H_IN = 34;
const PIP = 14, PIP_GAP = 4;

let _ctx = null;
export function textW(str, size = FS, weight = 600, family = MONO) {
  if (!_ctx) _ctx = document.createElement('canvas').getContext('2d');
  _ctx.font = `${weight} ${size}px ${family}`;
  return _ctx.measureText(str).width;
}

// ------------------------------------------------------------------------------------------------ per-graph hints
// cols: columns left to right, each a list of top-level node ids, top to bottom. Unlisted nodes are placed automatically.
export const HINTS = {
  event_router: { cols: [['validate'], ['ledger'], ['enrich'], ['classify'], ['decide'], ['open_incident', 'join_incident', 'escalate_storm', 'ignore'], ['finish']] },
  incident_commander: {
    cols: [['bootstrap'], ['supervisor'],
      ['topology_analyst', 'change_analyst', 'metrics_analyst', 'logs_analyst', 'knowledge_agent', 'clarify_change_intent'],
      ['rca_tournament'], ['remediation_planner', 'comms_drafter'], ['policy_check'], ['approval_gate', 'comms_gate', 'answer_question'],
      ['execute_action'], ['verify_recovery'], ['close_incident']],
    ghost: true,
  },
  rca_tournament: { cols: [['gather'], ['plan'], ['score_hypothesis'], ['rank'], ['report']] },
  storm_correlator: { cols: [['partition'], ['analyze_candidate'], ['reduce_level', 'merge_group'], ['finalize'], ['open_cluster_incident'], ['report']] },
  change_gate: {
    cols: [['assess_risk'], ['policy_gate'], ['clarify', 'break_glass'], ['request_approval'],
      ['answer_question', 'edit_change', 'revise_change', 'four_eyes'], ['reviewer'], ['collect_reviews'], ['apply_change'], ['finish']],
    ghost: true,
  },
  memory_keeper: { cols: [['load_source', 'scan'], ['distill', 'cluster'], ['dedupe', 'confirm_merge'], ['curate', 'apply'], ['write'], ['finish']] },
  shift_handoff: { cols: [['collect_incidents', 'reconcile'], ['collect_approvals', 'render_report'], ['compute_metrics', 'publish']] },
  eval_runner: { cols: [['plan'], ['run_scenario'], ['aggregate']] },
  ops_copilot: {
    cols: [['PIIMiddleware[email].before_model', 'PIIMiddleware[credit_card].before_model', 'SummarizationMiddleware.before_model', 'ModelCallLimitMiddleware.before_model', 'model'],
      ['ToolReviewMiddleware.after_model', 'ToolCallLimitMiddleware.after_model', 'ModelCallLimitMiddleware.after_model', 'PIIMiddleware[credit_card].after_model', 'PIIMiddleware[email].after_model', 'tools']],
    ghost: true,
  },
};

// ------------------------------------------------------------------------------------------------ normalise structure
const isStartId = (id) => id === '__start__';
const isEndId = (id) => id === '__end__';

export function normalize(raw, fan = new Map()) {
  const nodes = new Map();   // id -> {id,label,kind,container,fan}
  const groups = new Map();  // gid -> {id, members:[], inner edges}
  const rawIds = new Set(raw.nodes.map((n) => n.id));
  for (const n of raw.nodes) {
    const id = n.id;
    if (isStartId(id)) { nodes.set(id, { id, kind: 'start', label: '', container: null }); continue; }
    if (isEndId(id)) { nodes.set(id, { id, kind: 'end', label: '', container: null }); continue; }
    if (/^__error_handler__/.test(id)) continue;
    const m = /^([^:]+):(.+)$/.exec(id);
    if (m) {
      const [, gid, inner] = m;
      if (inner === '__start__' || inner === '__end__') { if (!groups.has(gid)) groups.set(gid, { id: gid, members: [], edges: [] }); continue; }
      const g = groups.get(gid) || { id: gid, members: [], edges: [] };
      groups.set(gid, g);
      const hook = /^Analyst\.(before_model|after_agent)$/.test(inner);
      const label = hook ? inner.replace(/^Analyst\./, '') : inner;
      nodes.set(id, { id, kind: hook ? 'hook' : 'node', label, container: gid, fan: fan.get(id) || 1 });
      g.members.push(id);
      continue;
    }
    nodes.set(id, { id, kind: 'node', label: id, container: null, fan: fan.get(id) || 1 });
  }
  // top-level node that has the same name as a group becomes the container itself
  for (const gid of groups.keys()) nodes.set(gid, { id: gid, kind: 'container', label: gid, container: null, fan: 1 });

  const endpointOf = (id) => {
    const m = /^([^:]+):(.+)$/.exec(id);
    return m && groups.has(m[1]) ? m[1] : id;
  };
  const top = new Map();     // "a>b" -> edge
  const innerEdges = [];
  for (const e of raw.edges) {
    const s = e.source, t = e.target;
    const gs = /^([^:]+):/.exec(s), gt = /^([^:]+):/.exec(t);
    const sg = gs && groups.has(gs[1]) ? gs[1] : null, tg = gt && groups.has(gt[1]) ? gt[1] : null;
    if (sg && tg && sg === tg) {
      if (/:__start__$|:__end__$/.test(s) || /:__start__$|:__end__$/.test(t)) continue;
      if (!nodes.has(s) || !nodes.has(t)) continue;
      innerEdges.push({ src: s, dst: t, cond: !!e.conditional, label: typeof e.data === 'string' ? e.data : '', group: sg });
      continue;
    }
    const a = sg || s, b = tg || t;
    if (!nodes.has(a) || !nodes.has(b)) continue;
    const key = `${a}>${b}`;
    const ex = top.get(key);
    if (ex) { ex.cond = ex.cond && !!e.conditional; if (e.data && !ex.label) ex.label = String(e.data); }
    else top.set(key, { src: a, dst: b, cond: !!e.conditional, label: typeof e.data === 'string' ? e.data : '' });
  }
  // drop nodes without any edge (except real nodes of a functional graph)
  const used = new Set();
  for (const e of top.values()) { used.add(e.src); used.add(e.dst); }
  const real = [...nodes.values()].filter((n) => n.kind === 'node' || n.kind === 'container');
  const functional = real.length === 1;
  for (const n of [...nodes.values()]) {
    if (n.kind === 'start' || n.kind === 'end') { if (!used.has(n.id)) nodes.delete(n.id); }
    else if (!n.container && !used.has(n.id) && !functional) nodes.delete(n.id);
  }
  const edges = [...top.values()];
  // merge opposite pairs into one bidirectional edge
  const merged = [];
  const seen = new Set();
  for (const e of edges) {
    const k = `${e.src}>${e.dst}`;
    if (seen.has(k)) continue;
    const opp = top.get(`${e.dst}>${e.src}`);
    seen.add(k);
    if (opp) { seen.add(`${e.dst}>${e.src}`); merged.push({ ...e, bi: true, rev: { src: opp.src, dst: opp.dst }, cond: e.cond && opp.cond }); }
    else merged.push({ ...e, bi: false });
  }
  return { nodes, groups, edges: merged, innerEdges, functional };
}

// ------------------------------------------------------------------------------------------------ sizes
function sizeOf(n, inner = false) {
  if (n.kind === 'start' || n.kind === 'end') return { w: 20, h: 20 };
  if (n.kind === 'hook') return { w: 16, h: 16 };
  const tw = textW(n.label);
  let w = Math.max(64, Math.ceil(tw + 30)), h = inner ? NODE_H_IN : NODE_H;
  if (n.fan > 1) {
    const small = n.fan > 10;                       // many instances: small pips in a single row
    const ps = small ? 10 : PIP, pg = small ? 3 : PIP_GAP;
    const per = n.fan, rows = 1;
    const pw = per * ps + (per - 1) * pg;
    if (tw + 30 + 12 + pw <= 250) { w = Math.ceil(tw + 30 + 14 + pw); n.pipMode = 'inline'; }
    else { w = Math.max(w, pw + 28); h = (inner ? NODE_H_IN : NODE_H) + ps + 12; n.pipMode = 'below'; }
    n.pipSize = ps; n.pipGap = pg;
    n.pipW = pw; n.pipPer = per; n.pipRows = rows;
  }
  return { w, h };
}

// ------------------------------------------------------------------------------------------------ path helpers
function toD(segs, dx = 0, dy = 0) {
  return segs.map((s) => {
    const p = s.p.map((v, i) => +(v + (i % 2 === 0 ? dx : dy)).toFixed(1));
    return s.c + p.join(' ');
  }).join(' ');
}
function roundedPoly(pts, r = 12) {
  const segs = [{ c: 'M', p: [pts[0][0], pts[0][1]] }];
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1], [cx, cy] = pts[i], [nx, ny] = pts[i + 1];
    const l1 = Math.hypot(cx - px, cy - py), l2 = Math.hypot(nx - cx, ny - cy);
    const rr = Math.min(r, l1 / 2, l2 / 2);
    const ax = cx - ((cx - px) / (l1 || 1)) * rr, ay = cy - ((cy - py) / (l1 || 1)) * rr;
    const bx = cx + ((nx - cx) / (l2 || 1)) * rr, by = cy + ((ny - cy) / (l2 || 1)) * rr;
    segs.push({ c: 'L', p: [ax, ay] }, { c: 'Q', p: [cx, cy, bx, by] });
  }
  const last = pts[pts.length - 1];
  segs.push({ c: 'L', p: [last[0], last[1]] });
  return segs;
}
function endAngle(segs) {
  const s = segs[segs.length - 1], q = segs[segs.length - 2];
  const end = s.p.slice(-2);
  let from;
  if (s.c === 'C') from = [s.p[2], s.p[3]];
  else if (s.c === 'Q') from = [s.p[0], s.p[1]];
  else from = q.p.slice(-2);
  if (Math.hypot(end[0] - from[0], end[1] - from[1]) < 0.5) from = q.p.slice(-2);
  return Math.atan2(end[1] - from[1], end[0] - from[0]);
}
function startAngle(segs) {
  const m = segs[0].p, s = segs[1];
  let to;
  if (s.c === 'C') to = [s.p[0], s.p[1]]; else if (s.c === 'Q') to = [s.p[0], s.p[1]]; else to = s.p.slice(-2);
  return Math.atan2(to[1] - m[1], to[0] - m[0]);
}
function bez(p0, p1, p2, p3, t) {
  const u = 1 - t;
  return [u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
    u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]];
}

// ------------------------------------------------------------------------------------------------ core LR layout
/** items: [{id,w,h,kind}], edges: [{src,dst,cond,label,bi}], opts: {cols, gap, vgap, labelW(fn)}  ->  {boxes, routed edges, w, h} */
export function layoutLR(items, edges, opts = {}) {
  const gapBase = opts.gap ?? 56, vgap = opts.vgap ?? 22;
  const byId = new Map(items.map((n) => [n.id, { ...n }]));
  const ids = items.map((n) => n.id);
  // ---- columns
  let col = new Map();
  const hintCols = opts.cols ? opts.cols.map((c) => c.filter((id) => byId.has(id))).filter((c) => c.length) : null;
  if (hintCols) {
    hintCols.forEach((c, i) => c.forEach((id) => col.set(id, i)));
  }
  const startIds = ids.filter((id) => byId.get(id).kind === 'start');
  const endIds = ids.filter((id) => byId.get(id).kind === 'end');
  const fwd = edges.filter((e) => e.src !== e.dst);
  if (!hintCols) {
    // auto: DFS back-edge detection + longest path layering
    const out = new Map(ids.map((i) => [i, []]));
    for (const e of fwd) out.get(e.src)?.push(e);
    const st = new Map(), back = new Set();
    const dfs = (u) => { st.set(u, 1); for (const e of out.get(u) || []) { const s = st.get(e.dst); if (s === 1) back.add(e); else if (!s) dfs(e.dst); } st.set(u, 2); };
    for (const r of [...startIds, ...ids]) if (!st.get(r)) dfs(r);
    const layer = new Map(ids.map((i) => [i, 0]));
    const f2 = fwd.filter((e) => !back.has(e));
    for (let it = 0; it < ids.length + 2; it++) { let ch = false; for (const e of f2) { if (layer.get(e.dst) < layer.get(e.src) + 1) { layer.set(e.dst, layer.get(e.src) + 1); ch = true; } } if (!ch) break; }
    for (const id of ids) col.set(id, layer.get(id));
  } else {
    // place unlisted nodes after their predecessors
    const maxC = hintCols.length - 1;
    for (const id of ids) if (!col.has(id)) col.set(id, byId.get(id).kind === 'start' ? -1 : byId.get(id).kind === 'end' ? maxC + 1 : maxC + 1);
    // start dot: one column before the first node it points to
    for (const s of startIds) {
      const tg = fwd.filter((e) => e.src === s).map((e) => col.get(e.dst));
      col.set(s, tg.length ? Math.min(...tg) - 1 : -1);
    }
    for (const t of endIds) {
      const sc = fwd.filter((e) => e.dst === t).map((e) => col.get(e.src));
      col.set(t, sc.length ? Math.max(...sc) + 1 : maxC + 1);
    }
  }
  const minC = Math.min(...col.values());
  for (const id of ids) col.set(id, col.get(id) - minC);
  // orient merged (bidirectional) hub edges from the earlier column to the later one
  for (const e of edges) if (e.bi && col.get(e.src) > col.get(e.dst)) { const s = e.src; e.src = e.dst; e.dst = s; e.rev = { src: e.dst, dst: e.src }; }
  const nCols = Math.max(...col.values()) + 1;
  const cols = Array.from({ length: nCols }, () => []);
  for (const id of ids) cols[col.get(id)].push(id);
  // ---- order within columns
  if (hintCols) {
    const hindex = new Map();
    hintCols.forEach((c) => c.forEach((id, i) => hindex.set(id, i)));
    for (const c of cols) c.sort((a, b) => (hindex.get(a) ?? 99) - (hindex.get(b) ?? 99));
  } else {
    const pos = new Map();
    const reindex = () => cols.forEach((c) => c.forEach((id, i) => pos.set(id, i)));
    reindex();
    const nb = new Map(ids.map((i) => [i, []]));
    for (const e of fwd) { nb.get(e.src).push(e.dst); nb.get(e.dst).push(e.src); }
    for (let sweep = 0; sweep < 6; sweep++) {
      const order = sweep % 2 ? [...cols.keys()].reverse() : [...cols.keys()];
      for (const ci of order) {
        const bary = new Map();
        for (const id of cols[ci]) {
          const ns = nb.get(id).filter((n) => col.get(n) !== ci && (sweep % 2 ? col.get(n) > ci : col.get(n) < ci));
          bary.set(id, ns.length ? ns.reduce((s, n) => s + pos.get(n), 0) / ns.length : pos.get(id));
        }
        cols[ci].sort((a, b) => bary.get(a) - bary.get(b));
        reindex();
      }
    }
  }
  // ---- gaps between columns (room for edge labels)
  const gaps = Array(nCols).fill(gapBase);
  for (const e of fwd) {
    const dc = col.get(e.dst) - col.get(e.src);
    if (dc === 1 && e.label) gaps[col.get(e.src)] = Math.max(gaps[col.get(e.src)], Math.ceil(textW(e.label, FS, 600, SANS)) + 54);
  }
  const outDeg = new Map(), inDeg = new Map();
  for (const e of fwd) { outDeg.set(e.src, (outDeg.get(e.src) || 0) + 1); inDeg.set(e.dst, (inDeg.get(e.dst) || 0) + 1); }
  for (const e of fwd) {
    const dc = col.get(e.dst) - col.get(e.src);
    if (dc === 1 && ((outDeg.get(e.src) || 0) >= 4 || (inDeg.get(e.dst) || 0) >= 4)) gaps[col.get(e.src)] = Math.max(gaps[col.get(e.src)], gapBase + 22);
  }
  // ---- x
  const colW = cols.map((c) => Math.max(...c.map((id) => byId.get(id).w)));
  const colX = [];
  let x = 0;
  for (let i = 0; i < nCols; i++) { colX.push(x); x += colW[i] + (i < nCols - 1 ? gaps[i] : 0); }
  for (let i = 0; i < nCols; i++) for (const id of cols[i]) { const b = byId.get(id); b.col = i; b.x = colX[i] + (colW[i] - b.w) / 2; b.colX = colX[i]; b.colW = colW[i]; }
  // ---- y: stacked + centred, then neighbour-median relaxation
  const nbAll = new Map(ids.map((i) => [i, []]));
  for (const e of fwd) { nbAll.get(e.src).push(e.dst); nbAll.get(e.dst).push(e.src); }
  const cy = new Map();
  for (const c of cols) {
    const total = c.reduce((s, id) => s + byId.get(id).h, 0) + vgap * (c.length - 1);
    let y = -total / 2;
    for (const id of c) { const b = byId.get(id); cy.set(id, y + b.h / 2); y += b.h + vgap; }
  }
  const relax = (order, nbf) => {
    for (const ci of order) {
      const c = cols[ci];
      if (!c.length) continue;
      const want = c.map((id) => { const ns = nbf(id, ci); return ns.length ? ns.reduce((s, n) => s + cy.get(n), 0) / ns.length : cy.get(id); });
      const ys = [];
      for (let i = 0; i < c.length; i++) {
        const b = byId.get(c[i]);
        let y = want[i];
        if (i > 0) y = Math.max(y, ys[i - 1] + byId.get(c[i - 1]).h / 2 + vgap + b.h / 2);
        ys.push(y);
      }
      const shift = ys.reduce((s, v, i) => s + (want[i] - v), 0) / ys.length;
      c.forEach((id, i) => cy.set(id, ys[i] + shift));
    }
  };
  const idx = [...cols.keys()];
  if (!opts.noRelax) {
    for (let it = 0; it < 6; it++) {
      relax(idx, (id, ci) => nbAll.get(id).filter((n) => col.get(n) < ci).filter((n) => fwd.some((e) => e.src === n && e.dst === id)));
      relax([...idx].reverse(), (id, ci) => nbAll.get(id).filter((n) => col.get(n) > ci).filter((n) => fwd.some((e) => e.src === id && e.dst === n)));
    }
  }
  for (const id of ids) { const b = byId.get(id); b.y = cy.get(id) - b.h / 2; b.cx = b.x + b.w / 2; b.cy = cy.get(id); }
  // ---- route edges
  const boxes = byId;
  const routed = routeEdges(fwd.concat(edges.filter((e) => e.src === e.dst)), boxes, cols, colX, colW, opts.laneBase ?? 16, opts.laneStep ?? 11);
  // ---- bounds + normalise to (0,0)
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const b of boxes.values()) { x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.w); y1 = Math.max(y1, b.y + b.h); }
  for (const r of routed) { x0 = Math.min(x0, r.bb[0]); y0 = Math.min(y0, r.bb[1]); x1 = Math.max(x1, r.bb[2]); y1 = Math.max(y1, r.bb[3]); }
  return { boxes, routed, bounds: { x0, y0, x1, y1 }, w: x1 - x0, h: y1 - y0 };
}

// ------------------------------------------------------------------------------------------------ edge routing
function routeEdges(edges, boxes, cols, colX, colW, laneBase = 16, laneStep = 11) {
  const routed = [];
  const topLanes = [], botLanes = []; // arrays of [x0,x1] intervals per level
  const colTop = cols.map((c) => Math.min(...c.map((id) => boxes.get(id).y)));
  const colBot = cols.map((c) => Math.max(...c.map((id) => boxes.get(id).y + boxes.get(id).h)));
  const offCount = new Map();
  const laneOff = (key) => { const n = offCount.get(key) || 0; offCount.set(key, n + 1); return [0, -9, 9, -18, 18, -27, 27][n] ?? 0; };
  const laneFree = (lanes, lvl, x0, x1) => !(lanes[lvl] || []).some(([a, b]) => !(x1 + 10 < a || x0 - 10 > b));
  const takeLane = (lanes, x0, x1) => { let lvl = 0; while (!laneFree(lanes, lvl, x0, x1)) lvl++; (lanes[lvl] = lanes[lvl] || []).push([x0, x1]); return lvl; };
  // port spreading: out ports on right side, in ports on left side
  const outs = new Map(), ins = new Map();
  for (const e of edges) {
    if (e.src === e.dst) continue;
    const u = boxes.get(e.src), v = boxes.get(e.dst);
    if (v.col > u.col) { (outs.get(e.src) || outs.set(e.src, []).get(e.src)).push(e); (ins.get(e.dst) || ins.set(e.dst, []).get(e.dst)).push(e); }
  }
  const portY = new Map();
  for (const [id, list] of outs) { const b = boxes.get(id); list.sort((p, q) => boxes.get(p.dst).cy - boxes.get(q.dst).cy); const step = Math.min(8, (b.h - 12) / Math.max(1, list.length - 1)); list.forEach((e, i) => portY.set(`o|${e.src}>${e.dst}`, b.cy + (i - (list.length - 1) / 2) * (list.length > 1 ? step : 0))); }
  for (const [id, list] of ins) { const b = boxes.get(id); list.sort((p, q) => boxes.get(p.src).cy - boxes.get(q.src).cy); const step = Math.min(8, (b.h - 12) / Math.max(1, list.length - 1)); list.forEach((e, i) => portY.set(`i|${e.src}>${e.dst}`, b.cy + (i - (list.length - 1) / 2) * (list.length > 1 ? step : 0))); }

  // longer edges first so they take the outer lanes... shorter ones nearer the nodes
  const order = edges.map((e, i) => ({ e, i, span: Math.abs((boxes.get(e.dst)?.col ?? 0) - (boxes.get(e.src)?.col ?? 0)) })).sort((p, q) => p.span - q.span || p.i - q.i);
  for (const { e } of order) {
    const u = boxes.get(e.src), v = boxes.get(e.dst);
    const r = { edge: e, segs: null, label: null, bb: [0, 0, 0, 0], kind: 'curve', ghost: false };
    const dc = v.col - u.col;
    if (e.src === e.dst) {
      const x = u.cx, y = u.y;
      r.segs = [{ c: 'M', p: [x - 14, y] }, { c: 'C', p: [x - 22, y - 30, x + 22, y - 30, x + 14, y] }];
      r.kind = 'self'; r.ghost = false;
    } else if (dc >= 1) {
      const sy = portY.get(`o|${e.src}>${e.dst}`) ?? u.cy, ty = portY.get(`i|${e.src}>${e.dst}`) ?? v.cy;
      const x1 = u.x + u.w, x2 = v.x;
      let useLane = false;
      if (dc >= 2) {
        const k = Math.max(30, (x2 - x1) * 0.5);
        const p0 = [x1, sy], p1 = [x1 + k, sy], p2 = [x2 - k, ty], p3 = [x2, ty];
        for (let s = 1; s < 24 && !useLane; s++) {
          const [px, py] = bez(p0, p1, p2, p3, s / 24);
          for (let c = u.col + 1; c < v.col && !useLane; c++) for (const id of cols[c]) { const b = boxes.get(id); if (px > b.x - 8 && px < b.x + b.w + 8 && py > b.y - 8 && py < b.y + b.h + 8) { useLane = true; break; } }
        }
      }
      if (!useLane) {
        const k = Math.max(26, (x2 - x1) * 0.5);
        r.segs = [{ c: 'M', p: [x1, sy] }, { c: 'C', p: [x1 + k, sy, x2 - k, ty, x2, ty] }];
        if (e.label) {
          const lw = Math.ceil(textW(e.label, 16, 600, SANS)) + 16;
          const want = x2 - 20 - lw / 2;
          let lo = 0, hi = 1;
          for (let q = 0; q < 24; q++) { const mid = (lo + hi) / 2; if (bez([x1, sy], [x1 + k, sy], [x2 - k, ty], [x2, ty], mid)[0] < want) lo = mid; else hi = mid; }
          const [lx, ly] = bez([x1, sy], [x1 + k, sy], [x2 - k, ty], [x2, ty], (lo + hi) / 2);
          r.label = { x: lx, y: ly };
        } else {
          const [lx, ly] = bez([x1, sy], [x1 + k, sy], [x2 - k, ty], [x2, ty], 0.5);
          r.label = { x: lx, y: ly };
        }
      } else {
        // lane over or under the intermediate columns; leave the source from its top/bottom and enter the target from above/below
        let top = Infinity, bot = -Infinity;
        for (let c = u.col; c <= v.col; c++) { top = Math.min(top, colTop[c]); bot = Math.max(bot, colBot[c]); }
        const lx0 = u.cx, lx1 = v.cx;
        const lt = takeLaneProbe(topLanes, lx0, lx1), lb = takeLaneProbe(botLanes, lx0, lx1);
        const yTop = top - laneBase - lt * laneStep, yBot = bot + laneBase + lb * laneStep;
        const costTop = Math.abs(u.y - yTop) + Math.abs(v.y - yTop) + lt * 20, costBot = Math.abs(u.y + u.h - yBot) + Math.abs(v.y + v.h - yBot) + lb * 20;
        const goTop = costTop <= costBot;
        const ly = goTop ? yTop : yBot;
        takeLane(goTop ? topLanes : botLanes, lx0, lx1);
        const side = goTop ? 't' : 'b';
        const so = laneOff(u.id + side + 'o'), to = laneOff(v.id + side + 'i');
        const sx = u.cx + so, tx = v.cx + to;
        const sy2 = goTop ? u.y : u.y + u.h, ty2 = goTop ? v.y : v.y + v.h;
        r.segs = roundedPoly([[sx, sy2], [sx, ly], [tx, ly], [tx, ty2]], 12);
        r.kind = 'lane'; r.ghost = true;
        r.label = { x: (sx + tx) / 2, y: ly };
      }
    } else if (dc <= -1) {
      // back edge: over or under, entering the target from its top/bottom
      let top = Infinity, bot = -Infinity;
      for (let c = v.col; c <= u.col; c++) { top = Math.min(top, colTop[c]); bot = Math.max(bot, colBot[c]); }
      const sideP = (Math.abs(u.y - (top - 16)) <= Math.abs(u.y + u.h - (bot + 16))) ? 't' : 'b';
      const ux = u.cx + laneOff(u.id + sideP + 'o'), vx = v.cx + laneOff(v.id + sideP + 'i');
      const lx0 = Math.min(ux, vx), lx1 = Math.max(ux, vx);
      const lt = takeLaneProbe(topLanes, lx0, lx1), lb = takeLaneProbe(botLanes, lx0, lx1);
      const yTop = top - laneBase - lt * laneStep, yBot = bot + laneBase + lb * laneStep;
      const goTop = Math.abs(u.y - yTop) + Math.abs(v.y - yTop) + lt * 20 <= Math.abs(u.y + u.h - yBot) + Math.abs(v.y + v.h - yBot) + lb * 20;
      const ly = goTop ? yTop : yBot;
      takeLane(goTop ? topLanes : botLanes, lx0, lx1);
      const sy = goTop ? u.y : u.y + u.h, ty = goTop ? v.y : v.y + v.h;
      // offset the vertical drops a little so merged hub edges don't sit on top of each other
      r.segs = roundedPoly([[ux, sy], [ux, ly], [vx, ly], [vx, ty]], 12);
      r.kind = 'lane'; r.ghost = true; r.back = true;
      r.label = { x: (ux + vx) / 2, y: ly };
    } else {
      // same column
      const below = v.cy > u.cy;
      const gapY = below ? v.y - (u.y + u.h) : u.y - (v.y + v.h);
      const adjacent = gapY < 60;
      if (adjacent) {
        const x = (Math.max(u.x, v.x) + Math.min(u.x + u.w, v.x + v.w)) / 2;
        const y1 = below ? u.y + u.h : u.y, y2 = below ? v.y : v.y + v.h;
        r.segs = [{ c: 'M', p: [x, y1] }, { c: 'L', p: [x, y2] }];
        r.label = { x, y: (y1 + y2) / 2 };
      } else {
        const rx = Math.max(u.x + u.w, v.x + v.w) + 24;
        r.segs = roundedPoly([[u.x + u.w, u.cy], [rx, u.cy], [rx, v.cy], [v.x + v.w, v.cy]], 12);
        r.kind = 'lane'; r.ghost = true;
        r.label = { x: rx, y: (u.cy + v.cy) / 2 };
      }
    }
    // bounding box (loose)
    let bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
    for (const s of r.segs) for (let i = 0; i < s.p.length; i += 2) { bx0 = Math.min(bx0, s.p[i]); bx1 = Math.max(bx1, s.p[i]); by0 = Math.min(by0, s.p[i + 1]); by1 = Math.max(by1, s.p[i + 1]); }
    r.bb = [bx0 - 2, by0 - 2, bx1 + 2, by1 + 2];
    r.endAng = endAngle(r.segs); r.startAng = r.segs.length > 1 ? startAngle(r.segs) : 0;
    r.end = r.segs[r.segs.length - 1].p.slice(-2);
    r.start = r.segs[0].p.slice(0, 2);
    routed.push(r);
  }
  return routed;
}
function takeLaneProbe(lanes, x0, x1) {
  let lvl = 0;
  while ((lanes[lvl] || []).some(([a, b]) => !(x1 + 10 < a || x0 - 10 > b))) lvl++;
  return lvl;
}

// ------------------------------------------------------------------------------------------------ full graph layout
/**
 * Returns {nodes: Map(id -> box), containers: Map, edges:[{id,src,dst,d,label,labelPos,...}], bounds, functional}
 * `fan`: Map nodeId -> max parallel instances (from the replayed run), used to size nodes.
 */
export function layoutGraph(structure, fan, graphId, extra = {}) {
  const norm = normalize(structure.graph, fan);
  const hint = HINTS[graphId] || {};
  const out = { nodes: new Map(), containers: new Map(), edges: [], bounds: null, functional: norm.functional, graphId, hint };
  // ---- inner layouts of containers
  const inner = new Map();
  for (const g of norm.groups.values()) {
    const mem = g.members.map((id) => norm.nodes.get(id));
    for (const m of mem) Object.assign(m, sizeOf(m, true), { id: m.id });
    const items = mem.map((m) => ({ id: m.id, w: m.w, h: m.h, kind: m.kind, label: m.label, fan: m.fan, pipMode: m.pipMode, pipW: m.pipW, pipPer: m.pipPer, pipRows: m.pipRows, pipSize: m.pipSize, pipGap: m.pipGap }));
    const edges = norm.innerEdges.filter((e) => e.group === g.id).map((e) => ({ ...e, bi: false }));
    let cols = null;
    const pre = (s) => `${g.id}:${s}`;
    const hasHook = mem.some((m) => m.kind === 'hook');
    const hide = new Set();
    if (hasHook) {
      cols = [[pre('Analyst.before_model')], [pre('model')], [pre('tools')], [pre('Analyst.after_agent')]];
      hide.add(`${pre('model')}>${pre('Analyst.before_model')}`);
    } else {
      const h = HINTS[g.id];
      const sub = {
        rca_tournament: [['gather'], ['plan'], ['score_hypothesis'], ['rank']],
      }[g.id];
      if (sub) cols = sub.map((c) => c.map(pre));
      else if (h && h.cols) cols = h.cols.map((c) => c.map(pre));
    }
    const es = edges.filter((e) => !hide.has(`${e.src}>${e.dst}`));
    const lay = layoutLR(items, es, { cols, gap: hasHook ? 24 : 40, vgap: 14, laneBase: 11, laneStep: 9 });
    inner.set(g.id, { lay, hidden: edges.filter((e) => hide.has(`${e.src}>${e.dst}`)) });
    const padX = 12, padY = 6;
    const b = lay.bounds;
    const titleW = textW(g.id, FS, 700);
    const w = padX + titleW + 16 + lay.w + padX, h = lay.h + 2 * padY;
    const cn = norm.nodes.get(g.id);
    cn.w = Math.ceil(w); cn.h = Math.ceil(h); cn.kind = 'container';
    cn.inner = { dx: padX + titleW + 16 - b.x0, dy: padY - b.y0, hdr: 0, titleX: padX };
  }
  // ---- top-level
  const items = [];
  for (const n of norm.nodes.values()) {
    if (n.container) continue;
    if (n.kind !== 'container') Object.assign(n, sizeOf(n));
    items.push({ id: n.id, w: n.w, h: n.h, kind: n.kind, label: n.label });
  }
  let lay;
  if (norm.functional) {
    lay = layoutFunctional(norm, items, fan, extra.fnH);
  } else {
    lay = layoutLR(items, norm.edges, { cols: hint.cols, gap: 58, vgap: hint.vgap ?? 14 });
  }
  const b0 = lay.bounds;
  const ox = -b0.x0, oy = -b0.y0;
  for (const [id, b] of lay.boxes) {
    const n = norm.nodes.get(id);
    const box = { id, kind: n.kind, label: n.label, fan: n.fan || 1, pipMode: n.pipMode, pipW: n.pipW, pipPer: n.pipPer, pipRows: n.pipRows, pipSize: n.pipSize, pipGap: n.pipGap, x: b.x + ox, y: b.y + oy, w: b.w, h: b.h, container: null };
    box.cx = box.x + box.w / 2; box.cy = box.y + box.h / 2;
    out.nodes.set(id, box);
    if (n.kind === 'container') {
      const ci = n.inner, il = inner.get(id).lay;
      const c = { id, label: id, x: box.x, y: box.y, w: box.w, h: box.h, hdr: ci.hdr, titleX: ci.titleX, members: [] };
      out.containers.set(id, c);
      for (const [mid, mb] of il.boxes) {
        const m = norm.nodes.get(mid);
        const x = mb.x + ci.dx + c.x, y = mb.y + ci.dy + c.y;
        const nb = { id: mid, kind: m.kind, label: m.label, fan: m.fan || 1, pipMode: m.pipMode, pipW: m.pipW, pipPer: m.pipPer, pipRows: m.pipRows, pipSize: m.pipSize, pipGap: m.pipGap, x, y, w: mb.w, h: mb.h, container: id };
        nb.cx = nb.x + nb.w / 2; nb.cy = nb.y + nb.h / 2;
        out.nodes.set(mid, nb); c.members.push(mid);
      }
      for (const r of il.routed) {
        out.edges.push(makeEdge(r, c.x + ci.dx, c.y + ci.dy, id, norm));
      }
    }
  }
  for (const r of lay.routed) out.edges.push(makeEdge(r, ox, oy, null, norm, hint));
  // bounds
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const b of out.nodes.values()) { x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.w); y1 = Math.max(y1, b.y + b.h); }
  for (const e of out.edges) { x0 = Math.min(x0, e.bb[0]); y0 = Math.min(y0, e.bb[1]); x1 = Math.max(x1, e.bb[2]); y1 = Math.max(y1, e.bb[3]); }
  out.bounds = { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
  out.norm = norm;
  out.extra = lay.extra || null;
  return out;
}

function makeEdge(r, dx, dy, container, norm, hint = {}) {
  const e = r.edge;
  const id = `${e.src}>${e.dst}`;
  const ghost = !!hint.ghost && e.cond && r.kind === 'lane';
  return {
    id, src: e.src, dst: e.dst, bi: !!e.bi, rev: e.rev || null, cond: !!e.cond, label: e.label || '', kind: r.kind, ghost,
    d: toD(r.segs, dx, dy),
    labelPos: r.label ? { x: r.label.x + dx, y: r.label.y + dy } : null,
    end: { x: r.end[0] + dx, y: r.end[1] + dy, ang: r.endAng },
    start: { x: r.start[0] + dx, y: r.start[1] + dy, ang: r.startAng },
    bb: [r.bb[0] + dx, r.bb[1] + dy, r.bb[2] + dx, r.bb[3] + dy],
    container,
  };
}

// ------------------------------------------------------------------------------------------------ Functional API graph
function layoutFunctional(norm, items, fan, fnH) {
  const entry = items.find((i) => i.kind === 'container' || i.kind === 'node');
  const start = items.find((i) => i.kind === 'start'), end = items.find((i) => i.kind === 'end');
  const boxes = new Map();
  const W = 880, H = fnH || 190;
  const ent = { ...entry, w: W, h: H, x: 0, y: 70, col: 1 };
  ent.cx = ent.x + W / 2; ent.cy = ent.y + H / 2;
  boxes.set(entry.id, ent);
  const routed = [];
  if (start) { const s = { ...start, x: W / 2 - 10, y: 0, col: 0 }; s.cx = s.x + 10; s.cy = s.y + 10; boxes.set(start.id, s);
    routed.push(fakeRoute({ src: start.id, dst: entry.id, cond: false }, [[s.cx, s.y + 20], [s.cx, ent.y]])); }
  if (end) { const t = { ...end, x: W / 2 - 10, y: ent.y + H + 50, col: 2 }; t.cx = t.x + 10; t.cy = t.y + 10; boxes.set(end.id, t);
    routed.push(fakeRoute({ src: entry.id, dst: end.id, cond: true }, [[t.cx, ent.y + H], [t.cx, t.y]])); }
  let x0 = 0, y0 = 0, x1 = W, y1 = (end ? ent.y + H + 50 + 20 : ent.y + H);
  return { boxes, routed, bounds: { x0, y0, x1, y1 }, w: x1, h: y1, extra: { entry: entry.id } };
}
function fakeRoute(e, pts) {
  const segs = [{ c: 'M', p: pts[0] }, { c: 'L', p: pts[1] }];
  return { edge: e, segs, label: null, bb: [Math.min(pts[0][0], pts[1][0]) - 2, Math.min(pts[0][1], pts[1][1]), Math.max(pts[0][0], pts[1][0]) + 2, Math.max(pts[0][1], pts[1][1])], kind: 'curve', ghost: false, endAng: Math.PI / 2, startAng: Math.PI / 2, end: pts[1], start: pts[0] };
}
