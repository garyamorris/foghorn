// Dataset browser: catalogue, paged table, JSON/markdown viewer, JSONL export, stream replay, ground-truth reveal.
import { h, mount, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, confirmDialog, emptyState, errorBox, errorMessage, inputEl, jsonViewer, markdown, notice, skeleton, table, viewHead, asyncClick, loadInto, skeletonBlock } from '../core/ui.js';
import { link } from '../core/router.js';
import { formatBytes, formatNumber, safeJson, truncate } from '../core/format.js';

const PAGE = 25;
const nameOf = (d) => d.name || d.id || d.dataset || d.key;
const countOf = (d) => d.records ?? d.count ?? d.total ?? d.size ?? d.n ?? null;

export function extractRecords(res) {
  if (Array.isArray(res)) return res;
  if (!res || typeof res !== 'object') return [];
  for (const k of ['items', 'records', 'rows', 'data', 'lines', 'entries', 'results']) if (Array.isArray(res[k])) return res[k];
  const arr = Object.values(res).find((v) => Array.isArray(v));
  if (arr) return arr;
  return [res];
}
export function totalOf(res, recs) {
  if (res && typeof res === 'object' && !Array.isArray(res)) for (const k of ['total', 'count', 'n', 'size', 'total_records']) if (typeof res[k] === 'number') return res[k];
  return recs.length;
}
export function columnsFor(recs, max = 6) {
  const counts = new Map();
  for (const r of recs.slice(0, 25)) {
    if (!r || typeof r !== 'object') continue;
    for (const [k, v] of Object.entries(r)) {
      if (v !== null && typeof v === 'object') continue;
      counts.set(k, (counts.get(k) || 0) + 1);
    }
  }
  const pri = ['id', 'scenario_id', 'name', 'title', 'type', 'tier', 'category', 'component', 'severity', 'ts', 'occurred_at'];
  return [...counts.keys()].sort((a, b) => (pri.indexOf(a) === -1 ? 99 : pri.indexOf(a)) - (pri.indexOf(b) === -1 ? 99 : pri.indexOf(b)) || counts.get(b) - counts.get(a)).slice(0, max);
}

export default async function datasets(root, route, app) {
  let index = [];
  let current = route.params.name || null;
  let offset = Number(route.query.offset || 0);
  let selectedRec = route.params.rec ?? null;
  let filter = '';
  const catEl = h('div', { class: 'col', style: 'gap:0' });
  const mainEl = h('div', { class: 'col gap-4' });

  mount(root, viewHead({ title: 'Datasets', sub: 'The deterministic reference corpora, scenarios with ground truth, replayable streams and evaluation case sets. Ground truth stays hidden until you reveal it.' }), h('div', { class: 'split' }, h('aside', { class: 'card split-list', 'aria-label': 'Dataset catalogue' }, h('div', { class: 'card-body flush scroll', style: 'max-height:calc(100vh - 170px)' }, catEl)), h('section', { class: 'split-detail' }, mainEl)));
  mount(catEl, skeleton(8));
  try {
    const res = await app.api.datasets();
    index = res.items || [];
    if (res.available === false) throw Object.assign(new Error('The data layer is not available on this server.'), { code: 'data_unavailable' });
  } catch (err) {
    mount(catEl, errorBox(err, () => location.reload(), 'Cannot load datasets'));
    mount(mainEl, emptyState({ icon: 'database', title: 'No datasets', text: errorMessage(err) }));
    return;
  }
  if (!current && index.length && window.innerWidth > 900) current = nameOf(index.find((d) => /scenario/.test(nameOf(d))) || index[0]);

  function renderCat() {
    mount(catEl, index.map((d) => h('a', { class: ['graph-row', nameOf(d) === current && 'selected'], href: link('datasets', nameOf(d)), 'aria-current': nameOf(d) === current ? 'page' : undefined },
      h('div', { class: 'row between' }, h('span', { class: 'mono strong truncate' }, nameOf(d)), countOf(d) != null && h('span', { class: 'tiny muted' }, formatNumber(countOf(d)))),
      h('div', { class: 'row wrap tiny muted' }, d.role && badge(d.role, 'info', { sq: true }), d.format && h('span', null, d.format), d.bytes != null && h('span', null, formatBytes(d.bytes))),
      d.description && h('div', { class: 'small muted' }, truncate(d.description, 80)),
    )));
  }
  renderCat();
  if (current) await showDataset(current);
  else mount(mainEl, emptyState({ icon: 'database', title: 'Choose a dataset' }));

  async function showDataset(name) {
    const meta = index.find((d) => nameOf(d) === name) || { name };
    const isScenarios = /^scenarios?($|[._/-])|scenarios/.test(name) && !/sweep|spec/.test(name);
    const isStream = /stream/.test(name) && !/labels/.test(name);
    const tableHost = h('div');
    const viewer = h('div');
    const pager = h('div', { class: 'row between' });
    const filterIn = inputEl({ size: 'sm', placeholder: 'Filter this page', label: 'Filter rows', oninput: debounce((e) => { filter = e.target.value.toLowerCase(); draw(); }, 120) });
    let recs = [];
    let total = 0;
    const exportBtn = button('Export page JSONL', { size: 'sm', icon: 'download', onclick: () => {
      const blob = new Blob([recs.map((r) => JSON.stringify(r)).join('\n') + '\n'], { type: 'application/x-ndjson' });
      const a = h('a', { href: URL.createObjectURL(blob), download: `${name}.page-${offset}.jsonl` });
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    } });
    const replayBtn = isStream && button('Replay this stream', { size: 'sm', icon: 'play', variant: 'primary' });
    if (replayBtn) asyncClick(replayBtn, async () => {
      const streamName = name.replace(/\.jsonl?$/, '').split('/').pop();
      await app.api.replayStart({ stream: streamName, speed: 60, loop: false, replace: true });
      app.toast({ title: `Replaying ${streamName}`, body: '60x on the virtual clock', tone: 'ok', ttl: 3000 });
      app.navigate('#/');
    }, (e) => app.toast({ title: 'Replay failed', body: errorMessage(e), tone: 'critical' }));

    mount(mainEl,
      card({ title: h('span', { class: 'mono' }, name), sub: meta.description || '', actions: [exportBtn, replayBtn || null], body: h('div', { class: 'col gap-3' },
        h('div', { class: 'row wrap' }, meta.role && badge(meta.role, 'info', { sq: true }), countOf(meta) != null && chip(`${formatNumber(countOf(meta))} records`), meta.format && chip(meta.format), h('span', { class: 'grow' }), h('div', { style: 'width:220px' }, filterIn)),
        tableHost, pager) }),
      viewer);
    mount(tableHost, skeleton(6));

    async function fetchPage() {
      try {
        const res = await app.api.dataset(name, { offset, limit: PAGE });
        recs = extractRecords(res);
        total = totalOf(res, recs);
        draw();
        if (selectedRec !== null) openRecord(recs.find((r, i) => String(r.id ?? r.scenario_id ?? offset + i) === String(selectedRec)) ?? recs[Number(selectedRec) - offset], selectedRec);
      } catch (err) {
        mount(tableHost, errorBox(err, fetchPage, 'Cannot read this dataset'));
      }
    }
    function draw() {
      const rows = recs.filter((r) => !filter || JSON.stringify(r).toLowerCase().includes(filter));
      if (!rows.length) {
        mount(tableHost, emptyState({ icon: 'database', title: recs.length ? 'No rows match the filter' : 'This dataset is empty' }));
      } else if (typeof rows[0] !== 'object' || rows[0] === null) {
        mount(tableHost, h('pre', { class: 'jv' }, rows.map((r) => String(r)).join('\n')));
      } else {
        const cols = columnsFor(rows);
        mount(tableHost, table({ caption: name, rows, onRow: (r) => openRecord(r, r.id ?? r.scenario_id ?? offset + recs.indexOf(r)), columns: [{ label: '#', num: true, render: (r) => h('span', { class: 'muted' }, offset + recs.indexOf(r) + 1) }, ...cols.map((c) => ({ label: c, render: (r) => h('span', { class: ['mono', 'small'] }, truncate(String(r[c] ?? ''), 48)) }))] }));
      }
      const end = Math.min(offset + recs.length, total || offset + recs.length);
      mount(pager, h('span', { class: 'small muted' }, total ? `${formatNumber(offset + 1)}-${formatNumber(end)} of ${formatNumber(total)}` : `${recs.length} rows`), h('div', { class: 'row' }, button('Previous', { size: 'sm', disabled: offset <= 0, onclick: () => { offset = Math.max(0, offset - PAGE); fetchPage(); } }), button('Next', { size: 'sm', disabled: !total || offset + PAGE >= total, onclick: () => { offset += PAGE; fetchPage(); } })));
    }
    function openRecord(rec, key) {
      if (!rec) return;
      selectedRec = key;
      const body = rec.body || rec.markdown || rec.text;
      const sid = rec.scenario_id || (isScenarios ? rec.id : null);
      const truthHost = h('div');
      const reveal = sid && button('Reveal ground truth', { size: 'sm', icon: 'eye' });
      if (reveal) asyncClick(reveal, async () => {
        const ok = await confirmDialog({ title: 'Reveal ground truth?', text: `This shows the injected root cause, red herrings and expected remediation for ${sid}.`, confirmLabel: 'Reveal' });
        if (!ok) return;
        const full = await app.api.scenario(sid, true);
        mount(truthHost, h('div', { class: 'col' }, notice('Ground truth overlay (what the generator injected).', 'medium', 'eye'), jsonViewer(full.truth || full, { open: 2 })));
        reveal.hidden = true;
      }, (e) => mount(truthHost, errorBox(e)));
      mount(viewer, card({ title: `Record ${typeof key === 'number' ? '#' + (key + 1) : key}`, actions: [reveal || null, button('', { icon: 'x', size: 'sm', title: 'Close', onclick: () => { selectedRec = null; mount(viewer); } })], body: h('div', { class: 'col gap-3' }, typeof body === 'string' && body.length > 40 ? markdown(body) : null, jsonViewer(rec, { open: 2 }), truthHost) }));
      viewer.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    await fetchPage();
  }
}
