// Scenarios: 16 labelled scenarios + launcher, raw event injector, and side-by-side run compare.
import { h, mount, debounce } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, card, chip, confirmDialog, emptyState, errorBox, errorMessage, field, incBadge, inputEl, jsonViewer, markdown, seg, selectEl, skeletonGrid, statusBadge, switchEl, tabs, textareaEl, viewHead, asyncClick, skeleton, notice } from '../core/ui.js';
import { link } from '../core/router.js';
import { lsGet, lsSet } from '../core/store.js';
import { formatDuration, hashString, safeJson, truncate } from '../core/format.js';

const TIER_TONE = { 1: 'ok', 2: 'medium', 3: 'high' };
const FALLBACK_NODES = ['bootstrap', 'supervisor', 'metrics_analyst', 'logs_analyst', 'change_analyst', 'topology_analyst', 'knowledge_agent', 'rca_tournament', 'clarify_change_intent', 'remediation_planner', 'policy_check', 'approval_gate', 'execute_action', 'verify_recovery', 'comms_drafter', 'close_incident'];
const SPEEDS = [1, 10, 60, 600];

const sid = (s) => s.scenario_id || s.id;

export default async function scenarios(root, route, app) {
  const tab = ['launcher', 'inject', 'compare'].includes(route.params.tab) ? route.params.tab : 'launcher';
  const panel = h('div');
  const disposers = [];
  const tabsEl = tabs([{ key: 'launcher', label: 'Launcher' }, { key: 'inject', label: 'Inject raw event' }, { key: 'compare', label: 'Compare runs' }], tab, (k) => app.navigate(link('scenarios', k)), { label: 'Scenario tools' });
  mount(root, viewHead({ title: 'Scenarios', sub: '16 labelled incidents in three difficulty tiers. Launch one and watch the agents work; ground truth stays hidden until you reveal it.', actions: [button('Fault injector', { href: '#/faults', icon: 'bolt' })] }), tabsEl, h('div', { class: 'mt-4' }, panel));

  if (tab === 'launcher') await launcher(panel, route, app, disposers);
  else if (tab === 'inject') await injector(panel, app);
  else await compare(panel, route, app);
  return () => disposers.forEach((d) => d());
}

// ======================================================================================================================
async function launcher(panel, route, app, disposers) {
  let items = [];
  let tierSel = new Set();
  let q = '';
  let selected = route.params.id || null;
  const gridEl = h('div');
  const detailHost = h('div');
  const launches = lsGet('launches', []);

  const filters = h('div', { class: 'toolbar mb-3' },
    h('div', { class: 'row', role: 'group', 'aria-label': 'Tier' }, [1, 2, 3].map((t) => h('button', { class: 'chip', type: 'button', 'aria-pressed': 'false', onclick: (e) => { if (tierSel.has(t)) tierSel.delete(t); else tierSel.add(t); e.currentTarget.setAttribute('aria-pressed', String(tierSel.has(t))); renderGrid(); } }, `Tier ${t}`))),
    h('div', { style: 'width:240px;max-width:100%' }, inputEl({ size: 'sm', placeholder: 'Search scenarios', label: 'Search scenarios', oninput: debounce((e) => { q = e.target.value.toLowerCase(); renderGrid(); }, 120) })),
  );
  mount(panel, filters, h('div', { class: 'launch-layout' }, gridEl, detailHost));
  mount(gridEl, skeletonGrid(8, 120));

  function featuresOf(s) {
    const f = s.features || s.tags || s.exercises || [];
    const out = [...f];
    const d = s.world?.difficulty;
    if (d && !f.length) {
      if (d.red_herrings > 0) out.push(`${d.red_herrings} red herring${d.red_herrings > 1 ? 's' : ''}`);
      if (d.lookback_days_needed > 0) out.push(`${d.lookback_days_needed}d lookback`);
      if (d.telemetry_gap_components?.length) out.push('telemetry gap');
      if (s.stats?.alerts) out.push(`${s.stats.alerts} alerts`);
    }
    if (s.expected_interrupts) for (const i of s.expected_interrupts) if (!out.includes(i.kind)) out.push(i.kind);
    return out.slice(0, 5);
  }

  function renderGrid() {
    const list = items.filter((s) => (!tierSel.size || tierSel.has(Number(s.tier))) && (!q || `${sid(s)} ${s.title} ${s.premise || ''} ${s.category || ''}`.toLowerCase().includes(q)));
    if (!list.length) {
      mount(gridEl, emptyState({ icon: 'target', title: items.length ? 'No scenarios match' : 'No scenarios available', text: items.length ? 'Clear the filters.' : 'The data layer returned no scenarios.' }));
      return;
    }
    mount(gridEl, h('div', { class: 'grid auto scen-grid' }, list.map((s) =>
      h('button', { class: ['card interactive scen-card', sid(s) === selected && 'selected'], type: 'button', onclick: () => select(sid(s)), 'aria-pressed': String(sid(s) === selected) },
        h('div', { class: 'row between' }, h('span', { class: 'mono strong' }, sid(s)), badge(`Tier ${s.tier ?? '?'}`, TIER_TONE[s.tier] || 'info', { sq: true })),
        h('div', { class: 'scen-title' }, s.title || sid(s)),
        (s.premise || s.description || s.summary) && h('div', { class: 'small muted scen-premise' }, truncate(s.premise || s.description || s.summary, 120)),
        h('div', { class: 'row wrap', style: 'margin-top:auto' }, s.category && chip(s.category), featuresOf(s).map((f) => chip(f))),
      ),
    )));
  }

  function select(id) {
    selected = id;
    renderGrid();
    renderDetail();
    history.replaceState(null, '', link('scenarios', 'launcher', id));
  }

  // ---- launch panel --------------------------------------------------------------------------------------
  async function renderDetail() {
    const s = items.find((x) => sid(x) === selected);
    if (!s) {
      mount(detailHost, h('div', { class: 'card launch-card' }, h('div', { class: 'card-body' }, emptyState({ icon: 'target', title: 'Choose a scenario', text: 'Pick a card to configure and launch it.' }))));
      return;
    }
    const cfg = app.config || {};
    const d = cfg.defaults || {};
    const opts = { mode: 'events', profile: d.model_profile || 'competent', approval_policy: d.approval_policy || 'standard', speed: d.replay_speed || 60, breakpoints: [], token_delay_ms: 15, memory_enabled: true, chaos: '' };
    const status = h('div', { class: 'col' });
    const truthHost = h('div');
    let nodes = FALLBACK_NODES;
    try {
      const g = await app.api.lgGraph('incident_commander', false);
      const ns = (g.nodes || []).map((n) => n.id).filter((x) => !x.startsWith('__') && !x.includes(':'));
      if (ns.length) nodes = ns;
    } catch {
      /* keep fallback */
    }
    const bpWrap = h('div', { class: 'row wrap' }, nodes.map((n) => h('button', { class: 'chip mono', type: 'button', 'aria-pressed': 'false', title: 'Static breakpoint: pause BEFORE this node (root-graph nodes only)', onclick: (e) => { const i = opts.breakpoints.indexOf(n); if (i >= 0) opts.breakpoints.splice(i, 1); else opts.breakpoints.push(n); e.currentTarget.setAttribute('aria-pressed', String(i < 0)); } }, n)));
    const delayLabel = h('span', { class: 'mono small' }, `${opts.token_delay_ms} ms`);
    const launchBtn = button('Launch', { icon: 'play', variant: 'primary' });
    asyncClick(launchBtn, async () => {
      const body = { mode: opts.mode, profile: opts.profile, approval_policy: opts.approval_policy, speed: opts.speed, breakpoints: opts.breakpoints, token_delay_ms: opts.token_delay_ms, memory_enabled: opts.memory_enabled, chaos: opts.chaos || null, replace: true };
      const res = await app.api.launchScenario(sid(s), body);
      launches.unshift({ scenario_id: sid(s), launch_id: res.launch_id, incident_id: res.incident_id || null, correlation_id: res.correlation_id, profile: opts.profile, policy: opts.approval_policy, at: Date.now() });
      lsSet('launches', launches.slice(0, 20));
      app.toast({ title: `Launched ${sid(s)}`, body: `${res.events_total ?? '?'} events at ${res.speed || opts.speed}x · ${opts.profile}`, tone: 'ok', ttl: 3500 });
      if (res.incident_id) return app.navigate(link('incidents', res.incident_id));
      mount(status, notice(h('span', null, 'Launched. Waiting for ', h('code', null, 'incident.opened'), '... ', h('a', { href: '#/' }, 'watch Mission Control')), 'accent', 'activity'));
      const off = app.live.on('event', ({ event: e }) => {
        if (e.type === 'incident.opened' && (e.correlation_id === res.correlation_id || e.scenario_id === sid(s) || e.payload?.scenario_id === sid(s))) {
          off();
          const id = e.payload?.incident_id;
          if (id) {
            launches[0].incident_id = id;
            lsSet('launches', launches.slice(0, 20));
            mount(status, notice(h('span', null, `${id} opened. `, h('a', { href: link('incidents', id) }, 'Open console')), 'ok', 'check-circle'));
            if (location.hash.startsWith('#/scenarios')) app.navigate(link('incidents', id));
          }
        }
      });
      disposers.push(off);
    }, (e) => mount(status, errorBox(e, null, 'Launch failed')));

    const revealBtn = button('Reveal ground truth', { icon: 'eye', size: 'sm' });
    asyncClick(revealBtn, async () => {
      const ran = launches.some((l) => l.scenario_id === sid(s));
      if (!ran) {
        const ok = await confirmDialog({ title: 'Spoiler warning', text: 'You have not run this scenario in this browser yet. Revealing the root cause and remediation now removes the detective game. Reveal anyway?', confirmLabel: 'Reveal' });
        if (!ok) return;
      }
      const full = await app.api.scenario(sid(s), true);
      mount(truthHost, h('div', { class: 'col' }, h('div', { class: 'notice t-medium' }, icon('eye'), h('div', { class: 'small' }, 'Ground truth is what the generator injected. Compare it with what the agents concluded.')), jsonViewer(full.truth || full, { open: 2 })));
      revealBtn.hidden = true;
    }, (e) => mount(truthHost, errorBox(e)));

    mount(
      detailHost,
      h('aside', { class: 'card launch-card', 'aria-label': `Launch ${sid(s)}` },
        h('div', { class: 'card-head' }, h('div', { class: 'grow' }, h('div', { class: 'row' }, h('strong', { class: 'mono' }, sid(s)), badge(`Tier ${s.tier ?? '?'}`, TIER_TONE[s.tier] || 'info', { sq: true })), h('div', { class: 'strong' }, s.title))),
        h('div', { class: 'card-body col gap-3' },
          (s.premise || s.description || s.summary) && h('p', { class: 'small muted' }, s.premise || s.description || s.summary),
          h('div', { class: 'grid cols-2', style: 'gap:12px' },
            field('Model profile', selectEl(cfg.model_profiles || ['competent', 'naive', 'cautious', 'gullible'], opts.profile, (v) => (opts.profile = v)), { hint: 'competent follows evidence; naive blames the first suspect; cautious asks; gullible obeys injected text' }),
            field('Approval policy', selectEl(cfg.approval_policies || ['strict', 'standard', 'auto_low_risk'], opts.approval_policy, (v) => (opts.approval_policy = v))),
          ),
          h('div', { class: 'grid cols-2', style: 'gap:12px' },
            field('Mode', seg([{ value: 'events', label: 'Via events', title: 'Publish the scenario events: triage decides' }, { value: 'direct', label: 'Direct', title: 'Skip the router, start the commander' }], opts.mode, (v) => (opts.mode = v), { label: 'Launch mode' })),
            field('Replay speed', seg(SPEEDS.map((v) => ({ value: v, label: `${v}x` })), opts.speed, (v) => (opts.speed = v), { label: 'Replay speed' })),
          ),
          field('Token delay', h('div', { class: 'row' }, h('input', { type: 'range', class: 'range', min: 0, max: 100, step: 5, value: opts.token_delay_ms, 'aria-label': 'Token delay in ms', oninput: (e) => { opts.token_delay_ms = Number(e.target.value); delayLabel.textContent = `${opts.token_delay_ms} ms`; } }), delayLabel), { hint: '0 for tests; 15 makes streaming visible' }),
          field('Breakpoints', bpWrap, { hint: 'Pause before these nodes (static interrupt_before). Resume from the console or Studio.' }),
          h('div', { class: 'row wrap' }, switchEl('Memory enabled', true, (v) => (opts.memory_enabled = v)), selectEl([{ value: '', label: 'No chaos' }, { value: 'tools', label: 'Chaos: transient tool failures' }], '', (v) => (opts.chaos = v), { size: 'sm', label: 'Chaos' })),
          h('div', { class: 'row wrap' }, launchBtn, revealBtn, launches.some((l) => l.scenario_id === sid(s)) && h('span', { class: 'small muted' }, `${launches.filter((l) => l.scenario_id === sid(s)).length} run(s) launched here`)),
          status,
          truthHost,
          launches.filter((l) => l.scenario_id === sid(s) && l.incident_id).length > 0 && h('div', null, h('h4', { class: 'mb-2' }, 'Your runs'), h('ul', { class: 'plain-list' }, launches.filter((l) => l.scenario_id === sid(s) && l.incident_id).slice(0, 5).map((l) => h('li', { class: 'row small' }, h('a', { class: 'mono', href: link('incidents', l.incident_id) }, l.incident_id), chip(l.profile), chip(l.policy), h('span', { class: 'grow' }), h('a', { href: link('scenarios', 'compare') + `?a=${encodeURIComponent(l.incident_id)}` }, 'compare'))))),
        ),
      ),
    );
  }

  try {
    items = (await app.api.scenarios()).items || [];
    items.sort((a, b) => String(sid(a)).localeCompare(String(sid(b)), undefined, { numeric: true }));
    if (!selected && items.length) selected = null;
    renderGrid();
    renderDetail();
  } catch (err) {
    mount(gridEl, errorBox(err, () => location.reload(), 'Cannot load scenarios'));
  }
}

// ======================================================================================================================
async function injector(panel, app) {
  let presets = [];
  const out = h('div');
  const typeIn = inputEl({ value: 'alert.fired', mono: true, label: 'Event type' });
  const compIn = inputEl({ value: 'checkout-service', label: 'Component' });
  const sevSel = selectEl(['info', 'low', 'medium', 'high', 'critical'], 'high', null, { label: 'Severity' });
  const payload = textareaEl({ rows: 7, mono: true, value: '{\n  "rule": "error_rate_high",\n  "value": 0.071,\n  "threshold": 0.02\n}', label: 'Payload JSON', spellcheck: false });
  const idem = inputEl({ placeholder: 'e.g. demo-1 (send twice to see dedupe)', mono: true, label: 'Idempotency key' });
  const dedupe = inputEl({ placeholder: 'optional dedupe_key', mono: true, label: 'Dedupe key' });
  const presetSel = selectEl([{ value: '', label: 'Loading presets...' }], '', (v) => {
    const p = presets.find((x) => x.preset === v);
    if (!p || !p.envelope) return;
    const e = p.envelope;
    typeIn.value = e.type || typeIn.value;
    compIn.value = e.subject?.component || '';
    sevSel.value = e.severity || 'info';
    payload.value = safeJson(e.payload || {});
    if (e.dedupe_key) dedupe.value = e.dedupe_key;
  }, { label: 'Preset' });

  const send = async (times = 1) => {
    let pl;
    try {
      pl = payload.value.trim() ? JSON.parse(payload.value) : {};
    } catch (e) {
      mount(out, errorBox(new Error(`Payload is not valid JSON: ${e.message}`), null, 'Check the payload'));
      return;
    }
    const envelope = { type: typeIn.value.trim(), source: 'ui.inject', severity: sevSel.value, subject: { component: compIn.value.trim() || null, environment: 'prod' }, payload: pl, dedupe_key: dedupe.value.trim() || undefined, idempotency_key: idem.value.trim() || undefined };
    const results = [];
    for (let i = 0; i < times; i++) {
      try {
        const r = await app.api.postEvent(envelope, idem.value.trim() || undefined);
        results.push({ ok: true, r });
      } catch (e) {
        results.push({ ok: false, e });
      }
    }
    mount(out, h('div', { class: 'col' }, results.map((x, i) => (x.ok
      ? h('div', { class: 'notice t-ok' }, icon('check-circle'), h('div', { class: 'small' }, `#${i + 1}: `, h('strong', null, x.r.status || 'accepted'), x.r.event_id && h('span', null, ' ', h('a', { class: 'mono', href: link('events', x.r.event_id) }, x.r.event_id)), x.r.reason && ` (${x.r.reason})`, (x.r.dispatches || []).length > 0 && ` → ${x.r.dispatches.map((d) => d.graph_id).join(', ')}`))
      : h('div', { class: 'notice t-critical' }, icon('alert-circle'), h('div', { class: 'small' }, `#${i + 1}: ${errorMessage(x.e)}`))))));
  };
  const sendBtn = button('Send event', { variant: 'primary', icon: 'send' });
  const twiceBtn = button('Send twice (dedupe demo)', { icon: 'copy', title: 'Sends the same envelope twice with one idempotency key' });
  asyncClick(sendBtn, () => send(1));
  asyncClick(twiceBtn, () => { if (!idem.value.trim()) idem.value = `demo-${Date.now().toString(36)}`; return send(2); });

  mount(panel, card({ title: 'Raw event', sub: 'All ingress paths share one pipeline. This posts a foghorn.event/1 envelope to POST /api/events.', body: h('div', { class: 'col gap-4', style: 'max-width:760px' },
    field('Preset', presetSel),
    h('div', { class: 'grid cols-3', style: 'gap:12px' }, field('Type', typeIn), field('Component', compIn), field('Severity', sevSel)),
    field('Payload', payload),
    h('div', { class: 'grid cols-2', style: 'gap:12px' }, field('Idempotency key', idem, { hint: 'same key = same delivery: the second send returns "duplicate"' }), field('Dedupe key', dedupe, { hint: 'different event, same meaning: coalesced as "x2"' })),
    h('div', { class: 'row wrap' }, sendBtn, twiceBtn),
    out,
  ) }));
  try {
    presets = (await app.api.eventPresets()).items || [];
    mount(presetSel, h('option', { value: '' }, 'Choose a preset...'), presets.map((p) => h('option', { value: p.preset }, p.label || p.preset)));
  } catch {
    mount(presetSel, h('option', { value: '' }, 'No presets available'));
  }
}

// ======================================================================================================================
async function compare(panel, route, app) {
  const body = h('div');
  let list = [];
  try {
    list = (await app.api.incidents({ limit: 100 })).items || [];
  } catch (err) {
    mount(panel, errorBox(err, () => location.reload()));
    return;
  }
  const opts = [{ value: '', label: 'Choose a run...' }, ...list.map((i) => ({ value: i.incident_id, label: `${i.incident_id}  ${i.scenario_id || ''}  ${i.severity || ''}  ${truncate(i.title || '', 40)}` }))];
  let a = route.query.a || '';
  let b = route.query.b || '';
  const aSel = selectEl(opts, a, (v) => { a = v; run(); }, { label: 'Run A' });
  const bSel = selectEl(opts, b, (v) => { b = v; run(); }, { label: 'Run B' });
  mount(panel, h('div', { class: 'col gap-4' }, h('div', { class: 'grid cols-2', style: 'gap:12px;max-width:900px' }, field('Run A', aSel), field('Run B', bSel)), body));

  async function load(id) {
    const inc = await app.api.incident(id);
    let state = null;
    const tid = inc.thread_id || inc.threads?.[0]?.thread_id;
    if (tid) state = await app.api.lgState(tid).catch(() => null);
    return { inc, state, values: state?.values || {} };
  }
  async function run() {
    if (!a || !b) {
      mount(body, emptyState({ icon: 'compare', title: 'Pick two runs', text: 'Run the same scenario with two model profiles or approval policies, then compare what each concluded.' }));
      return;
    }
    mount(body, skeleton(6));
    try {
      const [A, B] = await Promise.all([load(a), load(b)]);
      let truth = null;
      if (A.inc.scenario_id && A.inc.scenario_id === B.inc.scenario_id) truth = await app.api.scenario(A.inc.scenario_id, true).then((x) => x.truth).catch(() => null);
      const top = (v) => (Array.isArray(v.hypotheses) && v.hypotheses.length ? [...v.hypotheses].sort((x, y) => (y.score || 0) - (x.score || 0))[0] : null);
      const dur = (i) => (i.opened_at && i.resolved_at ? formatDuration(Date.parse(i.resolved_at) - Date.parse(i.opened_at)) : '');
      const rows = [
        ['Scenario', A.inc.scenario_id, B.inc.scenario_id],
        ['Severity', A.inc.severity, B.inc.severity, truth?.severity],
        ['Status', A.inc.status, B.inc.status],
        ['Top hypothesis', top(A.values)?.component, top(B.values)?.component, truth?.root_cause?.component],
        ['Hypothesis category', top(A.values)?.category, top(B.values)?.category, truth?.root_cause?.category],
        ['Chosen action', A.values.chosen_action ? `${A.values.chosen_action.id || A.values.chosen_action.action} ${A.values.chosen_action.target || ''}` : '', B.values.chosen_action ? `${B.values.chosen_action.id || B.values.chosen_action.action} ${B.values.chosen_action.target || ''}` : '', truth?.remediation?.preferred],
        ['Approvals', (A.inc.approvals || []).map((x) => `${x.kind}:${x.status}`).join(', '), (B.inc.approvals || []).map((x) => `${x.kind}:${x.status}`).join(', ')],
        ['Events', String(A.inc.events?.length ?? ''), String(B.inc.events?.length ?? '')],
        ['Duration (virtual)', dur(A.inc), dur(B.inc)],
        ['Flags', (A.values.flags || []).join(', '), (B.values.flags || []).join(', '), (truth?.flags || []).join(', ')],
      ];
      mount(body, card({ flush: true, title: 'Side by side', sub: truth ? 'Ground truth column shown (same scenario)' : 'Different scenarios: no ground-truth column', body: h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
        h('thead', null, h('tr', null, ['', 'Run A', 'Run B', truth && 'Ground truth'].filter((x) => x !== null && x !== false).map((x) => h('th', null, x)))),
        h('tbody', null, rows.map(([label, x, y, t]) => {
          const diff = String(x ?? '') !== String(y ?? '');
          const ok = (v) => (t !== undefined && t !== null && t !== '' ? (String(v ?? '').includes(String(t)) || String(t).includes(String(v ?? '') || '\u0000') ? 'ok' : 'bad') : '');
          return h('tr', { class: diff ? 'diff' : '' }, h('td', { class: 'strong' }, label), h('td', { class: ok(x) === 'bad' ? 'cmp-bad' : ok(x) === 'ok' ? 'cmp-ok' : '' }, x || h('span', { class: 'muted' }, '-')), h('td', { class: ok(y) === 'bad' ? 'cmp-bad' : ok(y) === 'ok' ? 'cmp-ok' : '' }, y || h('span', { class: 'muted' }, '-')), truth && h('td', { class: 'mono' }, t || ''));
        })),
      )) }), h('div', { class: 'row mt-3' }, button('Open run A', { href: link('incidents', a) }), button('Open run B', { href: link('incidents', b) })));
    } catch (err) {
      mount(body, errorBox(err, run));
    }
  }
  run();
}
