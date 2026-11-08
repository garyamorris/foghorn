"""``build_graph`` for Studio / the server (no checkpointer, no store: the server injects both) and for tests."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.func import entrypoint
from langgraph.pregel import Pregel
from langgraph.store.base import BaseStore

from foghorn.flows.postmortem_pipeline.context import PostmortemContext
from foghorn.flows.postmortem_pipeline.workflow import postmortem_pipeline


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None, store: BaseStore | None = None, **_deps: Any
) -> Pregel[Any, Any, Any, Any]:
    """The workflow as a ``Pregel`` graph. ``checkpointer`` / ``store`` are for tests only: a module-level graph must not
    carry its own checkpointer (it crashes ``langgraph dev``)."""
    graph = entrypoint(checkpointer=checkpointer, store=store, context_schema=PostmortemContext)(postmortem_pipeline)
    graph.name = "postmortem_pipeline"
    return graph
