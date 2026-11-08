"""The final ``CommanderReport`` / ``GraphResult`` (pure): what evaluators, the postmortem pipeline and the dashboard read."""

from __future__ import annotations

from typing import Any, cast

from foghorn.contracts.inputs import CommanderReport, GraphResult
from foghorn.flows.rca_tournament.facts import Facts


def cited_refs(state: dict[str, Any]) -> list[str]:
    """Evidence refs the report stands on: what supports the leading hypothesis, every material symptom measured, the
    log signatures and changes that matter, the runbook / memory that guided the plan."""
    f = Facts.from_evidence(state.get("evidence"), state.get("as_of"))
    hyps = state.get("hypotheses") or []
    refs: list[str] = list(hyps[0]["evidence"]) if hyps else []
    for e in state.get("evidence") or []:
        k, d = e.get("kind"), e.get("data") or {}
        if (
            (k == "metric" and d.get("material"))
            or (k == "log" and e.get("strength", 0) > 0.4)
            or (k in ("memory", "incident") and (d.get("score") or 0) >= 0.45)
        ):
            refs.append(e["ref"])
    top = hyps[0] if hyps else None
    if top is not None:
        for ch in f.changes_on(top["component"]):
            if f"change:{ch['change_id']}" in refs:
                break
        rb = [r for r in f.runbooks if r["runbook"]]
        if rb:
            refs.append(f"runbook:{rb[0]['runbook']}")
    return list(dict.fromkeys(refs))


def build_report(state: dict[str, Any], narrative: str = "") -> CommanderReport:
    inc = state.get("incident") or {}
    diag = state.get("diagnosis") or {}
    runs = state.get("agent_runs") or []
    return {
        "scenario_id": str(state.get("scenario_id")),
        "incident_id": str(inc.get("id")),
        "severity": inc.get("severity", "SEV3"),
        "root_cause": diag.get("root_cause"),
        "abstained": bool(diag.get("abstained")),
        "hypotheses": cast(
            Any,
            [
                {k: h[k] for k in ("id", "component", "category", "score", "evidence")} | {"label": h.get("label", "")}
                for h in (state.get("hypotheses") or [])[:6]
            ],
        ),
        "evidence_refs": cited_refs(state),
        "actions_proposed": [
            {
                k: a.get(k)
                for k in ("action", "target", "params", "risk", "source", "blocked", "block_reason", "secondary")
            }
            for a in (state.get("proposed") or [])
        ],
        "actions_executed": [
            {k: a.get(k) for k in ("action", "target", "params", "t_virtual", "outcome", "ok", "reason")}
            for a in (state.get("executed") or [])
        ],
        "recovered": state.get("recovered"),
        "approvals": list(state.get("approvals") or []),
        "interrupts_seen": list(state.get("interrupts_seen") or []),
        "comms_draft": str((state.get("comms") or {}).get("published") or ""),
        "memory_recalled": list(state.get("memory_recalled") or []),
        "memory_written": list(state.get("memory_written") or []),
        "flags": list(state.get("flags") or []),
        "tool_calls_used": sum(int(r.get("tool_calls", 0)) for r in runs),
        "supervisor_hops": int(state.get("supervisor_hops") or 0),
        "narrative": narrative,
    }


def build_result(state: dict[str, Any], report: CommanderReport) -> GraphResult:
    rc = report["root_cause"]
    cause = f"{rc['category']} on {rc['component']}" if rc else "no confident root cause"
    acts = (
        ", ".join(f"{a['action']}:{a['target']}" for a in report["actions_executed"] if a.get("ok"))
        or "no action executed"
    )
    rec = {True: "recovery verified", False: "not recovered", None: "recovery not verifiable"}[report["recovered"]]
    ids = list(state.get("trigger_event_ids") or [])
    return {
        "graph_id": "incident_commander",
        "status": "ok",
        "event_id": ids[0] if ids else None,
        "correlation_id": state.get("correlation_id"),
        "summary": f"{report['incident_id']} {report['severity']}: {cause}; {acts}; {rec}",
        "artifacts": [
            {
                "kind": "report",
                "name": "commander_report",
                "ref": report["incident_id"],
                "data": {"root_cause": rc, "flags": report["flags"]},
            },
            {"kind": "text", "name": "status_page_draft", "ref": None, "data": report["comms_draft"]},
        ],
        "metrics": {
            "tool_calls": float(report["tool_calls_used"]),
            "supervisor_hops": float(report["supervisor_hops"]),
            "confidence": float(rc["confidence"]) if rc else 0.0,
            "recovered": 1.0 if report["recovered"] else 0.0,
            "interrupts": float(len(report["interrupts_seen"])),
        },
        "child_threads": [],
    }
