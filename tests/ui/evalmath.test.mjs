import { test } from 'node:test';
import assert from 'node:assert/strict';
import { compareRuns, aggregate, band, progressOf, evaluatorColumns, mean } from '../../src/foghorn/server/static/js/core/evalmath.js';

const run = (cases, extra = {}) => ({ cases: cases.map(([scenario_id, score, passed, tier]) => ({ scenario_id, score, passed, tier })), ...extra });

test('compareRuns flags regressions, improvements, same, and one-sided scenarios', () => {
  const a = run([['S01', 0.9, true, 1], ['S02', 0.5, false, 1], ['S10', 0.95, true, 3], ['S11', 0.7, true, 3]]);
  const b = run([['S01', 0.9, true, 1], ['S02', 0.85, true, 1], ['S10', 0.4, false, 3], ['S12', 0.8, true, 3]]);
  const c = compareRuns(a, b);
  const st = Object.fromEntries(c.rows.map((r) => [r.scenario_id, r.status]));
  assert.deepEqual(st, { S01: 'same', S02: 'regression', S10: 'improvement', S11: 'only-a', S12: 'only-b' });
  assert.equal(c.regressions, 1);
  assert.equal(c.improvements, 1);
  assert.ok(Math.abs(c.rows.find((r) => r.scenario_id === 'S02').delta + 0.35) < 1e-9);
  assert.ok(c.meanDelta !== null);
});

test('a pass -> fail flip is a regression even with a tiny score change', () => {
  const c = compareRuns(run([['S05', 0.79, false]]), run([['S05', 0.8, true]]));
  assert.equal(c.rows[0].status, 'regression');
});

test('rows are ordered naturally (S2 before S10)', () => {
  const c = compareRuns(run([['S10', 1, true], ['S2', 1, true], ['S1', 1, true]]), run([]));
  assert.deepEqual(c.rows.map((r) => r.scenario_id), ['S1', 'S2', 'S10']);
});

test('aggregate by tier averages both sides and deltas', () => {
  const c = compareRuns(run([['S01', 1, true, 1], ['S02', 0.5, true, 1], ['S10', 0.6, true, 3]]), run([['S01', 0.8, true, 1], ['S02', 0.5, true, 1], ['S10', 0.9, true, 3]]));
  const g = aggregate(c.rows, (r) => r.tier);
  assert.deepEqual(g.map((x) => x.key), [1, 3]);
  assert.ok(Math.abs(g[0].delta - 0.1) < 1e-9);
  assert.ok(Math.abs(g[1].delta + 0.3) < 1e-9);
});

test('band, mean, progress, evaluator columns', () => {
  assert.equal(band(0.9), 'good');
  assert.equal(band(0.6), 'ok');
  assert.equal(band(0.1), 'bad');
  assert.equal(band(null), 'none');
  assert.equal(mean([1, null, 3]), 2);
  assert.equal(mean([]), null);
  assert.equal(progressOf({ progress: { done: 5, total: 16 } }), 5 / 16);
  assert.equal(progressOf({ status: 'finished' }), 1);
  assert.deepEqual(evaluatorColumns({ cases: [{ scores: { E02: 1, E01: 0 } }, { scores: { E03: 1 } }] }), ['E01', 'E02', 'E03']);
});
