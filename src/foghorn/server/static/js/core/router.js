// Hash router logic. Pure: parsing and matching only (the DOM glue lives in main.js).
//
//   #/incidents/INC-D0003?thread=abc&tab=raw   ->  { path:'/incidents/INC-D0003', segments:[...], query:{thread:'abc',tab:'raw'} }
//
// Route patterns: '/', '/incidents', '/incidents/:id', '/memory/*rest' (greedy tail).

export function parseHash(hash) {
  let h = String(hash || '');
  if (h.startsWith('#')) h = h.slice(1);
  const qi = h.indexOf('?');
  let pathPart = qi === -1 ? h : h.slice(0, qi);
  const queryPart = qi === -1 ? '' : h.slice(qi + 1);
  if (!pathPart.startsWith('/')) pathPart = '/' + pathPart;
  if (pathPart.length > 1 && pathPart.endsWith('/')) pathPart = pathPart.slice(0, -1);
  const segments = pathPart
    .split('/')
    .filter(Boolean)
    .map(safeDecode);
  const query = {};
  if (queryPart) {
    for (const pair of queryPart.split('&')) {
      if (!pair) continue;
      const eq = pair.indexOf('=');
      const k = safeDecode(eq === -1 ? pair : pair.slice(0, eq));
      const v = eq === -1 ? '' : safeDecode(pair.slice(eq + 1));
      query[k] = v;
    }
  }
  return { path: '/' + segments.map(encodeURIComponent).join('/'), segments, query };
}

function safeDecode(s) {
  try {
    return decodeURIComponent(s.replace(/\+/g, ' '));
  } catch {
    return s;
  }
}

export function buildHash(path, query) {
  let h = '#' + (path.startsWith('/') ? path : '/' + path);
  if (query) {
    const parts = [];
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    }
    if (parts.length) h += '?' + parts.join('&');
  }
  return h;
}

/** Encode each path segment (ids may contain slashes, colons, spaces). */
export function link(...segments) {
  return '#/' + segments.filter((s) => s !== undefined && s !== null && s !== '').map((s) => encodeURIComponent(String(s))).join('/');
}

/**
 * Match parsed.segments against a list of {pattern, ...route}. First match wins.
 * Returns {route, params} or null.
 */
export function matchRoute(routes, parsed) {
  const segs = parsed.segments;
  for (const route of routes) {
    const pat = route.pattern.split('/').filter(Boolean);
    const params = {};
    let ok = true;
    let greedy = false;
    for (let i = 0; i < pat.length; i++) {
      const p = pat[i];
      if (p[0] === '*') {
        params[p.slice(1) || 'rest'] = segs.slice(i).join('/');
        greedy = true;
        break;
      }
      if (i >= segs.length) {
        ok = false;
        break;
      }
      if (p[0] === ':') params[p.slice(1)] = segs[i];
      else if (p !== segs[i]) {
        ok = false;
        break;
      }
    }
    if (!ok) continue;
    if (!greedy && segs.length !== pat.length) continue;
    return { route, params };
  }
  return null;
}

/** Which nav item is active for a path: the longest nav `match` prefix. */
export function activeNav(navItems, parsed) {
  const first = parsed.segments[0] || '';
  for (const item of navItems) if (item.key === first || (item.key === '' && first === '')) return item.key;
  return null;
}
