"""Lifecycle events: how graphs tell the rest of the system (and the live feed) what just happened.

Graphs never talk to the event bus directly. They call :func:`emit_event`, which builds a ``foghorn.event/1``
envelope and hands it to the installed *sink*. The events subsystem installs the real sink during the custom app's
lifespan (``install_sink``); with no sink (unit tests, a graph run in isolation) events are kept in a small
in-memory ring so tests can assert on them via :func:`capture`.

* The sink MUST be non-blocking and thread-safe (nodes may be sync (threadpool) or async). The real sink only
  enqueues; persistence happens on the bus' own task. Never do SQLite/IO inside the sink call.
* ``idempotency_key`` is mandatory on purpose: ``interrupt()`` re-runs a node from line 1 on resume, so every
  emission must be safely repeatable. Use a business key, e.g. ``f"approval.requested:{approval_id}"``.
* ``emit_event`` never raises: observability must not break a run.
"""

from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

from foghorn.contracts.events import ALL_TYPES, EventEnvelope, Severity, Subject, Trace
from foghorn.logging import get_logger

log = get_logger("lifecycle")

Sink = Callable[[dict[str, Any]], None]

_sink: Sink | None = None
_ring: deque[dict[str, Any]] = deque(maxlen=500)


def install_sink(sink: Sink | None) -> None:
    """Called by the events subsystem at startup (and by tests). ``None`` uninstalls."""
    global _sink
    _sink = sink


def emit_event(
    type: str,
    *,
    idempotency_key: str,
    component: str | None = None,
    severity: Severity = "info",
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    dedupe_key: str | None = None,
    scenario_id: str | None = None,
    occurred_at: str | None = None,
) -> None:
    try:
        if type not in ALL_TYPES:
            log.warning("unknown_lifecycle_event_type", type=type)
        env = EventEnvelope(
            type=type,
            source="foghorn.internal",
            severity=severity,
            occurred_at=occurred_at,
            subject=Subject(component=component),
            payload=payload or {},
            dedupe_key=dedupe_key,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            trace=Trace(scenario_id=scenario_id),
        ).to_state()
        _ring.append(env)
        if _sink is not None:
            _sink(env)
    except Exception as exc:
        log.warning("lifecycle_emit_failed", type=type, error=repr(exc))


@contextlib.contextmanager
def capture() -> Iterator[list[dict[str, Any]]]:
    """Tests: ``with capture() as events: ...`` collects every event emitted inside the block (no sink needed)."""
    out: list[dict[str, Any]] = []
    previous = _sink
    install_sink(lambda env: out.append(env))
    try:
        yield out
    finally:
        install_sink(previous)
