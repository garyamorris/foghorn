import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SSEParser, parseSSE, decodeData, splitEventName, encodeSSE } from '../../src/foghorn/server/static/js/core/sse.js';

test('parses CRLF-terminated frames (\\r\\n\\r\\n)', () => {
  const evs = parseSSE('event: metadata\r\ndata: {"run_id":"r1"}\r\n\r\nevent: values\r\ndata: {"a":1}\r\n\r\n');
  assert.equal(evs.length, 2);
  assert.equal(evs[0].event, 'metadata');
  assert.deepEqual(decodeData(evs[0]), { run_id: 'r1' });
  assert.equal(evs[1].event, 'values');
});

test('parses LF-only and bare-CR frames', () => {
  assert.equal(parseSSE('event: a\ndata: 1\n\nevent: b\ndata: 2\n\n').length, 2);
  assert.equal(parseSSE('event: a\rdata: 1\r\rdata: 2\r\r').length, 2);
});

test('multi-line data is joined with \\n', () => {
  const [ev] = parseSSE('data: line1\r\ndata: line2\r\ndata:line3\r\n\r\n');
  assert.equal(ev.data, 'line1\nline2\nline3');
  assert.equal(ev.event, 'message');
});

test('frames split at arbitrary byte boundaries (incl. between \\r and \\n)', () => {
  const full = 'id: 17-0\r\nevent: messages\r\ndata: [{"content":"Hel"},{"langgraph_node":"x"}]\r\n\r\nevent: end\r\ndata: null\r\n\r\n';
  for (let cut = 1; cut < full.length; cut++) {
    const p = new SSEParser();
    const got = [...p.push(full.slice(0, cut)), ...p.push(full.slice(cut))];
    assert.equal(got.length, 2, `cut at ${cut}`);
    assert.equal(got[0].id, '17-0');
    assert.equal(got[0].event, 'messages');
    assert.deepEqual(decodeData(got[0])[0], { content: 'Hel' });
  }
  const p = new SSEParser();
  let n = 0;
  for (const ch of full) n += p.push(ch).length;
  assert.equal(n, 2);
});

test('comments (heartbeats) are skipped and reported', () => {
  const seen = [];
  const p = new SSEParser({ onComment: (c) => seen.push(c) });
  const evs = p.push(': ping - 2026\r\n\r\n: ping\r\n\r\nevent: x\r\ndata: 1\r\n\r\n');
  assert.equal(evs.length, 1);
  assert.equal(p.comments, 2);
  assert.equal(seen.length, 2);
});

test('id persists as lastEventId and BOM is stripped', () => {
  const p = new SSEParser();
  const evs = p.push('﻿id: 5\r\ndata: a\r\n\r\ndata: b\r\n\r\n');
  assert.equal(evs[0].id, '5');
  assert.equal(evs[1].id, '5');
  assert.equal(p.lastEventId, '5');
});

test('retry field and unknown fields', () => {
  const [ev] = parseSSE('retry: 3000\r\nfoo: bar\r\nevent: z\r\ndata: x\r\n\r\n');
  assert.equal(ev.retry, 3000);
  assert.equal(ev.event, 'z');
});

test('decodeData: JSON, text, empty', () => {
  assert.deepEqual(decodeData({ data: '{"a":[1,2]}' }), { a: [1, 2] });
  assert.equal(decodeData({ data: 'not json' }), 'not json');
  assert.equal(decodeData({ data: '' }), null);
});

test('splitEventName parses mode and namespace path', () => {
  assert.deepEqual(splitEventName('values'), { mode: 'values', ns: [], nsRaw: '' });
  const s = splitEventName('custom|metrics_analyst:3f2a|model:91bc');
  assert.equal(s.mode, 'custom');
  assert.deepEqual(s.ns, [
    { node: 'metrics_analyst', taskId: '3f2a' },
    { node: 'model', taskId: '91bc' },
  ]);
  assert.equal(splitEventName('updates|review').ns[0].node, 'review');
});

test('encodeSSE round-trips through the parser and ends with \\r\\n\\r\\n', () => {
  const frame = encodeSSE({ event: 'custom', id: '9', data: { text: 'a\nb' } });
  assert.ok(frame.endsWith('\r\n\r\n'));
  const [ev] = parseSSE(frame);
  assert.deepEqual(decodeData(ev), { text: 'a\nb' });
  assert.equal(ev.id, '9');
});

test('an unterminated trailing frame is discarded', () => {
  const p = new SSEParser();
  assert.equal(p.push('event: a\r\ndata: 1\r\n').length, 0);
  assert.equal(p.flush().length, 0);
});
