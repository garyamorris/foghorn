import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeSchema, buildFormModel, initialValue, collectValue, validateValue, findActionField, isFieldVisible, buildResume, staleResumeIds, normalizeInterrupt } from '../../src/foghorn/server/static/js/core/schemaform.js';

// pydantic JSON Schema for contracts/interrupts.py::Decision and friends
const DECISION = {
  title: 'Decision',
  type: 'object',
  properties: {
    decision: { enum: ['approve', 'reject', 'edit', 'ask'], title: 'Decision', type: 'string' },
    decided_by: { default: 'operator', title: 'Decided By', type: 'string' },
    note: { default: '', title: 'Note', type: 'string' },
    edits: { additionalProperties: true, title: 'Edits', type: 'object' },
    question: { default: '', title: 'Question', type: 'string' },
  },
  required: ['decision'],
};
const MEMORY = {
  title: 'MemoryDecision',
  type: 'object',
  properties: {
    decision: { enum: ['save', 'edit', 'discard'], title: 'Decision', type: 'string' },
    text: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null, title: 'Text' },
    decided_by: { default: 'operator', title: 'Decided By', type: 'string' },
  },
  required: ['decision'],
};
const ANSWER = { title: 'Answer', type: 'object', properties: { answer: { title: 'Answer', type: 'string' }, decided_by: { default: 'operator', type: 'string' } }, required: ['answer'] };

test('Decision schema: enum action field, optional strings, free-form edits as JSON', () => {
  const m = buildFormModel(DECISION);
  assert.equal(m.kind, 'object');
  const byKey = Object.fromEntries(m.properties.map((p) => [p.key, p]));
  assert.equal(byKey.decision.kind, 'enum');
  assert.equal(byKey.decision.required, true);
  assert.equal(byKey.edits.kind, 'json');
  assert.equal(byKey.note.required, false);
  assert.equal(byKey.note.kind, 'text'); // multiline hint from the field name
  const a = findActionField(m);
  assert.deepEqual(a.options, ['approve', 'reject', 'edit', 'ask']);
  assert.equal(a.field, 'decision');
});

test('collectValue builds the resume payload and omits blank optionals; defaults fill in', () => {
  const m = buildFormModel(DECISION);
  const raw = initialValue(m, { decision: 'approve', decided_by: 'on-call' });
  const { value, errors } = collectValue(m, raw);
  assert.deepEqual(errors, []);
  assert.equal(value.decision, 'approve');
  assert.equal(value.decided_by, 'on-call');
  assert.equal('question' in value, false); // blank optionals are omitted: the server applies its own defaults
  assert.equal('edits' in value, false);
});

test('required fields and enum validity are reported', () => {
  const m = buildFormModel(DECISION);
  const r = collectValue(m, { decision: undefined, decided_by: 'x' });
  assert.equal(r.errors.length, 1);
  assert.match(r.errors[0].message, /required/);
  assert.ok(collectValue(m, { decision: 'nuke' }).errors.length >= 1);
});

test('free-form JSON field parses text and reports syntax errors', () => {
  const m = buildFormModel(DECISION);
  const ok = collectValue(m, { decision: 'edit', edits: '{"sql":"CREATE INDEX CONCURRENTLY x"}' });
  assert.deepEqual(ok.value.edits, { sql: 'CREATE INDEX CONCURRENTLY x' });
  const bad = collectValue(m, { decision: 'edit', edits: '{oops' });
  assert.equal(bad.errors.length, 1);
  assert.match(bad.errors[0].message, /valid JSON/);
});

test('anyOf [string, null] is optional text; MemoryDecision', () => {
  const m = buildFormModel(MEMORY);
  const text = m.properties.find((p) => p.key === 'text');
  assert.equal(text.nullable, true);
  assert.equal(text.required, false);
  assert.ok(['string', 'text'].includes(text.kind));
  const { value, errors } = collectValue(m, { decision: 'edit', text: 'lesson: warm the cache' });
  assert.deepEqual(errors, []);
  assert.equal(value.text, 'lesson: warm the cache');
});

test('Answer schema requires the answer', () => {
  const m = buildFormModel(ANSWER);
  assert.equal(findActionField(m), null);
  assert.equal(collectValue(m, { answer: '' }).errors.length, 1);
  assert.equal(collectValue(m, { answer: 'yes, intended' }).errors.length, 0);
});

test('$ref / $defs / allOf resolve; nested objects and arrays', () => {
  const schema = {
    $defs: { Level: { enum: ['low', 'high'], type: 'string' }, Item: { type: 'object', properties: { name: { type: 'string' }, qty: { type: 'integer', minimum: 1 } }, required: ['name'] } },
    type: 'object',
    properties: {
      level: { $ref: '#/$defs/Level' },
      wrapped: { allOf: [{ $ref: '#/$defs/Level' }], default: 'low' },
      items: { type: 'array', items: { $ref: '#/$defs/Item' } },
      flag: { type: 'boolean', default: true },
      ratio: { type: ['number', 'null'] },
    },
    required: ['level'],
  };
  const m = buildFormModel(schema);
  const k = Object.fromEntries(m.properties.map((p) => [p.key, p]));
  assert.equal(k.level.kind, 'enum');
  assert.equal(k.wrapped.kind, 'enum');
  assert.equal(k.items.kind, 'array');
  assert.equal(k.items.items.kind, 'object');
  assert.equal(k.ratio.nullable, true);
  const r = collectValue(m, { level: 'high', items: [{ name: 'a', qty: '2' }, { name: 'b' }], flag: false, ratio: '0.5' });
  assert.deepEqual(r.errors, []);
  assert.deepEqual(r.value, { level: 'high', items: [{ name: 'a', qty: 2 }, { name: 'b' }], flag: false, ratio: 0.5 });
  const bad = collectValue(m, { level: 'high', items: [{ name: 'a', qty: '0' }] });
  assert.ok(bad.errors.some((e) => /at least 1/.test(e.message)));
});

test('string constraints', () => {
  const m = buildFormModel({ type: 'object', properties: { reason: { type: 'string', minLength: 20 } }, required: ['reason'] });
  assert.equal(collectValue(m, { reason: 'too short' }).errors.length, 1);
  assert.equal(collectValue(m, { reason: 'x'.repeat(25) }).errors.length, 0);
});

test('validateValue (client-side pre-check)', () => {
  assert.deepEqual(validateValue(DECISION, { decision: 'approve' }), []);
  assert.ok(validateValue(DECISION, {}).some((e) => e.path === 'decision'));
  assert.ok(validateValue(DECISION, { decision: 'zzz' }).length >= 1);
});

test('conditional field visibility by chosen action', () => {
  const opts = ['approve', 'reject', 'edit', 'ask'];
  assert.equal(isFieldVisible('edits', 'approve', opts), false);
  assert.equal(isFieldVisible('edits', 'edit', opts), true);
  assert.equal(isFieldVisible('question', 'ask', opts), true);
  assert.equal(isFieldVisible('question', 'reject', opts), false);
  assert.equal(isFieldVisible('note', 'approve', opts), true);
  assert.equal(isFieldVisible('text', 'save', ['save', 'edit', 'discard']), false);
  assert.equal(isFieldVisible('text', 'edit', ['save', 'edit', 'discard']), true);
});

test('buildResume always uses an {interrupt_id: value} map; parallel interrupts resume together', () => {
  const ia = 'a'.repeat(32);
  const ib = 'b'.repeat(32);
  const ints = [{ id: ia }, { id: ib }];
  const map = buildResume(ints, { [ia]: { decision: 'approve' }, [ib]: { decision: 'edit', edits: { text: 'x' } } });
  assert.deepEqual(Object.keys(map), [ia, ib]);
  assert.equal(map[ib].decision, 'edit');
  assert.deepEqual(buildResume([{ id: ia }], { [ia]: { answer: 'yes' } }), { [ia]: { answer: 'yes' } });
  // legacy interrupt without an id: bare value
  assert.deepEqual(buildResume([{ id: null }], { x: { answer: 'y' } }), { answer: 'y' });
});

test('staleResumeIds flags ids that are no longer pending', () => {
  assert.deepEqual(staleResumeIds(['a', 'b'], { a: 1, c: 2 }), ['c']);
  assert.deepEqual(staleResumeIds(['a'], 'bare'), []);
});

test('normalizeInterrupt', () => {
  assert.deepEqual(normalizeInterrupt({ id: 'i', value: { kind: 'approval' }, response_schema: { type: 'object' } }).value, { kind: 'approval' });
  assert.equal(normalizeInterrupt({ kind: 'question' }).value.kind, 'question');
  assert.equal(normalizeInterrupt(null), null);
});

test('normalizeSchema: Literal union via anyOf const', () => {
  const s = normalizeSchema({ anyOf: [{ const: 'a', type: 'string' }, { const: 'b', type: 'string' }, { type: 'null' }] });
  assert.deepEqual(s.enum, ['a', 'b']);
  assert.equal(s.nullable, true);
});
