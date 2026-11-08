// Incident Console: token-by-token war-room stream, node timeline, evidence / hypotheses / series board,
// raw stream drawer, reconnect via join-stream, cancel, final result.
import { h, mount, rafThrottle, stickToBottom, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, copyButton, emptyState, errorBox, errorMessage, incBadge, jsonViewer, skeleton, statusBadge, tabs, drawer, asyncClick, iconButton, confirmDialog, seg } from '../core/ui.js';
import { createRunState, applySSE, beginRun, finishRun, cancelRun, rankedHypotheses, laneList, laneText } from '../core/reducer.js';
import { normalizeInterrupt } from '../core/schemaform.js';
import { lineChart, sparkline } from '../core/charts.js';
import { zScore } from '../core/chartmath.js';
import { formatClock, formatDuration, formatMetric, formatPct, formatScore, hueFor, safeJson, truncate, shortId } from '../core/format.js';
import { link } from '../core/router.js';
import { joinRun, streamRun } from '../api.js';
import { summarizeEvent } from '../core/events.js';
import { studioThreadButtons, agentTag, eventRow, openEventDrawer } from './shared.js';
import { interruptGroup } from './interrupts.js';

const KIND_ICON = { metric: 'activity', log: 'terminal', change: 'branch', memory: 'chip', runbook: 'book', topology: 'graph', alert: 'alert' };

export default async function incidentView(root, route, app) {
  const incidentId = route.params.id || null;
  let threadId = route.params.threadId || route.query.thread || null;
  let graphId = route.query.graph || 'incident_commander';
  let incident = null;
  const disposers = [];
  let dead = false;

  mount(root, h('div', { class: 'col gap-4' }, skeleton(2, [30, 60]), h('div', { class: 'console-grid' }, h('div', { class: 'skeleton block', style: 'height:360px' }), h('div', { class: 'skeleton block', style: 'height:360px' }), h('div', { class: 'skeleton block', style: 'height:360px' }))));

  // ---- resolve incident -> thread ----------------------------------------------------------------------------
  if (incidentId) {
    try {
      incident = await app.api.incident(incidentId);
      threadId = threadId || incident.thread_id || incident.threads?.find((t) => t.graph_id === 'incident_commander')?.thread_id || incident.threads?.[0]?.thread_id;
      graphId = incident.threads?.find((t) => t.thread_id === threadId)?.graph_id || graphId;
    } catch (err) {
      mount(root, errorBox(err, () => location.reload(), `Cannot load ${incidentId}`), h('div', { class: 'mt-4' }, button('All incidents', { href: '#/incidents' })));
      return;
    }
  }
  if (!threadId) {
    mount(root, emptyState({ icon: 'alert', title: 'No run attached yet', text: incident ? `${incident.incident_id} has no thread yet. It appears when the commander starts.` : 'Open a run from an incident, an approval, or the Threads list.', action: button('Incidents', { href: '#/incidents' }) }));
    return;
  }

  const run = createRunState({ threadId });
  let handle = null;
  let conn = 'idle';
  let replayNote = '';
  const pendingChanged = new Set();

  // ---- header ---------------------------------------------------------------------------------------------
  const statusSlot = h('span');
  const connSlot = h('span');
  const cancelBtn = button('Cancel run', { icon: 'stop', variant: 'danger', size: 'sm' });
  asyncClick(cancelBtn, async () => {
    const ok = await confirmDialog({ title: 'Cancel this run?', text: 'The run is interrupted; the thread keeps its state so you can resume or fork it later.', confirmLabel: 'Cancel run', danger: true });
    if (!ok) return;
    if (run.runId) await app.api.lgCancel(threadId, run.runId, 'interrupt');
    if (handle) handle.close();
    cancelRun(run);
    flush(new Set(['status', 'timeline']));
    app.toast({ title: 'Run cancelled', tone: 'info', ttl: 2500 });
  }, (e) => app.toast({ title: 'Cancel failed', body: errorMessage(e), tone: 'critical' }));
  const rawBtn = button('Raw stream', { icon: 'terminal', size: 'sm', onclick: () => openDrawerTab('raw') });
  const ckptBtn = button('Time travel', { icon: 'history', size: 'sm', href: link('threads', threadId), title: 'Checkpoint history, state diff, edit and fork' });
  const head = h('div', { class: 'console-head' },
    h('div', { class: 'row wrap' },
      incident ? h('strong', { class: 'mono', style: 'font-size:var(--fs-4)' }, incident.incident_id) : h('strong', null, 'Run'),
      incident && incBadge(incident.severity),
      incident && statusBadge(incident.status),
      incident?.scenario_id && h('a', { class: 'chip mono', href: link('scenarios', 'launcher', incident.scenario_id) }, incident.scenario_id),
      chip(graphId, { mono: true, title: 'graph' }),
      chip(`thread ${shortId(threadId, 8)}`, { mono: true, title: threadId }),
      statusSlot,
      connSlot,
    ),
    incident?.title && h('div', { class: 'muted' }, incident.title),
    h('div', { class: 'row wrap mt-2' }, studioThreadButtons(app, threadId, graphId), rawBtn, ckptBtn, cancelBtn, h('span', { class: 'grow' }), seg([{ value: 'stream', label: 'Stream' }, { value: 'timeline', label: 'Timeline' }, { value: 'board', label: 'Board' }], 'stream', (v) => grid.setAttribute('data-pane', v), { label: 'Pane' })),
  );
  const banner = h('div', { class: 'interrupt-banner', hidden: true, role: 'alert' });

  // ---- panes ----------------------------------------------------------------------------------------------
  const timelineEl = h('div', { class: 'tl' });
  const streamEl = h('div', { class: 'stream', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions text', 'aria-label': 'War-room stream', tabindex: '0' });
  const boardEl = h('div', { class: 'board col gap-4' });
  const grid = h('div', { class: 'console-grid', 'data-pane': 'stream' },
    h('section', { class: 'pane pane-timeline', 'aria-label': 'Timeline' }, h('div', { class: 'pane-title' }, 'Timeline'), timelineEl),
    h('section', { class: 'pane pane-stream', 'aria-label': 'War-room stream' }, h('div', { class: 'pane-title' }, 'War-room stream', h('span', { class: 'muted tiny', id: 'tok-count' })), streamEl),
    h('section', { class: 'pane pane-board', 'aria-label': 'Incident board' }, h('div', { class: 'pane-title' }, 'Incident board'), boardEl),
  );
  const follow = stickToBottom(streamEl);

  // drawer tabs (inline, below the grid)
  const dock = h('section', { class: 'dock card' });
  const dockBody = h('div', { class: 'dock-body' });
  let dockTab = 'raw';
  const dockTabs = tabs([{ key: 'raw', label: 'Raw stream' }, { key: 'checkpoints', label: 'Checkpoints' }, { key: 'events', label: 'Linked events' }, { key: 'state', label: 'State JSON' }], dockTab, (k) => { dockTab = k; renderDock(); }, { label: 'Console drawer' });
  mount(dock, dockTabs, dockBody);
  function openDrawerTab(k) {
    dockTabs.select(k);
    dock.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  mount(root, head, banner, grid, dock);

  // ---- render: header / banner -------------------------------------------------------------------------------
  function renderHeader() {
    mount(statusSlot, statusBadge(run.status === 'idle' ? 'idle' : run.status));
    const c = { open: ['ok', 'streaming', true], connecting: ['medium', 'connecting', false], retrying: ['high', 'reconnecting', false], closed: ['info', 'stream closed', false], error: ['critical', 'stream error', false], idle: ['info', 'no stream', false] }[conn] || ['info', conn, false];
    mount(connSlot, badge(c[1], c[0], { dot: true, live: c[2], outline: true, title: replayNote || undefined }));
    cancelBtn.hidden = run.status !== 'running';
    mount(root.querySelector('#tok-count') || document.createElement('span'), run.counts.tokens ? ` ${run.counts.tokens} tokens` : '');
  }

  let respondDrawer = null;
  function renderBanner() {
    const n = run.interrupts.length;
    banner.hidden = n === 0;
    if (!n) {
      respondDrawer?.close();
      return;
    }
    const first = run.interrupts[0].value || {};
    mount(banner, icon('alert'), h('div', { class: 'grow' }, h('strong', null, n === 1 ? 'A decision is waiting' : `${n} decisions are waiting`), h('div', { class: 'small' }, n === 1 ? first.title || first.summary || 'The run is paused on an interrupt.' : run.interrupts.map((i) => i.value?.title).filter(Boolean).join(' · '))), button('Respond', { variant: 'primary', icon: 'edit', onclick: openRespond }));
  }
  function openRespond() {
    respondDrawer?.close();
    const group = interruptGroup(app, {
      threadId,
      graphId,
      interrupts: run.interrupts,
      onResumed: (r) => {
        respondDrawer?.close();
        run.interrupts = [];
        renderBanner();
        reattachSoon(r);
      },
    });
    respondDrawer = drawer({ title: run.interrupts.length > 1 ? `${run.interrupts.length} pending decisions` : run.interrupts[0].value?.title || 'Decision', wide: true, body: group.el, onClose: () => (respondDrawer = null) });
  }

  // ---- render: timeline ------------------------------------------------------------------------------------
  function renderTimeline() {
    if (!run.timeline.length) {
      mount(timelineEl, h('div', { class: 'small muted', style: 'padding:8px' }, run.status === 'idle' ? 'Waiting for the first step...' : 'No steps recorded yet.'));
      return;
    }
    const now = Date.now();
    mount(
      timelineEl,
      run.timeline.map((s) => {
        const dur = s.durationMs ?? (s.status === 'running' ? now - s.startedAt : null);
        const ic = { running: 'refresh', done: 'check-circle', error: 'x-circle', interrupted: 'pause', cancelled: 'stop' }[s.status] || 'clock';
        return h('div', { class: ['tl-step', `st-${s.status}`], style: { paddingLeft: 8 + s.depth * 14 + 'px' }, title: s.error || s.ns.join(' / ') },
          icon(ic, `sm ${s.status === 'running' ? 'spin' : ''}`),
          h('span', { class: 'tl-name truncate' }, s.name),
          s.ns.length > 0 && h('span', { class: 'tl-ns tiny muted truncate' }, s.ns[s.ns.length - 1]),
          h('span', { class: 'tl-dur tiny mono' }, dur != null ? formatDuration(dur) : ''),
        );
      }),
    );
  }

  // ---- render: stream (incremental) -----------------------------------------------------------------------
  const laneNodes = new Map(); // laneKey -> {root, text: Map(segIdx -> Text), tools: Map}
  function ensureLaneNode(lane) {
    let n = laneNodes.get(lane.key);
    if (n) return n;
    const body = h('div', { class: 'lane-body' });
    const rootEl = h('section', { class: 'lane', style: { '--lane': `hsl(${hueFor(lane.key)} 62% 48%)` }, dataset: { lane: lane.key } },
      h('header', { class: 'lane-head' }, h('span', { class: 'lane-dot' }), h('span', { class: 'lane-name' }, lane.label), lane.ns.length > 0 && h('span', { class: 'tiny muted' }, 'subgraph'), h('span', { class: 'lane-meta tiny muted' })),
      body,
    );
    n = { root: rootEl, body, text: new Map(), tools: new Map(), meta: rootEl.querySelector('.lane-meta') };
    laneNodes.set(lane.key, n);
    streamEl.appendChild(rootEl);
    return n;
  }
  function renderStream() {
    const lanes = laneList(run);
    if (!lanes.length) {
      if (!streamEl.querySelector('.empty')) mount(streamEl, emptyState({ icon: 'chat', title: run.status === 'running' ? 'Waiting for the first tokens...' : 'No narration captured', text: run.status === 'running' ? 'Agents stream here as they think, one lane per agent.' : run.counts.parts > 0 ? 'This run streamed no model tokens (deterministic nodes only). See the timeline, raw stream and state.' : replayNote || 'The stream replay window has passed. The board and state below reflect the thread.' }));
      return;
    }
    streamEl.querySelector(':scope > .empty')?.remove();
    for (const lane of lanes) {
      const n = ensureLaneNode(lane);
      n.root.classList.toggle('live', run.status === 'running' && !lane.done);
      n.meta.textContent = `${lane.tokens} tok`;
      lane.segments.forEach((seg, i) => {
        let t = n.text.get(i);
        if (!t) {
          const p = h('p', { class: 'lane-text' });
          const tn = document.createTextNode('');
          p.appendChild(tn);
          n.body.appendChild(p);
          t = { p, tn };
          n.text.set(i, t);
        }
        if (t.tn.data !== seg.text) t.tn.data = seg.text;
        t.p.classList.toggle('active', run.status === 'running' && i === lane.segments.length - 1 && !lane.done);
      });
      // tool calls
      for (const [k, call] of Object.entries(lane.tools)) {
        let tc = n.tools.get(k);
        if (!tc) {
          tc = { el: h('div', { class: 'toolcall' }, icon('tool', 'sm'), h('span', { class: 'tc-name mono' }), h('span', { class: 'tc-args mono muted truncate' }), h('span', { class: 'tc-res small' })) };
          n.tools.set(k, tc);
          n.body.appendChild(tc.el);
        }
        tc.el.querySelector('.tc-name').textContent = call.name || 'tool';
        tc.el.querySelector('.tc-args').textContent = truncate(call.args || '', 120);
        const res = tc.el.querySelector('.tc-res');
        res.textContent = call.result ? truncate(call.result, 220) : '';
        tc.el.classList.toggle('done', !!call.done);
      }
    }
    follow.follow();
  }

  // ---- render: board ---------------------------------------------------------------------------------------
  function hypothesesFromValues() {
    const hy = run.values && Array.isArray(run.values.hypotheses) ? run.values.hypotheses : [];
    return hy.filter((x) => typeof x.score === 'number').map((x) => ({ id: x.id || x.component, label: x.label || `${x.component || '?'} · ${x.category || ''}`, score: x.score, delta: undefined, history: [], for: x.evidence || [], against: [] }));
  }
  function renderBoard() {
    const hyp = rankedHypotheses(run);
    const hypShow = hyp.length ? hyp : hypothesesFromValues();
    const parts = [];

    if (run.progress && run.status === 'running') {
      parts.push(h('div', { class: 'col' }, h('div', { class: 'row between small' }, h('span', null, run.progress.label || 'Working'), h('span', { class: 'mono' }, run.progress.pct != null ? formatPct(run.progress.pct <= 1 ? run.progress.pct : run.progress.pct / 100, 0) : '')), h('div', { class: 'bar' }, h('i', { style: { width: Math.min(100, (run.progress.pct <= 1 ? run.progress.pct * 100 : run.progress.pct) || 0) + '%' } }))));
    }

    parts.push(
      boardSection('Hypotheses', hypShow.length || null,
        hypShow.length
          ? h('div', { class: 'col gap-3' }, hypShow.map((x, i) => {
              const tone = i === 0 && x.score >= 0.5 ? 'ok' : x.score >= 0.3 ? 'medium' : 'info';
              return h('div', { class: ['hyp', i === 0 && 'lead'], title: [...(x.for || []).map((f) => '+ ' + f), ...(x.against || []).map((f) => '- ' + f)].join('\n') },
                h('div', { class: 'row between' }, h('span', { class: 'hyp-label truncate' }, `${i + 1}. ${x.label || x.id}`), h('span', { class: 'row' }, x.delta !== undefined && x.delta !== null && h('span', { class: ['delta', x.delta >= 0 ? 'up' : 'down'] }, `${x.delta >= 0 ? '+' : ''}${formatScore(x.delta)}`), h('span', { class: 'mono strong' }, formatScore(x.score)))),
                h('div', { class: ['bar', `t-${tone}`], role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 1, 'aria-valuenow': x.score, 'aria-label': `${x.label || x.id} score` }, h('i', { style: { width: Math.max(2, x.score * 100) + '%' } })),
                (x.for?.length || x.against?.length) && h('div', { class: 'tiny muted' }, `${x.for?.length || 0} for · ${x.against?.length || 0} against`),
              );
            }))
          : emptyHint('Hypotheses rank here as the tournament scores them.'),
      ),
    );

    parts.push(
      boardSection('Metrics', run.seriesOrder.length || null,
        run.seriesOrder.length
          ? h('div', { class: 'series-grid' }, run.seriesOrder.map((id) => {
              const s = run.series[id];
              const z = zScore(s.points);
              const last = s.points?.length ? (Array.isArray(s.points[s.points.length - 1]) ? s.points[s.points.length - 1][1] : s.points[s.points.length - 1].v) : null;
              const bad = Math.abs(z) > 3;
              return h('figure', { class: 'series' },
                h('figcaption', { class: 'row between' }, h('span', { class: 'truncate small strong', title: id }, id), h('span', { class: 'row tiny' }, bad && badge('anomaly', 'critical', { sq: true }), h('span', { class: 'mono' }, last !== null ? formatMetric(s.metric, last) : ''))),
                sparkline(s.points, { width: 220, height: 44, baseline: s.baseline, onset: s.onset, bad, responsive: true, label: `${id} trend` }),
                s.baseline != null && h('div', { class: 'tiny muted' }, `baseline ${formatMetric(s.metric, s.baseline)}${s.agent ? ' · ' + s.agent : ''}`),
              );
            }))
          : emptyHint('Sparklines appear as agents query metrics.'),
      ),
    );

    parts.push(
      boardSection('Evidence', run.evidence.length || null,
        run.evidence.length
          ? h('ul', { class: 'plain-list evidence' }, run.evidence.map((e) =>
              h('li', { class: 'ev-card', id: `ev-${e.id}` },
                h('span', { class: 'ev-icon' }, icon(KIND_ICON[e.kind] || 'search', 'sm')),
                h('div', { class: 'grow' }, h('div', { class: 'small untrusted-text' }, truncate(e.summary || e.id, 200)), h('div', { class: 'row wrap tiny muted' }, e.kind && badge(e.kind, 'info', { sq: true }), e.ref && h('span', { class: 'mono' }, truncate(e.ref, 40)), e.agent && agentTag(e.agent), e.tVirtual && h('span', { class: 'mono' }, formatClock(e.tVirtual)))),
                typeof e.strength === 'number' && h('div', { class: 'strength', title: `strength ${formatScore(e.strength)}` }, h('i', { style: { height: Math.round(e.strength * 100) + '%' } })),
              ),
            ))
          : emptyHint('Evidence cards stream in from the specialists.'),
      ),
    );

    const planActions = run.plan?.actions || (run.values?.chosen_action ? [run.values.chosen_action] : []);
    parts.push(
      boardSection('Plan and policy', planActions.length || run.policy.length || null,
        h('div', { class: 'col gap-3' },
          planActions.length ? h('ul', { class: 'plain-list' }, planActions.map((a, i) => h('li', { class: 'plan-item' }, h('span', { class: 'plan-n' }, i + 1), h('div', { class: 'grow' }, h('div', { class: 'mono small strong' }, [a.action || a.id, a.target].filter(Boolean).join(' → ')), a.reason && h('div', { class: 'tiny muted' }, a.reason)), a.risk && badge(a.risk, a.risk === 'high' ? 'high' : a.risk === 'medium' ? 'medium' : 'low', { sq: true })))) : emptyHint('No action proposed yet.'),
          run.policy.length > 0 && h('ul', { class: 'plain-list' }, run.policy.map((p) => h('li', { class: 'row top small' }, badge(p.result || '?', p.result === 'pass' ? 'ok' : p.result === 'fail' ? 'critical' : 'medium', { sq: true }), h('span', { class: 'mono muted' }, p.rule), h('span', { class: 'grow' }, p.text)))),
        ),
      ),
    );

    if (run.memory.length) {
      parts.push(boardSection('Memory', run.memory.length, h('ul', { class: 'plain-list' }, run.memory.map((m) => h('li', { class: 'row top small' }, badge(m.type, m.type === 'written' ? 'ok' : 'accent', { sq: true }), h('a', { class: 'mono', href: link('memory', ...String(m.ns).split('.'), m.key) }, `${m.ns}/${m.key}`), m.score != null && h('span', { class: 'muted' }, formatScore(m.score)), m.summary && h('span', { class: 'grow muted' }, truncate(m.summary, 100)))))));
    }
    if (run.notes.size) {
      const notes = run.notes.toArray().slice(-8).reverse();
      parts.push(boardSection('Activity', notes.length, h('ul', { class: 'plain-list' }, notes.map((n) => h('li', { class: 'small row top' }, badge(n.kind, 'info', { sq: true }), h('span', { class: 'grow untrusted-text' }, truncate(n.text, 200)))))));
    }

    const comms = run.values?.comms_draft;
    if (comms) parts.push(boardSection('Comms draft', null, h('div', { class: 'col' }, h('div', { class: 'small untrusted-text', style: 'white-space:pre-wrap' }, typeof comms === 'string' ? comms : safeJson(comms)), h('div', { class: 'tiny muted' }, 'Edit via Time travel (state edit) or the comms approval card.'))));

    if (run.values?.result || (run.status === 'done' && run.values)) {
      const result = run.values.result || run.values.incident;
      if (result) parts.push(boardSection('Result', null, jsonViewer(result, { open: 2 })));
    }
    mount(boardEl, parts);
  }
  const emptyHint = (t) => h('div', { class: 'small muted' }, t);
  function boardSection(title, count, body) {
    return h('section', { class: 'bsec' }, h('h4', { class: 'bsec-title' }, title, count != null && h('span', { class: 'count', style: '--tone:var(--info);margin-left:6px' }, count)), body);
  }

  // ---- dock -----------------------------------------------------------------------------------------------
  const rawModes = new Set(['values', 'updates', 'messages', 'custom', 'tasks', 'checkpoints']);
  let rawFilter = new Set(['updates', 'custom', 'tasks', 'values', 'checkpoints']);
  function renderDock() {
    if (dockTab === 'raw') {
      const entries = run.raw.toArray().filter((r) => rawFilter.has(r.mode) || (!rawModes.has(r.mode) && rawFilter.has('other'))).slice(-150).reverse();
      mount(dockBody,
        h('div', { class: 'row wrap mb-2' }, [...rawModes, 'other'].map((m) => h('button', { class: 'chip', type: 'button', 'aria-pressed': String(rawFilter.has(m)), onclick: () => { if (rawFilter.has(m)) rawFilter.delete(m); else rawFilter.add(m); renderDock(); } }, `${m}${run.counts.byMode[m] ? ' ' + run.counts.byMode[m] : ''}`)), h('span', { class: 'muted tiny' }, `${run.raw.size} buffered (ring of ${run.raw.capacity}, ${run.raw.dropped} dropped)`)),
        entries.length ? h('div', { class: 'rawlist' }, entries.map((r) => h('details', { class: 'raw-item' }, h('summary', null, badge(r.mode, 'info', { sq: true }), r.ns && h('span', { class: 'mono tiny muted' }, r.ns), h('span', { class: 'mono small truncate' }, truncate(typeof r.data === 'string' ? r.data : JSON.stringify(r.data), 160))), h('pre', { class: 'mono tiny' }, safeJson(r.data))))) : emptyHint('No raw parts yet for the selected modes.'),
      );
    } else if (dockTab === 'checkpoints') {
      mount(dockBody, run.checkpoints.length ? h('div', { class: 'col' }, h('div', { class: 'row between' }, h('span', { class: 'small muted' }, `${run.checkpoints.length} checkpoints seen on the stream`), button('Open time travel', { href: link('threads', threadId), size: 'sm', icon: 'history', variant: 'primary' })), h('ol', { class: 'plain-list mono small' }, run.checkpoints.slice(-40).reverse().map((c) => h('li', null, `step ${c.step ?? '?'} · ${shortId(c.id, 12)} · ${c.source || ''} · next: ${(c.next || []).join(', ') || 'end'}`)))) : h('div', { class: 'col' }, emptyHint('Checkpoint snapshots were not on this stream (or the replay window passed).'), button('Open time travel for this thread', { href: link('threads', threadId), icon: 'history', variant: 'primary' })));
    } else if (dockTab === 'events') {
      const corr = incident?.correlation_id;
      const evs = incident?.events?.length ? incident.events.slice().reverse() : corr ? app.live.feed.ring.filter((e) => e.correlation_id === corr).reverse() : [];
      mount(dockBody, evs.length ? h('div', { class: 'ev-list' }, evs.map((e) => eventRow(e, { onOpen: (x) => openEventDrawer(app, x.id) }))) : emptyHint(incident ? 'No linked events recorded.' : 'Open this run from an incident to see its events.'));
    } else if (dockTab === 'state') {
      mount(dockBody, run.values ? jsonViewer(run.values, { open: 1 }) : emptyHint('No state snapshot yet.'));
    }
  }

  // ---- flush ---------------------------------------------------------------------------------------------
  const flushSoon = rafThrottle(() => {
    const ch = new Set(pendingChanged);
    pendingChanged.clear();
    flush(ch);
  });
  function flush(ch) {
    if (dead) return;
    if (ch.has('lanes')) renderStream();
    if (ch.has('timeline') || ch.has('status')) renderTimeline();
    if (ch.has('board') || ch.has('notes') || ch.has('values') || ch.has('status')) renderBoard();
    if (ch.has('interrupts') || ch.has('status')) renderBanner();
    if (ch.has('status') || ch.has('lanes')) renderHeader();
    if (ch.has('raw') || ch.has('checkpoints') || ch.has('values')) {
      if (dockTab !== 'raw' || Date.now() - lastRawPaint > 400) {
        lastRawPaint = Date.now();
        renderDock();
      }
    }
  }
  let lastRawPaint = 0;
  const tickTimeline = setInterval(() => {
    if (run.status === 'running') renderTimeline();
  }, 1000);
  disposers.push(() => clearInterval(tickTimeline));

  function onFrame(ev) {
    const ch = applySSE(run, ev);
    for (const c of ch) pendingChanged.add(c);
    if (ev.event === 'end') {
      refreshState();
      incidentRefreshSoon();
    }
    flushSoon();
  }

  // ---- hydrate + attach ------------------------------------------------------------------------------------
  async function refreshState() {
    try {
      const st = await app.api.lgState(threadId);
      if (dead) return;
      if (st?.values && !run.values) run.values = st.values;
      else if (st?.values) run.values = { ...run.values, ...st.values };
      const ints = [];
      for (const t of st?.tasks || []) for (const i of t.interrupts || []) ints.push(normalizeInterrupt(i));
      if (ints.length && !run.interrupts.length) {
        run.interrupts = ints.filter(Boolean);
        run.status = 'interrupted';
      } else if (!ints.length && run.status === 'interrupted' && !st?.next?.length) {
        run.status = 'done';
      }
      if (!st?.next?.length && !ints.length && run.status === 'idle') run.status = 'done';
      flush(new Set(['values', 'interrupts', 'status', 'board', 'timeline', 'lanes']));
    } catch (e) {
      if (e.status === 404) mount(boardEl, errorBox(e, null, 'Thread not found'));
    }
  }
  const incidentRefreshSoon = (() => {
    let t;
    return () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        if (!incidentId || dead) return;
        try {
          incident = await app.api.incident(incidentId);
          if (dockTab === 'events') renderDock();
        } catch {
          /* optional */
        }
      }, 800);
    };
  })();

  async function attach(fresh = true) {
    if (handle) {
      handle.close();
      handle = null;
    }
    let latest = null;
    try {
      const runs = await app.api.lgRuns(threadId, { limit: 1 });
      latest = Array.isArray(runs) ? runs[0] : runs?.runs?.[0] || null;
    } catch {
      /* no runs endpoint answer: rely on state */
    }
    if (dead) return;
    if (latest) {
      if (fresh || latest.run_id !== run.runId) beginRun(run, latest.run_id);
      if (latest.assistant_id && latest.metadata?.graph_id) graphId = latest.metadata.graph_id;
      run.status = { running: 'running', pending: 'running', success: 'done', error: 'error', interrupted: 'interrupted', timeout: 'error' }[latest.status] || run.status;
      if (latest.status === 'error') run.error = latest.error || 'run failed';
    }
    await refreshState();
    if (!latest) {
      conn = 'idle';
      replayNote = 'No run found on this thread yet.';
      flush(new Set(['status', 'lanes']));
      return;
    }
    conn = 'connecting';
    renderHeader();
    handle = joinRun({
      threadId,
      runId: latest.run_id,
      onEvent: onFrame,
      onState: (s, info) => {
        conn = s;
        if (s === 'error') {
          replayNote = info?.error?.status === 404 ? 'The stream replay window (about 2 minutes) has passed; showing the stored thread state.' : errorMessage(info?.error);
          if (run.status === 'running' && info?.error?.status === 404) run.status = 'done';
        }
        if (s === 'closed' && run.status === 'running') {
          finishRun(run);
          refreshState();
        }
        flush(new Set(['status', 'lanes']));
      },
    });
  }
  let reattachTimer = null;
  function reattachSoon() {
    clearTimeout(reattachTimer);
    reattachTimer = setTimeout(() => attach(false), 500);
  }
  disposers.push(() => {
    clearTimeout(reattachTimer);
    if (handle) handle.close();
  });

  // new approvals/decisions for this thread, made elsewhere (Studio, another tab), refresh us
  disposers.push(
    app.live.on('event', ({ event: e, backfill }) => {
      if (backfill) return;
      const same = (incident?.correlation_id && e.correlation_id === incident.correlation_id) || e.payload?.thread_id === threadId;
      if (!same) return;
      if (e.type === 'approval.decided' || e.type === 'run.started') reattachSoon();
      if (dockTab === 'events') incidentRefreshSoon();
    }),
  );

  renderHeader();
  renderTimeline();
  renderStream();
  renderBoard();
  renderDock();
  await attach(true);

  return () => {
    dead = true;
    disposers.forEach((d) => d());
    respondDrawer?.close();
  };
}

export { streamRun };
