// Approvals inbox: forms generated from each interrupt's response_schema; parallel interrupts resume together.
import { h, mount, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, emptyState, errorBox, jsonViewer, riskBadge, selectEl, skeleton, statusBadge, table, tabs, viewHead, incBadge } from '../core/ui.js';
import { link } from '../core/router.js';
import { formatDateTime, truncate } from '../core/format.js';
import { ago, studioThreadButtons } from './shared.js';
import { interruptGroup } from './interrupts.js';

const PERSONAS = ['on-call', 'incident-commander', 'dba', 'payments-lead'];

export default async function approvals(root, route, app) {
  let tab = route.query.tab === 'history' ? 'history' : 'pending';
  let kindFilter = '';
  let selectedId = route.params.id || null;
  let pending = [];
  let history = [];
  let dead = false;
  const disposers = [];

  const listEl = h('div', { class: 'col', style: 'gap:0' });
  const detailEl = h('div', { class: 'col gap-4' });
  const kindSel = selectEl([{ value: '', label: 'All kinds' }, ...['approval', 'edit', 'question', 'break_glass', 'memory_write', 'tool_review', 'four_eyes'].map((k) => ({ value: k, label: k }))], '', (v) => { kindFilter = v; renderList(); }, { size: 'sm', label: 'Filter by kind' });
  const personaSel = selectEl(PERSONAS, app.persona.get(), (v) => app.persona.set(v), { size: 'sm', label: 'Acting as' });
  disposers.push(app.persona.subscribe((p) => { personaSel.value = p; renderDetail(); }));

  const tabsEl = tabs([{ key: 'pending', label: 'Pending' }, { key: 'history', label: 'History' }], tab, (k) => { tab = k; selectedId = null; if (location.hash.startsWith('#/approvals/')) history_replace('#/approvals'); load(); }, { label: 'Approval lists' });
  function history_replace(h2) {
    window.history.replaceState(null, '', h2);
  }

  mount(
    root,
    viewHead({
      title: 'Approvals',
      sub: 'Every risky action waits here for a human. Forms are generated from the interrupt’s response schema, so new interrupt kinds need no UI code.',
      actions: [h('span', { class: 'row small' }, h('span', { class: 'muted' }, 'Acting as'), personaSel)],
    }),
    h('div', { class: 'split' },
      h('aside', { class: 'card split-list', 'aria-label': 'Approvals' }, h('div', { class: 'card-head' }, tabsEl, h('span', { class: 'grow' }), kindSel), h('div', { class: 'card-body flush scroll', style: 'max-height:calc(100vh - 250px)' }, listEl)),
      h('section', { class: 'split-detail', 'aria-label': 'Approval detail', 'aria-live': 'polite' }, detailEl),
    ),
  );

  function rowOf(a) {
    return h('button', { class: ['inbox-row', a.approval_id === selectedId && 'selected'], type: 'button', dataset: { id: a.approval_id }, onclick: () => select(a.approval_id), 'aria-current': a.approval_id === selectedId ? 'true' : undefined },
      h('div', { class: 'row between' }, h('span', { class: 'strong truncate' }, truncate(a.title || a.approval_id, 60)), h('span', { class: 'tiny muted nowrap' }, ago(tab === 'history' ? a.decided_at : a.requested_at))),
      h('div', { class: 'row wrap small' }, badge(a.kind, 'info', { sq: true }), a.risk && riskBadge(a.risk), a.incident_id && h('span', { class: 'mono muted' }, a.incident_id), tab === 'history' && a.decision && badge(typeof a.decision === 'string' ? a.decision : a.decision.decision || 'decided', 'ok', { sq: true })),
    );
  }

  function renderList() {
    const items = (tab === 'pending' ? pending : history).filter((a) => !kindFilter || a.kind === kindFilter);
    if (!items.length) {
      mount(listEl, emptyState({ icon: 'inbox', title: tab === 'pending' ? 'Inbox zero' : 'No decisions yet', text: tab === 'pending' ? 'Launch a scenario that needs approval (S01, S10, S11) and it will appear here.' : 'Decided approvals are listed here.', action: tab === 'pending' ? button('Scenarios', { href: '#/scenarios', size: 'sm' }) : undefined }));
      return;
    }
    // group by incident so parallel interrupts sit together
    const groups = new Map();
    for (const a of items) {
      const k = a.incident_id || a.thread_id || a.approval_id;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(a);
    }
    mount(listEl, [...groups.entries()].map(([k, arr]) => h('div', { class: 'inbox-group' }, arr.length > 1 && h('div', { class: 'inbox-group-head tiny muted' }, `${k} · ${arr.length} parallel`), arr.map(rowOf))));
  }

  async function renderDetail() {
    const list = tab === 'pending' ? pending : history;
    const sel = list.find((a) => a.approval_id === selectedId) || null;
    if (!sel) {
      if (selectedId && tab === 'pending') {
        // deep link to something not pending (decided or stale)
        try {
          const row = await app.api.approval(selectedId);
          if (!dead) return renderHistoryDetail(row);
        } catch {
          /* fall through */
        }
      }
      mount(detailEl, emptyState({ icon: 'inbox', title: list.length ? 'Select an approval' : 'Nothing selected', text: list.length ? 'Pick one from the list. Shortcuts: a approve, e edit, r reject, ? ask.' : undefined }));
      return;
    }
    if (tab === 'history') return renderHistoryDetail(sel);

    // all pending interrupts on the same thread decide together
    const siblings = pending.filter((a) => a.thread_id === sel.thread_id && a.interrupt_id);
    const ordered = [sel, ...siblings.filter((s) => s.approval_id !== sel.approval_id)];
    const interrupts = ordered.map((a) => ({ id: a.interrupt_id, value: a.payload || { kind: a.kind, title: a.title, summary: a.summary, risk: a.risk }, response_schema: a.response_schema }));
    const group = interruptGroup(app, {
      threadId: sel.thread_id,
      graphId: sel.graph_id,
      interrupts,
      onResumed: async () => {
        const next = pending.find((a) => a.thread_id !== sel.thread_id);
        selectedId = next ? next.approval_id : null;
        await load(true);
        app.refreshApprovals();
      },
    });
    group.cards.forEach((c, i) => { c.el.dataset.approvalId = ordered[i].approval_id; });
    currentGroup = group;
    mount(
      detailEl,
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('div', { class: 'row wrap' }, sel.incident_id && h('a', { class: 'mono strong', href: link('incidents', sel.incident_id) }, sel.incident_id), h('span', { class: 'muted small' }, sel.graph_id), h('span', { class: 'muted small' }, `requested ${ago(sel.requested_at)}`))), button('Open console', { size: 'sm', icon: 'play', href: sel.incident_id ? link('incidents', sel.incident_id) : link('runs', sel.thread_id) }), studioThreadButtons(app, sel.thread_id, sel.graph_id)),
        h('div', { class: 'card-body' }, ordered.length > 1 && h('div', { class: 'notice t-accent mb-3' }, icon('layers'), h('div', { class: 'small' }, `${ordered.length} interrupts are pending on this thread. Choose a decision on each card, then Resume all: they are sent as one {interrupt_id: decision} map.`)), group.el),
      ),
    );
    if (location.hash !== `#/approvals/${encodeURIComponent(sel.approval_id)}`) history_replace(`#/approvals/${encodeURIComponent(sel.approval_id)}`);
    document.title = `${sel.title || sel.approval_id} - Approvals - Foghorn`;
  }
  let currentGroup = null;

  function renderHistoryDetail(a) {
    mount(
      detailEl,
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('h3', { class: 'grow' }, a.title || a.approval_id), statusBadge(a.status)),
        h('div', { class: 'card-body col gap-3' },
          h('dl', { class: 'kv' }, ...[['approval', a.approval_id], ['kind', a.kind], ['incident', a.incident_id], ['thread', a.thread_id], ['decided by', a.decided_by], ['decided at', a.decided_at && formatDateTime(a.decided_at)], ['requested', a.requested_at && formatDateTime(a.requested_at)]].flatMap(([k, v]) => (v ? [h('dt', null, k), h('dd', { class: 'mono' }, String(v))] : []))),
          a.summary && h('p', { class: 'untrusted-text' }, a.summary),
          a.decision && h('div', null, h('h4', { class: 'mb-2' }, 'Decision'), jsonViewer(a.decision, { open: 3 })),
          a.payload && h('div', null, h('h4', { class: 'mb-2' }, 'Request'), jsonViewer(a.payload, { open: 1 })),
          h('div', { class: 'row' }, a.incident_id && button('Open incident', { href: link('incidents', a.incident_id), icon: 'play' }), studioThreadButtons(app, a.thread_id, a.graph_id)),
        ),
      ),
    );
  }

  function select(id) {
    selectedId = id;
    renderList();
    renderDetail();
  }

  async function load(quiet) {
    if (!quiet) {
      mount(listEl, skeleton(5));
    }
    try {
      if (tab === 'pending') {
        const res = await app.api.approvals({ status: 'pending' });
        pending = res.items || [];
        if (res.scan_ok === false) app.toast({ title: 'Agent Server not reachable', body: 'Showing the cached approvals projection.', tone: 'medium', ttl: 4000 });
        app.pendingApprovals.set(pending.length);
      } else {
        history = (await app.api.approvals({ status: 'decided', limit: 100 })).items || [];
      }
      if (dead) return;
      const list = tab === 'pending' ? pending : history;
      if (!selectedId && list.length && window.innerWidth > 900) selectedId = list[0].approval_id;
      if (selectedId && !list.some((a) => a.approval_id === selectedId) && !route.params.id) selectedId = list[0]?.approval_id || null;
      renderList();
      renderDetail();
    } catch (err) {
      mount(listEl, errorBox(err, () => load(), 'Cannot load approvals'));
    }
  }

  const reload = debounce(() => load(true), 400);
  disposers.push(app.live.on('event', ({ event: e, backfill }) => { if (!backfill && e.type.startsWith('approval.')) reload(); }));
  const poll = setInterval(() => load(true), 20000);

  const onKey = (e) => {
    if (e.target.matches?.('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
    if (!currentGroup || tab !== 'pending') return;
    const map = { a: 'approve', r: 'reject', e: 'edit', '?': 'ask' };
    const action = map[e.key];
    if (!action) return;
    const focused = document.activeElement?.closest?.('.icard');
    const card = currentGroup.cards.find((c) => c.el === focused) || currentGroup.cards[0];
    if (card && card.form.actions?.includes(action)) {
      e.preventDefault();
      if (action === 'edit' || action === 'ask') {
        card.form.setAction(action);
        card.el.querySelector('textarea, input:not([type=checkbox])')?.focus();
        const area = card.el.querySelector(`[data-field="${action === 'edit' ? 'edits' : 'question'}"] textarea, [data-field="${action === 'edit' ? 'edits' : 'question'}"] input`);
        area?.focus();
      } else card.trigger(action);
    }
  };
  document.addEventListener('keydown', onKey);

  await load();
  return () => {
    dead = true;
    clearInterval(poll);
    document.removeEventListener('keydown', onKey);
    disposers.forEach((d) => d());
  };
}
