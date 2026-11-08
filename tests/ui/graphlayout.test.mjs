import { test } from 'node:test';
import assert from 'node:assert/strict';
import { layoutGraph, collapseGroups, groupsIn, groupOfNode, nodeTags } from '../../src/foghorn/server/static/js/core/graphlayout.js';

const linear = {
  nodes: [{ id: '__start__' }, { id: 'a' }, { id: 'b' }, { id: 'c' }, { id: '__end__' }],
  edges: [
    { source: '__start__', target: 'a' },
    { source: 'a', target: 'b' },
    { source: 'a', target: 'c', conditional: true },
    { source: 'b', target: '__end__' },
    { source: 'c', target: '__end__' },
  ],
};

test('layers go top to bottom and siblings share a layer', () => {
  const l = layoutGraph(linear);
  const n = Object.fromEntries(l.nodes.map((x) => [x.id, x]));
  assert.ok(n.__start__.y < n.a.y);
  assert.ok(n.a.y < n.b.y);
  assert.equal(n.b.y, n.c.y);
  assert.ok(n.b.y < n.__end__.y);
  assert.notEqual(n.b.x, n.c.x);
  assert.equal(l.edges.length, 5);
  assert.ok(l.edges.every((e) => e.path.startsWith('M')));
  assert.ok(l.edges.find((e) => e.target === 'c').conditional);
});

test('no node overlaps within a layer', () => {
  const g = { nodes: [{ id: '__start__' }, ...'abcdef'.split('').map((id) => ({ id })), { id: '__end__' }], edges: [...'abcdef'.split('').flatMap((id) => [{ source: '__start__', target: id }, { source: id, target: '__end__' }])] };
  const l = layoutGraph(g);
  const row = l.nodes.filter((n) => n.y === l.nodes.find((x) => x.id === 'a').y).sort((a, b) => a.x - b.x);
  for (let i = 1; i < row.length; i++) assert.ok(row[i].x >= row[i - 1].x + row[i - 1].w);
});

test('cycles are handled: back edges are flagged and routed, layout terminates', () => {
  const g = {
    nodes: [{ id: '__start__' }, { id: 'plan' }, { id: 'act' }, { id: 'verify' }, { id: '__end__' }],
    edges: [
      { source: '__start__', target: 'plan' },
      { source: 'plan', target: 'act' },
      { source: 'act', target: 'verify' },
      { source: 'verify', target: 'plan', conditional: true },
      { source: 'verify', target: '__end__', conditional: true },
    ],
  };
  const l = layoutGraph(g);
  const back = l.edges.filter((e) => e.back);
  assert.equal(back.length, 1);
  assert.equal(back[0].source, 'verify');
  const n = Object.fromEntries(l.nodes.map((x) => [x.id, x]));
  assert.ok(n.plan.y < n.verify.y);
});

test('long edges get routed through intermediate layers', () => {
  const g = { nodes: [{ id: 's' }, { id: 'a' }, { id: 'b' }, { id: 'c' }], edges: [{ source: 's', target: 'a' }, { source: 'a', target: 'b' }, { source: 'b', target: 'c' }, { source: 's', target: 'c' }] };
  const l = layoutGraph(g);
  const long = l.edges.find((e) => e.source === 's' && e.target === 'c');
  assert.ok(long.points.length >= 4);
});

test('xray subgraph groups: boxes when expanded, single node when collapsed', () => {
  const g = {
    nodes: [{ id: '__start__' }, { id: 'worker' }, { id: 'review:critique' }, { id: 'review:polish' }, { id: 'summarize' }, { id: '__end__' }],
    edges: [
      { source: '__start__', target: 'worker' },
      { source: 'worker', target: 'review:critique' },
      { source: 'review:critique', target: 'review:polish' },
      { source: 'review:polish', target: 'summarize' },
      { source: 'summarize', target: '__end__' },
    ],
  };
  assert.deepEqual(groupsIn(g), ['review']);
  assert.equal(groupOfNode('review:critique'), 'review');
  const open = layoutGraph(g);
  assert.equal(open.groups.length, 1);
  assert.equal(open.groups[0].id, 'review');
  const crit = open.nodes.find((n) => n.id === 'review:critique');
  assert.ok(crit.x >= open.groups[0].x && crit.x + crit.w <= open.groups[0].x + open.groups[0].w);
  assert.equal(crit.label, 'critique');

  const col = collapseGroups(g, ['review']);
  assert.equal(col.nodes.length, 5);
  assert.deepEqual(col.edges.map((e) => `${e.source}>${e.target}`), ['__start__>worker', 'worker>review', 'review>summarize', 'summarize>__end__']);
  const closed = layoutGraph(g, { collapsed: ['review'] });
  assert.equal(closed.groups.length, 0);
  assert.equal(closed.nodes.find((n) => n.id === 'review').kind, 'subgraph');
  assert.equal(closed.nodes.find((n) => n.id === 'review').count, 2);
});

test('empty and single-node graphs do not throw', () => {
  assert.doesNotThrow(() => layoutGraph({ nodes: [], edges: [] }));
  const l = layoutGraph({ nodes: [{ id: 'only' }], edges: [] });
  assert.equal(l.nodes.length, 1);
  assert.ok(l.width > 0 && l.height > 0);
});

test('left-to-right direction swaps the axes', () => {
  const l = layoutGraph(linear, { direction: 'LR' });
  const n = Object.fromEntries(l.nodes.map((x) => [x.id, x]));
  assert.ok(n.__start__.x < n.a.x);
  assert.ok(n.a.x < n.b.x);
});

test('nodeTags derives feature chips', () => {
  assert.ok(nodeTags({ id: 'approval_gate', metadata: {} }).includes('interrupt'));
  assert.ok(nodeTags({ id: 'x', kind: 'subgraph' }).includes('subgraph'));
  assert.ok(nodeTags({ id: 'x', metadata: { studio_hint: 'retry-policy,stream-writer' } }).includes('retry-policy'));
});
