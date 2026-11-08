// Light client state: localStorage wrapper (always inside try/catch) and a tiny pub/sub.

const PREFIX = 'foghorn.';

export function lsGet(key, fallback = null) {
  try {
    const raw = globalThis.localStorage?.getItem(PREFIX + key);
    return raw === null || raw === undefined ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function lsSet(key, value) {
  try {
    const st = globalThis.localStorage;
    if (!st) return false;
    st.setItem(PREFIX + key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

export function lsRemove(key) {
  try {
    globalThis.localStorage?.removeItem(PREFIX + key);
  } catch {
    /* storage unavailable */
  }
}

export function createEmitter() {
  const map = new Map();
  return {
    on(type, fn) {
      if (!map.has(type)) map.set(type, new Set());
      map.get(type).add(fn);
      return () => map.get(type)?.delete(fn);
    },
    emit(type, payload) {
      const set = map.get(type);
      if (!set) return;
      for (const fn of [...set]) {
        try {
          fn(payload);
        } catch (e) {
          console.error('listener error', type, e);
        }
      }
    },
    clear() {
      map.clear();
    },
  };
}

/** Observable value with optional persistence. */
export function createState(initial, persistKey) {
  let value = persistKey ? lsGet(persistKey, initial) : initial;
  const subs = new Set();
  return {
    get: () => value,
    set(next) {
      value = typeof next === 'function' ? next(value) : next;
      if (persistKey) lsSet(persistKey, value);
      for (const fn of [...subs]) fn(value);
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}
