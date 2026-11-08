"""Narration: short, informative text built from REAL values (tool results / the digest). The offline model's scripts call
these functions, so every number a user sees streaming was computed, never hard-coded."""

from __future__ import annotations

from typing import Any

from foghorn.flows.rca_tournament.facts import Evidence, extract_evidence, hhmm


def evidence_of(results: list[tuple[str, dict[str, Any], dict[str, Any] | None]], agent: str) -> list[Evidence]:
    out: list[Evidence] = []
    for name, args, res in results:
        if res:
            out += extract_evidence(name, args, res, agent=agent)
    return out


def _clip(s: str, n: int = 190) -> str:
    return s if len(s) <= n else s[: n - 1] + "..."


def narrate_specialist(
    agent: str,
    task: dict[str, Any],
    results: list[tuple[str, dict[str, Any], dict[str, Any] | None]],
    terse: bool = False,
) -> str:
    ev = evidence_of(results, agent)
    by_kind: dict[str, list[Evidence]] = {}
    for e in ev:
        by_kind.setdefault(e["kind"], []).append(e)
    if not results:
        return f"{agent}: nothing to look at within my tool budget."
    clock = next((r for n, _a, r in results if n == "advance_time" and r), None)
    prefix = (
        f"Waited until {hhmm(clock['as_of'])} so the signal could firm up. " if clock and clock.get("as_of") else ""
    )
    if agent == "metrics_analyst":
        mat = sorted((e for e in by_kind.get("metric", []) if e["data"]["material"]), key=lambda e: -e["strength"])
        normal = [e for e in by_kind.get("metric", []) if not e["data"]["material"] and not e["data"]["flatline"]]
        flat = [e for e in by_kind.get("metric", []) if e["data"]["flatline"]]
        alerts = by_kind.get("alert", [])
        parts = []
        if mat:
            earliest = min(mat, key=lambda e: e["data"]["onset"])
            parts.append(
                f"{mat[0]['summary']}."
                + (
                    f" Earliest onset: {earliest['data']['service']} at {hhmm(earliest['data']['onset'])}."
                    if len(mat) > 1 and not terse
                    else ""
                )
            )
            if len(mat) > 2 and not terse:
                parts.append(f"{len(mat)} anomalous series in total.")
        if flat:
            parts.append(f"{flat[0]['summary']} - I cannot see {flat[0]['data']['service']}.")
        if normal:
            parts.append(
                f"{len(normal)} series checked and normal (e.g. {normal[0]['data']['service']} {normal[0]['data']['metric']})."
            )
        if alerts:
            parts.append(f"{len(alerts)} alert(s) in the last hour.")
        return _clip(prefix + " ".join(parts) or prefix + "no anomalous metric found.", 420)
    if agent == "logs_analyst":
        logs = sorted(by_kind.get("log", []), key=lambda e: -e["data"]["count"])
        inj = by_kind.get("injection", [])
        parts = [e["summary"] + "." for e in logs[:2]]
        if inj:
            parts.append(
                f"Warning: {inj[0]['data']['service']} logs carry embedded instructions; I treat them as data, flag them and do not act on them."
            )
        return _clip(" ".join(parts) or "No error signature found in the suspects' logs.", 420)
    if agent == "change_analyst":
        ch = by_kind.get("change", [])
        if not ch:
            return "No changes in the window."
        recent = ch[:2]
        txt = " ".join(f"{e['summary']} ({e['data']['minutes_before']:.0f} min before now)." for e in recent)
        more = f" {len(ch) - 2} older change(s) in the window." if len(ch) > 2 else ""
        lookback = f" Lookback {task['lookback_days']} days." if task.get("lookback_days") else ""
        return _clip(txt + more + lookback, 420)
    if agent == "topology_analyst":
        topo = by_kind.get("topology", [])
        return _clip(" ".join(e["summary"] + "." for e in topo[:2]) or "No topology found.", 420)
    runbooks = by_kind.get("runbook", [])
    incidents = by_kind.get("incident", [])
    parts = []
    if runbooks:
        parts.append(f"Best runbook hit: {runbooks[0]['summary']}.")
        trap = next((r for r in runbooks if r["data"]["trap"]), None)
        if trap is not None:
            parts.append(f"{trap['data']['runbook']} offers a never-automatic action: I will not follow it.")
    if incidents:
        parts.append(f"Similar past incident: {incidents[0]['summary']}.")
    return _clip(" ".join(parts) or "No matching runbook.", 420)


def narrate_supervisor(d: dict[str, Any], route: dict[str, Any], directives_echo: str = "", terse: bool = False) -> str:
    b = d["budget"]
    head = (directives_echo + " ") if directives_echo and d["budget"]["hops_used"] <= 1 else ""
    tail = (
        ""
        if terse
        else f" (hop {b['hops_used']}, {b['calls_used']} tool calls used of {b['calls_used'] + b['calls_left']})"
    )
    return _clip(head + route["reason"] + tail, 380)


def narrate_plan(
    hyp: dict[str, Any], plan: list[dict[str, Any]], severity: str, why_sev: str, memory: list[str]
) -> str:
    if not plan:
        return "No safe action can be derived from the evidence; I will escalate to the on-call human."
    a = plan[0]
    extra = (
        f" Companion: {plan[1]['action']}:{plan[1]['target']}." if len(plan) > 1 and plan[1].get("secondary") else ""
    )
    mem = f" This matches {memory[0]}." if memory else ""
    return _clip(
        f"{severity}: {why_sev}. Proposing {a['action']}:{a['target']} ({a['risk']} risk) - {a['rationale']}.{extra}{mem}",
        420,
    )


def narrate_question(q: dict[str, Any]) -> str:
    return str(q["question"])


def narrate_answer(
    question: str,
    action: dict[str, Any],
    policy: dict[str, Any],
    blast: dict[str, Any] | None,
    hyp: dict[str, Any] | None,
) -> str:
    bits = []
    if blast:
        bits.append(
            f"{action['target']} has {blast['total']} dependent caller(s); tier-0 exposed: {', '.join(blast['tier0']) or 'none'}"
        )
    bits.append(f"the action is {policy['risk']} risk and {'reversible' if policy['reversible'] else 'irreversible'}")
    if hyp:
        bits.append(
            f"the diagnosis ({hyp['category'].replace('_', ' ')} on {hyp['component']}) stands at {hyp['score']:.0%}"
        )
    return _clip(f"On '{question}': " + "; ".join(bits) + ".", 420)


def narrate_close(report: dict[str, Any]) -> str:
    rc = report["root_cause"]
    cause = (
        f"{rc['category'].replace('_', ' ')} on {rc['component']} ({rc['confidence']:.0%})"
        if rc
        else "no confident root cause (abstained)"
    )
    acts = ", ".join(f"{a['action']}:{a['target']}" for a in report["actions_executed"]) or "no action executed"
    rec = {True: "recovery verified", False: "not recovered", None: "recovery not verifiable"}[report["recovered"]]
    return _clip(
        f"{report['incident_id']} {report['severity']}: {cause}. {acts}; {rec}. {report['tool_calls_used']} tool calls, {report['supervisor_hops']} supervisor hops.",
        420,
    )
