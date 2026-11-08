// Threads list + Time-travel / Fork panel (the Studio features, in-app): history, state diff, edit state, fork, rerun.
import { h, mount, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, confirmDialog, copyButton, emptyState, errorBox, errorMessage, field, inputEl, jsonViewer, loadInto, notice, seg, selectEl, skeleton, skeletonBlock, statusBadge, switchEl, table, tabs, textareaEl, viewHead, asyncClick } from '../core/ui.js';
import { diffJson, buildStateUpdate, forkInfo, checkpointId } from '../core/jsondiff.js';
import { link } from '../core/router.js';
import { formatClock, formatDateTime, safeJson, shortId, truncate } from '../core/format.js';
import { ago, studioThreadButtons } from './shared.js';

export default async function threads(root, route, app) {
  if (route.params.threadId) return timeTravel(root, route, app);
  return threadList(root, route, app);
}

// ======================================================================================================================
async function threadList(root, route, app) {
  let status = '';
  let graph = '';
  let q = '';
  const body = h('div');
  const graphSel = selectEl([{ value: '', label: 'All graphs' }], '', (v) => { graph = v; load(); }, { size: 'sm', label: 'Graph' });
  mount(root, viewHead({ title: 'Threads', sub: 'Every conversation and run the Agent Server holds. Open one to step through its checkpoints, edit state and fork it.' }),
    h('div', { class: 'toolbar mb-3' },
      seg([{ value: '', label: 'All' }, { value: 'busy', label: 'Busy' }, { value: 'interrupted', label: 'Interrupted' }, { value: 'idle', label: 'Idle' }, { value: 'error', label: 'Error' }], status, (v) => { status = v; load(); }, { label: 'Status' }),
      graphSel,
      h('div', { style: 'width:230px' }, inputEl({ size: 'sm', placeholder: 'Find thread id / incident', label: 'Find', oninput: debounce((e) => { q = e.target.value.trim().toLowerCase(); render(); }, 150) })),
      h('span', { class: 'grow' }),
      button('Refresh', { icon: 'refresh', size: 'sm', onclick: () => load() }),
    ), body);
  let list = [];
  app.api.graphs().then((r) => mount(graphSel, h('option', { value: '' }, 'All graphs'), (r.items || []).map((g) => h('option', { value: g.graph_id }, g.graph_id)))).catch(() => {});

  function render() {
    const rows = list.filter((t) => !q || `${t.thread_id} ${t.metadata?.foghorn_incident_id || ''} ${t.metadata?.foghorn_scenario_id || ''}`.toLowerCase().includes(q));
    if (!rows.length) {
      mount(body, emptyState({ icon: 'history', title: 'No threads', text: list.length ? 'Nothing matches your search.' : 'Launch a scenario or run a graph sample.', action: button('Scenarios', { href: '#/scenarios' }) }));
      return;
    }
    mount(body, card({ flush: true, body: table({ caption: 'Threads', rows, onRow: (t) => app.navigate(link('threads', t.thread_id)), columns: [
      { label: 'Thread', render: (t) => h('span', { class: 'mono strong' }, shortId(t.thread_id, 13)) },
      { label: 'Graph', render: (t) => t.metadata?.foghorn_graph || t.metadata?.graph_id || '' },
      { label: 'Status', render: (t) => statusBadge(t.status) },
      { label: 'Incident', render: (t) => t.metadata?.foghorn_incident_id ? h('a', { class: 'mono', href: link('incidents', t.metadata.foghorn_incident_id), onclick: (e) => e.stopPropagation() }, t.metadata.foghorn_incident_id) : '' },
      { label: 'Scenario', render: (t) => t.metadata?.foghorn_scenario_id || '' },
      { label: 'Trigger', render: (t) => t.metadata?.foghorn_trigger || '' },
      { label: 'Updated', render: (t) => h('span', { class: 'small muted' }, ago(t.updated_at)) },
    ] }) }));
  }
  async function load() {
    mount(body, skeleton(6));
    try {
      const metadata = graph ? { foghorn_graph: graph } : undefined;
      const res = await app.api.lgThreadsSearch({ limit: 100, sort_by: 'updated_at', sort_order: 'desc', status: status || undefined, metadata });
      list = Array.isArray(res) ? res : res?.threads || [];
      render();
    } catch (err) {
      mount(body, errorBox(err, load, 'Cannot list threads'));
    }
  }
  await load();
}

// ======================================================================================================================
async function timeTravel(root, route, app) {
  const threadId = route.params.threadId;
  let thread = null;
  let hist = [];
  let selectedCp = route.query.cp || null;
  let tab = 'state';
  let dead = false;
  const detail = h('div');
  const listEl = h('div', { class: 'cp-list', role: 'listbox', 'aria-label': 'Checkpoints' });

  mount(root, h('div', { class: 'col gap-4' }, skeleton(2, [30, 60]), skeletonBlock(300)));
  try {
    [thread, hist] = await Promise.all([app.api.lgThread(threadId), app.api.lgHistory(threadId, { limit: 100 })]);
  } catch (err) {
    mount(root, errorBox(err, () => location.reload(), `Cannot load thread ${shortId(threadId, 8)}`), h('div', { class: 'mt-4' }, button('All threads', { href: '#/threads' })));
    return;
  }
  hist = Array.isArray(hist) ? hist : hist?.states || [];
  const graphId = thread?.metadata?.foghorn_graph || thread?.metadata?.graph_id || 'incident_commander';
  const { forks } = forkInfo(hist);
  const byId = new Map(hist.map((s) => [checkpointId(s), s]));
  if (!selectedCp || !byId.has(selectedCp)) selectedCp = checkpointId(hist[0]);

  const incId = thread?.metadata?.foghorn_incident_id;
  mount(
    root,
    viewHead({
      title: h('span', { class: 'row wrap' }, 'Time travel', h('span', { class: 'mono muted', style: 'font-size:var(--fs-2)' }, shortId(threadId, 13))),
      sub: 'Step through checkpoints, compare states, edit a value and fork: the original branch is always preserved.',
      actions: [statusBadge(thread?.status), chip(graphId, { mono: true }), incId && button(incId, { href: link('incidents', incId), size: 'sm' }), button('Open console', { href: `${link('runs', threadId)}?graph=${encodeURIComponent(graphId)}`, size: 'sm', icon: 'play' }), studioThreadButtons(app, threadId, graphId), (() => { const b = button('Copy thread', { size: 'sm', icon: 'copy', title: 'POST /threads/{id}/copy: clone this thread and all of its checkpoints' }); asyncClick(b, async () => { const t = await app.api.lgCopy(threadId); app.toast({ title: 'Thread copied', body: shortId(t.thread_id, 13), tone: 'ok', ttl: 4000, action: { label: 'Open', href: link('threads', t.thread_id) } }); }, (e) => app.toast({ title: 'Copy failed', body: errorMessage(e), tone: 'critical' })); return b; })()],
    }),
    h('div', { class: 'tt-layout' }, card({ title: `History (${hist.length})`, sub: 'newest first', flush: true, class: 'tt-history', body: listEl }), detail),
  );

  function cpLabel(s) {
    const writes = s.metadata?.writes ? Object.keys(s.metadata.writes).filter((k) => k !== '__start__') : [];
    const src = s.metadata?.source || '';
    const label = writes[0] || (src === 'input' ? 'input' : src === 'update' ? 'state edit' : (s.next || [])[0] || 'checkpoint');
    return { label, writes, src };
  }
  function renderList() {
    mount(listEl, hist.map((s, idx) => {
      const id = checkpointId(s);
      const { label, src } = cpLabel(s);
      const isFork = forks.has(id) || src === 'update';
      return h('button', { class: ['cp-item', id === selectedCp && 'selected'], type: 'button', role: 'option', 'aria-selected': String(id === selectedCp), onclick: () => { selectedCp = id; renderList(); renderDetail(); }, title: id },
        h('span', { class: 'cp-step mono' }, s.metadata?.step ?? hist.length - idx),
        h('span', { class: 'grow' }, h('div', { class: 'row wrap', style: 'gap:6px' }, h('span', { class: 'strong' }, label), isFork && badge('fork', 'accent', { sq: true }), (s.next || []).length === 0 && idx === 0 && badge('end', 'ok', { sq: true })), h('div', { class: 'tiny muted' }, `${src || 'loop'} · next: ${(s.next || []).join(', ') || 'none'}${(s.tasks || []).some((t) => (t.interrupts || []).length) ? ' · interrupt' : ''}`)),
        h('span', { class: 'tiny muted mono' }, s.created_at ? formatClock(s.created_at) : ''),
      );
    }));
  }

  function renderDetail() {
    const s = byId.get(selectedCp);
    if (!s) {
      mount(detail, emptyState({ icon: 'history', title: 'No checkpoint selected' }));
      return;
    }
    const parentId = s.parent_checkpoint?.checkpoint_id || s.parent_config?.configurable?.checkpoint_id;
    const parent = parentId ? byId.get(parentId) : null;
    const pane = h('div', { class: 'col gap-4' });
    const t = tabs([{ key: 'state', label: 'State' }, { key: 'diff', label: 'Diff vs parent', count: parent ? diffJson(parent.values || {}, s.values || {}).length : undefined }, { key: 'fork', label: 'Edit and fork' }], tab, (k) => { tab = k; draw(); }, { label: 'Checkpoint views' });
    mount(detail, h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('div', { class: 'strong' }, `Checkpoint ${shortId(selectedCp, 12)}`), h('div', { class: 'tiny muted' }, `${s.created_at ? formatDateTime(s.created_at) : ''} · next: ${(s.next || []).join(', ') || 'end'}`)), copyButton(selectedCp, 'Copy id', app.toast)), t, h('div', { class: 'card-body' }, pane)));
    function draw() {
      if (tab === 'state') {
        const ints = (s.tasks || []).flatMap((x) => x.interrupts || []);
        mount(pane, ints.length > 0 && notice(`This checkpoint is paused on ${ints.length} interrupt(s). Resume them from Approvals or the console.`, 'medium', 'pause'), jsonViewer(s.values || {}, { open: 2 }), s.metadata && h('details', null, h('summary', { class: 'small muted', style: 'cursor:pointer' }, 'Checkpoint metadata'), jsonViewer(s.metadata, { open: 1 })));
      } else if (tab === 'diff') {
        if (!parent) return mount(pane, emptyState({ icon: 'compare', title: 'No parent checkpoint', text: 'This is the first checkpoint on its branch.' }));
        const d = diffJson(parent.values || {}, s.values || {});
        mount(pane, d.length ? h('div', { class: 'diff-list' }, d.map((x) => h('div', { class: ['diff-row', `d-${x.kind}`] }, h('span', { class: 'diff-sign' }, x.kind === 'added' ? '+' : x.kind === 'removed' ? '-' : '~'), h('span', { class: 'mono diff-path' }, x.path), h('span', { class: 'diff-vals mono small' }, x.kind !== 'added' && h('span', { class: 'before' }, truncate(JSON.stringify(x.before), 160)), x.kind === 'changed' && ' → ', x.kind !== 'removed' && h('span', { class: 'after' }, truncate(JSON.stringify(x.after), 160)))))) : h('div', { class: 'muted' }, 'No change in state values (the step only moved control flow).'));
      } else {
        forkPane(pane, s);
      }
    }
    draw();
  }

  function forkPane(pane, s) {
    const cpIdSel = checkpointId(s);
    const original = s.values || {};
    const ta = textareaEl({ rows: 14, mono: true, value: safeJson(original), label: 'State JSON', spellcheck: false });
    let overwrite = true;
    const nodes = [...new Set([...(Object.keys(s.metadata?.writes || {}).filter((k) => k !== '__start__')), ...(s.next || [])])];
    const asNode = selectEl([{ value: '', label: '(infer)' }, ...nodes.map((n) => ({ value: n, label: n }))], nodes.find((n) => (s.metadata?.writes || {})[n]) || '', null, { label: 'As node' });
    const preview = h('div');
    const out = h('div');
    const update = () => {
      try {
        const ed = JSON.parse(ta.value);
        const u = buildStateUpdate(original, ed, { overwriteLists: overwrite });
        mount(preview, u.changedKeys.length ? h('div', { class: 'col' }, h('div', { class: 'small muted' }, `Will send ${u.changedKeys.length} changed key(s): ${u.changedKeys.join(', ')}`), u.notes.map((n) => h('div', { class: 'tiny muted' }, n)), jsonViewer(u.values, { open: 1 })) : h('div', { class: 'small muted' }, 'No changes yet. Edit a value above to create a fork.'));
        return u;
      } catch (e) {
        mount(preview, h('div', { class: 'err small' }, `Not valid JSON: ${e.message}`));
        return null;
      }
    };
    ta.addEventListener('input', debounce(update, 150));

    const forkBtn = button('Fork and rerun', { icon: 'branch', variant: 'primary', title: 'Write the edit as a new checkpoint (the original stays), then run from it' });
    const forkOnlyBtn = button('Fork only', { icon: 'branch', title: 'Write the edit as a new checkpoint without running' });
    const replayBtn = button('Replay from here', { icon: 'refresh', title: 'Run again from this checkpoint without any edit (new branch)' });
    const run = async (cp) => {
      const r = await app.api.lgCreateRun(threadId, { assistant_id: graphId, checkpoint: cp, input: null, stream_mode: ['values', 'updates', 'messages-tuple', 'custom', 'tasks', 'checkpoints'], stream_subgraphs: true, stream_resumable: true, on_disconnect: 'continue' });
      app.toast({ title: 'Run started from checkpoint', tone: 'ok', ttl: 3000 });
      app.navigate(`${link('runs', threadId)}?graph=${encodeURIComponent(graphId)}`);
      return r;
    };
    const doFork = async (alsoRun) => {
      const u = update();
      if (!u) throw new Error('Fix the JSON first');
      if (!u.changedKeys.length) throw new Error('Nothing changed: edit a value first');
      const cfg = { checkpoint_id: cpIdSel, checkpoint_ns: s.checkpoint?.checkpoint_ns || '' };
      const res = await app.api.lgUpdateState(threadId, { values: u.values, checkpoint: cfg, as_node: asNode.value || undefined });
      const newCp = res?.checkpoint || res;
      mount(out, notice(h('span', null, 'Forked: new checkpoint ', h('code', null, shortId(newCp?.checkpoint_id, 12)), '. The original branch is untouched.'), 'ok', 'check-circle'));
      if (alsoRun) await run({ checkpoint_id: newCp.checkpoint_id, checkpoint_ns: newCp.checkpoint_ns || '' });
      else {
        const fresh = await app.api.lgHistory(threadId, { limit: 100 });
        hist = Array.isArray(fresh) ? fresh : fresh?.states || hist;
        byId.clear();
        hist.forEach((x) => byId.set(checkpointId(x), x));
        selectedCp = newCp.checkpoint_id;
        renderList();
        renderDetail();
      }
    };
    asyncClick(forkBtn, () => doFork(true), (e) => mount(out, errorBox(e, null, 'Fork failed')));
    asyncClick(forkOnlyBtn, () => doFork(false), (e) => mount(out, errorBox(e, null, 'Fork failed')));
    asyncClick(replayBtn, () => run({ checkpoint_id: cpIdSel, checkpoint_ns: s.checkpoint?.checkpoint_ns || '' }), (e) => mount(out, errorBox(e, null, 'Replay failed')));

    mount(pane,
      notice('Edits go through the state reducers. Only changed top-level keys are sent; replaced lists are wrapped as an Overwrite so they replace instead of append. Recipes: change hypotheses[0], change incident.severity before policy_check, or rerun from approval_gate with a different decision.', 'info', 'alert-circle'),
      field('State', ta, { hint: 'Edit values directly. Keys you do not touch are not sent.' }),
      h('div', { class: 'row wrap' }, field('Write as node', asNode, { hint: 'Which node’s output this edit pretends to be' }), switchEl('Replace lists (Overwrite)', true, (v) => { overwrite = v; update(); })),
      preview,
      h('div', { class: 'row wrap' }, forkBtn, forkOnlyBtn, replayBtn),
      out,
    );
    update();
  }

  renderList();
  renderDetail();
  return () => { dead = true; void dead; };
}
