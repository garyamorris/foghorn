"""Studio entry point for `postmortem_pipeline`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.postmortem_pipeline.build import build_graph

graph = build_graph()
