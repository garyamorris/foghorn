"""Fixtures for the evaluator tests: CommanderReport-shaped dicts, built by hand and derived from the real ground truth."""

from __future__ import annotations

import copy
from typing import Any

from foghorn.data import api as data


def report(**over: Any) -> dict[str, Any]:
    """A complete, neutral CommanderReport (every key of contracts.inputs.CommanderReport)."""
    base: dict[str, Any] = {
        "scenario_id": "S10",
        "incident_id": "INC-12345",
        "severity": "SEV2",
        "root_cause": None,
        "abstained": False,
        "hypotheses": [],
        "evidence_refs": [],
        "actions_proposed": [],
        "actions_executed": [],
        "recovered": None,
        "approvals": [],
        "interrupts_seen": [],
        "comms_draft": "",
        "memory_recalled": [],
        "memory_written": [],
        "flags": [],
        "tool_calls_used": 0,
        "supervisor_hops": 0,
        "narrative": "",
    }
    base.update(copy.deepcopy(over))
    return base


def good(sid: str) -> dict[str, Any]:
    """A perfect report for scenario ``sid`` derived from its truth (tests may read truth)."""
    t = data.get_truth(sid)
    rc, rem, comms = t["root_cause"], t["remediation"], t["comms"]
    abstain = bool(rc) and rc["category"] == "telemetry_gap"
    pref_a, _, pref_t = rem["preferred"].partition(":")
    executed = [
        {"action": pref_a, "target": pref_t, "params": {}, "t_virtual": "2026-01-01T00:00:00Z", "outcome": "executed"}
    ]
    if rem.get("forbidden_patterns"):  # S11: the index SQL a human fixed to CONCURRENTLY
        executed[0]["params"] = {"sql": "CREATE INDEX CONCURRENTLY idx ON order_lines(order_id)"}
    refs = [f"{k['kind']}:{k['ref']}" for k in t["key_evidence"]]
    draft = ""
    if comms.get("status_page"):
        draft = (
            "We are seeing degraded service affecting "
            + ", ".join(comms["must_mention"])
            + ". Our team is working on it."
        )
    flags = ["red_herring_rejected"] if t["red_herrings"] else []
    if "prompt_injection_detected" in t["flags"]:
        flags.append("injection_detected")
    if abstain:
        flags.append("abstained")
    sev = t["severity"] if t["severity"] != "none" else "SEV4"
    return report(
        scenario_id=sid,
        severity=sev,
        root_cause=None
        if (abstain or not rc)
        else {"component": rc["component"], "category": rc["category"], "confidence": 0.9, "evidence_refs": refs},
        abstained=abstain,
        evidence_refs=refs,
        actions_proposed=[{"action": pref_a, "target": pref_t, "params": {}, "risk": "medium"}],
        actions_executed=executed,
        recovered=None if (abstain or t["is_noise"]) else True,
        interrupts_seen=[i["kind"] for i in t["expected_interrupts"]],
        comms_draft=draft,
        memory_recalled=[t["expected_memory"]["recall"]] if t["expected_memory"]["recall"] else [],
        flags=flags,
        tool_calls_used=max(t["budget"]["max_tool_calls"] - 1, 0),
        supervisor_hops=max(t["budget"]["max_supervisor_hops"] - 1, 0),
    )


def run(fn: Any, sid: str, rep: dict[str, Any]) -> dict[str, float]:
    """Run an evaluator on a report vs the scenario's truth; returns {score key: score}."""
    res = fn({"scenario_id": sid}, {"report": rep}, data.get_truth(sid))
    res = [res] if not isinstance(res, list | tuple) else res
    return {s.key: s.score for s in res}
