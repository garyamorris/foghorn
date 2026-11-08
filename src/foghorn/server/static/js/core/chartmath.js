// SVG chart math. Pure functions that turn data into coordinates / path strings; no DOM.

export function extent(values, { includeZero = false } = {}) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v === null || v === undefined || Number.isNaN(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (lo === Infinity) return [0, 1];
  if (includeZero) {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
  }
  if (lo === hi) {
    const d = lo === 0 ? 1 : Math.abs(lo) * 0.1;
    return [lo - d, hi + d];
  }
  return [lo, hi];
}

export function linearScale(d0, d1, r0, r1) {
  const span = d1 - d0;
  const f = (x) => (span === 0 ? (r0 + r1) / 2 : r0 + ((x - d0) / span) * (r1 - r0));
  f.invert = (y) => (r1 === r0 ? d0 : d0 + ((y - r0) / (r1 - r0)) * span);
  f.domain = [d0, d1];
  f.range = [r0, r1];
  return f;
}

/** "Nice" tick values covering [lo, hi] with about `count` ticks. */
export function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) return [lo];
  const rawStep = (hi - lo) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const first = Math.ceil(lo / step) * step;
  const ticks = [];
  for (let v = first; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

/** Normalise [[t, v], ...] points (t may be ISO string or number) to [{t:number, v:number}] sorted by t. */
export function normalizePoints(points) {
  const out = [];
  for (const p of points || []) {
    let t;
    let v;
    if (Array.isArray(p)) [t, v] = p;
    else if (p && typeof p === 'object') {
      t = p.t ?? p.ts ?? p.x;
      v = p.v ?? p.value ?? p.y;
    } else continue;
    const tn = typeof t === 'number' ? t : Date.parse(t);
    const vn = Number(v);
    if (Number.isNaN(tn) || Number.isNaN(vn)) continue;
    out.push({ t: tn, v: vn });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

/** Min/max-preserving downsample to at most `max` points (keeps spikes visible). */
export function downsample(points, max = 120) {
  if (points.length <= max) return points;
  const bucket = points.length / (max / 2);
  const out = [];
  for (let i = 0; i < points.length; i += bucket) {
    const slice = points.slice(Math.floor(i), Math.floor(i + bucket));
    if (!slice.length) continue;
    let lo = slice[0];
    let hi = slice[0];
    for (const p of slice) {
      if (p.v < lo.v) lo = p;
      if (p.v > hi.v) hi = p;
    }
    if (lo.t <= hi.t) out.push(lo, hi);
    else out.push(hi, lo);
  }
  return out;
}

/**
 * Layout for a line chart / sparkline.
 * opts: {width,height,pad:{t,r,b,l}, baseline?:number, onset?: number|string, yMin?, yMax?}
 * Returns {path, area, pts, x, y, baselineY, onsetX, yDomain, xDomain, last}
 */
export function lineLayout(rawPoints, opts = {}) {
  const width = opts.width ?? 120;
  const height = opts.height ?? 32;
  const pad = { t: 2, r: 2, b: 2, l: 2, ...(opts.pad || {}) };
  const pts = normalizePoints(rawPoints);
  if (!pts.length) return { path: '', area: '', pts: [], x: null, y: null, empty: true, width, height, pad };
  const vals = pts.map((p) => p.v);
  if (opts.baseline !== undefined && opts.baseline !== null) vals.push(opts.baseline);
  let [y0, y1] = extent(vals);
  if (opts.yMin !== undefined) y0 = opts.yMin;
  if (opts.yMax !== undefined) y1 = opts.yMax;
  if (opts.padRatio !== 0) {
    const r = (y1 - y0) * (opts.padRatio ?? 0.08);
    if (opts.yMin === undefined) y0 -= r;
    if (opts.yMax === undefined) y1 += r;
  }
  const x0 = pts[0].t;
  const x1 = pts[pts.length - 1].t;
  const x = linearScale(x0, x1 === x0 ? x0 + 1 : x1, pad.l, width - pad.r);
  const y = linearScale(y0, y1, height - pad.b, pad.t);
  const coords = pts.map((p) => [round(x(p.t)), round(y(p.v))]);
  const path = coords.map((c, i) => (i === 0 ? 'M' : 'L') + c[0] + ' ' + c[1]).join('');
  const bottom = round(height - pad.b);
  const area = path + `L${coords[coords.length - 1][0]} ${bottom}L${coords[0][0]} ${bottom}Z`;
  let onsetX = null;
  if (opts.onset !== undefined && opts.onset !== null) {
    const ot = typeof opts.onset === 'number' ? opts.onset : Date.parse(opts.onset);
    if (!Number.isNaN(ot) && ot >= x0 && ot <= x1) onsetX = round(x(ot));
  }
  return {
    path,
    area,
    pts,
    coords,
    x,
    y,
    width,
    height,
    pad,
    baselineY: opts.baseline !== undefined && opts.baseline !== null ? round(y(opts.baseline)) : null,
    onsetX,
    yDomain: [y0, y1],
    xDomain: [x0, x1],
    last: { x: coords[coords.length - 1][0], y: coords[coords.length - 1][1], v: pts[pts.length - 1].v },
  };
}

export function round(n, d = 2) {
  const f = Math.pow(10, d);
  return Math.round(n * f) / f;
}

/** Horizontal bar layout: values in [0,max] -> widths. */
export function barLayout(items, { width = 200, max } = {}) {
  const m = max ?? Math.max(1e-9, ...items.map((i) => Math.abs(i.value)));
  return items.map((it) => ({ ...it, w: round(Math.min(1, Math.abs(it.value) / m) * width) }));
}

/** Stacked/segmented bar: [{value, tone}] -> [{x, w}] */
export function segmentLayout(segments, width = 100) {
  const total = segments.reduce((a, s) => a + Math.max(0, s.value), 0) || 1;
  let x = 0;
  return segments.map((s) => {
    const w = (Math.max(0, s.value) / total) * width;
    const r = { ...s, x: round(x), w: round(w) };
    x += w;
    return r;
  });
}

/** Mini gauge arc: fraction 0..1 -> SVG arc path over a half circle. */
export function arcPath(cx, cy, r, fraction, start = Math.PI, sweep = Math.PI) {
  const f = Math.max(0, Math.min(1, fraction));
  const a0 = start;
  const a1 = start + sweep * f;
  const p = (a) => [round(cx + r * Math.cos(a)), round(cy + r * Math.sin(a))];
  const [x0, y0] = p(a0);
  const [x1, y1] = p(a1);
  const large = sweep * f > Math.PI ? 1 : 0;
  return `M${x0} ${y0}A${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

/** Heat colour class index 0..4 for a ratio. */
export function heatLevel(ratio) {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return 0;
  if (ratio < 0.25) return 1;
  if (ratio < 0.5) return 2;
  if (ratio < 0.8) return 3;
  return 4;
}

/** Z-score of the last point against the earlier ones (used for highlighting anomalies). */
export function zScore(points) {
  const vs = normalizePoints(points).map((p) => p.v);
  if (vs.length < 4) return 0;
  const last = vs[vs.length - 1];
  const base = vs.slice(0, -1);
  const mean = base.reduce((a, b) => a + b, 0) / base.length;
  const sd = Math.sqrt(base.reduce((a, b) => a + (b - mean) ** 2, 0) / base.length) || 1e-9;
  return (last - mean) / sd;
}
