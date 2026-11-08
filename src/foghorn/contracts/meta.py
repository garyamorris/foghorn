"""Thread metadata keys, deterministic ids and run-context keys shared by the dispatcher, graphs and dashboard.

Thread metadata is FLAT (Studio and ``threads.search(metadata=...)`` filter on top-level keys). The dashboard maps
Studio threads back to events/incidents through these keys, so they are a hard contract.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

#: namespace for uuid5 thread ids (fixed forever; changing it orphans every deterministic thread)
THREAD_NS = uuid.UUID("6f0c7a52-3a4e-5d6b-9b7e-0f06e6e7a001")

# --- thread metadata keys -----------------------------------------------------------------------------------------
M_EVENT_ID = "foghorn_event_id"
M_CORRELATION_ID = "foghorn_correlation_id"
M_INCIDENT_ID = "foghorn_incident_id"
M_SCENARIO_ID = "foghorn_scenario_id"
M_TRIGGER = "foghorn_trigger"  # "event" | "ui" | "cron" | "eval" | "replay" | "delegate"
M_EVAL_RUN_ID = "foghorn_eval_run_id"
M_KIND = (
    "foghorn_kind"  # "approval" | "incident" | "triage" | "storm" | "chat" | "handoff" | "memory" | "eval" | "change"
)
M_PARENT_THREAD = "foghorn_parent_thread_id"
M_GRAPH = "foghorn_graph"

# --- run context / configurable keys (assistants carry these; the dispatcher passes them per run) -----------------------
CTX_SCENARIO_ID = "scenario_id"
CTX_AS_OF = "as_of"
CTX_MODEL_PROFILE = "model_profile"  # competent | naive | cautious | gullible (offline model behaviour)
CTX_APPROVAL_POLICY = "approval_policy"  # strict | standard | auto_low_risk
CTX_BREAKPOINTS = "breakpoints"
CTX_MEMORY_ENABLED = "memory_enabled"
CTX_CHAOS = "chaos"
CTX_TOKEN_DELAY_MS = "token_delay_ms"  # noqa: S105 - a pacing knob, not a credential

MODEL_PROFILES = ("competent", "naive", "cautious", "gullible")

#: A BACKGROUND run only retains what its creator asked it to stream: the dashboard's console joins/replays that stream
#: later, so every run we create must request all of these, with subgraph streaming and a resumable stream.
RUN_STREAM_MODES = ("updates", "messages-tuple", "custom", "values", "tasks")

#: graph ids == langgraph.json keys == ``graph_id`` in run metadata
GRAPH_IDS = (
    "event_router",
    "incident_commander",
    "rca_tournament",
    "storm_correlator",
    "change_gate",
    "postmortem_pipeline",
    "ops_copilot",
    "shift_handoff",
    "memory_keeper",
    "eval_runner",
)


def det_thread_id(graph_id: str, *key_parts: str) -> str:
    """Deterministic thread id: the same (graph, key) always lands on the same thread.

    NOTE a deterministic thread id alone does NOT dedupe work (a second ``runs.create`` on it is a second run);
    dedupe is the event ledger's job. It exists so retries resume the SAME thread and so the dashboard can
    compute a thread id without a lookup.
    """
    return str(uuid.uuid5(THREAD_NS, "/".join((graph_id, *key_parts))))


def thread_metadata(kind: str, graph: str, **extra: Any) -> dict[str, Any]:
    """Flat thread metadata. ``extra`` keys are the ``M_*`` names (None values are dropped)."""
    md: dict[str, Any] = {M_KIND: kind, M_GRAPH: graph}
    md.update({k: v for k, v in extra.items() if v is not None})
    return md


def incident_id_for(correlation_id: str) -> str:
    """Deterministic human-friendly incident id (``INC-12345``) for a correlation id; the router and everything
    downstream derive the same id without a lookup."""
    n = int(hashlib.sha256(correlation_id.encode()).hexdigest()[:8], 16) % 90000 + 10000
    return f"INC-{n}"
