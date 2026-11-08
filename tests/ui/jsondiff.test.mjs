import { test } from 'node:test';
import assert from 'node:assert/strict';
import { diffJson, buildStateUpdate, forkInfo, checkpointId } from '../../src/foghorn/server/static/js/core/jsondiff.js';

test('diff: added, removed, changed at any depth', () => {
  const a = { incident: { id: 'INC-1', severity: 'SEV3' }, flags: [], gone: 1 };
  const b = { incident: { id: 'INC-1', severity: 'SEV2' }, flags: [], fresh: true };
  const d = diffJson(a, b);
  assert.deepEqual(d.map((x) => [x.path, x.kind]), [['fresh', 'added'], ['gone', 'removed'], ['incident.severity', 'changed']]);
  assert.equal(d[2].before, 'SEV3');
  assert.equal(d[2].after, 'SEV2');
});

test('diff: arrays of objects with ids match by id, not position', () => {
  const a = { hypotheses: [{ id: 'h1', score: 0.2 }, { id: 'h2', score: 0.5 }] };
  const b = { hypotheses: [{ id: 'h2', score: 0.5 }, { id: 'h1', score: 0.9 }] };
  const d = diffJson(a, b);
  assert.deepEqual(d.map((x) => x.path), ['hypotheses[id=h1].score']);
});

test('diff: plain arrays compare by index; identical values produce nothing', () => {
  assert.deepEqual(diffJson({ a: [1, 2, 3] }, { a: [1, 2] }).map((x) => [x.path, x.kind]), [['a[2]', 'removed']]);
  assert.deepEqual(diffJson({ a: { b: [1, { c: 2 }] } }, { a: { b: [1, { c: 2 }] } }), []);
  assert.deepEqual(diffJson(null, null), []);
  assert.equal(diffJson(1, 2).length, 1);
});

test('buildStateUpdate sends only changed top-level keys and wraps replaced lists in Overwrite', () => {
  const orig = { severity: 'SEV3', hypotheses: [{ id: 'h1' }], applied_actions: [{ a: 1 }], title: 't' };
  const edited = { severity: 'SEV2', hypotheses: [{ id: 'h2' }], applied_actions: [{ a: 1 }], title: 't' };
  const u = buildStateUpdate(orig, edited);
  assert.deepEqual(Object.keys(u.values), ['severity', 'hypotheses']);
  assert.deepEqual(u.values.hypotheses, { type: '__overwrite__', value: [{ id: 'h2' }] });
  assert.equal(u.values.severity, 'SEV2');
  const raw = buildStateUpdate(orig, edited, { overwriteLists: false });
  assert.deepEqual(raw.values.hypotheses, [{ id: 'h2' }]);
  assert.ok(raw.notes.some((n) => /reducer/.test(n)));
});

test('buildStateUpdate notes removed keys and ignores no-ops', () => {
  const u = buildStateUpdate({ a: 1, b: 2 }, { a: 1 });
  assert.deepEqual(u.values, {});
  assert.ok(u.notes.some((n) => n.startsWith('b:')));
});

test('forkInfo marks checkpoints that have more than one child', () => {
  const hist = [
    { checkpoint: { checkpoint_id: 'c3' }, parent_checkpoint: { checkpoint_id: 'c2' } },
    { checkpoint: { checkpoint_id: 'c3b' }, parent_checkpoint: { checkpoint_id: 'c2' } },
    { checkpoint: { checkpoint_id: 'c2' }, parent_checkpoint: { checkpoint_id: 'c1' } },
    { checkpoint: { checkpoint_id: 'c1' }, parent_checkpoint: null },
  ];
  const { forks } = forkInfo(hist);
  assert.deepEqual([...forks], ['c2']);
  assert.equal(checkpointId(hist[0]), 'c3');
});
