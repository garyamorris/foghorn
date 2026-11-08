"""Shared test helpers (import as `from tests.helpers import run_graph`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.types import Command


async def run_graph(
    graph: Any, input_: Any, *, resumes: Sequence[Any] = (), config: dict[str, Any] | None = None
) -> tuple[Any, list[Any]]:
    """Drive a graph to completion, answering each interrupt batch from `resumes` in order.

    Uses `version="v2"` (`GraphOutput(value, interrupts)`) so interrupts are data, not a magic `__interrupt__` key.
    A resume item may be a plain value (one pending interrupt) or a dict {interrupt_id: value} (several pending).
    Returns (final_value, every_interrupt_seen). Raises if the script runs out of resumes (a test bug, loudly).
    """
    cfg = config or {"configurable": {"thread_id": "t-run-graph"}}
    queue = list(resumes)
    seen: list[Any] = []
    out = await graph.ainvoke(input_, cfg, version="v2")
    while out.interrupts:
        seen.extend(out.interrupts)
        if not queue:
            raise AssertionError(f"graph interrupted with no resume left: {[i.value for i in out.interrupts]!r}")
        out = await graph.ainvoke(Command(resume=queue.pop(0)), cfg, version="v2")
    assert not queue, f"unused resumes (graph finished earlier than the test expected): {queue!r}"
    return out.value, seen
