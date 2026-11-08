import { lsGet, lsSet } from './store.js';

/** theme: 'auto' | 'light' | 'dark'. 'auto' follows prefers-color-scheme. */
export function getTheme() {
  return lsGet('theme', 'auto');
}

export function applyTheme(theme = getTheme()) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
  else root.removeAttribute('data-theme');
}

export function effectiveTheme() {
  const t = getTheme();
  if (t === 'light' || t === 'dark') return t;
  return globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function toggleTheme() {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  lsSet('theme', next);
  applyTheme(next);
  return next;
}
