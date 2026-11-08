// Evaluation math: run comparison, aggregation, score banding. Pure.

const num = (x) => (typeof x === 'number' && Number.isFinite(x) ? x : null);

/** Index a run's cases by scenario id. */
export function caseMap(run) {
  const m = new Map();
  for (const c of run?.cases || []) m.set(c.scenario_id, c);
  return m;
}

export function mean(values) {
  const v = values.filter((x) => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

/**
 * Compare run A (candidate) against B (baseline).
 * status: regression (A worse by > threshold, or pass -> fail) | improvement | same | only-a | only-b
 */
export function compareRuns(a, b, { threshold = 0.05 } = {}) {
  const am = caseMap(a);
  const bm = caseMap(b);
  const ids = [...new Set([...am.keys(), ...bm.keys()])].sort((x, y) => String(x).localeCompare(String(y), undefined, { numeric: true }));
  const rows = ids.map((id) => {
    const ca = am.get(id);
    const cb = bm.get(id);
    const sa = num(ca?.score);
    const sb = num(cb?.score);
    const delta = sa !== null && sb !== null ? sa - sb : null;
    let status = 'same';
    if (!ca) status = 'only-b';
    else if (!cb) status = 'only-a';
    else if ((cb.passed && ca.passed === false) || (delta !== null && delta < -threshold)) status = 'regression';
    else if ((cb.passed === false && ca.passed) || (delta !== null && delta > threshold)) status = 'improvement';
    return { scenario_id: id, a: sa, b: sb, delta, passedA: ca?.passed ?? null, passedB: cb?.passed ?? null, status, tier: ca?.tier ?? cb?.tier ?? null, category: ca?.category ?? cb?.category ?? null };
  });
  const meanA = mean(rows.map((r) => r.a));
  const meanB = mean(rows.map((r) => r.b));
  return {
    rows,
    meanA,
    meanB,
    meanDelta: meanA !== null && meanB !== null ? meanA - meanB : null,
    regressions: rows.filter((r) => r.status === 'regression').length,
    improvements: rows.filter((r) => r.status === 'improvement').length,
  };
}

/** Aggregate comparison rows by a key (tier, category). */
export function aggregate(rows, keyFn) {
  const groups = new Map();
  for (const r of rows) {
    const k = keyFn(r);
    if (k === null || k === undefined || k === '') continue;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }
  return [...groups.entries()]
    .map(([key, rs]) => {
      const a = mean(rs.map((r) => r.a));
      const b = mean(rs.map((r) => r.b));
      return { key, n: rs.length, a, b, delta: a !== null && b !== null ? a - b : null };
    })
    .sort((x, y) => String(x.key).localeCompare(String(y.key), undefined, { numeric: true }));
}

/** 'good' >= 0.8, 'ok' >= 0.5, 'bad' otherwise; 'none' for missing. */
export function band(score) {
  if (score === null || score === undefined || Number.isNaN(score)) return 'none';
  if (score >= 0.8) return 'good';
  if (score >= 0.5) return 'ok';
  return 'bad';
}

/** Progress of a run as a fraction. */
export function progressOf(run) {
  const p = run?.progress;
  if (p && p.total) return Math.min(1, (p.done || 0) / p.total);
  if (run?.status === 'finished') return 1;
  return 0;
}

/** Evaluators that appear in at least one case's `scores` map, in a stable order. */
export function evaluatorColumns(run) {
  const set = new Set();
  for (const c of run?.cases || []) for (const k of Object.keys(c.scores || {})) set.add(k);
  return [...set].sort();
}
