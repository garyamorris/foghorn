"""State, input and output schemas of ``eval_runner`` (plain JSON only; pydantic never enters state)."""

from __future__ import annotations

from typing import Annotated, Any

from typing_extensions import TypedDict

from foghorn.contracts.inputs import EvalRunnerInput, GraphResult


class EvalInput(EvalRunnerInput, total=False):
    """``EvalRunnerInput`` plus optional fields (contracts allow graph owners to ADD optional keys)."""

    eval_run_id: str  # the dashboard's ``evr_0001``; derived from thread metadata / the thread id when absent
    label: str | None
    save_baseline: bool  # store this run as the baseline of (suite, profile) in the Store


def merge_cases(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Upsert by ``scenario_id`` (first-seen order): a retried / replayed branch cannot double-count a scenario."""
    out = list(left or [])
    index = {c.get("scenario_id"): i for i, c in enumerate(out)}
    for case in right or []:
        sid = case.get("scenario_id")
        if sid in index:
            out[index[sid]] = case
        else:
            index[sid] = len(out)
            out.append(case)
    return out


class EvalState(EvalInput, total=False):
    plan: list[dict[str, Any]]  # [{scenario_id, title, tier}] in run order
    parent_thread: str
    commander: str  # 'real' (SDK) | 'stub' (a local stand-in): a baseline only gates runs of the same kind
    cases: Annotated[list[dict[str, Any]], merge_cases]
    summary: dict[str, Any]
    result: GraphResult


class EvalOutput(TypedDict):
    result: GraphResult
    summary: dict[str, Any]
    cases: list[dict[str, Any]]


class BranchInput(TypedDict):
    """The ``Send`` payload of one map branch (one scenario)."""

    task: dict[str, Any]
    eval_run_id: str
    profile: str
    concurrency: int
    total: int
    parent_thread: str
