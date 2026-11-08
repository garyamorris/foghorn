// Resilient SSE client: fetch + ReadableStream (so POST bodies, custom headers and Last-Event-ID all work), with
// exponential backoff + jitter, idle watchdog, resume via Last-Event-ID or a caller-supplied join request.
// No DOM; `fetchImpl`, timers and RNG are injectable so the logic is unit-testable in Node.

import { SSEParser } from './sse.js';

export class HttpError extends Error {
  constructor(status, body, url) {
    super(`HTTP ${status}${body ? ': ' + String(body).slice(0, 200) : ''}`);
    this.name = 'HttpError';
    this.status = status;
    this.body = body;
    this.url = url;
  }
}

/** Exponential backoff with +-jitter. attempt starts at 1. */
export function backoffDelay(attempt, { base = 500, cap = 15000, factor = 2, jitter = 0.25, rand = Math.random } = {}) {
  const raw = Math.min(cap, base * Math.pow(factor, Math.max(0, attempt - 1)));
  const spread = raw * jitter;
  return Math.max(0, Math.round(raw - spread + rand() * spread * 2));
}

export function isRetryableStatus(status) {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

/**
 * Open a reconnecting event stream.
 *
 * @param {object} o
 * @param {string} o.url
 * @param {string} [o.method]
 * @param {object} [o.headers]
 * @param {string|object} [o.body]  objects are JSON-encoded
 * @param {string} [o.lastEventId]
 * @param {(ev)=>void} o.onEvent                 receives {event,data,id}
 * @param {(state:string, info:object)=>void} [o.onState]  connecting|open|retrying|closed|error
 * @param {(res:Response)=>void} [o.onOpen]
 * @param {boolean} [o.reconnect=true]           reconnect after the server closes the stream
 * @param {(info)=>Promise<boolean>|boolean} [o.shouldReconnect] consulted after a CLEAN close (e.g. is the run still active?)
 * @param {(ev)=>boolean} [o.isTerminal]          stop for good when this returns true
 * @param {(lastId:string, attempt:number)=>object} [o.buildRequest] override url/method/headers/body when reconnecting
 */
export function openStream(o) {
  const f = o.fetchImpl || ((...a) => globalThis.fetch(...a));
  const sleepImpl = o.sleep || ((ms, signalFn) => defaultSleep(ms, signalFn));
  const maxRetries = o.maxRetries ?? Infinity;
  const reconnect = o.reconnect !== false;
  const parser = new SSEParser({ onComment: o.onComment });
  let lastId = o.lastEventId || '';
  let state = 'idle';
  let closed = false;
  let ctrl = null;
  let wake = null;
  const stats = { attempts: 0, events: 0, connects: 0, lastActivity: 0 };

  const setState = (s, info = {}) => {
    state = s;
    if (o.onState) {
      try {
        o.onState(s, info);
      } catch {
        /* a UI callback must never kill the stream */
      }
    }
  };

  const done = (async function run() {
    let attempt = 0;
    while (!closed) {
      ctrl = new AbortController();
      if (o.signal) {
        if (o.signal.aborted) break;
        o.signal.addEventListener('abort', () => ctrl.abort(), { once: true });
      }
      let sawTerminal = false;
      let error = null;
      let idleTimer = null;
      let idleFired = false;
      const armIdle = () => {
        if (!o.idleTimeoutMs) return;
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          idleFired = true;
          ctrl.abort();
        }, o.idleTimeoutMs);
      };
      try {
        setState('connecting', { attempt });
        stats.attempts++;
        const req = attempt > 0 && o.buildRequest ? o.buildRequest(lastId, attempt) : {};
        const url = req.url || o.url;
        const method = req.method || o.method || 'GET';
        const headers = { Accept: 'text/event-stream', ...(o.headers || {}), ...(req.headers || {}) };
        let body = req.body !== undefined ? req.body : o.body;
        if (body && typeof body === 'object' && !(body instanceof ArrayBuffer) && typeof body !== 'string') {
          body = JSON.stringify(body);
          headers['Content-Type'] = headers['Content-Type'] || 'application/json';
        }
        if (lastId && !req.headers?.['Last-Event-ID']) headers['Last-Event-ID'] = lastId;
        const res = await f(url, { method, headers, body, signal: ctrl.signal, cache: 'no-store' });
        if (!res.ok) {
          let text = '';
          try {
            text = await res.text();
          } catch {
            /* ignore */
          }
          throw new HttpError(res.status, text, url);
        }
        stats.connects++;
        setState('open', { attempt });
        if (o.onOpen) o.onOpen(res);
        attempt = 0;
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        armIdle();
        for (;;) {
          const { value, done: eof } = await reader.read();
          if (eof) break;
          stats.lastActivity = Date.now();
          armIdle();
          const evs = parser.push(dec.decode(value, { stream: true }));
          for (const ev of evs) {
            if (ev.id !== undefined && ev.id !== '') lastId = ev.id;
            stats.events++;
            if (o.onEvent) o.onEvent(ev);
            if (o.isTerminal && o.isTerminal(ev)) sawTerminal = true;
          }
          if (closed) break;
        }
        for (const ev of parser.flush()) {
          if (o.onEvent) o.onEvent(ev);
        }
      } catch (e) {
        if (closed) break;
        if (idleFired) error = new Error('stream idle timeout');
        else if (e && e.name === 'AbortError') {
          if (o.signal?.aborted) break;
          error = e;
        } else error = e;
      } finally {
        if (idleTimer) clearTimeout(idleTimer);
      }
      parser.reset();
      if (closed) break;
      if (sawTerminal) {
        setState('closed', { reason: 'terminal', lastEventId: lastId });
        break;
      }
      if (error instanceof HttpError && !isRetryableStatus(error.status)) {
        setState('error', { error, fatal: true });
        break;
      }
      if (!error) {
        if (!reconnect) {
          setState('closed', { reason: 'eof', lastEventId: lastId });
          break;
        }
        if (o.shouldReconnect) {
          let again = true;
          try {
            again = await o.shouldReconnect({ lastEventId: lastId });
          } catch {
            again = true;
          }
          if (!again) {
            setState('closed', { reason: 'finished', lastEventId: lastId });
            break;
          }
        }
      } else if (!reconnect) {
        setState('error', { error, fatal: true });
        break;
      }
      attempt++;
      if (attempt > maxRetries) {
        setState('error', { error: error || new Error('retries exhausted'), fatal: true });
        break;
      }
      const delay = backoffDelay(attempt, o.backoff);
      setState('retrying', { attempt, delay, error, lastEventId: lastId });
      await new Promise((resolve) => {
        wake = resolve;
        sleepImpl(delay, () => closed).then(resolve);
      });
      wake = null;
    }
    if (closed && state !== 'closed') setState('closed', { reason: 'closed', lastEventId: lastId });
  })();

  return {
    close() {
      closed = true;
      if (ctrl) ctrl.abort();
      if (wake) wake();
    },
    get state() {
      return state;
    },
    get lastEventId() {
      return lastId;
    },
    stats,
    done,
  };
}

function defaultSleep(ms, isCancelled) {
  return new Promise((resolve) => {
    const step = Math.min(ms, 250);
    let waited = 0;
    const tick = () => {
      if (isCancelled() || waited >= ms) return resolve();
      waited += step;
      setTimeout(tick, step);
    };
    tick();
  });
}
