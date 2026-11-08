"""Studio entry point for `incident_commander`. Thin on purpose: the server imports this file under a path-derived module name."""

from foghorn.flows.incident_commander.build import build_graph

graph = build_graph()
