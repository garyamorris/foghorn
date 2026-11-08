// JSON-Schema -> form model -> JSON value. Pure (no DOM): the renderer lives in views/forms.js.
//
// Interrupt response forms are generated from each interrupt's `response_schema` (pydantic JSON Schema), so new
// interrupt kinds need no UI code. Supported: object, string (enum, format, length, pattern), integer, number,
// boolean, array (of anything), $ref/$defs, allOf[$ref], anyOf/oneOf with null (optional) or all-const (enum),
// const, additionalProperties-only objects (free JSON). Anything else falls back to a raw JSON editor.

const MAX_DEPTH = 8;

function resolveRef(ref, root) {
  if (typeof ref !== 'string' || !ref.startsWith('#/')) return null;
  let cur = root;
  for (const part of ref.slice(2).split('/')) {
    cur = cur?.[part.replace(/~1/g, '/').replace(/~0/g, '~')];
    if (cur === undefined) return null;
  }
  return cur;
}

/** Collapse $ref / allOf / anyOf-null into one plain schema + {nullable}. */
export function normalizeSchema(schema, root = schema, depth = 0) {
  if (schema === true || schema === undefined || schema === null) return { type: 'any', nullable: true };
  if (schema === false) return { type: 'never' };
  if (depth > MAX_DEPTH) return { type: 'any' };
  let s = { ...schema };
  if (s.$ref) {
    const target = resolveRef(s.$ref, root);
    const { $ref, ...rest } = s;
    s = { ...(target ? normalizeSchema(target, root, depth + 1) : {}), ...rest };
  }
  if (Array.isArray(s.allOf) && s.allOf.length) {
    const { allOf, ...rest } = s;
    let merged = {};
    for (const part of allOf) merged = { ...merged, ...normalizeSchema(part, root, depth + 1) };
    s = { ...merged, ...rest };
  }
  const alt = s.anyOf || s.oneOf;
  if (Array.isArray(alt) && alt.length) {
    const { anyOf, oneOf, ...rest } = s;
    const parts = alt.map((a) => normalizeSchema(a, root, depth + 1));
    const nonNull = parts.filter((p) => p.type !== 'null');
    const hasNull = nonNull.length !== parts.length;
    if (nonNull.length && nonNull.every((p) => p.const !== undefined || (Array.isArray(p.enum) && p.enum.length))) {
      const values = [];
      for (const p of nonNull) {
        if (p.const !== undefined) values.push(p.const);
        else values.push(...p.enum);
      }
      s = { ...rest, type: typeof values[0] === 'number' ? 'number' : 'string', enum: values, nullable: hasNull };
    } else if (nonNull.length === 1) {
      s = { ...nonNull[0], ...rest, nullable: hasNull || nonNull[0].nullable };
    } else {
      // heterogeneous union: let the user type JSON
      s = { ...rest, type: 'any', nullable: hasNull, union: nonNull };
    }
  }
  if (Array.isArray(s.type)) {
    const types = s.type.filter((t) => t !== 'null');
    s = { ...s, nullable: s.nullable || types.length !== s.type.length, type: types.length === 1 ? types[0] : 'any' };
  }
  if (s.const !== undefined && !s.enum) s = { ...s, enum: [s.const] };
  if (!s.type) {
    if (s.properties) s.type = 'object';
    else if (s.items) s.type = 'array';
    else if (Array.isArray(s.enum)) s.type = typeof s.enum[0] === 'number' ? 'number' : 'string';
    else s.type = 'any';
  }
  return s;
}

/**
 * Build a form model tree.
 * node: {path, key, kind, title, description, required, nullable, default, enum, format, min/max..., multiline,
 *        properties?: node[], items?: node}
 * kind: 'string' | 'text' | 'integer' | 'number' | 'boolean' | 'enum' | 'array' | 'object' | 'json'
 */
export function buildFormModel(schema, opts = {}) {
  const root = opts.root || schema;
  return buildNode(schema, root, { path: '', key: '', required: true, title: opts.title ?? schema?.title ?? '' }, 0);
}

function buildNode(rawSchema, root, meta, depth) {
  const s = normalizeSchema(rawSchema, root);
  const base = {
    path: meta.path,
    key: meta.key,
    title: meta.title || s.title || humanize(meta.key),
    description: s.description || '',
    required: !!meta.required,
    nullable: !!s.nullable,
    default: s.default,
    schema: s,
  };
  if (depth > MAX_DEPTH) return { ...base, kind: 'json' };
  if (Array.isArray(s.enum)) {
    return { ...base, kind: 'enum', enum: s.enum, enumType: s.type };
  }
  switch (s.type) {
    case 'string': {
      const multiline =
        s.format === 'textarea' ||
        (s.maxLength && s.maxLength > 160) ||
        /(^|_)(text|note|body|sql|reason|summary|draft|message|comment|feedback|answer|question|prompt)s?$/.test(meta.key) ||
        (typeof s.default === 'string' && s.default.includes('\n'));
      return {
        ...base,
        kind: multiline ? 'text' : 'string',
        format: s.format,
        minLength: s.minLength,
        maxLength: s.maxLength,
        pattern: s.pattern,
      };
    }
    case 'integer':
    case 'number':
      return { ...base, kind: s.type, minimum: s.minimum ?? s.exclusiveMinimum, maximum: s.maximum ?? s.exclusiveMaximum };
    case 'boolean':
      return { ...base, kind: 'boolean' };
    case 'array': {
      const itemNode = buildNode(s.items || {}, root, { path: meta.path + '[]', key: 'item', required: true, title: 'Item' }, depth + 1);
      return { ...base, kind: 'array', items: itemNode, minItems: s.minItems, maxItems: s.maxItems };
    }
    case 'object': {
      const props = s.properties || {};
      const req = new Set(s.required || []);
      const names = Object.keys(props);
      if (!names.length) return { ...base, kind: 'json', freeform: true };
      return {
        ...base,
        kind: 'object',
        properties: names.map((k) =>
          buildNode(props[k], root, { path: meta.path ? meta.path + '.' + k : k, key: k, required: req.has(k) }, depth + 1),
        ),
      };
    }
    default:
      return { ...base, kind: 'json' };
  }
}

export function humanize(key) {
  return String(key || '')
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/^\w/, (c) => c.toUpperCase());
}

/** Initial raw value tree for a node. `seed` overrides defaults (shape follows the schema). */
export function initialValue(node, seed) {
  if (seed !== undefined) {
    if (node.kind === 'object' && seed && typeof seed === 'object' && !Array.isArray(seed)) {
      const out = {};
      for (const p of node.properties) out[p.key] = initialValue(p, seed[p.key]);
      return out;
    }
    return clone(seed);
  }
  if (node.default !== undefined) return clone(node.default);
  switch (node.kind) {
    case 'object': {
      const out = {};
      for (const p of node.properties) out[p.key] = initialValue(p);
      return out;
    }
    case 'array':
      return [];
    case 'boolean':
      return false;
    case 'enum':
      return node.required && !node.nullable ? node.enum[0] : undefined;
    case 'json':
      return node.freeform ? {} : undefined;
    default:
      return undefined;
  }
}

function clone(v) {
  return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

/** Convert an edited raw tree into a JSON value. Returns {value, errors:[{path,message}]}. */
export function collectValue(node, raw, errors = []) {
  const err = (message) => errors.push({ path: node.path, message });
  const isBlank = raw === undefined || raw === null || raw === '';
  if (isBlank) {
    if (node.kind === 'boolean') return { value: !!raw, errors };
    if (node.required && !node.nullable && node.kind !== 'array' && node.kind !== 'object') {
      if (node.default !== undefined) return { value: node.default, errors };
      err(`${node.title} is required`);
    }
    if (node.kind === 'array') return { value: [], errors };
    return { value: node.nullable && raw === null ? null : undefined, errors };
  }
  switch (node.kind) {
    case 'string':
    case 'text': {
      const s = String(raw);
      if (node.minLength && s.length < node.minLength) err(`${node.title} needs at least ${node.minLength} characters`);
      if (node.maxLength && s.length > node.maxLength) err(`${node.title} is limited to ${node.maxLength} characters`);
      if (node.pattern) {
        try {
          if (!new RegExp(node.pattern).test(s)) err(`${node.title} has an invalid format`);
        } catch {
          /* ignore unsupported regex dialects */
        }
      }
      return { value: s, errors };
    }
    case 'integer':
    case 'number': {
      const n = typeof raw === 'number' ? raw : Number(String(raw).trim());
      if (!Number.isFinite(n)) {
        err(`${node.title} must be a number`);
        return { value: undefined, errors };
      }
      if (node.kind === 'integer' && !Number.isInteger(n)) err(`${node.title} must be a whole number`);
      if (node.minimum !== undefined && n < node.minimum) err(`${node.title} must be at least ${node.minimum}`);
      if (node.maximum !== undefined && n > node.maximum) err(`${node.title} must be at most ${node.maximum}`);
      return { value: n, errors };
    }
    case 'boolean':
      return { value: raw === true || raw === 'true', errors };
    case 'enum': {
      const hit = node.enum.find((e) => e === raw || String(e) === String(raw));
      if (hit === undefined) err(`${node.title} must be one of ${node.enum.join(', ')}`);
      return { value: hit, errors };
    }
    case 'array': {
      const arr = Array.isArray(raw) ? raw : [];
      const out = [];
      arr.forEach((item, i) => {
        const sub = { ...node.items, path: `${node.path}[${i}]` };
        const r = collectValue(sub, item, errors);
        if (r.value !== undefined) out.push(r.value);
      });
      if (node.minItems && out.length < node.minItems) err(`${node.title} needs at least ${node.minItems} item(s)`);
      if (node.maxItems && out.length > node.maxItems) err(`${node.title} allows at most ${node.maxItems} item(s)`);
      return { value: out, errors };
    }
    case 'object': {
      const out = {};
      for (const p of node.properties) {
        const r = collectValue(p, raw?.[p.key], errors);
        if (r.value !== undefined) out[p.key] = r.value;
      }
      return { value: out, errors };
    }
    case 'json': {
      if (typeof raw === 'string') {
        try {
          return { value: JSON.parse(raw), errors };
        } catch (e) {
          err(`${node.title} must be valid JSON (${e.message})`);
          return { value: undefined, errors };
        }
      }
      if (!node.required && raw && typeof raw === 'object' && !Array.isArray(raw) && !Object.keys(raw).length) return { value: undefined, errors };
      return { value: raw, errors };
    }
    default:
      return { value: raw, errors };
  }
}

/** Minimal JSON-Schema validator for resume values (server validation stays authoritative). */
export function validateValue(schema, value, root = schema, path = '') {
  const s = normalizeSchema(schema, root);
  const errs = [];
  const add = (m) => errs.push({ path, message: m });
  if (value === null || value === undefined) {
    if (!s.nullable && s.type !== 'any') add('is required');
    return errs;
  }
  if (Array.isArray(s.enum) && !s.enum.includes(value)) add(`must be one of ${s.enum.join(', ')}`);
  switch (s.type) {
    case 'string':
      if (typeof value !== 'string') add('must be a string');
      else {
        if (s.minLength && value.length < s.minLength) add(`needs at least ${s.minLength} characters`);
        if (s.maxLength && value.length > s.maxLength) add(`is limited to ${s.maxLength} characters`);
      }
      break;
    case 'integer':
      if (!Number.isInteger(value)) add('must be an integer');
      break;
    case 'number':
      if (typeof value !== 'number') add('must be a number');
      break;
    case 'boolean':
      if (typeof value !== 'boolean') add('must be true or false');
      break;
    case 'array':
      if (!Array.isArray(value)) add('must be a list');
      else value.forEach((v, i) => errs.push(...validateValue(s.items || {}, v, root, `${path}[${i}]`)));
      break;
    case 'object':
      if (typeof value !== 'object' || Array.isArray(value)) add('must be an object');
      else {
        for (const k of s.required || []) if (value[k] === undefined) errs.push({ path: path ? `${path}.${k}` : k, message: 'is required' });
        for (const [k, sub] of Object.entries(s.properties || {})) {
          if (value[k] !== undefined) errs.push(...validateValue(sub, value[k], root, path ? `${path}.${k}` : k));
        }
      }
      break;
    default:
      break;
  }
  return errs;
}

// ---- decision-style schemas (Decision / Answer / MemoryDecision) -----------------------------------------------------

/** Which action-selector field a schema has, and its options: {field, options, kind}. */
export function findActionField(model) {
  if (!model || model.kind !== 'object') return null;
  for (const name of ['decision', 'action', 'choice', 'verdict']) {
    const p = model.properties.find((x) => x.key === name);
    if (p && p.kind === 'enum') return { field: name, options: p.enum, node: p };
  }
  return null;
}

const CONDITIONAL = {
  edits: ['edit'],
  question: ['ask'],
  text: ['edit'],
  edited: ['edit'],
  feedback: ['reject'],
};

/** Should field `key` be shown given the chosen action? Unknown fields are always shown. */
export function isFieldVisible(key, action, options = []) {
  const when = CONDITIONAL[key];
  if (!when || !action) return true;
  if (!when.some((w) => options.includes(w))) return true;
  return when.includes(action);
}

/** Label/tone hints for decision buttons. */
export const ACTION_META = {
  approve: { label: 'Approve', tone: 'positive', key: 'a' },
  save: { label: 'Save to memory', tone: 'positive', key: 'a' },
  edit: { label: 'Edit and approve', tone: 'neutral', key: 'e' },
  reject: { label: 'Reject', tone: 'danger', key: 'r' },
  discard: { label: 'Discard', tone: 'danger', key: 'r' },
  ask: { label: 'Ask a question', tone: 'neutral', key: '?' },
  answer: { label: 'Send answer', tone: 'positive', key: 'a' },
};

/**
 * Build the `Command(resume=...)` payload. With interrupt ids we always send an {id: value} map (safe for 1..n
 * interrupts: a bare value raises when more than one interrupt is pending - docs/design/02 gotcha 15).
 */
export function buildResume(interrupts, valuesById) {
  const entries = interrupts.filter((i) => i.id && valuesById[i.id] !== undefined);
  if (!entries.length) {
    const only = interrupts.length === 1 ? Object.values(valuesById)[0] : undefined;
    return only;
  }
  const map = {};
  for (const i of entries) map[i.id] = valuesById[i.id];
  return map;
}

/** Ids in `map` that are not pending (stale). Dashboard-side validation: the server silently re-interrupts. */
export function staleResumeIds(pendingIds, map) {
  if (!map || typeof map !== 'object') return [];
  const pending = new Set(pendingIds);
  return Object.keys(map).filter((k) => !pending.has(k));
}

/** Normalise a graph `__interrupt__` entry or thread.interrupts item into {id, value, response_schema}. */
export function normalizeInterrupt(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw.value !== undefined ? raw.value : raw;
  return {
    id: raw.id || raw.interrupt_id || value?.interrupt_id || null,
    value,
    response_schema: raw.response_schema || value?.response_schema || null,
    ns: raw.ns || null,
  };
}
