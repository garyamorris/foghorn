"""Studio entry point for `shift_handoff`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.shift_handoff.build import build_graph

graph = build_graph()
