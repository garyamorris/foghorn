// Interrupt cards: rendered generically from an interrupt payload + its JSON `response_schema`.
// New interrupt kinds need no UI code: the form, validation and action buttons all come from the schema.
import { h, mount } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, chip, field, inputEl, jsonViewer, selectEl, switchEl, textareaEl, riskBadge, asyncClick } from '../core/ui.js';
import { buildFormModel, initialValue, collectValue, findActionField, isFieldVisible, ACTION_META } from '../core/schemaform.js';
import { errorMessage } from '../core/ui.js';

// Fallbacks used only when an interrupt arrives without a response_schema (e.g. thread state read through the
// Agent Server). They mirror foghorn.contracts.interrupts.
const DECISION_SCHEMA = {
  title: 'Decision',
  type: 'object',
  properties: {
    decision: { enum: ['approve', 'reject', 'edit', 'ask'], type: 'string', title: 'Decision' },
    decided_by: { type: 'string', default: 'operator', title: 'Decided By' },
    note: { type: 'string', default: '', title: 'Note' },
    edits: { type: 'object', additionalProperties: true, title: 'Edits' },
    question: { type: 'string', default: '', title: 'Question' },
  },
  required: ['decision'],
};
const ANSWER_SCHEMA = { title: 'Answer', type: 'object', properties: { answer: { type: 'string', title: 'Answer' }, decided_by: { type: 'string', default: 'operator' } }, required: ['answer'] };
const MEMORY_SCHEMA = { title: 'MemoryDecision', type: 'object', properties: { decision: { enum: ['save', 'edit', 'discard'], type: 'string' }, text: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null }, decided_by: { type: 'string', default: 'operator' } }, required: ['decision'] };

export function fallbackSchema(kind) {
  if (kind === 'question') return ANSWER_SCHEMA;
  if (kind === 'memory_write') return MEMORY_SCHEMA;
  return DECISION_SCHEMA;
}

// ---- schema form renderer ---------------------------------------------------------------------------------------

function renderNode(node, parentState, key, ctx) {
  const id = `${ctx.prefix}-${node.path.replace(/[^\w]+/g, '_') || 'root'}`;
  const label = node.title;
  const get = () => parentState[key];
  const set = (v) => {
    parentState[key] = v;
    ctx.onChange();
  };
  const wrapErr = (control, extra = {}) => field(label, control, { hint: node.description || extra.hint, required: node.required && !node.nullable, id });
  switch (node.kind) {
    case 'enum': {
      const sel = selectEl(
        [...(node.required ? [] : [{ value: '', label: '(none)' }]), ...node.enum.map((v) => ({ value: String(v), label: String(v) }))],
        get() ?? '',
        (v) => set(v === '' ? undefined : node.enumType === 'number' || node.enumType === 'integer' ? Number(v) : v),
      );
      return wrapErr(sel);
    }
    case 'boolean': {
      return h('div', { class: 'field' }, switchEl(label, !!get(), (v) => set(v)), node.description && h('div', { class: 'hint' }, node.description));
    }
    case 'integer':
    case 'number': {
      const inp = inputEl({ type: 'number', value: get() ?? '', step: node.kind === 'integer' ? 1 : 'any', min: node.minimum, max: node.maximum, oninput: (e) => set(e.target.value === '' ? undefined : e.target.value) });
      return wrapErr(inp);
    }
    case 'text': {
      const ta = textareaEl({ value: get() ?? '', rows: 3, oninput: (e) => set(e.target.value) });
      return wrapErr(ta);
    }
    case 'array': {
      const list = h('div', { class: 'col' });
      const draw = () => {
        const arr = Array.isArray(get()) ? get() : [];
        mount(
          list,
          arr.map((_, i) => {
            const holder = { v: arr[i] };
            const sub = { ...node.items, path: `${node.path}[${i}]` };
            const inner = renderNode(sub, holder, 'v', { ...ctx, onChange: () => { arr[i] = holder.v; set(arr); } });
            return h('div', { class: 'row top' }, h('div', { class: 'grow' }, inner), button('', { icon: 'x', size: 'sm', title: 'Remove item', onclick: () => { arr.splice(i, 1); set(arr); draw(); } }));
          }),
          button('Add item', { icon: 'plus', size: 'sm', onclick: () => { const a = Array.isArray(get()) ? get() : []; a.push(initialValue(node.items)); set(a); draw(); } }),
        );
      };
      draw();
      return h('div', { class: 'field' }, h('label', null, label), list, node.description && h('div', { class: 'hint' }, node.description));
    }
    case 'object': {
      if (!parentState[key] || typeof parentState[key] !== 'object') parentState[key] = {};
      const inner = node.properties.map((p) => {
        const wrap = h('div', { dataset: { field: p.key } }, renderNode(p, parentState[key], p.key, ctx));
        ctx.registerField(p.key, wrap, node);
        return wrap;
      });
      return key === '' ? h('div', { class: 'col gap-3' }, inner) : h('fieldset', { class: 'fieldset' }, h('legend', null, label), h('div', { class: 'col gap-3' }, inner));
    }
    case 'json': {
      const cur = get();
      const ta = textareaEl({ value: typeof cur === 'string' ? cur : cur === undefined ? '' : JSON.stringify(cur, null, 2), rows: 4, mono: true, spellcheck: false, oninput: (e) => set(e.target.value) });
      return wrapErr(ta, { hint: node.description || 'JSON' });
    }
    default: {
      const inp = inputEl({ value: get() ?? '', oninput: (e) => set(e.target.value), mono: node.format === 'uri' });
      return wrapErr(inp);
    }
  }
}

/**
 * Build a form for `schema`. Returns {el, collect(action?) -> {value, errors}, setAction(a), model, state, actions}.
 * If the schema has an action enum (decision), the caller renders buttons from `actions` and the field itself is hidden.
 */
export function buildSchemaForm(schema, { seed = {}, prefix = 'f', onChange = () => {} } = {}) {
  const model = buildFormModel(schema);
  const state = initialValue(model, seed);
  const actionInfo = findActionField(model);
  let action = actionInfo ? state[actionInfo.field] : null;
  const fieldEls = new Map();
  const ctx = {
    prefix,
    onChange: () => {
      onChange();
    },
    registerField: (k, el, parent) => {
      if (parent === model) fieldEls.set(k, el);
    },
  };
  const el = h('div', { class: 'schema-form' });
  const draw = () => {
    fieldEls.clear();
    mount(el, renderNode(model, { '': state }, '', ctx));
    applyVisibility();
  };
  const applyVisibility = () => {
    for (const [k, node] of fieldEls) {
      if (actionInfo && k === actionInfo.field) node.hidden = true;
      else node.hidden = !isFieldVisible(k, action, actionInfo?.options);
    }
  };
  draw();
  return {
    el,
    model,
    state,
    actions: actionInfo ? actionInfo.options : null,
    actionField: actionInfo?.field || null,
    setAction(a) {
      action = a;
      if (actionInfo) state[actionInfo.field] = a;
      applyVisibility();
    },
    collect() {
      return collectValue(model, state);
    },
  };
}

// ---- interrupt card ---------------------------------------------------------------------------------------------

const KIND_LABEL = { approval: 'Approval', edit: 'Edit', question: 'Question', break_glass: 'Break glass', memory_write: 'Memory write', tool_review: 'Tool review', four_eyes: 'Two-person' };
const KIND_TONE = { approval: 'accent', edit: 'low', question: 'medium', break_glass: 'critical', memory_write: 'ok', tool_review: 'info', four_eyes: 'high' };

/**
 * Render one interrupt as a decision card.
 * interrupt: {id, value:<build_interrupt payload>, response_schema}
 * opts: {persona, onSubmit(value, ctx) => Promise, asked: string[], compact, primaryKey}
 * Returns {el, trigger(action), setBusy(b)}.
 */
export function interruptCard(interrupt, opts = {}) {
  const v = interrupt.value && typeof interrupt.value === 'object' ? interrupt.value : { summary: String(interrupt.value ?? '') };
  const kind = v.kind || 'approval';
  const schema = interrupt.response_schema || fallbackSchema(kind);
  const persona = opts.persona || 'operator';
  const errorsBox = h('div', { class: 'err small', role: 'alert', hidden: true });
  let busy = false;
  const form = buildSchemaForm(schema, { seed: { decided_by: persona, ...(kind === 'edit' && v.proposed ? {} : {}) }, prefix: `i${(interrupt.id || Math.random().toString(36)).toString().slice(0, 8)}`, onChange: () => { errorsBox.hidden = true; } });

  const checks = v.policy?.checks || [];
  const blast = v.blast_radius || [];
  const refs = v.context?.evidence_refs || [];
  const proposed = v.proposed && Object.keys(v.proposed).length ? v.proposed : null;

  const actionBtns = [];
  const submit = async (action) => {
    if (busy) return;
    if (form.actionField) form.setAction(action);
    const { value, errors } = form.collect();
    if (kind === 'break_glass' && (action === 'approve' || action === 'edit')) {
      const reason = String(value.note || '');
      if (reason.trim().length < 20) errors.push({ path: 'note', message: 'Break-glass needs a reason of at least 20 characters (put it in Note).' });
    }
    if (action === 'ask' && form.actionField && !String(value.question || '').trim()) errors.push({ path: 'question', message: 'Type the question you want answered.' });
    if (action === 'edit' && form.actionField && (!value.edits || !Object.keys(value.edits).length)) errors.push({ path: 'edits', message: 'Describe your changes in Edits (JSON), e.g. {"sql": "..."}.' });
    if (errors.length) {
      errorsBox.hidden = false;
      mount(errorsBox, errors.map((e) => h('div', null, e.message)));
      return;
    }
    errorsBox.hidden = true;
    setBusy(true);
    try {
      await opts.onSubmit(value, { action, interrupt });
    } catch (e) {
      errorsBox.hidden = false;
      mount(errorsBox, h('div', null, errorMessage(e)));
    } finally {
      setBusy(false);
    }
  };
  const setBusy = (b) => {
    busy = b;
    el.classList.toggle('busy', b);
    actionBtns.forEach((x) => (x.disabled = b));
  };

  let actionArea;
  if (form.actions) {
    for (const a of form.actions) {
      const meta = ACTION_META[a] || { label: a, tone: 'neutral' };
      const btn = button(meta.label, { variant: meta.tone === 'positive' ? 'positive' : meta.tone === 'danger' ? 'danger' : undefined, icon: meta.tone === 'positive' ? 'check' : meta.tone === 'danger' ? 'x' : a === 'ask' ? 'help' : 'edit', title: meta.key ? `Shortcut: ${meta.key}` : undefined, onclick: () => submit(a) });
      btn.dataset.action = a;
      btn.addEventListener('focus', () => form.setAction(a));
      btn.addEventListener('mouseenter', () => {});
      actionBtns.push(btn);
    }
    // show the conditional fields for the action the user is working on (edit/ask) without submitting
    const reveal = h('div', { class: 'row wrap small muted' }, 'Fill the fields above, then choose an action. ', ...['edit', 'ask'].filter((a) => form.actions.includes(a)).map((a) => button(a === 'edit' ? 'Show edit fields' : 'Show question field', { size: 'sm', variant: 'ghost', onclick: () => form.setAction(a) })));
    actionArea = h('div', { class: 'col gap-3' }, reveal, h('div', { class: 'row wrap' }, actionBtns));
  } else {
    const btn = button(kind === 'question' ? 'Send answer' : 'Submit', { variant: 'primary', icon: 'send', onclick: () => submit(null) });
    actionBtns.push(btn);
    actionArea = h('div', { class: 'row wrap' }, btn);
  }

  const el = h(
    'article',
    { class: ['icard', `kind-${kind}`], dataset: { interrupt: interrupt.id || '' }, tabindex: '-1', 'aria-label': v.title || KIND_LABEL[kind] || 'Interrupt' },
    h('header', { class: 'icard-head' },
      h('div', { class: 'grow' },
        h('div', { class: 'row wrap' }, badge(KIND_LABEL[kind] || kind, KIND_TONE[kind] || 'info', { sq: true }), v.risk && riskBadge(v.risk), v.reversible === false && badge('irreversible', 'critical', { outline: true }), v.reversible === true && badge('reversible', 'ok', { outline: true }), v.policy?.approvers_required > 1 && badge(`${v.policy.approvers_required} approvers`, 'high', { outline: true })),
        h('h3', { style: 'margin-top:6px' }, v.title || (kind === 'question' ? 'The agent has a question' : 'Decision needed')),
      ),
    ),
    v.summary && h('p', { class: 'icard-summary untrusted-text' }, String(v.summary)),
    blast.length > 0 && h('div', { class: 'icard-sec' }, h('div', { class: 'sec-label' }, 'Blast radius'), h('div', { class: 'row wrap' }, blast.map((b) => chip(String(b), { mono: true })))),
    checks.length > 0 && h('div', { class: 'icard-sec' }, h('div', { class: 'sec-label' }, 'Policy checks'), h('ul', { class: 'plain-list' }, checks.map((c) => h('li', { class: 'row top small' }, badge(c.status || '?', c.status === 'pass' ? 'ok' : c.status === 'fail' ? 'critical' : 'medium', { sq: true }), h('span', { class: 'mono muted' }, c.id), h('span', { class: 'grow' }, c.text))))),
    v.policy?.roles?.length > 0 && h('div', { class: 'icard-sec small muted' }, 'Roles that may decide: ', v.policy.roles.join(', ')),
    refs.length > 0 && h('div', { class: 'icard-sec' }, h('div', { class: 'sec-label' }, 'Evidence'), h('div', { class: 'row wrap' }, refs.map((r) => chip(String(r), { mono: true })))),
    proposed && h('details', { class: 'icard-sec', open: kind === 'edit' || kind === 'tool_review' || kind === 'memory_write' }, h('summary', { class: 'sec-label', style: 'cursor:pointer' }, 'Proposed'), jsonViewer(proposed, { open: 2 })),
    (opts.asked || []).length > 0 && h('div', { class: 'icard-sec' }, h('div', { class: 'sec-label' }, 'You asked'), h('ul', { class: 'plain-list' }, opts.asked.map((q) => h('li', { class: 'small' }, icon('help', 'sm'), ' ', q)))),
    h('div', { class: 'icard-form' }, form.el),
    errorsBox,
    actionArea,
  );
  return { el, trigger: submit, setBusy, form, kind };
}
export { asyncClick };
