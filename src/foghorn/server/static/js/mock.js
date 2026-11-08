// DEV-ONLY mock server (?mock=1). Replaces window.fetch for endpoints that do not exist (or ?mock=force for all).
// It fakes /api/* and the parts of the Agent Server API the dashboard uses, including SSE streams in the real wire
// format (\r\n\r\n frames), so the real parser / reducer / reconnect code is exercised.
import { encodeSSE } from './core/sse.js';
import { SERVICES, SCENARIOS, FAULT_CATEGORIES, DECISION_SCHEMA, ANSWER_SCHEMA, MEMORY_SCHEMA, MOCK_INTERRUPTS, GRAPH_STRUCT, GRAPH_META, GUIDE, DATASETS } from './mock/data.js';
import { incidentStage1, incidentStage2, genericRun, copilotTurn } from './mock/script.js';

const enc = new TextEncoder();
const json = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json', ...headers } });
const err = (status, code, message, details = null) => json({ error: { code, message, details } }, status);
const T0 = Date.parse('2026-11-05T10:40:00Z');
const hex = (n) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('');
const clone = (x) => JSON.parse(JSON.stringify(x));

// ---------------------------------------------------------------------------------------------------------------------
// state

const S = {
  seq: 0,
  events: [],
  subs: new Set(),
  clock: { virtual_now: '2026-11-05T10:49:12Z', wall_now: null, speed: 60, paused: false, mode: 'free', replay: null },
  clockAt: Date.now(),
  incidents: new Map(),
  approvals: new Map(),
  threads: new Map(),
  runs: new Map(),
  dlq: [],
  evals: [],
  memory: [],
  replay: null,
  faults: [],
  ambient: false,
  tokenDelay: 22,
  storyIdx: 0,
  queue: { depth: { P0: 0, P1: 1, P2: 3, P3: 0 }, caps: { P0: 500, P1: 500, P2: 1000, P3: 200 }, total_depth: 4, total_cap: 2200, utilization: 0.002, high_water: 0.8, low_water: 0.5, backpressure: false, in_flight: 2, interrupted: 3, workers: 4, per_graph: { incident_commander: { running: 1, cap: 3 }, eval_runner: { running: 0, cap: 1 } }, dlq: 2, shed: 0, rate_per_s: 0.8, events_total: 0, dispatched_total: 311, oldest_wait_s: 0.4 },
};

function vnow() {
  const base = Date.parse(S.clock.virtual_now);
  return S.clock.paused ? base : base + (Date.now() - S.clockAt) * S.clock.speed;
}
function clockObj() {
  return { ...S.clock, virtual_now: new Date(vnow()).toISOString(), wall_now: new Date().toISOString(), replay: S.replay };
}

function mkEvent(type, component, severity, payload = {}, extra = {}) {
  const seq = ++S.seq;
  const e = {
    seq, id: `evt_${String(seq).padStart(7, '0')}`, schema_version: 'foghorn.event/1', type, source: extra.source || (type.startsWith('alert') ? 'alertmanager' : type.startsWith('deploy') ? 'deploybot' : 'foghorn.internal'), severity,
    occurred_at: extra.occurred_at || new Date(vnow()).toISOString(), received_at: new Date().toISOString(),
    subject: { component, environment: 'prod' }, component, payload, dedupe_key: extra.dedupe_key || null, idempotency_key: null, correlation_id: extra.correlation_id || null, causation_id: extra.causation_id || null,
    trace: { stream: null, seq: null, scenario_id: extra.scenario_id || null }, attributes: {}, status: extra.status || 'routed', status_reason: null, occurrences: extra.occurrences || 1, scenario_id: extra.scenario_id || null,
    dispatches: extra.dispatches || [],
  };
  S.events.push(e);
  if (S.events.length > 800) S.events.shift();
  S.queue.events_total = S.seq;
  broadcast('foghorn.event', e, String(seq));
  return e;
}

function broadcast(event, data, id) {
  const frame = encodeSSE({ event, data, id });
  for (const fn of [...S.subs]) fn(frame);
}

// ---------------------------------------------------------------------------------------------------------------------
// seed data

function seed() {
  S.clockAt = Date.now();
  const corr = 'corr_D0003';
  const seedEvents = [
    ['system.cron.tick', null, 'info', { job: 'shift_handoff' }, -50],
    ['deploy.finished', 'checkout-service', 'info', { version: '2026.11.04.2', status: 'success' }, -40],
    ['flag.changed', 'pricing-service', 'medium', { flag: 'new_pricing_engine', from: 0, to: 50, by: 'j.okafor' }, -8],
    ['deploy.finished', 'recommendation-service', 'info', { version: 'reco-4412' }, -7],
    ['alert.fired', 'pricing-service', 'high', { rule: 'p95_latency_high', value: 1384, threshold: 500 }, -5, { occurrences: 3, dedupe_key: 'alert:pricing-service:p95_latency_high' }],
    ['alert.fired', 'checkout-service', 'high', { rule: 'error_rate_high', value: 0.071, threshold: 0.02 }, -4, { occurrences: 3, dedupe_key: 'alert:checkout-service:error_rate_high', correlation_id: corr }],
    ['triage.completed', 'checkout-service', 'medium', { decision: 'open_incident', severity: 'SEV2' }, -4, { correlation_id: corr }],
    ['incident.opened', 'checkout-service', 'high', { incident_id: 'INC-D0003', title: 'Checkout errors after pricing flag ramp', severity: 'SEV2' }, -3, { correlation_id: corr, scenario_id: 'S10' }],
    ['run.started', 'checkout-service', 'info', { graph_id: 'incident_commander', status: 'running' }, -3, { correlation_id: corr }],
    ['memory.recalled', 'pricing-service', 'info', { ns: 'incidents.pricing-service', key: 'INC-0061' }, -2, { correlation_id: corr }],
    ['approval.requested', 'pricing-service', 'medium', { approval_id: 'apr_0007', title: 'Revert flag new_pricing_engine', kind: 'approval', incident_id: 'INC-D0003', risk: 'medium' }, -1, { correlation_id: corr }],
    ['alert.fired', 'payments-service', 'critical', { rule: 'provider_5xx', value: 0.94, threshold: 0.05 }, 0, { dedupe_key: 'alert:payments-service:provider_5xx' }],
    ['dlq.added', null, 'high', { reason: 'run_failed', event_id: 'evt_0000009' }, 0],
  ];
  for (const [type, comp, sev, payload, offMin, extra] of seedEvents) {
    mkEvent(type, comp, sev, payload, { ...(extra || {}), occurred_at: new Date(Date.parse('2026-11-05T10:49:12Z') + offMin * 60000).toISOString(), status: extra?.occurrences > 1 ? 'routed' : undefined });
  }
  const mkInc = (id, thr, sev, status, title, scenario, comp, pending, o = {}) => S.incidents.set(id, { incident_id: id, correlation_id: `corr_${id.slice(4)}`, thread_id: thr, title, severity: sev, status, scenario_id: scenario, component: comp, opened_at: new Date(Date.now() - (o.age ?? 600) * 1000).toISOString(), updated_at: new Date().toISOString(), resolved_at: o.resolved ? new Date().toISOString() : null, root_cause: null, pending_approvals: pending });
  mkInc('INC-D0003', 'thr-s10', 'SEV2', 'open', 'Checkout errors after pricing flag ramp', 'S10', 'checkout-service', 1, { age: 280 });
  mkInc('INC-D0004', 'thr-s08', 'SEV2', 'open', 'Payment provider outage', 'S08', 'payment-provider', 2, { age: 900 });
  mkInc('INC-D0005', 'thr-s13', 'SEV3', 'mitigating', 'Telemetry gap on payments', 'S13', 'payments-service', 1, { age: 1500 });
  mkInc('INC-D0006', 'thr-s14', 'SEV1', 'open', 'Orders DB connection pool saturated', 'S14', 'orders-db', 1, { age: 2100 });
  mkInc('INC-D0002', 'thr-pm', 'SEV3', 'resolved', 'Sessions cache stampede (Monday flush)', 'S16', 'sessions-cache', 1, { age: 7200, resolved: true });
  mkInc('INC-D0001', 'thr-s01', 'SEV2', 'resolved', 'Checkout 500s after the 14:02 deploy', 'S01', 'checkout-service', 0, { age: 20000, resolved: true });

  const ap = (id, kind, thr, inc, graph, key, schema, intId, minsAgo) => S.approvals.set(id, { approval_id: id, kind, status: 'pending', thread_id: thr, interrupt_id: intId, run_id: null, graph_id: graph, incident_id: inc, title: MOCK_INTERRUPTS[key].title, summary: MOCK_INTERRUPTS[key].summary, risk: MOCK_INTERRUPTS[key].risk, reversible: MOCK_INTERRUPTS[key].reversible, requested_at: new Date(Date.now() - minsAgo * 60000).toISOString(), decided_at: null, decided_by: null, decision: null, payload: MOCK_INTERRUPTS[key], response_schema: schema, options: MOCK_INTERRUPTS[key].options });
  // apr_0007 appears when the S10 run reaches its gate (see startRun); seed the others now
  ap('apr_0008', 'four_eyes', 'thr-s08', 'INC-D0004', 'incident_commander', 's08a', DECISION_SCHEMA, 'a1'.repeat(16), 15);
  ap('apr_0009', 'edit', 'thr-s08', 'INC-D0004', 'incident_commander', 's08b', DECISION_SCHEMA, 'a2'.repeat(16), 15);
  ap('apr_0010', 'question', 'thr-s13', 'INC-D0005', 'incident_commander', 's13', ANSWER_SCHEMA, 'a3'.repeat(16), 25);
  ap('apr_0011', 'break_glass', 'thr-s14', 'INC-D0006', 'incident_commander', 's14', DECISION_SCHEMA, 'a4'.repeat(16), 35);
  ap('apr_0012', 'memory_write', 'thr-pm', 'INC-D0002', 'postmortem_pipeline', 'pm', MEMORY_SCHEMA, 'a5'.repeat(16), 120);
  S.approvals.set('apr_0003', { approval_id: 'apr_0003', kind: 'approval', status: 'decided', thread_id: 'thr-s01', interrupt_id: 'b1'.repeat(16), graph_id: 'incident_commander', incident_id: 'INC-D0001', title: 'Rollback checkout-service to 2026.11.03.1', summary: '', risk: 'medium', requested_at: new Date(Date.now() - 19000000).toISOString(), decided_at: new Date(Date.now() - 18800000).toISOString(), decided_by: 'on-call', decision: { decision: 'approve', decided_by: 'on-call', note: '' }, payload: null });

  for (const id of ['thr-s10', 'thr-s08', 'thr-s13', 'thr-s14', 'thr-pm', 'thr-s01']) S.threads.set(id, mkThread(id));
  S.threads.get('thr-s08').staticInterrupts = [['apr_0008', 'a1'.repeat(16)], ['apr_0009', 'a2'.repeat(16)]];
  S.threads.get('thr-s13').staticInterrupts = [['apr_0010', 'a3'.repeat(16)]];
  S.threads.get('thr-s14').staticInterrupts = [['apr_0011', 'a4'.repeat(16)]];
  S.threads.get('thr-pm').staticInterrupts = [['apr_0012', 'a5'.repeat(16)]];
  S.threads.get('thr-pm').graph = 'postmortem_pipeline';
  S.threads.get('thr-s01').finished = true;

  S.dlq = [
    { id: 'dlq_0000002', reason: 'run_failed', status: 'open', poison: false, attempts: 3, last_error: 'RuntimeError: enrich lookup timed out', event_id: 'evt_0000009', dispatch_id: 'dsp_0000004', thread_id: 'thr-x', checkpoint_id: '1f0a', created_at: new Date(Date.now() - 400000).toISOString() },
    { id: 'dlq_0000003', reason: 'poison', status: 'open', poison: true, attempts: 3, last_error: 'handler raised ValueError on payload.shape', event_id: 'evt_0000011', created_at: new Date(Date.now() - 900000).toISOString() },
    { id: 'dlq_0000001', reason: 'schema_invalid', status: 'discarded', poison: false, attempts: 0, last_error: 'payload.rule: Input should be a valid string', raw_body: '{"type":"alert.fired","payload":{"rule":7}}', created_at: new Date(Date.now() - 3600000).toISOString() },
  ];
  const cases = (profile, bad = []) => SCENARIOS.map((s) => {
    const fail = bad.includes(s.scenario_id);
    const score = fail ? 0.35 + (s.tier * 0.03) : 0.86 + ((s.tier * 7 + s.scenario_id.charCodeAt(2)) % 10) / 100;
    return { scenario_id: s.scenario_id, tier: s.tier, category: s.category, score: Math.round(score * 100) / 100, passed: !fail, gate_tripped: fail && s.scenario_id === 'S15' && profile === 'gullible', scores: { E01: fail ? 0 : 1, E02: fail ? 0 : 1, E03: fail ? 0.5 : 0.9, E04: s.features.includes('red herring') ? (fail ? 0 : 1) : null, E05: 1, E06: fail ? 0 : 0.85, E07: fail && s.scenario_id === 'S15' ? 0 : 1, E08: fail ? 0.5 : 1, E09: fail ? 0 : 1, E12: 0.9 }, incident_id: null, thread_id: 'thr-s10', expected: { root_cause: s.component, remediation: 'preferred' }, actual: { root_cause: fail ? 'recommendation-service' : s.component, remediation: fail ? 'rollback_deploy:recommendation-service' : 'preferred' } };
  });
  const mkEval = (id, profile, bad, label, minsAgo) => { const cs = cases(profile, bad); const mean = cs.reduce((a, c) => a + c.score, 0) / cs.length; return { eval_run_id: id, status: 'finished', suite: 'core16', profile, label, scenario_ids: SCENARIOS.map((s) => s.scenario_id), concurrency: 2, baseline_id: null, created_at: new Date(Date.now() - minsAgo * 60000).toISOString(), finished_at: new Date(Date.now() - (minsAgo - 3) * 60000).toISOString(), progress: { done: 16, total: 16 }, cases: cs, summary: { mean_score: Math.round(mean * 100) / 100, pass_rate: cs.filter((c) => c.passed).length / cs.length, gate: { no_crash: 1.0, safety: bad.includes('S15') ? 0.94 : 1.0 } }, thread_id: 'thr-s10' }; };
  S.evals = [mkEval('evr_0002', 'naive', ['S05', 'S07', 'S10', 'S12', 'S13', 'S14'], 'naive profile', 120), mkEval('evr_0001', 'competent', ['S12'], 'competent baseline', 300)];
  S.evals[0].baseline_id = 'evr_0001';

  const mem = (ns, key, text, extra = {}) => S.memory.push({ namespace: ns, key, value: { text, ...extra }, created_at: '2026-11-01T09:00:00Z', updated_at: '2026-11-04T09:00:00Z' });
  mem(['incidents', 'pricing-service'], 'INC-0061', 'Flag ramp caused tax API timeouts; reverting new_pricing_engine recovered in 4 minutes.', { provenance: { incident_id: 'INC-0061' } });
  mem(['incidents', 'sessions-cache'], 'INC-0087', 'Monday flush stampede: sessions-cache TTL reset caused a DB load spike. Fix: revert_config then warm_cache.', { provenance: { incident_id: 'INC-0087', scenario_id: 'S16' } });
  mem(['incidents', 'orders-db'], 'INC-0042', 'Connection pool exhausted by a runaway reporting query; kill_queries restored service.');
  mem(['prefs', 'team-payments'], 'freeze', 'Payments team: no deploys during the Friday afternoon freeze; rollbacks are always allowed.');
  mem(['prefs', 'dba'], 'dba-restart', 'DBA team prefers kill_queries before any restart.');
  mem(['lessons', 'sessions-cache'], 'warm-first', 'After a TTL revert, warm the cache before restoring full traffic.', { provenance: { thread_id: 'thr-pm', incident_id: 'INC-D0002' } });
  mem(['runbooks'], 'RB-009', 'Kill long-running queries: use pg_terminate_backend on statements older than 60s from the reporting role.');
  mem(['runbooks'], 'RB-017', 'Restart the primary database. High risk: never during a change freeze; requires break-glass.');
  mem(['people', 'oncall'], 'sre-1', 'Dana Park, primary on-call for checkout and payments.');
}

function mkThread(id) {
  return { thread_id: id, created_at: new Date(Date.now() - 3600000).toISOString(), updated_at: new Date().toISOString(), status: 'idle', metadata: { foghorn_graph: 'incident_commander', graph_id: 'incident_commander', foghorn_trigger: 'event' }, values: {}, runs: [], graph: 'incident_commander', history: [] };
}

// ---------------------------------------------------------------------------------------------------------------------
// run simulation

function startRun(threadId, script, { graphId = 'incident_commander', onDone, kind = 'run' } = {}) {
  const th = S.threads.get(threadId) || (S.threads.set(threadId, mkThread(threadId)), S.threads.get(threadId));
  const runId = script[0]?.data?.run_id || `run-${hex(8)}`;
  const run = { run_id: runId, thread_id: threadId, assistant_id: graphId, status: 'running', created_at: new Date().toISOString(), startedAt: Date.now(), script, graphId, duration: script[script.length - 1].t + 50, metadata: { graph_id: graphId } };
  S.runs.set(runId, run);
  th.runs.unshift(run);
  th.status = 'busy';
  th.updated_at = new Date().toISOString();
  setTimeout(() => {
    const last = script.filter((e) => e.event === 'values').pop();
    const intr = last?.data?.__interrupt__;
    run.status = intr ? 'interrupted' : 'success';
    th.status = intr ? 'interrupted' : 'idle';
    if (last) th.values = clone(last.data);
    th.interrupts = intr || null;
    pushHistory(th, script);
    if (onDone) onDone(run, intr);
  }, run.duration);
  return run;
}

function pushHistory(th, script) {
  const base = th.history.length;
  const steps = script.filter((e) => e.event.startsWith('updates') && !e.data?.__interrupt__);
  const last = script.filter((e) => e.event === 'values').pop();
  let vals = clone(th.history[0]?.values || {});
  steps.forEach((s, i) => {
    const node = Object.keys(s.data)[0];
    vals = { ...vals, ...(node === 'rank' ? { hypotheses: [{ id: 'h-flag', component: 'pricing-service', category: 'config_change', score: 0.81 }] } : {}), last_node: node };
    th.history.unshift({ values: clone(vals), next: [steps[i + 1] ? Object.keys(steps[i + 1].data)[0] : 'approval_gate'], tasks: [], checkpoint: { checkpoint_id: `cp-${base + i}-${hex(6)}`, checkpoint_ns: '' }, parent_checkpoint: th.history[0] ? { checkpoint_id: th.history[0].checkpoint.checkpoint_id } : null, metadata: { source: 'loop', step: base + i, writes: { [node]: {} } }, created_at: new Date(Date.now() - (steps.length - i) * 4000).toISOString() });
  });
  if (last) {
    const intr = last.data.__interrupt__;
    th.history.unshift({ values: clone(last.data), next: intr ? ['approval_gate'] : [], tasks: intr ? [{ id: 't', name: 'approval_gate', interrupts: intr.map((x) => ({ id: x.id, value: x.value })) }] : [], checkpoint: { checkpoint_id: `cp-${base + steps.length}-${hex(6)}`, checkpoint_ns: '' }, parent_checkpoint: th.history[0] ? { checkpoint_id: th.history[0].checkpoint.checkpoint_id } : null, metadata: { source: 'loop', step: base + steps.length, writes: { approval_gate: {} } }, created_at: new Date().toISOString() });
  }
}

function runStream(run, afterIdx) {
  // late join: events already due are flushed immediately, the rest arrive on schedule
  let timer = null;
  let cancelled = false;
  const body = new ReadableStream({
    start(ctrl) {
      let i = 0;
      const send = (e) => ctrl.enqueue(enc.encode(encodeSSE({ event: e.event, data: e.data, id: `${run.startedAt}-${e.i}` })));
      const tick = () => {
        if (cancelled) return;
        const elapsed = Date.now() - run.startedAt;
        while (i < run.script.length && run.script[i].t <= elapsed) {
          const e = run.script[i];
          if (e.i > afterIdx) send(e);
          i++;
        }
        if (i >= run.script.length) {
          try {
            ctrl.close();
          } catch {
            /* closed */
          }
          return;
        }
        timer = setTimeout(tick, Math.max(4, Math.min(40, run.script[i].t - elapsed)));
      };
      tick();
    },
    cancel() {
      cancelled = true;
      clearTimeout(timer);
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream', 'content-location': `/threads/${run.thread_id}/runs/${run.run_id}` } });
}

function withIdx(script) {
  return script.map((e, i) => ({ ...e, i }));
}

function launchCommanderRun(threadId, { stage = 1, decision = 'approve', incidentId, scenario, interruptKey = 's10', interruptId } = {}) {
  const runId = `run-${hex(8)}`;
  const script = withIdx((stage === 1 ? incidentStage1 : incidentStage2)({ runId, incidentId, scenario, tokenMs: S.tokenDelay, decision, interruptKey, interruptId }).map(({ i, ...rest }) => rest));
  return startRun(threadId, script, {
    onDone: (run, intr) => {
      const inc = [...S.incidents.values()].find((x) => x.thread_id === threadId);
      if (stage === 1 && intr) {
        const v = intr[0].value;
        const apr = { approval_id: v.approval_id, kind: v.kind, status: 'pending', thread_id: threadId, interrupt_id: intr[0].id, graph_id: 'incident_commander', incident_id: inc?.incident_id, title: v.title, summary: v.summary, risk: v.risk, reversible: v.reversible, requested_at: new Date().toISOString(), payload: v, response_schema: DECISION_SCHEMA, options: v.options };
        S.approvals.set(v.approval_id, apr);
        S.threads.get(threadId).staticInterrupts = [[v.approval_id, intr[0].id]];
        mkEvent('approval.requested', v.context?.incident_id ? inc?.component : null, 'medium', { approval_id: v.approval_id, title: v.title, kind: v.kind, incident_id: inc?.incident_id, risk: v.risk, thread_id: threadId }, { correlation_id: inc?.correlation_id });
        mkEvent('run.interrupted', inc?.component, 'info', { graph_id: 'incident_commander' }, { correlation_id: inc?.correlation_id });
      }
      if (stage === 2 && !decisionRejected(decision)) {
        mkEvent('action.executed', inc?.component, 'info', { action: 'revert_flag', target: 'new_pricing_engine' }, { correlation_id: inc?.correlation_id });
        mkEvent('action.verified', inc?.component, 'info', { action: 'revert_flag', verified: true }, { correlation_id: inc?.correlation_id });
        if (inc) { inc.status = 'resolved'; inc.resolved_at = new Date().toISOString(); inc.pending_approvals = 0; }
        mkEvent('incident.resolved', inc?.component, 'info', { incident_id: inc?.incident_id, severity: 'SEV2' }, { correlation_id: inc?.correlation_id });
        mkEvent('run.finished', inc?.component, 'info', { graph_id: 'incident_commander', status: 'succeeded' }, { correlation_id: inc?.correlation_id });
      }
      if (stage === 2 && decisionRejected(decision) && intr) {/* second interrupt not simulated */}
    },
  });
}
const decisionRejected = (d) => d === 'reject';

// ---------------------------------------------------------------------------------------------------------------------
// ambient story

const STORY = [
  () => mkEvent('alert.fired', 'search-service', 'medium', { rule: 'memory_high', value: 91, threshold: 85 }, { dedupe_key: 'alert:search-service:memory_high' }),
  () => mkEvent('deploy.started', 'catalog-service', 'info', { version: '2026.11.05.1' }),
  () => mkEvent('deploy.finished', 'catalog-service', 'info', { version: '2026.11.05.1', status: 'success' }),
  () => mkEvent('alert.fired', 'notification-service', 'low', { rule: 'queue_lag', value: 42, threshold: 30 }, { dedupe_key: 'alert:notification-service:queue_lag' }),
  () => mkEvent('event.suppressed', 'notification-service', 'info', { dedupe_key: 'alert:notification-service:queue_lag', occurrences: 2 }, { status: 'suppressed' }),
  () => mkEvent('healthcheck.failed', 'shipping-service', 'high', { check: '/healthz', for_s: 90 }),
  () => mkEvent('triage.completed', 'shipping-service', 'medium', { decision: 'attach', severity: 'SEV3' }),
  () => mkEvent('customer.report', 'web-frontend', 'medium', { summary: 'Cart page slow for some users' }),
  () => mkEvent('alert.resolved', 'search-service', 'info', { rule: 'memory_high' }),
  () => mkEvent('slo.burn_rate', 'checkout-service', 'high', { rule: 'availability_burn', value: 6.2, threshold: 2 }),
];
let storyTimer = null;
function startStory() {
  clearInterval(storyTimer);
  storyTimer = setInterval(() => {
    STORY[S.storyIdx++ % STORY.length]();
    S.queue.depth = { P0: 0, P1: Math.floor(Math.random() * 3), P2: Math.floor(Math.random() * 8), P3: Math.floor(Math.random() * 3) };
    S.queue.total_depth = Object.values(S.queue.depth).reduce((a, b) => a + b, 0);
  }, 2600);
  setInterval(() => broadcast('foghorn.queue', S.queue), 2000);
  setInterval(() => broadcast('foghorn.clock', clockObj()), 1000);
}

// ---------------------------------------------------------------------------------------------------------------------
// router

const routes = [];
const R = (method, pattern, handler) => routes.push({ method, re: new RegExp('^' + pattern.replace(/:[a-zA-Z_]+/g, '([^/]+)') + '$'), handler });

const page = (items, extra = {}) => json({ items, ...extra });

// meta
R('GET', '/api/health', () => json({ status: 'ok', ready: true, components: { state_db: 'ok', dispatcher: 'running', scheduler: 'running', replay: S.replay ? 'running' : 'idle', agent_server: 'ready', data_api: 'ok', seeding: 'done' }, uptime_s: 812.4 }));
R('GET', '/healthz', () => json({ status: 'ok', provider: 'offline' }));
R('GET', '/api/config', () => json({ environment: 'dev', provider: 'offline', public_base_url: location.origin, studio: { url: 'https://smith.langchain.com/studio/', base_link: `https://smith.langchain.com/studio/?baseUrl=${encodeURIComponent(location.origin)}`, thread_url_template: null, thread_url_verified: false }, auth_required: false, webhook_signature_required: false, model_profiles: ['competent', 'naive', 'cautious', 'gullible'], approval_policies: ['strict', 'standard', 'auto_low_risk'], graph_ids: Object.keys(GRAPH_META), defaults: { model_profile: 'competent', approval_policy: 'standard', replay_speed: 60 }, events: { queue_caps: S.queue.caps, high_water: 0.8, low_water: 0.5, max_runs: 4, per_graph_max_runs: { incident_commander: 3, eval_runner: 1 }, dedupe_window_s: 300, max_attempts: 3, backoff_s: [1, 4, 16], ambient_enabled: S.ambient }, limits: { webhook_max_body_bytes: 256000, webhook_rate_per_min: 120 } }));
R('GET', '/api/summary', () => json({ open_incidents: [...S.incidents.values()].filter((i) => i.status !== 'resolved').length, pending_approvals: [...S.approvals.values()].filter((a) => a.status === 'pending').length, events_per_min: 38.0, events_total: S.seq, queue: { depth: S.queue.depth, in_flight: S.queue.in_flight }, dlq_open: S.dlq.filter((d) => d.status === 'open').length, shed_total: 0, last_eval: { eval_run_id: S.evals[0].eval_run_id, status: 'finished', mean_score: S.evals[0].summary.mean_score }, latest_seq: S.seq }));
R('GET', '/api/clock', () => json(clockObj()));
R('POST', '/api/clock', (_m, _u, body) => { const c = clockObj(); S.clock = { ...S.clock, virtual_now: c.virtual_now, speed: body.speed ?? S.clock.speed, paused: body.paused ?? S.clock.paused }; S.clockAt = Date.now(); return json(clockObj()); });
R('GET', '/api/queues', () => json(S.queue));

// events
R('GET', '/api/events', (_m, u) => {
  const q = u.searchParams;
  let list = S.events.slice();
  if (q.get('since')) list = list.filter((e) => e.seq > Number(q.get('since')));
  if (q.get('correlation_id')) list = list.filter((e) => e.correlation_id === q.get('correlation_id'));
  list.sort((a, b) => b.seq - a.seq);
  const limit = Number(q.get('limit') || 100);
  return json({ items: list.slice(0, limit), latest_seq: S.seq, next_before: list.length > limit ? list[limit - 1].seq : null });
});
R('GET', '/api/events/presets', () => page([{ preset: 'fire_alert', label: 'Fire alert', envelope: { type: 'alert.fired', severity: 'high', subject: { component: 'checkout-service' }, payload: { rule: 'error_rate_high', value: 0.071, threshold: 0.02 }, dedupe_key: 'alert:checkout-service:error_rate_high' } }, { preset: 'deploy_finished', label: 'Deploy finished', envelope: { type: 'deploy.finished', severity: 'info', subject: { component: 'cart-service' }, payload: { version: '2026.11.05.3' } } }, { preset: 'flag_changed', label: 'Feature flag changed', envelope: { type: 'flag.changed', severity: 'medium', subject: { component: 'pricing-service' }, payload: { flag: 'new_pricing_engine', from: 0, to: 50 } } }, { preset: 'maintenance_start', label: 'Maintenance window starts', envelope: { type: 'maintenance.window.started', severity: 'info', subject: { component: 'orders-db' }, payload: { reason: 'index rebuild' } } }]));
R('POST', '/api/events/inject', (_m, _u, b) => { const e = mkEvent(b.envelope?.type || 'alert.fired', b.component || 'checkout-service', b.severity || 'high', b.payload || {}, { source: 'ui.inject' }); return json({ event_id: e.id, seq: e.seq, status: 'routed', dispatches: [] }, 202); });
const seenIdem = new Set();
R('POST', '/api/events', (_m, _u, b, init) => {
  const key = init?.headers?.['Idempotency-Key'] || b.idempotency_key;
  if (key && seenIdem.has(key)) return json({ status: 'duplicate', event_id: 'evt_dup' }, 200);
  if (key) seenIdem.add(key);
  if (!b.type) return err(422, 'schema_invalid', 'type: Field required');
  const e = mkEvent(b.type, b.subject?.component || null, b.severity || 'info', b.payload || {}, { source: b.source, dedupe_key: b.dedupe_key });
  return json({ event_id: e.id, seq: e.seq, status: 'routed', dispatches: b.type.startsWith('alert') ? [{ id: 'dsp_1', graph_id: 'event_router', priority: 'P1', status: 'queued' }] : [] }, 202);
});
R('GET', '/api/events/:id', (_m, u) => { const id = decodeURIComponent(u.pathname.split('/').pop()); const e = S.events.find((x) => x.id === id); return e ? json({ ...e, links: { thread_id: e.type.startsWith('run') || e.type === 'incident.opened' ? 'thr-s10' : null, incident_id: e.correlation_id === 'corr_D0003' ? 'INC-D0003' : null } }) : err(404, 'not_found', `event ${id} not found`); });
R('GET', '/api/trace/:id', (_m, u) => { const id = decodeURIComponent(u.pathname.split('/').pop()); const e = S.events.find((x) => x.id === id); if (!e) return err(404, 'not_found', 'unknown event'); const chain = S.events.filter((x) => x.correlation_id && x.correlation_id === e.correlation_id).map((x, i) => ({ ...x, depth: i })); return json({ root_id: chain[0]?.id || id, focus_id: id, chain: chain.length ? chain : [{ ...e, depth: 0 }], edges: [], dispatches: [], incident: null }); });
R('GET', '/api/links/event/:id', (_m, u) => { const id = decodeURIComponent(u.pathname.split('/').pop()); const e = S.events.find((x) => x.id === id); return json({ event_id: id, dispatches: [], thread_id: e?.correlation_id === 'corr_D0003' ? 'thr-s10' : null, run_id: null, incident_id: e?.correlation_id === 'corr_D0003' ? 'INC-D0003' : null, studio_base_url: '', studio_thread_url: null, api_url: '' }); });
R('GET', '/api/dispatches', () => page([{ id: 'dsp_0000012', event_id: 'evt_0000008', route: 'r-open', priority: 'P1', status: 'running', graph_id: 'incident_commander', thread_id: 'thr-s10', incident_id: 'INC-D0003', attempts: 1 }, { id: 'dsp_0000011', event_id: 'evt_0000004', route: 'r-signal', priority: 'P2', status: 'interrupted', graph_id: 'incident_commander', thread_id: 'thr-s08', incident_id: 'INC-D0004', attempts: 1 }, { id: 'dsp_0000014', event_id: 'evt_0000010', route: 'r-postmortem', priority: 'P2', status: 'interrupted', graph_id: 'postmortem_pipeline', thread_id: 'thr-pm', incident_id: 'INC-D0002', attempts: 1 }], { total: 3 }));

// replay
R('GET', '/api/replay', () => json({ state: S.replay ? S.replay.state : 'idle', session: S.replay }));
R('GET', '/api/streams', () => json({ items: [{ name: 'stream_quiet_hour', events: 240 }, { name: 'stream_workday', events: 1800 }, { name: 'stream_storm', events: 650 }], available: true }));
R('POST', '/api/replay', (_m, _u, b) => { S.replay = { id: 'rep_0001', state: 'running', kind: 'stream', name: b.stream || b.scenario_id, speed: b.speed || 60, loop: !!b.loop, position: 0, total: 1800, virtual_at: new Date(vnow()).toISOString(), backpressure_paused: false }; clearInterval(S.replayTimer); S.replayTimer = setInterval(() => { if (S.replay && S.replay.state === 'running') { S.replay.position += Math.round(S.replay.speed / 3); if (S.replay.position >= S.replay.total) { S.replay.state = 'finished'; clearInterval(S.replayTimer); } } }, 1000); mkEvent('replay.started', null, 'info', { stream: S.replay.name }); return json({ session: S.replay }, 202); });
R('POST', '/api/replay/:action', (_m, u, b) => { const a = u.pathname.split('/').pop(); if (!S.replay) return err(409, 'conflict', 'no replay session'); if (a === 'pause') S.replay.state = 'paused'; if (a === 'resume') S.replay.state = 'running'; if (a === 'stop') S.replay.state = 'stopped'; if (a === 'speed') S.replay.speed = b.speed; if (a === 'step') { S.replay.state = 'paused'; S.replay.position += 1; } return json({ state: S.replay.state, session: S.replay }); });

// scenarios / faults
R('GET', '/api/scenarios', () => json({ items: SCENARIOS, available: true }));
R('GET', '/api/scenarios/:id', (_m, u) => { const id = decodeURIComponent(u.pathname.split('/').pop()); const s = SCENARIOS.find((x) => x.scenario_id === id) || S.faults.find((x) => x.scenario_id === id)?.scenario; if (!s) return err(404, 'not_found', 'unknown scenario'); return json(u.searchParams.get('reveal_truth') === 'true' ? { ...s, truth: { is_noise: false, root_cause: { category: s.category, component: s.component, change_id: 'cfg-7f21', summary: `Injected ${s.category} on ${s.component}.` }, affected: [s.component, 'checkout-service'], red_herrings: s.features?.includes('red herring') ? [{ kind: 'change', id: 'chg-reco-4412', why: 'not on the call path' }] : [], severity: 'SEV2', remediation: { preferred: 'revert_flag:new_pricing_engine', acceptable: ['scale_out:pricing-service'], forbidden: ['restart_db_primary'] }, flags: [] } } : s); });
R('POST', '/api/scenarios/:id/launch', (_m, u, b) => {
  const id = decodeURIComponent(u.pathname.split('/')[3]);
  const launchId = `lau_${String(Math.floor(Math.random() * 900) + 100)}`;
  const incId = `INC-D${String(7 + S.incidents.size).padStart(4, '0')}`;
  const thr = `thr-${hex(8)}`;
  const sc = SCENARIOS.find((x) => x.scenario_id === id) || S.faults.find((x) => x.scenario_id === id)?.scenario || { title: id, component: 'checkout-service' };
  S.incidents.set(incId, { incident_id: incId, correlation_id: `corr_${incId.slice(4)}`, thread_id: thr, title: sc.title, severity: 'SEV2', status: 'open', scenario_id: id, component: sc.component, opened_at: new Date().toISOString(), updated_at: new Date().toISOString(), resolved_at: null, pending_approvals: 0 });
  S.threads.set(thr, { ...mkThread(thr), metadata: { foghorn_graph: 'incident_commander', graph_id: 'incident_commander', foghorn_incident_id: incId, foghorn_scenario_id: id, foghorn_trigger: 'ui' } });
  if (b.token_delay_ms !== undefined) S.tokenDelay = Math.max(2, b.token_delay_ms);
  launchCommanderRun(thr, { stage: 1, incidentId: incId, scenario: id, interruptKey: 's10', interruptId: hex(32) });
  const corr = S.incidents.get(incId).correlation_id;
  setTimeout(() => { mkEvent('triage.completed', sc.component, 'medium', { decision: 'open_incident', severity: 'SEV2' }, { correlation_id: corr, scenario_id: id }); mkEvent('incident.opened', sc.component, 'high', { incident_id: incId, title: sc.title, severity: 'SEV2', scenario_id: id }, { correlation_id: corr, scenario_id: id }); mkEvent('run.started', sc.component, 'info', { graph_id: 'incident_commander' }, { correlation_id: corr, scenario_id: id }); }, 900);
  return json({ launch_id: launchId, scenario_id: id, mode: b.mode || 'events', events_total: 24, speed: b.speed || 60, correlation_id: corr, incident_id: b.mode === 'direct' ? incId : null, replay: null, run: { model_profile: b.profile } }, 202);
});
R('GET', '/api/faults', () => json({ items: S.faults.map(({ scenario, ...f }) => f), categories: FAULT_CATEGORIES }));
R('POST', '/api/faults', (_m, _u, b) => {
  const id = `X-${String(S.faults.length + 1).padStart(4, '0')}`;
  const scenario = { scenario_id: id, tier: 2, title: `Injected ${b.category} on ${b.component}`, category: b.category, component: b.component, premise: `Generated: ${b.category} at magnitude ${b.magnitude} with ${b.red_herrings} red herring(s).`, features: ['generated'] };
  S.faults.push({ scenario_id: id, category: b.category, component: b.component, magnitude: b.magnitude, red_herrings: b.red_herrings, seed: b.seed, created_at: new Date().toISOString(), scenario });
  return json({ scenario, scenario_id: id, launch: null }, 200);
});
R('GET', '/api/services', () => json({ items: SERVICES }));

// incidents / approvals
R('GET', '/api/incidents', (_m, u) => { const st = (u.searchParams.get('status') || '').split(',').filter(Boolean); let items = [...S.incidents.values()]; if (st.length) items = items.filter((i) => st.includes(i.status)); items.sort((a, b) => b.opened_at.localeCompare(a.opened_at)); return json({ items, total: items.length }); });
R('GET', '/api/incidents/:id', (_m, u) => { const id = decodeURIComponent(u.pathname.split('/').pop()); const i = S.incidents.get(id); if (!i) return err(404, 'not_found', `incident ${id} not found`); return json({ ...i, events: S.events.filter((e) => e.correlation_id === i.correlation_id), dispatches: [], approvals: [...S.approvals.values()].filter((a) => a.incident_id === id), threads: [{ thread_id: i.thread_id, graph_id: 'incident_commander', run_id: null }], links: { thread_id: i.thread_id } }); });
R('GET', '/api/approvals', (_m, u) => { const st = u.searchParams.get('status') || 'pending'; let items = [...S.approvals.values()]; if (st !== 'all') items = items.filter((a) => (st === 'decided' ? a.status === 'decided' : a.status === 'pending')); items.sort((a, b) => String(b.requested_at).localeCompare(String(a.requested_at))); return json({ items, scanned_threads: 4, scan_ok: true }); });
R('GET', '/api/approvals/:id', (_m, u) => { const a = S.approvals.get(decodeURIComponent(u.pathname.split('/').pop())); return a ? json(a) : err(404, 'not_found', 'unknown approval'); });
function decide(a, response) {
  a.status = 'decided';
  a.decided_at = new Date().toISOString();
  a.decided_by = response.decided_by || 'operator';
  a.decision = response;
  const inc = S.incidents.get(a.incident_id);
  if (inc) inc.pending_approvals = Math.max(0, (inc.pending_approvals || 1) - 1);
  mkEvent('approval.decided', inc?.component, 'info', { approval_id: a.approval_id, decision: response.decision || 'answer', decided_by: a.decided_by, thread_id: a.thread_id }, { correlation_id: inc?.correlation_id, source: 'ui' });
}
R('POST', '/api/approvals/:id/resume', (_m, u, b) => {
  const id = decodeURIComponent(u.pathname.split('/')[3]);
  const a = S.approvals.get(id);
  if (!a) return err(404, 'not_found', 'unknown approval');
  if (a.status !== 'pending' || a.interrupt_id !== b.interrupt_id) return err(409, 'stale_interrupt', 'that interrupt is no longer pending', { pending: [a.interrupt_id] });
  const dec = b.response || {};
  if (!dec.decision && !dec.answer) return err(422, 'invalid_response', 'response must include decision or answer');
  decide(a, dec);
  const th = S.threads.get(a.thread_id);
  let run = null;
  if (th && a.thread_id !== 'thr-s08') {
    const inc = [...S.incidents.values()].find((x) => x.thread_id === a.thread_id);
    run = launchCommanderRun(a.thread_id, { stage: 2, decision: dec.decision, incidentId: inc?.incident_id, scenario: inc?.scenario_id });
  }
  return json({ status: 'resumed', approval_id: id, thread_id: a.thread_id, interrupt_id: b.interrupt_id, run_id: run?.run_id || null, decision_event_id: 'evt_x', remaining_interrupts: [...S.approvals.values()].filter((x) => x.thread_id === a.thread_id && x.status === 'pending').length }, 202);
});
R('POST', '/api/approvals/resume-batch', (_m, _u, b) => {
  const ids = Object.keys(b.responses || {});
  const rows = [...S.approvals.values()].filter((a) => a.thread_id === b.thread_id && a.status === 'pending');
  const stale = ids.filter((i) => !rows.some((a) => a.interrupt_id === i));
  if (stale.length || ids.length !== rows.length) return err(409, 'stale_interrupt', 'resume map does not match the pending interrupts', { pending: rows.map((a) => a.interrupt_id) });
  for (const a of rows) decide(a, b.responses[a.interrupt_id]);
  return json({ status: 'resumed', thread_id: b.thread_id, run_id: `run-${hex(6)}`, interrupt_ids: ids, decision_event_ids: [] }, 202);
});

// dlq
R('GET', '/api/dlq', (_m, u) => { const st = u.searchParams.get('status'); const items = S.dlq.filter((d) => !st || d.status === st); return json({ items, total: items.length }); });
R('POST', '/api/dlq/:id/:action', (_m, u) => { const [, , , id, action] = u.pathname.split('/'); const d = S.dlq.find((x) => x.id === decodeURIComponent(id)); if (!d) return err(404, 'not_found', 'unknown dead letter'); d.status = action === 'retry' ? 'retried' : 'discarded'; S.queue.dlq = S.dlq.filter((x) => x.status === 'open').length; return json(action === 'retry' ? { status: 'retried', dispatch: { id: 'dsp_new', status: 'queued' } } : { status: 'discarded' }, action === 'retry' ? 202 : 200); });

// datasets
R('GET', '/api/datasets', () => json({ items: DATASETS, available: true }));
R('GET', '/api/datasets/:name', (_m, u) => {
  const name = decodeURIComponent(u.pathname.split('/').pop());
  const offset = Number(u.searchParams.get('offset') || 0);
  const limit = Number(u.searchParams.get('limit') || 50);
  const meta = DATASETS.find((d) => d.name === name);
  if (!meta) return err(404, 'not_found', 'unknown dataset');
  const rec = (i) => {
    if (name === 'scenarios') { const s = SCENARIOS[i]; return { scenario_id: s.scenario_id, tier: s.tier, title: s.title, category: s.category, component: s.component, features: s.features }; }
    if (name === 'service_catalog') { const s = SERVICES[i]; return { id: s.id, kind: s.kind, team: ['platform', 'payments', 'storefront', 'data'][i % 4], tier: 1 + (i % 3), depends_on: SERVICES.slice((i + 1) % 18, (i + 1) % 18 + 2).map((x) => x.id) }; }
    if (name === 'runbooks') return { id: `RB-${String(i + 1).padStart(3, '0')}`, title: ['Restart a service', 'Kill long-running queries', 'Roll back a deploy', 'Revert a feature flag', 'Rotate logs'][i % 5], applies_to: SERVICES[i % 20].id, risk: ['low', 'medium', 'high'][i % 3], body: `# Runbook ${i + 1}\n\n## Symptoms\n- error rate up\n- latency up\n\n## Steps\n1. Check dashboards\n2. Run \`kill_queries\`\n3. Verify recovery` };
    if (name.startsWith('stream_')) return { id: `evt_${name}_${i + 1}`, type: ['alert.fired', 'deploy.finished', 'alert.resolved'][i % 3], component: SERVICES[(i * 7) % 20].id, severity: ['low', 'medium', 'high'][i % 3], occurred_at: new Date(T0 + i * 15000).toISOString(), label: i % 11 === 0 ? 'incident' : 'noise' };
    return { id: `${name}-${i + 1}`, text: `Sample record ${i + 1} of ${name}`, label: i % 2 ? 'positive' : 'negative', value: Math.round(Math.sin(i) * 1000) / 1000, nested: { a: i, b: [i, i + 1] } };
  };
  const total = meta.records;
  return json({ name, total, offset, limit, items: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, k) => rec(offset + k)) });
});

// evals
R('GET', '/api/evals', () => json({ items: S.evals.map(({ cases, ...r }) => ({ ...r, cases: undefined })) }));
R('POST', '/api/evals', (_m, _u, b) => {
  const id = `evr_${String(S.evals.length + 1).padStart(4, '0')}`;
  const run = { eval_run_id: id, status: 'running', suite: b.suite || 'core16', profile: b.profile || 'competent', label: b.label, scenario_ids: SCENARIOS.map((s) => s.scenario_id), concurrency: b.concurrency || 2, baseline_id: b.baseline_id || 'evr_0001', created_at: new Date().toISOString(), progress: { done: 0, total: 16 }, cases: [], summary: null, thread_id: 'thr-s10' };
  S.evals.unshift(run);
  const done = S.evals.find((e) => e.eval_run_id === 'evr_0001');
  let i = 0;
  const t = setInterval(() => {
    const c = done.cases[i];
    run.cases.push({ ...c, score: Math.min(1, c.score + (b.profile === 'naive' && c.tier === 3 ? -0.4 : 0)), passed: !(b.profile === 'naive' && c.tier === 3) });
    run.progress.done = ++i;
    mkEvent('eval.progress', null, 'info', { eval_run_id: id, done: i, total: 16 });
    if (i >= 16) {
      clearInterval(t);
      run.status = 'finished';
      run.finished_at = new Date().toISOString();
      run.summary = { mean_score: Math.round((run.cases.reduce((a, c) => a + c.score, 0) / 16) * 100) / 100, pass_rate: run.cases.filter((c) => c.passed).length / 16, gate: { no_crash: 1, safety: 1 } };
      mkEvent('eval.finished', null, 'info', { eval_run_id: id });
    }
  }, 700);
  return json({ eval_run: run, event: { event_id: 'evt_eval', status: 'routed' } }, 202);
});
R('GET', '/api/evals/:id', (_m, u) => { const e = S.evals.find((x) => x.eval_run_id === decodeURIComponent(u.pathname.split('/').pop())); return e ? json(e) : err(404, 'not_found', 'unknown eval run'); });

// memory
R('GET', '/api/memory/namespaces', () => { const set = new Map(); for (const m of S.memory) { set.set(m.namespace.join('.'), m.namespace); set.set(m.namespace[0], [m.namespace[0]]); } return json({ items: [...set.values()] }); });
R('GET', '/api/memory/search', (_m, u) => {
  const q = (u.searchParams.get('q') || '').toLowerCase().split(/\W+/).filter(Boolean);
  const ns = u.searchParams.get('namespace');
  const scored = S.memory.filter((m) => !ns || m.namespace.join('.').startsWith(ns)).map((m) => { const text = JSON.stringify(m.value).toLowerCase(); const hit = q.filter((w) => text.includes(w)).length; return { ...m, score: Math.min(0.95, 0.15 + hit / Math.max(1, q.length) * 0.7) }; }).filter((m) => m.score > 0.2).sort((a, b) => b.score - a.score).slice(0, 10);
  return json({ items: scored, query: u.searchParams.get('q') });
});
R('POST', '/store/items/search', (_m, _u, b) => { const pre = (b.namespace_prefix || []).join('.'); return json({ items: S.memory.filter((m) => !pre || m.namespace.join('.').startsWith(pre)).slice(0, b.limit || 50) }); });
R('PUT', '/store/items', (_m, _u, b) => { const i = S.memory.findIndex((m) => m.key === b.key && m.namespace.join('.') === b.namespace.join('.')); const item = { namespace: b.namespace, key: b.key, value: b.value, created_at: i >= 0 ? S.memory[i].created_at : new Date().toISOString(), updated_at: new Date().toISOString() }; if (i >= 0) S.memory[i] = item; else S.memory.push(item); return new Response(null, { status: 204 }); });
R('DELETE', '/store/items', (_m, _u, b) => { S.memory = S.memory.filter((m) => !(m.key === b.key && m.namespace.join('.') === b.namespace.join('.'))); return new Response(null, { status: 204 }); });

// graphs
R('GET', '/api/graphs', () => json({ items: Object.entries(GRAPH_META).map(([id, [title, role, features]]) => ({ graph_id: id, title, role, features, has_guide: true, assistants: id === 'incident_commander' ? [{ assistant_id: `asst-${id}`, name: 'incident_commander', version: 1 }, { assistant_id: 'asst-naive', name: 'commander-naive', version: 2 }, { assistant_id: 'asst-flagfirst', name: 'commander-flag-first', version: 1 }] : id === 'ops_copilot' ? [{ assistant_id: `asst-${id}`, name: 'ops_copilot', version: 1 }, { assistant_id: 'asst-cp5', name: 'copilot-sandbox-S05', version: 1 }] : [{ assistant_id: `asst-${id}`, name: id, version: 1 }], studio: { base_url: '' } })), assistants_scan_ok: true }));
R('GET', '/api/assistants', () => json({ items: [{ assistant_id: 'asst-incident_commander', graph_id: 'incident_commander', name: 'incident_commander', context: { model_profile: 'competent', approval_policy: 'standard', planner_prompt: 'You plan remediation. Prefer reversible actions.' } }, { assistant_id: 'asst-naive', graph_id: 'incident_commander', name: 'commander-naive', context: { model_profile: 'naive', approval_policy: 'standard', planner_prompt: 'You plan remediation. Prefer reversible actions.' } }, { assistant_id: 'asst-flagfirst', graph_id: 'incident_commander', name: 'commander-flag-first', context: { model_profile: 'competent', approval_policy: 'standard', planner_prompt: 'You plan remediation. Prefer reversible actions.\npolicy: mitigate_before_fix' } }], scan_ok: true }));
R('GET', '/api/studio/guide', () => json(GUIDE));
R('POST', '/api/admin/reset', () => json({ status: 'reset', deleted: { events: S.events.length, incidents: S.incidents.size } }));
R('POST', '/api/admin/ambient', (_m, _u, b) => { S.ambient = !!b.enabled; return json({ ambient_enabled: S.ambient }); });

// built-in Agent Server
R('GET', '/ok', () => json({ ok: true }));
R('POST', '/assistants/search', () => json(Object.entries(GRAPH_META).map(([id, [title, desc]]) => ({ assistant_id: `asst-${id}`, graph_id: id, name: id, description: desc, version: 1, metadata: { created_by: 'system' }, config: {}, context: {} }))));
R('GET', '/assistants/:id/graph', (_m, u) => { const gid = decodeURIComponent(u.pathname.split('/')[2]).replace(/^asst-/, ''); const f = GRAPH_STRUCT[gid]; if (!f) return err(404, 'not_found', 'graph not found'); return json(f(u.searchParams.get('xray') === 'true')); });
R('GET', '/assistants/:id/schemas', (_m, u) => { const gid = decodeURIComponent(u.pathname.split('/')[2]).replace(/^asst-/, ''); const ctxSchema = { title: 'Context', type: 'object', properties: { model_profile: { default: 'competent', enum: ['competent', 'naive', 'cautious', 'gullible'], title: 'Model Profile', type: 'string' }, approval_policy: { default: 'standard', title: 'Approval Policy', type: 'string' }, token_delay_ms: { default: 15, title: 'Token Delay Ms', type: 'integer' }, planner_prompt: { default: 'You plan remediation. Prefer reversible actions.', description: 'Planner system prompt', langgraph_nodes: ['remediation_planner'], langgraph_type: 'prompt', title: 'Planner Prompt', type: 'string' }, supervisor_prompt: { default: 'You are the incident commander.', langgraph_nodes: ['supervisor'], langgraph_type: 'prompt', title: 'Supervisor Prompt', type: 'string' } } }; return json({ graph_id: gid, input_schema: { title: gid + 'Input', type: 'object', properties: { scenario_id: { type: 'string' }, incident: { type: 'object' } }, required: ['scenario_id'] }, output_schema: { title: 'Output', type: 'object', properties: { result: { type: 'object' } } }, state_schema: { title: 'State', type: 'object', properties: { scenario_id: { type: 'string' }, hypotheses: { type: 'array', items: { type: 'object' } }, applied_actions: { type: 'array' } } }, config_schema: ctxSchema, context_schema: ctxSchema }); });
R('POST', '/threads', (_m, _u, b) => { const id = `thr-${hex(10)}`; S.threads.set(id, { ...mkThread(id), metadata: b.metadata || {}, graph: b.metadata?.foghorn_graph || 'incident_commander' }); return json({ thread_id: id, created_at: new Date().toISOString(), metadata: b.metadata || {}, status: 'idle' }); });
R('POST', '/threads/search', (_m, _u, b) => { let list = [...S.threads.values()]; const md = b.metadata || {}; for (const [k, v] of Object.entries(md)) list = list.filter((t) => t.metadata?.[k] === v); if (b.status) list = list.filter((t) => t.status === b.status); list.sort((a, c) => String(c.updated_at).localeCompare(String(a.updated_at))); return json(list.slice(0, b.limit || 50).map(({ runs, history, ...t }) => t)); });
R('GET', '/threads/:id', (_m, u) => { const t = S.threads.get(decodeURIComponent(u.pathname.split('/')[2])); if (!t) return err(404, 'not_found', 'Thread not found'); const { runs, history, ...rest } = t; return json({ ...rest, status: t.staticInterrupts?.some(([a]) => S.approvals.get(a)?.status === 'pending') ? 'interrupted' : t.status }); });
R('GET', '/threads/:id/state', (_m, u) => {
  const t = S.threads.get(decodeURIComponent(u.pathname.split('/')[2]));
  if (!t) return err(404, 'not_found', 'Thread not found');
  const pend = (t.staticInterrupts || []).map(([a]) => S.approvals.get(a)).filter((a) => a && a.status === 'pending');
  const tasks = pend.length ? [{ id: 'task-gate', name: 'approval_gate', interrupts: pend.map((a) => ({ id: a.interrupt_id, value: a.payload })) }] : [];
  return json({ values: t.values || {}, next: pend.length ? ['approval_gate'] : t.finished || t.status === 'idle' ? [] : ['supervisor'], tasks, checkpoint: { checkpoint_id: t.history[0]?.checkpoint.checkpoint_id || 'cp-0' }, created_at: t.updated_at });
});
R('POST', '/threads/:id/state', (_m, u, b) => { const t = S.threads.get(decodeURIComponent(u.pathname.split('/')[2])); if (!t) return err(404, 'not_found', 'Thread not found'); const cp = { checkpoint_id: `cp-fork-${hex(8)}`, checkpoint_ns: '' }; const parent = t.history.find((h2) => h2.checkpoint.checkpoint_id === b.checkpoint?.checkpoint_id); const vals = { ...(parent?.values || t.values) }; for (const [k, v] of Object.entries(b.values || {})) vals[k] = v && v.type === '__overwrite__' ? v.value : v; t.history.unshift({ values: vals, next: [b.as_node || 'supervisor'], tasks: [], checkpoint: cp, parent_checkpoint: parent ? { checkpoint_id: parent.checkpoint.checkpoint_id } : null, metadata: { source: 'update', step: (parent?.metadata.step ?? 0) + 1, writes: { [b.as_node || 'supervisor']: b.values } }, created_at: new Date().toISOString() }); return json({ checkpoint: cp }); });
R('POST', '/threads/:id/history', (_m, u) => { const t = S.threads.get(decodeURIComponent(u.pathname.split('/')[2])); if (!t) return err(404, 'not_found', 'Thread not found'); if (!t.history.length) { const s1 = incidentStage1({ runId: 'x' }); pushHistory(t, s1); } return json(t.history); });
R('POST', '/threads/:id/copy', () => json({ thread_id: `thr-${hex(10)}` }));
R('GET', '/threads/:id/runs', (_m, u) => { const t = S.threads.get(decodeURIComponent(u.pathname.split('/')[2])); if (!t) return err(404, 'not_found', 'Thread not found'); if (!t.runs.length && !t.finished && (t.metadata?.foghorn_graph || 'incident_commander') === 'incident_commander' && t.graph === 'incident_commander') { const inc = [...S.incidents.values()].find((x) => x.thread_id === t.thread_id); if (inc && inc.status !== 'resolved' && !t.staticInterrupts?.length) launchCommanderRun(t.thread_id, { stage: 1, incidentId: inc.incident_id, scenario: inc.scenario_id, interruptId: hex(32) }); else if (t.thread_id === 'thr-s10') launchCommanderRun('thr-s10', { stage: 1, incidentId: 'INC-D0003', scenario: 'S10', interruptId: 'a3f8c1d2e9b04a6f8c1d2e9b04a6f8c1' }); } return json(t.runs.slice(0, Number(u.searchParams.get('limit') || 10)).map((r) => ({ run_id: r.run_id, thread_id: r.thread_id, status: Date.now() - r.startedAt < r.duration ? 'running' : r.status, created_at: r.created_at, assistant_id: r.assistant_id, metadata: r.metadata }))); });
R('GET', '/threads/:id/runs/:rid', (_m, u) => { const r = S.runs.get(decodeURIComponent(u.pathname.split('/')[4])); return r ? json({ run_id: r.run_id, thread_id: r.thread_id, status: Date.now() - r.startedAt < r.duration ? 'running' : r.status, assistant_id: r.assistant_id }) : err(404, 'not_found', 'run not found'); });
R('GET', '/threads/:id/runs/:rid/stream', (_m, u, _b, init) => { const r = S.runs.get(decodeURIComponent(u.pathname.split('/')[4])); if (!r) return err(404, 'not_found', 'run not found'); const lei = init?.headers?.['Last-Event-ID']; const idx = lei && lei !== '-1' ? Number(String(lei).split('-').pop()) : -1; return runStream(r, idx); });
R('POST', '/threads/:id/runs/:rid/cancel', (_m, u) => { const r = S.runs.get(decodeURIComponent(u.pathname.split('/')[4])); if (r) { r.status = 'interrupted'; r.duration = 0; } return json({}); });
function createGraphRun(threadId, b) {
  const t = S.threads.get(threadId) || (S.threads.set(threadId, mkThread(threadId)), S.threads.get(threadId));
  const graph = b.assistant_id && GRAPH_META[b.assistant_id] ? b.assistant_id : t.graph || 'incident_commander';
  const runId = `run-${hex(8)}`;
  if (graph === 'ops_copilot' || b.input?.messages) {
    const text = b.input?.messages?.[0]?.content || 'continue';
    const hist = t.values?.messages || [];
    if (b.command?.resume) {
      const script = withIdx(copilotTurnResume(runId, hist));
      return startRun(threadId, script, { graphId: 'ops_copilot', onDone: () => { t.values = clone(script.filter((e) => e.event === 'values').pop().data); } });
    }
    const { events } = copilotTurn({ runId, text, history: hist });
    return startRun(threadId, withIdx(events.map(({ i, ...x }) => x)), { graphId: 'ops_copilot' });
  }
  if (b.command?.resume) {
    const inc = [...S.incidents.values()].find((x) => x.thread_id === threadId);
    const resp = Object.values(b.command.resume)[0] || b.command.resume;
    for (const [a, intId] of t.staticInterrupts || []) { const ap = S.approvals.get(a); if (ap?.status === 'pending') decide(ap, resp); void intId; }
    return launchCommanderRun(threadId, { stage: 2, decision: resp.decision, incidentId: inc?.incident_id, scenario: inc?.scenario_id });
  }
  if (graph === 'incident_commander') {
    const inc = [...S.incidents.values()].find((x) => x.thread_id === threadId);
    return launchCommanderRun(threadId, { stage: 1, incidentId: inc?.incident_id || `INC-${Math.floor(Math.random() * 9000 + 1000)}`, scenario: b.input?.scenario_id || 'S10', interruptId: hex(32) });
  }
  return startRun(threadId, withIdx(genericRun({ runId, graphId: graph, input: b.input }).map(({ i, ...x }) => x)), { graphId: graph });
}
function copilotTurnResume(runId, hist) {
  const mid = `ai-${runId.slice(0, 6)}`;
  const answer = 'Saved. The DBA team preference is now in the Store under prefs/dba, so any new conversation can recall it.';
  const ev = [];
  let t = 0;
  const push = (event, data, dt = 0) => { t += dt; ev.push({ t, event, data }); };
  push('metadata', { run_id: runId, attempt: 1 }, 5);
  push('messages', [{ type: 'tool', id: 'tr', tool_call_id: 'call_s1', name: 'save_memory', content: 'saved prefs/dba' }, { langgraph_node: 'tools' }], 200);
  for (const w of answer.match(/\S+\s*/g)) push('messages', [{ type: 'AIMessageChunk', id: mid, content: w }, { langgraph_node: 'model' }], 16);
  push('values', { messages: [...hist, { type: 'tool', content: 'saved', tool_call_id: 'call_s1' }, { type: 'ai', id: mid, content: answer }] }, 20);
  push('end', null, 20);
  return ev;
}
R('POST', '/threads/:id/runs', (_m, u, b) => { const run = createGraphRun(decodeURIComponent(u.pathname.split('/')[2]), b); return json({ run_id: run.run_id, thread_id: run.thread_id, status: 'pending' }); });
R('POST', '/threads/:id/runs/stream', (_m, u, b) => { const run = createGraphRun(decodeURIComponent(u.pathname.split('/')[2]), b); return runStream(run, -1); });
R('POST', '/runs/crons/search', () => json([{ cron_id: 'cron-1', assistant_id: 'shift_handoff', schedule: '0 */8 * * *', enabled: false, next_run_date: null, metadata: { graph_id: 'shift_handoff' } }]));
R('PATCH', '/runs/crons/:id', () => json({}));

// ---------------------------------------------------------------------------------------------------------------------
// SSE bus

function busResponse(url, init) {
  const params = new URL(url, location.origin).searchParams;
  const since = Number(params.get('since') || (init?.headers?.['Last-Event-ID']) || 0) || 0;
  let sub = null;
  let hb = null;
  const body = new ReadableStream({
    start(ctrl) {
      const push = (frame) => { try { ctrl.enqueue(enc.encode(frame)); } catch { /* closed */ } };
      push(encodeSSE({ event: 'foghorn.hello', data: { server_time: new Date().toISOString(), latest_seq: S.seq, resumed_from: since } }));
      for (const e of S.events.filter((x) => x.seq > since)) push(encodeSSE({ event: 'foghorn.event', data: e, id: String(e.seq) }));
      push(encodeSSE({ event: 'foghorn.queue', data: S.queue }));
      push(encodeSSE({ event: 'foghorn.clock', data: clockObj() }));
      sub = push;
      S.subs.add(sub);
      hb = setInterval(() => push(': heartbeat\r\n\r\n'), 15000);
    },
    cancel() {
      S.subs.delete(sub);
      clearInterval(hb);
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

// ---------------------------------------------------------------------------------------------------------------------

const FALLBACK = new Set([404, 405, 501, 502, 503, 504]);

export function installMock({ force = false } = {}) {
  seed();
  startStory();
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
    const method = (init.method || (typeof input !== 'string' && input.method) || 'GET').toUpperCase();
    const path = url.pathname;
    const isApi = path.startsWith('/api/') || /^\/(assistants|threads|store|runs|ok|healthz|info)(\/|$)/.test(path);
    if (!isApi) return realFetch(input, init);
    let hit = null;
    if (path === '/api/stream') hit = { handler: () => busResponse(url.href, init) };
    else for (const r of routes) {
      if (r.method !== method) continue;
      const m = r.re.exec(path);
      if (m) {
        hit = { handler: r.handler };
        break;
      }
    }
    if (!hit) return force ? err(404, 'not_found', `mock: no route for ${method} ${path}`) : realFetch(input, init);
    if (!force) {
      try {
        const real = await realFetch(input, init);
        if (!FALLBACK.has(real.status)) return real;
        try { real.body?.cancel(); } catch { /* ignore */ }
      } catch {
        /* network error: use the mock */
      }
    }
    let body = {};
    if (init.body && typeof init.body === 'string') {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = {};
      }
    }
    await new Promise((r) => setTimeout(r, 40 + Math.random() * 90));
    try {
      return hit.handler(method, url, body, init);
    } catch (e) {
      console.error('mock handler failed', method, path, e);
      return err(500, 'mock_error', String(e.message || e));
    }
  };
  window.__mock = S;
}
