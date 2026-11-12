// fhlib.js: tiny shared helpers for the Foghorn explainer pages (overview / streams / evals).
// Every page is a pure function of time: render(t) puts the DOM into the state it has at animation time t (default seconds).
// play() drives it in real time, or (?seek=S) draws one still frame; ?fit=S warps the whole animation to S seconds.
export const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
export const lerp = (a, b, t) => a + (b - a) * t;
export const easeOut = (t) => 1 - Math.pow(1 - clamp(t), 3);
export const easeInOut = (t) => { t = clamp(t); return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
export const easeOutBack = (t) => { t = clamp(t); const c1 = 1.5, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); };
/** progress 0..1 of a phase that starts at t0 and lasts d seconds */
export const ph = (t, t0, d) => clamp((t - t0) / d);
export const NS = 'http://www.w3.org/2000/svg';
export function svgEl(name, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
export function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const k in attrs) {
    if (k === 'class') e.className = attrs[k];
    else if (k === 'html') e.innerHTML = attrs[k];
    else if (k === 'style') e.style.cssText = attrs[k];
    else e.setAttribute(k, attrs[k]);
  }
  for (const c of kids) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
export const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
export const q = new URLSearchParams(location.search);

/**
 * play({duration, beats, render, extra}): duration in default seconds; beats = [{t, name, ...}] (default seconds).
 * window.__meta = {duration, beats[], ...extra} is expressed in OUTPUT seconds (after ?fit). render(t) receives DEFAULT seconds.
 */
export function play({ duration, beats, render, extra = {} }) {
  const fit = parseFloat(q.get('fit'));
  const warp = fit > 0 ? fit / duration : 1;
  window.__meta = { duration: +(duration * warp).toFixed(3), default_duration: duration, warp: +warp.toFixed(4), beats: beats.map((b) => ({ ...b, t: +(b.t * warp).toFixed(3) })), ...extra };
  const total = duration * warp;
  const drawOut = (tOut) => { window.__t = tOut; render(Math.max(0, Math.min(duration, tOut / warp))); };
  window.__seek = drawOut;
  const seek = q.get('seek');
  if (seek !== null && seek !== '') { drawOut(parseFloat(seek) || 0); window.__ready = true; window.__done = true; return; }
  drawOut(0);
  window.__ready = true;
  let t0 = null;
  const tick = (now) => {
    if (t0 === null) t0 = now;
    const tOut = (now - t0) / 1000;
    if (tOut >= total) { drawOut(total); window.__done = true; return; }
    drawOut(tOut);
    requestAnimationFrame(tick);
  };
  whenRecording(() => requestAnimationFrame(tick));
}
/** Start the clock when the `rec` recorder starts recording, so no animation time is lost before the first video frame.
 *  rec.t() counts from page load until recording starts, then restarts at 0: a drop means "recording began". Without a recorder
 *  (normal browser) or if no restart is seen within 4.5 s (rec --shot / --eval) it starts at once / after the timeout. */
function whenRecording(cb) {
  const R = window.rec;
  if (!R || typeof R.t !== 'function') { cb(); return; }
  let last, fired = false;
  const t00 = performance.now();
  const go = () => { if (!fired) { fired = true; cb(); } };
  try { last = R.t(); } catch (e) { go(); return; }
  const poll = () => {
    if (fired) return;
    let c;
    try { c = R.t(); } catch (e) { go(); return; }
    if (c < last - 0.2 || performance.now() - t00 > 4500) { go(); return; }
    last = c;
    setTimeout(poll, 8);
  };
  poll();
}
export async function getJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`GET ${url} -> ${r.status}`);
  return r.json();
}
window.__done = false;
