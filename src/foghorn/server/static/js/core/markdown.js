// Minimal, safe Markdown -> block AST (headings, paragraphs, lists, fenced code, quotes, tables, rules) with
// inline code / bold / italic / links. Pure; the DOM renderer is in ui.js. Raw HTML is never interpreted.

export function parseMarkdown(src) {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || '');
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
    if (fence) {
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
      i++;
      blocks.push({ type: 'code', lang: fence[2] || '', text: buf.join('\n') });
      continue;
    }
    const hm = line.match(/^(#{1,6})\s+(.*)$/);
    if (hm) {
      blocks.push({ type: 'heading', level: hm[1].length, inline: parseInline(hm[2].trim()) });
      i++;
      continue;
    }
    if (/^\s*([-*_])\1\1+\s*$/.test(line)) {
      blocks.push({ type: 'rule' });
      i++;
      continue;
    }
    if (line.includes('|') && isTableSep(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      blocks.push({ type: 'table', head: head.map(parseInline), rows: rows.map((r) => r.map(parseInline)) });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      blocks.push({ type: 'quote', inline: parseInline(buf.join(' ')) });
      continue;
    }
    const lm = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (lm) {
      const ordered = /\d/.test(lm[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (m) {
          items.push([m[3]]);
          i++;
        } else if (/^\s{2,}\S/.test(lines[i]) && items.length) {
          items[items.length - 1].push(lines[i++].trim());
        } else break;
      }
      blocks.push({ type: 'list', ordered, items: items.map((it) => parseInline(it.join(' '))) });
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*(```|~~~)|\s*>|\s*([-*+]|\d+[.)])\s)/.test(lines[i])) buf.push(lines[i++].trim());
    blocks.push({ type: 'p', inline: parseInline(buf.join(' ')) });
  }
  return blocks;
}

/** Inline -> [{t:'text'|'code'|'strong'|'em'|'link', text, href?}] */
export function parseInline(s) {
  const out = [];
  let rest = String(s ?? '');
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/;
  while (rest) {
    const m = re.exec(rest);
    if (!m) {
      out.push({ t: 'text', text: rest });
      break;
    }
    if (m.index > 0) out.push({ t: 'text', text: rest.slice(0, m.index) });
    const tok = m[0];
    if (tok.startsWith('`')) out.push({ t: 'code', text: tok.slice(1, -1) });
    else if (tok.startsWith('**') || tok.startsWith('__')) out.push({ t: 'strong', text: tok.slice(2, -2) });
    else if (tok.startsWith('*')) out.push({ t: 'em', text: tok.slice(1, -1) });
    else {
      const lm = tok.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      out.push({ t: 'link', text: lm[1], href: safeHref(lm[2]) });
    }
    rest = rest.slice(m.index + tok.length);
  }
  return out;
}

/** Only http(s), mailto and in-app hash links survive; anything else (javascript:, data:) becomes '#'. */
export function safeHref(href) {
  const h = String(href || '').trim();
  if (/^(https?:\/\/|mailto:|#)/i.test(h)) return h;
  return '#';
}
