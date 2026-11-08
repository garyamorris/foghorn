// Memory viewer: Store namespaces, semantic search, item inspector with provenance, edit and delete.
import { h, mount, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, confirmDialog, emptyState, errorBox, errorMessage, inputEl, jsonViewer, notice, skeleton, table, textareaEl, viewHead, asyncClick, tabs, field } from '../core/ui.js';
import { link } from '../core/router.js';
import { formatDateTime, formatScore, safeJson, truncate } from '../core/format.js';
import { ago } from './shared.js';

const nsKey = (ns) => (Array.isArray(ns) ? ns.join('.') : String(ns));
const textOf = (v) => (v && typeof v === 'object' ? v.text || v.summary || v.lesson || v.title || v.preference || v.description || JSON.stringify(v) : String(v ?? ''));

export default async function memory(root, route, app) {
  let nsList = [];
  let nsPath = route.params.rest ? route.params.rest.split('/').filter(Boolean) : [];
  let selectedKey = route.query.key || null;
  let query = route.query.q || '';
  let items = [];
  let mode = query ? 'search' : 'browse';
  const disposers = [];
  const recent = [];

  const treeEl = h('div', { class: 'col', style: 'gap:0' });
  const listEl = h('div');
  const itemEl = h('div');
  const recentEl = h('div');
  const searchIn = inputEl({ placeholder: 'Semantic search (e.g. "sessions cache stampede on monday")', label: 'Search memory', value: query, onkeydown: (e) => { if (e.key === 'Enter') { query = e.target.value.trim(); mode = query ? 'search' : 'browse'; run(); } } });
  const searchBtn = button('Search', { icon: 'search', variant: 'primary', onclick: () => { query = searchIn.value.trim(); mode = query ? 'search' : 'browse'; run(); } });

  mount(root, viewHead({ title: 'Memory', sub: 'The Store: past incidents, runbooks, preferences, lessons and people. Search is semantic; edits and deletes write straight through the Store API.' }),
    h('div', { class: 'row mb-3' }, h('div', { class: 'grow' }, searchIn), searchBtn),
    h('div', { class: 'mem-layout' },
      h('aside', { class: 'card', 'aria-label': 'Namespaces' }, h('div', { class: 'card-head' }, h('h3', null, 'Namespaces')), h('div', { class: 'card-body flush scroll', style: 'max-height:55vh' }, treeEl), h('div', { class: 'card-head', style: 'border-top:1px solid var(--border)' }, h('h3', null, 'Recent writes')), h('div', { class: 'card-body' }, recentEl)),
      h('section', { class: 'col gap-4' }, card({ title: mode === 'search' ? `Results for "${query}"` : nsPath.length ? `Items in ${nsPath.join(' / ')}` : 'Items', body: listEl, flush: true }), itemEl),
    ));
  mount(treeEl, skeleton(6));
  mount(recentEl, h('div', { class: 'small muted' }, 'Memory writes appear here live.'));

  try {
    nsList = (await app.api.memNamespaces({ max_depth: 3 })).items || [];
  } catch (err) {
    mount(treeEl, errorBox(err, () => location.reload(), 'Cannot read namespaces'));
  }

  function tree() {
    if (!nsList.length) {
      mount(treeEl, emptyState({ icon: 'chip', title: 'No namespaces', text: 'The Store is empty. Seeding runs at server start.' }));
      return;
    }
    const roots = new Map();
    for (const ns of nsList) {
      const r = ns[0];
      if (!roots.has(r)) roots.set(r, []);
      roots.get(r).push(ns);
    }
    mount(treeEl, [h('button', { class: ['tree-row', !nsPath.length && mode !== 'search' && 'selected'], type: 'button', onclick: () => go([]) }, icon('layers', 'sm'), 'All')].concat([...roots.entries()].sort().map(([r, children]) => {
      const kids = children.filter((c) => c.length > 1);
      return h('div', null,
        h('button', { class: ['tree-row', nsPath.length === 1 && nsPath[0] === r && 'selected'], type: 'button', onclick: () => go([r]) }, icon('database', 'sm'), h('span', { class: 'grow' }, r)),
        kids.map((c) => h('button', { class: ['tree-row child', nsKey(c) === nsKey(nsPath) && 'selected'], type: 'button', onclick: () => go(c) }, h('span', { class: 'grow truncate' }, c.slice(1).join(' / ')))),
      );
    })));
  }
  function go(path) {
    nsPath = path;
    mode = 'browse';
    query = '';
    searchIn.value = '';
    selectedKey = null;
    history.replaceState(null, '', link('memory', ...path));
    tree();
    run();
  }

  function renderList() {
    if (!items.length) {
      mount(listEl, emptyState({ icon: 'search', title: mode === 'search' ? 'Nothing matched' : 'No items here', text: mode === 'search' ? 'Try different words. Search is semantic over the indexed text field.' : 'Choose another namespace.' }));
      return;
    }
    mount(listEl, table({ caption: 'Store items', rows: items, rowKey: (r) => nsKey(r.namespace) + '/' + r.key, selected: selectedKey && nsKey(nsPath) + '/' + selectedKey, onRow: (r) => openItem(r), columns: [
      ...(mode === 'search' ? [{ label: 'Score', num: true, render: (r) => (r.score != null ? h('span', { class: 'mono' }, formatScore(r.score)) : '') }] : []),
      { label: 'Namespace', render: (r) => h('span', { class: 'mono small' }, nsKey(r.namespace).replace(/\./g, ' / ')) },
      { label: 'Key', render: (r) => h('span', { class: 'mono strong' }, r.key) },
      { label: 'Text', wrap: true, render: (r) => h('span', { class: 'untrusted-text' }, truncate(textOf(r.value), 140)) },
      { label: 'Updated', render: (r) => h('span', { class: 'muted small' }, r.updated_at ? ago(r.updated_at) : '') },
    ] }));
  }

  async function run() {
    mount(listEl, skeleton(5));
    try {
      if (mode === 'search') {
        const res = await app.api.memSearch({ q: query, namespace: nsPath.length ? nsPath.join('.') : undefined, limit: 15 });
        items = res.items || [];
      } else {
        const res = await app.api.storeSearch({ namespace_prefix: nsPath, limit: 50, offset: 0 });
        items = Array.isArray(res) ? res : res.items || [];
      }
      renderList();
      if (selectedKey) {
        const hit = items.find((i) => i.key === selectedKey);
        if (hit) openItem(hit);
      }
    } catch (err) {
      mount(listEl, errorBox(err, run, 'Cannot read the Store'));
    }
  }

  function provenance(v) {
    const out = [];
    const p = v?.provenance || v?.source || {};
    const add = (label, val, href) => val && out.push(href ? h('a', { class: 'chip mono', href }, `${label} ${truncate(String(val), 24)}`) : chip(`${label} ${truncate(String(val), 24)}`, { mono: true }));
    add('incident', p.incident_id || v?.incident_id, link('incidents', p.incident_id || v?.incident_id || ''));
    add('thread', p.thread_id || v?.thread_id, link('threads', p.thread_id || v?.thread_id || ''));
    add('event', p.event_id || v?.event_id, link('events', p.event_id || v?.event_id || ''));
    add('scenario', p.scenario_id || v?.scenario_id);
    return out;
  }

  function openItem(it) {
    selectedKey = it.key;
    const ns = Array.isArray(it.namespace) ? it.namespace : String(it.namespace).split('.');
    const ta = textareaEl({ rows: 12, mono: true, value: safeJson(it.value), label: 'Item value (JSON)', spellcheck: false });
    const status = h('div');
    const edit = h('div', { hidden: true, class: 'col gap-3' }, ta, h('div', { class: 'row' }, (() => {
      const b = button('Save', { variant: 'primary', icon: 'check' });
      asyncClick(b, async () => {
        let val;
        try {
          val = JSON.parse(ta.value);
        } catch (e) {
          mount(status, h('div', { class: 'err small' }, `Not valid JSON: ${e.message}`));
          return;
        }
        await app.api.storePut({ namespace: ns, key: it.key, value: val });
        it.value = val;
        mount(status, notice('Saved to the Store.', 'ok', 'check-circle'));
        edit.hidden = true;
        renderList();
        openItem(it);
      }, (e) => mount(status, errorBox(e, null, 'Save failed')));
      return b;
    })(), button('Cancel', { onclick: () => (edit.hidden = true) })));
    const del = button('Delete', { variant: 'danger', icon: 'trash', size: 'sm' });
    asyncClick(del, async () => {
      const ok = await confirmDialog({ title: 'Delete this memory?', text: `${ns.join(' / ')} / ${it.key} will be removed from the Store. This cannot be undone.`, confirmLabel: 'Delete', danger: true });
      if (!ok) return;
      await app.api.storeDelete({ namespace: ns, key: it.key });
      items = items.filter((x) => x !== it);
      mount(itemEl);
      renderList();
      app.toast({ title: 'Deleted', body: it.key, tone: 'ok', ttl: 2500 });
    }, (e) => mount(status, errorBox(e, null, 'Delete failed')));
    mount(itemEl, card({ title: h('span', { class: 'mono' }, it.key), sub: ns.join(' / '), actions: [button('Edit', { size: 'sm', icon: 'edit', onclick: () => (edit.hidden = !edit.hidden) }), del], body: h('div', { class: 'col gap-3' },
      h('div', { class: 'row wrap' }, it.score != null && badge(`score ${formatScore(it.score)}`, 'accent', { sq: true }), it.created_at && chip(`created ${formatDateTime(it.created_at)}`), it.updated_at && chip(`updated ${formatDateTime(it.updated_at)}`), provenance(it.value)),
      h('div', { class: 'untrusted-text' }, textOf(it.value)),
      status, edit, jsonViewer(it.value, { open: 2 }),
    ) }));
    itemEl.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }

  disposers.push(app.live.on('event', ({ event: e, backfill }) => {
    if (e.type !== 'memory.written' && e.type !== 'memory.recalled') return;
    if (backfill && recent.length > 5) return;
    recent.unshift(e);
    recent.length = Math.min(recent.length, 8);
    mount(recentEl, h('ul', { class: 'plain-list' }, recent.map((x) => h('li', { class: 'small row top' }, badge(x.type.split('.')[1], x.type === 'memory.written' ? 'ok' : 'accent', { sq: true }), h('span', { class: 'mono truncate' }, `${x.payload?.ns || ''}/${x.payload?.key || ''}`)))));
    if (!backfill && x_isCurrent(e)) run();
  }));
  const x_isCurrent = (e) => mode === 'browse' && (!nsPath.length || String(e.payload?.ns || '').startsWith(nsPath.join('.')));

  tree();
  await run();
  return () => disposers.forEach((d) => d());
}
