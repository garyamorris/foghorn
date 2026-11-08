"""Static graph catalog (ARCHITECTURE.md section 3) and the Studio guide aggregator.

Per-graph guides are ``src/foghorn/flows/<graph_id>/GUIDE.md``: YAML front matter (``graph_id, title, summary,
features[], studio_steps[], sample_inputs[{name,input}]``) followed by markdown. Missing or malformed files never raise:
the catalog entry is returned with ``has_guide: false`` and a warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from foghorn.contracts.meta import GRAPH_IDS

FLOWS_DIR = Path(__file__).resolve().parents[1] / "flows"

#: graph_id -> (title, role, features) - mirrors the table in docs/ARCHITECTURE.md section 3
CATALOG: dict[str, tuple[str, str, list[str]]] = {
    "event_router": (
        "Event router",
        "Single ingress for events: validate, dedupe, enrich (retry + cache), classify, route; delegates to other graphs.",
        [
            "RetryPolicy",
            "CachePolicy(key_func)",
            "conditional edges",
            "Send fan-out",
            "Store",
            "graph-to-graph delegate",
            "context_schema",
        ],
    ),
    "incident_commander": (
        "Incident commander",
        "Supervisor multi-agent response: specialists, RCA, remediation plan, policy check, human approval, execute, verify, comms, close.",
        [
            "Command(goto)",
            "create_agent subgraphs",
            "Command.PARENT",
            "interrupt(response_schema)",
            "static breakpoints",
            "get_stream_writer",
            "messages streaming",
            "update_state / fork",
        ],
    ),
    "rca_tournament": (
        "RCA tournament",
        "Competing root-cause hypotheses scored in parallel; also a subgraph of the commander.",
        ["Send map-reduce", "custom reducers", "defer=True reduce node", "per-Send timeout"],
    ),
    "storm_correlator": (
        "Storm correlator",
        "Collapses an alert storm into causal clusters.",
        ["Send map-reduce", "TimeoutPolicy", "error_handler", "CachePolicy", "set_node_defaults"],
    ),
    "change_gate": (
        "Change gate",
        "Change-approval HITL gym: approve, edit, reject, ask, four-eyes, parallel interrupts.",
        ["multiple + parallel interrupt()", "resume-by-id maps", "Command(resume=)", "interrupt_before/after"],
    ),
    "postmortem_pipeline": (
        "Postmortem pipeline",
        "Drafts, reviews and publishes a postmortem from an incident thread.",
        ["Functional API", "@entrypoint / @task", "previous", "entrypoint.final", "task retries"],
    ),
    "ops_copilot": (
        "Ops copilot",
        "Chat copilot for operators with tools, memory and a tool-call review step.",
        ["create_agent + middleware", "ToolRuntime", "structured output", "in-tool interrupt", "Store read/write"],
    ),
    "shift_handoff": (
        "Shift handoff",
        "Deterministic, durable shift report (no LLM), cron-driven.",
        [
            "durability=sync",
            "crash recovery",
            "tasks/checkpoints streams",
            "breakpoints",
            "DeltaChannel",
            "native cron",
        ],
    ),
    "memory_keeper": (
        "Memory keeper",
        "Learns from closed incidents: writes memories and lessons, consolidates on cron, human curates.",
        ["Store namespaces", "semantic search", "memory_write interrupt", "cron consolidation"],
    ),
    "eval_runner": (
        "Eval runner",
        "Runs scenarios through the commander, scores them against ground truth, regression-gates against a baseline.",
        ["Send over scenarios", "SDK delegation with concurrency limit", "evaluators + LLM judge", "structured output"],
    ),
}

SETUP_STEPS = [
    "Run `make dev` (langgraph dev on port 2026 with --n-jobs-per-worker 8; never add --allow-blocking).",
    "Open https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2026 in Chrome (Studio needs internet and a LangSmith login for its web UI only; Foghorn itself runs offline).",
    "If your browser blocks the loopback server (Safari, Brave) start the server with --tunnel and use the printed URL.",
    "Pick a graph in the top-left selector; each card below lists the click-by-click steps and sample inputs.",
]


def parse_guide(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---\\n<yaml>\\n---\\n<markdown>``; raises ``ValueError`` when the front matter is missing/invalid."""
    if not text.startswith("---"):
        raise ValueError("front matter missing")
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise ValueError("front matter is not terminated")
    meta = yaml.safe_load(parts[0][3:]) or {}
    if not isinstance(meta, dict):
        raise ValueError("front matter must be a mapping")
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    return meta, body.lstrip("\n")


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def guide_entry(graph_id: str, flows_dir: Path = FLOWS_DIR) -> tuple[dict[str, Any], str | None]:
    title, role, features = CATALOG.get(graph_id, (graph_id, "", []))
    entry: dict[str, Any] = {
        "graph_id": graph_id,
        "title": title,
        "summary": role,
        "features": list(features),
        "studio_steps": [],
        "sample_inputs": [],
        "body": "",
        "has_guide": False,
    }
    path = flows_dir / graph_id / "GUIDE.md"
    if not path.is_file():
        return entry, None
    try:
        meta, body = parse_guide(path.read_text(encoding="utf-8"))
    except (ValueError, yaml.YAMLError, OSError) as exc:
        return entry, f"flows/{graph_id}/GUIDE.md: {exc}"
    samples = [s for s in _list(meta.get("sample_inputs")) if isinstance(s, dict) and "name" in s and "input" in s]
    entry.update(
        {
            "title": str(meta.get("title") or title),
            "summary": str(meta.get("summary") or role),
            "features": [str(f) for f in _list(meta.get("features"))] or list(features),
            "studio_steps": [str(s) for s in _list(meta.get("studio_steps"))],
            "sample_inputs": samples,
            "body": body,
            "has_guide": True,
        }
    )
    return entry, None


def build_guide(flows_dir: Path = FLOWS_DIR) -> tuple[list[dict[str, Any]], list[str]]:
    entries, warnings = [], []
    for gid in GRAPH_IDS:
        entry, warn = guide_entry(gid, flows_dir)
        entries.append(entry)
        if warn:
            warnings.append(warn)
    return entries, warnings
