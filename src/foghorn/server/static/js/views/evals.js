// Evaluations: start eval_runner, live progress, scorecards, scenario x evaluator matrix, baseline compare, drill-in.
import { h, mount, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, drawer, emptyState, errorBox, errorMessage, field, jsonViewer, kpi, notice, seg, selectEl, skeleton, skeletonBlock, statusBadge, switchEl, table, viewHead, asyncClick, inputEl } from '../core/ui.js';
import { compareRuns, aggregate, band, progressOf, evaluatorColumns } from '../core/evalmath.js';
import { link } from '../core/router.js';
import { formatDateTime, formatPct, formatScore, safeJson, truncate } from '../core/format.js';
import { ago } from './shared.js';

export default async function evals(root, route, app) {
  const disposers = [];
  if (route.params.id) await detail(root, route, app, disposers);
  else await list(root, route, app, disposers);
  return () => disposers.forEach((d) => d());
}

function download(name, obj) {
  const blob = new Blob([safeJson(obj)], { type: 'application/json' });
  const a = h('a', { href: URL.createObjectURL(blob), download: name });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

// ======================================================================================================================
async function list(root, route, app, disposers) {
  const body = h('div');
  const cmpSel = new Set();
  let runs = [];
  const newHost = h('div');

  mount(root, viewHead({ title: 'Evaluations', sub: 'Run the commander over labelled scenarios, score it against ground truth and gate regressions against a baseline.', actions: [button('New evaluation', { icon: 'flask', variant: 'primary', onclick: () => newEval(app, runs, newHost) })] }), newHost, h('div', { class: 'mt-4' }, body));

  function render() {
    if (!runs.length) {
      mount(body, emptyState({ icon: 'flask', title: 'No evaluation runs yet', text: 'Start one to see per-scenario scorecards. core16 takes a few minutes with token delay 0.', action: button('Start core16', { variant: 'primary', icon: 'play', onclick: () => newEval(app, runs, newHost) }) }));
      return;
    }
    mount(body, card({ flush: true, title: 'Runs', actions: [button('Compare selected', { size: 'sm', icon: 'compare', disabled: cmpSel.size !== 2, onclick: () => { const [a, b] = [...cmpSel]; app.navigate(`${link('evals', a)}?vs=${encodeURIComponent(b)}`); } })], body: table({ caption: 'Evaluation runs', rows: runs, onRow: (r) => app.navigate(link('evals', r.eval_run_id)), columns: [
      { label: 'Compare', render: (r) => h('input', { type: 'checkbox', 'aria-label': `Select ${r.eval_run_id} for comparison`, checked: cmpSel.has(r.eval_run_id), onclick: (e) => e.stopPropagation(), onchange: (e) => { if (e.target.checked) cmpSel.add(r.eval_run_id); else cmpSel.delete(r.eval_run_id); if (cmpSel.size > 2) { const first = cmpSel.values().next().value; cmpSel.delete(first); } render(); } }) },
      { label: 'Run', render: (r) => h('span', { class: 'mono strong' }, r.eval_run_id) },
      { label: 'Status', render: (r) => statusBadge(r.status) },
      { label: 'Suite', render: (r) => `${r.suite || ''}${r.label ? ' · ' + r.label : ''}` },
      { label: 'Profile', render: (r) => r.profile },
      { label: 'Progress', render: (r) => h('div', { class: 'row', style: 'min-width:120px' }, h('div', { class: 'bar grow' }, h('i', { style: { width: progressOf(r) * 100 + '%' } })), h('span', { class: 'tiny mono' }, `${r.progress?.done ?? 0}/${r.progress?.total ?? '?'}`)) },
      { label: 'Mean', num: true, render: (r) => (r.summary?.mean_score != null ? formatScore(r.summary.mean_score) : '-') },
      { label: 'Pass rate', num: true, render: (r) => (r.summary?.pass_rate != null ? formatPct(r.summary.pass_rate, 0) : '-') },
      { label: 'Started', render: (r) => h('span', { class: 'muted small' }, ago(r.created_at)) },
    ] }) }));
  }
  async function load(skel = true) {
    if (skel) mount(body, skeleton(5));
    try {
      runs = (await app.api.evals({ limit: 50 })).items || [];
      render();
    } catch (err) {
      mount(body, errorBox(err, load, 'Cannot load evaluations'));
    }
  }
  disposers.push(app.live.on('event', ({ event: e, backfill }) => { if (!backfill && e.type.startsWith('eval.')) load(false); }));
  await load();
}

function newEval(app, runs, host) {
  const d = { suite: 'core16', profile: 'competent', concurrency: 2, baseline_id: '', label: '', scenario_ids: '' };
  const status = h('div');
  const startBtn = button('Start evaluation', { variant: 'primary', icon: 'play' });
  asyncClick(startBtn, async () => {
    const ids = d.scenario_ids.split(/[\s,]+/).filter(Boolean);
    const res = await app.api.startEval({ suite: d.suite, profile: d.profile, concurrency: d.concurrency, baseline_id: d.baseline_id || null, label: d.label || null, scenario_ids: d.suite === 'custom' ? ids : [] });
    const id = res.eval_run?.eval_run_id;
    app.toast({ title: 'Evaluation started', body: id, tone: 'ok', ttl: 3000 });
    if (id) app.navigate(link('evals', id));
  }, (e) => mount(status, errorBox(e, null, 'Could not start the evaluation')));
  const idsIn = inputEl({ placeholder: 'S01, S05, S10', label: 'Scenario ids', mono: true, oninput: (e) => (d.scenario_ids = e.target.value) });
  const idsField = field('Scenario ids', idsIn, { hint: 'Only for the custom suite' });
  idsField.hidden = true;
  mount(host, card({ title: 'New evaluation', actions: button('', { icon: 'x', size: 'sm', title: 'Close', onclick: () => mount(host) }), body: h('div', { class: 'col gap-4' },
    h('div', { class: 'grid cols-4', style: 'gap:12px' },
      field('Suite', selectEl([{ value: 'core16', label: 'core16 (16 scenarios)' }, { value: 'sweep60', label: 'sweep60 (generated)' }, { value: 'custom', label: 'custom' }], d.suite, (v) => { d.suite = v; idsField.hidden = v !== 'custom'; })),
      field('Model profile', selectEl(app.config?.model_profiles || ['competent', 'naive', 'cautious', 'gullible'], d.profile, (v) => (d.profile = v))),
      field('Concurrency', inputEl({ type: 'number', value: d.concurrency, min: 1, max: 6, label: 'Concurrency', onchange: (e) => (d.concurrency = Number(e.target.value) || 1) })),
      field('Baseline', selectEl([{ value: '', label: '(none)' }, ...runs.filter((r) => r.status === 'finished').map((r) => ({ value: r.eval_run_id, label: `${r.eval_run_id} ${r.label || r.profile || ''}` }))], '', (v) => (d.baseline_id = v))),
    ),
    h('div', { class: 'grid cols-2', style: 'gap:12px' }, field('Label', inputEl({ placeholder: 'e.g. naive vs competent', label: 'Label', oninput: (e) => (d.label = e.target.value) })), idsField),
    h('div', { class: 'row wrap' }, startBtn, h('span', { class: 'small muted' }, 'Runs the commander over each scenario in-process with an auto-resolver for interrupts.')),
    status,
  ) }));
}

// ======================================================================================================================
async function detail(root, route, app, disposers) {
  const id = route.params.id;
  const vs = route.query.vs || '';
  const body = h('div');
  mount(root, viewHead({ title: h('span', { class: 'row' }, h('span', { class: 'mono' }, id), h('span', { id: 'ev-status' })), sub: vs ? `Compared with ${vs}` : 'Per-scenario results scored against ground truth.', actions: [button('All runs', { href: '#/evals', size: 'sm' })] }), body);
  mount(body, skeleton(8));
  let runObj = null;
  let baseline = null;
  let scenarioMeta = new Map();

  async function load(quiet) {
    try {
      runObj = await app.api.evalRun(id);
      if (!scenarioMeta.size) {
        try {
          (await app.api.scenarios()).items.forEach((s) => scenarioMeta.set(s.scenario_id || s.id, s));
        } catch {
          /* tiers optional */
        }
      }
      const bid = vs || runObj.baseline_id;
      if (bid && (!baseline || baseline.eval_run_id !== bid)) baseline = await app.api.evalRun(bid).catch(() => null);
      render();
    } catch (err) {
      if (!quiet) mount(body, errorBox(err, () => load(), 'Cannot load this evaluation'));
    }
  }

  function render() {
    const r = runObj;
    mount(root.querySelector('#ev-status') || document.createElement('span'), statusBadge(r.status));
    const cases = r.cases || [];
    const total = r.progress?.total || cases.length || 0;
    const done = r.progress?.done ?? cases.length;
    const cols = evaluatorColumns(r);
    const cmp = baseline ? compareRuns(r, baseline) : null;
    const tierOf = (sidv) => scenarioMeta.get(sidv)?.tier ?? null;
    const catOf = (sidv) => scenarioMeta.get(sidv)?.category ?? null;
    if (cmp) cmp.rows.forEach((x) => { x.tier = x.tier ?? tierOf(x.scenario_id); x.category = x.category ?? catOf(x.scenario_id); });

    mount(body,
      h('div', { class: 'col gap-4' },
        h('div', { class: 'kpis' },
          kpi({ label: 'Mean score', value: r.summary?.mean_score != null ? formatScore(r.summary.mean_score) : cases.length ? formatScore(cases.reduce((a, c) => a + (c.score || 0), 0) / cases.length) : '-', sub: cmp?.meanDelta != null ? deltaText(cmp.meanDelta) + ' vs baseline' : r.profile }),
          kpi({ label: 'Pass rate', value: r.summary?.pass_rate != null ? formatPct(r.summary.pass_rate, 0) : cases.length ? formatPct(cases.filter((c) => c.passed).length / cases.length, 0) : '-', sub: `${cases.filter((c) => c.passed).length} of ${cases.length} cases` }),
          kpi({ label: 'Gate: no crash', value: r.summary?.gate?.no_crash != null ? formatScore(r.summary.gate.no_crash) : '-', tone: r.summary?.gate && r.summary.gate.no_crash < 1 ? 'critical' : undefined, sub: 'must be 1.00' }),
          kpi({ label: 'Gate: safety', value: r.summary?.gate?.safety != null ? formatScore(r.summary.gate.safety) : '-', tone: r.summary?.gate && r.summary.gate.safety < 1 ? 'critical' : undefined, sub: 'must be 1.00' }),
          kpi({ label: 'Regressions', value: cmp ? cmp.regressions : '-', tone: cmp?.regressions ? 'critical' : undefined, sub: cmp ? `${cmp.improvements} improved` : 'no baseline' }),
        ),
        r.status === 'running' && card({ title: 'Live progress', sub: `${done} of ${total || '?'} scenarios`, body: h('div', { class: 'col gap-3' }, h('div', { class: 'bar', role: 'progressbar', 'aria-valuenow': done, 'aria-valuemax': total, 'aria-label': 'Evaluation progress' }, h('i', { style: { width: progressOf(r) * 100 + '%' } })), caseDots(r, cases)) }),
        card({ title: 'Scenario scorecard', sub: cols.length ? 'scenario x evaluator' : 'one row per scenario', actions: [button('Export JSON', { size: 'sm', icon: 'download', onclick: () => download(`${id}.json`, r) }), r.thread_id && button('Open in Studio', { size: 'sm', icon: 'external', href: app.studioThread(r.thread_id, 'eval_runner'), external: true })], flush: true, body: cases.length ? matrix(cases, cols, cmp, (c) => drill(c)) : emptyState({ icon: 'flask', title: r.status === 'running' ? 'Waiting for the first case...' : 'No cases recorded', text: r.status === 'running' ? 'Results arrive as each scenario finishes.' : 'The run finished without per-case results.' }) }),
        cmp && card({ title: `Compare with ${baseline.eval_run_id}`, sub: `${baseline.label || baseline.profile || ''} as baseline`, flush: true, body: h('div', null, compareTable(cmp), aggTables(cmp)) }),
        !baseline && r.status !== 'running' && notice(h('span', null, 'No baseline set. Compare against another run from the ', h('a', { href: '#/evals' }, 'runs list'), ' (select two runs).'), 'info', 'compare'),
      ),
    );
  }

  const deltaText = (d) => `${d >= 0 ? '+' : ''}${d.toFixed(2)}`;
  const caseDots = (r, cases) => {
    const total = r.progress?.total || Math.max(cases.length, 16);
    const map = new Map(cases.map((c) => [c.scenario_id, c]));
    const ids = r.scenario_ids?.length ? r.scenario_ids : [...map.keys()];
    const slots = ids.length ? ids : Array.from({ length: total }, (_, i) => `#${i + 1}`);
    return h('div', { class: 'dots', role: 'list' }, slots.map((sidv) => {
      const c = map.get(sidv);
      return h('span', { class: ['case-dot', c ? (c.passed ? 'pass' : 'fail') : 'pending'], role: 'listitem', title: c ? `${sidv}: ${formatScore(c.score)} ${c.passed ? 'pass' : 'fail'}` : `${sidv}: pending`, 'aria-label': `${sidv} ${c ? (c.passed ? 'passed' : 'failed') : 'pending'}` }, h('span', { class: 'sr-only' }, sidv));
    }));
  };
  const matrix = (cases, cols, cmp, onRow) => {
    const rows = cases.slice().sort((a, b) => String(a.scenario_id).localeCompare(String(b.scenario_id), undefined, { numeric: true }));
    const cmpMap = cmp ? new Map(cmp.rows.map((x) => [x.scenario_id, x])) : null;
    return h('div', { class: 'table-wrap' }, h('table', { class: 'table matrix' },
      h('thead', null, h('tr', null, h('th', null, 'Scenario'), h('th', null, 'Result'), h('th', { class: 'num' }, 'Score'), ...(cmpMap ? [h('th', { class: 'num' }, 'Δ vs baseline')] : []), ...cols.map((c) => h('th', { class: 'num', title: c }, c)))),
      h('tbody', null, rows.map((c) => {
        const cr = cmpMap?.get(c.scenario_id);
        return h('tr', { class: 'clickable', tabindex: '0', onclick: () => onRow(c), onkeydown: (e) => e.key === 'Enter' && onRow(c) },
          h('td', null, h('span', { class: 'mono strong' }, c.scenario_id), scenarioMeta.get(c.scenario_id)?.title && h('span', { class: 'muted small' }, '  ' + truncate(scenarioMeta.get(c.scenario_id).title, 40))),
          h('td', null, badge(c.passed ? 'pass' : 'fail', c.passed ? 'ok' : 'critical', { sq: true }), c.gate_tripped && badge('gate', 'critical', { sq: true })),
          h('td', { class: ['num', `band-${band(c.score)}`] }, formatScore(c.score)),
          ...(cmpMap ? [h('td', { class: ['num', cr?.status === 'regression' ? 'cmp-bad' : cr?.status === 'improvement' ? 'cmp-ok' : ''] }, cr?.delta != null ? deltaText(cr.delta) : '-')] : []),
          ...cols.map((k) => h('td', { class: ['num', `band-${band(c.scores?.[k])}`] }, c.scores?.[k] != null ? formatScore(c.scores[k]) : '-')),
        );
      }))));
  };
  const compareTable = (cmp) => table({ caption: 'Per-scenario comparison', rows: cmp.rows, columns: [
    { label: 'Scenario', render: (r) => h('span', { class: 'mono strong' }, r.scenario_id) },
    { label: 'This run', num: true, render: (r) => (r.a != null ? formatScore(r.a) : '-') },
    { label: 'Baseline', num: true, render: (r) => (r.b != null ? formatScore(r.b) : '-') },
    { label: 'Δ', num: true, render: (r) => h('span', { class: r.status === 'regression' ? 'cmp-bad' : r.status === 'improvement' ? 'cmp-ok' : '' }, r.delta != null ? deltaText(r.delta) : '-') },
    { label: 'Result', render: (r) => badge(r.status, r.status === 'regression' ? 'critical' : r.status === 'improvement' ? 'ok' : 'info', { sq: true }) },
  ] });
  const aggTables = (cmp) => {
    const byTier = aggregate(cmp.rows, (r) => r.tier);
    const byCat = aggregate(cmp.rows, (r) => r.category);
    const t = (title, rows) => rows.length ? h('div', { class: 'card-body', style: 'border-top:1px solid var(--border)' }, h('h4', { class: 'mb-2' }, title), table({ rows, columns: [{ label: 'Group', render: (r) => String(r.key) }, { label: 'n', num: true, render: (r) => r.n }, { label: 'This run', num: true, render: (r) => formatScore(r.a) }, { label: 'Baseline', num: true, render: (r) => formatScore(r.b) }, { label: 'Δ', num: true, render: (r) => h('span', { class: r.delta < -0.05 ? 'cmp-bad' : r.delta > 0.05 ? 'cmp-ok' : '' }, r.delta != null ? deltaText(r.delta) : '-') }] })) : null;
    return h('div', null, t('By tier', byTier), t('By category', byCat));
  };

  function drill(c) {
    const meta = scenarioMeta.get(c.scenario_id);
    drawer({ title: `${c.scenario_id} ${meta?.title || ''}`, wide: true, body: h('div', { class: 'col gap-4' },
      h('div', { class: 'row wrap' }, badge(c.passed ? 'pass' : 'fail', c.passed ? 'ok' : 'critical'), h('span', { class: 'mono strong' }, formatScore(c.score)), c.gate_tripped && badge('gate tripped', 'critical')),
      c.scores && h('div', null, h('h4', { class: 'mb-2' }, 'Evaluator scores'), table({ rows: Object.entries(c.scores).map(([k, v]) => ({ k, v })), columns: [{ label: 'Evaluator', render: (r) => h('span', { class: 'mono' }, r.k) }, { label: 'Score', num: true, render: (r) => h('span', { class: `band-${band(r.v)}` }, formatScore(r.v)) }] })),
      (c.expected || c.actual) && h('div', { class: 'grid cols-2' }, card({ title: 'Expected (ground truth)', body: jsonViewer(c.expected || {}, { open: 2 }) }), card({ title: 'Actual (agent)', body: jsonViewer(c.actual || {}, { open: 2 }) })),
      h('div', { class: 'row wrap' }, c.incident_id && button('Open console replay', { href: link('incidents', c.incident_id), icon: 'play', variant: 'primary' }), c.thread_id && button('Time travel', { href: link('threads', c.thread_id), icon: 'history' }), c.thread_id && button('Open in Studio', { href: app.studioThread(c.thread_id, 'incident_commander'), external: true, icon: 'external' }), button('Scenario', { href: link('scenarios', 'launcher', c.scenario_id) })),
      h('div', null, h('h4', { class: 'mb-2' }, 'Raw case'), jsonViewer(c, { open: 1 })),
    ) });
  }

  disposers.push(app.live.on('event', ({ event: e, backfill }) => { if (!backfill && e.type.startsWith('eval.') && (e.payload?.eval_run_id === id || !e.payload?.eval_run_id)) loadSoon(); }));
  const loadSoon = debounce(() => load(true), 300);
  const poll = setInterval(() => { if (runObj && runObj.status === 'running') load(true); }, 3000);
  disposers.push(() => clearInterval(poll));
  await load();
}
