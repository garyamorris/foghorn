import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStream, backoffDelay, isRetryableStatus, HttpError } from '../../src/foghorn/server/static/js/core/stream.js';
import { encodeSSE } from '../../src/foghorn/server/static/js/core/sse.js';

const enc = new TextEncoder();
function sseResponse(frames, { status = 200, split = 7, closeAfter = true } = {}) {
  const bytes = enc.encode(frames.join(''));
  let i = 0;
  const body = new ReadableStream({
    pull(ctrl) {
      if (i >= bytes.length) {
        if (closeAfter) ctrl.close();
        return;
      }
      ctrl.enqueue(bytes.slice(i, i + split));
      i += split;
    },
  });
  return new Response(body, { status, headers: { 'content-type': 'text/event-stream' } });
}
const frame = (event, data, id) => encodeSSE({ event, data, id });
const noSleep = () => Promise.resolve();

test('backoffDelay grows exponentially, is capped and jittered within bounds', () => {
  const mid = () => 0.5;
  assert.equal(backoffDelay(1, { base: 500, rand: mid }), 500);
  assert.equal(backoffDelay(2, { base: 500, rand: mid }), 1000);
  assert.equal(backoffDelay(3, { base: 500, rand: mid }), 2000);
  assert.equal(backoffDelay(20, { base: 500, cap: 15000, rand: mid }), 15000);
  assert.equal(backoffDelay(3, { base: 500, rand: () => 0 }), 1500);
  assert.equal(backoffDelay(3, { base: 500, rand: () => 1 }), 2500);
});

test('retryable statuses', () => {
  assert.ok(isRetryableStatus(503) && isRetryableStatus(429) && isRetryableStatus(408));
  assert.ok(!isRetryableStatus(404) && !isRetryableStatus(422) && !isRetryableStatus(401));
});

test('delivers events across arbitrary chunking and stops at a terminal event', async () => {
  const got = [];
  const states = [];
  const h = openStream({
    url: '/x',
    fetchImpl: async () => sseResponse([frame('metadata', { run_id: 'r' }, '1-0'), frame('values', { a: 1 }, '2-0'), frame('end', null, '3-0')]),
    onEvent: (e) => got.push(e),
    onState: (s) => states.push(s),
    isTerminal: (e) => e.event === 'end',
    sleep: noSleep,
  });
  await h.done;
  assert.deepEqual(got.map((e) => e.event), ['metadata', 'values', 'end']);
  assert.equal(h.lastEventId, '3-0');
  assert.deepEqual(states, ['connecting', 'open', 'closed']);
});

test('reconnects after a drop sending Last-Event-ID, and does not re-deliver earlier events', async () => {
  const seenHeaders = [];
  let call = 0;
  const got = [];
  const states = [];
  const h = openStream({
    url: '/stream',
    fetchImpl: async (url, init) => {
      seenHeaders.push({ url, id: init.headers['Last-Event-ID'] });
      call++;
      if (call === 1) {
        // server drops the connection mid-frame after two events
        const r = sseResponse([frame('a', 1, '10-0'), frame('b', 2, '11-0')], { split: 1000 });
        return r;
      }
      return sseResponse([frame('c', 3, '12-0'), frame('end', null, '13-0')]);
    },
    onEvent: (e) => got.push(e.event),
    onState: (s, info) => states.push(s),
    isTerminal: (e) => e.event === 'end',
    sleep: noSleep,
    backoff: { rand: () => 0.5 },
  });
  await h.done;
  assert.deepEqual(got, ['a', 'b', 'c', 'end']);
  assert.equal(seenHeaders[0].id, undefined);
  assert.equal(seenHeaders[1].id, '11-0');
  assert.ok(states.includes('retrying'));
  assert.equal(states.at(-1), 'closed');
});

test('uses buildRequest for the join endpoint on reconnect', async () => {
  let call = 0;
  const urls = [];
  const h = openStream({
    url: '/threads/t/runs/stream',
    method: 'POST',
    body: { assistant_id: 'x' },
    fetchImpl: async (url, init) => {
      urls.push([url, init.method, init.headers['Last-Event-ID']]);
      call++;
      return call === 1 ? sseResponse([frame('metadata', { run_id: 'r1' }, '5-0')]) : sseResponse([frame('end', null, '6-0')]);
    },
    buildRequest: (lastId) => ({ url: '/threads/t/runs/r1/stream', method: 'GET', body: undefined, headers: { 'Last-Event-ID': lastId } }),
    onEvent: () => {},
    isTerminal: (e) => e.event === 'end',
    sleep: noSleep,
  });
  await h.done;
  assert.deepEqual(urls[0], ['/threads/t/runs/stream', 'POST', undefined]);
  assert.deepEqual(urls[1], ['/threads/t/runs/r1/stream', 'GET', '5-0']);
});

test('shouldReconnect=false ends cleanly after a normal close (finished run)', async () => {
  let calls = 0;
  const states = [];
  const h = openStream({
    url: '/x',
    fetchImpl: async () => (calls++, sseResponse([frame('values', {}, '1')])),
    onEvent: () => {},
    onState: (s, i) => states.push([s, i.reason]),
    shouldReconnect: async () => false,
    sleep: noSleep,
  });
  await h.done;
  assert.equal(calls, 1);
  assert.deepEqual(states.at(-1), ['closed', 'finished']);
});

test('retries 5xx with backoff then succeeds; 4xx is fatal and not retried', async () => {
  let n = 0;
  const states = [];
  const h = openStream({
    url: '/x',
    fetchImpl: async () => {
      n++;
      if (n < 3) return new Response('down', { status: 503 });
      return sseResponse([frame('end', null, '1')]);
    },
    onEvent: () => {},
    onState: (s, i) => states.push([s, i.attempt]),
    isTerminal: (e) => e.event === 'end',
    sleep: noSleep,
  });
  await h.done;
  assert.equal(n, 3);
  assert.deepEqual(states.filter((s) => s[0] === 'retrying').map((s) => s[1]), [1, 2]);

  let m = 0;
  let errState;
  const h2 = openStream({
    url: '/x',
    fetchImpl: async () => (m++, new Response('nope', { status: 404 })),
    onEvent: () => {},
    onState: (s, i) => s === 'error' && (errState = i),
    sleep: noSleep,
  });
  await h2.done;
  assert.equal(m, 1);
  assert.ok(errState.error instanceof HttpError);
  assert.equal(errState.error.status, 404);
});

test('gives up after maxRetries on network errors', async () => {
  let n = 0;
  let final;
  const h = openStream({
    url: '/x',
    fetchImpl: async () => {
      n++;
      throw new TypeError('network down');
    },
    onEvent: () => {},
    onState: (s, i) => (final = [s, i.fatal]),
    maxRetries: 3,
    sleep: noSleep,
  });
  await h.done;
  assert.equal(n, 4);
  assert.deepEqual(final, ['error', true]);
});

test('close() stops the loop', async () => {
  let n = 0;
  const h = openStream({
    url: '/x',
    fetchImpl: async () => {
      n++;
      return sseResponse([frame('a', 1, String(n))]);
    },
    onEvent: () => h.close(),
    sleep: noSleep,
  });
  await h.done;
  assert.equal(n, 1);
  assert.equal(h.state, 'closed');
});
