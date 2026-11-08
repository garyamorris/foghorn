// Formatters. Pure, locale-independent (deterministic output for tests and screenshots).

const pad = (n, w = 2) => String(n).padStart(w, '0');

export function formatNumber(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-';
  const v = Number(n);
  if (!Number.isFinite(v)) return String(v);
  const neg = v < 0;
  const fixed = Math.abs(v).toFixed(digits);
  const [i, f] = fixed.split('.');
  const grouped = i.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return (neg ? '-' : '') + grouped + (f ? '.' + f : '');
}

export function formatCompact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-';
  const v = Math.abs(Number(n));
  const sign = n < 0 ? '-' : '';
  if (v >= 1e9) return sign + trim(v / 1e9) + 'B';
  if (v >= 1e6) return sign + trim(v / 1e6) + 'M';
  if (v >= 1e3) return sign + trim(v / 1e3) + 'k';
  return sign + (Number.isInteger(v) ? String(v) : trim(v, 2));
}
function trim(x, d = 1) {
  return x.toFixed(d).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

export function formatPct(x, digits = 1) {
  if (x === null || x === undefined || Number.isNaN(x)) return '-';
  return (Number(x) * 100).toFixed(digits).replace(/\.0+$/, '') + '%';
}

export function formatBytes(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = Number(n);
  let u = 0;
  while (Math.abs(v) >= 1024 && u < units.length - 1) {
    v /= 1024;
    u++;
  }
  return (u === 0 ? String(v) : v.toFixed(v >= 10 ? 0 : 1)) + ' ' + units[u];
}

/** 1234 -> "1.2 s", 61000 -> "1m 01s", 3_700_000 -> "1h 01m". */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '-';
  const v = Math.max(0, Number(ms));
  if (v < 1) return '<1 ms';
  if (v < 1000) return Math.round(v) + ' ms';
  const s = v / 1000;
  if (s < 10) return s.toFixed(1) + ' s';
  if (s < 60) return Math.round(s) + ' s';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${pad(Math.floor(s % 60))}s`;
  const hr = Math.floor(m / 60);
  return `${hr}h ${pad(m % 60)}m`;
}

/** "5s ago" style relative time from epoch millis. */
export function relativeTime(thenMs, nowMs = Date.now()) {
  if (!Number.isFinite(thenMs)) return '-';
  const d = Math.round((nowMs - thenMs) / 1000);
  if (d < -1) return 'in ' + shortSpan(-d);
  if (d < 5) return 'just now';
  return shortSpan(d) + ' ago';
}
function shortSpan(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}

export function parseIso(s) {
  if (!s) return NaN;
  const t = Date.parse(s);
  return t;
}

/** ISO -> "10:49:12Z" (virtual clock, always UTC). */
export function formatClock(iso, withSeconds = true) {
  const t = parseIso(iso);
  if (Number.isNaN(t)) return '-';
  const d = new Date(t);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${withSeconds ? ':' + pad(d.getUTCSeconds()) : ''}Z`;
}

/** ISO -> "2026-11-05 10:49:12Z". */
export function formatDateTime(iso) {
  const t = parseIso(iso);
  if (Number.isNaN(t)) return '-';
  const d = new Date(t);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${formatClock(iso)}`;
}

export function formatDate(iso) {
  const t = parseIso(iso);
  if (Number.isNaN(t)) return '-';
  const d = new Date(t);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

export function shortId(id, n = 8) {
  const s = String(id ?? '');
  if (s.length <= n + 1) return s;
  return s.slice(0, n);
}

export function truncate(s, max = 80) {
  const str = String(s ?? '');
  return str.length <= max ? str : str.slice(0, Math.max(0, max - 1)) + '…';
}

export function titleCase(s) {
  return String(s ?? '')
    .replace(/[_.\-:]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function plural(n, one, many) {
  return `${formatNumber(n)} ${n === 1 ? one : many || one + 's'}`;
}

/** Score 0..1 -> two decimals. */
export function formatScore(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return '-';
  return Number(x).toFixed(2);
}

/** Metric value with a unit suggested by its name. */
export function formatMetric(metric, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '-';
  const m = String(metric || '');
  if (/error_rate|ratio|hit/.test(m)) return Number(v) <= 1 ? formatPct(v, 1) : formatNumber(v, 1);
  if (/_ms$|latency/.test(m)) return formatNumber(v, v < 10 ? 1 : 0) + ' ms';
  if (/_pct$|saturation/.test(m)) return formatNumber(v, 1) + '%';
  if (/rps/.test(m)) return formatNumber(v, v < 10 ? 1 : 0) + '/s';
  return formatNumber(v, Math.abs(v) < 10 ? 2 : 0);
}

export const SEVERITIES = ['info', 'low', 'medium', 'high', 'critical'];
export const SEVERITY_RANK = { info: 0, low: 1, medium: 2, high: 3, critical: 4 };
export const INCIDENT_RANK = { SEV4: 0, SEV3: 1, SEV2: 2, SEV1: 3 };

export function severityRank(s) {
  return SEVERITY_RANK[String(s || '').toLowerCase()] ?? -1;
}

/** Safe JSON stringify (cycles, BigInt, undefined). */
export function safeJson(v, indent = 2) {
  const seen = new WeakSet();
  try {
    return JSON.stringify(
      v,
      (k, val) => {
        if (typeof val === 'bigint') return String(val);
        if (val && typeof val === 'object') {
          if (seen.has(val)) return '[Circular]';
          seen.add(val);
        }
        return val;
      },
      indent,
    );
  } catch (e) {
    return String(e);
  }
}

export function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}

/** Deterministic string hash (FNV-1a), hex. Used for dedupe fingerprints and colour hashing. */
export function hashString(s) {
  let h = 0x811c9dc5;
  const str = String(s);
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

/** Pick a stable hue 0..359 for a string (lane colours). */
export function hueFor(s) {
  return parseInt(hashString(s), 16) % 360;
}
