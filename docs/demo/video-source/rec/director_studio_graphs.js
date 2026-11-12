// Studio: open the graph selector so all ten Foghorn graphs are listed, then pick incident_commander's neighbour to show another graph drawing.
await waitFor(() => q('.react-flow__node'), 40000, 'graph nodes');
await sleep(2500);
rec.mark('studio_loaded');
const trig = qa('button').filter(isVisible).find(e => /incident_commander/.test(e.textContent || '') && e.getBoundingClientRect().top < 120);
if (trig) { await click(trig); rec.mark('selector_open'); await sleep(5000); }
const el = byText('storm_correlator', { exact: true });
if (el) { await click(el); rec.mark('graph_switched'); await sleep(1500); }
await waitFor(() => q('.react-flow__node'), 20000, 'graph nodes');
await click('css:button[aria-label="Fit graph to view"]', { timeout: 6000 });
await sleep(4000);
rec.mark('studio_end');
