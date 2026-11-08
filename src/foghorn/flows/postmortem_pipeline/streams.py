"""Stream helper local to this flow.

BUG IN ``foghorn.common.stream.emit`` (reported to the integrator): its signature is ``emit(kind, key, *, agent, t_virtual,
**data)`` so documented payload fields called ``kind`` or ``key`` - ``artifact {name, kind, ref}``,
``evidence.found {id, kind, ...}``, ``memory.written {ns, key}`` - raise ``TypeError: got multiple values for argument``.
:func:`emit_safe` builds the same envelope with positional-only ``kind`` / ``ev_key`` so those fields can be used. The fix
upstream is one character: ``def emit(kind, key, /, *, ...)``.
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_stream_writer

from foghorn.common.stream import PAYLOAD_VERSION, StreamKind


def emit_safe(
    kind: StreamKind, ev_key: str, /, *, agent: str | None = None, t_virtual: str | None = None, **data: Any
) -> None:
    """Write one custom stream event (same envelope as ``common.stream.emit``); a silent no-op outside a run."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"v": PAYLOAD_VERSION, "kind": kind, "key": ev_key, "agent": agent, "t_virtual": t_virtual, "data": data})
