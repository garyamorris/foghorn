import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as c from '../../src/foghorn/server/static/js/core/chartmath.js';

test('extent handles empty, flat and nulls', () => {
  assert.deepEqual(c.extent([]), [0, 1]);
  assert.deepEqual(c.extent([3, 1, null, 2]), [1, 3]);
  const [lo, hi] = c.extent([5, 5]);
  assert.ok(lo < 5 && hi > 5);
});

test('linearScale maps and inverts', () => {
  const s = c.linearScale(0, 10, 100, 200);
  assert.equal(s(0), 100);
  assert.equal(s(10), 200);
  assert.equal(s(5), 150);
  assert.equal(s.invert(150), 5);
  assert.equal(c.linearScale(1, 1, 0, 10)(1), 5);
});

test('niceTicks gives round steps inside the domain', () => {
  assert.deepEqual(c.niceTicks(0, 100, 5), [0, 20, 40, 60, 80, 100]);
  const t = c.niceTicks(0.13, 0.87, 4);
  assert.ok(t.every((v) => v >= 0.13 && v <= 0.87));
  assert.deepEqual(c.niceTicks(3, 3), [3]);
});

test('normalizePoints accepts [t,v], {t,v} and ISO strings; sorts', () => {
  const pts = c.normalizePoints([
    ['2026-11-05T10:02:00Z', 3],
    ['2026-11-05T10:01:00Z', 2],
    { t: Date.parse('2026-11-05T10:03:00Z'), v: '4' },
    ['bad', 1],
    null,
  ]);
  assert.deepEqual(pts.map((p) => p.v), [2, 3, 4]);
  assert.ok(pts[0].t < pts[1].t);
});

test('downsample preserves spikes and is bounded', () => {
  const pts = Array.from({ length: 1000 }, (_, i) => ({ t: i, v: i === 500 ? 9999 : 1 }));
  const d = c.downsample(pts, 100);
  assert.ok(d.length <= 110);
  assert.ok(d.some((p) => p.v === 9999));
  assert.equal(c.downsample(pts.slice(0, 10), 100).length, 10);
});

test('lineLayout: path, baseline, onset and last point', () => {
  const raw = Array.from({ length: 10 }, (_, i) => [1000 * i, i * 10]);
  const l = c.lineLayout(raw, { width: 100, height: 40, baseline: 20, onset: 4000, padRatio: 0 });
  assert.match(l.path, /^M/);
  assert.equal(l.pts.length, 10);
  assert.equal(l.coords[0][0], 2);
  assert.equal(l.coords[9][0], 98);
  assert.ok(l.onsetX > 2 && l.onsetX < 98);
  assert.ok(l.baselineY > 2 && l.baselineY < 38);
  assert.equal(l.last.v, 90);
  // y grows downward: the max value is at the top
  assert.ok(l.coords[9][1] < l.coords[0][1]);
  assert.ok(l.area.endsWith('Z'));
});

test('lineLayout: empty and single point do not throw', () => {
  assert.equal(c.lineLayout([]).empty, true);
  const one = c.lineLayout([[0, 5]], { width: 50, height: 20 });
  assert.equal(one.pts.length, 1);
  assert.ok(Number.isFinite(one.coords[0][0]));
});

test('bar/segment layouts', () => {
  const b = c.barLayout([{ value: 0.5 }, { value: 1 }], { width: 100 });
  assert.deepEqual(b.map((x) => x.w), [50, 100]);
  const s = c.segmentLayout([{ value: 1 }, { value: 3 }], 100);
  assert.equal(s[0].w, 25);
  assert.equal(s[1].x, 25);
});

test('arcPath, heatLevel, zScore', () => {
  assert.match(c.arcPath(50, 50, 40, 0.5), /^M/);
  assert.equal(c.heatLevel(0.1), 1);
  assert.equal(c.heatLevel(0.9), 4);
  assert.equal(c.heatLevel(NaN), 0);
  const flat = Array.from({ length: 20 }, (_, i) => [i, 1 + (i % 2) * 0.01]);
  flat.push([20, 50]);
  assert.ok(c.zScore(flat) > 5);
});
