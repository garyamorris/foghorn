// The ONE module that knows endpoint paths. Everything else calls `api.*` / `paths.*`.
//
// Two backends on the same origin:
//   (a) Foghorn's custom routes under /api/*      (documented in docs/API.md)
//   (b) the built-in LangGraph Agent Server API   (/assistants, /threads, /runs, /store)
// A hand-off to a different origin is possible for development with `?api=http://host:port` (stored in localStorage).

import { lsGet, lsSet } from './core/store.js';
import { openStream } from './core/stream.js';

const qs = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams();
if (qs.get('api')) lsSet('api_base', qs.get('api').replace(/\/$/, ''));
if (qs.get('api') === '') lsSet('api_base', '');
export const API_BASE = lsGet('api_base', '') || '';

export const paths = {
  // --- Foghorn /api (docs/API.md) ---
  health: '/api/health',
  healthz: '/healthz',
  config: '/api/config',
  summary: '/api/summary',
  clock: '/api/clock',
  stream: '/api/stream',
  events: '/api/events',
  event: (id) => `/api/events/${enc(id)}`,
  eventsInject: '/api/events/inject',
  eventPresets: '/api/events/presets',
  trace: (id) => `/api/trace/${enc(id)}`,
  linksEvent: (id) => `/api/links/event/${enc(id)}`,
  dispatches: '/api/dispatches',
  replay: '/api/replay',
  replayAction: (a) => `/api/replay/${a}`,
  streams: '/api/streams',
  scenarios: '/api/scenarios',
  scenario: (id) => `/api/scenarios/${enc(id)}`,
  scenarioLaunch: (id) => `/api/scenarios/${enc(id)}/launch`,
  faults: '/api/faults',
  services: '/api/services',
  incidents: '/api/incidents',
  incident: (id) => `/api/incidents/${enc(id)}`,
  approvals: '/api/approvals',
  approval: (id) => `/api/approvals/${enc(id)}`,
  approvalResume: (id) => `/api/approvals/${enc(id)}/resume`,
  approvalsBatch: '/api/approvals/resume-batch',
  dlq: '/api/dlq',
  dlqItem: (id) => `/api/dlq/${enc(id)}`,
  dlqAction: (id, a) => `/api/dlq/${enc(id)}/${a}`,
  queues: '/api/queues',
  datasets: '/api/datasets',
  dataset: (name) => `/api/datasets/${enc(name)}`,
  evals: '/api/evals',
  evalRun: (id) => `/api/evals/${enc(id)}`,
  memNamespaces: '/api/memory/namespaces',
  memSearch: '/api/memory/search',
  memItem: '/api/memory/item',
  graphs: '/api/graphs',
  assistants: '/api/assistants',
  guide: '/api/studio/guide',
  adminReset: '/api/admin/reset',
  adminAmbient: '/api/admin/ambient',

  // --- built-in LangGraph Agent Server ---
  lgInfo: '/info',
  lgOk: '/ok',
  lgAssistantsSearch: '/assistants/search',
  lgAssistant: (id) => `/assistants/${enc(id)}`,
  lgAssistantGraph: (id) => `/assistants/${enc(id)}/graph`,
  lgAssistantSchemas: (id) => `/assistants/${enc(id)}/schemas`,
  lgAssistantSubgraphs: (id) => `/assistants/${enc(id)}/subgraphs`,
  lgThreads: '/threads',
  lgThreadsSearch: '/threads/search',
  lgThread: (id) => `/threads/${enc(id)}`,
  lgThreadState: (id) => `/threads/${enc(id)}/state`,
  lgThreadStateAt: (id, cp) => `/threads/${enc(id)}/state/${enc(cp)}`,
  lgThreadHistory: (id) => `/threads/${enc(id)}/history`,
  lgThreadCopy: (id) => `/threads/${enc(id)}/copy`,
  lgThreadRuns: (id) => `/threads/${enc(id)}/runs`,
  lgThreadRunStream: (id) => `/threads/${enc(id)}/runs/stream`,
  lgRun: (tid, rid) => `/threads/${enc(tid)}/runs/${enc(rid)}`,
  lgRunJoinStream: (tid, rid) => `/threads/${enc(tid)}/runs/${enc(rid)}/stream`,
  lgRunCancel: (tid, rid) => `/threads/${enc(tid)}/runs/${enc(rid)}/cancel`,
  lgStoreItems: '/store/items',
  lgStoreSearch: '/store/items/search',
  lgStoreNamespaces: '/store/namespaces',
};

function enc(s) {
  return encodeURIComponent(String(s));
}

export class ApiError extends Error {
  constructor(status, code, message, details, url) {
    super(message || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || `http_${status}`;
    this.details = details || null;
    this.url = url;
  }
}

function authHeaders() {
  const token = lsGet('token', '');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function setToken(token) {
  lsSet('token', token || '');
}

export function buildUrl(path, query) {
  let url = API_BASE + path;
  if (query) {
    const parts = [];
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      if (Array.isArray(v)) for (const x of v) parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(x)}`);
      else parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    }
    if (parts.length) url += (url.includes('?') ? '&' : '?') + parts.join('&');
  }
  return url;
}

/** Core request. Returns parsed JSON (or null for 204 / empty). Throws ApiError. */
export async function request(method, path, { query, body, signal, timeoutMs = 20000, headers } = {}) {
  const url = buildUrl(path, query);
  const ctrl = new AbortController();
  const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  if (signal) signal.addEventListener('abort', () => ctrl.abort(), { once: true });
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { Accept: 'application/json', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...authHeaders(), ...(headers || {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      cache: 'no-store',
    });
  } catch (e) {
    if (e && e.name === 'AbortError') throw new ApiError(0, signal?.aborted ? 'aborted' : 'timeout', signal?.aborted ? 'Request cancelled' : 'Request timed out', null, url);
    throw new ApiError(0, 'network', 'Cannot reach the server', null, url);
  } finally {
    if (timer) clearTimeout(timer);
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const err = data && typeof data === 'object' ? data.error || data : null;
    let message = typeof data === 'string' ? data.slice(0, 300) : null;
    let code = null;
    let details = null;
    if (err && typeof err === 'object') {
      code = err.code || null;
      message = err.message || (typeof err.detail === 'string' ? err.detail : Array.isArray(err.detail) ? err.detail.map((d) => d.msg || JSON.stringify(d)).join('; ') : null) || message;
      details = err.details || null;
    }
    throw new ApiError(res.status, code, message || res.statusText, details, url);
  }
  return data;
}

export const get = (path, opts) => request('GET', path, opts);
export const post = (path, body, opts) => request('POST', path, { ...(opts || {}), body: body ?? {} });
export const put = (path, body, opts) => request('PUT', path, { ...(opts || {}), body });
export const patch = (path, body, opts) => request('PATCH', path, { ...(opts || {}), body });
export const del = (path, opts) => request('DELETE', path, opts);

export const RUN_STREAM_MODES = ['values', 'updates', 'messages-tuple', 'custom', 'tasks', 'checkpoints'];

export const api = {
  // meta
  health: (o) => get(paths.health, o),
  healthz: (o) => get(paths.healthz, o),
  config: (o) => get(paths.config, o),
  summary: (o) => get(paths.summary, o),
  clock: (o) => get(paths.clock, o),
  setClock: (body) => post(paths.clock, body),
  ok: (o) => get(paths.lgOk, o),
  // events
  events: (query, o) => get(paths.events, { ...o, query }),
  event: (id) => get(paths.event(id)),
  injectEvent: (body) => post(paths.eventsInject, body),
  postEvent: (envelope, idempotencyKey) => request('POST', '/api/events', { body: envelope, headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined }),
  eventPresets: () => get(paths.eventPresets),
  trace: (id) => get(paths.trace(id)),
  linksFor: (id) => get(paths.linksEvent(id)),
  dispatches: (query) => get(paths.dispatches, { query }),
  // replay / clock
  replay: () => get(paths.replay),
  replayStart: (body) => post(paths.replay, body),
  replayAction: (action, body) => post(paths.replayAction(action), body),
  streams: () => get(paths.streams),
  // scenarios / faults
  scenarios: (query) => get(paths.scenarios, { query }),
  scenario: (id, reveal = false) => get(paths.scenario(id), { query: { reveal_truth: reveal ? 'true' : undefined } }),
  launchScenario: (id, body) => post(paths.scenarioLaunch(id), body),
  faults: () => get(paths.faults),
  injectFault: (body) => post(paths.faults, body),
  services: () => get(paths.services),
  // incidents / approvals
  incidents: (query) => get(paths.incidents, { query }),
  incident: (id) => get(paths.incident(id)),
  approvals: (query) => get(paths.approvals, { query, timeoutMs: 30000 }),
  approval: (id) => get(paths.approval(id)),
  resumeApproval: (id, body) => post(paths.approvalResume(id), body),
  resumeBatch: (body) => post(paths.approvalsBatch, body),
  // dlq / queues
  dlq: (query) => get(paths.dlq, { query }),
  dlqRetry: (id) => post(paths.dlqAction(id, 'retry'), {}),
  dlqDiscard: (id, body) => post(paths.dlqAction(id, 'discard'), body),
  queues: () => get(paths.queues),
  // data
  datasets: () => get(paths.datasets),
  dataset: (name, query) => get(paths.dataset(name), { query }),
  evals: (query) => get(paths.evals, { query }),
  evalRun: (id) => get(paths.evalRun(id)),
  startEval: (body) => post(paths.evals, body),
  // memory
  memNamespaces: (query) => get(paths.memNamespaces, { query }),
  memSearch: (query) => get(paths.memSearch, { query }),
  memItem: (query) => get(paths.memItem, { query }),
  storeSearch: (body) => post(paths.lgStoreSearch, body),
  storePut: (body) => put(paths.lgStoreItems, body),
  storeDelete: (body) => request('DELETE', paths.lgStoreItems, { body }),
  storeNamespaces: (body) => post(paths.lgStoreNamespaces, body),
  // graphs / guide
  graphs: () => get(paths.graphs),
  assistants: () => get(paths.assistants),
  guide: () => get(paths.guide),
  adminReset: () => post(paths.adminReset, { confirm: 'reset' }),
  adminAmbient: (enabled) => post(paths.adminAmbient, { enabled }),

  // built-in LangGraph API
  lgAssistants: (limit = 100) => post(paths.lgAssistantsSearch, { limit }),
  lgGraph: (id, xray = true) => get(paths.lgAssistantGraph(id), { query: { xray: xray ? 'true' : undefined } }),
  lgSchemas: (id) => get(paths.lgAssistantSchemas(id)),
  lgSubgraphs: (id) => get(paths.lgAssistantSubgraphs(id), { query: { recurse: 'true' } }),
  lgCreateThread: (body) => post(paths.lgThreads, body || {}),
  lgThread: (id) => get(paths.lgThread(id)),
  lgThreadsSearch: (body) => post(paths.lgThreadsSearch, body || {}),
  lgState: (id, query) => get(paths.lgThreadState(id), { query }),
  lgStateAt: (id, cp, query) => get(paths.lgThreadStateAt(id, cp), { query }),
  lgHistory: (id, body) => post(paths.lgThreadHistory(id), body || { limit: 50 }),
  lgUpdateState: (id, body) => post(paths.lgThreadState(id), body),
  lgCopy: (id) => post(paths.lgThreadCopy(id), {}),
  lgRuns: (id, query) => get(paths.lgThreadRuns(id), { query }),
  lgRun: (tid, rid) => get(paths.lgRun(tid, rid)),
  lgCronsSearch: () => post('/runs/crons/search', { limit: 20 }),
  lgCronPatch: (id, body) => patch(`/runs/crons/${enc(id)}`, body),
  lgCreateRun: (tid, body) => post(paths.lgThreadRuns(tid), body),
  lgCancel: (tid, rid, action = 'interrupt') => post(paths.lgRunCancel(tid, rid), {}, { query: { action, wait: 'false' } }),
};

// ---------------------------------------------------------------------------------------------------------------
// Streaming helpers

/** SSE for the Foghorn bus (events, queue, clock, dispatches). */
export function openBusStream({ since, channels = 'events,queue,clock,dispatches', onEvent, onState, filters = {} }) {
  return openStream({
    url: buildUrl(paths.stream, { channels, since: since || undefined, ...filters }),
    headers: authHeaders(),
    lastEventId: since ? String(since) : '',
    onEvent,
    onState,
    idleTimeoutMs: 45000,
    reconnect: true,
    backoff: { base: 700, cap: 15000 },
  });
}

/**
 * Start a run and stream it. The first request is `POST /threads/{id}/runs/stream`; if the connection drops we
 * rejoin with `GET /threads/{id}/runs/{run_id}/stream` + Last-Event-ID (the run was created with
 * stream_resumable:true and on_disconnect:continue, so it keeps going).
 */
export function streamRun({ threadId, assistantId, input, command, context, config, checkpoint, interruptBefore, interruptAfter, multitask, metadata, modes = RUN_STREAM_MODES, onEvent, onState, onRunId }) {
  let runId = null;
  const body = {
    assistant_id: assistantId,
    stream_mode: modes,
    stream_subgraphs: true,
    stream_resumable: true,
    on_disconnect: 'continue',
    ...(input !== undefined ? { input } : {}),
    ...(command ? { command } : {}),
    ...(context ? { context } : {}),
    ...(config ? { config } : {}),
    ...(checkpoint ? { checkpoint } : {}),
    ...(interruptBefore ? { interrupt_before: interruptBefore } : {}),
    ...(interruptAfter ? { interrupt_after: interruptAfter } : {}),
    ...(multitask ? { multitask_strategy: multitask } : {}),
    ...(metadata ? { metadata } : {}),
  };
  return openStream({
    url: buildUrl(paths.lgThreadRunStream(threadId)),
    method: 'POST',
    body,
    headers: authHeaders(),
    reconnect: true,
    isTerminal: (ev) => ev.event === 'end' || ev.event === 'error',
    onEvent: (ev) => {
      if (ev.event === 'metadata' && !runId) {
        try {
          runId = JSON.parse(ev.data).run_id || null;
          if (runId && onRunId) onRunId(runId);
        } catch {
          /* ignore */
        }
      }
      onEvent(ev);
    },
    onOpen: (res) => {
      const loc = res.headers.get('content-location');
      const m = loc && loc.match(/runs\/([^/?]+)/);
      if (m && !runId) {
        runId = m[1];
        if (onRunId) onRunId(runId);
      }
    },
    onState,
    buildRequest: (lastId) => {
      if (!runId) return {};
      return { url: buildUrl(paths.lgRunJoinStream(threadId, runId), {}), method: 'GET', body: undefined, headers: { 'Last-Event-ID': lastId || '-1' } };
    },
    shouldReconnect: async () => {
      if (!runId) return false;
      try {
        const run = await api.lgRun(threadId, runId);
        return run && (run.status === 'running' || run.status === 'pending');
      } catch {
        return false;
      }
    },
    backoff: { base: 600, cap: 8000 },
    maxRetries: 12,
  });
}

/** Join a run that is already in flight (or recently finished: resumable streams replay for ~2 minutes). */
// NOTE: a join must NOT send `stream_mode`: the server replays what the run captured at creation, and a stream_mode
// query makes it filter that replay down to (almost) nothing (verified live: 0 events with six modes, tasks only with five).
export function joinRun({ threadId, runId, modes = RUN_STREAM_MODES, onEvent, onState, replay = true }) {
  return openStream({
    url: buildUrl(paths.lgRunJoinStream(threadId, runId), {}),
    method: 'GET',
    headers: { ...authHeaders(), ...(replay ? { 'Last-Event-ID': '-1' } : {}) },
    reconnect: true,
    isTerminal: (ev) => ev.event === 'end',
    onEvent,
    onState,
    shouldReconnect: async () => {
      try {
        const run = await api.lgRun(threadId, runId);
        return run && (run.status === 'running' || run.status === 'pending');
      } catch {
        return false;
      }
    },
    backoff: { base: 600, cap: 8000 },
    maxRetries: 12,
  });
}

// ---------------------------------------------------------------------------------------------------------------
// Studio links

const DEFAULT_STUDIO = 'https://smith.langchain.com/studio/';

export function studioBase(config) {
  const origin = config?.public_base_url || (typeof location !== 'undefined' ? location.origin : '');
  return config?.studio?.base_link || `${config?.studio?.url || DEFAULT_STUDIO}?baseUrl=${encodeURIComponent(origin)}`;
}

/** Best-effort per-thread deep link (the exact form is unverified, so the UI also offers "Copy thread id"). */
export function studioThreadUrl(config, threadId, graphId) {
  const tpl = config?.studio?.thread_url_template;
  const base = config?.public_base_url || (typeof location !== 'undefined' ? location.origin : '');
  const root = config?.studio?.url || DEFAULT_STUDIO;
  if (tpl) {
    return tpl
      .replaceAll('{thread_id}', threadId)
      .replaceAll('{studio}', root.replace(/\/$/, ''))
      .replaceAll('{base}', encodeURIComponent(base))
      .replaceAll('{graph_id}', graphId || '');
  }
  return `${root.replace(/\/$/, '')}/thread/${encodeURIComponent(threadId)}?baseUrl=${encodeURIComponent(base)}`;
}

export function studioGraphUrl(config, graphId) {
  const base = studioBase(config);
  return graphId ? `${base}${base.includes('?') ? '&' : '?'}graph=${encodeURIComponent(graphId)}` : base;
}
