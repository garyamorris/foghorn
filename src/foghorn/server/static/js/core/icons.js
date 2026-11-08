// Inline SVG icon set (24x24 grid, stroke-based). No emoji, no external assets.
import { svg } from './dom.js';

const P = {
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  alert: 'M12 3.5 2.5 20h19L12 3.5zM12 10v4.5M12 17.2v.3',
  'alert-circle': 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7.5v5M12 15.7v.3',
  check: 'M4.5 12.5l5 5 10-11',
  'check-circle': 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12.3l2.8 2.8L16.2 9.5',
  x: 'M6 6l12 12M18 6 6 18',
  'x-circle': 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9 9l6 6M15 9l-6 6',
  play: 'M7 4.5v15l12-7.5z',
  pause: 'M8 5v14M16 5v14',
  step: 'M5 5v14l9-7zM18 5v14',
  stop: 'M6 6h12v12H6z',
  inbox: 'M3 13l3-8h12l3 8v6H3zM3 13h5l1.5 3h5l1.5-3h5',
  graph: 'M12 3l9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5',
  database: 'M4 6c0-2 16-2 16 0v12c0 2-16 2-16 0zM4 6c0 2 16 2 16 0M4 12c0 2 16 2 16 0',
  flask: 'M9 3h6M10 3v6l-5.5 9.5A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-2.5L14 9V3M7.5 15h9',
  chip: 'M7 7h10v10H7zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
  book: 'M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5zM5 19.5A1.5 1.5 0 0 0 6.5 21H19v-3',
  chat: 'M4 5h16v11H9.5L5 20v-4H4z',
  bolt: 'M13 2.5 4.5 14H11l-1 7.5L19.5 10H13z',
  sun: 'M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4',
  moon: 'M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z',
  external: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  copy: 'M9 9h10v11H9zM5 15V4h10',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4',
  filter: 'M3 5h18l-7 8v6l-4 2v-8z',
  refresh: 'M20 11a8 8 0 0 0-14.5-3.5L4 9M4 4v5h5M4 13a8 8 0 0 0 14.5 3.5L20 15M20 20v-5h-5',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6',
  edit: 'M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4',
  branch: 'M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9c0 6-12 3-12 6',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3.5 2',
  shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
  sliders: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4',
  menu: 'M4 6h16M4 12h16M4 18h16',
  'chevron-right': 'M9 5l7 7-7 7',
  'chevron-down': 'M5 9l7 7 7-7',
  'chevron-up': 'M5 15l7-7 7 7',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  send: 'M4 12l16-8-6 17-3-7z',
  flag: 'M5 21V4M5 4h11l-2 4 2 4H5',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  'eye-off': 'M3 3l18 18M10.6 6.2A10 10 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-3.2 3.9M6.3 7.6A17 17 0 0 0 2 12s3.5 7 10 7a9 9 0 0 0 4-.9',
  terminal: 'M4 5h16v14H4zM7.5 10l3 2.5-3 2.5M13 15h4',
  layers: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5',
  target: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM12 12.5v-1',
  crosshair: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 2v5M12 17v5M2 12h5M17 12h5',
  server: 'M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01',
  zap: 'M13 2.5 4.5 14H11l-1 7.5L19.5 10H13z',
  list: 'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01',
  compare: 'M8 4v16M16 4v16M4 8h8M12 16h8',
  download: 'M12 4v11M7 11l5 5 5-5M5 20h14',
  upload: 'M12 20V9M7 13l5-5 5 5M5 4h14',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-4 3.5-6 8-6s8 2 8 6',
  lock: 'M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3',
  help: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.6 9.4a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .9-1 1.6M12 16.7v.3',
  tool: 'M14.5 6.5a4 4 0 0 0 4.9 4.9L12 18.8a2.1 2.1 0 0 1-3-3zM14.5 6.5L17 4l3 3-2.5 2.5',
  history: 'M3.5 12a8.5 8.5 0 1 0 2.5-6M3.5 4v4.5H8M12 8v4.5l3 1.5',
  radio: 'M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 19a10 10 0 0 1 0-14M19.1 5a10 10 0 0 1 0 14',
  gauge: 'M4 18a9 9 0 1 1 16 0M12 13l4-4',
  home: 'M4 11l8-7 8 7M6 10v10h12V10',
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
};

export function icon(name, cls = '') {
  const el = svg('svg', { class: 'icon ' + cls, viewBox: '0 0 24 24', 'aria-hidden': 'true', focusable: 'false' });
  const d = P[name] || P.help;
  for (const seg of d.split(/(?=M)/)) if (seg.trim()) el.appendChild(svg('path', { d: seg }));
  return el;
}

export const iconNames = Object.keys(P);

/** Logo mark (a stylised lighthouse beam). */
export function logo() {
  const el = svg('svg', { class: 'icon lg', viewBox: '0 0 24 24', 'aria-hidden': 'true' });
  el.appendChild(svg('path', { d: 'M9 21l1.2-12h3.6L15 21zM10.4 9l.6-4h2l.6 4M3 6l5 1.2M3 10l5-.8M21 6l-5 1.2M21 10l-5-.8' }));
  return el;
}
