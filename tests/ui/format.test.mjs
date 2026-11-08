import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as f from '../../src/foghorn/server/static/js/core/format.js';

test('numbers', () => {
  assert.equal(f.formatNumber(1234567.891, 1), '1,234,567.9');
  assert.equal(f.formatNumber(-1234), '-1,234');
  assert.equal(f.formatNumber(null), '-');
  assert.equal(f.formatCompact(1500), '1.5k');
  assert.equal(f.formatCompact(2_000_000), '2M');
  assert.equal(f.formatCompact(12), '12');
  assert.equal(f.formatPct(0.071), '7.1%');
  assert.equal(f.formatPct(0.5), '50%');
  assert.equal(f.formatBytes(1536), '1.5 KB');
  assert.equal(f.formatBytes(10 * 1024 * 1024), '10 MB');
});

test('durations', () => {
  assert.equal(f.formatDuration(0.2), '<1 ms');
  assert.equal(f.formatDuration(850), '850 ms');
  assert.equal(f.formatDuration(1234), '1.2 s');
  assert.equal(f.formatDuration(42_000), '42 s');
  assert.equal(f.formatDuration(61_000), '1m 01s');
  assert.equal(f.formatDuration(3_700_000), '1h 01m');
  assert.equal(f.formatDuration(NaN), '-');
});

test('relative time', () => {
  const now = 1_000_000_000_000;
  assert.equal(f.relativeTime(now - 2000, now), 'just now');
  assert.equal(f.relativeTime(now - 45_000, now), '45s ago');
  assert.equal(f.relativeTime(now - 5 * 60_000, now), '5m ago');
  assert.equal(f.relativeTime(now - 3 * 3600_000, now), '3h ago');
  assert.equal(f.relativeTime(now + 30_000, now), 'in 30s');
});

test('virtual clock formatting is UTC and locale independent', () => {
  assert.equal(f.formatClock('2026-11-05T10:49:12Z'), '10:49:12Z');
  assert.equal(f.formatClock('2026-11-05T10:49:12Z', false), '10:49Z');
  assert.equal(f.formatDateTime('2026-11-05T10:49:12Z'), '2026-11-05 10:49:12Z');
  assert.equal(f.formatClock('garbage'), '-');
});

test('strings and ids', () => {
  assert.equal(f.shortId('6c1f2d1e-aaaa-bbbb'), '6c1f2d1e');
  assert.equal(f.shortId('abc'), 'abc');
  assert.equal(f.truncate('abcdefghij', 5), 'abcd…');
  assert.equal(f.titleCase('approval.requested'), 'Approval Requested');
  assert.equal(f.hashString('a'), f.hashString('a'));
  assert.notEqual(f.hashString('a'), f.hashString('b'));
  assert.ok(f.hueFor('x') >= 0 && f.hueFor('x') < 360);
});

test('metrics and severity', () => {
  assert.equal(f.formatMetric('error_rate', 0.071), '7.1%');
  assert.equal(f.formatMetric('p95_ms', 1384), '1,384 ms');
  assert.equal(f.formatMetric('cpu_pct', 55.56), '55.6%');
  assert.ok(f.severityRank('critical') > f.severityRank('high'));
  assert.equal(f.severityRank('nope'), -1);
});

test('safeJson survives cycles and bigint', () => {
  const a = { n: 1n };
  a.self = a;
  const s = f.safeJson(a);
  assert.match(s, /\[Circular\]/);
  assert.match(s, /"1"/);
});
