// Pieces used by several views: Studio buttons, event rows, the event drawer, small helpers.
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, copyButton, drawer, sevBadge, statusBadge, jsonViewer, skeleton, errorBox, chip, incBadge } from '../core/ui.js';
import { formatClock, formatDateTime, shortId } from '../core/format.js';
import { summarizeEvent, toneOfEvent } from '../core/events.js';
import { link } from '../core/router.js';

/** "Open in Studio" + "Copy thread id" for a thread. */
export function studioThreadButtons(app, threadId, graphId, { size = 'sm' } = {}) {
  if (!threadId) return null;
  return h(
    'span',
    { class: 'row' },
    button('Open in Studio', { size, icon: 'external', href: app.studioThread(threadId, graphId), external: true, title: 'Open this thread in LangGraph Studio (the per-thread link format is unverified; the base link and thread id are always available)' }),
    copyButton(threadId, 'Copy thread id', app.toast),
  );
}

export function personaName(app) {
  return app.persona.get();
}

/** Small colored lane/agent tag. */
export function agentTag(name) {
  const s = String(name || '');
  const n = [...s].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % 8;
  return h('span', { class: 'agent-tag', style: { '--lane': `var(--c${n + 1})` } }, s);
}

export function dispatchChip(d) {
  if (!d) return null;
  const tone = { running: 'accent', succeeded: 'ok', failed: 'critical', dead: 'critical', interrupted: 'medium', retrying: 'medium', queued: 'info' }[d.status] || 'info';
  return badge(`${d.graph_id || d.route || 'dispatch'} · ${d.status}`, tone, { outline: true });
}

export function eventRow(e, { onOpen, selected } = {}) {
  const tone = toneOfEvent(e);
  const sup = e.status === 'suppressed' || e.status === 'shed' || e.status === 'dlq';
  const row = h(
    'div',
    {
      class: ['ev-row', `t-${tone}`, sup && 'ev-dim', selected && 'selected'],
      role: 'button',
      tabindex: '0',
      dataset: { id: e.id || '', seq: e.seq ?? '' },
      onclick: () => onOpen && onOpen(e),
      onkeydown: (ev) => (ev.key === 'Enter' || ev.key === ' ') && (ev.preventDefault(), onOpen && onOpen(e)),
      'aria-label': `${e.type} ${e.component || ''} ${e.severity}`,
    },
    h('span', { class: 'ev-bar' }),
    h('span', { class: 'ev-time mono', title: `virtual ${e.occurred_at || ''}` }, formatClock(e.occurred_at)),
    h('span', { class: 'ev-type mono' }, e.type),
    h('span', { class: 'ev-main' }, h('span', { class: 'ev-comp' }, e.component || ''), h('span', { class: 'ev-sum' }, summaryAfterComponent(e))),
    h('span', { class: 'ev-tags' }, e.occurrences > 1 && badge(`x${e.occurrences}`, 'accent', { sq: true, title: `${e.occurrences} occurrences coalesced` }), sup && badge(e.status, e.status === 'dlq' ? 'critical' : 'info', { sq: true }), (e.dispatches || []).slice(0, 1).map(dispatchChip), e.correlation_id && h('span', { class: 'ev-corr mono muted' }, e.correlation_id)),
  );
  return row;
}

function summaryAfterComponent(e) {
  const s = summarizeEvent(e);
  return e.component && s.startsWith(e.component) ? s.slice(e.component.length).trim() : s;
}

/** Slide-over with everything about one event: envelope, links, causal chain. */
export function openEventDrawer(app, eventOrId) {
  const id = typeof eventOrId === 'string' ? eventOrId : eventOrId.id;
  const d = drawer({ title: id || 'Event', wide: true, onClose: () => { if (location.hash.startsWith('#/events/')) history.replaceState(null, '', '#/'); }, body: skeleton(8) });
  (async () => {
    try {
      const [ev, links, trace] = await Promise.all([
        typeof eventOrId === 'string' || !eventOrId.payload ? app.api.event(id) : Promise.resolve(eventOrId),
        app.api.linksFor(id).catch(() => null),
        app.api.trace(id).catch(() => null),
      ]);
      d.setTitle(`${ev.type}  ${shortId(ev.id, 14)}`);
      const l = links || ev.links || {};
      const threadId = l.thread_id || ev.dispatches?.find((x) => x.thread_id)?.thread_id;
      d.setBody(
        h(
          'div',
          { class: 'col gap-4' },
          h('div', { class: 'row wrap' }, sevBadge(ev.severity), badge(ev.status || 'accepted', ev.status === 'suppressed' ? 'info' : 'accent', { outline: true }), ev.occurrences > 1 && badge(`x${ev.occurrences}`, 'accent', { sq: true }), ev.scenario_id && chip(ev.scenario_id, { mono: true }), l.incident_id && h('a', { class: 'chip', href: link('incidents', l.incident_id) }, l.incident_id)),
          h('p', { class: 'strong' }, summarizeEvent(ev)),
          h(
            'dl',
            { class: 'kv' },
            ...[
              ['component', ev.component],
              ['source', ev.source],
              ['occurred (virtual)', formatDateTime(ev.occurred_at)],
              ['received (real)', ev.received_at],
              ['correlation', ev.correlation_id],
              ['causation', ev.causation_id],
              ['dedupe key', ev.dedupe_key],
              ['idempotency key', ev.idempotency_key],
              ['trace', ev.trace && Object.values(ev.trace).some(Boolean) ? JSON.stringify(ev.trace) : null],
            ].flatMap(([k, v]) => (v ? [h('dt', null, k), h('dd', { class: 'mono' }, String(v))] : [])),
          ),
          h(
            'div',
            { class: 'row wrap' },
            threadId && button('Open run', { size: 'sm', icon: 'play', href: l.incident_id ? link('incidents', l.incident_id) : link('runs', threadId), variant: 'primary' }),
            threadId && studioThreadButtons(app, threadId),
            button('Studio (server)', { size: 'sm', icon: 'external', href: app.studioUrl(), external: true }),
            copyButton(ev.id, 'Copy event id', app.toast),
          ),
          trace && trace.chain && trace.chain.length > 1 && h('section', null, h('h4', { class: 'mb-2' }, 'Causal chain'), h('ol', { class: 'chain' }, trace.chain.map((c) => h('li', { class: c.id === id ? 'here' : '' }, h('a', { href: link('events', c.id) }, c.type), ' ', h('span', { class: 'muted small' }, summarizeEvent(c)))))),
          (ev.dispatches || trace?.dispatches || []).length > 0 && h('section', null, h('h4', { class: 'mb-2' }, 'Dispatches'), h('div', { class: 'col' }, (ev.dispatches || trace.dispatches).map((x) => h('div', { class: 'row' }, statusBadge(x.status), h('span', { class: 'mono small' }, `${x.graph_id || x.route} · ${x.priority || ''} · attempts ${x.attempts ?? 0}`), x.error && h('span', { class: 'small', style: 'color:var(--critical)' }, x.error))))),
          h('section', null, h('h4', { class: 'mb-2' }, 'Envelope'), jsonViewer(ev, { open: 2 })),
        ),
      );
    } catch (err) {
      d.setBody(errorBox(err));
    }
  })();
  return d;
}

export function incidentTitleRow(inc) {
  return h('div', { class: 'row wrap' }, h('strong', { class: 'mono' }, inc.incident_id), incBadge(inc.severity), statusBadge(inc.status), inc.scenario_id && chip(inc.scenario_id, { mono: true }));
}

/** Render a pending count text. */
export function ago(isoOrMs, now = Date.now()) {
  const t = typeof isoOrMs === 'number' ? isoOrMs : Date.parse(isoOrMs || '');
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export { icon, mount };
