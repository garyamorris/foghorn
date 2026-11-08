"""Graph-to-graph calls INSIDE a running server, done the way that actually works (docs/design/02 section 1.11).

Pattern G: run the callee on a DERIVED CHILD THREAD through the in-process SDK client.

Why not the obvious ways:
* ``add_node("x", RemoteGraph(...))`` against the same server forwards the PARENT's thread_id and the callee
  queues behind the parent run on that thread -> deadlock.
* ``get_client()`` runs need a free worker slot; ``langgraph dev`` defaults to ONE (start with
  ``--n-jobs-per-worker 8``; the Makefile does).

Behaviour of :func:`delegate` (call it from a node):
* ``mode="await"``: start (or resume) the child, wait for it. If the child interrupts, the interrupt BUBBLES: the
  parent calls ``interrupt(child_payload)`` and, once answered, resumes the child with the answer. The child is a
  separate thread row in Studio (linked through metadata ``foghorn_parent_thread_id``).
* ``mode="background"``: fire and forget (``runs.create``); returns immediately with the child thread id.
* Idempotent restart: the child thread id is ``det_thread_id(child_graph, parent_thread, key)``; if the parent node
  re-runs (interrupt resume, retry) the existing child state is reused instead of starting a second child run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from langgraph_sdk import get_client

from foghorn.contracts.meta import M_GRAPH, M_PARENT_THREAD, RUN_STREAM_MODES, det_thread_id

ClientFactory = Callable[[], Any]
_client_factory: ClientFactory = lambda: get_client(url=None)  # noqa: E731 - in-process, no network hop


def set_client_factory(factory: ClientFactory | None) -> None:
    """Tests inject a fake SDK client; ``None`` restores the in-process client."""
    global _client_factory
    _client_factory = factory or (lambda: get_client(url=None))


def _is_conflict(exc: BaseException) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 409 or "409" in text or "conflict" in text or "already has an active run" in text


class DelegateResult(TypedDict):
    child_thread_id: str
    status: Literal["ok", "started", "interrupted"]
    values: dict[str, Any]


async def delegate(
    *,
    graph_id: str,
    key: str,
    config: RunnableConfig,
    input: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    mode: Literal["await", "background"] = "await",
    child_thread_id: str | None = None,
) -> DelegateResult:
    client = _client_factory()
    parent_tid = str(config["configurable"]["thread_id"])
    # default: derived from the parent thread + key. ``child_thread_id`` pins the child to a BUSINESS key instead
    # (e.g. det_thread_id("incident_commander", incident_id)) so repeated alerts for one incident share one thread.
    child_tid = child_thread_id or det_thread_id(graph_id, parent_tid, key)
    md = {**(metadata or {}), M_PARENT_THREAD: parent_tid, M_GRAPH: graph_id}
    await client.threads.create(thread_id=child_tid, if_exists="do_nothing", graph_id=graph_id, metadata=md)

    state = await client.threads.get_state(child_tid)
    pending = [i for t in state.get("tasks") or [] for i in (t.get("interrupts") or [])]
    already_started = bool(state.get("values")) or bool(state.get("next"))

    if mode == "background":
        if not already_started:
            try:
                await client.runs.create(
                    child_tid,
                    graph_id,
                    input=input,
                    context=context,
                    multitask_strategy="reject",
                    # the dashboard console joins/replays this run's stream later: it only exists if requested NOW
                    stream_mode=list(RUN_STREAM_MODES),
                    stream_subgraphs=True,
                    stream_resumable=True,
                )
            except Exception as exc:
                # Two alerts for one incident race: the thread has no state yet, but a run is already queued on it.
                # That is "joined", not a failure (the server answers 409 for multitask_strategy="reject").
                if not _is_conflict(exc):
                    raise
        return {"child_thread_id": child_tid, "status": "started", "values": dict(state.get("values") or {})}

    if not already_started:
        out = await client.runs.wait(child_tid, graph_id, input=input, context=context)
    elif pending:
        out = {**(state.get("values") or {}), "__interrupt__": pending}
    else:
        out = dict(state.get("values") or {})

    if out.get("__interrupt__"):
        answer = interrupt(out["__interrupt__"][0]["value"])  # bubble to the parent's human
        out = await client.runs.wait(child_tid, graph_id, command={"resume": answer}, context=context)
        if out.get("__interrupt__"):  # child asked again: surface it on the next resume cycle
            return {"child_thread_id": child_tid, "status": "interrupted", "values": dict(out)}
    return {"child_thread_id": child_tid, "status": "ok", "values": dict(out)}
