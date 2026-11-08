"""State of the memory keeper: flat keys per phase so Studio's state editor and time travel are meaningful."""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict

from foghorn.contracts.inputs import GraphResult


class KeeperState(TypedDict, total=False):
    # --- input (MemoryKeeperInput) ------------------------------------------------------------------------------
    mode: str
    incident_id: str
    scenario_id: str
    postmortem: dict[str, Any]
    # --- learn: load_source -> distill -> dedupe -> curate -> write ----------------------------------------------
    source: str  # "input" | "store" | "none"
    component: str | None
    candidates: list[dict[str, Any]]  # {id, text, category, confidence, action: new|reinforce, key, ns, near}
    incident_summary: str
    recalled: list[dict[str, Any]]
    decisions: list[
        dict[str, Any]
    ]  # per candidate: {id, action, key, ns, decision: save|edit|discard, text, decided_by}
    written: list[dict[str, Any]]  # {kind, ns, key, action, version}
    # --- consolidate: scan -> cluster -> (confirm_merge) -> apply -----------------------------------------------
    lessons: list[dict[str, Any]]  # compact rows of every lesson seen
    clusters: list[dict[str, Any]]  # duplicate groups {ns, keep, drop: [keys], scores}
    boosts: list[dict[str, Any]]  # {ns, key, from, to}
    stale: list[dict[str, Any]]  # {ns, key, last_seen}
    merge_decision: str  # "save" | "discard" | "none"
    report: dict[str, Any]
    result: GraphResult


class KeeperOutput(TypedDict):
    result: GraphResult
    written: list[dict[str, Any]]
    report: dict[str, Any]
