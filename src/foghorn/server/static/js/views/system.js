// System: health, queues, dead letters, clock and replay, ambient traffic, native cron, config snapshot, reset.
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, confirmDialog, emptyState, errorBox, errorMessage, jsonViewer, kpi, notice, seg, skeleton, statusBadge, switchEl, table, viewHead, asyncClick, tabs } from '../core/ui.js';
import { queuePressure } from '../core/events.js';
import { formatNumber, formatPct, formatDateTime, truncate } from '../core/format.js';
import { ago } from './shared.js';

export default async function system(root, route, app) {
  const disposers = [];
  const kpisEl = h('div', { class: 'kpis' });
  const healthEl = h('div');
  const queueEl = h('div');
  const dlqEl = h('div');
  const clockEl = h('div');
  const cronEl = h('div');
  const cfgEl = h('div');
  const adminEl = h('div');
  let dlqStatus = 'open';

  mount(root, viewHead({ title: 'System', sub: 'Queues, backpressure, dead letters, clocks and configuration of this Foghorn server.', actions: [button('Refresh', { icon: 'refresh', size: 'sm', onclick: () => refreshAll() })] }),
    h('div', { class: 'col gap-4' }, kpisEl,
      h('div', { class: 'grid cols-2' }, card({ title: 'Components', body: healthEl }), card({ title: 'Queues and backpressure', body: queueEl })),
      card({ title: 'Dead letters', actions: seg([{ value: 'open', label: 'Open' }, { value: 'retried', label: 'Retried' }, { value: 'discarded', label: 'Discarded' }], dlqStatus, (v) => { dlqStatus = v; loadDlq(); }, { label: 'Dead letter status' }), flush: true, body: dlqEl }),
      h('div', { class: 'grid cols-2' }, card({ title: 'Clock and ambient traffic', body: clockEl }), card({ title: 'Scheduled work (native cron)', sub: 'Server crons from the Agent Server', body: cronEl })),
      h('div', { class: 'grid cols-2' }, card({ title: 'Configuration snapshot', sub: 'Secrets are never shown', body: cfgEl }), card({ title: 'Danger zone', body: adminEl })),
    ));
  [healthEl, queueEl, dlqEl, clockEl, cronEl, cfgEl].forEach((e) => mount(e, skeleton(3)));

  // --- health ---
  async function loadHealth() {
    try {
      const hres = await app.api.health();
      const comps = hres.components || {};
      mount(healthEl, h('div', { class: 'col' }, h('div', { class: 'row wrap' }, statusBadge(hres.status === 'ok' ? 'ok' : 'degraded', hres.status), hres.ready === false && badge('not ready', 'medium'), hres.uptime_s != null && chip(`uptime ${Math.round(hres.uptime_s / 60)} min`)), h('ul', { class: 'plain-list' }, Object.entries(comps).map(([k, v]) => h('li', { class: 'row between small' }, h('span', { class: 'mono' }, k), statusBadge(['ok', 'running', 'ready', 'done', 'idle'].includes(v) ? 'ok' : ['waiting', 'pending'].includes(v) ? 'pending' : 'failed', v))))));
    } catch (err) {
      mount(healthEl, errorBox(err, loadHealth));
    }
  }

  // --- queues ---
  function renderQueue(q) {
    if (!q) return mount(queueEl, skeleton(4));
    const p = queuePressure(q);
    mount(queueEl, h('div', { class: 'col gap-3' },
      ['P0', 'P1', 'P2', 'P3'].map((k) => h('div', { class: 'qrow' }, h('span', { class: 'mono strong' }, k), h('div', { class: ['bar', `t-${p[k].tone === 'ok' ? 'ok' : p[k].tone}`] }, h('i', { style: { width: Math.min(100, Math.max(p[k].depth ? 2 : 0, p[k].ratio * 100)) + '%' } }), h('span', { class: 'hw', style: { left: (q.high_water ?? 0.8) * 100 + '%' } })), h('span', { class: 'num small' }, `${p[k].depth}/${p[k].cap}`))),
      h('div', { class: 'row wrap small' }, q.backpressure ? badge('backpressure', 'critical', { dot: true, live: true }) : badge('flowing', 'ok', { dot: true }), chip(`high water ${formatPct(q.high_water ?? 0.8, 0)}`), chip(`low water ${formatPct(q.low_water ?? 0.5, 0)}`), chip(`${q.in_flight ?? 0} in flight`), chip(`${q.workers ?? '?'} workers`), chip(`${q.interrupted ?? 0} interrupted`), chip(`oldest wait ${q.oldest_wait_s ?? 0}s`)),
      q.per_graph && h('div', { class: 'row wrap' }, Object.entries(q.per_graph).map(([g, v]) => chip(`${g} ${v.running}/${v.cap}`, { mono: true }))),
      h('div', { class: 'row wrap small muted' }, `${formatNumber(q.events_total ?? 0)} events ingested · ${formatNumber(q.dispatched_total ?? 0)} dispatched · ${formatNumber(q.shed ?? 0)} shed · ${formatNumber(q.rate_per_s ?? 0, 1)}/s`),
    ));
    mount(kpisEl,
      kpi({ label: 'Total depth', value: formatNumber(q.total_depth ?? 0), sub: `of ${formatNumber(q.total_cap ?? 0)}` }),
      kpi({ label: 'Utilisation', value: formatPct(q.utilization ?? 0, 1), tone: q.backpressure ? 'critical' : undefined, sub: q.backpressure ? 'backpressure' : 'ok' }),
      kpi({ label: 'In flight', value: formatNumber(q.in_flight ?? 0), sub: `${q.workers ?? '?'} workers` }),
      kpi({ label: 'Dead letters', value: formatNumber(q.dlq ?? 0), tone: q.dlq ? 'critical' : undefined, sub: 'open' }),
      kpi({ label: 'Shed', value: formatNumber(q.shed ?? 0), tone: q.shed ? 'high' : undefined, sub: 'dropped at hard cap' }),
      kpi({ label: 'Events / s', value: formatNumber(q.rate_per_s ?? 0, 1), sub: `${formatNumber(q.events_total ?? 0)} total` }),
    );
  }
  async function loadQueues() {
    try {
      renderQueue(app.live.state.queue || (await app.api.queues()));
    } catch (err) {
      mount(queueEl, errorBox(err, loadQueues));
    }
  }
  disposers.push(app.live.on('queue', renderQueue));

  // --- dlq ---
  async function loadDlq() {
    mount(dlqEl, skeleton(3));
    try {
      const res = await app.api.dlq({ status: dlqStatus, limit: 100 });
      const items = res.items || [];
      if (!items.length) return mount(dlqEl, emptyState({ icon: 'check-circle', title: dlqStatus === 'open' ? 'Nothing is stuck' : 'No entries', text: dlqStatus === 'open' ? 'Poison, malformed and failed events would appear here.' : undefined }));
      mount(dlqEl, table({ caption: 'Dead letters', rows: items, columns: [
        { label: 'Id', render: (d) => h('span', { class: 'mono strong' }, d.id) },
        { label: 'Reason', render: (d) => h('span', null, badge(d.reason, d.poison ? 'critical' : 'high', { sq: true }), ' ', d.poison && badge('poison', 'critical')) },
        { label: 'Event', render: (d) => d.event_id ? h('a', { class: 'mono', href: `#/events/${encodeURIComponent(d.event_id)}` }, d.event_id) : '' },
        { label: 'Attempts', num: true, render: (d) => d.attempts },
        { label: 'Last error', wrap: true, render: (d) => h('span', { class: 'small untrusted-text' }, truncate(d.last_error || d.raw_body || '', 120)) },
        { label: 'When', render: (d) => h('span', { class: 'small muted' }, ago(d.created_at)) },
        { label: '', render: (d) => (d.status === 'open' ? h('span', { class: 'row' }, (() => { const b = button('Retry', { size: 'sm', icon: 'refresh' }); asyncClick(b, async () => { await app.api.dlqRetry(d.id); app.toast({ title: 'Retried', body: d.id, tone: 'ok', ttl: 2500 }); loadDlq(); }, (e) => app.toast({ title: 'Retry failed', body: errorMessage(e), tone: 'critical' })); return b; })(), (() => { const b = button('Discard', { size: 'sm', icon: 'trash', variant: 'danger' }); asyncClick(b, async () => { await app.api.dlqDiscard(d.id, { reason: 'discarded from System', by: app.persona.get() }); loadDlq(); }, (e) => app.toast({ title: 'Discard failed', body: errorMessage(e), tone: 'critical' })); return b; })()) : statusBadge(d.status)) },
      ] }));
    } catch (err) {
      mount(dlqEl, h('div', { class: 'card-body' }, errorBox(err, loadDlq)));
    }
  }

  // --- clock / ambient ---
  async function loadClock() {
    try {
      const c = app.live.state.clock || (await app.api.clock());
      const speedSeg = seg([1, 10, 60, 600].map((v) => ({ value: v, label: `${v}x` })), c.speed || 60, async (v) => { try { await app.api.setClock({ speed: v }); } catch (e) { app.toast({ title: 'Clock', body: errorMessage(e), tone: 'critical' }); } }, { label: 'Clock speed' });
      const pauseBtn = button(c.paused ? 'Resume clock' : 'Pause clock', { size: 'sm', icon: c.paused ? 'play' : 'pause' });
      asyncClick(pauseBtn, async () => { await app.api.setClock({ paused: !c.paused }); app.live.state.clock = null; loadClock(); }, (e) => app.toast({ title: 'Clock', body: errorMessage(e), tone: 'critical' }));
      mount(clockEl, h('div', { class: 'col gap-3' },
        h('dl', { class: 'kv' }, h('dt', null, 'Virtual now'), h('dd', { class: 'mono' }, formatDateTime(c.virtual_now)), h('dt', null, 'Real time'), h('dd', { class: 'mono' }, c.wall_now || ''), h('dt', null, 'Mode'), h('dd', null, c.mode || 'free'), h('dt', null, 'Replay'), h('dd', null, c.replay ? `${c.replay.name || ''} ${c.replay.position}/${c.replay.total}` : 'idle')),
        h('div', { class: 'row wrap' }, speedSeg, pauseBtn),
        switchEl('Ambient background traffic', !!app.config?.events?.ambient_enabled, async (v) => { try { await app.api.adminAmbient(v); app.toast({ title: v ? 'Ambient on' : 'Ambient off', tone: 'ok', ttl: 2000 }); } catch (e) { app.toast({ title: 'Ambient', body: errorMessage(e), tone: 'critical' }); } }),
        h('div', { class: 'tiny muted' }, 'Ambient replays a quiet hour in a loop (stateless triage runs). It pauses itself under backpressure.'),
      ));
    } catch (err) {
      mount(clockEl, errorBox(err, loadClock));
    }
  }

  // --- cron ---
  async function loadCron() {
    try {
      const res = await app.api.lgCronsSearch();
      const crons = Array.isArray(res) ? res : res?.crons || [];
      if (!crons.length) return mount(cronEl, emptyState({ icon: 'clock', title: 'No native crons', text: 'The shift_handoff cron is created (disabled) when the server seeds.' }));
      mount(cronEl, h('ul', { class: 'plain-list' }, crons.map((c) => h('li', { class: 'row between' },
        h('div', null, h('div', { class: 'mono strong small' }, `${c.assistant_id?.length > 20 ? c.metadata?.graph_id || 'cron' : c.assistant_id} · ${c.schedule}`), h('div', { class: 'tiny muted' }, c.next_run_date ? `next run ${formatDateTime(c.next_run_date)}` : 'no next run')),
        switchEl(c.enabled === false ? 'disabled' : 'enabled', c.enabled !== false, async (v) => { try { await app.api.lgCronPatch(c.cron_id, { enabled: v }); app.toast({ title: v ? 'Cron enabled' : 'Cron disabled', tone: 'ok', ttl: 2000 }); loadCron(); } catch (e) { app.toast({ title: 'Cron', body: errorMessage(e), tone: 'critical' }); } }),
      ))));
    } catch (err) {
      mount(cronEl, errorBox(err, loadCron));
    }
  }

  // --- config ---
  function loadConfig() {
    const c = app.config;
    if (!c) return mount(cfgEl, emptyState({ title: 'Config unavailable' }));
    mount(cfgEl, h('div', { class: 'col gap-3' }, h('div', { class: 'row wrap' }, chip(`env ${c.environment || '?'}`), chip(`provider ${c.provider || '?'}`), c.auth_required && badge('auth required', 'medium'), c.webhook_signature_required && badge('HMAC required', 'medium')), jsonViewer(c, { open: 1 })));
  }

  // --- admin ---
  const resetBtn = button('Reset demo state', { variant: 'danger', icon: 'trash' });
  asyncClick(resetBtn, async () => {
    const ok = await confirmDialog({ title: 'Reset demo state?', text: 'Deletes events, dispatches, incidents, approvals projection, dead letters and eval runs, and resets the virtual clock. LangGraph threads and Store memory are not touched.', confirmLabel: 'Reset', danger: true });
    if (!ok) return;
    const r = await app.api.adminReset();
    app.live.feed.clear();
    app.toast({ title: 'Demo state reset', body: Object.entries(r.deleted || {}).map(([k, v]) => `${k}: ${v}`).join(', '), tone: 'ok', ttl: 5000 });
    refreshAll();
  }, (e) => app.toast({ title: 'Reset failed', body: errorMessage(e), tone: 'critical' }));
  mount(adminEl, h('div', { class: 'col gap-3' }, notice('Reset clears the event log, incidents, approvals projection, dead letters and eval runs. Threads and Store memory stay.', 'medium', 'alert'), h('div', null, resetBtn)));

  function refreshAll() {
    loadHealth();
    loadQueues();
    loadDlq();
    loadClock();
    loadCron();
    loadConfig();
  }
  refreshAll();
  const t = setInterval(() => { loadHealth(); loadClock(); }, 8000);
  disposers.push(() => clearInterval(t), app.live.on('clock', () => {}));
  return () => disposers.forEach((d) => d());
}
