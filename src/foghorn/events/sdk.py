"""Thin seam over the LangGraph SDK client so everything above it is testable with a fake.

Production uses the in-process client ``get_client(url=None)``: no network hop, same process as the Agent Server.
(It bypasses custom auth by design - every Foghorn route authenticates itself before it gets here.)
Tests pass a fake object with the same ``threads`` / ``runs`` / ``assistants`` / ``store`` / ``crons`` attributes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

AgentClient = Any
ClientFactory = Callable[[], AgentClient]


def default_client_factory() -> AgentClient:
    from langgraph_sdk import get_client

    return get_client(url=None)


def flatten_interrupts(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """``thread["interrupts"]`` is ``{task_id: [{id, value, ...}]}``; return one flat list with the task id attached."""
    out: list[dict[str, Any]] = []
    raw = thread.get("interrupts") or {}
    if isinstance(raw, dict):
        for task_id, items in raw.items():
            for it in items or []:
                if isinstance(it, dict) and it.get("id"):
                    out.append({**it, "task_id": task_id})
    elif isinstance(raw, list):  # defensive: some versions return a flat list
        out.extend({**it} for it in raw if isinstance(it, dict) and it.get("id"))
    return out
