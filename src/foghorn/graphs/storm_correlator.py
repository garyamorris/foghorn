"""Studio entry point for `storm_correlator`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.storm_correlator.build import build_graph

graph = build_graph()
