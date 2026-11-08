// Layered (Sugiyama-lite) layout of a LangGraph graph JSON (`GET /assistants/{id}/graph?xray=true`). Pure.
//
// Input : {nodes:[{id,name?,type?,metadata?}], edges:[{source,target,conditional?,data?}]}
//         xray node ids look like "review:critique"; the prefix before ":" is the subgraph group.
// Output: {nodes:[{id,label,x,y,w,h,group,kind,count?}], edges:[{id,source,target,path,points,conditional,back,label}],
//          groups:[{id,label,x,y,w,h}], width, height}
// Collapsing a group replaces its inner nodes with one node (id = group) and re-targets crossing edges.

const START = '__start__';
const END = '__end__';

export function groupOfNode(id) {
  const i = String(id).lastIndexOf(':');
  return i === -1 ? null : String(id).slice(0, i);
}

/** All group ids present (including nested prefixes). */
export function groupsIn(graph) {
  const set = new Set();
  for (const n of graph.nodes || []) {
    let g = groupOfNode(n.id);
    while (g) {
      set.add(g);
      g = groupOfNode(g);
    }
  }
  return [...set];
}

function labelOf(n) {
  if (n.id === START) return 'start';
  if (n.id === END) return 'end';
  const s = String(n.name || n.id);
  const i = s.lastIndexOf(':');
  return i === -1 ? s : s.slice(i + 1);
}

/** Apply collapse: returns a new {nodes, edges} where collapsed groups become single nodes. */
export function collapseGroups(graph, collapsed) {
  const col = collapsed instanceof Set ? collapsed : new Set(collapsed || []);
  if (!col.size) return { nodes: graph.nodes || [], edges: graph.edges || [] };
  const rep = (id) => {
    // outermost collapsed ancestor group wins
    let best = null;
    let g = groupOfNode(id);
    while (g) {
      if (col.has(g)) best = g;
      g = groupOfNode(g);
    }
    return best || id;
  };
  const nodes = [];
  const seen = new Set();
  const counts = {};
  for (const n of graph.nodes || []) {
    const r = rep(n.id);
    if (r !== n.id) counts[r] = (counts[r] || 0) + 1;
    if (seen.has(r)) continue;
    seen.add(r);
    nodes.push(r === n.id ? n : { id: r, name: r, type: 'subgraph', collapsedGroup: true, metadata: n.metadata });
  }
  for (const n of nodes) if (counts[n.id]) n.count = counts[n.id];
  const edges = [];
  const eseen = new Set();
  for (const e of graph.edges || []) {
    const s = rep(e.source);
    const t = rep(e.target);
    if (s === t) continue;
    const k = `${s}->${t}`;
    if (eseen.has(k)) continue;
    eseen.add(k);
    edges.push({ ...e, source: s, target: t });
  }
  return { nodes, edges };
}

/**
 * @param {object} graph {nodes, edges}
 * @param {object} [opts] {collapsed:Set|string[], direction:'TB'|'LR', charW, nodeH, gapX, gapY}
 */
export function layoutGraph(graph, opts = {}) {
  const dir = opts.direction || 'TB';
  const charW = opts.charW ?? 7.2;
  const nodeH = opts.nodeH ?? 34;
  const gapX = opts.gapX ?? 28;
  const gapY = opts.gapY ?? 46;
  const margin = opts.margin ?? 24;
  const { nodes: rawNodes, edges: rawEdges } = collapseGroups(graph, opts.collapsed);

  const nodes = rawNodes.map((n) => {
    const label = labelOf(n);
    const kind = n.id === START ? 'start' : n.id === END ? 'end' : n.collapsedGroup || n.type === 'subgraph' ? 'subgraph' : 'node';
    const w = kind === 'start' || kind === 'end' ? 58 : Math.max(84, Math.round(label.length * charW + 34 + (n.count ? 34 : 0)));
    return { id: n.id, label, kind, w, h: nodeH, group: groupOfNode(n.id), count: n.count, metadata: n.metadata, raw: n };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edgesIn = rawEdges.filter((e) => byId.has(e.source) && byId.has(e.target));

  // adjacency
  const out = new Map(nodes.map((n) => [n.id, []]));
  for (const e of edgesIn) out.get(e.source).push(e.target);

  // 1) break cycles with a DFS from start (then any unvisited), marking back edges
  const color = new Map();
  const back = new Set();
  const order = [];
  const dfs = (u) => {
    color.set(u, 1);
    order.push(u);
    for (const v of out.get(u)) {
      const c = color.get(v) || 0;
      if (c === 1) back.add(`${u}->${v}`);
      else if (c === 0) dfs(v);
    }
    color.set(u, 2);
  };
  if (byId.has(START)) dfs(START);
  for (const n of nodes) if (!color.get(n.id)) dfs(n.id);

  // 2) longest-path layering on the DAG
  const layer = new Map();
  const dagOut = new Map(nodes.map((n) => [n.id, []]));
  const indeg = new Map(nodes.map((n) => [n.id, 0]));
  for (const e of edgesIn) {
    if (back.has(`${e.source}->${e.target}`) || e.source === e.target) continue;
    dagOut.get(e.source).push(e.target);
    indeg.set(e.target, indeg.get(e.target) + 1);
  }
  const queue = nodes.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  for (const id of queue) layer.set(id, 0);
  const deg = new Map(indeg);
  while (queue.length) {
    const u = queue.shift();
    for (const v of dagOut.get(u)) {
      layer.set(v, Math.max(layer.get(v) ?? 0, layer.get(u) + 1));
      deg.set(v, deg.get(v) - 1);
      if (deg.get(v) === 0) queue.push(v);
    }
  }
  for (const n of nodes) if (!layer.has(n.id)) layer.set(n.id, 0);
  if (byId.has(END)) {
    const maxL = Math.max(0, ...nodes.filter((n) => n.id !== END).map((n) => layer.get(n.id)));
    layer.set(END, maxL + 1);
  }

  // 3) dummy nodes for long edges
  const lanes = []; // lanes[layerIdx] = array of ids
  const ensure = (l) => {
    while (lanes.length <= l) lanes.push([]);
  };
  for (const n of nodes) {
    const l = layer.get(n.id);
    ensure(l);
    lanes[l].push(n.id);
  }
  const chains = []; // {edge, ids:[source,...dummies,target], back}
  let dummyN = 0;
  for (const e of edgesIn) {
    const isBack = back.has(`${e.source}->${e.target}`) || e.source === e.target;
    let s = e.source;
    let t = e.target;
    if (isBack) [s, t] = [t, s];
    const ls = layer.get(s);
    const lt = layer.get(t);
    const ids = [s];
    for (let l = ls + 1; l < lt; l++) {
      const id = `__d${dummyN++}`;
      layer.set(id, l);
      ensure(l);
      lanes[l].push(id);
      ids.push(id);
    }
    ids.push(t);
    chains.push({ edge: e, ids, back: isBack });
  }

  // 4) crossing reduction: barycenter sweeps
  const pos = new Map();
  const reindex = () => lanes.forEach((ids) => ids.forEach((id, i) => pos.set(id, i)));
  reindex();
  const neighbours = (id, up) => {
    const res = [];
    for (const c of chains) {
      const i = c.ids.indexOf(id);
      if (i === -1) continue;
      const j = up ? i - 1 : i + 1;
      if (j >= 0 && j < c.ids.length) res.push(c.ids[j]);
    }
    return res;
  };
  for (let sweep = 0; sweep < 6; sweep++) {
    const down = sweep % 2 === 0;
    const range = down ? lanes.map((_, i) => i).slice(1) : lanes.map((_, i) => i).slice(0, -1).reverse();
    for (const li of range) {
      const bary = new Map();
      for (const id of lanes[li]) {
        const nb = neighbours(id, down);
        bary.set(id, nb.length ? nb.reduce((a, x) => a + pos.get(x), 0) / nb.length : pos.get(id));
      }
      lanes[li].sort((a, b) => bary.get(a) - bary.get(b) || pos.get(a) - pos.get(b));
      lanes[li].forEach((id, i) => pos.set(id, i));
    }
  }

  // 5) coordinates
  const sizeOf = (id) => byId.get(id) || { w: 6, h: 6 };
  const along = dir === 'TB' ? 'x' : 'y';
  const rowThick = lanes.map((ids) => Math.max(0, ...ids.map((id) => (dir === 'TB' ? sizeOf(id).h : sizeOf(id).w))));
  const rowWidth = lanes.map((ids) =>
    ids.reduce((a, id) => a + (dir === 'TB' ? sizeOf(id).w : sizeOf(id).h) + gapX, -gapX),
  );
  const maxRow = Math.max(0, ...rowWidth);
  let cursor = margin;
  const centers = new Map(); // id -> {x,y} center
  lanes.forEach((ids, li) => {
    let a = margin + (maxRow - rowWidth[li]) / 2;
    const c = cursor + rowThick[li] / 2;
    for (const id of ids) {
      const s = sizeOf(id);
      const span = dir === 'TB' ? s.w : s.h;
      const mid = a + span / 2;
      centers.set(id, dir === 'TB' ? { x: mid, y: c } : { x: c, y: mid });
      a += span + gapX;
    }
    cursor += rowThick[li] + gapY;
  });
  void along;
  for (const n of nodes) {
    const c = centers.get(n.id);
    n.x = Math.round(c.x - n.w / 2);
    n.y = Math.round(c.y - n.h / 2);
  }

  // 6) edge routes
  const anchorOut = (n) => (dir === 'TB' ? { x: n.x + n.w / 2, y: n.y + n.h } : { x: n.x + n.w, y: n.y + n.h / 2 });
  const anchorIn = (n) => (dir === 'TB' ? { x: n.x + n.w / 2, y: n.y } : { x: n.x, y: n.y + n.h / 2 });
  const edges = chains.map((c, i) => {
    const sN = byId.get(c.ids[0]);
    const tN = byId.get(c.ids[c.ids.length - 1]);
    let pts = [anchorOut(sN)];
    for (const id of c.ids.slice(1, -1)) pts.push(centers.get(id));
    pts.push(anchorIn(tN));
    if (c.back) {
      pts = pts.reverse();
      // flip: the real direction is edge.source -> edge.target (target sits above source)
      const real = [anchorOut(byId.get(c.edge.source)), anchorIn(byId.get(c.edge.target))];
      const bulge = dir === 'TB' ? 46 : 46;
      const a = real[0];
      const b = real[1];
      const path =
        dir === 'TB'
          ? `M${r(a.x)} ${r(a.y)}C${r(a.x + bulge + 40)} ${r(a.y + 24)} ${r(b.x + bulge + 40)} ${r(b.y - 24)} ${r(b.x)} ${r(b.y)}`
          : `M${r(a.x)} ${r(a.y)}C${r(a.x + 24)} ${r(a.y + bulge + 40)} ${r(b.x - 24)} ${r(b.y + bulge + 40)} ${r(b.x)} ${r(b.y)}`;
      return { id: `e${i}`, source: c.edge.source, target: c.edge.target, path, points: real, conditional: !!c.edge.conditional, back: true, label: edgeLabel(c.edge) };
    }
    return {
      id: `e${i}`,
      source: c.edge.source,
      target: c.edge.target,
      path: smoothPath(pts, dir),
      points: pts,
      conditional: !!c.edge.conditional,
      back: false,
      label: edgeLabel(c.edge),
    };
  });

  // 7) group boxes around expanded groups
  const groups = [];
  const gmap = new Map();
  for (const n of nodes) {
    let g = n.group;
    while (g) {
      if (!gmap.has(g)) gmap.set(g, []);
      gmap.get(g).push(n);
      g = groupOfNode(g);
    }
  }
  for (const [id, members] of gmap) {
    const pad = 14 + 8 * (id.split(':').length - 1 === 0 ? 0 : 0);
    const x0 = Math.min(...members.map((m) => m.x)) - pad;
    const y0 = Math.min(...members.map((m) => m.y)) - pad - 12;
    const x1 = Math.max(...members.map((m) => m.x + m.w)) + pad;
    const y1 = Math.max(...members.map((m) => m.y + m.h)) + pad;
    groups.push({ id, label: id.split(':').pop(), depth: id.split(':').length, x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
  }
  groups.sort((a, b) => a.depth - b.depth);

  const allX = nodes.map((n) => n.x + n.w).concat(groups.map((g) => g.x + g.w));
  const allY = nodes.map((n) => n.y + n.h).concat(groups.map((g) => g.y + g.h));
  const minX = Math.min(0, ...groups.map((g) => g.x));
  return {
    nodes,
    edges,
    groups,
    width: Math.max(0, ...allX) + margin + (edges.some((e) => e.back) ? 90 : 0) - Math.min(0, minX),
    height: Math.max(0, ...allY) + margin,
    offsetX: -Math.min(0, minX),
    direction: dir,
  };
}

function edgeLabel(e) {
  const d = e.data;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && typeof d.label === 'string') return d.label;
  return e.label || '';
}

const r = (n) => Math.round(n * 10) / 10;

function smoothPath(pts, dir) {
  if (pts.length === 2) {
    const [a, b] = pts;
    if (dir === 'TB') {
      const my = (a.y + b.y) / 2;
      return `M${r(a.x)} ${r(a.y)}C${r(a.x)} ${r(my)} ${r(b.x)} ${r(my)} ${r(b.x)} ${r(b.y)}`;
    }
    const mx = (a.x + b.x) / 2;
    return `M${r(a.x)} ${r(a.y)}C${r(mx)} ${r(a.y)} ${r(mx)} ${r(b.y)} ${r(b.x)} ${r(b.y)}`;
  }
  // multi-point: Catmull-Rom-ish through the dummy centres
  let d = `M${r(pts[0].x)} ${r(pts[0].y)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1 = { x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6 };
    const c2 = { x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6 };
    d += `C${r(c1.x)} ${r(c1.y)} ${r(c2.x)} ${r(c2.y)} ${r(p2.x)} ${r(p2.y)}`;
  }
  return d;
}

/** Heuristic feature tags from a node's metadata/id (shown as small chips in the explorer). */
export function nodeTags(n) {
  const tags = new Set();
  const md = n.metadata || n.raw?.metadata || {};
  const hint = md.studio_hint || md.hint || md.tags;
  const list = Array.isArray(hint) ? hint : typeof hint === 'string' ? hint.split(/[,\s]+/) : [];
  for (const t of list) if (t) tags.add(String(t));
  const id = String(n.id);
  if (/approval|gate|review|interrupt|human|clarify/.test(id)) tags.add('interrupt');
  if (/tournament|fan|map|partition|analy[sz]e|cluster/.test(id)) tags.add('Send');
  if (n.kind === 'subgraph') tags.add('subgraph');
  if (/memory|recall|lesson|store/.test(id)) tags.add('store');
  return [...tags];
}
