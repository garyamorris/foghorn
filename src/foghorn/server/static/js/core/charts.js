// DOM renderers for charts (inline SVG only). The math lives in chartmath.js.
import { svg } from './dom.js';
import { lineLayout, niceTicks, normalizePoints, downsample, round } from './chartmath.js';
import { formatClock, formatMetric, formatNumber } from './format.js';

/** Compact sparkline. opts: {width,height,baseline,onset,bad,label,responsive} */
export function sparkline(points, opts = {}) {
  const width = opts.width ?? 140;
  const height = opts.height ?? 36;
  const pts = downsample(normalizePoints(points), 90);
  const l = lineLayout(
    pts.map((p) => [p.t, p.v]),
    { width, height, baseline: opts.baseline, onset: opts.onset, pad: { t: 3, r: 4, b: 3, l: 2 } },
  );
  const el = svg('svg', {
    class: 'spark' + (opts.bad ? ' bad' : ''),
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': opts.label || 'trend',
    preserveAspectRatio: 'none',
    style: opts.responsive ? { width: '100%', height: height + 'px' } : { width: width + 'px', height: height + 'px' },
  });
  if (l.empty) return el;
  if (l.baselineY !== null) el.appendChild(svg('line', { class: 'baseline', x1: 0, x2: width, y1: l.baselineY, y2: l.baselineY }));
  if (l.onsetX !== null) el.appendChild(svg('line', { class: 'onset', x1: l.onsetX, x2: l.onsetX, y1: 0, y2: height }));
  el.appendChild(svg('path', { class: 'area', d: l.area }));
  el.appendChild(svg('path', { class: 'line', d: l.path }));
  el.appendChild(svg('circle', { class: 'dotlast', cx: l.last.x, cy: l.last.y, r: 2.4 }));
  return el;
}

/** Full line chart with axes. opts: {width,height,baseline,onset,metric,bad,title} */
export function lineChart(points, opts = {}) {
  const width = opts.width ?? 560;
  const height = opts.height ?? 180;
  const pad = { t: 10, r: 12, b: 22, l: 44 };
  const pts = downsample(normalizePoints(points), 240);
  const l = lineLayout(
    pts.map((p) => [p.t, p.v]),
    { width, height, baseline: opts.baseline, onset: opts.onset, pad, padRatio: 0.1 },
  );
  const el = svg('svg', {
    class: 'spark' + (opts.bad ? ' bad' : ''),
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': opts.title || 'time series',
    style: { width: '100%', height: 'auto', maxHeight: height + 'px' },
  });
  if (l.empty) {
    el.appendChild(svg('text', { x: width / 2, y: height / 2, 'text-anchor': 'middle', fill: 'currentColor', class: 'muted' }, 'no data'));
    return el;
  }
  const axis = svg('g', { class: 'chart-axis' });
  const ticks = niceTicks(l.yDomain[0], l.yDomain[1], 4);
  for (const t of ticks) {
    const y = round(l.y(t));
    if (y < pad.t - 1 || y > height - pad.b + 1) continue;
    axis.appendChild(svg('line', { x1: pad.l, x2: width - pad.r, y1: y, y2: y }));
    axis.appendChild(svg('text', { x: pad.l - 6, y: y + 3, 'text-anchor': 'end' }, opts.metric ? shortMetric(opts.metric, t) : formatNumber(t, Math.abs(t) < 10 ? 1 : 0)));
  }
  const [x0, x1] = l.xDomain;
  for (const f of [0, 0.5, 1]) {
    const tms = x0 + (x1 - x0) * f;
    const x = round(l.x(tms));
    axis.appendChild(svg('text', { x, y: height - 6, 'text-anchor': f === 0 ? 'start' : f === 1 ? 'end' : 'middle' }, formatClock(new Date(tms).toISOString(), false)));
  }
  el.appendChild(axis);
  if (l.baselineY !== null) el.appendChild(svg('line', { class: 'baseline', x1: pad.l, x2: width - pad.r, y1: l.baselineY, y2: l.baselineY }));
  if (l.onsetX !== null) {
    el.appendChild(svg('line', { class: 'onset', x1: l.onsetX, x2: l.onsetX, y1: pad.t, y2: height - pad.b }));
    el.appendChild(svg('text', { x: l.onsetX + 4, y: pad.t + 8, fill: 'var(--critical)', style: 'font-size:10px' }, 'onset'));
  }
  el.appendChild(svg('path', { class: 'area', d: l.area }));
  el.appendChild(svg('path', { class: 'line', d: l.path }));
  el.appendChild(svg('circle', { class: 'dotlast', cx: l.last.x, cy: l.last.y, r: 3 }));
  return el;
}

function shortMetric(metric, v) {
  const s = formatMetric(metric, v);
  return s.length > 7 ? s.replace(' ms', 'ms') : s;
}

/** Horizontal share bar with segments [{value,tone,label}] */
export function stackBar(segments, { height = 8 } = {}) {
  const total = segments.reduce((a, s) => a + Math.max(0, s.value), 0) || 1;
  const el = svg('svg', { viewBox: '0 0 100 ' + height, preserveAspectRatio: 'none', style: { width: '100%', height: height + 'px', borderRadius: '999px', overflow: 'hidden', display: 'block' }, role: 'img', 'aria-label': segments.map((s) => `${s.label}: ${s.value}`).join(', ') });
  el.appendChild(svg('rect', { x: 0, y: 0, width: 100, height, fill: 'var(--surface-3)' }));
  let x = 0;
  for (const s of segments) {
    const w = (Math.max(0, s.value) / total) * 100;
    el.appendChild(svg('rect', { x: round(x), y: 0, width: round(w), height, fill: `var(--${s.tone || 'accent'})` }));
    x += w;
  }
  return el;
}
