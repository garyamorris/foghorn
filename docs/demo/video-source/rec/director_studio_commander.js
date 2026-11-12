// LIVE run of incident_commander in LangGraph Studio: fill the schema-generated input form, Submit, nodes light up, the run
// pauses on approval_gate (interrupt rendered from the response schema), answer it, the run continues. Needs tlsproxy.
const right = () => qa('*').filter(e => e.children.length === 0 && e.getBoundingClientRect().left > 700 && isVisible(e)).map(e => norm(e.textContent)).join(' / ');
const expandField = async (label) => { const b = byText(label, { tag: 'button', exact: true }); if (b) { await click(b); await sleep(500); } else rec.log('WARN no field ' + label); };
const fillLast = async (text) => { const ta = qa('textarea').filter(isVisible).pop(); if (ta) await typeInto(ta, text, { delay: 45 }); };
await waitFor(() => q('.react-flow__node'), 40000, 'graph nodes');
await sleep(2200);
rec.mark('studio_loaded');
await click('css:button[aria-label="Fit graph to view"]', { timeout: 8000 });
await sleep(1000);
await click('css:button[aria-label="Zoom out"]', { timeout: 4000 });
await sleep(1500);
rec.mark('graph_fit');
// fill the form (generated from CommanderInput's JSON schema)
await expandField('scenario id'); await fillLast('S01');
await expandField('incident'); await sleep(400);
await expandField('id'); await fillLast('INC-STUDIO-1');
await expandField('severity'); await fillLast('SEV2');
await expandField('title'); await fillLast('Checkout 500s after the 14:02 deploy');
rec.mark('form_filled');
await sleep(1200);
await click('css:button[aria-label="Collapse input editor"]', { timeout: 5000 });
await sleep(700);
await click('css:button[aria-label="Fit graph to view"]', { timeout: 8000 });
await sleep(900);
await click('css:button[aria-label="Zoom out"]', { timeout: 4000 });
await sleep(900);
await click('css:button[aria-label="Submit"]', { timeout: 10000 });
rec.mark('run_submitted');
const t0 = performance.now();
const interrupted = await waitFor(() => /Provide a value to resume/i.test(right()), 240000, 'interrupt shown');
rec.mark('interrupt_shown');
await sleep(2500);
await safe('scroll to interrupt', async () => {
  const sc = qa('*').find(e => { const cs = getComputedStyle(e); return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 100 && e.getBoundingClientRect().left > 400; });
  if (sc) await smoothScrollBy(sc, 4000, 2200);
});
rec.log('RIGHT ' + right().slice(-900));
rec.mark('interrupt_form');
await sleep(4500);
// the resume form is generated from the interrupt's response schema: scroll to it and open the Decision dropdown
await safe('scroll to form', async () => {
  const sc = qa('*').find(e => { const cs = getComputedStyle(e); return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 100 && e.getBoundingClientRect().left > 400; });
  if (sc) { await smoothScrollBy(sc, sc.scrollHeight, 1800); }
});
await sleep(2500);
rec.mark('resume_form');
await safe('open decision', async () => {
  const cb = qa('[role=combobox],button').filter(isVisible).find(e => /^decision$/i.test(norm(e.textContent)));
  if (cb) { await click(cb); await sleep(700); }
});
rec.mark('decision_open');
await sleep(4500);
await pressKey('Escape');
await sleep(1500);
rec.mark('studio_end');
