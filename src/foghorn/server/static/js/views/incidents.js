// Incidents list + quick launch.
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, emptyState, errorBox, errorMessage, field, incBadge, loadInto, selectEl, skeleton, statusBadge, table, viewHead, asyncClick, seg } from '../core/ui.js';
import { link } from '../core/router.js';
import { ago } from './shared.js';

export default async function incidents(root, route, app) {
  let status = '';
  const body = h('div');
  const disposers = [];

  // quick launch
  const scenSel = selectEl([{ value: '', label: 'Loading scenarios...' }], '', null, { label: 'Scenario' });
  let profile = 'competent';
  const profSel = selectEl(['competent', 'naive', 'cautious', 'gullible'], profile, (v) => (profile = v), { label: 'Model profile' });
  const launchBtn = button('Launch incident', { icon: 'play', variant: 'primary', disabled: true });
  asyncClick(launchBtn, async () => {
    const id = scenSel.value;
    if (!id) return;
    const res = await app.api.launchScenario(id, { mode: 'direct', profile, speed: 60, replace: true });
    app.toast({ title: `Launched ${id}`, body: res.incident_id ? `Opening ${res.incident_id}` : 'Waiting for the incident to open...', tone: 'ok', ttl: 3500 });
    if (res.incident_id) app.navigate(link('incidents', res.incident_id));
    else load(false);
  }, (e) => app.toast({ title: 'Launch failed', body: errorMessage(e), tone: 'critical' }));

  mount(
    root,
    viewHead({ title: 'Incidents', sub: 'Open incidents and their war rooms. Pick one to watch the agents work.', actions: [seg([{ value: '', label: 'All' }, { value: 'open', label: 'Open' }, { value: 'resolved', label: 'Resolved' }], status, (v) => { status = v; load(); }, { label: 'Status filter' })] }),
    card({ title: 'Launch an incident', sub: 'Direct mode skips the event router and starts the commander straight away. Use Scenarios for the full launcher.', body: h('div', { class: 'row wrap bottom' }, h('div', { style: 'min-width:260px;flex:1' }, field('Scenario', scenSel)), h('div', { style: 'width:170px' }, field('Model profile', profSel)), launchBtn, button('Full launcher', { href: '#/scenarios', icon: 'target' })) }),
    h('div', { class: 'mt-4' }, body),
  );

  app.api.scenarios().then((r) => {
    const items = r.items || [];
    mount(scenSel, h('option', { value: '' }, 'Choose a scenario...'), items.map((s) => h('option', { value: s.scenario_id || s.id }, `${s.scenario_id || s.id}  ${s.title || ''}`)));
    launchBtn.disabled = !items.length;
  }).catch(() => {
    mount(scenSel, h('option', { value: '' }, 'Scenarios unavailable'));
  });

  async function load(skel = true) {
    if (skel) mount(body, skeleton(5));
    try {
      const res = await app.api.incidents({ status: status === 'open' ? 'open,mitigating,monitoring' : status || undefined, limit: 100 });
      const items = res.items || [];
      if (!items.length) {
        mount(body, emptyState({ icon: 'alert', title: 'No incidents yet', text: 'Launch a scenario above, replay a stream, or fire an alert from the event injector.', action: h('div', { class: 'row' }, button('Scenarios', { href: '#/scenarios', variant: 'primary' }), button('Inject event', { href: '#/scenarios/inject' })) }));
        return;
      }
      mount(body, card({ flush: true, body: table({
        caption: 'Incidents',
        rows: items,
        rowKey: (r) => r.incident_id,
        onRow: (r) => app.navigate(link('incidents', r.incident_id)),
        columns: [
          { label: 'Incident', render: (r) => h('span', { class: 'mono strong' }, r.incident_id) },
          { label: 'Sev', render: (r) => incBadge(r.severity) },
          { label: 'Status', render: (r) => statusBadge(r.status) },
          { label: 'Title', wrap: true, render: (r) => h('span', { class: 'untrusted-text' }, r.title || '') },
          { label: 'Component', render: (r) => r.component || '' },
          { label: 'Scenario', render: (r) => r.scenario_id && chip(r.scenario_id, { mono: true }) },
          { label: 'Approvals', render: (r) => (r.pending_approvals ? badge(`${r.pending_approvals} pending`, 'medium') : '') },
          { label: 'Opened', render: (r) => h('span', { class: 'muted small' }, ago(r.opened_at)) },
        ],
      }) }));
    } catch (err) {
      mount(body, errorBox(err, () => load(), 'Cannot load incidents'));
    }
  }
  disposers.push(app.live.on('event', ({ event: e, backfill }) => { if (!backfill && e.type.startsWith('incident.')) load(false); }));
  await load();
  return () => disposers.forEach((d) => d());
}
