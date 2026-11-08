// Copilot chat panel for ops_copilot: streamed messages, tool-call cards, tool-review interrupts.
import { h, mount, rafThrottle, stickToBottom } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { badge, button, iconButton, selectEl, errorBox, errorMessage, jsonViewer, emptyState, chip } from '../core/ui.js';
import { createRunState, applySSE, laneList, laneText, messagesFromValues, beginRun, finishRun } from '../core/reducer.js';
import { buildResume, normalizeInterrupt } from '../core/schemaform.js';
import { lsGet, lsSet } from '../core/store.js';
import { truncate, safeJson } from '../core/format.js';
import { streamRun } from '../api.js';
import { interruptCard } from './forms.js';
import { link } from '../core/router.js';

const GRAPH = 'ops_copilot';
const MODES = ['values', 'messages-tuple', 'updates', 'custom', 'tasks'];
const SUGGESTIONS = ['What is wrong with orders-db right now?', 'Which changes landed in the last hour?', 'What does the DBA team prefer?', 'Remember that the DBA team prefers kill_queries before any restart.'];

export function createCopilotPanel(app) {
  let open = false;
  let threadId = lsGet('copilot_thread', null);
  let assistantId = lsGet('copilot_assistant', GRAPH);
  let transcript = []; // committed messages from values
  let run = null;
  let handle = null;
  let busy = false;
  let assistants = [{ value: GRAPH, label: GRAPH }];
  const scrollBox = h('div', { class: 'cp-scroll', role: 'log', 'aria-live': 'polite', 'aria-label': 'Copilot conversation', tabindex: '0' });
  const committedEl = h('div', { class: 'col gap-3' });
  const liveEl = h('div', { class: 'col gap-3' });
  const pendingEl = h('div', { class: 'col gap-3' });
  scrollBox.append(committedEl, liveEl, pendingEl);
  const follow = stickToBottom(scrollBox);
  const input = h('textarea', { class: 'textarea cp-input', rows: 2, placeholder: 'Ask about an incident, a service, or what the team prefers...', 'aria-label': 'Message the copilot', onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } } });
  const sendBtn = button('', { icon: 'send', variant: 'primary', title: 'Send (Enter)', onclick: () => send() });
  const stopBtn = button('', { icon: 'stop', title: 'Stop', onclick: () => stop(), hidden: true });
  const status = h('span', { class: 'tiny muted' });
  const assistSel = selectEl(assistants, assistantId, (v) => { assistantId = v; lsSet('copilot_assistant', v); }, { size: 'sm', label: 'Copilot assistant' });

  const panel = h('aside', { class: 'copilot', role: 'complementary', 'aria-label': 'Ops copilot', hidden: true },
    h('header', { class: 'copilot-head' }, icon('chat'), h('strong', { class: 'grow' }, 'Ops copilot'), h('div', { style: 'width:160px' }, assistSel), iconButton('refresh', 'New conversation', () => reset(), { size: 'sm', variant: 'ghost' }), iconButton('x', 'Close copilot', () => toggle(false), { size: 'sm', variant: 'ghost' })),
    scrollBox,
    h('footer', { class: 'copilot-foot' }, h('div', { class: 'row top' }, h('div', { class: 'grow' }, input), h('div', { class: 'col' }, sendBtn, stopBtn)), h('div', { class: 'row between' }, status, threadId ? h('a', { class: 'tiny', href: link('threads', threadId) }, 'thread') : null)),
  );
  document.body.appendChild(panel);

  (async () => {
    try {
      const cat = await app.api.graphs();
      const entry = (cat.items || []).find((g) => g.graph_id === GRAPH);
      const list = (entry?.assistants || []).map((a) => ({ value: a.assistant_id, label: a.name || a.assistant_id }));
      if (list.length) {
        assistants = [{ value: GRAPH, label: `${GRAPH} (default)` }, ...list];
        mount(assistSel, assistants.map((a) => h('option', { value: a.value }, a.label)));
        assistSel.value = assistants.some((a) => a.value === assistantId) ? assistantId : GRAPH;
      }
    } catch {
      /* default assistant only */
    }
  })();

  // ---- rendering ----------------------------------------------------------------------------------------------
  function bubble(role, children, extra) {
    return h('div', { class: ['bubble', `b-${role}`, extra] }, h('div', { class: 'b-role tiny muted' }, role === 'user' ? 'You' : role === 'tool' ? 'Tool result' : 'Copilot'), children);
  }
  function toolCard(name, args, result, pending) {
    let argsObj = args;
    if (typeof args === 'string') {
      try {
        argsObj = JSON.parse(args);
      } catch {
        argsObj = args;
      }
    }
    return h('details', { class: ['toolcard', pending && 'pending'] }, h('summary', null, icon('tool', 'sm'), h('span', { class: 'mono strong' }, name || 'tool'), h('span', { class: 'mono muted truncate' }, truncate(typeof args === 'string' ? args : JSON.stringify(args || {}), 70)), pending ? badge('running', 'accent', { dot: true, live: true }) : result ? badge('done', 'ok', { sq: true }) : null),
      h('div', { class: 'col' }, argsObj && typeof argsObj === 'object' ? jsonViewer(argsObj, { open: 1 }) : null, result ? h('pre', { class: 'jv mono untrusted-text' }, truncate(result, 1500)) : null));
  }
  function renderCommitted() {
    if (!transcript.length && !run) {
      mount(committedEl, emptyState({ icon: 'chat', title: 'Ask the ops copilot', text: 'It can query metrics, search logs and runbooks, recall past incidents and remember team preferences. Risky tool calls ask you first.', action: h('div', { class: 'col' }, SUGGESTIONS.map((s) => h('button', { class: 'chip', type: 'button', onclick: () => { input.value = s; send(); } }, s))) }));
      return;
    }
    const byCall = new Map();
    for (const m of transcript) if (m.role === 'tool' && m.toolCallId) byCall.set(m.toolCallId, m.text);
    mount(committedEl, transcript.filter((m) => m.role !== 'system' && m.role !== 'tool').map((m) => bubble(m.role, [m.text && h('div', { class: 'b-text untrusted-text' }, m.text), ...m.toolCalls.map((tc) => toolCard(tc.name, tc.args, byCall.get(tc.id), false))])));
  }
  function renderLive() {
    if (!run) return mount(liveEl);
    const lanes = laneList(run);
    const nodes = [];
    for (const lane of lanes) {
      const text = laneText(lane);
      const tools = Object.values(lane.tools);
      if (!text && !tools.length) continue;
      nodes.push(bubble('assistant', [text && h('div', { class: ['b-text untrusted-text', run.status === 'running' && !lane.done && 'streaming'] }, text), ...tools.map((t) => toolCard(t.name, t.args, t.result, !t.done))], 'live'));
    }
    if (!nodes.length && run.status === 'running') nodes.push(bubble('assistant', h('div', { class: 'b-text streaming muted' }, 'Thinking'), 'live'));
    mount(liveEl, nodes);
  }
  function renderPending() {
    if (!run || !run.interrupts.length) return mount(pendingEl);
    mount(pendingEl, run.interrupts.map((it) => {
      const card = interruptCard(it, {
        persona: app.persona.get(),
        onSubmit: async (value) => {
          await stream({ command: { resume: buildResume(run.interrupts, { [it.id]: value }) } });
        },
      });
      return h('div', { class: 'cp-interrupt' }, card.el);
    }));
  }
  const paint = rafThrottle(() => {
    renderLive();
    renderPending();
    status.textContent = busy ? (run?.counts.tokens ? `${run.counts.tokens} tokens` : 'working...') : '';
    stopBtn.hidden = !busy;
    sendBtn.disabled = busy;
    follow.follow();
  });

  // ---- actions -----------------------------------------------------------------------------------------------
  async function ensureThread() {
    if (threadId) return threadId;
    const t = await app.api.lgCreateThread({ metadata: { foghorn_graph: GRAPH, foghorn_kind: 'chat', foghorn_trigger: 'ui' } });
    threadId = t.thread_id;
    lsSet('copilot_thread', threadId);
    return threadId;
  }
  async function stream(body) {
    busy = true;
    run = run || createRunState({ threadId });
    beginRun(run, null);
    run.interrupts = [];
    // each turn starts with clean lanes so the live bubble shows only the current answer
    run.lanes = {};
    run.laneOrder = [];
    paint();
    await new Promise((resolve) => {
      handle = streamRun({
        threadId,
        assistantId,
        modes: MODES,
        ...body,
        onEvent: (ev) => {
          applySSE(run, ev);
          paint();
        },
        onState: async (s, info) => {
          if (s === 'closed' || s === 'error') {
            if (s === 'error') mount(pendingEl, errorBox(info.error, null, 'The copilot stream failed'));
            finishRun(run);
            await commit();
            resolve();
          }
        },
      });
    });
    busy = false;
    handle = null;
    paint();
  }
  async function commit() {
    try {
      const st = await app.api.lgState(threadId);
      const msgs = messagesFromValues(st.values);
      if (msgs.length) {
        transcript = msgs;
        const ints = [];
        for (const t of st.tasks || []) for (const i of t.interrupts || []) ints.push(normalizeInterrupt(i));
        if (ints.length && !run.interrupts.length) run.interrupts = ints.filter(Boolean);
        run.lanes = {};
        run.laneOrder = [];
      }
    } catch {
      /* keep the live view */
    }
    renderCommitted();
    renderLive();
    renderPending();
  }
  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = '';
    try {
      await ensureThread();
    } catch (e) {
      mount(pendingEl, errorBox(e, null, 'Cannot start a conversation'));
      return;
    }
    transcript = [...transcript, { id: `u${Date.now()}`, role: 'user', text, toolCalls: [] }];
    renderCommitted();
    follow.pin();
    await stream({ input: { messages: [{ role: 'user', content: text }] }, metadata: { foghorn_trigger: 'ui' } });
  }
  function stop() {
    if (handle) handle.close();
    if (run?.runId && threadId) app.api.lgCancel(threadId, run.runId, 'interrupt').catch(() => {});
    busy = false;
    if (run) finishRun(run);
    paint();
  }
  function reset() {
    stop();
    threadId = null;
    lsSet('copilot_thread', null);
    transcript = [];
    run = null;
    renderCommitted();
    mount(liveEl);
    mount(pendingEl);
  }
  async function hydrate() {
    if (!threadId) return renderCommitted();
    try {
      const st = await app.api.lgState(threadId);
      transcript = messagesFromValues(st.values);
      const ints = [];
      for (const t of st.tasks || []) for (const i of t.interrupts || []) ints.push(normalizeInterrupt(i));
      if (ints.length) {
        run = createRunState({ threadId });
        run.interrupts = ints.filter(Boolean);
        run.status = 'interrupted';
        renderPending();
      }
    } catch {
      threadId = null;
      lsSet('copilot_thread', null);
    }
    renderCommitted();
  }
  function toggle(force) {
    open = force === undefined ? !open : force;
    panel.hidden = !open;
    document.body.classList.toggle('copilot-open', open);
    if (open) {
      if (!committedEl.firstChild) hydrate();
      setTimeout(() => input.focus(), 50);
    }
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && open && panel.contains(document.activeElement)) toggle(false);
  });
  return { toggle, el: panel, send };
}
