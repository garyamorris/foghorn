// Fault injector: category + component + magnitude + red herrings -> a brand-new labelled scenario (X-nnnn) you can launch.
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, emptyState, errorBox, errorMessage, field, inputEl, jsonViewer, loadInto, notice, selectEl, seg, skeleton, table, viewHead, asyncClick } from '../core/ui.js';
import { link } from '../core/router.js';
import { formatDateTime, formatPct, titleCase } from '../core/format.js';

const FALLBACK_CATEGORIES = ['bad_deploy', 'config_change', 'cert_expiry', 'disk_full', 'db_pool_exhaustion', 'cache_stampede', 'queue_poison_message', 'dependency_outage', 'memory_leak', 'slow_query_migration', 'network_partition', 'runaway_query'];

export default async function faults(root, route, app) {
  const draft = { category: '', component: 'checkout-service', magnitude: 0.7, red_herrings: 1, seed: 7, profile: 'competent', approval_policy: 'standard', speed: 60 };
  const previewHost = h('div');
  const listHost = h('div');
  const magLabel = h('span', { class: 'mono strong' }, formatPct(draft.magnitude, 0));
  const rhLabel = h('span', { class: 'mono strong' }, String(draft.red_herrings));
  let categories = FALLBACK_CATEGORIES;
  let services = [];
  try {
    const [f, s] = await Promise.all([app.api.faults().catch(() => null), app.api.services().catch(() => null)]);
    if (f?.categories?.length) categories = f.categories;
    services = (s?.items || []).map((x) => x.id || x.name || x.component).filter(Boolean);
    if (f?.items) renderList(f.items);
  } catch {
    /* defaults */
  }
  if (!services.length) services = ['checkout-service', 'payments-service', 'orders-db', 'pricing-service', 'cart-service', 'sessions-cache', 'search-service', 'notification-service', 'event-bus', 'auth-service'];
  draft.category = categories[0];
  if (!services.includes(draft.component)) draft.component = services[0];

  const catSel = selectEl(categories.map((c) => ({ value: c, label: titleCase(c) })), draft.category, (v) => (draft.category = v), { label: 'Fault category' });
  const compSel = selectEl(services, draft.component, (v) => (draft.component = v), { label: 'Component' });
  const seed = inputEl({ type: 'number', value: draft.seed, label: 'Seed', onchange: (e) => (draft.seed = Number(e.target.value) || 0) });
  const cfg = app.config || {};

  const genBtn = button('Generate scenario', { icon: 'bolt', variant: 'primary' });
  const genLaunchBtn = button('Generate and launch', { icon: 'play' });
  const request = (launch) => ({ category: draft.category, component: draft.component, magnitude: draft.magnitude, red_herrings: draft.red_herrings, seed: draft.seed, launch, speed: draft.speed, run: { profile: draft.profile, approval_policy: draft.approval_policy } });

  asyncClick(genBtn, async () => {
    const res = await app.api.injectFault(request(false));
    showPreview(res);
    refreshList();
  }, (e) => mount(previewHost, errorBox(e, null, 'Could not generate a scenario')));
  asyncClick(genLaunchBtn, async () => {
    const res = await app.api.injectFault(request(true));
    showPreview(res);
    refreshList();
    app.toast({ title: `Launched ${res.scenario_id}`, tone: 'ok', ttl: 3500 });
    if (res.launch?.incident_id) app.navigate(link('incidents', res.launch.incident_id));
  }, (e) => mount(previewHost, errorBox(e, null, 'Could not generate a scenario')));

  function showPreview(res) {
    const s = res.scenario || {};
    const id = res.scenario_id || s.scenario_id;
    const launchBtn = button('Launch', { icon: 'play', variant: 'primary' });
    asyncClick(launchBtn, async () => {
      const r = await app.api.launchScenario(id, { profile: draft.profile, approval_policy: draft.approval_policy, speed: draft.speed, mode: 'events', replace: true });
      app.toast({ title: `Launched ${id}`, tone: 'ok', ttl: 3000 });
      if (r.incident_id) app.navigate(link('incidents', r.incident_id));
      else app.navigate('#/');
    }, (e) => app.toast({ title: 'Launch failed', body: errorMessage(e), tone: 'critical' }));
    const truthHost = h('div');
    const reveal = button('Reveal ground truth', { icon: 'eye', size: 'sm' });
    asyncClick(reveal, async () => {
      const full = await app.api.scenario(id, true);
      mount(truthHost, jsonViewer(full.truth || full, { open: 2 }));
      reveal.hidden = true;
    });
    mount(previewHost, card({ title: `Generated ${id}`, sub: 'A fully labelled world: telemetry, logs, changes and alerts are generated; the truth stays hidden until you ask.', tone: 'ok', body: h('div', { class: 'col gap-3' },
      h('div', { class: 'row wrap' }, chip(draft.category), chip(draft.component), chip(`magnitude ${formatPct(draft.magnitude, 0)}`), chip(`${draft.red_herrings} red herring${draft.red_herrings === 1 ? '' : 's'}`), chip(`seed ${draft.seed}`)),
      s.title && h('div', { class: 'strong' }, s.title),
      s.premise && h('p', { class: 'muted small' }, s.premise),
      h('div', { class: 'row wrap' }, res.launch ? button('Open Mission Control', { href: '#/', variant: 'primary' }) : launchBtn, reveal),
      truthHost,
      jsonViewer(s, { open: 1 }),
    ) }));
  }

  async function refreshList() {
    try {
      renderList((await app.api.faults()).items || []);
    } catch {
      /* keep current */
    }
  }
  function renderList(items) {
    if (!items.length) {
      mount(listHost, emptyState({ icon: 'bolt', title: 'No generated scenarios yet', text: 'Generate one with the form.' }));
      return;
    }
    mount(listHost, table({ rows: items.slice().reverse(), columns: [
      { label: 'Scenario', render: (r) => h('span', { class: 'mono strong' }, r.scenario_id) },
      { label: 'Category', render: (r) => r.category },
      { label: 'Component', render: (r) => r.component },
      { label: 'Mag.', num: true, render: (r) => formatPct(r.magnitude, 0) },
      { label: 'Red herrings', num: true, render: (r) => r.red_herrings },
      { label: 'Created', render: (r) => h('span', { class: 'small muted' }, r.created_at ? formatDateTime(r.created_at) : '') },
      { label: '', render: (r) => { const b = button('Launch', { size: 'sm', icon: 'play' }); asyncClick(b, async () => { const x = await app.api.launchScenario(r.scenario_id, { profile: draft.profile, approval_policy: draft.approval_policy, speed: draft.speed, replace: true }); app.toast({ title: `Launched ${r.scenario_id}`, tone: 'ok', ttl: 3000 }); if (x.incident_id) app.navigate(link('incidents', x.incident_id)); }, (e) => app.toast({ title: 'Launch failed', body: errorMessage(e), tone: 'critical' })); return b; } },
    ] }));
  }
  mount(listHost, skeleton(3));
  if (!listHost.querySelector('table')) refreshList();

  mount(
    root,
    viewHead({ title: 'Fault injector', sub: 'Break something on purpose. The generator builds a new world around your fault (metrics, logs, changes, alerts, red herrings) with exact ground truth, then launches it through the normal pipeline.' }),
    h('div', { class: 'faults-layout' },
      card({ title: 'Design a fault', body: h('div', { class: 'col gap-4' },
        field('Category', catSel),
        field('Component', compSel, { hint: 'The root-cause component. Effects propagate up the call graph.' }),
        field('Magnitude', h('div', { class: 'row' }, h('input', { type: 'range', class: 'range', min: 0.1, max: 1, step: 0.05, value: draft.magnitude, 'aria-label': 'Magnitude', oninput: (e) => { draft.magnitude = Number(e.target.value); magLabel.textContent = formatPct(draft.magnitude, 0); } }), magLabel), { hint: 'Signal strength: low magnitudes are harder to separate from noise.' }),
        field('Red herrings', h('div', { class: 'row' }, h('input', { type: 'range', class: 'range', min: 0, max: 4, step: 1, value: draft.red_herrings, 'aria-label': 'Red herrings', oninput: (e) => { draft.red_herrings = Number(e.target.value); rhLabel.textContent = String(draft.red_herrings); } }), rhLabel), { hint: 'Innocent changes placed near the onset to tempt the naive profile.' }),
        h('div', { class: 'grid cols-2', style: 'gap:12px' }, field('Seed', seed), field('Replay speed', seg([1, 10, 60, 600].map((v) => ({ value: v, label: `${v}x` })), draft.speed, (v) => (draft.speed = v), { label: 'Replay speed' }))),
        h('div', { class: 'grid cols-2', style: 'gap:12px' }, field('Model profile', selectEl(cfg.model_profiles || ['competent', 'naive', 'cautious', 'gullible'], draft.profile, (v) => (draft.profile = v))), field('Approval policy', selectEl(cfg.approval_policies || ['strict', 'standard', 'auto_low_risk'], draft.approval_policy, (v) => (draft.approval_policy = v)))),
        h('div', { class: 'row wrap' }, genBtn, genLaunchBtn),
      ) }),
      h('div', { class: 'col gap-4' }, previewHost, card({ title: 'Generated scenarios', flush: true, body: listHost })),
    ),
  );
  if (!previewHost.firstChild) mount(previewHost, card({ body: emptyState({ icon: 'bolt', title: 'Nothing generated yet', text: 'Choose a category and component, then Generate. The preview shows the new scenario without its answer.' }) }));
}
