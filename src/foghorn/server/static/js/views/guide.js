// Studio guide: setup, common moves, and a per-graph click path (bundled from /api/studio/guide).
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, copyButton, emptyState, errorBox, jsonViewer, markdown, notice, skeleton, viewHead, renderInline } from '../core/ui.js';
import { parseInline } from '../core/markdown.js';
import { link } from '../core/router.js';
import { safeJson } from '../core/format.js';

const COMMON_MOVES = [
  ['Run', 'Open the input panel, paste the JSON, Submit. Start a new thread per scenario: an existing thread keeps its state.'],
  ['Stream modes', 'Choose them in the run settings. Pick messages + updates + custom to see tokens and Foghorn’s custom events.'],
  ['Subgraphs', 'Toggle the graph panel’s xray / subgraph expansion to open rca_tournament and the specialists.'],
  ['Interrupt', 'A pending interrupt shows its payload and a resume box. Paste JSON that matches the shown schema. The Interrupt dropdown next to Submit sets static breakpoints per node.'],
  ['Time travel', 'Open a step in the thread history, then Fork (or edit state on that step), change a value and run from there. The original branch is preserved.'],
  ['Assistants', 'Manage Assistants: pick or duplicate, edit prompt/config fields, save, then choose it for the next run.'],
  ['Threads', 'Filter the thread list by metadata foghorn_scenario_id or foghorn_incident_id.'],
  ['Store', 'If your Studio build has a memory browser, use it; otherwise use the Memory view in this dashboard.'],
];

export default async function guide(root, route, app) {
  const body = h('div', { class: 'col gap-4' });
  mount(root, viewHead({ title: 'Studio guide', sub: 'Everything the dashboard launches can also be launched from LangGraph Studio with a one to three field JSON input. Button labels drift between Studio releases, so this guide names things instead of pointing at pixels.', actions: [button('Open Studio', { icon: 'external', variant: 'primary', href: app.studioUrl(), external: true })] }), body);
  mount(body, skeleton(8));
  let data;
  try {
    data = await app.api.guide();
  } catch (err) {
    mount(body, errorBox(err, () => location.reload(), 'Cannot load the Studio guide'));
    return;
  }
  const studioUrl = data.studio?.base_url || app.studioUrl();
  const base = app.config?.public_base_url || location.origin;

  const inline = (s) => h('span', null, renderInline(parseInline(String(s))));
  const setup = data.setup || {};
  const urlBox = h('div', { class: 'urlbox' }, h('code', { class: 'grow', style: 'word-break:break-all' }, studioUrl), copyButton(studioUrl, 'Copy URL', app.toast), button('Open', { size: 'sm', icon: 'external', href: studioUrl, external: true }));

  const setupCard = card({ title: setup.title || 'Set up', body: h('div', { class: 'col gap-3' },
    h('ol', { class: 'steps' }, (setup.steps || []).map((s) => h('li', null, inline(s)))),
    h('div', null, h('div', { class: 'sec-label' }, 'Studio URL for this server'), urlBox),
    h('div', { class: 'small muted' }, `This server: ${base}. If your browser blocks the loopback server (Safari, Brave), start it with --tunnel and use the printed URL. Studio's web UI needs internet and a LangSmith login; Foghorn itself runs fully offline.`),
  ) });

  const moves = card({ title: 'Common moves', sub: 'Referenced by every graph card below', body: h('dl', { class: 'kv moves' }, COMMON_MOVES.flatMap(([k, v]) => [h('dt', { class: 'strong' }, k), h('dd', null, v)])) });

  const graphs = data.graphs || [];
  const nav = h('nav', { class: 'guide-nav', 'aria-label': 'Graphs' }, graphs.map((g) => h('a', { href: link('guide', g.graph_id), class: route.params.graph === g.graph_id ? 'sel' : '' }, g.graph_id)));

  const cards = graphs.map((g) => h('article', { class: 'card guide-card', id: `g-${g.graph_id}` },
    h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('h3', { class: 'mono' }, g.graph_id), g.title && g.title !== g.graph_id && h('div', { class: 'sub' }, g.title)), button('Open in Studio', { size: 'sm', icon: 'external', href: app.studioGraph(g.graph_id), external: true }), button('Explorer', { size: 'sm', icon: 'graph', href: link('graphs', g.graph_id) })),
    h('div', { class: 'card-body col gap-3' },
      g.summary && h('p', { style: 'margin:0' }, g.summary),
      (g.features || []).length > 0 && h('div', { class: 'row wrap' }, g.features.map((f) => chip(f))),
      (g.studio_steps || []).length > 0 && h('div', null, h('div', { class: 'sec-label' }, 'Click path'), h('ol', { class: 'steps' }, g.studio_steps.map((s) => h('li', null, inline(s))))),
      (g.sample_inputs || []).length > 0 && h('div', null, h('div', { class: 'sec-label' }, 'Sample inputs (paste into the Studio input panel)'), h('div', { class: 'col' }, g.sample_inputs.map((s) => h('div', { class: 'sample' }, h('div', { class: 'row between' }, h('span', { class: 'strong small' }, s.name), copyButton(safeJson(s.input), 'Copy JSON', app.toast)), h('pre', { class: 'jv mono' }, safeJson(s.input)))))),
      g.body && g.body.trim() && h('details', null, h('summary', { class: 'small', style: 'cursor:pointer' }, 'More: what to look for'), markdown(g.body)),
      !g.has_guide && notice('No GUIDE.md for this graph yet; the catalog entry is shown.', 'info'),
    ),
  ));

  mount(body, setupCard, moves, (data.warnings || []).length > 0 && notice(h('span', null, h('strong', null, 'Guide warnings: '), data.warnings.join(' · ')), 'medium', 'alert'), h('div', { class: 'guide-layout' }, nav, h('div', { class: 'col gap-4' }, cards.length ? cards : emptyState({ icon: 'book', title: 'No per-graph guides yet', text: 'Graph owners add flows/<id>/GUIDE.md.' }))));
  if (route.params.graph) document.getElementById(`g-${route.params.graph}`)?.scrollIntoView({ block: 'start' });
}
