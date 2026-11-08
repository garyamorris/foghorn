// Incremental Server-Sent-Events parser. Pure: no DOM, no network. Works in browsers and Node.
//
// Handles: \r\n, \n and bare \r line endings (LangGraph / sse-starlette terminate frames with \r\n\r\n),
// frames split across arbitrary chunk boundaries (including between \r and \n), multi-line `data:`,
// comment lines (":" heartbeats), `id`, `event`, `retry`, a leading BOM, and `data` without a space.

export class SSEParser {
  /** @param {{onComment?: (text:string)=>void}} [opts] */
  constructor(opts = {}) {
    this.onComment = opts.onComment || null;
    this.reset();
  }

  reset() {
    this.buf = '';
    this.bomChecked = false;
    this.data = [];
    this.hasData = false;
    this.event = '';
    this.id = undefined;
    this.retry = undefined;
    this.lastEventId = '';
    this.comments = 0;
    this.events = 0;
  }

  /**
   * Feed a decoded text chunk. Returns the events completed by this chunk.
   * @param {string} chunk
   * @returns {{event:string, data:string, id?:string, retry?:number}[]}
   */
  push(chunk) {
    if (!chunk) return [];
    let text = this.buf + chunk;
    this.buf = '';
    if (!this.bomChecked && text.length > 0) {
      this.bomChecked = true;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    }
    const out = [];
    let start = 0;
    const n = text.length;
    for (let i = 0; i < n; i++) {
      const c = text.charCodeAt(i);
      if (c !== 10 && c !== 13) continue;
      if (c === 13) {
        if (i === n - 1) break; // a lone \r at the end may be the first half of \r\n: wait for more input
        const line = text.slice(start, i);
        if (text.charCodeAt(i + 1) === 10) i++;
        start = i + 1;
        this._line(line, out);
      } else {
        this._line(text.slice(start, i), out);
        start = i + 1;
      }
    }
    this.buf = text.slice(start);
    return out;
  }

  /** Call when the stream ends. A trailing unterminated frame is discarded per the SSE spec. */
  flush() {
    const out = [];
    if (this.buf.length) {
      // a final line without terminator (rare); treat \r as terminator only
      const line = this.buf.replace(/\r$/, '');
      this.buf = '';
      this._line(line, out);
    }
    return out;
  }

  _line(line, out) {
    if (line === '') {
      this._dispatch(out);
      return;
    }
    if (line.charCodeAt(0) === 58) {
      // ':' comment
      this.comments++;
      if (this.onComment) this.onComment(line.slice(1).trim());
      return;
    }
    const idx = line.indexOf(':');
    let field;
    let value;
    if (idx === -1) {
      field = line;
      value = '';
    } else {
      field = line.slice(0, idx);
      value = line.slice(idx + 1);
      if (value.charCodeAt(0) === 32) value = value.slice(1);
    }
    switch (field) {
      case 'data':
        this.data.push(value);
        this.hasData = true;
        break;
      case 'event':
        this.event = value;
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
      default:
        break;
    }
  }

  _dispatch(out) {
    if (!this.hasData && !this.event) {
      this.data = [];
      this.hasData = false;
      this.id = undefined;
      this.retry = undefined;
      return;
    }
    if (this.id !== undefined) this.lastEventId = this.id;
    const ev = { event: this.event || 'message', data: this.data.join('\n') };
    if (this.id !== undefined) ev.id = this.id;
    else if (this.lastEventId) ev.id = this.lastEventId;
    if (this.retry !== undefined) ev.retry = this.retry;
    this.events++;
    out.push(ev);
    this.data = [];
    this.hasData = false;
    this.event = '';
    this.id = undefined;
    this.retry = undefined;
  }
}

/** Parse a full SSE document at once (tests, fixtures). */
export function parseSSE(text) {
  const p = new SSEParser();
  const evs = p.push(text);
  return evs.concat(p.flush());
}

/** JSON-decode an SSE event's data; falls back to the raw string. */
export function decodeData(ev) {
  const d = ev.data;
  if (d === '' || d == null) return null;
  try {
    return JSON.parse(d);
  } catch {
    return d;
  }
}

/** Split a LangGraph event name such as "custom|metrics:3f2a|model:91bc" into mode + namespace. */
export function splitEventName(name) {
  const s = String(name || '');
  const i = s.indexOf('|');
  if (i === -1) return { mode: s, ns: [], nsRaw: '' };
  const mode = s.slice(0, i);
  const nsRaw = s.slice(i + 1);
  const ns = nsRaw
    .split('|')
    .filter(Boolean)
    .map((seg) => {
      const j = seg.indexOf(':');
      return j === -1 ? { node: seg, taskId: '' } : { node: seg.slice(0, j), taskId: seg.slice(j + 1) };
    });
  return { mode, ns, nsRaw };
}

/** Encode a text/event-stream frame (used by the mock server and tests). Always ends with \r\n\r\n. */
export function encodeSSE({ event, data, id, retry } = {}) {
  let s = '';
  if (id !== undefined) s += `id: ${id}\r\n`;
  if (event) s += `event: ${event}\r\n`;
  if (retry !== undefined) s += `retry: ${retry}\r\n`;
  const body = typeof data === 'string' ? data : JSON.stringify(data ?? null);
  for (const line of body.split('\n')) s += `data: ${line}\r\n`;
  return s + '\r\n';
}
