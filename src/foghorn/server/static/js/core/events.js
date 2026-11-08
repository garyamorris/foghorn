// Event-feed logic: grouping, filtering, one-line summaries, rate math, ring-buffered feed with dedupe. Pure.

import { RingBuffer } from './ring.js';
import { severityRank, truncate, formatNumber, formatPct } from './format.js';

export const EVENT_GROUPS = [
  { key: 'signal', label: 'Signals', types: ['alert.', 'slo.', 'healthcheck.', 'customer.'] },
  { key: 'change', label: 'Changes', types: ['deploy.', 'config.', 'flag.', 'migration.', 'maintenance.', 'change.'] },
  {
    key: 'lifecycle',
    label: 'Lifecycle',
    types: ['triage.', 'incident.', 'run.', 'approval.', 'action.', 'memory.', 'postmortem.', 'eval.', 'handoff.'],
  },
  { key: 'system', label: 'System', types: ['system.', 'event.', 'dlq.', 'replay.'] },
];

export function groupOf(type) {
  const t = String(type || '');
  for (const g of EVENT_GROUPS) if (g.types.some((p) => t.startsWith(p))) return g.key;
  return 'other';
}

export function normalizeEvent(raw, sseId) {
  if (!raw || typeof raw !== 'object') return null;
  const e = { ...raw };
  const seq = Number(e.seq ?? sseId);
  e.seq = Number.isFinite(seq) ? seq : null;
  e.subject = e.subject || {};
  e.payload = e.payload || {};
  e.component = e.component || e.subject.component || null;
  e.severity = e.severity || 'info';
  e.group = groupOf(e.type);
  e.occurrences = e.occurrences || 1;
  return e;
}

/** filter: {groups?:string[], types?:string[], minSeverity?, component?, text?, correlationId?, hideSystem?} */
export function matchEvent(filter, e) {
  if (!filter) return true;
  if (filter.groups && filter.groups.length && !filter.groups.includes(e.group)) return false;
  if (filter.types && filter.types.length && !filter.types.some((t) => e.type === t || e.type.startsWith(t))) return false;
  if (filter.minSeverity && severityRank(e.severity) < severityRank(filter.minSeverity)) return false;
  if (filter.component && e.component !== filter.component) return false;
  if (filter.correlationId && e.correlation_id !== filter.correlationId) return false;
  if (filter.text) {
    const q = String(filter.text).toLowerCase();
    const hay = `${e.type} ${e.component || ''} ${e.id || ''} ${e.correlation_id || ''} ${summarizeEvent(e)}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function p(e, ...keys) {
  for (const k of keys) if (e.payload && e.payload[k] !== undefined && e.payload[k] !== null) return e.payload[k];
  return undefined;
}

/** One-line human summary of an envelope. Defensive: payload shapes vary by producer. */
export function summarizeEvent(e) {
  const c = e.component || '';
  const t = e.type || '';
  let s;
  switch (t) {
    case 'alert.fired':
    case 'alert.resolved': {
      const rule = p(e, 'rule', 'alertname', 'name');
      const val = p(e, 'value');
      const thr = p(e, 'threshold');
      s = `${c} ${rule || 'alert'}`;
      if (typeof val === 'number') s += ` ${/rate|ratio/.test(String(rule)) && val <= 1 ? formatPct(val, 1) : formatNumber(val, val < 10 ? 2 : 0)}`;
      if (typeof thr === 'number' && typeof val === 'number') s += ` (>${/rate|ratio/.test(String(rule)) && thr <= 1 ? formatPct(thr, 1) : thr})`;
      break;
    }
    case 'deploy.started':
    case 'deploy.finished':
    case 'deploy.rolled_back':
      s = `${c} ${p(e, 'version', 'revision', 'sha') || ''}`.trim();
      break;
    case 'flag.changed':
      s = `flag ${p(e, 'flag', 'name') || ''} ${p(e, 'from', 'old') ?? ''}→${p(e, 'to', 'new', 'value') ?? ''}`.trim();
      break;
    case 'triage.completed':
      s = `triage → ${p(e, 'decision') || '?'}${p(e, 'severity') ? ' ' + p(e, 'severity') : ''}`;
      break;
    case 'incident.opened':
    case 'incident.updated':
    case 'incident.resolved':
      s = `${p(e, 'incident_id', 'id') || ''} ${p(e, 'title') || ''} ${p(e, 'severity') || ''}`.trim();
      break;
    case 'run.started':
    case 'run.interrupted':
    case 'run.finished':
    case 'run.failed':
      s = `${p(e, 'graph_id', 'graph') || 'run'}${p(e, 'status') ? ' ' + p(e, 'status') : ''}${p(e, 'error') ? ': ' + p(e, 'error') : ''}`;
      break;
    case 'approval.requested':
    case 'approval.decided':
      s = `${p(e, 'action', 'title') || p(e, 'kind') || 'approval'}${p(e, 'decision') ? ' → ' + p(e, 'decision') : ''}`;
      break;
    case 'action.executed':
    case 'action.verified':
      s = `${p(e, 'action') || ''} ${p(e, 'target') || ''}${p(e, 'verified') !== undefined ? (p(e, 'verified') ? ' verified' : ' not recovered') : ''}`.trim();
      break;
    case 'event.suppressed':
      s = `suppressed ${p(e, 'dedupe_key') || ''} x${p(e, 'occurrences') || e.occurrences || 2}`;
      break;
    case 'event.shed':
      s = `shed ${p(e, 'type') || ''} (${p(e, 'reason') || 'backpressure'})`;
      break;
    case 'dlq.added':
      s = `${p(e, 'reason') || 'dead letter'}${p(e, 'event_id') ? ' ' + p(e, 'event_id') : ''}`;
      break;
    case 'system.cron.tick':
      s = `cron ${p(e, 'job') || ''}`;
      break;
    default:
      s = p(e, 'summary', 'message', 'title', 'text') || (c ? c : '');
  }
  return truncate(String(s || '').replace(/\s+/g, ' ').trim() || t, 140);
}

/** Tone class for a severity / event. */
export function toneOfEvent(e) {
  const s = String(e.severity || 'info').toLowerCase();
  if (e.type === 'run.failed' || e.type === 'dlq.added') return 'critical';
  return { critical: 'critical', high: 'high', medium: 'medium', low: 'low', info: 'info' }[s] || 'info';
}

/** Ring-buffered feed with id/seq dedupe (reconnect replays must not duplicate rows). */
export class EventFeed {
  constructor(capacity = 500) {
    this.ring = new RingBuffer(capacity);
    this.ids = new Set();
    this.maxSeq = 0;
    this.stamps = new RingBuffer(2000); // receipt times for rate math
  }

  /** Returns 'added' | 'updated' | 'duplicate'. */
  push(evt, nowMs = Date.now()) {
    if (!evt) return 'duplicate';
    const key = evt.id || (evt.seq != null ? `seq:${evt.seq}` : null);
    if (key && this.ids.has(key)) {
      // coalesced duplicate counters update in place (the "x3" badge)
      const existing = this.ring.find((x) => (x.id || `seq:${x.seq}`) === key);
      if (existing && (evt.occurrences || 1) > (existing.occurrences || 1)) {
        existing.occurrences = evt.occurrences;
        return 'updated';
      }
      return 'duplicate';
    }
    if (key) {
      this.ids.add(key);
      if (this.ids.size > this.ring.capacity * 2) {
        // keep the id set bounded: rebuild from the ring
        this.ids = new Set(this.ring.toArray().map((x) => x.id || `seq:${x.seq}`));
        this.ids.add(key);
      }
    }
    if (evt.seq != null && evt.seq > this.maxSeq) this.maxSeq = evt.seq;
    this.ring.push(evt);
    this.stamps.push(nowMs);
    return 'added';
  }

  /** newest first */
  list(filter, limit = 200) {
    const out = [];
    for (let i = this.ring.length - 1; i >= 0 && out.length < limit; i--) {
      const e = this.ring.at(i);
      if (matchEvent(filter, e)) out.push(e);
    }
    return out;
  }

  /** events per minute over the trailing window. */
  ratePerMin(nowMs = Date.now(), windowMs = 60000) {
    let n = 0;
    for (let i = this.stamps.length - 1; i >= 0; i--) {
      if (nowMs - this.stamps.at(i) <= windowMs) n++;
      else break;
    }
    return Math.round((n * 60000) / windowMs);
  }

  clear() {
    this.ring.clear();
    this.ids.clear();
    this.stamps.clear();
  }
}

/** Queue backpressure ratio and tone from a foghorn.queue payload. */
export function queuePressure(q) {
  const depth = q?.depth || {};
  const caps = q?.caps || { P0: 500, P1: 500, P2: 1000, P3: 200 };
  const out = {};
  for (const k of ['P0', 'P1', 'P2', 'P3']) {
    const d = depth[k] ?? 0;
    const cap = caps[k] || 1;
    out[k] = { depth: d, cap, ratio: d / cap, tone: d / cap >= 0.8 ? 'critical' : d / cap >= 0.5 ? 'medium' : 'ok' };
  }
  return out;
}
