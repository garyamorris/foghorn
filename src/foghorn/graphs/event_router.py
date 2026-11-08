"""Studio entry point for `event_router`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.event_router.build import build_graph

graph = build_graph()
