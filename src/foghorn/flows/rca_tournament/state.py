"""State / schemas of the RCA tournament. The same graph runs standalone (``RcaInput`` -> ``RcaOutput``) and, in its
``embedded`` variant, as a compiled-subgraph node of the incident commander: the shared keys are ``scenario_id``,
``as_of``, ``evidence``, ``candidates``, ``hypotheses`` and ``red_herrings``; ``scores``/``pairs`` are private to the
tournament (they never leak into the parent)."""

from __future__ import annotations

from typing import Annotated, Any

from typing_extensions import TypedDict

from foghorn.contracts.inputs import GraphResult, RcaInput
from foghorn.flows.rca_tournament.facts import merge_by_id


def last_wins(old: Any, new: Any) -> Any:
    """Parallel writers (two sub-agents returning the same shared key) must not raise: the newest non-None value wins."""
    return new if new is not None else old


class RcaState(TypedDict, total=False):
    scenario_id: Annotated[str, last_wins]
    as_of: Annotated[str | None, last_wins]
    evidence: Annotated[list[dict[str, Any]], merge_by_id]
    candidates: list[str]
    hypotheses: list[dict[str, Any]]
    red_herrings: list[dict[str, Any]]
    # private to the tournament
    scores: Annotated[list[dict[str, Any]], merge_by_id]
    pairs: list[list[str]]
    result: GraphResult


class RcaOutput(TypedDict):
    result: GraphResult
    hypotheses: list[dict[str, Any]]
    red_herrings: list[dict[str, Any]]


class WorkerArg(TypedDict):
    """What one ``Send`` carries to a ``score_hypothesis`` worker (a compact copy of the evidence)."""

    component: str
    category: str
    evidence: list[dict[str, Any]]
    profile: str
    fixed_score: float | None
    fixed_for: list[str]


__all__ = ["RcaInput", "RcaOutput", "RcaState", "WorkerArg", "last_wins"]
