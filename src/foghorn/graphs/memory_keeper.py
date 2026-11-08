"""Studio entry point for `memory_keeper`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.memory_keeper.build import build_graph

graph = build_graph()
