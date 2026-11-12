// director_hero.js: the dashboard "hero run" for the Foghorn walkthrough. S01 end to end as a human operator would do it:
// launch from Scenarios -> watch Mission Control events -> open the incident console -> approve the rollback -> approve the
// postmortem -> save the lesson to memory. Every wait is bounded; marks (rec.mark) give the video builder exact cut points.
const PEND = async () => (await (await fetch('/api/approvals?status=pending')).json());
const pendingItems = (j) => (Array.isArray(j) ? j : j.items || j.approvals || []);
const nav = (path) => click(`css:a[href="#/${path}"]`, { timeout: 8000 });
const incId = async () => { try { const j = await (await fetch('/api/incidents')).json(); const it = (Array.isArray(j) ? j : j.items || j.incidents || [])[0]; return it && (it.incident_id || it.id); } catch (e) { return null; } };
const waitResolved = async (timeout) => { const t0 = Date.now(); while (Date.now() - t0 < timeout) { try { const j = await (await fetch('/api/incidents')).json(); const it = (Array.isArray(j) ? j : j.items || j.incidents || [])[0]; if (it && /resolved|closed/i.test(String(it.status))) return true; } catch (e) {} await sleep(800); } rec.log('WARN timeout waiting for resolved'); return false; };
const goIncident = async () => { const id = await incId(); if (id) location.hash = '#/incidents/' + id; else rec.log('WARN no incident id'); await sleep(900); return id; };
const waitPending = async (n, timeout, label) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { const items = pendingItems(await PEND()); if (items.length >= n) return items; } catch (e) {}
    await sleep(700);
  }
  rec.log('WARN timeout waiting for pending approvals: ' + label);
  return [];
};

rec.mark('hero.start');
await sleep(1800);                                   // Mission Control, quiet, before anything happens
rec.mark('hero.mission_idle');
await nav('scenarios');
await waitText('Checkout 500s', 8000);
await sleep(1200);
rec.mark('hero.scenarios');
await click('S01', { timeout: 6000 });
await sleep(2200);
rec.mark('hero.s01_selected');
const launchBtn = byText('Launch', { tag: 'button', exact: true });
await click(launchBtn || 'Launch');
rec.mark('hero.launched');
await sleep(1500);

// ---- Mission Control: events flow in over SSE and the pipeline routes them
await nav('');                                       // home = Mission Control
await sleep(500);
rec.mark('hero.mission');
await waitText('incident.opened', 90000);
rec.mark('hero.incident_opened');
await sleep(4500);

// ---- Open the incident console and let the agents work
await nav('incidents');
await waitText(/INC-/, 20000);
await sleep(1200);
const row = byText(/INC-/, { tag: 'a' }) || byText(/INC-/);
await click(row || 'css:a[href^="#/incidents/INC"]');
rec.mark('hero.console_open');
await sleep(2500);

// watch the war room stream for a while, then peek at the board, the timeline and the raw stream drawer
await sleep(9000);
rec.mark('hero.console_stream');
const tabBoard = byText('Board', { tag: 'button', exact: true });
if (tabBoard) { await click(tabBoard); rec.mark('hero.console_board'); await sleep(7000); }
const tabTl = byText('Timeline', { tag: 'button', exact: true });
if (tabTl) { await click(tabTl); rec.mark('hero.console_timeline'); await sleep(5000); }
const tabSt = byText('Stream', { tag: 'button', exact: true });
if (tabSt) { await click(tabSt); await sleep(1500); }
const rawBtn = byText('Raw stream', { tag: 'button' });
if (rawBtn) { await click(rawBtn); rec.mark('hero.console_raw'); await sleep(6000); await click(byText('Raw stream', { tag: 'button' }) || rawBtn); await sleep(800); }

// wait for the approval (reasoning, specialists, hypothesis board, rca tournament all stream by in the meantime)
const first = await waitPending(1, 420000, 'rollback approval');
rec.mark('hero.approval_pending');
await sleep(4000);
rec.mark('hero.console_before_approval');

// ---- Approvals: read the card, then approve
await nav('approvals');
await sleep(2500);
rec.mark('hero.approvals_open');
const card = byText(/approval|Approve/i, { tag: 'button' });
await sleep(7000);                                   // time to read risk, blast radius, policy checks
rec.mark('hero.card_read');
const approve = byText('Approve', { tag: 'button' });
if (approve) await click(approve); else rec.log('WARN no Approve button');
rec.mark('hero.approved');
await sleep(2500);

// ---- Back to the console: execute, verify recovery, resolve
await nav('incidents');
await sleep(1200);
await goIncident();
rec.mark('hero.console_resume');
await waitResolved(240000);
rec.mark('hero.resolved');
await sleep(6000);

// ---- Postmortem review (Functional API pipeline waits for a human) then memory_write
const pm = await waitPending(1, 240000, 'postmortem review');
rec.mark('hero.postmortem_pending');
await nav('approvals');
await sleep(2500);
rec.mark('hero.pm_open');
await sleep(5000);
const pmApprove = byText(/^(Approve|Publish)/, { tag: 'button' });
if (pmApprove) await click(pmApprove); else rec.log('WARN no postmortem approve button');
rec.mark('hero.pm_approved');
await sleep(3000);

const mem = await waitPending(1, 240000, 'memory write');
rec.mark('hero.memory_pending');
await sleep(2500);
rec.mark('hero.memory_open');
await sleep(5000);
const save = byText(/^(Save|Approve|Remember)/, { tag: 'button' });
if (save) await click(save); else rec.log('WARN no memory save button');
rec.mark('hero.memory_saved');
await sleep(3000);

await nav('incidents');
await sleep(3000);
rec.mark('hero.end');
