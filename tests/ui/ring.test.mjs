import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RingBuffer } from '../../src/foghorn/server/static/js/core/ring.js';

test('keeps only the newest `capacity` items and counts drops', () => {
  const r = new RingBuffer(3);
  for (let i = 1; i <= 5; i++) r.push(i);
  assert.deepEqual(r.toArray(), [3, 4, 5]);
  assert.equal(r.size, 3);
  assert.equal(r.dropped, 2);
  assert.equal(r.total, 5);
  assert.equal(r.first(), 3);
  assert.equal(r.last(), 5);
  assert.equal(r.at(-2), 4);
  assert.equal(r.at(3), undefined);
});

test('is bounded under heavy push (no unbounded growth)', () => {
  const r = new RingBuffer(100);
  for (let i = 0; i < 100000; i++) r.push({ i });
  assert.equal(r.size, 100);
  assert.equal(r.items.length, 100);
  assert.equal(r.last().i, 99999);
});

test('iteration, filter, find, clear', () => {
  const r = new RingBuffer(4).pushAll([1, 2, 3, 4, 5, 6]);
  assert.deepEqual([...r], [3, 4, 5, 6]);
  assert.deepEqual(r.filter((x) => x % 2 === 0), [4, 6]);
  assert.equal(r.find((x) => x > 4), 5);
  r.clear();
  assert.equal(r.size, 0);
  assert.deepEqual(r.toArray(), []);
});

test('rejects invalid capacity', () => {
  assert.throws(() => new RingBuffer(0), RangeError);
  assert.throws(() => new RingBuffer(1.5), RangeError);
});
