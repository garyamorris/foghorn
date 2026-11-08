"""Studio entry point for `eval_runner`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.eval_runner.build import build_graph

graph = build_graph()
