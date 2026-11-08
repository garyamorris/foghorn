import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseHash, buildHash, matchRoute, link } from '../../src/foghorn/server/static/js/core/router.js';

test('parseHash: path, segments, query', () => {
  const p = parseHash('#/incidents/INC-D0003?thread=abc&tab=raw%20stream');
  assert.equal(p.path, '/incidents/INC-D0003');
  assert.deepEqual(p.segments, ['incidents', 'INC-D0003']);
  assert.deepEqual(p.query, { thread: 'abc', tab: 'raw stream' });
});

test('parseHash: empty, root, trailing slash, no leading slash', () => {
  assert.deepEqual(parseHash('').segments, []);
  assert.deepEqual(parseHash('#').segments, []);
  assert.deepEqual(parseHash('#/').segments, []);
  assert.deepEqual(parseHash('#/graphs/').segments, ['graphs']);
  assert.deepEqual(parseHash('#graphs/x').segments, ['graphs', 'x']);
});

test('parseHash tolerates malformed escapes', () => {
  assert.deepEqual(parseHash('#/a/%E0%A4%A').segments, ['a', '%E0%A4%A']);
});

test('buildHash and link encode segments', () => {
  assert.equal(buildHash('/graphs/x', { a: '1 2', b: '', c: undefined }), '#/graphs/x?a=1%202');
  assert.equal(link('threads', 'a/b', 'history'), '#/threads/a%2Fb/history');
  const p = parseHash(link('memory', 'prefs', 'team payments'));
  assert.deepEqual(p.segments, ['memory', 'prefs', 'team payments']);
});

test('matchRoute: params, exact length, greedy tail, first match wins', () => {
  const routes = [
    { pattern: '/', name: 'home' },
    { pattern: '/incidents', name: 'list' },
    { pattern: '/incidents/:id', name: 'detail' },
    { pattern: '/datasets/:name/:rec', name: 'rec' },
    { pattern: '/memory/*rest', name: 'mem' },
  ];
  const m = (h) => matchRoute(routes, parseHash(h));
  assert.equal(m('#/').route.name, 'home');
  assert.equal(m('#/incidents').route.name, 'list');
  assert.deepEqual(m('#/incidents/INC-1').params, { id: 'INC-1' });
  assert.equal(m('#/incidents/INC-1/extra'), null);
  assert.deepEqual(m('#/datasets/scenarios/S10').params, { name: 'scenarios', rec: 'S10' });
  assert.equal(m('#/memory').route.name, 'mem');
  assert.equal(m('#/memory/prefs/team-payments').params.rest, 'prefs/team-payments');
  assert.equal(m('#/nope'), null);
});
