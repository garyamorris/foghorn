// UI component library: small functions returning DOM nodes. Strings are always text nodes.
import { h, clear, mount } from './dom.js';
import { icon } from './icons.js';
import { parseMarkdown } from './markdown.js';
import { safeJson, formatNumber } from './format.js';
import { ApiError } from '../api.js';

// ---- tones ------------------------------------------------------------------------------------------------------
export const SEV_TONE = { critical: 'critical', high: 'high', medium: 'medium', low: 'low', info: 'info' };
export const INC_TONE = { SEV1: 'critical', SEV2: 'high', SEV3: 'medium', SEV4: 'low' };
export const RISK_TONE = { critical: 'critical', high: 'high', medium: 'medium', low: 'low' };
const STATUS_TONE = {
  ok: 'ok', pass: 'ok', passed: 'ok', success: 'ok', succeeded: 'ok', done: 'ok', finished: 'ok', resolved: 'ok', running: 'accent', open: 'high', pending: 'medium', queued: 'info',
  interrupted: 'medium', mitigating: 'medium', monitoring: 'low', failed: 'critical', fail: 'critical', error: 'critical', dead: 'critical', retrying: 'medium', warn: 'medium', decided: 'ok', stale: 'info', idle: 'info', paused: 'medium', stopped: 'info', discarded: 'info', retried: 'ok', cancelled: 'info', suppressed: 'info', routed: 'accent', recorded: 'info', shed: 'high', dlq: 'critical', degraded: 'medium',
};
export const statusTone = (s) => STATUS_TONE[String(s || '').toLowerCase()] || 'info';

export function badge(label, tone = 'info', opts = {}) {
  return h('span', { class: ['badge', `t-${tone}`, opts.outline && 'outline', opts.sq && 'sq'], title: opts.title }, opts.dot && h('span', { class: ['dot', opts.live && 'live'] }), opts.icon && icon(opts.icon, 'sm'), label);
}
export const sevBadge = (sev) => badge(String(sev || '?').toUpperCase(), SEV_TONE[String(sev).toLowerCase()] || 'info');
export const incBadge = (sev) => badge(sev || 'SEV?', INC_TONE[sev] || 'info', { sq: true });
export const riskBadge = (risk) => badge(`${String(risk || '?').toUpperCase()} RISK`, RISK_TONE[String(risk).toLowerCase()] || 'info');
export const statusBadge = (s, label) => badge(label || String(s || '?'), statusTone(s), { dot: true, live: String(s) === 'running' });
export const chip = (label, opts = {}) => h('span', { class: ['chip', opts.mono && 'mono'], title: opts.title }, label);

// ---- buttons ----------------------------------------------------------------------------------------------------
export function button(label, opts = {}) {
  const el = h(
    opts.href ? 'a' : 'button',
    {
      class: ['btn', opts.variant && `btn-${opts.variant}`, opts.size === 'sm' && 'btn-sm', !label && 'btn-icon', opts.class],
      type: opts.href ? undefined : opts.type || 'button',
      href: opts.href,
      target: opts.external ? '_blank' : undefined,
      rel: opts.external ? 'noopener noreferrer' : undefined,
      title: opts.title,
      'aria-label': opts.aria || (!label ? opts.title : undefined),
      'aria-pressed': opts.pressed === undefined ? undefined : String(!!opts.pressed),
      disabled: opts.disabled,
      onclick: opts.onclick,
    },
    opts.icon && icon(opts.icon, opts.size === 'sm' ? 'sm' : ''),
    label,
  );
  return el;
}
export const iconButton = (name, title, onclick, opts = {}) => button('', { icon: name, title, onclick, ...opts });

/** Run an async handler with a busy state on the button; errors go to `onError`. */
export function asyncClick(btn, fn, onError) {
  btn.addEventListener('click', async (e) => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('busy');
    try {
      await fn(e);
    } catch (err) {
      if (onError) onError(err);
      else console.error(err);
    } finally {
      btn.disabled = false;
      btn.classList.remove('busy');
    }
  });
  return btn;
}

export function copyButton(textOrFn, label = 'Copy', toast) {
  const b = button(label, { size: 'sm', icon: 'copy', title: 'Copy to clipboard' });
  b.addEventListener('click', async () => {
    const t = typeof textOrFn === 'function' ? textOrFn() : textOrFn;
    try {
      await navigator.clipboard.writeText(String(t));
      if (toast) toast({ title: 'Copied', tone: 'ok', ttl: 1800 });
      b.firstChild?.replaceWith(icon('check', 'sm'));
      setTimeout(() => b.firstChild?.replaceWith(icon('copy', 'sm')), 1200);
    } catch {
      window.prompt('Copy:', String(t));
    }
  });
  return b;
}

// ---- cards / kpis -----------------------------------------------------------------------------------------------
export function card({ title, sub, actions, body, foot, class: cls, flush, scroll, tone, bodyClass } = {}) {
  return h(
    'section',
    { class: ['card', tone && `t-${tone}`, cls] },
    (title || actions) &&
      h('header', { class: 'card-head' }, h('div', { class: 'grow' }, h('h3', null, title), sub && h('div', { class: 'sub' }, sub)), actions && h('div', { class: 'row' }, actions)),
    h('div', { class: ['card-body', flush && 'flush', scroll && 'scroll', bodyClass] }, body),
    foot && h('footer', { class: 'card-foot' }, foot),
  );
}

export function kpi({ label, value, sub, tone, href, title }) {
  const inner = [h('div', { class: 'k-label' }, label), h('div', { class: 'k-value' }, value ?? '-'), sub !== undefined && h('div', { class: 'k-sub' }, sub)];
  return href ? h('a', { class: ['kpi', tone && `t-${tone}`, 'k-link'], href, title, style: 'text-decoration:none;color:inherit' }, inner) : h('div', { class: ['kpi', tone && `t-${tone}`], title }, inner);
}

// ---- states -----------------------------------------------------------------------------------------------------
export function skeleton(lines = 3, widths) {
  return h(
    'div',
    { class: 'col', 'aria-busy': 'true', 'aria-label': 'Loading' },
    Array.from({ length: lines }, (_, i) => h('span', { class: 'skeleton', style: { width: (widths?.[i] ?? (i === lines - 1 ? 60 : 92 - (i % 3) * 12)) + '%' } })),
  );
}
export function skeletonBlock(height = 120) {
  return h('div', { class: 'skeleton block', style: { height: height + 'px' }, 'aria-busy': 'true', 'aria-label': 'Loading' });
}
export function skeletonGrid(n = 6, height = 110) {
  return h('div', { class: 'grid auto', 'aria-busy': 'true' }, Array.from({ length: n }, () => skeletonBlock(height)));
}
export function emptyState({ icon: ic = 'inbox', title = 'Nothing here yet', text, action } = {}) {
  return h('div', { class: 'empty' }, icon(ic, 'xl'), h('h4', null, title), text && h('div', { class: 'small' }, text), action);
}

export function errorMessage(err) {
  if (!err) return 'Unknown error';
  if (err instanceof ApiError) {
    if (err.code === 'network') return 'Cannot reach the server. Is it running?';
    if (err.status === 401) return 'Unauthorized. Set the API token in the user menu.';
    if (err.status === 503) return `Service unavailable${err.message ? ': ' + err.message : ''}`;
    return err.message || `HTTP ${err.status}`;
  }
  return err.message || String(err);
}

export function errorBox(err, onRetry, title = 'Something went wrong') {
  return h(
    'div',
    { class: 'errbox', role: 'alert' },
    icon('alert-circle'),
    h('div', { class: 'grow' }, h('div', { class: 'strong' }, title), h('div', { class: 'small' }, errorMessage(err)), err?.code && err.code !== 'network' && h('div', { class: 'tiny muted mono' }, err.code)),
    onRetry && button('Retry', { size: 'sm', icon: 'refresh', onclick: onRetry }),
  );
}

export function notice(text, tone = 'info', ic = 'alert-circle') {
  return h('div', { class: ['notice', `t-${tone}`], role: 'status' }, icon(ic), h('div', { class: 'grow small' }, text));
}

/**
 * Load-then-render with loading / error / empty states and stale-result protection.
 * loader: async () => data ; render: (data) => Node | Node[] ; returns {reload, destroyed}
 */
export function loadInto(container, loader, render, { skeleton: sk, empty, isEmpty } = {}) {
  let token = 0;
  let dead = false;
  async function reload(showSkeleton = true) {
    const mine = ++token;
    if (showSkeleton) mount(container, sk ? sk() : skeleton(4));
    try {
      const data = await loader();
      if (dead || mine !== token) return;
      if (isEmpty && isEmpty(data) && empty) mount(container, empty());
      else mount(container, render(data));
    } catch (err) {
      if (dead || mine !== token) return;
      mount(container, errorBox(err, () => reload(true)));
    }
  }
  reload();
  return { reload, destroy: () => (dead = true) };
}

// ---- tables -----------------------------------------------------------------------------------------------------
/** columns: [{key,label,render?(row),num?,wrap?,class?}] */
export function table({ columns, rows, onRow, rowKey, selected, caption, dense }) {
  const t = h(
    'table',
    { class: ['table', dense && 'dense'] },
    caption && h('caption', { class: 'sr-only' }, caption),
    h('thead', null, h('tr', null, columns.map((c) => h('th', { class: c.num ? 'num' : '', scope: 'col' }, c.label)))),
    h(
      'tbody',
      null,
      rows.map((r) =>
        h(
          'tr',
          {
            class: [onRow && 'clickable', selected && rowKey && selected === rowKey(r) && 'selected'],
            tabindex: onRow ? '0' : undefined,
            onclick: onRow ? () => onRow(r) : undefined,
            onkeydown: onRow ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onRow(r)) : undefined,
          },
          columns.map((c) => h('td', { class: [c.num && 'num', c.wrap && 'wrap', c.class] }, c.render ? c.render(r) : r[c.key] ?? '')),
        ),
      ),
    ),
  );
  return h('div', { class: 'table-wrap' }, t);
}

// ---- tabs / segmented -------------------------------------------------------------------------------------------
export function tabs(items, active, onChange, { label = 'Sections' } = {}) {
  const el = h('div', { class: 'tabs', role: 'tablist', 'aria-label': label });
  const btns = [];
  const select = (key, focus) => {
    btns.forEach((b) => {
      const on = b.dataset.key === key;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    onChange(key);
  };
  for (const it of items) {
    const b = h('button', { class: 'tab', role: 'tab', type: 'button', dataset: { key: it.key }, 'aria-selected': String(it.key === active), tabindex: it.key === active ? '0' : '-1', onclick: () => select(it.key) }, it.label, it.count != null && h('span', { class: 'count', style: 'margin-left:6px;--tone:var(--info)' }, it.count));
    btns.push(b);
    el.appendChild(b);
  }
  el.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const i = btns.findIndex((b) => b.getAttribute('aria-selected') === 'true');
    const n = (i + (e.key === 'ArrowRight' ? 1 : -1) + btns.length) % btns.length;
    select(btns[n].dataset.key, true);
    e.preventDefault();
  });
  el.select = select;
  return el;
}

export function seg(options, value, onChange, { label } = {}) {
  const el = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  const btns = options.map((o) =>
    h('button', {
      type: 'button',
      'aria-pressed': String(o.value === value),
      title: o.title,
      onclick: () => {
        value = o.value;
        btns.forEach((b, i) => b.setAttribute('aria-pressed', String(options[i].value === value)));
        onChange(value);
      },
    }, o.label),
  );
  btns.forEach((b) => el.appendChild(b));
  el.set = (v) => {
    value = v;
    btns.forEach((b, i) => b.setAttribute('aria-pressed', String(options[i].value === value)));
  };
  return el;
}

// ---- form controls ----------------------------------------------------------------------------------------------
export function field(label, control, { hint, error, required, id } = {}) {
  const cid = id || control.id || `f${Math.random().toString(36).slice(2, 8)}`;
  if (control.tagName && !control.id && ['INPUT', 'SELECT', 'TEXTAREA'].includes(control.tagName)) control.id = cid;
  return h('div', { class: 'field' }, label && h('label', { for: control.id || cid }, label, required && h('span', { class: 'req', 'aria-hidden': 'true' }, ' *')), control, hint && h('div', { class: 'hint' }, hint), error && h('div', { class: 'err', role: 'alert' }, error));
}
export function selectEl(options, value, onChange, opts = {}) {
  const el = h('select', { class: ['select', opts.size === 'sm' && 'sm'], 'aria-label': opts.label, onchange: (e) => onChange && onChange(e.target.value) }, options.map((o) => {
    const v = typeof o === 'object' ? o.value : o;
    const l = typeof o === 'object' ? o.label : o;
    return h('option', { value: v, selected: String(v) === String(value) }, l);
  }));
  el.value = value ?? '';
  return el;
}
export function inputEl(opts = {}) {
  return h('input', { class: ['input', opts.size === 'sm' && 'sm', opts.mono && 'mono'], type: opts.type || 'text', value: opts.value ?? '', placeholder: opts.placeholder, 'aria-label': opts.label, min: opts.min, max: opts.max, step: opts.step, oninput: opts.oninput, onchange: opts.onchange, onkeydown: opts.onkeydown, id: opts.id, autocomplete: 'off', spellcheck: 'false' });
}
export function textareaEl(opts = {}) {
  return h('textarea', { class: ['textarea', opts.mono && 'mono'], rows: opts.rows || 4, placeholder: opts.placeholder, 'aria-label': opts.label, oninput: opts.oninput, spellcheck: opts.spellcheck === false ? 'false' : undefined }, opts.value ?? '');
}
export function switchEl(label, checked, onChange) {
  const input = h('input', { type: 'checkbox', checked: !!checked, onchange: (e) => onChange(e.target.checked) });
  if (!checked) input.checked = false;
  return h('label', { class: 'switch' }, input, h('span', { class: 'track' }), h('span', null, label));
}

// ---- drawer / modal ---------------------------------------------------------------------------------------------
let overlayStack = [];

function trapFocus(container) {
  const onKey = (e) => {
    if (e.key !== 'Tab') return;
    const f = container.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])');
    if (!f.length) return;
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      last.focus();
      e.preventDefault();
    } else if (!e.shiftKey && document.activeElement === last) {
      first.focus();
      e.preventDefault();
    }
  };
  container.addEventListener('keydown', onKey);
}

function overlay(kind, { title, body, wide, onClose, actions, labelledBy }) {
  const prev = document.activeElement;
  const scrim = h('div', { class: 'scrim', onclick: () => api.close() });
  const titleId = `ov${Math.random().toString(36).slice(2, 7)}`;
  const el = h(
    'div',
    { class: [kind, wide && 'wide'], role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId },
    h('div', { class: 'drawer-head' }, h('h3', { id: titleId, class: 'grow truncate' }, title), actions, iconButton('x', 'Close', () => api.close(), { size: 'sm', variant: 'ghost' })),
    h('div', { class: 'drawer-body' }, body),
  );
  const onKey = (e) => {
    if (e.key === 'Escape' && overlayStack[overlayStack.length - 1] === api) {
      e.stopPropagation();
      api.close();
    }
  };
  const api = {
    el,
    body: el.querySelector('.drawer-body'),
    close() {
      document.removeEventListener('keydown', onKey, true);
      scrim.remove();
      el.remove();
      overlayStack = overlayStack.filter((o) => o !== api);
      if (prev && prev.focus) prev.focus();
      if (onClose) onClose();
    },
    setBody(...kids) {
      mount(api.body, ...kids);
    },
    setTitle(t) {
      el.querySelector(`#${titleId}`).textContent = t;
    },
  };
  void labelledBy;
  overlayStack.push(api);
  document.addEventListener('keydown', onKey, true);
  document.body.append(scrim, el);
  trapFocus(el);
  (el.querySelector('button[aria-label="Close"]') || el).focus();
  return api;
}
export const drawer = (opts) => overlay('drawer', opts);
/** Close every open drawer/modal (called on route changes). */
export function closeAllOverlays() {
  for (const o of [...overlayStack].reverse()) o.close();
}
export const modal = (opts) => overlay('modal', opts);

export function confirmDialog({ title, text, confirmLabel = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      m.close();
      resolve(v);
    };
    const m = modal({
      title,
      onClose: () => !settled && ((settled = true), resolve(false)),
      body: h('div', { class: 'col gap-3' }, h('p', null, text), h('div', { class: 'row', style: 'justify-content:flex-end' }, button('Cancel', { onclick: () => done(false) }), button(confirmLabel, { variant: danger ? 'danger' : 'primary', onclick: () => done(true) }))),
    });
  });
}

// ---- JSON viewer ------------------------------------------------------------------------------------------------
export function jsonViewer(value, { open = 2, maxStr = 600, class: cls } = {}) {
  const root = h('div', { class: ['jv', cls], role: 'tree' });
  root.appendChild(renderJson(value, 0, open, maxStr, null));
  return root;
}
function scalar(v, maxStr) {
  if (v === null) return h('span', { class: 'jv-null' }, 'null');
  if (typeof v === 'string') return h('span', { class: 'jv-str' }, JSON.stringify(v.length > maxStr ? v.slice(0, maxStr) + '…' : v));
  if (typeof v === 'number') return h('span', { class: 'jv-num' }, String(v));
  if (typeof v === 'boolean') return h('span', { class: 'jv-bool' }, String(v));
  return h('span', { class: 'jv-null' }, String(v));
}
function renderJson(v, depth, open, maxStr, key) {
  const keyEl = key !== null && h('span', { class: 'jv-key' }, `${key}`);
  const sep = key !== null && h('span', { class: 'jv-meta' }, ': ');
  if (v === null || typeof v !== 'object') return h('div', { class: 'jv-line' }, keyEl, sep, scalar(v, maxStr));
  const isArr = Array.isArray(v);
  const entries = isArr ? v.map((x, i) => [i, x]) : Object.entries(v);
  if (!entries.length) return h('div', { class: 'jv-line' }, keyEl, sep, h('span', { class: 'jv-meta' }, isArr ? '[]' : '{}'));
  const d = h('details', { open: depth < open }, h('summary', null, keyEl, sep, h('span', { class: 'jv-meta' }, isArr ? `[${entries.length}]` : `{${entries.length}}`)));
  if (entries.length > 200) {
    for (const [k, x] of entries.slice(0, 200)) d.appendChild(renderJson(x, depth + 1, open, maxStr, k));
    d.appendChild(h('div', { class: 'jv-meta' }, `… ${formatNumber(entries.length - 200)} more`));
  } else for (const [k, x] of entries) d.appendChild(renderJson(x, depth + 1, open, maxStr, k));
  return d;
}

// ---- markdown renderer ------------------------------------------------------------------------------------------
export function renderInline(parts) {
  return parts.map((p) => {
    switch (p.t) {
      case 'code':
        return h('code', null, p.text);
      case 'strong':
        return h('strong', null, p.text);
      case 'em':
        return h('em', null, p.text);
      case 'link':
        return h('a', { href: p.href, target: p.href.startsWith('#') ? undefined : '_blank', rel: 'noopener noreferrer' }, p.text);
      default:
        return p.text;
    }
  });
}
export function markdown(src) {
  const root = h('div', { class: 'md' });
  for (const b of parseMarkdown(src)) {
    switch (b.type) {
      case 'heading':
        root.appendChild(h('h' + Math.min(6, b.level + 1), null, renderInline(b.inline)));
        break;
      case 'p':
        root.appendChild(h('p', null, renderInline(b.inline)));
        break;
      case 'list':
        root.appendChild(h(b.ordered ? 'ol' : 'ul', null, b.items.map((it) => h('li', null, renderInline(it)))));
        break;
      case 'code':
        root.appendChild(h('pre', null, h('code', null, b.text)));
        break;
      case 'quote':
        root.appendChild(h('blockquote', null, renderInline(b.inline)));
        break;
      case 'rule':
        root.appendChild(h('hr'));
        break;
      case 'table':
        root.appendChild(h('table', null, h('thead', null, h('tr', null, b.head.map((c) => h('th', null, renderInline(c))))), h('tbody', null, b.rows.map((r) => h('tr', null, r.map((c) => h('td', null, renderInline(c))))))));
        break;
      default:
        break;
    }
  }
  return root;
}

// ---- view scaffolding -------------------------------------------------------------------------------------------
export function viewHead({ title, sub, actions }) {
  return h('div', { class: 'view-head' }, h('div', { class: 'titles' }, h('h1', null, title), sub && h('div', { class: 'sub' }, sub)), actions && h('div', { class: 'actions' }, actions));
}

export { h, clear, mount, icon };
