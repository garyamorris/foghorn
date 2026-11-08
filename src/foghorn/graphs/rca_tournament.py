"""Studio entry point for `rca_tournament`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.rca_tournament.build import build_graph

graph = build_graph()
