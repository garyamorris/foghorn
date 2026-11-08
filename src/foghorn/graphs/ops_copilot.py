"""Studio entry point for `ops_copilot`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.ops_copilot.build import build_graph

graph = build_graph()
