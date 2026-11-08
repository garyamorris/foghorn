"""State, input and output of ``storm_correlator`` (plain JSON; custom reducers on the fan-in channels)."""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict

from foghorn.contracts.inputs import GraphResult, StormInput
from foghorn.flows.storm_correlator.clustering import merge_clusters

Input = StormInput


class StormState(TypedDict, total=False):
    scenario_id: str
    alerts: list[dict[str, Any]]  # the raw envelopes of the input
    as_of: str | None
    scenario: str | None  # a REAL scenario id (tools usable) or None for stream-only alerts
    distinct: list[dict[str, Any]]  # normalized distinct signal alerts
    ignored: list[dict[str, Any]]
    comps: dict[str, dict[str, Any]]
    total_weight: float
    candidates: list[dict[str, Any]]
    chaos_target: str | None
    clusters: Annotated[dict[str, dict[str, Any]], merge_clusters]  # map + merge results, keyed by cluster id
    failures: Annotated[list[dict[str, Any]], operator.add]
    level: int  # merge-loop counter
    bound_hit: bool
    final: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    unexplained: list[str]
    incidents: Annotated[list[dict[str, Any]], operator.add]
    child_threads: Annotated[list[str], operator.add]
    result: GraphResult


class Output(TypedDict):
    result: GraphResult
    final: list[dict[str, Any]]
