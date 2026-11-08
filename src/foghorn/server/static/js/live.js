// Shared live connection to the Foghorn bus (GET /api/stream). One stream feeds the top bar, Mission Control,
// the Approvals badge and the toasts. Backfills history first, then resumes by `since`/Last-Event-ID.

import { api, openBusStream } from './api.js';
import { EventFeed, normalizeEvent } from './core/events.js';
import { decodeData } from './core/sse.js';
import { createEmitter } from './core/store.js';
import { parseIso } from './core/format.js';

export function createLive() {
  const bus = createEmitter();
  const feed = new EventFeed(600);
  const state = {
    status: 'connecting', // connecting | open | retrying | closed | error
    statusInfo: {},
    queue: null,
    clock: null,
    clockAt: 0,
    summary: null,
    lastSeq: 0,
    paused: false,
    gaps: 0,
    dispatches: new Map(),
  };
  let handle = null;
  let summaryTimer = null;

  async function backfill() {
    try {
      const res = await api.events({ limit: 150, order: 'desc' });
      const items = (res.items || []).slice().reverse();
      for (const raw of items) {
        const e = normalizeEvent(raw);
        if (e && feed.push(e) === 'added') bus.emit('event', { event: e, backfill: true });
      }
      state.lastSeq = Math.max(state.lastSeq, feed.maxSeq, res.latest_seq || 0);
      bus.emit('backfill', null);
    } catch (e) {
      bus.emit('backfill-error', e);
    }
  }

  async function refreshSummary() {
    try {
      state.summary = await api.summary();
      bus.emit('summary', state.summary);
    } catch {
      /* summary is optional: tiles fall back to derived counts */
    }
  }

  function onFrame(ev) {
    const data = decodeData(ev);
    switch (ev.event) {
      case 'foghorn.hello':
        bus.emit('hello', data);
        break;
      case 'foghorn.event': {
        const e = normalizeEvent(data, ev.id);
        if (!e) return;
        const r = feed.push(e);
        if (e.seq && e.seq > state.lastSeq) state.lastSeq = e.seq;
        if (r !== 'duplicate') bus.emit('event', { event: e, backfill: false, updated: r === 'updated' });
        break;
      }
      case 'foghorn.dispatch':
        if (data && data.id) {
          state.dispatches.set(data.id, data);
          if (state.dispatches.size > 400) state.dispatches.delete(state.dispatches.keys().next().value);
          bus.emit('dispatch', data);
        }
        break;
      case 'foghorn.queue':
        state.queue = data;
        bus.emit('queue', data);
        break;
      case 'foghorn.clock':
        state.clock = data;
        state.clockAt = Date.now();
        bus.emit('clock', data);
        break;
      case 'foghorn.gap':
        state.gaps++;
        bus.emit('gap', data);
        backfill();
        break;
      default:
        break;
    }
  }

  return {
    bus,
    feed,
    state,
    on: bus.on,
    async start() {
      if (handle) return;
      await backfill();
      refreshSummary();
      summaryTimer = setInterval(refreshSummary, 15000);
      handle = openBusStream({
        since: state.lastSeq || undefined,
        onEvent: onFrame,
        onState: (s, info) => {
          state.status = s;
          state.statusInfo = info || {};
          bus.emit('status', { status: s, info });
        },
      });
    },
    stop() {
      if (handle) handle.close();
      handle = null;
      clearInterval(summaryTimer);
    },
    refreshSummary,
    /** Interpolated virtual "now" (ms epoch) between 1 s clock frames. */
    virtualNow() {
      const c = state.clock;
      if (!c || !c.virtual_now) return NaN;
      const base = parseIso(c.virtual_now);
      if (c.paused || !c.speed) return base;
      return base + (Date.now() - state.clockAt) * c.speed;
    },
  };
}
