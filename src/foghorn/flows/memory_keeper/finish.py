"""The ``finish`` node: builds the ``GraphResult`` every handler graph exposes. Pure."""

from __future__ import annotations

from typing import Any

from foghorn.contracts.inputs import Artifact, GraphResult

from .state import KeeperState

CONSOLIDATE_METRICS = (
    "scanned",
    "duplicate_groups",
    "merged_groups",
    "deleted",
    "duplicates_marked",
    "boosted",
    "stale_flagged",
)


def _count(decisions: list[dict[str, Any]], what: str) -> float:
    return float(sum(1 for d in decisions if d["decision"] == what))


def _consolidation(state: KeeperState) -> GraphResult:
    r = state.get("report") or {}
    return GraphResult(
        graph_id="memory_keeper",
        status="ok",
        event_id=None,
        correlation_id=None,
        summary=(
            f"Consolidated {r.get('scanned', 0)} lessons: {r.get('merged_groups', 0)} merged group(s), "
            f"{r.get('boosted', 0)} boosted, {r.get('stale_flagged', 0)} flagged stale."
        ),
        artifacts=[
            Artifact(
                kind="report", name="consolidation", ref=f"store:reports/consolidation/{r.get('report_key')}", data=r
            )
        ],
        metrics={k: float(r.get(k, 0)) for k in CONSOLIDATE_METRICS},
        child_threads=[],
    )


def _learned(state: KeeperState) -> GraphResult:
    written = state.get("written") or []
    decisions = state.get("decisions") or []
    lessons = [w for w in written if w["kind"] == "lessons"]
    added = sum(1 for w in lessons if w["action"] == "added")
    reinforced = sum(1 for w in lessons if w["action"] == "reinforced")
    return GraphResult(
        graph_id="memory_keeper",
        status="ok",
        event_id=None,
        correlation_id=None,
        summary=(
            f"Learned from {state.get('incident_id')}: {len(lessons)} lesson write(s) ({added} new, {reinforced} reinforced), "
            f"{int(_count(decisions, 'discard'))} discarded."
        ),
        artifacts=[Artifact(kind="table", name="memory writes", ref=None, data=written)],
        metrics={
            "candidates": float(len(state.get("candidates") or [])),
            "saved": _count(decisions, "save"),
            "edited": _count(decisions, "edit"),
            "discarded": _count(decisions, "discard"),
            "written": float(len(written)),
            "recalled": float(len(state.get("recalled") or [])),
        },
        child_threads=[],
    )


def _skipped(state: KeeperState) -> GraphResult:
    return GraphResult(
        graph_id="memory_keeper",
        status="skipped",
        event_id=None,
        correlation_id=None,
        summary=f"No postmortem found for {state.get('incident_id') or 'this run'}: nothing to learn.",
        artifacts=[],
        metrics={},
        child_threads=[],
    )


async def finish(state: KeeperState) -> dict[str, Any]:
    if state.get("report"):
        result = _consolidation(state)
    elif state.get("postmortem"):
        result = _learned(state)
    else:
        result = _skipped(state)
    return {"result": result}
