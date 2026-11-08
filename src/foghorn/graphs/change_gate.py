"""Studio entry point for `change_gate`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.change_gate.build import build_graph

graph = build_graph()
