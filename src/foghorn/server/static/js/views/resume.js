// Resuming interrupts: prefer the Foghorn API (validates ids, records approval.decided); fall back to the
// built-in Agent Server (`Command(resume={interrupt_id: value})`) for threads Foghorn does not know about.
import { buildResume } from '../core/schemaform.js';

function nonce() {
  return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

export async function resumeOne(app, { threadId, graphId, interrupt, response, approvalId }) {
  const aid = approvalId || interrupt.value?.approval_id;
  if (aid) {
    try {
      return await app.api.resumeApproval(aid, { interrupt_id: interrupt.id, response, nonce: nonce() });
    } catch (e) {
      if (e.status !== 404) throw e;
    }
  }
  return resumeDirect(app, { threadId, graphId, interrupts: [interrupt], responses: { [interrupt.id]: response } });
}

export async function resumeMany(app, { threadId, graphId, interrupts, responses }) {
  if (interrupts.length === 1) return resumeOne(app, { threadId, graphId, interrupt: interrupts[0], response: responses[interrupts[0].id] });
  try {
    return await app.api.resumeBatch({ thread_id: threadId, responses, nonce: nonce() });
  } catch (e) {
    if (e.status !== 404) throw e;
  }
  return resumeDirect(app, { threadId, graphId, interrupts, responses });
}

export async function resumeDirect(app, { threadId, graphId, interrupts, responses }) {
  const resume = buildResume(interrupts, responses);
  const run = await app.api.lgCreateRun(threadId, {
    assistant_id: graphId || 'incident_commander',
    command: { resume },
    stream_mode: ['values', 'updates', 'messages-tuple', 'custom', 'tasks', 'checkpoints'],
    stream_subgraphs: true,
    stream_resumable: true,
    on_disconnect: 'continue',
  });
  return { status: 'resumed', thread_id: threadId, run_id: run?.run_id, via: 'agent-server' };
}
