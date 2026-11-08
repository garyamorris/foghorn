import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseMarkdown, parseInline, safeHref } from '../../src/foghorn/server/static/js/core/markdown.js';
import { lsGet, lsSet, createState, createEmitter } from '../../src/foghorn/server/static/js/core/store.js';

test('blocks: headings, lists, code fences, quotes, tables, rules', () => {
  const md = '# Title\n\nSome **bold** and `code` text.\n\n- one\n- two\n\n1. first\n2. second\n\n```sh\nmake dev\n```\n\n> quoted\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n---\n';
  const b = parseMarkdown(md);
  assert.deepEqual(b.map((x) => x.type), ['heading', 'p', 'list', 'list', 'code', 'quote', 'table', 'rule']);
  assert.equal(b[2].ordered, false);
  assert.equal(b[3].ordered, true);
  assert.equal(b[4].text, 'make dev');
  assert.deepEqual(b[6].rows, [[[{ t: 'text', text: '1' }], [{ t: 'text', text: '2' }]]]);
});

test('inline: code, strong, em, links', () => {
  const p = parseInline('a `x` **b** *c* [d](https://e.example/f)');
  assert.deepEqual(p.map((x) => x.t), ['text', 'code', 'text', 'strong', 'text', 'em', 'text', 'link']);
  assert.equal(p[7].href, 'https://e.example/f');
});

test('raw HTML is never interpreted and dangerous link schemes are neutralised', () => {
  const p = parseInline('<img src=x onerror=alert(1)> [click](javascript:alert(1)) [ok](#/graphs)');
  assert.equal(p[0].t, 'text');
  assert.ok(p[0].text.includes('<img'));
  assert.equal(p.find((x) => x.text === 'click').href, '#');
  assert.equal(p.find((x) => x.text === 'ok').href, '#/graphs');
  assert.equal(safeHref('data:text/html,<script>1</script>'), '#');
  assert.equal(safeHref(' https://x.y '), 'https://x.y');
});

test('storage helpers degrade silently without localStorage and persist when present', () => {
  const orig = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const setStorage = (v) => Object.defineProperty(globalThis, 'localStorage', { value: v, configurable: true, writable: true });
  try {
    setStorage(undefined);
    assert.equal(lsGet('nope', 'fallback'), 'fallback');
    assert.equal(lsSet('k', 1), false);
    const mem = new Map();
    setStorage({ getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) });
    assert.equal(lsSet('persona', 'dba'), true);
    assert.equal(lsGet('persona'), 'dba');
    const st = createState('x', 'persona');
    assert.equal(st.get(), 'dba');
    let seen;
    st.subscribe((v) => (seen = v));
    st.set('on-call');
    assert.equal(seen, 'on-call');
    assert.equal(lsGet('persona'), 'on-call');
    setStorage({ getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } });
    assert.equal(lsGet('persona', 'safe'), 'safe');
    assert.equal(lsSet('persona', 'x'), false);
  } finally {
    if (orig) Object.defineProperty(globalThis, 'localStorage', orig);
    else delete globalThis.localStorage;
  }
});

test('emitter: unsubscribe and a throwing listener does not break others', () => {
  const e = createEmitter();
  const got = [];
  const off = e.on('t', (x) => got.push(['a', x]));
  e.on('t', () => {
    throw new Error('boom');
  });
  e.on('t', (x) => got.push(['c', x]));
  const orig = console.error;
  console.error = () => {};
  try {
    e.emit('t', 1);
    off();
    e.emit('t', 2);
  } finally {
    console.error = orig;
  }
  assert.deepEqual(got, [['a', 1], ['c', 1], ['c', 2]]);
});
