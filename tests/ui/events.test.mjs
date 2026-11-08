import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventFeed, matchEvent, summarizeEvent, normalizeEvent, groupOf, queuePressure } from '../../src/foghorn/server/static/js/core/events.js';

const mk = (o) => normalizeEvent({ id: 'evt_' + Math.random().toString(36).slice(2), type: 'alert.fired', severity: 'high', subject: { component: 'checkout-service' }, payload: {}, ...o });

test('groupOf maps event types to feed groups', () => {
  assert.equal(groupOf('alert.fired'), 'signal');
  assert.equal(groupOf('deploy.finished'), 'change');
  assert.equal(groupOf('approval.requested'), 'lifecycle');
  assert.equal(groupOf('dlq.added'), 'system');
  assert.equal(groupOf('weird.type'), 'other');
});

test('summaries are concise and defensive', () => {
  const a = mk({ payload: { rule: 'error_rate_high', value: 0.071, threshold: 0.02 } });
  assert.match(summarizeEvent(a), /checkout-service error_rate_high 7\.1%/);
  assert.match(summarizeEvent(mk({ type: 'triage.completed', payload: { decision: 'open_incident', severity: 'SEV2' } })), /open_incident SEV2/);
  assert.match(summarizeEvent(mk({ type: 'flag.changed', payload: { flag: 'new_pricing_engine', from: 0, to: 50 } })), /new_pricing_engine 0.50/);
  assert.equal(typeof summarizeEvent(mk({ type: 'mystery', payload: null })), 'string');
  assert.ok(summarizeEvent(mk({ payload: { summary: 'x'.repeat(500) }, type: 'foo' })).length <= 140);
});

test('matchEvent: groups, severity floor, component, text', () => {
  const e = mk({ payload: { rule: 'latency' } });
  assert.ok(matchEvent({ groups: ['signal'] }, e));
  assert.ok(!matchEvent({ groups: ['system'] }, e));
  assert.ok(matchEvent({ minSeverity: 'medium' }, e));
  assert.ok(!matchEvent({ minSeverity: 'critical' }, e));
  assert.ok(matchEvent({ component: 'checkout-service' }, e));
  assert.ok(!matchEvent({ component: 'orders-db' }, e));
  assert.ok(matchEvent({ text: 'LATENCY' }, e));
  assert.ok(!matchEvent({ text: 'nothing like this' }, e));
  assert.ok(matchEvent({ types: ['alert.'] }, e));
  assert.ok(matchEvent(null, e));
});

test('feed: ring-bounded, dedupes by id (SSE reconnect replays), updates occurrence badges', () => {
  const f = new EventFeed(5);
  const e1 = mk({ id: 'evt_1', seq: 1 });
  assert.equal(f.push(e1), 'added');
  assert.equal(f.push({ ...e1 }), 'duplicate');
  assert.equal(f.push({ ...e1, occurrences: 3 }), 'updated');
  assert.equal(f.list()[0].occurrences, 3);
  for (let i = 2; i <= 20; i++) f.push(mk({ id: 'evt_' + i, seq: i }));
  assert.equal(f.ring.size, 5);
  assert.equal(f.maxSeq, 20);
  assert.equal(f.list(null, 3).length, 3);
  assert.equal(f.list()[0].id, 'evt_20'); // newest first
});

test('feed rate per minute', () => {
  const f = new EventFeed(100);
  const now = 1_000_000;
  for (let i = 0; i < 30; i++) f.push(mk({ id: 'e' + i }), now - i * 1000);
  assert.equal(f.ratePerMin(now), 30);
});

test('feed id set stays bounded', () => {
  const f = new EventFeed(10);
  for (let i = 0; i < 1000; i++) f.push(mk({ id: 'x' + i }));
  assert.ok(f.ids.size <= 21);
});

test('queuePressure tones', () => {
  const q = queuePressure({ depth: { P0: 0, P1: 300, P2: 900, P3: 10 } });
  assert.equal(q.P0.tone, 'ok');
  assert.equal(q.P1.tone, 'medium');
  assert.equal(q.P2.tone, 'critical');
});
