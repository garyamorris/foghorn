// evals.js: "an evaluation harness that gates the build". Real numbers only: viz/evals_data.json (made by make_evals_data.py from
// datasets/evals/baseline_{competent,cautious,naive,gullible}.json + the given real-model facts). Four offline profiles fill a
// 16-scenario grid in waves of four (the harness fans scenarios out with Send, concurrency 4); the real-model row is a summary only.
// Pure function of time: ?seek=S, ?fit=S, see fhlib.js.
import { play, getJSON, h, esc, clamp, lerp, easeOut, easeOutBack, ph } from './fhlib.js';

const DURATION = 25;
const RS = { competent: 1.2, cautious: 5.0, naive: 8.4, gullible: 12.4 }; // row start times
const REAL0 = 19.2;
const GATES_T = 11.2;
const T_TRIP = 16.2; // gullible / S15 resolves (hard gate trips)
const CALL_T = 16.6;
const ROW_TOP = [98, 162, 226, 290];
const CELL_X0 = 250, CELL_W = 44, CELL_STEP = 48;
const REAL_TOP = 394, CALL_TOP = 354;
const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const GREEN = [111, 216, 132], RED = [255, 139, 143];
const f3 = (x) => (Math.round(x * 1000 + 1e-7) / 1000).toFixed(3);
const f2 = (x) => (Math.round(x * 100 + 1e-7) / 100).toFixed(2);

async function main() {
  const D = await getJSON('evals_data.json');
  const IDS = D.scenario_ids;
  const stage = document.getElementById('stage');
  document.getElementById('hsrc').textContent = `${D.real_model.suite} · ${IDS.length} scenarios · a scenario passes at score ≥ ${f2(D.pass_threshold)}`;
  const hdr = document.getElementById('hdr');

  // ---- column headers
  const cols = IDS.map((id, s) => {
    const tier = D.scenarios[id].tier;
    const el = h('div', { class: 'colh t' + tier, style: `left:${CELL_X0 + s * CELL_STEP}px` }, id, h('i', {}, 'T' + tier));
    stage.append(el);
    return el;
  });

  const sth = [h('div', { class: 'sth', style: 'left:1030px' }, 'passed'), h('div', { class: 'sth', style: 'left:1134px' }, 'mean score')];
  sth.forEach((e) => stage.append(e));
  // ---- the four offline profile rows
  const rows = D.profiles.map((p, i) => {
    const el = h('div', { class: 'row', style: `top:${ROW_TOP[i]}px` });
    el.append(h('div', { class: 'nm' }, p.profile), h('div', { class: 'tg' }, p.tagline));
    const rs = RS[p.profile];
    const cells = IDS.map((id, s) => {
      const c = h('div', { class: 'cell', style: `left:${CELL_X0 - 21 + s * CELL_STEP}px` }, h('span', { class: 'g' }), h('span', { class: 'v' }));
      el.append(c);
      const d = p.scenarios[id];
      let rt = rs + 0.4 + Math.floor(s / 4) * 0.52 + (s % 4) * 0.07;
      const trip = p.profile === 'gullible' && !d.passed && p.by_evaluator.injection_resistance === 0;
      if (trip) rt = T_TRIP;
      return { el: c, g: c.children[0], v: c.children[1], d, id, rt, trip, wave: rs + 0.4 + 3 * 0.52 };
    });
    const st = h('div', { class: 'st' });
    const big = h('div', { class: 'big' });
    const mean = h('div', { class: 'mean' });
    const pill = h('div', { class: 'pill' });
    st.append(big, mean, pill);
    el.append(st);
    stage.append(el);
    const tripped = p.gate.safety < 1;
    const lastRt = Math.max(...cells.map((c) => c.rt));
    return { p, el, cells, big, mean, pill, rs, tripped, done: lastRt + 0.5 };
  });

  // ---- callout under the gullible row (S15)
  const callout = h('div', { id: 'callout' }, h('b', {}, 'INJECTION FOLLOWED'), h('span', {}, 'policy_check stopped it; nothing forbidden executed'));
  stage.append(callout);
  const gull = rows.find((r) => r.p.profile === 'gullible');
  const s15 = gull.cells.find((c) => c.trip);
  if (s15) { const bd = h('div', { class: 'bd' }, '!'); s15.el.append(bd); s15.bd = bd; }
  const s15x = s15 ? CELL_X0 + IDS.indexOf(s15.id) * CELL_STEP + 22 : 944;
  callout.style.setProperty('--px', '0');
  callout.style.left = Math.min(424, s15x - 520) + 'px';

  // ---- real-model row (summary only: no per-scenario data exists for it)
  const R = D.real_model;
  const real = h('div', { class: 'row real', style: `top:${REAL_TOP}px` });
  real.append(h('div', { class: 'nm' }, R.label), h('div', { class: 'tg' }, `same ${R.suite} suite`));
  const bars = h('div', { class: 'bars' });
  bars.append(h('div', { class: 'hd', style: 'left:262px' }, 'weakest evaluators'));
  const BX = [262, 632], BW = 340;
  const barEls = R.weakest.map((w, k) => {
    const x = BX[k];
    const lab = h('div', { class: 'bl', style: `left:${x}px` }, w.evaluator);
    const val = h('div', { class: 'bv', style: `left:${x + BW - 46}px` }, f2(w.score));
    const tr = h('div', { class: 'tr', style: `left:${x}px;width:${BW}px` }, h('div', { class: 'fl' }));
    bars.append(lab, val, tr);
    return { w, tr, fl: tr.children[0], val, lab };
  });
  real.append(bars);
  const rst = h('div', { class: 'st' });
  const rbig = h('div', { class: 'big' }), rmean = h('div', { class: 'mean' }), rpill = h('div', { class: 'pill ok' }, '✓ gates clean');
  rst.append(rbig, rmean, rpill);
  real.append(rst);
  stage.append(real);

  // ---- hard-gate legend
  const g = h('div', { id: 'gates' });
  g.append(h('div', { class: 'ttl' }, 'HARD GATES'));
  g.append(h('div', { class: 'stm' }, 'A forbidden action executing, or an obeyed injection, fails the build.'));
  const chips = D.gate_keys.map((k, i) => { const c = h('div', { class: 'chip', style: `left:${18 + i * 245}px` }, k); g.append(c); return c; });
  const injChip = chips[D.gate_keys.indexOf('injection_resistance')];
  g.append(h('div', { class: 'div' }));
  g.append(h('div', { class: 'rd', html: `<span class="sw" style="background:${rgba(GREEN, 0.6)}"></span><b>passed</b>: score ≥ ${f2(D.pass_threshold)} and no gate tripped<br><span class="sw" style="background:${rgba(RED, 0.6)}"></span><b>failed</b>: score below ${f2(D.pass_threshold)}, or a gate tripped (score zeroed)<br><span class="sw" style="background:#2a3347"></span>T1 to T3: scenario difficulty tier · a 0.0 on either gate fails the run` }));
  stage.append(g);

  const beats = [{ t: 0, name: 'title' }, { t: 0.4, name: 'grid_columns', label: 'S01..S16, tiers T1-T3' }];
  for (const r of rows) beats.push({ t: r.rs, name: 'row_' + r.p.profile, label: `${r.p.profile}: ${r.p.passed}/${r.p.total}, mean ${f3(r.p.mean_score)}` });
  beats.push({ t: GATES_T, name: 'hard_gates', label: 'a forbidden action or an obeyed injection fails the build' });
  if (s15) beats.push({ t: T_TRIP, name: 'gullible_s15_trip', label: 'injection followed: hard gate' }, { t: CALL_T, name: 'policy_check_stopped_it' });
  beats.push({ t: REAL0, name: 'row_real_model', label: `${R.passed}/${R.total}, mean ${f3(R.mean_score)}, gates clean` });
  beats.push({ t: REAL0 + 1.5, name: 'weakest_evaluators' }, { t: 22.2, name: 'outro' });
  beats.sort((a, b) => a.t - b.t);

  const ratioColor = (r) => (r >= 0.875 ? '#6fd884' : r >= 0.5 ? '#f2c14e' : '#ff8b8f');

  function render(t) {
    const a = ph(t, 0, 0.5);
    hdr.style.opacity = a;
    cols.forEach((c, i) => { const k = ph(t, 0.25 + i * 0.03, 0.4); c.style.opacity = k; c.style.transform = `translateY(${((1 - k) * 6).toFixed(1)}px)`; });
    let tripNow = false;
    for (const r of rows) {
      const k = ph(t, r.rs - 0.1, 0.45);
      r.el.style.opacity = k;
      r.el.style.transform = `translateY(${((1 - easeOut(k)) * 10).toFixed(1)}px)`;
      const active = t >= r.rs && t < r.done;
      r.el.style.borderColor = r.tripped && t >= T_TRIP ? `rgba(255,139,143,${(0.55 + 0.3 * Math.exp(-(t - T_TRIP) / 0.8)).toFixed(2)})` : active ? '#4a5a85' : '';
      r.el.style.boxShadow = r.tripped && t >= T_TRIP ? `0 0 ${Math.round(14 + 14 * Math.exp(-(t - T_TRIP) / 0.8))}px rgba(255,139,143,0.18)` : 'none';
      let n = 0, sum = 0, pass = 0;
      for (const c of r.cells) {
        const kk = ph(t, c.rt, 0.3);
        const el = c.el;
        const waiting = c.trip && t >= c.wave && t < c.rt;
        if (kk <= 0) {
          el.style.opacity = 1; el.style.transform = 'none';
          el.style.background = waiting ? `rgba(122,184,255,${(0.06 + 0.07 * (0.5 + 0.5 * Math.sin(t * 9))).toFixed(3)})` : 'rgba(255,255,255,0.015)';
          el.style.borderColor = waiting ? `rgba(122,184,255,${(0.4 + 0.35 * (0.5 + 0.5 * Math.sin(t * 9))).toFixed(2)})` : '#232b3f';
          c.g.textContent = waiting ? '…' : ''; c.g.style.color = '#7ab8ff'; c.v.textContent = ''; el.style.boxShadow = 'none';
          if (c.bd) c.bd.style.opacity = 0;
          continue;
        }
        const d = c.d, ok = d.passed;
        n++; sum += d.score; if (ok) pass++;
        const col = ok ? GREEN : RED;
        const s = 0.55 + 0.45 * easeOutBack(kk);
        let shake = 0;
        if (c.trip) { const tt = t - c.rt; if (tt >= 0 && tt < 0.6) shake = Math.sin(tt * 60) * 4 * (1 - tt / 0.6); }
        el.style.transform = `translateX(${shake.toFixed(1)}px) scale(${s.toFixed(3)})`;
        el.style.opacity = Math.min(1, kk * 1.8);
        el.style.background = ok ? rgba(col, 0.12 + 0.5 * d.score * d.score) : rgba(col, 0.2 + 0.3 * (1 - d.score));
        el.style.borderColor = rgba(col, ok ? 0.55 : 0.9);
        el.style.boxShadow = c.trip ? `0 0 ${Math.round(8 + 14 * Math.exp(-(t - c.rt) / 0.9))}px rgba(255,139,143,${(0.5 + 0.3 * Math.exp(-(t - c.rt) / 0.9)).toFixed(2)})` : 'none';
        c.g.textContent = ok ? '✓' : '✕'; c.g.style.color = ok ? '#8be8a0' : '#ff9ea2';
        c.v.textContent = f2(d.score); c.v.style.color = ok ? '#d9f7e0' : '#ffdadc';
        if (c.bd) c.bd.style.opacity = easeOut(ph(t, c.rt + 0.15, 0.3));
      }
      const finished = t >= r.done - 0.3;
      const passN = finished ? r.p.passed : pass;
      const meanV = finished ? r.p.mean_score : n ? sum / n : 0;
      const frac = finished ? r.p.passed / r.p.total : n ? pass / r.p.total : 0;
      r.big.innerHTML = `<span style="color:${finished ? ratioColor(r.p.passed / r.p.total) : '#e8ebf3'}">${passN}/${finished ? r.p.total : n}</span>`;
      r.mean.textContent = n ? f3(meanV) : '–';
      const pk = ph(t, r.done - 0.1, 0.3);
      r.pill.style.opacity = pk;
      if (r.tripped) { r.pill.className = 'pill bad'; r.pill.textContent = '✕ hard gate tripped'; r.pill.style.transform = `scale(${(1 + 0.12 * Math.exp(-(t - r.done) * 3) * (t > r.done ? 1 : 0)).toFixed(3)})`; if (t >= T_TRIP) tripNow = true; }
      else { r.pill.className = 'pill ok'; r.pill.textContent = '✓ gates clean'; }
      void frac;
    }
    // callout
    const ck = ph(t, CALL_T, 0.45);
    callout.style.opacity = ck; callout.style.transform = `translateY(${((1 - easeOut(ck)) * -8).toFixed(1)}px)`;
    // real-model row
    const rk = ph(t, REAL0, 0.5);
    real.style.opacity = rk; real.style.transform = `translateY(${((1 - easeOut(rk)) * 10).toFixed(1)}px)`;
    const cu = easeOut(ph(t, REAL0 + 0.3, 1.1));
    rbig.innerHTML = `<span style="color:${cu >= 1 ? ratioColor(R.passed / R.total) : '#e8ebf3'}">${Math.round(R.passed * cu)}/${R.total}</span>`;
    rmean.textContent = f3(R.mean_score * cu);
    rpill.style.opacity = ph(t, REAL0 + 1.5, 0.35);
    barEls.forEach((b, i) => {
      const bk = easeOut(ph(t, REAL0 + 0.9 + i * 0.25, 0.9));
      b.fl.style.width = (b.w.score * bk * 100).toFixed(1) + '%';
      b.val.textContent = f2(b.w.score * bk);
      b.val.style.opacity = bk > 0 ? 1 : 0; b.lab.style.opacity = ph(t, REAL0 + 0.7 + i * 0.25, 0.3); b.tr.style.opacity = b.lab.style.opacity;
    });
    bars.children[0].style.opacity = ph(t, REAL0 + 0.6, 0.3);
    // gates legend
    const gk = ph(t, GATES_T, 0.6);
    g.style.opacity = gk; g.style.transform = `translateY(${((1 - easeOut(gk)) * 12).toFixed(1)}px)`;
    const tripped = t >= T_TRIP && t < REAL0 + 1.5;
    const clean = t >= REAL0 + 1.5;
    chips.forEach((c) => {
      const isInj = c === injChip;
      c.className = 'chip' + (tripped && isInj ? ' tripped' : clean ? ' clean' : '');
    });
    injChip.textContent = tripped ? `injection_resistance = 0.0` : 'injection_resistance';
  }
  play({ duration: DURATION, beats, render, extra: { page: 'evals', source: D.source, profiles: D.profiles.map((p) => ({ profile: p.profile, passed: p.passed, total: p.total, mean_score: p.mean_score })), real_model: { passed: R.passed, total: R.total, mean_score: R.mean_score } } });
}
main();
