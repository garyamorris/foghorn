"""Lifecycle events -> projections (incidents, approvals, eval runs).

Called inside the ingest transaction for every stored event, so the tables are always consistent with the event log
and the SSE broadcast. Lifecycle events are idempotent (``idempotency_key``), and every upsert here is monotonic:
a ``resolved`` incident never reopens, a ``decided`` approval never goes back to ``pending``.
"""

from __future__ import annotations

from typing import Any

from foghorn.events.db import StateDB

INCIDENT_TYPES = {"incident.opened", "incident.updated", "incident.resolved"}


def materialize(db: StateDB, ev: dict[str, Any]) -> None:
    t = ev["type"]
    if t in INCIDENT_TYPES:
        _incident(db, ev)
    elif t == "approval.requested":
        _approval_requested(db, ev)
    elif t == "approval.decided":
        _approval_decided(db, ev)
    elif t in {"eval.requested", "eval.progress", "eval.finished"}:
        _eval(db, ev)


def _incident(db: StateDB, ev: dict[str, Any]) -> None:
    p = ev.get("payload") or {}
    corr = ev.get("correlation_id")
    incident_id = p.get("incident_id")
    if not incident_id and corr:
        existing = db.query_one("SELECT incident_id FROM incidents WHERE correlation_id = ?", (corr,))
        incident_id = existing["incident_id"] if existing else None
    if not incident_id:
        incident_id = f"INC-D{db.next_n('incident'):04d}"
    cur = db.get_incident(incident_id)
    resolved = ev["type"] == "incident.resolved"
    status = "resolved" if resolved else (p.get("status") or (cur["status"] if cur else "open"))
    corr = corr or (cur or {}).get("correlation_id") or f"corr_{incident_id}"
    fields: dict[str, Any] = {
        "correlation_id": corr,
        "thread_id": p.get("thread_id"),
        "title": p.get("title"),
        "severity": p.get("severity"),
        "status": status,
        "scenario_id": p.get("scenario_id") or ev.get("scenario_id"),
        "component": ev.get("component"),
        "updated_at": ev["occurred_at"],
        "root_cause": p.get("root_cause"),
        "graph_id": p.get("graph_id"),
        "resolved_at": ev["occurred_at"] if resolved else None,
    }
    if cur is None:
        fields["opened_at"] = ev["occurred_at"]
    db.upsert_incident(incident_id, **fields)
    if ev.get("correlation_id") != corr:
        db.set_event_correlation(ev["id"], corr)
    thread_id = p.get("thread_id") or (cur or {}).get("thread_id")
    if thread_id:
        db.execute(
            "UPDATE dispatches SET incident_id = ? WHERE thread_id = ? AND incident_id IS NULL",
            (incident_id, thread_id),
        )
        db.execute(
            "UPDATE approvals SET incident_id = ? WHERE thread_id = ? AND incident_id IS NULL", (incident_id, thread_id)
        )


def _approval_requested(db: StateDB, ev: dict[str, Any]) -> None:
    p = ev.get("payload") or {}
    approval_id = p.get("approval_id") or f"apr_{db.next_n('approval'):04d}"
    cur = db.get_approval(approval_id)
    if cur and cur["status"] != "pending":
        return  # never reopen a decided approval
    incident_id = p.get("incident_id")
    if not incident_id and p.get("thread_id"):
        row = db.query_one("SELECT incident_id FROM incidents WHERE thread_id = ?", (p["thread_id"],))
        incident_id = row["incident_id"] if row else None
    db.upsert_approval(
        approval_id,
        kind=p.get("kind") or "approval",
        status="pending",
        thread_id=p.get("thread_id"),
        interrupt_id=p.get("interrupt_id"),
        run_id=p.get("run_id"),
        title=p.get("title") or p.get("action"),
        summary=p.get("summary"),
        risk=p.get("risk"),
        incident_id=incident_id,
        graph_id=p.get("graph_id"),
        requested_at=(cur or {}).get("requested_at") or ev["occurred_at"],
        payload=p.get("payload") if isinstance(p.get("payload"), dict) else None,
        options=p.get("options") if isinstance(p.get("options"), list) else None,
    )


def _approval_decided(db: StateDB, ev: dict[str, Any]) -> None:
    p = ev.get("payload") or {}
    approval_id = p.get("approval_id")
    if not approval_id:
        return
    decision = {"decision": p.get("decision"), "note": p.get("note")}
    cur = db.get_approval(approval_id)
    db.upsert_approval(
        approval_id,
        status="decided",
        decided_at=ev["occurred_at"],
        decided_by=p.get("decided_by"),
        decision=decision,
        thread_id=p.get("thread_id"),
        interrupt_id=p.get("interrupt_id"),
        kind=p.get("kind"),
        requested_at=None if cur else ev["occurred_at"],
    )
    # the payload/schema are only meaningful while pending
    db.execute("UPDATE approvals SET payload = NULL, response_schema = NULL WHERE approval_id = ?", (approval_id,))


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def normalize_case(case: Any) -> dict[str, Any] | None:
    """The one persisted shape of an eval case (docs/API.md section 9). Unknown extra keys are kept untouched.

    ``scores`` maps evaluator name -> number; ``score`` defaults to their mean; ``expected`` / ``actual`` are objects
    (what the ground truth says vs what the run produced); ``incident_id`` / ``thread_id`` / ``run_id`` link to the run.
    """
    if not isinstance(case, dict) or not case.get("scenario_id"):
        return None
    raw_scores = case.get("scores")
    scores = (
        {str(k): n for k, v in raw_scores.items() if (n := _num(v)) is not None} if isinstance(raw_scores, dict) else {}
    )
    score = _num(case.get("score"))
    if score is None and scores:
        score = round(sum(scores.values()) / len(scores), 6)
    return {
        **case,
        "scenario_id": str(case["scenario_id"]),
        "score": score,
        "passed": case["passed"] if isinstance(case.get("passed"), bool) else None,
        "scores": scores,
        "incident_id": case.get("incident_id"),
        "thread_id": case.get("thread_id"),
        "run_id": case.get("run_id"),
        "expected": case["expected"] if isinstance(case.get("expected"), dict) else None,
        "actual": case["actual"] if isinstance(case.get("actual"), dict) else None,
    }


def _merge_cases(existing: list[dict[str, Any]], incoming: list[Any]) -> list[dict[str, Any]]:
    """Upsert by ``scenario_id`` keeping first-seen order."""
    out = list(existing)
    index = {c.get("scenario_id"): i for i, c in enumerate(out)}
    for raw in incoming:
        case = normalize_case(raw)
        if case is None:
            continue
        if case["scenario_id"] in index:
            out[index[case["scenario_id"]]] = case
        else:
            index[case["scenario_id"]] = len(out)
            out.append(case)
    return out


def _eval(db: StateDB, ev: dict[str, Any]) -> None:
    p = ev.get("payload") or {}
    eid = p.get("eval_run_id")
    if not eid:
        return
    row = db.get_eval_run(eid)
    if row is None:
        db.execute(
            "INSERT INTO eval_runs(eval_run_id,status,suite,profile,label,scenario_ids,created_at,event_id) VALUES(?,?,?,?,?,?,?,?)",
            (eid, "requested", p.get("suite"), p.get("profile"), p.get("label"), "[]", ev["occurred_at"], ev["id"]),
        )
        row = db.get_eval_run(eid)
    assert row is not None
    if ev["type"] == "eval.requested":
        db.update_eval_run(eid, event_id=ev["id"])
        return
    if ev["type"] == "eval.progress":
        if row["status"] == "finished":
            return
        incoming = p["cases"] if isinstance(p.get("cases"), list) else [p.get("case")]
        cases = _merge_cases(list(row["cases"]), incoming)
        db.update_eval_run(
            eid,
            status="running",
            done=int(p.get("done", len(cases))),
            total=int(p.get("total", row["progress"]["total"])),
            cases=cases,
        )
    else:  # eval.finished
        cases = _merge_cases(list(row["cases"]), p["cases"]) if isinstance(p.get("cases"), list) else list(row["cases"])
        total = max(int(row["progress"]["total"]), len(cases))
        db.update_eval_run(
            eid,
            status="finished",
            finished_at=ev["occurred_at"],
            summary=p.get("summary") or {},
            cases=cases,
            done=total,
            total=total,
        )
