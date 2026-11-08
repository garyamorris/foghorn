// Structural JSON diff for checkpoint comparison. Pure.
//
// diffJson(a, b) -> [{path:'incident.severity', kind:'added'|'removed'|'changed', before, after}]
// Arrays of objects with an `id` are matched by id (so a re-ordered hypotheses list does not look rewritten);
// other arrays are compared by index.

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

export function diffJson(a, b, path = '', out = [], depth = 0) {
  if (depth > 12) {
    if (JSON.stringify(a) !== JSON.stringify(b)) out.push({ path, kind: 'changed', before: a, after: b });
    return out;
  }
  if (isObj(a) && isObj(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of [...keys].sort()) {
      const p = path ? `${path}.${k}` : k;
      if (!(k in a)) out.push({ path: p, kind: 'added', after: b[k] });
      else if (!(k in b)) out.push({ path: p, kind: 'removed', before: a[k] });
      else diffJson(a[k], b[k], p, out, depth + 1);
    }
    return out;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    const keyed = a.every((x) => isObj(x) && x.id !== undefined) && b.every((x) => isObj(x) && x.id !== undefined) && (a.length || b.length);
    if (keyed) {
      const am = new Map(a.map((x) => [String(x.id), x]));
      const bm = new Map(b.map((x) => [String(x.id), x]));
      for (const id of new Set([...am.keys(), ...bm.keys()])) {
        const p = `${path}[id=${id}]`;
        if (!am.has(id)) out.push({ path: p, kind: 'added', after: bm.get(id) });
        else if (!bm.has(id)) out.push({ path: p, kind: 'removed', before: am.get(id) });
        else diffJson(am.get(id), bm.get(id), p, out, depth + 1);
      }
      return out;
    }
    const n = Math.max(a.length, b.length);
    for (let i = 0; i < n; i++) {
      const p = `${path}[${i}]`;
      if (i >= a.length) out.push({ path: p, kind: 'added', after: b[i] });
      else if (i >= b.length) out.push({ path: p, kind: 'removed', before: a[i] });
      else diffJson(a[i], b[i], p, out, depth + 1);
    }
    return out;
  }
  if (!Object.is(a, b) && JSON.stringify(a) !== JSON.stringify(b)) out.push({ path, kind: 'changed', before: a, after: b });
  return out;
}

/**
 * Build the `values` payload for POST /threads/{id}/state: only top-level keys that changed (unchanged list keys
 * would be re-appended by their reducers). Changed arrays that already existed are wrapped as an Overwrite when
 * `overwriteLists` is set, so the edit REPLACES the list instead of appending.
 */
export function buildStateUpdate(original, edited, { overwriteLists = true } = {}) {
  const values = {};
  const notes = [];
  for (const k of Object.keys(edited)) {
    if (JSON.stringify(edited[k]) === JSON.stringify(original?.[k])) continue;
    if (overwriteLists && Array.isArray(edited[k]) && Array.isArray(original?.[k])) {
      values[k] = { type: '__overwrite__', value: edited[k] };
      notes.push(`${k}: list replaced (Overwrite)`);
    } else {
      values[k] = edited[k];
      if (Array.isArray(edited[k])) notes.push(`${k}: list goes through its reducer (may append)`);
    }
  }
  for (const k of Object.keys(original || {})) if (!(k in edited)) notes.push(`${k}: removal is not supported by update_state and is ignored`);
  return { values, notes, changedKeys: Object.keys(values) };
}

/** Group a thread's checkpoint list into parent -> children to mark forks. */
export function forkInfo(history) {
  const children = new Map();
  for (const s of history) {
    const parent = s.parent_checkpoint?.checkpoint_id || s.parent_config?.configurable?.checkpoint_id || null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(s);
  }
  const forks = new Set();
  for (const [parent, kids] of children) if (parent && kids.length > 1) forks.add(parent);
  return { children, forks };
}

export function checkpointId(s) {
  return s?.checkpoint?.checkpoint_id || s?.config?.configurable?.checkpoint_id || null;
}
