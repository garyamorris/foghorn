// Mission Control: live event feed (SSE), KPI tiles, queues/backpressure, DLQ, replay and clock controls.
import { h, mount, rafThrottle, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, emptyState, errorBox, errorMessage, iconButton, inputEl, kpi, selectEl, seg, skeleton, statusBadge, switchEl, viewHead, asyncClick, sevBadge } from '../core/ui.js';
import { EVENT_GROUPS, queuePressure, matchEvent, summarizeEvent } from '../core/events.js';
import { formatNumber, formatPct, formatClock, formatDateTime, truncate, severityRank } from '../core/format.js';
import { link } from '../core/router.js';
import { eventRow, openEventDrawer, ago } from './shared.js';

const FEED_LIMIT = 200;

export default async function mission(root, route, app) {
  const live = app.live;
  const disposers = [];
  const timers = [];
  const on = (type, fn) => disposers.push(live.on(type, fn));

  const filter = { groups: [], minSeverity: '', component: '', text: '' };
  let paused = false;
  let pendingWhilePaused = 0;
  let selectedId = route.params.id || null;

  // ---- header ------------------------------------------------------------------------------------------------
  const head = viewHead({
    title: 'Mission Control',
    sub: 'Live events, queues and runs for Brightcart. Everything on this page updates over SSE.',
    actions: [button('Inject fault', { icon: 'bolt', href: '#/faults' }), button('Inject event', { icon: 'send', href: '#/scenarios/inject' })],
  });

  // ---- KPI strip -------------------------------------------------------------------------------------------
  const kpis = h('div', { class: 'kpis', role: 'group', 'aria-label': 'Key indicators' });
  const renderKpis = () => {
    const s = live.state.summary || {};
    const q = live.state.queue || {};
    const depth = q.depth || s.queue?.depth || {};
    const dlq = q.dlq ?? s.dlq_open ?? 0;
    const shed = q.shed ?? s.shed_total ?? 0;
    const rate = typeof s.events_per_min === 'number' ? s.events_per_min : live.feed.ratePerMin();
    const lastEval = s.last_eval;
    mount(
      kpis,
      kpi({ label: 'Open incidents', value: formatNumber(s.open_incidents ?? 0), href: '#/incidents', tone: (s.open_incidents || 0) > 0 ? 'high' : undefined, sub: (s.open_incidents || 0) > 0 ? 'needs attention' : 'all clear' }),
      kpi({ label: 'Pending approvals', value: formatNumber(app.pendingApprovals.get()), href: '#/approvals', tone: app.pendingApprovals.get() > 0 ? 'medium' : undefined, sub: app.pendingApprovals.get() > 0 ? 'waiting on a human' : 'inbox zero' }),
      kpi({ label: 'Events / min', value: formatNumber(rate, rate < 10 ? 1 : 0), sub: `${formatNumber(s.events_total ?? live.feed.ring.total)} total` }),
      kpi({ label: 'Queue P0 / P1 / P2 / P3', value: ['P0', 'P1', 'P2', 'P3'].map((k) => depth[k] ?? 0).join(' / '), sub: q.backpressure ? badge('backpressure', 'critical') : `${q.in_flight ?? s.queue?.in_flight ?? 0} in flight` }),
      kpi({ label: 'Dead letters', value: formatNumber(dlq), tone: dlq > 0 ? 'critical' : undefined, sub: `${formatNumber(shed)} shed` }),
      kpi({ label: 'Last eval', value: lastEval && lastEval.mean_score != null ? Number(lastEval.mean_score).toFixed(2) : '-', href: '#/evals', sub: lastEval ? `${lastEval.eval_run_id} · ${lastEval.status}` : 'no runs yet' }),
    );
  };

  // ---- live feed -------------------------------------------------------------------------------------------
  const feedList = h('div', { class: 'ev-list', role: 'log', 'aria-live': 'off', 'aria-label': 'Live event feed', tabindex: '0' });
  const announcer = h('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
  const countLabel = h('span', { class: 'muted small' }, '');
  const pausedBanner = h('div', { class: 'ev-paused', hidden: true }, icon('pause', 'sm'), h('span', { id: 'paused-text' }), button('Resume', { size: 'sm', onclick: () => setPaused(false) }));

  const groupChips = EVENT_GROUPS.map((g) =>
    h('button', {
      class: 'chip',
      type: 'button',
      'aria-pressed': 'false',
      dataset: { key: g.key },
      onclick: (e) => {
        const i = filter.groups.indexOf(g.key);
        if (i >= 0) filter.groups.splice(i, 1);
        else filter.groups.push(g.key);
        e.currentTarget.setAttribute('aria-pressed', String(i < 0));
        renderFeed();
      },
    }, g.label),
  );
  const sevSel = selectEl([{ value: '', label: 'Any severity' }, ...['info', 'low', 'medium', 'high', 'critical'].map((s) => ({ value: s, label: `≥ ${s}` }))], '', (v) => { filter.minSeverity = v; renderFeed(); }, { size: 'sm', label: 'Minimum severity' });
  const compSel = selectEl([{ value: '', label: 'All components' }], '', (v) => { filter.component = v; renderFeed(); }, { size: 'sm', label: 'Component' });
  const textIn = inputEl({ size: 'sm', placeholder: 'Filter text', label: 'Filter events', oninput: debounce((e) => { filter.text = e.target.value; renderFeed(); }, 150) });
  const pauseBtn = button('Pause', { size: 'sm', icon: 'pause', onclick: () => setPaused(!paused), pressed: false, title: 'Freeze the feed while you read (events keep arriving in the background)' });

  function setPaused(p) {
    paused = p;
    pauseBtn.setAttribute('aria-pressed', String(p));
    pauseBtn.replaceChildren(icon(p ? 'play' : 'pause', 'sm'), p ? 'Resume' : 'Pause');
    if (!p) {
      pendingWhilePaused = 0;
      renderFeed();
    }
    pausedBanner.hidden = !p;
    updatePausedText();
  }
  function updatePausedText() {
    const t = pausedBanner.querySelector('#paused-text');
    if (t) t.textContent = pendingWhilePaused ? `Paused · ${pendingWhilePaused} new event${pendingWhilePaused === 1 ? '' : 's'} waiting` : 'Paused';
  }

  const openEvent = (e) => {
    selectedId = e.id;
    openEventDrawer(app, e.id);
    feedList.querySelectorAll('.ev-row.selected').forEach((x) => x.classList.remove('selected'));
    feedList.querySelector(`.ev-row[data-id="${CSS.escape(e.id || '')}"]`)?.classList.add('selected');
  };

  const knownComponents = new Set();
  function refreshComponentOptions() {
    const comps = [...new Set(live.feed.ring.toArray().map((e) => e.component).filter(Boolean))].sort();
    if (comps.length === knownComponents.size && comps.every((c) => knownComponents.has(c))) return;
    knownComponents.clear();
    comps.forEach((c) => knownComponents.add(c));
    const cur = compSel.value;
    mount(compSel, h('option', { value: '' }, 'All components'), comps.map((c) => h('option', { value: c }, c)));
    compSel.value = comps.includes(cur) ? cur : '';
  }

  function renderFeed() {
    refreshComponentOptions();
    const evs = live.feed.list(filter, FEED_LIMIT);
    if (!evs.length) {
      mount(feedList, emptyState({ icon: 'activity', title: live.feed.ring.size ? 'No events match these filters' : 'Waiting for events', text: live.feed.ring.size ? 'Clear a filter to see more.' : 'Launch a scenario, start a replay or inject an event.', action: h('div', { class: 'row' }, button('Scenarios', { href: '#/scenarios', size: 'sm' }), button('Fault injector', { href: '#/faults', size: 'sm' })) }));
    } else {
      mount(feedList, evs.map((e) => eventRow(e, { onOpen: openEvent, selected: e.id === selectedId })));
    }
    countLabel.textContent = `${evs.length} shown · ${formatNumber(live.feed.ring.size)} buffered`;
  }

  let announceAt = 0;
  let announceBuf = [];
  function announce(e) {
    announceBuf.push(e);
    const now = Date.now();
    if (now - announceAt < 5000) return;
    announceAt = now;
    const worst = announceBuf.reduce((a, b) => (severityRank(b.severity) > severityRank(a.severity) ? b : a));
    announcer.textContent = `${announceBuf.length} new event${announceBuf.length === 1 ? '' : 's'}. Latest notable: ${worst.type} ${worst.component || ''} ${worst.severity}`;
    announceBuf = [];
  }

  function prependRow(e, updated) {
    if (!matchEvent(filter, e)) return;
    if (updated) {
      renderFeed();
      return;
    }
    const first = feedList.firstElementChild;
    if (first && first.classList.contains('empty')) feedList.textContent = '';
    feedList.insertBefore(eventRow(e, { onOpen: openEvent, selected: e.id === selectedId }), feedList.firstChild);
    while (feedList.children.length > FEED_LIMIT) feedList.lastElementChild.remove();
    if (!knownComponents.has(e.component) && e.component) refreshComponentOptions();
  }

  const renderKpisSoon = rafThrottle(renderKpis);
  on('event', ({ event: e, backfill, updated }) => {
    if (backfill) return;
    if (paused) {
      pendingWhilePaused++;
      updatePausedText();
    } else prependRow(e, updated);
    announce(e);
    if (e.type.startsWith('dlq.')) loadDlq();
    if (e.type.startsWith('incident.') || e.type.startsWith('approval.') || e.type.startsWith('eval.')) live.refreshSummary();
    if (e.type.startsWith('replay.')) loadReplay();
    renderHeatSoon();
    renderKpisSoon();
  });
  on('backfill', renderFeed);
  on('summary', renderKpis);
  on('queue', () => {
    renderKpisSoon();
    renderQueues();
  });
  disposers.push(app.pendingApprovals.subscribe(() => renderKpisSoon()));
  on('approvals', (res) => renderApprovals(res.items || []));

  const feedCard = card({
    title: 'Live event feed',
    sub: countLabel,
    actions: [pauseBtn],
    flush: true,
    class: 'feed-card',
    body: h('div', { class: 'col', style: 'gap:0;min-height:0;flex:1' }, h('div', { class: 'ev-filters' }, h('div', { class: 'row wrap', role: 'group', 'aria-label': 'Event groups' }, groupChips), h('div', { class: 'row wrap grow' }, sevSel, compSel, h('div', { class: 'grow', style: 'min-width:120px' }, textIn))), pausedBanner, feedList, announcer),
  });

  // ---- approvals ------------------------------------------------------------------------------------------
  const approvalsBody = h('div', null, skeleton(3));
  function renderApprovals(items) {
    if (!items.length) {
      mount(approvalsBody, emptyState({ icon: 'inbox', title: 'No pending approvals', text: 'Risky actions will wait here for a human.' }));
      return;
    }
    mount(
      approvalsBody,
      h('ul', { class: 'plain-list' }, items.slice(0, 5).map((a) =>
        h('li', { class: 'pa-item' },
          h('div', { class: 'grow' },
            h('a', { class: 'strong', href: link('approvals', a.approval_id) }, truncate(a.title || a.approval_id, 64)),
            h('div', { class: 'row wrap small muted' }, badge(a.kind, 'info', { sq: true }), a.risk && badge(a.risk, a.risk === 'high' || a.risk === 'critical' ? 'high' : a.risk === 'medium' ? 'medium' : 'low'), a.incident_id && h('a', { href: link('incidents', a.incident_id), class: 'mono' }, a.incident_id), h('span', null, ago(a.requested_at)))),
          button('Review', { size: 'sm', href: link('approvals', a.approval_id), variant: 'primary' }),
        ),
      )),
      items.length > 5 && h('div', { class: 'small muted', style: 'padding:8px 0 0' }, `+ ${items.length - 5} more in the inbox`),
    );
  }
  async function loadApprovals() {
    try {
      renderApprovals((await app.api.approvals({ status: 'pending' })).items || []);
    } catch (err) {
      mount(approvalsBody, errorBox(err, loadApprovals));
    }
  }

  // ---- active runs ---------------------------------------------------------------------------------------
  const runsBody = h('div', null, skeleton(3));
  async function loadRuns() {
    try {
      const res = await app.api.dispatches({ status: 'running,interrupted,retrying,queued', limit: 12 });
      const items = res.items || [];
      if (!items.length) {
        mount(runsBody, emptyState({ icon: 'play', title: 'No active runs', text: 'Dispatches appear here while graphs execute.' }));
        return;
      }
      mount(
        runsBody,
        h('ul', { class: 'plain-list' }, items.map((d) =>
          h('li', { class: 'pa-item' },
            h('div', { class: 'grow' }, h('div', { class: 'row wrap' }, h('span', { class: 'mono strong' }, d.graph_id || d.route), statusBadge(d.status), d.priority && badge(d.priority, 'info', { sq: true })), h('div', { class: 'small muted' }, d.incident_id || d.event_id)),
            d.thread_id && button('Open', { size: 'sm', href: d.incident_id ? link('incidents', d.incident_id) : link('runs', d.thread_id) }),
          ),
        )),
      );
    } catch (err) {
      mount(runsBody, errorBox(err, loadRuns));
    }
  }
  on('dispatch', debounce(loadRuns, 400));

  // ---- queues ---------------------------------------------------------------------------------------------
  const queuesBody = h('div', null, skeleton(4));
  function renderQueues() {
    const q = live.state.queue;
    if (!q) {
      mount(queuesBody, skeleton(4));
      return;
    }
    const p = queuePressure(q);
    const hw = q.high_water ?? 0.8;
    mount(
      queuesBody,
      h('div', { class: 'col gap-3' },
        ['P0', 'P1', 'P2', 'P3'].map((k) =>
          h('div', { class: 'qrow', title: `${p[k].depth} of ${p[k].cap}` },
            h('span', { class: 'mono strong' }, k),
            h('div', { class: ['bar', `t-${p[k].tone === 'ok' ? 'ok' : p[k].tone}`], role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': p[k].cap, 'aria-valuenow': p[k].depth, 'aria-label': `${k} queue depth` }, h('i', { style: { width: Math.min(100, Math.max(p[k].depth ? 2 : 0, p[k].ratio * 100)) + '%' } }), h('span', { class: 'hw', style: { left: hw * 100 + '%' }, title: `high water ${formatPct(hw, 0)}` })),
            h('span', { class: 'num small' }, `${p[k].depth}/${p[k].cap}`),
          ),
        ),
        h('div', { class: 'row wrap small' },
          q.backpressure ? badge('backpressure active', 'critical', { dot: true, live: true }) : badge('flowing', 'ok', { dot: true }),
          h('span', { class: 'muted' }, `${q.in_flight ?? 0} in flight · ${q.workers ?? '?'} workers · ${q.interrupted ?? 0} interrupted`),
          h('span', { class: 'muted' }, `${formatNumber(q.rate_per_s ?? 0, 1)}/s`),
        ),
        q.per_graph && h('div', { class: 'row wrap' }, Object.entries(q.per_graph).map(([g, v]) => chip(`${g} ${v.running}/${v.cap}`, { mono: true }))),
      ),
    );
  }

  // ---- DLQ -----------------------------------------------------------------------------------------------
  const dlqBody = h('div', null, skeleton(3));
  async function loadDlq() {
    try {
      const res = await app.api.dlq({ status: 'open', limit: 8 });
      const items = res.items || [];
      if (!items.length) {
        mount(dlqBody, emptyState({ icon: 'check-circle', title: 'Dead-letter queue is empty', text: 'Poison, malformed and failed events land here.' }));
        return;
      }
      mount(dlqBody, h('ul', { class: 'plain-list' }, items.map((d) => dlqItem(d))));
    } catch (err) {
      mount(dlqBody, errorBox(err, loadDlq));
    }
  }
  function dlqItem(d) {
    const retry = button('Retry', { size: 'sm', icon: 'refresh', title: 'Re-dispatch exactly once (a failed run resumes from its last checkpoint)' });
    const discard = button('Discard', { size: 'sm', icon: 'trash', variant: 'danger' });
    asyncClick(retry, async () => {
      await app.api.dlqRetry(d.id);
      app.toast({ title: 'Retried', body: d.id, tone: 'ok', ttl: 2500 });
      loadDlq();
    }, (e) => app.toast({ title: 'Retry failed', body: errorMessage(e), tone: 'critical' }));
    asyncClick(discard, async () => {
      await app.api.dlqDiscard(d.id, { reason: 'discarded from Mission Control', by: app.persona.get() });
      loadDlq();
    }, (e) => app.toast({ title: 'Discard failed', body: errorMessage(e), tone: 'critical' }));
    return h('li', { class: 'pa-item' },
      h('div', { class: 'grow' }, h('div', { class: 'row wrap' }, badge(d.reason, d.poison ? 'critical' : 'high', { sq: true }), d.poison && badge('poison', 'critical'), h('span', { class: 'mono small' }, d.event_id || d.id)), h('div', { class: 'small muted truncate', title: d.last_error || '' }, `${d.attempts} attempt${d.attempts === 1 ? '' : 's'}${d.last_error ? ' · ' + d.last_error : ''}`)),
      h('div', { class: 'row' }, retry, discard),
    );
  }

  // ---- replay + clock ---------------------------------------------------------------------------------------
  const replayBody = h('div', null, skeleton(4));
  let replaySession = null;
  let streams = [];
  let replayDraft = { name: '', speed: 60, loop: false };
  async function loadReplay() {
    try {
      const [r, s, c] = await Promise.all([app.api.replay(), app.api.streams().catch(() => ({ items: [] })), app.api.clock().catch(() => null)]);
      replaySession = r.session;
      streams = s.items || [];
      if (!replayDraft.name && streams.length) replayDraft.name = streams.find((x) => /workday/.test(x.name))?.name || streams[0].name;
      if (c && !live.state.clock) live.state.clock = c;
      renderReplay();
    } catch (err) {
      mount(replayBody, errorBox(err, loadReplay));
    }
  }
  const act = (fn, okMsg) => async () => {
    try {
      const r = await fn();
      if (okMsg) app.toast({ title: okMsg, tone: 'ok', ttl: 2000 });
      return r;
    } catch (e) {
      app.toast({ title: 'Request failed', body: errorMessage(e), tone: 'critical' });
      return null;
    } finally {
      loadReplay();
    }
  };
  function renderReplay() {
    const s = replaySession;
    const running = s && (s.state === 'running' || s.state === 'paused');
    const clock = live.state.clock || {};
    const streamSel = selectEl(streams.length ? streams.map((x) => ({ value: x.name, label: `${x.name}${x.events ? ` (${x.events})` : ''}` })) : [{ value: '', label: 'No streams available' }], replayDraft.name, (v) => (replayDraft.name = v), { size: 'sm', label: 'Replay stream' });
    streamSel.disabled = !streams.length;
    const startBtn = button('Start replay', { icon: 'play', variant: 'primary', size: 'sm', disabled: !streams.length || running });
    asyncClick(startBtn, act(() => app.api.replayStart({ stream: replayDraft.name, speed: replayDraft.speed, loop: replayDraft.loop, replace: false }), 'Replay started'));
    const ctrl = (icn, label, action, body) => {
      const b = iconButton(icn, label, null, { size: 'sm', disabled: !running });
      asyncClick(b, act(() => app.api.replayAction(action, body)));
      return b;
    };
    mount(
      replayBody,
      h('div', { class: 'col gap-3' },
        s ? h('div', { class: 'col' }, h('div', { class: 'row wrap' }, statusBadge(s.state), h('span', { class: 'mono small' }, s.name), h('span', { class: 'muted small' }, `${s.speed}x`), s.backpressure_paused && badge('paused by backpressure', 'medium')), h('div', { class: 'bar', role: 'progressbar', 'aria-valuenow': s.position, 'aria-valuemax': s.total, 'aria-label': 'Replay progress' }, h('i', { style: { width: (s.total ? (s.position / s.total) * 100 : 0) + '%' } })), h('div', { class: 'small muted' }, `${formatNumber(s.position)} / ${formatNumber(s.total)} events · virtual ${formatDateTime(s.virtual_at)}`)) : h('div', { class: 'small muted' }, 'No replay session. Pick a stream and start one.'),
        h('div', { class: 'row wrap' }, streamSel, seg([{ value: 1, label: '1x' }, { value: 10, label: '10x' }, { value: 60, label: '60x' }, { value: 600, label: '600x' }], replayDraft.speed, async (v) => {
          replayDraft.speed = v;
          if (running) await act(() => app.api.replayAction('speed', { speed: v }))();
        }, { label: 'Replay speed' })),
        h('div', { class: 'row wrap' }, startBtn, ctrl('pause', 'Pause replay', 'pause'), ctrl('play', 'Resume replay', 'resume'), ctrl('step', 'Release one event', 'step'), ctrl('stop', 'Stop replay', 'stop'), switchEl('Loop', replayDraft.loop, (v) => (replayDraft.loop = v))),
        h('hr', { style: 'margin:4px 0' }),
        h('div', { class: 'row wrap' }, h('span', { class: 'small strong' }, 'Virtual clock'), seg([{ value: 1, label: '1x' }, { value: 10, label: '10x' }, { value: 60, label: '60x' }, { value: 600, label: '600x' }], clock.speed || 60, (async (v) => { await app.api.setClock({ speed: v }).catch((e) => app.toast({ title: 'Clock', body: errorMessage(e), tone: 'critical' })); }), { label: 'Clock speed' }), (() => {
          const b = button(clock.paused ? 'Resume' : 'Pause', { size: 'sm', icon: clock.paused ? 'play' : 'pause' });
          asyncClick(b, act(() => app.api.setClock({ paused: !clock.paused })));
          return b;
        })()),
        h('div', { class: 'row wrap' }, switchEl('Ambient background traffic', !!app.config?.events?.ambient_enabled, async (v) => {
          try {
            await app.api.adminAmbient(v);
            app.toast({ title: v ? 'Ambient traffic on' : 'Ambient traffic off', tone: 'ok', ttl: 2000 });
          } catch (e) {
            app.toast({ title: 'Ambient', body: errorMessage(e), tone: 'critical' });
          }
        }), h('span', { class: 'tiny muted' }, 'replays a quiet hour in a loop')),
      ),
    );
  }

  // ---- service heat -----------------------------------------------------------------------------------------
  const heatBody = h('div', null, skeleton(3));
  let services = [];
  function renderHeat() {
    const open = new Map(); // component -> highest unresolved severity from the feed
    for (const e of live.feed.ring) {
      if (!e.component) continue;
      if (e.type === 'alert.fired') open.set(e.component, Math.max(open.get(e.component) ?? -1, severityRank(e.severity)));
      if (e.type === 'alert.resolved') open.delete(e.component);
      if (e.type === 'incident.resolved') open.delete(e.component);
    }
    if (!services.length) {
      mount(heatBody, emptyState({ icon: 'grid', title: 'Service catalog unavailable', text: 'The data layer did not answer GET /api/services.' }));
      return;
    }
    mount(
      heatBody,
      h('div', { class: 'heat-grid', role: 'list' }, services.map((svc) => {
        const name = svc.id || svc.name || svc.component;
        const lvl = open.get(name);
        const tone = lvl === undefined ? 'ok' : lvl >= 4 ? 'critical' : lvl >= 3 ? 'high' : lvl >= 2 ? 'medium' : 'low';
        return h('button', {
          class: ['heat-tile', `t-${tone}`, filter.component === name && 'selected'],
          role: 'listitem',
          type: 'button',
          title: `${name}${svc.kind ? ' (' + svc.kind + ')' : ''}: ${lvl === undefined ? 'no open alerts' : 'open ' + tone + ' alert'}. Click to filter the feed.`,
          onclick: () => {
            filter.component = filter.component === name ? '' : name;
            compSel.value = filter.component;
            renderFeed();
            renderHeat();
          },
        }, h('span', { class: 'dot' }), h('span', { class: 'truncate' }, name));
      })),
      h('div', { class: 'tiny muted', style: 'margin-top:8px' }, 'Derived from unresolved alert.fired events in the live feed; click a tile to filter by component.'),
    );
  }
  const renderHeatSoon = rafThrottle(renderHeat);
  async function loadServices() {
    try {
      services = (await app.api.services()).items || [];
    } catch {
      services = [];
    }
    renderHeat();
  }

  // ---- layout -----------------------------------------------------------------------------------------------
  mount(
    root,
    head,
    kpis,
    h('div', { class: 'mc-grid' },
      h('div', { class: 'mc-main' }, feedCard),
      h('div', { class: 'mc-side' },
        card({ title: 'Pending approvals', actions: button('Inbox', { size: 'sm', href: '#/approvals' }), body: approvalsBody }),
        card({ title: 'Active runs', body: runsBody }),
        card({ title: 'Queues and backpressure', body: queuesBody }),
        card({ title: 'Dead letters', body: dlqBody }),
        card({ title: 'Replay and clock', body: replayBody }),
      ),
    ),
    card({ title: 'Service heat', sub: '20 components', body: heatBody, class: 'mt-4' }),
  );
  renderKpis();
  renderFeed();
  renderQueues();
  loadApprovals();
  loadRuns();
  loadDlq();
  loadReplay();
  loadServices();
  timers.push(setInterval(renderKpis, 5000), setInterval(loadRuns, 15000));
  if (route.params.id) openEventDrawer(app, route.params.id);

  return () => {
    disposers.forEach((d) => d());
    timers.forEach(clearInterval);
  };
}
