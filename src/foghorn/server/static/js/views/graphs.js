// Graph Explorer: structure (SVG layered diagram with subgraph expansion), schemas, config fields, assistants,
// run-a-sample, recent threads. Reads the built-in Agent Server API; the catalog/guide come from /api/*.
import { h, mount, svg } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, copyButton, emptyState, errorBox, errorMessage, field, jsonViewer, loadInto, markdown, notice, seg, selectEl, skeleton, skeletonBlock, skeletonGrid, statusBadge, table, tabs, textareaEl, viewHead, asyncClick, switchEl } from '../core/ui.js';
import { layoutGraph, groupsIn, nodeTags, groupOfNode } from '../core/graphlayout.js';
import { link } from '../core/router.js';
import { safeJson, shortId, truncate } from '../core/format.js';
import { ago } from './shared.js';

export default async function graphs(root, route, app) {
  let selected = route.params.id || null;
  const listEl = h('div', { class: 'col' });
  const detailEl = h('div', { class: 'col gap-4' });
  const disposers = [];
  let catalog = [];
  let guide = null;
  let assistants = [];

  mount(root, viewHead({ title: 'Graph explorer', sub: 'All ten graphs in this deployment, read live from the Agent Server. Editing happens in Studio; everything here is read-only except Run sample.' }), h('div', { class: 'split graphs-split' }, h('aside', { class: 'card split-list', 'aria-label': 'Graphs' }, h('div', { class: 'card-body flush scroll', style: 'max-height:calc(100vh - 170px)' }, listEl)), h('section', { class: 'split-detail', 'aria-label': 'Graph detail' }, detailEl)));
  mount(listEl, skeleton(8));

  try {
    const [cat, g, asst] = await Promise.all([app.api.graphs().catch(() => null), app.api.guide().catch(() => null), app.api.lgAssistants().catch(() => null)]);
    guide = g;
    assistants = Array.isArray(asst) ? asst : asst?.assistants || [];
    catalog = cat?.items?.length ? cat.items : uniqueGraphs(assistants);
    if (!catalog.length) throw new Error('No graphs reported by the server');
  } catch (err) {
    mount(listEl, errorBox(err, () => location.reload(), 'Cannot load graphs'));
    return;
  }
  if (!selected && window.innerWidth > 900) selected = catalog.find((c) => c.graph_id === 'incident_commander')?.graph_id || catalog[0].graph_id;

  function renderList() {
    mount(listEl, catalog.map((g) => h('a', { class: ['graph-row', g.graph_id === selected && 'selected'], href: link('graphs', g.graph_id), 'aria-current': g.graph_id === selected ? 'page' : undefined },
      h('div', { class: 'row between' }, h('span', { class: 'mono strong' }, g.graph_id), (g.assistants || []).length > 1 && badge(`${g.assistants.length} assistants`, 'info', { sq: true })),
      h('div', { class: 'small muted' }, truncate(g.role || g.description || g.title || '', 90)),
    )));
  }
  renderList();
  if (selected) await renderDetail(selected);
  else mount(detailEl, emptyState({ icon: 'graph', title: 'Choose a graph' }));

  function assistantIdFor(graphId) {
    const fromCat = catalog.find((c) => c.graph_id === graphId)?.assistants?.[0]?.assistant_id;
    const sys = assistants.find((a) => a.graph_id === graphId && a.metadata?.created_by === 'system');
    return sys?.assistant_id || fromCat || assistants.find((a) => a.graph_id === graphId)?.assistant_id || graphId;
  }

  async function renderDetail(graphId) {
    const g = catalog.find((c) => c.graph_id === graphId);
    if (!g) {
      mount(detailEl, emptyState({ icon: 'graph', title: 'Unknown graph', text: graphId }));
      return;
    }
    const gdoc = guide?.graphs?.find((x) => x.graph_id === graphId);
    let tab = route.query.tab || 'structure';
    const body = h('div');
    const tabsEl = tabs([{ key: 'structure', label: 'Structure' }, { key: 'schemas', label: 'Schemas' }, { key: 'config', label: 'Config fields' }, { key: 'run', label: 'Run sample' }, { key: 'assistants', label: 'Assistants' }, { key: 'threads', label: 'Recent threads' }], tab, (k) => { tab = k; drawTab(); }, { label: 'Graph sections' });
    mount(
      detailEl,
      card({
        title: h('span', { class: 'row' }, h('span', { class: 'mono' }, graphId), g.title && h('span', { class: 'muted small' }, g.title)),
        actions: [button('Open in Studio', { size: 'sm', icon: 'external', href: app.studioGraph(graphId), external: true }), gdoc && button('Studio guide', { size: 'sm', icon: 'book', href: link('guide', graphId) })],
        body: h('div', { class: 'col gap-3' }, h('p', { class: 'muted', style: 'margin:0' }, g.role || g.description || gdoc?.summary || ''), (g.features || gdoc?.features || []).length > 0 && h('div', { class: 'row wrap' }, (g.features || gdoc?.features).map((f) => chip(f)))),
      }),
      tabsEl,
      body,
    );
    const aid = assistantIdFor(graphId);
    const cache = {};
    async function schemas() {
      if (!cache.schemas) cache.schemas = await app.api.lgSchemas(aid);
      return cache.schemas;
    }
    function drawTab() {
      document.title = `${graphId} - Graphs - Foghorn`;
      if (tab === 'structure') structureTab(body, app, graphId, aid, cache);
      else if (tab === 'schemas') loadInto(body, schemas, (s) => schemasView(s), { skeleton: () => skeletonBlock(220) });
      else if (tab === 'config') loadInto(body, schemas, (s) => configView(s, graphId), { skeleton: () => skeletonBlock(220) });
      else if (tab === 'run') runTab(body, app, graphId, gdoc);
      else if (tab === 'assistants') assistantsTab(body, app, graphId, g);
      else threadsTab(body, app, graphId);
    }
    drawTab();
  }
  return () => disposers.forEach((d) => d());
}

function uniqueGraphs(assistants) {
  const m = new Map();
  for (const a of assistants) {
    if (!m.has(a.graph_id)) m.set(a.graph_id, { graph_id: a.graph_id, title: a.name, role: a.description, assistants: [] });
    m.get(a.graph_id).assistants.push({ assistant_id: a.assistant_id, name: a.name, version: a.version });
  }
  return [...m.values()];
}

// ---- structure ---------------------------------------------------------------------------------------------------
function structureTab(host, app, graphId, aid, cache) {
  const state = { xray: true, direction: 'TB', collapsed: new Set(), scale: 1, selectedNode: null };
  const svgHost = h('div', { class: 'graph-canvas', tabindex: '0', role: 'group', 'aria-label': `${graphId} graph diagram` });
  const info = h('div', { class: 'graph-info' });
  const toolbar = h('div', { class: 'toolbar mb-3' });
  let groupIds = [];
  let raw = null;

  const draw = () => {
    if (!raw) return;
    const layout = layoutGraph(raw, { collapsed: state.collapsed, direction: state.direction });
    if (!state.fitted) {
      // fit wide graphs to the canvas on first draw (never below 62% so labels stay readable)
      const avail = (svgHost.clientWidth || 700) - 12;
      state.scale = Math.max(0.62, Math.min(1, avail / layout.width));
      state.fitted = true;
    }
    mount(svgHost, renderSvg(layout, state, (n) => { state.selectedNode = n.id; showNode(n); draw(); }, (gid) => { if (state.collapsed.has(gid)) state.collapsed.delete(gid); else state.collapsed.add(gid); draw(); }));
    svgHost.firstChild.style.width = layout.width * state.scale + 'px';
    svgHost.firstChild.style.height = layout.height * state.scale + 'px';
    drawToolbar();
  };
  const showNode = (n) => {
    mount(info, card({ title: h('span', { class: 'mono' }, n.id), body: h('div', { class: 'col gap-2 small' }, h('div', { class: 'row wrap' }, n.kind !== 'node' && badge(n.kind, 'info', { sq: true }), nodeTags(n).map((t) => badge(t, 'accent', { sq: true }))), n.metadata && Object.keys(n.metadata).length ? jsonViewer(n.metadata, { open: 1 }) : h('span', { class: 'muted' }, 'No node metadata.'), n.kind === 'subgraph' && h('div', { class: 'muted' }, `Contains ${n.count || 'several'} inner nodes. Double-click or use Expand to open it.`)) }));
  };
  const drawToolbar = () => {
    mount(
      toolbar,
      seg([{ value: 'TB', label: 'Top-down' }, { value: 'LR', label: 'Left-right' }], state.direction, (v) => { state.direction = v; draw(); }, { label: 'Direction' }),
      groupIds.length > 0 && button(state.collapsed.size ? 'Expand all subgraphs' : 'Collapse subgraphs', { size: 'sm', icon: 'layers', onclick: () => { if (state.collapsed.size) state.collapsed.clear(); else groupIds.forEach((g) => state.collapsed.add(g)); draw(); } }),
      button('Fit', { size: 'sm', title: 'Fit the diagram to the canvas', onclick: () => { state.fitted = false; draw(); } }),
      button('', { icon: 'minus', size: 'sm', title: 'Zoom out', onclick: () => { state.scale = Math.max(0.4, state.scale - 0.15); draw(); } }),
      button('', { icon: 'plus', size: 'sm', title: 'Zoom in', onclick: () => { state.scale = Math.min(2, state.scale + 0.15); draw(); } }),
      h('span', { class: 'tiny muted' }, `${raw.nodes.length} nodes, ${raw.edges.length} edges${groupIds.length ? `, ${groupIds.length} subgraph${groupIds.length === 1 ? '' : 's'}` : ''}`),
      h('span', { class: 'row tiny muted' }, svgLegend('solid', 'edge'), svgLegend('dashed', 'conditional'), svgLegend('curve', 'loop-back')),
    );
  };
  mount(host, h('div', { class: 'card' }, h('div', { class: 'card-body' }, toolbar, h('div', { class: 'graph-wrap' }, svgHost, info))));
  mount(svgHost, skeletonBlock(300));
  (async () => {
    try {
      if (!cache.graph) cache.graph = await app.api.lgGraph(aid, true);
      raw = cache.graph;
      groupIds = groupsIn(raw);
      groupIds.forEach((g) => state.collapsed.add(g));
      draw();
    } catch (err) {
      mount(svgHost, errorBox(err, () => structureTab(host, app, graphId, aid, cache), 'Cannot load graph structure'));
    }
  })();
}

function svgLegend(kind, label) {
  const s = svg('svg', { width: 22, height: 10, viewBox: '0 0 22 10', 'aria-hidden': 'true' });
  s.appendChild(svg('path', { d: kind === 'curve' ? 'M2 8C10 8 14 2 20 2' : 'M2 5H20', stroke: kind === 'curve' ? 'var(--accent)' : 'var(--text-3)', 'stroke-width': 1.5, fill: 'none', 'stroke-dasharray': kind === 'dashed' ? '3 3' : undefined }));
  return h('span', { class: 'row', style: 'gap:4px' }, s, label);
}

function renderSvg(layout, state, onNode, onGroup) {
  const root = svg('svg', { class: 'graph-svg', viewBox: `0 0 ${layout.width} ${layout.height}`, width: layout.width, height: layout.height, role: 'img', 'aria-label': 'Graph structure' });
  const defs = svg('defs');
  for (const [id, color] of [['arrow', 'var(--text-3)'], ['arrow-back', 'var(--accent)']]) {
    const m = svg('marker', { id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
    m.appendChild(svg('path', { d: 'M0 0L10 5L0 10z', fill: color }));
    defs.appendChild(m);
  }
  root.appendChild(defs);
  const ox = layout.offsetX || 0;
  const g = svg('g', { transform: `translate(${ox},0)` });
  for (const grp of layout.groups) {
    const gg = svg('g', { class: 'g-group' });
    gg.appendChild(svg('rect', { x: grp.x, y: grp.y, width: grp.w, height: grp.h, rx: 10 }));
    const label = svg('text', { x: grp.x + 10, y: grp.y + 13, class: 'g-group-label' }, `${grp.id}  (click to collapse)`);
    label.style.cursor = 'pointer';
    label.addEventListener('click', () => onGroup(grp.id));
    gg.appendChild(label);
    g.appendChild(gg);
  }
  for (const e of layout.edges) {
    g.appendChild(svg('path', { d: e.path, class: ['g-edge', e.conditional && 'cond', e.back && 'back'], 'marker-end': `url(#${e.back ? 'arrow-back' : 'arrow'})` }));
    if (e.label && e.points.length >= 2) {
      const mid = e.points[Math.floor(e.points.length / 2)];
      g.appendChild(svg('text', { x: mid.x + 6, y: mid.y - 3, class: 'g-edge-label' }, truncate(e.label, 24)));
    }
  }
  for (const n of layout.nodes) {
    const tags = nodeTags(n);
    const ng = svg('g', { class: ['g-node', `k-${n.kind}`, state.selectedNode === n.id && 'sel'], transform: `translate(${n.x},${n.y})`, tabindex: '0', role: 'button', 'aria-label': `${n.label}${n.kind === 'subgraph' ? ' (subgraph)' : ''}` });
    ng.appendChild(svg('rect', { width: n.w, height: n.h, rx: n.kind === 'start' || n.kind === 'end' ? n.h / 2 : 7 }));
    ng.appendChild(svg('text', { x: n.w / 2, y: n.h / 2 + 4, 'text-anchor': 'middle' }, n.label));
    if (n.count) ng.appendChild(svg('text', { x: n.w - 8, y: 11, 'text-anchor': 'end', class: 'g-count' }, `+${n.count}`));
    if (tags.includes('interrupt')) ng.appendChild(svg('circle', { cx: 7, cy: 7, r: 3.5, class: 'tag-interrupt' }, svg('title', null, 'interrupt')));
    if (tags.includes('Send')) ng.appendChild(svg('circle', { cx: n.w - 7, cy: n.h - 7, r: 3.5, class: 'tag-send' }, svg('title', null, 'Send fan-out')));
    ng.addEventListener('click', () => onNode(n));
    ng.addEventListener('dblclick', () => n.kind === 'subgraph' && onGroup(n.id));
    ng.addEventListener('keydown', (ev) => (ev.key === 'Enter' || ev.key === ' ') && (ev.preventDefault(), ev.shiftKey && n.kind === 'subgraph' ? onGroup(n.id) : onNode(n)));
    g.appendChild(ng);
  }
  root.appendChild(g);
  return root;
}

// ---- schemas / config -------------------------------------------------------------------------------------------------
function schemasView(s) {
  const items = [['Input schema', s.input_schema], ['Output schema', s.output_schema], ['State schema', s.state_schema]];
  return h('div', { class: 'grid cols-2' }, items.map(([title, sc]) => card({ title, body: sc ? jsonViewer(sc, { open: 2 }) : h('span', { class: 'muted' }, 'Not provided.') })));
}

function configView(s, graphId) {
  const sch = s.context_schema || s.config_schema;
  const props = sch?.properties || {};
  const keys = Object.keys(props);
  if (!keys.length) return emptyState({ icon: 'sliders', title: 'No configurable fields', text: `${graphId} exposes no context/config schema.` });
  return card({ title: 'Assistant configuration fields', sub: 'The same schema Studio renders in Manage Assistants. Fields tagged langgraph_type=prompt show a gear on their node in Studio.', flush: true, body: table({
    rows: keys.map((k) => ({ key: k, ...props[k] })),
    columns: [
      { label: 'Field', render: (r) => h('span', { class: 'mono strong' }, r.key) },
      { label: 'Type', render: (r) => h('span', { class: 'mono small' }, r.type || (r.enum ? 'enum' : r.anyOf ? 'union' : '')) },
      { label: 'Default', wrap: true, render: (r) => h('span', { class: 'mono small' }, r.default === undefined ? '' : truncate(typeof r.default === 'string' ? r.default : JSON.stringify(r.default), 80)) },
      { label: 'Notes', wrap: true, render: (r) => h('div', { class: 'row wrap' }, r.langgraph_type === 'prompt' && badge('prompt', 'accent', { sq: true }), (r.langgraph_nodes || []).map((n) => chip(n, { mono: true })), r.enum && h('span', { class: 'small muted' }, r.enum.join(' | ')), r.description && h('span', { class: 'small muted' }, r.description)) },
    ],
  }) });
}

// ---- run a sample ----------------------------------------------------------------------------------------------------
function runTab(host, app, graphId, gdoc) {
  const samples = gdoc?.sample_inputs || [];
  const ta = textareaEl({ rows: 9, mono: true, value: samples[0] ? safeJson(samples[0].input) : '{\n  "scenario_id": "S01"\n}', label: 'Input JSON', spellcheck: false });
  const status = h('div');
  const runBtn = button('Run', { icon: 'play', variant: 'primary' });
  asyncClick(runBtn, async () => {
    let input;
    try {
      input = JSON.parse(ta.value);
    } catch (e) {
      mount(status, errorBox(new Error(`Input is not valid JSON: ${e.message}`), null, 'Check the input'));
      return;
    }
    const thread = await app.api.lgCreateThread({ metadata: { foghorn_graph: graphId, foghorn_trigger: 'ui', foghorn_kind: 'explorer' } });
    await app.api.lgCreateRun(thread.thread_id, {
      assistant_id: graphId,
      input,
      stream_mode: ['values', 'updates', 'messages-tuple', 'custom', 'tasks', 'checkpoints'],
      stream_subgraphs: true,
      stream_resumable: true,
      on_disconnect: 'continue',
    });
    app.navigate(`${link('runs', thread.thread_id)}?graph=${encodeURIComponent(graphId)}`);
  }, (e) => mount(status, errorBox(e, null, 'Could not start the run')));
  mount(host, card({ title: 'Run a sample input', sub: 'Creates a fresh thread, starts the run and opens the console so you can watch it stream. Sample inputs come from each graph’s GUIDE.md.', body: h('div', { class: 'col gap-3' },
    samples.length > 0 && h('div', { class: 'row wrap' }, samples.map((s) => h('button', { class: 'chip', type: 'button', onclick: () => (ta.value = safeJson(s.input)) }, s.name))),
    ta,
    h('div', { class: 'row wrap' }, runBtn, copyButton(() => ta.value, 'Copy JSON', app.toast), graphId === 'ops_copilot' && button('Open Copilot panel', { icon: 'chat', onclick: () => app.toggleCopilot() })),
    !samples.length && notice('No sample inputs published for this graph (GUIDE.md missing). Edit the JSON above.', 'info'),
    status,
  ) }));
}

// ---- assistants ---------------------------------------------------------------------------------------------------------
async function assistantsTab(host, app, graphId, g) {
  mount(host, skeletonBlock(160));
  try {
    let list = (g.assistants || []).slice();
    let detail = [];
    try {
      detail = (await app.api.assistants()).items || [];
    } catch {
      /* fall back to catalog entries */
    }
    const rows = detail.filter((a) => a.graph_id === graphId);
    const items = rows.length ? rows : list;
    if (!items.length) {
      mount(host, emptyState({ icon: 'user', title: 'No assistants for this graph' }));
      return;
    }
    let a = items[0].assistant_id;
    let b = items[1]?.assistant_id || '';
    const diffHost = h('div');
    const sel = (cur, set) => selectEl([{ value: '', label: '(none)' }, ...items.map((x) => ({ value: x.assistant_id, label: x.name || x.assistant_id }))], cur, (v) => { set(v); drawDiff(); }, { size: 'sm', label: 'Assistant' });
    function drawDiff() {
      const A = items.find((x) => x.assistant_id === a);
      const B = items.find((x) => x.assistant_id === b);
      if (!A || !B) return mount(diffHost, h('div', { class: 'small muted' }, 'Pick two assistants to diff their context.'));
      const keys = [...new Set([...Object.keys(A.context || {}), ...Object.keys(B.context || {})])].sort();
      const diffs = keys.filter((k) => JSON.stringify(A.context?.[k]) !== JSON.stringify(B.context?.[k]));
      mount(diffHost, diffs.length ? table({ rows: diffs.map((k) => ({ k, a: A.context?.[k], b: B.context?.[k] })), columns: [{ label: 'Key', render: (r) => h('span', { class: 'mono' }, r.k) }, { label: A.name, wrap: true, render: (r) => h('span', { class: 'mono small' }, truncate(typeof r.a === 'string' ? r.a : JSON.stringify(r.a ?? null), 160)) }, { label: B.name, wrap: true, render: (r) => h('span', { class: 'mono small' }, truncate(typeof r.b === 'string' ? r.b : JSON.stringify(r.b ?? null), 160)) }] }) : h('div', { class: 'small muted' }, 'These two assistants have identical context.'));
    }
    mount(host, card({ title: 'Assistants', sub: 'Named configurations of this graph. Create and edit them in Studio (Manage Assistants).', flush: true, body: h('div', null, table({ rows: items, columns: [
      { label: 'Name', render: (r) => h('span', { class: 'strong' }, r.name || r.assistant_id) },
      { label: 'Id', render: (r) => h('span', { class: 'mono small' }, shortId(r.assistant_id, 12)) },
      { label: 'Version', num: true, render: (r) => r.version ?? '' },
      { label: 'Context', wrap: true, render: (r) => h('div', { class: 'row wrap' }, Object.entries(r.context || {}).slice(0, 5).map(([k, v]) => chip(`${k}=${truncate(typeof v === 'string' ? v : JSON.stringify(v), 22)}`, { mono: true }))) },
    ] }), items.length > 1 && h('div', { class: 'card-body col gap-3', style: 'border-top:1px solid var(--border)' }, h('h4', null, 'Diff two assistants'), h('div', { class: 'row wrap' }, sel(a, (v) => (a = v)), h('span', { class: 'muted' }, 'vs'), sel(b, (v) => (b = v))), diffHost)) }));
    drawDiff();
  } catch (err) {
    mount(host, errorBox(err));
  }
}

// ---- recent threads -------------------------------------------------------------------------------------------------------
function threadsTab(host, app, graphId) {
  loadInto(host, async () => {
    const res = await app.api.lgThreadsSearch({ metadata: { foghorn_graph: graphId }, limit: 12, sort_by: 'updated_at', sort_order: 'desc' });
    return Array.isArray(res) ? res : res?.threads || [];
  }, (threads) => (threads.length ? card({ title: 'Recent threads', flush: true, body: table({ rows: threads, columns: [
    { label: 'Thread', render: (t) => h('a', { class: 'mono', href: link('runs', t.thread_id) }, shortId(t.thread_id, 12)) },
    { label: 'Status', render: (t) => statusBadge(t.status) },
    { label: 'Incident', render: (t) => t.metadata?.foghorn_incident_id ? h('a', { href: link('incidents', t.metadata.foghorn_incident_id), class: 'mono' }, t.metadata.foghorn_incident_id) : '' },
    { label: 'Scenario', render: (t) => t.metadata?.foghorn_scenario_id || '' },
    { label: 'Updated', render: (t) => h('span', { class: 'small muted' }, ago(t.updated_at)) },
    { label: '', render: (t) => h('span', { class: 'row' }, button('Time travel', { size: 'sm', href: link('threads', t.thread_id), icon: 'history' }), button('Studio', { size: 'sm', href: app.studioThread(t.thread_id, graphId), external: true, icon: 'external' })) },
  ] }) }) : emptyState({ icon: 'history', title: 'No threads for this graph yet', text: 'Run a sample or launch a scenario.' })), { skeleton: () => skeletonBlock(160) });
}
