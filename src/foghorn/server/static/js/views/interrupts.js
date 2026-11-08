// A group of pending interrupts on ONE thread. One interrupt resumes immediately; several are staged one by one and
// resumed together with a single {interrupt_id: decision} map ("Resume all"), as LangGraph requires.
import { h, mount } from '../core/dom.js';
import { badge, button, asyncClick, errorMessage } from '../core/ui.js';
import { interruptCard } from './forms.js';
import { resumeMany, resumeOne } from './resume.js';

const askedLog = new Map(); // interrupt/approval id -> [questions]

export function interruptGroup(app, { threadId, graphId, interrupts, onResumed, onStaged }) {
  const multi = interrupts.length > 1;
  const staged = {};
  const cards = [];
  const resumeAll = button('Resume all', { variant: 'primary', icon: 'play', disabled: true, title: 'Send every staged decision in one resume map' });
  const status = h('span', { class: 'small muted' });
  const refreshStatus = () => {
    const n = Object.keys(staged).length;
    status.textContent = `${n} of ${interrupts.length} decisions staged`;
    resumeAll.disabled = n !== interrupts.length;
    if (onStaged) onStaged(n);
  };

  interrupts.forEach((it) => {
    const key = it.value?.approval_id || it.id;
    const card = interruptCard(it, {
      persona: app.persona.get(),
      asked: askedLog.get(key) || [],
      onSubmit: async (value, { action }) => {
        if (action === 'ask' && value.question) {
          askedLog.set(key, [...(askedLog.get(key) || []), value.question]);
        }
        if (!multi) {
          const r = await resumeOne(app, { threadId, graphId, interrupt: it, response: value });
          app.toast({ title: action === 'reject' ? 'Rejected' : action === 'ask' ? 'Question sent' : 'Decision sent', body: it.value?.title || '', tone: 'ok', ttl: 3500 });
          if (onResumed) onResumed(r, { action, interrupt: it });
          return;
        }
        staged[it.id] = value;
        const note = card.el.querySelector('.staged');
        const label = value.decision || value.answer || 'staged';
        if (note) note.remove();
        card.el.appendChild(h('div', { class: 'staged row' }, badge(`staged: ${String(label).slice(0, 40)}`, 'ok', { dot: true }), button('Undo', { size: 'sm', variant: 'ghost', onclick: () => { delete staged[it.id]; card.el.querySelector('.staged')?.remove(); refreshStatus(); } })));
        refreshStatus();
      },
    });
    cards.push(card);
  });

  asyncClick(resumeAll, async () => {
    const r = await resumeMany(app, { threadId, graphId, interrupts, responses: staged });
    app.toast({ title: 'Resumed all', body: `${interrupts.length} decisions sent`, tone: 'ok', ttl: 3500 });
    if (onResumed) onResumed(r, { action: 'batch' });
  }, (e) => app.toast({ title: 'Resume failed', body: errorMessage(e), tone: 'critical' }));

  const el = h('div', { class: 'col gap-4 igroup' }, cards.map((c) => c.el), multi && h('div', { class: 'row wrap igroup-foot' }, resumeAll, status));
  if (multi) refreshStatus();
  return { el, cards };
}

export function askedFor(key) {
  return askedLog.get(key) || [];
}
