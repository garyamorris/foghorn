// Run-stream reducer: folds LangGraph SSE parts into one mutable run model that the Incident Console, Copilot and
// Evaluations render. Pure (no DOM/network); `now` is injectable for tests.
//
// A "part" is {event, data, id?}. `event` is the SSE event name, e.g. "messages", "updates|metrics_analyst:3f2a",
// "custom", "tasks", "values", "checkpoints", "metadata", "error", "end". `data` is the decoded JSON.
//
// Custom events use the Foghorn envelope {v:1, kind, key, agent, t_virtual, data}; repeats with the same `key` and
// identical data are dropped (interrupt() re-runs a node from line 1, so resumed nodes re-emit); the same key with
// DIFFERENT data is an upsert (e.g. a series refreshed after remediation).

import { splitEventName, decodeData } from './sse.js';
import { RingBuffer } from './ring.js';
import { hashString } from './format.js';
import { normalizeInterrupt } from './schemaform.js';

export const GENERIC_NODES = new Set(['model', 'agent', 'tools', 'call_model', 'llm']);
export const INTERNAL_NODES = new Set(['__start__', '__end__', '__interrupt__']);
const SEEN_MAX = 4000;

export function createRunState(opts = {}) {
  return {
    runId: opts.runId || null,
    threadId: opts.threadId || null,
    status: 'idle', // idle | running | interrupted | done | error | cancelled
    error: null,
    startedAt: null,
    endedAt: null,
    lanes: {},
    laneOrder: [],
    timeline: [],
    evidence: [],
    hypotheses: {},
    hypothesisOrder: [],
    series: {},
    seriesOrder: [],
    plan: null,
    policy: [],
    memory: [],
    notes: new RingBuffer(opts.noteCapacity ?? 300),
    artifacts: [],
    metrics: {},
    handoffs: [],
    progress: null,
    interrupts: [],
    values: null,
    checkpoints: [],
    seen: new Map(),
    raw: new RingBuffer(opts.rawCapacity ?? 800),
    counts: { parts: 0, tokens: 0, duplicates: 0, byMode: {} },
    lastEventId: null,
    seq: 0,
  };
}

/** Reset transient per-run fields but keep the board (used when a resume starts a new run on the same thread). */
export function beginRun(state, runId, now = Date.now()) {
  state.runId = runId || state.runId;
  state.status = 'running';
  state.error = null;
  state.interrupts = [];
  state.endedAt = null;
  if (!state.startedAt) state.startedAt = now;
  return state;
}

/** Decode a raw SSE event ({event,data:string,id}) and apply it. */
export function applySSE(state, ev, now = Date.now()) {
  const data = decodeData(ev);
  if (ev.id !== undefined && ev.id !== null && ev.id !== '') state.lastEventId = ev.id;
  return applyPart(state, { event: ev.event, data, id: ev.id }, now);
}

/** Apply one decoded part. Returns the Set of changed areas: lanes|timeline|board|interrupts|values|status|raw|notes. */
export function applyPart(state, part, now = Date.now()) {
  const changed = new Set(['raw']);
  const { mode, ns, nsRaw } = splitEventName(part.event);
  state.counts.parts++;
  state.counts.byMode[mode] = (state.counts.byMode[mode] || 0) + 1;
  state.seq++;
  if (state.status === 'idle') {
    state.status = 'running';
    state.startedAt = state.startedAt || now;
    changed.add('status');
  }
  const data = part.data;
  // Token chunks dominate the volume: keep the raw drawer light (the chunk text only).
  state.raw.push({ n: state.seq, at: now, mode, ns: nsRaw, data });

  switch (mode) {
    case 'metadata':
      if (data && data.run_id) state.runId = data.run_id;
      break;
    case 'values':
      if (!ns.length) {
        state.values = data;
        changed.add('values');
        const intr = data && data.__interrupt__;
        if (Array.isArray(intr) && intr.length) setInterrupts(state, intr, changed);
      }
      break;
    case 'updates':
      applyUpdates(state, data, ns, nsRaw, now, changed);
      break;
    case 'messages':
      applyMessage(state, part.event, data, ns, now, changed);
      break;
    case 'tasks':
      applyTask(state, data, ns, nsRaw, now, changed);
      break;
    case 'checkpoints':
      applyCheckpoint(state, data, ns, changed);
      break;
    case 'custom':
      applyCustom(state, data, ns, now, changed);
      break;
    case 'error':
      state.status = 'error';
      state.error = typeof data === 'string' ? data : data?.message || data?.error || JSON.stringify(data);
      state.endedAt = now;
      closeRunning(state, now, 'error');
      changed.add('status');
      changed.add('timeline');
      break;
    case 'end':
      finishRun(state, now, changed);
      break;
    default:
      break; // debug / events / unknown: raw drawer only
  }
  return changed;
}

/** Mark the stream complete (call on `end` or when a stream closes normally and the run is no longer active). */
export function finishRun(state, now = Date.now(), changed = new Set()) {
  if (state.status === 'running') state.status = state.interrupts.length ? 'interrupted' : 'done';
  if (state.status !== 'idle') state.endedAt = state.endedAt || now;
  closeRunning(state, now, state.status === 'interrupted' ? 'interrupted' : 'done');
  for (const l of Object.values(state.lanes)) l.done = true;
  changed.add('status');
  changed.add('timeline');
  changed.add('lanes');
  return changed;
}

export function cancelRun(state, now = Date.now()) {
  state.status = 'cancelled';
  state.endedAt = now;
  closeRunning(state, now, 'cancelled');
}

function closeRunning(state, now, status) {
  for (const s of state.timeline) {
    if (s.status === 'running') {
      s.status = status === 'done' ? 'done' : status;
      s.endedAt = now;
      s.durationMs = now - s.startedAt;
    }
  }
}

function setInterrupts(state, list, changed) {
  const out = [];
  for (const raw of list) {
    const it = normalizeInterrupt(raw);
    if (it) out.push(it);
  }
  state.interrupts = out;
  if (out.length) {
    state.status = 'interrupted';
    changed.add('status');
  }
  changed.add('interrupts');
}

// ---- updates / tasks / timeline ------------------------------------------------------------------------------------

function applyUpdates(state, data, ns, nsRaw, now, changed) {
  if (!data || typeof data !== 'object') return;
  for (const [node, upd] of Object.entries(data)) {
    if (node === '__interrupt__') {
      if (Array.isArray(upd)) setInterrupts(state, upd, changed);
      continue;
    }
    if (INTERNAL_NODES.has(node)) continue;
    let step = null;
    for (let i = state.timeline.length - 1; i >= 0; i--) {
      const s = state.timeline[i];
      if (s.name === node && s.nsRaw === nsRaw && s.status === 'running') {
        step = s;
        break;
      }
    }
    if (step) {
      finishStep(step, now, 'done');
    } else if (!state.counts.byMode.tasks) {
      // tasks mode not requested: synthesise a completed step from the update
      const prev = state.timeline.length ? state.timeline[state.timeline.length - 1] : null;
      state.timeline.push({
        id: `u${state.seq}`,
        name: node,
        ns: ns.map((n) => n.node),
        nsRaw,
        depth: ns.length,
        status: 'done',
        startedAt: prev?.endedAt ?? state.startedAt ?? now,
        endedAt: now,
        durationMs: Math.max(0, now - (prev?.endedAt ?? state.startedAt ?? now)),
        update: upd,
      });
    }
    if (step) step.update = upd;
    markLaneDone(state, node, ns);
    changed.add('timeline');
    changed.add('lanes');
  }
}

function finishStep(step, now, status) {
  step.status = status;
  step.endedAt = now;
  step.durationMs = Math.max(0, now - step.startedAt);
}

function applyTask(state, data, ns, nsRaw, now, changed) {
  if (!data || typeof data !== 'object') return;
  const id = data.id || `t${state.seq}`;
  const name = data.name || '?';
  if (INTERNAL_NODES.has(name)) return;
  const isResult = 'result' in data || 'error' in data;
  let step = state.timeline.find((s) => s.id === id);
  if (!isResult) {
    if (!step) {
      step = {
        id,
        name,
        ns: ns.map((n) => n.node),
        nsRaw,
        depth: ns.length,
        status: 'running',
        startedAt: now,
        endedAt: null,
        durationMs: null,
        triggers: data.triggers,
      };
      state.timeline.push(step);
    }
  } else {
    if (!step) {
      step = { id, name, ns: ns.map((n) => n.node), nsRaw, depth: ns.length, status: 'running', startedAt: now, triggers: data.triggers };
      state.timeline.push(step);
    }
    if (data.error) {
      finishStep(step, now, 'error');
      step.error = typeof data.error === 'string' ? data.error : data.error.message || JSON.stringify(data.error);
    } else if (Array.isArray(data.interrupts) && data.interrupts.length) {
      finishStep(step, now, 'interrupted');
      setInterrupts(state, data.interrupts, changed);
    } else {
      finishStep(step, now, 'done');
    }
    if (data.result !== undefined) step.result = data.result;
    markLaneDone(state, name, ns);
  }
  changed.add('timeline');
}

function markLaneDone(state, node, ns) {
  const nsNodes = ns.map((n) => n.node);
  for (const lane of Object.values(state.lanes)) {
    if (lane.node === node && lane.ns.join('/') === nsNodes.join('/')) lane.done = true;
    // a finished subgraph node closes every lane inside it
    if (!ns.length && lane.ns[0] === node) lane.done = true;
  }
}

function applyCheckpoint(state, data, ns, changed) {
  if (!data || typeof data !== 'object') return;
  const cfg = data.config?.configurable || data.configurable || {};
  const id = cfg.checkpoint_id || data.checkpoint_id || data.id;
  if (!id) return;
  if (state.checkpoints.some((c) => c.id === id)) return;
  state.checkpoints.push({
    id,
    ns: ns.map((n) => n.node).join('/'),
    step: data.metadata?.step ?? null,
    source: data.metadata?.source ?? null,
    next: data.next || [],
    parent: data.parent_config?.configurable?.checkpoint_id || null,
    createdAt: data.created_at || null,
  });
  if (state.checkpoints.length > 500) state.checkpoints.splice(0, state.checkpoints.length - 500);
  changed.add('checkpoints');
}

// ---- messages (token streaming) ---------------------------------------------------------------------------------------

export function contentText(content) {
  if (content === null || content === undefined) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    let s = '';
    for (const b of content) {
      if (typeof b === 'string') s += b;
      else if (b && (b.type === 'text' || b.type === 'text_delta') && typeof b.text === 'string') s += b.text;
    }
    return s;
  }
  if (typeof content === 'object' && typeof content.text === 'string') return content.text;
  return '';
}

export function laneIdentity(nsNodes, meta) {
  const node = (meta && meta.langgraph_node) || '';
  let path = nsNodes.slice();
  if (!path.length && meta && typeof meta.langgraph_checkpoint_ns === 'string' && meta.langgraph_checkpoint_ns.includes('|')) {
    path = meta.langgraph_checkpoint_ns
      .split('|')
      .map((s) => s.split(':')[0])
      .filter(Boolean);
    if (path[path.length - 1] === node) path.pop();
  }
  const parts = path.slice();
  if (node && !GENERIC_NODES.has(node) && parts[parts.length - 1] !== node) parts.push(node);
  if (!parts.length && meta && meta.lc_agent_name) parts.push(meta.lc_agent_name);
  if (!parts.length) parts.push(node || 'agent');
  return { key: parts.join('/'), label: parts.join(' / '), node, ns: path };
}

function applyMessage(state, eventName, data, ns, now, changed) {
  if (!Array.isArray(data)) return;
  let chunk;
  let meta;
  const legacy = eventName.startsWith('messages/');
  if (legacy) {
    if (eventName.startsWith('messages/metadata')) return;
    // messages/partial|complete: [message, ...] cumulative content, metadata keyed by id (ignored): lane = ns
    for (const msg of data) {
      if (!msg || typeof msg !== 'object') continue;
      const id = laneIdentity(ns.map((n) => n.node), { langgraph_node: msg.name || '' });
      upsertSegment(state, id, msg, contentText(msg.content), now, true, changed);
    }
    return;
  }
  [chunk, meta] = data;
  if (!chunk || typeof chunk !== 'object') return;
  const type = String(chunk.type || '').toLowerCase();
  if (type === 'human' || type === 'system' || type === 'humanmessage' || type === 'systemmessage') return;
  const id = laneIdentity(ns.map((n) => n.node), meta);
  if (type === 'tool' || type === 'toolmessage' || type === 'toolmessagechunk') {
    const tid = chunk.tool_call_id || chunk.id || `tool${state.seq}`;
    // a result belongs to the call that announced it, in whichever lane that was (calls are keyed
    // "<message>:<index>", results by tool_call_id; a root-level agent has separate model/tools lanes)
    let owner = null;
    let call = null;
    for (const k of state.laneOrder) {
      const found = Object.values(state.lanes[k].tools).find((c) => c.id === tid);
      if (found) {
        owner = state.lanes[k];
        call = found;
        break;
      }
    }
    if (!call) {
      owner = ensureLane(state, id, now, changed);
      call = owner.tools[tid] = { id: tid, name: chunk.name || '', args: '', result: '' };
    }
    if (chunk.name && !call.name) call.name = chunk.name;
    call.result = (call.result || '') + contentText(chunk.content);
    call.done = true;
    owner.lastAt = now;
    changed.add('lanes');
    return;
  }
  const text = contentText(chunk.content);
  if (Array.isArray(chunk.tool_call_chunks) && chunk.tool_call_chunks.length) {
    const lane = ensureLane(state, id, now, changed);
    for (const tc of chunk.tool_call_chunks) {
      const idx = tc.index ?? 0;
      const k = `${chunk.id || 'm'}:${idx}`;
      const call = lane.tools[k] || (lane.tools[k] = { id: tc.id || k, name: '', args: '', result: '' });
      if (tc.id && !String(call.id).startsWith(tc.id)) call.id = tc.id;
      if (tc.name) call.name = tc.name;
      if (tc.args) call.args += tc.args;
    }
    lane.lastAt = now;
    changed.add('lanes');
  }
  if (Array.isArray(chunk.tool_calls) && chunk.tool_calls.length && !chunk.tool_call_chunks?.length) {
    const lane = ensureLane(state, id, now, changed);
    for (const tc of chunk.tool_calls) {
      const k = tc.id || `${chunk.id}:${tc.name}`;
      lane.tools[k] = { ...(lane.tools[k] || {}), id: tc.id || k, name: tc.name, args: JSON.stringify(tc.args ?? {}), result: lane.tools[k]?.result || '' };
    }
    changed.add('lanes');
  }
  if (text) upsertSegment(state, id, chunk, text, now, false, changed);
}

function ensureLane(state, id, now, changed) {
  let lane = state.lanes[id.key];
  if (!lane) {
    lane = {
      key: id.key,
      label: id.label,
      node: id.node,
      ns: id.ns,
      segments: [],
      tools: {},
      tokens: 0,
      startedAt: now,
      lastAt: now,
      done: false,
    };
    state.lanes[id.key] = lane;
    state.laneOrder.push(id.key);
    changed.add('lanes');
  }
  return lane;
}

function upsertSegment(state, id, chunk, text, now, cumulative, changed) {
  const lane = ensureLane(state, id, now, changed);
  const mid = chunk.id || null;
  let seg = lane.segments[lane.segments.length - 1];
  if (!seg || (mid && seg.id && seg.id !== mid)) {
    seg = { id: mid, text: '', tokens: 0 };
    lane.segments.push(seg);
    if (lane.segments.length > 40) lane.segments.shift();
  } else if (mid && !seg.id) {
    seg.id = mid;
  }
  if (cumulative) seg.text = text;
  else {
    seg.text += text;
    seg.tokens++;
    lane.tokens++;
    state.counts.tokens++;
  }
  lane.lastAt = now;
  lane.done = false;
  changed.add('lanes');
}

// ---- custom events --------------------------------------------------------------------------------------------------

function applyCustom(state, payload, ns, now, changed) {
  if (!payload || typeof payload !== 'object') {
    pushNote(state, { kind: 'custom', text: String(payload), at: now }, changed);
    return;
  }
  if (payload.v !== 1 || !payload.kind) {
    // foreign custom event (not the Foghorn envelope): show it generically
    pushNote(state, { kind: 'custom', text: JSON.stringify(payload).slice(0, 400), at: now, ns: ns.map((n) => n.node).join('/') }, changed);
    return;
  }
  const { kind } = payload;
  const d = payload.data || {};
  const key = payload.key || `${kind}:${hashString(JSON.stringify(d))}`;
  const fp = hashString(JSON.stringify(d));
  if (state.seen.get(key) === fp) {
    state.counts.duplicates++;
    return;
  }
  state.seen.set(key, fp);
  if (state.seen.size > SEEN_MAX) {
    let n = 0;
    for (const k of state.seen.keys()) {
      state.seen.delete(k);
      if (++n >= SEEN_MAX / 4) break;
    }
  }
  const agent = payload.agent || (ns.length ? ns[ns.length - 1].node : null);
  const meta = { agent, tVirtual: payload.t_virtual || null, key, at: now, ns: ns.map((n) => n.node).join('/') };
  switch (kind) {
    case 'progress':
      state.progress = { label: d.label, pct: d.pct, ...meta };
      changed.add('board');
      break;
    case 'evidence.found': {
      const id = d.id || key;
      const item = { ...d, id, ...meta };
      const i = state.evidence.findIndex((e) => e.id === id);
      if (i >= 0) state.evidence[i] = item;
      else state.evidence.push(item);
      if (state.evidence.length > 300) state.evidence.shift();
      changed.add('board');
      break;
    }
    case 'series': {
      const sid = `${d.service}/${d.metric}`;
      if (!state.series[sid]) state.seriesOrder.push(sid);
      state.series[sid] = { id: sid, ...d, ...meta };
      changed.add('board');
      break;
    }
    case 'hypothesis.update': {
      const id = d.id || key;
      let h = state.hypotheses[id];
      if (!h) {
        h = { id, history: [] };
        state.hypotheses[id] = h;
        state.hypothesisOrder.push(id);
      }
      const prev = h.score;
      Object.assign(h, d, { id, ...meta });
      if (typeof d.score === 'number') h.history.push(d.score);
      if (h.history.length > 40) h.history.shift();
      if (h.delta === undefined && prev !== undefined && typeof d.score === 'number') h.delta = d.score - prev;
      changed.add('board');
      break;
    }
    case 'plan.proposed':
      state.plan = { actions: d.actions || [], ...meta };
      changed.add('board');
      break;
    case 'policy.check': {
      const i = state.policy.findIndex((p) => p.rule === d.rule);
      const item = { ...d, ...meta };
      if (i >= 0) state.policy[i] = item;
      else state.policy.push(item);
      changed.add('board');
      break;
    }
    case 'memory.recalled':
    case 'memory.written':
      state.memory.push({ type: kind === 'memory.recalled' ? 'recalled' : 'written', ...d, ...meta });
      if (state.memory.length > 100) state.memory.shift();
      changed.add('board');
      break;
    case 'metric':
      state.metrics[d.name] = { value: d.value, unit: d.unit, ...meta };
      changed.add('board');
      break;
    case 'handoff':
      state.handoffs.push({ ...d, ...meta });
      pushNote(state, { kind, text: `${meta.agent || '?'} → ${d.to}${d.reason ? ': ' + d.reason : ''}`, ...meta }, changed);
      break;
    case 'artifact':
      state.artifacts.push({ ...d, ...meta });
      changed.add('board');
      break;
    case 'decision':
      pushNote(state, { kind, text: `${d.what}${d.why ? ' — ' + d.why : ''}`, ...meta }, changed);
      break;
    case 'log':
      pushNote(state, { kind, level: d.level, text: d.text, ...meta }, changed);
      break;
    case 'note':
      pushNote(state, { kind, text: d.text, ...meta }, changed);
      break;
    default:
      pushNote(state, { kind, text: JSON.stringify(d).slice(0, 300), ...meta }, changed);
  }
}

function pushNote(state, note, changed) {
  state.notes.push(note);
  changed.add('notes');
}

// ---- selectors --------------------------------------------------------------------------------------------------------

export function rankedHypotheses(state) {
  return state.hypothesisOrder
    .map((id) => state.hypotheses[id])
    .filter((h) => h && typeof h.score === 'number')
    .sort((a, b) => b.score - a.score);
}

export function laneList(state) {
  return state.laneOrder.map((k) => state.lanes[k]);
}

export function laneText(lane) {
  return lane.segments.map((s) => s.text).join('\n\n');
}

export function runningSteps(state) {
  return state.timeline.filter((s) => s.status === 'running');
}

export function isActive(state) {
  return state.status === 'running';
}

/** Parse a stack of messages from a `values` snapshot into chat-ish items (copilot hydration). */
export function messagesFromValues(values) {
  const msgs = values && Array.isArray(values.messages) ? values.messages : [];
  return msgs.map((m, i) => ({
    id: m.id || `m${i}`,
    role: roleOf(m),
    text: contentText(m.content),
    name: m.name || null,
    toolCalls: (m.tool_calls || []).map((tc) => ({ id: tc.id, name: tc.name, args: tc.args })),
    toolCallId: m.tool_call_id || null,
  }));
}

function roleOf(m) {
  const t = String(m.type || m.role || '').toLowerCase();
  if (t.startsWith('human') || t === 'user') return 'user';
  if (t.startsWith('ai') || t === 'assistant') return 'assistant';
  if (t.startsWith('tool')) return 'tool';
  return 'system';
}
