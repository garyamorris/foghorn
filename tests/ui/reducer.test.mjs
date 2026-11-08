import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRunState, applyPart, applySSE, rankedHypotheses, laneList, laneText, beginRun, finishRun, messagesFromValues, laneIdentity } from '../../src/foghorn/server/static/js/core/reducer.js';
import { encodeSSE, SSEParser } from '../../src/foghorn/server/static/js/core/sse.js';

const tok = (text, node = 'supervisor', extra = {}, id = 'msg-1') => ({
  event: 'messages',
  data: [{ type: 'AIMessageChunk', id, content: text }, { langgraph_node: node, ...extra }],
});
const custom = (kind, key, data, agent = 'metrics_analyst', ev = 'custom') => ({ event: ev, data: { v: 1, kind, key, agent, t_virtual: '2026-11-05T10:49:12Z', data } });

test('token chunks accumulate per lane, in order, with token counts', () => {
  const s = createRunState();
  for (const t of ['Check', 'out ', 'error ', 'rate']) applyPart(s, tok(t), 1000);
  const [lane] = laneList(s);
  assert.equal(laneText(lane), 'Checkout error rate');
  assert.equal(lane.tokens, 4);
  assert.equal(s.counts.tokens, 4);
  assert.equal(s.status, 'running');
});

test('subgraph tokens get their own lane from the SSE namespace; generic node names are folded', () => {
  const s = createRunState();
  applyPart(s, { ...tok('hi', 'model'), event: 'messages|metrics_analyst:3f2a' });
  applyPart(s, tok('top', 'supervisor'));
  applyPart(s, { ...tok('deep', 'test_hypothesis', {}, 'm9'), event: 'messages|rca_tournament:aa11|test_hypothesis:bb22' });
  assert.deepEqual(s.laneOrder, ['metrics_analyst', 'supervisor', 'rca_tournament/test_hypothesis']);
  assert.equal(s.lanes['metrics_analyst'].label, 'metrics_analyst');
  assert.equal(s.lanes['rca_tournament/test_hypothesis'].ns[0], 'rca_tournament');
});

test('laneIdentity falls back to langgraph_checkpoint_ns when the SSE namespace is empty', () => {
  const id = laneIdentity([], { langgraph_node: 'model', langgraph_checkpoint_ns: 'logs_analyst:12|model:34' });
  assert.equal(id.key, 'logs_analyst');
});

test('a new message id starts a new segment in the same lane', () => {
  const s = createRunState();
  applyPart(s, tok('first', 'supervisor', {}, 'a'));
  applyPart(s, tok('second', 'supervisor', {}, 'b'));
  assert.equal(laneList(s)[0].segments.length, 2);
  assert.equal(laneText(laneList(s)[0]), 'first\n\nsecond');
});

test('content blocks and tool call chunks', () => {
  const s = createRunState();
  applyPart(s, { event: 'messages', data: [{ type: 'AIMessageChunk', id: 'x', content: [{ type: 'text', text: 'Looking' }, { type: 'thinking', text: 'hidden' }] }, { langgraph_node: 'agent' }] });
  applyPart(s, { event: 'messages', data: [{ type: 'AIMessageChunk', id: 'x', content: '', tool_call_chunks: [{ name: 'query_metric', args: '{"serv', id: 'call_1', index: 0 }] }, { langgraph_node: 'agent' }] });
  applyPart(s, { event: 'messages', data: [{ type: 'AIMessageChunk', id: 'x', content: '', tool_call_chunks: [{ args: 'ice":"orders-db"}', index: 0 }] }, { langgraph_node: 'agent' }] });
  applyPart(s, { event: 'messages', data: [{ type: 'tool', tool_call_id: 'call_1', name: 'query_metric', content: '{"ok":true}' }, { langgraph_node: 'tools' }] });
  const lane = laneList(s)[0];
  assert.equal(laneList(s).length, 1, 'a result from the tools node does not open a lane of its own');
  assert.equal(Object.keys(lane.tools).length, 1, 'the tool result attaches to the announced call');
  assert.equal(Object.values(lane.tools)[0].result, '{"ok":true}');
  assert.equal(laneText(lane), 'Looking');
  const call = Object.values(lane.tools)[0];
  assert.equal(call.name, 'query_metric');
  assert.equal(call.args, '{"service":"orders-db"}');
});

test('human/system chunks are not streamed', () => {
  const s = createRunState();
  applyPart(s, { event: 'messages', data: [{ type: 'human', content: 'hello' }, { langgraph_node: 'x' }] });
  assert.equal(laneList(s).length, 0);
});

test('custom events: evidence, series, hypotheses, plan, policy, memory, notes', () => {
  const s = createRunState();
  applyPart(s, custom('evidence.found', 'ev:1', { id: 'ev1', kind: 'change', ref: 'cfg-7f21', summary: 'flag ramp', strength: 0.9 }));
  applyPart(s, custom('series', 's:1', { service: 'pricing-service', metric: 'p95_ms', points: [[0, 1], [1, 5]], baseline: 1, onset: 1 }));
  applyPart(s, custom('hypothesis.update', 'h:1:1', { id: 'h1', label: 'flag', score: 0.5, delta: 0.5, for: [], against: [] }));
  applyPart(s, custom('hypothesis.update', 'h:2:1', { id: 'h2', label: 'deploy', score: 0.2, delta: 0.2, for: [], against: [] }));
  applyPart(s, custom('hypothesis.update', 'h:1:2', { id: 'h1', label: 'flag', score: 0.81, delta: 0.31, for: ['x'], against: [] }));
  applyPart(s, custom('plan.proposed', 'p:1', { actions: [{ id: 'revert_flag' }] }));
  applyPart(s, custom('policy.check', 'pol:1', { rule: 'POL-005', result: 'pass', text: 'ok' }));
  applyPart(s, custom('memory.recalled', 'm:1', { ns: 'incidents', key: 'INC-0061', score: 0.8, summary: 'similar' }));
  applyPart(s, custom('note', 'n:1', { text: 'hello' }));
  applyPart(s, custom('handoff', 'ho:1', { to: 'logs_analyst', reason: 'next' }));
  assert.equal(s.evidence.length, 1);
  assert.equal(s.series['pricing-service/p95_ms'].points.length, 2);
  assert.deepEqual(rankedHypotheses(s).map((h) => [h.id, h.score]), [['h1', 0.81], ['h2', 0.2]]);
  assert.deepEqual(s.hypotheses.h1.history, [0.5, 0.81]);
  assert.equal(s.plan.actions[0].id, 'revert_flag');
  assert.equal(s.policy[0].rule, 'POL-005');
  assert.equal(s.memory[0].type, 'recalled');
  assert.equal(s.notes.size, 2);
  assert.equal(s.handoffs[0].to, 'logs_analyst');
});

test('custom events dedupe by key: identical repeats are dropped, changed data upserts', () => {
  const s = createRunState();
  const ev = custom('evidence.found', 'evidence:metric:checkout:error_rate', { id: 'e1', kind: 'metric', ref: 'x', summary: 'a', strength: 0.5 });
  applyPart(s, ev);
  applyPart(s, ev); // interrupt() re-ran the node
  applyPart(s, ev);
  assert.equal(s.evidence.length, 1);
  assert.equal(s.counts.duplicates, 2);
  applyPart(s, custom('evidence.found', 'evidence:metric:checkout:error_rate', { id: 'e1', kind: 'metric', ref: 'x', summary: 'updated', strength: 0.9 }));
  assert.equal(s.evidence.length, 1);
  assert.equal(s.evidence[0].summary, 'updated');
  assert.equal(s.counts.duplicates, 2);
});

test('custom events from subgraphs carry their namespace as agent when none is given', () => {
  const s = createRunState();
  applyPart(s, { event: 'custom|rca_tournament:abc|test_hypothesis:def', data: { v: 1, kind: 'note', key: 'k', data: { text: 't' } } });
  assert.equal(s.notes.last().agent, 'test_hypothesis');
  assert.equal(s.notes.last().ns, 'rca_tournament/test_hypothesis');
});

test('foreign custom payloads are shown generically, never crash', () => {
  const s = createRunState();
  applyPart(s, { event: 'custom', data: { event: 'plan.created', items: [1] } });
  applyPart(s, { event: 'custom', data: 'plain text' });
  applyPart(s, { event: 'custom', data: null });
  assert.equal(s.notes.size, 3);
});

test('seen-key map stays bounded', () => {
  const s = createRunState();
  for (let i = 0; i < 6000; i++) applyPart(s, custom('note', 'k' + i, { text: String(i) }));
  assert.ok(s.seen.size <= 4001);
  assert.equal(s.notes.size, 300);
});

test('updates drive the timeline when tasks mode is absent', () => {
  const s = createRunState({}, 0);
  applyPart(s, { event: 'updates', data: { bootstrap: { a: 1 } } }, 100);
  applyPart(s, { event: 'updates', data: { supervisor: { b: 1 } } }, 250);
  assert.deepEqual(s.timeline.map((x) => [x.name, x.status]), [['bootstrap', 'done'], ['supervisor', 'done']]);
  assert.equal(s.timeline[1].durationMs, 150);
});

test('tasks events: start, finish, error, interrupt; updates close the same step', () => {
  const s = createRunState();
  applyPart(s, { event: 'tasks', data: { id: 't1', name: 'metrics_analyst', input: {}, triggers: ['x'] } }, 1000);
  assert.equal(s.timeline[0].status, 'running');
  applyPart(s, { event: 'tasks', data: { id: 't1', name: 'metrics_analyst', error: null, result: [['a', 1]], interrupts: [] } }, 2200);
  assert.equal(s.timeline[0].status, 'done');
  assert.equal(s.timeline[0].durationMs, 1200);
  applyPart(s, { event: 'tasks', data: { id: 't2', name: 'tool', input: {}, triggers: [] } }, 2300);
  applyPart(s, { event: 'tasks', data: { id: 't2', name: 'tool', error: 'boom', result: null } }, 2400);
  assert.equal(s.timeline[1].status, 'error');
  assert.equal(s.timeline[1].error, 'boom');
  applyPart(s, { event: 'tasks|rca:1', data: { id: 't3', name: 'approval_gate', input: {}, triggers: [] } }, 2500);
  assert.equal(s.timeline[2].depth, 1);
  applyPart(s, { event: 'tasks|rca:1', data: { id: 't3', name: 'approval_gate', error: null, result: null, interrupts: [{ id: 'i1', value: { kind: 'approval' } }] } }, 2600);
  assert.equal(s.timeline[2].status, 'interrupted');
  assert.equal(s.status, 'interrupted');
  assert.equal(s.interrupts[0].id, 'i1');
  // updates after tasks must not duplicate steps
  applyPart(s, { event: 'updates', data: { metrics_analyst: {} } }, 3000);
  assert.equal(s.timeline.length, 3);
});

test('values with __interrupt__ sets interrupts + status; subgraph values do not overwrite root values', () => {
  const s = createRunState();
  applyPart(s, { event: 'values', data: { incident: { id: 'INC-1' } } });
  applyPart(s, { event: 'values|child:1', data: { other: 1 } });
  assert.equal(s.values.incident.id, 'INC-1');
  applyPart(s, { event: 'values', data: { incident: { id: 'INC-1' }, __interrupt__: [{ id: 'abc', value: { kind: 'approval', title: 't' }, response_schema: { type: 'object' } }] } });
  assert.equal(s.status, 'interrupted');
  assert.equal(s.interrupts[0].response_schema.type, 'object');
  assert.equal(s.interrupts[0].value.kind, 'approval');
});

test('updates with __interrupt__', () => {
  const s = createRunState();
  applyPart(s, { event: 'updates', data: { __interrupt__: [{ id: 'z', value: { kind: 'question' } }] } });
  assert.equal(s.interrupts.length, 1);
  assert.equal(s.timeline.length, 0);
});

test('end finishes the run; error marks it; beginRun resets transient state but keeps the board', () => {
  const s = createRunState();
  applyPart(s, custom('evidence.found', 'k', { id: 'e', kind: 'metric', ref: 'r', summary: 's', strength: 1 }));
  applyPart(s, { event: 'tasks', data: { id: 't', name: 'x', input: {}, triggers: [] } }, 10);
  applyPart(s, { event: 'end', data: null }, 50);
  assert.equal(s.status, 'done');
  assert.equal(s.timeline[0].status, 'done');
  beginRun(s, 'run-2', 60);
  assert.equal(s.status, 'running');
  assert.equal(s.evidence.length, 1);
  applyPart(s, { event: 'error', data: { error: 'ValueError', message: 'bad' } }, 70);
  assert.equal(s.status, 'error');
  assert.equal(s.error, 'bad');
});

test('end after an interrupt leaves the run interrupted, not done', () => {
  const s = createRunState();
  applyPart(s, { event: 'values', data: { __interrupt__: [{ id: 'i', value: {} }] } });
  applyPart(s, { event: 'end', data: null });
  assert.equal(s.status, 'interrupted');
});

test('applySSE decodes raw frames incl. CRLF and records the last event id', () => {
  const s = createRunState();
  const p = new SSEParser();
  const text =
    encodeSSE({ event: 'metadata', id: '1-0', data: { run_id: 'r-9' } }) +
    encodeSSE({ event: 'messages', id: '2-0', data: [{ type: 'AIMessageChunk', id: 'm', content: 'Hi' }, { langgraph_node: 'commander' }] }) +
    encodeSSE({ event: 'end', id: '3-0', data: null });
  for (const ev of p.push(text)) applySSE(s, ev, 5);
  assert.equal(s.runId, 'r-9');
  assert.equal(laneText(laneList(s)[0]), 'Hi');
  assert.equal(s.lastEventId, '3-0');
  assert.equal(s.status, 'done');
});

test('raw drawer is a ring buffer (bounded) and counts modes', () => {
  const s = createRunState({ rawCapacity: 50 });
  for (let i = 0; i < 500; i++) applyPart(s, tok('x'));
  assert.equal(s.raw.size, 50);
  assert.equal(s.counts.byMode.messages, 500);
});

test('messagesFromValues extracts chat items with tool calls', () => {
  const items = messagesFromValues({ messages: [{ type: 'human', content: 'hi' }, { type: 'ai', content: [{ type: 'text', text: 'yo' }], tool_calls: [{ id: 'c1', name: 'search_logs', args: { q: 1 } }] }, { type: 'tool', content: 'r', tool_call_id: 'c1' }] });
  assert.deepEqual(items.map((i) => i.role), ['user', 'assistant', 'tool']);
  assert.equal(items[1].text, 'yo');
  assert.equal(items[1].toolCalls[0].name, 'search_logs');
});

test('finishRun is idempotent', () => {
  const s = createRunState();
  applyPart(s, tok('a'));
  finishRun(s, 5);
  finishRun(s, 9);
  assert.equal(s.status, 'done');
  assert.equal(s.endedAt, 5);
});
