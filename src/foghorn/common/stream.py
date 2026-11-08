"""Custom stream events (``stream_mode="custom"``): ONE envelope for every graph, produced by ONE helper.

    {"v": 1, "kind": <kind>, "key": <deterministic key>, "agent": <node or None>, "t_virtual": <iso or None>,
     "data": {...}}

* ``key`` is deterministic (e.g. ``"evidence:metric:checkout-service:error_rate"``) so the dashboard can drop
  duplicates that a resumed node re-emits (interrupt() re-runs the node from line 1).
* graph / node path are NOT in the payload: clients derive them from the SSE namespace (``custom|node:<task_id>``),
  identically at any subgraph depth.
* Outside a run (unit tests calling a node function directly) ``emit`` is a silent no-op.

Kinds and their ``data`` shape (the dashboard renders these without knowing the graph):
  progress        {label, pct}
  log             {level, text}
  metric          {name, value, unit?}
  evidence.found  {id, kind, ref, summary, strength}
  series          {service, metric, points: [[t, v], ...<=60], baseline, onset?}
  hypothesis.update {id, label, score, delta, for: [], against: []}
  plan.proposed   {actions: [...]}
  memory.recalled {ns, key, score, summary}
  memory.written  {ns, key}
  policy.check    {rule, result, text}
  decision        {what, why}
  handoff         {to, reason}
  artifact        {name, kind, ref}
  note            {text}
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.config import get_stream_writer

StreamKind = Literal[
    "progress",
    "log",
    "metric",
    "evidence.found",
    "series",
    "hypothesis.update",
    "plan.proposed",
    "memory.recalled",
    "memory.written",
    "policy.check",
    "decision",
    "handoff",
    "artifact",
    "note",
]
STREAM_KINDS: tuple[str, ...] = StreamKind.__args__  # type: ignore[attr-defined]
PAYLOAD_VERSION = 1


def stream_payload(
    kind: StreamKind, key: str, /, *, agent: str | None = None, t_virtual: str | None = None, **data: Any
) -> dict[str, Any]:
    return {"v": PAYLOAD_VERSION, "kind": kind, "key": key, "agent": agent, "t_virtual": t_virtual, "data": data}


def emit(kind: StreamKind, key: str, /, *, agent: str | None = None, t_virtual: str | None = None, **data: Any) -> None:
    """Write one custom stream event. Safe to call from sync or async nodes and from tests."""
    try:
        writer = get_stream_writer()
    except RuntimeError:  # called outside a graph run
        return
    writer(stream_payload(kind, key, agent=agent, t_virtual=t_virtual, **data))
