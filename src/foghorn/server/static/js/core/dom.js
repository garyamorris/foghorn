// Tiny DOM builder. Strings become TEXT NODES (never innerHTML): log lines contain prompt injection by design.

const SVG_NS = 'http://www.w3.org/2000/svg';
const BOOL_ATTRS = new Set(['disabled', 'checked', 'selected', 'readonly', 'required', 'hidden', 'open', 'multiple', 'autofocus']);

function applyProps(el, props, isSvg) {
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class' || k === 'className') {
      const cls = Array.isArray(v) ? v.filter(Boolean).join(' ') : v;
      if (isSvg) el.setAttribute('class', cls);
      else el.className = cls;
    } else if (k === 'style') {
      if (typeof v === 'string') el.style.cssText = v;
      else for (const [sk, sv] of Object.entries(v)) {
        if (sv === undefined || sv === null) continue;
        if (sk.startsWith('--')) el.style.setProperty(sk, sv);
        else el.style[sk] = sv;
      }
    } else if (k === 'dataset') {
      for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    } else if (k === 'text') {
      el.textContent = v;
    } else if (k.startsWith('on') && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'value' && !isSvg) {
      el.value = v;
    } else if (BOOL_ATTRS.has(k) && !isSvg) {
      el[k] = true;
      if (k !== 'hidden' && k !== 'open') el.setAttribute(k, '');
    } else if (k === 'ref' && typeof v === 'function') {
      v(el);
    } else {
      el.setAttribute(k, v === true ? '' : String(v));
    }
  }
}

function append(el, kids) {
  for (const kid of kids) {
    if (kid === undefined || kid === null || kid === false || kid === true) continue;
    if (Array.isArray(kid)) append(el, kid);
    else if (typeof kid === 'string' || typeof kid === 'number') el.appendChild(document.createTextNode(String(kid)));
    else el.appendChild(kid);
  }
}

/** h('div', {class:'x', onclick}, 'text', child, [more]) */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props && (typeof props !== 'object' || props.nodeType || Array.isArray(props))) {
    kids.unshift(props);
    props = null;
  }
  if (props) applyProps(el, props, false);
  append(el, kids);
  return el;
}

export function svg(tag, props, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  if (props && (typeof props !== 'object' || props.nodeType || Array.isArray(props))) {
    kids.unshift(props);
    props = null;
  }
  if (props) applyProps(el, props, true);
  append(el, kids);
  return el;
}

export function frag(...kids) {
  const f = document.createDocumentFragment();
  append(f, kids);
  return f;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** Replace the children of `el`. */
export function mount(el, ...kids) {
  clear(el);
  append(el, kids);
  return el;
}

export function text(s) {
  return document.createTextNode(s == null ? '' : String(s));
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Run `fn` at most once per animation frame (coalesce streaming re-renders). */
export function rafThrottle(fn) {
  let queued = false;
  let lastArgs;
  const run = () => {
    queued = false;
    fn(...lastArgs);
  };
  const t = (...args) => {
    lastArgs = args;
    if (queued) return;
    queued = true;
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    else setTimeout(run, 16);
  };
  t.flush = () => {
    if (queued) run();
  };
  return t;
}

export function debounce(fn, ms = 200) {
  let t;
  const d = (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
  d.cancel = () => clearTimeout(t);
  return d;
}

/** Keep a scroll container pinned to the bottom unless the user scrolled up. */
export function stickToBottom(el, threshold = 48) {
  let pinned = true;
  const onScroll = () => {
    pinned = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  };
  el.addEventListener('scroll', onScroll, { passive: true });
  return {
    follow() {
      if (pinned) el.scrollTop = el.scrollHeight;
    },
    get pinned() {
      return pinned;
    },
    pin() {
      pinned = true;
      el.scrollTop = el.scrollHeight;
    },
  };
}
