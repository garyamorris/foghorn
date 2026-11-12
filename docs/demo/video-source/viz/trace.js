// trace.js: animated, readable LangSmith-style trace of ONE real Foghorn run (data: trace_data_hero.json from make_trace_data.py).
// The page is a pure function of time: render(t) puts the DOM in the state it has at animation time t (default seconds).
//   ?seek=S  draw one still frame at S (after ?fit warp) and stop     ?fit=S  warp the whole animation to S seconds
//   ?focus=<row name>[#k]  open that row's detail card (LLM / tool / interrupt row, or "thread") and, without ?seek, seek to it
//   ?data=file.json  data file next to this page (default trace_data_hero.json)
// Time-warp: animation time t -> thread time th (seconds of the real run). Long/boring stretches (the 31.7 s human wait,
// supervisor waits) are compressed; the specialists' parallel stretch and the LLM/tool zoom moments are shown near real speed.
const Q = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);
const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const lerp = (a, b, t) => a + (b - a) * t;
const easeOut = (t) => 1 - Math.pow(1 - clamp(t), 3);
const easeInOut = (t) => { t = clamp(t); return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
window.__done = false;

// ---- layout (CSS px)
const VIEW_H = 436;                       // rows viewport (inside #rowsclip, which sits at y=146)
const CLIP_Y = 146;
const BAR_L = 340, BAR_W = 680;                      // waterfall column
const LAT_R = 1100, TOK_R = 1182, COST_R = 1264;     // right-aligned number columns
const CARD = { x: 354, y: 112, w: 912, h: 490 };     // LLM card (bottom edge 602 < 610: subtitle safe area stays free)
const TOOLCARD = { x: 354, w: 912, h: 336 };
const THREADCARD = { x: 16, y: 112, w: 1248, h: 490 };
const STRIP = { x: 16, y: 62, w: 1248, h: 42 };
const OPEN = 0.5, CLOSE = 0.38;

const SVG = {
  chain: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="7.2" height="7.2" rx="2"/><rect x="7.8" y="7.8" width="7.2" height="7.2" rx="2"/></svg>',
  llm: '<svg viewBox="0 0 18 18" fill="currentColor"><path d="M9 2.4 10.8 7.2 15.6 9 10.8 10.8 9 15.6 7.2 10.8 2.4 9 7.2 7.2Z"/></svg>',
  tool: '<svg viewBox="0 0 18 18" fill="currentColor"><path d="M10.6 1.8 4.4 10.2h3.8l-.9 6 6.3-8.6H9.9Z"/></svg>',
  int: '<svg viewBox="0 0 18 18" fill="currentColor"><rect x="4.4" y="3.4" width="3.2" height="11.2" rx="1"/><rect x="10.4" y="3.4" width="3.2" height="11.2" rx="1"/></svg>',
};

const fmtS = (s) => (s < 0.0005 ? '<1 ms' : s < 1 ? `${Math.round(s * 1000)} ms` : s < 10 ? `${s.toFixed(2)} s` : `${s.toFixed(1)} s`);
const fmtN = (n) => Math.round(n).toLocaleString('en-US');
const fmtCost = (c) => `$${c.toFixed(4)}`;

async function main() {
  const url = Q.get('data') || 'trace_data_hero.json';
  const r0 = await fetch(url, { cache: 'no-store' });
  if (!r0.ok) throw new Error(`GET ${url} -> ${r0.status}`);
  const D = await r0.json();
  const TR = D.traces;
  const gaps = (D.gaps_ms || []).map((g) => g / 1000);

  // ---------- data model
  let base = 0;
  TR.forEach((tr, k) => {
    tr.k = k; tr.T = tr.ms / 1000; tr.base = base; tr.endTh = base + tr.T;
    const big = k > 0;                       // the short resume trace is drawn with larger rows (it has only 7)
    tr.rowH = big ? 38 : 24; tr.ics = big ? 22 : 18; tr.fs = big ? 19 : 16; tr.fs2 = big ? 17 : 15; tr.bh = big ? 16 : 12;
    base += tr.T + (gaps[k] || 0);
    tr.rows.forEach((r) => { r.k = k; r.s = r.off / 1000; r.e = (r.off + r.ms) / 1000; r.kids = []; });
    tr.rows.forEach((r) => { if (r.p >= 0) tr.rows[r.p].kids.push(r); });
    for (let i = tr.rows.length - 1; i >= 0; i--) { const r = tr.rows[i]; r.last = r.kids.length ? r.kids[r.kids.length - 1].last : r.i; }
    tr.rows.forEach((r) => {
      r.isLast = r.p < 0 || tr.rows[r.p].kids[tr.rows[r.p].kids.length - 1] === r;
      r.anc = []; for (let a = r.p; a >= 0; a = tr.rows[a].p) r.anc.push(a);   // parent chain (nearest first)
      r.gl = []; // for levels 1..d-1: does the ancestor at that depth have a later sibling?
      for (let L = 1; L < r.d; L++) { const a = tr.rows[r.anc.find((ix) => tr.rows[ix].d === L)]; r.gl.push(!a.isLast); }
    });
  });
  const nT = TR.length;
  const T0 = TR[0], T1 = TR[1] || null;
  const rowsOf = (tr) => tr.rows;
  const byName = (tr, name, n = 1) => tr.rows.filter((r) => r.name === name)[n - 1];

  // totals (every number computed from the data)
  const SUM = {
    runs: TR.reduce((a, t) => a + t.n_runs, 0), llm: TR.reduce((a, t) => a + t.llm.length, 0),
    tok: TR.reduce((a, t) => a + t.tokens, 0), cost: TR.reduce((a, t) => a + t.cost, 0),
    wall: TR.reduce((a, t) => a + t.T, 0), wait: gaps.reduce((a, g) => a + g, 0),
  };
  const shortId = D.thread_short || (D.thread_id.slice(0, 8) + '…' + D.thread_id.slice(-7));

  // ---------- timeline: authored sweeps/holds in default seconds, th = thread time
  const KF = [[0, 0]]; const beats = []; const cards = [];
  let t = 0, th = 0;
  const sweep = (to, dur) => { t += dur; th = to; KF.push([t, th]); };
  const hold = (dur) => { t += dur; KF.push([t, th]); };
  const beat = (label, dt = 0) => beats.push({ t: +(t + dt).toFixed(3), label });
  const card = (kind, tr, row, dur) => { const c = { kind, tr, row: row.i, t0: t, t1: t + dur }; cards.push(c); hold(dur); return c; };
  const end = (tr, r) => tr.base + r.e, start = (tr, r) => tr.base + r.s;

  const root = T0.rows[0];
  const sup1 = T0.rows.find((r) => r.name === 'supervisor' && r.d === 1);
  const sup1Llm = sup1 && T0.rows.find((r) => r.p === sup1.i && r.ty === 'llm');
  const parRows = T0.rows.filter((r) => r.par === 0);
  const spec0 = parRows[0];
  const specLlm = spec0 && T0.rows.find((r) => r.p === spec0.i && r.ty === 'llm');
  const specTool = spec0 && T0.rows.find((r) => r.p === spec0.i && r.ty === 'tool');
  const rca = T0.rows.find((r) => r.name === 'rca_tournament');
  const intr = T0.rows.find((r) => r.st === 'interrupted');
  const sup3 = T0.rows.filter((r) => r.name === 'supervisor' && r.d === 1).slice(-1)[0];
  const lastNodeEnd = Math.max(...T0.rows.filter((r) => r.d === 1).map((r) => r.e));

  beat(`root run: ${root.name}`, 0.15);
  hold(0.7);
  if (sup1Llm) { sweep(end(T0, sup1Llm) + 0.03, 1.1); hold(0.3); beat('one LLM call: the supervisor routes'); card('llm', T0, sup1Llm, 3.1); }
  if (spec0) {
    sweep(start(T0, spec0) + 0.05, 0.6); beat(`${parRows.length} specialists in parallel`);
    const tgt = specTool ? end(T0, specTool) + 0.012 : end(T0, specLlm || spec0);
    sweep(tgt, 1.8); hold(0.5);
    if (specLlm) { beat('one LLM call: a specialist chooses tools'); card('llm', T0, specLlm, 2.8); }
    if (specTool) { beat('tool call'); card('tool', T0, specTool, 2.2); }
    sweep(Math.max(...parRows.map((r) => end(T0, r))), 1.2); hold(0.6);
  }
  if (rca) {
    sweep(start(T0, rca) - 0.004, 1.3); beat('rca_tournament: scoring tasks');
    sweep(end(T0, rca) + 0.02, 0.3); hold(1.2);
  }
  if (sup3 && sup3.i !== (sup1 && sup1.i)) sweep(end(T0, sup3), 1.5);
  sweep(T0.endTh, 1.3); hold(0.45);
  if (intr) beat('interrupt: the approval gate pauses the run');
  hold(0.45);
  let thread = null, tSwitch = Infinity;
  if (T1) {
    beat('two traces, one thread_id: interrupt / resume');
    thread = { kind: 'thread', tr: T0, row: -1, t0: t };
    hold(1.2); sweep(T1.base, 1.9); hold(0.8); thread.t1 = t + CLOSE; cards.push(thread); hold(CLOSE * 0.6);
    tSwitch = thread.t0 + OPEN + 0.12;
    beat('resume trace: after the human approves');
    sweep(T1.endTh, 2.0);
  }
  hold(1.5);
  const DURATION = t;

  const thetaAt = (tt) => {
    if (tt <= 0) return 0; if (tt >= DURATION) return KF[KF.length - 1][1];
    for (let i = 1; i < KF.length; i++) if (KF[i][0] >= tt) { const [t0, a] = KF[i - 1], [t1, b] = KF[i]; return t1 > t0 ? lerp(a, b, (tt - t0) / (t1 - t0)) : b; }
    return th;
  };
  const tAt = (theta) => {
    if (theta <= KF[0][1]) return KF[0][0];
    for (let i = 1; i < KF.length; i++) { const [t0, a] = KF[i - 1], [t1, b] = KF[i]; if (b > a && b >= theta - 1e-9) return t0 + clamp((theta - a) / (b - a)) * (t1 - t0); }
    return Infinity;
  };

  // per-row appearance / collapse schedule (animation seconds)
  TR.forEach((tr) => {
    const lastSib = new Map();   // siblings that start within 80 ms of each other are revealed one after another (parallel groups together)
    tr.rows.forEach((r) => {
      r.stg = 0;
      const L = lastSib.get(r.p);
      if (r.par == null && L && r.s - L.s < 0.08) r.stg = L.stg + 1;
      lastSib.set(r.p, { s: r.s, stg: r.stg });
      r.tApp = tAt(tr.base + r.s) + r.stg * 0.17;
      if (r.p >= 0) r.tApp = Math.max(r.tApp, tr.rows[r.p].tApp + 0.14);
      r.colT = Infinity;
    });
  });
  // collapse finished subtrees of the first (long) trace so the tree stays compact; trace 2 stays fully open
  TR.forEach((tr) => {
    if (tr.k !== 0) return;
    tr.rows.forEach((r) => {
      if (r.p < 0 || !r.kids.length) return;
      let e = r.e; if (r.par != null) e = Math.max(...tr.rows.filter((x) => x.par === r.par).map((x) => x.e));
      const hold_ = r.name === 'rca_tournament' ? 1.3 : 0.6;
      r.colT = tAt(tr.base + e) + hold_;
    });
  });
  cards.forEach((c) => { if (c.row < 0) return; const tr = c.tr, r = tr.rows[c.row]; r.anc.forEach((a) => { const ar = tr.rows[a]; if (ar.p >= 0 && ar.colT < Infinity) ar.colT = Math.max(ar.colT, c.t1 + 0.25); }); });

  // ---------- DOM: header / tiles
  $('t1').textContent = T0.graph || root.name;
  $('t2').textContent = `LangSmith · project ${D.project}`;
  const tileDefs = [['runs', 'RUNS'], ['llm', 'LLM CALLS'], ['tok', 'TOKENS'], ['cost', 'COST'], ['wall', 'WALL TIME']];
  $('tiles').innerHTML = tileDefs.map(([k, l]) => `<div class="tile" id="tile_${k}"><i>${l}</i><b id="tv_${k}">0</b>${k === 'wall' ? '<em id="tv_wait"></em>' : ''}</div>`).join('');
  const tv = {}; tileDefs.forEach(([k]) => { tv[k] = $('tv_' + k); }); tv.wait = $('tv_wait');
  const setTxt = (el, s) => { if (el._s !== s) { el._s = s; el.textContent = s; } };

  // ---------- thread timeline (used twice: small strip + big thread card)
  function makeThreadTL(host, o) {
    // o: {x0, w, y, h, gapW, room, lanes:false|true, laneGap}
    const kpx = (o.w - o.gapW - o.room) / (T0.T + (T1 ? T1.T : 0));
    const seg = (cls, x, y, w, h, bg, label, big) => {
      const d = document.createElement('div'); d.className = cls; d.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px;background:${bg}`;
      d.innerHTML = `<div class="fill"></div><div class="tx"></div>`; host.appendChild(d); return d;
    };
    const x1 = o.x0, w1 = T0.T * kpx;
    const xg = x1 + w1, xs = xg + (T1 ? o.gapW : 0);
    const w2 = T1 ? T1.T * kpx : 0;
    const yA = o.y, yB = o.lanes ? o.y + o.laneGap : o.y;
    const s1 = seg(o.big ? 'th-lane' : 'tl-seg', x1, yA, w1, o.h, '#1d2650');
    s1.querySelector('.fill').style.background = '#4b63d0';
    const out = { kpx, xg, xs, w1, w2, s1 };
    let gap = null, s2 = null;
    if (T1) {
      gap = document.createElement('div'); gap.className = 'tl-gap';
      gap.style.cssText = `left:${xg}px;top:${yA}px;width:${o.gapW}px;height:${(o.lanes ? o.laneGap : 0) + o.h}px`;
      gap.innerHTML = '<div class="fill"></div><div class="tx"></div>'; host.appendChild(gap);
      s2 = seg(o.big ? 'th-lane' : 'tl-seg', xs, yB, Math.max(4, w2), o.h, '#14301d');
      s2.querySelector('.fill').style.background = '#2f9a52';
    }
    const head = document.createElement('div'); head.className = 'tl-head';
    head.style.cssText = `top:${yA - (o.lanes ? 8 : 3)}px;height:${(o.lanes ? o.laneGap + o.h + 16 : o.h + 6)}px`; host.appendChild(head);
    out.gap = gap; out.s2 = s2; out.head = head;
    out.update = (thv) => {
      const f1 = clamp(thv / T0.T);
      s1.querySelector('.fill').style.width = `${f1 * 100}%`;
      let hx = x1 + clamp(thv, 0, T0.T) * kpx;
      if (T1) {
        const g = gaps[0], fg = clamp((thv - T0.T) / g);
        gap.querySelector('.fill').style.width = `${fg * 100}%`;
        const f2 = clamp((thv - T1.base) / T1.T);
        s2.querySelector('.fill').style.width = `${f2 * 100}%`;
        if (thv > T0.T) hx = xg + fg * o.gapW;
        if (thv > T1.base) hx = xs + f2 * w2;
        out.fg = fg;
      }
      head.style.left = `${hx - 1}px`;
      head.style.opacity = thv > 0 ? 1 : 0;
    };
    return out;
  }

  // small strip
  const strip = $('strip');
  strip.innerHTML = `<div class="lab">thread <b class="mono">${esc(shortId)}</b></div>`;
  const stripTL = makeThreadTL(strip, { x0: 250, w: 986, y: 6, h: 28, gapW: 168, room: 200, lanes: false, big: false });
  stripTL.s1.querySelector('.tx').textContent = `trace 1 · ${fmtS(T0.T)}${T0.kind === 'interrupt' ? ' · interrupt' : ''}`;
  if (T1) {
    stripTL.s2.querySelector('.tx').textContent = '';
    const n = document.createElement('div'); n.className = 'tl-note'; n.style.cssText = `left:${stripTL.xs + Math.max(4, stripTL.w2) + 8}px;top:0;line-height:40px`;
    n.innerHTML = `<b style="color:var(--ok)">trace 2</b> · ${fmtS(T1.T)} · resume`; strip.appendChild(n);
  }
  const stripGapTx = T1 ? stripTL.gap.querySelector('.tx') : null;

  // ---------- panel: axes, rows, bands
  const axes = $('axes'), layerHost = $('rows'), grid = $('grid');
  const layers = [], axisEls = [], gridEls = [];
  TR.forEach((tr) => {
    // axis ticks
    const steps = [0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20]; const step = steps.find((s) => tr.T / s <= 6) || 20;
    const ax = document.createElement('div'); const gr = document.createElement('div');
    let h = '', g = '';
    for (let v = 0; v <= tr.T + 1e-6; v += step) {
      const x = BAR_L + (v / tr.T) * BAR_W;
      const lab = v === 0 ? '0' : step < 1 ? `${(+v.toFixed(2))} s` : `${Math.round(v)} s`;
      h += `<span class="ax" style="left:${x}px;transform:translateX(${v === 0 ? '-3px' : '-50%'})">${lab}</span>`;
      g += `<div class="grid" style="left:${x}px"></div>`;
    }
    ax.innerHTML = h; gr.innerHTML = g; axes.appendChild(ax); grid.appendChild(gr); axisEls.push(ax); gridEls.push(gr);
    // rows
    const layer = document.createElement('div'); layer.style.cssText = `position:absolute;left:0;top:0;width:1280px;height:436px;--rh:${tr.rowH}px;--ics:${tr.ics}px;--fs:${tr.fs}px;--fs2:${tr.fs2}px;--bh:${tr.bh}px`;
    layerHost.appendChild(layer); layers.push(layer);
    tr.bands = [];
    const groups = [...new Set(tr.rows.filter((r) => r.par != null).map((r) => r.par))];
    groups.forEach((g_) => {
      const members = tr.rows.filter((r) => r.par === g_);
      const band = document.createElement('div'); band.className = 'band'; const lab = document.createElement('div'); lab.className = 'bandlab';
      lab.textContent = `${members.length} in parallel`;
      layer.appendChild(band); layer.appendChild(lab);
      tr.bands.push({ g: g_, members, band, lab, a: members[0].i, b: members[members.length - 1].last });
    });
    tr.rows.forEach((r) => {
      const el = document.createElement('div'); el.className = 'row';
      const kind = r.st === 'interrupted' ? 'int' : r.ty;
      let h2 = '<div class="hl"></div><div class="sep"></div>';
      const st = tr.ics;
      for (let L = 1; L < r.d; L++) if (r.gl[L - 1]) h2 += `<div class="g" style="left:${16 + (L - 1) * st + st / 2}px;top:0;height:100%"></div>`;
      if (r.d > 0) { const x = 16 + (r.d - 1) * st + st / 2; h2 += `<div class="g" style="left:${x}px;top:0;height:${r.isLast ? '50%' : '100%'}"></div><div class="gt" style="left:${x}px;top:50%;width:${st / 2}px"></div>`; }
      h2 += `<div class="ic ${kind}" style="left:${16 + r.d * st}px">${SVG[kind]}</div>`;
      h2 += `<div class="nm" style="left:${16 + r.d * st + st}px"><span class="lb">${esc(r.name)}</span>${r.n > 1 ? `<span class="x">×${r.n}</span>` : ''}${r.model ? `<span class="md">${esc(r.model)}</span>` : ''}${r.st === 'interrupted' ? '<span class="ib">⏸ interrupted</span>' : ''}</div>`;
      h2 += '<div class="segs"></div><div class="bar ' + (r.st === 'interrupted' ? 'int' : r.ty) + '"></div>';
      h2 += `<div class="v lat" style="left:${LAT_R - 100}px"></div><div class="v tok" style="left:${TOK_R - 100}px"></div><div class="v cost" style="left:${COST_R - 100}px"></div>`;
      el.innerHTML = h2; layer.appendChild(el);
      r.el = el; r.bar = el.querySelector('.bar'); r.lab = el.querySelector('.lb'); r.hl = el.querySelector('.hl'); r.nm = el.querySelector('.nm');
      r.vl = el.querySelector('.lat'); r.vt = el.querySelector('.tok'); r.vc = el.querySelector('.cost');
      // children LLM spans drawn inside a collapsed parent's bar ("where the time went")
      r.segEls = [];
      if (r.kids.length && r.p >= 0) {
        const segs = el.querySelector('.segs'); segs.style.cssText = `position:absolute;left:${BAR_L + r.s / tr.T * BAR_W}px;top:0;width:0;height:${tr.rowH}px;`;
        tr.rows.slice(r.i + 1, r.last + 1).filter((x) => x.ty === 'llm').forEach((x) => {
          const sd = document.createElement('div'); sd.className = 'seg'; segs.appendChild(sd); r.segEls.push({ el: sd, x });
        });
      }
      // callouts
      if (r.det && r.det.interrupt) {
        const c = document.createElement('div'); c.className = 'callout'; c.style.color = 'var(--amber)'; c.style.opacity = 0;
        const i2 = r.det.interrupt; c.textContent = `interrupt() · ${i2.kind || 'approval'} needed · ${i2.title}`; layer.appendChild(c); r.callout = c; r.calloutRight = true;
      } else if (r.det && r.det.resume) {
        const c = document.createElement('div'); c.className = 'callout'; c.style.color = 'var(--ok)'; c.style.opacity = 0;
        c.textContent = `resumed · ${r.det.resume.status} by ${r.det.resume.decided_by}`; layer.appendChild(c); r.callout = c; r.calloutRight = false;
      }
    });
  });
  const pill = $('pill'), cursor = $('cursor'), cursorlab = $('cursorlab'), quote = $('quote');
  const qRow = T1 ? [...T1.rows].reverse().find((r) => r.ty === 'llm' && r.det && r.det.reply) : null;
  if (qRow) quote.innerHTML = `<div class="k">${esc(T1.rows[qRow.p].name)} · ${esc(qRow.model)} wrote the final report</div><div class="q">${esc(qRow.det.reply)}</div>`;
  $('foot').innerHTML = `<span><i class="ic chain" style="position:static">${SVG.chain}</i>chain</span><span><i class="ic llm" style="position:static">${SVG.llm}</i>LLM call</span><span><i class="ic tool" style="position:static">${SVG.tool}</i>tool call</span><span id="footnote"></span>`;
  const footnote = $('footnote'), foot = $('foot');

  // ---------- cards
  const cardEl = $('card'), cardIn = $('cardin'), arrow = $('arrow');
  let cardKey = '';
  const ancestorsLabel = (tr, r) => r.anc.slice().reverse().map((i) => tr.rows[i].name).join(' › ');
  const kv = (args) => args.map(([k, v]) => `${esc(k)}=${esc(v)}`).join(' ');
  function llmHTML(tr, r) {
    const d = r.det;
    let reply = '', rk = '';
    if (d.calls && d.calls.length === 1) {
      const c = d.calls[0]; const args = c.args.filter(([k]) => k !== 'reason').concat(c.args.filter(([k]) => k === 'reason'));
      rk = `tool call <b style="color:var(--tool)">${esc(c.name)}</b>${d.forced ? ' (forced: structured output)' : ''}`;
      reply = `<div class="txt reply">${args.map(([k, v], ix) => `<span class="${k === 'reason' ? '' : 'cl'}"><span class="kk">${esc(k)}</span>: ${esc(v)}</span>`).join('')}</div>`;
      reply = reply.replace(/<span class="">/, '<span style="display:block;max-height:42px;overflow:hidden">');
    } else if (d.calls && d.calls.length > 1) {
      rk = `${d.calls.length} tool calls, requested together`;
      reply = `<div class="txt reply">${d.calls.slice(0, 6).map((c) => `<span class="cl"><span class="nn">${esc(c.name)}</span>(${c.args.map(([k, v]) => `${esc(k)}=${esc(v)}`).join(', ')})</span>`).join('')}</div>`;
    } else { rk = 'text, no tool call'; reply = `<div class="txt reply"><div class="in l5">${esc(d.reply)}</div></div>`; }
    const tools = d.bound && d.bound.length && !d.forced ? ` · tools bound: ${d.bound.map(esc).join(', ')}` : '';
    return `<div class="chead"><span class="ic llm">${SVG.llm}</span><b>${esc(r.name === 'model' ? 'model › ChatOpenAI' : r.name)}</b><span class="m">${esc(d.model)}</span><span class="sp"></span><span class="where">in ${esc(ancestorsLabel(tr, r))}</span></div>
<div class="facts"><div class="fact"><i>LATENCY</i><b>${fmtS(r.ms / 1000)}</b></div><div class="fact"><i>TOKENS IN</i><b>${fmtN(d.ptok)}</b></div><div class="fact"><i>TOKENS OUT</i><b>${fmtN(d.ctok)}</b></div><div class="fact"><i>REASONING</i><b>${fmtN(d.reasoning)}</b></div><div class="fact"><i>COST</i><b>${fmtCost(d.cost)}</b></div></div>
<div class="sec"><div class="k">PROMPT<span>${esc(d.ctx.join(' · '))}${tools}</span></div>
<div class="txt"><div class="in l2"><span class="rl">system</span>${esc(d.sys)}</div></div><div class="txt"><div class="in l3"><span class="rl">${esc(d.last_role)} · tail</span>${esc(d.last)}</div></div></div>
<div class="sec"><div class="k">REPLY<span>${rk}</span></div>${reply}</div>`;
  }
  function toolHTML(tr, r) {
    const calls = r.det.calls || [];
    const c0 = calls[0] || { args: [], out: '', out_len: 0 };
    const [oh, ot] = c0.out.split(' … ');
    return `<div class="chead"><span class="ic tool">${SVG.tool}</span><b>${esc(r.name)}${r.n > 1 ? ` ×${r.n}` : ''}</b><span class="m">${r.n > 1 ? 'run in parallel' : 'tool call'} · ${fmtS(r.ms / 1000)}</span><span class="sp"></span><span class="where">called by ${esc(ancestorsLabel(tr, r))}</span></div>
<div class="sec"><div class="k">ARGS<span>${calls.length} call${calls.length > 1 ? 's' : ''}</span></div><div class="txt tl">${calls.slice(0, 4).map((c) => `<span class="cl">${kv(c.args)}</span>`).join('')}</div></div>
<div class="sec"><div class="k">RESULT<span>call 1 · ${fmtN(c0.out_len)} characters returned to the model</span></div><div class="txt"><div class="in l2">${esc(oh)}</div></div>${ot ? `<div class="txt"><div class="in l1"><span class="rl">… tail</span>${esc(ot)}</div></div>` : ''}</div>`;
  }
  // thread card (built once; dynamic parts updated per frame)
  let threadTL = null, thCountEl = null;
  function threadHTML() {
    const a = T0, b = T1; const nodes1 = b.rows.filter((r) => r.d === 1).map((r) => r.name);
    const ir = intr && intr.det && intr.det.interrupt; const rr = b.rows.find((r) => r.det && r.det.resume);
    cardIn.innerHTML = `<div class="chead"><b style="font-size:21px">One thread, two traces</b><span class="m mono">thread_id ${esc(shortId)}</span><span class="sp"></span><span class="pill amber">⏸ interrupt</span><span style="color:var(--text-3)">/</span><span class="pill green">▶ resume</span></div><div id="thhost" style="position:absolute;left:0;top:0;width:1248px;height:230px"></div>
<div class="th-col" style="left:22px;top:238px;width:590px"><h4 style="color:var(--amber)">⏸ interrupt · trace 1 · ${esc(a.short)}</h4><div><b>${esc(intr ? intr.name : 'approval_gate')}</b> calls <b class="mono">interrupt()</b>; the run stops, state is checkpointed</div>${ir ? `<div>asks: <b>${esc(ir.title)}</b> · risk ${esc(ir.risk)}${ir.reversible ? ', reversible' : ''}</div><div>policy: ${ir.approvers} approver (${esc((ir.roles || []).join(', '))}) · ${ir.checks} checks passed</div>` : ''}</div>
<div class="th-col" style="left:660px;top:238px;width:590px"><h4 style="color:var(--ok)">▶ resume · trace 2 · ${esc(b.short)}</h4><div>same <b class="mono">thread_id</b>, new run: <b class="mono">Command(resume=…)</b></div>${rr ? `<div>${esc(rr.name)} returns: <b>${esc(rr.det.resume.status)}</b> by <b>${esc(rr.det.resume.decided_by)}</b></div>` : ''}<div>then <b>${nodes1.slice(1).map(esc).join(' → ')}</b></div></div>
<div class="facts" style="position:absolute;left:22px;top:372px;width:1204px"><div class="fact"><i>TRACE 1 · ${a.n_runs} RUNS</i><b>${fmtS(a.T)} <small>· ${fmtN(a.tokens)} tok · ${fmtCost(a.cost)}</small></b></div><div class="fact" style="border-color:#6d5a1f"><i>HUMAN WAIT</i><b id="thwait" style="color:var(--amber)">0 s</b></div><div class="fact"><i>TRACE 2 · ${b.n_runs} RUNS</i><b>${fmtS(b.T)} <small>· ${fmtN(b.tokens)} tok · ${fmtCost(b.cost)}</small></b></div></div>`;
    const host = $('thhost');
    threadTL = makeThreadTL(host, { x0: 22, w: 1204, y: 90, h: 38, gapW: 250, room: 340, lanes: true, laneGap: 58, big: true });
    const e1 = threadTL.s1.querySelector('.tx'); e1.textContent = `trace 1 · ${a.short} · ${a.n_runs} runs · ${fmtS(a.T)}`;
    const pin1 = document.createElement('div'); pin1.className = 'th-txt'; pin1.style.cssText = `left:${threadTL.xg - 200}px;top:58px;width:200px;text-align:right;color:var(--amber);font-weight:700`; pin1.textContent = '⏸ interrupt ▾'; host.appendChild(pin1);
    const pin2 = document.createElement('div'); pin2.className = 'th-txt'; pin2.style.cssText = `left:${threadTL.xs - 4}px;top:122px;color:var(--ok);font-weight:700`; pin2.textContent = '▶ resume ▾'; host.appendChild(pin2);
    const lab2 = document.createElement('div'); lab2.className = 'th-txt'; lab2.style.cssText = `left:${threadTL.xs + Math.max(4, threadTL.w2) + 10}px;top:${90 + 58 + 8}px;color:var(--text)`;
    lab2.innerHTML = `<b style="color:var(--ok)">trace 2</b> · ${esc(b.short)} · ${b.n_runs} runs · ${fmtS(b.T)}`; host.appendChild(lab2);
    thCountEl = threadTL.gap.querySelector('.tx');
  }

  // ---------- totals / strip / rows update
  function totalsAt(thv) {
    let runs = 0, llm = 0, tok = 0, cost = 0, wall = 0, wait = 0;
    TR.forEach((tr, k) => {
      const tl = thv - tr.base, ms = tl * 1000;
      runs += tr.starts.filter((s) => s <= ms + 0.5).length;
      tr.llm.forEach(([s, e, tk, c]) => { if (ms >= s) { llm++; const f = e > s ? clamp((ms - s) / (e - s)) : 1; tok += tk * f; cost += c * f; } });
      wall += clamp(tl, 0, tr.T);
      if (k > 0) wait += clamp(thv - TR[k - 1].endTh, 0, gaps[k - 1] || 0);
    });
    return { runs, llm, tok, cost, wall, wait };
  }

  // forced focus card
  let FOCUS = null, focusSeek = null;
  const fq = Q.get('focus');
  if (fq) {
    if (fq === 'thread' && thread) { FOCUS = thread; focusSeek = thread.t0 + 2.5; }
    else {
      const m = fq.match(/^(.*?)(?:#(\d+))?$/); let n = parseInt(m[2] || '1', 10); let found = null;
      for (const tr of TR) { for (const r of tr.rows) if (r.name === m[1] && --n === 0) { found = [tr, r]; break; } if (found) break; }
      if (found) {
        const [tr, r] = found; const kind = r.ty === 'tool' ? 'tool' : (r.ty === 'llm' ? 'llm' : null);
        const sched = cards.find((c) => c.tr === tr && c.row === r.i);
        if (kind) FOCUS = sched || { kind, tr, row: r.i, t0: 0, t1: 1e9 };
        focusSeek = sched ? sched.t0 + 1.4 : Math.min(DURATION, tAt(tr.base + r.e) + 0.2);
      }
    }
  }

  // ---------- render(t): t in default seconds
  const VIS = TR.map((tr) => new Float32Array(tr.rows.length)), YY = TR.map((tr) => new Float32Array(tr.rows.length));
  function render(tt) {
    const thv = thetaAt(tt);
    // active card
    let cs = null;
    for (const c of cards) { const p = Math.min(easeInOut((tt - c.t0) / OPEN), easeInOut((c.t1 - tt) / CLOSE)); if (p > 0.0005 && (!cs || p > cs.p)) cs = { c, p }; }
    if (FOCUS) cs = { c: FOCUS, p: 1 };
    const cp = cs ? cs.p : 0;
    const sw = easeInOut((tt - tSwitch) / 0.3);          // 0 = trace 1 shown, 1 = trace 2 shown
    const kShow = sw > 0.5 ? 1 : 0;

    // tiles
    const tot = totalsAt(thv);
    setTxt(tv.runs, fmtN(tot.runs)); setTxt(tv.llm, fmtN(tot.llm)); setTxt(tv.tok, fmtN(tot.tok)); setTxt(tv.cost, fmtCost(tot.cost));
    setTxt(tv.wall, tot.wall < 0.001 ? '0 s' : fmtS(tot.wall)); setTxt(tv.wait, tot.wait > 0.05 ? `+${tot.wait.toFixed(1)} s wait` : '');
    // strip
    stripTL.update(thv);
    if (stripGapTx) { const w = tot.wait; setTxt(stripGapTx, w > 0.05 && w < gaps[0] - 0.05 ? `waiting ${w.toFixed(1)} s` : `human wait ${fmtS(gaps[0])}`); }
    strip.style.boxShadow = cs && cs.c.kind === 'thread' ? '0 0 0 2px rgba(242,193,78,.55)' : 'none';

    // panel head
    TR.forEach((tr, k) => { const o = k === 0 ? 1 - sw : sw; axisEls[k].style.opacity = o; gridEls[k].style.opacity = o; layers[k].style.opacity = o; layers[k].style.display = o < 0.004 ? 'none' : 'block'; });
    const trS = TR[kShow];
    const kind = trS.kind === 'resume' ? 'resume' : 'interrupt';
    pill.textContent = nT > 1 ? (kShow === 0 ? `TRACE 1 of ${nT} · ${trS.short} · ⏸ interrupt` : `TRACE 2 of ${nT} · ${trS.short} · ▶ resume`) : `TRACE · ${trS.short}`;
    pill.style.color = kShow === 0 ? 'var(--amber)' : 'var(--ok)'; pill.style.background = kShow === 0 ? 'var(--amber-soft)' : 'var(--ok-soft)'; pill.style.borderColor = kShow === 0 ? '#6d5a1f' : '#1f6a3a';
    foot.style.opacity = 1 - clamp(cp * 3);
    setTxt(footnote, `${trS.n_rows} of ${trS.n_runs} runs shown · wrappers & middleware folded into their parent`);
    // cursor
    const tl = thv - trS.base; const cx = BAR_L + clamp(tl, 0, trS.T) / trS.T * BAR_W;
    const cvis = (tl > 0.0005 ? 1 : 0) * (1 - 0.5 * clamp((tl - trS.T) / 0.2));
    cursor.style.left = `${cx}px`; const co = kShow === 0 ? clamp(1 - sw * 2) : 1; cursor.style.opacity = cvis * co; cursorlab.style.left = `${cx}px`; cursorlab.style.opacity = cvis; cursorlab.style.transform = cx > 900 ? 'translateX(-100%)' : 'translateX(-50%)';
    setTxt(cursorlab, fmtS(clamp(tl, 0, trS.T)));

    // rows
    let focusY = null, focusRow = null;
    TR.forEach((tr, k) => {
      if ((k === 0 ? 1 - sw : sw) < 0.004) return;
      const rows = tr.rows, vis = VIS[k], yy = YY[k], tlk = thv - tr.base;
      const col = rows.map((r) => (r.colT < Infinity ? easeInOut((tt - r.colT) / 0.5) : 0));
      let y = 0; const RH = tr.rowH;
      for (let i = 0; i < rows.length; i++) {
        const r = rows[i];
        let v = easeOut((tt - r.tApp) / 0.3);
        for (const a of r.anc) { if (rows[a].p >= 0) v *= 1 - col[a]; }
        vis[i] = v; yy[i] = y; y += RH * v;
      }
      const overflow = Math.max(0, y - VIEW_H);
      if (k === 1 && qRow) { const done = tlk >= qRow.e; quote.style.top = `${y + 26}px`; quote.style.opacity = done ? easeOut((tt - tAt(tr.base + qRow.e)) / 0.45) * sw : 0; }
      for (let i = 0; i < rows.length; i++) {
        const r = rows[i], v = vis[i], el = r.el;
        if (v < 0.004) { if (el._d !== 'none') { el._d = 'none'; el.style.display = 'none'; if (r.callout) r.callout.style.opacity = 0; } continue; }
        if (el._d !== 'block') { el._d = 'block'; el.style.display = 'block'; }
        const yp = yy[i] - overflow;
        el.style.transform = `translateY(${yp.toFixed(2)}px)`;
        const isFocus = cs && cs.c.tr === tr && cs.c.row === i;
        const dim = cs && !isFocus ? 1 - 0.55 * cp : 1;
        el.style.opacity = (v * v * dim).toFixed(3);
        r.hl.style.opacity = isFocus ? (0.95 * cp).toFixed(3) : 0;
        if (isFocus) { focusY = yp; focusRow = r; }
        const started = tlk >= r.s - 1e-9, done = tlk >= r.e;
        const prog = r.e > r.s ? clamp((tlk - r.s) / (r.e - r.s)) : (started ? 1 : 0);
        let wpx = started ? Math.max(3, (Math.min(tlk, r.e) - r.s) / tr.T * BAR_W) : 0;
        const lx = BAR_L + r.s / tr.T * BAR_W;
        r.bar.style.left = `${Math.min(lx, BAR_L + BAR_W - 3)}px`; r.bar.style.width = `${wpx}px`; r.bar.style.display = started ? 'block' : 'none';
        const run = started && !done && prog < 1;
        const cn = (r.st === 'interrupted' ? 'bar int' : `bar ${r.ty}`) + (run ? ' run' : '') + (r.colT < Infinity && col[i] > 0.5 ? ' dim' : '');
        if (r._cn !== cn) { r._cn = cn; r.bar.className = cn; }
        r.lab.style.color = run ? '#fff' : 'var(--text-2)';
        if (r.segEls.length) r.segEls.forEach((s) => { const sx = (s.x.s - r.s) / tr.T * BAR_W, sw_ = Math.max(2, (s.x.e - s.x.s) / tr.T * BAR_W); s.el.style.left = `${sx}px`; s.el.style.width = `${sw_}px`; s.el.style.opacity = col[i] > 0.4 && done ? Math.min(1, (col[i] - 0.4) / 0.6) : 0; });
        // numbers
        setTxt(r.vl, started ? fmtS((done ? r.e - r.s : tlk - r.s)) : '');
        r.vl.style.color = done ? 'var(--text)' : 'var(--text-3)';
        setTxt(r.vt, done && r.tok > 0 ? fmtN(r.tok) : ''); r.vt.style.color = r.ty === 'llm' ? 'var(--llm)' : 'var(--text-2)';
        setTxt(r.vc, done && r.cost > 0 ? fmtCost(r.cost) : ''); r.vc.style.color = 'var(--text-2)';
        if (r.callout) {
          const cw = r.callout.offsetWidth || 380; const bx = Math.min(lx, BAR_L + BAR_W - 3);
          r.callout.style.left = `${r.calloutRight ? bx - cw - 14 : bx + 14}px`; r.callout.style.transform = `translateY(${yp.toFixed(2)}px)`;
          r.callout.style.opacity = started ? (v * (r.calloutRight ? 1 : easeOut((tt - r.tApp) / 0.5))).toFixed(3) : 0;
          if (cs && !isFocus) r.callout.style.opacity = (parseFloat(r.callout.style.opacity) * (1 - 0.55 * cp)).toFixed(3);
        }
      }
      // parallel bands
      tr.bands.forEach((b) => {
        const first = rows[b.a], v = vis[b.a];
        if (v < 0.01 || tlk < first.s) { b.band.style.display = 'none'; b.lab.style.display = 'none'; return; }
        const lastRow = rows[b.b]; const top = yy[b.a] - overflow - 1; const bot = yy[b.b] - overflow + RH * vis[b.b] + 1;
        const x0 = BAR_L + Math.min(...b.members.map((m) => m.s)) / tr.T * BAR_W - 4;
        const x1 = BAR_L + Math.max(...b.members.map((m) => Math.min(tlk, m.e))) / tr.T * BAR_W + 5;
        b.band.style.cssText = `display:block;left:${x0}px;top:${top}px;width:${Math.max(8, x1 - x0)}px;height:${Math.max(10, bot - top)}px;opacity:${(v * (1 - 0.5 * cp)).toFixed(3)}`;
        const lw = b.lab.offsetWidth || 120; const lx2 = x1 + 8 + lw > LAT_R - 70 ? x0 - lw - 8 : x1 + 8;
        b.lab.style.cssText = `display:block;left:${lx2}px;top:${top + 1}px;opacity:${(v * (1 - 0.6 * cp)).toFixed(3)}`;
      });
    });

    // card
    let key = '', rect = null, op = 0;
    if (cs && cs.p > 0.0005) {
      const c = cs.c, tr = c.tr; const p = easeInOut(cs.p);
      let target, origin;
      if (c.kind === 'thread') {
        if (cardEl._key !== 'thread') { cardEl._key = 'thread'; threadHTML(); }
        key = 'thread'; target = THREADCARD; origin = STRIP;
        if (threadTL) {
          threadTL.update(thv);
          const w = totalsAt(thv).wait; setTxt(thCountEl, w > 0.05 && w < gaps[0] - 0.05 ? `waiting for a human · ${w.toFixed(1)} s` : `waiting for a human · ${fmtS(gaps[0])}`);
          setTxt($('thwait'), w < 0.05 ? '0 s' : `${w.toFixed(1)} s`);
        }
      } else {
        const r = tr.rows[c.row]; key = `${c.kind}:${tr.k}:${c.row}`;
        const ry = (focusY == null ? 200 : focusY) + CLIP_Y;
        if (c.kind === 'llm') target = CARD; else { const h = TOOLCARD.h; target = { x: TOOLCARD.x, y: clamp(ry - 70, 112, 600 - h), w: TOOLCARD.w, h }; }
        origin = { x: BAR_L, y: ry, w: BAR_W, h: tr.rowH };
        if (cardEl._key !== key) { cardEl._key = key; cardIn.innerHTML = c.kind === 'llm' ? llmHTML(tr, r) : toolHTML(tr, r); }
        cardEl._kind = c.kind;
      }
      rect = { x: lerp(origin.x, target.x, p), y: lerp(origin.y, target.y, p), w: lerp(origin.w, target.w, p), h: lerp(origin.h, target.h, p) };
      op = clamp(cs.p * 3);
      cardIn.style.width = `${target.w}px`; cardIn.style.opacity = clamp((p - 0.45) / 0.45);
      // arrow pointing from the card to the focused row
      if (c.kind !== 'thread' && focusY != null) { arrow.style.opacity = op * clamp((p - 0.6) / 0.4); arrow.style.left = `${target.x - 12}px`; arrow.style.top = `${focusY + CLIP_Y + tr.rowH / 2 - 9}px`; } else arrow.style.opacity = 0;
    } else { arrow.style.opacity = 0; }
    if (rect) {
      cardEl.style.cssText = `left:${rect.x}px;top:${rect.y}px;width:${rect.w}px;height:${rect.h}px;opacity:${op};border-radius:${lerp(6, 12, op)}px`;
    } else { cardEl.style.opacity = 0; cardEl._key = ''; }
  }

  // ---------- play (same conventions as fhlib.play)
  const fit = parseFloat(Q.get('fit'));
  const warp = fit > 0 ? fit / DURATION : 1;
  window.__meta = { duration: +(DURATION * warp).toFixed(3), default_duration: +DURATION.toFixed(3), warp: +warp.toFixed(4), beats: beats.map((b) => ({ ...b, t: +(b.t * warp).toFixed(3) })),
    totals: { runs: SUM.runs, llm_calls: SUM.llm, tokens: SUM.tok, cost: +SUM.cost.toFixed(6), wall_s: +SUM.wall.toFixed(2), human_wait_s: +SUM.wait.toFixed(2) }, thread_id: D.thread_id, traces: TR.map((x) => ({ id: x.id, runs: x.n_runs, ms: x.ms })) };
  const drawOut = (tOut) => { window.__t = tOut; render(clamp(tOut / warp, 0, DURATION)); };
  window.__seek = drawOut;
  const seek = Q.get('seek');
  if (seek !== null && seek !== '') { drawOut(parseFloat(seek) || 0); window.__ready = true; window.__done = true; return; }
  if (focusSeek !== null) { drawOut(focusSeek * warp); window.__ready = true; window.__done = true; return; }
  drawOut(0); window.__ready = true;
  // Clock: under the `rec` recorder the animation time is seconds since the RECORDING started (rec.t(), reset by the recorder), so a
  // clip begins at t=0 and is not clipped by the page-load + settle delay; the page holds t=0 until the recorder starts (max 2 s,
  // then it assumes no recording, e.g. a --shot --after preview). Without `rec` (plain browser) it is the page's own clock.
  const rec = window.rec, t0Init = rec && rec._t0, pageStart = performance.now(); let recStarted = false;
  const clockT = (now) => {
    if (rec && typeof rec.t === 'function') {
      if (!recStarted && rec._t0 !== t0Init) recStarted = true;
      if (recStarted) return rec.t();
      return now - pageStart < 2000 ? 0 : (now - pageStart) / 1000;
    }
    return (now - pageStart) / 1000;
  };
  const total = DURATION * warp;
  const tick = (now) => {
    const tOut = Math.max(0, clockT(now));
    if (tOut >= total) { drawOut(total); window.__done = true; return; }
    drawOut(tOut); requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

main().catch((e) => {
  console.error(e);
  document.body.insertAdjacentHTML('beforeend', `<div style="position:absolute;left:30px;top:300px;color:#ff9aa6;font-size:24px">${String(e && e.message || e)}</div>`);
  window.__error = String(e); window.__done = true;
});
