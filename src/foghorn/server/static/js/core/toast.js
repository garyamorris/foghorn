import { h } from './dom.js';
import { icon } from './icons.js';

let host = null;
const TONE_ICON = { ok: 'check-circle', accent: 'inbox', info: 'alert-circle', medium: 'alert', high: 'alert', critical: 'alert-circle', danger: 'alert-circle' };

export function initToasts() {
  if (host) return host;
  host = h('div', { class: 'toasts', role: 'region', 'aria-label': 'Notifications', 'aria-live': 'polite' });
  document.body.appendChild(host);
  return host;
}

/** toast({title, body, tone, ttl, action:{label,href|onclick}}) */
export function toast({ title, body, tone = 'accent', ttl = 7000, action } = {}) {
  initToasts();
  const close = () => el.remove();
  const el = h(
    'div',
    { class: ['toast', `t-${tone}`], role: 'status' },
    icon(TONE_ICON[tone] || 'alert-circle'),
    h('div', { class: 'grow' }, h('div', { class: 't-title' }, title), body && h('div', { class: 't-body' }, body), action && h('div', { style: 'margin-top:6px' }, h(action.href ? 'a' : 'button', { class: 'btn btn-sm', href: action.href, onclick: (e) => { if (action.onclick) action.onclick(e); close(); } }, action.label))),
    h('button', { class: 'btn btn-ghost btn-sm btn-icon', 'aria-label': 'Dismiss', onclick: close }, icon('x', 'sm')),
  );
  host.appendChild(el);
  while (host.children.length > 4) host.firstChild.remove();
  if (ttl) setTimeout(close, ttl);
  return { close };
}
